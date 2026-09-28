# This file contains the WSGI configuration required to serve up your
# web application on PythonAnywhere
# 
# IMPORTANT: Update the path below to match your PythonAnywhere username and project directory
# Example: '/home/yourusername/Attendancify'

import sys
import os

# Dynamically determine the project directory (the directory containing this wsgi.py)
path = '/home/AttendanceIQ/ZoomAttendancify'
if path not in sys.path:
    sys.path.append(path)

# Change working directory to the project directory
os.chdir(path)

# Set environment variables for production
os.environ['FLASK_ENV'] = 'production'
os.environ['FLASK_DEBUG'] = 'False'
# IMPORTANT: Set your secret key in PythonAnywhere's Web tab → Environment variables
# Or uncomment and set it here (less secure):
# os.environ['FLASK_SECRET_KEY'] = 'your-secret-key-here'

# Import flask app but need to call it "application" for WSGI to work
try:
    from comprehensive_app import app as application
except Exception:
    import traceback, os
    error_path = os.path.join(path, 'wsgi_error.log')
    with open(error_path, 'a') as f:
        f.write('Application import error:\n' + traceback.format_exc() + '\n')
    raise

# Initialize database on first load
try:
    from database import init_db
    init_db()
except Exception:
    import traceback, os
    error_path = os.path.join(path, 'wsgi_error.log')
    with open(error_path, 'a') as f:
        f.write('Database initialization error:\n' + traceback.format_exc() + '\n')
    # Continue without raising to avoid WSGI crash
    pass