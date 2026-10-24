# Signalpost agent

An evidence-first research agent for Norwegian companies, built for the
[Builderr Signalpost challenge](https://builderr.ai/challenges/signalpost).

Give it a batch of organisation numbers. For every company it returns exactly one terminal
envelope with dated, sourced claims about legal identity, annual accounts, leadership,
registered workplaces, the verified official website and company-owned profiles, hiring and
dated public activity — plus an evidence-bounded brief, explicit availability states and a
refresh diff against the previous run. A static HTML viewer (desktop and mobile) is written
next to the results.

- **Pure Python standard library.** No third-party packages, no install step, no API keys.
- **No language model.** Synthesis is a deterministic template over published claims, so it
  cannot invent facts. Third-party cost per run: **USD 0**.
- **Precision first.** Company-owned content is read only from a website with page-level proof
  tied to the exact legal entity. A homepage that only the register declares is reported as such,
  without reading its pages. Name similarity alone always abstains.

## Run it (one command)

Requires Python 3.9+ and outbound HTTPS.

```bash
./run.sh batch.jsonl out
```

`batch.jsonl` may be JSONL (`{"organisation_number": "923609016"}` per line), a JSON list or
object with `organisation_numbers`, or plain text with one number per line. Equivalent without
the wrapper:

```bash
python3 -m signalpost --organisations batch.jsonl --output out/envelopes.jsonl
```

Outputs:

| Path | Content |
|---|---|
| `out/envelopes.jsonl` | one terminal envelope per input, in input order |
| `out/run-report.json` | counts, availability by information family, requests, p50/p95, validation checks |
| `out/site/index.html` | static viewer: search, filters, side-by-side comparison, one evidence page per company |
| `out/state/` | previous envelopes used for the next refresh diff (plus NAV ad cache) |

The process exits 0 only when the batch contract validates (one envelope per input, unique
numbers, valid states, every available claim backed by evidence with source and time).

Useful options (all have defaults): `--workers 16`, `--request-budget N` (hard cap on HTTP attempts
for the batch; default 16 per company), `--time-budget 2100` (seconds for the whole batch;
unfinished companies still receive a terminal `failed` envelope), `--company-budget 75`,
`--previous prior.jsonl` (explicit previous snapshot), `--bulk brreg.csv.gz` (frozen Brreg entity
snapshot used if the live API fails), `--no-nav`, `--expected-count N`. Starter-kit argument names
(`--organisations`, `--output`, `--report`, `--run-id`, `--profiles-output`) are accepted.

Tests (offline): `python3 -m unittest discover -s tests`

## What it does per company

1. **Official identity anchor** — Brønnøysundregistrene Enhetsregisteret entity, roles and
   subunits; Regnskapsregisteret structured annual accounts (up to three years). Every value
   cites the API URL, retrieval time, response hash and JSON path.
2. **Dated official events** — founding, register entries, VAT registration, articles date,
   share-capital changes, name changes, role-group changes, employee-count updates.
3. **Website discovery** — candidates from the registry website, Wikidata items that carry the
   exact organisation number (P2333 → official website P856, one query per batch), the registry
   e-mail domain (free-mail domains excluded), subunit websites, and domains derived from the
   legal name, former names and subunit names. DNS-filtered, then fetched (robots.txt respected).
4. **Identity gate** — see below. Failing candidates are recorded with the reason.
5. **Company-owned extraction** on the verified site (≤ ~16 pages): social profiles that belong
   to the company, contact e-mail/phone, visiting addresses, public brand, self-description,
   registered leaders confirmed on the site, named people with titles, careers pages and job
   links, dated posts (RSS/Atom, article metadata, dated URLs). See the ownership rules below.
6. **Jobs from NAV** — the public NAV job feed (arbeidsplassen.nav.no). The run walks the feed of
   active ads once; for each company it fetches the details of at most 30 ads whose employer name
   resembles the company or one of its registered subunits, and matches an ad only through the
   employer organisation number in those details (or the full legal name when NAV omits the
   number). Resolved numbers are cached across runs.
7. **Synthesis** — a one-line headline, decision-useful key points (profit history, revenue
   trend, equity buffer, stale filings, register status, CEO/chair structure, recent leadership
   register updates, workplaces, hiring, recent news, web footprint, holding/property context),
   derived ratios (operating and net margin, equity ratio, revenue per employee, revenue growth)
   that state their formula, a short brief, what changed since the last run, recent official
   events and every information family that remains unknown with its reason. Every sentence,
   point and ratio carries the claim ids it rests on.
8. **Refresh** — stable claim ids, typed changes (`new_role`, `closed_job`, `new_filing`,
   `changed_business_address`, …), carried-forward values when a source fails, superseded
   evidence preserved. Re-running on the same snapshot produces no changes.

## Identity gate (publication rules)

A domain becomes `official_website` only with one of these proofs on the fetched page:

| Proof | Condition |
|---|---|
| `organisation_number_on_page` | our organisation number appears (text or JSON-LD) and at most two other organisation numbers do (otherwise it is a group listing) |
| `registry_phone_on_page` / `registry_email_on_page` | the phone or e-mail from the official register appears, **and** the legal name is on the page or the domain is derived from it |
| `registry_website_with_legal_name` / `registry_email_domain_with_legal_name` | the register itself declares the domain, and the legal name is on the page |
| `legal_name_and_registered_leader_on_page` | legal name plus the registered CEO/chair by name |
| `legal_name_and_registered_address_on_page` | distinctive legal name plus registered street and postcode |
| `registry_website_with_registered_address` / `registry_email_domain_with_registered_address` | the register declares the domain, the page shows the registered street, postcode and town, **and** the page names the company or the domain is derived from its name |
| `registered_name_with_legal_form_on_name_domain` | the complete registered name including its legal form (e.g. "Bokonsept AS"; registered names are unique in Norway) on a domain derived from that name, Norwegian context, distinctive name, no other entity named |

**Own page on a chain site.** When a company's domain redirects to its own member or dealer page
on a chain or platform site (the page path carries the company name), that page is published as
the website and the rest of the chain site is not crawled.

**Registry-declared website.** When the register's own `hjemmeside` field names a reachable (or
robots-blocked) domain that carries no page-level proof, the domain is published as
`official_website` with `relation: "registry_declared"`, confidence 0.8 and the register field
as evidence — and **nothing is read from its pages**, because such a domain can belong to a chain,
group or franchise brand. Not published this way: parked or unreachable domains, a declared
domain that now redirects to a different domain, and housing cooperatives, co-ownerships and
foundations (usually on their manager's domain).

Always rejected (recorded as `ambiguous` or `not_available` with the reason): parked or
placeholder domains, pages without Norwegian context (foreign group sites), pages that name a
different legal entity as owner while ours is absent, pages whose title, site name or copyright
line identify another legal entity while ours appears only in running text (group, owner, manager
or service-provider sites), a name match for a company whose name is a street address
("Lyseveien 16 AS"), matches through a former name that another registered entity now carries,
a registered name that the page pairs with a different organisation number, holding companies whose contact details
appear only on the operating company's site, housing cooperatives / foundations / co-ownerships
on a manager's domain, and name-only matches. It is better to miss a site than to publish
another company's facts.

## Company-owned content (social profiles, news, jobs)

**Group, parent and sister-company sites.** A verified site can still belong to the group: the
footer of a group site often lists each subsidiary's organisation number. The site counts as a
group site when its copyright line names another organisation number (and not ours), when its
title, site name, JSON-LD organisation or copyright line name another legal entity while ours is
absent there, when the site's brand is the leading part of our name followed by a country or
unit word ("tamigo" for TAMIGO NORGE AS, "Skeidar" for SKEIDAR SUPPLY CHAIN AS), or when a
non-.no site serves four or more languages. The website is published with
`relation: "group_site"` and the owner in the note; **no social profiles, news or jobs are taken
from it** (NAV jobs matched by organisation number still are). Contact details and addresses on
the page are kept.

**Social profiles.** A profile counts only when it is the `sameAs` of the site's own JSON-LD
organisation, or when its handle matches the company: the legal or former name (with or without
generic words), the domain label, the public brand, a distinctive name word, the initials of the
name, or a close spelling variant. Opaque numeric ids count only when the link text names the
company. Links to theme or platform vendors (Wix, Squarespace, …), agencies, clients, chains and
employees do not match and are listed under `identity_resolution.social_links_excluded`.

**News.** Only real dated posts: the date must be a publication date (RSS `pubDate`, Atom
`published`, `article:published_time`, JSON-LD `datePublished`, a `<time>` marked as published,
a dated permalink, the date on the news index card, or a date printed in the article header right
after the post title or after "Publisert") – never Atom `updated`, modification times, dates
labelled "Oppdatert" or dates further down in running text. Items are dropped when they are navigation entries, the site or a
section root, product, shop, category or tag pages, theme demo or placeholder posts ("Sample
Post", lorem ipsum, "Hello world"), spam injected into a hacked site (casino, betting – then no
item of that feed is trusted), feeds that list pages instead of posts, or one title repeated three
or more times. Careers pages are matched only as their own path segment ("/karriere",
"/ledige-stillinger"), never inside an article slug. Posts must stay on
the company's domain or its newsroom (Mynewsdesk, Cision, NTB, Newsweb). News publishers
(NACE 58.12–58.14, 60, 63.91; SN2007 and SN2025 codes) get no company news, because their articles are editorial content.
Dropped items are listed under `identity_resolution.news_items_excluded`.

## Envelope

Superset of the starter `OUTPUT_CONTRACT.md`: `organisation_number`, `state`, `run`
(`run_id`, `started_at`, `completed_at`, `terminal_status`), `claims` (`field`, `value`,
`availability`, `confidence`, `relation`, `evidence_ids`, `reporting_period`, `effective_date`,
`first_observed`, `last_observed`), `evidence` (`source_url`, `source_class`, `retrieved_at`,
`content_sha256`, `claim_span`, `extraction_method`, redirects, HTTP status), `changes`,
`errors`, `operations` — plus `legal_identity`, `sections` (the seven required sections),
`field_families` (state per information family), `synthesis`, `refresh` and
`identity_resolution`. Families that were checked but not found appear as claims with
`availability` ≠ `available`, `value: null` and the reason; absence is never turned into zero.

States: `available`, `not_available`, `blocked` (the source disallows access, e.g. robots.txt), `not_applicable`,
`ambiguous` (a candidate exists but cannot be tied to the exact entity), `failed`.

## Sources, rights and policies

| Source | Access | Terms |
|---|---|---|
| Brønnøysundregistrene Enhetsregisteret and Regnskapsregisteret (data.brreg.no) | open JSON API | NLOD 2.0 |
| NAV job feed (pam-stilling-feed.nav.no) | public API with NAV's published public token (fetched at run time; `NAV_FEED_TOKEN` overrides) | [vilkår](https://arbeidsplassen.nav.no/vilkar-api): republishing active ads permitted; inactive ads are dropped; contact persons are not published |
| Verified company websites | HTTP GET, robots.txt respected, ≤ 2 concurrent requests per host | company-owned content, cited with URL, time and hash |
| Wikidata Query Service (query.wikidata.org) | one SPARQL query per 200 organisation numbers, descriptive User-Agent | CC0; only a website candidate, published only after the page-level gate |

Not used: search-engine result pages, LinkedIn/Meta/Glassdoor/Indeed/Google scraping, paid
data providers, language models. Social profiles are reported only as links published by the
verified company site; the platforms themselves are not crawled.

**URL safety:** only `http`/`https` on default ports; hosts resolving to private, loopback,
link-local or reserved addresses are refused (re-checked on every redirect); responses are capped
at 2.5 MB; at most five redirects. **Secrets:** none are needed; the only optional variable is
`NAV_FEED_TOKEN`. **Caches:** `state/` holds previous envelopes and a map from NAV ad id to
employer organisation number; both are optional and rebuilt when absent.

## Cost, request budget and runtime

- Third-party API cost: **USD 0** (no paid APIs, no model calls).
- **Request budget.** Builderr allows 2,000 requests per 100 companies. The agent counts every HTTP
  attempt (redirect hops, robots.txt, retries and failed attempts included) and enforces a hard cap
  of 16 per company (1,600 per 100 companies, a 20 % margin; `--request-budget` overrides it).
  Four requests per company that has not started yet stay reserved for the official registers.
  When the cap is reached, optional sources (company websites, NAV, Wikidata) are refused first:
  the affected families get an explicit state, and every company still receives its envelope
  with registry facts. `run-report.json` lists requests by source and any refused requests.
- **Rate limits.** HTTP 429/503 answers are honoured: the agent waits for `Retry-After` (up to 30 s),
  retries once and slows down further requests to that host.
- **Time budget.** Default 2,100 s for the whole batch (below Builderr's 45 minutes).
- Measured runtimes and request counts: see `reports/smoke-100/README.md`.

## Limitations

- Websites are found only when the register lists one, the register e-mail domain points to
  one, or a name-derived domain carries proof. Sites rendered only by JavaScript are not read.
- People on websites are extracted with conservative name/title patterns; registry roles remain
  the authoritative leadership record.
- No reviews, ratings, sentiment or third-party news: no permitted, licensed source was used.

## How this was built

Built by Claude Code (Anthropic, Claude Opus 5.5) agents working autonomously on behalf of Mirco Stege, who submits
this entry and is responsible for it. Commits carry a `Co-Authored-By: Claude` trailer; Builderr's entry guide allows
AI assistants. The agent itself uses no language model at run time.
