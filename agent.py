import base64
import json
from anthropic import AsyncAnthropic
from categories import CATEGORIES, detect_category
from tools.memory import get_all_memory
from tools.google_docs import fetch_docs_for_category, extract_doc_id
from tools.log import get_drops, get_recent_drops
from tools.payments import add_payment, summary as payment_summary

SYSTEM_PROMPT = """You are a wedding planning assistant for a couple planning their wedding. They drop notes, screenshots, and discussions into this chat as they go — treat everything they've sent as your source of truth.

Your job is to help them make sense of what they've gathered, answer questions, spot gaps, and keep things moving.

WEDDING CATEGORIES
{categories}

HOW TO RESPOND
- Be concise and practical
- Reference specific things they've actually dropped — quotes, details, numbers
- If a screenshot contains a quote, venue, menu, or price — extract and summarise it clearly
- Plain text only, no asterisks or markdown symbols
- CAPS for section headers
- Sound like a sharp friend helping them plan, not a robot"""


class WeddingAgent:
    def __init__(self):
        self.client = AsyncAnthropic()

    def _build_system_prompt(self) -> str:
        cat_lines = "\n".join(
            f"- {v['emoji']} {k}: {v['name']} — {v['description']}"
            for k, v in CATEGORIES.items()
        )
        return SYSTEM_PROMPT.format(categories=cat_lines)

    def _drops_block(self, drops: list[dict], label: str = "") -> str:
        if not drops:
            return ""
        lines = [f"{label}\n"] if label else []
        for d in drops:
            ts = d["ts"][:10]
            icon = "📸" if d["kind"] == "image" else "💬"
            cat_tag = f"[{d.get('category', '')}] " if d.get("category") else ""
            lines.append(f"{icon} {ts} {cat_tag}{d['content']}")
        return "\n".join(lines)

    async def handle_message(self, text: str) -> dict:
        category = detect_category(text)
        drops = get_drops(category=category, limit=40) if category else get_recent_drops(limit=30)
        context = self._drops_block(drops, "WHAT YOUVE SHARED SO FAR:")

        # Auto-detect Google Doc URLs and note them
        doc_note = ""
        if "docs.google.com" in text:
            doc_id = extract_doc_id(text)
            if doc_id and category:
                from tools.memory import link_doc_to_category
                link_doc_to_category(category, doc_id)
                cat_name = CATEGORIES.get(category, {}).get("name", category)
                doc_note = f"\n\n[Doc auto-linked to {cat_name}]"
            docs = fetch_docs_for_category(category) if category else ""
            if docs:
                context += f"\n\nLINKED DOC CONTENT:\n{docs}"

        user_content = f"[Context]\n{context}\n\n[Message]\n{text}" if context else text

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": user_content}],
        )

        return {
            "text": response.content[0].text + doc_note,
            "detected_category": category,
        }

    async def _extract_payment(self, image_bytes: bytes, caption: str) -> dict | None:
        """Try to extract structured payment data from a financial screenshot."""
        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        prompt = """Look at this image. If it contains financial information (invoice, quote, payment confirmation, bank transfer, receipt, bill), extract the details as JSON.

Return ONLY a JSON object with these fields (omit fields you can't determine):
{
  "vendor": "who is being paid e.g. Molenvliet Venue",
  "amount": 45000,
  "currency": "ZAR",
  "paid_by": "name of person who paid, or null if unknown",
  "status": "paid OR owing OR deposit OR quote",
  "date": "YYYY-MM-DD or null",
  "notes": "one line description e.g. 50% deposit for reception venue"
}

If this image has no financial content, return: {"skip": true}"""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": (caption + "\n\n" if caption else "") + prompt},
                ],
            }],
        )

        try:
            text = response.content[0].text.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text.strip())
            if data.get("skip"):
                return None
            return data
        except (json.JSONDecodeError, IndexError):
            return None

    async def handle_image(self, image_bytes: bytes, caption: str) -> dict:
        import asyncio
        category = detect_category(caption) if caption else None
        drops = get_drops(category=category, limit=30) if category else get_recent_drops(limit=20)
        context = self._drops_block(drops, "WHAT YOUVE SHARED SO FAR:")

        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

        content = []
        if context:
            content.append({"type": "text", "text": f"[Context]\n{context}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}})
        content.append({
            "type": "text",
            "text": caption or "What's in this image? Extract anything relevant to our wedding planning.",
        })

        # Run main response + payment extraction in parallel
        main_call = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": content}],
        )
        payment_call = self._extract_payment(image_bytes, caption)

        response, payment = await asyncio.gather(main_call, payment_call)

        suffix = ""
        if payment:
            add_payment(payment)
            status_label = {"paid": "paid", "deposit": "deposit paid", "owing": "still owed", "quote": "quoted"}.get(payment.get("status", ""), payment.get("status", ""))
            currency = payment.get("currency", "")
            amount = payment.get("amount", "")
            vendor = payment.get("vendor", "")
            paid_by = payment.get("paid_by")
            by_str = f" by {paid_by}" if paid_by else ""
            suffix = f"\n\n💰 Logged: {currency} {amount:,} {status_label}{by_str} — {vendor}"

        return {
            "text": response.content[0].text + suffix,
            "detected_category": category or "budget",
        }

    async def category_status(self, category: str) -> str:
        cat = CATEGORIES[category]
        drops = get_drops(category=category, limit=60)
        decisions = get_all_memory().get(category, {}).get("decisions", [])
        docs = fetch_docs_for_category(category)

        parts = []

        # Budget category gets a financial summary up front
        if category == "budget":
            fin = payment_summary()
            if fin["payments"]:
                lines = ["PAYMENTS LOGGED:"]
                for p in fin["payments"]:
                    status_label = {"paid": "paid", "deposit": "deposit", "owing": "owing", "quote": "quote"}.get(p.get("status", ""), p.get("status", ""))
                    cur = p.get("currency", "")
                    amt = p.get("amount", 0)
                    by = f" — {p['paid_by']}" if p.get("paid_by") else ""
                    lines.append(f"  {p.get('vendor', 'unknown')}: {cur} {amt:,} ({status_label}){by}")
                lines.append(f"\nTotal paid/deposited: {list(fin['by_person'].items())[0][1] if fin['by_person'] else 0:,}")
                for person, amt in fin["by_person"].items():
                    lines.append(f"  {person}: {amt:,}")
                if fin["total_owing"]:
                    lines.append(f"Still owing: {fin['total_owing']:,}")
                parts.append("\n".join(lines))

        if drops:
            parts.append(self._drops_block(drops, "DROPS:"))
        if decisions:
            parts.append("LOCKED DECISIONS:\n" + "\n".join(f"• {d}" for d in decisions))
        if docs:
            parts.append(f"LINKED DOCS:\n{docs}")

        if not parts:
            return f"{cat['emoji']} Nothing dropped for {cat['name']} yet.\n\nJust start talking about it — I'll pick it up."

        context = "\n\n".join(parts)
        prompt = f"""{context}

Give me a status brief for {cat['name']}. Structure it like this:

WHATS CONFIRMED
Anything that looks like a firm decision or booking.

WHATS BEING CONSIDERED
Options discussed, quotes seen, things in the running.

STILL OPEN
Key decisions not made yet for this area.

NEXT STEP
One concrete thing to do next.

Tight and plain. No fluff."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return f"{cat['emoji']} {cat['name'].upper()}\n\n{response.content[0].text}"

    async def bring_me_up_to_speed(self) -> str:
        all_drops = get_recent_drops(limit=100)
        all_decisions = get_all_memory()

        parts = []

        if all_drops:
            lines = []
            for d in all_drops:
                ts = d["ts"][:10]
                icon = "📸" if d["kind"] == "image" else "💬"
                cat_tag = f"[{d.get('category', 'general')}] "
                lines.append(f"{icon} {ts} {cat_tag}{d['content']}")
            parts.append("ALL DROPS:\n" + "\n".join(lines))

        locked = []
        for cat_key, data in all_decisions.items():
            for dec in data.get("decisions", []):
                cat_name = CATEGORIES.get(cat_key, {}).get("name", cat_key)
                locked.append(f"[{cat_name}] {dec}")
        if locked:
            parts.append("LOCKED DECISIONS:\n" + "\n".join(locked))

        if not parts:
            return "Nothing dropped yet. Just start talking — about venue, budget, guests, anything — and I'll start building the picture."

        context = "\n\n".join(parts)
        prompt = f"""{context}

Give a catch-up brief across all wedding planning. Structure it like this:

WHATS BEEN SORTED
Categories with real progress or confirmed decisions.

WHATS IN MOTION
Things discussed or being considered but not locked in.

WHATS UNTOUCHED
Wedding categories with nothing dropped yet.

ONE THING TO DO NEXT
The single most useful next action right now.

Keep it tight. Plain text, CAPS headers."""

        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=self._build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
