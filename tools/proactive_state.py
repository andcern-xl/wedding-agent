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
