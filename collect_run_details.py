"""Sync Garmin lap/split detail into a coach-readable Google Sheet tab."""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

import gspread
from garminconnect import Garmin

from collect_runs import (
    LOCAL_TZ,
    RUN_TYPES,
    SPREADSHEET_ID,
    TOKEN_STORE,
    _activity_date,
    _number,
    _retry,
    get_sheets_client,
)

DETAIL_SHEET_NAME = "Run Splits"
HEADERS = (
    "activity_id",
    "date",
    "activity_name",
    "split_number",
    "segment_type",
    "distance_km",
    "duration_min",
    "pace_min_km",
    "avg_hr_bpm",
    "max_hr_bpm",
    "cadence_spm",
    "elevation_gain_m",
)
SEGMENT_FIELDS = ("purposeTypeKey", "purpose", "intensity", "lapType", "workoutStepLabel", "message")
SEGMENT_NAMES = {
    "warmup": "warmup",
    "warm_up": "warmup",
    "cooldown": "cooldown",
    "cool_down": "cooldown",
    "recovery": "recovery",
    "interval": "interval",
    "threshold": "threshold",
    "rest": "recovery",
}


def _segment_type(split: dict[str, Any]) -> str:
    """Return Garmin's workout segment label without inventing one."""
    for field in SEGMENT_FIELDS:
        raw = split.get(field)
        if raw:
            value = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
            return SEGMENT_NAMES.get(value, value)
    return ""


def normalize_split(
    activity_id: str,
    activity_date: str,
    activity_name: str,
    split_number: int,
    raw: dict[str, Any],
) -> dict[str, Any] | None:
    """Map one Garmin lap into the Run Splits sheet schema."""
    distance_m = _number(raw.get("distance") or raw.get("distanceMeters"))
    duration_s = _number(raw.get("duration") or raw.get("elapsedDuration"))
    if not distance_m or not duration_s:
        return None
    distance_km = distance_m / 1000
    duration_min = duration_s / 60
    return {
        "activity_id": activity_id,
        "date": activity_date,
        "activity_name": activity_name,
        "split_number": split_number,
        "segment_type": _segment_type(raw),
        "distance_km": round(distance_km, 3),
        "duration_min": round(duration_min, 2),
        "pace_min_km": round(duration_min / distance_km, 2),
        "avg_hr_bpm": raw.get("averageHR"),
        "max_hr_bpm": raw.get("maxHR"),
        "cadence_spm": raw.get("averageRunCadence") or raw.get("averageCadence"),
        "elevation_gain_m": raw.get("elevationGain"),
    }


def _split_items(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Find lap records across Garmin's known split response shapes."""
    for key in ("lapDTOs", "splitDTOs", "splits", "activitySplitSummaries"):
        value = payload.get(key)
        if isinstance(value, list):
            return (item for item in value if isinstance(item, dict))
    return ()


def _sheet(client: gspread.Client) -> gspread.Worksheet:
    """Open or create the detail tab."""
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    try:
        sheet = spreadsheet.worksheet(DETAIL_SHEET_NAME)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=DETAIL_SHEET_NAME, rows=1000, cols=len(HEADERS))
    if sheet.row_values(1) != list(HEADERS):
        sheet.update("A1", [list(HEADERS)])
    return sheet


def sync_splits(sheet: gspread.Worksheet, rows: list[dict[str, Any]]) -> None:
    """Upsert split rows by Garmin activity ID and split number."""
    existing = sheet.get_all_values()
    existing_keys = {
        (values[0], values[3]): index + 2
        for index, values in enumerate(existing[1:])
        if len(values) > 3 and values[0] and values[3]
    }
    print("Run Splits column mapping (dry-run):")
    for index, header in enumerate(HEADERS, start=1):
        print(f"  {index}: {header} [Garmin]")
    for row in rows:
        values = [row.get(header, "") for header in HEADERS]
        key = (str(row["activity_id"]), str(row["split_number"]))
        row_number = existing_keys.get(key)
        if row_number is None:
            sheet.append_row(values)
        else:
            sheet.update(f"A{row_number}", [values])


def main() -> None:
    """Fetch recent running laps and sync them to Google Sheets."""
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID is required")
    client = Garmin(email=os.environ.get("GARMIN_EMAIL"), password=os.environ.get("GARMIN_PASSWORD"))
    _retry(lambda: client.login(TOKEN_STORE), "Garmin login")
    end_date = datetime.now(LOCAL_TZ).date()
    start_date = end_date - timedelta(days=30)
    activities = _retry(
        lambda: client.get_activities_by_date(start_date.isoformat(), end_date.isoformat()),
        "Garmin activity fetch",
    ) or []
    rows: list[dict[str, Any]] = []
    for activity in activities:
        activity_type = (activity.get("activityType") or {}).get("typeKey")
        activity_id = str(activity.get("activityId", ""))
        if activity_type not in RUN_TYPES or not activity_id:
            continue
        activity_date = _activity_date(activity)
        if not activity_date:
            continue
        payload = _retry(lambda: client.get_activity_splits(activity_id), f"Garmin splits fetch {activity_id}") or {}
        for split_number, raw in enumerate(_split_items(payload), start=1):
            split = normalize_split(activity_id, activity_date, str(activity.get("activityName", "")), split_number, raw)
            if split:
                rows.append(split)
    print(f"Found {len(rows)} running splits in the last 30 days.")
    if rows:
        sync_splits(_sheet(get_sheets_client()), rows)


if __name__ == "__main__":
    main()
