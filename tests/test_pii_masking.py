"""
tests/test_pii_masking.py — Coverage for the output PII masking layer that
sits at the maximo_client._request boundary (G3).

A regression here means PII (emails, phone numbers, SSNs, salary, etc.) leaks
from Maximo into the LLM prompt window. These tests pin the masking contract:
which fields are masked, which keys pass through, and that the toggle and
extras work as documented in .env.example.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from core import pii as pii_module
from core.pii import (
    DEFAULT_MASK_VALUE,
    DEFAULT_PII_FIELDS,
    mask_pii,
    mask_response,
)


# ── mask_pii: pure function, no settings dependency ─────────────────────────

class TestMaskPiiDefaults:
    def test_email_field_masked(self):
        out = mask_pii({"email": "user@example.com"})
        assert out == {"email": DEFAULT_MASK_VALUE}

    def test_phone_field_masked(self):
        out = mask_pii({"phonenum": "555-1234"})
        assert out["phonenum"] == DEFAULT_MASK_VALUE

    def test_ssn_field_masked(self):
        out = mask_pii({"ssn": "123-45-6789"})
        assert out["ssn"] == DEFAULT_MASK_VALUE

    def test_salary_field_masked(self):
        out = mask_pii({"salary": 95000})
        assert out["salary"] == DEFAULT_MASK_VALUE

    def test_birthdate_field_masked(self):
        out = mask_pii({"dob": "1980-01-01"})
        assert out["dob"] == DEFAULT_MASK_VALUE

    def test_personal_address_masked(self):
        out = mask_pii({
            "homeaddress": "1 Main St",
            "personalcity": "Boston",
            "personalstate": "MA",
            "personalzip": "02101",
        })
        for k in ("homeaddress", "personalcity", "personalstate", "personalzip"):
            assert out[k] == DEFAULT_MASK_VALUE

    @pytest.mark.parametrize("field", [
        "siteid", "wonum", "status", "description", "assetnum", "priority",
        "reportdate", "location", "city", "state",  # site/asset address NOT masked
    ])
    def test_business_fields_not_masked(self, field):
        out = mask_pii({field: "VALUE"})
        assert out[field] == "VALUE"


class TestMaskPiiCaseInsensitive:
    @pytest.mark.parametrize("key", ["EMAIL", "Email", "eMaIl"])
    def test_key_case_insensitive(self, key):
        out = mask_pii({key: "user@example.com"})
        assert out[key] == DEFAULT_MASK_VALUE


class TestMaskPiiDottedNames:
    """Maximo attribute names are often dotted (LABOR.EMPLOYEEEMAIL).
    The leaf-name match must apply."""

    def test_dotted_email_masked(self):
        out = mask_pii({"LABOR.EMPLOYEEEMAIL": "x@y.com"})
        assert out["LABOR.EMPLOYEEEMAIL"] == DEFAULT_MASK_VALUE

    def test_dotted_primary_email_masked(self):
        out = mask_pii({"PERSON.PRIMARYEMAIL": "x@y.com"})
        assert out["PERSON.PRIMARYEMAIL"] == DEFAULT_MASK_VALUE

    def test_dotted_business_field_not_masked(self):
        out = mask_pii({"WORKORDER.SITEID": "BEDFORD"})
        assert out["WORKORDER.SITEID"] == "BEDFORD"

    def test_deeply_dotted_only_leaf_considered(self):
        out = mask_pii({"a.b.c.email": "x@y.com"})
        assert out["a.b.c.email"] == DEFAULT_MASK_VALUE


class TestMaskPiiPrivateKeys:
    """Bookkeeping keys starting with '_' must pass through untouched, even if
    the leaf name happens to look like a PII field."""

    def test_underscore_duration_passes_through(self):
        out = mask_pii({"_duration_ms": 42})
        assert out == {"_duration_ms": 42}

    def test_underscore_email_key_passes_through(self):
        # Edge case: if a tool stamps `_email_count`, we don't redact metadata.
        out = mask_pii({"_email": 5})
        assert out == {"_email": 5}


class TestMaskPiiEmptyValues:
    """Existence checks downstream depend on None/empty preservation."""

    @pytest.mark.parametrize("empty", [None, "", [], {}])
    def test_empty_pii_preserved(self, empty):
        out = mask_pii({"email": empty})
        assert out["email"] == empty


class TestMaskPiiNested:
    def test_nested_dict_masked(self):
        out = mask_pii({"person": {"email": "x@y.com", "name": "Alice"}})
        assert out == {"person": {"email": DEFAULT_MASK_VALUE, "name": "Alice"}}

    def test_list_of_dicts_masked(self):
        out = mask_pii({
            "members": [
                {"email": "a@x.com", "role": "tech"},
                {"email": "b@x.com", "role": "lead"},
            ]
        })
        assert out["members"][0]["email"] == DEFAULT_MASK_VALUE
        assert out["members"][1]["email"] == DEFAULT_MASK_VALUE
        assert out["members"][0]["role"] == "tech"

    def test_oslc_member_envelope_masked(self):
        # Mirrors the Maximo OSLC response shape.
        payload = {
            "member": [
                {
                    "wonum": "1001",
                    "siteid": "BEDFORD",
                    "LABOR.EMPLOYEEEMAIL": "tech@x.com",
                    "salary": 90000,
                }
            ],
            "_duration_ms": 12,
        }
        out = mask_pii(payload)
        assert out["member"][0]["wonum"] == "1001"
        assert out["member"][0]["siteid"] == "BEDFORD"
        assert out["member"][0]["LABOR.EMPLOYEEEMAIL"] == DEFAULT_MASK_VALUE
        assert out["member"][0]["salary"] == DEFAULT_MASK_VALUE
        assert out["_duration_ms"] == 12


class TestMaskPiiPure:
    def test_input_not_mutated(self):
        original = {"email": "x@y.com", "name": "Alice"}
        snapshot = dict(original)
        _ = mask_pii(original)
        assert original == snapshot

    def test_nested_input_not_mutated(self):
        original = {"members": [{"email": "x@y.com"}]}
        out = mask_pii(original)
        assert original["members"][0]["email"] == "x@y.com"
        assert out["members"][0]["email"] == DEFAULT_MASK_VALUE


class TestMaskPiiCustomFields:
    def test_extra_field_masked(self):
        out = mask_pii({"badge_id": "B-99"}, fields=["badge_id"])
        assert out["badge_id"] == DEFAULT_MASK_VALUE

    def test_default_field_not_masked_when_overridden(self):
        # Passing an explicit field set replaces the default.
        out = mask_pii({"email": "x@y.com"}, fields=["badge_id"])
        assert out["email"] == "x@y.com"

    def test_custom_mask_value(self):
        out = mask_pii({"email": "x@y.com"}, mask="<redacted>")
        assert out["email"] == "<redacted>"


class TestMaskPiiDepthGuard:
    def test_deeply_nested_does_not_crash(self):
        # Build a 100-deep nested dict; depth guard returns the value at limit.
        node: dict = {"email": "leak@x.com"}
        for _ in range(100):
            node = {"child": node}
        # Should not raise and should not loop forever.
        out = mask_pii(node)
        assert isinstance(out, dict)


class TestMaskPiiPassThrough:
    @pytest.mark.parametrize("value", [None, "string", 42, 3.14, True, False])
    def test_scalars_returned_unchanged(self, value):
        assert mask_pii(value) is value

    def test_top_level_list_recursed(self):
        out = mask_pii([{"email": "x@y.com"}, {"siteid": "B"}])
        assert out[0]["email"] == DEFAULT_MASK_VALUE
        assert out[1]["siteid"] == "B"


# ── mask_response: settings-driven wrapper ──────────────────────────────────

@pytest.fixture
def fake_settings(monkeypatch):
    """Inject a stub `core.settings` module so mask_response's lazy import
    resolves to a configurable namespace. Avoids loading the real module
    (which depends on pydantic_settings) and any singleton caching."""
    state = SimpleNamespace(
        PII_MASK_ENABLED=True,
        PII_MASK_FIELDS=[],
        PII_MASK_VALUE=DEFAULT_MASK_VALUE,
    )
    fake = ModuleType("core.settings")
    fake.get_settings = lambda: state  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.settings", fake)
    return state


class TestMaskResponse:
    def test_default_enabled_masks(self, fake_settings):
        out = mask_response({"email": "x@y.com", "wonum": "1"})
        assert out["email"] == DEFAULT_MASK_VALUE
        assert out["wonum"] == "1"

    def test_disabled_is_noop(self, fake_settings):
        fake_settings.PII_MASK_ENABLED = False
        payload = {"email": "x@y.com", "salary": 90000}
        out = mask_response(payload)
        assert out == payload

    def test_extra_fields_added_to_default_set(self, fake_settings):
        fake_settings.PII_MASK_FIELDS = ["badge_id"]
        out = mask_response({
            "email": "x@y.com",   # default
            "badge_id": "B-99",   # extra
            "wonum": "1",         # neither
        })
        assert out["email"] == DEFAULT_MASK_VALUE
        assert out["badge_id"] == DEFAULT_MASK_VALUE
        assert out["wonum"] == "1"

    def test_custom_mask_value_honored(self, fake_settings):
        fake_settings.PII_MASK_VALUE = "<redacted>"
        out = mask_response({"email": "x@y.com"})
        assert out["email"] == "<redacted>"

    def test_extras_case_insensitive(self, fake_settings):
        fake_settings.PII_MASK_FIELDS = ["BadgeID"]
        out = mask_response({"badgeid": "B-99"})
        assert out["badgeid"] == DEFAULT_MASK_VALUE


class TestDefaultFieldSet:
    """Sanity: the documented PII categories are present in the default set."""

    @pytest.mark.parametrize("field", [
        "email", "phonenum", "ssn", "taxid", "passport", "driverslicense",
        "salary", "wagerate", "bankaccount", "creditcard",
        "dob", "birthdate", "gender",
        "homeaddress", "personalcity",
    ])
    def test_field_in_default_set(self, field):
        assert field in DEFAULT_PII_FIELDS

    @pytest.mark.parametrize("field", [
        "siteid", "wonum", "status", "description", "assetnum", "city", "state",
    ])
    def test_business_field_not_in_default_set(self, field):
        assert field not in DEFAULT_PII_FIELDS


# ── Smoke: module-level constants exposed for downstream consumers ──────────

def test_default_mask_value_constant():
    assert pii_module.DEFAULT_MASK_VALUE == "***MASKED***"


def test_default_pii_fields_is_frozen():
    assert isinstance(DEFAULT_PII_FIELDS, frozenset)
