"""Google Sheet as the entire data store: users, jobs, AND the polished text.

(Google no longer grants service accounts any Drive storage of their own, so
files can't be uploaded to Drive for free. Instead, each polished chapter is
stored as rows in a 'chapters' worksheet - a cell holds up to 50k characters -
and downloads are stitched together from those rows.)

Worksheets:
  users:    email | password_protected | created_at | keys_encrypted
  jobs:     job_id | email | title | start_url | total_chapters
            | last_completed | status | file_id | updated_at
  chapters: job_id | chapter | part | text

Secrets (environment variables):
  GOOGLE_CREDS_JSON  - full service-account JSON, pasted as one line
  SHEET_ID           - the long id from the Google Sheet's URL
"""
import json
import os
import threading
import uuid
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

_lock = threading.Lock()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDS_JSON"]), scopes=SCOPES)
_gc = gspread.authorize(_creds)
_sheet = _gc.open_by_key(os.environ["SHEET_ID"])

USERS_HEADERS = ["email", "password_protected", "created_at", "keys_encrypted"]
JOBS_HEADERS = ["job_id", "email", "title", "start_url", "total_chapters",
                "last_completed", "status", "file_id", "updated_at"]
CHAP_HEADERS = ["job_id", "chapter", "part", "text"]
CELL_CHARS = 45000   # stay under Sheets' 50k-per-cell limit


def _ws(name, headers):
    try:
        ws = _sheet.worksheet(name)
        if ws.row_values(1) != headers:  # migrate: extend header row for new columns
            ws.update(values=[headers], range_name="A1")
    except gspread.WorksheetNotFound:
        ws = _sheet.add_worksheet(name, rows=1000, cols=len(headers))
        ws.append_row(headers)
    return ws


_users = _ws("users", USERS_HEADERS)
_jobs = _ws("jobs", JOBS_HEADERS)
_chaps = _ws("chapters", CHAP_HEADERS)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------- users ----------

def get_user(email):
    with _lock:
        for r in _users.get_all_records():
            if r["email"] == email:
                return r
    return None


def create_user(email, password_protected):
    with _lock:
        _users.append_row([email, password_protected, _now(), ""])


def set_user_keys(email, keys_encrypted):
    with _lock:
        cell = _users.find(email, in_column=1)
        if cell:
            _users.update_cell(cell.row, USERS_HEADERS.index("keys_encrypted") + 1, keys_encrypted)


# ---------- jobs ----------

def _job_rows():
    return _jobs.get_all_records()


def get_job(job_id):
    with _lock:
        for r in _job_rows():
            if str(r["job_id"]) == job_id:
                return r
    return None


def jobs_for(email):
    with _lock:
        rows = [r for r in _job_rows() if r["email"] == email]
    keep = ["job_id", "title", "total_chapters", "last_completed", "status", "file_id"]
    return [{k: r[k] for k in keep} for r in rows]


def find_or_create_job(email, start_url, total_chapters, filename):
    """Same user + same novel URL (ignoring the chapter number) = same job -> resume."""
    from editor import novel_key, guess_title
    key = novel_key(start_url)
    with _lock:
        for r in _job_rows():
            if r["email"] == email and novel_key(r["start_url"]) == key:
                if int(total_chapters) > int(r["total_chapters"]):
                    _update(r["job_id"], {"total_chapters": total_chapters})
                    r["total_chapters"] = total_chapters
                return r
    title = filename or guess_title(start_url)
    job = {"job_id": uuid.uuid4().hex[:12], "email": email, "title": title,
           "start_url": start_url, "total_chapters": total_chapters,
           "last_completed": 0, "status": "new", "file_id": "", "updated_at": _now()}
    with _lock:
        _jobs.append_row([job[h] for h in JOBS_HEADERS])
    return job


def _update(job_id, fields):
    cell = _jobs.find(str(job_id), in_column=1)
    if not cell:
        return
    fields["updated_at"] = _now()
    for k, v in fields.items():
        _jobs.update_cell(cell.row, JOBS_HEADERS.index(k) + 1, str(v))


def set_job_status(job_id, status):
    with _lock:
        _update(job_id, {"status": status})


def record_progress(job_id, last_completed):
    with _lock:
        _update(job_id, {"last_completed": last_completed, "file_id": "sheet"})


def set_title(job_id, title):
    with _lock:
        _update(job_id, {"title": title})


# ---------- chapter text (replaces Drive files) ----------

def save_chapter(job_id, chapter, text):
    """One polished chapter -> one or more rows (split to fit the cell limit)."""
    parts = [text[i:i + CELL_CHARS] for i in range(0, len(text), CELL_CHARS)] or [""]
    with _lock:
        _chaps.append_rows([[str(job_id), int(chapter), p_i, part]
                            for p_i, part in enumerate(parts)])


def read_novel(job_id):
    """Stitch every saved chapter of a job into the final txt."""
    with _lock:
        rows = [r for r in _chaps.get_all_records() if str(r["job_id"]) == str(job_id)]
    merged = {}  # (chapter, part) -> text; later rows win, so retries can't duplicate
    for r in rows:
        merged[(int(r["chapter"]), int(r["part"]))] = r["text"]
    chapters = {}
    for (chap, part), text in sorted(merged.items()):
        chapters.setdefault(chap, []).append(text)
    out = []
    for chap in sorted(chapters):
        out.append(f"\n\nCHAPTER {chap}\n\n{''.join(chapters[chap])}\n\n" + "-" * 40 + "\n\n")
    return "".join(out)
