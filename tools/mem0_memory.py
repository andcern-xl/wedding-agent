import os
import logging

logger = logging.getLogger(__name__)

_client = None


def get_client():
    global _client
    if _client is None:
        key = os.getenv("MEM0_API_KEY")
        if not key:
            return None
        try:
            from mem0 import MemoryClient
            _client = MemoryClient(api_key=key)
        except Exception as e:
            logger.warning(f"mem0 client init failed: {e}")
            return None
    return _client


def add_exchange(user_msg: str, bot_reply: str, user_id: int) -> None:
    """Extract and store facts from a conversation turn. Call in a thread."""
    client = get_client()
    if not client:
        return
    try:
        client.add(
            [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": bot_reply},
            ],
            user_id=str(user_id),
        )
    except Exception as e:
        logger.debug(f"mem0 add_exchange failed: {e}")


def search_memories(query: str, user_id: int, limit: int = 6) -> str:
    """Return bullet-formatted relevant memories for this user, or empty string."""
    client = get_client()
    if not client:
        return ""
    try:
        results = client.search(query, user_id=str(user_id), limit=limit)
        items = (results or {}).get("results", [])
        if not items:
            return ""
        return "\n".join(f"• {r['memory']}" for r in items if r.get("memory"))
    except Exception as e:
        logger.debug(f"mem0 search failed: {e}")
        return ""


def get_all_memories(user_id: int) -> list[dict]:
    """All stored memories for a user — used by /memory command."""
    client = get_client()
    if not client:
        return []
    try:
        results = client.get_all(user_id=str(user_id))
        return (results or {}).get("results", [])
    except Exception:
        return []
