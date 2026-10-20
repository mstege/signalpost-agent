"""Claims and evidence for one company profile.

A claim is published only with at least one evidence record. Evidence records point at an
immutable snapshot (URL + retrieval time + content hash) and the exact span that supports the
claim. Claim ids are stable hashes of (field, normalised value) so refreshes are idempotent.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .net import Snapshot

STATES = ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed")

# Section ids follow the seven required envelope sections.
SECTIONS = {
    "identity": "Legal identity and public brand",
    "financials": "Latest annual accounts and available history",
    "leadership_locations": "Leadership and registered workplaces",
    "web_presence": "Verified official website and company-owned profiles",
    "activity": "Hiring and dated public activity",
}


def stable_id(*parts: Any) -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def norm_value(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).strip()
    return value


class Ledger:
    """Collects evidence and claims for one organisation."""

    def __init__(self, org: str):
        self.org = org
        self.evidence: dict[str, dict] = {}
        self.claims: dict[str, dict] = {}
        self.field_states: dict[str, dict] = {}
        self.errors: list[dict] = []
        self.candidates: list[dict] = []

    # evidence -----------------------------------------------------------
    def add_evidence(self, snap: Snapshot | None, *, source_class: str, span: str | None, method: str,
                     source_url: str | None = None, reporting_period: str | None = None,
                     effective_date: str | None = None) -> str:
        url = source_url or (snap.final_url or snap.requested_url if snap else None)
        record = {
            "source_url": url,
            "requested_url": snap.requested_url if snap else url,
            "redirects": snap.redirects if snap else [],
            "http_status": snap.status if snap else None,
            "source_class": source_class,
            "retrieved_at": snap.retrieved_at if snap else None,
            "content_sha256": snap.content_sha256 if snap else None,
            "claim_span": (span or "")[:500] or None,
            "extraction_method": method,
            "reporting_period": reporting_period,
            "effective_date": effective_date,
        }
        ev_id = "ev-" + stable_id(record["source_url"], record["content_sha256"], record["claim_span"], method)
        record["id"] = ev_id
        self.evidence.setdefault(ev_id, record)
        return ev_id

    # claims -------------------------------------------------------------
    def claim(self, section: str, field: str, value: Any, evidence_ids: list[str], *, confidence: float = 0.99,
              reporting_period: str | None = None, effective_date: str | None = None, key: Any = None,
              note: str | None = None, relation: str = "exact_entity") -> str | None:
        value = norm_value(value)
        if value in (None, "", [], {}):
            return None
        if not evidence_ids:
            raise ValueError(f"claim without evidence: {field}")
        claim_id = "cl-" + stable_id(self.org, field, key if key is not None else value)
        existing = self.claims.get(claim_id)
        if existing:
            for ev in evidence_ids:
                if ev not in existing["evidence_ids"]:
                    existing["evidence_ids"].append(ev)
            existing["confidence"] = max(existing["confidence"], confidence)
            return claim_id
        record = {
            "id": claim_id,
            "section": section,
            "field": field,
            "value": value,
            "availability": "available",
            "confidence": round(confidence, 3),
            "relation": relation,
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
        }
        if reporting_period:
            record["reporting_period"] = reporting_period
        if effective_date:
            record["effective_date"] = effective_date
        if note:
            record["note"] = note
        self.claims[claim_id] = record
        self.mark(field, "available")
        return claim_id

    def mark(self, field: str, state: str, reason: str | None = None, *, checked: list[str] | None = None) -> None:
        """Record the availability state of a field family. 'available' always wins."""
        assert state in STATES, state
        current = self.field_states.get(field)
        if current and current["state"] == "available" and state != "available":
            return
        entry = {"state": state}
        if reason:
            entry["reason"] = reason
        if checked:
            entry["sources_checked"] = sorted(set(checked))[:20]
        if current and state != "available" and current.get("sources_checked"):
            entry["sources_checked"] = sorted(set(current["sources_checked"]) | set(checked or []))[:20]
        self.field_states[field] = entry

    def error(self, stage: str, message: str, url: str | None = None) -> None:
        self.errors.append({"stage": stage, "message": message[:300], **({"url": url} if url else {})})

    def claims_for(self, field: str) -> list[dict]:
        return [c for c in self.claims.values() if c["field"] == field]
