"""Official-website discovery, exact-entity verification and company-owned page extraction.

Publication gate: a domain is published as the company's official website only with page-level
proof tied to the legal entity (organisation number, registry phone or e-mail on the page, or the
registry-declared site/e-mail domain combined with the legal name). Name similarity alone abstains.
"""
from __future__ import annotations

import difflib
import re
import time
import urllib.parse
from datetime import datetime

from .evidence import Ledger
from .names import (FREE_MAIL, GENERIC, core_name, strip_accents_no, tokens, domain_slugs, distinctive, domain_candidates, full_name_in_text, name_in_text, orgnr_regex, orgnrs_in,
                    phone_digits, phones_in, registered_domain)
from .net import Fetcher, Snapshot, resolve
from .page import Page, snippet

PARKED = re.compile(
    r"(domain (name )?is for sale|domenet er til salgs|this domain (is|may be) for sale|buy this domain|parked (domain|free)|"
    r"domain parking|kjøp dette domenet|webhotell.{0,40}(kommer snart|under construction)|"
    r"hugedomains|sedo\.com|dan\.com|afternic|future home of something quite cool|"
    r"this site can.t be reached|account (has been )?suspended|default web site page|it works!|"
    r"welcome to nginx|apache2 (ubuntu|debian) default page|plesk|cpanel|domeneshop.{0,60}(parkert|parked))",
    re.IGNORECASE)

SECTION_KEYWORDS = {
    "contact": ["kontakt", "contact", "finn-oss", "find-us", "kontakt-oss", "contact-us"],
    "about": ["om-oss", "about", "hvem-er-vi", "om-selskapet", "selskapet", "historie", "company", "who-we-are", "om_oss", "/om"],
    "people": ["ansatte", "medarbeidere", "ledelse", "team", "people", "folk", "styret", "management", "vare-folk", "våre-ansatte", "kontaktpersoner"],
    "careers": ["karriere", "jobb", "ledige-stillinger", "ledig-stilling", "ledige", "stilling", "career", "jobs", "bli-med", "work-with-us", "join-us", "rekruttering"],
    "news": ["nyheter", "news", "aktuelt", "blogg", "blog", "presse", "press", "artikler", "media", "nytt"],
    "locations": ["avdeling", "kontorer", "lokasjoner", "locations", "butikker", "stores", "våre-kontorer", "finn-butikk", "offices"],
    "privacy": ["personvern", "privacy", "cookies", "vilkar", "vilkår", "terms"],
}

SOCIAL_PATTERNS = [
    ("facebook", re.compile(r"^https?://(?:www\.|m\.|no-nb\.|nb-no\.)?facebook\.com/(?!sharer|share|dialog|plugins|tr\?|login|policy|privacy|help)(?:pages/[^/?#]+/)?([A-Za-z0-9.\-_]{2,})/?(?:\?.*)?$", re.I)),
    ("instagram", re.compile(r"^https?://(?:www\.)?instagram\.com/(?!p/|explore|accounts|reel)([A-Za-z0-9._]{2,})/?(?:\?.*)?$", re.I)),
    ("linkedin", re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company|school|showcase)/([^/?#]{2,})/?", re.I)),
    ("youtube", re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/(?:(channel|c|user)/([^/?#]+)|(@[^/?#]+))", re.I)),
    ("x", re.compile(r"^https?://(?:www\.)?(?:twitter|x)\.com/(?!intent|share|home|search)([A-Za-z0-9_]{2,15})/?(?:\?.*)?$", re.I)),
    ("tiktok", re.compile(r"^https?://(?:www\.)?tiktok\.com/(@[A-Za-z0-9._]{2,})", re.I)),
    ("pinterest", re.compile(r"^https?://(?:[a-z]{2}\.|www\.)?pinterest\.[a-z.]+/([A-Za-z0-9_]{3,})/?$", re.I)),
    ("vimeo", re.compile(r"^https?://(?:www\.)?vimeo\.com/([A-Za-z][A-Za-z0-9_]{2,})/?$", re.I)),
    ("github", re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9-]{2,})/?$", re.I)),
    ("threads", re.compile(r"^https?://(?:www\.)?threads\.net/(@[A-Za-z0-9._]{2,})", re.I)),
    ("snapchat", re.compile(r"^https?://(?:www\.)?snapchat\.com/add/([A-Za-z0-9._-]{2,})", re.I)),
]
SOCIAL_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "youtube.com", "twitter.com", "x.com", "tiktok.com",
                "pinterest.", "vimeo.com", "github.com", "threads.net", "snapchat.com")
PLATFORM_OWN_PAGES = {"facebook", "instagram", "youtube", "twitter", "linkedin", "tiktok", "x", "share", "home", "terms.php",
                      "privacy", "policies", "legal", "about", "help", "watch", "groups", "events", "hashtag", "profile.php"}

ATS_HOSTS = ("jobylon.com", "recman.no", "recman.page", "webcruiter.no", "webcruiter.com", "teamtailor.com", "easycruit.com",
             "hrmanager.no", "hr-manager.net", "reachmee.com", "finn.no/job", "jobbnorge.no", "arbeidsplassen.nav.no/stillinger/stilling",
             "varbi.com", "smartrecruiters.com", "workable.com", "lever.co", "greenhouse.io", "career.ki", "talentech", "jobteaser",
             "homerun.co", "personio", "simployer", "zalaris", "emply.com", "hr-skyen", "cvpartner", "jobs.ashbyhq.com", "recruitee.com")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,100}\.[A-Za-z]{2,10}")
POSTCODE_RE = re.compile(r"([A-ZÆØÅ][A-Za-zÆØÅæøåéü.\-' ]{2,40}\s\d{1,4}[A-Za-z]?(?:[,\s]+[A-Za-z0-9 .\-]{0,20})?)[,\s]+(\d{4})\s+([A-ZÆØÅ][A-Za-zÆØÅæøå\-]{1,30}(?:\s[A-ZÆØÅ][A-Za-zÆØÅæøå\-]{1,30})?)")
TITLE_WORDS = re.compile(
    r"\b(daglig leder|administrerende direktør|adm\.? dir\.?|ceo|cfo|cto|coo|styreleder|styrets leder|chairman|founder|gründer|grunnlegger|"
    r"medgründer|co-founder|partner|eier|innehaver|økonomisjef|finansdirektør|salgssjef|markedssjef|driftssjef|driftsleder|avdelingsleder|"
    r"prosjektleder|kontorleder|butikksjef|regionsjef|direktør|managing director|general manager|head of [a-z]+|sales manager|"
    r"daglig ansvarlig|kjøkkensjef|teknisk sjef|personalsjef|hr-sjef|hr-leder|leder)\b", re.IGNORECASE)
PERSON_RE = re.compile(r"^([A-ZÆØÅ][a-zæøåéü\-]+(?:\s[A-ZÆØÅ]\.?)?(?:\s[A-ZÆØÅ][a-zæøåéü\-]+){1,3})$")
NOT_NAME_WORDS = {"our", "vår", "våre", "vårt", "om", "kontakt", "oss", "ansatte", "team", "les", "mer", "the", "about", "designers",
                  "ledelse", "styret", "kunder", "tjenester", "produkter", "nyheter", "hjem", "meny", "logg", "inn", "se", "alle", "vis",
                  "daglig", "leder", "salg", "service", "avdeling", "kontor", "sentralbord", "kundeservice", "post", "faktura", "support",
                  "administrasjon", "økonomi", "drift", "prosjekt", "board", "management", "contact", "people", "staff", "sales", "office",
                  "head", "chief", "senior", "junior", "partner", "partners", "eiendom", "holding", "gruppen", "group", "norge", "norway",
                  "mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag", "åpningstider", "adresse", "telefon", "epost",
                  "e-post", "org", "nr", "as", "asa", "ltd", "avd", "for", "og", "med", "til", "på", "av", "din", "ditt", "dine", "ny", "nye"}
DATE_IN_URL = re.compile(r"/(20\d{2})[/-](0[1-9]|1[0-2])(?:[/-](0[1-9]|[12]\d|3[01]))?/")
NO_MONTHS = {"januar": 1, "februar": 2, "mars": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9,
             "oktober": 10, "november": 11, "desember": 12, "january": 1, "february": 2, "march": 3, "may": 5, "june": 6,
             "july": 7, "october": 10, "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
             "sep": 9, "sept": 9, "okt": 10, "oct": 10, "nov": 11, "des": 12, "dec": 12}
TEXT_DATE = re.compile(r"\b(\d{1,2})\.?\s+(januar|februar|mars|april|mai|juni|juli|august|september|oktober|november|desember|"
                       r"january|february|march|may|june|july|october|december|jan|feb|mar|apr|jun|jul|aug|sept?|okt|oct|nov|des|dec)\.?\s+(20\d{2})\b", re.I)
NUM_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b")


def _normalise_site_url(value: str) -> str | None:
    value = (value or "").strip()
    if not value or " " in value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value.lstrip("/")
    parts = urllib.parse.urlsplit(value)
    if not parts.hostname or "." not in parts.hostname:
        return None
    return f"{parts.scheme.lower()}://{parts.hostname.lower()}{parts.path or '/'}"


def build_candidates(ctx: dict) -> list[dict]:
    org_name = ctx.get("legal_name") or ""
    out: list[dict] = []

    def add(url: str | None, origin: str):
        url = _normalise_site_url(url or "")
        if not url:
            return
        dom = registered_domain(urllib.parse.urlsplit(url).hostname or "")
        if any(registered_domain(urllib.parse.urlsplit(c["url"]).hostname or "") == dom for c in out):
            return
        if any(h in dom for h in SOCIAL_HOSTS):
            return
        out.append({"url": url, "origin": origin})

    add(ctx.get("website"), "registry_website")
    wd_sites = sorted((ctx.get("wikidata") or {}).get("sites") or [], key=lambda u: (not urllib.parse.urlsplit(u).hostname.endswith(".no"), len(u)))
    for site in wd_sites[:2]:
        add(site, "wikidata_website")
    email = (ctx.get("email") or "").strip().lower()
    if "@" in email:
        domain = email.split("@", 1)[1]
        if domain not in FREE_MAIL:
            add("https://" + domain + "/", "registry_email_domain")
    for sub in ctx.get("subunits") or []:
        if sub.get("website"):
            add(sub["website"], "subunit_website")
    for dom in domain_candidates(org_name):
        add("https://" + dom + "/", "name_guess")
    legal_core = core_name(org_name).lower()
    for sub in (ctx.get("subunits") or [])[:3]:
        sub_core = core_name(sub.get("name") or "").lower()
        if sub_core and sub_core != legal_core and len(sub_core) >= 4:
            for dom in domain_candidates(sub["name"], limit=2):
                add("https://" + dom + "/", "subunit_name_guess")
    for former in (ctx.get("former_names") or [])[:2]:
        for dom in domain_candidates(former, limit=3):
            add("https://" + dom + "/", "former_name_guess")
    return out


class SiteVisit:
    def __init__(self, snap: Snapshot, page: Page):
        self.snap = snap
        self.page = page


META_REFRESH = re.compile(r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+content=[\"']?\s*\d+\s*;\s*url=([^\"'>\s]+)", re.I)
JS_REDIRECT = re.compile(r"(?:window|document|top)\.location(?:\.href)?\s*=\s*[\"']([^\"']+)[\"']", re.I)
PARKING_HOSTS = ("hugedomains.", "goldname.", "sedo.", "dan.com", "afternic.", "parkingcrew.", "bodis.", "above.com", "domainmarket.",
                 "uniregistry.", "undeveloped.", "domainnameshop.", "parklogic.", "voodoo.com", "namebright.", "brandbucket.", "squadhelp.")


def _fetch_page(fetcher: Fetcher, url: str) -> SiteVisit | None:
    snap = fetcher.get(url)
    for _ in range(2):
        if not (snap.ok and snap.text and len(snap.text) < 6000):
            break
        m = META_REFRESH.search(snap.text) or (JS_REDIRECT.search(snap.text) if len(snap.text) < 2500 else None)
        if not m:
            break
        target = urllib.parse.urljoin(snap.final_url or url, m.group(1).strip())
        if target.rstrip("/") == (snap.final_url or url).rstrip("/"):
            break
        hop = fetcher.get(target)
        hop.redirects = (snap.redirects or []) + [snap.final_url or url] + (hop.redirects or [])
        hop.requested_url = url
        snap = hop
    if not snap.ok or not snap.text:
        return SiteVisit(snap, Page(url, "")) if snap.error == "blocked_by_robots" else None
    if "html" not in snap.content_type and "xml" not in snap.content_type and snap.content_type:
        return None
    return SiteVisit(snap, Page(snap.final_url or url, snap.text))


def _parked(visit: SiteVisit) -> bool:
    host = (urllib.parse.urlsplit(visit.snap.final_url or "").hostname or "").lower()
    if any(p in host for p in PARKING_HOSTS):
        return True
    return bool(PARKED.search(visit.page.flat[:3000] + " " + visit.page.title)) and len(visit.page.flat) < 4000


ENTITY_NAME_RE = re.compile(r"([A-ZÆØÅ0-9][\w&.'\- ]{1,50}?)\s(AS|ASA|A/S|SA|DA|ANS|NUF)\b")


def other_entities(ctx: dict, page: Page, zones: list[str] | None = None) -> list[str]:
    """Legal-entity names the page uses to identify its owner (title, site name, footer) that are not ours."""
    ours = [core_name(n).lower() for n in [ctx.get("legal_name") or ""] + (ctx.get("former_names") or []) if n]
    ours += [core_name(s.get("name") or "").lower() for s in ctx.get("subunits") or [] if s.get("name")]
    zones = zones if zones is not None else [page.title, page.meta.get("og:site_name", ""), page.flat[-1500:]]
    found = []
    for zone in zones:
        for m in ENTITY_NAME_RE.finditer(zone or ""):
            name = m.group(1).strip(" .,-|©")
            name = re.sub(r"^(?:(?:©|copyright|\(c\))\s*)?(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*", "", name, flags=re.I).strip()
            words = name.split()
            if not words or len(name) < 3:
                continue
            # keep the trailing 1-4 words, which is where the entity name sits in running text
            candidate = " ".join(words[-4:]).lower()
            squash = re.sub(r"[^a-z0-9æøå]", "", candidate)
            if any(o and (o in candidate or candidate.endswith(o) or o.endswith(candidate)) for o in ours) or \
                    any(len(sq) >= 4 and (sq in squash or squash.endswith(sq)) for sq in (re.sub(r"[^a-z0-9æøå]", "", o) for o in ours)):
                continue
            if candidate in ("org", "org.nr", "mva", "orgnr") or re.fullmatch(r"[\d\s.]+", candidate):
                continue
            found.append(" ".join(words[-4:]) + " " + m.group(2))
    return list(dict.fromkeys(found))[:5]


def _leader_on_page(ctx: dict, text: str) -> tuple[str, int] | None:
    low = text.lower()
    for role in ctx.get("roles") or []:
        if not role.get("is_person") or role.get("role_code") not in ("DAGL", "LEDE", "INNH", "DTPR", "DTSO"):
            continue
        parts = role["name"].split()
        for cand in {role["name"], f"{parts[0]} {parts[-1]}"}:
            i = low.find(cand.lower())
            if i >= 0 and len(cand) >= 7:
                return cand, i
    return None


def _proof(ctx: dict, visit: SiteVisit, origin: str, candidate_host: str) -> dict | None:
    """Return proof dict {level, span} or None."""
    org = ctx["organisation_number"]
    page = visit.page
    text = page.flat
    hay = text + " " + " ".join(str(v) for item in page.jsonld for k, v in item.items() if k in ("taxID", "vatID", "identifier", "leiCode", "legalName", "name"))
    hay += " " + page.meta.get("description", "")
    legal = ctx.get("legal_name") or ""
    final_host = urllib.parse.urlsplit(visit.snap.final_url or "").hostname or ""
    same_domain = registered_domain(final_host) == registered_domain(candidate_host)
    named = name_in_text(legal, hay + " " + page.title) or any(name_in_text(f, hay + " " + page.title) for f in ctx.get("former_names") or [])
    if address_name(legal):
        named = False  # a street address used as company name ("Lyseveien 16 AS") proves nothing on a page
    m = orgnr_regex(org).search(hay)
    if m:
        others = orgnrs_in(hay) - {org} - {s.get("organisation_number") for s in ctx.get("subunits") or []}
        if len(others) <= 2:
            return {"level": "organisation_number_on_page", "span": snippet(hay, m.start(), m.end()), "named": named}
        return {"level": "group_listing", "span": snippet(hay, m.start(), m.end()), "named": named, "reject": True}
    label = re.sub(r"[^a-z0-9æøå]", "", registered_domain(final_host or candidate_host).split(".")[0])
    slugs = {re.sub(r"[^a-z0-9æøå]", "", x) for n in [legal] + (ctx.get("former_names") or []) for x in domain_slugs(n)}
    domain_named = bool(label) and any(label == sl or (len(sl) >= 6 and sl in label) for sl in slugs)
    norwegian = (page.lang or "").lower()[:2] in ("no", "nb", "nn") or registered_domain(final_host or candidate_host).endswith(".no") \
        or bool(re.search(r"(?<!\d)\d{4}\s+[A-ZÆØÅ][a-zæøå]+", text) and re.search(r"(\+47|org\.?\s?nr|mva|telefon|tlf)", text, re.I))
    if not norwegian:
        return {"level": "no_norwegian_context", "span": f"lang={page.lang}; host={final_host}", "named": named, "reject": True}
    others = other_entities(ctx, page)
    if not named and others:
        return {"level": "site_names_other_entity", "span": "; ".join(others), "named": False, "reject": True, "others": others}
    if others and not _owner_zone_named(ctx, page) and not _path_named(ctx, visit.snap.final_url or ""):
        # the title, site name or footer identify another legal entity and ours appears only in running text:
        # a group, owner, manager or service provider site
        return {"level": "site_owned_by_other_entity", "span": "; ".join(others), "named": named, "reject": True, "others": others}
    managed = _managed_entity(ctx)
    if managed and not domain_named:
        return {"level": "managed_entity_without_own_domain", "span": final_host, "named": named, "reject": True}
    holding = "holding" in legal.lower() or str(ctx.get("industry_code") or "").startswith("64.2")
    named_now = name_in_text(legal, hay + " " + page.title)
    if holding and not named_now and origin != "registry_website":
        return {"level": "holding_without_own_name_on_site", "span": "", "named": False, "reject": True}
    phone = phone_digits(ctx.get("phone"))
    if phone:
        for digits, start, end in phones_in(text):
            if digits == phone and (named or domain_named):
                return {"level": "registry_phone_on_page", "span": snippet(text, start, end), "named": named}
    email = (ctx.get("email") or "").lower()
    if email and "@" in email and email.split("@")[1] not in FREE_MAIL:
        idx = text.lower().find(email)
        if idx < 0:
            mail_links = [l["url"] for l in page.links if l["url"].lower().startswith("mailto:" + email)]
            if mail_links and (named or domain_named):
                return {"level": "registry_email_on_page", "span": f"mailto link {mail_links[0]}", "named": named}
        elif named or domain_named:
            return {"level": "registry_email_on_page", "span": snippet(text, idx, idx + len(email)), "named": named}
    if named and same_domain and origin in ("registry_website", "registry_email_domain", "subunit_website", "wikidata_website") and \
            (domain_named or _owner_zone_named(ctx, page)):
        span = _name_span(legal, hay + " " + page.title)
        return {"level": f"{origin}_with_legal_name", "span": span, "named": True}
    if same_domain and (named or domain_named) and origin in ("registry_website", "registry_email_domain", "wikidata_website"):
        hit = _address_on_page(ctx, text)
        if hit is not None:
            return {"level": f"{origin}_with_registered_address", "span": snippet(text, hit[0], hit[1]), "named": named}
    leader = _leader_on_page(ctx, text) if named and same_domain else None
    if leader and (distinctive(legal) or origin not in ("name_guess", "former_name_guess", "subunit_name_guess")):
        return {"level": "legal_name_and_registered_leader_on_page", "span": snippet(text, leader[1], leader[1] + len(leader[0])), "named": True}
    if named and same_domain and origin in ("name_guess", "former_name_guess") and distinctive(legal):
        addr = ctx.get("address") or {}
        street = " ".join((addr.get("adresse") or [])[:1]).lower()
        postcode = addr.get("postnummer") or ""
        low = text.lower()
        street_hit = street and len(street) > 5 and street in low
        post_hit = postcode and re.search(r"(?<!\d)" + postcode + r"(?!\d)", text) and (addr.get("poststed") or "").lower() in low
        if street_hit and post_hit:
            i = low.find(street)
            return {"level": "legal_name_and_registered_address_on_page", "span": snippet(text, i, i + len(street)), "named": True}
    full_slugs = {re.sub(r"[^a-z0-9æøå]", "", x) for x in domain_slugs(legal, trim_generic=False)}
    foreign_orgnrs = orgnrs_in(hay) - {org} - {s.get("organisation_number") for s in ctx.get("subunits") or []}
    if named and same_domain and label in full_slugs and origin == "name_guess" and distinctive(legal) and not others and not foreign_orgnrs:
        flat = " ".join((hay + " " + page.title).split())
        full = full_name_in_text(legal, flat)
        if full is not None:
            return {"level": "registered_name_with_legal_form_on_name_domain", "span": snippet(flat, full[0], full[1]), "named": True}
    if named:
        return {"level": "name_only", "span": _name_span(legal, hay + " " + page.title), "named": True, "reject": True}
    return None


BRREG_NAME_SEARCH = "https://data.brreg.no/enhetsregisteret/api/enheter?navn={name}&size=20"


def _former_name_taken(ctx: dict, fetcher: Fetcher) -> bool:
    """True when another registered entity now carries one of our former names (the site is then most likely theirs)."""
    own = ctx["organisation_number"]
    for former in ctx.get("former_names") or []:
        snap = fetcher.get(BRREG_NAME_SEARCH.format(name=urllib.parse.quote(former)), accept="application/json", check_robots=False)
        if not snap.ok:
            return True  # cannot rule it out: abstain
        try:
            rows = (snap.json().get("_embedded") or {}).get("enheter") or []
        except ValueError:
            return True
        target = " ".join(former.upper().split())
        if any(" ".join((r.get("navn") or "").upper().split()) == target and r.get("organisasjonsnummer") != own for r in rows):
            return True
    return False


ADDRESS_NAME = re.compile(r"\b[\wæøåÆØÅ.]*(?:gate|gata|gaten|veien|vegen|vei|veg|allé|alle|plass|torg|bakken|stien|brygge)\s+\d+", re.I)


def address_name(legal_name: str) -> bool:
    """True when the company name is a street address, typical for property-holding companies."""
    return bool(ADDRESS_NAME.search(core_name(legal_name)))


COPYRIGHT_RE = re.compile(r"(?:©|&copy;|\(c\)|copyright)[^|\n]{0,120}", re.I)


def _owner_zone_named(ctx: dict, page: Page) -> bool:
    """Our name where a site states its owner: title, site name, JSON-LD organisation name, copyright line."""
    ld_names = [str(item.get(k)) for item in page.jsonld for k in ("name", "legalName") if item.get(k)
                and str(item.get("@type", "")).lower() in ("organization", "localbusiness", "corporation", "website", "")]
    zones = " ".join([page.title, page.meta.get("og:site_name", "")] + ld_names + [m.group(0) for m in COPYRIGHT_RE.finditer(page.flat)])
    return any(name_in_text(n, zones) for n in [ctx.get("legal_name") or ""] + (ctx.get("former_names") or []) if n)


def _path_named(ctx: dict, url: str) -> bool:
    """A dealer or member page on a chain site whose path carries the company's own name."""
    path = re.sub(r"[^a-z0-9æøå]", "", urllib.parse.unquote(urllib.parse.urlsplit(url).path.lower()))
    return any(len(sl) >= 5 and sl in path for sl in {re.sub(r"[^a-z0-9æøå]", "", x) for x in domain_slugs(ctx.get("legal_name") or "", trim_generic=False)})


def _managed_entity(ctx: dict) -> bool:
    """Housing cooperatives, co-ownerships, foundations, funds and address-named property companies are usually run from a manager's domain."""
    return address_name(ctx.get("legal_name") or "") or ctx.get("legal_form") in ("BRL", "BBL", "ESEK", "STI", "SAM", "PK") or \
        bool(re.search(r"\b(borettslag|sameie|sameiet|stiftelse|stiftelsen|gavefond|legat|boligbyggelag)\b", ctx.get("legal_name") or "", re.I))


def _address_on_page(ctx: dict, text: str) -> tuple[int, int] | None:
    """Registered business or postal street plus postcode and town on the page."""
    low = text.lower()
    for addr in (ctx.get("address") or {}, ctx.get("postal") or {}):
        street = " ".join((addr.get("adresse") or [])[:1]).lower().strip()
        postcode = addr.get("postnummer") or ""
        town = (addr.get("poststed") or "").lower()
        if len(street) <= 5 or not re.search(r"\d", street) or not postcode or not town or street.startswith(("postboks", "pb ")):
            continue
        i = low.find(street)
        if i >= 0 and re.search(r"(?<!\d)" + postcode + r"(?!\d)", text) and town in low:
            return i, i + len(street)
    return None


def _name_span(legal: str, text: str) -> str:
    core = core_name(legal).lower()
    i = text.lower().find(core)
    return snippet(text, i, i + len(core)) if i >= 0 else core


NEWS_SEGMENTS = {"nyheter", "news", "aktuelt", "blogg", "blog", "artikler", "presse", "nytt", "article", "articles", "artikkel"}


def _links_for(page: Page, site_domain: str, section: str) -> list[str]:
    out = []
    keys = SECTION_KEYWORDS[section]
    for link in page.links:
        url = link["url"]
        if not url.startswith("http"):
            continue
        host = urllib.parse.urlsplit(url).hostname or ""
        if registered_domain(host) != site_domain:
            continue
        path = urllib.parse.urlsplit(url).path.lower()
        text = (link["text"] or "").lower()
        if section == "careers":
            # a careers page is a path segment that starts with the keyword ("/karriere", "/ledige-stillinger"), not an
            # article slug that merely contains it ("…-ny-toppjobb", "/bestilling")
            segments = [x for x in path.split("/") if x]
            if NEWS_SEGMENTS & set(segments) or not (any(seg.startswith(k.strip("/")) for seg in segments for k in keys) or
                                                      any(text.startswith(k.replace("-", " ")) for k in keys if len(k) > 3)):
                continue
            if url not in out and not re.search(r"\.(pdf|jpg|png|zip|docx?)$", path):
                out.append(url)
            continue
        if any(k in path for k in keys) or any(k.strip("/").replace("-", " ") == text.strip() for k in keys) or \
                any(text.startswith(k.strip("/").replace("-", " ")) for k in keys if len(k) > 3):
            if url not in out and not re.search(r"\.(pdf|jpg|png|zip|docx?)$", path):
                out.append(url)
    out.sort(key=lambda u: len(u))
    return out


def discover_and_verify(ctx: dict, fetcher: Fetcher, ledger: Ledger, deadline: float, max_fetches: int = 10) -> dict | None:
    candidates = build_candidates(ctx)
    ctx["website_candidates"] = []
    fetches = 0
    rejected_registry = False
    for cand in candidates:
        if time.monotonic() > deadline or fetches >= max_fetches:
            break
        host = urllib.parse.urlsplit(cand["url"]).hostname or ""
        if cand["origin"] in ("name_guess", "former_name_guess", "subunit_name_guess"):
            try:
                if not resolve(host.encode("idna").decode()):
                    continue
            except UnicodeError:
                continue
        visit = _fetch_page(fetcher, cand["url"])
        fetches += 1
        record = {"url": cand["url"], "origin": cand["origin"]}
        ctx["website_candidates"].append(record)
        if visit is None and cand["url"].startswith("https://"):
            visit = _fetch_page(fetcher, "http://" + cand["url"][8:])
            fetches += 1
        if visit is None:
            record["result"] = "unreachable"
            continue
        if visit.snap.error == "blocked_by_robots":
            record["result"] = "blocked_by_robots"
            if cand["origin"] == "registry_website":
                ledger.mark("official_website", "blocked", "registry website disallows automated access (robots.txt)")
                ctx["registry_site_blocked"] = cand["url"]
            continue
        if _parked(visit):
            record["result"] = "parked_or_placeholder"
            if cand["origin"] == "registry_website":
                rejected_registry = True
            continue
        proof = _proof(ctx, visit, cand["origin"], host)
        pages_checked = [visit]
        visit_proof = None
        if not proof or proof.get("reject"):
            site_domain = registered_domain(urllib.parse.urlsplit(visit.snap.final_url or cand["url"]).hostname or "")
            extra = (_links_for(visit.page, site_domain, "contact")[:1] + _links_for(visit.page, site_domain, "about")[:1] +
                     _links_for(visit.page, site_domain, "privacy")[:1])
            if not extra:
                base = visit.snap.final_url or cand["url"]
                extra = [urllib.parse.urljoin(base, "/kontakt"), urllib.parse.urljoin(base, "/om-oss")]
            for url in extra[:3]:
                if time.monotonic() > deadline or fetches >= max_fetches + 3:
                    break
                sub = _fetch_page(fetcher, url)
                fetches += 1
                if sub is None or not sub.page.flat:
                    continue
                pages_checked.append(sub)
                sub_proof = _proof(ctx, sub, cand["origin"], host)
                if sub_proof and not sub_proof.get("reject"):
                    proof, visit_proof = sub_proof, sub
                    break
            if not proof or proof.get("reject") or visit_proof is None:
                record["result"] = (proof or {}).get("level", "no_identity_proof")
                if cand["origin"] == "registry_website":
                    ctx["registry_site_unproven"] = {"url": visit.snap.final_url or cand["url"], "result": record["result"], "visit": visit}
                level = (proof or {}).get("level")
                genuine = level in ("name_only", "group_listing", "holding_without_own_name_on_site", "managed_entity_without_own_domain") or \
                    (level == "site_names_other_entity" and cand["origin"] in ("registry_website", "registry_email_domain"))
                if genuine:
                    ctx.setdefault("ambiguous_sites", []).append({"url": visit.snap.final_url, "origin": cand["origin"], "reason": record["result"]})
                if cand["origin"] == "registry_website":
                    rejected_registry = True
                continue
        else:
            visit_proof = visit
        if cand["origin"] == "former_name_guess" and _former_name_taken(ctx, fetcher):
            record["result"] = "former_name_now_held_by_other_entity"
            continue
        record["result"] = "verified:" + proof["level"]
        final = visit.snap.final_url or cand["url"]
        parts = urllib.parse.urlsplit(final)
        home = f"{parts.scheme}://{parts.hostname}/"
        if registered_domain(parts.hostname or "") != registered_domain(host) and _path_named(ctx, final):
            home = f"{parts.scheme}://{parts.hostname}{parts.path}"  # own page on a chain or platform site, not the chain's homepage
            ctx["website_relation"] = "page_on_chain_site"
            for family in WEBSITE_DEPENDENT:
                ledger.mark(family, "not_available", "the verified page sits on a chain or platform site; the rest of that site is not attributed to this entity")
        ev = ledger.add_evidence(visit_proof.snap, source_class="company_owned_website", span=proof["span"],
                                 method=f"identity_proof:{proof['level']}")
        support = [ev]
        if cand["origin"] in ("registry_website", "registry_email_domain"):
            reg_snap = ctx.get("entity_snapshot")
            if reg_snap is not None:
                field = "$.hjemmeside" if cand["origin"] == "registry_website" else "$.epostadresse"
                support.append(ledger.add_evidence(reg_snap, source_class="official_registry", method="json_path",
                                                   span=f"{field} = {ctx.get('website') if field == '$.hjemmeside' else ctx.get('email')}"))
        if cand["origin"] == "wikidata_website" and (ctx.get("wikidata") or {}).get("snapshot") is not None:
            wd = ctx["wikidata"]
            support.append(ledger.add_evidence(wd["snapshot"], source_class="open_knowledge_base_cc0", method="sparql",
                                               source_url=wd["item"], span=f"{wd['item']} P2333 = {ctx['organisation_number']}; P856 = {cand['url']}"))
        confidence = 0.99 if proof["level"] in ("organisation_number_on_page", "registry_phone_on_page", "registry_email_on_page") else 0.95
        scope = site_scope(ctx, visit) if ctx.get("website_relation") != "page_on_chain_site" else None
        note = f"discovered via {cand['origin']}; identity proof: {proof['level']}"
        if scope:
            ctx["website_relation"] = "group_site"
            ctx["group_site"] = scope
            note += f"; site belongs to a group, parent or sister company ({scope['reason']}: {scope['owner']}), so its social profiles, news and jobs are not attributed to this entity"
        ledger.claim("web_presence", "official_website", home, support, confidence=confidence, key="official_website", note=note,
                     relation="group_site" if scope else "exact_entity")
        return {"home": home, "domain": registered_domain(parts.hostname or ""), "visits": pages_checked, "origin": cand["origin"],
                "proof": proof["level"], "crawl": ctx.get("website_relation") != "page_on_chain_site", "group": scope}
    checked = [c["url"] for c in ctx["website_candidates"]]
    if _publish_registry_declared(ctx, ledger):
        return None
    if ctx.get("registry_site_blocked"):
        return None
    if ctx.get("ambiguous_sites"):
        ledger.mark("official_website", "ambiguous", "candidate site names the company but carries no exact identity proof", checked=checked)
    elif rejected_registry:
        ledger.mark("official_website", "not_available", "registry-listed website is parked, unreachable or unrelated to this entity", checked=checked)
    else:
        ledger.mark("official_website", "not_available", "no verifiable company-owned website found", checked=checked)
    return None


COUNTRY_WORDS = {"norge", "norway", "noreg", "norden", "nordic", "nordics", "nordisk", "scandinavia", "skandinavia", "international",
                 "sverige", "sweden", "danmark", "denmark", "finland", "europe", "europa", "global"}
UNIT_WORDS = {"supply", "chain", "logistikk", "logistics", "distribusjon", "distribution", "eiendom", "eiendommer", "property", "properties",
              "drift", "operations", "produksjon", "production", "retail", "butikker", "salg", "sales", "finans", "finance", "innkjøp",
              "procurement", "services", "service", "it"}
PARENT_WORDS = {"group", "gruppen", "gruppe", "holding", "konsern", "capital", "industrier", "industries"}
GROUP_SCOPED = ("social_profile", "company_news", "job_posting")


def _owner_lines(page: Page) -> tuple[list[str], list[str]]:
    """Where a site names its owner: title, site name, JSON-LD organisation names and copyright lines."""
    ld = [str(item.get(k)) for item in page.jsonld for k in ("name", "legalName") if item.get(k)
          and str(item.get("@type", "")).lower() in ("organization", "localbusiness", "corporation", "website")]
    return [page.title, page.meta.get("og:site_name", "")] + ld, [m.group(0) for m in COPYRIGHT_RE.finditer(page.flat)][:3]


def site_scope(ctx: dict, home: SiteVisit) -> dict | None:
    """Detect a verified site that belongs to the company's group, parent or sister company rather than to the company itself.

    The site may still carry our organisation number (a group site listing its subsidiaries in the footer), so the
    identity proof holds, but its social profiles, news and jobs are the group's. Returns {reason, owner} or None.
    """
    page = home.page
    org = ctx["organisation_number"]
    names, copyright = _owner_lines(page)
    lines = names + copyright
    zone = " | ".join(x for x in lines if x)
    ours = [n for n in [ctx.get("legal_name") or ""] + (ctx.get("former_names") or []) if n]
    own_named = any(name_in_text(n, zone) for n in ours)
    owner_orgnrs = orgnrs_in(" ".join(copyright)) | {re.sub(r"\D", "", str(item.get(k)))[-9:] for item in page.jsonld
                                                     for k in ("taxID", "vatID", "identifier") if isinstance(item.get(k), (str, int))}
    owner_orgnrs = {o for o in owner_orgnrs if len(o) == 9 and o != org} - {s.get("organisation_number") for s in ctx.get("subunits") or []}
    copyright_orgnr = org in orgnrs_in(" ".join(copyright))
    if owner_orgnrs and not copyright_orgnr:
        holder = next(iter(other_entities(ctx, page, zones=copyright)), "")
        return {"reason": "owner_line_names_other_organisation_number", "owner": (holder + ", " if holder else "") + "org.nr. " + sorted(owner_orgnrs)[0]}
    if own_named or copyright_orgnr:
        return None
    others = [x for x in other_entities(ctx, page, zones=lines) if x]
    if others:
        return {"reason": "owner_line_names_other_entity", "owner": others[0]}
    core = tokens(core_name(ctx.get("legal_name") or ""))
    host = urllib.parse.urlsplit(home.snap.final_url or "").hostname or ""
    label = re.sub(r"[^a-z0-9æøå]", "", registered_domain(host).split(".")[0])
    brands = [label] + [re.sub(r"[^a-z0-9æøå]", "", x.lower()) for x in names[1:] if x and len(x) < 40]
    for i in range(1, len(core)):
        prefix, rest = "".join(core[:i]), set(core[i:])
        if len(prefix) >= 3 and prefix in brands and rest & (COUNTRY_WORDS | UNIT_WORDS) and not rest & PARENT_WORDS:
            return {"reason": "site_of_parent_brand", "owner": prefix}
    langs = {h.split("-")[0] for h in page.hreflangs if h != "x-default"}
    if not registered_domain(host).endswith(".no") and len(langs) >= 4:
        return {"reason": "international_group_site", "owner": registered_domain(host)}
    return None


REGISTRY_DECLARED_OK = ("blocked_by_robots", "no_identity_proof", "no_norwegian_context", "site_names_other_entity", "site_owned_by_other_entity",
                        "name_only")
WEBSITE_DEPENDENT = ("public_brand", "website_locations", "people_on_website", "social_profile", "company_news")


def _publish_registry_declared(ctx: dict, ledger: Ledger) -> bool:
    """The register itself names the company's homepage, but the page carries no exact identity proof.

    Publish the domain as the registry-declared website (labelled, lower confidence) and do not extract
    company-owned content from it: the page may belong to a chain, group or franchise brand.
    Parked, unreachable and manager-run (housing cooperative, co-ownership, foundation) domains stay unpublished.
    """
    reg_snap = ctx.get("entity_snapshot")
    if reg_snap is None or not ctx.get("website"):
        return False
    if ctx.get("registry_site_blocked"):
        url, result, visit = ctx["registry_site_blocked"], "blocked_by_robots", None
    elif ctx.get("registry_site_unproven"):
        info = ctx["registry_site_unproven"]
        url, result, visit = info["url"], info["result"], info["visit"]
    else:
        return False
    if result not in REGISTRY_DECLARED_OK or _managed_entity(ctx):
        return False
    parts = urllib.parse.urlsplit(url)
    declared = urllib.parse.urlsplit(_normalise_site_url(ctx["website"]) or "").hostname or ""
    if not parts.hostname or registered_domain(parts.hostname) != registered_domain(declared):
        return False  # the declared domain now redirects elsewhere (sold, merged or expired)
    home = f"{parts.scheme}://{parts.hostname}/"
    support = [ledger.add_evidence(reg_snap, source_class="official_registry", method="json_path", span=f"$.hjemmeside = {ctx['website']}")]
    if visit is not None and visit.snap.ok:
        support.append(ledger.add_evidence(visit.snap, source_class="company_website_unverified", method="http_get",
                                           span=f"page reachable; title “{visit.page.title[:120]}”; identity check: {result}"))
    ledger.claim("web_presence", "official_website", home, support, confidence=0.8, key="official_website", relation="registry_declared",
                 note=f"declared as homepage in Enhetsregisteret; no page-level identity proof ({result}); company-owned content not extracted")
    state = "blocked" if result == "blocked_by_robots" else "not_available"
    reason = ("registry-declared website disallows automated access (robots.txt)" if state == "blocked" else
              "registry-declared website has no page-level identity proof, so its content is not attributed to this entity")
    for family in WEBSITE_DEPENDENT:
        ledger.mark(family, state, reason)
    ctx["website_relation"] = "registry_declared"
    return True


# ----------------------------------------------------------------------------------------------
# Crawl and extraction on the verified site


def crawl_site(ctx: dict, site: dict, fetcher: Fetcher, ledger: Ledger, deadline: float, max_pages: int = 12) -> dict:
    visits: list[SiteVisit] = list(site["visits"])
    seen = {v.snap.final_url for v in visits} | {v.snap.requested_url for v in visits}
    home_page = visits[0].page
    domain = site["domain"]
    wanted: list[tuple[str, str]] = []
    group = site.get("group")
    if group:
        reason = (f"the verified website belongs to a group, parent or sister company ({group['owner']}); "
                  "its social profiles, news and jobs are not attributed to this entity")
        for family in GROUP_SCOPED:
            ledger.mark(family, "not_available", reason, checked=[site["home"]])
    for section in ("contact", "about", "people") + (() if group else ("careers", "news")) + ("locations",):
        for url in _links_for(home_page, domain, section)[:2 if section in ("people", "news", "careers") else 1]:
            wanted.append((section, url))
    sections: dict[str, list[SiteVisit]] = {"home": [visits[0]]}
    for section, url in wanted:
        if len(visits) >= max_pages or time.monotonic() > deadline:
            break
        if url in seen:
            continue
        seen.add(url)
        v = _fetch_page(fetcher, url)
        if v is None or not v.page.flat:
            continue
        visits.append(v)
        sections.setdefault(section, []).append(v)
    # Feeds and article dates for news
    feed_urls = []
    for v in ([] if group else visits):
        feed_urls += [f for f in v.page.feeds if registered_domain(urllib.parse.urlsplit(f).hostname or "") == domain]
    news_items = []
    feed_urls = [f for f in feed_urls if not re.search(r"(comments|product|produkt|shop|/wp-json/)", f, re.I)]
    if feed_urls and time.monotonic() < deadline:
        snap = fetcher.get(feed_urls[0], accept="application/rss+xml,application/atom+xml,application/xml,text/xml")
        if snap.ok:
            news_items += _parse_feed(snap, domain)
    if len(news_items) < 3 and not group:
        for v in sections.get("news", []):
            news_items += _news_links(v, domain, section_page=True)
        news_items += _news_links(visits[0], domain)
        news_items = _dedupe_news(news_items)
        undated = [n for n in news_items if not n.get("date")]
        for item in undated[:4]:
            if time.monotonic() > deadline or len(visits) >= max_pages + 4:
                break
            v = _fetch_page(fetcher, item["url"])
            if v is None:
                continue
            visits.append(v)
            if v.page.meta.get("og:type", "").lower() == "product" or v.page.ld_of_type("Product"):
                continue
            og_title = v.page.meta.get("og:title") or ""
            date, span = _article_date(v.page, (og_title, item.get("title") or ""))
            if date:
                item["date"], item["span"], item["visit"] = date, span, v
                card = item.get("title") or ""
                heading = next((h for h in v.page.headings if len(h) >= 8 and card.lower().startswith(h.lower())), "")
                if len(og_title) >= 8 or heading or len(card) < 8:
                    item["title"] = strip_site_suffix(og_title if len(og_title) >= 8 else heading or card or v.page.title, domain,
                                                      v.page.meta.get("og:site_name", ""))
    site["all_visits"] = visits
    _extract(ctx, site, visits, sections, ledger)
    if not group:
        _publish_news(ctx, _dedupe_news(news_items), ledger, domain, menu=_menu_urls(visits[0].page))
    return site


def _extract(ctx: dict, site: dict, visits: list[SiteVisit], sections: dict, ledger: Ledger) -> None:
    domain = site["domain"]
    home = visits[0]
    org_ld = [x for v in visits[:3] for x in v.page.ld_of_type(*ORG_TYPES)]
    brand = _brand(ctx, site, home, org_ld)
    if not site.get("group"):
        _extract_socials(ctx, site, visits, org_ld, brand, ledger)

    # Contact e-mail and phone on the verified site
    emails: dict[str, tuple] = {}
    for v in visits:
        for link in v.page.links:
            if link["url"].lower().startswith("mailto:"):
                addr = urllib.parse.unquote(link["url"][7:].split("?")[0]).strip().lower()
                if EMAIL_RE.fullmatch(addr):
                    emails.setdefault(addr, (v, f"mailto:{addr}"))
        for m in EMAIL_RE.finditer(v.page.flat):
            addr = m.group(0).lower().rstrip(".")
            emails.setdefault(addr, (v, snippet(v.page.flat, m.start(), m.end(), 40)))
    reg_email = (ctx.get("email") or "").lower()
    kept = 0
    for addr, (v, span) in emails.items():
        dom = addr.split("@")[1]
        if registered_domain(dom) == domain or addr == reg_email:
            if re.search(r"(example|sentry|wixpress|domain\.|noreply|no-reply|\.png|\.jpg)", addr):
                continue
            ev = ledger.add_evidence(v.snap, source_class="company_owned_website", method="email_pattern", span=span)
            ledger.claim("identity", "contact_email", addr, [ev], key=("email", addr), confidence=0.97)
            kept += 1
            if kept >= 6:
                break
    phones: dict[str, tuple] = {}
    for v in visits:
        for link in v.page.links:
            if link["url"].lower().startswith("tel:"):
                d = phone_digits(urllib.parse.unquote(link["url"][4:]))
                if d:
                    phones.setdefault(d, (v, link["url"]))
        for digits, start, end in phones_in(v.page.flat):
            ctx_text = v.page.flat[max(0, start - 25):start].lower()
            if re.search(r"(tlf|tel|telefon|phone|mob|ring|sentralbord|kontakt)", ctx_text):
                phones.setdefault(digits, (v, snippet(v.page.flat, start, end, 40)))
    for digits, (v, span) in list(phones.items())[:6]:
        ev = ledger.add_evidence(v.snap, source_class="company_owned_website", method="phone_pattern", span=span)
        pretty = f"+47 {digits[:3]} {digits[3:5]} {digits[5:]}" if digits[0] in "49" else f"+47 {digits[:2]} {digits[2:4]} {digits[4:6]} {digits[6:]}"
        ledger.claim("identity", "contact_phone", pretty, [ev], key=("phone", digits), confidence=0.95)

    # Addresses / visiting locations on the site
    addresses: dict[str, tuple] = {}
    for v in [x for k in ("contact", "locations", "home", "about") for x in sections.get(k, [])]:
        for line in v.page.text.split("\n"):
            if len(line) > 160:
                continue
            for m in POSTCODE_RE.finditer(line):
                street, postcode, city = m.group(1).strip(" ,"), m.group(2), m.group(3)
                if re.search(r"(postboks|pb\.|boks)\b", street, re.I):
                    continue
                value = f"{street}, {postcode} {city}"
                addresses.setdefault(value.lower(), (value, v, line))
    for value, v, line in list(addresses.values())[:8]:
        ev = ledger.add_evidence(v.snap, source_class="company_owned_website", method="address_pattern", span=line[:300])
        ledger.claim("leadership_locations", "website_location", value, [ev], key=("site_address", value.lower()), confidence=0.9)

    # Public brand and self-description
    if brand:
        ev = ledger.add_evidence(home.snap, source_class="company_owned_website", method="og:site_name/title/json-ld",
                                 span=f"brand marker: {brand}")
        ledger.claim("identity", "public_brand", brand, [ev], key="public_brand", confidence=0.9, relation="self_declared_by_verified_site")
        ctx["brand"] = brand
    desc_sources = [(v, v.page.meta.get("description") or v.page.meta.get("og:description")) for v in [home] + sections.get("about", [])]
    for v, desc in desc_sources:
        if desc and len(desc) >= 40:
            ev = ledger.add_evidence(v.snap, source_class="company_owned_website", method="meta_description", span=desc[:300])
            ledger.claim("identity", "website_description", desc[:400], [ev], key=("desc", v.snap.final_url), confidence=0.9,
                         relation="company_self_description")
            ctx.setdefault("descriptions", []).append(desc)
            break
    founding = next((x.get("foundingDate") for x in org_ld if x.get("foundingDate")), None)
    if founding:
        ev = ledger.add_evidence(home.snap, source_class="company_owned_website", method="json_ld", span=f"foundingDate = {founding}")
        ledger.claim("identity", "website_founding_date", str(founding), [ev], key="website_founding", confidence=0.85)

    # People: registry roles confirmed on the site, plus named people with titles on team/contact pages
    roles = ctx.get("roles") or []
    names_found = set()
    for role in roles:
        if not role.get("is_person"):
            continue
        name = role["name"]
        parts = name.split()
        short = f"{parts[0]} {parts[-1]}" if len(parts) > 2 else name
        for v in visits:
            low = v.page.flat.lower()
            for candidate in (name, short):
                i = low.find(candidate.lower())
                if i >= 0:
                    ev = ledger.add_evidence(v.snap, source_class="company_owned_website", method="registry_name_match",
                                             span=snippet(v.page.flat, i, i + len(candidate), 60))
                    ledger.claim("leadership_locations", "leader_on_website", f"{name} ({role['role']})", [ev], key=("leader_web", name),
                                 confidence=0.95)
                    names_found.add(candidate.lower())
                    break
            else:
                continue
            break
    people_pages = sections.get("people", []) + sections.get("contact", []) + sections.get("about", [])
    count = 0
    for v in people_pages:
        lines = v.page.text.split("\n")
        for i, line in enumerate(lines):
            if count >= 25:
                break
            pm = PERSON_RE.match(line.strip())
            if not pm or line.lower() in names_found:
                continue
            window = " | ".join(lines[i + 1:i + 3])
            tm = TITLE_WORDS.search(window[:80])
            if not tm:
                continue
            title = window[:80].split(" | ")[0].strip()
            if len(title) > 60 or EMAIL_RE.search(title):
                title = tm.group(0)
            person = pm.group(1)
            if any(w.lower().strip(".") in NOT_NAME_WORDS for w in person.split()):
                continue
            if re.search(r"\b(of|at|hos|fra|from|i)\s+[A-ZÆØÅ]", title) or not TITLE_WORDS.match(title.strip()) and not TITLE_WORDS.search(title[:30]):
                continue
            ev = ledger.add_evidence(v.snap, source_class="company_owned_website", method="name_title_pattern",
                                     span=f"{line.strip()} | {window[:120]}")
            ledger.claim("leadership_locations", "person_on_website", f"{person} – {title}", [ev], key=("person", person.lower()),
                         confidence=0.85)
            count += 1

    # Jobs linked from the site (company careers pages and applicant-tracking systems)
    if site.get("group"):
        return
    jobs = 0
    career_visits = sections.get("careers", [])
    for v in visits:
        for jp in v.page.ld_of_type("JobPosting"):
            title = jp.get("title") or jp.get("name")
            if not title:
                continue
            value = {"title": str(title)[:160], "url": jp.get("url") or v.snap.final_url, "source": "company_site_jobposting"}
            if jp.get("datePosted"):
                value["date_posted"] = str(jp["datePosted"])[:10]
            if jp.get("validThrough"):
                value["valid_through"] = str(jp["validThrough"])[:10]
            ev = ledger.add_evidence(v.snap, source_class="company_owned_website", method="json_ld:JobPosting",
                                     span=f"JobPosting title={title}; datePosted={jp.get('datePosted')}", effective_date=value.get("date_posted"))
            ledger.claim("activity", "job_posting", value, [ev], key=("job", value["title"].lower(), value["url"]), confidence=0.95,
                         effective_date=value.get("date_posted"))
            jobs += 1
    for v in career_visits + [home]:
        for link in v.page.links:
            url = link["url"]
            text = (link["text"] or "").strip()
            if not any(h in url.lower() for h in ATS_HOSTS) or len(text) < 6:
                continue
            path = urllib.parse.urlsplit(url).path.lower()
            specific = bool(re.search(r"/(job|jobs|stilling|stillinger|vacancy|vacancies|position|positions|annonse|ad|jobb)/[^/]{3,}", path)
                            or re.search(r"\d{4,}", path)) and not re.search(r"(login|sign-?up|register|signin|logg-inn|profile|candidate)", path)
            if re.search(r"(login|sign-?up|register|signin|logg-inn)", path):
                continue
            if not specific or re.fullmatch(r"(se|vis|alle|ledige|søk|apply|les mer|read more|her|stillinger|jobs?|karriere|career|registrer)[\w\s]*", text.lower()):
                ledger.claim("activity", "careers_page", url.split("?")[0], [ledger.add_evidence(v.snap, source_class="company_owned_website",
                                                                                                  method="html_anchor", span=f'<a href="{url}">{text}</a>')],
                             key=("careers", url.split("?")[0]), confidence=0.9)
                continue
            value = {"title": text[:160], "url": url, "source": "ats_link_on_verified_site"}
            ev = ledger.add_evidence(v.snap, source_class="company_owned_website", method="html_anchor", span=f'<a href="{url}">{text}</a>')
            ledger.claim("activity", "job_posting", value, [ev], key=("job", text.lower(), url), confidence=0.9)
            jobs += 1
    for v in career_visits:
        ledger.claim("activity", "careers_page", v.snap.final_url,
                     [ledger.add_evidence(v.snap, source_class="company_owned_website", method="careers_page", span=(v.page.title or "")[:200])],
                     key=("careers", v.snap.final_url), confidence=0.9)
    if not jobs and not career_visits:
        ledger.mark("job_posting", "not_available", "no careers page or job links on verified site", checked=[site["home"]])


ORG_TYPES = ("Organization", "LocalBusiness", "Corporation", "Store", "Restaurant", "ProfessionalService", "HomeAndConstructionBusiness",
             "AutoDealer", "MedicalBusiness", "LegalService", "FinancialService")
SOCIAL_GENERIC = GENERIC | {"senter", "sentrum", "center", "centre", "transport", "bygg", "byggservice", "regnskap", "solutions", "systems",
                            "teknikk", "maskin", "handel", "partner", "partners", "studio", "design", "media", "norsk", "nordic", "oslo",
                            "bergen", "trondheim", "stavanger", "tromsø", "eiendomsmegling", "entreprenør", "entreprenor", "elektro",
                            "rørlegger", "rorlegger", "klinikk", "stiftelsen", "foreningen", "forening", "selskap", "selskapet", "senteret"}
THEME_HANDLES = {"wix", "wixcom", "squarespace", "wordpress", "wordpressdotcom", "elementor", "themeforest", "envato", "shopify", "webflow",
                 "godaddy", "hubspot", "mailchimp", "weebly", "jimdo", "yoast", "woocommerce", "joomla", "drupal", "themeisle", "astra",
                 "generatepress", "divi", "elegantthemes", "mystore", "mystorebuilder", "domeneshop", "one.com", "onecom", "proisp",
                 "webnode", "simvoly", "framer", "cargo", "carrd", "strikingly", "zyro", "hostinger", "facebook", "instagram"}
OPAQUE_HANDLE = re.compile(r"^(?:\d{5,}|UC[\w-]{20,}|profile\.php)$", re.I)
_SQUASH = re.compile(r"[^a-z0-9æøå]")


def _name_keys(ctx: dict, site: dict, brand: str | None) -> dict:
    names = [n for n in [ctx.get("legal_name") or ""] + (ctx.get("former_names") or []) if n]
    slugs = {_SQUASH.sub("", x) for n in names for trim in (False, True) for x in domain_slugs(n, trim_generic=trim)}
    core_tokens = [t for n in names for t in tokens(core_name(n))]
    single = len(set(core_tokens)) == 1
    distinct = {t for t in core_tokens if t not in SOCIAL_GENERIC and (len(t) >= 6 or single and len(t) >= 4)}
    initials = ["".join(t[0] for t in tokens(core_name(n)) if t not in ("og", "and", "i")) for n in names]
    label = _SQUASH.sub("", site["domain"].split(".")[0])
    return {"slugs": {x for x in slugs if len(x) >= 4}, "short": {x for x in slugs | {label} if 2 <= len(x) < 4},
            "label": label, "brand": _SQUASH.sub("", (brand or "").lower()), "tokens": distinct,
            "initials": {i for i in initials if len(i) >= 3}, "names": names + ([brand] if brand else [])}


def social_owner(keys: dict, handle: str, link_text: str = "") -> str | None:
    """Why a social handle belongs to the company (matches its name, brand or domain), or None when it does not."""
    h = _SQUASH.sub("", handle.lower().lstrip("@"))
    if not h or h in THEME_HANDLES:
        return None
    if OPAQUE_HANDLE.match(handle.split("/")[-1]) or re.fullmatch(r"[\w-]*?\d{8,}", handle) and not any(s in h for s in keys["slugs"]):
        return "opaque_id_named_in_link" if link_text and any(name_in_text(n, link_text) for n in keys["names"]) else None
    full = keys["slugs"] | ({keys["label"]} if len(keys["label"]) >= 4 else set()) | ({keys["brand"]} if len(keys["brand"]) >= 4 else set())
    if any(x in h for x in full):
        return "name_brand_or_domain_in_handle"
    if any(h.startswith(x) or len(x) == 3 and h.endswith(x) for x in keys["short"]):
        return "short_name_or_domain_in_handle"
    if any(t in h for t in keys["tokens"]):
        return "distinctive_name_token_in_handle"
    if any(h.startswith(i) and len(h) <= len(i) + 6 for i in keys["initials"]):
        return "name_initials_handle"
    if len(h) >= 6 and any(len(x) >= 6 and difflib.SequenceMatcher(None, h, x).ratio() >= 0.88 for x in full):
        return "spelling_variant_of_name"
    return None


def _parse_social(url: str) -> tuple[str, str] | None:
    for platform, pattern in SOCIAL_PATTERNS:
        m = pattern.match(url)
        if not m:
            continue
        handle = next((g for g in m.groups()[::-1] if g), "")
        if "." in handle.split("/")[-1] and handle.lower().endswith((".php", ".html", ".htm", ".aspx")) and handle != "profile.php":
            return None
        if handle.lower().strip("@") in PLATFORM_OWN_PAGES or handle.lower() in ("company", "channel", "user", "c"):
            return None
        return platform, handle
    return None


def _url_key(url: str) -> str:
    return re.sub(r"^https?://(www\.|m\.|[a-z]{2}-[a-z]{2}\.)?", "", url.lower()).rstrip("/")


def _ld_is_ours(item: dict, keys: dict, domain: str) -> bool:
    url = item.get("url") if isinstance(item.get("url"), str) else ""
    if url and registered_domain(urllib.parse.urlsplit(url).hostname or "") == domain:
        return True
    name = " ".join(str(item.get(k) or "") for k in ("name", "legalName"))
    return any(name_in_text(n, name) for n in keys["names"])


def _extract_socials(ctx: dict, site: dict, visits: list[SiteVisit], org_ld: list[dict], brand: str | None, ledger: Ledger) -> None:
    """Social profiles that belong to the company: JSON-LD sameAs of its own organisation, or links whose handle matches its
    name, brand or domain. Footer links to a group, agency, theme vendor, client or employee are excluded."""
    keys = _name_keys(ctx, site, brand)
    found: dict[tuple, dict] = {}
    excluded: list[dict] = []
    for v in visits:
        for link in v.page.links:
            url = link["url"]
            if not url.startswith("http") or not any(h in url.lower() for h in SOCIAL_HOSTS):
                continue
            parsed = _parse_social(url)
            if not parsed or (parsed[0], parsed[1].lower()) in found:
                continue
            why = social_owner(keys, parsed[1], link["text"])
            if why is None:
                if len(excluded) < 12 and all(e["url"] != url.split("?")[0] for e in excluded):
                    excluded.append({"url": url.split("?")[0], "page": v.snap.final_url, "zone": link.get("zone"),
                                     "reason": "handle does not match the company's name, brand or domain"})
                continue
            found[(parsed[0], parsed[1].lower())] = {"url": url.split("?")[0], "visit": v, "text": link["text"], "how": why,
                                                    "platform": parsed[0]}
    for v in visits[:3]:
        for item in v.page.ld_of_type(*ORG_TYPES):
            if not _ld_is_ours(item, keys, site["domain"]):
                continue
            same = item.get("sameAs")
            for url in (same if isinstance(same, list) else [same]):
                url = url.strip().split("?")[0] if isinstance(url, str) else ""
                parsed = _parse_social(url) if url.startswith("http") else None
                if not parsed or any(_url_key(f["url"]) == _url_key(url) for f in found.values()):
                    continue
                found.setdefault((parsed[0], parsed[1].lower()), {"url": url, "visit": v, "text": "", "how": "json_ld_sameas",
                                                                  "platform": parsed[0]})
    accepted = {_url_key(f["url"]) for f in found.values()} | {(f["platform"], k[1]) for k, f in found.items()}
    excluded = [e for e in excluded if _url_key(e["url"]) not in accepted and
                ((_parse_social(e["url"]) or ("", ""))[0], (_parse_social(e["url"]) or ("", ""))[1].lower()) not in accepted]
    by_platform: dict[str, list] = {}
    for item in found.values():
        by_platform.setdefault(item["platform"], []).append(item)
    published = 0
    for platform, items in by_platform.items():
        # Many links to one platform usually means a news/aggregator page, not company-owned profiles.
        if len(items) > 3:
            continue
        for item in items:
            url, v = item["url"], item["visit"]
            if item["how"] == "json_ld_sameas":
                span, method = f"JSON-LD Organization sameAs {url}", "json_ld:sameAs"
            else:
                span, method = f'<a href="{url}">{item["text"] or platform}</a> on verified site; ownership: {item["how"]}', "html_anchor"
            ev = ledger.add_evidence(v.snap, source_class="company_owned_website", method=method, span=span)
            ledger.claim("web_presence", "social_profile", {"platform": platform, "url": url}, [ev], key=("social", url.lower().rstrip("/")),
                         confidence=0.95, relation="linked_from_verified_site", note=f"ownership: {item['how']}")
            published += 1
    if excluded:
        ctx["social_excluded"] = excluded
    if not published:
        reason = "verified site links no social profiles" if not excluded else \
            "social links on the verified site do not match the company's name, brand or domain (group, agency, theme or third-party accounts)"
        ledger.mark("social_profile", "not_available", reason, checked=[site["home"]])


def _brand(ctx: dict, site: dict, home: SiteVisit, org_ld: list[dict]) -> str | None:
    legal_tokens = {t for t in tokens(core_name(ctx.get("legal_name") or "")) if len(t) >= 3}
    dom_label = site["domain"].split(".")[0].replace("-", "")
    candidates = [home.page.meta.get("og:site_name")] + [x.get("name") for x in org_ld if isinstance(x.get("name"), str)]
    candidates += [seg.strip() for seg in re.split(r"\s[|\-–—:•·]\s", home.page.title or "") if seg.strip()]
    for cand in candidates:
        if not cand:
            continue
        cand = re.split(r"\s[|•·–—]\s", cand)[0].strip()
        if not (2 < len(cand) < 60) or re.search(r"^(hjem|home|forside|velkommen|welcome|start)$", cand, re.I):
            continue
        if core_name(cand) != cand and core_name(cand).lower() != core_name(ctx.get("legal_name") or "").lower():
            continue  # another registered entity's name (a sister or parent company), not our brand
        ctoks = set(tokens(cand))
        squashed = re.sub(r"[^a-z0-9æøå]", "", cand.lower())
        if ctoks & legal_tokens or (len(dom_label) >= 4 and (dom_label in squashed or squashed in dom_label)):
            return cand
    return None


def _parse_feed(snap: Snapshot, domain: str) -> list[dict]:
    import xml.etree.ElementTree as ET
    items = []
    try:
        root = ET.fromstring(snap.text.encode("utf-8", "ignore"))
    except ET.ParseError:
        return items
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {child.tag.split("}")[-1]: child for child in node}
        title = (fields.get("title").text or "").strip() if fields.get("title") is not None else ""
        link_node = fields.get("link")
        link = ""
        if link_node is not None:
            link = (link_node.text or link_node.attrib.get("href") or "").strip()
        date_raw, date_key = None, None
        for key in ("pubDate", "published", "date"):  # publication dates only; Atom <updated> is a modification time
            if fields.get(key) is not None and fields[key].text:
                date_raw, date_key = fields[key].text.strip(), key
                break
        date = _parse_any_date(date_raw)
        if title and link and date:
            items.append({"title": title[:200], "url": link, "date": date, "span": f"<item><title>{title[:120]}</title><{date_key}>{date_raw}</{date_key}>",
                          "snap": snap, "method": "rss_item"})
        if len(items) >= 15:
            break
    return items


def _parse_any_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"(20\d{2})-(\d{2})-(\d{2})", raw)
    if m:
        return "-".join(m.groups())
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    m = TEXT_DATE.search(raw)
    if m:
        try:
            return datetime(int(m.group(3)), NO_MONTHS[m.group(2).lower().rstrip(".")], int(m.group(1))).date().isoformat()
        except (KeyError, ValueError):
            return None
    m = NUM_DATE.search(raw)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date().isoformat()
        except ValueError:
            return None
    return None


DEFAULT_POSTS = re.compile(r"^(hello world!?|hei verden!?|sample page|eksempelside|uncategorized|ukategorisert)$", re.I)


def _news_links(v: SiteVisit, domain: str, section_page: bool = False) -> list[dict]:
    out = []
    index_path = urllib.parse.urlsplit(v.snap.final_url or "").path.rstrip("/")
    for link in v.page.links:
        url = link["url"]
        if not url.startswith("http"):
            continue
        parts = urllib.parse.urlsplit(url)
        if registered_domain(parts.hostname or "") != domain:
            continue
        path = parts.path.rstrip("/")
        text = link["text"] or ""
        is_news_path = any(k in path.lower() for k in ("/nyheter/", "/news/", "/aktuelt/", "/blogg/", "/blog/", "/artikler/", "/presse/", "/nytt/", "/article"))
        deeper = section_page and index_path and path.startswith(index_path + "/") and path != index_path
        if not (is_news_path or deeper) or len(text) < 15 or len(text) > 220 or DEFAULT_POSTS.match(text.strip()):
            continue
        clean = re.sub(r"^\s*(\d{1,2}\.?\s+\w+\.?\s+20\d{2}|\d{1,2}\.\d{1,2}\.20\d{2}|20\d{2}-\d{2}-\d{2})(\s*\|\s*\d{1,2}[:.]\d{2}(\s*\([A-Z]{3,4}\))?)?\s*[-–|:]?\s*", "", text)
        item = {"title": clean_news_title(clean or text, url), "url": url.split("#")[0], "snap": v.snap, "method": "news_index_link"}
        dm = TEXT_DATE.search(text[:40]) or NUM_DATE.search(text[:40])
        if dm and _parse_any_date(dm.group(0)):
            item["date"] = _parse_any_date(dm.group(0))
            item["span"] = f"link text: {text[:160]}"
        m = DATE_IN_URL.search(parts.path + "/")
        if m:
            day = m.group(3) or "01"
            if m.group(3):
                item["date"] = f"{m.group(1)}-{m.group(2)}-{day}"
                item["span"] = f"date in URL path: {parts.path}"
        out.append(item)
    return out[:12]


PUBLISHED_META = ("article:published_time", "og:published_time", "datepublished", "pubdate", "publishdate", "publish-date", "publish_date",
                  "dc.date.issued", "dcterms.issued", "sailthru.date", "parsely-pub-date")


def _article_date(page: Page, titles: tuple[str, ...] = ()) -> tuple[str | None, str | None]:
    """Publication date of an article page. Explicit publication markers count, and a date printed in the article
    header (right after the post title, or labelled "Publisert"). Modification dates (article:modified_time,
    dateModified, <time class="updated">, "Oppdatert …") and dates further down in running text do not."""
    for key in PUBLISHED_META:
        if page.meta.get(key):
            d = _parse_any_date(page.meta[key])
            if d:
                return d, f'<meta {key}="{page.meta[key]}">'
    for item in page.jsonld:
        if item.get("datePublished"):
            d = _parse_any_date(str(item["datePublished"]))
            if d:
                return d, f"JSON-LD datePublished = {item['datePublished']}"
    for t in page.times[:6]:
        kind = t.get("kind", "")
        if re.search(r"(updated|modified|oppdatert)", kind) or not re.search(r"(published|publish|pubdate|entry-date|post-date|posted|date-?published|publisert)", kind):
            continue
        d = _parse_any_date(t["datetime"] or t["text"])
        if d:
            return d, f'<time datetime="{t["datetime"]}" class/itemprop="{kind.strip()[:60]}">{t["text"][:60]}</time>'
    head = page.flat[:3000]
    zones = [m.end() for m in re.finditer(r"\b(publisert|published|posted|postet|skrevet)\b[:\s]*", head, re.I)]
    for title in titles:
        key = " ".join((title or "").split())[:40]
        if len(key) >= 12 and head.find(key) >= 0:
            zones.append(head.find(key) + len(key))
    for start in zones:
        window = head[start:start + 260]
        m = TEXT_DATE.search(window) or NUM_DATE.search(window)
        if m and not re.search(r"(oppdatert|updated|endret|modified|sist)", window[:m.start()][-30:], re.I):
            d = _parse_any_date(m.group(0))
            if d:
                return d, snippet(head, start + m.start(), start + m.end(), 50)
    return None, None


NOT_ARTICLE_PATH = re.compile(r"/(produkt|produkter|product|products|shop|butikk|nettbutikk|vare|varer|collections?|kategori|category|categories|"
                              r"tag|tags|emne|author|forfatter|page/\d+|side/\d+|sok|search|kurv|cart|checkout|kasse|min-side|login|logg-inn)(/|$)", re.I)
NEWSROOM_HOSTS = ("mynewsdesk.com", "cision.com", "newsweb.oslobors.no", "kommunikasjon.ntb.no", "ntbinfo.no", "globenewswire.com",
                  "prnewswire.com", "via.ritzau.dk", "newsroom.notified.com")
LOREM = {"lorem", "ipsum", "dolor", "amet", "consectetur", "adipiscing", "elit", "praesent", "donec", "nullam", "aliquam", "mauris", "urna",
         "turpis", "vitae", "nibh", "odio", "taciti", "sociosqu", "litora", "aptent", "etiam", "vestibulum", "curabitur", "pellentesque",
         "quis", "nisi", "tellus", "duis", "enim", "fusce", "integer", "morbi", "phasellus", "suspendisse", "vivamus", "maecenas", "odiosters",
         "enims", "sadips", "nunc", "cras", "proin", "quisque", "sed", "eget", "felis", "nulla", "tempor", "incididunt", "labore", "magna",
         "nec", "quam", "sodales", "sollicitudin", "sollicit", "sollicits", "ante", "ligula", "lacus", "risus", "augue", "eros", "justo",
         "massa", "metus", "neque", "purus", "sapien", "velit", "arcu", "libero", "lectus", "dictum", "eleifend", "erat", "euismod",
         "facilisis", "fermentum", "posuere", "rhoncus", "rutrum", "sagittis", "semper", "tincidunt", "tortor", "tristique", "ultrices",
         "varius", "viverra", "volutpat", "consequat", "commodo", "ullamcorper", "pharetra", "placerat", "egestas", "blandit"}
DEMO_TITLE = re.compile(r"^(hello world!?|hei verden!?|sample page|eksempelside|uncategorized|ukategorisert|sample post\b.*|.*\bpost example|"
                        r"(gallery|video|audio|image|quote|link|aside|standard|sticky|status|chat) post\b.*|(blog )?post title( here)?|"
                        r"overskrift( her)?|tittel( her)?|ny+t+ innleg+|test(innlegg| post| artikkel| nyhet)?\s*\d*|testing|"
                        r"lorem ipsum.*|this is (a|an|the) (sample|test|demo).*|demo( post)?\s*\d*|eksempel(innlegg|artikkel)?\s*\d*)$", re.I)
EDITORIAL_NACE = ("58.12", "58.13", "58.14", "60.", "63.91")  # newspapers, periodicals, broadcasting, news agencies (SN2007 and SN2025)


def clean_news_title(title: str, url: str) -> str:
    """A post title from link or card text: drop image-credit prefixes and the date/teaser text that cards append."""
    t = " ".join((title or "").split())
    t = re.sub(r"^(illus(trasjon)?|foto|bilde|image|photo|ill\.?)\s*[:.|–-]\s*", "", t, flags=re.I)
    if re.match(r"^(foto|bilde|image|photo|portrett|illustrasjon) (av|of)\b", t, re.I):
        slug = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
        t = re.sub(r"[-_]+", " ", re.sub(r"\.\w{2,4}$", "", slug)).strip().capitalize() or t
    m = re.search(r"\s(\d{1,2}\.?\s+[A-Za-zæøåÆØÅ]{3,9}\.?\s+20\d{2}|\d{1,2}\.\d{1,2}\.20\d{2}|20\d{2}-\d{2}-\d{2})\b", t)
    if m and m.start() >= 12:
        t = t[:m.start()]
    t = re.sub(r"(\s*(\.{3}|…)?\s*(les mer|read more|les hele saken)|\s+(→|»))\s*$", "", t, flags=re.I)
    return t.strip(" -–|:")[:200]


SPAM_TITLE = re.compile(r"\b(casino|kasino|betting|bookmaker|slot machines?|apk|viagra|cialis|porn\w*|escort service|payday loans?|"
                        r"pinco|melbet|mostbet|1win|1xbet|bonus code|giriş|kayıt|türkiye|kazino|kasyno|казино|ставки)\b|[\u0400-\u04ff]{4,}", re.I)


def junk_news_title(title: str) -> bool:
    t = " ".join((title or "").split()).strip(" .!")
    words = re.findall(r"[a-zæøå]+", t.lower())
    return not t or bool(DEMO_TITLE.match(t)) or bool(DEMO_TITLE.match(re.sub(r"[^\w\s#]", "", t))) or bool(SPAM_TITLE.search(t)) or (len(words) >= 2 and sum(w in LOREM for w in words) >= 2)


def strip_site_suffix(title: str, domain: str, site_name: str = "") -> str:
    """'Post title - Sirius Økonomi' -> 'Post title' when the last segment is the site's name or domain."""
    parts = re.split(r"\s+[|–—-]\s+", title or "")
    label = re.sub(r"[^a-z0-9æøå]", "", domain.split(".")[0])
    if len(parts) > 1:
        last = re.sub(r"[^a-z0-9æøå]", "", parts[-1].lower())
        if last and (last == re.sub(r"[^a-z0-9æøå]", "", site_name.lower()) or len(label) >= 3 and (label in last or
                     strip_accents_no(last) == label or last in label)):
            return " – ".join(parts[:-1]).strip()
    return title


def _menu_urls(page: Page) -> set[str]:
    """Pages linked from the site's navigation and header, or from a home-page tile whose label is a short page name
    ("Bading", "Priser"): menu entries, not posts."""
    nav = {l["url"].rstrip("/").lower() for l in page.links if l["url"].startswith("http") and l.get("zone") in ("nav", "header")}
    tiles = {l["url"].rstrip("/").lower() + "\t" + " ".join(l["text"].lower().split()) for l in page.links
             if l["url"].startswith("http") and 0 < len((l["text"] or "").split()) <= 3}
    return nav | tiles


def news_rejection(item: dict, domain: str, menu: set[str]) -> str | None:
    """Why a dated item is not a real post (menu entry, product or taxonomy page, demo or placeholder content), or None."""
    parts = urllib.parse.urlsplit(item.get("url") or "")
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return "no_article_url"
    if registered_domain(host) != domain and not any(host == h or host.endswith("." + h) for h in NEWSROOM_HOSTS):
        return "link_leaves_company_site"
    if parts.path.strip("/") == "" or parts.path.rstrip("/").lower() in ("/nb", "/no", "/en", "/index.php", "/blog", "/nyheter", "/news", "/aktuelt"):
        return "site_or_section_root_not_a_post"
    url_key = item["url"].rstrip("/").lower()
    if not DATE_IN_URL.search(parts.path + "/") and (url_key in menu or url_key + "\t" + " ".join((item.get("title") or "").lower().split()) in menu):
        return "menu_entry"
    if NOT_ARTICLE_PATH.search(parts.path) or re.search(r"\.(jpe?g|png|gif|webp|svg|pdf|mp4|mp3|zip|docx?|xlsx?)$", parts.path, re.I):
        return "product_or_listing_page"
    if junk_news_title(item.get("title") or ""):
        return "demo_or_placeholder_post"
    return None


def _dedupe_news(items: list[dict]) -> list[dict]:
    seen = {}
    for item in items:
        key = item["url"].rstrip("/").lower()
        if key not in seen or (item.get("date") and not seen[key].get("date")):
            seen[key] = item
    return list(seen.values())


def _publish_news(ctx: dict, items: list[dict], ledger: Ledger, domain: str, menu: set[str] | None = None) -> None:
    """Dated posts from the company's own site or newsroom, newest first; menu entries, product pages, demo posts,
    mass-duplicated titles and editorial content of news publishers are left out."""
    today = datetime.utcnow().date().isoformat()
    if str(ctx.get("industry_code") or "").startswith(EDITORIAL_NACE):
        ledger.mark("company_news", "not_available", "the company is a news publisher: its articles are editorial content, not news about the company")
        return
    titles: dict[str, int] = {}
    for item in items:
        key = re.sub(r"\W+", " ", (item.get("title") or "").lower()).strip()
        titles[key] = titles.get(key, 0) + 1
    published, seen_titles, rejected = 0, set(), []
    # a hacked site: spam posts (casino, betting, pills) injected among the company's own; trust none of its dated items
    spam_site = sum(bool(SPAM_TITLE.search(i.get("title") or "")) for i in items) >= 2
    # a feed that lists pages ("Priser", "Bading", "Kano") rather than posts: one-word titles without a dated permalink
    page_feed = sum(len((i.get("title") or "").split()) == 1 and not DATE_IN_URL.search(urllib.parse.urlsplit(i["url"]).path + "/")
                    for i in items) >= 3
    for item in sorted([i for i in items if i.get("date")], key=lambda i: i["date"], reverse=True):
        if item["date"] > today or item["date"] < "2000-01-01":
            continue
        item["title"] = clean_news_title(item.get("title") or "", item["url"])
        key = re.sub(r"\W+", " ", item["title"].lower()).strip()
        why = news_rejection(item, domain, menu or set())
        if not why and spam_site:
            why = "spam_injected_into_site"
        if not why and page_feed and len(item["title"].split()) <= 2 and not DATE_IN_URL.search(urllib.parse.urlsplit(item["url"]).path + "/"):
            why = "page_not_post"
        if not why and titles.get(key, 0) >= 3:
            why = "same_title_repeated_in_feed"
        if not why and key in seen_titles:
            continue
        if why:
            rejected.append({"url": item["url"], "title": item["title"][:120], "reason": why})
            continue
        seen_titles.add(key)
        visit = item.get("visit")
        snap = visit.snap if visit else item["snap"]
        ev = ledger.add_evidence(snap, source_class="company_owned_website", method=item.get("method", "article_page"),
                                 span=item.get("span") or item["title"], effective_date=item["date"])
        ledger.claim("activity", "company_news", {"title": item["title"], "url": item["url"], "date": item["date"]}, [ev],
                     key=("news", item["url"].rstrip("/").lower()), effective_date=item["date"], confidence=0.9)
        published += 1
        if published >= 10:
            break
    if rejected:
        ctx["news_excluded"] = rejected[:12]
    if not published:
        ledger.mark("company_news", "not_available", "no dated posts found on verified site" if not rejected else
                    "dated items on the verified site are menu entries, product pages or placeholder posts, not news")
