from tools.db import get_client


def get_category_memory(category: str) -> dict:
    rows = (
        get_client().table("wedding_memory")
        .select("*")
        .eq("category", category)
        .execute()
        .data or []
    )
    result: dict = {"notes": [], "decisions": [], "docs": []}
    for row in rows:
        result.setdefault(row["field"], []).append(row["value"])
    return result


def get_all_memory() -> dict:
    rows = get_client().table("wedding_memory").select("*").execute().data or []
    result: dict = {}
    for row in rows:
        cat = row["category"]
        if cat not in result:
            result[cat] = {"notes": [], "decisions": [], "docs": []}
        result[cat].setdefault(row["field"], []).append(row["value"])
    return result


def save_to_category(category: str, field: str, value: str):
    get_client().table("wedding_memory").insert({
        "category": category,
        "field": field,
        "value": value,
    }).execute()


def delete_from_category(category: str, field: str, index: int):
    rows = (
        get_client().table("wedding_memory")
        .select("id")
        .eq("category", category)
        .eq("field", field)
        .execute()
        .data or []
    )
    if 0 <= index < len(rows):
        get_client().table("wedding_memory").delete().eq("id", rows[index]["id"]).execute()
        return True
    return False


def link_doc_to_category(category: str, doc_id: str):
    existing = (
        get_client().table("wedding_memory")
        .select("id")
        .eq("category", category)
        .eq("field", "docs")
        .eq("value", doc_id)
        .execute()
        .data or []
    )
    if not existing:
        save_to_category(category, "docs", doc_id)
