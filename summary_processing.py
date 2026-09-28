"""
Meeting summary extraction.

Reads one row of headline information out of each uploaded attendance workbook
and collects them into a single report. The layout is the one produced by the
Attendance Generator (see write_excel in attendance_processing.py):

    Sheet 1 ("Sheet1")   raw Zoom log, written without a header row, so the
                         Zoom header sits in row 1 and its values in row 2:
                             A2 -> Subject / meeting topic
                             B2 -> Meeting ID
                             F2 -> End Time
    Sheet 2 ("Attendance")  one row per participant; the name column is counted

A CSV upload only carries the first sheet, so it yields the three metadata
fields and no participant count.
"""

import csv
import math
import os
from datetime import datetime, date, time as datetime_time

import pandas as pd

# Column order of the generated report. "Source File" and "Status" bracket the
# four requested fields so a bulk run stays traceable back to its inputs.
SUMMARY_COLUMNS = [
    'Source File',
    'Subject',
    'Meeting ID',
    'End Time',
    'Total Names',
    'Unique Names',
    'Status',
]

# Cell positions on the first sheet, as (row_index, column_index), zero-based.
SUBJECT_CELL = (1, 0)      # A2
MEETING_ID_CELL = (1, 1)   # B2
END_TIME_CELL = (1, 5)     # F2

ATTENDANCE_SHEET_NAMES = ('attendance',)

# The Attendance sheet's name column, allowing for the renaming that the output
# column settings can apply.
NAME_COLUMN_CANDIDATES = (
    'name',
    'name (original name)',
    'participant name',
    'full name',
    'student name',
)

EXCEL_EXTENSIONS = ('.xlsx', '.xlsm', '.xls')
CSV_EXTENSIONS = ('.csv', '.txt')
SUPPORTED_EXTENSIONS = EXCEL_EXTENSIONS + CSV_EXTENSIONS


def _is_blank(value):
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _format_cell(value):
    """Render a raw cell as the string the report should carry."""
    if _is_blank(value):
        return ''
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, datetime_time):
        return value.strftime('%H:%M:%S')
    if isinstance(value, float) and float(value).is_integer():
        # Meeting IDs arrive from Excel as floats; 8_5712_3456.0 is not an ID.
        return str(int(value))
    return str(value).strip()


def _count_names(frame):
    """Count participants on an Attendance sheet: (total rows, distinct names)."""
    if frame is None or frame.empty:
        return 0, 0

    name_column = None
    for column in frame.columns:
        if str(column).strip().lower() in NAME_COLUMN_CANDIDATES:
            name_column = column
            break
    if name_column is None:
        # Custom column names are allowed, so fall back to the leftmost column
        # rather than reporting zero for a sheet that clearly has people on it.
        name_column = frame.columns[0]

    values = frame[name_column].dropna().astype(str).str.strip()
    values = values[values != '']
    return int(len(values)), int(values.nunique())


def _read_csv_metadata(file_path):
    """First two rows of a CSV, tolerating the encodings Zoom exports use."""
    for encoding in ('utf-8-sig', 'utf-8', 'latin-1'):
        try:
            with open(file_path, newline='', encoding=encoding) as handle:
                rows = []
                for row in csv.reader(handle):
                    rows.append(row)
                    if len(rows) >= 2:
                        break
            return rows
        except UnicodeDecodeError:
            continue
    raise ValueError('Could not decode this CSV file')


def _cell_from_rows(rows, position):
    row_index, column_index = position
    if len(rows) <= row_index:
        return ''
    row = rows[row_index]
    if len(row) <= column_index:
        return ''
    return row[column_index]


def _cell_from_frame(frame, position):
    row_index, column_index = position
    if frame.shape[0] <= row_index or frame.shape[1] <= column_index:
        return ''
    return frame.iat[row_index, column_index]


def read_meeting_summary(file_path, display_name=None):
    """Extract one summary row from a single uploaded file.

    Never raises: a file that cannot be read comes back as a row whose Status
    explains why, so one bad file in a bulk run does not sink the others.
    """
    name = display_name or os.path.basename(file_path)
    row = {
        'Source File': name,
        'Subject': '',
        'Meeting ID': '',
        'End Time': '',
        'Total Names': 0,
        'Unique Names': 0,
        'Status': 'OK',
    }

    extension = os.path.splitext(file_path)[1].lower()
    if extension not in SUPPORTED_EXTENSIONS:
        row['Status'] = f'Unsupported file type ({extension or "no extension"})'
        return row

    try:
        if extension in CSV_EXTENSIONS:
            rows = _read_csv_metadata(file_path)
            if len(rows) < 2:
                row['Status'] = 'No data row found (row 2 is empty)'
                return row
            row['Subject'] = _format_cell(_cell_from_rows(rows, SUBJECT_CELL))
            row['Meeting ID'] = _format_cell(_cell_from_rows(rows, MEETING_ID_CELL))
            row['End Time'] = _format_cell(_cell_from_rows(rows, END_TIME_CELL))
            row['Total Names'] = ''
            row['Unique Names'] = ''
            row['Status'] = 'Metadata only - a CSV has no Attendance sheet'
            return row

        with pd.ExcelFile(file_path) as workbook:
            sheet_names = list(workbook.sheet_names)
            if not sheet_names:
                row['Status'] = 'Workbook has no sheets'
                return row

            metadata = pd.read_excel(
                workbook, sheet_name=sheet_names[0], header=None, nrows=2
            )
            if metadata.shape[0] < 2:
                row['Status'] = 'Sheet 1 has no data row (row 2 is empty)'
            else:
                row['Subject'] = _format_cell(_cell_from_frame(metadata, SUBJECT_CELL))
                row['Meeting ID'] = _format_cell(_cell_from_frame(metadata, MEETING_ID_CELL))
                row['End Time'] = _format_cell(_cell_from_frame(metadata, END_TIME_CELL))

            attendance_sheet = None
            for sheet_name in sheet_names:
                if str(sheet_name).strip().lower() in ATTENDANCE_SHEET_NAMES:
                    attendance_sheet = sheet_name
                    break
            if attendance_sheet is None and len(sheet_names) > 1:
                attendance_sheet = sheet_names[1]

            if attendance_sheet is None:
                row['Total Names'] = 0
                row['Unique Names'] = 0
                row['Status'] = 'No Attendance sheet found'
            else:
                attendance = pd.read_excel(workbook, sheet_name=attendance_sheet)
                total, unique = _count_names(attendance)
                row['Total Names'] = total
                row['Unique Names'] = unique
                if total == 0:
                    row['Status'] = 'Attendance sheet is empty'

        missing = [
            label for label, key in
            (('Subject', 'Subject'), ('Meeting ID', 'Meeting ID'), ('End Time', 'End Time'))
            if not row[key]
        ]
        if missing and row['Status'] == 'OK':
            row['Status'] = 'Missing: ' + ', '.join(missing)
        return row

    except Exception as exc:  # noqa: BLE001 - reported per file, never fatal
        row['Status'] = f'Could not read file: {exc}'
        return row


def build_summary_rows(files):
    """Summarise many files. `files` is an iterable of (path, display_name)."""
    return [read_meeting_summary(path, display_name) for path, display_name in files]


def write_summary_report(rows, output_path, output_format='xlsx'):
    """Write the collected rows as .xlsx or .csv, always in SUMMARY_COLUMNS order."""
    frame = pd.DataFrame(rows, columns=SUMMARY_COLUMNS)
    if output_format == 'csv':
        frame.to_csv(output_path, index=False)
    else:
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            frame.to_excel(writer, sheet_name='Summary', index=False)
            worksheet = writer.sheets['Summary']
            for index, column in enumerate(SUMMARY_COLUMNS, start=1):
                longest = max(
                    [len(str(column))] +
                    [len(str(row.get(column, ''))) for row in rows]
                )
                worksheet.column_dimensions[
                    worksheet.cell(row=1, column=index).column_letter
                ].width = min(max(longest + 2, 12), 60)
    return output_path
