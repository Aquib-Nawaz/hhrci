import csv
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, TypedDict

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

import drive_storage
import mail1


DATA_DIR = Path(os.getenv("MAIL_DASHBOARD_DATA_DIR", ROOT / "data")).expanduser()
if not DATA_DIR.is_absolute():
    DATA_DIR = ROOT / DATA_DIR
MAPPINGS_FILE = DATA_DIR / "mappings.csv"
PROGRESS_FILE = DATA_DIR / "student_mail_progress.csv"
PROGRESS_COLUMNS = (
    "test_name",
    "file",
    "roll_number",
    "recipient",
    "sender_account",
    "status",
    "sent_at",
    "error",
)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ProgressRecord(TypedDict):
    test_name: str
    file: str
    roll_number: str
    recipient: str
    sender_account: str
    status: Literal["sending", "sent", "failed"]
    sent_at: str
    error: str


@dataclass(frozen=True)
class StudentCopy:
    roll_number: str
    path: Path
    recipient: str


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def load_mappings(path: Path = MAPPINGS_FILE) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(
            f"Recipient map not found: {path}. Create it with columns "
            "'roll_number,mail_account' (the dashboard can generate this CSV)."
        )
    mappings: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames != ["roll_number", "mail_account"]:
            raise ValueError(
                f"{path} must have the columns roll_number,mail_account in that order."
            )
        for line_number, row in enumerate(reader, start=2):
            roll_number = (row.get("roll_number") or "").strip()
            recipient = (row.get("mail_account") or "").strip()
            if not roll_number and not recipient:
                continue
            if not re.fullmatch(r"\d{5}", roll_number):
                raise ValueError(
                    f"{path}:{line_number}: roll_number must contain exactly five digits."
                )
            if not EMAIL_PATTERN.fullmatch(recipient):
                raise ValueError(
                    f"{path}:{line_number}: invalid recipient email for {roll_number}."
                )
            if roll_number in mappings:
                raise ValueError(
                    f"{path}:{line_number}: duplicate roll number {roll_number}."
                )
            mappings[roll_number] = recipient
    return mappings


def discover_student_copies(
    folders: list[Path], mappings: dict[str, str]
) -> tuple[list[StudentCopy], list[str]]:
    files_by_roll: dict[str, list[Path]] = {}
    skipped: list[str] = []
    seen_paths: set[str] = set()
    for folder in folders:
        if not folder.is_dir():
            raise NotADirectoryError(f"Folder does not exist: {folder}")
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            normalized = _normalized_path(path)
            if normalized in seen_paths:
                continue
            seen_paths.add(normalized)
            roll_numbers = re.findall(r"(?<!\d)\d{5}(?!\d)", path.stem)
            if len(roll_numbers) != 1:
                skipped.append(f"{path}: expected exactly one 5-digit roll number")
                continue
            files_by_roll.setdefault(roll_numbers[0], []).append(path.resolve())

    copies: list[StudentCopy] = []
    for roll_number, paths in sorted(files_by_roll.items()):
        if len(paths) != 1:
            skipped.append(
                f"Roll {roll_number}: ambiguous; multiple files found: "
                + ", ".join(str(path) for path in paths)
            )
            continue
        recipient = mappings.get(roll_number)
        if not recipient:
            skipped.append(f"Roll {roll_number}: no recipient mapping")
            continue
        copies.append(StudentCopy(roll_number, paths[0], recipient))
    return copies, skipped


def load_progress(path: Path = PROGRESS_FILE) -> dict[tuple[str, str], ProgressRecord]:
    if not path.exists():
        return {}
    records: dict[tuple[str, str], ProgressRecord] = {}
    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames != list(PROGRESS_COLUMNS):
            raise ValueError(
                f"{path} has an unsupported format. Expected columns: "
                + ",".join(PROGRESS_COLUMNS)
            )
        for line_number, row in enumerate(reader, start=2):
            test_name = (row.get("test_name") or "").strip()
            filepath = (row.get("file") or "").strip()
            if not test_name or not filepath:
                raise ValueError(f"{path}:{line_number}: test_name and file are required.")
            key = (test_name.casefold(), _normalized_path(Path(filepath)))
            if key in records:
                raise ValueError(
                    f"{path}:{line_number}: duplicate progress record for {filepath}."
                )
            status = (row.get("status") or "").strip()
            if status not in ("sending", "sent", "failed"):
                raise ValueError(
                    f"{path}:{line_number}: invalid progress status {status!r}."
                )
            records[key] = ProgressRecord(
                test_name=test_name,
                file=filepath,
                roll_number=row.get("roll_number") or "",
                recipient=row.get("recipient") or "",
                sender_account=row.get("sender_account") or "",
                status=status,
                sent_at=row.get("sent_at") or "",
                error=row.get("error") or "",
            )
    return records


def save_progress(
    records: dict[tuple[str, str], ProgressRecord], path: Path = PROGRESS_FILE
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as csv_file:
            fieldnames: list[str] = list(PROGRESS_COLUMNS)
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            for key in sorted(records):
                writer.writerow(records[key])
        os.replace(temporary_name, path)
    except OSError:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def send_pending_copies(
    test_name: str,
    sender_account: str,
    copies: list[StudentCopy],
    progress_path: Path = PROGRESS_FILE,
    send: Callable[..., None] | None = None,
) -> tuple[int, int, int]:
    records = load_progress(progress_path)
    send_message = send or mail1.send_attachment
    sent_count = 0
    failed_count = 0
    skipped_count = 0
    normalized_name = test_name.strip()

    for copy in copies:
        filepath = _normalized_path(copy.path)
        key = (normalized_name.casefold(), filepath)
        previous = records.get(key)
        if previous and previous.get("status") == "sent":
            print(f"Already sent; skipping: {copy.path}")
            skipped_count += 1
            continue

        record: ProgressRecord = {
            "test_name": normalized_name,
            "file": str(copy.path.resolve()),
            "roll_number": copy.roll_number,
            "recipient": copy.recipient,
            "sender_account": sender_account,
            "status": "sending",
            "sent_at": "",
            "error": "",
        }
        records[key] = record
        save_progress(records, progress_path)
        print(f"Sending {copy.path.name} (roll {copy.roll_number}) to {copy.recipient}…")
        try:
            send_message(
                sender_account,
                copy.recipient,
                copy.roll_number,
                copy.path,
            )
        except (OSError, RuntimeError, ValueError) as error:
            record.update(status="failed", error=str(error))
            save_progress(records, progress_path)
            failed_count += 1
            print(f"FAILED: {copy.path}: {error}", file=sys.stderr)
            continue

        record.update(
            status="sent",
            sent_at=datetime.now(timezone.utc).isoformat(),
            error="",
        )
        save_progress(records, progress_path)
        sent_count += 1
        print(f"Sent: {copy.path.name}")

    return sent_count, failed_count, skipped_count


def _prompt_for_folders() -> list[Path]:
    print("Enter attachment folder paths (one per line); leave blank when finished.")
    folders: list[Path] = []
    while True:
        value = input("Folder: ").strip().strip('"')
        if not value:
            break
        folder = Path(value).expanduser().resolve()
        if not folder.is_dir():
            print(f"Folder does not exist: {folder}")
            continue
        if folder not in folders:
            folders.append(folder)
    if not folders:
        raise ValueError("Select at least one folder.")
    return folders


def _authorize_gmail() -> str:
    status = mail1.gmail_status()
    if not status["configured"]:
        raise RuntimeError(
            f"{status['message']} Add credentials.json or set "
            "GOOGLE_DRIVE_CLIENT_SECRETS_FILE."
        )
    try:
        account = mail1.gmail_account()
    except RuntimeError:
        account = None
    if account:
        answer = input(f"Use Gmail account {account}? [Y/n, r=reconnect]: ").strip().casefold()
        if answer in ("", "y", "yes"):
            return account
        if answer not in ("r", "reconnect", "n", "no"):
            raise ValueError("Enter Y, n, or r.")
    print("Opening Google sign-in in your browser…")
    mail1.connect_gmail()
    account = mail1.gmail_account()
    if not account:
        raise RuntimeError("Google authorization completed, but no Gmail account was returned.")
    print(f"Authorized as {account}.")
    return account


def _prepare_large_file_sending(
    copies: list[StudentCopy],
) -> tuple[list[StudentCopy], int]:
    attachment_limit = drive_storage.gmail_attachment_limit_bytes()
    large_copies = [
        copy for copy in copies if copy.path.stat().st_size > attachment_limit
    ]
    if not large_copies:
        return copies, 0

    status = drive_storage.drive_status()
    if status["connected"]:
        return copies, 0

    print(
        f"{len(large_copies)} file(s) exceed Gmail's attachment limit "
        f"({attachment_limit} bytes). They need Google Drive access."
    )
    if not status["configured"]:
        print(status["message"])
        return (
            [copy for copy in copies if copy not in large_copies],
            len(large_copies),
        )
    answer = input("Authorize Google Drive now for these files? [y/N]: ").strip().casefold()
    if answer in ("y", "yes"):
        drive_storage.connect_drive()
        return copies, 0
    return [copy for copy in copies if copy not in large_copies], len(large_copies)


def main() -> int:
    try:
        sender_account = _authorize_gmail()
        test_name = input("Test name (used to resume progress): ").strip()
        if not test_name:
            raise ValueError("Test name cannot be empty.")
        folders = _prompt_for_folders()
        mappings = load_mappings()
        copies, discovery_skips = discover_student_copies(folders, mappings)
        if discovery_skips:
            print(f"\nSkipped during scan: {len(discovery_skips)}")
            for reason in discovery_skips:
                print(f"  - {reason}")

        progress = load_progress()
        pending_copies = [
            copy
            for copy in copies
            if progress.get(
                (test_name.casefold(), _normalized_path(copy.path)), {}
            ).get("status")
            != "sent"
        ]
        already_sent_count = len(copies) - len(pending_copies)
        pending_copies, drive_skipped = _prepare_large_file_sending(pending_copies)
        if not pending_copies:
            print(
                f"No sendable mapped files found for test {test_name!r}. "
                f"Skipped pending large files without Drive authorization: {drive_skipped}."
            )
            return 1 if drive_skipped else 0

        print(f"\nSender: {sender_account}")
        print(f"Test: {test_name}")
        print(f"Pending files: {len(pending_copies)}")
        print(f"Already sent in this test: {already_sent_count}")
        for copy in pending_copies:
            print(f"  {copy.roll_number} | {copy.path} -> {copy.recipient}")
        if input("\nSend these files? [y/N]: ").strip().casefold() not in ("y", "yes"):
            print("Cancelled; nothing was sent.")
            return 0

        sent, failed, skipped = send_pending_copies(
            test_name,
            sender_account,
            pending_copies,
        )
        print(
            f"\nFinished. Sent: {sent}; failed: {failed}; "
            f"already sent/skipped: {skipped}; "
            f"large files left pending without Drive: {drive_skipped}."
        )
        print(f"Progress saved in {PROGRESS_FILE}.")
        return 1 if failed or drive_skipped else 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Cannot send student copies: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
