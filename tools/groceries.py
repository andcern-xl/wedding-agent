from tools.db import get_client


def get_active_lists() -> list[dict]:
    """All active grocery lists with their items."""
    client = get_client()
    lists = client.table("grocery_lists").select("*").eq("status", "active").order("created_at").execute().data or []
    for lst in lists:
        lst["items"] = (
            client.table("grocery_items")
            .select("*")
            .eq("list_id", lst["id"])
            .eq("done", False)
            .order("created_at")
            .execute().data or []
        )
    return lists


def get_list_by_name(name: str) -> dict | None:
    """Find an active list by name (case-insensitive substring match)."""
    lists = get_active_lists()
    name_lower = name.lower().strip()
    for lst in lists:
        if name_lower in lst["name"].lower():
            return lst
    return None


def get_or_create_list(name: str, created_by: int) -> dict:
    """Return matching active list or create it."""
    existing = get_list_by_name(name)
    if existing:
        return existing
    row = get_client().table("grocery_lists").insert({"name": name, "created_by": created_by, "status": "active"}).execute().data[0]
    row["items"] = []
    return row


def add_items(list_id: str, items: list[str], added_by: int) -> list[dict]:
    rows = [{"list_id": list_id, "item": item.strip(), "added_by": added_by} for item in items if item.strip()]
    if not rows:
        return []
    return get_client().table("grocery_items").insert(rows).execute().data or []


def remove_item_by_text(list_id: str, item_text: str) -> bool:
    """Remove the first item matching item_text (case-insensitive) from a list."""
    items = (
        get_client().table("grocery_items")
        .select("id,item")
        .eq("list_id", list_id)
        .eq("done", False)
        .execute().data or []
    )
    query = item_text.lower().strip()
    for it in items:
        if query in it["item"].lower():
            get_client().table("grocery_items").delete().eq("id", it["id"]).execute()
            return True
    return False


def check_off_item(list_id: str, item_text: str) -> bool:
    """Mark an item as done."""
    items = (
        get_client().table("grocery_items")
        .select("id,item")
        .eq("list_id", list_id)
        .eq("done", False)
        .execute().data or []
    )
    query = item_text.lower().strip()
    for it in items:
        if query in it["item"].lower():
            get_client().table("grocery_items").update({"done": True}).eq("id", it["id"]).execute()
            return True
    return False


def close_list(list_id: str) -> bool:
    result = get_client().table("grocery_lists").update({"status": "done"}).eq("id", list_id).execute()
    return bool(result.data)
