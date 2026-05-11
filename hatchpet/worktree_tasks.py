from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_bridge import clean_text, codex_candidate_paths
from .codex_monitor import ACTIVE_SESSION_DIR


WORKTREE_TASKS_FILE = "worktree-tasks.json"
WORKTREE_TASKS_DIR = "worktrees"
WORKTREE_POINTERS_DIR = "worktree-pointers"


class WorktreeTaskError(Exception):
    pass


@dataclass(frozen=True)
class WorktreeTask:
    task_id: str
    label: str
    repo_root: Path
    worktree_path: Path
    base_ref: str
    branch: str
    mode: str
    status: str
    prompt: str = ""
    owner_id: str = ""
    owner_label: str = ""
    pointer_path: Path | None = None
    session_id: str = ""
    rollout_path: Path | None = None
    terminal_state: str = ""
    terminal_pid: int | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    removed_at: float = 0.0
    local_dirty_at_create: bool = False

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> WorktreeTask:
        pointer = clean_text(record.get("pointer_path"), 1000)
        rollout = clean_text(record.get("rollout_path"), 1000)
        terminal_pid = record.get("terminal_pid")
        try:
            terminal_pid_int = int(terminal_pid) if terminal_pid is not None else None
        except (TypeError, ValueError):
            terminal_pid_int = None
        return cls(
            task_id=clean_text(record.get("task_id"), 120),
            label=clean_text(record.get("label"), 220),
            repo_root=Path(clean_text(record.get("repo_root"), 1000)).expanduser(),
            worktree_path=Path(clean_text(record.get("worktree_path"), 1000)).expanduser(),
            base_ref=clean_text(record.get("base_ref"), 220) or "HEAD",
            branch=clean_text(record.get("branch"), 220),
            mode=clean_text(record.get("mode"), 80) or "detached",
            status=clean_text(record.get("status"), 80) or "created",
            prompt=str(record.get("prompt") or ""),
            owner_id=clean_text(record.get("owner_id"), 120),
            owner_label=clean_text(record.get("owner_label"), 220),
            pointer_path=Path(pointer).expanduser() if pointer else None,
            session_id=clean_text(record.get("session_id"), 120),
            rollout_path=Path(rollout).expanduser() if rollout else None,
            terminal_state=clean_text(record.get("terminal_state"), 80),
            terminal_pid=terminal_pid_int,
            created_at=float(record.get("created_at") or 0.0),
            updated_at=float(record.get("updated_at") or 0.0),
            removed_at=float(record.get("removed_at") or 0.0),
            local_dirty_at_create=bool(record.get("local_dirty_at_create", False)),
        )

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "task_id": self.task_id,
            "label": self.label,
            "repo_root": str(self.repo_root),
            "worktree_path": str(self.worktree_path),
            "base_ref": self.base_ref,
            "branch": self.branch,
            "mode": self.mode,
            "status": self.status,
            "prompt": self.prompt,
            "owner_id": self.owner_id,
            "owner_label": self.owner_label,
            "session_id": self.session_id,
            "terminal_state": self.terminal_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "removed_at": self.removed_at,
            "local_dirty_at_create": self.local_dirty_at_create,
        }
        if self.pointer_path is not None:
            record["pointer_path"] = str(self.pointer_path)
        if self.rollout_path is not None:
            record["rollout_path"] = str(self.rollout_path)
        if self.terminal_pid is not None:
            record["terminal_pid"] = int(self.terminal_pid)
        return record


def state_root(codex_home: Path | None = None) -> Path:
    home = codex_home or (Path.home() / ".codex")
    return home.expanduser() / ACTIVE_SESSION_DIR


def tasks_registry_path(codex_home: Path | None = None) -> Path:
    return state_root(codex_home) / WORKTREE_TASKS_FILE


def default_worktrees_root(codex_home: Path | None = None) -> Path:
    home = codex_home or (Path.home() / ".codex")
    return home.expanduser() / WORKTREE_TASKS_DIR / ACTIVE_SESSION_DIR


def default_pointer_path(task_id: str, codex_home: Path | None = None) -> Path:
    return state_root(codex_home) / WORKTREE_POINTERS_DIR / f"{task_id}-session.json"


def load_task_registry(codex_home: Path | None = None) -> dict[str, Any]:
    path = tasks_registry_path(codex_home)
    if not path.is_file():
        return {"tasks": {}}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tasks": {}}
    if not isinstance(parsed, dict):
        return {"tasks": {}}
    tasks = parsed.get("tasks")
    if not isinstance(tasks, dict):
        parsed["tasks"] = {}
    return parsed


def save_task_registry(data: dict[str, Any], codex_home: Path | None = None) -> Path:
    path = tasks_registry_path(codex_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        data["tasks"] = {}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def list_worktree_tasks(
    *,
    codex_home: Path | None = None,
    include_removed: bool = False,
) -> list[WorktreeTask]:
    data = load_task_registry(codex_home)
    tasks = []
    for record in data.get("tasks", {}).values():
        if not isinstance(record, dict):
            continue
        task = WorktreeTask.from_record(record)
        if not task.task_id:
            continue
        if not include_removed and task.status == "removed":
            continue
        tasks.append(task)
    tasks.sort(key=lambda task: task.updated_at or task.created_at, reverse=True)
    return tasks


def get_worktree_task(task_id: str, *, codex_home: Path | None = None) -> WorktreeTask:
    task_key = clean_task_id(task_id)
    data = load_task_registry(codex_home)
    record = data.get("tasks", {}).get(task_key)
    if not isinstance(record, dict):
        raise WorktreeTaskError(f"No worktree task found for {task_id!r}.")
    return WorktreeTask.from_record(record)


def update_worktree_task(
    task_id: str,
    *,
    codex_home: Path | None = None,
    **updates: Any,
) -> WorktreeTask:
    task_key = clean_task_id(task_id)
    data = load_task_registry(codex_home)
    tasks = data.get("tasks")
    if not isinstance(tasks, dict) or task_key not in tasks or not isinstance(tasks[task_key], dict):
        raise WorktreeTaskError(f"No worktree task found for {task_id!r}.")
    record = dict(tasks[task_key])
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, Path):
            record[key] = str(value.expanduser())
        else:
            record[key] = value
    record["updated_at"] = time.time()
    tasks[task_key] = record
    save_task_registry(data, codex_home)
    return WorktreeTask.from_record(record)


def clean_task_id(value: object) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,120}", text):
        raise WorktreeTaskError("Worktree task id is invalid.")
    return text


def clean_slug(value: object, *, fallback: str = "task", limit: int = 36) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not text:
        text = fallback
    return text[:limit].strip("-") or fallback


def repo_slug(repo_root: Path) -> str:
    name = clean_slug(repo_root.name or "repo", fallback="repo", limit=32)
    digest = hashlib.sha1(str(repo_root).encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def task_id_for(label: str, repo_root: Path) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = clean_slug(label, fallback=repo_root.name or "task", limit=28)
    digest = hashlib.sha1(f"{repo_root}:{label}:{time.time()}".encode("utf-8")).hexdigest()[:6]
    return f"wt-{stamp}-{slug}-{digest}"


def git_run(
    cwd: Path,
    args: list[str],
    *,
    timeout: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise WorktreeTaskError("Git is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeTaskError("Git command timed out: git " + " ".join(args)) from exc
    if check and result.returncode != 0:
        detail = clean_git_output(result.stderr or result.stdout) or f"git {' '.join(args)} exited {result.returncode}"
        raise WorktreeTaskError(detail)
    return result


def git_stdout(cwd: Path, args: list[str], *, timeout: float = 120.0) -> str:
    return git_run(cwd, args, timeout=timeout).stdout.strip()


def resolve_git_root(cwd: Path) -> Path:
    candidate = cwd.expanduser()
    if candidate.is_file():
        candidate = candidate.parent
    try:
        output = git_stdout(candidate, ["rev-parse", "--show-toplevel"], timeout=10.0)
    except WorktreeTaskError as exc:
        raise WorktreeTaskError("Worktree tasks require a Git repository.") from exc
    if not output:
        raise WorktreeTaskError("Worktree tasks require a Git repository.")
    return Path(output).expanduser().resolve()


def is_dirty(path: Path) -> bool:
    return bool(git_stdout(path, ["status", "--porcelain"], timeout=20.0))


def current_ref(path: Path) -> str:
    try:
        branch = git_stdout(path, ["branch", "--show-current"], timeout=10.0)
    except WorktreeTaskError:
        branch = ""
    if branch:
        return branch
    return git_stdout(path, ["rev-parse", "--short", "HEAD"], timeout=10.0) or "HEAD"


def create_worktree_task(
    *,
    cwd: Path,
    label: str = "",
    prompt: str = "",
    base_ref: str = "HEAD",
    branch: str = "",
    owner_id: str = "",
    owner_label: str = "",
    codex_home: Path | None = None,
    worktrees_root: Path | None = None,
) -> WorktreeTask:
    repo_root = resolve_git_root(cwd)
    try:
        git_stdout(repo_root, ["rev-parse", "--verify", base_ref], timeout=15.0)
    except WorktreeTaskError as exc:
        raise WorktreeTaskError(f"Could not resolve base ref {base_ref!r}.") from exc

    clean_label = clean_text(label, 180) or clean_text(prompt, 80) or f"Codex task from {current_ref(repo_root)}"
    task_id = task_id_for(clean_label, repo_root)
    root = (worktrees_root.expanduser() if worktrees_root else default_worktrees_root(codex_home)) / repo_slug(repo_root)
    worktree_path = root / task_id
    if worktree_path.exists():
        raise WorktreeTaskError(f"Worktree path already exists: {worktree_path}")

    root.mkdir(parents=True, exist_ok=True)
    clean_branch = clean_text(branch, 220)
    args = ["worktree", "add"]
    mode = "detached"
    if clean_branch:
        args.extend(["-b", clean_branch])
        mode = "branch"
    else:
        args.append("--detach")
    args.extend([str(worktree_path), base_ref])
    try:
        git_run(repo_root, args, timeout=180.0)
    except WorktreeTaskError:
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
        raise

    now = time.time()
    task = WorktreeTask(
        task_id=task_id,
        label=clean_label,
        prompt=str(prompt or "").strip(),
        repo_root=repo_root,
        worktree_path=worktree_path.resolve(),
        base_ref=base_ref,
        branch=clean_branch,
        mode=mode,
        status="created",
        owner_id=clean_text(owner_id, 120),
        owner_label=clean_text(owner_label, 180),
        created_at=now,
        updated_at=now,
        local_dirty_at_create=is_dirty(repo_root),
    )
    data = load_task_registry(codex_home)
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        tasks = {}
        data["tasks"] = tasks
    tasks[task.task_id] = task.to_record()
    save_task_registry(data, codex_home)
    return task


def branch_worktree_task(
    task_id: str,
    branch: str,
    *,
    codex_home: Path | None = None,
) -> WorktreeTask:
    task = get_worktree_task(task_id, codex_home=codex_home)
    clean_branch = clean_text(branch, 220)
    if not clean_branch:
        raise WorktreeTaskError("Branch name is required.")
    if not task.worktree_path.exists():
        raise WorktreeTaskError(f"Worktree path is missing: {task.worktree_path}")
    git_run(task.worktree_path, ["switch", "-c", clean_branch], timeout=120.0)
    return update_worktree_task(task.task_id, codex_home=codex_home, branch=clean_branch, mode="branch")


def remove_worktree_task(
    task_id: str,
    *,
    codex_home: Path | None = None,
    force: bool = False,
) -> WorktreeTask:
    task = get_worktree_task(task_id, codex_home=codex_home)
    if task.worktree_path.exists() and not force and is_dirty(task.worktree_path):
        raise WorktreeTaskError(
            "Worktree has uncommitted changes. Commit, stash, or rerun with --force before removing it."
        )
    if task.worktree_path.exists():
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(task.worktree_path))
        git_run(task.repo_root, args, timeout=180.0)
    return update_worktree_task(
        task.task_id,
        codex_home=codex_home,
        status="removed",
        removed_at=time.time(),
        terminal_state="closed",
    )


def task_status_report(task: WorktreeTask) -> dict[str, Any]:
    report: dict[str, Any] = {
        "task_id": task.task_id,
        "label": task.label,
        "status": task.status,
        "repo_root": str(task.repo_root),
        "worktree_path": str(task.worktree_path),
        "base_ref": task.base_ref,
        "branch": task.branch,
        "mode": task.mode,
        "owner_id": task.owner_id,
        "owner_label": task.owner_label,
        "terminal_state": task.terminal_state,
        "session_id": task.session_id,
        "exists": task.worktree_path.exists(),
        "dirty": False,
        "changed_files": [],
        "diff_stat": "",
        "head": "",
        "local_dirty_at_create": task.local_dirty_at_create,
    }
    if not task.worktree_path.exists() or task.status == "removed":
        return report
    report["head"] = git_stdout(task.worktree_path, ["rev-parse", "--short", "HEAD"], timeout=10.0)
    status = git_stdout(task.worktree_path, ["status", "--porcelain"], timeout=20.0)
    report["dirty"] = bool(status)
    report["changed_files"] = [line for line in status.splitlines() if line.strip()]
    stat = git_run(task.worktree_path, ["diff", "--stat", "HEAD"], timeout=30.0, check=False)
    report["diff_stat"] = clean_git_output(stat.stdout)
    if not report["branch"]:
        report["branch"] = git_stdout(task.worktree_path, ["branch", "--show-current"], timeout=10.0)
    return report


def format_task_report(task: WorktreeTask) -> str:
    report = task_status_report(task)
    lines = [
        f"{report['label']} ({report['task_id']})",
        f"Status: {report['status']}",
        f"Repo: {report['repo_root']}",
        f"Worktree: {report['worktree_path']}",
        f"Mode: {report['mode']}" + (f" on {report['branch']}" if report.get("branch") else ""),
        f"HEAD: {report['head'] or 'unknown'}",
        f"Dirty: {'yes' if report['dirty'] else 'no'}",
    ]
    if report["terminal_state"]:
        lines.append(f"Terminal: {report['terminal_state']}")
    if report["session_id"]:
        lines.append(f"Codex session: {str(report['session_id'])[:8]}")
    if report["local_dirty_at_create"]:
        lines.append("Note: local checkout had uncommitted changes when this task was created.")
    changed = report.get("changed_files") or []
    if changed:
        lines.append("Changed files:")
        lines.extend(f"  {line}" for line in changed[:12])
        if len(changed) > 12:
            lines.append(f"  ... {len(changed) - 12} more")
    if report.get("diff_stat"):
        lines.append("Diff stat:")
        lines.append(str(report["diff_stat"]))
    return "\n".join(lines)


def summarize_tasks(tasks: list[WorktreeTask], *, limit: int = 8) -> str:
    if not tasks:
        return "No active worktree tasks."
    lines = []
    for task in tasks[:limit]:
        report = task_status_report(task)
        dirty = "dirty" if report["dirty"] else "clean"
        owner = f" - {task.owner_label}" if task.owner_label else ""
        lines.append(f"{task.task_id}: {task.label} ({task.status}, {dirty}){owner}")
        lines.append(f"  {task.worktree_path}")
    if len(tasks) > limit:
        lines.append(f"... {len(tasks) - limit} more")
    return "\n".join(lines)


def default_codex_command(cwd: Path, prompt: str = "") -> list[str]:
    executable = next((path for path in codex_candidate_paths() if path.is_file() and os.access(path, os.X_OK)), None)
    codex = str(executable) if executable is not None else shutil.which("codex") or "codex"
    command = [
        codex,
        "--cd",
        str(cwd),
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "untrusted",
        "--no-alt-screen",
    ]
    if prompt.strip():
        command.append(prompt.strip())
    return command


def terminal_launcher_args(title: str, command: list[str], terminal_name: str = "auto") -> list[str] | None:
    candidates = [terminal_name] if terminal_name and terminal_name != "auto" else [
        "gnome-terminal",
        "x-terminal-emulator",
        "kgx",
        "konsole",
        "xfce4-terminal",
        "xterm",
    ]
    for candidate in candidates:
        program = shutil.which(candidate)
        if not program:
            continue
        name = Path(program).name
        if name == "gnome-terminal":
            return [program, "--title", title, "--", *command]
        if name == "kgx":
            return [program, "--title", title, "--", *command]
        if name == "konsole":
            return [program, "--new-tab", "-p", f"tabtitle={title}", "-e", *command]
        if name == "xfce4-terminal":
            return [program, "--title", title, "--command", " ".join(shlex_quote(part) for part in command)]
        if name in {"x-terminal-emulator", "xterm"}:
            return [program, "-T", title, "-e", *command]
    return None


def launch_worktree_task_terminal(
    task: WorktreeTask,
    *,
    owner_id: str = "",
    owner_label: str = "",
    codex_home: Path | None = None,
    pointer_path: Path | None = None,
    terminal_name: str = "auto",
    title: str = "",
    prompt: str = "",
    command: list[str] | None = None,
) -> tuple[bool, str]:
    pointer = pointer_path or default_pointer_path(task.task_id, codex_home)
    resolved_owner_id = clean_text(owner_id or task.owner_id, 120)
    resolved_owner_label = clean_text(owner_label or task.owner_label, 180)
    task_prompt = prompt if prompt else task.prompt
    codex_command = list(command or default_codex_command(task.worktree_path, task_prompt))
    bridge = [
        sys.executable,
        "-m",
        "hatchpet.codex_terminal_bridge",
        "--pointer",
        str(pointer),
        "--cwd",
        str(task.worktree_path),
        "--owner-id",
        resolved_owner_id,
        "--owner-label",
        resolved_owner_label,
        "--worktree-task-id",
        task.task_id,
        "--",
        *codex_command,
    ]
    terminal_title = title or f"Codex Worktree {task.task_id[:11]}"
    terminal_args = terminal_launcher_args(terminal_title, bridge, terminal_name)
    if terminal_args is None:
        return False, "Could not find a supported terminal emulator."
    try:
        subprocess.Popen(
            terminal_args,
            cwd=str(task.worktree_path),
            env=os.environ.copy(),
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return False, str(exc)
    update_worktree_task(
        task.task_id,
        codex_home=codex_home,
        owner_id=resolved_owner_id,
        owner_label=resolved_owner_label,
        pointer_path=pointer,
        terminal_state="launching",
        status="terminal-launching",
    )
    return True, f"Opened {terminal_title} in {task.worktree_path}."


def clean_git_output(value: object, limit: int = 2400) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
