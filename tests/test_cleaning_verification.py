import io
import os
import sqlite3
import tempfile
import unittest

import app as application


JPEG_BYTES = b"\xff\xd8\xff\xe0test-image"
PNG_BYTES = b"\x89PNG\r\n\x1a\n"


class CleaningVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.temp_dir.name, "test.db")
        self.upload_path = os.path.join(self.temp_dir.name, "uploads")
        os.makedirs(self.upload_path)
        self.original_get_db = application.get_db
        self.original_upload_folder = application.app.config["UPLOAD_FOLDER"]

        def test_get_db():
            conn = sqlite3.connect(self.database_path)
            conn.row_factory = sqlite3.Row
            return conn

        application.get_db = test_get_db
        application.app.config.update(
            TESTING=True,
            UPLOAD_FOLDER=self.upload_path
        )
        application.init_db()

        conn = application.get_db()
        conn.execute(
            "INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)",
            ("Test Citizen", "citizen@example.test", "password", "citizen")
        )
        conn.execute(
            "INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)",
            ("Other Staff", "other@example.test", "password", "personnel")
        )
        admin_id = conn.execute(
            "SELECT id FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO ward_pincodes (pincode, city, ward_name, added_by) VALUES (?, ?, ?, ?)",
            ("123456", "Test City", "Test Ward", admin_id)
        )
        conn.commit()
        conn.close()
        self.client = application.app.test_client()

    def tearDown(self):
        application.get_db = self.original_get_db
        application.app.config["UPLOAD_FOLDER"] = self.original_upload_folder
        application.app.config["TESTING"] = False
        self.temp_dir.cleanup()

    def set_user(self, role, email):
        conn = application.get_db()
        user = conn.execute(
            "SELECT id, name FROM users WHERE email = ?",
            (email,)
        ).fetchone()
        conn.close()
        with self.client.session_transaction() as session:
            session["user_id"] = user["id"]
            session["name"] = user["name"]
            session["role"] = role
        return user["id"]

    def create_assigned_report(self, status="Assigned"):
        conn = application.get_db()
        citizen_id = conn.execute(
            "SELECT id FROM users WHERE email = 'citizen@example.test'"
        ).fetchone()["id"]
        staff_id = conn.execute(
            "SELECT id FROM users WHERE email = 'staff@gmail.com'"
        ).fetchone()["id"]
        cursor = conn.execute("""
            INSERT INTO reports
                (user_id, location, description, photo, status, assigned_to, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            citizen_id,
            "Test location",
            "Test waste description",
            "before.jpg",
            status,
            staff_id,
            "2026-01-01 10:00:00"
        ))
        report_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return report_id

    def test_complete_citizen_staff_admin_workflow(self):
        citizen_id = self.set_user("citizen", "citizen@example.test")
        with self.client.session_transaction() as session:
            session["report_pincode"] = "123456"

        response = self.client.post("/report", data={
            "pincode": "123456",
            "location": "Test park",
            "description": "Uncollected waste near the entrance",
            "photo": (io.BytesIO(JPEG_BYTES), "before.jpg")
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)

        conn = application.get_db()
        report = conn.execute(
            "SELECT * FROM reports WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (citizen_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(report["status"], "Pending")
        self.assertTrue(report["photo"])

        self.set_user("admin", "admin@gmail.com")
        response = self.client.post("/assign/{}".format(report["id"]), data={
            "personnel_id": str(self.get_user_id("staff@gmail.com"))
        })
        self.assertEqual(response.status_code, 302)

        self.set_user("personnel", "staff@gmail.com")
        response = self.client.post(
            "/update_status/{}".format(report["id"]),
            data={"status": "In Progress"}
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            "/upload_after_photo/{}".format(report["id"]),
            data={"after_photo": (io.BytesIO(PNG_BYTES), "after.png")},
            content_type="multipart/form-data"
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            "/update_status/{}".format(report["id"]),
            data={"status": "Completed"}
        )
        self.assertEqual(response.status_code, 302)

        conn = application.get_db()
        completed = conn.execute(
            "SELECT status, after_photo, verification_status FROM reports WHERE id = ?",
            (report["id"],)
        ).fetchone()
        conn.close()
        self.assertEqual(completed["status"], "Completed")
        self.assertTrue(completed["after_photo"])
        self.assertEqual(
            completed["verification_status"],
            "Proof Uploaded – Awaiting Admin Review"
        )

        self.set_user("admin", "admin@gmail.com")
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Cleaning Verification", response.data)
        self.assertIn(b"After cleaning</th>", response.data)
        self.assertIn(b"Completed", response.data)
        self.assertIn(completed["after_photo"].encode(), response.data)

        response = self.client.post(
            "/admin/verify/{}".format(report["id"])
        )
        self.assertEqual(response.status_code, 302)
        conn = application.get_db()
        verified = conn.execute(
            "SELECT status, verification_status FROM reports WHERE id = ?",
            (report["id"],)
        ).fetchone()
        conn.close()
        self.assertEqual(verified["status"], "Completed")
        self.assertEqual(verified["verification_status"], "Verified by Admin")

    def test_staff_can_update_status_without_after_photo(self):
        report_id = self.create_assigned_report()
        self.set_user("personnel", "staff@gmail.com")

        response = self.client.post(
            "/update_status/{}".format(report_id),
            data={"status": "In Progress"}
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            "/update_status/{}".format(report_id),
            data={"status": "Completed"},
            follow_redirects=True
        )
        self.assertEqual(response.status_code, 200)

        conn = application.get_db()
        report = conn.execute(
            "SELECT status, after_photo FROM reports WHERE id = ?",
            (report_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(report["status"], "Completed")
        self.assertIsNone(report["after_photo"])

    def test_unassigned_staff_cannot_change_status_or_upload_proof(self):
        report_id = self.create_assigned_report()
        self.set_user("personnel", "other@example.test")

        self.client.post(
            "/update_status/{}".format(report_id),
            data={"status": "In Progress"}
        )
        self.client.post(
            "/upload_after_photo/{}".format(report_id),
            data={"after_photo": (io.BytesIO(PNG_BYTES), "after.png")},
            content_type="multipart/form-data"
        )

        conn = application.get_db()
        report = conn.execute(
            "SELECT status, after_photo FROM reports WHERE id = ?",
            (report_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(report["status"], "Assigned")
        self.assertIsNone(report["after_photo"])

    def test_reassignment_requires_new_cleaning_proof(self):
        report_id = self.create_assigned_report("Completed")
        conn = application.get_db()
        conn.execute("""
            UPDATE reports
            SET after_photo = 'previous-after.jpg',
                verification_status = 'Verified by Admin',
                completed_at = '2026-01-02 10:00:00'
            WHERE id = ?
        """, (report_id,))
        conn.commit()
        conn.close()

        self.set_user("admin", "admin@gmail.com")
        other_staff_id = self.get_user_id("other@example.test")
        response = self.client.post(
            "/assign/{}".format(report_id),
            data={"personnel_id": str(other_staff_id)}
        )
        self.assertEqual(response.status_code, 302)

        conn = application.get_db()
        report = conn.execute("""
            SELECT assigned_to, status, after_photo, verification_status, completed_at
            FROM reports WHERE id = ?
        """, (report_id,)).fetchone()
        conn.close()
        self.assertEqual(report["assigned_to"], other_staff_id)
        self.assertEqual(report["status"], "Assigned")
        self.assertIsNone(report["after_photo"])
        self.assertEqual(report["verification_status"], "Awaiting Proof")
        self.assertIsNone(report["completed_at"])

    def test_migration_preserves_existing_complaint_and_before_photo(self):
        migration_path = os.path.join(self.temp_dir.name, "legacy.db")
        conn = sqlite3.connect(migration_path)
        conn.execute("""
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                location TEXT NOT NULL,
                description TEXT NOT NULL,
                photo TEXT,
                status TEXT DEFAULT 'Pending',
                assigned_to INTEGER,
                created_at TEXT,
                completed_at TEXT
            )
        """)
        conn.execute("""
            INSERT INTO reports (user_id, location, description, photo, status)
            VALUES (1, 'Legacy site', 'Existing complaint', 'legacy-before.jpg', 'Pending')
        """)
        conn.commit()
        conn.close()

        current_get_db = application.get_db

        def migration_get_db():
            legacy_conn = sqlite3.connect(migration_path)
            legacy_conn.row_factory = sqlite3.Row
            return legacy_conn

        application.get_db = migration_get_db
        try:
            application.init_db()
            application.init_db()
            conn = application.get_db()
            report = conn.execute(
                "SELECT photo, after_photo, verification_status FROM reports WHERE id = 1"
            ).fetchone()
            conn.close()
        finally:
            application.get_db = current_get_db

        self.assertEqual(report["photo"], "legacy-before.jpg")
        self.assertIsNone(report["after_photo"])
        self.assertEqual(report["verification_status"], "Awaiting Proof")

    def get_user_id(self, email):
        conn = application.get_db()
        user_id = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,)
        ).fetchone()["id"]
        conn.close()
        return user_id


if __name__ == "__main__":
    unittest.main()
