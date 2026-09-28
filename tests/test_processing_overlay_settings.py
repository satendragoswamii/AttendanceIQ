"""Tests for the Processing Animation settings: saving, merging, and safety."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402

os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret-key')


class ProcessingOverlaySettingsTests(unittest.TestCase):
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
        database.create_user('boss@example.com', pw, 'superadmin', must_change_password=False)
        database.create_user('worker@example.com', pw, 'user', must_change_password=False)
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _login(self, email='boss@example.com', role='superadmin'):
        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = email
            flask_session['role'] = role
            flask_session['logged_in'] = True
            flask_session['_csrf_token'] = 'test-token'
            flask_session['last_activity'] = datetime.now().isoformat()

    def _config(self):
        with self.app_module.app.test_request_context('/'):
            return self.app_module.get_processing_overlay_config()

    # ---------------- defaults ----------------

    def test_defaults_apply_when_nothing_is_saved(self):
        config = self._config()
        self.assertTrue(config['enabled'])
        self.assertEqual(len(config['tools']), 6)
        self.assertEqual(config['tools']['smart-matcher']['steps'][0], 'Uploading your file(s)')

    def test_every_wired_tool_has_a_stage_set(self):
        config = self._config()
        for key, _label in self.app_module.PROCESSING_OVERLAY_TOOLS:
            self.assertIn(key, config['tools'])
            self.assertTrue(config['tools'][key]['steps'])

    # ---------------- saving ----------------

    def _post(self, data):
        payload = {'_csrf_token': 'test-token'}
        payload.update(data)
        return self.client.post('/admin/settings/processing-overlay', data=payload)

    def test_custom_stages_are_saved_and_read_back(self):
        self._login()
        response = self._post({
            'overlay_enabled': 'on',
            'overlay_show_elapsed': 'on',
            'overlay_subtitle': 'Hang tight',
            'steps__smart-matcher': 'Step one\nStep two\nStep three',
            'title__smart-matcher': 'Matching now',
        })
        self.assertIn(response.status_code, (301, 302))
        config = self._config()
        self.assertEqual(config['tools']['smart-matcher']['steps'],
                         ['Step one', 'Step two', 'Step three'])
        self.assertEqual(config['tools']['smart-matcher']['title'], 'Matching now')
        self.assertEqual(config['subtitle'], 'Hang tight')

    def test_untouched_tools_keep_their_defaults(self):
        self._login()
        self._post({'overlay_enabled': 'on', 'steps__smart-matcher': 'Only this one'})
        config = self._config()
        self.assertEqual(config['tools']['smart-matcher']['steps'], ['Only this one'])
        self.assertEqual(config['tools']['summary']['steps'],
                         self.app_module.PROCESSING_OVERLAY_DEFAULTS['summary']['steps'])

    def test_blank_and_whitespace_lines_are_dropped(self):
        self._login()
        self._post({'overlay_enabled': 'on', 'steps__summary': 'One\n\n   \nTwo\n'})
        self.assertEqual(self._config()['tools']['summary']['steps'], ['One', 'Two'])

    def test_a_tool_left_empty_keeps_its_built_in_stages(self):
        """Saving nothing must not leave a tool with an empty overlay."""
        self._login()
        self._post({'overlay_enabled': 'on', 'steps__summary': '   '})
        self.assertEqual(self._config()['tools']['summary']['steps'],
                         self.app_module.PROCESSING_OVERLAY_DEFAULTS['summary']['steps'])

    def test_step_count_is_capped(self):
        self._login()
        many = '\n'.join(f'Step {i}' for i in range(40))
        self._post({'overlay_enabled': 'on', 'steps__summary': many})
        self.assertEqual(len(self._config()['tools']['summary']['steps']),
                         self.app_module.PROCESSING_OVERLAY_MAX_STEPS)

    def test_animation_can_be_switched_off(self):
        self._login()
        self._post({'steps__summary': 'One'})          # checkbox absent = off
        self.assertFalse(self._config()['enabled'])

    def test_elapsed_timer_can_be_switched_off(self):
        self._login()
        self._post({'overlay_enabled': 'on', 'steps__summary': 'One'})
        self.assertFalse(self._config()['show_elapsed'])

    def test_reset_restores_the_defaults(self):
        self._login()
        self._post({'overlay_enabled': 'on', 'steps__summary': 'Custom only'})
        self.assertEqual(self._config()['tools']['summary']['steps'], ['Custom only'])
        self.client.post('/admin/settings/processing-overlay/reset',
                         data={'_csrf_token': 'test-token'})
        self.assertEqual(self._config()['tools']['summary']['steps'],
                         self.app_module.PROCESSING_OVERLAY_DEFAULTS['summary']['steps'])

    # ---------------- robustness and access ----------------

    def test_corrupt_saved_value_falls_back_to_defaults(self):
        database.set_app_setting(self.app_module.PROCESSING_OVERLAY_SETTING_KEY, '{not json')
        config = self._config()
        self.assertTrue(config['enabled'])
        self.assertEqual(config['tools']['summary']['steps'],
                         self.app_module.PROCESSING_OVERLAY_DEFAULTS['summary']['steps'])

    def test_unknown_tool_key_in_saved_value_is_ignored(self):
        database.set_app_setting(
            self.app_module.PROCESSING_OVERLAY_SETTING_KEY,
            json.dumps({'tools': {'not-a-tool': {'steps': ['x']}}})
        )
        config = self._config()
        self.assertNotIn('not-a-tool', config['tools'])
        self.assertEqual(len(config['tools']), 6)

    def test_regular_user_cannot_change_the_settings(self):
        self._login(email='worker@example.com', role='user')
        response = self._post({'overlay_enabled': 'on', 'steps__summary': 'Sneaky'})
        self.assertIn(response.status_code, (301, 302))
        self.assertEqual(self._config()['tools']['summary']['steps'],
                         self.app_module.PROCESSING_OVERLAY_DEFAULTS['summary']['steps'])

    def test_config_reaches_the_page_for_the_browser(self):
        self._login(email='worker@example.com', role='user')
        body = self.client.get('/summary').data.decode('utf-8', 'replace')
        self.assertIn('PROCESSING_PROGRESS_CONFIG', body)
        self.assertIn('Counting participants', body)

    def test_markup_in_stage_text_is_escaped_on_the_page(self):
        """Stage text is admin-editable, so it must not be able to inject script."""
        self._login()
        self._post({'overlay_enabled': 'on',
                    'steps__summary': '<script>alert(1)</script>'})
        body = self.client.get('/summary').data.decode('utf-8', 'replace')
        self.assertNotIn('<script>alert(1)</script>', body)

    def test_settings_page_shows_the_tab(self):
        self._login()
        body = self.client.get('/admin/settings').data.decode('utf-8', 'replace')
        self.assertIn('Processing Animation', body)
        self.assertIn('steps__smart-matcher', body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
