"""Unit tests for the redesigned Output Format Settings (rename/hide columns per app).

Covers: get_output_column_config()'s parsing (new {renames, hidden} shape, the legacy
flat-dict-only shape, missing/malformed JSON), the /admin/app_formats/update route
building that JSON from per-column form fields, and the rename+hide actually applying
to a real generated file for the Zoom Attendance Generator (app1). Also covers the
excluded-participants name-matching fix (it used to only recognize the old
"Name (Original Name)" key and silently stopped matching by name after the output
format was unified to a plain "Name" column).

Runs against a temporary SQLite file so it never touches the real attendancify.db.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database

os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret-key')


class OutputFormatSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, import_tmp_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        database.DATABASE = import_tmp_path
        database.init_db()
        global app
        import comprehensive_app as app
        os.remove(import_tmp_path)

    def setUp(self):
        fd, self._tmp_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        database.DATABASE = self._tmp_path
        database.init_db()
        database.ensure_superadmin_exists()  # @superadmin_required re-checks the DB, not just the session

        import hashlib
        pw_hash = hashlib.sha256(b'pw').hexdigest()
        database.create_user('u@example.com', pw_hash, 'user', must_change_password=False)

        self.client = app.app.test_client()
        self._csv_paths = []
        with self.client.session_transaction() as sess:
            sess['user_id'] = 'superadmin@attendancify.com'
            sess['role'] = 'superadmin'
            sess['logged_in'] = True

    def tearDown(self):
        for f in database.get_processed_files():
            if os.path.exists(f['file_path']):
                os.remove(f['file_path'])
        os.remove(self._tmp_path)
        for path in self._csv_paths:
            if os.path.exists(path):
                os.remove(path)

    def test_no_saved_config_returns_empty(self):
        renames, hidden = app.get_output_column_config('app1')
        self.assertEqual(renames, {})
        self.assertEqual(hidden, set())

    def test_legacy_flat_dict_format_is_treated_as_renames_only(self):
        import json
        database.set_app_output_format('app1', custom_columns_json=json.dumps({'Name': 'Participant'}))
        renames, hidden = app.get_output_column_config('app1')
        self.assertEqual(renames, {'Name': 'Participant'})
        self.assertEqual(hidden, set())

    def test_new_format_parses_renames_and_hidden(self):
        import json
        database.set_app_output_format('app1', custom_columns_json=json.dumps({
            'renames': {'Name': 'Participant', 'Email': 'Email Address'},
            'hidden': ['Total Duration'],
        }))
        renames, hidden = app.get_output_column_config('app1')
        self.assertEqual(renames, {'Name': 'Participant', 'Email': 'Email Address'})
        self.assertEqual(hidden, {'Total Duration'})

    def test_malformed_json_returns_empty_instead_of_raising(self):
        database.set_app_output_format('app1', custom_columns_json='{not valid json')
        renames, hidden = app.get_output_column_config('app1')
        self.assertEqual(renames, {})
        self.assertEqual(hidden, set())

    def test_update_route_builds_config_from_per_column_fields(self):
        import json
        resp = self.client.post('/admin/app_formats/update', data={
            'app_name': 'app1',
            'rename__Name': 'Participant',
            'rename__Email': '',  # blank means "keep original" -- must not be saved
            'hidden__Total Duration': '1',
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

        saved = json.loads(database.get_app_output_format('app1')['custom_columns_json'])
        self.assertEqual(saved['renames'], {'Name': 'Participant'})
        self.assertEqual(saved['hidden'], ['Total Duration'])

    def test_unknown_app_name_is_rejected(self):
        resp = self.client.post('/admin/app_formats/update', data={'app_name': 'not_a_real_app'}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(database.get_app_output_format('not_a_real_app'))

    def _write_raw_zoom_csv(self, name, lines):
        path = os.path.join(app.TEMP_DIR, name)
        with open(path, 'w') as f:
            f.write('Meeting Title,Foo\nMeeting ID,123\nMeeting Time,2026-01-01\n')
            f.write('Name (Original Name),Email,Join Time,Leave Time,Duration (minutes)\n')
            f.writelines(lines)
        self._csv_paths.append(path)
        return path

    def test_rename_and_hide_apply_to_real_generated_file(self):
        import pandas as pd

        resp = self.client.post('/admin/app_formats/update', data={
            'app_name': 'app1',
            'rename__Name': 'Participant',
            'hidden__Total Duration': '1',
        })
        self.assertEqual(resp.status_code, 302)

        path = self._write_raw_zoom_csv('settings_test_zoom.csv', [
            'Alice Adams,alice@x.com,2026-01-01 10:00:00,2026-01-01 10:30:00,30\n',
        ])
        with self.client.session_transaction() as sess:
            sess['user_id'] = 'u@example.com'
            sess['role'] = 'user'
            sess['logged_in'] = True
            sess['mode'] = 'single'
            sess['file_paths'] = [path]
            sess['file_names'] = ['settings_test_zoom.csv']

        form = {
            'session_row_count': '1', 'session_order': '0', 'date_mode': 'per_session',
            'grace_period_enabled': '1', 'grace_period_value': '50', 'grace_period_unit': 'seconds',
            'file_name_0': 'settings_test_zoom.csv',
            'start_time_0': '2026-01-01T09:00', 'end_time_0': '2026-01-01T12:00',
            'time_required_0': '65',
        }
        resp2 = self.client.post('/process_attendance', data=form, follow_redirects=False)
        self.assertEqual(resp2.status_code, 302)

        out = database.get_processed_files()[0]['file_path']
        df = pd.read_excel(out, sheet_name='Attendance')
        self.assertIn('Participant', df.columns)
        self.assertNotIn('Name', df.columns)
        self.assertNotIn('Total Duration', df.columns)

    def test_excluded_participant_still_filtered_after_column_unification(self):
        """Regression: filter_excluded_records() used to only recognize the old
        "Name (Original Name)" key. After the output format was unified to a plain
        "Name" column, name-based exclusion silently stopped working (only email-based
        exclusion still worked) -- this confirms name matching works again."""
        import pandas as pd

        database.add_excluded_participant('Carol Clark', 'name', 'tester@example.com')

        path = self._write_raw_zoom_csv('exclusion_test_zoom.csv', [
            'Alice Adams,alice@x.com,2026-01-01 09:00:00,2026-01-01 12:00:00,180\n',
            'Carol Clark,carol@x.com,2026-01-01 09:00:00,2026-01-01 12:00:00,180\n',
        ])
        with self.client.session_transaction() as sess:
            sess['user_id'] = 'u@example.com'
            sess['role'] = 'user'
            sess['logged_in'] = True
            sess['mode'] = 'single'
            sess['file_paths'] = [path]
            sess['file_names'] = ['exclusion_test_zoom.csv']

        form = {
            'session_row_count': '1', 'session_order': '0', 'date_mode': 'per_session',
            'grace_period_enabled': '1', 'grace_period_value': '50', 'grace_period_unit': 'seconds',
            'file_name_0': 'exclusion_test_zoom.csv',
            'start_time_0': '2026-01-01T09:00', 'end_time_0': '2026-01-01T12:00',
            'time_required_0': '65',
        }
        resp = self.client.post('/process_attendance', data=form, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

        out = database.get_processed_files()[0]['file_path']
        df = pd.read_excel(out, sheet_name='Attendance')
        names = list(df['Name'])
        self.assertIn('Alice Adams', names)
        self.assertNotIn('Carol Clark', names)


if __name__ == '__main__':
    unittest.main()
