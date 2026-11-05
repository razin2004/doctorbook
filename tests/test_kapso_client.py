"""
tests/test_kapso_client.py
==========================
Comprehensive Unit & Integration Test Suite for Kapso WhatsApp Business API Client (Phase 2C).

Verifies official Kapso specifications:
  - Header: X-API-Key authentication (Meta Cloud / Kapso Gateway standard).
  - Messaging endpoint: POST /meta/whatsapp/{api_version}/{phone_number_id}/messages (default v24.0).
  - Phone health endpoint: GET /platform/v1/whatsapp/phone_numbers/{phone_number_id}/health.
  - HMAC-SHA256 signature verification with raw request bytes and constant-time comparison.
  - Separate verify token (KAPSO_WEBHOOK_VERIFY_TOKEN) and webhook secret (KAPSO_WEBHOOK_SECRET).
  - Health check caching with 180-second (3-minute) TTL.
  - Fail-closed outbound feature flag (KAPSO_OUTBOUND_ENABLED=false).
  - Sanitized logging and zero credential leakage.
"""

import os
import hmac
import hashlib
import time
import unittest
from unittest.mock import patch, MagicMock
import requests

from services.kapso_client import KapsoConfig, KapsoClient, sanitize_log_message


class TestKapsoConfig(unittest.TestCase):
    """Tests for KapsoConfig configuration model and environment loading."""

    def test_default_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = KapsoConfig.from_env()
            self.assertFalse(cfg.is_configured())
            self.assertEqual(cfg.api_key, "")
            self.assertEqual(cfg.phone_number_id, "")
            self.assertEqual(cfg.base_url, "https://api.kapso.ai")
            self.assertEqual(cfg.api_version, "v24.0")
            self.assertEqual(cfg.webhook_verify_token, "")
            self.assertEqual(cfg.webhook_secret, "")
            self.assertFalse(cfg.outbound_enabled)

    def test_configured_from_init(self):
        cfg = KapsoConfig(
            api_key="test_api_key_123",
            phone_number_id="10987654321",
            base_url="https://api.kapso.ai/",
            api_version="v24.0",
            webhook_verify_token="verify_token_xyz",
            webhook_secret="secret_hmac_key",
            outbound_enabled=True,
            timeout=15
        )
        self.assertTrue(cfg.is_configured())
        self.assertEqual(cfg.api_key, "test_api_key_123")
        self.assertEqual(cfg.phone_number_id, "10987654321")
        self.assertEqual(cfg.base_url, "https://api.kapso.ai")
        self.assertEqual(cfg.api_version, "v24.0")
        self.assertEqual(cfg.webhook_verify_token, "verify_token_xyz")
        self.assertEqual(cfg.webhook_secret, "secret_hmac_key")
        self.assertTrue(cfg.outbound_enabled)
        self.assertEqual(cfg.timeout, 15)

    def test_from_env_loading(self):
        test_env = {
            "KAPSO_API_KEY": "env_api_key_456",
            "KAPSO_PHONE_NUMBER_ID": "env_phone_id_789",
            "KAPSO_BASE_URL": "https://api.kapso.ai/",
            "KAPSO_API_VERSION": "v24.0",
            "KAPSO_WEBHOOK_VERIFY_TOKEN": "custom_verify_token",
            "KAPSO_WEBHOOK_SECRET": "custom_webhook_secret",
            "KAPSO_OUTBOUND_ENABLED": "true"
        }
        with patch.dict(os.environ, test_env, clear=True):
            cfg = KapsoConfig.from_env()
            self.assertTrue(cfg.is_configured())
            self.assertEqual(cfg.api_key, "env_api_key_456")
            self.assertEqual(cfg.phone_number_id, "env_phone_id_789")
            self.assertEqual(cfg.base_url, "https://api.kapso.ai")
            self.assertEqual(cfg.api_version, "v24.0")
            self.assertEqual(cfg.webhook_verify_token, "custom_verify_token")
            self.assertEqual(cfg.webhook_secret, "custom_webhook_secret")
            self.assertTrue(cfg.outbound_enabled)

    def test_validate_success(self):
        cfg = KapsoConfig(api_key="valid_key", phone_number_id="12345")
        cfg.validate()

    def test_validate_missing_api_key_raises(self):
        cfg = KapsoConfig(api_key="", phone_number_id="12345")
        with self.assertRaises(ValueError) as ctx:
            cfg.validate()
        self.assertIn("KAPSO_API_KEY", str(ctx.exception))

    def test_validate_missing_phone_number_id_raises(self):
        cfg = KapsoConfig(api_key="valid_key", phone_number_id="")
        with self.assertRaises(ValueError) as ctx:
            cfg.validate()
        self.assertIn("KAPSO_PHONE_NUMBER_ID", str(ctx.exception))


class TestKapsoClientHealthCheck(unittest.TestCase):
    """Tests for KapsoClient.health_check() endpoint, headers, and 180-second (3-minute) caching."""

    def setUp(self):
        self.config = KapsoConfig(
            api_key="test_api_key",
            phone_number_id="phone_num_999",
            base_url="https://api.kapso.ai",
            api_version="v24.0"
        )
        self.client = KapsoClient(self.config)

    def test_health_check_unconfigured(self):
        unconfigured_client = KapsoClient(KapsoConfig(api_key="", phone_number_id=""))
        result = unconfigured_client.health_check()
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "unconfigured")
        self.assertIsNone(result["status_code"])

    @patch("requests.get")
    def test_health_check_200_healthy_and_caching(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "AVAILABLE", "quality_rating": "GREEN"}
        mock_get.return_value = mock_response

        # First call: makes network request
        result1 = self.client.health_check()
        self.assertTrue(result1["healthy"])
        self.assertEqual(result1["status"], "healthy")
        self.assertEqual(result1["status_code"], 200)

        # Assert correct endpoint and X-API-Key header per official spec
        expected_url = "https://api.kapso.ai/platform/v1/whatsapp/phone_numbers/phone_num_999/health"
        mock_get.assert_called_once_with(
            expected_url,
            headers={
                "X-API-Key": "test_api_key",
                "Accept": "application/json"
            },
            timeout=10
        )

        # Second call within TTL: returns cached result without network request
        result2 = self.client.health_check()
        self.assertTrue(result2["healthy"])
        self.assertEqual(mock_get.call_count, 1)

        # Force refresh: bypasses cache and calls network
        result3 = self.client.health_check(force_refresh=True)
        self.assertTrue(result3["healthy"])
        self.assertEqual(mock_get.call_count, 2)

    def test_health_check_ttl_is_three_minutes(self):
        self.assertEqual(self.client._health_cache["ttl"], 180.0)

    @patch("requests.get")
    def test_health_check_missing_status_invalid_response(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"some_other_key": 123}'
        mock_response.json.return_value = {"some_other_key": 123}
        mock_get.return_value = mock_response

        result = self.client.health_check(force_refresh=True)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "invalid_response")
        self.assertEqual(result["health_status"], "UNKNOWN")

    @patch("requests.get")
    def test_health_check_empty_json_invalid_response(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = ""
        mock_response.json.side_effect = ValueError("No JSON")
        mock_get.return_value = mock_response

        result = self.client.health_check(force_refresh=True)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "invalid_response")
        self.assertEqual(result["health_status"], "UNKNOWN")

    @patch("requests.get")
    def test_health_check_degraded_status(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"status": "degraded"}'
        mock_response.json.return_value = {"status": "degraded"}
        mock_get.return_value = mock_response

        result = self.client.health_check(force_refresh=True)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["health_status"], "DEGRADED")

    @patch("requests.get")
    def test_health_check_unhealthy_status(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"status": "unhealthy"}'
        mock_response.json.return_value = {"status": "unhealthy"}
        mock_get.return_value = mock_response

        result = self.client.health_check(force_refresh=True)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["health_status"], "UNHEALTHY")

    @patch("requests.get")
    def test_health_check_error_status(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"status": "error"}'
        mock_response.json.return_value = {"status": "error"}
        mock_get.return_value = mock_response

        result = self.client.health_check(force_refresh=True)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["health_status"], "ERROR")

    @patch("requests.get")
    def test_health_check_unknown_status(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"status": "unrecognized_val"}'
        mock_response.json.return_value = {"status": "unrecognized_val"}
        mock_get.return_value = mock_response

        result = self.client.health_check(force_refresh=True)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["health_status"], "UNRECOGNIZED_VAL")

    @patch("requests.get")
    def test_health_check_preserves_timestamp_checks_retry_after(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"status": "healthy", "timestamp": "2026-11-04T00:00:00Z", "checks": {"gateway": "up"}, "retry_after": 0}'
        mock_response.json.return_value = {
            "status": "healthy",
            "timestamp": "2026-11-04T00:00:00Z",
            "checks": {"gateway": "up"},
            "retry_after": 0
        }
        mock_get.return_value = mock_response

        result = self.client.health_check(force_refresh=True)
        self.assertTrue(result["healthy"])
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["timestamp"], "2026-11-04T00:00:00Z")
        self.assertEqual(result["checks"], {"gateway": "up"})
        self.assertEqual(result["retry_after"], 0)

    @patch("requests.get")
    def test_health_check_429_retry_after_honored_and_avoids_repeated_requests(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "45"}
        mock_get.return_value = mock_response

        result1 = self.client.health_check(force_refresh=True)
        self.assertFalse(result1["healthy"])
        self.assertEqual(result1["status"], "rate_limited")
        self.assertEqual(result1["retry_after"], 45)
        self.assertEqual(mock_get.call_count, 1)

        # Subsequent call before retry-after expires should not trigger network request
        result2 = self.client.health_check()
        self.assertFalse(result2["healthy"])
        self.assertEqual(result2["status"], "rate_limited")
        self.assertEqual(mock_get.call_count, 1)

    @patch("requests.get")
    def test_health_check_401_unauthorized(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized API key"
        mock_get.return_value = mock_response

        result = self.client.health_check(force_refresh=True)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(result["status_code"], 401)

    @patch("requests.get", side_effect=requests.exceptions.Timeout("Connection timed out"))
    def test_health_check_timeout(self, mock_get):
        result = self.client.health_check(force_refresh=True)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "timeout")
        self.assertIsNone(result["status_code"])

    @patch("requests.get", side_effect=requests.exceptions.ConnectionError("DNS failed"))
    def test_health_check_network_error(self, mock_get):
        result = self.client.health_check(force_refresh=True)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], "connection_error")


class TestKapsoClientWebhookSecurity(unittest.TestCase):
    """Tests for HMAC-SHA256 raw bytes verification and handshake verification."""

    def setUp(self):
        self.secret = "webhook_secret_key_123"
        self.verify_token = "verify_token_456"
        self.config = KapsoConfig(
            api_key="api_key",
            phone_number_id="phone_id",
            webhook_secret=self.secret,
            webhook_verify_token=self.verify_token
        )
        self.client = KapsoClient(self.config)

    def test_webhook_signature_valid(self):
        raw_body = b'{"entry":[{"id":"123","changes":[{"value":{"messages":[{"from":"919847012345"}]}}]}]}'
        expected_sig = "sha256=" + hmac.new(self.secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()

        # Both with prefix and raw hex
        self.assertTrue(self.client.verify_webhook_signature(raw_body, expected_sig))
        self.assertTrue(self.client.verify_webhook_signature(raw_body, expected_sig.replace("sha256=", "")))

    def test_webhook_signature_tampered_payload_rejected(self):
        raw_body = b'{"entry":[{"id":"123"}]}'
        expected_sig = "sha256=" + hmac.new(self.secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()

        tampered_body = b'{"entry":[{"id":"123","tampered":true}]}'
        self.assertFalse(self.client.verify_webhook_signature(tampered_body, expected_sig))

    def test_webhook_signature_wrong_secret_rejected(self):
        raw_body = b'{"test":"data"}'
        wrong_sig = "sha256=" + hmac.new(b"wrong_secret", raw_body, hashlib.sha256).hexdigest()
        self.assertFalse(self.client.verify_webhook_signature(raw_body, wrong_sig))

    def test_webhook_signature_missing_header_rejected(self):
        self.assertFalse(self.client.verify_webhook_signature(b'{}', None))
        self.assertFalse(self.client.verify_webhook_signature(b'{}', ""))

    def test_handshake_valid(self):
        verified, challenge = self.client.verify_webhook_handshake(
            mode="subscribe",
            verify_token="verify_token_456",
            challenge="11223344"
        )
        self.assertTrue(verified)
        self.assertEqual(challenge, "11223344")

    def test_handshake_invalid_token(self):
        verified, challenge = self.client.verify_webhook_handshake(
            mode="subscribe",
            verify_token="attacker_token",
            challenge="11223344"
        )
        self.assertFalse(verified)
        self.assertIsNone(challenge)

    def test_handshake_invalid_mode(self):
        verified, challenge = self.client.verify_webhook_handshake(
            mode="unsubscribe",
            verify_token="verify_token_456",
            challenge="11223344"
        )
        self.assertFalse(verified)
        self.assertIsNone(challenge)


class TestKapsoClientOutboundControl(unittest.TestCase):
    """Tests for fail-closed outbound sending and X-API-Key messaging endpoints."""

    def test_outbound_disabled_by_default(self):
        # Default config has outbound_enabled=False
        with patch.dict(os.environ, {"KAPSO_OUTBOUND_ENABLED": "false"}):
            client = KapsoClient(KapsoConfig(api_key="valid_key", phone_number_id="100200300", outbound_enabled=False))
            res_text = client.send_text_message("+919847012345", "Test Message")
            self.assertFalse(res_text["success"])
            self.assertEqual(res_text["status"], "outbound_disabled")

            res_tpl = client.send_template_message("+919847012345", "appointment_confirmation")
            self.assertFalse(res_tpl["success"])
            self.assertEqual(res_tpl["status"], "outbound_disabled")

    @patch("requests.post")
    def test_send_text_message_when_outbound_enabled(self, mock_post):
        client = KapsoClient(KapsoConfig(
            api_key="valid_api_key",
            phone_number_id="100200300",
            api_version="v24.0",
            outbound_enabled=True
        ))
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"messages": [{"id": "wamid.TXT123"}]}'
        mock_response.json.return_value = {"messages": [{"id": "wamid.TXT123"}]}
        mock_post.return_value = mock_response

        res = client.send_text_message("9847012345", "Hello Patient")
        self.assertTrue(res["success"])
        self.assertEqual(res["message_id"], "wamid.TXT123")
        self.assertEqual(res["status"], "accepted")

        # Assert correct official endpoint and X-API-Key header
        expected_url = "https://api.kapso.ai/meta/whatsapp/v24.0/100200300/messages"
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], expected_url)
        self.assertEqual(kwargs["headers"]["X-API-Key"], "valid_api_key")
        self.assertEqual(kwargs["json"]["messaging_product"], "whatsapp")
        self.assertEqual(kwargs["json"]["to"], "919847012345")
        self.assertEqual(kwargs["json"]["text"]["body"], "Hello Patient")

    @patch("requests.post")
    def test_send_template_message_when_outbound_enabled(self, mock_post):
        client = KapsoClient(KapsoConfig(
            api_key="valid_api_key",
            phone_number_id="100200300",
            api_version="v24.0",
            outbound_enabled=True
        ))
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"messages": [{"id": "wamid.TPL456"}]}'
        mock_response.json.return_value = {"messages": [{"id": "wamid.TPL456"}]}
        mock_post.return_value = mock_response

        components = [{"type": "body", "parameters": [{"type": "text", "text": "Token 10"}]}]
        res = client.send_template_message("+919847012345", "booking_confirmation", components=components)
        self.assertTrue(res["success"])
        self.assertEqual(res["message_id"], "wamid.TPL456")

        expected_url = "https://api.kapso.ai/meta/whatsapp/v24.0/100200300/messages"
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], expected_url)
        self.assertEqual(kwargs["headers"]["X-API-Key"], "valid_api_key")
        self.assertEqual(kwargs["json"]["template"]["name"], "booking_confirmation")

    def test_log_sanitization(self):
        secret_msg = "Failed with key: my_secret_api_key_12345 and phone: +919847012345"
        sanitized = sanitize_log_message(secret_msg)
        self.assertNotIn("+919847012345", sanitized)

    @patch("requests.get")
    @patch("requests.post")
    def test_no_network_activity_during_client_construction(self, mock_post, mock_get):
        cfg = KapsoConfig(api_key="key", phone_number_id="pid")
        _ = KapsoClient(cfg)
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    @patch("requests.post", side_effect=requests.exceptions.Timeout("Post timeout"))
    def test_send_text_timeout(self, mock_post):
        client = KapsoClient(KapsoConfig(api_key="k", phone_number_id="p", outbound_enabled=True))
        res = client.send_text_message("+919847012345", "hello")
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "timeout")

    @patch("requests.post")
    def test_send_text_rate_limited_429(self, mock_post):
        client = KapsoClient(KapsoConfig(api_key="k", phone_number_id="p", outbound_enabled=True))
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_post.return_value = mock_resp
        res = client.send_text_message("+919847012345", "hello")
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "rate_limited")
        self.assertEqual(res["status_code"], 429)

    @patch("requests.post")
    def test_send_text_unauthorized_401(self, mock_post):
        client = KapsoClient(KapsoConfig(api_key="k", phone_number_id="p", outbound_enabled=True))
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_post.return_value = mock_resp
        res = client.send_text_message("+919847012345", "hello")
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "unauthorized")
        self.assertEqual(res["status_code"], 401)

    def test_send_text_invalid_recipient_rejected(self):
        client = KapsoClient(KapsoConfig(api_key="k", phone_number_id="p", outbound_enabled=True))
        res = client.send_text_message("invalid-phone-abc", "hello")
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "invalid_phone")

    @patch("requests.post")
    def test_send_text_invalid_json_handled_gracefully(self, mock_post):
        client = KapsoClient(KapsoConfig(api_key="k", phone_number_id="p", outbound_enabled=True))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "NOT JSON"
        mock_resp.json.side_effect = ValueError("Invalid JSON")
        mock_post.return_value = mock_resp
        res = client.send_text_message("+919847012345", "hello")
        self.assertTrue(res["success"])
        self.assertIsNone(res["message_id"])

    @patch("requests.post")
    @patch("requests.get")
    def test_zero_database_modifications(self, mock_get, mock_post):
        """Verifies that Kapso client operations execute purely without side-effect database modifications."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"status": "healthy"}'
        mock_resp.json.return_value = {"status": "healthy"}
        mock_get.return_value = mock_resp

        from app import app, db, User, PatientBooking
        with app.app_context():
            user_count_before = User.query.count()
            booking_count_before = PatientBooking.query.count()

            cfg = KapsoConfig(api_key="k", phone_number_id="p", webhook_secret="secret", outbound_enabled=False)
            client = KapsoClient(cfg)

            client.health_check()
            client.verify_webhook_signature(b'{"test": 1}', "invalid")
            client.send_text_message("+919847012345", "test")

            self.assertEqual(User.query.count(), user_count_before)
            self.assertEqual(PatientBooking.query.count(), booking_count_before)


if __name__ == "__main__":
    unittest.main()
