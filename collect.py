import os
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from garminconnect import Garmin
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

TOKEN_STORE = os.path.expanduser("~/.garminconnect")
LOCAL_TZ = ZoneInfo("Australia/Brisbane")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
SHEET_NAME = "Daily"


def ms_gmt_to_local_str(ts_ms: int | None) -> str | None:
    """Convert a GMT millisecond timestamp to a local datetime string."""
    if ts_ms is None:
        return None
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def collect_sleep_row(client: Garmin, target_date: date) -> dict:
    date_str = target_date.isoformat()
    sleep_data = client.get_sleep_data(date_str)
    hrv_data = client.get_hrv_data(date_str)

    dto = sleep_data.get("dailySleepDTO") or {}
    scores = dto.get("sleepScores") or {}
    sleep_need = dto.get("sleepNeed") or {}
    hrv_summary = hrv_data.get("hrvSummary") or {}

    return {
        "date": date_str,
        "sleep_start_local": ms_gmt_to_local_str(dto.get("sleepStartTimestampGMT")),
        "sleep_end_local": ms_gmt_to_local_str(dto.get("sleepEndTimestampGMT")),
        "total_sleep_seconds": dto.get("sleepTimeSeconds"),
        "deep_sleep_seconds": dto.get("deepSleepSeconds"),
        "light_sleep_seconds": dto.get("lightSleepSeconds"),
        "rem_sleep_seconds": dto.get("remSleepSeconds"),
        "awake_seconds": dto.get("awakeSleepSeconds"),
        "restless_moments": sleep_data.get("restlessMomentsCount"),
        "sleep_score": (scores.get("overall") or {}).get("value"),
        "sleep_score_qualifier": (scores.get("overall") or {}).get("qualifierKey"),
        "sleep_score_feedback": dto.get("sleepScoreFeedback"),
        "avg_hrv": sleep_data.get("avgOvernightHrv"),
        "hrv_status": sleep_data.get("hrvStatus"),
        "hrv_weekly_avg": hrv_summary.get("weeklyAvg"),
        "hrv_5min_high": hrv_summary.get("lastNight5MinHigh"),
        "avg_heart_rate": dto.get("avgHeartRate"),
        "resting_heart_rate": sleep_data.get("restingHeartRate"),
        "avg_spo2": dto.get("averageSpO2Value"),
        "lowest_spo2": dto.get("lowestSpO2Value"),
        "avg_respiration": dto.get("averageRespirationValue"),
        "avg_sleep_stress": dto.get("avgSleepStress"),
        "body_battery_change": sleep_data.get("bodyBatteryChange"),
        "skin_temp_deviation_c": sleep_data.get("avgSkinTempDeviationC"),
        "sleep_need_baseline_min": sleep_need.get("baseline"),
        "sleep_need_actual_min": sleep_need.get("actual"),
        "breathing_disruption": dto.get("breathingDisruptionSeverity"),
    }


def get_sheets_client() -> gspread.Client:
    """Build a gspread client from a local credentials file or an env var (CI)."""
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)

    return gspread.authorize(creds)


def upsert_rows(sheet: gspread.Worksheet, rows: list[dict]) -> None:
    """Write rows to the sheet, updating existing dates and appending new ones."""
    headers = list(rows[0].keys())

    existing = sheet.get_all_values()
    if not existing:
        sheet.append_row(headers)
        existing_dates = {}
    else:
        # Row 1 is the header, data starts at row 2 (index 1)
        existing_dates = {
            row[0]: idx + 2
            for idx, row in enumerate(existing[1:])
            if row
        }

    for row in rows:
        values = [row.get(h, "") for h in headers]
        row_date = row["date"]

        if row_date in existing_dates:
            row_num = existing_dates[row_date]
            sheet.update(f"A{row_num}", [values])
            print(f"  Updated row for {row_date}")
        else:
            sheet.append_row(values)
            print(f"  Appended row for {row_date}")


def main():
    # --- Garmin ---
    client = Garmin(
        email=os.environ.get("GARMIN_EMAIL"),
        password=os.environ.get("GARMIN_PASSWORD"),
    )
    client.login(TOKEN_STORE)
    print(f"Authenticated as: {client.get_full_name()}\n")

    today = datetime.now(LOCAL_TZ).date()
    yesterday = today - timedelta(days=1)

    rows = []
    for target_date in [yesterday, today]:
        print(f"Collecting sleep data for {target_date}...")
        try:
            row = collect_sleep_row(client, target_date)
            rows.append(row)
            print(f"  sleep_score={row['sleep_score']}, avg_hrv={row['avg_hrv']}, total_sleep={row['total_sleep_seconds']}s")
        except Exception as e:
            print(f"  Failed for {target_date}: {e}")

    if not rows:
        print("No rows collected, exiting.")
        return

    # --- Google Sheets ---
    print(f"\nWriting {len(rows)} rows to Google Sheets...")
    gc = get_sheets_client()
    sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    upsert_rows(sheet, rows)
    print("Done.")


if __name__ == "__main__":
    main()
