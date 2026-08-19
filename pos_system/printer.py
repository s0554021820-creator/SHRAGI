# -*- coding: utf-8 -*-
"""
printer.py
חיבור אופציונלי למדפסת קבלות ESC/POS + מגירת כסף, דרך החבילה python-escpos.
אם החבילה לא מותקנת, או שאין מדפסת מחוברת, הפונקציות פשוט מחזירות False
והתוכנה ממשיכה לעבוד רגיל עם תצוגת קבלה על המסך בלבד.

להתקנה (רק אם יש מדפסת פיזית מחוברת ב-USB):
    pip install python-escpos pyusb
"""

try:
    from escpos.printer import Usb
    ESCPOS_AVAILABLE = True
except ImportError:
    ESCPOS_AVAILABLE = False


def print_receipt(sale, business_name, business_address="", business_id="", vendor_id=None, product_id=None):
    """
    מנסה להדפיס קבלה למדפסת USB אמיתית.
    vendor_id / product_id: מזהי USB של המדפסת (מתקבלים מ-`lsusb` בלינוקס או מנהל ההתקנים בווינדוס).
    מחזיר True אם ההדפסה הצליחה, False אחרת (ואז יש להציג את הקבלה על המסך במקום).
    """
    if not ESCPOS_AVAILABLE or vendor_id is None or product_id is None:
        return False
    try:
        p = Usb(vendor_id, product_id, 0)
        p.set(align="center", bold=True, width=2, height=2)
        p.text(business_name + "\n")
        p.set(align="center", bold=False, width=1, height=1)
        if business_address:
            p.text(business_address + "\n")
        if business_id:
            p.text(f"עוסק מורשה {business_id}\n")
        p.text("-" * 32 + "\n")
        p.set(align="right")
        for item in sale["items"]:
            line_total = item["price"] * item["qty"]
            p.text(f'{item["name"]}  x{item["qty"]}  {line_total:.2f} ₪\n')
        p.text("-" * 32 + "\n")
        p.set(bold=True)
        p.text(f'סה"כ לתשלום: {sale["total"]:.2f} ₪\n')
        p.set(bold=False)
        p.text(f'מע"מ ({sale["vat_rate"]}%): {sale["vat_amount"]:.2f} ₪\n')
        p.set(align="center")
        p.text("תודה על הקנייה\n")
        p.cut()
        p.cashdraw(2)  # פותח את מגירת הכסף המחוברת דרך המדפסת (פין 2)
        return True
    except Exception as e:
        print(f"שגיאת הדפסה: {e}")
        return False


def kick_cash_drawer(vendor_id=None, product_id=None):
    """פותח את מגירת הכסף בלבד, ללא הדפסה - לתחילת יום או פתיחה ידנית."""
    if not ESCPOS_AVAILABLE or vendor_id is None or product_id is None:
        return False
    try:
        p = Usb(vendor_id, product_id, 0)
        p.cashdraw(2)
        return True
    except Exception as e:
        print(f"שגיאת פתיחת מגירה: {e}")
        return False
