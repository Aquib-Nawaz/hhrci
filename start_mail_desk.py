import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
URL = "http://127.0.0.1:8000"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def _prepare_environment() -> Path:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required.")
    python = _venv_python()
    if not python.is_file():
        print("Creating the local Python environment…")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)

    expected_hash = _requirements_hash()
    marker = VENV_DIR / ".requirements-hash"
    if not marker.is_file() or marker.read_text(encoding="utf-8") != expected_hash:
        print("Installing or updating Mail Desk dependencies…")
        subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
            cwd=ROOT,
            check=True,
        )
        marker.write_text(expected_hash, encoding="utf-8")
    return python


def _is_mail_desk_running() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/openapi.json", timeout=1) as response:
            specification = json.load(response)
        return specification.get("info", {}).get("title") == "Mail Dashboard"
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return False


def _wait_until_ready(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Mail Desk server exited during startup (exit code {process.returncode})."
            )
        if _is_mail_desk_running():
            return
        time.sleep(0.5)
    raise RuntimeError("Mail Desk did not start within 45 seconds.")


def main() -> int:
    server = None
    try:
        if _is_mail_desk_running():
            print(f"Mail Desk is already running. Opening {URL}")
            webbrowser.open(URL)
            return 0

        python = _prepare_environment()
        print("Starting Mail Desk. Keep this window open while using the dashboard.")
        server = subprocess.Popen(
            [
                str(python),
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ],
            cwd=ROOT,
        )
        _wait_until_ready(server)
        webbrowser.open(URL)
        print(f"Mail Desk is open at {URL}. Press Ctrl+C to stop it.")
        server.wait()
        return server.returncode or 0
    except KeyboardInterrupt:
        print("\nStopping Mail Desk…")
        return 0
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"\nCould not start Mail Desk: {error}", file=sys.stderr)
        return 1
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    raise SystemExit(main())
