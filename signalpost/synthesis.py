"""Evidence-bounded synthesis: dated official events and a short decision-useful brief.

Every sentence cites the claim ids it rests on. Nothing is inferred beyond the claims; missing
families are listed as unknowns instead of being guessed.
"""
from __future__ import annotations

from .evidence import Ledger

FAMILY_LABELS = {
    "official_website": "official website",
    "social_profile": "company-owned social profiles",
    "job_posting": "job postings",
    "company_news": "dated company news",
    "annual_accounts": "structured annual accounts",
    "leadership": "registered leadership",
    "contact_email": "contact e-mail",
    "contact_phone": "contact phone",
}


def official_events(ctx: dict, ledger: Ledger) -> None:
    """Dated public activity that the official registers themselves record."""
    entity = ctx.get("entity") or {}
    snap = ctx.get("entity_snapshot")
    if not entity or snap is None:
        return

    def event(date: str | None, text: str, path: str) -> None:
        if not date:
            return
        date = str(date)[:10]
        ev = ledger.add_evidence(snap, source_class="official_registry", method="json_path", span=f"{path} = {date}", effective_date=date)
        ledger.claim("activity", "registry_event", {"date": date, "event": text}, [ev], key=("event", path, date), effective_date=date)

    event(entity.get("stiftelsesdato"), "Company founded (stiftelsesdato)", "$.stiftelsesdato")
    event(entity.get("registreringsdatoEnhetsregisteret"), "Registered in the Central Coordinating Register", "$.registreringsdatoEnhetsregisteret")
    event(entity.get("registreringsdatoForetaksregisteret"), "Registered in the Register of Business Enterprises", "$.registreringsdatoForetaksregisteret")
    event(entity.get("registreringsdatoMerverdiavgiftsregisteret"), "Registered in the VAT register", "$.registreringsdatoMerverdiavgiftsregisteret")
    event(entity.get("vedtektsdato"), "Articles of association dated", "$.vedtektsdato")
    cap = entity.get("kapital") or {}
    if cap.get("innfortDato"):
        event(cap.get("innfortDato"), f"Share capital registered at {cap.get('belop'):,.0f} {cap.get('valuta') or 'NOK'}".replace(",", " "),
              "$.kapital.innfortDato")
    for item in entity.get("historiskeNavn") or []:
        if item.get("tilDato") and item.get("navn"):
            event(item["tilDato"], f"Name changed from {item['navn']}", "$.historiskeNavn.tilDato")
    if entity.get("registreringsdatoAntallAnsatteNAVAaregisteret") and entity.get("antallAnsatte") is not None:
        event(entity["registreringsdatoAntallAnsatteNAVAaregisteret"], f"Employee count updated to {entity['antallAnsatte']} (NAV Aa-register)",
              "$.registreringsdatoAntallAnsatteNAVAaregisteret")
    for role_change in sorted({r.get("last_changed") for r in ctx.get("roles") or [] if r.get("last_changed")}, reverse=True)[:3]:
        groups = sorted({r["group"] for r in ctx["roles"] if r.get("last_changed") == role_change and r.get("group")})
        event(role_change, f"Role group last changed: {', '.join(groups)}", "$.rollegrupper.sistEndret")
    if entity.get("sisteInnsendteAarsregnskap"):
        ev = ledger.add_evidence(snap, source_class="official_registry", method="json_path",
                                 span=f"$.sisteInnsendteAarsregnskap = {entity['sisteInnsendteAarsregnskap']}")
        ledger.claim("financials", "latest_filed_accounts_year", str(entity["sisteInnsendteAarsregnskap"]), [ev], key="latest_filed")


FORM_NAMES = {"AS": "private limited company", "ASA": "public limited company", "ENK": "sole proprietorship", "NUF": "Norwegian branch of a foreign company",
              "SA": "cooperative", "DA": "partnership with shared liability", "ANS": "general partnership", "STI": "foundation",
              "BRL": "housing cooperative", "FLI": "association", "KS": "limited partnership", "ESEK": "jointly owned property",
              "SF": "state enterprise", "IKS": "inter-municipal company", "KF": "municipal enterprise", "BA": "company with limited liability",
              "ORGL": "organisational unit", "SPA": "savings bank", "PK": "pension fund", "BBL": "housing building cooperative"}


def _fmt_money(amount, currency: str) -> str:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1e9:
        text = f"{value / 1e9:.1f} bn"
    elif value >= 1e6:
        text = f"{value / 1e6:.1f} m"
    elif value >= 1e3:
        text = f"{value / 1e3:.0f} k"
    else:
        text = f"{value:.0f}"
    return f"{sign}{currency} {text}"


def _pct(new, old) -> str | None:
    try:
        new, old = float(new), float(old)
    except (TypeError, ValueError):
        return None
    if old == 0:
        return None
    change = (new - old) / abs(old) * 100
    return f"{change:+.0f}%"


def build_synthesis(ctx: dict, ledger: Ledger, changes: list[dict]) -> dict:
    sentences: list[dict] = []

    def say(text: str, *claim_ids: str | None) -> None:
        ids = [c for c in claim_ids if c]
        if ids:
            sentences.append({"text": text, "claim_ids": ids})

    def first(field: str) -> dict | None:
        items = ledger.claims_for(field)
        return items[0] if items else None

    name = first("legal_name")
    form = first("legal_form")
    muni = first("municipality")
    industry = first("industry")
    purpose = first("stated_purpose")
    founded = first("founded_date") or first("registered_date")
    brand = first("public_brand")
    if name:
        text = f"{name['value']}"
        if form:
            code = form["value"].split(" (")[0]
            text += f" is a {FORM_NAMES.get(code, form['value'].split(' (')[-1].rstrip(')').lower())} ({code})"
        else:
            text += " is a registered entity"
        if muni:
            text += f" based in {muni['value'].title()}"
        if founded:
            text += f", founded {founded['value'][:4]}"
        text += "."
        say(text, name["id"], form and form["id"], muni and muni["id"], founded and founded["id"])
    if industry:
        say(f"Registered industry: {industry['value'][7:].strip() or industry['value']} (NACE {industry['value'].split()[0]}).", industry["id"])
    desc = first("website_description")
    if desc:
        say(f"Its website describes it as: “{desc['value'][:220]}”", desc["id"])
    elif purpose:
        say(f"Registered purpose: {purpose['value'][:220]}", purpose["id"])
    if brand and name and brand["value"].lower() not in name["value"].lower():
        say(f"It presents itself publicly as “{brand['value']}”.", brand["id"])

    accounts = ctx.get("accounts") or []
    if accounts:
        latest = accounts[-1]
        prev = accounts[-2] if len(accounts) > 1 else None
        ids = [c["id"] for c in ledger.claims.values() if c["section"] == "financials" and c.get("reporting_period") == latest["period"]]
        parts = []
        if latest.get("revenue") is not None:
            p = f"revenue {_fmt_money(latest['revenue'], latest['currency'])}"
            if prev and prev.get("revenue") is not None and _pct(latest["revenue"], prev["revenue"]):
                p += f" ({_pct(latest['revenue'], prev['revenue'])} vs {prev['year']})"
            parts.append(p)
        if latest.get("operating_result") is not None:
            parts.append(f"operating result {_fmt_money(latest['operating_result'], latest['currency'])}")
        if latest.get("net_result") is not None:
            parts.append(f"net result {_fmt_money(latest['net_result'], latest['currency'])}")
        if latest.get("equity") is not None:
            parts.append(f"equity {_fmt_money(latest['equity'], latest['currency'])}")
        if parts:
            prev_ids = [c["id"] for c in ledger.claims.values() if prev and c["section"] == "financials" and c.get("reporting_period") == prev["period"]
                        and c["field"] == "revenue"]
            say(f"Latest filed accounts ({latest['year']}): " + ", ".join(parts) + ".", *(ids + prev_ids))
        if latest.get("equity") is not None and float(latest["equity"]) < 0:
            eq = [c["id"] for c in ledger.claims.values() if c["field"] == "equity" and c.get("reporting_period") == latest["period"]]
            say(f"Equity was negative at the end of {latest['year']}.", *eq)
    employees = first("registered_employees")
    if employees:
        say(f"The register lists {employees['value']} employees.", employees["id"])
    ceo = next((c for c in ledger.claims_for("leadership") if c["value"].startswith("Daglig leder")), None)
    chair = next((c for c in ledger.claims_for("leadership") if c["value"].startswith("Styrets leder")), None)
    if ceo or chair:
        bits = []
        if ceo:
            bits.append(f"CEO {ceo['value'].split(': ', 1)[-1]}")
        if chair:
            bits.append(f"chair {chair['value'].split(': ', 1)[-1]}")
        say("Leadership: " + " and ".join(bits) + ".", ceo and ceo["id"], chair and chair["id"])
    workplaces = ledger.claims_for("registered_workplace")
    if workplaces:
        say(f"{len(workplaces)} registered workplace(s) (subunits).", *[c["id"] for c in workplaces[:5]])
    site = first("official_website")
    socials = ledger.claims_for("social_profile")
    if site:
        text = f"Verified website: {site['value']}"
        if socials:
            text += f", linking {len(socials)} company-owned profile(s) ({', '.join(sorted({s['value']['platform'] for s in socials}))})"
        say(text + ".", site["id"], *[s["id"] for s in socials[:6]])
    jobs = ledger.claims_for("job_posting")
    if jobs:
        nav = [j for j in jobs if j["value"].get("source", "").startswith("NAV")]
        say(f"Hiring signal: {len(jobs)} job posting(s) found" + (f", {len(nav)} active on NAV arbeidsplassen" if nav else "") + ".",
            *[j["id"] for j in jobs[:8]])
    news = sorted(ledger.claims_for("company_news"), key=lambda c: c["value"]["date"], reverse=True)
    if news:
        say(f"Most recent company news ({news[0]['value']['date']}): “{news[0]['value']['title'][:120]}”.", news[0]["id"])
    status = first("legal_status")
    if status and "active" not in status["value"]:
        say(f"Warning: the register marks the company as {status['value']}.", status["id"])

    change_lines = []
    for ch in changes[:10]:
        change_lines.append({"text": ch["summary"], "claim_ids": [x for x in (ch.get("claim_id"), ch.get("previous_claim_id")) if x]})
    events = sorted(ledger.claims_for("registry_event"), key=lambda c: c["value"]["date"], reverse=True)
    recent_events = [{"text": f"{e['value']['date']}: {e['value']['event']}", "claim_ids": [e["id"]]} for e in events[:5]]

    unknowns = []
    for family, label in FAMILY_LABELS.items():
        state = ledger.field_states.get(family, {}).get("state")
        if state and state != "available":
            unknowns.append({"family": family, "state": state, "text": f"{label}: {state.replace('_', ' ')}"
                             + (f" – {ledger.field_states[family].get('reason')}" if ledger.field_states[family].get("reason") else "")})
    return {
        "method": "deterministic template over published claims (no language model)",
        "summary": " ".join(s["text"] for s in sentences),
        "sentences": sentences,
        "what_changed": change_lines,
        "recent_official_events": recent_events,
        "unknowns": unknowns,
    }
