import json
from app import app, PushSubscription
from pywebpush import webpush, WebPushException

VAPID_CLAIMS = {"sub": "mailto:admin@primecare.com"}
VAPID_PRIVATE_KEY = ""

try:
    with open('vapid_keys.json', 'r') as f:
        keys = json.load(f)
        VAPID_PRIVATE_KEY = keys.get('private_key', '')
except Exception as e:
    print(f"Error loading VAPID keys: {e}")

def run_test():
    with app.app_context():
        subs = PushSubscription.query.all()
        if not subs:
            print("❌ No active push subscriptions found in database!")
            return
            
        print(f"📦 Found {len(subs)} subscription(s). Sending test push...")
        
        for idx, sub in enumerate(subs):
            push_info = {
                "endpoint": sub.endpoint,
                "keys": {
                    "p256dh": sub.p256dh,
                    "auth": sub.auth
                }
            }
            
            payload = json.dumps({
                "title": "🧪 Test Notification Server",
                "body": "If you are seeing this, background Web Push is fully functioning!",
                "tag": "primecare-test",
                "silent": False
            })
            
            try:
                webpush(
                    subscription_info=push_info,
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims=VAPID_CLAIMS
                )
                print(f"✅ Success sending to subscriber #{idx + 1}")
            except WebPushException as ex:
                print(f"❌ Failed to push subscriber #{idx + 1}: {repr(ex)}")

if __name__ == "__main__":
    if not VAPID_PRIVATE_KEY:
        print("❌ Cannot run test without VAPID keys configured.")
    else:
        run_test()
