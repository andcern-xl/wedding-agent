import os
import re
import base64
import logging
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

_log = logging.getLogger(__name__)

# ── Whitelist ─────────────────────────────────────────────────────────────────
# The ONLY senders this module will ever touch. Enforced at three layers:
#   1. Query construction   — from: clause always restricted to this list
#   2. Post-fetch validation — any email whose From header isn't here is dropped
#   3. Scope               — OAuth token is gmail.readonly; Google rejects writes
ALLOWED_SENDERS = {
    "newsletter@mail.milkroad.com",
    "notifications@milkroad.com",
    "macronewsletter@macro.milkroad.com",
    "newsletter@ai.milkroad.com",
    "zendaily@substack.com",
    "dan@tldrnewsletter.com",
    "notboring@substack.com",
    "weeklywizdom@weeklywizdom.com",
    "newsletter@mail.coinbase.com",
}

# ── OAuth scopes ───────────────────────────────────────────────────────────────
# gmail.readonly is the narrowest Gmail scope Google offers.
# It grants: read messages/threads/labels/drafts/settings.
# It explicitly forbids: send, modify, delete, insert, import.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    GMAIL_SCOPE,
]

# ── Allowed API call surface ───────────────────────────────────────────────────
# Only these two Gmail resource+method combinations are permitted in this module.
# Any other call is a programming error and raises immediately.
_ALLOWED_CALLS = {
    ("messages", "list"),
    ("messages", "get"),
}

_creds = None
_service = None


# ── Service wrapper ────────────────────────────────────────────────────────────

class _GuardedGmail:
    """Thin wrapper around the Gmail API service.

    Permits only the two read-only calls in _ALLOWED_CALLS.
    Any attempt to call a write method (send, modify, delete, insert, trash,
    untrash, import, batchModify, batchDelete) raises PermissionError before
    the request is even constructed.
    """

    _WRITE_METHODS = {
        "send", "modify", "delete", "insert", "import",
        "trash", "untrash", "batchModify", "batchDelete",
        "create", "update", "patch",
    }

    def __init__(self, svc):
        self._svc = svc

    def messages(self):
        return _GuardedMessages(self._svc.users().messages(), self)

    def _assert_allowed(self, resource: str, method: str):
        if method in self._WRITE_METHODS:
            raise PermissionError(
                f"Gmail write operation blocked: {resource}.{method}(). "
                "This module is read-only."
            )
        if (resource, method) not in _ALLOWED_CALLS:
            raise PermissionError(
                f"Gmail call not in allowlist: {resource}.{method}(). "
                f"Permitted: {_ALLOWED_CALLS}"
            )


class _GuardedMessages:
    def __init__(self, resource, guard: _GuardedGmail):
        self._resource = resource
        self._guard = guard

    def list(self, **kwargs):
        self._guard._assert_allowed("messages", "list")
        # Enforce whitelist is always part of the query
        q = kwargs.get("q", "")
        if not any(s in q for s in ALLOWED_SENDERS):
            raise PermissionError(
                "Gmail list() called without a whitelisted sender in query. "
                "This is a bug — use _safe_query() to build queries."
            )
        return self._resource.list(**kwargs)

    def get(self, **kwargs):
        self._guard._assert_allowed("messages", "get")
        return self._resource.get(**kwargs)


# ── Credentials / service ──────────────────────────────────────────────────────

def _get_service() -> _GuardedGmail:
    global _creds, _service
    if _creds is None:
        _creds = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
    if not _creds.valid:
        _creds.refresh(Request())
        _service = None
    if _service is None:
        raw = build("gmail", "v1", credentials=_creds, cache_discovery=False)
        _service = _GuardedGmail(raw)
    return _service


# ── Query builder ──────────────────────────────────────────────────────────────

def _safe_query(sender_hint: str | None = None) -> str:
    """Build a Gmail search query restricted to whitelisted senders.

    sender_hint: optional substring to filter within the whitelist
                 (e.g. "milkroad" → only milkroad senders).
                 None → all whitelisted senders.
    Raises ValueError if hint matches nothing in the whitelist.
    """
    if sender_hint:
        hint = sender_hint.lower()
        matched = {s for s in ALLOWED_SENDERS if hint in s.lower()}
        if not matched:
            raise ValueError(
                f"'{sender_hint}' matches no whitelisted sender.\n"
                f"Allowed: {sorted(ALLOWED_SENDERS)}"
            )
        senders = matched
    else:
        senders = ALLOWED_SENDERS

    from_clause = " OR ".join(f"from:{s}" for s in sorted(senders))
    return f"({from_clause})"


# ── Sender validation ──────────────────────────────────────────────────────────

def _extract_email(from_header: str) -> str:
    """Pull bare email address out of a From header like 'Name <addr@x.com>'."""
    m = re.search(r"<([^>]+)>", from_header)
    return (m.group(1) if m else from_header).strip().lower()


def _assert_sender_allowed(from_header: str):
    """Raise if the actual sender of a fetched email is not whitelisted."""
    addr = _extract_email(from_header)
    if addr not in {s.lower() for s in ALLOWED_SENDERS}:
        raise PermissionError(
            f"Fetched email from non-whitelisted sender '{addr}'. "
            "Dropping — this should never happen."
        )


# ── Public API ─────────────────────────────────────────────────────────────────

def get_emails(sender_hint: str | None = None, max_results: int = 5) -> list[dict]:
    """Fetch recent emails from whitelisted senders.

    sender_hint: optional filter within the whitelist (e.g. "milkroad").
    max_results: max emails to return (capped at 20).
    """
    max_results = min(max_results, 20)  # hard cap
    svc = _get_service()
    query = _safe_query(sender_hint)

    result = svc.messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()

    messages = result.get("messages", [])
    emails = []

    for msg in messages:
        detail = svc.messages().get(
            userId="me",
            id=msg["id"],
            format="full",
        ).execute()

        headers = {h["name"]: h["value"] for h in detail["payload"].get("headers", [])}
        sender = headers.get("From", "")

        # Layer 2 guard: verify the actual From header before returning
        try:
            _assert_sender_allowed(sender)
        except PermissionError as e:
            _log.error(str(e))
            continue  # skip this email silently

        subject = headers.get("Subject", "(no subject)")
        date = headers.get("Date", "")
        body = _extract_body(detail["payload"])

        emails.append({
            "id": msg["id"],
            "subject": subject,
            "from": sender,
            "date": date,
            "body": body[:8000],  # cap per email
        })

    return emails


# ── Body extraction ────────────────────────────────────────────────────────────

def _extract_body(payload: dict) -> str:
    """Recursively extract plain text from a Gmail message payload."""
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            # Remove scripts, styles, and their content first
            raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
            # Replace block-level tags with newlines to preserve structure
            raw = re.sub(r"<(br|p|div|li|h[1-6]|tr)[^>]*>", "\n", raw, flags=re.IGNORECASE)
            # Strip remaining tags
            raw = re.sub(r"<[^>]+>", "", raw)
            # Collapse whitespace but preserve line breaks
            raw = re.sub(r"[ \t]+", " ", raw)
            raw = re.sub(r"\n\s*\n+", "\n\n", raw)
            return raw.strip()

    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text.strip():
            return text

    return ""
