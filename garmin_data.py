"""
Shared data layer for plotting scripts.
Reads from Google Sheets (populated daily by collect.py) so the Garmin API
is only called once per day. Import this in any plotting script.
"""

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

LOCAL_TZ = ZoneInfo("Australia/Brisbane")
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def get_sheets_client() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return gspread.authorize(creds)


def _parse_dt(value: str | int | float | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)


def _int(value: str | int | float | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _float(value: str | int | float | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def fetch_sleep_data(days: int = 30) -> list[dict]:
    """
    Read the last `days` days of sleep data from the Daily sheet.
    Returns a list of dicts, oldest first, skipping days with no sleep recorded.
    """
    gc = get_sheets_client()
    sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("Daily")
    # The Daily tab has intentionally blank spacer columns in its header row.
    # Build records from raw values so newer gspread versions do not reject
    # those repeated blank headers.
    values = sheet.get_all_values()
    headers = values[0] if values else []
    records = [dict(zip(headers, row)) for row in values[1:]]

    rows = []
    for r in records:
        sleep_start = _parse_dt(r.get("sleep_start_local", ""))
        sleep_end = _parse_dt(r.get("sleep_end_local", ""))
        total = _int(r.get("total_sleep_seconds"))

        if not sleep_start or not sleep_end or not total:
            continue

        rows.append({
            "date": datetime.strptime(str(r["date"]), "%Y-%m-%d").date(),
            "sleep_start": sleep_start,
            "sleep_end": sleep_end,
            "total_sleep_seconds": total,
            "deep_sleep_seconds": _int(r.get("deep_sleep_seconds")),
            "light_sleep_seconds": _int(r.get("light_sleep_seconds")),
            "rem_sleep_seconds": _int(r.get("rem_sleep_seconds")),
            "awake_seconds": _int(r.get("awake_seconds")),
            "sleep_score": _int(r.get("sleep_score")),
            "avg_hrv": _float(r.get("avg_hrv")),
        })

    # Sort and limit to requested window
    rows.sort(key=lambda r: r["date"])
    return rows[-days:]
