import email
import base64
import html
import mimetypes
import os
import re
import threading
from datetime import date
from email import encoders
from email.header import decode_header, make_header
from html.parser import HTMLParser
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.message import Message
from pathlib import Path
from typing import Any

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import drive_storage


ROOT = Path(__file__).resolve().parent
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
_gmail_data_dir = Path(os.getenv("MAIL_DASHBOARD_DATA_DIR", ROOT / "data")).expanduser()
if not _gmail_data_dir.is_absolute():
    _gmail_data_dir = ROOT / _gmail_data_dir
_GMAIL_TOKEN_PATH = _gmail_data_dir / "gmail-token.json"
_gmail_credentials_lock = threading.Lock()


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def _google_client_secrets_path() -> Path:
    value = os.getenv("GOOGLE_DRIVE_CLIENT_SECRETS_FILE")
    path = Path(value).expanduser() if value else ROOT / "credentials.json"
    return path if path.is_absolute() else ROOT / path


def _save_gmail_credentials(credentials: Credentials) -> None:
    _GMAIL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        _GMAIL_TOKEN_PATH,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
        token_file.write(credentials.to_json())


def gmail_status() -> dict[str, Any]:
    client_secrets = _google_client_secrets_path()
    if not client_secrets.is_file():
        return {
            "configured": False,
            "connected": False,
            "message": (
                "Add a Google OAuth Desktop client JSON file at "
                f"{client_secrets} to enable Gmail API access."
            ),
        }
    if not _GMAIL_TOKEN_PATH.is_file():
        return {
            "configured": True,
            "connected": False,
            "message": "Google Gmail API is ready to connect.",
        }
    try:
        credentials = Credentials.from_authorized_user_file(
            str(_GMAIL_TOKEN_PATH), GMAIL_SCOPES
        )
    except (GoogleAuthError, OSError, ValueError) as error:
        return {
            "configured": True,
            "connected": False,
            "message": f"Could not read saved Gmail authorization: {error}",
        }
    if not credentials.has_scopes(GMAIL_SCOPES):
        return {
            "configured": True,
            "connected": False,
            "message": "Gmail authorization needs to be renewed with Gmail access.",
        }
    return {
        "configured": True,
        "connected": credentials.valid or credentials.refresh_token is not None,
        "message": "Gmail API is connected."
        if credentials.valid or credentials.refresh_token
        else "Gmail authorization expired; connect again.",
    }


def connect_gmail() -> dict[str, Any]:
    client_secrets = _google_client_secrets_path()
    if not client_secrets.is_file():
        raise RuntimeError(
            "Google Gmail API is not configured. Add an OAuth Desktop client JSON "
            f"file at {client_secrets}."
        )
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets), GMAIL_SCOPES
        )
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=True,
            access_type="offline",
            prompt="consent",
        )
        if not isinstance(credentials, Credentials):
            raise RuntimeError("Google returned an unsupported Gmail credential type.")
        _save_gmail_credentials(credentials)
    except (GoogleAuthError, OSError, ValueError) as error:
        raise RuntimeError(f"Could not connect the Gmail API: {error}") from error
    return gmail_status()


def _gmail_service(gmail_user: str) -> Any:
    client_secrets = _google_client_secrets_path()
    if not client_secrets.is_file():
        raise RuntimeError(
            "Google Gmail API is not configured. Add an OAuth Desktop client JSON "
            f"file at {client_secrets}."
        )
    if not _GMAIL_TOKEN_PATH.is_file():
        raise RuntimeError(
            "Gmail API is not connected. Use the Connect Gmail API button in the dashboard."
        )

    with _gmail_credentials_lock:
        try:
            credentials = Credentials.from_authorized_user_file(
                str(_GMAIL_TOKEN_PATH), GMAIL_SCOPES
            )
            if not credentials.has_scopes(GMAIL_SCOPES):
                raise RuntimeError(
                    "Gmail authorization is missing required read/send permissions. "
                    "Connect the Gmail API again."
                )
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                _save_gmail_credentials(credentials)
            if not credentials.valid:
                raise RuntimeError(
                    "Gmail authorization expired. Connect the Gmail API again."
                )
        except (GoogleAuthError, OSError, ValueError) as error:
            raise RuntimeError(f"Could not load Gmail authorization: {error}") from error

    try:
        service = build(
            "gmail",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )
        profile = service.users().getProfile(userId="me").execute()
    except HttpError as error:
        raise RuntimeError(f"Gmail API request failed: {error}") from error
    profile_email = profile.get("emailAddress", "")
    if profile_email.casefold() != gmail_user.casefold():
        raise RuntimeError(
            "The connected Gmail account does not match GMAIL_USER "
            f"({profile_email or 'unknown account'})."
        )
    return service


def search_emails(
    gmail_user: str,
    after_date: date,
    senders: list[str] | None = None,
) -> list[dict[str, str]]:
    """Return message headers matching the date and any specified senders."""
    service = _gmail_service(gmail_user)
    after_query = f"after:{after_date.strftime('%Y/%m/%d')}"
    queries = (
        [f"{after_query} from:{sender}" for sender in senders]
        if senders
        else [after_query]
    )
    message_ids: set[str] = set()
    for query in queries:
        page_token = None
        while True:
            response = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    labelIds=["INBOX"],
                    maxResults=500,
                    pageToken=page_token,
                )
                .execute()
            )
            message_ids.update(
                item["id"] for item in response.get("messages", [])
            )
            page_token = response.get("nextPageToken")
            if not page_token:
                break

    messages: list[dict[str, str]] = []
    for message_id in sorted(message_ids):
        try:
            response = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
        except HttpError as error:
            raise RuntimeError(
                f"Gmail could not read message {message_id}: {error}"
            ) from error
        headers = {
            header["name"].casefold(): header["value"]
            for header in response.get("payload", {}).get("headers", [])
        }
        messages.append(
            {
                "uid": message_id,
                "sender": _decode_header_value(headers.get("from")),
                "subject": _decode_header_value(headers.get("subject")),
                "date": _decode_header_value(headers.get("date")),
            }
        )
    return messages


def _fetch_message(gmail_user: str, uid: str) -> Message:
    service = _gmail_service(gmail_user)
    try:
        response = (
            service.users()
            .messages()
            .get(userId="me", id=uid, format="raw")
            .execute()
        )
    except HttpError as error:
        raise RuntimeError(f"Could not fetch Gmail message {uid}: {error}") from error
    encoded_message = response.get("raw")
    if not encoded_message:
        raise RuntimeError(f"Gmail returned no raw data for message {uid}.")
    padding = "=" * (-len(encoded_message) % 4)
    try:
        raw_message = base64.urlsafe_b64decode(encoded_message + padding)
    except (ValueError, TypeError) as error:
        raise RuntimeError(f"Gmail returned invalid message data for {uid}.") from error
    return email.message_from_bytes(raw_message)


def _attachment_parts(message: Message) -> list[tuple[str, Any]]:
    attachments: list[tuple[str, Any]] = []
    for part in message.walk():
        filename = part.get_filename()
        if filename:
            attachments.append((_decode_header_value(filename), part))
    return attachments


class _DriveLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del tag
        for name, value in attrs:
            if name.casefold() == "href" and value:
                self.links.append(value)


def _message_drive_file_ids(message: Message) -> list[str]:
    links: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_filename():
            continue
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        charset = part.get_content_charset() or "utf-8"
        content = payload.decode(charset, errors="replace")
        if content_type == "text/html":
            parser = _DriveLinkParser()
            parser.feed(content)
            links.extend(parser.links)
        links.extend(
            re.findall(r"https?://(?:drive|docs)\.google\.com/[^\s<>\"']+", content, re.I)
        )

    file_ids: list[str] = []
    seen: set[str] = set()
    for link in links:
        cleaned = html.unescape(link).rstrip(".,;:!?)\"]}")
        file_id = drive_storage.extract_file_id(cleaned)
        if file_id and file_id not in seen:
            seen.add(file_id)
            file_ids.append(file_id)
    return file_ids


def download_attachments(
    gmail_user: str, uid: str, output_dir: str | Path
) -> list[str]:
    message = _fetch_message(gmail_user, uid)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    saved_files: list[str] = []
    for filename, part in _attachment_parts(message):
        safe_name = Path(filename.replace("\\", "/")).name
        if not safe_name or safe_name in {".", ".."}:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise RuntimeError(f"Attachment {safe_name} has no decoded byte payload.")
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        collision_number = 0
        while True:
            if collision_number == 0:
                candidate_name = safe_name
            elif collision_number == 1:
                candidate_name = f"{uid}_{safe_name}"
            else:
                candidate_name = f"{uid}_{collision_number}_{stem}{suffix}"
            filepath = destination / candidate_name
            try:
                with filepath.open("xb") as output_file:
                    output_file.write(payload)
                break
            except FileExistsError:
                collision_number += 1
        saved_files.append(str(filepath))
    drive_file_ids = _message_drive_file_ids(message)
    if drive_file_ids:
        service = drive_storage.build_drive_service()
        for file_id in drive_file_ids:
            saved_files.append(
                drive_storage.download_file(service, file_id, destination, uid)
            )
    return saved_files


def _send_message(gmail_user: str, message: Message) -> None:
    service = _gmail_service(gmail_user)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()
    except HttpError as error:
        raise RuntimeError(f"Gmail could not send the message: {error}") from error


def send_attachment(
    gmail_user: str,
    recipient: str,
    roll_number: str,
    attachment_path: str | Path,
    subject: str | None = None,
    body: str = "Please find the attached file.",
) -> None:
    """Send one local attachment for a roll number."""
    path = Path(attachment_path)
    if not path.is_file():
        raise FileNotFoundError(f"Upload attachment does not exist: {path}")

    message = MIMEMultipart()
    message["From"] = gmail_user
    message["To"] = recipient
    message["Subject"] = subject or f"Attachment for roll number {roll_number}"
    html_body: str | None = None
    if path.stat().st_size > drive_storage.gmail_attachment_limit_bytes():
        drive_link = drive_storage.upload_file_for_recipient(path, recipient)
        body = (
            f"{body}\n\nThe file is too large to attach to this email. "
            f"Download it from Google Drive: {drive_link}\n"
            f"Drive access has been granted to {recipient}."
        )
        escaped_name = html.escape(path.name, quote=True)
        escaped_link = html.escape(drive_link, quote=True)
        html_body = (
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;margin-top:16px;">'
            "<tr><td>"
            f'<a href="{escaped_link}" style="display:inline-block;'
            "text-decoration:none;color:#202124;"
            'font-family:Arial,sans-serif;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="border:1px solid #dadce0;border-radius:4px;'
            'background:#f8f9fa;border-collapse:separate;">'
            "<tr>"
            '<td style="padding:12px 10px;color:#1a73e8;'
            'font-size:20px;line-height:24px;">&#128196;</td>'
            f'<td style="padding:12px 14px 12px 4px;font-size:14px;'
            f'line-height:20px;">{escaped_name}</td>'
            "</tr></table></a>"
            "</td></tr></table>"
            '<p style="color:#5f6368;font:12px Arial,sans-serif;">'
            f"Drive access has been granted to {html.escape(recipient)}."
            "</p>"
        )
    else:
        mime_type, _ = mimetypes.guess_type(path.name)
        if mime_type is None:
            mime_type = "application/octet-stream"
        main_type, sub_type = mime_type.split("/", 1)
        attachment = MIMEBase(main_type, sub_type)
        attachment.set_payload(path.read_bytes())
        encoders.encode_base64(attachment)
        attachment.add_header(
            "Content-Disposition", "attachment", filename=path.name
        )
        message.attach(attachment)
    if html_body is None:
        message.attach(MIMEText(body, "plain", "utf-8"))
    else:
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(body, "plain", "utf-8"))
        body_part.attach(
            MIMEText(
                f"<html><body><p>{html.escape(body).replace(chr(10), '<br>')}</p>"
                f"{html_body}</body></html>",
                "html",
                "utf-8",
            )
        )
        message.attach(body_part)

    _send_message(gmail_user, message)


if __name__ == "__main__":
    raise SystemExit(
        "Use the FastAPI dashboard. Configure GMAIL_USER and connect the Gmail API "
        "from the dashboard before starting mail operations."
    )
