"""
tools/compliance.py — Compliance & EHS tools for IBM Maximo.

Six tools:
  list_calibration_due       — calibration PMs / cal-flagged assets due in window
  list_inspections_due       — INSP-type WOs / PMs due in window
  list_permits_to_work       — open Permit to Work records (MAS add-on)
  list_certifications_expiring — labor qualifications/certs expiring in window
  list_incidents             — safety / HSE incidents (SRs or MXINCIDENT)
  get_compliance_dashboard   — site-wide aggregate of the above

Each list-* tool follows the established Wave-N pattern:
  - single-condition WHERE only (compound clauses can drop the connection)
  - server-side `+field` / `-field` orderBy with explicit direction prefix
  - multi-candidate OS fallback (mx* legacy → mxapi* MAS 9.x)
  - graceful `data_unavailable=True` flag with admin-action hint when the
    underlying OSLC object structure is not published

The dashboard composes the other five and tolerates partial failure: if one
backing query 404s, that section reports `data_unavailable=True` while the
rest still populate.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role


# ── Endpoint candidate lists ──────────────────────────────────────────────────
# Tried in order; first non-404 wins. Same pattern as JP_OS_CANDIDATES,
# FAILURELIST_OS_CANDIDATES, etc. on the existing tool surface.
PM_OS_CANDIDATES = ("/os/mxpm", "/os/mxapipm")
WO_OS = "/os/mxwo"
SR_OS = "/os/mxsr"
LABOR_OS_CANDIDATES = ("/os/mxlabor", "/os/mxapilabor")
PERMIT_OS_CANDIDATES = ("/os/mxpermit", "/os/mxapipermit", "/os/mxptw", "/os/mxapiptw")
INCIDENT_OS_CANDIDATES = ("/os/mxincident", "/os/mxapiincident", "/os/mxsafetyincident")


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


async def _try_candidates(client, candidates, params) -> Tuple[Dict[str, Any], str]:
    """Iterate candidate OSLC endpoints; return (response, used_endpoint) on first success."""
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


def _parse_dt(s: Any) -> Optional[datetime]:
    """Parse a Maximo ISO date/time, normalising to naive (no tz)."""
    if not s or not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 1. Calibration due
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def list_calibration_due(
    site_id: str,
    days_ahead: int = 30,
) -> Dict[str, Any]:
    """
    Surface calibration PMs whose `nextdate` falls within the look-ahead
    window (default 30 days). On Maximo deployments without the Calibration
    add-on, calibration is tracked as a PM whose worktype is 'CAL' or
    description starts with 'CAL' — both heuristics are tried.

    Args:
        site_id:    Site to analyse
        days_ahead: Look-ahead window in days (default 30)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    now = datetime.now()

    try:
        client = await get_connected_client()
        # Single-condition WHERE on siteid; calibration filter applied in Python
        # because compound WHERE on PM is unreliable on some builds.
        params = client.build_oslc_query(
            where=f'siteid="{oslc_escape(site_id)}"',
            select="pmnum,description,siteid,assetnum,nextdate,frequency,frequnit,status,worktype",
            page_size=200,
        )
        try:
            data, used = await _try_candidates(client, PM_OS_CANDIDATES, params)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                return _envelope(
                    {
                        "site_id": site_id,
                        "days_ahead": days_ahead,
                        "data_unavailable": True,
                        "data_unavailable_note": (
                            "PM object structure not published in this Maximo instance "
                            f"(tried: {', '.join(PM_OS_CANDIDATES)}). "
                            "Ask your Maximo admin to publish mxpm or mxapipm via the Integration Framework."
                        ),
                        "calibration_pms": [],
                    },
                    duration_ms=int((time.monotonic() - start) * 1000),
                    record_count=0,
                )
            raise

        members: List[Dict] = data.get("member", [])
        site_u = site_id.upper()

        def _is_cal(p: Dict) -> bool:
            wt = (p.get("worktype") or "").upper()
            desc = (p.get("description") or "").upper()
            return wt == "CAL" or desc.startswith("CAL")

        rows: List[Dict[str, Any]] = []
        for p in members:
            if (p.get("siteid") or "").upper() != site_u:
                continue
            if (p.get("status") or "").upper() == "INACTIVE":
                continue
            if not _is_cal(p):
                continue
            nd = _parse_dt(p.get("nextdate"))
            if nd is None:
                continue
            days_until = (nd - now).days
            if days_until > days_ahead:
                continue
            rows.append(
                {
                    "pmnum": p.get("pmnum"),
                    "description": p.get("description"),
                    "assetnum": p.get("assetnum"),
                    "nextdate": nd.strftime("%Y-%m-%d"),
                    "days_until_due": days_until,
                    "frequency": p.get("frequency"),
                    "frequnit": p.get("frequnit"),
                    "is_overdue": days_until < 0,
                }
            )
        rows.sort(key=lambda r: r["days_until_due"])

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "days_ahead": days_ahead,
                "endpoint": used,
                "total_due": len(rows),
                "overdue_count": sum(1 for r in rows if r["is_overdue"]),
                "calibration_pms": rows,
            },
            duration_ms=duration_ms, record_count=len(rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# 2. Inspections due
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def list_inspections_due(
    site_id: str,
    days_ahead: int = 30,
) -> Dict[str, Any]:
    """
    Open WOs with worktype=INSP whose target start date falls within the
    look-ahead window. Useful for regulatory / safety inspection planning.

    Args:
        site_id:    Site to analyse
        days_ahead: Look-ahead window in days (default 30)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()
    now = datetime.now()

    try:
        client = await get_connected_client()
        # Single-condition WHERE on worktype (most selective). Site + status +
        # date filters applied in Python.
        params = client.build_oslc_query(
            where='worktype="INSP"',
            select="wonum,description,worktype,siteid,status,assetnum,location,targstartdate,targcompdate,wopriority",
            page_size=200,
        )
        data = await client.get(WO_OS, params=params)
        members: List[Dict] = data.get("member", [])

        site_u = site_id.upper()
        terminal = {"COMP", "CLOSE", "CAN"}
        rows: List[Dict[str, Any]] = []
        for w in members:
            if (w.get("siteid") or "").upper() != site_u:
                continue
            if (w.get("status") or "").upper() in terminal:
                continue
            target = _parse_dt(w.get("targstartdate")) or _parse_dt(w.get("targcompdate"))
            if target is None:
                continue
            days_until = (target - now).days
            if days_until > days_ahead:
                continue
            rows.append(
                {
                    "wonum": w.get("wonum"),
                    "description": w.get("description"),
                    "assetnum": w.get("assetnum"),
                    "location": w.get("location"),
                    "status": w.get("status"),
                    "priority": w.get("wopriority"),
                    "targstartdate": target.strftime("%Y-%m-%d"),
                    "days_until_due": days_until,
                    "is_overdue": days_until < 0,
                }
            )
        rows.sort(key=lambda r: r["days_until_due"])

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "days_ahead": days_ahead,
                "total_due": len(rows),
                "overdue_count": sum(1 for r in rows if r["is_overdue"]),
                "inspections": rows,
            },
            duration_ms=duration_ms, record_count=len(rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# 3. Permits to work
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def list_permits_to_work(
    site_id: Optional[str] = None,
    status: Optional[str] = None,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List Permit to Work records. The PtW object structure is part of the
    Maximo Health, Safety & Environment (HSE) add-on; many Maximo deployments
    do not publish it. The tool tries multiple candidate endpoints and
    returns a graceful `data_unavailable=True` response when none resolves.

    Args:
        site_id:   Optional site filter (Python post-filter)
        status:    Optional status filter (Python post-filter)
        page_size: Records per page (default 50, max 200)
        page_num:  1-based page number
    """
    page_size = max(1, min(int(page_size or 50), 200))
    start = time.monotonic()

    try:
        client = await get_connected_client()
        params = client.build_oslc_query(
            select="permitnum,description,siteid,status,permittype,issueddate,expirydate,assetnum,location",
            page_size=200,
        )
        try:
            data, used = await _try_candidates(client, PERMIT_OS_CANDIDATES, params)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                return _envelope(
                    {
                        "site_id": site_id,
                        "data_unavailable": True,
                        "data_unavailable_note": (
                            f"Permit to Work object structure not published in this Maximo "
                            f"instance (tried: {', '.join(PERMIT_OS_CANDIDATES)}). "
                            "PtW typically requires the Maximo HSE add-on or a custom object structure."
                        ),
                        "permits": [],
                    },
                    duration_ms=int((time.monotonic() - start) * 1000),
                    record_count=0,
                )
            raise

        members: List[Dict] = data.get("member", [])

        def _matches(p: Dict) -> bool:
            if site_id and (p.get("siteid") or "").upper() != site_id.upper():
                return False
            if status and (p.get("status") or "").upper() != status.upper():
                return False
            return True

        filtered = [p for p in members if _matches(p)]
        total = len(filtered)
        start_idx = (page_num - 1) * page_size
        page_rows = filtered[start_idx:start_idx + page_size]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "endpoint": used,
                "permits": page_rows,
                "totalCount": total,
            },
            duration_ms=duration_ms, record_count=len(page_rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# 4. Certifications expiring
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def list_certifications_expiring(
    site_id: Optional[str] = None,
    days_ahead: int = 90,
) -> Dict[str, Any]:
    """
    Surface labor qualifications/certifications expiring within the
    look-ahead window. Pulls the `laborqual` child collection from each
    labor record and bucket the rows by EXPIRED / EXPIRING_SOON / ACTIVE.

    Args:
        site_id:    Optional site filter — labor is org-scoped on most builds,
                    so we post-filter and silently keep records where
                    `siteid` is missing (matches Wave-3 labor behaviour).
        days_ahead: Window for the EXPIRING_SOON bucket (default 90 days)
    """
    start = time.monotonic()
    now = datetime.now()
    cutoff = now + timedelta(days=days_ahead)

    try:
        client = await get_connected_client()
        # NOTE: `siteid` is not queryable on mxlabor; filter status only.
        params = client.build_oslc_query(
            where='status="ACTIVE"',
            select="laborcode,personid,siteid,craft,status,laborqual",
            page_size=200,
        )
        try:
            data, used = await _try_candidates(client, LABOR_OS_CANDIDATES, params)
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" in msg or "not found" in msg.lower():
                return _envelope(
                    {
                        "site_id": site_id,
                        "days_ahead": days_ahead,
                        "data_unavailable": True,
                        "data_unavailable_note": (
                            "Labor object structure not published in this Maximo instance "
                            f"(tried: {', '.join(LABOR_OS_CANDIDATES)})."
                        ),
                        "certifications": [],
                    },
                    duration_ms=int((time.monotonic() - start) * 1000),
                    record_count=0,
                )
            raise

        members: List[Dict] = data.get("member", [])
        site_u = site_id.upper() if site_id else None

        rows: List[Dict[str, Any]] = []
        rows_seen_with_qual = 0
        buckets = {"EXPIRED": 0, "EXPIRING_SOON": 0, "ACTIVE": 0}
        for L in members:
            row_site = (L.get("siteid") or "").upper()
            if site_u and row_site and row_site != site_u:
                continue
            quals = L.get("laborqual") or []
            if quals:
                rows_seen_with_qual += 1
            for q in quals:
                exp = _parse_dt(q.get("expirationdate") or q.get("expdate"))
                if exp is None:
                    continue
                days_until = (exp - now).days
                if exp < now:
                    bucket = "EXPIRED"
                elif exp <= cutoff:
                    bucket = "EXPIRING_SOON"
                else:
                    bucket = "ACTIVE"
                buckets[bucket] += 1
                if bucket == "ACTIVE":
                    continue  # only surface expired / soon-to-expire in the list
                rows.append(
                    {
                        "laborcode": L.get("laborcode"),
                        "personid": L.get("personid"),
                        "craft": L.get("craft"),
                        "qualification": q.get("qualificationid") or q.get("qualtype") or q.get("qualification"),
                        "expirationdate": exp.strftime("%Y-%m-%d"),
                        "days_until_expiry": days_until,
                        "bucket": bucket,
                    }
                )
        rows.sort(key=lambda r: r["days_until_expiry"])

        # If no laborqual rows came back at all, surface a data_unavailable hint.
        data_unavailable_note = None
        if members and rows_seen_with_qual == 0:
            data_unavailable_note = (
                "Labor records returned but the `laborqual` child collection is empty / "
                "not exposed via OSLC. Ask your Maximo admin to add laborqual to the "
                "MXLABOR object structure."
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "days_ahead": days_ahead,
                "endpoint": used,
                "total_active_labor": len(members),
                "buckets": buckets,
                "data_unavailable": bool(data_unavailable_note),
                "data_unavailable_note": data_unavailable_note,
                "expiring_certifications": rows,
            },
            duration_ms=duration_ms, record_count=len(rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# 5. Incidents
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def list_incidents(
    site_id: Optional[str] = None,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    page_size: Optional[int] = None,
    page_num: int = 1,
) -> Dict[str, Any]:
    """
    List safety / HSE incidents. Tries the dedicated MXINCIDENT object
    structure first and falls back to MXSR records flagged with a SAFETY
    classification when the dedicated OS isn't published.

    Args:
        site_id:   Optional site filter
        status:    Optional status filter
        severity:  Optional severity filter
        page_size: Records per page (default 50, max 200)
        page_num:  1-based page number
    """
    page_size = max(1, min(int(page_size or 50), 200))
    start = time.monotonic()

    try:
        client = await get_connected_client()
        # Try dedicated incident OS first
        try:
            params = client.build_oslc_query(
                select="incidentnum,description,siteid,status,severity,reportdate,reportedby,assetnum,location",
                page_size=200,
            )
            data, used = await _try_candidates(client, INCIDENT_OS_CANDIDATES, params)
            source = "incident_os"
            members: List[Dict] = data.get("member", [])
            normalised = [
                {
                    "id": m.get("incidentnum"),
                    "description": m.get("description"),
                    "siteid": m.get("siteid"),
                    "status": m.get("status"),
                    "severity": m.get("severity"),
                    "reportdate": m.get("reportdate"),
                    "reportedby": m.get("reportedby"),
                    "assetnum": m.get("assetnum"),
                    "location": m.get("location"),
                    "source": source,
                }
                for m in members
            ]
        except (MaximoAPIError, MaximoAuthError) as exc:
            msg = str(exc)
            if "404" not in msg and "not found" not in msg.lower():
                raise
            # Fall back: SR records with a SAFETY classification
            params = client.build_oslc_query(
                select="ticketid,description,classstructureid,class,status,siteid,reportdate,reportedby,assetnum",
                page_size=200,
            )
            data = await client.get(SR_OS, params=params)
            used = SR_OS
            source = "sr_safety"
            members = data.get("member", [])
            normalised = [
                {
                    "id": m.get("ticketid"),
                    "description": m.get("description"),
                    "siteid": m.get("siteid"),
                    "status": m.get("status"),
                    "severity": None,  # SR doesn't carry severity directly
                    "reportdate": m.get("reportdate"),
                    "reportedby": m.get("reportedby"),
                    "assetnum": m.get("assetnum"),
                    "location": None,
                    "classstructureid": m.get("classstructureid"),
                    "source": source,
                }
                for m in members
                # Heuristic — keep only rows whose classification or description
                # smells like a safety/HSE incident.
                if (
                    "SAFETY" in (m.get("classstructureid") or "").upper()
                    or "INCIDENT" in (m.get("description") or "").upper()
                    or "INJURY" in (m.get("description") or "").upper()
                    or "HAZARD" in (m.get("description") or "").upper()
                )
            ]

        # Apply caller-supplied filters in Python
        def _matches(r: Dict) -> bool:
            if site_id and (r.get("siteid") or "").upper() != site_id.upper():
                return False
            if status and (r.get("status") or "").upper() != status.upper():
                return False
            if severity and (r.get("severity") or "").upper() != severity.upper():
                return False
            return True

        filtered = [r for r in normalised if _matches(r)]
        total = len(filtered)
        start_idx = (page_num - 1) * page_size
        page_rows = filtered[start_idx:start_idx + page_size]
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "site_id": site_id,
                "endpoint": used,
                "source": source,
                "incidents": page_rows,
                "totalCount": total,
            },
            duration_ms=duration_ms, record_count=len(page_rows),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# 6. Compliance dashboard
# ══════════════════════════════════════════════════════════════════════════════

@require_role("manager")
async def get_compliance_dashboard(
    site_id: str,
    days_ahead: int = 30,
) -> Dict[str, Any]:
    """
    Site-wide compliance dashboard composed of the other Wave-5 tools.
    Tolerates partial failure — if any backing query 404s, that section
    surfaces `data_unavailable=True` while the rest still populate.

    Args:
        site_id:    Site to analyse
        days_ahead: Look-ahead window in days for due-soon counts (default 30)
    """
    if not site_id:
        return _error("site_id is required", "VALIDATION_ERROR")

    start = time.monotonic()

    # Run all five lookups, but tolerate per-section failure so the dashboard
    # remains useful when one OS is missing.
    sections: Dict[str, Any] = {}

    async def _safe(label: str, coro):
        try:
            r = await coro
            sections[label] = r
        except Exception as exc:
            sections[label] = {
                "success": False,
                "error": f"{type(exc).__name__}: {exc!r}",
                "error_code": "SECTION_ERROR",
            }

    await _safe("calibration", list_calibration_due(site_id=site_id, days_ahead=days_ahead))
    await _safe("inspections", list_inspections_due(site_id=site_id, days_ahead=days_ahead))
    await _safe("permits", list_permits_to_work(site_id=site_id))
    await _safe("certifications", list_certifications_expiring(site_id=site_id, days_ahead=days_ahead * 3))
    await _safe("incidents", list_incidents(site_id=site_id))

    def _count(label: str, key: str) -> Optional[int]:
        s = sections.get(label, {})
        if not s.get("success"):
            return None
        d = s.get("data", {})
        # Top-level counts the section already exposes
        if key in d:
            v = d[key]
            return int(v) if isinstance(v, (int, float)) else None
        return None

    summary = {
        "calibration_due_in_window": _count("calibration", "total_due"),
        "calibration_overdue": _count("calibration", "overdue_count"),
        "inspections_due_in_window": _count("inspections", "total_due"),
        "inspections_overdue": _count("inspections", "overdue_count"),
        "open_permits": _count("permits", "totalCount"),
        "certifications_expiring": (
            sections["certifications"]["data"]["buckets"]["EXPIRING_SOON"]
            if sections.get("certifications", {}).get("success")
            and isinstance(sections["certifications"].get("data", {}).get("buckets"), dict)
            else None
        ),
        "certifications_expired": (
            sections["certifications"]["data"]["buckets"]["EXPIRED"]
            if sections.get("certifications", {}).get("success")
            and isinstance(sections["certifications"].get("data", {}).get("buckets"), dict)
            else None
        ),
        "open_incidents": _count("incidents", "totalCount"),
    }

    # Surface which sections degraded
    data_unavailable_sections: List[str] = []
    for label, s in sections.items():
        if not s.get("success"):
            data_unavailable_sections.append(label)
        elif s.get("data", {}).get("data_unavailable"):
            data_unavailable_sections.append(label)

    duration_ms = int((time.monotonic() - start) * 1000)
    return _envelope(
        {
            "site_id": site_id,
            "days_ahead": days_ahead,
            "summary": summary,
            "data_unavailable_sections": data_unavailable_sections,
            "sections": sections,
        },
        duration_ms=duration_ms,
    )
