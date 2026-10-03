---
type: Runbook
title: Deployment (Coolify / Hetzner)
description: Docker on Coolify; persist only /app/data and /app/config; required and optional env vars.
tags: [operations, deployment, coolify, docker, env]
timestamp: 2026-10-03
resource: Dockerfile
---

# Deployment (Coolify / Hetzner)

*The app runs on Coolify (Hetzner) via Docker as a FastAPI/uvicorn server on port 8000. There is no local dev server — read code/templates directly for verification.*

## Volumes

Persist only the runtime dirs, never the whole `/app` tree (that would hide updated code from new images):

- `/app/data` — `scraper.db` (all communities + cache).
- `/app/config` — YAML edits made through the admin UI.

## Deploy behavior

- Push to `main` → webhook deploy (~2–3 min build). Deploys **restart the container
  and kill any running pipeline run** — re-trigger the collector/extractor manually
  after deploying inside a work window.
- Concurrent deploys: Coolify queues or fails the second one — after overlapping
  deploys verify YOUR commit SHA in the Deployments list, not just app health
  (see [[2026-07-ga4-env-buildtime-failure]]).
- Deploy-heavy days fill the disk with stale images — runbook: [[coolify-disk-cleanup]].

## TLS between Cloudflare and the origin

Both zones run **Full (strict)** since 2026-10-03, against **Cloudflare Origin CA certificates**
(`kozossegek.com` + `*.kozossegek.com` to 2041-05-09, `meetapedia.com` + `*.meetapedia.com` to
2041-09-29) in `/data/coolify/proxy/certs/`, loaded by `/data/coolify/proxy/dynamic/tls.yml`
(`tls.certificates`). The directory is root-only; the `claude` SSH user reaches it through
`docker run --rm -v /data/coolify/proxy:/p alpine …`.

Why: Traefik's Let's Encrypt HTTP-01 renewal cannot work behind the orange cloud. kozossegek's LE
certificate expired on 2026-08-11 unnoticed, because that zone was plain Full; meetapedia's would
have expired 2026-12-11 under Full (strict) — a 526 on the whole site. An Origin CA certificate for
the same host was already on disk since May, but `tls.yml` was a broken heredoc (indented, with a
trailing `EOF`) and never loaded.

Two traps: **a router's ACME certificate beats a file certificate for the same host**, so the LE
entries for `kozossegek.com` and `meetapedia.com` were removed from `acme.json` (backup
`acme.json.bak-20261003`) before a proxy restart; with a matching file certificate Traefik no longer
requests one. And **`docker restart coolify-proxy` answers 404 for a few seconds** — Google's live
URL test hit exactly that on 2026-10-03. `www` on meetapedia is a Cloudflare Redirect Rule, not a
Traefik route.

## Environment variables

- **Required:** `ADMIN_PASSWORD` (gates the entire `/admin` UI; unset → 503). `ADMIN_USER` defaults to `admin`.
- **Search:** `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` (the sole search provider since the 2026-07 cleanup — see [[dataforseo]]).
- **Extraction:** `DEEPSEEK_API_KEY` (the sole extractor — see [[deepseek]]). `GROQ_API_KEY`, `SERPER_DEV_API_KEY`, `SEARCH_WORKER_TOKEN` are obsolete — remove them from Coolify.
- **Email (Resend):** `RESEND_API_KEY`, `FEEDBACK_EMAIL`, `RESEND_FROM`, `REPORT_EMAIL`. Optional — missing = silent no-op. See [[resend-email]].
- **Analytics:** `GA4_PROPERTY_ID`, `GA4_CREDENTIALS_JSON` — **runtime-only**, never build-time ([[ga4-reporting]]).

## CSS build

`scraper/web/static/css/app.css` is gitignored; Docker builds it from `input.css` via `pytailwindcss` at image build time. For local edits, maintain `app.css` by hand; committing `input.css` changes suffices for production. Note the runtime pages also load the Tailwind **CDN JIT** — see [[tailwind-cdn-jit-large-lists]].

## Tests / lint

```
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/python scripts/lint_wiki.py
```
The repo requires Python ≥ 3.12.

After deploying, verify from outside — the container's own health signals
missed four consecutive outages on 2026-08-16. See [[production-monitoring]].
