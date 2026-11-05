"""
tests/test_doctor_mobile_updates.py
===================================
Tests for doctor mobile phone updates, confirm_phone_number decoupling,
and SHA-256 hashed OTP verification with fail-closed Kapso outbound control.
"""

import json
import unittest
import hashlib
from unittest.mock import patch, MagicMock

import tests.test_isolation
from app import app, db, User, DoctorSession, OTP, ADMIN_EMAIL, hash_otp
from phone_utils import normalize_phone_number
from tests.test_isolation import enforce_test_isolation


class DoctorMobileUpdatesTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        enforce_test_isolation(app)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

        # Disable external emails during tests
        cls.email_patcher = patch('app.send_email')
        cls.mock_send_email = cls.email_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.email_patcher.stop()
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_01_admin_add_doctor_without_confirm_phone_succeeds(self):
        """1. Admin adding doctor without confirm_phone_number succeeds (no longer mandatory)"""
        phone = "9847111099"
        norm_phone = normalize_phone_number(phone)
        email = "doctest_noconfirm@primecare.com"

        with app.app_context():
            User.query.filter_by(email=email).delete()
            User.query.filter_by(phone_number=norm_phone).delete()
            DoctorSession.query.filter_by(email=email).delete()
            DoctorSession.query.filter_by(phone_number=norm_phone).delete()
            db.session.commit()

        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_email'] = ADMIN_EMAIL
            sess['admin_logged_in'] = True

        res = self.client.post('/admin_add_doctor', data={
            'name': 'Dr. NoConfirm Test',
            'email': email,
            'phone_number': phone,
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
        """2. Logged-in doctor requests change phone OTP via WhatsApp (secured with SHA-256 and outbound flag)"""
        doc_email = "doctest_change@primecare.com"
        current_phone = "+919847111222"
        new_phone = "9847333444"
        norm_new_phone = normalize_phone_number(new_phone)

        with app.app_context():
            User.query.filter_by(email=doc_email).delete()
            User.query.filter_by(phone_number=norm_new_phone).delete()
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

        # Case A: When outbound is disabled (default), returns OUTBOUND_DISABLED
        with patch.dict('os.environ', {'KAPSO_OUTBOUND_ENABLED': 'false'}):
            res_disabled = self.client.post('/doctor_send_change_phone_otp', json={
                'new_phone_number': new_phone
            })
            data_disabled = res_disabled.get_json()
            self.assertFalse(data_disabled.get('success'))
            self.assertEqual(data_disabled.get('error_code'), 'OUTBOUND_DISABLED')

        # Case B: When outbound is enabled, generates cryptographically secure OTP and hashes it
        mock_client = MagicMock()
        mock_client.config.outbound_enabled = True
        mock_client.send_text_message.return_value = {"success": True, "status": "accepted"}

        with patch('services.kapso_client.KapsoClient', return_value=mock_client):
            res = self.client.post('/doctor_send_change_phone_otp', json={
                'new_phone_number': new_phone
            })
            data = res.get_json()
            self.assertTrue(data.get('success'), f"Request OTP failed: {data.get('msg')}")
            self.assertEqual(data.get('phone'), norm_new_phone)

        with app.app_context():
            otp_key = f"doctor_phone_change_{doc_email.lower()}"
            otp_rec = OTP.query.filter_by(email=otp_key).first()
            self.assertIsNotNone(otp_rec)
            # Must be a 64-char SHA-256 hex digest, never raw 6 digits
            self.assertEqual(len(otp_rec.otp), 64)
        print("  [PASS] 2. Logged-in doctor requests change phone OTP via WhatsApp (SHA-256 secured)")

    def test_03_doctor_verify_change_phone_otp_updates_profile(self):
        """3. Doctor verifies WhatsApp OTP: updates DoctorSession, User, phone_verified via database"""
        doc_email = "doctest_change@primecare.com"
        norm_new_phone = "+919847333444"
        raw_otp = "842915"
        hashed_otp = hash_otp(raw_otp)

        with app.app_context():
            otp_key = f"doctor_phone_change_{doc_email.lower()}"
            otp_rec = OTP.query.filter_by(email=otp_key).first()
            if not otp_rec:
                from datetime import datetime, timedelta
                otp_rec = OTP(email=otp_key, otp=hashed_otp, expiry=datetime.utcnow() + timedelta(minutes=10))
                db.session.add(otp_rec)
            else:
                otp_rec.otp = hashed_otp
            db.session.commit()

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
        res_correct = self.client.post('/doctor_verify_change_phone_otp', json={'otp': raw_otp})
        data_correct = res_correct.get_json()
        self.assertTrue(data_correct.get('success'), f"Verify OTP failed: {data_correct.get('msg')}")
        self.assertEqual(data_correct.get('new_phone'), norm_new_phone)

        with app.app_context():
            doc = DoctorSession.query.filter_by(email=doc_email).first()
            user = User.query.filter_by(email=doc_email).first()
            self.assertEqual(doc.phone_number, norm_new_phone)
            self.assertEqual(user.phone_number, norm_new_phone)
            self.assertTrue(user.phone_verified)
            self.assertEqual(doc.phone_updated_by, doc_email)
        print("  [PASS] 3. Doctor verifies WhatsApp OTP and updates mobile number across DB")


if __name__ == '__main__':
    unittest.main()
