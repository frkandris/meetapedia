---
type: Decision
title: Visual Identity — Town Notice Board, Not Generated Page
description: White paper, ink, one town-green accent and Atkinson Hyperlegible Next replace the terracotta-gradient look; About stops listing every city.
tags: [design, ui, templates, accessibility, i18n]
timestamp: 2026-10-23
resource: scraper/web/templates/public_base.html
---
# Visual Identity — Town Notice Board, Not Generated Page

*On 2026-10-23 the operator said the site looked AI-made and that some pages made
no sense to a person; this is the first iteration in answer.*

## What it looked like

Every public page opened on a terracotta gradient band (`.brand-gradient`,
#8A4226→#E88E6B) with white text, on warm cream, in Inter, with tracked
all-caps labels above headings and the same bordered card everywhere — the
"warm cream + terracotta" pattern that most reads as a generated page. The
About page ended in 35 topic tiles and every city of the site, A to Z (1,312
on kozossegek.com). A community page printed its name three times above the
fold (band, breadcrumb, card).

## What it is now

- **Palette:** white `#FFFFFF`, ink `#1A1D1B`, town green `#1E6B50`
  (hover `#185A43`, dark `#134A37`, tint `#E8F2EC`), neutral rules `#E1E5E2`.
  Remapped in every public template, `input.css` and the favicon; secondary
  greys went one step darker for contrast.
- **Type:** one family, **Atkinson Hyperlegible Next** (Braille Institute,
  drawn for low-vision readers; latin-ext covers ő and ű). Chosen for the
  audience — seniors' clubs are one of the largest categories. It is set in
  `tailwind.config` in `public_base.html`, because the Tailwind CDN injects its
  preflight after every stylesheet and a plain `html { font-family }` lost.
- **Page head:** `.brand-gradient` keeps its name (fifteen templates use it)
  but is paper with a rule; CSS in `public_base.html` turns the templates'
  white-on-gradient text into ink. The home H1 is the one large element.
- **No all-caps labels:** removed with their tracking; section labels are
  sentence-case ink; one-line page-head eyebrows that only repeated the page
  name were deleted.
- **About** shows what the site is, one sentence of counts, how it works,
  data quality and how to report a gap — `about_how_it_works_text` and
  `about_data_quality_text` existed in many languages and were never shown.
- **The A–Z bar** is off for a city's communities (`filter_bar(az=false)`):
  readers know how to spell a town, not a club's name.
- **Community page:** no head band; the card's name is the h1.

`scripts/render_preview.py` renders pages from a throwaway DB to static HTML,
which is how this was reviewed without a local server.

## Not done in this iteration

Person and venue pages still repeat their name in a head band; links and
buttons still end in "→"; topic tiles on the home page still carry an icon,
a label and a count in one card each.
