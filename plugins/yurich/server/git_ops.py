"""Focused Git status, commit, and push operations for YURICH."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from filesystem_ops import YurichError, canonical_root


def run_git(
    directory: Path,
    *arguments: str,
    check: bool = True,
    timeout: int = 60,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = os.environ.copy()
    if environment:
        env.update(environment)
    try:
        completed = subprocess.run(
            ["git", "-C", str(directory), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=flags,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise YurichError("Git is not installed or is not available in PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise YurichError("Git did not finish before the timeout.") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise YurichError(detail or f"Git exited with code {completed.returncode}.")
    return completed


def repository_root(root_value: Any) -> Path:
    selected = canonical_root(root_value)
    completed = run_git(selected, "rev-parse", "--show-toplevel", check=False)
    if completed.returncode != 0:
        raise YurichError("The selected folder is not inside a Git repository.")
    try:
        repository = Path(completed.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        raise YurichError(f"Git repository is unavailable: {exc}") from exc
    if not repository.is_dir():
        raise YurichError("Git repository root is not a directory.")
    return repository


def optional_git(directory: Path, *arguments: str) -> str:
    completed = run_git(directory, *arguments, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def parse_status(raw: str) -> list[dict[str, Any]]:
    parts = raw.split("\0")
    files: list[dict[str, Any]] = []
    index = 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if not entry:
            continue
        code = entry[:2]
        path = entry[3:]
        original = ""
        if "R" in code or "C" in code:
            if index < len(parts):
                original = parts[index]
                index += 1
        if code == "!!":
            continue
        files.append({
            "path": path,
            "originalPath": original,
            "code": code,
            "staged": code[0] not in {" ", "?"},
            "unstaged": code[1] != " ",
            "untracked": code == "??",
            "conflicted": "U" in code or code in {"AA", "DD"},
        })
    return files


def git_status(args: dict[str, Any]) -> dict[str, Any]:
    repository = repository_root(args.get("root"))
    raw = run_git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    files = parse_status(raw)
    branch = optional_git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    detached = not bool(branch)
    if detached:
        branch = optional_git(repository, "rev-parse", "--short", "HEAD") or "detached"
    upstream = optional_git(repository, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    ahead = behind = 0
    if upstream:
        counts = optional_git(repository, "rev-list", "--left-right", "--count", f"{upstream}...HEAD").split()
        if len(counts) == 2:
            behind, ahead = (int(counts[0]), int(counts[1]))
    remote = optional_git(repository, "remote", "get-url", "--push", "origin")
    last = optional_git(repository, "log", "-1", "--pretty=format:%h%x00%s%x00%cr").split("\0")
    return {
        "status": "ok",
        "root": str(repository),
        "branch": branch,
        "detached": detached,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "remote": remote,
        "clean": not files,
        "files": files,
        "lastCommit": {
            "hash": last[0] if len(last) > 0 else "",
            "subject": last[1] if len(last) > 1 else "",
            "relativeTime": last[2] if len(last) > 2 else "",
        },
    }


def selected_paths(repository: Path, requested: Any) -> list[str]:
    if not isinstance(requested, list):
        raise YurichError("Select at least one changed file.")
    current = {item["path"]: item for item in git_status({"root": str(repository)})["files"]}
    selected: list[str] = []
    for value in requested:
        if not isinstance(value, str) or value not in current:
            raise YurichError("The selected file list is stale. Refresh Git status and try again.")
        if value not in selected:
            selected.append(value)
    if not selected:
        raise YurichError("Select at least one changed file.")
    paths: list[str] = []
    for path in selected:
        item = current[path]
        for value in (item.get("originalPath"), path):
            if value and value not in paths:
                paths.append(value)
    return paths


def git_commit(args: dict[str, Any]) -> dict[str, Any]:
    repository = repository_root(args.get("root"))
    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        raise YurichError("Enter a commit message.")
    message = message.strip()
    if len(message) > 10000:
        raise YurichError("Commit message is too long.")
    paths = selected_paths(repository, args.get("files"))
    run_git(repository, "add", "-A", "--", *paths)
    completed = run_git(repository, "commit", "--only", "-m", message, "--", *paths, timeout=180)
    commit_hash = optional_git(repository, "rev-parse", "--short", "HEAD")
    return {
        "status": "committed",
        "commit": commit_hash,
        "message": message,
        "output": (completed.stdout or completed.stderr).strip(),
        "repository": git_status({"root": str(repository)}),
    }


def git_push(args: dict[str, Any]) -> dict[str, Any]:
    repository = repository_root(args.get("root"))
    status = git_status({"root": str(repository)})
    if status["detached"]:
        raise YurichError("Cannot push while HEAD is detached.")
    environment = {"GIT_TERMINAL_PROMPT": "0"}
    if status["upstream"]:
        command = ("push",)
    elif status["remote"]:
        command = ("push", "--set-upstream", "origin", status["branch"])
    else:
        raise YurichError("No Git remote named origin is configured.")
    completed = run_git(repository, *command, timeout=300, environment=environment)
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    return {
        "status": "pushed",
        "output": output or "Push completed.",
        "repository": git_status({"root": str(repository)}),
    }
