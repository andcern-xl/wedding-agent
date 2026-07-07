"""Per-loop delta state — every scheduled sender remembers what it already said.

One row per (loop_name, user_id) in the loop_state table. user_id=COUPLE (0)
for loops that broadcast one shared message to both users. Generalizes the old
proactive_state table (kept for one release as rollback).
"""
from tools.db import get_client

COUPLE = 0  # sentinel user_id for couple-wide broadcast loops


def load_state(loop_name: str, user_id: int) -> dict:
    """Return {last_output, last_run_date} for this loop+user, or {}."""
    try:
        rows = (
            get_client()
            .table("loop_state")
            .select("last_output,last_run_date")
            .eq("loop_name", loop_name)
            .eq("user_id", user_id)
            .execute()
            .data or []
        )
        return rows[0] if rows else {}
    except Exception:
        return {}


def save_state(loop_name: str, user_id: int, last_output: str, run_date: str,
               max_len: int = 6000) -> None:
    try:
        get_client().table("loop_state").upsert({
            "loop_name": loop_name,
            "user_id": user_id,
            "last_output": (last_output or "")[-max_len:],
            "last_run_date": run_date,
        }).execute()
    except Exception:
        pass


def append_state(loop_name: str, user_id: int, text: str, run_date: str,
                 max_len: int = 6000) -> None:
    """Append a block to this loop's last_output (keeps most recent max_len chars)."""
    try:
        prev = load_state(loop_name, user_id)
        combined = ((prev.get("last_output") or "") + "\n\n" + text).strip()
        save_state(loop_name, user_id, combined, run_date, max_len=max_len)
    except Exception:
        pass


def already_sent(user_id: int, loop_names: list[str], max_len: int = 8000) -> str:
    """Everything these loops recently told this user — for delta-only prompts.
    Falls back to the couple-wide row when a loop has no per-user row."""
    blocks = []
    for name in loop_names:
        state = load_state(name, user_id)
        if not state.get("last_output") and user_id != COUPLE:
            state = load_state(name, COUPLE)
        output = (state.get("last_output") or "").strip()
        if output:
            blocks.append(f"=== {name} ({state.get('last_run_date', '?')}) ===\n{output}")
    return "\n\n".join(blocks)[-max_len:]
