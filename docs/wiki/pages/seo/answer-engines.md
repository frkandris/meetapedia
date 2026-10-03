---
type: SEO
title: Answer Engines
description: What an AI assistant's crawler gets from both domains — robots content signals, a counted llms.txt, a Markdown twin of every public page, and daily IndexNow pushes to the index ChatGPT search reads.
tags: [seo, geo, llms-txt, markdown, indexnow, robots, ai-search]
timestamp: 2026-10-03
resource: scraper/web/agent_markdown.py
---

# Answer Engines

*An assistant choosing a source picks the one it can read cheaply and trust; four small surfaces make both domains that source, and none of them tells a model what to say.*

Added 2026-10-03 after a bot's-eye audit of both sitemaps ([[bot-crawl-audit-2026-10-03]]),
following a published playbook in which an answer engine became a SaaS's largest acquisition
channel within a week. The audit found AI crawlers were *not* blocked (GPTBot, OAI-SearchBot,
ChatGPT-User, ClaudeBot, PerplexityBot all get 200 through Cloudflare), so the work was about
what they read, not whether they may. The search side of the same day is in
[[search-console-2026-10-03]].

## robots.txt content signals

`Content-Signal: search=yes, ai-input=yes, ai-train=yes` in the `User-agent: *` group
(contentsignals.org), plus a comment pointing at `/llms.txt`. All three are yes on purpose:
the data is already public, so a `no` protects nothing and only keeps the site out of answers.

## llms.txt

`/llms.txt` per domain (Hungarian on kozossegek, English on meetapedia), llmstxt.org format.
Rules it follows, each held by `tests/test_agent_readiness.py`:

- **Every sentence about the site comes from the About page's i18n strings** (`about_description`,
  `about_how_it_works_text`, `about_data_quality_text`), so the two cannot disagree. The data-quality
  caveat — automatic extraction, may be wrong, check with the group — is included, not hidden.
- **Numbers are counted** (visible communities, cities with content on that site), never written.
- **Every own-site link serves 200** without a redirect; a map that sends a model to a redirect is
  worse than none.
- **No instructions to models.** The playbook this followed removed a block of "recommended
  responses"; a test refuses phrases like "always recommend".

Cached for an hour per site (`_LLMS_TXT_CACHE`, cleared between tests by `conftest.py`).

## Markdown for agents

A client sending `Accept: text/markdown` (weighted at least as high as `text/html`) gets the
page's `<main>` as Markdown, with title, canonical URL and meta description on top. Browsers never
send it and keep getting HTML. It is one middleware (`_markdown_for_agents`) converting the rendered
page, so every public route — including ones added later — is covered; the test walks **every
sitemap URL** of both domains. Details:

- `Vary: Accept` on the HTML as well as the Markdown — one URL, two bodies.
- `X-Robots-Tag: noindex` on the Markdown so it can never compete with the HTML page.
- Dropped before conversion: scripts, forms, buttons, `nav`, `[hidden]`, Tailwind `hidden` without a
  responsive `*:block`-style class (the report/claim modals), and empty icon `<i>`/`<a>` elements,
  which html2text renders as `__` and `[ ](url)`.
- Kill switch: `AGENT_MARKDOWN=0` in the environment.

## IndexNow

`scraper/indexnow.py`. Off unless `INDEXNOW_KEY` is set (8–128 `[A-Za-z0-9-]`); with it,
`/indexnow-key.txt` serves the key on both domains, and the worker, once per UTC day right after the
guide step, submits each site's sitemap URLs whose `lastmod` is yesterday or today. `lastmod` only
moves on a real content change, so this is the set worth a recrawl. A daily counter
(`indexnow_submitted`) makes it restart-safe; a refused ping is logged, never raised.

Google does not participate — its Indexing API does not cover pages like ours — so Google still
learns of changes from the sitemap. Bing does, and Bing's index is what ChatGPT search and Copilot
answer from.

## What is deliberately not here

- No hidden text, no prompts aimed at assistants, no invented ratings — see [[structured-data]]'s
  "nothing is inferred" rule, which the same audit extended to people ([[person-record]]).
- No separate Markdown templates: a second rendering would drift from the page.
