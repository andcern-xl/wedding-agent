"""Google Drive — read-only, and ONLY inside folders on an inclusion list.

Ansen, 29 Sep 2026: "drive access - but only specific to 1 folder (so an
inclusion list rather than crawl the whole thing)". The OAuth scope Google
offers for reading existing files (drive.readonly) covers the whole Drive, so
the boundary is enforced here, in code, three ways:

1. There is no global search. Files are only ever listed by
   `'<folder>' in parents`, starting from an included folder and recursing
   into its subfolders. `_call` refuses a files.list whose query does not
   name a folder already known to be in scope.
2. Anything opened by id (a pasted link) is checked first: `in_scope()` walks
   its parents up to an included folder, or it is refused.
3. Every API call goes through `_call`, which allows only list/get/export —
   the same pattern as tools/gmail.py.

The inclusion list is DRIVE_FOLDER_IDS (comma-separated, Railway env) plus any
added from Telegram with /drive add, stored in loop_state so no deploy is
needed to change it. Everything included is SHARED between them.

OWNERSHIP — Ansen, same day: "neither jess and i can read any of each other's
private drive unless we explicitly give you the drive link". The bot reads
through ANSEN's Google account, so without a rule Jess (or anyone holding her
Telegram) could paste a link to one of his private docs and the bot would open
it. `may_give()` decides who may hand an item over, by its Drive OWNER:
  - Ansen: items he owns, or a third party's shared with him — never Jess's.
  - Jess: only items she owns (shared with Ansen's account).
Jess's Google address comes from GOOGLE_EMAIL_JESS; until it is set the check
fails closed — Jess can give nothing, and Ansen only what he owns, because a
folder owned by "someone else" can't be told apart from one owned by Jess.
The refusal never names the item, so a probe learns nothing.
"""
import io
import logging
import os
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

# Its own credentials with ONLY the Drive scope. Refreshing with a subset of the
# granted scopes is allowed; adding drive.readonly to gcal/gmail SCOPES instead
# would make their refresh fail with invalid_scope until the re-auth ran.
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

_ALLOWED_CALLS = {("files", "list"), ("files", "get"), ("files", "export"),
                  ("files", "get_media"), ("about", "get")}

ANSEN_TG, JESS_TG = 63756531, 6927468999
NAMES = {ANSEN_TG: "Ansen", JESS_TG: "Jess"}

FOLDER_MIME = "application/vnd.google-apps.folder"
_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    # xlsx, not csv: a csv export returns only the FIRST tab of a sheet.
    "application/vnd.google-apps.spreadsheet":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_DOWNLOADS = {"application/pdf", "text/plain", "text/markdown", "text/csv",
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
SUPPORTED = set(_EXPORTS) | _DOWNLOADS

MAX_FILES = 500
MAX_CHARS = 60000
_LS_FOLDERS = "drive_folders"

_creds = None
_service = None
_scope_ids: set = set()   # folder ids proven in scope by traversal this process


class OutOfScope(PermissionError):
    pass


def _get_service():
    global _creds, _service
    if _creds is None:
        _creds = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=[DRIVE_SCOPE],
        )
    if not _creds.valid:
        _creds.refresh(Request())
        _service = None
    if _service is None:
        _service = build("drive", "v3", credentials=_creds, cache_discovery=False)
    return _service


def _call(resource: str, method: str, **kwargs):
    if (resource, method) not in _ALLOWED_CALLS:
        raise PermissionError(f"gdrive: {resource}.{method} is not an allowed call")
    if method == "list":
        m = re.match(r"^'([A-Za-z0-9_-]+)' in parents\b", kwargs.get("q") or "")
        if not m or m.group(1) not in _scope_ids:
            raise OutOfScope("gdrive: files.list must be scoped to an included folder")
    svc = _get_service()
    return getattr(svc.about() if resource == "about" else svc.files(), method)(**kwargs)


# ── inclusion list ─────────────────────────────────────────────────────────────

def folder_id_from(link_or_id: str) -> str | None:
    s = (link_or_id or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", s) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9_-]{10,}", s) else None


def file_id_from(link_or_id: str) -> str | None:
    s = (link_or_id or "").strip()
    m = re.search(r"/d/([A-Za-z0-9_-]{10,})", s) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9_-]{10,}", s) else None


_account_email = None


def account_email() -> str:
    """The Google account the bot reads through (Ansen's)."""
    global _account_email
    if _account_email is None:
        _account_email = (_call("about", "get", fields="user(emailAddress)").execute()
                          .get("user", {}).get("emailAddress") or "").lower()
    return _account_email


def jess_email() -> str:
    return os.getenv("GOOGLE_EMAIL_JESS", "").strip().lower()


def owner_of(meta: dict) -> str:
    owners = meta.get("owners") or []
    return (owners[0].get("emailAddress") or "").lower() if owners else ""


def may_give(meta: dict, user_id: int) -> tuple[bool, str]:
    """May this person hand this Drive item to the bot? See module docstring.

    Items in a Google SHARED DRIVE have no individual owner — the drive owns
    them — so they are nobody's private Drive and either of them may give one.
    That is the setup they're starting with (29 Sep 2026: "for now, we just do
    shared drives with information").
    """
    if user_id not in (ANSEN_TG, JESS_TG):
        return False, "Not allowed."
    if meta.get("driveId"):
        return True, ""
    owner = owner_of(meta)
    ansen, jess = account_email(), jess_email()
    if user_id == JESS_TG:
        if not jess:
            return False, "Jess's Google address isn't set up yet (GOOGLE_EMAIL_JESS), so I can't confirm it's hers."
        if owner == jess:
            return True, ""
        return False, "I can only take Drive items Jess owns from Jess. If it's Ansen's, he has to share it."
    if user_id == ANSEN_TG:
        if owner and owner == ansen:
            return True, ""
        if not jess:
            return False, ("I can only take items you own until Jess's Google address is set "
                           "(GOOGLE_EMAIL_JESS), since I can't tell a third party's from hers.")
        if owner == jess:
            return False, "That belongs to Jess — she has to give me the link herself."
        return True, ""
    return False, "Not allowed."


def _records() -> list[dict]:
    """Included folders: [{id, name, added_by, added}] — env ones first."""
    out = [{"id": x.strip(), "name": None, "added_by": ANSEN_TG, "added": "env", "env": True}
           for x in os.getenv("DRIVE_FOLDER_IDS", "").split(",") if x.strip()]
    try:
        import json
        from tools.loop_state import load_state, COUPLE
        raw = (load_state(_LS_FOLDERS, COUPLE) or {}).get("last_output") or ""
        extra = json.loads(raw) if raw.strip().startswith("[") else [
            {"id": x.strip(), "name": None, "added_by": ANSEN_TG, "added": "?"}
            for x in raw.split(",") if x.strip()]
    except Exception:
        extra = []
    seen = {r["id"] for r in out}
    for r in extra:
        if isinstance(r, dict) and r.get("id") and r["id"] not in seen:
            out.append(r)
            seen.add(r["id"])
    return out


def folder_records() -> list[dict]:
    return _records()


def included_folders() -> list[str]:
    return [r["id"] for r in _records()]


def _save_records(records: list[dict]) -> None:
    import json
    from tools.loop_state import save_state, COUPLE
    from tools.tz import local_today
    save_state(_LS_FOLDERS, COUPLE, json.dumps([r for r in records if not r.get("env")]),
               local_today().isoformat(), 100000)


def add_folder(link_or_id: str, user_id: int) -> dict:
    fid = folder_id_from(link_or_id)
    if not fid:
        return {"ok": False, "error": "That doesn't look like a Drive folder link."}
    try:
        meta = _call("files", "get", fileId=fid, fields="id,name,mimeType,driveId,owners(emailAddress)",
                     supportsAllDrives=True).execute()
    except Exception:
        return {"ok": False, "error": "I can't open that folder. Is it shared with Ansen's Google account?"}
    ok, why = may_give(meta, user_id)
    if not ok:
        return {"ok": False, "error": why}
    if meta.get("mimeType") != FOLDER_MIME:
        return {"ok": False, "error": "That's a file, not a folder — paste it in chat to have me read it."}
    recs = _records()
    if fid not in {r["id"] for r in recs}:
        from tools.tz import local_today
        recs.append({"id": fid, "name": meta.get("name"), "added_by": user_id,
                     "added": local_today().isoformat()})
        _save_records(recs)
    return {"ok": True, "id": fid, "name": meta.get("name")}


def remove_folder(fid: str, user_id: int) -> tuple[bool, str]:
    """Either of them may stop the bot reading a folder — removing access is
    always safe. Env-configured folders have to be removed in Railway."""
    recs = _records()
    rec = next((r for r in recs if r["id"] == fid), None)
    if not rec:
        return False, "Not included."
    if rec.get("env"):
        return False, "That one is set in Railway (DRIVE_FOLDER_IDS) — remove it there."
    _save_records([r for r in recs if r["id"] != fid])
    _scope_ids.discard(fid)
    return True, rec.get("name") or fid


# ── listing (in-scope only) ────────────────────────────────────────────────────

def list_files() -> tuple[list[dict], dict]:
    """Every file under the included folders, recursing into subfolders.

    Returns (files, info). info has `folders` (id→name of each included root),
    `truncated` (count beyond MAX_FILES — logged, never silent) and `errors`.
    """
    files, info = [], {"folders": {}, "truncated": 0, "errors": []}
    queue = []
    for root in included_folders():
        try:
            meta = _call("files", "get", fileId=root, fields="id,name,mimeType",
                         supportsAllDrives=True).execute()
        except Exception as e:
            info["errors"].append(f"{root}: {str(e)[:80]}")
            continue
        if meta.get("mimeType") != FOLDER_MIME:
            info["errors"].append(f"{root}: not a folder")
            continue
        info["folders"][root] = meta.get("name")
        _scope_ids.add(root)
        queue.append((root, meta.get("name") or "", root))
    seen = set()
    while queue:
        fid, path, root = queue.pop(0)
        if fid in seen:
            continue
        seen.add(fid)
        token = None
        while True:
            resp = _call("files", "list",
                         q=f"'{fid}' in parents and trashed = false",
                         fields="nextPageToken, files(id,name,mimeType,modifiedTime,webViewLink)",
                         pageSize=200, pageToken=token, corpora="allDrives",
                         supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            for f in resp.get("files", []):
                if f.get("mimeType") == FOLDER_MIME:
                    _scope_ids.add(f["id"])
                    queue.append((f["id"], f"{path}/{f.get('name')}", root))
                elif len(files) < MAX_FILES:
                    files.append({**f, "path": path, "root": root})
                else:
                    info["truncated"] += 1
            token = resp.get("nextPageToken")
            if not token:
                break
    if info["truncated"]:
        log.warning("gdrive.list_files: %d files beyond MAX_FILES=%d not listed",
                    info["truncated"], MAX_FILES)
    return files, info


def in_scope(file_id: str, _hops: int = 12) -> bool:
    """Is this file inside an included folder? Walks parents; never guesses."""
    roots = set(included_folders())
    cur = file_id
    for _ in range(_hops):
        if cur in roots or cur in _scope_ids:
            return True
        try:
            meta = _call("files", "get", fileId=cur, fields="parents",
                         supportsAllDrives=True).execute()
        except Exception:
            return False
        parents = meta.get("parents") or []
        if not parents:
            return False
        cur = parents[0]
    return False


# ── reading ────────────────────────────────────────────────────────────────────

def _download(request) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def _xlsx_text(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"## Sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).strip() for c in row]
            if any(cells):
                out.append(" | ".join(cells).rstrip(" |"))
    return "\n".join(out)


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def effective_mime(f: dict) -> str:
    """Uploads sometimes arrive as application/octet-stream; trust the extension
    for the types we can read, so "Portfolio (2).pdf" isn't silently skipped."""
    mime = f.get("mimeType") or ""
    if mime == "application/octet-stream":
        name = (f.get("name") or "").lower()
        for ext, m in ((".pdf", "application/pdf"), (".csv", "text/csv"), (".txt", "text/plain"),
                       (".md", "text/markdown"),
                       (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")):
            if name.endswith(ext):
                return m
    return mime


def is_supported(f: dict) -> bool:
    return effective_mime(f) in SUPPORTED


def read_file(file: dict | str, user_id: int | None = None) -> dict:
    """{id, name, text, truncated} for a file the bot may read, or {error}.

    A dict comes from list_files (already in scope). A string is a pasted link:
    read if it sits in an included folder, or — one-off — if `user_id` may give
    it (may_give). Otherwise OutOfScope, without naming the file.
    """
    fields = "id,name,mimeType,modifiedTime,webViewLink,driveId,owners(emailAddress)"
    if isinstance(file, str):
        fid = file_id_from(file) or file
        try:
            meta = _call("files", "get", fileId=fid, fields=fields, supportsAllDrives=True).execute()
        except Exception:
            raise OutOfScope("I can't open that link. Is it shared with Ansen's Google account?")
        if not in_scope(fid):
            ok, why = may_give(meta, user_id) if user_id is not None else (False, "")
            if not ok:
                raise OutOfScope(why or "That file isn't inside an included Drive folder.")
        file = meta
    elif not in_scope(file["id"]):
        raise OutOfScope("That file isn't inside an included Drive folder.")
    mime = effective_mime(file)
    try:
        if mime in _EXPORTS:
            data = _download(_call("files", "export", fileId=file["id"], mimeType=_EXPORTS[mime]))
            text = _xlsx_text(data) if _EXPORTS[mime].endswith("sheet") else data.decode("utf-8", "replace")
        elif mime in _DOWNLOADS:
            data = _download(_call("files", "get_media", fileId=file["id"], supportsAllDrives=True))
            if mime == "application/pdf":
                text = _pdf_text(data)
            elif mime.endswith("sheet"):
                text = _xlsx_text(data)
            else:
                text = data.decode("utf-8", "replace")
        else:
            return {"id": file["id"], "name": file.get("name"), "error": f"unsupported type {mime}"}
    except OutOfScope:
        raise
    except Exception as e:
        log.exception("gdrive.read_file failed for %s", file.get("name"))
        return {"id": file["id"], "name": file.get("name"), "error": str(e)[:160]}
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    truncated = max(0, len(text) - MAX_CHARS)
    if truncated:
        log.warning("gdrive.read_file: %s truncated by %d chars", file.get("name"), truncated)
    return {"id": file["id"], "name": file.get("name"), "modified": file.get("modifiedTime"),
            "link": file.get("webViewLink"), "text": text[:MAX_CHARS], "truncated": truncated}


# ── reverse gear ───────────────────────────────────────────────────────────────

def forget_doc(file_id: str) -> int:
    """Retire every active vault fact learned from one Drive file.

    Facts are tagged source='drive:<file id>', so a doc that turns out to be
    wrong — or poisoned — can be pulled out in one tap. Superseded, not
    deleted; the ids are kept so undo_forget() restores exactly these.
    """
    import json
    from tools.db import get_client
    from tools.loop_state import save_state, COUPLE
    from tools.tz import local_today
    from tools.user_memory import supersede_entries
    rows = (get_client().table("brain_entries").select("id")
            .eq("source", f"drive:{file_id}").eq("status", "active").execute().data or [])
    ids = [r["id"] for r in rows]
    if ids:
        supersede_entries(ids)
        save_state(f"drive_forgotten:{file_id}", COUPLE, json.dumps(ids), local_today().isoformat(), 100000)
        log.warning("gdrive.forget_doc: retired %d facts from %s", len(ids), file_id)
    return len(ids)


def undo_forget(file_id: str) -> int:
    import json
    from tools.loop_state import load_state, COUPLE
    from tools.user_memory import restore_entries
    try:
        ids = json.loads((load_state(f"drive_forgotten:{file_id}", COUPLE) or {}).get("last_output") or "[]")
    except Exception:
        ids = []
    return restore_entries(ids) if ids else 0
