"""
tools/ai_intelligence.py — AI-powered maintenance intelligence tools.
Anomaly detection, root cause analysis, NL-to-OSLC query translation,
asset health scoring, and semantic knowledge search.
"""

import re
import statistics
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List

from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import (
    oslc_escape,
    validate_oslc_orderby,
    validate_oslc_select,
    validate_oslc_where,
)
from core.rag_engine import get_rag_engine
from core.rbac import require_role
from core.settings import get_settings


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0) -> Dict:
    return {"success": True, "data": data, "metadata": {"cached": cached, "duration_ms": duration_ms}}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


# ── NL-to-OSLC Query ──────────────────────────────────────────────────────────

# Pattern rules for common query elements
_STATUS_MAP = {
    "waiting approval": "WAPPR", "waiting for approval": "WAPPR",
    "approved": "APPR", "in progress": "INPRG", "completed": "COMP",
    "cancelled": "CAN", "waiting material": "WMATL", "closed": "CLOSE",
    "operating": "OPERATING", "decommissioned": "DECOMMISSIONED",
}
_PRIORITY_MAP = {"emergency": 1, "urgent": 2, "high": 3, "medium": 4, "low": 5}
_WO_TYPE_MAP = {"corrective": "CM", "preventive": "PM", "emergency": "EM", "inspection": "INSP"}


def _pattern_parse_oslc(query: str, object_structure: str) -> Dict[str, str]:
    """
    Rule-based NL-to-OSLC parser.
    Returns partial OSLC clauses; LLM fallback enriches these.
    """
    q = query.lower()
    where_parts = []
    order_by = "-changedate"

    # Site detection
    site_match = re.search(r'\b(?:in|at|for)\s+([A-Z]{2,20})\b', query, re.IGNORECASE)
    if site_match:
        site = site_match.group(1).upper()
        where_parts.append(f'siteid="{oslc_escape(site)}"')

    # Status detection
    for phrase, code in _STATUS_MAP.items():
        if phrase in q:
            where_parts.append(f'status="{oslc_escape(code)}"')
            break

    # Priority detection
    for word, num in _PRIORITY_MAP.items():
        if f"priority {word}" in q or f"{word} priority" in q or f"priority {num}" in q:
            where_parts.append(f"priority={num}")
            break

    # Date keywords
    now = datetime.now()
    if "overdue" in q:
        now_str = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        where_parts.append(f'schedfinish<="{now_str}"')
        where_parts.append('status in ["WAPPR","APPR","INPRG","WMATL","WSCH"]')
    elif "today" in q:
        today = now.strftime("%Y-%m-%dT00:00:00+00:00")
        where_parts.append(f'reportdate>="{today}"')
    elif "this week" in q:
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%dT00:00:00+00:00")
        where_parts.append(f'reportdate>="{week_start}"')
    elif "last 30 days" in q or "past month" in q:
        cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00+00:00")
        where_parts.append(f'reportdate>="{cutoff}"')

    # Asset number (e.g., "asset PUMP-001" or "for PUMP-001")
    asset_match = re.search(r'\basset(?:\s+number)?\s+([A-Z0-9\-]+)\b', query, re.IGNORECASE)
    if asset_match:
        where_parts.append(f'assetnum="{oslc_escape(asset_match.group(1).upper())}"')

    # Ordering
    if "oldest" in q or "earliest" in q:
        order_by = "+reportdate"
    elif "newest" in q or "latest" in q or "recent" in q:
        order_by = "-reportdate"

    # Default select based on object structure
    select_map = {
        "mxwo": "wonum,description,status,priority,assetnum,siteid,reportdate,schedfinish",
        "mxasset": "assetnum,description,status,siteid,assettype,location",
        "mxpm": "pmnum,description,assetnum,siteid,nextduedate,frequency,frequnit",
        "mxinventory": "itemnum,description,curbal,reorderpoint,storeloc,siteid",
    }
    select = select_map.get(object_structure.lower(), "*")

    return {
        "oslc_where": " and ".join(where_parts) if where_parts else "",
        "oslc_select": select,
        "oslc_order_by": order_by,
    }


async def _llm_enhance_oslc(
    query: str, object_structure: str, pattern_result: Dict[str, str]
) -> Dict[str, str]:
    """Use OpenAI to enhance the pattern-parsed OSLC query."""
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        return pattern_result

    try:
        import openai  # type: ignore
        from config.prompt_templates import NL_TO_OSLC_SYSTEM, NL_TO_OSLC_USER

        client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        user_msg = NL_TO_OSLC_USER.format(
            object_structure=object_structure,
            natural_language_query=query,
            current_date=datetime.now().strftime("%Y-%m-%d"),
        )
        resp = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": NL_TO_OSLC_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            max_tokens=500,
            temperature=0,
        )
        import json
        result = json.loads(resp.choices[0].message.content or "{}")
        candidate_where = result.get("oslc_where", pattern_result["oslc_where"])
        candidate_select = result.get("oslc_select", pattern_result["oslc_select"])
        candidate_orderby = result.get("oslc_orderBy", pattern_result["oslc_order_by"])

        # Trust boundary: the LLM produced free-text OSLC. Validate every
        # clause against the strict whitelist before letting it reach the
        # OSLC client. Any failure → discard the LLM result, fall back to
        # the pattern-parsed clauses (which are already injection-safe).
        try:
            validate_oslc_where(candidate_where)
            validate_oslc_select(candidate_select)
            validate_oslc_orderby(candidate_orderby)
        except ValueError as ve:
            return {
                **pattern_result,
                "llm_validation_error": str(ve),
                "llm_enhanced": False,
            }

        return {
            "oslc_where": candidate_where,
            "oslc_select": candidate_select,
            "oslc_order_by": candidate_orderby,
            "explanation": result.get("explanation", ""),
            "llm_enhanced": True,
        }
    except Exception as exc:
        return {**pattern_result, "llm_error": str(exc), "llm_enhanced": False}


@require_role("technician")
async def nl_to_oslc_query(
    natural_language_query: str,
    object_structure: str = "mxwo",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Convert a natural language query into a valid Maximo OSLC query string.
    Optionally execute the query and return sample results.

    Args:
        natural_language_query: Plain English query (e.g., "show overdue work orders in Bedford with priority 1")
        object_structure:       Maximo object structure to query (mxwo, mxasset, mxpm, etc.)
        dry_run:                If True, execute the query and include sample results

    Returns:
        OSLC query parameters and optionally sample results.
    """
    if not natural_language_query:
        return _error("natural_language_query is required", "VALIDATION_ERROR")

    start = time.monotonic()

    # Step 1: Pattern-based parse
    pattern_result = _pattern_parse_oslc(natural_language_query, object_structure)

    # Step 2: LLM enhancement (if available)
    enhanced = await _llm_enhance_oslc(natural_language_query, object_structure, pattern_result)

    result: Dict[str, Any] = {
        "object_structure": object_structure,
        "natural_language_query": natural_language_query,
        "oslc_where": enhanced.get("oslc_where", ""),
        "oslc_select": enhanced.get("oslc_select", "*"),
        "oslc_order_by": enhanced.get("oslc_order_by", "-changedate"),
        "llm_enhanced": enhanced.get("llm_enhanced", False),
        "explanation": enhanced.get("explanation", "Pattern-based parse"),
        "ready_to_use_params": {
            "oslc.where": enhanced.get("oslc_where", ""),
            "oslc.select": enhanced.get("oslc_select", "*"),
            "oslc.orderBy": enhanced.get("oslc_order_by", "-changedate"),
            "lean": "1",
        },
    }

    # Step 3: Execute if dry_run=True
    if dry_run and enhanced.get("oslc_where"):
        try:
            client = await get_connected_client()
            params = client.build_oslc_query(
                where=enhanced["oslc_where"],
                select=enhanced.get("oslc_select", "*"),
                order_by=enhanced.get("oslc_order_by", "-changedate"),
                page_size=5,
            )
            sample = await client.get(f"/os/{object_structure}", params=params)
            result["sample_results"] = sample.get("member", [])
            result["sample_count"] = len(result["sample_results"])
        except Exception as exc:
            result["dry_run_error"] = str(exc)

    duration_ms = int((time.monotonic() - start) * 1000)
    return _envelope(result, duration_ms=duration_ms)


# ── Anomaly Detection ──────────────────────────────────────────────────────────

@require_role("supervisor")
async def detect_asset_anomalies(
    asset_num: str,
    site_id: str,
    lookback_days: int = 90,
) -> Dict[str, Any]:
    """
    Detect statistical anomalies in an asset's failure and maintenance history.
    Flags deviations > 2 standard deviations from baseline as anomalies.

    Args:
        asset_num:     Asset number to analyse
        site_id:       Site ID
        lookback_days: Historical window for analysis (default: 90 days)

    Returns:
        anomaly_detected, severity, description, and recommended_action.
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()
        # Single-condition WHERE — compound clauses cause connection drops on some
        # Maximo builds. Site, date, and worktype filters applied in Python below.
        params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="wonum,siteid,worktype,reportdate,actfinish,actlabhrs,failurecode",
            order_by="-reportdate",
            page_size=200,
        )
        data = await client.get("/os/mxwo", params=params)
        raw: List[Dict] = data.get("member", [])
        site_u = site_id.upper()
        corrective = {"CM", "EM"}
        wos: List[Dict] = [
            w for w in raw
            if (w.get("siteid") or "").upper() == site_u
            and (w.get("worktype") or "").upper() in corrective
            and (w.get("reportdate") or "") >= cutoff
        ]

        # Group failures by week
        weekly_counts: Dict[str, int] = {}
        weekly_hrs: Dict[str, float] = {}
        for wo in wos:
            try:
                dt = datetime.fromisoformat(wo["reportdate"].replace("Z", "+00:00"))
                week = dt.strftime("%Y-W%W")
                weekly_counts[week] = weekly_counts.get(week, 0) + 1
                weekly_hrs[week] = weekly_hrs.get(week, 0) + float(wo.get("actlabhrs", 0) or 0)
            except Exception:
                pass

        anomalies = []
        if len(weekly_counts) >= 3:
            counts = list(weekly_counts.values())
            mean_c = statistics.mean(counts)
            stdev_c = statistics.stdev(counts) if len(counts) > 1 else 0
            last_week = sorted(weekly_counts.keys())[-1]
            last_count = weekly_counts.get(last_week, 0)

            if stdev_c > 0 and (last_count - mean_c) > 2 * stdev_c:
                anomalies.append({
                    "type": "FAILURE_SPIKE",
                    "detail": f"Last week had {last_count} failures vs. avg {mean_c:.1f} (baseline)",
                    "z_score": round((last_count - mean_c) / stdev_c, 2),
                })

        severity = "NONE"
        if anomalies:
            max_z = max(a.get("z_score", 0) for a in anomalies)
            severity = "HIGH" if max_z > 3 else "MEDIUM"

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "asset_num": asset_num,
                "site_id": site_id,
                "lookback_days": lookback_days,
                "total_failures_in_window": len(wos),
                "anomaly_detected": len(anomalies) > 0,
                "severity": severity,
                "anomalies": anomalies,
                "recommended_action": (
                    "Schedule immediate inspection and RCA review."
                    if severity == "HIGH" else
                    "Monitor closely and review PM schedule."
                    if severity == "MEDIUM" else
                    "Asset performing within normal parameters."
                ),
                "weekly_failure_counts": weekly_counts,
            },
            duration_ms=duration_ms
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("technician")
async def suggest_root_cause(
    asset_num: str,
    site_id: str,
    failure_description: str,
) -> Dict[str, Any]:
    """
    Suggest probable root causes for an asset failure using historical data and AI.
    Returns top 3 causes with confidence scores and recommended corrective actions.

    Args:
        asset_num:           Asset number that failed
        site_id:             Site ID
        failure_description: Description of the current failure symptom

    Returns:
        Top 3 root causes with confidence scores, evidence, and corrective actions.
    """
    if not all([asset_num, site_id, failure_description]):
        return _error("asset_num, site_id, and failure_description are required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        client = await get_connected_client()
        # Get recent failure history
        cutoff = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%dT00:00:00+00:00")
        # Single-condition WHERE — siteid, worktype, date filtered in Python.
        params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="wonum,description,failurecode,actlabhrs,reportdate,siteid,worktype",
            order_by="-reportdate",
            page_size=200,
        )
        history_raw_data = await client.get("/os/mxwo", params=params)
        site_u = site_id.upper()
        corrective = {"CM", "EM"}
        history: List[Dict] = [
            w for w in history_raw_data.get("member", [])
            if (w.get("siteid") or "").upper() == site_u
            and (w.get("worktype") or "").upper() in corrective
            and (w.get("reportdate") or "") >= cutoff
        ][:50]

        # Tally failure codes
        fc_counts: Dict[str, int] = {}
        for wo in history:
            fc = wo.get("failurecode", "UNKNOWN")
            fc_counts[fc] = fc_counts.get(fc, 0) + 1
        top_fcs = sorted(fc_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        # Try LLM for intelligent RCA
        settings = get_settings()
        if settings.OPENAI_API_KEY:
            try:
                import openai
                from config.prompt_templates import ROOT_CAUSE_SYSTEM, ROOT_CAUSE_USER
                import json
                oai = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
                user_msg = ROOT_CAUSE_USER.format(
                    asset_num=asset_num,
                    site_id=site_id,
                    failure_description=failure_description,
                    failure_history=str([{"desc": w.get("description"), "fc": w.get("failurecode")} for w in history[:10]]),
                    similar_failures=str(top_fcs),
                )
                resp = await oai.chat.completions.create(
                    model=settings.OPENAI_MODEL,
                    messages=[{"role": "system", "content": ROOT_CAUSE_SYSTEM}, {"role": "user", "content": user_msg}],
                    response_format={"type": "json_object"},
                    max_tokens=800, temperature=0.2,
                )
                rca = json.loads(resp.choices[0].message.content or "{}")
                duration_ms = int((time.monotonic() - start) * 1000)
                return _envelope({
                    "asset_num": asset_num, "site_id": site_id,
                    "failure_description": failure_description,
                    "root_causes": rca.get("root_causes", []),
                    "immediate_action": rca.get("immediate_action", ""),
                    "long_term_recommendation": rca.get("long_term_recommendation", ""),
                    "historical_failures_analysed": len(history),
                    "top_failure_codes": dict(top_fcs),
                    "source": "LLM+history",
                }, duration_ms=duration_ms)
            except Exception:
                pass

        # Fallback: rule-based RCA from failure codes
        root_causes = []
        fc_recommendations = {
            "ELEC": ("Electrical fault", "Check wiring, insulation, and power supply quality."),
            "MECH": ("Mechanical wear/failure", "Inspect bearings, seals, and moving parts."),
            "FLUID": ("Fluid system issue", "Check for leaks, blockages, and fluid quality."),
            "CTRL": ("Control system fault", "Check sensors, PLCs, and control wiring."),
            "STRUCT": ("Structural failure", "Inspect for cracks, corrosion, and stress damage."),
        }
        for i, (fc, count) in enumerate(top_fcs[:3], 1):
            prefix = fc[:5].upper() if fc else "UNKN"
            recommendation = fc_recommendations.get(
                prefix, (f"Failure code {fc}", "Review failure history and consult equipment manual.")
            )
            root_causes.append({
                "rank": i,
                "cause": recommendation[0],
                "failure_code": fc,
                "confidence": round(0.9 - (i - 1) * 0.15, 2),
                "evidence": f"Occurred {count} times in last 2 years",
                "corrective_action": recommendation[1],
            })

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope({
            "asset_num": asset_num, "site_id": site_id,
            "failure_description": failure_description,
            "root_causes": root_causes,
            "immediate_action": "Isolate equipment and conduct inspection.",
            "historical_failures_analysed": len(history),
            "top_failure_codes": dict(top_fcs),
            "source": "rule-based",
        }, duration_ms=duration_ms)

    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("technician")
async def summarize_asset_health(asset_num: str, site_id: str) -> Dict[str, Any]:
    """
    Generate a comprehensive asset health summary with an overall score (0-100).
    Aggregates recent WOs, PM compliance, downtime trend, and cost metrics.

    Args:
        asset_num: Asset number
        site_id:   Site ID

    Returns:
        overall_score (0-100), status label, key_issues list, and recommendations.
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff_90 = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()

        # Fetch recent work orders — single-condition WHERE; site/date filtered in Python.
        wo_params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="wonum,siteid,status,worktype,actlabhrs,reportdate,actfinish",
            order_by="-reportdate",
            page_size=200,
        )
        wo_data = await client.get("/os/mxwo", params=wo_params)
        site_u = site_id.upper()
        wos: List[Dict] = [
            w for w in wo_data.get("member", [])
            if (w.get("siteid") or "").upper() == site_u
            and (w.get("reportdate") or "") >= cutoff_90
        ]

        # Fetch PM compliance — single-condition WHERE; site/status filtered in Python.
        # Some Maximo builds publish only mxapipm; try both gracefully.
        pms: List[Dict] = []
        for pm_endpoint in ("/os/mxpm", "/os/mxapipm"):
            try:
                pm_params = client.build_oslc_query(
                    where=f'assetnum="{oslc_escape(asset_num)}"',
                    select="pmnum,siteid,status,nextduedate,lastcompdate,frequency,frequnit",
                    page_size=50,
                )
                pm_data = await client.get(pm_endpoint, params=pm_params)
                pms = [
                    p for p in pm_data.get("member", [])
                    if (p.get("siteid") or "").upper() == site_u
                    and (p.get("status") or "").upper() == "ACTIVE"
                ]
                break
            except (MaximoAPIError, MaximoAuthError) as exc:
                msg = str(exc)
                if "404" in msg or "not found" in msg.lower():
                    continue
                # Non-404 errors mean PM data is degraded; continue with empty pms.
                pms = []
                break

        # Calculate metrics
        total_wos = len(wos)
        corrective_wos = [w for w in wos if w.get("worktype") in ("CM", "EM")]
        open_wos = [w for w in wos if w.get("status") not in ("COMP", "CAN", "CLOSE")]
        total_downtime = sum(float(w.get("actlabhrs", 0) or 0) for w in corrective_wos)

        # PM compliance: how many PMs are overdue
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        overdue_pms = [p for p in pms if p.get("nextduedate", "9999") < now_str]
        pm_compliance = round(((len(pms) - len(overdue_pms)) / len(pms)) * 100, 1) if pms else 100

        # Score calculation (0-100)
        score = 100
        key_issues = []

        if len(corrective_wos) > 5:
            score -= 20
            key_issues.append(f"{len(corrective_wos)} corrective WOs in last 90 days")
        elif len(corrective_wos) > 2:
            score -= 10
            key_issues.append(f"{len(corrective_wos)} corrective WOs in last 90 days")

        if total_downtime > 24:
            score -= 15
            key_issues.append(f"High downtime: {total_downtime:.1f} hours")

        if open_wos:
            score -= min(10, len(open_wos) * 3)
            key_issues.append(f"{len(open_wos)} open work orders")

        if pm_compliance < 80:
            score -= 20
            key_issues.append(f"PM compliance at {pm_compliance}% ({len(overdue_pms)} overdue PMs)")
        elif pm_compliance < 95:
            score -= 10
            key_issues.append(f"PM compliance at {pm_compliance}%")

        score = max(0, score)
        if score >= 90:
            status_label = "EXCELLENT"
        elif score >= 70:
            status_label = "GOOD"
        elif score >= 50:
            status_label = "FAIR"
        elif score >= 30:
            status_label = "POOR"
        else:
            status_label = "CRITICAL"

        recommendations = []
        if len(open_wos) > 3:
            recommendations.append("Clear open work order backlog")
        if pm_compliance < 90:
            recommendations.append("Catch up on overdue preventive maintenance")
        if len(corrective_wos) > 5:
            recommendations.append("Conduct reliability analysis and consider increasing PM frequency")
        if total_downtime > 48:
            recommendations.append("Evaluate replacement or major overhaul")

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "asset_num": asset_num,
                "site_id": site_id,
                "overall_score": score,
                "status_label": status_label,
                "summary": {
                    "total_wos_90_days": total_wos,
                    "corrective_wos_90_days": len(corrective_wos),
                    "open_wos": len(open_wos),
                    "total_downtime_hrs": round(total_downtime, 2),
                    "pm_compliance_pct": pm_compliance,
                    "active_pms": len(pms),
                    "overdue_pms": len(overdue_pms),
                },
                "key_issues": key_issues,
                "recommendations": recommendations,
            },
            duration_ms=duration_ms
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


@require_role("readonly")
async def search_maximo_knowledge(
    query: str,
    doc_type: str = "all",
) -> Dict[str, Any]:
    """
    Semantic search over embedded Maximo documentation and procedures.
    Returns top 5 most relevant passages with source references.

    Args:
        query:    Natural language search query
        doc_type: Document category: "procedures" | "api_docs" | "failure_codes" | "all"

    Returns:
        Top 5 relevant knowledge passages with relevance scores.
    """
    if not query:
        return _error("query is required", "VALIDATION_ERROR")

    valid_types = {"procedures", "api_docs", "failure_codes", "all"}
    if doc_type not in valid_types:
        return _error(f"doc_type must be one of {valid_types}", "VALIDATION_ERROR")

    start = time.monotonic()
    rag = get_rag_engine()

    if not rag._ready:
        await rag.initialize()
        if not rag._ready:
            return _error(
                "Knowledge base not available. Install: pip install chromadb sentence-transformers",
                "DEPENDENCY_ERROR"
            )
        # Seed with sample docs if collection is empty
        if rag._collection and rag._collection.count() == 0:
            await rag.seed_sample_documents()

    results = await rag.search(query, doc_type=doc_type, top_k=5)
    duration_ms = int((time.monotonic() - start) * 1000)

    return _envelope(
        {"query": query, "doc_type": doc_type, "results": results, "result_count": len(results)},
        duration_ms=duration_ms
    )
