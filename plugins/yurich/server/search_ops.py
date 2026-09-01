from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

from filesystem_ops import YurichError, canonical_root, is_within, read_utf8


DEFAULT_IGNORES = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "out",
    ".next",
    "coverage",
    ".cache",
    ".turbo",
    "target",
    "__pycache__",
}


def _patterns(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace(",", "\n").splitlines()
    if not isinstance(value, list):
        raise YurichError("Glob patterns must be a string or an array of strings.")
    return [str(item).strip().replace("\\", "/") for item in value if str(item).strip()]


def _matches_patterns(relative: str, includes: list[str], excludes: list[str]) -> bool:
    relative = relative.replace("\\", "/")
    name = relative.rsplit("/", 1)[-1]
    if includes and not any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern) for pattern in includes):
        return False
    if any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern) for pattern in excludes):
        return False
    return True


def _context(lines: list[str], line_number: int, radius: int = 2) -> list[dict[str, Any]]:
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    return [
        {"lineNumber": number, "text": lines[number - 1], "isMatch": number == line_number}
        for number in range(start, end + 1)
    ]


def _group(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for match in matches:
        grouped.setdefault(match["path"], []).append(match)
    return [{"path": path, "matches": items} for path, items in grouped.items()]


def _byte_to_column(text: str, byte_offset: int) -> int:
    raw = text.encode("utf-8")
    byte_offset = max(0, min(byte_offset, len(raw)))
    return len(raw[:byte_offset].decode("utf-8", errors="ignore")) + 1


def _rg_search(
    root: Path,
    query: str,
    *,
    case_sensitive: bool,
    regex: bool,
    whole_word: bool,
    includes: list[str],
    excludes: list[str],
    max_results: int,
    context_lines: int,
) -> tuple[list[dict[str, Any]], bool] | None:
    rg = shutil.which("rg")
    if not rg:
        return None

    command = [
        rg,
        "--json",
        "--color",
        "never",
        "--no-messages",
        "--with-filename",
        "--line-number",
        "--hidden",
    ]
    command.append("--case-sensitive" if case_sensitive else "--ignore-case")
    if not regex:
        command.append("--fixed-strings")
    if whole_word:
        command.append("--word-regexp")
    for folder in sorted(DEFAULT_IGNORES):
        command.extend(["--glob", f"!**/{folder}/**"])
    for pattern in includes:
        command.extend(["--glob", pattern])
    for pattern in excludes:
        command.extend(["--glob", f"!{pattern}"])
    command.extend(["--", query, str(root)])

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except OSError:
        return None

    matches: list[dict[str, Any]] = []
    line_cache: dict[Path, list[str]] = {}
    truncated = False
    assert process.stdout is not None
    try:
        for raw_event in process.stdout:
            try:
                event = json.loads(raw_event)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data", {})
            path_text = (data.get("path") or {}).get("text")
            if not path_text:
                continue
            path = Path(path_text).resolve()
            if not is_within(root, path) or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if not _matches_patterns(relative, includes, excludes):
                continue
            line_text = ((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
            line_number = int(data.get("line_number") or 1)
            submatches = data.get("submatches") or []
            start_byte = int(submatches[0].get("start", 0)) if submatches else 0
            end_byte = int(submatches[0].get("end", start_byte)) if submatches else start_byte
            try:
                if path not in line_cache:
                    line_cache[path] = read_utf8(path)[0].splitlines()
                context = _context(line_cache[path], line_number, context_lines)
            except YurichError:
                context = [{"lineNumber": line_number, "text": line_text, "isMatch": True}]
            matches.append(
                {
                    "path": relative,
                    "lineNumber": line_number,
                    "column": _byte_to_column(line_text, start_byte),
                    "endColumn": _byte_to_column(line_text, end_byte),
                    "text": line_text,
                    "context": context,
                }
            )
            if len(matches) >= max_results:
                truncated = True
                process.terminate()
                break
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
        process.stdout.close()
    return matches, truncated


def _candidate_files(root: Path) -> Iterable[Path]:
    git = shutil.which("git")
    if git and (root / ".git").exists():
        try:
            completed = subprocess.run(
                [git, "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
                shell=False,
            )
            if completed.returncode == 0:
                for raw in completed.stdout.split(b"\0"):
                    if not raw:
                        continue
                    relative = raw.decode("utf-8", errors="surrogateescape")
                    if any(part in DEFAULT_IGNORES for part in Path(relative).parts):
                        continue
                    path = (root / relative).resolve()
                    if is_within(root, path) and path.is_file():
                        yield path
                return
        except (OSError, subprocess.TimeoutExpired):
            pass

    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [
            name
            for name in directories
            if name not in DEFAULT_IGNORES and not Path(current, name).is_symlink()
        ]
        for filename in filenames:
            path = Path(current, filename)
            if path.is_file() and not path.is_symlink():
                yield path


def _fallback_search(
    root: Path,
    query: str,
    *,
    case_sensitive: bool,
    regex: bool,
    whole_word: bool,
    includes: list[str],
    excludes: list[str],
    max_results: int,
    context_lines: int,
) -> tuple[list[dict[str, Any]], bool]:
    flags = 0 if case_sensitive else re.IGNORECASE
    expression = query if regex else re.escape(query)
    if whole_word:
        expression = rf"\b(?:{expression})\b"
    try:
        matcher = re.compile(expression, flags)
    except re.error as error:
        raise YurichError(f"Invalid regular expression: {error}") from error

    matches: list[dict[str, Any]] = []
    truncated = False
    for path in _candidate_files(root):
        relative = path.relative_to(root).as_posix()
        if not _matches_patterns(relative, includes, excludes):
            continue
        try:
            text, _ = read_utf8(path)
        except YurichError:
            continue
        lines = text.splitlines()
        for line_number, line in enumerate(lines, start=1):
            found = matcher.search(line)
            if not found:
                continue
            matches.append(
                {
                    "path": relative,
                    "lineNumber": line_number,
                    "column": found.start() + 1,
                    "endColumn": found.end() + 1,
                    "text": line,
                    "context": _context(lines, line_number, context_lines),
                }
            )
            if len(matches) >= max_results:
                truncated = True
                return matches, truncated
    return matches, truncated


def search_project(arguments: dict[str, Any]) -> dict[str, Any]:
    root = canonical_root(str(arguments.get("root") or ""))
    query = str(arguments.get("query") or "")
    if not query:
        raise YurichError("Enter text to search for.")
    max_results = int(arguments.get("maxResults") or 500)
    max_results = max(1, min(max_results, 2000))
    context_lines = int(arguments.get("contextLines", 2))
    context_lines = max(0, min(context_lines, 10))
    options = {
        "case_sensitive": bool(arguments.get("caseSensitive", False)),
        "regex": bool(arguments.get("regex", False)),
        "whole_word": bool(arguments.get("wholeWord", False)),
        "includes": _patterns(arguments.get("includeGlobs")),
        "excludes": _patterns(arguments.get("excludeGlobs")),
        "max_results": max_results,
        "context_lines": context_lines,
    }
    found = _rg_search(root, query, **options)
    if found is None:
        matches, truncated = _fallback_search(root, query, **options)
        engine = "python"
    else:
        matches, truncated = found
        engine = "ripgrep"
    return {
        "root": str(root),
        "query": query,
        "engine": engine,
        "count": len(matches),
        "truncated": truncated,
        "results": matches,
        "groups": _group(matches),
    }


def search_files(arguments: dict[str, Any]) -> dict[str, Any]:
    root = canonical_root(str(arguments.get("root") or ""))
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise YurichError("Enter a file name to search for.")
    max_results = max(1, min(int(arguments.get("maxResults") or 500), 2000))
    case_sensitive = bool(arguments.get("caseSensitive", False))
    whole_word = bool(arguments.get("wholeWord", False))
    regex = bool(arguments.get("regex", False))
    includes = _patterns(arguments.get("includeGlobs"))
    excludes = _patterns(arguments.get("excludeGlobs"))
    expression = query if regex else re.escape(query)
    if whole_word:
        expression = rf"\b(?:{expression})\b"
    try:
        matcher = re.compile(expression, 0 if case_sensitive else re.IGNORECASE)
    except re.error as error:
        raise YurichError(f"Invalid regular expression: {error}") from error

    files: list[str] = []
    truncated = False
    for path in _candidate_files(root):
        relative = path.relative_to(root).as_posix()
        if not _matches_patterns(relative, includes, excludes):
            continue
        if not matcher.search(path.name) and not matcher.search(relative):
            continue
        files.append(relative)
        if len(files) >= max_results:
            truncated = True
            break
    files.sort(key=str.casefold)
    return {
        "root": str(root),
        "query": query,
        "engine": "filesystem",
        "mode": "files",
        "count": len(files),
        "truncated": truncated,
        "files": files,
    }
