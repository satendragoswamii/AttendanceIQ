# 🎓 AttendanceIQ – Smart Zoom Attendance Calculator

<div align="center">

![AttendanceIQ](https://img.shields.io/badge/AttendanceIQ-Zoom_Attendance_Calculator-6366f1?style=for-the-badge)

[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-2.3.2-000000?style=flat&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Calculate accurate attendance from Zoom logs – session-wise, with custom rules**

</div>

---

## 🎯 What is AttendanceIQ?

A web tool for **schools, colleges, universities, and organizations** running online Zoom classes that need accurate attendance.

**The problem:** during a Zoom class, learners join late, leave early, disconnect and rejoin. Working out by hand who attended "enough" time is slow and error-prone.

**The solution:** upload your Zoom participant report → configure sessions and rules → download an attendance report with **Present/Absent** status and shortfall details.

---

## 💡 Example

A **120-minute class**:
- Session 1: 50 min (10:00 – 10:50)
- Break: 20 min
- Session 2: 50 min (11:10 – 12:00)

**Rule:** a learner must attend at least **45 minutes per session** to be marked Present.

| Learner | Session 1 | Session 2 | Shortfall |
|---------|-----------|-----------|-----------|
| John | P | P | - |
| Jane | A | P | Session 1: -6 min |

---

## ✨ Features

- **Multi-session support** – multiple sessions with breaks
- **Custom attendance rules** – minimum required minutes per session
- **Shortfall reports** – how many minutes absent learners missed
- **Smart name matching** – fuzzy matching of Zoom display names to your roster, with batch learning
- **Batch processing** – process several files at once
- **Excel export** – formatted reports with configurable output columns
- **Summary tool** – consolidated summaries across processed files
- **User management** – roles (superadmin, subsuperadmin, admin, user), account expiry, forced password change, activity and login logs
- **Tango** – faculty session-invite (`.ics`) generator, built in as a blueprint

---

## 🧱 Tech stack

- Python 3.9+ / Flask 2.3 / Werkzeug
- SQLite (created automatically on first run)
- pandas, openpyxl – Excel/CSV processing
- rapidfuzz – fuzzy name matching
- icalendar, pytz – calendar invites
- Bootstrap + vanilla JS front end (Jinja templates)

---

## 🚀 Getting started

### 1. Install

```bash
git clone https://github.com/satendragoswamii/AttendanceIQ.git
cd AttendanceIQ
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy the example file and fill in your own values:

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste into FLASK_SECRET_KEY
```

| Variable | Required | Purpose |
|---|---|---|
| `FLASK_SECRET_KEY` | Yes (production) | Signs session cookies. If unset, a random key is used and everyone is logged out on restart. |
| `FLASK_ENV` | No | `development` for local http; `production` makes cookies HTTPS-only. |
| `FLASK_DEBUG` | No | `true` only for local development. |
| `SUPERADMIN_RESET_KEY` | No | Enables the hidden superadmin password-recovery page. Leave blank to disable it. |

The app reads variables from the environment; it does not load `.env` itself. Load it into your shell before starting:

```bash
set -a; source .env; set +a
```

`.env` is git-ignored — never commit real secrets.

### 3. Run

```bash
python3 run_app.py
```

Open `http://localhost:5000`.

### 4. Database and first login

No database ships with the repository. On first start the app creates `attendancify.db` (and `tango.db`) with the full schema, and creates a superadmin account with a **random password printed once in the server output**:

```
Created the superadmin account.
  email    : superadmin@attendancify.com
  password : <random>
```

Sign in with it; you will be asked to set your own password immediately.

### Docker

```bash
docker compose up --build
```

The app is published on `http://localhost:5051`. Set `FLASK_SECRET_KEY` in `docker-compose.yml` or your environment before using it for real.

### Production

See [DEPLOYMENT.md](DEPLOYMENT.md) for the PythonAnywhere deployment workflow. Use a WSGI server (`wsgi.py`), set `FLASK_ENV=production`, and always set a fixed `FLASK_SECRET_KEY`.

---

## 📖 How to use

1. In Zoom, download the participant report for the meeting (CSV).
2. Sign in to AttendanceIQ and upload the file.
3. Set the session times and the minimum attendance duration.
4. Review the matching preview and download your attendance report.

---

## 🧪 Tests

```bash
python3 -m unittest discover tests
```

Tests use temporary databases and never touch `attendancify.db`.

---

## 🔒 Data & privacy

Attendance files, databases, uploaded photos/logos, logs and `.env` are excluded by `.gitignore`. Keep it that way — the database contains learner data and account credentials.

---

## 💬 Feedback & contact

This is a **free passion project** — feedback, suggestions and questions are welcome.

📧 **Email:** [satendragoshwamii@gmail.com](mailto:satendragoshwamii@gmail.com)

[![Instagram](https://img.shields.io/badge/Instagram-@satendragoswamii-E4405F?style=flat&logo=instagram&logoColor=white)](https://www.instagram.com/satendragoswamii/)
[![GitHub](https://img.shields.io/badge/GitHub-satendragoswamii-181717?style=flat&logo=github&logoColor=white)](https://github.com/satendragoswamii)

---

## 📄 License

MIT License – free for personal and commercial use.
