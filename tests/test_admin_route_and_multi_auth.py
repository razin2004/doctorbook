import unittest
import os
import json
from unittest.mock import patch
from werkzeug.security import generate_password_hash

import tests.test_isolation
from app import app, db, User, AppSettings, AdminOTPChallenge, ADMIN_EMAIL, get_admin_phone, is_valid_admin_identifier, is_password_login_enabled


class TestAdminRouteAndMultiAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_isolation.initialize_test_database(app, db)
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.session.rollback()
        AppSettings.query.delete()
        AdminOTPChallenge.query.delete()
        User.query.delete()
        db.session.commit()
        self.client = app.test_client()

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def test_01_admin_route_renders_correctly(self):
        """Test GET /admin renders admin portal cleanly when unauthenticated and authenticated."""
        # Unauthenticated:
        res = self.client.get('/admin')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Admin Portal", res.data)
        # Verify patient sections are not rendered in admin view
        self.assertNotIn(b'id="panelDoctor"', res.data)
        self.assertNotIn(b'id="panelDept"', res.data)

        # Authenticated:
        with self.client.session_transaction() as sess:
            sess['admin_logged_in'] = True
            sess['admin_email'] = ADMIN_EMAIL
        res_auth = self.client.get('/admin?panel=roster')
        self.assertEqual(res_auth.status_code, 200)
        self.assertIn(b"PrimeCare Clinic", res_auth.data)

    def test_02_booking_view_admin_redirects_to_admin(self):
        """Test /booking?view=admin redirects (302) to /admin preserving panel parameter."""
        res = self.client.get('/booking?view=admin')
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.headers.get('Location').endswith('/admin'))

        res_panel = self.client.get('/booking?view=admin&panel=welcome')
        self.assertEqual(res_panel.status_code, 302)
        self.assertIn('/admin?panel=welcome', res_panel.headers.get('Location'))

    def test_03_admin_password_login_success_and_disabled_policy(self):
        """Test /admin_password_login with email and WhatsApp phone, and enforcement of disabled setting."""
        # 1. Set admin password
        admin_pass = "ClinicAdminSecret123!"
        pwd_setting = AppSettings(key='admin_password_hash', value=generate_password_hash(admin_pass))
        phone_setting = AppSettings(key='admin_whatsapp_phone', value='+918592031725')
        db.session.add(pwd_setting)
        db.session.add(phone_setting)
        db.session.commit()

        # Success via email
        res_email = self.client.post('/admin_password_login', json={
            'identifier': ADMIN_EMAIL,
            'password': admin_pass
        })
        self.assertEqual(res_email.status_code, 200)
        data_email = res_email.get_json()
        self.assertTrue(data_email.get('success'))
        self.assertEqual(data_email.get('redirect_url'), '/admin')

        # Success via WhatsApp phone number
        res_phone = self.client.post('/admin_password_login', json={
            'identifier': '+91 85920 31725',
            'password': admin_pass
        })
        self.assertEqual(res_phone.status_code, 200)
        self.assertTrue(res_phone.get_json().get('success'))

        # Wrong password
        res_wrong = self.client.post('/admin_password_login', json={
            'identifier': ADMIN_EMAIL,
            'password': 'WrongPassword!'
        })
        self.assertEqual(res_wrong.status_code, 401)
        self.assertFalse(res_wrong.get_json().get('success'))

        # 2. Disable password login policy
        policy_setting = AppSettings(key='password_login_enabled', value='0')
        db.session.add(policy_setting)
        db.session.commit()

        res_disabled = self.client.post('/admin_password_login', json={
            'identifier': ADMIN_EMAIL,
            'password': admin_pass
        })
        self.assertEqual(res_disabled.status_code, 403)
        self.assertFalse(res_disabled.get_json().get('success'))
        self.assertIn("disabled", res_disabled.get_json().get('msg').lower())

    def test_04_admin_otp_login_via_whatsapp_and_email(self):
        """Test /send_admin_otp via WhatsApp channel and email channel."""
        phone_setting = AppSettings(key='admin_whatsapp_phone', value='+918592031725')
        db.session.add(phone_setting)
        db.session.commit()

        # Test WhatsApp delivery
        with patch('app.send_whatsapp_message', return_value={"success": True}) as mock_wa:
            res_wa = self.client.post('/send_admin_otp', json={
                'identifier': '+918592031725',
                'channel': 'whatsapp'
            })
            self.assertEqual(res_wa.status_code, 200)
            self.assertTrue(res_wa.get_json().get('success'))
            self.assertIn("WhatsApp", res_wa.get_json().get('msg'))
            mock_wa.assert_called_once()

        # Clear challenge to reset the 60-second rate-limit cooldown for the second test call
        AdminOTPChallenge.query.delete()
        db.session.commit()

        # Test Email delivery
        with patch('app.send_email', return_value=True) as mock_email:
            res_email = self.client.post('/send_admin_otp', json={
                'identifier': ADMIN_EMAIL,
                'channel': 'email'
            })
            self.assertEqual(res_email.status_code, 200)
            self.assertTrue(res_email.get_json().get('success'))
            mock_email.assert_called_once()

    def test_05_patient_login_password_policy(self):
        """Test /patient_login respects password_login_enabled setting."""
        patient_pwd = "PatientPass123!"
        patient = User(
            name="Test Patient",
            phone_number="+919876543210",
            email="patient@example.com",
            password_hash=generate_password_hash(patient_pwd),
            role="patient",
            phone_verified=True
        )
        db.session.add(patient)
        db.session.commit()

        # Enabled by default: login via phone
        res_phone = self.client.post('/patient_login', json={
            'phone_number': '+919876543210',
            'password': patient_pwd
        })
        self.assertEqual(res_phone.status_code, 200)
        self.assertTrue(res_phone.get_json().get('success'))

        # Login via email
        res_email = self.client.post('/patient_login', json={
            'identifier': 'patient@example.com',
            'password': patient_pwd
        })
        self.assertEqual(res_email.status_code, 200)
        self.assertTrue(res_email.get_json().get('success'))

        # Disable password login policy
        policy = AppSettings(key='password_login_enabled', value='0')
        db.session.add(policy)
        db.session.commit()

        res_blocked = self.client.post('/patient_login', json={
            'phone_number': '+919876543210',
            'password': patient_pwd
        })
        self.assertFalse(res_blocked.get_json().get('success'))
        self.assertIn("disabled", res_blocked.get_json().get('msg').lower())


if __name__ == '__main__':
    unittest.main()
