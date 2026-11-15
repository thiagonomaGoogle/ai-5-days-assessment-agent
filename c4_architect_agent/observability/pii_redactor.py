"""Active PII and secret redaction pipeline for logs, prompts, and persistent memory.

Combines Google Cloud Sensitive Data Protection (Cloud DLP API) when enabled in GCP
with deterministic local scrubbing rules so sensitive identifiers (emails, phone
numbers, SSNs, Brazilian CPFs, credit cards, IPv4s, and API/OAuth tokens) are
redacted before hitting logs or persistent storage.
"""

from __future__ import annotations

import functools
import re
from typing import Any

from ..config.settings import get_settings

try:
    from google.cloud import dlp_v2  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - optional when running offline tests
    dlp_v2 = None  # type: ignore[assignment]


_SENSITIVE_KEY_NAMES = frozenset({
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "private_key",
    "client_secret",
    "webhook_token",
    "ssn",
    "cpf",
    "credit_card",
})

# Ordered deterministic scrubbing patterns (most specific tokens first)
_REDACTION_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "API_TOKEN",
        re.compile(
            r"\b(?:AIza[0-9A-Za-z\-_]{20,}|ya29\.[0-9A-Za-z\-_]+|gh[pousr]_[0-9A-Za-z]{20,}|sk-[0-9A-Za-z]{20,})\b"
        ),
        "[REDACTED_API_TOKEN]",
    ),
    (
        "JWT_TOKEN",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "[REDACTED_JWT]",
    ),
    (
        "BEARER_HEADER",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*"),
        "Bearer [REDACTED_TOKEN]",
    ),
    (
        "EMAIL_ADDRESS",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
    (
        "US_SSN",
        re.compile(r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
        "[REDACTED_SSN]",
    ),
    (
        "BR_CPF",
        re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"),
        "[REDACTED_CPF]",
    ),
    (
        "CREDIT_CARD",
        re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
        "[REDACTED_CARD]",
    ),
    (
        "PHONE_NUMBER",
        re.compile(
            r"(?<!\d)(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,3}\)[\s.\-]?|\d{2,3}[\s.\-])\d{4,5}[\s.\-]\d{4}(?!\d)"
        ),
        "[REDACTED_PHONE]",
    ),
    (
        "IPV4_ADDRESS",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
        ),
        "[REDACTED_IPV4]",
    ),
)


def _passes_luhn(digits: str) -> bool:
    """Checks whether a digit sequence is a plausible payment card via Luhn checksum."""
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    reverse_digits = digits[::-1]
    for idx, ch in enumerate(reverse_digits):
        n = ord(ch) - 48
        if idx % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


class PiiRedactionPipeline:
    """Scrubs PII and credentials from text and nested JSON structures.

    Uses Google Cloud Sensitive Data Protection (Cloud DLP) when enabled and
    always applies deterministic local regex/checksum scrubbing as a zero-latency
    defense-in-depth layer.
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        enable_cloud_dlp: bool | None = None,
        dlp_client: Any | None = None,
    ) -> None:
        settings = get_settings()
        self.project_id = project_id if project_id is not None else settings.gcp_project_id
        self.enable_cloud_dlp = (
            enable_cloud_dlp if enable_cloud_dlp is not None else settings.enable_cloud_dlp
        )
        self._dlp_client = dlp_client
        self.redaction_counter: dict[str, int] = {}

    def _get_dlp_client(self) -> Any | None:
        if self._dlp_client is not None:
            return self._dlp_client
        if not self.enable_cloud_dlp or not self.project_id or dlp_v2 is None:
            return None
        self._dlp_client = dlp_v2.DlpServiceClient()
        return self._dlp_client

    def _deidentify_with_cloud_dlp(self, text: str) -> str:
        """Invokes Google Cloud DLP deidentify_content when configured."""
        client = self._get_dlp_client()
        if client is None or not text.strip():
            return text
        try:
            parent = f"projects/{self.project_id}/locations/global"
            inspect_config = {
                "info_types": [
                    {"name": "EMAIL_ADDRESS"},
                    {"name": "PHONE_NUMBER"},
                    {"name": "US_SOCIAL_SECURITY_NUMBER"},
                    {"name": "BRAZIL_CPF_NUMBER"},
                    {"name": "CREDIT_CARD_NUMBER"},
                    {"name": "AUTH_TOKEN"},
                ]
            }
            deidentify_config = {
                "info_type_transformations": {
                    "transformations": [
                        {
                            "primitive_transformation": {
                                "replace_with_info_type_config": {}
                            }
                        }
                    ]
                }
            }
            response = client.deidentify_content(
                request={
                    "parent": parent,
                    "inspect_config": inspect_config,
                    "deidentify_config": deidentify_config,
                    "item": {"value": text},
                }
            )
            return str(response.item.value)
        except Exception:
            # Fail safe to deterministic local scrubbing below
            return text

    def redact_text(self, text: str) -> str:
        """Scrubs sensitive tokens and PII from a string."""
        if not text:
            return text

        scrubbed = self._deidentify_with_cloud_dlp(text)

        for label, pattern, replacement in _REDACTION_PATTERNS:
            if label == "CREDIT_CARD":
                def _replace_card(match: re.Match[str]) -> str:
                    raw = match.group(0)
                    digits = "".join(ch for ch in raw if ch.isdigit())
                    if _passes_luhn(digits):
                        self.redaction_counter[label] = self.redaction_counter.get(label, 0) + 1
                        return replacement
                    return raw

                scrubbed = pattern.sub(_replace_card, scrubbed)
            else:
                scrubbed, count = pattern.subn(replacement, scrubbed)
                if count > 0:
                    self.redaction_counter[label] = self.redaction_counter.get(label, 0) + count

        return scrubbed

    def redact_payload(self, payload: Any) -> Any:
        """Recursively scrubs strings and sensitive keys in dicts, lists, and tuples."""
        if payload is None or isinstance(payload, (bool, int, float)):
            return payload
        if isinstance(payload, str):
            return self.redact_text(payload)
        if isinstance(payload, dict):
            sanitized: dict[str, Any] = {}
            for key, val in payload.items():
                key_str = str(key)
                if key_str.lower() in _SENSITIVE_KEY_NAMES:
                    self.redaction_counter["SENSITIVE_KEY"] = (
                        self.redaction_counter.get("SENSITIVE_KEY", 0) + 1
                    )
                    sanitized[key_str] = "[REDACTED_SECRET_FIELD]"
                else:
                    sanitized[key_str] = self.redact_payload(val)
            return sanitized
        if isinstance(payload, list):
            return [self.redact_payload(item) for item in payload]
        if isinstance(payload, tuple):
            return tuple(self.redact_payload(item) for item in payload)
        return self.redact_text(str(payload))


@functools.lru_cache(maxsize=1)
def get_pii_redactor() -> PiiRedactionPipeline:
    """Returns singleton PII redaction pipeline."""
    return PiiRedactionPipeline()
