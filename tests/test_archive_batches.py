import os
import unittest
from contextlib import closing
from unittest.mock import patch

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

from app import admin, db, files, management
from app.config import MCP_TOKEN
from app.sections import SECTIONS
from support import http_client, isolated_store, original_user_id


class ArchiveBatchTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_store(self)
        self.owner_id = original_user_id()
        with closing(db.connect()) as conn:
            self.other_id = db.create_user(conn, "colleague", "colleague-password")["id"]
        self.client = http_client(self)
        self.other = http_client(self, "colleague", "colleague-password")

    def record(self, section="projects", *, owner=None, archived=True, title="Private record"):
        owner = self.owner_id if owner is None else owner
        schema = SECTIONS[section]
        with closing(db.connect()) as conn:
            id = db.insert_row(conn, schema, {schema.title_field: title}, owner)
            username = db.get_user(conn, owner)["username"]
            field = next((field.key for field in schema.fields if field.type == "file"), None)
            values = {schema.title_field: title}
            if field:
                rel = f"{section}/{id}/{field}.pdf"
                directory = files.entry_directory(username, section, id)
                directory.mkdir(parents=True, exist_ok=True)
                (self.root / username / rel).write_bytes(b"%PDF-" + title.encode())
                values.update({field: rel, f"{field}_name": "attachment.pdf"})
            db.update_row(conn, schema, id, values, owner, activity_action="create")
            if archived:
                db.archive_row(conn, schema, id, owner)
        return {"section": section, "id": id}

    def post(self, action, records, client=None, **kwargs):
        return (client or self.client).post(f"/api/archive/{action}", json={"records": records}, **kwargs)

    def row(self, record, owner=None):
        with closing(db.connect()) as conn:
            return db.get_row(conn, SECTIONS[record["section"]], record["id"],
                              self.owner_id if owner is None else owner, include_archived=True)

    def path(self, record, username="zhangrui"):
        return self.root / username / record["section"] / str(record["id"])

    def snapshot(self):
        with closing(db.connect()) as conn:
            tables = [section.key for section in SECTIONS.values()]
            tables += ["record_events", "archive_deletions", "users", "ocr_settings", "table_layouts"]
            return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
                    for table in tables}

    def test_request_order_restore_appends_each_section_tail_and_preserves_downloads(self):
        first = self.record(title="First")
        paper = self.record("papers", title="Paper")
        second = self.record(title="Second")
        active = self.record(archived=False, title="Active")
        data = {tuple(record.values()): self.row(record) for record in (first, second, paper)}
        request = [second, paper, first]
        result = self.post("restore", request)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"restored": 3})
        with closing(db.connect()) as conn:
            self.assertEqual([row["id"] for row in db.list_rows(conn, SECTIONS["projects"], self.owner_id)],
                             [active["id"], second["id"], first["id"]])
            events = conn.execute("SELECT section_key, entry_id FROM record_events WHERE action='restore' ORDER BY id").fetchall()
            self.assertEqual([tuple(event) for event in events], [(record["section"], record["id"]) for record in request])
        for record in request:
            before = data[tuple(record.values())]
            after = self.row(record)
            self.assertEqual(after["id"], before["id"])
            self.assertEqual(after["created_at"], before["created_at"])
            for field in SECTIONS[record["section"]].fields:
                if field.type == "file" and before[field.key]:
                    self.assertEqual(after[field.key], before[field.key])
                    result = self.client.get(f'/files/{record["section"]}/{record["id"]}/{field.key}')
                    self.assertEqual(result.content, (self.root / "zhangrui" / before[field.key]).read_bytes())
        self.assertEqual(self.client.get("/api/archive").json(), [])
        self.assertEqual(self.client.post(f'/api/entries/projects/{first["id"]}/restore').status_code, 404)

    def test_strict_invalid_sets_leave_database_and_attachments_untouched(self):
        valid = self.record()
        active = self.record(archived=False)
        foreign = self.record("papers", owner=self.other_id)
        before = self.snapshot()
        attachment = next(self.path(valid).iterdir()).read_bytes()
        malformed = [None, [], "records", {}, {"records": []}, {"records": {}},
                     {"records": [valid], "owner": self.other_id},
                     {"records": [valid, {**valid, "owner": self.other_id}]},
                     {"records": [valid, {"section": "basic", "id": 1}]},
                     {"records": [valid, {"section": "missing", "id": 1}]},
                     {"records": [valid, {"section": [], "id": 1}]},
                     {"records": [valid, valid]}, {"records": [valid, active]}]
        malformed += [{"records": [valid, {"section": "papers", "id": id}]}
                      for id in (True, False, 0, -1, 1.0, "1", None)]
        for action in ("restore", "delete"):
            for body in malformed:
                with self.subTest(action=action, body=body):
                    response = self.client.post(f"/api/archive/{action}", json=body)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(set(response.json()), {"error"})
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(next(self.path(valid).iterdir()).read_bytes(), attachment)
            for missing in (foreign, {"section": "projects", "id": 999999},
                            {"section": "projects", "id": 2 ** 100}):
                response = self.post(action, [valid, missing])
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(self.snapshot(), before)
                self.assertTrue(self.path(valid).exists())
            for kwargs in ({"content": "{"}, {"data": {"records": "invalid"}}):
                self.assertEqual(self.client.post(f"/api/archive/{action}", **kwargs).status_code, 400)
                self.assertEqual(self.snapshot(), before)

    def test_delete_only_selected_archived_records_files_and_record_specific_history(self):
        project = self.record(title="Erased project title")
        paper = self.record("papers", title="Erased paper title")
        self.assertEqual(project["id"], paper["id"])
        foreign = self.record("papers", owner=self.other_id, title="Foreign")
        active = self.record(archived=False, title="Active")
        archived = self.record(title="Kept archive")
        kept = {tuple(record.values()): self.row(record, self.other_id if record == foreign else None)
                for record in (foreign, active, archived)}
        with closing(db.connect()) as conn:
            db._record_event(conn, SECTIONS["projects"], self.owner_id, None, "reorder")
            conn.commit()
            basic = db.get_basic(conn, self.owner_id)
            basic_directory = files.entry_directory("zhangrui", "basic", basic["id"])
            basic_directory.mkdir(parents=True, exist_ok=True)
            photo = basic_directory / "photo.png"
            photo.write_bytes(b"preserved basic photo")
            db.update_row(conn, SECTIONS["basic"], basic["id"],
                          {"photo": f'basic/{basic["id"]}/photo.png'}, self.owner_id)
            basic = db.get_basic(conn, self.owner_id)
            db.save_ocr_settings(conn, self.owner_id, "https://example.invalid/v1/chat/completions", "model", "key")
            layout = db.get_table_layout(conn, SECTIONS["projects"], self.owner_id)
            db.save_table_layout(conn, SECTIONS["projects"], self.owner_id, layout)
        unrelated_before = {key: value for key, value in self.snapshot().items()
                            if key in ("users", "ocr_settings", "table_layouts")}
        result = self.post("delete", [paper, project])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"deleted": 2})
        for record in (paper, project):
            self.assertIsNone(self.row(record))
            self.assertFalse(self.path(record).exists())
        for record in (foreign, active, archived):
            owner = self.other_id if record == foreign else None
            self.assertEqual(self.row(record, owner), kept[tuple(record.values())])
            self.assertTrue(self.path(record, "colleague" if owner else "zhangrui").exists())
        with closing(db.connect()) as conn:
            self.assertEqual(db.get_basic(conn, self.owner_id), basic)
            events = [dict(row) for row in conn.execute("SELECT * FROM record_events")]
            self.assertFalse(any(event["title"] in ("Erased project title", "Erased paper title") for event in events))
            self.assertTrue(any(event["entry_id"] is None and event["action"] == "reorder" for event in events))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_deletions").fetchone()[0], 0)
        titles = [event["title"] for event in self.client.get("/api/dashboard").json()["recent"]]
        self.assertNotIn("Erased project title", titles)
        self.assertNotIn("Erased paper title", titles)
        for action in ("restore", "delete"):
            before = self.snapshot()
            self.assertEqual(self.post(action, [archived, paper]).status_code, 404)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.post("delete", [archived], client=self.other).status_code, 404)
        self.assertEqual(photo.read_bytes(), b"preserved basic photo")
        self.assertEqual({key: value for key, value in self.snapshot().items() if key in unrelated_before},
                         unrelated_before)

    def test_restore_event_failure_rolls_back_entire_batch(self):
        first, second = self.record(), self.record("papers")
        with closing(db.connect()) as conn:
            conn.execute("CREATE TRIGGER fail_restore BEFORE INSERT ON record_events WHEN NEW.action='restore' "
                         "AND NEW.section_key='papers' BEGIN SELECT RAISE(ABORT, 'failure'); END")
            conn.commit()
        before = self.snapshot()
        response = self.post("restore", [first, second])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.path(first).exists())
        self.assertTrue(self.path(second).exists())

    def test_filesystem_failure_retains_whole_batch_and_replays_before_restore(self):
        first, second = self.record(), self.record("papers")
        before_rows = [self.row(first), self.row(second)]
        delete = files.delete_entry_dir
        def fail_second(username, section, id):
            if section == "papers":
                raise OSError("synthetic attachment failure")
            delete(username, section, id)
        with patch.object(files, "delete_entry_dir", side_effect=fail_second):
            result = self.post("delete", [first, second])
        self.assertEqual(result.status_code, 503)
        self.assertEqual(set(result.json()), {"error"})
        self.assertEqual([self.row(first), self.row(second)], before_rows)
        self.assertFalse(self.path(first).exists())
        self.assertTrue(self.path(second).exists())
        with closing(db.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_deletions").fetchone()[0], 1)
        # A pending deletion cannot be restored. Replay is idempotent for the first missing directory.
        self.assertEqual(self.post("restore", [first]).status_code, 404)
        self.assertIsNone(self.row(first))
        self.assertIsNone(self.row(second))
        self.assertFalse(self.path(second).exists())
        db.init_db()
        db.init_db()

    def test_database_failure_retains_intent_and_all_rows_history_until_recovery(self):
        first, second = self.record(), self.record("papers")
        before_rows = [self.row(first), self.row(second)]
        with closing(db.connect()) as conn:
            before_events = [tuple(row) for row in conn.execute("SELECT * FROM record_events ORDER BY id")]
            conn.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON papers BEGIN SELECT RAISE(ABORT, 'failure'); END")
            conn.commit()
        self.assertEqual(self.post("delete", [first, second]).status_code, 503)
        self.assertEqual([self.row(first), self.row(second)], before_rows)
        self.assertFalse(self.path(first).exists())
        self.assertFalse(self.path(second).exists())
        with closing(db.connect()) as conn:
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM record_events ORDER BY id")], before_events)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_deletions").fetchone()[0], 1)
            conn.execute("DROP TRIGGER fail_delete")
            conn.commit()
        db.init_db()
        self.assertIsNone(self.row(first))
        self.assertIsNone(self.row(second))
        with closing(db.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_deletions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM record_events").fetchone()[0], 0)

    def test_all_paths_are_preflighted_before_any_irreversible_delete(self):
        first = self.record()
        second = self.record("papers")
        external = self.root / "colleague" / "protected"
        external.mkdir()
        (external / "proof").write_bytes(b"do not delete")
        directory = self.path(second)
        parked = directory.with_name("saved")
        directory.rename(parked)
        directory.symlink_to(external, target_is_directory=True)
        before = self.snapshot()
        self.assertEqual(self.post("delete", [first, second]).status_code, 503)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.path(first).exists())
        self.assertEqual((external / "proof").read_bytes(), b"do not delete")
        directory.unlink()
        parked.rename(directory)
        # Symlinks inside the entry are removed as links; their targets are never traversed.
        (directory / "outside-link").symlink_to(external, target_is_directory=True)
        self.assertEqual(self.post("delete", [first, second]).status_code, 200)
        self.assertEqual((external / "proof").read_bytes(), b"do not delete")

    def test_replay_rejects_changed_symlink_and_account_rename_drains_pending_delete(self):
        deleted = self.record()
        kept = self.record(archived=False)
        with patch.object(files, "delete_entry_dir", side_effect=OSError("failure")):
            self.assertEqual(self.post("delete", [deleted]).status_code, 503)
        original = self.path(deleted)
        parked = original.with_name("parked")
        original.rename(parked)
        original.symlink_to(self.path(kept), target_is_directory=True)
        with self.assertRaises(ValueError):
            db.init_db()
        self.assertTrue(self.path(kept).exists())
        self.assertIsNotNone(self.row(deleted))
        original.unlink()
        parked.rename(original)
        with closing(db.connect()) as conn:
            db.update_user(conn, self.owner_id, "renamed", None)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_deletions").fetchone()[0], 0)
        self.assertIsNone(self.row(deleted))
        self.assertFalse(self.path(deleted, "renamed").exists())
        self.assertTrue(self.path(kept, "renamed").exists())
        self.assertFalse((self.root / "zhangrui").exists())

    def test_cookie_session_bearer_and_origin_boundaries_write_nothing(self):
        record = self.record()
        anonymous = http_client(self, username=None)
        before = self.snapshot()
        headers = {"Authorization": f"Bearer {MCP_TOKEN}"}
        for action in ("restore", "delete"):
            self.assertEqual(self.post(action, [record], client=anonymous).status_code, 401)
            self.assertEqual(self.post(action, [record], client=anonymous, headers=headers).status_code, 401)
            self.assertEqual(self.post(action, [record], headers=headers).status_code, 401)
            self.assertEqual(self.post(action, [record], headers={"Origin": "https://evil.example"}).status_code, 403)
            self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.path(record).exists())

    def test_each_batch_revalidates_session_under_write_lock(self):
        for action in ("restore", "delete"):
            with self.subTest(action=action):
                record = self.record()
                client = http_client(self)
                active = management.active_session_user
                def revoke(request, conn):
                    conn.execute("UPDATE users SET session_version=session_version+1 WHERE id=?", (self.owner_id,))
                    conn.commit()
                    return active(request, conn)
                with patch.object(admin, "active_session_user", side_effect=revoke):
                    self.assertEqual(self.post(action, [record], client=client).status_code, 401)
                self.assertIsNotNone(self.row(record)["archived_at"])
                self.assertTrue(self.path(record).exists())
                with closing(db.connect()) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_deletions").fetchone()[0], 0)

    def test_account_delete_drains_pending_record_deletion_before_owner_removal(self):
        record = self.record()
        foreign = self.record("papers", owner=self.other_id)
        with patch.object(files, "delete_entry_dir", side_effect=OSError("failure")):
            self.assertEqual(self.post("delete", [record]).status_code, 503)
        with closing(db.connect()) as conn:
            db.delete_user(conn, self.owner_id)
            self.assertIsNone(db.get_user(conn, self.owner_id))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_deletions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM storage_operations").fetchone()[0], 0)
        self.assertFalse((self.root / "zhangrui").exists())
        self.assertIsNotNone(self.row(foreign, self.other_id))
        self.assertTrue(self.path(foreign, "colleague").exists())

    def test_section_and_owner_symlinks_are_not_followed(self):
        record = self.record()
        for original in (self.root / "zhangrui" / "projects", self.root / "zhangrui"):
            with self.subTest(original=original):
                parked = original.with_name(original.name + "-parked")
                original.rename(parked)
                original.symlink_to(self.root / "colleague", target_is_directory=True)
                before = self.snapshot()
                self.assertEqual(self.post("delete", [record]).status_code, 503)
                self.assertEqual(self.snapshot(), before)
                self.assertTrue(parked.exists())
                self.assertTrue((self.root / "colleague").exists())
                original.unlink()
                parked.rename(original)

    def test_initialized_workspace_adds_journal_idempotently_without_data_changes(self):
        record = self.record()
        before_row = self.row(record)
        attachment = next(self.path(record).iterdir()).read_bytes()
        with closing(db.connect()) as conn:
            conn.execute("DROP TABLE archive_deletions")
            conn.commit()
            self.assertEqual(conn.execute("SELECT value FROM app_metadata WHERE key='workspace_schema_version'").fetchone()[0], "1")
        db.init_db()
        db.init_db()
        self.assertEqual(self.row(record), before_row)
        self.assertEqual(next(self.path(record).iterdir()).read_bytes(), attachment)
        with closing(db.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_deletions").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_foreign_pending_delete_does_not_block_owner_archive_or_account_updates(self):
        blocked = self.record("papers", owner=self.other_id, title="Blocked foreign deletion")
        restored = self.record(title="Owner restore")
        deleted = self.record("papers", title="Owner delete")
        original_delete = files.delete_entry_dir

        def fail_foreign(username, section, id):
            if username == "colleague":
                raise OSError("foreign attachment failure")
            original_delete(username, section, id)

        with patch.object(files, "delete_entry_dir", side_effect=fail_foreign):
            self.assertEqual(self.post("delete", [blocked], client=self.other).status_code, 503)
            response = self.post("restore", [restored])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"restored": 1})
            response = self.post("delete", [deleted])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"deleted": 1})
            with closing(db.connect()) as conn:
                changed = db.update_user(conn, self.owner_id, "renamed")
                self.assertEqual(changed["username"], "renamed")
                intents = conn.execute("SELECT user_id FROM archive_deletions").fetchall()
                self.assertEqual([row["user_id"] for row in intents], [self.other_id])
        self.assertIsNone(self.row(deleted))
        self.assertIsNone(self.row(restored)["archived_at"])
        self.assertTrue(self.path(restored, "renamed").exists())
        self.assertIsNotNone(self.row(blocked, self.other_id)["archived_at"])
        self.assertTrue(self.path(blocked, "colleague").exists())


if __name__ == "__main__":
    unittest.main()
