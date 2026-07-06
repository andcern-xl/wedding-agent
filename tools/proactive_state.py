from tools.db import get_client


def load_state(user_id: int) -> dict:
    """Return the last proactive state for this user, or {}."""
    try:
        rows = (
            get_client()
            .table("proactive_state")
            .select("last_output,last_run_date")
            .eq("user_id", user_id)
            .execute()
            .data or []
        )
        return rows[0] if rows else {}
    except Exception:
        return {}


def save_state(user_id: int, last_output: str, run_date: str) -> None:
    """Upsert the proactive output for this user."""
    try:
        get_client().table("proactive_state").upsert({
            "user_id": user_id,
            "last_output": last_output,
            "last_run_date": run_date,
        }).execute()
    except Exception:
        pass


def append_state(user_id: int, text: str, run_date: str, max_len: int = 6000) -> None:
    """Append a block (e.g. the morning brief) to last_output so the nightly
    check can dedup against everything already sent today, not just its own
    previous output. Keeps the most recent max_len chars."""
    try:
        prev = load_state(user_id)
        combined = ((prev.get("last_output") or "") + "\n\n" + text).strip()
        save_state(user_id, combined[-max_len:], run_date)
    except Exception:
        pass
