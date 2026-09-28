#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-shot replace-and-install for PythonAnywhere.
#
# What it does, in order:
#   1. Verifies the uploaded zip exists.
#   2. Backs up the current project folder to a timestamped .tar.gz next to it.
#   3. Deletes the old project folder completely.
#   4. Unzips the new version into the same path.
#   5. REMOVES the old virtualenv and builds a FRESH one (Python 3.12), then
#      installs requirements.txt into it.
#   6. Reloads the web app by touching the WSGI file.
#
# Usage on PythonAnywhere (Bash console):
#   1. Upload ZoomAttendancify_deploy.zip to your home directory (~).
#   2. Run:  bash ~/install_on_pythonanywhere.sh
#
# Everything below is safe to re-run. It never deletes your virtualenv or the
# backup it just made.
# ---------------------------------------------------------------------------
set -euo pipefail

# ---- Settings (edit only if your setup differs) ---------------------------
USER_HOME="${HOME}"
PROJECT_DIR="${USER_HOME}/ZoomAttendancify"                 # where the app lives
ZIP_PATH="${USER_HOME}/ZoomAttendancify_deploy.zip"         # the uploaded zip
VENV_NAME="ZoomAttendancify"                                # virtualenv name
VENV_DIR="${USER_HOME}/.virtualenvs/${VENV_NAME}"           # its path on disk
PYTHON_VERSION="3.12"                                       # must match the Web app's Python
# WSGI file PythonAnywhere serves. Adjust <username> if needed.
WSGI_FILE="/var/www/attendanceiq_pythonanywhere_com_wsgi.py"
# ---------------------------------------------------------------------------

ts="$(date +%Y%m%d_%H%M%S)"
echo "==> Deploy started at ${ts}"

# 1. Zip must exist
if [[ ! -f "${ZIP_PATH}" ]]; then
  echo "ERROR: zip not found at ${ZIP_PATH}"
  echo "Upload ZoomAttendancify_deploy.zip to your home directory first."
  exit 1
fi

# Never run from inside the folder we are about to delete.
cd "${USER_HOME}"

# 2. Back up the current app (if present)
if [[ -d "${PROJECT_DIR}" ]]; then
  backup="${USER_HOME}/ZoomAttendancify_backup_${ts}.tar.gz"
  echo "==> Backing up current app to ${backup}"
  tar -czf "${backup}" -C "${USER_HOME}" "$(basename "${PROJECT_DIR}")"
  echo "    Backup size: $(du -h "${backup}" | cut -f1)"
else
  echo "==> No existing app folder at ${PROJECT_DIR} (nothing to back up)"
fi

# 3. Delete the old app
echo "==> Removing old app at ${PROJECT_DIR}"
rm -rf "${PROJECT_DIR}"

# 4. Unzip the new version into place
echo "==> Installing new version"
mkdir -p "${PROJECT_DIR}"
unzip -q -o "${ZIP_PATH}" -d "${PROJECT_DIR}"
echo "    Files installed: $(find "${PROJECT_DIR}" -type f | wc -l | tr -d ' ')"

# 5. Remove the old virtualenv and build a fresh one
PY_BIN="python${PYTHON_VERSION}"
if ! command -v "${PY_BIN}" >/dev/null 2>&1; then
  echo "WARNING: ${PY_BIN} not found; falling back to python3"
  PY_BIN="python3"
fi
echo "==> Using interpreter: $(command -v "${PY_BIN}")  ($("${PY_BIN}" --version 2>&1))"

# Drop the old venv completely so nothing stale is carried over. Try
# virtualenvwrapper's rmvirtualenv first (keeps its bookkeeping tidy), then
# make sure the directory is gone either way.
echo "==> Removing old virtualenv '${VENV_NAME}'"
if [[ -f "${VIRTUALENVWRAPPER_SCRIPT:-/usr/local/bin/virtualenvwrapper.sh}" ]]; then
  # shellcheck disable=SC1090
  source "${VIRTUALENVWRAPPER_SCRIPT:-/usr/local/bin/virtualenvwrapper.sh}" 2>/dev/null || true
  rmvirtualenv "${VENV_NAME}" 2>/dev/null || true
fi
rm -rf "${VENV_DIR}"

echo "==> Creating fresh virtualenv '${VENV_NAME}' (Python ${PYTHON_VERSION})"
if command -v mkvirtualenv >/dev/null 2>&1; then
  mkvirtualenv --python="$(command -v "${PY_BIN}")" "${VENV_NAME}"
else
  # virtualenvwrapper not loaded in this non-interactive shell: plain venv at
  # the same path the Web tab expects.
  mkdir -p "${USER_HOME}/.virtualenvs"
  "${PY_BIN}" -m venv "${VENV_DIR}"
fi

if [[ ! -x "${VENV_DIR}/bin/pip" ]]; then
  echo "ERROR: fresh virtualenv was not created at ${VENV_DIR}"
  echo "       Create it manually, then install deps:"
  echo "         mkvirtualenv --python=${PY_BIN} ${VENV_NAME}"
  echo "         pip install -r ${PROJECT_DIR}/requirements.txt"
  exit 1
fi

echo "==> Installing dependencies into the fresh virtualenv"
"${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
"${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"
echo "    Virtualenv ready: ${VENV_DIR}"
echo "    Web tab 'Virtualenv' path should be: ${VENV_DIR}"

# 6. Reload the web app
if [[ -f "${WSGI_FILE}" ]]; then
  echo "==> Reloading web app (touch ${WSGI_FILE})"
  touch "${WSGI_FILE}"
else
  echo "NOTE: WSGI file not found at ${WSGI_FILE}."
  echo "      Click 'Reload' on the PythonAnywhere Web tab to apply changes."
fi

echo "==> Done. New version is live."
echo "    Rollback if needed:  rm -rf ${PROJECT_DIR} && tar -xzf ${USER_HOME}/ZoomAttendancify_backup_${ts}.tar.gz -C ${USER_HOME}"
