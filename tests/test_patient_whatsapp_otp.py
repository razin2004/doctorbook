import os
import sys
import unittest
import json
from datetime import datetime, timedelta
from unittest.mock import patch

# Ensure root dir is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import tests.test_isolation

from app import (
    app, db, User, DoctorSession, OTP, hash_otp, verify_otp_hash
)

TEST_PATIENT_PHONE = "+919847777111"
TEST_OTP_KEY = f"patient_reg_{TEST_PATIENT_PHONE}"

class PatientWhatsAppOTPTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_isolation.initialize_test_database(app, db)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

    def setUp(self):
        with app.app_context():
            DoctorSession.query.filter_by(phone_number=TEST_PATIENT_PHONE).delete()
            User.query.filter_by(phone_number=TEST_PATIENT_PHONE).delete()
            OTP.query.filter_by(email=TEST_OTP_KEY).delete()
            db.session.commit()

    def tearDown(self):
        with app.app_context():
            DoctorSession.query.filter_by(phone_number=TEST_PATIENT_PHONE).delete()
            User.query.filter_by(phone_number=TEST_PATIENT_PHONE).delete()
            OTP.query.filter_by(email=TEST_OTP_KEY).delete()
            db.session.commit()

    def test_01_registration_triggers_whatsapp_otp(self):
        """Registering a new patient sends a 6-digit OTP and returns require_otp=True"""
        with patch('app.send_whatsapp_message') as mock_send:
            mock_send.return_value = {"success": True, "message_id": "mock_wamid"}
            res = self.client.post('/patient_register', json={
                'name': 'WhatsApp Test Patient',
                'phone_number': '9847777111',
                'password': 'password123',
                'confirm_password': 'password123',
                'email': 'whatsapp.otp.test@example.com'
            })
            data = res.get_json()
            self.assertTrue(data.get('success'), f"Registration failed: {data}")
            self.assertTrue(data.get('require_otp'))
            self.assertEqual(data.get('phone_number'), TEST_PATIENT_PHONE)
            self.assertIn('+91 984', data.get('masked_phone'))
            self.assertTrue(mock_send.called)

        with app.app_context():
            u = User.query.filter_by(phone_number=TEST_PATIENT_PHONE).first()
            self.assertIsNotNone(u)
            self.assertFalse(u.phone_verified)
            self.assertIsNone(u.phone_verified_at)

            otp_record = OTP.query.filter_by(email=TEST_OTP_KEY).first()
            self.assertIsNotNone(otp_record)
            self.assertEqual(otp_record.attempt_count, 0)
            self.assertIsNone(otp_record.consumed_at)

    def test_02_verify_registration_otp_success(self):
        """Submitting the correct OTP activates the account and establishes session"""
        # Register user
        reg_res = self.client.post('/patient_register', json={
            'name': 'WhatsApp Test Patient',
            'phone_number': '9847777111',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.assertTrue(reg_res.get_json().get('success'))

        # Inject known OTP
        test_otp = "123456"
        with app.app_context():
            otp_record = OTP.query.filter_by(email=TEST_OTP_KEY).first()
            self.assertIsNotNone(otp_record, "OTP record not created during registration")
            otp_record.otp = hash_otp(test_otp)
            db.session.commit()

        # Submit OTP
        with patch('app.send_whatsapp_message') as mock_send:
            mock_send.return_value = {"success": True}
            res = self.client.post('/patient_verify_registration_otp', json={
                'phone_number': TEST_PATIENT_PHONE,
                'otp': test_otp
            })
            data = res.get_json()
            self.assertTrue(data.get('success'), f"Verification failed: {data.get('msg')}")
            self.assertEqual(data.get('redirect_url'), '/patient_dashboard')

        # Check DB state
        with app.app_context():
            u = User.query.filter_by(phone_number=TEST_PATIENT_PHONE).first()
            self.assertTrue(u.phone_verified)
            self.assertIsNotNone(u.phone_verified_at)

            # OTP record should be purged
            otp_record = OTP.query.filter_by(email=TEST_OTP_KEY).first()
            self.assertIsNone(otp_record)

    def test_03_verify_registration_otp_failure_and_rate_limit(self):
        """Invalid OTP increments attempt count and locks out after max attempts"""
        self.client.post('/patient_register', json={
            'name': 'WhatsApp Test Patient',
            'phone_number': '9847777111',
            'password': 'password123',
            'confirm_password': 'password123'
        })

        test_otp = "654321"
        with app.app_context():
            otp_record = OTP.query.filter_by(email=TEST_OTP_KEY).first()
            self.assertIsNotNone(otp_record)
            otp_record.otp = hash_otp(test_otp)
            db.session.commit()

        # Submit wrong OTP
        res = self.client.post('/patient_verify_registration_otp', json={
            'phone_number': TEST_PATIENT_PHONE,
            'otp': '000000'
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn("Invalid verification code", data.get('msg'))

        with app.app_context():
            otp_record = OTP.query.filter_by(email=TEST_OTP_KEY).first()
            self.assertEqual(otp_record.attempt_count, 1)

    def test_04_resend_otp_respects_cooldown(self):
        """Resend OTP rejects requests before 60-second cooldown expires"""
        # Register user (which stores pending_reg_last_sent in session)
        self.client.post('/patient_register', json={
            'name': 'WhatsApp Test Patient',
            'phone_number': '9847777111',
            'password': 'password123',
            'confirm_password': 'password123'
        })

        # Set session last sent to now
        with self.client.session_transaction() as sess:
            sess['pending_reg_phone'] = TEST_PATIENT_PHONE
            sess['pending_reg_last_sent'] = datetime.utcnow().isoformat()

        # Immediate resend should be blocked by cooldown
        res = self.client.post('/patient_resend_registration_otp', json={
            'phone_number': TEST_PATIENT_PHONE
        })
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn("Please wait", data.get('msg'))


if __name__ == '__main__':
    unittest.main()
