"""Smoke + integration tests for the licence admin website.

Verifies the dashboard flow and — crucially — that the signed /licenses.json the
server produces is accepted by the SAME verifier the Android app uses
(salon.licensing.verify with the embedded public key).
"""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

# Configure the app BEFORE importing it.
os.environ["ADMIN_PASSWORD"] = "test-pass"
os.environ["SECRET_KEY"] = "x" * 40
os.environ["VENDOR_PRIVATE_KEY_FILE"] = os.path.join(ROOT, "tools",
                                                     "vendor_private_key.json")


@pytest.fixture
def client(tmp_path):
    os.environ["DATA_FILE"] = str(tmp_path / "salons.json")
    import importlib
    import app as appmod
    importlib.reload(appmod)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def _login(c):
    return c.post("/login", data={"password": "test-pass"},
                  follow_redirects=True)


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_login_and_add_salon(client):
    _login(client)
    client.post("/add", data={"email": "owner@gmail.com",
                              "name": "Glamour", "expiry": "2099-01-01"},
                follow_redirects=True)
    body = client.get("/").get_data(as_text=True)
    assert "Glamour" in body
    assert "owner@gmail.com" not in body  # raw email is never shown/stored


def test_toggle_and_renew(client):
    from salon.licensing import email_hash
    _login(client)
    h = email_hash("x@gmail.com")
    client.post("/add", data={"email": "x@gmail.com", "name": "X",
                              "expiry": "2099-01-01"})
    client.post("/toggle", data={"salon_id": h})
    assert "Switch on" in client.get("/").get_data(as_text=True)  # now off
    client.post("/renew", data={"salon_id": h, "expiry": "2100-06-01"})
    body = client.get("/").get_data(as_text=True)
    assert "2100-06-01" in body and "Switch off" in body  # renew re-enables


def test_bad_email_rejected(client):
    _login(client)
    client.post("/add", data={"email": "notanemail", "name": "Bad",
                              "expiry": "2099-01-01"})
    assert "Bad" not in client.get("/").get_data(as_text=True)


def test_signed_feed_verifies_with_app_key(client):
    from salon import licensing as lic
    _login(client)
    client.post("/add", data={"email": "feed@gmail.com", "name": "Feed",
                              "expiry": "2099-01-01"})
    r = client.get("/licenses.json")
    assert r.status_code == 200
    payload = json.loads(r.get_data(as_text=True))
    # The Android app's own verifier must accept this signature, keyed by the
    # email fingerprint.
    data = lic.parse_registry(r.get_data())  # raises if signature is bad
    assert lic.email_hash("feed@gmail.com") in data
    assert int(payload["sig"]) > 0


def test_health(client):
    r = client.get("/health")
    j = json.loads(r.get_data(as_text=True))
    assert j["ok"] and j["signing_key"] is True


def test_github_storage_mode(monkeypatch, tmp_path):
    """With GitHub env set, the salon list + signed feed live in a (mocked)
    repo, and the signed feed still verifies with the app's key."""
    os.environ["DATA_FILE"] = str(tmp_path / "unused.json")
    os.environ["GITHUB_TOKEN"] = "tok"
    os.environ["GITHUB_REPO"] = "me/licences"
    import importlib
    import app as appmod
    importlib.reload(appmod)

    repo = {}  # path -> bytes  (fake GitHub contents)

    def fake_gh_get(path):
        return (repo.get(path), "sha" if path in repo else None)

    def fake_gh_put(path, data, message):
        repo[path] = data

    monkeypatch.setattr(appmod, "_gh_get", fake_gh_get)
    monkeypatch.setattr(appmod, "_gh_put", fake_gh_put)

    from salon import licensing as lic
    h = lic.email_hash("repo@gmail.com")
    assert appmod.github_enabled()
    appmod.save_salons({h: {"name": "Repo Salon", "active": True,
                            "expiry": "2099-01-01"}})
    # Both files were written to the repo.
    assert "salons.json" in repo and "licenses.json" in repo
    # Round-trips through the mocked repo.
    assert appmod.load_salons()[h]["name"] == "Repo Salon"
    # The published signed feed verifies with the Android app's key.
    data = lic.parse_registry(repo["licenses.json"])
    assert h in data

    for k in ("GITHUB_TOKEN", "GITHUB_REPO"):
        os.environ.pop(k, None)
    importlib.reload(appmod)
