# 100-company smoke test

- Agent commit: `9040dbd` (cloned into an empty folder, fresh Python 3.12 virtual environment, no install step)
- Command: `./run.sh reports/smoke-100/smoke-companies.jsonl <out>`
- Input: `smoke-companies.jsonl` - 100 organisation numbers drawn uniformly at random (seed 20261020) from the public
  411,160-company universe (`signalpost-company-universe-2025.jsonl.gz`)
- Run: `run-20261020T145546Z`, 2026-10-20T14:55:46Z to 2026-10-20T15:00:49Z (302.5 s, cold caches)

## Contract

- Envelopes emitted: **100 / 100** (one per input, input order); validation passed: **True**
- Envelope states: {'available': 100}; terminal status: {'completed': 99, 'partial': 1}
- Published claims: 4785; every available claim has evidence with source URL and retrieval time
- Requests: 10519 (including 9657 NAV ad lookups that build the shared job index once per batch); third-party cost: USD 0.0
- Per-company runtime: p50 2592 ms, p95 42658 ms

## Availability by information family (number of companies)

| Family | available | not_available | ambiguous | blocked | failed |
|---|---:|---:|---:|---:|---:|
| legal_identity | 100 | 0 | 0 | 0 | 0 |
| public_brand | 13 | 82 | 5 | 0 | 0 |
| contact_details | 36 | 64 | 0 | 0 | 0 |
| annual_accounts | 100 | 0 | 0 | 0 | 0 |
| leadership | 100 | 0 | 0 | 0 | 0 |
| registered_workplace | 76 | 24 | 0 | 0 | 0 |
| website_locations | 8 | 87 | 5 | 0 | 0 |
| people_on_website | 9 | 86 | 5 | 0 | 0 |
| official_website | 13 | 82 | 5 | 0 | 0 |
| social_profile | 8 | 87 | 5 | 0 | 0 |
| job_posting | 2 | 98 | 0 | 0 | 0 |
| company_news | 6 | 89 | 5 | 0 | 0 |
| registry_event | 100 | 0 | 0 | 0 | 0 |

Files: `envelopes.jsonl` (results), `run-report.json` (machine-readable report), `site/index.html` (static viewer).
These outputs only show that the agent runs end to end; Builderr scores fresh official batches.
