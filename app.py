"""Salon Manager — licence admin website (vendor side).

A small, self-contained Flask app you host once and open from your iPhone (or
any browser) to manage the salons you rent the app to:

* see every salon and its live status (active / expiring / expired / off),
* add a salon, renew it, or switch it on/off with one tap,
* it serves a **signed** ``/licenses.json`` that the Android salon apps check —
  so a change here reaches the tablets the next time they're online.

Your RSA signing key lives on the server (as an environment variable), never in
the browser. The dashboard is behind a password login.

Run locally
-----------
    pip install -r requirements.txt
    # point it at the signing key you already generated with the CLI tool:
    set ADMIN_PASSWORD=choose-a-password        (Windows)  /  export … (mac/Linux)
    set SECRET_KEY=any-long-random-string
    set VENDOR_PRIVATE_KEY_FILE=../tools/vendor_private_key.json
    python app.py
    # open http://localhost:8000  (or http://YOUR-PC-IP:8000 from your phone)

Deploy: see README.md (Render / Railway, free tier, ~5 minutes).

Config (environment variables)
    ADMIN_PASSWORD          required — the password to log in
    SECRET_KEY              required — random string that signs your login cookie
    VENDOR_PRIVATE_KEY      the signing key as JSON  {"n":"…","e":65537,"d":"…"}
      (or) VENDOR_PRIVATE_KEY_FILE   path to that JSON file
    DATA_FILE              where the salon list is stored (default ./data/salons.json)
    REMINDER_DAYS          days-before-expiry to show 'expiring' (default 30)

Durable free storage (recommended for free hosts like Render):
    GITHUB_TOKEN           a GitHub token with read/write to one repo's contents
    GITHUB_REPO            "owner/repo" that holds the licence files
    GITHUB_BRANCH          branch to commit to (default: main)
    SALONS_PATH            path in the repo for the salon list (default salons.json)
    LICENSES_PATH          path for the signed feed (default licenses.json)
  When these are set the app keeps the salon list IN GitHub (so it survives the
  free tier wiping its disk) and writes the signed licenses.json there too — the
  tablets then read that file from raw.githubusercontent.com, which is always up
  even when this dashboard is asleep. Without them it falls back to DATA_FILE.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.request
import secrets
from datetime import date, datetime
from functools import wraps

from flask import (Flask, Response, abort, redirect, request, session,
                   url_for)

# --------------------------------------------------------------------------
# Signing (must match salon/licensing.py in the app, byte-for-byte)
# --------------------------------------------------------------------------

SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _pkcs1_encode(message: bytes, k: int) -> bytes:
    t = SHA256_DIGESTINFO + hashlib.sha256(message).digest()
    ps = b"\xff" * (k - len(t) - 3)
    return b"\x00\x01" + ps + b"\x00" + t


def canonical(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode(
        "utf-8")


def sign(message: bytes, priv) -> int:
    k = (priv["n"].bit_length() + 7) // 8
    em = _pkcs1_encode(message, k)
    return pow(int.from_bytes(em, "big"), priv["d"], priv["n"])


def _load_private_key():
    raw = os.environ.get("VENDOR_PRIVATE_KEY")
    if not raw:
        path = os.environ.get("VENDOR_PRIVATE_KEY_FILE")
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
    if not raw:
        return None
    k = json.loads(raw)
    return {"n": int(k["n"]), "e": int(k["e"]), "d": int(k["d"])}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

DATA_FILE = os.environ.get("DATA_FILE",
                           os.path.join(os.path.dirname(__file__), "data",
                                        "salons.json"))
REMINDER_DAYS = int(os.environ.get("REMINDER_DAYS", "30"))

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
SALONS_PATH = os.environ.get("SALONS_PATH", "salons.json")
LICENSES_PATH = os.environ.get("LICENSES_PATH", "licenses.json")


def github_enabled() -> bool:
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def _gh(method: str, path: str, body: dict | None = None) -> dict:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "salon-licence-admin",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _gh_get(path: str) -> tuple[bytes | None, str | None]:
    """Return (file bytes, sha) or (None, None) if the file doesn't exist."""
    try:
        meta = _gh("GET", f"{path}?ref={GITHUB_BRANCH}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, None
        raise
    content = base64.b64decode(meta.get("content", "")) if meta.get(
        "content") else b""
    return content, meta.get("sha")


def _gh_put(path: str, data: bytes, message: str) -> None:
    _, sha = _gh_get(path)
    body = {"message": message, "branch": GITHUB_BRANCH,
            "content": base64.b64encode(data).decode()}
    if sha:
        body["sha"] = sha
    _gh("PUT", path, body)


def load_salons() -> dict:
    if github_enabled():
        try:
            raw, _ = _gh_get(SALONS_PATH)
            if raw:
                return (json.loads(raw) or {}).get("salons", {})
            return {}
        except (urllib.error.URLError, ValueError):
            return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("salons", {})
    except (OSError, ValueError):
        return {}


def save_salons(salons: dict) -> None:
    if github_enabled():
        blob = json.dumps({"salons": salons}, indent=2).encode()
        _gh_put(SALONS_PATH, blob, "Update salon list")
        # Also publish the SIGNED feed the tablets read, so raw.githubusercontent
        # serves the latest even while this dashboard is asleep.
        priv = _load_private_key()
        if priv:
            signed = json.dumps({
                "data": salons, "sig": str(sign(canonical(salons), priv)),
                "generated": datetime.now().isoformat(timespec="seconds")}
            ).encode()
            _gh_put(LICENSES_PATH, signed, "Update signed licences")
        return
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"salons": salons}, fh, indent=2)
    os.replace(tmp, DATA_FILE)


def feed_url(fallback: str) -> str:
    """Where the tablets should point LICENSE_URL — GitHub raw when configured,
    otherwise this server's own /licenses.json."""
    if github_enabled():
        return (f"https://raw.githubusercontent.com/{GITHUB_REPO}/"
                f"{GITHUB_BRANCH}/{LICENSES_PATH}")
    return fallback


def valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def email_hash(email: str) -> str:
    """One-way fingerprint of a Google email — the licence key. Must match
    salon/licensing.py exactly (lower-cased, trimmed, SHA-256 hex)."""
    return hashlib.sha256((email or "").strip().lower().encode("utf-8")
                          ).hexdigest()


def valid_email(s: str) -> bool:
    s = (s or "").strip()
    return "@" in s and "." in s.split("@")[-1] and len(s) >= 6


def status_of(entry: dict, today: date | None = None) -> tuple[str, int | None]:
    """Return (label, days_left). label ∈ off | expired | expiring | active."""
    today = today or date.today()
    if not entry.get("active"):
        return "off", None
    try:
        days = (datetime.strptime(entry["expiry"], "%Y-%m-%d").date()
                - today).days
    except (ValueError, KeyError, TypeError):
        return "expired", None
    if days < 0:
        return "expired", days
    if days <= REMINDER_DAYS:
        return "expiring", days
    return "active", days


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


# ---- public: the signed feed the salon apps read -------------------------

@app.route("/licenses.json")
def licenses_json():
    priv = _load_private_key()
    if not priv:
        abort(503, "Signing key not configured")
    salons = load_salons()
    payload = {"data": salons, "sig": str(sign(canonical(salons), priv)),
               "generated": datetime.now().isoformat(timespec="seconds")}
    return Response(json.dumps(payload), mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


@app.route("/health")
def health():
    return {"ok": True, "salons": len(load_salons()),
            "signing_key": bool(_load_private_key())}


# ---- auth ----------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        want = os.environ.get("ADMIN_PASSWORD", "")
        got = request.form.get("password", "")
        if want and secrets.compare_digest(got, want):
            session["auth"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Wrong password."
    return render(LOGIN_TMPL, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---- dashboard + actions -------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    salons = load_salons()
    today = date.today()
    rows = []
    for sid, s in sorted(salons.items(), key=lambda kv: kv[1].get("name", "")):
        label, days = status_of(s, today)
        rows.append({"id": sid, "name": s.get("name", ""),
                     "expiry": s.get("expiry", ""), "active": s.get("active"),
                     "status": label, "days": days})
    counts = {k: sum(1 for r in rows if r["status"] == k)
              for k in ("active", "expiring", "expired", "off")}
    return render(DASH_TMPL, rows=rows, counts=counts, total=len(rows),
                  key_ok=bool(_load_private_key()),
                  feed=feed_url(url_for("licenses_json", _external=True)),
                  gh=github_enabled())


@app.route("/add", methods=["POST"])
@login_required
def add():
    # Accept a plain Client ID (what the tablet sends) or a Google email — both
    # are hashed to the same fingerprint the app computes, so either works.
    ident = (request.form.get("identifier")
             or request.form.get("email") or "").strip()
    name = (request.form.get("name") or "").strip()
    expiry = (request.form.get("expiry") or "").strip()
    if name and ident and valid_date(expiry):
        salons = load_salons()
        # Store only the one-way fingerprint of the identifier, never the raw
        # value. email_hash lower-cases + trims, matching salon/licensing.py.
        salons[email_hash(ident)] = {"name": name, "active": True,
                                     "expiry": expiry}
        save_salons(salons)
    return redirect(url_for("dashboard"))


@app.route("/renew", methods=["POST"])
@login_required
def renew():
    sid = request.form.get("salon_id", "")
    expiry = (request.form.get("expiry") or "").strip()
    salons = load_salons()
    if sid in salons and valid_date(expiry):
        salons[sid]["expiry"] = expiry
        salons[sid]["active"] = True
        save_salons(salons)
    return redirect(url_for("dashboard"))


@app.route("/toggle", methods=["POST"])
@login_required
def toggle():
    sid = request.form.get("salon_id", "")
    salons = load_salons()
    if sid in salons:
        salons[sid]["active"] = not salons[sid].get("active")
        save_salons(salons)
    return redirect(url_for("dashboard"))


@app.route("/remove", methods=["POST"])
@login_required
def remove():
    sid = request.form.get("salon_id", "")
    salons = load_salons()
    if salons.pop(sid, None) is not None:
        save_salons(salons)
    return redirect(url_for("dashboard"))


@app.route("/export")
@login_required
def export():
    return Response(json.dumps({"salons": load_salons()}, indent=2),
                    mimetype="application/json",
                    headers={"Content-Disposition":
                             "attachment; filename=salons-backup.json"})


# --------------------------------------------------------------------------
# Templates (inline, mobile-first, matching the app's teal/coral brand)
# --------------------------------------------------------------------------

def render(tmpl: str, **ctx) -> str:
    from flask import render_template_string
    return render_template_string(tmpl, **ctx)


BASE_CSS = """
:root{--teal:#15808b;--teal-deep:#0e626b;--coral:#c05a3e;--ink:#1a2b31;
--muted:#5c7076;--bg:#eef1f0;--card:#fff;--line:#dbe4e4;--amber:#e9a400}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
.bar{background:var(--teal-deep);color:#fff;padding:14px 18px;display:flex;
align-items:center;gap:10px;position:sticky;top:0;z-index:5}
.bar h1{font-size:18px;margin:0;font-weight:800;letter-spacing:-.01em}
.bar .sp{flex:1}
.bar a{color:#cfe6e8;font-size:14px;text-decoration:none;font-weight:600}
.wrap{max-width:760px;margin:0 auto;padding:18px 16px 60px}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:2px 0 18px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:10px 8px;text-align:center}
.tile b{display:block;font-size:22px;font-weight:800;line-height:1.1}
.tile span{font-size:11px;color:var(--muted);text-transform:uppercase;
letter-spacing:.04em}
.tile.active b{color:var(--teal)} .tile.expiring b{color:#a06a00}
.tile.expired b{color:var(--coral)} .tile.off b{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:14px 16px;margin-bottom:12px}
.card .top{display:flex;align-items:center;gap:10px}
.card .nm{font-weight:800;font-size:16px}
.card .id{font-size:12px;color:var(--muted);font-family:ui-monospace,monospace}
.pill{margin-left:auto;font-size:11px;font-weight:800;padding:4px 10px;
border-radius:999px;letter-spacing:.03em;text-transform:uppercase;white-space:nowrap}
.pill.active{background:#dcefe9;color:var(--teal-deep)}
.pill.expiring{background:#ffefc2;color:#7a4f00}
.pill.expired{background:#f7ddd4;color:var(--coral)}
.pill.off{background:#e6ecec;color:var(--muted)}
.meta{font-size:13px;color:var(--muted);margin:8px 0 12px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,button{font:inherit}
input[type=text],input[type=date]{padding:9px 11px;border:1px solid var(--line);
border-radius:9px;background:#fbfdfd;color:var(--ink);min-width:0}
.btn{border:0;border-radius:9px;padding:9px 14px;font-weight:700;font-size:14px;
cursor:pointer}
.btn.p{background:var(--teal);color:#fff}
.btn.o{background:#fff;border:1px solid var(--teal);color:var(--teal-deep)}
.btn.d{background:#fff;border:1px solid var(--coral);color:var(--coral)}
.btn.g{background:#fff;border:1px solid var(--line);color:var(--muted)}
details.add{background:var(--card);border:1px dashed var(--line);border-radius:14px;
padding:12px 16px;margin:6px 0 18px}
details.add summary{font-weight:800;cursor:pointer;color:var(--teal-deep)}
details.add .row{margin-top:12px}
.hint{font-size:12px;color:var(--muted);margin-top:14px;line-height:1.5}
.hint code{background:#fff;border:1px solid var(--line);border-radius:5px;
padding:1px 6px;font-size:12px}
.warn{background:#fff4d6;border:1px solid #f0d488;color:#7a4f00;border-radius:12px;
padding:10px 14px;font-size:13px;margin-bottom:16px;font-weight:600}
form.inline{display:inline}
.center{min-height:100vh;display:grid;place-items:center;padding:24px}
.login{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:28px 24px;width:100%;max-width:360px}
.login h2{margin:0 0 4px;font-size:20px}
.login p{margin:0 0 18px;color:var(--muted);font-size:14px}
.login .row{margin-top:14px}
.err{color:var(--coral);font-size:13px;font-weight:600;margin-top:10px}
"""

LOGIN_TMPL = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Licence admin — sign in</title><style>{{css|safe}}</style></head>
<body><div class=center><form class=login method=post>
<h2>Salon Manager</h2><p>Licence admin — sign in</p>
<input type=password name=password placeholder="Password" autofocus
style="width:100%">
<div class=row><button class="btn p" style="width:100%">Sign in</button></div>
{% if error %}<div class=err>{{error}}</div>{% endif %}
</form></div></body></html>
"""

DASH_TMPL = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Licence admin</title><style>{{css|safe}}</style></head><body>
<div class=bar><h1>Salon licences</h1><div class=sp></div>
<a href="{{ url_for('export') }}">Backup</a>
<a href="{{ url_for('logout') }}">Sign out</a></div>
<div class=wrap>
{% if not key_ok %}<div class=warn>⚠ No signing key configured — the salon apps
can't be verified yet. Set VENDOR_PRIVATE_KEY on the server.</div>{% endif %}
<div class=tiles>
<div class="tile active"><b>{{counts.active}}</b><span>Active</span></div>
<div class="tile expiring"><b>{{counts.expiring}}</b><span>Expiring</span></div>
<div class="tile expired"><b>{{counts.expired}}</b><span>Expired</span></div>
<div class="tile off"><b>{{counts.off}}</b><span>Off</span></div>
</div>

<details class=add><summary>+ Add a salon</summary>
<form method=post action="{{ url_for('add') }}">
<div class=row>
<input type=text name=name placeholder="Salon name" required
 style="flex:1 1 160px">
<input type=text name=identifier placeholder="Client ID (e.g. Fabulook2026)"
 required style="flex:1 1 200px">
<input type=date name=expiry required>
<button class="btn p">Add</button></div>
<div class=hint>Enter the Client ID you set on the tablet (Settings → Set Client
ID). Case-insensitive. It's stored only as a private fingerprint. You can also
paste a Google email here instead if a salon activates with an account.</div>
</form></details>

{% for r in rows %}
<div class=card>
<div class=top>
<div><div class=nm>{{r.name or '(unnamed)'}}</div>
<div class=id>Client ID · fingerprint {{r.id[:8]}}</div></div>
<span class="pill {{r.status}}">
{% if r.status=='active' %}Active · {{r.days}}d
{% elif r.status=='expiring' %}Expires in {{r.days}}d
{% elif r.status=='expired' %}Expired
{% else %}Switched off{% endif %}</span>
</div>
<div class=meta>Expiry on record: <b>{{r.expiry or '—'}}</b></div>
<div class=row>
<form class=inline method=post action="{{ url_for('renew') }}">
<input type=hidden name=salon_id value="{{r.id}}">
<input type=date name=expiry required>
<button class="btn p">Renew</button></form>
<form class=inline method=post action="{{ url_for('toggle') }}">
<input type=hidden name=salon_id value="{{r.id}}">
<button class="btn {{ 'd' if r.active else 'o' }}">
{{ 'Switch off' if r.active else 'Switch on' }}</button></form>
<form class=inline method=post action="{{ url_for('remove') }}"
 onsubmit="return confirm('Remove {{r.name}}? Their app will lock.')">
<input type=hidden name=salon_id value="{{r.id}}">
<button class="btn g">Remove</button></form>
</div></div>
{% endfor %}
{% if not rows %}<p style="color:var(--muted)">No salons yet — add your first
one above.</p>{% endif %}

<div class=hint>Salon apps read this signed feed:<br>
<code>{{feed}}</code><br>
Set that URL as <code>LICENSE_URL</code> in the app build. Changes here reach a
tablet the next time it's online (up to a 7-day offline grace).</div>
</div></body></html>
"""


@app.context_processor
def _inject_css():
    return {"css": BASE_CSS}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
