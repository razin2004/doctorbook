"""
services/groq_client.py
=======================
Groq API Client Abstraction for PrimeCare Clinic WhatsApp AI Assistant.

Compliant with Groq official Python SDK (groq==1.7.0):
  - Model: configurable via GROQ_MODEL environment variable (default: openai/gpt-oss-120b).
  - Uses `max_completion_tokens` rather than deprecated `max_tokens`.
  - GPT-OSS reasoning handling: uses `include_reasoning=False` for GPT-OSS models.
    Never sends both `include_reasoning` and `reasoning_format`.
  - Strips/excludes internal reasoning from caller responses and user-facing messages.
  - Fail-closed API key validation: rejects missing, empty, or whitespace-only keys before SDK instantiation.
  - Zero network calls on client instantiation.
  - Bounded retry logic for transient errors (HTTP 429, 5xx, timeouts, connection errors).
  - Strict non-retry on client errors (HTTP 400, 401, 403, 404, 422).
  - PII, secret, and API key sanitization in all logs and exception messages.
  - Complete architectural independence from the website Gemini assistant.
"""

import os
import re
import time
import logging
from typing import Optional, List, Dict, Any, Callable

logger = logging.getLogger("groq_client")


# =====================================================================
# EXCEPTIONS
# =====================================================================

class GroqClientError(Exception):
    """Base exception for all Groq client errors."""
    pass


class GroqConfigurationError(GroqClientError, ValueError):
    """Raised when Groq configuration or API key is missing or invalid."""
    pass


class GroqAPIError(GroqClientError):
    """Raised when Groq API returns an unrecoverable or server error."""
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class GroqAuthenticationError(GroqAPIError):
    """Raised when Groq authentication fails (HTTP 401)."""
    pass


class GroqBadRequestError(GroqAPIError):
    """Raised when Groq rejects a request (HTTP 400 or other 4xx)."""
    pass


class GroqRateLimitError(GroqAPIError):
    """Raised when Groq rate limit is exceeded (HTTP 429)."""
    pass


class GroqTimeoutError(GroqAPIError):
    """Raised when a request to Groq times out."""
    pass


class GroqConnectionError(GroqAPIError):
    """Raised when connection to Groq fails."""
    pass


class GroqResponseError(GroqAPIError):
    """Raised when Groq returns an invalid or malformed response."""
    pass


# =====================================================================
# SANITIZATION UTILITIES
# =====================================================================

def sanitize_log_message(msg: Any) -> str:
    """Sanitizes PII, API keys, phone numbers, and secrets from log messages."""
    raw = str(msg)
    # Redact Groq API keys (e.g. gsk_...)
    cleaned = re.sub(r'gsk_[a-zA-Z0-9_\-]+', '[REDACTED_GROQ_KEY]', raw)
    # Redact generic api keys or tokens
    cleaned = re.sub(
        r'(api[_-]?key|secret|token|bearer|authorization)[\s:=]+([a-zA-Z0-9_\-]+)',
        r'\1=[REDACTED]',
        cleaned,
        flags=re.IGNORECASE
    )
    # Redact phone numbers (E.164 and standard international formats)
    cleaned = re.sub(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', '[REDACTED_PHONE]', cleaned)
    # Redact email addresses
    cleaned = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '[REDACTED_EMAIL]', cleaned)
    return cleaned


# =====================================================================
# CONFIGURATION
# =====================================================================

class GroqConfig:
    """
    Configuration model for Groq API client.
    Enforces fail-closed credential and parameter validation.
    """
    DEFAULT_MODEL = "openai/gpt-oss-120b"
    DEFAULT_TIMEOUT = 30.0
    DEFAULT_MAX_RETRIES = 2
    DEFAULT_MAX_COMPLETION_TOKENS = 1024
    DEFAULT_TEMPERATURE = 0.3
    DEFAULT_INCLUDE_REASONING = False

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        max_completion_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        include_reasoning: Optional[bool] = None,
        reasoning_format: Optional[str] = None
    ):
        raw_key = api_key if api_key is not None else os.environ.get("GROQ_API_KEY", "")
        self.api_key = str(raw_key).strip() if raw_key else ""

        raw_model = model if model is not None else os.environ.get("GROQ_MODEL", self.DEFAULT_MODEL)
        self.model = str(raw_model).strip() if raw_model else self.DEFAULT_MODEL

        self.timeout = float(timeout) if timeout is not None else self.DEFAULT_TIMEOUT
        self.max_retries = int(max_retries) if max_retries is not None else self.DEFAULT_MAX_RETRIES
        self.max_completion_tokens = int(max_completion_tokens) if max_completion_tokens is not None else self.DEFAULT_MAX_COMPLETION_TOKENS
        self.temperature = float(temperature) if temperature is not None else self.DEFAULT_TEMPERATURE
        
        # Enforce reasoning parameter rules
        if include_reasoning is not None and reasoning_format is not None:
            raise GroqConfigurationError(
                "Cannot configure both 'include_reasoning' and 'reasoning_format'. "
                "For GPT-OSS models (e.g. openai/gpt-oss-120b), use include_reasoning=False."
            )
        
        if include_reasoning is not None:
            self.include_reasoning = bool(include_reasoning)
            self.reasoning_format = None
        elif reasoning_format is not None:
            self.include_reasoning = None
            self.reasoning_format = str(reasoning_format).strip()
        else:
            # Default for GPT-OSS: include_reasoning=False, no reasoning_format
            self.include_reasoning = self.DEFAULT_INCLUDE_REASONING
            self.reasoning_format = None

    @classmethod
    def from_env(cls) -> "GroqConfig":
        """Loads Groq configuration from the active environment variables."""
        return cls()

    def validate(self) -> None:
        """
        Validates configuration integrity.
        Raises GroqConfigurationError if required fields are missing or invalid.
        """
        if not self.api_key:
            raise GroqConfigurationError(
                "GROQ_API_KEY is required and cannot be empty or whitespace-only."
            )
        if not self.model:
            raise GroqConfigurationError("GROQ_MODEL cannot be empty or whitespace-only.")
        if self.timeout <= 0:
            raise GroqConfigurationError("timeout must be greater than 0.")
        if self.max_retries < 0:
            raise GroqConfigurationError("max_retries cannot be negative.")
        if self.max_completion_tokens <= 0:
            raise GroqConfigurationError("max_completion_tokens must be greater than 0.")
        if self.include_reasoning is not None and self.reasoning_format is not None:
            raise GroqConfigurationError(
                "Cannot configure both 'include_reasoning' and 'reasoning_format'."
            )

    def __repr__(self) -> str:
        masked_key = f"{self.api_key[:4]}***{self.api_key[-4:]}" if len(self.api_key) > 8 else "***"
        return f"<GroqConfig model='{self.model}' timeout={self.timeout} api_key='{masked_key}'>"


# =====================================================================
# RESPONSE MODEL
# =====================================================================

class GroqCompletionResponse:
    """
    Structured representation of a Groq chat completion response.
    Guarantees that internal model reasoning is strictly excluded from user-facing text.
    """
    def __init__(
        self,
        content: str = "",
        model: str = "",
        finish_reason: Optional[str] = None,
        usage: Optional[Dict[str, int]] = None,
        raw_response: Any = None
    ):
        self.content = content.strip() if content else ""
        self.model = model
        self.finish_reason = finish_reason
        self.usage = usage or {}
        self.raw_response = raw_response

    def __str__(self) -> str:
        return self.content

    def __bool__(self) -> bool:
        return bool(self.content)


# =====================================================================
# CLIENT IMPLEMENTATION
# =====================================================================

class GroqClient:
    """
    Reusable Groq API Client abstraction for PrimeCare Clinic.

    Guarantees:
      - Validates configuration fail-closed before SDK instantiation.
      - Zero network calls on instantiation.
      - Uses `include_reasoning=False` for GPT-OSS models (never sends both reasoning parameters).
      - Bounded exponential backoff retries for transient errors (429, 5xx, timeouts, connection errors).
      - Immediate fail-fast without retry for authentication (401) and client request (400) errors.
      - Never logs API keys, secrets, or patient PII.
    """
    def __init__(
        self,
        config: Optional[GroqConfig] = None,
        client: Optional[Any] = None,
        sleep_fn: Optional[Callable[[float], None]] = None
    ):
        cfg = config if config is not None else GroqConfig.from_env()
        cfg.validate()
        self.config = cfg
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep

        if client is not None:
            self._client = client
        else:
            import groq
            # Official Groq SDK client instantiation (performs zero network calls)
            self._client = groq.Groq(
                api_key=self.config.api_key,
                timeout=self.config.timeout
            )

    def _compute_backoff(self, attempt: int) -> float:
        """Computes exponential backoff delay."""
        delay = 0.5 * (2 ** attempt)
        return min(delay, 8.0)

    def generate_chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        max_completion_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        include_reasoning: Optional[bool] = None,
        reasoning_format: Optional[str] = None,
        timeout: Optional[float] = None
    ) -> GroqCompletionResponse:
        """
        Generates a chat completion using the official Groq SDK.

        Args:
            messages: List of chat message dicts, e.g. [{"role": "system", "content": ...}, ...]
            model: Optional model override. Defaults to config.model.
            max_completion_tokens: Optional token limit. Defaults to config.max_completion_tokens.
            temperature: Optional sampling temperature. Defaults to config.temperature.
            include_reasoning: Optional bool for GPT-OSS reasoning control (defaults to False).
            reasoning_format: Optional reasoning format for non-GPT-OSS models.
            timeout: Optional per-request timeout override.

        Returns:
            GroqCompletionResponse object containing sanitized .content and metadata.
        """
        import groq

        target_model = (model or self.config.model).strip()
        target_tokens = max_completion_tokens or self.config.max_completion_tokens
        target_temp = temperature if temperature is not None else self.config.temperature

        create_kwargs: Dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "max_completion_tokens": target_tokens,
            "temperature": target_temp,
        }

        # Apply reasoning options: NEVER send both include_reasoning and reasoning_format
        # For GPT-OSS models, use include_reasoning=False
        is_gpt_oss = "gpt-oss" in target_model.lower()
        if is_gpt_oss or include_reasoning is not None:
            val = include_reasoning if include_reasoning is not None else (
                self.config.include_reasoning if self.config.include_reasoning is not None else False
            )
            create_kwargs["include_reasoning"] = val
        elif reasoning_format or self.config.reasoning_format:
            create_kwargs["reasoning_format"] = reasoning_format or self.config.reasoning_format

        if timeout is not None:
            create_kwargs["timeout"] = timeout

        for attempt in range(1 + self.config.max_retries):
            try:
                response = self._client.chat.completions.create(**create_kwargs)

                # Parse and validate response
                if not response or not hasattr(response, "choices") or not response.choices:
                    logger.warning("Groq API returned an empty choices list or None.")
                    return GroqCompletionResponse(
                        content="",
                        model=target_model,
                        finish_reason=None,
                        raw_response=response
                    )

                choice = response.choices[0]
                message = getattr(choice, "message", None)
                content = ""
                if message is not None:
                    # Extract content; strictly ignore internal reasoning
                    content = getattr(message, "content", "") or ""

                finish_reason = getattr(choice, "finish_reason", None)
                usage_dict = {}
                if hasattr(response, "usage") and response.usage:
                    usage_dict = {
                        "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                        "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                        "total_tokens": getattr(response.usage, "total_tokens", 0)
                    }

                return GroqCompletionResponse(
                    content=content,
                    model=getattr(response, "model", target_model),
                    finish_reason=finish_reason,
                    usage=usage_dict,
                    raw_response=response
                )

            except groq.AuthenticationError as e:
                # HTTP 401: Invalid or unauthorized API key - DO NOT RETRY
                sanitized_msg = sanitize_log_message(str(e))
                logger.error(f"Groq authentication error (401): {sanitized_msg}")
                raise GroqAuthenticationError(
                    "Groq authentication failed (HTTP 401). Please check GROQ_API_KEY.",
                    status_code=401
                ) from None

            except groq.BadRequestError as e:
                # HTTP 400: Malformed request - DO NOT RETRY
                sanitized_msg = sanitize_log_message(str(e))
                logger.error(f"Groq bad request error (400): {sanitized_msg}")
                raise GroqBadRequestError(
                    f"Groq request rejected (HTTP 400): {sanitized_msg}",
                    status_code=400
                ) from None

            except (groq.PermissionDeniedError, groq.NotFoundError, groq.UnprocessableEntityError) as e:
                # Other 4xx client errors - DO NOT RETRY
                status = getattr(e, "status_code", 400)
                sanitized_msg = sanitize_log_message(str(e))
                logger.error(f"Groq client error ({status}): {sanitized_msg}")
                raise GroqBadRequestError(
                    f"Groq client error ({status}): {sanitized_msg}",
                    status_code=status
                ) from None

            except groq.RateLimitError as e:
                # HTTP 429: Rate limited - Bounded retry
                if attempt < self.config.max_retries:
                    delay = self._compute_backoff(attempt)
                    logger.warning(
                        f"Groq rate limit (429). Retrying in {delay:.2f}s (attempt {attempt + 1}/{self.config.max_retries})"
                    )
                    self._sleep(delay)
                    continue
                logger.error("Groq rate limit (429) exceeded maximum retry attempts.")
                raise GroqRateLimitError(
                    "Groq rate limit exceeded after maximum retries.",
                    status_code=429
                ) from None

            except groq.APITimeoutError as e:
                # Timeout: Transient - Bounded retry
                if attempt < self.config.max_retries:
                    delay = self._compute_backoff(attempt)
                    logger.warning(
                        f"Groq API timeout. Retrying in {delay:.2f}s (attempt {attempt + 1}/{self.config.max_retries})"
                    )
                    self._sleep(delay)
                    continue
                logger.error("Groq API request timed out after maximum retries.")
                raise GroqTimeoutError("Groq API request timed out after maximum retries.") from None

            except groq.APIConnectionError as e:
                # Connection failure: Transient - Bounded retry
                if attempt < self.config.max_retries:
                    delay = self._compute_backoff(attempt)
                    logger.warning(
                        f"Groq connection failure. Retrying in {delay:.2f}s (attempt {attempt + 1}/{self.config.max_retries})"
                    )
                    self._sleep(delay)
                    continue
                logger.error("Groq API connection failure after maximum retries.")
                raise GroqConnectionError("Groq API connection failed after maximum retries.") from None

            except (groq.InternalServerError, groq.APIStatusError) as e:
                # HTTP 5xx: Transient server error - Bounded retry
                status = getattr(e, "status_code", 500)
                if attempt < self.config.max_retries and (status is None or status >= 500):
                    delay = self._compute_backoff(attempt)
                    logger.warning(
                        f"Groq server error ({status}). Retrying in {delay:.2f}s (attempt {attempt + 1}/{self.config.max_retries})"
                    )
                    self._sleep(delay)
                    continue
                logger.error(f"Groq server error ({status}) after maximum retries.")
                raise GroqAPIError(
                    f"Groq server error ({status}) after maximum retries.",
                    status_code=status
                ) from None

            except GroqClientError:
                raise

            except Exception as e:
                sanitized_msg = sanitize_log_message(str(e))
                logger.error(f"Unexpected error during Groq API call: {sanitized_msg}")
                raise GroqAPIError(f"Unexpected Groq API error: {sanitized_msg}") from None
