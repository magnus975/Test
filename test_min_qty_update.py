import json
import os
import sqlite3
import tempfile

import pytest

_tmp_dir = tempfile.mkdtemp()

import main

main.DB_PATH = os.path.join(_tmp_dir, "test_min_qty.db")
main.ALERTS_PATH = os.path.join(_tmp_dir, "test_min_qty_alerts.json")


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


def _add(client, name, min_qty, cur_qty):
    return client.post(
        "/api/products",
        data=json.dumps({"name": name, "min_quantity": min_qty, "current_quantity": cur_qty}),
        content_type="application/json",
    )


def _get_id(client, name):
    products = json.loads(client.get("/api/products").data)
    return next(p["id"] for p in products if p["name"] == name)


class TestMinQuantityPatch:
    def test_patch_min_quantity(self, client):
        """PATCH with min_quantity updates the field and returns updated product."""
        _add(client, "Widget", 10, 20)
        pid = _get_id(client, "Widget")

        resp = client.patch(
            f"/api/products/{pid}",
            data=json.dumps({"min_quantity": 25}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["min_quantity"] == 25
        assert data["current_quantity"] == 20  # unchanged

    def test_patch_min_quantity_reflected_in_list(self, client):
        """After patching min_quantity, GET /api/products reflects the new value."""
        _add(client, "Gadget", 5, 15)
        pid = _get_id(client, "Gadget")

        client.patch(
            f"/api/products/{pid}",
            data=json.dumps({"min_quantity": 30}),
            content_type="application/json",
        )

        products = json.loads(client.get("/api/products").data)
        gadget = next(p for p in products if p["name"] == "Gadget")
        assert gadget["min_quantity"] == 30

    def test_patch_min_quantity_zero(self, client):
        """min_quantity of 0 is valid."""
        _add(client, "Item", 10, 5)
        pid = _get_id(client, "Item")

        resp = client.patch(
            f"/api/products/{pid}",
            data=json.dumps({"min_quantity": 0}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["min_quantity"] == 0

    def test_patch_min_quantity_negative_rejected(self, client):
        """Negative min_quantity is rejected with 400."""
        _add(client, "Thing", 10, 10)
        pid = _get_id(client, "Thing")

        resp = client.patch(
            f"/api/products/{pid}",
            data=json.dumps({"min_quantity": -5}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_patch_both_fields_simultaneously(self, client):
        """Patching both current_quantity and min_quantity in one request works."""
        _add(client, "Combo", 10, 10)
        pid = _get_id(client, "Combo")

        resp = client.patch(
            f"/api/products/{pid}",
            data=json.dumps({"current_quantity": 50, "min_quantity": 20}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["current_quantity"] == 50
        assert data["min_quantity"] == 20

    def test_patch_min_qty_status_flips_to_low(self, client):
        """Raising min_quantity above current causes low-stock status on check-alerts."""
        _add(client, "BorderItem", 5, 10)
        pid = _get_id(client, "BorderItem")

        # Raise min_qty so it exceeds current
        client.patch(
            f"/api/products/{pid}",
            data=json.dumps({"min_quantity": 20}),
            content_type="application/json",
        )

        resp = client.post("/api/check-alerts")
        data = json.loads(resp.data)
        assert data["low_stock_count"] == 1
        assert data["alerts"][0]["product_name"] == "BorderItem"
        assert data["alerts"][0]["min_quantity"] == 20

    def test_patch_min_qty_status_flips_to_ok(self, client):
        """Lowering min_quantity below current resolves the low-stock alert."""
        _add(client, "AlmostEmpty", 50, 5)
        pid = _get_id(client, "AlmostEmpty")

        # Confirm it's low initially
        data = json.loads(client.post("/api/check-alerts").data)
        assert data["low_stock_count"] == 1

        # Lower min_qty so it no longer triggers
        client.patch(
            f"/api/products/{pid}",
            data=json.dumps({"min_quantity": 3}),
            content_type="application/json",
        )

        data = json.loads(client.post("/api/check-alerts").data)
        assert data["low_stock_count"] == 0

    def test_patch_nonexistent_product(self, client):
        """Patching a product that doesn't exist returns 404."""
        resp = client.patch(
            "/api/products/9999",
            data=json.dumps({"min_quantity": 5}),
            content_type="application/json",
        )
        assert resp.status_code == 404
