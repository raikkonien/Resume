import json
from datetime import date, datetime, timezone
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

from app import db
from app.dashboard import build_dashboard
from app.sections import BASIC, ENTRY_SECTIONS, SECTIONS, Section, event_year, parse_form, record_label

from support import ADMIN_TEST_PASSWORD, original_user_id

class EntryStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._patch(db, "DATA_DIR", self.tmp_dir)
        self._patch(db, "DB_PATH", self.tmp_dir / "resume.db")
        self._patch(db, "INITIAL_ADMIN_PASSWORD", ADMIN_TEST_PASSWORD)
        self.section = SECTIONS["work"]

    def _patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def open_db(self):
        return closing(db.connect())

    @property
    def user_id(self):
        return original_user_id()

    def test_migration_adds_sort_order_and_preserves_order_across_reinit(self):
        # Simulate a database created before sort_order existed: plain id order.
        with closing(sqlite3.connect(db.DB_PATH)) as raw, raw:
            raw.execute(
                'CREATE TABLE "work" (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                '"begin_date" TEXT, "end_date" TEXT, "employer" TEXT, "title" TEXT)'
            )
            raw.executemany(
                'INSERT INTO "work" (employer) VALUES (?)',
                [("Employer A",), ("Employer B",), ("Employer C",)],
            )

        db.init_db()

        with self.open_db() as conn:
            rows = db.list_rows(conn, self.section, self.user_id)
        self.assertEqual([row["employer"] for row in rows], ["Employer A", "Employer B", "Employer C"])
        ids = [row["id"] for row in rows]

        # A custom order set after migration must survive repeated init_db calls
        # (e.g. app restarts), not be reset back to id order.
        with self.open_db() as conn:
            db.reorder_rows(conn, self.section, [ids[2], ids[0], ids[1]], self.user_id)
        db.init_db()
        with self.open_db() as conn:
            rows_again = db.list_rows(conn, self.section, self.user_id)
        self.assertEqual([row["id"] for row in rows_again], [ids[2], ids[0], ids[1]])

    def test_reorder_persists_and_is_shared_through_list_rows(self):
        db.init_db()
        with self.open_db() as conn:
            ids = [db.insert_row(conn, self.section, {"employer": f"E{i}"}, self.user_id) for i in range(3)]
        reordered = [ids[2], ids[0], ids[1]]

        with self.open_db() as conn:
            db.reorder_rows(conn, self.section, reordered, self.user_id)

        # Reconnecting (a fresh sqlite3 connection, simulating another request
        # or process restart) must see the same persisted order.
        with self.open_db() as conn:
            rows = db.list_rows(conn, self.section, self.user_id)
        self.assertEqual([row["id"] for row in rows], reordered)

    def test_archive_then_insertion_appends_after_remaining_active_rows(self):
        db.init_db()
        with self.open_db() as conn:
            ids = [db.insert_row(conn, self.section, {"employer": f"E{i}"}, self.user_id) for i in range(3)]
        with self.open_db() as conn:
            db.reorder_rows(conn, self.section, [ids[2], ids[0], ids[1]], self.user_id)
        with self.open_db() as conn:
            db.archive_row(conn, self.section, ids[0], self.user_id)
        with self.open_db() as conn:
            new_id = db.insert_row(conn, self.section, {"employer": "New"}, self.user_id)

        with self.open_db() as conn:
            rows = db.list_rows(conn, self.section, self.user_id)
        self.assertEqual([row["id"] for row in rows], [ids[2], ids[1], new_id])

    def test_reorder_rejects_invalid_permutation_and_leaves_order_unchanged(self):
        db.init_db()
        with self.open_db() as conn:
            ids = [db.insert_row(conn, self.section, {"employer": f"E{i}"}, self.user_id) for i in range(3)]

        cases = [
            ids[:2],                              # missing an id
            [*ids, ids[0]],                        # duplicate id
            [ids[0], ids[1], 9999],                # unknown id
            [ids[0], ids[1], "not-an-int"],        # non-integer id
            [True, ids[1], ids[2]],                # bool is not an accepted id, even if == 1
        ]
        for bad_ids in cases:
            with self.subTest(bad_ids=bad_ids):
                with self.assertRaises(ValueError):
                    with self.open_db() as conn:
                        db.reorder_rows(conn, self.section, bad_ids, self.user_id)

        with self.open_db() as conn:
            rows = db.list_rows(conn, self.section, self.user_id)
        self.assertEqual([row["id"] for row in rows], ids)

    def test_archive_preserves_row_and_file_restore_uses_same_id_at_active_tail(self):
        db.init_db()
        owner = self.user_id
        projects = SECTIONS["projects"]
        with self.open_db() as conn:
            first = db.insert_row(conn, projects, {"name": "First", "contract": "projects/1/contract.pdf",
                                                  "contract_name": "原附件.pdf"}, owner)
            second = db.insert_row(conn, projects, {"name": "Second"}, owner)
            user = db.get_user(conn, owner)
            attachment = self.tmp_dir / user["username"] / "projects" / "1" / "contract.pdf"
            attachment.parent.mkdir(parents=True)
            attachment.write_bytes(b"%PDF-original")
            before = db.get_row(conn, projects, first, owner)
            db.archive_row(conn, projects, first, owner)
            self.assertEqual([row["id"] for row in db.list_rows(conn, projects, owner)], [second])
            self.assertIsNone(db.get_row(conn, projects, first, owner))
            archived = db.list_rows(conn, projects, owner, archived=True)
            self.assertEqual([row["id"] for row in archived], [first])
            self.assertIsNotNone(archived[0]["archived_at"])
            for key, value in before.items():
                if key != "archived_at":
                    self.assertEqual(archived[0][key], value)
            self.assertEqual(attachment.read_bytes(), b"%PDF-original")
            with self.assertRaises(ValueError):
                db.archive_row(conn, projects, first, owner)
            with self.assertRaises(ValueError):
                db.reorder_rows(conn, projects, [first, second], owner)
            third = db.insert_row(conn, projects, {"name": "Third"}, owner)
            db.restore_row(conn, projects, first, owner)
            self.assertEqual([row["id"] for row in db.list_rows(conn, projects, owner)], [second, third, first])
            self.assertEqual(db.get_row(conn, projects, first, owner)["contract"], before["contract"])
            self.assertEqual(db.list_rows(conn, projects, owner, archived=True), [])
            self.assertEqual(attachment.read_bytes(), b"%PDF-original")
            with self.assertRaises(ValueError):
                db.restore_row(conn, projects, first, owner)

    def test_archiving_restoring_and_scoped_inclusion_cannot_target_another_owner(self):
        db.init_db()
        owner = self.user_id
        with self.open_db() as conn:
            other = db.create_user(conn, "colleague", "colleague-password")["id"]
            row_id = db.insert_row(conn, self.section, {"employer": "Private"}, owner)
            for operation in (db.archive_row, db.restore_row):
                with self.assertRaises(ValueError):
                    operation(conn, self.section, row_id, other)
            db.archive_row(conn, self.section, row_id, owner)
            self.assertIsNone(db.get_row(conn, self.section, row_id, other, include_archived=True))
            self.assertEqual(db.list_rows(conn, self.section, other, archived=True), [])
            for operation in (db.archive_row, db.restore_row):
                with self.assertRaises(ValueError):
                    operation(conn, BASIC, db.get_basic(conn, owner)["id"], owner)
            self.assertEqual(db.list_rows(conn, BASIC, owner, archived=True), [])

    def test_events_only_record_completed_creates_actual_changes_and_forced_file_replacements(self):
        db.init_db()
        owner = self.user_id
        with self.open_db() as conn:
            discarded = db.insert_row(conn, self.section, {"employer": "Failed upload"}, owner)
            db._discard_new_row(conn, self.section, discarded, owner)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM record_events").fetchone()[0], 0)
            row_id = db.insert_row(conn, self.section, {"employer": "First"}, owner)
            db.update_row(conn, self.section, row_id, {"employer": "First"}, owner, activity_action="create")
            db.update_row(conn, self.section, row_id, {"employer": "First"}, owner)
            db.update_row(conn, self.section, row_id, {}, owner)
            db.update_row(conn, self.section, row_id, {"employer": "Changed"}, owner)
            db.update_row(conn, self.section, row_id, {"employer": "Changed"}, owner, force_activity=True)
            other = db.create_user(conn, "colleague", "colleague-password")["id"]
            db.update_row(conn, self.section, row_id, {"employer": "Foreign"}, other, force_activity=True)
            db.update_row(conn, self.section, 999, {}, owner, activity_action="create")
            db.reorder_rows(conn, self.section, [row_id], owner)
            second = db.insert_row(conn, self.section, {"employer": "Second"}, owner)
            db.reorder_rows(conn, self.section, [second, row_id], owner)
            db.reorder_rows(conn, self.section, [second, row_id], owner)
            db.archive_row(conn, self.section, row_id, owner)
            db.restore_row(conn, self.section, row_id, owner)
            basic = db.get_basic(conn, owner)
            db.update_row(conn, BASIC, basic["id"], {"name": "Person", "photo": "basic/photo.png"}, owner)
            db.update_row(conn, BASIC, basic["id"], {"photo": None}, owner)
            events = [dict(row) for row in conn.execute("SELECT * FROM record_events ORDER BY id")]
            self.assertEqual([row["action"] for row in events],
                             ["create", "update", "update", "reorder", "archive", "restore", "update", "update"])
            self.assertEqual([row["title"] for row in events],
                             ["First", "Changed", "Changed", self.section.label, "Changed", "Changed",
                              "个人基本信息", "个人基本信息"])
            self.assertIsNone(events[3]["entry_id"])
            for row in events:
                self.assertEqual(row["user_id"], owner)
                self.assertEqual(datetime.fromisoformat(row["occurred_at"]).tzinfo, timezone.utc)
            self.assertIsNone(db.get_row(conn, self.section, discarded, owner))

    def test_event_write_failure_rolls_back_record_update_archive_restore_and_reorder(self):
        db.init_db()
        owner = self.user_id
        with self.open_db() as conn:
            first = db.insert_row(conn, self.section, {"employer": "Original"}, owner)
            second = db.insert_row(conn, self.section, {"employer": "Second"}, owner)
            conn.execute(
                "CREATE TRIGGER fail_events BEFORE INSERT ON record_events "
                "BEGIN SELECT RAISE(ABORT, 'event failure'); END"
            )
            for operation in (
                lambda: db.update_row(conn, self.section, first, {"employer": "Must roll back"}, owner),
                lambda: db.archive_row(conn, self.section, first, owner),
                lambda: db.reorder_rows(conn, self.section, [second, first], owner),
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    operation()
            self.assertEqual(db.get_row(conn, self.section, first, owner)["employer"], "Original")
            self.assertEqual([row["id"] for row in db.list_rows(conn, self.section, owner)], [first, second])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM record_events").fetchone()[0], 0)
            conn.execute("DROP TRIGGER fail_events")
            db.archive_row(conn, self.section, first, owner)
            conn.execute(
                "CREATE TRIGGER fail_events BEFORE INSERT ON record_events "
                "BEGIN SELECT RAISE(ABORT, 'event failure'); END"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                db.restore_row(conn, self.section, first, owner)
            self.assertIsNone(db.get_row(conn, self.section, first, owner))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM record_events").fetchone()[0], 1)

    def test_new_business_date_migration_does_not_invent_historical_years(self):
        db.init_db()
        owner = self.user_id
        originals = {}
        new_fields = (("software", "year"), ("standards", "publish_date"))
        with self.open_db() as conn, conn:
            for key, new_field in new_fields:
                current = SECTIONS[key]
                old = Section(key, current.label, tuple(field for field in current.fields if field.key != new_field))
                conn.execute(f'DROP TABLE "{key}"')
                db._create_section(conn, old, key)
                values = {"name": "Undated historical record"}
                if key == "software":
                    values.update({
                        "holder": "Original holder", "reg_number": "2024SR123456",
                        "reg_date": "2024-02-29", "cert_number": "Original certificate", "rank": "2/7",
                        "certificate": "software/41/certificate.pdf", "certificate_name": "原证书.pdf",
                    })
                id = db.insert_row(conn, old, values, owner)
                if key == "software":
                    with conn:
                        conn.execute(
                            "UPDATE software SET id = 41, sort_order = 7, created_at = ? WHERE id = ?",
                            ("2026-10-11 12:00:00", id),
                        )
                    id = 41
                originals[key] = (id, db.get_row(conn, old, id, owner))
        db.init_db()
        db.init_db()
        with self.open_db() as conn:
            year_column = next(row for row in conn.execute("PRAGMA table_info(software)") if row["name"] == "year")
            self.assertEqual(year_column["type"], "INTEGER")
            self.assertEqual(year_column["notnull"], 0)
            self.assertIsNone(year_column["dflt_value"])
            dashboard = build_dashboard(conn, owner, today=date(2026, 10, 12))
            software = next(section for section in dashboard["sections"] if section["key"] == "software")
            self.assertEqual(software["counts"], [0, 0, 0])
            self.assertEqual(software["undated_count"], 1)
            for key, new_field in new_fields:
                id, before = originals[key]
                row = db.get_row(conn, SECTIONS[key], id, owner)
                self.assertIsNone(row[new_field])
                self.assertIsNone(event_year(SECTIONS[key], row))
                for field, value in before.items():
                    self.assertEqual(row[field], value, field)
                form = {field.key: before.get(field.key) or "" for field in SECTIONS[key].fields if field.type != "file"}
                form[new_field] = "2025" if key == "software" else "2026-10-11"
                parsed = parse_form(SECTIONS[key], form)
                db.update_row(conn, SECTIONS[key], id, parsed, owner)
        db.init_db()
        with self.open_db() as conn:
            for key, new_field in new_fields:
                id, before = originals[key]
                row = db.get_row(conn, SECTIONS[key], id, owner)
                self.assertEqual(row[new_field], 2025 if key == "software" else "2026-10-11")
                self.assertEqual(event_year(SECTIONS[key], row), 2025 if key == "software" else 2026)
                for field, value in before.items():
                    self.assertEqual(row[field], value, field)
            dashboard = build_dashboard(conn, owner, today=date(2026, 10, 12))
            software = next(section for section in dashboard["sections"] if section["key"] == "software")
            self.assertEqual(software["counts"], [0, 1, 0])
            self.assertEqual(software["current_count"], 0)
            self.assertEqual(software["undated_count"], 0)

class PaperStorageTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(db, "DATA_DIR", root))
        self.enterContext(patch.object(db, "DB_PATH", root / "resume.db"))
        self.enterContext(patch.object(db, "INITIAL_ADMIN_PASSWORD", ADMIN_TEST_PASSWORD))
        self.papers = SECTIONS["papers"]
        self.authors = [
            {"name": "张瑞", "affiliation": "浙江大学；研究院",
             "is_first": True, "is_corresponding": True},
            {"name": "Zoë Li", "affiliation": "Université de Montréal",
             "is_first": True, "is_corresponding": False},
        ]

    def open_db(self):
        return closing(db.connect())

    @property
    def user_id(self):
        return original_user_id()

    def test_form_database_roundtrip_update_and_profile_isolation(self):
        db.init_db()
        owner = self.user_id
        values = parse_form(self.papers, {
            "title": "原题目", "authors": json.dumps(self.authors, ensure_ascii=False),
            "volume": "32", "issue": "3", "pages": "760–771",
            "cas_quartile": "一区", "jcr_quartile": "Q2",
        })
        with self.open_db() as conn:
            row_id = db.insert_row(conn, self.papers, values, owner)
            colleague = db.create_user(conn, "colleague", "colleague-password")["id"]
        with self.open_db() as conn:
            row = db.get_row(conn, self.papers, row_id, owner)
            self.assertEqual(row["authors"], self.authors)
            self.assertEqual(db.list_rows(conn, self.papers, owner)[0], row)
            raw = conn.execute("SELECT authors FROM papers WHERE id = ?", (row_id,)).fetchone()[0]
            self.assertIsInstance(raw, str)
            self.assertIn("张瑞", raw)
            self.assertEqual(json.loads(raw), self.authors)
            self.assertIsNone(db.get_row(conn, self.papers, row_id, colleague))
            self.assertEqual(db.list_rows(conn, self.papers, colleague), [])
            db.update_row(conn, self.papers, row_id, {"authors": []}, colleague)
            self.assertEqual(db.get_row(conn, self.papers, row_id, owner)["authors"], self.authors)
            reversed_authors = list(reversed(self.authors))
            db.update_row(conn, self.papers, row_id, {"authors": reversed_authors}, owner)
            self.assertEqual(db.get_row(conn, self.papers, row_id, owner)["authors"], reversed_authors)
            db.update_row(conn, self.papers, row_id, {"authors": []}, owner)
            row = db.get_row(conn, self.papers, row_id, owner)
            self.assertEqual(row["authors"], [])
            self.assertEqual([row[key] for key in ("title", "volume", "issue", "pages", "cas_quartile", "jcr_quartile")],
                             ["原题目", "32", "3", "760–771", "一区", "Q2"])
            empty_id = db.insert_row(conn, self.papers, {"title": "No authors yet"}, owner)
            self.assertEqual(db.get_row(conn, self.papers, empty_id, owner)["authors"], [])
            book_id = db.insert_row(conn, SECTIONS["books"], {"authors": "旧式论著作者"}, owner)
            self.assertEqual(db.get_row(conn, SECTIONS["books"], book_id, owner)["authors"], "旧式论著作者")

    def test_additive_paper_migration_preserves_existing_rows_and_attachment_paths(self):
        db.init_db()
        owner = self.user_id
        old_keys = {"year", "journal", "title", "rank", "authorship", "doi", "article_pdf"}
        old_section = Section("papers", "文章", tuple(field for field in self.papers.fields if field.key in old_keys))
        with self.open_db() as conn, conn:
            # Recreate only the empty test table with the pre-enhancement schema.
            conn.execute("DROP TABLE papers")
            db._create_section(conn, old_section, "papers")
            original = {
                "year": 2025, "journal": "Original journal", "title": "原文章",
                "rank": "2/7", "authorship": "共同第一作者", "doi": "10.1234/original",
                "article_pdf": "papers/1/article_pdf.pdf", "article_pdf_name": "原附件.pdf",
            }
            row_id = db.insert_row(conn, old_section, original, owner)
            before = db.get_row(conn, old_section, row_id, owner)
        db.init_db()
        db.init_db()
        with self.open_db() as conn:
            after = db.get_row(conn, self.papers, row_id, owner)
            for key, value in before.items():
                self.assertEqual(after[key], value, key)
            self.assertEqual(after["authors"], [])
            for key in ("volume", "issue", "pages", "cas_quartile", "jcr_quartile"):
                self.assertIsNone(after[key], key)
            self.assertIsNone(conn.execute("SELECT authors FROM papers WHERE id = ?", (row_id,)).fetchone()[0])
            self.assertEqual(db.list_rows(conn, self.papers, owner), [after])



class BusinessYearTests(unittest.TestCase):
    def test_dates_are_strict_and_priority_is_business_year_not_storage_or_end_year(self):
        cases = [
            ("papers", {"year": 2026}, 2026),
            ("papers", {"year": "2026"}, 2026),
            ("annual_reviews", {"year": "2026"}, 2026),
            ("patents", {"grant_date": "2025-02-28", "apply_date": "2024-01-01"}, 2025),
            ("patents", {"grant_date": "2025-02-29", "apply_date": "2026-01-01"}, None),
            ("patents", {"grant_date": "2025-not-a-date", "apply_date": "2026-01-01"}, None),
            ("patents", {"apply_date": "2026-01-01"}, None),
            ("work", {"begin_date": "2024-12-31", "end_date": "2026-01-01"}, 2024),
            ("appointments", {"begin_date": "2024-12-31", "end_date": "2026-01-01"}, 2024),
            ("positions", {"begin_date": "2024-12-31"}, 2024),
            ("projects", {"begin_date": "2024-12-31", "end_date": "2026-01-01", "close_date": "2026-02-02"}, 2024),
            ("trainings", {"begin_date": "2024-12-31", "end_date": "2026-01-01"}, 2024),
            ("education", {"award_year": 2026, "begin_date": "2024-01-01", "end_date": "2025-01-01"}, 2026),
            ("education", {"begin_date": "2024-01-01", "end_date": "2026-01-01"}, None),
            ("education", {"award_year": "2025x", "begin_date": "2024-01-01"}, None),
            ("memberships", {"begin_month": "2025-12"}, 2025),
            ("memberships", {"begin_month": "2025-13"}, None),
            ("software", {"reg_number": "2026xyz", "created_at": "2026-10-11"}, None),
            ("software", {"reg_date": "2026-10-11"}, None),
            ("software", {"year": 2025, "reg_date": "2026-10-11"}, 2025),
            ("software", {"year": "2025", "reg_date": "2026-10-11"}, 2025),
            ("software", {"year": "2025x", "reg_date": "2026-10-11"}, None),
            ("standards", {"publish_date": "2026-10-11"}, 2026),
            ("basic", {"birth_month": "2000-01"}, None),
        ]
        for key, row, expected in cases:
            with self.subTest(key=key, row=row):
                self.assertEqual(event_year(SECTIONS[key], row), expected)
        for key, field in (("papers", "year"), ("software", "year"), ("education", "award_year")):
            for value in (True, 2026.0, "2026x", "2026-01-01", " 2026", 1899, 2101, None, ""):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(event_year(SECTIONS[key], {field: value}))
            for value in (1900, 2100):
                with self.subTest(key=key, value=value):
                    self.assertEqual(event_year(SECTIONS[key], {field: value}), value)
        for key, field in (("work", "begin_date"), ("appointments", "begin_date"),
                           ("positions", "begin_date"), ("projects", "begin_date"),
                           ("trainings", "begin_date"), ("patents", "grant_date")):
            for value in ("2026-1-01", "2026-01-1", "2026-04-31", "2026-01-01junk",
                          "2101-01-01", "1899-12-31", None, ""):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(event_year(SECTIONS[key], {field: value}))

    def test_record_labels_and_metadata_cover_every_section(self):
        self.assertEqual(record_label(BASIC, {"name": "Person"}), "个人基本信息")
        for section in ENTRY_SECTIONS:
            self.assertIn(section.title_field, {field.key for field in section.fields})
            self.assertTrue(section.year_fields)
            self.assertEqual(record_label(section, {"id": 7}), f"{section.label} #7")
            self.assertEqual(record_label(section, {section.title_field: "  Title  ", "id": 7}), "Title")
            self.assertEqual(len(record_label(section, {section.title_field: "长" * 121})), 120)


if __name__ == "__main__":
    unittest.main()
