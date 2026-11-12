"""
tests/test_whatsapp_notifications.py
====================================
Comprehensive Test Suite for Phase 4C:
  1. Two-Way Saved Patients (WhatsApp <-> Web Portal Sync & Quick Booking Selection)
  2. Smart Pre-Consultation & Queue WhatsApp Notifications
  3. Custom Target Token Alert (REMIND <token>)
  4. Rolling 2-Tokens-Ahead, Live Call, Skip, and Thank You Alerts
  5. 1-Hour & 10-Minute Pre-Consultation Scheduled Alerts
  6. Malayalam and English Parity
"""

import os
import unittest
from datetime import datetime, timedelta
import pytz
from unittest.mock import patch, MagicMock

import tests.test_isolation
from app import (
    app, db, User, Doctor, DoctorSchedule, DoctorSession, PatientBooking,
    BookingNotificationTracker, SavedPatient, WhatsAppConversation,
    WhatsAppMessage, WhatsAppWebhookEvent, WhatsAppBookingSession,
    get_or_create_whatsapp_patient_user
)
from services.whatsapp_patient_mgmt import (
    get_saved_patients_for_phone,
    save_patient_for_phone,
    format_saved_patients_list,
    format_patient_selection_prompt,
    handle_whatsapp_patient_mgmt,
    parse_add_patient_command
)
from services.whatsapp_notification_service import (
    notify_patient_called,
    notify_patient_approaching,
    notify_patient_custom_target,
    notify_patient_skipped,
    notify_patient_consultation_completed,
    handle_custom_reminder_command,
    check_and_send_scheduled_pre_consultation_reminders,
    _get_or_create_tracker
)
from services.whatsapp_booking_flow import handle_whatsapp_booking_flow
from services.whatsapp_webhook_service import dispatch_inbound_message_reply


class WhatsAppNotificationsTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_isolation.initialize_test_database(app, db)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['KAPSO_OUTBOUND_ENABLED'] = False
        cls.client = app.test_client()

    def setUp(self):
        self.app_context = app.app_context()
        self.app_context.push()
        self.session = db.session

        # Clean tables
        self.session.query(WhatsAppMessage).delete()
        self.session.query(WhatsAppBookingSession).delete()
        self.session.query(WhatsAppWebhookEvent).delete()
        self.session.query(WhatsAppConversation).delete()
        self.session.query(BookingNotificationTracker).delete()
        self.session.query(PatientBooking).delete()
        self.session.query(SavedPatient).delete()
        self.session.query(DoctorSession).delete()
        self.session.commit()

        # Setup test doctor
        self.doc = Doctor.query.get(1)
        if not self.doc:
            self.doc = Doctor(
                id=1,
                name="Dr. Arshitha Abdurahman",
                specialization="Gynecologist",
                is_active=True,
                max_daily_patients=30
            )
            self.session.add(self.doc)
            self.session.commit()

        # Doctor schedules (Mon-Sun)
        existing_schedules = DoctorSchedule.query.filter_by(doctor_id=1).all()
        if not existing_schedules:
            for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']:
                sch = DoctorSchedule(
                    doctor_id=1,
                    day_of_week=day,
                    start_time="09:00",
                    end_time="13:00",
                    is_available=True
                )
                self.session.add(sch)
            self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.query(WhatsAppMessage).delete()
        self.session.query(WhatsAppBookingSession).delete()
        self.session.query(WhatsAppWebhookEvent).delete()
        self.session.query(WhatsAppConversation).delete()
        self.session.query(BookingNotificationTracker).delete()
        self.session.query(PatientBooking).delete()
        self.session.query(SavedPatient).delete()
        self.session.query(DoctorSession).delete()
        self.session.commit()
        self.app_context.pop()

    def _create_convo(self, phone="+919847055001"):
        import uuid
        convo = WhatsAppConversation(
            sender_phone=phone,
            conversation_uuid=str(uuid.uuid4()),
            status="active"
        )
        self.session.add(convo)
        self.session.commit()
        return convo

    def _get_or_create_user(self, phone="+919847055001", name="Test Patient"):
        from app import get_or_create_whatsapp_patient_user
        return get_or_create_whatsapp_patient_user(phone, name=name)

    # =====================================================================
    # 1. TWO-WAY SAVED PATIENTS TESTS
    # =====================================================================

    def test_save_patient_for_phone_and_web_api_sync(self):
        """WhatsApp saved patient is immediately accessible in Web Portal API."""
        phone = "+91 98470 55001"
        res = save_patient_for_phone(
            phone_str=phone,
            name="Rahul Sharma",
            age="28",
            gender="Male",
            patient_phone="+919847055001",
            relationship="Self"
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["patient"]["name"], "Rahul Sharma")

        # Verify User record was auto-provisioned
        user = get_or_create_whatsapp_patient_user(phone)
        self.assertIsNotNone(user)

        # Verify SavedPatient retrieved via helper
        saved = get_saved_patients_for_phone(phone)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["name"], "Rahul Sharma")
        self.assertEqual(saved[0]["age"], "28")
        self.assertEqual(saved[0]["gender"], "Male")

        # Verify Web Portal API access (/api/saved_patients) with user in session
        with self.client.session_transaction() as sess:
            sess['user_id'] = user.id
            sess['user_role'] = 'patient'

        resp = self.client.get('/api/saved_patients')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['patients']), 1)
        self.assertEqual(data['patients'][0]['name'], "Rahul Sharma")

    def test_malayalam_transliteration_in_saved_patient(self):
        """Malayalam names and genders are saved in pure English in DB."""
        phone = "+91 98470 55002"
        res = save_patient_for_phone(
            phone_str=phone,
            name="രാഹുൽ",
            age="32",
            gender="പുരുഷൻ",
            patient_phone=phone,
            relationship="Self"
        )
        self.assertTrue(res["success"])
        # Transliterated name should be in English Latin alphabet
        saved_name = res["patient"]["name"]
        self.assertTrue(all(ord(c) < 128 for c in saved_name.replace(" ", "")))
        self.assertEqual(res["patient"]["gender"], "Male")

    def test_whatsapp_patient_mgmt_commands(self):
        """Tests 'Patients', 'My Patients', and 'ADD PATIENT' WhatsApp commands."""
        phone = "+91 98470 55003"
        # 1. Empty patients list
        res_empty = handle_whatsapp_patient_mgmt("Patients", phone, user_lang="en")
        self.assertTrue(res_empty["handled"])
        self.assertIn("do not have any saved patient profiles", res_empty["reply"])

        # 2. Add patient command
        add_cmd = "ADD PATIENT Ananya, 24, Female, SAME, Sister"
        res_add = handle_whatsapp_patient_mgmt(add_cmd, phone, user_lang="en")
        self.assertTrue(res_add["handled"])
        self.assertIn("Ananya", res_add["reply"])
        self.assertIn("saved successfully", res_add["reply"])

        # 3. Add Malayalam patient command
        add_ml = "ADD PATIENT ഫാത്തിമ, 26, സ്ത്രീ, SAME, Wife"
        res_add_ml = handle_whatsapp_patient_mgmt(add_ml, phone, user_lang="ml")
        self.assertTrue(res_add_ml["handled"])
        self.assertIn("വിജയകരമായി സേവ് ചെയ്തു", res_add_ml["reply"])

        # 4. View patients in Malayalam
        res_view_ml = handle_whatsapp_patient_mgmt("എന്റെ രോഗികൾ", phone, user_lang="ml")
        self.assertTrue(res_view_ml["handled"])
        self.assertIn("സേവ് ചെയ്ത രോഗികൾ", res_view_ml["reply"])

    def test_booking_flow_patient_selection_by_index(self):
        """User can select from saved patients via 1-digit index during booking."""
        phone = "919847055004"
        convo = self._create_convo(phone)

        # Pre-save 2 patients
        save_patient_for_phone(phone, "Rahul Sharma", "29", "Male", relationship="Self")
        save_patient_for_phone(phone, "Priya Sharma", "26", "Female", relationship="Wife")

        # Create active booking session at PATIENT_DETAILS
        ist = pytz.timezone('Asia/Kolkata')
        tomorrow = (datetime.now(ist) + timedelta(days=1)).strftime("%Y-%m-%d")
        b_session = WhatsAppBookingSession(
            conversation_id=convo.id,
            doctor_id=1,
            doctor_name="Dr. Arshitha Abdurahman",
            specialization="Gynecologist",
            appointment_date=tomorrow,
            time_slot="09:00 - 13:00",
            step='PATIENT_DETAILS',
            status='in_progress',
            contact_phone=phone
        )
        self.session.add(b_session)
        self.session.commit()

        # User replies "2" to choose Priya Sharma
        res = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="2",
            sender_phone=phone,
            sender_role="patient",
            user_lang="en"
        )
        self.assertTrue(res["handled"])
        self.assertEqual(b_session.step, 'CONFIRMATION')
        self.assertEqual(b_session.patient_name, "Priya Sharma")
        self.assertEqual(b_session.patient_gender, "Female")
        self.assertIn("Priya Sharma", res["reply"])

    def test_booking_flow_auto_saves_new_patient(self):
        """Typing new patient details in booking flow auto-saves profile for web sync."""
        phone = "919847055005"
        convo = self._create_convo(phone)

        ist = pytz.timezone('Asia/Kolkata')
        tomorrow = (datetime.now(ist) + timedelta(days=1)).strftime("%Y-%m-%d")
        b_session = WhatsAppBookingSession(
            conversation_id=convo.id,
            doctor_id=1,
            doctor_name="Dr. Arshitha Abdurahman",
            specialization="Gynecologist",
            appointment_date=tomorrow,
            time_slot="09:00 - 13:00",
            step='PATIENT_DETAILS',
            status='in_progress',
            contact_phone=phone
        )
        self.session.add(b_session)
        self.session.commit()

        # User types: "Vivek Kumar, 35, Male, SAME"
        res = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="Vivek Kumar, 35, Male, SAME",
            sender_phone=phone,
            sender_role="patient",
            user_lang="en"
        )
        self.assertTrue(res["handled"])
        self.assertEqual(b_session.step, 'CONFIRMATION')
        self.assertEqual(b_session.patient_name, "Vivek Kumar")

        # Verify auto-saved in SavedPatient table
        saved = get_saved_patients_for_phone(phone)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["name"], "Vivek Kumar")
        self.assertEqual(saved[0]["age"], "35")

    # =====================================================================
    # 2. QUEUE MOVEMENT NOTIFICATIONS (CALL, AHEAD, SKIP, COMPLETE)
    # =====================================================================

    @patch("services.kapso_client.KapsoClient.send_text_message")
    def test_live_patient_called_alert(self, mock_send):
        """Dispatches 'Dr. is ready to see you now' WhatsApp alert to the called token."""
        mock_send.return_value = {"success": True, "status": "sent"}
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        phone = "+919847055010"
        user = self._get_or_create_user(phone, name="Sneha Roy")

        booking = PatientBooking(
            user_id=user.id,
            patient_name="Sneha Roy",
            phone_number=phone,
            doctor_name="Dr. Arshitha Abdurahman",
            specialization="Gynecologist",
            date=today_str,
            time="09:00 - 13:00",
            token=3,
            status="confirmed"
        )
        self.session.add(booking)
        self.session.commit()

        notify_patient_called(booking, "Dr. Arshitha Abdurahman")
        self.assertTrue(mock_send.called)
        sent_body = mock_send.call_args[1]["text_body"]
        self.assertIn("ready to see you now", sent_body)
        self.assertIn("Token #3", sent_body)

        # Idempotency check: Calling again should NOT dispatch another message
        mock_send.reset_mock()
        notify_patient_called(booking, "Dr. Arshitha Abdurahman")
        self.assertFalse(mock_send.called)

    @patch("services.kapso_client.KapsoClient.send_text_message")
    def test_rolling_2_tokens_ahead_alert(self, mock_send):
        """Dispatches rolling 2-tokens-ahead warning to Token N+2."""
        mock_send.return_value = {"success": True, "status": "sent"}
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        phone = "+919847055011"
        user = self._get_or_create_user(phone, name="Deepak Menon")

        booking = PatientBooking(
            user_id=user.id,
            patient_name="Deepak Menon",
            phone_number=phone,
            doctor_name="Dr. Arshitha Abdurahman",
            specialization="Gynecologist",
            date=today_str,
            time="09:00 - 13:00",
            token=5,
            status="confirmed"
        )
        self.session.add(booking)
        self.session.commit()

        # Doctor is consulting Token 3 -> Token 5 receives 2-ahead alert
        notify_patient_approaching(booking, "Dr. Arshitha Abdurahman", current_token=3)
        self.assertTrue(mock_send.called)
        sent_body = mock_send.call_args[1]["text_body"]
        self.assertIn("2 patient(s) ahead", sent_body)
        self.assertIn("#5", sent_body)

    @patch("services.kapso_client.KapsoClient.send_text_message")
    def test_skipped_and_consultation_completed_alerts(self, mock_send):
        """Dispatches skipped notice and post-consultation thank you notes."""
        mock_send.return_value = {"success": True, "status": "sent"}
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        phone = "+919847055012"
        user = self._get_or_create_user(phone, name="Ajay Varma")

        b_skip = PatientBooking(
            user_id=user.id,
            patient_name="Ajay Varma",
            phone_number=phone,
            doctor_name="Dr. Arshitha Abdurahman",
            specialization="Gynecologist",
            date=today_str,
            time="09:00 - 13:00",
            token=4,
            status="confirmed"
        )
        self.session.add(b_skip)
        self.session.commit()

        # Test skipped alert
        notify_patient_skipped(b_skip, "Dr. Arshitha Abdurahman")
        self.assertTrue(mock_send.called)
        sent_body = mock_send.call_args[1]["text_body"]
        self.assertIn("skipped", sent_body.lower())

        mock_send.reset_mock()

        # Test consultation completed alert
        notify_patient_consultation_completed(b_skip, "Dr. Arshitha Abdurahman")
        self.assertTrue(mock_send.called)
        sent_body = mock_send.call_args[1]["text_body"]
        self.assertIn("Thank You", sent_body)

    # =====================================================================
    # 3. CUSTOM REMINDER COMMAND (REMIND <token>)
    # =====================================================================

    def test_custom_reminder_command(self):
        """User can set a custom target token reminder (e.g. REMIND 3)."""
        phone = "+919847055020"
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        user = self._get_or_create_user(phone, name="Manju K")

        # Booking with Token 6
        booking = PatientBooking(
            user_id=user.id,
            patient_name="Manju K",
            phone_number=phone,
            doctor_name="Dr. Arshitha Abdurahman",
            specialization="Gynecologist",
            date=today_str,
            time="09:00 - 13:00",
            token=6,
            status="confirmed"
        )
        self.session.add(booking)
        self.session.commit()

        # 1. Invalid target token (>= 6)
        res_inv = handle_custom_reminder_command("REMIND 6", phone, user_lang="en")
        self.assertTrue(res_inv["handled"])
        self.assertIn("between 1 and your token #6", res_inv["reply"])

        # 2. Valid target token (REMIND 3)
        res_val = handle_custom_reminder_command("REMIND 3", phone, user_lang="en")
        self.assertTrue(res_val["handled"])
        self.assertIn("Custom Alert Set", res_val["reply"])
        self.assertIn("Token #3", res_val["reply"])

        # Verify DB tracker
        tracker = BookingNotificationTracker.query.filter_by(booking_id=booking.id).first()
        self.assertIsNotNone(tracker)
        self.assertEqual(tracker.custom_target_token, 3)
        self.assertFalse(tracker.custom_reminded)

    def test_token_one_cannot_set_custom_reminder(self):
        """Token #1 is informed that no custom reminder is needed."""
        phone = "+919847055021"
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        user = self._get_or_create_user(phone, name="First Patient")

        booking = PatientBooking(
            user_id=user.id,
            patient_name="First Patient",
            phone_number=phone,
            doctor_name="Dr. Arshitha Abdurahman",
            specialization="Gynecologist",
            date=today_str,
            time="09:00 - 13:00",
            token=1,
            status="confirmed"
        )
        self.session.add(booking)
        self.session.commit()

        res = handle_custom_reminder_command("REMIND 1", phone, user_lang="en")
        self.assertTrue(res["handled"])
        self.assertIn("token is #1", res["reply"])

    # =====================================================================
    # 4. SCHEDULED PRE-CONSULTATION REMINDERS (1-HR & 10-MIN)
    # =====================================================================

    @patch("services.kapso_client.KapsoClient.send_text_message")
    def test_scheduled_1hour_and_10min_reminders(self, mock_send):
        """Tests 1-hour pre-consultation reminder and 10-minute early warning."""
        mock_send.return_value = {"success": True, "status": "sent"}
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        today_str = now_ist.strftime("%Y-%m-%d")
        weekday = now_ist.strftime("%A")

        # Configure doctor schedule to start 40 minutes in future (triggers 1-hr reminder)
        start_in_40m = (now_ist + timedelta(minutes=40)).strftime("%H:%M")
        sch = DoctorSchedule.query.filter_by(doctor_id=1, day_of_week=weekday).first()
        sch.start_time = start_in_40m
        sch.end_time = "18:00"
        self.session.commit()

        # Token 1 and Token 3 bookings
        phone1 = "+919847055031"
        phone3 = "+919847055032"
        u1 = self._get_or_create_user(phone1, name="Patient One")
        u3 = self._get_or_create_user(phone3, name="Patient Three")

        b1 = PatientBooking(
            user_id=u1.id,
            patient_name="Patient One",
            phone_number=phone1,
            doctor_name="Dr. Arshitha Abdurahman",
            specialization="Gynecologist",
            date=today_str,
            time="09:00 - 13:00",
            token=1,
            status="confirmed"
        )
        b3 = PatientBooking(
            user_id=u3.id,
            patient_name="Patient Three",
            phone_number=phone3,
            doctor_name="Dr. Arshitha Abdurahman",
            specialization="Gynecologist",
            date=today_str,
            time="09:00 - 13:00",
            token=3,
            status="confirmed"
        )
        self.session.add_all([b1, b3])
        self.session.commit()

        # Run scheduled reminder evaluation
        check_and_send_scheduled_pre_consultation_reminders()

        # Both Token 1 and Token 3 should receive 1-hour reminders
        self.assertEqual(mock_send.call_count, 2)

        # Token 1 message should NOT have custom reminder prompt
        call_b1 = [c for c in mock_send.call_args_list if c[1]["recipient_phone"] == "+919847055031"][0]
        self.assertNotIn("REMIND <token>", call_b1[1]["text_body"])

        # Token 3 message SHOULD have custom reminder prompt
        call_b3 = [c for c in mock_send.call_args_list if c[1]["recipient_phone"] == "+919847055032"][0]
        self.assertIn("REMIND <token>", call_b3[1]["text_body"])

        # Test 10-Minute early warning for Tokens 1 and 2
        mock_send.reset_mock()
        # Shift start time to 5 minutes in future (triggers 10-min warning)
        start_in_5m = (now_ist + timedelta(minutes=5)).strftime("%H:%M")
        sch.start_time = start_in_5m
        self.session.commit()

        check_and_send_scheduled_pre_consultation_reminders()

        # Token 1 receives 10-min warning, Token 3 does NOT
        self.assertEqual(mock_send.call_count, 1)
        call_10m = mock_send.call_args_list[0]
        self.assertEqual(call_10m[1]["recipient_phone"], "+919847055031")
        self.assertIn("10 minutes", call_10m[1]["text_body"])

    def test_webhook_e2e_patients_and_custom_remind(self):
        """End-to-end webhook dispatch for patient management and custom reminders."""
        phone = "+919847055040"
        convo = self._create_convo(phone)
        u = self._get_or_create_user(phone, name="E2E Patient")

        # 1. Dispatch "ADD PATIENT Aisha, 22, Female, SAME, Daughter"
        evt1 = WhatsAppWebhookEvent(event_id="evt-1", event_type="messages", payload_json="{}")
        self.session.add(evt1)
        self.session.commit()

        msg1 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid-1",
            direction="inbound",
            message_type="text",
            content="ADD PATIENT Aisha, 22, Female, SAME, Daughter"
        )
        self.session.add(msg1)
        self.session.commit()

        res1 = dispatch_inbound_message_reply(
            conversation_id=convo.id,
            inbound_msg_id=msg1.id,
            webhook_event_id=evt1.id,
            message={"text": {"body": "ADD PATIENT Aisha, 22, Female, SAME, Daughter"}},
            sender_phone=phone,
            scoped_user_id=None,
            parent_scoped_id=None,
            contact_name="E2E Patient"
        )
        self.assertTrue(res1.get("patient_mgmt_handled"))
        self.assertIn("Aisha", res1.get("reply"))

        # 2. Dispatch "Patients" (view list)
        evt2 = WhatsAppWebhookEvent(event_id="evt-2", event_type="messages", payload_json="{}")
        self.session.add(evt2)
        self.session.commit()

        msg2 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid-2",
            direction="inbound",
            message_type="text",
            content="Patients"
        )
        self.session.add(msg2)
        self.session.commit()

        res2 = dispatch_inbound_message_reply(
            conversation_id=convo.id,
            inbound_msg_id=msg2.id,
            webhook_event_id=evt2.id,
            message={"text": {"body": "Patients"}},
            sender_phone=phone,
            scoped_user_id=None,
            parent_scoped_id=None,
            contact_name="E2E Patient"
        )
        self.assertTrue(res2.get("patient_mgmt_handled"))
        self.assertIn("Aisha", res2.get("reply"))

        # 3. Create active booking for today (Token 4) and dispatch "REMIND 2"
        ist = pytz.timezone('Asia/Kolkata')
        today_str = datetime.now(ist).strftime("%Y-%m-%d")

        b4 = PatientBooking(
            user_id=u.id,
            patient_name="E2E Patient",
            phone_number=phone,
            doctor_name="Dr. Arshitha Abdurahman",
            specialization="Gynecologist",
            date=today_str,
            time="09:00 - 13:00",
            token=4,
            status="confirmed"
        )
        self.session.add(b4)
        self.session.commit()

        evt3 = WhatsAppWebhookEvent(event_id="evt-3", event_type="messages", payload_json="{}")
        self.session.add(evt3)
        self.session.commit()

        msg3 = WhatsAppMessage(
            conversation_id=convo.id,
            wamid="wamid-3",
            direction="inbound",
            message_type="text",
            content="REMIND 2"
        )
        self.session.add(msg3)
        self.session.commit()

        res3 = dispatch_inbound_message_reply(
            conversation_id=convo.id,
            inbound_msg_id=msg3.id,
            webhook_event_id=evt3.id,
            message={"text": {"body": "REMIND 2"}},
            sender_phone=phone,
            scoped_user_id=None,
            parent_scoped_id=None,
            contact_name="E2E Patient"
        )
        self.assertTrue(res3.get("reminder_handled"))
        self.assertIn("Custom Alert Set", res3.get("reply"))


if __name__ == '__main__':
    unittest.main()
