import json
from datetime import datetime, timezone
from tools.db import get_client

_MAX_MESSAGES = 40


def load_history(chat_id: int) -> list:
    try:
        rows = (
            get_client()
            .table("conversation_history")
            .select("messages")
            .eq("chat_id", chat_id)
            .execute()
            .data
        )
        if rows and rows[0].get("messages"):
            msgs = rows[0]["messages"]
            # Supabase may return already-parsed list or a JSON string
            if isinstance(msgs, str):
                msgs = json.loads(msgs)
            return msgs[-_MAX_MESSAGES:]
        return []
    except Exception:
        return []


def save_history(chat_id: int, messages: list) -> None:
    try:
        trimmed = messages[-_MAX_MESSAGES:]
        get_client().table("conversation_history").upsert(
            {
                "chat_id": chat_id,
                "messages": trimmed,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="chat_id",
        ).execute()
    except Exception:
        pass
