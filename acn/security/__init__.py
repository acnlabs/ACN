"""Security utilities for ACN.

Exposes:

* SSRF / DNS-rebinding defences (``ssrf``) — shared across route
  handlers and outbound clients.
* Outbound-error sanitisation (``error_sanitizer``) — maps caught
  exceptions to safe, white-listed category strings before they reach
  remote callers (security audit M12).
"""

from .error_sanitizer import safe_error_payload, safe_external_error
from .ssrf import (
    SSRFViolation,
    safe_resolve_target,
    validate_endpoint_url,
)
from .tls_check import check_tls_config

__all__ = [
    "SSRFViolation",
    "check_tls_config",
    "safe_error_payload",
    "safe_external_error",
    "safe_resolve_target",
    "validate_endpoint_url",
]
