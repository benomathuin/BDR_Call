# BDR Call Analytics — AirCall → Google Sheets

Automated sync of AirCall call data, transcripts, and recording URLs to Google Sheets.

## Setup

### 1. GitHub Secrets

Add these secrets in **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `AIRCALL_API_ID` | Your AirCall API ID |
| `AIRCALL_API_TOKEN` | Your AirCall API Token |
| `GOOGLE_SERVICE_ACCOUNT` | Base64-encoded Google service account JSON (see below) |
| `GSHEET_ID` | Google Sheet ID (from the URL) |
| `GSHEET_TAB` | Sheet tab name (default: `claude`) |

### 2. Google Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create or select a project
3. Enable **Google Sheets API** and **Google Drive API**
4. Create a **Service Account** under Credentials
5. Download the JSON key file
6. Base64 encode it: `base64 -i service_account.json | tr -d '\n'`
7. Paste the result as the `GOOGLE_SERVICE_ACCOUNT` secret
8. Share your Google Sheet with the service account email (`client_email` in the JSON)

### 3. Workflows

- **Daily Sync** — Runs automatically at 4:00 AM UTC. Fetches yesterday's calls and appends to the sheet.
- **Backfill** — Manual trigger. Run from Actions tab to backfill Nov 2025 → Apr 6 2026.

## Columns

| Column | Description |
|---|---|
| `call_id` | AirCall call ID |
| `call_direction` | inbound / outbound |
| `started_at` | Call start timestamp |
| `ended_at` | Call end timestamp |
| `call_month` | YYYY-MM |
| `duration_seconds` | Total call duration |
| `aircall_user_id` | Agent user ID |
| `aircall_user_name` | Agent name |
| `phone_number` | Number dialed/received |
| `company_name` | Contact company |
| `aircall_contact_id` | AirCall contact ID |
| `answered_status` | answered / voicemail / no_answer / missed:reason |
| `transcript_status` | available / not_available / empty / too_short / not_applicable |
| `transcript_text` | Full transcript with speaker labels |
| `recording_url` | Pre-signed S3 recording URL |
