CATEGORIES = {
    "budget": {
        "name": "Budget & Finances",
        "emoji": "💰",
        "description": "Overall budget, deposits, payments, quotes, cost tracking",
        "keywords": ["budget", "cost", "price", "pay", "deposit", "money", "quote", "invoice", "afford", "spend", "expensive", "cheap", "r", "rand"],
    },
    "venue": {
        "name": "Venue",
        "emoji": "🏛️",
        "description": "Ceremony and reception locations, capacity, pricing, site visits",
        "keywords": ["venue", "location", "hall", "garden", "church", "chapel", "reception", "ceremony space", "estate", "farm"],
    },
    "guests": {
        "name": "Guest List & RSVPs",
        "emoji": "👥",
        "description": "Guest list, invitations, RSVPs, dietary requirements, seating plan",
        "keywords": ["guest", "invite", "invitation", "rsvp", "dietary", "seating", "table", "list", "attending", "plus one", "head count"],
    },
    "catering": {
        "name": "Catering & Food",
        "emoji": "🍽️",
        "description": "Food, drinks, cake, menu planning, catering quotes",
        "keywords": ["food", "catering", "menu", "cake", "drinks", "bar", "cocktail", "dinner", "lunch", "eat", "chef", "buffet", "canapes"],
    },
    "photography": {
        "name": "Photography & Video",
        "emoji": "📸",
        "description": "Photographer, videographer, shot list, editing, delivery timeline",
        "keywords": ["photo", "photographer", "video", "videographer", "shoot", "shots", "editing", "album", "film", "capture", "drone"],
    },
    "decor": {
        "name": "Flowers & Decor",
        "emoji": "💐",
        "description": "Florist, flowers, centerpieces, decorations, theme, colour palette",
        "keywords": ["flower", "floral", "decor", "decoration", "centerpiece", "bouquet", "florist", "theme", "colour", "color", "candle", "light", "arch", "table setting"],
    },
    "entertainment": {
        "name": "Entertainment & Music",
        "emoji": "🎵",
        "description": "DJ, band, MC, playlist, first dance, ceremony music",
        "keywords": ["dj", "band", "music", "mc", "entertainment", "dance", "song", "playlist", "first dance", "ceremony music", "sound"],
    },
    "attire": {
        "name": "Attire & Fashion",
        "emoji": "👗",
        "description": "Wedding dress, suits, bridesmaids, groomsmen, accessories, fittings",
        "keywords": ["dress", "suit", "attire", "bridesmaid", "groomsman", "wear", "outfit", "shoes", "veil", "tux", "fashion", "fitting", "tailor"],
    },
    "ceremony": {
        "name": "Ceremony & Vows",
        "emoji": "💍",
        "description": "Officiant, vows, rings, order of service, readings, processional",
        "keywords": ["ceremony", "vow", "ring", "officiant", "reading", "order of service", "processional", "recessional", "exchange", "priest", "pastor"],
    },
    "logistics": {
        "name": "Transport & Accommodation",
        "emoji": "🚗",
        "description": "Guest accommodation, transport, shuttles, parking, honeymoon travel",
        "keywords": ["transport", "car", "hotel", "accommodation", "stay", "travel", "parking", "shuttle", "airport", "drive", "uber", "guests staying"],
    },
    "vendors": {
        "name": "Vendors & Contracts",
        "emoji": "📋",
        "description": "All vendor contacts, contracts, bookings, payment schedules",
        "keywords": ["vendor", "contract", "book", "confirm", "supplier", "contact", "signed", "agreement", "hire", "booking", "deposit paid"],
    },
    "timeline": {
        "name": "Day-Of Timeline",
        "emoji": "📅",
        "description": "Run sheet, schedule, day-of coordination, timing",
        "keywords": ["timeline", "schedule", "run sheet", "day of", "timing", "start time", "end time", "when", "order", "program", "itinerary"],
    },
    "honeymoon": {
        "name": "Honeymoon",
        "emoji": "✈️",
        "description": "Honeymoon destination, flights, hotels, activities, travel plans",
        "keywords": ["honeymoon", "trip", "holiday", "vacation", "destination", "flight", "resort", "after wedding", "travel"],
    },
}


def detect_category(text: str) -> str | None:
    text_lower = text.lower()
    scores = {}
    for key, cat in CATEGORIES.items():
        score = sum(1 for kw in cat["keywords"] if kw in text_lower)
        if score > 0:
            scores[key] = score
    return max(scores, key=scores.get) if scores else None
