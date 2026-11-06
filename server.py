#!/usr/bin/env python3
"""
DECA relay server (runs on Railway). It holds the Claude key, so the DECA app never needs it.

Set these in Railway, under your project's Variables:
    ANTHROPIC_API_KEY   your Claude API key (starts with sk-ant-)
    APP_SECRET          a long random password you make up. Paste the SAME value into the
                        DECA app's Setup page as "Relay secret".

Railway sets PORT for you. The start command is:  python server.py
Standard library only, so nothing needs to be installed.
"""
import hmac
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
ALLOWED_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5"}
ALLOWED_FIELDS = ("model", "max_tokens", "system", "messages", "tools")
MAX_TOKENS_CAP = 1500          # keeps each request's cost small
MAX_BODY = 300_000
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
APP_SECRET = os.environ.get("APP_SECRET", "")


class Handler(BaseHTTPRequestHandler):
    server_version = "DECARelay"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/claude":
            return self._json(404, {"error": "not found"})
        if not API_KEY or not APP_SECRET:
            return self._json(500, {"error": "The relay isn't set up. Add ANTHROPIC_API_KEY and APP_SECRET in Railway."})
        if not hmac.compare_digest(self.headers.get("X-App-Secret", ""), APP_SECRET):
            return self._json(401, {"error": "Wrong relay secret. Check the value in Setup."})
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._json(413, {"error": "That request is too big."})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"error": "Bad request."})
        if not isinstance(body, dict) or body.get("model") not in ALLOWED_MODELS:
            return self._json(400, {"error": "That model isn't allowed on this relay."})
        clean = {k: body[k] for k in ALLOWED_FIELDS if k in body}
        clean["max_tokens"] = min(int(clean.get("max_tokens", 1000)), MAX_TOKENS_CAP)
        req = urllib.request.Request(
            CLAUDE_URL, data=json.dumps(clean).encode(), method="POST",
            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            # Don't pass Claude's details back, in case they echo anything sensitive.
            return self._json(502, {"error": f"Claude said no ({e.code}). Check the key and billing on the server."})
        except urllib.error.URLError:
            return self._json(502, {"error": "The relay couldn't reach Claude."})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"DECA relay listening on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
