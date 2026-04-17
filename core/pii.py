"""
core/pii.py — Output sanitization for Maximo response data.

Recursively walks dict/list structures and masks any value whose key matches
the configured PII field set, before the response leaves the trust boundary
toward an LLM caller.

Field names are matched case-insensitively against the *leaf* attribute name
(the part after the final '.' in dotted Maximo attribute names like
LABOR.EMPLOYEEEMAIL). Internal bookkeeping keys starting with '_' are
preserved untouched (e.g. '_duration_ms').

Wired into core.maximo_client._request via mask_response(); disable by setting
PII_MASK_ENABLED=False. Extend the field list via PII_MASK_FIELDS.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional


DEFAULT_PII_FIELDS: frozenset = frozenset({
    # Email
    "email", "emailaddress", "email_address",
    "primaryemail", "personalemail", "workemail", "useremail",
    "employeeemail", "contactemail",
    # Phone
    "phone", "phonenum", "phonenumber",
    "primaryphone", "homephone", "cellphone", "mobilephone",
    "workphone", "cellnum", "personalphone",
    "employeephone", "contactphone",
    # Government IDs
    "ssn", "socialsecurity", "socialsecuritynumber",
    "taxid", "nationalid",
    "passport", "passportnumber",
    "driverslicense", "dlnumber",
    # Financial
    "salary", "hourlyrate", "wagerate", "payrate", "wage",
    "bankaccount", "accountnumber",
    "creditcard", "creditcardnum", "cardnumber",
    # Personal demographics
    "birthdate", "dateofbirth", "dob",
    "gender", "maritalstatus",
    # Personal address (Maximo prefixes residential addresses with "personal*"
    # and uses "homeaddress"; site/asset addresses use plain city/state and
    # are intentionally NOT masked).
    "homeaddress", "personalstreet", "personalcity",
    "personalstate", "personalzip", "personalcountry",
})

DEFAULT_MASK_VALUE = "***MASKED***"
_MAX_DEPTH = 32


def _leaf_name(key: str) -> str:
    """Return the lowercased leaf attribute name (LABOR.EMPLOYEEEMAIL → employeeemail)."""
    if "." in key:
        key = key.rsplit(".", 1)[1]
    return key.lower()


def mask_pii(
    value: Any,
    fields: Optional[Iterable[str]] = None,
    mask: str = DEFAULT_MASK_VALUE,
) -> Any:
    """
    Return a deep copy of value with leaf values under PII keys replaced by
    `mask`. Lists and nested dicts are recursed; private keys starting with
    '_' are passed through untouched. Pure: never mutates input.
    """
    field_set = (
        DEFAULT_PII_FIELDS
        if fields is None
        else frozenset(f.lower() for f in fields)
    )
    return _walk(value, field_set, mask, 0)


def _walk(value: Any, field_set: frozenset, mask: str, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if isinstance(k, str) and k.startswith("_"):
                out[k] = v
                continue
            if isinstance(k, str) and _leaf_name(k) in field_set:
                # Mask non-empty values opaquely; leave None/empty alone so
                # downstream existence checks behave predictably.
                out[k] = mask if v not in (None, "", [], {}) else v
                continue
            out[k] = _walk(v, field_set, mask, depth + 1)
        return out
    if isinstance(value, list):
        return [_walk(item, field_set, mask, depth + 1) for item in value]
    return value


def get_active_field_set() -> frozenset:
    """Resolve DEFAULT_PII_FIELDS ∪ settings.PII_MASK_FIELDS."""
    from core.settings import get_settings
    s = get_settings()
    extras = tuple(getattr(s, "PII_MASK_FIELDS", ()) or ())
    if not extras:
        return DEFAULT_PII_FIELDS
    return frozenset({*DEFAULT_PII_FIELDS, *(f.lower() for f in extras)})


def mask_response(value: Any) -> Any:
    """
    Apply PII masking using settings. Returns value unchanged when
    PII_MASK_ENABLED=False, so callers can invoke unconditionally.
    """
    from core.settings import get_settings
    s = get_settings()
    if not getattr(s, "PII_MASK_ENABLED", True):
        return value
    return mask_pii(
        value,
        fields=get_active_field_set(),
        mask=getattr(s, "PII_MASK_VALUE", DEFAULT_MASK_VALUE),
    )
