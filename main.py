#!/usr/bin/env python3
"""
DECA ASSISTANT SERVER (runs on Railway)  -  version 0.3.0, stage 1 of 4

Routes:
  GET  /        a short note. Nothing private on this page.
  GET  /health  JSON: ok, version, time, storage, which services are set up, admin password check.
                It never shows any secret values.
  GET  /status  the same information as a readable page, with what to fix.
  GET  /privacy what the app stores and why.
  GET  /admin   admin page (browser sign-in: any username + ADMIN_PASSWORD). Not linked from anywhere.

Coming in later stages: /slack/events and /slack/actions (Slack), /google/callback (Google),
and the advisor's web page (forms, deadlines, attendance, budget, sponsors, tasks).

Standard library only, so there is nothing to install (see requirements.txt).
"""
import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "0.3.0"
WHATS_NEW = [
    "The server now runs the whole app, the way Ironside does. It lives on Railway and keeps data on a Volume.",
    "Health check, status page, admin page and privacy page. None of them show secrets.",
    "Stage 1 of 4. Slack, Google and the advisor's web page come in the next stages.",
]


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


# ---------------------------------------------------------------- settings (read from Railway Variables)
PUBLIC_URL = env("PUBLIC_URL").rstrip("/")
ADMIN_PASSWORD = env("ADMIN_PASSWORD")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
AI_MODEL = env("AI_MODEL", "claude-haiku-5-5")
TIMEZONE = env("TIMEZONE", "America/Chicago")
SLACK_BOT_TOKEN = env("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = env("SLACK_SIGNING_SECRET")
ADVISOR_SLACK_IDS = [x for x in re.split(r"[,\s]+", env("ADVISOR_SLACK_IDS")) if x]
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET")
DATA_DIR = Path(env("DATA_DIR") or ("/data" if os.path.isdir("/data") else "data"))
MIN_ADMIN_LENGTH = 12


def config_problems():
    """Plain-English mistakes to fix. Shows the reason, never the value."""
    out = []
    if not PUBLIC_URL.startswith("https://"):
        out.append("PUBLIC_URL should start with https:// and be the address Railway gives this service.")
    if not ADMIN_PASSWORD:
        out.append("ADMIN_PASSWORD isn't set, so the admin page is locked.")
    elif len(ADMIN_PASSWORD) < MIN_ADMIN_LENGTH:
        out.append(f"ADMIN_PASSWORD is shorter than {MIN_ADMIN_LENGTH} characters. Use a longer one.")
    if not ANTHROPIC_API_KEY:
        out.append("ANTHROPIC_API_KEY isn't set, so the assistant can't answer yet.")
    elif not ANTHROPIC_API_KEY.startswith("sk-ant-"):
        out.append("ANTHROPIC_API_KEY should start with sk-ant-. Check you copied the whole key.")
    if SLACK_BOT_TOKEN and not SLACK_BOT_TOKEN.startswith("xoxb-"):
        out.append("SLACK_BOT_TOKEN should start with xoxb-. Check you copied the bot token, not the signing secret.")
    if not SLACK_SIGNING_SECRET:
        out.append("SLACK_SIGNING_SECRET isn't set yet, so Slack requests can't be checked.")
    if any(not x.startswith("U") for x in ADVISOR_SLACK_IDS):
        out.append("ADVISOR_SLACK_IDS should be Slack member IDs that start with U, separated by commas.")
    if not ADVISOR_SLACK_IDS:
        out.append("ADVISOR_SLACK_IDS isn't set, so nobody can give the assistant commands yet.")
    if not tz_ok():
        out.append(f"TIMEZONE '{TIMEZONE}' isn't a time zone name. Use something like America/Chicago.")
    return out


def tz_ok():
    try:
        ZoneInfo(TIMEZONE)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def admin_status():
    if not ADMIN_PASSWORD:
        return "locked (not set)"
    if len(ADMIN_PASSWORD) < MIN_ADMIN_LENGTH:
        return "weak"
    return "ok"


# ---------------------------------------------------------------- storage (JSON files on the Volume, saved safely)
_store_lock = threading.RLock()


def store_file():
    return DATA_DIR / "store.json"


def storage_ok():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def load_store():
    try:
        data = json.loads(store_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"activity": []}
    except (OSError, ValueError):
        return {"activity": []}


def save_store(data):
    """Write to a temporary file first, then swap it in, so a crash never leaves half a file."""
    with _store_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = store_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, store_file())


SECRET_PATTERNS = [re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), re.compile(r"xox[abp]-[A-Za-z0-9\-]+"),
                   re.compile(r"(?i)(password|secret|token)=\S+")]


def clean_for_log(text):
    for pat in SECRET_PATTERNS:
        text = pat.sub("[hidden]", text)
    return text


def log_event(text):
    """Keeps the last 300 things that happened. Never stores secrets."""
    with _store_lock:
        data = load_store()
        data.setdefault("activity", []).append({"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                                "text": clean_for_log(text)})
        data["activity"] = data["activity"][-300:]
        save_store(data)


def recent_activity(n=50):
    with _store_lock:
        return list(reversed(load_store().get("activity", [])))[:n]


# ---------------------------------------------------------------- firewall: rate limit and admin lockout
_hits, _bans = {}, {}
_rate_lock = threading.Lock()
RATE_WINDOW, RATE_MAX, BAN_SECONDS = 300, 1500, 60


def rate_ok(ip):
    now = time.time()
    with _rate_lock:
        if len(_hits) > 10000:          # keep memory small
            _hits.clear()
        if _bans.get(ip, 0) > now:
            return False
        hits = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
        hits.append(now)
        _hits[ip] = hits
        if len(hits) > RATE_MAX:
            _bans[ip] = now + BAN_SECONDS
            _hits[ip] = []
            return False
        return True


_fails = {}
LOCK_TRIES, LOCK_SECONDS = 5, 900


def admin_auth(ip, header):
    """'ok', 'bad', 'locked' or 'not_set'. Five wrong tries lock the admin page for 15 minutes."""
    now = time.time()
    count, until = _fails.get(ip, (0, 0))
    if until > now:
        return "locked"
    if not ADMIN_PASSWORD:
        return "not_set"
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        _, _, pw = raw.partition(":")
        ok = hmac.compare_digest(pw.strip(), ADMIN_PASSWORD)
    except (AttributeError, IndexError, ValueError, TypeError):
        ok = False
    if ok:
        _fails.pop(ip, None)
        return "ok"
    count += 1
    _fails[ip] = (0, now + LOCK_SECONDS) if count >= LOCK_TRIES else (count, 0)
    return "bad"


# ---------------------------------------------------------------- pages (one inline style block, fingerprinted for the CSP)
STYLE = """
body{font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;background:#f5f6f8;color:#14191f;margin:0}
main{max-width:760px;margin:0 auto;padding:32px 20px 60px}
h1{font-size:30px;letter-spacing:-.02em}h2{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:#6b7480}
.card{background:#fff;border:1px solid #e4e8ee;border-radius:14px;padding:18px;margin:14px 0}
.ok{color:#1f7a4d;font-weight:600}.bad{color:#b3261e;font-weight:600}.mute{color:#6b7480;font-size:14px}
li{margin:6px 0}code{background:#eef1f5;padding:1px 6px;border-radius:6px}
"""
STYLE_HASH = "sha256-" + base64.b64encode(hashlib.sha256(STYLE.encode("utf-8")).digest()).decode("ascii")
CSP = (f"default-src 'none'; style-src '{STYLE_HASH}'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def page(title, body):
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
            f"<body><main>{body}</main></body></html>")


def health_data():
    age = None if _heartbeat["last"] is None else round(time.time() - _heartbeat["last"])
    return {
        "ok": True,
        "version": VERSION,
        "time_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "storage": storage_ok(),
        "timezone_ok": tz_ok(),
        "admin_password": admin_status(),
        "integrations": {
            "public_url": PUBLIC_URL.startswith("https://"),
            "slack": bool(SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET),
            "google": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
            "ai": bool(ANTHROPIC_API_KEY),
        },
        "background_heartbeat_seconds_ago": age,
    }


def status_page():
    h = health_data()
    rows = []
    for label, good in [("Storage (Volume attached)", h["storage"]), ("Time zone", h["timezone_ok"]),
                        ("Public address", h["integrations"]["public_url"]),
                        ("Slack set up", h["integrations"]["slack"]), ("Google set up", h["integrations"]["google"]),
                        ("AI key set up", h["integrations"]["ai"]),
                        ("Admin password", h["admin_password"] == "ok")]:
        rows.append(f"<li class=\"{'ok' if good else 'bad'}\">{'Yes' if good else 'Not yet'} · {html.escape(label)}</li>")
    problems = config_problems()
    fixes = "".join(f"<li>{html.escape(p)}</li>" for p in problems) or "<li class=\"ok\">Nothing needs fixing.</li>"
    body = (f"<h1>Status</h1><p class=\"mute\">Version {VERSION} · server time {h['time_utc']}</p>"
            f"<div class=\"card\"><h2>Setup</h2><ul>{''.join(rows)}</ul></div>"
            f"<div class=\"card\"><h2>What to fix</h2><ul>{fixes}</ul></div>")
    return page("DECA Assistant status", body)


def admin_page():
    problems = config_problems()
    fixes = "".join(f"<li>{html.escape(p)}</li>" for p in problems) or "<li class=\"ok\">Nothing needs fixing.</li>"
    acts = "".join(f"<li class=\"mute\">{html.escape(a['time'])} · {html.escape(a['text'])}</li>"
                   for a in recent_activity())
    body = (f"<h1>Admin</h1><p class=\"mute\">Version {VERSION}. Keep this page private.</p>"
            f"<div class=\"card\"><h2>Problems</h2><ul>{fixes}</ul></div>"
            f"<div class=\"card\"><h2>Activity</h2><ul>{acts or '<li class=\"mute\">Nothing yet.</li>'}</ul></div>"
            f"<div class=\"card\"><h2>What's new</h2><ul>{''.join('<li>' + html.escape(w) + '</li>' for w in WHATS_NEW)}</ul></div>")
    return page("DECA Assistant admin", body)


def home_page():
    return page("DECA Assistant", "<h1>DECA Assistant</h1><p class=\"mute\">This is the server for the chapter's "
                                  "assistant. Advisor sign-in comes in a later stage.</p>")


def privacy_page():
    return page("Privacy", "<h1>Privacy</h1><div class=\"card\"><ul>"
                           "<li>Only what the advisor adds is stored: member names, emails, grades, events, roles, "
                           "deadlines, budget entries, sponsors and tasks.</li>"
                           "<li>No birthdays. No student passwords, phone numbers or home addresses.</li>"
                           "<li>No keys or tokens are written to files or logs.</li>"
                           "<li>Anyone's information can be removed on request.</li></ul></div>")


# ---------------------------------------------------------------- the background heartbeat
_heartbeat = {"last": None}


def background_loop():
    while True:
        _heartbeat["last"] = time.time()
        time.sleep(60)


# ---------------------------------------------------------------- the web server
class Handler(BaseHTTPRequestHandler):
    server_version = "DECA"

    def log_message(self, *a):
        pass

    def _ip(self):
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return forwarded or self.client_address[0]

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        if PUBLIC_URL.startswith("https://"):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        ip = self._ip()
        if not rate_ok(ip):
            return self._send(429, "Too many requests. Wait a few minutes and try again.", "text/plain; charset=utf-8")
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._send(200, home_page())
        if path == "/health":
            return self._json(200, health_data())
        if path == "/status":
            return self._send(200, status_page())
        if path == "/privacy":
            return self._send(200, privacy_page())
        if path == "/admin":
            result = admin_auth(ip, self.headers.get("Authorization"))
            if result == "locked":
                return self._send(429, "Locked for 15 minutes after too many wrong tries.", "text/plain; charset=utf-8")
            if result == "not_set":
                return self._send(503, "ADMIN_PASSWORD isn't set in Railway yet.", "text/plain; charset=utf-8")
            if result != "ok":
                return self._send(401, "Sign in with any name and the admin password.", "text/plain; charset=utf-8",
                                  {"WWW-Authenticate": 'Basic realm="DECA admin"'})
            return self._send(200, admin_page())
        self._send(404, "Not found.", "text/plain; charset=utf-8")


def main():
    for problem in config_problems():
        print("SETUP:", problem)
    threading.Thread(target=background_loop, daemon=True).start()
    try:
        log_event(f"Server started, version {VERSION}.")
    except OSError:
        print("SETUP: couldn't write to storage. Attach a Volume at /data in Railway.")
    port = int(env("PORT", "8080"))
    print(f"DECA Assistant server {VERSION} listening on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
