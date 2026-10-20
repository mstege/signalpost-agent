"""Refresh as a diff against the previous envelope for the same organisation.

- Claim ids are stable hashes, so re-running on the same source snapshot yields no changes.
- Single-valued fields pair old and new values into one 'changed' event.
- When a source fails or is blocked this run, the previous supported claims for that family are
  carried forward (flagged) instead of being reported as removed.
- Prior evidence for changed or removed claims is preserved in the envelope history.
"""
from __future__ import annotations

import json
from pathlib import Path

SINGLE_VALUED = {
    "legal_name", "legal_form", "business_address", "postal_address", "municipality", "registered_employees", "share_capital",
    "registry_website", "registry_email", "registry_phone", "legal_status", "public_brand", "official_website", "stated_purpose",
    "latest_filed_accounts_year", "part_of_group", "registered_in", "website_description",
}
FIELD_FAMILY = {
    "leadership": "leadership", "registered_workplace": "registered_workplace", "job_posting": "job_posting",
    "company_news": "company_news", "social_profile": "official_website", "contact_email": "official_website",
    "contact_phone": "official_website", "website_location": "official_website", "person_on_website": "official_website",
    "leader_on_website": "official_website", "public_brand": "official_website", "website_description": "official_website",
    "careers_page": "official_website", "official_website": "official_website",
}
CHANGE_TYPES = {
    "leadership": ("new_role", "removed_role"),
    "registered_workplace": ("new_location", "closed_location"),
    "job_posting": ("new_job", "closed_job"),
    "company_news": ("new_news_item", "news_item_no_longer_listed"),
    "social_profile": ("new_social_profile", "social_profile_removed"),
    "revenue": ("new_filing", "filing_removed"),
    "registry_event": ("new_registry_event", "registry_event_removed"),
}
MATERIAL = {"new_role", "removed_role", "new_location", "closed_location", "new_job", "closed_job", "new_filing",
            "changed_legal_name", "changed_business_address", "changed_legal_status", "changed_official_website",
            "changed_registered_employees", "changed_share_capital", "changed_legal_form"}


def load_previous(path: str | None, state_dir: str | None, orgs: list[str]) -> dict[str, dict]:
    previous: dict[str, dict] = {}
    if path and Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                env = json.loads(line)
                if env.get("organisation_number") in orgs:
                    previous[env["organisation_number"]] = env
    elif state_dir:
        base = Path(state_dir) / "envelopes"
        for org in orgs:
            f = base / f"{org}.json"
            if f.exists():
                previous[org] = json.loads(f.read_text(encoding="utf-8"))
    return previous


def save_state(state_dir: str, envelopes: list[dict]) -> None:
    base = Path(state_dir) / "envelopes"
    base.mkdir(parents=True, exist_ok=True)
    for env in envelopes:
        tmp = base / f"{env['organisation_number']}.json.tmp"
        tmp.write_text(json.dumps(env, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(base / f"{env['organisation_number']}.json")


def _label(claim: dict) -> str:
    v = claim["value"]
    if isinstance(v, dict):
        if "title" in v:
            return v["title"]
        if "amount" in v:
            return f"{v['amount']} {v.get('currency', '')} ({v.get('year')})"
        if "platform" in v:
            return f"{v['platform']}: {v['url']}"
        if "event" in v:
            return f"{v['date']} {v['event']}"
        return json.dumps(v, ensure_ascii=False)[:120]
    return str(v)


def diff(ledger, previous: dict | None, run_time: str) -> tuple[list[dict], dict]:
    meta = {"previous_run_id": None, "previous_completed_at": None, "added": 0, "removed": 0, "changed": 0, "carried_forward": 0,
            "unchanged": 0}
    for claim in ledger.claims.values():
        claim["first_observed"] = run_time
        claim["last_observed"] = run_time
    if not previous:
        return [], meta
    meta["previous_run_id"] = (previous.get("run") or {}).get("run_id")
    meta["previous_completed_at"] = (previous.get("run") or {}).get("completed_at")
    prev_claims = {c["id"]: c for c in previous.get("claims") or [] if c.get("availability", "available") == "available"}
    prev_evidence = {e["id"]: e for e in previous.get("evidence") or []}
    changes: list[dict] = []
    current = ledger.claims

    for cid, claim in current.items():
        if cid in prev_claims:
            claim["first_observed"] = prev_claims[cid].get("first_observed") or meta["previous_completed_at"] or run_time
            if prev_claims[cid]["value"] != claim["value"]:
                changes.append(_change(f"changed_{claim['field']}", claim, prev_claims[cid], run_time))
            else:
                meta["unchanged"] += 1
    removed = {cid: c for cid, c in prev_claims.items() if cid not in current and not c.get("carried_forward")}
    added = {cid: c for cid, c in current.items() if cid not in prev_claims}
    # Pair single-valued fields
    for rid, old in list(removed.items()):
        if old["field"] in SINGLE_VALUED:
            match = next((a for a in added.values() if a["field"] == old["field"]), None)
            if match:
                changes.append(_change(f"changed_{old['field']}", match, old, run_time))
                added.pop(match["id"], None)
                removed.pop(rid, None)
    # Carry forward claims of families that failed/blocked this run
    for rid, old in list(removed.items()):
        family = FIELD_FAMILY.get(old["field"], old["field"])
        state = ledger.field_states.get(family, {}).get("state") or ledger.field_states.get(old["field"], {}).get("state")
        if state in ("failed", "blocked"):
            carried = dict(old)
            carried["carried_forward"] = True
            carried["note"] = f"source {state} on this run; last supported value kept from run {meta['previous_run_id']}"
            ledger.claims[rid] = carried
            for ev_id in carried["evidence_ids"]:
                if ev_id in prev_evidence:
                    ledger.evidence.setdefault(ev_id, prev_evidence[ev_id])
            meta["carried_forward"] += 1
            removed.pop(rid)
    for claim in added.values():
        kind = CHANGE_TYPES.get(claim["field"], (f"new_{claim['field']}", None))[0]
        changes.append(_change(kind, claim, None, run_time))
    history = []
    for old in removed.values():
        kind = CHANGE_TYPES.get(old["field"], (None, f"removed_{old['field']}"))[1]
        changes.append(_change(kind, None, old, run_time))
        history.append(old)
    for ch in changes:
        if ch.get("previous_claim_id") and ch["previous_claim_id"] in prev_claims:
            history.append(prev_claims[ch["previous_claim_id"]])
    meta["history_claims"] = []
    seen = set()
    for old in history:
        if old["id"] in seen:
            continue
        seen.add(old["id"])
        meta["history_claims"].append({**old, "superseded_at": run_time})
        for ev_id in old.get("evidence_ids") or []:
            if ev_id in prev_evidence and ev_id not in ledger.evidence:
                ledger.evidence[ev_id] = {**prev_evidence[ev_id], "superseded": True}
    meta["added"] = sum(1 for c in changes if c["change"] == "added")
    meta["removed"] = sum(1 for c in changes if c["change"] == "removed")
    meta["changed"] = sum(1 for c in changes if c["change"] == "changed")
    # Preserve history from earlier runs too
    for old_hist in (previous.get("refresh") or {}).get("history_claims") or []:
        if old_hist["id"] not in seen:
            meta["history_claims"].append(old_hist)
            seen.add(old_hist["id"])
            for ev_id in old_hist.get("evidence_ids") or []:
                if ev_id in prev_evidence and ev_id not in ledger.evidence:
                    ledger.evidence[ev_id] = {**prev_evidence[ev_id], "superseded": True}
    changes.sort(key=lambda c: (c["materiality"] != "material", c["type"]))
    return changes, meta


def _change(kind: str, new: dict | None, old: dict | None, run_time: str) -> dict:
    change = "changed" if new and old else "added" if new else "removed"
    field = (new or old)["field"]
    if change == "changed":
        summary = f"{field.replace('_', ' ')} changed from “{_label(old)}” to “{_label(new)}”"
    elif change == "added":
        summary = f"{kind.replace('_', ' ')}: {_label(new)}"
    else:
        summary = f"{kind.replace('_', ' ')}: {_label(old)}"
    return {
        "type": kind,
        "change": change,
        "field": field,
        "materiality": "material" if kind in MATERIAL else "minor",
        "detected_at": run_time,
        "claim_id": new["id"] if new else None,
        "previous_claim_id": old["id"] if old else None,
        "current_value": new["value"] if new else None,
        "previous_value": old["value"] if old else None,
        "evidence_ids": (new or {}).get("evidence_ids", []) + (old or {}).get("evidence_ids", []),
        "summary": summary,
    }
