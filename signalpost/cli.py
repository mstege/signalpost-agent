"""Batch runner: one terminal envelope per input organisation number.

    python -m signalpost --organisations batch.jsonl --output out/envelopes.jsonl

Accepts JSON, JSONL or plain-text organisation-number lists (same shapes as the Builderr starter).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from . import __version__
from .agent import failed_envelope, finalize_company, research_company
from .jobs import NavJobIndex
from .net import Fetcher, HostGate, utc_now
from .refresh import load_previous, save_state
from .registry import BulkRegistry


class CountingFetcher:
    """Per-company view on the shared fetcher so request counts stay per company."""

    def __init__(self, base: Fetcher):
        self.base = base
        self.requests = 0

    def get(self, *args, **kwargs):
        self.requests += 1
        return self.base.get(*args, **kwargs)


def read_inputs(path: str) -> list[str]:
    source = Path(path)
    text = source.read_text(encoding="utf-8-sig")
    values: list = []
    stripped = text.strip()
    if source.suffix == ".json" or stripped.startswith("[") or (stripped.startswith("{") and "\n" not in stripped):
        body = json.loads(stripped)
        values = body if isinstance(body, list) else body.get("organisation_numbers") or body.get("companies") or body.get("items") or []
    elif source.suffix == ".jsonl" or stripped.startswith("{"):
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        values = [line.split(",")[0].strip() for line in text.splitlines() if line.strip()]
    orgs: list[str] = []
    for value in values:
        raw = value.get("organisation_number") or value.get("organisationNumber") or value.get("orgnr") or value.get("org") if isinstance(value, dict) else value
        org = re.sub(r"\D", "", str(raw or ""))
        if len(org) == 9 and org not in orgs:
            orgs.append(org)
        elif len(org) != 9:
            print(f"warning: skipping invalid organisation number {raw!r}", file=sys.stderr)
    return orgs


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="signalpost", description="Signalpost company research agent")
    p.add_argument("--organisations", "--input", "-i", dest="organisations", default=os.environ.get("SIGNALPOST_INPUT"),
                   help="JSON/JSONL/text list of organisation numbers")
    p.add_argument("--output", "-o", default=os.environ.get("SIGNALPOST_OUTPUT", "out/envelopes.jsonl"))
    p.add_argument("--report", default=None, help="run report JSON (default: next to output)")
    p.add_argument("--site", default=None, help="static HTML viewer directory (default: <output dir>/site)")
    p.add_argument("--run-id", default=None)
    p.add_argument("--expected-count", type=int, default=None)
    p.add_argument("--bulk", default=os.environ.get("SIGNALPOST_BULK"), help="optional frozen Brreg entity snapshot (CSV or CSV.gz)")
    p.add_argument("--previous", default=None, help="previous envelopes JSONL for refresh diffs")
    p.add_argument("--state-dir", default=os.environ.get("SIGNALPOST_STATE_DIR", "state"))
    p.add_argument("--workers", type=int, default=int(os.environ.get("SIGNALPOST_WORKERS", "16")))
    p.add_argument("--time-budget", type=float, default=float(os.environ.get("SIGNALPOST_TIME_BUDGET", "3000")),
                   help="seconds for the whole batch; unfinished companies still receive a terminal envelope")
    p.add_argument("--company-budget", type=float, default=float(os.environ.get("SIGNALPOST_COMPANY_BUDGET", "75")))
    p.add_argument("--nav-window-days", type=int, default=120)
    p.add_argument("--no-nav", action="store_true")
    p.add_argument("--no-site", action="store_true")
    p.add_argument("--profiles-output", default=None, help="accepted for starter compatibility (ignored)")
    p.add_argument("--resume", action="store_true", help="reuse envelopes of this run id already in --output")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)
    if not args.organisations:
        p.error("--organisations is required (or set SIGNALPOST_INPUT)")

    t0 = time.monotonic()
    started_at = utc_now()
    run_id = args.run_id or "run-" + started_at.replace(":", "").replace("-", "")
    orgs = read_inputs(args.organisations)
    if args.expected_count is not None and len(orgs) != args.expected_count:
        print(f"warning: expected {args.expected_count} organisations, read {len(orgs)}", file=sys.stderr)
    output = Path(args.output)
    report_path = Path(args.report) if args.report else output.with_name("run-report.json")
    global_deadline = t0 + args.time_budget - 20

    fetcher = Fetcher(gate=HostGate(per_host=2, min_interval=0.2))
    registry_gate = HostGate(per_host=8, min_interval=0.0)
    fetcher.gate = _RoutingGate(fetcher.gate, registry_gate)
    bulk = BulkRegistry(args.bulk, set(orgs)) if args.bulk else None
    previous = load_previous(args.previous, args.state_dir, orgs)
    nav = None
    if not args.no_nav:
        nav = NavJobIndex(fetcher, window_days=args.nav_window_days, time_limit=min(480.0, args.time_budget * 0.3),
                          detail_limit=min(600.0, args.time_budget * 0.4),
                          cache_path=os.path.join(args.state_dir, "nav-ad-orgnr-cache.json") if args.state_dir else None)
        nav.start()

    done: dict[str, dict] = {}
    if args.resume and output.exists():
        for line in output.read_text(encoding="utf-8").splitlines():
            if line.strip():
                env = json.loads(line)
                if env.get("run", {}).get("run_id") == run_id and env.get("organisation_number") in orgs:
                    done[env["organisation_number"]] = env

    def research(org: str):
        if time.monotonic() > global_deadline:
            return failed_envelope(org, run_id, "batch time budget exhausted before this company started", previous.get(org))
        cf = CountingFetcher(fetcher)
        return research_company(org, fetcher=cf, bulk=bulk, previous=previous.get(org), budget_seconds=args.company_budget,
                                global_deadline=global_deadline, use_sites=not args.no_site)

    def finalize(work) -> dict:
        if isinstance(work, dict):
            return work
        return finalize_company(work, run_id=run_id, nav=nav, deadline=min(global_deadline, time.monotonic() + 60))

    pending = [o for o in orgs if o not in done]
    works: dict[str, object] = {}
    checkpoint = output.with_suffix(".partial.jsonl")
    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    futures = {pool.submit(research, org): org for org in pending}
    not_done = set(futures)
    while not_done:
        remaining = global_deadline - 60 - time.monotonic()
        finished, not_done = wait(not_done, timeout=max(1.0, min(30.0, remaining)), return_when=FIRST_COMPLETED)
        for fut in finished:
            org = futures[fut]
            try:
                works[org] = fut.result()
            except Exception as exc:  # noqa: BLE001
                works[org] = failed_envelope(org, run_id, f"{type(exc).__name__}: {exc}", previous.get(org))
        print(f"[signalpost] research {len(works)}/{len(pending)} companies, {fetcher.requests} requests, "
              f"{int(time.monotonic() - t0)}s", file=sys.stderr, flush=True)
        if remaining < 0:
            break
    for fut in not_done:
        fut.cancel()
        org = futures[fut]
        works.setdefault(org, failed_envelope(org, run_id, "company research did not finish within the batch time budget", previous.get(org)))
    if nav is not None:
        nav.ready.wait(timeout=max(0.0, global_deadline - 45 - time.monotonic()))
    # Finalisation runs in its own pool so it never queues behind research threads that are still busy.
    fin_pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    fin_futures = {fin_pool.submit(finalize, works[org]): org for org in pending}
    finished, _ = wait(set(fin_futures), timeout=max(1.0, global_deadline + 10 - time.monotonic()))
    for fut, org in fin_futures.items():
        if fut in finished:
            try:
                done[org] = fut.result()
                continue
            except Exception as exc:  # noqa: BLE001
                reason = f"finalisation failed: {type(exc).__name__}: {exc}"
        else:
            fut.cancel()
            reason = "finalisation did not finish within the batch time budget"
        done[org] = failed_envelope(org, run_id, reason, previous.get(org))
    fin_pool.shutdown(wait=False, cancel_futures=True)
    pool.shutdown(wait=False, cancel_futures=True)

    envelopes = [done[o] for o in orgs]
    write_jsonl(output, envelopes)
    if checkpoint.exists():
        checkpoint.unlink()
    if args.state_dir:
        save_state(args.state_dir, envelopes)
    report = build_report(envelopes, orgs, run_id, started_at, t0, fetcher, nav, args, output)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        from .viewer import write_site
        write_site(envelopes, report, Path(args.site) if args.site else output.parent / "site")
    except Exception as exc:  # noqa: BLE001 - the viewer must never invalidate a batch
        print(f"warning: viewer generation failed: {exc}", file=sys.stderr)
    print(json.dumps({k: report[k] for k in ("run_id", "input_count", "emitted_envelopes", "validation", "runtime_seconds")}, indent=2))
    return 0 if report["validation"]["passed"] else 1


class _RoutingGate:
    """Registry APIs get their own wider gate; everything else uses polite per-host limits."""

    def __init__(self, default: HostGate, registry: HostGate):
        self.default = default
        self.registry = registry

    def acquire(self, host: str):
        gate = self.registry if host.endswith("brreg.no") or host.endswith("nav.no") else self.default
        return gate.acquire(host)


def build_report(envelopes, orgs, run_id, started_at, t0, fetcher, nav, args, output) -> dict:
    states = {}
    terminal = {}
    families: dict[str, dict] = {}
    runtimes = sorted(e["operations"].get("runtime_ms") or 0 for e in envelopes)
    for env in envelopes:
        states[env["state"]] = states.get(env["state"], 0) + 1
        t = env["run"]["terminal_status"]
        terminal[t] = terminal.get(t, 0) + 1
        for fam, info in env["field_families"].items():
            families.setdefault(fam, {}).setdefault(info["state"], 0)
            families[fam][info["state"]] += 1
    emitted = [e["organisation_number"] for e in envelopes]
    checks = {
        "one_envelope_per_input": len(envelopes) == len(orgs) and emitted == orgs,
        "unique_organisation_numbers": len(set(emitted)) == len(emitted),
        "valid_states": all(e["state"] in ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed") for e in envelopes),
        "every_available_claim_has_evidence": all(
            c["evidence_ids"] and all(x in {ev["id"] for ev in e["evidence"]} for x in c["evidence_ids"])
            for e in envelopes for c in e["claims"] if c["availability"] == "available"),
        "unavailable_claims_carry_no_value": all(c["value"] is None for e in envelopes for c in e["claims"] if c["availability"] != "available"),
        "every_evidence_has_source_and_time": all(ev.get("source_url") and ev.get("retrieved_at") for e in envelopes for ev in e["evidence"]),
    }
    return {
        "run_id": run_id,
        "agent": f"signalpost-agent {__version__}",
        "started_at": started_at,
        "completed_at": utc_now(),
        "runtime_seconds": round(time.monotonic() - t0, 1),
        "input_count": len(orgs),
        "emitted_envelopes": len(envelopes),
        "output": str(output),
        "envelope_states": states,
        "terminal_status": terminal,
        "field_family_states": families,
        "companies_with_verified_website": sum(1 for e in envelopes if e["field_families"].get("official_website", {}).get("state") == "available"),
        "claims_total": sum(1 for e in envelopes for c in e["claims"] if c["availability"] == "available"),
        "changes_total": sum(len(e["changes"]) for e in envelopes),
        "operations": {
            "requests": fetcher.requests,
            "bytes": fetcher.bytes,
            "p50_company_ms": runtimes[len(runtimes) // 2] if runtimes else None,
            "p95_company_ms": runtimes[min(len(runtimes) - 1, int(len(runtimes) * 0.95))] if runtimes else None,
            "third_party_cost_usd": 0.0,
            "nav_index": {"state": nav.state, "pages": nav.pages, "active_ads": len(nav.active), "details_fetched": nav.details_fetched,
                          "details_cached": nav.details_cached, "employers": len(nav.by_org), "error": nav.error} if nav else None,
        },
        "settings": {"workers": args.workers, "time_budget_s": args.time_budget, "company_budget_s": args.company_budget,
                     "nav_window_days": args.nav_window_days, "bulk_snapshot": bool(args.bulk)},
        "validation": {"passed": all(checks.values()), "checks": checks},
    }


if __name__ == "__main__":
    raise SystemExit(main())
