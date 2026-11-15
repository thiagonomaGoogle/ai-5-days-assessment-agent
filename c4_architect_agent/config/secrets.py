"""Secure secret management backed by Google Cloud Secret Manager (IAM/ADC).

Never hardcodes API keys, webhook tokens, or database credentials in code.
All secrets are resolved dynamically via Application Default Credentials (ADC)
or Workload Identity Federation (WIF) from Google Cloud Secret Manager.
"""

from __future__ import annotations

import functools
import os
import time
from typing import Any, Callable

try:
    from google.cloud import secretmanager  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - optional in offline unit-test runners
    secretmanager = None  # type: ignore[assignment]


class SecretResolutionError(RuntimeError):
    """Raised when a required secret cannot be retrieved safely."""


class SecretManagerResolver:
    """Resolves secrets securely from Google Cloud Secret Manager with TTL caching.

    Attributes:
        project_id: Target GCP project hosting Secret Manager resources.
        ttl_seconds: In-memory cache TTL in seconds to minimize RPC latency.
    """

    def __init__(
        self,
        project_id: str | None = None,
        *,
        ttl_seconds: int = 300,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.project_id = (
            project_id if project_id is not None else os.getenv("GOOGLE_CLOUD_PROJECT", "")
        )
        self.ttl_seconds = ttl_seconds
        self._client_factory = client_factory
        self._client: Any | None = None
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        if secretmanager is None:
            raise SecretResolutionError(
                "google-cloud-secret-manager is not installed in the active environment."
            )
        self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def get_secret(
        self,
        secret_id: str,
        *,
        version: str = "latest",
        env_fallback_var: str | None = None,
        required: bool = False,
    ) -> str | None:
        """Fetches a secret payload from Google Cloud Secret Manager.

        Args:
            secret_id: Secret Manager identifier (not the secret value itself).
            version: Secret version to access (defaults to 'latest').
            env_fallback_var: Optional Cloud Run mounted secret env var name
                injected via Secret Manager volume/env binding in Terraform.
            required: If True, raises SecretResolutionError when unavailable.

        Returns:
            Decoded secret string, or None when optional and unconfigured.
        """
        if not secret_id or not secret_id.strip():
            raise ValueError("secret_id must be a non-empty Secret Manager resource ID.")

        cache_key = (secret_id, version)
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None and cached[1] > now:
            return cached[0]

        # 1. Prefer Google Cloud Secret Manager API via ADC / Workload Identity
        if self.project_id:
            resource_name = (
                f"projects/{self.project_id}/secrets/{secret_id}/versions/{version}"
            )
            try:
                client = self._get_client()
                response = client.access_secret_version(request={"name": resource_name})
                payload: str = response.payload.data.decode("utf-8").strip()
                self._cache[cache_key] = (payload, now + self.ttl_seconds)
                return payload
            except Exception as exc:
                if env_fallback_var is None and required:
                    raise SecretResolutionError(
                        f"Failed to resolve secret '{secret_id}' from Secret Manager "
                        f"in project '{self.project_id}': {type(exc).__name__}"
                    ) from exc

        # 2. Support Cloud Run native Secret Manager env injection (configured in Terraform)
        if env_fallback_var:
            mounted_val = os.getenv(env_fallback_var)
            if mounted_val:
                self._cache[cache_key] = (mounted_val, now + self.ttl_seconds)
                return mounted_val

        if required:
            raise SecretResolutionError(
                f"Required secret '{secret_id}' could not be resolved from "
                "Google Cloud Secret Manager or Cloud Run secret mount."
            )
        return None

    def clear_cache(self) -> None:
        """Evicts all cached secret values from process memory."""
        self._cache.clear()


@functools.lru_cache(maxsize=1)
def get_secret_resolver() -> SecretManagerResolver:
    """Returns a singleton SecretManagerResolver bound to the current GCP project."""
    return SecretManagerResolver()
