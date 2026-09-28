# Attendancify — PythonAnywhere Deployment Runbook

Replace the application code on the live server without touching databases,
uploaded files, or installed packages. About five minutes end to end.

| | |
|---|---|
| Server user | `AttendanceIQ` |
| App directory | `/home/AttendanceIQ/ZoomAttendancify` |
| Zip staging | `/home/AttendanceIQ/zips` |
| Backups | `/home/AttendanceIQ/backups` |
| Reload trigger | `/var/www/AttendanceIQ_pythonanywhere_com_wsgi.py` |

Tooling: `make_deploy_zip.sh` (runs locally), `deploy_pythonanywhere.sh` (runs on the server).

---

## 0. Quick reference

**On your Mac**, in the project folder:

```bash
./make_deploy_zip.sh
```

**Upload** `attendancify_deploy_<stamp>.zip` into `~/zips/` via the Files tab.
Delete any older zip already there.

**On the PythonAnywhere Bash console:**

```bash
ls ~/zips                                  # exactly one zip
bash ~/deploy_pythonanywhere.sh --dry-run
bash ~/deploy_pythonanywhere.sh
touch /var/www/AttendanceIQ_pythonanywhere_com_wsgi.py
```

The script never deletes a database and stops on the first error. A failed run
destroys nothing — read the message, fix it, run again.

---

## 1. What the deploy changes

| Path | Fate | Why |
|---|---|---|
| `attendancify.db` | **kept** | All Attendancify / AttendanceIQ records |
| `tango.db` | **kept** | Tango module data |
| `*.sqlite`, `*.db-wal`, `*.db-shm` | **kept** | Any database added later is covered automatically |
| `.env` | **kept** | Secrets never leave the server |
| `uploads/`, `data/` | **kept** | User files and generated reports |
| `static/branding/` | **kept** | Logos uploaded through Settings |
| `static/landing/` | **kept** | Landing page photos uploaded through the app |
| `.venv/`, `venv/`, `env/` | **kept** | Installed packages stay put |
| `.git/`, `backups/`, `logs/` | **kept** | History and diagnostics |
| All `*.py` | replaced | The point of the deploy |
| `templates/`, `tango_templates/` | replaced | Wholesale, so deleted pages actually disappear |
| `static/` (css, js, images) | replaced | Branding and landing uploads merged back after |
| Files no longer in the repo | deleted | Old patch scripts and dead templates cleared out |

`static/branding/` and `static/landing/` sit inside a folder that gets replaced,
so the script copies them out before the wipe and merges them back after
extraction. Where a filename exists in both, **the server's live file wins** — an
uploaded logo is never overwritten by the copy in the zip.

Two further protections:

- A stale `attendancify.db` or `.env` inside the zip is stripped out before
  anything is copied, so a zip can never overwrite live data.
- The script refuses to run if launched from inside `~/ZoomAttendancify`, which
  would delete the script mid-execution.

---

## 2. Build the zip (local)

```bash
./make_deploy_zip.sh
```

```
Built attendancify_deploy_20260810_123404.zip (670K)
Sanity check - these must all be absent:
  ok  attendancify.db excluded
  ok  tango.db excluded
  ok  .env excluded
```

Those three `ok` lines are the safety check. If any becomes a warning, **do not
upload** — a database has been packaged by mistake.

The zip is deliberately flat: files sit at its root, so they land directly in
`~/ZoomAttendancify/` rather than a nested folder.

Excluded on purpose: databases, `.env`, `uploads/`, `data/`, the virtualenv, git
history, `__pycache__`, `*.patch`, the one-off codemod scripts
(`apply_fix.py`, `patch_attendanceiq.py`, `patch_tango_access.py`,
`fix_dashboard_tz.py`, `check_server.py`), and the Docker setup.

---

## 3. Deploy (server)

**1 — Clear out the old zip.** Everything in `~/zips` gets extracted, in filename
order. Leaving last month's zip there means deploying it too.

```bash
ls ~/zips
rm ~/zips/*.zip        # if anything is listed
```

**2 — Upload the new zip.** Files tab → open `zips/` → *Upload a file*. It is
binary, so it must go through the Files tab; it cannot be pasted into a console.

**3 — Confirm the script is present.** It lives in the home directory, *outside*
the app folder. Re-upload it only when it has actually changed.

```bash
ls -la ~/deploy_pythonanywhere.sh ~/zips/
```

**4 — Dry run.** Prints every action, changes nothing.

```bash
bash ~/deploy_pythonanywhere.sh --dry-run
```

Confirm both databases and `uploads` say `keeping`. If a database says
`deleting`, stop and investigate.

**5 — Run it.**

```bash
bash ~/deploy_pythonanywhere.sh
```

Ends with `==> Done.` and a count of databases present. Anything else means it
stopped early — see Troubleshooting.

**6 — Reload.** New code is not live until the worker restarts.

```bash
touch /var/www/AttendanceIQ_pythonanywhere_com_wsgi.py
```

The green **Reload** button on the Web tab does the same thing.

**Only if `requirements.txt` changed**, then reload again:

```bash
pip install --user -r ~/ZoomAttendancify/requirements.txt
```

---

## 4. Verify

1. **Databases present** — `ls -la ~/ZoomAttendancify/*.db`, sizes as before.
2. **Site loads and login works** — proves database, sessions and templates are wired up.
3. **Your change is live** — if the page looks old, the reload did not happen.
4. **Uploads intact** — landing photos and header logo still render.

Expected server layout:

```
/home/AttendanceIQ/
├── deploy_pythonanywhere.sh   the script, kept outside the app folder
├── zips/                      staging, empty between deploys
├── backups/                   one folder per deploy
└── ZoomAttendancify/
    ├── comprehensive_app.py   replaced
    ├── templates/  static/    replaced
    ├── attendancify.db        kept
    ├── tango.db               kept
    ├── uploads/  data/        kept
    └── static/branding/       kept, merged over the zip
```

---

## 5. Flags and overrides

| Flag | Effect |
|---|---|
| `--dry-run` | Prints every action, changes nothing. Always safe. |
| `--wipe-all` | Drops all protection — deletes databases, `.env` and `uploads/` too. Backups still taken first. |
| `--help` | Prints the script's header comment. |

Path overrides, useful for a staging copy:

```bash
APP_DIR=~/Staging ZIP_DIR=~/staging_zips bash ~/deploy_pythonanywhere.sh --dry-run
```

> **`--wipe-all` destroys live data.** It exists for rebuilding a server from
> scratch. On a running deployment it deletes every record users have entered.
> There is no prompt — the flag *is* the confirmation.

---

## 6. Backups and rollback

Every run creates `~/backups/attendancify_<timestamp>/` before touching anything:

| Inside the backup | Contents |
|---|---|
| `attendancify.db`, `tango.db` | Copies of the databases as they were |
| `.env` | Only if one existed |
| `_code/` | Every top-level `.py`, including hand edits made on the server |
| `_nested/` | Uploaded logos and landing photos |

**Restore a database:**

```bash
ls ~/backups
cp ~/backups/attendancify_20260810_071218/attendancify.db ~/ZoomAttendancify/
touch /var/www/AttendanceIQ_pythonanywhere_com_wsgi.py
```

**Restore a hand-edited Python file:**

```bash
cp ~/backups/attendancify_20260810_071218/_code/wsgi.py ~/ZoomAttendancify/
```

**Go back to the previous release:** put the older zip back in `~/zips` on its
own and run the script again. Data is unaffected — it was never in the zip.

**Housekeeping:** backups accumulate. Delete old ones every few months;
PythonAnywhere disk quota is not generous.

---

## 7. Troubleshooting

**`bash: /home/AttendanceIQ/deploy_pythonanywhere.sh: No such file or directory`**
The script was never uploaded, or went to the wrong folder. Check with `ls -la ~/`.
Upload it while viewing `/home/AttendanceIQ/`, not from inside `ZoomAttendancify/`.

**`This script is running from inside ~/ZoomAttendancify, which it is about to erase`**
You ran the copy the zip extracted into the app folder. Run `bash ~/deploy_pythonanywhere.sh` instead.

**`No .zip files found in /home/AttendanceIQ/zips`**
The upload landed elsewhere: `mv ~/attendancify_deploy_*.zip ~/zips/`

**Site still shows the old version**
Almost always a missed reload. Run the `touch` command, then hard-refresh to
defeat cached CSS/JS. If it persists, check the Web tab's **Source code** and
**Working directory** both read `/home/AttendanceIQ/ZoomAttendancify`.

**Site returns a 500**
Web tab → **Error log** → read the last traceback. Usual causes:
`ModuleNotFoundError` (new dependency — run the pip install, reload) or
`TemplateNotFound` (a template missing from the zip — check the exclude rules in
`make_deploy_zip.sh`). Nothing is lost while debugging; databases were untouched.

**Everyone logged out on every reload**
Expected until a fixed secret key is set — see Config notes.

**Landing photos or header logo vanished**

```bash
cp -a ~/backups/<stamp>/_nested/static/landing/.  ~/ZoomAttendancify/static/landing/
cp -a ~/backups/<stamp>/_nested/static/branding/. ~/ZoomAttendancify/static/branding/
```

**Two versions deployed at once**
More than one zip was in `~/zips`. All get extracted in filename order, so the
last alphabetically wins on shared files. Empty the folder, deploy one zip.

---

## 8. Config notes for this server

**The app does not read `.env`.** There is no `python-dotenv` in the project, so
an `.env` file in the app folder is inert — configuration comes from real
environment variables. Its absence on the server is normal.

**`FLASK_SECRET_KEY` is unset, so sessions do not survive a reload.** Without it,
`comprehensive_app.py` generates a random key at startup, invalidating every
session each time the worker restarts. Fix once in the PythonAnywhere WSGI
configuration file (Web tab), above the line importing the app:

```python
import os
os.environ['FLASK_SECRET_KEY'] = '<a long random string>'
```

That file is `/var/www/AttendanceIQ_pythonanywhere_com_wsgi.py`, outside the app
folder, so deploys never overwrite it.

**Folders recreated after every deploy**, because the app writes into them and
Flask will not create them itself: `uploads/`, `data/`, `static/branding/`,
`static/landing/`, `static/css/`, `static/js/`, `static/images/`, `templates/`,
`tango_static/`, `tango_templates/`.

**Editing files directly on the server** — anything edited live is overwritten by
the next deploy. Port hotfixes back into the local project the same day. The
`_code/` backup covers top-level Python files only, not templates.

---

*Last revised 10 August 2026.*
