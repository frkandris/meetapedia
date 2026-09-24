import hashlib
import json as _json
import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


def _coerce_str(v):
    """Convert LLM-produced dict/list values to strings before Pydantic validates them."""
    if v is None:
        return v
    if isinstance(v, dict):
        return _json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return v


class SearchResult(BaseModel):
    url: str
    title: str
    snippet: str = ""


class CommunityRecord(BaseModel):
    name: str
    topic: str
    city: str
    locale: str
    description: str | None = None
    # SEO enrichment (written by scraper/enrich.py, NOT by extraction). Kept
    # separate from `description` so a re-extraction can't revert them, and
    # preserved across saves by _merge_source_urls. short = one-line card/meta
    # text; long = the rich community-page body.
    short_description: str | None = None
    long_description: str | None = None
    meeting_schedule: str | None = None
    location: str | None = None
    contact: str | None = None
    website: str | None = None
    social_links: list[str] = Field(default_factory=list)
    source_url: str
    source_urls: list[str] = Field(default_factory=list)
    extracted_at: str
    confidence: float | None = None
    joinable: bool = True  # open, recurring group a person can join
    community_id: str = ""
    # Extended profile fields
    founding_year: int | None = None
    member_count: str | None = None
    fee: str | None = None
    age_range: str | None = None
    skill_level: str | None = None
    join_process: str | None = None
    leader: str | None = None
    email: str | None = None
    phone: str | None = None
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    history: str | None = None      # community background / founding story
    frequency: str | None = None    # meeting regularity: Heti / Kéthetente / Havi / Alkalmi

    _NULL_STRINGS: frozenset = frozenset({
        "nincs megadva", "n/a", "nem ismert", "unknown", "none",
        "not provided", "not available", "-", "–", "na", "ismeretlen",
        "keine angabe", "unbekannt",
    })

    # Matches leaked JSON tail: `", 0.9, true, ...` or `", 2025", 0.9, true, ...`
    _LEAKED_JSON_RE = re.compile(r'[""]\s*,\s*(?:\d[\d.]*\s*[",]|true\b|false\b).*$', re.DOTALL)

    @field_validator(
        "description", "short_description", "long_description",
        "meeting_schedule", "location", "contact", "website",
        "member_count", "fee", "age_range", "skill_level", "join_process",
        "leader", "email", "phone", "language", "history", "frequency",
        mode="before",
    )
    @classmethod
    def _coerce_str_fields(cls, v):
        return _coerce_str(v)

    @model_validator(mode="after")
    def _clean_and_generate_id(self) -> "CommunityRecord":
        # Strip JSON artifacts that bled into the name field (LLM formatting glitch)
        cleaned = self._LEAKED_JSON_RE.sub('', self.name).strip(' ",')
        if cleaned and cleaned != self.name:
            self.name = cleaned

        # Null out placeholder strings in text fields
        for field in ("phone", "contact", "location", "meeting_schedule",
                      "description", "fee", "history", "frequency",
                      "leader", "join_process", "skill_level", "age_range", "language",
                      "member_count", "website", "email"):
            v = getattr(self, field, None)
            if isinstance(v, str) and v.strip().lower() in self._NULL_STRINGS:
                setattr(self, field, None)

        # Normalize website: add https:// if no scheme present, and drop what is
        # not an address at all. Before this, "N/A" and "Lerne Deutsch in Bern"
        # were published as links (47 visible records, 2026-09-24).
        if self.website:
            w = self.website.strip()
            if w and not w.startswith(("http://", "https://")):
                w = "https://" + w
            host = urlparse(w).netloc if w else ""
            self.website = w if host and "." in host and not any(
                c.isspace() for c in host) else None

        # Keep only actual URLs in social_links
        self.social_links = [
            lnk for lnk in self.social_links
            if isinstance(lnk, str) and lnk.strip().startswith(("http://", "https://"))
        ]

        # Email must contain @
        if self.email and "@" not in self.email:
            self.email = None

        # Phone must contain at least one digit
        if self.phone and not any(c.isdigit() for c in self.phone):
            self.phone = None

        # Tags: strip, deduplicate, cap at 8
        if self.tags:
            self.tags = list(dict.fromkeys(t.strip() for t in self.tags if t.strip()))[:8]

        # Ensure source_url is always in source_urls
        if self.source_url and self.source_url not in self.source_urls:
            self.source_urls = [self.source_url] + self.source_urls

        if not self.community_id:
            key = f"{self.name.lower()}|{self.city.lower()}"
            self.community_id = hashlib.sha256(key.encode()).hexdigest()[:12]
        return self


class VenueRecord(BaseModel):
    """A physical location that hosts community activities."""
    name: str
    city: str
    locale: str
    address: str | None = None
    venue_type: str | None = None        # café | park | cultural_center | library | church | sports_hall | studio | other
    welcomed_topics: list[str] = Field(default_factory=list)   # topic name slugs
    description: str | None = None
    website: str | None = None
    social_links: list[str] = Field(default_factory=list)
    email: str | None = None
    phone: str | None = None
    contact: str | None = None
    source_url: str
    source_urls: list[str] = Field(default_factory=list)
    extracted_at: str
    venue_id: str = ""
    community_ids: list[str] = Field(default_factory=list)     # communities meeting here

    _NULL_STRINGS: frozenset = frozenset({
        "nincs megadva", "n/a", "nem ismert", "unknown", "none",
        "not provided", "not available", "-", "–", "na", "ismeretlen",
    })

    @field_validator(
        "address", "venue_type", "description", "website", "email", "phone", "contact",
        mode="before",
    )
    @classmethod
    def _coerce_str_fields(cls, v):
        return _coerce_str(v)

    @model_validator(mode="after")
    def _clean_and_generate_id(self) -> "VenueRecord":
        for field in ("address", "venue_type", "description", "contact", "phone"):
            v = getattr(self, field, None)
            if isinstance(v, str) and v.strip().lower() in self._NULL_STRINGS:
                setattr(self, field, None)

        if self.website:
            w = self.website.strip()
            if w and not w.startswith(("http://", "https://")):
                w = "https://" + w
            self.website = w or None

        self.social_links = [
            lnk for lnk in self.social_links
            if isinstance(lnk, str) and lnk.strip().startswith(("http://", "https://"))
        ]

        if self.email and "@" not in self.email:
            self.email = None

        if self.phone and not any(c.isdigit() for c in self.phone):
            self.phone = None

        if self.source_url and self.source_url not in self.source_urls:
            self.source_urls = [self.source_url] + self.source_urls

        if not self.venue_id:
            key = f"{self.name.lower()}|{self.city.lower()}"
            self.venue_id = hashlib.sha256(key.encode()).hexdigest()[:12]
        return self


PERSON_ROLES = (
    "leader", "instructor", "speaker",
    "organizer", "founder", "coach", "trainer",
    "moderator", "admin", "member", "volunteer", "coordinator",
)


class PersonRecord(BaseModel):
    """A person associated with a community as leader, instructor, or speaker."""
    name: str
    role: str                            # leader | instructor | speaker
    city: str
    topic: str
    community_name: str
    community_id: str = ""
    bio: str | None = None              # 1–2 sentence description
    email: str | None = None
    website: str | None = None
    social_links: list[str] = Field(default_factory=list)
    source_url: str
    source_urls: list[str] = Field(default_factory=list)
    extracted_at: str
    person_id: str = ""

    @field_validator("bio", "email", "website", mode="before")
    @classmethod
    def _coerce_str_fields(cls, v):
        return _coerce_str(v)

    @model_validator(mode="after")
    def _clean_and_generate_id(self) -> "PersonRecord":
        if self.role not in PERSON_ROLES:
            self.role = "leader"

        if self.email and "@" not in self.email:
            self.email = None

        if self.website:
            w = self.website.strip()
            if w and not w.startswith(("http://", "https://")):
                w = "https://" + w
            self.website = w or None

        self.social_links = [
            lnk for lnk in self.social_links
            if isinstance(lnk, str) and lnk.strip().startswith(("http://", "https://"))
        ]

        if self.source_url and self.source_url not in self.source_urls:
            self.source_urls = [self.source_url] + self.source_urls

        if len(self.name.split()) < 2:
            raise ValueError(f"Person name is a single word, skipping: {self.name!r}")

        if not self.person_id:
            key = f"{self.name.lower()}|{self.city.lower()}|{self.role}|{self.community_name.lower()}"
            self.person_id = hashlib.sha256(key.encode()).hexdigest()[:12]
        return self


class RunMetadata(BaseModel):
    last_run: str
    records_by_city_topic: dict[str, dict[str, int]] = Field(default_factory=dict)
    total_records: int = 0
