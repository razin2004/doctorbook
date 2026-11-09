import os
import sys
import unittest
from dotenv import load_dotenv

load_dotenv()
from app import app, ADMIN_EMAIL

class TestWebAIRestoration(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_01_home_page_ai_elements(self):
        """Verify home.html contains all restored patient AI elements."""
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="aiChatFab"', html)
        self.assertIn('id="aiChatOverlay"', html)
        self.assertIn('id="aiChatPanel"', html)
        self.assertIn('id="aiChatBody"', html)
        self.assertIn('id="aiChatInput"', html)
        self.assertIn('id="aiChatSend"', html)
        self.assertIn('toggleAiChat', html)
        self.assertIn('/ai_triage', html)
        self.assertIn('Chest pain', html)

    def test_02_booking_page_admin_ai_elements(self):
        """Verify booking.html contains all restored admin AI elements."""
        resp = self.client.get('/booking')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="adminAiChatFab"', html)
        self.assertIn('id="adminAiChatOverlay"', html)
        self.assertIn('id="adminAiChatPanel"', html)
        self.assertIn('id="adminAiChatMessages"', html)
        self.assertIn('id="adminAiChatInput"', html)
        self.assertIn('id="adminAiChatSend"', html)
        self.assertIn('toggleAdminAiChat', html)
        self.assertIn('/admin_ai_chat', html)

    def test_03_ai_triage_empty_history(self):
        """Verify /ai_triage returns greeting when history is empty."""
        resp = self.client.post('/ai_triage', json={"history": []})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn('PrimeCare AI Assistant', data.get('reply', ''))

    def test_04_ai_triage_symptom_inquiry(self):
        """Verify /ai_triage returns intelligent response using active LLM (Gemini or Groq)."""
        payload = {
            "history": [
                {"role": "user", "text": "I have severe chest pain and breathlessness. Who should I see?"}
            ]
        }
        resp = self.client.post('/ai_triage', json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get('success'), f"Failed response: {data}")
        reply = data.get('reply', '')
        self.assertTrue(len(reply) > 20)

    def test_05_ai_triage_clinic_query(self):
        """Verify /ai_triage answers clinic operational queries."""
        payload = {
            "history": [
                {"role": "user", "text": "Who is available on Monday and what are the clinic hours?"}
            ]
        }
        resp = self.client.post('/ai_triage', json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get('success'), f"Failed response: {data}")
        reply = data.get('reply', '')
        self.assertTrue(len(reply) > 20)

    def test_06_admin_ai_chat_unauthorized(self):
        """Verify /admin_ai_chat rejects unauthorized requests."""
        resp = self.client.post('/admin_ai_chat', json={"message": "What are our stats?"})
        self.assertEqual(resp.status_code, 403)
        data = resp.get_json()
        self.assertFalse(data.get('success'))

    def test_07_admin_ai_chat_authorized(self):
        """Verify /admin_ai_chat processes queries when authenticated as admin."""
        with self.client.session_transaction() as sess:
            sess['admin_logged_in'] = True
            sess['admin_email'] = ADMIN_EMAIL

        payload = {
            "message": "Give me a brief summary of our system status and doctors.",
            "history": []
        }
        resp = self.client.post('/admin_ai_chat', json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get('success'), f"Failed admin chat: {data}")
        reply = data.get('reply', '')
        self.assertTrue(len(reply) > 20)

if __name__ == '__main__':
    unittest.main()

