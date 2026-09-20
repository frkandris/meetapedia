# AI article brief: research and implementation notes

Date: 2026-09-20

## Finding

AI authorship is not itself a Google penalty or advantage. The risk is scaled pages with little original value. The defensible asset here is not generic prose: it is Meetapedia's city-level dataset, exposed as a transparent comparison that helps a reader choose a community.

Google's current guidance repeatedly asks whether a page contains original information or analysis, is complete enough to achieve the reader's goal, adds substantial value, uses accurate metadata, and explains who/how/why where automation is material. It explicitly warns that many AI-generated pages without added value can be scaled-content abuse. Therefore the daily number is a ceiling and every page must pass data-density gates; the system must not weaken the gates merely to reach ten.

## Recommended brief anatomy

1. Give the model a role tied to the product: careful community-directory editor.
2. State one reader job: choose a suitable group to contact or visit.
3. Put trusted rules in the system message and untrusted records in a clearly delimited fact packet.
4. Supply the actual unique evidence: city, topic, counts, coverage, comparison fields and bounded community summaries.
5. Define prohibited claims: no invented facts, first-hand experience, rankings, popularity, quality or safety claims.
6. Separate grounded reporting from generic conditional advice.
7. Request a strict JSON schema so code can validate and render the answer.
8. Set useful length bounds, not a keyword target, and reject malformed or thin output.
9. Ask the model to silently check every number and named-group claim against the packet.
10. Keep machine-verifiable facts outside the model: deterministic title, summary, counts, links, cards and structured data.
11. Disclose substantial AI assistance and explain the data-driven method on the page.
12. Cap attempts and publications; monitor usefulness and Search Console before increasing volume.

The implemented prompt is `_WRITER_SYSTEM` in `scraper/guides.py`. It follows this structure and sends no live web content or browsing capability to the writer.

## Primary sources

- Google Search Central, [Creating helpful, reliable, people-first content](https://developers.google.com/search/docs/fundamentals/creating-helpful-content)
- Google Search Central, [Guidance on using generative AI content](https://developers.google.com/search/docs/fundamentals/using-gen-ai-content)
- Google Search Central, [Spam policies: scaled content abuse](https://developers.google.com/search/docs/essentials/spam-policies#scaled-content)
- Google Search Central, [Optimizing for generative AI features](https://developers.google.com/search/docs/fundamentals/ai-optimization-guide)
- Google Search Central, [Search Essentials](https://developers.google.com/search/docs/essentials)
