"""Daily learning nuggets — top posts from parenting subreddits so the nightly
wrap can teach, not just recap. First source: r/daddit.

Reddit blocks anonymous JSON (403) but serves RSS — with a strict per-IP rate
limit (429s after a few quick requests). This is a once-nightly background job,
so we space requests out and back off politely. If Railway's IP gets blocked
outright, the upgrade path is Reddit OAuth (personal-use script app)."""
import html as _html
import re
import time
import xml.etree.ElementTree as ET

import httpx

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_NS = {"a": "http://www.w3.org/2005/Atom"}
_PAUSE_BETWEEN_REQUESTS = 6   # seconds — stay under the anonymous rate limit
_RETRIES = 3


def _get_xml(url: str) -> ET.Element | None:
    for attempt in range(_RETRIES):
        try:
            r = httpx.get(url, headers=_UA, timeout=20, follow_redirects=True)
            if r.status_code == 200 and r.content.lstrip().startswith(b"<?xml"):
                return ET.fromstring(r.content)
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            return None
        except Exception:
            time.sleep(5)
    return None


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _html.unescape(s or ""))).strip()


def fetch_top_posts(subreddit: str = "daddit", window: str = "day", limit: int = 25) -> list[dict]:
    """Top posts in the window via RSS. Feed order = top ranking."""
    root = _get_xml(f"https://www.reddit.com/r/{subreddit}/top/.rss?t={window}&limit={limit}")
    if root is None:
        return []
    posts = []
    for e in root.findall("a:entry", _NS):
        pid = (e.findtext("a:id", "", _NS) or "").replace("t3_", "")
        link_el = e.find("a:link", _NS)
        text = _strip_html(e.findtext("a:content", "", _NS))
        # RSS content embeds "[link] [comments]" boilerplate — drop it
        text = text.replace("[link]", "").replace("[comments]", "").strip()
        posts.append({
            "id": pid,
            "title": _strip_html(e.findtext("a:title", "", _NS)),
            "selftext": text[:900],
            "has_text": len(text) > 120,   # proxy for a real text post
            "permalink": link_el.get("href") if link_el is not None else "",
        })
    return posts[:limit]


def fetch_top_comments(post_id: str, limit: int = 6) -> list[str]:
    """Top comments via the post's RSS feed — on r/daddit the wisdom lives here.
    Sleeps first: the caller loops over posts and the rate limit is per-IP."""
    time.sleep(_PAUSE_BETWEEN_REQUESTS)
    root = _get_xml(f"https://www.reddit.com/r/daddit/comments/{post_id}/.rss")
    if root is None:
        return []
    out = []
    for e in root.findall("a:entry", _NS):
        eid = e.findtext("a:id", "", _NS) or ""
        if eid.startswith("t3_"):   # the post itself
            continue
        body = _strip_html(e.findtext("a:content", "", _NS))
        if body and len(body) > 40:
            out.append(body[:700])
        if len(out) >= limit:
            break
    return out
