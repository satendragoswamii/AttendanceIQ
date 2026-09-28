"""Tests for the Processed Files ZIP download - all files and a chosen subset."""

import io
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402

os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret-key')


class ProcessedFilesZipTests(unittest.TestCase):
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
        database.create_user('owner@example.com', pw, 'user', must_change_password=False)
        database.create_user('boss@example.com', pw, 'superadmin', must_change_password=False)

        self.dir = tempfile.mkdtemp()
        self.files = []
        for name in ('alpha.xlsx', 'beta.xlsx', 'gamma.xlsx'):
            path = os.path.join(self.dir, name)
            with open(path, 'wb') as handle:
                handle.write(b'x' * 64)
            database.register_processed_file(
                file_path=path, file_name=name,
                user_email='owner@example.com', processing_type='summary'
            )
            self.files.append(name)

        self.ids = {}
        for record in database.get_processed_files():
            record = dict(record)
            self.ids[record['file_name']] = record['id']

        self.client = self.app_module.app.test_client()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _login(self, email='owner@example.com', role='user'):
        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = email
            flask_session['role'] = role
            flask_session['logged_in'] = True
            flask_session['last_activity'] = datetime.now().isoformat()

    def _names_in_zip(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/zip')
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            return sorted(archive.namelist())

    def test_no_ids_downloads_everything(self):
        self._login()
        names = self._names_in_zip(self.client.get('/admin/processed_files/download_zip'))
        self.assertEqual(names, ['alpha.xlsx', 'beta.xlsx', 'gamma.xlsx'])

    def test_selected_ids_download_only_those_files(self):
        self._login()
        response = self.client.get(
            f"/admin/processed_files/download_zip?file_ids={self.ids['alpha.xlsx']}"
            f"&file_ids={self.ids['gamma.xlsx']}"
        )
        self.assertEqual(self._names_in_zip(response), ['alpha.xlsx', 'gamma.xlsx'])

    def test_a_single_selected_file_still_returns_a_zip(self):
        self._login()
        response = self.client.get(
            f"/admin/processed_files/download_zip?file_ids={self.ids['beta.xlsx']}"
        )
        self.assertEqual(self._names_in_zip(response), ['beta.xlsx'])

    def test_comma_separated_ids_are_accepted(self):
        self._login()
        joined = f"{self.ids['alpha.xlsx']},{self.ids['beta.xlsx']}"
        response = self.client.get(f'/admin/processed_files/download_zip?file_ids={joined}')
        self.assertEqual(self._names_in_zip(response), ['alpha.xlsx', 'beta.xlsx'])

    def test_superadmin_gets_files_owned_by_someone_else_when_asked(self):
        """Regression: this raised AttributeError on sqlite3.Row before the fix."""
        self._login(email='boss@example.com', role='superadmin')
        names = self._names_in_zip(
            self.client.get('/admin/processed_files/download_zip?owner=all'))
        self.assertEqual(len(names), 3)

    def test_superadmin_download_all_matches_the_default_page_view(self):
        """"All" must mean all of what is on screen, not everything on the system."""
        self._login(email='boss@example.com', role='superadmin')
        response = self.client.get('/admin/processed_files/download_zip')
        # The superadmin owns nothing here, so the default scope is empty.
        self.assertIn(response.status_code, (301, 302))

    def test_superadmin_can_zip_one_users_files(self):
        self._login(email='boss@example.com', role='superadmin')
        names = self._names_in_zip(self.client.get(
            '/admin/processed_files/download_zip?owner=owner@example.com'))
        self.assertEqual(names, ['alpha.xlsx', 'beta.xlsx', 'gamma.xlsx'])

    def test_superadmin_selecting_rows_still_zips_those_rows(self):
        """Ticked ids win over the owner scope - they are already an explicit ask."""
        self._login(email='boss@example.com', role='superadmin')
        response = self.client.get(
            f"/admin/processed_files/download_zip?file_ids={self.ids['beta.xlsx']}")
        self.assertEqual(self._names_in_zip(response), ['beta.xlsx'])

    def test_other_user_gets_nothing_and_is_redirected(self):
        import hashlib
        database.create_user('stranger@example.com', hashlib.sha256(b'pw').hexdigest(),
                             'user', must_change_password=False)
        self._login(email='stranger@example.com', role='user')
        response = self.client.get('/admin/processed_files/download_zip')
        self.assertIn(response.status_code, (301, 302))

    def test_unknown_ids_are_ignored_rather_than_crashing(self):
        self._login()
        response = self.client.get(
            f"/admin/processed_files/download_zip?file_ids={self.ids['alpha.xlsx']}&file_ids=999999"
        )
        self.assertEqual(self._names_in_zip(response), ['alpha.xlsx'])

    def test_missing_file_on_disk_is_skipped(self):
        os.remove(os.path.join(self.dir, 'beta.xlsx'))
        self._login()
        names = self._names_in_zip(self.client.get('/admin/processed_files/download_zip'))
        self.assertEqual(names, ['alpha.xlsx', 'gamma.xlsx'])

    def test_page_offers_exactly_one_zip_control(self):
        """Counts controls, not label text - the label also appears in the JS."""
        self._login()
        body = self.client.get('/admin/processed_files').data.decode('utf-8', 'replace')
        self.assertNotIn('downloadAllZipLink', body)          # old duplicate link, gone
        self.assertEqual(body.count('onclick="downloadSelected()"'), 1)
        self.assertEqual(body.count('id="downloadZipLabel"'), 1)
        # No anchor pointing straight at the zip route any more.
        self.assertNotIn('href="/admin/processed_files/download_zip', body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
