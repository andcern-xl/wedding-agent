import json
from pathlib import Path

MEMORY_FILE = Path(__file__).parent.parent / "data" / "memory.json"


def _load() -> dict:
    MEMORY_FILE.parent.mkdir(exist_ok=True)
    if not MEMORY_FILE.exists():
        return {}
    with open(MEMORY_FILE) as f:
        return json.load(f)


def _save(data: dict):
    MEMORY_FILE.parent.mkdir(exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_category_memory(category: str) -> dict:
    data = _load()
    return data.get(category, {"notes": [], "decisions": [], "docs": []})


def get_all_memory() -> dict:
    return _load()


def save_to_category(category: str, field: str, value: str):
    data = _load()
    if category not in data:
        data[category] = {"notes": [], "decisions": [], "docs": []}
    data[category].setdefault(field, []).append(value)
    _save(data)


def delete_from_category(category: str, field: str, index: int):
    data = _load()
    if category in data and field in data[category]:
        items = data[category][field]
        if 0 <= index < len(items):
            items.pop(index)
            _save(data)
            return True
    return False


def link_doc_to_category(category: str, doc_id: str):
    data = _load()
    if category not in data:
        data[category] = {"notes": [], "decisions": [], "docs": []}
    docs = data[category].setdefault("docs", [])
    if doc_id not in docs:
        docs.append(doc_id)
        _save(data)
