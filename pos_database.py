# -*- coding: utf-8 -*-
"""
pos_database.py
שכבת מסד נתונים (SQLite) לתוכנת הקופה. אין תלות בחבילות חיצוניות - רק sqlite3 המובנה בפייתון.
"""
import sqlite3
import os
import uuid
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pos_data.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """יוצר את הטבלאות אם אינן קיימות, וממלא הגדרות ברירת מחדל."""
    conn = get_connection()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            barcode TEXT UNIQUE,
            price REAL NOT NULL,
            cost REAL,
            qty REAL NOT NULL DEFAULT 0,
            min_qty REAL NOT NULL DEFAULT 3,
            category TEXT DEFAULT 'כללי'
        );

        CREATE TABLE IF NOT EXISTS sales (
            id TEXT PRIMARY KEY,
            invoice_no INTEGER NOT NULL,
            doc_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ok',        -- ok / voided / partial_refund
            raw_total REAL NOT NULL,
            discount_amount REAL NOT NULL DEFAULT 0,
            subtotal REAL NOT NULL,
            vat_amount REAL NOT NULL,
            vat_rate REAL NOT NULL,
            total REAL NOT NULL,
            refunded_total REAL NOT NULL DEFAULT 0,
            customer_name TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sale_items (
            id TEXT PRIMARY KEY,
            sale_id TEXT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            product_id TEXT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            qty REAL NOT NULL,
            returned_qty REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            sale_id TEXT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            method TEXT NOT NULL,      -- cash / credit / check / store_credit
            amount REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS shifts (
            id TEXT PRIMARY KEY,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            opening_float REAL NOT NULL DEFAULT 0,
            expected_cash REAL,
            actual_cash REAL
        );

        CREATE TABLE IF NOT EXISTS shift_movements (
            id TEXT PRIMARY KEY,
            shift_id TEXT NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,     -- in / out
            amount REAL NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    defaults = {
        "business_name": "העסק שלי",
        "vat_rate": "17",
        "next_invoice_no": "1",
        "admin_pin": "",
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


def uid():
    return uuid.uuid4().hex


# ---------- הגדרות ----------

def get_setting(key, default=None):
    conn = get_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


# ---------- מוצרים ----------

def get_all_products(search=""):
    conn = get_connection()
    if search:
        q = f"%{search.lower()}%"
        rows = conn.execute(
            "SELECT * FROM products WHERE lower(name) LIKE ? OR barcode LIKE ? ORDER BY name",
            (q, f"%{search}%"),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_product_by_barcode(barcode):
    conn = get_connection()
    row = conn.execute("SELECT * FROM products WHERE barcode = ?", (barcode,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_product_by_id(product_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_product(product):
    """יוצר מוצר חדש אם אין id, או מעדכן מוצר קיים."""
    conn = get_connection()
    if product.get("id"):
        conn.execute(
            "UPDATE products SET name=?, barcode=?, price=?, cost=?, qty=?, min_qty=?, category=? WHERE id=?",
            (
                product["name"], product.get("barcode") or None, product["price"], product.get("cost"),
                product["qty"], product.get("min_qty", 3), product.get("category", "כללי"), product["id"],
            ),
        )
    else:
        product["id"] = uid()
        conn.execute(
            "INSERT INTO products (id, name, barcode, price, cost, qty, min_qty, category) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                product["id"], product["name"], product.get("barcode") or None, product["price"],
                product.get("cost"), product["qty"], product.get("min_qty", 3), product.get("category", "כללי"),
            ),
        )
    conn.commit()
    conn.close()
    return product["id"]


def delete_product(product_id):
    conn = get_connection()
    conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()


def barcode_exists(barcode, exclude_id=None):
    if not barcode:
        return False
    conn = get_connection()
    if exclude_id:
        row = conn.execute("SELECT id FROM products WHERE barcode=? AND id<>?", (barcode, exclude_id)).fetchone()
    else:
        row = conn.execute("SELECT id FROM products WHERE barcode=?", (barcode,)).fetchone()
    conn.close()
    return row is not None


# ---------- מכירות ----------

class InsufficientStockError(Exception):
    pass


def next_invoice_no():
    conn = get_connection()
    row = conn.execute("SELECT value FROM settings WHERE key='next_invoice_no'").fetchone()
    n = int(row["value"]) if row else 1
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('next_invoice_no', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(n + 1),),
    )
    conn.commit()
    conn.close()
    return n


def record_sale(cart_items, payments, doc_type="חשבונית מס קבלה", discount_amount=0.0, customer_name=None):
    """
    cart_items: [{product_id, name, price, qty}, ...]  (product_id יכול להיות None לפריט חופשי)
    payments:   [{method, amount}, ...]
    מבצע הכל בטרנזקציה אחת: בודק מלאי, יוצר מסמך מכירה, יורד מלאי.
    """
    vat_rate = float(get_setting("vat_rate", "17"))
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        # בדיקת מלאי זמין לפני שמתחילים לרשום כלום
        for item in cart_items:
            if item.get("product_id"):
                row = conn.execute("SELECT qty FROM products WHERE id=?", (item["product_id"],)).fetchone()
                if row is None or row["qty"] < item["qty"]:
                    raise InsufficientStockError(f'אין מספיק מלאי עבור "{item["name"]}"')

        raw_total = sum(i["price"] * i["qty"] for i in cart_items)
        total = max(0.0, raw_total - discount_amount)
        subtotal = total / (1 + vat_rate / 100)
        vat_amount = total - subtotal

        paid = sum(p["amount"] for p in payments)
        if round(paid, 2) < round(total, 2):
            raise ValueError("סכום התשלום נמוך מסך החשבונית")

        sale_id = uid()
        invoice_no_row = conn.execute("SELECT value FROM settings WHERE key='next_invoice_no'").fetchone()
        invoice_no = int(invoice_no_row["value"]) if invoice_no_row else 1
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('next_invoice_no', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(invoice_no + 1),),
        )

        conn.execute(
            "INSERT INTO sales (id, invoice_no, doc_type, status, raw_total, discount_amount, "
            "subtotal, vat_amount, vat_rate, total, refunded_total, customer_name, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sale_id, invoice_no, doc_type, "ok", raw_total, discount_amount,
                subtotal, vat_amount, vat_rate, total, 0.0, customer_name,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        for item in cart_items:
            conn.execute(
                "INSERT INTO sale_items (id, sale_id, product_id, name, price, qty, returned_qty) "
                "VALUES (?,?,?,?,?,?,0)",
                (uid(), sale_id, item.get("product_id"), item["name"], item["price"], item["qty"]),
            )
            if item.get("product_id"):
                conn.execute(
                    "UPDATE products SET qty = qty - ? WHERE id = ?", (item["qty"], item["product_id"])
                )
        for p in payments:
            conn.execute(
                "INSERT INTO payments (id, sale_id, method, amount) VALUES (?,?,?,?)",
                (uid(), sale_id, p["method"], p["amount"]),
            )
        conn.commit()
        return get_sale(sale_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_sale(sale_id):
    conn = get_connection()
    sale = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    if not sale:
        conn.close()
        return None
    items = conn.execute("SELECT * FROM sale_items WHERE sale_id=?", (sale_id,)).fetchall()
    pays = conn.execute("SELECT * FROM payments WHERE sale_id=?", (sale_id,)).fetchall()
    conn.close()
    result = dict(sale)
    result["items"] = [dict(i) for i in items]
    result["payments"] = [dict(p) for p in pays]
    return result


def recent_sales(limit=30):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM sales ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def refund_sale(sale_id, items_to_return, reason=""):
    """items_to_return: [{sale_item_id, qty}, ...] - זיכוי מלא או חלקי."""
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        sale = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if not sale:
            raise ValueError("מכירה לא נמצאה")
        refund_total = 0.0
        for entry in items_to_return:
            si = conn.execute(
                "SELECT * FROM sale_items WHERE id=?", (entry["sale_item_id"],)
            ).fetchone()
            available = si["qty"] - si["returned_qty"]
            qty = min(entry["qty"], available)
            if qty <= 0:
                continue
            refund_total += si["price"] * qty
            conn.execute(
                "UPDATE sale_items SET returned_qty = returned_qty + ? WHERE id=?", (qty, si["id"])
            )
            if si["product_id"]:
                conn.execute("UPDATE products SET qty = qty + ? WHERE id=?", (qty, si["product_id"]))
        conn.execute(
            "UPDATE sales SET refunded_total = refunded_total + ? WHERE id=?", (refund_total, sale_id)
        )
        items = conn.execute("SELECT qty, returned_qty FROM sale_items WHERE sale_id=?", (sale_id,)).fetchall()
        fully_returned = all(i["returned_qty"] >= i["qty"] for i in items)
        if fully_returned:
            conn.execute("UPDATE sales SET status='voided' WHERE id=?", (sale_id,))
        elif refund_total > 0:
            conn.execute("UPDATE sales SET status='partial_refund' WHERE id=?", (sale_id,))
        conn.commit()
        return refund_total
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- דוחות ----------

def low_stock_products():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM products WHERE qty <= min_qty ORDER BY qty").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def today_summary():
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT * FROM sales WHERE created_at LIKE ?", (f"{today}%",)
    ).fetchall()
    conn.close()
    total = sum((r["total"] if r["status"] != "voided" else 0) - r["refunded_total"] for r in rows)
    count = sum(1 for r in rows if r["status"] != "voided")
    return {"total": total, "count": count}


def _period_profit(prefix):
    """רווח גולמי משוער לתקופה (יום/חודש) - לפי מחיר עלות נוכחי של המוצר, לא היסטורי."""
    conn = get_connection()
    sales_rows = conn.execute("SELECT * FROM sales WHERE created_at LIKE ?", (f"{prefix}%",)).fetchall()
    profit = 0.0
    for sale in sales_rows:
        items = conn.execute("SELECT * FROM sale_items WHERE sale_id=?", (sale["id"],)).fetchall()
        for item in items:
            if not item["product_id"]:
                continue
            p = conn.execute("SELECT cost FROM products WHERE id=?", (item["product_id"],)).fetchone()
            if not p or p["cost"] is None:
                continue
            qty = item["qty"] - item["returned_qty"]
            profit += qty * (item["price"] - p["cost"])
    conn.close()
    return profit


def today_profit():
    return _period_profit(datetime.now().strftime("%Y-%m-%d"))


def month_profit():
    return _period_profit(datetime.now().strftime("%Y-%m"))


# ---------- משמרות ----------

def current_shift():
    conn = get_connection()
    row = conn.execute("SELECT * FROM shifts WHERE closed_at IS NULL ORDER BY opened_at DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def open_shift(opening_float):
    if current_shift():
        raise ValueError("כבר יש משמרת פתוחה")
    conn = get_connection()
    shift_id = uid()
    conn.execute(
        "INSERT INTO shifts (id, opened_at, opening_float) VALUES (?,?,?)",
        (shift_id, datetime.now().isoformat(timespec="seconds"), opening_float),
    )
    conn.commit()
    conn.close()
    return shift_id


def add_shift_movement(shift_id, kind, amount, note=""):
    conn = get_connection()
    conn.execute(
        "INSERT INTO shift_movements (id, shift_id, kind, amount, note, created_at) VALUES (?,?,?,?,?,?)",
        (uid(), shift_id, kind, amount, note, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def expected_cash(shift):
    """קופה צפויה = פתיחה + מכירות מזומן מרגע הפתיחה - זיכויים במזומן + הפקדות - משיכות."""
    conn = get_connection()
    sales_rows = conn.execute("SELECT * FROM sales WHERE created_at >= ?", (shift["opened_at"],)).fetchall()
    cash_total = 0.0
    for sale in sales_rows:
        pays = conn.execute("SELECT * FROM payments WHERE sale_id=? AND method='cash'", (sale["id"],)).fetchall()
        cash_paid = sum(p["amount"] for p in pays)
        if sale["status"] == "voided":
            continue
        # יחס גס: אם שולם חלק מזומן, מניחים שגם הזיכוי (אם יש) נלקח באותו יחס מהמזומן
        cash_total += cash_paid
        if sale["refunded_total"] > 0 and sale["total"] > 0:
            cash_ratio = cash_paid / sale["total"] if sale["total"] else 0
            cash_total -= sale["refunded_total"] * cash_ratio
    movements = conn.execute("SELECT * FROM shift_movements WHERE shift_id=?", (shift["id"],)).fetchall()
    moves_total = sum((m["amount"] if m["kind"] == "in" else -m["amount"]) for m in movements)
    conn.close()
    return shift["opening_float"] + cash_total + moves_total


def close_shift(shift_id, actual_cash):
    conn = get_connection()
    shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if not shift:
        conn.close()
        raise ValueError("משמרת לא נמצאה")
    exp = expected_cash(dict(shift))
    conn.execute(
        "UPDATE shifts SET closed_at=?, expected_cash=?, actual_cash=? WHERE id=?",
        (datetime.now().isoformat(timespec="seconds"), exp, actual_cash, shift_id),
    )
    conn.commit()
    conn.close()
    return exp


def recent_shifts(limit=20):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM shifts WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- גיבוי ושחזור ----------

def export_all():
    conn = get_connection()
    data = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "products": [dict(r) for r in conn.execute("SELECT * FROM products").fetchall()],
        "sales": [dict(r) for r in conn.execute("SELECT * FROM sales").fetchall()],
        "sale_items": [dict(r) for r in conn.execute("SELECT * FROM sale_items").fetchall()],
        "payments": [dict(r) for r in conn.execute("SELECT * FROM payments").fetchall()],
        "settings": {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings").fetchall()},
        "shifts": [dict(r) for r in conn.execute("SELECT * FROM shifts").fetchall()],
        "shift_movements": [dict(r) for r in conn.execute("SELECT * FROM shift_movements").fetchall()],
    }
    conn.close()
    return data


def restore_all(data):
    """מחליף את כל הנתונים הקיימים בנתונים מהגיבוי. פעולה הרסנית - יש לאשר מול המשתמש לפני קריאה לכאן."""
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        for table in ["payments", "sale_items", "sales", "shift_movements", "shifts", "products"]:
            conn.execute(f"DELETE FROM {table}")
        for p in data.get("products", []):
            conn.execute(
                "INSERT INTO products (id,name,barcode,price,cost,qty,min_qty,category) VALUES (?,?,?,?,?,?,?,?)",
                (p["id"], p["name"], p.get("barcode"), p["price"], p.get("cost"), p["qty"],
                 p.get("min_qty", 3), p.get("category", "כללי")),
            )
        for s in data.get("sales", []):
            conn.execute(
                "INSERT INTO sales (id,invoice_no,doc_type,status,raw_total,discount_amount,subtotal,"
                "vat_amount,vat_rate,total,refunded_total,customer_name,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s["id"], s["invoice_no"], s["doc_type"], s["status"], s["raw_total"], s["discount_amount"],
                 s["subtotal"], s["vat_amount"], s["vat_rate"], s["total"], s["refunded_total"],
                 s.get("customer_name"), s["created_at"]),
            )
        for i in data.get("sale_items", []):
            conn.execute(
                "INSERT INTO sale_items (id,sale_id,product_id,name,price,qty,returned_qty) VALUES (?,?,?,?,?,?,?)",
                (i["id"], i["sale_id"], i.get("product_id"), i["name"], i["price"], i["qty"], i["returned_qty"]),
            )
        for p in data.get("payments", []):
            conn.execute(
                "INSERT INTO payments (id,sale_id,method,amount) VALUES (?,?,?,?)",
                (p["id"], p["sale_id"], p["method"], p["amount"]),
            )
        for sh in data.get("shifts", []):
            conn.execute(
                "INSERT INTO shifts (id,opened_at,closed_at,opening_float,expected_cash,actual_cash) "
                "VALUES (?,?,?,?,?,?)",
                (sh["id"], sh["opened_at"], sh.get("closed_at"), sh["opening_float"],
                 sh.get("expected_cash"), sh.get("actual_cash")),
            )
        for m in data.get("shift_movements", []):
            conn.execute(
                "INSERT INTO shift_movements (id,shift_id,kind,amount,note,created_at) VALUES (?,?,?,?,?,?)",
                (m["id"], m["shift_id"], m["kind"], m["amount"], m.get("note", ""), m["created_at"]),
            )
        for k, v in data.get("settings", {}).items():
            conn.execute(
                "INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, v),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
