"""Markdown for agents: the same page, as text, when a client asks for it.

An AI assistant fetching a page runs no JavaScript and pays for every token of
markup, navigation and inline script it has to read past. A client that sends
`Accept: text/markdown` gets the page's `<main>` as Markdown — title, canonical
URL and description on top — and a browser, which never asks for it, keeps
getting HTML. One conversion of the rendered page covers every public route,
including ones added later; there is no second template to forget.

`AGENT_MARKDOWN=0` turns it off without a deploy of code.
"""
import os
import re

import html2text
from lxml import html as lxml_html

#: Elements that carry no reading content: chrome, forms, widgets.
_DROP_XPATH = (
    "//script | //style | //noscript | //template | //svg | //form | //button"
    " | //select | //input | //textarea | //dialog | //nav | //*[@hidden]"
    " | //*[@aria-hidden='true']"
)


def enabled() -> bool:
    return os.environ.get("AGENT_MARKDOWN", "1") != "0"


def wants_markdown(accept: str | None) -> bool:
    """True when `text/markdown` is accepted at least as much as `text/html`.

    Browsers send `text/html` first and never `text/markdown`; an agent that
    lists both without weights prefers whichever it lists, which by the HTTP
    rule is a tie — and a tie goes to Markdown, because the client said it
    can read it and HTML is what it is trying to avoid.
    """
    if not accept:
        return False
    weights: dict[str, float] = {}
    for part in accept.split(","):
        media, *params = (p.strip() for p in part.split(";"))
        q = 1.0
        for p in params:
            if p.startswith("q="):
                try:
                    q = float(p[2:])
                except ValueError:
                    q = 0.0
        weights[media.lower()] = q
    md = weights.get("text/markdown", 0.0)
    return md > 0 and md >= weights.get("text/html", 0.0)


def html_to_markdown(page: str, base_url: str) -> str:
    """The reading content of a rendered page, as Markdown."""
    doc = lxml_html.fromstring(page)
    title = " ".join((doc.findtext(".//title") or "").split())
    canonical = next(iter(doc.xpath("//link[@rel='canonical']/@href")), base_url)
    description = next(iter(doc.xpath("//meta[@name='description']/@content")), "")
    root = next(iter(doc.xpath("//main")), None)
    if root is None:
        root = next(iter(doc.xpath("//body")), doc)
    for el in root.xpath(_relative(_DROP_XPATH)):
        el.drop_tree()
    for el in root.xpath(".//*[@class]"):
        if _display_none(el.get("class", "")):
            el.drop_tree()
    for el in root.xpath(".//i | .//em | .//span | .//a"):
        # Icon fonts render as an empty <i>, which html2text turns into "__",
        # and an icon-only link into "[ ](url)".
        if not el.text_content().strip():
            el.drop_tree()
    converter = html2text.HTML2Text(baseurl=base_url)
    converter.body_width = 0
    converter.ignore_images = True
    converter.single_line_break = False
    body = converter.handle(lxml_html.tostring(root, encoding="unicode")).strip()
    head = [f"# {title}" if title else "", f"URL: {canonical}"]
    if description:
        head.append(f"> {' '.join(description.split())}")
    return "\n\n".join(h for h in head if h) + "\n\n" + body + "\n"


_RESPONSIVE_SHOW = re.compile(r"^[\w-]+:(?:block|flex|inline|inline-block|inline-flex|grid|table)$")


def _display_none(classes: str) -> bool:
    """Tailwind's `hidden` with no breakpoint bringing it back: modals, menus.

    `hidden md:block` is visible on a desktop and stays.
    """
    tokens = classes.split()
    return "hidden" in tokens and not any(_RESPONSIVE_SHOW.match(t) for t in tokens)


def _relative(xpath: str) -> str:
    """`//a | //b` → `.//a | .//b`, so the search stays inside the subtree."""
    return " | ".join("." + p.strip() for p in xpath.split(" | "))
