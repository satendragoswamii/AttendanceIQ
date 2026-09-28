# Attendancify – Master Project Overview (Keep for Future Reference)

This document summarizes the essential information needed to run, configure, deploy, and maintain the project. It replaces the many smaller docs we plan to delete for space.

---

## 1) Stack & Runtime
- Python 3.x
- Flask 2.3.2
- SQLite (file: `attendancify.db`)
- Key libs: `pandas>=2.0.0`, `openpyxl==3.1.2`, `rapidfuzz==3.4.0`
- Optional UI lib: `ttkbootstrap==1.10.1` (listed in requirements)

### Install
```bash
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Run (dev)
```bash
python comprehensive_app.py
# open http://localhost:5000
```

---

## 2) Core Files
- `comprehensive_app.py`  – main Flask app (routes, logic, downloads)
- `database.py`           – DB schema, helpers, connection management
- `attendance_processing.py` – attendance processing helpers
- `static/`               – CSS/JS/assets (theme, matcher JS, particles)
- `templates/`            – Jinja2 templates (all UI)
- `wsgi.py`               – PythonAnywhere entrypoint (update path/secret)
- `requirements.txt`      – dependencies
- `attendancify.db`       – SQLite database (prod data; do NOT commit)

---

## 3) Roles & Access
- `superadmin` – full access; manages users, settings, hidden users, formats.
- `admin`      – manage users they create (and other admin-enabled areas).
- `subsuperadmin` – extended permissions but controlled by superadmin.
- `user`       – normal usage.

---

## 4) Settings & Customization (superadmin)
Go to: **Dashboard → Settings**

### Tabs
- **App Names**: Rename the 3 tools.
- **Output Formats (Per App)**: Configure per-app file format, headers, timestamps, user info, custom columns (JSON mapping).
- **Advanced: Role Formats**: Optional per-role formats.
- **Advanced: System Settings**: Lower-level settings (use cautiously).

### App Names (stored in DB)
- `app1_name` → Zoom Attendance Analyzer (Attendance Generator)
- `app2_name` → Intelligent Record Matcher (Smart Matcher)
- `app3_name` → Session Report Generator (Final Excel)

### Output Format Settings (per app)
- File format: xlsx/csv/xls
- Date/time format
- Header title/subtitle
- Include header / timestamp / user info
- Custom columns (JSON):
  ```json
  {
    "User Email": "Email Address",
    "Name (First Name)": "Student Name"
  }
  ```

### Column References (defaults)
- App1 (Zoom Attendance Analyzer): Name (First Name), User Name (Display Name), User Email, Join & Leave Time, Duration (Minutes), Session columns.
- App2 (Intelligent Record Matcher): Email, Participant Name, Raw Name, Session columns.
- App3 (Session Report Generator): email_id, attendance(absent/present/leave).

---

## 5) File Naming & Downloads
- Outputs now use original filename with app prefix:
  - App1: `AppName_OriginalFile.xlsx`
  - App2: `AppName_OriginalFile.xlsx`
  - App3: `AppName_OriginalFile_Session.xlsx`
- Downloads use `send_file(..., as_attachment=True, download_name=...)`.  
  If browser prompts “Save/rename”, disable “Ask where to save each file” in browser download settings.

---

## 6) Database & Connections
- SQLite file: `attendancify.db`
- Connection helper uses absolute path, timeout, `check_same_thread=False`, WAL mode.
- Always closes connections/cursors to avoid “too many open files”.
- Don’t commit the DB; `.gitignore` excludes `*.db`.

### Initialize DB (if needed)
```bash
python -c "from database import init_db; init_db(); print('DB ready')"
```

---

## 7) User Management
- Hide/Unhide users (activity hidden but login allowed); superadmin safety checks for hide/unhide.
- Delete user cleans up related records.
- Can create admin/subsuperadmin (superadmin only).
- Optional first/last name fields for superadmin-created users.

---

## 8) Deployment (PythonAnywhere quick notes)
- Update `wsgi.py` path to your PA username/project path.
- Set env vars in PA Web tab: `FLASK_ENV=production`, `FLASK_DEBUG=False`, `FLASK_SECRET_KEY=...`.
- `init_db()` is called on first load in `wsgi.py`.
- Ensure `requirements.txt` is installed in the PA virtualenv.

---

## 9) Removed/Non-Essential Files
Already removed to save space:
- `upload_to_github.bat`, `upload_to_github.sh` (helper scripts)

Planned to remove (after keeping this overview):
- Old feature docs, quick starts, deployment/how-to markdowns (all the per-feature docs).

---

## 10) Browser Notes
- To avoid download prompts: disable “Ask where to save each file” in browser downloads settings.
- Hard refresh after template/CSS changes: `Cmd+Shift+R` (Mac) / `Ctrl+F5` (Win).

---

## 11) Common Commands
```bash
# Run dev
python comprehensive_app.py

# Lint recently edited file
python -m py_compile comprehensive_app.py

# Init DB (if missing)
python -c "from database import init_db; init_db()"
```

---

## 12) Support Checklist (when things break)
- DB errors / open files: ensure `init_db` ran; WAL enabled; restart app.
- Download prompts: adjust browser download settings.
- Column names not applied: JSON invalid or keys don’t match exact column names; reprocess after saving.
- App names not updated: ensure restart + hard refresh; check `app_settings` table.

---

Keep this file. It consolidates everything needed to operate and redeploy the app. ***
