"""Sync Garmin lap/split detail into a coach-readable Google Sheet tab."""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timedelta
from math import isfinite
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
    _prepare_headers,
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
    "split_source",
    "is_partial",
)
SEGMENT_FIELDS = (
    "purposeTypeKey", "purpose", "intensityType", "intensity", "lapType", "workoutStepLabel", "message"
)
SEGMENT_NAMES = {
    "warmup": "warmup",
    "warm_up": "warmup",
    "cooldown": "cooldown",
    "cool_down": "cooldown",
    "recovery": "recovery",
    "interval": "interval",
    "threshold": "threshold",
    "rest": "recovery",
    "active": "work",
    "work": "work",
}


def _segment_type(split: dict[str, Any]) -> str:
    """Return Garmin's workout segment label without inventing one."""
    for field in SEGMENT_FIELDS:
        raw = split.get(field)
        if raw:
            value = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
            return SEGMENT_NAMES.get(value, value)
    return ""


def summarize_segments(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build visible Runs-tab totals from labeled detailed split rows."""
    summary: dict[str, Any] = {
        "detail_split_count": len(rows),
        "detail_distance_km": round(sum(float(row.get("distance_km") or 0) for row in rows), 3),
        "detail_duration_min": round(sum(float(row.get("duration_min") or 0) for row in rows), 2),
        "warmup_distance_km": "", "warmup_duration_min": "",
        "work_distance_km": "", "work_duration_min": "",
        "cooldown_distance_km": "", "cooldown_duration_min": "",
    }
    if summary["detail_distance_km"]:
        summary["detail_avg_pace_min_km"] = round(summary["detail_duration_min"] / summary["detail_distance_km"], 2)
    else:
        summary["detail_avg_pace_min_km"] = ""
    for label, prefix in (("warmup", "warmup"), ("work", "work"), ("threshold", "work"), ("interval", "work"), ("cooldown", "cooldown")):
        matching = [row for row in rows if row.get("segment_type") == label]
        if not matching:
            continue
        distance = round(sum(float(row.get("distance_km") or 0) for row in matching), 3)
        duration = round(sum(float(row.get("duration_min") or 0) for row in matching), 2)
        summary[f"{prefix}_distance_km"] = round(float(summary[f"{prefix}_distance_km"] or 0) + distance, 3)
        summary[f"{prefix}_duration_min"] = round(float(summary[f"{prefix}_duration_min"] or 0) + duration, 2)
    return summary


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
        "split_source": "garmin_lap",
        "is_partial": False,
        "distance_km": round(distance_km, 3),
        "duration_min": round(duration_min, 2),
        "pace_min_km": round(duration_min / distance_km, 2),
        "avg_hr_bpm": raw.get("averageHR"),
        "max_hr_bpm": raw.get("maxHR"),
        "cadence_spm": raw.get("averageRunCadence") or raw.get("averageCadence"),
        "elevation_gain_m": raw.get("elevationGain"),
    }


def compute_accurate_splits(
    details: dict[str, Any], activity_id: str, activity_date: str, activity_name: str
) -> list[dict[str, Any]]:
    """Compute kilometre splits by interpolating Garmin GPS/sensor samples."""
    descriptors = details.get("metricDescriptors") or []
    indices = {
        str(item.get("key")): item.get("metricsIndex")
        for item in descriptors
        if isinstance(item, dict) and isinstance(item.get("metricsIndex"), int)
    }
    distance_index = indices.get("sumDistance")
    time_index = next(
        (indices[key] for key in ("sumMovingDuration", "sumDuration", "sumElapsedDuration") if indices.get(key) is not None),
        None,
    )
    samples: list[tuple[float, float]] = []
    for entry in details.get("activityDetailMetrics") or []:
        metrics = entry.get("metrics", []) if isinstance(entry, dict) else []
        if not isinstance(distance_index, int) or not isinstance(time_index, int):
            continue
        if max(distance_index, time_index) >= len(metrics):
            continue
        time_value = _number(metrics[time_index])
        distance_value = _number(metrics[distance_index])
        if time_value is not None and distance_value is not None:
            samples.append((time_value, distance_value))
    samples = sorted((time, distance) for time, distance in samples if isfinite(time) and isfinite(distance))
    if len(samples) < 2 or samples[-1][1] < 1000:
        return []

    def time_at_distance(target: float) -> float:
        for (time_a, distance_a), (time_b, distance_b) in zip(samples, samples[1:]):
            if distance_a <= target <= distance_b:
                if distance_b == distance_a:
                    return time_a
                ratio = (target - distance_a) / (distance_b - distance_a)
                return time_a + ratio * (time_b - time_a)
        return samples[-1][0]

    total_distance = samples[-1][1]
    complete_splits = int(total_distance // 1000)
    rows: list[dict[str, Any]] = []
    previous_time = 0.0
    for split_number in range(1, complete_splits + 1):
        split_time = time_at_distance(split_number * 1000)
        duration_seconds = split_time - previous_time
        rows.append(
            {
                "activity_id": activity_id,
                "date": activity_date,
                "activity_name": activity_name,
                "split_number": split_number,
                "segment_type": "",
                "split_source": "gps_sensor",
                "is_partial": False,
                "distance_km": 1.0,
                "duration_min": round(duration_seconds / 60, 2),
                "pace_min_km": round(duration_seconds / 60, 2),
                "avg_hr_bpm": None,
                "max_hr_bpm": None,
                "cadence_spm": None,
                "elevation_gain_m": None,
            }
        )
        previous_time = split_time
    remaining_distance = total_distance - complete_splits * 1000
    if remaining_distance >= 100:
        duration_seconds = samples[-1][0] - previous_time
        distance_km = remaining_distance / 1000
        rows.append(
            {
                "activity_id": activity_id,
                "date": activity_date,
                "activity_name": activity_name,
                "split_number": complete_splits + 1,
                "segment_type": "",
                "split_source": "gps_sensor",
                "is_partial": True,
                "distance_km": round(distance_km, 3),
                "duration_min": round(duration_seconds / 60, 2),
                "pace_min_km": round(duration_seconds / 60 / distance_km, 2),
                "avg_hr_bpm": None,
                "max_hr_bpm": None,
                "cadence_spm": None,
                "elevation_gain_m": None,
            }
        )
    return rows


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
    """Batch-replace recent split rows, avoiding Sheets write-rate limits."""
    print("Run Splits column mapping (dry-run):")
    for index, header in enumerate(HEADERS, start=1):
        print(f"  {index}: {header} [Garmin]")
    values = [[row.get(header, "") for header in HEADERS] for row in rows]
    sheet.batch_clear([f"A2:{_column_letter(len(HEADERS))}{max(sheet.row_count, len(values) + 1)}"])
    if values:
        sheet.update(values, range_name="A2")


def _column_letter(column_number: int) -> str:
    """Convert a one-based column number to its Sheets letter."""
    letters = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _update_run_summaries(client: gspread.Client, overrides: dict[str, dict[str, Any]]) -> None:
    """Replace summary pace with GPS-derived totals when available."""
    if not overrides:
        return
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Runs")
    headers = _prepare_headers(sheet)
    values = sheet.get_all_values()
    if len(values) < 2 or "activity_id" not in headers:
        return
    id_index = headers.index("activity_id")
    for row_index, row in enumerate(values[1:], start=1):
        if len(row) <= id_index:
            continue
        override = overrides.get(str(row[id_index]))
        if not override:
            continue
        padded = row + [""] * (len(headers) - len(row))
        for field in (
            "distance_km", "duration_min", "avg_pace_min_km", "pace_source",
            "detail_split_count", "detail_distance_km", "detail_duration_min",
            "detail_avg_pace_min_km", "warmup_distance_km", "warmup_duration_min",
            "work_distance_km", "work_duration_min", "cooldown_distance_km",
            "cooldown_duration_min", "detail_source",
        ):
            if field in headers and field in override:
                padded[headers.index(field)] = override[field]
        values[row_index] = padded
    sheet.update(values[1:], range_name="A2")


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
    summary_overrides: dict[str, dict[str, Any]] = {}
    for activity in activities:
        activity_type = (activity.get("activityType") or {}).get("typeKey")
        activity_id = str(activity.get("activityId", ""))
        if activity_type not in RUN_TYPES or not activity_id:
            continue
        activity_date = _activity_date(activity)
        if not activity_date:
            continue
        payload = _retry(lambda: client.get_activity_splits(activity_id), f"Garmin splits fetch {activity_id}") or {}
        raw_splits = list(_split_items(payload))
        activity_name = str(activity.get("activityName", ""))
        accurate_rows: list[dict[str, Any]] = []
        if len(raw_splits) <= 1:
            details = _retry(
                lambda: client.get_activity_details(activity_id, maxchart=2000),
                f"Garmin detail fetch {activity_id}",
            ) or {}
            accurate_rows = compute_accurate_splits(details, activity_id, activity_date, activity_name)
        selected_rows = accurate_rows
        if not selected_rows:
            selected_rows = [
                split
                for split_number, raw in enumerate(raw_splits, start=1)
                if (split := normalize_split(activity_id, activity_date, activity_name, split_number, raw))
            ]
        rows.extend(selected_rows)
        if selected_rows:
            distance_km = sum(float(row["distance_km"]) for row in selected_rows)
            duration_min = sum(float(row["duration_min"]) for row in selected_rows)
            detail_summary = summarize_segments(selected_rows)
            summary_overrides[activity_id] = {
                "distance_km": round(distance_km, 3),
                "duration_min": round(duration_min, 2),
                "avg_pace_min_km": round(duration_min / distance_km, 2),
                "pace_source": "gps_sensor_splits" if accurate_rows else "garmin_lap_splits",
                **detail_summary,
                "detail_source": "gps_sensor" if accurate_rows else "garmin_lap",
            }
    print(f"Found {len(rows)} running splits in the last 30 days.")
    if rows:
        sheets_client = get_sheets_client()
        sync_splits(_sheet(sheets_client), rows)
        _update_run_summaries(sheets_client, summary_overrides)


if __name__ == "__main__":
    main()
