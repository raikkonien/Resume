import json
import os
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

from app import admin, db
from app.config import ADMIN_PASSWORD, MCP_TOKEN
from app.sections import BASIC, SECTIONS
from support import http_client, isolated_store, original_user_id
from test_admin_ocr_stream import make_pdf_bytes


class UploadPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_store(self)
        self.owner_id = original_user_id()
        self.client = http_client(self)
        with closing(db.connect()) as conn:
            self.other_id = db.create_user(conn, "colleague", "colleague-password")["id"]
        self.other = http_client(self, "colleague", "colleague-password")

    def url(self, section="projects", field="fund_proof"):
        return f"/api/upload-preferences/{section}/{field}"

    def preferences(self, section="projects", user_id=None):
        with closing(db.connect()) as conn:
            return db.get_upload_preferences(conn, SECTIONS[section], user_id or self.owner_id)

    def events(self, response):
        return [json.loads(line) for line in response.text.splitlines() if line]

    def test_defaults_and_opt_out_cover_only_every_actual_file_field(self):
        with closing(db.connect()) as conn:
            for schema in SECTIONS.values():
                expected = {field.key: True for field in schema.fields if field.type == "file"}
                self.assertTrue(expected, schema.key)
                self.assertEqual(db.get_upload_preferences(conn, schema, self.owner_id), expected)
                for field in expected:
                    self.assertEqual(db.save_upload_preference(conn, schema, self.owner_id, field, False),
                                     {"enabled": False})
                self.assertEqual(db.get_upload_preferences(conn, schema, self.owner_id),
                                 dict.fromkeys(expected, False))
                self.assertEqual(db.get_upload_preferences(conn, schema, self.other_id), expected)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM record_events").fetchone()[0], 0)

    def test_account_isolation_and_restart_rename_relogin_and_delete_persistence(self):
        before_settings = {}
        with closing(db.connect()) as conn:
            before_settings = {owner: db.get_ocr_settings(conn, owner) for owner in (self.owner_id, self.other_id)}
        self.assertEqual(self.client.get(self.url()).json(), {"enabled": True})
        self.assertEqual(self.client.put(self.url(), json={"enabled": False}).json(), {"enabled": False})
        self.assertEqual(self.other.get(self.url()).json(), {"enabled": True})
        self.assertTrue(self.preferences()["close_proof"])
        self.client.post("/logout")
        relogged = http_client(self)
        self.assertEqual(relogged.get(self.url()).json(), {"enabled": False})
        with closing(db.connect()) as conn:
            db.update_user(conn, self.owner_id, "renamed_owner")
        db.init_db()
        renamed = http_client(self, "renamed_owner", ADMIN_PASSWORD)
        self.assertEqual(renamed.get(self.url()).json(), {"enabled": False})
        with closing(db.connect()) as conn:
            for owner, settings in before_settings.items():
                self.assertEqual(db.get_ocr_settings(conn, owner), settings)
            db.delete_user(conn, self.owner_id)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM upload_preferences WHERE user_id = ?",
                                          (self.owner_id,)).fetchone()[0], 0)
            self.assertTrue(db.get_upload_preferences(conn, SECTIONS["projects"], self.other_id)["fund_proof"])
        self.assertEqual(renamed.get(self.url()).status_code, 401)

    def test_strict_body_boolean_and_field_validation_never_overwrite(self):
        self.client.put(self.url(), json={"enabled": False})
        cases = [None, [], "false", {}, {"enabled": None}, {"enabled": 0}, {"enabled": 1},
                 {"enabled": "false"}, {"enabled": []}, {"enabled": {}},
                 {"enabled": True, "user_id": self.other_id}, {"enabled": True, "extra": 1}]
        for body in cases:
            with self.subTest(body=body):
                response = self.client.put(self.url(), json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(set(response.json()), {"error"})
                self.assertFalse(self.preferences()["fund_proof"])
        self.assertEqual(self.client.put(self.url(), content="{").status_code, 400)
        for field in ("name", "user_id", "fund_proof_name", "unknown"):
            self.assertEqual(self.client.put(self.url(field=field), json={"enabled": False}).status_code, 400)
            self.assertEqual(self.client.get(self.url(field=field)).status_code, 400)
        self.assertEqual(self.client.put(self.url(section="unknown"), json={"enabled": False}).status_code, 404)
        with closing(db.connect()) as conn:
            for field, enabled in (("fund_proof", 0), ("fund_proof", "false"), (None, True), ([], True), ("name", True)):
                with self.assertRaises(ValueError):
                    db.save_upload_preference(conn, SECTIONS["projects"], self.owner_id, field, enabled)
            with self.assertRaises(ValueError):
                db.save_upload_preference(conn, SECTIONS["projects"], 999999, "fund_proof", False)
            with self.assertRaises(ValueError):
                db.get_upload_preferences(conn, replace(BASIC, key="unknown"), self.owner_id)

    def test_anonymous_bearer_cross_origin_and_stale_sessions_cannot_change_preferences(self):
        anonymous = http_client(self, username=None)
        for headers in ({}, {"Authorization": f"Bearer {MCP_TOKEN}"}):
            self.assertEqual(anonymous.put(self.url(), json={"enabled": False}, headers=headers).status_code, 401)
            self.assertEqual(anonymous.get(self.url(), headers=headers).status_code, 401)
        self.assertEqual(self.client.put(self.url(), json={"enabled": False},
                                        headers={"Origin": "https://evil.example"}).status_code, 403)
        with closing(db.connect()) as conn:
            db.update_user(conn, self.owner_id, "zhangrui", "replacement-password")
        self.assertEqual(self.client.put(self.url(), json={"enabled": False}).status_code, 401)
        self.assertEqual(self.client.get(self.url()).status_code, 401)
        self.assertTrue(self.preferences()["fund_proof"])

    def test_preference_write_revalidates_session_under_lock(self):
        active = admin.active_session_user

        def invalidate(request, conn):
            db.update_user(conn, self.owner_id, "zhangrui", "replacement-password")
            return active(request, conn)

        with patch.object(admin, "active_session_user", side_effect=invalidate):
            response = self.client.put(self.url(), json={"enabled": False})
        self.assertEqual(response.status_code, 401)
        self.assertTrue(self.preferences()["fund_proof"])

    def test_fragments_receive_current_account_preferences_without_business_save(self):
        self.client.put(self.url(), json={"enabled": False})
        self.client.put(self.url("basic", "photo"), json={"enabled": False})
        self.assertFalse(self.client.get("/api/entries/projects/new").context["upload_preferences"]["fund_proof"])
        self.assertTrue(self.other.get("/api/entries/projects/new").context["upload_preferences"]["fund_proof"])
        self.assertEqual(self.client.get("/api/basic/edit").context["upload_preferences"], {"photo": False})
        with closing(db.connect()) as conn:
            self.assertEqual(db.list_rows(conn, SECTIONS["projects"], self.owner_id), [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM record_events").fetchone()[0], 0)

    def test_disabled_uploaded_recognition_is_blocked_for_every_file_field(self):
        with patch("app.admin.recognize") as recognize, patch("app.ocr._call_model") as model:
            for section, schema in SECTIONS.items():
                for field in (field for field in schema.fields if field.type == "file"):
                    with self.subTest(section=section, field=field.key):
                        self.client.put(self.url(section, field.key), json={"enabled": False})
                        route = "/api/basic/ocr" if schema.single else f"/api/entries/{section}/ocr"
                        response = self.client.post(route, data={"field": field.key},
                                                    files={"file": ("invalid.pdf", b"unreadable", "application/pdf")})
                        self.assertEqual(response.status_code, 400, response.text)
                        self.assertIn("关闭", response.json()["error"])
            recognize.assert_not_called()
            model.assert_not_called()

    def test_disabled_preference_does_not_block_upload_and_save(self):
        self.client.put(self.url(), json={"enabled": False})
        pdf = make_pdf_bytes(1)
        with patch("app.ocr._call_model") as model:
            response = self.client.post("/api/entries/projects", data={"name": "Manual entry"},
                                        files={"fund_proof": ("proof.pdf", pdf, "application/pdf")})
            self.assertEqual(response.status_code, 201, response.text)
            model.assert_not_called()
        with closing(db.connect()) as conn:
            row, = db.list_rows(conn, SECTIONS["projects"], self.owner_id)
        self.assertEqual((self.root / "zhangrui" / row["fund_proof"]).read_bytes(), pdf)
        self.assertFalse(self.preferences()["fund_proof"])

    def test_basic_recognition_returns_draft_and_save_keeps_singleton(self):
        with closing(db.connect()) as conn:
            original = db.get_basic(conn, self.owner_id)
        with patch("app.ocr._call_model", return_value='{"fields": {"name": "Recognized Person"}}'):
            response = self.client.post("/api/basic/ocr", data={"field": "photo"},
                                        files={"file": ("profile.pdf", make_pdf_bytes(1), "application/pdf")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.events(response)[-1]["fields"], {"name": "Recognized Person"})
        with closing(db.connect()) as conn:
            self.assertEqual(db.get_basic(conn, self.owner_id), original)
        self.assertEqual(self.client.put("/api/basic", data={"name": "Recognized Person"}).status_code, 200)
        with closing(db.connect()) as conn:
            saved, = db.list_rows(conn, BASIC, self.owner_id)
            self.assertEqual(saved["id"], original["id"])
            self.assertEqual(saved["name"], "Recognized Person")

    def test_zip_upload_recognition_never_starts_worker_or_model(self):
        with patch("app.admin.recognize") as recognize, patch("app.ocr._call_model") as model:
            for route, field in (("/api/basic/ocr", "photo"), ("/api/entries/projects/ocr", "fund_proof"),
                                 ("/api/entries/contracts/ocr", "contract_pdf")):
                response = self.client.post(route, data={"field": field},
                                            files={"file": ("attachment.ZIP", b"not a PDF", "application/zip")})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("ZIP", response.json()["error"])
            recognize.assert_not_called()
            model.assert_not_called()

    def test_ambiguous_missing_field_cannot_bypass_per_field_opt_out(self):
        self.client.put(self.url(), json={"enabled": False})
        with patch("app.admin.recognize") as recognize:
            response = self.client.post("/api/entries/projects/ocr",
                                        files={"file": ("proof.pdf", make_pdf_bytes(1), "application/pdf")})
            self.assertEqual(response.status_code, 400, response.text)
            recognize.assert_not_called()

    def test_upload_ocr_revalidates_session_before_worker_starts(self):
        active = admin.active_session_user

        def invalidate(request, conn):
            db.update_user(conn, self.owner_id, "zhangrui", "replacement-password")
            return active(request, conn)

        with patch.object(admin, "active_session_user", side_effect=invalidate), patch.object(admin, "recognize") as recognize:
            response = self.client.post("/api/entries/projects/ocr", data={"field": "contract"},
                                        files={"file": ("contract.pdf", make_pdf_bytes(1), "application/pdf")})
            self.assertEqual(response.status_code, 401)
            recognize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
