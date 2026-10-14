from contextlib import closing
from datetime import date
import unittest

from app import db
from app.dashboard import build_dashboard
from app.sections import SECTIONS
from support import http_client, isolated_store, original_user_id
from test_zip_uploads import zip_bytes


class HonorEntryTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_store(self)
        self.owner = original_user_id()
        self.client = http_client(self)
        self.other = http_client(self, username="admin")
        self.section = SECTIONS["honors"]
        self.values = {"issuer": "颁发单位", "name": "荣誉名称", "level": "省级",
                       "year": "2026", "ownership": "个人"}

    def rows(self):
        with closing(db.connect()) as conn:
            return db.list_rows(conn, self.section, self.owner)

    def test_honor_values_year_statistics_and_owner_scoped_update(self):
        response = self.client.post("/api/entries/honors", data=self.values)
        self.assertEqual(response.status_code, 201, response.text)
        row, = self.rows()
        for key, value in self.values.items():
            self.assertEqual(row[key], int(value) if key == "year" else value)
        with closing(db.connect()) as conn:
            payload = build_dashboard(conn, self.owner, today=date(2026, 10, 14))
        honor_stats = next(item for item in payload["sections"] if item["key"] == "honors")
        self.assertEqual((honor_stats["current_count"], honor_stats["counts"]), (1, [0, 0, 1]))
        self.assertEqual(self.other.get(f"/api/entries/honors/{row['id']}/edit").status_code, 404)
        self.assertEqual(self.other.put(f"/api/entries/honors/{row['id']}", data=self.values).status_code, 404)
        for year in ("2026.5", "2026-01-01", "1899", "2101"):
            with self.subTest(year=year):
                invalid = self.client.put(f"/api/entries/honors/{row['id']}",
                                          data={**self.values, "year": year, "name": "Must not save"})
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(self.rows(), [row])
        updated = self.client.put(f"/api/entries/honors/{row['id']}", data={**self.values, "year": ""})
        self.assertEqual(updated.status_code, 200)
        with closing(db.connect()) as conn:
            payload = build_dashboard(conn, self.owner, today=date(2026, 10, 14))
        honor_stats = next(item for item in payload["sections"] if item["key"] == "honors")
        self.assertEqual((honor_stats["current_count"], honor_stats["undated_count"]), (0, 1))
        db.init_db()
        self.assertIsNone(self.rows()[0]["year"])

    def test_certificate_opt_out_archive_restore_and_permanent_delete(self):
        preference = self.client.put("/api/upload-preferences/honors/certificate", json={"enabled": False})
        self.assertEqual(preference.status_code, 200)
        payload = zip_bytes()
        created = self.client.post("/api/entries/honors", data=self.values,
            files={"certificate": ("荣誉证书.zip", payload, "application/zip")})
        self.assertEqual(created.status_code, 201, created.text)
        row, = self.rows()
        url = f"/files/honors/{row['id']}/certificate?download=1"
        self.assertEqual(self.client.get(url).content, payload)
        self.assertEqual(self.other.get(url).status_code, 404)
        self.assertEqual(self.client.post(f"/api/entries/honors/{row['id']}/archive").status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 404)
        records = {"records": [{"section": "honors", "id": row["id"]}]}
        self.assertEqual(self.client.post("/api/archive/restore", json=records).status_code, 200)
        restored, = self.rows()
        self.assertEqual((restored["id"], restored["certificate"]), (row["id"], row["certificate"]))
        self.assertEqual(self.client.get(url).content, payload)
        self.assertEqual(self.client.post(f"/api/entries/honors/{row['id']}/archive").status_code, 200)
        self.assertEqual(self.client.post("/api/archive/delete", json=records).status_code, 200)
        self.assertFalse((self.root / "zhangrui" / row["certificate"]).exists())
        with closing(db.connect()) as conn:
            self.assertIsNone(conn.execute("SELECT id FROM honors WHERE id=?", (row["id"],)).fetchone())
        self.assertEqual(self.client.get("/api/upload-preferences/honors/certificate").json(), {"enabled": False})

    def test_appointment_new_names_save_and_invalid_replacement_preserves_record(self):
        for name in ("部长", "副部长"):
            with self.subTest(name=name):
                response = self.client.post("/api/entries/appointments", data={"name": name})
                self.assertEqual(response.status_code, 201, response.text)
        with closing(db.connect()) as conn:
            rows = db.list_rows(conn, SECTIONS["appointments"], self.owner)
        self.assertEqual([row["name"] for row in rows], ["部长", "副部长"])
        response = self.client.put(f"/api/entries/appointments/{rows[0]['id']}", data={"name": "unknown role"})
        self.assertEqual(response.status_code, 400)
        with closing(db.connect()) as conn:
            self.assertEqual(db.list_rows(conn, SECTIONS["appointments"], self.owner), rows)
