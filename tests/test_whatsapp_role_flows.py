"""
tests/test_whatsapp_role_flows.py
=================================
Automated test suite verifying role-based WhatsApp flows:
  - Admin Flow (stats, doctor roster toggle, holiday declaration, booking cancellation).
  - Doctor Flow (queue inspection, call next patient, status updates, doctor leave).
  - Role isolation and strict authorization gates.
"""

import os
import uuid
import unittest
from datetime import datetime, date, timedelta
from unittest.mock import patch, MagicMock

os.environ['DATABASE_URL'] = 'sqlite:///instance/test_primecare.db'
os.environ['APP_ENV'] = 'testing'
os.environ['TESTING'] = 'true'
os.environ['ADMIN_WHATSAPP_PHONE'] = '+919847055099'

from app import (
    app, db, User, Doctor, DoctorSchedule, DoctorLeave, ClinicHoliday,
    PatientBooking, WhatsAppConversation, WhatsAppMessage, AppSettings
)
from services.whatsapp_admin_flow import (
    handle_whatsapp_admin_flow, build_admin_menu_interactive_payload
)
from services.whatsapp_doctor_flow import (
    handle_whatsapp_doctor_flow, build_doctor_menu_interactive_payload
)
from services.whatsapp_webhook_service import process_inbound_webhook
from whatsapp_role_resolver import resolve_whatsapp_sender_role


class WhatsAppRoleFlowsTestCase(unittest.TestCase):
    def setUp(self):
        self.app_context = app.app_context()
        self.app_context.push()
        self.session = db.session

        # Clean tables (preserve baseline doc1)
        self.session.query(WhatsAppMessage).delete()
        self.session.query(WhatsAppConversation).delete()
        self.session.query(PatientBooking).delete()
        self.session.query(DoctorLeave).delete()
        self.session.query(DoctorSchedule).filter(DoctorSchedule.doctor_id != 1).delete()
        self.session.query(ClinicHoliday).delete()
        self.session.query(Doctor).filter(Doctor.id != 1).delete()
        self.session.query(User).delete()
        self.session.commit()
        from app import get_all_doctors
        get_all_doctors(force_refresh=True)

        # Seed test doctor (use id != 1, e.g. doctor 2)
        self.doctor = Doctor.query.get(2)
        if not self.doctor:
            self.doctor = Doctor(
                id=2,
                name="Dr. Sarah Jenkins",
                specialization="General Medicine",
                phone_number="+919847055003",
                is_active=True,
                max_daily_patients=25
            )
            self.session.add(self.doctor)
            self.session.commit()

        # Seed test patient user
        self.user = User(
            name="Test Patient",
            role="patient",
            phone_number="+919847055100",
            email="test_role_patient@primecare.com",
            password_hash="pbkdf2:sha256:dummyhash"
        )
        self.session.add(self.user)
        self.session.commit()

        # Seed admin setting (preserve original value for clean tearDown)
        admin_setting = AppSettings.query.filter_by(key="admin_whatsapp_phone").first()
        self._original_admin_phone = admin_setting.value if admin_setting else None
        if not admin_setting:
            admin_setting = AppSettings(key="admin_whatsapp_phone", value="+919847055099")
            self.session.add(admin_setting)
        else:
            admin_setting.value = "+919847055099"
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        # Restore original admin phone
        admin_setting = AppSettings.query.filter_by(key="admin_whatsapp_phone").first()
        if admin_setting:
            if self._original_admin_phone:
                admin_setting.value = self._original_admin_phone
            else:
                self.session.delete(admin_setting)
            self.session.commit()
        self.session.query(WhatsAppMessage).delete()
        self.session.query(WhatsAppConversation).delete()
        self.session.query(PatientBooking).delete()
        self.session.query(DoctorLeave).delete()
        self.session.query(DoctorSchedule).filter(DoctorSchedule.doctor_id != 1).delete()
        self.session.query(ClinicHoliday).delete()
        self.session.query(Doctor).filter(Doctor.id != 1).delete()
        self.session.query(User).delete()
        self.session.commit()
        from app import get_all_doctors
        get_all_doctors(force_refresh=True)
        self.app_context.pop()

    def _create_convo(self, phone):
        convo = WhatsAppConversation(
            conversation_uuid=str(uuid.uuid4()),
            sender_phone=phone,
            status='active'
        )
        self.session.add(convo)
        self.session.commit()
        return convo

    # -------------------------------------------------------------
    # 1. Administrator Flow Tests
    # -------------------------------------------------------------
    def test_01_admin_greeting_receives_admin_menu(self):
        convo = self._create_convo("+919847055099")
        res = handle_whatsapp_admin_flow(
            conversation=convo,
            message_body="menu",
            sender_phone="+919847055099",
            sender_role="admin"
        )
        self.assertTrue(res["handled"])
        self.assertIn("Administrator Portal", res["reply"])
        self.assertIn("interactive", res)
        self.assertEqual(res["interactive"]["button_text"], "Admin Controls")

    def test_02_admin_stats_overview(self):
        convo = self._create_convo("+919847055099")
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        # Add test booking for today
        b = PatientBooking(
            user_id=self.user.id,
            doctor_id=self.doctor.id,
            patient_name="John Doe",
            date=today_str,
            token=1,
            status="confirmed",
            phone_number="+919847055100"
        )
        self.session.add(b)
        self.session.commit()

        res = handle_whatsapp_admin_flow(
            conversation=convo,
            message_body="stats",
            sender_phone="+919847055099",
            sender_role="admin"
        )
        self.assertTrue(res["handled"])
        self.assertIn("Daily Overview", res["reply"])
        self.assertIn("Total Booked: *1*", res["reply"])
        self.assertIn("Waiting in Queue: *1*", res["reply"])

    def test_03_admin_toggle_doctor_status(self):
        convo = self._create_convo("+919847055099")
        self.assertTrue(self.doctor.is_active)

        # Toggle off
        res_off = handle_whatsapp_admin_flow(
            conversation=convo,
            message_body=f"/doctor {self.doctor.id} off",
            sender_phone="+919847055099",
            sender_role="admin"
        )
        self.assertTrue(res_off["handled"])
        self.assertIn("INACTIVE", res_off["reply"])
        db.session.refresh(self.doctor)
        self.assertFalse(self.doctor.is_active)

        # Toggle on
        res_on = handle_whatsapp_admin_flow(
            conversation=convo,
            message_body=f"/doctor {self.doctor.id} on",
            sender_phone="+919847055099",
            sender_role="admin"
        )
        self.assertTrue(res_on["handled"])
        self.assertIn("ACTIVE", res_on["reply"])
        db.session.refresh(self.doctor)
        self.assertTrue(self.doctor.is_active)

    def test_04_admin_declare_holiday(self):
        convo = self._create_convo("+919847055099")
        target_holiday = (date.today() + timedelta(days=5)).strftime('%Y-%m-%d')

        res = handle_whatsapp_admin_flow(
            conversation=convo,
            message_body=f"/holiday {target_holiday} Deepavali Holiday",
            sender_phone="+919847055099",
            sender_role="admin"
        )
        self.assertTrue(res["handled"])
        self.assertIn("Clinic Holiday Declared", res["reply"])
        self.assertIn(target_holiday, res["reply"])

        h = ClinicHoliday.query.filter_by(date=target_holiday).first()
        self.assertIsNotNone(h)
        self.assertEqual(h.reason, "Deepavali Holiday")

    def test_05_admin_flow_blocks_non_admin(self):
        convo = self._create_convo("+919847055001")
        res = handle_whatsapp_admin_flow(
            conversation=convo,
            message_body="stats",
            sender_phone="+919847055001",
            sender_role="patient"
        )
        self.assertFalse(res["handled"])

    # -------------------------------------------------------------
    # 2. Doctor Flow Tests
    # -------------------------------------------------------------
    def test_06_doctor_greeting_receives_doctor_menu(self):
        convo = self._create_convo("+919847055003")
        res = handle_whatsapp_doctor_flow(
            conversation=convo,
            message_body="menu",
            sender_phone="+919847055003",
            sender_role="doctor",
            doctor_id=self.doctor.id,
            display_name=self.doctor.name
        )
        self.assertTrue(res["handled"])
        self.assertIn("Doctor Workspace", res["reply"])
        self.assertIn("interactive", res)
        self.assertEqual(res["interactive"]["button_text"], "Doctor Menu")

    def test_07_doctor_queue_inspection(self):
        convo = self._create_convo("+919847055003")
        today_str = datetime.now().strftime('%Y-%m-%d')

        b1 = PatientBooking(
            user_id=self.user.id,
            doctor_id=self.doctor.id,
            patient_name="Rahul Varma",
            age="28",
            date=today_str,
            token=1,
            status="confirmed",
            phone_number="+919847055101"
        )
        self.session.add(b1)
        self.session.commit()

        res = handle_whatsapp_doctor_flow(
            conversation=convo,
            message_body="queue",
            sender_phone="+919847055003",
            sender_role="doctor",
            doctor_id=self.doctor.id
        )
        self.assertTrue(res["handled"])
        self.assertIn("Rahul Varma", res["reply"])
        self.assertIn("Waiting Patients", res["reply"])

    def test_08_doctor_call_next_patient_advances_queue(self):
        convo = self._create_convo("+919847055003")
        today_str = datetime.now().strftime('%Y-%m-%d')

        b1 = PatientBooking(
            user_id=self.user.id,
            doctor_id=self.doctor.id,
            patient_name="Amina Beevi",
            age="34",
            date=today_str,
            token=1,
            status="confirmed",
            phone_number="+919847055102"
        )
        b2 = PatientBooking(
            user_id=self.user.id,
            doctor_id=self.doctor.id,
            patient_name="George Thomas",
            age="45",
            date=today_str,
            token=2,
            status="confirmed",
            phone_number="+919847055103"
        )
        self.session.add_all([b1, b2])
        self.session.commit()

        # Doctor calls NEXT
        with patch('services.kapso_client.KapsoClient.send_text_message', return_value={"success": True}):
            res_call1 = handle_whatsapp_doctor_flow(
                conversation=convo,
                message_body="next",
                sender_phone="+919847055003",
                sender_role="doctor",
                doctor_id=self.doctor.id
            )
        self.assertTrue(res_call1["handled"])
        self.assertIn("Amina Beevi", res_call1["reply"])
        self.assertIn("Now Consulting: *Token #1*", res_call1["reply"])

        db.session.refresh(b1)
        self.assertEqual(b1.status, 'in_consultation')

        # Doctor calls NEXT again -> b1 completes, b2 becomes in_consultation
        with patch('services.kapso_client.KapsoClient.send_text_message', return_value={"success": True}):
            res_call2 = handle_whatsapp_doctor_flow(
                conversation=convo,
                message_body="next",
                sender_phone="+919847055003",
                sender_role="doctor",
                doctor_id=self.doctor.id
            )
        self.assertTrue(res_call2["handled"])
        self.assertIn("George Thomas", res_call2["reply"])
        self.assertIn("Now Consulting: *Token #2*", res_call2["reply"])

        db.session.refresh(b1)
        db.session.refresh(b2)
        self.assertEqual(b1.status, 'completed')
        self.assertEqual(b2.status, 'in_consultation')

    def test_09_doctor_apply_leave(self):
        convo = self._create_convo("+919847055003")
        target_leave = (date.today() + timedelta(days=7)).strftime('%Y-%m-%d')

        res = handle_whatsapp_doctor_flow(
            conversation=convo,
            message_body=f"/leave {target_leave} Medical Conference",
            sender_phone="+919847055003",
            sender_role="doctor",
            doctor_id=self.doctor.id
        )
        self.assertTrue(res["handled"])
        self.assertIn("Doctor Leave Registered", res["reply"])
        self.assertIn(target_leave, res["reply"])

        leave = DoctorLeave.query.filter_by(doctor_id=self.doctor.id, date=target_leave).first()
        self.assertIsNotNone(leave)
        self.assertEqual(leave.reason, "Medical Conference")

    def test_10_doctor_flow_blocks_non_doctor(self):
        convo = self._create_convo("+919847055001")
        res = handle_whatsapp_doctor_flow(
            conversation=convo,
            message_body="queue",
            sender_phone="+919847055001",
            sender_role="patient"
        )
        self.assertFalse(res["handled"])


if __name__ == '__main__':
    unittest.main()
