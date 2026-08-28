# Licence admin website — setup & deploy

A small private website (Flask) where **you** manage the salons you rent Salon
Manager to — check status, renew, switch on/off — from your **iPhone** or any
browser. It serves a **signed** `/licenses.json` that the Android salon apps
read, so a change here reaches the tablets the next time they're online.

Your signing key stays on the server; the dashboard is behind a password.

---

## What it does
- **Dashboard** — every salon with a live status pill (Active / Expiring /
  Expired / Off), counts at the top.
- **Add / Renew / Switch off / Switch on / Remove** — one tap each.
- **`/licenses.json`** — the public signed feed the apps check.
- **Backup** — download your salon list any time.

---

## 1. Try it on your PC first (2 minutes)
```bash
cd web-admin
pip install -r requirements.txt
# use the signing key you generated with tools/license_admin.py keygen:
set VENDOR_PRIVATE_KEY_FILE=../tools/vendor_private_key.json   # Windows
set ADMIN_PASSWORD=choose-a-password
set SECRET_KEY=any-long-random-text
python app.py
```
Open <http://localhost:8000>. To reach it from your iPhone on the **same Wi‑Fi**,
find your PC's IP (`ipconfig`) and visit `http://THAT-IP:8000` on the phone.

*(On Mac/Linux use `export` instead of `set`.)*

---

## 2. Put it online so you can use it anywhere (≈5 min, free)

Any Python host works. Easiest is **Render** (free tier):

1. Push this `web-admin` folder to a GitHub repo.
2. On <https://render.com> → **New → Web Service** → connect that repo.
3. Settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app`
4. Add **Environment variables** (Render → Environment):
   | Key | Value |
   |---|---|
   | `ADMIN_PASSWORD` | your dashboard password |
   | `SECRET_KEY` | a long random string |
   | `VENDOR_PRIVATE_KEY` | your key as JSON: `{"n":"…","e":65537,"d":"…"}` (paste the contents of `tools/vendor_private_key.json`) |
   | `DATA_FILE` | `/var/data/salons.json` |
5. **Add a Disk** (Render → Disks): mount path `/var/data`, 1 GB. This keeps your
   salon list across restarts. *(Skip this and the list resets on redeploy —
   the Disk matters.)*
6. Deploy. Your site is `https://your-name.onrender.com`.

> **Railway** or **Fly.io** work the same way — a web service + a small volume
> for `DATA_FILE`, the four env vars above, start command `gunicorn app:app`.

> Free Render services **sleep after 15 min idle** and take ~30s to wake. That's
> fine here: the salon apps carry a 7‑day offline grace, and you just wait a
> moment when you open the dashboard. Paid tiers (~$7/mo) stay awake.

---

## 3. Point the app at your site
In `salon/licensing.py` set:
```python
LICENSE_URL = "https://your-name.onrender.com/licenses.json"
```
Rebuild the app (`sync_core.ps1` → `flet build apk …`). Done — every tablet now
checks your site.

---

## Keeping data safe
- Tap **Backup** on the dashboard now and then to download `salons-backup.json`.
- Your **private key** is only in the `VENDOR_PRIVATE_KEY` env var and your local
  `tools/vendor_private_key.json` — never in the repo (it's git‑ignored). Back it
  up privately; if you lose it, every salon must be re‑keyed.

## Security notes
- Always use the **https** URL the host gives you (they provide the certificate).
- Pick a strong `ADMIN_PASSWORD` and a long random `SECRET_KEY`.
- The `/licenses.json` feed is public by design — it holds only salon IDs, names,
  and on/off flags, no customer data — and it's signed so it can't be forged.
