import hashlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

from starlette.datastructures import UploadFile

from app import db, files
from app.passwords import hash_password, verify_password
from app.sections import BASIC, SECTIONS


class MultiProfileStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.object(db, "DATA_DIR", self.root))
        self.enterContext(patch.object(db, "DB_PATH", self.root / "resume.db"))
        self.enterContext(patch.object(db, "ADMIN_PASSWORD", "old"))
        self.enterContext(patch.object(db, "INITIAL_ADMIN_PASSWORD", "test-bootstrap-admin"))
        self.enterContext(patch.object(db, "MCP_TOKEN", "original-token"))
        self.work = SECTIONS["work"]
        self.projects = SECTIONS["projects"]
        self.proof = next(field for field in self.projects.fields if field.key == "close_proof")

    def open_db(self):
        return closing(db.connect())

    def initialize(self):
        db.init_db()
        with self.open_db() as conn:
            return db.get_user_by_username(conn, "zhangrui")

    def create_legacy_data(self):
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute(
                "CREATE TABLE basic (id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id = 1), "
                "created_at TEXT NOT NULL DEFAULT (datetime('now')), name TEXT, photo TEXT, photo_name TEXT)"
            )
            conn.execute(
                "INSERT INTO basic (id, name, photo, photo_name) VALUES (1, 'Original person', 'basic/1/photo.png', '原照片.png')"
            )
            conn.execute(
                "CREATE TABLE work (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "created_at TEXT NOT NULL DEFAULT (datetime('now')), employer TEXT, sort_order INTEGER, old_note TEXT)"
            )
            conn.executemany(
                "INSERT INTO work (id, employer, sort_order, old_note) VALUES (?, ?, ?, ?)",
                [(4, "First", 8, "retained"), (7, "Second", 3, "also retained"), (9, "Third", None, "last")],
            )
            conn.execute("INSERT INTO work (id, employer) VALUES (50, 'Deleted before migration')")
            conn.execute("DELETE FROM work WHERE id = 50")
        uploads = self.root / "uploads"
        (uploads / "basic" / "1").mkdir(parents=True)
        (uploads / "basic" / "1" / "photo.png").write_bytes(b"original-image-bytes")
        (uploads / "unreferenced.bin").write_bytes(b"keep-this-too")

    def save_proof(self, username, row_id, content=b"%PDF-1.7\nproof"):
        upload = UploadFile(filename="proof.pdf", file=io.BytesIO(content))
        self.addCleanup(upload.file.close)
        return files.save_upload(username, "projects", row_id, self.proof, upload, ".pdf")

    def test_fresh_install_creates_only_admin_and_preserves_new_profiles_on_restart(self):
        with patch.object(db, "ADMIN_PASSWORD", ""), patch.object(db, "INITIAL_ADMIN_PASSWORD", ""), \
                patch.object(db, "MCP_TOKEN", ""):
            with self.assertLogs(level="WARNING") as logs:
                db.init_db()
            password = logs.records[0].args[0]
            with self.open_db() as conn:
                admin = db.get_user_by_username(conn, "admin")
                self.assertEqual([user["username"] for user in db.list_users(conn)], ["admin"])
                self.assertNotIn("role", admin)
                self.assertTrue(verify_password(password, admin["password_hash"]))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM basic").fetchone()[0], 1)
                self.assertEqual(db.get_ocr_settings(conn, admin["id"]), db.OCR_DEFAULTS)
                self.assertTrue((self.root / "admin").is_dir())
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM mcp_credentials").fetchone()[0], 0)
                user = db.create_user(conn, "newperson", "newperson-password")
                row_id = db.insert_row(conn, self.work, {"employer": "Retained employer"}, user["id"])
            with patch.object(db, "INITIAL_ADMIN_PASSWORD", "different-password"):
                db.init_db()
            with self.open_db() as conn:
                self.assertTrue(verify_password(password, db.get_user_by_username(conn, "admin")["password_hash"]))
                self.assertEqual(db.get_row(conn, self.work, row_id, user["id"])["employer"],
                                 "Retained employer")
                self.assertEqual(db.get_basic(conn, user["id"])["user_id"], user["id"])
            self.assertFalse((self.root / "zhangrui").exists())

    def test_legacy_install_without_password_leaves_original_database_and_files_unchanged(self):
        self.create_legacy_data()
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            before = list(conn.iterdump())
        with patch.object(db, "ADMIN_PASSWORD", ""):
            with self.assertRaises(RuntimeError):
                db.init_db()
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertEqual(list(conn.iterdump()), before)
        self.assertEqual((self.root / "uploads" / "basic" / "1" / "photo.png").read_bytes(),
                         b"original-image-bytes")
        self.assertFalse((self.root / "zhangrui").exists())

    def test_orphan_legacy_uploads_require_migration_password_before_bootstrap(self):
        uploads = self.root / "uploads"
        uploads.mkdir()
        marker = uploads / "original.pdf"
        marker.write_bytes(b"%PDF-original")
        with patch.object(db, "ADMIN_PASSWORD", ""):
            with self.assertRaises(RuntimeError):
                db.init_db()
        self.assertEqual(marker.read_bytes(), b"%PDF-original")
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'users'").fetchone())

    def test_legacy_migration_retains_fields_ids_paths_order_and_unreferenced_files(self):
        self.create_legacy_data()
        original = self.initialize()
        with self.open_db() as conn:
            basic = db.get_basic(conn, original["id"])
            self.assertEqual((basic["id"], basic["name"]), (1, "Original person"))
            self.assertEqual((basic["photo"], basic["photo_name"]), ("basic/1/photo.png", "原照片.png"))
            self.assertIn("pinyin", basic)
            rows = db.list_rows(conn, self.work, original["id"])
            self.assertEqual([row["id"] for row in rows], [7, 4, 9])
            self.assertEqual([row["sort_order"] for row in rows], [3, 8, 9])
            self.assertEqual(rows[1]["old_note"], "retained")
            new_id = db.insert_row(conn, self.work, {"employer": "New"}, original["id"])
            self.assertGreater(new_id, 50)
            db.reorder_rows(conn, self.work, [9, 4, new_id, 7], original["id"])
            colleague = db.create_user(conn, "colleague", "colleague-password")
            self.assertNotEqual(db.get_basic(conn, colleague["id"])["id"], basic["id"])
        self.assertFalse((self.root / "uploads").exists())
        self.assertEqual(files.upload_path("zhangrui", basic["photo"]).read_bytes(), b"original-image-bytes")
        self.assertEqual((self.root / "zhangrui" / "unreferenced.bin").read_bytes(), b"keep-this-too")
        db.init_db()
        db.init_db()
        with self.open_db() as conn:
            self.assertEqual([row["id"] for row in db.list_rows(conn, self.work, original["id"])], [9, 4, new_id, 7])
            self.assertEqual(len(db.list_users(conn)), 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM storage_operations").fetchone()[0], 0)

    def test_legacy_schema_and_file_cutover_recovers_before_and_after_move(self):
        self.create_legacy_data()
        with patch.object(db, "_move_storage", side_effect=OSError("interrupted before rename")):
            with self.assertRaises(OSError):
                db.init_db()
        with self.open_db() as conn:
            original = db.get_user_by_username(conn, "zhangrui")
            self.assertEqual(db.get_basic(conn, original["id"])["name"], "Original person")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM storage_operations").fetchone()[0], 1)
        self.assertTrue((self.root / "uploads").exists())
        move = db._move_storage

        def move_then_interrupt(source, target):
            move(source, target)
            raise OSError("interrupted after rename")

        with patch.object(db, "_move_storage", side_effect=move_then_interrupt):
            with self.assertRaises(OSError):
                db.init_db()
        self.assertFalse((self.root / "uploads").exists())
        self.assertEqual((self.root / "zhangrui" / "unreferenced.bin").read_bytes(), b"keep-this-too")
        db.init_db()
        with self.open_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM storage_operations").fetchone()[0], 0)
            self.assertEqual(db.get_basic(conn, original["id"])["photo"], "basic/1/photo.png")

    def test_legacy_target_collision_fails_without_overwriting_or_cutting_over(self):
        self.create_legacy_data()
        (self.root / "zhangrui").mkdir()
        marker = self.root / "zhangrui" / "unrelated.txt"
        marker.write_bytes(b"unrelated")
        with self.assertRaises(ValueError):
            db.init_db()
        self.assertEqual(marker.read_bytes(), b"unrelated")
        self.assertEqual((self.root / "uploads" / "unreferenced.bin").read_bytes(), b"keep-this-too")
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertNotIn("user_id", {row[1] for row in conn.execute("PRAGMA table_info(basic)")})
            self.assertEqual(conn.execute("SELECT name FROM basic WHERE id = 1").fetchone()[0], "Original person")

    def test_legacy_upload_root_symlink_is_never_followed(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        marker = outside / "untouched"
        marker.write_bytes(b"outside")
        (self.root / "uploads").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            db.init_db()
        self.assertEqual(marker.read_bytes(), b"outside")
        self.assertTrue((self.root / "uploads").is_symlink())

    def test_bootstrap_passwords_are_salted_persistent_and_all_accounts_have_profiles(self):
        original = self.initialize()
        with self.open_db() as conn:
            admin = db.get_user_by_username(conn, "admin")
            self.assertTrue(verify_password("old", original["password_hash"]))
            self.assertTrue(verify_password("test-bootstrap-admin", admin["password_hash"]))
            self.assertFalse(verify_password("old", admin["password_hash"]))
            self.assertTrue((self.root / "admin").is_dir())
            for user in db.list_users(conn):
                self.assertNotIn("password_hash", user)
                self.assertNotIn("role", user)
            self.assertEqual(db.get_basic(conn, admin["id"])["user_id"], admin["id"])
            self.assertEqual(db.list_rows(conn, self.work, admin["id"]), [])
            own = db.insert_row(conn, self.work, {"employer": "Admin's employer"}, admin["id"])
            self.assertIsNone(db.get_row(conn, self.work, own, original["id"]))
            self.assertEqual(db.get_ocr_settings(conn, admin["id"]), db.OCR_DEFAULTS)
            token_hash = conn.execute("SELECT token_hash FROM mcp_credentials").fetchone()[0]
            self.assertEqual(token_hash, hashlib.sha256(b"original-token").hexdigest())
        with patch.object(db, "ADMIN_PASSWORD", "new-environment-password"), patch.object(db, "MCP_TOKEN", "new-token"):
            db.init_db()
        with self.open_db() as conn:
            self.assertEqual(db.get_user(conn, original["id"])["password_hash"], original["password_hash"])
            self.assertIsNone(db.get_mcp_user(conn, "new-token"))
            self.assertEqual(db.get_mcp_user(conn, "original-token")["id"], original["id"])
        first, second = hash_password("identical-password"), hash_password("identical-password")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("identical-password", first))
        for invalid in ("", "plain-password", "pbkdf2_sha256$999999999$a$b", None):
            self.assertFalse(verify_password("identical-password", invalid))

    def test_generated_admin_password_is_logged_only_on_initial_bootstrap(self):
        with patch.object(db, "INITIAL_ADMIN_PASSWORD", ""), patch.object(db.logging, "warning") as warning:
            db.init_db()
            db.init_db()
        self.assertEqual(warning.call_count, 1)
        password = warning.call_args.args[1]
        self.assertGreaterEqual(len(password), 24)
        with self.open_db() as conn:
            self.assertTrue(verify_password(password, db.get_user_by_username(conn, "admin")["password_hash"]))
            self.assertNotIn(password, "\n".join(conn.iterdump()))

    def test_crud_reorder_and_basic_are_always_owned(self):
        original = self.initialize()
        with self.open_db() as conn:
            colleague = db.create_user(conn, "colleague", "colleague-password")
            own = [db.insert_row(conn, self.work, {"employer": str(index)}, original["id"]) for index in range(2)]
            other = db.insert_row(conn, self.work, {"employer": "Private"}, colleague["id"])
            self.assertEqual(db.get_row(conn, self.work, other, colleague["id"])["sort_order"], 0)
            self.assertIsNone(db.get_row(conn, self.work, other, original["id"]))
            db.update_row(conn, self.work, other, {"employer": "Intrusion"}, original["id"])
            with self.assertRaises(ValueError):
                db.archive_row(conn, self.work, other, original["id"])
            self.assertEqual(db.get_row(conn, self.work, other, colleague["id"])["employer"], "Private")
            with self.assertRaises(ValueError):
                db.reorder_rows(conn, self.work, [*own, other], original["id"])
            db.reorder_rows(conn, self.work, list(reversed(own)), original["id"])
            self.assertEqual([row["id"] for row in db.list_rows(conn, self.work, original["id"])], list(reversed(own)))
            self.assertEqual([row["id"] for row in db.list_rows(conn, self.work, colleague["id"])], [other])
            with self.assertRaises(ValueError):
                db.insert_row(conn, self.work, {"user_id": colleague["id"]}, original["id"])
            basic = db.get_basic(conn, colleague["id"])
            db.update_row(conn, BASIC, basic["id"], {"name": "Private name"}, original["id"])
            self.assertIsNone(db.get_basic(conn, colleague["id"])["name"])
            with self.assertRaises(TypeError):
                db.list_rows(conn, self.work)
            with self.assertRaises(ValueError):
                db.list_rows(conn, self.work, True)

    def test_rename_password_reset_and_delete_cascade_only_target_profile(self):
        original = self.initialize()
        with self.open_db() as conn:
            colleague = db.create_user(conn, "colleague", "colleague-password")
            own = db.insert_row(conn, self.projects, {"name": "Original"}, original["id"])
            other = db.insert_row(conn, self.projects, {"name": "Colleague"}, colleague["id"])
            own_path, _ = self.save_proof("zhangrui", own, b"%PDF-original")
            other_path, _ = self.save_proof("colleague", other, b"%PDF-colleague")
            db.update_row(conn, self.projects, own, {"close_proof": own_path}, original["id"])
            db.update_row(conn, self.projects, other, {"close_proof": other_path}, colleague["id"])
            renamed = db.update_user(conn, original["id"], "renamed_user", "replacement-password")
            self.assertEqual(renamed["session_version"], original["session_version"] + 1)
            self.assertNotIn("password_hash", renamed)
            self.assertFalse((self.root / "zhangrui").exists())
            self.assertEqual(files.upload_path("renamed_user", own_path).read_bytes(), b"%PDF-original")
            self.assertEqual(db.get_row(conn, self.projects, own, original["id"])["close_proof"], own_path)
            self.assertEqual(db.get_mcp_user(conn, "original-token")["username"], "renamed_user")
            self.assertFalse(verify_password("old", db.get_user(conn, original["id"])["password_hash"]))
            self.assertTrue(verify_password("replacement-password", db.get_user(conn, original["id"])["password_hash"]))
            unchanged = db.update_user(conn, original["id"], "renamed_user")
            self.assertEqual(unchanged["session_version"], renamed["session_version"])
            reset = db.update_user(conn, original["id"], "renamed_user", "next-password")
            self.assertEqual(reset["session_version"], renamed["session_version"] + 1)
            db.delete_user(conn, original["id"])
            self.assertIsNone(db.get_user(conn, original["id"]))
            self.assertIsNone(db.get_mcp_user(conn, "original-token"))
            for section in SECTIONS.values():
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM "{section.key}" WHERE user_id = ?', (original["id"],)).fetchone()[0], 0)
            self.assertFalse((self.root / "renamed_user").exists())
            self.assertEqual(files.upload_path("colleague", other_path).read_bytes(), b"%PDF-colleague")
            self.assertEqual(db.get_row(conn, self.projects, other, colleague["id"])["name"], "Colleague")
        with patch.object(db, "ADMIN_PASSWORD", ""), patch.object(db, "MCP_TOKEN", ""):
            db.init_db()
        with self.open_db() as conn:
            self.assertIsNone(db.get_user_by_username(conn, "zhangrui"))
            replacement = db.create_user(conn, "zhangrui", "different-password")
            self.assertNotEqual(replacement["id"], original["id"])
            self.assertIsNone(db.get_mcp_user(conn, "original-token"))
            self.assertEqual(db.list_rows(conn, self.projects, replacement["id"]), [])

    def test_interrupted_rename_replays_once_and_interrupted_delete_finishes(self):
        original = self.initialize()
        marker = self.root / "zhangrui" / "original.bin"
        marker.write_bytes(b"retain-on-rename")
        move = db._move_storage

        def move_then_interrupt(source, target):
            move(source, target)
            raise OSError("interrupted after filesystem rename")

        with self.open_db() as conn, patch.object(db, "_move_storage", side_effect=move_then_interrupt):
            with self.assertRaises(OSError):
                db.update_user(conn, original["id"], "new_name")
        db.init_db()
        db.init_db()
        with self.open_db() as conn:
            user = db.get_user(conn, original["id"])
            self.assertEqual(user["username"], "new_name")
            self.assertEqual(user["session_version"], original["session_version"] + 1)
            self.assertEqual((self.root / "new_name" / "original.bin").read_bytes(), b"retain-on-rename")
            with patch.object(db.shutil, "rmtree", side_effect=OSError("interrupted deletion")):
                with self.assertRaises(OSError):
                    db.delete_user(conn, original["id"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM storage_operations").fetchone()[0], 1)
        db.init_db()
        with self.open_db() as conn:
            self.assertIsNone(db.get_user(conn, original["id"]))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM storage_operations").fetchone()[0], 0)
        self.assertEqual(list(self.root.glob(".deleted-user-*")), [])

    def test_managed_users_reserve_system_paths_and_allow_admin_lifecycle(self):
        original = self.initialize()
        (self.root / "orphan").mkdir()
        (self.root / "taken").write_bytes(b"file")
        (self.root / "linked").symlink_to(self.root / "orphan", target_is_directory=True)
        with self.open_db() as conn:
            for name in ("admin", "backup", "backups", "uploads", "orphan", "taken", "linked", "zhangrui",
                         "a", "Upper", "9name", "../escape", "中文用户", "bad/name", "a" * 33):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    db.create_user(conn, name, "valid-password")
            for password in ("short", "x" * 257, ""):
                with self.subTest(password_length=len(password)), self.assertRaises(ValueError):
                    db.create_user(conn, "valid_name", password)
            admin = db.get_user_by_username(conn, "admin")
            changed = db.update_user(conn, admin["id"], "changed_admin", "updated-admin-password")
            self.assertEqual(changed["session_version"], admin["session_version"] + 1)
            self.assertTrue(verify_password("updated-admin-password", db.get_user(conn, admin["id"])["password_hash"]))
            self.assertTrue((self.root / "changed_admin").is_dir())
            replacement = db.create_user(conn, "admin", "replacement-admin-password")
            db.delete_user(conn, changed["id"])
            self.assertIsNone(db.get_user(conn, changed["id"]))
            self.assertEqual(db.get_basic(conn, replacement["id"])["user_id"], replacement["id"])
            for name in ("orphan", "taken", "linked", "admin"):
                with self.subTest(rename=name), self.assertRaises(ValueError):
                    db.update_user(conn, original["id"], name)
            self.assertEqual(db.get_user(conn, original["id"])["username"], "zhangrui")

    def test_file_operations_are_tenant_scoped_and_reject_traversal_or_symlinks(self):
        original = self.initialize()
        with self.open_db() as conn:
            colleague = db.create_user(conn, "colleague", "colleague-password")
            first = db.insert_row(conn, self.projects, {}, original["id"])
            second = db.insert_row(conn, self.projects, {}, colleague["id"])
        first_path, _ = self.save_proof("zhangrui", first, b"%PDF-original")
        second_path, _ = self.save_proof("colleague", second, b"%PDF-colleague")
        files.delete_upload("zhangrui", second_path)
        self.assertEqual(files.upload_path("colleague", second_path).read_bytes(), b"%PDF-colleague")
        files.delete_entry_dir("zhangrui", "projects", second)
        self.assertTrue(files.upload_path("colleague", second_path).exists())
        self.assertEqual(files.user_directory("admin"), self.root / "admin")
        for username in ("../colleague", "/absolute", "uploads"):
            with self.subTest(username=username), self.assertRaises(ValueError):
                files.user_directory(username)
        for relative in ("../colleague/secret", "/etc/passwd", "projects/../secret", "projects//secret",
                         "projects/./secret", "projects\\secret", "projects/\0secret"):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                files.delete_upload("zhangrui", relative)
        with self.assertRaises(ValueError):
            files.delete_entry_dir("zhangrui", "../colleague", first)
        with self.assertRaises(ValueError):
            files.delete_entry_dir("zhangrui", "projects", "../colleague")
        link = self.root / "zhangrui" / "linked"
        link.symlink_to(self.root / "colleague", target_is_directory=True)
        with self.assertRaises(ValueError):
            files.upload_path("zhangrui", f"linked/{second_path}")
        destination = files.upload_path("zhangrui", first_path)
        destination.unlink()
        destination.symlink_to(files.upload_path("colleague", second_path))
        with self.assertRaises(ValueError):
            self.save_proof("zhangrui", first, b"%PDF-overwrite")
        with self.assertRaises(ValueError):
            files.delete_upload("zhangrui", first_path)
        self.assertEqual(files.upload_path("colleague", second_path).read_bytes(), b"%PDF-colleague")
        destination.unlink()
        files.delete_entry_dir("zhangrui", "projects", first)
        self.assertFalse((self.root / "zhangrui" / "projects" / str(first)).exists())
        self.assertTrue(files.upload_path("colleague", second_path).exists())

    def create_prior_workspace(self, *, multiple=False, ambiguous=False):
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, "
                "password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','user')), "
                "session_version INTEGER NOT NULL DEFAULT 1)"
            )
            users = [(20, "zhangrui", hash_password("old"), "user", 7)]
            if not ambiguous:
                users.insert(0, (10, "admin", hash_password("old-admin-password"), "admin", 4))
            if multiple or ambiguous:
                users.append((30, "colleague", hash_password("colleague-password"), "user", 2))
            conn.executemany("INSERT INTO users VALUES (?, ?, ?, ?, ?)", users)
            conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.executemany("INSERT INTO app_metadata VALUES (?, ?)",
                             [("bootstrap_complete", "1"), ("original_user_id", "20")])
            conn.execute(
                "CREATE TABLE mcp_credentials (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
                "token_hash TEXT NOT NULL UNIQUE)"
            )
            conn.execute("INSERT INTO mcp_credentials VALUES (20, ?)", (hashlib.sha256(b"original-token").hexdigest(),))
            conn.execute(
                "CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, sort_order INTEGER, "
                "name TEXT, contract TEXT, contract_name TEXT, old_note TEXT)"
            )
            original = (7, "2021-02-03 04:05:06", 20, 8, "Retained project",
                        "projects/7/contract.pdf", "original.pdf", "custom field")
            conn.execute("INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?)", original)
            conn.execute("INSERT INTO projects (id, created_at, user_id) VALUES (99, '2000-01-01', 20)")
            conn.execute("DELETE FROM projects WHERE id = 99")
            conn.execute(
                "CREATE TABLE ocr_settings (id INTEGER PRIMARY KEY CHECK(id=1), "
                "endpoint TEXT NOT NULL, model TEXT NOT NULL, api_key TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO ocr_settings VALUES (1, 'https://example.test/chat', 'vision', 'old-secret')")
        attachment = self.root / "zhangrui" / "projects" / "7" / "contract.pdf"
        attachment.parent.mkdir(parents=True)
        attachment.write_bytes(b"%PDF-retained-original")
        return users, original, attachment

    def test_workspace_upgrade_routes_single_person_config_without_changing_identity_or_records(self):
        users, original, attachment = self.create_prior_workspace()
        digest = hashlib.sha256(attachment.read_bytes()).digest()
        db.init_db()
        with self.open_db() as conn:
            self.assertNotIn("role", {row["name"] for row in conn.execute("PRAGMA table_info(users)")})
            self.assertEqual(conn.execute("SELECT value FROM app_metadata WHERE key='workspace_schema_version'").fetchone()[0], "1")
            for id, username, password_hash, role, version in users:
                user = db.get_user(conn, id)
                self.assertEqual((user["id"], user["username"], user["password_hash"], user["session_version"]),
                                 (id, username, password_hash, version))
            row = db.get_row(conn, self.projects, 7, 20)
            self.assertEqual(tuple(row[key] for key in ("id", "created_at", "user_id", "sort_order", "name",
                                                       "contract", "contract_name", "old_note")), original)
            self.assertIsNone(row["archived_at"])
            self.assertEqual(db.get_ocr_settings(conn, 20)["api_key"], "old-secret")
            self.assertEqual(db.get_ocr_settings(conn, 10), db.OCR_DEFAULTS)
            self.assertEqual(db.get_mcp_user(conn, "original-token")["id"], 20)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM record_events").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertGreater(db.insert_row(conn, self.projects, {"name": "New project"}, 20), 99)
            db.save_ocr_settings(conn, 10, "https://admin.test/chat", "own-model", "own-secret")
            db.save_ocr_settings(conn, 20, "https://person.test/chat", "person-model", "person-secret")
        db.init_db()
        db.init_db()
        with self.open_db() as conn:
            self.assertEqual(db.get_ocr_settings(conn, 10)["api_key"], "own-secret")
            self.assertEqual(db.get_ocr_settings(conn, 20)["api_key"], "person-secret")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(hashlib.sha256(attachment.read_bytes()).digest(), digest)

    def test_workspace_upgrade_routes_multiple_person_config_only_to_old_admin(self):
        self.create_prior_workspace(multiple=True)
        db.init_db()
        with self.open_db() as conn:
            self.assertEqual(db.get_ocr_settings(conn, 10)["api_key"], "old-secret")
            for owner in (20, 30):
                self.assertEqual(db.get_ocr_settings(conn, owner), db.OCR_DEFAULTS)
                self.assertEqual(db.get_basic(conn, owner)["user_id"], owner)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_ambiguous_nonempty_config_rolls_back_entire_workspace_upgrade(self):
        _, _, attachment = self.create_prior_workspace(ambiguous=True)
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            before = list(conn.iterdump())
        with self.assertRaisesRegex(RuntimeError, "无法确定旧模型配置归属"):
            db.init_db()
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertEqual(list(conn.iterdump()), before)
        self.assertEqual(attachment.read_bytes(), b"%PDF-retained-original")
        self.assertFalse((self.root / "colleague").exists())

    def test_old_sqlite_version_stops_before_opening_or_mutating_database(self):
        self.create_legacy_data()
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            before = list(conn.iterdump())
        with patch.object(db.sqlite3, "sqlite_version_info", (3, 34, 9)), patch.object(db, "connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "SQLite >= 3.35"):
                db.init_db()
            connect.assert_not_called()
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_empty_ambiguous_old_config_can_upgrade_without_assigning_secrets(self):
        self.create_prior_workspace(ambiguous=True)
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("UPDATE ocr_settings SET endpoint='', model='', api_key=''")
        db.init_db()
        with self.open_db() as conn:
            for owner in (20, 30):
                self.assertEqual(db.get_ocr_settings(conn, owner), db.OCR_DEFAULTS)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unbootstrapped_legacy_model_config_moves_to_created_original_account(self):
        self.create_legacy_data()
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute(
                "CREATE TABLE ocr_settings (id INTEGER PRIMARY KEY CHECK(id=1), "
                "endpoint TEXT NOT NULL, model TEXT NOT NULL, api_key TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO ocr_settings VALUES (1, 'https://legacy.test/chat', 'legacy', 'legacy-key')")
        user = self.initialize()
        with self.open_db() as conn:
            self.assertEqual(db.get_ocr_settings(conn, user["id"])["api_key"], "legacy-key")
            admin = db.get_user_by_username(conn, "admin")
            self.assertEqual(db.get_ocr_settings(conn, admin["id"]), db.OCR_DEFAULTS)

    def test_personal_ocr_keys_never_fall_back_to_another_account(self):
        user = self.initialize()
        with self.open_db() as conn:
            colleague = db.create_user(conn, "colleague", "colleague-password")
            db.save_ocr_settings(conn, user["id"], "https://a.test/chat", "a", "a-key")
            db.save_ocr_settings(conn, colleague["id"], "https://b.test/chat", "b", "b-key")
            db.save_ocr_settings(conn, user["id"], "https://a2.test/chat", "a2", None)
            self.assertEqual(db.get_ocr_settings(conn, user["id"])["api_key"], "a-key")
            self.assertEqual(db.get_ocr_settings(conn, colleague["id"])["api_key"], "b-key")
            db.save_ocr_settings(conn, user["id"], "https://a2.test/chat", "a2", "")
            self.assertEqual(db.get_ocr_settings(conn, user["id"])["api_key"], "")
            with self.assertRaises(TypeError):
                db.get_ocr_settings(conn)
            with self.assertRaises(ValueError):
                db.get_ocr_settings(conn, True)
            with self.assertRaises(ValueError):
                db.save_ocr_settings(conn, 999, "", "", "never-written")
            db.delete_user(conn, colleague["id"])
            self.assertIsNone(conn.execute("SELECT 1 FROM ocr_settings WHERE user_id=?", (colleague["id"],)).fetchone())


if __name__ == "__main__":
    unittest.main()
