import webbrowser
from threading import Timer
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    send_from_directory,
)
from werkzeug.exceptions import NotFound
import sqlite3
import os
import re
import secrets
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime, timezone
import click
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "public_waste_collection_secret"
)

DATA_DIR = os.environ.get("DATA_DIR")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_DATABASE_PATH = os.environ.get(
    "LEGACY_DATABASE_PATH",
    os.environ.get(
        "DATABASE_PATH",
        os.path.join(DATA_DIR, "database.db")
        if DATA_DIR
        else os.path.join(PROJECT_DIR, "database.db")
    )
)
UPLOAD_FOLDER = os.environ.get(
    "UPLOAD_FOLDER",
    os.path.join(DATA_DIR, "uploads")
    if DATA_DIR
    else os.path.join(PROJECT_DIR, "static", "uploads")
)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

STAFF_CATEGORIES = (
    "Waste Collection Staff",
    "Sanitation Inspector",
    "Area Supervisor",
    "Waste Disposal Staff",
)
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


# =========================================================
# DATABASE CONNECTION
# =========================================================

MONGODB_URI = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
MONGODB_DATABASE = os.environ.get(
    "MONGODB_DATABASE",
    os.environ.get("MONGO_DB_NAME", "public_waste_collection")
)
_mongo_client = None


def get_db():
    global _mongo_client
    uri = app.config.get("MONGODB_URI") or MONGODB_URI
    if not uri:
        raise RuntimeError(
            "Set MONGODB_URI (or MONGO_URI) to your MongoDB Atlas connection string."
        )
    if _mongo_client is None:
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    return _mongo_client[MONGODB_DATABASE]


def next_id(database, collection_name):
    counter = database.counters.find_one_and_update(
        {"_id": collection_name},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return counter["value"]


def prepare_document(collection_name, document):
    result = dict(document)
    if "id" not in result:
        result["id"] = next_id(get_db(), collection_name)
    result["_id"] = result["id"]
    return result


def migrate_legacy_sqlite(database, source_path=None):
    source_path = source_path or LEGACY_DATABASE_PATH
    if not source_path or not os.path.isfile(source_path):
        return False

    marker_id = "sqlite-to-mongodb-v1"
    if database.migrations.find_one({"_id": marker_id}):
        return False

    legacy = sqlite3.connect(source_path)
    legacy.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"] for row in legacy.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        imported = False
        for collection_name in ("users", "reports", "ward_pincodes"):
            if collection_name not in tables:
                continue
            for row in legacy.execute(
                'SELECT * FROM "{}"'.format(collection_name)
            ):
                document = dict(row)
                document["_id"] = document["id"]
                if collection_name == "users":
                    document["email_normalized"] = document["email"].strip().lower()
                elif collection_name == "reports":
                    document.setdefault("assigned_category", None)
                    document.setdefault("after_photo", None)
                    document.setdefault(
                        "verification_status",
                        "Awaiting Proof"
                    )
                database[collection_name].update_one(
                    {"_id": document["_id"]},
                    {"$setOnInsert": document},
                    upsert=True,
                )
                imported = True
        for collection_name in ("users", "reports", "ward_pincodes"):
            highest_id = database[collection_name].find_one(
                sort=[("id", -1)]
            )
            if highest_id:
                database.counters.update_one(
                    {"_id": collection_name},
                    {"$max": {"value": highest_id["id"]}},
                    upsert=True,
                )
        database.migrations.update_one(
            {"_id": marker_id},
            {"$setOnInsert": {
                "source": os.path.abspath(source_path),
                "imported": imported,
                "completed_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        return imported
    finally:
        legacy.close()


@app.template_filter("local_datetime")
def local_datetime(value):
    if not value:
        return "—"

    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)

    return parsed.strftime("%b %d, %Y %I:%M %p")


def save_uploaded_image(upload):

    if not upload or not upload.filename:
        raise ValueError("Choose an image to upload.")

    extension = os.path.splitext(upload.filename)[1].lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Only JPG, JPEG, and PNG images are allowed.")

    signature = upload.stream.read(8)
    upload.stream.seek(0)
    is_jpeg = extension in {".jpg", ".jpeg"} and signature.startswith(b"\xff\xd8\xff")
    is_png = extension == ".png" and signature == b"\x89PNG\r\n\x1a\n"
    if not (is_jpeg or is_png):
        raise ValueError("The uploaded file is not a valid JPG, JPEG, or PNG image.")

    filename = secure_filename(f"{secrets.token_hex(16)}{extension}")
    upload.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    return filename


# =========================================================
# CREATE DATABASE
# =========================================================

def init_db():
    database = get_db()
    database.client.admin.command("ping")
    migrate_legacy_sqlite(database)

    database.users.create_index("id", unique=True)
    database.users.create_index("email_normalized", unique=True)
    database.reports.create_index("id", unique=True)
    database.reports.create_index([("user_id", 1), ("id", -1)])
    database.reports.create_index([("assigned_to", 1), ("id", -1)])
    database.ward_pincodes.create_index("id", unique=True)
    database.ward_pincodes.create_index("pincode", unique=True)

    for collection_name in ("users", "reports", "ward_pincodes"):
        highest_id = database[collection_name].find_one(sort=[("id", -1)])
        if highest_id:
            database.counters.update_one(
                {"_id": collection_name},
                {"$max": {"value": highest_id["id"]}},
                upsert=True,
            )

    defaults = (
        ("System Admin", "admin@gmail.com", "admin@123", "admin"),
        (
            "Waste Collection Staff",
            "staff@gmail.com",
            "staff@123",
            "personnel",
        ),
    )
    for name, email, password, role in defaults:
        normalized_email = email.lower()
        if not database.users.find_one({"email_normalized": normalized_email}):
            user = prepare_document("users", {
                "name": name,
                "email": normalized_email,
                "email_normalized": normalized_email,
                "password": password,
                "role": role,
            })
            database.users.update_one(
                {"email_normalized": normalized_email},
                {"$setOnInsert": user},
                upsert=True,
            )
    

@app.cli.command("area-admin-setup")
def area_admin_setup():
    """Create or replace Area Administrator credentials interactively."""

    init_db()
    database = get_db()
    admins = list(database.users.find(
        {"role": "area_admin"},
        {"id": 1}
    ).sort("id", 1))
    if len(admins) > 1:
        raise click.ClickException(
            "Multiple Area Administrator accounts exist; resolve them before setup."
        )

    email = click.prompt("Area Administrator email").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise click.ClickException("Enter a valid email address.")

    password = click.prompt(
        "New password",
        hide_input=True,
        confirmation_prompt=True
    )
    if len(password) < 12:
        raise click.ClickException(
            "Password must contain at least 12 characters."
        )

    admin_id = admins[0]["id"] if admins else None
    email_normalized = email.lower()
    existing_email = database.users.find_one({
        "email_normalized": email_normalized
    })
    if existing_email and existing_email["id"] != admin_id:
        raise click.ClickException(
            "That email is already used by another account."
        )

    if admin_id is not None:
        database.users.update_one(
            {"id": admin_id, "role": "area_admin"},
            {"$set": {
                "email": email,
                "email_normalized": email_normalized,
                "password": generate_password_hash(password),
            }}
        )
        action = "updated"
    else:
        name = click.prompt("Area Administrator name").strip()
        if not name:
            raise click.ClickException("Name cannot be empty.")
        database.users.insert_one(prepare_document("users", {
            "name": name,
            "email": email,
            "email_normalized": email_normalized,
            "password": generate_password_hash(password),
            "role": "area_admin",
        }))
        action = "created"

    click.echo(f"Area Administrator account {action}.")



# =========================================================
# HOME
# =========================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    try:
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)
    except NotFound:
        return send_from_directory(
            os.path.join(PROJECT_DIR, "static", "uploads"),
            filename
        )


# =========================================================
# REGISTER
# =========================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form["name"]
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        confirm_password = request.form.get("confirm_password", "")

        if password != confirm_password:
            flash("Passwords do not match. Please try again.")
            return render_template("register.html"), 400

        database = get_db()
        try:
            database.users.insert_one(prepare_document("users", {
                "name": name,
                "email": email,
                "email_normalized": email,
                "password": generate_password_hash(password),
                "role": "citizen",
            }))
            flash("Registration successful. Please login.")
        except DuplicateKeyError:
            flash("Email already registered.")

        return redirect(url_for("login"))

    return render_template("register.html")


# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form.get("username", request.form.get("email", "")).strip()
        login_value = username.lower()
        password = request.form["password"]

        database = get_db()
        user = database.users.find_one({"email_normalized": login_value})
        if user is None:
            user = database.users.find_one(
                {"name": re.compile("^{}$".format(re.escape(login_value)), re.IGNORECASE)},
                sort=[("id", 1)]
            )

        password_matches = False
        if user:
            stored_password = user["password"]
            if stored_password.startswith(("scrypt:", "pbkdf2:")):
                password_matches = check_password_hash(
                    stored_password,
                    password
                )
            else:
                password_matches = stored_password == password

        if user and password_matches:

            session["user_id"] = user["id"]
            session["name"] = user["name"]
            session["role"] = user["role"]

            if user["role"] == "admin":

                return redirect(
                    url_for("admin_dashboard")
                )

            elif user["role"] == "personnel":

                return redirect(
                    url_for("personnel_dashboard")
                )

            elif user["role"] == "area_admin":

                return redirect(
                    url_for("area_admin_dashboard")
                )

            else:

                return redirect(
                    url_for("dashboard")
                )

        flash("Invalid email or password.", "error")
        return render_template("login.html", login_error=True, submitted_username=username)

    return render_template("login.html")


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("index"))


@app.route("/admin/reset-citizen-password", methods=["POST"])
def reset_citizen_password():
    if session.get("role") != "admin":
        return redirect(url_for("login"))

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if password != confirm_password:
        flash("The new passwords do not match.", "error")
        return redirect(url_for("admin_dashboard"))
    if len(password) < 8:
        flash("Use at least 8 characters for the new password.", "error")
        return redirect(url_for("admin_dashboard"))

    result = get_db().users.update_one(
        {"email_normalized": email, "role": "citizen"},
        {"$set": {"password": generate_password_hash(password)}}
    )

    if result.matched_count:
        flash("Citizen password reset. Sign in with the new password.", "success")
    else:
        flash("No citizen account was found with that email.", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/area_admin_login", methods=["GET", "POST"])
def area_admin_login():
    return redirect(url_for("login"))


# =========================================================
# CITIZEN DASHBOARD
# =========================================================

@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:

        return redirect(url_for("login"))

    return render_template(
        "dashboard.html",
        name=session["name"]
    )


# =========================================================
# REPORT WASTE
# =========================================================

@app.route("/report", methods=["GET", "POST"])
def report():

    if "user_id" not in session:

        return redirect(url_for("login"))

    if request.method == "POST":

        if request.form.get("action") == "validate_pincode":

            pincode = request.form.get("pincode", "").strip()

            if is_approved_pincode(pincode):
                session["report_pincode"] = pincode
                flash(
                    "Valid Pincode! You can continue with your complaint.",
                    "success"
                )
            else:
                session.pop("report_pincode", None)
                flash(
                    "Invalid Pincode! Your area is currently not supported.",
                    "error"
                )

            return redirect(url_for("report"))

        pincode = request.form.get("pincode", "").strip()

        if not is_approved_pincode(pincode):
            session.pop("report_pincode", None)
            flash(
                "Invalid Pincode! Your area is currently not supported.",
                "error"
            )
            return redirect(url_for("report"))

        if session.get("report_pincode") != pincode:
            flash("Please verify your pincode before submitting your complaint.", "error")
            return redirect(url_for("report"))

        location = request.form["location"]

        description = request.form["description"]

        photo = request.files.get("photo")

        filename = None

        if photo and photo.filename:
            try:
                filename = save_uploaded_image(photo)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("report"))

        database = get_db()
        database.reports.insert_one(prepare_document("reports", {
            "user_id": session["user_id"],
            "location": location,
            "description": description,
            "photo": filename,
            "status": "Pending",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "assigned_to": None,
            "assigned_category": None,
            "completed_at": None,
            "after_photo": None,
            "verification_status": "Awaiting Proof",
        }))

        session.pop("report_pincode", None)
        flash("Waste report submitted successfully!")

        return redirect(
            url_for("my_reports")
        )

    pincode = session.get("report_pincode")
    if not pincode or not is_approved_pincode(pincode):
        session.pop("report_pincode", None)
        return render_template("report.html", pincode_validated=False)

    return render_template(
        "report.html",
        pincode_validated=True,
        pincode=pincode
    )


def is_approved_pincode(pincode):

    if not isinstance(pincode, str) or not re.fullmatch(r"[0-9]{6}", pincode):
        return False

    return get_db().ward_pincodes.find_one({"pincode": pincode}) is not None


def area_admin_required():

    if session.get("role") != "area_admin":
        return redirect(url_for("login"))

    return None


@app.route("/area-admin")
def area_admin_dashboard():

    access_denied = area_admin_required()
    if access_denied:
        return access_denied

    pincodes = list(get_db().ward_pincodes.find(
        {},
        {"id": 1, "pincode": 1, "city": 1, "ward_name": 1}
    ).sort([("city", 1), ("ward_name", 1), ("pincode", 1)]))

    return render_template(
        "area_admin_dashboard.html",
        pincodes=pincodes
    )


@app.route("/area-admin/pincodes", methods=["POST"])
def add_ward_pincode():

    access_denied = area_admin_required()
    if access_denied:
        return access_denied

    pincode = request.form.get("pincode", "").strip()
    city = request.form.get("city", "").strip()
    ward_name = request.form.get("ward_name", "").strip()

    if not re.fullmatch(r"[0-9]{6}", pincode):
        flash("Enter a valid six-digit pincode.", "error")
        return redirect(url_for("area_admin_dashboard"))

    if not city or not ward_name:
        flash("City and ward name are required.", "error")
        return redirect(url_for("area_admin_dashboard"))

    database = get_db()
    try:
        database.ward_pincodes.insert_one(prepare_document("ward_pincodes", {
            "pincode": pincode,
            "city": city,
            "ward_name": ward_name,
            "added_by": session["user_id"],
        }))
    except DuplicateKeyError:
        flash("This pincode has already been added.", "error")
        return redirect(url_for("area_admin_dashboard"))

    flash("Pincode added successfully.", "success")
    return redirect(url_for("area_admin_dashboard"))


@app.route("/area-admin/pincodes/<int:pincode_id>/delete", methods=["POST"])
def delete_ward_pincode(pincode_id):

    access_denied = area_admin_required()
    if access_denied:
        return access_denied

    result = get_db().ward_pincodes.delete_one({"id": pincode_id})

    if result.deleted_count:
        flash("Pincode deleted successfully.", "success")
    else:
        flash("Pincode was not found.", "error")

    return redirect(url_for("area_admin_dashboard"))


# =========================================================
# MY REPORTS
# =========================================================

@app.route("/my_reports")
def my_reports():

    if "user_id" not in session:

        return redirect(url_for("login"))

    reports = list(get_db().reports.find(
        {"user_id": session["user_id"]}
    ).sort("id", -1))

    return render_template(
        "my_reports.html",
        reports=reports
    )


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin")
def admin_dashboard():

    if session.get("role") != "admin":

        return redirect(url_for("login"))

    database = get_db()
    reports = list(database.reports.find().sort("id", -1))
    users_by_id = {
        user["id"]: user
        for user in database.users.find(
            {},
            {"id": 1, "name": 1, "role": 1}
        )
    }
    for report in reports:
        citizen = users_by_id.get(report.get("user_id"))
        personnel_user = users_by_id.get(report.get("assigned_to"))
        report["citizen_name"] = citizen["name"] if citizen else None
        report["personnel_name"] = (
            personnel_user["name"] if personnel_user else None
        )

    total = len(reports)
    pending = sum(report.get("status") == "Pending" for report in reports)
    assigned = sum(report.get("status") == "Assigned" for report in reports)
    progress = sum(report.get("status") == "In Progress" for report in reports)
    completed = sum(report.get("status") == "Completed" for report in reports)
    personnel = list(database.users.find({"role": "personnel"}).sort("id", 1))
    location_counts = {}
    for report in reports:
        location = report.get("location")
        location_counts[location] = location_counts.get(location, 0) + 1
    locations = [
        {"location": location, "count": count}
        for location, count in sorted(
            location_counts.items(),
            key=lambda item: (-item[1], item[0] or "")
        )
    ]

    return render_template(
        "admin_dashboard.html",
        total=total,
        pending=pending,
        assigned=assigned,
        progress=progress,
        completed=completed,
        reports=reports,
        personnel=personnel,
        staff_categories=STAFF_CATEGORIES,
        locations=locations
    )


# =========================================================
# ASSIGN PERSONNEL
# =========================================================

@app.route("/assign/<int:report_id>", methods=["POST"])
def assign(report_id):

    if session.get("role") != "admin":

        return redirect(url_for("login"))

    database = get_db()

    selected_staff = request.form.get("personnel_id", "")
    if selected_staff in STAFF_CATEGORIES:
        personnel = database.users.find_one(
            {"role": "personnel", "name": selected_staff},
            {"id": 1},
            sort=[("id", 1)]
        )

        if personnel is None:
            personnel = database.users.find_one(
                {"role": "personnel", "name": "Waste Collection Staff"},
                {"id": 1},
                sort=[("id", 1)]
            )
        if personnel is None:
            personnel = database.users.find_one(
                {"role": "personnel"},
                {"id": 1},
                sort=[("id", 1)]
            )

        if personnel is None:
            flash("No personnel account is available for assignment.")
            return redirect(url_for("admin_dashboard"))

        personnel_id = personnel["id"]
        assigned_category = selected_staff
    else:
        try:
            personnel_id = int(selected_staff)
        except ValueError:
            flash("Please choose a valid staff category.")
            return redirect(url_for("admin_dashboard"))

        personnel = database.users.find_one(
            {"id": personnel_id, "role": "personnel"},
            {"id": 1, "name": 1}
        )
        if personnel is None:
            flash("Please choose a valid staff account.")
            return redirect(url_for("admin_dashboard"))

        assigned_category = personnel["name"]

    report = database.reports.find_one({"id": report_id})
    if report:
        verification_status = (
            "Proof Uploaded – Awaiting Admin Review"
            if report.get("after_photo")
            else "Awaiting Proof"
        )
        database.reports.update_one(
            {"id": report_id},
            {"$set": {
                "assigned_to": personnel_id,
                "status": "Assigned",
                "assigned_category": assigned_category,
                "verification_status": verification_status,
                "completed_at": None,
            }}
        )

    return redirect(
        url_for("admin_dashboard")
    )


# =========================================================
# PERSONNEL DASHBOARD
# =========================================================

@app.route("/personnel")
def personnel_dashboard():

    if session.get("role") != "personnel":

        return redirect(url_for("login"))

    database = get_db()
    reports = list(database.reports.find(
        {"assigned_to": session["user_id"]}
    ).sort("id", -1))
    users_by_id = {
        user["id"]: user
        for user in database.users.find({}, {"id": 1, "name": 1})
    }
    for report in reports:
        citizen = users_by_id.get(report.get("user_id"))
        report["citizen_name"] = citizen["name"] if citizen else None

    return render_template(
        "personnel_dashboard.html",
        reports=reports
    )


# =========================================================
# UPDATE STATUS
# =========================================================

@app.route(
    "/update_status/<int:report_id>",
    methods=["POST"]
)
def update_status(report_id):

    if session.get("role") != "personnel":

        return redirect(url_for("login"))

    requested_status = request.form.get("status", "")
    if requested_status not in {"Assigned", "In Progress", "Completed"}:
        flash("Please choose a valid work status.", "error")
        return redirect(url_for("personnel_dashboard"))

    completed_at = (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if requested_status == "Completed"
        else None
    )
    database = get_db()
    if requested_status == "Completed":
        report = database.reports.find_one(
            {"id": report_id, "assigned_to": session["user_id"]},
            {"after_photo": 1}
        )
        if report is None:
            flash("This complaint is not assigned to your account.", "error")
            return redirect(url_for("personnel_dashboard"))
        if not report["after_photo"]:
            flash(
                "Upload an after-cleaning photo before marking this complaint completed.",
                "error"
            )
            return redirect(url_for("personnel_dashboard"))

    result = database.reports.update_one(
        {"id": report_id, "assigned_to": session["user_id"]},
        {"$set": {
            "status": requested_status,
            "completed_at": completed_at,
        }}
    )

    if not result.matched_count:
        flash("This complaint is not assigned to your account.", "error")
        return redirect(url_for("personnel_dashboard"))

    flash("Complaint status updated successfully.", "success")

    return redirect(
        url_for("personnel_dashboard")
    )


@app.route("/upload_after_photo/<int:report_id>", methods=["POST"])
def upload_after_photo(report_id):

    if session.get("role") != "personnel":
        return redirect(url_for("login"))

    database = get_db()
    try:
        filename = save_uploaded_image(request.files.get("after_photo"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("personnel_dashboard"))

    result = database.reports.update_one(
        {"id": report_id, "assigned_to": session["user_id"]},
        {"$set": {
            "after_photo": filename,
            "verification_status": "Proof Uploaded – Awaiting Admin Review",
        }}
    )

    if result.matched_count:
        flash("AFTER cleaning photograph uploaded successfully.", "success")
    else:
        flash("The complaint is no longer available for an image upload.", "error")

    return redirect(url_for("personnel_dashboard"))


@app.route("/admin/verify/<int:report_id>", methods=["POST"])
def verify_cleaning(report_id):

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    result = get_db().reports.update_one(
        {
            "id": report_id,
            "status": "Completed",
            "after_photo": {"$exists": True, "$nin": [None, ""]},
        },
        {"$set": {"verification_status": "Verified by Admin"}}
    )

    if result.matched_count:
        flash("Cleaning evidence verified successfully.", "success")
    else:
        flash(
            "Only completed complaints with an AFTER photograph can be verified.",
            "error"
        )
    return redirect(url_for("admin_dashboard"))


# =========================================================
# START APPLICATION
# =========================================================


if __name__ == "__main__":
    init_db()

    Timer(
        1.5,
        lambda: webbrowser.open("http://127.0.0.1:5000/")
    ).start()

    app.run(debug=True)

   
