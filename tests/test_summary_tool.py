"""Tests for the Meeting Summary tool: cell extraction, bulk runs, and the routes."""

import os
import sys
import tempfile
import unittest
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402

os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret-key')

from summary_processing import (  # noqa: E402
    SUMMARY_COLUMNS,
    build_summary_rows,
    read_meeting_summary,
    write_summary_report,
)


def make_report(path, subject='CEO Batch 3', meeting_id=85712345678,
                end_time=datetime(2026, 5, 31, 12, 15), names=('A', 'B', 'B'),
                attendance_sheet='Attendance'):
    """A workbook shaped like write_excel() output: raw Sheet1 + Attendance."""
    raw = pd.DataFrame([
        ['Topic', 'Meeting ID', 'Email', 'Duration', 'Start Time', 'End Time'],
        [subject, meeting_id, 'host@example.com', 195, datetime(2026, 5, 31, 9, 0), end_time],
    ])
    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        raw.to_excel(writer, sheet_name='Sheet1', index=False, header=False)
        pd.DataFrame({'Name': list(names)}).to_excel(
            writer, sheet_name=attendance_sheet, index=False
        )
    return path


class TestCellExtraction(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_reads_a2_b2_f2_and_counts_names(self):
        path = make_report(os.path.join(self.dir, 'r.xlsx'))
        row = read_meeting_summary(path)
        self.assertEqual(row['Subject'], 'CEO Batch 3')
        self.assertEqual(row['Meeting ID'], '85712345678')
        self.assertEqual(row['End Time'], '2026-05-31 12:15:00')
        self.assertEqual(row['Total Names'], 3)
        self.assertEqual(row['Unique Names'], 2)
        self.assertEqual(row['Status'], 'OK')

    def test_meeting_id_does_not_become_a_float(self):
        path = make_report(os.path.join(self.dir, 'r.xlsx'), meeting_id=99988877766)
        self.assertEqual(read_meeting_summary(path)['Meeting ID'], '99988877766')

    def test_second_sheet_used_when_not_named_attendance(self):
        path = make_report(os.path.join(self.dir, 'r.xlsx'), attendance_sheet='Sheet2')
        self.assertEqual(read_meeting_summary(path)['Total Names'], 3)

    def test_renamed_name_column_still_counted(self):
        path = os.path.join(self.dir, 'renamed.xlsx')
        raw = pd.DataFrame([
            ['Topic', 'Meeting ID', 'Email', 'Duration', 'Start', 'End'],
            ['Sub', 123, 'h@x.com', 60, 'a', 'b'],
        ])
        with pd.ExcelWriter(path, engine='openpyxl') as writer:
            raw.to_excel(writer, sheet_name='Sheet1', index=False, header=False)
            pd.DataFrame({'Participant Name': ['x', 'y']}).to_excel(
                writer, sheet_name='Attendance', index=False
            )
        self.assertEqual(read_meeting_summary(path)['Total Names'], 2)

    def test_csv_gives_metadata_without_a_count(self):
        path = os.path.join(self.dir, 'log.csv')
        with open(path, 'w') as handle:
            handle.write('Topic,Meeting ID,Email,Duration,Start,End\n')
            handle.write('Weekly Sync,999,h@x.com,60,2026-06-01 10:00,2026-06-01 11:00\n')
        row = read_meeting_summary(path)
        self.assertEqual(row['Subject'], 'Weekly Sync')
        self.assertEqual(row['Meeting ID'], '999')
        self.assertEqual(row['End Time'], '2026-06-01 11:00')
        self.assertEqual(row['Total Names'], '')
        self.assertIn('CSV', row['Status'])

    def test_unreadable_file_reports_instead_of_raising(self):
        path = os.path.join(self.dir, 'broken.xlsx')
        with open(path, 'wb') as handle:
            handle.write(b'not a workbook')
        row = read_meeting_summary(path)
        self.assertNotEqual(row['Status'], 'OK')
        self.assertEqual(row['Subject'], '')

    def test_unsupported_extension_reported(self):
        path = os.path.join(self.dir, 'notes.pdf')
        with open(path, 'wb') as handle:
            handle.write(b'%PDF')
        self.assertIn('Unsupported', read_meeting_summary(path)['Status'])


class TestBulkAndReport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_one_bad_file_does_not_sink_the_batch(self):
        good = make_report(os.path.join(self.dir, 'good.xlsx'))
        bad = os.path.join(self.dir, 'bad.xlsx')
        with open(bad, 'wb') as handle:
            handle.write(b'junk')
        rows = build_summary_rows([(good, 'good.xlsx'), (bad, 'bad.xlsx')])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['Status'], 'OK')
        self.assertNotEqual(rows[1]['Status'], 'OK')

    def test_xlsx_report_has_the_expected_headers(self):
        rows = build_summary_rows([(make_report(os.path.join(self.dir, 'a.xlsx')), 'a.xlsx')])
        out = write_summary_report(rows, os.path.join(self.dir, 'out.xlsx'), 'xlsx')
        frame = pd.read_excel(out, sheet_name='Summary')
        self.assertEqual(list(frame.columns), SUMMARY_COLUMNS)
        self.assertEqual(len(frame), 1)

    def test_csv_report_has_the_expected_headers(self):
        rows = build_summary_rows([(make_report(os.path.join(self.dir, 'a.xlsx')), 'a.xlsx')])
        out = write_summary_report(rows, os.path.join(self.dir, 'out.csv'), 'csv')
        frame = pd.read_csv(out)
        self.assertEqual(list(frame.columns), SUMMARY_COLUMNS)


class TestSummaryRoutes(unittest.TestCase):
    """Drives the real Flask app: login required, bulk upload, report download.

    Runs against a temporary SQLite file so the real attendancify.db is never
    touched, following the convention in test_attendance_preview.py.
    """

    @classmethod
    def setUpClass(cls):
        handle, bootstrap_db = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        database.DATABASE = bootstrap_db
        database.init_db()
        import comprehensive_app
        cls.app_module = comprehensive_app
        comprehensive_app.app.config['TESTING'] = True
        os.remove(bootstrap_db)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        database.DATABASE = self.db_path
        database.init_db()

        import hashlib
        database.create_user(
            'route-test@example.com',
            hashlib.sha256(b'pw').hexdigest(),
            'user',
            must_change_password=False
        )
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        for record in database.get_processed_files():
            path = dict(record).get('file_path')
            if path and os.path.exists(path):
                os.remove(path)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _login(self, role='user', email='route-test@example.com'):
        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = email
            flask_session['role'] = role
            flask_session['logged_in'] = True
            flask_session['last_activity'] = datetime.now().isoformat()

    def _csrf(self):
        with self.client.session_transaction() as flask_session:
            token = 'test-token'
            flask_session['_csrf_token'] = token
        return token

    def test_requires_login(self):
        response = self.client.get('/summary')
        self.assertIn(response.status_code, (301, 302))

    def test_page_loads_for_a_regular_user(self):
        self._login()
        response = self.client.get('/summary')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Meeting Summary', response.data)

    def test_bulk_upload_returns_a_row_per_file(self):
        self._login()
        token = self._csrf()
        paths = [
            make_report(os.path.join(self.dir, 'one.xlsx'), subject='Batch One'),
            make_report(os.path.join(self.dir, 'two.xlsx'), subject='Batch Two'),
        ]
        data = {'_csrf_token': token, 'output_format': 'xlsx'}
        handles = [open(p, 'rb') for p in paths]
        try:
            data['summary_files'] = [
                (handle, os.path.basename(path)) for handle, path in zip(handles, paths)
            ]
            response = self.client.post(
                '/summary/process', data=data, content_type='multipart/form-data'
            )
        finally:
            for handle in handles:
                handle.close()

        self.assertEqual(response.status_code, 200)
        body = response.data.decode('utf-8', 'replace')
        self.assertIn('Batch One', body)
        self.assertIn('Batch Two', body)
        self.assertIn('85712345678', body)
        self.assertIn('Attendance_Summary_', body)

    def test_no_files_selected_redirects(self):
        self._login()
        token = self._csrf()
        response = self.client.post(
            '/summary/process',
            data={'_csrf_token': token, 'output_format': 'xlsx'},
            content_type='multipart/form-data'
        )
        self.assertIn(response.status_code, (301, 302))

    def test_progress_overlay_is_wired_into_the_tool_page(self):
        """The staged overlay needs its assets loaded and the form tagged."""
        self._login()
        body = self.client.get('/summary').data.decode('utf-8', 'replace')
        self.assertIn('processing-progress.css', body)
        self.assertIn('processing-progress.js', body)
        self.assertIn('data-progress="summary"', body)

    def test_dashboard_hides_recent_activity_from_a_regular_user(self):
        """The tools row goes full width for users; admins keep the activity panel."""
        database.update_user('route-test@example.com', can_view_dashboard=1)
        self._login()
        body = self.client.get('/dashboard').data.decode('utf-8', 'replace')
        self.assertNotIn('Recent Activity', body)
        self.assertIn('Meeting Summary', body)
        self.assertIn('ds-grid-1', body)
        self.assertNotIn('Total Users', body)

    def test_dashboard_keeps_recent_activity_for_an_admin(self):
        import hashlib
        database.create_user(
            'admin-test@example.com',
            hashlib.sha256(b'pw').hexdigest(),
            'admin',
            must_change_password=False
        )
        self._login(role='admin', email='admin-test@example.com')
        body = self.client.get('/dashboard').data.decode('utf-8', 'replace')
        self.assertIn('Recent Activity', body)
        self.assertIn('Meeting Summary', body)
        self.assertIn('Total Users', body)

    def test_missing_csrf_token_is_rejected(self):
        self._login()
        with self.client.session_transaction() as flask_session:
            flask_session['_csrf_token'] = 'expected'
        response = self.client.post(
            '/summary/process',
            data={'output_format': 'xlsx'},
            content_type='multipart/form-data'
        )
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
