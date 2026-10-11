import json
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
from app.sections import SECTIONS, Section, parse_form

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

    def test_deletion_then_insertion_appends_after_remaining_rows(self):
        db.init_db()
        with self.open_db() as conn:
            ids = [db.insert_row(conn, self.section, {"employer": f"E{i}"}, self.user_id) for i in range(3)]
        with self.open_db() as conn:
            db.reorder_rows(conn, self.section, [ids[2], ids[0], ids[1]], self.user_id)
        with self.open_db() as conn:
            db.delete_row(conn, self.section, ids[0], self.user_id)
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



if __name__ == "__main__":
    unittest.main()
