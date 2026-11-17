"""Read-only view into Ansen's Pocket (heypocketai.com) voice recordings.

Pocket exposes a remote MCP server (streamable HTTP, bearer key). We speak the
tiny slice of the protocol we need (initialize -> tools/call) with httpx, so
there is no MCP SDK dependency. Needs POCKET_API_KEY (the full Authorization
header value, e.g. "Bearer pk_...").

Privacy: the account holds Ansen's work calls and private recordings, so the
agent only exposes this to Ansen (user_id check in the search_pocket branch
of agent.py's _execute_tool_inner).
Transcripts are trimmed hard before the model sees them."""
import json
import os

import httpx

POCKET_URL = os.getenv("POCKET_MCP_URL", "https://public.heypocketai.com/mcp")
_SNIPPET = 500        # chars of transcript text kept per hit
_MAX_HITS = 6


def available() -> bool:
    return bool(os.getenv("POCKET_API_KEY"))


def _headers(session: str | None = None) -> dict:
    h = {
        "Authorization": os.environ["POCKET_API_KEY"],
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        h["Mcp-Session-Id"] = session
    return h


def _parse(resp: httpx.Response) -> dict:
    """Responses come back as SSE ('event: message\\ndata: {...}') or plain JSON."""
    text = resp.text
    if "data:" in text:
        text = text.split("data:", 1)[1].strip()
    return json.loads(text)


def _call(tool: str, arguments: dict) -> dict:
    with httpx.Client(timeout=25) as c:
        init = c.post(POCKET_URL, headers=_headers(), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "wedding-agent", "version": "1"}}})
        init.raise_for_status()
        sid = init.headers.get("Mcp-Session-Id")
        c.post(POCKET_URL, headers=_headers(sid),
               json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = c.post(POCKET_URL, headers=_headers(sid), json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}})
        r.raise_for_status()
        body = _parse(r)
    if "error" in body:
        raise RuntimeError(body["error"].get("message", "pocket error"))
    out = json.loads(body["result"]["content"][0]["text"])
    if not out.get("success", True):
        raise RuntimeError(str(out)[:200])
    return out.get("data", out)


def search_recordings(query: str | None = None, after: str | None = None,
                      before: str | None = None) -> dict:
    """Query mode: best-matching sections. No query: the latest recordings."""
    args: dict = {}
    if query:
        args["query"] = query
    if after:
        args["recordingDateAfter"] = after
    if before:
        args["recordingDateBefore"] = before
    data = _call("search_pocket_conversations", args)
    hits = []
    for r in (data.get("recordings") or [])[:_MAX_HITS]:
        hits.append({
            "title": r.get("recordingTitle"),
            "recorded": r.get("recordingDateLocal") or r.get("recordingDate"),
            "speakers": r.get("speakers"),
            "section": r.get("sectionTitle"),
            "summary": r.get("sectionSummary"),
            "excerpt": (r.get("content") or r.get("transcript") or "")[:_SNIPPET],
        })
    return {"recordings": hits}


def search_action_items(query: str | None = None, status: str | None = None,
                        due_before: str | None = None) -> dict:
    args: dict = {}
    if query:
        args["query"] = query
    if status:
        args["status"] = status
    if due_before:
        args["dueBefore"] = due_before
    data = _call("search_pocket_actionitems", args)
    items = [{
        "label": a.get("label"),
        "status": a.get("status"),
        "due": a.get("dueDate"),
        "priority": a.get("priority"),
        "from_recording": a.get("recordingTitle"),
        "recorded": a.get("recordingDateLocal"),
        "context": (a.get("context") or "")[:200],
    } for a in (data.get("actions") or [])[:20]]
    return {"action_items": items}
