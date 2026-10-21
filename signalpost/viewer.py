"""Static, dependency-free HTML viewer: directory with search/filter/compare and one evidence page per company."""
from __future__ import annotations

import html
import json
from pathlib import Path

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1f;--muted:#5f6368;--line:#e3e3df;--accent:#0b5cad;--ok:#1e7d3a;--warn:#a15c00;--bad:#b3261e;--chip:#eef2f7}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--card:#1d1f22;--ink:#ececec;--muted:#a0a4a8;--line:#30333a;--accent:#7fb3ff;--ok:#6fcf8e;--warn:#f0b35a;--bad:#ff8a80;--chip:#262a31}}
*{box-sizing:border-box}html,body{max-width:100%;overflow-x:hidden}main,header{overflow-wrap:anywhere}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
a{color:var(--accent)}header{padding:16px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
header h1{margin:0;font-size:18px}header p{margin:2px 0 0;color:var(--muted);font-size:13px}
main{max-width:1100px;margin:0 auto;padding:16px}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}.controls input,.controls select{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
.controls input{flex:1 1 240px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.co{display:block;text-decoration:none;color:inherit}.co h3{margin:0 0 4px;font-size:15px}.co .meta{color:var(--muted);font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.chip{font-size:12px;padding:2px 8px;border-radius:999px;background:var(--chip);color:var(--muted)}
.chip.ok{color:var(--ok)}.chip.warn{color:var(--warn)}.chip.bad{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}.num{text-align:right;font-variant-numeric:tabular-nums}
.tablewrap{overflow-x:auto}
h2{font-size:16px;margin:0 0 8px}.state{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.state.available{color:var(--ok)}.state.not_available,.state.not_applicable{color:var(--muted)}.state.ambiguous,.state.blocked{color:var(--warn)}.state.failed{color:var(--bad)}
.claim{padding:6px 0;border-bottom:1px dashed var(--line)}.claim ul{margin:2px 0 0;padding-left:18px}.kf td:first-child{color:var(--muted);width:38%}.claim:last-child{border-bottom:0}.field{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.src{font-size:12px;color:var(--muted)}sup a{text-decoration:none;font-size:11px}
.headline{font-size:17px;font-weight:600;margin:4px 0 10px}.points{padding-left:18px}.points li{margin:4px 0}.points .chip{font-size:11px;padding:1px 6px}
details summary{cursor:pointer;color:var(--accent)}.ev{font-size:12px;border-left:3px solid var(--line);padding:4px 8px;margin:6px 0;word-break:break-word}
.ev:target{border-color:var(--accent);background:var(--chip)}
.cmp{display:none}.cmp.on{display:block}
.pick{margin-right:6px}
@media (max-width:600px){main{padding:10px}.card{padding:10px}header{padding:12px}}
"""


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _value(claim: dict) -> str:
    v = claim["value"]
    if isinstance(v, dict):
        if "amount" in v:
            try:
                return f"{float(v['amount']):,.0f} {_e(v.get('currency'))} ({_e(v.get('year'))})".replace(",", " ")
            except (TypeError, ValueError):
                return _e(v)
        if "platform" in v:
            return f'{_e(v["platform"])}: <a href="{_e(v["url"])}" rel="noopener nofollow">{_e(v["url"])}</a>'
        if "event" in v:
            return f"{_e(v['date'])} – {_e(v['event'])}"
        if "title" in v:
            bits = [f'<a href="{_e(v.get("url"))}" rel="noopener nofollow">{_e(v["title"])}</a>' if v.get("url") else _e(v["title"])]
            for k in ("date", "published", "application_due", "employer_name"):
                if v.get(k):
                    bits.append(f'<span class="src">{_e(k.replace("_", " "))}: {_e(v[k])}</span>')
            if v.get("locations"):
                bits.append(f'<span class="src">{_e("; ".join(v["locations"]))}</span>')
            return " · ".join(bits)
        return _e(json.dumps(v, ensure_ascii=False))
    s = str(v)
    if s.startswith("http"):
        return f'<a href="{_e(s)}" rel="noopener nofollow">{_e(s)}</a>'
    return _e(s)


# The five data areas Builderr's own sample uses to describe profile coverage.
DATA_AREAS = {
    "Company record": ("legal_identity",),
    "Financials": ("annual_accounts",),
    "People & locations": ("leadership", "registered_workplace", "website_locations", "people_on_website"),
    "Company website": ("official_website",),
    "Public footprint": ("social_profile", "job_posting", "company_news"),
}


def data_areas(fams: dict) -> list[str]:
    return [area for area, families in DATA_AREAS.items() if any(fams.get(f, {}).get("state") == "available" for f in families)]


def summary_row(env: dict) -> dict:
    claims = [c for c in env["claims"] if c.get("availability", "available") == "available"]

    def first(field):
        return next((c["value"] for c in claims if c["field"] == field), None)

    revenue = sorted([c["value"] for c in claims if c["field"] == "revenue"], key=lambda v: v.get("year", ""))
    result = sorted([c["value"] for c in claims if c["field"] == "net_result"], key=lambda v: v.get("year", ""))
    fams = env["field_families"]
    return {
        "org": env["organisation_number"],
        "name": env["legal_identity"].get("legal_name") or env["organisation_number"],
        "brand": env["legal_identity"].get("public_brand"),
        "muni": (env["legal_identity"].get("municipality") or "").title(),
        "form": env["legal_identity"].get("legal_form"),
        "industry": first("industry"),
        "employees": first("registered_employees"),
        "revenue": revenue[-1]["amount"] if revenue else None,
        "revenue_year": revenue[-1]["year"] if revenue else None,
        "result": result[-1]["amount"] if result else None,
        "website": first("official_website"),
        "website_declared": any(c["field"] == "official_website" and c.get("relation") == "registry_declared" for c in claims),
        "jobs": sum(1 for c in claims if c["field"] == "job_posting"),
        "news": sum(1 for c in claims if c["field"] == "company_news"),
        "social": sum(1 for c in claims if c["field"] == "social_profile"),
        "areas": data_areas(fams),
        "areas_n": len(data_areas(fams)),
        "families": sum(1 for f in fams.values() if f["state"] == "available"),
        "families_total": len(fams),
        "changes": len(env.get("changes") or []),
        "state": env["state"],
    }


def write_site(envelopes: list[dict], report: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "c").mkdir(exist_ok=True)
    rows = [summary_row(e) for e in envelopes]
    for env in envelopes:
        (out_dir / "c" / f"{env['organisation_number']}.html").write_text(company_page(env), encoding="utf-8")
    (out_dir / "index.html").write_text(index_page(rows, report), encoding="utf-8")


def _head(title: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_e(title)}</title><style>{CSS}</style></head><body>")


def index_page(rows: list[dict], report: dict) -> str:
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    fams = report.get("field_family_states", {})
    coverage = "".join(
        f"<tr><td>{_e(f)}</td><td class='num'>{v.get('available', 0)}</td><td class='num'>{sum(v.values()) - v.get('available', 0)}</td></tr>"
        for f, v in fams.items())
    return _head("Signalpost – company research") + f"""
<header><h1>Signalpost company research</h1><p>Run {_e(report.get('run_id'))} · {_e(report.get('completed_at'))} ·
{report.get('emitted_envelopes')} companies · every fact links to its source and retrieval date</p></header>
<main>
<div class="controls"><input id="q" type="search" placeholder="Search name, org. number, municipality, industry…" aria-label="Search">
<select id="f" aria-label="Filter"><option value="">All companies</option><option value="all5">All five data areas</option><option value="web">With website</option>
<option value="jobs">Hiring (job postings)</option><option value="news">With dated news</option><option value="changes">Changed since last run</option></select>
<select id="s" aria-label="Sort"><option value="families">Most verified data</option><option value="name">Name A–Z</option>
<option value="revenue">Revenue</option><option value="employees">Employees</option></select>
<button id="cmpbtn" class="chip" style="border:0;cursor:pointer;font-size:14px;padding:8px 12px">Compare selected (0)</button></div>
<div id="cmp" class="card cmp"></div>
<div id="list" class="grid"></div>
<details class="card" style="margin-top:12px"><summary>Run coverage by information family</summary><div class="tablewrap"><table>
<tr><th>Family</th><th class="num">Available</th><th class="num">Not available / other state</th></tr>{coverage}</table></div></details>
</main>
<script>
const rows={data};
const fmt=n=>n==null?'–':(Math.abs(n)>=1e6?(n/1e6).toFixed(1)+' m':Math.abs(n)>=1e3?(n/1e3).toFixed(0)+' k':String(n));
const sel=new Set();
function render(){{
 const q=document.getElementById('q').value.toLowerCase().trim(),f=document.getElementById('f').value,s=document.getElementById('s').value;
 let r=rows.filter(x=>!q||[x.name,x.org,x.muni,x.industry,x.brand,x.website].join(' ').toLowerCase().includes(q));
 if(f==='all5')r=r.filter(x=>x.areas.length===5);if(f==='web')r=r.filter(x=>x.website);if(f==='jobs')r=r.filter(x=>x.jobs);if(f==='news')r=r.filter(x=>x.news);if(f==='changes')r=r.filter(x=>x.changes);
 r.sort((a,b)=>s==='name'?a.name.localeCompare(b.name):s==='revenue'?(b.revenue||-1e18)-(a.revenue||-1e18):s==='employees'?(b.employees||-1)-(a.employees||-1):(b.areas.length-a.areas.length)||(b.families-a.families));
 document.getElementById('list').innerHTML=r.slice(0,600).map(x=>`<div class="card"><label class="pick"><input type="checkbox" data-org="${{x.org}}" ${{sel.has(x.org)?'checked':''}}> compare</label>
 <a class="co" href="c/${{x.org}}.html"><h3>${{esc(x.name)}}</h3><div class="meta">${{x.org}} · ${{esc(x.form||'')}} · ${{esc(x.muni||'')}}</div>
 <div class="meta">${{esc((x.industry||'').slice(0,60))}}</div><div class="chips">
 <span class="chip ${{x.areas.length===5?'ok':''}}" title="${{esc(x.areas.join(', '))}}">${{x.areas.length}}/5 data areas</span>${{x.revenue!=null?`<span class="chip">Rev ${{fmt(x.revenue)}} (${{x.revenue_year}})</span>`:''}}
 ${{x.website?(x.website_declared?'<span class="chip">website (register)</span>':'<span class="chip ok">website ✓</span>'):''}}${{x.jobs?`<span class="chip ok">${{x.jobs}} jobs</span>`:''}}${{x.news?`<span class="chip">${{x.news}} news</span>`:''}}
 ${{x.changes?`<span class="chip warn">${{x.changes}} changes</span>`:''}}</div></a></div>`).join('')||'<p>No companies match.</p>';
 document.querySelectorAll('[data-org]').forEach(cb=>cb.onchange=()=>{{cb.checked?sel.add(cb.dataset.org):sel.delete(cb.dataset.org);document.getElementById('cmpbtn').textContent=`Compare selected (${{sel.size}})`}});
}}
function esc(s){{return String(s??'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]))}}
document.getElementById('cmpbtn').onclick=()=>{{const box=document.getElementById('cmp');const c=rows.filter(x=>sel.has(x.org));
 if(!c.length){{box.classList.remove('on');return}}box.classList.add('on');
 const fields=[['Org. number','org'],['Legal form','form'],['Municipality','muni'],['Industry','industry'],['Employees (register)','employees'],['Revenue','revenue'],['Net result','result'],['Website','website'],['Job postings','jobs'],['News items','news'],['Social profiles','social'],['Data areas (of 5)','areas_n'],['Information families with data','families']];
 box.innerHTML=`<h2>Comparison</h2><div class="tablewrap"><table><tr><th></th>${{c.map(x=>`<th><a href="c/${{x.org}}.html">${{esc(x.name)}}</a></th>`).join('')}}</tr>`+
 fields.map(([l,k])=>`<tr><td>${{l}}</td>${{c.map(x=>`<td class="${{typeof x[k]==='number'?'num':''}}">${{k==='revenue'||k==='result'?fmt(x[k]):esc(x[k]??'–')}}</td>`).join('')}}</tr>`).join('')+'</table></div>';}};
['q','f','s'].forEach(id=>document.getElementById(id).addEventListener('input',render));render();
</script></body></html>"""


def company_page(env: dict) -> str:
    li = env["legal_identity"]
    ev_index = {e["id"]: i + 1 for i, e in enumerate(env["evidence"])}
    claims_by_id = {c["id"]: c for c in env["claims"] if c.get("availability", "available") == "available"}

    def cite(ids) -> str:
        out = []
        for cid in ids:
            c = claims_by_id.get(cid)
            for ev in (c["evidence_ids"] if c else []):
                if ev in ev_index:
                    out.append(f'<a href="#{ev}">[{ev_index[ev]}]</a>')
        return "<sup>" + "".join(dict.fromkeys(out)) + "</sup>" if out else ""

    syn = env.get("synthesis") or {}
    brief = "".join(f"{_e(s['text'])}{cite(s['claim_ids'])} " for s in syn.get("sentences", [])) or "No supported summary available."
    head = syn.get("headline") or {}
    head_html = f"<p class='headline'>{_e(head['text'])}{cite(head['claim_ids'])}</p>" if head.get("text") else ""
    points = "".join(f"<li><span class='chip'>{_e(p['kind'])}</span> {_e(p['text'])}{cite(p['claim_ids'])}</li>" for p in syn.get("key_points", []))
    metrics = "".join(f"<tr><td>{_e(m['text'].split(': ', 1)[0])}</td><td class='num'>{_e(m['text'].split(': ', 1)[-1])}{cite(m['claim_ids'])}</td>"
                      f"<td class='src'>{_e(m['formula'])}</td></tr>" for m in syn.get("derived_metrics", []))
    changed = "".join(f"<li>{_e(c['text'])}{cite(c['claim_ids'])}</li>" for c in syn.get("what_changed", []))
    events = "".join(f"<li>{_e(c['text'])}{cite(c['claim_ids'])}</li>" for c in syn.get("recent_official_events", []))
    unknown = "".join(f"<li><span class='state {_e(u['state'])}'>{_e(u['state'].replace('_', ' '))}</span> {_e(u['text'].split(':', 1)[0])}"
                      + (f" <span class='src'>– {_e(u['text'].split(' – ', 1)[1])}</span>" if ' – ' in u['text'] else '') + "</li>" for u in syn.get("unknowns", []))
    sections_html = []
    for sid, sec in env["sections"].items():
        if "claim_ids" not in sec:
            continue
        items = []
        grouped: dict[str, list] = {}
        for cid in sec["claim_ids"]:
            grouped.setdefault(claims_by_id[cid]["field"], []).append(claims_by_id[cid])
        for field, group in grouped.items():
            rows = []
            for c in group:
                refs = "".join(f'<a href="#{ev}">[{ev_index[ev]}]</a>' for ev in c["evidence_ids"] if ev in ev_index)
                extra = []
                if c.get("reporting_period"):
                    extra.append(f"period {c['reporting_period']}")
                if c.get("effective_date") and field not in ("registry_event",):
                    extra.append(f"effective {c['effective_date']}")
                if c.get("relation") and c["relation"] != "exact_entity":
                    extra.append(c["relation"].replace("_", " "))
                if c.get("carried_forward"):
                    extra.append("carried forward (source failed this run)")
                rows.append(f"{_value(c)}<sup>{refs}</sup>" + (f" <span class='src'>{_e(' · '.join(extra))}</span>" if extra else ""))
            body = rows[0] if len(rows) == 1 else "<ul>" + "".join(f"<li>{r}</li>" for r in rows) + "</ul>"
            items.append(f"<div class='claim'><div class='field'>{_e(field.replace('_', ' '))}"
                         f"{(' (' + str(len(rows)) + ')') if len(rows) > 1 else ''}</div>{body}</div>")
        fams = " ".join(f"<span class='chip'>{_e(f.replace('_', ' '))}: <span class='state {_e(s)}'>{_e(s.replace('_', ' '))}</span></span>"
                        for f, s in sec.get("families", {}).items())
        sections_html.append(f"<section class='card'><h2>{_e(sec['title'])} <span class='state {_e(sec['state'])}'>{_e(sec['state'].replace('_', ' '))}</span></h2>"
                             f"<div class='chips'>{fams}</div>{''.join(items) or '<p class=src>Nothing published for this section.</p>'}</section>")
    evidence = "".join(
        f"<div class='ev' id='{_e(e['id'])}'>[{ev_index[e['id']]}] <a href='{_e(e.get('source_url'))}' rel='noopener nofollow'>{_e(e.get('source_url'))}</a>"
        f"<br><span class='src'>{_e(e.get('source_class'))} · retrieved {_e(e.get('retrieved_at'))} · {_e(e.get('extraction_method'))}"
        f"{' · period ' + _e(e.get('reporting_period')) if e.get('reporting_period') else ''} · sha256 {_e((e.get('content_sha256') or '')[:12])}"
        f"{' · superseded' if e.get('superseded') else ''}</span><br>“{_e(e.get('claim_span'))}”</div>"
        for e in env["evidence"])
    changes = "".join(f"<tr><td>{_e(c['type'].replace('_', ' '))}</td><td>{_e(c['materiality'])}</td><td>{_e(c['summary'])}</td></tr>"
                      for c in env.get("changes") or [])
    row = summary_row(env)
    ceo = next((c["value"].split(": ", 1)[-1] for c in env["claims"] if c["field"] == "leadership" and str(c.get("value") or "").startswith("Daglig leder")), None)
    facts = [("Organisation number", row["org"]), ("Legal form", row["form"]), ("Municipality", row["muni"]), ("Industry", row["industry"]),
             ("Registered employees", row["employees"]), ("Revenue", f"{row['revenue']:,.0f} ({row['revenue_year']})".replace(",", " ") if row["revenue"] is not None else None),
             ("Net result", f"{row['result']:,.0f}".replace(",", " ") if row["result"] is not None else None), ("CEO", ceo),
             ("Website", f'<a href="{_e(row["website"])}" rel="noopener nofollow">{_e(row["website"])}</a>'
              + (' <span class="src">declared in the register, not verified on the page</span>' if row["website_declared"] else '') if row["website"] else None),
             ("Job postings", row["jobs"] or None), ("Dated news items", row["news"] or None), ("Data areas", f"{len(row['areas'])}/5 ({', '.join(row['areas'])})"), ("Information families with data", f"{row['families']}/{row['families_total']}")]
    key_facts = "".join(f"<tr><td>{_e(k)}</td><td>{v if k == 'Website' else _e(v)}</td></tr>" for k, v in facts if v not in (None, ""))
    run = env["run"]
    return _head(f"{li.get('legal_name') or env['organisation_number']} – Signalpost") + f"""
<header><h1>{_e(li.get('legal_name') or env['organisation_number'])}</h1>
<p>Org. no. {_e(env['organisation_number'])} · {_e(li.get('legal_form'))} · {_e((li.get('municipality') or '').title())}
{(' · brand “' + _e(li.get('public_brand')) + '”') if li.get('public_brand') else ''} · <a href="../index.html">all companies</a></p></header>
<main>
<section class="card"><h2>Key facts</h2><div class="tablewrap"><table class="kf">{key_facts}</table></div></section>
<section class="card"><h2>Brief</h2>{head_html}
{f'<h2>Key points</h2><ul class="points">{points}</ul>' if points else ''}
<p>{brief}</p>
{f'<h2>Derived ratios</h2><div class="tablewrap"><table><tr><th>Ratio</th><th>Value</th><th>Formula</th></tr>{metrics}</table></div>' if metrics else ''}
{f'<h2>What changed since the previous run</h2><ul>{changed}</ul>' if changed else '<p class="src">No changes since the previous run (or first run).</p>'}
{f'<h2>Recent official events</h2><ul>{events}</ul>' if events else ''}
{f'<h2>Unknown or unavailable</h2><ul>{unknown}</ul>' if unknown else ''}
<p class="src">{_e(syn.get('method', ''))}. Numbers in brackets link to the evidence below.</p></section>
{''.join(sections_html)}
{f'<section class="card"><h2>Changes (refresh diff)</h2><div class="tablewrap"><table><tr><th>Type</th><th>Materiality</th><th>Detail</th></tr>{changes}</table></div></section>' if changes else ''}
<section class="card"><h2>Evidence ({len(env['evidence'])})</h2>{evidence}</section>
<p class="src">Run {_e(run.get('run_id'))} · {_e(run.get('terminal_status'))} · completed {_e(run.get('completed_at'))} · {_e(run.get('agent'))}</p>
</main></body></html>"""
