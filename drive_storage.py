import json
import mimetypes
import os
import re
import threading
from pathlib import Path
from typing import Any

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


ROOT = Path(__file__).resolve().parent
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]
_credentials_lock = threading.Lock()
_GOOGLE_DOC_EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}


def _configured_path(variable: str, default: Path) -> Path:
    value = os.getenv(variable)
    path = Path(value).expanduser() if value else default
    return path if path.is_absolute() else ROOT / path


def _client_secrets_path() -> Path:
    return _configured_path("GOOGLE_DRIVE_CLIENT_SECRETS_FILE", ROOT / "credentials.json")


def _token_path() -> Path:
    data_dir = _configured_path("MAIL_DASHBOARD_DATA_DIR", ROOT / "data")
    return data_dir / "drive-token.json"


def _save_credentials(credentials: Credentials) -> None:
    token_path = _token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = token_path.with_suffix(".json.tmp")
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
        token_file.write(credentials.to_json())
    temporary_path.replace(token_path)


def _load_credentials() -> Credentials:
    client_secrets = _client_secrets_path()
    if not client_secrets.is_file():
        raise RuntimeError(
            "Google Drive is not configured. Add an OAuth desktop-client JSON file "
            f"at {client_secrets} and connect Drive from the dashboard."
        )

    token_path = _token_path()
    if not token_path.is_file():
        raise RuntimeError(
            "Google Drive is not connected. Use the Connect Google Drive button "
            "in the dashboard."
        )

    with _credentials_lock:
        try:
            credentials = Credentials.from_authorized_user_file(
                str(token_path), SCOPES
            )
        except (OSError, ValueError) as error:
            raise RuntimeError(
                f"Could not read Google Drive authorization at {token_path}: {error}"
            ) from error
        if not isinstance(credentials, Credentials):
            raise RuntimeError(
                "The saved Google Drive authorization uses an unsupported credential type."
            )

        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                _save_credentials(credentials)
            except (GoogleAuthError, OSError, ValueError) as error:
                raise RuntimeError(
                    f"Could not refresh Google Drive authorization: {error}"
                ) from error
        if not credentials.valid:
            raise RuntimeError(
                "Google Drive authorization has expired. Connect Google Drive again."
            )
        return Credentials.from_authorized_user_info(
            json.loads(credentials.to_json()), SCOPES
        )


def drive_status() -> dict[str, Any]:
    client_secrets = _client_secrets_path()
    token_path = _token_path()
    if not client_secrets.is_file():
        return {
            "configured": False,
            "connected": False,
            "message": (
                "Add a Google OAuth desktop-client JSON file at "
                f"{client_secrets} to enable Drive."
            ),
        }
    if not token_path.is_file():
        return {
            "configured": True,
            "connected": False,
            "message": "Google Drive is ready to connect.",
        }
    try:
        credentials = Credentials.from_authorized_user_file(
            str(token_path), SCOPES
        )
    except (GoogleAuthError, OSError, ValueError) as error:
        return {
            "configured": True,
            "connected": False,
            "message": f"Could not read saved Google Drive authorization: {error}",
        }
    if credentials.valid or credentials.refresh_token:
        return {
            "configured": True,
            "connected": True,
            "message": "Google Drive is connected.",
        }
    return {
        "configured": True,
        "connected": False,
        "message": "Google Drive authorization has expired; connect again.",
    }


def connect_drive() -> None:
    client_secrets = _client_secrets_path()
    if not client_secrets.is_file():
        raise RuntimeError(
            "Google Drive is not configured. Add an OAuth desktop-client JSON file "
            f"at {client_secrets}."
        )
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets), SCOPES
        )
        credentials = flow.run_local_server(
            host="127.0.0.1", port=0, open_browser=True
        )
        if not isinstance(credentials, Credentials):
            raise RuntimeError(
                "Google returned an unsupported authorization credential type."
            )
        _save_credentials(credentials)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Could not connect Google Drive: {error}") from error


def build_drive_service() -> Any:
    try:
        return build(
            "drive",
            "v3",
            credentials=_load_credentials(),
            cache_discovery=False,
        )
    except HttpError as error:
        raise RuntimeError(f"Could not initialize Google Drive: {error}") from error


def _safe_filename(filename: str) -> str:
    safe_name = Path(filename.replace("\\", "/")).name
    if not safe_name or safe_name in {".", ".."}:
        raise RuntimeError("Google Drive returned an invalid file name.")
    return safe_name


def download_file(
    service: Any, file_id: str, output_dir: str | Path, uid: str
) -> str:
    metadata = service.files().get(
        fileId=file_id,
        fields="id,name,mimeType,shortcutDetails",
    ).execute()
    if metadata.get("mimeType") == "application/vnd.google-apps.shortcut":
        target_id = metadata.get("shortcutDetails", {}).get("targetId")
        if not target_id:
            raise RuntimeError("Google Drive shortcut has no accessible target.")
        metadata = service.files().get(
            fileId=target_id,
            fields="id,name,mimeType",
        ).execute()

    name = _safe_filename(metadata.get("name", ""))
    mime_type = metadata.get("mimeType", "application/octet-stream")
    if mime_type in _GOOGLE_DOC_EXPORTS:
        export_type, extension = _GOOGLE_DOC_EXPORTS[mime_type]
        if Path(name).suffix.casefold() != extension:
            name = f"{Path(name).stem}{extension}"
        request = service.files().export_media(
            fileId=metadata["id"],
            mimeType=export_type,
        )
    elif mime_type.startswith("application/vnd.google-apps."):
        raise RuntimeError(
            f"Google Drive file type {mime_type} cannot be downloaded as an attachment."
        )
    else:
        request = service.files().get_media(fileId=metadata["id"])

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem
    suffix = Path(name).suffix
    collision_number = 0
    while True:
        if collision_number == 0:
            candidate_name = name
        elif collision_number == 1:
            candidate_name = f"{uid}_{name}"
        else:
            candidate_name = f"{uid}_{collision_number}_{stem}{suffix}"
        filepath = destination / candidate_name
        try:
            output_file = filepath.open("xb")
            break
        except FileExistsError:
            collision_number += 1

    completed = False
    try:
        downloader = MediaIoBaseDownload(output_file, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        completed = True
    finally:
        output_file.close()
        if not completed:
            filepath.unlink(missing_ok=True)
    return str(filepath)


def upload_file_for_recipient(path: str | Path, recipient: str) -> str:
    file_path = Path(path)
    mime_type, _ = mimetypes.guess_type(file_path.name)
    service = build_drive_service()
    uploaded = service.files().create(
        body={"name": file_path.name},
        media_body=MediaFileUpload(
            str(file_path),
            mimetype=mime_type or "application/octet-stream",
            resumable=True,
        ),
        fields="id,webViewLink",
    ).execute()
    file_id = uploaded["id"]
    try:
        service.permissions().create(
            fileId=file_id,
            body={"type": "user", "role": "reader", "emailAddress": recipient},
            sendNotificationEmail=False,
        ).execute()
    except HttpError as error:
        try:
            service.files().delete(fileId=file_id).execute()
        except HttpError as cleanup_error:
            raise RuntimeError(
                "The file was uploaded to Drive but recipient access could not be "
                f"granted, and the private Drive copy could not be removed (file ID "
                f"{file_id}): {cleanup_error}"
            ) from error
        raise RuntimeError(
            f"Could not grant Google Drive access to {recipient}: {error}"
        ) from error
    return uploaded.get("webViewLink") or f"https://drive.google.com/open?id={file_id}"


def gmail_attachment_limit_bytes() -> int:
    value = os.getenv("GMAIL_ATTACHMENT_MAX_BYTES", str(16 * 1024 * 1024))
    try:
        limit = int(value)
    except ValueError as error:
        raise RuntimeError(
            "GMAIL_ATTACHMENT_MAX_BYTES must be a positive integer."
        ) from error
    if limit <= 0:
        raise RuntimeError("GMAIL_ATTACHMENT_MAX_BYTES must be a positive integer.")
    return limit


def extract_file_id(url: str) -> str | None:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "drive.google.com",
        "docs.google.com",
    }:
        return None
    match = re.search(
        r"/(?:file|document|spreadsheets|presentation|drawings)/"
        r"(?:u/\d+/)?d/([A-Za-z0-9_-]+)",
        parsed.path,
    )
    if match:
        return match.group(1)
    file_ids = parse_qs(parsed.query).get("id", [])
    if file_ids and re.fullmatch(r"[A-Za-z0-9_-]+", file_ids[0]):
        return file_ids[0]
    return None
