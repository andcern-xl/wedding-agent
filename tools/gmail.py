import os
import re
import base64
import logging
from html import unescape as _html_unescape
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from tools.tz import local_today

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

def _safe_query(sender_hint: str | None = None, days_back: int | None = None) -> str:
    """Build a Gmail search query restricted to whitelisted senders.

    sender_hint: optional substring to filter within the whitelist.
    days_back: if set, only fetch emails from the last N days.
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
    query = f"({from_clause})"

    if days_back:
        from datetime import date, timedelta
        cutoff = (local_today() - timedelta(days=days_back)).strftime("%Y/%m/%d")
        query += f" after:{cutoff}"

    return query


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

def get_emails(sender_hint: str | None = None, max_results: int = 5, days_back: int | None = None) -> list[dict]:
    """Fetch recent emails from whitelisted senders.

    sender_hint: optional filter within the whitelist (e.g. "milkroad").
    max_results: max emails to return (capped at 20).
    days_back: if set, only fetch emails from the last N days.
    """
    max_results = min(max_results, 20)  # hard cap
    svc = _get_service()
    query = _safe_query(sender_hint, days_back=days_back)

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
        raw_html = _extract_raw_html(detail["payload"])
        image_urls = extract_content_image_urls(raw_html) if raw_html else []
        article_urls = extract_article_urls(raw_html, sender) if raw_html else []

        emails.append({
            "id": msg["id"],
            "subject": subject,
            "from": sender,
            "date": date,
            "body": body[:30000],
            "image_urls": image_urls,    # content images → vision extraction
            "article_urls": article_urls,  # article links → fetch full text
        })

    return emails


# ── Body extraction ────────────────────────────────────────────────────────────

_SKIP_IMG_PATTERNS = re.compile(
    r"(logo|icon|avatar|pixel|track|transparent|spacer|badge|button|arrow|chevron)",
    re.IGNORECASE,
)


_NEWSLETTER_DOMAINS = {
    "milkroad.com", "beehiiv.com",
    "substack.com",
    "tldrnewsletter.com",
    "coinbase.com",
    "weeklywizdom.com",
}

_SKIP_URL_PATTERNS = re.compile(
    r"(unsubscribe|manage|preferences|optout|opt-out|"
    r"twitter\.com|x\.com|linkedin\.com|facebook\.com|instagram\.com|"
    r"apple\.com/app|play\.google\.com|"
    r"forward|referral|refer-a-friend|sponsor|advertise)",
    re.IGNORECASE,
)


def extract_content_image_urls(html: str) -> list[str]:
    """Return URLs of content images in a newsletter HTML email.
    Filters out tracking pixels, logos, icons, tiny images. Capped at 6.
    """
    urls = []
    seen = set()
    for m in re.finditer(r"<img\b[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        src_m = re.search(r'\bsrc=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if not src_m:
            continue
        src = src_m.group(1)
        if not src.startswith("https://") or src in seen:
            continue
        if _SKIP_IMG_PATTERNS.search(src):
            continue
        if re.search(r"(pixel|track|beacon|open)\.", src, re.IGNORECASE):
            continue
        w_m = re.search(r'\bwidth=["\']?(\d+)["\']?', tag, re.IGNORECASE)
        h_m = re.search(r'\bheight=["\']?(\d+)["\']?', tag, re.IGNORECASE)
        if w_m and int(w_m.group(1)) < 80:
            continue
        if h_m and int(h_m.group(1)) < 80:
            continue
        seen.add(src)
        urls.append(src)
        if len(urls) >= 6:
            break
    return urls


def extract_article_urls(html: str, from_addr: str) -> list[str]:
    """Return article/post URLs linked from a newsletter email.

    Only follows links to the sender's own domain (e.g. milkroad.com links
    in a Milkroad email). Skips unsubscribe, social, tracking links.
    Capped at 4 URLs.
    """
    # Determine sender domain
    sender_domain = ""
    for domain in _NEWSLETTER_DOMAINS:
        if domain in from_addr.lower():
            sender_domain = domain
            break
    if not sender_domain:
        return []

    urls = []
    seen = set()
    for m in re.finditer(r'<a\b[^>]*\bhref=["\']([^"\']+)["\']', html, re.IGNORECASE):
        href = m.group(1)
        if not href.startswith("https://"):
            continue
        if sender_domain not in href:
            continue
        if _SKIP_URL_PATTERNS.search(href):
            continue
        # Must look like an article path (has slug or /p/ or /issues/)
        path = href.split("?")[0]
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            continue
        if href in seen:
            continue
        seen.add(href)
        urls.append(href)
        if len(urls) >= 4:
            break
    return urls


def _clean_html(raw: str) -> str:
    """Convert HTML email body to clean readable text."""
    # Remove entire script/style/head blocks
    raw = re.sub(r"<(script|style|head)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    # Remove img tags entirely (use extract_content_image_urls() separately to get URLs)
    raw = re.sub(r"<img[^>]*>", " ", raw, flags=re.IGNORECASE)
    # Keep hyperlink visible text, drop URL
    raw = re.sub(r"<a\s[^>]*>(.*?)</a>", r" \1 ", raw, flags=re.DOTALL | re.IGNORECASE)
    # Block-level tags → newlines
    raw = re.sub(r"<(br|p|div|li|h[1-6]|tr|td|table|section|article)[^>]*>", "\n", raw, flags=re.IGNORECASE)
    # Strip all remaining tags
    raw = re.sub(r"<[^>]+>", "", raw)
    # Decode HTML entities
    raw = _html_unescape(raw)
    # Clean lines
    clean_lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r'^https?://\S+$', stripped):
            continue
        if re.match(r'^(view|read|open|unsubscribe|click here|update preferences|manage|copyright|all rights)', stripped, re.IGNORECASE):
            continue
        clean_lines.append(stripped)
    result = "\n".join(clean_lines)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _extract_body(payload: dict) -> str:
    """Recursively extract the best readable text from a Gmail message payload.

    For multipart/alternative (newsletters), prefers HTML over plain text
    because the plain text version is usually just 'View in browser...'
    """
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            return _clean_html(raw)

    parts = payload.get("parts", [])
    if not parts:
        return ""

    # For multipart/alternative, collect both plain and HTML, prefer whichever is richer
    if mime == "multipart/alternative":
        plain_text = ""
        html_text = ""
        for part in parts:
            part_mime = part.get("mimeType", "")
            if part_mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    plain_text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            elif part_mime == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                    html_text = _clean_html(raw)
        # Prefer HTML if it's substantially richer than plain text
        if len(html_text) > len(plain_text) * 1.5 or len(plain_text) < 200:
            return html_text or plain_text
        return plain_text or html_text

    # For other multipart types, recurse and return the richest part
    best = ""
    for part in parts:
        text = _extract_body(part)
        if len(text) > len(best):
            best = text
    return best


def _extract_raw_html(payload: dict) -> str:
    """Return raw HTML string from a Gmail message payload — no cleaning applied.
    Used to extract image URLs before they get stripped by _clean_html().
    """
    mime = payload.get("mimeType", "")
    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    parts = payload.get("parts", [])
    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        # Recurse into nested multipart
        if part_mime.startswith("multipart/"):
            result = _extract_raw_html(part)
            if result:
                return result
    return ""
