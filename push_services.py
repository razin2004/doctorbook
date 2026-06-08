import os
import json
import threading
from datetime import datetime
from pywebpush import webpush, WebPushException

# Load VAPID private key
VAPID_CLAIMS = {
    "sub": "mailto:admin@primecare.com"
}
VAPID_PRIVATE_KEY = ""

try:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    VAPID_KEYS_PATH = os.path.join(APP_DIR, 'vapid_keys.json')
    with open(VAPID_KEYS_PATH, 'r') as f:
        keys = json.load(f)
        VAPID_PRIVATE_KEY = keys.get('private_key', '')
except Exception as e:
    print(f"[Push Service] Warning: Could not load VAPID keys: {e}")

def trigger_push(doctor_name, date_str, current_token, status, app, db, PatientBooking, PushSubscription):
    """
    Spawns a background thread to identify and send push notifications
    to patients who are exactly next or 2 tokens away, or skipped.
    """
    if not VAPID_PRIVATE_KEY:
        print("[Push Service] No VAPID keys installed. Aborting push.")
        return

    def run_push():
        with app.app_context():
            try:
                # 1. Find all active bookings for this doctor today
                bookings = PatientBooking.query.filter_by(
                    doctor_name=doctor_name,
                    date=date_str
                ).all()

                for b in bookings:
                    token = b.token
                    if token < current_token and status != 'skipped':
                        continue # past token

                    ahead = token - current_token

                    title = ""
                    body = ""
                    tag = ""
                    vibrate = []
                    
                    if status == "skipped" and ahead == 0:
                        # The particular token being evaluated was skipped!
                        title = "Token Skipped"
                        body = "Your token was skipped. Please contact the reception."
                        tag = "primecare-skip"
                        vibrate = [400, 200, 400]
                    elif status == "active":
                        if ahead == 0:
                            title = "Token Alert"
                            body = f"It's your turn! Proceed to {doctor_name} immediately."
                            tag = "primecare-alert"
                            vibrate = [300, 100, 300, 100, 500]
                        elif ahead == 1:
                            title = "Token Alert"
                            body = f"You're next for {doctor_name}. Please be ready!"
                            tag = "primecare-alert"
                            vibrate = [300, 100, 300, 100, 500]
                        elif ahead == 2:
                            title = "Token Alert"
                            body = f"2 patients ahead — {doctor_name}. Get ready soon."
                            tag = "primecare-alert"
                            vibrate = [300, 100, 300, 100, 500]

                    if not title:
                        continue # Not in alert range
                    
                    # Target subscriber
                    # Note: We push to ALL devices logged in as this user_id
                    subs = PushSubscription.query.filter_by(user_id=b.user_id).all()
                    
                    for sub in subs:
                        push_info = {
                            "endpoint": sub.endpoint,
                            "keys": {
                                "p256dh": sub.p256dh,
                                "auth": sub.auth
                            }
                        }
                        
                        payload = json.dumps({
                            "title": title,
                            "body": body,
                            "tag": tag,
                            "vibrate": vibrate,
                            "silent": False
                        })
                        
                        try:
                            webpush(
                                subscription_info=push_info,
                                data=payload,
                                vapid_private_key=VAPID_PRIVATE_KEY,
                                vapid_claims=VAPID_CLAIMS
                            )
                        except WebPushException as ex:
                            if ex.response and ex.response.status_code in [404, 410]:
                                # Subscription expired or unsubscribed
                                print(f"[Push Service] Unsubscribed endpoint {sub.endpoint}. Deleting...")
                                db.session.delete(sub)
                                db.session.commit()
                            else:
                                print(f"[Push Service] WebPush Error: {repr(ex)}")

            except Exception as e:
                print(f"[Push Service] Worker Error: {e}")

    thread = threading.Thread(target=run_push)
    thread.start()

def send_leave_notification(doctor_name, date_str, reason, app, db, PatientBooking, PushSubscription):
    """
    Finds bookings for `doctor_name` on `date_str` and sends a web push notification
    alerting the patients that the doctor is on temporary leave.
    """
    if not VAPID_PRIVATE_KEY:
        print("[Push Service] No VAPID keys installed. Aborting push.")
        return

    def run_push():
        with app.app_context():
            try:
                # Find all active bookings for this doctor on this date
                bookings = PatientBooking.query.filter(
                    db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_name.strip().lower(),
                    PatientBooking.date == date_str,
                    PatientBooking.status != 'cancelled'
                ).all()

                for b in bookings:
                    title = "Doctor on Leave"
                    doc_disp = doctor_name.strip()
                    if not doc_disp.lower().startswith("dr."):
                        doc_disp = f"Dr. {doc_disp}"
                    body = f"{doc_disp} is on temporary leave on {date_str}. Please reschedule/book another day."
                    tag = f"leave-{doctor_name}-{date_str}"
                    
                    subs = PushSubscription.query.filter_by(user_id=b.user_id).all() if b.user_id else []
                    for sub in subs:
                        push_info = {
                            "endpoint": sub.endpoint,
                            "keys": {
                                "p256dh": sub.p256dh,
                                "auth": sub.auth
                            }
                        }
                        payload = json.dumps({
                            "title": title,
                            "body": body,
                            "tag": tag,
                            "vibrate": [300, 100, 300],
                            "silent": False
                        })
                        try:
                            webpush(
                                subscription_info=push_info,
                                data=payload,
                                vapid_private_key=VAPID_PRIVATE_KEY,
                                vapid_claims=VAPID_CLAIMS
                            )
                        except WebPushException as ex:
                            if ex.response and ex.response.status_code in [404, 410]:
                                print(f"[Push Service] Unsubscribed endpoint {sub.endpoint}. Deleting...")
                                db.session.delete(sub)
                                db.session.commit()
                            else:
                                print(f"[Push Service] WebPush Error: {repr(ex)}")
            except Exception as e:
                print(f"[Push Service] Leave Notification Error: {e}")

    thread = threading.Thread(target=run_push)
    thread.start()

def send_holiday_notification(date_str, reason, app, db, PatientBooking, PushSubscription):
    """
    Finds bookings on `date_str` and sends a web push notification
    alerting the patients that the clinic is closed due to a holiday.
    """
    if not VAPID_PRIVATE_KEY:
        print("[Push Service] No VAPID keys installed. Aborting push.")
        return

    def run_push():
        with app.app_context():
            try:
                # Find all active bookings on this date
                bookings = PatientBooking.query.filter(
                    PatientBooking.date == date_str,
                    PatientBooking.status != 'cancelled'
                ).all()

                for b in bookings:
                    title = "Clinic Closed (Holiday)"
                    body = f"The clinic is closed on {date_str} due to a holiday ({reason or 'General Holiday'}). Please reschedule/book another day."
                    tag = f"holiday-{date_str}"
                    
                    subs = PushSubscription.query.filter_by(user_id=b.user_id).all() if b.user_id else []
                    for sub in subs:
                        push_info = {
                            "endpoint": sub.endpoint,
                            "keys": {
                                "p256dh": sub.p256dh,
                                "auth": sub.auth
                            }
                        }
                        payload = json.dumps({
                            "title": title,
                            "body": body,
                            "tag": tag,
                            "vibrate": [300, 100, 300],
                            "silent": False
                        })
                        try:
                            webpush(
                                subscription_info=push_info,
                                data=payload,
                                vapid_private_key=VAPID_PRIVATE_KEY,
                                vapid_claims=VAPID_CLAIMS
                            )
                        except WebPushException as ex:
                            if ex.response and ex.response.status_code in [404, 410]:
                                print(f"[Push Service] Unsubscribed endpoint {sub.endpoint}. Deleting...")
                                db.session.delete(sub)
                                db.session.commit()
                            else:
                                print(f"[Push Service] WebPush Error: {repr(ex)}")
            except Exception as e:
                print(f"[Push Service] Holiday Notification Error: {e}")

    thread = threading.Thread(target=run_push)
    thread.start()

def send_leave_removal_notification(doctor_name, date_str, app, db, PatientBooking, PushSubscription):
    """
    Finds bookings for `doctor_name` on `date_str` and sends a web push notification
    alerting the patients that the doctor's leave was removed/cancelled.
    """
    if not VAPID_PRIVATE_KEY:
        print("[Push Service] No VAPID keys installed. Aborting push.")
        return

    def run_push():
        with app.app_context():
            try:
                bookings = PatientBooking.query.filter(
                    db.func.lower(db.func.trim(PatientBooking.doctor_name)) == doctor_name.strip().lower(),
                    PatientBooking.date == date_str,
                    PatientBooking.status != 'cancelled'
                ).all()

                for b in bookings:
                    title = "Doctor Leave Cancelled"
                    doc_disp = doctor_name.strip()
                    if not doc_disp.lower().startswith("dr."):
                        doc_disp = f"Dr. {doc_disp}"
                    body = f"{doc_disp}'s temporary leave on {date_str} has been cancelled. Your appointment is now active."
                    tag = f"leave-removal-{doctor_name}-{date_str}"
                    
                    subs = PushSubscription.query.filter_by(user_id=b.user_id).all() if b.user_id else []
                    for sub in subs:
                        push_info = {
                            "endpoint": sub.endpoint,
                            "keys": {
                                "p256dh": sub.p256dh,
                                "auth": sub.auth
                            }
                        }
                        payload = json.dumps({
                            "title": title,
                            "body": body,
                            "tag": tag,
                            "vibrate": [300, 100, 300],
                            "silent": False
                        })
                        try:
                            webpush(
                                subscription_info=push_info,
                                data=payload,
                                vapid_private_key=VAPID_PRIVATE_KEY,
                                vapid_claims=VAPID_CLAIMS
                            )
                        except WebPushException as ex:
                            if ex.response and ex.response.status_code in [404, 410]:
                                print(f"[Push Service] Unsubscribed endpoint {sub.endpoint}. Deleting...")
                                db.session.delete(sub)
                                db.session.commit()
                            else:
                                print(f"[Push Service] WebPush Error: {repr(ex)}")
            except Exception as e:
                print(f"[Push Service] Leave Removal Notification Error: {e}")

    thread = threading.Thread(target=run_push)
    thread.start()

def send_holiday_removal_notification(date_str, app, db, PatientBooking, PushSubscription):
    """
    Finds bookings on `date_str` and sends a web push notification
    alerting the patients that the clinic holiday was removed/cancelled.
    """
    if not VAPID_PRIVATE_KEY:
        print("[Push Service] No VAPID keys installed. Aborting push.")
        return

    def run_push():
        with app.app_context():
            try:
                bookings = PatientBooking.query.filter(
                    PatientBooking.date == date_str,
                    PatientBooking.status != 'cancelled'
                ).all()

                for b in bookings:
                    title = "Clinic Holiday Cancelled"
                    body = f"The clinic holiday on {date_str} has been cancelled. The clinic will remain open, and your appointment is active."
                    tag = f"holiday-removal-{date_str}"
                    
                    subs = PushSubscription.query.filter_by(user_id=b.user_id).all() if b.user_id else []
                    for sub in subs:
                        push_info = {
                            "endpoint": sub.endpoint,
                            "keys": {
                                "p256dh": sub.p256dh,
                                "auth": sub.auth
                            }
                        }
                        payload = json.dumps({
                            "title": title,
                            "body": body,
                            "tag": tag,
                            "vibrate": [300, 100, 300],
                            "silent": False
                        })
                        try:
                            webpush(
                                subscription_info=push_info,
                                data=payload,
                                vapid_private_key=VAPID_PRIVATE_KEY,
                                vapid_claims=VAPID_CLAIMS
                            )
                        except WebPushException as ex:
                            if ex.response and ex.response.status_code in [404, 410]:
                                print(f"[Push Service] Unsubscribed endpoint {sub.endpoint}. Deleting...")
                                db.session.delete(sub)
                                db.session.commit()
                            else:
                                print(f"[Push Service] WebPush Error: {repr(ex)}")
            except Exception as e:
                print(f"[Push Service] Holiday Removal Notification Error: {e}")

    thread = threading.Thread(target=run_push)
    thread.start()
