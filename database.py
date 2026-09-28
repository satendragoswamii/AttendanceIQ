import sqlite3
import os
from datetime import datetime, timedelta

# ============================================================================
# CRITICAL FIX: Use absolute path to prevent "unable to open database file" errors
# This ensures the database is found regardless of current working directory
# ============================================================================
DATABASE = os.path.abspath(os.path.join(os.path.dirname(__file__), 'attendancify.db'))

def get_db_connection():
    """
    CRITICAL: Centralized database connection helper to prevent connection leaks.
    
    This function is the ONLY way to get a database connection throughout the app.
    
    Key fixes:
    1. Uses absolute path to prevent path resolution errors
    2. timeout=30.0 prevents hanging connections
    3. check_same_thread=False allows Flask's multi-threaded environment
    4. isolation_level=None enables autocommit mode (safer for concurrent access)
    5. Returns a connection that MUST be closed by caller using try/finally
    
    IMPORTANT: Every caller MUST close the connection in a finally block:
        conn = get_db_connection()
        try:
            # Use connection
        finally:
            conn.close()
    """
    try:
        conn = sqlite3.connect(
            DATABASE,
            timeout=30.0,  # Wait up to 30 seconds if database is locked
            check_same_thread=False,  # Required for Flask multi-threading
            isolation_level=None  # Autocommit mode for safer concurrent access
        )
        # Row factory allows dict-like access to query results
        conn.row_factory = sqlite3.Row
        # Disable foreign keys to prevent cascade issues (as per original design)
        conn.execute('PRAGMA foreign_keys=OFF')
        # Enable WAL mode for better concurrency (safe for production)
        conn.execute('PRAGMA journal_mode=WAL')
        return conn
    except sqlite3.Error as e:
        print(f"CRITICAL DATABASE ERROR: Failed to connect to {DATABASE}: {e}")
        raise

def get_db():
    """
    DEPRECATED: Use get_db_connection() instead.
    Kept for backward compatibility only.
    """
    return get_db_connection()

def init_db():
    """Initialize the database with required tables"""
    # CRITICAL FIX: Use centralized connection with proper cleanup
    conn = get_db_connection()
    try:
        # Create file processing logs table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS file_processing_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                file_name TEXT NOT NULL,
                processing_type TEXT NOT NULL,
                file_size INTEGER,
                processing_time REAL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE
            )
        ''')
        
        # Create users table for persistent storage
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                expires_at DATETIME,
                must_change_password BOOLEAN DEFAULT 1,
                password_plain TEXT,
                first_name TEXT,
                last_name TEXT,
                can_use_batch_matching BOOLEAN DEFAULT 0,
                created_by TEXT,
                can_manage_roles BOOLEAN DEFAULT 0,
                can_manage_batch_matching BOOLEAN DEFAULT 0
            )
        ''')

        # Ensure new columns exist for older databases
        try:
            conn.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN last_name TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN can_use_batch_matching BOOLEAN DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN created_by TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN can_manage_roles BOOLEAN DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN can_manage_batch_matching BOOLEAN DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN can_manage_batches BOOLEAN DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN can_access_processed_files BOOLEAN DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN can_view_dashboard BOOLEAN DEFAULT 0")
        except Exception:
            pass
        try:
            # NEW: Add hidden column for hide user feature
            conn.execute("ALTER TABLE users ADD COLUMN hidden BOOLEAN DEFAULT 0")
        except Exception:
            pass
        try:
            # Tango access, so it can be granted to any user rather than being
            # decided by role alone.
            conn.execute("ALTER TABLE users ADD COLUMN can_access_tango BOOLEAN DEFAULT 0")
        except Exception:
            pass

        # Create login_history table for tracking user logins and IP addresses
        conn.execute('''
            CREATE TABLE IF NOT EXISTS login_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                ip_address TEXT,
                login_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                logout_time DATETIME,
                session_duration INTEGER,
                FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE
            )
        ''')
        
        # Create index for faster queries
        try:
            conn.execute('CREATE INDEX IF NOT EXISTS idx_login_history_user_email ON login_history(user_email)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_login_history_login_time ON login_history(login_time)')
        except Exception:
            pass

        # Batch Groups for organizing batches
        conn.execute('''
            CREATE TABLE IF NOT EXISTS batch_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                created_by TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (created_by) REFERENCES users(email) ON DELETE SET NULL
            )
        ''')
        
        # User access to batch groups
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_group_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                granted_by TEXT NOT NULL,
                granted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE,
                FOREIGN KEY (group_id) REFERENCES batch_groups(id) ON DELETE CASCADE,
                FOREIGN KEY (granted_by) REFERENCES users(email) ON DELETE SET NULL,
                UNIQUE(user_email, group_id)
            )
        ''')

        # Batch Sub-Groups for organizing batches under groups
        conn.execute('''
            CREATE TABLE IF NOT EXISTS batch_subgroups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                group_id INTEGER NOT NULL,
                created_by TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_completed INTEGER DEFAULT 0,
                FOREIGN KEY (group_id) REFERENCES batch_groups(id) ON DELETE CASCADE,
                FOREIGN KEY (created_by) REFERENCES users(email) ON DELETE SET NULL,
                UNIQUE(group_id, name)
            )
        ''')

        # Add is_completed to batch_subgroups if it doesn't exist (migration)
        try:
            conn.execute('ALTER TABLE batch_subgroups ADD COLUMN is_completed INTEGER DEFAULT 0')
        except Exception:
            pass

        # Batches for Smart Matcher master data
        conn.execute('''
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                created_by TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                group_id INTEGER,
                subgroup_id INTEGER,
                is_completed INTEGER DEFAULT 0,
                required_duration_minutes REAL,
                FOREIGN KEY (created_by) REFERENCES users(email) ON DELETE SET NULL,
                FOREIGN KEY (group_id) REFERENCES batch_groups(id) ON DELETE SET NULL,
                FOREIGN KEY (subgroup_id) REFERENCES batch_subgroups(id) ON DELETE SET NULL
            )
        ''')
        
        # Add group_id to batches if it doesn't exist (migration)
        try:
            conn.execute('ALTER TABLE batches ADD COLUMN group_id INTEGER')
        except Exception:
            pass

        # Add subgroup_id to batches if it doesn't exist (migration)
        try:
            conn.execute('ALTER TABLE batches ADD COLUMN subgroup_id INTEGER')
        except Exception:
            pass

        # Add is_completed to batches if it doesn't exist (migration)
        try:
            conn.execute('ALTER TABLE batches ADD COLUMN is_completed INTEGER DEFAULT 0')
        except Exception:
            pass

        try:
            conn.execute('ALTER TABLE batches ADD COLUMN required_duration_minutes REAL')
        except Exception:
            pass

        # Create indexes for faster queries
        try:
            conn.execute('CREATE INDEX IF NOT EXISTS idx_batches_group_id ON batches(group_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_batches_subgroup_id ON batches(subgroup_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_batches_is_completed ON batches(is_completed)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_batch_subgroups_group_id ON batch_subgroups(group_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_batch_subgroups_is_completed ON batch_subgroups(is_completed)')
        except Exception:
            pass

        conn.execute('''
            CREATE TABLE IF NOT EXISTS batch_students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                institute_email TEXT,
                contact_number TEXT,
                FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
            )
        ''')

        # Add institute_email/contact_number to batch_students if they don't exist (migration).
        # Both are optional, purely informational fields -- never required for matching.
        try:
            conn.execute('ALTER TABLE batch_students ADD COLUMN institute_email TEXT')
        except Exception:
            pass
        try:
            conn.execute('ALTER TABLE batch_students ADD COLUMN contact_number TEXT')
        except Exception:
            pass

        # Batch-scoped Zoom-name -> roster-name learning for Smart Matcher / Final Excel Generator.
        # Learned pairs are strictly scoped by batch_id so the same Zoom display name can resolve
        # to different roster names in different batches without ever leaking across them.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS batch_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                zoom_display_name TEXT NOT NULL,
                roster_name_matched TEXT NOT NULL,
                confidence REAL,
                source TEXT NOT NULL CHECK(source IN ('single_csv_upload', 'manual_review')),
                seen_count INTEGER NOT NULL DEFAULT 1,
                last_seen_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
                UNIQUE(batch_id, zoom_display_name)
            )
        ''')
        try:
            conn.execute('CREATE INDEX IF NOT EXISTS idx_batch_records_batch_id ON batch_records(batch_id)')
        except Exception:
            pass

        # Create processed_files table for tracking processed files
        conn.execute('''
            CREATE TABLE IF NOT EXISTS processed_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                user_email TEXT NOT NULL,
                processing_type TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                eligible_for_deletion_at DATETIME,
                manually_deleted BOOLEAN DEFAULT 0,
                FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE
            )
        ''')

        # Create processed_files_permissions table for access control
        conn.execute('''
            CREATE TABLE IF NOT EXISTS processed_files_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                user_email TEXT NOT NULL,
                granted_by TEXT NOT NULL,
                granted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (file_id) REFERENCES processed_files(id) ON DELETE CASCADE,
                FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE,
                FOREIGN KEY (granted_by) REFERENCES users(email) ON DELETE CASCADE,
                UNIQUE(file_id, user_email)
            )
        ''')
        
        # Create excluded_participants table for filtering attendance
        conn.execute('''
            CREATE TABLE IF NOT EXISTS excluded_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT NOT NULL,
                identifier_type TEXT NOT NULL DEFAULT 'name',
                added_by TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_permanently_blocked INTEGER DEFAULT 0,
                UNIQUE(identifier, identifier_type)
            )
        ''')
        
        # Add is_permanently_blocked column if it doesn't exist (migration for existing DBs)
        try:
            conn.execute('ALTER TABLE excluded_participants ADD COLUMN is_permanently_blocked INTEGER DEFAULT 0')
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Create app_settings table for superadmin configuration
        conn.execute('''
            CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setting_key TEXT NOT NULL UNIQUE,
                setting_value TEXT,
                setting_type TEXT DEFAULT 'string',
                description TEXT,
                updated_by TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create output_format_settings table for user-specific file format configurations
        conn.execute('''
            CREATE TABLE IF NOT EXISTS output_format_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_role TEXT NOT NULL UNIQUE,
                file_format TEXT DEFAULT 'xlsx',
                date_format TEXT DEFAULT '%Y-%m-%d',
                time_format TEXT DEFAULT '%H:%M:%S',
                include_header BOOLEAN DEFAULT 1,
                custom_header_json TEXT,
                column_order_json TEXT,
                created_by TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create activity_logs table for comprehensive logging
        conn.execute('''
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                action_type TEXT NOT NULL,
                action_description TEXT,
                ip_address TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                metadata_json TEXT,
                FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE
            )
        ''')

        # The browser/client string is kept alongside the IP: an address on its
        # own rarely identifies anything on a shared or mobile network.
        try:
            conn.execute("ALTER TABLE activity_logs ADD COLUMN user_agent TEXT")
        except Exception:
            pass

        # Create index for faster log queries
        try:
            conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_logs_user_email ON activity_logs(user_email)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_logs_timestamp ON activity_logs(timestamp)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_logs_action_type ON activity_logs(action_type)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_logs_ip_address ON activity_logs(ip_address)')
        except Exception:
            pass

        # Insert default app settings if not exists
        default_settings = [
            ('max_upload_size_mb', '50', 'integer', 'Maximum file upload size in MB'),
            ('session_timeout_minutes', '60', 'integer', 'Session timeout in minutes'),
            ('auto_delete_files_after_hours', '24', 'integer', 'Auto delete processed files after hours'),
            ('enable_file_auto_deletion', '1', 'boolean', 'Enable automatic file deletion'),
            ('max_concurrent_uploads', '5', 'integer', 'Maximum concurrent file uploads'),
            ('enable_user_registration', '0', 'boolean', 'Allow user self-registration'),
            ('app_tagline', 'Intelligent Attendance Automation', 'string', 'Tagline shown under the site name in the header'),
            ('brand_mode', 'text', 'string', 'Header brand style: "text" (name + tagline) or "image" (uploaded logo)'),
            ('brand_logo', '', 'string', 'Static path of the uploaded header logo image'),
            ('glass_opacity', '82', 'integer', 'Opacity of the frosted glass panels, in percent (20-100)'),
            ('fluid_size', '20', 'integer', 'Fluid cursor: trail size (10-60, used as splat radius / 100)'),
            ('fluid_force', '6000', 'integer', 'Fluid cursor: trail strength / splat force (1000-12000)'),
            ('fluid_fade', '35', 'integer', 'Fluid cursor: how fast the trail fades (5-80, used /10)'),
            ('fluid_swirl', '3', 'integer', 'Fluid cursor: swirl / curl amount (0-20)'),
            ('fluid_quality', '1440', 'integer', 'Fluid cursor: dye resolution (512, 1024 or 1440)'),
            ('cursor_effect', 'both', 'string', 'Cursor effect: glow, fluid, both or none'),
            ('cursor_glow_color', '#22D3EE', 'string', 'Colour of the glow that follows the cursor'),
            ('cursor_glow_shape', 'circle', 'string', 'Shape of the cursor glow: circle, square, diamond or star'),
            ('cursor_glow_size', '88', 'integer', 'Diameter of the cursor glow in pixels (40-200)'),
            ('cursor_glow_opacity', '100', 'integer', 'Strength of the cursor glow in percent (20-100)'),
            ('app1_name', 'Zoom Attendance Analyzer', 'string', 'Custom name for Attendance Generator app'),
            ('app2_name', 'Intelligent Record Matcher', 'string', 'Custom name for Smart Matcher app'),
            ('app3_name', 'Session Report Generator', 'string', 'Custom name for Final Excel Generator app'),
        ]
        
        for key, value, stype, desc in default_settings:
            try:
                conn.execute('''
                    INSERT OR IGNORE INTO app_settings (setting_key, setting_value, setting_type, description)
                    VALUES (?, ?, ?, ?)
                ''', (key, value, stype, desc))
            except Exception:
                pass

        # Insert default output format settings for each role
        default_formats = [
            ('user', 'xlsx', '%Y-%m-%d', '%H:%M:%S', 1),
            ('admin', 'xlsx', '%Y-%m-%d', '%H:%M:%S', 1),
            ('superadmin', 'xlsx', '%Y-%m-%d', '%H:%M:%S', 1),
            ('subsuperadmin', 'xlsx', '%Y-%m-%d', '%H:%M:%S', 1),
        ]
        
        for role, fmt, date_fmt, time_fmt, header in default_formats:
            try:
                conn.execute('''
                    INSERT OR IGNORE INTO output_format_settings 
                    (user_role, file_format, date_format, time_format, include_header)
                    VALUES (?, ?, ?, ?, ?)
                ''', (role, fmt, date_fmt, time_fmt, header))
            except Exception:
                pass

        # Create app-specific output format settings table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS app_output_formats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_name TEXT NOT NULL UNIQUE,
                file_format TEXT DEFAULT 'xlsx',
                date_format TEXT DEFAULT '%Y-%m-%d',
                time_format TEXT DEFAULT '%H:%M:%S',
                include_header BOOLEAN DEFAULT 1,
                header_title TEXT,
                header_subtitle TEXT,
                include_timestamp BOOLEAN DEFAULT 1,
                include_user_info BOOLEAN DEFAULT 1,
                custom_columns_json TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Insert default app-specific formats
        default_app_formats = [
            ('app1', 'xlsx', '%Y-%m-%d', '%H:%M:%S', 1, 'Zoom Attendance Report', 'Generated from Zoom Meeting Data', 1, 1),
            ('app2', 'xlsx', '%Y-%m-%d', '%H:%M:%S', 1, 'Matched Records Report', 'Intelligent Record Matching Results', 1, 1),
            ('app3', 'xlsx', '%Y-%m-%d', '%H:%M:%S', 1, 'Session Report', 'Individual Session Analysis', 1, 1),
        ]
        
        for app, fmt, date_fmt, time_fmt, header, title, subtitle, timestamp, user_info in default_app_formats:
            try:
                conn.execute('''
                    INSERT OR IGNORE INTO app_output_formats 
                    (app_name, file_format, date_format, time_format, include_header, 
                     header_title, header_subtitle, include_timestamp, include_user_info)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (app, fmt, date_fmt, time_fmt, header, title, subtitle, timestamp, user_info))
            except Exception:
                pass

    finally:
        # CRITICAL: Always close connection to prevent file descriptor leaks
        conn.close()

def set_app_setting(key, value, updated_by=None):
    """Upsert an application setting"""
    conn = get_db_connection()
    try:
        conn.execute('''
            INSERT INTO app_settings (setting_key, setting_value, updated_by, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                updated_by = excluded.updated_by,
                updated_at = CURRENT_TIMESTAMP
        ''', (key, value, updated_by))
        conn.commit()
    finally:
        conn.close()

def log_file_processing(user_email, file_name, processing_type, file_size=None, processing_time=None):
    """Log a file processing event"""
    conn = get_db_connection()
    try:
        conn.execute('''
            INSERT INTO file_processing_logs 
            (user_email, file_name, processing_type, file_size, processing_time)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_email, file_name, processing_type, file_size, processing_time))
    finally:
        conn.close()

def get_user_processing_stats(user_email=None):
    """Get file processing statistics for a user or all users - Optimized"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Create index if it doesn't exist
        try:
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_processing_logs_user_email ON file_processing_logs(user_email)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_processing_logs_timestamp ON file_processing_logs(timestamp)')
        except Exception:
            pass
        
        if user_email:
            cursor.execute('''
                SELECT * FROM file_processing_logs 
                WHERE user_email = ?
                ORDER BY timestamp DESC
            ''', (user_email,))
        else:
            cursor.execute('''
                SELECT * FROM file_processing_logs 
                ORDER BY timestamp DESC
            ''')
        
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def get_processing_summary():
    """Get a summary of processing statistics by user"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 
                user_email,
                COUNT(*) as total_files_processed,
                SUM(file_size) as total_file_size,
                AVG(processing_time) as avg_processing_time,
                MIN(timestamp) as first_processing,
                MAX(timestamp) as last_processing
            FROM file_processing_logs
            GROUP BY user_email
            ORDER BY total_files_processed DESC
        ''')
        
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def delete_processing_logs(days_old=None, log_ids=None):
    """Delete processing logs - either by age or by specific IDs"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        if log_ids:
            # Delete specific logs by ID
            placeholders = ','.join(['?' for _ in log_ids])
            cursor.execute(f'DELETE FROM file_processing_logs WHERE id IN ({placeholders})', log_ids)
        elif days_old:
            # Delete logs older than X days
            cursor.execute('''
                DELETE FROM file_processing_logs 
                WHERE datetime(timestamp) < datetime('now', '-' || ? || ' days')
            ''', (days_old,))
        
        deleted = cursor.rowcount
        conn.commit()
        cursor.close()
        return deleted
    finally:
        conn.close()

def clear_all_processing_logs():
    """Clear all processing logs"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM file_processing_logs')
        deleted = cursor.rowcount
        conn.commit()
        cursor.close()
        return deleted
    finally:
        conn.close()

def get_processing_logs_with_ids(limit=500):
    """Get processing logs with IDs for deletion UI"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, user_email, file_name, processing_type, file_size, processing_time, timestamp
            FROM file_processing_logs 
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (limit,))
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()

def create_user(email, password_hash, role, expires_at=None, must_change_password=True, password_plain=None, first_name=None, last_name=None, can_use_batch_matching=False, created_by=None, can_manage_roles=False, can_manage_batch_matching=False, can_manage_batches=False, can_access_processed_files=False, can_view_dashboard=False, can_access_tango=False):
    """Create a new user in the database"""
    conn = get_db_connection()
    try:
        conn.execute('''
            INSERT INTO users (email, password_hash, role, expires_at, must_change_password, password_plain, first_name, last_name, can_use_batch_matching, created_by, can_manage_roles, can_manage_batch_matching, can_manage_batches, can_access_processed_files, can_view_dashboard, can_access_tango)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (email, password_hash, role, expires_at, must_change_password, password_plain, first_name, last_name, int(bool(can_use_batch_matching)), created_by, int(bool(can_manage_roles)), int(bool(can_manage_batch_matching)), int(bool(can_manage_batches)), int(bool(can_access_processed_files)), int(bool(can_view_dashboard)), int(bool(can_access_tango))))
    finally:
        conn.close()

def get_user(email):
    """Get user data from database"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE email = ?', (email,))
        result = cursor.fetchone()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def update_user(email, **kwargs):
    """Update user data in database"""
    if not kwargs:
        return
    
    conn = get_db_connection()
    try:
        set_clause = ', '.join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [email]
        
        conn.execute(f'UPDATE users SET {set_clause} WHERE email = ?', values)
    finally:
        conn.close()

def delete_user(email):
    """Delete user from database"""
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM users WHERE email = ?', (email,))
    finally:
        conn.close()

def get_all_users(include_hidden=False):
    """Get all users from database, optionally excluding hidden users"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if include_hidden:
            cursor.execute('SELECT * FROM users ORDER BY created_at DESC')
        else:
            # Exclude hidden users from regular views
            cursor.execute('SELECT * FROM users WHERE hidden = 0 OR hidden IS NULL ORDER BY created_at DESC')
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def search_users(query, limit=10):
    """Search users by partial email, first name, or last name (case-insensitive)."""
    pattern = f"%{query.strip()}%"
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT email, first_name, last_name, role
            FROM users
            WHERE LOWER(email) LIKE LOWER(?)
               OR LOWER(COALESCE(first_name, '')) LIKE LOWER(?)
               OR LOWER(COALESCE(last_name, '')) LIKE LOWER(?)
            ORDER BY created_at DESC
            LIMIT ?
        ''', (pattern, pattern, pattern, limit))
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()


# -------- Batch Groups Management --------

def create_batch_group(name, description, created_by):
    """Create a new batch group"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO batch_groups (name, description, created_by) VALUES (?, ?, ?)', 
                      (name, description, created_by))
        last_id = cursor.lastrowid
        cursor.close()
        return last_id
    finally:
        conn.close()

def update_batch_group(group_id, name=None, description=None):
    """Update a batch group"""
    updates = {}
    if name is not None:
        updates['name'] = name
    if description is not None:
        updates['description'] = description
    if not updates:
        return
    conn = get_db_connection()
    try:
        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [group_id]
        conn.execute(f'UPDATE batch_groups SET {set_clause} WHERE id = ?', values)
    finally:
        conn.close()

def delete_batch_group(group_id):
    """Delete a batch group (batches in it become ungrouped)"""
    conn = get_db_connection()
    try:
        # First, set subgroup_id to NULL on batches whose subgroup belongs to this group
        conn.execute('''
            UPDATE batches 
            SET subgroup_id = NULL 
            WHERE subgroup_id IN (SELECT id FROM batch_subgroups WHERE group_id = ?)
        ''', (group_id,))
        
        # Delete all subgroups in this group
        conn.execute('DELETE FROM batch_subgroups WHERE group_id = ?', (group_id,))
        
        # Remove group assignment from batches
        conn.execute('UPDATE batches SET group_id = NULL WHERE group_id = ?', (group_id,))
        
        # Delete the group
        conn.execute('DELETE FROM batch_groups WHERE id = ?', (group_id,))
        
        # Also delete user access records
        conn.execute('DELETE FROM user_group_access WHERE group_id = ?', (group_id,))
    finally:
        conn.close()


def get_batch_groups():
    """Get all batch groups"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM batch_groups ORDER BY name ASC')
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()

def get_batch_group(group_id):
    """Get a single batch group by ID"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM batch_groups WHERE id = ?', (group_id,))
        result = cursor.fetchone()
        cursor.close()
        return result
    finally:
        conn.close()

def assign_batch_to_group(batch_id, group_id):
    """Assign a batch to a group"""
    conn = get_db_connection()
    try:
        conn.execute('UPDATE batches SET group_id = ? WHERE id = ?', (group_id, batch_id))
    finally:
        conn.close()

def remove_batch_from_group(batch_id):
    """Remove a batch from its group"""
    conn = get_db_connection()
    try:
        conn.execute('UPDATE batches SET group_id = NULL WHERE id = ?', (batch_id,))
    finally:
        conn.close()

def get_batches_in_group(group_id, is_completed=0):
    """Get all batches in a specific group"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if is_completed is None:
            cursor.execute('SELECT * FROM batches WHERE group_id = ? ORDER BY name ASC', (group_id,))
        else:
            cursor.execute('SELECT * FROM batches WHERE group_id = ? AND is_completed = ? ORDER BY name ASC', (group_id, is_completed))
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()

def get_batches_in_subgroup(subgroup_id, is_completed=0):
    """Get all batches in a specific subgroup"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if is_completed is None:
            cursor.execute('SELECT * FROM batches WHERE subgroup_id = ? ORDER BY name ASC', (subgroup_id,))
        else:
            cursor.execute('SELECT * FROM batches WHERE subgroup_id = ? AND is_completed = ? ORDER BY name ASC', (subgroup_id, is_completed))
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()

def get_ungrouped_batches(is_completed=0):
    """Get all batches not assigned to any group"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if is_completed is None:
            cursor.execute('SELECT * FROM batches WHERE group_id IS NULL ORDER BY name ASC')
        else:
            cursor.execute('SELECT * FROM batches WHERE group_id IS NULL AND is_completed = ? ORDER BY name ASC', (is_completed,))
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()

# -------- User Group Access Management --------

def grant_user_group_access(user_email, group_id, granted_by):
    """Grant a user access to a batch group"""
    conn = get_db_connection()
    try:
        conn.execute('''
            INSERT OR REPLACE INTO user_group_access (user_email, group_id, granted_by) 
            VALUES (?, ?, ?)
        ''', (user_email, group_id, granted_by))
    finally:
        conn.close()

def revoke_user_group_access(user_email, group_id):
    """Revoke a user's access to a batch group"""
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM user_group_access WHERE user_email = ? AND group_id = ?', 
                    (user_email, group_id))
    finally:
        conn.close()

def get_user_group_access(user_email):
    """Get all groups a user has access to"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT bg.* FROM batch_groups bg
            JOIN user_group_access uga ON bg.id = uga.group_id
            WHERE uga.user_email = ?
            ORDER BY bg.name ASC
        ''', (user_email,))
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()

def get_group_users(group_id):
    """Get all users who have access to a group"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT u.email, u.first_name, u.last_name, u.role, uga.granted_at, uga.granted_by
            FROM users u
            JOIN user_group_access uga ON u.email = uga.user_email
            WHERE uga.group_id = ?
            ORDER BY u.email ASC
        ''', (group_id,))
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()

def user_has_group_access(user_email, group_id):
    """Check if a user has access to a specific group"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM user_group_access WHERE user_email = ? AND group_id = ?', 
                      (user_email, group_id))
        result = cursor.fetchone() is not None
        cursor.close()
        return result
    finally:
        conn.close()

def get_batches_for_user(user_email, user_role, is_completed=0):
    """Get batches accessible to a user based on their group access.
    Superadmin and admin see all batches; others see only batches in their assigned groups plus ungrouped.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        if user_role in ['superadmin', 'admin']:
            # Admins see all batches
            if is_completed is None:
                cursor.execute('SELECT * FROM batches ORDER BY name ASC')
            else:
                cursor.execute('SELECT * FROM batches WHERE is_completed = ? ORDER BY name ASC', (is_completed,))
        else:
            # Regular users see batches in their assigned groups + ungrouped batches
            if is_completed is None:
                cursor.execute('''
                    SELECT DISTINCT b.* FROM batches b
                    LEFT JOIN user_group_access uga ON b.group_id = uga.group_id
                    WHERE b.group_id IS NULL 
                       OR uga.user_email = ?
                    ORDER BY b.name ASC
                ''', (user_email,))
            else:
                cursor.execute('''
                    SELECT DISTINCT b.* FROM batches b
                    LEFT JOIN user_group_access uga ON b.group_id = uga.group_id
                    WHERE (b.group_id IS NULL OR uga.user_email = ?)
                      AND b.is_completed = ?
                    ORDER BY b.name ASC
                ''', (user_email, is_completed))
        
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()

def get_groups_for_user(user_email, user_role):
    """Get groups accessible to a user.
    Superadmin and admin see all groups; others see only their assigned groups.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        if user_role in ['superadmin', 'admin']:
            cursor.execute('SELECT * FROM batch_groups ORDER BY name ASC')
        else:
            cursor.execute('''
                SELECT bg.* FROM batch_groups bg
                JOIN user_group_access uga ON bg.id = uga.group_id
                WHERE uga.user_email = ?
                ORDER BY bg.name ASC
            ''', (user_email,))
        
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()

def get_batch_count_by_group(is_completed=0):
    """Get count of batches in each group"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if is_completed is None:
            cursor.execute('''
                SELECT group_id, COUNT(*) as count 
                FROM batches 
                WHERE group_id IS NOT NULL 
                GROUP BY group_id
            ''')
        else:
            cursor.execute('''
                SELECT group_id, COUNT(*) as count 
                FROM batches 
                WHERE group_id IS NOT NULL AND is_completed = ?
                GROUP BY group_id
            ''', (is_completed,))
        result = {row['group_id']: row['count'] for row in cursor.fetchall()}
        cursor.close()
        return result
    finally:
        conn.close()


# -------- Batch Sub-Group Management Helpers --------

def create_batch_subgroup(name, description, group_id, created_by):
    """Create a new batch subgroup"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO batch_subgroups (name, description, group_id, created_by) 
            VALUES (?, ?, ?, ?)
        ''', (name, description, group_id, created_by))
        last_id = cursor.lastrowid
        cursor.close()
        return last_id
    finally:
        conn.close()

def update_batch_subgroup(subgroup_id, name=None, description=None, is_completed=None):
    """Update a batch subgroup"""
    updates = {}
    if name is not None:
        updates['name'] = name
    if description is not None:
        updates['description'] = description
    if is_completed is not None:
        updates['is_completed'] = is_completed
    if not updates:
        return
    conn = get_db_connection()
    try:
        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [subgroup_id]
        conn.execute(f'UPDATE batch_subgroups SET {set_clause} WHERE id = ?', values)
    finally:
        conn.close()

def delete_batch_subgroup(subgroup_id):
    """Delete a batch subgroup (batches in it become unassigned from subgroup but keep parent group)"""
    conn = get_db_connection()
    try:
        # First, remove subgroup assignment from batches
        conn.execute('UPDATE batches SET subgroup_id = NULL WHERE subgroup_id = ?', (subgroup_id,))
        # Then delete the subgroup
        conn.execute('DELETE FROM batch_subgroups WHERE id = ?', (subgroup_id,))
    finally:
        conn.close()

def get_batch_subgroups(group_id=None, is_completed=None):
    """Get all batch subgroups, optionally filtered by group_id and/or completion status"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        conditions = []
        params = []
        if group_id is not None:
            conditions.append('group_id = ?')
            params.append(group_id)
        if is_completed is not None:
            conditions.append('is_completed = ?')
            params.append(is_completed)
        query = 'SELECT * FROM batch_subgroups'
        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)
        query += ' ORDER BY name ASC'
        cursor.execute(query, params)
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()

def get_batch_subgroup(subgroup_id):
    """Get a single batch subgroup by ID"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM batch_subgroups WHERE id = ?', (subgroup_id,))
        result = cursor.fetchone()
        cursor.close()
        return result
    finally:
        conn.close()

def get_subgroups_for_user(user_email, user_role, group_id=None, is_completed=None):
    """Get subgroups accessible to a user.
    Superadmin and admin see all subgroups; others see subgroups of groups they have access to.
    Pass is_completed=0/1 to restrict to active/completed subgroups, or leave None for both.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if user_role in ['superadmin', 'admin']:
            conditions = []
            params = []
            if group_id is not None:
                conditions.append('group_id = ?')
                params.append(group_id)
            if is_completed is not None:
                conditions.append('is_completed = ?')
                params.append(is_completed)
            query = 'SELECT * FROM batch_subgroups'
            if conditions:
                query += ' WHERE ' + ' AND '.join(conditions)
            query += ' ORDER BY name ASC'
            cursor.execute(query, params)
        else:
            conditions = ['uga.user_email = ?']
            params = [user_email]
            if group_id is not None:
                conditions.append('bs.group_id = ?')
                params.append(group_id)
            if is_completed is not None:
                conditions.append('bs.is_completed = ?')
                params.append(is_completed)
            query = '''
                SELECT bs.* FROM batch_subgroups bs
                JOIN user_group_access uga ON bs.group_id = uga.group_id
                WHERE ''' + ' AND '.join(conditions) + '''
                ORDER BY bs.name ASC
            '''
            cursor.execute(query, params)
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()

def assign_batch_to_subgroup(batch_id, subgroup_id):
    """Assign a batch to a subgroup (and automatically to its parent group)"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if subgroup_id and subgroup_id > 0:
            # Find parent group_id
            cursor.execute('SELECT group_id FROM batch_subgroups WHERE id = ?', (subgroup_id,))
            row = cursor.fetchone()
            if row:
                group_id = row['group_id']
                conn.execute('UPDATE batches SET group_id = ?, subgroup_id = ? WHERE id = ?', 
                             (group_id, subgroup_id, batch_id))
        else:
            conn.execute('UPDATE batches SET subgroup_id = NULL WHERE id = ?', (batch_id,))
        cursor.close()
    finally:
        conn.close()

def remove_batch_from_subgroup(batch_id):
    """Remove a batch from its subgroup"""
    conn = get_db_connection()
    try:
        conn.execute('UPDATE batches SET subgroup_id = NULL WHERE id = ?', (batch_id,))
    finally:
        conn.close()


# -------- Batch & Batch Student Helpers (for Smart Matcher) --------

def create_batch(name, description, created_by, group_id=None, subgroup_id=None, is_completed=0, required_duration_minutes=None):
    """Create a new batch, optionally in a group and subgroup"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO batches (name, description, created_by, group_id, subgroup_id, is_completed, required_duration_minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (name, description, created_by, group_id, subgroup_id, is_completed, required_duration_minutes))
        last_id = cursor.lastrowid
        cursor.close()
        return last_id
    finally:
        conn.close()


def update_batch(batch_id, name=None, description=None, group_id=None, subgroup_id=None, is_completed=None, required_duration_minutes='__unchanged__'):
    """Update a batch - pass group_id=0/subgroup_id=0 to remove from group/subgroup, None to leave unchanged.
    required_duration_minutes defaults to a sentinel (not None) so calls that don't mention
    it -- completing, restoring, moving a batch -- don't silently wipe its configured duration;
    pass required_duration_minutes=None explicitly to clear it."""
    updates = {}
    if name is not None:
        updates['name'] = name
    if description is not None:
        updates['description'] = description
    if group_id is not None:
        # group_id=0 means remove from group (set to NULL)
        updates['group_id'] = None if group_id == 0 else group_id
    if subgroup_id is not None:
        # subgroup_id=0 means remove from subgroup (set to NULL)
        updates['subgroup_id'] = None if subgroup_id == 0 else subgroup_id
    if is_completed is not None:
        updates['is_completed'] = is_completed
    if required_duration_minutes != '__unchanged__':
        updates['required_duration_minutes'] = required_duration_minutes
        
    if not updates:
        return
    conn = get_db_connection()
    try:
        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [batch_id]
        conn.execute(f'UPDATE batches SET {set_clause} WHERE id = ?', values)
    finally:
        conn.close()


def delete_batch(batch_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM batches WHERE id = ?', (batch_id,))
    finally:
        conn.close()


def get_batches(is_completed=0):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # CHANGED: Sort batches alphabetically by name (A-Z) for easier finding
        if is_completed is None:
            cursor.execute('SELECT * FROM batches ORDER BY name ASC')
        else:
            cursor.execute('SELECT * FROM batches WHERE is_completed = ? ORDER BY name ASC', (is_completed,))
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()


def get_batch(batch_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM batches WHERE id = ?', (batch_id,))
        result = cursor.fetchone()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()


def add_student_to_batch(batch_id, name, email, institute_email=None, contact_number=None):
    conn = get_db_connection()
    try:
        conn.execute(
            'INSERT INTO batch_students (batch_id, name, email, institute_email, contact_number) VALUES (?, ?, ?, ?, ?)',
            (batch_id, name, email, institute_email or None, contact_number or None)
        )
    finally:
        conn.close()


def update_batch_student(student_id, name=None, email=None, institute_email=None, contact_number=None):
    updates = {}
    if name is not None:
        updates['name'] = name
    if email is not None:
        updates['email'] = email
    if institute_email is not None:
        updates['institute_email'] = institute_email or None
    if contact_number is not None:
        updates['contact_number'] = contact_number or None
    if not updates:
        return
    conn = get_db_connection()
    try:
        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [student_id]
        conn.execute(f'UPDATE batch_students SET {set_clause} WHERE id = ?', values)
    finally:
        conn.close()


def delete_batch_student(student_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM batch_students WHERE id = ?', (student_id,))
    finally:
        conn.close()


def get_batch_students(batch_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM batch_students WHERE batch_id = ? ORDER BY name ASC', (batch_id,))
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()


def get_all_batch_students_with_batches():
    """Return all students joined with their batches, for full export."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT b.id AS batch_id, b.name AS batch_name, s.id AS student_id,
                   s.name AS student_name, s.email AS student_email,
                   s.institute_email AS student_institute_email,
                   s.contact_number AS student_contact_number
            FROM batch_students s
            JOIN batches b ON s.batch_id = b.id
            ORDER BY b.name ASC, s.name ASC
        ''')
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()


# ----------- Batch-Scoped Learning (Smart Matcher <-> Final Excel Generator) -----------

VALID_BATCH_RECORD_SOURCES = ('single_csv_upload', 'manual_review')


def upsert_batch_record(batch_id, zoom_display_name, roster_name_matched, confidence=None, source='single_csv_upload'):
    """Learn (or reinforce) a Zoom-name -> roster-name pairing for one batch.

    Strictly scoped by batch_id: the same zoom_display_name in a different batch is a
    separate row and never affects or is affected by this one.

    - New (batch_id, zoom_display_name) pair: inserted as-is.
    - Existing pair: seen_count is incremented and last_seen_date refreshed. The stored
      roster_name_matched/confidence/source are only overwritten when the new source is
      'manual_review', so a lower-confidence automatic re-match can never silently clobber
      a previously confirmed manual correction.

    Raises ValueError if batch_id is missing or does not reference a real batch.
    """
    if not batch_id:
        raise ValueError("upsert_batch_record requires a batch_id.")
    if source not in VALID_BATCH_RECORD_SOURCES:
        raise ValueError(f"Invalid source '{source}'. Must be one of {VALID_BATCH_RECORD_SOURCES}.")
    if not zoom_display_name or not roster_name_matched:
        raise ValueError("upsert_batch_record requires zoom_display_name and roster_name_matched.")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM batches WHERE id = ?', (batch_id,))
        if cursor.fetchone() is None:
            cursor.close()
            raise ValueError(f"batch_id {batch_id} does not exist in batches table.")

        cursor.execute('''
            INSERT INTO batch_records (batch_id, zoom_display_name, roster_name_matched, confidence, source, seen_count, last_seen_date)
            VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(batch_id, zoom_display_name) DO UPDATE SET
                seen_count = seen_count + 1,
                last_seen_date = CURRENT_TIMESTAMP,
                roster_name_matched = CASE WHEN excluded.source = 'manual_review' THEN excluded.roster_name_matched ELSE roster_name_matched END,
                confidence = CASE WHEN excluded.source = 'manual_review' THEN excluded.confidence ELSE confidence END,
                source = CASE WHEN excluded.source = 'manual_review' THEN excluded.source ELSE source END
        ''', (batch_id, zoom_display_name, roster_name_matched, confidence, source))
        cursor.close()
    finally:
        conn.close()


def touch_batch_record(batch_id, zoom_display_name):
    """Bump seen_count/last_seen_date for an already-learned pair without changing the match.

    Used when a Tier-1 (already-learned) hit is reused on a later run: it confirms the
    pairing is still being seen, but must not re-trigger learning as if it were new data.
    """
    if not batch_id or not zoom_display_name:
        return
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE batch_records SET seen_count = seen_count + 1, last_seen_date = CURRENT_TIMESTAMP
            WHERE batch_id = ? AND zoom_display_name = ?
        ''', (batch_id, zoom_display_name))
    finally:
        conn.close()


def get_batch_record(batch_id, zoom_display_name):
    """Tier-1 lookup: exact (batch_id, zoom_display_name) match, or None."""
    if not batch_id or not zoom_display_name:
        return None
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM batch_records WHERE batch_id = ? AND zoom_display_name = ?
        ''', (batch_id, zoom_display_name))
        result = cursor.fetchone()
        cursor.close()
        return result
    finally:
        conn.close()


def get_batch_records_map(batch_id):
    """All learned records for one batch, as {zoom_display_name: sqlite3.Row}.

    Built for Smart Matcher's Tier-1 lookup during a matching run: one query instead of
    one per raw name, still strictly scoped to this batch_id.
    """
    if not batch_id:
        return {}
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM batch_records WHERE batch_id = ?', (batch_id,))
        rows = cursor.fetchall()
        cursor.close()
        return {row['zoom_display_name']: row for row in rows}
    finally:
        conn.close()


def get_batch_records(batch_id):
    """All learned Zoom-name -> roster-name records for one batch, most recently seen first.

    For display (e.g. the batch details page) rather than lookup -- see
    get_batch_records_map for the dict form used during matching.
    """
    if not batch_id:
        return []
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM batch_records WHERE batch_id = ? ORDER BY last_seen_date DESC', (batch_id,))
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()


DEFAULT_SUPERADMIN_EMAIL = 'superadmin@attendancify.com'
# The old fixed default. Kept only so an existing install can be recognised as
# still using it and made to change -- never handed out again.
LEGACY_DEFAULT_SUPERADMIN_PASSWORD = 'admin123'


def ensure_superadmin_exists():
    """Ensure superadmin user exists, create if not.

    A fresh install gets a random password, printed once to the server log, and
    must change it at first login. The old build shipped every install with the
    same 'admin123' and did not ask for it to be changed, which meant anyone who
    knew the project could sign in as superadmin and read every student record.
    """
    existing = get_user(DEFAULT_SUPERADMIN_EMAIL)
    if not existing:
        import hashlib
        import secrets
        password = secrets.token_urlsafe(12)
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 260000)
        create_user(
            email=DEFAULT_SUPERADMIN_EMAIL,
            password_hash=f"pbkdf2_sha256$260000${salt.hex()}${digest.hex()}",
            role='superadmin',
            expires_at=None,
            must_change_password=True,
            first_name='Super',
            last_name='Admin',
            can_manage_roles=True,
            can_manage_batch_matching=True
        )
        print('=' * 68)
        print('Created the superadmin account.')
        print(f'  email    : {DEFAULT_SUPERADMIN_EMAIL}')
        print(f'  password : {password}')
        print('Sign in now and set your own password. This is shown once.')
        print('=' * 68)
        return True
    return False

# -------- Processed Files Management --------

def register_processed_file(file_path, file_name, user_email, processing_type, eligible_for_deletion_at=None):
    """Register a processed file in the database"""
    conn = get_db_connection()
    try:
        try:
            conn.execute('''
                INSERT OR REPLACE INTO processed_files 
                (file_path, file_name, user_email, processing_type, eligible_for_deletion_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (file_path, file_name, user_email, processing_type, eligible_for_deletion_at))
            return True
        except Exception as e:
            print(f"Error registering processed file: {e}")
            return False
    finally:
        conn.close()

def get_processed_files(user_email=None, processing_type=None, include_deleted=False):
    """Get processed files, optionally filtered by user and type - Optimized"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Create index if it doesn't exist (for faster queries)
        try:
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_processed_files_user_email ON processed_files(user_email)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_processed_files_processing_type ON processed_files(processing_type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_processed_files_created_at ON processed_files(created_at)')
        except Exception:
            pass
        
        query = 'SELECT * FROM processed_files WHERE 1=1'
        params = []
        
        if not include_deleted:
            query += ' AND manually_deleted = 0'
        
        if user_email:
            query += ' AND user_email = ?'
            params.append(user_email)
        
        if processing_type:
            query += ' AND processing_type = ?'
            params.append(processing_type)
        
        query += ' ORDER BY created_at DESC'
        
        cursor.execute(query, params)
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def get_processed_file_by_path(file_path):
    """Get a processed file by its path"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM processed_files WHERE file_path = ?', (file_path,))
        result = cursor.fetchone()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def mark_file_eligible_for_deletion(file_path, eligible_at):
    """Mark a file as eligible for deletion at a specific time"""
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE processed_files 
            SET eligible_for_deletion_at = ?
            WHERE file_path = ?
        ''', (eligible_at, file_path))
    finally:
        conn.close()

def mark_file_manually_deleted(file_path):
    """Mark a file as manually deleted"""
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE processed_files 
            SET manually_deleted = 1
            WHERE file_path = ?
        ''', (file_path,))
    finally:
        conn.close()

def update_processed_file_name(file_id, new_name):
    """Update the display name of a processed file"""
    conn = get_db_connection()
    try:
        try:
            conn.execute('''
                UPDATE processed_files 
                SET file_name = ?
                WHERE id = ?
            ''', (new_name, file_id))
            return True
        except Exception as e:
            print(f"Error updating file name: {e}")
            return False
    finally:
        conn.close()

def get_files_eligible_for_auto_deletion():
    """Get files that are eligible for automatic deletion (15 minutes after eligibility time)
    
    IMPORTANT: This uses local time comparison to avoid UTC/local time mismatch issues.
    Files are deleted 15 minutes AFTER their eligible_for_deletion_at time.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Calculate the cutoff time in local time (15 minutes ago)
        # Files with eligible_for_deletion_at <= cutoff_time should be deleted
        from datetime import datetime, timedelta
        cutoff_time = (datetime.now() - timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
        
        # Files eligible for deletion are those where eligible_for_deletion_at is not null
        # and was 15+ minutes ago (using local time), and not manually deleted
        cursor.execute('''
            SELECT * FROM processed_files 
            WHERE eligible_for_deletion_at IS NOT NULL
            AND eligible_for_deletion_at <= ?
            AND manually_deleted = 0
        ''', (cutoff_time,))
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def permanently_delete_old_records(days_old=30):
    """Permanently remove database records for files that were deleted more than X days ago"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Delete records that were marked as deleted more than X days ago
        # Also delete associated permissions
        deleted_count = 0
        try:
            # First, get file IDs that will be deleted
            cursor.execute('''
                SELECT id FROM processed_files 
                WHERE manually_deleted = 1
                AND datetime(created_at) <= datetime('now', '-' || ? || ' days')
            ''', (days_old,))
            file_ids = [row[0] for row in cursor.fetchall()]
            
            if file_ids:
                # Delete associated permissions first
                placeholders = ','.join('?' * len(file_ids))
                cursor.execute(f'''
                    DELETE FROM processed_files_permissions 
                    WHERE file_id IN ({placeholders})
                ''', file_ids)
                
                # Then delete the file records
                cursor.execute(f'''
                    DELETE FROM processed_files 
                    WHERE id IN ({placeholders})
                ''', file_ids)
                
                deleted_count = len(file_ids)
        except Exception as e:
            print(f"Error permanently deleting old records: {e}")
        finally:
            cursor.close()  # CRITICAL: Close cursor
        
        return deleted_count
    finally:
        conn.close()

def grant_file_access(file_id, user_email, granted_by):
    """Grant a user access to a processed file"""
    conn = get_db_connection()
    try:
        try:
            conn.execute('''
                INSERT OR REPLACE INTO processed_files_permissions 
                (file_id, user_email, granted_by)
                VALUES (?, ?, ?)
            ''', (file_id, user_email, granted_by))
            return True
        except Exception as e:
            print(f"Error granting file access: {e}")
            return False
    finally:
        conn.close()

def revoke_file_access(file_id, user_email):
    """Revoke a user's access to a processed file"""
    conn = get_db_connection()
    try:
        conn.execute('''
            DELETE FROM processed_files_permissions 
            WHERE file_id = ? AND user_email = ?
        ''', (file_id, user_email))
    finally:
        conn.close()

def get_file_permissions(file_id):
    """Get all permissions for a file"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM processed_files_permissions 
            WHERE file_id = ?
        ''', (file_id,))
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def can_user_access_file(file_id, user_email):
    """Check if a user can access a file (owner, superadmin, or has permission)"""
    file = None
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM processed_files WHERE id = ?', (file_id,))
        file = cursor.fetchone()
        cursor.close()  # CRITICAL: Close cursor
    finally:
        conn.close()
    
    if not file:
        return False
    
    # Owner can always access
    if file['user_email'] == user_email:
        return True
    
    # Check if user is superadmin
    user_row = get_user(user_email)
    # get_user returns a sqlite3.Row, which has no .get() - calling it raised
    # AttributeError for every file the caller did not own, breaking bulk
    # downloads for non-superadmin users.
    user = dict(user_row) if user_row is not None and hasattr(user_row, 'keys') else user_row
    if user and user.get('role') == 'superadmin':
        return True
    
    # Check if user has explicit permission
    permissions = get_file_permissions(file_id)
    for perm in permissions:
        if perm['user_email'] == user_email:
            return True
    
    return False

# -------- Login History & Dashboard Functions --------

def log_user_login(user_email, ip_address):
    """Log a user login with IP address"""
    conn = get_db_connection()
    try:
        conn.execute('''
            INSERT INTO login_history (user_email, ip_address, login_time)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (user_email, ip_address))
    finally:
        conn.close()

def get_last_login(user_email):
    """Get the last login time and IP for a user"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT login_time, ip_address 
            FROM login_history 
            WHERE user_email = ? 
            ORDER BY login_time DESC 
            LIMIT 1
        ''', (user_email,))
        result = cursor.fetchone()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def get_user_login_history(user_email=None, limit=50):
    """Get login history for a user or all users"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if user_email:
            cursor.execute('''
                SELECT * FROM login_history 
                WHERE user_email = ? 
                ORDER BY login_time DESC 
                LIMIT ?
            ''', (user_email, limit))
        else:
            cursor.execute('''
                SELECT * FROM login_history 
                ORDER BY login_time DESC 
                LIMIT ?
            ''', (limit,))
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def get_dashboard_stats(user_email=None):
    """Get dashboard statistics for a user or all users - Optimized with indexes"""
    conn = get_db_connection()
    try:
        stats = {}
        
        # Time saved calculation constants (in minutes)
        TIME_SAVED_ATTENDANCE = 7  # Manual attendance creation
        TIME_SAVED_PA_STATUS = 7   # Manual P/A status marking
        TIME_SAVED_MATCHING = 8     # Manual matching
        TOTAL_TIME_SAVED_PER_OPERATION = TIME_SAVED_ATTENDANCE + TIME_SAVED_PA_STATUS + TIME_SAVED_MATCHING
        
        cursor = conn.cursor()
        
        # Get processing stats - Filter out hidden users' activity
        if user_email:
            cursor.execute('''
                SELECT 
                    processing_type,
                    COUNT(*) as count,
                    SUM(file_size) as total_size,
                    AVG(processing_time) as avg_time
                FROM file_processing_logs 
                WHERE user_email = ?
                GROUP BY processing_type
            ''', (user_email,))
            processing_stats = cursor.fetchall()
            
            cursor.execute('''
                SELECT COUNT(*) as total_files
                FROM file_processing_logs 
                WHERE user_email = ?
            ''', (user_email,))
            total_files = cursor.fetchone()['total_files']
        else:
            # Count only work done by users that still exist and are not hidden,
            # so removing a user takes their rows out of the totals too.
            cursor.execute('''
                SELECT
                    fpl.processing_type,
                    COUNT(*) as count,
                    SUM(fpl.file_size) as total_size,
                    AVG(fpl.processing_time) as avg_time
                FROM file_processing_logs fpl
                INNER JOIN users u ON fpl.user_email = u.email
                WHERE (u.hidden = 0 OR u.hidden IS NULL)
                GROUP BY fpl.processing_type
            ''')
            processing_stats = cursor.fetchall()

            cursor.execute('''
                SELECT COUNT(*) as total_files
                FROM file_processing_logs fpl
                INNER JOIN users u ON fpl.user_email = u.email
                WHERE (u.hidden = 0 OR u.hidden IS NULL)
            ''')
            total_files = cursor.fetchone()['total_files']
        
        # Per-tool breakdown. Smart Matcher writes one row per file, under a few
        # different type names depending on how the run was set up:
        #   attendance_matching_batch  - roster came from a saved batch
        #   attendance_matching_master - the uploaded roster file
        #   attendance_matching_raw    - the uploaded attendance file(s)
        #   attendance_matching        - legacy rows from older versions
        # Counting only some of them (as the dashboard used to) under-reports the
        # tool, so every variant is folded into one bucket here.
        MATCHING_TYPES = ('attendance_matching', 'attendance_matching_batch',
                          'attendance_matching_master', 'attendance_matching_raw')
        counts = {row['processing_type']: row['count'] for row in processing_stats}

        breakdown = {
            'attendance': counts.get('attendance_generation', 0),
            'matching': sum(counts.get(t, 0) for t in MATCHING_TYPES),
            'final_excel': counts.get('final_excel', 0),
            'raw_excel': counts.get('raw_excel_generation', 0),
        }

        # Calculate time saved
        # Attendance Generator saves: 7 min (attendance) + 7 min (P/A status) = 14 minutes
        # Smart Matcher saves: 8 minutes per matching run. A run logs one row per
        # file, and the master/roster file is an input rather than a run of its
        # own, so it is left out here to avoid counting the same match twice.
        matching_runs = (counts.get('attendance_matching', 0)
                         + counts.get('attendance_matching_batch', 0)
                         + counts.get('attendance_matching_raw', 0))
        total_time_saved = (
            breakdown['attendance'] * (TIME_SAVED_ATTENDANCE + TIME_SAVED_PA_STATUS)
            + matching_runs * TIME_SAVED_MATCHING
        )

        stats['total_files_processed'] = total_files
        stats['processing_stats'] = [dict(s) for s in processing_stats]
        stats['breakdown'] = breakdown
        stats['total_time_saved_minutes'] = total_time_saved
        stats['total_time_saved_hours'] = round(total_time_saved / 60, 2)
        stats['total_time_saved_days'] = round(total_time_saved / (60 * 24), 2)
        
        # Get user activity stats
        if user_email:
            cursor.execute('''
                SELECT COUNT(*) as login_count,
                       MAX(login_time) as last_login,
                       MIN(login_time) as first_login
                FROM login_history 
                WHERE user_email = ?
            ''', (user_email,))
        else:
            # Only count users that still exist and are not hidden. Counting
            # distinct emails in login_history instead (as this used to) kept
            # counting people long after their account was deleted, so the
            # dashboard never came back down when users were removed.
            cursor.execute('''
                SELECT
                    (SELECT COUNT(*) FROM users WHERE hidden = 0 OR hidden IS NULL) as total_users,
                    COUNT(*) as total_logins,
                    MAX(lh.login_time) as last_login
                FROM login_history lh
                INNER JOIN users u ON lh.user_email = u.email
                WHERE (u.hidden = 0 OR u.hidden IS NULL)
            ''')

        activity_stats = cursor.fetchone()
        stats['activity'] = dict(activity_stats)
        
        # Get recent activity - Exclude deleted and hidden users
        if user_email:
            cursor.execute('''
                SELECT * FROM login_history 
                WHERE user_email = ? 
                ORDER BY login_time DESC 
                LIMIT 10
            ''', (user_email,))
        else:
            # Only show logins from existing, non-hidden users
            cursor.execute('''
                SELECT lh.*, u.first_name, u.last_name 
                FROM login_history lh
                INNER JOIN users u ON lh.user_email = u.email
                WHERE (u.hidden = 0 OR u.hidden IS NULL)
                ORDER BY lh.login_time DESC 
                LIMIT 20
            ''')
        
        stats['recent_logins'] = [dict(row) for row in cursor.fetchall()]
        
        cursor.close()  # CRITICAL: Close cursor before returning
        return stats
    finally:
        conn.close()

MATCHING_PROCESSING_TYPES = ('attendance_matching', 'attendance_matching_batch',
                             'attendance_matching_master', 'attendance_matching_raw')


def _series_last_days(cursor, sql, params, days=7):
    """Run a "date -> count" query and pad it into a dense day-by-day list
    (oldest first) so the dashboard sparklines have one point per day."""
    cursor.execute(sql, params)
    by_day = {row['day']: row['count'] for row in cursor.fetchall()}
    today = datetime.now().date()
    return [by_day.get((today - timedelta(days=offset)).isoformat(), 0)
            for offset in range(days - 1, -1, -1)]


def get_dashboard_trends(user_email=None, days=7):
    """Trend figures and 7-day series behind the dashboard stat cards.

    Scoped to one user when user_email is given; otherwise counts every
    existing, non-hidden user (matching get_dashboard_stats).
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        scope_sql = ' AND fpl.user_email = ?' if user_email else ''
        scope_params = (user_email,) if user_email else ()

        # This month vs last month, for the "x% this month" captions
        cursor.execute(f'''
            SELECT
                SUM(CASE WHEN strftime('%Y-%m', fpl.timestamp) = strftime('%Y-%m', 'now') THEN 1 ELSE 0 END) as this_month,
                SUM(CASE WHEN strftime('%Y-%m', fpl.timestamp) = strftime('%Y-%m', 'now', '-1 month') THEN 1 ELSE 0 END) as last_month
            FROM file_processing_logs fpl
            INNER JOIN users u ON fpl.user_email = u.email
            WHERE (u.hidden = 0 OR u.hidden IS NULL){scope_sql}
        ''', scope_params)
        row = cursor.fetchone()
        files_this_month = row['this_month'] or 0
        files_last_month = row['last_month'] or 0

        # Hours saved follows the same rows, weighted per tool
        cursor.execute(f'''
            SELECT fpl.processing_type,
                   SUM(CASE WHEN strftime('%Y-%m', fpl.timestamp) = strftime('%Y-%m', 'now') THEN 1 ELSE 0 END) as this_month,
                   SUM(CASE WHEN strftime('%Y-%m', fpl.timestamp) = strftime('%Y-%m', 'now', '-1 month') THEN 1 ELSE 0 END) as last_month
            FROM file_processing_logs fpl
            INNER JOIN users u ON fpl.user_email = u.email
            WHERE (u.hidden = 0 OR u.hidden IS NULL){scope_sql}
            GROUP BY fpl.processing_type
        ''', scope_params)

        minutes_this_month = minutes_last_month = 0
        for r in cursor.fetchall():
            if r['processing_type'] == 'attendance_generation':
                weight = 14
            elif r['processing_type'] in ('attendance_matching', 'attendance_matching_batch', 'attendance_matching_raw'):
                weight = 8
            else:
                weight = 0
            minutes_this_month += (r['this_month'] or 0) * weight
            minutes_last_month += (r['last_month'] or 0) * weight

        # New users this month + logins today (whole-site figures only)
        cursor.execute('''
            SELECT COUNT(*) as c FROM users
            WHERE (hidden = 0 OR hidden IS NULL)
              AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        ''')
        new_users_this_month = cursor.fetchone()['c']

        login_scope_sql = ' AND lh.user_email = ?' if user_email else ''
        cursor.execute(f'''
            SELECT COUNT(*) as c
            FROM login_history lh
            INNER JOIN users u ON lh.user_email = u.email
            WHERE (u.hidden = 0 OR u.hidden IS NULL)
              AND date(lh.login_time) = date('now'){login_scope_sql}
        ''', scope_params)
        logins_today = cursor.fetchone()['c']

        # 7-day series for the sparklines
        files_series = _series_last_days(cursor, f'''
            SELECT date(fpl.timestamp) as day, COUNT(*) as count
            FROM file_processing_logs fpl
            INNER JOIN users u ON fpl.user_email = u.email
            WHERE (u.hidden = 0 OR u.hidden IS NULL)
              AND date(fpl.timestamp) >= date('now', ?){scope_sql}
            GROUP BY day
        ''', (f'-{days - 1} days',) + scope_params, days)

        logins_series = _series_last_days(cursor, f'''
            SELECT date(lh.login_time) as day, COUNT(*) as count
            FROM login_history lh
            INNER JOIN users u ON lh.user_email = u.email
            WHERE (u.hidden = 0 OR u.hidden IS NULL)
              AND date(lh.login_time) >= date('now', ?){login_scope_sql}
            GROUP BY day
        ''', (f'-{days - 1} days',) + scope_params, days)

        users_series = _series_last_days(cursor, '''
            SELECT date(created_at) as day, COUNT(*) as count
            FROM users
            WHERE (hidden = 0 OR hidden IS NULL) AND date(created_at) >= date('now', ?)
            GROUP BY day
        ''', (f'-{days - 1} days',), days)

        def pct_change(now, before):
            if not before:
                return 100 if now else 0
            return int(round((now - before) / before * 100))

        cursor.close()
        return {
            'files_this_month': files_this_month,
            'files_pct': pct_change(files_this_month, files_last_month),
            'hours_this_month': round(minutes_this_month / 60, 1),
            'hours_pct': pct_change(minutes_this_month, minutes_last_month),
            'new_users_this_month': new_users_this_month,
            'logins_today': logins_today,
            'files_series': files_series,
            'logins_series': logins_series,
            'users_series': users_series,
            # hours track files one-for-one, so the same shape works for both
            'hours_series': files_series,
        }
    finally:
        conn.close()


def get_tool_last_used(user_email=None):
    """When each tool was last run, for the "Continue Working" cards."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        scope_sql = ' AND user_email = ?' if user_email else ''
        params = (user_email,) if user_email else ()
        cursor.execute(f'''
            SELECT processing_type, MAX(timestamp) as last_used
            FROM file_processing_logs
            WHERE 1=1{scope_sql}
            GROUP BY processing_type
        ''', params)
        by_type = {row['processing_type']: row['last_used'] for row in cursor.fetchall()}
        cursor.close()

        matching = [by_type[t] for t in MATCHING_PROCESSING_TYPES if by_type.get(t)]
        return {
            'attendance': by_type.get('attendance_generation'),
            'matching': max(matching) if matching else None,
            'final_excel': by_type.get('final_excel'),
        }
    finally:
        conn.close()


def get_usage_record_counts():
    """How many rows back the dashboard counters, for the reset UI."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) as c FROM file_processing_logs')
        processing = cursor.fetchone()['c']
        cursor.execute('SELECT COUNT(*) as c FROM login_history')
        logins = cursor.fetchone()['c']
        cursor.close()
        return {'processing_records': processing, 'login_records': logins}
    finally:
        conn.close()


def clear_login_history():
    """Wipe login history: Total Logins and the recent-login list reset.
    Returns how many rows were removed."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) as c FROM login_history')
        removed = cursor.fetchone()['c']
        cursor.execute('DELETE FROM login_history')
        cursor.close()
        return removed
    finally:
        conn.close()


def get_user_usage_summary():
    """Get usage summary for all users - Optimized query (excludes hidden users)"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Exclude superadmin and hidden users from usage summary
        cursor.execute('''
            SELECT 
                u.email,
                u.first_name,
                u.last_name,
                u.role,
                COUNT(DISTINCT fpl.id) as files_processed,
                MAX(fpl.timestamp) as last_processed,
                MAX(lh.login_time) as last_login,
                MAX(lh.ip_address) as last_ip
            FROM users u
            LEFT JOIN file_processing_logs fpl ON u.email = fpl.user_email
            LEFT JOIN login_history lh ON u.email = lh.user_email
            WHERE u.email != 'superadmin@attendancify.com'
                AND (u.hidden = 0 OR u.hidden IS NULL)
            GROUP BY u.email, u.first_name, u.last_name, u.role
            ORDER BY files_processed DESC, last_login DESC
        ''')
        
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

# ============ Excluded Participants Functions ============

def add_excluded_participant(identifier, identifier_type, added_by, is_permanently_blocked=0):
    """Add a participant to the exclusion list"""
    conn = get_db_connection()
    try:
        try:
            conn.execute('''
                INSERT INTO excluded_participants (identifier, identifier_type, added_by, is_permanently_blocked)
                VALUES (?, ?, ?, ?)
            ''', (identifier.strip().lower(), identifier_type, added_by, is_permanently_blocked))
            return True
        except sqlite3.IntegrityError:
            return False  # Already exists
        except Exception as e:
            print(f"Error adding excluded participant: {e}")
            return False
    finally:
        conn.close()

def toggle_permanent_block(participant_id, is_blocked):
    """Toggle permanent block status for a participant"""
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE excluded_participants 
            SET is_permanently_blocked = ?
            WHERE id = ?
        ''', (1 if is_blocked else 0, participant_id))
        return True
    except Exception as e:
        print(f"Error toggling permanent block: {e}")
        return False
    finally:
        conn.close()

def get_permanently_blocked_identifiers():
    """Get permanently blocked identifiers for filtering from raw data"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT identifier, identifier_type FROM excluded_participants WHERE is_permanently_blocked = 1')
        rows = cursor.fetchall()
        cursor.close()
        
        blocked_names = set()
        blocked_emails = set()
        
        for row in rows:
            if row['identifier_type'] == 'name':
                blocked_names.add(row['identifier'].lower())
            else:
                blocked_emails.add(row['identifier'].lower())
        
        return blocked_names, blocked_emails
    finally:
        conn.close()

def remove_excluded_participant(participant_id):
    """Remove a participant from the exclusion list"""
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM excluded_participants WHERE id = ?', (participant_id,))
    finally:
        conn.close()

def get_excluded_participants():
    """Get all excluded participants"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM excluded_participants 
            ORDER BY identifier_type, identifier
        ''')
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def get_excluded_identifiers():
    """Get excluded identifiers as sets for quick lookup"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT identifier, identifier_type FROM excluded_participants')
        rows = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor
        
        excluded_names = set()
        excluded_emails = set()
        
        for row in rows:
            if row['identifier_type'] == 'name':
                excluded_names.add(row['identifier'].lower())
            else:
                excluded_emails.add(row['identifier'].lower())
        
        return excluded_names, excluded_emails
    finally:
        conn.close()

def bulk_add_excluded_participants(identifiers, identifier_type, added_by, is_permanently_blocked=0):
    """Add multiple participants to the exclusion list"""
    conn = get_db_connection()
    try:
        added = 0
        for identifier in identifiers:
            identifier = identifier.strip()
            if identifier:
                try:
                    conn.execute('''
                        INSERT OR IGNORE INTO excluded_participants (identifier, identifier_type, added_by, is_permanently_blocked)
                        VALUES (?, ?, ?, ?)
                    ''', (identifier.lower(), identifier_type, added_by, is_permanently_blocked))
                    added += 1
                except Exception:
                    pass
        return added
    finally:
        conn.close()

def update_excluded_participant(participant_id, identifier, identifier_type):
    """Update an excluded participant's identifier"""
    conn = get_db_connection()
    try:
        try:
            # Check if the new identifier already exists (excluding current record)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id FROM excluded_participants 
                WHERE identifier = ? AND identifier_type = ? AND id != ?
            ''', (identifier.strip().lower(), identifier_type, participant_id))
            if cursor.fetchone():
                cursor.close()
                return False  # Duplicate exists
            
            # Update the record
            conn.execute('''
                UPDATE excluded_participants 
                SET identifier = ?, identifier_type = ?
                WHERE id = ?
            ''', (identifier.strip().lower(), identifier_type, participant_id))
            cursor.close()  # CRITICAL: Close cursor
            return True
        except Exception as e:
            print(f"Error updating excluded participant: {e}")
            return False
    finally:
        conn.close()

# ============ App Settings Functions ============

def get_app_setting(key, default=None):
    """Get an app setting value"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM app_settings WHERE setting_key = ?', (key,))
        result = cursor.fetchone()
        cursor.close()  # CRITICAL: Close cursor
        
        if result:
            value = result['setting_value']
            setting_type = result['setting_type']
            # Convert value based on type
            if setting_type == 'integer':
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return default
            elif setting_type == 'boolean':
                return value in ('1', 'true', 'True', True, 1)
            elif setting_type == 'float':
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return default
            return value
        return default
    finally:
        conn.close()



def get_all_app_settings():
    """Get all app settings"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM app_settings ORDER BY setting_key')
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def create_app_setting(key, value, setting_type='string', description='', updated_by=None):
    """Create a new app setting"""
    conn = get_db_connection()
    try:
        try:
            conn.execute('''
                INSERT INTO app_settings (setting_key, setting_value, setting_type, description, updated_by)
                VALUES (?, ?, ?, ?, ?)
            ''', (key, str(value), setting_type, description, updated_by))
            return True
        except sqlite3.IntegrityError:
            return False  # Key already exists
    finally:
        conn.close()

def delete_app_setting(key):
    """Delete an app setting"""
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM app_settings WHERE setting_key = ?', (key,))
    finally:
        conn.close()

# ============ Output Format Settings Functions ============

def get_output_format_settings(user_role):
    """Get output format settings for a user role"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM output_format_settings WHERE user_role = ?', (user_role,))
        result = cursor.fetchone()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def set_output_format_settings(user_role, file_format=None, date_format=None, time_format=None, 
                                include_header=None, custom_header_json=None, column_order_json=None, updated_by=None):
    """Set or update output format settings for a user role"""
    updates = {}
    if file_format is not None:
        updates['file_format'] = file_format
    if date_format is not None:
        updates['date_format'] = date_format
    if time_format is not None:
        updates['time_format'] = time_format
    if include_header is not None:
        updates['include_header'] = int(bool(include_header))
    if custom_header_json is not None:
        updates['custom_header_json'] = custom_header_json
    if column_order_json is not None:
        updates['column_order_json'] = column_order_json
    
    if not updates:
        return False
    
    updates['updated_at'] = datetime.now().isoformat()
    
    conn = get_db_connection()
    try:
        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [user_role]
        conn.execute(f'UPDATE output_format_settings SET {set_clause} WHERE user_role = ?', values)
        return True
    finally:
        conn.close()

def get_all_output_format_settings():
    """Get all output format settings"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM output_format_settings ORDER BY user_role')
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

# ============ Activity Log Functions ============

def log_activity(user_email, action_type, action_description='', ip_address=None,
                 metadata_json=None, user_agent=None):
    """Log an activity. Never raises: an audit write must not fail a request."""
    try:
        conn = get_db_connection()
    except Exception as exc:
        print(f"Activity log skipped (no connection): {exc}")
        return False
    try:
        conn.execute('''
            INSERT INTO activity_logs
            (user_email, action_type, action_description, ip_address, metadata_json, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_email, action_type, action_description, ip_address, metadata_json, user_agent))
        return True
    except Exception as exc:
        print(f"Activity log failed: {exc}")
        return False
    finally:
        conn.close()

def get_activity_logs(user_email=None, action_type=None, limit=100, offset=0, ip_address=None):
    """Get activity logs with optional filters.

    ip_address matches by prefix, so a whole subnet can be pulled up by typing
    the first octets ("103.21." finds every address in that range).
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        query = 'SELECT * FROM activity_logs WHERE 1=1'
        params = []

        if user_email:
            query += ' AND user_email = ?'
            params.append(user_email)

        if action_type:
            query += ' AND action_type = ?'
            params.append(action_type)

        if ip_address:
            query += ' AND ip_address LIKE ?'
            params.append(f'{ip_address}%')

        query += ' ORDER BY timestamp DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])

        cursor.execute(query, params)
        result = cursor.fetchall()
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def get_ip_activity_summary(limit=25, days=30):
    """Per-IP rollup for the superadmin: who used it, how much, and when.

    An address used by more than one account is the signal worth seeing first,
    so the result is ordered by distinct account count before volume.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT
                ip_address,
                COUNT(*) AS event_count,
                COUNT(DISTINCT user_email) AS user_count,
                GROUP_CONCAT(DISTINCT user_email) AS users,
                MIN(timestamp) AS first_seen,
                MAX(timestamp) AS last_seen
            FROM activity_logs
            WHERE ip_address IS NOT NULL
              AND ip_address != ''
              AND ip_address != 'Unknown'
              AND timestamp >= datetime('now', ?)
            GROUP BY ip_address
            ORDER BY user_count DESC, event_count DESC
            LIMIT ?
        ''', (f'-{int(days)} days', limit))
        result = cursor.fetchall()
        cursor.close()
        return result
    except Exception as exc:
        print(f"IP summary failed: {exc}")
        return []
    finally:
        conn.close()

def prune_activity_logs(days=180):
    """Drop audit rows older than `days`. Returns how many were removed."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM activity_logs WHERE timestamp < datetime('now', ?)",
            (f'-{int(days)} days',)
        )
        removed = cursor.rowcount or 0
        cursor.close()
        return removed
    except Exception as exc:
        print(f"Activity log prune failed: {exc}")
        return 0
    finally:
        conn.close()

def get_activity_log_count(user_email=None, action_type=None, ip_address=None):
    """Get total count of activity logs. Filters match get_activity_logs()."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        query = 'SELECT COUNT(*) FROM activity_logs WHERE 1=1'
        params = []

        if user_email:
            query += ' AND user_email = ?'
            params.append(user_email)

        if action_type:
            query += ' AND action_type = ?'
            params.append(action_type)

        if ip_address:
            query += ' AND ip_address LIKE ?'
            params.append(f'{ip_address}%')

        cursor.execute(query, params)
        result = cursor.fetchone()[0]
        cursor.close()  # CRITICAL: Close cursor before returning
        return result
    finally:
        conn.close()

def delete_activity_logs(days_old=None, user_email=None):
    """Delete activity logs older than specified days or for a specific user"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        if days_old:
            cursor.execute('''
                DELETE FROM activity_logs 
                WHERE datetime(timestamp) <= datetime('now', '-' || ? || ' days')
            ''', (days_old,))
        elif user_email:
            cursor.execute('DELETE FROM activity_logs WHERE user_email = ?', (user_email,))
        else:
            # Delete all logs
            cursor.execute('DELETE FROM activity_logs')
        
        deleted_count = cursor.rowcount
        cursor.close()  # CRITICAL: Close cursor
        return deleted_count
    finally:
        conn.close()

def clear_all_activity_logs():
    """Clear all activity logs"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM activity_logs')
        deleted_count = cursor.rowcount
        cursor.close()  # CRITICAL: Close cursor
        return deleted_count
    finally:
        conn.close()

# ============ App-Specific Output Format Functions ============

def get_app_output_format(app_name):
    """Get output format settings for a specific app (app1, app2, app3)"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM app_output_formats WHERE app_name = ?', (app_name,))
        result = cursor.fetchone()
        cursor.close()
        return result
    finally:
        conn.close()

def set_app_output_format(app_name, file_format=None, date_format=None, time_format=None, 
                          include_header=None, header_title=None, header_subtitle=None,
                          include_timestamp=None, include_user_info=None, custom_columns_json=None):
    """Set or update output format settings for a specific app"""
    updates = {}
    if file_format is not None:
        updates['file_format'] = file_format
    if date_format is not None:
        updates['date_format'] = date_format
    if time_format is not None:
        updates['time_format'] = time_format
    if include_header is not None:
        updates['include_header'] = int(bool(include_header))
    if header_title is not None:
        updates['header_title'] = header_title
    if header_subtitle is not None:
        updates['header_subtitle'] = header_subtitle
    if include_timestamp is not None:
        updates['include_timestamp'] = int(bool(include_timestamp))
    if include_user_info is not None:
        updates['include_user_info'] = int(bool(include_user_info))
    if custom_columns_json is not None:
        updates['custom_columns_json'] = custom_columns_json
    
    if not updates:
        return False
    
    updates['updated_at'] = datetime.now().isoformat()
    
    conn = get_db_connection()
    try:
        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [app_name]
        conn.execute(f'UPDATE app_output_formats SET {set_clause} WHERE app_name = ?', values)
        return True
    finally:
        conn.close()

def get_all_app_output_formats():
    """Get all app-specific output format settings"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM app_output_formats ORDER BY app_name')
        result = cursor.fetchall()
        cursor.close()
        return result
    finally:
        conn.close()
