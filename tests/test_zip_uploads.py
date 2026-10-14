import io
from contextlib import closing
from pathlib import Path
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf

from app import db, files
from app.sections import BASIC, SECTIONS
from support import http_client, isolated_store, original_user_id


def zip_bytes():
    with io.BytesIO() as output:
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("证明.txt", "只保存附件，不解压")
            archive.writestr("../../must-not-extract.txt", "opaque member")
        return output.getvalue()


class ZipUploadTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_store(self)
        self.owner = original_user_id()
        self.client = http_client(self)
        self.payload = zip_bytes()

    def test_every_attachment_field_roundtrips_zip_without_extracting_members(self):
        for section in SECTIONS.values():
            uploads = {field.key: (f"{field.key}.ZIP", self.payload, "application/zip")
                       for field in section.fields if field.type == "file"}
            with self.subTest(section=section.key):
                if section.single:
                    response = self.client.put("/api/basic", files=uploads)
                    with closing(db.connect()) as conn:
                        row = db.get_basic(conn, self.owner)
                else:
                    response = self.client.post(f"/api/entries/{section.key}", files=uploads)
                    with closing(db.connect()) as conn:
                        row, = db.list_rows(conn, section, self.owner)
                self.assertEqual(response.status_code, 200 if section.single else 201, response.text)
                for key in uploads:
                    download = self.client.get(f"/files/{section.key}/{row['id']}/{key}?download=1")
                    self.assertEqual(download.status_code, 200)
                    self.assertEqual(download.content, self.payload)
                    self.assertEqual(download.headers["content-type"], "application/zip")
                    self.assertEqual(Path(row[key]).suffix, ".zip")
        stored = [path for path in (self.root / "zhangrui").rglob("*") if path.is_file()]
        self.assertTrue(all(path.suffix == ".zip" for path in stored))
        self.assertFalse((self.root / "must-not-extract.txt").exists())
        other = http_client(self, username="admin")
        self.assertEqual(other.get(f"/files/{section.key}/{row['id']}/{key}").status_code, 404)

    def test_corrupt_zip_replacement_keeps_saved_record_and_archive(self):
        section = SECTIONS["transfers"]
        created = self.client.post("/api/entries/transfers", data={"name": "Original"},
                                   files={"payment_proof": ("到账证明.zip", self.payload, "application/zip")})
        self.assertEqual(created.status_code, 201, created.text)
        with closing(db.connect()) as conn:
            original, = db.list_rows(conn, section, self.owner)
        for content in (b"not a zip", self.payload[:30], b"PK\x03\x04invalid archive"):
            with self.subTest(content=content):
                response = self.client.put(f"/api/entries/transfers/{original['id']}",
                    data={"name": "Changed"}, files={"payment_proof": ("bad.zip", content, "application/zip")})
                self.assertEqual(response.status_code, 400)
                with closing(db.connect()) as conn:
                    self.assertEqual(db.get_row(conn, section, original["id"], self.owner), original)
                self.assertEqual(self.client.get(f"/files/transfers/{original['id']}/payment_proof").content,
                                 self.payload)

    def test_zip_size_limit_applies_before_save(self):
        with patch.object(files, "MAX_UPLOAD_MB", 0):
            response = self.client.post("/api/entries/work",
                files={"attachment": ("proof.zip", self.payload, "application/zip")})
        self.assertEqual(response.status_code, 400)
        with closing(db.connect()) as conn:
            self.assertEqual(db.list_rows(conn, SECTIONS["work"], self.owner), [])

    def test_photo_zip_is_downloadable_but_not_decoded_as_portrait_on_pdf_export(self):
        response = self.client.put("/api/basic", data={"name": "ZIP portrait attachment"},
            files={"photo": ("portrait.zip", self.payload, "application/zip")})
        self.assertEqual(response.status_code, 200, response.text)
        exported = self.client.post("/resume/export")
        self.assertEqual(exported.status_code, 200, exported.text[:200])
        with pymupdf.open(stream=exported.content, filetype="pdf") as document:
            self.assertIn("ZIP portrait attachment", "".join(page.get_text() for page in document))
            self.assertIn("portrait.zip", "".join(page.get_text() for page in document))
            self.assertEqual(sum(len(page.get_images()) for page in document), 0)
        with closing(db.connect()) as conn:
            basic = db.get_basic(conn, self.owner)
        self.assertEqual(self.client.get(f"/files/basic/{basic['id']}/photo").content, self.payload)
