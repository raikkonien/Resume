import io
import os
import re
import unittest
import unicodedata
from contextlib import closing
from urllib.parse import unquote

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

import pymupdf
from PIL import Image

from app import db
from app.config import MCP_TOKEN
from app.sections import BASIC, ENTRY_SECTIONS, SECTIONS
from support import http_client, isolated_store, original_user_id


def normalized(text):
    return re.sub(r"[\s\u200b]", "", unicodedata.normalize("NFKC", text))


class ResumePdfTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_store(self)
        self.owner_id = original_user_id()
        self.client = http_client(self)

    def test_export_uses_current_profile_and_preserves_saved_record_order(self):
        with closing(db.connect()) as conn:
            colleague = db.create_user(conn, "colleague", "colleague-password")
            db.update_row(conn, BASIC, db.get_basic(conn, self.owner_id)["id"],
                          {"name": "张三", "specialty": "本人专属资料"}, self.owner_id)
            db.update_row(conn, BASIC, db.get_basic(conn, colleague["id"])["id"],
                          {"name": "李四", "specialty": "另一用户机密"}, colleague["id"])
            ids = [db.insert_row(conn, SECTIONS["work"], {"employer": value}, self.owner_id)
                   for value in ("单位甲", "单位乙")]
            db.reorder_rows(conn, SECTIONS["work"], list(reversed(ids)), self.owner_id)
        response = self.client.post(f"/resume/export?user_id={colleague['id']}")
        self.assertEqual(response.status_code, 200, response.text[:100] if response.status_code != 200 else "")
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertIn("张三_简历.pdf", unquote(response.headers["content-disposition"]))
        with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
            text = normalized("".join(page.get_text() for page in pdf))
            self.assertIn("本人专属资料", text)
            self.assertNotIn("另一用户机密", text)
            self.assertLess(text.index("单位乙"), text.index("单位甲"))
            self.assertEqual([item[1] for item in pdf.get_toc()], [BASIC.label, *(s.label for s in ENTRY_SECTIONS)])
        other = http_client(self, "colleague", "colleague-password")
        with pymupdf.open(stream=other.post("/resume/export").content, filetype="pdf") as pdf:
            text = normalized("".join(page.get_text() for page in pdf))
            self.assertIn("另一用户机密", text)
            self.assertNotIn("本人专属资料", text)
        self.assertEqual(http_client(self, "admin").post("/resume/export").status_code, 403)
        anonymous = http_client(self, username=None)
        self.assertEqual(anonymous.post("/resume/export").status_code, 401)
        self.assertEqual(anonymous.post("/resume/export", headers={"Authorization": f"Bearer {MCP_TOKEN}"}).status_code, 401)

    def test_photo_chinese_multiline_and_long_identifiers_survive_pagination(self):
        with io.BytesIO() as photo, Image.new("RGB", (30, 40), "green") as image:
            image.save(photo, format="PNG")
            self.assertEqual(self.client.post("/admin/basic", data={"name": "张三 <b>本人</b>"},
                files={"photo": ("照片.png", photo.getvalue(), "image/png")}).status_code, 200)
        long_value = "LongIdentifier" * 40
        paragraphs = [f"研究经历第{index:03d}段，中文与English完整保留。" for index in range(90)]
        with closing(db.connect()) as conn:
            db.insert_row(conn, SECTIONS["trainings"], {"content": "\n".join(paragraphs)}, self.owner_id)
            db.insert_row(conn, SECTIONS["papers"], {"title": "题目<script>不执行</script>", "doi": long_value,
                "article_pdf_name": "论文附件.pdf"}, self.owner_id)
        response = self.client.post("/resume/export")
        self.assertEqual(response.status_code, 200)
        with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
            text = normalized("".join(page.get_text() for page in pdf))
            for paragraph in paragraphs:
                self.assertIn(normalized(paragraph), text)
            for value in (long_value, "张三<b>本人</b>", "题目<script>不执行</script>", "论文附件.pdf"):
                self.assertIn(value, text)
            self.assertEqual(sum(len(page.get_images()) for page in pdf), 1)
            for page in pdf:
                body = [word for word in page.get_text("words") if word[1] < page.rect.height - 40]
                self.assertTrue(body, "PDF contains a blank body page")
                for x0, y0, x1, y1, *_ in body:
                    self.assertGreaterEqual(x0, 40)
                    self.assertGreaterEqual(y0, 35)
                    self.assertLessEqual(x1, page.rect.width - 40)
                    self.assertLessEqual(y1, page.rect.height - 40)

    def test_record_tails_are_not_clipped_when_sections_meet_a_page_boundary(self):
        expected = []
        with closing(db.connect()) as conn:
            for section in ENTRY_SECTIONS:
                for index in range(3):
                    values = {}
                    for field in section.fields:
                        if field.type in ("text", "textarea"):
                            value = f"{section.key}-{index}-{field.key}：" + "内容完整保留" * (index + 1)
                            values[field.key] = value
                            expected.append(value)
                    db.insert_row(conn, section, values, self.owner_id)
        response = self.client.post("/resume/export")
        self.assertEqual(response.status_code, 200)
        with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
            text = normalized("".join(page.get_text() for page in pdf))
            for value in expected:
                self.assertIn(normalized(value), text)

    def test_missing_photo_returns_error_not_a_broken_download_and_can_retry(self):
        with closing(db.connect()) as conn:
            basic = db.get_basic(conn, self.owner_id)
            db.update_row(conn, BASIC, basic["id"], {"photo": "basic/1/missing.png"}, self.owner_id)
        response = self.client.post("/resume/export")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertNotIn("content-disposition", response.headers)
        with closing(db.connect()) as conn:
            db.update_row(conn, BASIC, basic["id"], {"photo": None}, self.owner_id)
        response = self.client.post("/resume/export")
        self.assertEqual(response.status_code, 200)
        with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
            self.assertIn("个人基本信息", "".join(page.get_text() for page in pdf))


if __name__ == "__main__":
    unittest.main()
