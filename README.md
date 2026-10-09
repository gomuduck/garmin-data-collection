# Garmin Data Collection

Automatically collects daily sleep, HRV, recovery, and running data from Garmin Connect, syncs it to Google Sheets, and publishes an interactive sleep consistency chart to GitHub Pages — ready to embed in Notion or any iframe-capable tool.

**Live chart:** `https://<your-username>.github.io/<your-repo>/sleep_consistency.html`

---

## What it does

- Runs daily via GitHub Actions at 8 AM Australia/Brisbane time
- Retries transient failures three times and automatically runs a second recovery pass at 9 AM
- Collects sleep + HRV data for yesterday and today from Garmin Connect
- Collects the last 30 days of running, treadmill, and trail-running activities
- Writes/updates rows in the `Daily`, `Runs`, and `Run Splits` tabs of a Google Sheet
- Regenerates an interactive Plotly chart and publishes it to GitHub Pages

---

## Architecture

```
GitHub Actions (daily, 7 AM UTC)
    │
    ├── collect.py          → Garmin Connect API → Google Sheets (Daily tab)
    ├── collect_runs.py     → Garmin Connect API → Google Sheets (Runs tab)
    ├── collect_run_details.py → Garmin Connect API → Google Sheets (Run Splits tab)
    │
    └── garmin_sleep_consistency.py
            │
            └── garmin_data.py  ← reads from Google Sheets
                    │
                    └── docs/sleep_consistency.html → GitHub Pages
```

The Garmin API is called only by the daily collection steps. All plotting scripts read from Google Sheets so there are no redundant API calls.

---

## Prerequisites

- Python 3.12+
- A Garmin Connect account with a compatible device (tested on Venu 3)
- A Google account
- A GitHub account with GitHub Pages enabled on your repo

---

## Setup guide

### 1. Clone and install

```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Authenticate with Garmin Connect

Run the interactive auth script once locally. This creates a token file at `~/.garminconnect/garmin_tokens.json` that auto-refreshes indefinitely.

```bash
python auth_setup.py
```

You will be prompted for your Garmin email, password, and MFA code (if enabled). If Garmin requires MFA, complete it — subsequent runs will use the saved token without prompting.

### 3. Export the token for GitHub Actions

```bash
python export_token.py
```

Copy the printed base64 string — you'll need it in step 6.

### 4. Set up Google Sheets

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a new project
2. Enable the **Google Sheets API** for the project
3. Go to **IAM & Admin → Service Accounts → Create service account** (no roles needed)
4. On the service account page: **Keys → Add Key → Create new key → JSON** — download the file and save it as `credentials.json` in the project root (it is gitignored)
5. Create a Google Sheet with tabs named `Runs`, `Daily`, `Tests`, and `Weekly`
6. Share the sheet with the service account email (e.g. `name@project.iam.gserviceaccount.com`) — give it **Editor** access
7. Copy the spreadsheet ID from the URL: `docs.google.com/spreadsheets/d/`**`SPREADSHEET_ID`**`/edit`

### 5. Backfill historical data

Run this once to populate your sheet with all past data from a given start date:

Current date here is fine as oldest data is from 2026-02-06

```bash
python backfill.py --start 2026-02-05
```

Skips days with no recorded sleep (watch not worn). Safe to re-run — it clears and rewrites the sheet from scratch.

### 6. Add GitHub Actions secrets

Go to your GitHub repo → **Settings → Secrets and variables → Actions** and create:

| Secret | Value |
|---|---|
| `GARMIN_TOKENS` | output of `python export_token.py` |
| `GARMIN_EMAIL` | your Garmin account email |
| `GARMIN_PASSWORD` | your Garmin account password |
| `GOOGLE_CREDENTIALS_JSON` | full contents of `credentials.json` |
| `SPREADSHEET_ID` | your Google Sheet ID |

`GARMIN_EMAIL` and `GARMIN_PASSWORD` are used only as a fallback if the cached token is invalid or expired. No additional token-maintenance secret is required.

### 7. Enable GitHub Pages

Go to your repo → **Settings → Pages → Source: Deploy from a branch** → Branch: `main`, folder: `/docs`.

The chart will be live at:
```
https://<your-username>.github.io/<your-repo>/sleep_consistency.html
```

### 8. Test the workflow

Trigger a manual run from **Actions → Garmin Data Sync → Run workflow** and verify both steps complete successfully.

### Recovery path

Each collection and chart step retries three times with a short backoff. The
workflow also runs automatically at 9 AM Australia/Brisbane as a recovery pass
for failures at 8 AM. Writes are idempotent: reruns update the same date/run
record, preserve `rpe_1_10`, `shoes`, and `notes`, and do not create duplicate
activities. If both scheduled passes fail, open the failed run under **Actions**
to read the exact error, then use **Run workflow** after correcting the cause.

---

## Project structure

```
.
├── auth_setup.py                  # One-time local Garmin authentication
├── export_token.py                # Exports saved token as base64 for GitHub secret
├── collect.py                     # Daily sync: Garmin → Google Sheets
├── collect_runs.py                # 30-day running activity sync → Runs tab
├── backfill.py                    # One-time historical import
├── garmin_data.py                 # Shared data layer — reads from Google Sheets
├── garmin_sleep_consistency.py    # Generates sleep window chart → docs/
├── docs/
│   ├── index.html                 # Redirects to sleep_consistency.html
│   └── sleep_consistency.html    # Published chart (GitHub Pages)
├── requirements.txt
├── .gitignore                     # Excludes credentials.json, .venv, *.json
├── test_collect_runs.py            # Run normalization and sheet-preservation tests
└── .github/
    └── workflows/
        └── garmin-sync.yml        # Daily GitHub Actions workflow
```

---

## Data collected (Google Sheets — Daily tab)

| Column | Description |
|---|---|
| `date` | Calendar date (local timezone, Australia/Brisbane) |
| `sleep_start_local` | Bedtime in local time |
| `sleep_end_local` | Wake time in local time |
| `total_sleep_seconds` | Total sleep duration |
| `deep/light/rem/awake_seconds` | Sleep stage breakdown |
| `restless_moments` | Number of restless moments |
| `sleep_score` | Garmin overall sleep score (0–100) |
| `sleep_score_qualifier` | EXCELLENT / GOOD / FAIR / POOR |
| `sleep_score_feedback` | Garmin's feedback key |
| `avg_hrv` | Overnight average HRV |
| `hrv_status` | BALANCED / UNBALANCED / etc. |
| `hrv_weekly_avg` | 7-day rolling HRV average |
| `hrv_5min_high` | Best 5-minute HRV of the night |
| `avg/resting_heart_rate` | Sleep and resting heart rate |
| `avg_spo2` / `lowest_spo2` | Blood oxygen saturation |
| `avg_respiration` | Breathing rate during sleep |
| `avg_sleep_stress` | Stress level during sleep |
| `body_battery_change` | Body battery gained overnight |
| `skin_temp_deviation_c` | Skin temperature deviation from baseline |
| `sleep_need_baseline/actual_min` | Garmin's sleep need recommendation |
| `breathing_disruption` | Breathing disruption severity |

---

## Data collected (Google Sheets — Runs tab)

The sync reads the existing row-1 headers before writing. If the Garmin-owned
columns `activity_name`, `calories`, or `elevation_gain_m` are missing, the
workflow appends those headers automatically. It then prints the resolved
column mapping before any activity rows are written.

| Column | Owner | Description |
|---|---|---|
| `date` | Garmin | Activity date in Australia/Brisbane |
| `activity_id` | Garmin | Stable Garmin activity identifier |
| `activity_name` | Garmin | Garmin activity name |
| `garmin_url` | Garmin | Clickable Garmin Connect activity URL |
| `session_type` | Manual/planning | Left blank for new Garmin rows |
| `distance_km` | Garmin | Distance in kilometres |
| `duration_min` | Garmin | Duration in minutes |
| `avg_pace_min_km` | Garmin | Average pace in minutes per kilometre |
| `avg_hr_bpm` | Garmin | Average heart rate |
| `max_hr_bpm` | Garmin | Maximum heart rate |
| `rpe_1_10` | Manual-only | Your perceived effort; never overwritten |
| `cadence_spm` | Garmin | Average running cadence |
| `shoes` | Manual-only | Shoes used; never overwritten |
| `notes` | Manual-only | Training notes; never overwritten |
| `calories` | Garmin | Activity calories |
| `elevation_gain_m` | Garmin | Elevation gain in metres |

Activities are filtered to `running`, `treadmill_running`, and `trail_running`.
Rows are matched using local date plus distance, so rerunning the workflow does
not duplicate the same activity. The stable Garmin activity ID and URL are also
stored for drill-down.

### Run Splits tab

The workflow fetches Garmin lap/split data for the last 30 days and creates the
`Run Splits` tab automatically if needed. It includes distance, duration, pace,
heart rate, cadence, elevation gain, and Garmin's workout segment label when
available. Segment labels such as `warmup`, `interval`, `recovery`, and
`cooldown` are preserved; blank means Garmin did not label that lap, rather than
the sync guessing incorrectly. Rows are matched by activity ID plus split number.
This gives the coach detailed pacing without putting raw GPS points into the
main `Runs` tab; open `garmin_url` for the route map.

## Adding new plots

`garmin_data.py` is the shared data layer. To add a new visualisation:

1. Add a new `fetch_*` function to `garmin_data.py` if you need a different data shape
2. Create a new `garmin_<chart_name>.py` that imports from `garmin_data` and writes to `docs/<chart_name>.html`
3. Add a step to `.github/workflows/garmin-sync.yml` after the existing **Generate charts** step:
   ```yaml
   - name: Generate <chart name>
     run: python garmin_<chart_name>.py
     env:
       GOOGLE_CREDENTIALS_JSON: ${{ secrets.GOOGLE_CREDENTIALS_JSON }}
       SPREADSHEET_ID: ${{ secrets.SPREADSHEET_ID }}
   ```
4. Link to it from `docs/index.html`

---

## Timezone

All dates and times are stored in **Australia/Brisbane** (UTC+10). Python's `zoneinfo` module handles the conversion from Garmin timestamps consistently.

If you are in a different timezone, update `LOCAL_TZ` in [garmin_data.py](garmin_data.py), [collect.py](collect.py), and [collect_runs.py](collect_runs.py).

---

## Token maintenance

Garmin tokens auto-refresh indefinitely as long as they remain valid. If a token is ever invalidated (password change, Garmin session revocation), re-run locally:

```bash
python auth_setup.py
python export_token.py
```

Then update the `GARMIN_TOKENS` secret in GitHub.

---

## Security notes

- `credentials.json` is gitignored — never commit it
- All secrets are stored in GitHub Actions secrets, never in code
- The Google service account only has access to the specific sheet you share with it
- Consider rotating the service account key periodically in Google Cloud Console
