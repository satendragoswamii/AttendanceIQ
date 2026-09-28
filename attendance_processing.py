import os
import csv
import math
import re
from copy import copy
from datetime import datetime, timedelta
import pandas as pd
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import PatternFill

# ====================================================
# Helper Functions
# ====================================================

def format_time(time_in_minutes):
    """Converts a float (minutes) to a string 'M minutes S seconds'."""
    minutes = int(math.floor(time_in_minutes))
    seconds = int(round((time_in_minutes - minutes) * 60))
    return f"{minutes} minutes {seconds} seconds"

# Default grace period subtracted from the configured Min. Duration before comparing against
# a participant's actual session duration, used when the caller doesn't specify one. E.g. with
# Min. Duration = 65 minutes and a 50-second grace period, a participant is marked present at
# 64 minutes 10 seconds or above, and absent below that. Configurable per-request from the
# Configure Attendance Sessions page (0 disables the exception entirely).
DEFAULT_PRESENCE_GRACE_SECONDS = 50

def get_column(df, candidates, col_description="column"):
    """Find one of `candidates` among the file's columns.

    Matching ignores case and surrounding spaces: Zoom writes "Duration
    (Minutes)" while the candidate lists here were spelled "Duration
    (minutes)", so an exact match rejected files in the very format the app
    asks users to upload.
    """
    for candidate in candidates:
        if candidate in df.columns:
            return candidate

    normalised = {str(col).strip().lower(): col for col in df.columns}
    for candidate in candidates:
        match = normalised.get(candidate.strip().lower())
        if match is not None:
            return match

    raise ValueError(f"CSV file must contain one of the following {col_description}: {', '.join(candidates)}")

def read_attendance_log(file_path):
    if file_path.lower().endswith(('.xlsx', '.xls')):
        df = pd.read_excel(file_path, skiprows=3)
    else:
        df = pd.read_csv(file_path, skiprows=3)
    df.columns = [str(col).strip() for col in df.columns]
    return df

def parse_datetime(dt_str):
    try:
        # Try different datetime formats
        formats = [
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%dT%H:%M'
        ]
        for fmt in formats:
            try:
                return datetime.strptime(dt_str.strip(), fmt)
            except ValueError:
                continue
        raise ValueError(f"Invalid datetime format: {dt_str}. Expected format: YYYY-MM-DD HH:MM:SS or YYYY-MM-DDTHH:MM")
    except Exception as e:
        raise ValueError(f"Error parsing datetime {dt_str}: {str(e)}")

def merge_intervals(intervals):
    if not intervals:
        return []
    intervals.sort(key=lambda x: x[0])
    merged = [intervals[0]]
    for current in intervals[1:]:
        last = merged[-1]
        if current[0] <= last[1]:
            merged[-1] = (last[0], max(last[1], current[1]))
        else:
            merged.append(current)
    return merged

def compute_total_duration(intervals):
    total = timedelta()
    for start, end in intervals:
        total += (end - start)
    return total.total_seconds() / 60

def intersect_interval(interval, period):
    start, end = interval
    p_start, p_end = period
    new_start = max(start, p_start)
    new_end = min(end, p_end)
    return (new_start, new_end) if new_start < new_end else None

def get_global_times(file_path):
    try:
        df = read_attendance_log(file_path)
    except Exception as e:
        raise ValueError(f"Error reading file '{file_path}' for global times: {e}")
    name_col = get_column(df, ["Name", "Name (Original Name)", "Name (original name)"], "name column")
    join_col = get_column(df, ["Join Time", "Join time"], "Join Time")
    leave_col = get_column(df, ["Leave Time", "Leave time"], "Leave Time")
    try:
        # Clean and parse datetime strings
        df[join_col] = pd.to_datetime(df[join_col], format='mixed', errors='coerce')
        df[leave_col] = pd.to_datetime(df[leave_col], format='mixed', errors='coerce')
    except Exception as e:
        raise ValueError(f"Error converting join/leave times in '{file_path}': {e}")
    df["Name_lower"] = df[name_col].str.lower()
    global_times = {}
    for name_lower, group in df.groupby("Name_lower"):
        try:
            # Ensure timezone-aware comparison
            join_times = pd.to_datetime(group[join_col], utc=True)
            leave_times = pd.to_datetime(group[leave_col], utc=True)
            global_times[name_lower] = (join_times.min(), leave_times.max())
        except Exception as e:
            print(f"Warning: Error processing times for {name_lower}: {str(e)}")
            continue
    return global_times

def get_total_durations(file_path):
    try:
        df = read_attendance_log(file_path)
        name_col = get_column(df, ["Name", "Name (Original Name)", "Name (original name)"], "name column")
        duration_col = get_column(df, ["Duration", "Duration (minutes)"], "duration column")
        try:
            df[duration_col] = pd.to_numeric(df[duration_col], errors="coerce")
            df["Name_lower"] = df[name_col].str.lower()
            return df.groupby("Name_lower")[duration_col].sum().to_dict()
        except KeyError:
            # If duration column is missing, return empty dict and let processing continue
            return {}
    except Exception as e:
        raise ValueError(f"Error computing total durations from '{file_path}': {e}")

def process_csv_session(file_path, session_start, session_end, time_required, grace_seconds=DEFAULT_PRESENCE_GRACE_SECONDS):
    try:
        df = read_attendance_log(file_path)
    except Exception as e:
        raise ValueError(f"Error reading file '{file_path}': {e}")
    name_col = get_column(df, ["Name", "Name (Original Name)", "Name (original name)"], "name column")
    email_col = get_column(df, ["Email", "User Email"], "Email")
    join_col = get_column(df, ["Join Time", "Join time"], "Join Time")
    leave_col = get_column(df, ["Leave Time", "Leave time"], "Leave Time")
    try:
        # Ensure proper datetime parsing with mixed formats
        df[join_col] = pd.to_datetime(df[join_col], format='mixed', errors='coerce', utc=True)
        df[leave_col] = pd.to_datetime(df[leave_col], format='mixed', errors='coerce', utc=True)
        
        # Convert session times to UTC for comparison
        session_start = pd.to_datetime(session_start).tz_localize('UTC')
        session_end = pd.to_datetime(session_end).tz_localize('UTC')
    except Exception as e:
        raise ValueError(f"Error converting join/leave times in '{file_path}': {e}")
    df["Name_lower"] = df[name_col].str.lower()
    session_results = {}
    for name_lower, group in df.groupby("Name_lower"):
        original_name = group.iloc[0][name_col]
        email = group.iloc[0][email_col]
        intervals = [(row[join_col], row[leave_col]) for _, row in group.iterrows()]
        merged = merge_intervals(intervals)
        session_intervals = [intersect_interval(interval, (session_start, session_end))
                             for interval in merged if intersect_interval(interval, (session_start, session_end))]
        session_intervals = merge_intervals(session_intervals)
        session_duration = compute_total_duration(session_intervals)
        effective_threshold = max(0.0, time_required - (grace_seconds / 60))
        if session_duration >= effective_threshold:
            status = "P"
            shortfall = 0
        else:
            status = "A"
            shortfall = round(time_required - session_duration, 2)
        session_results[name_lower] = {
            "Name": original_name,
            "Email": email,
            "session_duration": session_duration,
            "status": status,
            "shortfall": shortfall
        }
    return session_results

def process_sessions_for_file(file_path, sessions_info):
    """Build one attendance record per participant, merged across all configured
    sessions for this file. Column naming is fixed (Name, Email, Join Time, Leave Time,
    one column per session, Total Duration, Shortfall Reason) regardless of who's
    running the report -- there used to be a second, more verbose naming variant for
    regular (non-admin) users, but the output is the same for everyone now."""
    global_participants = {}
    total_sessions = len(sessions_info)
    session_labels = []

    # Process sessions and collect data
    for session_index, session in enumerate(sessions_info, start=1):
        try:
            grace_seconds = session.get("grace_seconds", DEFAULT_PRESENCE_GRACE_SECONDS)
            session_results = process_csv_session(file_path, session["session_start"], session["session_end"], session["time_required"], grace_seconds)
            session_global_times = get_global_times(file_path)
        except ValueError as ve:
            raise ValueError(str(ve))

        for name_lower, details in session_results.items():
            if name_lower not in session_global_times:
                continue
            g_join, g_leave = session_global_times[name_lower]
            session_detail = {
                "status": details["status"],
                "shortfall": details["shortfall"],
                "session_duration": details["session_duration"]
            }
            if name_lower not in global_participants:
                global_participants[name_lower] = {
                    "Name": details["Name"],
                    "Email": details["Email"],
                    "global_join": g_join,
                    "global_leave": g_leave,
                    "total_duration": details["session_duration"],
                    "sessions": {session_index: session_detail}
                }
            else:
                curr = global_participants[name_lower]
                curr["global_join"] = min(curr["global_join"], g_join)
                curr["global_leave"] = max(curr["global_leave"], g_leave)
                curr["total_duration"] += details["session_duration"]
                curr["sessions"][session_index] = session_detail

        session_labels.append(f"Session {session_index} ({session['session_start'].strftime('%Y-%m-%d %H:%M:%S')})")

    # Handle missing sessions
    for participant in global_participants.values():
        for i in range(1, total_sessions + 1):
            if i not in participant["sessions"]:
                req = sessions_info[i-1]["time_required"]
                participant["sessions"][i] = {"status": "A", "shortfall": req, "session_duration": 0}

    # Get raw durations
    try:
        raw_durations = get_total_durations(file_path)
    except Exception as e:
        raise ValueError(str(e))

    for name_lower, participant in global_participants.items():
        if name_lower in raw_durations:
            participant["total_duration"] = raw_durations[name_lower]

    output_records = []
    for participant in global_participants.values():
        record = {
            "Name": participant["Name"],
            "Email": participant["Email"],
            "Join Time": participant["global_join"].strftime('%Y-%m-%d %H:%M:%S'),
            "Leave Time": participant["global_leave"].strftime('%Y-%m-%d %H:%M:%S')
        }

        for i in range(1, total_sessions + 1):
            record[session_labels[i-1]] = participant["sessions"][i]["status"]

        record["Total Duration"] = round(participant["total_duration"], 2)

        breakdown_msgs = []
        for i in range(1, total_sessions + 1):
            sess = participant["sessions"][i]
            if sess["status"] == "A":
                req = sessions_info[i-1]["time_required"]
                computed = sess["session_duration"]
                breakdown_msgs.append(f"User duration is just {format_time(computed)} out of {format_time(req)} minutes, which is why marking absent in {session_labels[i-1]}")

        if breakdown_msgs:
            record["Shortfall Reason"] = "; ".join(breakdown_msgs)

        output_records.append(record)

    # Generate session summary
    session_summary = []
    for i in range(1, total_sessions + 1):
        present_count = sum(1 for p in global_participants.values() if p["sessions"].get(i, {}).get("status", "A") == "P")
        absent_count = sum(1 for p in global_participants.values() if p["sessions"].get(i, {}).get("status", "A") == "A")
        session_summary.append(f"Session {i}: Present: {present_count}, Absent: {absent_count}")

    return output_records, session_labels, session_summary


def filter_excluded_records(output_records):
    """Remove participants on the excluded-participants list from a list of computed
    attendance records (matched by email first, then by name -- same priority as the
    rest of the app's exclusion handling).

    Shared by write_excel() (the real export) and the Configure Attendance Sessions
    Preview, so a participant excluded there is excluded identically in both places --
    the preview never shows someone who wouldn't end up in the final report.
    """
    try:
        from database import get_excluded_identifiers
        excluded_names, excluded_emails = get_excluded_identifiers()
    except Exception:
        excluded_names, excluded_emails = set(), set()

    filtered_records = []
    for record in output_records:
        # Try to find email in various possible column names (case-insensitive)
        email = ''
        for key in record.keys():
            key_lower = str(key).lower()
            if 'email' in key_lower:
                email = str(record.get(key, '')).strip().lower()
                break

        # Get name from record (case-insensitive comparison). Checks the exact "Name"
        # key first (the current unified output format), then older/verbose variants,
        # then falls back to any key containing "name" at all.
        name = ''
        for candidate_key in ('Name', 'Name (Original Name)', 'Name (original name)'):
            if candidate_key in record:
                name = str(record.get(candidate_key, '')).strip().lower()
                if name:
                    break
        if not name:
            for key in record.keys():
                if 'name' in str(key).lower():
                    name = str(record.get(key, '')).strip().lower()
                    break

        # Check if this participant should be excluded
        # PRIORITY: Email matching first (more reliable identifier)
        is_excluded = False

        # First check email (highest priority)
        if email and email in excluded_emails:
            is_excluded = True
        # Only check name if email didn't match or email is not available
        elif not email or email not in excluded_emails:
            if name and name in excluded_names:
                is_excluded = True

        if not is_excluded:
            filtered_records.append(record)

    return filtered_records


def _safe_excel_duration(value):
    """Convert only unambiguous duration strings to Excel numeric values."""
    if not isinstance(value, str):
        return value, None

    text = value.strip()
    if not text:
        return value, None

    try:
        number = float(text)
        if not math.isfinite(number):
            return value, None
        return (int(number) if number.is_integer() else number), None
    except ValueError:
        pass

    match = re.fullmatch(r'(\d+):(\d{2})(?::(\d{2}))?', text)
    if not match:
        return value, None

    hours, minutes, seconds = (int(part) if part is not None else 0 for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        return value, None

    excel_value = (hours * 3600 + minutes * 60 + seconds) / 86400
    return excel_value, '[h]:mm:ss' if match.group(3) else '[h]:mm'


def _format_attendance_workbook(workbook):
    """Apply presentation-only formatting to the Attendance Generator workbook."""
    sheet1 = workbook['Sheet1'] if 'Sheet1' in workbook.sheetnames else None
    attendance = workbook['Attendance'] if 'Attendance' in workbook.sheetnames else None

    if sheet1 is not None:
        for cell in sheet1[4]:
            if cell.value is not None:
                font = copy(cell.font)
                font.bold = True
                cell.font = font

        name_column = None
        for cell in sheet1[4]:
            if str(cell.value).strip().lower() in {'name', 'name (original name)'}:
                name_column = cell.column
                break

        if name_column and sheet1.max_row > 4:
            rows = list(sheet1.iter_rows(min_row=5, max_row=sheet1.max_row, values_only=True))
            rows.sort(key=lambda row: (str(row[name_column - 1] or '').casefold(),))
            for row_number, values in enumerate(rows, start=5):
                for column_number, value in enumerate(values, start=1):
                    sheet1.cell(row=row_number, column=column_number).value = value

        for row_number in range(5, sheet1.max_row + 1):
            cell = sheet1.cell(row=row_number, column=5)
            converted, number_format = _safe_excel_duration(cell.value)
            cell.value = converted
            if number_format:
                cell.number_format = number_format

    if attendance is not None:
        header_fill = PatternFill(fill_type='solid', fgColor='87CEEB')
        for cell in attendance[1]:
            if cell.value is not None:
                font = copy(cell.font)
                font.bold = True
                cell.font = font
                cell.fill = header_fill

        for row in attendance.iter_rows(min_col=3, max_col=6):
            for cell in row:
                alignment = copy(cell.alignment)
                alignment.horizontal = 'center'
                cell.alignment = alignment

        duplicate_fill = PatternFill(fill_type='solid', fgColor='FFF2CC')
        attendance.conditional_formatting.add(
            f'A2:A{max(attendance.max_row, 2)}',
            FormulaRule(formula=['COUNTIF($A:$A,A2)>1'], fill=duplicate_fill)
        )
        attendance.conditional_formatting.add(
            f'B2:B{max(attendance.max_row, 2)}',
            FormulaRule(formula=['COUNTIF($B:$B,B2)>1'], fill=duplicate_fill)
        )

    for worksheet in (sheet1, attendance):
        if worksheet is None:
            continue
        for column_cells in worksheet.columns:
            column_letter = column_cells[0].column_letter
            width = max((len(str(cell.value)) if cell.value is not None else 0 for cell in column_cells), default=0)
            worksheet.column_dimensions[column_letter].width = min(max(width + 2, 10), 60)


def write_excel(raw_log_df, output_records, output_file, custom_columns=None, hidden_columns=None):
    """
    Write Excel file with optional custom column mappings

    Args:
        raw_log_df: Raw log dataframe
        output_records: List of attendance records
        output_file: Output file path
        custom_columns: Dict mapping original column names to custom names
        hidden_columns: Iterable of original column names to omit entirely from the
            Attendance sheet (checked before renaming, so it always refers to the
            original/default name regardless of any custom_columns mapping)
    """
    try:
        # Filter out excluded participants from the Attendance sheet
        filtered_records = filter_excluded_records(output_records)

        hidden_set = set(hidden_columns) if hidden_columns else set()
        if hidden_set:
            filtered_records = [
                {k: v for k, v in record.items() if k not in hidden_set}
                for record in filtered_records
            ]

        # Apply custom column mappings if provided
        if custom_columns and isinstance(custom_columns, dict):
            renamed_records = []
            for record in filtered_records:
                new_record = {}
                for old_col, value in record.items():
                    # Use custom name if mapping exists, otherwise keep original
                    new_col = custom_columns.get(old_col, old_col)
                    new_record[new_col] = value
                renamed_records.append(new_record)
            filtered_records = renamed_records

        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            # Sheet1: Raw log data (unchanged - keep all data)
            raw_log_df.to_excel(writer, sheet_name="Sheet1", index=False, header=False)
            # Attendance: Filtered records (excluded participants removed, with custom column names)
            pd.DataFrame(filtered_records).to_excel(writer, sheet_name="Attendance", index=False)
            try:
                _format_attendance_workbook(writer.book)
            except Exception as formatting_error:
                print(f"Warning: Could not apply attendance workbook formatting: {formatting_error}")
    except Exception as e:
        raise ValueError(f"Error saving output Excel file: {e}")
