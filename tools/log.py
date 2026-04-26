import json
from datetime import datetime
from pathlib import Path

LOG_FILE = Path(__file__).parent.parent / "data" / "drops.jsonl"


def _ensure():
    LOG_FILE.parent.mkdir(exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.touch()


def drop(category: str | None, kind: str, content: str, user_id: int):
    _ensure()
    entry = {
        "ts": datetime.now().isoformat(),
        "user": user_id,
        "category": category,
        "kind": kind,       # "text" | "image"
        "content": content,
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def get_drops(category: str | None = None, limit: int = 60) -> list[dict]:
    _ensure()
    entries = []
    with open(LOG_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if category:
        entries = [e for e in entries if e.get("category") == category]

    return entries[-limit:]


def get_recent_drops(limit: int = 30) -> list[dict]:
    return get_drops(category=None, limit=limit)
