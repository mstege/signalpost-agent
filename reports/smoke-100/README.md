# 100-company smoke test

- Agent commit: `a8a4fd7` (cloned into an empty folder, fresh Python 3.12 virtual environment, no install step)
- Command: `./run.sh reports/smoke-100/smoke-companies.jsonl <out>`
- Input: `smoke-companies.jsonl` - 100 organisation numbers drawn uniformly at random (seed 20261020) from the public
  411,160-company universe (`signalpost-company-universe-2025.jsonl.gz`)
- Run: `run-20261024T095613Z`, 2026-10-24T09:56:13Z to 2026-10-24T09:57:18Z (64.7 s, cold caches)

## Contract

- Envelopes emitted: **100 / 100** (one per input, input order); validation passed: **True**
- Envelope states: {'available': 100}; terminal status: {'completed': 100}
- Published claims: 4784; every available claim has evidence with source URL and retrieval time
- HTTP attempts (redirects, robots.txt, retries and failures included): **1074** of a budget of 1600 (Builderr: 2,000 per 100 companies); by source: brreg 400, nav 267, web 406, wikidata 1; refused by the budget: 0; 429/503 answers: 0
- NAV: 214 feed pages, 52 ad details; third-party cost: USD 0.0
- Per-company runtime: p50 2637 ms, p95 42419 ms

## Availability by information family (number of companies)

| Family | available | not_available | ambiguous | blocked | failed |
|---|---:|---:|---:|---:|---:|
| legal_identity | 100 | 0 | 0 | 0 | 0 |
| public_brand | 13 | 80 | 7 | 0 | 0 |
| contact_details | 36 | 64 | 0 | 0 | 0 |
| annual_accounts | 100 | 0 | 0 | 0 | 0 |
| leadership | 100 | 0 | 0 | 0 | 0 |
| registered_workplace | 76 | 24 | 0 | 0 | 0 |
| website_locations | 8 | 85 | 7 | 0 | 0 |
| people_on_website | 9 | 84 | 7 | 0 | 0 |
| official_website | 15 | 78 | 7 | 0 | 0 |
| social_profile | 7 | 86 | 7 | 0 | 0 |
| job_posting | 2 | 98 | 0 | 0 | 0 |
| company_news | 6 | 87 | 7 | 0 | 0 |
| registry_event | 100 | 0 | 0 | 0 | 0 |

Audit exclusions in this batch: 0 group sites, 2 social links and 3 dated items excluded (listed per envelope under `identity_resolution`).

Files: `envelopes.jsonl` (results), `run-report.json` (machine-readable report), `site/index.html` (static viewer).
These outputs only show that the agent runs end to end; Builderr scores fresh official batches.
## Budget checks (same commit, same fresh clone)

- 1,000 random companies from the public universe: 1000/1000 envelopes ({'completed': 998, 'partial': 2}), validation passed: True, 407.6 s, 9707 HTTP attempts of a budget of 16000 (brreg 4001, nav 1067, web 4634, wikidata 5), 11 answers with 429/503 handled by back-off.
- Stress test, the same 100 companies with `--request-budget 500`: 100/100 envelopes ({'completed': 100}), 500 attempts (brreg 400, nav 24, web 75, wikidata 1); refused: nav 1, web 283. Registry calls keep their reserve; only optional website and NAV calls are dropped.
