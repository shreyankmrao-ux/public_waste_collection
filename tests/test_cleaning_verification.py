import io
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

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

        self.set_user("citizen", "citizen@example.test")
        citizen_reports = self.client.get("/my_reports")
        self.assertEqual(citizen_reports.status_code, 200)
        self.assertIn(b"After cleaning</th>", citizen_reports.data)
        self.assertIn(completed["after_photo"].encode(), citizen_reports.data)
        after_photo_response = self.client.get(
            "/uploads/{}".format(completed["after_photo"])
        )
        self.assertEqual(after_photo_response.status_code, 200)
        self.assertEqual(after_photo_response.data, PNG_BYTES)
        after_photo_response.close()

        self.set_user("personnel", "staff@gmail.com")
        staff_dashboard = self.client.get("/personnel")
        self.assertEqual(staff_dashboard.status_code, 200)
        self.assertIn(completed["after_photo"].encode(), staff_dashboard.data)

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

    def test_admin_and_assigned_staff_can_see_reports_without_citizen_rows(self):
        staff_id = self.get_user_id("staff@gmail.com")
        conn = application.get_db()
        cursor = conn.execute("""
            INSERT INTO reports
                (user_id, location, description, status, assigned_to, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            999999,
            "Legacy location",
            "Report with missing citizen account",
            "Assigned",
            staff_id,
            "2026-01-02 10:00:00",
            "2026-01-03 14:25:00"
        ))
        report_id = cursor.lastrowid
        conn.commit()
        conn.close()

        self.set_user("admin", "admin@gmail.com")
        admin_dashboard = self.client.get("/admin")
        self.assertEqual(admin_dashboard.status_code, 200)
        self.assertIn(f"#{report_id}".encode(), admin_dashboard.data)
        self.assertIn(b"Unknown citizen", admin_dashboard.data)
        self.assertIn(b"Reported", admin_dashboard.data)
        self.assertIn(b"Completed", admin_dashboard.data)
        self.assertIn(b"2026-01-02 10:00:00", admin_dashboard.data)
        self.assertIn(b"2026-01-03 14:25:00", admin_dashboard.data)

        self.set_user("personnel", "staff@gmail.com")
        personnel_dashboard = self.client.get("/personnel")
        self.assertEqual(personnel_dashboard.status_code, 200)
        self.assertIn(f"#{report_id}".encode(), personnel_dashboard.data)
        self.assertIn(b"Unknown citizen", personnel_dashboard.data)

    def test_assignment_sends_staff_email_using_configured_smtp(self):
        report_id = self.create_assigned_report()
        self.set_user("admin", "admin@gmail.com")
        smtp_connection = Mock()
        smtp_context = Mock()
        smtp_context.__enter__ = Mock(return_value=smtp_connection)
        smtp_context.__exit__ = Mock(return_value=False)

        with patch.dict(os.environ, {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "mailer@example.test",
            "SMTP_PASSWORD": "test-secret",
            "MAIL_FROM": "mailer@example.test",
        }), patch("app.smtplib.SMTP", return_value=smtp_context) as smtp:
            response = self.client.post(
                "/assign/{}".format(report_id),
                data={"personnel_id": str(self.get_user_id("staff@gmail.com"))},
                follow_redirects=True
            )

        self.assertEqual(response.status_code, 200)
        smtp.assert_called_once_with(
            "smtp.example.test",
            587,
            timeout=15
        )
        smtp_connection.starttls.assert_called_once()
        smtp_connection.login.assert_called_once_with(
            "mailer@example.test",
            "test-secret"
        )
        message = smtp_connection.send_message.call_args.args[0]
        self.assertEqual(message["To"], "staff@gmail.com")
        self.assertIn(f"#{report_id}", message["Subject"])
        self.assertIn("Test location", message.get_content())
        self.assertIn(
            b"staff notification email sent",
            response.data
        )

    def test_assignment_succeeds_when_smtp_is_not_configured(self):
        report_id = self.create_assigned_report()
        staff_id = self.get_user_id("staff@gmail.com")
        self.set_user("admin", "admin@gmail.com")

        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post(
                "/assign/{}".format(report_id),
                data={"personnel_id": str(staff_id)},
                follow_redirects=True
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"staff notification email was not sent",
            response.data
        )
        conn = application.get_db()
        report = conn.execute(
            "SELECT assigned_to, status FROM reports WHERE id = ?",
            (report_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(report["assigned_to"], staff_id)
        self.assertEqual(report["status"], "Assigned")

    def test_wsgi_startup_migrates_database_before_assignment(self):
        legacy_database_path = os.path.join(self.temp_dir.name, "legacy.db")
        conn = sqlite3.connect(legacy_database_path)
        conn.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL
            );
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
            );
            CREATE TABLE ward_pincodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pincode TEXT NOT NULL UNIQUE,
                city TEXT NOT NULL,
                ward_name TEXT NOT NULL,
                added_by INTEGER NOT NULL
            );
        """)
        conn.commit()
        conn.close()

        original_database_path = application.DATABASE_PATH
        mocked_get_db = application.get_db
        application.DATABASE_PATH = legacy_database_path
        application.get_db = self.original_get_db
        sys.modules.pop("wsgi", None)
        try:
            importlib.import_module("wsgi")

            conn = application.get_db()
            citizen_cursor = conn.execute(
                "INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)",
                ("Legacy Citizen", "legacy@example.test", "password", "citizen")
            )
            report_cursor = conn.execute("""
                INSERT INTO reports
                    (user_id, location, description, status)
                VALUES (?, ?, ?, ?)
            """, (citizen_cursor.lastrowid, "Legacy location", "Legacy report", "Pending"))
            report_id = report_cursor.lastrowid
            conn.commit()
            conn.close()

            self.set_user("admin", "admin@gmail.com")
            response = self.client.post(
                "/assign/{}".format(report_id),
                data={"personnel_id": str(self.get_user_id("staff@gmail.com"))}
            )
            self.assertEqual(response.status_code, 302)

            conn = application.get_db()
            report = conn.execute("""
                SELECT assigned_to, assigned_category, status
                FROM reports WHERE id = ?
            """, (report_id,)).fetchone()
            conn.close()
            self.assertEqual(report["status"], "Assigned")
            self.assertEqual(
                report["assigned_to"],
                self.get_user_id("staff@gmail.com")
            )
        finally:
            application.DATABASE_PATH = original_database_path
            application.get_db = mocked_get_db
            sys.modules.pop("wsgi", None)

    def test_staff_cannot_complete_without_after_photo(self):
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
        self.assertIn(
            b"Upload an after-cleaning photo before marking this complaint completed.",
            response.data
        )

        conn = application.get_db()
        report = conn.execute(
            "SELECT status, after_photo FROM reports WHERE id = ?",
            (report_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(report["status"], "In Progress")
        self.assertIsNone(report["after_photo"])

    def test_login_matches_email_case_insensitively_and_trims_whitespace(self):
        conn = application.get_db()
        conn.execute(
            "UPDATE users SET email = ? WHERE email = ?",
            ("Citizen@Example.Test", "citizen@example.test")
        )
        conn.commit()
        conn.close()

        response = self.client.post("/login", data={
            "email": "  CITIZEN@example.test ",
            "password": "password"
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard")
        with self.client.session_transaction() as session:
            self.assertEqual(session["role"], "citizen")

    def test_registration_normalizes_email_address(self):
        response = self.client.post("/register", data={
            "name": "New Citizen",
            "email": "  New.User@Example.Test ",
            "password": "new-password"
        })

        self.assertEqual(response.status_code, 302)
        conn = application.get_db()
        user = conn.execute(
            "SELECT email, password FROM users WHERE name = ?",
            ("New Citizen",)
        ).fetchone()
        conn.close()
        self.assertEqual(user["email"], "new.user@example.test")
        self.assertTrue(
            application.check_password_hash(user["password"], "new-password")
        )

    def test_existing_uploads_are_migrated_without_overwriting_disk_files(self):
        source_folder = os.path.join(self.temp_dir.name, "legacy-uploads")
        destination_folder = os.path.join(self.temp_dir.name, "persistent-uploads")
        os.makedirs(source_folder)
        os.makedirs(destination_folder)
        with open(os.path.join(source_folder, "legacy.jpg"), "wb") as image:
            image.write(JPEG_BYTES)
        with open(os.path.join(source_folder, "existing.jpg"), "wb") as image:
            image.write(b"old-version")
        with open(os.path.join(destination_folder, "existing.jpg"), "wb") as image:
            image.write(b"persistent-version")

        application.migrate_existing_uploads(source_folder, destination_folder)

        with open(os.path.join(destination_folder, "legacy.jpg"), "rb") as image:
            self.assertEqual(image.read(), JPEG_BYTES)
        with open(os.path.join(destination_folder, "existing.jpg"), "rb") as image:
            self.assertEqual(image.read(), b"persistent-version")

    def test_database_initialization_preserves_existing_admin_and_staff_passwords(self):
        conn = application.get_db()
        conn.execute(
            "UPDATE users SET password = ? WHERE email = ?",
            ("custom-admin-password", "admin@gmail.com")
        )
        conn.execute(
            "UPDATE users SET password = ? WHERE email = ?",
            ("custom-staff-password", "staff@gmail.com")
        )
        conn.commit()
        conn.close()

        application.init_db()

        conn = application.get_db()
        passwords = conn.execute("""
            SELECT email, password
            FROM users
            WHERE email IN ('admin@gmail.com', 'staff@gmail.com')
        """).fetchall()
        conn.close()
        self.assertEqual(
            {row["email"]: row["password"] for row in passwords},
            {
                "admin@gmail.com": "custom-admin-password",
                "staff@gmail.com": "custom-staff-password",
            }
        )

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
        self.assertEqual(report["after_photo"], "previous-after.jpg")
        self.assertEqual(
            report["verification_status"],
            "Proof Uploaded – Awaiting Admin Review"
        )
        self.assertIsNone(report["completed_at"])

        self.set_user("personnel", "other@example.test")
        staff_dashboard = self.client.get("/personnel")
        self.assertEqual(staff_dashboard.status_code, 200)
        self.assertIn(b"previous-after.jpg", staff_dashboard.data)

        self.set_user("admin", "admin@gmail.com")
        admin_dashboard = self.client.get("/admin")
        self.assertEqual(admin_dashboard.status_code, 200)
        self.assertIn(b"previous-after.jpg", admin_dashboard.data)

    def test_admin_displays_selected_assignment_category(self):
        report_id = self.create_assigned_report()
        self.set_user("admin", "admin@gmail.com")

        response = self.client.post(
            "/assign/{}".format(report_id),
            data={"personnel_id": "Sanitation Inspector"}
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Sanitation Inspector", response.data)

        conn = application.get_db()
        report = conn.execute(
            "SELECT assigned_category FROM reports WHERE id = ?",
            (report_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(report["assigned_category"], "Sanitation Inspector")

    def test_admin_dashboard_renders_staff_assignment_controls(self):
        self.create_assigned_report()
        self.set_user("admin", "admin@gmail.com")

        response = self.client.get("/admin")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Assign personnel", response.data)
        self.assertIn(b'name="personnel_id"', response.data)
        self.assertIn(b"Choose staff", response.data)
        self.assertIn(b"Sanitation Inspector", response.data)

    def test_report_numbers_are_sequential_in_each_report_list(self):
        first_report_id = self.create_assigned_report()
        second_report_id = self.create_assigned_report()
        self.assertNotEqual(first_report_id, second_report_id)

        self.set_user("admin", "admin@gmail.com")
        admin_dashboard = self.client.get("/admin")
        self.assertEqual(admin_dashboard.status_code, 200)
        self.assertIn(b"<td>#1</td>", admin_dashboard.data)
        self.assertIn(b"<td>#2</td>", admin_dashboard.data)

        self.set_user("personnel", "staff@gmail.com")
        personnel_dashboard = self.client.get("/personnel")
        self.assertEqual(personnel_dashboard.status_code, 200)
        self.assertIn(b"<td>#1</td>", personnel_dashboard.data)
        self.assertIn(b"<td>#2</td>", personnel_dashboard.data)

        self.set_user("citizen", "citizen@example.test")
        my_reports = self.client.get("/my_reports")
        self.assertEqual(my_reports.status_code, 200)
        self.assertIn(b"<td>#1</td>", my_reports.data)
        self.assertIn(b"<td>#2</td>", my_reports.data)

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
