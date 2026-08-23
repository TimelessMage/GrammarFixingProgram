"""The polishing engine — your original script's logic, adapted for the server.

Changes from the local version:
  - Selenium/Chrome replaced with plain HTTP fetching (chapter URLs follow a
    number pattern, so we build each chapter's URL directly). If a site turns
    out to block this, we add a browser fallback later.
  - Notifications happen on the site itself (job status + library).
  - Progress and the growing txt are saved online after EVERY chapter, so a
    server restart just resumes.
  - current_chapt bugs fixed: one clean chapter counter, resume comes from the
    Google Sheet instead of counting "CHAPTER " in a local file.
  - Filename auto-generated from the novel's URL slug/title (user can override).
"""
import os
import re
import threading
import time
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

import storage


def _keep_awake(stop_event, log):
    """Render's free tier sleeps after ~15 min without web traffic, which would
    kill a running job. While a job is active, ping our own public URL every
    5 minutes so the server stays awake. (RENDER_EXTERNAL_URL is set by Render
    automatically; harmless no-op anywhere else.)"""
    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("SELF_URL", "")
    if not url:
        return
    while not stop_event.wait(300):
        try:
            requests.get(url, timeout=10)
            log("   [keep-awake] self-ping ok")
        except Exception:
            pass

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
MODELS = [
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
]
MAX_EXHAUSTED_WAITS = 6          # 6 x 5 min of "all keys limited" before giving up
WAIT_WHEN_EXHAUSTED = 300
FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

SYSTEM_INSTRUCTION = (
    "You are an expert fiction editor. Your goal is to take rough, potentially "
    "poorly-translated web novel text and rewrite it into high-quality, "
    "professional English prose. Fix all grammatical errors, improve sentence "
    "flow, and use more descriptive vocabulary while maintaining the original "
    "plot and character names. Remove all non-story elements like  "
    " website advertisements. Keep the tone of the original  "
    "CRITICAL: Use frequent line breaks between paragraphs to improve readability."
    "CRITICAL: Carefully analyze the text so as to not mix up gender pronouns, point of view or character names. Maintain the original story's meaning and emotional impact."
    "Focus on making the text read like a modern best-selling novel. Do not add any new content or change the story, just polish the existing text to a high standard."
    "IMPORTANT: Do not include any internal monologue, 'thinking' blocks, "
    "or introductory explanations. Provide ONLY the edited story text, do not include any commentary or notes. Do not add any new content or change the story, just polish the existing text to a high standard."
    "IMPORTANT: do not cut the text short. Do not summarize or condense the story. Keep all original content intact, just polish it."
    "IMPORTANT: Be aware that you are editing a chapter in a story, and the text may contain references to previous chapters."
    "IMPORTANT: If the text contains anything that is blocked by your company's content filter, do not edit those parts and just merge them into the rest of the edited story without changing their content, unless you can edit then without changing the meaning of the text, or the emotional impact of the text."
    "IMPORTANT: If the text contains highly graphic or violent content, do not edit those parts and just merge them into the rest of the edited story without changing their content."
)


# ---------- URL helpers ----------

_CHAPTER_RE = re.compile(r"(\d+)(?=/?$)")


def extract_chapter_number(url):
    m = _CHAPTER_RE.search(url.rstrip("/"))
    return int(m.group(1)) if m else None


def url_for_chapter(start_url, n):
    return _CHAPTER_RE.sub(str(n), start_url.rstrip("/")) + "/"


def novel_key(url):
    """The URL minus the chapter number — identifies the novel for resume."""
    return _CHAPTER_RE.sub("N", url.rstrip("/"))


def guess_title(url):
    """'.../novel/shepherd-wizard/chapter/1/' -> 'Shepherd Wizard - Polished'"""
    parts = [p for p in url.split("/") if p]
    slug = ""
    for i, p in enumerate(parts):
        if p.lower() in ("novel", "book", "series") and i + 1 < len(parts):
            slug = parts[i + 1]
            break
    if not slug:
        skip = ("chapter", "chapters", "read")
        cands = [p for p in parts[2:] if not p.isdigit() and p.lower() not in skip]
        slug = cands[-1] if cands else "novel"
    return slug.replace("-", " ").replace("_", " ").title() + " - Polished"


# ---------- scraping & cleaning (your logic, HTTP instead of Selenium) ----------

BLOCKED_SIGNS = ("just a moment", "verify you are human", "checking your browser", "enable javascript")

# Site data APIs — some novel sites are JS apps whose pages are empty shells,
# but their chapter API returns clean JSON. Trying the API first is faster,
# lighter, and skips Chromium entirely.
_CHIKARI_RE = re.compile(r"^(https?://chikari\.moe)/novels/([^/]+)/(\d+)$")


def _api_fetch(url, log):
    """Returns chapter text via a known site API, or None if no API matches."""
    m = _CHIKARI_RE.match(url.rstrip("/"))
    if not m:
        return None
    base, slug, num = m.groups()
    api_url = f"{base}/api/novels/{slug}/chapters/{num}/read"
    log(f"   [api] fetching {api_url}")
    r = requests.get(api_url, timeout=30, headers={
        **FETCH_HEADERS, "Accept": "application/json", "Referer": url})
    r.raise_for_status()
    data = r.json()
    body = (data.get("body") or "").strip()
    title = data.get("title") or ""
    if data.get("locked"):
        raise RuntimeError(f"Chapter {num} is locked on the site"
                           + (f" ({data.get('lock_reason')})" if data.get("lock_reason") else "") + ".")
    if len(body) < 100:
        return None  # empty/odd response — fall through to the other methods
    return (title + "\n\n" + body) if title else body


def _extract_story(html):
    """Paragraphs first (your original logic); fall back to common story
    containers, then to the whole body, for sites that don't use <p>."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = "\n".join(p.get_text() for p in soup.find_all("p") if len(p.get_text()) > 5)
    if len(text) >= 500:
        return text
    for sel in ("article", "main", "[class*=chapter]", "[class*=content]", "[id*=chapter]"):
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > len(text):
            text = node.get_text("\n", strip=True)
    if len(text) < 500 and soup.body:
        text = soup.body.get_text("\n", strip=True)
    return text


def _looks_like_story(text):
    return len(text) >= 500 and not any(s in text.lower() for s in BLOCKED_SIGNS)


def fetch_chapter_text_browser(url, log):
    """Headless Chromium via Playwright, for JS-rendered sites (e.g. chikari.moe)."""
    from playwright.sync_api import sync_playwright
    log("   [browser] rendering page with headless Chromium...")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(user_agent=FETCH_HEADERS["User-Agent"])
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:  # wait for real text to appear, not just the empty shell
                page.wait_for_function(
                    "document.body && document.body.innerText.length > 800", timeout=20000)
            except Exception:
                pass  # take whatever rendered — extraction below judges it
            html = page.content()
        finally:
            browser.close()
    return _extract_story(html)


def fetch_chapter_text(url, log=print):
    """Site API first (cleanest), then plain HTTP, then headless browser."""
    try:
        text = _api_fetch(url, log)
        if text is not None:
            return text
    except RuntimeError:
        raise  # locked chapter — a real answer, don't mask it with fallbacks
    except Exception as e:
        log(f"   [api] failed ({e}) - trying plain HTTP.")
    try:
        r = requests.get(url, headers=FETCH_HEADERS, timeout=30)
        r.raise_for_status()
        text = _extract_story(r.text)
        if _looks_like_story(text):
            return text
        log("   [fetch] HTTP gave no story text - switching to browser.")
    except Exception as e:
        log(f"   [fetch] HTTP failed ({e}) - switching to browser.")
    text = fetch_chapter_text_browser(url, log)
    if not _looks_like_story(text):
        raise RuntimeError(f"Could not extract story text from {url} even with a browser.")
    return text


def clean_blacklist_text(raw_text):
    raw_text = re.sub(r"<think>.*?</think>", " ", raw_text, flags=re.DOTALL)
    blacklist = [
        "Please follow common sense when posting comments.",
        "Spam, phishing, or any sort of suspicious comment",
        "verification code", "6-digit code", "Staff account detected",
        "© 2025 Light Novel World", "boost your favorite novels",
    ]
    lines = raw_text.splitlines()
    return "\n".join(line for line in lines if not any(junk in line for junk in blacklist))


# ---------- AI editing with key rotation (your hierarchy logic) ----------

def build_providers(keys):
    return [{"name": f"Gemini key {i + 1}",
             "client": OpenAI(api_key=k, base_url=GEMINI_BASE),
             "models": MODELS} for i, k in enumerate(keys)]


def ai_editor(text_block, providers, log):
    if len(text_block.strip()) < 100:
        return ""
    messages = [
        {"role": "system", "content": SYSTEM_INSTRUCTION},
        {"role": "user", "content": f"Please edit this story text for professional flow:\n\n{text_block}"},
    ]
    for provider in providers:
        for model_name in provider["models"]:
            try:
                log(f"   [Trying {provider['name']} - {model_name}]")
                response = provider["client"].chat.completions.create(model=model_name, messages=messages)
                return response.choices[0].message.content
            except Exception as e:
                err = str(e).lower()
                if "429" in err or "rate_limit" in err:
                    log(f"   [!] {provider['name']} ({model_name}) limited.")
                else:
                    log(f"   [!] Error with {provider['name']}: {e}")
                continue
    return None  # every key/model failed this round


# ---------- the job loop ----------

def run_job(job_id, keys):
    log = lambda msg: print(f"[{job_id}] {msg}", flush=True)
    job = storage.get_job(job_id)
    title = job["title"]
    providers = build_providers(keys)

    start_chapter = max(int(job["last_completed"]) + 1, extract_chapter_number(job["start_url"]) or 1)
    total = int(job["total_chapters"])
    file_id = job.get("file_id") or ""
    body = storage.download_file(file_id).decode("utf-8") if file_id else ""

    stay_awake = threading.Event()
    threading.Thread(target=_keep_awake, args=(stay_awake, log), daemon=True).start()

    try:
        exhausted_waits = 0
        chapter = start_chapter
        while chapter <= total:
            log(f"--- Polishing Chapter {chapter} ---")
            raw = fetch_chapter_text(url_for_chapter(job["start_url"], chapter), log)
            polished = ai_editor(clean_blacklist_text(raw), providers, log)

            if polished:
                exhausted_waits = 0
                body += f"\n\nCHAPTER {chapter}\n\n{polished}\n\n" + "-" * 40 + "\n\n"
                file_id = storage.upsert_file(file_id, title, body)
                storage.record_progress(job_id, chapter, file_id)
                log(f"✓ Chapter {chapter} saved.")
                chapter += 1
                time.sleep(4)
            else:
                exhausted_waits += 1
                if exhausted_waits > MAX_EXHAUSTED_WAITS:
                    raise RuntimeError("All API keys stayed rate-limited for too long.")
                log("[!!!] ALL KEYS EXHAUSTED. Waiting 5 minutes...")
                time.sleep(WAIT_WHEN_EXHAUSTED)

        storage.set_job_status(job_id, "done")
        log("Job complete.")

    except Exception as e:
        storage.set_job_status(job_id, "interrupted")
        log(f"Job interrupted: {e}")
    finally:
        stay_awake.set()  # let the server sleep again once no job is running
