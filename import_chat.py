"""One-shot import of Telegram chat export into Supabase."""
import asyncio
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from anthropic import AsyncAnthropic
from tools.db import get_client
from categories import detect_category

EXPORT_DIR = Path("/Users/ansen/Downloads/Telegram Desktop/ChatExport_2026-04-26 (1)")
BOT_ID = "user8701477323"
USER_MAP = {"cas": 63756531, "j": 6927468999}

client = AsyncAnthropic()


def extract_text(msg: dict) -> str:
    text = msg.get("text", "")
    if isinstance(text, list):
        return "".join(t if isinstance(t, str) else t.get("text", "") for t in text).strip()
    return str(text).strip()


async def analyze_photo(photo_path: Path, caption: str = "") -> str:
    image_b64 = base64.standard_b64encode(photo_path.read_bytes()).decode()
    content = []
    if caption:
        content.append({"type": "text", "text": caption})
    content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}})
    content.append({"type": "text", "text": "Describe what's in this image in the context of wedding planning. Extract any specific names, prices, venues, menu items, dates, or other concrete details. Be thorough."})

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


async def main():
    with open(EXPORT_DIR / "result.json") as f:
        data = json.load(f)

    msgs = data["messages"]
    db = get_client()

    imported = 0
    for msg in msgs:
        if msg.get("from_id") == BOT_ID:
            continue
        if msg.get("type") != "message":
            continue

        sender = msg.get("from", "unknown")
        user_id = USER_MAP.get(sender, 0)
        ts = msg["date"] + "+00:00"

        photo_rel = msg.get("photo")
        text = extract_text(msg)

        if text.startswith("/"):
            continue

        if photo_rel:
            photo_path = EXPORT_DIR / photo_rel
            if not photo_path.exists():
                print(f"  SKIP missing photo: {photo_path}")
                continue
            caption = text
            print(f"  Analyzing photo {photo_rel}...")
            content = await analyze_photo(photo_path, caption)
            full_content = f"[screenshot] {caption + ' — ' if caption else ''}{content}"
            category = detect_category(caption) or "venue"
            db.table("wedding_drops").insert({
                "ts": ts, "user_id": user_id, "category": category,
                "kind": "image", "content": full_content,
            }).execute()
            print(f"  ✓ photo → [{category}]")
            imported += 1

        elif text:
            category = detect_category(text)
            db.table("wedding_drops").insert({
                "ts": ts, "user_id": user_id, "category": category,
                "kind": "text", "content": text,
            }).execute()
            print(f"  ✓ text → [{category}] {text[:60]}")
            imported += 1

    print(f"\nDone. {imported} items imported into Supabase.")


if __name__ == "__main__":
    asyncio.run(main())
