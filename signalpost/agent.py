"""Per-company research pipeline and terminal envelope assembly."""
from __future__ import annotations

import time
import traceback

from . import __version__
from .evidence import SECTIONS, Ledger, stable_id
from .jobs import NavJobIndex, research_jobs
from .net import Fetcher, utc_now
from .refresh import diff
from .registry import BulkRegistry, fetch_registry
from .synthesis import build_synthesis, official_events
from .website import crawl_site, discover_and_verify

FAMILIES = {
    # family: (section, fields that make it available)
    "legal_identity": ("identity", ["legal_name"]),
    "public_brand": ("identity", ["public_brand"]),
    "contact_details": ("identity", ["contact_email", "contact_phone", "registry_email", "registry_phone"]),
    "annual_accounts": ("financials", ["revenue", "net_result", "total_assets", "equity", "operating_result"]),
    "leadership": ("leadership_locations", ["leadership"]),
    "registered_workplace": ("leadership_locations", ["registered_workplace"]),
    "website_locations": ("leadership_locations", ["website_location"]),
    "people_on_website": ("leadership_locations", ["leader_on_website", "person_on_website"]),
    "official_website": ("web_presence", ["official_website"]),
    "social_profile": ("web_presence", ["social_profile"]),
    "job_posting": ("activity", ["job_posting", "careers_page"]),
    "company_news": ("activity", ["company_news"]),
    "registry_event": ("activity", ["registry_event"]),
}
FAMILY_LABELS = {
    "public_brand": "public brand", "contact_details": "contact details", "annual_accounts": "structured annual accounts",
    "leadership": "registered leadership", "registered_workplace": "registered workplaces", "website_locations": "locations on the website",
    "people_on_website": "people named on the website", "official_website": "official website", "social_profile": "company-owned social profiles",
    "job_posting": "job postings", "company_news": "dated company news", "registry_event": "dated register events",
}
DEPENDS_ON_WEBSITE = {"public_brand", "website_locations", "people_on_website", "social_profile", "company_news"}


class Work:
    """Intermediate state of one company between the research phase and envelope assembly."""

    def __init__(self, org: str):
        self.org = org
        self.ledger = Ledger(org)
        self.ctx: dict = {"organisation_number": org}
        self.started_wall = utc_now()
        self.started = time.monotonic()
        self.terminal = "completed"
        self.requests = 0
        self.previous: dict | None = None
        self.research_ms = 0


def research_company(org: str, *, fetcher: Fetcher, bulk: BulkRegistry | None, previous: dict | None, budget_seconds: float,
                     global_deadline: float, use_sites: bool = True, wikidata: dict | None = None) -> Work:
    """Phase 1: official records, website discovery and crawl."""
    work = Work(org)
    work.previous = previous
    deadline = min(global_deadline, work.started + budget_seconds)
    ledger = work.ledger
    try:
        work.ctx = fetch_registry(org, fetcher, ledger, bulk)
        ctx = work.ctx
        if wikidata:
            ctx["wikidata"] = wikidata
        if ctx.get("entity"):
            official_events(ctx, ledger)
            site = None
            if not use_sites:
                ledger.mark("official_website", "not_available", "website research disabled for this run")
            elif time.monotonic() < deadline:
                site = discover_and_verify(ctx, fetcher, ledger, deadline)
            else:
                ledger.mark("official_website", "failed", "company time budget exhausted before website discovery")
            if site and time.monotonic() < deadline:
                crawl_site(ctx, site, fetcher, ledger, deadline)
        else:
            work.terminal = "failed"
    except Exception as exc:  # noqa: BLE001 - one company must never break the batch
        work.terminal = "failed"
        ledger.error("pipeline", f"{type(exc).__name__}: {exc}")
        ledger.error("traceback", traceback.format_exc()[-600:])
    if time.monotonic() > deadline and work.terminal == "completed":
        work.terminal = "partial"
    work.requests = getattr(fetcher, "requests", 0)
    work.research_ms = int((time.monotonic() - work.started) * 1000)
    return work


def finalize_company(work: Work, *, run_id: str, nav: NavJobIndex | None, deadline: float) -> dict:
    """Phase 2: job-feed matching (needs the shared NAV index), refresh diff, synthesis and envelope."""
    ledger, ctx = work.ledger, work.ctx
    final_started = time.monotonic()
    if nav is not None and ctx.get("entity"):
        try:
            research_jobs(ctx, nav, ledger, deadline)
        except Exception as exc:  # noqa: BLE001
            ledger.mark("nav_job_posting", "failed", f"{type(exc).__name__}: {exc}")
    completed_wall = utc_now()
    changes, refresh_meta = diff(ledger, work.previous, completed_wall)
    try:
        synthesis = build_synthesis(ctx, ledger, changes, today=completed_wall[:10])
    except Exception as exc:  # noqa: BLE001
        synthesis = {"summary": "", "sentences": [], "error": f"{type(exc).__name__}: {exc}"}
    return assemble(work.org, ledger, ctx, run_id=run_id, started=work.started_wall, completed=completed_wall, terminal=work.terminal,
                    synthesis=synthesis, changes=changes, refresh_meta=refresh_meta,
                    operations={"requests": work.requests,
                                "runtime_ms": work.research_ms + int((time.monotonic() - final_started) * 1000),
                                "third_party_cost_usd": 0.0,
                                "note": "runtime_ms counts this company's own work; the shared NAV job index is built once per batch"})


def family_states(ledger: Ledger, has_website: bool, terminal: str) -> dict:
    out = {}
    for family, (section, fields) in FAMILIES.items():
        claims = [c for c in ledger.claims.values() if c["field"] in fields]
        if claims:
            out[family] = {"state": "available", "claims": len(claims)}
            continue
        recorded = [ledger.field_states.get(f) for f in [family] + fields if ledger.field_states.get(f)]
        if family == "job_posting":
            recorded = [ledger.field_states.get(f) for f in ("nav_job_posting", "job_posting") if ledger.field_states.get(f)]
        if recorded:
            priority = ["failed", "blocked", "ambiguous", "not_available", "not_applicable"]
            best = sorted(recorded, key=lambda r: priority.index(r["state"]) if r["state"] in priority else 9)[0]
            if family == "job_posting":
                states = {r["state"] for r in recorded}
                best = next((r for r in recorded if r["state"] == "not_available"), best) if states <= {"not_available"} else best
            out[family] = dict(best)
        elif family in DEPENDS_ON_WEBSITE and not has_website:
            web = ledger.field_states.get("official_website", {})
            state = "ambiguous" if web.get("state") == "ambiguous" else "blocked" if web.get("state") == "blocked" else "not_available"
            out[family] = {"state": state, "reason": "requires a verified company website, which is " + web.get("state", "not_available").replace("_", " ")}
        elif terminal == "failed":
            out[family] = {"state": "failed", "reason": "pipeline failed before this family was checked"}
        else:
            out[family] = {"state": "not_available", "reason": "checked sources returned nothing for this family"}
    return out


def assemble(org: str, ledger: Ledger, ctx: dict, *, run_id: str, started: str, completed: str, terminal: str, synthesis: dict,
             changes: list, refresh_meta: dict, operations: dict) -> dict:
    claims = sorted(ledger.claims.values(), key=lambda c: (list(SECTIONS).index(c["section"]) if c["section"] in SECTIONS else 9, c["field"]))
    has_site = any(c["field"] == "official_website" for c in claims)
    families = family_states(ledger, has_site, terminal)
    sections = {}
    for sid, title in SECTIONS.items():
        sec_claims = [c["id"] for c in claims if c["section"] == sid and c.get("availability", "available") == "available"]
        fams = {f: families[f]["state"] for f, (s, _) in FAMILIES.items() if s == sid}
        state = "available" if sec_claims else ("failed" if terminal == "failed" else
                                                next((s for s in ("blocked", "ambiguous") if s in fams.values()), "not_available"))
        sections[sid] = {"title": title, "state": state, "claim_ids": sec_claims, "families": fams}
    sections["evidence"] = {"title": "Claim-level evidence and availability state", "state": "available" if ledger.evidence else "not_available",
                            "evidence_count": len(ledger.evidence)}
    sections["refresh"] = {"title": "Refresh metadata and material changes since the previous run",
                           "state": "available", "changes": len(changes),
                           "material_changes": sum(1 for c in changes if c["materiality"] == "material")}
    legal = ctx.get("entity") or {}
    if legal and isinstance(synthesis, dict) and "error" not in synthesis:
        synthesis = dict(synthesis)
        synthesis["unknowns"] = [{"family": f, "state": info["state"],
                                  "text": f"{FAMILY_LABELS.get(f, f.replace('_', ' '))}: {info['state'].replace('_', ' ')}"
                                          + (f" – {info['reason']}" if info.get("reason") else "")}
                                 for f, info in families.items() if info["state"] != "available"]
    if terminal == "failed" and not legal:
        overall = "failed"
    else:
        overall = "available" if claims else "not_available"
    for family, info in families.items():
        if info["state"] == "available":
            continue
        claims.append({
            "id": "cl-" + stable_id(org, "availability", family),
            "section": FAMILIES[family][0],
            "field": family,
            "value": None,
            "availability": info["state"],
            "confidence": None,
            "evidence_ids": [],
            "reason": info.get("reason"),
            **({"sources_checked": info["sources_checked"]} if info.get("sources_checked") else {}),
        })
    used_evidence = {e for c in claims for e in c["evidence_ids"]} | {e for ch in changes for e in ch.get("evidence_ids", [])} | \
                    {e for h in refresh_meta.get("history_claims", []) for e in h.get("evidence_ids", [])}
    evidence = [ledger.evidence[e] for e in sorted(used_evidence) if e in ledger.evidence]
    return {
        "schema_version": "signalpost-envelope/1.1",
        "organisation_number": org,
        "state": overall,
        "run": {"run_id": run_id, "started_at": started, "completed_at": completed, "terminal_status": terminal,
                "agent": f"signalpost-agent {__version__}"},
        "legal_identity": {
            "organisation_number": org,
            "legal_name": legal.get("navn"),
            "legal_form": (legal.get("organisasjonsform") or {}).get("kode"),
            "municipality": (legal.get("forretningsadresse") or {}).get("kommune"),
            "public_brand": ctx.get("brand"),
            "identity_anchor": "Brønnøysundregistrene Enhetsregisteret (organisation number)",
        },
        "sections": sections,
        "field_families": families,
        "claims": claims,
        "evidence": evidence,
        "synthesis": synthesis,
        "changes": changes,
        "refresh": refresh_meta,
        "identity_resolution": {
            "website_candidates": ctx.get("website_candidates", []),
            "ambiguous_sites": ctx.get("ambiguous_sites", []),
            "rule": "publish a website only with page-level proof tied to the organisation number, registry phone/e-mail, "
                    "or the registry-declared domain plus the legal name; name similarity alone abstains",
        },
        "errors": ledger.errors,
        "operations": operations,
    }


def failed_envelope(org: str, run_id: str, reason: str, previous: dict | None = None) -> dict:
    ledger = Ledger(org)
    ledger.error("pipeline", reason)
    now = utc_now()
    changes, meta = diff(ledger, previous, now)
    return assemble(org, ledger, {}, run_id=run_id, started=now, completed=now, terminal="failed",
                    synthesis={"summary": "", "sentences": [], "unknowns": [{"family": "all", "state": "failed", "text": reason}]},
                    changes=changes, refresh_meta=meta, operations={"requests": 0, "runtime_ms": 0, "third_party_cost_usd": 0.0})
