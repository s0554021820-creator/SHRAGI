# -*- coding: utf-8 -*-
"""
app.py
שרת Flask מקומי למערכת קופה וניהול מלאי.
מתחבר למסד הנתונים (SQLite, דרך pos_database.py) ומגיש ממשק ווב יחיד (templates/index.html)
שמדבר עם השרת באמצעות API בפורמט JSON.

הרצה:
    pip install flask
    python app.py
ואז פותחים דפדפן בכתובת: http://127.0.0.1:5000
"""
from flask import Flask, render_template, request, jsonify

import pos_database as db
import payment_logic as pay
import printer

app = Flask(__name__)
db.init_db()


# ================= עמוד הבית =================

@app.route("/")
def index():
    return render_template("index.html")


# ================= API: מוצרים =================

@app.route("/api/products", methods=["GET"])
def api_products_list():
    search = request.args.get("q", "")
    return jsonify(db.get_all_products(search))


@app.route("/api/products/barcode/<barcode>", methods=["GET"])
def api_product_by_barcode(barcode):
    product = db.get_product_by_barcode(barcode)
    if not product:
        return jsonify({"error": "לא נמצא מוצר עם ברקוד זה"}), 404
    return jsonify(product)


@app.route("/api/products", methods=["POST"])
def api_product_save():
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
    if db.barcode_exists(barcode, exclude_id=data.get("id")):
        return jsonify({"error": "ברקוד זה כבר בשימוש עבור מוצר אחר"}), 400

    product = {
        "id": data.get("id"), "name": name, "barcode": barcode, "price": price,
        "cost": cost, "qty": qty, "min_qty": min_qty, "category": (data.get("category") or "כללי").strip(),
    }
    product_id = db.save_product(product)
    return jsonify(db.get_product_by_id(product_id))


@app.route("/api/products/<product_id>", methods=["DELETE"])
def api_product_delete(product_id):
    db.delete_product(product_id)
    return jsonify({"ok": True})


@app.route("/api/products/generate-barcode", methods=["GET"])
def api_generate_barcode():
    import time
    return jsonify({"barcode": "IN" + str(int(time.time() * 1000))[-9:]})


# ================= API: מכירה ותשלום =================

@app.route("/api/checkout", methods=["POST"])
def api_checkout():
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
        sale = db.record_sale(cart, payments, doc_type=doc_type, discount_amount=discount_amount,
                               customer_name=customer_name)
    except db.InsufficientStockError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # ניסיון הדפסה אמיתית - לא חוסם את התגובה אם אין מדפסת מוגדרת
    business_name = db.get_setting("business_name", "העסק שלי")
    printed = printer.print_receipt(sale, business_name)

    return jsonify({
        "sale": sale,
        "change_due": result["change_due"],
        "change_breakdown": result["change_breakdown"],
        "printed": printed,
    })


@app.route("/api/sales", methods=["GET"])
def api_sales_recent():
    limit = int(request.args.get("limit", 30))
    return jsonify(db.recent_sales(limit))


@app.route("/api/sales/<sale_id>", methods=["GET"])
def api_sale_detail(sale_id):
    sale = db.get_sale(sale_id)
    if not sale:
        return jsonify({"error": "מסמך לא נמצא"}), 404
    return jsonify(sale)


@app.route("/api/sales/<sale_id>/refund", methods=["POST"])
def api_sale_refund(sale_id):
    data = request.get_json(force=True)
    ok, err = _check_pin(data)
    if not ok:
        return jsonify({"error": err}), 403
    items = data.get("items") or []
    reason = data.get("reason", "")
    if not items:
        return jsonify({"error": "יש לבחור לפחות פריט אחד להחזרה"}), 400
    try:
        refund_total = db.refund_sale(sale_id, items, reason)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"refund_total": refund_total, "sale": db.get_sale(sale_id)})


# ================= API: דוחות =================

@app.route("/api/reports/today", methods=["GET"])
def api_reports_today():
    return jsonify(db.today_summary())


@app.route("/api/reports/low-stock", methods=["GET"])
def api_reports_low_stock():
    return jsonify(db.low_stock_products())


# ================= API: הגדרות =================

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify({
        "business_name": db.get_setting("business_name", ""),
        "vat_rate": db.get_setting("vat_rate", "17"),
        "admin_pin": db.get_setting("admin_pin", ""),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.get_json(force=True)
    try:
        vat = float(data.get("vat_rate"))
        if vat < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": 'יש להזין אחוז מע"מ תקין'}), 400
    db.set_setting("business_name", (data.get("business_name") or "").strip())
    db.set_setting("vat_rate", vat)
    db.set_setting("admin_pin", (data.get("admin_pin") or "").strip())
    return jsonify({"ok": True})


def _check_pin(data):
    """מחזיר (True, None) אם ה-PIN תקין או שאין הגנת PIN מוגדרת, אחרת (False, הודעת שגיאה)."""
    admin_pin = db.get_setting("admin_pin", "")
    if not admin_pin:
        return True, None
    if (data or {}).get("pin") != admin_pin:
        return False, "קוד PIN שגוי או חסר"
    return True, None


# ================= API: משמרות =================

@app.route("/api/shifts/current", methods=["GET"])
def api_shift_current():
    shift = db.current_shift()
    if not shift:
        return jsonify(None)
    shift["expected_cash_now"] = db.expected_cash(shift)
    return jsonify(shift)


@app.route("/api/shifts/open", methods=["POST"])
def api_shift_open():
    data = request.get_json(force=True)
    try:
        opening_float = float(data.get("opening_float") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "יש להזין סכום פתיחה תקין"}), 400
    try:
        shift_id = db.open_shift(opening_float)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": shift_id})


@app.route("/api/shifts/movement", methods=["POST"])
def api_shift_movement():
    data = request.get_json(force=True)
    shift = db.current_shift()
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
    db.add_shift_movement(shift["id"], kind, amount, data.get("note", ""))
    return jsonify({"ok": True})


@app.route("/api/shifts/close", methods=["POST"])
def api_shift_close():
    data = request.get_json(force=True)
    ok, err = _check_pin(data)
    if not ok:
        return jsonify({"error": err}), 403
    shift = db.current_shift()
    if not shift:
        return jsonify({"error": "אין משמרת פתוחה"}), 400
    try:
        actual_cash = float(data.get("actual_cash"))
    except (TypeError, ValueError):
        return jsonify({"error": "יש להזין סכום שנספר תקין"}), 400
    expected = db.close_shift(shift["id"], actual_cash)
    return jsonify({"expected_cash": expected, "actual_cash": actual_cash, "diff": actual_cash - expected})


@app.route("/api/shifts/recent", methods=["GET"])
def api_shifts_recent():
    return jsonify(db.recent_shifts(20))


# ================= API: דוחות מורחבים =================

@app.route("/api/reports/profit", methods=["GET"])
def api_reports_profit():
    return jsonify({"today": db.today_profit(), "month": db.month_profit()})


@app.route("/api/reports/export/sales.csv", methods=["GET"])
def api_export_sales_csv():
    import csv
    import io
    from urllib.parse import quote
    sales = db.recent_sales(100000)
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM כדי שאקסל יציג עברית נכון
    writer = csv.writer(buf)
    writer.writerow(["מספר מסמך", "תאריך ושעה", "סוג", "סכום", "סטטוס"])
    status_labels = {"ok": "תקינה", "voided": "זוכתה במלואה", "partial_refund": "זוכתה חלקית"}
    for s in sales:
        writer.writerow([s["invoice_no"], s["created_at"], s["doc_type"], s["total"],
                          status_labels.get(s["status"], s["status"])])
    # שם קובץ עברי לא חוקי בכותרת HTTP רגילה (ASCII בלבד) - קידוד RFC 5987 עם filename* פותר את זה
    fname = quote("מכירות.csv")
    return app.response_class(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=sales.csv; filename*=UTF-8''{fname}"},
    )


@app.route("/api/reports/export/inventory.csv", methods=["GET"])
def api_export_inventory_csv():
    import csv
    import io
    from urllib.parse import quote
    products = db.get_all_products()
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
def api_backup_export():
    return jsonify(db.export_all())


@app.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    data = request.get_json(force=True)
    ok, err = _check_pin(data)
    if not ok:
        return jsonify({"error": err}), 403
    backup = data.get("backup")
    if not backup or "products" not in backup or "sales" not in backup:
        return jsonify({"error": "קובץ גיבוי לא תקין"}), 400
    try:
        db.restore_all(backup)
    except Exception as e:
        return jsonify({"error": f"שגיאה בשחזור: {e}"}), 400
    return jsonify({"ok": True})


# ================= API: מחיקת מוצר (מוגן ב-PIN) =================

@app.route("/api/products/<product_id>/delete-with-pin", methods=["POST"])
def api_product_delete_with_pin(product_id):
    data = request.get_json(force=True) if request.data else {}
    ok, err = _check_pin(data)
    if not ok:
        return jsonify({"error": err}), 403
    db.delete_product(product_id)
    return jsonify({"ok": True})


# ================= הרצה =================

if __name__ == "__main__":
    # host="0.0.0.0" חושף את השרת לרשת המקומית (לא רק למחשב הזה) - כדי לגשת ממכשירים אחרים
    # debug=False למניעת קריסות שקטות של ה-reloader
    app.run(host="0.0.0.0", port=5000, debug=False)