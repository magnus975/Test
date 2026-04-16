import json
import os
import sqlite3
import io
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request, send_file, send_from_directory
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "inventory.db")
ALERTS_PATH = os.path.join(APP_DIR, "pending_alerts.json")

app = Flask(__name__, static_folder="static")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            min_quantity INTEGER NOT NULL DEFAULT 0,
            current_quantity INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("alert_email", "magnus@ostin.no"),
    )
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    """Read a setting from the database (usable outside Flask request context)."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


# --- Routes ---

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/products", methods=["GET"])
def list_products():
    db = get_db()
    rows = db.execute(
        "SELECT id, name, min_quantity, current_quantity FROM products ORDER BY name"
    ).fetchall()
    products = [dict(r) for r in rows]
    return jsonify(products)


@app.route("/api/products", methods=["POST"])
def add_product():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Product name is required"}), 400

    min_qty = data.get("min_quantity")
    if min_qty is None or not str(min_qty).lstrip("-").isdigit():
        return jsonify({"error": "Minimum quantity must be a number"}), 400
    min_qty = int(min_qty)
    if min_qty < 0:
        return jsonify({"error": "Minimum quantity cannot be negative"}), 400

    current_qty = int(data.get("current_quantity", 0))
    if current_qty < 0:
        return jsonify({"error": "Current quantity cannot be negative"}), 400

    db = get_db()
    try:
        db.execute(
            "INSERT INTO products (name, min_quantity, current_quantity) VALUES (?, ?, ?)",
            (name, min_qty, current_qty),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": f"Product '{name}' already exists"}), 409

    row = db.execute(
        "SELECT id, name, min_quantity, current_quantity FROM products WHERE name = ?",
        (name,),
    ).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/products/<int:product_id>", methods=["PATCH"])
def update_product(product_id):
    data = request.get_json(force=True)
    db = get_db()

    row = db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
    if not row:
        return jsonify({"error": "Product not found"}), 404

    updates = []
    params = []

    if "current_quantity" in data:
        qty = data["current_quantity"]
        if not str(qty).lstrip("-").isdigit():
            return jsonify({"error": "Current quantity must be a number"}), 400
        qty = int(qty)
        if qty < 0:
            return jsonify({"error": "Current quantity cannot be negative"}), 400
        updates.append("current_quantity = ?")
        params.append(qty)

    if "min_quantity" in data:
        mq = data["min_quantity"]
        if not str(mq).lstrip("-").isdigit():
            return jsonify({"error": "Minimum quantity must be a number"}), 400
        mq = int(mq)
        if mq < 0:
            return jsonify({"error": "Minimum quantity cannot be negative"}), 400
        updates.append("min_quantity = ?")
        params.append(mq)

    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return jsonify({"error": "Product name is required"}), 400
        updates.append("name = ?")
        params.append(name)

    if not updates:
        return jsonify({"error": "No fields to update"}), 400

    params.append(product_id)
    db.execute(f"UPDATE products SET {', '.join(updates)} WHERE id = ?", params)
    db.commit()

    row = db.execute(
        "SELECT id, name, min_quantity, current_quantity FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()
    return jsonify(dict(row))


@app.route("/api/products/<int:product_id>", methods=["DELETE"])
def delete_product(product_id):
    db = get_db()
    row = db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
    if not row:
        return jsonify({"error": "Product not found"}), 404
    db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/check-alerts", methods=["POST"])
def check_alerts():
    db = get_db()
    rows = db.execute(
        """
        SELECT name, current_quantity, min_quantity
        FROM products
        WHERE current_quantity < min_quantity
        ORDER BY name
        """
    ).fetchall()

    alerts = [
        {
            "product_name": r["name"],
            "current_quantity": r["current_quantity"],
            "min_quantity": r["min_quantity"],
        }
        for r in rows
    ]

    payload = {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "low_stock_count": len(alerts),
        "alerts": alerts,
    }

    with open(ALERTS_PATH, "w") as f:
        json.dump(payload, f, indent=2)

    return jsonify(payload)


@app.route("/api/pending-alerts", methods=["GET"])
def get_pending_alerts():
    if not os.path.exists(ALERTS_PATH):
        return jsonify({"low_stock_count": 0, "alerts": []})
    with open(ALERTS_PATH) as f:
        return jsonify(json.load(f))


@app.route("/api/export-xlsx", methods=["GET"])
def export_xlsx():
    db = get_db()
    rows = db.execute(
        "SELECT name, min_quantity, current_quantity FROM products ORDER BY name"
    ).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"

    # -- Styles --
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="0284C7", end_color="0284C7", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style="thin", color="D1D5DB"),
        right=Side(style="thin", color="D1D5DB"),
        top=Side(style="thin", color="D1D5DB"),
        bottom=Side(style="thin", color="D1D5DB"),
    )
    low_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
    low_font = Font(color="B91C1C")
    ok_fill = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
    ok_font = Font(color="15803D")
    center_align = Alignment(horizontal="center")

    # -- Header row --
    headers = ["Product Name", "Min Quantity", "Current Quantity", "Status"]
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    # -- Data rows --
    for row_idx, r in enumerate(rows, 2):
        is_low = r["current_quantity"] < r["min_quantity"]
        status = "⚠ Low Stock" if is_low else "✓ OK"

        ws.cell(row=row_idx, column=1, value=r["name"]).border = thin_border
        c_min = ws.cell(row=row_idx, column=2, value=r["min_quantity"])
        c_min.alignment = center_align
        c_min.border = thin_border
        c_cur = ws.cell(row=row_idx, column=3, value=r["current_quantity"])
        c_cur.alignment = center_align
        c_cur.border = thin_border
        c_status = ws.cell(row=row_idx, column=4, value=status)
        c_status.alignment = center_align
        c_status.border = thin_border

        if is_low:
            c_status.fill = low_fill
            c_status.font = low_font
        else:
            c_status.fill = ok_fill
            c_status.font = ok_font

    # -- Column widths --
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 16

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"inventory_{timestamp}.xlsx"

    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/settings", methods=["GET"])
def get_settings():
    db = get_db()
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json(force=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    db = get_db()
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return jsonify({"error": f"Both key and value must be strings"}), 400
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    db.commit()

    rows = db.execute("SELECT key, value FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    return jsonify(settings)


if __name__ == "__main__":
    init_db()
    print(f"✅ Inventory Management App running at http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
