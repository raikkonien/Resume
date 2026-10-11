import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

from app import admin, db
from support import http_client, isolated_store


class OcrSettingsProofTests(unittest.TestCase):
    def setUp(self):
        isolated_store(self)
        self.client = http_client(self, "admin")
        self.url = "/admin/ocr/settings"
        self.original = self.client.get(self.url).json()
        self.candidate = {"endpoint": "https://model.example/v1/chat/completions", "model": "vision"}

    def test_only_successfully_tested_exact_configuration_can_be_saved(self):
        self.assertEqual(self.client.put(self.url, json=self.candidate).status_code, 400)
        with patch.object(admin, "check_model", return_value=None):
            tested = self.client.post(self.url + "/test", json=self.candidate)
        self.assertEqual(tested.status_code, 200)
        self.assertEqual(self.client.get(self.url).json(), self.original)
        proof = tested.json()["test_token"]
        for change in ({"model": "other"}, {"endpoint": "https://other.example/api"},
                       {"api_key": "different-key"}, {"test_token": "forged"}):
            with self.subTest(change=change):
                response = self.client.put(self.url, json={**self.candidate, "test_token": proof, **change})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.client.get(self.url).json(), self.original)
        saved = self.client.put(self.url, json={**self.candidate, "test_token": proof})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.client.get(self.url).json()["model"], "vision")

    def test_failed_and_expired_tests_cannot_change_saved_configuration(self):
        with patch.object(admin, "check_model", side_effect=ValueError("vision unsupported")):
            failed = self.client.post(self.url + "/test", json=self.candidate)
        self.assertEqual(failed.status_code, 400)
        self.assertNotIn("test_token", failed.json())
        class ExpiredSigner(admin.ocr_test_signer.signer):
            def get_timestamp(self):
                return 1000

        with patch.object(admin, "check_model", return_value=None), patch.object(admin.ocr_test_signer, "signer", ExpiredSigner):
            proof = self.client.post(self.url + "/test", json=self.candidate).json()["test_token"]
        expired = self.client.put(self.url, json={**self.candidate, "test_token": proof})
        self.assertEqual(expired.status_code, 400)
        self.assertEqual(self.client.get(self.url).json(), self.original)

    def test_secret_keep_and_clear_are_bound_to_the_tested_configuration(self):
        secret = "test-only-secret"
        with patch.object(admin, "check_model", return_value=None):
            keyed = {**self.candidate, "api_key": secret}
            tested = self.client.post(self.url + "/test", json=keyed).json()
            saved = self.client.put(self.url, json={**keyed, **tested})
            self.assertEqual(saved.status_code, 200)
            self.assertTrue(saved.json()["has_api_key"])
            self.assertNotIn(secret, saved.text)
            keep = self.client.post(self.url + "/test", json=self.candidate).json()
            clear = {**self.candidate, "api_key": ""}
            self.assertEqual(self.client.put(self.url, json={**clear, **keep}).status_code, 400)
            self.assertEqual(self.client.put(self.url, json={**self.candidate, **keep}).status_code, 200)
            self.assertTrue(self.client.get(self.url).json()["has_api_key"])
            tested_clear = self.client.post(self.url + "/test", json=clear).json()
            self.assertEqual(self.client.put(self.url, json={**clear, **tested_clear}).status_code, 200)
            self.assertFalse(self.client.get(self.url).json()["has_api_key"])
