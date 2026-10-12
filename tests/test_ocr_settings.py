import os
import json
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

from app import db, management
import pymupdf
from app.sections import SECTIONS
from support import http_client, isolated_store


class OcrSettingsProofTests(unittest.TestCase):
    def setUp(self):
        isolated_store(self)
        self.client = http_client(self, "admin")
        with closing(db.connect()) as conn:
            self.actor = db.get_user_by_username(conn, "admin")
            self.other_user = db.create_user(conn, "colleague", "colleague-password")
        self.other = http_client(self, "colleague", "colleague-password")
        self.url = "/settings/model"
        self.original = self.client.get(self.url).json()
        self.candidate = {"endpoint": "https://model.example/v1/chat/completions", "model": "vision"}

    def stored(self, user_id=None):
        with closing(db.connect()) as conn:
            return db.get_ocr_settings(conn, user_id or self.actor["id"])

    def proof(self, client=None, candidate=None):
        response = (client or self.client).post(self.url + "/test", json=candidate or self.candidate)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["test_token"]

    def test_only_successfully_tested_exact_configuration_can_be_saved(self):
        self.assertEqual(self.client.put(self.url, json=self.candidate).status_code, 400)
        with patch.object(management, "check_model", return_value=None) as check:
            proof = self.proof()
        check.assert_called_once_with({**self.candidate, "api_key": ""})
        self.assertEqual(self.client.get(self.url).json(), self.original)
        for change in ({"model": "other"}, {"endpoint": "https://other.example/api"},
                       {"api_key": "different-key"}, {"test_token": "forged"}):
            with self.subTest(change=change):
                response = self.client.put(self.url, json={**self.candidate, "test_token": proof, **change})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.client.get(self.url).json(), self.original)
        saved = self.client.put(self.url, json={**self.candidate, "test_token": proof})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.client.get(self.url).json()["model"], "vision")

    def test_failed_expired_and_old_format_tickets_do_not_save(self):
        secret = "upstream-secret-must-not-appear"
        with patch.object(management, "check_model", side_effect=ValueError(secret)):
            failed = self.client.post(self.url + "/test", json=self.candidate)
        self.assertEqual(failed.status_code, 400)
        self.assertNotIn("test_token", failed.json())
        self.assertNotIn(secret, failed.text)
        class ExpiredSigner(management.ocr_test_signer.signer):
            def get_timestamp(self):
                return 1000
        with patch.object(management, "check_model", return_value=None), patch.object(management.ocr_test_signer, "signer", ExpiredSigner):
            proof = self.proof()
        old = management.ocr_test_signer.dumps(management.settings_fingerprint({**self.candidate, "api_key": ""}))
        from itsdangerous import URLSafeTimedSerializer
        from app.config import SECRET_KEY
        legacy = URLSafeTimedSerializer(SECRET_KEY, salt="ocr-settings-test").dumps("old-string")
        for token in (proof, old, legacy):
            self.assertEqual(self.client.put(self.url, json={**self.candidate, "test_token": token}).status_code, 400)
            self.assertEqual(self.client.get(self.url).json(), self.original)

    def test_keys_are_private_omission_and_null_reuse_only_own_key(self):
        secret_a, secret_b = "test-only-secret-a", "test-only-secret-b"
        with patch.object(management, "check_model", return_value=None) as check:
            for client, secret in ((self.client, secret_a), (self.other, secret_b)):
                candidate = {**self.candidate, "api_key": secret}
                proof = self.proof(client, candidate)
                saved = client.put(self.url, json={**candidate, "test_token": proof})
                self.assertEqual(saved.status_code, 200)
                self.assertTrue(saved.json()["has_api_key"])
                self.assertNotIn(secret, saved.text)
                self.assertNotIn(secret, client.get(self.url).text)
            for candidate in (self.candidate, {**self.candidate, "api_key": None}):
                proof = self.proof(self.client, candidate)
                self.assertEqual(check.call_args.args[0]["api_key"], secret_a)
                self.assertEqual(self.client.put(self.url, json={**candidate, "test_token": proof}).status_code, 200)
                self.assertEqual(self.stored()["api_key"], secret_a)
                self.assertEqual(self.stored(self.other_user["id"])["api_key"], secret_b)
            keep = self.proof()
            clear = {**self.candidate, "api_key": ""}
            self.assertEqual(self.client.put(self.url, json={**clear, "test_token": keep}).status_code, 400)
            tested_clear = self.proof(candidate=clear)
            self.assertEqual(self.client.put(self.url, json={**clear, "test_token": tested_clear}).status_code, 200)
            self.assertFalse(self.client.get(self.url).json()["has_api_key"])
            self.assertEqual(self.stored(self.other_user["id"])["api_key"], secret_b)

    def test_tickets_are_bound_to_user_and_session_not_username(self):
        with patch.object(management, "check_model", return_value=None):
            proof = self.proof()
        payload = management.ocr_test_signer.loads(proof)
        self.assertEqual(set(payload), {"user_id", "session_version", "fingerprint"})
        self.assertEqual(payload["user_id"], self.actor["id"])
        self.assertEqual(self.other.put(self.url, json={**self.candidate, "test_token": proof}).status_code, 400)
        self.assertEqual(self.other.get(self.url).json(), self.original)
        with closing(db.connect()) as conn:
            db.update_user(conn, self.actor["id"], "renamed", "replacement-password")
        self.assertEqual(self.client.put(self.url, json={**self.candidate, "test_token": proof}).status_code, 401)
        renamed = http_client(self, "renamed", "replacement-password")
        self.assertEqual(renamed.put(self.url, json={**self.candidate, "test_token": proof}).status_code, 400)
        self.assertEqual(renamed.get(self.url).json(), self.original)
        with patch.object(management, "check_model", return_value=None):
            fresh = self.proof(renamed)
        self.assertEqual(renamed.put(self.url, json={**self.candidate, "test_token": fresh}).status_code, 200)

    def test_session_is_revalidated_after_network_and_before_write(self):
        def revoke(_candidate):
            with closing(db.connect()) as conn:
                db.update_user(conn, self.actor["id"], "admin", "replacement-password")
        with patch.object(management, "check_model", side_effect=revoke):
            response = self.client.post(self.url + "/test", json=self.candidate)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("test_token", response.json())
        self.assertEqual(self.stored(), {"endpoint": "", "model": "", "api_key": ""})
        fresh = http_client(self, "admin", "replacement-password")
        with patch.object(management, "check_model", return_value=None):
            proof = self.proof(fresh)
        original_active = management.active_session_user
        def revoke_before_lock(request, conn):
            db.update_user(conn, self.actor["id"], "admin", "third-password")
            return original_active(request, conn)
        with patch.object(management, "active_session_user", side_effect=revoke_before_lock):
            response = fresh.put(self.url, json={**self.candidate, "test_token": proof})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.stored(), {"endpoint": "", "model": "", "api_key": ""})

    def test_invalid_json_fields_types_and_endpoints_do_not_test_or_save(self):
        with patch.object(management, "check_model") as check:
            for body in ([], "text", {**self.candidate, "user_id": self.other_user["id"]},
                         {**self.candidate, "api_key": 42}, {**self.candidate, "api_key": "unsafe\nkey"},
                         {**self.candidate, "endpoint": "https://name:password@model.example/api"},
                         {**self.candidate, "endpoint": "ftp://model.example/api"},
                         {**self.candidate, "endpoint": "https://model.example:invalid/api"},
                         {**self.candidate, "endpoint": "https://model.example/api#fragment"},
                         {**self.candidate, "model": None}):
                for method, path in (("post", self.url + "/test"), ("put", self.url)):
                    result = getattr(self.client, method)(path, json=body)
                    self.assertEqual(result.status_code, 400, result.text)
                    self.assertEqual(set(result.json()), {"error"})
            self.assertEqual(self.client.post(self.url + "/test", content="{").status_code, 400)
            check.assert_not_called()
        self.assertEqual(self.client.get(self.url).json(), self.original)

    def test_uploaded_and_saved_documents_use_only_the_owners_model_over_real_http(self):
        expected = {
            "alpha": ("alpha-key", "Alpha extracted title"),
            "beta": ("beta-key", "Beta extracted title"),
        }
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key, title = expected.get(body.get("model"), ("", ""))
                text = json.dumps(body.get("messages"))
                valid = (self.headers.get("Authorization") == f"Bearer {key}"
                         and "Owner-scoped paper fixture" in text)
                result = {"choices": [{"message": {"content": json.dumps({"fields": {"title": title}})},
                                       "finish_reason": "stop"}]} if valid else {"error": "Invalid fixture credentials"}
                payload = json.dumps(result).encode()
                self.send_response(200 if valid else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(upstream.shutdown)
        endpoint = f"http://127.0.0.1:{upstream.server_port}/chat"
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), "Owner-scoped paper fixture")
            attachment = document.tobytes()
        for client, actor, model in ((self.client, self.actor, "alpha"),
                                     (self.other, self.other_user, "beta")):
            key, extracted_title = expected[model]
            with closing(db.connect()) as conn:
                db.save_ocr_settings(conn, actor["id"], endpoint, model, key)
            original_title = f"Original {model} title"
            created = client.post("/api/entries/papers", data={"title": original_title, "year": "2026"},
                                  files={"article_pdf": ("fixture.pdf", attachment, "application/pdf")})
            self.assertEqual(created.status_code, 201, created.text)
            with closing(db.connect()) as conn:
                row = db.list_rows(conn, SECTIONS["papers"], actor["id"])[-1]
            for path, options in (
                ("/api/entries/papers/ocr", {"data": {"field": "article_pdf"},
                  "files": {"file": ("fixture.pdf", attachment, "application/pdf")}}),
                (f"/api/entries/papers/{row['id']}/ocr", {}),
            ):
                response = client.post(path, **options)
                self.assertEqual(response.status_code, 200, response.text)
                events = [json.loads(line) for line in response.text.splitlines() if line]
                self.assertEqual(events[-1]["type"], "result", events)
                self.assertEqual(events[-1]["fields"]["title"], extracted_title)
                with closing(db.connect()) as conn:
                    current = db.get_row(conn, SECTIONS["papers"], row["id"], actor["id"])
                self.assertEqual(current["title"], original_title)
                self.assertEqual(client.get(f"/files/papers/{row['id']}/article_pdf").content, attachment)
