"""Shared Pydantic field validators used across route models.

Keeping validators here (rather than inline per-model) ensures:
* One place to tighten or relax the budget as load data arrives.
* Consistent 422 error messages across all dict-typed fields.
* No accidental drift between e.g. ``task.metadata`` and
  ``communication.message`` size limits.

Usage
-----
In a Pydantic model::

    from pydantic import field_validator
    from acn.core.validators import check_dict_size_64k, check_dict_size_256k

    class MyModel(BaseModel):
        metadata: dict = {}

        @field_validator("metadata")
        @classmethod
        def _metadata_size(cls, v: dict) -> dict:
            return check_dict_size_64k("metadata", v)

Or use the ``make_dict_size_validator`` factory for custom caps::

    from acn.core.validators import make_dict_size_validator

    _check = make_dict_size_validator(max_bytes=16_384)  # 16 KB

    @field_validator("small_field")
    @classmethod
    def _small_field_size(cls, v: dict) -> dict:
        return _check("small_field", v)
"""

from __future__ import annotations

import json
from typing import Any

# Canonical caps (bytes, post-JSON-serialisation).  Adjust here only.
_64K = 64 * 1024   # 65 536 bytes  — default for most metadata / card fields
_256K = 256 * 1024  # 262 144 bytes — extended cap for message payloads (communication)


def make_dict_size_validator(max_bytes: int):
    """Return a validator function that raises ``ValueError`` when the
    serialised size of *value* exceeds *max_bytes*.

    The returned function has the signature ``(field_name, value) → value``
    and is designed to be called from a Pydantic ``@field_validator``
    method.

    We serialise via ``json.dumps`` (compact, no extra whitespace) so
    the measured size matches what would be written to the DB / Redis
    by the service layer.  Unicode characters are preserved as-is
    (``ensure_ascii=False``) so an emoji-heavy dict that looks small
    on screen is measured at its actual UTF-8 size.
    """

    def _check(field_name: str, value: Any) -> Any:
        if not isinstance(value, dict):
            # Non-dict values: let Pydantic's own type checks handle it.
            return value
        try:
            blob = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            # Non-JSON-serialisable dicts are rejected later by the service;
            # don't raise here so the error site stays in one place.
            return value
        size = len(blob.encode("utf-8"))
        if size > max_bytes:
            raise ValueError(
                f"{field_name} is too large ({size} bytes); "
                f"maximum allowed is {max_bytes} bytes. "
                "Split the payload or reduce the field size."
            )
        return value

    return _check


# Pre-built validators for the two canonical caps.
check_dict_size_64k = make_dict_size_validator(_64K)
check_dict_size_256k = make_dict_size_validator(_256K)
