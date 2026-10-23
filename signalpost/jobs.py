"""Active job vacancies from NAV's public job feed (arbeidsplassen.nav.no, pam-stilling-feed).

Access: free public API ("Alle kan bruke tenesta"), terms at https://arbeidsplassen.nav.no/vilkar-api.
Republishing active ads is permitted; inactive ads are dropped; contact persons are not published.
The run builds one in-memory index of active ads (feed window, default 120 days) and matches an ad
to a company only through the employer organisation number in the ad (main unit or one of its
registered subunits), or — when NAV omits the number — through the full legal name including the
legal-form suffix.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from .evidence import Ledger
from .names import core_name
from .net import Fetcher, Snapshot

FEED = "https://pam-stilling-feed.nav.no"
TOKEN_URL = FEED + "/api/publicToken"


def norm(name: str) -> str:
    return " ".join(re.sub(r"[^0-9A-ZÆØÅ&]+", " ", (name or "").upper()).split())


class NavJobIndex:
    def __init__(self, fetcher: Fetcher, *, window_days: int = 120, time_limit: float = 420.0, detail_limit: float = 420.0,
                 cache_path: str | None = None, detail_workers: int = 8):
        self.fetcher = fetcher
        self.window_days = window_days
        self.time_limit = time_limit
        self.active: dict[str, dict] = {}
        self.by_name: dict[str, list[str]] = {}
        self.by_first: dict[str, list[str]] = {}
        self.state = "not_started"
        self.error: str | None = None
        self.pages = 0
        self.ready = threading.Event()
        self.token: str | None = None
        self.feed_snapshot: Snapshot | None = None
        self._details: dict[str, Snapshot] = {}
        self._lock = threading.Lock()
        self.detail_limit = detail_limit
        self.detail_workers = detail_workers
        self.cache_path = cache_path
        self.by_org: dict[str, list[str]] = {}
        self.details_fetched = 0
        self.details_cached = 0
        # Bulk resolution of every active ad costs one request per ad (~10,000), far above Builderr's budget of
        # 2,000 requests per 100 companies. By default details are fetched only for ads whose employer name
        # resembles a company in the batch (see research_jobs); matching itself stays exact by organisation number.
        self.bulk_details = False
        self._cache: dict[str, dict] = {}

    def start(self) -> None:
        threading.Thread(target=self._build, name="nav-index", daemon=True).start()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _build(self) -> None:
        started = time.monotonic()
        try:
            self.token = os.environ.get("NAV_FEED_TOKEN")
            if not self.token:
                snap = self.fetcher.get(TOKEN_URL, accept="text/plain", check_robots=False)
                m = re.search(r"eyJ[\w.-]+", snap.text or "")
                if not m:
                    raise RuntimeError(f"public token unavailable ({snap.error or snap.status})")
                self.token = m.group(0)
            since = datetime.now(timezone(timedelta(hours=1))) - timedelta(days=self.window_days)
            headers = self._headers() | {"If-Modified-Since": format_datetime(since)}
            url = FEED + "/api/v1/feed"
            while url:
                if time.monotonic() - started > self.time_limit:
                    self.error = f"feed walk stopped at time limit after {self.pages} pages"
                    break
                snap = self.fetcher.get(url, accept="application/json", headers=headers, check_robots=False)
                if not snap.ok:
                    raise RuntimeError(f"feed page failed: {snap.error or snap.status}")
                body = snap.json()
                self.pages += 1
                self.feed_snapshot = snap
                for item in body.get("items") or []:
                    entry = item.get("_feed_entry") or {}
                    uuid = entry.get("uuid")
                    if not uuid:
                        continue
                    if entry.get("status") == "ACTIVE":
                        self.active[uuid] = {"uuid": uuid, "title": entry.get("title"), "business": entry.get("businessName"),
                                             "municipal": entry.get("municipal"), "modified": entry.get("sistEndret"),
                                             "url": FEED + item.get("url", f"/api/v1/feedentry/{uuid}")}
                    else:
                        self.active.pop(uuid, None)
                next_url = body.get("next_url")
                url = FEED + next_url if next_url and body.get("items") else None
                headers = self._headers()
            for uuid, ad in self.active.items():
                key = norm(ad.get("business") or "")
                if key:
                    self.by_name.setdefault(key, []).append(uuid)
            for key in self.by_name:
                self.by_first.setdefault(key.split()[0], []).append(key)
            self._fetch_all_details()
            self.state = "ready" if not self.error else "partial"
        except Exception as exc:  # noqa: BLE001
            self.state = "failed"
            self.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            self.ready.set()

    def _fetch_all_details(self) -> None:
        """Resolve the employer organisation number of every active ad (exact matching), with a cross-run cache."""
        from concurrent.futures import ThreadPoolExecutor
        cache: dict[str, dict] = {}
        if self.cache_path and os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, encoding="utf-8") as handle:
                    cache = json.load(handle)
            except (OSError, ValueError):
                cache = {}
        started = time.monotonic()
        todo = []
        for uuid, ad in self.active.items():
            hit = cache.get(uuid)
            if hit and hit.get("modified") == ad.get("modified"):
                self._register(uuid, hit.get("orgnr"))
                self.details_cached += 1
            else:
                todo.append(uuid)

        def one(uuid: str) -> None:
            if time.monotonic() - started > self.detail_limit:
                return
            snap = self.detail(uuid)
            if not snap.ok:
                return
            try:
                ad = snap.json().get("ad_content") or {}
            except ValueError:
                return
            orgnr = re.sub(r"\D", "", str((ad.get("employer") or {}).get("orgnr") or ""))
            with self._lock:
                self.details_fetched += 1
                cache[uuid] = {"modified": self.active[uuid].get("modified"), "orgnr": orgnr}
            self._register(uuid, orgnr)

        if self.bulk_details:
            with ThreadPoolExecutor(max_workers=self.detail_workers) as pool:
                list(pool.map(one, todo))
            if len(todo) and self.details_fetched < len(todo):
                self.error = (self.error or "") + f" details resolved for {self.details_fetched}/{len(todo)} uncached ads"
        self._cache = cache
        if self.cache_path and self.bulk_details:
            live = {u: v for u, v in cache.items() if u in self.active}
            try:
                os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
                with open(self.cache_path + ".tmp", "w", encoding="utf-8") as handle:
                    json.dump(live, handle)
                os.replace(self.cache_path + ".tmp", self.cache_path)
            except OSError:
                pass

    def _register(self, uuid: str, orgnr: str | None) -> None:
        if orgnr and len(orgnr) == 9:
            with self._lock:
                self.by_org.setdefault(orgnr, []).append(uuid)

    def candidates(self, names: list[str]) -> list[str]:
        out: list[str] = []
        keys = [norm(n) for n in names if n and len(norm(n)) >= 4]
        for key in keys:
            for business in self.by_first.get(key.split()[0], []):
                if business == key or business.startswith(key + " "):
                    out.extend(self.by_name[business])
        return list(dict.fromkeys(out))

    def detail(self, uuid: str) -> Snapshot:
        with self._lock:
            if uuid in self._details:
                return self._details[uuid]
        snap = self.fetcher.get(self.active[uuid]["url"], accept="application/json", headers=self._headers(), check_robots=False)
        with self._lock:
            self._details[uuid] = snap
            if snap.ok:
                self.details_fetched += 1
        return snap

    def name_candidates(self, names: list[str], limit: int) -> list[str]:
        """Active ads whose employer name matches a company name: exact, then prefix, then same first word."""
        exact, prefix, first = [], [], []
        for name in names:
            key = norm(name)
            words = key.split()
            if not words or len(words[0]) < 3:
                continue
            for business in self.by_first.get(words[0], []):
                bucket = exact if business == key else prefix if business.startswith(key + " ") or key.startswith(business + " ") else first
                bucket.extend(self.by_name[business])
        return list(dict.fromkeys(exact + prefix + first))[:limit]

    def save_cache(self) -> None:
        """Remember resolved employer numbers across runs (refreshes then cost fewer requests)."""
        if not self.cache_path:
            return
        with self._lock:
            live = {u: v for u, v in self._cache.items() if u in self.active}
            for uuid, snap in self._details.items():
                if snap.ok and uuid in self.active:
                    try:
                        orgnr = re.sub(r"\D", "", str(((snap.json().get("ad_content") or {}).get("employer") or {}).get("orgnr") or ""))
                    except ValueError:
                        continue
                    live[uuid] = {"modified": self.active[uuid].get("modified"), "orgnr": orgnr}
        try:
            os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
            with open(self.cache_path + ".tmp", "w", encoding="utf-8") as handle:
                json.dump(live, handle)
            os.replace(self.cache_path + ".tmp", self.cache_path)
        except OSError:
            pass


def research_jobs(ctx: dict, index: NavJobIndex, ledger: Ledger, deadline: float, max_details: int = 30) -> None:
    if not index.ready.wait(timeout=max(0.0, deadline - time.monotonic())):
        ledger.mark("nav_job_posting", "failed", "NAV job index not ready within the company time budget")
        return
    if index.state == "failed":
        ledger.mark("nav_job_posting", "blocked" if "401" in (index.error or "") or "403" in (index.error or "") else "failed",
                    f"NAV job feed unavailable: {index.error}")
        return
    org = ctx["organisation_number"]
    legal = ctx.get("legal_name") or ""
    subunit_orgs = {s["organisation_number"] for s in ctx.get("subunits") or [] if s.get("organisation_number")}
    names = [legal, core_name(legal)] + [s.get("name") for s in ctx.get("subunits") or []] + [ctx.get("brand") or ""]
    names = [n for n in names if n and len(n) >= 4]
    found = 0
    today = datetime.now(timezone.utc).date().isoformat()
    exact = [u for o in [org] + sorted(subunit_orgs) for u in index.by_org.get(o, [])]
    by_name = [u for u in index.name_candidates(names, limit=max_details + len(exact)) if u not in exact][:max_details]
    for uuid in list(dict.fromkeys(exact))[:60] + by_name:
        if time.monotonic() > deadline:
            break
        snap = index.detail(uuid)
        if not snap.ok:
            continue
        try:
            body = snap.json()
        except ValueError:
            continue
        ad = body.get("ad_content") or body.get("json") or {}
        if not ad:
            continue
        employer = ad.get("employer") or {}
        emp_org = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
        business = norm(employer.get("name") or index.active[uuid].get("business") or "")
        legal_key = norm(legal)
        if emp_org:
            if emp_org == org:
                relation, conf, proof = "exact_entity", 0.99, f"employer.orgnr = {emp_org}"
            elif emp_org in subunit_orgs:
                relation, conf, proof = "exact_entity_subunit", 0.98, f"employer.orgnr = {emp_org} (registered subunit of {org})"
            else:
                continue
        elif legal_key and (business == legal_key or business.startswith(legal_key + " ")) and re.search(r"\b(AS|ASA|SA|DA|ANS|NUF|BA|KS|IKS|SF|HF|KF)\b", legal_key):
            relation, conf, proof = "employer_full_legal_name", 0.9, f"employer.name = {employer.get('name')}"
        else:
            continue
        expires = (ad.get("expires") or "")[:10]
        if expires and expires < today:
            continue
        title = (ad.get("jobtitle") or ad.get("title") or "").strip()
        value = {
            "title": title[:200],
            "ad_title": (ad.get("title") or "").strip()[:200],
            "url": ad.get("link") or f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}",
            "employer_name": employer.get("name"),
            "locations": sorted({", ".join(x for x in (w.get("city") or w.get("municipal"), w.get("county")) if x)
                                 for w in ad.get("workLocations") or [] if (w.get("city") or w.get("municipal"))})[:5],
            "published": (ad.get("published") or "")[:10] or None,
            "application_due": (str(ad.get("applicationDue") or "")[:10]) or None,
            "expires": expires or None,
            "extent": ad.get("extent"),
            "engagement": ad.get("engagementtype"),
            "positions": ad.get("positioncount"),
            "source": "NAV arbeidsplassen job feed",
        }
        if ad.get("applicationUrl"):
            value["apply_url"] = ad["applicationUrl"]
        ev = ledger.add_evidence(snap, source_class="official_job_feed", method="nav_feed_entry",
                                 span=f"{proof}; title = {title[:100]}; published = {value['published']}",
                                 effective_date=value["published"])
        ledger.claim("activity", "job_posting", value, [ev], key=("nav_job", uuid), confidence=conf, relation=relation,
                     effective_date=value["published"])
        found += 1
    if found:
        ledger.mark("nav_job_posting", "available")
    else:
        ledger.mark("nav_job_posting", "not_available",
                    f"no active NAV job ad matched the organisation number or full legal name (feed window {index.window_days} days)")
