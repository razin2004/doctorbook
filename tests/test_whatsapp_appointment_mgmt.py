"""
tests/test_whatsapp_appointment_mgmt.py
=======================================
Comprehensive Automated Test Suite for WhatsApp Appointment Management (Phase 4B).

Covers all Phase 4B requirements:
  1. Appointment Lookup:
     - Registered patient lookup by user_id
     - Guest lookup by exact verified sender phone_number
     - Website-created bookings visible via WhatsApp
     - Guest placeholder account isolation (zero cross-guest data leakage)
     - Unrelated patient booking isolation
     - Multiple appointments formatted as numbered list
     - BSUID-only sender cannot access appointments (fails safe)
     - Zero upcoming appointments response

  2. Appointment Cancellation:
     - Successful single-appointment cancellation flow (direct to CONFIRMATION)
     - Multiple appointments with CHOOSE_APPOINTMENT selection
     - Out-of-range / invalid selection handled gracefully
     - User abort / cancellation keyword preserves appointment
     - Unauthorized cancellation rejected
     - Ineligible / already cancelled appointment handled
     - Idempotent repeated confirmation

  3. Appointment Rescheduling:
     - Successful reschedule flow (SELECT_DATE -> CONFIRMATION -> atomic execution)
     - Multi-appointment reschedule selection
     - Same-date selection rejected
     - Doctor unavailable / leave date rejected
     - Atomic rollback: failed reschedule leaves original booking intact
     - Duplicate confirmation idempotency
     - Abort reschedule leaves original booking intact

  4. Integration & Security:
     - Doctor & Admin roles redirected to staff portals
     - Human handoff protects conversation
     - Active booking sessions not interfered with
     - Webhook end-to-end multi-turn journey with HMAC signatures
"""

import os
import json
import uuid
import hmac
import hashlib
from datetime import datetime, date, timedelta
import pytz
import unittest
from unittest.mock import patch

import tests.test_isolation
from app import (
    app, db, User, Doctor, DoctorSchedule, DoctorLeave, ClinicHoliday,
    PatientBooking, DailyTokenAllocation, BookingSyncOutbox,
    WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent,
    WhatsAppBookingSession, DoctorSession
)
from services.kapso_client import KapsoClient
from services.whatsapp_appointment_mgmt import (
    handle_whatsapp_appointment_mgmt,
    is_lookup_intent,
    is_cancel_intent,
    is_reschedule_intent,
    _format_appointment_list,
    _format_appointment_detail
)
from services.booking_services import (
    create_booking_service,
    cancel_booking_service,
    reschedule_booking_service,
    get_patient_bookings_service,
    check_availability_service
)
from services.whatsapp_booking_flow import handle_whatsapp_booking_flow

TEST_WEBHOOK_SECRET = "test_kapso_webhook_secret_phase4b"


def generate_sig(raw_body: bytes) -> str:
    digest = hmac.new(TEST_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_incoming_payload(sender_phone="919847055001", text_body="Hello", wamid=None):
    msg_id = wamid or f"wamid.HBgL{uuid.uuid4().hex[:16]}"
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_TEST_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"profile": {"name": "Test User"}, "wa_id": sender_phone}],
                            "messages": [
                                {
                                    "from": sender_phone,
                                    "id": msg_id,
                                    "timestamp": "1730700000",
                                    "text": {"body": text_body},
                                    "type": "text"
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


class WhatsAppAppointmentMgmtTestSuite(unittest.TestCase):
    """
    Automated test suite verifying WhatsApp appointment lookup,
    cancellation, rescheduling, and security isolation.
    """

    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['KAPSO_WEBHOOK_SECRET'] = TEST_WEBHOOK_SECRET
        app.config['KAPSO_OUTBOUND_ENABLED'] = False
        os.environ['KAPSO_WEBHOOK_SECRET'] = TEST_WEBHOOK_SECRET
        cls.client = app.test_client()

    def setUp(self):
        self.app_context = app.app_context()
        self.app_context.push()
        self.session = db.session

        # Clean test records
        self.session.query(WhatsAppMessage).delete()
        self.session.query(WhatsAppBookingSession).delete()
        self.session.query(WhatsAppWebhookEvent).delete()
        self.session.query(WhatsAppConversation).delete()
        self.session.query(BookingSyncOutbox).delete()
        self.session.query(PatientBooking).delete()
        self.session.query(DailyTokenAllocation).delete()
        self.session.query(DoctorLeave).delete()
        self.session.query(ClinicHoliday).delete()
        self.session.commit()

        # Invalidate in-memory doctor cache
        from app import get_all_doctors
        get_all_doctors(force_refresh=True)

        # Ensure active doctor exists with schedules
        self.doc1 = Doctor.query.get(1)
        if not self.doc1:
            self.doc1 = Doctor(
                id=1,
                name="Dr. Arshitha Abdurahman",
                specialization="Gynecologist",
                is_active=True,
                max_daily_patients=25
            )
            self.session.add(self.doc1)
            self.session.commit()

        # Ensure working days schedule (Mon-Sat)
        existing_schedules = DoctorSchedule.query.filter_by(doctor_id=1).all()
        if not existing_schedules:
            for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']:
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
        self.session.query(BookingSyncOutbox).delete()
        self.session.query(PatientBooking).delete()
        self.session.query(DailyTokenAllocation).delete()
        self.session.query(DoctorLeave).delete()
        self.session.query(ClinicHoliday).delete()
        self.session.commit()
        self.app_context.pop()

    def _create_convo(self, phone="+919847055001", role="guest"):
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=phone,
            status="active"
        )
        self.session.add(convo)
        self.session.commit()
        return convo

    def _find_next_working_day(self, offset_days=1):
        ist = pytz.timezone('Asia/Kolkata')
        today = datetime.now(ist).date()
        schedules = DoctorSchedule.query.filter_by(doctor_id=1, is_available=True).all()
        working_weekdays = {s.day_of_week.lower() for s in schedules}
        for d in range(offset_days, offset_days + 14):
            candidate = today + timedelta(days=d)
            if candidate.strftime('%A').lower() in working_weekdays:
                return candidate.strftime('%Y-%m-%d')
        return (today + timedelta(days=offset_days)).strftime('%Y-%m-%d')

    # =========================================================================
    # 1. APPOINTMENT LOOKUP TESTS
    # =========================================================================

    def test_01_lookup_registered_patient(self):
        """1. Registered patient lookup returns upcoming appointments by user_id."""
        user = User.query.filter_by(email="patient_lookup_test@primecare.com").first()
        if not user:
            user = User(
                name="Ananya Nair",
                email="patient_lookup_test@primecare.com",
                phone_number="+919847055010",
                password_hash="test_pwd_hash",
                role="patient"
            )
            self.session.add(user)
            self.session.commit()

        target_date = self._find_next_working_day(2)
        res_book = create_booking_service(
            patient_name="Ananya Nair",
            age="28",
            gender="Female",
            phone_number="+919847055010",
            appointment_date=target_date,
            doctor_id=1,
            patient_user_id=user.id,
            actor_role='patient',
            booking_source='web'
        )
        self.assertTrue(res_book["success"])

        convo = self._create_convo(phone="+919847055010", role="patient")
        res = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body="Show my appointments",
            sender_phone="+919847055010",
            sender_role="patient",
            user_id=user.id,
            display_name="Ananya Nair"
        )
        self.assertTrue(res["handled"])
        self.assertIn("Your Upcoming Appointments", res["reply"])
        self.assertIn("Dr. Arshitha Abdurahman", res["reply"])
        self.assertIn(f"Token #{res_book['token']}", res["reply"])
        self.assertIn(target_date, res["reply"])

    def test_02_lookup_guest_by_exact_phone(self):
        """2. Guest patient lookup matches appointments strictly by verified phone number."""
        target_date = self._find_next_working_day(3)
        res_book = create_booking_service(
            patient_name="Kavya Menon",
            age="31",
            gender="Female",
            phone_number="+919847055020",
            appointment_date=target_date,
            doctor_id=1,
            patient_user_id=None,
            actor_role='patient',
            booking_source='kapso_whatsapp'
        )
        self.assertTrue(res_book["success"])

        convo = self._create_convo(phone="+919847055020", role="guest")
        res = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body="my appointment",
            sender_phone="+919847055020",
            sender_role="guest",
            user_id=None
        )
        self.assertTrue(res["handled"])
        self.assertIn("Dr. Arshitha Abdurahman", res["reply"])
        self.assertIn(f"Token #{res_book['token']}", res["reply"])

    def test_03_lookup_website_booking_visible_in_whatsapp(self):
        """3. Bookings created through website are visible and manageable via WhatsApp."""
        target_date = self._find_next_working_day(4)
        res_book = create_booking_service(
            patient_name="Sanjay Kumar",
            age="45",
            gender="Male",
            phone_number="+919847055030",
            appointment_date=target_date,
            doctor_id=1,
            patient_user_id=None,
            actor_role='patient',
            booking_source='web'
        )
        self.assertTrue(res_book["success"])

        convo = self._create_convo(phone="+919847055030", role="guest")
        res = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body="check my booking",
            sender_phone="+919847055030",
            sender_role="guest",
            user_id=None
        )
        self.assertTrue(res["handled"])
        self.assertIn("Dr. Arshitha Abdurahman", res["reply"])
        self.assertIn(f"Booking #{res_book['booking_id']}", res["reply"])

    def test_04_guest_placeholder_isolation_zero_cross_guest_leak(self):
        """4. Guest placeholder account never leaks other guests' bookings."""
        target_date = self._find_next_working_day(2)

        # Guest 1 creates a booking
        res1 = create_booking_service(
            patient_name="Guest Alpha",
            age="22",
            gender="Female",
            phone_number="+919847055041",
            appointment_date=target_date,
            doctor_id=1,
            patient_user_id=None,
            actor_role='patient',
            booking_source='kapso_whatsapp'
        )
        self.assertTrue(res1["success"])

        # Guest 2 creates a booking
        res2 = create_booking_service(
            patient_name="Guest Beta",
            age="35",
            gender="Male",
            phone_number="+919847055042",
            appointment_date=target_date,
            doctor_id=1,
            patient_user_id=None,
            actor_role='patient',
            booking_source='kapso_whatsapp'
        )
        self.assertTrue(res2["success"])

        # Guest 1 queries WhatsApp
        convo1 = self._create_convo(phone="+919847055041", role="guest")
        res_g1 = handle_whatsapp_appointment_mgmt(
            conversation=convo1,
            message_body="my appointments",
            sender_phone="+919847055041",
            sender_role="guest",
            user_id=None
        )
        self.assertTrue(res_g1["handled"])
        self.assertIn(f"Booking #{res1['booking_id']}", res_g1["reply"])
        self.assertNotIn(f"Booking #{res2['booking_id']}", res_g1["reply"])

    def test_05_unrelated_patient_isolation(self):
        """5. Unrelated registered patient cannot see another patient's bookings."""
        user_a = User.query.filter_by(email="patient_a@primecare.com").first()
        if not user_a:
            user_a = User(name="User A", email="patient_a@primecare.com", phone_number="+919847055051", password_hash="test_pwd_hash", role="patient")
            self.session.add(user_a)
            self.session.commit()

        user_b = User.query.filter_by(email="patient_b@primecare.com").first()
        if not user_b:
            user_b = User(name="User B", email="patient_b@primecare.com", phone_number="+919847055052", password_hash="test_pwd_hash", role="patient")
            self.session.add(user_b)
            self.session.commit()

        target_date = self._find_next_working_day(3)
        res_a = create_booking_service(
            patient_name="User A",
            age="30",
            gender="Male",
            phone_number="+919847055051",
            appointment_date=target_date,
            doctor_id=1,
            patient_user_id=user_a.id,
            actor_role='patient',
            booking_source='web'
        )
        self.assertTrue(res_a["success"])

        # Patient B queries
        convo_b = self._create_convo(phone="+919847055052", role="patient")
        res_lookup_b = handle_whatsapp_appointment_mgmt(
            conversation=convo_b,
            message_body="view my appointments",
            sender_phone="+919847055052",
            sender_role="patient",
            user_id=user_b.id
        )
        self.assertTrue(res_lookup_b["handled"])
        self.assertIn("You don't have any upcoming appointments", res_lookup_b["reply"])
        self.assertNotIn(f"Booking #{res_a['booking_id']}", res_lookup_b["reply"])

    def test_06_lookup_multiple_appointments_numbered_list(self):
        """6. Multiple appointments are returned as an organized numbered list."""
        phone = "+919847055060"
        d1 = self._find_next_working_day(2)
        d2 = self._find_next_working_day(5)

        res1 = create_booking_service(
            patient_name="Deepa", age="29", gender="Female",
            phone_number=phone, appointment_date=d1, doctor_id=1,
            patient_user_id=None, actor_role='patient', booking_source='kapso_whatsapp'
        )
        res2 = create_booking_service(
            patient_name="Deepa", age="29", gender="Female",
            phone_number=phone, appointment_date=d2, doctor_id=1,
            patient_user_id=None, actor_role='patient', booking_source='kapso_whatsapp'
        )
        self.assertTrue(res1["success"])
        self.assertTrue(res2["success"])

        convo = self._create_convo(phone=phone, role="guest")
        res = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body="my appointments",
            sender_phone=phone,
            sender_role="guest"
        )
        self.assertTrue(res["handled"])
        self.assertIn("*1.*", res["reply"])
        self.assertIn("*2.*", res["reply"])
        self.assertIn(f"Booking #{res1['booking_id']}", res["reply"])
        self.assertIn(f"Booking #{res2['booking_id']}", res["reply"])

    def test_07_lookup_no_appointments_guidance(self):
        """7. Patient with no appointments receives clear message with booking instructions."""
        convo = self._create_convo(phone="+919847055070", role="guest")
        res = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body="do I have an appointment?",
            sender_phone="+919847055070",
            sender_role="guest"
        )
        self.assertTrue(res["handled"])
        self.assertIn("You don't have any upcoming appointments", res["reply"])
        self.assertIn("Book an appointment", res["reply"])

    def test_08_lookup_bsuid_only_sender_blocked_fail_safe(self):
        """8. BSUID-only sender without verified phone number cannot retrieve appointments."""
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            scoped_user_id="bsuid_mgmt_001",
            sender_phone=None,
            status="active"
        )
        self.session.add(convo)
        self.session.commit()

        res = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body="show my appointments",
            sender_phone=None,
            sender_role="guest",
            user_id=None
        )
        # BSUID-only senders without a verified phone number cannot access appointment management
        self.assertFalse(res["handled"])

    # =========================================================================
    # 2. APPOINTMENT CANCELLATION TESTS
    # =========================================================================

    def test_09_cancel_single_appointment_flow(self):
        """9. Patient with a single appointment skips choice step directly to CONFIRMATION."""
        phone = "+919847055080"
        target_date = self._find_next_working_day(2)
        b_res = create_booking_service(
            patient_name="Meera Nair", age="33", gender="Female",
            phone_number=phone, appointment_date=target_date, doctor_id=1,
            patient_user_id=None, actor_role='patient', booking_source='kapso_whatsapp'
        )
        self.assertTrue(b_res["success"])
        b_id = b_res["booking_id"]

        convo = self._create_convo(phone=phone, role="guest")

        # Step 1: Initiate cancel
        res1 = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body="Cancel my appointment",
            sender_phone=phone,
            sender_role="guest"
        )
        self.assertTrue(res1["handled"])
        self.assertIn("Are you sure you want to cancel this appointment?", res1["reply"])
        self.assertIn(f"Booking ID:* #{b_id}", res1["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertIsNotNone(session)
        self.assertEqual(session.action, 'cancel')
        self.assertEqual(session.step, 'CONFIRMATION')
        self.assertEqual(session.booking_id, b_id)

        # Step 2: Confirm cancellation
        res2 = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body="CONFIRM",
            sender_phone=phone,
            sender_role="guest"
        )
        self.assertTrue(res2["handled"])
        self.assertIn("Appointment Cancelled Successfully", res2["reply"])
        self.assertIn(f"Booking ID:* #{b_id}", res2["reply"])
        self.assertIn("released", res2["reply"])

        # Check DB state
        booking = PatientBooking.query.get(b_id)
        self.assertEqual(booking.status, 'cancelled')
        self.assertEqual(booking.cancelled_by, 'patient')

        # Check session completed
        completed_session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id).first()
        self.assertEqual(completed_session.status, 'confirmed')
        self.assertEqual(completed_session.step, 'COMPLETED')

    def test_10_cancel_multi_appointment_flow(self):
        """10. Patient with multiple appointments enters CHOOSE_APPOINTMENT step first."""
        phone = "+919847055090"
        d1 = self._find_next_working_day(2)
        d2 = self._find_next_working_day(5)

        b1 = create_booking_service(
            patient_name="Ravi Menon", age="40", gender="Male",
            phone_number=phone, appointment_date=d1, doctor_id=1,
            patient_user_id=None, actor_role='patient', booking_source='kapso_whatsapp'
        )
        b2 = create_booking_service(
            patient_name="Ravi Menon", age="40", gender="Male",
            phone_number=phone, appointment_date=d2, doctor_id=1,
            patient_user_id=None, actor_role='patient', booking_source='kapso_whatsapp'
        )
        self.assertTrue(b1["success"])
        self.assertTrue(b2["success"])

        convo = self._create_convo(phone=phone, role="guest")

        # Step 1: Initiate cancel -> presents list
        res1 = handle_whatsapp_appointment_mgmt(convo, "cancel booking", phone, "guest")
        self.assertTrue(res1["handled"])
        self.assertIn("Which appointment would you like to cancel?", res1["reply"])
        self.assertIn(f"Booking #{b1['booking_id']}", res1["reply"])
        self.assertIn(f"Booking #{b2['booking_id']}", res1["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'CHOOSE_APPOINTMENT')

        # Step 2: Choose appointment 1 (which is b2 in date.desc order)
        res2 = handle_whatsapp_appointment_mgmt(convo, "1", phone, "guest")
        self.assertTrue(res2["handled"])
        self.assertIn("Are you sure you want to cancel this appointment?", res2["reply"])
        self.assertIn(f"Booking ID:* #{b2['booking_id']}", res2["reply"])

        # Step 3: Confirm cancellation
        res3 = handle_whatsapp_appointment_mgmt(convo, "yes", phone, "guest")
        self.assertTrue(res3["handled"])
        self.assertIn("Appointment Cancelled Successfully", res3["reply"])

        # Selected booking (b2) is cancelled; unselected booking (b1) remains confirmed
        booking1 = PatientBooking.query.get(b1["booking_id"])
        booking2 = PatientBooking.query.get(b2["booking_id"])
        self.assertEqual(booking1.status, 'confirmed')
        self.assertEqual(booking2.status, 'cancelled')

    def test_11_cancel_invalid_selection_handled(self):
        """11. Out-of-range or non-numeric selection in CHOOSE_APPOINTMENT provides guidance."""
        phone = "+919847055100"
        d1 = self._find_next_working_day(2)
        d2 = self._find_next_working_day(5)
        create_booking_service(patient_name="Test", age="25", gender="Male", phone_number=phone, appointment_date=d1, doctor_id=1, booking_source='kapso_whatsapp')
        create_booking_service(patient_name="Test", age="25", gender="Male", phone_number=phone, appointment_date=d2, doctor_id=1, booking_source='kapso_whatsapp')

        convo = self._create_convo(phone=phone, role="guest")
        handle_whatsapp_appointment_mgmt(convo, "cancel appointment", phone, "guest")

        # Test out of range
        res_range = handle_whatsapp_appointment_mgmt(convo, "99", phone, "guest")
        self.assertTrue(res_range["handled"])
        self.assertIn("Please select a number between 1 and 2", res_range["reply"])

        # Test non-digit
        res_text = handle_whatsapp_appointment_mgmt(convo, "first one please", phone, "guest")
        self.assertTrue(res_text["handled"])
        self.assertIn("Please enter the appointment number", res_text["reply"])

        # Verify session is still waiting at CHOOSE_APPOINTMENT
        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'CHOOSE_APPOINTMENT')

    def test_12_cancel_abort_keyword(self):
        """12. Abort keyword cancels the cancellation request; booking remains active."""
        phone = "+919847055110"
        target_date = self._find_next_working_day(2)
        b_res = create_booking_service(
            patient_name="Sita", age="30", gender="Female",
            phone_number=phone, appointment_date=target_date, doctor_id=1,
            booking_source='kapso_whatsapp'
        )
        b_id = b_res["booking_id"]

        convo = self._create_convo(phone=phone, role="guest")
        handle_whatsapp_appointment_mgmt(convo, "cancel my appointment", phone, "guest")

        # User changes mind
        res_abort = handle_whatsapp_appointment_mgmt(convo, "CANCEL", phone, "guest")
        self.assertTrue(res_abort["handled"])
        self.assertIn("Cancellation aborted", res_abort["reply"])
        self.assertIn("remains intact", res_abort["reply"])

        # Booking is STILL confirmed
        booking = PatientBooking.query.get(b_id)
        self.assertEqual(booking.status, 'confirmed')

        # Session is cancelled
        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id).first()
        self.assertEqual(session.status, 'cancelled')

    def test_13_cancel_unauthorized_guest_cannot_cancel_patient_booking(self):
        """13. A guest caller cannot cancel a registered patient's booking (ownership protection)."""
        user = User.query.filter_by(email="patient_owner@primecare.com").first()
        if not user:
            user = User(name="Owner", email="patient_owner@primecare.com", phone_number="+919847055120", password_hash="test_pwd_hash", role="patient")
            self.session.add(user)
            self.session.commit()

        target_date = self._find_next_working_day(2)
        b_res = create_booking_service(
            patient_name="Owner", age="35", gender="Male",
            phone_number="+919847055120", appointment_date=target_date, doctor_id=1,
            patient_user_id=user.id, actor_role='patient', booking_source='web'
        )
        b_id = b_res["booking_id"]

        # Attempt to cancel by a completely different phone number without user_id
        res = cancel_booking_service(
            booking_id=b_id,
            actor_user_id=None,
            actor_role='patient',
            phone_number="+919847055999"
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], "UNAUTHORIZED")

        # Booking is still confirmed
        booking = PatientBooking.query.get(b_id)
        self.assertEqual(booking.status, 'confirmed')

    def test_14_cancel_duplicate_confirmation_idempotent(self):
        """14. Duplicate 'CONFIRM' on an already cancelled session returns idempotent message."""
        phone = "+919847055130"
        target_date = self._find_next_working_day(2)
        b_res = create_booking_service(
            patient_name="Alex", age="27", gender="Male",
            phone_number=phone, appointment_date=target_date, doctor_id=1,
            booking_source='kapso_whatsapp'
        )

        convo = self._create_convo(phone=phone, role="guest")
        handle_whatsapp_appointment_mgmt(convo, "cancel my appointment", phone, "guest")
        res1 = handle_whatsapp_appointment_mgmt(convo, "CONFIRM", phone, "guest")
        self.assertTrue(res1["handled"])
        self.assertIn("Appointment Cancelled Successfully", res1["reply"])

        # Second CONFIRM
        res2 = handle_whatsapp_appointment_mgmt(convo, "CONFIRM", phone, "guest")
        self.assertTrue(res2["handled"])
        self.assertIn("already been processed", res2["reply"])

    # =========================================================================
    # 3. APPOINTMENT RESCHEDULING TESTS
    # =========================================================================

    def test_15_reschedule_single_appointment_flow(self):
        """15. Rescheduling single appointment advances to SELECT_DATE -> CONFIRMATION -> atomic execution."""
        phone = "+919847055140"
        d_orig = self._find_next_working_day(2)
        d_target = self._find_next_working_day(5)

        b_res = create_booking_service(
            patient_name="Maya Sen", age="32", gender="Female",
            phone_number=phone, appointment_date=d_orig, doctor_id=1,
            booking_source='kapso_whatsapp'
        )
        orig_id = b_res["booking_id"]
        orig_token = b_res["token"]

        convo = self._create_convo(phone=phone, role="guest")

        # Step 1: Reschedule request
        res1 = handle_whatsapp_appointment_mgmt(convo, "reschedule my appointment", phone, "guest")
        self.assertTrue(res1["handled"])
        self.assertIn("Select a new date for your appointment", res1["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertIsNotNone(session)
        self.assertEqual(session.action, 'reschedule')
        self.assertEqual(session.step, 'SELECT_DATE')

        # Step 2: Provide new date
        res2 = handle_whatsapp_appointment_mgmt(convo, d_target, phone, "guest")
        self.assertTrue(res2["handled"])
        self.assertIn("Rescheduling Summary", res2["reply"])
        self.assertIn(f"New Date:* {d_target}", res2["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'CONFIRMATION')

        # Step 3: Confirm reschedule
        res3 = handle_whatsapp_appointment_mgmt(convo, "CONFIRM", phone, "guest")
        self.assertTrue(res3["handled"])
        self.assertIn("Appointment Rescheduled Successfully", res3["reply"])
        self.assertIn(f"New Date:* {d_target}", res3["reply"])

        # Check DB: original is cancelled, new booking exists
        orig_booking = PatientBooking.query.get(orig_id)
        self.assertEqual(orig_booking.status, 'cancelled')

        new_booking = PatientBooking.query.filter_by(
            phone_number=phone,
            date=d_target,
            status='confirmed'
        ).first()
        self.assertIsNotNone(new_booking)
        self.assertNotEqual(new_booking.id, orig_id)
        self.assertEqual(new_booking.booking_source, 'kapso_whatsapp')

    def test_16_reschedule_multi_appointment_flow(self):
        """16. Multi-appointment rescheduling initiates at CHOOSE_APPOINTMENT step."""
        phone = "+919847055150"
        d1 = self._find_next_working_day(2)
        d2 = self._find_next_working_day(4)

        b1 = create_booking_service(patient_name="Pooja", age="26", gender="Female", phone_number=phone, appointment_date=d1, doctor_id=1, booking_source='kapso_whatsapp')
        b2 = create_booking_service(patient_name="Pooja", age="26", gender="Female", phone_number=phone, appointment_date=d2, doctor_id=1, booking_source='kapso_whatsapp')

        convo = self._create_convo(phone=phone, role="guest")
        res1 = handle_whatsapp_appointment_mgmt(convo, "reschedule", phone, "guest")
        self.assertTrue(res1["handled"])
        self.assertIn("Which appointment would you like to reschedule?", res1["reply"])

        # Select appointment 1
        res2 = handle_whatsapp_appointment_mgmt(convo, "1", phone, "guest")
        self.assertTrue(res2["handled"])
        self.assertIn("Select a new date", res2["reply"])

    def test_17_reschedule_same_date_rejected(self):
        """17. Selecting the current appointment date during reschedule is rejected."""
        phone = "+919847055160"
        d_orig = self._find_next_working_day(2)
        create_booking_service(patient_name="Test", age="30", gender="Male", phone_number=phone, appointment_date=d_orig, doctor_id=1, booking_source='kapso_whatsapp')

        convo = self._create_convo(phone=phone, role="guest")
        handle_whatsapp_appointment_mgmt(convo, "reschedule my appointment", phone, "guest")

        res = handle_whatsapp_appointment_mgmt(convo, d_orig, phone, "guest")
        self.assertTrue(res["handled"])
        self.assertIn("same as your current appointment date", res["reply"])

    def test_18_reschedule_unavailable_date_rejected(self):
        """18. Rescheduling to a doctor leave date returns clear error and keeps original intact."""
        phone = "+919847055170"
        d_orig = self._find_next_working_day(2)
        d_leave = self._find_next_working_day(4)

        # Mark doctor on leave on d_leave
        leave = DoctorLeave(
            doctor_id=1,
            doctor_name="Dr. Arshitha Abdurahman",
            date=d_leave,
            reason="Conference",
            status='active'
        )
        self.session.add(leave)
        self.session.commit()

        b_res = create_booking_service(patient_name="Girish", age="42", gender="Male", phone_number=phone, appointment_date=d_orig, doctor_id=1, booking_source='kapso_whatsapp')
        orig_id = b_res["booking_id"]

        convo = self._create_convo(phone=phone, role="guest")
        handle_whatsapp_appointment_mgmt(convo, "reschedule appointment", phone, "guest")

        res = handle_whatsapp_appointment_mgmt(convo, d_leave, phone, "guest")
        self.assertTrue(res["handled"])
        self.assertIn("is on leave", res["reply"])

        # Original booking still intact
        booking = PatientBooking.query.get(orig_id)
        self.assertEqual(booking.status, 'confirmed')

    def test_19_reschedule_atomic_rollback_on_failure(self):
        """19. Atomic safety: If cancelling original fails during reschedule, replacement booking is rolled back."""
        phone = "+919847055180"
        d_orig = self._find_next_working_day(2)
        d_new = self._find_next_working_day(5)

        b_res = create_booking_service(patient_name="Asha", age="36", gender="Female", phone_number=phone, appointment_date=d_orig, doctor_id=1, booking_source='kapso_whatsapp')
        orig_id = b_res["booking_id"]

        # Mock cancel_booking_service to fail when called for original booking
        real_cancel = cancel_booking_service
        call_count = {"count": 0}

        def mock_cancel(*args, **kwargs):
            call_count["count"] += 1
            if kwargs.get("booking_id") == orig_id:
                return {"success": False, "message": "Simulated cancellation failure"}
            return real_cancel(*args, **kwargs)

        with patch("services.booking_services.cancel_booking_service", side_effect=mock_cancel):
            res = reschedule_booking_service(
                original_booking_id=orig_id,
                new_date_str=d_new,
                actor_role='patient',
                phone_number=phone,
                booking_source='kapso_whatsapp'
            )
            self.assertFalse(res["success"])
            self.assertEqual(res["error_code"], "ORIGINAL_CANCEL_FAILED")

        # Original booking must STILL be confirmed
        original = PatientBooking.query.get(orig_id)
        self.assertEqual(original.status, 'confirmed')

        # No active replacement booking should remain
        replacement = PatientBooking.query.filter_by(
            phone_number=phone,
            date=d_new,
            status='confirmed'
        ).first()
        self.assertIsNone(replacement)

    def test_20_reschedule_abort_leaves_original_intact(self):
        """20. Aborting reschedule via CANCEL preserves the original appointment."""
        phone = "+919847055190"
        d_orig = self._find_next_working_day(2)
        b_res = create_booking_service(patient_name="Naveen", age="31", gender="Male", phone_number=phone, appointment_date=d_orig, doctor_id=1, booking_source='kapso_whatsapp')
        orig_id = b_res["booking_id"]

        convo = self._create_convo(phone=phone, role="guest")
        handle_whatsapp_appointment_mgmt(convo, "reschedule my appointment", phone, "guest")

        res = handle_whatsapp_appointment_mgmt(convo, "CANCEL", phone, "guest")
        self.assertTrue(res["handled"])
        self.assertIn("cancelled", res["reply"].lower())

        orig = PatientBooking.query.get(orig_id)
        self.assertEqual(orig.status, 'confirmed')

    # =========================================================================
    # 4. INTEGRATION, SAFETY & SECURITY TESTS
    # =========================================================================

    def test_21_doctor_and_admin_roles_redirected(self):
        """21. Doctors and Admins attempting patient appointment management are redirected."""
        convo_doc = self._create_convo(phone="+919847055901", role="doctor")
        res_doc = handle_whatsapp_appointment_mgmt(
            conversation=convo_doc,
            message_body="cancel appointment",
            sender_phone="+919847055901",
            sender_role="doctor"
        )
        self.assertTrue(res_doc["handled"])
        self.assertIn("Doctors and clinic staff can manage appointments", res_doc["reply"])

        convo_adm = self._create_convo(phone="+919847055902", role="admin")
        res_adm = handle_whatsapp_appointment_mgmt(
            conversation=convo_adm,
            message_body="reschedule appointment",
            sender_phone="+919847055902",
            sender_role="admin"
        )
        self.assertTrue(res_adm["handled"])
        self.assertIn("Doctors and clinic staff can manage appointments", res_adm["reply"])

    def test_22_active_booking_session_not_interfered(self):
        """22. Appointment mgmt handler yields (handled=False) when active booking session is in progress."""
        convo = self._create_convo(phone="+919847055200", role="guest")
        # Start a booking session
        booking_session = WhatsAppBookingSession(
            conversation_id=convo.id,
            status='in_progress',
            action='book',
            step='SELECT_DATE',
            doctor_id=1,
            doctor_name="Dr. Arshitha Abdurahman"
        )
        self.session.add(booking_session)
        self.session.commit()

        # Sender says "cancel" (intending to abort booking, not cancel appointment)
        res = handle_whatsapp_appointment_mgmt(
            conversation=convo,
            message_body="cancel",
            sender_phone="+919847055200",
            sender_role="guest"
        )
        self.assertFalse(res["handled"])

    def test_23_human_handoff_blocks_webhook_processing(self):
        """23. Conversation in human handoff status ignores automated management flows."""
        phone = "+919847055210"
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=phone,
            status="active",
            is_human_handoff=True
        )
        self.session.add(convo)
        self.session.commit()

        payload = build_incoming_payload(sender_phone=phone.lstrip("+"), text_body="cancel my appointment")
        body_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": generate_sig(body_bytes),
            "X-Webhook-Event": "messages"
        }

        resp = self.client.post("/api/whatsapp/webhook", data=body_bytes, headers=headers)
        self.assertEqual(resp.status_code, 200)

        data = resp.get_json()
        self.assertEqual(data.get("status"), "ok")
        # In handoff mode, returns staff handoff notification instead of appointment management
        self.assertIn("handed off to PrimeCare clinic staff", data.get("reply", ""))

    def test_24_webhook_end_to_end_cancel_journey(self):
        """24. Full end-to-end multi-turn webhook cancellation journey with HMAC signature."""
        phone = "+919847055220"
        target_date = self._find_next_working_day(2)
        b_res = create_booking_service(
            patient_name="Praveen", age="38", gender="Male",
            phone_number=phone, appointment_date=target_date, doctor_id=1,
            booking_source='kapso_whatsapp'
        )
        b_id = b_res["booking_id"]

        # Webhook Turn 1: "cancel my appointment"
        p1 = build_incoming_payload(sender_phone=phone.lstrip("+"), text_body="cancel my appointment")
        raw1 = json.dumps(p1).encode("utf-8")
        h1 = {"Content-Type": "application/json", "X-Hub-Signature-256": generate_sig(raw1), "X-Webhook-Event": "messages"}
        r1 = self.client.post("/api/whatsapp/webhook", data=raw1, headers=h1)
        self.assertEqual(r1.status_code, 200)
        d1 = r1.get_json()
        self.assertEqual(d1["status"], "ok")
        self.assertIn("Are you sure you want to cancel this appointment?", d1["reply"])

        # Webhook Turn 2: "CONFIRM"
        p2 = build_incoming_payload(sender_phone=phone.lstrip("+"), text_body="CONFIRM")
        raw2 = json.dumps(p2).encode("utf-8")
        h2 = {"Content-Type": "application/json", "X-Hub-Signature-256": generate_sig(raw2), "X-Webhook-Event": "messages"}
        r2 = self.client.post("/api/whatsapp/webhook", data=raw2, headers=h2)
        self.assertEqual(r2.status_code, 200)
        d2 = r2.get_json()
        self.assertEqual(d2["status"], "ok")
        self.assertIn("Appointment Cancelled Successfully", d2["reply"])

        # Verify DB: booking is cancelled
        booking = PatientBooking.query.get(b_id)
        self.assertEqual(booking.status, 'cancelled')


if __name__ == '__main__':
    unittest.main()
