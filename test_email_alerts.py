import json
import os
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock

import pytest

_tmp_dir = tempfile.mkdtemp()

import main


@pytest.fixture(autouse=True)
def fresh_db():
    # Re-apply path overrides in case another test module changed them
    main.DB_PATH = os.path.join(_tmp_dir, "test_email.db")
    main.ALERTS_PATH = os.path.join(_tmp_dir, "test_email_alerts.json")

    if os.path.exists(main.DB_PATH):
        os.remove(main.DB_PATH)
    pending_path = os.path.join(_tmp_dir, "pending_email.json")
    if os.path.exists(pending_path):
        os.remove(pending_path)
    if os.path.exists(main.ALERTS_PATH):
        os.remove(main.ALERTS_PATH)
    main.init_db()
    yield
    if os.path.exists(main.DB_PATH):
        os.remove(main.DB_PATH)
    if os.path.exists(pending_path):
        os.remove(pending_path)
    if os.path.exists(main.ALERTS_PATH):
        os.remove(main.ALERTS_PATH)


@pytest.fixture
def client():
    main.app.config["TESTING"] = True
    with main.app.test_client() as c:
        yield c


def _add(client, name, min_qty, cur_qty):
    return client.post(
        "/api/products",
        data=json.dumps({"name": name, "min_quantity": min_qty, "current_quantity": cur_qty}),
        content_type="application/json",
    )


class TestBuildAlertEmailHtml:
    def test_single_item(self):
        items = [{"product_name": "Widget A", "current_quantity": 2, "min_quantity": 10}]
        html = main._build_alert_email_html(items)
        assert "Widget A" in html
        assert "Low Stock Alert" in html
        assert "1 product" in html
        assert "-8" in html  # deficit

    def test_multiple_items(self):
        items = [
            {"product_name": "Widget A", "current_quantity": 2, "min_quantity": 10},
            {"product_name": "Widget B", "current_quantity": 0, "min_quantity": 5},
        ]
        html = main._build_alert_email_html(items)
        assert "Widget A" in html
        assert "Widget B" in html
        assert "2 products" in html

    def test_html_contains_table(self):
        items = [{"product_name": "Test", "current_quantity": 1, "min_quantity": 5}]
        html = main._build_alert_email_html(items)
        assert "<table" in html
        assert "<th" in html
        assert "Product" in html
        assert "Current" in html
        assert "Minimum" in html
        assert "Deficit" in html


class TestBuildAlertEmailPlain:
    def test_single_item(self):
        items = [{"product_name": "Widget A", "current_quantity": 2, "min_quantity": 10}]
        text = main._build_alert_email_plain(items)
        assert "Widget A" in text
        assert "2/10" in text
        assert "need 8 more" in text
        assert "LOW STOCK ALERT" in text

    def test_multiple_items(self):
        items = [
            {"product_name": "A", "current_quantity": 1, "min_quantity": 10},
            {"product_name": "B", "current_quantity": 0, "min_quantity": 5},
        ]
        text = main._build_alert_email_plain(items)
        assert "2 product(s)" in text
        assert "A" in text
        assert "B" in text


class TestSendAlertEmail:
    def test_no_email_returns_error(self):
        result = main.send_alert_email("", [{"product_name": "X", "current_quantity": 0, "min_quantity": 5}])
        assert result["success"] is False
        assert "No alert email" in result["message"]

    def test_no_items_returns_error(self):
        result = main.send_alert_email("test@example.com", [])
        assert result["success"] is False
        assert "No low-stock items" in result["message"]

    def test_writes_pending_email_json(self):
        items = [{"product_name": "Widget", "current_quantity": 2, "min_quantity": 10}]
        result = main.send_alert_email("test@example.com", items)
        assert result["success"] is True

        pending_path = os.path.join(os.path.dirname(main.ALERTS_PATH), "pending_email.json")
        assert os.path.exists(pending_path), f"Expected {pending_path} to exist"

        with open(pending_path) as f:
            data = json.load(f)
        assert data["to_email"] == "test@example.com"
        assert "Widget" in data["body_html"]
        assert "Widget" in data["body_plain"]
        assert data["status"] in ("pending", "sent")
        assert len(data["low_stock_items"]) == 1

    def test_pending_email_subject_contains_count(self):
        items = [
            {"product_name": "A", "current_quantity": 1, "min_quantity": 10},
            {"product_name": "B", "current_quantity": 0, "min_quantity": 5},
        ]
        main.send_alert_email("test@example.com", items)
        pending_path = os.path.join(os.path.dirname(main.ALERTS_PATH), "pending_email.json")
        with open(pending_path) as f:
            data = json.load(f)
        assert "2 products" in data["subject"]

    def test_singular_subject_for_one_item(self):
        items = [{"product_name": "Solo", "current_quantity": 1, "min_quantity": 10}]
        main.send_alert_email("test@example.com", items)
        pending_path = os.path.join(os.path.dirname(main.ALERTS_PATH), "pending_email.json")
        with open(pending_path) as f:
            data = json.load(f)
        assert "1 product " in data["subject"]
        assert "products" not in data["subject"]

    @patch("main.requests.post")
    def test_api_call_success(self, mock_post):
        main.WINGMAN_API_BASE = "https://fake.api"
        main.WINGMAN_API_TOKEN = "tok123"
        try:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '{"ok": true}'
            mock_post.return_value = mock_resp

            items = [{"product_name": "Widget", "current_quantity": 2, "min_quantity": 10}]
            result = main.send_alert_email("test@example.com", items)
            assert result["success"] is True
            assert "sent" in result["message"].lower()

            pending_path = os.path.join(os.path.dirname(main.ALERTS_PATH), "pending_email.json")
            with open(pending_path) as f:
                data = json.load(f)
            assert data["status"] == "sent"
        finally:
            main.WINGMAN_API_BASE = ""
            main.WINGMAN_API_TOKEN = ""

    @patch("main.requests.post")
    def test_api_call_failure_still_queues(self, mock_post):
        main.WINGMAN_API_BASE = "https://fake.api"
        main.WINGMAN_API_TOKEN = "tok123"
        try:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.text = "Internal Server Error"
            mock_post.return_value = mock_resp

            items = [{"product_name": "Widget", "current_quantity": 2, "min_quantity": 10}]
            result = main.send_alert_email("test@example.com", items)
            assert result["success"] is True
            assert "queued" in result["message"].lower() or "written" in result["message"].lower()

            pending_path = os.path.join(os.path.dirname(main.ALERTS_PATH), "pending_email.json")
            with open(pending_path) as f:
                data = json.load(f)
            assert data["status"] == "api_error"
        finally:
            main.WINGMAN_API_BASE = ""
            main.WINGMAN_API_TOKEN = ""

    @patch("main.requests.post", side_effect=Exception("Connection refused"))
    def test_api_unreachable_still_queues(self, mock_post):
        main.WINGMAN_API_BASE = "https://fake.api"
        main.WINGMAN_API_TOKEN = "tok123"
        try:
            items = [{"product_name": "Widget", "current_quantity": 2, "min_quantity": 10}]
            result = main.send_alert_email("test@example.com", items)
            assert result["success"] is True

            pending_path = os.path.join(os.path.dirname(main.ALERTS_PATH), "pending_email.json")
            with open(pending_path) as f:
                data = json.load(f)
            assert data["status"] == "api_unreachable"
        finally:
            main.WINGMAN_API_BASE = ""
            main.WINGMAN_API_TOKEN = ""


class TestCheckAlertsWithEmail:
    def test_no_low_stock_no_email(self, client):
        _add(client, "Widget", 5, 10)
        resp = client.post("/api/check-alerts")
        data = json.loads(resp.data)
        assert data["low_stock_count"] == 0
        assert data["email_status"]["success"] is True
        assert "fully stocked" in data["email_status"]["message"].lower()

    def test_low_stock_triggers_email(self, client):
        _add(client, "Widget", 10, 2)
        resp = client.post("/api/check-alerts")
        data = json.loads(resp.data)
        assert data["low_stock_count"] == 1
        assert data["email_status"]["success"] is True
        assert "magnus@ostin.no" in data["email_status"]["message"]

        pending_path = os.path.join(os.path.dirname(main.ALERTS_PATH), "pending_email.json")
        assert os.path.exists(pending_path)
        with open(pending_path) as f:
            email_data = json.load(f)
        assert email_data["to_email"] == "magnus@ostin.no"
        assert "Widget" in email_data["body_html"]

    def test_low_stock_uses_updated_email(self, client):
        client.post(
            "/api/settings",
            data=json.dumps({"alert_email": "newalert@example.com"}),
            content_type="application/json",
        )
        _add(client, "Gadget", 20, 3)
        resp = client.post("/api/check-alerts")
        data = json.loads(resp.data)
        assert data["low_stock_count"] == 1
        assert "newalert@example.com" in data["email_status"]["message"]

    def test_no_email_configured(self, client):
        conn = sqlite3.connect(main.DB_PATH)
        conn.execute("DELETE FROM settings WHERE key = 'alert_email'")
        conn.commit()
        conn.close()

        _add(client, "Widget", 10, 2)
        resp = client.post("/api/check-alerts")
        data = json.loads(resp.data)
        assert data["low_stock_count"] == 1
        assert data["email_status"]["success"] is False
        assert "no alert email" in data["email_status"]["message"].lower()

    def test_check_alerts_still_writes_json(self, client):
        _add(client, "Widget", 10, 2)
        client.post("/api/check-alerts")
        assert os.path.exists(main.ALERTS_PATH)
        with open(main.ALERTS_PATH) as f:
            data = json.load(f)
        assert data["low_stock_count"] == 1

    def test_response_includes_email_status(self, client):
        _add(client, "Widget", 5, 10)
        resp = client.post("/api/check-alerts")
        data = json.loads(resp.data)
        assert "email_status" in data
        assert "success" in data["email_status"]
        assert "message" in data["email_status"]


class TestPendingEmailEndpoint:
    def test_no_pending_email(self, client):
        resp = client.get("/api/pending-email")
        data = json.loads(resp.data)
        assert data["status"] == "none"

    def test_pending_email_after_alert(self, client):
        _add(client, "Widget", 10, 2)
        client.post("/api/check-alerts")

        resp = client.get("/api/pending-email")
        data = json.loads(resp.data)
        assert data["to_email"] == "magnus@ostin.no"
        assert data["status"] in ("pending", "sent")
        assert "Widget" in data["body_html"]


class TestDbPathPersistence:
    def test_default_paths_valid(self):
        assert main.DB_PATH.endswith(".db")
        assert main.ALERTS_PATH.endswith(".json")

    def test_env_var_override_in_source(self):
        import inspect
        source = inspect.getsource(main)
        assert "INVENTORY_DB_DIR" in source
