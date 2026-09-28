from flask import Flask, render_template, request, redirect, url_for, send_file, flash, session, jsonify, abort, g, has_request_context
from markupsafe import Markup, escape
from werkzeug.exceptions import HTTPException
import os
import pandas as pd
from datetime import datetime, timedelta, timezone, time as datetime_time
import io
import tempfile
import csv
import re
import shutil
import sqlite3
import zipfile
from urllib.parse import quote
import hashlib
import secrets
import time
from rapidfuzz import fuzz
import unicodedata
from werkzeug.utils import secure_filename
import logging
import warnings

# Indian Standard Time (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_now():
    """Get current time in Indian Standard Time (IST)"""
    return datetime.now(IST)

def make_aware(dt):
    """Make a datetime object timezone-aware (IST) if it's naive"""
    if dt is None:
        return None
    if isinstance(dt, str):
        raw = dt
        try:
            # Try ISO first
            dt = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            # Fallbacks for common string formats that may be stored in older DBs.
            # Parse from `raw` every time: reusing `dt` here meant the second
            # attempt was strptime(None, ...), a TypeError this clause never caught.
            dt = None
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt

def format_ist_datetime(dt):
    """Format datetime to IST string"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).strftime('%Y-%m-%d %H:%M:%S')

# Import the core processing functions from the new module
from attendance_processing import (
    process_sessions_for_file, parse_datetime, write_excel, DEFAULT_PRESENCE_GRACE_SECONDS,
    filter_excluded_records, get_global_times
)

from summary_processing import (
    build_summary_rows, write_summary_report, SUMMARY_COLUMNS
)

# Import database functions
from database import (
    init_db, log_file_processing, get_processing_summary,
    get_user_processing_stats, get_user, update_user,
    delete_user, get_all_users, ensure_superadmin_exists, search_users,
    create_batch, update_batch, delete_batch, get_batches,
    get_batch, add_student_to_batch, update_batch_student,
    delete_batch_student, get_batch_students,
    get_all_batch_students_with_batches,
    create_user as db_create_user,  # Import with alias to avoid conflict
    register_processed_file, get_processed_files, get_processed_file_by_path,
    mark_file_eligible_for_deletion, mark_file_manually_deleted,
    add_excluded_participant, remove_excluded_participant, get_excluded_participants,
    get_excluded_identifiers, bulk_add_excluded_participants, update_excluded_participant,
    toggle_permanent_block, get_permanently_blocked_identifiers,
    get_files_eligible_for_auto_deletion, permanently_delete_old_records, grant_file_access, revoke_file_access,
    get_file_permissions, can_user_access_file,
    log_user_login, get_last_login, get_user_login_history, get_dashboard_stats, get_user_usage_summary,
    clear_login_history, get_usage_record_counts, get_dashboard_trends, get_tool_last_used,
    DEFAULT_SUPERADMIN_EMAIL, LEGACY_DEFAULT_SUPERADMIN_PASSWORD,
    get_app_setting, set_app_setting, get_all_app_settings, create_app_setting, delete_app_setting,
    get_output_format_settings, set_output_format_settings, get_all_output_format_settings,
    log_activity, get_activity_logs, get_activity_log_count, delete_activity_logs, clear_all_activity_logs,
    get_app_output_format, set_app_output_format, get_all_app_output_formats,
    delete_processing_logs, clear_all_processing_logs, get_processing_logs_with_ids,
    # Batch Group Management
    create_batch_group, update_batch_group, delete_batch_group, get_batch_groups, get_batch_group,
    assign_batch_to_group, remove_batch_from_group, get_batches_in_group, get_ungrouped_batches,
    grant_user_group_access, revoke_user_group_access, get_user_group_access, get_group_users,
    user_has_group_access, get_batches_for_user, get_groups_for_user, get_batch_count_by_group,
    # Batch Sub-Group Management
    create_batch_subgroup, update_batch_subgroup, delete_batch_subgroup, get_batch_subgroups, get_batch_subgroup,
    get_subgroups_for_user, assign_batch_to_subgroup, remove_batch_from_subgroup, get_batches_in_subgroup,
    # Batch-Scoped Learning
    upsert_batch_record, touch_batch_record, get_batch_records_map, get_batch_records,
    # Audit trail
    get_ip_activity_summary, prune_activity_logs
)

# The app-level log_activity below wraps this one so every call records the
# caller's address without each site having to work it out for itself.
from database import log_activity as _db_log_activity

# ----------- Processing overlay configuration -----------
# The staged overlay shown while a tool's form POST is in flight. Superadmins
# can retitle the stages, add or remove them, or switch the whole thing off from
# Settings; these are the defaults used until they do.
PROCESSING_OVERLAY_TOOLS = [
    ('attendance-upload', 'Attendance Generator - upload step'),
    ('attendance-process', 'Attendance Generator - generate step'),
    ('smart-matcher', 'Smart Matcher'),
    ('final-excel', 'Session Report Generator'),
    ('raw-excel', 'Raw Excel Generator'),
    ('summary', 'Meeting Summary'),
]

PROCESSING_OVERLAY_DEFAULTS = {
    'attendance-upload': {
        'title': 'Uploading Zoom logs',
        'steps': [
            'Uploading your file(s)',
            'Checking the file structure',
            'Detecting session times',
            'Opening the session setup',
        ],
    },
    'attendance-process': {
        'title': 'Generating attendance',
        'steps': [
            'Sending your session settings',
            'Reading join and leave rows',
            'Merging duplicate entries',
            'Calculating time in each session',
            'Marking present and absent',
            'Building the attendance workbook',
        ],
    },
    'smart-matcher': {
        'title': 'Matching participants',
        'steps': [
            'Uploading your file(s)',
            'Reading the master list',
            'Reading raw participant names',
            'Matching names against the list',
            'Building the matched workbook',
        ],
    },
    'final-excel': {
        'title': 'Generating session reports',
        'steps': [
            'Uploading your file(s)',
            'Reading the attendance sheet',
            'Splitting the sessions',
            'Writing one report per session',
        ],
    },
    'raw-excel': {
        'title': 'Generating Excel',
        'steps': [
            'Uploading your file(s)',
            'Reading the raw log',
            'Extracting participant rows',
            'Building the Excel file',
        ],
    },
    'summary': {
        'title': 'Building meeting summary',
        'steps': [
            'Uploading your file(s)',
            'Reading Subject, Meeting ID and End Time',
            'Counting participants',
            'Building the summary report',
        ],
    },
}

PROCESSING_OVERLAY_SETTING_KEY = 'processing_overlay_config'
PROCESSING_OVERLAY_MAX_STEPS = 12


def get_processing_overlay_config():
    """Overlay settings merged over the defaults.

    A saved value only overrides what it actually contains, so a tool that was
    never customised keeps its built-in stages, and a stored value that has gone
    bad falls back rather than leaving the overlay blank.
    """
    config = {
        'enabled': True,
        'show_elapsed': True,
        'subtitle': 'Keep this tab open until it finishes.',
        'slow_notice': 'This is taking longer than usual. The file is still being '
                       'processed - a large file or a slow connection can stretch this out.',
        'tools': {key: {'title': value['title'], 'steps': list(value['steps'])}
                  for key, value in PROCESSING_OVERLAY_DEFAULTS.items()},
    }

    try:
        raw = get_app_setting(PROCESSING_OVERLAY_SETTING_KEY)
        if not raw:
            return config
        import json as _json
        saved = _json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(saved, dict):
            return config

        if 'enabled' in saved:
            config['enabled'] = bool(saved['enabled'])
        if 'show_elapsed' in saved:
            config['show_elapsed'] = bool(saved['show_elapsed'])
        for text_key in ('subtitle', 'slow_notice'):
            if saved.get(text_key):
                config[text_key] = str(saved[text_key])[:300]

        for key, tool in (saved.get('tools') or {}).items():
            if key not in config['tools'] or not isinstance(tool, dict):
                continue
            if tool.get('title'):
                config['tools'][key]['title'] = str(tool['title'])[:120]
            steps = tool.get('steps')
            if isinstance(steps, list):
                cleaned = [str(step).strip()[:160] for step in steps if str(step).strip()]
                if cleaned:
                    config['tools'][key]['steps'] = cleaned[:PROCESSING_OVERLAY_MAX_STEPS]
    except Exception as exc:
        print(f'Processing overlay config unreadable, using defaults: {exc}')

    return config


# ----------- Maintenance Mode -----------
# A superadmin-controlled "site is down" page shown to everyone who is NOT a
# superadmin while it is switched on. The normal login/landing pages are left
# exactly as they are; this only decides whether a visitor sees them or the
# maintenance page instead. Superadmin always passes straight through so the
# site can still be managed (and switched back on) while it is "down".
MAINTENANCE_SETTING_KEY = 'maintenance_config'

MAINTENANCE_DEFAULTS = {
    'enabled': False,
    'heading': 'This site is currently down\ndue to nonpayment',
    'message': ('The domain for this PythonAnywhere-hosted site has been suspended '
                'because the account is not active due to nonpayment.\n\n'
                'Once the payment is made and the account is reactivated, '
                'the site will be available again.'),
}


def get_maintenance_config():
    """Maintenance-page settings merged over the defaults.

    A stored value only overrides what it actually contains, and a value that
    has gone bad falls back to the defaults rather than leaving the page blank
    or, worse, leaving the whole site walled off with no readable message.
    """
    config = dict(MAINTENANCE_DEFAULTS)

    try:
        raw = get_app_setting(MAINTENANCE_SETTING_KEY)
        if not raw:
            return config
        import json as _json
        saved = _json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(saved, dict):
            return config

        if 'enabled' in saved:
            config['enabled'] = bool(saved['enabled'])
        if saved.get('heading'):
            config['heading'] = str(saved['heading'])[:200]
        if saved.get('message'):
            config['message'] = str(saved['message'])[:2000]
    except Exception as exc:
        print(f'Maintenance config unreadable, using defaults: {exc}')

    return config


def maintenance_is_active():
    """True only when maintenance mode is switched on."""
    try:
        return bool(get_maintenance_config().get('enabled'))
    except Exception:
        return False


# Helper function to get app names
def get_app_names():
    """Get application names from settings"""
    try:
        return {
            'app1': get_app_setting('app1_name', 'Zoom Attendance Analyzer'),
            'app2': get_app_setting('app2_name', 'Intelligent Record Matcher'),
            'app3': get_app_setting('app3_name', 'Session Report Generator')
        }
    except Exception as e:
        print(f"Error fetching app names: {e}")
        return {
            'app1': 'Zoom Attendance Analyzer',
            'app2': 'Intelligent Record Matcher',
            'app3': 'Session Report Generator'
        }


# Output Format Settings: the only columns that are safe to offer for rename/hide are
# the fixed, predictable ones each tool always produces. Session columns are excluded
# on purpose -- their labels embed a live timestamp/date (Zoom Attendance Generator) or
# come straight from whatever the uploaded file's own headers happen to be (Smart
# Matcher), so a saved exact-string mapping for one would silently stop matching next
# time a file with a different date/label is generated.
APP_OUTPUT_CONFIGURABLE_COLUMNS = {
    'app1': ['Name', 'Email', 'Join Time', 'Leave Time', 'Total Duration', 'Shortfall Reason'],
    'app2': ['Email', 'Participant Name', 'Raw Name', 'Confidence'],
    'app3': ['email_id', 'attendance(absent/present/leave)'],
}


def get_output_column_config(app_name):
    """Parse an app's custom_columns_json into (renames, hidden).

    Robust to: the app having no saved config yet, malformed JSON (both return empty
    rather than raising -- a bad save should never block file generation), and the
    older flat rename-only dict format from before "hide a column" existed (treated as
    renames with nothing hidden).
    """
    app_format = get_app_output_format(app_name)
    renames, hidden = {}, set()
    if not app_format or not app_format['custom_columns_json']:
        return renames, hidden
    try:
        import json
        data = json.loads(app_format['custom_columns_json'])
        if isinstance(data, dict):
            if 'renames' in data or 'hidden' in data:
                renames = data.get('renames') or {}
                hidden = set(data.get('hidden') or [])
            else:
                renames = data  # legacy flat {original: new_name} format
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        print(f"Warning: Could not parse custom columns JSON for {app_name}: {e}")
    return renames, hidden

app = Flask(__name__, static_url_path='/static', static_folder='static')

# Werkzeug caps non-file form fields (e.g. a pasted-signature hidden input
# carrying an inline base64 image) at 500KB by default -- fine for ordinary
# forms, but too small for something like a logo pasted into Tango's
# signature editor. Flask doesn't expose this via app.config, so it has to be
# raised on the Request class directly; a small subclass keeps that change
# local to this app instead of mutating Werkzeug's shared base class.
class _RequestWithLargerFormMemory(Flask.request_class):
    max_form_memory_size = 5 * 1024 * 1024  # 5 MB

app.request_class = _RequestWithLargerFormMemory
# ----------------------------------------------------------------------
# Duplicate root handler removed – index is defined later (around line 2970)
# ----------------------------------------------------------------------
# SECURITY: Set FLASK_SECRET_KEY environment variable in production!
# Generate a secure key: python -c "import secrets; print(secrets.token_hex(32))"
secret_key = os.environ.get('FLASK_SECRET_KEY')
if not secret_key:
    import warnings
    warnings.warn("FLASK_SECRET_KEY not set! Using fallback key. Set environment variable for production!", UserWarning)
    secret_key = 'fallback-key-change-in-production-' + secrets.token_hex(16)
app.secret_key = secret_key

# Hidden superadmin password recovery endpoint (see near the /svc/9f4a71c2 route below).
# Unset by default -- the endpoint returns 404 unless this env var is explicitly set.
# Generate one with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
SUPERADMIN_RESET_KEY = os.environ.get('SUPERADMIN_RESET_KEY')
# Template auto-reload should be disabled in production for security and performance
# Only enable in development by setting FLASK_ENV=development
is_development = os.environ.get('FLASK_ENV', 'production').lower() == 'development'
app.config['TEMPLATES_AUTO_RELOAD'] = is_development
if not is_development:
    # In production, use template caching for better performance
    app.jinja_env.cache = {}
else:
    # In development, clear cache to force template recompile
    app.jinja_env.cache = {}

# Configure session settings
# Security: In production with HTTPS, SESSION_COOKIE_SECURE must be True
# For local development over HTTP, it can be False
is_production = os.environ.get('FLASK_ENV', 'production').lower() == 'production'
app.config['SESSION_COOKIE_SECURE'] = is_production  # True in production (HTTPS), False in development
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Prevent JavaScript access to session cookie
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # Prevent CSRF
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)  # Session expires after 24 hours
app.config['SESSION_REFRESH_EACH_REQUEST'] = True  # Refresh session on each request

@app.before_request
def make_session_permanent():
    session.permanent = True  # Make session persistent


# ----------- CSRF protection -----------
# Without this, any page on the internet can make a logged-in administrator's
# browser POST to this app - creating users, changing roles, deleting batches -
# because the session cookie rides along automatically. Every unsafe request
# must carry a token that only this app's own pages can know.
CSRF_FIELD = '_csrf_token'
CSRF_HEADER = 'X-CSRFToken'
CSRF_SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS', 'TRACE'}


def csrf_token():
    """The caller's token, minted on first use and kept for the session."""
    token = session.get(CSRF_FIELD)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_FIELD] = token
    return token


def csrf_field():
    """The hidden input every form includes."""
    return Markup(f'<input type="hidden" name="{CSRF_FIELD}" value="{escape(csrf_token())}">')


app.jinja_env.globals['csrf_token'] = csrf_token
app.jinja_env.globals['csrf_field'] = csrf_field


@app.before_request
def enforce_csrf():
    if request.method in CSRF_SAFE_METHODS:
        return
    if getattr(app.view_functions.get(request.endpoint), '_csrf_exempt', False):
        return
    expected = session.get(CSRF_FIELD)
    supplied = request.form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER, '')
    if not expected or not supplied or not secrets.compare_digest(str(supplied), str(expected)):
        # 400, not a redirect: this is a malformed request, and answering it
        # with a normal page would hide the failure from whoever sent it.
        abort(400, description='Security check failed. Reload the page and try again.')


def csrf_exempt(view):
    """Mark a view as not needing a token (nothing uses this yet)."""
    view._csrf_exempt = True
    return view


@app.after_request
def set_security_headers(response):
    """Headers every response should carry.

    No Content-Security-Policy: the pages rely on inline <script> and style
    attributes throughout, so a policy strict enough to be worth having would
    break them. That is worth doing later, deliberately, page by page.
    """
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')          # no framing => no clickjacking
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
    if request.is_secure:
        response.headers.setdefault('Strict-Transport-Security',
                                    'max-age=31536000; includeSubDomains')
    return response


# ----------- Login throttling -----------
# Held in memory, so it resets when the web app is reloaded and each worker
# counts on its own. That makes it a speed bump rather than a wall, but it turns
# an unlimited online guessing attack into a slow one. A shared store (the
# database, or Redis) would be needed to make it strict.
LOGIN_MAX_ATTEMPTS = 8
LOGIN_LOCKOUT_SECONDS = 900
_login_failures = {}


def get_client_ip():
    """The caller's address, as seen from behind PythonAnywhere's proxy.

    remote_addr is the proxy on a hosted deployment, so the real client sits at
    the head of X-Forwarded-For. Only the first entry is trusted - the rest are
    supplied by the client and can say anything.
    """
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        candidate = forwarded.split(',')[0].strip()
        if candidate:
            return candidate[:45]  # long enough for IPv6, short enough to store
    real_ip = request.headers.get('X-Real-IP', '').strip()
    if real_ip:
        return real_ip[:45]
    return (request.remote_addr or 'Unknown')[:45]


def get_client_user_agent():
    """The caller's browser string, trimmed for storage."""
    return (request.headers.get('User-Agent', '') or 'Unknown')[:300]


def log_activity(user_email, action_type, action_description='', ip_address=None,
                 metadata_json=None, user_agent=None):
    """Record an audited action, stamping the caller's address automatically.

    Wraps the database function so the ~30 existing call sites all gain the
    client IP and browser string without repeating the header parsing, and so
    the catch-all hook below knows this request was already accounted for.
    """
    if has_request_context():
        if ip_address is None:
            ip_address = get_client_ip()
        if user_agent is None:
            user_agent = get_client_user_agent()
        g._activity_logged = True
    return _db_log_activity(
        user_email=user_email or 'anonymous',
        action_type=action_type,
        action_description=action_description,
        ip_address=ip_address,
        metadata_json=metadata_json,
        user_agent=user_agent
    )


# Endpoints deliberately left out of the automatic trail: the search box fires
# on every keystroke, and the audit page itself would log every page turn.
AUDIT_SKIP_ENDPOINTS = {
    'static', 'dashboard_search', 'activity_logs', 'health_check'
}

# Reads worth recording even though they change nothing: taking data out of the
# system is exactly what an investigation needs to reconstruct.
AUDIT_READ_KEYWORDS = ('download', 'export', 'backup')


@app.after_request
def audit_request(response):
    """Record every state-changing request, and every download, against an IP.

    Runs after the view so the recorded status code reflects what the caller
    actually got - a denied attempt is as interesting as a successful one.
    """
    try:
        endpoint = request.endpoint or ''
        if endpoint in AUDIT_SKIP_ENDPOINTS:
            return response

        method = request.method.upper()
        changes_state = method not in ('GET', 'HEAD', 'OPTIONS', 'TRACE')
        is_tracked_read = any(word in endpoint for word in AUDIT_READ_KEYWORDS)
        if not changes_state and not is_tracked_read:
            return response

        # A view that logged its own, richer entry has already been recorded.
        if getattr(g, '_activity_logged', False):
            return response

        import json as _json
        _db_log_activity(
            user_email=session.get('user_id') or 'anonymous',
            action_type=endpoint or 'unknown_endpoint',
            action_description=f'{method} {request.path} -> {response.status_code}',
            ip_address=get_client_ip(),
            metadata_json=_json.dumps({
                'method': method,
                'path': request.path,
                'endpoint': endpoint,
                'status': response.status_code,
                'referrer': (request.referrer or '')[:200],
            }),
            user_agent=get_client_user_agent()
        )
    except Exception as exc:
        # An audit failure must never take the response down with it.
        print(f'Audit hook error: {exc}')
    return response


def _login_bucket(email):
    return (get_client_ip(), (email or '').strip().lower())


def login_lockout_remaining(email):
    """Seconds left before this address may try this account again, or 0."""
    count, until = _login_failures.get(_login_bucket(email), (0, 0.0))
    if count >= LOGIN_MAX_ATTEMPTS and time.time() < until:
        return int(until - time.time())
    return 0


def note_login_failure(email):
    key = _login_bucket(email)
    count, _ = _login_failures.get(key, (0, 0.0))
    _login_failures[key] = (count + 1, time.time() + LOGIN_LOCKOUT_SECONDS)

    # Recorded against the attempted address rather than the session, which is
    # empty here: a run of these from one IP is what a break-in attempt looks like.
    attempts = count + 1
    log_activity(
        (email or 'unknown').strip().lower(),
        'login_failed',
        f'Failed sign-in attempt {attempts} of {LOGIN_MAX_ATTEMPTS} for {email or "unknown"}'
        + (' - address now locked out' if attempts >= LOGIN_MAX_ATTEMPTS else '')
    )

    # Keep the table from growing without bound on a long-running worker.
    if len(_login_failures) > 2048:
        now = time.time()
        for k, (_, expiry) in list(_login_failures.items()):
            if expiry < now:
                _login_failures.pop(k, None)


def clear_login_failures(email):
    _login_failures.pop(_login_bucket(email), None)

# ----------- Signed-out landing page content -----------
# Every sentence on the story landing page lives in this spec. Settings ->
# Landing Page renders it as a form, so the page can be reworded without
# touching the template. Each field is one app_settings row; a missing row
# falls back to the default below.
#   'text'   -> single line
#   'block'  -> blank lines split paragraphs, single newlines wrap
#   'lines'  -> one block, every line kept on its own line
#   'toggle' -> a show/hide switch, stored as '1' or '0'
# The three text kinds accept the inline Markdown subset (**bold**, *italic*,
# `code`, [text](link)) and are HTML-escaped first, so nothing typed into the
# editor can inject markup into the page.
LANDING_SECTIONS = [
    {
        'id': 'hero',
        'title': 'Introduction (top of the page)',
        'icon': 'fa-star',
        'fields': [
            {'key': 'lp_hero_show', 'label': 'Show this section on the page', 'type': 'toggle',
             'default': '1',
             'help': 'Off removes the whole section and its Story Navigation link.'},
            {'key': 'lp_hero_pill', 'label': 'Badge', 'type': 'text', 'default': 'Welcome',
             'help': 'Small pill above the heading.'},
            {'key': 'lp_hero_title', 'label': 'Heading', 'type': 'text', 'default': 'Hello World! 👋'},
            {'key': 'lp_hero_subtitle', 'label': 'Sub-heading', 'type': 'text',
             'default': 'Welcome to this page.'},
            {'key': 'lp_hero_intro', 'label': 'Opening paragraph', 'type': 'block',
             'default': 'This domain is intended **only for official use**. Access is strictly '
                        'restricted to the authorized administrators:'},
            # The two name chips under the opening paragraph. Their own fields, not
            # the sidebar's: the same people can be named here and not there, or the
            # other way round.
            {'key': 'lp_chip1_name', 'label': 'Name chip 1', 'type': 'text', 'default': 'Satendra Goswami',
             'help': 'Shown under the opening paragraph. Separate from the sidebar card.'},
            {'key': 'lp_chip1_show', 'label': 'Show name chip 1', 'type': 'toggle', 'default': '1'},
            {'key': 'lp_chip2_name', 'label': 'Name chip 2', 'type': 'text', 'default': '',
             'help': 'Shown under the opening paragraph. Separate from the sidebar card.'},
            {'key': 'lp_chip2_show', 'label': 'Show name chip 2', 'type': 'toggle', 'default': '1'},
            {'key': 'lp_hero_outro', 'label': 'Closing paragraph', 'type': 'block',
             'default': "If you've reached this page, take a moment to discover the story behind "
                        'this project — a journey of determination, continuous learning, resilience, '
                        'and the pursuit of solving real-world problems through technology.'},
        ],
    },
    {
        'id': 'story',
        'title': 'The story timeline',
        'icon': 'fa-map-signs',
        'fields': [
            {'key': 'lp_story_show', 'label': 'Show this section on the page', 'type': 'toggle',
             'default': '1',
             'help': 'Off removes the whole section and its Story Navigation link.'},
            {'key': 'lp_story_title', 'label': 'Section heading', 'type': 'text',
             'default': 'The Story Behind This Application'},
            {'key': 'lp_story_sub', 'label': 'Section sub-heading', 'type': 'text',
             'default': 'Every meaningful solution starts with a challenge.'},
            {'key': 'lp_step1_title', 'label': 'Step 1 — title', 'type': 'text', 'default': '2024'},
            {'key': 'lp_step1_text', 'label': 'Step 1 — text', 'type': 'block',
             'default': 'The idea was conceived by **Satendra Goswami** while handling one of the most '
                        'repetitive parts of his daily work — preparing attendance manually from Zoom '
                        'meeting logs.'},
            {'key': 'lp_step2_title', 'label': 'Step 2 — title', 'type': 'text', 'default': 'The Challenge'},
            {'key': 'lp_step2_text', 'label': 'Step 2 — text', 'type': 'block',
             'default': 'The task was time-consuming, mentally exhausting, and prone to human error. '
                        'Balancing work responsibilities while dealing with health concerns was never '
                        'easy, but instead of accepting the situation, he turned the challenge into an '
                        'opportunity.'},
            {'key': 'lp_step3_title', 'label': 'Step 3 — title', 'type': 'text', 'default': 'The Beginning'},
            {'key': 'lp_step3_text', 'label': 'Step 3 — text', 'type': 'block',
             'default': 'With no dedicated development team and only limited time outside regular duties, '
                        'Satendra began researching, learning, experimenting, and building the application '
                        'from scratch. Every stage demanded patience, persistence, and continuous learning.'},
            {'key': 'lp_step4_title', 'label': 'Step 4 — title', 'type': 'text', 'default': 'Transformation'},
            {'key': 'lp_step4_text', 'label': 'Step 4 — text', 'type': 'block',
             'default': 'Months of consistent effort transformed a simple Python tool into a full-fledged '
                        'attendance automation platform with multiple intelligent features.'},
            {'key': 'lp_step5_title', 'label': 'Step 5 — title', 'type': 'text', 'default': 'Today'},
            {'key': 'lp_step5_text', 'label': 'Step 5 — text', 'type': 'block',
             'default': 'The application now includes powerful automation tools like the **Bulk ICS '
                        'Generator**, which creates standard-compliant calendar invitations for large '
                        'numbers of recipients.'},
            {'key': 'lp_quote', 'label': 'Quote', 'type': 'lines',
             'default': 'If a repetitive task can be automated,\n'
                        'valuable human time should be spent on more meaningful work instead.',
             'help': 'Each line is shown on its own line inside the quote box.'},
        ],
    },
    {
        'id': 'creator',
        'title': 'About the creator',
        'icon': 'fa-user-astronaut',
        # The portrait beside this writing is the Person 1 photo from the
        # Photos card above - one person, one head-shot.
        'fields': [
            {'key': 'lp_creator_show', 'label': 'Show this section on the page', 'type': 'toggle',
             'default': '1',
             'help': 'Off removes the whole section and its Story Navigation link.'},
            {'key': 'lp_creator_title', 'label': 'Section heading', 'type': 'text',
             'default': 'About the Creator'},
            {'key': 'lp_creator_lead', 'label': 'Opening line', 'type': 'text',
             'default': 'Every project has a story, and every story begins with a person '
                        'determined to solve a real problem.'},
            {'key': 'lp_creator_name', 'label': 'Card — name', 'type': 'text',
             'default': 'Satendra Goswami'},
            {'key': 'lp_creator_role', 'label': 'Card — role', 'type': 'text',
             'default': 'Creator, Designer & Developer'},
            {'key': 'lp_creator_instagram', 'label': 'Instagram link', 'type': 'text', 'default': '',
             'help': 'Full https:// address. Leave blank to hide the button.'},
            {'key': 'lp_creator_work', 'label': 'The professional journey', 'type': 'block',
             'default': 'AttendanceIQ was conceived, designed, and developed by **Satendra Goswami**, '
                        'a technology professional with over 10 years of experience across multiple '
                        'technical domains. Throughout his career, he has worked in diverse roles, '
                        'gaining practical knowledge from infrastructure management to software '
                        'development and automation.\n\n'
                        'His professional journey includes serving as a **Server Administrator**, where '
                        'he managed server infrastructure, deployments, and system administration. He has '
                        'also worked with **Apple India** as an Equipment Testing Engineer, followed by '
                        'the role of Service Engineer Trainer, contributing to technical operations and '
                        'knowledge transfer.\n\n'
                        'Beyond enterprise support, Satendra has been actively involved in the online '
                        'assessment industry, where he worked on the server-side operations of examination '
                        'platforms. His responsibilities ranged from configuring examination servers before '
                        'assessments to ensuring reliable system performance and supporting the complete '
                        'examination lifecycle through result generation.\n\n'
                        'Satendra also has hands-on experience with **Amazon Web Services (AWS)**, '
                        'including cloud deployment, application migration, and infrastructure management. '
                        'Over the years, he has explored various areas of technology, including Android ROM '
                        'testing, mobile software evaluation, and performance testing, further broadening '
                        'his technical perspective.',
             'help': 'Leave a blank line between paragraphs.'},
            {'key': 'lp_creator_pivot', 'label': 'Turning line', 'type': 'text',
             'default': 'Technology, however, is only one part of who he is.',
             'help': 'Shown on its own between the professional and personal halves.'},
            {'key': 'lp_creator_personal', 'label': 'Beyond the work', 'type': 'block',
             'default': 'Outside of his professional work, Satendra enjoys reading — particularly '
                        '*Dharmik Granth* (Hindu scriptures and philosophical literature) — which has '
                        'greatly influenced his perspective on discipline, patience, and continuous '
                        'self-improvement. He also enjoys writing and regularly shares his thoughts and '
                        'reflections through his Instagram profile.\n\n'
                        'Those who know him personally often describe him as a patient learner, a '
                        'thoughtful listener, and someone who is always willing to help whenever he can.',
             'help': 'Leave a blank line between paragraphs.'},
            # Written in the first person, where the rest of the section is in the
            # third. That is deliberate: it is set apart on the page under its own
            # heading, so the change of voice reads as the man speaking for himself
            # rather than as a slip.
            {'key': 'lp_creator_reflection_title', 'label': 'Reflection — heading', 'type': 'text',
             'default': 'In his own words',
             'help': 'Marks the switch from "he" to "I". Leave blank to drop the heading.'},
            {'key': 'lp_creator_reflection', 'label': 'Reflection — text', 'type': 'block',
             'default': "Many years ago, I watched *Iron Man* for the first time. What inspired me most "
                        "wasn't when Tony Stark became Iron Man — it was the moment before that.\n\n"
                        'After being kidnapped and held captive, he found himself in one of the darkest '
                        'situations of his life. Injured, surrounded by enemies, and with very limited '
                        'resources, he could have accepted defeat. Instead, he chose to think differently. '
                        'Rather than focusing on the problem, he focused on finding a solution. He used his '
                        'knowledge, determination, and creativity to build a way out.\n\n'
                        'That moment has stayed with me ever since.\n\n'
                        'Whenever I face a challenge in life or in my work, I try to follow the same '
                        "mindset. I don't believe in running away from problems. Instead, I believe every "
                        "difficult situation has a solution if you're willing to learn, think patiently, "
                        'and keep moving forward. Challenges are not roadblocks — they are opportunities '
                        'to grow, improve, and create something better.\n\n'
                        'This philosophy has shaped the way I approach both life and technology. Every '
                        'obstacle becomes a chance to learn, every setback becomes a lesson, and every '
                        'project becomes an opportunity to build something meaningful.',
             'help': 'Leave a blank line between paragraphs.'},
            {'key': 'lp_creator_reflection_show', 'label': 'Show the reflection', 'type': 'toggle',
             'default': '1'},
            {'key': 'lp_creator_closing', 'label': 'Closing paragraph', 'type': 'block',
             'default': 'This application is more than a software project — it reflects years of '
                        'practical experience, continuous learning, and the belief that technology should '
                        "solve real-world problems while making people's work simpler, faster, and more "
                        'efficient.'},
            {'key': 'lp_creator_quote', 'label': 'Quote', 'type': 'lines',
             'default': "I don't believe learning has a finish line.\n"
                        'Every challenge is another opportunity to improve, and every solution is '
                        'another step toward helping someone work better.',
             'help': 'Each line is shown on its own line inside the quote box.'},
            {'key': 'lp_creator_quote_by', 'label': 'Quote — attributed to', 'type': 'text',
             'default': 'Satendra Goswami'},
        ],
    },
    {
        'id': 'support',
        'title': 'Support along the journey',
        'icon': 'fa-hands-helping',
        'fields': [
            {'key': 'lp_support_show', 'label': 'Show this section on the page', 'type': 'toggle',
             'default': '1',
             'help': 'Off removes the whole section and its Story Navigation link.'},
            {'key': 'lp_support_title', 'label': 'Section heading', 'type': 'text',
             'default': 'Support Along the Journey'},
            {'key': 'lp_support_text', 'label': 'Text', 'type': 'block',
             'default': 'Although this application was designed and developed by **Satendra Goswami**, '
                        'every meaningful journey becomes stronger with the support of people who '
                        'genuinely believe in it.',
             'help': 'Leave a blank line between paragraphs.'},
            {'key': 'lp_support_card_name', 'label': 'Card — name', 'type': 'text', 'default': '',
             'help': 'The name on the card in this section. Type any name here to replace it.'},
            # Its own line rather than borrowing the sidebar's Person 2 role, so
            # the same person can be described differently in the two places -
            # and so renaming the card does not leave someone else's title on it.
            {'key': 'lp_support_card_role', 'label': 'Card — role', 'type': 'text',
             'default': 'Administrator',
             'help': 'Shown under the name. Leave blank to show the name on its own.'},
            {'key': 'lp_support_photo_show', 'label': 'Show a photo on this card', 'type': 'toggle',
             'default': '1',
             'help': 'Uses the Person 2 head-shot from the Photos card above. '
                     'Off shows the initials badge instead; the uploaded file is kept.'},
            {'key': 'lp_support_card_text', 'label': 'Card — lines', 'type': 'lines',
             'default': 'A good friend,\na supportive colleague,\na brother from another mother.'},
        ],
    },
    {
        'id': 'values',
        'title': 'What this application represents',
        'icon': 'fa-gem',
        'fields': [
            {'key': 'lp_values_show', 'label': 'Show this section on the page', 'type': 'toggle',
             'default': '1',
             'help': 'Off removes the whole section and its Story Navigation link.'},
            {'key': 'lp_values_title', 'label': 'Section heading', 'type': 'text',
             'default': 'What This Application Represents'},
            {'key': 'lp_value1_title', 'label': 'Card 1 — title', 'type': 'text', 'default': 'Months of Research'},
            {'key': 'lp_value1_text', 'label': 'Card 1 — text', 'type': 'text',
             'default': 'Continuous learning and exploration.'},
            {'key': 'lp_value2_title', 'label': 'Card 2 — title', 'type': 'text', 'default': 'Perseverance'},
            {'key': 'lp_value2_text', 'label': 'Card 2 — text', 'type': 'text',
             'default': 'Overcoming challenges with determination.'},
            {'key': 'lp_value3_title', 'label': 'Card 3 — title', 'type': 'text', 'default': 'Problem Solving'},
            {'key': 'lp_value3_text', 'label': 'Card 3 — text', 'type': 'text',
             'default': 'Solving real-world problems with technology.'},
            {'key': 'lp_value4_title', 'label': 'Card 4 — title', 'type': 'text',
             'default': 'Continuous Improvement'},
            {'key': 'lp_value4_text', 'label': 'Card 4 — text', 'type': 'text',
             'default': 'Evolving every day to deliver more value.'},
        ],
    },
    {
        'id': 'thanks',
        'title': 'Thank you & contact',
        'icon': 'fa-heart',
        'fields': [
            {'key': 'lp_thanks_show', 'label': 'Show this section on the page', 'type': 'toggle',
             'default': '1',
             'help': 'Off removes the whole section and its Story Navigation link.'},
            {'key': 'lp_thanks_title', 'label': 'Section heading', 'type': 'text', 'default': 'Thank You'},
            {'key': 'lp_thanks_text', 'label': 'Text', 'type': 'block',
             'default': 'Thank you for taking the time to read the story behind this project.\n\n'
                        'Every feature represents months of learning, research, problem-solving, and '
                        'continuous improvement.\n\n'
                        'The journey continues, and every new feature is another step toward making '
                        'everyday work faster, more accurate, and more efficient.',
             'help': 'Leave a blank line between paragraphs.'},
            {'key': 'lp_contact_text', 'label': 'Contact card text', 'type': 'block',
             'default': 'If you have suggestions, feedback, or would simply like to discuss the project, '
                        'feel free to contact:'},
            {'key': 'lp_contact_email', 'label': 'Contact email', 'type': 'text', 'default': '',
             'help': 'Leave blank to use the footer email from the Footer tab.'},
        ],
    },
    {
        'id': 'people',
        'title': 'Official access (sidebar)',
        'icon': 'fa-shield-alt',
        # Every line of this card can be switched off on its own, so it can be
        # trimmed down to just the heading, just the people, or hidden outright.
        'fields': [
            {'key': 'lp_access_show', 'label': 'Show this card on the page', 'type': 'toggle', 'default': '1',
             'help': 'Off hides the whole card, whatever the switches below say.'},
            {'key': 'lp_access_title', 'label': 'Card heading', 'type': 'text', 'default': 'Official Access'},
            {'key': 'lp_access_title_show', 'label': 'Show the heading', 'type': 'toggle', 'default': '1'},
            {'key': 'lp_access_text', 'label': 'Card text', 'type': 'block',
             'default': 'This application is intended for official use only. Access is strictly '
                        'restricted to the authorized administrators.'},
            {'key': 'lp_access_text_show', 'label': 'Show the card text', 'type': 'toggle', 'default': '1'},
            {'key': 'lp_person1_name', 'label': 'Person 1 — name', 'type': 'text', 'default': 'Satendra Goswami'},
            {'key': 'lp_person1_role', 'label': 'Person 1 — role', 'type': 'text',
             'default': 'Creator & Administrator'},
            {'key': 'lp_person1_show', 'label': 'Show person 1 on the card', 'type': 'toggle', 'default': '1',
             'help': 'This card only. The name chips in the introduction have their own fields.'},
            {'key': 'lp_person2_name', 'label': 'Person 2 — name', 'type': 'text', 'default': ''},
            {'key': 'lp_person2_role', 'label': 'Person 2 — role', 'type': 'text', 'default': 'Administrator'},
            {'key': 'lp_person2_show', 'label': 'Show person 2 on the card', 'type': 'toggle', 'default': '1',
             'help': 'This card only. The name chips in the introduction have their own fields.'},
        ],
    },
    {
        'id': 'motivation',
        'title': 'Motivation (sidebar)',
        'icon': 'fa-quote-left',
        # A third card under the sidebar: someone else's photo and the words of
        # theirs worth keeping in view. Off until there is something to put in it.
        'fields': [
            {'key': 'lp_motivation_show', 'label': 'Show this card on the page', 'type': 'toggle', 'default': '0',
             'help': 'Upload the photo in slot 3 above and write the quote first, then switch it on.'},
            {'key': 'lp_motivation_title', 'label': 'Card heading', 'type': 'text', 'default': 'Motivation'},
            {'key': 'lp_motivation_title_show', 'label': 'Show the heading', 'type': 'toggle', 'default': '1'},
            {'key': 'lp_motivation_quote', 'label': 'The quote', 'type': 'lines',
             'default': 'Small steps, taken every day,\nare what finish long journeys.',
             'help': 'Each line is kept on its own line. Quote marks are added by the page.'},
            {'key': 'lp_person3_name', 'label': 'Person 3 — name', 'type': 'text', 'default': '',
             'help': 'Who said it. Leave blank to show the quote on its own.'},
            {'key': 'lp_person3_role', 'label': 'Person 3 — who they are', 'type': 'text', 'default': '',
             'help': 'A few words under the name, e.g. "Author" or "My father".'},
        ],
    },
    {
        'id': 'nav',
        'title': 'Story navigation labels',
        'icon': 'fa-list',
        # One link per section on the page, in page order. A link is only drawn
        # when its section is switched on, so a hidden section cannot leave
        # behind a link that jumps nowhere.
        'fields': [
            {'key': 'lp_nav_intro', 'label': 'Link 1 → Introduction', 'type': 'text', 'default': 'Introduction'},
            {'key': 'lp_nav_story', 'label': 'Link 2 → The story', 'type': 'text', 'default': 'The Journey',
             'help': 'Covers the whole timeline: the challenge, the beginning, the transformation and today.'},
            {'key': 'lp_nav_creator', 'label': 'Link 3 → About the creator', 'type': 'text',
             'default': 'The Creator'},
            {'key': 'lp_nav_support', 'label': 'Link 4 → Support', 'type': 'text', 'default': 'Support'},
            {'key': 'lp_nav_values', 'label': 'Link 5 → What it represents', 'type': 'text', 'default': 'What It Represents'},
            {'key': 'lp_nav_thanks', 'label': 'Link 6 → Thank you', 'type': 'text', 'default': 'Thank You'},
        ],
    },
]

LANDING_FIELDS = [field for section in LANDING_SECTIONS for field in section['fields']]
LANDING_DEFAULTS = {field['key']: field['default'] for field in LANDING_FIELDS}
# Uploaded head-shots, stored the same way as the header logo.
LANDING_PHOTO_KEYS = {'1': 'lp_person1_photo', '2': 'lp_person2_photo', '3': 'lp_person3_photo'}
# Whether each head-shot is shown. Kept beside the upload rather than in the
# text spec above, because it belongs to the photo: "Restore default text"
# leaves both the file and this switch alone. Off falls back to the initials
# badge, so the photo can be hidden without deleting it.
LANDING_PHOTO_SHOW_KEYS = {'1': 'lp_person1_photo_show', '2': 'lp_person2_photo_show',
                           '3': 'lp_person3_photo_show'}

# Inline Markdown handled by the lp_* template filters: **bold**, *italic*,
# `code` and [text](link). Everything is HTML-escaped first, so the rendered
# page can never carry markup (or script) straight through from the editor.
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)\s]+)\)')
_MD_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')
_MD_ITALIC_RE = re.compile(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)')
_MD_CODE_RE = re.compile(r'`([^`]+)`')
_MD_SAFE_HREF_RE = re.compile(r'^(https?://|mailto:|/)', re.IGNORECASE)


def _render_md_inline(text):
    """Escape, then apply the inline Markdown subset."""
    out = (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
               .replace('"', '&quot;'))

    def link(match):
        label, href = match.group(1), match.group(2)
        if not _MD_SAFE_HREF_RE.match(href):
            return label  # drop javascript:/data: and friends, keep the words
        target = '' if href.startswith('mailto:') else ' target="_blank" rel="noopener"'
        return f'<a href="{href}"{target}>{label}</a>'

    out = _MD_CODE_RE.sub(r'<code>\1</code>', out)
    out = _MD_LINK_RE.sub(link, out)
    out = _MD_BOLD_RE.sub(r'<strong>\1</strong>', out)
    out = _MD_ITALIC_RE.sub(r'<em>\1</em>', out)
    return out


def _render_landing_blocks(text):
    """Blank lines split paragraphs, single newlines wrap inside one."""
    blocks = []
    for chunk in re.split(r'\n\s*\n', (text or '').replace('\r\n', '\n')):
        lines = [line.strip() for line in chunk.split('\n') if line.strip()]
        if lines:
            blocks.append('<p>' + '<br>'.join(_render_md_inline(line) for line in lines) + '</p>')
    return '\n'.join(blocks)


def _render_landing_lines(text):
    """Every line kept, joined with <br> - the quote and the support card."""
    lines = [line.strip() for line in (text or '').replace('\r\n', '\n').split('\n') if line.strip()]
    return '<br>'.join(_render_md_inline(line) for line in lines)


@app.template_filter('lp_text')
def _lp_text_filter(value):
    return Markup(_render_md_inline(str(value or '').strip()))


@app.template_filter('lp_blocks')
def _lp_blocks_filter(value):
    return Markup(_render_landing_blocks(str(value or '')))


@app.template_filter('lp_lines')
def _lp_lines_filter(value):
    return Markup(_render_landing_lines(str(value or '')))


# Straight and curly quotes, either kind, single or double.
_LP_QUOTE_EDGES = '"“”‘’\''


@app.template_filter('lp_quote')
def _lp_quote_filter(value):
    """Lines, with any quote marks typed around the whole thing taken off. The
    page draws its own pair, and two sets of them read as a mistake."""
    text = str(value or '').strip().strip(_LP_QUOTE_EDGES).strip()
    return Markup(_render_landing_lines(text))


@app.template_filter('lp_on')
def _lp_on_filter(value):
    """A landing-page show/hide switch. Only a stored '0' turns something off,
    so a missing or unreadable setting leaves the page as it was."""
    return str(value).strip() != '0'


def _landing_initials(name):
    """Fallback avatar text for a person with no photo uploaded."""
    parts = [part for part in re.split(r'\s+', (name or '').strip()) if part]
    if not parts:
        return '?'
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else '')).upper()


def get_landing_content():
    """Every landing-page string, stored value or default, in one DB read."""
    photo_keys = set(LANDING_PHOTO_KEYS.values())
    content = dict(LANDING_DEFAULTS)
    content.update({key: '' for key in photo_keys})
    # A photo that has never been switched off is shown.
    content.update({key: '1' for key in LANDING_PHOTO_SHOW_KEYS.values()})

    try:
        for row in get_all_app_settings():
            key = row['setting_key']
            if key in content:
                content[key] = row['setting_value'] if row['setting_value'] is not None else ''
    except Exception:
        pass  # a settings read failure must never take the public page down

    content['lp_person1_initials'] = _landing_initials(content['lp_person1_name'])
    content['lp_person2_initials'] = _landing_initials(content['lp_person2_name'])
    content['lp_person3_initials'] = _landing_initials(content['lp_person3_name'])
    content['lp_support_card_initials'] = _landing_initials(content['lp_support_card_name'])
    return content


def landing_content_is_custom(content):
    """True once anything has been edited away from the built-in text."""
    if any(content.get(key) for key in LANDING_PHOTO_KEYS.values()):
        return True
    return any((content.get(key) or '') != default for key, default in LANDING_DEFAULTS.items())


# ----------- Cursor glow appearance -----------
CURSOR_GLOW_DEFAULT_COLOR = '#22D3EE'
CURSOR_GLOW_SHAPES = ('circle', 'square', 'diamond', 'star')
CURSOR_EFFECTS = ('glow', 'fluid', 'both', 'none')
GLASS_OPACITY_RANGE = (20, 100)
# Fluid cursor dials: (setting key, low, high, default)
FLUID_RANGES = {
    'fluid_size': (10, 60, 20),
    'fluid_force': (1000, 12000, 6000),
    'fluid_fade': (5, 80, 35),
    'fluid_swirl': (0, 20, 3),
}
FLUID_QUALITIES = (512, 1024, 1440)
CURSOR_GLOW_SIZE_RANGE = (40, 200)
CURSOR_GLOW_OPACITY_RANGE = (20, 100)


def _safe_glow_color(value):
    """Only a plain #rrggbb hex reaches the page's inline style."""
    if isinstance(value, str) and re.fullmatch(r'#[0-9A-Fa-f]{6}', value.strip()):
        return value.strip()
    return CURSOR_GLOW_DEFAULT_COLOR


def _safe_glow_shape(value):
    return value if value in CURSOR_GLOW_SHAPES else 'circle'


def _safe_cursor_effect(value):
    return value if value in CURSOR_EFFECTS else 'both'


def _safe_glow_number(value, low, high, default):
    """Clamp to the allowed range; anything unparseable falls back."""
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


# Make get_remaining_time and site settings available to templates
@app.context_processor
def inject_template_globals():
    from datetime import datetime
    
    # Get site settings
    site_settings = {
        'app_name': get_app_setting('app_name', 'AttendanceIQ'),
        'app_tagline': get_app_setting('app_tagline', 'Intelligent Attendance Automation'),
        # Header brand: 'text' shows the mark + name + tagline, 'image' shows the
        # uploaded logo instead (falls back to text when no logo is stored).
        'brand_mode': get_app_setting('brand_mode', 'text'),
        'brand_logo': get_app_setting('brand_logo', ''),
        # Cursor glow appearance (validated on save, re-checked here so a hand
        # edited setting can never inject CSS into the page).
        # Glass panel opacity, clamped here so a hand-edited row can never put an
        # unusable value into every page's inline style.
        'glass_opacity': _safe_glow_number(get_app_setting('glass_opacity', 82), *GLASS_OPACITY_RANGE, 82),
        'cursor_effect': _safe_cursor_effect(get_app_setting('cursor_effect', 'both')),
        # Fluid cursor tuning, clamped on the way out so the page always gets
        # usable numbers.
        'fluid': {
            key: _safe_glow_number(get_app_setting(key, default), low, high, default)
            for key, (low, high, default) in FLUID_RANGES.items()
        } | {
            'fluid_quality': (lambda q: q if q in FLUID_QUALITIES else 1440)(
                _safe_glow_number(get_app_setting('fluid_quality', 1440), 512, 1440, 1440))
        },
        'cursor_glow_color': _safe_glow_color(get_app_setting('cursor_glow_color', CURSOR_GLOW_DEFAULT_COLOR)),
        'cursor_glow_shape': _safe_glow_shape(get_app_setting('cursor_glow_shape', 'circle')),
        'cursor_glow_size': _safe_glow_number(get_app_setting('cursor_glow_size', 88), *CURSOR_GLOW_SIZE_RANGE, 88),
        'cursor_glow_opacity': _safe_glow_number(get_app_setting('cursor_glow_opacity', 100), *CURSOR_GLOW_OPACITY_RANGE, 100),
        'footer_text': get_app_setting('footer_text', 'Developed By'),
        'footer_link': get_app_setting('footer_link', 'https://www.instagram.com/satendragoswamii/'),
        'footer_author': get_app_setting('footer_author', 'Satendra Goswami'),
        'footer_email': get_app_setting('footer_email', 'satendragoshwamii@gmail.com'),
        'current_year': datetime.now().year,
    }
    
    # Get user permissions from database for real-time updates (not from session cache)
    user_permissions = {}
    if session.get('user_id'):
        try:
            user = get_user(session['user_id'])
            if user:
                user_dict = dict(user) if hasattr(user, 'keys') else user
                user_permissions = {
                    'can_manage_batches': bool(user_dict.get('can_manage_batches', 0)),
                    'can_view_dashboard': bool(user_dict.get('can_view_dashboard', 0)),
                    'can_manage_roles': bool(user_dict.get('can_manage_roles', 0)),
                    'can_access_processed_files': bool(user_dict.get('can_access_processed_files', 0)),
                    'can_use_batch_matching': bool(user_dict.get('can_use_batch_matching', 0)),
                    'can_access_tango': bool(user_dict.get('can_access_tango', 0)),
                }
        except Exception:
            pass
    
    return dict(
        get_remaining_time=get_remaining_time,
        site_settings=site_settings,
        user_perms=user_permissions,
        processing_overlay=get_processing_overlay_config(),
        maintenance=get_maintenance_config()
    )

# Custom Jinja2 filter for formatting datetime strings
@app.template_filter('format_datetime')
def format_datetime_filter(s):
    """Format a datetime string for display and convert to IST."""
    dt = None
    if isinstance(s, str):
        try:
            # Attempt to parse the most common format first
            dt = datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            try:
                # Fallback for other ISO-like formats
                dt = datetime.fromisoformat(s)
            except (ValueError, TypeError):
                return s  # Return original string if parsing fails
    elif isinstance(s, datetime):
        dt = s

    if dt:
        # Convert to IST (UTC+5:30)
        ist_dt = dt + timedelta(hours=5, minutes=30)
        return ist_dt.strftime('%Y-%m-%d %H:%M:%S')
    
    return s

# Directory for temporary files - use dedicated uploads folder instead of system temp
# This prevents OS-level temp cleanup and gives us more control
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

# For backward compatibility, TEMP_DIR points to our uploads directory
TEMP_DIR = UPLOAD_DIR

# Path to main SQLite database (for backup/restore)
DB_PATH = os.path.join(os.path.dirname(__file__), 'attendancify.db')

# When this worker started, for the dashboard's System Health panel
APP_STARTED_AT = time.time()

# Periodic cleanup for temp files created by this app
LAST_CLEANUP = 0

def cleanup_temp_files(max_age_minutes: int = 120):
    """Remove temp files created by this app older than max_age_minutes.
    Files are identified by a 16-hex prefix followed by an underscore.
    This function excludes files that are registered in processed_files table,
    and also excludes files that are not yet eligible for deletion."""
    cutoff = time.time() - max_age_minutes * 60
    try:
        # Get all registered processed files to exclude from cleanup
        registered_files = get_processed_files(include_deleted=False)
        registered_paths = set()
        now = datetime.now()
        
        for f in registered_files:
            file_path = f['file_path']
            # Only exclude if file is not yet eligible for deletion
            if f['eligible_for_deletion_at']:
                try:
                    eligible_dt = datetime.strptime(f['eligible_for_deletion_at'], '%Y-%m-%d %H:%M:%S')
                    # If not yet eligible, exclude from cleanup
                    if eligible_dt > now:
                        registered_paths.add(file_path)
                except Exception:
                    # If we can't parse the date, exclude it to be safe
                    registered_paths.add(file_path)
            else:
                # If no eligibility time set, exclude it (still being processed)
                registered_paths.add(file_path)
        
        for name in os.listdir(TEMP_DIR):
            # Only target files that match our random-prefix pattern "<16hex>_..."
            if '_' in name and len(name.split('_', 1)[0]) == 16:
                path = os.path.join(TEMP_DIR, name)
                if os.path.isfile(path):
                    # Skip if file is registered and not yet eligible for deletion
                    if path in registered_paths:
                        continue
                    try:
                        if os.path.getmtime(path) < cutoff:
                            os.remove(path)
                    except OSError:
                        pass
    except Exception:
        pass

def cleanup_eligible_processed_files():
    """Auto-delete processed files that are eligible for deletion (15 minutes after eligibility time)"""
    try:
        eligible_files = get_files_eligible_for_auto_deletion()
        for file_record in eligible_files:
            file_path = file_record['file_path']
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    mark_file_manually_deleted(file_path)
                except OSError:
                    pass
    except Exception:
        pass

# How long the audit trail is kept. Long enough to investigate something noticed
# months later, short enough that the table does not grow without limit.
AUDIT_RETENTION_DAYS = 365


def cleanup_old_database_records():
    """Permanently remove database records for files deleted more than 30 days ago"""
    try:
        # Delete records older than 30 days (configurable)
        deleted_count = permanently_delete_old_records(days_old=30)
        if deleted_count > 0:
            print(f"Permanently removed {deleted_count} old deleted file records from database")
    except Exception as e:
        print(f"Error cleaning up old database records: {e}")

    try:
        pruned = prune_activity_logs(days=AUDIT_RETENTION_DAYS)
        if pruned:
            print(f"Pruned {pruned} activity log rows older than {AUDIT_RETENTION_DAYS} days")
    except Exception as e:
        print(f"Error pruning activity logs: {e}")

# Track last database cleanup (run less frequently)
LAST_DB_CLEANUP = 0

@app.before_request
def _periodic_cleanup():
    global LAST_CLEANUP, LAST_DB_CLEANUP
    now = time.time()
    # Run file cleanup roughly every 60 seconds
    if now - LAST_CLEANUP > 60:
        cleanup_temp_files()
        cleanup_eligible_processed_files()
        LAST_CLEANUP = now
    
    # Run database cleanup once per day (86400 seconds)
    if now - LAST_DB_CLEANUP > 86400:
        cleanup_old_database_records()
        LAST_DB_CLEANUP = now

# Configure upload settings
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max file size

# Tango (faculty invite generator) mounted as an in-app tool at /tango.
# Reuses this app's own session/login (see tango_access_required in tango.py)
# instead of a separate login, and keeps its data in its own tango.db.
from tango import tango_bp
app.register_blueprint(tango_bp)

# Marker row that records the backfill below has already run.
BATCH_PERM_BACKFILL_KEY = 'migration_batch_manage_backfill'


def backfill_batch_manage_permission():
    """One-time: keep batch access for admins who had it by role alone.

    The batch pages used to open for any admin or subsuperadmin whatever the
    'Can manage batches' switch said. Now the switch decides (see
    batch_manager_required), so without this every existing admin would silently
    lose access the moment this version goes live.

    Guarded by a marker in app_settings so it runs exactly once: after that the
    switch is the superadmin's to set, and a restart will not quietly hand the
    permission back to someone it was taken from.
    """
    try:
        if get_app_setting(BATCH_PERM_BACKFILL_KEY):
            return
        granted = []
        for row in get_all_users(include_hidden=True):
            user = dict(row) if hasattr(row, 'keys') else row
            if user.get('role') in ('admin', 'subsuperadmin') and not user.get('can_manage_batches'):
                update_user(user['email'], can_manage_batches=1)
                granted.append(user['email'])
        set_app_setting(BATCH_PERM_BACKFILL_KEY, '1')
        if granted:
            print(f"Batch permission backfill: kept batch access for {len(granted)} "
                  f"existing admin(s): {', '.join(granted)}")
    except Exception as exc:
        # Never let a migration stop the app from starting; the permission can
        # still be ticked by hand in Edit User.
        print(f"Batch permission backfill skipped: {exc}")


TANGO_ACCESS_BACKFILL_KEY = 'migration_tango_access_backfill'


def backfill_tango_access():
    """One-time: keep Tango for the admins who reached it by role alone.

    Tango used to open for any admin or superadmin. It is now a per-account
    permission that works for any role, so without this every existing admin
    would lose it the moment this version goes live. Its own marker, separate
    from the batch backfill, because that one has already run on live servers.
    """
    try:
        if get_app_setting(TANGO_ACCESS_BACKFILL_KEY):
            return
        granted = []
        for row in get_all_users(include_hidden=True):
            user = dict(row) if hasattr(row, 'keys') else row
            if user.get('role') in ('admin', 'superadmin') and not user.get('can_access_tango'):
                update_user(user['email'], can_access_tango=1)
                granted.append(user['email'])
        set_app_setting(TANGO_ACCESS_BACKFILL_KEY, '1')
        if granted:
            print(f"Tango access backfill: kept access for {len(granted)} "
                  f"existing admin(s): {', '.join(granted)}")
    except Exception as exc:
        print(f"Tango access backfill skipped: {exc}")


def purge_plaintext_passwords():
    """Clear users.password_plain, which used to hold real passwords.

    Naturally idempotent: it only writes where something is still stored, so a
    restart on a clean database does nothing. Nothing in the app reads this
    column for display - password recovery goes through the reset flow.
    """
    try:
        cleared = 0
        for row in get_all_users(include_hidden=True):
            user = dict(row) if hasattr(row, 'keys') else row
            if user.get('password_plain'):
                update_user(user['email'], password_plain=None)
                cleared += 1
        if cleared:
            print(f"Security: cleared stored plaintext passwords for {cleared} account(s).")
    except Exception as exc:
        print(f"Plaintext password purge skipped: {exc}")


def retire_default_superadmin_password():
    """Make the superadmin choose a password if theirs is still the shipped one.

    Their access is untouched - they sign in exactly as before and are then
    asked to set a new password. Doing it this way rather than resetting the
    password outright means nobody is locked out by this upgrade.
    """
    try:
        user = get_user(DEFAULT_SUPERADMIN_EMAIL)
        if not user:
            return
        user_dict = dict(user) if hasattr(user, 'keys') else user
        if user_dict.get('must_change_password'):
            return                      # already being asked
        if verify_password(user_dict.get('password_hash', ''),
                           LEGACY_DEFAULT_SUPERADMIN_PASSWORD):
            update_user(DEFAULT_SUPERADMIN_EMAIL, must_change_password=1)
            print("Security: the superadmin password is still the shipped default. "
                  "A new one must be set at the next login.")
    except Exception as exc:
        print(f"Default superadmin check skipped: {exc}")


# Initialize database and ensure superadmin exists
init_db()
ensure_superadmin_exists()
backfill_batch_manage_permission()
backfill_tango_access()
purge_plaintext_passwords()
# retire_default_superadmin_password() needs verify_password, which is defined
# in the section below; it is called at the end of that section.

# ----------- Authentication Functions -----------
# Passwords are stored as  pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>.
#
# Earlier versions stored a bare, unsalted SHA-256 digest. SHA-256 is built to
# be fast, which is the opposite of what a password needs: commodity hardware
# tries billions of guesses a second against it, and without a salt one rainbow
# table cracks every account at once. PBKDF2 with a per-password salt makes each
# guess cost real work and each account cost its own attack.
#
# Old digests are still accepted at login and rewritten in the new format on the
# way through (see verify_and_upgrade_password), so no one has to reset a
# password for this upgrade.
PBKDF2_ITERATIONS = 260000
_LEGACY_SHA256_RE = re.compile(r'^[0-9a-f]{64}$', re.IGNORECASE)


def hash_password(password, iterations=PBKDF2_ITERATIONS):
    """Hash a password for storing."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def password_is_legacy(stored_password_hash):
    """True for a bare SHA-256 digest written by an earlier version."""
    return bool(_LEGACY_SHA256_RE.match(stored_password_hash or ''))


def verify_password(stored_password_hash, provided_password):
    """Verify a stored password hash against one provided by user"""
    stored = stored_password_hash or ''
    if stored.startswith('pbkdf2_sha256$'):
        try:
            _, iterations, salt_hex, digest_hex = stored.split('$', 3)
            expected = hashlib.pbkdf2_hmac(
                'sha256', provided_password.encode(), bytes.fromhex(salt_hex), int(iterations))
        except (ValueError, TypeError):
            return False
        # compare_digest, not ==, so the comparison cannot be timed to learn
        # how much of the hash was guessed correctly.
        return secrets.compare_digest(expected.hex(), digest_hex)
    return secrets.compare_digest(stored, hashlib.sha256(provided_password.encode()).hexdigest())


def verify_and_upgrade_password(email, stored_password_hash, provided_password):
    """Verify at login, and quietly restore a legacy digest in the new format."""
    if not verify_password(stored_password_hash, provided_password):
        return False
    if password_is_legacy(stored_password_hash):
        try:
            update_user(email, password_hash=hash_password(provided_password))
        except Exception as exc:
            # A failed rewrite must never block a correct password.
            print(f"Password hash upgrade skipped for {email}: {exc}")
    return True


# Runs here rather than beside the other start-up migrations because it needs
# verify_password, which is defined just above.
retire_default_superadmin_password()


def sync_session_role():
    """Copy the user's current role from the database into their session.

    Without this a role change only reaches someone the next time they log in:
    an admin demoted to a normal user keeps 'admin' in their session, is still
    shown as Admin, and - because is_admin() trusts the session - can still open
    the admin pages until they happen to log out. Sessions last 24 hours and are
    extended on every request, so that can be a long time.
    """
    if not (session.get('user_id') and session.get('logged_in')):
        return
    try:
        user = get_user(session['user_id'])
    except Exception:
        return                      # a lookup failure must not break the request
    if not user:
        return
    user_dict = dict(user) if hasattr(user, 'keys') else user
    role = user_dict.get('role', 'user')
    if session.get('role') != role:
        session['role'] = role


# Endpoints that must keep working even while the site is "down", so the
# superadmin can still sign in and switch maintenance back off, static assets
# for the maintenance page load, and the hidden recovery route is reachable.
MAINTENANCE_ALLOWED_ENDPOINTS = {
    'static', 'login', 'logout', 'maintenance', '_hidden_superadmin_reset',
}


@app.before_request
def enforce_maintenance_mode():
    """Show the maintenance page to everyone except superadmin while it is on.

    Registered before check_session_validity so, when maintenance is on, a
    logged-out visitor gets the maintenance page rather than being bounced to
    the login screen by the session check. Superadmin is never gated, so the
    normal login/landing experience is untouched for them and the site can
    always be managed and switched back on.
    """
    try:
        if not maintenance_is_active():
            return
    except Exception:
        # A broken settings read must never wall the whole site off.
        return

    # Let the essentials through (static files, login, logout, the maintenance
    # page itself, hidden recovery).
    if request.endpoint in MAINTENANCE_ALLOWED_ENDPOINTS:
        # A non-superadmin trying to log in during maintenance is allowed to
        # submit, but only superadmin actually gets a session out of it - the
        # login route redirects everyone to '/', which lands back here.
        return

    # Superadmin always passes through untouched.
    try:
        if is_superadmin():
            return
    except Exception:
        pass

    # Everyone else sees the maintenance page in place of whatever they asked
    # for, with a 503 so bots and monitors read it as "temporarily down".
    maintenance = get_maintenance_config()
    return render_template('maintenance.html',
                           maintenance=maintenance,
                           show_navigation=False), 503


@app.before_request
def check_session_validity():
    """Check session validity before each request"""
    # List of endpoints that don't require authentication
    public_endpoints = ['static', 'login', 'logout', 'index', 'maintenance', '_hidden_superadmin_reset']

    # Don't check session for public endpoints
    if request.endpoint in public_endpoints:
        # The home page still reads session.role to build its menu, so a role
        # changed a moment ago has to reach it here too.
        sync_session_role()
        return

    try:
        if 'user_id' in session and 'logged_in' in session:
            # Update last activity
            session['last_activity'] = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
            
            # Get user information from database
            user = get_user(session['user_id'])
            if not user:
                session.clear()
                flash('User account no longer exists.')
                return redirect(url_for('login'))
            
            # Convert to dict for reliable access
            user_dict = dict(user) if hasattr(user, 'keys') else user
            
            # Refresh session permissions from database. The role is refreshed
            # with them: it decides what the menus show and what is_admin() lets
            # through, so a stale one outlives the change an administrator made.
            session['role'] = user_dict.get('role', 'user')
            session['can_manage_roles'] = bool(user_dict.get('can_manage_roles', 0))
            session['can_manage_batch_matching'] = bool(user_dict.get('can_manage_batch_matching', 0))
            session['can_manage_batches'] = bool(user_dict.get('can_manage_batches', 0))
            session['can_access_processed_files'] = bool(user_dict.get('can_access_processed_files', 0))
            session['can_view_dashboard'] = bool(user_dict.get('can_view_dashboard', 0))
            session['can_access_tango'] = bool(user_dict.get('can_access_tango', 0))
            
            # Check if it's superadmin or admin - they always have access (no expiration check)
            user_role = user_dict.get('role', '')
            if user_role in ['superadmin', 'admin']:
                return
                
            # For non-superadmin users, check expiration and password change requirement
            expires_at = make_aware(user_dict.get('expires_at'))
            if expires_at and datetime.now(IST) > expires_at:
                session.clear()
                flash('Account has expired.')
                return redirect(url_for('login'))
                
            # Redirect to force password change if required (except for superadmin)
            if user_dict.get('must_change_password', False) and request.endpoint != 'force_change_password':
                flash('You must change your password before continuing.', 'warning')
                return redirect(url_for('force_change_password'))
                
        elif request.endpoint not in public_endpoints:
            flash('Please log in to access this page.')
            return redirect(url_for('login'))
            
    except Exception as e:
        print(f"Session validity check error: {str(e)}")
        session.clear()
        flash('Session error occurred. Please login again.')
        return redirect(url_for('login'))

def is_logged_in():
    """Check if user is logged in"""
    return 'user_id' in session and ('logged_in' in session and session['logged_in'])

def is_admin():
    """Check if the logged-in user is an admin, subsuperadmin, or superadmin"""
    if not is_logged_in():
        return False
    
    # Nothing about the session or the user row is printed here. This ran on
    # every permission check and wrote the whole session, and then the whole
    # users row - password_hash and password_plain included - into the server
    # log, where it is kept far longer than any credential should be.
    if 'role' in session:
        return session['role'] in ['admin', 'superadmin', 'subsuperadmin']

    # Fallback to database check
    user = get_user(session['user_id'])
    if user:
        if 'role' in user:
            # Update session with role from database
            session['role'] = user['role']
            return user['role'] in ['admin', 'superadmin', 'subsuperadmin']
    
    return False

def is_superadmin():
    """Check if the logged-in user is a superadmin"""
    if not is_logged_in():
        print("is_superadmin: User not logged in")
        return False
    
    # Always check the database to ensure the role is up-to-date
    user = get_user(session['user_id'])
    if not user:
        print("is_superadmin: User not found in database")
        return False
    
    # Convert to dict if it's a Row object
    user_dict = dict(user)
    is_super = user_dict.get('role') == 'superadmin'
    
    # Update session role if needed
    if is_super and session.get('role') != 'superadmin':
        session['role'] = 'superadmin'
        print(f"Updated session role to 'superadmin' for {session['user_id']}")
    
    print(f"is_superadmin check for {session['user_id']}: {is_super}")
    return is_super

def check_token_validity():
    """Check if user's token is still valid"""
    if not is_logged_in():
        return False
    
    user = get_user(session['user_id'])
    if not user:
        return False
    
    # Convert to dict for reliable access
    user_dict = dict(user) if hasattr(user, 'keys') else user
    
    # Superadmin, subsuperadmin, and admin always valid (no expiration)
    if user_dict.get('role') in ['superadmin', 'subsuperadmin', 'admin']:
        return True
    
    # Check if account has expired
    expires_at = make_aware(user_dict.get('expires_at'))
    if expires_at and datetime.now(IST) > expires_at:
        return False
    
    return True

def get_remaining_time(user_id):
    """Get remaining time for a user account"""
    user = get_user(user_id)
    if not user:
        return None
    
    # Convert sqlite3.Row to dict for reliable key access
    user_dict = dict(user) if hasattr(user, 'keys') else user
    
    expires_at_raw = user_dict.get('expires_at')
    if not expires_at_raw:
        return None
    
    expires_at = make_aware(expires_at_raw)
    if not expires_at:
        return None
    
    now = datetime.now(IST)
    
    if expires_at <= now:
        return "Expired"
    
    remaining = expires_at - now
    days = remaining.days
    hours, remainder = divmod(remaining.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    
    if days > 0:
        return f"{days} days, {hours} hours"
    elif hours > 0:
        return f"{hours} hours, {minutes} minutes"
    else:
        return f"{minutes} minutes"

# ----------- Authentication Decorator -----------
def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_logged_in() or not check_token_validity():
            flash('Please log in to access this page.')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_logged_in() or not is_admin() or not check_token_validity():
            flash('Access denied. Admin privileges required.')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def superadmin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_logged_in() or not is_superadmin() or not check_token_validity():
            flash('Access denied. Superadmin privileges required.')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def subsuperadmin_required(f):
    """Allow superadmin or subsuperadmin access"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_logged_in() or not check_token_validity():
            flash('Please log in to access this page.')
            return redirect(url_for('login'))
        
        user = get_user(session.get('user_id'))
        if user:
            user_dict = dict(user) if hasattr(user, 'keys') else user
            if user_dict.get('role') in ['superadmin', 'subsuperadmin']:
                return f(*args, **kwargs)
        
        flash('Access denied. Sub-Superadmin or Superadmin privileges required.')
        return redirect(url_for('login'))
    return decorated_function

def batch_manager_required(f):
    """Superadmin always; everyone else needs the can_manage_batches permission.

    Being an admin is deliberately not enough. The 'Can manage batches' switch in
    Edit User says it controls access to the batch pages, so it has to decide for
    admins and subsuperadmins too - otherwise unticking it does nothing and the
    page stays open to anyone who types the URL. Superadmin is the one exception,
    so the permission can always be granted back.
    """
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_logged_in() or not check_token_validity():
            flash('Please log in to access this page.')
            return redirect(url_for('login'))
        if is_superadmin():
            return f(*args, **kwargs)
        # Read the flag from the database, not the session, so revoking it takes
        # effect on the next request rather than at the user's next login.
        user = get_user(session.get('user_id'))
        if user:
            user_dict = dict(user) if hasattr(user, 'keys') else user
            if user_dict.get('can_manage_batches'):
                return f(*args, **kwargs)
        flash('Access denied. Batch management privileges required.')
        return redirect(url_for('index'))
    return decorated_function

# ----------- Authentication Routes -----------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()  # Normalize email
        password = request.form.get('password', '')
        
        if not email or not password:
            flash('Please enter both email and password.', 'error')
            return render_template('login_advanced.html')

        locked = login_lockout_remaining(email)
        if locked:
            flash(f'Too many failed attempts. Try again in {max(1, locked // 60)} minute(s).', 'error')
            return render_template('login_advanced.html')

        user = get_user(email)

        if not user:
            # Counted like any other failure, so guessing addresses is throttled
            # too, and the reply is the same either way - it must not reveal
            # whether the account exists.
            note_login_failure(email)
            flash('Invalid email or password.', 'error')
            return render_template('login_advanced.html')
        
        # Convert sqlite3.Row to dict if needed
        user_dict = dict(user) if hasattr(user, 'keys') else user
        
        # Verify password (and re-store it hashed properly if it is an old one)
        if not verify_and_upgrade_password(email, user_dict.get('password_hash', ''), password):
            note_login_failure(email)
            flash('Invalid email or password.', 'error')
            return render_template('login_advanced.html')
        clear_login_failures(email)
        
        # Check if account is expired
        expires_at = make_aware(user_dict.get('expires_at'))
        if expires_at and expires_at < datetime.now(IST):
            flash('This account has expired. Please contact an administrator.', 'error')
            return render_template('login_advanced.html')
        
        # Get IP address (get_client_ip already resolves the proxy chain)
        ip_address = get_client_ip()

        # Log login with IP address
        log_user_login(email, ip_address)
        log_activity(
            user_dict.get('email'), 'login',
            f'Signed in successfully from {ip_address}', ip_address
        )

        # Set up session
        session.clear()
        session['user_id'] = user_dict.get('email')
        session['logged_in'] = True
        session['role'] = user_dict.get('role', 'user')
        # Store basic profile info for display
        session['first_name'] = user_dict.get('first_name') or ''
        # Feature/permission flags
        session['can_manage_roles'] = bool(user_dict.get('can_manage_roles', 0))
        session['can_manage_batch_matching'] = bool(user_dict.get('can_manage_batch_matching', 0))
        session['can_manage_batches'] = bool(user_dict.get('can_manage_batches', 0))
        session['can_access_processed_files'] = bool(user_dict.get('can_access_processed_files', 0))
        session['can_view_dashboard'] = bool(user_dict.get('can_view_dashboard', 0))
        expires_at = make_aware(user_dict.get('expires_at'))
        if expires_at:
            session['expires_at_display'] = expires_at.strftime('%Y-%m-%d %H:%M')
        else:
            session['expires_at_display'] = ''
        session.permanent = True
        
        # Who logged in and as what, and nothing else. The session dict this
        # used to print carries the whole permission set and is written to a
        # log file that outlives the session itself.
        print(f"Login: {email} as {user_dict.get('role')}")
        
        # Check if password needs to be changed
        if user_dict.get('must_change_password', False):
            flash('You must change your password on first login.', 'warning')
            return redirect(url_for('force_change_password'))

        flash('Login successful!', 'success')

        # Land directly in Tango if the user picked it on the login screen and has access
        target_app = request.form.get('target_app', 'attendancify')
        if target_app == 'tango' and (user_dict.get('role') == 'superadmin'
                                      or user_dict.get('can_access_tango')):
            return redirect(url_for('tango.tango_home'))

        return redirect(url_for('index'))
    
    return render_template('login_advanced.html')

# Hidden superadmin password recovery. Not linked from any UI. Disabled unless
# SUPERADMIN_RESET_KEY is set in the environment -- with no key configured, this
# always 404s so the endpoint's existence isn't revealed. Once configured, visiting
# the bare URL shows a key-entry form (key is submitted via POST body, not the
# querystring, to avoid it landing in browser history/server logs); the old
# bookmark-style "?key=..." link still works too and skips straight to stage 2.
@app.route('/svc/9f4a71c2', methods=['GET', 'POST'])
def _hidden_superadmin_reset():
    if not SUPERADMIN_RESET_KEY:
        abort(404)

    provided_key = request.values.get('key', '')
    key_valid = bool(provided_key) and secrets.compare_digest(provided_key, SUPERADMIN_RESET_KEY)

    if not key_valid:
        # Only show "incorrect key" after an actual submission attempt, not on first visit.
        key_error = 'Incorrect recovery key.' if (request.method == 'POST' and provided_key) else None
        return render_template('_hidden_superadmin_reset.html', stage='key', error=key_error)

    superadmin_email = 'superadmin@attendancify.com'
    error = None
    success = False

    if request.method == 'POST' and 'new_password' in request.form:
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        if len(new_password) < 8:
            error = 'Password must be at least 8 characters long.'
        elif new_password != confirm_password:
            error = 'Passwords do not match.'
        else:
            update_user(
                superadmin_email,
                password_hash=hash_password(new_password),
                must_change_password=False
            )
            ip_address = get_client_ip()
            log_activity(superadmin_email, 'password_reset', 'Superadmin password reset via hidden recovery endpoint', ip_address)
            success = True

    return render_template('_hidden_superadmin_reset.html', stage='password', key=provided_key, error=error, success=success)

# Debug route - REMOVE IN PRODUCTION or restrict to superadmin only
@app.route('/debug/user')
@login_required
@superadmin_required  # Only superadmin can access debug routes
def debug_user():
    user = get_user(session['user_id'])
    if not user:
        return jsonify({'error': 'User not found'}), 404
        
    user_dict = dict(user)
    return jsonify({
        'email': user_dict.get('email'),
        'role': user_dict.get('role'),
        'session_role': session.get('role'),
        'is_superadmin': is_superadmin(),
        'is_admin': is_admin(),
        'session_data': dict(session)
    })

@app.route('/process_attendance_matching', methods=['POST'])
@login_required
def process_attendance_matching():
    """Process one or more attendance matching pairs.

    Supports:
    - File mode: each pair has its own master + raw file.
    - Batch mode: a single selected batch is matched against multiple raw files.
    - Auto-extract: automatically extract raw data from Attendance Generator output.
    """
    try:
        user_email = session.get('user_id')
        output_format = request.form.get('output_format', 'xlsx')
        pair_count = int(request.form.get('pair_count', 1))
        match_mode = request.form.get('match_mode', 'file')  # 'file' or 'batch'
        auto_extract = request.form.get('auto_extract') == '1'  # Auto-extract from Attendance Generator output
        # User-controlled: only add an "Institute Email" column to the generated file when
        # the student has one on file AND the user has opted in for this run.
        include_institute_email = request.form.get('include_institute_email') == '1'

        output_files = []
        server_files = {
            str(file['id']): file
            for file in _server_files_for_user(user_email, 'attendance_generation')
        }

        # Check if user can use batch matching
        db_user_row = get_user(user_email)
        db_user = dict(db_user_row) if db_user_row and hasattr(db_user_row, 'keys') else db_user_row
        can_use_batch = bool(db_user and (db_user.get('role') == 'superadmin' or db_user.get('can_use_batch_matching')))

        batch = None
        batch_students_data = None

        # If batch mode requested, prepare batch data once
        # Per-file auto-arrangement submits batch_id_0, batch_id_1, ... rather than
        # one global batch_id. In that case each pair is validated below.
        global_batch_id_raw = request.form.get('batch_id')
        if match_mode == 'batch' and can_use_batch and global_batch_id_raw:
            try:
                batch_id = int(global_batch_id_raw)
            except (TypeError, ValueError):
                batch_id = None

            if not batch_id:
                flash('Please select a batch for batch mode matching.', 'error')
                return redirect(url_for('index'))

            batch = get_batch(batch_id)
            if not batch:
                flash('Selected batch was not found.', 'error')
                return redirect(url_for('index'))

            students_rows = get_batch_students(batch_id)
            if not students_rows:
                flash('Selected batch has no students to match against.', 'error')
                return redirect(url_for('index'))

            batch_students_data = [
                {"Email": s['email'], "Participant Name": s['name'], "Institute Email": s['institute_email'] or ''}
                for s in students_rows
            ]

        # Debug logging
        print(f"[Smart Matcher] Processing {pair_count} pairs, auto_extract={auto_extract}, match_mode={match_mode}, can_use_batch={can_use_batch}")
        
        # Process each pair according to mode
        for i in range(pair_count):
            raw_file = request.files.get(f'raw_file_{i}')
            server_file_id = request.form.get(f'server_file_id_{i}', '').strip()
            server_file = server_files.get(server_file_id) if server_file_id else None
            if (not raw_file or not raw_file.filename) and not server_file:
                print(f"[Smart Matcher] Pair {i}: No raw file uploaded, skipping")
                continue

            # Check per-pair master source (for Split View dashboard)
            pair_master_source = request.form.get(f'master_source_{i}', 'file')
            pair_batch_id_raw = request.form.get(f'batch_id_{i}')
            print(f"[Smart Matcher] Pair {i}: master_source={pair_master_source}, batch_id={pair_batch_id_raw}, raw_file={(raw_file.filename if raw_file else server_file['file_name'] if server_file else '')}")
            
            # Determine if this pair uses batch mode
            use_batch_for_pair = False
            pair_batch = None
            pair_batch_students = None
            
            if pair_master_source == 'batch' and can_use_batch:
                # Per-pair batch mode
                try:
                    pair_batch_id = int(pair_batch_id_raw) if pair_batch_id_raw else None
                except (TypeError, ValueError):
                    pair_batch_id = None
                
                print(f"[Smart Matcher] Pair {i}: Using per-pair batch mode, batch_id={pair_batch_id}")
                
                if pair_batch_id:
                    pair_batch = get_batch(pair_batch_id)
                    if pair_batch:
                        students_rows = get_batch_students(pair_batch_id)
                        if students_rows:
                            pair_batch_students = [
                                {"Email": s['email'], "Participant Name": s['name'], "Institute Email": s['institute_email'] or ''}
                                for s in students_rows
                            ]
                            use_batch_for_pair = True
                            print(f"[Smart Matcher] Pair {i}: Batch found with {len(pair_batch_students)} students")
                        else:
                            print(f"[Smart Matcher] Pair {i}: Batch has no students")
                    else:
                        print(f"[Smart Matcher] Pair {i}: Batch not found")
                else:
                    print(f"[Smart Matcher] Pair {i}: No batch_id provided for batch mode")
            elif match_mode == 'batch' and can_use_batch and batch and batch_students_data:
                # Global batch mode
                use_batch_for_pair = True
                pair_batch = batch
                pair_batch_students = batch_students_data

            # ----- Batch mode: use batch students as master -----
            if use_batch_for_pair and pair_batch and pair_batch_students:
                try:
                    suffix = secrets.token_hex(8)
                    raw_filename = secure_filename(raw_file.filename) if raw_file and raw_file.filename else server_file['file_name']
                    raw_path = server_file['file_path'] if server_file else os.path.join(TEMP_DIR, f"{suffix}_{raw_filename}")
                    if raw_file and raw_file.filename:
                        raw_file.save(raw_path)
                    
                    # Auto-extract: run Excel Extractor on the raw file first
                    if auto_extract:
                        try:
                            extracted_df = extract_raw_from_excel(raw_path)
                            extracted_filename = f"{suffix}_extracted_{secure_filename(raw_filename)}"
                            extracted_path = os.path.join(TEMP_DIR, extracted_filename.replace('.xlsx', '.csv').replace('.xls', '.csv'))
                            extracted_df.to_csv(extracted_path, index=False)
                            raw_path = extracted_path  # Use extracted file for matching
                        except Exception as e:
                            flash(f'Auto-extract failed for {raw_filename}: {str(e)}. Using original file.', 'warning')

                    start_time = time.time()
                    output_path = match_and_write_from_students(pair_batch['name'], pair_batch_students, raw_path, output_format, batch_id=pair_batch['id'], include_institute_email=include_institute_email)
                    processing_time = time.time() - start_time

                    if output_path and os.path.exists(output_path):
                        output_filename = os.path.basename(output_path)
                        # Register processed file in database immediately (not eligible for deletion yet)
                        # This protects the file during the entire workflow
                        register_processed_file(
                            file_path=output_path,
                            file_name=output_filename,
                            user_email=user_email,
                            processing_type='attendance_matching',
                            eligible_for_deletion_at=None  # Not eligible yet - will be set after completion
                        )
                        output_files.append({'path': output_path, 'name': output_filename})

                    raw_file_size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
                    log_file_processing(
                        user_email=user_email,
                        file_name=f"batch:{pair_batch['name']}",
                        processing_type='attendance_matching_batch',
                        file_size=raw_file_size,
                        processing_time=processing_time
                    )
                except Exception as e:
                    print(f"Batch mode matching error for pair {i}: {e}")
                    continue

            # ----- File mode: per-pair master + raw -----
            else:
                print(f"[Smart Matcher] Pair {i}: Trying file mode (batch mode not used)")
                master_file = request.files.get(f'master_file_{i}')
                if not master_file or not master_file.filename:
                    print(f"[Smart Matcher] Pair {i}: No master file uploaded for file mode, skipping")
                    continue

                suffix = secrets.token_hex(8)
                master_filename = f"{suffix}_" + secure_filename(master_file.filename)
                raw_filename = secure_filename(raw_file.filename) if raw_file and raw_file.filename else server_file['file_name']
                master_path = os.path.join(TEMP_DIR, master_filename)
                raw_path = server_file['file_path'] if server_file else os.path.join(TEMP_DIR, f"{suffix}_{raw_filename}")
                master_file.save(master_path)
                if raw_file and raw_file.filename:
                    raw_file.save(raw_path)
                
                # Auto-extract: run Excel Extractor on the raw file first
                if auto_extract:
                    try:
                        extracted_df = extract_raw_from_excel(raw_path)
                        extracted_filename = f"{suffix}_extracted_{secure_filename(raw_filename)}"
                        extracted_path = os.path.join(TEMP_DIR, extracted_filename.replace('.xlsx', '.csv').replace('.xls', '.csv'))
                        extracted_df.to_csv(extracted_path, index=False)
                        raw_path = extracted_path  # Use extracted file for matching
                    except Exception as e:
                        flash(f'Auto-extract failed for {raw_filename}: {str(e)}. Using original file.', 'warning')

                start_time = time.time()
                output_path = match_and_write(master_path, raw_path, output_format)
                processing_time = time.time() - start_time

                output_filename = os.path.basename(output_path)
                # Register processed file in database immediately (not eligible for deletion yet)
                # This protects the file during the entire workflow
                register_processed_file(
                    file_path=output_path,
                    file_name=output_filename,
                    user_email=user_email,
                    processing_type='attendance_matching',
                    eligible_for_deletion_at=None  # Not eligible yet - will be set after completion
                )
                output_files.append({'path': output_path, 'name': output_filename})

                master_file_size = os.path.getsize(master_path) if os.path.exists(master_path) else 0
                raw_file_size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
                log_file_processing(
                    user_email=user_email,
                    file_name=master_filename,
                    processing_type='attendance_matching_master',
                    file_size=master_file_size,
                    processing_time=processing_time
                )
                log_file_processing(
                    user_email=user_email,
                    file_name=raw_filename,
                    processing_type='attendance_matching_raw',
                    file_size=raw_file_size,
                    processing_time=processing_time
                )

        if not output_files:
            # Check if request is AJAX
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': f'No valid file pairs were processed. Pairs requested: {pair_count}'}), 400
            flash(f'No valid file pairs were processed. Please ensure each pair has a master source (file or batch) and a raw file.')
            return redirect(url_for('index'))

        # Debug logging
        print(f"Successfully processed {len(output_files)} file pairs")
        print(f"Output files: {output_files}")

        session['matching_output_files'] = output_files
        
        # Mark all files as eligible for deletion after 2 hours from now
        # (Files are already registered above, but we ensure eligibility time is set)
        eligible_time = (datetime.now() + timedelta(minutes=120)).strftime('%Y-%m-%d %H:%M:%S')
        for file_info in output_files:
            mark_file_eligible_for_deletion(file_info['path'], eligible_time)

        # Check if request is AJAX
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            # For AJAX requests, return JSON with redirect URL to Processed Files
            return jsonify({'success': True, 'download_url': url_for('processed_files_management')})

        # Redirect to Processed Files page
        flash(f'Successfully processed {len(output_files)} file(s). Download from below.', 'success')
        return redirect(url_for('processed_files_management'))

    except Exception as e:
        error_message = f'Error processing files: {str(e)}'
        print(f"Error in process_attendance_matching: {error_message}")
        # Check if request is AJAX
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': error_message}), 500
        flash(error_message)
        return redirect(url_for('index'))

@app.route('/force_change_password', methods=['GET', 'POST'])
@login_required
def force_change_password():
    user_row = get_user(session['user_id'])
    user = dict(user_row) if user_row is not None and hasattr(user_row, 'keys') else user_row
    
    # Check if password change is still required
    if not user or not user.get('must_change_password', False):
        # If not required, redirect to index
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        current_password = request.form['current_password']
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']
        
        # Verify current password
        if not user or not verify_password(user['password_hash'], current_password):
            flash('Current password is incorrect.', 'error')
            return render_template('force_change_password.html')
        
        # Check if new passwords match
        if new_password != confirm_password:
            flash('New passwords do not match.', 'error')
            return render_template('force_change_password.html')
        
        # Check password length
        if len(new_password) < 6:
            flash('Password must be at least 6 characters long.', 'error')
            return render_template('force_change_password.html')
        
        # Update password
        # The hash only. A readable copy of the password buys nothing that the
        # reset flow cannot do, and turns any copy of the database into a list
        # of working credentials - which people reuse elsewhere.
        update_user(session['user_id'],
                   password_hash=hash_password(new_password),
                   must_change_password=False)
        
        # Remove force change flag from user data
        flash('Password changed successfully! You can now continue using the application.', 'success')
        return redirect(url_for('index'))
    
    return render_template('force_change_password.html')

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form['current_password']
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']
        
        user = get_user(session['user_id'])
        
        # Verify current password
        if not user or not verify_password(user['password_hash'], current_password):
            flash('Current password is incorrect.')
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': 'Current password is incorrect.'}), 400
            return render_template('change_password.html')
        
        # Check if new passwords match
        if new_password != confirm_password:
            flash('New passwords do not match.')
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': 'New passwords do not match.'}), 400
        
        # Update password
        update_user(session['user_id'], password_hash=hash_password(new_password))
        flash('Password changed successfully.')
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'message': 'Password changed successfully.'})
        return redirect(url_for('index'))
    
    return render_template('change_password.html')

@app.route('/admin/create_user', methods=['GET', 'POST'])
@admin_required
def create_user_route():
    if request.method == 'POST':
        try:
            email = request.form.get('email', '').strip().lower()  # Normalize email
            password = request.form.get('password', '')  # Not stripped: must match verification at login, which also doesn't strip
            role = request.form.get('role', 'user').strip().lower()
            expires_at = request.form.get('expires_at', '').strip()
            first_name = request.form.get('first_name', '').strip() or None
            last_name = request.form.get('last_name', '').strip() or None
            
            # Basic validation
            if not email or '@' not in email:
                flash('Please enter a valid email address.', 'error')
                return render_template('create_user.html', show_navigation=True)
                
            if len(password) < 8:
                flash('Password must be at least 8 characters long.', 'error')
                return render_template('create_user.html', show_navigation=True)
            
            # Role validation
            current_user_role = session.get('role', 'user')
            valid_roles = ['user', 'admin', 'superadmin', 'subsuperadmin']
            if role not in valid_roles:
                flash(f'Invalid role specified. Valid roles are: {", ".join(valid_roles)}', 'error')
                return redirect(url_for('admin_panel'))
            
            if current_user_role == 'admin' and role in ['admin', 'superadmin', 'subsuperadmin']:
                flash('Admins cannot create other admins, superadmins, or subsuperadmins. Only superadmins can create these roles.', 'error')
                return redirect(url_for('admin_panel'))
            
            if current_user_role == 'subsuperadmin' and role in ['superadmin', 'subsuperadmin']:
                flash('Subsuperadmins cannot create superadmins or other subsuperadmins.', 'error')
                return redirect(url_for('admin_panel'))
            
            # Check if user already exists
            if get_user(email):
                flash('A user with this email already exists.', 'error')
                return render_template('create_user.html', show_navigation=True)
            
            # Handle expiration date
            expires_at_datetime = None
            if expires_at:
                try:
                    expires_at_datetime = datetime.strptime(expires_at, '%Y-%m-%dT%H:%M')
                    # Make it timezone-aware for comparison
                    expires_at_datetime = expires_at_datetime.replace(tzinfo=IST)
                    if expires_at_datetime < datetime.now(IST):
                        flash('Expiration date cannot be in the past.', 'error')
                        return render_template('create_user.html', show_navigation=True)
                except ValueError:
                    flash('Invalid expiration date format. Please use the date picker.', 'error')
                    return render_template('create_user.html', show_navigation=True)
            
            # Hash the password
            password_hash = hash_password(password)

            # Create the user, recording who created it
            creator_email = session.get('user_id')
            db_create_user(
                email=email,
                password_hash=password_hash,
                role=role,
                expires_at=expires_at_datetime,
                must_change_password=True,  # Force password change on first login
                first_name=first_name,
                last_name=last_name,
                created_by=creator_email
            )
            
            flash('User created successfully. They will be required to change password on first login.', 'success')
            return redirect(url_for('admin_panel'))
            
        except Exception as e:
            flash(f'Error creating user: {str(e)}', 'error')
            print(f"Error in create_user_route: {str(e)}")
            import traceback
            traceback.print_exc()
            return render_template('create_user.html', show_navigation=True)
    
    # For GET request, show the form
    return render_template('create_user.html', show_navigation=True)

@app.route('/admin')
@admin_required
def admin_panel():
    """Redirect to dashboard which now includes user management"""
    return redirect(url_for('dashboard'))

@app.route('/admin/users')
@admin_required
def admin_panel_legacy():
    """Redirect to dashboard, which now includes user management"""
    return redirect(url_for('dashboard'))


@app.route('/admin/update_user_flags/<user_id>', methods=['POST'])
@superadmin_required
def update_user_flags(user_id):
    """Update per-user feature flags such as can_use_batch_matching (superadmin only)."""
    can_use_batch_matching = 1 if request.form.get('can_use_batch_matching') == '1' else 0
    try:
        update_user(user_id, can_use_batch_matching=can_use_batch_matching)
        flash('User feature flags updated.', 'success')
    except Exception as e:
        flash(f'Error updating user flags: {str(e)}', 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/edit_user/<user_id>', methods=['GET', 'POST'])
@admin_required
def edit_user(user_id):
    user = get_user(user_id)
    if not user:
        flash('User not found.')
        return redirect(url_for('admin_panel'))
    # Ensure we have a plain dict for safe access
    user_dict = dict(user) if hasattr(user, 'keys') else user

    # Enforce that admins can only edit users they created and can never touch superadmins
    current_role = session.get('role')
    current_email = session.get('user_id')
    target_role = user_dict.get('role')
    target_creator = user_dict.get('created_by')

    # Superadmin safety: nobody except superadmin can ever edit a superadmin account
    if target_role == 'superadmin' and current_role != 'superadmin':
        flash('Only superadmin can edit superadmin accounts.')
        return redirect(url_for('admin_panel'))

    # Admins are restricted to editing only users they created
    if current_role == 'admin' and target_creator and target_creator != current_email:
        flash('Admins can only edit users they have created.')
        return redirect(url_for('admin_panel'))
    # Normalize expires_at to a datetime object if it's a string
    expires_at_value = user_dict.get('expires_at') if isinstance(user_dict, dict) else None
    if expires_at_value and isinstance(expires_at_value, str):
        try:
            from datetime import datetime
            user_dict['expires_at'] = datetime.fromisoformat(expires_at_value)
        except (ValueError, TypeError):
            user_dict['expires_at'] = None
    
    if request.method == 'POST':
        # Only admin or superadmin can edit users (already enforced by decorator)
        if session.get('role') not in ['admin', 'superadmin']:
            flash('Only admins can edit users.')
            return redirect(url_for('admin_panel'))

        # Update user data
        update_data = {}
        if 'first_name' in request.form:
            update_data['first_name'] = request.form['first_name'].strip() or None
        if 'last_name' in request.form:
            update_data['last_name'] = request.form['last_name'].strip() or None
        if 'role' in request.form:
            requested_role = request.form['role']
            current_role = session.get('role')
            can_manage_roles_flag = bool(session.get('can_manage_roles'))
            # Only superadmin, or admins explicitly allowed, may change roles
            if current_role == 'admin' and not can_manage_roles_flag:
                flash('You are not allowed to change user roles.')
                return render_template('edit_user.html', user=user_dict, user_id=user_id, show_navigation=True)
            # Admins are not allowed to promote anyone to superadmin or modify superadmin role
            if current_role == 'admin' and requested_role == 'superadmin':
                flash('Admins are not allowed to assign the superadmin role.')
                return render_template('edit_user.html', user=user_dict, user_id=user_id, show_navigation=True)
            # Extra safety: never allow non-superadmin to change an existing superadmin
            if target_role == 'superadmin' and current_role != 'superadmin':
                flash('Only superadmin can change superadmin role.')
                return redirect(url_for('admin_panel'))
            update_data['role'] = requested_role
        
        if 'expires_at' in request.form:
            expires_at = request.form['expires_at']
            update_data['expires_at'] = datetime.strptime(expires_at, '%Y-%m-%dT%H:%M') if expires_at else None
        
        # Handle batch matching permission (only for superadmin or admins explicitly allowed)
        can_manage_batch_flag = bool(session.get('can_manage_batch_matching'))
        if session.get('role') == 'superadmin' or (session.get('role') == 'admin' and can_manage_batch_flag):
            update_data['can_use_batch_matching'] = 1 if 'can_use_batch_matching' in request.form else 0
        
        # Handle email change
        if 'email' in request.form and request.form['email'] != user_id:
            new_email = request.form['email']
            if get_user(new_email):
                flash('Email already exists.')
                return render_template('edit_user.html', user=user_dict, user_id=user_id, show_navigation=True)
            # Create new user with updated email and delete old one
            update_data['email'] = new_email
            db_create_user(
                email=new_email, 
                password_hash=user_dict['password_hash'], 
                role=user_dict['role'], 
                expires_at=user_dict.get('expires_at'), 
                must_change_password=user_dict.get('must_change_password', False),
                first_name=update_data.get('first_name', user_dict.get('first_name')),
                last_name=update_data.get('last_name', user_dict.get('last_name'))
            )
            delete_user(user_id)
            flash('User email updated successfully.')
            return redirect(url_for('edit_user', user_id=new_email))
        
        if session.get('role') == 'superadmin':
            # Superadmin can also update per-admin management flags for this user
            can_manage_roles_val = 1 if request.form.get('can_manage_roles') == '1' else 0
            can_manage_batch_val = 1 if request.form.get('can_manage_batch_matching') == '1' else 0
            can_manage_batches_val = 1 if request.form.get('can_manage_batches') == '1' else 0
            can_access_processed_files_val = 1 if request.form.get('can_access_processed_files') == '1' else 0
            can_view_dashboard_val = 1 if request.form.get('can_view_dashboard') == '1' else 0
            can_access_tango_val = 1 if request.form.get('can_access_tango') == '1' else 0
            update_data['can_manage_roles'] = can_manage_roles_val
            update_data['can_manage_batch_matching'] = can_manage_batch_val
            update_data['can_manage_batches'] = can_manage_batches_val
            update_data['can_access_processed_files'] = can_access_processed_files_val
            update_data['can_view_dashboard'] = can_view_dashboard_val
            update_data['can_access_tango'] = can_access_tango_val

        if update_data:
            update_user(user_id, **update_data)
        
        flash('User updated successfully.')
        return redirect(url_for('admin_panel'))
    
    return render_template('edit_user.html', user=user_dict, user_id=user_id, show_navigation=True)

@app.route('/admin/view_password/<user_id>')
@superadmin_required
def view_user_password(user_id):
    # Feature retired for security. Keep route to avoid 404s for stale clients.
    return jsonify({'success': False, 'message': 'Password viewing is disabled'}), 404

@app.route('/admin/change_password/<user_id>', methods=['POST'])
@admin_required
def admin_change_password(user_id):
    # Only superadmin and admin can change passwords
    if session.get('role') not in ['admin', 'superadmin', 'subsuperadmin']:
        flash('Access denied.')
        return redirect(url_for('admin_panel'))
    
    user = get_user(user_id)
    if not user:
        flash('User not found.')
        return redirect(url_for('admin_panel'))
    
    new_password = request.form.get('new_password')
    if not new_password:
        flash('New password is required.')
        return redirect(url_for('admin_panel'))
    
    # Update password
    update_user(user_id, password_hash=hash_password(new_password))
    
    # Log the activity
    ip_address = get_client_ip()
    log_activity(session.get('user_id'), 'password_change', f'Changed password for user: {user_id}', ip_address)
    
    flash(f'Password for {user_id} changed successfully.')
    return redirect(url_for('admin_panel'))

@app.route('/admin/delete_user/<user_id>', methods=['POST'])
@superadmin_required
def delete_user_route(user_id):
    """Delete a user - Only superadmin can delete users"""
    try:
        # Prevent deleting superadmin accounts
        user = get_user(user_id)
        if not user:
            flash('User not found.', 'error')
            return redirect(url_for('admin_panel'))
        
        user_dict = dict(user) if hasattr(user, 'keys') else user
        if user_dict.get('role') == 'superadmin':
            flash('Cannot delete superadmin accounts.', 'error')
            return redirect(url_for('admin_panel'))
        
        # Log the deletion
        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'user_deletion', f'Deleted user: {user_id}', ip_address)
        
        # Delete the user
        delete_user(user_id)
        flash(f'User {user_id} deleted successfully.', 'success')
        
    except Exception as e:
        flash(f'Error deleting user: {str(e)}', 'error')
        print(f"Error in delete_user_route: {str(e)}")
    
    return redirect(url_for('admin_panel'))

@app.route('/admin/hide_user/<user_id>', methods=['POST'])
@superadmin_required
def hide_user_route(user_id):
    """Hide a user - User can still login but activity hidden from dashboard"""
    try:
        user = get_user(user_id)
        if not user:
            flash('User not found.', 'error')
            return redirect(url_for('admin_panel'))
        
        user_dict = dict(user) if hasattr(user, 'keys') else user
        if user_dict.get('role') == 'superadmin':
            flash('Cannot hide superadmin accounts.', 'error')
            return redirect(url_for('admin_panel'))
        
        # Hide the user
        update_user(user_id, hidden=1)
        
        # Log the action
        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'user_hidden', f'Hidden user: {user_id}', ip_address)
        
        flash(f'User {user_id} hidden successfully. They can still login but their activity is hidden.', 'success')
        
    except Exception as e:
        flash(f'Error hiding user: {str(e)}', 'error')
        print(f"Error in hide_user_route: {str(e)}")
    
    return redirect(url_for('admin_panel'))

@app.route('/admin/unhide_user/<user_id>', methods=['POST'])
@superadmin_required
def unhide_user_route(user_id):
    """Unhide a user - Show their activity again"""
    try:
        user = get_user(user_id)
        if not user:
            flash('User not found.', 'error')
            return redirect(url_for('admin_panel'))
        
        # Protect superadmin accounts from being toggled by mistake
        user_dict = dict(user) if hasattr(user, 'keys') else user
        if user_dict.get('role') == 'superadmin':
            flash('Cannot unhide superadmin accounts via this action.', 'error')
            return redirect(url_for('admin_panel'))
        
        # Unhide the user
        update_user(user_id, hidden=0)
        
        # Log the action
        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'user_unhidden', f'Unhidden user: {user_id}', ip_address)
        
        flash(f'User {user_id} unhidden successfully. Their activity is now visible.', 'success')
        
    except Exception as e:
        flash(f'Error unhiding user: {str(e)}', 'error')
        print(f"Error in unhide_user_route: {str(e)}")
    
    return redirect(url_for('admin_panel'))

@app.route('/admin/view_hidden_users')
@superadmin_required
def view_hidden_users():
    """View all hidden users"""
    try:
        from database import get_db_connection
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE hidden = 1 ORDER BY created_at DESC')
            hidden_users = cursor.fetchall()
            cursor.close()
        finally:
            conn.close()
        
        return render_template(
            'hidden_users.html',
            hidden_users=hidden_users,
            show_navigation=True
        )
    except Exception as e:
        flash(f'Error viewing hidden users: {str(e)}', 'error')
        return redirect(url_for('admin_panel'))

@app.route('/admin/file_processing_stats')
@superadmin_required
def file_processing_stats():
    """Display file processing statistics for all users"""
    # Get processing summary
    processing_summary = get_processing_summary()
    
    # Get detailed logs with IDs for deletion
    detailed_logs = get_processing_logs_with_ids(limit=500)
    
    return render_template('file_processing_stats.html', 
                         processing_summary=processing_summary,
                         detailed_logs=detailed_logs,
                         show_navigation=True)


@app.route('/admin/delete_processing_logs', methods=['POST'])
@superadmin_required
def delete_processing_logs_route():
    """Delete processing logs - by age, specific IDs, or all"""
    action = request.form.get('action')
    user_email = session.get('user_id', 'superadmin')
    ip_address = request.remote_addr
    
    try:
        if action == 'delete_all':
            deleted = clear_all_processing_logs()
            flash(f'Successfully deleted all {deleted} processing logs.', 'success')
            log_activity(user_email, 'processing_logs_clear', f'Cleared all {deleted} processing logs', ip_address)
            
        elif action == 'delete_old':
            days = int(request.form.get('days', 30))
            deleted = delete_processing_logs(days_old=days)
            flash(f'Successfully deleted {deleted} processing logs older than {days} days.', 'success')
            log_activity(user_email, 'processing_logs_delete', f'Deleted {deleted} logs older than {days} days', ip_address)
            
        elif action == 'delete_selected':
            log_ids = request.form.getlist('log_ids')
            if log_ids:
                log_ids = [int(id) for id in log_ids]
                deleted = delete_processing_logs(log_ids=log_ids)
                flash(f'Successfully deleted {deleted} selected processing logs.', 'success')
                log_activity(user_email, 'processing_logs_delete', f'Deleted {deleted} selected logs', ip_address)
            else:
                flash('No logs selected for deletion.', 'warning')
        else:
            flash('Invalid action specified.', 'error')
            
    except Exception as e:
        flash(f'Error deleting processing logs: {str(e)}', 'error')
    
    return redirect(url_for('file_processing_stats'))


def _live_db_path():
    """Where the database file actually lives.

    DB_PATH can be a symlink - the Docker image points it at the mounted data
    volume - and both writing and replacing must land on the real file. Swapping
    the symlink itself would drop the database back into the container's own
    layer, where the next rebuild throws it away.
    """
    return os.path.realpath(DB_PATH)


def _remove_sqlite_sidecars(path):
    """Delete the -wal and -shm files belonging to a SQLite database.

    Opening a WAL-mode database creates them, so even the read-only probe below
    leaves a pair next to whatever it touched. Left behind they are litter at
    best; sitting next to a database they no longer describe, they are how a
    restore turns into "database disk image is malformed".
    """
    for sidecar in (path + '-wal', path + '-shm'):
        try:
            os.remove(sidecar)
        except OSError:
            pass


@app.route('/admin/backup')
@superadmin_required
def download_backup():
    """Download a full SQLite database backup (superadmin only)."""
    if not os.path.exists(DB_PATH):
        flash('Database file not found for backup.', 'error')
        return redirect(url_for('admin_panel'))

    backup_name = f"attendancify_backup_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}.db"

    # Not send_file(DB_PATH): the app runs in WAL mode, so a commit lands in the
    # -wal sidecar first. Copying the bare .db file while another request is
    # mid-write hands back a snapshot that misses those rows, or a torn one.
    # SQLite's online backup API takes a consistent, fully checkpointed copy.
    scratch_dir = tempfile.mkdtemp()
    snapshot_path = os.path.join(scratch_dir, backup_name)
    try:
        source = sqlite3.connect(DB_PATH)
        try:
            destination = sqlite3.connect(snapshot_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        with open(snapshot_path, 'rb') as f:
            snapshot = io.BytesIO(f.read())
    except Exception as e:
        flash(f'Error creating backup: {str(e)}', 'error')
        return redirect(url_for('admin_panel'))
    finally:
        # Held in memory instead of streamed from disk so the copy is gone by
        # the time this returns - a stray database full of user records must not
        # be left lying in a temp directory.
        shutil.rmtree(scratch_dir, ignore_errors=True)

    return send_file(snapshot, as_attachment=True, download_name=backup_name,
                     mimetype='application/octet-stream')


@app.route('/admin/restore', methods=['POST'])
@superadmin_required
def restore_backup():
    """Restore the SQLite database from an uploaded backup (superadmin only)."""
    file = request.files.get('backup_file')
    if not file or not file.filename:
        flash('Please select a backup file to restore.', 'error')
        return redirect(url_for('admin_panel'))

    db_path = _live_db_path()
    # Staged beside the real database, not in the uploads directory. os.replace
    # cannot move a file across filesystems, and under Docker those two
    # directories are separate bind mounts - the old code failed there with
    # "Invalid cross-device link" and left the upload behind.
    staged_path = f"{db_path}.restore-{secrets.token_hex(8)}"

    try:
        file.save(staged_path)

        # A valid header is not enough: a truncated or unrelated SQLite file
        # would pass it and then break every page after the swap.
        try:
            probe = sqlite3.connect(staged_path)
            try:
                if probe.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('the file is a damaged SQLite database')
                tables = {row[0] for row in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            finally:
                probe.close()
                # Before the rename, while they still belong to this file.
                _remove_sqlite_sidecars(staged_path)
        except sqlite3.DatabaseError:
            raise ValueError('the file is not a SQLite database')

        missing = {'users', 'batches'} - tables
        if missing:
            raise ValueError(
                'it is not an Attendancify backup (missing the '
                + ', '.join(sorted(missing)) + ' table)'
            )

        # Keep the outgoing database recoverable if the swap goes wrong.
        previous_path = f"{db_path}.replaced-{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}"
        if os.path.exists(db_path):
            # Fold the write-ahead log into the file first, so the copy left
            # behind is complete on its own.
            try:
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                finally:
                    conn.close()
            except Exception:
                pass
            shutil.copy2(db_path, previous_path)

        os.replace(staged_path, db_path)

        # The old sidecars describe the database that was just thrown away.
        _remove_sqlite_sidecars(db_path)
        # And any the probe recreated after the cleanup above, now orphaned by
        # the rename.
        _remove_sqlite_sidecars(staged_path)

        flash(
            'Database restored successfully from backup. The previous database was '
            f'kept as {os.path.basename(previous_path)}.', 'success'
        )
    except Exception as e:
        if os.path.exists(staged_path):
            try:
                os.remove(staged_path)
            except OSError:
                pass
        _remove_sqlite_sidecars(staged_path)
        flash(f'Error restoring backup: {str(e)}', 'error')
    return redirect(url_for('admin_panel'))


# ----------- Batch Management (Admin, Superadmin, or users with can_manage_batches) -----------

def _parse_id_list(raw):
    """Turn a "3,7,12" form field into a de-duplicated list of ints.

    Unparseable entries are skipped rather than aborting the whole request:
    one stray value should not stop the other selected rows from being acted on.
    """
    ids = []
    for token in str(raw or '').split(','):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except (TypeError, ValueError):
            continue
        if value not in ids:
            ids.append(value)
    return ids


@app.route('/admin/batches')
@batch_manager_required
def manage_batches():
    """List all batches and optionally show a selected batch's students."""
    user_email = session.get('user_id')
    user_role = session.get('role', 'user')
    
    # Get active and completed batches based on user's group access
    batches = [dict(batch) for batch in get_batches_for_user(user_email, user_role, is_completed=0)]
    completed_batches = [dict(batch) for batch in get_batches_for_user(user_email, user_role, is_completed=1)]
    for batch in batches + completed_batches:
        if not batch.get('required_duration_minutes'):
            batch['required_duration_minutes'] = _required_minutes_for_batch_label(batch['name'])
    
    # Get groups and subgroups based on user's access
    groups = get_groups_for_user(user_email, user_role)
    subgroups = get_subgroups_for_user(user_email, user_role)
    
    # Get ungrouped batches (active and completed)
    ungrouped_batches = get_ungrouped_batches(is_completed=0)
    ungrouped_completed_batches = get_ungrouped_batches(is_completed=1)
    
    # Get batch counts by group (active and completed)
    group_batch_counts = get_batch_count_by_group(is_completed=0)
    group_completed_batch_counts = get_batch_count_by_group(is_completed=1)
    
    batch_counts = {}
    batch_students = {}
    for b in (batches + completed_batches):
        students = get_batch_students(b['id'])
        batch_counts[b['id']] = len(students)
        batch_students[b['id']] = students

    # Determine which batch should be selected (if any)
    selected_batch = None
    selected_id = request.args.get('batch_id', type=int)
    if selected_id:
        for b in (batches + completed_batches):
            if b['id'] == selected_id:
                selected_batch = b
                break
    
    # Get all users for group assignment (only for admin/superadmin)
    all_users = []
    if user_role in ['superadmin', 'admin']:
        all_users = get_all_users()

    return render_template(
        'batches.html',
        batches=batches,
        completed_batches=completed_batches,
        groups=groups,
        subgroups=subgroups,
        ungrouped_batches=ungrouped_batches,
        ungrouped_completed_batches=ungrouped_completed_batches,
        group_batch_counts=group_batch_counts,
        group_completed_batch_counts=group_completed_batch_counts,
        batch_counts=batch_counts,
        batch_students=batch_students,
        selected_batch=selected_batch,
        all_users=all_users,
        show_navigation=True
    )

@app.route('/admin/batches/<int:batch_id>/details')
@batch_manager_required
def batch_details_route(batch_id):
    """Return the HTML partial for a single batch's details."""
    user_email = session.get('user_id')
    user_role = session.get('role', 'user')
    
    # Verify user has access to this batch
    batch_info = get_batch(batch_id)
    if not batch_info:
        return "Batch not found", 404
        
    # Get students for this batch
    students = get_batch_students(batch_id)

    # Learned Zoom-name -> roster-name matches for this batch (from Final Excel Generator
    # uploads and Smart Matcher batch-mode runs)
    batch_records = get_batch_records(batch_id)

    # We also need groups and subgroups for the 'Move to Group' dropdown
    groups = get_groups_for_user(user_email, user_role)
    subgroups = get_subgroups_for_user(user_email, user_role)

    return render_template(
        '_batch_details_partial.html',
        b=batch_info,
        students=students,
        batch_records=batch_records,
        groups=groups,
        subgroups=subgroups
    )


# ----------- Batch Group Management -----------

@app.route('/admin/batch_groups/create', methods=['POST'])
@batch_manager_required
def create_batch_group_route():
    """Create a new batch group"""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip() or None
    
    if not name:
        flash('Group name is required.', 'error')
        return redirect(url_for('manage_batches'))
    
    try:
        create_batch_group(name, description, session.get('user_id', 'superadmin'))
        flash(f'Group "{name}" created successfully.', 'success')
    except Exception as e:
        if 'UNIQUE' in str(e):
            flash(f'A group with name "{name}" already exists.', 'error')
        else:
            flash(f'Error creating group: {str(e)}', 'error')
    
    return redirect(url_for('manage_batches'))


@app.route('/admin/batch_groups/<int:group_id>/edit', methods=['POST'])
@batch_manager_required
def edit_batch_group_route(group_id):
    """Edit a batch group"""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip() or None
    
    if not name:
        flash('Group name is required.', 'error')
        return redirect(url_for('manage_batches'))
    
    try:
        update_batch_group(group_id, name=name, description=description)
        flash(f'Group updated successfully.', 'success')
    except Exception as e:
        flash(f'Error updating group: {str(e)}', 'error')
    
    return redirect(url_for('manage_batches'))


@app.route('/admin/batch_groups/<int:group_id>/delete', methods=['POST'])
@batch_manager_required
def delete_batch_group_route(group_id):
    """Delete a batch group"""
    try:
        group = get_batch_group(group_id)
        group_name = group['name'] if group else 'Unknown'
        delete_batch_group(group_id)
        flash(f'Group "{group_name}" deleted. Batches in this group are now ungrouped.', 'success')
    except Exception as e:
        flash(f'Error deleting group: {str(e)}', 'error')
    
    return redirect(url_for('manage_batches'))


@app.route('/admin/batch_groups/<int:group_id>/assign_batch', methods=['POST'])
@batch_manager_required
def assign_batch_to_group_route(group_id):
    """Assign a batch to a group"""
    batch_id = request.form.get('batch_id', type=int)
    
    if not batch_id:
        flash('Please select a batch.', 'error')
        return redirect(url_for('manage_batches'))
    
    try:
        assign_batch_to_group(batch_id, group_id)
        flash('Batch assigned to group successfully.', 'success')
    except Exception as e:
        flash(f'Error assigning batch: {str(e)}', 'error')
    
    return redirect(url_for('manage_batches'))


@app.route('/admin/batches/<int:batch_id>/remove_from_group', methods=['POST'])
@batch_manager_required
def remove_batch_from_group_route(batch_id):
    """Remove a batch from its group"""
    try:
        remove_batch_from_group(batch_id)
        # Also remove from subgroup if any
        remove_batch_from_subgroup(batch_id)
        flash('Batch removed from group.', 'success')
    except Exception as e:
        flash(f'Error removing batch from group: {str(e)}', 'error')
    
    return redirect(url_for('manage_batches'))


@app.route('/admin/batches/<int:batch_id>/move_to_group', methods=['POST'])
@batch_manager_required
def move_batch_to_group_route(batch_id):
    """Move a batch to a different group"""
    group_id = request.form.get('group_id', type=int)
    
    try:
        if group_id and group_id > 0:
            assign_batch_to_group(batch_id, group_id)
            flash('Batch moved to group successfully.', 'success')
        else:
            remove_batch_from_group(batch_id)
            # Also remove from subgroup
            remove_batch_from_subgroup(batch_id)
            flash('Batch removed from group.', 'success')
    except Exception as e:
        flash(f'Error moving batch: {str(e)}', 'error')
    
    return redirect(url_for('manage_batches'))


@app.route('/admin/batches/bulk_move_to_group', methods=['POST'])
@batch_manager_required
def bulk_move_batches_to_group():
    """Move multiple batches to a group, a sub-group, or back to ungrouped."""
    destination = request.form.get('destination', '').strip()
    batch_id_list = _parse_id_list(request.form.get('batch_ids', ''))

    if not batch_id_list:
        flash('No batches selected.', 'warning')
        return redirect(url_for('manage_batches'))

    try:
        if destination.startswith('subgroup-'):
            subgroup_id = int(destination.split('-', 1)[1])
            subgroup = get_batch_subgroup(subgroup_id)
            if not subgroup:
                flash('Selected sub-group no longer exists.', 'error')
                return redirect(url_for('manage_batches'))
            for batch_id in batch_id_list:
                assign_batch_to_subgroup(batch_id, subgroup_id)
            flash(f'Successfully moved {len(batch_id_list)} batch(es) to sub-group "{subgroup["name"]}".', 'success')

        elif destination.startswith('group-'):
            group_id = int(destination.split('-', 1)[1])
            for batch_id in batch_id_list:
                # Moving to a plain group clears any existing sub-group membership,
                # since a batch's sub-group must belong to its current group.
                remove_batch_from_subgroup(batch_id)
                assign_batch_to_group(batch_id, group_id)
            flash(f'Successfully moved {len(batch_id_list)} batch(es) to group.', 'success')

        else:
            for batch_id in batch_id_list:
                remove_batch_from_subgroup(batch_id)
                remove_batch_from_group(batch_id)
            flash(f'Successfully removed {len(batch_id_list)} batch(es) from groups.', 'success')

    except Exception as e:
        flash(f'Error moving batches: {str(e)}', 'error')

    return redirect(url_for('manage_batches'))


# ----------- Batch Sub-Group Management -----------

@app.route('/admin/subgroups/create', methods=['POST'])
@batch_manager_required
def create_batch_subgroup_route():
    """Create a new batch subgroup"""
    name = request.form.get('name', '').strip()
    group_id = request.form.get('group_id', type=int)
    description = request.form.get('description', '').strip() or None
    
    if not name or not group_id:
        flash('Sub-group name and parent Group are required.', 'error')
        return redirect(url_for('manage_batches'))
    
    try:
        create_batch_subgroup(name, description, group_id, session.get('user_id', 'superadmin'))
        flash(f'Sub-group "{name}" created successfully.', 'success')
    except Exception as e:
        if 'UNIQUE' in str(e):
            flash(f'A sub-group with name "{name}" already exists in this group.', 'error')
        else:
            flash(f'Error creating sub-group: {str(e)}', 'error')
    
    return redirect(url_for('manage_batches'))


@app.route('/admin/subgroups/<int:subgroup_id>/edit', methods=['POST'])
@batch_manager_required
def edit_batch_subgroup_route(subgroup_id):
    """Edit a batch subgroup"""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip() or None
    
    if not name:
        flash('Sub-group name is required.', 'error')
        return redirect(url_for('manage_batches'))
    
    try:
        update_batch_subgroup(subgroup_id, name=name, description=description)
        flash(f'Sub-group updated successfully.', 'success')
    except Exception as e:
        flash(f'Error updating sub-group: {str(e)}', 'error')
    
    return redirect(url_for('manage_batches'))


@app.route('/admin/subgroups/<int:subgroup_id>/delete', methods=['POST'])
@batch_manager_required
def delete_batch_subgroup_route(subgroup_id):
    """Delete a batch subgroup"""
    try:
        subgroup = get_batch_subgroup(subgroup_id)
        subgroup_name = subgroup['name'] if subgroup else 'Unknown'
        delete_batch_subgroup(subgroup_id)
        flash(f'Sub-group "{subgroup_name}" deleted. Batches are now unassigned from the sub-group.', 'success')
    except Exception as e:
        flash(f'Error deleting sub-group: {str(e)}', 'error')

    return redirect(url_for('manage_batches'))


@app.route('/admin/subgroups/<int:subgroup_id>/complete', methods=['POST'])
@batch_manager_required
def complete_batch_subgroup_route(subgroup_id):
    """Mark a sub-group as completed so it (and its batches) no longer appear in Smart Matcher"""
    try:
        update_batch_subgroup(subgroup_id, is_completed=1)
        flash('Sub-group marked as completed and excluded from Smart Matcher.', 'success')
    except Exception as e:
        flash(f'Error completing sub-group: {str(e)}', 'error')
    return redirect(url_for('manage_batches'))


@app.route('/admin/subgroups/<int:subgroup_id>/restore', methods=['POST'])
@batch_manager_required
def restore_batch_subgroup_route(subgroup_id):
    """Restore a completed sub-group back to active status"""
    try:
        update_batch_subgroup(subgroup_id, is_completed=0)
        flash('Sub-group restored to active status.', 'success')
    except Exception as e:
        flash(f'Error restoring sub-group: {str(e)}', 'error')
    return redirect(url_for('manage_batches'))


# ----------- Batch Status (Complete / Restore) & Subgroup Movement -----------

@app.route('/admin/batches/<int:batch_id>/complete', methods=['POST'])
@batch_manager_required
def complete_batch_route(batch_id):
    """Archive a batch as completed"""
    try:
        update_batch(batch_id, is_completed=1)
        flash('Batch marked as completed.', 'success')
    except Exception as e:
        flash(f'Error completing batch: {str(e)}', 'error')
    return redirect(url_for('manage_batches'))


@app.route('/admin/batches/bulk_complete', methods=['POST'])
@batch_manager_required
def bulk_complete_batches():
    """Archive multiple batches as completed"""
    batch_id_list = _parse_id_list(request.form.get('batch_ids', ''))
    if not batch_id_list:
        flash('No batches selected.', 'warning')
        return redirect(url_for('manage_batches'))
    try:
        for batch_id in batch_id_list:
            update_batch(batch_id, is_completed=1)
        flash(f'Successfully marked {len(batch_id_list)} batch(es) as completed.', 'success')
    except Exception as e:
        flash(f'Error completing batches: {str(e)}', 'error')
    return redirect(url_for('manage_batches'))


@app.route('/admin/batches/<int:batch_id>/restore', methods=['POST'])
@batch_manager_required
def restore_batch_route(batch_id):
    """Restore a completed batch to active status"""
    try:
        update_batch(batch_id, is_completed=0)
        flash('Batch restored to active status.', 'success')
    except Exception as e:
        flash(f'Error restoring batch: {str(e)}', 'error')
    return redirect(url_for('manage_batches'))


@app.route('/admin/batches/bulk_restore', methods=['POST'])
@batch_manager_required
def bulk_restore_batches():
    """Restore multiple completed batches to active status"""
    batch_id_list = _parse_id_list(request.form.get('batch_ids', ''))
    if not batch_id_list:
        flash('No batches selected.', 'warning')
        return redirect(url_for('manage_batches'))
    try:
        for batch_id in batch_id_list:
            update_batch(batch_id, is_completed=0)
        flash(f'Successfully restored {len(batch_id_list)} batch(es) to active status.', 'success')
    except Exception as e:
        flash(f'Error restoring batches: {str(e)}', 'error')
    return redirect(url_for('manage_batches'))


@app.route('/admin/batches/bulk_set_duration', methods=['POST'])
@batch_manager_required
def bulk_set_duration_batches():
    """Set the Required Duration (minutes) on multiple batches at once."""
    batch_id_list = _parse_id_list(request.form.get('batch_ids', ''))
    if not batch_id_list:
        flash('No batches selected.', 'warning')
        return redirect(url_for('manage_batches'))

    duration_raw = request.form.get('required_duration_minutes', '').strip()
    try:
        required_duration_minutes = float(duration_raw)
        if required_duration_minutes <= 0:
            raise ValueError
    except ValueError:
        flash('Required duration must be a positive number of minutes.', 'error')
        return redirect(url_for('manage_batches'))

    try:
        for batch_id in batch_id_list:
            update_batch(batch_id, required_duration_minutes=required_duration_minutes)
        flash(f'Set required duration to {required_duration_minutes:g} minute(s) for {len(batch_id_list)} batch(es).', 'success')
    except Exception as e:
        flash(f'Error setting duration: {str(e)}', 'error')
    return redirect(url_for('manage_batches'))


@app.route('/admin/batches/<int:batch_id>/move_to_subgroup', methods=['POST'])
@batch_manager_required
def move_batch_to_subgroup_route(batch_id):
    """Move a batch to a subgroup"""
    subgroup_id = request.form.get('subgroup_id', type=int)
    try:
        assign_batch_to_subgroup(batch_id, subgroup_id)
        if subgroup_id and subgroup_id > 0:
            flash('Batch moved to sub-group successfully.', 'success')
        else:
            flash('Batch removed from sub-group.', 'success')
    except Exception as e:
        flash(f'Error moving batch: {str(e)}', 'error')
    return redirect(url_for('manage_batches'))


# ----------- User Group Access Management -----------

@app.route('/admin/batch_groups/<int:group_id>/users', methods=['GET'])
@superadmin_required
def get_group_users_route(group_id):
    """Get users who have access to a group (AJAX)"""
    try:
        users = get_group_users(group_id)
        return jsonify({
            'success': True,
            'users': [dict(u) for u in users]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/batch_groups/<int:group_id>/grant_access', methods=['POST'])
@superadmin_required
def grant_group_access_route(group_id):
    """Grant a user access to a group"""
    user_email = request.form.get('user_email', '').strip()
    
    if not user_email:
        flash('Please select a user.', 'error')
        return redirect(url_for('manage_batches'))
    
    try:
        grant_user_group_access(user_email, group_id, session.get('user_id', 'superadmin'))
        flash(f'Access granted to {user_email}.', 'success')
    except Exception as e:
        flash(f'Error granting access: {str(e)}', 'error')
    
    return redirect(url_for('manage_batches'))


@app.route('/admin/batch_groups/<int:group_id>/revoke_access', methods=['POST'])
@superadmin_required
def revoke_group_access_route(group_id):
    """Revoke a user's access to a group"""
    user_email = request.form.get('user_email', '').strip()
    
    if not user_email:
        flash('Please select a user.', 'error')
        return redirect(url_for('manage_batches'))
    
    try:
        revoke_user_group_access(user_email, group_id)
        flash(f'Access revoked for {user_email}.', 'success')
    except Exception as e:
        flash(f'Error revoking access: {str(e)}', 'error')
    
    return redirect(url_for('manage_batches'))


@app.route('/admin/user_groups/<path:user_email>', methods=['GET'])
@superadmin_required
def get_user_groups_route(user_email):
    """Get all groups a user has access to (AJAX)"""
    try:
        user_groups = get_user_group_access(user_email)
        group_ids = [g['id'] for g in user_groups]
        return jsonify({
            'success': True,
            'group_ids': group_ids
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/user_groups/update', methods=['POST'])
@superadmin_required
def update_user_groups_route():
    """Update a user's group access - assign multiple groups at once"""
    user_email = request.form.get('user_email', '').strip()
    group_ids = request.form.getlist('group_ids')
    
    if not user_email:
        flash('Please select a user.', 'error')
        return redirect(url_for('manage_batches'))
    
    try:
        # Get current group access
        current_groups = get_user_group_access(user_email)
        current_group_ids = set(g['id'] for g in current_groups)
        new_group_ids = set(int(gid) for gid in group_ids if gid)
        
        granted_by = session.get('user_id', 'superadmin')
        
        # Revoke access from groups that are no longer selected
        for gid in current_group_ids - new_group_ids:
            revoke_user_group_access(user_email, gid)
        
        # Grant access to newly selected groups
        for gid in new_group_ids - current_group_ids:
            grant_user_group_access(user_email, gid, granted_by)
        
        flash(f'Group access updated for {user_email}. Assigned to {len(new_group_ids)} group(s).', 'success')
    except Exception as e:
        flash(f'Error updating group access: {str(e)}', 'error')
    
    return redirect(url_for('manage_batches'))


@app.route('/admin/batches/create', methods=['GET', 'POST'])
@batch_manager_required
def create_batch_route():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip() or None
        duration_raw = request.form.get('required_duration_minutes', '').strip()
        try:
            required_duration_minutes = float(duration_raw) if duration_raw else None
            if required_duration_minutes is not None and required_duration_minutes <= 0:
                raise ValueError
        except ValueError:
            flash('Required duration must be a positive number of minutes.', 'error')
            return redirect(url_for('create_batch_route'))
        if not name:
            flash('Batch name is required.', 'error')
            return redirect(url_for('create_batch_route'))
        try:
            create_batch(name, description, session.get('user_id', 'superadmin@attendancify.com'), required_duration_minutes=required_duration_minutes)
            flash('Batch created successfully.', 'success')
            return redirect(url_for('manage_batches'))
        except Exception as e:
            flash(f'Error creating batch: {str(e)}', 'error')
            return redirect(url_for('create_batch_route'))
    return render_template('simple_form.html',
                           title='Create Batch',
                           form_action=url_for('create_batch_route'),
                           fields=[
                               {'name': 'name', 'label': 'Batch Name', 'type': 'text', 'required': True},
                               {'name': 'description', 'label': 'Description', 'type': 'text', 'required': False},
                               {'name': 'required_duration_minutes', 'label': 'Required Duration (minutes)', 'type': 'number', 'required': False},
                           ],
                           show_navigation=True)


@app.route('/admin/batches/<int:batch_id>/edit', methods=['GET', 'POST'])
@batch_manager_required
def edit_batch_route(batch_id):
    batch = get_batch(batch_id)
    if not batch:
        flash('Batch not found.', 'error')
        return redirect(url_for('manage_batches'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip() or None
        duration_raw = request.form.get('required_duration_minutes', '').strip()
        try:
            required_duration_minutes = float(duration_raw) if duration_raw else None
            if required_duration_minutes is not None and required_duration_minutes <= 0:
                raise ValueError
        except ValueError:
            flash('Required duration must be a positive number of minutes.', 'error')
            return redirect(url_for('edit_batch_route', batch_id=batch_id))
        if not name:
            flash('Batch name is required.', 'error')
            return redirect(url_for('edit_batch_route', batch_id=batch_id))
        try:
            update_batch(batch_id, name=name, description=description, required_duration_minutes=required_duration_minutes)
            flash('Batch updated successfully.', 'success')
        except Exception as e:
            flash(f'Error updating batch: {str(e)}', 'error')
        return redirect(url_for('manage_batches', batch_id=batch_id))
    return render_template('simple_form.html',
                           title='Edit Batch',
                           form_action=url_for('edit_batch_route', batch_id=batch_id),
                           fields=[
                               {'name': 'name', 'label': 'Batch Name', 'type': 'text', 'required': True, 'value': batch['name']},
                               {'name': 'description', 'label': 'Description', 'type': 'text', 'required': False, 'value': batch['description']},
                               {'name': 'required_duration_minutes', 'label': 'Required Duration (minutes)', 'type': 'number', 'required': False, 'value': batch['required_duration_minutes'] if batch['required_duration_minutes'] is not None else _required_minutes_for_batch_label(batch['name'])},
                           ],
                           show_navigation=True)


@app.route('/admin/batches/<int:batch_id>/delete', methods=['POST'])
@batch_manager_required
def delete_batch_route(batch_id):
    try:
        delete_batch(batch_id)
        flash('Batch deleted successfully.', 'success')
    except Exception as e:
        flash(f'Error deleting batch: {str(e)}', 'error')
    return redirect(url_for('manage_batches'))

@app.route('/admin/batches/delete_selected', methods=['POST'])
@batch_manager_required
def delete_selected_batches_route():
    batch_ids = _parse_id_list(request.form.get('batch_ids', ''))

    if not batch_ids:
        flash('No batches selected for deletion.', 'warning')
        return redirect(url_for('manage_batches'))

    deleted_count = 0
    for batch_id in batch_ids:
        try:
            if get_batch(batch_id):
                delete_batch(batch_id)
                deleted_count += 1
        except Exception as e:
            flash(f'Error deleting batch {batch_id}: {str(e)}', 'error')

    if deleted_count:
        flash(f'Deleted {deleted_count} batch(es) successfully.', 'success')
    return redirect(url_for('manage_batches'))


@app.route('/admin/batches/<int:batch_id>/upload_students', methods=['POST'])
@batch_manager_required
def upload_batch_students(batch_id):
    batch = get_batch(batch_id)
    if not batch:
        flash('Batch not found.', 'error')
        return redirect(url_for('manage_batches'))
    file = request.files.get('students_file')
    replace = request.form.get('replace') == '1'
    if not file or not file.filename:
        flash('Please select a file to upload.', 'error')
        return redirect(url_for('manage_batches', batch_id=batch_id))
    try:
        # Read CSV or Excel into DataFrame
        filename = secure_filename(file.filename)
        temp_path = os.path.join(TEMP_DIR, f"{secrets.token_hex(8)}_{filename}")
        file.save(temp_path)
        if temp_path.lower().endswith(('.xlsx', '.xls')):
            df = pd.read_excel(temp_path)
        else:
            df = pd.read_csv(temp_path)
        df.columns = df.columns.str.strip()
        if 'Name' not in df.columns or 'Email' not in df.columns:
            flash('Upload file must contain columns "Name" and "Email".', 'error')
            return redirect(url_for('manage_batches', batch_id=batch_id))
        has_institute_email = 'InstituteEmail' in df.columns
        has_contact_number = 'ContactNumber' in df.columns
        if replace:
            # Delete existing students
            existing = get_batch_students(batch_id)
            for s in existing:
                delete_batch_student(s['id'])
        # Insert students
        for _, row in df.iterrows():
            name = str(row['Name']).strip()
            email = str(row['Email']).strip().lower()
            if not name or not email:
                continue
            institute_email = str(row['InstituteEmail']).strip() if has_institute_email and pd.notna(row['InstituteEmail']) else None
            contact_number = str(row['ContactNumber']).strip() if has_contact_number and pd.notna(row['ContactNumber']) else None
            add_student_to_batch(batch_id, name, email, institute_email=institute_email, contact_number=contact_number)
        flash('Students uploaded successfully.', 'success')
    except Exception as e:
        flash(f'Error uploading students: {str(e)}', 'error')
    return redirect(url_for('manage_batches', batch_id=batch_id))


@app.route('/admin/batches/<int:batch_id>/students/<int:student_id>/delete', methods=['POST'])
@batch_manager_required
def delete_batch_student_route(batch_id, student_id):
    try:
        delete_batch_student(student_id)
        flash('Student deleted from batch.', 'success')
    except Exception as e:
        flash(f'Error deleting student: {str(e)}', 'error')
    return redirect(url_for('manage_batches', batch_id=batch_id))


@app.route('/admin/batches/<int:batch_id>/students/delete_selected', methods=['POST'])
@batch_manager_required
def delete_selected_batch_students_route(batch_id):
    """Delete several of a batch's students in one request."""
    student_ids = _parse_id_list(request.form.get('student_ids', ''))
    if not student_ids:
        flash('No students selected for deletion.', 'warning')
        return redirect(url_for('manage_batches', batch_id=batch_id))

    # Only delete rows that belong to this batch, so a tampered id list cannot
    # reach into another batch.
    allowed = {s['id'] for s in get_batch_students(batch_id)}
    deleted_count = 0
    for student_id in student_ids:
        if student_id not in allowed:
            continue
        try:
            delete_batch_student(student_id)
            deleted_count += 1
        except Exception as e:
            flash(f'Error deleting student {student_id}: {str(e)}', 'error')

    if deleted_count:
        flash(f'Deleted {deleted_count} student(s) from the batch.', 'success')
    return redirect(url_for('manage_batches', batch_id=batch_id))


@app.route('/admin/batches/<int:batch_id>/students/<int:student_id>/edit', methods=['POST'])
@batch_manager_required
def edit_batch_student_route(batch_id, student_id):
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    institute_email = request.form.get('institute_email', '').strip()
    contact_number = request.form.get('contact_number', '').strip()
    if not name or not email:
        flash('Name and Email are required.', 'error')
        return redirect(url_for('manage_batches', batch_id=batch_id))
    try:
        update_batch_student(student_id, name=name, email=email, institute_email=institute_email, contact_number=contact_number)
        flash('Student updated successfully.', 'success')
    except Exception as e:
        flash(f'Error updating student: {str(e)}', 'error')
    return redirect(url_for('manage_batches', batch_id=batch_id))


@app.route('/admin/batches/<int:batch_id>/students/add', methods=['POST'])
@batch_manager_required
def add_batch_student_manual(batch_id):
    """Allow superadmin to add a single student manually to a batch."""
    batch = get_batch(batch_id)
    if not batch:
        flash('Batch not found.', 'error')
        return redirect(url_for('manage_batches'))

    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    institute_email = request.form.get('institute_email', '').strip() or None
    contact_number = request.form.get('contact_number', '').strip() or None
    if not name or not email:
        flash('Name and Email are required to add a student.', 'error')
        return redirect(url_for('manage_batches', batch_id=batch_id))

    try:
        add_student_to_batch(batch_id, name, email, institute_email=institute_email, contact_number=contact_number)
        flash('Student added successfully.', 'success')
    except Exception as e:
        flash(f'Error adding student: {str(e)}', 'error')
    return redirect(url_for('manage_batches', batch_id=batch_id))


@app.route('/admin/batches/download_template')
@batch_manager_required
def download_batch_template():
    """Download a simple CSV template for batch student upload (Name,Email, + optional InstituteEmail,ContactNumber)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Name', 'Email', 'InstituteEmail', 'ContactNumber'])
    writer.writerow(['John Doe', 'john.doe@example.com', 'john.doe@institute.edu', '9876543210'])
    mem = io.BytesIO(buf.getvalue().encode('utf-8'))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name='batch_students_template.csv', mimetype='text/csv')


@app.route('/admin/batches/<int:batch_id>/download')
@batch_manager_required
def download_batch_students(batch_id):
    batch = get_batch(batch_id)
    if not batch:
        flash('Batch not found.', 'error')
        return redirect(url_for('manage_batches'))
    students = get_batch_students(batch_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Name', 'Email', 'InstituteEmail', 'ContactNumber'])
    for s in students:
        writer.writerow([s['name'], s['email'], s['institute_email'] or '', s['contact_number'] or ''])
    mem = io.BytesIO(buf.getvalue().encode('utf-8'))
    mem.seek(0)
    filename = f"batch_{batch['name']}_students.csv"
    return send_file(mem, as_attachment=True, download_name=filename, mimetype='text/csv')


@app.route('/admin/batches/download_all')
@batch_manager_required
def download_all_batches_students():
    rows = get_all_batch_students_with_batches()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['BatchName', 'Name', 'Email', 'InstituteEmail', 'ContactNumber'])
    for r in rows:
        writer.writerow([r['batch_name'], r['student_name'], r['student_email'], r['student_institute_email'] or '', r['student_contact_number'] or ''])
    mem = io.BytesIO(buf.getvalue().encode('utf-8'))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name='all_batches_students.csv', mimetype='text/csv')


@app.route('/admin/batch_student_search')
@batch_manager_required
def batch_student_search_api():
    """Search batch students by name or email and return suggestions with batch info."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])

    # Simple search over batch_students joined with batches
    from database import get_all_batch_students_with_batches
    try:
        pattern = q.lower()
        rows = get_all_batch_students_with_batches()
        suggestions = []
        for r in rows:
            d = dict(r) if hasattr(r, 'keys') else r
            name = d.get('student_name', '') or ''
            email = d.get('student_email', '') or ''
            if pattern in name.lower() or pattern in email.lower():
                suggestions.append({
                    'student_id': d.get('student_id'),
                    'name': name,
                    'email': email,
                    'batch_id': d.get('batch_id'),
                    'batch_name': d.get('batch_name'),
                })
            if len(suggestions) >= 20:
                break
        return jsonify(suggestions)
    except Exception as e:
        print(f"batch_student_search_api error: {e}")
        return jsonify([]), 500


@app.route('/admin/batches/download_bulk_template')
@batch_manager_required
def download_batches_bulk_template():
    """Template for bulk batch + students upload (BatchName,Name,Email,GroupName,SubGroupName, + optional InstituteEmail,ContactNumber)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['BatchName', 'Name', 'Email', 'GroupName', 'SubGroupName', 'InstituteEmail', 'ContactNumber'])
    writer.writerow(['Batch A 2025', 'John Doe', 'john.doe@example.com', 'IIM Indore', 'PGP-1', 'john.doe@institute.edu', '9876543210'])
    writer.writerow(['Batch A 2025', 'Jane Smith', 'jane.smith@example.com', 'IIM Indore', 'PGP-1', '', ''])
    writer.writerow(['Batch B 2025', 'Alice Kumar', 'alice.kumar@example.com', 'IIM Visakhapatnam', 'EPGP-2', '', ''])
    mem = io.BytesIO(buf.getvalue().encode('utf-8'))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name='batches_bulk_template.csv', mimetype='text/csv')


@app.route('/admin/batches/upload_bulk', methods=['POST'])
@batch_manager_required
def upload_batches_bulk():
    """Bulk upload batches and students from a single file with columns BatchName,Name,Email,GroupName,SubGroupName."""
    file = request.files.get('batches_file')
    replace = request.form.get('replace') == '1'
    if not file or not file.filename:
        flash('Please select a file to upload.', 'error')
        return redirect(url_for('manage_batches'))
    try:
        filename = secure_filename(file.filename)
        temp_path = os.path.join(TEMP_DIR, f"{secrets.token_hex(8)}_{filename}")
        file.save(temp_path)
        if temp_path.lower().endswith(('.xlsx', '.xls')):
            df = pd.read_excel(temp_path)
        else:
            df = pd.read_csv(temp_path)
        df.columns = df.columns.str.strip()
        required_cols = {'BatchName', 'Name', 'Email'}
        if not required_cols.issubset(set(df.columns)):
            flash('Bulk file must contain columns BatchName, Name, and Email.', 'error')
            return redirect(url_for('manage_batches'))

        # Get optional group/subgroup/contact columns if they exist
        has_group = 'GroupName' in df.columns
        has_subgroup = 'SubGroupName' in df.columns
        has_institute_email = 'InstituteEmail' in df.columns
        has_contact_number = 'ContactNumber' in df.columns

        # Optionally clear students for affected batches first
        if replace:
            affected_batches = set(df['BatchName'].dropna().astype(str).str.strip())
            for bname in affected_batches:
                if not bname:
                    continue
                # Find or create batch
                existing_batches = [b for b in get_batches(is_completed=None) if b['name'].lower() == bname.lower()]
                if existing_batches:
                    b = existing_batches[0]
                    existing_students = get_batch_students(b['id'])
                    for s in existing_students:
                        delete_batch_student(s['id'])

        # Insert/append students
        for _, row in df.iterrows():
            batch_name = str(row['BatchName']).strip()
            name = str(row['Name']).strip()
            email = str(row['Email']).strip().lower()
            if not batch_name or not name or not email:
                continue

            group_name = str(row['GroupName']).strip() if (has_group and pd.notna(row['GroupName'])) else None
            subgroup_name = str(row['SubGroupName']).strip() if (has_subgroup and pd.notna(row['SubGroupName'])) else None
            institute_email = str(row['InstituteEmail']).strip() if (has_institute_email and pd.notna(row['InstituteEmail'])) else None
            contact_number = str(row['ContactNumber']).strip() if (has_contact_number and pd.notna(row['ContactNumber'])) else None

            group_id = None
            if group_name:
                # Find or create group
                existing_groups = [g for g in get_batch_groups() if g['name'].lower() == group_name.lower()]
                if existing_groups:
                    group_id = existing_groups[0]['id']
                else:
                    group_id = create_batch_group(group_name, f"Bulk uploaded group: {group_name}", session.get('user_id', 'superadmin'))

            subgroup_id = None
            if subgroup_name and group_id:
                # Find or create subgroup
                existing_subgroups = [sg for sg in get_batch_subgroups(group_id) if sg['name'].lower() == subgroup_name.lower()]
                if existing_subgroups:
                    subgroup_id = existing_subgroups[0]['id']
                else:
                    subgroup_id = create_batch_subgroup(subgroup_name, f"Bulk uploaded sub-group: {subgroup_name}", group_id, session.get('user_id', 'superadmin'))

            # Find or create batch
            existing_batches = [b for b in get_batches(is_completed=None) if b['name'].lower() == batch_name.lower()]
            if existing_batches:
                b = existing_batches[0]
                batch_id = b['id']
                # Update batch assignments if they were provided
                update_batch(batch_id, group_id=group_id, subgroup_id=subgroup_id)
            else:
                batch_id = create_batch(batch_name, None, session.get('user_id', 'superadmin@attendancify.com'), group_id=group_id, subgroup_id=subgroup_id)
            add_student_to_batch(batch_id, name, email, institute_email=institute_email, contact_number=contact_number)
        flash('Bulk batches and students uploaded successfully.', 'success')
    except Exception as e:
        flash(f'Error uploading bulk batches: {str(e)}', 'error')
    return redirect(url_for('manage_batches'))

# ----------- Excluded Participants Management -----------
@app.route('/admin/excluded_participants')
@login_required
def manage_excluded_participants():
    """Manage excluded participants - Superadmin only"""
    if session.get('role') != 'superadmin':
        flash('Access denied. Superadmin access required.', 'error')
        return redirect(url_for('index'))
    
    excluded = get_excluded_participants()
    return render_template('excluded_participants.html', excluded=excluded, show_navigation=True)

@app.route('/admin/excluded_participants/add', methods=['POST'])
@login_required
def add_excluded():
    """Add a participant to exclusion list"""
    if session.get('role') != 'superadmin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))
    
    identifier = request.form.get('identifier', '').strip()
    identifier_type = request.form.get('identifier_type', 'name')
    is_permanently_blocked = 1 if request.form.get('is_permanently_blocked') == '1' else 0
    
    if not identifier:
        flash('Please enter a name or email to exclude.', 'error')
        return redirect(url_for('manage_excluded_participants'))
    
    if add_excluded_participant(identifier, identifier_type, session.get('user_id'), is_permanently_blocked):
        status = 'permanently blocked' if is_permanently_blocked else 'excluded'
        flash(f'Added "{identifier}" as {status}.', 'success')
    else:
        flash(f'"{identifier}" is already in the exclusion list.', 'warning')
    
    return redirect(url_for('manage_excluded_participants'))

@app.route('/admin/excluded_participants/toggle_block/<int:participant_id>', methods=['POST'])
@login_required
def toggle_block(participant_id):
    """Toggle permanent block status for a participant"""
    if session.get('role') != 'superadmin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))
    
    # Get current status
    participants = get_excluded_participants()
    current_status = 0
    for p in participants:
        if p['id'] == participant_id:
            current_status = p.get('is_permanently_blocked', 0)
            break
    
    # Toggle
    new_status = 0 if current_status else 1
    if toggle_permanent_block(participant_id, new_status):
        status_text = 'permanently blocked' if new_status else 'excluded only'
        flash(f'Changed status to {status_text}.', 'success')
    else:
        flash('Error updating status.', 'error')
    
    return redirect(url_for('manage_excluded_participants'))

@app.route('/admin/excluded_participants/bulk_add', methods=['POST'])
@login_required
def bulk_add_excluded():
    """Add multiple participants to exclusion list"""
    if session.get('role') != 'superadmin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))
    
    identifiers_text = request.form.get('identifiers', '').strip()
    identifier_type = request.form.get('identifier_type', 'name')
    is_permanently_blocked = 1 if request.form.get('is_permanently_blocked') == '1' else 0
    
    if not identifiers_text:
        flash('Please enter names or emails to exclude.', 'error')
        return redirect(url_for('manage_excluded_participants'))
    
    # Split by newlines and commas
    identifiers = []
    for line in identifiers_text.split('\n'):
        for item in line.split(','):
            item = item.strip()
            if item:
                identifiers.append(item)
    
    added = bulk_add_excluded_participants(identifiers, identifier_type, session.get('user_id'), is_permanently_blocked)
    status = 'permanently blocked' if is_permanently_blocked else 'excluded'
    flash(f'Added {added} entries as {status}.', 'success')
    
    return redirect(url_for('manage_excluded_participants'))

@app.route('/admin/excluded_participants/upload', methods=['POST'])
@login_required
def upload_excluded():
    """Upload CSV/Excel file with participants to exclude"""
    if session.get('role') != 'superadmin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))
    
    file = request.files.get('file')
    identifier_type = request.form.get('identifier_type', 'name')
    
    if not file or not file.filename:
        flash('Please select a file.', 'error')
        return redirect(url_for('manage_excluded_participants'))
    
    is_permanently_blocked = 1 if request.form.get('is_permanently_blocked') == '1' else 0
    
    try:
        # Read file
        if file.filename.endswith('.csv'):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)
        
        # Find the column with identifiers (first column or column named 'name'/'email')
        identifiers = []
        if identifier_type == 'email' and 'email' in df.columns.str.lower().tolist():
            col = df.columns[df.columns.str.lower() == 'email'][0]
            identifiers = df[col].dropna().astype(str).tolist()
        elif identifier_type == 'name' and 'name' in df.columns.str.lower().tolist():
            col = df.columns[df.columns.str.lower() == 'name'][0]
            identifiers = df[col].dropna().astype(str).tolist()
        else:
            # Use first column
            identifiers = df.iloc[:, 0].dropna().astype(str).tolist()
        
        added = bulk_add_excluded_participants(identifiers, identifier_type, session.get('user_id'), is_permanently_blocked)
        status = 'permanently blocked' if is_permanently_blocked else 'excluded'
        flash(f'Uploaded {added} entries as {status}.', 'success')
    except Exception as e:
        flash(f'Error uploading file: {str(e)}', 'error')
    
    return redirect(url_for('manage_excluded_participants'))

@app.route('/admin/excluded_participants/edit/<int:participant_id>', methods=['POST'])
@login_required
def edit_excluded(participant_id):
    """Edit an excluded participant's identifier"""
    if session.get('role') != 'superadmin':
        flash('Access denied.', 'error')
        return redirect(url_for('manage_excluded_participants'))
    
    # Support both JSON and form data
    if request.is_json:
        data = request.get_json()
        identifier = data.get('identifier', '').strip()
        identifier_type = data.get('identifier_type', 'email')
    else:
        identifier = request.form.get('identifier', '').strip()
        identifier_type = request.form.get('identifier_type', 'email')
    
    if not identifier:
        if request.is_json:
            return {'success': False, 'message': 'Identifier cannot be empty'}
        flash('Identifier cannot be empty.', 'error')
        return redirect(url_for('manage_excluded_participants'))
    
    if update_excluded_participant(participant_id, identifier, identifier_type):
        if request.is_json:
            return {'success': True, 'message': 'Updated successfully'}
        flash('Updated successfully.', 'success')
    else:
        if request.is_json:
            return {'success': False, 'message': 'Update failed. Identifier may already exist.'}
        flash('Update failed. Identifier may already exist.', 'error')
    
    return redirect(url_for('manage_excluded_participants'))

@app.route('/admin/excluded_participants/delete/<int:participant_id>', methods=['POST'])
@login_required
def delete_excluded(participant_id):
    """Remove a participant from exclusion list"""
    if session.get('role') != 'superadmin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))
    
    remove_excluded_participant(participant_id)
    flash('Removed from exclusion list.', 'success')
    return redirect(url_for('manage_excluded_participants'))

@app.route('/admin/excluded_participants/bulk_delete', methods=['POST'])
@login_required
def bulk_delete_excluded():
    """Remove several participants from the exclusion list in one request."""
    if session.get('role') != 'superadmin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))

    participant_ids = _parse_id_list(request.form.get('participant_ids', ''))
    if not participant_ids:
        flash('No exclusions selected.', 'warning')
        return redirect(url_for('manage_excluded_participants'))

    for participant_id in participant_ids:
        remove_excluded_participant(participant_id)
    flash(f'Removed {len(participant_ids)} entr(ies) from the exclusion list.', 'success')
    return redirect(url_for('manage_excluded_participants'))

@app.route('/admin/excluded_participants/bulk_toggle_block', methods=['POST'])
@login_required
def bulk_toggle_block_excluded():
    """Set the permanent-block flag on several participants at once."""
    if session.get('role') != 'superadmin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))

    participant_ids = _parse_id_list(request.form.get('participant_ids', ''))
    if not participant_ids:
        flash('No exclusions selected.', 'warning')
        return redirect(url_for('manage_excluded_participants'))

    # An explicit target state, not a per-row toggle: selecting a mix of blocked
    # and unblocked rows and pressing "Block" should leave every one blocked.
    new_status = 1 if request.form.get('block') == '1' else 0
    updated = sum(1 for pid in participant_ids if toggle_permanent_block(pid, new_status))
    status_text = 'permanently blocked' if new_status else 'excluded only'
    flash(f'Changed {updated} entr(ies) to {status_text}.', 'success')
    return redirect(url_for('manage_excluded_participants'))

@app.route('/admin/excluded_participants/clear_all', methods=['POST'])
@login_required
def clear_all_excluded():
    """Clear all excluded participants"""
    if session.get('role') != 'superadmin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))
    
    try:
        import sqlite3
        from database import DATABASE
        with sqlite3.connect(DATABASE) as conn:
            conn.execute('DELETE FROM excluded_participants')
            conn.commit()
        flash('Cleared all exclusions.', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    
    return redirect(url_for('manage_excluded_participants'))

@app.route('/admin/excluded_participants/download_template')
@login_required
def download_excluded_template():
    """Download template file for excluded participants"""
    if session.get('role') != 'superadmin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))
    
    try:
        import io
        # Create a simple CSV template
        template_data = io.StringIO()
        template_data.write('Email\n')
        template_data.write('user1@example.com\n')
        template_data.write('user2@example.com\n')
        template_data.write('user3@example.com\n')
        template_data.seek(0)
        
        # Create BytesIO for response
        output = io.BytesIO()
        output.write(template_data.getvalue().encode('utf-8'))
        output.seek(0)
        
        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name='excluded_participants_template.csv'
        )
    except Exception as e:
        flash(f'Error generating template: {str(e)}', 'error')
        return redirect(url_for('manage_excluded_participants'))

@app.route('/admin/excluded_participants/export')
@login_required
def export_excluded_participants():
    """Export the current excluded participants list as an Excel file."""
    if session.get('role') != 'superadmin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))

    try:
        import io
        rows = get_excluded_participants()
        export_df = pd.DataFrame([
            {
                'Identifier': row['identifier'],
                'Type': row['identifier_type'],
                'Status': 'Permanently Blocked' if row['is_permanently_blocked'] else 'Excluded',
                'Added By': row['added_by'],
                'Created': row['created_at'],
            }
            for row in rows
        ], columns=['Identifier', 'Type', 'Status', 'Added By', 'Created'])

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            export_df.to_excel(writer, index=False, sheet_name='Excluded Participants')
        output.seek(0)

        return send_file(
            output,
            as_attachment=True,
            download_name='excluded_participants.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    except Exception as e:
        flash(f'Error exporting excluded participants: {str(e)}', 'error')
        return redirect(url_for('manage_excluded_participants'))

# ----------- Name Normalization -----------
def normalize_name(name: str) -> str:
    if not isinstance(name, str):
        name = str(name)
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip().lower()

# ----------- Raw Excel Generator Functions -----------
SHEET_NAME = "Attendance"

def extract_raw_from_excel(xl_path):
    """
    Extract attendance data from Excel file.
    - Looks for 'Name (Original Name)' column for names
    - Looks for date-time columns (e.g., '2025-10-18 10:30:00') for P/A status
    - Output: Name, Session 1 (date_time), Session 2 (date_time), ...
    - xl_path can be a file path (str) or a file-like object (BytesIO)
    """
    xl = pd.ExcelFile(xl_path)
    
    # Robustly find the Attendance sheet
    target_sheet = None
    for sn in xl.sheet_names:
        if "attendance" in str(sn).lower().strip():
            target_sheet = sn
            break
            
    if target_sheet:
        df = xl.parse(target_sheet)
    else:
        # Fallback to the first sheet if nothing resembling 'Attendance' is found
        df = xl.parse(0)
    
    # Find the Name column - prioritize "Name (Original Name)"
    name_col = None
    name_candidates = [
        "name (original name)", 
        "name(original name)",
        "name (original)",
        "original name",
        "name",
        "participant name"
    ]
    
    for col in df.columns:
        col_lower = str(col).strip().lower()
        for candidate in name_candidates:
            if candidate in col_lower or col_lower == candidate:
                name_col = col
                break
        if name_col:
            break
    
    if not name_col:
        raise ValueError("No 'Name' or 'Name (Original Name)' column found.")
    
    # Find date-time columns for P/A status
    # These columns typically have datetime format like '2025-10-18 10:30:00'
    datetime_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}[\s_]\d{2}:\d{2}:\d{2}$')
    date_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}$')
    
    session_cols = []
    for col in df.columns:
        col_str = str(col).strip()
        # Check if column name matches datetime pattern
        if datetime_pattern.match(col_str) or date_pattern.match(col_str):
            session_cols.append(col)
        # Also check if column contains P/A values (fallback)
        elif col != name_col:
            unique_vals = df[col].dropna().astype(str).str.strip().str.upper().unique()
            if set(unique_vals).issubset({'P', 'A', 'N/A', ''}):
                session_cols.append(col)
    
    # If no datetime columns found, try columns with "Session" in name
    if not session_cols:
        session_cols = [c for c in df.columns if "session" in str(c).lower() and c != name_col]
    
    # If still no session columns, look for any columns with P/A values
    if not session_cols:
        for col in df.columns:
            if col != name_col:
                unique_vals = df[col].dropna().astype(str).str.strip().str.upper().unique()
                # Check if column has mostly P/A values
                pa_count = sum(1 for v in unique_vals if v in ('P', 'A'))
                if pa_count > 0 and pa_count >= len(unique_vals) * 0.5:
                    session_cols.append(col)
    
    if not session_cols:
        raise ValueError("No session/date-time columns with P/A values found.")
    
    # Create output dataframe
    out = df[[name_col] + session_cols].copy()
    
    # Rename columns: Name stays as "Name", date-time columns become "Session N (datetime)"
    new_column_names = ["Name"]
    for i, col in enumerate(session_cols, 1):
        col_str = str(col).strip()
        # If it's a datetime column, format as "Session N (datetime)"
        if datetime_pattern.match(col_str) or date_pattern.match(col_str):
            new_column_names.append(f"Session {i} ({col_str})")
        else:
            # Keep original name if it already has Session format
            new_column_names.append(str(col))
    
    out.columns = new_column_names
    
    # Normalize P/A values - only keep "P"/"A", replace anything else with "N/A"
    for col in out.columns[1:]:  # Skip the Name column
        out[col] = out[col].apply(lambda x: str(x).strip().upper() if str(x).strip().upper() in ("P", "A") else "N/A")
    
    return out

# ----------- Attendance Matching Functions -----------

def normalize_session_header(header: str) -> str:
    """
    Normalize session column headers to support multiple date formats.
    Supports:
    - "Session 1 (26.11.2025_19:30)" - DD.MM.YYYY_HH:MM format
    - "Session 1 (2025-09-06 17:30:00)" - YYYY-MM-DD HH:MM:SS format
    - "Session 1 (2025-09-06)" - YYYY-MM-DD format
    - Any other format is preserved as-is
    """
    header = str(header).strip()
    
    # Pattern for DD.MM.YYYY_HH:MM format (e.g., "Session 1 (26.11.2025_19:30)")
    pattern1 = r'^(Session\s*\d+)\s*\((\d{1,2})\.(\d{1,2})\.(\d{4})_(\d{1,2}):(\d{2})\)$'
    match1 = re.match(pattern1, header, re.IGNORECASE)
    if match1:
        session_name = match1.group(1)
        day, month, year, hour, minute = match1.groups()[1:]
        # Normalize to "Session N (YYYY-MM-DD HH:MM)"
        return f"{session_name} ({year}-{month.zfill(2)}-{day.zfill(2)} {hour.zfill(2)}:{minute})"
    
    # Pattern for YYYY-MM-DD HH:MM:SS format (e.g., "Session 1 (2025-09-06 17:30:00)")
    pattern2 = r'^(Session\s*\d+)\s*\((\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2}):?(\d{2})?\)$'
    match2 = re.match(pattern2, header, re.IGNORECASE)
    if match2:
        session_name = match2.group(1)
        year, month, day, hour, minute = match2.groups()[1:6]
        # Normalize to "Session N (YYYY-MM-DD HH:MM)"
        return f"{session_name} ({year}-{month.zfill(2)}-{day.zfill(2)} {hour.zfill(2)}:{minute})"
    
    # Pattern for YYYY-MM-DD format without time (e.g., "Session 1 (2025-09-06)")
    pattern3 = r'^(Session\s*\d+)\s*\((\d{4})-(\d{1,2})-(\d{1,2})\)$'
    match3 = re.match(pattern3, header, re.IGNORECASE)
    if match3:
        session_name = match3.group(1)
        year, month, day = match3.groups()[1:]
        return f"{session_name} ({year}-{month.zfill(2)}-{day.zfill(2)})"
    
    # Return original header if no pattern matches
    return header


def read_raw_file(raw_path: str, normalize_headers: bool = True) -> pd.DataFrame:
    """
    Read raw attendance file and normalize headers.
    Supports both CSV and Excel formats.
    Handles multiple session header date formats.
    """
    df = pd.read_csv(raw_path) if raw_path.lower().endswith(".csv") else pd.read_excel(raw_path)
    
    # Normalize headers (strip unicode spaces, unify casing)
    def _norm_header(h):
        s = unicodedata.normalize('NFKC', str(h))
        s = s.replace('\u00A0', ' ').replace('\u200B', '')  # NBSP and zero-width space
        s = re.sub(r"\s+", " ", s).strip().lower()
        return s
    
    original_cols = list(df.columns)
    norm_map = {orig: _norm_header(orig) for orig in original_cols}

    # Try to find a 'Name' column by robust matching
    candidates_exact = {"name", "participant name", "participant", "full name", "name (original name)", "name (original)"}
    name_col = None
    for orig, norm in norm_map.items():
        if norm in candidates_exact:
            name_col = orig
            break
    if name_col is None:
        # Fallback: any header containing whole word 'name'
        for orig, norm in norm_map.items():
            if re.search(r"\bname\b", norm):
                name_col = orig
                break
    if name_col is None:
        # Last resort: strip non-letters and compare
        def letters_only(s):
            return re.sub(r"[^a-z]", "", s)
        for orig, norm in norm_map.items():
            lo = letters_only(norm)
            if lo in {"name", "participantname", "fullname"}:
                name_col = orig
                break
    if name_col is None:
        raise ValueError("Raw file needs a 'Name' column.")

    # Session/status columns are all others
    session_cols = [c for c in original_cols if c != name_col]
    if not session_cols:
        raise ValueError("Raw file contains no session/status columns.")
    
    # Normalize session column headers if requested
    if normalize_headers:
        new_session_cols = [normalize_session_header(c) for c in session_cols]
        rename_map = {old: new for old, new in zip(session_cols, new_session_cols)}
        df = df.rename(columns=rename_map)
        session_cols = new_session_cols
    
    def norm(x):
        val = str(x).strip().upper()
        return val if val in ("P", "A") else "N/A"
    for col in session_cols:
        df[col] = df[col].apply(norm)
    out = df[[name_col] + session_cols].copy()
    out.columns = ["Name"] + session_cols
    return out

def postprocess_attendance(df, session_cols):
    # Replace P→present, A→absent (case-insensitive), but only in session columns
    for col in session_cols:
        df[col] = df[col].replace({"P": "present", "A": "absent", "p": "present", "a": "absent"})
    return df

def _match_and_write_core(mdf: pd.DataFrame, raw_file: str, out_fmt: str, master_label: str, batch_id: int = None) -> str:
    email_col = next((c for c in mdf.columns if str(c).strip().lower() in ("email", "email_id")), None)
    name_col = next((c for c in mdf.columns if str(c).strip().lower() in ("participant name", "name")), None)
    # Institute Email is optional and only ever present here when the caller (batch mode)
    # explicitly opted in to including it in the generated output -- see include_institute_email
    # in match_and_write_from_students.
    institute_email_col = next((c for c in mdf.columns if str(c).strip().lower() == "institute email"), None)

    if email_col is None or name_col is None:
        raise ValueError("Master file must have 'Email' and 'Participant Name' columns.")
    keep_cols = [email_col, name_col]
    out_cols = ["Email", "Participant Name"]
    if institute_email_col is not None:
        keep_cols.append(institute_email_col)
        out_cols.append("Institute Email")
    mdf = mdf[keep_cols].copy()
    mdf.columns = out_cols
    rdf = read_raw_file(raw_file)
    session_cols = list(rdf.columns[1:])
    raw_norm_names = [normalize_name(n) for n in list(rdf["Name"])]

    # Tier 1: batch-scoped learned pairs (Zoom display name -> roster name), reversed by
    # normalized roster name so each master row can look itself up directly. Only applies
    # when this run is tied to a stored batch (batch mode) -- file-mode matching has no
    # batch_id and always falls through to fuzzy matching.
    learned_records = {}
    learned_reverse = {}
    if batch_id:
        try:
            learned_records = get_batch_records_map(batch_id)
            for zoom_name, rec in learned_records.items():
                learned_reverse.setdefault(normalize_name(rec['roster_name_matched']), []).append(zoom_name)
        except Exception as e:
            print(f"[Smart Matcher] Tier-1 learned lookup failed for batch {batch_id}: {e}")
            learned_records, learned_reverse = {}, {}
    raw_name_to_indices = {}
    for j, nm in enumerate(rdf["Name"]):
        raw_name_to_indices.setdefault(nm, []).append(j)

    matched_df = mdf.copy()
    # Insert Raw Name column right after Participant Name
    matched_df.insert(2, 'Raw Name', '')
    matched_df['Confidence'] = None
    for col in session_cols:
        matched_df[col] = "N/A"
    threshold = 85
    matched_indices = set()
    for idx, row in matched_df.iterrows():
        master_name = normalize_name(str(row["Participant Name"]))
        best_score, best_j, tier1_hit = -1, None, False

        # Tier 1: exact learned match for this batch
        for candidate_zoom_name in learned_reverse.get(master_name, []):
            for j in raw_name_to_indices.get(candidate_zoom_name, []):
                if j not in matched_indices:
                    best_j = j
                    best_score = float(learned_records[candidate_zoom_name]['confidence'] or 100)
                    tier1_hit = True
                    break
            if best_j is not None:
                break

        # Tier 2: fall back to fuzzy/phonetic matching
        if best_j is None:
            for j, raw_norm in enumerate(raw_norm_names):
                score = fuzz.token_set_ratio(master_name, raw_norm)
                if score > best_score:
                    best_score = score
                    best_j = j

        # Tier 3: below threshold stays unmatched, flagged for manual review via "Unmatched Raw"
        if best_score >= threshold:
            raw_row = rdf.iloc[best_j]
            # Set raw name (from the raw file)
            try:
                matched_df.at[idx, 'Raw Name'] = raw_row['Name']
            except Exception:
                matched_df.at[idx, 'Raw Name'] = ''
            matched_df.at[idx, 'Confidence'] = round(best_score, 2)
            for col in session_cols:
                matched_df.at[idx, col] = raw_row[col]
            matched_indices.add(best_j)

            if tier1_hit:
                # Already-learned pair: confirm it was seen again, but don't treat it as new
                # data to (re-)learn.
                try:
                    touch_batch_record(batch_id, raw_row['Name'])
                except Exception as e:
                    print(f"[Smart Matcher] Failed to touch batch_record for batch {batch_id}: {e}")

    unmatched_df = rdf.iloc[[i for i in range(len(rdf)) if i not in matched_indices]].copy()
    if not unmatched_df.empty:
        unmatched_df.rename(columns={"Name": "Raw Name (not found in Master)"}, inplace=True)
    # ---- Here: replace P/A
    matched_df = postprocess_attendance(matched_df, session_cols)
    if not unmatched_df.empty:
        unmatched_df = postprocess_attendance(unmatched_df, session_cols)
    out_dir = TEMP_DIR
    # Use a clean master label for output filenames (batch name or master file base)
    mbase = master_label
    rstored = os.path.basename(raw_file)
    rbase_raw = rstored.split('_', 1)[1] if '_' in rstored else rstored
    rbase = os.path.splitext(rbase_raw)[0]
    if out_fmt == "xlsx":
        # Get app-specific naming for App2 (Smart Matcher)
        app_name_display = get_app_setting('app2_name', 'Intelligent_Record_Matcher')
        app_name_for_file = app_name_display.replace(' ', '_').replace('-', '_')
        
        # Get original raw filename (strip random hex prefix)
        rstored = os.path.basename(raw_file)
        rbase_raw = rstored.split('_', 1)[1] if '_' in rstored else rstored
        rbase = os.path.splitext(rbase_raw)[0]
        if rbase.startswith('extracted_'):
            rbase = rbase[len('extracted_'):]
        
        # Apply configured column hiding/renaming, if any
        custom_columns, hidden_columns = get_output_column_config('app2')
        if hidden_columns:
            matched_df = matched_df.drop(columns=[c for c in hidden_columns if c in matched_df.columns])
            if not unmatched_df.empty:
                unmatched_df = unmatched_df.drop(columns=[c for c in hidden_columns if c in unmatched_df.columns])
        if custom_columns:
            matched_df = matched_df.rename(columns=custom_columns)
            if not unmatched_df.empty:
                unmatched_df = unmatched_df.rename(columns=custom_columns)
        
        # Prefix with tool name and the match result: AppName_100_OriginalFileName.xlsx
        match_result = '100' if unmatched_df.empty else '90'
        out_path = os.path.join(out_dir, f"{app_name_for_file}_{match_result}_{rbase}.xlsx")
        with pd.ExcelWriter(out_path, engine="openpyxl") as w:
            matched_df.to_excel(w, index=False, sheet_name="Matched")
            if not unmatched_df.empty:
                unmatched_df.to_excel(w, index=False, sheet_name="Unmatched Raw")
            pd.DataFrame([], columns=["email_id", "attendance(absent/present/leave)"]).to_excel(w, index=False, sheet_name="Summary")
        return out_path
    prefix = os.path.join(out_dir, f"{mbase}_matched_with_{rbase}_")
    matched_df.to_csv(prefix + "matched.csv", index=False)
    if not unmatched_df.empty:
        unmatched_df.to_csv(prefix + "unmatched.csv", index=False)
    pd.DataFrame([], columns=["email_id", "attendance(absent/present/leave)"]).to_csv(prefix + "summary.csv", index=False)
    return prefix + "matched.csv"


def match_and_write(master_file: str, raw_file: str, out_fmt: str = "xlsx") -> str:
    """Existing entry point that reads master from a file path."""
    mdf = pd.read_csv(master_file) if master_file.lower().endswith(".csv") else pd.read_excel(master_file)
    # Derive a user-facing label from the stored filename (strip random hex)
    mstored = os.path.basename(master_file)
    mbase_raw = mstored.split('_', 1)[1] if '_' in mstored else mstored
    master_label = os.path.splitext(mbase_raw)[0]
    return _match_and_write_core(mdf, raw_file, out_fmt, master_label)


def match_and_write_from_students(batch_name: str, students: list[dict], raw_file: str, out_fmt: str = "xlsx", batch_id: int = None, include_institute_email: bool = False) -> str:
    """Match using an in-memory student list (batch-based master)."""
    if not students:
        raise ValueError("Selected batch has no students.")
    mdf = pd.DataFrame(students)

    # Ensure expected column names
    if 'Email' not in mdf.columns or 'Participant Name' not in mdf.columns:
        raise ValueError(f"Batch student data must have 'Email' and 'Participant Name' columns. Got: {list(mdf.columns)}")
    # Institute Email is purely optional/informational -- only carried into the generated
    # file when the user explicitly opts in for this run via the Smart Matcher toggle.
    if not include_institute_email and 'Institute Email' in mdf.columns:
        mdf = mdf.drop(columns=['Institute Email'])
    return _match_and_write_core(mdf, raw_file, out_fmt, batch_name, batch_id=batch_id)


def _learn_batch_records_from_matched_sheet(df: pd.DataFrame, batch_id: int) -> None:
    """Learn Zoom-name -> roster-name pairs from a Smart Matcher 'Matched' sheet.

    Called as a side effect of Final Excel Generator (Single CSV Generator) uploads.
    Strictly scoped to batch_id -- rows are upserted into batch_records only for this
    batch, never affecting or reading data learned for any other batch. Rows Smart
    Matcher left unmatched (no Raw Name) are skipped since there's nothing to learn yet.
    """
    if not batch_id:
        return
    roster_col = next((c for c in df.columns if str(c).strip().lower() == 'participant name'), None)
    zoom_col = next((c for c in df.columns if str(c).strip().lower() == 'raw name'), None)
    if roster_col is None or zoom_col is None:
        return  # Not a Smart Matcher "Matched" sheet -- nothing to learn from.
    confidence_col = next((c for c in df.columns if str(c).strip().lower() == 'confidence'), None)

    for _, row in df.iterrows():
        zoom_val = row[zoom_col]
        roster_val = row[roster_col]
        if pd.isna(zoom_val) or pd.isna(roster_val):
            continue
        zoom_display_name = str(zoom_val).strip()
        roster_name_matched = str(roster_val).strip()
        if not zoom_display_name or not roster_name_matched:
            continue
        confidence = None
        if confidence_col is not None:
            try:
                confidence = float(row[confidence_col])
            except (TypeError, ValueError):
                confidence = None
        upsert_batch_record(batch_id, zoom_display_name, roster_name_matched, confidence=confidence, source='single_csv_upload')


# A roster is only filed under a batch when the overlap is this confident, so an
# unrelated upload (or one from before any batch existed) is left unlearned rather than
# attached to the wrong batch.
BATCH_AUTO_DETECT_MIN_MATCH_RATIO = 0.8


def _detect_batch_for_roster(emails, names=None):
    """Guess which existing batch an uploaded roster belongs to, with no user prompt.

    Overlaps the roster's Email column (primary signal) against every stored batch's
    student list; Participant Name is used as a fallback signal when email overlap alone
    isn't confident enough. Returns the best-matching batch id, or None if nothing clears
    BATCH_AUTO_DETECT_MIN_MATCH_RATIO -- callers should skip learning in that case rather
    than guess wrong.
    """
    uploaded_emails = {str(e).strip().lower() for e in (emails or []) if str(e).strip() and str(e).strip().lower() != 'nan'}
    uploaded_names = {normalize_name(n) for n in (names or []) if str(n).strip()}
    if not uploaded_emails and not uploaded_names:
        return None

    best_batch_id, best_score = None, 0.0
    for batch in get_batches(is_completed=None):
        students = get_batch_students(batch['id'])
        if not students:
            continue
        batch_emails = {s['email'].strip().lower() for s in students if s['email']}
        batch_names = {normalize_name(s['name']) for s in students if s['name']}

        score = 0.0
        if uploaded_emails and batch_emails:
            score = len(uploaded_emails & batch_emails) / len(uploaded_emails)
        if score < BATCH_AUTO_DETECT_MIN_MATCH_RATIO and uploaded_names and batch_names:
            score = max(score, len(uploaded_names & batch_names) / len(uploaded_names))

        if score > best_score:
            best_score = score
            best_batch_id = batch['id']

    return best_batch_id if best_score >= BATCH_AUTO_DETECT_MIN_MATCH_RATIO else None


_BATCH_FILENAME_STOPWORDS = {
    'batch', 'attendance', 'attendancify', 'report', 'matched', 'raw', 'file', 'output',
    'zoom', 'session', 'sessions', 'meeting', 'export', 'download', 'csv', 'xlsx', 'xls',
}


def _normalise_batch_filename_text(value):
    """Normalize batch/file labels for conservative server-side batch suggestions."""
    # Hyphens/dots/etc. are separators here (mirrors the client-side matcher in
    # static/js/smart-matcher.js) -- left to normalize_name they'd simply be
    # dropped, gluing "eposcm-07-class" into one unmatchable token instead of
    # ["eposcm", "07", "class"].
    text = re.sub(r'[_\-./()\[\]{}]+', ' ', str(value))
    text = normalize_name(text)
    tokens = []
    for token in text.split():
        if token in _BATCH_FILENAME_STOPWORDS:
            continue
        if token.isdigit():
            token = str(int(token))
        tokens.append(token)
    return tokens


def _detect_batch_for_filename(file_name, batches):
    """Suggest a batch from a generated filename without making a hard decision.

    A batch is only auto-selected when its full name appears as a contiguous,
    in-order phrase in the filename (e.g. "EPOSCM 07" literally next to each
    other in that order). Matching used to accept a batch whenever every one of
    its tokens showed up *anywhere* in the filename -- which let an unrelated
    number elsewhere in the name (a date, a session count, a timestamp)
    coincidentally satisfy a different batch's numeric suffix, e.g. a file for
    "EPOSCM 07" getting auto-matched to "EPOSCM 09" because a "09" from the
    file's date happened to be present too. The loose ratio is still computed
    and returned as a display-only confidence hint when nothing clears the bar.
    """
    file_tokens = _normalise_batch_filename_text(file_name)
    if not file_tokens:
        return None, 0

    def is_contiguous_match(batch_tokens):
        n = len(batch_tokens)
        return any(
            file_tokens[i:i + n] == batch_tokens
            for i in range(len(file_tokens) - n + 1)
        )

    contiguous_matches = []
    best_ratio = 0.0
    for batch in batches:
        batch_tokens = _normalise_batch_filename_text(batch['name'])
        if not batch_tokens:
            continue
        matched = sum(1 for token in batch_tokens if token in file_tokens)
        best_ratio = max(best_ratio, matched / len(batch_tokens))
        if is_contiguous_match(batch_tokens):
            contiguous_matches.append((len(batch_tokens), batch))

    if not contiguous_matches:
        return None, round(best_ratio * 100)

    # Prefer the most specific (longest) contiguous match; stay silent only if
    # two equally specific batch names both match the same filename.
    contiguous_matches.sort(key=lambda item: item[0], reverse=True)
    if len(contiguous_matches) > 1 and contiguous_matches[1][0] == contiguous_matches[0][0]:
        return None, 100
    return contiguous_matches[0][1], 100


def _server_files_for_user(user_email, processing_type=None):
    """Return accessible, existing processed files for the current user."""
    files = get_processed_files(user_email=user_email, processing_type=processing_type)
    return [dict(file) for file in files if os.path.isfile(dict(file).get('file_path', ''))]

# Tools that /?tool=<id> can open directly in the home page's split view
SPLIT_VIEW_TOOLS = ('attendance-generator', 'smart-matcher', 'final-excel-generator')


# ----------- Routes -----------
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Handle login form submission
        email = request.form['email']
        password = request.form['password']

        # This form is a second way into the same accounts, so it is throttled
        # the same way /login is - otherwise the limit there is simply bypassed.
        locked = login_lockout_remaining(email)
        if locked:
            flash(f'Too many failed attempts. Try again in {max(1, locked // 60)} minute(s).')
            return render_template('landing.html', show_navigation=True,
                                   landing=get_landing_content())

        user = get_user(email)

        if user:
            user_dict = dict(user) if hasattr(user, 'keys') else user
            if verify_and_upgrade_password(email, user_dict.get('password_hash', ''), password):
                # Check if account has expired
                expires_at_dt = make_aware(user_dict.get('expires_at'))
                if expires_at_dt and datetime.now(IST) > expires_at_dt:
                    flash('Your access has expired. Please contact the admin to renew your access.')
                    return render_template('landing.html', show_navigation=True,
                                           landing=get_landing_content())
                
                # Get IP address
                ip_address = get_client_ip()
                if ip_address and ',' in ip_address:
                    ip_address = ip_address.split(',')[0].strip()
                
                # Log login with IP address
                log_user_login(email, ip_address)
                
                # Set session variables
                session['user_id'] = email
                session['role'] = user_dict.get('role', 'user')
                session['logged_in'] = True
                session['last_activity'] = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
                # Store profile info for display
                session['first_name'] = user_dict.get('first_name') or ''
                # Feature/permission flags
                session['can_manage_roles'] = bool(user_dict.get('can_manage_roles', 0))
                session['can_manage_batch_matching'] = bool(user_dict.get('can_manage_batch_matching', 0))
                session['can_manage_batches'] = bool(user_dict.get('can_manage_batches', 0))
                session['can_access_processed_files'] = bool(user_dict.get('can_access_processed_files', 0))
                session['can_view_dashboard'] = bool(user_dict.get('can_view_dashboard', 0))
                if expires_at_dt:
                    session['expires_at_display'] = expires_at_dt.strftime('%Y-%m-%d %H:%M')
                else:
                    session['expires_at_display'] = ''
                
                # Check if password change is required
                if user_dict.get('must_change_password', False):
                    flash('You must change your password on first login.', 'warning')
                    return redirect(url_for('force_change_password'))
                
                clear_login_failures(email)
                flash('Login successful!')
                return redirect(url_for('index'))
            else:
                note_login_failure(email)
                flash('Invalid email or password.')
        else:
            note_login_failure(email)
            flash('Invalid email or password.')
    
    # Allow public access to the homepage
    # Compute batch mode context for Smart Matcher modal
    can_use_batch_matching_flag = False
    batches = []
    batch_counts = {}
    groups = []
    subgroups = []
    if 'user_id' in session:
        session_role = session.get('role')
        session_user = session.get('user_id')
        if session_role == 'superadmin':
            can_use_batch_matching_flag = True
        else:
            try:
                user_row = get_user(session_user)
                user = dict(user_row) if user_row is not None and hasattr(user_row, 'keys') else user_row
            except Exception:
                user = None
            if user and user.get('can_use_batch_matching'):
                can_use_batch_matching_flag = True

        if can_use_batch_matching_flag:
            batches = get_batches_for_user(session_user, session_role, is_completed=0)
            groups = get_groups_for_user(session_user, session_role)
            subgroups = get_subgroups_for_user(session_user, session_role, is_completed=0)
            active_subgroup_ids = {sg['id'] for sg in subgroups}
            # A batch inside a completed sub-group is excluded too, even though the batch
            # itself is still active, so it disappears from Smart Matcher along with its sub-group.
            batches = [b for b in batches if not b['subgroup_id'] or b['subgroup_id'] in active_subgroup_ids]
            for b in batches:
                try:
                    students = get_batch_students(b['id'])
                    batch_counts[b['id']] = len(students)
                except Exception:
                    batch_counts[b['id']] = 0

    # Get custom app names from settings
    app_names = {
        'app1': get_app_setting('app1_name', 'Zoom Attendance Analyzer'),
        'app2': get_app_setting('app2_name', 'Intelligent Record Matcher'),
        'app3': get_app_setting('app3_name', 'Session Report Generator'),
    }
    
    # Get user stats for dashboard
    stats = None
    auto_match_files = []
    auto_final_files = []
    if 'user_id' in session:
        try:
            user_stats = get_user_usage_summary(session['user_id'])
            if user_stats:
                stats = {
                    'files_today': user_stats.get('files_today', 0),
                    'total_files': user_stats.get('total_files', 0),
                    'hours_saved': round(user_stats.get('total_files', 0) * 0.5, 1)  # Estimate: 30 min saved per file
                }
        except Exception:
            stats = {'files_today': 0, 'total_files': 0, 'hours_saved': 0}

        try:
            for file in _server_files_for_user(session['user_id'], 'attendance_generation'):
                suggested_batch, score = _detect_batch_for_filename(file['file_name'], batches)
                auto_match_files.append({
                    'id': file['id'],
                    'name': _processed_file_display_name(file),
                    'created_at': file.get('created_at'),
                    'suggested_batch_id': suggested_batch['id'] if suggested_batch else None,
                    'suggested_batch_name': suggested_batch['name'] if suggested_batch else '',
                    'suggestion_score': score,
                })
            auto_final_files = [
                {
                    'id': file['id'],
                    'name': _processed_file_display_name(file),
                    'created_at': file.get('created_at'),
                }
                for file in _server_files_for_user(session['user_id'], 'attendance_matching')
                if '_100_' in str(file['file_name'])
                and str(file['file_name']).lower().endswith(('.xlsx', '.xls'))
            ]
        except Exception as exc:
            print(f'Could not load server automation files: {exc}')
    
    # Tool to open directly in the split view, if the caller asked for one
    initial_tool = request.args.get('tool')
    if initial_tool not in SPLIT_VIEW_TOOLS:
        initial_tool = None

    # Logged-out visitors get a compact landing page introducing both apps
    # (Attendance Suite + Tango); logged-in users get the full tool workspace.
    if 'user_id' not in session:
        return render_template(
            'landing.html',
            show_navigation=True,
            app_names=app_names,
            landing=get_landing_content()
        )

    return render_template(
        'comprehensive_index.html',
        show_navigation=True,
        can_use_batch_matching=can_use_batch_matching_flag,
        batches=batches,
        batch_counts=batch_counts,
        groups=groups,
        subgroups=subgroups,
        app_names=app_names,
        stats=stats,
        # /?tool=<id> opens straight into the split view. Resolved here so the
        # page is rendered in split mode from the first paint -- opening it
        # client-side made the home view flash for a moment first.
        initial_tool=initial_tool,
        initial_tool_origin=('dashboard' if request.args.get('from') == 'dashboard' else None),
        auto_match_files=auto_match_files,
        auto_final_files=auto_final_files
    )

# ----------- Attendance Generator Routes -----------
AUTO_START_OFFSET_MINUTES = 30
AUTO_SESSION_SLOT_MINUTES = 75
AUTO_SESSION_GAP_MINUTES = 15
AUTO_TIME_REQUIRED_MINUTES = 65
AUTO_TEMPLATE_BACKWARD_GRACE_MINUTES = 30
DEFAULT_SESSION_PATTERN_DATE = '2025-10-25'
DEFAULT_SESSION_TIME_PATTERNS = (
    ('09:00', '10:15'),
    ('10:30', '11:45'),
    ('12:00', '13:15'),
    ('14:30', '15:45'),
    ('16:00', '17:15'),
    ('15:00', '16:15'),
    ('16:30', '17:45'),
    ('15:30', '16:45'),
    ('17:00', '18:15'),
    ('17:30', '18:45'),
    ('19:00', '20:15'),
    ('20:00', '21:15'),
    ('20:30', '21:45'),
)
AUTO_SESSION_TEMPLATE_STARTS = tuple(
    (int(start_hm[:2]), int(start_hm[3:5]))
    for start_hm, _ in DEFAULT_SESSION_TIME_PATTERNS
)
DEFAULT_SESSION_PATTERN_ENDS = dict(DEFAULT_SESSION_TIME_PATTERNS)

# Batch-specific minimum attendance durations used only by automatic session
# detection. Matching ignores case, punctuation, "Batch", and leading zeroes.
ATTENDANCE_BATCH_REQUIRED_MINUTES = {
    'CEOP 01': 45, 'COOP-01': 45, 'CPAFRM 03': 65, 'CPDMS 05': 45,
    'CPDMS 06': 45, 'CPLCM 06': 45, 'CPLCM 07': 45, 'CPPM 11': 45,
    'CPPM 12': 45, 'CPPM 13': 45, 'ECPSLP 06': 45, 'ECPSLP 07': 45,
    'ECPSLSP 08': 45, 'EMP-MMS 01': 65, 'EPDT 06': 45, 'EPDT 07': 45,
    'EPGCPM04': 65, 'EPGDPM&AI 03': 65, 'EPHM 02': 65, 'EPOSCM 05': 45,
    'EPOSCM-06': 65, 'EPSM 05': 45, 'SMP 03': 45, 'SMP 04': 65,
    'EPHM 03': 65, 'CPPM 14': 45, 'CPDMS-07': 45, 'ECPSLSP 09': 45,
    'CPLCM 08': 45, 'EPOSCM 07': 65, 'CEOP 02': 45, 'SMP 05': 65,
    'EPHM 04': 65, 'CPPM 15': 45, 'CPPM 16': 45, 'CPDMS 08': 45,
    'COOP 02': 45, 'EPSM 06': 45, 'CPPM 17': 45, 'CEOP 03': 45,
    'ECPSLSP 10': 45, 'EMP MMS 03': 65, 'CPLCM 09': 45,
    'EPOSCM 08': 65, 'EPDT 08': 65, 'SMP 06': 65, 'CPPM 18': 45,
    'GLDPE': 65, 'PGCPCMIB 01': 65, 'EPOSCM 09': 65,
}


def _normalise_attendance_batch_label(value):
    """Normalize a batch label for matching against an uploaded filename."""
    value = re.sub(r'(?i)batch', ' ', str(value))
    value = re.sub(r'[^a-zA-Z0-9]+', ' ', value).lower()
    parts = []
    for part in value.split():
        if part.isdigit():
            part = str(int(part))
        parts.append(part)
    return ''.join(parts)


_NORMALISED_ATTENDANCE_BATCH_RULES = tuple(
    (_normalise_attendance_batch_label(batch), minutes)
    for batch, minutes in ATTENDANCE_BATCH_REQUIRED_MINUTES.items()
)


def _required_minutes_for_attendance_filename(file_name):
    """Return a configured duration for a confidently recognized batch filename."""
    filename_key = _normalise_attendance_batch_label(
        _uploaded_file_display_name(os.path.basename(str(file_name)))
    )
    try:
        configured_matches = []
        for batch in get_batches(is_completed=None):
            duration = batch['required_duration_minutes']
            batch_key = _normalise_attendance_batch_label(batch['name'])
            if batch_key and batch_key in filename_key:
                if duration is None or not str(duration).strip():
                    # A known batch with no manual override uses the application default.
                    configured_matches.append((len(batch_key), AUTO_TIME_REQUIRED_MINUTES))
                else:
                    configured_matches.append((len(batch_key), float(duration)))
        if configured_matches:
            longest_length = max(length for length, _ in configured_matches)
            longest_durations = {
                duration for length, duration in configured_matches if length == longest_length
            }
            if len(longest_durations) == 1:
                return next(iter(longest_durations))
    except Exception as exc:
        print(f'[Attendance Generator] Could not read batch duration settings: {exc}')

    matches = [
        minutes for batch_key, minutes in _NORMALISED_ATTENDANCE_BATCH_RULES
        if batch_key and batch_key in filename_key
    ]
    return matches[0] if len(set(matches)) == 1 and matches else AUTO_TIME_REQUIRED_MINUTES


def _required_minutes_for_batch_label(batch_name):
    """Resolve the duration shown for a batch whose manual value is not saved yet."""
    batch_key = _normalise_attendance_batch_label(batch_name)
    matches = [
        minutes for rule_name, minutes in _NORMALISED_ATTENDANCE_BATCH_RULES
        if rule_name and rule_name in batch_key
    ]
    return matches[0] if len(set(matches)) == 1 and matches else AUTO_TIME_REQUIRED_MINUTES

def _uploaded_file_display_name(file_name):
    return file_name.split('_', 1)[1] if '_' in file_name else file_name

def _default_session_pattern_rows():
    return [
        {
            'file': None,
            'start_hm': start_hm,
            'end_hm': end_hm,
            'time_required': AUTO_TIME_REQUIRED_MINUTES
        }
        for start_hm, end_hm in DEFAULT_SESSION_TIME_PATTERNS
    ]

def _default_session_template_csv_rows():
    return [
        (
            f'{DEFAULT_SESSION_PATTERN_DATE} {start_hm}:00',
            f'{DEFAULT_SESSION_PATTERN_DATE} {end_hm}:00',
            AUTO_TIME_REQUIRED_MINUTES
        )
        for start_hm, end_hm in DEFAULT_SESSION_TIME_PATTERNS
    ]

def _read_metadata_cells_e2_f2(file_path):
    if file_path.lower().endswith(('.xlsx', '.xls')):
        df = pd.read_excel(file_path, header=None, nrows=2)
        if df.shape[0] < 2 or df.shape[1] < 6:
            return None, None
        return df.iat[1, 4], df.iat[1, 5]

    encodings = ('utf-8-sig', 'utf-8', 'latin-1')
    last_error = None
    for encoding in encodings:
        try:
            with open(file_path, newline='', encoding=encoding) as csv_file:
                rows = []
                reader = csv.reader(csv_file)
                for row in reader:
                    rows.append(row)
                    if len(rows) >= 2:
                        break
            if len(rows) < 2 or len(rows[1]) < 6:
                return None, None
            return rows[1][4], rows[1][5]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return None, None

def _parse_detected_session_datetime(value, base_date=None):
    if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
        return None

    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().replace(tzinfo=None)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, datetime_time) and base_date:
        return datetime.combine(base_date, value)
    if isinstance(value, (int, float)) and 0 <= value < 1 and base_date:
        seconds = int(round(float(value) * 24 * 60 * 60))
        return datetime.combine(base_date, datetime.min.time()) + timedelta(seconds=seconds)
    if isinstance(value, (int, float)) and value > 1000:
        parsed = pd.to_datetime(value, unit='D', origin='1899-12-30', errors='coerce')
        if not pd.isna(parsed):
            return parsed.to_pydatetime().replace(tzinfo=None)

    value_str = re.sub(r'\s+', ' ', str(value).strip().strip('"').strip("'")).replace(', ', ' ')
    if not value_str:
        return None

    datetime_formats = (
        '%m/%d/%y %I:%M %p', '%m/%d/%Y %I:%M %p',
        '%m/%d/%y %I:%M:%S %p', '%m/%d/%Y %I:%M:%S %p',
        '%m/%d/%y %H:%M', '%m/%d/%Y %H:%M',
        '%m/%d/%y %H:%M:%S', '%m/%d/%Y %H:%M:%S',
        '%d/%m/%y %I:%M %p', '%d/%m/%Y %I:%M %p',
        '%d/%m/%y %I:%M:%S %p', '%d/%m/%Y %I:%M:%S %p',
        '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M',
        '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M'
    )
    for fmt in datetime_formats:
        try:
            return datetime.strptime(value_str, fmt)
        except ValueError:
            continue

    if base_date:
        for fmt in ('%I:%M %p', '%I:%M:%S %p', '%H:%M', '%H:%M:%S'):
            try:
                return datetime.combine(base_date, datetime.strptime(value_str, fmt).time())
            except ValueError:
                continue

    parsed = pd.to_datetime(value_str, dayfirst=False, errors='coerce')
    if not pd.isna(parsed):
        return parsed.to_pydatetime().replace(tzinfo=None)
    return None

def _round_to_nearest_half_hour(dt):
    rounded = dt.replace(second=0, microsecond=0)
    minute = 30 if dt.minute >= 15 and dt.minute < 45 else 0
    if dt.minute >= 45:
        rounded = rounded.replace(minute=0) + timedelta(hours=1)
    else:
        rounded = rounded.replace(minute=minute)
    return rounded

def _snap_to_session_template_start(dt):
    candidates = sorted(
        datetime.combine(dt.date(), datetime_time(hour, minute))
        for hour, minute in AUTO_SESSION_TEMPLATE_STARTS
    )
    previous_candidates = [candidate for candidate in candidates if candidate <= dt]
    next_candidates = [candidate for candidate in candidates if candidate >= dt]

    if previous_candidates:
        previous = previous_candidates[-1]
        if dt - previous <= timedelta(minutes=AUTO_TEMPLATE_BACKWARD_GRACE_MINUTES):
            return previous

    if next_candidates:
        return next_candidates[0]

    if previous_candidates:
        previous = previous_candidates[-1]
        if dt - previous <= timedelta(minutes=AUTO_TEMPLATE_BACKWARD_GRACE_MINUTES):
            return previous

    return _round_to_nearest_half_hour(dt)

def _build_auto_session_rows_from_start(session_start, detected_end, file_name, required_minutes=None):
    session_gap = timedelta(minutes=AUTO_SESSION_GAP_MINUTES)
    required_minutes = required_minutes or AUTO_TIME_REQUIRED_MINUTES
    required_length = timedelta(minutes=required_minutes)
    rows = []

    while len(rows) < 24:
        start_hm = session_start.strftime('%H:%M')
        end_hm = DEFAULT_SESSION_PATTERN_ENDS.get(start_hm)
        if not end_hm:
            break

        session_end = datetime.combine(
            session_start.date(),
            datetime.strptime(end_hm, '%H:%M').time()
        )
        if session_end <= session_start:
            session_end += timedelta(days=1)
        if session_start + required_length > detected_end:
            break

        rows.append({
            'file': file_name,
            'session_start': session_start.strftime('%Y-%m-%d %H:%M:%S'),
            'session_end': session_end.strftime('%Y-%m-%d %H:%M:%S'),
            'time_required': required_minutes,
            'auto_generated': True
        })
        session_start = session_end + session_gap

    return rows

def _auto_sessions_for_uploaded_file(file_path, file_name):
    start_cell, end_cell = _read_metadata_cells_e2_f2(file_path)
    detected_start = _parse_detected_session_datetime(start_cell)
    if not detected_start:
        return [], f'{_uploaded_file_display_name(file_name)}: could not read start time from E2'

    detected_end = _parse_detected_session_datetime(end_cell, detected_start.date())
    if not detected_end:
        return [], f'{_uploaded_file_display_name(file_name)}: could not read end time from F2'
    if detected_end <= detected_start:
        detected_end += timedelta(days=1)

    required_minutes = _required_minutes_for_attendance_filename(file_name)
    candidate_starts = sorted({
        _snap_to_session_template_start(detected_start),
        _snap_to_session_template_start(detected_start + timedelta(minutes=AUTO_START_OFFSET_MINUTES))
    })
    rows = []
    for candidate_start in candidate_starts:
        candidate_rows = _build_auto_session_rows_from_start(candidate_start, detected_end, file_name, required_minutes)
        if len(candidate_rows) > len(rows):
            rows = candidate_rows

    if not rows:
        return [], f'{_uploaded_file_display_name(file_name)}: no complete session windows fit between E2 and F2'
    return rows, None

def _build_auto_session_config(file_paths, file_names):
    auto_rows = []
    warnings = []
    for file_path, file_name in zip(file_paths, file_names):
        try:
            rows, warning = _auto_sessions_for_uploaded_file(file_path, file_name)
            auto_rows.extend(rows)
            if warning:
                warnings.append(warning)
        except Exception as exc:
            warnings.append(f'{_uploaded_file_display_name(file_name)}: {exc}')
    return auto_rows, warnings

@app.route('/attendance_generator')
def attendance_generator():
    # Allow public viewing of the tool UI
    app_names = get_app_names()
    return render_template('attendance_generator.html', show_navigation=True, app_names=app_names)

@app.route('/upload_attendance', methods=['POST'])
@login_required
def upload_attendance_file():
    # Get the mode (single or multiple)
    mode = request.form.get('mode', 'single')
    configuration_mode = request.form.get('configuration_mode', 'auto')
    
    files = request.files.getlist('csv_files')
    if not files or not any(f.filename for f in files):
        flash('No files selected')
        return redirect(url_for('attendance_generator'))

    # Save all files and store their info
    file_paths = []
    file_names = []
    for file in files:
        if file.filename:
            suffix = secrets.token_hex(8)
            filename = f"{suffix}_" + secure_filename(file.filename)
            file_path = os.path.join(TEMP_DIR, filename)
            file.save(file_path)
            file_paths.append(file_path)
            file_names.append(filename)

    # Determine mode based on number of files
    mode = 'multiple' if len(file_paths) > 1 else 'single'

    # Store file info in session
    session['file_paths'] = file_paths
    session['file_names'] = file_names
    session['mode'] = mode
    for key in ('session_config', 'session_config_filename', 'session_config_time_only', 'session_config_auto_generated'):
        session.pop(key, None)
    
    if configuration_mode == 'auto':
        auto_config, auto_warnings = _build_auto_session_config(file_paths, file_names)
        if auto_config:
            session['session_config'] = auto_config
            session['session_config_filename'] = 'Auto-detected from E2/F2'
            session['session_config_time_only'] = False
            session['session_config_auto_generated'] = True
            flash(f'Auto-detected {len(auto_config)} session(s) from uploaded file time metadata.', 'success')
        elif auto_warnings:
            session['session_config_auto_generated'] = False
            flash('Could not auto-detect sessions from E2/F2. Please configure sessions manually.', 'warning')
    else:
        session['session_config_auto_generated'] = False
        flash('Manual configuration selected. Please add or choose session times.', 'info')
    
    # For single file mode, also store singular path/name for compatibility
    if mode == 'single':
        session['file_path'] = file_paths[0]
        session['filename'] = file_names[0]

    return redirect(url_for('configure_attendance_sessions'))

@app.route('/configure_attendance_sessions')
@login_required
def configure_attendance_sessions():
    mode = session.get('mode', 'single')
    file_names = session.get('file_names', [])
    file_paths = session.get('file_paths', [])
    session_config = session.get('session_config', [])
    session_config_filename = session.get('session_config_filename', '')
    session_config_time_only = session.get('session_config_time_only', False)
    session_config_auto_generated = session.get('session_config_auto_generated', False)
    
    # Verify files still exist - check on every page load
    if not file_paths:
        flash('No files uploaded. Please upload your Zoom log files first.', 'error')
        return redirect(url_for('attendance_generator'))
    
    # Check which files still exist
    valid_paths = []
    valid_names = []
    for fp, fn in zip(file_paths, file_names):
        if os.path.exists(fp):
            valid_paths.append(fp)
            valid_names.append(fn)
    
    if not valid_paths:
        flash('Your uploaded files have expired. Please upload again. (Files are kept for 2 hours)', 'error')
        session.pop('file_paths', None)
        session.pop('file_names', None)
        return redirect(url_for('attendance_generator'))
    
    # Update session if some files were lost
    if len(valid_paths) < len(file_paths):
        session['file_paths'] = valid_paths
        session['file_names'] = valid_names
        file_names = valid_names
        flash(f'{len(file_paths) - len(valid_paths)} file(s) expired. {len(valid_paths)} file(s) remaining.', 'warning')

    # If no config loaded, seed with default time-only slots
    if not session_config:
        session_config = _default_session_pattern_rows()
        session['session_config'] = session_config
        session['session_config_time_only'] = True
        session['session_config_auto_generated'] = False
        session_config_time_only = True

    return render_template(
        'configure_attendance.html',
        mode=mode,
        file_names=file_names,
        session_config=session_config,
        session_config_filename=session_config_filename,
        session_config_time_only=session_config_time_only,
        session_config_auto_generated=session_config_auto_generated,
        default_session_patterns=_default_session_pattern_rows(),
        default_time_required=AUTO_TIME_REQUIRED_MINUTES,
        show_navigation=True
    )

@app.route('/upload_session_config', methods=['POST'])
@login_required
def upload_session_config():
    try:
        file = request.files.get('session_config_csv')
        if not file or not file.filename:
            flash('Please choose a Session Config CSV file to upload.')
            return redirect(url_for('configure_attendance_sessions'))

        # Save temporary
        suffix = secrets.token_hex(8)
        filename = f"{suffix}_" + secure_filename(file.filename)
        temp_path = os.path.join(TEMP_DIR, filename)
        file.save(temp_path)

        # Parse CSV
        df = pd.read_csv(temp_path)
        df.columns = df.columns.str.strip()
        required = ["Session Start", "Session End", "Time Required"]
        for col in required:
            if col not in df.columns:
                flash(f"Session Config CSV must contain column '{col}'.")
                return redirect(url_for('configure_attendance_sessions'))
        has_file = 'File' in df.columns
        rows = []
        for _, row in df.iterrows():
            try:
                s = row["Session Start"]
                e = row["Session End"]
                tr = float(row["Time Required"]) if pd.notna(row["Time Required"]) else AUTO_TIME_REQUIRED_MINUTES
                # Parse and strip date -> keep HH:MM only
                def _parse_any(x):
                    if pd.isna(x):
                        return None
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
                        try:
                            return datetime.strptime(str(x).strip(), fmt)
                        except Exception:
                            continue
                    try:
                        return pd.to_datetime(x).to_pydatetime()
                    except Exception:
                        return None
                sd = _parse_any(s)
                ed = _parse_any(e)
                if not sd or not ed:
                    continue
                file_val = str(row['File']).strip() if has_file and pd.notna(row['File']) else None
                rows.append({
                    'file': file_val,
                    'start_hm': sd.strftime('%H:%M'),
                    'end_hm': ed.strftime('%H:%M'),
                    'time_required': tr
                })
            except Exception:
                continue

        if not rows:
            flash('No valid session configurations found in uploaded CSV.')
            return redirect(url_for('configure_attendance_sessions'))

        # Store in session
        session['session_config'] = rows
        session['session_config_filename'] = file.filename
        session['session_config_time_only'] = True
        session['session_config_auto_generated'] = False
        flash('Session configuration loaded. You can now import sessions from it.', 'success')
        return redirect(url_for('configure_attendance_sessions'))
    except Exception as e:
        flash(f'Failed to load session configuration: {str(e)}')
        return redirect(url_for('configure_attendance_sessions'))


def _parse_sessions_by_file_from_form(mode, file_paths, file_names):
    """Turn the Configure Attendance Sessions form into {file_path: {"file_name":..., "sessions":[...]}}.

    Shared by /process_attendance (the real generation run) and /preview_attendance (the
    read-only "Preview" check) so the two never drift apart -- both resolve session
    start/end/time_required/grace the exact same way from the exact same form fields.

    Returns (sessions_by_file, error_message). error_message is None on success; on
    failure sessions_by_file is None and error_message describes what went wrong.
    """
    date_mode = request.form.get('date_mode', 'per_session')
    session_row_count = int(request.form.get('session_row_count', 0))
    session_order = []
    for raw_index in request.form.get('session_order', '').split(','):
        raw_index = raw_index.strip()
        if not raw_index:
            continue
        try:
            session_index_value = int(raw_index)
        except ValueError:
            continue
        if session_index_value >= 0 and session_index_value not in session_order:
            session_order.append(session_index_value)

    if session_row_count <= 0:
        return None, 'Please add at least one session configuration.'
    if not session_order:
        session_order = list(range(session_row_count))

    # Global date handling
    global_date_str = request.form.get('global_date') if date_mode == 'global' else None
    global_date = None
    if global_date_str:
        try:
            global_date = datetime.strptime(global_date_str, '%Y-%m-%d').date()
        except Exception as e:
            return None, f'Invalid global date format: {str(e)}'

    # Early-arrival exception (grace period): how much below a session's Min. Duration
    # a participant can fall and still be marked Present. Applies to every session in
    # this submission.
    grace_period_enabled = request.form.get('grace_period_enabled') == '1'
    if grace_period_enabled:
        try:
            grace_period_value = float(request.form.get('grace_period_value', DEFAULT_PRESENCE_GRACE_SECONDS))
        except (TypeError, ValueError):
            grace_period_value = DEFAULT_PRESENCE_GRACE_SECONDS
        grace_period_unit = request.form.get('grace_period_unit', 'seconds')
        grace_seconds = grace_period_value * 60 if grace_period_unit == 'minutes' else grace_period_value
        grace_seconds = max(0.0, grace_seconds)
    else:
        grace_seconds = 0

    # Collect session info from form
    sessions_by_file = {}
    for i in session_order:
        # In single mode, file_name is not in the form, so get it from the session
        if mode == 'single':
            file_name = file_names[0] if file_names else None
        else:
            file_name = request.form.get(f'file_name_{i}')

        start_str = request.form.get(f'start_time_{i}')
        end_str = request.form.get(f'end_time_{i}')
        time_required = float(request.form.get(f'time_required_{i}', AUTO_TIME_REQUIRED_MINUTES))

        if not file_name or not start_str or not end_str:
            continue

        # Find the corresponding file path
        file_path = next((fp for fn, fp in zip(file_names, file_paths) if fn == file_name), None)
        if not file_path:
            continue

        # Parse start and end times
        try:
            if date_mode == 'global' and global_date:
                start_dt = datetime.strptime(start_str, '%H:%M').time()
                end_dt = datetime.strptime(end_str, '%H:%M').time()
                session_start = datetime.combine(global_date, start_dt)
                session_end = datetime.combine(global_date, end_dt)
            else:
                session_start = datetime.strptime(start_str, '%Y-%m-%dT%H:%M')
                session_end = datetime.strptime(end_str, '%Y-%m-%dT%H:%M')
        except Exception as e:
            return None, f'Invalid date/time format for session in file {file_name}: {str(e)}'

        if session_start >= session_end:
            return None, f'Error in session for file {file_name}: Start time must be before end time.'

        # Store session info
        if file_path not in sessions_by_file:
            sessions_by_file[file_path] = {"file_name": file_name, "sessions": []}
        sessions_by_file[file_path]["sessions"].append({
            "session_start": session_start,
            "session_end": session_end,
            "time_required": time_required,
            "grace_seconds": grace_seconds
        })

    if not sessions_by_file:
        return None, 'No valid sessions were configured. Please check your inputs.'

    return sessions_by_file, None


def _read_raw_log_df(file_path, file_name):
    """Read the raw uploaded file's contents verbatim (no header row, no parsing) --
    exactly what /process_attendance writes to Sheet1 of the final output. Shared with
    the Preview's Sheet1 view so both show identical data.
    """
    if file_path.lower().endswith(('.xlsx', '.xls')):
        try:
            return pd.read_excel(file_path, header=None)
        except Exception as e:
            raise ValueError(f"Error reading Excel file '{file_name}': {e}")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            raw_data = list(csv.reader(f))
        return pd.DataFrame(raw_data)
    except UnicodeDecodeError:
        # Try fallback encoding if UTF-8 fails
        with open(file_path, 'r', encoding='latin-1') as f:
            raw_data = list(csv.reader(f))
        return pd.DataFrame(raw_data)


def _coerce_preview_value(value):
    """Best-effort turn an edited preview cell's string back into a number when it
    looks like one, so numeric columns (durations, etc.) still write as numbers in the
    output file rather than as text after being edited in the browser."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return stripped
    try:
        as_float = float(stripped)
    except ValueError:
        return value
    return int(as_float) if as_float.is_integer() else as_float


def _output_records_from_override(override):
    """Reconstruct output_records (list of dicts keyed by the original column labels)
    from an edited Preview grid -- exactly the shape process_sessions_for_file would
    have produced -- so write_excel() downstream can't tell the difference between a
    fresh computation and a user-reviewed/edited one.

    Column ORDER must come from override['columns'] (a JSON array, so its order is
    guaranteed) -- never from iterating override['column_labels'] as a dict. jsonify()
    alphabetically re-sorts dict keys by default, so a dict round-tripped through the
    browser can no longer be trusted for ordering, even though its values are still
    correct; using it for iteration order silently scrambled the generated file's
    column order into A-Z instead of Name/Email/Join Time/.../Total Duration/....
    """
    ordered_slugs = override.get('columns') or []
    column_labels = override.get('column_labels') or {}
    if not ordered_slugs:
        # Back-compat for any override payload saved before "columns" was included.
        ordered_slugs = list(column_labels.keys())

    records = []
    for row in override.get('rows') or []:
        record = {}
        for slug in ordered_slugs:
            label = column_labels.get(slug, slug)
            record[label] = _coerce_preview_value(row.get(slug, ''))
        records.append(record)
    return records


@app.route('/process_attendance', methods=['POST'])
@login_required
def process_attendance():
    try:
        mode = session.get('mode', 'single')
        user_email = session.get('user_id')

        file_paths = session.get('file_paths', [])
        file_names = session.get('file_names', [])

        if not file_paths:
            flash('No files found in session. Please upload your files again.', 'error')
            return redirect(url_for('attendance_generator'))

        # Verify all files still exist on disk
        missing_files = []
        valid_paths = []
        valid_names = []
        for fp, fn in zip(file_paths, file_names):
            if os.path.exists(fp):
                valid_paths.append(fp)
                valid_names.append(fn)
            else:
                missing_files.append(fn)

        if missing_files:
            if not valid_paths:
                flash(f'All uploaded files have expired or were removed. Please upload again. Files may expire after 2 hours of inactivity.', 'error')
                # Clear session data
                session.pop('file_paths', None)
                session.pop('file_names', None)
                return redirect(url_for('attendance_generator'))
            else:
                flash(f'Some files were not found and will be skipped: {", ".join(missing_files)}. Please re-upload if needed.', 'warning')
                # Update session with valid files only
                session['file_paths'] = valid_paths
                session['file_names'] = valid_names
                file_paths = valid_paths
                file_names = valid_names

        # Get session configurations from form (shared with /preview_attendance so both
        # resolve start/end/time_required/grace identically)
        sessions_by_file, error_message = _parse_sessions_by_file_from_form(mode, file_paths, file_names)
        if error_message:
            flash(error_message)
            return redirect(url_for('configure_attendance_sessions'))

        # If the instructor opened Preview and edited anything there, use that data
        # verbatim for the matching file(s) instead of recomputing from the raw log --
        # this is what makes Preview a real pre-flight edit step rather than a read-only
        # look. Files never previewed (or missing/malformed here) fall back to the
        # normal computation below, unchanged.
        overrides_by_filename = {}
        preview_overrides_raw = request.form.get('preview_overrides', '')
        if preview_overrides_raw:
            try:
                import json
                overrides_by_filename = json.loads(preview_overrides_raw)
            except (TypeError, ValueError) as e:
                print(f"[Attendance Generator] Ignoring malformed preview_overrides: {e}")
                overrides_by_filename = {}

        # Process each file
        output_files = []
        for file_path, data in sessions_by_file.items():
            try:
                # Check if file still exists before processing
                if not os.path.exists(file_path):
                    flash(f'File {data["file_name"]} no longer exists. Please upload again.', 'error')
                    continue

                start_time = time.time()
                override = overrides_by_filename.get(data['file_name'])
                if override:
                    output_records = _output_records_from_override(override)
                else:
                    output_records, _, _ = process_sessions_for_file(file_path, data["sessions"])
                processing_time = time.time() - start_time

                # Read raw log data for Excel output (support CSV and Excel uploads)
                raw_log_df = _read_raw_log_df(file_path, data['file_name'])

                # Create output Excel file with app-specific naming
                app_name_display = get_app_setting('app1_name', 'Zoom_Attendance_Analyzer')

                # Format app name for filename (replace spaces with underscores)
                app_name_for_file = app_name_display.replace(' ', '_').replace('-', '_')

                # Get original filename (strip random hex prefix from stored temp name)
                stored_name = data["file_name"]
                base_name = stored_name.split('_', 1)[1] if '_' in stored_name else stored_name
                # Remove extension from original name
                base_root = os.path.splitext(base_name)[0]

                # Apply configured column hiding/renaming, if any
                custom_columns, hidden_columns = get_output_column_config('app1')

                # write_excel() always writes real .xlsx bytes, so the filename always
                # ends in .xlsx regardless of any other setting.
                output_filename = f"{app_name_for_file}_{base_root}.xlsx"
                output_path = os.path.join(TEMP_DIR, output_filename)
                write_excel(raw_log_df, output_records, output_path, custom_columns=custom_columns, hidden_columns=hidden_columns)
                
                # Register processed file in database immediately (not eligible for deletion yet)
                # This protects the file during the entire workflow
                # Will be marked eligible for deletion after batch completion
                register_processed_file(
                    file_path=output_path,
                    file_name=output_filename,
                    user_email=user_email,
                    processing_type='attendance_generation',
                    eligible_for_deletion_at=None  # Not eligible yet - will be set after completion
                )
                
                output_files.append({'path': output_path, 'name': output_filename})

                # Log processing
                file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
                log_file_processing(
                    user_email=user_email,
                    file_name=data["file_name"],
                    processing_type='attendance_generation',
                    file_size=file_size,
                    processing_time=processing_time
                )
            except Exception as e:
                flash(f'Error processing file {data["file_name"]}: {str(e)}')
                return redirect(url_for('configure_attendance_sessions'))

        # Store output files in session for download
        session['attendance_output_files'] = output_files
        flash('Processing Complete! Your attendance reports have been generated.', 'success')
        
        # Mark all files as eligible for deletion after 2 hours from now
        # This happens AFTER all processing is complete, ensuring files are protected during the entire workflow
        eligible_time = (datetime.now() + timedelta(minutes=120)).strftime('%Y-%m-%d %H:%M:%S')
        for file_info in output_files:
            mark_file_eligible_for_deletion(file_info['path'], eligible_time)

        # Redirect to Processed Files page where users can download their files
        flash(f'Successfully processed {len(output_files)} file(s). Download from below.', 'success')
        return redirect(url_for('processed_files_management'))
        
    except Exception as e:
        flash(f'Error processing attendance: {str(e)}')
        return redirect(url_for('configure_attendance_sessions'))


def _slugify_column(col):
    """Turn an arbitrary result column name into a safe JSON/HTML key."""
    s = re.sub(r'[^a-zA-Z0-9]+', '_', str(col).strip().lower()).strip('_')
    return s or 'col'


def _build_attendance_preview_for_file(file_path, sessions):
    """Build a JSON-able preview of one file's attendance results.

    Reuses process_sessions_for_file() -- the exact same function /process_attendance
    calls to generate the real report -- so duplicate join/leave rows are merged and
    present/absent status is computed identically here; nothing is reimplemented.
    Also reuses filter_excluded_records() so a participant who'd be excluded from the
    final report is excluded from the preview too, instead of showing up there and then
    mysteriously disappearing from the generated file.
    """
    output_records, _session_labels, _session_summary = process_sessions_for_file(file_path, sessions)
    output_records = filter_excluded_records(output_records)
    if not output_records:
        return [], [], {}, 'name'

    # Union of keys across ALL records, not just the first one: fields like
    # "Shortfall Reason" only exist on records that actually had an absence, so deriving
    # columns from a single record would silently drop that column (and its data on
    # every other row) whenever the first participant happened to be fully present.
    ordered_keys = []
    seen_keys = set()
    for record in output_records:
        for key in record.keys():
            if key not in seen_keys:
                seen_keys.add(key)
                ordered_keys.append(key)

    columns = [_slugify_column(k) for k in ordered_keys]
    column_labels = {_slugify_column(k): k for k in ordered_keys}
    name_key = columns[0]

    rows = []
    for record in output_records:
        row = {}
        for key in ordered_keys:
            val = record.get(key, '')
            row[_slugify_column(key)] = '' if val is None else str(val)
        rows.append(row)

    return rows, columns, column_labels, name_key


def _attendance_preview_warning(file_path, sessions, output_rows):
    """Explain an all-absent preview without changing the computed result."""
    if not output_rows or not sessions:
        return None

    status_keys = [key for key in output_rows[0] if str(key).lower().startswith('session_')]
    if not status_keys or not all(
        str(row.get(key, '')).strip().upper() == 'A'
        for row in output_rows for key in status_keys
    ):
        return None

    try:
        global_times = get_global_times(file_path)
        has_session_overlap = any(
            pd.Timestamp(join_time) < pd.Timestamp(session['session_end'])
            and pd.Timestamp(leave_time) > pd.Timestamp(session['session_start'])
            for join_time, leave_time in global_times.values()
            for session in sessions
        )
    except Exception:
        has_session_overlap = True

    if not has_session_overlap:
        return 'All students are currently Absent because the configured session date/time does not overlap the Join/Leave times in the uploaded file.'
    return 'All students are currently Absent because their attendance duration is below the configured Required Duration for the session(s).'


def _build_sheet1_preview(file_path, file_name):
    """Build a JSON-able preview of Sheet1 -- the raw uploaded file, completely
    unfiltered (Sheet1 in the real output is never touched by exclusion filtering
    either, so this stays consistent with the final file)."""
    raw_log_df = _read_raw_log_df(file_path, file_name)
    raw_log_df = raw_log_df.astype(object).where(pd.notna(raw_log_df), '')
    return [[('' if v is None else str(v)) for v in row] for row in raw_log_df.values.tolist()]


@app.route('/preview_attendance', methods=['POST'])
@login_required
def preview_attendance():
    """Read-only, in-browser preview of attendance results for the Configure Attendance
    Sessions page's "Preview" button. Uses the exact same session-resolution
    (_parse_sessions_by_file_from_form) and duplicate-merge/status logic
    (process_sessions_for_file) as the real /process_attendance run, but only computes
    in memory: no output file is written, nothing is registered as a processed file or
    logged, and no database row is touched."""
    try:
        mode = session.get('mode', 'single')
        file_paths = session.get('file_paths', [])
        file_names = session.get('file_names', [])

        if not file_paths:
            return jsonify({'success': False, 'message': 'No files found in session. Please upload your files again.'}), 400

        sessions_by_file, error_message = _parse_sessions_by_file_from_form(mode, file_paths, file_names)
        if error_message:
            return jsonify({'success': False, 'message': error_message}), 400

        files_preview = []
        for file_path, data in sessions_by_file.items():
            if not os.path.exists(file_path):
                return jsonify({'success': False, 'message': f'File {data["file_name"]} no longer exists. Please upload again.'}), 400
            try:
                rows, columns, column_labels, name_key = _build_attendance_preview_for_file(file_path, data['sessions'])
                sheet1_rows = _build_sheet1_preview(file_path, data['file_name'])
                warning = _attendance_preview_warning(file_path, data['sessions'], rows)
            except Exception as e:
                return jsonify({'success': False, 'message': f'Error previewing file {data["file_name"]}: {e}'}), 400
            files_preview.append({
                'file_name': data['file_name'],
                'attendance': {
                    'columns': columns,
                    'column_labels': column_labels,
                    'name_key': name_key,
                    'rows': rows,
                },
                'sheet1': {
                    'rows': sheet1_rows,
                },
                'warning': warning,
            })

        return jsonify({'success': True, 'files': files_preview})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error building preview: {e}'}), 400


@app.route('/download_attendance')
@login_required
def download_attendance():
    output_files = session.get('attendance_output_files', [])
    
    if not output_files:
        flash('No processed files found.')
        return redirect(url_for('attendance_generator'))

    # Check if a specific file is requested for download
    requested_file = request.args.get('file')
    if requested_file:
        for file_info in output_files:
            if file_info['name'] == requested_file:
                # Check if file exists before sending
                if os.path.exists(file_info['path']):
                    # Don't delete file - it's tracked in database and will be auto-deleted later
                    return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])
                else:
                    flash(f'File {file_info["name"]} no longer exists.', 'error')
                    return redirect(url_for('attendance_generator'))
    
    # Check if download=true parameter is set (for auto-download)
    if request.args.get('download') == 'true':
        if len(output_files) == 1:
            file_info = output_files[0]
            if os.path.exists(file_info['path']):
                return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])
            else:
                flash('Processed file no longer exists. Please process again.', 'error')
                return redirect(url_for('attendance_generator'))
        else:
            # Multiple files - create zip
            zip_filename = 'attendance_reports.zip'
            zip_path = os.path.join(TEMP_DIR, zip_filename)
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                for file_info in output_files:
                    if os.path.exists(file_info['path']):
                        zipf.write(file_info['path'], file_info['name'])
            if os.path.exists(zip_path):
                return send_file(zip_path, as_attachment=True, download_name=zip_filename)
            else:
                flash('Error creating zip file.', 'error')
                return redirect(url_for('attendance_generator'))

    # If single file, auto-download it
    if len(output_files) == 1:
        file_info = output_files[0]
        if os.path.exists(file_info['path']):
            return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])
        else:
            flash('Processed file no longer exists. Please process again.', 'error')
            return redirect(url_for('attendance_generator'))

    # If not a specific file request and multiple files, render the download page
    app_names = get_app_names()
    return render_template('download_attendance.html',
                         files=output_files,
                         single_file=len(output_files) == 1,
                         app_names=app_names)

@app.route('/download_session_config_template')
@login_required
def download_session_config_template():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Session Start','Session End','Time Required','File'])
    for s,e,tr in _default_session_template_csv_rows():
        writer.writerow([s,e,tr,''])
    mem = io.BytesIO(buf.getvalue().encode('utf-8'))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name='session_config_template.csv', mimetype='text/csv')

# ----------- Raw Excel Generator Routes -----------
@app.route('/raw_excel_generator')
def raw_excel_generator():
    # Allow public viewing of the tool UI
    app_names = get_app_names()
    return render_template('raw_excel_generator.html', show_navigation=True, app_names=app_names)

@app.route('/process_raw_excel', methods=['POST'])
@login_required
def process_raw_excel():
    try:
        # Get uploaded files
        files = request.files.getlist('excel_files')
        user_email = session.get('user_id')
        
        if not files or not any(f.filename for f in files):
            flash('No files selected')
            return redirect(url_for('raw_excel_generator'))
        
        # Process files in memory and return direct downloads (no storage)
        processed_data = []
        
        for file in files:
            if file.filename:
                # Read file into memory
                file_content = file.read()
                file.seek(0)  # Reset file pointer
                
                # Start timing the processing
                start_time = time.time()
                
                # Process the file in memory using BytesIO
                file_stream = io.BytesIO(file_content)
                raw_df = extract_raw_from_excel(file_stream)
                
                # Calculate processing time
                processing_time = time.time() - start_time
                
                # Generate output filename
                base_name = secure_filename(file.filename)
                base_root = os.path.splitext(base_name)[0]
                output_filename = f"Raw Excel Generator - {base_root}-RAW.xlsx"
                
                # Create Excel file in memory
                output_buffer = io.BytesIO()
                with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer:
                    raw_df.to_excel(writer, index=False)
                output_buffer.seek(0)
                
                processed_data.append({
                    'buffer': output_buffer,
                    'filename': output_filename
                })
                
                # Log the file processing (file size from content length)
                file_size = len(file_content)
                log_file_processing(
                    user_email=user_email,
                    file_name=file.filename,
                    processing_type='raw_excel_generation',
                    file_size=file_size,
                    processing_time=processing_time
                )
        
        # If only one file, return direct download
        if len(processed_data) == 1:
            data = processed_data[0]
            return send_file(
                data['buffer'],
                as_attachment=True,
                download_name=data['filename'],
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
        else:
            # Multiple files: create zip in memory
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w') as zipf:
                for data in processed_data:
                    zipf.writestr(data['filename'], data['buffer'].getvalue())
            zip_buffer.seek(0)
            
            return send_file(
                zip_buffer,
                as_attachment=True,
                download_name='raw_excel_files.zip',
                mimetype='application/zip'
            )
        
    except Exception as e:
        flash(f'Error processing files: {str(e)}')
        return redirect(url_for('raw_excel_generator'))

@app.route('/download_raw_excel')
@login_required
def download_raw_excel():
    output_files = session.get('raw_output_files', [])
    
    # Check if a specific file is requested
    requested_file = request.args.get('file')
    if requested_file:
        for file_info in output_files:
            if file_info['name'] == requested_file:
                if os.path.exists(file_info['path']):
                    response = send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])
                    # Clean up the temporary file after sending
                    try:
                        os.remove(file_info['path'])
                    except OSError:
                        pass  # File might already be deleted
                    return response
                else:
                    flash(f'File {file_info["name"]} no longer exists.', 'error')
                    return redirect(url_for('raw_excel_generator'))
    
    if not output_files:
        flash('No processed files found.')
        return redirect(url_for('raw_excel_generator'))
    
    # If only one file, download it directly
    if len(output_files) == 1:
        file_info = output_files[0]
        if os.path.exists(file_info['path']):
            response = send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])
            # Clean up the temporary file after sending
            try:
                os.remove(file_info['path'])
            except OSError:
                pass  # File might already be deleted
            return response
        else:
            flash('Processed file no longer exists. Please process again.', 'error')
            return redirect(url_for('raw_excel_generator'))
    else:
        # Create a zip file with all outputs
        zip_filename = 'raw_excel_files.zip'
        zip_path = os.path.join(TEMP_DIR, zip_filename)
        
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for file_info in output_files:
                zipf.write(file_info['path'], file_info['name'])
        
        response = send_file(zip_path, as_attachment=True, download_name=zip_filename)
        # Clean up the temporary zip file after sending
        try:
            os.remove(zip_path)
        except OSError:
            pass  # File might already be deleted
        return response

# ----------- Attendance Matching Routes -----------
@app.route('/attendance_matching')
def attendance_matching():
    """Attendance Matching UI. Public can view, but batch mode requires login and permission."""
    can_use_batch_matching_flag = False
    batches = []
    batch_counts = {}
    groups = []
    subgroups = []
    if 'user_id' in session:
        # Trust session role for superadmin, fall back to DB for flags
        session_role = session.get('role')
        session_user = session.get('user_id')
        if session_role == 'superadmin':
            can_use_batch_matching_flag = True
        else:
            try:
                user_row = get_user(session_user)
                user = dict(user_row) if user_row is not None and hasattr(user_row, 'keys') else user_row
            except Exception:
                user = None
            if user and user.get('can_use_batch_matching'):
                can_use_batch_matching_flag = True
        
        if can_use_batch_matching_flag:
            batches = get_batches_for_user(session_user, session_role, is_completed=0)
            groups = get_groups_for_user(session_user, session_role)
            subgroups = get_subgroups_for_user(session_user, session_role, is_completed=0)
            active_subgroup_ids = {sg['id'] for sg in subgroups}
            # A batch inside a completed sub-group is excluded too, even though the batch
            # itself is still active, so it disappears from Smart Matcher along with its sub-group.
            batches = [b for b in batches if not b['subgroup_id'] or b['subgroup_id'] in active_subgroup_ids]
            for b in batches:
                try:
                    students = get_batch_students(b['id'])
                    batch_counts[b['id']] = len(students)
                except Exception:
                    batch_counts[b['id']] = 0
    app_names = get_app_names()
    return render_template(
        'attendance_matching.html',
        show_navigation=True,
        can_use_batch_matching=can_use_batch_matching_flag,
        batches=batches,
        batch_counts=batch_counts,
        groups=groups,
        subgroups=subgroups,
        app_names=app_names
    )


@app.route('/smart_matcher_auto')
@login_required
def smart_matcher_auto():
    """Show generated Attendance files and conservative batch suggestions."""
    user_email = session.get('user_id')
    user_files = _server_files_for_user(user_email, 'attendance_generation')
    batches = get_batches_for_user(user_email, session.get('role'), is_completed=0)
    files = []
    for file in user_files:
        suggested_batch, score = _detect_batch_for_filename(file['file_name'], batches)
        files.append({
            'id': file['id'],
            'name': _processed_file_display_name(file),
            'created_at': file.get('created_at'),
            'suggested_batch_id': suggested_batch['id'] if suggested_batch else None,
            'suggested_batch_name': suggested_batch['name'] if suggested_batch else '',
            'suggestion_score': score,
        })
    return render_template('smart_matcher_auto.html', files=files, batches=batches, app_names=get_app_names())


@app.route('/final_excel_auto')
@login_required
def final_excel_auto():
    """Show only 100% Smart Matcher workbooks available on the server."""
    files = [
        dict(file) for file in _server_files_for_user(session.get('user_id'), 'attendance_matching')
        if str(file.get('file_name', '')).lower().endswith(('.xlsx', '.xls'))
        and '_100_' in str(file.get('file_name', ''))
    ]
    for file in files:
        file['display_name'] = _processed_file_display_name(file)
    return render_template('final_excel_auto.html', files=files, app_names=get_app_names())


@app.route('/download_attendance_matching')
@login_required
def download_attendance_matching():
    output_files = session.get('matching_output_files', [])
    
    # Check if a specific file is requested
    requested_file = request.args.get('file')
    if requested_file:
        for file_info in output_files:
            if file_info['name'] == requested_file:
                if os.path.exists(file_info['path']):
                    # Don't delete file - it's tracked in database and will be auto-deleted later
                    return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])
                else:
                    flash(f'File {file_info["name"]} no longer exists.', 'error')
                    return redirect(url_for('attendance_matching'))
    
    if not output_files:
        flash('No processed files found.')
        return redirect(url_for('attendance_matching'))
    
    # If only one file, download it directly
    if len(output_files) == 1:
        file_info = output_files[0]
        if os.path.exists(file_info['path']):
            # Don't delete file - it's tracked in database and will be auto-deleted later
            return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])
        else:
            flash('Processed file no longer exists. Please process again.', 'error')
            return redirect(url_for('attendance_matching'))
    else:
        # Create a zip file with all outputs (temporary zip, can be deleted immediately)
        zip_filename = 'matching_results.zip'
        zip_path = os.path.join(TEMP_DIR, zip_filename)
        
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for file_info in output_files:
                zipf.write(file_info['path'], file_info['name'])
        
        response = send_file(zip_path, as_attachment=True, download_name=zip_filename)
        # Clean up the temporary zip file after sending (zip is not tracked)
        try:
            os.remove(zip_path)
        except OSError:
            pass  # File might already be deleted
        return response

# ----------- Processed Files Management Routes (Superadmin Only) -----------
@app.route('/admin/processed_files')
@login_required
def processed_files_management():
    """List processed files - Superadmin sees all, other users see only their own"""
    user_email = session.get('user_id')
    user_role = session.get('role')
    
    # Nobody sees anyone else's output by default - not even a superadmin, who
    # has to ask for it explicitly with the owner picker. Regular users see what
    # they made plus whatever has been shared with them.
    owner_filter = (request.args.get('owner') or '').strip()
    owner_options = []
    if user_role == 'superadmin':
        try:
            owner_options = sorted({
                dict(record).get('user_email')
                for record in get_processed_files()
                if dict(record).get('user_email')
            })
        except Exception as exc:
            print(f'Could not build the owner list: {exc}')
            owner_options = []
    else:
        # The picker is a superadmin control; ignore the parameter for everyone
        # else so it cannot be used to look at another account's files.
        owner_filter = ''

    try:
        if user_role == 'superadmin' and owner_filter == 'all':
            files = list(get_processed_files())
        elif user_role == 'superadmin' and owner_filter:
            files = list(get_processed_files(user_email=owner_filter))
        elif user_role == 'superadmin':
            # Default view: the superadmin's own files only.
            files = list(get_processed_files(user_email=user_email))
        else:
            # Regular users see their own files AND files shared with them
            # Get files owned by the user
            own_files = list(get_processed_files(user_email=user_email))
            
            # Get files shared with the user via permissions
            shared_files = []
            try:
                all_files = list(get_processed_files())  # Get all files to check permissions
                for f in all_files:
                    try:
                        if can_user_access_file(f['id'], user_email) and f['user_email'] != user_email:
                            # This file is shared with the user (not owned by them)
                            shared_files.append(f)
                    except Exception as e:
                        print(f"Error checking access for file {f.get('id')}: {e}")
                        continue  # Skip this file if there's an error
            except Exception as e:
                print(f"Error getting all files for permission check: {e}")
                # If we can't get all files, just use own files
                shared_files = []
            
            # Combine and deduplicate (in case of any duplicates)
            file_ids_seen = set()
            files = []
            for f in own_files + shared_files:
                if f['id'] not in file_ids_seen:
                    file_ids_seen.add(f['id'])
                    files.append(f)
    except Exception as e:
        print(f"Error getting processed files: {e}")
        import traceback
        traceback.print_exc()
        flash('Error loading processed files. Please try again.', 'error')
        return redirect(url_for('index'))
    
    # Format files with additional info
    files_list = []
    for f in files:
        file_info = dict(f)
        # Check if file still exists
        file_info['exists'] = os.path.exists(file_info['file_path'])
        # Calculate time remaining until deletion eligibility
        if file_info['eligible_for_deletion_at']:
            try:
                eligible_dt = datetime.strptime(file_info['eligible_for_deletion_at'], '%Y-%m-%d %H:%M:%S')
                now = datetime.now()
                if eligible_dt > now:
                    remaining = eligible_dt - now
                    file_info['time_remaining'] = f"{int(remaining.total_seconds() / 60)} minutes"
                else:
                    file_info['time_remaining'] = "Eligible for deletion"
            except Exception:
                file_info['time_remaining'] = "Unknown"
        else:
            file_info['time_remaining'] = "Not set"
        
        files_list.append(file_info)

    # Compute stats from the full list before paginating, so the summary cards
    # always reflect true totals regardless of which page is being viewed.
    total_files = len(files_list)
    available_files = sum(1 for f in files_list if f['exists'])
    expired_files = total_files - available_files

    # Group by the day the file was produced. The list already arrives newest
    # first, so the groups come out in order without a second sort. Paginating
    # by group rather than by file keeps a day's files together instead of
    # splitting them across two pages.
    today = datetime.now().date()
    groups = []
    group_index = {}
    for file_info in files_list:
        raw_created = str(file_info.get('created_at') or '')
        day_key = raw_created[:10] or 'unknown'

        if day_key not in group_index:
            try:
                day = datetime.strptime(day_key, '%Y-%m-%d').date()
                if day == today:
                    label = 'Today'
                elif (today - day).days == 1:
                    label = 'Yesterday'
                else:
                    label = day.strftime('%d %B %Y')
                sub_label = day.strftime('%A, %d %b %Y')
            except ValueError:
                label, sub_label = 'Undated', ''

            group_index[day_key] = len(groups)
            groups.append({
                'key': day_key,
                'label': label,
                'sub_label': sub_label,
                'files': [],
                'available': 0,
            })

        group = groups[group_index[day_key]]
        group['files'].append(file_info)
        if file_info['exists']:
            group['available'] += 1

    for group in groups:
        group['count'] = len(group['files'])

    groups_per_page = 10
    total_pages = max(1, (len(groups) + groups_per_page - 1) // groups_per_page)
    page = request.args.get('page', 1, type=int) or 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * groups_per_page
    paged_groups = groups[start:start + groups_per_page]

    return render_template(
        'processed_files.html',
        date_groups=paged_groups,
        files=[f for group in paged_groups for f in group['files']],
        total_files=total_files,
        available_files=available_files,
        expired_files=expired_files,
        page=page,
        total_pages=total_pages,
        owner_filter=owner_filter,
        owner_options=owner_options,
        is_superadmin_view=(user_role == 'superadmin'),
        attendance_reedit_available=bool(
            session.get('file_paths') and
            session.get('file_names') and
            any(os.path.exists(path) for path in session.get('file_paths', []))
        ),
        show_navigation=True
    )

def _parse_processed_file_ids():
    file_ids = []
    raw_values = request.args.getlist('file_ids') + request.args.getlist('file_id')
    for raw_value in raw_values:
        for part in str(raw_value).split(','):
            part = part.strip()
            if not part:
                continue
            try:
                file_id = int(part)
            except ValueError:
                continue
            if file_id not in file_ids:
                file_ids.append(file_id)
    return file_ids

def _safe_zip_member_name(display_name, used_names):
    base_name = os.path.basename(str(display_name).replace('\\', '/')) or 'processed_file'
    name, extension = os.path.splitext(base_name)
    candidate = base_name
    counter = 2
    while candidate in used_names:
        candidate = f"{name}_{counter}{extension}"
        counter += 1
    used_names.add(candidate)
    return candidate

# App-specific filename prefixes are applied when files are generated (see App1/App2/App3
# output naming above): f"{app_name_for_file}_{original_name}...". These map each processing
# type back to the same setting key/default used at generation time, so downloads can offer
# the file with or without that prefix.
_PROCESSED_FILE_APP_NAME_SETTINGS = {
    'attendance_generation': ('app1_name', 'Zoom_Attendance_Analyzer'),
    'attendance_matching': ('app2_name', 'Intelligent_Record_Matcher'),
    'final_excel': ('app3_name', 'Session_Report_Generator'),
}

def _app_name_prefix_for_processing_type(processing_type):
    setting = _PROCESSED_FILE_APP_NAME_SETTINGS.get(processing_type)
    if not setting:
        return None
    setting_key, default_name = setting
    app_name_display = get_app_setting(setting_key, default_name)
    return app_name_display.replace(' ', '_').replace('-', '_')

def _processed_file_download_name(file_record, naming_pref):
    """Resolve the filename to present for download, honoring the with/without-app-name choice."""
    file_name = file_record.get('file_name') or os.path.basename(file_record.get('file_path', 'processed_file'))
    if naming_pref != 'without_app_name':
        return file_name
    prefix = _app_name_prefix_for_processing_type(file_record.get('processing_type'))
    if prefix and file_name.startswith(prefix + '_'):
        stripped = file_name[len(prefix) + 1:]
        return stripped or file_name
    return file_name


def _processed_file_display_name(file_record):
    """Return an app-prefix-free label for server-file automation UIs only."""
    file_name = _processed_file_download_name(file_record, 'without_app_name')
    if file_record.get('processing_type') == 'attendance_matching':
        # Also handle files generated before an app-name setting was renamed.
        file_name = re.sub(r'^.+_(?=(?:100|90)_)', '', file_name) or file_name
        source_prefixes = {
            _app_name_prefix_for_processing_type('attendance_generation'),
            'Attendance_Generator',
            'Zoom_Attendance_Analyzer',
        }
        prefix_pattern = '|'.join(re.escape(prefix) for prefix in source_prefixes if prefix)
        if prefix_pattern:
            file_name = re.sub(rf'^((?:100|90)_)(?:{prefix_pattern})_', r'\1', file_name)
    return file_name

@app.route('/admin/processed_files/download/<int:file_id>')
@login_required
def download_processed_file(file_id):
    """Download a processed file - Superadmin, owner, or user with granted access"""
    user_email = session.get('user_id')
    user_role = session.get('role')
    
    # Get file record - check all files to find the one requested
    all_files = get_processed_files()
    file_record = None
    for f in all_files:
        if f['id'] == file_id:
            file_record = dict(f)
            break
    
    if not file_record:
        flash('File not found or access denied.', 'error')
        return redirect(url_for('processed_files_management'))
    
    # Security check: ensure user has access (superadmin, owner, or has permission)
    if user_role != 'superadmin':
        if not can_user_access_file(file_id, user_email):
            flash('Access denied. You do not have permission to download this file.', 'error')
            return redirect(url_for('processed_files_management'))
    
    if not os.path.exists(file_record['file_path']):
        flash('File no longer exists on server.', 'error')
        return redirect(url_for('processed_files_management'))

    naming_pref = request.args.get('naming', 'with_app_name')
    return send_file(
        file_record['file_path'],
        as_attachment=True,
        download_name=_processed_file_download_name(file_record, naming_pref)
    )

@app.route('/admin/processed_files/download_zip')
@login_required
def download_processed_files_zip():
    """Download selected or all accessible processed files as one ZIP archive."""
    user_email = session.get('user_id')
    user_role = session.get('role')
    requested_ids = _parse_processed_file_ids()
    requested_id_set = set(requested_ids) if requested_ids else None

    # Match whatever the page is showing. Without this a superadmin looking at
    # "Only mine" would still get everyone's files in the archive.
    owner_filter = (request.args.get('owner') or '').strip()
    if user_role != 'superadmin':
        owner_filter = ''

    if user_role == 'superadmin' and owner_filter == 'all':
        candidate_files = get_processed_files()
    elif user_role == 'superadmin' and owner_filter:
        candidate_files = get_processed_files(user_email=owner_filter)
    elif user_role == 'superadmin' and not requested_id_set:
        candidate_files = get_processed_files(user_email=user_email)
    else:
        candidate_files = get_processed_files()

    files_to_zip = []
    for f in candidate_files:
        file_record = dict(f)
        file_id = file_record['id']
        
        if requested_id_set is not None and file_id not in requested_id_set:
            continue
        
        if user_role != 'superadmin' and not can_user_access_file(file_id, user_email):
            continue
        
        if not os.path.exists(file_record['file_path']):
            continue
        
        files_to_zip.append(file_record)
    
    if requested_ids:
        requested_order = {file_id: index for index, file_id in enumerate(requested_ids)}
        files_to_zip.sort(key=lambda file_record: requested_order.get(file_record['id'], len(requested_order)))
    
    if not files_to_zip:
        flash('No downloadable files found for the ZIP archive.', 'error')
        return redirect(url_for('processed_files_management'))

    naming_pref = request.args.get('naming', 'with_app_name')
    zip_buffer = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file_record in files_to_zip:
            zipf.write(
                file_record['file_path'],
                _safe_zip_member_name(_processed_file_download_name(file_record, naming_pref), used_names)
            )
    zip_buffer.seek(0)

    # The ZIP archive itself always carries the application name, regardless of the
    # with/without-app-name choice applied to the files inside it.
    site_app_name = get_app_setting('app_name', 'Attendancify').replace(' ', '_').replace('-', '_')
    zip_filename = f'{site_app_name}_Processed_Files_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'
    return send_file(
        zip_buffer,
        as_attachment=True,
        download_name=zip_filename,
        mimetype='application/zip'
    )

@app.route('/admin/processed_files/delete/<int:file_id>', methods=['POST'])
@login_required
def delete_processed_file(file_id):
    """Delete a processed file manually - Superadmin only (users cannot delete files)"""
    user_email = session.get('user_id')
    user_role = session.get('role')
    
    if user_role != 'superadmin':
        flash('Access denied. Only superadmin can delete files.', 'error')
        return redirect(url_for('index'))
    
    # Get file record
    files = get_processed_files()
    file_record = None
    for f in files:
        if f['id'] == file_id:
            file_record = dict(f)
            break
    
    if not file_record:
        flash('File not found.', 'error')
        return redirect(url_for('processed_files_management'))
    
    # Delete file from disk
    if os.path.exists(file_record['file_path']):
        try:
            os.remove(file_record['file_path'])
        except OSError:
            pass
    
    # Mark as manually deleted in database
    mark_file_manually_deleted(file_record['file_path'])
    
    flash('File deleted successfully.', 'success')
    return redirect(url_for('processed_files_management'))

@app.route('/admin/processed_files/delete_selected', methods=['POST'])
@login_required
def delete_selected_processed_files():
    """Delete selected processed files in one request - Superadmin only."""
    if session.get('role') != 'superadmin':
        return jsonify({'success': False, 'message': 'Access denied. Only superadmin can delete files.'}), 403

    data = request.get_json(silent=True) or {}
    raw_ids = data.get('file_ids')
    if raw_ids is None:
        raw_ids = request.form.getlist('file_ids') or request.form.get('file_ids', '')
    if not isinstance(raw_ids, list):
        raw_ids = str(raw_ids).split(',')

    file_ids = []
    for raw_id in raw_ids:
        try:
            file_id = int(str(raw_id).strip())
        except (TypeError, ValueError):
            continue
        if file_id not in file_ids:
            file_ids.append(file_id)

    if not file_ids:
        return jsonify({'success': False, 'message': 'No files selected.'}), 400

    files_by_id = {int(f['id']): dict(f) for f in get_processed_files()}
    deleted_count = 0
    missing_count = 0

    for file_id in file_ids:
        file_record = files_by_id.get(file_id)
        if not file_record:
            missing_count += 1
            continue

        file_path = file_record['file_path']
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        mark_file_manually_deleted(file_path)
        deleted_count += 1

    return jsonify({
        'success': True,
        'deleted_count': deleted_count,
        'missing_count': missing_count,
        'message': f'Deleted {deleted_count} file(s).'
    })

@app.route('/admin/processed_files/rename/<int:file_id>', methods=['POST'])
@login_required
def rename_processed_file(file_id):
    """Rename a processed file - User can rename their own files"""
    user_email = session.get('user_id')
    user_role = session.get('role')
    
    # Get new name from request
    data = request.get_json()
    new_name = data.get('new_name', '').strip() if data else ''
    
    if not new_name:
        return {'success': False, 'message': 'New name is required'}
    
    # Get file record - check all files
    all_files = get_processed_files(include_deleted=False)
    file_record = None
    for f in all_files:
        if f['id'] == file_id:
            file_record = dict(f)
            break
    
    if not file_record:
        return {'success': False, 'message': 'File not found'}
    
    # Check permission: Superadmin can rename any, users can rename their own or shared files
    if user_role != 'superadmin':
        if not can_user_access_file(file_id, user_email):
            return {'success': False, 'message': 'Access denied'}
    
    # Update file name in database
    try:
        from database import update_processed_file_name
        if update_processed_file_name(file_id, new_name):
            return {'success': True, 'message': 'File renamed successfully'}
        else:
            return {'success': False, 'message': 'Failed to rename file'}
    except Exception as e:
        return {'success': False, 'message': str(e)}

@app.route('/admin/processed_files/<int:file_id>/permissions', methods=['GET', 'POST'])
@login_required
def manage_file_permissions(file_id):
    """Manage user permissions for a processed file - Superadmin only"""
    if session.get('role') != 'superadmin':
        flash('Access denied. Superadmin access required.', 'error')
        return redirect(url_for('index'))
    
    # Get file record
    files = get_processed_files()
    file_record = None
    for f in files:
        if f['id'] == file_id:
            file_record = dict(f)
            break
    
    if not file_record:
        flash('File not found.', 'error')
        return redirect(url_for('processed_files_management'))
    
    if request.method == 'POST':
        action = request.form.get('action')
        user_email = request.form.get('user_email', '').strip().lower()
        
        if action == 'grant':
            if user_email:
                if grant_file_access(file_id, user_email, session.get('user_id')):
                    flash(f'Access granted to {user_email}.', 'success')
                else:
                    flash(f'Error granting access to {user_email}.', 'error')
        elif action == 'revoke':
            if user_email:
                revoke_file_access(file_id, user_email)
                flash(f'Access revoked from {user_email}.', 'success')
    
    # Get current permissions
    permissions = get_file_permissions(file_id)
    all_users = get_all_users()
    
    return render_template(
        'file_permissions.html',
        file=file_record,
        permissions=permissions,
        all_users=all_users,
        show_navigation=True
    )


# ----------- Final Excel Generator Routes -----------
@app.route('/process_final_excel', methods=['POST'])
@login_required
def process_final_excel():
    """Process matched Excel files and generate individual session files"""
    try:
        files = request.files.getlist('excel_files')
        server_file_ids = request.form.getlist('server_file_id')
        output_format = request.form.get('output_format', 'xlsx')

        server_files = {
            str(file['id']): file
            for file in _server_files_for_user(session.get('user_id'), 'attendance_matching')
        }
        server_inputs = [server_files[file_id] for file_id in server_file_ids if file_id in server_files]

        if (not files or not any(f.filename for f in files)) and not server_inputs:
            flash('Please select at least one file to process.', 'error')
            return redirect(url_for('index'))

        # Unique token for this processing run's temp filenames (unrelated to the batches table --
        # kept as "batch_id" in the session/URLs below for backward compatibility with existing
        # download routes).
        processing_token = secrets.token_hex(8)
        processed_files = []

        input_items = [(file, None) for file in files if file.filename]
        input_items.extend((None, server_file) for server_file in server_inputs)
        for file, server_file in input_items:
            source_filename = file.filename if file else server_file['file_name']
            source_path = server_file['file_path'] if server_file else None
            if not source_filename:
                continue

            try:
                file_started_at = time.time()
                if source_path:
                    source_file_size = os.path.getsize(source_path)
                else:
                    file.stream.seek(0, os.SEEK_END)
                    source_file_size = file.stream.tell()
                    file.stream.seek(0)

                # Read Excel file
                xls = pd.ExcelFile(source_path or file)
                sheet_name = "Matched" if "Matched" in xls.sheet_names else xls.sheet_names[0]
                df = pd.read_excel(xls, sheet_name=sheet_name)

                # Find email column (matching original script logic)
                email_col = None
                for col in df.columns:
                    if re.search(r'email', str(col), re.IGNORECASE):
                        email_col = col
                        break
                # Try alternative email column names
                if not email_col:
                    for col in df.columns:
                        if re.search(r'email_id|e-mail|e mail', str(col), re.IGNORECASE):
                            email_col = col
                            break

                if not email_col:
                    flash(f'No email column found in {source_filename}. Skipping.', 'warning')
                    continue

                # Side effect only: figure out which stored batch this roster belongs to by
                # overlapping its emails (and, as a fallback, its Participant Names) against
                # every batch's student list, then learn Zoom-name -> roster-name pairs under
                # that batch. If no batch can be confidently identified, learning is skipped --
                # this never blocks or changes the session-file generation below.
                try:
                    roster_name_col = next((c for c in df.columns if str(c).strip().lower() == 'participant name'), None)
                    detected_batch_id = _detect_batch_for_roster(
                        df[email_col].tolist(),
                        df[roster_name_col].tolist() if roster_name_col is not None else None
                    )
                    if detected_batch_id:
                        _learn_batch_records_from_matched_sheet(df, detected_batch_id)
                except Exception as e:
                    print(f"[Final Excel Generator] Batch auto-detection/learning skipped for {source_filename}: {e}")


                # Find session columns (matching original script logic)
                session_cols = [c for c in df.columns if re.search(r'\bSession\b', str(c), re.IGNORECASE)]
                if not session_cols:
                    session_cols = [c for c in df.columns if 'Session' in str(c)]
                
                if not session_cols:
                    flash(f'No session columns found in {source_filename}. Skipping.', 'warning')
                    continue
                
                base_name = os.path.splitext(os.path.basename(secure_filename(source_filename)))[0]
                
                # Process each session column
                for col in session_cols:
                    # Normalize attendance values
                    def normalize_att(val):
                        if pd.isna(val):
                            return "absent"
                        s = str(val).strip().lower()
                        if 'n/a' in s or s == 'na':
                            return "absent"
                        if "present" in s:
                            return "present"
                        if "absent" in s:
                            return "absent"
                        if "leave" in s or "left" in s:
                            return "leave"
                        if re.search(r'\d', s):
                            return "present"
                        return "absent"
                    
                    # Create output dataframe
                    out_df = pd.DataFrame({
                        "email_id": df[email_col].astype(str).str.strip(),
                        "attendance(absent/present/leave)": df[col].apply(normalize_att)
                    })
                    
                    # Remove empty email rows
                    out_df = out_df[out_df['email_id'].str.len() > 0].reset_index(drop=True)
                    
                    # Get app-specific naming for App3 (Session Report Generator)
                    app_name_display = get_app_setting('app3_name', 'Session_Report_Generator')
                    app_name_for_file = app_name_display.replace(' ', '_').replace('-', '_')
                    
                    # Get original filename (base_name already has original filename)
                    # base_name is like "abc" if original file was "abc.xlsx"
                    
                    # Apply configured column hiding/renaming, if any
                    custom_columns, hidden_columns = get_output_column_config('app3')
                    if hidden_columns:
                        out_df = out_df.drop(columns=[c for c in hidden_columns if c in out_df.columns])
                    if custom_columns:
                        out_df = out_df.rename(columns=custom_columns)
                    
                    # Sanitize session label for filename
                    sess_label_clean = str(col)
                    sess_label_clean = sess_label_clean.replace(':', '_')
                    sess_label_clean = re.sub(r'[\\/*?:"<>|]', '_', sess_label_clean)
                    
                    # Generate filename: AppName_OriginalFileName_Session.ext
                    out_filename = f"{app_name_for_file}_{base_name}_{sess_label_clean}.{output_format}"
                    
                    # Save to temp directory
                    unique_filename = f"{processing_token}_{out_filename}"
                    file_path = os.path.join(TEMP_DIR, unique_filename)
                    
                    if output_format == 'xlsx':
                        with pd.ExcelWriter(file_path, engine='openpyxl') as writer:
                            out_df.to_excel(writer, sheet_name="Sheet2", index=False)
                    else:
                        out_df.to_csv(file_path, index=False)
                    
                    # Register file in database for Processed Files page
                    user_email = session.get('user_id')
                    # Set eligible for deletion after 2 hours
                    eligible_time = (datetime.now() + timedelta(minutes=120)).strftime('%Y-%m-%d %H:%M:%S')
                    register_processed_file(
                        file_path=file_path,
                        file_name=out_filename,
                        user_email=user_email,
                        processing_type='final_excel',
                        eligible_for_deletion_at=eligible_time
                    )
                    
                    # Track processed file
                    processed_files.append({
                        'filename': out_filename,
                        'path': file_path,
                        'unique_filename': unique_filename,
                        'source_file': source_filename,
                        'session': str(col),
                        'rows': len(out_df),
                        'processing_type': 'final_excel'
                    })
            
                # Record the run so this tool shows up in the dashboard's
                # Processing Breakdown (it used to generate files without ever
                # logging, so its card sat at zero no matter how much it was used).
                log_file_processing(
                    user_email=session.get('user_id'),
                    file_name=source_filename,
                    processing_type='final_excel',
                    file_size=source_file_size,
                    processing_time=round(time.time() - file_started_at, 3)
                )

            except Exception as e:
                flash(f'Error processing {source_filename}: {str(e)}', 'warning')
                continue

        if not processed_files:
            flash('No files were processed. Please check your input files.', 'error')
            return redirect(url_for('index'))
        
        # Store in session for the results page (kept for backward compatibility)
        session['final_excel_batch_id'] = processing_token
        session['final_excel_files'] = processed_files
        session['final_excel_format'] = output_format
        
        # Redirect to Processed Files page
        flash(f'Successfully processed {len(processed_files)} file(s). Download from below.', 'success')
        return redirect(url_for('processed_files_management'))
        
    except Exception as e:
        flash(f'Error processing files: {str(e)}', 'error')
        return redirect(url_for('index'))

@app.route('/final_excel_results')
@login_required
def final_excel_results():
    """Show processed files from Final Excel Generator"""
    batch_id = session.get('final_excel_batch_id')
    processed_files = session.get('final_excel_files', [])
    output_format = session.get('final_excel_format', 'xlsx')
    
    if not batch_id or not processed_files:
        flash('No processed files found. Please process files first.', 'warning')
        return redirect(url_for('index'))
    
    return render_template(
        'final_excel_results.html',
        batch_id=batch_id,
        processed_files=processed_files,
        output_format=output_format,
        show_navigation=True
    )

@app.route('/final_excel_download/<batch_id>/<filename>')
@login_required
def final_excel_download_file(batch_id, filename):
    """Download a single processed file from Final Excel Generator"""
    # Verify batch ID matches session
    if session.get('final_excel_batch_id') != batch_id:
        flash('Invalid download request.', 'error')
        return redirect(url_for('index'))
    
    processed_files = session.get('final_excel_files', [])
    
    # Find the file
    for f in processed_files:
        if f['unique_filename'] == filename:
            if os.path.exists(f['path']):
                download_name = _processed_file_download_name(f, request.args.get('naming', 'with_app_name'))
                return send_file(f['path'], as_attachment=True, download_name=download_name)
    
    flash('File not found.', 'error')
    return redirect(url_for('final_excel_results'))

@app.route('/final_excel_download_all/<batch_id>')
@login_required
def final_excel_download_all(batch_id):
    """Download all processed files as ZIP from Final Excel Generator"""
    # Verify batch ID matches session
    if session.get('final_excel_batch_id') != batch_id:
        flash('Invalid download request.', 'error')
        return redirect(url_for('index'))
    
    processed_files = session.get('final_excel_files', [])
    
    if not processed_files:
        flash('No files to download.', 'error')
        return redirect(url_for('index'))
    
    # Create ZIP in memory
    zip_buffer = io.BytesIO()
    
    naming_pref = request.args.get('naming', 'with_app_name')
    used_names = set()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for f in processed_files:
            if os.path.exists(f['path']):
                member_name = _safe_zip_member_name(_processed_file_download_name(f, naming_pref), used_names)
                zipf.write(f['path'], member_name)
    
    zip_buffer.seek(0)
    
    zip_name = _app_name_prefix_for_processing_type('final_excel') or 'Session_Report_Generator'
    return send_file(
        zip_buffer,
        as_attachment=True,
        download_name=f'{zip_name}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip',
        mimetype='application/zip'
    )

@app.route('/final_excel_cleanup/<batch_id>')
@login_required
def final_excel_cleanup(batch_id):
    """Clean up processed files and return to home"""
    # Verify batch ID matches session
    if session.get('final_excel_batch_id') == batch_id:
        processed_files = session.get('final_excel_files', [])
        
        # Delete temp files
        for f in processed_files:
            try:
                if os.path.exists(f['path']):
                    os.remove(f['path'])
            except:
                pass
        
        # Clear session
        session.pop('final_excel_batch_id', None)
        session.pop('final_excel_files', None)
        session.pop('final_excel_format', None)
    
    flash('Processing session cleared.', 'success')
    return redirect(url_for('index'))

@app.route('/final_excel_delete/<batch_id>/<filename>', methods=['DELETE'])
@login_required
def final_excel_delete_file(batch_id, filename):
    """Delete a single processed file from Final Excel Generator"""
    # Verify batch ID matches session
    if session.get('final_excel_batch_id') != batch_id:
        return jsonify({'success': False, 'error': 'Invalid request'})
    
    processed_files = session.get('final_excel_files', [])
    
    # Find and delete the file
    for i, f in enumerate(processed_files):
        if f['unique_filename'] == filename:
            try:
                if os.path.exists(f['path']):
                    os.remove(f['path'])
            except:
                pass
            
            # Remove from session
            processed_files.pop(i)
            session['final_excel_files'] = processed_files
            session.modified = True
            
            return jsonify({'success': True})
    
    return jsonify({'success': False, 'error': 'File not found'})

@app.route('/final_excel_rename/<batch_id>/<filename>', methods=['POST'])
@login_required
def final_excel_rename_file(batch_id, filename):
    """Rename a processed file from Final Excel Generator"""
    # Verify batch ID matches session
    if session.get('final_excel_batch_id') != batch_id:
        return jsonify({'success': False, 'error': 'Invalid request'})
    
    data = request.get_json()
    new_name = data.get('new_name', '').strip()
    
    if not new_name:
        return jsonify({'success': False, 'error': 'Invalid filename'})
    
    # Sanitize new filename
    new_name = secure_filename(new_name)
    if not new_name:
        return jsonify({'success': False, 'error': 'Invalid filename'})
    
    processed_files = session.get('final_excel_files', [])
    
    # Find and rename the file
    for f in processed_files:
        if f['unique_filename'] == filename:
            f['filename'] = new_name
            session['final_excel_files'] = processed_files
            session.modified = True
            return jsonify({'success': True, 'new_name': new_name})
    
    return jsonify({'success': False, 'error': 'File not found'})

# ----------- Meeting Summary -----------
# Open to every signed-in account, whatever the role: it only reads headline
# fields out of files the caller uploads and writes nothing back to the system.

SUMMARY_MAX_FILES = 200


def _processed_file_id_for_path(file_path):
    """The processed_files row id for a path we just registered, if present."""
    for record in get_processed_files():
        record = dict(record)
        if record.get('file_path') == file_path:
            return record.get('id')
    return None


@app.route('/summary')
@login_required
def summary_tool():
    """Upload form for the meeting summary report."""
    return render_template(
        'summary.html',
        show_navigation=True,
        app_names=get_app_names(),
        summary_columns=SUMMARY_COLUMNS,
        rows=None,
        download_id=None,
        report_name=None
    )


@app.route('/summary/process', methods=['POST'])
@login_required
def summary_process():
    """Read Subject/Meeting ID/End Time and the participant count from each
    uploaded workbook, then hand back one combined report."""
    files = [f for f in request.files.getlist('summary_files') if f and f.filename]
    if not files:
        flash('Please choose at least one attendance file to summarise.', 'error')
        return redirect(url_for('summary_tool'))

    if len(files) > SUMMARY_MAX_FILES:
        flash(f'Please upload at most {SUMMARY_MAX_FILES} files at a time.', 'error')
        return redirect(url_for('summary_tool'))

    output_format = request.form.get('output_format', 'xlsx')
    if output_format not in ('xlsx', 'csv'):
        output_format = 'xlsx'

    user_email = session.get('user_id')
    started_at = time.time()
    saved = []
    try:
        for uploaded in files:
            token = secrets.token_hex(8)
            stored_path = os.path.join(TEMP_DIR, f'{token}_{secure_filename(uploaded.filename)}')
            uploaded.save(stored_path)
            saved.append((stored_path, uploaded.filename))

        rows = build_summary_rows(saved)

        for stored_path, original_name in saved:
            try:
                log_file_processing(
                    user_email=user_email,
                    file_name=original_name,
                    processing_type='summary',
                    file_size=os.path.getsize(stored_path) if os.path.exists(stored_path) else None,
                    processing_time=None
                )
            except Exception as exc:
                print(f'Summary: could not log processing for {original_name}: {exc}')

        report_name = f'Attendance_Summary_{datetime.now().strftime("%Y%m%d_%H%M%S")}.{output_format}'
        report_path = os.path.join(TEMP_DIR, f'{secrets.token_hex(8)}_{report_name}')
        write_summary_report(rows, report_path, output_format)

        eligible_time = (datetime.now() + timedelta(minutes=120)).strftime('%Y-%m-%d %H:%M:%S')
        register_processed_file(
            file_path=report_path,
            file_name=report_name,
            user_email=user_email,
            processing_type='summary',
            eligible_for_deletion_at=eligible_time
        )
    finally:
        # The uploads are only ever read; nothing links to them afterwards, so
        # they are removed now rather than left for the two-hour sweep.
        for stored_path, _ in saved:
            try:
                if os.path.exists(stored_path):
                    os.remove(stored_path)
            except OSError:
                pass

    ok_count = sum(1 for row in rows if row.get('Status') == 'OK')
    if ok_count == len(rows):
        flash(f'Summarised {len(rows)} file(s) in {round(time.time() - started_at, 1)}s.', 'success')
    else:
        flash(
            f'Summarised {len(rows)} file(s); {len(rows) - ok_count} need attention - '
            'see the Status column.', 'warning'
        )

    return render_template(
        'summary.html',
        show_navigation=True,
        app_names=get_app_names(),
        summary_columns=SUMMARY_COLUMNS,
        rows=rows,
        download_id=_processed_file_id_for_path(report_path),
        report_name=report_name
    )

# ----------- Dashboard Route -----------
@app.route('/dashboard')
@login_required
def dashboard():
    """Dashboard showing usage statistics and analytics"""
    user_email = session.get('user_id')
    user_role = session.get('role')
    
    # Refresh permissions from database (check_session_validity already does this, but double-check here)
    user = get_user(user_email)
    user_dict = None
    if user:
        user_dict = dict(user) if hasattr(user, 'keys') else user
        # Update session with latest permissions
        session['can_view_dashboard'] = bool(user_dict.get('can_view_dashboard', 0))
        user_role = user_dict.get('role', user_role)  # Update role from DB
    
    # Check if user has permission - admin/subsuperadmin/superadmin always have access (they
    # need it to manage users below); regular users need explicit can_view_dashboard permission.
    # Only exception: the main superadmin (superadmin@attendancify.com) always has access
    if user_email != 'superadmin@attendancify.com' and user_role not in ['admin', 'superadmin', 'subsuperadmin']:
        if not user_dict or not user_dict.get('can_view_dashboard'):
            flash('Access denied. You do not have permission to view the dashboard.', 'error')
            return redirect(url_for('index'))

    # Get dashboard stats
    # Superadmin and subsuperadmin see all stats, regular users see only their own
    is_super = user_role in ['superadmin', 'subsuperadmin']
    stats = get_dashboard_stats(user_email if not is_super else None)

    # Get user usage summary (only for superadmin and subsuperadmin)
    user_summary = None
    if is_super:
        user_summary = get_user_usage_summary()

    # Get last login info for current user
    last_login_info = get_last_login(user_email)

    # Get user list and stats for the User Management section (admin/subsuperadmin/superadmin only)
    users = []
    user_stats = {}
    total_files_processed = 0
    if user_role in ['admin', 'superadmin', 'subsuperadmin']:
        db_users = get_all_users()
        for u in db_users:
            if u['email'] != 'superadmin@attendancify.com':
                u_dict = dict(u) if hasattr(u, 'keys') else u
                # make_aware, not fromisoformat: rows written before the app was
                # timezone-aware store a naive timestamp while newer ones store
                # one with a +05:30 offset. The template compares expires_at to
                # `now`, and mixing the two kinds raises TypeError.
                created_at = make_aware(u_dict.get('created_at'))
                expires_at = make_aware(u_dict.get('expires_at'))
                users.append({
                    'email': u_dict.get('email'),
                    'role': u_dict.get('role', 'user'),
                    'created_at': created_at,
                    'expires_at': expires_at,
                    'can_use_batch_matching': u_dict.get('can_use_batch_matching', 0),
                    'created_by': u_dict.get('created_by'),
                })

        if is_superadmin():
            processing_summary = get_processing_summary()
            for summary in processing_summary:
                user_stats[summary['user_email']] = {
                    'total_files_processed': summary['total_files_processed'],
                    'last_processing': summary['last_processing']
                }
                total_files_processed += summary['total_files_processed'] or 0

    return render_template(
        'dashboard.html',
        stats=stats,
        user_summary=user_summary,
        last_login_info=last_login_info,
        is_superadmin=is_super,
        users=users,
        user_stats=user_stats,
        total_files_processed=total_files_processed,
        # Panels of the dashboard shell
        trends=get_dashboard_trends(None if is_super else user_email),
        tool_last_used=_dashboard_tool_last_used(None if is_super else user_email),
        app_names=get_app_names(),
        now=datetime.now(IST),  # aware, to match the expires_at values above
        recent_activity=_dashboard_recent_activity(user_email, is_super),
        recent_files=_dashboard_recent_files(user_email, is_super),
        health=_dashboard_system_health(),
        today_label=datetime.now(IST).strftime('%b %d, %Y'),
        hide_app_chrome=True,
        show_navigation=True
    )


# ----------- Dashboard panel helpers -----------
TOOL_LABELS = {
    'attendance_generation': 'Attendance Generator',
    'attendance_matching': 'Smart Matcher',
    'attendance_matching_batch': 'Smart Matcher',
    'attendance_matching_master': 'Smart Matcher',
    'attendance_matching_raw': 'Smart Matcher',
    'final_excel': 'Final Excel Generator',
    'raw_excel_generation': 'Raw Excel Generator',
}

ACTIVITY_ICONS = {
    'login': ('fa-sign-in-alt', 'blue'),
    'logout': ('fa-sign-out-alt', 'slate'),
    'user_create': ('fa-user-plus', 'purple'),
    'user_update': ('fa-user-edit', 'purple'),
    'user_delete': ('fa-user-minus', 'red'),
    'settings_update': ('fa-cog', 'amber'),
    'processing_logs_clear': ('fa-broom', 'amber'),
    'processing_logs_delete': ('fa-broom', 'amber'),
    'login_history_clear': ('fa-broom', 'amber'),
    'log_deletion': ('fa-broom', 'amber'),
    'file_processing': ('fa-file-import', 'green'),
    'batch_create': ('fa-layer-group', 'purple'),
    'batch_update': ('fa-layer-group', 'purple'),
}


def _humanise_since(value):
    """'Today, 09:14 AM' / 'Yesterday, 04:35 PM' / 'May 21, 2025' / 'Never'."""
    if not value:
        return None
    if isinstance(value, str):
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
            try:
                value = datetime.strptime(value.split('+')[0].strip(), fmt)
                break
            except ValueError:
                continue
    if not isinstance(value, datetime):
        return None

    today = datetime.now().date()
    if value.date() == today:
        return f"Today, {value.strftime('%I:%M %p')}"
    if (today - value.date()).days == 1:
        return f"Yesterday, {value.strftime('%I:%M %p')}"
    return value.strftime('%b %d, %Y')


def _dashboard_tool_last_used(user_email):
    """Raw last-used timestamps plus the label the cards display."""
    raw = get_tool_last_used(user_email)
    return {
        'attendance': raw.get('attendance'),
        'matching': raw.get('matching'),
        'final_excel': raw.get('final_excel'),
        'attendance_label': _humanise_since(raw.get('attendance')),
        'matching_label': _humanise_since(raw.get('matching')),
        'final_excel_label': _humanise_since(raw.get('final_excel')),
    }


def _dashboard_recent_activity(user_email, is_super, limit=15):
    """Latest audit-trail entries for the Today's Activity panel."""
    try:
        rows = get_activity_logs(user_email=None if is_super else user_email, limit=limit)
    except Exception:
        return []

    items = []
    for row in rows:
        row = dict(row)
        icon, tone = ACTIVITY_ICONS.get(row.get('action_type'), ('fa-info-circle', 'slate'))
        items.append({
            'title': (row.get('action_type') or 'activity').replace('_', ' ').title(),
            'detail': row.get('action_description') or row.get('user_email') or '',
            'time': _humanise_since(row.get('timestamp')) or '',
            'icon': icon,
            'tone': tone,
        })
    return items


def _dashboard_recent_files(user_email, is_super, limit=12):
    """Newest generated files, with their size read off disk."""
    try:
        rows = get_processed_files(user_email=None if is_super else user_email)
    except Exception:
        return []

    files = []
    for row in rows[:limit]:
        row = dict(row)
        size_mb = None
        try:
            path = row.get('file_path')
            if path and os.path.exists(path):
                size_mb = round(os.path.getsize(path) / (1024 * 1024), 1)
        except OSError:
            size_mb = None
        files.append({
            'name': row.get('file_name'),
            'tool': TOOL_LABELS.get(row.get('processing_type'), 'Processed file'),
            'size': f"{size_mb} MB" if size_mb is not None else '-',
            'time': _humanise_since(row.get('created_at')) or '',
            'missing': size_mb is None,
        })
    return files


def _dashboard_system_health():
    """Real numbers for the System Health panel."""
    health = []

    # Database file size
    try:
        db_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
        health.append({'label': 'Database', 'detail': f'{db_mb:.1f} MB', 'value': 'Healthy',
                       'pct': None, 'icon': 'fa-database', 'tone': 'green', 'ok': True})
    except OSError:
        health.append({'label': 'Database', 'detail': 'Not reachable', 'value': 'Error',
                       'pct': None, 'icon': 'fa-database', 'tone': 'red', 'ok': False})

    # Disk usage for the volume the app runs on
    try:
        usage = shutil.disk_usage(os.path.dirname(os.path.abspath(__file__)))
        used_pct = int(round(usage.used / usage.total * 100))
        health.append({'label': 'Storage', 'detail': f'{used_pct}% used',
                       'value': f'{used_pct}%', 'pct': used_pct, 'icon': 'fa-hdd',
                       'tone': 'green' if used_pct < 85 else 'red', 'ok': used_pct < 85})
    except Exception:
        pass

    # Files currently held for download
    try:
        stored = len(get_processed_files())
        health.append({'label': 'Processed files', 'detail': f'{stored} stored',
                       'value': 'OK', 'pct': None, 'icon': 'fa-folder-open',
                       'tone': 'blue', 'ok': True})
    except Exception:
        pass

    # Uptime of this worker
    uptime_seconds = int(time.time() - APP_STARTED_AT)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes = remainder // 60
    uptime = f'{hours}h {minutes}m' if hours else f'{minutes}m'
    health.append({'label': 'Server', 'detail': f'Up {uptime}', 'value': 'Operational',
                   'pct': None, 'icon': 'fa-server', 'tone': 'green', 'ok': True})

    return health

@app.route('/api/search')
@login_required
def dashboard_search():
    """Backs the dashboard's search box: users, batches and generated files.
    Each section is only searched if the caller may see it."""
    query = (request.args.get('q') or '').strip()
    if len(query) < 2:
        return jsonify({'results': []})

    user_email = session.get('user_id')
    role = session.get('role', 'user')
    is_admin = role in ('admin', 'superadmin', 'subsuperadmin')
    needle = query.lower()
    results = []

    try:
        if is_admin:
            for user in get_all_users():
                user = dict(user)
                haystack = f"{user.get('email', '')} {user.get('first_name') or ''} {user.get('last_name') or ''}".lower()
                if needle in haystack:
                    results.append({
                        'type': 'User', 'icon': 'fa-user',
                        'title': user.get('email'),
                        'subtitle': (user.get('role') or 'user').title(),
                        'url': url_for('edit_user', user_id=user.get('email')),
                    })
                    if len(results) >= 8:
                        break
    except Exception:
        pass

    try:
        # Same rule as batch_manager_required: being an admin is not enough, or
        # search would hand back the batch names the batch pages just refused.
        if role == 'superadmin' or session.get('can_manage_batches'):
            for batch in get_batches_for_user(user_email, role):
                batch = dict(batch)
                if needle in (batch.get('name') or '').lower():
                    results.append({
                        'type': 'Batch', 'icon': 'fa-layer-group',
                        'title': batch.get('name'),
                        'subtitle': 'Batch',
                        'url': url_for('manage_batches', batch_id=batch.get('id')),
                    })
                    if len(results) >= 16:
                        break
    except Exception:
        pass

    try:
        files = get_processed_files(user_email=None if is_admin else user_email)
        for row in files:
            row = dict(row)
            if needle in (row.get('file_name') or '').lower():
                results.append({
                    'type': 'File', 'icon': 'fa-file-alt',
                    'title': row.get('file_name'),
                    'subtitle': TOOL_LABELS.get(row.get('processing_type'), 'Processed file'),
                    'url': url_for('processed_files_management'),
                })
                if len(results) >= 24:
                    break
    except Exception:
        pass

    return jsonify({'results': results[:24]})


# ----------- Activity Logs Management Routes -----------
@app.route('/admin/activity_logs')
@superadmin_required
def activity_logs():
    """View activity logs - Superadmin only"""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    offset = (page - 1) * per_page

    user_filter = request.args.get('user', '')
    action_filter = request.args.get('action', '')
    ip_filter = request.args.get('ip', '').strip()

    logs = get_activity_logs(
        user_email=user_filter if user_filter else None,
        action_type=action_filter if action_filter else None,
        ip_address=ip_filter if ip_filter else None,
        limit=per_page,
        offset=offset
    )

    total_count = get_activity_log_count(
        user_email=user_filter if user_filter else None,
        action_type=action_filter if action_filter else None,
        ip_address=ip_filter if ip_filter else None
    )

    total_pages = (total_count + per_page - 1) // per_page

    # Addresses seen recently, most-shared first. An address used by several
    # accounts is the one worth looking at.
    ip_summary = get_ip_activity_summary(limit=15, days=30)

    return render_template(
        'activity_logs.html',
        logs=logs,
        page=page,
        total_pages=total_pages,
        user_filter=user_filter,
        action_filter=action_filter,
        ip_filter=ip_filter,
        ip_summary=ip_summary,
        show_navigation=True
    )

@app.route('/admin/delete_logs', methods=['POST'])
@superadmin_required
def delete_logs():
    """Delete activity logs - Superadmin only"""
    try:
        action = request.form.get('action')
        
        if action == 'delete_all':
            deleted = clear_all_activity_logs()
            flash(f'Deleted all {deleted} activity logs.', 'success')
        elif action == 'delete_old':
            days = request.form.get('days', 30, type=int)
            deleted = delete_activity_logs(days_old=days)
            flash(f'Deleted {deleted} activity logs older than {days} days.', 'success')
        else:
            flash('Invalid action specified.', 'error')
        
        # Log this action
        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'log_deletion', f'Deleted activity logs: {action}', ip_address)
        
    except Exception as e:
        flash(f'Error deleting logs: {str(e)}', 'error')
    
    return redirect(url_for('activity_logs'))

@app.route('/admin/delete_login_history', methods=['POST'])
@superadmin_required
def delete_login_history():
    """Delete login history from dashboard - Superadmin only"""
    try:
        action = request.form.get('action')
        
        if action == 'delete_all':
            # Delete all login history
            from database import get_db_connection
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM login_history')
                deleted_count = cursor.rowcount
                cursor.close()
                flash(f'Deleted all {deleted_count} login history records.', 'success')
            finally:
                conn.close()
        elif action == 'delete_old':
            days = request.form.get('days', 30, type=int)
            # Delete old login history
            from database import get_db_connection
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    DELETE FROM login_history 
                    WHERE datetime(login_time) <= datetime('now', '-' || ? || ' days')
                ''', (days,))
                deleted_count = cursor.rowcount
                cursor.close()
                flash(f'Deleted {deleted_count} login records older than {days} days.', 'success')
            finally:
                conn.close()
        else:
            flash('Invalid action specified.', 'error')
        
        # Log this action
        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'login_history_deletion', f'Deleted login history: {action}', ip_address)
        
    except Exception as e:
        flash(f'Error deleting login history: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

# ----------- Settings Management Routes -----------
@app.route('/admin/settings')
@superadmin_required
def settings_management():
    """Settings management - Superadmin only"""
    app_settings = get_all_app_settings()
    output_formats = get_all_output_format_settings()
    app_output_formats = get_all_app_output_formats()
    landing_content = get_landing_content()
    usage_counts = get_usage_record_counts()

    # Get app names for display
    app_names = {
        'app1': get_app_setting('app1_name', 'Zoom Attendance Analyzer'),
        'app2': get_app_setting('app2_name', 'Intelligent Record Matcher'),
        'app3': get_app_setting('app3_name', 'Session Report Generator'),
    }

    # Per-app column rename/hide config, for the Output Formats tab
    output_column_configs = {}
    for app_key, columns in APP_OUTPUT_CONFIGURABLE_COLUMNS.items():
        renames, hidden = get_output_column_config(app_key)
        output_column_configs[app_key] = {
            'columns': columns,
            'renames': renames,
            'hidden': hidden,
        }

    return render_template(
        'settings_management.html',
        app_settings=app_settings,
        output_formats=output_formats,
        app_output_formats=app_output_formats,
        app_names=app_names,
        output_column_configs=output_column_configs,
        landing_sections=LANDING_SECTIONS,
        landing_content=landing_content,
        landing_is_custom=landing_content_is_custom(landing_content),
        usage_counts=usage_counts,
        overlay_tools=PROCESSING_OVERLAY_TOOLS,
        overlay_max_steps=PROCESSING_OVERLAY_MAX_STEPS,
        show_navigation=True
    )

@app.route('/admin/settings/processing-overlay', methods=['POST'])
@superadmin_required
def update_processing_overlay():
    """Save the processing overlay's wording and behaviour - Superadmin only."""
    try:
        config = {
            'enabled': request.form.get('overlay_enabled') == 'on',
            'show_elapsed': request.form.get('overlay_show_elapsed') == 'on',
            'subtitle': (request.form.get('overlay_subtitle') or '').strip()[:300],
            'slow_notice': (request.form.get('overlay_slow_notice') or '').strip()[:300],
            'tools': {},
        }

        skipped = []
        for key, label in PROCESSING_OVERLAY_TOOLS:
            title = (request.form.get(f'title__{key}') or '').strip()[:120]
            raw_steps = request.form.get(f'steps__{key}') or ''
            steps = [line.strip()[:160] for line in raw_steps.splitlines() if line.strip()]

            # An empty list would leave that tool with no stages at all, so the
            # built-in ones are kept instead of saving something unusable.
            if not steps:
                skipped.append(label)
                continue

            config['tools'][key] = {
                'title': title or PROCESSING_OVERLAY_DEFAULTS[key]['title'],
                'steps': steps[:PROCESSING_OVERLAY_MAX_STEPS],
            }

        import json as _json
        set_app_setting(
            PROCESSING_OVERLAY_SETTING_KEY,
            _json.dumps(config),
            updated_by=session.get('user_id')
        )
        log_activity(
            session.get('user_id'), 'settings_update',
            'Updated processing overlay settings'
        )

        if skipped:
            flash(
                'Processing overlay saved. Left on the built-in stages (no text given): '
                + ', '.join(skipped), 'warning'
            )
        else:
            flash('Processing overlay settings saved.', 'success')
    except Exception as exc:
        flash(f'Could not save the processing overlay settings: {exc}', 'error')

    return redirect(url_for('settings_management') + '#processing-overlay')


@app.route('/admin/settings/processing-overlay/reset', methods=['POST'])
@superadmin_required
def reset_processing_overlay():
    """Drop the saved overlay wording and go back to the built-in stages."""
    try:
        set_app_setting(PROCESSING_OVERLAY_SETTING_KEY, '', updated_by=session.get('user_id'))
        log_activity(session.get('user_id'), 'settings_update',
                     'Reset processing overlay settings to defaults')
        flash('Processing overlay reset to the default stages.', 'success')
    except Exception as exc:
        flash(f'Could not reset the processing overlay settings: {exc}', 'error')
    return redirect(url_for('settings_management') + '#processing-overlay')


@app.route('/admin/settings/maintenance', methods=['POST'])
@superadmin_required
def update_maintenance():
    """Turn the maintenance page on/off and set its wording - Superadmin only."""
    try:
        heading = (request.form.get('maintenance_heading') or '').strip()[:200]
        message = (request.form.get('maintenance_message') or '').strip()[:2000]
        config = {
            'enabled': request.form.get('maintenance_enabled') == 'on',
            # Fall back to the defaults so a blank field never leaves the page
            # with an empty heading or body.
            'heading': heading or MAINTENANCE_DEFAULTS['heading'],
            'message': message or MAINTENANCE_DEFAULTS['message'],
        }

        import json as _json
        set_app_setting(
            MAINTENANCE_SETTING_KEY,
            _json.dumps(config),
            updated_by=session.get('user_id')
        )
        log_activity(
            session.get('user_id'), 'settings_update',
            f"Maintenance mode {'enabled' if config['enabled'] else 'disabled'}"
        )

        if config['enabled']:
            flash('Maintenance mode is ON. Everyone except superadmin now sees '
                  'the maintenance page.', 'warning')
        else:
            flash('Maintenance mode is OFF. The site is available to everyone '
                  'again.', 'success')
    except Exception as exc:
        flash(f'Could not save the maintenance settings: {exc}', 'error')

    return redirect(url_for('settings_management') + '#maintenance')


@app.route('/admin/settings/update', methods=['POST'])
@superadmin_required
def update_settings():
    """Update app settings - Superadmin only"""
    try:
        setting_key = request.form.get('setting_key')
        setting_value = request.form.get('setting_value')
        
        if not setting_key:
            flash('Setting key is required.', 'error')
            return redirect(url_for('settings_management'))
        
        set_app_setting(setting_key, setting_value, updated_by=session.get('user_id'))
        
        # Log this action
        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'settings_update', f'Updated setting: {setting_key} = {setting_value}', ip_address)
        
        flash(f'Setting "{setting_key}" updated successfully.', 'success')
        
    except Exception as e:
        flash(f'Error updating setting: {str(e)}', 'error')
    
    return redirect(url_for('settings_management'))

# ----------- Header Brand Logo -----------
BRAND_LOGO_DIR = os.path.join(app.static_folder, 'branding')
BRAND_LOGO_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'}
BRAND_LOGO_MAX_BYTES = 2 * 1024 * 1024  # 2MB


def _delete_stored_brand_logo():
    """Remove the currently stored logo file (best effort)."""
    current = get_app_setting('brand_logo', '')
    if not current:
        return
    old_path = os.path.join(app.static_folder, current.replace('/', os.sep))
    try:
        if os.path.isfile(old_path) and os.path.abspath(old_path).startswith(os.path.abspath(BRAND_LOGO_DIR)):
            os.remove(old_path)
    except OSError:
        pass


@app.route('/admin/settings/brand-logo', methods=['POST'])
@superadmin_required
def upload_brand_logo():
    """Upload the header logo image - Superadmin only"""
    try:
        logo = request.files.get('brand_logo')
        if not logo or not logo.filename:
            flash('Please choose an image file to upload.', 'error')
            return redirect(url_for('settings_management'))

        ext = os.path.splitext(logo.filename)[1].lower().lstrip('.')
        if ext not in BRAND_LOGO_EXTENSIONS:
            flash('Unsupported image type. Use PNG, JPG, GIF, WEBP or SVG.', 'error')
            return redirect(url_for('settings_management'))

        # Size check without trusting the client-reported length
        logo.stream.seek(0, os.SEEK_END)
        size = logo.stream.tell()
        logo.stream.seek(0)
        if size > BRAND_LOGO_MAX_BYTES:
            flash('Logo is too large. Maximum size is 2MB.', 'error')
            return redirect(url_for('settings_management'))

        os.makedirs(BRAND_LOGO_DIR, exist_ok=True)
        _delete_stored_brand_logo()

        # Timestamped name so browsers pick up the new file instead of a cached one
        filename = f"logo_{datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
        logo.save(os.path.join(BRAND_LOGO_DIR, filename))

        set_app_setting('brand_logo', f'branding/{filename}', updated_by=session.get('user_id'))
        set_app_setting('brand_mode', 'image', updated_by=session.get('user_id'))

        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'settings_update', f'Uploaded header logo: {filename}', ip_address)

        flash('Logo uploaded. The header now shows your image.', 'success')

    except Exception as e:
        flash(f'Error uploading logo: {str(e)}', 'error')

    return redirect(url_for('settings_management'))


@app.route('/admin/settings/brand-logo/remove', methods=['POST'])
@superadmin_required
def remove_brand_logo():
    """Delete the header logo image and fall back to text - Superadmin only"""
    try:
        _delete_stored_brand_logo()
        set_app_setting('brand_logo', '', updated_by=session.get('user_id'))
        set_app_setting('brand_mode', 'text', updated_by=session.get('user_id'))

        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'settings_update', 'Removed header logo', ip_address)

        flash('Logo removed. The header shows the site name and tagline again.', 'success')

    except Exception as e:
        flash(f'Error removing logo: {str(e)}', 'error')

    return redirect(url_for('settings_management'))


@app.route('/admin/settings/reset-stats', methods=['POST'])
@superadmin_required
def reset_dashboard_stats():
    """Reset the dashboard counters - Superadmin only.

    processing -> Files Processed, Hours Saved and the Processing Breakdown
    logins     -> Total Logins and the recent-login list
    """
    scope = request.form.get('scope', '')
    user_email = session.get('user_id')
    ip_address = get_client_ip()

    try:
        if scope not in ('processing', 'logins', 'all'):
            flash('Nothing to reset: unknown option.', 'error')
            return redirect(url_for('settings_management'))

        messages = []

        if scope in ('processing', 'all'):
            removed = clear_all_processing_logs()
            messages.append(f'{removed} processing record(s)')
            log_activity(user_email, 'processing_logs_clear',
                         f'Reset dashboard processing stats ({removed} records)', ip_address)

        if scope in ('logins', 'all'):
            removed = clear_login_history()
            messages.append(f'{removed} login record(s)')
            log_activity(user_email, 'login_history_clear',
                         f'Reset dashboard login stats ({removed} records)', ip_address)

        flash('Dashboard statistics reset: ' + ' and '.join(messages) + ' cleared.', 'success')

    except Exception as e:
        flash(f'Error resetting statistics: {str(e)}', 'error')

    return redirect(url_for('settings_management'))


@app.route('/admin/settings/landing-content', methods=['POST'])
@superadmin_required
def update_landing_content():
    """Save the signed-out story page text - Superadmin only.

    Only fields present in the submitted form are touched, and only when the
    value actually changed, so saving one section leaves the rest alone.
    """
    try:
        current = get_landing_content()
        changed = 0
        # An unticked checkbox posts nothing at all, so each form also names the
        # switches it carries - without that, turning one off would look exactly
        # like a form that never had it.
        submitted_toggles = set(request.form.getlist('_toggles'))

        for field in LANDING_FIELDS:
            key = field['key']
            if field['type'] == 'toggle':
                if key not in submitted_toggles:
                    continue
                value = '1' if request.form.get(key) else '0'
            elif key not in request.form:
                continue
            else:
                value = request.form.get(key, '').replace('\r\n', '\n').strip()
            if value != (current.get(key) or ''):
                set_app_setting(key, value, updated_by=session.get('user_id'))
                changed += 1

        if changed:
            ip_address = get_client_ip()
            log_activity(session.get('user_id'), 'settings_update',
                         f'Updated landing page content ({changed} field(s))', ip_address)
            flash(f'Landing page saved ({changed} field(s) updated).', 'success')
        else:
            flash('Nothing to save - the landing page is already up to date.', 'info')

    except Exception as e:
        flash(f'Error saving landing page: {str(e)}', 'error')

    return redirect(url_for('settings_management') + '#landing-page')


@app.route('/admin/settings/landing-content/reset', methods=['POST'])
@superadmin_required
def reset_landing_content():
    """Put every landing-page field back to the built-in text - Superadmin only"""
    try:
        for key, default in LANDING_DEFAULTS.items():
            set_app_setting(key, default, updated_by=session.get('user_id'))

        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'settings_update', 'Restored default landing page text', ip_address)

        flash('Default landing page text restored. Uploaded photos were kept.', 'success')

    except Exception as e:
        flash(f'Error restoring landing page: {str(e)}', 'error')

    return redirect(url_for('settings_management') + '#landing-page')


# ----------- Landing page photos -----------
LANDING_PHOTO_DIR = os.path.join(app.static_folder, 'landing')
LANDING_PHOTO_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
LANDING_PHOTO_MAX_BYTES = 5 * 1024 * 1024  # 5MB


def _delete_stored_landing_photo(setting_key):
    """Remove the file a photo slot currently points at (best effort)."""
    current = get_app_setting(setting_key, '')
    if not current:
        return
    old_path = os.path.join(app.static_folder, current.replace('/', os.sep))
    try:
        if os.path.isfile(old_path) and os.path.abspath(old_path).startswith(os.path.abspath(LANDING_PHOTO_DIR)):
            os.remove(old_path)
    except OSError:
        pass


@app.route('/admin/settings/landing-photo', methods=['POST'])
@superadmin_required
def upload_landing_photo():
    """Upload a person's photo for the landing page - Superadmin only.

    Any size or aspect ratio is fine: the page crops it to a circle with
    object-fit, so nothing has to be resized before uploading.
    """
    try:
        setting_key = LANDING_PHOTO_KEYS.get(request.form.get('slot', ''))
        if not setting_key:
            flash('Unknown photo slot.', 'error')
            return redirect(url_for('settings_management') + '#landing-page')

        photo = request.files.get('photo')
        if not photo or not photo.filename:
            flash('Please choose an image file to upload.', 'error')
            return redirect(url_for('settings_management') + '#landing-page')

        ext = os.path.splitext(photo.filename)[1].lower().lstrip('.')
        if ext not in LANDING_PHOTO_EXTENSIONS:
            flash('Unsupported image type. Use PNG, JPG, GIF or WEBP.', 'error')
            return redirect(url_for('settings_management') + '#landing-page')

        # Size check without trusting the client-reported length
        photo.stream.seek(0, os.SEEK_END)
        size = photo.stream.tell()
        photo.stream.seek(0)
        if size > LANDING_PHOTO_MAX_BYTES:
            flash('Photo is too large. Maximum size is 5MB.', 'error')
            return redirect(url_for('settings_management') + '#landing-page')

        os.makedirs(LANDING_PHOTO_DIR, exist_ok=True)
        _delete_stored_landing_photo(setting_key)

        # Timestamped name so browsers pick up the new file, not a cached one
        filename = f"{setting_key}_{datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
        photo.save(os.path.join(LANDING_PHOTO_DIR, filename))
        set_app_setting(setting_key, f'landing/{filename}', updated_by=session.get('user_id'))

        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'settings_update',
                     f'Uploaded landing page photo: {filename}', ip_address)

        flash('Photo uploaded. It is cropped to a circle automatically.', 'success')

    except Exception as e:
        flash(f'Error uploading photo: {str(e)}', 'error')

    return redirect(url_for('settings_management') + '#landing-page')


@app.route('/admin/settings/landing-photo/visibility', methods=['POST'])
@superadmin_required
def toggle_landing_photo():
    """Show or hide a photo without deleting it - Superadmin only.

    Hidden means the page draws the initials badge instead, everywhere that
    head-shot appears, and the uploaded file stays where it is.
    """
    try:
        slot = request.form.get('slot', '')
        setting_key = LANDING_PHOTO_SHOW_KEYS.get(slot)
        if not setting_key:
            flash('Unknown photo slot.', 'error')
            return redirect(url_for('settings_management') + '#landing-page')

        show = request.form.get('show') == '1'
        set_app_setting(setting_key, '1' if show else '0', updated_by=session.get('user_id'))

        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'settings_update',
                     f"Landing page photo {slot} {'shown' if show else 'hidden'}", ip_address)

        flash('Photo is now shown on the page.' if show
              else 'Photo hidden. The initials avatar is shown instead - the file is kept.', 'success')

    except Exception as e:
        flash(f'Error updating photo visibility: {str(e)}', 'error')

    return redirect(url_for('settings_management') + '#landing-page')


@app.route('/admin/settings/landing-photo/remove', methods=['POST'])
@superadmin_required
def remove_landing_photo():
    """Drop a photo and fall back to the initials avatar - Superadmin only"""
    try:
        setting_key = LANDING_PHOTO_KEYS.get(request.form.get('slot', ''))
        if not setting_key:
            flash('Unknown photo slot.', 'error')
            return redirect(url_for('settings_management') + '#landing-page')

        _delete_stored_landing_photo(setting_key)
        set_app_setting(setting_key, '', updated_by=session.get('user_id'))

        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'settings_update', 'Removed a landing page photo', ip_address)

        flash('Photo removed. The initials avatar is shown again.', 'success')

    except Exception as e:
        flash(f'Error removing photo: {str(e)}', 'error')

    return redirect(url_for('settings_management') + '#landing-page')


@app.route('/admin/settings/landing-preview')
@superadmin_required
def landing_message_preview():
    """See the signed-out page exactly as a visitor would, while still logged in."""
    return render_template(
        'landing.html',
        show_navigation=True,
        landing=get_landing_content(),
        force_public_landing=True,
        is_landing_preview=True
    )


@app.route('/admin/settings/fluid-cursor', methods=['POST'])
@superadmin_required
def update_fluid_cursor():
    """Tune the fluid cursor simulation - Superadmin only"""
    try:
        for key, (low, high, default) in FLUID_RANGES.items():
            set_app_setting(key, _safe_glow_number(request.form.get(key), low, high, default),
                            updated_by=session.get('user_id'))

        quality = _safe_glow_number(request.form.get('fluid_quality'), 512, 1440, 1440)
        if quality not in FLUID_QUALITIES:
            quality = 1440
        set_app_setting('fluid_quality', quality, updated_by=session.get('user_id'))

        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'settings_update', 'Updated fluid cursor settings', ip_address)

        flash('Fluid cursor updated.', 'success')

    except Exception as e:
        flash(f'Error updating fluid cursor: {str(e)}', 'error')

    return redirect(url_for('settings_management'))


@app.route('/admin/settings/cursor-glow', methods=['POST'])
@superadmin_required
def update_cursor_glow():
    """Update the cursor glow colour, shape, size and opacity - Superadmin only"""
    try:
        color = (request.form.get('cursor_glow_color') or '').strip()
        shape = (request.form.get('cursor_glow_shape') or '').strip()

        if not re.fullmatch(r'#[0-9A-Fa-f]{6}', color):
            flash('Pick a colour in #rrggbb form.', 'error')
            return redirect(url_for('settings_management'))

        if shape not in CURSOR_GLOW_SHAPES:
            flash('Choose one of the available glow shapes.', 'error')
            return redirect(url_for('settings_management'))

        effect = _safe_cursor_effect(request.form.get('cursor_effect'))
        size = _safe_glow_number(request.form.get('cursor_glow_size'), *CURSOR_GLOW_SIZE_RANGE, 88)
        opacity = _safe_glow_number(request.form.get('cursor_glow_opacity'), *CURSOR_GLOW_OPACITY_RANGE, 100)

        set_app_setting('cursor_effect', effect, updated_by=session.get('user_id'))
        set_app_setting('cursor_glow_color', color, updated_by=session.get('user_id'))
        set_app_setting('cursor_glow_shape', shape, updated_by=session.get('user_id'))
        set_app_setting('cursor_glow_size', size, updated_by=session.get('user_id'))
        set_app_setting('cursor_glow_opacity', opacity, updated_by=session.get('user_id'))

        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'settings_update',
                     f'Cursor effect {effect}: {shape} {color} {size}px @ {opacity}%', ip_address)

        flash('Cursor effect updated.', 'success')

    except Exception as e:
        flash(f'Error updating cursor glow: {str(e)}', 'error')

    return redirect(url_for('settings_management'))


@app.route('/admin/settings/create', methods=['POST'])
@superadmin_required
def create_setting():
    """Create new app setting - Superadmin only"""
    try:
        setting_key = request.form.get('setting_key')
        setting_value = request.form.get('setting_value')
        setting_type = request.form.get('setting_type', 'string')
        description = request.form.get('description', '')
        
        if not setting_key:
            flash('Setting key is required.', 'error')
            return redirect(url_for('settings_management'))
        
        success = create_app_setting(setting_key, setting_value, setting_type, description, updated_by=session.get('user_id'))
        
        if success:
            # Log this action
            ip_address = get_client_ip()
            log_activity(session.get('user_id'), 'settings_create', f'Created setting: {setting_key}', ip_address)
            
            flash(f'Setting "{setting_key}" created successfully.', 'success')
        else:
            flash(f'Setting "{setting_key}" already exists.', 'error')
        
    except Exception as e:
        flash(f'Error creating setting: {str(e)}', 'error')
    
    return redirect(url_for('settings_management'))

@app.route('/admin/settings/delete/<setting_key>', methods=['POST'])
@superadmin_required
def delete_setting(setting_key):
    """Delete app setting - Superadmin only"""
    try:
        delete_app_setting(setting_key)
        
        # Log this action
        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'settings_delete', f'Deleted setting: {setting_key}', ip_address)
        
        flash(f'Setting "{setting_key}" deleted successfully.', 'success')
        
    except Exception as e:
        flash(f'Error deleting setting: {str(e)}', 'error')
    
    return redirect(url_for('settings_management'))

@app.route('/admin/output_formats/update', methods=['POST'])
@superadmin_required
def update_output_format():
    """Update output format settings - Superadmin only"""
    try:
        user_role = request.form.get('user_role')
        file_format = request.form.get('file_format')
        date_format = request.form.get('date_format')
        time_format = request.form.get('time_format')
        include_header = request.form.get('include_header') == '1'
        custom_header_json = request.form.get('custom_header_json')
        column_order_json = request.form.get('column_order_json')
        
        if not user_role:
            flash('User role is required.', 'error')
            return redirect(url_for('settings_management'))
        
        set_output_format_settings(
            user_role=user_role,
            file_format=file_format,
            date_format=date_format,
            time_format=time_format,
            include_header=include_header,
            custom_header_json=custom_header_json,
            column_order_json=column_order_json,
            updated_by=session.get('user_id')
        )
        
        # Log this action
        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'format_update', f'Updated output format for role: {user_role}', ip_address)
        
        flash(f'Output format for "{user_role}" updated successfully.', 'success')
        
    except Exception as e:
        flash(f'Error updating output format: {str(e)}', 'error')
    
    return redirect(url_for('settings_management'))

@app.route('/admin/app_formats/update', methods=['POST'])
@superadmin_required
def update_app_format():
    """Update an app's output column rename/hide settings - Superadmin only.

    One rename__<column> text field and one hidden__<column> checkbox per column in
    APP_OUTPUT_CONFIGURABLE_COLUMNS[app_name] -- built entirely server-side from the
    known column list, so there's no way to submit malformed JSON from this form.
    """
    try:
        import json
        app_name = request.form.get('app_name')
        if not app_name or app_name not in APP_OUTPUT_CONFIGURABLE_COLUMNS:
            flash('Unknown app name.', 'error')
            return redirect(url_for('settings_management'))

        renames = {}
        hidden = []
        for col in APP_OUTPUT_CONFIGURABLE_COLUMNS[app_name]:
            new_name = request.form.get(f'rename__{col}', '').strip()
            if new_name and new_name != col:
                renames[col] = new_name
            if request.form.get(f'hidden__{col}') == '1':
                hidden.append(col)

        set_app_output_format(
            app_name=app_name,
            custom_columns_json=json.dumps({'renames': renames, 'hidden': hidden})
        )

        # Log this action
        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'app_format_update', f'Updated output format for app: {app_name}', ip_address)

        # Get app display name
        app_display = get_app_setting(f'{app_name}_name', app_name)
        flash(f'Output format for "{app_display}" updated successfully.', 'success')

    except Exception as e:
        flash(f'Error updating app format: {str(e)}', 'error')

    return redirect(url_for('settings_management'))

# ----------- File Management Routes -----------
@app.route('/admin/files')
@superadmin_required
def file_management():
    """File management - Superadmin only"""
    all_files = get_processed_files()
    
    # Get file system stats
    import os
    total_size = 0
    for file in all_files:
        file_path = file['file_path']
        if os.path.exists(file_path):
            total_size += os.path.getsize(file_path)
    
    total_size_mb = total_size / (1024 * 1024)
    
    return render_template(
        'file_management.html',
        files=all_files,
        total_size_mb=total_size_mb,
        show_navigation=True
    )

@app.route('/admin/files/delete/<int:file_id>', methods=['POST'])
@superadmin_required
def delete_file_by_id(file_id):
    """Delete a file - Superadmin only"""
    try:
        # Get file info
        files = get_processed_files()
        file_to_delete = None
        for f in files:
            if f['id'] == file_id:
                file_to_delete = f
                break
        
        if not file_to_delete:
            flash('File not found in database.', 'error')
            return redirect(url_for('file_management'))
        
        # Delete physical file
        import os
        file_path = file_to_delete['file_path']
        if os.path.exists(file_path):
            os.remove(file_path)
        
        # Mark as manually deleted in database
        mark_file_manually_deleted(file_path)
        
        # Log this action
        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'file_deletion', f'Deleted file: {file_to_delete["file_name"]}', ip_address)
        
        flash(f'File "{file_to_delete["file_name"]}" deleted successfully.', 'success')
        
    except Exception as e:
        flash(f'Error deleting file: {str(e)}', 'error')
    
    return redirect(url_for('file_management'))

@app.route('/admin/files/delete_old', methods=['POST'])
@superadmin_required
def delete_old_files():
    """Delete old files - Superadmin only"""
    try:
        days_old = request.form.get('days_old', 30, type=int)
        deleted_count = 0
        
        # Get all files
        all_files = get_processed_files(include_deleted=False)
        
        from datetime import datetime, timedelta
        cutoff_date = datetime.now() - timedelta(days=days_old)
        
        import os
        for file in all_files:
            created_at = datetime.strptime(file['created_at'], '%Y-%m-%d %H:%M:%S')
            if created_at < cutoff_date:
                # Delete physical file
                file_path = file['file_path']
                if os.path.exists(file_path):
                    os.remove(file_path)
                # Mark as deleted
                mark_file_manually_deleted(file_path)
                deleted_count += 1
        
        # Log this action
        ip_address = get_client_ip()
        log_activity(session.get('user_id'), 'bulk_file_deletion', f'Deleted {deleted_count} files older than {days_old} days', ip_address)
        
        flash(f'Deleted {deleted_count} files older than {days_old} days.', 'success')
        
    except Exception as e:
        flash(f'Error deleting old files: {str(e)}', 'error')
    
    return redirect(url_for('file_management'))

# ----------- Authentication Routes -----------
@app.route('/logout')
def logout():
    """Logout route to clear session"""
    session.clear()
    flash('You have been logged out successfully.')
    return redirect(url_for('index'))


@app.route('/maintenance')
def maintenance():
    """The maintenance / "site is down" page.

    Public on purpose. When maintenance mode is off it just shows the same page
    with a note that the site is available, so the superadmin can preview the
    exact wording before switching it on. When it is on, the before_request gate
    also renders this same template in place of every other page.
    """
    return render_template('maintenance.html',
                           maintenance=get_maintenance_config(),
                           show_navigation=False)

# ============ Global Error Handlers ============
# These send the visitor home with a flash message. They must return the
# redirect's own 302 status: a Location header on a 4xx/5xx response is not
# followed by browsers, so the visitor is left staring at the bare
# "Redirecting... you should be redirected to /" body instead of the homepage.
@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors"""
    flash('Page not found.', 'error')
    return redirect(url_for('index'))

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    flash('An internal error occurred. Please try again or contact support.', 'error')
    return redirect(url_for('index'))

@app.errorhandler(403)
def forbidden_error(error):
    """Handle 403 errors"""
    flash('Access denied. You do not have permission to access this resource.', 'error')
    return redirect(url_for('index'))

@app.errorhandler(Exception)
def handle_exception(e):
    """Global exception handler for unhandled errors"""
    # A deliberate abort() - a rejected CSRF token, a 405, a 401 - is an answer,
    # not a crash. Flask routes HTTPExceptions through this handler too, and
    # turning them into a friendly redirect told the caller the request had
    # succeeded and hid the refusal. Let them answer for themselves; the 404,
    # 403 and 500 handlers above still apply to their own codes.
    if isinstance(e, HTTPException):
        return e
    import traceback
    error_trace = traceback.format_exc()
    print(f"Unhandled exception: {error_trace}")
    flash('An unexpected error occurred. Please try again.', 'error')
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Initialize the database
    init_db()
    # ----------------------------------------------------------------------
    # Application entry point – production configuration
    # ----------------------------------------------------------------------
    # Initialise the database (if needed)
    init_db()

    # Production settings – debug disabled by default
    debug_mode = False

    # Optional: enable debug via FLASK_DEBUG env var for local testing
    if os.environ.get('FLASK_DEBUG', 'false').lower() == 'true':
        debug_mode = True

    # Suppress Werkzeug and Flask CLI banners in production
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    warnings.filterwarnings("ignore", message=".*development server.*")
    warnings.filterwarnings("ignore", category=UserWarning, module="werkzeug")
    try:
        import flask.cli
        flask.cli.show_server_banner = lambda *args, **kwargs: None
    except Exception:
        pass

    # Run the application – this will start Flask's built‑in server only when you
    # explicitly invoke this script (e.g., for local testing). In a real production
    # deployment you should use a WSGI server such as gunicorn or waitress.
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=debug_mode)
    print("Open your web browser and go to: http://localhost:5000")
    print("\n⚠️  NOTE: This is the development server. For production, use a WSGI server.")
    print("   For PythonAnywhere, use the wsgi.py file instead.\n")
    
    # IMPORTANT: debug=False for production security
    # Debug mode should ONLY be enabled for local development
    # Enabling debug in production exposes security vulnerabilities:
    # - Interactive debugger allows code execution
    # - Detailed error pages reveal sensitive information
    # - Auto-reload impacts performance
    import os
    import warnings
    import logging
    import sys
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    # Suppress the Werkzeug development server warning for local testing
    # This warning is correct - don't use app.run() in production!
    # For production deployment, use: gunicorn, waitress, or wsgi.py (PythonAnywhere)
    
    # Method 1: Suppress Werkzeug logger warnings
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    
    # Method 2: Suppress Python warnings
    warnings.filterwarnings("ignore", message=".*development server.*")
    warnings.filterwarnings("ignore", category=UserWarning, module="werkzeug")
    
    # Method 3: Suppress Flask CLI banner (if available)
    try:
        import flask.cli
        flask.cli.show_server_banner = lambda *args: None
    except (AttributeError, ImportError):
        pass
    
    app.run(debug=debug_mode, host='0.0.0.0')
