"""Who can see which processed files, and how they are grouped by date.

The privacy rule under test: nobody sees another account's output until it is
explicitly shared, and a superadmin sees only their own until they ask for
someone else's with the owner picker.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402

os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret-key')


class ProcessedFilesVisibilityTests(unittest.TestCase):
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
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        database.DATABASE = self.db_path
        database.init_db()

        import hashlib
        pw = hashlib.sha256(b'pw').hexdigest()
        for email, role in (('alice@example.com', 'user'),
                            ('bob@example.com', 'user'),
                            ('boss@example.com', 'superadmin')):
            database.create_user(email, pw, role, must_change_password=False)

        self.dir = tempfile.mkdtemp()
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _make_file(self, name, owner, created_at=None):
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as handle:
            handle.write(b'data')
        database.register_processed_file(
            file_path=path, file_name=name,
            user_email=owner, processing_type='summary'
        )
        file_id = None
        for record in database.get_processed_files():
            record = dict(record)
            if record['file_name'] == name:
                file_id = record['id']
                break
        if created_at:
            conn = database.get_db_connection()
            try:
                conn.execute('UPDATE processed_files SET created_at = ? WHERE id = ?',
                             (created_at, file_id))
            finally:
                conn.close()
        return file_id

    def _login(self, email, role='user'):
        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = email
            flask_session['role'] = role
            flask_session['logged_in'] = True
            flask_session['last_activity'] = datetime.now().isoformat()

    def _page(self, query=''):
        response = self.client.get('/admin/processed_files' + query)
        self.assertEqual(response.status_code, 200)
        return response.data.decode('utf-8', 'replace')

    # ---------------- privacy ----------------

    def test_a_user_sees_only_their_own_files(self):
        self._make_file('alice_report.xlsx', 'alice@example.com')
        self._make_file('bob_report.xlsx', 'bob@example.com')
        self._login('alice@example.com')
        body = self._page()
        self.assertIn('alice_report.xlsx', body)
        self.assertNotIn('bob_report.xlsx', body)

    def test_a_shared_file_becomes_visible(self):
        self._make_file('alice_report.xlsx', 'alice@example.com')
        bob_file = self._make_file('bob_report.xlsx', 'bob@example.com')
        database.grant_file_access(bob_file, "alice@example.com", "bob@example.com")
        self._login('alice@example.com')
        body = self._page()
        self.assertIn('bob_report.xlsx', body)

    def test_a_user_cannot_use_the_owner_parameter_to_peek(self):
        """The picker is a superadmin control and must be inert for others."""
        self._make_file('bob_report.xlsx', 'bob@example.com')
        self._login('alice@example.com')
        self.assertNotIn('bob_report.xlsx', self._page('?owner=all'))
        self.assertNotIn('bob_report.xlsx', self._page('?owner=bob@example.com'))

    # ---------------- superadmin scoping ----------------

    def test_superadmin_sees_only_their_own_by_default(self):
        self._make_file('boss_report.xlsx', 'boss@example.com')
        self._make_file('alice_report.xlsx', 'alice@example.com')
        self._login('boss@example.com', 'superadmin')
        body = self._page()
        self.assertIn('boss_report.xlsx', body)
        self.assertNotIn('alice_report.xlsx', body)

    def test_superadmin_can_ask_for_everyone(self):
        self._make_file('boss_report.xlsx', 'boss@example.com')
        self._make_file('alice_report.xlsx', 'alice@example.com')
        self._make_file('bob_report.xlsx', 'bob@example.com')
        self._login('boss@example.com', 'superadmin')
        body = self._page('?owner=all')
        for name in ('boss_report.xlsx', 'alice_report.xlsx', 'bob_report.xlsx'):
            self.assertIn(name, body)

    def test_superadmin_can_pick_one_user(self):
        self._make_file('alice_report.xlsx', 'alice@example.com')
        self._make_file('bob_report.xlsx', 'bob@example.com')
        self._login('boss@example.com', 'superadmin')
        body = self._page('?owner=alice@example.com')
        self.assertIn('alice_report.xlsx', body)
        self.assertNotIn('bob_report.xlsx', body)

    def test_owner_picker_is_shown_only_to_superadmin(self):
        self._make_file('alice_report.xlsx', 'alice@example.com')
        self._login('boss@example.com', 'superadmin')
        self.assertIn('Files of:', self._page())
        self._login('alice@example.com')
        self.assertNotIn('Files of:', self._page())

    # ---------------- date grouping ----------------

    def test_files_are_grouped_under_their_date(self):
        today = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        older = (datetime.now() - timedelta(days=9)).strftime('%Y-%m-%d %H:%M:%S')
        self._make_file('today_a.xlsx', 'alice@example.com', today)
        self._make_file('today_b.xlsx', 'alice@example.com', today)
        self._make_file('yesterday.xlsx', 'alice@example.com', yesterday)
        self._make_file('older.xlsx', 'alice@example.com', older)

        self._login('alice@example.com')
        body = self._page()
        self.assertIn('Today', body)
        self.assertIn('Yesterday', body)
        self.assertIn('2 files', body)           # the two from today
        # One onclick per header row - counts markup, not the JS that also
        # mentions the class name.
        self.assertEqual(body.count('onclick="toggleDateGroup('), 3)   # three distinct days

    def test_the_newest_day_starts_open_and_the_rest_collapsed(self):
        today = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        self._make_file('today.xlsx', 'alice@example.com', today)
        self._make_file('yesterday.xlsx', 'alice@example.com', yesterday)

        self._login('alice@example.com')
        body = self._page()
        self.assertEqual(body.count('onclick="toggleDateGroup('), 2)
        # Of the two header rows, exactly one carries the collapsed class.
        self.assertEqual(body.count('date-group-header is-collapsed'), 1)
        # And it is not the first one: today's files render without display:none.
        self.assertIn('data-group="g0"', body)
        first_header = body.index('<tr class="date-group-header')
        self.assertNotIn('is-collapsed', body[first_header:first_header + 40])

    def test_grouping_survives_a_missing_timestamp(self):
        file_id = self._make_file('undated.xlsx', 'alice@example.com')
        conn = database.get_db_connection()
        try:
            conn.execute('UPDATE processed_files SET created_at = NULL WHERE id = ?', (file_id,))
        finally:
            conn.close()
        self._login('alice@example.com')
        body = self._page()
        self.assertIn('undated.xlsx', body)
        self.assertIn('Undated', body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
