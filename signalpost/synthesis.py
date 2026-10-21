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


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fin_claim(ledger: Ledger, field: str, period: str) -> dict | None:
    return next((c for c in ledger.claims_for(field) if c.get("reporting_period") == period), None)


def _ratio_text(value: float) -> str:
    return f"{value * 100:.0f}%" if abs(value) >= 0.095 else f"{value * 100:.1f}%"


def derived_metrics(ctx: dict, ledger: Ledger) -> list[dict]:
    """Ratios computed from filed accounts. Each states its formula and cites the claims it uses."""
    accounts = ctx.get("accounts") or []
    out: list[dict] = []
    if not accounts:
        return out
    latest = accounts[-1]
    period, year = latest["period"], latest["year"]
    revenue, op, net = _num(latest.get("revenue")), _num(latest.get("operating_result")), _num(latest.get("net_result"))
    equity, assets = _num(latest.get("equity")), _num(latest.get("total_assets"))

    def ids(*fields: str, p: str = period) -> list[str]:
        return [c["id"] for c in (_fin_claim(ledger, f, p) for f in fields) if c]

    if revenue and revenue > 0 and op is not None:
        out.append({"metric": "operating_margin", "year": year, "value": round(op / revenue, 4),
                    "text": f"Operating margin {year}: {_ratio_text(op / revenue)}", "formula": "operating result / revenue",
                    "claim_ids": ids("operating_result", "revenue")})
    if revenue and revenue > 0 and net is not None:
        out.append({"metric": "net_margin", "year": year, "value": round(net / revenue, 4),
                    "text": f"Net margin {year}: {_ratio_text(net / revenue)}", "formula": "net result / revenue",
                    "claim_ids": ids("net_result", "revenue")})
    if assets and assets > 0 and equity is not None:
        out.append({"metric": "equity_ratio", "year": year, "value": round(equity / assets, 4),
                    "text": f"Equity ratio {year}: {_ratio_text(equity / assets)}", "formula": "equity / total assets",
                    "claim_ids": ids("equity", "total_assets")})
    employees = first_claim(ledger, "registered_employees")
    emp = _num(employees["value"]) if employees else None
    if revenue and emp and emp >= 1:
        out.append({"metric": "revenue_per_employee", "year": year, "value": round(revenue / emp),
                    "text": f"Revenue per registered employee {year}: {_fmt_money(revenue / emp, latest['currency'])}",
                    "formula": "revenue / employees in the register (register count may be dated later than the accounts)",
                    "claim_ids": ids("revenue") + [employees["id"]]})
    with_rev = [a for a in accounts if _num(a.get("revenue")) is not None]
    if len(with_rev) >= 2:
        first, last = with_rev[0], with_rev[-1]
        r0, r1 = _num(first["revenue"]), _num(last["revenue"])
        n = int(last["year"]) - int(first["year"])
        if r0 and r0 > 0 and r1 is not None and r1 > 0 and n >= 1:
            cagr = (r1 / r0) ** (1 / n) - 1
            out.append({"metric": "revenue_cagr", "year": f"{first['year']}–{last['year']}", "value": round(cagr, 4),
                        "text": f"Revenue growth {first['year']}–{last['year']}: {cagr * 100:+.0f}% per year",
                        "formula": "(latest revenue / earliest revenue)^(1/years) − 1",
                        "claim_ids": ids("revenue", p=first["period"]) + ids("revenue", p=last["period"])})
    return [m for m in out if m["claim_ids"]]


def first_claim(ledger: Ledger, field: str) -> dict | None:
    items = ledger.claims_for(field)
    return items[0] if items else None


def key_points(ctx: dict, ledger: Ledger, metrics: list[dict], today: str) -> list[dict]:
    """Short decision-useful observations. Each is a direct reading of cited claims, never a guess."""
    points: list[dict] = []

    def point(kind: str, text: str, ids: list) -> None:
        ids = [i for i in ids if i]
        if ids:
            points.append({"kind": kind, "text": text, "claim_ids": list(dict.fromkeys(ids))})

    accounts = ctx.get("accounts") or []
    with_net = [a for a in accounts if _num(a.get("net_result")) is not None]
    if with_net:
        net_ids = [c["id"] for a in with_net for c in [_fin_claim(ledger, "net_result", a["period"])] if c]
        years = f"{with_net[0]['year']}–{with_net[-1]['year']}" if len(with_net) > 1 else with_net[0]["year"]
        signs = [_num(a["net_result"]) >= 0 for a in with_net]
        if len(signs) == 1:
            point("financial", f"{'Profitable' if signs[0] else 'Net loss'} in {years}, the only filed year available.", net_ids)
        elif all(signs):
            point("financial", f"Profitable in every filed year shown ({years}).", net_ids)
        elif not any(signs):
            point("financial", f"Net loss in every filed year shown ({years}).", net_ids)
        elif signs[-1] and not signs[-2]:
            point("financial", f"Returned to profit in {with_net[-1]['year']} after a loss in {with_net[-2]['year']}.", net_ids[-2:])
        elif not signs[-1]:
            point("financial", f"Net loss in {with_net[-1]['year']} after a profit in {with_net[-2]['year']}.", net_ids[-2:])
    by_metric = {m["metric"]: m for m in metrics}
    if "revenue_cagr" in by_metric:
        m = by_metric["revenue_cagr"]
        trend = "growing" if m["value"] > 0.05 else "shrinking" if m["value"] < -0.05 else "roughly flat"
        point("financial", f"Revenue is {trend} ({m['text'].split(': ', 1)[1]}, {m['year']}).", m["claim_ids"])
    if "equity_ratio" in by_metric:
        m = by_metric["equity_ratio"]
        if m["value"] < 0:
            point("risk", f"Negative equity in {m['year']}: liabilities exceed assets ({m['text'].split(': ', 1)[1]} equity ratio).", m["claim_ids"])
        elif m["value"] < 0.1:
            point("risk", f"Thin equity buffer in {m['year']} ({m['text'].split(': ', 1)[1]} equity ratio).", m["claim_ids"])
        elif m["value"] >= 0.4:
            point("financial", f"Solid equity buffer in {m['year']} ({m['text'].split(': ', 1)[1]} equity ratio).", m["claim_ids"])
    latest_year = first_claim(ledger, "latest_filed_accounts_year")
    if latest_year and today[:4].isdigit() and int(today[:4]) - int(str(latest_year["value"])[:4]) >= 2:
        point("risk", f"Latest filed accounts are for {latest_year['value']}; newer figures are not in the register.", [latest_year["id"]])
    status = first_claim(ledger, "legal_status")
    if status and "active" not in str(status["value"]):
        point("risk", f"Register status: {status['value']}.", [status["id"]])

    roles = ctx.get("roles") or []
    ceo = next((r for r in roles if r.get("role_code") == "DAGL"), None)
    chair = next((r for r in roles if r.get("role_code") == "LEDE"), None)
    leaders = ledger.claims_for("leadership")
    if ceo and chair and ceo.get("name") and ceo["name"] == chair.get("name"):
        ids = [c["id"] for c in leaders if ceo["name"] in str(c["value"]) and str(c["value"]).startswith(("Daglig leder", "Styrets leder"))]
        point("leadership", f"{ceo['name']} is both CEO and board chair.", ids)
    elif not ceo and chair:
        ids = [c["id"] for c in leaders if str(c["value"]).startswith("Styrets leder")]
        point("leadership", f"No CEO (daglig leder) is registered; the board is chaired by {chair['name']}.", ids)
    cutoff = f"{int(today[:4]) - 1}{today[4:10]}" if today[:4].isdigit() else ""
    for ev in sorted(ledger.claims_for("registry_event"), key=lambda c: c["value"]["date"], reverse=True):
        text = ev["value"]["event"]
        if cutoff and ev["value"]["date"] >= cutoff and text.startswith("Role group last changed") and \
                any(g in text for g in ("Daglig leder", "Styre", "Innehaver")):
            point("leadership", f"Leadership register updated within the last 12 months ({ev['value']['date']}: {text.split(': ', 1)[1]}).", [ev["id"]])
            break
    workplaces = ledger.claims_for("registered_workplace")
    if len(workplaces) >= 2:
        point("operations", f"Operates from {len(workplaces)} registered workplaces.", [c["id"] for c in workplaces[:5]])

    site = first_claim(ledger, "official_website")
    socials = ledger.claims_for("social_profile")
    news = sorted(ledger.claims_for("company_news"), key=lambda c: c["value"]["date"], reverse=True)
    jobs = ledger.claims_for("job_posting")
    if jobs:
        point("activity", f"Hiring: {len(jobs)} job posting(s) found in permitted sources.", [j["id"] for j in jobs[:8]])
    if news and cutoff and news[0]["value"]["date"] >= cutoff:
        point("activity", f"Recently active publicly: company news dated {news[0]['value']['date']}.", [news[0]["id"]])
    if site and site.get("relation") == "registry_declared":
        point("footprint", "Website is declared in the register but could not be verified on the page; its content is not used.", [site["id"]])
    elif site:
        point("footprint", f"Verified company website{' with ' + str(len(socials)) + ' linked social profile(s)' if socials else ''}.",
              [site["id"]] + [s["id"] for s in socials[:6]])
    code = str(ctx.get("industry_code") or "")
    industry = first_claim(ledger, "industry")
    if industry and code.startswith("64.2"):
        point("context", "Registered as a holding company: its own accounts may not reflect the activity of companies it owns.", [industry["id"]])
    elif industry and code.startswith("68.2"):
        point("context", "Registered for letting of own or leased real estate: revenue is typically rental income.", [industry["id"]])
    return points


def headline(ctx: dict, ledger: Ledger, metrics: list[dict]) -> dict | None:
    name = first_claim(ledger, "legal_name")
    if not name:
        return None
    ids = [name["id"]]
    accounts = ctx.get("accounts") or []
    parts = []
    employees = first_claim(ledger, "registered_employees")
    industry = first_claim(ledger, "industry")
    muni = first_claim(ledger, "municipality")
    desc = (ctx.get("industry") or "").strip()
    where = f" ({str(muni['value']).title()})" if muni else ""
    parts.append(f"{name['value']}{where}: {desc or 'registered entity'}")
    if industry:
        ids.append(industry["id"])
    if muni:
        ids.append(muni["id"])
    if accounts and _num(accounts[-1].get("revenue")) is not None:
        latest = accounts[-1]
        rev = _fin_claim(ledger, "revenue", latest["period"])
        text = f"revenue {_fmt_money(latest['revenue'], latest['currency'])} in {latest['year']}"
        net = _num(latest.get("net_result"))
        if net is not None:
            text += ", profitable" if net >= 0 else ", loss-making"
            nc = _fin_claim(ledger, "net_result", latest["period"])
            ids += [nc["id"]] if nc else []
        parts.append(text)
        ids += [rev["id"]] if rev else []
    if employees:
        parts.append(f"{employees['value']} registered employees")
        ids.append(employees["id"])
    return {"text": "; ".join(parts) + ".", "claim_ids": ids}


def build_synthesis(ctx: dict, ledger: Ledger, changes: list[dict], today: str = "") -> dict:
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
        declared = site.get("relation") == "registry_declared"
        text = f"{'Website declared in the register (not verified on the page)' if declared else 'Verified website'}: {site['value']}"
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
    metrics = derived_metrics(ctx, ledger)
    head = headline(ctx, ledger, metrics)
    return {
        "method": "deterministic template over published claims (no language model); derived ratios state their formula",
        "headline": head,
        "summary": " ".join(s["text"] for s in sentences),
        "sentences": sentences,
        "key_points": key_points(ctx, ledger, metrics, today or ""),
        "derived_metrics": metrics,
        "what_changed": change_lines,
        "recent_official_events": recent_events,
        "unknowns": unknowns,
    }
