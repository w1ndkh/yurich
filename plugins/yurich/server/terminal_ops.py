"""Local project command runner for the YURICH terminal panel."""

from __future__ import annotations

import atexit
import base64
import codecs
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filesystem_ops import YurichError, canonical_root

MAX_COMMAND_LENGTH = 8192
MAX_OUTPUT_CHARS = 2 * 1024 * 1024
MAX_POLL_CHARS = 256 * 1024
MAX_PACKAGE_JSON_BYTES = 1024 * 1024
ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


@dataclass
class CommandRecord:
    identifier: str
    root: Path
    command: str
    process: subprocess.Popen[bytes]
    output: str = ""
    base_cursor: int = 0
    done: bool = False
    exit_code: int | None = None
    stopped: bool = False
    started_at: float = field(default_factory=time.time)
    lock: threading.RLock = field(default_factory=threading.RLock)


_records: dict[str, CommandRecord] = {}
_records_lock = threading.RLock()


def _append_output(record: CommandRecord, value: str) -> None:
    if not value:
        return
    value = ANSI_ESCAPE.sub("", value).replace("\x00", "")
    with record.lock:
        record.output += value
        excess = len(record.output) - MAX_OUTPUT_CHARS
        if excess > 0:
            record.output = record.output[excess:]
            record.base_cursor += excess


def _read_process(record: CommandRecord) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    stream = record.process.stdout
    try:
        if stream is not None:
            descriptor = stream.fileno()
            while True:
                chunk = os.read(descriptor, 4096)
                if not chunk:
                    break
                _append_output(record, decoder.decode(chunk))
            _append_output(record, decoder.decode(b"", final=True))
    except OSError as error:
        _append_output(record, f"\n[YURICH could not read command output: {error}]\n")
    finally:
        try:
            exit_code = record.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            exit_code = record.process.poll()
        with record.lock:
            record.exit_code = exit_code
            record.done = exit_code is not None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _command_line(command: str) -> tuple[list[str], dict[str, Any]]:
    if os.name == "nt":
        script = (
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
            "$OutputEncoding=[Console]::OutputEncoding;"
            + command
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        return ["powershell.exe", "-NoLogo", "-NoProfile", "-EncodedCommand", encoded], {
            "creationflags": flags
        }
    shell = os.environ.get("SHELL") or "/bin/sh"
    return [shell, "-lc", command], {"start_new_session": True}


def _remove_old_records() -> None:
    with _records_lock:
        completed = sorted(
            (record for record in _records.values() if record.done),
            key=lambda record: record.started_at,
            reverse=True,
        )
        for record in completed[10:]:
            _records.pop(record.identifier, None)


def start_command(arguments: dict[str, Any]) -> dict[str, Any]:
    root = canonical_root(arguments.get("root"))
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise YurichError("Enter a command to run.")
    command = command.strip()
    if len(command) > MAX_COMMAND_LENGTH:
        raise YurichError(f"Command exceeds the {MAX_COMMAND_LENGTH} character limit.")

    _remove_old_records()
    with _records_lock:
        if any(record.process.poll() is None for record in _records.values()):
            raise YurichError("Another terminal command is already running. Stop it first.")

    argv, platform_options = _command_line(command)
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            shell=False,
            **platform_options,
        )
    except OSError as error:
        raise YurichError(f"Could not start the command: {error}") from error

    identifier = uuid.uuid4().hex
    record = CommandRecord(identifier, root, command, process)
    with _records_lock:
        _records[identifier] = record
    threading.Thread(target=_read_process, args=(record,), daemon=True).start()
    return {
        "status": "running",
        "processId": identifier,
        "systemPid": process.pid,
        "root": str(root),
        "command": command,
        "shell": "PowerShell" if os.name == "nt" else Path(argv[0]).name,
        "cursor": 0,
    }


def _record(identifier: Any) -> CommandRecord:
    if not isinstance(identifier, str) or not identifier:
        raise YurichError("Terminal process ID is required.")
    with _records_lock:
        record = _records.get(identifier)
    if record is None:
        raise YurichError("Terminal process is no longer available.")
    return record


def poll_command(arguments: dict[str, Any]) -> dict[str, Any]:
    record = _record(arguments.get("processId"))
    try:
        cursor = max(0, int(arguments.get("cursor") or 0))
    except (TypeError, ValueError) as error:
        raise YurichError("Terminal cursor must be an integer.") from error
    with record.lock:
        truncated = cursor < record.base_cursor
        start = 0 if truncated else min(len(record.output), cursor - record.base_cursor)
        output = record.output[start : start + MAX_POLL_CHARS]
        next_cursor = record.base_cursor + start + len(output)
        running = record.process.poll() is None and not record.done
        exit_code = record.exit_code if record.done else record.process.poll()
        return {
            "status": "running" if running else ("stopped" if record.stopped else "completed"),
            "processId": record.identifier,
            "output": output,
            "cursor": next_cursor,
            "truncated": truncated,
            "hasMore": next_cursor < record.base_cursor + len(record.output),
            "running": running,
            "exitCode": exit_code,
            "root": str(record.root),
            "command": record.command,
        }


def _terminate(record: CommandRecord) -> None:
    process = record.process
    if process.poll() is not None:
        return
    record.stopped = True
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=1.5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=8,
                creationflags=flags,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=1.5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired as error:
            raise YurichError("The terminal process did not stop in time.") from error
    with record.lock:
        record.exit_code = process.poll()
        record.done = record.exit_code is not None


def stop_command(arguments: dict[str, Any]) -> dict[str, Any]:
    record = _record(arguments.get("processId"))
    _terminate(record)
    return poll_command({"processId": record.identifier, "cursor": arguments.get("cursor", 0)})


def list_package_scripts(arguments: dict[str, Any]) -> dict[str, Any]:
    root = canonical_root(arguments.get("root"))
    package_path = root / "package.json"
    if not package_path.is_file():
        return {"status": "missing", "root": str(root), "scripts": []}
    try:
        raw = package_path.read_bytes()
        if len(raw) > MAX_PACKAGE_JSON_BYTES:
            raise YurichError("package.json is too large to inspect safely.")
        payload = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as error:
        raise YurichError("package.json is not valid UTF-8.") from error
    except json.JSONDecodeError as error:
        raise YurichError(f"package.json is not valid JSON: {error}") from error
    except OSError as error:
        raise YurichError(f"Could not read package.json: {error}") from error
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        return {"status": "ok", "root": str(root), "scripts": []}
    names = sorted(
        str(name) for name, value in scripts.items()
        if isinstance(name, str) and name.strip() and isinstance(value, str)
    )[:50]
    return {"status": "ok", "root": str(root), "scripts": names}


def _stop_all() -> None:
    with _records_lock:
        records = list(_records.values())
    for record in records:
        try:
            _terminate(record)
        except Exception:
            pass


atexit.register(_stop_all)
