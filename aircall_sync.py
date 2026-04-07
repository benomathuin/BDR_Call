#!/usr/bin/env python3
"""
AirCall → Google Sheets Sync (GitHub Actions version)
Fetches calls + transcripts + recording URLs from AirCall API
and pushes to Google Sheets.

Modes:
  python aircall_sync.py --backfill                    # Nov 2025 → Apr 6 2026
  python aircall_sync.py --daily                       # Yesterday's data
  python aircall_sync.py --from-date 2026-03-01 --to-date 2026-03-31

Environment variables (set as GitHub Secrets):
  AIRCALL_API_ID          AirCall API ID
  AIRCALL_API_TOKEN       AirCall API Token
  GOOGLE_SERVICE_ACCOUNT  Google service account JSON (base64 encoded)
  GSHEET_ID               Google Sheet ID
  GSHEET_TAB              Google Sheet tab name (default: claude)
"""

import json
import csv
import time
import os
import sys
import argparse
import urllib.request
import urllib.error
import base64
import tempfile
from datetime import datetime, timedelta

# Config from environment
API_ID = os.environ.get("AIRCALL_API_ID", "")
API_TOKEN = os.environ.get("AIRCALL_API_TOKEN", "")
SHEET_ID = os.environ.get("GSHEET_ID", "1MPOHz-8Np_FW7BvTdVdOulvebHgfcOH7poyXx15B6kQ")
SHEET_TAB = os.environ.get("GSHEET_TAB", "claude")

# Rate limiting: AirCall allows 60 req/min
REQUEST_INTERVAL = 1.1
TRANSCRIPT_INTERVAL = 1.1

FIELDNAMES = [
    "call_id", "call_direction", "started_at", "ended_at",
    "call_month", "duration_seconds", "aircall_user_id",
    "aircall_user_name", "phone_number", "company_name",
    "aircall_contact_id", "answered_status", "transcript_status",
    "transcript_text", "recording_url"
]


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def aircall_get(endpoint, params=None, retries=2):
    """Make authenticated GET request to AirCall API using urllib."""
    url = f"https://api.aircall.io/v1/{endpoint}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += f"?{qs}"

    auth_string = base64.b64encode(f"{API_ID}:{API_TOKEN}".encode()).decode()

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Basic {auth_string}")
            req.add_header("Accept", "application/json")

            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode())

            # Handle rate limiting
            if data.get("message") == "TooManyRequest" or data.get("details") == "RateLimitExceeded":
                if attempt < retries:
                    log("  RATE LIMITED — waiting 65s...")
                    time.sleep(65)
                    continue
                log("  RATE LIMITED — exhausted retries")
                return None

            return data

        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt < retries:
                    log("  HTTP 429 RATE LIMITED — waiting 65s...")
                    time.sleep(65)
                    continue
                return None
            elif e.code == 404:
                return {"error": "NotFound"}
            else:
                log(f"  HTTP error {e.code}: {e.reason}")
                return None
        except urllib.error.URLError as e:
            log(f"  URL error: {e.reason}")
            return None
        except Exception as e:
            log(f"  Request error: {e}")
            return None

    return None


def fetch_calls_for_range(from_ts, to_ts):
    """Fetch all calls within a date range, handling pagination."""
    all_calls = []
    page = 1

    while True:
        params = {
            "from": str(from_ts),
            "to": str(to_ts),
            "order": "asc",
            "per_page": "50",
            "page": str(page),
            "fetch_contact": "true",
        }

        data = aircall_get("calls", params)
        if not data or "calls" not in data:
            break

        calls = data["calls"]
        all_calls.extend(calls)

        meta = data.get("meta", {})
        total = meta.get("total", 0)
        log(f"  Page {page}: {len(calls)} calls (total: {len(all_calls)}/{total})")

        if not meta.get("next_page_link"):
            break

        page += 1
        time.sleep(REQUEST_INTERVAL)

    return all_calls


def fetch_transcript(call_id):
    """Fetch transcript for a single call."""
    data = aircall_get(f"calls/{call_id}/transcription")
    if not data or data.get("error") or data.get("status") == 404:
        return None
    return data


def extract_transcript_text(transcription_data):
    """Extract speaker-labelled text from AirCall transcription response."""
    if not transcription_data:
        return ""

    lines = []
    utterances = None

    if "transcription" in transcription_data:
        trans = transcription_data["transcription"]
        if isinstance(trans, dict):
            content = trans.get("content")
            if isinstance(content, dict):
                utterances = content.get("utterances")
            if not utterances:
                utterances = (trans.get("segments") or trans.get("turns") or
                              trans.get("utterances"))

    if not utterances:
        utterances = (transcription_data.get("segments") or
                      transcription_data.get("turns") or
                      transcription_data.get("utterances"))

    if utterances and isinstance(utterances, list):
        for utt in utterances:
            participant = utt.get("participant_type", "")
            if participant == "internal":
                speaker = "Agent"
            elif participant == "external":
                speaker = "Contact"
            else:
                speaker = (utt.get("speaker") or utt.get("speaker_name") or
                           utt.get("role") or "Unknown")
            text = utt.get("text") or utt.get("content") or utt.get("transcript") or ""
            if not text and "sentences" in utt:
                text = " ".join(s.get("text", "") for s in utt["sentences"])
            if text.strip():
                lines.append(f"[{speaker}]: {text.strip()}")

    if not lines:
        plain = transcription_data.get("text") or transcription_data.get("transcript")
        if plain and isinstance(plain, str):
            lines = [plain]

    return "\n".join(lines)


def format_ts(unix_ts):
    if not unix_ts:
        return ""
    return datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")


def get_call_month(unix_ts):
    if not unix_ts:
        return ""
    return datetime.fromtimestamp(unix_ts).strftime("%Y-%m")


def process_call(call):
    """Process a single raw call into a structured row dict."""
    user = call.get("user") or {}
    contact = call.get("contact") or {}

    if call.get("answered_at"):
        answered_status = "answered"
    elif call.get("voicemail"):
        answered_status = "voicemail"
    elif call.get("missed_call_reason"):
        answered_status = f"missed:{call['missed_call_reason']}"
    else:
        answered_status = "no_answer"

    contact_name = ""
    if contact:
        first = contact.get("first_name", "")
        last = contact.get("last_name", "")
        contact_name = f"{first} {last}".strip()

    recording_url = call.get("recording", "")

    return {
        "call_id": call["id"],
        "call_direction": call.get("direction", ""),
        "started_at": format_ts(call.get("started_at")),
        "ended_at": format_ts(call.get("ended_at")),
        "call_month": get_call_month(call.get("started_at")),
        "duration_seconds": call.get("duration", 0),
        "aircall_user_id": user.get("id", ""),
        "aircall_user_name": user.get("name", "Unassigned"),
        "phone_number": call.get("raw_digits", ""),
        "company_name": contact.get("company_name", ""),
        "aircall_contact_id": contact.get("id", ""),
        "answered_status": answered_status,
        "transcript_status": "",
        "transcript_text": "",
        "recording_url": recording_url,
    }


def fetch_transcripts_for_rows(rows):
    """Fetch transcripts for all answered calls with sufficient duration."""
    transcript_count = 0
    eligible = [(i, r) for i, r in enumerate(rows)
                if r["answered_status"] == "answered" and r["duration_seconds"] > 10]

    log(f"Fetching transcripts for {len(eligible)} eligible calls...")

    for idx, (i, row) in enumerate(eligible):
        call_id = row["call_id"]

        trans_data = fetch_transcript(call_id)
        if trans_data:
            text = extract_transcript_text(trans_data)
            if text:
                row["transcript_status"] = "available"
                row["transcript_text"] = text
                transcript_count += 1
            else:
                row["transcript_status"] = "empty"
        else:
            row["transcript_status"] = "not_available"

        if (idx + 1) % 50 == 0:
            log(f"  [{idx+1}/{len(eligible)}] Transcripts found so far: {transcript_count}")

        time.sleep(TRANSCRIPT_INTERVAL)

    for row in rows:
        if not row["transcript_status"]:
            if row["answered_status"] == "answered":
                row["transcript_status"] = "too_short"
            else:
                row["transcript_status"] = "not_applicable"

    log(f"Transcripts found: {transcript_count}/{len(eligible)}")
    return transcript_count


def generate_monthly_ranges(from_date, to_date):
    """Split a date range into monthly chunks to avoid AirCall's 10k limit."""
    ranges = []
    current = from_date

    while current < to_date:
        month_end = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        range_end = min(month_end, to_date)
        ranges.append((current, range_end))
        current = month_end

    return ranges


def setup_google_creds():
    """Decode base64 service account JSON from env var and write to temp file."""
    sa_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
    if not sa_b64:
        log("WARNING: GOOGLE_SERVICE_ACCOUNT not set, skipping Sheets push")
        return None

    try:
        sa_json = base64.b64decode(sa_b64).decode()
        # Validate it's valid JSON
        json.loads(sa_json)

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tmp.write(sa_json)
        tmp.close()
        return tmp.name
    except Exception as e:
        log(f"ERROR: Failed to decode service account: {e}")
        return None


def push_to_gsheets(rows, mode="replace"):
    """Push rows to Google Sheets using gspread."""
    try:
        import gspread
    except ImportError:
        log("ERROR: gspread not installed")
        return False

    creds_file = setup_google_creds()
    if not creds_file:
        return False

    try:
        gc = gspread.service_account(filename=creds_file)
        sh = gc.open_by_key(SHEET_ID)

        try:
            worksheet = sh.worksheet(SHEET_TAB)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = sh.add_worksheet(title=SHEET_TAB, rows=max(len(rows) + 10, 1000), cols=len(FIELDNAMES))

        # Truncate transcript text to avoid Google Sheets 50k char cell limit
        for row in rows:
            if len(row.get("transcript_text", "")) > 49000:
                row["transcript_text"] = row["transcript_text"][:49000] + "\n[TRUNCATED]"

        values = [FIELDNAMES]
        for row in rows:
            values.append([str(row.get(f, "")) for f in FIELDNAMES])

        if mode == "replace":
            worksheet.clear()
            # Batch update in chunks of 5000 rows to avoid API limits
            chunk_size = 5000
            for i in range(0, len(values), chunk_size):
                chunk = values[i:i + chunk_size]
                start_row = i + 1
                worksheet.update(range_name=f"A{start_row}", values=chunk)
                if i + chunk_size < len(values):
                    time.sleep(2)
            log(f"Replaced sheet '{SHEET_TAB}' with {len(rows)} rows")
        elif mode == "append":
            existing = worksheet.get_all_values()
            if not existing:
                worksheet.update(range_name="A1", values=values)
            else:
                data_rows = values[1:]  # skip header
                chunk_size = 5000
                for i in range(0, len(data_rows), chunk_size):
                    chunk = data_rows[i:i + chunk_size]
                    worksheet.append_rows(chunk, value_input_option="RAW")
                    if i + chunk_size < len(data_rows):
                        time.sleep(2)
            log(f"Appended {len(rows)} rows to sheet '{SHEET_TAB}'")

        return True

    except Exception as e:
        log(f"ERROR: Google Sheets push failed: {e}")
        return False
    finally:
        if creds_file and os.path.exists(creds_file):
            os.unlink(creds_file)


def run_backfill():
    """Backfill from Nov 2025 to Apr 6 2026."""
    from_date = datetime(2025, 11, 1)
    to_date = datetime(2026, 4, 6, 23, 59, 59)

    monthly_ranges = generate_monthly_ranges(from_date, to_date)
    all_rows = []

    log(f"{'='*70}")
    log(f"AIRCALL BACKFILL: {from_date.strftime('%Y-%m-%d')} -> {to_date.strftime('%Y-%m-%d')}")
    log(f"Monthly chunks: {len(monthly_ranges)}")
    log(f"{'='*70}")

    for chunk_idx, (chunk_start, chunk_end) in enumerate(monthly_ranges):
        chunk_label = f"{chunk_start.strftime('%Y-%m-%d')} -> {chunk_end.strftime('%Y-%m-%d')}"
        log(f"\n[{chunk_idx+1}/{len(monthly_ranges)}] {chunk_label}")
        log(f"{'─'*50}")

        from_ts = int(chunk_start.timestamp())
        to_ts = int(chunk_end.timestamp())

        log("Fetching calls...")
        raw_calls = fetch_calls_for_range(from_ts, to_ts)
        log(f"Fetched {len(raw_calls)} calls")

        rows = [process_call(c) for c in raw_calls]
        fetch_transcripts_for_rows(rows)
        all_rows.extend(rows)

        log(f"Chunk complete: {len(rows)} calls (running total: {len(all_rows)})")

    # Push to Google Sheets
    log("\nPushing to Google Sheets...")
    push_to_gsheets(all_rows, mode="replace")

    # Summary
    log(f"\n{'='*70}")
    log(f"BACKFILL COMPLETE")
    log(f"{'='*70}")
    log(f"Total calls: {len(all_rows)}")
    answered = sum(1 for r in all_rows if r["answered_status"] == "answered")
    transcripts = sum(1 for r in all_rows if r["transcript_status"] == "available")
    recordings = sum(1 for r in all_rows if r["recording_url"])
    log(f"Answered: {answered}")
    log(f"With transcripts: {transcripts}")
    log(f"With recordings: {recordings}")


def run_daily():
    """Fetch yesterday's calls and append to Google Sheets."""
    yesterday = datetime.now() - timedelta(days=1)
    from_date = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    to_date = from_date + timedelta(days=1) - timedelta(seconds=1)

    from_ts = int(from_date.timestamp())
    to_ts = int(to_date.timestamp())

    log(f"{'='*70}")
    log(f"AIRCALL DAILY SYNC: {from_date.strftime('%Y-%m-%d')}")
    log(f"{'='*70}")

    log("Fetching calls...")
    raw_calls = fetch_calls_for_range(from_ts, to_ts)
    log(f"Fetched {len(raw_calls)} calls")

    if not raw_calls:
        log("No calls found for yesterday. Nothing to sync.")
        return

    rows = [process_call(c) for c in raw_calls]
    fetch_transcripts_for_rows(rows)

    # Push to Google Sheets
    log("\nAppending to Google Sheets...")
    push_to_gsheets(rows, mode="append")

    log(f"\n{'='*70}")
    log(f"DAILY SYNC COMPLETE: {from_date.strftime('%Y-%m-%d')}")
    log(f"{'='*70}")
    log(f"Calls: {len(rows)}")
    answered = sum(1 for r in rows if r["answered_status"] == "answered")
    transcripts = sum(1 for r in rows if r["transcript_status"] == "available")
    log(f"Answered: {answered}, With transcripts: {transcripts}")


def run_custom_range(from_date_str, to_date_str):
    """Fetch calls for a custom date range."""
    from_date = datetime.strptime(from_date_str, "%Y-%m-%d")
    to_date = datetime.strptime(to_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    monthly_ranges = generate_monthly_ranges(from_date, to_date)
    all_rows = []

    log(f"{'='*70}")
    log(f"AIRCALL CUSTOM RANGE: {from_date_str} -> {to_date_str}")
    log(f"{'='*70}")

    for chunk_idx, (chunk_start, chunk_end) in enumerate(monthly_ranges):
        chunk_label = f"{chunk_start.strftime('%Y-%m-%d')} -> {chunk_end.strftime('%Y-%m-%d')}"
        log(f"\n[{chunk_idx+1}/{len(monthly_ranges)}] {chunk_label}")

        from_ts = int(chunk_start.timestamp())
        to_ts = int(chunk_end.timestamp())

        raw_calls = fetch_calls_for_range(from_ts, to_ts)
        log(f"Fetched {len(raw_calls)} calls")

        rows = [process_call(c) for c in raw_calls]
        fetch_transcripts_for_rows(rows)
        all_rows.extend(rows)

    log("\nPushing to Google Sheets...")
    push_to_gsheets(all_rows, mode="replace")

    log(f"\nTotal: {len(all_rows)} calls")


def main():
    parser = argparse.ArgumentParser(description="AirCall -> Google Sheets sync")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backfill", action="store_true",
                       help="Backfill Nov 2025 -> Apr 6 2026")
    group.add_argument("--daily", action="store_true",
                       help="Fetch yesterday's data")
    group.add_argument("--from-date",
                       help="Custom range start (YYYY-MM-DD), requires --to-date")
    parser.add_argument("--to-date", help="Custom range end (YYYY-MM-DD)")
    args = parser.parse_args()

    if not API_ID or not API_TOKEN:
        log("ERROR: AIRCALL_API_ID and AIRCALL_API_TOKEN must be set")
        sys.exit(1)

    if args.backfill:
        run_backfill()
    elif args.daily:
        run_daily()
    elif args.from_date:
        if not args.to_date:
            parser.error("--from-date requires --to-date")
        run_custom_range(args.from_date, args.to_date)


if __name__ == "__main__":
    main()
