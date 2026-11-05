"""
tests/test_groq_client.py
=========================
Unit Test Suite for Groq API Client Abstraction (Phase 3D).

All tests are 100% mocked; zero live Groq API calls or network requests.
Validates:
  1. Missing API key rejected.
  2. Empty API key rejected.
  3. Whitespace-only API key rejected.
  4. Valid configuration initialized using a mock.
  5. Correct model ID passed (and max_completion_tokens enforced).
  6. Successful response parsed (reasoning excluded from text content).
  7. Empty response handled safely without crashing.
  8. HTTP 401 handled (fail-fast, zero retries).
  9. HTTP 429 handled (transient retry, respects limit).
  10. Timeout handled (APITimeoutError caught and retried).
  11. Connection failure handled (APIConnectionError caught and retried).
  12. Transient failure retry limits respected.
"""

import os
import unittest
from unittest.mock import MagicMock, patch
import httpx
import groq

from services.groq_client import (
    GroqClient,
    GroqConfig,
    GroqClientError,
    GroqConfigurationError,
    GroqAPIError,
    GroqAuthenticationError,
    GroqBadRequestError,
    GroqRateLimitError,
    GroqTimeoutError,
    GroqConnectionError,
    GroqResponseError,
    sanitize_log_message
)


class GroqClientTestSuite(unittest.TestCase):
    """Automated tests for GroqClient and GroqConfig."""

    def setUp(self):
        self.mock_req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        self.dummy_sleep = MagicMock()

    # -----------------------------------------------------------------
    # Test 1: Missing API key rejected
    # -----------------------------------------------------------------
    def test_01_missing_api_key_rejected(self):
        """1. Missing API key rejected fail-closed before SDK instantiation."""
        with patch.dict(os.environ, {}, clear=True):
            config = GroqConfig(api_key=None)
            with self.assertRaises(GroqConfigurationError) as ctx:
                config.validate()
            self.assertIn("GROQ_API_KEY is required", str(ctx.exception))
            self.assertTrue(issubclass(GroqConfigurationError, ValueError))

            # GroqClient initialization also fails closed
            with self.assertRaises(GroqConfigurationError):
                GroqClient(config=config)

    # -----------------------------------------------------------------
    # Test 2: Empty API key rejected
    # -----------------------------------------------------------------
    def test_02_empty_api_key_rejected(self):
        """2. Empty API key rejected fail-closed."""
        config = GroqConfig(api_key="")
        with self.assertRaises(GroqConfigurationError) as ctx:
            config.validate()
        self.assertIn("GROQ_API_KEY is required", str(ctx.exception))

        with self.assertRaises(GroqConfigurationError):
            GroqClient(config=config)

    # -----------------------------------------------------------------
    # Test 3: Whitespace-only API key rejected
    # -----------------------------------------------------------------
    def test_03_whitespace_only_api_key_rejected(self):
        """3. Whitespace-only API key rejected fail-closed."""
        config = GroqConfig(api_key="   \t  \n  ")
        with self.assertRaises(GroqConfigurationError) as ctx:
            config.validate()
        self.assertIn("GROQ_API_KEY is required", str(ctx.exception))

        with self.assertRaises(GroqConfigurationError):
            GroqClient(config=config)

    # -----------------------------------------------------------------
    # Test 4: Valid configuration initialized using a mock
    # -----------------------------------------------------------------
    def test_04_valid_configuration_initialized_using_mock(self):
        """4. Valid configuration initialized using a mock (zero network activity)."""
        mock_sdk = MagicMock()
        config = GroqConfig(
            api_key="gsk_test_mock_secret_key_12345",
            model="openai/gpt-oss-120b",
            timeout=15.0,
            max_retries=2,
            max_completion_tokens=512
        )
        config.validate()  # Passes without error

        client = GroqClient(config=config, client=mock_sdk, sleep_fn=self.dummy_sleep)
        self.assertEqual(client.config.api_key, "gsk_test_mock_secret_key_12345")
        self.assertEqual(client.config.model, "openai/gpt-oss-120b")
        self.assertEqual(client.config.timeout, 15.0)
        self.assertEqual(client.config.max_completion_tokens, 512)
        # Ensure zero network calls during instantiation
        mock_sdk.chat.completions.create.assert_not_called()

        # Check representation masks secret key
        repr_str = repr(config)
        self.assertNotIn("secret_key", repr_str)
        self.assertIn("gsk_***2345", repr_str)

    # -----------------------------------------------------------------
    # Test 5: Correct model ID passed (and include_reasoning=False for GPT-OSS)
    # -----------------------------------------------------------------
    def test_05_correct_model_id_passed(self):
        """5. Correct model ID, max_completion_tokens, and include_reasoning=False passed to SDK."""
        mock_sdk = MagicMock()
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Dr. John consults on Monday."
        mock_choice.finish_reason = "stop"
        mock_response.choices = [mock_choice]
        mock_response.model = "openai/gpt-oss-120b"
        mock_sdk.chat.completions.create.return_value = mock_response

        config = GroqConfig(api_key="gsk_mock_valid_key", model="openai/gpt-oss-120b")
        client = GroqClient(config=config, client=mock_sdk, sleep_fn=self.dummy_sleep)

        messages = [{"role": "user", "content": "Who is available?"}]
        resp = client.generate_chat_completion(messages=messages)

        mock_sdk.chat.completions.create.assert_called_once()
        kwargs = mock_sdk.chat.completions.create.call_args[1]
        self.assertEqual(kwargs["model"], "openai/gpt-oss-120b")
        self.assertEqual(kwargs["max_completion_tokens"], 1024)
        self.assertIs(kwargs["include_reasoning"], False)
        self.assertNotIn("reasoning_format", kwargs)  # Never send both; GPT-OSS does not support reasoning_format
        self.assertNotIn("max_tokens", kwargs)  # Deprecated parameter not used

    def test_05b_reasoning_parameters_mutually_exclusive(self):
        """5b. Reasoning parameters are mutually exclusive (cannot configure both)."""
        with self.assertRaises(GroqConfigurationError) as ctx:
            GroqConfig(
                api_key="gsk_mock_valid_key",
                include_reasoning=False,
                reasoning_format="parsed"
            )
        self.assertIn("Cannot configure both", str(ctx.exception))

    # -----------------------------------------------------------------
    # Test 6: Successful response parsed
    # -----------------------------------------------------------------
    def test_06_successful_response_parsed(self):
        """6. Successful response parsed and internal reasoning strictly excluded."""
        mock_sdk = MagicMock()
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "PrimeCare Clinic is located at Koorachundu."
        mock_choice.message.reasoning = "Internal reasoning: The user is asking for location... step 1, step 2"
        mock_choice.finish_reason = "stop"
        mock_response.choices = [mock_choice]
        mock_response.model = "openai/gpt-oss-120b"
        mock_response.usage.prompt_tokens = 45
        mock_response.usage.completion_tokens = 12
        mock_response.usage.total_tokens = 57
        mock_sdk.chat.completions.create.return_value = mock_response

        config = GroqConfig(api_key="gsk_mock_valid_key")
        client = GroqClient(config=config, client=mock_sdk, sleep_fn=self.dummy_sleep)

        resp = client.generate_chat_completion(messages=[{"role": "user", "content": "Where are you?"}])
        self.assertEqual(resp.content, "PrimeCare Clinic is located at Koorachundu.")
        self.assertNotIn("Internal reasoning", resp.content)
        self.assertNotIn("step 1", resp.content)
        self.assertEqual(resp.finish_reason, "stop")
        self.assertEqual(resp.usage["total_tokens"], 57)
        self.assertTrue(bool(resp))

        config = GroqConfig(api_key="gsk_mock_valid_key")
        client = GroqClient(config=config, client=mock_sdk, sleep_fn=self.dummy_sleep)

        resp = client.generate_chat_completion(messages=[{"role": "user", "content": "Where are you?"}])
        self.assertEqual(resp.content, "PrimeCare Clinic is located at Koorachundu.")
        self.assertNotIn("Internal reasoning", resp.content)
        self.assertEqual(resp.finish_reason, "stop")
        self.assertEqual(resp.usage["total_tokens"], 57)
        self.assertTrue(bool(resp))

    # -----------------------------------------------------------------
    # Test 7: Empty response handled safely
    # -----------------------------------------------------------------
    def test_07_empty_response_handled_safely(self):
        """7. Empty response handled safely without IndexError or crash."""
        mock_sdk = MagicMock()
        # Case A: empty choices list
        mock_empty_resp = MagicMock()
        mock_empty_resp.choices = []
        mock_sdk.chat.completions.create.return_value = mock_empty_resp

        config = GroqConfig(api_key="gsk_mock_valid_key")
        client = GroqClient(config=config, client=mock_sdk, sleep_fn=self.dummy_sleep)

        resp_a = client.generate_chat_completion(messages=[{"role": "user", "content": "test"}])
        self.assertEqual(resp_a.content, "")
        self.assertFalse(bool(resp_a))

        # Case B: choice with None content
        mock_none_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_none_resp.choices = [mock_choice]
        mock_sdk.chat.completions.create.return_value = mock_none_resp

        resp_b = client.generate_chat_completion(messages=[{"role": "user", "content": "test"}])
        self.assertEqual(resp_b.content, "")

    # -----------------------------------------------------------------
    # Test 8: HTTP 401 handled
    # -----------------------------------------------------------------
    def test_08_http_401_handled(self):
        """8. HTTP 401 Authentication failure handled fail-fast with zero retries."""
        mock_sdk = MagicMock()
        res401 = httpx.Response(401, request=self.mock_req)
        mock_sdk.chat.completions.create.side_effect = groq.AuthenticationError(
            "Invalid API Key",
            response=res401,
            body={"error": {"message": "Invalid API Key"}}
        )

        config = GroqConfig(api_key="gsk_invalid_key", max_retries=2)
        client = GroqClient(config=config, client=mock_sdk, sleep_fn=self.dummy_sleep)

        with self.assertRaises(GroqAuthenticationError) as ctx:
            client.generate_chat_completion(messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(ctx.exception.status_code, 401)
        # Strict requirement: zero retry attempts on authentication failures
        self.assertEqual(mock_sdk.chat.completions.create.call_count, 1)
        self.dummy_sleep.assert_not_called()

    # -----------------------------------------------------------------
    # Test 9: HTTP 429 handled
    # -----------------------------------------------------------------
    def test_09_http_429_handled(self):
        """9. HTTP 429 Rate limit handled with bounded retries."""
        mock_sdk = MagicMock()
        res429 = httpx.Response(429, request=self.mock_req)
        mock_sdk.chat.completions.create.side_effect = groq.RateLimitError(
            "Rate limit exceeded",
            response=res429,
            body={"error": {"message": "Rate limit reached"}}
        )

        config = GroqConfig(api_key="gsk_valid_key", max_retries=2)
        client = GroqClient(config=config, client=mock_sdk, sleep_fn=self.dummy_sleep)

        with self.assertRaises(GroqRateLimitError) as ctx:
            client.generate_chat_completion(messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(ctx.exception.status_code, 429)
        # 1 initial + 2 retries = 3 calls
        self.assertEqual(mock_sdk.chat.completions.create.call_count, 3)
        self.assertEqual(self.dummy_sleep.call_count, 2)

    # -----------------------------------------------------------------
    # Test 10: Timeout handled
    # -----------------------------------------------------------------
    def test_10_timeout_handled(self):
        """10. Timeout handled (APITimeoutError caught and retried)."""
        mock_sdk = MagicMock()
        mock_sdk.chat.completions.create.side_effect = groq.APITimeoutError(request=self.mock_req)

        config = GroqConfig(api_key="gsk_valid_key", max_retries=1)
        client = GroqClient(config=config, client=mock_sdk, sleep_fn=self.dummy_sleep)

        with self.assertRaises(GroqTimeoutError):
            client.generate_chat_completion(messages=[{"role": "user", "content": "hi"}])

        # 1 initial + 1 retry = 2 calls
        self.assertEqual(mock_sdk.chat.completions.create.call_count, 2)
        self.assertEqual(self.dummy_sleep.call_count, 1)

    # -----------------------------------------------------------------
    # Test 11: Connection failure handled
    # -----------------------------------------------------------------
    def test_11_connection_failure_handled(self):
        """11. Connection failure handled (APIConnectionError caught and retried)."""
        mock_sdk = MagicMock()
        mock_sdk.chat.completions.create.side_effect = groq.APIConnectionError(request=self.mock_req)

        config = GroqConfig(api_key="gsk_valid_key", max_retries=1)
        client = GroqClient(config=config, client=mock_sdk, sleep_fn=self.dummy_sleep)

        with self.assertRaises(GroqConnectionError):
            client.generate_chat_completion(messages=[{"role": "user", "content": "hi"}])

        # 1 initial + 1 retry = 2 calls
        self.assertEqual(mock_sdk.chat.completions.create.call_count, 2)
        self.assertEqual(self.dummy_sleep.call_count, 1)

    # -----------------------------------------------------------------
    # Test 12: Transient failure retry limits respected
    # -----------------------------------------------------------------
    def test_12_transient_failure_retry_limits_respected(self):
        """12. Transient failure retry limits respected (recovers on transient error)."""
        mock_sdk = MagicMock()
        res429 = httpx.Response(429, request=self.mock_req)
        res503 = httpx.Response(503, request=self.mock_req)

        # Attempt 1 fails (429), Attempt 2 fails (503), Attempt 3 succeeds
        mock_success = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Recovered successfully!"
        mock_choice.finish_reason = "stop"
        mock_success.choices = [mock_choice]
        mock_success.model = "openai/gpt-oss-120b"

        mock_sdk.chat.completions.create.side_effect = [
            groq.RateLimitError("Rate limited", response=res429, body={}),
            groq.InternalServerError("Service unavailable", response=res503, body={}),
            mock_success
        ]

        config = GroqConfig(api_key="gsk_valid_key", max_retries=2)
        client = GroqClient(config=config, client=mock_sdk, sleep_fn=self.dummy_sleep)

        resp = client.generate_chat_completion(messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "Recovered successfully!")
        self.assertEqual(mock_sdk.chat.completions.create.call_count, 3)
        self.assertEqual(self.dummy_sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
