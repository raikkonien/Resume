import os
import unittest
from contextlib import closing

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

from app import db
from app.config import ADMIN_PASSWORD, MCP_TOKEN
from app.sections import BASIC, SECTIONS
from support import http_client, isolated_store, original_user_id


class ProfileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_store(self)
        self.owner_id = original_user_id()
        with closing(db.connect()) as conn:
            self.colleague = db.create_user(conn, "colleague", "colleague-password")
        self.owner = http_client(self)
        self.other = http_client(self, "colleague", "colleague-password")
        self.admin = http_client(self, "admin")

    def test_resume_rows_files_and_mutations_are_owner_scoped(self):
        pdf = b"%PDF-1.4\noriginal attachment"
        created = self.owner.post("/admin/projects", data={"name": "Private original project"},
                                  files={"contract": ("contract.pdf", pdf, "application/pdf")})
        self.assertEqual(created.status_code, 201, created.text)
        with closing(db.connect()) as conn:
            row, = db.list_rows(conn, SECTIONS["projects"], self.owner_id)
        url = f"/files/projects/{row['id']}/contract"
        self.assertEqual(self.owner.get(url).content, pdf)
        self.assertEqual(self.other.get(url).status_code, 404)
        self.assertNotIn("Private original project", self.other.get("/").text)
        self.assertIn("Private original project", self.owner.get("/").text)
        self.assertEqual(self.other.get(f"/admin/projects/{row['id']}/edit").status_code, 404)
        self.assertEqual(self.other.put(f"/admin/projects/{row['id']}",
                                       data={"name": "stolen"}).status_code, 404)
        self.assertEqual(self.other.delete(f"/admin/projects/{row['id']}").status_code, 404)
        self.assertEqual(self.other.post("/admin/projects/reorder",
                                        json={"ids": [row["id"]]}).status_code, 400)
        with closing(db.connect()) as conn:
            self.assertEqual(db.get_row(conn, SECTIONS["projects"], row["id"], self.owner_id), row)
        self.assertEqual((self.root / "zhangrui" / row["contract"]).read_bytes(), pdf)

    def test_new_profile_basic_information_and_photo_have_distinct_identity(self):
        from test_uploads import image_bytes
        photo = image_bytes("PNG")
        result = self.other.post("/admin/basic", data={"name": "Colleague Person"},
                                 files={"photo": ("photo.png", photo, "image/png")})
        self.assertEqual(result.status_code, 200, result.text)
        with closing(db.connect()) as conn:
            other_basic = db.get_basic(conn, self.colleague["id"])
            original_basic = db.get_basic(conn, self.owner_id)
        self.assertNotEqual(other_basic["id"], original_basic["id"])
        self.assertNotEqual(original_basic["name"], "Colleague Person")
        url = f"/files/basic/{other_basic['id']}/photo"
        self.assertIn(url, self.other.get("/").text)
        self.assertEqual(self.other.get(url).content, photo)
        self.assertEqual(self.owner.get(url).status_code, 404)
        self.assertEqual(self.other.post("/admin/basic/photo/delete").status_code, 200)
        self.assertEqual(self.other.get(url).status_code, 404)
        self.assertFalse((self.root / "colleague" / other_basic["photo"]).exists())

    def test_ordinary_user_cannot_manage_ocr_or_accounts_and_admin_has_no_profile(self):
        for method, path, kwargs in (
            ("get", "/admin/ocr/settings", {}),
            ("post", "/admin/ocr/settings/test", {"json": {}}),
            ("put", "/admin/ocr/settings", {"json": {}}),
            ("get", "/admin/users", {}),
            ("post", "/admin/users", {"data": {"username": "intruder", "password": "password123"}}),
            ("post", f"/admin/users/{self.colleague['id']}/edit", {"data": {"username": "stolen"}}),
            ("post", f"/admin/users/{self.colleague['id']}/delete", {"data": {"confirm_username": "colleague"}}),
        ):
            with self.subTest(path=path, method=method):
                self.assertEqual(getattr(self.owner, method)(path, **kwargs).status_code, 403)
        self.assertNotIn("ocr-settings-form", self.owner.get("/admin").text)
        self.assertIn("ocr-settings-form", self.admin.get("/admin").text)
        self.assertEqual(self.admin.post("/admin/basic", data={"name": "admin"}).status_code, 403)
        self.assertEqual(self.admin.get("/files/basic/1/photo").status_code, 403)

    def test_password_reset_rename_and_delete_revoke_existing_sessions(self):
        with closing(db.connect()) as conn:
            db.update_user(conn, self.colleague["id"], "colleague", "changed-password")
        self.assertEqual(self.other.get("/admin").status_code, 401)
        failed = self.other.post("/login", data={"username": "colleague", "password": "colleague-password"},
                                 follow_redirects=False)
        self.assertNotEqual(failed.status_code, 303)
        renewed = http_client(self, "colleague", "changed-password")
        with closing(db.connect()) as conn:
            db.update_user(conn, self.colleague["id"], "renamed")
        self.assertEqual(renewed.get("/admin").status_code, 401)
        renamed = http_client(self, "renamed", "changed-password")
        with closing(db.connect()) as conn:
            db.delete_user(conn, self.colleague["id"])
        self.assertEqual(renamed.get("/admin").status_code, 401)
        db.init_db()
        with closing(db.connect()) as conn:
            self.assertIsNone(db.get_user(conn, self.colleague["id"]))

    def test_legacy_bearer_is_read_only_and_deleted_owner_revokes_it(self):
        headers = {"Authorization": f"Bearer {MCP_TOKEN}"}
        anonymous = http_client(self, username=None)
        for method, path in (("get", "/admin"), ("get", "/admin/ocr/settings"),
                             ("post", "/admin/work"), ("get", "/")):
            response = getattr(anonymous, method)(path, headers=headers)
            self.assertIn(response.status_code, (401, 403))
        pdf = b"%PDF-1.4\nowned"
        self.owner.post("/admin/projects", files={"contract": ("a.pdf", pdf, "application/pdf")})
        with closing(db.connect()) as conn:
            row, = db.list_rows(conn, SECTIONS["projects"], self.owner_id)
        url = f"/files/projects/{row['id']}/contract"
        self.assertEqual(anonymous.get(url, headers=headers).content, pdf)
        with closing(db.connect()) as conn:
            db.delete_user(conn, self.owner_id)
        self.assertEqual(anonymous.get(url, headers=headers).status_code, 401)
        db.init_db()
        self.assertNotEqual(anonymous.post("/login", data={"username": "zhangrui", "password": ADMIN_PASSWORD},
                                          follow_redirects=False).status_code, 303)

    def test_wrong_password_and_missing_username_do_not_authenticate(self):
        anonymous = http_client(self, username=None)
        for data in ({"password": ADMIN_PASSWORD}, {"username": "zhangrui", "password": "wrong"},
                     {"username": "nonexistent", "password": ADMIN_PASSWORD}):
            self.assertNotEqual(anonymous.post("/login", data=data, follow_redirects=False).status_code, 303)
            self.assertEqual(anonymous.get("/admin").status_code, 401)


    def test_admin_account_lifecycle_and_delete_confirmation(self):
        result = self.admin.post("/admin/users", data={"username": "newperson", "password": "newperson-password"})
        self.assertEqual(result.status_code, 200, result.text)
        with closing(db.connect()) as conn:
            user = db.get_user_by_username(conn, "newperson")
            admin_id = db.get_user_by_username(conn, "admin")["id"]
        account = http_client(self, "newperson", "newperson-password")
        self.assertEqual(account.post("/admin/work", data={"employer": "Retained on rename"}).status_code, 201)
        edited = self.admin.post(f"/admin/users/{user['id']}/edit",
                                 data={"username": "newname", "password": "replacement-password"})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(account.get("/admin").status_code, 401)
        renamed = http_client(self, "newname", "replacement-password")
        self.assertIn("Retained on rename", renamed.get("/").text)
        self.assertEqual(self.admin.post(f"/admin/users/{user['id']}/delete",
                                         data={"confirm_username": "wrong"}).status_code, 400)
        self.assertIn("Retained on rename", renamed.get("/").text)
        self.assertEqual(self.admin.post(f"/admin/users/{admin_id}/delete",
                                         data={"confirm_username": "admin"}).status_code, 400)
        self.assertEqual(self.admin.post(f"/admin/users/{admin_id}/edit",
                                         data={"username": "newadmin"}).status_code, 400)
        self.assertEqual(self.admin.post(f"/admin/users/{user['id']}/delete",
                                         data={"confirm_username": "newname"}).status_code, 200)
        self.assertEqual(renamed.get("/admin").status_code, 401)
        self.assertFalse((self.root / "newname").exists())

    def test_cross_origin_account_writes_are_rejected(self):
        result = self.admin.post("/admin/users", data={"username": "forged", "password": "forged-password"},
                                 headers={"Origin": "https://evil.example"})
        self.assertEqual(result.status_code, 403)
        with closing(db.connect()) as conn:
            self.assertIsNone(db.get_user_by_username(conn, "forged"))


if __name__ == "__main__":
    unittest.main()
