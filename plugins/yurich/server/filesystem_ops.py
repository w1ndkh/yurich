"""Safe folder and UTF-8 file operations for YURICH."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 8 * 1024 * 1024


class YurichError(Exception):
    pass


def canonical_root(value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise YurichError("Choose a project folder first.")
    root = Path(os.path.expandvars(os.path.expanduser(value.strip())))
    if not root.is_absolute():
        raise YurichError("Project folder must be an absolute path.")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise YurichError(f"Project folder is unavailable: {exc}") from exc
    if not root.is_dir():
        raise YurichError("Project folder is not a directory.")
    return root


def is_within(root: Path, target: Path) -> bool:
    try:
        left = os.path.normcase(str(root))
        right = os.path.normcase(str(target))
        return os.path.commonpath((left, right)) == left
    except ValueError:
        return False


def safe_file(root_value: Any, relative_value: Any) -> tuple[Path, Path]:
    root = canonical_root(root_value)
    if not isinstance(relative_value, str) or not relative_value.strip():
        raise YurichError("File path is required.")
    supplied = Path(relative_value)
    if supplied.is_absolute():
        raise YurichError("File path must be relative to the selected folder.")
    if any(part == ".." for part in supplied.parts):
        raise YurichError("Parent path traversal is not allowed.")
    try:
        target = root.joinpath(supplied).resolve(strict=True)
    except OSError as exc:
        raise YurichError(f"File is unavailable: {exc}") from exc
    if not is_within(root, target):
        raise YurichError("The resolved file escapes the selected folder.")
    if not target.is_file():
        raise YurichError("Path does not point to a regular file.")
    return root, target


def relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def read_utf8(path: Path) -> tuple[str, bytes, bool]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise YurichError(f"Cannot read file: {exc}") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise YurichError(f"File exceeds the {MAX_FILE_BYTES // 1024 // 1024} MB editor limit.")
    if b"\x00" in raw[:8192]:
        raise YurichError("Binary files are not supported.")
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig" if bom else "utf-8")
    except UnicodeDecodeError as exc:
        raise YurichError("File is not valid UTF-8.") from exc
    return text, raw, bom


def line_ending(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    kinds = sum(bool(value) for value in (crlf, lf, cr))
    if kinds > 1:
        return "MIXED"
    if crlf:
        return "CRLF"
    if cr:
        return "CR"
    return "LF"


def version_info(path: Path, raw: bytes | None = None) -> dict[str, Any]:
    stat = path.stat()
    if raw is None:
        raw = path.read_bytes()
    return {
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def file_metadata(root: Path, path: Path, text: str, raw: bytes, bom: bool) -> dict[str, Any]:
    return {
        "path": relative_path(root, path),
        **version_info(path, raw),
        "encoding": "utf-8",
        "bom": bom,
        "lineEnding": line_ending(text),
        "lineCount": text.count("\n") + 1,
    }


def validate_root(args: dict[str, Any]) -> dict[str, Any]:
    import shutil
    root = canonical_root(args.get("root"))
    return {
        "status": "ok",
        "root": str(root),
        "name": root.name or str(root),
        "isGitRepository": (root / ".git").exists(),
        "rgAvailable": bool(shutil.which("rg")),
    }


def choose_folder(_: dict[str, Any]) -> dict[str, Any]:
    if os.name != "nt":
        return {"status": "unsupported", "message": "Folder picker is currently available on Windows only."}
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description='Choose a folder for YURICH';"
        "$d.ShowNewFolderButton=$false;"
        "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;Write-Output $d.SelectedPath}"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            creationflags=flags,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "message": f"Could not open the folder picker: {exc}"}
    selected = completed.stdout.strip()
    if not selected:
        return {"status": "cancelled"}
    return validate_root({"root": selected})


def read_file(args: dict[str, Any]) -> dict[str, Any]:
    root, path = safe_file(args.get("root"), args.get("path"))
    text, raw, bom = read_utf8(path)
    return {"status": "ok", "content": text, "metadata": file_metadata(root, path, text, raw, bom)}


def open_in_notepad(args: dict[str, Any]) -> dict[str, Any]:
    root, path = safe_file(args.get("root"), args.get("path"))
    if os.name != "nt":
        raise YurichError("Opening files in Notepad is available on Windows only.")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            ["notepad.exe", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=flags,
        )
    except OSError as exc:
        raise YurichError(f"Could not open the file in Notepad: {exc}") from exc
    return {"status": "opened", "path": relative_path(root, path)}


def normalize_line_endings(content: str, style: str) -> str:
    if style == "MIXED":
        return content
    separator = {"CRLF": "\r\n", "CR": "\r", "LF": "\n"}[style]
    return re.sub(r"\r\n|\r|\n", separator, content)


def save_file(args: dict[str, Any]) -> dict[str, Any]:
    root, path = safe_file(args.get("root"), args.get("path"))
    content = args.get("content")
    expected = args.get("expectedVersion")
    if not isinstance(content, str):
        raise YurichError("content must be a string.")
    if not isinstance(expected, dict):
        raise YurichError("expectedVersion from read_file is required.")
    current_text, current_raw, bom = read_utf8(path)
    current = version_info(path, current_raw)
    expected_subset = {key: expected.get(key) for key in ("size", "mtimeNs", "sha256")}
    if expected_subset != current:
        return {
            "status": "conflict",
            "message": "The file changed on disk after it was opened.",
            "currentVersion": current,
        }
    content = normalize_line_endings(content, line_ending(current_text))
    encoded = content.encode("utf-8")
    if bom:
        encoded = b"\xef\xbb\xbf" + encoded
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", delete=False, dir=path.parent,
            prefix=f".{path.name}.yurich-", suffix=".tmp",
        ) as handle:
            temp_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, path.stat().st_mode)
        os.replace(temp_name, path)
    except OSError as exc:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise YurichError(f"Could not save file: {exc}") from exc
    saved_raw = path.read_bytes()
    return {
        "status": "success",
        "metadata": {
            "path": relative_path(root, path),
            **version_info(path, saved_raw),
            "encoding": "utf-8",
            "bom": bom,
            "lineEnding": line_ending(content),
        },
    }
