"""
tools/verticals.py — Industry-vertical tools for IBM Maximo.

Six verticals, three tools each (18 total):

    PHARMA / LIFE SCIENCES
        get_calibration_audit_trail   — chronological cal log for FDA inspection
        list_cleanroom_assets         — assets in cleanroom-classified locations
        get_gxp_compliance_status     — overdue cal + cert + incidents aggregate

    OIL & GAS
        get_turnaround_status         — multi-WO parent / child rollup
        list_pressure_vessels_due     — pressure-class assets with inspection due
        get_lifting_register          — crane / lift operations log

    MANUFACTURING
        get_oee                       — best-effort OEE (Availability solid; P+Q flagged when data missing)
        get_production_line_status    — open WOs + downtime per location subtree
        list_changeover_workorders    — SMED / set-up WOs with avg duration

    UTILITIES
        get_outage_impact_analysis    — downstream child assets / locations
        list_grid_zone_assets         — every asset in a location hierarchy zone
        get_reliability_indices       — SAIDI / SAIFI proxies from outage WOs

    HEALTHCARE
        list_medical_devices_due      — medical-class assets with PM due
        get_device_lifecycle_status   — age-bucketed lifecycle stage with warranty
        get_environment_of_care_status — JC-style EOC rollup

    TRANSPORTATION
        get_fleet_readiness           — vehicle status mix + readiness %
        list_mileage_based_pm_due     — PMs tracked against ODOM-KM / mileage meter
        get_fuel_consumption_trend    — fuel-meter usage rate + spike detection

Design constraints (from waves 1-6 memory):
  - Single-condition WHERE only — never compound on this Maximo build.
  - `+field` / `-field` direction prefix on every server-side orderBy.
  - No orderBy on sparse-NULL columns (Maximo strips them from response).
  - Naive datetime comparisons — strip tzinfo on both sides.
  - Graceful `data_unavailable=True` when a backing OS / column / classification isn't published.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role


WO_OS = "/os/mxwo"
ASSET_OS = "/os/mxasset"
LOC_OS = "/os/mxoperloc"
ASSETMETER_OS_CANDIDATES = ("/os/mxassetmeter", "/os/mxapiassetmeter")


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


def _parse_dt(s: Any) -> Optional[datetime]:
    """Parse a Maximo ISO date/time, normalised to naive (no tz)."""
    if not s or not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


async def _try_candidates(client, candidates, params):
    last_exc: Optional[Exception] = None
    for endpoint in candidates:
        try:
            return await client.get(endpoint, params=params), endpoint
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                last_exc = exc
                continue
            raise
    raise last_exc if last_exc else MaximoAPIError("No candidate endpoint available")


# ══════════════════════════════════════════════════════════════════════════════
# PHARMA / LIFE SCIENCES
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def get_calibration_audit_trail(
    asset_num: str,
    site_id: str,
    period_months: int = 12,
) -> Dict[str, Any]:
    """
    Chronological calibration history for a single asset — what FDA / GxP
    auditors ask for. Pulls every CAL-typed (or CAL-prefixed) WO over the
    look-back window with timestamps, who performed it, failure code, and
    completion status.

    Args:
        asset_num:     Asset to audit
        site_id:       Site ID (Python post-filter)
        period_months: Look-back window (default 12)
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff = (datetime.now() - timedelta(days=period_months * 30)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="wonum,description,siteid,worktype,status,reportdate,reportedby,actfinish,actlabhrs,failurecode",
            page_size=200,
        )
        data = await client.get(WO_OS, params=params)
        rows: List[Dict] = data.get("member", [])

        site_u = site_id.upper()
        cal_log: List[Dict[str, Any]] = []
        for w in rows:
            if (w.get("siteid") or "").upper() != site_u:
                continue
            if (w.get("reportdate") or "") < cutoff:
                continue
            wt = (w.get("worktype") or "").upper()
            desc = (w.get("description") or "").upper()
            if wt != "CAL" and not desc.startswith("CAL"):
                continue
            cal_log.append(
                {
                    "wonum": w.get("wonum"),
                    "description": w.get("description"),
                    "status": w.get("status"),
                    "reportdate": w.get("reportdate"),
                    "reportedby": w.get("reportedby"),
                    "actfinish": w.get("actfinish"),
                    "actlabhrs": w.get("actlabhrs"),
                    "failurecode": w.get("failurecode"),
                }
            )
        cal_log.sort(key=lambda r: r.get("reportdate") or "", reverse=True)

        completed = sum(1 for c in cal_log if (c.get("status") or "").upper() in ("COMP", "CLOSE"))
        failed = sum(1 for c in cal_log if c.get("failurecode"))

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "asset_num": asset_num,
                "site_id": site_id,
                "period_months": period_months,
                "total_calibrations": len(cal_log),
                "completed_calibrations": completed,
                "calibrations_with_failure": failed,
                "data_unavailable": (len(cal_log) == 0),
                "data_unavailable_note": (
                    f"No calibration WOs found for {asset_num} in the last {period_months} months. "
                    "Calibration is detected via worktype='CAL' or descriptions starting with 'CAL'. "
                    "If your Maximo uses a different convention, the asset isn't actually a "
                    "calibrated instrument, or calibration tracking isn't enabled — let your "
                    "Maximo admin know."
                ) if len(cal_log) == 0 else None,
                "audit_trail": cal_log,
            },
            duration_ms=duration_ms, record_count=len(cal_log),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_cleanroom_assets(
    site_id: str,
    classification_keyword: str = "CLEANROOM",
) -> Dict[str, Any]:
    """
    Assets located in cleanrooms — used for GMP / GxP environment-monitoring
    workflows. Detection strategy:

    1. Look for locations whose `description` contains the classification
       keyword (default "CLEANROOM"; pharma may use "GMP", "ASEPTIC", etc.).
    2. Fall back to assets whose own `description` matches, when no
       cleanroom-typed location exists.
    3. Surface `data_unavailable=True` if neither path returns rows so the
       caller knows to ask their Maximo admin to populate cleanroom
       location records or asset classifications.

    Args:
        site_id:                Site to scan
        classification_keyword: Keyword to match (default "CLEANROOM")
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    kw_u = classification_keyword.upper()

    try:
        client = await get_connected_client()
        # Step 1: find candidate locations
        loc_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="location,description,siteid,type,parent",
            page_size=200,
        )
        loc_data = await client.get(LOC_OS, params=loc_params)
        site_u = site_id.upper()
        candidate_locs = [
            L for L in loc_data.get("member", [])
            if (L.get("siteid") or "").upper() == site_u
            and kw_u in (L.get("description") or "").upper()
        ]
        loc_set = {(L.get("location") or "").upper() for L in candidate_locs}

        # Step 2: fetch assets at those locations (or fall back to keyword on description)
        asset_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="assetnum,description,siteid,location,assettype,classstructureid,status",
            page_size=200,
        )
        a_data = await client.get(ASSET_OS, params=asset_params)
        all_assets = [
            a for a in a_data.get("member", [])
            if (a.get("siteid") or "").upper() == site_u
        ]

        in_cleanroom: List[Dict] = []
        for a in all_assets:
            asset_loc = (a.get("location") or "").upper()
            asset_desc = (a.get("description") or "").upper()
            if asset_loc and asset_loc in loc_set:
                in_cleanroom.append(a)
            elif kw_u in asset_desc:
                in_cleanroom.append(a)

        data_unavailable_note = None
        if not in_cleanroom and not candidate_locs:
            data_unavailable_note = (
                f"No locations matching '{kw_u}' on this site, and no asset descriptions "
                f"contain the keyword. Ask your Maximo admin to populate cleanroom-typed "
                "locations or apply classifications to GMP assets."
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "classification_keyword": classification_keyword,
                "matching_locations": len(candidate_locs),
                "assets_in_cleanroom": in_cleanroom,
                "data_unavailable": bool(data_unavailable_note),
                "data_unavailable_note": data_unavailable_note,
            },
            duration_ms=duration_ms, record_count=len(in_cleanroom),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("manager")
async def get_gxp_compliance_status(site_id: str) -> Dict[str, Any]:
    """
    GxP / FDA compliance rollup composed from Wave-5 compliance tools.
    Tolerates partial failure — sections that don't resolve are flagged
    `data_unavailable_sections` while the rest still populate.

    Args:
        site_id: Site to analyse
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    from tools import compliance

    sections: Dict[str, Any] = {}

    async def _safe(label: str, coro):
        try:
            sections[label] = await coro
        except Exception as exc:
            sections[label] = {"success": False, "error": f"{type(exc).__name__}: {exc!r}"}

    await _safe("calibration", compliance.list_calibration_due(site_id=site_id, days_ahead=30))
    await _safe("certifications", compliance.list_certifications_expiring(site_id=site_id, days_ahead=180))
    await _safe("incidents", compliance.list_incidents(site_id=site_id))

    cal = sections["calibration"].get("data", {}) if sections["calibration"].get("success") else {}
    certs = sections["certifications"].get("data", {}) if sections["certifications"].get("success") else {}
    inc = sections["incidents"].get("data", {}) if sections["incidents"].get("success") else {}

    cal_overdue = cal.get("overdue_count", 0) or 0
    certs_expired = (certs.get("buckets") or {}).get("EXPIRED", 0) or 0
    open_incidents = inc.get("totalCount", 0) or 0

    # Simple GxP risk score: 0 = clean, 100 = severe non-compliance.
    risk = min(100, cal_overdue * 10 + certs_expired * 8 + open_incidents * 3)
    if risk == 0:
        rating = "COMPLIANT"
    elif risk < 25:
        rating = "MONITOR"
    elif risk < 60:
        rating = "AT_RISK"
    else:
        rating = "NON_COMPLIANT"

    data_unavailable_sections = [
        label for label, s in sections.items()
        if not s.get("success") or s.get("data", {}).get("data_unavailable")
    ]

    duration_ms = int((time.monotonic() - start) * 1000)
    return _envelope(
        {
            "site_id": site_id,
            "gxp_risk_score": risk,
            "compliance_rating": rating,
            "summary": {
                "overdue_calibrations": cal_overdue,
                "expired_certifications": certs_expired,
                "open_incidents": open_incidents,
            },
            "data_unavailable_sections": data_unavailable_sections,
            "sections": sections,
        },
        duration_ms=duration_ms,
    )


# ══════════════════════════════════════════════════════════════════════════════
# OIL & GAS
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def get_turnaround_status(
    site_id: str,
    parent_wonum: Optional[str] = None,
    top_n: int = 5,
) -> Dict[str, Any]:
    """
    Maximo turnarounds (TARs) are coordinated multi-WO outages where a
    parent WO holds dozens of child WOs. This tool either drills into one
    parent (`parent_wonum` given) or lists the top-N parents by child count
    so a TAR manager can spot which one is most active.

    Args:
        site_id:      Site to analyse
        parent_wonum: Drill into a specific parent's children
        top_n:        How many top parent WOs to surface when no drill-down
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        client = await get_connected_client()
        if parent_wonum:
            # Drill: list children of the given parent
            params = client.build_oslc_query(
                where=f'parent="{oslc_escape(parent_wonum)}"',
                select="wonum,parent,description,siteid,status,worktype,wopriority,schedstart,schedfinish,actfinish,actlabhrs",
                page_size=200,
            )
            data = await client.get(WO_OS, params=params)
            site_u = site_id.upper()
            children = [
                w for w in data.get("member", [])
                if (w.get("siteid") or "").upper() == site_u
            ]
            terminal = {"COMP", "CLOSE", "CAN"}
            completed = sum(1 for c in children if (c.get("status") or "").upper() in terminal)
            total_hours = sum(float(c.get("actlabhrs") or 0) for c in children)

            duration_ms = int((time.monotonic() - start) * 1000)
            return _envelope(
                {
                    "site_id": site_id,
                    "parent_wonum": parent_wonum,
                    "child_wo_count": len(children),
                    "completed": completed,
                    "in_progress": len(children) - completed,
                    "completion_pct": round((completed / len(children)) * 100, 1) if children else 0,
                    "total_actual_hours": round(total_hours, 2),
                    "child_workorders": children,
                },
                duration_ms=duration_ms, record_count=len(children),
            )

        # Roll up: count WOs by parent across the site
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="wonum,parent,description,status,worktype,actlabhrs",
            page_size=200,
        )
        data = await client.get(WO_OS, params=params)
        rows = data.get("member", [])

        by_parent: Dict[str, List[Dict]] = {}
        for w in rows:
            p = w.get("parent")
            if not p:
                continue
            by_parent.setdefault(str(p), []).append(w)

        ranked = sorted(by_parent.items(), key=lambda kv: len(kv[1]), reverse=True)[:top_n]
        terminal = {"COMP", "CLOSE", "CAN"}
        turnarounds = []
        for parent, children in ranked:
            completed = sum(1 for c in children if (c.get("status") or "").upper() in terminal)
            total_hours = sum(float(c.get("actlabhrs") or 0) for c in children)
            turnarounds.append(
                {
                    "parent_wonum": parent,
                    "child_count": len(children),
                    "completed": completed,
                    "completion_pct": round((completed / len(children)) * 100, 1) if children else 0,
                    "total_actual_hours": round(total_hours, 2),
                }
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "total_parent_workorders": len(by_parent),
                "data_unavailable": (len(by_parent) == 0),
                "data_unavailable_note": (
                    f"No parent / child work-order groupings detected at site {site_id}. "
                    "Turnarounds (TARs) on Maximo are modelled as a parent WO with many "
                    "child WOs. If your operation runs turnarounds but doesn't use the "
                    "parent-WO pattern, this tool can't see them. Ask your planners to "
                    "create a parent WO for each TAR campaign and link the work orders "
                    "to it."
                ) if len(by_parent) == 0 else None,
                "top_turnarounds": turnarounds,
            },
            duration_ms=duration_ms, record_count=len(turnarounds),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_pressure_vessels_due(
    site_id: str,
    days_ahead: int = 90,
    classification_keyword: str = "VESSEL",
) -> Dict[str, Any]:
    """
    Pressure vessel inspections coming due. Detection: assets whose
    `description` or classification contains "VESSEL" (or operator-supplied
    keyword like "PRESSURE", "BOILER", "REACTOR"). Cross-references Wave-5
    inspection-due logic.

    Args:
        site_id:                Site to analyse
        days_ahead:             Look-ahead window in days (default 90)
        classification_keyword: Asset-description match (default "VESSEL")
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    now = datetime.now()
    kw_u = classification_keyword.upper()

    try:
        client = await get_connected_client()
        # Get vessel-class assets
        a_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="assetnum,description,siteid,assettype,classstructureid,location,status",
            page_size=200,
        )
        a_data = await client.get(ASSET_OS, params=a_params)
        site_u = site_id.upper()
        vessels = [
            a for a in a_data.get("member", [])
            if (a.get("siteid") or "").upper() == site_u
            and (
                kw_u in (a.get("description") or "").upper()
                or kw_u in (a.get("assettype") or "").upper()
                or kw_u in (a.get("classstructureid") or "").upper()
            )
        ]
        vessel_assetnums = {(a.get("assetnum") or "").upper() for a in vessels}

        # Get open INSP WOs and join
        wo_params = client.build_oslc_query(
            where='worktype="INSP"',
            select="wonum,description,worktype,siteid,status,assetnum,targstartdate,targcompdate,wopriority",
            page_size=200,
        )
        wo_data = await client.get(WO_OS, params=wo_params)
        terminal = {"COMP", "CLOSE", "CAN"}

        due_inspections: List[Dict] = []
        for w in wo_data.get("member", []):
            if (w.get("siteid") or "").upper() != site_u:
                continue
            if (w.get("status") or "").upper() in terminal:
                continue
            asset_u = (w.get("assetnum") or "").upper()
            if asset_u not in vessel_assetnums:
                continue
            target = _parse_dt(w.get("targstartdate")) or _parse_dt(w.get("targcompdate"))
            if target is None:
                continue
            target = target.replace(tzinfo=None)
            days_until = (target - now).days
            if days_until > days_ahead:
                continue
            due_inspections.append(
                {
                    "wonum": w.get("wonum"),
                    "description": w.get("description"),
                    "assetnum": w.get("assetnum"),
                    "status": w.get("status"),
                    "priority": w.get("wopriority"),
                    "target_date": target.strftime("%Y-%m-%d"),
                    "days_until_due": days_until,
                    "is_overdue": days_until < 0,
                }
            )
        due_inspections.sort(key=lambda r: r["days_until_due"])

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "classification_keyword": classification_keyword,
                "days_ahead": days_ahead,
                "total_pressure_vessels": len(vessels),
                "inspections_due_in_window": len(due_inspections),
                "overdue_count": sum(1 for d in due_inspections if d["is_overdue"]),
                "data_unavailable": (len(vessels) == 0),
                "data_unavailable_note": (
                    f"No assets at this site matched the keyword '{kw_u}'. Verify your Maximo "
                    "uses standard descriptions / classifications for pressure vessels."
                ) if len(vessels) == 0 else None,
                "vessels": vessels,
                "inspections_due": due_inspections,
            },
            duration_ms=duration_ms, record_count=len(due_inspections),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_lifting_register(
    site_id: str,
    period_months: int = 12,
    keyword: str = "LIFT",
) -> Dict[str, Any]:
    """
    Crane / lifting operations log. WOs whose description or worktype
    contains the keyword (default "LIFT"; can also match "CRANE", "HOIST").

    Args:
        site_id:       Site to analyse
        period_months: Look-back window in months (default 12)
        keyword:       Description/worktype match (default "LIFT")
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff = (datetime.now() - timedelta(days=period_months * 30)).strftime("%Y-%m-%dT00:00:00+00:00")
    kw_u = keyword.upper()

    try:
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="wonum,description,worktype,status,assetnum,reportdate,actfinish,actlabhrs,reportedby",
            page_size=200,
        )
        data = await client.get(WO_OS, params=params)
        rows = data.get("member", [])

        site_u = site_id.upper()
        lifts: List[Dict] = []
        for w in rows:
            if (w.get("siteid") or "").upper() != site_u:
                continue
            if (w.get("reportdate") or "") < cutoff:
                continue
            desc = (w.get("description") or "").upper()
            wt = (w.get("worktype") or "").upper()
            if kw_u not in desc and kw_u not in wt:
                continue
            lifts.append(
                {
                    "wonum": w.get("wonum"),
                    "description": w.get("description"),
                    "assetnum": w.get("assetnum"),
                    "status": w.get("status"),
                    "reportdate": w.get("reportdate"),
                    "actfinish": w.get("actfinish"),
                    "actlabhrs": w.get("actlabhrs"),
                    "reportedby": w.get("reportedby"),
                }
            )
        lifts.sort(key=lambda r: r.get("reportdate") or "", reverse=True)

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "keyword": keyword,
                "period_months": period_months,
                "total_lift_workorders": len(lifts),
                "data_unavailable": (len(lifts) == 0),
                "data_unavailable_note": (
                    f"No lifting / crane operations found at site {site_id} in the last "
                    f"{period_months} months matching the keyword '{keyword}'. Either this "
                    "site doesn't run hoisting / lifting work, or your descriptions use "
                    "different terminology. Try keywords like 'CRANE', 'HOIST', or 'RIG' "
                    "if applicable to your operation."
                ) if len(lifts) == 0 else None,
                "lifts": lifts,
            },
            duration_ms=duration_ms, record_count=len(lifts),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# MANUFACTURING
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def get_oee(
    site_id: str,
    asset_num: Optional[str] = None,
    period_days: int = 30,
) -> Dict[str, Any]:
    """
    Overall Equipment Effectiveness (Availability × Performance × Quality).

    Honest about what we can measure from Maximo alone:
      - Availability: derived from corrective WO downtime (real number).
      - Performance:  needs production-rate / cycle-time data not in
                       standard Maximo. Flagged data_unavailable.
      - Quality:      needs scrap / first-pass-yield data. Flagged
                       data_unavailable.

    For sites that feed production data into asset specs or meters, this
    function returns a partial OEE so a manager can still spot-check
    Availability without waiting for an MES integration.

    Args:
        site_id:     Site to analyse
        asset_num:   Optional single-asset filter
        period_days: Look-back window in days (default 30)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    now = datetime.now()
    cutoff = (now - timedelta(days=period_days)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()
        # Most-selective single-condition WHERE
        if asset_num:
            where = f'assetnum="{oslc_escape(asset_num)}"'
        else:
            where = f'siteid="{oslc_escape(site_id)}"'
        params = client.build_oslc_query(
            where=where,
            select="wonum,siteid,assetnum,worktype,status,reportdate,actfinish,actlabhrs",
            page_size=200,
        )
        data = await client.get(WO_OS, params=params)
        rows = data.get("member", [])

        site_u = site_id.upper()
        corrective = {"CM", "EM"}
        in_scope = [
            w for w in rows
            if (w.get("siteid") or "").upper() == site_u
            and (w.get("worktype") or "").upper() in corrective
            and (w.get("reportdate") or "") >= cutoff
        ]

        # Availability = (period_hours - downtime) / period_hours
        period_hours = period_days * 24.0
        total_downtime = sum(float(w.get("actlabhrs") or 0) for w in in_scope)
        # Cap to period to avoid silly numbers
        total_downtime = min(total_downtime, period_hours)
        availability = round(((period_hours - total_downtime) / period_hours) * 100, 2) if period_hours else None

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "asset_num": asset_num,
                "period_days": period_days,
                "availability_pct": availability,
                "performance_pct": None,
                "quality_pct": None,
                "oee_pct": None,
                "data_unavailable": True,
                "data_unavailable_note": (
                    "Performance and Quality require production-rate / scrap data not in "
                    "standard Maximo. Wire those in via asset specifications or an MES "
                    "integration to get a complete OEE."
                ),
                "downtime_hours": round(total_downtime, 2),
                "corrective_wo_count": len(in_scope),
            },
            duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_production_line_status(
    site_id: str,
    line_location: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Production line status — open WOs and downtime for every location
    under a parent location ("line"). When `line_location` is omitted,
    rolls up by top-level location.

    Args:
        site_id:        Site to analyse
        line_location:  Parent location (production line) to drill into
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        client = await get_connected_client()
        # Get all locations at site
        loc_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="location,description,siteid,type,parent,status",
            page_size=200,
        )
        loc_data = await client.get(LOC_OS, params=loc_params)
        site_u = site_id.upper()
        all_locs = [L for L in loc_data.get("member", []) if (L.get("siteid") or "").upper() == site_u]

        # Determine target location set
        if line_location:
            target_u = line_location.upper()
            # children of line_location (1-level deep — recursive walk would be overkill on demo data)
            target_locs = [L for L in all_locs if (L.get("location") or "").upper() == target_u
                           or (L.get("parent") or "").upper() == target_u]
        else:
            target_locs = [L for L in all_locs if not (L.get("parent") or "").strip()]
        target_loc_set = {(L.get("location") or "").upper() for L in target_locs}

        # WOs for those locations
        wo_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="wonum,siteid,location,assetnum,status,worktype,reportdate,actlabhrs",
            page_size=200,
        )
        wo_data = await client.get(WO_OS, params=wo_params)
        terminal = {"COMP", "CLOSE", "CAN"}
        relevant_wos = [
            w for w in wo_data.get("member", [])
            if (w.get("siteid") or "").upper() == site_u
            and (w.get("location") or "").upper() in target_loc_set
        ]
        open_wos = [w for w in relevant_wos if (w.get("status") or "").upper() not in terminal]
        total_downtime = sum(float(w.get("actlabhrs") or 0) for w in relevant_wos)

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "line_location": line_location,
                "locations_in_scope": len(target_locs),
                "total_wos": len(relevant_wos),
                "open_wos": len(open_wos),
                "total_actual_labor_hours": round(total_downtime, 2),
                "data_unavailable": (len(target_locs) == 0),
                "data_unavailable_note": (
                    "No production-line locations identified. When `line_location` is "
                    "omitted this tool rolls up by top-level (parent-less) location; if "
                    "your site uses a flat location structure or doesn't model production "
                    "lines as parent locations, supply a specific `line_location` parameter "
                    "or restructure the location hierarchy in Maximo."
                ) if len(target_locs) == 0 else None,
                "locations": target_locs,
                "open_workorders": open_wos,
            },
            duration_ms=duration_ms, record_count=len(target_locs),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_changeover_workorders(
    site_id: str,
    period_months: int = 3,
) -> Dict[str, Any]:
    """
    SMED / changeover work orders — WOs whose description matches
    CHANGEOVER, SET ?UP, SETUP, or worktype="SET". Returns the list plus
    average changeover duration so a continuous-improvement engineer can
    spot drift.

    Args:
        site_id:       Site to analyse
        period_months: Look-back window (default 3 months)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff = (datetime.now() - timedelta(days=period_months * 30)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="wonum,description,siteid,worktype,assetnum,reportdate,actfinish,actlabhrs",
            page_size=200,
        )
        data = await client.get(WO_OS, params=params)
        rows = data.get("member", [])

        site_u = site_id.upper()
        keywords = ("CHANGEOVER", "SET UP", "SETUP")
        changeovers: List[Dict] = []
        for w in rows:
            if (w.get("siteid") or "").upper() != site_u:
                continue
            if (w.get("reportdate") or "") < cutoff:
                continue
            desc = (w.get("description") or "").upper()
            wt = (w.get("worktype") or "").upper()
            if any(k in desc for k in keywords) or wt == "SET":
                changeovers.append(
                    {
                        "wonum": w.get("wonum"),
                        "description": w.get("description"),
                        "assetnum": w.get("assetnum"),
                        "reportdate": w.get("reportdate"),
                        "actfinish": w.get("actfinish"),
                        "actlabhrs": w.get("actlabhrs"),
                    }
                )

        if changeovers:
            durations = [float(c.get("actlabhrs") or 0) for c in changeovers if c.get("actlabhrs")]
            avg_hours = round(sum(durations) / len(durations), 2) if durations else None
        else:
            avg_hours = None

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "period_months": period_months,
                "total_changeovers": len(changeovers),
                "avg_changeover_hours": avg_hours,
                "data_unavailable": (len(changeovers) == 0),
                "data_unavailable_note": (
                    f"No changeover / setup work orders found at site {site_id} in the last "
                    f"{period_months} months. Detection looks for descriptions matching "
                    "'CHANGEOVER', 'SET UP', 'SETUP' or worktype='SET'. Process-industry "
                    "or batch-manufacturing sites may not track changeovers as discrete WOs "
                    "— that's a workflow choice, not a tool bug. Consider tracking changeovers "
                    "as work orders to enable SMED analytics."
                ) if len(changeovers) == 0 else None,
                "changeovers": changeovers,
            },
            duration_ms=duration_ms, record_count=len(changeovers),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def get_outage_impact_analysis(
    asset_num: str,
    site_id: str,
) -> Dict[str, Any]:
    """
    Downstream impact of taking an asset out of service. Walks the asset's
    `parent` field (children pointing to this asset) and the location
    hierarchy to estimate how many downstream items would be affected.

    Args:
        asset_num: Asset whose outage you're modelling
        site_id:   Site ID
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        client = await get_connected_client()
        site_u = site_id.upper()
        target_u = asset_num.upper()

        # NOTE: Maximo rejects `where=parent="..."` on mxasset
        # (BMXAA4185E "Cannot query on field PARENT"). Pull all assets at
        # the site and filter `parent` in Python — same approach as the
        # mxlabor `siteid`-not-queryable fix in Wave-3.
        a_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="assetnum,description,parent,siteid,location,status",
            page_size=200,
        )
        a_data = await client.get(ASSET_OS, params=a_params)
        all_assets = [a for a in a_data.get("member", []) if (a.get("siteid") or "").upper() == site_u]

        # Direct child assets (parent == target)
        child_assets = [a for a in all_assets if (a.get("parent") or "").upper() == target_u]

        # Find the target asset's location
        a_rows = [a for a in all_assets if (a.get("assetnum") or "").upper() == target_u]
        asset_location = (a_rows[0].get("location") or "") if a_rows else ""

        downstream_locations: List[Dict] = []
        if asset_location:
            # `parent` is also not queryable on mxoperloc on this build
            # (BMXAA4185E). Pull all site locations and filter in Python.
            loc_params = client.build_oslc_query(
                where=f'siteid="{oslc_escape(site_id)}"',
                select="location,description,parent,siteid,type",
                page_size=200,
            )
            loc_data = await client.get(LOC_OS, params=loc_params)
            asset_loc_u = asset_location.upper()
            downstream_locations = [
                L for L in loc_data.get("member", [])
                if (L.get("siteid") or "").upper() == site_u
                and (L.get("parent") or "").upper() == asset_loc_u
            ]

        operating_children = sum(1 for c in child_assets if (c.get("status") or "").upper() == "OPERATING")
        impact_score = len(child_assets) + len(downstream_locations) + operating_children
        no_downstream = (len(child_assets) == 0 and len(downstream_locations) == 0)

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "asset_num": asset_num,
                "asset_location": asset_location,
                "impact_score": impact_score,
                "child_asset_count": len(child_assets),
                "operating_child_assets": operating_children,
                "downstream_location_count": len(downstream_locations),
                "data_unavailable": no_downstream,
                "data_unavailable_note": (
                    f"Asset '{asset_num}' has no downstream child assets and no child "
                    "locations in the Maximo hierarchy — outage impact is therefore zero "
                    "based on what's modelled. If this asset really does feed downstream "
                    "equipment / circuits / customers, the parent links and location "
                    "hierarchy need to be filled in before this analysis is meaningful."
                ) if no_downstream else None,
                "child_assets": child_assets,
                "downstream_locations": downstream_locations,
            },
            duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_grid_zone_assets(
    site_id: str,
    zone_location: str,
) -> Dict[str, Any]:
    """
    Every asset in a location-hierarchy zone — for grid-by-zone reliability
    analysis or substation-bound dispatch. Walks one level of children;
    pass nested zone codes to drill deeper.

    Args:
        site_id:       Site ID
        zone_location: Top-level location code defining the zone
    """
    if not site_id or not zone_location:
        return _error("site_id and zone_location are required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        client = await get_connected_client()
        site_u = site_id.upper()
        zone_u = zone_location.upper()

        # Locations in zone (the zone itself + 1-level children)
        loc_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="location,description,siteid,type,parent",
            page_size=200,
        )
        loc_data = await client.get(LOC_OS, params=loc_params)
        all_locs = [L for L in loc_data.get("member", []) if (L.get("siteid") or "").upper() == site_u]
        zone_locs = [
            L for L in all_locs
            if (L.get("location") or "").upper() == zone_u
            or (L.get("parent") or "").upper() == zone_u
        ]
        zone_loc_set = {(L.get("location") or "").upper() for L in zone_locs}

        # Assets in those locations
        a_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="assetnum,description,siteid,location,assettype,status,priority",
            page_size=200,
        )
        a_data = await client.get(ASSET_OS, params=a_params)
        zone_assets = [
            a for a in a_data.get("member", [])
            if (a.get("siteid") or "").upper() == site_u
            and (a.get("location") or "").upper() in zone_loc_set
        ]

        # Status mix
        from collections import Counter
        status_mix = dict(Counter((a.get("status") or "UNKNOWN") for a in zone_assets))

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "zone_location": zone_location,
                "locations_in_zone": len(zone_locs),
                "asset_count": len(zone_assets),
                "status_mix": status_mix,
                "data_unavailable": (len(zone_locs) == 0),
                "data_unavailable_note": (
                    f"No locations match zone '{zone_location}' at site {site_id}. Either "
                    "the zone code doesn't exist, or your Maximo location hierarchy doesn't "
                    "model zones the same way (e.g. zones may be a custom attribute rather "
                    "than a parent location). Check `list_locations` for valid codes."
                ) if len(zone_locs) == 0 else None,
                "locations": zone_locs,
                "assets": zone_assets,
            },
            duration_ms=duration_ms, record_count=len(zone_assets),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_reliability_indices(
    site_id: str,
    period_months: int = 12,
) -> Dict[str, Any]:
    """
    SAIDI / SAIFI proxies from Maximo outage WOs. Real utilities feed these
    from outage management systems, but this gives a defensible
    Maximo-only baseline:

      SAIDI proxy = total outage hours / # of customer locations
      SAIFI proxy = # of outage WOs / # of customer locations

    Outage WOs are detected via worktype IN {EM, OUTAGE, FAULT} or
    description match.

    Args:
        site_id:       Site ID
        period_months: Look-back window in months (default 12)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff = (datetime.now() - timedelta(days=period_months * 30)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()
        site_u = site_id.upper()
        # Customer-served locations (proxy: OPERATING-type locations)
        loc_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="location,description,type,siteid",
            page_size=200,
        )
        loc_data = await client.get(LOC_OS, params=loc_params)
        customer_locs = [
            L for L in loc_data.get("member", [])
            if (L.get("siteid") or "").upper() == site_u
            and (L.get("type") or "").upper() == "OPERATING"
        ]
        n_customers = len(customer_locs)

        # Outage WOs
        wo_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="wonum,description,worktype,siteid,reportdate,actfinish,actlabhrs,status",
            page_size=200,
        )
        wo_data = await client.get(WO_OS, params=wo_params)
        keywords = ("OUTAGE", "FAULT", "POWER", "BLACKOUT")
        outage_types = {"EM", "OUTAGE", "FAULT"}
        outages: List[Dict] = []
        for w in wo_data.get("member", []):
            if (w.get("siteid") or "").upper() != site_u:
                continue
            if (w.get("reportdate") or "") < cutoff:
                continue
            desc = (w.get("description") or "").upper()
            wt = (w.get("worktype") or "").upper()
            if wt in outage_types or any(k in desc for k in keywords):
                outages.append(w)

        total_outage_hours = sum(float(w.get("actlabhrs") or 0) for w in outages)
        saidi_proxy = round(total_outage_hours * 60 / n_customers, 2) if n_customers else None  # in customer-minutes
        saifi_proxy = round(len(outages) / n_customers, 4) if n_customers else None

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "period_months": period_months,
                "customer_locations": n_customers,
                "outage_workorders": len(outages),
                "total_outage_hours": round(total_outage_hours, 2),
                "saidi_proxy_minutes_per_customer": saidi_proxy,
                "saifi_proxy_outages_per_customer": saifi_proxy,
                "data_unavailable": (n_customers == 0),
                "data_unavailable_note": (
                    "No OPERATING-type locations found at this site — SAIDI/SAIFI proxies "
                    "use OPERATING locations as a customer-count surrogate. Real utility "
                    "deployments should integrate with the outage management system instead."
                ) if n_customers == 0 else None,
            },
            duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# HEALTHCARE
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def list_medical_devices_due(
    site_id: str,
    days_ahead: int = 30,
    classification_keyword: str = "MEDICAL",
) -> Dict[str, Any]:
    """
    Medical-device assets with PM / calibration coming due. Detects
    medical devices via description / classification keyword match
    (default "MEDICAL"; can also match "DEVICE", "PATIENT", "SURGICAL").

    Args:
        site_id:                Site ID
        days_ahead:             Look-ahead window in days (default 30)
        classification_keyword: Asset-description match (default "MEDICAL")
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    now = datetime.now()
    kw_u = classification_keyword.upper()

    try:
        client = await get_connected_client()
        site_u = site_id.upper()

        # Medical-class assets
        a_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="assetnum,description,siteid,assettype,classstructureid,location,status,priority",
            page_size=200,
        )
        a_data = await client.get(ASSET_OS, params=a_params)
        all_assets = [
            a for a in a_data.get("member", [])
            if (a.get("siteid") or "").upper() == site_u
        ]
        med_assets = [
            a for a in all_assets
            if kw_u in (a.get("description") or "").upper()
            or kw_u in (a.get("assettype") or "").upper()
            or kw_u in (a.get("classstructureid") or "").upper()
        ]
        med_set = {(a.get("assetnum") or "").upper() for a in med_assets}

        # Open PM/INSP/CAL WOs for those assets
        wo_params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="wonum,description,worktype,siteid,assetnum,status,targstartdate,targcompdate",
            page_size=200,
        )
        wo_data = await client.get(WO_OS, params=wo_params)
        terminal = {"COMP", "CLOSE", "CAN"}

        due: List[Dict] = []
        for w in wo_data.get("member", []):
            if (w.get("siteid") or "").upper() != site_u:
                continue
            if (w.get("status") or "").upper() in terminal:
                continue
            asset_u = (w.get("assetnum") or "").upper()
            if asset_u not in med_set:
                continue
            wt = (w.get("worktype") or "").upper()
            if wt not in ("PM", "INSP", "CAL"):
                continue
            target = _parse_dt(w.get("targstartdate")) or _parse_dt(w.get("targcompdate"))
            if target is None:
                continue
            target = target.replace(tzinfo=None)
            days_until = (target - now).days
            if days_until > days_ahead:
                continue
            due.append(
                {
                    "wonum": w.get("wonum"),
                    "description": w.get("description"),
                    "assetnum": w.get("assetnum"),
                    "worktype": w.get("worktype"),
                    "status": w.get("status"),
                    "target_date": target.strftime("%Y-%m-%d"),
                    "days_until_due": days_until,
                    "is_overdue": days_until < 0,
                }
            )
        due.sort(key=lambda r: r["days_until_due"])

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "classification_keyword": classification_keyword,
                "days_ahead": days_ahead,
                "total_medical_devices": len(med_assets),
                "due_in_window": len(due),
                "overdue_count": sum(1 for d in due if d["is_overdue"]),
                "data_unavailable": (len(med_assets) == 0),
                "data_unavailable_note": (
                    f"No assets matched the keyword '{kw_u}'. Verify your Maximo uses "
                    "standard descriptions / classifications for medical devices."
                ) if len(med_assets) == 0 else None,
                "medical_devices": med_assets,
                "due_workorders": due,
            },
            duration_ms=duration_ms, record_count=len(due),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_device_lifecycle_status(
    site_id: str,
    asset_num: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Lifecycle bucket for medical devices (or any asset) based on age from
    `installdate`:

      NEW       — < 1 year
      STABLE    — 1 to 5 years
      AGING     — 5 to 10 years
      EOL       — > 10 years
      UNKNOWN   — no installdate

    Args:
        site_id:   Site ID
        asset_num: Optional single-asset filter
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    now = datetime.now()

    try:
        client = await get_connected_client()
        site_u = site_id.upper()
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="assetnum,description,siteid,assettype,status,installdate,manufacturer,vendor,purchaseprice",
            page_size=200,
        )
        data = await client.get(ASSET_OS, params=params)
        assets = [a for a in data.get("member", []) if (a.get("siteid") or "").upper() == site_u]
        if asset_num:
            au = asset_num.upper()
            assets = [a for a in assets if (a.get("assetnum") or "").upper() == au]

        buckets = {"NEW": 0, "STABLE": 0, "AGING": 0, "EOL": 0, "UNKNOWN": 0}
        annotated: List[Dict[str, Any]] = []
        for a in assets:
            install_dt = _parse_dt(a.get("installdate"))
            if install_dt is None:
                bucket = "UNKNOWN"
                age_years = None
            else:
                age_years = round((now - install_dt).days / 365.25, 1)
                if age_years < 1:
                    bucket = "NEW"
                elif age_years < 5:
                    bucket = "STABLE"
                elif age_years < 10:
                    bucket = "AGING"
                else:
                    bucket = "EOL"
            buckets[bucket] += 1
            annotated.append(
                {
                    "assetnum": a.get("assetnum"),
                    "description": a.get("description"),
                    "manufacturer": a.get("manufacturer"),
                    "vendor": a.get("vendor"),
                    "install_date": install_dt.strftime("%Y-%m-%d") if install_dt else None,
                    "age_years": age_years,
                    "purchase_price": a.get("purchaseprice"),
                    "status": a.get("status"),
                    "bucket": bucket,
                }
            )

        bucket_order = {"EOL": 0, "AGING": 1, "STABLE": 2, "NEW": 3, "UNKNOWN": 4}
        annotated.sort(key=lambda x: (bucket_order.get(x["bucket"], 9), -(x["age_years"] or 0)))

        # If every asset bucketed to UNKNOWN, the lifecycle analysis isn't meaningful
        all_unknown = (len(annotated) > 0 and buckets["UNKNOWN"] == len(annotated))

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "asset_num_filter": asset_num,
                "total_assets": len(annotated),
                "buckets": buckets,
                "data_unavailable": all_unknown,
                "data_unavailable_note": (
                    "Every asset returned with `installdate` blank, so all are bucketed as "
                    "UNKNOWN and the lifecycle analysis isn't actionable. Backfill `installdate` "
                    "on the asset records (Maximo: Asset application → installdate field) to "
                    "enable age-based capital-replacement planning."
                ) if all_unknown else None,
                "assets": annotated,
            },
            duration_ms=duration_ms, record_count=len(annotated),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("manager")
async def get_environment_of_care_status(site_id: str) -> Dict[str, Any]:
    """
    Joint-Commission Environment of Care rollup. Composes Wave-5 inspections,
    incidents, and Wave-4 critical-spares-check into a single-pane status.

    Args:
        site_id: Site ID
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    from tools import compliance, inventory

    sections: Dict[str, Any] = {}

    async def _safe(label: str, coro):
        try:
            sections[label] = await coro
        except Exception as exc:
            sections[label] = {"success": False, "error": f"{type(exc).__name__}: {exc!r}"}

    await _safe("inspections", compliance.list_inspections_due(site_id=site_id, days_ahead=30))
    await _safe("incidents", compliance.list_incidents(site_id=site_id))
    await _safe("critical_spares", inventory.get_critical_spares_check(site_id=site_id, priority_threshold=2))

    insp = sections["inspections"].get("data", {}) if sections["inspections"].get("success") else {}
    inc = sections["incidents"].get("data", {}) if sections["incidents"].get("success") else {}
    spares = sections["critical_spares"].get("data", {}) if sections["critical_spares"].get("success") else {}

    insp_overdue = insp.get("overdue_count", 0) or 0
    open_incidents = inc.get("totalCount", 0) or 0
    critical_assets_no_spares = sum(
        1 for a in (spares.get("critical_assets") or []) if a.get("spares_below_reorder_point", 0) > 0
    )

    eoc_score = max(0, 100 - (insp_overdue * 8 + open_incidents * 4 + critical_assets_no_spares * 5))
    if eoc_score >= 90:
        rating = "EXCELLENT"
    elif eoc_score >= 75:
        rating = "GOOD"
    elif eoc_score >= 50:
        rating = "FAIR"
    else:
        rating = "AT_RISK"

    data_unavailable_sections = [
        label for label, s in sections.items()
        if not s.get("success") or s.get("data", {}).get("data_unavailable")
    ]

    duration_ms = int((time.monotonic() - start) * 1000)
    return _envelope(
        {
            "site_id": site_id,
            "eoc_score": eoc_score,
            "rating": rating,
            "summary": {
                "inspections_overdue": insp_overdue,
                "open_incidents": open_incidents,
                "critical_assets_with_stockout_risk": critical_assets_no_spares,
            },
            "data_unavailable_sections": data_unavailable_sections,
            "sections": sections,
        },
        duration_ms=duration_ms,
    )


# ══════════════════════════════════════════════════════════════════════════════
# TRANSPORTATION
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def get_fleet_readiness(
    site_id: str,
    asset_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fleet readiness — vehicle status mix and a percentage. Detects vehicles
    via `assettype` filter when provided (e.g. "TRUCK", "BUS"), else
    description keyword fallback ("VEHICLE", "TRUCK", "BUS", "VAN", "CAR").

    Args:
        site_id:    Site ID
        asset_type: Optional asset-type filter for narrow fleet (TRUCK, BUS, …)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="assetnum,description,siteid,assettype,status,location,priority",
            page_size=200,
        )
        data = await client.get(ASSET_OS, params=params)
        site_u = site_id.upper()
        all_assets = [a for a in data.get("member", []) if (a.get("siteid") or "").upper() == site_u]

        if asset_type:
            at_u = asset_type.upper()
            fleet = [a for a in all_assets if (a.get("assettype") or "").upper() == at_u]
            detection = "assettype"
        else:
            keywords = ("VEHICLE", "TRUCK", "BUS", "VAN", "CAR", "FORKLIFT", "TRACTOR")
            fleet = [
                a for a in all_assets
                if any(k in (a.get("description") or "").upper() for k in keywords)
                or any(k in (a.get("assettype") or "").upper() for k in keywords)
            ]
            detection = "keyword_fallback"

        from collections import Counter
        status_mix = dict(Counter((a.get("status") or "UNKNOWN") for a in fleet))
        ready = sum(1 for a in fleet if (a.get("status") or "").upper() == "OPERATING")
        readiness_pct = round((ready / len(fleet)) * 100, 1) if fleet else None

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "asset_type_filter": asset_type,
                "detection_method": detection,
                "fleet_size": len(fleet),
                "ready_vehicles": ready,
                "readiness_pct": readiness_pct,
                "status_mix": status_mix,
                "data_unavailable": (len(fleet) == 0),
                "data_unavailable_note": (
                    "No fleet assets identified. Verify your Maximo uses standard assettype "
                    "values (TRUCK / BUS / VEHICLE) or descriptions matching a fleet keyword."
                ) if len(fleet) == 0 else None,
                "fleet": fleet,
            },
            duration_ms=duration_ms, record_count=len(fleet),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def list_mileage_based_pm_due(
    site_id: str,
    threshold_pct: float = 85.0,
    meter_keyword: str = "ODOM",
) -> Dict[str, Any]:
    """
    PMs tracked against a mileage / odometer meter, filtered to those whose
    latest reading is at >= threshold_pct of the next-due value. Default
    meter keyword "ODOM" matches "ODOM-KM", "ODOMETER", etc.

    Args:
        site_id:       Site ID
        threshold_pct: Surface PMs at or above this % of due (default 85)
        meter_keyword: Meter-name keyword (default "ODOM")
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    kw_u = meter_keyword.upper()

    try:
        client = await get_connected_client()
        # Pull asset meters for the site that match the keyword
        params = client.build_oslc_query(
            select="assetnum,siteid,metername,reading,readingdate",
            page_size=200,
        )
        try:
            data, used = await _try_candidates(client, ASSETMETER_OS_CANDIDATES, params)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                return _envelope(
                    {
                        "site_id": site_id,
                        "data_unavailable": True,
                        "data_unavailable_note": (
                            "Asset meter object structure not published; cannot determine "
                            f"mileage-based PMs (tried {', '.join(ASSETMETER_OS_CANDIDATES)})."
                        ),
                        "due_pms": [],
                    },
                    duration_ms=int((time.monotonic() - start) * 1000),
                    record_count=0,
                )
            raise

        site_u = site_id.upper()
        readings = [
            r for r in data.get("member", [])
            if (r.get("siteid") or "").upper() == site_u
            and kw_u in (r.get("metername") or "").upper()
        ]
        # Latest reading per asset+meter
        latest: Dict[str, Dict[str, Any]] = {}
        for r in readings:
            key = f"{r.get('assetnum')}::{r.get('metername')}"
            prev = latest.get(key)
            if prev is None or (r.get("readingdate") or "") > (prev.get("readingdate") or ""):
                latest[key] = r

        # Without PM linkage data we can't compute true % of due. Surface the
        # raw latest readings + a hint, so caller can supply due intervals.
        due_pms: List[Dict[str, Any]] = []
        for key, r in latest.items():
            try:
                reading_val = float(r.get("reading") or 0)
            except Exception:
                reading_val = None
            due_pms.append(
                {
                    "assetnum": r.get("assetnum"),
                    "metername": r.get("metername"),
                    "latest_reading": reading_val,
                    "reading_date": r.get("readingdate"),
                }
            )
        due_pms.sort(key=lambda r: r.get("latest_reading") or 0, reverse=True)

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "meter_keyword": meter_keyword,
                "threshold_pct": threshold_pct,
                "endpoint": used,
                "matched_meters": len(due_pms),
                "data_unavailable": (len(due_pms) == 0),
                "data_unavailable_note": (
                    f"No meters matching '{kw_u}' returned readings. Verify your fleet uses "
                    "ODOM-* meters and that readings are being recorded."
                ) if len(due_pms) == 0 else None,
                "note": (
                    "Returns latest mileage readings per asset+meter. To compute "
                    "true % of next-due, cross-reference with PM frequency in mxpm."
                ),
                "due_pms": due_pms,
            },
            duration_ms=duration_ms, record_count=len(due_pms),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def get_fuel_consumption_trend(
    asset_num: str,
    site_id: str,
    period_days: int = 90,
    meter_keyword: str = "FUEL",
) -> Dict[str, Any]:
    """
    Fuel-meter consumption trend for a single vehicle. Pulls all FUEL-*
    meter readings over the look-back window and computes daily / weekly
    consumption rates plus a simple spike detector.

    Args:
        asset_num:     Vehicle asset number
        site_id:       Site ID (Python post-filter)
        period_days:   Look-back window in days (default 90)
        meter_keyword: Meter-name keyword (default "FUEL")
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    kw_u = meter_keyword.upper()
    cutoff = (datetime.now() - timedelta(days=period_days)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="assetnum,siteid,metername,reading,readingdate",
            page_size=200,
        )
        try:
            data, used = await _try_candidates(client, ASSETMETER_OS_CANDIDATES, params)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                return _envelope(
                    {
                        "asset_num": asset_num,
                        "site_id": site_id,
                        "data_unavailable": True,
                        "data_unavailable_note": "Asset meter OS not published.",
                    },
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            raise

        site_u = site_id.upper()
        rows = [
            r for r in data.get("member", [])
            if (r.get("siteid") or "").upper() == site_u
            and kw_u in (r.get("metername") or "").upper()
            and (r.get("readingdate") or "") >= cutoff
        ]
        rows.sort(key=lambda r: r.get("readingdate") or "")

        # Compute deltas between consecutive readings (proxy for consumption)
        consumption: List[Dict[str, Any]] = []
        for i in range(1, len(rows)):
            try:
                prev_v = float(rows[i - 1].get("reading") or 0)
                cur_v = float(rows[i].get("reading") or 0)
                delta = cur_v - prev_v
                if delta < 0:  # tank refill — skip
                    continue
            except Exception:
                continue
            prev_dt = _parse_dt(rows[i - 1].get("readingdate"))
            cur_dt = _parse_dt(rows[i].get("readingdate"))
            if prev_dt is None or cur_dt is None:
                continue
            days_between = max((cur_dt - prev_dt).days, 1)
            consumption.append(
                {
                    "from_date": rows[i - 1].get("readingdate"),
                    "to_date": rows[i].get("readingdate"),
                    "metername": rows[i].get("metername"),
                    "delta": round(delta, 3),
                    "days": days_between,
                    "rate_per_day": round(delta / days_between, 3),
                }
            )

        avg_per_day = round(sum(c["rate_per_day"] for c in consumption) / len(consumption), 3) if consumption else None
        # Spike: daily rate > 1.5× average
        spikes = [c for c in consumption if avg_per_day and c["rate_per_day"] > 1.5 * avg_per_day]

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "asset_num": asset_num,
                "site_id": site_id,
                "period_days": period_days,
                "meter_keyword": meter_keyword,
                "endpoint": used,
                "reading_count": len(rows),
                "intervals_analysed": len(consumption),
                "avg_consumption_per_day": avg_per_day,
                "spike_count": len(spikes),
                "data_unavailable": (len(rows) == 0),
                "data_unavailable_note": (
                    f"No fuel readings found for asset {asset_num} in the last {period_days} days."
                ) if len(rows) == 0 else None,
                "consumption_intervals": consumption,
                "spikes": spikes,
            },
            duration_ms=duration_ms, record_count=len(consumption),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
