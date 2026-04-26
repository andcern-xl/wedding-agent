import re
from pathlib import Path

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

from tools.memory import get_category_memory

SCOPES = [
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
TOKEN_FILE = Path.home() / ".wedding-agent" / "google_token.json"
CREDS_FILE = Path.home() / ".wedding-agent" / "credentials.json"


def extract_doc_id(url: str) -> str | None:
    match = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None


def _get_credentials():
    if not GOOGLE_AVAILABLE:
        return None

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                return None
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.parent.mkdir(exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds


def fetch_doc(doc_id: str) -> str | None:
    if not GOOGLE_AVAILABLE:
        return f"[Google Docs not configured — install google packages to enable]"

    creds = _get_credentials()
    if not creds:
        return f"[Google Docs not authenticated — run setup_google_auth.py]"

    try:
        service = build("docs", "v1", credentials=creds)
        doc = service.documents().get(documentId=doc_id).execute()

        lines = []
        for element in doc.get("body", {}).get("content", []):
            if "paragraph" in element:
                para_text = ""
                for pe in element["paragraph"].get("elements", []):
                    if "textRun" in pe:
                        para_text += pe["textRun"]["content"]
                if para_text.strip():
                    lines.append(para_text.rstrip())

        title = doc.get("title", doc_id)
        return f"[Doc: {title}]\n" + "\n".join(lines)
    except Exception as e:
        return f"[Could not fetch doc {doc_id}: {e}]"


def fetch_docs_for_category(category: str) -> str:
    memory = get_category_memory(category)
    doc_ids = memory.get("docs", [])
    if not doc_ids:
        return ""

    parts = [fetch_doc(doc_id) for doc_id in doc_ids if fetch_doc(doc_id)]
    return "\n\n".join(parts)


def setup_google_auth():
    """Run this once interactively to authenticate."""
    if not GOOGLE_AVAILABLE:
        print("Install: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
        return

    if not CREDS_FILE.exists():
        print(f"Place your OAuth credentials file at: {CREDS_FILE}")
        print("Get it from: console.cloud.google.com → APIs & Services → Credentials → OAuth 2.0 Client")
        return

    creds = _get_credentials()
    if creds:
        print("Google Docs authentication successful.")
    else:
        print("Authentication failed.")


if __name__ == "__main__":
    setup_google_auth()
