"""
services/kapso_client.py
========================
Kapso WhatsApp Business API Integration Client for PrimeCare Clinic.

Compliant with official Kapso API documentation:
  - Messaging: POST https://api.kapso.ai/meta/whatsapp/{api_version}/{phone_number_id}/messages
  - Phone Health: GET https://api.kapso.ai/platform/v1/whatsapp/phone_numbers/{phone_number_id}/health
  - Authentication: X-API-Key: <KAPSO_API_KEY>
  - Webhook Security: HMAC-SHA256 signature verification (X-Webhook-Signature) using raw body bytes
  - Feature Flag: KAPSO_OUTBOUND_ENABLED (default: False) strictly protects live patient communication

Security Guarantees:
  - Zero PII, OTP, secret, or phone number leakage in application logs.
  - Fail-closed error handling: returns structured, sanitized error states without crashing.
  - Zero network activity on client instantiation.
  - Health check response caching to prevent startup polling loops.
"""

import os
import re
import json
import time
import math
import hmac
import hashlib
import logging
from typing import Optional, Dict, Any, Tuple
import requests

from phone_utils import normalize_phone_number

logger = logging.getLogger("kapso_client")


def _mask_phone(phone: Optional[str]) -> str:
    """Masks phone number for safe logging (e.g. +91*****1234)."""
    if not phone:
        return "[EMPTY]"
    clean = str(phone).strip()
    if len(clean) <= 4:
        return "****"
    return f"{clean[:3]}*****{clean[-4:]}"


def sanitize_log_message(msg: str) -> str:
    """Sanitizes PII, API keys, and phone numbers from logs."""
    raw = str(msg)
    cleaned = re.sub(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', '[REDACTED_PHONE]', raw)
    cleaned = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '[REDACTED_EMAIL]', cleaned)
    cleaned = re.sub(r'(api[_-]?key|secret|token)[\s:=]+([a-zA-Z0-9_\-]+)', r'\1=[REDACTED]', cleaned, flags=re.IGNORECASE)
    return cleaned


class KapsoConfig:
    """
    Configuration model for Kapso WhatsApp Business API credentials and endpoints.
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        phone_number_id: Optional[str] = None,
        base_url: Optional[str] = None,
        api_version: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        webhook_verify_token: Optional[str] = None,
        webhook_mode: Optional[str] = None,
        outbound_enabled: Optional[bool] = None,
        timeout: int = 10
    ):
        self.api_key = str(api_key if api_key is not None else os.environ.get("KAPSO_API_KEY", "")).strip()
        self.phone_number_id = str(phone_number_id if phone_number_id is not None else os.environ.get("KAPSO_PHONE_NUMBER_ID", "")).strip()
        self.base_url = str(base_url if base_url is not None else os.environ.get("KAPSO_BASE_URL", "https://api.kapso.ai")).strip().rstrip("/")
        self.api_version = str(api_version if api_version is not None else os.environ.get("KAPSO_API_VERSION", "v24.0")).strip().lstrip("/")
        self.webhook_secret = str(webhook_secret if webhook_secret is not None else os.environ.get("KAPSO_WEBHOOK_SECRET", "")).strip()
        self.webhook_verify_token = str(webhook_verify_token if webhook_verify_token is not None else os.environ.get("KAPSO_WEBHOOK_VERIFY_TOKEN", "")).strip()
        
        raw_mode = (webhook_mode or os.environ.get("KAPSO_WEBHOOK_MODE", "kapso")).strip().lower()
        self.webhook_mode = raw_mode if raw_mode in ("kapso", "meta") else "kapso"

        if outbound_enabled is not None:
            self.outbound_enabled = bool(outbound_enabled)
        else:
            raw_flag = os.environ.get("KAPSO_OUTBOUND_ENABLED", "false").strip().lower()
            self.outbound_enabled = raw_flag in ("true", "1", "yes", "enabled")
            
        self.timeout = max(1, int(timeout))

    @classmethod
    def from_env(cls) -> "KapsoConfig":
        """Loads configuration from active environment variables."""
        return cls()

    def is_configured(self) -> bool:
        """Returns True if essential Kapso credentials are present."""
        return bool(self.api_key and self.phone_number_id)

    def validate(self) -> None:
        """
        Validates configuration integrity.
        Raises ValueError if required fields are missing.
        """
        if not self.api_key:
            raise ValueError("KAPSO_API_KEY is required but not configured.")
        if not self.phone_number_id:
            raise ValueError("KAPSO_PHONE_NUMBER_ID is required but not configured.")
        if not self.base_url:
            raise ValueError("KAPSO_BASE_URL is invalid or empty.")
        if not self.api_version:
            raise ValueError("KAPSO_API_VERSION is invalid or empty.")


class KapsoClient:
    """
    Official HTTP client for Kapso WhatsApp API interactions.
    Provides mockable, fail-closed methods for templates, text messages, health checks, and webhooks.
    """
    def __init__(self, config: Optional[KapsoConfig] = None):
        self.config = config or KapsoConfig.from_env()
        self._health_cache = {
            "data": None,
            "ts": 0.0,
            "ttl": 180.0  # seconds to cache health response (3 minutes per provider interval)
        }
        self._retry_after_until = 0.0

    @property
    def is_configured(self) -> bool:
        return self.config.is_configured()

    def _get_headers(self) -> Dict[str, str]:
        """Documented authentication header: X-API-Key."""
        return {
            "X-API-Key": self.config.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def health_check(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Performs a health check against the Kapso phone health endpoint.
        Documented endpoint: GET /platform/v1/whatsapp/phone_numbers/{phone_number_id}/health
        """
        if not self.is_configured:
            return {
                "healthy": False,
                "status": "unconfigured",
                "message": "Kapso API credentials are not configured in environment.",
                "status_code": None
            }

        now = time.time()
        # Backoff check when 429 Retry-After is active
        if not force_refresh and now < self._retry_after_until:
            remaining = int(max(1, math.ceil(self._retry_after_until - now)))
            return {
                "healthy": False,
                "status": "rate_limited",
                "message": f"Kapso phone health check rate limit exceeded. Backoff active for {remaining}s.",
                "status_code": 429,
                "retry_after": remaining
            }

        if not force_refresh and self._health_cache["data"] is not None:
            if (now - self._health_cache["ts"]) < self._health_cache["ttl"]:
                return self._health_cache["data"]

        endpoint = f"{self.config.base_url}/platform/v1/whatsapp/phone_numbers/{self.config.phone_number_id}/health"
        try:
            response = requests.get(
                endpoint,
                headers={"X-API-Key": self.config.api_key, "Accept": "application/json"},
                timeout=self.config.timeout
            )
            if response.status_code == 200:
                data = None
                try:
                    if response.text and response.text.strip():
                        data = response.json()
                except Exception:
                    data = None

                if not isinstance(data, dict) or not data:
                    result = {
                        "healthy": False,
                        "status": "invalid_response",
                        "message": "Kapso health check returned empty or invalid JSON response.",
                        "status_code": 200,
                        "health_status": "UNKNOWN"
                    }
                else:
                    raw_status = data.get("status") or data.get("health_status")
                    if not raw_status or not isinstance(raw_status, str):
                        result = {
                            "healthy": False,
                            "status": "invalid_response",
                            "message": "Kapso health check response missing required top-level status.",
                            "status_code": 200,
                            "health_status": "UNKNOWN"
                        }
                    else:
                        norm_status = raw_status.strip().lower()
                        # Documented statuses: healthy, degraded, unhealthy, error
                        is_healthy = norm_status in ("healthy", "available", "connected", "online", "active")
                        is_degraded = norm_status in ("degraded", "warning")
                        is_unhealthy = norm_status in ("unhealthy", "disconnected", "offline", "down")
                        is_error = norm_status in ("error", "failed")

                        if is_healthy:
                            computed_status = "healthy"
                        elif is_degraded:
                            computed_status = "degraded"
                        elif is_unhealthy:
                            computed_status = "unhealthy"
                        elif is_error:
                            computed_status = "error"
                        else:
                            computed_status = "unknown"

                        result = {
                            "healthy": is_healthy,
                            "status": computed_status,
                            "message": sanitize_log_message(f"Kapso WhatsApp phone health status: {norm_status}"),
                            "status_code": 200,
                            "health_status": norm_status.upper()
                        }

                    # Preserve timestamp, checks, retry_after if present in response
                    if isinstance(data, dict):
                        if "timestamp" in data:
                            result["timestamp"] = data["timestamp"]
                        if "checks" in data:
                            result["checks"] = data["checks"]
                        if "retry_after" in data:
                            result["retry_after"] = data["retry_after"]

            elif response.status_code == 401:
                result = {
                    "healthy": False,
                    "status": "unauthorized",
                    "message": "Kapso API authentication failed: invalid API key.",
                    "status_code": 401
                }
            elif response.status_code == 404:
                result = {
                    "healthy": False,
                    "status": "not_found",
                    "message": "Kapso WhatsApp phone number ID not found.",
                    "status_code": 404
                }
            elif response.status_code == 429:
                retry_after_hdr = getattr(response, "headers", {}).get("Retry-After") if hasattr(response, "headers") else None
                retry_seconds = 60
                if retry_after_hdr:
                    try:
                        retry_seconds = int(str(retry_after_hdr).strip())
                    except (ValueError, TypeError):
                        retry_seconds = 60
                self._retry_after_until = now + retry_seconds
                result = {
                    "healthy": False,
                    "status": "rate_limited",
                    "message": f"Kapso phone health check rate limit exceeded. Retry after {retry_seconds}s.",
                    "status_code": 429,
                    "retry_after": retry_seconds
                }
            elif response.status_code in (500, 502, 503, 504):
                result = {
                    "healthy": False,
                    "status": "server_error",
                    "message": f"Kapso health check returned upstream server error HTTP {response.status_code}.",
                    "status_code": response.status_code
                }
            else:
                result = {
                    "healthy": False,
                    "status": "error",
                    "message": f"Kapso health check returned HTTP {response.status_code}.",
                    "status_code": response.status_code
                }
        except requests.exceptions.Timeout:
            result = {
                "healthy": False,
                "status": "timeout",
                "message": f"Kapso health check timed out after {self.config.timeout} seconds.",
                "status_code": None
            }
        except requests.exceptions.RequestException as e:
            result = {
                "healthy": False,
                "status": "connection_error",
                "message": f"Kapso health check network error: {type(e).__name__}",
                "status_code": None
            }

        self._health_cache = {"data": result, "ts": now, "ttl": self._health_cache["ttl"]}
        return result

    def send_template_message(
        self,
        recipient_phone: str,
        template_name: str,
        language_code: str = "en",
        components: Optional[list] = None
    ) -> Dict[str, Any]:
        """
        Sends an approved WhatsApp message template to a recipient phone number.
        Documented endpoint: POST /meta/whatsapp/{api_version}/{phone_number_id}/messages
        """
        # Strict Feature Flag Check
        if not self.config.outbound_enabled:
            return {
                "success": False,
                "message_id": None,
                "status": "outbound_disabled",
                "error": "Outbound WhatsApp messaging is disabled by system configuration.",
                "status_code": None
            }

        if not self.is_configured:
            return {
                "success": False,
                "message_id": None,
                "status": "unconfigured",
                "error": "Kapso credentials not configured.",
                "status_code": None
            }

        norm_phone = normalize_phone_number(recipient_phone)
        if not norm_phone:
            return {
                "success": False,
                "message_id": None,
                "status": "invalid_phone",
                "error": "Invalid recipient phone number format.",
                "status_code": 400
            }

        clean_recipient = norm_phone.lstrip("+")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_recipient,
            "type": "template",
            "template": {
                "name": template_name.strip(),
                "language": {"code": language_code.strip()},
                "components": components or []
            }
        }

        endpoint = f"{self.config.base_url}/meta/whatsapp/{self.config.api_version}/{self.config.phone_number_id}/messages"
        try:
            response = requests.post(
                endpoint,
                headers=self._get_headers(),
                json=payload,
                timeout=self.config.timeout
            )
            if response.status_code in (200, 201, 202):
                try:
                    data = response.json() if response.text else {}
                except Exception:
                    data = {}
                messages = data.get("messages", [])
                msg_id = messages[0].get("id") if messages else None
                return {
                    "success": True,
                    "message_id": msg_id,
                    "status": "accepted",  # API accepted the message; distinct from delivered status
                    "error": None,
                    "status_code": response.status_code
                }
            elif response.status_code == 400:
                return {
                    "success": False,
                    "message_id": None,
                    "status": "rejected",
                    "error": "Kapso API rejected request parameters (HTTP 400).",
                    "status_code": 400
                }
            elif response.status_code == 401:
                return {
                    "success": False,
                    "message_id": None,
                    "status": "unauthorized",
                    "error": "Kapso API authentication failed: invalid API key.",
                    "status_code": 401
                }
            elif response.status_code == 429:
                return {
                    "success": False,
                    "message_id": None,
                    "status": "rate_limited",
                    "error": "Kapso API rate limit reached.",
                    "status_code": 429
                }
            else:
                return {
                    "success": False,
                    "message_id": None,
                    "status": "failed",
                    "error": f"Kapso API returned error HTTP {response.status_code}.",
                    "status_code": response.status_code
                }
        except requests.exceptions.Timeout:
            return {
                "success": False,
                "message_id": None,
                "status": "timeout",
                "error": f"Request timed out after {self.config.timeout}s.",
                "status_code": None
            }
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "message_id": None,
                "status": "network_error",
                "error": f"Network exception: {type(e).__name__}",
                "status_code": None
            }

    def send_text_message(
        self,
        recipient_phone: Optional[str] = None,
        text_body: str = "",
        bsuid: Optional[str] = None,
        scoped_user_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Sends a standard WhatsApp text message to a recipient (phone number or BSUID).
        Documented endpoint: POST /meta/whatsapp/{api_version}/{phone_number_id}/messages
        """
        # Strict Feature Flag Check
        if not self.config.outbound_enabled:
            return {
                "success": False,
                "message_id": None,
                "status": "outbound_disabled",
                "error": "Outbound WhatsApp messaging is disabled by system configuration.",
                "status_code": None
            }

        if not self.is_configured:
            return {
                "success": False,
                "message_id": None,
                "status": "unconfigured",
                "error": "Kapso credentials not configured.",
                "status_code": None
            }

        scoped_target = (bsuid or scoped_user_id or "").strip()
        phone_target = (recipient_phone or "").strip()

        if phone_target:
            norm_phone = normalize_phone_number(phone_target)
            if not norm_phone:
                if not scoped_target:
                    return {
                        "success": False,
                        "message_id": None,
                        "status": "invalid_phone",
                        "error": "Invalid recipient phone number format.",
                        "status_code": 400
                    }
                clean_recipient = scoped_target
            else:
                clean_recipient = norm_phone.lstrip("+")
        elif scoped_target:
            clean_recipient = scoped_target
        else:
            return {
                "success": False,
                "message_id": None,
                "status": "invalid_phone",
                "error": "No recipient phone number or BSUID specified.",
                "status_code": 400
            }

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_recipient,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": text_body
            }
        }

        endpoint = f"{self.config.base_url}/meta/whatsapp/{self.config.api_version}/{self.config.phone_number_id}/messages"
        try:
            response = requests.post(
                endpoint,
                headers=self._get_headers(),
                json=payload,
                timeout=self.config.timeout
            )
            if response.status_code in (200, 201, 202):
                try:
                    data = response.json() if response.text else {}
                except Exception:
                    data = {}
                messages = data.get("messages", [])
                msg_id = messages[0].get("id") if messages else None
                return {
                    "success": True,
                    "message_id": msg_id,
                    "status": "accepted",
                    "error": None,
                    "status_code": response.status_code
                }
            elif response.status_code == 400:
                return {
                    "success": False,
                    "message_id": None,
                    "status": "rejected",
                    "error": "Kapso API rejected request parameters (HTTP 400).",
                    "status_code": 400
                }
            elif response.status_code == 401:
                return {
                    "success": False,
                    "message_id": None,
                    "status": "unauthorized",
                    "error": "Kapso API authentication failed: invalid API key.",
                    "status_code": 401
                }
            elif response.status_code == 429:
                return {
                    "success": False,
                    "message_id": None,
                    "status": "rate_limited",
                    "error": "Kapso API rate limit reached.",
                    "status_code": 429
                }
            else:
                return {
                    "success": False,
                    "message_id": None,
                    "status": "failed",
                    "error": f"Kapso API returned HTTP {response.status_code}.",
                    "status_code": response.status_code
                }
        except requests.exceptions.Timeout:
            return {
                "success": False,
                "message_id": None,
                "status": "timeout",
                "error": f"Request timed out after {self.config.timeout}s.",
                "status_code": None
            }
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "message_id": None,
                "status": "network_error",
                "error": f"Network exception: {type(e).__name__}",
                "status_code": None
            }

    def send_interactive_list_message(
        self,
        recipient_phone: Optional[str] = None,
        body_text: str = "",
        button_text: str = "Select Option",
        sections: Optional[list] = None,
        header_text: Optional[str] = None,
        footer_text: Optional[str] = None,
        bsuid: Optional[str] = None,
        scoped_user_id: Optional[str] = None,
        fallback_text: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Sends a WhatsApp Interactive List message (popup menu) to a recipient.
        Complies with Meta WhatsApp Cloud API specifications:
          - button_text: max 20 chars
          - header_text: max 60 chars (optional)
          - footer_text: max 60 chars (optional)
          - body_text: max 1024 chars (required)
          - sections: max 10 sections, max 10 rows total across all sections
          - row.id: max 200 chars
          - row.title: max 24 chars (required)
          - row.description: max 72 chars (optional)
        If the interactive send fails (e.g. unsupported client or validation),
        automatically falls back to sending a standard text message.
        """
        # Strict Feature Flag Check
        if not self.config.outbound_enabled:
            return {
                "success": False,
                "message_id": None,
                "status": "outbound_disabled",
                "error": "Outbound WhatsApp messaging is disabled by system configuration.",
                "status_code": None
            }

        if not self.is_configured:
            return {
                "success": False,
                "message_id": None,
                "status": "unconfigured",
                "error": "Kapso credentials not configured.",
                "status_code": None
            }

        scoped_target = (bsuid or scoped_user_id or "").strip()
        phone_target = (recipient_phone or "").strip()

        if phone_target:
            norm_phone = normalize_phone_number(phone_target)
            if not norm_phone:
                if not scoped_target:
                    return {
                        "success": False,
                        "message_id": None,
                        "status": "invalid_phone",
                        "error": "Invalid recipient phone number format.",
                        "status_code": 400
                    }
                clean_recipient = scoped_target
            else:
                clean_recipient = norm_phone.lstrip("+")
        elif scoped_target:
            clean_recipient = scoped_target
        else:
            return {
                "success": False,
                "message_id": None,
                "status": "invalid_phone",
                "error": "No recipient phone number or BSUID specified.",
                "status_code": 400
            }

        # Format and validate sections and rows to comply with Meta limits
        formatted_sections = []
        total_rows = 0
        for s in (sections or []):
            if not isinstance(s, dict):
                continue
            s_title = str(s.get("title", "Options")).strip()[:24]
            s_rows = []
            for r in s.get("rows", []):
                if total_rows >= 10:
                    break
                if not isinstance(r, dict):
                    continue
                r_id = str(r.get("id", f"row_{total_rows}")).strip()[:200]
                r_title = str(r.get("title", "")).strip()[:24]
                if not r_title:
                    continue
                row_dict = {"id": r_id, "title": r_title}
                if r.get("description"):
                    row_dict["description"] = str(r.get("description")).strip()[:72]
                s_rows.append(row_dict)
                total_rows += 1
            if s_rows:
                formatted_sections.append({"title": s_title, "rows": s_rows})
            if total_rows >= 10:
                break

        if not formatted_sections:
            return self.send_text_message(
                recipient_phone=recipient_phone,
                text_body=fallback_text or body_text,
                bsuid=bsuid,
                scoped_user_id=scoped_user_id
            )

        interactive_payload: Dict[str, Any] = {
            "type": "list",
            "body": {"text": (body_text or "Please choose an option:")[:1024]},
            "action": {
                "button": (button_text or "Select Option")[:20],
                "sections": formatted_sections
            }
        }
        if header_text:
            interactive_payload["header"] = {
                "type": "text",
                "text": str(header_text)[:60]
            }
        if footer_text:
            interactive_payload["footer"] = {
                "text": str(footer_text)[:60]
            }

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_recipient,
            "type": "interactive",
            "interactive": interactive_payload
        }

        endpoint = f"{self.config.base_url}/meta/whatsapp/{self.config.api_version}/{self.config.phone_number_id}/messages"
        try:
            response = requests.post(
                endpoint,
                headers=self._get_headers(),
                json=payload,
                timeout=self.config.timeout
            )
            if response.status_code in (200, 201, 202):
                try:
                    data = response.json() if response.text else {}
                except Exception:
                    data = {}
                messages = data.get("messages", [])
                msg_id = messages[0].get("id") if messages else None
                return {
                    "success": True,
                    "message_id": msg_id,
                    "status": "accepted",
                    "error": None,
                    "status_code": response.status_code
                }
            else:
                logger.warning(
                    sanitize_log_message(
                        f"Interactive list message rejected (HTTP {response.status_code}): {response.text}. "
                        "Attempting plain text fallback."
                    )
                )
                if fallback_text:
                    return self.send_text_message(
                        recipient_phone=recipient_phone,
                        text_body=fallback_text,
                        bsuid=bsuid,
                        scoped_user_id=scoped_user_id
                    )
                return {
                    "success": False,
                    "message_id": None,
                    "status": "rejected" if response.status_code == 400 else "failed",
                    "error": f"Interactive message rejected (HTTP {response.status_code}).",
                    "status_code": response.status_code
                }
        except requests.exceptions.Timeout:
            if fallback_text:
                return self.send_text_message(recipient_phone=recipient_phone, text_body=fallback_text, bsuid=bsuid, scoped_user_id=scoped_user_id)
            return {
                "success": False,
                "message_id": None,
                "status": "timeout",
                "error": f"Request timed out after {self.config.timeout}s.",
                "status_code": None
            }
        except requests.exceptions.RequestException as e:
            if fallback_text:
                return self.send_text_message(recipient_phone=recipient_phone, text_body=fallback_text, bsuid=bsuid, scoped_user_id=scoped_user_id)
            return {
                "success": False,
                "message_id": None,
                "status": "network_error",
                "error": f"Network exception: {type(e).__name__}",
                "status_code": None
            }

    def send_interactive_button_message(
        self,
        recipient_phone: Optional[str] = None,
        body_text: str = "",
        buttons: Optional[list] = None,
        header_text: Optional[str] = None,
        footer_text: Optional[str] = None,
        bsuid: Optional[str] = None,
        scoped_user_id: Optional[str] = None,
        fallback_text: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Sends a WhatsApp Interactive Button message (up to 3 quick action buttons).
        Meta constraints:
          - buttons: max 3 buttons
          - button title: max 20 chars
          - body_text: max 1024 chars
        """
        if not self.config.outbound_enabled:
            return {
                "success": False,
                "message_id": None,
                "status": "outbound_disabled",
                "error": "Outbound WhatsApp messaging is disabled by system configuration.",
                "status_code": None
            }

        if not self.is_configured:
            return {
                "success": False,
                "message_id": None,
                "status": "unconfigured",
                "error": "Kapso credentials not configured.",
                "status_code": None
            }

        scoped_target = (bsuid or scoped_user_id or "").strip()
        phone_target = (recipient_phone or "").strip()

        if phone_target:
            norm_phone = normalize_phone_number(phone_target)
            if not norm_phone:
                if not scoped_target:
                    return {
                        "success": False,
                        "message_id": None,
                        "status": "invalid_phone",
                        "error": "Invalid recipient phone number format.",
                        "status_code": 400
                    }
                clean_recipient = scoped_target
            else:
                clean_recipient = norm_phone.lstrip("+")
        elif scoped_target:
            clean_recipient = scoped_target
        else:
            return {
                "success": False,
                "message_id": None,
                "status": "invalid_phone",
                "error": "No recipient phone number or BSUID specified.",
                "status_code": 400
            }

        formatted_buttons = []
        for idx, btn in enumerate(buttons or []):
            if idx >= 3:
                break
            if not isinstance(btn, dict):
                continue
            b_id = str(btn.get("id", f"btn_{idx}")).strip()[:256]
            b_title = str(btn.get("title", f"Option {idx+1}")).strip()[:20]
            formatted_buttons.append({
                "type": "reply",
                "reply": {
                    "id": b_id,
                    "title": b_title
                }
            })

        if not formatted_buttons:
            return self.send_text_message(
                recipient_phone=recipient_phone,
                text_body=fallback_text or body_text,
                bsuid=bsuid,
                scoped_user_id=scoped_user_id
            )

        interactive_payload: Dict[str, Any] = {
            "type": "button",
            "body": {"text": (body_text or "Please choose an option:")[:1024]},
            "action": {
                "buttons": formatted_buttons
            }
        }
        if header_text:
            interactive_payload["header"] = {
                "type": "text",
                "text": str(header_text)[:60]
            }
        if footer_text:
            interactive_payload["footer"] = {
                "text": str(footer_text)[:60]
            }

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_recipient,
            "type": "interactive",
            "interactive": interactive_payload
        }

        endpoint = f"{self.config.base_url}/meta/whatsapp/{self.config.api_version}/{self.config.phone_number_id}/messages"
        try:
            response = requests.post(
                endpoint,
                headers=self._get_headers(),
                json=payload,
                timeout=self.config.timeout
            )
            if response.status_code in (200, 201, 202):
                try:
                    data = response.json() if response.text else {}
                except Exception:
                    data = {}
                messages = data.get("messages", [])
                msg_id = messages[0].get("id") if messages else None
                return {
                    "success": True,
                    "message_id": msg_id,
                    "status": "accepted",
                    "error": None,
                    "status_code": response.status_code
                }
            else:
                if fallback_text:
                    return self.send_text_message(recipient_phone=recipient_phone, text_body=fallback_text, bsuid=bsuid, scoped_user_id=scoped_user_id)
                return {
                    "success": False,
                    "message_id": None,
                    "status": "rejected" if response.status_code == 400 else "failed",
                    "error": f"Interactive button rejected (HTTP {response.status_code}).",
                    "status_code": response.status_code
                }
        except requests.exceptions.RequestException as e:
            if fallback_text:
                return self.send_text_message(recipient_phone=recipient_phone, text_body=fallback_text, bsuid=bsuid, scoped_user_id=scoped_user_id)
            return {
                "success": False,
                "message_id": None,
                "status": "network_error",
                "error": f"Network exception: {type(e).__name__}",
                "status_code": None
            }

    def verify_webhook_signature(self, raw_body: bytes, signature: str, secret: Optional[str] = None) -> bool:
        """
        Validates Kapso HMAC-SHA256 signature for incoming POST webhooks.
        Official doc: X-Webhook-Signature header containing HMAC-SHA256 hex digest of raw request body.
        
        Args:
          raw_body (bytes): Exact unmodified raw request body bytes.
          signature (str): Header value from X-Webhook-Signature.
          secret (str, optional): Webhook secret (defaults to config.webhook_secret).
          
        Returns:
          bool: True if signature is valid and authentic, False otherwise.
        """
        sec = (secret or self.config.webhook_secret or "").encode('utf-8')
        if not sec:
            logger.warning("Kapso webhook signature check failed: KAPSO_WEBHOOK_SECRET is not configured.")
            return False

        if not signature or not isinstance(raw_body, (bytes, bytearray)):
            return False

        sig_clean = signature.strip()
        # Handle optional 'sha256=' prefix if present in the header
        if sig_clean.startswith("sha256="):
            sig_clean = sig_clean[7:].strip()

        try:
            expected = hmac.new(sec, raw_body, hashlib.sha256).hexdigest()
            # Timing-safe comparison to prevent side-channel attacks
            return hmac.compare_digest(expected.lower(), sig_clean.lower())
        except Exception:
            return False

    def verify_webhook_handshake(self, mode: str, verify_token: str, challenge: str) -> Tuple[bool, Optional[str]]:
        """
        Validates inbound WhatsApp webhook subscription handshake (GET challenge).
        """
        if not self.config.webhook_verify_token:
            logger.warning("Kapso webhook handshake rejected: KAPSO_WEBHOOK_VERIFY_TOKEN is not configured.")
            return False, None

        if mode == "subscribe" and verify_token == self.config.webhook_verify_token:
            return True, challenge
        return False, None

    @staticmethod
    def extract_idempotency_key(headers: Optional[Dict[str, str]]) -> Optional[str]:
        """Extracts X-Idempotency-Key header for webhook deduplication."""
        if not headers:
            return None
        for k, v in headers.items():
            if str(k).lower() in ("x-idempotency-key", "idempotency-key"):
                return str(v).strip()
        return None

    @staticmethod
    def extract_webhook_event(headers: Optional[Dict[str, str]], payload: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Extracts event name from X-Webhook-Event header or payload body."""
        if headers:
            for k, v in headers.items():
                if str(k).lower() in ("x-webhook-event", "webhook-event"):
                    return str(v).strip()
        if payload and isinstance(payload, dict):
            return payload.get("event") or payload.get("event_type")
        return None

    @staticmethod
    def extract_payload_version(headers: Optional[Dict[str, str]], payload: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Extracts payload version from X-Webhook-Payload-Version header or payload body."""
        if headers:
            for k, v in headers.items():
                if str(k).lower() in ("x-webhook-payload-version", "webhook-payload-version"):
                    return str(v).strip()
        if payload and isinstance(payload, dict):
            return payload.get("payload_version") or payload.get("version")
        return None
