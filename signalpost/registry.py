"""Official Norwegian records from Brønnøysundregistrene (open data, NLOD 2.0).

The organisation number is the identity anchor. Every registry claim cites the exact API URL,
retrieval time, response hash and the JSON path it came from.
"""
from __future__ import annotations

import csv
import gzip
import io
from pathlib import Path
from typing import Any

from .evidence import Ledger
from .net import Fetcher, Snapshot, utc_now

ENTITY = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
ROLES = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"
SUBUNITS = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=200"
ACCOUNTS = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
BULK_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"
JSON = "application/json"

LEADERSHIP_ORDER = ["DAGL", "LEDE", "NEST", "MEDL", "VARA", "OBS", "KONT", "INNH", "DTPR", "DTSO", "KOMP", "BEST", "REPR", "REVI", "REGN"]


def _get(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def address_text(addr: dict | None) -> str | None:
    if not isinstance(addr, dict):
        return None
    lines = [line for line in (addr.get("adresse") or []) if line]
    tail = " ".join(x for x in (addr.get("postnummer"), addr.get("poststed")) if x)
    parts = lines + ([tail] if tail else [])
    if addr.get("land") and addr.get("landkode") not in (None, "NO"):
        parts.append(addr["land"])
    return ", ".join(parts) or None


class BulkRegistry:
    """Optional frozen registry snapshot (Brreg CSV, plain or gzip) used when the live API fails."""

    def __init__(self, path: str | None, wanted: set[str]):
        self.rows: dict[str, dict] = {}
        self.path = path
        self.sha256 = None
        if not path:
            return
        import hashlib
        raw = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8-sig", "replace")
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=";,\t")
        for row in csv.DictReader(io.StringIO(text), dialect=dialect):
            org = (row.get("organisasjonsnummer") or row.get("Organisasjonsnummer") or "").strip()
            if org in wanted:
                self.rows[org] = row

    def entity(self, org: str) -> dict | None:
        row = self.rows.get(org)
        if not row:
            return None
        lower = {k.lower(): v for k, v in row.items()}

        def addr(prefix: str) -> dict | None:
            lines = [v for k, v in lower.items() if k.startswith(prefix + ".adresse") and v]
            out = {"adresse": lines, "postnummer": lower.get(prefix + ".postnummer"), "poststed": lower.get(prefix + ".poststed"),
                   "kommune": lower.get(prefix + ".kommune"), "kommunenummer": lower.get(prefix + ".kommunenummer"),
                   "land": lower.get(prefix + ".land"), "landkode": lower.get(prefix + ".landkode")}
            return out if any(out.values()) else None

        employees = lower.get("antallansatte")
        return {
            "organisasjonsnummer": org,
            "navn": lower.get("navn"),
            "organisasjonsform": {"kode": lower.get("organisasjonsform.kode"), "beskrivelse": lower.get("organisasjonsform.beskrivelse")},
            "hjemmeside": lower.get("hjemmeside"),
            "epostadresse": lower.get("epostadresse"),
            "telefon": lower.get("telefon"),
            "mobil": lower.get("mobil"),
            "forretningsadresse": addr("forretningsadresse"),
            "postadresse": addr("postadresse"),
            "naeringskode1": {"kode": lower.get("naeringskode1.kode"), "beskrivelse": lower.get("naeringskode1.beskrivelse")},
            "antallAnsatte": int(employees) if (employees or "").isdigit() else None,
            "registreringsdatoEnhetsregisteret": lower.get("registreringsdatoenhetsregisteret"),
            "stiftelsesdato": lower.get("stiftelsesdato"),
            "konkurs": (lower.get("konkurs") or "").lower() == "true",
            "underAvvikling": (lower.get("underavvikling") or "").lower() == "true",
            "sisteInnsendteAarsregnskap": lower.get("sisteinnsendteaarsregnskap"),
            "_source": "bulk",
        }


def fetch_registry(org: str, fetcher: Fetcher, ledger: Ledger, bulk: BulkRegistry | None = None) -> dict:
    """Fetch official modules and add claims. Returns a context dict used by later stages."""
    ctx: dict[str, Any] = {"organisation_number": org}
    snap = fetcher.get(ENTITY.format(org=org), accept=JSON, check_robots=False)
    entity = None
    if snap.ok:
        try:
            entity = snap.json()
        except ValueError:
            ledger.error("registry", "entity response was not JSON", snap.requested_url)
    elif snap.status in (404, 410):
        ledger.error("registry", f"entity not found in live register ({snap.status})", snap.requested_url)
    else:
        ledger.error("registry", f"entity fetch failed: {snap.error}", snap.requested_url)
    if entity is None and bulk is not None:
        entity = bulk.entity(org)
        if entity:
            snap = Snapshot(BULK_URL, BULK_URL, 200, utc_now(), content_sha256=bulk.sha256)
    if entity is None:
        ledger.mark("legal_identity", "failed", "registry entity unavailable")
        ctx["entity"] = None
        return ctx
    ctx["entity"] = entity
    ctx["entity_snapshot"] = snap
    _identity_claims(entity, snap, ledger, ctx)

    roles_snap = fetcher.get(ROLES.format(org=org), accept=JSON, check_robots=False)
    ctx["roles"] = _roles(roles_snap, ledger)
    sub_snap = fetcher.get(SUBUNITS.format(org=org), accept=JSON, check_robots=False)
    ctx["subunits"] = _subunits(sub_snap, ledger)
    acc_snap = fetcher.get(ACCOUNTS.format(org=org), accept=JSON, check_robots=False)
    ctx["accounts"] = _accounts(acc_snap, ledger, entity)
    return ctx


def _ev(ledger: Ledger, snap: Snapshot, path: str, value: Any, method: str = "json_path") -> str:
    return ledger.add_evidence(snap, source_class="official_registry", span=f"{path} = {value}", method=method)


def _identity_claims(e: dict, snap: Snapshot, ledger: Ledger, ctx: dict) -> None:
    sec = "identity"

    def c(field: str, path: str, value: Any, **kw) -> None:
        if value in (None, "", [], {}):
            return
        ledger.claim(sec, field, value, [_ev(ledger, snap, path, value)], **kw)

    c("legal_name", "$.navn", e.get("navn"))
    c("organisation_number", "$.organisasjonsnummer", e.get("organisasjonsnummer"))
    form = _get(e, "organisasjonsform", "kode")
    if form:
        c("legal_form", "$.organisasjonsform", f"{form} ({_get(e, 'organisasjonsform', 'beskrivelse') or ''})".replace(" ()", ""))
    c("registered_date", "$.registreringsdatoEnhetsregisteret", e.get("registreringsdatoEnhetsregisteret"),
      effective_date=e.get("registreringsdatoEnhetsregisteret"))
    c("founded_date", "$.stiftelsesdato", e.get("stiftelsesdato"), effective_date=e.get("stiftelsesdato"))
    c("business_address", "$.forretningsadresse", address_text(e.get("forretningsadresse")))
    c("postal_address", "$.postadresse", address_text(e.get("postadresse")))
    c("municipality", "$.forretningsadresse.kommune", _get(e, "forretningsadresse", "kommune"))
    for i in (1, 2, 3):
        code = _get(e, f"naeringskode{i}", "kode")
        if code:
            c("industry", f"$.naeringskode{i}", f"{code} {_get(e, f'naeringskode{i}', 'beskrivelse') or ''}".strip(),
              note="primary" if i == 1 else "secondary")
    activity = " ".join(e.get("vedtektsfestetFormaal") or e.get("aktivitet") or [])
    c("stated_purpose", "$.vedtektsfestetFormaal" if e.get("vedtektsfestetFormaal") else "$.aktivitet", activity,
      effective_date=e.get("vedtektsdato"))
    if e.get("antallAnsatte") is not None and e.get("harRegistrertAntallAnsatte", True):
        c("registered_employees", "$.antallAnsatte", e.get("antallAnsatte"),
          effective_date=e.get("registreringsdatoAntallAnsatteNAVAaregisteret") or e.get("registreringsdatoAntallAnsatteEnhetsregisteret"))
    elif e.get("harRegistrertAntallAnsatte") is False:
        ledger.mark("registered_employees", "not_available", "no employee count registered")
    cap = e.get("kapital") or {}
    if cap.get("belop") is not None:
        c("share_capital", "$.kapital", f"{cap.get('belop'):,.0f} {cap.get('valuta') or 'NOK'}".replace(",", " "),
          effective_date=cap.get("innfortDato"))
    c("registry_website", "$.hjemmeside", e.get("hjemmeside"))
    c("registry_email", "$.epostadresse", e.get("epostadresse"))
    c("registry_phone", "$.telefon", e.get("telefon") or e.get("mobil"))
    flags = []
    for key, label in (("registrertIMvaregisteret", "VAT register"), ("registrertIForetaksregisteret", "Register of Business Enterprises"),
                       ("registrertIFrivillighetsregisteret", "Register of Non-Profit Organisations"),
                       ("registrertIStiftelsesregisteret", "Foundation register")):
        if e.get(key):
            flags.append(label)
    if flags:
        c("registered_in", "$.registrertI*", ", ".join(flags))
    status = []
    if e.get("konkurs"):
        status.append("bankrupt")
    if e.get("underAvvikling"):
        status.append("under liquidation")
    if e.get("underTvangsavviklingEllerTvangsopplosning"):
        status.append("forced dissolution")
    c("legal_status", "$.konkurs/$.underAvvikling", ", ".join(status) if status else "active (not bankrupt or in liquidation)")
    if e.get("overordnetEnhet"):
        c("parent_unit", "$.overordnetEnhet", e.get("overordnetEnhet"), relation="parent")
    if e.get("erIKonsern") is not None:
        c("part_of_group", "$.erIKonsern", "yes" if e.get("erIKonsern") else "no")
    for item in e.get("historiskeNavn") or []:
        if item.get("navn"):
            value = f"{item['navn']} ({(item.get('fraDato') or '')[:10]} – {(item.get('tilDato') or '')[:10]})"
            c("former_name", "$.historiskeNavn", value, effective_date=(item.get("tilDato") or "")[:10] or None)
    ctx["legal_name"] = e.get("navn")
    ctx["former_names"] = [x.get("navn") for x in e.get("historiskeNavn") or [] if x.get("navn")]
    ctx["address"] = e.get("forretningsadresse") or {}
    ctx["postal"] = e.get("postadresse") or {}
    ctx["phone"] = e.get("telefon") or e.get("mobil")
    ctx["email"] = e.get("epostadresse")
    ctx["website"] = e.get("hjemmeside")
    ctx["employees"] = e.get("antallAnsatte")
    ctx["legal_form"] = form
    ctx["industry"] = _get(e, "naeringskode1", "beskrivelse")
    ctx["industry_code"] = _get(e, "naeringskode1", "kode")


def _roles(snap: Snapshot, ledger: Ledger) -> list[dict]:
    if snap.status in (404, 410):
        ledger.mark("leadership", "not_available", "no roles registered")
        return []
    if not snap.ok:
        ledger.mark("leadership", "failed", f"roles fetch failed: {snap.error}")
        return []
    try:
        body = snap.json()
    except ValueError:
        ledger.mark("leadership", "failed", "roles response not JSON")
        return []
    roles = []
    for group in body.get("rollegrupper", []) or []:
        changed = group.get("sistEndret")
        for item in group.get("roller", []) or []:
            if item.get("avregistrert") or item.get("fratraadt"):
                continue
            person = item.get("person") or {}
            name = person.get("navn") or {}
            entity = item.get("enhet") or {}
            display = " ".join(filter(None, [name.get("fornavn"), name.get("mellomnavn"), name.get("etternavn")]))
            if not display:
                ent_name = entity.get("navn")
                display = " ".join(ent_name) if isinstance(ent_name, list) else ent_name
            if not display:
                continue
            roles.append({
                "name": display,
                "is_person": bool(person),
                "organisation_number": entity.get("organisasjonsnummer"),
                "role_code": _get(item, "type", "kode"),
                "role": _get(item, "type", "beskrivelse"),
                "group": _get(group, "type", "beskrivelse"),
                "last_changed": changed,
            })
    order = {code: i for i, code in enumerate(LEADERSHIP_ORDER)}
    roles.sort(key=lambda r: (order.get(r["role_code"], 99), r["name"]))
    for role in roles:
        label = f"{role['role']}: {role['name']}"
        if role["organisation_number"]:
            label += f" (org. {role['organisation_number']})"
        ev = ledger.add_evidence(snap, source_class="official_registry", method="json_path",
                                 span=f"$.rollegrupper[{role['group']}].roller[{role['role_code']}] = {role['name']}",
                                 effective_date=role["last_changed"])
        ledger.claim("leadership_locations", "leadership", label, [ev], key=(role["role_code"], role["name"], role["organisation_number"]),
                     effective_date=role["last_changed"])
    if not roles:
        ledger.mark("leadership", "not_available", "register lists no active roles")
    return roles


def _subunits(snap: Snapshot, ledger: Ledger) -> list[dict]:
    if not snap.ok:
        ledger.mark("registered_workplaces", "failed", f"subunit fetch failed: {snap.error}")
        return []
    try:
        rows = _get(snap.json(), "_embedded", "underenheter") or []
    except ValueError:
        ledger.mark("registered_workplaces", "failed", "subunit response not JSON")
        return []
    out = []
    for row in rows:
        if row.get("nedleggelsesdato"):
            continue
        addr = row.get("beliggenhetsadresse") or row.get("postadresse")
        item = {
            "organisation_number": row.get("organisasjonsnummer"),
            "name": row.get("navn"),
            "address": address_text(addr),
            "municipality": _get(addr, "kommune"),
            "employees": row.get("antallAnsatte"),
            "started": row.get("oppstartsdato"),
            "website": row.get("hjemmeside"),
            "industry": _get(row, "naeringskode1", "beskrivelse"),
        }
        out.append(item)
        value = f"{item['name']} – {item['address'] or 'address not registered'} (subunit {item['organisation_number']})"
        if item["employees"]:
            value += f", {item['employees']} registered employees"
        ev = ledger.add_evidence(snap, source_class="official_registry", method="json_path",
                                 span=f"$._embedded.underenheter[{item['organisation_number']}] = {item['name']}; {item['address']}",
                                 effective_date=item["started"])
        ledger.claim("leadership_locations", "registered_workplace", value, [ev], key=item["organisation_number"],
                     effective_date=item["started"])
    if not out:
        ledger.mark("registered_workplace", "not_available", "no active subunits registered")
    return out


MONEY_FIELDS = [
    ("revenue", ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter")),
    ("operating_result", ("resultatregnskapResultat", "driftsresultat", "driftsresultat")),
    ("profit_before_tax", ("resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad")),
    ("net_result", ("resultatregnskapResultat", "aarsresultat")),
    ("total_assets", ("eiendeler", "sumEiendeler")),
    ("equity", ("egenkapitalGjeld", "egenkapital", "sumEgenkapital")),
    ("total_debt", ("egenkapitalGjeld", "gjeldOversikt", "sumGjeld")),
]


def _accounts(snap: Snapshot, ledger: Ledger, entity: dict) -> list[dict]:
    if snap.status in (404, 410):
        ledger.mark("annual_accounts", "not_available", "no structured accounts published by Regnskapsregisteret")
        return []
    if not snap.ok:
        ledger.mark("annual_accounts", "failed", f"accounts fetch failed: {snap.error}")
        return []
    try:
        body = snap.json()
    except ValueError:
        ledger.mark("annual_accounts", "failed", "accounts response not JSON")
        return []
    records = body if isinstance(body, list) else []
    # Prefer company accounts over group accounts for the same period.
    records.sort(key=lambda r: (_get(r, "regnskapsperiode", "tilDato") or "", r.get("regnskapstype") == "SELSKAP"))
    by_period: dict[str, dict] = {}
    for rec in records:
        period_end = _get(rec, "regnskapsperiode", "tilDato")
        if not period_end:
            continue
        by_period[period_end] = rec
    out = []
    for period_end in sorted(by_period):
        rec = by_period[period_end]
        start = _get(rec, "regnskapsperiode", "fraDato")
        period = f"{start}/{period_end}"
        currency = rec.get("valuta") or "NOK"
        item = {"period": period, "year": period_end[:4], "currency": currency, "type": rec.get("regnskapstype")}
        for name, path in MONEY_FIELDS:
            value = _get(rec, *path)
            if value is None:
                continue
            item[name] = value
            ev = ledger.add_evidence(snap, source_class="official_annual_accounts", method="json_path",
                                     span=f"{'.'.join(path)} = {value} {currency} (period {period}, id {rec.get('id')})",
                                     reporting_period=period)
            ledger.claim("financials", name, {"amount": value, "currency": currency, "year": item["year"]}, [ev],
                         key=(name, period), reporting_period=period)
        audit = rec.get("revisjon") or {}
        if audit:
            item["audited"] = not audit.get("ikkeRevidertAarsregnskap") and not audit.get("fravalgRevisjon")
        out.append(item)
    if not out:
        ledger.mark("annual_accounts", "not_available", "accounts response contained no periods")
    return out
