"""Redline backend — FastAPI app for Hugging Face Spaces.

Routes: signup/login, start job, list jobs, download finished file.
State lives in a Google Sheet (users + jobs) and Google Drive (txt files).
"""
import json
import os
import threading
from datetime import datetime, timezone

import requests as http
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel

import keycrypto
import storage
import user_encrypt
from editor import run_job, extract_chapter_number

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    # Lock this down to your GitHub Pages origin once deployed, e.g.
    # FRONTEND_ORIGIN=https://yourname.github.io
    allow_origins=[os.environ.get("FRONTEND_ORIGIN", "*")],
    allow_methods=["*"],
    allow_headers=["*"],
)

signer = URLSafeTimedSerializer(os.environ["SECRET_KEY"], salt="redline-session")
TOKEN_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


# ---------- auth helpers ----------

def make_token(email: str) -> str:
    return signer.dumps(email)


def require_user(authorization: str | None, token_qs: str | None = None) -> str:
    raw = token_qs or (authorization.split(" ", 1)[1] if authorization and " " in authorization else None)
    if not raw:
        raise HTTPException(401, "Your session expired — please sign in again.")
    try:
        return signer.loads(raw, max_age=TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Your session expired — please sign in again.")


class Creds(BaseModel):
    email: str
    password: str


class JobReq(BaseModel):
    start_url: str
    total_chapters: int
    filename: str = ""
    key1: str
    key2: str


# ---------- routes ----------

@app.get("/")
def root():
    return {"ok": True, "app": "redline"}


@app.get("/api/diag/chikari")
def diag_chikari():
    """One-shot check: can THIS server's IP read chikari's chapter API?"""
    url = ("https://chikari.moe/api/novels/"
           "how-to-survive-as-the-second-son-of-a-mage-family/chapters/1/read")
    try:
        r = http.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://chikari.moe/novels/how-to-survive-as-the-second-son-of-a-mage-family/1/"})
        body_chars = len((r.json().get("body") or "")) if r.status_code == 200 else 0
        return {"status": r.status_code, "story_chars": body_chars,
                "verdict": "REACHABLE - jobs can run on this server" if body_chars > 500
                           else "BLOCKED or empty - keep jobs on another machine"}
    except Exception as e:
        return {"status": "error", "detail": str(e), "verdict": "BLOCKED - keep jobs on another machine"}


@app.post("/api/signup")
def signup(c: Creds):
    email = c.email.strip().lower()
    if "@" not in email or len(c.password) < 8:
        raise HTTPException(400, "Enter a valid email and a password of at least 8 characters.")
    if storage.get_user(email):
        raise HTTPException(400, "That email already has an account — sign in instead.")
    storage.create_user(email, user_encrypt.protect_password(c.password))
    return {"token": make_token(email), "email": email}


@app.post("/api/login")
def login(c: Creds):
    email = c.email.strip().lower()
    row = storage.get_user(email)
    if not row or not user_encrypt.verify_password(c.password, row["password_protected"]):
        raise HTTPException(400, "Email or password doesn't match.")
    return {"token": make_token(email), "email": email}


@app.get("/api/me")
def me(authorization: str | None = Header(None)):
    return {"email": require_user(authorization)}


def _dispatch_worker(job_id: str, keys: list):
    """Press GitHub Actions' start button for this job."""
    repo = os.environ["GH_REPO"]          # e.g. "yourname/grammar-fixer"
    r = http.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/polish.yml/dispatches",
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json"},
        json={"ref": "main", "inputs": {"job_id": str(job_id),
                                        "keys_encrypted": keycrypto.encrypt(json.dumps(keys))}},
        timeout=20,
    )
    if r.status_code != 204:
        raise HTTPException(500, f"Couldn't start the worker (GitHub said {r.status_code}). "
                                 "Check the GH_TOKEN and GH_REPO settings.")


def _is_stale(job) -> bool:
    """True if a 'running' job hasn't saved progress in 30+ min (worker died)."""
    try:
        t = datetime.strptime(job["updated_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() > 1800
    except Exception:
        return True


@app.post("/api/jobs")
def start_job(req: JobReq, authorization: str | None = Header(None)):
    email = require_user(authorization)
    if extract_chapter_number(req.start_url) is None:
        raise HTTPException(400, "The link must end with a chapter number, like .../chapter/1/")
    if not (1 <= req.total_chapters <= 5000):
        raise HTTPException(400, "Last chapter number must be between 1 and 5000.")
    if not req.key1:
        raise HTTPException(400, "At least API key 1 is required.")

    job = storage.find_or_create_job(email, req.start_url, req.total_chapters, req.filename)
    if job["status"] == "running" and not _is_stale(job):
        raise HTTPException(400, "That novel is already being polished — check Jobs below.")
    if job["status"] == "done":
        return {"message": "This novel is already complete! Find it in your library below."}

    keys = [k for k in (req.key1, req.key2) if k]
    if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
        _dispatch_worker(job["job_id"], keys)          # run on GitHub Actions
    else:
        threading.Thread(target=run_job, args=(job["job_id"], keys),
                         daemon=True).start()          # run right here on Render
    storage.set_job_status(job["job_id"], "running")

    resumed = int(job["last_completed"]) > 0
    return {"message": ("Resuming from chapter %s. " % (int(job["last_completed"]) + 1) if resumed else "Started! ")
            + "You'll get an email when it finishes — feel free to close this tab."}


@app.get("/api/jobs")
def list_jobs(authorization: str | None = Header(None)):
    email = require_user(authorization)
    return {"jobs": storage.jobs_for(email)}


@app.get("/api/download/{job_id}")
def download(job_id: str, token: str = Query(...)):
    email = require_user(None, token)
    job = storage.get_job(job_id)
    if not job or job["email"] != email:
        raise HTTPException(404, "File not found.")
    if not job.get("file_id"):
        raise HTTPException(404, "No file yet — the first chapter hasn't finished.")
    data = storage.download_file(job["file_id"])
    headers = {"Content-Disposition": 'attachment; filename="%s.txt"' % job["title"].replace('"', "")}
    return StreamingResponse(iter([data]), media_type="text/plain; charset=utf-8", headers=headers)
