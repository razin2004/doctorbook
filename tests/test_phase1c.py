import os
import sys
import unittest
import json
import secrets
import hashlib
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Ensure root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import (
    app, db, User, DoctorSession, DoctorInvitation, PatientBooking, AppSettings, 
    OTP, SavedPatient, build_clinic_context, ADMIN_EMAIL, sync_doctors_from_sheet
)
from werkzeug.security import generate_password_hash, check_password_hash
from phone_utils import normalize_phone_number
from whatsapp_role_resolver import resolve_whatsapp_sender_role

class Phase1CTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

        # Mock send_email so tests do not depend on SMTP/Brevo network timeouts
        cls.email_patcher = patch('app.send_email', return_value=True)
        cls.mock_send_email = cls.email_patcher.start()

        # Setup baseline test entities
        with app.app_context():
            # Setup Admin WhatsApp Phone
            admin_setting = AppSettings.query.filter_by(key="admin_whatsapp_phone").first()
            if not admin_setting:
                admin_setting = AppSettings(key="admin_whatsapp_phone", value="+918592031725")
                db.session.add(admin_setting)
            else:
                admin_setting.value = "+918592031725"

            # Setup Doctor Session
            doc = DoctorSession.query.filter_by(email="cardiodoc@primecare.com").first()
            if not doc:
                doc = DoctorSession(
                    doctor_name="Dr. Cardio Test",
                    specialization="Cardiology",
                    email="cardiodoc@primecare.com",
                    phone_number="+919847000001",
                    status="idle",
                    phone_updated_by=ADMIN_EMAIL,
                    phone_updated_at=datetime.utcnow()
                )
                db.session.add(doc)
            else:
                doc.phone_number = "+919847000001"

            # Setup Activated Doctor User
            doc_user = User.query.filter_by(email="cardiodoc@primecare.com").first()
            if not doc_user:
                doc_user = User(
                    name="Dr. Cardio Test",
                    email="cardiodoc@primecare.com",
                    phone_number="+919847000001",
                    password_hash=generate_password_hash("docsecure123"),
                    role="doctor",
                    email_verified=True,
                    phone_verified=False  # Key: Doctor mobile is NOT OTP-verified
                )
                db.session.add(doc_user)
            else:
                doc_user.role = "doctor"
                doc_user.email_verified = True
                doc_user.phone_verified = False
                doc_user.password_hash = generate_password_hash("docsecure123")
                doc_user.phone_number = "+919847000001"

            # Setup Patient User
            pat_user = User.query.filter_by(phone_number="+919847111001").first()
            if not pat_user:
                pat_user = User(
                    name="Verified Patient Test",
                    email="patienttest@primecare.com",
                    phone_number="+919847111001",
                    password_hash=generate_password_hash("patpass123"),
                    role="patient",
                    email_verified=False,
                    phone_verified=True
                )
                db.session.add(pat_user)
            else:
                pat_user.phone_verified = True

            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.email_patcher.stop()

    def test_01_add_doctor_without_mobile_fails(self):
        """1. Adding a doctor without a mobile number must fail"""
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        res = self.client.post('/admin_add_doctor', data={
            'name': 'Dr. No Phone',
            'email': 'nophone@primecare.com',
            'phone_number': '',
            'confirm_phone_number': '',
            'specialization': 'Pediatrics',
            'days': 'Monday, Tuesday',
            'day_times': json.dumps({'Monday': '09:00-11:00', 'Tuesday': '09:00-11:00'})
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn("mandatory", data.get('msg', '').lower())
        print("  [PASS] 1. Adding doctor without mobile number fails")

    def test_02_add_doctor_with_invalid_mobile_fails(self):
        """2. Adding a doctor with an invalid number must fail"""
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        res = self.client.post('/admin_add_doctor', data={
            'name': 'Dr. Bad Phone',
            'email': 'badphone@primecare.com',
            'phone_number': '12345',
            'confirm_phone_number': '12345',
            'specialization': 'Pediatrics',
            'days': 'Monday',
            'day_times': json.dumps({'Monday': '09:00-11:00'})
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn("invalid", data.get('msg', '').lower())
        print("  [PASS] 2. Adding doctor with invalid number fails")

    def test_03_add_doctor_with_conflicting_mobile_fails(self):
        """3. Adding a doctor with conflicting number must fail"""
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        # Conflict with existing patient number +919847111001
        res = self.client.post('/admin_add_doctor', data={
            'name': 'Dr. Conflict Phone',
            'email': 'conflictphone@primecare.com',
            'phone_number': '9847111001',
            'confirm_phone_number': '9847111001',
            'specialization': 'Dermatology',
            'days': 'Monday',
            'day_times': json.dumps({'Monday': '09:00-11:00'})
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertTrue("registered" in data.get('msg', '').lower() or "conflict" in data.get('msg', '').lower())
        print("  [PASS] 3. Adding doctor with conflicting number fails")

    @patch('app.client.create')
    @patch('app.doctors_ws.append_row')
    def test_04_add_doctor_with_valid_mobile_succeeds(self, mock_append, mock_create):
        """4. Adding a doctor with valid number must succeed"""
        mock_sheet = MagicMock()
        mock_doc = MagicMock()
        mock_doc.id = "mock_sheet_id_123"
        mock_doc.sheet1 = mock_sheet
        mock_create.return_value = mock_doc

        valid_phone = "9847000099"
        norm_phone = normalize_phone_number(valid_phone)

        with app.app_context():
            DoctorInvitation.query.filter_by(email="newdoc@primecare.com").delete()
            DoctorSession.query.filter_by(email="newdoc@primecare.com").delete()
            DoctorSession.query.filter_by(phone_number=norm_phone).delete()
            db.session.commit()

        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        res = self.client.post('/admin_add_doctor', data={
            'name': 'Dr. Valid New',
            'email': 'newdoc@primecare.com',
            'phone_number': valid_phone,
            'confirm_phone_number': valid_phone,
            'specialization': 'Neurology',
            'days': 'Monday, Wednesday',
            'day_times': json.dumps({'Monday': '10:00-12:00', 'Wednesday': '10:00-12:00'})
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Add doctor failed: {data.get('msg')}")

        with app.app_context():
            doc = DoctorSession.query.filter_by(email="newdoc@primecare.com").first()
            self.assertIsNotNone(doc)
            self.assertEqual(doc.phone_number, norm_phone)
            self.assertEqual(doc.phone_updated_by, ADMIN_EMAIL)
            self.assertIsNotNone(doc.phone_updated_at)
        print("  [PASS] 4. Adding doctor with valid number succeeds")

    @patch('app.doctors_ws.update')
    @patch('app.doctors_ws.get_all_values')
    def test_05_edit_doctor_mobile_succeeds(self, mock_get_all, mock_update):
        """5. Editing doctor mobile number must succeed"""
        mock_get_all.return_value = [
            ['No', 'Name', 'Specialization', 'Days', 'MondayTime', 'TuesdayTime', 'WednesdayTime', 'ThursdayTime', 'FridayTime', 'SaturdayTime', 'SundayTime', 'SheetName', 'SheetURL', 'Image', 'Email', 'Phone_Number'],
            ['1', 'Dr. Cardio Test', 'Cardiology', 'Monday, Tuesday', '09:00-11:00', '09:00-11:00', '', '', '', '', '', 'Sheet_1', 'http://sheet', '', 'cardiodoc@primecare.com', '+919847000001']
        ]

        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        new_phone = "9847000005"
        norm_phone = normalize_phone_number(new_phone)

        with app.app_context():
            DoctorSession.query.filter_by(phone_number=norm_phone).delete()
            db.session.commit()

        res = self.client.post('/admin_edit_doctor', json={
            'combined': 'Dr. Cardio Test - Cardiology',
            'email': 'cardiodoc@primecare.com',
            'phone': new_phone,
            'days': ['Monday', 'Tuesday'],
            'day_times': {'Monday': '09:00-11:00', 'Tuesday': '09:00-11:00'}
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Edit doctor failed: {data.get('msg')}")

        with app.app_context():
            doc = DoctorSession.query.filter_by(email="cardiodoc@primecare.com").first()
            self.assertEqual(doc.phone_number, norm_phone)
            self.assertEqual(doc.phone_updated_by, ADMIN_EMAIL)
            u = User.query.filter_by(email="cardiodoc@primecare.com").first()
            if u:
                self.assertEqual(u.phone_number, norm_phone)
        print("  [PASS] 5. Editing doctor mobile number succeeds")

    def test_06_conflicting_number_updates_fail(self):
        """6. Conflicting number updates must fail"""
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        # Attempt to update doctor to admin's phone +918592031725
        res = self.client.post('/admin_edit_doctor', json={
            'combined': 'Dr. Cardio Test - Cardiology',
            'email': 'cardiodoc@primecare.com',
            'phone': '8592031725',
            'days': ['Monday'],
            'day_times': {'Monday': '09:00-11:00'}
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn("conflict", data.get('msg', '').lower())
        print("  [PASS] 6. Conflicting number updates fail")

    @patch('app.doctors_ws.get_all_values')
    def test_07_google_sheets_phone_synchronization(self, mock_get_all):
        """7. Google Sheets phone synchronization must work"""
        with app.app_context():
            existing_docs = DoctorSession.query.all()
            sheet_rows = [
                ['No', 'Name', 'Specialization', 'Days', 'MondayTime', 'TuesdayTime', 'WednesdayTime', 'ThursdayTime', 'FridayTime', 'SaturdayTime', 'SundayTime', 'SheetName', 'SheetURL', 'Image', 'Email', 'Phone_Number']
            ]
            for idx, d in enumerate(existing_docs):
                phone = "+919847000007" if d.email == "cardiodoc@primecare.com" else (d.phone_number or "")
                sheet_rows.append([
                    str(idx + 1), d.doctor_name, d.specialization, 'Monday', '09:00-11:00', '', '', '', '', '', '', f'Sheet_{idx}', '', '', d.email, phone
                ])
            mock_get_all.return_value = sheet_rows

            sync_success = sync_doctors_from_sheet()
            self.assertTrue(sync_success, "sync_doctors_from_sheet should succeed")
            doc = DoctorSession.query.filter_by(email="cardiodoc@primecare.com").first()
            self.assertEqual(doc.phone_number, "+919847000007")
        print("  [PASS] 7. Google Sheets phone synchronization works")

    def test_08_doctor_email_login_works(self):
        """8. Doctor email login must work and redirect directly to dashboard"""
        with self.client.session_transaction() as sess:
            sess.clear()

        res = self.client.post('/doctor_login', json={
            'identifier': 'cardiodoc@primecare.com',
            'password': 'docsecure123'
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Doctor email login failed: {data.get('msg')}")
        self.assertEqual(data.get('redirect_url'), '/doctor_dashboard')
        print("  [PASS] 8. Doctor email login works")

    def test_09_doctor_mobile_login_works(self):
        """9. Doctor mobile login must work and redirect directly to dashboard"""
        with self.client.session_transaction() as sess:
            sess.clear()

        with app.app_context():
            doc = DoctorSession.query.filter_by(email="cardiodoc@primecare.com").first()
            doc_phone = doc.phone_number

        res = self.client.post('/doctor_login', json={
            'identifier': doc_phone,
            'password': 'docsecure123'
        })
        data = res.get_json()
        self.assertTrue(data.get('success'), f"Doctor mobile login failed: {data.get('msg')}")
        self.assertEqual(data.get('redirect_url'), '/doctor_dashboard')
        print("  [PASS] 9. Doctor mobile login works")

    def test_10_doctor_wrong_password_rejected(self):
        """10. Wrong password must be rejected"""
        res = self.client.post('/doctor_login', json={
            'identifier': 'cardiodoc@primecare.com',
            'password': 'wrongpassword999'
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn("invalid", data.get('msg', '').lower())
        print("  [PASS] 10. Wrong password rejected")

    def test_11_unactivated_doctor_login_rejected(self):
        """11. Unactivated doctor login must be rejected"""
        with app.app_context():
            doc_unact = DoctorSession.query.filter_by(email="unactdoc@primecare.com").first()
            if not doc_unact:
                doc_unact = DoctorSession(
                    doctor_name="Dr. Unactivated",
                    specialization="ENT",
                    email="unactdoc@primecare.com",
                    phone_number="+919847000011"
                )
                db.session.add(doc_unact)
                db.session.commit()

            # Ensure no active User exists
            User.query.filter_by(email="unactdoc@primecare.com").delete()
            # Add pending invitation
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
            inv = DoctorInvitation(
                doctor_id=doc_unact.id,
                email="unactdoc@primecare.com",
                token_hash=token_hash,
                expires_at=datetime.utcnow() + timedelta(hours=24),
                is_used=False
            )
            db.session.add(inv)
            db.session.commit()

        res = self.client.post('/doctor_login', json={
            'identifier': 'unactdoc@primecare.com',
            'password': 'anypassword'
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn("activated", data.get('msg', '').lower())
        print("  [PASS] 11. Unactivated doctor login rejected")

    def test_12_activated_doctor_with_phone_verified_false_accesses_dashboard(self):
        """12. Activated doctor with phone_verified=False must access Doctor Dashboard"""
        with app.app_context():
            u = User.query.filter_by(email="cardiodoc@primecare.com").first()
            doc = DoctorSession.query.filter_by(email="cardiodoc@primecare.com").first()
            user_id = u.id
            doc_id = doc.id
            # Verify requirement: phone_verified is False
            self.assertFalse(u.phone_verified)
            self.assertTrue(u.email_verified)

        with self.client.session_transaction() as sess:
            sess.clear()
            sess['user_id'] = user_id
            sess['user_email'] = 'cardiodoc@primecare.com'
            sess['user_role'] = 'doctor'
            sess['doctor_id'] = doc_id
            sess['doctor_name'] = 'Dr. Cardio Test'

        res = self.client.get('/doctor_dashboard')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Dr. Cardio Test", res.data)
        print("  [PASS] 12. Activated doctor with phone_verified=False accesses dashboard directly")

    def test_13_doctor_forgot_password_using_email_works(self):
        """13. Doctor forgot password using email must work"""
        res = self.client.post('/doctor_send_reset_otp', json={
            'identifier': 'cardiodoc@primecare.com'
        })
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertIn("sent", data.get('msg', '').lower())

        with app.app_context():
            otp_rec = OTP.query.filter_by(email="doctor_reset_cardiodoc@primecare.com").first()
            self.assertIsNotNone(otp_rec)
            self.assertEqual(len(otp_rec.otp), 6)
            self.assertGreater(otp_rec.expiry, datetime.utcnow())
        print("  [PASS] 13. Doctor forgot password using email works")

    def test_14_doctor_forgot_password_using_mobile_delivers_otp_to_email(self):
        """14. Doctor forgot password using mobile delivers OTP to registered email"""
        with app.app_context():
            doc = DoctorSession.query.filter_by(email="cardiodoc@primecare.com").first()
            doc_phone = doc.phone_number

        with self.client.session_transaction() as sess:
            sess['doc_reset_last_sent'] = (datetime.utcnow() - timedelta(seconds=70)).isoformat()

        res = self.client.post('/doctor_send_reset_otp', json={
            'identifier': doc_phone
        })
        data = res.get_json()
        self.assertTrue(data.get('success'))

        with app.app_context():
            otp_rec = OTP.query.filter_by(email="doctor_reset_cardiodoc@primecare.com").first()
            self.assertIsNotNone(otp_rec)
            otp_code = otp_rec.otp

        # Verify OTP
        res_ver = self.client.post('/doctor_verify_reset_otp', json={
            'identifier': doc_phone,
            'otp': otp_code
        })
        self.assertTrue(res_ver.get_json().get('success'))

        # Reset Password
        res_reset = self.client.post('/doctor_reset_password', json={
            'identifier': doc_phone,
            'otp': otp_code,
            'new_password': 'newpassword456',
            'confirm_password': 'newpassword456'
        })
        self.assertTrue(res_reset.get_json().get('success'))

        with app.app_context():
            u = User.query.filter_by(email="cardiodoc@primecare.com").first()
            self.assertTrue(check_password_hash(u.password_hash, 'newpassword456'))
        print("  [PASS] 14. Doctor forgot password using mobile delivers OTP to email & resets password")

    def test_15_patient_cannot_use_doctor_login(self):
        """15. Patient cannot use Doctor Login"""
        res = self.client.post('/doctor_login', json={
            'identifier': 'patienttest@primecare.com',
            'password': 'patpass123'
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertTrue("no doctor" in data.get('msg', '').lower() or "patient" in data.get('msg', '').lower())
        print("  [PASS] 15. Patient cannot use Doctor Login")

    def test_16_doctor_cannot_use_patient_login(self):
        """16. Doctor cannot use Patient Login"""
        with app.app_context():
            doc = DoctorSession.query.filter_by(email="cardiodoc@primecare.com").first()
            doc_phone = doc.phone_number

        res = self.client.post('/patient_login', json={
            'phone_number': doc_phone,
            'password': 'newpassword456'
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn("doctor", data.get('msg', '').lower())
        print("  [PASS] 16. Doctor cannot use Patient Login")

    def test_17_patient_login_interface_doctor_icon_works(self):
        """17. Patient login interface doctor icon works (portal navigation)"""
        res = self.client.get('/')
        html = res.data.decode('utf-8')
        self.assertIn('id="portalSwitchBtn"', html)
        self.assertIn('data-lucide="stethoscope"', html)
        self.assertIn("switchAuthView('doctorLogin')", html)
        print("  [PASS] 17. Patient login interface doctor icon present and functional")

    def test_18_doctor_login_interface_patient_icon_works(self):
        """18. Doctor login interface patient icon works (reciprocal navigation)"""
        res = self.client.get('/')
        html = res.data.decode('utf-8')
        self.assertIn('id="authView-doctorLogin"', html)
        self.assertIn('id="doclogin-identifier"', html)
        self.assertIn("switchAuthView('login')", html)
        self.assertIn("switchAuthView('doctorForgot')", html)
        print("  [PASS] 18. Doctor login interface patient icon & reciprocal navigation present")

    def test_19_doctor_role_resolver_recognizes_active_doctor_without_phone_otp(self):
        """19. Doctor role resolver recognizes an active admin-listed doctor without phone OTP"""
        with app.app_context():
            doc = DoctorSession.query.filter_by(email="cardiodoc@primecare.com").first()
            doc_phone = doc.phone_number
            u = User.query.filter_by(email="cardiodoc@primecare.com").first()
            # Explicit requirement: User.phone_verified is False
            self.assertFalse(u.phone_verified)
            self.assertTrue(u.email_verified)

            role_info = resolve_whatsapp_sender_role(doc_phone)
            self.assertEqual(role_info["role"], "doctor")
            self.assertEqual(role_info["status"], "success")
        print("  [PASS] 19. Doctor role resolver recognizes active doctor without phone OTP")

    def test_20_unactivated_doctor_number_resolves_to_guest(self):
        """20. Unactivated doctor number resolves to Guest"""
        with app.app_context():
            role_info = resolve_whatsapp_sender_role("+919847000011") # unactivated doc from test 11
            self.assertEqual(role_info["role"], "guest")
            self.assertEqual(role_info["status"], "guest")
        print("  [PASS] 20. Unactivated doctor number resolves to Guest")

    def test_21_patient_role_resolver_still_requires_phone_verification(self):
        """21. Patient role resolver still requires actual phone verification"""
        with app.app_context():
            # Verified patient resolves to patient
            ver_pat = resolve_whatsapp_sender_role("+919847111001")
            self.assertEqual(ver_pat["role"], "patient")
            self.assertEqual(ver_pat["status"], "success")

            # Temporarily unverify patient
            p = User.query.filter_by(phone_number="+919847111001").first()
            p.phone_verified = False
            db.session.commit()

            unver_pat = resolve_whatsapp_sender_role("+919847111001")
            self.assertEqual(unver_pat["role"], "guest")
            self.assertEqual(unver_pat["status"], "guest")

            # Restore verified status
            p.phone_verified = True
            db.session.commit()
        print("  [PASS] 21. Patient role resolver strictly requires actual phone verification")

    def test_22_admin_whatsapp_number_added_and_edited_without_otp(self):
        """22. Admin WhatsApp number can be added and edited without OTP"""
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        res = self.client.post('/admin_save_settings', json={
            'admin_whatsapp_phone': '8592031725',
            'confirm_admin_whatsapp_phone': '8592031725'
        })
        self.assertTrue(res.get_json().get('success'))

        with app.app_context():
            adm_role = resolve_whatsapp_sender_role("+918592031725")
            self.assertEqual(adm_role["role"], "admin")
            self.assertEqual(adm_role["status"], "success")
        print("  [PASS] 22. Admin WhatsApp number configured without OTP")

    def test_23_existing_booking_history_remains_intact(self):
        """23. Existing booking history remains intact"""
        with app.app_context():
            count = PatientBooking.query.count()
            self.assertGreaterEqual(count, 31)
        print("  [PASS] 23. Existing booking history remains intact (>= 31 bookings)")

    def test_24_existing_gemini_ai_intact(self):
        """24. Existing Gemini patient AI and Admin AI continue working"""
        routes = [rule.rule for rule in app.url_map.iter_rules()]
        self.assertIn('/ai_triage', routes)
        self.assertIn('/admin_ai_chat', routes)

        with app.app_context():
            ctx = build_clinic_context()
            self.assertIsInstance(ctx, str)
            self.assertIn("CLINIC DOCTOR DIRECTORY", ctx)
        print("  [PASS] 24. Existing Gemini AI endpoints and clinic context intact")

if __name__ == '__main__':
    unittest.main()
