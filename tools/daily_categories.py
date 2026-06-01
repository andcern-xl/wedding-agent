from tools.db import get_client

BUILT_IN_CATEGORIES = {
    "finance": {
        "name": "Finance",
        "emoji": "💳",
        "description": "bills, banking, subscriptions, investments",
        "keywords": ["bill", "bank", "payment", "pay ", "invoice", "subscription", "invest", "budget", "money", "transfer", "tax", "insurance", "rent", "salary"],
    },
    "health": {
        "name": "Health",
        "emoji": "🏥",
        "description": "appointments, gym, medication, doctors",
        "keywords": ["doctor", "dentist", "appointment", "gym", "medication", "medicine", "health", "hospital", "clinic", "exercise", "workout", "physio", "therapy"],
    },
    "home": {
        "name": "Home",
        "emoji": "🏠",
        "description": "repairs, maintenance, groceries, errands",
        "keywords": ["grocery", "groceries", "shopping", "repair", "fix", "clean", "laundry", "maintenance", "plumber", "electrician", "supermarket", "pick up", "drop off", "errand"],
    },
    "work": {
        "name": "Work",
        "emoji": "💼",
        "description": "meetings, deadlines, projects, calls",
        "keywords": ["meeting", "deadline", "project", "work", "office", "email", "client", "report", "presentation", "call ", "interview", "review", "submit"],
    },
    "social": {
        "name": "Social",
        "emoji": "🎉",
        "description": "events, birthdays, plans with people",
        "keywords": ["birthday", "party", "dinner", "lunch", "drinks", "catch up", "event", "wedding", "celebrate", "gift", "invite", "rsvp", "visit"],
    },
    "travel": {
        "name": "Travel",
        "emoji": "✈️",
        "description": "trips, bookings, transport, flights",
        "keywords": ["flight", "hotel", "trip", "travel", "holiday", "vacation", "book", "visa", "passport", "airport", "train", "drive to", "uber"],
    },
    "personal": {
        "name": "Personal",
        "emoji": "🙋",
        "description": "anything personal that doesn't fit elsewhere",
        "keywords": [],
    },
}


def get_all_categories() -> dict:
    """Return built-ins merged with custom categories from DB."""
    custom_rows = get_client().table("daily_categories").select("*").execute().data or []
    result = dict(BUILT_IN_CATEGORIES)
    for row in custom_rows:
        result[row["slug"]] = {
            "name": row["name"],
            "emoji": row["emoji"],
            "description": row.get("description", ""),
            "keywords": [],
            "custom": True,
        }
    return result


def add_custom_category(name: str, emoji: str, created_by: int, description: str = "") -> dict:
    slug = name.lower().strip().replace(" ", "_")
    row = {
        "name": name.strip().title(),
        "slug": slug,
        "emoji": emoji,
        "description": description,
        "created_by": created_by,
    }
    return get_client().table("daily_categories").insert(row).execute().data[0]


def detect_daily_category(text: str) -> str | None:
    text_lower = text.lower()
    scores: dict[str, int] = {}
    for key, cat in BUILT_IN_CATEGORIES.items():
        score = sum(1 for kw in cat["keywords"] if kw in text_lower)
        if score > 0:
            scores[key] = score
    # Also check custom categories from DB (slug/name match)
    custom_rows = get_client().table("daily_categories").select("slug,name").execute().data or []
    for row in custom_rows:
        if row["slug"] in text_lower or row["name"].lower() in text_lower:
            scores[row["slug"]] = scores.get(row["slug"], 0) + 2
    if not scores:
        return None
    return max(scores, key=scores.get)
