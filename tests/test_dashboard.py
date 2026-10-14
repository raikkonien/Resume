from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from app import dashboard, db
from app.sections import BASIC, ENTRY_SECTIONS, SECTIONS, event_year
from support import http_client, isolated_store


TODAY = date(2026, 10, 11)


def author(name, affiliation=""):
    return {"name": name, "affiliation": affiliation,
            "is_first": True, "is_corresponding": True}


class BusinessYearTests(unittest.TestCase):
    def test_business_dates_do_not_use_end_dates_or_creation_time(self):
        expected = {
            "work": "begin_date", "contracts": "begin_date", "annual_reviews": "year",
            "appointments": "begin_date", "positions": "begin_date", "education": "award_year",
            "papers": "year", "patents": "grant_date", "software": "year",
            "projects": "begin_date", "transfers": "sign_date", "books": "pub_date",
            "memberships": "begin_month", "standards": "publish_date",
            "trainings": "begin_date", "conferences": "begin_date", "licenses": "begin_date",
        }
        for key, field in expected.items():
            with self.subTest(section=key):
                value = "2024" if field in ("year", "award_year") else (
                    "2024-02" if field == "begin_month" else "2024-02-29")
                row = {"end_date": "2026-01-01", "close_date": "2026-01-01",
                       "award_year": 2026, "created_at": "2026-01-01T00:00:00+00:00",
                       field: value}
                self.assertEqual(event_year(SECTIONS[key], row), 2024)
        self.assertIsNone(event_year(BASIC, {"birth_month": "2026-01"}))

    def test_strict_year_date_month_parsing_and_bounds(self):
        for key, field in (("papers", "year"), ("education", "award_year"), ("software", "year")):
            for value in (None, "", " 2026", "2026 ", "2026extra", "2026-01-01", True, False,
                          2026.0, "２０２６", "1899", 2101):
                with self.subTest(section=key, year=value):
                    self.assertIsNone(event_year(SECTIONS[key], {field: value}))
            for value in (1900, "1900", 2100, "2100"):
                with self.subTest(section=key, year=value):
                    self.assertEqual(event_year(SECTIONS[key], {field: value}), int(value))
        for value in ("2026-02-29", "2026-13-01", "2026-01-32", "2026-1-01", "20261011",
                      "2026-01-01T00:00:00", "2026.01.01", "1899-01-01", "2101-01-01", True):
            with self.subTest(date=value):
                self.assertIsNone(event_year(SECTIONS["work"], {"begin_date": value}))
        for value in ("2026-00", "2026-13", "2026-1", "2026-01-01", "2026.01", True):
            with self.subTest(month=value):
                self.assertIsNone(event_year(SECTIONS["memberships"], {"begin_month": value}))
        self.assertEqual(event_year(SECTIONS["work"], {"begin_date": "2024-02-29"}), 2024)
        self.assertEqual(event_year(SECTIONS["memberships"], {"begin_month": "2026-12"}), 2026)

    def test_patents_require_grant_date_and_never_fall_back_to_application_date(self):
        patents = SECTIONS["patents"]
        self.assertEqual(event_year(patents, {"grant_date": "2025-03-01", "apply_date": "2024-01-01"}), 2025)
        self.assertIsNone(event_year(patents, {"apply_date": "2026-01-01"}))
        for invalid in (None, "", "2025-02-30", "2101-01-01", True):
            with self.subTest(grant_date=invalid):
                self.assertIsNone(event_year(patents, {"grant_date": invalid, "apply_date": "2026-01-01"}))

    def test_missing_years_do_not_use_other_dates_or_storage_and_identifier_years(self):
        for key in ("software", "standards", "education", "projects"):
            with self.subTest(section=key):
                self.assertIsNone(event_year(SECTIONS[key], {
                    "reg_number": "2026SR001", "number": "2026-01", "certificate_name": "2026.pdf",
                    "created_at": "2026-01-01", "end_date": "2026-01-01", "close_date": "2026-01-01",
                    "reg_date": "2026-01-01",
                }))
        self.assertIsNone(event_year(SECTIONS["education"], {"begin_date": "2026-01-01"}))
        self.assertEqual(event_year(SECTIONS["education"], {
            "award_year": 2026, "begin_date": "2024-01-01", "end_date": "2025-01-01",
        }), 2026)
        self.assertEqual(event_year(SECTIONS["software"], {"year": 2026, "reg_date": "2024-01-01"}), 2026)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(db, "ADMIN_PASSWORD", ""))
        self.enterContext(patch.object(db, "MCP_TOKEN", ""))
        self.root = isolated_store(self)
        self.conn = self.enterContext(closing(db.connect()))
        self.owner = db.get_user_by_username(self.conn, "admin")["id"]
        self.other = db.create_user(self.conn, "colleague", "colleague-password")["id"]

    def add(self, key, values, owner=None):
        return db.insert_row(self.conn, SECTIONS[key], values, self.owner if owner is None else owner)

    def payload(self, today=TODAY):
        return dashboard.build_dashboard(self.conn, self.owner, today=today)

    def event(self, key, entry_id, action="update", title="Event", owner=None,
              timestamp="2026-10-11T01:02:03+00:00"):
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO record_events (user_id, section_key, entry_id, action, title, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self.owner if owner is None else owner, key, entry_id, action, title, timestamp),
            )
        return cursor.lastrowid

    def fixture(self):
        papers = [self.add("papers", {"year": year, "title": f"Paper {year}"})
                  for year in (2024, 2025, 2026)]
        granted = self.add("patents", {"name": "Granted", "grant_date": "2025-02-01",
                                        "apply_date": "2024-02-01"})
        applied = self.add("patents", {"name": "Applied", "apply_date": "2026-02-01"})
        project = self.add("projects", {"name": "Project", "begin_date": "2024-01-01",
                                        "end_date": "2026-01-01", "close_date": "2026-01-01"})
        software = self.add("software", {"name": "Registered", "year": 2026, "reg_date": "2024-03-01"})
        undated = self.add("software", {"name": "Undated", "reg_number": "2026SR0001",
                                        "reg_date": "2026-03-01"})
        review = self.add("annual_reviews", {"year": "2026"})
        archived = self.add("papers", {"year": 2026, "title": "Archived"})
        db.archive_row(self.conn, SECTIONS["papers"], archived, self.owner)
        foreign = self.add("papers", {"year": 2026, "title": "Foreign"}, self.other)
        return {"papers": papers, "granted": granted, "applied": applied, "project": project,
                "software": software, "undated": undated, "review": review,
                "archived": archived, "foreign": foreign}

    def test_empty_account_has_no_fabricated_records_or_events(self):
        payload = self.payload()
        self.assertEqual(set(payload), {"year", "years", "current_total", "undated_total", "sections", "recent", "graph"})
        self.assertEqual(payload["years"], [2024, 2025, 2026])
        self.assertEqual(payload["current_total"], 0)
        self.assertEqual(payload["undated_total"], 0)
        self.assertEqual(payload["recent"], [])
        self.assertTrue(all(section["counts"] == [0, 0, 0] and section["undated_count"] == 0
                            for section in payload["sections"]))
        self.assertEqual([(node["id"], node["kind"], node["value"]) for node in payload["graph"]["nodes"]],
                         [("self", "self", 0)])
        self.assertEqual(payload["graph"]["edges"], [])
        basic = db.get_basic(self.conn, self.owner)
        db.update_row(self.conn, BASIC, basic["id"], {"name": "Owner", "birth_month": "2026-01"}, self.owner)
        after = self.payload()
        self.assertEqual(after["current_total"], 0)
        self.assertEqual(after["graph"]["nodes"][0]["label"], "Owner")
        self.assertEqual(len(after["graph"]["nodes"]), 1)

    def test_fixed_fixture_aggregates_owner_active_records_once(self):
        ids = self.fixture()
        payload = self.payload()
        self.assertEqual(payload["current_total"], 3)
        self.assertEqual(payload["undated_total"], 2)
        self.assertEqual([s["key"] for s in payload["sections"]], [s.key for s in ENTRY_SECTIONS])
        expected = {"papers": [1, 1, 1], "patents": [0, 1, 0], "projects": [1, 0, 0],
                    "software": [0, 0, 1], "annual_reviews": [0, 0, 1]}
        for section in payload["sections"]:
            self.assertEqual(set(section), {"key", "label", "current_count", "counts", "undated_count"})
            self.assertEqual(section["counts"], expected.get(section["key"], [0, 0, 0]))
            self.assertEqual(section["current_count"], section["counts"][2])
            self.assertEqual(section["undated_count"], int(section["key"] in ("patents", "software")))
        graph = payload["graph"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        records = [node for node in nodes.values() if node["kind"] == "record"]
        self.assertEqual(len(records), 9)
        self.assertTrue(all(node["value"] == 1 for node in records))
        self.assertNotIn(f"record:papers:{ids['archived']}", nodes)
        self.assertNotIn(f"record:papers:{ids['foreign']}", nodes)
        self.assertNotIn("section:work", nodes)
        self.assertEqual(nodes["self"]["value"], 9)
        self.assertIsNone(nodes[f"record:software:{ids['undated']}"]["year"])
        self.assertIsNone(nodes[f"record:patents:{ids['applied']}"]["year"])
        self.assertEqual({node["year"]: node["value"] for node in nodes.values() if node["kind"] == "year"},
                         {2024: 2, 2025: 2, 2026: 3})
        for node in nodes.values():
            self.assertEqual(set(node), {"id", "kind", "label", "section", "entry_id", "year", "value"})
        for edge in graph["edges"]:
            self.assertEqual(set(edge), {"source", "target", "kind", "evidence"})
            self.assertIn(edge["source"], nodes)
            self.assertIn(edge["target"], nodes)
        record_edges = {edge["target"]: edge["source"] for edge in graph["edges"] if edge["kind"] == "record"}
        for node in records:
            self.assertEqual(record_edges[node["id"]], f"section:{node['section']}")
        section_edges = {edge["target"]: edge["source"] for edge in graph["edges"] if edge["kind"] == "section"}
        for node in nodes.values():
            if node["kind"] == "section":
                self.assertEqual(section_edges[node["id"]], "self")
        year_edges = {edge["source"]: edge for edge in graph["edges"] if edge["kind"] == "year"}
        self.assertEqual(year_edges[f"record:patents:{ids['granted']}"]["evidence"], "patents.grant_date")
        self.assertEqual(year_edges[f"record:software:{ids['software']}"]["evidence"], "software.year")
        self.assertNotIn(f"record:patents:{ids['applied']}", year_edges)
        self.assertNotIn(f"record:software:{ids['undated']}", year_edges)
        encoded = json.dumps(payload)
        self.assertNotIn("user_id", encoded)
        self.assertNotIn("archived_at", encoded)
        self.assertNotIn("api_key", encoded)
        self.assertNotIn("Foreign", encoded)
        self.assertEqual(payload, self.payload())

    def test_nine_business_fields_drive_counts_and_graph_without_date_substitution(self):
        cases = {
            "work": ("begin_date", ("end_date",)),
            "appointments": ("begin_date", ("end_date",)),
            "positions": ("begin_date", ()),
            "education": ("award_year", ("begin_date", "end_date")),
            "papers": ("year", ()),
            "patents": ("grant_date", ("apply_date",)),
            "projects": ("begin_date", ("end_date", "close_date")),
            "trainings": ("begin_date", ("end_date",)),
            "software": ("year", ("reg_date",)),
        }
        active = {}
        excluded = []
        for key, (field, other_dates) in cases.items():
            year_field = field in ("year", "award_year")
            current = 2026 if year_field else "2026-01-01"
            historical = 2024 if year_field else "2024-01-01"
            invalid = "2026extra" if year_field else "2026-02-30"
            rows = (
                ("current", {field: current}, "2024-01-01", 2026),
                ("historical", {field: historical}, "2026-01-01", 2024),
                ("missing", {}, "2026-01-01", None),
                ("invalid", {field: invalid}, "2026-01-01", None),
            )
            for kind, values, other_date, expected_year in rows:
                values.update({other_field: other_date for other_field in other_dates})
                entry_id = self.add(key, values)
                # A storage timestamp must not supply a missing business year or override one.
                with self.conn:
                    self.conn.execute(
                        f'UPDATE "{key}" SET created_at = ? WHERE id = ? AND user_id = ?',
                        (other_date + "T00:00:00+00:00", entry_id, self.owner),
                    )
                active[f"record:{key}:{entry_id}"] = (key, field, kind, expected_year)
            archived = self.add(key, {field: current})
            db.archive_row(self.conn, SECTIONS[key], archived, self.owner)
            foreign = self.add(key, {field: current}, self.other)
            excluded.extend((f"record:{key}:{archived}", f"record:{key}:{foreign}"))

        payload = self.payload()
        self.assertEqual(payload["current_total"], 9)
        self.assertEqual(payload["undated_total"], 18)
        sections = {section["key"]: section for section in payload["sections"]}
        for key in cases:
            with self.subTest(section=key):
                self.assertEqual(sections[key]["counts"], [1, 0, 1])
                self.assertEqual(sections[key]["current_count"], 1)
                self.assertEqual(sections[key]["undated_count"], 2)
        for key in sections.keys() - cases.keys():
            self.assertEqual(sections[key]["counts"], [0, 0, 0])
            self.assertEqual(sections[key]["undated_count"], 0)

        nodes = {node["id"]: node for node in payload["graph"]["nodes"]}
        self.assertEqual({node["id"] for node in nodes.values() if node["kind"] == "record"}, set(active))
        self.assertEqual(nodes["self"]["value"], 36)
        self.assertEqual({node["year"]: node["value"] for node in nodes.values() if node["kind"] == "year"},
                         {2024: 9, 2026: 9})
        year_edges = {edge["source"]: edge for edge in payload["graph"]["edges"] if edge["kind"] == "year"}
        self.assertEqual(len(year_edges), 18)
        for record_id, (key, field, kind, expected_year) in active.items():
            with self.subTest(section=key, record=kind):
                self.assertEqual(nodes[record_id]["value"], 1)
                self.assertEqual(nodes[record_id]["year"], expected_year)
                self.assertEqual(nodes[f"section:{key}"]["value"], 4)
                if expected_year is None:
                    self.assertNotIn(record_id, year_edges)
                else:
                    self.assertEqual(year_edges[record_id]["target"], f"year:{expected_year}")
                    self.assertEqual(year_edges[record_id]["evidence"], f"{key}.{field}")
        for record_id in excluded:
            self.assertNotIn(record_id, nodes)

        client = http_client(self, "admin")
        with patch.object(dashboard, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 11, tzinfo=timezone(timedelta(hours=8)))
            response = client.get("/api/dashboard?user_id=" + str(self.other))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), payload)

    def test_year_boundary_and_old_records_remain_in_graph(self):
        self.fixture()
        old = self.add("papers", {"year": 1900, "title": "Historical"})
        future = self.add("papers", {"year": 2100, "title": "Future"})
        payload = self.payload(date(2027, 1, 1))
        self.assertEqual(payload["years"], [2025, 2026, 2027])
        self.assertEqual(payload["current_total"], 0)
        sections = {section["key"]: section for section in payload["sections"]}
        self.assertEqual(sections["papers"]["counts"], [1, 1, 0])
        self.assertEqual(sections["patents"]["counts"], [1, 0, 0])
        self.assertEqual(sections["projects"]["counts"], [0, 0, 0])
        self.assertEqual(payload["undated_total"], 2)
        nodes = {node["id"] for node in payload["graph"]["nodes"]}
        self.assertTrue({f"record:papers:{old}", f"record:papers:{future}", "year:1900", "year:2100"} <= nodes)

    def test_single_record_real_skeleton_and_duplicate_references_are_not_shared(self):
        row_id = self.add("papers", {"year": 2026, "title": "One", "authors": [
            author("Ａlice  Smith", "Acme Lab"), author("alice smith", "acme lab")]})
        payload = self.payload()
        nodes = {node["id"] for node in payload["graph"]["nodes"]}
        self.assertEqual(nodes, {"self", "section:papers", f"record:papers:{row_id}", "year:2026"})
        self.assertEqual(len(payload["graph"]["edges"]), 3)
        second = self.add("papers", {"title": "Two", "authors": [author(" Alice\tSMITH ", "Ａcme　 Lab")]})
        shared = self.payload()["graph"]
        for kind, normalized in (("author", "alice smith"), ("organization", "acme lab")):
            node_id = kind + ":" + hashlib.sha256(normalized.encode()).hexdigest()
            node = next(node for node in shared["nodes"] if node["id"] == node_id)
            self.assertEqual(node["value"], 2)
            self.assertIsNone(node["section"])
            self.assertIsNone(node["entry_id"])
            self.assertIsNone(node["year"])
            edges = [edge for edge in shared["edges"] if edge["target"] == node_id]
            self.assertEqual(len(edges), 2)
            self.assertEqual({edge["source"] for edge in edges}, {f"record:papers:{row_id}", f"record:papers:{second}"})
            first_edge = next(edge for edge in edges if edge["source"] == f"record:papers:{row_id}")
            suffix = "name" if kind == "author" else "affiliation"
            self.assertEqual(first_edge["evidence"], f"papers.authors[0].{suffix}, papers.authors[1].{suffix}")

    def test_all_specified_organization_fields_supply_real_evidence(self):
        fields = {"work": "employer", "positions": "employer", "education": "school",
                  "projects": "funder", "books": "publisher", "trainings": "organizer",
                  "conferences": "organizer", "licenses": "issuer"}
        paper = self.add("papers", {"authors": [author(f"Author {index}", f"Org {key}")
                                                for index, key in enumerate(fields)]})
        ids = {key: self.add(key, {field: f"ORG {key}"}) for key, field in fields.items()}
        graph = self.payload()["graph"]
        shared = [node for node in graph["nodes"] if node["kind"] == "organization"]
        self.assertEqual(len(shared), 8)
        self.assertFalse(any(node["kind"] == "author" for node in graph["nodes"]))
        for index, (key, field) in enumerate(fields.items()):
            node_id = "organization:" + hashlib.sha256(f"org {key}".encode()).hexdigest()
            edges = [edge for edge in graph["edges"] if edge["target"] == node_id]
            self.assertEqual({(edge["source"], edge["evidence"]) for edge in edges}, {
                (f"record:papers:{paper}", f"papers.authors[{index}].affiliation"),
                (f"record:{key}:{ids[key]}", f"{key}.{field}"),
            })

    def test_no_theme_free_text_or_foreign_archived_relation_inference(self):
        name = "Shared Person"
        first = self.add("papers", {"title": "人工智能", "authors": [author(name, "Unique Org")]})
        self.add("papers", {"title": "人工智能"})
        archived = self.add("papers", {"authors": [author(name, "Unique Org")]})
        db.archive_row(self.conn, SECTIONS["papers"], archived, self.owner)
        self.add("papers", {"authors": [author(name, "Unique Org")]}, self.other)
        self.add("patents", {"name": "人工智能", "inventors": name, "holder": "Unique Org"})
        self.add("books", {"title": "人工智能", "authors": name})
        self.add("software", {"holder": "Unique Org"})
        self.add("transfers", {"company": "Unique Org"})
        basic = db.get_basic(self.conn, self.owner)
        db.update_row(self.conn, BASIC, basic["id"], {"name": name, "employer": "Unique Org"}, self.owner)
        graph = self.payload()["graph"]
        self.assertFalse(any(node["kind"] in ("author", "organization") for node in graph["nodes"]))
        self.assertTrue(all(edge["kind"] in ("section", "record", "year") for edge in graph["edges"]))
        self.assertIn(f"record:papers:{first}", {node["id"] for node in graph["nodes"]})

    def test_recent_latest_ten_are_owner_scoped_and_same_second_stable(self):
        row_id = self.add("papers", {"title": "Paper"})
        ids = [self.event("papers", row_id, title=str(index)) for index in range(12)]
        self.event("papers", row_id, owner=self.other, timestamp="2099-01-01T00:00:00+00:00")
        recent = self.payload()["recent"]
        self.assertEqual([event["id"] for event in recent], list(reversed(ids))[:10])
        for event in recent:
            self.assertEqual(set(event), {"id", "section_key", "entry_id", "action", "title", "occurred_at", "href"})
            self.assertEqual(event["occurred_at"], "2026-10-11T01:02:03+00:00")
            self.assertEqual(event["href"], f"/resume?tab=papers&entry={row_id}")
        older = self.event("papers", row_id, timestamp="2020-01-01T00:00:00+00:00")
        self.assertNotIn(older, [event["id"] for event in self.payload()["recent"]])

    def test_recent_links_follow_archive_state_and_reorder_basic_semantics(self):
        active = self.add("papers", {"title": "Active"})
        archived = self.add("papers", {"title": "Archived"})
        db.archive_row(self.conn, SECTIONS["papers"], archived, self.owner)
        basic = db.get_basic(self.conn, self.owner)
        cases = [
            ("papers", active, "create", f"/resume?tab=papers&entry={active}"),
            ("papers", active, "restore", f"/resume?tab=papers&entry={active}"),
            ("papers", active, "archive", "/settings#archive"),
            ("papers", archived, "update", "/settings#archive"),
            ("papers", None, "reorder", "/resume?tab=papers"),
            ("basic", basic["id"], "update", "/resume?tab=basic"),
        ]
        expected = {self.event(key, row, action): href for key, row, action, href in cases}
        links = {event["id"]: event["href"] for event in self.payload()["recent"]}
        for id, href in expected.items():
            self.assertEqual(links[id], href)

    def test_read_snapshot_survives_concurrent_commit_across_all_reads(self):
        basic = db.get_basic(self.conn, self.owner)
        original_get_basic = dashboard.get_basic

        def interleaved_read(conn, user_id):
            result = original_get_basic(conn, user_id)
            with closing(db.connect()) as writer, writer:
                writer.execute("UPDATE basic SET name = 'After' WHERE user_id = ?", (self.owner,))
                cursor = writer.execute("INSERT INTO papers (user_id, title, year, sort_order) VALUES (?, 'After', 2026, 0)",
                                        (self.owner,))
                writer.execute(
                    "INSERT INTO record_events (user_id, section_key, entry_id, action, title, occurred_at) "
                    "VALUES (?, 'papers', ?, 'create', 'After', '2026-10-11T00:00:00+00:00')",
                    (self.owner, cursor.lastrowid),
                )
            return result

        changes = self.conn.total_changes
        with patch.object(dashboard, "get_basic", side_effect=interleaved_read):
            before = self.payload()
        self.assertEqual(before["current_total"], 0)
        self.assertEqual(before["recent"], [])
        self.assertNotEqual(before["graph"]["nodes"][0]["label"], "After")
        self.assertEqual(self.conn.total_changes, changes)
        self.assertFalse(self.conn.in_transaction)
        after = self.payload()
        self.assertEqual(after["current_total"], 1)
        self.assertEqual(after["graph"]["nodes"][0]["label"], "After")
        self.assertEqual(len(after["recent"]), 1)
        self.assertEqual(db.get_basic(self.conn, self.owner)["id"], basic["id"])

    def test_existing_transaction_and_failed_reads_do_not_commit_or_rollback_caller(self):
        self.conn.execute("BEGIN")
        self.conn.execute("UPDATE basic SET name = 'Uncommitted' WHERE user_id = ?", (self.owner,))
        self.assertEqual(self.payload()["graph"]["nodes"][0]["label"], "Uncommitted")
        self.assertTrue(self.conn.in_transaction)
        with patch.object(dashboard, "list_rows", side_effect=sqlite3.OperationalError("private")):
            with self.assertRaises(sqlite3.OperationalError):
                self.payload()
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(db.get_basic(self.conn, self.owner)["name"], "Uncommitted")
        self.conn.rollback()
        self.assertNotEqual(db.get_basic(self.conn, self.owner)["name"], "Uncommitted")
        with patch.object(dashboard, "list_rows", side_effect=sqlite3.OperationalError("private")):
            with self.assertRaises(sqlite3.OperationalError):
                self.payload()
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.payload()["current_total"], 0)

    def test_router_cookie_only_owner_and_one_shanghai_natural_date(self):
        self.fixture()
        client = http_client(self, "admin")
        shanghai = timezone(timedelta(hours=8))
        # UTC December 31 is already January 1 in the requested business timezone.
        boundary = datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc).astimezone(shanghai)
        with patch.object(dashboard, "datetime") as clock:
            clock.now.return_value = boundary
            response = client.get("/api/dashboard?user_id=" + str(self.other))
        self.assertEqual(response.status_code, 200, response.text)
        clock.now.assert_called_once_with(shanghai)
        self.assertEqual(response.json()["year"], 2027)
        self.assertEqual(response.json()["years"], [2025, 2026, 2027])
        self.assertNotIn("Foreign", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        token = "isolated-dashboard-token"
        with self.conn:
            self.conn.execute("INSERT INTO mcp_credentials (user_id, token_hash) VALUES (?, ?)",
                              (self.owner, hashlib.sha256(token.encode()).hexdigest()))
        self.assertEqual(db.get_mcp_user(self.conn, token)["id"], self.owner)
        with patch.object(dashboard, "build_dashboard") as build:
            denied = client.get("/api/dashboard", headers={"Authorization": "Bearer " + token})
        self.assertEqual(denied.status_code, 401)
        build.assert_not_called()
        client.cookies.clear()
        self.assertEqual(client.get("/api/dashboard").status_code, 401)

    def test_router_failure_is_generic_and_retry_reads_real_data(self):
        client = http_client(self, "admin")
        secret = "secret-key /private/attachments/database.db"
        with patch.object(dashboard, "connect", side_effect=sqlite3.OperationalError(secret)):
            response = client.get("/api/dashboard")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(set(response.json()), {"error"})
        self.assertNotIn(secret, response.text)
        self.assertNotIn("current_total", response.json())
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.add("papers", {"title": "Retry", "year": 2026})
        with patch.object(dashboard, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 11, tzinfo=timezone(timedelta(hours=8)))
            retry = client.get("/api/dashboard")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["current_total"], 1)


if __name__ == "__main__":
    unittest.main()
