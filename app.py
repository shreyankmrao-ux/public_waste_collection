import webbrowser
from threading import Timer
from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
import os
import re
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime
import click

app = Flask(__name__)

app.secret_key = "public_waste_collection_secret"

UPLOAD_FOLDER = "static/uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

STAFF_CATEGORIES = (
    "Waste Collection Staff",
    "Sanitation Inspector",
    "Area Supervisor",
    "Waste Disposal Staff",
)


# =========================================================
# DATABASE CONNECTION
# =========================================================

def get_db():
    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    return conn


# =========================================================
# CREATE DATABASE
# =========================================================

def init_db():

    conn = get_db()

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

    # DEFAULT ADMIN

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

    # DEFAULT PERSONNEL

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

    conn.execute(
        "UPDATE users SET password = ? WHERE email = ?",
        ("admin@123", "admin@gmail.com")
    )

    conn.execute(
        "UPDATE users SET password = ? WHERE email = ?",
        ("staff@123", "staff@gmail.com")
    )

    conn.commit()
    conn.close()
    

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


# =========================================================
# REGISTER
# =========================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form["name"]
        email = request.form["email"]
        password = request.form["password"]

        conn = get_db()

        try:

            conn.execute("""
                INSERT INTO users
                (name, email, password, role)
                VALUES (?, ?, ?, ?)
            """, (
                name,
                email,
                password,
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

        email = request.form["email"]
        password = request.form["password"]

        conn = get_db()

        user = conn.execute("""
            SELECT *
            FROM users
            WHERE email = ? AND password = ?
        """, (
            email,
            password
        )).fetchone()

        conn.close()

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

    if request.method == "POST":

        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        conn = get_db()

        user = conn.execute("""
            SELECT *
            FROM users
            WHERE email = ? AND role = 'area_admin'
        """, (
            email,
        )).fetchone()

        conn.close()

        if (
            user
            and user["password"].startswith(("scrypt:", "pbkdf2:"))
            and check_password_hash(user["password"], password)
        ):

            session["user_id"] = user["id"]
            session["name"] = user["name"]
            session["role"] = user["role"]

            return redirect(url_for("area_admin_dashboard"))

        flash("Invalid Area Administrator email or password.")

    return render_template("area_admin_login.html")


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

            original_name = secure_filename(
                photo.filename
            )

            unique_name = (
                str(datetime.now().timestamp())
                + "_"
                + original_name
            )

            photo.save(
                os.path.join(
                    app.config["UPLOAD_FOLDER"],
                    unique_name
                )
            )

            filename = unique_name

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
        return redirect(url_for("area_admin_login"))

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
            users.name AS citizen_name
        FROM reports
        JOIN users
        ON reports.user_id = users.id
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
            SELECT id
            FROM users
            WHERE role = 'personnel' AND name = ?
            ORDER BY id
            LIMIT 1
        """, (selected_staff,)).fetchone()

        if personnel is None:
            personnel = conn.execute("""
                SELECT id
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
            SELECT id, name
            FROM users
            WHERE id = ? AND role = 'personnel'
        """, (personnel_id,)).fetchone()
        if personnel is None:
            conn.close()
            flash("Please choose a valid staff account.")
            return redirect(url_for("admin_dashboard"))

        assigned_category = personnel["name"]

    conn.execute("""
        UPDATE reports
        SET
            assigned_to = ?,
            status = 'Assigned',
            assigned_category = ?
        WHERE id = ?
    """, (
        personnel_id,
        assigned_category,
        report_id
    ))

    conn.commit()
    conn.close()

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
        JOIN users
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

    status = request.form["status"]

    completed_at = None

    if status == "Completed":

        completed_at = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    conn = get_db()

    conn.execute("""
        UPDATE reports
        SET
            status = ?,
            completed_at = ?
        WHERE
            id = ?
            AND assigned_to = ?
    """, (
        status,
        completed_at,
        report_id,
        session["user_id"]
    ))

    conn.commit()
    conn.close()

    return redirect(
        url_for("personnel_dashboard")
    )


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

   