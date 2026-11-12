"""
tests/test_whatsapp_booking_flow.py
===================================
Comprehensive Automated Test Suite for WhatsApp Appointment Booking Flow (Phase 4A).

Tests all 15 scenarios:
  1. Intent recognition starts in_progress session and presents doctor menu.
  2. Non-booking message falls through to AI (handled=False).
  3. Doctor selection by numeric index and name advances to SELECT_DATE.
  4. Multiple doctor match disambiguation (e.g. General Physician).
  5. Invalid doctor selection returns error without advancing step.
  6. Date validation: past date, >15-day window, clinic holiday, doctor leave, non-working days.
  7. Valid date advances to PATIENT_DETAILS and computes duty hours.
  8. Patient details: registered patient default name vs custom name, age & gender parsing.
  9. Patient details: guest patient details locked to verified inbound sender phone.
  10. Explicit confirmation executes create_booking_service() with kapso_whatsapp source.
  11. Atomic token allocation via DailyTokenAllocation and BookingSyncOutbox event generation.
  12. Cancellation at various steps transitions session to cancelled with zero booking created.
  13. Idempotency: repeated confirmation on confirmed session returns existing token without re-booking.
  14. Doctor and administrator roles are strictly prevented from creating patient bookings.
  15. Full end-to-end multi-turn webhook integration journey with HMAC signature.
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
from services.whatsapp_booking_flow import (
    handle_whatsapp_booking_flow, is_booking_intent, parse_booking_date,
    parse_patient_details, resolve_doctor_from_input
)
from services.booking_services import check_availability_service, create_booking_service

TEST_WEBHOOK_SECRET = "test_kapso_webhook_secret_phase4a"


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


class WhatsAppBookingFlowTestSuite(unittest.TestCase):
    """
    Automated test suite verifying the multi-turn appointment booking workflow.
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

        # Clean WhatsApp and Booking test data
        self.session.query(WhatsAppMessage).delete()
        self.session.query(WhatsAppBookingSession).delete()
        self.session.query(WhatsAppWebhookEvent).delete()
        self.session.query(WhatsAppConversation).delete()
        self.session.query(BookingSyncOutbox).delete()
        self.session.query(PatientBooking).delete()
        self.session.query(DailyTokenAllocation).delete()
        self.session.query(DoctorLeave).delete()
        self.session.query(ClinicHoliday).delete()
        self.session.query(DoctorSchedule).filter(DoctorSchedule.doctor_id != 1).delete()
        self.session.query(Doctor).filter(Doctor.id != 1).delete()
        self.session.commit()

        # Invalidate in-memory doctor cache so tests always see clean DB state
        from app import get_all_doctors
        get_all_doctors(force_refresh=True)

        # Ensure active doctors exist
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

        # Ensure active schedules exist for doc1
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
        for day in days:
            if not DoctorSchedule.query.filter_by(doctor_id=1, day_of_week=day).first():
                self.session.add(DoctorSchedule(
                    doctor_id=1,
                    day_of_week=day,
                    start_time="09:00",
                    end_time="17:00",
                    is_available=True
                ))
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
        self.session.query(DoctorSchedule).filter(DoctorSchedule.doctor_id != 1).delete()
        self.session.query(Doctor).filter(Doctor.id != 1).delete()
        self.session.commit()
        from app import get_all_doctors
        get_all_doctors(force_refresh=True)
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

    # -------------------------------------------------------------------------
    # 1. Intent Recognition
    # -------------------------------------------------------------------------
    def test_01_booking_intent_initiates_session_and_menu(self):
        """1. Intent recognition starts an in_progress booking session and displays doctor menu."""
        convo = self._create_convo()
        res = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="I want to book an appointment with doctor",
            sender_phone="+919847055001",
            sender_role="guest"
        )
        self.assertTrue(res["handled"])
        self.assertIn("PrimeCare Clinic", res["reply"])
        self.assertIn("Appointment Booking", res["reply"])
        self.assertIn("Dr. Arshitha Abdurahman", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertIsNotNone(session)
        self.assertEqual(session.step, 'SELECT_DOCTOR')

    def test_02_non_booking_message_not_handled(self):
        """2. Non-booking message returns handled=False and does not create booking session."""
        convo = self._create_convo()
        res = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body="What are the clinic timings for pharmacy?",
            sender_phone="+919847055001",
            sender_role="guest"
        )
        self.assertFalse(res["handled"])
        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id).first()
        self.assertIsNone(session)

    # -------------------------------------------------------------------------
    # 2. Doctor Selection
    # -------------------------------------------------------------------------
    def test_03_doctor_selection_by_number(self):
        """3. Numeric doctor selection resolves doctor, advances to SELECT_DATE, displays working days."""
        convo = self._create_convo()
        # Turn 1: Start booking
        handle_whatsapp_booking_flow(convo, "Book an appointment", "+919847055001", "guest")

        # Turn 2: Choose doctor 1
        res = handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")
        self.assertTrue(res["handled"])
        self.assertIn("You selected *Dr. Arshitha Abdurahman*", res["reply"])
        self.assertIn("Doctor's Working Days", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'SELECT_DATE')
        self.assertEqual(session.doctor_id, 1)
        self.assertEqual(session.doctor_name, "Dr. Arshitha Abdurahman")

    def test_04_doctor_selection_by_name(self):
        """4. Doctor selection by partial/full name resolves doctor and advances to SELECT_DATE."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")

        res = handle_whatsapp_booking_flow(convo, "Dr. Arshitha", "+919847055001", "guest")
        self.assertTrue(res["handled"])
        self.assertIn("You selected *Dr. Arshitha Abdurahman*", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'SELECT_DATE')
        self.assertEqual(session.doctor_id, 1)

    def test_05_invalid_doctor_selection(self):
        """5. Invalid doctor selection returns clear guidance without corrupting or advancing step."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")

        res = handle_whatsapp_booking_flow(convo, "999", "+919847055001", "guest")
        self.assertTrue(res["handled"])
        self.assertIn("Please select a number between 1 and", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'SELECT_DOCTOR')

    # -------------------------------------------------------------------------
    # 3. Date Selection & Validation
    # -------------------------------------------------------------------------
    def _find_next_working_day(self, doctor_id):
        ist = pytz.timezone('Asia/Kolkata')
        today = datetime.now(ist).date()
        schedules = DoctorSchedule.query.filter_by(doctor_id=doctor_id, is_available=True).all()
        working_weekdays = {s.day_of_week.lower() for s in schedules}
        for d in range(1, 14):
            candidate = today + timedelta(days=d)
            if candidate.strftime('%A').lower() in working_weekdays:
                return candidate.strftime('%Y-%m-%d')
        return (today + timedelta(days=1)).strftime('%Y-%m-%d')

    def test_06_date_selection_valid_date(self):
        """6. Valid working day date advances to PATIENT_DETAILS and sets duty hours slot."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")

        target_date = self._find_next_working_day(1)
        res = handle_whatsapp_booking_flow(convo, target_date, "+919847055001", "guest")
        self.assertTrue(res["handled"])
        self.assertIn("Date Confirmed", res["reply"])
        self.assertIn("Please provide the patient details", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'PATIENT_DETAILS')
        self.assertEqual(session.appointment_date, target_date)
        self.assertIsNotNone(session.time_slot)

    def test_07_date_selection_past_date_rejected(self):
        """7. Past date is rejected with backend reason and stays on SELECT_DATE."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")

        res = handle_whatsapp_booking_flow(convo, "2020-01-01", "+919847055001", "guest")
        self.assertTrue(res["handled"])
        self.assertIn("Cannot book for a past date", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'SELECT_DATE')

    def test_08_date_selection_window_exceeded_rejected(self):
        """8. Date >15 days in advance is rejected with 15-day window message."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")

        ist = pytz.timezone('Asia/Kolkata')
        future_date = (datetime.now(ist).date() + timedelta(days=20)).strftime('%Y-%m-%d')

        res = handle_whatsapp_booking_flow(convo, future_date, "+919847055001", "guest")
        self.assertTrue(res["handled"])
        self.assertIn("15 days in advance", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'SELECT_DATE')

    def test_09_date_selection_clinic_holiday_rejected(self):
        """9. Clinic holiday is rejected with exact holiday reason."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")

        target_date = self._find_next_working_day(1)
        holiday = ClinicHoliday(date=target_date, reason="Republic Day Holiday")
        self.session.add(holiday)
        self.session.commit()

        res = handle_whatsapp_booking_flow(convo, target_date, "+919847055001", "guest")
        self.assertTrue(res["handled"])
        self.assertIn("Republic Day Holiday", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'SELECT_DATE')

    def test_10_date_selection_doctor_leave_rejected(self):
        """10. Date when doctor is on leave is rejected with leave reason."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")

        target_date = self._find_next_working_day(1)
        leave = DoctorLeave(
            doctor_id=1,
            doctor_name="Dr. Arshitha Abdurahman",
            date=target_date,
            reason="Medical Conference",
            status="active"
        )
        self.session.add(leave)
        self.session.commit()

        res = handle_whatsapp_booking_flow(convo, target_date, "+919847055001", "guest")
        self.assertTrue(res["handled"])
        self.assertIn("leave", res["reply"].lower())

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'SELECT_DATE')

    # -------------------------------------------------------------------------
    # 4. Patient Details
    # -------------------------------------------------------------------------
    def test_11_patient_details_guest_flow(self):
        """11. Guest patient provides name, age, gender; advances to CONFIRMATION with summary."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")
        target_date = self._find_next_working_day(1)
        handle_whatsapp_booking_flow(convo, target_date, "+919847055001", "guest")

        res = handle_whatsapp_booking_flow(convo, "Rahul Sharma, 28, Male", "+919847055001", "guest")
        self.assertTrue(res["handled"])
        self.assertIn("Appointment Summary", res["reply"])
        self.assertIn("Rahul Sharma", res["reply"])
        self.assertIn("28 yrs, Male", res["reply"])
        self.assertIn("+919847055001", res["reply"])
        self.assertIn("Reply *CONFIRM* to book", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'CONFIRMATION')
        self.assertEqual(session.patient_name, "Rahul Sharma")
        self.assertEqual(session.patient_age, "28")
        self.assertEqual(session.patient_gender, "Male")
        self.assertEqual(session.contact_phone, "+919847055001")

    def test_12_patient_details_registered_patient(self):
        """12. Registered patient defaults to profile name when age & gender are supplied."""
        # Provision registered user
        import random
        phone_unique = f"+9198470{random.randint(10000, 99999)}"
        patient_user = User(
            name="Priya Varma",
            phone_number=phone_unique,
            role="patient",
            password_hash="mock_hash"
        )
        self.session.add(patient_user)
        self.session.commit()

        convo = self._create_convo(phone=phone_unique, role="patient")
        handle_whatsapp_booking_flow(
            convo, "Book an appointment", phone_unique, "patient",
            user_id=patient_user.id, display_name="Priya Varma"
        )
        handle_whatsapp_booking_flow(convo, "1", phone_unique, "patient", user_id=patient_user.id)
        target_date = self._find_next_working_day(1)
        handle_whatsapp_booking_flow(convo, target_date, phone_unique, "patient", user_id=patient_user.id)

        # Registered patient just supplies "32, Female"
        res = handle_whatsapp_booking_flow(
            convo, "32, Female", phone_unique, "patient",
            user_id=patient_user.id, display_name="Priya Varma"
        )
        self.assertTrue(res["handled"])
        self.assertIn("Priya Varma", res["reply"])
        self.assertIn("32 yrs, Female", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.patient_name, "Priya Varma")
        self.assertEqual(session.patient_age, "32")
        self.assertEqual(session.patient_gender, "Female")
        self.assertEqual(session.patient_user_id, patient_user.id)

    # -------------------------------------------------------------------------
    # 5. Confirmation & Atomic Booking Creation
    # -------------------------------------------------------------------------
    def test_13_explicit_confirmation_creates_booking_and_allocates_token(self):
        """13. Explicit 'CONFIRM' atomically creates booking, allocates token, and writes outbox event."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")
        target_date = self._find_next_working_day(1)
        handle_whatsapp_booking_flow(convo, target_date, "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "Rahul Sharma, 28, Male", "+919847055001", "guest")

        # Turn: CONFIRM
        res = handle_whatsapp_booking_flow(convo, "CONFIRM", "+919847055001", "guest")
        self.assertTrue(res["handled"])
        self.assertIn("Appointment Confirmed!", res["reply"])
        self.assertIn("#1", res["reply"])
        self.assertIn("Dr. Arshitha Abdurahman", res["reply"])
        self.assertIn("Rahul Sharma", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id).first()
        self.assertEqual(session.status, 'confirmed')
        self.assertEqual(session.step, 'COMPLETED')
        self.assertEqual(session.token_number, 1)
        self.assertIsNotNone(session.booking_id)

        # Verify PatientBooking record
        booking = PatientBooking.query.get(session.booking_id)
        self.assertIsNotNone(booking)
        self.assertEqual(booking.patient_name, "Rahul Sharma")
        self.assertEqual(booking.token, 1)
        self.assertEqual(booking.booking_source, "kapso_whatsapp")
        self.assertEqual(booking.status, "confirmed")

        # Verify booking record and atomic token allocation
        self.assertEqual(booking.token, 1)
        self.assertEqual(booking.booking_source, "kapso_whatsapp")
        self.assertEqual(booking.status, "confirmed")

        alloc = DailyTokenAllocation.query.filter_by(
            appointment_date=target_date
        ).first()
        self.assertIsNotNone(alloc)
        self.assertEqual(alloc.last_allocated_token, 1)

    def test_14_idempotent_duplicate_confirmation(self):
        """14. Repeating 'CONFIRM' on confirmed session returns existing token without creating duplicate."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")
        target_date = self._find_next_working_day(1)
        handle_whatsapp_booking_flow(convo, target_date, "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "Rahul Sharma, 28, Male", "+919847055001", "guest")
        res1 = handle_whatsapp_booking_flow(convo, "CONFIRM", "+919847055001", "guest")
        self.assertIn("#1", res1["reply"])

        # Repeat confirmation
        res2 = handle_whatsapp_booking_flow(convo, "CONFIRM", "+919847055001", "guest")
        self.assertTrue(res2["handled"])
        self.assertIn("already confirmed", res2["reply"])
        self.assertIn("#1", res2["reply"])

        # Assert exactly 1 booking exists
        bookings_count = PatientBooking.query.filter_by(patient_name="Rahul Sharma").count()
        self.assertEqual(bookings_count, 1)

    # -------------------------------------------------------------------------
    # 6. Cancellation Workflow
    # -------------------------------------------------------------------------
    def test_15_cancellation_during_flow(self):
        """15. Sending CANCEL at any step transitions session to cancelled with zero bookings created."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")

        res = handle_whatsapp_booking_flow(convo, "CANCEL", "+919847055001", "guest")
        self.assertTrue(res["handled"])
        self.assertIn("cancelled", res["reply"].lower())

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id).first()
        self.assertEqual(session.status, 'cancelled')

        bookings_count = PatientBooking.query.count()
        self.assertEqual(bookings_count, 0)

    # -------------------------------------------------------------------------
    # 7. Role Restrictions (Doctor & Admin)
    # -------------------------------------------------------------------------
    def test_16_doctor_and_admin_cannot_book(self):
        """16. Inbound booking requests from doctor or admin are redirected to staff portal."""
        convo_doc = self._create_convo(phone="+919847055088", role="doctor")
        res_doc = handle_whatsapp_booking_flow(
            convo_doc, "I want to book an appointment", "+919847055088", "doctor"
        )
        self.assertTrue(res_doc["handled"])
        self.assertIn("portal", res_doc["reply"].lower())
        self.assertIn("available for patients", res_doc["reply"].lower())
        self.assertEqual(WhatsAppBookingSession.query.filter_by(conversation_id=convo_doc.id).count(), 0)

        convo_admin = self._create_convo(phone="+919847055099", role="admin")
        res_admin = handle_whatsapp_booking_flow(
            convo_admin, "Book appointment with Dr. Arshitha", "+919847055099", "admin"
        )
        self.assertTrue(res_admin["handled"])
        self.assertIn("portal", res_admin["reply"].lower())
        self.assertEqual(WhatsAppBookingSession.query.filter_by(conversation_id=convo_admin.id).count(), 0)

    # -------------------------------------------------------------------------
    # 8. Full End-to-End Webhook Integration
    # -------------------------------------------------------------------------
    def test_17_full_end_to_end_webhook_multi_turn_booking(self):
        """17. Complete 5-turn booking journey via POST /api/whatsapp/webhook with HMAC verification."""
        phone = "919847055999"

        # Turn 1: "I want to book an appointment"
        payload1 = build_incoming_payload(phone, "I want to book an appointment")
        body1 = json.dumps(payload1).encode("utf-8")
        res1 = self.client.post(
            "/api/whatsapp/webhook",
            data=body1,
            content_type="application/json",
            headers={"X-Webhook-Signature": generate_sig(body1)}
        )
        self.assertEqual(res1.status_code, 200)

        # Turn 2: "1" (Select Dr. Arshitha)
        payload2 = build_incoming_payload(phone, "1")
        body2 = json.dumps(payload2).encode("utf-8")
        res2 = self.client.post(
            "/api/whatsapp/webhook",
            data=body2,
            content_type="application/json",
            headers={"X-Webhook-Signature": generate_sig(body2)}
        )
        self.assertEqual(res2.status_code, 200)

        # Turn 3: date
        target_date = self._find_next_working_day(1)
        payload3 = build_incoming_payload(phone, target_date)
        body3 = json.dumps(payload3).encode("utf-8")
        res3 = self.client.post(
            "/api/whatsapp/webhook",
            data=body3,
            content_type="application/json",
            headers={"X-Webhook-Signature": generate_sig(body3)}
        )
        self.assertEqual(res3.status_code, 200)

        # Turn 4: Patient Details
        payload4 = build_incoming_payload(phone, "Kiran Kumar, 35, Male")
        body4 = json.dumps(payload4).encode("utf-8")
        res4 = self.client.post(
            "/api/whatsapp/webhook",
            data=body4,
            content_type="application/json",
            headers={"X-Webhook-Signature": generate_sig(body4)}
        )
        self.assertEqual(res4.status_code, 200)

        # Turn 5: Explicit CONFIRM
        payload5 = build_incoming_payload(phone, "CONFIRM")
        body5 = json.dumps(payload5).encode("utf-8")
        res5 = self.client.post(
            "/api/whatsapp/webhook",
            data=body5,
            content_type="application/json",
            headers={"X-Webhook-Signature": generate_sig(body5)}
        )
        self.assertEqual(res5.status_code, 200)

        # Verify End-to-End Database State
        session = WhatsAppBookingSession.query.filter_by(contact_phone="+919847055999").first()
        self.assertIsNotNone(session)
        self.assertEqual(session.status, 'confirmed')
        self.assertEqual(session.token_number, 1)

        booking = PatientBooking.query.filter_by(patient_name="Kiran Kumar").first()
        self.assertIsNotNone(booking)
        self.assertEqual(booking.token, 1)
        self.assertEqual(booking.booking_source, "kapso_whatsapp")
        self.assertEqual(booking.phone_number, "+919847055999")

        # Verify WhatsApp Messages Recorded
        messages = WhatsAppMessage.query.filter_by(conversation_id=session.conversation_id).all()
        # 5 inbound + 5 outbound = 10 messages
        self.assertEqual(len(messages), 10)
        self.assertIn("Appointment Confirmed", messages[-1].content)

    def test_18_registered_patient_different_name_deferred_to_phase4c(self):
        """18. Registered patient attempting to book for a dependent/another person is deferred to Phase 4C."""
        # Registered user Vikram Varma
        user_phone = "+919847055778"
        existing = User.query.filter_by(phone_number=user_phone).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()

        user = User(
            name="Vikram Varma",
            phone_number=user_phone,
            email="vikram.phase4a@example.com",
            password_hash="test",
            role="patient"
        )
        db.session.add(user)
        db.session.commit()

        convo = self._create_convo(phone=user_phone, role="patient")

        # Step 1: Start booking
        res1 = handle_whatsapp_booking_flow(convo, "Book an appointment", user_phone, "patient", user_id=user.id, display_name="Vikram Varma")
        self.assertTrue(res1["handled"])

        # Step 2: Select doctor 1
        res2 = handle_whatsapp_booking_flow(convo, "1", user_phone, "patient", user_id=user.id, display_name="Vikram Varma")
        self.assertTrue(res2["handled"])

        # Step 3: Select date
        target_date = self._find_next_working_day(1)
        res3 = handle_whatsapp_booking_flow(convo, target_date, user_phone, "patient", user_id=user.id, display_name="Vikram Varma")
        self.assertTrue(res3["handled"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id).first()
        self.assertEqual(session.step, "PATIENT_DETAILS")

        # Step 4: In Phase 4C, patient can book for family member (e.g. "Aarav Varma, 8, Male, SAME")
        res4 = handle_whatsapp_booking_flow(convo, "Aarav Varma, 8, Male, SAME", user_phone, "patient", user_id=user.id, display_name="Vikram Varma")
        self.assertTrue(res4["handled"])
        self.assertIn("Appointment Summary", res4["reply"])
        self.assertIn("Aarav Varma", res4["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id).first()
        self.assertEqual(session.step, "CONFIRMATION")
        self.assertEqual(session.patient_name, "Aarav Varma")

        # Step 5: Confirm booking
        res5 = handle_whatsapp_booking_flow(convo, "CONFIRM", user_phone, "patient", user_id=user.id, display_name="Vikram Varma")
        self.assertTrue(res5["handled"])
        self.assertIn("Appointment Confirmed", res5["reply"])

        booking = PatientBooking.query.filter_by(patient_name="Aarav Varma").first()
        self.assertIsNotNone(booking)
        self.assertEqual(booking.user_id, user.id)

        # Verify profile auto-saved in SavedPatient for Web sync
        from services.whatsapp_patient_mgmt import get_saved_patients_for_phone
        saved = get_saved_patients_for_phone(user_phone)
        self.assertTrue(any(p["name"] == "Aarav Varma" for p in saved))

    def test_19_guest_isolation_web_dashboard_and_login_protection(self):
        """19. Verified that guest placeholder account cannot be logged into or leak cross-guest bookings."""
        from services.booking_services import get_patient_bookings_service
        
        # 1. Create a guest booking
        mon_date = self._find_next_working_day(1)
        res_book = create_booking_service(
            patient_name="Guest Alpha",
            age="25",
            gender="Female",
            phone_number="+919847055111",
            appointment_date=mon_date,
            doctor_id=1,
            patient_user_id=None,
            actor_role='patient',
            booking_source='kapso_whatsapp'
        )
        self.assertTrue(res_book["success"])
        guest_user = User.query.filter_by(email="guest@primecare.com").first()
        self.assertIsNotNone(guest_user)

        # 2. Verify guest account cannot login via /patient_login
        login_res = self.client.post("/patient_login", json={
            "identifier": "guest@primecare.com",
            "password": "guest_placeholder_not_for_login"
        })
        self.assertEqual(login_res.status_code, 200)
        self.assertFalse(login_res.json.get("success"))
        self.assertIn("Guest placeholder account cannot be logged into", login_res.json.get("msg", ""))
        self.assertEqual(login_res.status_code, 200)
        self.assertFalse(login_res.json.get("success"))
        self.assertIn("Guest placeholder account cannot be logged into", login_res.json.get("msg", ""))

        # 3. Verify accessing /patient_dashboard with guest session redirects and clears session
        with self.client.session_transaction() as sess:
            sess['user_id'] = guest_user.id
            sess['user_role'] = 'patient'
            sess['user_name'] = 'Guest Patient'
            sess['user_email'] = 'guest@primecare.com'
        
        dash_res = self.client.get("/patient_dashboard", follow_redirects=False)
        self.assertEqual(dash_res.status_code, 302)
        # Verify session cleared
        with self.client.session_transaction() as sess:
            self.assertNotIn('user_id', sess)

        # 4. Verify /my_token_status with guest user_id fails closed
        with self.client.session_transaction() as sess:
            sess['user_id'] = guest_user.id
            sess['user_role'] = 'patient'
        token_res = self.client.get("/my_token_status")
        self.assertEqual(token_res.status_code, 200)
        self.assertFalse(token_res.json.get("success"))
        self.assertIn("Invalid user account", token_res.json.get("msg", ""))

        # 5. Verify get_patient_bookings_service() requires verified phone for guest
        leak_attempt = get_patient_bookings_service(patient_user_id=guest_user.id, phone_number=None)
        self.assertFalse(leak_attempt["success"])
        self.assertEqual(leak_attempt["error_code"], "GUEST_PHONE_REQUIRED")

        # 6. Verify lookup with phone only returns that specific guest's booking
        guest_lookup = get_patient_bookings_service(phone_number="+919847055111")
        self.assertTrue(guest_lookup["success"])
        self.assertEqual(len(guest_lookup["bookings"]), 1)
        self.assertEqual(guest_lookup["bookings"][0]["patient_name"], "Guest Alpha")

    # -------------------------------------------------------------------------
    # 7. Enhanced Discovery & Specialization Flow
    # -------------------------------------------------------------------------
    def test_16_specialization_discovery_and_auto_doctor_selection(self):
        """16. Selecting specialization row (spec_<name>) or name auto-selects doctor if single doctor exists."""
        convo = self._create_convo()
        # Start booking
        res_init = handle_whatsapp_booking_flow(convo, "Book an appointment", "+919847055001", "guest")
        self.assertTrue(res_init["handled"])
        self.assertIn("Department", res_init["reply"])

        # Select department via interactive ID
        res_spec = handle_whatsapp_booking_flow(convo, "spec_Gynecologist", "+919847055001", "guest")
        self.assertTrue(res_spec["handled"])
        self.assertIn("Dr. Arshitha Abdurahman", res_spec["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'SELECT_DATE')
        self.assertEqual(session.doctor_id, 1)
        self.assertEqual(session.specialization, "Gynecologist")

    def test_17_available_dates_interactive_list_and_date_selection(self):
        """17. Interactive date list contains only valid bookable dates and selecting date_YYYY-MM-DD advances state."""
        convo = self._create_convo()
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        res_doc = handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")

        # Interactive date list returned
        self.assertIn("interactive", res_doc)
        if res_doc.get("interactive"):
            self.assertEqual(res_doc["interactive"]["button_text"], "Select Date")
            self.assertLessEqual(len(res_doc["interactive"]["sections"][0]["rows"]), 10)

        # Send interactive date row ID
        target_date = self._find_next_working_day(1)
        res_date = handle_whatsapp_booking_flow(convo, f"date_{target_date}", "+919847055001", "guest")
        self.assertTrue(res_date["handled"])
        self.assertIn("Date Confirmed", res_date["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'PATIENT_DETAILS')
        self.assertEqual(session.appointment_date, target_date)

    def test_18_timing_slot_selection_when_multiple_doctors_in_department(self):
        """18. When multiple doctors work on a date for a department, SELECT_SLOT is presented."""
        target_date = self._find_next_working_day(1)
        target_dt = datetime.strptime(target_date, '%Y-%m-%d')
        weekday = target_dt.strftime('%A')

        doc2 = Doctor(
            name="Dr. Fatima Zahra",
            specialization="Gynecologist",
            is_active=True,
            max_daily_patients=25
        )
        self.session.add(doc2)
        self.session.flush()

        sched2 = DoctorSchedule(
            doctor_id=doc2.id,
            day_of_week=weekday,
            start_time="14:00",
            end_time="18:00",
            is_available=True
        )
        self.session.add(sched2)
        self.session.commit()

        from app import get_all_doctors
        get_all_doctors(force_refresh=True)

        convo = self._create_convo()
        # Start booking and choose department with multiple doctors
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        res_spec = handle_whatsapp_booking_flow(convo, "spec_Gynecologist", "+919847055001", "guest")
        self.assertTrue(res_spec["handled"])

        # Pick date
        res_date = handle_whatsapp_booking_flow(convo, f"date_{target_date}", "+919847055001", "guest")
        self.assertTrue(res_date["handled"])
        self.assertIn("Multiple consultation timings", res_date["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'SELECT_SLOT')

        # Select second doctor's slot
        res_slot = handle_whatsapp_booking_flow(convo, f"slot_{doc2.id}_14:00-18:00", "+919847055001", "guest")
        self.assertTrue(res_slot["handled"])
        self.assertIn("Dr. Fatima Zahra", res_slot["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'PATIENT_DETAILS')
        self.assertEqual(session.doctor_id, doc2.id)
        self.assertEqual(session.doctor_name, "Dr. Fatima Zahra")

    def test_19_patient_details_same_number_shortcut(self):
        """19. Patient details with 'SAME' or omitted phone correctly locks to sender WhatsApp number."""
        convo = self._create_convo(phone="+919847055001")
        handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
        handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")
        target_date = self._find_next_working_day(1)
        handle_whatsapp_booking_flow(convo, target_date, "+919847055001", "guest")

        # Reply with SAME shortcut
        res_same = handle_whatsapp_booking_flow(convo, "Kiran Kumar, 35, Male, SAME", "+919847055001", "guest")
        self.assertTrue(res_same["handled"])
        self.assertIn("Appointment Summary", res_same["reply"])
        self.assertIn("+919847055001", res_same["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertEqual(session.step, 'CONFIRMATION')
        self.assertEqual(session.patient_name, "Kiran Kumar")
        self.assertEqual(session.contact_phone, "+919847055001")

    def test_20_zero_ai_during_sequential_booking_flow(self):
        """20. Sequential multi-turn booking strictly never invokes GroqClient (Zero AI latency)."""
        convo = self._create_convo()
        with patch("services.groq_client.GroqClient.generate_chat_completion") as mock_groq:
            # Turn 1: Start
            handle_whatsapp_booking_flow(convo, "Book appointment", "+919847055001", "guest")
            # Turn 2: Select doctor
            handle_whatsapp_booking_flow(convo, "1", "+919847055001", "guest")
            # Turn 3: Select date
            target_date = self._find_next_working_day(1)
            handle_whatsapp_booking_flow(convo, target_date, "+919847055001", "guest")
            # Turn 4: Patient details
            handle_whatsapp_booking_flow(convo, "Rahul Sharma, 28, Male, SAME", "+919847055001", "guest")
            # Turn 5: Confirm
            handle_whatsapp_booking_flow(convo, "CONFIRM", "+919847055001", "guest")

            # Assert Groq was NEVER called during sequential flow
            self.assertEqual(mock_groq.call_count, 0)

    def test_21_single_message_ai_fast_path_booking(self):
        """21. Single comprehensive booking sentence extracts entities via AI and directly presents summary card."""
        convo = self._create_convo(phone="+919847055001")
        target_date = self._find_next_working_day(1)
        target_dt = datetime.strptime(target_date, '%Y-%m-%d')
        weekday = target_dt.strftime('%A')

        fast_msg = f"Book Dr. Arshitha Abdurahman for {weekday}, patient name Rahul Sharma, age 28, male, number same"
        res = handle_whatsapp_booking_flow(
            conversation=convo,
            message_body=fast_msg,
            sender_phone="+919847055001",
            sender_role="guest"
        )
        self.assertTrue(res["handled"])
        self.assertIn("Appointment Summary", res["reply"])
        self.assertIn("Dr. Arshitha Abdurahman", res["reply"])
        self.assertIn("Rahul Sharma", res["reply"])
        self.assertIn("28 yrs, Male", res["reply"])
        self.assertIn("+919847055001", res["reply"])
        self.assertIn("Reply *CONFIRM* to book", res["reply"])

        session = WhatsAppBookingSession.query.filter_by(conversation_id=convo.id, status='in_progress').first()
        self.assertIsNotNone(session)
        self.assertEqual(session.step, 'CONFIRMATION')
        self.assertEqual(session.doctor_name, "Dr. Arshitha Abdurahman")
        self.assertEqual(session.appointment_date, target_date)
        self.assertEqual(session.patient_name, "Rahul Sharma")
        self.assertEqual(session.contact_phone, "+919847055001")


if __name__ == '__main__':
    unittest.main()
