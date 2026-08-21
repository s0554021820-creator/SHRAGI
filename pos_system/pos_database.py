# -*- coding: utf-8 -*-
"""
pos_database.py
שכבת מסד נתונים (SQLite) לתוכנת הקופה - גרסה Multi-Tenant.
כל הטבלאות העסקיות (products/sales/shifts) שייכות לחנות (store_id) ספציפית,
כך שכמה חנויות יכולות להשתמש באותו שרת/מסד נתונים בלי לראות אחת את הנתונים של השנייה.
אין תלות בחבילות חיצוניות - רק sqlite3 המובנה בפייתון.
"""
import sqlite3
import os
import uuid
from datetime import datetime, timedelta

# ניתן לדרוס את מיקום הקובץ עם משתנה סביבה DB_PATH - שימושי בענן (Render וכו') כדי להצביע
# לדיסק קבוע (Persistent Disk) שלא נמחק בכל דיפלוי. מקומית, פשוט נשמר באותה תיקייה כרגיל.
DB_PATH = os.environ.get("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "pos_data.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def uid():
    return uuid.uuid4().hex


def init_db():
    """יוצר את הטבלאות אם אינן קיימות."""
    conn = get_connection()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS stores (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            subscription_status TEXT NOT NULL DEFAULT 'trial',   -- trial / active / expired / cancelled
            monthly_price REAL NOT NULL DEFAULT 400,
            trial_ends_at TEXT,
            active_until TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            barcode TEXT,
            price REAL NOT NULL,
            cost REAL,
            qty REAL NOT NULL DEFAULT 0,
            min_qty REAL NOT NULL DEFAULT 3,
            category TEXT DEFAULT 'כללי'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_products_store_barcode ON products(store_id, barcode) WHERE barcode IS NOT NULL;

        CREATE TABLE IF NOT EXISTS sales (
            id TEXT PRIMARY KEY,
            store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
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
            store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (store_id, key)
        );

        CREATE TABLE IF NOT EXISTS shifts (
            id TEXT PRIMARY KEY,
            store_id TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
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

        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()
    conn.close()


# ---------- הגדרות כלל-אפליקטיביות (לא שייכות לחנות ספציפית, כמו מפתח ההצפנה של השרת) ----------

def get_app_meta(key, default=None):
    conn = get_connection()
    row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_app_meta(key, value):
    conn = get_connection()
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


# ---------- חנויות ומשתמשים ----------

def create_store_with_user(store_name, username, password_hash, trial_days=14):
    """יוצר חנות חדשה + משתמש בעלים ראשון עבורה, בטרנזקציה אחת. מחזיר (store_id, user_id)."""
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        existing = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            raise ValueError("שם המשתמש הזה כבר תפוס")
        store_id = uid()
        user_id = uid()
        now = datetime.now()
        now_str = now.isoformat(timespec="seconds")
        trial_ends = (now + timedelta(days=trial_days)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO stores (id, name, created_at, subscription_status, monthly_price, trial_ends_at) "
            "VALUES (?,?,?,?,?,?)",
            (store_id, store_name, now_str, "trial", 400, trial_ends),
        )
        conn.execute(
            "INSERT INTO users (id, store_id, username, password_hash, created_at) VALUES (?,?,?,?,?)",
            (user_id, store_id, username, password_hash, now_str),
        )
        defaults = {
            "business_name": store_name, "vat_rate": "17", "next_invoice_no": "1", "admin_pin": "",
            "offline_mode_enabled": "0",
        }
        for k, v in defaults.items():
            conn.execute("INSERT INTO settings (store_id, key, value) VALUES (?,?,?)", (store_id, k, v))
        conn.commit()
        return store_id, user_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def subscription_state(store_id):
    """מחזיר dict עם is_active (bool) וסיבה - משמש לבדוק אם מותר לחנות להיכנס למערכת."""
    store = get_store(store_id)
    if not store:
        return {"is_active": False, "reason": "חנות לא נמצאה"}
    now = datetime.now().isoformat(timespec="seconds")
    status = store["subscription_status"]
    if status == "cancelled":
        return {"is_active": False, "reason": "המנוי בוטל", "store": store}
    if status == "trial":
        if store["trial_ends_at"] and now > store["trial_ends_at"]:
            return {"is_active": False, "reason": "תקופת הניסיון הסתיימה", "store": store}
        return {"is_active": True, "reason": None, "store": store}
    if status == "active":
        if store["active_until"] and now > store["active_until"]:
            return {"is_active": False, "reason": "תוקף המנוי פג", "store": store}
        return {"is_active": True, "reason": None, "store": store}
    return {"is_active": False, "reason": "סטטוס לא ידוע", "store": store}


def all_stores():
    """כל החנושת - לשימוש פאנל הניהול של בעל הפלטפורמה בלבד."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM stores ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_subscription(store_id, status, extend_days=None):
    """קובע סטטוס מנוי לחנות. אם extend_days ניתן, מאריך את active_until מהיום (או מהתאריך הקודם אם עתידי)."""
    conn = get_connection()
    if extend_days:
        store = conn.execute("SELECT active_until FROM stores WHERE id=?", (store_id,)).fetchone()
        base = datetime.now()
        if store and store["active_until"]:
            try:
                existing = datetime.fromisoformat(store["active_until"])
                if existing > base:
                    base = existing
            except ValueError:
                pass
        new_until = (base + timedelta(days=extend_days)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE stores SET subscription_status=?, active_until=? WHERE id=?", (status, new_until, store_id)
        )
    else:
        conn.execute("UPDATE stores SET subscription_status=? WHERE id=?", (status, store_id))
    conn.commit()
    conn.close()


def get_user_by_username(username):
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def username_exists(username):
    conn = get_connection()
    row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return row is not None


def get_store(store_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def any_store_exists():
    """שימושי לדף הנחיתה - אם אין אף חנות עדיין, אפשר להראות הודעת 'ברוכים הבאים' שונה."""
    conn = get_connection()
    row = conn.execute("SELECT id FROM stores LIMIT 1").fetchone()
    conn.close()
    return row is not None


# ---------- הגדרות (לפי חנות) ----------

def get_setting(store_id, key, default=None):
    conn = get_connection()
    row = conn.execute("SELECT value FROM settings WHERE store_id=? AND key=?", (store_id, key)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(store_id, key, value):
    conn = get_connection()
    conn.execute(
        "INSERT INTO settings (store_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(store_id, key) DO UPDATE SET value = excluded.value",
        (store_id, key, str(value)),
    )
    conn.commit()
    conn.close()


# ---------- מוצרים ----------

def get_all_products(store_id, search=""):
    conn = get_connection()
    if search:
        q = f"%{search.lower()}%"
        rows = conn.execute(
            "SELECT * FROM products WHERE store_id=? AND (lower(name) LIKE ? OR barcode LIKE ?) ORDER BY name",
            (store_id, q, f"%{search}%"),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM products WHERE store_id=? ORDER BY name", (store_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_product_by_barcode(store_id, barcode):
    conn = get_connection()
    row = conn.execute("SELECT * FROM products WHERE store_id=? AND barcode=?", (store_id, barcode)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_product_by_id(store_id, product_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM products WHERE store_id=? AND id=?", (store_id, product_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_product(store_id, product):
    """יוצר מוצר חדש אם אין id, או מעדכן מוצר קיים (תמיד בתוך אותה חנות)."""
    conn = get_connection()
    if product.get("id"):
        conn.execute(
            "UPDATE products SET name=?, barcode=?, price=?, cost=?, qty=?, min_qty=?, category=? "
            "WHERE id=? AND store_id=?",
            (
                product["name"], product.get("barcode") or None, product["price"], product.get("cost"),
                product["qty"], product.get("min_qty", 3), product.get("category", "כללי"),
                product["id"], store_id,
            ),
        )
    else:
        product["id"] = uid()
        conn.execute(
            "INSERT INTO products (id, store_id, name, barcode, price, cost, qty, min_qty, category) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                product["id"], store_id, product["name"], product.get("barcode") or None, product["price"],
                product.get("cost"), product["qty"], product.get("min_qty", 3), product.get("category", "כללי"),
            ),
        )
    conn.commit()
    conn.close()
    return product["id"]


def delete_product(store_id, product_id):
    conn = get_connection()
    conn.execute("DELETE FROM products WHERE id=? AND store_id=?", (product_id, store_id))
    conn.commit()
    conn.close()


def barcode_exists(store_id, barcode, exclude_id=None):
    if not barcode:
        return False
    conn = get_connection()
    if exclude_id:
        row = conn.execute(
            "SELECT id FROM products WHERE store_id=? AND barcode=? AND id<>?", (store_id, barcode, exclude_id)
        ).fetchone()
    else:
        row = conn.execute("SELECT id FROM products WHERE store_id=? AND barcode=?", (store_id, barcode)).fetchone()
    conn.close()
    return row is not None


# ---------- מכירות ----------

class InsufficientStockError(Exception):
    pass


def record_sale(store_id, cart_items, payments, doc_type="חשבונית מס קבלה", discount_amount=0.0, customer_name=None):
    """
    cart_items: [{product_id, name, price, qty}, ...]  (product_id יכול להיות None לפריט חופשי)
    payments:   [{method, amount}, ...]
    מבצע הכל בטרנזקציה אחת: בודק מלאי, יוצר מסמך מכירה, יורד מלאי - הכל בתוך גבולות החנות הזו בלבד.
    """
    vat_rate = float(get_setting(store_id, "vat_rate", "17"))
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        for item in cart_items:
            if item.get("product_id"):
                row = conn.execute(
                    "SELECT qty FROM products WHERE id=? AND store_id=?", (item["product_id"], store_id)
                ).fetchone()
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
        invoice_no_row = conn.execute(
            "SELECT value FROM settings WHERE store_id=? AND key='next_invoice_no'", (store_id,)
        ).fetchone()
        invoice_no = int(invoice_no_row["value"]) if invoice_no_row else 1
        conn.execute(
            "INSERT INTO settings (store_id, key, value) VALUES (?, 'next_invoice_no', ?) "
            "ON CONFLICT(store_id, key) DO UPDATE SET value = excluded.value",
            (store_id, str(invoice_no + 1)),
        )

        conn.execute(
            "INSERT INTO sales (id, store_id, invoice_no, doc_type, status, raw_total, discount_amount, "
            "subtotal, vat_amount, vat_rate, total, refunded_total, customer_name, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sale_id, store_id, invoice_no, doc_type, "ok", raw_total, discount_amount,
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
                    "UPDATE products SET qty = qty - ? WHERE id = ? AND store_id=?",
                    (item["qty"], item["product_id"], store_id),
                )
        for p in payments:
            conn.execute(
                "INSERT INTO payments (id, sale_id, method, amount) VALUES (?,?,?,?)",
                (uid(), sale_id, p["method"], p["amount"]),
            )
        conn.commit()
        return get_sale(store_id, sale_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_sale(store_id, sale_id):
    conn = get_connection()
    sale = conn.execute("SELECT * FROM sales WHERE id=? AND store_id=?", (sale_id, store_id)).fetchone()
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


def recent_sales(store_id, limit=30):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM sales WHERE store_id=? ORDER BY created_at DESC LIMIT ?", (store_id, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def refund_sale(store_id, sale_id, items_to_return, reason=""):
    """items_to_return: [{sale_item_id, qty}, ...] - זיכוי מלא או חלקי, מוגבל למכירה בתוך אותה חנות."""
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        sale = conn.execute("SELECT * FROM sales WHERE id=? AND store_id=?", (sale_id, store_id)).fetchone()
        if not sale:
            raise ValueError("מכירה לא נמצאה")
        refund_total = 0.0
        for entry in items_to_return:
            si = conn.execute(
                "SELECT * FROM sale_items WHERE id=? AND sale_id=?", (entry["sale_item_id"], sale_id)
            ).fetchone()
            if not si:
                continue
            available = si["qty"] - si["returned_qty"]
            qty = min(entry["qty"], available)
            if qty <= 0:
                continue
            refund_total += si["price"] * qty
            conn.execute(
                "UPDATE sale_items SET returned_qty = returned_qty + ? WHERE id=?", (qty, si["id"])
            )
            if si["product_id"]:
                conn.execute(
                    "UPDATE products SET qty = qty + ? WHERE id=? AND store_id=?",
                    (qty, si["product_id"], store_id),
                )
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

def low_stock_products(store_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM products WHERE store_id=? AND qty <= min_qty ORDER BY qty", (store_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def today_summary(store_id):
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT * FROM sales WHERE store_id=? AND created_at LIKE ?", (store_id, f"{today}%")
    ).fetchall()
    conn.close()
    total = sum((r["total"] if r["status"] != "voided" else 0) - r["refunded_total"] for r in rows)
    count = sum(1 for r in rows if r["status"] != "voided")
    return {"total": total, "count": count}


def _period_profit(store_id, prefix):
    """רווח גולמי משוער לתקופה (יום/חודש) - לפי מחיר עלות נוכחי של המוצר, לא היסטורי."""
    conn = get_connection()
    sales_rows = conn.execute(
        "SELECT * FROM sales WHERE store_id=? AND created_at LIKE ?", (store_id, f"{prefix}%")
    ).fetchall()
    profit = 0.0
    for sale in sales_rows:
        items = conn.execute("SELECT * FROM sale_items WHERE sale_id=?", (sale["id"],)).fetchall()
        for item in items:
            if not item["product_id"]:
                continue
            p = conn.execute(
                "SELECT cost FROM products WHERE id=? AND store_id=?", (item["product_id"], store_id)
            ).fetchone()
            if not p or p["cost"] is None:
                continue
            qty = item["qty"] - item["returned_qty"]
            profit += qty * (item["price"] - p["cost"])
    conn.close()
    return profit


def today_profit(store_id):
    return _period_profit(store_id, datetime.now().strftime("%Y-%m-%d"))


def month_profit(store_id):
    return _period_profit(store_id, datetime.now().strftime("%Y-%m"))


# ---------- משמרות ----------

def current_shift(store_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM shifts WHERE store_id=? AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 1", (store_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def open_shift(store_id, opening_float):
    if current_shift(store_id):
        raise ValueError("כבר יש משמרת פתוחה")
    conn = get_connection()
    shift_id = uid()
    conn.execute(
        "INSERT INTO shifts (id, store_id, opened_at, opening_float) VALUES (?,?,?,?)",
        (shift_id, store_id, datetime.now().isoformat(timespec="seconds"), opening_float),
    )
    conn.commit()
    conn.close()
    return shift_id


def add_shift_movement(store_id, shift_id, kind, amount, note=""):
    conn = get_connection()
    # ודא שהמשמרת אכן שייכת לחנות הזו לפני שמוסיפים תנועה
    owns = conn.execute("SELECT id FROM shifts WHERE id=? AND store_id=?", (shift_id, store_id)).fetchone()
    if not owns:
        conn.close()
        raise ValueError("משמרת לא נמצאה")
    conn.execute(
        "INSERT INTO shift_movements (id, shift_id, kind, amount, note, created_at) VALUES (?,?,?,?,?,?)",
        (uid(), shift_id, kind, amount, note, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def expected_cash(shift):
    """קופה צפויה = פתיחה + מכירות מזומן מרגע הפתיחה - זיכויים במזומן + הפקדות - משיכות."""
    conn = get_connection()
    sales_rows = conn.execute(
        "SELECT * FROM sales WHERE store_id=? AND created_at >= ?", (shift["store_id"], shift["opened_at"])
    ).fetchall()
    cash_total = 0.0
    for sale in sales_rows:
        pays = conn.execute("SELECT * FROM payments WHERE sale_id=? AND method='cash'", (sale["id"],)).fetchall()
        cash_paid = sum(p["amount"] for p in pays)
        if sale["status"] == "voided":
            continue
        cash_total += cash_paid
        if sale["refunded_total"] > 0 and sale["total"] > 0:
            cash_ratio = cash_paid / sale["total"] if sale["total"] else 0
            cash_total -= sale["refunded_total"] * cash_ratio
    movements = conn.execute("SELECT * FROM shift_movements WHERE shift_id=?", (shift["id"],)).fetchall()
    moves_total = sum((m["amount"] if m["kind"] == "in" else -m["amount"]) for m in movements)
    conn.close()
    return shift["opening_float"] + cash_total + moves_total


def close_shift(store_id, shift_id, actual_cash):
    conn = get_connection()
    shift = conn.execute("SELECT * FROM shifts WHERE id=? AND store_id=?", (shift_id, store_id)).fetchone()
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


def recent_shifts(store_id, limit=20):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM shifts WHERE store_id=? AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?",
        (store_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- גיבוי ושחזור (מוגבל לחנות אחת) ----------

def export_all(store_id):
    conn = get_connection()
    sale_ids = [r["id"] for r in conn.execute("SELECT id FROM sales WHERE store_id=?", (store_id,)).fetchall()]
    shift_ids = [r["id"] for r in conn.execute("SELECT id FROM shifts WHERE store_id=?", (store_id,)).fetchall()]

    def in_clause(ids):
        return "(" + ",".join("?" * len(ids)) + ")" if ids else "(NULL)"

    sale_items = conn.execute(
        f"SELECT * FROM sale_items WHERE sale_id IN {in_clause(sale_ids)}", sale_ids
    ).fetchall() if sale_ids else []
    payments = conn.execute(
        f"SELECT * FROM payments WHERE sale_id IN {in_clause(sale_ids)}", sale_ids
    ).fetchall() if sale_ids else []
    shift_movements = conn.execute(
        f"SELECT * FROM shift_movements WHERE shift_id IN {in_clause(shift_ids)}", shift_ids
    ).fetchall() if shift_ids else []

    data = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "products": [dict(r) for r in conn.execute("SELECT * FROM products WHERE store_id=?", (store_id,)).fetchall()],
        "sales": [dict(r) for r in conn.execute("SELECT * FROM sales WHERE store_id=?", (store_id,)).fetchall()],
        "sale_items": [dict(r) for r in sale_items],
        "payments": [dict(r) for r in payments],
        "settings": {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings WHERE store_id=?", (store_id,)).fetchall()},
        "shifts": [dict(r) for r in conn.execute("SELECT * FROM shifts WHERE store_id=?", (store_id,)).fetchall()],
        "shift_movements": [dict(r) for r in shift_movements],
    }
    conn.close()
    return data


def restore_all(store_id, data):
    """מחליף את כל הנתונים הקיימים של החנות הזו בלבד בנתונים מהגיבוי. פעולה הרסנית."""
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        old_sale_ids = [r["id"] for r in conn.execute("SELECT id FROM sales WHERE store_id=?", (store_id,)).fetchall()]
        old_shift_ids = [r["id"] for r in conn.execute("SELECT id FROM shifts WHERE store_id=?", (store_id,)).fetchall()]
        if old_sale_ids:
            qs = ",".join("?" * len(old_sale_ids))
            conn.execute(f"DELETE FROM sale_items WHERE sale_id IN ({qs})", old_sale_ids)
            conn.execute(f"DELETE FROM payments WHERE sale_id IN ({qs})", old_sale_ids)
        if old_shift_ids:
            qs = ",".join("?" * len(old_shift_ids))
            conn.execute(f"DELETE FROM shift_movements WHERE shift_id IN ({qs})", old_shift_ids)
        conn.execute("DELETE FROM sales WHERE store_id=?", (store_id,))
        conn.execute("DELETE FROM shifts WHERE store_id=?", (store_id,))
        conn.execute("DELETE FROM products WHERE store_id=?", (store_id,))

        for p in data.get("products", []):
            conn.execute(
                "INSERT INTO products (id,store_id,name,barcode,price,cost,qty,min_qty,category) VALUES (?,?,?,?,?,?,?,?,?)",
                (p["id"], store_id, p["name"], p.get("barcode"), p["price"], p.get("cost"), p["qty"],
                 p.get("min_qty", 3), p.get("category", "כללי")),
            )
        for s in data.get("sales", []):
            conn.execute(
                "INSERT INTO sales (id,store_id,invoice_no,doc_type,status,raw_total,discount_amount,subtotal,"
                "vat_amount,vat_rate,total,refunded_total,customer_name,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s["id"], store_id, s["invoice_no"], s["doc_type"], s["status"], s["raw_total"], s["discount_amount"],
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
                "INSERT INTO shifts (id,store_id,opened_at,closed_at,opening_float,expected_cash,actual_cash) "
                "VALUES (?,?,?,?,?,?,?)",
                (sh["id"], store_id, sh["opened_at"], sh.get("closed_at"), sh["opening_float"],
                 sh.get("expected_cash"), sh.get("actual_cash")),
            )
        for m in data.get("shift_movements", []):
            conn.execute(
                "INSERT INTO shift_movements (id,shift_id,kind,amount,note,created_at) VALUES (?,?,?,?,?,?)",
                (m["id"], m["shift_id"], m["kind"], m["amount"], m.get("note", ""), m["created_at"]),
            )
        for k, v in data.get("settings", {}).items():
            conn.execute(
                "INSERT INTO settings (store_id,key,value) VALUES (?,?,?) "
                "ON CONFLICT(store_id, key) DO UPDATE SET value=excluded.value",
                (store_id, k, v),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
