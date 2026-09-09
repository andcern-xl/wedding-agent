"""Weekly self-audit — the bot checks its own memory instead of waiting to be told.

Three memory regressions have shipped to this bot, and all three had the same
shape: information silently stopped being visible, nothing crashed, and the only
detector was Ansen noticing bad answers weeks or months later.

  Jul 2026  recall narrowed while wedding_drops stayed a silo   ("my DJ plans are gone")
  Aug 2026  knowledge_sweep read "the last 20 drops"            ("forgetting April and May")
  Aug 2026  supersession retired facts with no containment check (43 of 85 lost a specific)

A guard rail for the first one already existed — sweep_recall.py, plus a line in
the QA checklist saying to run it after any memory change. It had been failing
for weeks. Nobody ran it. That is the actual lesson: a check a human has to
remember during unrelated work is not a guarantee.

So the bot runs these itself, weekly, and messages Ansen ONLY when something
fails. Silence means the invariants hold.

    python self_audit.py            # full report, exit 1 on failure
    python self_audit.py --quiet     # only failures

Invariants asserted:
  1 reachability   every knowledge store is surfaceable through unified recall
  2 preservation   no recent retirement dropped a specific
  3 loop closure   no open question whose answer is already known
  4 freshness      no store or scheduled loop has gone quiet
  5 contradiction  no active fact argues with another
  6 duplicates     nothing recorded twice, no impossible date ranges
  7 structured IDs durable identifiers are queryable, not buried in prose
"""
import json
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv("/Users/ansen/wedding-agent/.env")

from tools.db import get_client                                      # noqa: E402
from tools.tz import local_today                                     # noqa: E402
from tools.user_memory import _norm, fact_specifics, supersession_is_safe  # noqa: E402

MODEL = "claude-sonnet-4-6"

# How long a store or loop may be silent before it counts as broken. Generous —
# this is looking for "stopped working", not "quiet week".
SILENT_DAYS = {
    "brain_entries": 14,
    "check_ins": 21,
    "conversation_history": 7,
    "wedding_drops": 30,
}
# Every scheduled loop that is supposed to leave a trace, and how long it may go
# quiet. Checked against the EXPECTED list, not against whatever rows happen to
# exist — priority_brief had no row at all because it had crashed on every run
# since inception, and a check that iterates existing rows cannot see a job that
# has never run once. "Never ran" is a worse failure than "stopped running" and
# was the harder one to notice.
LOOP_SILENT_DAYS = {
    "morning_brief": 3,
    "proactive_check": 5,
    "baby_weekly": 10,
    "priority_brief": 10,        # Sunday wedding priorities
    "appointment_prebrief": 30,  # only fires the night before a medical event
    "babybumps_nuggets": 5,
    "knowledge_sweep_drops": 14,
    "calendar_snapshot": 3,
    "conversation_sweep": 3,     # daily; capped window, so a gap loses facts      # written every reconciliation; stale = sweep broken
}

_STOP = {"the", "and", "for", "with", "this", "that", "your", "you", "are", "was",
         "has", "have", "will", "from", "into", "wedding", "screenshot", "details",
         "summary", "event", "https", "http", "com", "jess", "ansen", "jessica"}


# ── helpers ─────────────────────────────────────────────────────────────────

def _client():
    from anthropic import Anthropic
    return Anthropic()


def _ask(prompt: str, max_tokens: int = 300) -> dict:
    try:
        r = _client().messages.create(model=MODEL, max_tokens=max_tokens,
                                      messages=[{"role": "user", "content": prompt}])
        m = re.search(r"\{.*\}", r.content[0].text, re.DOTALL)
        return json.loads(m.group()) if m else {}
    except Exception as e:
        return {"error": str(e)[:80]}


def _words(t: str) -> set:
    return {w.strip(".,;:") for w in re.findall(r"[a-z0-9$.]{3,}", (t or "").lower())}


def _distinctive(text: str, idf: Counter, n: int = 3) -> list[str]:
    """Pick the RAREST words in a row, not the first few.

    sweep_recall.py takes the first three long words, which for a screenshot
    beginning "Wedding Planning Message - DJ/Music Details" gives
    ['planning','message','music'] — generic terms that match everything and
    prove nothing. A real question uses the distinctive words, so the test
    should too."""
    seen, cands = set(), []
    for w in re.findall(r"[a-zA-Z]{4,}", (text or "").lower()):
        if w in _STOP or w in seen:
            continue
        seen.add(w)
        cands.append(w)
    cands.sort(key=lambda w: idf.get(w, 0))
    return cands[:n]


class Result:
    def __init__(self, name: str, headline: str):
        self.name, self.headline = name, headline
        self.failures: list[str] = []
        self.notes: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.failures

    def fail(self, msg: str):
        self.failures.append(msg)

    def note(self, msg: str):
        self.notes.append(msg)


# ── 1. reachability ─────────────────────────────────────────────────────────

def check_reachability(sample: int = 4) -> Result:
    r = Result("reachability", "every store surfaceable through unified recall")
    import agent as agent_mod

    c = get_client()
    stores = [
        ("brain_entries", lambda: [x for x in c.table("brain_entries").select("fact")
                                   .eq("status", "active").limit(400).execute().data or []],
         lambda x: x.get("fact", "")),
        ("wedding_drops", lambda: c.table("wedding_drops").select("content")
                                  .order("ts", desc=True).limit(300).execute().data or [],
         lambda x: x.get("content", "")),
        ("baby_knowledge", lambda: c.table("baby_knowledge").select("summary")
                                   .limit(200).execute().data or [],
         lambda x: x.get("summary", "")),
        # Travel documents must be reachable, because the pre-trip reminder is
        # required to quote them and cannot fall back on prose in a summary.
        # The needle is the NUMBER, not a prefix of the row: recall renders
        # documents as "Jess — passport — United States — no. A06688257", which
        # never contains the first 40 characters of the raw row. Testing the
        # prefix reported all four as unreachable while every real query
        # returned them — a false red, which is the failure mode that got the
        # old sweep_recall ignored.
        ("travel_docs", lambda: c.table("travel_docs").select("*").limit(50).execute().data or [],
         lambda x: " ".join(str(x.get(k) or "") for k in
                            ("person", "doc_type", "nationality", "number", "notes")),
         lambda x: str(x.get("number") or "")),
    ]

    for spec in stores:
        name, loader, textof = spec[0], spec[1], spec[2]
        needle_of = spec[3] if len(spec) > 3 else (lambda x: None)
        try:
            rows = loader()
        except Exception as e:
            r.fail(f"{name}: could not read ({str(e)[:50]})")
            continue
        if not rows:
            r.note(f"{name}: empty, skipped")
            continue

        idf = Counter()
        for row in rows:
            idf.update(set(re.findall(r"[a-zA-Z]{4,}", textof(row).lower())))

        # A very short row has no distinctive words to test with — sampling one
        # produces a meaningless query ("what budget") and a false miss.
        substantial = [r for r in rows if len(textof(r)) >= 40] or rows
        step = max(1, len(substantial) // sample)
        tested = substantial[::step][:sample]
        misses = []
        for row in tested:
            text = textof(row)
            kws = _distinctive(text, idf)
            if not kws:
                continue
            try:
                res = agent_mod._query_brain_sync(" ".join(kws))
            except Exception as e:
                r.fail(f"{name}: recall raised ({str(e)[:40]})")
                break
            blob = _norm(" ".join(
                str(v) for sec in res.values() if isinstance(sec, list)
                for item in sec for v in item.values()))
            # Strip leading tags like "[screenshot] " — recall drops them when
            # rendering, so a needle that keeps them never matches. Third
            # variant of the same artifact; each one produced a false red, and
            # a check that cries wolf is a check nobody reads.
            needle = needle_of(row) or re.sub(r"^\s*(\[[^\]]{1,24}\]\s*)+", "", text)[:40]
            if _norm(needle) not in blob:
                misses.append(f"{kws} → {text[:50]}")
        if misses:
            r.fail(f"{name}: {len(misses)}/{len(tested)} sampled rows unreachable "
                   f"— e.g. {misses[0]}")
        else:
            r.note(f"{name}: {len(rows)} rows, {len(tested)} sampled, all reachable")
    return r


def check_structured_identifiers() -> Result:
    """Durable identifiers must live in a queryable store, not in prose.

    Ansen's Singapore passport number sat in user_summaries for months. That
    store reaches chat by injection but is invisible to query_brain, so
    trip_milestone_brief — the thing that writes his travel reminders — could
    not see it, and neither could I when I went looking: I told him it was not
    recorded and he correctly said he had shared it.

    Wiring user_summaries into unified recall wholesale is not the fix; it holds
    a wallet address, medication and a dentist, and _query_brain_sync has no
    user context, so one person's private summary would surface in the other's
    chat. The invariant instead: if a durable identifier appears in prose, it
    must ALSO exist in the purpose-built store. Only the identifier's TYPE is
    reported, never its value.
    """
    r = Result("structured identifiers", "durable IDs are queryable, not just prose")
    patterns = {
        "passport/NRIC-format ID": re.compile(r"\b[A-Z]\d{7}[A-Z]\b"),
    }
    try:
        summaries = get_client().table("user_summaries").select("user_id,summary").execute().data or []
    except Exception as e:
        r.fail(f"could not read user_summaries ({str(e)[:40]})")
        return r

    try:
        from tools.travel_docs import get_docs
        known = {(d.get("number") or "").strip() for d in get_docs(include_inactive=True)}
    except Exception:
        known = set()

    orphans: dict = {}
    for row in summaries:
        text = row.get("summary") or ""
        for label, rx in patterns.items():
            for match in set(rx.findall(text)):
                if match not in known:
                    # The value is deliberately never printed — only its type
                    # and where it is stranded.
                    orphans.setdefault((row.get("user_id"), label), 0)
                    orphans[(row.get("user_id"), label)] += 1
    for (uid, label), count in orphans.items():
        r.fail(f"{count} {label}(s) sit only in user_summaries(user_id={uid}) — not in "
               f"travel_docs, so no brief can quote them and no search will find them")
    if not orphans:
        r.note(f"{len(summaries)} summaries scanned, every identifier also in a store")
    return r


# ── 2. preservation ─────────────────────────────────────────────────────────

def check_preservation(window_days: int = 14) -> Result:
    """Did anything retired RECENTLY drop a specific?

    Scoped to a window on purpose. The historical backlog is known and already
    triaged by restore_eroded.py; leaving it in scope would keep this red
    forever, and a check that is permanently red is a check nobody reads."""
    r = Result("preservation", f"no retirement in the last {window_days}d dropped a specific")
    c = get_client()
    rows = c.table("brain_entries").select("*").execute().data or []
    byid = {x["id"]: x for x in rows}
    cutoff = (local_today() - timedelta(days=window_days)).isoformat()

    recent = [x for x in rows
              if x.get("status") == "superseded"
              and (x.get("updated_at") or "")[:10] >= cutoff]
    unlinked = [x for x in recent if not x.get("superseded_by")]
    lossy = []
    for x in recent:
        rep = byid.get(x.get("superseded_by"))
        if not rep:
            continue
        safe, why = supersession_is_safe(x.get("fact", ""), rep.get("fact", ""))
        if not safe:
            lossy.append((x.get("fact", ""), why))

    for f, w in lossy[:3]:
        r.fail(f"dropped a specific ({w}) — {f[:64]}")
    if len(lossy) > 3:
        r.fail(f"…and {len(lossy)-3} more retirement(s) that dropped a specific")
    for x in unlinked[:2]:
        r.fail(f"retired with no replacement recorded — {x.get('fact','')[:64]}")
    if len(unlinked) > 2:
        r.fail(f"…and {len(unlinked)-2} more with no replacement recorded")
    if not recent:
        r.note("nothing retired in the window")
    else:
        r.note(f"{len(recent)} retired in window, {len(recent)-len(lossy)-len(unlinked)} clean")
    return r


# ── 3. loop closure ─────────────────────────────────────────────────────────

def check_loop_closure() -> Result:
    """Is the bot still asking things it already knows, or asking twice?"""
    r = Result("loop closure", "no open question whose answer is already known")
    c = get_client()
    try:
        open_cis = c.table("check_ins").select("*").eq("status", "open").execute().data or []
    except Exception as e:
        r.fail(f"could not read check_ins ({str(e)[:40]})")
        return r
    if not open_cis:
        r.note("no open check-ins")
        return r

    # duplicates — deterministic, no model needed
    dupes = []
    for i, a in enumerate(open_cis):
        for b in open_cis[i + 1:]:
            wa, wb = _words(a.get("question", "")), _words(b.get("question", ""))
            if wa and wb and len(wa & wb) / len(wa | wb) >= 0.5:
                dupes.append((a.get("question", "")[:56], b.get("question", "")[:56]))
    for x, y in dupes[:3]:
        r.fail(f"asked twice — answering one leaves the other nagging:\n   ↳ {x}\n   ↳ {y}")
    if len(dupes) > 3:
        r.fail(f"…and {len(dupes)-3} more duplicate pair(s)")

    # already answered — needs judgement, so ask, but only for a handful
    import agent as agent_mod
    def already_known(ci):
        q = ci.get("question", "")
        try:
            res = agent_mod._query_brain_sync(q)
        except Exception:
            return None
        ev = "\n".join(
            f"[{sec}] " + " ".join(str(v) for v in item.values())[:200]
            for sec, items in res.items() if isinstance(items, list)
            for item in items[:6])
        v = _ask(f"""An assistant is still asking its users this OPEN question:

QUESTION: {q}

Its own memory returns this when searched on the subject:
{ev or "(nothing)"}

Is the question ALREADY ANSWERED by that evidence? Reply ONLY JSON:
{{"answered": true/false, "answer": "<20 words>"}}
Answer true only if the evidence directly resolves it.""", 200)
        return (ci, v) if v.get("answered") else None

    with ThreadPoolExecutor(max_workers=8) as ex:
        hits = [x for x in ex.map(already_known, open_cis[:15]) if x]
    for ci, v in hits[:4]:
        r.fail(f"already answered, still asking — {ci.get('question','')[:56]}\n"
               f"   ↳ known: {v.get('answer','')[:64]}")
    if len(hits) > 4:
        r.fail(f"…and {len(hits)-4} more already answered")

    stale = [ci for ci in open_cis
             if (ci.get("created_at") or "")[:10] <
             (local_today() - timedelta(days=10)).isoformat()]
    if stale:
        r.note(f"{len(stale)} card(s) open >10d — they will expire unanswered")
    r.note(f"{len(open_cis)} open card(s) checked")
    return r


# ── 4. freshness ────────────────────────────────────────────────────────────

def check_freshness() -> Result:
    """A store or scheduled loop that stopped writing is a broken feature that
    reports no error. Jess's proactive_check went quiet for two weeks and
    nothing said so."""
    r = Result("freshness", "no store or scheduled loop has gone quiet")
    c = get_client()
    today = local_today()

    for table, limit_days in SILENT_DAYS.items():
        col = "updated_at" if table == "conversation_history" else (
            "ts" if table == "wedding_drops" else "created_at")
        try:
            rows = (c.table(table).select(col).order(col, desc=True)
                    .limit(1).execute().data or [])
        except Exception as e:
            r.fail(f"{table}: unreadable ({str(e)[:40]})")
            continue
        if not rows:
            r.note(f"{table}: empty")
            continue
        last = (rows[0].get(col) or "")[:10]
        try:
            age = (today - date.fromisoformat(last)).days
        except ValueError:
            continue
        if age > limit_days:
            r.fail(f"{table}: no write in {age}d (limit {limit_days}d) — last {last}")
        else:
            r.note(f"{table}: last write {age}d ago")

    try:
        loops = c.table("loop_state").select("*").execute().data or []
    except Exception as e:
        r.fail(f"loop_state unreadable ({str(e)[:40]})")
        return r
    by_name = defaultdict(list)
    for row in loops:
        by_name[row.get("loop_name") or ""].append(row)

    for name, limit_days in LOOP_SILENT_DAYS.items():
        rows = by_name.get(name) or []
        if not rows:
            # No row has ever been written. Either it is newly added, or every
            # run has thrown before reaching its state save.
            r.fail(f"loop '{name}' has NEVER run — no state row exists")
            continue
        for row in rows:
            last = row.get("last_run_date") or ""
            try:
                age = (today - date.fromisoformat(last)).days
            except ValueError:
                r.fail(f"loop '{name}' (user {row.get('user_id')}): unreadable last_run_date {last!r}")
                continue
            if age > limit_days:
                r.fail(f"loop '{name}' (user {row.get('user_id')}): last ran {last}, "
                       f"{age}d ago (limit {limit_days}d)")
            else:
                r.note(f"loop '{name}' ran {age}d ago")
    return r


# ── 6. duplicate records ────────────────────────────────────────────────────

def check_duplicates() -> Result:
    """The same thing recorded twice is not cosmetic: the Seoul trip had its
    flights in one row and its hotel in the other, so neither could answer a
    question about the trip, and the travel view listed Seoul twice. Nothing
    deduplicated trips on insert until Sep 2026."""
    r = Result("duplicates", "nothing recorded twice")
    try:
        from tools.trips import find_duplicate_trips
        pairs = find_duplicate_trips()
    except Exception as e:
        r.fail(f"trip duplicate check failed ({str(e)[:50]})")
        return r
    for a, b in pairs[:4]:
        r.fail(f"same trip twice — {a.get('destination')} {a.get('start_date')} "
               f"({a['id'][:8]} / {b['id'][:8]})")
    if len(pairs) > 4:
        r.fail(f"…and {len(pairs)-4} more duplicate trip pair(s)")

    # An end date before a start date sorts the trip into the wrong month and
    # hides it — the Bangkok bachelor trip vanished exactly this way.
    try:
        rows = get_client().table("trips").select("*").execute().data or []
    except Exception:
        rows = []
    for t in rows:
        st, en = t.get("start_date"), t.get("end_date")
        if st and en and en < st:
            r.fail(f"impossible dates — {t.get('destination')} runs {st} to {en}")
    if not r.failures:
        r.note(f"{len(rows)} trip(s), no duplicates, all date ranges valid")
    return r


# ── 5. contradiction ────────────────────────────────────────────────────────

def check_contradictions(max_pairs: int = 25) -> Result:
    """Two active facts that disagree make every answer a coin flip. Only pairs
    sharing a distinctive entity are compared — comparing all of them is
    quadratic and mostly noise."""
    r = Result("contradiction", "no active fact argues with another")
    c = get_client()
    rows = [x for x in (c.table("brain_entries").select("id,fact,domain,fact_date,status")
                        .limit(600).execute().data or [])
            if x.get("status") == "active"]

    by_entity = defaultdict(list)
    for x in rows:
        sp = fact_specifics(x.get("fact", ""))
        for e in sp["entities"]:
            if len(e) >= 5 and not e.isdigit():
                by_entity[e.lower()].append(x)

    # Only DISTINCTIVE entities link two facts meaningfully. Sharing "November"
    # or "Singapore" is not a relationship — the first run paired a marriage
    # certificate against a hotel room offer on exactly that basis.
    pairs, seen = [], set()
    for ent, group in sorted(by_entity.items(), key=lambda kv: len(kv[1])):
        if not (2 <= len(group) <= 4):
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                key = tuple(sorted((a["id"], b["id"])))
                if key in seen:
                    continue
                seen.add(key)
                sa = fact_specifics(a.get("fact", ""))
                sb = fact_specifics(b.get("fact", ""))
                # only worth asking when they both assert a value or a term
                if (sa["money"] or sa["date"] or sa["terms"]) and \
                   (sb["money"] or sb["date"] or sb["terms"]):
                    pairs.append((ent, a, b))
    pairs = pairs[:max_pairs]
    if not pairs:
        r.note("no comparable pairs")
        return r

    def judge(item):
        ent, a, b = item
        v = _ask(f"""Two facts are both ACTIVE in a couple's shared memory. Today is {local_today()}.

A ({a.get('fact_date')}): {a.get('fact','')}
B ({b.get('fact_date')}): {b.get('fact','')}

Do they CONTRADICT? Answer true ONLY if you can name the specific value that
differs — the same quantity with two amounts, the same date with two days, or
one calling open what the other calls settled.

Complementary detail about a shared subject is NOT a contradiction. Two facts
about the same person or venue are NOT a contradiction. Different topics that
merely mention the same name are NOT a contradiction. When in doubt, false.

Reply ONLY JSON:
{{"contradicts": true/false, "what": "the value that differs, <15 words>"}}""", 180)
        return (a, b, v) if v.get("contradicts") else None

    with ThreadPoolExecutor(max_workers=10) as ex:
        hits = [x for x in ex.map(judge, pairs) if x]
    for a, b, v in hits[:3]:
        r.fail(f"{v.get('what','conflict')[:60]}\n"
               f"   ↳ {a.get('fact','')[:58]}\n"
               f"   ↳ {b.get('fact','')[:58]}")
    if len(hits) > 3:
        r.fail(f"…and {len(hits)-3} more contradicting pair(s)")
    r.note(f"{len(pairs)} pair(s) compared")
    return r


# ── runner ──────────────────────────────────────────────────────────────────

CHECKS = (check_reachability, check_preservation, check_loop_closure,
          check_freshness, check_contradictions, check_duplicates,
          check_structured_identifiers)


def run_all() -> list[Result]:
    out = []
    for fn in CHECKS:
        try:
            out.append(fn())
        except Exception as e:
            bad = Result(fn.__name__.replace("check_", ""), "check itself errored")
            bad.fail(f"the check raised: {str(e)[:110]}")
            out.append(bad)
    return out


def telegram_report(results: list[Result]) -> str | None:
    """Message for Ansen — only built when something failed. Silence is the
    healthy state, so a passing week sends nothing at all."""
    failed = [r for r in results if not r.ok]
    if not failed:
        return None
    lines = ["🔍 <b>Memory self-audit</b>", ""]
    for r in failed:
        lines.append(f"🔴 <b>{r.name.title()}</b> — {r.headline}")
        for f in r.failures[:4]:
            lines.append(f"• {f}")
        if len(r.failures) > 4:
            lines.append(f"• …{len(r.failures)-4} more")
        lines.append("")
    passed = [r.name for r in results if r.ok]
    if passed:
        lines.append(f"✅ Passing: {', '.join(passed)}")
    return "\n".join(lines)


def main() -> int:
    quiet = "--quiet" in sys.argv
    results = run_all()
    for r in results:
        mark = "✅" if r.ok else "❌"
        if not quiet or not r.ok:
            print(f"\n{mark} {r.name.upper()} — {r.headline}")
            for f in r.failures:
                print(f"   🔴 {f}")
            if not quiet:
                for n in r.notes:
                    print(f"      {n}")
    bad = [r for r in results if not r.ok]
    print(f"\n{'❌' if bad else '✅'} {len(results)-len(bad)}/{len(results)} invariants hold")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
