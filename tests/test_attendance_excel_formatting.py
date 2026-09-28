import os
import sys
import tempfile
import unittest
from datetime import timedelta

import pandas as pd
from openpyxl import load_workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from attendance_processing import write_excel


class AttendanceExcelFormattingTests(unittest.TestCase):
    def test_formatting_preserves_rows_and_applies_requested_styles(self):
        raw_log = pd.DataFrame([
            ['Meeting Title', 'Demo'],
            ['Meeting ID', '123'],
            ['Meeting Time', '2026-01-01'],
            ['Name', 'Email', 'Join Time', 'Leave Time', 'Duration'],
            ['Zoe', 'z@example.com', 'join-z', 'leave-z', '12.5'],
            ['Amy', 'a@example.com', 'join-a', 'leave-a', '01:30'],
            ['Bad', 'b@example.com', 'join-b', 'leave-b', 'not-a-duration'],
        ])
        records = [
            {'Name': 'Alex', 'Email': 'same@example.com', 'Join Time': 'j1', 'Leave Time': 'l1', 'Session 1': 'P'},
            {'Name': 'Alex', 'Email': 'same@example.com', 'Join Time': 'j2', 'Leave Time': 'l2', 'Session 1': 'A'},
            {'Name': 'Bea', 'Email': 'other@example.com', 'Join Time': 'j3', 'Leave Time': 'l3', 'Session 1': 'P'},
        ]

        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as output:
            output_path = output.name
        try:
            write_excel(raw_log, records, output_path)
            workbook = load_workbook(output_path)
            sheet1 = workbook['Sheet1']
            attendance = workbook['Attendance']

            self.assertEqual([sheet1.cell(row, 1).value for row in range(5, 8)], ['Amy', 'Bad', 'Zoe'])
            self.assertEqual(sheet1['E5'].value, timedelta(minutes=90))
            self.assertEqual(sheet1['E5'].number_format, '[h]:mm')
            self.assertEqual(sheet1['E6'].value, 'not-a-duration')
            self.assertTrue(sheet1['A4'].font.bold)
            self.assertTrue(attendance['A1'].font.bold)
            self.assertEqual(attendance['A1'].fill.fgColor.rgb, '0087CEEB')
            self.assertEqual(attendance['C2'].alignment.horizontal, 'center')
            self.assertEqual(attendance['F2'].alignment.horizontal, 'center')
            self.assertEqual(len(attendance.conditional_formatting), 2)
            formulas = [str(rule.formula[0]) for rules in attendance.conditional_formatting for rule in rules.rules]
            self.assertIn('COUNTIF($A:$A,A2)>1', formulas)
            self.assertIn('COUNTIF($B:$B,B2)>1', formulas)
            self.assertGreaterEqual(sheet1.column_dimensions['A'].width, 10)
            self.assertGreaterEqual(attendance.column_dimensions['A'].width, 10)
        finally:
            os.remove(output_path)


if __name__ == '__main__':
    unittest.main()