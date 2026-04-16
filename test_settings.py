import json
import os
import sqlite3
import tempfile

import pytest

_tmp_dir = tempfile.mkdtemp()

import main

main.DB_PATH = os.path.join(_tmp_dir, "test_settings.db")
main.ALERTS_PATH = os.path.join(_tmp_dir, "test_settings_alerts.json")


@pytest.fixture(autouse=True)
def fresh_db():
    if os.path.exists(main.DB_PATH):
        os.remove(main.DB_PATH)
    main.init_db()
    yield
    if os.path.exists(main.DB_PATH):
        os.remove(main.DB_PATH)


@pytest.fixture
def client():
    main.app.config["TESTING"] = True
    with main.app.test_client() as c:
        yield c


class TestSettingsTable:
    def test_init_db_creates_settings_table(self):
        """init_db creates the settings table."""
        conn = sqlite3.connect(main.DB_PATH)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchall()
        conn.close()
        assert len(tables) == 1

    def test_init_db_seeds_alert_email(self):
        """init_db seeds alert_email with default value."""
        conn = sqlite3.connect(main.DB_PATH)
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'alert_email'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "magnus@ostin.no"

    def test_init_db_does_not_overwrite_existing_seed(self):
        """Calling init_db again does not overwrite an updated alert_email."""
        conn = sqlite3.connect(main.DB_PATH)
        conn.execute(
            "UPDATE settings SET value = 'changed@example.com' WHERE key = 'alert_email'"
        )
        conn.commit()
        conn.close()

        # Re-init
        main.init_db()

        conn = sqlite3.connect(main.DB_PATH)
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'alert_email'"
        ).fetchone()
        conn.close()
        assert row[0] == "changed@example.com"


class TestGetSettings:
    def test_get_settings_returns_all(self, client):
        """GET /api/settings returns all settings as a dict."""
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "alert_email" in data
        assert data["alert_email"] == "magnus@ostin.no"

    def test_get_settings_empty_db(self, client):
        """GET /api/settings returns empty dict if no settings."""
        conn = sqlite3.connect(main.DB_PATH)
        conn.execute("DELETE FROM settings")
        conn.commit()
        conn.close()

        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data == {}


class TestPostSettings:
    def test_update_alert_email(self, client):
        """POST /api/settings updates alert_email."""
        resp = client.post(
            "/api/settings",
            data=json.dumps({"alert_email": "newemail@example.com"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["alert_email"] == "newemail@example.com"

    def test_update_persists(self, client):
        """Updated setting is returned on subsequent GET."""
        client.post(
            "/api/settings",
            data=json.dumps({"alert_email": "persist@example.com"}),
            content_type="application/json",
        )
        resp = client.get("/api/settings")
        data = json.loads(resp.data)
        assert data["alert_email"] == "persist@example.com"

    def test_update_multiple_settings(self, client):
        """POST can update/create multiple settings at once."""
        resp = client.post(
            "/api/settings",
            data=json.dumps({"alert_email": "a@b.com", "theme": "dark"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["alert_email"] == "a@b.com"
        assert data["theme"] == "dark"

    def test_update_creates_new_key(self, client):
        """POST can create a new setting that didn't exist."""
        resp = client.post(
            "/api/settings",
            data=json.dumps({"new_setting": "some_value"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["new_setting"] == "some_value"
        assert data["alert_email"] == "magnus@ostin.no"

    def test_post_empty_body_rejected(self, client):
        """POST with empty body is rejected."""
        resp = client.post(
            "/api/settings",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_post_non_string_value_rejected(self, client):
        """POST with non-string value is rejected."""
        resp = client.post(
            "/api/settings",
            data=json.dumps({"alert_email": 123}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_post_non_dict_body_rejected(self, client):
        """POST with non-dict body is rejected."""
        resp = client.post(
            "/api/settings",
            data=json.dumps("just a string"),
            content_type="application/json",
        )
        assert resp.status_code == 400


class TestGetSettingHelper:
    def test_get_setting_returns_value(self):
        """get_setting() returns the stored value."""
        result = main.get_setting("alert_email")
        assert result == "magnus@ostin.no"

    def test_get_setting_returns_default_for_missing_key(self):
        """get_setting() returns default when key doesn't exist."""
        result = main.get_setting("nonexistent", "fallback")
        assert result == "fallback"

    def test_get_setting_returns_none_for_missing_key(self):
        """get_setting() returns None by default when key doesn't exist."""
        result = main.get_setting("nonexistent")
        assert result is None

    def test_get_setting_reflects_api_update(self, client):
        """get_setting() reflects changes made via the API."""
        client.post(
            "/api/settings",
            data=json.dumps({"alert_email": "updated@test.com"}),
            content_type="application/json",
        )
        result = main.get_setting("alert_email")
        assert result == "updated@test.com"
