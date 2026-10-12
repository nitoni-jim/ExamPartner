"""
tests/test_licensing_config.py — a malformed MIN_WINDOWS_APP_VERSION fails
licensing CLOSED, and only licensing (brief §4.E, F9).

The version gate is the one lever for stopping a broken Windows client
(426). A typo in the Render setting must therefore never silently switch it
off — and, because this backend also serves the live Android app, it must
never stop the backend either. So: one ERROR at startup, licensing
unavailable (503 on /licence/*, /health "unavailable" with 200), and every
other route working.
"""
import logging
import uuid

import pytest

from conftest import TEST_USER, make_signals

CLIENT = {"platform": "windows", "app_version": "1.0.0", "os_version": "10.0.22631"}
MALFORMED = ["1.0", "v1.0.0", "1.0.0.0", "", "   ", "abc", "1.0.0-rc1", "1. 0.0", "-1.0.0"]


@pytest.fixture
def restore_licensing_config():
    """Whatever the test configures, put the licensing config back afterwards."""
    from services import licence_lease, licensing_service

    signing, gate = dict(licence_lease._signing), dict(licensing_service._client_gate)
    yield
    licence_lease._signing.clear()
    licence_lease._signing.update(signing)
    licensing_service._client_gate.clear()
    licensing_service._client_gate.update(gate)


def _key_config(monkeypatch):
    import base64
    import os

    import config

    monkeypatch.setattr(config, "LICENCE_SIGNING_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setattr(config, "LICENCE_SIGNING_KID", "1")


def _register_and_login(client):
    """The Android path, end to end: a core route that must keep working."""
    identifier = f"android-{uuid.uuid4().hex[:10]}@example.com"
    registered = client.post("/auth/register", json={"identifier": identifier, "password": "pw-1234",
                                                     "device_id": "android-device-1", "platform": "android"})
    assert registered.status_code == 200, registered.text
    login = client.post("/auth/login", json={"identifier": identifier, "password": "pw-1234",
                                             "device_id": "android-device-1", "platform": "android"})
    assert login.status_code == 200, login.text
    assert login.json()["token"]


def _valid_activate_body(pool_id, app_version="1.0.0"):
    return {"request_id": str(uuid.uuid4()), "pool_id": pool_id, "machine_label": "LAB-PC-07",
            "fingerprint_signals": make_signals(),
            "client": {"platform": "windows", "app_version": app_version, "os_version": "10.0.22631"}}


# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", MALFORMED)
def test_malformed_minimum_is_refused_with_one_error(value, monkeypatch, caplog, restore_licensing_config):
    import config
    from services import licensing_service

    monkeypatch.setattr(config, "MIN_WINDOWS_APP_VERSION", value)
    with caplog.at_level(logging.ERROR):
        assert licensing_service.configure_client_gate() is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "MIN_WINDOWS_APP_VERSION" in r.getMessage()]
    assert len(errors) == 1 and "LICENSING UNAVAILABLE" in errors[0].getMessage()
    assert licensing_service.client_gate_status()["ready"] is False


@pytest.mark.parametrize("value", ["1.0", "", "v2.0.0"])
def test_startup_with_a_malformed_minimum_closes_licensing_and_nothing_else(
        value, sqlite_database, make_pool, monkeypatch, caplog, restore_licensing_config):
    """The real startup event, with a valid key and a bad minimum."""
    import config
    from fastapi.testclient import TestClient

    from app import app
    from services.licence_session import issue_activation_token

    _key_config(monkeypatch)
    monkeypatch.setattr(config, "MIN_WINDOWS_APP_VERSION", value)
    pool = make_pool(5)

    with caplog.at_level(logging.ERROR):
        with TestClient(app) as client:          # runs startup: init_db, signing key, client gate
            health = client.get("/health")
            assert health.status_code == 200
            assert health.json()["ok"] is True
            assert health.json()["licensing"] == "unavailable"

            # The backend keeps working for everyone else.
            _register_and_login(client)

            # Licensing is closed: 503, after the contract's earlier checks.
            session = client.post("/licence/activation-session",
                                  json={"identifier": TEST_USER, "password": "x", "client": CLIENT})
            assert session.status_code == 503 and session.json()["detail"]["code"] == "licensing_unavailable"

            token = issue_activation_token(TEST_USER, "institution_owner")[0]
            headers = {"Authorization": f"Bearer {token}"}
            activate = client.post("/licence/activate", json=_valid_activate_body(pool["pool_id"]), headers=headers)
            assert activate.status_code == 503 and activate.json()["detail"]["code"] == "licensing_unavailable"

            # A very old client is NOT told 426 — there is no usable minimum to
            # judge it by — it gets the same 503.
            old = client.post("/licence/activate", json=_valid_activate_body(pool["pool_id"], "0.0.1"), headers=headers)
            assert old.status_code == 503 and old.json()["detail"]["code"] == "licensing_unavailable"

            # Earlier precedence still holds: shape problems are reported first.
            bad_version = client.post("/licence/activate",
                                      json=_valid_activate_body(pool["pool_id"], "1.0"), headers=headers)
            assert bad_version.status_code == 400
            assert bad_version.json()["detail"]["field"] == "client.app_version"
            assert client.post("/licence/activate", json={"pool_id": "x"}, headers=headers).status_code == 422
            assert client.post("/licence/activate", json=_valid_activate_body(pool["pool_id"])).status_code == 401

    assert any(r.levelno == logging.ERROR and "MIN_WINDOWS_APP_VERSION" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value", ["0.0.0", "2.3.4", " 2.3.4 "])
def test_valid_minimum_keeps_licensing_ready(value, sqlite_database, monkeypatch, restore_licensing_config):
    import config
    from fastapi.testclient import TestClient

    from app import app

    _key_config(monkeypatch)
    monkeypatch.setattr(config, "MIN_WINDOWS_APP_VERSION", value)
    with TestClient(app) as client:
        assert client.get("/health").json()["licensing"] == "ready"


def test_the_default_minimum_blocks_nothing(sqlite_database, make_pool, monkeypatch, restore_licensing_config):
    """No MIN_WINDOWS_APP_VERSION in the environment → 0.0.0 → ready, and
    the oldest possible client version is accepted."""
    import importlib

    import config
    from fastapi.testclient import TestClient

    from app import app
    from services.licence_session import issue_activation_token

    monkeypatch.delenv("MIN_WINDOWS_APP_VERSION", raising=False)
    fresh = importlib.reload(config)
    try:
        assert fresh.MIN_WINDOWS_APP_VERSION == "0.0.0"
        _key_config(monkeypatch)
        pool = make_pool(5)
        with TestClient(app) as client:
            assert client.get("/health").json()["licensing"] == "ready"
            token = issue_activation_token(TEST_USER, "institution_owner")[0]
            response = client.post("/licence/activate", json=_valid_activate_body(pool["pool_id"], "0.0.0"),
                                   headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200, response.text
    finally:
        importlib.reload(config)


def test_a_valid_minimum_is_enforced(sqlite_database, make_pool, monkeypatch, restore_licensing_config):
    import config
    from fastapi.testclient import TestClient

    from app import app
    from services.licence_session import issue_activation_token

    _key_config(monkeypatch)
    monkeypatch.setattr(config, "MIN_WINDOWS_APP_VERSION", "2.3.4")
    pool = make_pool(5)
    with TestClient(app) as client:
        token = issue_activation_token(TEST_USER, "institution_owner")[0]
        headers = {"Authorization": f"Bearer {token}"}
        below = client.post("/licence/activate", json=_valid_activate_body(pool["pool_id"], "2.3.3"), headers=headers)
        assert below.status_code == 426 and below.json()["detail"]["min_app_version"] == "2.3.4"
        at = client.post("/licence/activate", json=_valid_activate_body(pool["pool_id"], "2.3.4"), headers=headers)
        assert at.status_code == 200, at.text
