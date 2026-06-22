import os
from datetime import datetime, timezone, timedelta
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
]

_creds = None
_service = None


def _get_service():
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
        _service = None  # rebuild with fresh token
    if _service is None:
        _service = build("calendar", "v3", credentials=_creds, cache_discovery=False)
    return _service


def get_events(days_ahead: int = 7, max_results: int = 20) -> list[dict]:
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    result = (
        _get_service()
        .events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        )
        .execute()
    )
    events = []
    for e in result.get("items", []):
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        end_time = e["end"].get("dateTime", e["end"].get("date", ""))
        events.append({
            "id": e["id"],
            "title": e.get("summary", "(no title)"),
            "start": start,
            "end": end_time,
            "description": e.get("description", ""),
            "location": e.get("location", ""),
        })
    return events


def create_event(
    title: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
) -> dict:
    event = {
        "summary": title,
        "start": {"dateTime": start, "timeZone": os.getenv("REMINDER_TZ", "Asia/Singapore")},
        "end": {"dateTime": end, "timeZone": os.getenv("REMINDER_TZ", "Asia/Singapore")},
    }
    if description:
        event["description"] = description
    if location:
        event["location"] = location
    result = _get_service().events().insert(calendarId=CALENDAR_ID, body=event).execute()
    return {"id": result["id"], "title": title, "start": start, "end": end, "link": result.get("htmlLink", "")}


def delete_event(event_id: str) -> bool:
    try:
        _get_service().events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        return True
    except Exception:
        return False
