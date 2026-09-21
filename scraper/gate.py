"""The joinability gate: one cheap typed question before an expensive extraction.

**What it is for.** 81.8% of extracted pages yield no community (104,795 of
128,072, measured 2026-09-20), and every one of them costs a full extraction
call from a free fleet that allows ~2,100 a day. A Jev `noul` question costs
about $0.0001 and answers, with a calibrated probability, whether the page is
worth the call at all. Measured on 525 corpus-proportioned pages: 16% rejected,
**one** genuine loss, 40.9% of calls saved.

**What it must never do.** A gate decision is a classifier's opinion, not an
extraction result. It is stored in its own table, never in the extraction
cache, because writing "no communities" there would record it permanently
under the current fingerprint — the one thing CLAUDE.md forbids outright. Every
uncertainty resolves the same way: **when in doubt, extract.** A network error,
a 429, a spent budget, a missing key, an unreadable answer — each of them lets
the page through. The gate can only ever *save* work, never *lose* it through
its own failure.

**Releasing.** The fingerprint covers the model, the question and the
threshold, and is half the primary key, so changing any of them re-opens every
held page automatically — the same mechanism the extraction quarantine uses. A
single page can be released by hand from /admin/gate.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog

from .db import bump_daily_counter, get_daily_counter, get_gate_scores, save_gate_decision

log = structlog.get_logger()

#: The question, verbatim from the benchmark that measured the thresholds. It
#: is part of the fingerprint: editing a word re-opens every held page, which
#: is correct — the measured 0.06 was measured for *this* wording.
GATE_QUESTION = (
    "Does this page provide evidence of at least one genuine community group "
    "or club in or near the specified city and relevant to the specified topic, "
    "where that same group meets or organizes activities regularly, is open to "
    "new members from the general public, and has a group identity rather than "
    "being only a venue, commercial course, professional ensemble, one-time "
    "event, annual event, news article, or directory entry for another city?"
)

CF_URL_TEMPLATE = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"

#: Per-million input tokens. Output is free — Jev returns typed decisions, not
#: generated text — so this is the whole price.
USD_PER_MTOK = 0.042

#: Daily counter names, so the report and /admin can read the same numbers.
CALLS_COUNTER = "gate_calls"
TOKENS_COUNTER = "gate_input_tokens"
SKIPPED_COUNTER = "gate_skipped_pages"


def gate_fingerprint(model: str, threshold: float) -> str:
    """Identity of one gate configuration.

    Model, question and threshold together. A change to any of them makes the
    old decisions inapplicable rather than merely stale — a page rejected at
    0.06 might pass at 0.03 — so they key the table and the old rows simply
    stop matching.
    """
    material = f"{model}|{GATE_QUESTION}|{threshold:.4f}"
    return hashlib.sha256(material.encode()).hexdigest()[:12]


class JoinabilityGate:
    """Decides which pages are worth extracting. Fails open, always."""

    def __init__(self, db_path: Path, *, enabled: bool = False,
                 threshold: float = 0.06, model: str = "typesafe/jev",
                 provider: str = "cloudflare", account_id: str = "",
                 max_text_chars: int = 8000, daily_budget_usd: float = 0.0,
                 timeout_seconds: float = 60.0):
        self.db_path = db_path
        self.threshold = float(threshold)
        self.model = model
        self.provider = provider
        self.account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        self.max_text_chars = int(max_text_chars)
        self.daily_budget_usd = float(daily_budget_usd)
        self.timeout_seconds = float(timeout_seconds)
        self.fingerprint = gate_fingerprint(model, threshold)
        self._key = self._resolve_key()
        #: Reasons the gate stood aside, for the run log.
        self.skipped = 0
        self.errors = 0
        self.budget_spent = False
        self._enabled = bool(enabled)
        self._scores: dict[str, float] | None = None
        if self._enabled and not self._key:
            log.warning("gate_disabled", reason="no api key for provider",
                        provider=provider)

    # ── Configuration ────────────────────────────────────────────────────────

    def _resolve_key(self) -> str:
        if self.provider == "typesafe":
            return os.environ.get("TYPESAFE_API_KEY", "")
        return os.environ.get("CLOUDFLARE_API_TOKEN", "")

    @property
    def enabled(self) -> bool:
        """Configured, keyed, and inside its budget. Anything else is off."""
        return bool(self._enabled and self._key and not self.budget_spent)

    def _scores_for_run(self) -> dict[str, float]:
        if self._scores is None:
            self._scores = get_gate_scores(self.db_path, self.fingerprint)
            log.info("gate_ready", fingerprint=self.fingerprint,
                     threshold=self.threshold, known=len(self._scores),
                     model=self.model, provider=self.provider)
        return self._scores

    # ── Budget ───────────────────────────────────────────────────────────────

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def spent_today_usd(self) -> float:
        tokens = get_daily_counter(self.db_path, self._today(), TOKENS_COUNTER)
        return tokens / 1e6 * USD_PER_MTOK

    def _over_budget(self) -> bool:
        """A ceiling, not a permission. 0 means no gate calls at all.

        The paid-provider post-mortem of 2026-08 is the reason this exists in
        the same shape: a permission without an amount burned ~$60 in four
        days. Reaching the ceiling turns the gate off for the day; it never
        aborts a run, because a gate that stops working simply means every page
        is extracted, which is what happened before it existed.
        """
        if self.daily_budget_usd <= 0:
            return True
        return self.spent_today_usd() >= self.daily_budget_usd

    # ── The decision ─────────────────────────────────────────────────────────

    async def allows(self, url_hash: str, url: str, text: str,
                     city: str, topic: str) -> bool:
        """True when the page should be extracted. False only on a confident no.

        Never raises. Every failure path returns True.
        """
        if not self.enabled or not text:
            return True

        known = self._scores_for_run().get(url_hash)
        if known is not None:
            if known < self.threshold:
                self.skipped += 1
                return False
            return True

        if self._over_budget():
            if not self.budget_spent:
                log.info("gate_budget_spent", spent_usd=round(self.spent_today_usd(), 4),
                         budget_usd=self.daily_budget_usd)
            self.budget_spent = True
            return True

        try:
            score, tokens = await self._ask(url, text, city, topic)
        except Exception as exc:
            # Down, rate-limited, refused, unreadable — all the same answer.
            self.errors += 1
            log.warning("gate_unavailable", url=url, error=str(exc)[:200])
            return True

        bump_daily_counter(self.db_path, self._today(), CALLS_COUNTER, 1)
        bump_daily_counter(self.db_path, self._today(), TOKENS_COUNTER, tokens)
        save_gate_decision(self.db_path, url_hash, self.fingerprint, score,
                           self.model, url)
        self._scores_for_run()[url_hash] = score

        if score < self.threshold:
            self.skipped += 1
            bump_daily_counter(self.db_path, self._today(), SKIPPED_COUNTER, 1)
            log.info("gate_rejected", url=url, score=round(score, 4))
            return False
        return True

    async def _ask(self, url: str, text: str, city: str, topic: str) -> tuple[float, int]:
        state = {"city": city, "topic": topic, "source_url": url,
                 "page_text": text[: self.max_text_chars]}
        questions = {"has_joinable_community": {"type": "noul",
                                                "instructions": GATE_QUESTION}}
        if self.provider == "cloudflare":
            if not self.account_id:
                raise RuntimeError("CLOUDFLARE_ACCOUNT_ID is not set")
            endpoint = CF_URL_TEMPLATE.format(account=self.account_id)
            payload = {"model": self.model,
                       "input": {"state": state, "questions": questions}}
        else:
            endpoint = TYPESAFE_URL
            payload = {"model": self.model, "state": state, "questions": questions}

        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
        # Workers AI nests the model's envelope inside the gateway's; TypeSafe
        # does not. Unwrap until the answer appears rather than counting levels.
        for _ in range(3):
            if "answers" in body or not isinstance(body.get("result"), dict):
                break
            body = body["result"]
        answers = body.get("answers") or {}
        if "has_joinable_community" not in answers:
            raise RuntimeError(f"no answer in response: {json.dumps(body)[:200]}")
        usage = int((body.get("usage") or {}).get("input_tokens", 0))
        return float(answers["has_joinable_community"]["noul"]), usage

    def summary(self) -> dict:
        return {"enabled": self.enabled, "fingerprint": self.fingerprint,
                "threshold": self.threshold, "skipped": self.skipped,
                "errors": self.errors, "budget_spent": self.budget_spent,
                "spent_usd": round(self.spent_today_usd(), 4)}
