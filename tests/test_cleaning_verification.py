import io
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import mongomock

import app as application


JPEG_BYTES = b"\xff\xd8\xff\xe0test-image"
PNG_BYTES = b"\x89PNG\r\n\x1a\ntest-image"


class MongoPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.original_client = application._mongo_client
        self.original_uri = application.MONGODB_URI
        self.original_legacy_path = application.LEGACY_DATABASE_PATH
        self.original_upload_folder = application.app.config["UPLOAD_FOLDER"]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.upload_path = os.path.join(self.temp_dir.name, "uploads")
        os.makedirs(self.upload_path)

        application._mongo_client = mongomock.MongoClient()
        application.MONGODB_URI = "mongodb://localhost/test"
        application.LEGACY_DATABASE_PATH = os.path.join(
            self.temp_dir.name,
            "no-legacy-database.db"
        )
        application.app.config.update(
            TESTING=True,
            MONGODB_URI=application.MONGODB_URI,
            UPLOAD_FOLDER=self.upload_path,
        )
        application.init_db()
        self.database = application.get_db()
        self.citizen_id = self.add_user(
            "Test Citizen",
            "citizen@example.test",
            "password",
            "citizen"
        )
        self.other_staff_id = self.add_user(
            "Other Staff",
            "other@example.test",
            "password",
            "personnel"
        )
        self.database.ward_pincodes.insert_one(
            application.prepare_document("ward_pincodes", {
                "pincode": "123456",
                "city": "Test City",
                "ward_name": "Test Ward",
                "added_by": self.get_user_id("admin@gmail.com"),
            })
        )
        self.client = application.app.test_client()

    def tearDown(self):
        application._mongo_client.close()
        application._mongo_client = self.original_client
        application.MONGODB_URI = self.original_uri
        application.LEGACY_DATABASE_PATH = self.original_legacy_path
        application.app.config.update(
            TESTING=False,
            MONGODB_URI=None,
            UPLOAD_FOLDER=self.original_upload_folder,
        )
        self.temp_dir.cleanup()

    def add_user(self, name, email, password, role):
        return self.database.users.insert_one(
            application.prepare_document("users", {
                "name": name,
                "email": email,
                "email_normalized": email.lower(),
                "password": password,
                "role": role,
            })
        ).inserted_id

    def get_user_id(self, email):
        return self.database.users.find_one(
            {"email_normalized": email.lower()}
        )["id"]

    def set_user(self, email):
        user = self.database.users.find_one(
            {"email_normalized": email.lower()}
        )
        with self.client.session_transaction() as user_session:
            user_session["user_id"] = user["id"]
            user_session["name"] = user["name"]
            user_session["role"] = user["role"]
        return user["id"]

    def create_assigned_report(self, status="Assigned"):
        return self.database.reports.insert_one(
            application.prepare_document("reports", {
                "user_id": self.citizen_id,
                "location": "Test location",
                "description": "Test waste description",
                "photo": "before.jpg",
                "status": status,
                "assigned_to": self.get_user_id("staff@gmail.com"),
                "assigned_category": "Waste Collection Staff",
                "created_at": "2026-01-01 10:00:00",
                "completed_at": None,
                "after_photo": None,
                "verification_status": "Awaiting Proof",
            })
        ).inserted_id

    def test_registration_login_and_email_normalization(self):
        response = self.client.post("/register", data={
            "name": "New Citizen",
            "email": "  New.User@Example.Test ",
            "password": "new-password",
            "confirm_password": "new-password",
        })
        self.assertEqual(response.status_code, 302)
        user = self.database.users.find_one(
            {"email_normalized": "new.user@example.test"}
        )
        self.assertTrue(
            application.check_password_hash(user["password"], "new-password")
        )
        response = self.client.post("/login", data={
            "email": " NEW.USER@EXAMPLE.TEST ",
            "password": "new-password",
        })
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as user_session:
            self.assertEqual(user_session["role"], "citizen")

    def test_report_assignment_cleaning_proof_and_admin_verification(self):
        self.set_user("citizen@example.test")
        with self.client.session_transaction() as user_session:
            user_session["report_pincode"] = "123456"
        response = self.client.post("/report", data={
            "pincode": "123456",
            "location": "Test park",
            "description": "Waste near the entrance",
            "photo": (io.BytesIO(JPEG_BYTES), "before.jpg"),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        report = self.database.reports.find_one({"user_id": self.citizen_id})
        self.assertEqual(report["status"], "Pending")
        self.assertTrue(report["photo"])

        self.set_user("admin@gmail.com")
        response = self.client.post(
            "/assign/{}".format(report["id"]),
            data={"personnel_id": str(self.get_user_id("staff@gmail.com"))}
        )
        self.assertEqual(response.status_code, 302)
        self.set_user("staff@gmail.com")
        self.client.post(
            "/update_status/{}".format(report["id"]),
            data={"status": "In Progress"}
        )
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

        self.set_user("admin@gmail.com")
        self.client.post("/admin/verify/{}".format(report["id"]))
        updated = self.database.reports.find_one({"id": report["id"]})
        self.assertEqual(updated["status"], "Completed")
        self.assertEqual(updated["verification_status"], "Verified by Admin")
        self.assertTrue(updated["after_photo"])

    def test_staff_cannot_complete_without_photo_or_update_another_staff_report(self):
        report_id = self.create_assigned_report()
        self.set_user("staff@gmail.com")
        response = self.client.post(
            "/update_status/{}".format(report_id),
            data={"status": "Completed"},
            follow_redirects=True
        )
        self.assertIn(
            b"Upload an after-cleaning photo before marking this complaint completed.",
            response.data
        )
        self.set_user("other@example.test")
        self.client.post(
            "/update_status/{}".format(report_id),
            data={"status": "In Progress"}
        )
        self.assertEqual(
            self.database.reports.find_one({"id": report_id})["status"],
            "Assigned"
        )

    def test_admin_dashboard_joins_mongo_documents_and_counts_reports(self):
        self.create_assigned_report()
        self.set_user("admin@gmail.com")
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test Citizen", response.data)
        self.assertIn(b"Waste Collection Staff", response.data)

    def test_init_preserves_credentials_and_increments_existing_ids(self):
        self.database.users.update_one(
            {"email_normalized": "admin@gmail.com"},
            {"$set": {"password": "custom-password"}}
        )
        self.database.users.insert_one({
            "_id": 80,
            "id": 80,
            "name": "High ID",
            "email": "high@example.test",
            "email_normalized": "high@example.test",
            "password": "password",
            "role": "citizen",
        })
        application.init_db()
        self.assertEqual(
            self.database.users.find_one(
                {"email_normalized": "admin@gmail.com"}
            )["password"],
            "custom-password"
        )
        inserted = self.database.users.insert_one(
            application.prepare_document("users", {
                "name": "Next",
                "email": "next@example.test",
                "email_normalized": "next@example.test",
                "password": "password",
                "role": "citizen",
            })
        )
        self.assertGreater(inserted.inserted_id, 80)

    def test_legacy_sqlite_import_preserves_data_and_is_idempotent(self):
        legacy_path = os.path.join(self.temp_dir.name, "legacy.db")
        legacy = sqlite3.connect(legacy_path)
        legacy.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT,
                email TEXT,
                password TEXT,
                role TEXT
            );
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                location TEXT,
                description TEXT,
                photo TEXT,
                status TEXT,
                assigned_to INTEGER,
                created_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE ward_pincodes (
                id INTEGER PRIMARY KEY,
                pincode TEXT,
                city TEXT,
                ward_name TEXT,
                added_by INTEGER
            );
            INSERT INTO users VALUES
                (17, 'Legacy Citizen', 'LEGACY@example.test', 'hash', 'citizen');
            INSERT INTO reports
                (id, user_id, location, description, photo, status)
                VALUES (29, 17, 'Legacy site', 'Existing report', 'before.jpg', 'Pending');
            INSERT INTO ward_pincodes VALUES
                (31, '654321', 'Old City', 'Old Ward', 1);
        """)
        legacy.commit()
        legacy.close()

        self.assertTrue(
            application.migrate_legacy_sqlite(self.database, legacy_path)
        )
        report = self.database.reports.find_one({"id": 29})
        user = self.database.users.find_one({"id": 17})
        pincode = self.database.ward_pincodes.find_one({"id": 31})
        self.assertEqual(report["photo"], "before.jpg")
        self.assertIsNone(report["after_photo"])
        self.assertEqual(report["verification_status"], "Awaiting Proof")
        self.assertEqual(user["email_normalized"], "legacy@example.test")
        self.assertEqual(pincode["ward_name"], "Old Ward")
        self.assertFalse(
            application.migrate_legacy_sqlite(self.database, legacy_path)
        )

    def test_area_admin_pincode_crud_uses_mongodb(self):
        self.add_user(
            "Area Admin",
            "area@example.test",
            "password",
            "area_admin"
        )
        self.set_user("area@example.test")
        response = self.client.post("/area-admin/pincodes", data={
            "pincode": "222333",
            "city": "New City",
            "ward_name": "New Ward",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            self.database.ward_pincodes.find_one({"pincode": "222333"})
        )


if __name__ == "__main__":
    unittest.main()
