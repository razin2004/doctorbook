import sys
import os
import json
import time
from datetime import datetime, timedelta, date

# Add workspace path
sys.path.append(r"c:\Users\SLIM5\Desktop\doctorbook")

from app import app, db, ADMIN_EMAIL, admin_add_leave, admin_delete_leave

print("=== Starting direct request context tests ===")

# Test 1
past_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
with app.test_request_context(json={
    "combined": "Dr. John Doe - Cardiologist",
    "date": past_date,
    "reason": "Test past leave"
}):
    from flask import session
    session['admin_email'] = ADMIN_EMAIL
    
    print("Test 1: Rejects adding leave for past dates")
    res = admin_add_leave()
    data = res.get_json()
    print("Response data:", data)
    assert data['success'] is False
    assert "Leave cannot be set for past dates" in data['msg']
    print("Test 1 Passed!")

# Test 2
with app.test_request_context(json={
    "combined": "Dr. John Doe - Cardiologist",
    "date": past_date
}):
    from flask import session
    session['admin_email'] = ADMIN_EMAIL
    
    print("\nTest 2: Rejects deleting leave for past dates")
    res_del = admin_delete_leave()
    data_del = res_del.get_json()
    print("Response data:", data_del)
    assert data_del['success'] is False
    assert "Past leaves cannot be deleted" in data_del['msg']
    print("Test 2 Passed!")

# Test 3: Verify sorting of leaves
class MockWorksheet:
    def get_all_values(self):
        return [
            ["DoctorName", "Specialization", "Date", "Reason"],
            ["Dr. John Doe", "Cardiologist", "2026-06-05", "Finished leave 2 days ago"],
            ["Dr. John Doe", "Cardiologist", "2026-06-08", "Today's leave"],
            ["Dr. John Doe", "Cardiologist", "2026-06-15", "Future leave next week"],
            ["Dr. John Doe", "Cardiologist", "2026-06-07", "Finished leave yesterday"],
            ["Dr. John Doe", "Cardiologist", "2026-06-12", "Future leave this week"],
        ]

import app as app_module
original_get_leave_ws = app_module.get_leave_worksheet
app_module.get_leave_worksheet = lambda: MockWorksheet()
app_module.get_india_today = lambda: date(2026, 6, 8)

from app import admin_get_leaves

with app.test_request_context(query_string={"combined": "Dr. John Doe - Cardiologist"}):
    from flask import session
    session['admin_email'] = ADMIN_EMAIL
    
    print("\nTest 3: Verify sorting of leaves (upcoming ascending, past descending)")
    res_get = admin_get_leaves()
    data_get = res_get.get_json()
    print("Leaves returned:")
    for l in data_get['leaves']:
        print(l)
        
    dates_returned = [l['date'] for l in data_get['leaves']]
    expected_dates = ["2026-06-08", "2026-06-12", "2026-06-15", "2026-06-07", "2026-06-05"]
    assert dates_returned == expected_dates
    print("Test 3 Passed!")

app_module.get_leave_worksheet = original_get_leave_ws

# Test 4 & 5: Mocked Push notifications
import push_services
pushed_notifications = []

def mock_webpush(subscription_info, data, vapid_private_key, vapid_claims):
    pushed_notifications.append(json.loads(data))

original_webpush = push_services.webpush
push_services.webpush = mock_webpush

# Setup database fixtures for patient booking and subscription
with app.app_context():
    from app import User, PatientBooking, PushSubscription
    test_user = User.query.filter_by(email="testpatient@primecare.com").first()
    if not test_user:
        test_user = User(name="Test Patient", email="testpatient@primecare.com", password_hash="dummy_hash")
        db.session.add(test_user)
        db.session.commit()
        
    # Add booking for Dr. John Doe on June 15, 2026
    booking_date = "2026-06-15"
    test_booking = PatientBooking.query.filter_by(
        user_id=test_user.id,
        date=booking_date
    ).first()
    if not test_booking:
        test_booking = PatientBooking(
            user_id=test_user.id,
            doctor_name="Dr. John Doe",
            specialization="Cardiologist",
            patient_name="Test Patient",
            date=booking_date,
            time="10:00 AM",
            token=10
        )
        db.session.add(test_booking)
        db.session.commit()
        
    # Add push subscription for test user
    sub = PushSubscription.query.filter_by(user_id=test_user.id).first()
    if not sub:
        sub = PushSubscription(
            user_id=test_user.id,
            endpoint="https://fcm.googleapis.com/fcm/send/test-endpoint",
            p256dh="test-p256dh",
            auth="test-auth"
        )
        db.session.add(sub)
        db.session.commit()

from push_services import send_leave_notification, send_holiday_notification, send_leave_removal_notification, send_holiday_removal_notification

print("\nTest 4: Verify push notification triggered for doctor leave")
send_leave_notification("Dr. John Doe", "2026-06-15", "Cardiology conference", app, db, PatientBooking, PushSubscription)

# Wait a brief moment since it spawns a thread
time.sleep(1)
print("Notifications pushed:", pushed_notifications)
assert len(pushed_notifications) == 1
assert "Dr. John Doe is on temporary leave on 2026-06-15" in pushed_notifications[0]["body"]
print("Test 4 Passed!")

# Reset pushed_notifications list
pushed_notifications.clear()

print("\nTest 5: Verify push notification triggered for clinic holiday")
send_holiday_notification("2026-06-15", "Independence Day", app, db, PatientBooking, PushSubscription)
time.sleep(1)
print("Notifications pushed:", pushed_notifications)
assert len(pushed_notifications) == 1
assert "closed on 2026-06-15 due to a holiday" in pushed_notifications[0]["body"]
print("Test 5 Passed!")

# Reset pushed_notifications list
pushed_notifications.clear()

print("\nTest 6: Verify push notification triggered for doctor leave removal")
send_leave_removal_notification("Dr. John Doe", "2026-06-15", app, db, PatientBooking, PushSubscription)
time.sleep(1)
print("Notifications pushed:", pushed_notifications)
assert len(pushed_notifications) == 1
assert pushed_notifications[0]["title"] == "Doctor Leave Cancelled"
assert "Dr. John Doe's temporary leave on 2026-06-15 has been cancelled. Your appointment is now active." in pushed_notifications[0]["body"]
assert pushed_notifications[0]["tag"] == "leave-removal-Dr. John Doe-2026-06-15"
print("Test 6 Passed!")

# Reset pushed_notifications list
pushed_notifications.clear()

print("\nTest 7: Verify push notification triggered for clinic holiday removal")
send_holiday_removal_notification("2026-06-15", app, db, PatientBooking, PushSubscription)
time.sleep(1)
print("Notifications pushed:", pushed_notifications)
assert len(pushed_notifications) == 1
assert pushed_notifications[0]["title"] == "Clinic Holiday Cancelled"
assert "The clinic holiday on 2026-06-15 has been cancelled. The clinic will remain open, and your appointment is active." in pushed_notifications[0]["body"]
assert pushed_notifications[0]["tag"] == "holiday-removal-2026-06-15"
print("Test 7 Passed!")

# Restore original webpush
push_services.webpush = original_webpush
print("\n=== All verification tests completed successfully! ===")
