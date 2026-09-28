"""Tango — faculty invite (.ics) generator, mounted as a blueprint inside Attendancify.

Ported from the standalone Tango app (TimesPro_Faculty_Invite_WebApp_Full/app.py).
Auth is delegated to Attendancify's own session (see tango_access_required below)
instead of Tango's original standalone env-var login. Data lives in its own
tango.db file, kept separate from attendancify.db.
"""

from flask import Blueprint, render_template, request, send_file, jsonify, session, redirect, url_for, flash
import pandas as pd, os, re, tempfile, zipfile, sqlite3
from datetime import datetime
from icalendar import Calendar, Event, vCalAddress, vText
import pytz
from functools import wraps

tango_bp = Blueprint(
    "tango", __name__,
    url_prefix="/tango",
    template_folder="tango_templates",
    static_folder="tango_static",
    static_url_path="/static",
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tango.db")
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

DEFAULT_TEMPLATE_BODY = """Dear {salutation},

I hope this message finds you well.

We are delighted to extend our warm greetings to you.

We are pleased to provide you with the invitation link for an upcoming session scheduled for the {date}. Here are the details:

Batch: {programme}
Day: {day}
Topic: {topic}
Time: {session_time} (Indian Standard Time)

To join the session, kindly register in advance by clicking on the following link:

{zoom_link}

Meeting ID: {meeting_id}
Password: {passcode}

Upon completing the registration, you will receive a confirmation email that contains essential information on how to access the meeting.

Studio Location: {location}
"""

DEFAULT_SUBJECT_TEMPLATE = "{date} | Session Link_{prefix}_{programme}_{topic}"


def user_can_access_tango(email):
    """True when this account is allowed into Tango.

    Superadmin always; everyone else needs the 'Can access Tango' permission,
    whatever their role. Read from the database rather than the session so that
    granting or revoking it takes effect on the next request instead of waiting
    for the user to log out.
    """
    if session.get("role") == "superadmin":
        return True
    if not email:
        return False
    try:
        from database import get_user
        row = get_user(email)
    except Exception:
        return False
    if not row:
        return False
    user = dict(row) if hasattr(row, "keys") else row
    return bool(user.get("can_access_tango"))


def tango_access_required(f):
    """Reuses Attendancify's own session (set by its /login route) instead of a
    separate login. Tango handles faculty PII, so access is granted per account
    in Edit User rather than following from someone's role."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("logged_in"):
            if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.path.endswith("/generate"):
                return jsonify({"success": False, "error": "Unauthorized. Please log in."}), 401
            return redirect(url_for("login"))
        if not user_can_access_tango(session.get("user_id")):
            if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.path.endswith("/generate"):
                return jsonify({"success": False, "error": "You don't have access to Tango."}), 403
            flash("You don't have access to Tango.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated_function


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS faculty_gender (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL COLLATE NOCASE,
            name TEXT NOT NULL DEFAULT '',
            gender TEXT NOT NULL CHECK(gender IN ('M', 'F')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS email_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            body TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS generation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generated_at TEXT NOT NULL,
            template_name TEXT,
            prefix TEXT,
            location TEXT,
            total_rows INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS subject_line_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            body TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    gender_cols = [r["name"] for r in conn.execute("PRAGMA table_info(faculty_gender)").fetchall()]
    if "name" not in gender_cols:
        conn.execute("ALTER TABLE faculty_gender ADD COLUMN name TEXT NOT NULL DEFAULT ''")

    existing = conn.execute("SELECT COUNT(*) AS c FROM email_templates").fetchone()["c"]
    if existing == 0:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO email_templates (name, body, is_default, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
            ("Standard Invite", DEFAULT_TEMPLATE_BODY, now, now)
        )

    subject_existing = conn.execute("SELECT COUNT(*) AS c FROM subject_line_templates").fetchone()["c"]
    if subject_existing == 0:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO subject_line_templates (name, body, is_default, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
            ("Standard Subject", DEFAULT_SUBJECT_TEMPLATE, now, now)
        )
    conn.commit()
    conn.close()


init_db()


def load_gender_map():
    conn = get_db()
    rows = conn.execute("SELECT email, gender FROM faculty_gender").fetchall()
    conn.close()
    return {r["email"].strip().lower(): r["gender"] for r in rows}


def normalize_gender(value):
    v = str(value).strip().lower()
    if v in ("m", "male"):
        return "M"
    if v in ("f", "female"):
        return "F"
    return None


def safe_str(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def get_template(template_id=None):
    conn = get_db()
    row = None
    if template_id:
        row = conn.execute("SELECT * FROM email_templates WHERE id = ?", (template_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM email_templates WHERE is_default = 1").fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM email_templates ORDER BY id LIMIT 1").fetchone()
    conn.close()
    return row


def get_subject_template(template_id=None):
    conn = get_db()
    row = None
    if template_id:
        row = conn.execute("SELECT * FROM subject_line_templates WHERE id = ?", (template_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM subject_line_templates WHERE is_default = 1").fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM subject_line_templates ORDER BY id LIMIT 1").fetchone()
    conn.close()
    return row


def find_column(df, keyword):
    keyword_lower = keyword.lower().strip()
    for col in df.columns:
        if keyword_lower in str(col).lower():
            return col

    fallbacks = {
        "metting id": ["meeting", "metting", "id", "meeting id", "metting id"],
        "teaching faculty": ["faculty", "teacher", "instructor", "lecturer", "teaching faculty"],
        "session": ["time", "session", "session time", "hour", "timing"],
        "pass": ["passcode", "password", "pass", "pwd"]
    }

    if keyword_lower in fallbacks:
        for alternative in fallbacks[keyword_lower]:
            for col in df.columns:
                if alternative in str(col).lower():
                    return col

    raise Exception(f"Column matching keyword '{keyword}' not found. Available columns in Excel: {', '.join(df.columns)}")


def parse_time_range(txt):
    parts = re.split(r"\s+to\s+", str(txt).strip().lower())
    start = datetime.strptime(parts[0].strip(), "%I:%M%p").time()
    end = datetime.strptime(parts[1].strip(), "%I:%M%p").time()
    return start, end


@tango_bp.route("/")
@tango_access_required
def tango_home():
    conn = get_db()
    templates = conn.execute("SELECT * FROM email_templates ORDER BY is_default DESC, name ASC").fetchall()
    subject_templates = conn.execute("SELECT * FROM subject_line_templates ORDER BY is_default DESC, name ASC").fetchall()
    conn.close()
    return render_template("index.html", templates=templates, subject_templates=subject_templates)


@tango_bp.route("/download-template")
@tango_access_required
def download_template():
    import io
    output = io.BytesIO()
    df_template = pd.DataFrame({
        "Programme": ["IIMI_PGP_01", "IIMI_PGP_02"],
        "Date": ["15-Jun-2026", "16-Jun-2026"],
        "Day": ["Monday", "Tuesday"],
        "Session Time": ["10:00AM to 11:30AM", "02:00PM to 03:30PM"],
        "Topic": ["AI Foundations", "Advanced Neural Networks"],
        "Teaching Faculty": ["Dr. Alan Turing", "Dr. Grace Hopper"],
        "Email": ["turing@example.com", "hopper@example.com"],
        "Zoom Link": ["https://zoom.us/j/123456789", "https://zoom.us/j/987654321"],
        "Meeting ID": ["123 456 789", "987 654 321"],
        "Passcode": ["turing123", "hopper123"]
    })

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_template.to_excel(writer, index=False, sheet_name="Template")

    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name="tango_faculty_invite_template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@tango_bp.route("/emails")
@tango_access_required
def emails_list():
    search = request.args.get("search", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = 20
    page = max(1, page)

    conn = get_db()
    if search:
        like = f"%{search.lower()}%"
        total_items = conn.execute(
            "SELECT COUNT(*) AS c FROM faculty_gender WHERE lower(name) LIKE ? OR lower(email) LIKE ?",
            (like, like)
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM faculty_gender WHERE lower(name) LIKE ? OR lower(email) LIKE ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (like, like, per_page, (page - 1) * per_page)
        ).fetchall()
    else:
        total_items = conn.execute("SELECT COUNT(*) AS c FROM faculty_gender").fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM faculty_gender ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page)
        ).fetchall()
    conn.close()

    total_pages = max(1, (total_items + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages

    return render_template(
        "emails.html",
        emails=rows,
        search=search,
        page=page,
        total_pages=total_pages,
        total_items=total_items,
        per_page=per_page
    )


@tango_bp.route("/emails/suggest")
@tango_access_required
def emails_suggest():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    like = f"%{q.lower()}%"
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, email, gender FROM faculty_gender WHERE lower(name) LIKE ? OR lower(email) LIKE ? ORDER BY created_at DESC LIMIT 8",
        (like, like)
    ).fetchall()
    conn.close()

    return jsonify([{"id": r["id"], "name": r["name"], "email": r["email"], "gender": r["gender"]} for r in rows])


@tango_bp.route("/emails/add", methods=["POST"])
@tango_access_required
def emails_add():
    raw = request.form.get("emails", "")
    gender = request.form.get("gender", "").strip().upper()
    if gender not in ("M", "F"):
        flash("Please select Male or Female.", "error")
        return redirect(url_for("tango.emails_list"))

    candidates = [e.strip() for e in re.split(r"[\n,;]+", raw) if e.strip()]
    if not candidates:
        flash("Please enter at least one email address.", "error")
        return redirect(url_for("tango.emails_list"))

    added, invalid, skipped = 0, [], []
    conn = get_db()
    now = datetime.now().isoformat()
    for email in candidates:
        if not EMAIL_REGEX.match(email):
            invalid.append(email)
            continue
        existing = conn.execute(
            "SELECT 1 FROM faculty_gender WHERE lower(email) = lower(?) LIMIT 1",
            (email,)
        ).fetchone()
        if existing:
            skipped.append(email)
            continue
        conn.execute(
            "INSERT INTO faculty_gender (email, gender, created_at) VALUES (?, ?, ?)",
            (email, gender, now)
        )
        added += 1
    conn.commit()
    conn.close()

    label = "Male (Sir)" if gender == "M" else "Female (Ma'am)"
    if added:
        flash(f"Saved {added} email(s) as {label}.", "success")
    if skipped:
        flash(f"Skipped {len(skipped)} already registered email(s).", "warning")
    if invalid:
        plural = "y" if len(invalid) == 1 else "ies"
        flash(f"Skipped {len(invalid)} invalid entr{plural}: {', '.join(invalid[:5])}", "error")
    return redirect(url_for("tango.emails_list"))


@tango_bp.route("/emails/add-single", methods=["POST"])
@tango_access_required
def emails_add_single():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    gender = request.form.get("gender", "").strip().upper()

    if not EMAIL_REGEX.match(email):
        flash("Please enter a valid email address.", "error")
        return redirect(url_for("tango.emails_list"))
    if gender not in ("M", "F"):
        flash("Please select Male or Female.", "error")
        return redirect(url_for("tango.emails_list"))

    conn = get_db()
    now = datetime.now().isoformat()
    existing = conn.execute(
        "SELECT 1 FROM faculty_gender WHERE lower(email) = lower(?) LIMIT 1",
        (email,)
    ).fetchone()
    if existing:
        conn.close()
        flash(f"'{email}' is already in the directory.", "warning")
        return redirect(url_for("tango.emails_list"))

    conn.execute(
        "INSERT INTO faculty_gender (email, name, gender, created_at) VALUES (?, ?, ?, ?)",
        (email, name, gender, now)
    )
    conn.commit()
    conn.close()
    flash(f"Saved '{email}'.", "success")
    return redirect(url_for("tango.emails_list"))


@tango_bp.route("/emails/edit/<int:email_id>", methods=["POST"])
@tango_access_required
def emails_edit(email_id):
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    gender = request.form.get("gender", "").strip().upper()

    if not EMAIL_REGEX.match(email):
        flash("Please enter a valid email address.", "error")
        return redirect(url_for("tango.emails_list"))
    if gender not in ("M", "F"):
        flash("Please select Male or Female.", "error")
        return redirect(url_for("tango.emails_list"))

    conn = get_db()
    try:
        conn.execute(
            "UPDATE faculty_gender SET name = ?, email = ?, gender = ? WHERE id = ?",
            (name, email, gender, email_id)
        )
        conn.commit()
        flash("Email entry updated.", "success")
    except sqlite3.IntegrityError:
        flash(f"Another entry already uses the email '{email}'.", "error")
    conn.close()
    return redirect(url_for("tango.emails_list"))


@tango_bp.route("/emails/import", methods=["POST"])
@tango_access_required
def emails_import():
    if "file" not in request.files or not request.files["file"].filename:
        flash("Please choose a CSV or Excel file to import.", "error")
        return redirect(url_for("tango.emails_list"))

    file = request.files["file"]
    filename = file.filename.lower()
    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(file, dtype=str)
        else:
            df = pd.read_excel(file, dtype=str, engine="openpyxl")
    except Exception as e:
        flash(f"Failed to read file: {e}", "error")
        return redirect(url_for("tango.emails_list"))

    try:
        EMAIL_COL = find_column(df, "Email")
        GENDER_COL = find_column(df, "Gender")
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("tango.emails_list"))

    try:
        NAME_COL = find_column(df, "Name")
    except Exception:
        NAME_COL = None

    added, skipped = 0, []
    conn = get_db()
    now = datetime.now().isoformat()
    for index, row in df.iterrows():
        row_num = index + 2
        email = safe_str(row[EMAIL_COL])
        gender = normalize_gender(row[GENDER_COL])
        name = safe_str(row[NAME_COL]) if NAME_COL else ""

        if not EMAIL_REGEX.match(email) or gender is None:
            skipped.append(f"Row {row_num}: {email or '(blank)'}")
            continue

        existing = conn.execute(
            "SELECT 1 FROM faculty_gender WHERE lower(email) = lower(?) LIMIT 1",
            (email,)
        ).fetchone()
        if existing:
            skipped.append(f"Row {row_num}: {email}")
            continue

        conn.execute(
            "INSERT INTO faculty_gender (email, name, gender, created_at) VALUES (?, ?, ?, ?)",
            (email, name, gender, now)
        )
        added += 1
    conn.commit()
    conn.close()

    if added:
        flash(f"Imported {added} email(s) from file.", "success")
    if skipped:
        flash(f"Skipped {len(skipped)} row(s) because they were already registered or had invalid email/gender: {', '.join(skipped[:5])}", "warning")
    return redirect(url_for("tango.emails_list"))


@tango_bp.route("/emails/download-template")
@tango_access_required
def emails_download_template():
    import io
    output = io.BytesIO()
    df_template = pd.DataFrame({
        "Name": ["Dr. Alan Turing", "Dr. Grace Hopper"],
        "Email": ["turing@example.com", "hopper@example.com"],
        "Gender": ["Male", "Female"]
    })
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_template.to_excel(writer, index=False, sheet_name="Emails")
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name="email_directory_template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@tango_bp.route("/emails/delete/<int:email_id>", methods=["POST"])
@tango_access_required
def emails_delete(email_id):
    conn = get_db()
    conn.execute("DELETE FROM faculty_gender WHERE id = ?", (email_id,))
    conn.commit()
    conn.close()
    flash("Email removed.", "success")
    return redirect(url_for("tango.emails_list"))


@tango_bp.route("/templates")
@tango_access_required
def templates_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM email_templates ORDER BY is_default DESC, name ASC").fetchall()
    conn.close()
    return render_template("templates.html", email_templates=rows)


@tango_bp.route("/subject-lines")
@tango_access_required
def subject_lines_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM subject_line_templates ORDER BY is_default DESC, name ASC").fetchall()
    conn.close()
    return render_template("subject_templates.html", subject_templates=rows)


@tango_bp.route("/subject-lines/add", methods=["POST"])
@tango_access_required
def subject_lines_add():
    name = request.form.get("name", "").strip()
    body = request.form.get("body", "")
    if not name or not body.strip():
        flash("Subject line template name and body are required.", "error")
        return redirect(url_for("tango.subject_lines_list"))

    now = datetime.now().isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO subject_line_templates (name, body, is_default, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
            (name, body, now, now)
        )
        conn.commit()
        flash(f"Subject line template '{name}' created.", "success")
    except sqlite3.IntegrityError:
        flash(f"A subject line template named '{name}' already exists.", "error")
    conn.close()
    return redirect(url_for("tango.subject_lines_list"))


@tango_bp.route("/subject-lines/edit/<int:template_id>", methods=["POST"])
@tango_access_required
def subject_lines_edit(template_id):
    name = request.form.get("name", "").strip()
    body = request.form.get("body", "")
    if not name or not body.strip():
        flash("Subject line template name and body are required.", "error")
        return redirect(url_for("tango.subject_lines_list"))

    now = datetime.now().isoformat()
    conn = get_db()
    try:
        conn.execute(
            "UPDATE subject_line_templates SET name = ?, body = ?, updated_at = ? WHERE id = ?",
            (name, body, now, template_id)
        )
        conn.commit()
        flash(f"Subject line template '{name}' updated.", "success")
    except sqlite3.IntegrityError:
        flash(f"A subject line template named '{name}' already exists.", "error")
    conn.close()
    return redirect(url_for("tango.subject_lines_list"))


@tango_bp.route("/subject-lines/delete/<int:template_id>", methods=["POST"])
@tango_access_required
def subject_lines_delete(template_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM subject_line_templates WHERE id = ?", (template_id,)).fetchone()
    total = conn.execute("SELECT COUNT(*) AS c FROM subject_line_templates").fetchone()["c"]
    if row is None:
        conn.close()
        flash("Subject line template not found.", "error")
        return redirect(url_for("tango.subject_lines_list"))
    if row["is_default"]:
        conn.close()
        flash("Cannot delete the default subject line template. Set another template as default first.", "error")
        return redirect(url_for("tango.subject_lines_list"))
    if total <= 1:
        conn.close()
        flash("At least one subject line template must remain.", "error")
        return redirect(url_for("tango.subject_lines_list"))
    conn.execute("DELETE FROM subject_line_templates WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()
    flash(f"Subject line template '{row['name']}' deleted.", "success")
    return redirect(url_for("tango.subject_lines_list"))


@tango_bp.route("/subject-lines/set-default/<int:template_id>", methods=["POST"])
@tango_access_required
def subject_lines_set_default(template_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM subject_line_templates WHERE id = ?", (template_id,)).fetchone()
    if row is None:
        conn.close()
        flash("Subject line template not found.", "error")
        return redirect(url_for("tango.subject_lines_list"))
    conn.execute("UPDATE subject_line_templates SET is_default = 0")
    conn.execute("UPDATE subject_line_templates SET is_default = 1 WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()
    flash(f"'{row['name']}' is now the default subject line template.", "success")
    return redirect(url_for("tango.subject_lines_list"))


@tango_bp.route("/templates/add", methods=["POST"])
@tango_access_required
def templates_add():
    name = request.form.get("name", "").strip()
    body = request.form.get("body", "")
    if not name or not body.strip():
        flash("Template name and body are required.", "error")
        return redirect(url_for("tango.templates_list"))

    now = datetime.now().isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO email_templates (name, body, is_default, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
            (name, body, now, now)
        )
        conn.commit()
        flash(f"Template '{name}' created.", "success")
    except sqlite3.IntegrityError:
        flash(f"A template named '{name}' already exists.", "error")
    conn.close()
    return redirect(url_for("tango.templates_list"))


@tango_bp.route("/templates/edit/<int:template_id>", methods=["POST"])
@tango_access_required
def templates_edit(template_id):
    name = request.form.get("name", "").strip()
    body = request.form.get("body", "")
    if not name or not body.strip():
        flash("Template name and body are required.", "error")
        return redirect(url_for("tango.templates_list"))

    now = datetime.now().isoformat()
    conn = get_db()
    try:
        conn.execute(
            "UPDATE email_templates SET name = ?, body = ?, updated_at = ? WHERE id = ?",
            (name, body, now, template_id)
        )
        conn.commit()
        flash(f"Template '{name}' updated.", "success")
    except sqlite3.IntegrityError:
        flash(f"A template named '{name}' already exists.", "error")
    conn.close()
    return redirect(url_for("tango.templates_list"))


@tango_bp.route("/templates/delete/<int:template_id>", methods=["POST"])
@tango_access_required
def templates_delete(template_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM email_templates WHERE id = ?", (template_id,)).fetchone()
    total = conn.execute("SELECT COUNT(*) AS c FROM email_templates").fetchone()["c"]
    if row is None:
        conn.close()
        flash("Template not found.", "error")
        return redirect(url_for("tango.templates_list"))
    if row["is_default"]:
        conn.close()
        flash("Cannot delete the default template. Set another template as default first.", "error")
        return redirect(url_for("tango.templates_list"))
    if total <= 1:
        conn.close()
        flash("At least one template must remain.", "error")
        return redirect(url_for("tango.templates_list"))
    conn.execute("DELETE FROM email_templates WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()
    flash(f"Template '{row['name']}' deleted.", "success")
    return redirect(url_for("tango.templates_list"))


@tango_bp.route("/templates/set-default/<int:template_id>", methods=["POST"])
@tango_access_required
def templates_set_default(template_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM email_templates WHERE id = ?", (template_id,)).fetchone()
    if row is None:
        conn.close()
        flash("Template not found.", "error")
        return redirect(url_for("tango.templates_list"))
    conn.execute("UPDATE email_templates SET is_default = 0")
    conn.execute("UPDATE email_templates SET is_default = 1 WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()
    flash(f"'{row['name']}' is now the default template.", "success")
    return redirect(url_for("tango.templates_list"))


@tango_bp.route("/history")
@tango_access_required
def history():
    conn = get_db()
    rows = conn.execute("SELECT * FROM generation_history ORDER BY generated_at DESC").fetchall()
    summary = conn.execute(
        "SELECT COUNT(*) AS batches, COALESCE(SUM(success_count), 0) AS total_ics, MAX(generated_at) AS last_run FROM generation_history"
    ).fetchone()
    conn.close()
    return render_template("history.html", history=rows, summary=summary)


@tango_bp.route("/generate", methods=["POST"])
@tango_access_required
def generate():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    excel = request.files["file"]
    if not excel.filename:
        return jsonify({"success": False, "error": "No selected file"}), 400

    try:
        df = pd.read_excel(excel, dtype=str, engine="openpyxl")
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to read Excel file: {str(e)}"}), 400

    try:
        PROGRAMME = find_column(df, "Programme")
        DATE = find_column(df, "Date")
        DAY = find_column(df, "Day")
        TIME = find_column(df, "Session")
        TOPIC = find_column(df, "Topic")
        FACULTY = find_column(df, "Teaching Faculty")
        EMAIL = find_column(df, "Email")
        ZOOM = find_column(df, "Zoom")
        MID = find_column(df, "Metting ID")
        PASS = find_column(df, "Pass")
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

    template_id = request.form.get("template_id", type=int)
    template_row = get_template(template_id)
    template_body = template_row["body"]
    template_name = template_row["name"]
    subject_template_id = request.form.get("subject_template_id", type=int)
    subject_template_row = get_subject_template(subject_template_id)
    subject_template_body = subject_template_row["body"]
    subject_template_name = subject_template_row["name"]
    gender_map = load_gender_map()

    prefix = request.form.get("prefix", "IIMI").strip() or "IIMI"
    location = request.form.get("location", "Sports Complex first floor").strip() or "Sports Complex first floor"

    tz = pytz.timezone("Asia/Kolkata")

    tmp = tempfile.mkdtemp()
    zip_path = os.path.join(tmp, "Faculty_Invites.zip")

    success_count = 0
    errors = []

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for index, row in df.iterrows():
            row_num = index + 2
            try:
                date_val = pd.to_datetime(row[DATE])
                start_t, end_t = parse_time_range(row[TIME])

                start_dt = tz.localize(datetime.combine(date_val.date(), start_t))
                end_dt = tz.localize(datetime.combine(date_val.date(), end_t))

                programme = str(row[PROGRAMME]).strip()
                topic = str(row[TOPIC]).strip()
                faculty = str(row[FACULTY]).strip()
                email = str(row[EMAIL]).strip()

                subject = subject_template_body.format(
                    date=date_val.strftime('%d-%b-%Y'),
                    prefix=prefix,
                    programme=programme,
                    topic=topic,
                    day=str(row[DAY]).strip(),
                    faculty_name=faculty
                )

                salutation = "Ma'am" if gender_map.get(email.lower()) == "F" else "Sir"

                description = template_body.format(
                    date=date_val.strftime("%d-%b-%Y"),
                    programme=programme,
                    day=str(row[DAY]).strip(),
                    topic=topic,
                    session_time=str(row[TIME]).strip(),
                    zoom_link=str(row[ZOOM]).strip(),
                    meeting_id=str(row[MID]).strip(),
                    passcode=str(row[PASS]).strip(),
                    location=location,
                    salutation=salutation,
                    faculty_name=faculty
                )

                cal = Calendar()
                event = Event()
                event.add("summary", subject)
                event.add("dtstart", start_dt)
                event.add("dtend", end_dt)
                event.add("description", description)
                event.add("location", location)

                attendee = vCalAddress(f"MAILTO:{email}")
                attendee.params["cn"] = vText(faculty)
                attendee.params["ROLE"] = vText("REQ-PARTICIPANT")
                event.add("attendee", attendee)

                cal.add_component(event)

                fname = re.sub(r'[\\/*?:"<>|]', "_", subject) + ".ics"
                zf.writestr(fname, cal.to_ical())
                success_count += 1
            except Exception as e:
                err_msg = f"Row {row_num}: {str(e)}"
                print(err_msg)
                errors.append(err_msg)

    conn = get_db()
    conn.execute(
        """INSERT INTO generation_history
           (generated_at, template_name, prefix, location, total_rows, success_count, failed_count)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(tz).isoformat(), template_name, prefix, location, len(df), success_count, len(errors))
    )
    conn.commit()
    conn.close()

    if success_count == 0:
        error_details = "; ".join(errors) if errors else "No valid rows found in Excel sheet."
        return jsonify({"success": False, "error": f"Failed to generate invites: {error_details}"}), 400

    response = send_file(zip_path, as_attachment=True)
    response.headers["X-Processed-Count"] = str(success_count)
    response.headers["X-Failed-Count"] = str(len(errors))
    if errors:
        clean_errors = [e.replace("\n", " ") for e in errors[:5]]
        response.headers["X-Errors"] = " | ".join(clean_errors)
    return response
