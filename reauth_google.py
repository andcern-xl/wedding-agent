"""
Run this once to get a fresh Google OAuth refresh token.
It opens a browser, you log in, and it prints the new GOOGLE_REFRESH_TOKEN to paste into Railway.
"""
from dotenv import load_dotenv
load_dotenv()

import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    # Drive read-only, for the master-docs sync. The scope covers the whole
    # Drive; tools/gdrive.py confines every call to the included folders.
    "https://www.googleapis.com/auth/drive.readonly",
]

client_id = os.environ["GOOGLE_CLIENT_ID"]
client_secret = os.environ["GOOGLE_CLIENT_SECRET"]

client_config = {
    "installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
# prompt=consent forces Google to issue a NEW refresh token carrying the added scope
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

print("\n✅ New refresh token:")
print(creds.refresh_token)
print("\nUpdate GOOGLE_REFRESH_TOKEN in Railway with this value.")
