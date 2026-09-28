#!/bin/bash
# ---------------------------------------------------------------------------
# Attendancify - PythonAnywhere redeploy helper
#
# Replaces the app code from zip file(s) while leaving your live data alone.
#
# NEVER TOUCHED (skipped by the delete, and never overwritten by a zip):
#   *.db / *.sqlite  ->  attendancify.db (AttendanceIQ), tango.db, +wal/shm
#   .env             ->  secrets
#   data/ uploads/   ->  user data
#   venv/ .venv/ env/ and ~/.local, ~/.virtualenvs  ->  installed python libs
#   .git/ backups/ logs/
#
# Usage (from a PythonAnywhere Bash console):
#   chmod +x ~/deploy_pythonanywhere.sh
#   ~/deploy_pythonanywhere.sh --dry-run     # preview, changes nothing
#   ~/deploy_pythonanywhere.sh               # normal redeploy
#   ~/deploy_pythonanywhere.sh --wipe-all    # nuke everything incl. db/.env
#   ZIP_DIR=~/incoming ~/deploy_pythonanywhere.sh
# ---------------------------------------------------------------------------
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/ZoomAttendancify}"
ZIP_DIR="${ZIP_DIR:-$HOME/zips}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/attendancify_$STAMP"

DRY_RUN=0
WIPE_ALL=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)  DRY_RUN=1 ;;
        --wipe-all) WIPE_ALL=1 ;;
        -h|--help)  sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
    esac
done

# Top-level names that survive the wipe. Anything matching stays exactly where
# it is - it is never copied, moved or restored, so nothing can go wrong with it.
KEEP_GLOBS=(
    "*.db" "*.db-wal" "*.db-shm" "*.sqlite" "*.sqlite3"
    ".env"
    "data" "uploads"
    "venv" ".venv" "env" ".virtualenvs"
    ".git" "backups" "logs"
)

# Nested upload targets. These live inside static/ (which does get replaced),
# so they are copied out before the wipe and merged back after extraction.
# The live file wins over a same-named file from the zip.
PRESERVE_NESTED=(
    "static/branding"   # header brand logo uploads
    "static/landing"    # landing page photo uploads
)

# Folders the app creates or reads at runtime.
REQUIRED_DIRS=(
    "uploads"
    "data"
    "static/branding"
    "static/landing"
    "static/css"
    "static/js"
    "static/images"
    "templates"
    "tango_static"
    "tango_templates"
)

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
run()  { if [ "$DRY_RUN" -eq 1 ]; then printf '    (dry-run) %s\n' "$*"; else eval "$@"; fi; }

is_kept() {
    local name="$1" g
    [ "$WIPE_ALL" -eq 1 ] && return 1
    for g in "${KEEP_GLOBS[@]}"; do
        # shellcheck disable=SC2053
        [[ "$name" == $g ]] && return 0
    done
    return 1
}

# --- sanity checks ---------------------------------------------------------
# Never let a bad APP_DIR turn this into "rm -rf /" or "rm -rf $HOME".
case "$APP_DIR" in
    ""|"/"|"$HOME"|"$HOME/") echo "Refusing to operate on APP_DIR='$APP_DIR'"; exit 1 ;;
esac

# The zip ships a copy of this script, which lands inside APP_DIR. Running that
# copy would delete the script out from under the running shell.
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
case "$SELF" in
    "$APP_DIR"/*)
        echo "This script is running from inside $APP_DIR, which it is about to erase."
        echo "Copy it out first, then run that copy:"
        echo "    cp '$SELF' ~/deploy_pythonanywhere.sh && bash ~/deploy_pythonanywhere.sh"
        exit 1 ;;
esac

if [ ! -d "$APP_DIR" ]; then
    say "App dir $APP_DIR does not exist - creating it"
    run "mkdir -p '$APP_DIR'"
fi

shopt -s nullglob
ZIPS=("$ZIP_DIR"/*.zip "$ZIP_DIR"/*.ZIP)
shopt -u nullglob

if [ ${#ZIPS[@]} -eq 0 ]; then
    echo "No .zip files found in $ZIP_DIR"
    echo "Upload your zip(s) there first (Files tab), or run with ZIP_DIR=/path/to/zips"
    exit 1
fi

say "App dir : $APP_DIR"
say "Zip dir : $ZIP_DIR (${#ZIPS[@]} archive(s), applied in name order)"
[ "$DRY_RUN" -eq 1 ]  && warn "DRY RUN - nothing will be changed"
[ "$WIPE_ALL" -eq 1 ] && warn "WIPE-ALL - databases, .env and uploads WILL be deleted too"

# --- 1. safety copy of the small critical files ----------------------------
# Code is in your zip already, so only the irreplaceable bits are copied.
# Big folders (uploads/, venv/) are skipped - they are never deleted anyway.
say "Backing up databases and .env to $BACKUP_DIR"
run "mkdir -p '$BACKUP_DIR'"
shopt -s nullglob
for f in "$APP_DIR"/*.db "$APP_DIR"/*.sqlite "$APP_DIR"/*.sqlite3 "$APP_DIR"/.env; do
    # .env has no wildcard, so nullglob cannot drop it when it is missing.
    [ -e "$f" ] || continue
    echo "    saving $(basename "$f")"
    run "cp -a '$f' '$BACKUP_DIR/'"
done

# Top-level .py files are small, and one of them may carry a hand edit made on
# the server (e.g. FLASK_SECRET_KEY uncommented in wsgi.py). Keep a copy.
for f in "$APP_DIR"/*.py; do
    [ -e "$f" ] || continue
    run "mkdir -p '$BACKUP_DIR/_code'"
    run "cp -a '$f' '$BACKUP_DIR/_code/'"
done
[ -d "$BACKUP_DIR/_code" ] && echo "    saving top-level *.py to _code/"
shopt -u nullglob

# Uploaded logos/photos live inside static/, which is about to be replaced.
if [ "$WIPE_ALL" -eq 0 ]; then
    for rel in "${PRESERVE_NESTED[@]}"; do
        if [ -d "$APP_DIR/$rel" ] && [ -n "$(ls -A "$APP_DIR/$rel" 2>/dev/null)" ]; then
            echo "    saving $rel/ ($(ls -1 "$APP_DIR/$rel" | wc -l | tr -d ' ') file(s))"
            run "mkdir -p '$BACKUP_DIR/_nested/$rel'"
            run "cp -a '$APP_DIR/$rel/.' '$BACKUP_DIR/_nested/$rel/'"
        fi
    done
fi

# --- 2. delete the code, keep the data -------------------------------------
say "Deleting old app files"
shopt -s dotglob nullglob
for path in "$APP_DIR"/*; do
    name="$(basename "$path")"
    if is_kept "$name"; then
        echo "    keeping  $name"
    else
        echo "    deleting $name"
        run "rm -rf '$path'"
    fi
done
shopt -u dotglob nullglob

# --- 3. extract every zip --------------------------------------------------
for zip in "${ZIPS[@]}"; do
    say "Extracting $(basename "$zip")"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '    (dry-run) unzip -> %s\n' "$APP_DIR"
        continue
    fi

    TMP="$(mktemp -d)"
    if command -v unzip >/dev/null 2>&1; then
        unzip -qo "$zip" -d "$TMP"
    else
        python3 -m zipfile -e "$zip" "$TMP"
    fi

    # Drop macOS junk before deciding how the archive is shaped.
    rm -rf "$TMP/__MACOSX"
    find "$TMP" -name '.DS_Store' -delete 2>/dev/null || true

    # If the zip wraps everything in a single top-level folder, unwrap it so
    # files land directly in APP_DIR instead of APP_DIR/Attendancify_June 2026/.
    # Only unwrap a real wrapper: a lone "static/" or "templates/" folder is
    # already at the right level and must be left alone.
    SRC="$TMP"
    entries=("$TMP"/*)
    if [ ${#entries[@]} -eq 1 ] && [ -d "${entries[0]}" ]; then
        candidate="${entries[0]}"
        name="$(basename "$candidate")"
        case "$name" in
            static|templates|uploads|data|tango_static|tango_templates|docker|tests|__pycache__)
                : ;;   # a real app folder - keep it
            *)
                # Treat as a wrapper only if it looks like the project root.
                for marker in comprehensive_app.py wsgi.py requirements.txt database.py templates static; do
                    if [ -e "$candidate/$marker" ]; then
                        echo "    unwrapping '$name/'"
                        SRC="$candidate"
                        break
                    fi
                done ;;
        esac
    fi

    # A stale database or .env inside the zip must not clobber the live one.
    if [ "$WIPE_ALL" -eq 0 ]; then
        while IFS= read -r stale; do
            echo "    ignoring $(basename "$stale") from zip (live copy wins)"
            rm -f "$stale"
        done < <(find "$SRC" \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \
                                -o -name '*.sqlite' -o -name '*.sqlite3' -o -name '.env' \) 2>/dev/null)
    fi

    # Copy contents including dotfiles; later zips overwrite earlier ones.
    (shopt -s dotglob nullglob; cp -a "$SRC"/* "$APP_DIR"/)
    rm -rf "$TMP"
done

# --- 4. required folders ---------------------------------------------------
say "Creating required folders"
for d in "${REQUIRED_DIRS[@]}"; do
    if [ -d "$APP_DIR/$d" ]; then
        echo "    exists  $d/"
    else
        echo "    created $d/"
        run "mkdir -p '$APP_DIR/$d'"
    fi
done

# --- 5. merge uploaded logos/photos back -----------------------------------
if [ "$WIPE_ALL" -eq 0 ]; then
    for rel in "${PRESERVE_NESTED[@]}"; do
        if [ -d "$BACKUP_DIR/_nested/$rel" ]; then
            say "Restoring uploaded files into $rel/"
            run "mkdir -p '$APP_DIR/$rel'"
            run "cp -a '$BACKUP_DIR/_nested/$rel/.' '$APP_DIR/$rel/'"
        fi
    done
fi

# --- 6. housekeeping -------------------------------------------------------
say "Cleaning caches and fixing permissions"
run "find '$APP_DIR' -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true"
run "find '$APP_DIR' -name '*.pyc' -delete 2>/dev/null || true"
run "find '$APP_DIR' -name '.DS_Store' -delete 2>/dev/null || true"
run "rm -rf '$APP_DIR/__MACOSX' 2>/dev/null || true"
run "chmod -R u+rwX '$APP_DIR'"
[ -f "$APP_DIR/.env" ] && run "chmod 600 '$APP_DIR/.env'"

# --- summary ---------------------------------------------------------------
echo
say "Done."
echo "    Backup : $BACKUP_DIR"
echo "    App    : $APP_DIR"
if [ "$DRY_RUN" -eq 0 ]; then
    shopt -s nullglob
    dbs=("$APP_DIR"/*.db)
    shopt -u nullglob
    echo "    DBs    : ${#dbs[@]} present$( [ ${#dbs[@]} -gt 0 ] && printf ' -> %s' "$(cd "$APP_DIR" && ls -1 *.db | tr '\n' ' ')" )"
fi
echo
echo "Next steps on PythonAnywhere:"
echo "  1. Reload the web app:"
echo "       touch /var/www/\${USER}_pythonanywhere_com_wsgi.py"
echo "     (or click Reload on the Web tab)"
echo "  2. Only if requirements.txt actually changed:"
echo "       pip install --user -r $APP_DIR/requirements.txt"
echo "     Your installed libs live in ~/.local or ~/.virtualenvs and were untouched."
echo "  3. Check the error log on the Web tab if the site 500s."
