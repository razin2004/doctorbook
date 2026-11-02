import os
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import json
from app import app, db, User, DoctorSession, DoctorInvitation, OTP, normalize_phone_number, ADMIN_EMAIL

class DoctorMobileUpdatesTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()
        cls.email_patcher = patch('app.send_email', return_value=True)
        cls.mock_send_email = cls.email_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.email_patcher.stop()

    def test_01_admin_add_doctor_without_confirm_phone_succeeds(self):
        """1. Admin adding doctor without confirm_phone_number succeeds (no longer mandatory)"""
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        email = "doc_noconfirm@primecare.com"
        phone = "9847999888"
        norm_phone = normalize_phone_number(phone)

        with app.app_context():
            User.query.filter_by(email=email).delete()
            old_doc = DoctorSession.query.filter_by(email=email).first()
            if old_doc:
                DoctorInvitation.query.filter_by(doctor_id=old_doc.id).delete()
                db.session.delete(old_doc)
            db.session.commit()

        # Mock gspread client create and doctors_ws
        with patch('app.client.create') as mock_create, \
             patch('app.doctors_ws.append_row') as mock_append, \
             patch('app.doctors_ws.get_all_values', return_value=[["No", "Name", "Specialization", "Days", "Time", "Sheet", "URL", "Img", "Email", "Phone_Number"]]), \
             patch('app.doctors_ws.get_all_records', return_value=[]):

            mock_sheet = MagicMock()
            mock_create.return_value = mock_sheet
            mock_sheet.id = "mock_sheet_noconfirm_123"

            # Form data WITHOUT confirm_phone_number
            res = self.client.post('/admin_add_doctor', data={
                'name': 'Dr. NoConfirm Tester',
                'email': email,
                'phone_number': phone, # Single phone number only
                'specialization': 'General Medicine',
                'days': 'Monday, Tuesday',
                'day_times': json.dumps({'Monday': '09:00 - 13:00', 'Tuesday': '09:00 - 13:00'})
            })
            data = res.get_json()
            self.assertTrue(data.get('success'), f"Add doctor without confirm phone failed: {data.get('msg')}")

        with app.app_context():
            doc = DoctorSession.query.filter_by(email=email).first()
            self.assertIsNotNone(doc)
            self.assertEqual(doc.phone_number, norm_phone)
            self.assertEqual(doc.phone_updated_by, ADMIN_EMAIL)
        print("  [PASS] 1. Admin adding doctor without confirm_phone succeeds")

    def test_02_doctor_send_change_phone_otp_whatsapp(self):
        """2. Logged-in doctor requests change phone OTP via WhatsApp"""
        doc_email = "doctest_change@primecare.com"
        current_phone = "+919847111222"
        new_phone = "9847333444"
        norm_new_phone = normalize_phone_number(new_phone)

        with app.app_context():
            User.query.filter_by(email=doc_email).delete()
            DoctorSession.query.filter_by(email=doc_email).delete()
            doc = DoctorSession(
                doctor_name="Dr. Mobile Changer",
                specialization="Dermatology",
                email=doc_email,
                phone_number=current_phone,
                status="idle"
            )
            user = User(
                name="Dr. Mobile Changer",
                email=doc_email,
                phone_number=current_phone,
                role="doctor",
                email_verified=True,
                phone_verified=False,
                password_hash="fakehash"
            )
            db.session.add(doc)
            db.session.add(user)
            db.session.commit()
            uid = user.id

        # Doctor logged in session
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['user_role'] = 'doctor'
            sess['user_email'] = doc_email
            sess['user_id'] = uid

        with patch('app.send_whatsapp_otp', return_value=True) as mock_wa:
            res = self.client.post('/doctor_send_change_phone_otp', json={
                'new_phone_number': new_phone
            })
            data = res.get_json()
            self.assertTrue(data.get('success'), f"Request OTP failed: {data.get('msg')}")
            self.assertEqual(data.get('phone'), norm_new_phone)
            self.assertTrue(mock_wa.called)

        with app.app_context():
            otp_key = f"doctor_phone_change_{doc_email.lower()}"
            otp_rec = OTP.query.filter_by(email=otp_key).first()
            self.assertIsNotNone(otp_rec)
            self.assertEqual(len(otp_rec.otp), 6)
        print("  [PASS] 2. Logged-in doctor requests change phone OTP via WhatsApp")

    def test_03_doctor_verify_change_phone_otp_updates_profile(self):
        """3. Doctor verifies WhatsApp OTP: updates DoctorSession, User, phone_verified, and Sheet"""
        doc_email = "doctest_change@primecare.com"
        norm_new_phone = "+919847333444"

        with app.app_context():
            otp_key = f"doctor_phone_change_{doc_email.lower()}"
            otp_rec = OTP.query.filter_by(email=otp_key).first()
            valid_otp = otp_rec.otp

        with self.client.session_transaction() as sess:
            sess['user_role'] = 'doctor'
            sess['user_email'] = doc_email
            sess['doc_pending_new_phone'] = norm_new_phone

        # Wrong OTP test
        res_wrong = self.client.post('/doctor_verify_change_phone_otp', json={'otp': '000000'})
        data_wrong = res_wrong.get_json()
        self.assertFalse(data_wrong.get('success'))
        self.assertIn("Invalid OTP", data_wrong.get('msg'))

        # Correct OTP test
        with patch('app.doctors_ws.get_all_values', return_value=[
            ["No", "Name", "Specialization", "Days", "Time", "Sheet", "URL", "Img", "Email", "Phone_Number"],
            ["1", "Dr. Mobile Changer", "Dermatology", "Monday", "09:00", "s", "u", "i", doc_email, "+919847111222"]
        ]), patch('app.doctors_ws.update') as mock_sheet_update:

            res_correct = self.client.post('/doctor_verify_change_phone_otp', json={'otp': valid_otp})
            data_correct = res_correct.get_json()
            self.assertTrue(data_correct.get('success'), f"Verify OTP failed: {data_correct.get('msg')}")
            self.assertEqual(data_correct.get('new_phone'), norm_new_phone)
            self.assertTrue(mock_sheet_update.called)

        with app.app_context():
            doc = DoctorSession.query.filter_by(email=doc_email).first()
            user = User.query.filter_by(email=doc_email).first()
            self.assertEqual(doc.phone_number, norm_new_phone)
            self.assertEqual(user.phone_number, norm_new_phone)
            self.assertTrue(user.phone_verified)
            self.assertEqual(doc.phone_updated_by, doc_email)
        print("  [PASS] 3. Doctor verifies WhatsApp OTP and updates mobile number across DB & Sheets")

if __name__ == '__main__':
    unittest.main()
