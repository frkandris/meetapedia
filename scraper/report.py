"""Daily summary email: traffic + changes per site (HU / international) + totals.

Sent via Resend (same env vars as the feedback emails: RESEND_API_KEY,
RESEND_FROM; recipient = REPORT_EMAIL or FEEDBACK_EMAIL). One email covers the
previous UTC day. Triggered by the report cron in main.py or manually via
POST /admin/api/send-daily-report.
"""
from __future__ import annotations

import html as html_lib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from .db import get_daily_summary, get_data_guides_for_day, get_traffic_for_day

log = structlog.get_logger()

_ROW = "<tr><td style='padding:4px 12px 4px 0;color:#6A6259'>{label}</td>" \
       "<td align='right' style='padding:4px 8px;font-weight:600'>{hu}</td>" \
       "<td align='right' style='padding:4px 8px;font-weight:600'>{intl}</td>" \
       "<td align='right' style='padding:4px 0 4px 8px;font-weight:700'>{total}</td></tr>"

_METRICS = [
    ("new_communities", "Új közösség"),
    ("changed_communities", "Módosult közösség"),
    ("change_rows", "Mezőváltozás"),
    ("new_venues", "Új helyszín"),
    ("new_persons", "Új személy"),
    ("searches", "Lekeresett város–téma páros"),
    ("pages_scraped", "Letöltött oldal"),
    ("pages_extracted", "AI-feldolgozott oldal"),
]

_STOCK_METRICS = [
    ("communities", "Közösség"),
    ("venues", "Helyszín"),
    ("persons", "Személy"),
    ("pages_cached", "Cache-elt oldal"),
    ("pages_extracted", "AI-feldolgozott oldal"),
    ("covered_pairs", "Lekeresett város–téma páros"),
]


def fetch_ga4_traffic(day: str) -> dict | None:
    """Visitors per hostname from the GA4 Data API for one day.

    Requires GA4_PROPERTY_ID and GA4_CREDENTIALS_JSON (service-account key JSON
    content) env vars; the SA email must be a Viewer on the GA4 property.
    Returns {site: {"visitors": n, "sessions": n, "pageviews": n}} with sites
    mapped from hostName (kozossegek/meetapedia), or None when not configured
    or on any error — the email then falls back to the server-side counter.
    """
    import json as _json
    property_id = os.environ.get("GA4_PROPERTY_ID", "")
    creds_json = os.environ.get("GA4_CREDENTIALS_JSON", "")
    if not property_id or not creds_json:
        return None
    try:
        import httpx
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as _GARequest

        info = _json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/analytics.readonly"])
        creds.refresh(_GARequest())

        body = {
            "dateRanges": [{"startDate": day, "endDate": day}],
            "dimensions": [{"name": "hostName"}],
            "metrics": [{"name": "activeUsers"}, {"name": "sessions"},
                        {"name": "screenPageViews"}],
        }
        resp = httpx.post(
            f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
            json=body, headers={"Authorization": f"Bearer {creds.token}"}, timeout=30.0)
        resp.raise_for_status()
        out: dict = {}
        for row in resp.json().get("rows", []):
            host = (row["dimensionValues"][0]["value"] or "").removeprefix("www.")
            site = ("kozossegek" if "kozossegek" in host
                    else "meetapedia" if "meetapedia" in host else None)
            if not site:
                continue
            vals = [int(v["value"]) for v in row["metricValues"]]
            agg = out.setdefault(site, {"visitors": 0, "sessions": 0, "pageviews": 0})
            agg["visitors"] += vals[0]
            agg["sessions"] += vals[1]
            agg["pageviews"] += vals[2]
        return out
    except Exception as exc:
        log.warning("ga4_fetch_failed", error=str(exc))
        return None


def build_report_html(day: str, summary: dict, traffic: dict,
                      ga4: dict | None = None,
                      funnel: dict | None = None,
                      guides: list[dict] | None = None) -> tuple[str, str]:
    """Returns (subject, html)."""
    hu, intl = summary["hu"], summary["intl"]
    totals = summary["totals"]

    def t_site(site: str, key: str) -> int:
        return traffic.get(site, {}).get(key, 0)

    def g_site(site: str, key: str) -> int:
        return (ga4 or {}).get(site, {}).get(key, 0)

    total_new = hu["new_communities"] + intl["new_communities"]
    if ga4 is not None:
        total_visitors = g_site("kozossegek", "visitors") + g_site("meetapedia", "visitors")
    else:
        total_visitors = t_site("kozossegek", "visitors") + t_site("meetapedia", "visitors")
    subject = (f"[közösségek] Napi összefoglaló {day} — "
               f"{total_new} új közösség, {total_visitors} látogató")

    metric_rows = "".join(
        _ROW.format(label=label, hu=hu[k], intl=intl[k], total=hu[k] + intl[k])
        for k, label in _METRICS)

    # Older summaries have no "stock" — fall back to the totals we do have.
    stock = summary.get("stock") or {
        "hu": {"communities": totals["hu"], "covered_pairs": totals["covered_pairs_hu"]},
        "intl": {"communities": totals["intl"], "covered_pairs": totals["covered_pairs_intl"]},
    }

    def s_site(sc: str, key: str) -> int:
        return stock.get(sc, {}).get(key, 0)

    # The funnel. Pageviews and visitors already had a table; what a visit
    # *leads to* did not, so the only reachable conclusion from the report was
    # "traffic went up or down". An outclick is the one event where the site
    # did its job — someone left for the community itself.
    funnel_html = ""
    if funnel:
        _stages = [
            ("Kimenő kattintás", funnel["outclicks"], funnel["outclicks_total"]),
            ("Feliratkozás", funnel["subscriptions"], funnel["subscriptions_total"]),
            ("Közösség igénylés", funnel["claims"], funnel["claims_total"]),
            ("Beküldött közösség", funnel["submissions"], funnel["submissions_total"]),
            ("Javítási kérés", funnel["edit_requests"], funnel["edit_requests_total"]),
        ]
        _rows = "".join(
            f"<tr><td style='padding:4px 12px 4px 0'>{label}</td>"
            f"<td align='right' style='padding:4px 8px;font-weight:600'>{recent}</td>"
            f"<td align='right' style='padding:4px 0 4px 8px;color:#8C8478'>{total}</td></tr>"
            for label, recent, total in _stages)
        _days = funnel.get("days", 30)
        funnel_html = f"""
  <h3 style="margin:18px 0 6px">Vevőszerzés</h3>
  <table style="border-collapse:collapse;font-size:14px">
    <tr style="color:#8C8478;font-size:12px">
      <td style="padding:4px 12px 4px 0"></td>
      <td align="right" style="padding:4px 8px">{_days} nap</td>
      <td align="right" style="padding:4px 0 4px 8px">Mindösszesen</td></tr>
    {_rows}
  </table>
  <p style="color:#B5ADA0;font-size:11px;margin:4px 0 16px">
    {funnel["subscribers_total"]} feliratkozó (egyedi email).</p>"""

    stock_rows = "".join(
        _ROW.format(label=label, hu=s_site("hu", k), intl=s_site("intl", k),
                    total=s_site("hu", k) + s_site("intl", k))
        for k, label in _STOCK_METRICS)

    guide_rows = []
    for guide in guides or []:
        data = guide.get("data") or {}
        domain = "kozossegek.com" if guide.get("site") == "kozossegek" else "meetapedia.com"
        prefix = "utmutatok" if guide.get("site") == "kozossegek" else "guides"
        url = f"https://{domain}/{prefix}/{guide['slug']}"
        model = html_lib.escape(str(data.get("writer_model") or "ismeretlen modell"))
        guide_rows.append(
            "<li style='margin:5px 0'>"
            f"<a href='{url}' style='color:#A8512F'>{html_lib.escape(guide['title'])}</a> "
            f"<span style='color:#8C8478'>· {domain} · {model}</span></li>"
        )
    hu_guides = sum(1 for g in guides or [] if g.get("site") == "kozossegek")
    intl_guides = len(guides or []) - hu_guides
    guides_html = (
        "<h3 style='margin:18px 0 6px'>Új adatútmutatók</h3>"
        f"<p style='margin:0 0 6px;font-size:14px'><b>{len(guides or [])}</b> készült "
        f"({hu_guides} magyar, {intl_guides} nemzetközi).</p>"
        + (f"<ul style='margin:0;padding-left:18px'>{''.join(guide_rows)}</ul>"
           if guide_rows else
           "<p style='margin:0;color:#8C8478;font-size:13px'>Nem készült új útmutató ezen a napon — a napi keret már teljesült, nem volt megfelelő jelölt, vagy az AI-író későbbre halasztódott.</p>")
    )

    # Free-tier AI usage for the day. Its own block because the whole point of
    # the router is that this number stays inside allowances nobody pays for —
    # a provider quietly hitting its ceiling every day is the failure that costs
    # coverage without costing money, so it has to be visible daily.
    ai_html = ""
    providers = summary.get("providers") or []
    if providers:
        rows = []
        for p in providers:
            used, budget = p.get("used", 0), p.get("budget", 0)
            pct = (used * 100 // budget) if budget else 0
            # Amber past 60%, red past 90% — the point is to notice a ceiling
            # before it starts silently truncating a window.
            colour = "#DC2626" if pct >= 90 else ("#D97706" if pct >= 60 else "#16A34A")
            note = []
            if p.get("observed_limit"):
                note.append(f"észlelt plafon {p['observed_limit']}")
            # Tokens, because for several free tiers that is the ceiling that
            # actually ends the day — Groq's 200,000/day runs out while 13,000
            # requests are still nominally available.
            if p.get("tokens"):
                note.append(f"{int(p['tokens']):,} token".replace(",", " "))
            if p.get("rate_limits"):
                note.append(f"{p['rate_limits']}× 429")
            if p.get("failures"):
                note.append(f"{p['failures']} hiba")
            if p.get("cost_usd"):
                note.append(f"${p['cost_usd']:.2f}")
            # A paid provider that answers nothing is the expensive failure, and
            # it is invisible in a table of call counts: `openrouter_paid` sat at
            # 41 calls / 41 failures for four days while every page fell through
            # to a fallback costing four times as much. Named here because a
            # report that only shows totals is how it stayed unnoticed.
            if p.get("paid") and used and p.get("failures") == used:
                note.append("⚠️ egyetlen hívása sem sikerült")
            rows.append(
                f"<tr><td style='padding:4px 12px 4px 0'>{p['name']}</td>"
                f"<td align='right' style='padding:4px 8px;color:{colour};font-weight:600'>"
                f"{used}</td>"
                f"<td align='right' style='padding:4px 8px;color:#8C8478'>{budget}</td>"
                f"<td align='right' style='padding:4px 8px'>{pct}%</td>"
                f"<td style='padding:4px 0 4px 8px;color:#8C8478;font-size:12px'>"
                f"{', '.join(note)}</td></tr>")
        ai_html = (
            "<h3 style='margin:18px 0 6px'>AI-keret (aznapi hívások)</h3>"
            "<table style='border-collapse:collapse;font-size:14px'>"
            "<tr style='color:#8C8478;font-size:12px'>"
            "<td style='padding:4px 12px 4px 0'>Szolgáltató</td>"
            "<td align='right' style='padding:4px 8px'>Hívás</td>"
            "<td align='right' style='padding:4px 8px'>Keret</td>"
            "<td align='right' style='padding:4px 8px'>%</td>"
            "<td style='padding:4px 0 4px 8px'></td></tr>"
            + "".join(rows) + "</table>")

        # The question this block could not answer: how much can we process in a
        # day, and are we collecting about that much? On 2026-08-18 we fetched
        # 2,434 pages and AI-processed 353 of them — a sevenfold over-collection
        # that took an afternoon of arithmetic to notice. Both numbers are
        # already here; the ratio is what makes them mean something.
        _calls = sum(int(p.get("used") or 0) for p in providers)
        _fails = sum(int(p.get("failures") or 0) for p in providers)
        _budget = sum(int(p.get("budget") or 0) for p in providers)
        # The money line. Free capacity ends in a 429 that nobody has to read;
        # paid capacity ends when someone notices the invoice, so the day's
        # spend and the ceiling it is measured against are stated every morning,
        # whether or not anything went wrong.
        _spent = sum(float(p.get("cost_usd") or 0.0) for p in providers)
        _cap = float(summary.get("paid_budget_usd") or 0.0)
        if _spent or _cap:
            _pct = int(_spent * 100 // _cap) if _cap else 0
            _cap_txt = (f"${_cap:.2f} napi keret ({_pct}%)" if _cap
                        else "nincs napi keret beállítva — fizetős "
                             "szolgáltató nem hívható")
            ai_html += (
                "<p style='margin:8px 0 0;font-size:13px'>"
                f"<b>Fizetős költés ma: ${_spent:.2f}</b> / {_cap_txt}.</p>")
        _quarantined = int(summary.get("quarantined_pages") or 0)
        if _quarantined:
            ai_html += (
                "<p style='margin:6px 0 0;font-size:13px;color:#8C8478'>"
                f"Karanténban {_quarantined} oldal — ugyanannál az ujjlenyomatnál "
                "többször ugyanúgy elakadt a kinyerésük, ezért nem próbáljuk "
                "újra. A prompt vagy a modell változása mindet feloldja.</p>")
        _ok = max(0, _calls - _fails)
        _done = int(hu.get("pages_extracted") or 0) + int(intl.get("pages_extracted") or 0)
        _fetched = int(hu.get("pages_scraped") or 0) + int(intl.get("pages_scraped") or 0)
        # Enrichment updates existing records; extraction creates them. Both
        # spend the same free budget, and telling them apart in the report is
        # what turns "488 international records modified" from a puzzle into a
        # number with a cause.
        _hu_mod = int(hu.get("changed_communities") or 0)
        _intl_mod = int(intl.get("changed_communities") or 0)
        if _hu_mod + _intl_mod:
            ai_html += (
                "<p style='margin:6px 0 0;font-size:13px;color:#8C8478'>"
                f"Módosítás: {_hu_mod} magyar, {_intl_mod} nemzetközi rekord — "
                "a leírás-generálás (enrichment) ugyanabból a keretből költ, "
                "mint a kinyerés.</p>")
        if _done:
            # No derived "pages/day capacity" here, deliberately. Five versions
            # of that number were wrong in one morning — modified records vs
            # calls, batch totals vs per-call, successes vs attempts, logical
            # calls vs provider attempts, inferred vs measured — and the last
            # review closed it for good: the budget is not one scalar. Groq's
            # binding limit is 200,000 tokens a day, not its 14,400 requests,
            # so summing request allowances across the fleet and dividing by
            # attempts-per-page is not a computation with an answer. Computing
            # it honestly needs per-workload attempt tagging *and* a mixed-unit
            # budget model; that is a project, not a report line.
            #
            # What is left is measured: what was spent, what came out, and the
            # ratio between collection and processing. Pages per day is then an
            # observation across reports rather than a derivation inside one.
            _enrich = int(summary.get("enrich_attempts") or 0)
            _extract = int(summary.get("extract_attempts") or 0)
            # `max(0, ...)` used to sit here, and on 2026-09-10 it printed
            # "2400 hívás (11 kinyerés 2641 leírás)" — a line whose own numbers
            # do not add up, with nothing said about it. The workload counters
            # and the quota ledger are supposed to measure the same universe of
            # provider attempts; when they disagree the *ledger* is the one that
            # governs routing, so an excess means calls were made that no budget
            # was charged for. Undercounting a budget is how a router walks into
            # a hard block, which makes this the one direction that must never
            # be rounded away. Reported, not clamped.
            _unaccounted = _calls - _enrich - _extract
            _other = max(0, _unaccounted)
            ratio = f"{_fetched / _done:.1f}×" if _done else "—"
            refused = f"{_fails * 100 // _calls}%" if _calls else "0%"
            _split = f"{_extract} kinyerés, {_enrich} leírás"
            if _other:
                # preflight() probes every provider once per run, and the /v1
                # gateway is other software entirely. Named, not folded in.
                _split += f", {_other} egyéb (preflight, átjáró)"
            elif _unaccounted < 0:
                _split += (f" — ⚠️ {-_unaccounted} hívással több, mint amennyit "
                           f"a kvótakönyvelés lát")
            ai_html += (
                "<p style='margin:8px 0 0;font-size:13px;color:#8C8478'>"
                f"{_calls} hívás ({_split}) → <b>{_done} feldolgozott oldal</b>. "
                f"Letöltve {_fetched} oldal — {ratio} a feldolgozottnak. "
                f"Elutasított hívás: {refused}.</p>".replace(",", " "))

    runs_html = ""
    if summary["runs"]:
        items = []
        for r in summary["runs"]:
            # ⚠️ = the run finished, some items failed and are queued for the next
            # run; ❌ = it stopped early. One transient timeout out of 1414 pairs
            # is not the same event as a dead provider (2026-07-31).
            outcome = r.get("outcome") or ("ok" if r["success"] else "aborted")
            state = {"ok": "✅", "warning": "⚠️"}.get(outcome, "❌")
            fails = ""
            if r["search_failed"] or r["extract_failed"]:
                causes = [c for c in (r.get("search_error"), r.get("extract_error")) if c]
                cause = (f" · ok: {html_lib.escape(' / '.join(causes))}" if causes else "")
                colour = "#B4231F" if outcome == "aborted" else "#8A6410"
                fails = (f" — <span style='color:{colour}'>hibák: "
                         f"{r['search_failed']} keresés, {r['extract_failed']} oldal"
                         f" (nem cache-elve, a következő futás újrapróbálja){cause}</span>")
            if r.get("error"):
                fails += (f" — <span style='color:#B4231F'>futási hiba: "
                          f"{html_lib.escape(r['error'])}</span>")
            items.append(
                f"<li style='margin:3px 0'>{state} <b>{r['mode']}</b> · "
                f"{(r['started_at'] or '')[11:16]} UTC · {r['pairs']} város–téma páros · "
                f"{r['records']} rekord{fails}</li>")
        runs_html = ("<h3 style='margin:18px 0 6px'>Futások</h3>"
                     f"<ul style='margin:0;padding-left:18px'>{''.join(items)}</ul>")
    else:
        runs_html = "<p style='color:#8C8478'>Nem futott pipeline ezen a napon.</p>"

    html = f"""
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1C1917;max-width:640px">
  <h2 style="margin:0 0 2px">Napi összefoglaló — {day}</h2>
  <p style="margin:0 0 16px;color:#8C8478;font-size:13px">közösségek.com + meetapedia.com</p>

  <h3 style="margin:0 0 6px">Látogatók{" (GA4)" if ga4 is not None else ""}</h3>
  <table style="border-collapse:collapse;font-size:14px">
    <tr style="color:#8C8478;font-size:12px">
      <td style="padding:4px 12px 4px 0"></td>
      <td align="right" style="padding:4px 8px">Látogató</td>
      {"<td align='right' style='padding:4px 8px'>Munkamenet</td>" if ga4 is not None else ""}
      <td align="right" style="padding:4px 0 4px 8px">Oldalletöltés</td></tr>
    {"".join(
        f"<tr><td style='padding:4px 12px 4px 0'>{site}.com</td>"
        f"<td align='right' style='padding:4px 8px;font-weight:600'>{g_site(site, 'visitors') if ga4 is not None else t_site(site, 'visitors')}</td>"
        + (f"<td align='right' style='padding:4px 8px'>{g_site(site, 'sessions')}</td>" if ga4 is not None else "")
        + f"<td align='right' style='padding:4px 0 4px 8px'>{g_site(site, 'pageviews') if ga4 is not None else t_site(site, 'pageviews')}</td></tr>"
        for site in ("kozossegek", "meetapedia"))}
    <tr style="border-top:1px solid #EAE5DB"><td style="padding:4px 12px 4px 0;font-weight:700">Összesen</td>
      <td align="right" style="padding:4px 8px;font-weight:700">{total_visitors}</td>
      {f"<td align='right' style='padding:4px 8px;font-weight:700'>{g_site('kozossegek', 'sessions') + g_site('meetapedia', 'sessions')}</td>" if ga4 is not None else ""}
      <td align="right" style="padding:4px 0 4px 8px;font-weight:700">{(g_site("kozossegek", "pageviews") + g_site("meetapedia", "pageviews")) if ga4 is not None else (t_site("kozossegek", "pageviews") + t_site("meetapedia", "pageviews"))}</td></tr>
  </table>
  <p style="color:#B5ADA0;font-size:11px;margin:4px 0 16px">
    {"Forrás: Google Analytics 4. Szerveroldali számláló (bot-szűrt): " + str(t_site("kozossegek", "visitors") + t_site("meetapedia", "visitors")) + " látogató." if ga4 is not None else "Szerveroldali számláló (botok kiszűrve); GA4-bekötéshez GA4_PROPERTY_ID + GA4_CREDENTIALS_JSON env kell."}</p>

  {funnel_html}

  <h3 style="margin:0 0 6px">Változások</h3>
  <table style="border-collapse:collapse;font-size:14px">
    <tr style="color:#8C8478;font-size:12px">
      <td style="padding:4px 12px 4px 0"></td>
      <td align="right" style="padding:4px 8px">Magyar</td>
      <td align="right" style="padding:4px 8px">Nemzetközi</td>
      <td align="right" style="padding:4px 0 4px 8px">Össz</td></tr>
    {metric_rows}
  </table>

  {runs_html}

  {guides_html}

  {ai_html}

  <h3 style="margin:18px 0 6px">Állomány (aktuális összesen)</h3>
  <table style="border-collapse:collapse;font-size:14px">
    <tr style="color:#8C8478;font-size:12px">
      <td style="padding:4px 12px 4px 0"></td>
      <td align="right" style="padding:4px 8px">Magyar</td>
      <td align="right" style="padding:4px 8px">Nemzetközi</td>
      <td align="right" style="padding:4px 0 4px 8px">Össz</td></tr>
    {stock_rows}
  </table>

  <p style="color:#B5ADA0;font-size:11px;margin-top:20px">
    közösségek.com napi riport · <a href="https://kozossegek.com/admin" style="color:#A8512F">admin</a></p>
</div>"""
    return subject, html


async def send_daily_report(db_path: Path, hu_cities: set, day: str | None = None) -> dict:
    """Build and send the report for one UTC day (default: yesterday)."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    recipient = os.environ.get("REPORT_EMAIL", "") or os.environ.get("FEEDBACK_EMAIL", "")
    sender = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
    if not api_key or not recipient:
        log.warning("daily_report_skipped", reason="RESEND_API_KEY or FEEDBACK_EMAIL/REPORT_EMAIL missing")
        return {"ok": False, "error": "RESEND_API_KEY or FEEDBACK_EMAIL/REPORT_EMAIL missing"}

    if day is None:
        day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    start_iso = f"{day}T00:00:00"
    end_day = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    end_iso = f"{end_day}T00:00:00"

    summary = get_daily_summary(db_path, start_iso, end_iso, hu_cities)
    # Enrichment spends the same free budget as extraction; without this the
    # report's per-page figure counts description calls against pages they
    # never touched.
    try:
        from .db import get_daily_counter
        summary["enrich_attempts"] = get_daily_counter(db_path, day, "enrich_attempts")
        summary["extract_attempts"] = get_daily_counter(db_path, day, "extract_attempts")
    except Exception as exc:  # noqa: BLE001 — a counter must not stop the report
        log.warning("report_enrich_counter_failed", error=str(exc))
        summary["enrich_attempts"] = summary["extract_attempts"] = 0
    # Free-tier AI spend for the reported day. Best-effort: a report must still
    # go out if the router config is unreadable.
    try:
        from .providers import load_catalogue
        from .router import QuotaLedger
        cat = load_catalogue()
        ledger = QuotaLedger(db_path, day=day)
        summary["providers"] = [p for p in ledger.snapshot(cat)
                                if p["configured"] and p["used"]]
        summary["paid_budget_usd"] = float(cat.router.daily_budget_usd or 0.0)
    except Exception as exc:
        log.warning("report_provider_usage_failed", error=str(exc))
        summary["providers"] = []
        summary["paid_budget_usd"] = 0.0
    # Pages the pipeline has stopped paying to re-extract. Not an error — it is
    # the guard working — but a number that must not grow quietly: a rising
    # count means the token cap or the prompt is wrong for a whole class of
    # pages, and nothing else in the report would say so.
    try:
        import yaml as _yaml

        from .config import CONFIG_DIR, extract_quarantine_threshold
        from .db import count_quarantined_pages
        from .extract import get_extract_fingerprint
        _settings = _yaml.safe_load(
            (CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8")) or {}
        _ds = _settings.get("deepseek") or {}
        _fp = get_extract_fingerprint(_ds.get("fingerprint_model") or _ds.get("model") or "")
        summary["quarantined_pages"] = count_quarantined_pages(
            db_path, _fp, extract_quarantine_threshold())
    except Exception as exc:
        log.warning("report_quarantine_count_failed", error=str(exc))
        summary["quarantined_pages"] = 0
    traffic = get_traffic_for_day(db_path, day)
    ga4 = fetch_ga4_traffic(day)
    # Best-effort, like the provider block above: a missing funnel must not stop
    # the report that carries everything else.
    try:
        from .db import get_funnel_counts
        funnel = get_funnel_counts(db_path, days=30)
    except Exception as exc:
        log.warning("report_funnel_failed", error=str(exc))
        funnel = None
    try:
        guides = get_data_guides_for_day(db_path, day)
    except Exception as exc:
        log.warning("report_guides_failed", error=str(exc))
        guides = []
    subject, html = build_report_html(day, summary, traffic, ga4, funnel, guides)

    import resend
    resend.api_key = api_key
    resend.Emails.send({"from": sender, "to": [recipient],
                        "subject": subject, "html": html})
    log.info("daily_report_sent", day=day, to=recipient)
    return {"ok": True, "day": day, "to": recipient, "subject": subject}
