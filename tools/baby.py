from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

_SGT = ZoneInfo("Asia/Singapore")

# ── Pregnancy constants ────────────────────────────────────────────────────────
LMP = date(2026, 5, 14)       # Last menstrual period
DUE_DATE = LMP + timedelta(days=280)  # 40 weeks


def _today() -> date:
    return datetime.now(_SGT).date()


def current_week() -> int:
    days = (_today() - LMP).days
    return max(1, min(42, days // 7 + 1))


def current_day_in_week() -> int:
    return (_today() - LMP).days % 7 + 1


def trimester() -> int:
    w = current_week()
    if w <= 13:
        return 1
    elif w <= 26:
        return 2
    return 3


def days_until_due() -> int:
    return max(0, (DUE_DATE - _today()).days)


def pregnancy_summary() -> dict:
    week = current_week()
    day = current_day_in_week()
    tri = trimester()
    due = DUE_DATE
    days_left = days_until_due()
    return {
        "week": week,
        "day": day,
        "trimester": tri,
        "total_weeks": 40,
        "due_date": due.strftime("%-d %B %Y"),
        "days_until_due": days_left,
        "lmp": LMP.strftime("%-d %B %Y"),
    }


# ── Key clinical milestones by week ───────────────────────────────────────────
# Used to surface "coming up" warnings before each milestone.
MILESTONES = [
    (6,  "Viability scan (heartbeat check) — usually weeks 6–8"),
    (8,  "First OB appointment if not already done"),
    (10, "NIPT blood test (non-invasive prenatal testing) — weeks 10–13"),
    (11, "Nuchal translucency (NT) scan — weeks 11–14"),
    (13, "End of first trimester — miscarriage risk drops significantly"),
    (16, "Second trimester blood screening (if doing quad screen)"),
    (20, "Anatomy scan (detailed ultrasound) — weeks 18–22"),
    (24, "Glucose tolerance test (GDH) — weeks 24–28"),
    (28, "Rhesus factor injection if Jess is Rh-negative"),
    (32, "Growth scan — weeks 32–36"),
    (36, "Group B strep swab — weeks 35–37"),
    (36, "Weekly OB appointments begin"),
    (38, "Hospital bag should be ready"),
    (40, "Due date — 18 February 2027"),
]


def upcoming_milestones(within_weeks: int = 4) -> list[str]:
    week = current_week()
    result = []
    for milestone_week, desc in MILESTONES:
        if week <= milestone_week <= week + within_weeks:
            weeks_away = milestone_week - week
            if weeks_away == 0:
                result.append(f"📍 This week (W{milestone_week}): {desc}")
            elif weeks_away == 1:
                result.append(f"⏳ Next week (W{milestone_week}): {desc}")
            else:
                result.append(f"🗓 In {weeks_away} weeks (W{milestone_week}): {desc}")
    return result
