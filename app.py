import csv
import json
import os
import random
import re
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv

import mail1


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.getenv("MAIL_DASHBOARD_DATA_DIR", ROOT / "data")).expanduser()
TESTS_DIR = DATA_DIR / "tests"
MAPPINGS_FILE = DATA_DIR / "mappings.csv"
EVALUATORS_FILE = DATA_DIR / "evaluators.csv"
MAPPING_COLUMNS = ("roll_number", "mail_account")
MappingDict = dict[Literal["roll_number", "mail_account"], str]
MessageRecord = dict[str, Any]
WORKER_COUNT = 8
UPLOAD_SUBJECT_TEMPLATE = "Attachment for roll number {roll_number}"
UPLOAD_BODY = "Please find the attached file."


@asynccontextmanager
async def lifespan(_: FastAPI):
    _recover_interrupted_jobs()
    yield


app = FastAPI(title="Mail Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
_jobs = ThreadPoolExecutor(max_workers=WORKER_COUNT, thread_name_prefix="mail-job")
_locks_guard = threading.Lock()
_test_locks: dict[str, threading.RLock] = {}
_mappings_lock = threading.Lock()


class SearchRequest(BaseModel):
    test_name: str = ""
    account: str = Field(min_length=1)
    senders: list[str] = Field(default_factory=list)
    after_date: date


class MessageSelection(BaseModel):
    account: str
    uid: str


class DownloadRequest(BaseModel):
    output_folder: str = Field(min_length=1)
    messages: list[MessageSelection] = Field(min_length=1)


class MappingRow(BaseModel):
    roll_number: str
    mail_account: str


class MappingUpdate(BaseModel):
    rows: list[MappingRow]


class EvaluatorRow(BaseModel):
    name: str
    email: str


class EvaluatorUpdate(BaseModel):
    rows: list[EvaluatorRow]


class EvaluatorAssignment(BaseModel):
    roll_number: str
    file: str
    evaluator_name: str
    evaluator_email: str


class MappingDiscoveryRequest(BaseModel):
    folder: str = Field(min_length=1)


class UploadRequest(BaseModel):
    test_name: str = ""
    folder: str = Field(min_length=1)
    sender_account: str = Field(min_length=1)
    subject_template: str = Field(default=UPLOAD_SUBJECT_TEMPLATE, min_length=1)
    send_mode: Literal["student", "evaluator"] = "student"
    assignments: list[EvaluatorAssignment] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _accounts() -> dict[str, str]:
    configured = os.getenv("GMAIL_ACCOUNTS")
    if configured:
        try:
            value = json.loads(configured)
        except json.JSONDecodeError as error:
            raise HTTPException(
                status_code=500,
                detail="GMAIL_ACCOUNTS must be a JSON object mapping email addresses to app passwords.",
            ) from error
        if not isinstance(value, dict) or not all(
            isinstance(email_address, str)
            and isinstance(password, str)
            and email_address.strip()
            and password.strip()
            for email_address, password in value.items()
        ):
            raise HTTPException(
                status_code=500,
                detail="GMAIL_ACCOUNTS must map non-empty email addresses to non-empty app passwords.",
            )
        return {address: password for address, password in value.items()}

    user = os.getenv("GMAIL_USER")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if user and password:
        return {user: password}
    return {}


def _require_single_account(accounts: dict[str, str]) -> str:
    if len(accounts) != 1:
        raise HTTPException(
            status_code=500,
            detail="Configure exactly one Gmail account using GMAIL_USER/GMAIL_APP_PASSWORD or a one-entry GMAIL_ACCOUNTS object.",
        )
    return next(iter(accounts))


def _test_path(test_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(test_id))
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid test ID.") from error
    return TESTS_DIR / f"{normalized}.json"


def _lock_for(test_id: str) -> threading.RLock:
    with _locks_guard:
        return _test_locks.setdefault(test_id, threading.RLock())


def _read_test(test_id: str) -> dict[str, Any]:
    path = _test_path(test_id)
    with _lock_for(test_id):
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Test run not found.")
        try:
            test_run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=500, detail="Could not read the saved test run."
            ) from error
        original = json.dumps(test_run, sort_keys=True)
        legacy_errors = test_run.get("errors", [])
        if "search_fields" not in test_run:
            legacy_accounts = test_run.get("accounts", [])
            test_run["search_fields"] = {
                "account": legacy_accounts[0] if legacy_accounts else "",
                "senders": [],
                "download_folder": test_run.get("folder", ""),
                "after_date": test_run.get("after_date", ""),
            }
        test_run["search_fields"].setdefault("account", "")
        test_run["search_fields"].setdefault("senders", [])
        test_run.setdefault("test_name", "")
        if not test_run["search_fields"]["account"]:
            legacy_accounts = test_run["search_fields"].get("accounts", [])
            if legacy_accounts:
                test_run["search_fields"]["account"] = legacy_accounts[0]
        test_run["search_fields"].setdefault(
            "download_folder", test_run["search_fields"].get("folder", "")
        )
        test_run.setdefault(
            "download_fields",
            {
                "output_folder": test_run["search_fields"]["download_folder"],
                "selected_messages": [],
            },
        )
        test_run["download_fields"].setdefault(
            "output_folder", test_run["search_fields"]["download_folder"]
        )
        test_run.setdefault(
            "upload_fields", {"folder": "", "sender_account": ""}
        )
        test_run["upload_fields"].setdefault("send_mode", "student")
        test_run["upload_fields"].setdefault("evaluators", [])
        test_run["upload_fields"].setdefault("assignments", [])
        test_run["upload_fields"].setdefault("files_by_roll", {})
        test_run["upload_fields"].setdefault("file_status", [])
        test_run["upload_fields"].setdefault("ignored_files", [])
        test_run["upload_fields"].setdefault("mappings", [])
        test_run["upload_fields"].setdefault(
            "subject_template", UPLOAD_SUBJECT_TEMPLATE
        )
        test_run["upload_fields"].setdefault("body", UPLOAD_BODY)
        messages = test_run.setdefault("messages", [])
        for message in messages:
            message.setdefault("downloaded", False)
            message.setdefault("downloaded_files", [])
            message.setdefault("updated", False)
        test_run.setdefault("updates", [])
        if not test_run["upload_fields"]["file_status"]:
            sent_files = {
                update.get("file")
                for update in test_run["updates"]
                if update.get("file")
            }
            test_run["upload_fields"]["file_status"] = [
                {
                    "file": filepath,
                    "roll_number": roll_number,
                    "recipient": "",
                    "status": "sent" if filepath in sent_files else "not sent",
                }
                for roll_number, filepaths in test_run["upload_fields"][
                    "files_by_roll"
                ].items()
                for filepath in filepaths
            ]
        operations = test_run.setdefault("operations", {})
        for name in ("search", "download", "upload"):
            operation = operations.setdefault(name, _operation())
            operation.setdefault("status", "completed" if name == "search" else "idle")
            operation.setdefault("started_at", None)
            operation.setdefault("completed_at", None)
            operation.setdefault(
                "progress",
                {
                    "completed": len(messages) if name == "search" else 0,
                    "total": 1
                    if name == "search"
                    else 0,
                },
            )
            operation.setdefault("errors", [])
            operation.setdefault("unmatched", [])
        if legacy_errors and not operations["search"]["errors"]:
            operations["search"]["errors"] = legacy_errors
            operations["search"]["status"] = "completed_with_errors"
        test_run.pop("errors", None)
        _refresh_counts(test_run)
        if json.dumps(test_run, sort_keys=True) != original:
            _write_test_unlocked(test_run)
        return test_run


def _recover_interrupted_jobs() -> None:
    if not TESTS_DIR.exists():
        return
    for path in TESTS_DIR.glob("*.json"):
        test_id = path.stem
        test_run = _read_test(test_id)
        interrupted = [
            name
            for name, operation in test_run["operations"].items()
            if operation["status"] == "running"
        ]
        if not interrupted:
            continue

        def mark_interrupted(current: dict[str, Any]) -> None:
            for name in interrupted:
                operation = current["operations"][name]
                operation["status"] = "interrupted"
                operation["completed_at"] = _now()
                operation["errors"].append(
                    {"message": "The server restarted while this operation was running."}
                )

        _update_test(test_id, mark_interrupted)


def _write_test_unlocked(test_run: dict[str, Any]) -> None:
    TESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _test_path(test_run["id"])
    temp_path = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(test_run, indent=2), encoding="utf-8")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_test(test_run: dict[str, Any]) -> None:
    with _lock_for(test_run["id"]):
        _write_test_unlocked(test_run)


def _update_test(test_id: str, update: Any) -> dict[str, Any]:
    with _lock_for(test_id):
        test_run = _read_test(test_id)
        update(test_run)
        _write_test_unlocked(test_run)
        return test_run


def _operation(status: str = "idle") -> dict[str, Any]:
    return {
        "status": status,
        "started_at": None,
        "completed_at": None,
        "progress": {"completed": 0, "total": 0},
        "errors": [],
        "unmatched": [],
    }


def _refresh_counts(test_run: dict[str, Any]) -> None:
    messages = test_run["messages"]
    test_run["counts"] = {
        "found": len(messages),
        "downloaded": sum(1 for message in messages if message.get("downloaded")),
        "downloaded_files": sum(
            len(message.get("downloaded_files", [])) for message in messages
        ),
        "updated": len(test_run.get("updates", [])),
    }


def _set_operation(
    test_id: str,
    name: Literal["search", "download", "upload"],
    **changes: Any,
) -> dict[str, Any]:
    def update(test_run: dict[str, Any]) -> None:
        test_run["operations"][name].update(changes)

    return _update_test(test_id, update)


def _finish_operation(
    test_id: str,
    name: Literal["search", "download", "upload"],
    status: str,
) -> None:
    _set_operation(test_id, name, status=status, completed_at=_now())


def _run_job(
    test_id: str,
    operation: Literal["search", "download", "upload"],
    job: Callable[..., None],
    *args: Any,
) -> None:
    try:
        job(test_id, *args)
    except Exception as error:
        _set_operation(
            test_id,
            operation,
            status="failed",
            completed_at=_now(),
            errors=[{"message": str(error)}],
        )


def _submit_job(
    test_id: str,
    operation: Literal["search", "download", "upload"],
    job: Callable[..., None],
    *args: Any,
) -> None:
    _jobs.submit(_run_job, test_id, operation, job, *args)


def _read_mappings() -> list[MappingDict]:
    with _mappings_lock:
        if not MAPPINGS_FILE.exists():
            return []
        try:
            with MAPPINGS_FILE.open(newline="", encoding="utf-8") as csv_file:
                reader = csv.DictReader(csv_file)
                if reader.fieldnames not in (
                    list(MAPPING_COLUMNS),
                    ["roll_number", "mail_account", "sent"],
                    ["attachment_name", "mail_account", "sent"],
                    ["attachment_name", "mail_account"],
                ):
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "mappings.csv must have these columns in order: "
                            + ", ".join(MAPPING_COLUMNS)
                        ),
                    )
                rows = [
                    MappingDict(
                        {
                            "roll_number": (
                                row.get("roll_number")
                                or row.get("attachment_name")
                                or ""
                            ).strip(),
                            "mail_account": (row.get("mail_account") or "").strip(),
                        }
                    )
                    for row in reader
                ]
            if reader.fieldnames != list(MAPPING_COLUMNS):
                _write_mappings_unlocked(rows)
            return rows
        except OSError as error:
            raise HTTPException(
                status_code=500, detail="Could not read mappings.csv."
            ) from error


def _write_mappings_unlocked(rows: list[MappingDict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = MAPPINGS_FILE.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=MAPPING_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        temp_path.replace(MAPPINGS_FILE)
    except OSError as error:
        raise HTTPException(
            status_code=500, detail="Could not save mappings.csv."
        ) from error
    finally:
        temp_path.unlink(missing_ok=True)


def _write_mappings(rows: list[MappingDict]) -> None:
    with _mappings_lock:
        _write_mappings_unlocked(rows)


def _read_evaluators() -> list[dict[str, str]]:
    with _mappings_lock:
        if not EVALUATORS_FILE.exists():
            return []
        try:
            with EVALUATORS_FILE.open(newline="", encoding="utf-8") as csv_file:
                reader = csv.DictReader(csv_file)
                if reader.fieldnames != ["name", "email"]:
                    raise HTTPException(
                        status_code=500,
                        detail="evaluators.csv must have columns in order: name,email",
                    )
                return [
                    {
                        "name": (row.get("name") or "").strip(),
                        "email": (row.get("email") or "").strip(),
                    }
                    for row in reader
                ]
        except OSError as error:
            raise HTTPException(
                status_code=500, detail="Could not read evaluators.csv."
            ) from error


def _write_evaluators(rows: list[dict[str, str]]) -> None:
    with _mappings_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = EVALUATORS_FILE.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=("name", "email"))
                writer.writeheader()
                for row in rows:
                    writer.writerow({"name": row["name"], "email": row["email"]})
            temp_path.replace(EVALUATORS_FILE)
        except OSError as error:
            raise HTTPException(
                status_code=500, detail="Could not save evaluators.csv."
            ) from error
        finally:
            temp_path.unlink(missing_ok=True)


def _find_upload_files(folder: Path) -> tuple[dict[str, list[Path]], list[str]]:
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail=f"Upload folder does not exist: {folder}")

    files_by_roll: dict[str, list[Path]] = {}
    ignored: list[str] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        roll_numbers = re.findall(r"(?<!\d)\d{5}(?!\d)", path.stem)
        if len(roll_numbers) != 1:
            ignored.append(path.name)
            continue
        files_by_roll.setdefault(roll_numbers[0], []).append(path)
    return files_by_roll, ignored


def _prepare_upload(
    request: UploadRequest,
) -> tuple[
    Path,
    dict[str, list[Path]],
    list[str],
    list[MappingDict],
    list[dict[str, str]],
]:
    accounts = _accounts()
    _require_single_account(accounts)
    if request.sender_account not in accounts:
        raise HTTPException(
            status_code=400,
            detail=f"Sender account is not configured: {request.sender_account}",
        )
    folder = Path(request.folder).expanduser().resolve()
    if not folder.is_dir():
        raise HTTPException(
            status_code=400, detail=f"Upload folder does not exist: {folder}"
        )
    files_by_roll, ignored_files = _find_upload_files(folder)
    mappings = _read_mappings()
    evaluators = _read_evaluators() if request.send_mode == "evaluator" else []
    if request.send_mode == "student":
        if not any(
            re.fullmatch(r"\d{5}", row["roll_number"])
            and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", row["mail_account"])
            and row["roll_number"] in files_by_roll
            for row in mappings
        ):
            raise HTTPException(
                status_code=400,
                detail="Add a valid recipient mapping for at least one file in the selected folder before starting the upload.",
            )
    else:
        if not evaluators:
            raise HTTPException(
                status_code=400,
                detail="Add at least one evaluator name and email before starting random distribution.",
            )
        if any(
            not evaluator["name"]
            or not re.fullmatch(
                r"[^@\s]+@[^@\s]+\.[^@\s]+", evaluator["email"]
            )
            for evaluator in evaluators
        ):
            raise HTTPException(
                status_code=400,
                detail="Every evaluator needs a name and valid email address.",
            )
        if not any(len(paths) == 1 for paths in files_by_roll.values()):
            raise HTTPException(
                status_code=400,
                detail="No uniquely identified student files are available to distribute.",
            )
    return folder, files_by_roll, ignored_files, mappings, evaluators


def _assign_evaluator_files(
    files_by_roll: dict[str, list[Path]], evaluators: list[dict[str, str]]
) -> list[dict[str, str]]:
    files = [
        (roll_number, str(paths[0]))
        for roll_number, paths in files_by_roll.items()
        if len(paths) == 1
    ]
    random.SystemRandom().shuffle(files)
    shuffled_evaluators = list(evaluators)
    random.SystemRandom().shuffle(shuffled_evaluators)
    return [
        {
            "roll_number": roll_number,
            "file": filepath,
            "evaluator_name": evaluator["name"],
            "evaluator_email": evaluator["email"],
            "status": "assigned",
        }
        for index, (roll_number, filepath) in enumerate(files)
        for evaluator in [shuffled_evaluators[index % len(shuffled_evaluators)]]
    ]


def _validated_evaluator_assignments(
    files_by_roll: dict[str, list[Path]],
    evaluators: list[dict[str, str]],
    assignments: list[EvaluatorAssignment],
) -> list[dict[str, str]]:
    expected_files = {
        (roll_number, str(paths[0]))
        for roll_number, paths in files_by_roll.items()
        if len(paths) == 1
    }
    evaluator_by_identity = {
        (evaluator["name"], evaluator["email"]): evaluator
        for evaluator in evaluators
    }
    received_files: set[tuple[str, str]] = set()
    validated: list[dict[str, str]] = []
    for assignment in assignments:
        file_key = (assignment.roll_number, str(Path(assignment.file).resolve()))
        if file_key not in expected_files or file_key in received_files:
            raise HTTPException(
                status_code=400,
                detail="Evaluator assignments must include each eligible file exactly once.",
            )
        evaluator = evaluator_by_identity.get(
            (assignment.evaluator_name, assignment.evaluator_email)
        )
        if evaluator is None:
            raise HTTPException(
                status_code=400,
                detail="Every assigned evaluator must be in the saved evaluator list.",
            )
        received_files.add(file_key)
        validated.append(
            {
                "roll_number": assignment.roll_number,
                "file": file_key[1],
                "evaluator_name": evaluator["name"],
                "evaluator_email": evaluator["email"],
                "status": "assigned",
            }
        )
    if received_files != expected_files:
        raise HTTPException(
            status_code=400,
            detail="Evaluator assignments must include each eligible file exactly once.",
        )
    return validated


def _file_status_rows(
    folder: Path,
    files_by_roll: dict[str, list[Path]],
    mappings: list[MappingDict],
    assignments: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    recipient_by_roll = {
        row["roll_number"]: row["mail_account"] for row in mappings
    }
    roll_by_file = {
        str(path): roll_number
        for roll_number, paths in files_by_roll.items()
        for path in paths
    }
    assignment_by_file = {
        assignment["file"]: assignment for assignment in assignments or []
    }
    status_rows = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        roll_number = roll_by_file.get(str(path), "")
        assignment = assignment_by_file.get(str(path))
        status_rows.append({
            "file": str(path),
            "roll_number": roll_number,
            "recipient": (
                assignment["evaluator_email"]
                if assignment
                else recipient_by_roll.get(roll_number, "")
            ),
            "evaluator_name": assignment["evaluator_name"] if assignment else "",
            "status": (
                "ignored_no_roll_number"
                if not roll_number
                else "ambiguous"
                if len(files_by_roll[roll_number]) > 1
                else "assigned"
                if assignment
                else "not sent"
            ),
        })
    return status_rows


def _pick_folder(purpose: Literal["download", "upload"]) -> str:
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "folder_picker.py"), purpose],
            capture_output=True,
            check=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as error:
        raise HTTPException(
            status_code=408, detail="Folder selection timed out."
        ) from error
    except (OSError, subprocess.CalledProcessError) as error:
        detail = (
            error.stderr.strip()
            if isinstance(error, subprocess.CalledProcessError) and error.stderr
            else str(error)
        )
        raise HTTPException(
            status_code=500, detail=f"Could not open the system folder picker: {detail}"
        ) from error
    return result.stdout.strip()


def _search_one_account(
    account: str, password: str, after_date: date, senders: list[str]
) -> list[dict[str, str]]:
    return mail1.search_emails(account, password, after_date, senders)


def _run_search(test_id: str) -> None:
    test_run = _read_test(test_id)
    credentials = _accounts()
    account = test_run["search_fields"]["account"]
    senders = test_run["search_fields"].get("senders", [])
    after_date = date.fromisoformat(test_run["search_fields"]["after_date"])
    messages: list[MessageRecord] = []
    errors: list[dict[str, str]] = []
    operation = test_run["operations"]["search"]
    operation["progress"]["total"] = 1
    _set_operation(
        test_id,
        "search",
        progress={"completed": 0, "total": 1},
    )

    try:
        with ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="gmail-search",
        ) as pool:
            future = pool.submit(
                _search_one_account,
                account,
                credentials[account],
                after_date,
                senders,
            )
            try:
                results = future.result()
                messages.extend(
                    [
                        {
                            **result,
                            "account": account,
                            "downloaded": False,
                            "downloaded_files": [],
                            "updated": False,
                        }
                        for result in results
                    ]
                )
            except Exception as error:
                errors.append({"account": account, "message": str(error)})
            _set_operation(
                test_id,
                "search",
                progress={"completed": 1, "total": 1},
                errors=errors,
            )

        def save_results(current: dict[str, Any]) -> None:
            current["messages"] = messages
            current["operations"]["search"]["errors"] = errors
            current["operations"]["search"]["status"] = (
                "completed_with_errors" if errors else "completed"
            )
            current["operations"]["search"]["completed_at"] = _now()
            _refresh_counts(current)

        _update_test(test_id, save_results)
    except Exception as error:
        _set_operation(
            test_id,
            "search",
            status="failed",
            completed_at=_now(),
            errors=[{"message": str(error)}],
        )


def _run_download(test_id: str, selections: list[MessageSelection]) -> None:
    test_run = _read_test(test_id)
    credentials = _accounts()
    folder = Path(test_run["download_fields"]["output_folder"])
    selections_by_key = {(item.account, item.uid) for item in selections}
    messages = [
        message
        for message in test_run["messages"]
        if (message["account"], message["uid"]) in selections_by_key
    ]
    errors: list[dict[str, str]] = []
    operation = test_run["operations"]["download"]
    operation["progress"]["total"] = len(messages)
    _set_operation(
        test_id,
        "download",
        progress=operation["progress"],
        selected=[{"account": item.account, "uid": item.uid} for item in selections],
    )

    try:
        with ThreadPoolExecutor(
            max_workers=min(WORKER_COUNT, max(1, len(messages))),
            thread_name_prefix="gmail-download",
        ) as pool:
            futures = {
                pool.submit(
                    mail1.download_attachments,
                    message["account"],
                    credentials[message["account"]],
                    message["uid"],
                    folder,
                ): message
                for message in messages
            }
            for future in as_completed(futures):
                message = futures[future]
                key = (message["account"], message["uid"])
                try:
                    saved_files = future.result()

                    def mark_downloaded(current: dict[str, Any]) -> None:
                        for item in current["messages"]:
                            if (item["account"], item["uid"]) == key:
                                item["downloaded"] = True
                                item["downloaded_files"] = saved_files
                                break
                        _refresh_counts(current)

                    _update_test(test_id, mark_downloaded)
                except Exception as error:
                    errors.append(
                        {
                            "account": message["account"],
                            "uid": message["uid"],
                            "message": str(error),
                        }
                    )

                operation = _read_test(test_id)["operations"]["download"]
                _set_operation(
                    test_id,
                    "download",
                    progress={
                        "completed": operation["progress"]["completed"] + 1,
                        "total": len(messages),
                    },
                    errors=errors,
                )

        _finish_operation(
            test_id, "download", "completed_with_errors" if errors else "completed"
        )
    except Exception as error:
        _set_operation(
            test_id,
            "download",
            status="failed",
            completed_at=_now(),
            errors=[{"message": str(error)}],
        )


def _run_upload(test_id: str) -> None:
    test_run = _read_test(test_id)
    try:
        credentials = _accounts()
        upload_fields = test_run["upload_fields"]
        mapping_rows = upload_fields.get("mappings")
        send_mode = upload_fields.get("send_mode", "student")
        assignments = upload_fields.get("assignments", [])
        if send_mode == "evaluator":
            mapping_rows = [
                {
                    "roll_number": assignment["roll_number"],
                    "mail_account": assignment["evaluator_email"],
                }
                for assignment in assignments
            ]
        elif mapping_rows is None:
            mapping_rows = _read_mappings()
        mappings = [
            MappingDict(
                {
                    "roll_number": row["roll_number"],
                    "mail_account": row["mail_account"],
                }
            )
            for row in mapping_rows
        ]
        files_by_roll, _ = _find_upload_files(Path(upload_fields["folder"]))
        sender_account = upload_fields["sender_account"]
        password = credentials[sender_account]
        subject_template = upload_fields.get(
            "subject_template", UPLOAD_SUBJECT_TEMPLATE
        )
        body = upload_fields.get("body", UPLOAD_BODY)
    except Exception as error:
        _set_operation(
            test_id,
            "upload",
            status="failed",
            completed_at=_now(),
            errors=[{"message": str(error)}],
        )
        return
    errors: list[dict[str, Any]] = []
    unmatched: list[str] = []
    ready: list[tuple[MappingDict, Path]] = []
    file_status = {
        row["file"]: row
        for row in upload_fields.get("file_status", [])
    }
    total = 0
    for row in mappings:
        matches = files_by_roll.get(row["roll_number"], [])
        if not matches:
            continue
        if not row["mail_account"]:
            for path in matches:
                status_row = file_status.get(str(path))
                if status_row:
                    status_row.update(status="missing_mapping", recipient="")
            continue
        total += len(matches)
        if len(matches) > 1:
            unmatched.append(row["roll_number"])
            for path in matches:
                status_row = file_status.get(str(path))
                if status_row:
                    status_row.update(
                        status="ambiguous",
                        recipient=row["mail_account"],
                    )
            errors.append(
                {
                    "roll_number": row["roll_number"],
                    "message": "Multiple files have this roll number; none were sent.",
                    "files": [path.name for path in matches],
                }
            )
            continue
        path = matches[0]
        status_row = file_status.get(str(path))
        if status_row and status_row["status"] == "sent":
            continue
        if status_row:
            status_row.update(status="sending", recipient=row["mail_account"])
        ready.append((row, path))
    _update_test(
        test_id,
        lambda current: current["upload_fields"].update(file_status=list(file_status.values())),
    )
    _set_operation(
        test_id,
        "upload",
        progress={"completed": total - len(ready), "total": total},
        errors=errors,
        unmatched=unmatched,
    )

    try:
        with ThreadPoolExecutor(
            max_workers=min(WORKER_COUNT, max(1, len(ready))),
            thread_name_prefix="gmail-upload",
        ) as pool:
            futures = {
                pool.submit(
                    mail1.send_attachment,
                    sender_account,
                    password,
                    row["mail_account"],
                    row["roll_number"],
                    attachment_path,
                    subject_template.format(roll_number=row["roll_number"]),
                    body,
                ): (row, attachment_path)
                for row, attachment_path in ready
            }
            for future in as_completed(futures):
                row, attachment_path = futures[future]
                try:
                    future.result()

                    def mark_updated(current: dict[str, Any]) -> None:
                        for status_row in current["upload_fields"]["file_status"]:
                            if status_row["file"] == str(attachment_path):
                                status_row.update(
                                    status="sent",
                                    recipient=row["mail_account"],
                                    error="",
                                )
                                break
                        for assignment in current["upload_fields"].get(
                            "assignments", []
                        ):
                            if assignment["file"] == str(attachment_path):
                                assignment["status"] = "sent"
                                break
                        current.setdefault("updates", []).append(
                            {
                                "roll_number": row["roll_number"],
                                "recipient": row["mail_account"],
                                "sender_account": sender_account,
                                "file": str(attachment_path),
                                "updated_at": _now(),
                            }
                        )
                        _refresh_counts(current)

                    _update_test(test_id, mark_updated)
                except Exception as error:
                    errors.append(
                        {
                            "roll_number": row["roll_number"],
                            "recipient": row["mail_account"],
                            "message": str(error),
                        }
                    )

                    def mark_failed(current: dict[str, Any]) -> None:
                        for status_row in current["upload_fields"]["file_status"]:
                            if status_row["file"] == str(attachment_path):
                                status_row.update(
                                    status="failed",
                                    recipient=row["mail_account"],
                                    error=str(error),
                                )
                                break
                        for assignment in current["upload_fields"].get(
                            "assignments", []
                        ):
                            if assignment["file"] == str(attachment_path):
                                assignment.update(status="failed", error=str(error))
                                break

                    _update_test(test_id, mark_failed)

                operation = _read_test(test_id)["operations"]["upload"]
                _set_operation(
                    test_id,
                    "upload",
                    progress={
                        "completed": operation["progress"]["completed"] + 1,
                        "total": total,
                    },
                    errors=errors,
                    unmatched=unmatched,
                )

        _finish_operation(
            test_id,
            "upload",
            "completed_with_errors" if errors or unmatched else "completed",
        )
    except Exception as error:
        _set_operation(
            test_id,
            "upload",
            status="failed",
            completed_at=_now(),
            errors=[{"message": str(error)}],
        )


@app.get("/")
def downloader_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/send")
def sender_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "send.html")


@app.get("/api/accounts")
def list_accounts() -> dict[str, list[str]]:
    accounts = _accounts()
    if not accounts:
        return {"accounts": []}
    return {"accounts": [_require_single_account(accounts)]}


@app.get("/api/folders/pick")
@app.post("/api/folders/pick")
def pick_folder(purpose: Literal["download", "upload"]) -> dict[str, str | None]:
    folder = _pick_folder(purpose)
    return {"folder": str(Path(folder).resolve()) if folder else None}


def _list_test_records(kind: Literal["download", "upload"]) -> dict[str, list[dict[str, Any]]]:
    TESTS_DIR.mkdir(parents=True, exist_ok=True)
    tests = []
    for path in sorted(
        TESTS_DIR.glob("*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        test_run = _read_test(path.stem)
        test_kind = test_run.get("kind")
        if test_kind is None:
            test_kind = (
                "upload"
                if test_run["operations"]["upload"]["status"] != "idle"
                or test_run.get("updates")
                else "download"
            )
        if test_kind != kind:
            continue
        tests.append(
            {
                "id": test_run["id"],
                "test_name": test_run.get("test_name", ""),
                "upload_mode": test_run.get("upload_fields", {}).get(
                    "send_mode", "student"
                ),
                "created_at": test_run["created_at"],
                "message_count": len(test_run["messages"]),
                "counts": test_run["counts"],
                "operations": test_run["operations"],
            }
        )
    return {"tests": tests}


@app.get("/api/tests")
def list_tests() -> dict[str, list[dict[str, Any]]]:
    return _list_test_records("download")


@app.get("/api/upload-runs")
def list_upload_runs() -> dict[str, list[dict[str, Any]]]:
    return _list_test_records("upload")


@app.post("/api/tests", status_code=202)
def create_test(request: SearchRequest) -> dict[str, Any]:
    accounts = _accounts()
    _require_single_account(accounts)
    if request.account not in accounts:
        raise HTTPException(
            status_code=400,
            detail=f"Account is not configured: {request.account}",
        )

    if any(
        not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", sender)
        for sender in request.senders
    ):
        raise HTTPException(
            status_code=400, detail="Sender filters must be valid email addresses."
        )
    if len(set(request.senders)) != len(request.senders):
        raise HTTPException(status_code=400, detail="Sender filters must not contain duplicates.")

    test_id = str(uuid.uuid4())
    test_run: dict[str, Any] = {
        "id": test_id,
        "kind": "download",
        "test_name": request.test_name.strip(),
        "created_at": _now(),
        "search_fields": {
            "account": request.account,
            "senders": request.senders,
            "download_folder": "",
            "after_date": request.after_date.isoformat(),
        },
        "download_fields": {
            "output_folder": "",
            "selected_messages": [],
        },
        "upload_fields": {
            "send_mode": "student",
            "folder": "",
            "sender_account": "",
            "evaluators": [],
            "assignments": [],
            "files_by_roll": {},
            "file_status": [],
            "ignored_files": [],
            "mappings": [],
            "subject_template": UPLOAD_SUBJECT_TEMPLATE,
            "body": UPLOAD_BODY,
        },
        "messages": [],
        "updates": [],
        "counts": {
            "found": 0,
            "downloaded": 0,
            "downloaded_files": 0,
            "updated": 0,
        },
        "operations": {
            "search": {
                **_operation("running"),
                "started_at": _now(),
                "progress": {"completed": 0, "total": 1},
            },
            "download": _operation(),
            "upload": _operation(),
        },
    }
    _write_test(test_run)
    _submit_job(test_id, "search", _run_search)
    return _read_test(test_id)


@app.get("/api/tests/{test_id}")
def get_test(test_id: str) -> dict[str, Any]:
    return _read_test(test_id)


@app.delete("/api/tests/{test_id}/messages/{account}/{uid}")
def remove_message(test_id: str, account: str, uid: str) -> dict[str, Any]:
    test_run = _read_test(test_id)
    if any(
        operation["status"] == "running"
        for operation in test_run["operations"].values()
    ):
        raise HTTPException(status_code=409, detail="Wait for active operations to complete.")

    def remove(current: dict[str, Any]) -> None:
        before = len(current["messages"])
        current["messages"] = [
            message
            for message in current["messages"]
            if not (message["account"] == account and message["uid"] == uid)
        ]
        if len(current["messages"]) == before:
            raise HTTPException(status_code=404, detail="Message not found in this test run.")
        _refresh_counts(current)

    return _update_test(test_id, remove)


@app.post("/api/tests/{test_id}/download", status_code=202)
def download_messages(test_id: str, request: DownloadRequest) -> dict[str, Any]:
    with _lock_for(test_id):
        test_run = _read_test(test_id)
        if test_run["operations"]["search"]["status"] == "running":
            raise HTTPException(status_code=409, detail="Wait for search to complete.")
        if test_run["operations"]["download"]["status"] == "running":
            raise HTTPException(
                status_code=409, detail="A download is already running for this test."
            )

        known_messages = {
            (message["account"], message["uid"]) for message in test_run["messages"]
        }
        invalid = [
            {"account": item.account, "uid": item.uid}
            for item in request.messages
            if (item.account, item.uid) not in known_messages
        ]
        if invalid:
            raise HTTPException(
                status_code=400, detail=f"Messages are not in this test run: {invalid}"
            )

        folder = Path(request.output_folder).expanduser().resolve()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise HTTPException(
                status_code=400, detail=f"Cannot use output folder: {error}"
            ) from error

        selected_messages = [
            {"account": item.account, "uid": item.uid}
            for item in request.messages
        ]
        _set_operation(
            test_id,
            "download",
            status="running",
            started_at=_now(),
            completed_at=None,
            progress={"completed": 0, "total": len(request.messages)},
            selected=selected_messages,
            errors=[],
            unmatched=[],
        )
        _update_test(
            test_id,
            lambda current: current["download_fields"].update(
                output_folder=str(folder),
                selected_messages=selected_messages,
            ),
        )
        _submit_job(test_id, "download", _run_download, request.messages)
        return _read_test(test_id)


@app.get("/api/mappings")
def get_mappings() -> dict[str, Any]:
    return {"columns": list(MAPPING_COLUMNS), "rows": _read_mappings()}


@app.get("/api/evaluators")
def get_evaluators() -> dict[str, Any]:
    return {"rows": _read_evaluators()}


@app.put("/api/evaluators")
def update_evaluators(request: EvaluatorUpdate) -> dict[str, Any]:
    seen_emails: set[str] = set()
    rows: list[dict[str, str]] = []
    for evaluator in request.rows:
        name = evaluator.name.strip()
        email_address = evaluator.email.strip()
        if not name and not email_address:
            continue
        if not name:
            raise HTTPException(status_code=400, detail="Every evaluator needs a name.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email_address):
            raise HTTPException(
                status_code=400,
                detail=f"Evaluator email is invalid: {email_address}",
            )
        if email_address.casefold() in seen_emails:
            raise HTTPException(
                status_code=400,
                detail=f"Evaluator email is duplicated: {email_address}",
            )
        seen_emails.add(email_address.casefold())
        rows.append({"name": name, "email": email_address})
    _write_evaluators(rows)
    return {"rows": _read_evaluators()}


@app.post("/api/upload-preview")
def preview_upload(request: UploadRequest) -> dict[str, Any]:
    if request.send_mode != "evaluator":
        raise HTTPException(
            status_code=400,
            detail="Assignment preview is only available in evaluator mode.",
        )
    folder, files_by_roll, ignored_files, mappings, evaluators = _prepare_upload(
        request
    )
    assignments = _assign_evaluator_files(files_by_roll, evaluators)
    return {
        "folder": str(folder),
        "assignments": assignments,
        "file_status": _file_status_rows(
            folder, files_by_roll, mappings, assignments
        ),
        "ignored_files": ignored_files,
    }


@app.post("/api/mappings/discover")
def discover_mappings(request: MappingDiscoveryRequest) -> dict[str, Any]:
    folder = Path(request.folder).expanduser().resolve()
    files_by_roll, ignored = _find_upload_files(folder)
    existing = {row["roll_number"]: row for row in _read_mappings()}
    rows: list[MappingDict] = list(existing.values())
    for roll_number in sorted(files_by_roll):
        prior = existing.get(roll_number, {})
        discovered = MappingDict(
            {
                "roll_number": roll_number,
                "mail_account": prior.get("mail_account", ""),
            }
        )
        if roll_number in existing:
            rows = [
                discovered if row["roll_number"] == roll_number else row
                for row in rows
            ]
        else:
            rows.append(discovered)
    _write_mappings(rows)
    return {
        "folder": str(folder),
        "rows": _read_mappings(),
        "files": [
            {
                "roll_number": roll_number,
                "files": [path.name for path in paths],
                "status": "ambiguous" if len(paths) > 1 else "ready",
            }
            for roll_number, paths in sorted(files_by_roll.items())
        ],
        "ignored_files": ignored,
    }


@app.put("/api/mappings")
def update_mappings(request: MappingUpdate) -> dict[str, Any]:
    seen_roll_numbers: set[str] = set()
    for row in request.rows:
        if row.roll_number.strip() and not re.fullmatch(r"\d{5}", row.roll_number.strip()):
            raise HTTPException(
                status_code=400,
                detail=f"Roll number must be exactly 5 digits: {row.roll_number}",
            )
        if row.roll_number.strip() and row.roll_number.strip() in seen_roll_numbers:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate roll number: {row.roll_number}",
            )
        if row.roll_number.strip():
            seen_roll_numbers.add(row.roll_number.strip())
        if row.roll_number.strip() and row.mail_account.strip() and not re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+", row.mail_account.strip()
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Recipient must be an email address: {row.mail_account}",
            )
    rows: list[MappingDict] = [
        MappingDict(
            {
                "roll_number": row.roll_number.strip(),
                "mail_account": row.mail_account.strip(),
            }
        )
        for row in request.rows
    ]
    _write_mappings(rows)
    return {"rows": _read_mappings()}


@app.post("/api/tests/{test_id}/send-mappings", status_code=202)
def send_mappings(test_id: str, request: UploadRequest) -> dict[str, Any]:
    with _lock_for(test_id):
        test_run = _read_test(test_id)
        if any(
            test_run["operations"][name]["status"] == "running"
            for name in ("search", "download")
        ):
            raise HTTPException(
                status_code=409, detail="Wait for search and download to complete."
            )
        if test_run["operations"]["upload"]["status"] == "running":
            raise HTTPException(
                status_code=409, detail="An upload is already running for this test."
            )
        (
            folder,
            files_by_roll,
            ignored_files,
            mapping_snapshot,
            evaluator_snapshot,
        ) = _prepare_upload(request)
        previous_upload = test_run.get("upload_fields", {})
        if test_run.get("updates") and (
            previous_upload.get("folder") != str(folder)
            or previous_upload.get("sender_account") != request.sender_account
        ):
            raise HTTPException(
                status_code=409,
                detail="This test ID already has successful uploads. Start a new test for a different upload folder or sender.",
            )

        assignments = previous_upload.get("assignments", [])
        if request.send_mode == "evaluator" and not assignments:
            assignments = _assign_evaluator_files(files_by_roll, evaluator_snapshot)
        file_status = _file_status_rows(
            folder,
            files_by_roll,
            mapping_snapshot,
            assignments if request.send_mode == "evaluator" else None,
        )
        if previous_upload.get("folder") == str(folder):
            previous_file_status = {
                item["file"]: item
                for item in previous_upload.get("file_status", [])
            }
            for item in file_status:
                previous = previous_file_status.get(item["file"])
                if previous and previous.get("status") == "sent":
                    item.update(
                        status="sent",
                        recipient=previous.get("recipient", item["recipient"]),
                    )

        _update_test(
            test_id,
            lambda current: current.update(
                kind="upload",
                test_name=request.test_name.strip() or current.get("test_name", ""),
                upload_fields={
                    "send_mode": request.send_mode,
                    "folder": str(folder),
                    "sender_account": request.sender_account,
                    "evaluators": evaluator_snapshot,
                    "assignments": assignments,
                    "file_status": file_status,
                    "files_by_roll": {
                        roll_number: [str(path) for path in paths]
                        for roll_number, paths in files_by_roll.items()
                    },
                    "ignored_files": ignored_files,
                    "mappings": mapping_snapshot,
                    "subject_template": request.subject_template.strip(),
                    "body": UPLOAD_BODY,
                }
            ),
        )
        _set_operation(
            test_id,
            "upload",
            status="running",
            started_at=_now(),
            completed_at=None,
            progress={"completed": 0, "total": 0},
            errors=[],
            unmatched=[],
        )
        _submit_job(test_id, "upload", _run_upload)
        return _read_test(test_id)


@app.post("/api/upload-runs", status_code=202)
def create_upload_run(request: UploadRequest) -> dict[str, Any]:
    (
        folder,
        files_by_roll,
        ignored_files,
        mapping_snapshot,
        evaluator_snapshot,
    ) = _prepare_upload(request)
    assignments = (
        _validated_evaluator_assignments(
            files_by_roll, evaluator_snapshot, request.assignments
        )
        if request.send_mode == "evaluator" and request.assignments
        else         _validated_evaluator_assignments(
            files_by_roll, evaluator_snapshot, request.assignments
        )
        if request.send_mode == "evaluator" and request.assignments
        else _assign_evaluator_files(files_by_roll, evaluator_snapshot)
        if request.send_mode == "evaluator"
        else []
    )
    test_id = str(uuid.uuid4())
    created_at = _now()
    test_run: dict[str, Any] = {
        "id": test_id,
        "kind": "upload",
        "test_name": request.test_name.strip(),
        "created_at": created_at,
        "search_fields": {
            "account": "",
            "senders": [],
            "download_folder": "",
            "after_date": "",
        },
        "download_fields": {"output_folder": "", "selected_messages": []},
        "upload_fields": {
            "send_mode": request.send_mode,
            "folder": str(folder),
            "sender_account": request.sender_account,
            "evaluators": evaluator_snapshot,
            "assignments": assignments,
            "files_by_roll": {
                roll_number: [str(path) for path in paths]
                for roll_number, paths in files_by_roll.items()
            },
            "file_status": _file_status_rows(
                folder,
                files_by_roll,
                mapping_snapshot,
                assignments if request.send_mode == "evaluator" else None,
            ),
            "ignored_files": ignored_files,
            "mappings": mapping_snapshot,
            "subject_template": UPLOAD_SUBJECT_TEMPLATE,
            "body": UPLOAD_BODY,
        },
        "messages": [],
        "updates": [],
        "counts": {"found": 0, "downloaded": 0, "downloaded_files": 0, "updated": 0},
        "operations": {
            "search": _operation("completed"),
            "download": _operation("completed"),
            "upload": _operation(),
        },
    }
    _write_test(test_run)
    return send_mappings(test_id, request)


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="dashboard")
