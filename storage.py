"""Google Sheet as database + Google Drive as file storage.

Sheet has two worksheets:
  users: email | password_protected | created_at
  jobs:  job_id | email | title | start_url | total_chapters | last_completed | status | file_id | updated_at

Secrets (environment variables on the Space):
  GOOGLE_CREDS_JSON  - full service-account JSON, pasted as one line
  SHEET_ID           - the long id from the Google Sheet's URL
  DRIVE_FOLDER_ID    - the id from the shared Drive folder's URL
"""
import io
import json
import os
import threading
import uuid
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

_lock = threading.Lock()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
_creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDS_JSON"]), scopes=SCOPES)
_gc = gspread.authorize(_creds)
_sheet = _gc.open_by_key(os.environ["SHEET_ID"])
_drive = build("drive", "v3", credentials=_creds)
FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]

USERS_HEADERS = ["email", "password_protected", "created_at"]
JOBS_HEADERS = ["job_id", "email", "title", "start_url", "total_chapters",
                "last_completed", "status", "file_id", "updated_at"]


def _ws(name, headers):
    try:
        ws = _sheet.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = _sheet.add_worksheet(name, rows=1000, cols=len(headers))
        ws.append_row(headers)
    return ws


_users = _ws("users", USERS_HEADERS)
_jobs = _ws("jobs", JOBS_HEADERS)


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
        _users.append_row([email, password_protected, _now()])


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


def record_progress(job_id, last_completed, file_id):
    with _lock:
        _update(job_id, {"last_completed": last_completed, "file_id": file_id})


def set_title(job_id, title):
    with _lock:
        _update(job_id, {"title": title})


# ---------- Drive files ----------

def upsert_file(file_id, name, text):
    """Create the txt on first save, overwrite it afterwards. Returns file id."""
    media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype="text/plain", resumable=False)
    if file_id:
        _drive.files().update(fileId=file_id, media_body=media).execute()
        return file_id
    meta = {"name": f"{name}.txt", "parents": [FOLDER_ID]}
    f = _drive.files().create(body=meta, media_body=media, fields="id").execute()
    return f["id"]


def download_file(file_id) -> bytes:
    req = _drive.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()
