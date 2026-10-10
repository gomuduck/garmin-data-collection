from datetime import datetime
from zoneinfo import ZoneInfo

from collect_run_details import compute_accurate_splits, normalize_split, summarize_segments
from collect_runs import normalize_activity, sync_runs


def test_normalize_activity_converts_units_and_brisbane_date() -> None:
    activity = {
        "activityType": {"typeKey": "running"},
        "activityName": "Threshold",
        "startTimeGMT": datetime(2026, 10, 8, 14, 30, tzinfo=ZoneInfo("UTC")).timestamp(),
        "distance": 10000,
        "duration": 2700,
        "averageHR": 165,
        "maxHR": 178,
        "averageRunCadence": 182,
        "calories": 700,
        "elevationGain": 45,
    }

    assert normalize_activity(activity) == {
        "date": "2026-10-09",
        "activity_id": "",
        "activity_name": "Threshold",
        "garmin_url": "",
        "session_type": "",
        "distance_km": 10.0,
        "duration_min": 45.0,
        "avg_pace_min_km": 4.5,
        "pace_source": "garmin_summary",
        "avg_hr_bpm": 165,
        "max_hr_bpm": 178,
        "cadence_spm": 182,
        "calories": 700,
        "elevation_gain_m": 45,
    }


def test_normalize_activity_ignores_non_running_and_invalid_distance() -> None:
    assert normalize_activity({"activityType": {"typeKey": "cycling"}}) is None
    assert normalize_activity({"activityType": {"typeKey": "running"}, "distance": 0, "duration": 10}) is None


class FakeSheet:
    def __init__(self) -> None:
        self.rows = [["date", "session_type", "distance_km", "duration_min", "avg_pace_min_km", "avg_hr_bpm", "max_hr_bpm", "rpe_1_10", "cadence_spm", "shoes", "notes"]]

    def row_values(self, row: int) -> list[str]:
        return self.rows[row - 1]

    def get_all_values(self) -> list[list[str]]:
        return self.rows

    def update(self, _range: str, values: list[list[str | float | int]]) -> None:
        if _range == "A1":
            self.rows[0] = [str(value) for value in values[0]]
        else:
            row_number = int(_range[1:])
            self.rows[row_number - 1] = [str(value) for value in values[0]]

    def append_row(self, values: list[str | float | int]) -> None:
        self.rows.append([str(value) for value in values])


def test_sync_runs_preserves_manual_fields_and_is_idempotent() -> None:
    sheet = FakeSheet()
    sheet.rows.append(["2026-10-09", "", "10.0", "45.0", "4.5", "160", "175", "8", "180", "Shoes A", "Good threshold"])
    activity = normalize_activity({
        "activityType": {"typeKey": "running"},
        "startTimeGMT": "2026-10-08T14:30:00",
        "distance": 10000,
        "duration": 2700,
        "averageHR": 165,
        "maxHR": 178,
        "averageRunCadence": 182,
    })
    assert activity is not None

    sync_runs(sheet, [activity])

    assert len(sheet.rows) == 2
    assert sheet.rows[1][7:11] == ["8", "182", "Shoes A", "Good threshold"]
    assert sheet.rows[1][5:7] == ["165", "178"]


def test_normalize_split_keeps_garmin_lap_metrics_and_segment_label() -> None:
    split = normalize_split(
        activity_id="123",
        activity_date="2026-10-09",
        activity_name="Threshold",
        split_number=2,
        raw={
            "distance": 1000,
            "duration": 270,
            "averageHR": 168,
            "maxHR": 176,
            "averageRunCadence": 184,
            "elevationGain": 4,
            "purposeTypeKey": "INTERVAL",
        },
    )

    assert split == {
        "activity_id": "123",
        "date": "2026-10-09",
        "activity_name": "Threshold",
        "split_number": 2,
        "segment_type": "interval",
        "distance_km": 1.0,
        "duration_min": 4.5,
        "pace_min_km": 4.5,
        "avg_hr_bpm": 168,
        "max_hr_bpm": 176,
        "cadence_spm": 184,
        "elevation_gain_m": 4,
        "split_source": "garmin_lap",
        "is_partial": False,
    }


def test_compute_accurate_splits_interpolates_gps_distance_boundaries() -> None:
    details = {
        "metricDescriptors": [
            {"key": "sumDuration", "metricsIndex": 0},
            {"key": "sumDistance", "metricsIndex": 1},
        ],
        "activityDetailMetrics": [
            {"metrics": [0, 0]},
            {"metrics": [300, 1100]},
            {"metrics": [570, 2000]},
        ],
    }

    splits = compute_accurate_splits(details, "123", "2026-10-09", "Test run")

    assert [split["split_number"] for split in splits] == [1, 2]
    assert [split["duration_min"] for split in splits] == [4.55, 4.95]
    assert splits[0]["split_source"] == "gps_sensor"


def test_summarize_segments_exposes_labeled_work_blocks() -> None:
    summary = summarize_segments([
        {"segment_type": "warmup", "distance_km": 2.0, "duration_min": 12.0},
        {"segment_type": "work", "distance_km": 5.0, "duration_min": 25.0},
        {"segment_type": "cooldown", "distance_km": 1.0, "duration_min": 6.0},
    ])
    assert summary["detail_split_count"] == 3
    assert summary["detail_avg_pace_min_km"] == 5.38
    assert summary["warmup_distance_km"] == 2.0
    assert summary["work_duration_min"] == 25.0
    assert summary["cooldown_distance_km"] == 1.0
