"""Name normalisation, domain candidates and identity patterns for Norwegian entities."""
from __future__ import annotations

import re
import unicodedata

LEGAL_SUFFIXES = r"(AS|ASA|SA|DA|ANS|NUF|BA|SE|STI|BRL|KS|IKS|ENK|HF|KF|FLI|SPA|SF|BBL|ESEK|ORGL|SAM|PK|AB|A/S|ApS|GmbH|LTD|LIMITED|INC|OY)"
SUFFIX_RE = re.compile(r"[\s,.]+" + LEGAL_SUFFIXES + r"\.?$", re.IGNORECASE)
GENERIC = {"holding", "invest", "investment", "eiendom", "eiendommer", "drift", "norge", "norway", "norsk", "gruppen", "group",
           "og", "and", "the", "i", "pa", "på", "av", "for", "til", "service", "services", "consult", "consulting", "as", "company"}
FREE_MAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.com", "live.no", "msn.com",
    "yahoo.com", "yahoo.no", "icloud.com", "me.com", "mac.com", "online.no", "frisurf.no", "broadpark.no", "getmail.no",
    "start.no", "c2i.net", "lyse.net", "altibox.no", "epost.no", "ebnett.no", "haugnett.no", "enivest.net", "mail.com",
    "protonmail.com", "proton.me", "aol.com", "tele2.no", "chello.no", "bluezone.no", "netcom.no", "telia.no", "telenor.no",
    "loqal.no", "hotmail.se", "gmx.com", "gmx.net", "yandex.com", "vikenfiber.no", "tussa.com", "nordnet.no", "neasonline.no",
    "tdcadsl.no", "sf-nett.no", "mimer.no", "gmail.no", "hamarnett.no", "eidsiva.net", "fiberlink.no", "kvinnherad.net",
}


def strip_accents_no(text: str, mode: int = 0) -> str:
    table = [{"æ": "ae", "ø": "o", "å": "a"}, {"æ": "ae", "ø": "oe", "å": "aa"}, {}][mode]
    out = "".join(table.get(ch, ch) for ch in text.lower())
    out = unicodedata.normalize("NFKD", out)
    return "".join(ch for ch in out if not unicodedata.combining(ch) or ch in "æøå")


def core_name(legal_name: str) -> str:
    name = " ".join((legal_name or "").split())
    previous = None
    while previous != name:
        previous = name
        name = SUFFIX_RE.sub("", name).strip()
    name = re.sub(r"^(AS|A/S)\s+", "", name, flags=re.IGNORECASE)
    return name


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9æøåäöüé]+", (text or "").lower())


def name_in_text(legal_name: str, text: str) -> bool:
    """True when the core legal name occurs in text as a whole-word phrase (case-insensitive)."""
    core = core_name(legal_name).lower()
    if len(core) < 3:
        return False
    flat = " ".join((text or "").lower().split())
    variants = {core, core.replace("&", "og"), core.replace(" og ", " & ")}
    for v in variants:
        pattern = r"(?<![a-z0-9æøå])" + re.escape(v) + r"(?![a-z0-9æøå])"
        if re.search(pattern, flat):
            return True
    return False


def full_name_in_text(legal_name: str, text: str) -> tuple[int, int] | None:
    """Span of the complete registered name including its legal-form suffix (e.g. "Bokonsept AS"), case-insensitive."""
    full = " ".join((legal_name or "").lower().split())
    if full == core_name(legal_name).lower() or len(full) < 6:
        return None
    flat = " ".join((text or "").lower().split())
    for v in {full, full.replace("&", "og"), full.replace(" og ", " & ")}:
        m = re.search(r"(?<![a-z0-9æøå])" + re.escape(v) + r"(?![a-z0-9æøå])", flat)
        if m:
            return m.start(), m.end()
    return None


def distinctive(legal_name: str) -> bool:
    """A name is distinctive when its core has a non-generic token of length >= 4 or >= 2 tokens."""
    toks = [t for t in tokens(core_name(legal_name)) if t not in GENERIC]
    return bool(toks) and (len(toks) >= 2 or len(toks[0]) >= 5)


def domain_slugs(legal_name: str, trim_generic: bool = True) -> list[str]:
    core = core_name(legal_name)
    out: list[str] = []
    for mode in (0, 1, 2):
        s = strip_accents_no(core, mode).replace("&", " og ").replace("+", " ")
        s = re.sub(r"[^a-z0-9æøå\s-]", " ", s)
        words = s.split()
        if not words:
            continue
        variants = ["".join(words), "-".join(words)]
        trimmed = [w for w in words if w not in GENERIC]
        if trim_generic and trimmed and trimmed != words:
            variants += ["".join(trimmed), "-".join(trimmed)]
        if "og" in words:
            variants.append("".join(w for w in words if w != "og"))
        for v in variants:
            if len(v.replace("-", "")) >= 3 and v not in out:
                out.append(v)
    return out


def domain_candidates(legal_name: str, limit: int = 12) -> list[str]:
    slugs = domain_slugs(legal_name)
    out = []
    for slug in slugs:
        out.append(slug + ".no")
    for slug in slugs[:2]:
        out.append(slug + ".com")
    if re.search(r"\bASA?$", (legal_name or "").strip(), re.IGNORECASE) and slugs:
        out += [slugs[0] + "as.no", slugs[0] + "-as.no"]
    seen = []
    for d in out:
        if d not in seen:
            seen.append(d)
    return seen[:limit]


def orgnr_regex(org: str) -> re.Pattern:
    a, b, c = org[0:3], org[3:6], org[6:9]
    sep = r"[\s.  -]?"
    return re.compile(r"(?<![0-9])(?:NO\s?)?" + a + sep + b + sep + c + r"(?:\s?MVA)?(?![0-9])", re.IGNORECASE)


ANY_ORGNR = re.compile(r"(?<![0-9])([89]\d{2})[\s. ]?(\d{3})[\s. ]?(\d{3})(?![0-9])")


def valid_orgnr(org: str) -> bool:
    if not re.fullmatch(r"\d{9}", org):
        return False
    weights = [3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(d) * w for d, w in zip(org[:8], weights))
    check = 11 - total % 11
    check = 0 if check == 11 else check
    return check != 10 and check == int(org[8])


def orgnrs_in(text: str) -> set[str]:
    out = set()
    context = re.compile(r"(org|foretak|organisasjon|mva|vat|reg)", re.IGNORECASE)
    for m in ANY_ORGNR.finditer(text):
        org = "".join(m.groups())
        if valid_orgnr(org) and context.search(text[max(0, m.start() - 40):m.start()]):
            out.add(org)
    return out


def phone_digits(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0047"):
        digits = digits[4:]
    elif digits.startswith("47") and len(digits) == 10:
        digits = digits[2:]
    return digits if len(digits) == 8 else None


PHONE_RE = re.compile(r"(?<![\d])(?:\+47[\s. ]?|0047[\s. ]?)?(\d{2}[\s. ]?\d{2}[\s. ]?\d{2}[\s. ]?\d{2}|\d{3}[\s. ]?\d{2}[\s. ]?\d{3})(?![\d])")


def phones_in(text: str) -> list[tuple[str, int, int]]:
    out = []
    for m in PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if len(digits) == 8 and digits[0] in "2345679":
            out.append((digits, m.start(), m.end()))
    return out


def registered_domain(host: str) -> str:
    host = (host or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
