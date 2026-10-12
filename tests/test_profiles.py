import io
import os
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from unittest.mock import patch

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

from app import accounts, db
from app.config import ADMIN_PASSWORD, MCP_TOKEN
from app.sections import SECTIONS
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

    def account_body(self, **changes):
        return {"current_password": ADMIN_PASSWORD, "username": "zhangrui",
                "new_password": "", "confirm_password": "", **changes}

    def user(self, user_id=None):
        with closing(db.connect()) as conn:
            return db.get_user(conn, user_id or self.owner_id)

    def test_resume_rows_files_and_mutations_are_owner_scoped(self):
        pdf = b"%PDF-1.4\noriginal attachment"
        created = self.owner.post("/api/entries/projects", data={"name": "Private original project"},
                                  files={"contract": ("contract.pdf", pdf, "application/pdf")})
        self.assertEqual(created.status_code, 201, created.text)
        with closing(db.connect()) as conn:
            row, = db.list_rows(conn, SECTIONS["projects"], self.owner_id)
        url = f"/files/projects/{row['id']}/contract"
        self.assertEqual(self.owner.get(url).content, pdf)
        self.assertEqual(self.other.get(url).status_code, 404)
        self.assertNotIn("Private original project", self.other.get("/resume").text)
        self.assertIn("Private original project", self.owner.get("/resume").text)
        for method, path, kwargs in (
            ("get", f"/api/entries/projects/{row['id']}/edit", {}),
            ("put", f"/api/entries/projects/{row['id']}", {"data": {"name": "stolen"}}),
            ("post", f"/api/entries/projects/{row['id']}/archive", {}),
            ("post", "/api/archive/restore", {"json": {"records": [{"section": "projects", "id": row["id"]}]}}),
        ):
            self.assertEqual(getattr(self.other, method)(path, **kwargs).status_code, 404)
        self.assertEqual(self.other.post("/api/entries/projects/reorder", json={"ids": [row["id"]]}).status_code, 400)
        with closing(db.connect()) as conn:
            self.assertEqual(db.get_row(conn, SECTIONS["projects"], row["id"], self.owner_id), row)
        self.assertEqual((self.root / "zhangrui" / row["contract"]).read_bytes(), pdf)

    def test_each_account_including_admin_has_own_workspace_and_basic(self):
        for client in (self.owner, self.other, self.admin):
            for path in ("/", "/resume", "/settings", "/settings/model", "/api/basic/edit"):
                self.assertEqual(client.get(path).status_code, 200, path)
        from test_uploads import image_bytes
        photo = image_bytes("PNG")
        result = self.other.put("/api/basic", data={"name": "Colleague Person"},
                                files={"photo": ("photo.png", photo, "image/png")})
        self.assertEqual(result.status_code, 200, result.text)
        with closing(db.connect()) as conn:
            basic = db.get_basic(conn, self.colleague["id"])
            self.assertNotEqual(basic["id"], db.get_basic(conn, self.owner_id)["id"])
        url = f"/files/basic/{basic['id']}/photo"
        self.assertEqual(self.other.get(url).content, photo)
        self.assertEqual(self.owner.get(url).status_code, 404)
        self.assertEqual(self.other.put("/api/basic", data={"name": "Colleague Person", "remove_file": "photo"}).status_code, 200)
        self.assertEqual(self.other.get(url).status_code, 404)
        self.assertFalse((self.root / "colleague" / basic["photo"]).exists())
        self.assertEqual(self.admin.put("/api/basic", data={"name": "Admin Person"}).status_code, 200)

    def test_old_management_routes_are_removed_not_privileged(self):
        for client in (self.owner, self.admin):
            for method, path in (("get", "/admin"), ("get", "/admin/users"),
                                 ("post", "/admin/users"), ("get", "/admin/ocr/settings"),
                                 ("post", f"/admin/users/{self.colleague['id']}/edit"),
                                 ("post", f"/admin/users/{self.colleague['id']}/delete")):
                self.assertEqual(getattr(client, method)(path).status_code, 404)

    def test_account_write_revalidates_session_inside_lock(self):
        from app import management
        active = management.active_session_user
        def invalidate(request, conn):
            db.update_user(conn, self.owner_id, "zhangrui", "deployment-reset-password")
            return active(request, conn)
        with patch.object(management, "active_session_user", side_effect=invalidate):
            result = self.owner.post("/settings/account", json=self.account_body(username="mustnotrename"))
        self.assertEqual(result.status_code, 401)
        self.assertEqual(set(result.json()), {"error"})
        self.assertEqual(self.user()["username"], "zhangrui")
        http_client(self, "zhangrui", "deployment-reset-password")

    def test_account_noop_preserves_hash_and_session_version(self):
        original = self.user()
        for body in (self.account_body(), self.account_body(new_password=ADMIN_PASSWORD, confirm_password=ADMIN_PASSWORD)):
            result = self.owner.post("/settings/account", json=body)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json(), {"reauthenticate": False})
            self.assertEqual(self.user(), original)
            self.assertEqual(self.owner.get("/settings/model").status_code, 200)

    def test_invalid_account_inputs_never_change_target_or_expose_password(self):
        original, colleague = self.user(), self.user(self.colleague["id"])
        cases = [self.account_body(current_password="wrong"), self.account_body(username="colleague"),
                 self.account_body(username="../unsafe"), self.account_body(username="occupied"),
                 self.account_body(new_password="short", confirm_password="short"),
                 self.account_body(new_password="long-password", confirm_password="different-password"),
                 self.account_body(current_password=None), self.account_body(user_id=self.colleague["id"]),
                 self.account_body(target_id=self.colleague["id"]), [], "not an object"]
        (self.root / "occupied").mkdir()
        for body in cases:
            with self.subTest(body=body):
                result = self.owner.post("/settings/account", json=body)
                self.assertEqual(result.status_code, 400, result.text)
                self.assertEqual(set(result.json()), {"error"})
                self.assertEqual(self.user(), original)
                self.assertEqual(self.user(self.colleague["id"]), colleague)
        for kwargs in ({"content": "{"}, {"data": self.account_body()}):
            self.assertEqual(self.owner.post("/settings/account", **kwargs).status_code, 400)

    def test_self_rename_password_change_revoke_sessions_and_preserve_files_and_token(self):
        second_session = http_client(self)
        pdf = b"%PDF-1.4\nowned"
        self.owner.post("/api/entries/projects", files={"contract": ("a.pdf", pdf, "application/pdf")})
        with closing(db.connect()) as conn:
            row, = db.list_rows(conn, SECTIONS["projects"], self.owner_id)
        original, colleague = self.user(), self.user(self.colleague["id"])
        result = self.owner.post("/settings/account", json=self.account_body(
            username="renamed", new_password="replacement-password", confirm_password="replacement-password"))
        self.assertEqual(result.json(), {"reauthenticate": True, "login_url": "/login"})
        self.assertEqual(self.owner.get("/settings/model").status_code, 401)
        self.assertEqual(second_session.get("/settings/model").status_code, 401)
        renamed = http_client(self, "renamed", "replacement-password")
        url = f"/files/projects/{row['id']}/contract"
        self.assertEqual(renamed.get(url).content, pdf)
        self.assertFalse((self.root / "zhangrui").exists())
        self.assertEqual(self.user()["session_version"], original["session_version"] + 1)
        self.assertEqual(self.user(self.colleague["id"]), colleague)
        anonymous = http_client(self, username=None)
        self.assertEqual(anonymous.get(url, headers={"Authorization": f"Bearer {MCP_TOKEN}"}).content, pdf)

    def test_cross_origin_account_writes_are_rejected(self):
        original = self.user()
        result = self.owner.post("/settings/account", json=self.account_body(username="forged"),
                                 headers={"Origin": "https://evil.example"})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.user(), original)

    def test_bearer_is_readonly_cookie_not_accepted_as_mcp(self):
        headers = {"Authorization": f"Bearer {MCP_TOKEN}"}
        anonymous = http_client(self, username=None)
        for method, path in (("get", "/settings"), ("get", "/settings/model"),
                             ("post", "/settings/account"), ("put", "/settings/model"),
                             ("post", "/settings/model/test"), ("get", "/resume"), ("get", "/")):
            self.assertEqual(getattr(anonymous, method)(path, headers=headers).status_code, 401)
        self.assertEqual(self.owner.post("/mcp", json={}).status_code, 401)

    def test_deleted_accounts_and_password_reset_revoke_sessions(self):
        with closing(db.connect()) as conn:
            db.update_user(conn, self.colleague["id"], "colleague", "changed-password")
        self.assertEqual(self.other.get("/settings/model").status_code, 401)
        renewed = http_client(self, "colleague", "changed-password")
        with closing(db.connect()) as conn:
            db.delete_user(conn, self.colleague["id"])
        self.assertEqual(renewed.get("/settings/model").status_code, 401)
        db.init_db()
        self.assertIsNone(self.user(self.colleague["id"]))

    def test_login_has_no_role_redirect_and_rejects_wrong_credentials(self):
        anonymous = http_client(self, username=None)
        result = anonymous.post("/login", data={"username": "admin", "password": "test-bootstrap-admin", "next": "/resume"}, follow_redirects=False)
        self.assertEqual(result.headers["location"], "/resume")
        for data in ({"password": ADMIN_PASSWORD}, {"username": "zhangrui", "password": "wrong"},
                     {"username": "nonexistent", "password": ADMIN_PASSWORD}):
            self.assertNotEqual(anonymous.post("/login", data=data, follow_redirects=False).status_code, 303)
            self.assertEqual(anonymous.get("/settings/model").status_code, 401)

    def test_cli_create_reset_delete_and_invalid_outcomes(self):
        def invoke(command, username, passwords=(), confirmation=""):
            output = io.StringIO()
            with patch.object(accounts, "getpass", side_effect=passwords), patch("builtins.input", return_value=confirmation), redirect_stdout(output), redirect_stderr(output):
                result = accounts.main([command, username])
            self.assertNotIn("cli-secret-password", output.getvalue())
            return result
        self.assertEqual(invoke("create", "newperson", ["cli-secret-password"] * 2), 0)
        with closing(db.connect()) as conn:
            user = db.get_user_by_username(conn, "newperson")
            self.assertIsNotNone(db.get_basic(conn, user["id"]))
            self.assertEqual(db.get_ocr_settings(conn, user["id"])["api_key"], "")
        client = http_client(self, "newperson", "cli-secret-password")
        self.assertEqual(invoke("create", "newperson"), 1)
        self.assertEqual(invoke("create", "mismatch", ["cli-secret-password", "different-password"]), 1)
        self.assertEqual(invoke("reset-password", "nonexistent"), 1)
        self.assertEqual(invoke("create", "../unsafe"), 1)
        self.assertEqual(invoke("delete", "newperson", confirmation="wrong"), 1)
        self.assertEqual(client.get("/settings/model").status_code, 200)
        self.assertEqual(invoke("reset-password", "newperson", ["replacement-password"] * 2), 0)
        self.assertEqual(client.get("/settings/model").status_code, 401)
        http_client(self, "newperson", "replacement-password")
        self.assertEqual(invoke("delete", "newperson", confirmation="newperson"), 0)
        self.assertFalse((self.root / "newperson").exists())
        with closing(db.connect()) as conn:
            self.assertIsNone(db.get_user_by_username(conn, "mismatch"))
            self.assertIsNone(db.get_user_by_username(conn, "newperson"))


if __name__ == "__main__":
    unittest.main()
