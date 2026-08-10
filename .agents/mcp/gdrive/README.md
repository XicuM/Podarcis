# Google Drive MCP Server (Read-only)

A FastMCP server that provides read-only access to files and folders in Google Drive, with automatic conversion of Google Docs/Sheets to Markdown/CSV, saving them directly to the `sources/internal_documentation/` directory.

## Setup Instructions

Choose **one** of the authentication methods:

### Method 1: Automatic OAuth 2.0 Client (Recommended & Easiest)
1. Run the interactive setup helper:
   ```bash
   python scripts/setup.py
   ```
   Or run the authentication script directly:
   ```bash
   .venv/bin/python .agents/mcp/gdrive/server.py --auth
   ```
2. The script will automatically launch a browser window using pre-configured, built-in application client credentials.
3. Log in with your Google account, authorize read-only access to Google Drive, and the server will automatically save `token.json` under `.agents/mcp/gdrive/`. No manual API key or credentials download is needed!

### Method 2: Custom OAuth 2.0 Client (Using your own GCP Project)
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Google Drive API** for your project.
3. Set up the **OAuth consent screen** (Internal or External, add your email, and add the scope `.../auth/drive.readonly`).
4. Go to **Credentials**, click **Create Credentials** > **OAuth client ID**.
5. Select **Desktop app** as the application type, download the credentials JSON, rename it to `credentials.json`, and place it in this directory:
   `.agents/mcp/gdrive/credentials.json`
6. Run the authentication flow:
   ```bash
   .venv/bin/python .agents/mcp/gdrive/server.py --auth
   ```

### Method 3: Google Service Account (Recommended for Headless Automation)
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Google Drive API** for your project.
3. Go to **APIs & Services > Credentials**.
4. Click **Create Credentials** > **Service account**.
5. Name it, then click on the newly created Service Account.
6. Under the **Keys** tab, click **Add Key** > **Create new key** (select JSON format).
7. Download the JSON key file, rename it to `service_account.json`, and place it in this directory:
   `.agents/mcp/gdrive/service_account.json`
8. Share the Google Drive files/folders you want the agent to access with the service account's email address (e.g. `your-service-account@project-id.iam.gserviceaccount.com`).

---

## Exposed Tools

### `gdrive_list_files`
List or search files in Google Drive.
- `query` (optional): Filter query in Google Drive `q` parameter syntax. E.g., `name contains 'meeting'` or `mimeType = 'application/vnd.google-apps.folder'`.
- `page_size` (optional): Max number of files to return (default: 20).

### `gdrive_download_file`
Download a file from Google Drive and copy/convert it to the `sources/internal_documentation/` directory.
- `file_id`: The ID of the file to download.
- `dest_subdir` (optional): Subdirectory under `sources/internal_documentation/` to place the downloaded file.

Google Docs (`application/vnd.google-apps.document`) are automatically converted to Markdown (`.md`).
Google Sheets (`application/vnd.google-apps.spreadsheet`) are automatically exported to CSV (`.csv`).
Files are saved using `snake_case` naming convention.
