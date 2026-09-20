# Wiki Log

Date-grouped operation log, newest first. See [SCHEMA.md](SCHEMA.md).

## 2026-09-20
- **Integration**: Jev runs on Cloudflare once the **AI Gateway prepaid balance** is topped up —
  not the Workers Paid plan and not the neuron allowance (error 2021, "add money to your gateway or
  use BYOK"). Two facts only the live service gave: Workers AI nests the answer twice
  (`result.result.answers`), and concurrency 6 draws 429 — one rate limit ended a 1,200-page run at
  page 422, since `asyncio.gather` loses every in-flight page with the first exception. The runner
  now retries 429/5xx with doubling backoff and runs at concurrency 3. Measured price, from real
  `usage`: 2,136 input tokens per page — $0.11 for the comparison run, $0.09/day for new pages,
  $11.49 to gate the entire corpus once. [[jev-joinability-gate]].
- **Measurement**: **Jev on Cloudflare answers 402 Payment Required** — it is a third-party partner
  model, outside the free neuron allowance. The smoke test established it and ruled out the
  alternative explanation in the same minute: the same token in the same container got 200 from
  `@cf/openai/gpt-oss-20b`, so the extraction fleet's daily quota was never the issue and was never
  touched. Measuring Jev therefore costs money by every route (TypeSafe waitlist, Cloudflare Workers
  Paid, or a gateway account). Not urgent: the free local gate already buys ~11% at 99.5% recall.
  [[jev-joinability-gate]].
- **Integration**: The gate benchmark can reach Jev through Cloudflare Workers AI
  (`--provider cloudflare`), because TypeSafe's console is waitlisted and the `CLOUDFLARE_API_TOKEN`
  is already configured. Same model and questions; the envelope nests under `input` and the answer
  may nest under `result`, and tests assert both wire formats since neither can be checked live yet.
  Noted with its cost: that route spends the same 10,000-neuron daily allowance gpt-oss-20b
  extraction runs on, so a full benchmark costs the fleet a day of that provider.
- **Correction**: The local gate's saving is **11.1%, not 26.8%**. Re-run at `--per-class 3000`
  (1,200 held-out pages against 406), the same model on the same corpus gives 99.46% recall while
  rejecting 13.6% of negatives — the earlier figure rested on two false negatives and was optimistic
  by more than double. 98.75% recall buys ~20%. Quote the larger run. It also sharpens the Jev
  question: the free gate's bar is now 11% at 99.5% recall, and a paid one has to clear that by a
  wide margin, not match it. [[jev-joinability-gate]].
- **Measurement**: The calibrated local gate scores **98.9% positive recall while rejecting a third
  of negatives** (406 held-out pages, 189 positive) — against 74.6% for the same model on the same
  data before calibration, which confirms the first run measured the score rather than the corpus.
  On the real 81.8%-negative corpus that is ~27% of all extraction work removed for ~1% of
  communities lost, free and with no vendor. Jev's bar is now this, not zero. Two caveats on the
  record: 98.9% rests on two false negatives, so a larger sample comes first, and the lowest-scored
  positives include a Guardian article and a booking page — pages where the incumbent label is
  itself suspect. [[jev-joinability-gate]].
- **Measurement**: The local joinability gate scored **74.6% positive recall** on production, far
  under the 99% a gate needs — but the threshold column was inert (0.01 -> 0.50 moved recall half a
  point), which says the score was uncalibrated rather than the corpus inseparable. Naive Bayes sums
  one log-probability per n-gram, so the total scaled with page length and every page sat on the ±50
  clamp; each "worst positive" scored exactly 0.0000. The scorer now normalizes per n-gram and fits
  Platt scaling on a calibration split held back by hostname. Measured on a mixed-difficulty
  synthetic corpus: 0% of pages in the 0.02-0.98 band before, 27% after, and the threshold moves the
  decision. [[jev-joinability-gate]].
- **Post-mortem**: [[2026-09-benchmark-materialized-the-corpus]] — the Jev gate benchmark's sampler
  did one `fetchall()` over every extracted page's text (128,072 rows × ~30 KB) to keep 2,000 of
  them, and was killed on production with 237 MB of RAM free. Selection now happens on keys, served
  from the `idx_cache_pages_done` partial index, with `substr` truncating in SQL: 888 MB -> 86 MB on
  a 739 MB corpus, and flat (100 MB) on a 2.6 GB one where the old shape was linear. Also measured
  while there: **81.8% of extracted pages yield zero communities** (104,795 of 128,072) — the number
  the gate question turns on, and one nothing in the repo had ever written down.
- **Fix**: meetapedia.com now submits its venue and person pages. It listed 38,108 URLs and not one
  of them, because the branch that adds those pages was guarded `if not is_meetapedia` and carried
  prefixes — `/venue/`, `/person/` — for an English URL scheme that was never built:
  `/vienna/venue/x` answers 404, `/vienna/helyszin/x` answers 200 on the same domain. The fix uses
  the routes that exist. HU cities are already excluded from that edition's set (they canonicalize
  to kozossegek), so this adds the international corpus and no duplicates. kozossegek.com was
  unaffected and already listed 9,282 venues and 11,554 people. `tests/test_sitemap_routes.py`
  now seeds both entity types, so its existing "every submitted URL serves 200 and
  self-canonicalizes" contract covers them.
- **Creation**: [[structured-data]] — a structured-data audit and what it found. Only the community
  page and explore emitted JSON-LD; venue and person pages, the two shapes search engines read best,
  had none, and the community object carried eight properties out of the two dozen the record holds.
  Venue (`Place`, narrowed by `venue_type`), person (`Person` with `memberOf`) and home
  (`WebSite` + `SearchAction` + `Organization`) are now generated, and the community object gained
  email, telephone, foundingDate, keywords, knowsLanguage, member, a PostalAddress and
  mainEntityOfPage. The tag moved into `public_base.html` so a new page type cannot forget it.
  Listing pages still get no ItemList on purpose — that is a DB-proportional list in the document.
- **Fix**: The community card ends with a button per destination — the group's own page, then each
  source page — after a reader reported they could not work out how to join. The links were all
  already on the page; none of them looked like the next step. The enriched `short_description` now
  also appears on the page it describes, instead of only in the `<meta>` tag.
- **Fix**: Topic labels now follow the page's language everywhere. The community page's report-form
  picker and the person page's community chips were built from `app.py:TOPIC_LABELS`, the English
  fallback, under context keys that `**lang_context(request)` does not override. A Hungarian reader
  chose between "Religion & Faith" and "Book Club" — with "Hagyományőrzés", "Baba & Szülő" and
  "Kisállat" mixed in, because those three have no English label in that dictionary at all. The mix
  is what identified it as the wrong dictionary rather than a missing translation. Reported with a
  screenshot; `tests/test_topic_label_language.py` holds both directions.
- **Simplification**: The twin cost-saver crons and the startup-recovery plan are deleted.
  Nothing had registered them since the worker shipped on 2026-08-18 — `_cron_run` was written
  and never called, `_startup_plan` was reachable only through a `_startup_run` that returns
  immediately under `worker_enabled`, and eight `settings.yaml` keys were read on every boot
  without being able to change what the process did. `main.py` 929 → 624 lines; four schedule
  keys remain. What made this worth doing is not the lines: the config claimed extraction runs
  00:30→10:00 and the startup plan claimed a deploy resumes a collection, and both were false.
  `run_pipeline(stop_at=…)` survives, still tested, unused in production. [[continuous-worker]],
  [[scheduler-disabled-no-cron]].
- **Optimization**: Same-city duplicate detection filters in SQL instead of in Python.
  `detect_community_candidates` runs once per processed pair, and it was reading every
  visible community row — blob column included, ~45,785 of them in production — to keep one
  city's few dozen. `get_all_communities` now takes an optional `city`, served by
  `idx_comm_city_topic`; a test asserts the plan is a SEARCH, so dropping that index fails
  loudly instead of silently returning to a scan. `wrong_city` deliberately still reads the
  whole table: its question is global.
- **Creation**: Added [[jev-joinability-gate]] and a recall-first shadow benchmark for the
  newly released Jev 1.13. The script measures Jev as a page gate, not an extractor — it cannot
  generate community names or fields — and compares it on the identical deterministic sample with
  a dependency-free local character n-gram classifier. Neither runner has production authority;
  incumbent `records_count` is a weak label, and the lowest-scored positives are printed for manual
  review before any threshold may drop work. A JSON response cache prevents repeated Jev spend.
- **Creation**: Added `report_inline_enrichment.py`, which converts the already persisted
  `enrich_log` into attempts/searches, approximate LLM calls, successful records, yield, and fields
  added. Inline enrichment remains on until production evidence says its contact data is not worth
  the extra search and call; description enrichment is not a substitute for that evidence.
- **Optimization**: HTTP page fetches now share one `httpx` connection pool per event loop, matching
  search and extraction. The old one-client-per-URL path threw away DNS, TLS, and keep-alive state
  and recreated the socket pattern behind the earlier file-descriptor incident. Redirect targets
  still receive the same per-hop SSRF and blocked-domain checks.
- **Simplification**: Playwright is an optional `browser` dependency and Chromium is no longer
  installed in the production image while `playwright_domains` is empty. The runtime already imports
  it only when a domain is configured; paying the image, build, deploy, and dependency cost on every
  release supplied no capability in the current configuration.

## 2026-09-19
- **Correction**: **SambaNova was declined on a misreading.** The 2026-09-05 entry recorded "20 RPM /
  **20 RPD** / 200K TPD on its own rate-limit page, i.e. half of OpenRouter's already-tightest
  allowance". Read again at the source (docs.sambanova.ai/docs/en/models/rate-limits, 2026-09-19):
  **RPM and RPD are per model**, and the free tier lists five — `DeepSeek-V3.1`,
  `Meta-Llama-3.3-70B-Instruct`, `gpt-oss-120b`, and the preview `DeepSeek-V3.2` and `gemma-4-31B-it`
  — at 20 RPD and 200K TPD each. That is **~100 requests a day**, which is not half of anything: it is
  about what Cloudflare actually contributes (95/day, and the neuron arithmetic says our `rpd: 100`
  there is roughly exact, not conservative). The tier is standing rather than credit-based and applies
  "when there is no payment method linked with your account", so criterion 4 holds too. Adding it
  needs **five provider entries sharing one key**, because `rpd` lives on `ProviderSpec` and not on
  `ModelSpec` — the same shape the 2026-09-10 Gemini split took, and for the same reason.
- **Correction, next day, with a key in hand**: **SambaNova is declined again — and this time it is
  measured, not read.** An account was created and `SAMBANOVA_API_KEY` set, and `GET /v1/models`
  answers 200 with seven models. Every free-tier model then answers the *same* thing to a real chat
  call: `PAYMENT_METHOD_REQUIRED — "A payment method is required. Add one at
  cloud.sambanova.ai/plans/billing to continue."` All five, and three attempts on `DeepSeek-V3.1`,
  whose first refusal was a transient "experiencing high demand" and therefore looked like the
  exception. So the sentence the rate-limit page leads with — Free Tier applies "when there is no
  payment method linked with your account" — defines the tier rather than granting access: without a
  card the API serves nothing. Nothing is configured and the key in Coolify is inert. Adding a card
  would make this a paid provider, which `allow_paid: false` and `daily_budget_usd: 0.00` exist to
  prevent. **Twice now this provider has been judged from its documentation and twice the
  documentation was the wrong source** — first the per-model limits, now the tier itself. That is the
  free-models skill's own rule, earned again: do not read docs, ask the API.
- **Observed**: Gemini and Mistral **no longer publish free-tier limits at all**. Google's rate-limits
  page has no free table and defers to the AI Studio dashboard; Mistral's numbers sit behind
  `admin.mistral.ai`. Our `rpd` for both is therefore an estimate that cannot be re-verified from a
  vendor page, and the only thing keeping it honest is the ledger's learned ceiling — 406 on
  2026-09-17 and 382 on 2026-09-19 for Mistral, against 500 configured. Recorded in
  `providers.yaml` beside both numbers, and deliberately *not* hand-corrected: a figure typed from
  one day's observation goes stale the way Groq's `rpd: 14400` did, while the learned ceiling
  re-measures daily for the price of one refused call. Groq's own table still matches our config
  exactly (30 RPM / 1,000 RPD / 200K TPD), and OpenRouter's $10-lifetime rule that lifts `:free` from
  50 to 1,000 a day is confirmed — the ~$10.80 purchase is doing what it was bought for.
- **Observed**: GitHub Models is **fully retired**, not browning out — "As of July 30, 2026… the
  playground, model catalog, inference API, and bring your own key (BYOK) are no longer available to
  any customer." And Cerebras has no free tier to return to: "Is there a permanently free tier?
  **No.**… $5 in credits that expire 30 days after they're granted", with a verified payment method
  required. Both entries stay in the catalogue as headstones, now with the primary-source sentence
  that closes the question — `gemma-4-31b`'s quality of 80, still the highest number in the file, is
  not an argument for re-enabling an account that cannot be free.
- **Fix**: scoring charges the quota ledger. `score_model` drives an `_ApiExtractor` directly
  rather than through `FallbackExtractor`, so nothing recorded its calls — a 20-page run over three
  models is **60 real requests** the router never learned about, after which it planned the rest of
  the day against an allowance that much too large. The provider charges them either way; only our
  books disagreed. Same under-counting the 2026-09-12 enrichment fix removed, in the one place still
  doing it, and it is why the 2026-09-18 evening's `openrouter used=337` never moved while 40 calls
  went out.
  Reserved before the call and settled after, the order `FallbackExtractor` already uses, so a slot
  is taken while the request is in flight rather than after it returns. Failures count too, for the
  reason they count everywhere else. Never raises: losing a ledger write costs one number, losing
  the run costs the minutes of LLM calls that produced it — the rule `_note_router` follows.
  **Not changed, deliberately**: scoring still runs when a provider is out of budget. Gating it on
  `has_capacity` would be a second decision — whether an operator's measurement may spend the
  crawl's allowance — and the defect here was that the spending was *invisible*, not that it
  happened.
  Verified by mutation: removing the success-path charge fails the new test, which then names what
  is missing (1 of 3 calls settled). Also removes a duplicated docstring on `ModelRouter.note`,
  where the second string literal was a silent no-op expression.

- **Decision**: `deepseek/deepseek-v4-flash-0731:free` scores **76** and its cap goes to **8,000**.
  Measured on 20 pages against its OpenRouter stablemates: 76 answering 14/20, versus nemotron's 68
  answering 10/20 and gemma's nothing. It is the fleet's second-best model now, behind only
  `mistral-small` — which has been blocked most of the week.
  The number also settles why it was worth adding. At the global 1,500-token cap this same snapshot
  answered **4 of 14** on 2026-08-27; at 4,000 it answers **14 of 20**. The model was never the
  problem, the cap was. And the run said where the rest went: all **six** remaining failures were
  `llm_output_truncated` — the same wall, further along — so the cap doubled again. The trade is
  one-sided on this provider only: OpenRouter budgets in *requests per day*, so a truncated call
  spends a request and returns nothing, and room to finish the JSON costs seconds while buying back
  whole requests. On Groq the same change would reserve tokens against an 8,000-per-minute window
  before generating.
- **Fix**: `groq/qwen/qwen3.6-27b` removed — retired at Groq, and this time the UNLISTED hint is a
  verdict: a real call returns HTTP 404, "The model `qwen/qwen3.6-27b` does not exist or you do not
  have access to it". Groq serves `qwen3.8-27b` in its place. **Deliberately not replaced**: it
  scored 55, below both gpt-oss models already there, so its successor would occupy a slot the
  router reaches only when the better two are spent — while costing one preflight probe per run,
  which is what the dead entry was costing. Roughly **80 guaranteed 404s a day**, part of the 746
  preflight calls on 2026-09-17 that looked like an unavoidable design cost.
- **Observed**: gemma's 0/20 is **not** a quality result. Every failure was `api_rate_limited` with
  a 60-second wait — Google AI Studio refusing upstream, which is what OpenRouter's `:free` gemma
  sits behind, and the same refusal a manual call hit on 2026-09-05. Left at 52; a provider that
  will not answer has not been measured.
- **Observed**: `scoring.py` calls `_ApiExtractor.extract` directly and never touches the ledger —
  no `note_call`, no `reserve_call`. A scoring run spends the provider's real daily allowance and
  the router never learns of it, which is the same class of under-counting the 2026-09-12 enrichment
  fix removed. **Correction to the 2026-09-05 entry**: the claim that scoring "cost 86 of the day's
  95 Cloudflare calls" was wrong — those 42 scoring calls were never charged, so the crawler spent
  that budget. Not fixed here: whether an operator's measurement should eat the crawl's allowance is
  a decision, and the silent part is what makes it wrong either way.

## 2026-09-18
- **Creation**: `deepseek/deepseek-v4-flash-0731:free` added to the OpenRouter entry, unscored,
  with a per-model `max_output_tokens: 4000`. It is new in OpenRouter's free catalogue since
  2026-09-05 — and it is a model we have already measured, as the *paid* sibling. On 2026-08-27
  the 0423 snapshot scored 80 answering 13 of 14 Hungarian pages; this 0731 snapshot scored the
  same 80 and answered **4 of 14**. Every failure was `llm_output_truncated`: it is a reasoning
  model, the reasoning is emitted as output, and it spent the 1,500-token cap before the JSON
  closed. So 0731 is not a worse model, it is a model our cap strangles — and on a `:free` slug the
  usual objection to raising the cap does not apply, because OpenRouter bills nothing and budgets
  in requests per day rather than tokens. The cap costs seconds, never money; the same reasoning
  localgpu's 4,000 already rests on.
  It buys **quality, not capacity** — it shares the provider's 1000/day pool with the two models
  above rather than adding to it — so it is worth having only if it beats nemotron's 58. `quality: 0`
  until `POST /v1/score` says; putting the sibling's 80 here would be borrowing the number from the
  snapshot that worked.
- **Observed**: OpenRouter's free catalogue moved by five since 2026-09-05 —
  `deepseek-v4-flash-0731`, `qwen/qwen3.8-27b`, `nex-agi/nex-n2.5-{mini,pro}`,
  `inclusionai/ling-3.0-flash-vl` in; both `minimax` slugs out. Neither of our two configured slugs
  is affected. Checked unauthenticated against the public catalogue; **the rest of the fleet was
  not checked** — Groq, Gemini, Mistral and Cloudflare need `ROUTER_API_KEY` for
  `check_free_models.py --remote`, so "no new models" is not a claim that can be made about them
  today.
- **Fix**: `extract.py`'s pooled HTTP client is keyed by the loop object too. `b263436` found and
  fixed exactly this in `search.py` the day before — `id(loop)` is unique only while the loop is
  alive, CPython reuses the address once it is collected, and a fresh loop is handed the dead one's
  client — but the identical copy one module over was left behind. Same `WeakKeyDictionary`, same
  conftest fixture, nested one level deeper because this pool also keys by timeout. Production has
  a single loop and neither copy ever fired there; the suite has one per test, which is where it
  bites.
  **Verified by mutation**, and the first attempt at that was wrong: mutating a *copy* of the pool
  left `_http_clients` empty, so the test passed and appeared to prove nothing was caught. Restoring
  the `id()` key on the real pool fails it immediately, with the defect in plain sight — the second
  loop handed the same client object, at the same address. A mutation that does not reproduce the
  original code path tests the mutation, not the code.
- **Observed**: `localgpu` recovered without a config change. Five days at 100% failure (142 calls,
  142 errors) became **1,519 calls with 239 errors** and 3.1M tokens — more than any other provider
  — and `config/providers.yaml` is untouched, so it was the machine or the tunnel, possibly helped
  back in by `0ff4cb6` ("a recovered provider rejoins within one pause, not one restart"). Output
  followed: new communities 43 → **215**, pages 288 → **510**, people 129 → **291**.
- **Observed**: Mistral has taken localgpu's place as the broken one — 453 calls, **415 refusals**,
  38 served — with its config equally untouched, and it was healthy two days earlier (475 calls, 25
  errors). It oscillates rather than being misconfigured, which is why the catalogue was left alone.
- **Observed**: preflight is 746 of the day's 3,716 calls (20%), up from 214, and it is **not**
  waste to reclaim cheaply. It costs one probe per model per run and the run count was unusually
  high; `run_pipeline` already returns before the main preflight when there are no pairs to run
  ("every path below this point has new work pending by definition"), so the obvious guard is in
  place. Cutting it further means a process-level TTL on the probe result, traded against the
  2026-07-24 window that produced 5 records from 1,368 pages because a dead model went unnoticed —
  a decision, not a cleanup.
- **Open**: the 20:59 UTC run on 2026-09-17 ended `run unfinished (still running, container
  restart, or OOM)`. Deploy churn explains the day's five zero-pair runs (three land between
  commits at 18:35/18:51 and right after 19:27) but **not** this one, which is 1.5 h after the last
  deploy. Its three candidate causes cannot be told apart without the container log.

## 2026-09-17
- **Update**: the funnel answers contactability for **people** and in **addresses**, not only in
  community rows — `persons`, `persons_with_email`, `persons_email_distinct` and
  `records_email_distinct` in `get_funnel_counts`, so `/v1/funnel` carries them too. Prompted by a
  question the existing numbers could not answer: production has **7,494 of 43,748 communities with
  an email** (17.1%, against 544 with a website), and nothing at all was countable for `persons`
  without opening the production database through the browser-only terminal. Distinct addresses
  matter because rows overstate reach: one address appears on a club's page and on its leader's, and
  a community centre's office address serves every group in the building. Addresses are folded on
  case and whitespace — verified by mutation, because the first version of the test asserted the
  distinct counts without distinguishing them from the row counts and passed with the folding
  removed. Contactability is still not a send list: see [[acquisition-funnel]] for the Advertising
  Act constraint, and `subscriptions` remains at zero.
- **Correction**: the entry below blaming the API `restart` for a 404 outage **overstates what was
  measured**, and the correction is worth more than the original. Watching the next push deploy
  end to end: `d2efea8` served 200 while Coolify still said `in_progress`, then Traefik answered the
  same bare `404 page not found` from 19:11:06 until somewhere before 19:14:57 UTC, then 200 again
  with no intervention. **Every deploy has a ~4-minute window with no route** — which is what
  `smoke_test.py --wait 420` has always been waiting for. So the 18:41 404 is equally consistent with
  the 18:37 push deploy's own window, the restart is not proven guilty, and the forced deploy at
  18:42 most likely *extended* the outage by starting the swap again rather than ending it. The
  "35 minutes" in that entry is the gap between my checks, not measured downtime. What survives: a
  `text/plain` 404 is the proxy rather than the app, `status=running:healthy` says nothing about
  routing, and the fix for a 404 that outlives the deploy window is the forced deploy. Recorded
  rather than edited away, because a wrong root cause that was already committed is exactly the kind
  of thing this log exists to catch — see [[production-monitoring]] for the ordered check.
- **Fix**: the suite's one order-dependent test was not a timing fluke but a **pooled HTTP client
  keyed by `id(loop)`**. `search.shared_client()` caches one client per event loop; pytest-asyncio
  builds a loop per test, and CPython reuses an address once an object is collected, so a fresh loop
  could be handed a dead one's client — in the suite, a client built while some earlier test had
  monkeypatched `httpx.AsyncClient`, i.e. *another test's fake*. Two tests in `test_search.py` were
  affected and which one failed depended on the file set: with `tests/test_enrich_loop.py` present
  the whole suite passed, without it `test_standard_search_falls_back_to_us_location_for_unknown_locale`
  failed every time, and running `test_search.py` alone failed a different test with
  `'FakeClient' object has no attribute 'is_closed'`. One cause, three faces. The pool is now a
  `WeakKeyDictionary` keyed by the loop object, so an entry dies with its loop; a conftest fixture
  clears it between tests anyway, because isolation should not depend on when the collector runs;
  and the three fakes declare `is_closed`, since `_search_standard` asks for a client per poll and a
  fake that omits it is not standing in for a client. Verified by mutation: restoring the `id()` key
  turns the new regression test red.
- **Observed**: the `restart` warning in [[production-monitoring]] earned itself a date. An API
  `POST /api/v1/applications/{uuid}/restart` — taken deliberately, to rebuild a provider chain
  before the code fix existed — left the app `running:healthy` in Coolify while Traefik answered a
  bare `404 page not found` (content-type `text/plain`, so unmistakably the proxy) on every host and
  path: both public domains, `/v1`, `/admin`. Two things are worth keeping. The push deploy that
  followed did not restore the route and only a `deploy?force=true` did, so "deploy again" is not
  interchangeable with the forced one. And the page that says exactly this was already in the wiki:
  it was not read before the restart, which is the actual mistake — `scripts/smoke_test.py` takes
  under 5 s and would have caught the 404 immediately instead of 35 minutes later.
- **Update**: `_enrich_body` moved out of `main()`'s closure to module level, with
  `free_quota_available` injected alongside the `enrich_batch` and `_build_extractor` parameters it
  already had, and a `main.py:_sleep` seam mirroring `extract.py`'s. The loop that spends the whole
  free-tier budget had **no test at all**, because nothing in the suite can reach a closure — and the
  `enrich_chain_rebuilt` fix above is a `nonlocal` rebind in the middle of 130 lines, which wants a
  test rather than a careful reading. Six tests in `tests/test_enrich_loop.py`, each ending the
  unbounded loop by cancelling from the fake batch, because cancellation is how the real one ends.
  Checked by mutation, not by passing: deleting the rebuild turns exactly the three tests that assert
  it red. The seam is what keeps them honest *and* fast — real pauses are 75 s and 900 s, so one test
  of the rate-limit branch would otherwise cost more than the whole 12-second suite. Deliberately no
  virtual clock: `extract.py` needs one because `pace_wait` subtracts `monotonic()`, and this loop has
  no such arithmetic, so recording the requested waits is the honest substitute.
- **Creation**: [[local-gpu-machine-setup]] — the `localgpu` machine was reinstalled and the provider
  had been answering 100% errors for days, so the rebuild is now a runbook. Two things must be
  *retaken* rather than re-created, and both were learned by doing the opposite first. The named
  tunnel still exists in the Cloudflare account with its DNS record, so `cloudflared tunnel token
  --cred-file` fetches its credentials; a second tunnel cannot have `gpu.meetapedia.com`. And
  `LOCAL_GPU_KEY` in Coolify is the server's copy that survived the wipe — minting a fresh key on the
  machine produced a *misleading* failure, because `/v1/models` and `/v1/quota` still answer 200 (they
  only read the catalogue) while every completion returns Cloudflare's bare `error code: 502`. The app
  log is unambiguous where the HTTP status is not: `api_request_failed … "Invalid API Key" … status=401`
  then `gateway_upstream_unavailable … model=localgpu`. Taking the machine to the server's value needs
  no deploy. Rebuilt throughput matches 2026-09-06: a full 8,000-char page in **57.6 s** (4,129 prompt
  tokens at 287 tok/s, 1,094 generated at 25.4 tok/s), inside Cloudflare's 100 s ceiling.
- **Observed**: `extractor_preflight_ok` is the fastest health check for this provider — it printed
  `retired=1` with `localgpu` absent from `live=[…]` while every other entry read `(no budget)`. On
  2026-09-17 every free provider's daily allowance was spent by 17:30 UTC, so the local machine was the
  only one with budget: exactly the hole [[our-own-gpu-in-the-fleet]] was added to fill, observed in
  production for the first time.
- **Fix**: every pause in the enrichment loop now **rebuilds** the provider chain
  (`main.py:_pause`, logging `enrich_chain_rebuilt`), so a provider that recovers is picked up within
  one pause. Deliberately no preflight on the rebuild: model names cannot change without a deploy, so
  the probe that is right once per run would be one wasted call per pause. First shipped untested and
  the loop was then extracted so it could be tested — see the entry below.
- **Observed**: a provider that recovered mid-window did **not** rejoin the enrichment run.
  `main.py:_enrich_body` builds its chain once per run and `schedule.worker_enabled: true` makes that
  run unbounded, so `localgpu` — retired by the circuit breaker while it was 401ing — stayed retired
  and every batch logged `all providers rate limited` for 20 minutes after the machine was verifiably
  serving the `ai_only` pipeline, which had rebuilt its own chain at its next preflight. A container
  restart cleared it. The asymmetry is worth fixing in code: the pipeline rebuilds per run, enrichment
  does not, and the unbounded worker makes "per run" mean "until someone restarts it".
- **Observed**: `Python-urllib` gets a Cloudflare **403** at `gpu.meetapedia.com` while
  `python-httpx` — what the server actually sends — gets 200. A hand-written probe can therefore
  report a healthy provider as broken; set a `User-Agent` before believing one.

## 2026-09-16
- **Fix**: the test suite runs in **13.2 s instead of 138.4 s**, and the flakiest test in it is
  deterministic. Measured before touched: three tests took **128.6 of the 138.4 seconds** while the
  other 570 shared the remaining ten. Two were sleeping out rpm pacing a real second at a time, and
  the longer of those — `test_one_broken_provider_does_not_retire_a_healthy_one` — had been failing
  roughly one run in five since 2026-09-05, which is what a result that depends on wall-clock timing
  looks like.
  `scraper/extract.py` now has one named `_sleep` seam for the three places the module waits, and
  the tests substitute a **virtual clock**. The distinction is the whole lesson and cost a first
  attempt: a no-op sleep leaves `QuotaLedger.pace_wait`'s `monotonic() - last_call` frozen, so every
  provider is paced out forever and the chain raises "all providers rate limited" — two green tests
  turned red for a reason unrelated to what they test. A stub that advances a fake clock by exactly
  the requested amount runs every decision, against a clock that moves when the code asks it to.
  The third test slept 30 s to outlast a 3 s endpoint ceiling; the ceiling is now
  `_HEALTH_COUNT_TIMEOUT_S` and the stub sleeps one second past it — 30 s → 4 s.
  **Verified not neutered**, which is the check Google's review guide asks for: reintroducing the
  global failure counter the per-provider breaker replaced still fails the accelerated test. And
  0 failures in 25 consecutive runs, against roughly 1 in 5 before.
  Sources: [Fowler, *Eradicating Non-Determinism in
  Tests*](https://martinfowler.com/articles/nonDeterminism.html) — "always wrap the system clock";
  [Google, *What to look for in a code
  review*](https://google.github.io/eng-practices/review/reviewer/looking-for.html) — tests must
  "actually fail when the code is broken".
- **Observed**: the 2026-09-12 counter fix is confirmed in production. The 2026-09-15 report reads
  "2296 hívás (582 kinyerés 1500 leírás 214 egyéb)" — 582+1500+214 = 2296 exactly, with no
  discrepancy warning, against 464 unaccounted attempts four days earlier. Fleet refusals are down
  to **14%** from 87% on 2026-09-09.
- **Open**: `localgpu` is on its fifth consecutive day at 100% failure — 142 calls, 142 errors.

## 2026-09-12
- **Fix**: the unaccounted attempts had a cause, and it was a floor. `_count_attempts` recorded
  `max(1, n)`, so an enrichment call that raised **before reaching any provider** — fleet paced
  out, breaker open, quota spent, all of which `write_descriptions` raises on without calling
  anyone — still booked one attempt against a ledger that had seen nothing. Those cluster exactly
  on the days a refusing fleet produces them in bulk, so the phantom count tracked the refusal
  rate: **252** unaccounted on 2026-09-10, **464** on 2026-09-11. Zero is now a real answer, and
  whether the extractor counts attempts at all is asked once from the object (`hasattr`) rather
  than inferred from a delta of zero — on a real extractor that delta is information, on a stub it
  is nothing. Surfaced by the 2026-09-11 report change that stopped clamping the difference; the
  number had been there all along.
- **Fix**: `gemini_flash` removed. The *entry* was correct — it took exactly the 19-20 calls a day
  its real limits allow — but the model was not worth having: **39 calls over two days produced 2
  answers** (19/19 errors on 09-10, 18/20 on 09-11) for one quality point over Flash-Lite, a gap
  [[free-tier-model-router]] already calls unreliable. Its own block said deleting it was the whole
  change if twenty calls ever cost more than they returned.
- **Observed**: the 2026-09-10 split is vindicated for Flash-Lite, which is what mattered. Gemini
  went from **1,102 errors in 1,200 calls** (09-09, one shared entry) to **3 in 479** (09-11). Fleet
  refusals 87% → 50% → **31%**, pages processed 39 → 7 → **226**, extraction attempts 87 → 11 →
  **447**. The 09-10 dip was the priority scope catching up, not starvation — worth remembering
  before reading a single quiet day as a regression.
- **Open**: `localgpu` has now answered **142 calls with 142 errors**, a third consecutive day at
  100% failure.

## 2026-09-11
- **Observed**: the 2026-09-10 fixes worked at the provider level. Refused calls **87% → 50%**;
  OpenRouter went from 712 refusals in 800 calls to **24 in 950**; and the expiring ceiling did its
  job — Groq ended 2026-09-09 pinned at 187 and spent 2026-09-10 working against its configured
  950 with no pin at all. The Gemini split behaved exactly as measured: `gemini_flash` took 19
  calls against its real allowance of 20 and learned a ceiling of 17.
- **Fix**: the daily report's call-split line no longer hides a discrepancy. On 2026-09-10 it read
  "2400 hívás (11 kinyerés 2641 leírás)" — its own parts exceeding its own total by **252** —
  because `max(0, _calls - _enrich - _extract)` clamped the difference to zero. The workload
  counters and the quota ledger are supposed to count the same provider attempts; the ledger is
  what governs routing, so an excess means calls nobody charged a budget for, and undercounting a
  budget is how a router walks into a hard block. The line now names the gap. **Cause not yet
  found** — `calls_made` explicitly *excludes* preflight, which would make it smaller than the
  ledger, not larger, so the usual suspect is ruled out. This is the sixth time this one line has
  been wrong (see 2026-08-27); each previous version was wrong by computing something, this one by
  concealing something.
- **Open**: `gemini` (Flash-Lite) still took **454 refusals in 510 calls** with correct per-model
  limits configured (RPD 500, RPM 15). Hypothesis worth testing before touching config again:
  Google's daily quota may not reset at 00:00 **UTC**, in which case our UTC day straddles two
  Google days and half the allowance is already spent when ours begins.
- **Open**: `localgpu` answered **136 calls with 136 errors**, a second full day at 100% failure.
- **Observed**: extraction fell to 11 attempts and 7 pages, with 0 new communities, 0 pages
  fetched and every `search_only` run returning 0 records all day. Most likely the priority scope
  is simply caught up and the budget flowed to descriptions as [[free-tier-model-router]] intends —
  but "no work found" and "no work left" are different states and the report cannot tell them apart.

## 2026-09-10
- **Decision**: a learned daily ceiling is a **hypothesis with a TTL**, not a verdict for the day.
  `observed_limit` is inferred from a refusal, the inference is sometimes wrong in the expensive
  direction — a per-minute *token* 429 is indistinguishable from a spent day — and it was only ever
  lowered, so one bad reading owned the provider until midnight. On 2026-09-09 that ended Groq's
  day at **187 calls against a real allowance of 1,000**.
  Two changes, in opposite directions and deliberately asymmetric. **Expiry**: past
  `_OBSERVED_LIMIT_TTL_S` (30 min) `budget()` stops believing the pin and plans against the
  configured number again; if the provider really is finished it refuses once more and the pin
  returns, so being wrong now costs *one call per cooldown* instead of a day's allowance. Every
  refusal re-stamps `observed_at`, including one that does not change the value — otherwise a
  correctly learned ceiling would expire while the provider was still saying no. **Unlearning**: a
  call that *succeeds* past the ceiling clears it outright (`clear_observed_limit`), because that
  is the provider demonstrating the inference was wrong rather than an optimistic number talking a
  proven one back up. The MIN that guards lowering is untouched, and so is the rule from
  2026-08-18 that `near_daily` compares against the *configured* rpd — that ratchet walked Groq's
  13,680 down to 336 and is exactly what must not come back.
  `budget()` stays a pure function of row and clock. An earlier draft handed out "probe slots" from
  `remaining()`, which feeds the admin page and the daily report as well as routing — loading
  `/admin/providers` would have consumed one.
- **Fix**: Gemini is **two catalogue entries** now, because its limits are per model and one entry
  can only hold one set. The compromise `rpm: 10, rpd: 1500` produced **1,090 HTTP 429s across
  1,200 calls** on 2026-09-09 — about a third of the whole fleet's daily refusals from a single
  provider. The real numbers came from this account's own AI Studio rate-limit dashboard, because
  Google stopped publishing them: **3.6 Flash is RPM 5 / RPD 20**, **3.5 Flash-Lite is RPM 15 /
  RPD 500**. True daily allowance 520, not 1,500.
  The mechanism is worth keeping: 3.6 Flash scored one point higher (64 vs 63), so the router
  chose it first, spent its twenty calls, and took a 429 on every attempt after. And the wrong
  `rpd` **disabled the ledger's own correction** — `QuotaLedger` lowers `observed_limit` only when
  a 429 arrives near the configured allowance, so a refusal at call 21 against a configured 1,500
  reads as a per-minute limit and teaches it nothing. A 75x-too-high number does not merely
  over-plan; it guarantees the ledger never learns better. Flash-Lite also gains throughput: its
  real RPM is 15 and the old shared 10 left a third of it unused.
  `fetch_upstream_models` now matches `spec.name.startswith("gemini")` — an exact match would have
  sent the new second entry to the OpenAI-compat `/models` Google does not implement, which is the
  bug the 2026-09-05 review caught for Cloudflare, one provider later.
- **Observed**: `localgpu` answered **152 calls with 152 errors** on 2026-09-09 — a provider at
  100% failure, the shape [[2026-08-cerebras-free-tier-ended]] and the paid-fallback post-mortem
  both cost us before. Not yet diagnosed; its own config comment names the two candidates
  (Cloudflare's 100 s origin timeout answering 524, and the `enable_thinking:false` flag whose
  absence makes every answer unparseable).
- **Observed**: no new free-tier provider worth adding. **Vercel AI Gateway** has a renewing
  monthly credit and an OpenAI-compatible endpoint but deliberately publishes no numbers — "this
  page describes behavior rather than fixed numbers… contact Vercel" — and the ledger cannot plan
  against a support ticket. **Cohere** is 1,000 calls a month and non-commercial use only.
  **BazaarLink** is 50/day. SambaNova, NVIDIA NIM and SiliconFlow were declined on 2026-09-05.
- **Correction**: the enrichment/extraction split (2,409 calls against 87 on 2026-09-09) is **not**
  a defect and was re-opened here in error on 2026-09-05. `main.py:_enrich_body` carries the
  reasoning: yielding to extraction was tried on 2026-08-21 and reverted the same day, because
  42,091 community pages with 68% missing long descriptions and 34 visitors a day make the
  marginal extracted page worth less than a thin page made rankable. The traffic since supports
  it — meetapedia.com went from 8 visitors on 2026-09-05 to 65 on 2026-09-09. Read the comment
  before proposing the change again.

## 2026-09-06
- **Decision**: `localgpu` runs **Qwen3-4B Q4_K_M, measured 73** — the *smallest* of the three candidates and the best of them. One shared sample (`d13dfe914a92`, 16 hu pages, 17 expected), so the numbers are comparable: 4B **73** (2.5 GB), 8B **67** (5.0 GB), gpt-oss-20b **65** (12.1 GB), and the 4B answered 16/16. This is the header of `providers.yaml` proving itself — LLMStructBench found prompting strategy outweighs model size for JSON extraction — so read it as our prompt doing the work, not as 4B beating 20B in general. The practical lesson is to measure the small model **first**, not last: two days of the fleet's history assume bigger is better. Memory settled the rest. On a 16 GB machine its owner is actually using, the 8B ran at 5.4 tok/s against 15.8-16.6 idle, because everything else was paging and unified memory means that steals the bandwidth the GPU needs; at 5.4 tok/s one extraction is ~109 s. Open risk recorded, not solved: the scored run averaged **89 s/page** against Cloudflare's 100 s origin timeout, and it ran over loopback where no timeout applied. Through the tunnel a slow page will 524 — benign (retried, never quarantined) but a cap on how much this provider can contribute.
- **Observed**: a thinking model that reasons in **plain text** is a quarantine hazard, not just a bad answer. Qwen3-4B ignores `--reasoning-budget 0` (which works on gpt-oss) and emits `"Okay, let's tackle this..."` as `content` — no `<think>` tags, no `reasoning` field, so neither llama.cpp nor `_json_items` can separate it. All 16 golden pages came back as `ExtractorContentError`, and that is **exactly** what `_Quarantine` counts: in production three of those retire a real page permanently under the current fingerprint. A misconfigured local model would therefore delete pages from the corpus while looking merely unlucky. The fix is `--chat-template-kwargs '{"enable_thinking":false}'`, which reaches the model's own template instead of llama.cpp's generic budget. Worth remembering when adding any model: verify the *shape* of one answer before trusting a score, because a score of `n/a` and a score of 0 look alike in a summary and mean opposite things.
- **Fix**: the concurrency limit is now shared **per provider**, not per extractor object — the first version limited nothing. `build_extractor()` builds a chain for the pipeline and `_enrich_run` builds its own, deliberately concurrent with it, so one provider has several live extractors in a single process; each politely held itself to one call while **three ran at once**. Measured after the deploy: 3 slots busy, 1.6-4.0 tok/s, single calls stretched to **76 s** against Cloudflare's 100 s ceiling. The semaphore now lives in a registry keyed by provider name, because the thing being rationed is one GPU rather than one Python object. Consolation for the residual risk: a 524 is an `ExtractorUnavailableError`, not an `ExtractorContentError`, so it never counts toward the quarantine — the page is simply retried next run.
- **Fix**: `max_concurrency` per provider, set to **1** for `localgpu`. The first production traffic to our own GPU came back **HTTP 524** — Cloudflare's origin timeout, 100 s on every non-Enterprise plan. The cause was not the tunnel: `pipeline.extract_concurrency: 4` is tuned for hosted APIs, where the wait is network latency and overlapping is free, and it is exactly wrong for a model on a GPU we own. Four concurrent pages there do not return four answers in the time of one; they return four answers each about four times slower — **27 tok/s alone against 1.85 tok/s with four slots busy** — so nothing is gained and every single call's latency quadruples past the ceiling. The queue has to be on *our* side: waiting on the semaphore holds no HTTP connection open, so the proxy's clock does not start until the model is free. `--parallel 1` on llama-server would have done the opposite — the request would sit in the origin's own queue with the connection open and time out exactly as before. Also recorded: 600 s in `timeout_seconds` is not the binding limit and never was; 100 s is, whatever the config says.
- **Fix**: the local GPU moved from a Cloudflare *quick* tunnel to a **named** one (`gpu.meetapedia.com`). The quick tunnel needs no account, which is why it was used first, but its hostname is random and regenerated on every reconnect — and the first night's ran 8 hours, dropped, then failed 22 times with `control stream encountered a failure while serving`, unable to retake its own name. The failure mode is what matters: `LOCAL_GPU_URL` still points somewhere, `configured` is still true because the variable is still set, and every call fails at connect time until a person notices. A named tunnel keeps its hostname across restarts, reboots and network changes, so the env var is set once — one browser login, and `base_url_env` stops being a maintenance burden. On the brand domain on purpose: the name should say which project owns the provider, Cloudflare proxies it so the machine's IP never shows, and it 401s everything without the key.
- **Decision**: `localgpu` joins the fleet — a model on hardware we own, with no daily allowance to spend. Measured on the hu golden set (sample `d13dfe914a92`, 16 pages, 17 expected, verified identical to the server's by fingerprint): **Qwen3-8B Q4_K_M scores 67** at 27 s/page, **gpt-oss-20b 65** at 45 s/page. Qwen wins on everything but the score — 1.7x faster and 5.0 GB of weights against 12.1, which is the difference between a laptop that still works and one that does not. The number that decided it was not the score: on 2026-09-05 **every free provider's daily allowance was spent by 22:30 UTC**, so a provider at 67 that is always there beats one at 74 that has been gone since lunchtime. Routing is unchanged — quality order still sends the better free models first. See [[our-own-gpu-in-the-fleet]].
- **Fix**: the first local measurement said **145 s/page** and looked like a verdict on the hardware. It was the packaging. Ollama ships gpt-oss as MXFP4 and that path does not reach Metal on Apple Silicon — `ollama ps` reported `19%/81% CPU/GPU`. Same machine, same weights, llama.cpp GGUF: prefill **124 -> 290 tok/s**, generation **14.5 -> 27-29 tok/s**. Two more non-obvious requirements: `iogpu.wired_limit_mb=13500` (the default GPU budget on 16 GB is ~11.8 GiB, gpt-oss-20b is 12.1 GB, and Metal answers `kIOGPUCommandBufferCallbackErrorOutOfMemory` at load), and `-c 8192` — because a runtime short of context **trims the prompt rather than failing**, and the page text is the tail of the message. A trimmed prompt still scores; it scores the truncation.
- **Creation**: `base_url_env` on `ProviderSpec`. For an endpoint we host ourselves the URL is deployment state, not code: a Cloudflare quick tunnel gets a new hostname on every reconnect and `config/` is not a persisted volume, so a URL in the YAML would make each reconnection a code deploy. Identity stays in git, address moves to the environment — the same split `api_key_env` already makes for the credential. `configured` now also requires an address, so a provider whose tunnel is down is *absent* rather than built-and-failing: without that, every call in the run fails at connect time until the circuit breaker retires it, and a run-long outage gets reported as a bad provider instead of a missing one.
- **Creation**: `timeout_seconds` per provider. The global 60 is sized for a hosted API answering in seconds; a full 8,000-char page on our own GPU measured **66.5 s** end to end through the tunnel. A timeout is scored as a *failure*, so the wrong number here would not report a slow provider — it would retire a working one through the circuit breaker. Raising the global instead would let a genuinely hung hosted call hold a slot for as long as the slowest local model is allowed.
- **Fix**: `_json_items` now unwraps a markdown code fence before giving up. Not a formatting nicety: it raises `ExtractorContentError`, `_Quarantine` counts exactly that error, and three of them retire the page **permanently** under the current fingerprint — so a model that habitually fences would delete pages from the corpus over a wrapper whose contents are correct JSON. Measured on Qwen3-8B at roughly one answer in sixteen. Strict parse still runs first and genuinely broken output still raises; the fence is a second chance and the only one.
- **Observed**: `tests/test_router.py::test_one_broken_provider_does_not_retire_a_healthy_one` fails when the file runs as a whole and passes in isolation (58.8 s alone, ~74 s in the file). Confirmed **pre-existing** by stashing all local changes and re-running against `6843fe3`. The healthy provider becomes unavailable around call 16 of 30. Not diagnosed; recorded because the root `CLAUDE.md` states the suite passes with nothing to ignore, and on this machine that is currently false.
- **Observed**: llama.cpp **ignores `response_format: {"type":"json_object"}`** — a 0.6B model answered with prose and a fence — while `response_format: {"type":"json_schema", ...}` is enforced at the sampler. So `json_mode: true` in the catalogue is a no-op against a local llama.cpp endpoint, and the prompt is doing all the work. Passing the project's own `EXTRACTION_SCHEMA` via `--json-schema-file` failed with `Failed to initialize samplers: std::exception`, cause unknown. Worth returning to: it would make Qwen's one failure mode impossible to emit.

## 2026-09-05
- **Fix**: `scripts/score_providers.py` now prints the golden set's `sample` fingerprint and locale, not just the page count. `score_fleet` computes that field precisely so two measurements can be told apart, and the CLI was withholding the one value that answers "are these two numbers about the same pages?" — which is what every comparison against an already-written `quality:` is actually asking. Page counts do not settle it: two samples of the same size drawn from different prefixes are different samples, and that is the failure [[measuring-extraction-quality]] exists to prevent.
- **Creation**: `scripts/make_golden_db.py` — the golden set can now leave production. Three constraints made the obvious routes fail: `data/scraper.db` is **8.7 GB**, Coolify's terminal is *inside* the container so `docker cp` is not there (`sh: docker: not found`), and that terminal inserts newlines into pasted input — a heredoc dies on the first wrap and leaves the shell on a `>` prompt, and a 1.5 KB single-line base64 blob dies the same way. Anything over ~60 characters is unreliable, so the logic went into the image, where invoking it is one short command. The export copies exactly what `scraper/scoring.py` reads and nothing else: the first 400 `cache_pages` rows **by `url_hash`** (the prefix reproduces the identical deterministic sample the full DB would yield, which is what keeps an off-server score comparable with the `quality:` values) plus `communities` names for the generic-token derivation. Round-tripped against a synthetic DB before shipping: `golden_set()` returned 12 pages and `corpus_names()` 50 names. See [[exporting-a-golden-set]]. Written to serve a local-model experiment — can hardware we own match the free fleet? — but the export itself is independent of how that turns out.
- **Fix**: review round on the day's four commits (`codex review` plus the repo's own code-review skill) found six things the change itself should have carried. The two that were outright wrong: `CLAUDE.md`/`AGENTS.md` still said "no credit on the OpenRouter account" in the very paragraph an agent reads before deciding whether flipping `allow_paid` is safe — the premise had inverted and the doc kept the old reason; and the fleet enumeration omitted Cloudflare while still calling DeepSeek merely "parked". Also fixed: the quota runbook told an operator to set `allow_paid: true`, which is now a **silent** no-op because `enabled: false` makes `configured` fail without an error, so it now names all three switches; [[free-tier-model-router]] and the index still described six providers; the Cloudflare model listing took one `per_page=200` page without reading `result_info`, which would turn a configured model into `UNLISTED` and exit 1 the day the account crosses the page size; and a `_LIST_PATH` dict in `check_free_models.py` that nothing has ever read was dutifully extended for Cloudflare, changing nothing — deleted, because a second place to register a provider is how the shared-function bug happened. Left as a note rather than a change: Groq serves the same weights at `quality: 67` against Cloudflare's 74, and since one sample moves this score by more than seven points, that ordering is a capacity decision and is now labelled as one.
- **Fix**: Cloudflare's model discovery lived in the *script's* copy of the logic, not the shared one — so `check_free_models.py --remote`, which is the normal way to run the check because the provider keys only exist on the server, asked `/v1/models/upstream`, which calls `providers.fetch_upstream_models()`, which still hit the OpenAI-compat `/models` and reported **HTTP 405** for a provider that was working. The local path — the one nobody uses — was the only one that worked. `live_models()` in the script had been a near-duplicate of `fetch_upstream_models()` (same branches, different timeout and error wording); it is now a one-line delegation, so the Gemini and Cloudflare special cases exist once. Found by `codex review`, not by us: the concern raised by hand before the review was that `removesuffix('/v1')` might build a bad URL if the suffix were absent, which is wrong — `removesuffix` is a no-op then. The real defect was the duplication, and it was invisible from the diff of the file that was edited.
- **Decision**: **no path may spend money on a model.** `router.allow_paid` goes to `false`, and `deepseek` and `openrouter_paid` are both `enabled: false`, on top of the `daily_budget_usd: 0.00` that was already there. `paid_allowed()` needs the permission *and* the amount, so any one of these alone would block a paid call — all four are set because the OpenRouter account now holds $10.80 of real money (bought to unlock the free tier, not to spend) and `openrouter_paid` shares its key, making it the one paid provider that could actually succeed. A paid call now needs three separate reversals instead of one line. See [[paid-spend-guard]] and [[2026-08-paid-fallback-burned-the-budget]].
- **Creation**: Cloudflare Workers AI added to the fleet — and the estimate that justified looking at it was wrong by 3-5x. The published "10,000 Neurons/day" plus the docs' per-million-token rate for a *different* model gave ~250-300 calls a day. Measured on the real workload — our own `SYSTEM_PROMPT` and a page at the `max_text_chars: 8000` cap — one extraction costs **91.9 neurons** on `@cf/openai/gpt-oss-20b` (108/day) and **176.9** on gpt-oss-120b (56/day). The catalogue lists only the 20b: it halves the neuron spend, our own scores already put the 20b above the 120b on Groq, and one model makes the daily cost predictable. `rpd: 100`, which the ledger's 5% headroom takes to ~8,700 neurons, 87% of the allowance. Two things make this provider unusual: the response reports `usage.neurons`, so its cost is *booked* rather than estimated — the only provider we can say that about — and the account is on the **Workers Free** plan, where exceeding the allowance refuses requests instead of billing. An upgrade to Workers Paid would change that and must re-open this entry. Quality measured the same day: **74**, third in the free fleet behind Mistral's two. Four scoring runs, and the spread is the finding — 2 pages gave **20**, one 10-page sample gave **89** and **84** on a re-run, and an independent 20-page sample gave **74**. The 84/89 pair is the *same* sample twice, so it measures the model's non-determinism (±5), not sample sensitivity; the 20 and the 74 are different samples and are what show the score moves with the pages. Taking the largest independent sample rather than the flattering mid-size one keeps Cloudflare below `mistral-small` (80), so the fleet still opens on Mistral. Concretely the caution [[free-tier-model-router]] takes from arXiv:2606.13221 about point-estimate ranking: 89 and 74 would have been different decisions. **Cost of learning that: 86 of the day's 95 calls.** The fourth run should not have been started the same evening — the allowance refills at 00:00 UTC and the question was not urgent. `check_free_models.py` learned Cloudflare's native `/ai/models/search` because the OpenAI-compat `/models` answers 405, the same special case Gemini already needed.
- **Observed**: OpenRouter's `GET /api/v1/key` reports `is_free_tier: false` after the purchase, which is the documented condition for the 1000/day `:free` cap. It does not state the number — its `rate_limit` field is marked deprecated and returns `requests: -1` — and no `x-ratelimit-*` headers come back on a completion, so the cap itself is only observable by exceeding 50 free calls in a day. Verified by direct call rather than through our gateway, since the router's own OpenRouter budget was spent.
- **Decision**: $11.00 bought on OpenRouter, and the `:free` daily cap goes **50 → 1000**. OpenRouter gates that cap on credit *purchased all time*, not on the balance — under $10 it is 50/day, at or above it 1000/day, and the higher tier survives the balance being spent back down (openrouter.ai/docs/api-reference/limits). The money is therefore not for extraction and `router.daily_budget_usd` **stays 0.00**: `openrouter_paid` shares `OPENROUTER_API_KEY`, so the ceiling is the only thing between that balance and a paid call, and a *negative* balance makes OpenRouter answer 402 on the free models too — spending it would cost us the capacity it was bought for. The 2026-08-27 rationale for the zero ("no credit on the account") was true then and is now false; the setting is unchanged but the reason is the opposite one. `rpm` stays 20: that applies to every `:free` model regardless of purchase history, so a full 1000 needs ~50 minutes of OpenRouter time. The ledger's learned ceiling does not block the new number — `observed_limit` lives in the per-UTC-day row, so each day starts from config again. Scale: +~960 calls against a fleet that made 1,784 on 2026-09-04, of which 1,454 were description enrichment and 71 extraction — the new capacity follows that same split unless `enrich_batch_limit` changes.
- **Incident**: the `/v1` gateway accepted a guessable key. `ROUTER_API_KEY` held two comma-separated values and the first was `ROUTER_API_KEY` written three times — a string derivable from this public repository's own docs. It reached `/v1/chat/completions` (spends the free-tier budget) and `/v1/control/*` (starts and stops runs, reachable because `CONTROL_API_KEY` is unset and `_control_keys()` falls back). Found while fetching a key to run the free-model check, not by a monitor. Removed from the Coolify variable; **the removal only took effect at the next deploy** — the running container answered 200 to the deleted key for as long as it kept the old environment, so the UI showing it gone was not evidence it was revoked. See [[router-gateway-api]]. Setting `CONTROL_API_KEY` to separate the two authorities is still open.
- **Fix**: Groq's `rpd` is **1000**, not 14400. 14,400/day is Groq's organisation-wide default; its per-model table (console.groq.com/docs/rate-limits, read 2026-09-05) gives every model we run — `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.6-27b` — "RPM 30, RPD 1K, TPM 8K, TPD 200K". The discrepancy was flagged by a search on 2026-08-18 (see the 2026-08-18 entry) and left unconfirmed for eighteen days; this closes it against the vendor's own table. It is **not** the binding limit — Groq's day ends on tokens (189,300 of 200,000 spent in 140 calls on 2026-09-04) — so the correction buys honesty in the ledger, not capacity. The stale "most generous daily request budget of the set" comment above the entry went with it: that is Gemini.
- **Fix**: `openai/gpt-oss-20b:free` removed — this time the hint held. Absent from OpenRouter's public catalogue (431 models, 19 `:free`), and the real call agreed: HTTP 404 "This model is unavailable for free. The paid version is available now - use this slug instead: openai/gpt-oss-20b". Made directly against OpenRouter with the provider key, because our own ledger had spent the day's OpenRouter budget and would have answered `quota_exhausted` — the trap that left this question open on three previous days. Not re-added as the paid slug: quality 45 was the weakest of the three and `daily_budget_usd` is 0.00. The other two were re-confirmed live the same day (`gemma-4-31b-it:free` after one transient upstream 429 from Google AI Studio, which is the model's provider refusing, not our daily cap). `open-mistral-nemo` stays: still unlisted, still not disproven.
- **Observed**: no viable free-tier provider outside the fleet. **SambaNova** — 20 RPM / **20 RPD** / 200K TPD on its own rate-limit page, i.e. half of OpenRouter's already-tightest allowance; **NVIDIA NIM** — ~1,000 signup inference credits, a trial rather than a standing tier, which makes the daily ledger meaningless; **SiliconFlow** — real-name identity verification required for free models since 2026-05-15. All three were the named candidates in the 2026-08-18 entry; all three are now checked and declined.
- **Fix**: Sitemap topic listings now require configured topics, preserving legacy-topic community details; Meetapedia about/explore entries use their actual canonical paths. Legacy detail navigation/JSON-LD no longer points at missing topic listings, untranslated topics receive a public label, and report identity is preserved. Added both-domain HTTP/canonical regression tests. Community related listings now match the detail column width. See [[search-console-2026-09-05]].
- **Follow-up**: [[search-console-2026-09-05]] now includes ten drilldown exports (nine distinct reports). Robots examples are utility URLs, 709 of 720 HU duplicates are submission-form variants, and 541 of 1,000 HU crawled-not-indexed examples have crawl dates on/after August 23. Live verification found 11 redirecting URLs still in Meetapedia's sitemap, including ten unconfigured `community-general` topic routes; recorded code mechanism and remaining evidence gaps without changing SEO behavior.
- **Analysis**: Added [[search-console-2026-09-05]] from four user-provided GSC exports and sampled live GET checks: HU 2 clicks / 14 impressions over the latest 28 days; meetapedia 53 / 1,247. Distinguished HU's 23,812 crawled-not-indexed URLs from meetapedia's 36,166 discovered-not-indexed backlog; corrected the collapse date to May 31 and separated historical causal hypotheses from measured facts. Flagged the August 21 thin-page policy reversal in [[indexing-strategy]] and glossary. No production changes.

## 2026-08-27
- **Post-mortem**: [[2026-08-paid-fallback-burned-the-budget]] — `allow_paid` went on alone on 2026-08-24. The provider it was switched on *for*, `openrouter_paid`, had no credit on the account: 41 calls a day, all refused, one per run because `preflight()`'s skip condition asked about requests and tokens but never about money. Every page fell through to DeepSeek's own API — four times the price, quality 23, 4 answers in 14 — for about $60 over four days. And the bill was not extraction: **10,319 attempts produced 335 pages**, because a page that fails deterministically is re-attempted by every run forever (a failed extraction is never cached, correctly) and each attempt walks the whole fleet. The same `112 pairs … 21 pages` in run after run, thirty times a day.
- **Creation**: [[paid-spend-guard]] — `router.daily_budget_usd`, a hard daily ceiling across every paid provider, with `0` (no paid calls at all) as the default. `allow_paid` is a permission and the budget is the amount; neither works alone now, so the exact historical failure — a boolean switched on by itself — is a no-op. `provider_usage.cost_usd` accumulates from the provider's own reported usage and a per-model price; refused calls count, because a truncated answer is charged in full and truncation was most of what the experiment bought. `build_extractors` refuses to build an unpriced paid model: it would report $0.00 against the ceiling. Follows the circuit-breaker rules for state — logged on change, revealed at `/admin/providers` and in the daily report, self-resetting at 00:00 UTC.
- **Creation**: [[extraction-quarantine]] — three content failures at one fingerprint and the page stops being attempted. `ExtractorContentError` (truncated or malformed answer) is the only thing that counts; outages, 429s and spent quotas say nothing about the page, and `_call` raises it only when every provider that answered agreed and nothing transient happened in the same call. The fingerprint is half the key, so a prompt or model change releases everything automatically. `/admin/quarantine` lists and releases by hand.
- **Decision**: `router.daily_budget_usd` ships at **0.00** — paid providers permitted and nothing to spend. No credit on the OpenRouter account, $0.69 left on DeepSeek's, and no top-up for now; zero is the setting that says that, rather than one that would drain the DeepSeek remainder through the quality-23 model. Verified against the real catalogue that the free fleet is unaffected: twelve extractors built, both paid ones out of the routing order, `has_capacity()` true, best available quality 80 (Mistral). Restoring paid extraction is putting credit on the account and setting an amount here; $2.00 is the figure to return to.
- **Fix**: a content failure ends the call instead of sitting out a pacing window. `_call`'s second round exists to give a *paced* provider its turn and will wait up to fifteen minutes to do it — right when the alternative is losing the page, wrong when every provider that answered has said the answer does not fit. Caught by the test suite hanging: the first version of the content/transient split let a truncated page sleep the full window. Three separate runs each offer the page a differently-paced fleet before the quarantine takes it.
- **Fix**: `pages_worked()` subtracts quarantined pages as well as failed ones. It exists because a page that fails caches nothing, so `urls_found - cache_hits_extract` kept reading as outstanding work and one permanently failing page drove ~100 runs on 2026-08-18; a quarantined page caches nothing either, and leaving it in the count would have rebuilt that empty loop one guard later. The read-only views agree with the pipeline for the same reason — `/admin/coverage` and `/v1/backlog` pass the threshold now, via `config.extract_quarantine_threshold()` (cached: `load_config` parses thousands of cities and coverage polls every three seconds).
- **Fix**: `max_output_tokens` is per model, then per provider, then global. It could not be raised for a reasoning endpoint before without handing the same number to Groq, whose free tier reserves `prompt + max_tokens` against an 8,000-token minute window before generating — named as the prerequisite in `735d177` and now done. The paid DeepSeek snapshots get 4,000; Groq keeps 1,500.
- **Fix**: a truncated answer says so. `_warn_if_truncated` logged and let the parser report "invalid JSON", which reads as a bad model rather than a cap we set; the error now names `max_output_tokens=N`, which is what makes the quarantine list actionable. A cap hit *after* the closing brace still yields the answer — the parser decides, this only relabels the failure.
- **Fix**: `preflight()` no longer probes a provider that is out for the day on *money*, not just on requests and tokens (`ModelRouter.done_for_today`). That one condition was the whole of `openrouter_paid`'s 41 calls a day.
- **Observed**: a paid provider at 100% failure is now named in the daily report and at `/admin/providers`. The same shape was missed on 2026-08-22 ([[2026-08-cerebras-free-tier-ended]], 283 calls / 283 errors) and again for four days here — twice is a reporting bug, not bad luck.

## 2026-08-24
- **Decision**: paid providers on, and `deepseek/deepseek-v4-flash` (0423, via OpenRouter) is the one they route to — measured 80 on 13 of 14 Hungarian pages. The same score as DeepSeek's own API serving the 0731 snapshot at four times the price, which answered **4 of 14**; and `qwen/qwen3.7-flash`, the cheapest list price on the market, answered **0 of 14** and was removed the same day. Every failure is `llm_output_truncated`: all three are reasoning models, the `reasoning` text is billed as output, and it spends the 1,500-token cap before the JSON closes. A truncated call is charged in full. The score itself does not include the answer rate — two models scored 80 with a tenfold difference in usefulness — see [[measuring-extraction-quality]].
- **Fix**: the daily report's capacity line divided *every* successful call by the pages extracted, but enrichment spends the same free budget — on 2026-08-23 that read "8.9 calls/page, ~239 pages/day" while 384 of the 936 calls had written descriptions. The real figures are 5.3 and ~332. The number had already been used to size a paid-model decision. Modified records cannot stand in for enrichment (extraction modifies records too, and the proxy exceeded the calls made on an existing test), so `daily_counters` persists a real count. Review caught the first version counting the wrong thing in three ways at once: it wrote the batch total after the loop, so a cancelled batch — routine, the admin stop route exists for it — lost every call while the ledger stayed charged; it counted *accepted records*, leaving refusals and unparseable answers blamed on extraction; and it stamped the batch's end date, moving an evening's spend across midnight. Counting one **attempt** at the call site fixes all three, and attempts are the unit the whole line now works in — a daily allowance is denominated in attempts, and a refused call spends one. Two wrong versions shipped through review first: successes divided by pages (5.3), which overstates capacity by the refusal rate, 47% that day; and before that all calls divided by pages (8.9). The honest figure for 2026-08-23 is **13.4 attempts per page and ~130 pages/day**. The counter sits in a `finally` so a cancelled call still counts — `CancelledError` is a BaseException and never reaches `except Exception` — and it counts the *provider* attempts a call consumed (`extractor.calls_made` delta), because one description can walk several providers down the fallback chain and the ledger counts each of them. And extraction's own attempts are **measured** (`extract_attempts`, persisted per run from the extractor's `calls_made`) rather than inferred as "everything that is not enrichment" — that inference charges extraction for `preflight()`, which probes every provider once per run against a worker that starts one every twenty minutes, and for the `/v1/chat/completions` gateway, which is other people's software. **And then the derived number was removed.** Five versions of it were wrong in one morning — modified records vs calls, batch totals vs per-call, successes vs attempts, logical calls vs provider attempts, inferred vs measured — and the last review closed the question rather than the bug: the budget is not one scalar. Groq's binding limit is 200,000 tokens a day, not its 14,400 requests, so summing request allowances across the fleet and dividing by attempts-per-page is not a computation with an answer. Doing it honestly needs per-workload attempt tagging *and* a mixed-unit budget model — a project, not a report line. The report now states what was spent (extraction / descriptions / other, each measured at its source, with preflight and the `/v1` gateway named rather than blamed on extraction) and what came out. Pages per day is an observation across reports again.
- **Finding**: the funnel block's first morning showed outclicks at 0 over thirty days against 1,403 lifetime. `log_outclick` has no callers — the tracking went out in `e7d373e` with the listing shuffle, and rightly so: it routed every outbound link through a `/out?url=` redirect on our own domain. Not restored; a `sendBeacon` version that leaves the link alone is the one worth building. See [[acquisition-funnel]].
- **Observed**: the worker's empty loop is gone. Runs went from every 3-4 minutes to every ~20 from 19:11 UTC, when the fix deployed. Cerebras is absent from the quota table. Gemini took 704 rate-limit refusals across 1,062 calls, most of the day at the old `rpm: 15`.

## 2026-08-23
- **Refactor**: the worker's post-run bookkeeping moved out of `_worker_loop`'s closure into `pipeline.worker_after_run`, a pure function, after a review round showed the point: reverting the collector to consult `worked` left every test in the new file green, because they tested `pages_fetched` and never whether anything called it. Three more false-positive tests came out of the same round — a substring check that survived deleting an ON CONFLICT clause, an import check that survived deleting the task launch, and an index-plan check that survived deleting the index (EXPLAIN QUERY PLAN cannot tell a covering partial index from a thin one). Each is now asserted on behaviour and verified by breaking the code.
- **Fix**: the continuous worker's collector branch measured itself with `pages_worked`, which is an extraction measure — a `search_only` run extracts nothing, so it degraded to "URLs the search returned" and a pass that downloaded nothing still cleared the extraction cooldown. Around 200 runs on 2026-08-22 with 0 pages downloaded and 0 pairs searched. `pages_fetched` is the collector's own signal, and three empty passes now put the worker to sleep instead of polling every minute. Third wrong answer in a week to "was that pass worth anything?", each one a mode-specific signal read as general — see [[continuous-worker]].
- **Fix**: `/v1/backlog` answered 524 after 125 s. Two guesses were wrong before the measurement settled it: the JSON functions were not the cost and neither was the column list — SQLite was scanning a table whose rows are ~30 KB each. A covering index over `(url_hash, extract_fingerprint, records_count)` took the filter from **11.03 s to 0.31 s** on a 6.15 GB synthetic copy; the new `records_count` column exists so the index can cover the query at all, replacing two JSON traversals that cannot be indexed — with a `-1` sentinel for "scraped, never extracted", because a first version that used NULL for both that and "not backfilled yet" left every page in the backlog NULL forever and had the fallback open its blob on every scan. The backfill is chunked so it cannot hold SQLite's single writer slot for a multi-minute rewrite, and the filter falls back to the blob for rows it has not reached — calling those "unextracted" would have re-extracted the whole corpus, a year of work at the fleet's current rate. Measure the artefact at production scale: with small blobs the same query benchmarked at 0.71 s and looked fine. See [[done-pair-url-hash-not-city-topic]].
- **Creation**: [[2026-08-cerebras-free-tier-ended]] — the daily report showed cerebras at 283 calls and 283 errors while the fleet's output fell from 387 pages to 78. A real call answered `billing limit (HTTP 402)`: Cerebras ended its free API tier on 2026-08-17. A 402 only retired the provider for one run, and the worker rebuilds an extractor every few minutes, so the highest-quality model in the catalogue absorbed every run's first pick. The ledger now blocks a 402 provider until the next UTC midnight and persists it; cerebras is disabled; Gemini's `rpm` goes 15 → 10 (Google publishes 10 for Flash, 15 for Flash-Lite, and one number covers both).
- **Fix**: four defects found by review in the previous change. Claims were stored with `change_type='claim'` but the approval route sent them to `apply_community_edit`, which answers "unsupported" — Approve errored and Reject was the only way to clear the highest-intent row on the page. The funnel's date windows compared Python's `2026-07-24T00:00:01+00:00` against SQLite's `2026-07-24 15:34:25` as text, and "T" sorts above " ", so rows fifteen hours outside the window counted as inside. The venue city index dropped an active topic filter. And two `_total` columns are standing rows rather than lifetime events, which the docstring now says instead of implying otherwise.
- **Decision**: `stealth/ox-alpha` (OpenRouter, free, 1M context) evaluated and **not** added — an unnamed provider's time-boxed preview that retains prompts, riding an OpenRouter budget already spent every day. Fails the free-models skill's "standing free tier, not a trial" test and adds no capacity.

## 2026-08-21
- **Creation**: [[2026-08-boilerplate-outweighed-the-content]] — the customer-acquisition round began by fetching one live community page and found that 76% of it was a hidden city dropdown, byte-identical across all 42,091 community pages: 5,089 words of text of which 510 were the community. `/helyszinek` was worse — 15.5 MB and 34.4 seconds, all 7,676 venues rendered on the event loop, so one request to it stalled the whole site for half a minute. The pages Google declined are thin *and* padded with shared boilerplate, and the outages chased all week as a deploy problem had a blocking render behind at least some of them. CLAUDE.md already forbade server-rendering large lists — in admin templates only.
- **Creation**: [[acquisition-funnel]] — a customer-acquisition round found the funnel fully instrumented and entirely unreadable: pageviews, outclicks, subscriptions and submissions all in the database, all behind the admin password. `/v1/funnel` and a **Vevőszerzés** block in the daily report expose them. Two findings came out of the reading. Claims — an organiser typing their own address in to ask for their listing, the strongest signal the site produces — were emailed and stored nowhere, so an unset `RESEND_API_KEY` or one Resend failure lost them behind a green tick; they are now persisted before the mail is attempted. And the classic directory move, mass "claim your listing" outreach to the ~42K scraped addresses, is not available here: 2008. évi XLVIII. tv. §6 requires prior express consent for advertising email to a natural person with no legitimate-interest exception. The opt-in `subscriptions` list is the channel that is legally open — and nothing has ever been sent to it.
- **Fix**: the rendered sitemap is cached for an hour keyed by site alone, which is right in production and wrong in a test run where every test swaps the database under it. The first test to fetch /sitemap.xml pinned its corpus for the whole session, producing a test that passed alone and failed in the suite — the shape that gets a real assertion deleted as flaky.
- **Decision**: `noindex` on communities without a description is removed, and they are back in the sitemap. The rule was right when such a page was a name and nothing else; with up to two dozen neighbour links it is content and a crawl path, and 68% of the corpus excluding itself while 23,461 pages sat in "Crawled – currently not indexed" was not a road to being indexed.
- **Creation**: [[2026-06-search-index-collapse]] — Search Console, read for the first time this session, shows indexed pages down from ~25,000 to **2,430** since the first week of June, with traffic flat at almost zero ever since. The trigger is in the git log (`shuffle community listings`, 05-29, reverted 06-05 by a session that had already spotted the drop); what kept it down is not. 23,461 pages sit in "Crawled – currently not indexed" — Google fetched them and declined — which is what 68% of communities having no long description looks like from outside. And the site 404s for minutes several times a day whenever the pipeline stalls the event loop, which is the strongest de-indexing signal there is. Availability is now an SEO priority, enrichment is the ranking lever, and more pages are worth close to nothing while 23,461 existing ones are rejected.
- **Fix**: enrichment follows `pipeline.country_priority` instead of `ORDER BY id` over the whole corpus. The international records were imported first, so an unscoped enrichment spent its whole budget on the secondary market — 488 international records updated on 2026-08-20 against three Hungarian ones. A same-day experiment making enrichment *yield* to extraction was reverted once the traffic numbers were beside it: the two-thirds share was right, the destination was wrong.
- **Fix**: the application log is a rotating, gzipped file on the persisted volume instead of a 500-line ring in memory. Under the continuous worker the ring held a few minutes, and every attempt this week to answer "what happened last night?" reached a buffer that had already forgotten.
- **Fix**: `/v1/score` scanned the golden set and the whole communities table on the event loop, and a fleet measurement outlived the CDN's 100-second request timeout — so running one took the site down and lost its own result. Both scans moved off the loop; the run is backgrounded and its scores logged as `fleet_scored`.
- **Refactor**: the twin-cron schedule the continuous worker replaced is deleted, three days after the worker took over. Six source-inspecting tests are gone with it: `pages_worked()` and `next_worker_action()` came out of the worker's closures so the decisions can be tested as decisions rather than as substrings of `main.py`.

## 2026-08-19
- **Fix**: every LLM request now carries `max_tokens` (`deepseek.max_output_tokens`, default 1500). We sent none, and free tiers charge `prompt + max_tokens` against a per-minute token window **before generating** — Groq's is 8,000 — so each call reserved the model's maximum: about one request per minute. That is why Groq stopped at 354 calls on 2026-08-18, why 838 of Gemini's 1,205 came back 429, and why an 8,000-character prompt returned a plain HTTP 413 rather than a truncated answer (which had been diagnosed the day before as "this model's context is too small" — wrong). A truncated answer is now logged as `llm_output_truncated` instead of surfacing as invalid JSON, which reads like a bad model rather than a budget we set. Recorded as an invariant in `CLAUDE.md`: raising the cap "for safety" makes runs slower, not safer; shorten `max_text_chars` first. Learned from a sibling project.
- **Fix**: the worker's work signal counted a *failing* page as work, so one permanently failing page drove ~100 `ai_only` runs in four and a half hours, each writing a run record. "Worked" now means pages newly extracted and cached. The signal has been wrong twice in two days, so three consecutive record-free passes now stand aside for the collector regardless of what it says.
- **Update**: the daily report states processing capacity — successful calls per processed page, the pages/day the budget buys, and how many pages were fetched against how many were processed. On 2026-08-18 that ratio was **6.9x**: 2,434 pages fetched, 353 processed. Both numbers were already in the email and the over-collection was invisible.

## 2026-08-18
- **Fix**: the collector's search prefetch batched by *city*, so `search_concurrency: 8` never engaged — a core city has six topics and most are already cached, leaving batches of one or two. Measured **0.16 pairs/min**, below the serial collector it replaced. A rolling prefetch that reads ahead along the run's whole pair list took it to **2.7 pairs/min** (bursts of 5-7 as each batch lands). Two defects found on the way: prefetched results were never released on the cache-hit path — one result list per pair for the life of the run, the same OOM shape this pipeline learned once before — and `search_done` had vanished from the logs entirely because the line stayed at a call site the prefetch bypasses. Cost follows throughput: ~2.7x the daily DataForSEO spend for ~5x the pairs, the per-pair price having halved with `standard_priority: 1`.
- **Fix**: enrichment spun for hours — 37 batches, zero records, one refused call every 75s. A spent daily allowance answers 429 exactly like a per-minute limit, so "wait 75s and retry" was right for one and meaningless for the other. It now waits for the quota reset when there is no budget left to wait for.
- **Fix**: `config/` is **not** a persisted volume, and five places said it was — `CLAUDE.md`, the free-models skill, two wiki pages and the catalogue's own header all told the reader a model-name or settings change needed no deploy. Disproved on 2026-08-18: a `settings.yaml` change made in git reached production (1/1/2 → 4/8/1), which a mounted volume would have shadowed, while the same change made through `/admin/config` vanished at the next deploy. Corrected everywhere, and the settings editor is read-only.
- **Update**: the free-models skill now also searches for free tiers **outside** the fleet — `check_free_models.py` can only ask providers we already hold a key for, so it cannot notice a new one. Candidates recurring in 2026-08 listings and absent from our catalogue: SambaNova, NVIDIA NIM, SiliconFlow. The skill also carries a warning that `rpd` was written from vendor docs and is not self-correcting: a search reported Groq's free tier at 1,000 requests/day against our configured 14,400, which would have the router plan for fourteen times the real allowance.
- **Creation**: [[continuous-worker]] — the twin time windows are gone. Both existed for reasons that had expired: DeepSeek's off-peak discount (extraction runs on a free fleet now) and a belief that DataForSEO was cheaper at some hours — checked against their pricing and false, it is priced by queue. What is left is one condition, not a schedule: free quota left → extract, none left → collect, with the collector watching for the 00:00 UTC reset through a new `should_stop` predicate on `run_pipeline`. A restart is now indistinguishable from a continuation, which is what stops a deploy from costing a collection run (2026-08-17: three collector runs died as `run cancelled` with 0 pairs, and the day yielded 206 pairs instead of ~800). Operating it moved to `/v1/control/{status,run,stop,resume}` with its own key, deliberately outside the OpenAI-compatible surface, and `launch_pipeline_run()` is now the single place a run is started. Ten defects across three review rounds, two of them introduced by the previous round's fix — the worst would have re-bought all 45,570 pairs of search by inheriting the admin form's Full Refresh defaults.
- **Fix**: HTTP clients are pooled per event loop. Eight-way collection exhausted the container's file descriptors within minutes — every request opened its own client and the standard-mode search opened one per poll, up to 150 per search — and the symptom was SQLite and `providers.yaml` failing to open. Concurrency revealed it rather than causing it.
- **Fix**: `settings.yaml` is version-controlled only; the admin editor is read-only and says why. Settings entered through the UI were silently reverted by the next deploy, which is how a day of concurrency tuning was lost. Values now set in the repo: `extract_concurrency: 4`, `search_concurrency: 8`, `standard_priority: 1` — halving the search bill, viable once the waits overlap and the poll window follows the priority.
- **Fix**: HTTP 413 retires the model instead of opening the circuit breaker. The 00:30 extraction run was reported as a fleet outage because one model's context is smaller than our 8,000-character prompt.
- **Creation**: `GET /v1/backlog` — how much work is queued, and the settings the running process actually has. Both questions had been answered by inference from a log buffer that holds minutes.

## 2026-08-17
- **Creation**: [[concurrent-extraction]] — a pair's pages are now extracted several at a time (`pipeline.extract_concurrency`, default 1 = the old serial chain). The measurement that forced it: 3.3 extractions/min against a fleet ceiling of 185 calls/min, and 15,690 free calls expiring unused. Two behaviour-preserving commits landed first, per Fowler's preparatory refactoring: provenance now comes out *with* the result (`extract_traced`) instead of being read off `last_model` after the await, and the quota ledger claims a provider's slot at call *start* (`reserve_call`) instead of recording it on return — without which every concurrent page picks the same provider. `_run_full` stays serial: it is not on the twin schedule. Three review rounds found nine concurrency defects, each now an invariant on the page — the sharpest being that a completed extraction could be discarded unwritten (work the fleet was already charged for), that a stop with every page attempted left nothing absent to notice it by, and that a provider retired by the breaker stayed retired even after a request already in flight came back successfully.
- **Creation**: `sources/2026-08-17-dataforseo-business-listings.md` — DataForSEO's Business Listings Search (their own Google Maps entity database, queried by coordinate + category, no SERP/fetch/LLM) investigated as a cheaper collection route and **rejected on measured data**. One live request 5 km around Szentendre returned 3 indexable organisations out of 30 — the rest a bus stop, a sculpture, a hair salon, an auto repair shop — putting the cost per *usable* record at ~$0.0076 against ~$0.001 today. Price also is not flat: two points fit base ~$0.0096 + ~$0.00044/result. Filtering by category was not pursued because the shape of the miss is structural: Hungarian `egyesület`-type groups are largely not on Maps, and leaning on a commercial-skewed source would shift the index away from community groups while looking like growth. The probe script was removed once it had answered.
- **Creation**: [[2026-08-rate-limits-opened-the-breaker]] — the first full night on the free fleet aborted 45 minutes into a 9.5 h window with `no extraction provider configured (20 consecutive failures)` while `/v1/quota` still showed 13,523 Groq calls. The breaker counted rpm cooldowns as failures, so five healthy providers all being inside their per-minute limit at once read as a dead fleet; 368 `enrich_call_failed` lines are the same event, one per record, each after a wasted source fetch. Rate limits no longer touch the failure counter (`_all_temporarily_blocked()` / `rate_limited_out`), the wait ceiling went 300 s → 900 s, a rate-limited pause ends the pass without aborting the run, and enrichment stops on the first failure once the extractor reports nothing left. The structural cause — a serial chain across five providers with independent limits — is unchanged and still the next real fix.
- **Fix**: seven codex review rounds on the above (24 findings). Two rounds found bugs in the previous round's fix, and both were the same mistake — relaxing a safety rule without asking what relied on it. The load-bearing ones: exempting rate limits from the breaker also exempted a *dead* fleet, because the quota ledger paces every attempt including failed ones, so five providers returning 500s looked identical to five in cooldown; `rate_limited_out` was latched, which would have stopped extraction for a whole window after one unlucky moment and permanently masked a fleet that died afterwards; and `pair_log["extract_error"]` was indexed on a path that never sets it, turning the shipped clean-pause fix into a `KeyError` in `_run_full`. The breaker is now **per provider** — one endpoint stuck on 500s used to retire the whole fleet — with failures counted once per `_call` (the retry round halved the threshold) and the provider that answered excluded from its own call's tally. A 429 back-off no longer sleeps past the window end (`extractor.deadline`), a quota/rate-limit stop leaves the city loop instead of letting the next city pay DataForSEO for unextractable pages, and `extractor_throughput` finally measures what the window achieved: the overnight run managed 3.3 extractions/min against a combined fleet ceiling of 185 calls/min, and nothing recorded whether the gap was latency or pacing.
- **Fix**: `insert_duplicate_candidate` — the post-run duplicate scan died on `UNIQUE constraint failed: duplicate_candidates.entity_type, winner_key, loser_key`. `idx_dup_pair` is partial (`WHERE resolution IS NULL`), which permits two *pending* rows for one pair in opposite orientations; reorienting one onto the other then violates the index. The redundant row is now deleted rather than the write failing, and the insert carries a matching `ON CONFLICT … WHERE resolution IS NULL DO NOTHING` so the non-atomic select-then-insert cannot lose the race either.
- **Creation**: [[measuring-extraction-quality]] — the scoring matcher, rewritten three times in one session and now documented. Exact-key matching understates every model (MINEA: 59.4% vs 88.4%), but loosening it let the bare topic word "Sakk" score 100 on a page of chess clubs, and `--apply` would have written that into the routing order. Tightening then broke the largest market instead: `SV Musterstadt` ≠ `Sportverein Musterstadt` scored a perfect extraction at 20. The common error was inferring genericness from a token's *shape* — impossible across languages where Hungarian generics are short (`klub`) and German ones long compounds (`Schachverein`). Now: a club-type suffix rule plus document frequency over the whole `communities` table (which is the only thing that can know `sakk` identifies nothing). Pairing is maximum-matching rather than greedy, so the score no longer depends on the order a model lists clubs; `expected` is deduplicated; unmeasured models score `null`, never 0.
- **Creation**: [[2026-08-healthz-db-query-outage]] + [[production-monitoring]] — `/healthz` ran `SELECT COUNT(*)`, so a pipeline write lock failed the Docker healthcheck and Traefik pulled the container from rotation: four apparent outages with a healthy process, none caught by monitoring because all three existing signals answer "is the process alive". `/healthz` is now a liveness probe (cached count, off the event loop, `db:"busy"` instead of failing). Contributing loads removed the same evening: the sitemap's N+1 (one `get_communities` per city×topic pair, >30s at 3.8K cities, now one query + worker thread + 1h cache) and `init_db()` running per request on a dozen routes (CREATE TABLE = write lock; now once per path). `scripts/smoke_test.py` checks the public hostnames over the CDN and runs from GitHub Actions on every push and every 15 minutes — deliberately off-server, so the checker does not share fate with what it checks.
- **Update**: [[cost-saver-schedule]] — twin windows reordered. The extractor now runs 00:30-10:00 UTC, immediately after free-tier quotas reset at 00:00; the DataForSEO collector takes 10:30-23:50. The old order existed for DeepSeek's off-peak discount, which no longer applies to a free fleet — extraction was starting 16 h after the budget refilled, and anything unreached by 00:20 was lost. The 9.5 h window is sized from ~16.5K daily calls at a serial ~2 s/call.

## 2026-08-16
- **Creation**: [[free-tier-model-router]] + [[ai-provider-quota-runbook]] — extraction now routes over a fleet of six free-tier providers (`config/providers.yaml`, `scraper/providers.py`, `scraper/router.py`) chosen **before** generation by quality under a persisted per-day `provider_usage` ledger that learns real limits from 429s; paid DeepSeek is parked behind `router.allow_paid: false`. Every model shares one `fingerprint_model` so routing cannot invalidate the extraction cache; `cache_pages.extract_model`/`extract_quality` record which model actually ran, outside every key. `_preflight_fleet()` probes each model once and retires broken ones. `_run_quality_upgrade()` re-extracts weak pages only after new work is exhausted. `scripts/score_providers.py` replaces benchmark priors with measured golden-set scores. Admin view at `/admin/providers`. Grounded in `sources/2026-08-16-llm-routing-arxiv-research.md`.
- **Creation**: [[router-gateway-api]] — the router is exposed as a public OpenAI-compatible gateway at `/v1/*` (`scraper/web/api.py`) so other software can call it with any existing OpenAI client by changing only `base_url`/`api_key`. Deliberately general purpose: no project prompt or schema is injected. Bearer auth from comma-separated `ROUTER_API_KEY` (unset = gateway off, not open); `model` selects a routing policy (`auto` / `provider` / `provider:model`); streaming rejected with 400 rather than silently answered; responses carry an additive `x_router` provenance object. `GET /v1/models` lists only models with quota left, `GET /v1/quota` exposes the shared daily ledger. Tests: `test_gateway_api.py`.
- **Creation**: [[importing-city-lists]] — `scripts/import_cities.py` imports a country's settlements from Wikidata additively (never rewrites existing entries), resolving accent-fold slug collisions with a "(Region)" suffix. Applied: every Hungarian settlement ≥1000 inhabitants (973 new, 968 `topic_tier: core`, Leányfalu included) and 83 Indonesian kota, with `id` locale search terms in `topics.yaml` and `id → 2360` in `LOCALE_TO_DATAFORSEO_LOCATION`. `_saver_city_groups` became an ordered list driven by `pipeline.country_priority` (Hungary → Germany → Indonesia → Sweden → rest); Hungary moved to the front because the import made it the largest unprocessed backlog on the primary market. Updated [[adding-city-topic]].
- **Fix**: three codex review rounds on the above (36 findings, all addressed). The load-bearing ones: the quality-upgrade sweep was unreachable dead code behind `run_pipeline`'s early return; `extract_quality IS NULL` counted as 0, which would have had the sweep overwrite ~74K DeepSeek extractions with weaker free-model output; rpm was parsed but never enforced while a per-minute 429 collapsed a provider's *daily* ceiling (Gemini 1500/day → 15); spent free quota was reported as a provider outage in the run banner and daily email; and — introduced by the round-1 fixes — rpm pacing acted as a veto rather than a wait, so the breaker opened after 20 "failures" and aborted the run within seconds, while `ExtractorQuotaError` (not a subclass of `ExtractorUnavailableError`) sailed past every caller's `except`. Round 3 separated "callable now" (`order()`) from "callable today" (`with_budget()`/`has_capacity()`), which had made the sweep skip and the gateway answer a spurious 429. Regression tests cover each. Also: the sweep now runs only for the last country group (`allow_upgrade`), filters cities in SQL before `LIMIT`, batches `save_results` per pair, and documents that it can only add records, never remove them.
- **Creation**: [[public-listing-widgets]] + [[2026-08-mobile-city-search-datalist]] — the home city search was unusable on iOS (Safari ignores `<datalist>`) while an exact-match submit guard silently blocked every near miss. Replaced with `/static/js/listing.js`: accent-folding `MpText.norm`, a touch-sized autocomplete panel, and an auto-wiring A-Z + free-text filter (`templates/_listing_filter.html`) now used by the cities, venues, people and explore pages, all of which sort alphabetically server-side.

## 2026-07-31
- **Creation**: [[run-outcome-three-states]] — `runs.outcome` ('ok'/'warning'/'aborted') replaces the success boolean as the reported state; `pipeline.classify_run_outcome()` is the single classifier, aborts are explicitly marked in the pair log, `success` now means "completed", and startup recovery (`_startup_plan`) no longer re-runs a warning run. Updated [[daily-report]], [[pipeline-orchestration]], [[run-modes-and-startup]], [[sqlite-schema]].
- **Creation**: [[2026-07-llm-bare-array-run-abort]] — the 2026-07-30 `ai_only` window died on `'list' object has no attribute 'get'` with 0 pairs logged; all three LLM parsers now go through `_json_items()` (non-object top level → `ExtractorUnavailableError`, page retried not cached) and `FallbackExtractor._call` catches bare `Exception` as a transient failure so an untyped bug skips one page instead of aborting a run. The review round found the same hole open in `FallbackSearchClient.search`/`search_all` (DataForSEO parsers assume the documented shape) and closed it identically. Updated [[extraction-layer]], [[extractor-circuit-breaker]], [[search-layer]].

## 2026-07-27
- **Fix**: [[cost-saver-schedule]] — the managed enrichment off-peak cutoff is now enforced *inside* each batch, not only between rounds. A round of up to `enrich_batch_limit` (200) sequential LLM calls started at 00:29 could previously bleed hundreds of paid calls into peak pricing before the between-batch `_within_window` check ran again. `enrich_batch` gained a `deadline` param (UTC); `_enrich_run` passes `_next_window_end(now, enrich_until)` and the loop checks it before every paid `write_descriptions` call, breaking with `stopped_at_deadline` the instant the discount window closes. Manual `/api/enrich` passes no deadline (unbounded, admin-driven). Tests: `test_deadline_stops_before_any_paid_call`, `test_future_deadline_does_not_block`. Caught by a codex review round on the managed-schedule commit.
- **Update**: [[cost-saver-schedule]] / [[run-modes-and-startup]] — description enrichment promoted to a **managed schedule** (`schedule.enrich_enabled`, `_enrich_run` in `main.py`): an APScheduler cron at `enrich_cron` (16:30 UTC) plus a startup-resume hook, so it survives container restarts (no manual re-launch). Runs only in the off-peak window (`enrich_until` 00:30), bounded rounds (`enrich_batch_limit`), idempotent/resumable via the `long_description` marker, self-gated to the configured `enrich_cron`→`enrich_until` window (via `_within_window`), and coexists with `ai_only` (no coordinator reservation; `_merge_source_urls` protects enriched fields). `_enrich_running` is a shared mutex with the admin `/api/enrich` endpoint; both the managed and manual runs are cancellable via `/api/stop` (`_enrich_task`) and abort if the extractor circuit-breaker opens. Replaces the detached `enrich_offpeak.py` ops script.
- **Creation**: `scraper/enrich.py` + `POST /admin/api/enrich` — SEO description enrichment tool. New `CommunityRecord.short_description` (cards/meta) + `long_description` (~200-word page body), generated together by `extractor.write_descriptions` from the community's page text (cached or fetched fresh). Separate from the extractor's `description` and preserved across re-extraction by `_merge_source_urls` (durable; `long_description` present = the "already enriched" marker). Reads: community page → long, cards/OG → short. Prompt-injection bounded (system-message instructions + untrusted-data delimiting), output validated, endpoint reserves the run coordinator + hard cap. Updated [[description-enrichment-plan]]. Tests `test_enrich.py`. Refined across 3 codex rounds (cache-revert, coordinator, starvation, marker-drop, multiple-source durability, injection).
- **Creation**: [[description-enrichment-plan]] — recorded the staged plan for enriching thin community descriptions (biggest re-indexing lever) and the deliberate decision to defer the run to supervised, capped, off-peak batches rather than executing it autonomously (cost at 26K-page scale + [[2026-06-seo-traffic-collapse]] corpus-churn risk + unreviewable AI copy).

## 2026-07-26
- **Update**: [[web-app]] — community pages offer a copyable **backlink badge** ("Feature this on your website"): a collapsible section with the escaped `<a href=…>` snippet linking to the canonical community URL + a copy button. Encourages community owners to link back → inbound links / domain authority for the SEO recovery. i18n keys `community_badge_*`; test `test_community_badge.py`.
- **Update**: [[indexing-strategy]] — hreflang added (`i18n.py:_hreflang_alternates`): `rel=alternate` hu/en/x-default in `<head>` for the shared static pages with clean site-aware URLs (home, map, people). Deliberately excludes content pages (301'd/country-specific) and the not-yet-localized static aliases (about/explore/cities/submit) to avoid wrong alternates. Closes the long-standing "no hreflang anywhere" gap for the pages where it's valid. Tests: `test_hreflang.py`. Full international *content* localization (translated UI per locale) remains a separate large effort.
- **Update**: [[indexing-strategy]] — hub-page intro copy: city and city+topic explore pages now render a unique, deterministic i18n'd intro sentence (count + city + topic, no LLM/churn) to give thin hub pages crawlable descriptive text. City-only pages count via `available_topics`. Keys `explore_intro_city`/`explore_intro_topic_city`.
- **Update**: [[indexing-strategy]] — breadcrumbs added (codex SEO audit): visible `<nav aria-label="Breadcrumb">` + `BreadcrumbList` JSON-LD on community, city, topic-explore, venue, and person pages (Home → City → Topic → Community). `schema.py:breadcrumb_jsonld` + `_crumbs()` helper + `breadcrumbs` template var in `public_base.html`. Reinforces site hierarchy for SERP breadcrumbs and internal linking without touching page content. Tests: `test_breadcrumbs.py`.
- **Update**: [[indexing-strategy]] and [[persistence-layer]] — sitemap now emits `<lastmod>` per community page. `_bulk_upsert_communities` only advances `communities.updated_at` on a real content change (compares merged data to the pre-delete snapshot), so a fingerprint re-extraction that reproduces identical data does NOT churn every page's freshness date (the 2026-06 corpus-instability lesson). New `get_community_lastmods()` feeds the sitemap in one query. Tests: `test_sitemap_lastmod.py`.
- **Update**: [[i18n-and-site-detection]] — People page reworked: URL localized per site (`/people` on meetapedia, `/emberek` on kozossegek, site-aware 301s via `_render_people`), fully i18n'd (was hardcoded HU), role filter + role-badge removed (only `leader` exists), removed from the top nav, and the all-cities/all-persons dump replaced with an empty state → pick a city → list that city's people. Tests in `test_listing_filters.py`.
- **Expansion**: Germany is the next market — all **2,057 Städte** (Wikipedia list) added to `cities.yaml` (2,046 new after deduping the 11 existing incl. exonyms Hanover=Hannover, Nuremberg=Nürnberg, Frankfurt am Main=Frankfurt). Tier from the authoritative 80-city Großstadt (>100k) list → full; the ~1,976 smaller ones core. Geocoded via GeoNames with **state(admin1)-disambiguated matching** (strict name+asciiname, then alternatenames, both state-confirmed) so homonyms (Aken/Aachen, Halle Westf./Halle Saale, Munster/Münster, Furth im Wald/Fürth) never inherit a big city's coords or population; the 72 unmatched merged-municipalities get no pin rather than a wrong one. `CITY_COORDS`: DE 1985/2057; **Sweden backfilled 2→290/290** (ADM2-preferred + manual Ale/Ed/Järfälla/Tyresö/Upplands-Bro), fixing Sweden's absence from the map. Accent-fold slug collisions (Münster/Munster, Löhne/Lohne) get a state suffix on the lower-priority city so `_city_from_slug` stays unambiguous — locked by `tests/test_city_uniqueness.py`. `_saver_city_groups` now leads Germany → Sweden → world → Hungary (Sweden/Hungary fast-skipped by the done-pair pre-filter). Sweden complete (4200/4200 pairs, ~$30). Three codex review rounds caught the exonym dup, homonym mis-geocoding/mis-tiering, and the slug collision.
- **Update**: [[seo-cross-domain-canonical]] — HU pages on meetapedia now **301** to kozossegek (was canonical-only, which GSC showed Google ignored: meetapedia won 551 HU impressions to kozossegek's 33, and only 43 of kozossegek's 27K pages were indexed). New `_hu_redirect()` in `web/app.py` called from every city-scoped route; keyword-rich community `<title>` (name – topic city | site). Tests: `tests/test_hu_redirect.py`. Diagnosed via the GA4 service account granted Search Console API access.
- **Creation**: [[2026-07-deploy-truncates-collector]] — production `runs` table (via SSH+sqlite) showed the 01:00 `search_only` collector cancelled hours in on 07-24/07-25 by mid-window deploys; with `auto_run_on_startup: false` it never resumed and APScheduler won't re-fire a job that already fired, so the day's remaining page collection was silently lost. Fix: `_startup_plan()` makes startup a saver-aware crash-recovery net (resume interrupted `search_only`/`ai_only` boxed to its window; clean boots do nothing; never launch `full` under the saver split), `auto_run_on_startup: true`. Updated [[run-modes-and-startup]], root `CLAUDE.md`/`AGENTS.md`; new `tests/test_startup_recovery.py`.

## 2026-07-25
- **Update**: [[sister-site-cross-links]] — the About page's project block became one sentence continuing the intro paragraph, with author / open-source / sister-site as inline links instead of a heading plus a button row.
- **Update**: [[sister-site-cross-links]] — the cross-edition notice was scoped down the same day it shipped: the full-width strip above the content is gone, replaced by a translate icon in the top-right corner of the community record card, and it renders on community pages only.
- **Creation**: [[2026-07-deepseek-model-retired]] — DeepSeek dropped `deepseek-chat` (→ v4-pro/v4-flash) and the 2026-07-24 ai_only window 400'd on all 1368 pages; new `deepseek.fingerprint_model` pin keeps the 74K-page cache valid while the wire model moves to `deepseek-v4-flash`. [[deepseek]] updated for the model rename + peak-valley pricing (extract window unaffected).
- **Creation**: [[extractor-circuit-breaker]] — `run_pipeline()` preflights one live extraction before any pair loop and `FallbackExtractor` opens a breaker after 20 consecutive failures; `providers_down` (not `exhausted`) aborts the run, `extract_error` travels to the run-detail banner and the daily email, and venue/person failures finally count as `extract_failed`. Updated [[extraction-layer]], [[2026-07-deepseek-model-retired]] (follow-up), [[daily-report]].
- **Creation**: [[sister-site-cross-links]] — the two domains now link each other: a Wikipedia-style strip above the content (same path on the other host, suppressed where kozossegek.com would 302 home), a footer link both ways, and an About-page section naming the project, its author, and the open-source repo. Updated [[i18n-and-site-detection]], [[web-app]].
- **Update**: [[doc-drift-project-readme]] — repo renamed community-scraper → meetapedia (pyproject, admin titles, bot name, git remote); `PROJECT.md` archived behind an out-of-date banner; `README.md` rewritten as a project introduction that links into this wiki; `AGENTS.md` is now generated from `CLAUDE.md` by `scripts/sync_agents_md.py` with a test enforcing sync; the stale "always ignore test_city_page.py" instruction is gone (the whole suite passes).

## 2026-07-24
- **Creation**: [[wrong-city-detection]] — new `wrong_city.py` scan + `wrong_city_candidates` table + `/admin/wrong-city` review queue; admin nav gains a "Data quality" dropdown grouping it with [[duplicate-detection]]. [[sqlite-schema]] table added; interaction admin pages (edit requests, duplicates, not-community) now link to the live public community page.
- **Creation**: [[2026-07-wrong-city-approve-conflict]] — approving a wrong_city edit request failed on a boolean-collapsed error; `apply_community_edit` now returns statuses and merges into an existing target identity instead of failing.
- **Lint**: content-drift sweep after the admin simplification — stale active-voice claims about the removed revalidate/patch_results flows corrected in [[pipeline-orchestration]], [[shared-run-task-slot]], [[asyncio-task-cancellation]], [[search-ttl-3650-days]], [[deepseek]], [[end-to-end-pair-walkthrough]], [[fuzzy-dedup-and-record-key]]; [[web-app]] gained the client-supplied-record_key security rule.
- **Update**: [[web-app]] and [[duplicate-detection]] — follow-up hardening: edit-request approval re-resolves records from the admin-visible identity (client-supplied record_key was spoofable), entity merges union list fields, manual duplicate flags always stamp `signal='manual'` and are exempt from stale auto-cleanup.
- **Update**: remaining review items closed — real venue/person duplicate merges (`merge_entity_into`) + data loading on the admin page ([[duplicate-detection]]), venue edit requests applied on approve, failed submission scrapes re-queued to pending ([[web-app]]), and atomic cache writes via `db.update_cache_page` ([[cache-blob-read-modify-write]] FIXED); manual duplicate flags now stamp and stick their orientation.
- **Update**: verify-round hardening — synthesized leader persons carry `origin='leader_field'` so stale cleanup spares AI-extracted leaders ([[pipeline-orchestration]]); pending duplicate rows get their orientation corrected on re-scan except manual flags ([[duplicate-detection]]).
- **Update**: deferred codex-review findings fixed — [[duplicate-detection]] (winner follows richness, both-order pair idempotency, manual flag keeps the admin's choice, no loop-variable mutation), [[daily-report]] (DISTINCT counting fixes multi-topic inflation of new/changed numbers), [[non-quota-errors-drop-page]] (malformed LLM JSON raises instead of caching empty), leader-person cleanup on re-extraction, joinable gate in submission/re-extract flows.

## 2026-07-23
- **Creation**: [[admin-simplification-2026-07]] — deleted the revalidate/recategorize/description-maintenance flows and the Full Rebuild preset; new admin Inbox nav group with live pending badges; People (Emberek) link added to the public nav. Updated [[pipeline-run-modes]], [[run-modes-and-startup]], [[web-app]], [[sqlite-schema]], [[unicode-safe-identity-keys]], glossary.
- **Update**: [[2026-07-search-provider-down-noise]] and [[dataforseo]] — root cause found on the DataForSEO error dashboard: 12 unmapped city locales (Bratislava sk, Tokyo ja, …) produced location-less `task_post`s rejected with 40501, whose dead ids were polled for 5 minutes each; locales mapped, US fallback added, rejected posts now fail fast, locale coverage locked by test.
- **Creation**: [[2026-07-search-provider-down-noise]] — a dead DataForSEO provider became 4972 per-pair "failures" from 3 real errors; `_run_full` now aborts on an exhausted client, the catch-up pass is skipped, and `failure_reason` travels into the daily email as `· ok: <error>`.
- **Update**: [[search-layer]] (failure_reason + abort semantics) and [[daily-report]] (search_error surfaced in the run row).
- **Creation**: [[function-local-import-shadowing]] — the explore route's `UnboundLocalError: get_city_topic_counts` from a branch-local import shadowing the module-level one.

## 2026-07-21
- **Update**: Claude review hardening in [[cost-saver-schedule]], [[search-layer]], and [[daily-report]] — startup now preserves Sweden-first ordering, all-page fetch outages retry, transient search faults require three consecutive failures before pass-level disablement, JSON-null extraction rows remain runnable, and run reporting avoids false certainty or duplicate provider errors.
- **Update**: [[2026-07-search-only-cache-replay]], [[cost-saver-schedule]], [[pipeline-orchestration]], and [[daily-report]] — fixed the follow-up zero-work incident with pair-scoped `ai_only` cache reads, interrupted-run diagnosis, and provider failures that correctly fail the run.
- **Update**: [[dataforseo]] and [[search-layer]] — production standard tasks now use high priority (`priority: 2`, ~$1.2/1K); normal priority can exceed the sequential client's five-minute timeout and caused paid tasks to be abandoned/re-posted.

## 2026-07-14
- **Creation**: [[2026-07-search-only-cache-replay]] — made `search_only` a strict post-fetch exit, added terminal `search_cache.collected_at`, prioritized Sweden in bounded saver runs, and persisted top-level run errors into the daily report.
- **Update**: [[cost-saver-schedule]], [[pipeline-run-modes]], [[pipeline-orchestration]], [[done-pair-url-hash-not-city-topic]], [[sqlite-schema]], [[daily-report]], [[sweden-pipeline-priority]], and [[hungary-sweden-intl-three-passes]] now document the corrected collection boundary, completion semantics, priority, and error surface.

## 2026-07-10
- **Update**: Adopted the llm-wiki-seed conventions (kondfox/ai-utils) on top of Karpathy+OKF — new `CLAUDE.md` (maintenance triggers + same-commit rule), root `glossary.md` + `faq.md`, `union` merge driver for this log, and a wiki-wiring section in the repo root `CLAUDE.md`.
- **Creation**: Integrations category — [[dataforseo]], [[deepseek]], [[resend-email]] (kozossegek.com verified 2026-07-09; free plan = 1 domain), [[ga4-reporting]] (property 536914034, SA Viewer, runtime-only env vars).
- **Creation**: [[daily-report]] (GA4 primary + server-counter fallback + stock totals), [[end-to-end-pair-walkthrough]], [[coolify-disk-cleanup]] (93%→20% reference prune), [[2026-07-ga4-env-buildtime-failure]], [[country-landing-pages]] (/cities/<slug> + 301 + sitemap).
- **Update**: [[deployment-coolify]] (deploy-kills-run + concurrent-deploy verification + GA4 env vars), [[indexing-strategy]] (country pages in sitemap), [[pipeline-orchestration]] (walkthrough link). UI same-day: explore/city pages use one 2-column card grid (cards = icon+name+desc only, chips filter via data-topic); submit page fully i18n'd; meetapedia stats include HU.
- **Update**: Full wiki drift audit — corrected scheduler/provider/history/cache/UI claims, made all frontmatter valid YAML, synchronized all 60 index descriptions, repaired the link graph, and added `scripts/lint_wiki.py` plus a regression test for structure, resources, links, orphans, and log ordering.
- **Update**: [[web-app]] — aligned stale city-page tests with the intentional communities-only city view, retained topic-specific venues and deduplicated `/emberek`, and removed the unused per-city venue/person queries and template context.
- **Update**: [[false-positive-injection]], [[extraction-fingerprints]], and [[pipeline-orchestration]] — `ai_only` now receives pair-scoped negative examples and attributes shared URLs through `search_cache`; add/remove explicitly invalidates the affected community extraction cache, while global rules invalidate all and preserve downloaded text.
- **Update**: [[shared-run-task-slot]] and [[asyncio-task-cancellation]] — all long-run paths now use one `RunCoordinator`; synchronous reservation prevents overlap and task-identity release prevents stale cleanup from clearing a newer run.
- **Creation**: [[server-side-url-safety]] — centralized HTTP(S)/hostname/IP/DNS validation, manual redirect checks, Playwright request interception, and approval-time validation for public community submissions.
- **Update**: [[done-pair-url-hash-not-city-topic]], [[pipeline-run-modes]], and [[extraction-fingerprints]] — removed the visible-community shortcut, made done detection honor community/venue/person fingerprints and phase flags, and split `search_only` fetch completion from AI freshness.
- **Creation**: [[unicode-safe-identity-keys]] — centralized community/venue/person record keys in `scraper.identity`, added a reference-preserving `unicode_record_keys_v2` migration, and made non-Latin public slugs non-empty and collision-resistant.
- **Creation**: [[not-community-moderation-flow]] — pending public reports no longer hide communities through `init_db()`; authenticated approval now performs the visibility change and creates the false-positive example, while dismiss remains non-mutating.

## 2026-07-09
- **Creation**: Daily summary email — scraper/report.py (Resend) + traffic_daily/traffic_visitors tables + bot-filtered pageview middleware + get_daily_summary (HU/intl scopes: new/changed communities, venues, persons, searches, pages, runs with failure counters, totals). Cron 04:30 UTC (schedule.report_enabled), manual trigger POST /admin/api/send-daily-report. Recipient = REPORT_EMAIL or FEEDBACK_EMAIL.
- **Update**: UI review round — meetapedia's hardcoded-Hungarian <title> + all hardcoded-HU og_desc blocks moved to i18n keys (SEO fix, regression-tested); search hero uses .brand-gradient; source-fallback links labelled "Forrás" not "Csatlakozz"; tautological frequency hidden; empty city chips hidden; popularity ranking = topic diversity + count-minus-'other'; meetapedia interest grid shows intl (non-HU) counts; home "Frissen felvett" section; sticky topic chips + card meta badges on city pages; "Közeli városok" internal-linking block (haversine); search dedupes by community_id + snippets; venue city groups ordered by count; map bubbles smaller/translucent; footer admin nofollow.
- **Creation**: [[2026-07-bug-hunt]] — three-batch fix round from the repo-wide review: moderation survival on re-scrape, domain-boundary blocking, persons lookup, recategorize topic column, venue-only scope, timeline dedup, og:url↔canonical, topics.yaml typos, and the hot-path optimizations (map 17.5K→1 query, explore N+1, coverage memo, pipeline double-save, indexes). Repo is ruff-clean.
- **Creation**: [[cost-saver-schedule]] — saver twin crons (search_only collector 01:00→16:20 UTC + ai_only off-peak extractor 16:35→00:20 UTC), `stop_at` window boxing in run_pipeline, `SearchUnavailableError` for transient search failures (never cached), full-key pair logs (fixes run-detail 500), Retry-After parse guard. `dataforseo_mode: standard` + `saver_enabled: true` switched on.
- **Update**: Proper provider-failure handling — new `ExtractorUnavailableError`; `_post` raises instead of returning `{}`; `FallbackExtractor._call` shared runner (quota→exhaust, rate-limit→wait ≤5 min, transient→one retry); pipeline skips caching on failure (no more permanent empty results), `FallbackSearchClient` raises `SearchQuotaError` when nothing could be searched so pairs aren't falsely marked done; run-end failure summary. Rewrote [[non-quota-errors-drop-page]] as fixed. docker-compose purged of searxng/ollama relics.
- **Deprecation**: Provider cleanup — only DeepSeek (LLM) and DataForSEO (search) remain. Removed: GroqExtractor, SerperSearchClient, GooglePlaywrightSearchClient, DuckDuckGoSearchClient, the local search worker (script + /admin/api/search endpoints + SEARCH_WORKER_TOKEN), dead locale tables, groq settings block. FallbackSearchClient/FallbackExtractor stay as single-provider wrappers. Updated: [[search-layer]], [[extraction-layer]], [[search-provider-fallback-chain]], [[extraction-provider-fallback-chain]], [[local-search-worker]] (removal note), [[deployment-coolify]] (obsolete env vars), hack pages.
- **Creation**: [[cost-optimization-2026-07]] — empty-search caching (recurring-leak fix), query short-circuit (`stop_after`), venue extraction gated on communities, canonical venue/person fingerprints, opt-in off-peak cron, DataForSEO standard mode, topic tiering (260 small Swedish kommuner → 12 core topics; −6,240 pairs).
- **Update**: [[scheduler-disabled-no-cron]] (cron now opt-in), [[extraction-fingerprints]] + [[canonical-fingerprint-provider-shift]] (venue/person canonical fix landed).
- **Creation**: [[local-search-worker]] — new `scripts/local_search_worker.py` + `/admin/api/search/{jobs,ingest}` endpoints let a residential-IP browser do Google searches and feed `search_cache`, replacing datacenter DataForSEO calls. `GooglePlaywrightSearchClient` gained a `headless` param.
- **Update**: Migrated the wiki to the combined Karpathy + OKF v0.1 format — rewrote SCHEMA.md, added `okf_version: "0.1"` to index.md, gave every page YAML frontmatter (`type` required), and switched log.md to date-grouped newest-first.
- **Creation**: New subsystem pages from a full-codebase sweep — [[persistence-layer]], [[search-layer]], [[fetch-layer]], [[extraction-layer]], [[pipeline-orchestration]], [[duplicate-detection]], [[web-app]], [[i18n-and-site-detection]].
- **Creation**: Data-model pages — [[sqlite-schema]], [[community-record]], [[person-record]], [[venue-record]], [[extraction-fingerprints]].
- **Creation**: Concept pages — [[joinable-quality-gate]], [[false-positive-injection]], [[done-pair-url-hash-not-city-topic]], [[fuzzy-dedup-and-record-key]], [[history-created-sentinel-overcounting]].
- **Creation**: Decisions — [[hungary-sweden-intl-three-passes]], [[scheduler-disabled-no-cron]], [[doc-drift-project-readme]].
- **Creation**: Hacks — [[canonical-fingerprint-provider-shift]], [[pyyaml-no-norway-boolean]], [[searchquotaerror-reraise-ordering]], [[non-quota-errors-drop-page]], [[get-prompt-empty-override-falls-back]], [[name-json-tail-bleed]], [[cache-blob-read-modify-write]], [[shared-run-task-slot]], [[url-hash-triplicated]].
- **Creation**: SEO + post-mortem for this session's work — [[indexing-strategy]], [[seo-cross-domain-canonical]], [[2026-06-seo-traffic-collapse]].
- **Creation**: Operations runbooks — [[run-modes-and-startup]], [[deployment-coolify]], [[adding-city-topic]].

## 2026-06-04
- **Update**: Session-3 — fixed amber cells never turning blue ([[2026-06-coverage-amber-cells]]); added coverage cell live-update + `/admin/api/restamp-fingerprints`; removed Hungarian example bias from SYSTEM_PROMPT ([[llm-prompt-language-bias]]); i18n'd meetapedia community pages; SEO groundwork (canonical, noindex, robots); split `/admin/stats` into 3 sub-pages.
- **Creation**: [[2026-06-coverage-amber-cells]], [[init-db-before-prompt-overrides]], [[llm-prompt-language-bias]].

## 2026-05-30
- **Update**: Session-2 — 290 Swedish municipalities; coverage page (country dropdown, 5 cell states, jump-to-active, JS live highlight); pipeline done-pair pre-filter via `get_fully_processed_pairs`; `on_pair_start` callback; `search_ttl_days` → 3650; Resend email notifications on 4 routes.
- **Creation**: Wiki initialized — pre-populated architecture, hacks, post-mortems, decisions, and concepts from codebase knowledge.
