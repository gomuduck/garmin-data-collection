"""Sync recent Garmin running activities into the Runs Google Sheet tab."""

import os
import time
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

import gspread
from garminconnect import Garmin
from garminconnect import GarminConnectConnectionError, GarminConnectTooManyRequestsError

from collect import get_sheets_client

TOKEN_STORE = os.path.expanduser("~/.garminconnect")
LOCAL_TZ = ZoneInfo("Australia/Brisbane")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
SHEET_NAME = "Runs"
RUN_TYPES = {"running", "treadmill_running", "trail_running"}
REQUIRED_HEADERS = {"date", "session_type", "distance_km", "duration_min", "avg_pace_min_km", "avg_hr_bpm", "max_hr_bpm", "rpe_1_10", "cadence_spm", "shoes", "notes"}
GARMIN_HEADERS = ("activity_name", "calories", "elevation_gain_m")
MAX_ATTEMPTS = 4


def _retry(operation: Any, description: str) -> Any:
    """Retry transient Garmin failures with bounded exponential backoff."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            return operation()
        except (GarminConnectConnectionError, GarminConnectTooManyRequestsError, TimeoutError, OSError):
            if attempt == MAX_ATTEMPTS - 1:
                raise
            delay = 2**attempt
            print(f"  {description} failed transiently; retrying in {delay}s...")
            time.sleep(delay)
    raise RuntimeError("unreachable")


def _activity_date(activity: dict[str, Any]) -> str | None:
    """Convert Garmin's activity timestamp to a Brisbane calendar date."""
    raw_gmt = activity.get("startTimeGMT")
    raw_local = activity.get("startTimeLocal")
    raw = raw_gmt or raw_local
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc).astimezone(LOCAL_TZ).date().isoformat()
    value = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc if raw_gmt else LOCAL_TZ)
    return parsed.astimezone(LOCAL_TZ).date().isoformat()


def _number(value: Any) -> float | None:
    """Return a finite numeric value or None for missing Garmin fields."""
    try:
        number = float(value) if value is not None else None
        return number if number is not None and isfinite(number) else None
    except (TypeError, ValueError):
        return None


def normalize_activity(activity: dict[str, Any]) -> dict[str, Any] | None:
    """Map one Garmin activity summary to the Runs sheet schema."""
    activity_type = (activity.get("activityType") or {}).get("typeKey")
    if activity_type not in RUN_TYPES:
        return None

    activity_date = _activity_date(activity)
    distance_m = _number(activity.get("distance"))
    duration_s = _number(activity.get("duration"))
    if activity_date is None or not distance_m or not duration_s:
        return None

    distance_km = distance_m / 1000
    duration_min = duration_s / 60
    return {
        "date": activity_date,
        "activity_name": activity.get("activityName", ""),
        "session_type": "",
        "distance_km": round(distance_km, 3),
        "duration_min": round(duration_min, 2),
        "avg_pace_min_km": round(duration_min / distance_km, 2),
        "avg_hr_bpm": activity.get("averageHR"),
        "max_hr_bpm": activity.get("maxHR"),
        "cadence_spm": activity.get("averageRunCadence") or activity.get("averageRunningCadenceInStepsPerMinute"),
        "calories": activity.get("calories"),
        "elevation_gain_m": activity.get("elevationGain"),
    }


def _key(row: dict[str, Any]) -> tuple[str, float] | None:
    """Build the requested date-plus-distance idempotency key."""
    try:
        return str(row["date"]), round(float(row["distance_km"]), 3)
    except (KeyError, TypeError, ValueError):
        return None


def _prepare_headers(sheet: gspread.Worksheet) -> list[str]:
    """Validate the existing schema and append missing Garmin-owned columns."""
    headers = sheet.row_values(1)
    missing = REQUIRED_HEADERS - set(headers)
    if missing:
        raise RuntimeError(f"Runs tab is missing required headers: {sorted(missing)}")
    additions = [header for header in GARMIN_HEADERS if header not in headers]
    headers.extend(additions)
    print("Runs column mapping (dry-run):")
    for index, header in enumerate(headers, start=1):
        mode = "manual-only" if header in {"rpe_1_10", "shoes", "notes"} else "Garmin/blank"
        print(f"  {index}: {header} [{mode}]")
    if additions:
        print(f"Schema migration: append Garmin columns {additions}")
        sheet.update("A1", [headers])
    return headers


def sync_runs(sheet: gspread.Worksheet, activities: list[dict[str, Any]]) -> None:
    """Upsert running activities while preserving manual columns."""
    headers = _prepare_headers(sheet)
    print("Rows to write (dry-run):")
    for activity in activities:
        print(f"  {activity['date']} | {activity.get('activity_name', '')} | {activity['distance_km']} km")
    existing = sheet.get_all_values()
    existing_keys = {
        key: index + 2
        for index, values in enumerate(existing[1:])
        if (key := _key(dict(zip(headers, values)))) is not None
    }

    manual_headers = {"rpe_1_10", "shoes", "notes"}
    for activity in activities:
        key = _key(activity)
        if key is None:
            continue
        values = [activity.get(header, "") for header in headers]
        row_number = existing_keys.get(key)
        if row_number is None:
            sheet.append_row(values)
            print(f"  Appended run {key[0]} / {key[1]:.3f} km")
            continue

        current = dict(zip(headers, existing[row_number - 1]))
        for header in manual_headers | {"session_type"}:
            values[headers.index(header)] = current.get(header, "")
        sheet.update(f"A{row_number}", [values])
        print(f"  Updated run {key[0]} / {key[1]:.3f} km; manual fields preserved")


def main() -> None:
    """Fetch the last 30 days of running activities and sync them."""
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID is required")
    client = Garmin(
        email=os.environ.get("GARMIN_EMAIL"),
        password=os.environ.get("GARMIN_PASSWORD"),
    )
    _retry(lambda: client.login(TOKEN_STORE), "Garmin login")
    end_date = datetime.now(LOCAL_TZ).date()
    start_date = end_date - timedelta(days=30)
    activities = _retry(
        lambda: client.get_activities_by_date(start_date.isoformat(), end_date.isoformat()),
        "Garmin activity fetch",
    ) or []
    rows = [row for activity in activities for row in [normalize_activity(activity)] if row is not None]
    print(f"Found {len(rows)} running activities in the last 30 days.")
    if not rows:
        return
    sheet = get_sheets_client().open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    sync_runs(sheet, rows)


if __name__ == "__main__":
    main()
