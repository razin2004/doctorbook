"""
tests/test_whatsapp_ai_service.py
=================================
Unit Test Suite for WhatsApp AI Response Service (Phase 3D).

All tests are 100% mocked; zero live Groq or Gemini API calls.
Validates:
  13. Patient instructions selected correctly.
  14. Guest instructions selected correctly.
  15. Doctor instructions selected correctly.
  16. Admin instructions selected correctly.
  17. Invalid role rejected fail-closed.
  18. Conversation history bounded.
  19. Conversation history ordering preserved.
  20. Sensitive information excluded from prompt context.
  21. Another sender's conversation history is never included (cross-sender isolation).
  22. Emergency handling bypasses Groq deterministically.
  23. Clinic context is sanitized.
  24. Gemini integration remains completely independent.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from services.groq_client import GroqClient, GroqConfig, GroqCompletionResponse
from services.whatsapp_ai_service import (
    VALID_ROLES,
    validate_role,
    detect_medical_emergency,
    get_emergency_guidance_response,
    sanitize_clinic_context,
    build_sanitized_whatsapp_clinic_context,
    get_system_prompt_for_role,
    format_conversation_history,
    generate_whatsapp_ai_response,
    CLINIC_PHONE,
    CLINIC_LOCATION
)


class WhatsAppAIServiceTestSuite(unittest.TestCase):
    """Automated tests for WhatsApp AI response service."""

    def setUp(self):
        self.mock_client = MagicMock(spec=GroqClient)
        # Default mock response for standard AI generation
        mock_resp = GroqCompletionResponse(
            content="Welcome to PrimeCare Clinic! Dr. Sarah consults today at 10 AM.",
            model="openai/gpt-oss-120b",
            finish_reason="stop"
        )
        self.mock_client.generate_chat_completion.return_value = mock_resp

    # -----------------------------------------------------------------
    # Test 13: Patient instructions selected correctly
    # -----------------------------------------------------------------
    def test_13_patient_instructions_selected_correctly(self):
        """13. Patient instructions selected correctly in system prompt."""
        prompt = get_system_prompt_for_role("patient", sender_display_name="Rahul")
        self.assertIn("ROLE: Patient & General Enquiries Assistant", prompt)
        self.assertIn("Rahul", prompt)
        self.assertIn("Malayalam", prompt)
        self.assertIn("NO website registration and NO OTP are needed", prompt)
        self.assertIn("STRICT CLINICAL SAFETY RULES", prompt)

        # Verify through end-to-end service call
        resp = generate_whatsapp_ai_response(
            sender_role="patient",
            user_message="When is general medicine open?",
            client=self.mock_client
        )
        self.mock_client.generate_chat_completion.assert_called_once()
        messages = self.mock_client.generate_chat_completion.call_args[1]["messages"]
        system_msg = messages[0]["content"]
        self.assertIn("ROLE: Patient & General Enquiries Assistant", system_msg)
        self.assertIn("Welcome to PrimeCare Clinic!", resp)

    # -----------------------------------------------------------------
    # Test 14: Guest instructions selected correctly
    # -----------------------------------------------------------------
    def test_14_guest_instructions_selected_correctly(self):
        """14. Guest instructions selected correctly in system prompt."""
        prompt = get_system_prompt_for_role("guest")
        self.assertIn("ROLE: Patient & General Enquiries Assistant", prompt)
        self.assertIn("Patient / Guest", prompt)
        self.assertIn("Explain how to access clinic services", prompt)
        self.assertIn("NO website registration and NO OTP are needed", prompt)

    # -----------------------------------------------------------------
    # Test 15: Doctor instructions selected correctly
    # -----------------------------------------------------------------
    def test_15_doctor_instructions_selected_correctly(self):
        """15. Doctor instructions selected correctly with isolation and portal guidance."""
        prompt = get_system_prompt_for_role("doctor", sender_display_name="Dr. Matthew")
        self.assertIn("ROLE: Doctor Portal Assistant (Authenticated Doctor: Dr. Matthew)", prompt)
        self.assertIn("STRICT ISOLATION: Never disclose another doctor's schedule", prompt)
        self.assertIn("Doctor Web Portal", prompt)
        self.assertNotIn("ROLE: Patient & General Enquiries Assistant", prompt)

    # -----------------------------------------------------------------
    # Test 16: Admin instructions selected correctly
    # -----------------------------------------------------------------
    def test_16_admin_instructions_selected_correctly(self):
        """16. Admin instructions selected correctly with dashboard guidance."""
        prompt = get_system_prompt_for_role("admin", sender_display_name="Admin Anita")
        self.assertIn("ROLE: Clinic Administration Assistant (Authenticated Admin: Admin Anita)", prompt)
        self.assertIn("Admin Dashboard", prompt)
        self.assertNotIn("ROLE: Patient & General Enquiries Assistant", prompt)

    # -----------------------------------------------------------------
    # Test 17: Invalid role rejected
    # -----------------------------------------------------------------
    def test_17_invalid_role_rejected(self):
        """17. Invalid role rejected fail-closed with ValueError."""
        invalid_roles = ["superadmin", "unknown", "anonymous", "hacker", "root", "", None, 123]
        for role in invalid_roles:
            with self.assertRaises(ValueError) as ctx:
                validate_role(role)
            self.assertIn("Invalid sender role", str(ctx.exception))

            with self.assertRaises(ValueError):
                generate_whatsapp_ai_response(
                    sender_role=role,
                    user_message="Hello",
                    client=self.mock_client
                )
        self.mock_client.generate_chat_completion.assert_not_called()

    # -----------------------------------------------------------------
    # Test 18: Conversation history bounded
    # -----------------------------------------------------------------
    def test_18_conversation_history_bounded(self):
        """18. Conversation history bounded to recent turns (default: 10)."""
        long_history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"Turn message {i}"}
            for i in range(25)
        ]
        bounded = format_conversation_history(long_history, max_turns=10)
        self.assertEqual(len(bounded), 10)
        # Most recent turn is Turn 24
        self.assertEqual(bounded[-1]["content"], "Turn message 24")
        # Oldest bounded turn is Turn 15
        self.assertEqual(bounded[0]["content"], "Turn message 15")

        # Verify through generate_whatsapp_ai_response
        generate_whatsapp_ai_response(
            sender_role="patient",
            user_message="Latest question",
            conversation_history=long_history,
            client=self.mock_client
        )
        call_messages = self.mock_client.generate_chat_completion.call_args[1]["messages"]
        # 1 system + 10 bounded history + 1 current user message = 12 messages
        self.assertEqual(len(call_messages), 12)
        self.assertEqual(call_messages[-1]["content"], "Latest question")

    # -----------------------------------------------------------------
    # Test 19: Conversation history ordering preserved
    # -----------------------------------------------------------------
    def test_19_conversation_history_ordering_preserved(self):
        """19. Conversation history ordering strictly preserved chronologically."""
        ordered_history = [
            {"role": "user", "content": "Question 1: Timings?"},
            {"role": "assistant", "content": "Answer 1: 9 AM to 5 PM."},
            {"role": "user", "content": "Question 2: Dr. Sarah available?"},
            {"role": "assistant", "content": "Answer 2: Yes, on Wednesday."}
        ]
        formatted = format_conversation_history(ordered_history)
        self.assertEqual(len(formatted), 4)
        for i in range(4):
            self.assertEqual(formatted[i]["content"], ordered_history[i]["content"])
            self.assertEqual(formatted[i]["role"], ordered_history[i]["role"])

    # -----------------------------------------------------------------
    # Test 20: Sensitive information excluded from prompt context
    # -----------------------------------------------------------------
    def test_20_sensitive_information_excluded_from_prompt_context(self):
        """20. Passwords, OTPs, session keys, and secrets excluded from prompt context."""
        dirty_history = [
            {"role": "user", "content": "My password is Password123! and otp is 982143"},
            {"role": "assistant", "content": "Your session token is token=abc987secret"}
        ]
        formatted = format_conversation_history(dirty_history)
        for msg in formatted:
            self.assertNotIn("Password123!", msg["content"])
            self.assertNotIn("982143", msg["content"])
            self.assertIn("[REDACTED]", msg["content"])

        dirty_context = (
            "=== CLINIC DIRECTORY ===\n"
            "Doctor: Dr. John | Specialization: General Medicine\n"
            "Admin password hash: pbkdf2:sha256:secretpasswordhash\n"
            "OTP Secret Key: JBSWY3DPEHPK3PXP\n"
            "API_KEY = gsk_private_key_abc123\n"
            "Clinic Phone: +91 8592031725\n"
        )
        sanitized = sanitize_clinic_context(dirty_context, role="guest")
        self.assertNotIn("secretpasswordhash", sanitized)
        self.assertNotIn("JBSWY3DPEHPK3PXP", sanitized)
        self.assertNotIn("gsk_private_key_abc123", sanitized)
        self.assertIn("Dr. John", sanitized)
        self.assertIn("+91 8592031725", sanitized)

    # -----------------------------------------------------------------
    # Test 21: Another sender's conversation history is never included
    # -----------------------------------------------------------------
    def test_21_cross_sender_conversation_isolation(self):
        """21. Another sender's conversation history is strictly excluded (cross-sender isolation)."""
        mixed_history = [
            {"sender_phone": "+918888888888", "role": "user", "content": "Rahul's message"},
            {"sender_phone": "+919999999999", "role": "user", "content": "Attacker's leaked message"},
            {"sender_phone": "+918888888888", "role": "assistant", "content": "Reply to Rahul"}
        ]
        # Current sender is Rahul (+918888888888)
        isolated = format_conversation_history(mixed_history, current_sender_phone="+918888888888")
        self.assertEqual(len(isolated), 2)
        contents = [m["content"] for m in isolated]
        self.assertIn("Rahul's message", contents)
        self.assertIn("Reply to Rahul", contents)
        self.assertNotIn("Attacker's leaked message", contents)

    # -----------------------------------------------------------------
    # Test 22: Emergency handling bypasses Groq
    # -----------------------------------------------------------------
    def test_22_emergency_handling_bypasses_groq(self):
        """22. Emergency messages deterministically bypass Groq and return verified guidance."""
        emergency_queries = [
            "My father has severe chest pain and cannot breathe",
            "Patient is unconscious and bleeding heavily",
            "nenjuvedhana undu, swasam muttunu",
            "Profuse bleeding from wound after accident",
            "Sudden seizure and unresponsive"
        ]

        for query in emergency_queries:
            resp = generate_whatsapp_ai_response(
                sender_role="patient",
                user_message=query,
                client=self.mock_client
            )
            # Deterministic requirement: Groq is NEVER called
            self.mock_client.generate_chat_completion.assert_not_called()
            self.assertIn("EMERGENCY MEDICAL NOTICE", resp)
            self.assertIn("112", resp)
            self.assertIn("108", resp)
            self.assertNotIn("Emergency Desk", resp)
            self.assertNotIn("emergency desk", resp.lower())
            self.assertIn("Do not wait for a WhatsApp reply", resp)

    # -----------------------------------------------------------------
    # Test 23: Clinic context is sanitized
    # -----------------------------------------------------------------
    def test_23_clinic_context_is_sanitized(self):
        """23. Clinic context strips private patient bookings and administrative secrets."""
        raw_context_with_bookings = (
            "--- SYSTEM TIME & STATUS ---\n"
            "Current Time: Monday, November 04, 2026, 12:00:00 IST\n"
            "=== USER'S PERSONAL BOOKINGS ===\n"
            "  Total upcoming bookings: 1\n"
            "  - NEXT UPCOMING: Doctor John Doe on 2026-11-05 at 10:00 (Token 4, Patient: Jane Doe, Phone: +919123456789)\n"
            "\n"
            "=== CLINIC DOCTOR DIRECTORY ===\n"
            "  Doctor: Dr. Sarah | Specialization: Pediatrics\n"
            "--- Clinic Contact Details ---\n"
            "  Phone / WhatsApp: +91 8592031725\n"
        )

        sanitized = sanitize_clinic_context(raw_context_with_bookings, role="guest")
        self.assertNotIn("USER'S PERSONAL BOOKINGS", sanitized)
        self.assertNotIn("Jane Doe", sanitized)
        self.assertNotIn("+919123456789", sanitized)
        self.assertNotIn("Token 4", sanitized)
        self.assertIn("Dr. Sarah", sanitized)
        self.assertIn("+91 8592031725", sanitized)

    # -----------------------------------------------------------------
    # Test 24: Gemini integration remains independent
    # -----------------------------------------------------------------
    def test_24_gemini_integration_remains_independent(self):
        """24. WhatsApp AI operates independently without importing or mutating Gemini."""
        import services.whatsapp_ai_service as was
        import services.groq_client as gc

        # Verify no google.genai or GOOGLE_API_KEY references in whatsapp_ai_service module
        with open(was.__file__, "r", encoding="utf-8") as f:
            was_source = f.read()
        with open(gc.__file__, "r", encoding="utf-8") as f:
            gc_source = f.read()

        self.assertNotIn("google.genai", was_source)
        self.assertNotIn("GOOGLE_API_KEY", was_source)
        self.assertNotIn("google.genai", gc_source)
        self.assertNotIn("GOOGLE_API_KEY", gc_source)

        # Service functions when GOOGLE_API_KEY is not set
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test_key_123"}, clear=False):
            resp = generate_whatsapp_ai_response(
                sender_role="patient",
                user_message="What are your clinic hours?",
                client=self.mock_client
            )
            self.assertIn("Welcome to PrimeCare Clinic!", resp)


if __name__ == "__main__":
    unittest.main()
