"""Minimal HTML parsing with the standard library: visible text, links, meta tags, JSON-LD."""
from __future__ import annotations

import json
import re
import urllib.parse
from html.parser import HTMLParser

SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe"}
BLOCK_TAGS = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "section", "article",
              "header", "footer", "address", "ul", "ol", "table", "dd", "dt", "figcaption", "blockquote", "time"}


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[dict] = []
        self.meta: dict[str, str] = {}
        self.jsonld_raw: list[str] = []
        self.times: list[dict] = []
        self.feeds: list[str] = []
        self.title = ""
        self.lang = ""
        self._skip = 0
        self._in_title = False
        self._in_jsonld = False
        self._jsonld_buf: list[str] = []
        self._link_stack: list[dict] = []
        self._time_stack: list[dict] = []
        self._headings: list[str] = []
        self._in_heading = 0
        self._heading_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"]
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_jsonld = True
            self._jsonld_buf = []
            return
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        elif tag == "link":
            rel = a.get("rel", "").lower()
            typ = a.get("type", "").lower()
            if "alternate" in rel and ("rss" in typ or "atom" in typ) and a.get("href"):
                self.feeds.append(a["href"])
            if "canonical" in rel and a.get("href"):
                self.meta.setdefault("canonical", a["href"])
        elif tag == "a":
            link = {"href": a.get("href", ""), "text": [], "aria": a.get("aria-label", "") or a.get("title", "")}
            self._link_stack.append(link)
            self.links.append(link)
        elif tag == "time":
            item = {"datetime": a.get("datetime", ""), "text": []}
            self._time_stack.append(item)
            self.times.append(item)
        elif tag in ("h1", "h2", "h3", "h4"):
            self._in_heading += 1
            self._heading_buf = []
        if tag == "img" and self._link_stack and a.get("alt"):
            self._link_stack[-1]["text"].append(a["alt"])
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld_raw.append("".join(self._jsonld_buf))
            return
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._link_stack:
            self._link_stack.pop()
        elif tag == "time" and self._time_stack:
            self._time_stack.pop()
        elif tag in ("h1", "h2", "h3", "h4") and self._in_heading:
            self._in_heading -= 1
            text = " ".join("".join(self._heading_buf).split())
            if text:
                self._headings.append(text)
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_jsonld:
            self._jsonld_buf.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self.parts.append(data)
        for link in self._link_stack:
            link["text"].append(data)
        for item in self._time_stack:
            item["text"].append(data)
        if self._in_heading:
            self._heading_buf.append(data)


class Page:
    def __init__(self, url: str, html: str):
        self.url = url
        parser = _Parser()
        try:
            parser.feed(html)
            parser.close()
        except Exception:  # noqa: BLE001 - malformed markup must not crash a run
            pass
        raw = "".join(parser.parts)
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        self.text = "\n".join(line for line in lines if line)
        self.flat = " ".join(self.text.split())
        self.title = " ".join(parser.title.split())
        self.meta = parser.meta
        self.lang = parser.lang
        self.headings = parser._headings
        base = url
        self.links = []
        for link in parser.links:
            href = link["href"].strip()
            if not href or href.startswith(("javascript:", "#")):
                continue
            absolute = href if href.startswith(("mailto:", "tel:")) else urllib.parse.urljoin(base, href)
            text = " ".join(" ".join(link["text"]).split()) or link["aria"]
            self.links.append({"url": absolute.split("#")[0] if not absolute.startswith(("mailto:", "tel:")) else absolute, "text": text})
        self.times = [{"datetime": t["datetime"], "text": " ".join(" ".join(t["text"]).split())} for t in parser.times]
        self.feeds = [urllib.parse.urljoin(base, f) for f in parser.feeds]
        self.jsonld = []
        for raw_ld in parser.jsonld_raw:
            try:
                data = json.loads(raw_ld.strip())
            except ValueError:
                try:
                    data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw_ld.strip()))
                except ValueError:
                    continue
            self.jsonld.extend(flatten_jsonld(data))

    def ld_of_type(self, *types: str) -> list[dict]:
        wanted = {t.lower() for t in types}
        out = []
        for item in self.jsonld:
            typ = item.get("@type")
            names = typ if isinstance(typ, list) else [typ]
            if any(str(n).lower() in wanted for n in names if n):
                out.append(item)
        return out


def flatten_jsonld(data) -> list[dict]:
    out: list[dict] = []
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            out.append(item)
            if "@graph" in item:
                stack.append(item["@graph"])
            for key in ("publisher", "author", "hiringOrganization", "mainEntity", "provider", "parentOrganization", "subOrganization",
                        "department", "brand", "itemListElement", "item"):
                if isinstance(item.get(key), (dict, list)):
                    stack.append(item[key])
    return out


def snippet(text: str, start: int, end: int, width: int = 90) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    return ("…" if left else "") + text[left:right].strip() + ("…" if right < len(text) else "")
