# Handoff: cost audit and Jev joinability gate

Date: 2026-09-20

Repository: `/Users/p.tothandras/Code/meetapedia`

## Objective

The user asked for a project-wide review of opportunities to make Meetapedia
simpler, cheaper, or faster, with separate research into whether the newly
released TypeSafe Jev model could reduce the cost of deciding whether a page
contains a joinable community. The user then authorized implementation of all
safe improvements and asked for the result to be committed.

## Conclusions

Jev is suitable only as a page-level gate, not as a replacement for the current
extractor. The current generative extractor finds community names and produces
profile fields. Jev returns bounded typed decisions and cannot generate those
strings. The possible cascade is:

```text
raw cached page
  -> Jev confidently says no joinable community -> skip generative extraction
  -> Jev says yes or is uncertain              -> current extractor
```

Because the production extractor fleet is currently free-tier or locally owned,
Jev would primarily save scarce daily request/token capacity and latency rather
than cash. Positive pages pay both input costs, so a gate is useful only if it
safely rejects enough negative pages.

Jev must not receive production authority before a recall-first shadow test and
manual false-negative review. The incumbent label, `records_count > 0`, is
agreement with the existing extractor rather than ground truth. A local
classifier may ultimately be preferable because the corpus already provides
tens of thousands of weak labels and a local model has no API cost or vendor
dependency.

## Implemented changes

### Shared HTTP fetch pool

`scraper/fetch.py` now reuses one `httpx.AsyncClient` per asyncio event loop.
Previously every URL constructed a new client and discarded DNS, TLS, keep-alive,
and socket-pool state. Redirect targets still go through the existing SSRF and
blocked-domain checks on every hop.

Tests cover reuse within one loop and isolation between loops. The autouse test
fixture clears the pool so monkeypatched clients do not leak between tests.

### Smaller production image

Playwright moved from required dependencies to the `browser` optional dependency
group. The Dockerfile no longer installs Chromium. This is safe while
`fetch.playwright_domains` remains empty, which it currently is. The runtime
already imports Playwright only when a configured domain requires it.

To restore browser fetching in a future image:

```bash
pip install '.[browser]'
playwright install chromium --with-deps
```

and update the Dockerfile deliberately.

### Jev and local-gate benchmark

New script: `scripts/benchmark_joinability_gate.py`.

It draws an equal number of positive and zero-record pages from `cache_pages`,
in a deterministic order. Both runners see the same page text capped at the
production 8,000-character limit.

Available runners:

- `local`: dependency-free hashed character 3–5 gram multinomial Naive Bayes;
  evaluation is held out by hostname where the sample permits it.
- `jev`: calls `https://api.typesafe.ai/v1/systemone`, asks one complete
  joinability Noul question plus three diagnostic questions, and optionally
  caches raw answers in a JSON file.

The report shows, for several rejection thresholds:

- pages skipped;
- true negative pages skipped;
- incumbent-positive pages incorrectly skipped;
- retained positive recall.

It also prints the twenty lowest-scored incumbent-positive URLs for manual
review. No runner mutates the scraper database, and no production code reads
the results.

### Inline enrichment measurement

New script: `scripts/report_inline_enrichment.py`.

It reads the telemetry already stored in `cache_pages.data.enrich_log` and
reports:

- attempts/search queries;
- approximate LLM calls from fetched research URLs;
- successful records;
- success rate;
- which fields were added.

Inline contact enrichment remains enabled. It should not be disabled merely
because separate description enrichment exists; the latter does not replace
contact discovery. Run the report against production data first.

### Documentation

The durable design and external-service findings are in:

- `docs/wiki/pages/integrations/jev-joinability-gate.md`
- `docs/wiki/pages/concepts/joinable-quality-gate.md`
- `docs/wiki/index.md`
- `docs/wiki/log.md`

## Files changed

- `Dockerfile`
- `pyproject.toml`
- `scraper/fetch.py`
- `scraper/playwright_fetch.py`
- `scripts/benchmark_joinability_gate.py`
- `scripts/report_inline_enrichment.py`
- `tests/conftest.py`
- `tests/test_fetch.py`
- `tests/test_cost_audit_scripts.py`
- `docs/wiki/index.md`
- `docs/wiki/log.md`
- `docs/wiki/pages/concepts/joinable-quality-gate.md`
- `docs/wiki/pages/integrations/jev-joinability-gate.md`
- this handoff

## Verification completed

The final pre-handoff verification succeeded:

```text
590 passed, 2 warnings in 13.51s
Ruff: all checks passed
Wiki lint: 106 pages, index and link graph consistent
git diff --check: clean
pip install --dry-run --no-deps .: wheel metadata built successfully
```

The two warnings predate this work:

- Starlette TestClient warns about the old `httpx` integration;
- one test uses deprecated `datetime.utcnow()`.

## Production work still required

There is no production database in the local checkout (`data/` contains only
`.gitkeep`) and `TYPESAFE_API_KEY` was not present. Therefore no real corpus
measurement or paid Jev call has been made.

After deploying the commit, open a terminal in the production container and
confirm `/app/data/scraper.db` exists.

### 1. Run the free local baseline

```bash
cd /app
PYTHONPATH=. python scripts/benchmark_joinability_gate.py local \
  --db data/scraper.db \
  --per-class 1000 \
  | tee data/local-gate-result.txt
```

### 2. Obtain a TypeSafe key

Create a key in <https://console.typesafe.ai/> and expose it only as the
`TYPESAFE_API_KEY` environment variable. Never commit it or paste it into a
handoff/result file.

### 3. Run a small Jev smoke benchmark

```bash
cd /app
test -n "$TYPESAFE_API_KEY" && echo present || echo missing

PYTHONPATH=. python scripts/benchmark_joinability_gate.py jev \
  --db data/scraper.db \
  --per-class 50 \
  --concurrency 4 \
  --cache data/jev-gate-cache.json \
  | tee data/jev-gate-result-100.txt
```

On HTTP 429, rerun with `--concurrency 1`. The response cache prevents repeated
spend for successful pages.

### 4. Run the larger Jev benchmark

```bash
PYTHONPATH=. python scripts/benchmark_joinability_gate.py jev \
  --db data/scraper.db \
  --per-class 1000 \
  --concurrency 4 \
  --cache data/jev-gate-cache.json \
  | tee data/jev-gate-result-2000.txt
```

Review every incumbent positive below the candidate threshold and at least 50
confident negatives manually. Stratify the review by Hungarian, German,
Swedish, and Indonesian pages. A tentative requirement is 99–99.5% positive
recall, but manually confirmed false negatives determine whether any production
threshold is acceptable.

### 5. Measure inline enrichment

```bash
PYTHONPATH=. python scripts/report_inline_enrichment.py \
  --db data/scraper.db \
  | tee data/inline-enrichment-report.txt
```

Do not flip `pipeline.enrich_communities` until this result is reviewed. A yield
below roughly 10% is a strong reason to disable or narrow the path; useful email
or contact discovery may justify retaining it even at lower volume.

## Preconditions for a production gate

If the benchmark supports proceeding, a new implementation still needs:

1. a dedicated gate fingerprint containing the exact Jev model and questions;
2. an auditable, reversible negative-decision cache distinct from the extraction
   cache;
3. uncertain/error/429 cases always falling through to normal extraction;
4. no permanent empty extraction written from a classifier result;
5. language/market-specific thresholds if measurement shows calibration drift;
6. daily cost/request accounting and an explicit Jev budget;
7. an admin release mechanism comparable to extraction quarantine;
8. regression tests proving a provider outage cannot silently classify a page
   as empty.

Do not implement the production gate before the real benchmark and human review.

## Useful external references

- TypeSafe launch and pricing:
  <https://typesafe.ai/blog/introducing-system-one-models-and-jev>
- TypeSafe quick start:
  <https://docs.typesafe.ai/introduction/quickstart>
- Jev 1.13 limitations:
  <https://docs.typesafe.ai/model-jaggedness/jev-1.13>
- TypeSafe extraction cascade:
  <https://docs.typesafe.ai/cookbooks/sde_cascade>
