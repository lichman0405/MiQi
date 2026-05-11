"""Smoke test the desktop backend JSON-RPC stdio contract.

This script starts a real ``miqi desktop-backend --stdio`` process with an
isolated temporary config/data root, sends a handful of JSON-RPC lines, and
prints a compact result summary. It intentionally avoids React/Tauri DOM work
and never prints secrets or backend stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_ENV_RE = re.compile(r"(API|KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)


class SmokeError(RuntimeError):
    pass


def _default_python() -> str:
    windows_python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if windows_python.exists():
        return str(windows_python)
    return sys.executable


def _host_triple() -> str | None:
    try:
        result = subprocess.run(
            ["rustc", "--print", "host-tuple"],
            cwd=REPO_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()
    except Exception:
        try:
            result = subprocess.run(
                ["rustc", "-Vv"],
                cwd=REPO_ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
        except Exception:
            return None

    for line in result.stdout.splitlines():
        if line.startswith("host:"):
            return line.split(None, 1)[1].strip()
    return None


def _dev_sidecar_path() -> Path:
    triple = _host_triple()
    if not triple:
        raise SmokeError("Could not determine Rust host target triple for --dev-sidecar")
    ext = ".exe" if os.name == "nt" else ""
    path = REPO_ROOT / "desktop" / "src-tauri" / "binaries" / f"miqi-desktop-backend-{triple}{ext}"
    if not path.exists():
        raise SmokeError(
            f"Dev sidecar not found: {path}. Run `cd desktop && npm run sidecar:dev` first."
        )
    return path


def _write_smoke_config(root: Path) -> Path:
    config_path = root / "config.json"
    workspace = root / "workspace"
    config = {
        "agents": {
            "defaults": {
                "name": "desktop-smoke",
                "workspace": str(workspace),
                "model": "ollama_local/llama3.2",
                "max_tokens": 128,
                "temperature": 0.0,
            }
        },
        "providers": {
            "ollama_local": {
                "api_key": "",
                "api_base": "http://127.0.0.1:11434",
            }
        },
        "tools": {
            "restrict_to_workspace": True,
        },
    }
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path


def _smoke_env(config_path: Path, data_dir: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
        or not SENSITIVE_ENV_RE.search(key)
    }
    env["MIQI_CONFIG_PATH"] = str(config_path)
    env["MIQI_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = (
        str(REPO_ROOT)
        if not env.get("PYTHONPATH")
        else str(REPO_ROOT) + os.pathsep + env["PYTHONPATH"]
    )
    return env


def _command(args: argparse.Namespace) -> list[str]:
    if args.dev_sidecar:
        return [str(_dev_sidecar_path()), "--stdio"]
    if args.command:
        return args.command
    return [_default_python(), "-m", "miqi.cli.commands", "desktop-backend", "--stdio"]


def _read_stdout_line(proc: subprocess.Popen[str], timeout: float = 10.0) -> str:
    if proc.stdout is None:
        raise SmokeError("Sidecar stdout is not available")

    result: queue.Queue[str] = queue.Queue(maxsize=1)

    def _read() -> None:
        result.put(proc.stdout.readline())

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    try:
        return result.get(timeout=timeout)
    except queue.Empty as exc:
        raise SmokeError("Timed out waiting for sidecar JSON-RPC response") from exc


def _send_raw(proc: subprocess.Popen[str], raw: str) -> dict[str, Any]:
    if proc.stdin is None or proc.stdout is None:
        raise SmokeError("Sidecar pipes are not available")
    proc.stdin.write(raw + "\n")
    proc.stdin.flush()
    line = _read_stdout_line(proc)
    if not line:
        raise SmokeError("Sidecar closed stdout before responding")
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise SmokeError(f"Sidecar returned malformed JSON: {exc}") from exc


def _request(proc: subprocess.Popen[str], request_id: int, method: str) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    return _send_raw(proc, json.dumps(payload))


def _assert_success(response: dict[str, Any], request_id: int, method: str) -> dict[str, Any]:
    if response.get("id") != request_id or "result" not in response or response.get("error"):
        raise SmokeError(f"{method} did not return a JSON-RPC success response")
    result = response["result"]
    if not isinstance(result, dict):
        raise SmokeError(f"{method} result is not an object")
    return result


def _assert_error(response: dict[str, Any], code: int, label: str) -> None:
    error = response.get("error")
    if not isinstance(error, dict) or error.get("code") != code:
        raise SmokeError(f"{label} did not return JSON-RPC error code {code}")


def run_smoke(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="miqi-desktop-smoke-") as tmp:
        root = Path(tmp)
        data_dir = root / "data"
        config_path = _write_smoke_config(root)
        command = _command(args)

        proc = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=_smoke_env(config_path, data_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        try:
            malformed = _send_raw(proc, "{bad json")
            _assert_error(malformed, -32700, "malformed JSON")
            print("ok malformed-json parse-error", flush=True)

            app_status = _assert_success(_request(proc, 1, "app.status"), 1, "app.status")
            for field in ("status", "model", "workspace", "agent_name"):
                if field not in app_status:
                    raise SmokeError(f"app.status missing field: {field}")
            print(f"ok app.status status={app_status['status']} model={app_status['model']}", flush=True)

            sessions = _assert_success(_request(proc, 2, "session.list"), 2, "session.list")
            if "sessions" not in sessions or "count" not in sessions:
                raise SmokeError("session.list missing sessions/count")
            print(f"ok session.list count={sessions['count']}", flush=True)

            tools = _assert_success(_request(proc, 3, "tool.list"), 3, "tool.list")
            if "tools" not in tools or "count" not in tools:
                raise SmokeError("tool.list missing tools/count")
            print(f"ok tool.list count={tools['count']}", flush=True)

            memory = _assert_success(_request(proc, 4, "memory.status"), 4, "memory.status")
            for field in ("ltm_items", "snapshot_exists", "lessons_count"):
                if field not in memory:
                    raise SmokeError(f"memory.status missing field: {field}")
            print("ok memory.status", flush=True)

            unknown = _request(proc, 5, "smoke.unknownMethod")
            _assert_error(unknown, -32601, "unknown method")
            print("ok unknown-method method-not-found", flush=True)
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)

        if proc.returncode != 0:
            raise SmokeError(
                f"sidecar exited with code {proc.returncode}; stderr captured but suppressed"
            )

        print("ok sidecar closed cleanly", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test MiQi desktop backend stdio JSON-RPC")
    parser.add_argument(
        "--dev-sidecar",
        action="store_true",
        help="Run the generated Tauri dev sidecar from desktop/src-tauri/binaries",
    )
    parser.add_argument(
        "--command",
        nargs="+",
        help="Custom command to run instead of the default Python backend command",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        run_smoke(parse_args())
    except SmokeError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
