from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .codex_monitor import write_session_pointer, write_terminal_pointer_state
from .worktree_tasks import WorktreeTaskError, update_worktree_task


def rollout_files(codex_home: Path) -> list[Path]:
    sessions = codex_home / "sessions"
    if not sessions.exists():
        return []
    return [path for path in sessions.rglob("rollout-*.jsonl") if path.is_file()]


def rollout_meta(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(24):
                line = handle.readline()
                if not line:
                    break
                if '"session_meta"' not in line:
                    continue
                event = json.loads(line)
                payload = event.get("payload")
                return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def same_cwd(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return left.expanduser() == right.expanduser()


def choose_rollout(codex_home: Path, cwd: Path, known: dict[Path, float], started_at: float) -> Path | None:
    candidates: list[tuple[float, Path]] = []
    for path in rollout_files(codex_home):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in known:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < started_at - 4:
            continue
        meta = rollout_meta(path)
        meta_cwd = meta.get("cwd")
        if isinstance(meta_cwd, str) and meta_cwd.strip() and not same_cwd(Path(meta_cwd), cwd):
            continue
        source = meta.get("source")
        if isinstance(source, dict) and "subagent" in source:
            score = mtime - 10_000
        else:
            score = mtime
        candidates.append((score, resolved))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1].resolve()


def default_codex_command(cwd: Path) -> list[str]:
    codex = (
        os.environ.get("CODEX_BINARY")
        or os.environ.get("CODEX_CLI")
        or os.environ.get("CODEX_CLI_PATH")
        or shutil.which("codex")
        or "codex"
    )
    return [
        codex,
        "--cd",
        str(cwd),
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "untrusted",
        "--no-alt-screen",
    ]


def run_bridge(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    codex_home = Path(args.codex_home).expanduser().resolve() if args.codex_home else Path.home() / ".codex"
    pointer = Path(args.pointer).expanduser()
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = default_codex_command(cwd)

    known = {}
    for path in rollout_files(codex_home):
        try:
            known[path.resolve()] = path.stat().st_mtime
        except OSError:
            continue

    started_at = time.time()
    print("Starting Codex terminal session for desktop companion.", flush=True)
    print(f"Working directory: {cwd}", flush=True)
    if args.owner_label or args.owner_id:
        print(f"Session owner: {args.owner_label or args.owner_id}", flush=True)
    linked: Path | None = None
    stop_requested = False
    process = subprocess.Popen(command, cwd=str(cwd))

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        if process.poll() is None:
            process.terminate()

    for sig in (getattr(signal, "SIGHUP", None), signal.SIGTERM, signal.SIGINT):
        if sig is None:
            continue
        try:
            signal.signal(sig, request_stop)
        except (OSError, ValueError):
            continue

    write_terminal_pointer_state(
        pointer_path=pointer,
        codex_home=codex_home,
        selector="terminal",
        cwd=cwd,
        owner_id=args.owner_id,
        owner_label=args.owner_label,
        terminal_state="running",
        terminal_pid=process.pid,
    )
    if args.worktree_task_id:
        update_task_state(
            args.worktree_task_id,
            codex_home=codex_home,
            pointer_path=pointer,
            cwd=cwd,
            owner_id=args.owner_id,
            owner_label=args.owner_label,
            terminal_state="running",
            terminal_pid=process.pid,
            status="terminal-running",
        )

    try:
        while process.poll() is None and not stop_requested:
            candidate = choose_rollout(codex_home, cwd, known, started_at)
            if candidate is not None and candidate != linked:
                linked = candidate
                write_session_pointer(
                    pointer_path=pointer,
                    codex_home=codex_home,
                    session_path=candidate,
                    selector="terminal",
                    cwd=cwd,
                    owner_id=args.owner_id,
                    owner_label=args.owner_label,
                    terminal_state="running",
                    terminal_pid=process.pid,
                )
                if args.worktree_task_id:
                    update_task_state(
                        args.worktree_task_id,
                        codex_home=codex_home,
                        pointer_path=pointer,
                        cwd=cwd,
                        owner_id=args.owner_id,
                        owner_label=args.owner_label,
                        terminal_state="running",
                        terminal_pid=process.pid,
                        status="linked",
                        session_path=candidate,
                    )
                print(f"Linked companion to Codex rollout: {candidate}", flush=True)
            time.sleep(max(0.5, float(args.poll_seconds)))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)

        exit_code = int(process.returncode or 0)
        if linked is not None:
            write_session_pointer(
                pointer_path=pointer,
                codex_home=codex_home,
                session_path=linked,
                selector="terminal",
                cwd=cwd,
                owner_id=args.owner_id,
                owner_label=args.owner_label,
                terminal_state="closed",
                terminal_pid=process.pid,
                terminal_exit_code=exit_code,
                terminal_closed_at=time.time(),
            )
            if args.worktree_task_id:
                update_task_state(
                    args.worktree_task_id,
                    codex_home=codex_home,
                    pointer_path=pointer,
                    cwd=cwd,
                    owner_id=args.owner_id,
                    owner_label=args.owner_label,
                    terminal_state="closed",
                    terminal_pid=process.pid,
                    terminal_exit_code=exit_code,
                    status="terminal-closed",
                    session_path=linked,
                )
        else:
            write_terminal_pointer_state(
                pointer_path=pointer,
                codex_home=codex_home,
                selector="terminal",
                cwd=cwd,
                owner_id=args.owner_id,
                owner_label=args.owner_label,
                terminal_state="closed",
                terminal_pid=process.pid,
                terminal_exit_code=exit_code,
                terminal_closed_at=time.time(),
            )
            if args.worktree_task_id:
                update_task_state(
                    args.worktree_task_id,
                    codex_home=codex_home,
                    pointer_path=pointer,
                    cwd=cwd,
                    owner_id=args.owner_id,
                    owner_label=args.owner_label,
                    terminal_state="closed",
                    terminal_pid=process.pid,
                    terminal_exit_code=exit_code,
                    status="terminal-closed",
                )
    return int(process.returncode or 0)


def update_task_state(
    task_id: str,
    *,
    codex_home: Path,
    pointer_path: Path,
    cwd: Path,
    owner_id: str | None,
    owner_label: str | None,
    terminal_state: str,
    terminal_pid: int | None,
    status: str,
    session_path: Path | None = None,
    terminal_exit_code: int | None = None,
) -> None:
    updates: dict[str, Any] = {
        "pointer_path": pointer_path,
        "terminal_state": terminal_state,
        "terminal_pid": terminal_pid,
        "status": status,
        "owner_id": owner_id or "",
        "owner_label": owner_label or "",
    }
    if session_path is not None:
        updates["rollout_path"] = session_path
        try:
            from .codex_monitor import CodexSessionMonitor

            monitor = CodexSessionMonitor(selector=str(session_path), codex_home=codex_home)
            updates["session_id"] = monitor.session_id_from_path(session_path)
        except Exception:
            pass
    if terminal_exit_code is not None:
        updates["terminal_exit_code"] = int(terminal_exit_code)
    try:
        update_worktree_task(task_id, codex_home=codex_home, **updates)
    except WorktreeTaskError:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Link a spawned desktop pet to a Codex terminal rollout.")
    parser.add_argument("--pointer", required=True, help="JSON pointer file watched by the pet.")
    parser.add_argument("--cwd", required=True, help="Working directory for the Codex terminal.")
    parser.add_argument("--codex-home", help="Override Codex home. Defaults to ~/.codex.")
    parser.add_argument("--owner-id", help="Pet id that owns the spawned terminal session.")
    parser.add_argument("--owner-label", help="Display name for the pet that owns the session.")
    parser.add_argument("--worktree-task-id", help="Optional worktree task id linked to this terminal.")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run after --. Defaults to codex.")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_bridge(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
