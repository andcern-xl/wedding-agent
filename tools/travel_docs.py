"""Travel documents — passports, passes, known-traveler numbers.

Ansen: "for travel related reminder, always include our passport details in the
reminder - we always have to bump it up."

A detail a reminder must ALWAYS carry cannot live as prose in a summary and be
found by search. Before this, the only passport data sat inside
user_summaries(user_id=63756531) — injected into chat but invisible to
query_brain, so trip_milestone_brief, which writes the pre-trip reminders, could
not see it. It also had no expiry field, so nothing could check the six-month
validity rule most countries apply.

MISSING is a first-class status. A reminder that says "your Singapore passport
isn't on file — add it" is useful; one that silently omits it is how you end up
looking it up yourself again.
"""
from datetime import date, timedelta

from tools.db import get_client
from tools.tz import local_today

PEOPLE = ("ansen", "jess")

# Most destinations require a passport valid for six months beyond entry, so an
# in-date passport can still fail. This is the check nobody runs by hand.
VALIDITY_MONTHS_REQUIRED = 6


def get_docs(person: str | None = None, include_inactive: bool = False) -> list[dict]:
    try:
        q = get_client().table("travel_docs").select("*")
        if person:
            q = q.eq("person", person.lower())
        rows = q.order("person").execute().data or []
    except Exception:
        return []
    if include_inactive:
        return rows
    return [r for r in rows if r.get("status") in ("active", "missing")]


def upsert_doc(person: str, doc_type: str, number: str | None = None,
               nationality: str | None = None, issued: str | None = None,
               expires: str | None = None, notes: str | None = None,
               status: str = "active") -> dict | None:
    """One row per (person, doc_type) — a new passport supersedes the old one
    rather than sitting alongside it."""
    person, doc_type = person.lower(), doc_type.lower()
    payload = {k: v for k, v in {
        "person": person, "doc_type": doc_type, "number": number,
        "nationality": nationality, "issued": issued, "expires": expires,
        "notes": notes, "status": status,
        "updated_at": local_today().isoformat(),
    }.items() if v is not None}
    try:
        existing = (get_client().table("travel_docs").select("id")
                    .eq("person", person).eq("doc_type", doc_type)
                    .in_("status", ["active", "missing"]).execute().data or [])
        if existing:
            get_client().table("travel_docs").update(payload).eq("id", existing[0]["id"]).execute()
            return {**payload, "id": existing[0]["id"]}
        return (get_client().table("travel_docs").insert(payload).execute().data or [None])[0]
    except Exception:
        return None


def validity_warning(doc: dict, travel_date: str | None) -> str | None:
    """Is this document a problem for a trip on `travel_date`?"""
    expires = doc.get("expires")
    if not expires:
        return None
    try:
        exp = date.fromisoformat(expires)
        when = date.fromisoformat(travel_date) if travel_date else local_today()
    except (TypeError, ValueError):
        return None
    if exp <= when:
        return f"EXPIRED before travel ({expires})"
    needed = when + timedelta(days=VALIDITY_MONTHS_REQUIRED * 30)
    if exp < needed:
        months = max(0, (exp - when).days) // 30
        return (f"expires {expires} — only ~{months} month(s) of validity at travel, "
                f"under the {VALIDITY_MONTHS_REQUIRED} months most countries require")
    return None


def render_for_reminder(travel_date: str | None = None) -> str:
    """The block a travel reminder pastes in verbatim.

    Deliberately spells out what is NOT on file. Silence reads as "nothing to
    say", which is how the details ended up being looked up by hand every time.
    """
    docs = get_docs()
    if not docs:
        return ("TRAVEL DOCUMENTS: nothing on file yet. Tell them so, and ask for "
                "passport numbers and expiry dates for both of them.")

    by_person: dict = {}
    for d in docs:
        by_person.setdefault(d.get("person") or "?", []).append(d)

    lines = []
    for person in PEOPLE:
        rows = by_person.get(person)
        label = person.capitalize()
        if not rows:
            lines.append(f"  {label}: nothing on file — ask for passport number and expiry")
            continue
        for d in sorted(rows, key=lambda r: r.get("doc_type") or ""):
            kind = (d.get("doc_type") or "doc").replace("_", " ")
            if d.get("status") == "missing":
                lines.append(f"  {label} — {kind}: NOT ON FILE. {d.get('notes') or ''}".rstrip())
                continue
            bits = []
            if d.get("nationality"):
                bits.append(d["nationality"])
            if d.get("number"):
                bits.append(f"no. {d['number']}")
            if d.get("expires"):
                bits.append(f"expires {d['expires']}")
            line = f"  {label} — {kind}: " + ", ".join(bits)
            warn = validity_warning(d, travel_date)
            if warn:
                line += f"  ⚠️ {warn}"
            if d.get("notes"):
                line += f" ({d['notes']})"
            lines.append(line)
    return "TRAVEL DOCUMENTS (include these verbatim in any travel reminder):\n" + "\n".join(lines)


def nationalities() -> dict:
    """Person → passport nationality, for running a visa check per passport
    rather than once for the pair."""
    out = {}
    for d in get_docs():
        if d.get("doc_type") == "passport" and d.get("nationality"):
            out[d.get("person")] = d["nationality"]
    return out


def search(query: str) -> list[dict]:
    """Unified-recall hook. A new knowledge store that is not wired into
    _query_brain_sync becomes a silo, which is the bug that lost the DJ plans
    in July — see CLAUDE.md."""
    q = (query or "").lower()
    if not q:
        return []
    terms = [w for w in q.split() if len(w) > 2]
    out = []
    for d in get_docs():
        blob = " ".join(str(d.get(k) or "") for k in
                        ("person", "doc_type", "nationality", "number", "notes")).lower()
        if any(t in blob for t in terms) or any(
                t in ("passport", "visa", "document", "documents", "travel") for t in terms):
            out.append(d)
    return out
