# -*- coding: utf-8 -*-
"""
payment_logic.py
לוגיקת תשלום, פיצול תשלומים וחישוב עודף עם פילוח שטרות/מטבעות.
פונקציות טהורות, ללא תלות ב-UI או במסד נתונים - קל לבדוק בנפרד.
"""

ILS_DENOMINATIONS = [200, 100, 50, 20, 10, 5, 2, 1, 0.5, 0.1]  # ש"ח


def breakdown_change(amount):
    """מפרק סכום עודף לשטרות ומטבעות. עובד באגורות כדי להימנע משגיאות נקודה צפה."""
    remaining = round(amount * 100)
    result = []
    for denom in ILS_DENOMINATIONS:
        denom_cents = round(denom * 100)
        count = remaining // denom_cents
        if count > 0:
            result.append({"denom": denom, "count": count})
            remaining -= count * denom_cents
    return result


def process_payment(total_due, payments):
    """
    payments: [{"method": "cash"/"credit"/"check"/"store_credit", "amount": float}, ...]
    מחזיר dict: {"ok": bool, "error": str|None, "change_due": float|None, "change_breakdown": [...]}
    """
    total_due_cents = round(total_due * 100)
    paid_cents = round(sum(p["amount"] for p in payments) * 100)

    non_cash_cents = round(sum(p["amount"] for p in payments if p["method"] != "cash") * 100)
    cash_cents = round(sum(p["amount"] for p in payments if p["method"] == "cash") * 100)

    # תשלום שאינו מזומן (אשראי/צ'ק/הקפה) לא יכול "לתת עודף" - הוא לא אמור לחרוג מסך החוב בעצמו.
    # מזומן כן יכול לחרוג (וזה בדיוק המקרה הרגיל של תשלום עם עודף), ולכן לא נכלל בבדיקה הזו.
    if non_cash_cents > total_due_cents + 1:
        return {"ok": False, "error": "סכום התשלום הלא-מזומן חורג מסך החוב"}

    if paid_cents < total_due_cents:
        missing = (total_due_cents - paid_cents) / 100
        return {"ok": False, "error": f"חסרים {missing:.2f} ₪ להשלמת התשלום"}

    change_cents = paid_cents - total_due_cents
    if change_cents == 0:
        return {"ok": True, "error": None, "change_due": 0.0, "change_breakdown": []}

    if change_cents > 0 and cash_cents < change_cents:
        return {"ok": False, "error": "לא ניתן להחזיר עודף - סכום המזומן ששולם קטן מהעודף המחושב"}

    change_due = change_cents / 100
    return {"ok": True, "error": None, "change_due": change_due, "change_breakdown": breakdown_change(change_due)}


def format_breakdown(breakdown):
    """הופך פירוק עודף לטקסט קריא, למשל: 'שטר 20 ₪ x1, מטבע 2 ₪ x1'"""
    parts = []
    for b in breakdown:
        kind = "שטר" if b["denom"] >= 20 else "מטבע"
        val = int(b["denom"]) if b["denom"] == int(b["denom"]) else b["denom"]
        parts.append(f'{kind} {val} ₪ x{b["count"]}')
    return ", ".join(parts) if parts else "אין עודף"