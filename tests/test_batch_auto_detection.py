"""Unit tests for automatic batch detection in the Final Excel Generator upload handler.

There is no manual batch picker: the uploaded roster's Email column (and Participant Name
as a fallback) is matched against each stored batch's student list to decide which batch
the learned Zoom-name -> roster-name pairs belong to. Runs against a temporary SQLite file
so it never touches the real attendancify.db.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database

os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret-key')


class BatchAutoDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import only after DATABASE is patched (in a throwaway temp file), so
        # comprehensive_app's module-level init_db()/ensure_superadmin_exists() calls
        # never touch the real database file.
        fd, import_tmp_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        database.DATABASE = import_tmp_path
        database.init_db()
        global app
        import comprehensive_app as app
        os.remove(import_tmp_path)

    def setUp(self):
        import pandas as pd
        self.pd = pd
        fd, self._tmp_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        database.DATABASE = self._tmp_path
        database.init_db()

        self.batch_a = database.create_batch('Batch A', None, 'tester@example.com')
        self.batch_b = database.create_batch('Batch B', None, 'tester@example.com')
        database.add_student_to_batch(self.batch_a, 'Alice Adams', 'alice@x.com')
        database.add_student_to_batch(self.batch_a, 'Bob Brown', 'bob@x.com')
        database.add_student_to_batch(self.batch_b, 'Carol Clark', 'carol@y.com')
        database.add_student_to_batch(self.batch_b, 'Dave Diaz', 'dave@y.com')

    def tearDown(self):
        os.remove(self._tmp_path)

    def test_detects_correct_batch_by_email_overlap(self):
        matched_df = self.pd.DataFrame({
            'Email': ['alice@x.com', 'bob@x.com'],
            'Participant Name': ['Alice Adams', 'Bob Brown'],
            'Raw Name': ['alice a', 'bobb'],
            'Confidence': [90.0, 85.0],
        })
        detected = app._detect_batch_for_roster(matched_df['Email'].tolist(), matched_df['Participant Name'].tolist())
        self.assertEqual(detected, self.batch_a)

    def test_learning_from_detected_batch_does_not_leak_into_other_batch(self):
        matched_df = self.pd.DataFrame({
            'Email': ['alice@x.com', 'bob@x.com'],
            'Participant Name': ['Alice Adams', 'Bob Brown'],
            'Raw Name': ['alice a', 'bobb'],
            'Confidence': [90.0, 85.0],
        })
        detected = app._detect_batch_for_roster(matched_df['Email'].tolist(), matched_df['Participant Name'].tolist())
        app._learn_batch_records_from_matched_sheet(matched_df, detected)

        self.assertEqual(database.get_batch_records_map(self.batch_a)['alice a']['roster_name_matched'], 'Alice Adams')
        self.assertEqual(database.get_batch_records_map(self.batch_b), {})

    def test_unrelated_roster_is_not_matched_to_any_batch(self):
        unrelated_df = self.pd.DataFrame({
            'Email': ['zed@nowhere.com'],
            'Participant Name': ['Zed Zephyr'],
            'Raw Name': ['zedz'],
            'Confidence': [80.0],
        })
        detected = app._detect_batch_for_roster(unrelated_df['Email'].tolist(), unrelated_df['Participant Name'].tolist())
        self.assertIsNone(detected)

    def test_partial_overlap_below_threshold_is_not_matched(self):
        # Only one of several emails overlaps Batch A -- not confident enough to file under it.
        mixed_df = self.pd.DataFrame({
            'Email': ['alice@x.com', 'unknown1@z.com', 'unknown2@z.com', 'unknown3@z.com'],
            'Participant Name': ['Alice Adams', 'Someone Else', 'Another Person', 'Yet Another'],
        })
        detected = app._detect_batch_for_roster(mixed_df['Email'].tolist(), mixed_df['Participant Name'].tolist())
        self.assertIsNone(detected)


if __name__ == '__main__':
    unittest.main()
