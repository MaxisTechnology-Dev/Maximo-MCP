"""
tools/ai_moat.py — Wave 8 AI tools — the differentiation layer.

Six LLM-enhanced tools, each with a deterministic rule-based or statistical
fallback so they remain useful when no `OPENAI_API_KEY` is configured:

    generate_workorder_summary       — natural-language WO summary
    auto_classify_failure            — pick failure code from free-text
    chat_with_asset                  — Q&A over one asset's full context
    recommend_pm_optimization        — tune PM frequency from failure history
    predict_failure_window           — statistical next-failure window
    generate_runbook_from_history    — synthesise step-by-step runbook from past WOs

All six follow the same pattern:
  1. Pull data with single-condition WHERE + Python post-filter (Wave-N memory)
  2. Compute the rule-based / statistical answer (always works)
  3. If `OPENAI_API_KEY` is set, call the LLM to enrich (better answers)
  4. Always include `source: "rule-based" | "llm-enhanced"` in the response
     so the caller knows which path produced the output
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.maximo_client import MaximoAPIError, MaximoAuthError, get_connected_client
from core.oslc_utils import oslc_escape
from core.rbac import require_role
from core.settings import get_settings


WO_OS = "/os/mxwo"
ASSET_OS = "/os/mxasset"
PM_OS_CANDIDATES = ("/os/mxpm", "/os/mxapipm")
ASSETMETER_OS_CANDIDATES = ("/os/mxassetmeter", "/os/mxapiassetmeter")


def _envelope(data: Any, cached: bool = False, duration_ms: int = 0, record_count: Optional[int] = None) -> Dict:
    meta: Dict[str, Any] = {"cached": cached, "duration_ms": duration_ms}
    if record_count is not None:
        meta["record_count"] = record_count
    return {"success": True, "data": data, "metadata": meta}


def _error(message: str, code: str = "API_ERROR") -> Dict:
    return {"success": False, "error": message, "error_code": code}


def _parse_dt(s: Any) -> Optional[datetime]:
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


async def _llm_call(
    system_prompt: str,
    user_prompt: str,
    response_format_json: bool = True,
    max_tokens: int = 800,
    temperature: float = 0.2,
) -> Optional[str]:
    """
    Optional LLM enrichment. Returns None whenever:
      - OPENAI_API_KEY isn't set
      - the openai package isn't installed
      - the call fails for any reason
    Callers must handle None gracefully (use the rule-based result).
    """
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        return None
    try:
        import openai  # type: ignore

        client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        kwargs: Dict[str, Any] = {
            "model": settings.OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format_json:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or None
    except Exception:
        return None


def _safe_json_parse(s: Optional[str]) -> Optional[Dict[str, Any]]:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 1. generate_workorder_summary
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def generate_workorder_summary(wonum: str, site_id: str) -> Dict[str, Any]:
    """
    Natural-language summary of a single work order — suitable for a
    management review or shift handover. Builds a structured rule-based
    summary always; enriches with LLM when `OPENAI_API_KEY` is set.

    Args:
        wonum:   Work order number
        site_id: Site ID
    """
    if not wonum or not site_id:
        return _error("wonum and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        client = await get_connected_client()
        # Single-condition fetch on wonum, post-filter siteid
        wo_params = client.build_oslc_query(
            where=f'wonum="{oslc_escape(wonum)}"',
            select="wonum,description,siteid,assetnum,worktype,status,wopriority,reportdate,reportedby,actstart,actfinish,actlabhrs,acttotalcost,failurecode,location",
            page_size=5,
        )
        wo_data = await client.get(WO_OS, params=wo_params)
        site_u = site_id.upper()
        wos = [w for w in wo_data.get("member", []) if (w.get("siteid") or "").upper() == site_u]
        if not wos:
            return _error(f"Work order '{wonum}' not found in site '{site_id}'.", "NOT_FOUND")
        wo = wos[0]

        # Pull a one-line asset description for context
        asset_num = wo.get("assetnum") or ""
        asset_description = ""
        if asset_num:
            a_params = client.build_oslc_query(
                where=f'assetnum="{oslc_escape(asset_num)}"',
                select="assetnum,description,siteid",
                page_size=2,
            )
            a_data = await client.get(ASSET_OS, params=a_params)
            for a in a_data.get("member", []):
                if (a.get("siteid") or "").upper() == site_u:
                    asset_description = a.get("description") or ""
                    break

        # Compute rule-based summary
        report_dt = _parse_dt(wo.get("reportdate"))
        finish_dt = _parse_dt(wo.get("actfinish"))
        elapsed_days = (finish_dt - report_dt).days if (report_dt and finish_dt) else None

        rule_based = {
            "headline": (
                f"WO {wo.get('wonum')} — {wo.get('worktype') or 'work'} on "
                f"{asset_num or 'no asset'}: status {wo.get('status')!r}."
            ),
            "timeline": {
                "reported": wo.get("reportdate"),
                "reported_by": wo.get("reportedby"),
                "actual_start": wo.get("actstart"),
                "actual_finish": wo.get("actfinish"),
                "elapsed_days": elapsed_days,
            },
            "resolution": {
                "failure_code": wo.get("failurecode"),
                "actual_hours": wo.get("actlabhrs"),
                "actual_cost": wo.get("acttotalcost"),
            },
            "description": wo.get("description"),
        }

        # Optional LLM enhancement
        from config.prompt_templates import WO_SUMMARY_SYSTEM, WO_SUMMARY_USER
        user_prompt = WO_SUMMARY_USER.format(
            wonum=wo.get("wonum"),
            site_id=site_id,
            asset_num=asset_num or "(unset)",
            asset_description=asset_description or "(no description)",
            status=wo.get("status") or "",
            worktype=wo.get("worktype") or "",
            reportdate=wo.get("reportdate") or "",
            reportedby=wo.get("reportedby") or "",
            actfinish=wo.get("actfinish") or "(open)",
            description=wo.get("description") or "(no description)",
            resolution_notes=wo.get("description") or "",
            failure_code=wo.get("failurecode") or "(none)",
            actlabhrs=wo.get("actlabhrs") or 0,
            acttotalcost=wo.get("acttotalcost") or 0,
        )
        llm_text = await _llm_call(WO_SUMMARY_SYSTEM, user_prompt, response_format_json=False, max_tokens=400, temperature=0.3)

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "wonum": wo.get("wonum"),
                "site_id": site_id,
                "asset_num": asset_num,
                "summary_paragraph": llm_text if llm_text else (
                    f"{rule_based['headline']} Reported {wo.get('reportdate') or 'unknown'} by "
                    f"{wo.get('reportedby') or 'unknown'}. "
                    + (f"Actual hours: {wo.get('actlabhrs')}. " if wo.get('actlabhrs') else "")
                    + (f"Failure code: {wo.get('failurecode')}. " if wo.get('failurecode') else "")
                    + (f"Description: {wo.get('description')}." if wo.get('description') else "")
                ),
                "structured": rule_based,
                "source": "llm-enhanced" if llm_text else "rule-based",
            },
            duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# 2. auto_classify_failure
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def auto_classify_failure(
    description: str,
    asset_num: Optional[str] = None,
    site_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Pick the best-fit failure code from the customer's published failure
    class hierarchy, given a free-text problem description. Mirrors what
    IBM's commercial Maximo Work Order Intelligence does, available here
    via OpenAI (when configured) or a keyword-overlap fallback.

    Args:
        description: Free-text problem description (the operator complaint)
        asset_num:   Optional — narrow the suggestion using asset type
        site_id:     Optional — for asset-type lookup
    """
    if not description:
        return _error("description is required", "VALIDATION_ERROR")

    start = time.monotonic()

    try:
        # Pull failure class hierarchy (Wave-2 helper, but we duplicate the
        # OS probe here so this tool stays self-contained).
        from tools.assets import get_failure_class_hierarchy
        fc_envelope = await get_failure_class_hierarchy()
        if not fc_envelope.get("success"):
            return _error(
                fc_envelope.get("error", "Failure class hierarchy unavailable"),
                fc_envelope.get("error_code", "DEPENDENCY_ERROR"),
            )
        fc_classes: List[Dict] = fc_envelope["data"].get("classes", [])
        if not fc_classes:
            return _error("No failure classes available to classify against.", "NOT_FOUND")

        # Asset type for context (best-effort)
        asset_type = ""
        if asset_num and site_id:
            client = await get_connected_client()
            a_params = client.build_oslc_query(
                where=f'assetnum="{oslc_escape(asset_num)}"',
                select="assetnum,assettype,description,siteid",
                page_size=2,
            )
            a_data = await client.get(ASSET_OS, params=a_params)
            site_u = site_id.upper()
            for a in a_data.get("member", []):
                if (a.get("siteid") or "").upper() == site_u:
                    asset_type = a.get("assettype") or a.get("description") or ""
                    break

        # Rule-based: keyword overlap between description and each failure class
        desc_tokens = set(t for t in description.upper().split() if len(t) >= 3)
        rule_scored: List[Dict[str, Any]] = []
        for c in fc_classes:
            # `failurelist` can be int on some Maximo builds; coerce to str.
            code = str(c.get("failurelist") or "")
            cdesc = (c.get("description") or "").upper()
            if not code:
                continue
            class_tokens = set(t for t in cdesc.split() if len(t) >= 3) | {code.upper()}
            overlap = len(desc_tokens & class_tokens)
            if overlap > 0:
                rule_scored.append(
                    {
                        "failurecode": code,
                        "description": c.get("description"),
                        "rule_score": overlap,
                    }
                )
        rule_scored.sort(key=lambda r: r["rule_score"], reverse=True)
        rule_top3 = rule_scored[:3]

        # LLM enhancement
        from config.prompt_templates import CLASSIFY_FAILURE_SYSTEM, CLASSIFY_FAILURE_USER
        class_lines = "\n".join(
            f"  {c.get('failurelist')} — {c.get('description')}" for c in fc_classes[:50]
        )
        user_prompt = CLASSIFY_FAILURE_USER.format(
            description=description,
            asset_type=asset_type or "(unknown)",
            failure_class_list=class_lines,
        )
        llm_raw = await _llm_call(CLASSIFY_FAILURE_SYSTEM, user_prompt, max_tokens=500, temperature=0.2)
        llm_parsed = _safe_json_parse(llm_raw)
        llm_rankings = (llm_parsed or {}).get("rankings") if isinstance(llm_parsed, dict) else None

        # Prefer LLM rankings when present + valid; else fall back to rule-based
        rankings: List[Dict[str, Any]]
        source: str
        if llm_rankings and isinstance(llm_rankings, list):
            # `failurelist` may come back as int on some Maximo builds — coerce.
            valid_codes = {str(c.get("failurelist") or "").upper() for c in fc_classes}
            rankings = [
                r for r in llm_rankings
                if isinstance(r, dict) and (r.get("failurecode") or "").upper() in valid_codes
            ][:3]
            source = "llm-enhanced" if rankings else "rule-based"
        else:
            rankings = []
            source = "rule-based"
        if not rankings:
            rankings = [
                {
                    "failurecode": r["failurecode"],
                    "confidence": round(r["rule_score"] / max(len(desc_tokens), 1), 2),
                    "reasoning": f"Keyword overlap score: {r['rule_score']}",
                }
                for r in rule_top3
            ]

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "description": description,
                "asset_num": asset_num,
                "asset_type": asset_type,
                "rankings": rankings,
                "rule_based_top3": rule_top3,
                "total_classes_considered": len(fc_classes),
                "source": source,
            },
            duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# 3. chat_with_asset
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def chat_with_asset(
    asset_num: str,
    site_id: str,
    question: str,
    lookback_days: int = 365,
) -> Dict[str, Any]:
    """
    Conversational Q&A over a single asset's full Maximo context — recent
    work orders, downtime stats, meter readings. The LLM receives all of
    that as context and answers the user's question with WO citations.

    Args:
        asset_num:     Asset to ask about
        site_id:       Site ID
        question:      User's natural-language question
        lookback_days: How far back to include WO history (default 365)
    """
    if not asset_num or not site_id or not question:
        return _error("asset_num, site_id, and question are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()
        site_u = site_id.upper()

        # Asset details
        a_params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="assetnum,description,siteid,status,priority,assettype,location,installdate,manufacturer,vendor",
            page_size=5,
        )
        a_data = await client.get(ASSET_OS, params=a_params)
        assets = [a for a in a_data.get("member", []) if (a.get("siteid") or "").upper() == site_u]
        if not assets:
            return _error(f"Asset '{asset_num}' not found in site '{site_id}'.", "NOT_FOUND")
        asset = assets[0]

        # Recent WO history
        wo_params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="wonum,description,siteid,status,worktype,reportdate,actfinish,actlabhrs,failurecode",
            page_size=200,
        )
        wo_data = await client.get(WO_OS, params=wo_params)
        wos = [
            w for w in wo_data.get("member", [])
            if (w.get("siteid") or "").upper() == site_u
            and (w.get("reportdate") or "") >= cutoff
        ]
        wos.sort(key=lambda w: w.get("reportdate") or "", reverse=True)

        # Quick downtime stats from history
        corrective = [w for w in wos if (w.get("worktype") or "").upper() in ("CM", "EM")]
        total_downtime = sum(float(w.get("actlabhrs") or 0) for w in corrective)
        period_hours = max(lookback_days * 24, 1)
        availability = round(((period_hours - min(total_downtime, period_hours)) / period_hours) * 100, 2)

        # Failure intervals for MTBF
        failure_dates = [
            _parse_dt(w.get("reportdate")) for w in corrective
            if _parse_dt(w.get("reportdate"))
        ]
        failure_dates.sort()
        if len(failure_dates) >= 2:
            intervals = [
                (failure_dates[i] - failure_dates[i - 1]).total_seconds() / 3600
                for i in range(1, len(failure_dates))
            ]
            mtbf_hours = round(statistics.mean(intervals), 2)
        else:
            mtbf_hours = None
        mttr_hours = round(total_downtime / len(corrective), 2) if corrective else None

        # Meter readings (best-effort)
        meter_summary = "(no meter data)"
        try:
            m_params = client.build_oslc_query(
                where=f'assetnum="{oslc_escape(asset_num)}"',
                select="assetnum,siteid,metername,reading,readingdate",
                page_size=20,
            )
            m_data, _ = await _try_candidates(client, ASSETMETER_OS_CANDIDATES, m_params)
            meter_rows = [
                r for r in m_data.get("member", [])
                if (r.get("siteid") or "").upper() == site_u
            ]
            if meter_rows:
                meter_rows.sort(key=lambda r: r.get("readingdate") or "", reverse=True)
                meter_summary = "\n".join(
                    f"  {r.get('metername')}: {r.get('reading')} on {r.get('readingdate')}"
                    for r in meter_rows[:8]
                )
        except (MaximoAPIError, MaximoAuthError):
            # Meter data is best-effort context for the LLM. If the asset-meter
            # OS isn't published on this Maximo, or the call 404s, we proceed
            # without it — the chat answer just won't reference meter readings.
            pass

        # Rule-based answer (template — always returned)
        rule_based_answer = (
            f"Asset {asset.get('assetnum')} ({asset.get('description') or 'no description'}) "
            f"at site {site_id} has had {len(wos)} work orders in the last {lookback_days} days "
            f"({len(corrective)} corrective). Availability: {availability}%, "
            f"MTBF: {mtbf_hours}h, MTTR: {mttr_hours}h. "
            f"Top recent WOs: {', '.join(w.get('wonum', '?') for w in wos[:5])}."
        )

        # LLM enhancement
        from config.prompt_templates import CHAT_WITH_ASSET_SYSTEM, CHAT_WITH_ASSET_USER
        wo_summary_lines = "\n".join(
            f"  {w.get('wonum')}: {w.get('worktype')} | {w.get('status')} | "
            f"{w.get('reportdate', '')[:10]} | {(w.get('description') or '')[:80]} | "
            f"failurecode={w.get('failurecode') or 'N/A'}"
            for w in wos[:20]
        ) or "  (no work orders in window)"

        user_prompt = CHAT_WITH_ASSET_USER.format(
            asset_num=asset.get("assetnum"),
            asset_description=asset.get("description") or "",
            site_id=site_id,
            status=asset.get("status") or "",
            priority=asset.get("priority") or "",
            installdate=asset.get("installdate") or "",
            lookback_days=lookback_days,
            wo_history=wo_summary_lines,
            mttr_hours=mttr_hours or "n/a",
            mtbf_hours=mtbf_hours or "n/a",
            availability_pct=availability,
            meter_summary=meter_summary,
            question=question,
        )
        llm_answer = await _llm_call(
            CHAT_WITH_ASSET_SYSTEM, user_prompt,
            response_format_json=False, max_tokens=500, temperature=0.3,
        )

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "asset_num": asset.get("assetnum"),
                "site_id": site_id,
                "question": question,
                "answer": llm_answer if llm_answer else rule_based_answer,
                "context_used": {
                    "wo_count": len(wos),
                    "corrective_wo_count": len(corrective),
                    "mtbf_hours": mtbf_hours,
                    "mttr_hours": mttr_hours,
                    "availability_pct": availability,
                    "lookback_days": lookback_days,
                },
                "source": "llm-enhanced" if llm_answer else "rule-based",
            },
            duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# 4. recommend_pm_optimization
# ══════════════════════════════════════════════════════════════════════════════

@require_role("supervisor")
async def recommend_pm_optimization(
    asset_num: str,
    site_id: str,
    period_months: int = 24,
) -> Dict[str, Any]:
    """
    Tune PM frequency for a specific asset based on its recent failure
    history. For each active PM:
      - >= 0.2 corrective failures per PM cycle → INCREASE frequency
      - <= 0.05 failures per PM cycle           → DECREASE frequency
      - between                                 → KEEP

    The deterministic recommendation always runs; LLM is invited to review
    and provide rationale paragraphs when configured.

    Args:
        asset_num:     Asset to analyse
        site_id:       Site ID
        period_months: Look-back window in months (default 24)
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff = (datetime.now() - timedelta(days=period_months * 30)).strftime("%Y-%m-%dT00:00:00+00:00")
    period_days = period_months * 30

    try:
        client = await get_connected_client()
        site_u = site_id.upper()

        # Active PMs for asset (try both PM endpoint variants)
        pms: List[Dict] = []
        for ep in PM_OS_CANDIDATES:
            try:
                p = client.build_oslc_query(
                    where=f'assetnum="{oslc_escape(asset_num)}"',
                    select="pmnum,description,siteid,assetnum,frequency,frequnit,status",
                    page_size=50,
                )
                pm_data = await client.get(ep, params=p)
                pms = [
                    r for r in pm_data.get("member", [])
                    if (r.get("siteid") or "").upper() == site_u
                    and (r.get("status") or "").upper() == "ACTIVE"
                ]
                break
            except (MaximoAPIError, MaximoAuthError) as exc:
                msg = str(exc)
                if "404" in msg or "not found" in msg.lower():
                    continue
                break

        if not pms:
            return _envelope(
                {
                    "asset_num": asset_num,
                    "site_id": site_id,
                    "data_unavailable": True,
                    "data_unavailable_note": (
                        "No active PMs found on this asset (or PM object structure not "
                        "exposed via OSLC). Cannot make tuning recommendations."
                    ),
                    "recommendations": [],
                },
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        # Corrective WOs in period
        wo_params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="wonum,description,worktype,siteid,reportdate,failurecode,actlabhrs",
            page_size=200,
        )
        wo_data = await client.get(WO_OS, params=wo_params)
        cm_wos = [
            w for w in wo_data.get("member", [])
            if (w.get("siteid") or "").upper() == site_u
            and (w.get("worktype") or "").upper() in ("CM", "EM")
            and (w.get("reportdate") or "") >= cutoff
        ]

        # Per-PM recommendation
        recommendations: List[Dict[str, Any]] = []
        unit_to_days = {"DAYS": 1, "WEEKS": 7, "MONTHS": 30, "YEARS": 365, "HOURS": 1 / 24}
        for pm in pms:
            try:
                freq = float(pm.get("frequency") or 0)
            except Exception:
                freq = 0
            unit = (pm.get("frequnit") or "DAYS").upper()
            freq_days = freq * unit_to_days.get(unit, 1)
            cycles_in_period = period_days / freq_days if freq_days > 0 else 0
            failures = len(cm_wos)
            failure_rate = failures / cycles_in_period if cycles_in_period > 0 else 0

            if failure_rate >= 0.2:
                action = "INCREASE"
                suggested = round(freq_days * 0.7, 0)
                rationale = f"{failures} corrective failures over ~{cycles_in_period:.1f} PM cycles ({failure_rate:.2f}/cycle) — under-maintained."
            elif failure_rate <= 0.05 and cycles_in_period > 4:
                action = "DECREASE"
                suggested = round(freq_days * 1.3, 0)
                rationale = f"Only {failures} failures over ~{cycles_in_period:.1f} cycles ({failure_rate:.2f}/cycle) — likely over-maintained."
            else:
                action = "KEEP"
                suggested = freq_days
                rationale = f"Failure rate {failure_rate:.2f}/cycle is within healthy range."
            recommendations.append(
                {
                    "pmnum": pm.get("pmnum"),
                    "description": pm.get("description"),
                    "current_frequency_days": round(freq_days, 1) if freq_days else None,
                    "suggested_frequency_days": suggested if suggested else None,
                    "action": action,
                    "rationale": rationale,
                    "confidence": round(min(0.5 + abs(failure_rate - 0.1) * 2, 0.95), 2),
                    "failure_rate_per_cycle": round(failure_rate, 3),
                }
            )

        # LLM enhancement (overall paragraph + per-PM rationale rewrite)
        from config.prompt_templates import PM_OPTIMIZATION_SYSTEM, PM_OPTIMIZATION_USER
        pm_lines = "\n".join(
            f"  {p.get('pmnum')}: '{p.get('description')}' freq={p.get('frequency')} {p.get('frequnit')}"
            for p in pms
        )
        cm_summary = "\n".join(
            f"  {w.get('wonum')} {w.get('reportdate', '')[:10]} {w.get('failurecode') or 'NONE'} — {(w.get('description') or '')[:60]}"
            for w in cm_wos[:30]
        ) or "  (none)"
        rate_lines = "\n".join(
            f"  {r['pmnum']}: {r['failure_rate_per_cycle']}/cycle ({r['action']})"
            for r in recommendations
        )
        user_prompt = PM_OPTIMIZATION_USER.format(
            asset_num=asset_num,
            site_id=site_id,
            period_months=period_months,
            pm_list=pm_lines,
            corrective_wo_summary=cm_summary,
            failure_rate_table=rate_lines,
        )
        llm_raw = await _llm_call(PM_OPTIMIZATION_SYSTEM, user_prompt, max_tokens=600, temperature=0.2)
        llm_parsed = _safe_json_parse(llm_raw) or {}
        overall_recommendation = llm_parsed.get("overall_recommendation")

        # Merge LLM rationales when present (and they refer to a real pmnum)
        if isinstance(llm_parsed.get("recommendations"), list):
            llm_by_pm = {
                r.get("pmnum"): r for r in llm_parsed["recommendations"] if isinstance(r, dict)
            }
            for r in recommendations:
                llm_r = llm_by_pm.get(r["pmnum"])
                if llm_r:
                    r["llm_rationale"] = llm_r.get("rationale")
                    if isinstance(llm_r.get("confidence"), (int, float)):
                        r["confidence"] = float(llm_r["confidence"])

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "asset_num": asset_num,
                "site_id": site_id,
                "period_months": period_months,
                "active_pms": len(pms),
                "corrective_wos": len(cm_wos),
                "overall_recommendation": overall_recommendation,
                "recommendations": recommendations,
                "source": "llm-enhanced" if overall_recommendation else "statistical",
            },
            duration_ms=duration_ms, record_count=len(recommendations),
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# 5. predict_failure_window
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def predict_failure_window(
    asset_num: str,
    site_id: str,
    lookback_months: int = 24,
) -> Dict[str, Any]:
    """
    Statistical next-failure prediction. Uses the asset's MTBF (mean time
    between corrective failures) over the look-back window and the time
    since last failure to project the next likely failure window.

    No LLM call — this is pure statistics so it works the same regardless
    of OPENAI_API_KEY.

    Args:
        asset_num:        Asset to project
        site_id:          Site ID
        lookback_months:  Period over which to compute MTBF (default 24)
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    now = datetime.now()
    cutoff = (now - timedelta(days=lookback_months * 30)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()
        site_u = site_id.upper()

        wo_params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="wonum,description,worktype,siteid,reportdate",
            page_size=200,
        )
        wo_data = await client.get(WO_OS, params=wo_params)
        cm_wos = [
            w for w in wo_data.get("member", [])
            if (w.get("siteid") or "").upper() == site_u
            and (w.get("worktype") or "").upper() in ("CM", "EM")
            and (w.get("reportdate") or "") >= cutoff
        ]

        failure_dates = [d for d in (_parse_dt(w.get("reportdate")) for w in cm_wos) if d]
        failure_dates.sort()

        if len(failure_dates) < 2:
            return _envelope(
                {
                    "asset_num": asset_num,
                    "site_id": site_id,
                    "lookback_months": lookback_months,
                    "data_unavailable": True,
                    "data_unavailable_note": (
                        f"Only {len(failure_dates)} corrective failures in the lookback window — "
                        "need at least 2 to compute MTBF for prediction."
                    ),
                    "corrective_failure_count": len(failure_dates),
                },
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        # MTBF + standard deviation
        intervals_days = [
            (failure_dates[i] - failure_dates[i - 1]).total_seconds() / 86400
            for i in range(1, len(failure_dates))
        ]
        mtbf_days = round(statistics.mean(intervals_days), 1)
        stdev_days = round(statistics.stdev(intervals_days), 1) if len(intervals_days) > 1 else None
        last_failure = failure_dates[-1]
        days_since_last = (now - last_failure).days
        days_until_predicted = max(0, mtbf_days - days_since_last)
        predicted_date = (now + timedelta(days=days_until_predicted)).strftime("%Y-%m-%d")

        # Confidence: higher when more data + lower stdev
        n = len(intervals_days)
        if stdev_days and mtbf_days:
            cv = stdev_days / mtbf_days  # coefficient of variation
            confidence = round(max(0.3, min(0.95, (1 - cv) * (1 - 1 / (n + 1)))), 2)
        else:
            confidence = round(min(0.6, n / 10), 2)

        if days_until_predicted < 7:
            urgency = "HIGH"
        elif days_until_predicted < 30:
            urgency = "MEDIUM"
        else:
            urgency = "LOW"

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "asset_num": asset_num,
                "site_id": site_id,
                "lookback_months": lookback_months,
                "corrective_failure_count": len(failure_dates),
                "mtbf_days": mtbf_days,
                "mtbf_stdev_days": stdev_days,
                "last_failure_date": last_failure.strftime("%Y-%m-%d"),
                "days_since_last_failure": days_since_last,
                "predicted_next_failure_date": predicted_date,
                "days_until_predicted_failure": days_until_predicted,
                "confidence": confidence,
                "urgency": urgency,
                "source": "statistical",
            },
            duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# 6. generate_runbook_from_history
# ══════════════════════════════════════════════════════════════════════════════

@require_role("readonly")
async def generate_runbook_from_history(
    asset_num: str,
    site_id: str,
    problem_description: str,
    lookback_months: int = 36,
) -> Dict[str, Any]:
    """
    Synthesize a step-by-step runbook for a problem on a specific asset by
    pulling resolution notes from past similar work orders (same asset
    first, then same asset type). LLM consolidates the historical
    resolutions into an ordered procedure with tools/parts list and
    estimated duration. When no LLM is configured, returns a structured
    chronological list of past resolutions for the operator to read.

    Args:
        asset_num:           Asset where the problem is occurring
        site_id:             Site ID
        problem_description: Free-text problem statement
        lookback_months:     Look-back window for past WO history (default 36)
    """
    if not asset_num or not site_id or not problem_description:
        return _error("asset_num, site_id, and problem_description are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cutoff = (datetime.now() - timedelta(days=lookback_months * 30)).strftime("%Y-%m-%dT00:00:00+00:00")

    try:
        client = await get_connected_client()
        site_u = site_id.upper()

        # Asset details (for type)
        a_params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="assetnum,description,siteid,assettype,location",
            page_size=2,
        )
        a_data = await client.get(ASSET_OS, params=a_params)
        assets = [a for a in a_data.get("member", []) if (a.get("siteid") or "").upper() == site_u]
        asset = assets[0] if assets else {}
        asset_type = (asset.get("assettype") or "").upper()
        asset_description = asset.get("description") or ""

        # WOs on the SAME asset
        wo_params = client.build_oslc_query(
            where=f'assetnum="{oslc_escape(asset_num)}"',
            select="wonum,description,siteid,worktype,status,reportdate,actfinish,actlabhrs,failurecode,assetnum",
            page_size=200,
        )
        wo_data = await client.get(WO_OS, params=wo_params)
        same_asset_wos = [
            w for w in wo_data.get("member", [])
            if (w.get("siteid") or "").upper() == site_u
            and (w.get("reportdate") or "") >= cutoff
        ]

        # Keyword overlap with problem_description for ranking
        problem_tokens = set(t for t in problem_description.upper().split() if len(t) >= 3)

        def _relevance(w: Dict) -> int:
            text = ((w.get("description") or "") + " " + (w.get("failurecode") or "")).upper()
            return sum(1 for t in problem_tokens if t in text)

        same_asset_wos.sort(key=lambda w: (_relevance(w), w.get("reportdate") or ""), reverse=True)
        relevant_same = [w for w in same_asset_wos if _relevance(w) > 0][:8]

        # WOs on SIMILAR assets (same asset type) — best-effort
        similar_wos: List[Dict] = []
        if asset_type:
            # Pull other assets of the same type at this site
            sa_params = client.build_oslc_query(
                where=f'siteid="{oslc_escape(site_id)}"',
                select="assetnum,assettype,siteid",
                page_size=200,
            )
            sa_data = await client.get(ASSET_OS, params=sa_params)
            similar_asset_set = {
                (a.get("assetnum") or "").upper()
                for a in sa_data.get("member", [])
                if (a.get("siteid") or "").upper() == site_u
                and (a.get("assettype") or "").upper() == asset_type
                and (a.get("assetnum") or "").upper() != asset_num.upper()
            }
            if similar_asset_set:
                # Pull WOs at site, then filter
                sw_params = client.build_oslc_query(
                    where=f'siteid="{oslc_escape(site_id)}"',
                    select="wonum,description,worktype,siteid,assetnum,reportdate,actfinish,actlabhrs,failurecode",
                    page_size=200,
                )
                sw_data = await client.get(WO_OS, params=sw_params)
                similar_wos = [
                    w for w in sw_data.get("member", [])
                    if (w.get("siteid") or "").upper() == site_u
                    and (w.get("assetnum") or "").upper() in similar_asset_set
                    and (w.get("reportdate") or "") >= cutoff
                ]
                similar_wos.sort(key=lambda w: (_relevance(w), w.get("reportdate") or ""), reverse=True)
                similar_wos = [w for w in similar_wos if _relevance(w) > 0][:8]

        if not relevant_same and not similar_wos:
            return _envelope(
                {
                    "asset_num": asset_num,
                    "site_id": site_id,
                    "problem_description": problem_description,
                    "data_unavailable": True,
                    "data_unavailable_note": (
                        "No relevant past work orders found on this asset or similar-type assets. "
                        "Cannot synthesise a runbook from history."
                    ),
                    "runbook": None,
                },
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        # Rule-based fallback runbook (chronological resolution snippets)
        rule_steps: List[Dict[str, Any]] = []
        for i, w in enumerate(relevant_same + similar_wos, 1):
            rule_steps.append(
                {
                    "step": i,
                    "instruction": (w.get("description") or "(no description)")[:240],
                    "based_on_wo": w.get("wonum"),
                    "from_asset": w.get("assetnum"),
                    "completed_on": w.get("actfinish"),
                    "actual_hours": w.get("actlabhrs"),
                    "failure_code": w.get("failurecode"),
                }
            )

        # LLM enhancement
        from config.prompt_templates import RUNBOOK_SYSTEM, RUNBOOK_USER
        same_lines = "\n".join(
            f"  {w.get('wonum')} ({w.get('reportdate', '')[:10]}, {w.get('actlabhrs') or 0}h, "
            f"failurecode={w.get('failurecode') or 'NONE'}): {(w.get('description') or '')[:200]}"
            for w in relevant_same
        ) or "  (none)"
        similar_lines = "\n".join(
            f"  {w.get('wonum')} on {w.get('assetnum')} ({w.get('reportdate', '')[:10]}, "
            f"{w.get('actlabhrs') or 0}h): {(w.get('description') or '')[:200]}"
            for w in similar_wos
        ) or "  (none)"
        user_prompt = RUNBOOK_USER.format(
            asset_num=asset_num,
            asset_description=asset_description,
            problem_description=problem_description,
            same_asset_history=same_lines,
            similar_asset_history=similar_lines,
        )
        llm_raw = await _llm_call(RUNBOOK_SYSTEM, user_prompt, max_tokens=900, temperature=0.2)
        llm_parsed = _safe_json_parse(llm_raw)

        runbook_payload: Dict[str, Any]
        if isinstance(llm_parsed, dict) and isinstance(llm_parsed.get("steps"), list):
            runbook_payload = llm_parsed
            source = "llm-enhanced"
        else:
            runbook_payload = {
                "preconditions": [],
                "tools_required": [],
                "spare_parts_likely_needed": [],
                "steps": rule_steps,
                "verification": "Compare condition against pre-work baseline.",
                "estimated_total_minutes": sum(
                    int(float(s.get("actual_hours") or 0) * 60) for s in rule_steps
                ) or None,
            }
            source = "rule-based"

        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(
            {
                "asset_num": asset_num,
                "site_id": site_id,
                "problem_description": problem_description,
                "lookback_months": lookback_months,
                "same_asset_relevant_wos": len(relevant_same),
                "similar_asset_relevant_wos": len(similar_wos),
                "runbook": runbook_payload,
                "source": source,
            },
            duration_ms=duration_ms,
        )
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
