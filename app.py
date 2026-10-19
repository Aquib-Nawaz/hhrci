import csv
import json
import os
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
MAPPING_COLUMNS = ("roll_number", "mail_account", "sent")
MappingDict = dict[Literal["roll_number", "mail_account", "sent"], str]
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
    sent: str = ""


class MappingUpdate(BaseModel):
    rows: list[MappingRow]


class MappingDiscoveryRequest(BaseModel):
    folder: str = Field(min_length=1)


class UploadRequest(BaseModel):
    folder: str = Field(min_length=1)
    sender_account: str = Field(min_length=1)


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
        test_run["upload_fields"].setdefault("files_by_roll", {})
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
                    ["attachment_name", "mail_account", "sent"],
                ):
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "mappings.csv must have these columns in order: "
                            + ", ".join(MAPPING_COLUMNS)
                        ),
                    )
                return [
                    MappingDict(
                        {
                            "roll_number": (
                                row.get("roll_number")
                                or row.get("attachment_name")
                                or ""
                            ).strip(),
                            "mail_account": (row.get("mail_account") or "").strip(),
                            "sent": (row.get("sent") or "").strip(),
                        }
                    )
                    for row in reader
                ]
        except OSError as error:
            raise HTTPException(
                status_code=500, detail="Could not read mappings.csv."
            ) from error


def _write_mappings(rows: list[MappingDict]) -> None:
    with _mappings_lock:
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


def _safe_account_folder(account: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", account)


def _is_sent(value: str) -> bool:
    return value.strip().casefold() in {"yes", "true", "1", "sent"}


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
                    folder / test_id / _safe_account_folder(message["account"]),
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
        if mapping_rows is None:
            mapping_rows = _read_mappings()
        mappings = [
            MappingDict(row)
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
    pending = [
        (index, row)
        for index, row in enumerate(mappings)
        if row["roll_number"] in files_by_roll
        and row["mail_account"]
        and not _is_sent(row["sent"])
        and not any(
            update.get("roll_number") == row["roll_number"]
            for update in test_run.get("updates", [])
        )
    ]
    errors: list[dict[str, Any]] = []
    unmatched: list[str] = []
    ready: list[tuple[int, MappingDict, Path]] = []
    for index, row in pending:
        matches = files_by_roll.get(row["roll_number"], [])
        if not matches:
            unmatched.append(row["roll_number"])
        elif len(matches) > 1:
            errors.append(
                {
                    "roll_number": row["roll_number"],
                    "message": "Multiple files have this roll number; none were sent.",
                    "files": [path.name for path in matches],
                }
            )
        else:
            ready.append((index, row, matches[0]))
    operation = test_run["operations"]["upload"]
    operation["progress"]["total"] = len(pending)
    _set_operation(
        test_id,
        "upload",
        progress={"completed": len(pending) - len(ready), "total": len(pending)},
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
                ): (index, row, attachment_path)
                for index, row, attachment_path in ready
            }
            for future in as_completed(futures):
                index, row, attachment_path = futures[future]
                try:
                    future.result()
                    mappings[index]["sent"] = "sent"
                    _write_mappings(mappings)

                    def mark_updated(current: dict[str, Any]) -> None:
                        for mapping in current["upload_fields"]["mappings"]:
                            if mapping["roll_number"] == row["roll_number"]:
                                mapping["sent"] = "sent"
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

                operation = _read_test(test_id)["operations"]["upload"]
                _set_operation(
                    test_id,
                    "upload",
                    progress={
                        "completed": operation["progress"]["completed"] + 1,
                        "total": len(pending),
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


@app.get("/api/tests")
def list_tests() -> dict[str, list[dict[str, Any]]]:
    TESTS_DIR.mkdir(parents=True, exist_ok=True)
    tests = []
    for path in sorted(
        TESTS_DIR.glob("*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        test_run = _read_test(path.stem)
        tests.append(
            {
                "id": test_run["id"],
                "created_at": test_run["created_at"],
                "message_count": len(test_run["messages"]),
                "counts": test_run["counts"],
                "operations": test_run["operations"],
            }
        )
    return {"tests": tests}


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
            "folder": "",
            "sender_account": "",
            "files_by_roll": {},
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
                "sent": prior.get("sent", ""),
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
        if row.roll_number.strip() and "@" not in row.mail_account:
            raise HTTPException(
                status_code=400,
                detail=f"Recipient must be an email address: {row.mail_account}",
            )
    rows: list[MappingDict] = [
        MappingDict(
            {
                "roll_number": row.roll_number.strip(),
                "mail_account": row.mail_account.strip(),
                "sent": row.sent.strip(),
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
        mapping_snapshot = _read_mappings()
        previous_upload = test_run.get("upload_fields", {})
        if test_run.get("updates") and (
            previous_upload.get("folder") != str(folder)
            or previous_upload.get("sender_account") != request.sender_account
        ):
            raise HTTPException(
                status_code=409,
                detail="This test ID already has successful uploads. Start a new test for a different upload folder or sender.",
            )

        _update_test(
            test_id,
            lambda current: current.update(
                upload_fields={
                    "folder": str(folder),
                    "sender_account": request.sender_account,
                    "files_by_roll": {
                        roll_number: [str(path) for path in paths]
                        for roll_number, paths in files_by_roll.items()
                    },
                    "ignored_files": ignored_files,
                    "mappings": mapping_snapshot,
                    "subject_template": UPLOAD_SUBJECT_TEMPLATE,
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


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="dashboard")
