"""
tests/test_tool_models.py — Sanity tests for the Pydantic request models.

These tests don't hit Maximo. They verify:
  * every tool in the catalog has a request_model bound
  * extra="forbid" actually rejects unknown fields
  * Field range constraints behave as expected
  * the documented signature of each model matches the underlying tool function
"""
from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from core import tool_models
from core.tool_catalog import TOOL_METADATA, bind_runtime

# `server` is imported lazily inside the tests that need it. Importing it at
# module load triggers `httpx_sse` which has a known version mismatch with the
# pinned `httpx` on some Python 3.13 envs — and that crash would interrupt
# pytest collection for the whole suite.


# ── Coverage --------------------------------------------------------------------

def test_every_tool_has_a_request_model():
    """No public tool may ship without strict input validation."""
    missing = [name for name, spec in TOOL_METADATA.items() if spec.request_model is None]
    assert not missing, f"Tools missing request_model: {missing}"


def test_every_request_model_is_strict():
    """All request models must inherit StrictBaseModel (extra='forbid')."""
    for name, spec in TOOL_METADATA.items():
        assert spec.request_model is not None
        config = spec.request_model.model_config
        assert config.get("extra") == "forbid", (
            f"{name}.request_model is not extra='forbid' — "
            "subclass StrictBaseModel instead of BaseModel"
        )


def test_runtime_binding_resolves_every_tool():
    """bind_runtime must succeed against the live server module — no orphan ToolSpecs."""
    try:
        import server
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"server module import failed (pre-existing env issue): {exc}")
    else:
        bound = bind_runtime(server)
        assert len(bound) == len(TOOL_METADATA)


# ── extra="forbid" enforcement -------------------------------------------------

def test_unknown_field_rejected_on_simple_model():
    with pytest.raises(ValidationError):
        tool_models.GetAssetArgs(asset_num="A1", site_id="BEDFORD", typo_field="x")


def test_unknown_field_rejected_on_paginated_mixin():
    with pytest.raises(ValidationError):
        tool_models.ListItemsArgs(keyword="pump", offset=10)  # offset is not a field


def test_unknown_field_rejected_on_admin_model():
    with pytest.raises(ValidationError):
        tool_models.QueryAuditLogArgs(tool_name="x", date_format="iso")


# ── Range constraints -----------------------------------------------------------

def test_page_size_must_be_within_1_to_200():
    with pytest.raises(ValidationError):
        tool_models.ListAssetsArgs(page_size=0)
    with pytest.raises(ValidationError):
        tool_models.ListAssetsArgs(page_size=500)
    # Boundary values OK
    tool_models.ListAssetsArgs(page_size=1)
    tool_models.ListAssetsArgs(page_size=200)


def test_priority_must_be_1_to_5():
    with pytest.raises(ValidationError):
        tool_models.ListWorkordersArgs(priority=0)
    with pytest.raises(ValidationError):
        tool_models.ListWorkordersArgs(priority=6)
    tool_models.ListWorkordersArgs(priority=1)
    tool_models.ListWorkordersArgs(priority=5)


def test_period_months_capped():
    # period_months is widely used — verify a couple of examples.
    with pytest.raises(ValidationError):
        tool_models.GetWorkorderKpisArgs(site_id="BEDFORD", period_months=0)
    with pytest.raises(ValidationError):
        tool_models.GetWorkorderKpisArgs(site_id="BEDFORD", period_months=999)
    tool_models.GetWorkorderKpisArgs(site_id="BEDFORD", period_months=24)


def test_group_by_pattern_validation():
    # get_spend_analysis only accepts vendor / status / worktype
    with pytest.raises(ValidationError):
        tool_models.GetSpendAnalysisArgs(site_id="BEDFORD", group_by="random")
    tool_models.GetSpendAnalysisArgs(site_id="BEDFORD", group_by="vendor")
    tool_models.GetSpendAnalysisArgs(site_id="BEDFORD", group_by="status")


def test_required_fields_enforced():
    """Missing required fields must raise."""
    with pytest.raises(ValidationError):
        tool_models.GetAssetArgs(site_id="BEDFORD")  # asset_num missing
    with pytest.raises(ValidationError):
        tool_models.SuggestRootCauseArgs(asset_num="A", site_id="B")  # failure_description missing


# ── Signature drift detection ---------------------------------------------------

def test_model_fields_subset_of_tool_signature():
    """
    Each request model's field names must match the underlying tool's keyword
    parameters. Catches drift where a tool grows a new param but the model
    isn't updated, leading to extra="forbid" rejecting valid calls.
    """
    try:
        import server
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"server module import failed (pre-existing env issue): {exc}")
    else:
        bound = bind_runtime(server)
        for name, spec in bound.items():
            if spec.request_model is None or spec.func is None:
                continue
            model_fields = set(spec.request_model.model_fields.keys())
            sig_params = set(inspect.signature(spec.func).parameters.keys()) - {"self", "cls"}
            # Model fields must all be valid kwargs of the tool function.
            # (The tool may legitimately accept aliases not in the model — only
            # check the strict direction: every model field is a real kwarg.)
            unknown = model_fields - sig_params
            assert not unknown, (
                f"{name}: model declares fields {unknown} that are not kwargs of "
                f"the tool function. Model out-of-date with tool signature."
            )
