import os
from supabase import create_client, Client

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        _client = create_client(url, key)
    return _client


def as_num(value, default: float | int = 0):
    """Coerce a database value to a number.

    `row.get("amount", 0)` looks safe and is not: the default applies only when
    the KEY IS ABSENT, and a Postgres column that exists holding NULL comes back
    as None. So `total += row.get("amount", 0)` raises
    "unsupported operand type(s) for +: 'int' and 'NoneType'" the moment one row
    has a null amount — which is what killed the wedding priority brief for its
    entire life (9 of 56 wedding_payments rows had a null amount, the oldest
    from 17 June 2026) and what surfaced as a [DEBUG] TypeError in chat when
    Ansen logged a photographer as free.

    Also absorbs the numeric strings the API hands back for numeric columns.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return default
