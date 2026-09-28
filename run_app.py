"""
Compatibility runner for Python 3.14 where `pkgutil.get_loader` was removed.
This script adds a small shim for `pkgutil.get_loader` then imports and runs
the Flask app from `comprehensive_app.py`.

Usage: python3 run_app.py
"""
import pkgutil
import importlib.util
import os

# Provide a compatibility shim for pkgutil.get_loader if missing (Python 3.14)
if not hasattr(pkgutil, 'get_loader'):
    def _get_loader(name):
        spec = importlib.util.find_spec(name)
        if spec is None:
            return None
        return getattr(spec, 'loader', None)
    pkgutil.get_loader = _get_loader

# Now import the application and run it
from comprehensive_app import app, init_db

if __name__ == '__main__':
    try:
        init_db()
    except Exception:
        # initialization errors are non-fatal for local testing
        pass

    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=debug_mode)
