"""google-drive-mcp — FastMCP server for searching and reading files directly from Google Drive.

Exposes tools:
  - gdrive_list_files: Search/list files in Google Drive.
  - gdrive_read_file: Read file content directly, converting Google Docs, Sheets, Slides, and PDFs to Markdown/CSV text in-memory.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from markitdown import MarkItDown

# ── Path bootstrap ────────────────────────────────────────────────────────────

def _find_root() -> Path:
    env = os.environ.get("PROJECT_ROOT")
    if env: return Path(env).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "AGENTS.md").exists(): return parent
    raise RuntimeError(
        "Cannot locate project root. Set the PROJECT_ROOT environment variable."
    )

ROOT = _find_root()
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

DEFAULT_CLIENT_CONFIG = {
    "installed": {
        "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
        "project_id": "your-project-id",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "YOUR_CLIENT_SECRET",
        "redirect_uris": ["http://localhost"]
    }
}

# ── Authentication Helper ──────────────────────────────────────────────────────

def get_credentials():
    service_account_path = os.environ.get("GDRIVE_SERVICE_ACCOUNT_PATH") or str(
        ROOT / ".agents" / "mcp" / "gdrive" / "service_account.json"
    )
    if os.path.exists(service_account_path):
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_file(
            service_account_path, scopes=SCOPES
        )

    token_path = os.environ.get("GDRIVE_TOKEN_PATH") or str(
        ROOT / ".agents" / "mcp" / "gdrive" / "token.json"
    )

    from google.oauth2.credentials import Credentials
    creds = None

    # Load from .podarcis/config.yaml
    try:
        import yaml
        podarcis_path = ROOT / ".podarcis" / "config.yaml"
        if podarcis_path.exists():
            with open(podarcis_path) as f:
                pod_data = yaml.safe_load(f) or {}
            token_json = pod_data.get("gdrive", {}).get("token")
            if token_json:
                creds = Credentials.from_authorized_user_info(
                    json.loads(token_json), SCOPES)
    except Exception:
        pass

    # Fallback to token.json
    if not creds and os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            raise RuntimeError(
                f"Google Drive OAuth token is missing or expired. Run setup:\n"
                f"PROJECT_ROOT={ROOT} .venv/bin/python .agents/mcp/gdrive/server.py --auth"
            )
    return creds

# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "google-drive-mcp",
    instructions=(
        "Google Drive search and content reader. Read-only access to files in Google Drive. "
        "Allows listing/searching files and reading file contents directly in-memory "
        "(converting Google Docs/Sheets/Slides/PDFs to Markdown/CSV)."
    ),
)

def get_drive_service():
    creds = get_credentials()
    return build("drive", "v3", credentials=creds)

# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def gdrive_list_files(
    query: Annotated[str | None, "Search query (Google Drive q parameter format e.g. name contains 'design')"] = None,
    page_size: Annotated[int, "Number of results to return (max 100)"] = 20,
) -> str:
    """List or search files in Google Drive."""
    try:
        service = get_drive_service()
        q_parts = []
        if query:
            q_parts.append(query)
        q_parts.append("trashed = false")
        q = " and ".join(q_parts)

        results = service.files().list(
            q=q,
            pageSize=min(page_size, 100),
            fields="files(id, name, mimeType, modifiedTime, size)",
        ).execute()

        files = results.get("files", [])
        if not files:
            return "No files found."

        output = ["| Name | ID | Mime Type | Size (bytes) | Modified Time |", "|---|---|---|---|---|"]
        for f in files:
            size = f.get("size", "N/A")
            output.append(f"| {f['name']} | `{f['id']}` | {f['mimeType']} | {size} | {f['modifiedTime']} |")

        return "\n".join(output)
    except Exception as e:
        return f"Error listing files: {str(e)}"


@mcp.tool()
async def gdrive_read_file(
    file_id: Annotated[str, "Google Drive File ID"],
) -> str:
    """Read a file directly from Google Drive in-memory, converting Docs/Sheets/Slides/PDFs to Markdown/CSV text."""
    try:
        service = get_drive_service()
        meta = service.files().get(fileId=file_id, fields="id, name, mimeType").execute()
        file_name = meta["name"]
        mime_type = meta["mimeType"]

        if mime_type == "application/vnd.google-apps.folder":
            return f"Error: '{file_name}' is a folder. Use gdrive_list_files to list folder contents."

        # Handle Google Workspace documents
        if mime_type == "application/vnd.google-apps.document":
            request = service.files().export_media(
                fileId=file_id,
                mimeType="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            return _convert_media_to_markdown(request, suffix=".docx")

        elif mime_type == "application/vnd.google-apps.spreadsheet":
            request = service.files().export_media(fileId=file_id, mimeType="text/csv")
            return _download_text(request)

        elif mime_type == "application/vnd.google-apps.presentation":
            request = service.files().export_media(fileId=file_id, mimeType="application/pdf")
            return _convert_media_to_markdown(request, suffix=".pdf")

        # Binary/Standard Files
        request = service.files().get_media(fileId=file_id)
        if mime_type.startswith("text/") or file_name.endswith((".txt", ".csv", ".json", ".md", ".py", ".html", ".xml")):
            return _download_text(request)
        else:
            ext = Path(file_name).suffix or ".bin"
            return _convert_media_to_markdown(request, suffix=ext)

    except Exception as e:
        return f"Error reading file '{file_id}': {str(e)}"


# ── Internal Conversion Helpers ───────────────────────────────────────────────

def _download_text(request) -> str:
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue().decode("utf-8", errors="replace")


def _convert_media_to_markdown(request, suffix: str) -> str:
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(fh.getvalue())
        tmp_path = tmp.name

    try:
        md = MarkItDown()
        result = md.convert(tmp_path)
        return result.text_content
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ── Main / CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--auth" in sys.argv:
        from google_auth_oauthlib.flow import InstalledAppFlow
        credentials_path = os.environ.get("GDRIVE_CREDENTIALS_PATH") or str(
            ROOT / ".agents" / "mcp" / "gdrive" / "credentials.json"
        )
        token_path = os.environ.get("GDRIVE_TOKEN_PATH") or str(
            ROOT / ".agents" / "mcp" / "gdrive" / "token.json"
        )

        if not os.path.exists(credentials_path):
            print("Credentials file not found. Using default application credentials...")
            flow = InstalledAppFlow.from_client_config(DEFAULT_CLIENT_CONFIG, SCOPES)
        else:
            print("Using user-provided credentials.json...")
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)

        print("Starting OAuth interactive flow. Opening browser...")
        creds = flow.run_local_server(port=0)

        with open(token_path, "w") as token:
            token.write(creds.to_json())

        print(f"Success! Token saved to '{token_path}'")
        sys.exit(0)

    mcp.run()
