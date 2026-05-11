# Changelog

All notable changes to **maximo-enterprise-mcp** are tracked here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [1.1.0] — 2026-05-08

**Tool surface: 69 → 95 (26 net-new across Waves 7, 8, 9 + a cross-cutting
friendly-message audit + the multi-version compatibility statement).**

### Added — Wave 7: industry verticals (18 new tools)

Six verticals × three tools each. Every tool gracefully returns
`data_unavailable: true` with an admin-action note when the customer's
Maximo doesn't expose the underlying classification, asset spec, or
meter type.

**Pharma / life sciences**
- `get_calibration_audit_trail` — chronological cal log per asset (FDA / GxP audit prep)
- `list_cleanroom_assets` — assets in cleanroom-classified locations (GMP / GxP environment monitoring)
- `get_gxp_compliance_status` — overdue cal + cert + incidents → risk score and rating

**Oil & gas**
- `get_turnaround_status` — multi-WO parent / child rollup (TAR planning)
- `list_pressure_vessels_due` — pressure-class assets with inspection due in window
- `get_lifting_register` — crane / lifting operations log (LIFT / CRANE / HOIST keyword)

**Manufacturing**
- `get_oee` — OEE with Availability solid; Performance / Quality flagged when MES data isn't in Maximo
- `get_production_line_status` — open WOs + downtime per location subtree
- `list_changeover_workorders` — SMED / set-up WOs with average duration for drift detection

**Utilities**
- `get_outage_impact_analysis` — downstream child assets + locations of an outage
- `list_grid_zone_assets` — every asset under a location-hierarchy zone
- `get_reliability_indices` — SAIDI / SAIFI proxies from outage WOs

**Healthcare**
- `list_medical_devices_due` — medical-class assets with PM / calibration due
- `get_device_lifecycle_status` — bucket by age (NEW / STABLE / AGING / EOL) with warranty
- `get_environment_of_care_status` — Joint Commission EOC rollup

**Transportation**
- `get_fleet_readiness` — vehicle status mix and ready-vehicle %
- `list_mileage_based_pm_due` — PMs tracked against ODOM-KM / mileage meters
- `get_fuel_consumption_trend` — fuel-meter usage rate per day with simple spike detection

### Added — Wave 8: AI moat (6 new tools)

The differentiation layer. Six tools that are LLM-enhanced when
`OPENAI_API_KEY` is set, with deterministic / statistical fallbacks so
they remain useful without one. Every response includes a
`source: "llm-enhanced" | "rule-based" | "statistical"` flag so the
caller knows which path produced the output.

- `generate_workorder_summary` — natural-language WO summary plus
  structured timeline / resolution / cost breakdown for shift handover
  or management review
- `auto_classify_failure` — pick the best-fit failure code from a
  free-text problem description; LLM ranks against the customer's
  published failure-class hierarchy, keyword-overlap fallback otherwise
  (mirrors IBM's Maximo Work Order Intelligence)
- `chat_with_asset` — conversational Q&A over one asset's full
  Maximo context: WO history + downtime stats + meter readings.
  Answers cite specific WO numbers and never hallucinate beyond
  the data given
- `recommend_pm_optimization` — tune PM frequency per asset based on
  actual failure rate vs PM cycles. Deterministic decision rules
  (KEEP / INCREASE / DECREASE / RETIRE) always run; LLM adds rationale
  paragraphs when configured
- `predict_failure_window` — pure statistical: MTBF + standard deviation
  + time-since-last-failure → predicted next failure date with
  HIGH / MEDIUM / LOW urgency and confidence score. No LLM needed
- `generate_runbook_from_history` — synthesise step-by-step runbook
  from past WO resolutions on this and similar-type assets. Returns
  preconditions, tools, parts likely needed, ordered steps with WO
  citations and per-step duration estimates. The Tractian-style killer
  feature

### Added — Wave 9: spatial / GIS (2 new tools)

- `find_assets_near_location` — radius search for assets around a lat/lon
  (field dispatch, incident response, geofence alerting). Tries multiple
  coordinate-field naming conventions (`latitudey/longitudex`,
  `latitude/longitude`, `ycoord/xcoord`) and uses the first variant that
  yields populated values. When no convention works, returns
  `data_unavailable=True` with an admin-friendly note instead of a
  cryptic error
- `get_route_for_technician` — daily-route ordering for a technician's
  open work orders. When asset coordinates exist, runs a greedy
  nearest-neighbour TSP tour with cumulative distance per leg. When
  coordinates don't exist (the common case without Maximo Spatial),
  falls back to priority + target-start ordering and clearly flags
  `geographic_optimisation: false` so the caller knows the route is
  logical, not literally geographic

### Changed — Friendly "data unavailable" messaging policy

Audited Wave 7 (industry-vertical tools) and added explicit
`data_unavailable_note` fields to 8 tools that previously returned
empty results without explanation. Every empty-result path now produces
a tool-specific user-friendly message describing what's missing and
what an admin can do:

- `get_calibration_audit_trail` — when no CAL-typed WOs found
- `get_turnaround_status` — when no parent / child WO groupings
- `get_lifting_register` — when no LIFT/CRANE/HOIST WOs in window
- `get_production_line_status` — when no parent locations modeled
- `list_changeover_workorders` — when no SMED / set-up WOs
- `get_outage_impact_analysis` — when no downstream children/locations
- `list_grid_zone_assets` — when zone code not in location hierarchy
- `get_device_lifecycle_status` — when every asset has blank installdate

This is now the consistent policy across all 95 tools: **users get an
actionable message, never a cryptic error**.

### Documented Maximo build quirks (with code-level handling)

- `parent` field is not queryable in OSLC WHERE on `mxasset` /
  `mxoperloc` (`BMXAA4185E`) even though it is selectable — filter in
  Python after a single-condition `siteid` fetch. `mxwo` exempt;
  `parent` queryable there for turnaround drill-down.
- `failurelist` field on `/os/mxapifailurelist` can come back as int on
  some Maximo builds. Coerce with `str(...)` before any string
  operations like `.upper()`.

### Added — Maximo version compatibility matrix (README)

Explicit support claims clarified after user confirmed test instance is
**Maximo 7.6.x with mxapi integration extensions enabled** (not MAS 9 as
previously inferred from `mxapi*` endpoint availability):

| Build | Status |
|---|---|
| Maximo 7.6.x with mxapi extensions | ✅ Verified live (8/8 wave smokes pass in ~3.5 min) |
| Maximo 7.6.x without mxapi extensions | ✅ Should work (multi-candidate falls back to `mx*`) |
| MAS 8.x (Manage on RHOCP) | ⚠️ Should work, not tested (MAS SSO may need `MCP_AUTH_MODE=jwt`) |
| MAS 9.x (Manage on RHOCP) | ⚠️ Should work, not tested (same auth caveat) |

### Tool surface

- `95` total tools (was 69)
- Every public tool retains a strict `extra="forbid"` Pydantic input model
- Full integration suite: 270 unit tests + 8 wave smoke tests passing live in ~3.5 minutes
- 12 Pydantic sanity tests (coverage / strict-extra / range / pattern / drift detection)

---

## [1.0.0] — 2026-05-07

First major release. **Tool surface goes 31 → 69 across 6 waves.**

### Added — Wave 1: catalog and intake gaps (10 tools)

Closed asymmetric "you can `get_X` but not `list_X`" holes.

- `list_purchase_orders`, `list_vendors`, `list_purchase_requisitions`
- `list_items`, `get_item`, `list_storerooms`
- `list_service_requests`, `get_service_request`
- `list_job_plans`
- `get_my_assigned_workorders`, `get_workorder_tasks`

### Added — Wave 2: reliability (5 new + 3 re-enabled)

- `get_failure_class_hierarchy` — Maximo problem / cause / remedy taxonomy
- `get_meter_readings` — per-meter trend deltas over a look-back window
- `get_asset_criticality_matrix` — bucket by `priority` (1=highest)
- `get_failure_pareto` — top failure codes with cumulative %
- `get_bad_actor_assets` — top-N by corrective WO count, hours, cost

Re-enabled after refactoring to single-condition WHERE:
- `detect_asset_anomalies` — statistical (>2σ) failure-pattern flagging
- `suggest_root_cause` — RCA from failure history (LLM-enhanced if `OPENAI_API_KEY`, rule-based fallback otherwise)
- `summarize_asset_health` — 0–100 health score with key issues + recommendations

### Added — Wave 3: planner / scheduler (6 tools)

- `get_job_plan` — full plan with embedded tasks / labor / material / tools
- `get_workorder_actuals_vs_planned` — variance analysis (hrs, labor, material, total)
- `get_schedule_calendar` — date-bucketed scheduled-WO view
- `estimate_workorder_cost` — sums labor + material + tool from a job plan
- `list_crafts` — craft / trade master
- `find_available_technician` — active labor sorted by open-assignment count

### Added — Wave 4: procurement and cost depth (6 tools)

- `list_purchase_requisitions` — upstream of POs in the procurement workflow
- `get_spend_analysis` — by vendor / status / worktype with concentration metrics
- `get_workorder_costs` — labor + material + service + tool actual breakdown
- `get_inventory_valuation` — total $ valuation + top-N items by line value (uses embedded `invcost` child)
- `get_critical_spares_check` — stockout risk for priority-1/2 asset spares
- `get_warranty_status` — ACTIVE / EXPIRING_SOON / EXPIRED / UNKNOWN buckets

### Added — Wave 5: compliance and EHS (6 tools)

- `list_calibration_due` — calibration PMs due in window (worktype CAL or description prefix CAL)
- `list_inspections_due` — open INSP-typed WOs due in window
- `list_permits_to_work` — Permit to Work records (HSE add-on; graceful fallback when not published)
- `list_certifications_expiring` — labor qualifications expiring soon (bucketed)
- `list_incidents` — safety / HSE incidents (`mxincident` first, SR fallback with classification heuristic)
- `get_compliance_dashboard` — site-wide rollup composing all of the above

### Re-enabled — Wave 6: exports

- `export_workorders_excel` — Excel workbook (.xlsx) with branded header
- `export_asset_report_pdf` — A4 PDF with branded header

### Added — Pydantic input validation

- 49 strict request models in `core/tool_models.py` (was 8)
- Every public tool now has a `request_model` wired in `core/tool_catalog.py`
- New test module `tests/test_tool_models.py` with 12 sanity tests covering coverage / strict-extra / range / pattern / drift detection
- The drift detector caught one real bug: `search_assets` server wrapper was dropping `page_num` — fixed end-to-end

### Added — Integration test framework

- 5 pytest-driven smoke tests under `tests/integration/` (one per wave)
- Every test runs as a pytest module (`pytest tests/integration -m integration`) AND standalone (`python tests/integration/test_smoke_wave1.py`)
- `tests/integration/conftest.py` auto-skips when `MAXIMO_URL` isn't set — CI-safe

### Documented Maximo build quirks (with code-level handling)

- OSLC `orderBy` requires explicit `+` / `-` direction prefix on strict-OSLC builds
- Compound WHERE with `>= date` or `in [...]` drops the connection on some builds — refactored 3 AI tools to single-condition WHERE
- `orderBy` on sparse-NULL columns silently strips the column from the response — sort in Python instead
- Maximo timestamps carry tz offsets, user inputs don't — `replace(tzinfo=None)` before compare
- Multi-candidate OS fallback (`mx*` → `mxapi*`) so the same code works on
  legacy Maximo 7.6 (only `mx*` published), Maximo 7.6.1.x with the
  integration patch pack (both published), and MAS 8 / MAS 9 (both published,
  `mxapi*` is the preferred name)
- Graceful `data_unavailable=True` flag when an OSLC object structure isn't published
- `siteid` is not queryable on `mxlabor` / `mxapilabor` — filter by `craft` / `status`, post-filter siteid in Python

### Internal hardening

- Untracked `.claude/settings.local.json` (had personal dev paths)
- `tests/integration/conftest.py` no longer mutates env at module-load — fixes a CI failure where the integration conftest clobbered the unit-test dummy creds
- 3 CodeQL findings closed: `bind_runtime(server)` calls moved into `else:` branches; unused `sample_vendor` cleaned up

---

## [0.1.2] — 2026-04-09

Initial public release with 31 tools. Production-grade security posture:
RBAC, JWT/OIDC inbound auth, PII masking at the response boundary, pluggable
audit sinks, OSLC injection guards with adversarial test suite, multi-stage
hardened Docker, fail-closed hosted mode.

[1.1.0]: https://github.com/MaxisTechnology-Dev/Maximo-MCP/releases/tag/v1.1.0
[1.0.0]: https://github.com/MaxisTechnology-Dev/Maximo-MCP/releases/tag/v1.0.0
[0.1.2]: https://github.com/MaxisTechnology-Dev/Maximo-MCP/releases/tag/v0.1.2
