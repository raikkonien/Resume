import io
import os
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

from PIL import Image
from starlette.datastructures import UploadFile
from app import db, files
from app.sections import BASIC, SECTIONS, Field
from support import http_client, isolated_store, original_user_id


def image_bytes(format):
    with io.BytesIO() as stream, Image.new("RGB", (8, 8), "white") as image:
        image.save(stream, format=format)
        return stream.getvalue()


class AttachmentUploadTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_store(self)
        self.upload_dir = self.root / "zhangrui"
        self.user_id = original_user_id()
        self.client = http_client(self)
        self.section = SECTIONS["projects"]

    def rows(self):
        with closing(db.connect()) as conn:
            return db.list_rows(conn, self.section, self.user_id)

    def test_jpg_named_png_proofs_save_and_download_with_correct_type(self):
        png = image_bytes("PNG")
        response = self.client.post(
            "/api/entries/projects", data={"name": "Proof upload"},
            files={key: (f"{key}.JPG", png, "image/jpeg")
                   for key in ("fund_proof", "close_proof")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        row, = self.rows()
        for key in ("fund_proof", "close_proof"):
            with self.subTest(field=key):
                download = self.client.get(f"/files/projects/{row['id']}/{key}?download=1")
                self.assertEqual(download.status_code, 200)
                self.assertEqual(download.content, png)
                self.assertEqual(download.headers["content-type"], "image/png")
                self.assertIn(f'filename="{key}.png"', download.headers["content-disposition"])
                self.assertEqual((self.upload_dir / row[key]).read_bytes(), png)

    def test_replacement_corrects_reverse_mismatch_and_removes_old_file(self):
        png, jpeg = image_bytes("PNG"), image_bytes("JPEG")
        response = self.client.post(
            "/api/entries/projects", files={"close_proof": ("old.png", png, "image/png")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        row, = self.rows()
        old_path = self.upload_dir / row["close_proof"]
        response = self.client.put(
            f"/api/entries/projects/{row['id']}",
            files={"close_proof": ("new.png", jpeg, "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        download = self.client.get(f"/files/projects/{row['id']}/close_proof")
        self.assertEqual(download.content, jpeg)
        self.assertEqual(download.headers["content-type"], "image/jpeg")
        self.assertIn('filename="new.jpg"', download.headers["content-disposition"])
        self.assertFalse(old_path.exists())

    def test_invalid_second_proof_leaves_existing_record_and_files_unchanged(self):
        jpeg = image_bytes("JPEG")
        response = self.client.post(
            "/api/entries/projects", data={"name": "Original"},
            files={key: (f"{key}.jpeg", jpeg, "image/jpeg")
                   for key in ("fund_proof", "close_proof")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        original, = self.rows()
        response = self.client.put(
            f"/api/entries/projects/{original['id']}", data={"name": "Changed"},
            files={"fund_proof": ("valid.jpg", image_bytes("PNG"), "image/jpeg"),
                   "close_proof": ("invalid.jpg", b"<html>not an image</html>", "image/jpeg")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows(), [original])
        for key in ("fund_proof", "close_proof"):
            download = self.client.get(f"/files/projects/{original['id']}/{key}")
            self.assertEqual(download.content, jpeg)
            self.assertIn(f'filename="{key}.jpeg"', download.headers["content-disposition"])

    def test_actual_format_must_be_allowed_for_field(self):
        photo = next(field for field in BASIC.fields if field.key == "photo")
        with io.BytesIO(b"%PDF-1.7\n") as stream:
            upload = UploadFile(filename="photo.jpg", file=stream)
            with self.assertRaises(ValueError):
                files.validate_upload(photo, upload)
        png_only = Field("proof", "证明", "file", accept=".png")
        with io.BytesIO(image_bytes("JPEG")) as stream:
            upload = UploadFile(filename="proof.png", file=stream)
            with self.assertRaises(ValueError):
                files.validate_upload(png_only, upload)

    def test_normalization_does_not_bypass_size_limit(self):
        field = next(field for field in self.section.fields if field.key == "fund_proof")
        with io.BytesIO(image_bytes("PNG")) as stream, patch.object(files, "MAX_UPLOAD_MB", 0):
            upload = UploadFile(filename="proof.jpg", file=stream)
            with self.assertRaises(ValueError):
                files.validate_upload(field, upload)

    def test_fake_and_truncated_images_cannot_create_a_record(self):
        for content in (b"\xff\xd8not a jpeg", image_bytes("JPEG")[:-20],
                        image_bytes("PNG")[:40], image_bytes("PNG")[:-8]):
            with self.subTest(content=content[:16]):
                response = self.client.post(
                    "/api/entries/projects",
                    files={"close_proof": ("proof.jpg", content, "image/jpeg")},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.rows(), [])
                self.assertEqual(list(self.upload_dir.rglob("*")), [])

    def test_archive_preserves_attachment_and_restore_appends_original_id(self):
        payload = image_bytes("PNG")
        response = self.client.post("/api/entries/projects", data={"name": "Archived project"},
                                    files={"close_proof": ("proof.png", payload, "image/png")})
        self.assertEqual(response.status_code, 201)
        row, = self.rows()
        later = self.client.post("/api/entries/projects", data={"name": "Later project"})
        self.assertEqual(later.status_code, 201)
        url = f"/files/projects/{row['id']}/close_proof?download=1"
        archived = self.client.post(f"/api/entries/projects/{row['id']}/archive")
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual([item["name"] for item in self.rows()], ["Later project"])
        self.assertEqual((self.upload_dir / row["close_proof"]).read_bytes(), payload)
        self.assertEqual(self.client.post(f"/api/entries/projects/{row['id']}/archive").status_code, 404)
        self.assertEqual(self.client.post("/api/entries/projects/reorder", json={"ids": [row["id"]]}).status_code, 400)
        self.assertEqual(self.client.post("/api/archive/restore",
                                         json={"records": [{"section": "projects", "id": row["id"]}]}).status_code, 200)
        restored = self.rows()
        self.assertEqual(restored[-1]["id"], row["id"])
        self.assertEqual(restored[-1]["close_proof"], row["close_proof"])
        self.assertEqual(self.client.get(url).content, payload)
        self.assertEqual(self.client.get("/api/archive").json(), [])
        self.assertEqual(self.client.post(f"/api/entries/basic/{row['id']}/archive").status_code, 404)

    def test_only_successful_actual_saves_generate_activity(self):
        def events():
            with closing(db.connect()) as conn:
                return [item[0] for item in conn.execute(
                    "SELECT action FROM record_events WHERE user_id=? ORDER BY id", (self.user_id,))]
        with patch("app.admin.save_upload", side_effect=OSError("synthetic failure")):
            response = self.client.post("/api/entries/projects", data={"name": "Failed"},
                                        files={"close_proof": ("proof.png", image_bytes("PNG"), "image/png")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(events(), [])
        self.assertEqual(self.rows(), [])
        png = image_bytes("PNG")
        self.assertEqual(self.client.post("/api/entries/projects", data={"name": "Same"},
            files={"close_proof": ("proof.png", png, "image/png")}).status_code, 201)
        row, = self.rows()
        self.assertEqual(events(), ["create"])
        url = f"/api/entries/projects/{row['id']}"
        self.assertEqual(self.client.put(url, data={"name": "Same"}).status_code, 200)
        self.assertEqual(events(), ["create"])
        self.assertEqual(self.client.put(url, data={"name": "Same"},
            files={"close_proof": ("proof.png", png, "image/png")}).status_code, 200)
        self.assertEqual(events(), ["create", "update"])


if __name__ == "__main__":
    unittest.main()
