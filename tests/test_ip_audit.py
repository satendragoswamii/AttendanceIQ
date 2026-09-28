"""Tests for the IP audit trail.

Covers the three things that make it trustworthy: the address is resolved
correctly from behind a proxy, every state-changing request is recorded, and the
superadmin can pull a whole address back out again.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402

os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret-key')


class AuditTrailTests(unittest.TestCase):
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
        self.pw_hash = hashlib.sha256(b'pw').hexdigest()
        database.create_user('auditor@example.com', self.pw_hash, 'superadmin',
                             must_change_password=False)
        database.create_user('worker@example.com', self.pw_hash, 'user',
                             must_change_password=False)
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _login(self, email='worker@example.com', role='user'):
        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = email
            flask_session['role'] = role
            flask_session['logged_in'] = True
            flask_session['_csrf_token'] = 'test-token'
            flask_session['last_activity'] = datetime.now().isoformat()

    def _logs(self):
        return [dict(row) for row in database.get_activity_logs(limit=200)]

    # ---------------- address resolution ----------------

    def test_forwarded_header_wins_over_proxy_address(self):
        """On PythonAnywhere remote_addr is the proxy; the client is in XFF."""
        with self.app_module.app.test_request_context(
            '/', headers={'X-Forwarded-For': '203.0.113.9, 10.0.0.1'},
            environ_base={'REMOTE_ADDR': '10.0.0.1'}
        ):
            self.assertEqual(self.app_module.get_client_ip(), '203.0.113.9')

    def test_falls_back_to_remote_addr_without_a_proxy(self):
        with self.app_module.app.test_request_context(
            '/', environ_base={'REMOTE_ADDR': '198.51.100.7'}
        ):
            self.assertEqual(self.app_module.get_client_ip(), '198.51.100.7')

    def test_only_the_first_forwarded_entry_is_trusted(self):
        """Later entries are client-supplied and must not be believed."""
        with self.app_module.app.test_request_context(
            '/', headers={'X-Forwarded-For': '203.0.113.9, 1.2.3.4, 5.6.7.8'}
        ):
            self.assertEqual(self.app_module.get_client_ip(), '203.0.113.9')

    # ---------------- automatic capture ----------------

    def test_state_changing_request_is_recorded_with_its_ip(self):
        self._login()
        self.client.post(
            '/summary/process',
            data={'_csrf_token': 'test-token'},
            content_type='multipart/form-data',
            headers={'X-Forwarded-For': '203.0.113.55'}
        )
        entries = [row for row in self._logs() if row['ip_address'] == '203.0.113.55']
        self.assertTrue(entries, 'the POST should have produced an audit row')
        self.assertEqual(entries[0]['user_email'], 'worker@example.com')

    def test_plain_page_view_is_not_recorded(self):
        """GETs that change nothing would bury the trail in noise."""
        self._login()
        before = len(self._logs())
        self.client.get('/summary', headers={'X-Forwarded-For': '203.0.113.60'})
        self.assertEqual(len(self._logs()), before)

    def test_download_is_recorded_even_though_it_is_a_get(self):
        """Taking data out is what an investigation needs to reconstruct."""
        self._login()
        self.client.get('/admin/processed_files/download/999999',
                        headers={'X-Forwarded-For': '203.0.113.61'})
        entries = [row for row in self._logs() if row['ip_address'] == '203.0.113.61']
        self.assertTrue(entries, 'a download attempt should be recorded')

    def test_failed_login_is_recorded_against_the_attempted_account(self):
        # A real sign-in form carries a token; without one the request is
        # rejected before it reaches the credential check.
        with self.client.session_transaction() as flask_session:
            flask_session['_csrf_token'] = 'test-token'
        self.client.post('/login', data={
            '_csrf_token': 'test-token',
            'email': 'worker@example.com',
            'password': 'wrong-password'
        }, headers={'X-Forwarded-For': '198.51.100.66'})
        entries = [row for row in self._logs() if row['action_type'] == 'login_failed']
        self.assertTrue(entries, 'a failed sign-in should be recorded')
        self.assertEqual(entries[0]['ip_address'], '198.51.100.66')
        self.assertIn('worker@example.com', entries[0]['user_email'])

    def test_user_agent_is_captured(self):
        self._login()
        self.client.post(
            '/summary/process',
            data={'_csrf_token': 'test-token'},
            content_type='multipart/form-data',
            headers={'X-Forwarded-For': '203.0.113.70', 'User-Agent': 'TestBrowser/9.9'}
        )
        entries = [row for row in self._logs() if row['ip_address'] == '203.0.113.70']
        self.assertTrue(entries)
        self.assertEqual(entries[0]['user_agent'], 'TestBrowser/9.9')

    def test_one_request_produces_one_row(self):
        """A view that logs explicitly must not also be logged by the hook."""
        self._login()
        self.client.post(
            '/summary/process',
            data={'_csrf_token': 'test-token'},
            content_type='multipart/form-data',
            headers={'X-Forwarded-For': '203.0.113.80'}
        )
        entries = [row for row in self._logs() if row['ip_address'] == '203.0.113.80']
        self.assertEqual(len(entries), 1)

    # ---------------- superadmin visibility ----------------

    def test_logs_can_be_filtered_to_one_address(self):
        database.log_activity('a@x.com', 'test', 'from A', '203.0.113.1')
        database.log_activity('b@x.com', 'test', 'from B', '198.51.100.2')
        found = [dict(r) for r in database.get_activity_logs(ip_address='203.0.113.1')]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['user_email'], 'a@x.com')

    def test_partial_address_matches_a_whole_range(self):
        database.log_activity('a@x.com', 'test', '', '203.0.113.5')
        database.log_activity('b@x.com', 'test', '', '203.0.113.99')
        database.log_activity('c@x.com', 'test', '', '198.51.100.1')
        found = database.get_activity_logs(ip_address='203.0.113.')
        self.assertEqual(len(found), 2)

    def test_count_honours_the_same_ip_filter(self):
        database.log_activity('a@x.com', 'test', '', '203.0.113.5')
        database.log_activity('b@x.com', 'test', '', '198.51.100.1')
        self.assertEqual(database.get_activity_log_count(ip_address='203.0.113.5'), 1)

    def test_shared_address_surfaces_in_the_summary(self):
        """Two accounts on one address is the case worth spotting."""
        database.log_activity('a@x.com', 'test', '', '203.0.113.200')
        database.log_activity('b@x.com', 'test', '', '203.0.113.200')
        database.log_activity('c@x.com', 'test', '', '198.51.100.9')
        summary = [dict(r) for r in database.get_ip_activity_summary()]
        top = summary[0]
        self.assertEqual(top['ip_address'], '203.0.113.200')
        self.assertEqual(top['user_count'], 2)
        self.assertEqual(top['event_count'], 2)

    def test_summary_ignores_rows_without_an_address(self):
        database.log_activity('a@x.com', 'test', '', None)
        database.log_activity('b@x.com', 'test', '', 'Unknown')
        self.assertEqual(len(database.get_ip_activity_summary()), 0)

    def test_activity_page_renders_for_superadmin(self):
        database.log_activity('worker@example.com', 'login', 'test entry', '203.0.113.44')
        self._login(email='auditor@example.com', role='superadmin')
        response = self.client.get('/admin/activity_logs')
        self.assertEqual(response.status_code, 200)
        body = response.data.decode('utf-8', 'replace')
        self.assertIn('203.0.113.44', body)
        self.assertIn('Addresses seen in the last 30 days', body)

    def test_activity_page_is_closed_to_regular_users(self):
        self._login()
        response = self.client.get('/admin/activity_logs')
        self.assertIn(response.status_code, (301, 302))

    # ---------------- retention ----------------

    def test_prune_drops_only_old_rows(self):
        database.log_activity('a@x.com', 'test', 'recent', '203.0.113.1')
        conn = database.get_db_connection()
        try:
            conn.execute(
                "INSERT INTO activity_logs (user_email, action_type, ip_address, timestamp) "
                "VALUES ('old@x.com', 'test', '203.0.113.2', datetime('now', '-400 days'))"
            )
        finally:
            conn.close()
        self.assertEqual(database.prune_activity_logs(days=365), 1)
        remaining = [dict(r)['user_email'] for r in database.get_activity_logs()]
        self.assertEqual(remaining, ['a@x.com'])

    def test_logging_never_raises(self):
        """An audit write must not be able to fail a user's request."""
        original = database.DATABASE
        database.DATABASE = '/nonexistent-directory/nope.db'
        try:
            self.assertFalse(database.log_activity('a@x.com', 'test', '', '1.2.3.4'))
        finally:
            database.DATABASE = original


if __name__ == '__main__':
    unittest.main(verbosity=2)
