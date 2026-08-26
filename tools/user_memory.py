from datetime import date, datetime, timedelta, timezone
from tools.db import get_client
from tools.tz import local_today

SHARED_BRAIN_ID = 0  # sentinel user_id for the couple's shared context


def get_summary(user_id: int) -> str:
    rows = (
        get_client().table("user_summaries")
        .select("summary")
        .eq("user_id", user_id)
        .execute()
        .data or []
    )
    return rows[0]["summary"] if rows else ""


def save_summary(user_id: int, summary: str, message_count: int):
    get_client().table("user_summaries").upsert({
        "user_id": user_id,
        "summary": summary,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "message_count": message_count,
    }).execute()


# ── Shared brain vault (brain_entries table) ────────────────────────────────
# One fact per row, domain-filed, supersede-not-rewrite. The legacy blob on
# user_summaries user_id=0 is frozen as the rollback path — never write it.

DOMAINS = ("baby", "wedding", "travel", "money", "life")

_DOMAIN_ALIASES = {
    "baby": "baby", "baby_questions": "baby", "pregnancy": "baby",
    "wedding": "wedding",
    "travel": "travel", "trip": "travel", "trips": "travel",
    "money": "money", "finance": "money", "financial": "money",
    "stocks": "money", "budget": "money",
    "life": "life",
}


def normalize_domain(raw: str | None) -> str:
    return _DOMAIN_ALIASES.get((raw or "").strip().lower(), "life")


def get_active_entries(domain: str | None = None, kind: str | None = None) -> list[dict]:
    try:
        q = (
            get_client().table("brain_entries")
            .select("id,domain,fact,fact_date,source,kind")
            .eq("status", "active")
        )
        if domain:
            q = q.eq("domain", domain)
        if kind:
            q = q.eq("kind", kind)
        rows = q.order("fact_date", desc=False).execute().data or []
    except Exception:
        # kind column not migrated yet — treat everything as a fact
        if kind == "episode":
            return []
        q = (
            get_client().table("brain_entries")
            .select("id,domain,fact,fact_date,source")
            .eq("status", "active")
        )
        if domain:
            q = q.eq("domain", domain)
        rows = q.order("fact_date", desc=False).execute().data or []
        for r in rows:
            r["kind"] = "fact"
    order = {d: i for i, d in enumerate(DOMAINS)}
    rows.sort(key=lambda r: (order.get(r.get("domain"), len(DOMAINS)), r.get("fact_date") or ""))
    return rows


def get_episodes(days: int = 45) -> list[dict]:
    """Active episodes (dated life events), newest first, within the window."""
    cutoff = (local_today() - timedelta(days=days)).isoformat()
    rows = [
        e for e in get_active_entries(kind="episode")
        if (e.get("fact_date") or "") >= cutoff
    ]
    rows.sort(key=lambda r: r.get("fact_date") or "", reverse=True)
    return rows


def add_brain_entry(fact: str, domain: str = "life", source: str = "chat",
                    fact_date: str | None = None, kind: str = "fact") -> dict:
    payload = {
        "fact": fact.strip(),
        "domain": normalize_domain(domain),
        "source": source,
        "fact_date": fact_date or local_today().isoformat(),
        "kind": kind,
    }
    try:
        row = get_client().table("brain_entries").insert(payload).execute().data or [{}]
    except Exception:
        # kind column not migrated yet
        payload.pop("kind", None)
        row = get_client().table("brain_entries").insert(payload).execute().data or [{}]
    return row[0]


def supersede_entries(entry_ids: list[str], superseded_by: str | None = None) -> None:
    if not entry_ids:
        return
    payload = {
        "status": "superseded",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if superseded_by:
        payload["superseded_by"] = superseded_by
    get_client().table("brain_entries").update(payload).in_("id", entry_ids).execute()


def render_shared_summary(entries: list[dict]) -> str:
    """Render vault entries in the legacy bullet grammar `• YYYY-MM-DD: [domain] fact`.
    No header lines — _relevant_bullets parses dates from fixed offsets per line."""
    return "\n".join(
        f"• {e['fact_date']}: [{e['domain']}] {e['fact']}" for e in entries
    )


def get_legacy_shared_blob() -> str:
    rows = (
        get_client().table("user_summaries")
        .select("summary")
        .eq("user_id", SHARED_BRAIN_ID)
        .execute()
        .data or []
    )
    return rows[0]["summary"] if rows else ""


def get_shared_summary() -> str:
    """Facts only. Episodes are dated life events — they reach the agent through
    RECALL (query_brain / get_episodes), not by being injected into every brief's
    brain slice. Rendering them here fed dead nags ("Unanswered check-in: travel
    insurance still not done") back in as standing knowledge, which is how briefs
    ended up repeating resolved items."""
    try:
        entries = get_active_entries(kind="fact")
    except Exception:
        entries = []
    return render_shared_summary(entries) if entries else get_legacy_shared_blob()


def append_shared_summary(content: str, domain: str = "life", source: str = "manual") -> str:
    """Add one fact to the shared brain vault. Returns the full updated summary."""
    add_brain_entry(content, domain=domain, source=source)
    return get_shared_summary()


def get_message_count(user_id: int) -> int:
    rows = (
        get_client().table("user_summaries")
        .select("message_count")
        .eq("user_id", user_id)
        .execute()
        .data or []
    )
    return rows[0]["message_count"] if rows else 0


# ── Supersession safety guard ───────────────────────────────────────────────
# A supersession is a DELETE: the old row stops being visible to every reader.
# The Aug 2026 audit found 43 of 85 real retirements had destroyed a specific —
# Emily DJ's payment terms, the FYSH contract total, the bar vendor's name and
# balance date, Kayue's lighting/video constraint. The merge judgement is one
# cheap LLM call over a numbered list, with nothing checking that the winner
# actually carried the loser's content. This is that check.
#
# The rule is asymmetric on purpose:
#   ENTITIES  (names, vendors, flight codes, refs, emails) must carry over
#             literally — an entity does not change when a fact is updated.
#   VALUES    (money, dates, percentages, times) may change value, but the new
#             fact must still carry a value of that KIND. "$940 paid" may become
#             "$1,880 paid"; it may not become "the deposit is paid".
#
# Failing open (keeping both rows) costs a duplicate. Failing closed costs a
# fact, permanently, with no way to notice. Duplicates are the cheaper mistake.

import re as _re

_ENTITY_STOP = {
    "The", "This", "That", "They", "There", "Then", "These", "Those", "Their",
    "And", "But", "For", "Not", "Was", "Were", "Are", "Has", "Have", "Had",
    "With", "From", "Into", "Onto", "Over", "Under", "After", "Before",
    "All", "Any", "Both", "Each", "Only", "Still", "Also", "Now", "Yet",
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Sept",
    "Oct", "Nov", "Dec", "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday", "Week", "Confirmed", "Booked", "Paid",
    "Pending", "Total", "Deposit", "Balance", "Cost", "Price", "Rate",
    "January", "February", "March", "April", "June", "July", "August",
    "September", "October", "November", "December",
    "Pte", "Ltd", "Inc", "Pty", "Co", "Llc",
    "Ansen", "Jess", "Jessica",  # present in nearly every fact; no signal
}

_MONEY_RE   = _re.compile(r"(?:S?\$|SGD|USD|EUR|GBP|KRW|THB)\s?[\d,]+(?:\.\d+)?\s?[KkMm]?\b", _re.I)
_PCT_RE     = _re.compile(r"\d+(?:\.\d+)?\s?%")
_TIME_RE    = _re.compile(r"\b\d{1,2}:\d{2}\s?(?:am|pm)?\b|\b\d{1,2}[.:]\d{2}\s?(?:am|pm)\b", _re.I)
_ISODATE_RE = _re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TEXTDATE_RE = _re.compile(
    r"\b\d{1,2}\s?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\b"
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s?\d{1,2}\b", _re.I)
_CODE_RE    = _re.compile(r"\b[A-Z]{2,3}\d{2,5}\b")          # SQ714, EK349
_REF_RE     = _re.compile(r"\b\d{7,}\b")                      # confirmation numbers
_EMAIL_RE   = _re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_URL_RE     = _re.compile(r"https?://\S+|\b[\w-]+\.(?:com|sg|net|org|io|ai)\b", _re.I)
_PROPER_RE  = _re.compile(r"\b[A-Z][a-zA-Z'&-]{2,}\b")

# Terms are the clauses that GOVERN behaviour — when a thing is payable, whether
# it can be undone, what is ruled out. Emily's row is the reason this exists:
# "$350", "$525" and "Emily" all survived elsewhere, so every specifics check
# said nothing was lost — while "no upfront deposit, payment due after the
# event" was gone, which is the only part that decides whether to chase a
# payment in August for a November set.
_TERM_RES = [
    _re.compile(r"no\s+(?:upfront\s+)?deposit(?:\s+(?:required|needed))?", _re.I),
    _re.compile(r"payment\s+due\s+(?:after|before|on|upon)\s+\w+(?:\s+\w+){0,3}", _re.I),
    _re.compile(r"(?:balance|remainder|remaining)\s+due\s+(?:by\s+|on\s+)?\w+(?:\s+\w+){0,3}", _re.I),
    _re.compile(r"non-?refundable|fully\s+refundable|refundable\s+rate", _re.I),
    _re.compile(r"paid\s+(?:in\s+full|upfront|on\s+the\s+day)", _re.I),
    _re.compile(r"cannot\s+(?:also\s+)?\w+(?:\s+\w+){0,2}", _re.I),
    _re.compile(r"(?:not|never)\s+(?:be\s+)?(?:included|covered|eligible|required|allowed)", _re.I),
]


def _proper_entities(t: str) -> set:
    """Capitalised words that are really entities.

    Two artifacts made the audit cry wolf, and a check that cries wolf gets
    ignored — which is the failure this whole guard exists to prevent:

      "Jess's hospitalisation insurance ..."  -> "Jess's" read as a new entity
      "Found out baby is a boy."              -> "Found" read as a name

    So possessives collapse to the base word, and a capitalised token only
    counts if it appears somewhere that is NOT the start of a sentence. A real
    name shows up mid-sentence; a capitalised verb only ever leads one."""
    out = set()
    for m in _PROPER_RE.finditer(t):
        word = m.group()
        for suffix in ("'s", "\u2019s"):
            if word.endswith(suffix):
                word = word[:-len(suffix)]
                break
        word = word.rstrip("'\u2019")
        if not word or word in _ENTITY_STOP:
            continue
        before = t[:m.start()].rstrip()
        sentence_initial = (not before) or before[-1] in ".!?\n:—-•"
        if not sentence_initial:
            out.add(word)
            continue
        # led a sentence here — keep it only if it also appears mid-sentence
        for m2 in _re.finditer(_re.escape(m.group()), t):
            b2 = t[:m2.start()].rstrip()
            if b2 and b2[-1] not in ".!?\n:—-•":
                out.add(word)
                break
    return out


def _norm(s: str) -> str:
    return _re.sub(r"[\s,]", "", (s or "").lower())


def fact_specifics(text: str) -> dict:
    """Pull the load-bearing details out of a fact, split into entities (must
    survive a rewrite) and values (may change, but the kind must survive)."""
    t = text or ""
    entities = set()
    for rx in (_CODE_RE, _REF_RE, _EMAIL_RE, _URL_RE):
        entities |= {m.group() for m in rx.finditer(t)}
    entities |= _proper_entities(t)
    terms = set()
    for rx in _TERM_RES:
        terms |= {_re.sub(r"\s+", " ", m.group().lower()) for m in rx.finditer(t)}
    return {
        "entities": entities,
        "terms": terms,
        "money": {m.group() for m in _MONEY_RE.finditer(t)},
        "pct":   {m.group() for m in _PCT_RE.finditer(t)},
        "time":  {m.group() for m in _TIME_RE.finditer(t)},
        "date":  {m.group() for m in _ISODATE_RE.finditer(t)}
                 | {m.group() for m in _TEXTDATE_RE.finditer(t)},
    }


def supersession_is_safe(old_fact: str, new_fact: str) -> tuple[bool, str]:
    """May `new_fact` retire `old_fact` without losing information?

    Returns (safe, reason). Safe means every entity in the old fact reappears in
    the new one, and for every kind of value the old fact carried, the new fact
    carries a value of that kind too."""
    old, new = fact_specifics(old_fact), fact_specifics(new_fact)
    new_blob = _norm(new_fact)

    missing = sorted((e for e in old["entities"]
                      if _norm(e) not in new_blob
                      and _norm(e).rstrip("s") not in new_blob),
                     key=lambda e: (-len(e), e))
    if missing:
        return False, f"drops entity: {', '.join(missing[:4])}"

    lost_terms = sorted(x for x in old["terms"] if _norm(x) not in new_blob)
    if lost_terms:
        return False, f"drops term: {'; '.join(lost_terms[:2])}"

    # money and dates are load-bearing: a fact that had them must not become one
    # that has none. Percentages and times move too freely to gate on (a 50%
    # deposit legitimately becomes "fully paid") — they are recorded, not enforced.
    for kind in ("money", "date"):
        if old[kind] and not new[kind]:
            return False, f"drops {kind}: {', '.join(sorted(old[kind])[:3])}"

    return True, "specifics carried over"


def restore_entries(entry_ids: list[str]) -> int:
    """Un-supersede. The vault had no reverse gear, which is why the erosion was
    invisible: nothing could ever come back."""
    if not entry_ids:
        return 0
    res = (
        get_client().table("brain_entries")
        .update({"status": "active",
                 "superseded_by": None,
                 "updated_at": datetime.now(timezone.utc).isoformat()})
        .in_("id", entry_ids)
        .execute()
    )
    return len(res.data or [])
