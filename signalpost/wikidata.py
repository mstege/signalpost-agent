"""Wikidata (CC0) lookup by Norwegian organisation number (property P2333), once per batch.

Only items that carry the exact organisation number are used. Their official website (P856) becomes a
website candidate that still has to pass the page-level identity gate; nothing from Wikidata is
published without that gate.
"""
from __future__ import annotations

import json
import urllib.parse

from .net import Fetcher

ENDPOINT = "https://query.wikidata.org/sparql"
QUERY = """SELECT ?org ?item (GROUP_CONCAT(DISTINCT ?site; separator=" ") AS ?sites) WHERE {
  VALUES ?org { %s }
  ?item wdt:P2333 ?org .
  OPTIONAL { ?item wdt:P856 ?site }
} GROUP BY ?org ?item"""


def lookup(orgs: list[str], fetcher: Fetcher, chunk: int = 200) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(orgs), chunk):
        values = " ".join(f'"{o}"' for o in orgs[i:i + chunk])
        url = ENDPOINT + "?format=json&query=" + urllib.parse.quote(QUERY % values)
        snap = fetcher.get(url, accept="application/sparql-results+json", check_robots=False)
        if not snap.ok:
            continue
        try:
            rows = json.loads(snap.text)["results"]["bindings"]
        except (ValueError, KeyError):
            continue
        for row in rows:
            org = row["org"]["value"]
            sites = [s for s in row.get("sites", {}).get("value", "").split() if s.startswith("http")]
            out[org] = {"item": row["item"]["value"], "sites": sites, "snapshot": snap}
    return out
