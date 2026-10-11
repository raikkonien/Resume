import json
import os
import unittest
from contextlib import closing
from unittest.mock import patch

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

from app import db
from app.sections import SECTIONS
from support import http_client, isolated_store, original_user_id
from test_admin_ocr_stream import make_pdf_bytes


class SavedPaperTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_store(self)
        self.owner_id = original_user_id()
        self.client = http_client(self)
        self.pdf = make_pdf_bytes(2)
        response = self.client.post(
            "/admin/papers", data={"title": "Saved original", "cas_quartile": "二区", "jcr_quartile": "Q3"},
            files={"article_pdf": ("original.pdf", self.pdf, "application/pdf")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        with closing(db.connect()) as conn:
            self.row, = db.list_rows(conn, SECTIONS["papers"], self.owner_id)
        self.url = f"/admin/papers/{self.row['id']}/ocr"

    def test_saved_recognition_is_a_draft_and_preserves_attachment_and_manual_quartiles(self):
        with patch("app.ocr._call_model", return_value=json.dumps({"fields": {
            "title": "Recognized title", "volume": "12", "issue": "S1", "pages": "e456",
            "cas_quartile": "一区", "jcr_quartile": "Q1",
        }})):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual([event["type"] for event in events], ["progress", "result"])
        self.assertEqual(events[-1]["fields"], {
            "title": "Recognized title", "volume": "12", "issue": "S1", "pages": "e456",
        })
        with closing(db.connect()) as conn:
            self.assertEqual(db.get_row(conn, SECTIONS["papers"], self.row["id"], self.owner_id), self.row)
        self.assertEqual((self.root / "zhangrui" / self.row["article_pdf"]).read_bytes(), self.pdf)

    def test_details_and_saved_ocr_do_not_expose_another_profile(self):
        with closing(db.connect()) as conn:
            db.create_user(conn, "colleague", "colleague-password")
        other = http_client(self, "colleague", "colleague-password")
        administrator = http_client(self, "admin")
        anonymous = http_client(self, username=None)
        detail_url = f"/papers/{self.row['id']}"
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        with patch("app.ocr._call_model") as model:
            for client, code in ((other, 404), (administrator, 403), (anonymous, 401)):
                self.assertEqual(client.get(detail_url).status_code, code)
                self.assertEqual(client.post(self.url).status_code, code)
            self.assertEqual(self.client.post(self.url, headers={"Origin": "https://evil.example"}).status_code, 403)
            model.assert_not_called()

    def test_missing_non_pdf_and_unsafe_saved_files_fail_without_calling_model(self):
        path = self.root / "zhangrui" / self.row["article_pdf"]
        path.unlink()
        with patch("app.ocr._call_model") as model:
            self.assertEqual(self.client.post(self.url).status_code, 400)
            for value in (None, "papers/1/article_pdf.png", "../outside.pdf"):
                with closing(db.connect()) as conn:
                    db.update_row(conn, SECTIONS["papers"], self.row["id"], {"article_pdf": value}, self.owner_id)
                self.assertEqual(self.client.post(self.url).status_code, 400)
            model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
