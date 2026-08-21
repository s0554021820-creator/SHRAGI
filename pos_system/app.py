# -*- coding: utf-8 -*-
"""
app.py
שרת Flask למערכת קופה וניהול מלאי - Multi-Tenant: כל חנות פותחת חשבון נפרד
ורואה רק את הנתונים שלה. מגיש דף נחיתה ציבורי, הרשמה, התחברות, וממשק הקופה עצמו.

הרצה:
    pip install -r requirements.txt
    python app.py
ואז פותחים דפדפן בכתובת: http://127.0.0.1:5000
"""
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
import functools
import secrets
import os

import pos_database as db
import payment_logic as pay
import printer

app = Flask(__name__)
db.init_db()
app.secret_key = db.get_app_meta("flask_secret_key")
if not app.secret_key:
    app.secret_key = secrets.token_hex(32)
    db.set_app_meta("flask_secret_key", app.secret_key)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("store_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "יש להתחבר מחדש"}), 401
            return redirect(url_for("login"))
        state = db.subscription_state(session["store_id"])
        if not state["is_active"]:
            if request.path.startswith("/api/"):
                return jsonify({"error": "המנוי אינו פעיל - " + (state["reason"] or "")}), 402
            return redirect(url_for("subscription_locked"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/subscription-locked")
def subscription_locked():
    if not session.get("store_id"):
        return redirect(url_for("login"))
    state = db.subscription_state(session["store_id"])
    if state["is_active"]:
        return redirect(url_for("index"))
    return render_template("subscription_locked.html", reason=state["reason"], store=state.get("store"))


def current_store_id():
    return session["store_id"]


# ================= דף נחיתה ציבורי =================

@app.route("/")
def landing():
    if session.get("store_id"):
        return redirect(url_for("index"))
    return render_template("landing.html")


# ================= הרשמה =================

@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        store_name = request.form.get("store_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not store_name:
            error = "יש להזין שם עסק/חנות"
        elif not username or len(username) < 3:
            error = "שם משתמש חייב להיות באורך 3 תווים לפחות"
        elif len(password) < 4:
            error = "סיסמה חייבת להיות באורך 4 תווים לפחות"
        elif db.username_exists(username):
            error = "שם המשתמש הזה כבר תפוס - בחרו שם אחר"
        else:
            try:
                store_id, user_id = db.create_store_with_user(store_name, username, generate_password_hash(password))
                session["store_id"] = store_id
                session["user_id"] = user_id
                session["username"] = username
                return redirect(url_for("index"))
            except ValueError as e:
                error = str(e)
    return render_template("signup.html", error=error)


# ================= התחברות / התנתקות =================

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.get_user_by_username(username)
        if user and check_password_hash(user["password_hash"], password):
            session["store_id"] = user["store_id"]
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))
        error = "שם משתמש או סיסמה שגויים"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


# ================= פאנל בעל הפלטפורמה (ניהול מנויים ידני) =================
# מוגן במפתח סודי (לא סיסמת חנות) - נוצר אוטומטית בהפעלה ראשונה ומודפס ללוגים של השרת.

def get_owner_secret():
    secret = db.get_app_meta("owner_secret")
    if not secret:
        secret = secrets.token_hex(16)
        db.set_app_meta("owner_secret", secret)
        print(f"\n>>> מפתח ניהול הפלטפורמה שלך (owner secret): {secret}\n"
              f">>> שמור את זה במקום בטוח - גישה לכתובת /owner עם מפתח זה מאפשרת לראות ולנהל את כל החנויות.\n")
    return secret


@app.route("/owner/login", methods=["GET", "POST"])
def owner_login():
    error = None
    if request.method == "POST":
        key = request.form.get("key", "").strip()
        if key == get_owner_secret():
            session["is_owner"] = True
            return redirect(url_for("owner_panel"))
        error = "מפתח שגוי"
    return render_template("owner_login.html", error=error)


def owner_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_owner"):
            return redirect(url_for("owner_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/owner")
@owner_required
def owner_panel():
    return render_template("owner_panel.html", stores=db.all_stores())


@app.route("/owner/subscription/<store_id>", methods=["POST"])
@owner_required
def owner_set_subscription(store_id):
    action = request.form.get("action")
    if action == "extend":
        db.set_subscription(store_id, "active", extend_days=30)
    elif action == "expire":
        db.set_subscription(store_id, "expired")
    elif action == "cancel":
        db.set_subscription(store_id, "cancelled")
    return redirect(url_for("owner_panel"))


# ================= עמוד הקופה עצמו =================

@app.route("/app")
@login_required
def index():
    store = db.get_store(current_store_id())
    return render_template("index.html", store_name=store["name"] if store else "")


@app.route("/sw.js")
def service_worker():
    """Service Worker למצב לא מקוון - מוגש מהשורש (לא מ-/static/) כדי לקבל היקף שליטה על כל האתר."""
    js = """
const CACHE_NAME = 'arnet-pos-v1';
const SHELL_URL = '/app';

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.add(SHELL_URL).catch(() => {}))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

// אסטרטגיה: לנסות תמיד רשת קודם (מידע עדכני), ואם אין רשת - ליפול חזרה לעותק השמור של דף הקופה.
// בקשות API (fetch-נתונים) לא נשמרות במטמון - הן מנוהלות בנפרד ב-localStorage בתוך העמוד עצמו.
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (url.pathname !== SHELL_URL) return;  // רק דף הקופה עצמו נשמר במטמון, לא ה-API

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(SHELL_URL, clone));
        return response;
      })
      .catch(() => caches.match(SHELL_URL))
  );
});
"""
    return app.response_class(js, mimetype="application/javascript")


# ================= API: מוצרים =================

@app.route("/api/products", methods=["GET"])
@login_required
def api_products_list():
    search = request.args.get("q", "")
    return jsonify(db.get_all_products(current_store_id(), search))


@app.route("/api/products/barcode/<barcode>", methods=["GET"])
@login_required
def api_product_by_barcode(barcode):
    product = db.get_product_by_barcode(current_store_id(), barcode)
    if not product:
        return jsonify({"error": "לא נמצא מוצר עם ברקוד זה"}), 404
    return jsonify(product)


@app.route("/api/products", methods=["POST"])
@login_required
def api_product_save():
    store_id = current_store_id()
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    barcode = (data.get("barcode") or "").strip() or None
    try:
        price = float(data.get("price"))
        qty = float(data.get("qty"))
        min_qty = float(data.get("min_qty") or 3)
        cost = float(data["cost"]) if data.get("cost") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "יש למלא מחיר וכמות תקינים"}), 400

    if not name or price < 0 or qty < 0:
        return jsonify({"error": "יש למלא שם, מחיר וכמות תקינים"}), 400
    if db.barcode_exists(store_id, barcode, exclude_id=data.get("id")):
        return jsonify({"error": "ברקוד זה כבר בשימוש עבור מוצר אחר"}), 400

    product = {
        "id": data.get("id"), "name": name, "barcode": barcode, "price": price,
        "cost": cost, "qty": qty, "min_qty": min_qty, "category": (data.get("category") or "כללי").strip(),
    }
    product_id = db.save_product(store_id, product)
    return jsonify(db.get_product_by_id(store_id, product_id))


@app.route("/api/products/<product_id>", methods=["DELETE"])
@login_required
def api_product_delete(product_id):
    db.delete_product(current_store_id(), product_id)
    return jsonify({"ok": True})


@app.route("/api/products/generate-barcode", methods=["GET"])
@login_required
def api_generate_barcode():
    import time
    return jsonify({"barcode": "IN" + str(int(time.time() * 1000))[-9:]})


# ================= API: מכירה ותשלום =================

@app.route("/api/checkout", methods=["POST"])
@login_required
def api_checkout():
    store_id = current_store_id()
    data = request.get_json(force=True)
    cart = data.get("cart") or []
    payments = data.get("payments") or []
    discount_amount = float(data.get("discount_amount") or 0)
    doc_type = data.get("doc_type") or "חשבונית מס קבלה"
    customer_name = data.get("customer_name") or None

    if not cart:
        return jsonify({"error": "הסל ריק"}), 400

    raw_total = sum(i["price"] * i["qty"] for i in cart)
    total_due = max(0.0, raw_total - discount_amount)

    result = pay.process_payment(total_due, payments)
    if not result["ok"]:
        return jsonify({"error": result["error"]}), 400

    try:
        sale = db.record_sale(store_id, cart, payments, doc_type=doc_type, discount_amount=discount_amount,
                               customer_name=customer_name)
    except db.InsufficientStockError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    business_name = db.get_setting(store_id, "business_name", "העסק שלי")
    printed = printer.print_receipt(sale, business_name)

    return jsonify({
        "sale": sale,
        "change_due": result["change_due"],
        "change_breakdown": result["change_breakdown"],
        "printed": printed,
    })


@app.route("/api/sales", methods=["GET"])
@login_required
def api_sales_recent():
    limit = int(request.args.get("limit", 30))
    return jsonify(db.recent_sales(current_store_id(), limit))


@app.route("/api/sales/<sale_id>", methods=["GET"])
@login_required
def api_sale_detail(sale_id):
    sale = db.get_sale(current_store_id(), sale_id)
    if not sale:
        return jsonify({"error": "מסמך לא נמצא"}), 404
    return jsonify(sale)


@app.route("/api/sales/<sale_id>/refund", methods=["POST"])
@login_required
def api_sale_refund(sale_id):
    store_id = current_store_id()
    data = request.get_json(force=True)
    ok, err = _check_pin(store_id, data)
    if not ok:
        return jsonify({"error": err}), 403
    items = data.get("items") or []
    reason = data.get("reason", "")
    if not items:
        return jsonify({"error": "יש לבחור לפחות פריט אחד להחזרה"}), 400
    try:
        refund_total = db.refund_sale(store_id, sale_id, items, reason)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"refund_total": refund_total, "sale": db.get_sale(store_id, sale_id)})


# ================= API: דוחות =================

@app.route("/api/reports/today", methods=["GET"])
@login_required
def api_reports_today():
    return jsonify(db.today_summary(current_store_id()))


@app.route("/api/reports/low-stock", methods=["GET"])
@login_required
def api_reports_low_stock():
    return jsonify(db.low_stock_products(current_store_id()))


# ================= API: הגדרות =================

@app.route("/api/settings", methods=["GET"])
@login_required
def api_settings_get():
    store_id = current_store_id()
    return jsonify({
        "business_name": db.get_setting(store_id, "business_name", ""),
        "vat_rate": db.get_setting(store_id, "vat_rate", "17"),
        "admin_pin": db.get_setting(store_id, "admin_pin", ""),
        "offline_mode_enabled": db.get_setting(store_id, "offline_mode_enabled", "0") == "1",
    })


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings_save():
    store_id = current_store_id()
    data = request.get_json(force=True)
    try:
        vat = float(data.get("vat_rate"))
        if vat < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": 'יש להזין אחוז מע"מ תקין'}), 400
    db.set_setting(store_id, "business_name", (data.get("business_name") or "").strip())
    db.set_setting(store_id, "vat_rate", vat)
    db.set_setting(store_id, "admin_pin", (data.get("admin_pin") or "").strip())
    db.set_setting(store_id, "offline_mode_enabled", "1" if data.get("offline_mode_enabled") else "0")
    return jsonify({"ok": True})


def _check_pin(store_id, data):
    """מחזיר (True, None) אם ה-PIN תקין או שאין הגנת PIN מוגדרת, אחרת (False, הודעת שגיאה)."""
    admin_pin = db.get_setting(store_id, "admin_pin", "")
    if not admin_pin:
        return True, None
    if (data or {}).get("pin") != admin_pin:
        return False, "קוד PIN שגוי או חסר"
    return True, None


# ================= API: משמרות =================

@app.route("/api/shifts/current", methods=["GET"])
@login_required
def api_shift_current():
    shift = db.current_shift(current_store_id())
    if not shift:
        return jsonify(None)
    shift["expected_cash_now"] = db.expected_cash(shift)
    return jsonify(shift)


@app.route("/api/shifts/open", methods=["POST"])
@login_required
def api_shift_open():
    data = request.get_json(force=True)
    try:
        opening_float = float(data.get("opening_float") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "יש להזין סכום פתיחה תקין"}), 400
    try:
        shift_id = db.open_shift(current_store_id(), opening_float)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": shift_id})


@app.route("/api/shifts/movement", methods=["POST"])
@login_required
def api_shift_movement():
    store_id = current_store_id()
    data = request.get_json(force=True)
    shift = db.current_shift(store_id)
    if not shift:
        return jsonify({"error": "אין משמרת פתוחה"}), 400
    try:
        amount = float(data.get("amount"))
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "יש להזין סכום תקין"}), 400
    kind = data.get("kind")
    if kind not in ("in", "out"):
        return jsonify({"error": "סוג תנועה לא תקין"}), 400
    db.add_shift_movement(store_id, shift["id"], kind, amount, data.get("note", ""))
    return jsonify({"ok": True})


@app.route("/api/shifts/close", methods=["POST"])
@login_required
def api_shift_close():
    store_id = current_store_id()
    data = request.get_json(force=True)
    ok, err = _check_pin(store_id, data)
    if not ok:
        return jsonify({"error": err}), 403
    shift = db.current_shift(store_id)
    if not shift:
        return jsonify({"error": "אין משמרת פתוחה"}), 400
    try:
        actual_cash = float(data.get("actual_cash"))
    except (TypeError, ValueError):
        return jsonify({"error": "יש להזין סכום שנספר תקין"}), 400
    expected = db.close_shift(store_id, shift["id"], actual_cash)
    return jsonify({"expected_cash": expected, "actual_cash": actual_cash, "diff": actual_cash - expected})


@app.route("/api/shifts/recent", methods=["GET"])
@login_required
def api_shifts_recent():
    return jsonify(db.recent_shifts(current_store_id(), 20))


# ================= API: דוחות מורחבים =================

@app.route("/api/reports/profit", methods=["GET"])
@login_required
def api_reports_profit():
    store_id = current_store_id()
    return jsonify({"today": db.today_profit(store_id), "month": db.month_profit(store_id)})


@app.route("/api/reports/export/sales.csv", methods=["GET"])
@login_required
def api_export_sales_csv():
    import csv
    import io
    from urllib.parse import quote
    sales = db.recent_sales(current_store_id(), 100000)
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM כדי שאקסל יציג עברית נכון
    writer = csv.writer(buf)
    writer.writerow(["מספר מסמך", "תאריך ושעה", "סוג", "סכום", "סטטוס"])
    status_labels = {"ok": "תקינה", "voided": "זוכתה במלואה", "partial_refund": "זוכתה חלקית"}
    for s in sales:
        writer.writerow([s["invoice_no"], s["created_at"], s["doc_type"], s["total"],
                          status_labels.get(s["status"], s["status"])])
    fname = quote("מכירות.csv")
    return app.response_class(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=sales.csv; filename*=UTF-8''{fname}"},
    )


@app.route("/api/reports/export/inventory.csv", methods=["GET"])
@login_required
def api_export_inventory_csv():
    import csv
    import io
    from urllib.parse import quote
    products = db.get_all_products(current_store_id())
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["שם מוצר", "ברקוד", "מחיר מכירה", "מחיר עלות", "כמות במלאי", "סף מלאי נמוך", "קטגוריה"])
    for p in products:
        writer.writerow([p["name"], p["barcode"] or "", p["price"], p["cost"] if p["cost"] is not None else "",
                          p["qty"], p["min_qty"], p["category"]])
    fname = quote("מלאי.csv")
    return app.response_class(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=inventory.csv; filename*=UTF-8''{fname}"},
    )


# ================= API: גיבוי ושחזור =================

@app.route("/api/backup/export", methods=["GET"])
@login_required
def api_backup_export():
    return jsonify(db.export_all(current_store_id()))


@app.route("/api/backup/restore", methods=["POST"])
@login_required
def api_backup_restore():
    store_id = current_store_id()
    data = request.get_json(force=True)
    ok, err = _check_pin(store_id, data)
    if not ok:
        return jsonify({"error": err}), 403
    backup = data.get("backup")
    if not backup or "products" not in backup or "sales" not in backup:
        return jsonify({"error": "קובץ גיבוי לא תקין"}), 400
    try:
        db.restore_all(store_id, backup)
    except Exception as e:
        return jsonify({"error": f"שגיאה בשחזור: {e}"}), 400
    return jsonify({"ok": True})


# ================= API: מחיקת מוצר (מוגן ב-PIN) =================

@app.route("/api/products/<product_id>/delete-with-pin", methods=["POST"])
@login_required
def api_product_delete_with_pin(product_id):
    store_id = current_store_id()
    data = request.get_json(force=True) if request.data else {}
    ok, err = _check_pin(store_id, data)
    if not ok:
        return jsonify({"error": err}), 403
    db.delete_product(store_id, product_id)
    return jsonify({"ok": True})


# ================= הרצה =================

if __name__ == "__main__":
    get_owner_secret()  # מייצר ומדפיס את המפתח בפעם הראשונה שהשרת עולה
    port = int(os.environ.get("PORT", 5000))
    try:
        from waitress import serve
        print(f"מריץ עם שרת ייצור (waitress) על פורט {port} ...")
        serve(app, host="0.0.0.0", port=port)
    except ImportError:
        print("waitress לא מותקן (pip install waitress) - מריץ עם שרת הפיתוח של Flask בינתיים.")
        app.run(host="0.0.0.0", port=port, debug=False)
