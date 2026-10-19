import email
import imaplib
import mimetypes
import os
import smtplib
from datetime import date
from email import encoders
from email.header import decode_header, make_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.message import Message
from pathlib import Path
from typing import Any


def _credentials(gmail_user: str | None, app_password: str | None) -> tuple[str, str]:
    user = gmail_user or os.getenv("GMAIL_USER")
    password = app_password or os.getenv("GMAIL_APP_PASSWORD")
    if not user or not password:
        raise RuntimeError("Gmail account credentials are not configured.")
    return user, password


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def _imap_connection(gmail_user: str, app_password: str) -> imaplib.IMAP4_SSL:
    connection = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        connection.login(gmail_user, app_password)
    except Exception:
        connection.logout()
        raise
    return connection


def search_emails(
    gmail_user: str,
    app_password: str,
    after_date: date,
    senders: list[str] | None = None,
) -> list[dict[str, str]]:
    """Return message headers matching the date and any specified senders."""
    connection = _imap_connection(gmail_user, app_password)
    try:
        status, _ = connection.select("inbox", readonly=True)
        if status != "OK":
            raise RuntimeError("Could not open the Gmail inbox.")

        criteria = ["SINCE", after_date.strftime("%d-%b-%Y")]
        sender_ids: set[bytes] = set()
        if senders:
            for sender in senders:
                status, response = connection.uid(
                    "search",
                    *criteria,
                    "FROM",
                    f'"{sender}"',
                )
                if status != "OK":
                    raise RuntimeError(
                        f"Gmail could not search for sender {sender}."
                    )
                sender_ids.update(response[0].split())
            email_ids = sorted(sender_ids, key=lambda uid: int(uid))
        else:
            status, response = connection.uid("search", *criteria)
            if status != "OK":
                raise RuntimeError("Gmail could not search the inbox.")
            email_ids = response[0].split()

        messages: list[dict[str, str]] = []
        for uid in email_ids:
            uid_text = uid.decode("ascii")
            status, fetched = connection.uid(
                "fetch",
                uid_text,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
            )
            if status != "OK":
                raise RuntimeError(f"Could not read message {uid_text}.")
            header_bytes = next(
                (
                    item[1]
                    for item in fetched
                    if isinstance(item, tuple) and isinstance(item[1], bytes)
                ),
                None,
            )
            if header_bytes is None:
                continue
            message = email.message_from_bytes(header_bytes)
            messages.append(
                {
                    "uid": uid_text,
                    "sender": _decode_header_value(message.get("From")),
                    "subject": _decode_header_value(message.get("Subject")),
                    "date": _decode_header_value(message.get("Date")),
                }
            )
        return messages
    finally:
        connection.logout()


def _fetch_message(
    gmail_user: str, app_password: str, uid: str
) -> Message:
    connection = _imap_connection(gmail_user, app_password)
    try:
        status, _ = connection.select("inbox", readonly=True)
        if status != "OK":
            raise RuntimeError("Could not open the Gmail inbox.")
        status, fetched = connection.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK":
            raise RuntimeError(f"Could not fetch message {uid}.")
        raw_message = next(
            (
                item[1]
                for item in fetched
                if isinstance(item, tuple) and isinstance(item[1], bytes)
            ),
            None,
        )
        if raw_message is None:
            raise RuntimeError(f"Gmail returned no data for message {uid}.")
        return email.message_from_bytes(raw_message)
    finally:
        connection.logout()


def _attachment_parts(message: Message) -> list[tuple[str, Any]]:
    attachments: list[tuple[str, Any]] = []
    for part in message.walk():
        filename = part.get_filename()
        if filename:
            attachments.append((_decode_header_value(filename), part))
    return attachments


def download_attachments(
    gmail_user: str, app_password: str, uid: str, output_dir: str | Path
) -> list[str]:
    message = _fetch_message(gmail_user, app_password, uid)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    saved_files: list[str] = []
    for filename, part in _attachment_parts(message):
        safe_name = Path(filename.replace("\\", "/")).name
        if not safe_name or safe_name in {".", ".."}:
            continue
        filepath = destination / safe_name
        if filepath.exists():
            filepath = destination / f"{uid}_{safe_name}"
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise RuntimeError(f"Attachment {safe_name} has no decoded byte payload.")
        filepath.write_bytes(payload)
        saved_files.append(str(filepath))
    return saved_files


def forward_matching_email(
    gmail_user: str,
    app_password: str,
    uid: str,
    recipient: str,
    attachment_match: str,
) -> bool:
    """Forward a matching message with its attachments; return False if unmatched."""
    message = _fetch_message(gmail_user, app_password, uid)
    attachments = _attachment_parts(message)
    token = Path(attachment_match).stem.casefold().strip()
    if not token or not any(token in Path(name).name.casefold() for name, _ in attachments):
        return False

    body_text = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    body_text = payload.decode(charset, errors="replace")
                    break
    else:
        payload = message.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = message.get_content_charset() or "utf-8"
            body_text = payload.decode(charset, errors="replace")

    forwarded = MIMEMultipart()
    forwarded["From"] = gmail_user
    forwarded["To"] = recipient
    forwarded["Subject"] = f"Fwd: {_decode_header_value(message.get('Subject'))}"
    forwarded.attach(
        MIMEText(
            "Forwarded message\n"
            f"From: {_decode_header_value(message.get('From'))}\n"
            f"Date: {_decode_header_value(message.get('Date'))}\n"
            f"Subject: {_decode_header_value(message.get('Subject'))}\n\n"
            f"{body_text}",
            "plain",
        )
    )

    for filename, part in attachments:
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise RuntimeError(f"Attachment {filename} has no decoded byte payload.")
        attachment = MIMEBase(
            part.get_content_maintype(), part.get_content_subtype()
        )
        attachment.set_payload(payload)
        encoders.encode_base64(attachment)
        attachment.add_header("Content-Disposition", "attachment", filename=filename)
        forwarded.attach(attachment)

    user, password = _credentials(gmail_user, app_password)
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [recipient], forwarded.as_string())
    return True


def send_attachment(
    gmail_user: str,
    app_password: str,
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
    message.attach(MIMEText(body, "plain", "utf-8"))

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

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, app_password)
        server.sendmail(gmail_user, [recipient], message.as_string())


if __name__ == "__main__":
    raise SystemExit(
        "Use the FastAPI dashboard. Configure GMAIL_USER and GMAIL_APP_PASSWORD "
        "or GMAIL_ACCOUNTS before starting it."
    )
