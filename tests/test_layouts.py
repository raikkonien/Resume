import copy
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import db
from app.sections import BASIC, Field, SECTIONS


class TableLayoutTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(db, "DATA_DIR", self.root))
        self.enterContext(patch.object(db, "DB_PATH", self.root / "resume.db"))
        self.enterContext(patch.object(db, "ADMIN_PASSWORD", ""))
        self.enterContext(patch.object(db, "INITIAL_ADMIN_PASSWORD", "test-bootstrap-admin"))
        self.enterContext(patch.object(db, "MCP_TOKEN", ""))
        db.init_db()
        self.conn = self.enterContext(closing(db.connect()))
        self.owner = db.get_user_by_username(self.conn, "admin")["id"]
        self.other = db.create_user(self.conn, "colleague", "colleague-password")["id"]
        self.section = SECTIONS["projects"]

    def layout(self, section=None):
        return db.get_table_layout(self.conn, section or self.section, self.owner)

    def test_defaults_include_every_field_and_basic_has_layout_without_row_order(self):
        for section in (BASIC, *SECTIONS.values()):
            layout = self.layout(section)
            self.assertEqual(layout, {"version": 1, "column_order": [field.key for field in section.fields],
                                      "hidden_columns": [], "sort": None, "filters": {}})
            self.assertEqual(db.save_table_layout(self.conn, section, self.owner, layout), layout)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM record_events").fetchone()[0], 0)

    def test_layout_roundtrip_is_owner_scoped_and_survives_rename_and_restart(self):
        layout = self.layout()
        layout["column_order"].reverse()
        layout["hidden_columns"] = ["amount", "close_proof"]
        layout["sort"] = {"field": "name", "direction": "desc"}
        layout["filters"] = {"name": {"op": "contains", "value": "  Artificial Intelligence  "},
                             "amount": {"op": "range", "min": 0.0, "max": 100.5}}
        saved = db.save_table_layout(self.conn, self.section, self.owner, layout)
        self.assertEqual(saved["filters"]["name"]["value"], "Artificial Intelligence")
        self.assertEqual(self.layout(), saved)
        self.assertEqual(db.get_table_layout(self.conn, self.section, self.other), db._default_layout(self.section))
        db.update_user(self.conn, self.owner, "renamed_admin")
        db.init_db()
        self.assertEqual(self.layout(), saved)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM record_events").fetchone()[0], 0)
        db.delete_user(self.conn, self.owner)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM table_layouts WHERE user_id=?", (self.owner,)).fetchone())

    def test_invalid_layout_structure_is_rejected_without_overwriting_saved_state(self):
        saved = db.save_table_layout(self.conn, self.section, self.owner, self.layout())
        cases = []
        for key, value in (("version", True), ("version", 2), ("column_order", []),
                           ("column_order", ["name", "name"]), ("column_order", ["unknown"]),
                           ("column_order", [True]), ("hidden_columns", ["unknown"]),
                           ("hidden_columns", ["name", "name"]),
                           ("hidden_columns", list(saved["column_order"])),
                           ("sort", {"field": "unknown", "direction": "asc"}),
                           ("sort", {"field": "name", "direction": "sideways"}),
                           ("sort", {"field": "name", "direction": "asc", "extra": 1}),
                           ("filters", {"user_id": {"op": "eq", "value": self.other}})):
            layout = copy.deepcopy(saved)
            layout[key] = value
            cases.append(layout)
        missing = copy.deepcopy(saved)
        del missing["filters"]
        cases += [missing, {**saved, "row_order": [1]}, None, [], "not-json"]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    db.save_table_layout(self.conn, self.section, self.owner, candidate)
                self.assertEqual(self.layout(), saved)
        with self.assertRaises(ValueError):
            db.save_table_layout(self.conn, self.section, 999, saved)
        with self.assertRaises(ValueError):
            db.save_table_layout(self.conn, replace(self.section, key="unknown"), self.owner, saved)

    def test_valid_filter_types_are_normalized_and_empty_filters_are_not_stored(self):
        cases = [
            ("projects", "name", {"op": "contains", "value": "  literal <img>  "}),
            ("projects", "rank", {"op": "contains", "value": "2/3"}),
            ("papers", "authors", {"op": "contains", "value": "University"}),
            ("projects", "status", {"op": "eq", "value": "在研"}),
            ("education", "full_time", {"op": "eq", "value": False}),
            ("projects", "contract", {"op": "presence", "value": "present"}),
            ("projects", "fund_proof", {"op": "presence", "value": "missing"}),
            ("projects", "begin_date", {"op": "range", "min": "2024-02-29", "max": "2026-10-11"}),
            ("memberships", "begin_month", {"op": "range", "min": "2024-12", "max": "2026-01"}),
            ("papers", "year", {"op": "range", "min": 1900, "max": 2100}),
            ("books", "word_count", {"op": "range", "min": 0, "max": 10}),
            ("projects", "amount", {"op": "range", "min": -0.5, "max": 1.5}),
        ]
        for section_key, field, value in cases:
            section = SECTIONS[section_key]
            layout = self.layout(section)
            layout["filters"] = {field: value}
            expected = copy.deepcopy(value)
            if expected["op"] == "contains":
                expected["value"] = expected["value"].strip()
            with self.subTest(section=section_key, field=field):
                saved = db.save_table_layout(self.conn, section, self.owner, layout)
                self.assertEqual(saved["filters"], {field: expected})
                self.assertEqual(self.layout(section), saved)
        layout = self.layout()
        layout["filters"] = {"name": {"op": "contains", "value": "  "},
                             "amount": {"op": "range", "min": None, "max": None}}
        self.assertEqual(db.save_table_layout(self.conn, self.section, self.owner, layout)["filters"], {})

    def test_invalid_filter_types_and_bounds_leave_database_unchanged(self):
        cases = [
            ("projects", "name", {"op": "regex", "value": ".*"}),
            ("projects", "name", {"op": "contains", "value": 1}),
            ("projects", "name", {"op": "contains", "value": "x", "extra": True}),
            ("projects", "status", {"op": "eq", "value": "invented"}),
            ("projects", "status", {"op": "eq", "value": True}),
            ("education", "full_time", {"op": "eq", "value": 1}),
            ("projects", "contract", {"op": "presence", "value": "/etc/passwd"}),
            ("projects", "contract", {"op": "contains", "value": "pdf"}),
            ("projects", "begin_date", {"op": "range", "min": "2025-02-29", "max": None}),
            ("projects", "begin_date", {"op": "range", "min": "2025-2-01", "max": None}),
            ("projects", "begin_date", {"op": "range", "min": "2026-01-01", "max": "2025-01-01"}),
            ("memberships", "begin_month", {"op": "range", "min": "2026-13", "max": None}),
            ("papers", "year", {"op": "range", "min": True, "max": 2026}),
            ("papers", "year", {"op": "range", "min": 2025.0, "max": 2026}),
            ("papers", "year", {"op": "range", "min": "2025", "max": 2026}),
            ("books", "word_count", {"op": "range", "min": 1.1, "max": 2}),
            ("projects", "amount", {"op": "range", "min": 2, "max": 1}),
            ("projects", "amount", {"op": "range", "min": float("inf"), "max": None}),
            ("projects", "amount", {"op": "range", "min": float("nan"), "max": None}),
            ("projects", "amount", {"op": "range", "min": 1}),
        ]
        for section_key, field, value in cases:
            section = SECTIONS[section_key]
            before = self.layout(section)
            layout = copy.deepcopy(before)
            layout["filters"] = {field: value}
            with self.subTest(section=section_key, field=field, value=value):
                with self.assertRaises(ValueError):
                    db.save_table_layout(self.conn, section, self.owner, layout)
                self.assertEqual(self.layout(section), before)

    def test_old_layout_drops_removed_fields_and_adds_new_fields_visible_at_end(self):
        current = SECTIONS["software"]
        old_order = [field.key for field in current.fields if field.key != "reg_date"]
        old_order.reverse()
        old = {"version": 1, "column_order": ["removed", *old_order],
               "hidden_columns": ["removed", "reg_date", "holder"],
               "sort": {"field": "removed", "direction": "asc"},
               "filters": {"removed": {"op": "contains", "value": "old"},
                           "name": {"op": "contains", "value": "  retained  "}}}
        with self.conn:
            self.conn.execute("INSERT INTO table_layouts VALUES (?, ?, ?)",
                              (self.owner, current.key, json.dumps(old)))
        loaded = self.layout(current)
        self.assertEqual(loaded["column_order"], [*old_order, "reg_date"])
        self.assertEqual(loaded["hidden_columns"], ["holder"])
        self.assertIsNone(loaded["sort"])
        self.assertEqual(loaded["filters"], {"name": {"op": "contains", "value": "retained"}})
        self.assertEqual(db.save_table_layout(self.conn, current, self.owner, loaded), loaded)

    def test_new_field_loaded_from_extended_metadata_is_visible(self):
        layout = self.layout()
        layout["hidden_columns"] = ["name"]
        db.save_table_layout(self.conn, self.section, self.owner, layout)
        extended = replace(self.section, fields=(*self.section.fields, Field("new_business_field", "New", "text")))
        loaded = db.get_table_layout(self.conn, extended, self.owner)
        self.assertEqual(loaded["column_order"], [*layout["column_order"], "new_business_field"])
        self.assertNotIn("new_business_field", loaded["hidden_columns"])

    def test_malformed_stored_json_returns_default_without_creating_activity(self):
        with self.conn:
            self.conn.execute("INSERT INTO table_layouts VALUES (?, ?, ?)", (self.owner, self.section.key, "{bad-json"))
        self.assertEqual(self.layout(), db._default_layout(self.section))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM record_events").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
