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
import shutil
import smtplib
import ssl
import tempfile
from email.message import EmailMessage
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime, timedelta, timezone
import click

app = Flask(__name__)

REPORT_DISPLAY_TIMEZONE = timezone(timedelta(hours=5, minutes=30), "IST")

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "public_waste_collection_secret"
)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR")
CONFIGURED_DATABASE_PATH = os.environ.get("DATABASE_PATH")
CONFIGURED_UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER")

if DATA_DIR:
    DATA_DIR = os.path.abspath(DATA_DIR)
    DATABASE_PATH = os.path.join(DATA_DIR, "database.db")
    UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
else:
    DATABASE_PATH = CONFIGURED_DATABASE_PATH or os.path.join(
        PROJECT_DIR,
        "database.db"
    )
    UPLOAD_FOLDER = CONFIGURED_UPLOAD_FOLDER or os.path.join(
        PROJECT_DIR,
        "static",
        "uploads"
    )

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


def copy_file_if_missing(source, destination):
    source_path = os.path.abspath(source)
    destination_path = os.path.abspath(destination)
    if source_path == destination_path or not os.path.isfile(source_path):
        return False

    destination_folder = os.path.dirname(destination_path)
    os.makedirs(destination_folder, exist_ok=True)
    if os.path.exists(destination_path):
        return False

    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".copy-",
        dir=destination_folder
    )
    os.close(descriptor)
    try:
        shutil.copy2(source_path, temporary_path)
        try:
            os.link(temporary_path, destination_path)
        except FileExistsError:
            return False
        return True
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def migrate_existing_database(source_path, destination_path):
    source_path = os.path.abspath(source_path)
    destination_path = os.path.abspath(destination_path)
    if (
        source_path == destination_path
        or not os.path.isfile(source_path)
        or os.path.exists(destination_path)
    ):
        return False

    destination_folder = os.path.dirname(destination_path)
    os.makedirs(destination_folder, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".database-",
        suffix=".db",
        dir=destination_folder
    )
    os.close(descriptor)
    try:
        source = sqlite3.connect(source_path)
        destination = sqlite3.connect(temporary_path)
        try:
            tables = {
                row[0] for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            application_tables = {"users", "reports"}.intersection(tables)
            if not application_tables or not any(
                source.execute(
                    f'SELECT 1 FROM "{table}" LIMIT 1'
                ).fetchone()
                for table in application_tables
            ):
                return False
            source.backup(destination)
        finally:
            destination.close()
            source.close()

        try:
            os.link(temporary_path, destination_path)
        except FileExistsError:
            return False
        return True
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def migrate_existing_uploads(source_folder, destination_folder):
    if os.path.abspath(source_folder) == os.path.abspath(destination_folder):
        return

    if not os.path.isdir(source_folder):
        return

    for filename in os.listdir(source_folder):
        source = os.path.join(source_folder, filename)
        destination = os.path.join(destination_folder, filename)
        if os.path.isfile(source):
            copy_file_if_missing(source, destination)


os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

if DATA_DIR:
    legacy_database_paths = (
        CONFIGURED_DATABASE_PATH,
        os.path.join(PROJECT_DIR, "database.db"),
    )
    for legacy_database_path in legacy_database_paths:
        if os.path.exists(DATABASE_PATH):
            break
        if legacy_database_path:
            migrate_existing_database(
                legacy_database_path,
                DATABASE_PATH
            )

    legacy_upload_folders = (
        CONFIGURED_UPLOAD_FOLDER,
        os.path.join(PROJECT_DIR, "static", "uploads"),
    )
    for legacy_upload_folder in legacy_upload_folders:
        if legacy_upload_folder:
            migrate_existing_uploads(legacy_upload_folder, UPLOAD_FOLDER)

STAFF_CATEGORIES = (
    "Waste Collection Staff",
    "Sanitation Inspector",
    "Area Supervisor",
    "Waste Disposal Staff",
)
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(value):
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def is_valid_email(value):
    email = normalize_email(value)
    return bool(email and EMAIL_PATTERN.fullmatch(email))


# =========================================================
# DATABASE CONNECTION
# =========================================================

def get_db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@app.template_filter("local_datetime")
def local_datetime(value):
    if not value:
        return "—"

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value

    if parsed.tzinfo is None:
        parsed = parsed.astimezone()

    return parsed.astimezone(REPORT_DISPLAY_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def send_assignment_email(recipient, report_id, location, description):
    settings = {
        "SMTP_HOST": os.environ.get("SMTP_HOST", "").strip(),
        "SMTP_USERNAME": os.environ.get("SMTP_USERNAME", "").strip(),
        "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD", ""),
        "MAIL_FROM": os.environ.get("MAIL_FROM", "").strip(),
    }
    missing_settings = [
        key for key, value in settings.items() if not value
    ]
    if missing_settings:
        app.logger.warning(
            "Assignment email not sent; missing Render environment variables: %s",
            ", ".join(missing_settings)
        )
        return False

    try:
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        use_ssl = os.environ.get(
            "SMTP_USE_SSL",
            "false"
        ).strip().lower() in {"1", "true", "yes"}
        message = EmailMessage()
        message["Subject"] = f"Waste collection report #{report_id} assigned"
        message["From"] = settings["MAIL_FROM"]
        message["To"] = recipient
        message.set_content(
            "A waste collection report has been assigned to you.\n\n"
            f"Report: #{report_id}\n"
            f"Location: {location}\n"
            f"Description: {description}\n"
        )

        if use_ssl:
            smtp_client = smtplib.SMTP_SSL(
                settings["SMTP_HOST"],
                smtp_port,
                timeout=15,
                context=ssl.create_default_context()
            )
        else:
            smtp_client = smtplib.SMTP(
                settings["SMTP_HOST"],
                smtp_port,
                timeout=15
            )

        with smtp_client as smtp:
            smtp.ehlo()
            if not use_ssl:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(
                settings["SMTP_USERNAME"],
                settings["SMTP_PASSWORD"]
            )
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError):
        app.logger.exception("Failed to send staff assignment email")
        return False

    return True


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

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                location TEXT NOT NULL,
                description TEXT NOT NULL,
                photo TEXT,
                status TEXT DEFAULT 'Pending',
                assigned_to INTEGER,
                created_at TEXT,
                completed_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(assigned_to) REFERENCES users(id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ward_pincodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pincode TEXT NOT NULL UNIQUE
                    CHECK (length(pincode) = 6 AND pincode NOT GLOB '*[^0-9]*'),
                city TEXT NOT NULL,
                ward_name TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                FOREIGN KEY(added_by) REFERENCES users(id)
            )
        """)

        report_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(reports)")
        }
        if "assigned_category" not in report_columns:
            conn.execute("ALTER TABLE reports ADD COLUMN assigned_category TEXT")
            conn.execute("""
                UPDATE reports
                SET assigned_category = (
                    SELECT name FROM users WHERE users.id = reports.assigned_to
                )
                WHERE assigned_to IS NOT NULL
            """)
            report_columns.add("assigned_category")
        if "after_photo" not in report_columns:
            conn.execute("ALTER TABLE reports ADD COLUMN after_photo TEXT")
        if "verification_status" not in report_columns:
            conn.execute("""
                ALTER TABLE reports
                ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'Awaiting Proof'
            """)


        admin = conn.execute(
            "SELECT * FROM users WHERE email = ?",
            ("admin@gmail.com",)
        ).fetchone()
        if admin is None:
            conn.execute("""
                INSERT INTO users
                (name, email, password, role)
                VALUES (?, ?, ?, ?)
            """, (
                "System Admin",
                "admin@gmail.com",
                "admin@123",
                "admin"
            ))

        staff = conn.execute(
            "SELECT * FROM users WHERE email = ?",
            ("staff@gmail.com",)
        ).fetchone()
        if staff is None:
            conn.execute("""
                INSERT INTO users
                (name, email, password, role)
                VALUES (?, ?, ?, ?)
            """, (
                "Waste Collection Staff",
                "staff@gmail.com",
                "staff@123",
                "personnel"
            ))

        conn.commit()
    finally:
        conn.close()
    

init_db()


@app.cli.command("area-admin-setup")
def area_admin_setup():
    """Create or replace Area Administrator credentials interactively."""

    conn = get_db()
    try:
        tables = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "users" not in tables:
            raise click.ClickException(
                "The users table does not exist. Initialize the application database first."
            )

        admins = conn.execute("""
            SELECT id
            FROM users
            WHERE role = 'area_admin'
            ORDER BY id
        """).fetchall()
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

        existing_email = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,)
        ).fetchone()
        admin_id = admins[0]["id"] if admins else None
        if existing_email and existing_email["id"] != admin_id:
            raise click.ClickException(
                "That email is already used by another account."
            )

        password_hash = generate_password_hash(password)
        if admin_id is not None:
            conn.execute("""
                UPDATE users
                SET email = ?, password = ?
                WHERE id = ? AND role = 'area_admin'
            """, (email, password_hash, admin_id))
            action = "updated"
        else:
            name = click.prompt("Area Administrator name").strip()
            if not name:
                raise click.ClickException("Name cannot be empty.")
            conn.execute("""
                INSERT INTO users (name, email, password, role)
                VALUES (?, ?, ?, 'area_admin')
            """, (name, email, password_hash))
            action = "created"

        conn.commit()
        click.echo(f"Area Administrator account {action}.")
    finally:
        conn.close()



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
        email = normalize_email(request.form["email"])
        password = request.form["password"]

        if not is_valid_email(email):
            flash("Enter a valid email address.")
            return redirect(url_for("register"))

        conn = get_db()

        try:

            conn.execute("""
                INSERT INTO users
                (name, email, password, role)
                VALUES (?, ?, ?, ?)
            """, (
                name,
                email,
                generate_password_hash(password),
                "citizen"
            ))

            conn.commit()

            flash("Registration successful. Please login.")

        except sqlite3.IntegrityError:

            flash("Email already registered.")

        conn.close()

        return redirect(url_for("login"))

    return render_template("register.html")


# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        identifier = normalize_email(
            request.form.get(
                "username",
                request.form.get(
                    "identifier",
                    request.form.get("email", "")
                )
            )
        )
        password = request.form.get("password", "")

        if not identifier or not password:
            flash("Invalid email or password.")
            return render_template("login.html")

        conn = get_db()

        users = conn.execute("""
            SELECT *
            FROM users
            WHERE lower(email) = ? OR lower(name) = ?
        """, (
            identifier,
            identifier,
        )).fetchall()

        conn.close()

        user = None
        for candidate in users:
            password_matches = False
            if candidate:
                stored_password = candidate["password"]
                if stored_password.startswith(("scrypt:", "pbkdf2:")):
                    password_matches = check_password_hash(
                        stored_password,
                        password
                    )
                else:
                    password_matches = stored_password == password

            if password_matches:
                user = candidate
                break

        if user:
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

        flash("Invalid email or password.")

    return render_template("login.html")


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("index"))


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

        conn = get_db()

        conn.execute("""
            INSERT INTO reports
            (
                user_id,
                location,
                description,
                photo,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            session["user_id"],
            location,
            description,
            filename,
            "Pending",
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        ))

        conn.commit()
        conn.close()

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

    conn = get_db()
    try:
        return conn.execute(
            "SELECT 1 FROM ward_pincodes WHERE pincode = ?",
            (pincode,)
        ).fetchone() is not None
    finally:
        conn.close()


def area_admin_required():

    if session.get("role") != "area_admin":
        return redirect(url_for("login"))

    return None


@app.route("/area-admin")
def area_admin_dashboard():

    access_denied = area_admin_required()
    if access_denied:
        return access_denied

    conn = get_db()
    pincodes = conn.execute("""
        SELECT id, pincode, city, ward_name
        FROM ward_pincodes
        ORDER BY city, ward_name, pincode
    """).fetchall()
    conn.close()

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

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO ward_pincodes (pincode, city, ward_name, added_by)
            VALUES (?, ?, ?, ?)
        """, (
            pincode,
            city,
            ward_name,
            session["user_id"]
        ))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        duplicate = conn.execute(
            "SELECT 1 FROM ward_pincodes WHERE pincode = ?",
            (pincode,)
        ).fetchone() is not None
        conn.close()
        if not duplicate:
            raise
        flash("This pincode has already been added.", "error")
        return redirect(url_for("area_admin_dashboard"))

    conn.close()
    flash("Pincode added successfully.", "success")
    return redirect(url_for("area_admin_dashboard"))


@app.route("/area-admin/pincodes/<int:pincode_id>/delete", methods=["POST"])
def delete_ward_pincode(pincode_id):

    access_denied = area_admin_required()
    if access_denied:
        return access_denied

    conn = get_db()
    result = conn.execute(
        "DELETE FROM ward_pincodes WHERE id = ?",
        (pincode_id,)
    )
    conn.commit()
    conn.close()

    if result.rowcount:
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

    conn = get_db()

    reports = conn.execute("""
        SELECT *
        FROM reports
        WHERE user_id = ?
        ORDER BY id DESC
    """, (
        session["user_id"],
    )).fetchall()

    conn.close()

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

    conn = get_db()

    total = conn.execute("""
        SELECT COUNT(*)
        FROM reports
    """).fetchone()[0]

    pending = conn.execute("""
        SELECT COUNT(*)
        FROM reports
        WHERE status = 'Pending'
    """).fetchone()[0]

    assigned = conn.execute("""
        SELECT COUNT(*)
        FROM reports
        WHERE status = 'Assigned'
    """).fetchone()[0]

    progress = conn.execute("""
        SELECT COUNT(*)
        FROM reports
        WHERE status = 'In Progress'
    """).fetchone()[0]

    completed = conn.execute("""
        SELECT COUNT(*)
        FROM reports
        WHERE status = 'Completed'
    """).fetchone()[0]

    reports = conn.execute("""
        SELECT
            reports.*,
            users.name AS citizen_name,
            personnel.name AS personnel_name
        FROM reports
        LEFT JOIN users
        ON reports.user_id = users.id
        LEFT JOIN users AS personnel
        ON reports.assigned_to = personnel.id
        ORDER BY reports.id DESC
    """).fetchall()

    personnel = conn.execute("""
        SELECT *
        FROM users
        WHERE role = 'personnel'
    """).fetchall()

    locations = conn.execute("""
        SELECT
            location,
            COUNT(*) AS count
        FROM reports
        GROUP BY location
        ORDER BY count DESC
    """).fetchall()

    conn.close()

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

    conn = get_db()

    selected_staff = request.form.get("personnel_id", "")
    if selected_staff in STAFF_CATEGORIES:
        personnel = conn.execute("""
            SELECT id, name, email
            FROM users
            WHERE role = 'personnel' AND name = ?
            ORDER BY id
            LIMIT 1
        """, (selected_staff,)).fetchone()

        if personnel is None:
            personnel = conn.execute("""
                SELECT id, name, email
                FROM users
                WHERE role = 'personnel'
                ORDER BY CASE WHEN name = 'Waste Collection Staff' THEN 0 ELSE 1 END, id
                LIMIT 1
            """).fetchone()

        if personnel is None:
            conn.close()
            flash("No personnel account is available for assignment.")
            return redirect(url_for("admin_dashboard"))

        personnel_id = personnel["id"]
        assigned_category = selected_staff
    else:
        try:
            personnel_id = int(selected_staff)
        except ValueError:
            conn.close()
            flash("Please choose a valid staff category.")
            return redirect(url_for("admin_dashboard"))

        personnel = conn.execute("""
            SELECT id, name, email
            FROM users
            WHERE id = ? AND role = 'personnel'
        """, (personnel_id,)).fetchone()
        if personnel is None:
            conn.close()
            flash("Please choose a valid staff account.")
            return redirect(url_for("admin_dashboard"))

        assigned_category = personnel["name"]

    report = conn.execute("""
        SELECT location, description
        FROM reports
        WHERE id = ?
    """, (report_id,)).fetchone()
    if report is None:
        conn.close()
        flash("The report could not be found.")
        return redirect(url_for("admin_dashboard"))

    conn.execute("""
        UPDATE reports
        SET
            assigned_to = ?,
            status = 'Assigned',
            assigned_category = ?,
            verification_status = CASE
                WHEN after_photo IS NOT NULL AND after_photo != ''
                THEN 'Proof Uploaded – Awaiting Admin Review'
                ELSE 'Awaiting Proof'
            END,
            completed_at = NULL
        WHERE id = ?
    """, (
        personnel_id,
        assigned_category,
        report_id
    ))

    conn.commit()
    conn.close()

    email_sent = send_assignment_email(
        personnel["email"],
        report_id,
        report["location"],
        report["description"]
    )
    if email_sent:
        flash("Report assigned and staff notification email sent.", "success")
    else:
        flash(
            "Report assigned, but the staff notification email was not sent. "
            "Check the SMTP environment variables and Render logs.",
            "warning"
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

    conn = get_db()

    reports = conn.execute("""
        SELECT
            reports.*,
            users.name AS citizen_name
        FROM reports
        LEFT JOIN users
        ON reports.user_id = users.id
        WHERE reports.assigned_to = ?
        ORDER BY reports.id DESC
    """, (
        session["user_id"],
    )).fetchall()

    conn.close()

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
    conn = get_db()
    if requested_status == "Completed":
        report = conn.execute("""
            SELECT after_photo
            FROM reports
            WHERE id = ? AND assigned_to = ?
        """, (report_id, session["user_id"])).fetchone()
        if report is None:
            conn.close()
            flash("This complaint is not assigned to your account.", "error")
            return redirect(url_for("personnel_dashboard"))
        if not report["after_photo"]:
            conn.close()
            flash(
                "Upload an after-cleaning photo before marking this complaint completed.",
                "error"
            )
            return redirect(url_for("personnel_dashboard"))

    result = conn.execute("""
        UPDATE reports
        SET status = ?, completed_at = ?
        WHERE id = ? AND assigned_to = ?
    """, (
        requested_status,
        completed_at,
        report_id,
        session["user_id"]
    ))

    if not result.rowcount:
        conn.close()
        flash("This complaint is not assigned to your account.", "error")
        return redirect(url_for("personnel_dashboard"))

    conn.commit()
    flash("Complaint status updated successfully.", "success")
    conn.close()

    return redirect(
        url_for("personnel_dashboard")
    )


@app.route("/upload_after_photo/<int:report_id>", methods=["POST"])
def upload_after_photo(report_id):

    if session.get("role") != "personnel":
        return redirect(url_for("login"))

    conn = get_db()
    try:
        filename = save_uploaded_image(request.files.get("after_photo"))
    except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return redirect(url_for("personnel_dashboard"))

    result = conn.execute("""
        UPDATE reports
        SET after_photo = ?,
            verification_status = 'Proof Uploaded – Awaiting Admin Review'
        WHERE id = ?
            AND assigned_to = ?
    """, (filename, report_id, session["user_id"]))

    if result.rowcount:
        conn.commit()
        flash("AFTER cleaning photograph uploaded successfully.", "success")
    else:
        conn.rollback()
        flash("The complaint is no longer available for an image upload.", "error")
    conn.close()

    return redirect(url_for("personnel_dashboard"))


@app.route("/admin/verify/<int:report_id>", methods=["POST"])
def verify_cleaning(report_id):

    if session.get("role") != "admin":
        return redirect(url_for("login"))

    conn = get_db()
    result = conn.execute("""
        UPDATE reports
        SET verification_status = 'Verified by Admin'
        WHERE id = ?
            AND status = 'Completed'
            AND after_photo IS NOT NULL
            AND after_photo != ''
    """, (report_id,))

    if result.rowcount:
        conn.commit()
        flash("Cleaning evidence verified successfully.", "success")
    else:
        conn.rollback()
        flash(
            "Only completed complaints with an AFTER photograph can be verified.",
            "error"
        )
    conn.close()

    return redirect(url_for("admin_dashboard"))


# =========================================================
# START APPLICATION
# =========================================================


if __name__ == "__main__":
    Timer(
        1.5,
        lambda: webbrowser.open("http://127.0.0.1:5000/")
    ).start()

    app.run(debug=True, use_reloader=False)

   