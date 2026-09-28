"""Unit tests for the Configure Attendance Sessions "Preview" feature.

The /preview_attendance route reuses process_sessions_for_file() -- the exact same
function /process_attendance calls to generate the real report -- so duplicate
join/leave rows are merged and present/absent status computed identically. These tests
confirm: the preview reflects duplicate-merging correctly, no output file/DB row is
ever created by a preview, and the real /process_attendance route still works after
factoring the shared session-parsing helper out of it.

Runs against a temporary SQLite file so it never touches the real attendancify.db, and
against temp files under the app's own TEMP_DIR so they're cleaned up afterward.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database

os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret-key')


class AttendancePreviewTests(unittest.TestCase):
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

        import hashlib
        pw_hash = hashlib.sha256(b'pw').hexdigest()
        database.create_user('u@example.com', pw_hash, 'user', must_change_password=False)

        self.client = app.app.test_client()
        self._csv_paths = []

    def tearDown(self):
        # Clean up any output file /process_attendance registered in TEMP_DIR before
        # dropping the temp database that tracks it.
        for f in database.get_processed_files():
            if os.path.exists(f['file_path']):
                os.remove(f['file_path'])
        os.remove(self._tmp_path)
        for path in self._csv_paths:
            if os.path.exists(path):
                os.remove(path)

    def _write_raw_zoom_csv(self, name, lines):
        path = os.path.join(app.TEMP_DIR, name)
        with open(path, 'w') as f:
            f.write('Meeting Title,Foo\nMeeting ID,123\nMeeting Time,2026-01-01\n')
            f.write('Name (Original Name),Email,Join Time,Leave Time,Duration (minutes)\n')
            f.writelines(lines)
        self._csv_paths.append(path)
        return path

    def _login(self, raw_csv_path, file_name):
        with self.client.session_transaction() as sess:
            sess['user_id'] = 'u@example.com'
            sess['role'] = 'user'
            sess['logged_in'] = True
            sess['mode'] = 'single'
            sess['file_paths'] = [raw_csv_path]
            sess['file_names'] = [file_name]

    def test_preview_merges_duplicate_join_leave_rows(self):
        path = self._write_raw_zoom_csv('t1_zoom.csv', [
            'Alice Adams,alice@x.com,2026-01-01 10:00:00,2026-01-01 10:30:00,30\n',
            'Alice Adams,alice@x.com,2026-01-01 10:35:00,2026-01-01 11:05:00,30\n',
        ])
        self._login(path, 't1_zoom.csv')

        form = {
            'session_row_count': '1', 'session_order': '0', 'date_mode': 'per_session',
            'grace_period_enabled': '1', 'grace_period_value': '50', 'grace_period_unit': 'seconds',
            'file_name_0': 't1_zoom.csv',
            'start_time_0': '2026-01-01T09:00', 'end_time_0': '2026-01-01T12:00',
            'time_required_0': '30',
        }
        resp = self.client.post('/preview_attendance', data=form)
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        rows = payload['files'][0]['attendance']['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['total_duration'], '60')  # 30+30 merged, disjoint

    def test_preview_creates_no_file_or_db_row(self):
        path = self._write_raw_zoom_csv('t2_zoom.csv', [
            'Bob Brown,bob@x.com,2026-01-01 10:00:00,2026-01-01 10:05:00,5\n',
        ])
        self._login(path, 't2_zoom.csv')

        form = {
            'session_row_count': '1', 'session_order': '0', 'date_mode': 'per_session',
            'grace_period_enabled': '1', 'grace_period_value': '50', 'grace_period_unit': 'seconds',
            'file_name_0': 't2_zoom.csv',
            'start_time_0': '2026-01-01T09:00', 'end_time_0': '2026-01-01T12:00',
            'time_required_0': '65',
        }
        resp = self.client.post('/preview_attendance', data=form)
        self.assertTrue(resp.get_json()['success'])
        self.assertEqual(database.get_processed_files(), [])

    def test_preview_missing_session_config_returns_error_not_crash(self):
        path = self._write_raw_zoom_csv('t3_zoom.csv', [
            'Bob Brown,bob@x.com,2026-01-01 10:00:00,2026-01-01 10:05:00,5\n',
        ])
        self._login(path, 't3_zoom.csv')
        resp = self.client.post('/preview_attendance', data={'session_row_count': '0'})
        self.assertEqual(resp.status_code, 400)
        payload = resp.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('session', payload['message'].lower())

    def test_process_attendance_still_works_after_shared_helper_refactor(self):
        path = self._write_raw_zoom_csv('t4_zoom.csv', [
            'Alice Adams,alice@x.com,2026-01-01 10:00:00,2026-01-01 11:10:00,70\n',
        ])
        self._login(path, 't4_zoom.csv')

        form = {
            'session_row_count': '1', 'session_order': '0', 'date_mode': 'per_session',
            'grace_period_enabled': '1', 'grace_period_value': '50', 'grace_period_unit': 'seconds',
            'file_name_0': 't4_zoom.csv',
            'start_time_0': '2026-01-01T09:00', 'end_time_0': '2026-01-01T12:00',
            'time_required_0': '65',
        }
        resp = self.client.post('/process_attendance', data=form, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('processed_files', resp.headers.get('Location', ''))
        output_files = database.get_processed_files()
        self.assertEqual(len(output_files), 1)

    def test_edited_preview_values_flow_into_the_real_generated_file(self):
        """The core new capability: correcting a name or flipping a status in the
        Preview grid, then clicking Process Attendance, must produce a generated file
        reflecting the edit -- not a fresh recomputation from the raw log."""
        import json
        import pandas as pd

        path = self._write_raw_zoom_csv('t5_zoom.csv', [
            # 5-minute session well below the 65-min threshold -> computes Absent
            'Alice Adamz,alice@x.com,2026-01-01 10:00:00,2026-01-01 10:05:00,5\n',
        ])
        self._login(path, 't5_zoom.csv')

        form = {
            'session_row_count': '1', 'session_order': '0', 'date_mode': 'per_session',
            'grace_period_enabled': '1', 'grace_period_value': '50', 'grace_period_unit': 'seconds',
            'file_name_0': 't5_zoom.csv',
            'start_time_0': '2026-01-01T09:00', 'end_time_0': '2026-01-01T12:00',
            'time_required_0': '65',
        }

        preview_resp = self.client.post('/preview_attendance', data=form)
        file_data = preview_resp.get_json()['files'][0]
        attendance = file_data['attendance']
        name_key = attendance['name_key']
        session_col_key = next(
            c for c in attendance['columns']
            if c not in ('name', 'email', 'join_time', 'leave_time',
                         'total_duration', 'shortfall_reason')
        )
        self.assertEqual(attendance['rows'][0][session_col_key], 'A')  # computed absent

        # Simulate the browser-side edit: fix the garbled name, flip status to Present
        attendance['rows'][0][name_key] = 'Alice Adams'
        attendance['rows'][0][session_col_key] = 'P'

        form_with_override = dict(form)
        form_with_override['preview_overrides'] = json.dumps({
            file_data['file_name']: {
                'columns': attendance['columns'],
                'column_labels': attendance['column_labels'],
                'rows': attendance['rows'],
            }
        })

        resp = self.client.post('/process_attendance', data=form_with_override, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

        output_files = database.get_processed_files()
        self.assertEqual(len(output_files), 1)
        df = pd.read_excel(output_files[0]['file_path'], sheet_name='Attendance')
        row = df.iloc[0]
        self.assertEqual(row['Name'], 'Alice Adams')
        session_label_col = next(c for c in df.columns if '2026' in str(c))
        self.assertEqual(row[session_label_col], 'P')

    def test_downloaded_column_order_is_not_scrambled_by_json_round_trip(self):
        """Regression: Flask's jsonify() alphabetically re-sorts dict keys by default, so
        overrides['column_labels'] can't be trusted for ORDER once it's round-tripped
        through the browser -- even though its values are still correct. Reconstructing
        output_records by iterating that dict (instead of the JSON-array-safe "columns"
        list) silently produced alphabetical column order in downloaded files instead of
        Name/Email/Join Time/.../Total Duration/Shortfall Reason."""
        import json
        import pandas as pd

        path = self._write_raw_zoom_csv('t10_zoom.csv', [
            'Alice Adams,alice@x.com,2026-07-19 10:00:00,2026-07-19 10:05:00,5\n',
        ])
        self._login(path, 't10_zoom.csv')

        form = {
            'session_row_count': '1', 'session_order': '0', 'date_mode': 'per_session',
            'grace_period_enabled': '1', 'grace_period_value': '50', 'grace_period_unit': 'seconds',
            'file_name_0': 't10_zoom.csv',
            'start_time_0': '2026-07-19T09:00', 'end_time_0': '2026-07-19T12:00',
            'time_required_0': '65',
        }

        # This goes through the real HTTP JSON response (get_json() parses jsonify()'s
        # actual output), so attendance['column_labels'] here really is alphabetized --
        # exactly what a browser would receive.
        preview_resp = self.client.post('/preview_attendance', data=form)
        file_data = preview_resp.get_json()['files'][0]
        attendance = file_data['attendance']
        self.assertNotEqual(list(attendance['column_labels'].keys()), attendance['columns'],
                             "this test assumes jsonify() reorders the dict -- if this ever "
                             "stops being true the scenario it guards against no longer applies")

        # Simulate the browser downloading without editing anything -- just previewed, then Download
        form_with_override = dict(form)
        form_with_override['preview_overrides'] = json.dumps({
            file_data['file_name']: {
                'columns': attendance['columns'],
                'column_labels': attendance['column_labels'],
                'rows': attendance['rows'],
            }
        })
        resp = self.client.post('/process_attendance', data=form_with_override, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

        output_files = database.get_processed_files()
        df = pd.read_excel(output_files[0]['file_path'], sheet_name='Attendance')
        session_label_col = next(c for c in df.columns if '2026' in str(c))
        self.assertEqual(
            list(df.columns),
            ['Name', 'Email', 'Join Time', 'Leave Time', session_label_col, 'Total Duration', 'Shortfall Reason']
        )

    def test_no_preview_means_process_attendance_computes_fresh_as_before(self):
        """A user who never clicks Preview gets exactly the old behavior: the real
        computation from the raw log, with no override involved at all."""
        import pandas as pd

        path = self._write_raw_zoom_csv('t6_zoom.csv', [
            'Carl Chen,carl@x.com,2026-01-01 10:00:00,2026-01-01 10:05:00,5\n',
        ])
        self._login(path, 't6_zoom.csv')

        form = {
            'session_row_count': '1', 'session_order': '0', 'date_mode': 'per_session',
            'grace_period_enabled': '1', 'grace_period_value': '50', 'grace_period_unit': 'seconds',
            'file_name_0': 't6_zoom.csv',
            'start_time_0': '2026-01-01T09:00', 'end_time_0': '2026-01-01T12:00',
            'time_required_0': '65',
            'preview_overrides': '',  # matches what the JS sends when nothing was previewed
        }
        resp = self.client.post('/process_attendance', data=form, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

        output_files = database.get_processed_files()
        df = pd.read_excel(output_files[0]['file_path'], sheet_name='Attendance')
        row = df.iloc[0]
        self.assertEqual(row['Name'], 'Carl Chen')
        session_label_col = next(c for c in df.columns if '2026' in str(c))
        self.assertEqual(row[session_label_col], 'A')  # freshly computed, not overridden

    def test_excluded_participant_does_not_appear_in_attendance_view(self):
        path = self._write_raw_zoom_csv('t7_zoom.csv', [
            'Alice Adams,alice@x.com,2026-01-01 09:00:00,2026-01-01 12:00:00,180\n',
            'Carol Clark,carol@x.com,2026-01-01 09:00:00,2026-01-01 12:00:00,180\n',
        ])
        database.add_excluded_participant('carol@x.com', 'email', 'tester@example.com')
        self._login(path, 't7_zoom.csv')

        form = {
            'session_row_count': '1', 'session_order': '0', 'date_mode': 'per_session',
            'grace_period_enabled': '1', 'grace_period_value': '50', 'grace_period_unit': 'seconds',
            'file_name_0': 't7_zoom.csv',
            'start_time_0': '2026-01-01T09:00', 'end_time_0': '2026-01-01T12:00',
            'time_required_0': '65',
        }
        resp = self.client.post('/preview_attendance', data=form)
        attendance = resp.get_json()['files'][0]['attendance']
        names = [r[attendance['name_key']] for r in attendance['rows']]
        self.assertNotIn('Carol Clark', names)
        self.assertIn('Alice Adams', names)

    def test_shortfall_reason_column_present_even_when_first_row_has_none(self):
        """Regression: columns used to be derived from only the first record's keys,
        so "Shortfall Reason" (which only exists on records with an absence) would
        silently vanish -- along with its data on every row -- whenever the first
        participant happened to be fully present."""
        path = self._write_raw_zoom_csv('t8_zoom.csv', [
            'Alice Adams,alice@x.com,2026-01-01 09:00:00,2026-01-01 12:00:00,180\n',  # present, no shortfall key
            'Bob Brown,bob@x.com,2026-01-01 10:00:00,2026-01-01 10:05:00,5\n',         # absent, has shortfall key
        ])
        self._login(path, 't8_zoom.csv')

        form = {
            'session_row_count': '1', 'session_order': '0', 'date_mode': 'per_session',
            'grace_period_enabled': '1', 'grace_period_value': '50', 'grace_period_unit': 'seconds',
            'file_name_0': 't8_zoom.csv',
            'start_time_0': '2026-01-01T09:00', 'end_time_0': '2026-01-01T12:00',
            'time_required_0': '65',
        }
        resp = self.client.post('/preview_attendance', data=form)
        attendance = resp.get_json()['files'][0]['attendance']
        self.assertIn('shortfall_reason', attendance['columns'])
        bob_row = next(r for r in attendance['rows'] if r[attendance['name_key']] == 'Bob Brown')
        self.assertTrue(bob_row['shortfall_reason'])

    def test_sheet1_raw_data_present_and_unfiltered_by_exclusions(self):
        path = self._write_raw_zoom_csv('t9_zoom.csv', [
            'Alice Adams,alice@x.com,2026-01-01 09:00:00,2026-01-01 12:00:00,180\n',
            'Carol Clark,carol@x.com,2026-01-01 09:00:00,2026-01-01 12:00:00,180\n',
        ])
        database.add_excluded_participant('carol@x.com', 'email', 'tester@example.com')
        self._login(path, 't9_zoom.csv')

        form = {
            'session_row_count': '1', 'session_order': '0', 'date_mode': 'per_session',
            'grace_period_enabled': '1', 'grace_period_value': '50', 'grace_period_unit': 'seconds',
            'file_name_0': 't9_zoom.csv',
            'start_time_0': '2026-01-01T09:00', 'end_time_0': '2026-01-01T12:00',
            'time_required_0': '65',
        }
        resp = self.client.post('/preview_attendance', data=form)
        sheet1_rows = resp.get_json()['files'][0]['sheet1']['rows']
        flat = [str(v) for row in sheet1_rows for v in row]
        self.assertTrue(any('Alice Adams' in v for v in flat))
        # Sheet1 mirrors the untouched raw file -- exclusions never apply to it
        self.assertTrue(any('Carol Clark' in v for v in flat))


if __name__ == '__main__':
    unittest.main()
