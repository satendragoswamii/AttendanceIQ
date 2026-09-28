"""Unit tests for batch-scoped Zoom-name -> roster-name learning (batch_records table).

Covers: new record insert, existing record update (auto vs. manual source), and
cross-batch isolation. Runs against a temporary SQLite file so it never touches the
real attendancify.db.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


class BatchRecordsLearningTests(unittest.TestCase):
    def setUp(self):
        fd, self._tmp_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self._original_database = database.DATABASE
        database.DATABASE = self._tmp_path
        database.init_db()
        self.batch_a = database.create_batch('Batch A', None, 'tester@example.com')
        self.batch_b = database.create_batch('Batch B', None, 'tester@example.com')

    def tearDown(self):
        database.DATABASE = self._original_database
        os.remove(self._tmp_path)

    def test_new_record_insert(self):
        database.upsert_batch_record(self.batch_a, 'John S', 'John Smith', confidence=91.5, source='single_csv_upload')
        rec = database.get_batch_record(self.batch_a, 'John S')
        self.assertIsNotNone(rec)
        self.assertEqual(rec['roster_name_matched'], 'John Smith')
        self.assertEqual(rec['seen_count'], 1)
        self.assertEqual(rec['source'], 'single_csv_upload')

    def test_repeat_automatic_upsert_does_not_overwrite_match(self):
        """A later, lower-confidence automatic re-match must not silently override the
        stored pairing -- only a manual_review upsert is allowed to change it."""
        database.upsert_batch_record(self.batch_a, 'John S', 'John Smith', confidence=91.5, source='single_csv_upload')
        database.upsert_batch_record(self.batch_a, 'John S', 'Jon Smyth', confidence=70.0, source='single_csv_upload')
        rec = database.get_batch_record(self.batch_a, 'John S')
        self.assertEqual(rec['roster_name_matched'], 'John Smith')
        self.assertEqual(rec['seen_count'], 2)
        self.assertEqual(rec['source'], 'single_csv_upload')

    def test_manual_review_overrides_previous_automatic_match(self):
        database.upsert_batch_record(self.batch_a, 'John S', 'John Smith', confidence=80.0, source='single_csv_upload')
        database.upsert_batch_record(self.batch_a, 'John S', 'Johnathan Smith', confidence=None, source='manual_review')
        rec = database.get_batch_record(self.batch_a, 'John S')
        self.assertEqual(rec['roster_name_matched'], 'Johnathan Smith')
        self.assertEqual(rec['source'], 'manual_review')
        self.assertEqual(rec['seen_count'], 2)

    def test_cross_batch_isolation(self):
        """The same zoom_display_name in two different batches must resolve independently
        and never leak into the other batch's lookup map."""
        database.upsert_batch_record(self.batch_a, 'John S', 'John Smith', confidence=90.0, source='single_csv_upload')
        database.upsert_batch_record(self.batch_b, 'John S', 'Johnny Stark', confidence=88.0, source='single_csv_upload')

        rec_a = database.get_batch_record(self.batch_a, 'John S')
        rec_b = database.get_batch_record(self.batch_b, 'John S')
        self.assertEqual(rec_a['roster_name_matched'], 'John Smith')
        self.assertEqual(rec_b['roster_name_matched'], 'Johnny Stark')

        map_a = database.get_batch_records_map(self.batch_a)
        map_b = database.get_batch_records_map(self.batch_b)
        self.assertEqual(map_a['John S']['roster_name_matched'], 'John Smith')
        self.assertEqual(map_b['John S']['roster_name_matched'], 'Johnny Stark')
        self.assertNotIn('Johnny Stark', [r['roster_name_matched'] for r in map_a.values()])
        self.assertNotIn('John Smith', [r['roster_name_matched'] for r in map_b.values()])

    def test_guard_clause_rejects_missing_or_invalid_batch_id(self):
        with self.assertRaises(ValueError):
            database.upsert_batch_record(None, 'John S', 'John Smith')
        with self.assertRaises(ValueError):
            database.upsert_batch_record(999999, 'John S', 'John Smith')

    def test_touch_batch_record_updates_seen_count_without_changing_match(self):
        database.upsert_batch_record(self.batch_a, 'John S', 'John Smith', confidence=95.0, source='single_csv_upload')
        database.touch_batch_record(self.batch_a, 'John S')
        rec = database.get_batch_record(self.batch_a, 'John S')
        self.assertEqual(rec['seen_count'], 2)
        self.assertEqual(rec['roster_name_matched'], 'John Smith')


if __name__ == '__main__':
    unittest.main()
