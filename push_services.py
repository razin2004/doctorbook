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
    with open('vapid_keys.json', 'r') as f:
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
                        title = "⚠️ Token Skipped"
                        body = "Your token was skipped. Please contact the reception."
                        tag = "primecare-skip"
                        vibrate = [400, 200, 400]
                    elif status == "active":
                        if ahead == 0:
                            title = "🔔 Token Alert"
                            body = f"It's your turn! Proceed to {doctor_name} immediately."
                            tag = "primecare-alert"
                            vibrate = [300, 100, 300, 100, 500]
                        elif ahead == 1:
                            title = "🔔 Token Alert"
                            body = f"You're next for {doctor_name}. Please be ready!"
                            tag = "primecare-alert"
                            vibrate = [300, 100, 300, 100, 500]
                        elif ahead == 2:
                            title = "🔔 Token Alert"
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
