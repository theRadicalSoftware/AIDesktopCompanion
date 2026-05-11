from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

ACTIVE_SESSION_DIR = "ai-desktop-companion"
ACTIVE_SESSION_FILE = "active-session.json"
SESSION_OWNERS_FILE = "session-owners.json"


def clean_owner_id(value: Any) -> str:
    return " ".join(str(value or "").split())


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return left.expanduser() == right.expanduser()


@dataclass
class CodexStatus:
    text: str
    active: bool
    session_id: str | None
    session_path: Path | None
    headline: str = ""
    detail: str = ""
    meta: str = ""
    visible: bool = False
    waiting_for_user: bool = False
    waiting_kind: str = ""
    replyable: bool = False


class CodexSessionMonitor:
    def __init__(
        self,
        *,
        selector: str = "latest",
        codex_home: Path | None = None,
        tail_bytes: int = 2_000_000,
        owner_id: str | None = None,
        exclude_foreign_owned: bool = True,
    ) -> None:
        self.selector = selector
        self.codex_home = codex_home or (Path.home() / ".codex")
        self.tail_bytes = tail_bytes
        self.owner_id = clean_owner_id(owner_id)
        self.exclude_foreign_owned = exclude_foreign_owned
        self.session_path: Path | None = None
        self.offset = 0
        self.discard_partial_line = False
        self.status_text = "Watching Codex"
        self.active = False
        self.current_turn_id: str | None = None
        self.pending_calls: dict[str, str] = {}
        self.pending_approval_calls: dict[str, tuple[str, float, float]] = {}
        self.request_text = ""
        self.current_step = ""
        self.current_action = ""
        self.last_result = ""
        self.agent_text = ""
        self.awaiting_user = False
        self.waiting_kind = ""
        self.replyable = False

    def poll(self) -> CodexStatus:
        path = self.resolve_session_path()
        if path is None:
            return CodexStatus(
                "No Codex session found",
                False,
                None,
                None,
                headline="Codex link offline",
                detail="No local rollout file is available yet.",
            )

        if path != self.session_path:
            self.session_path = path
            self.offset = max(0, path.stat().st_size - self.tail_bytes)
            self.discard_partial_line = self.offset > 0
            self.status_text = "Linked to Codex"
            self.active = False
            self.awaiting_user = False
            self.pending_calls.clear()
            self.pending_approval_calls.clear()
            self.request_text = self.latest_history_text_for_session(self.session_id_from_path(path)) or ""
            self.current_step = ""
            self.current_action = "Reading the local Codex session stream"
            self.last_result = ""
            self.agent_text = ""
            self.waiting_kind = ""
            self.replyable = False

        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                if self.discard_partial_line:
                    handle.readline()
                    self.discard_partial_line = False
                lines = handle.readlines()
                self.offset = handle.tell()
        except OSError:
            return CodexStatus(
                "Codex session unavailable",
                False,
                self.session_id_from_path(path),
                path,
                headline="Codex link unavailable",
                detail="The rollout file could not be read.",
            )

        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.consume_event(event)

        return self.build_status(path)

    def build_status(self, path: Path) -> CodexStatus:
        pending_approval = self.pending_approval_summary()
        waiting_for_user = self.awaiting_user
        waiting_kind = self.waiting_kind if self.awaiting_user else ""
        replyable = self.replyable if self.awaiting_user else False
        active = self.active
        if pending_approval:
            waiting_for_user = True
            waiting_kind = "approval"
            replyable = False
            active = False
        if pending_approval:
            headline = "Action required"
            detail = pending_approval
            visible = True
        elif self.awaiting_user:
            headline = "Waiting on you"
            detail = self.current_action or "Codex needs your response to continue."
            visible = True
        elif self.active:
            headline = "Codex is working"
            detail_parts = []
            if self.current_step:
                detail_parts.append("Plan: " + self.current_step)
            if self.current_action:
                detail_parts.append("Now: " + self.current_action)
            if self.last_result:
                detail_parts.append("Done: " + self.last_result)
            if self.agent_text:
                detail_parts.append(self.agent_text)
            detail = "\n".join(detail_parts[:4]) or "Working through the current turn."
            visible = True
        else:
            headline = "Codex is idle"
            detail = self.last_result or self.current_step or "Waiting for Codex activity."
            visible = False
        meta = ""
        text = " ".join(part for part in [headline, detail.replace("\n", " ")] if part)
        return CodexStatus(
            self.clean(text, 260),
            active,
            self.session_id_from_path(path),
            path,
            headline=headline,
            detail=detail,
            meta=meta,
            visible=visible,
            waiting_for_user=waiting_for_user,
            waiting_kind=waiting_kind,
            replyable=replyable,
        )

    def resolve_session_path(self) -> Path | None:
        selector = (self.selector or "latest").strip()
        if selector.lower() in {"off", "none", "disabled", "false", "0"}:
            return None

        if selector.startswith("pointer:"):
            pointer = Path(selector[len("pointer:") :]).expanduser()
            return self.resolve_session_pointer_path(pointer)

        candidate = Path(selector).expanduser()
        if candidate.is_file():
            pointer = self.resolve_session_pointer_path(candidate)
            if pointer is not None:
                return pointer
            return candidate.resolve()

        if selector.lower() in {"active", "selected", "pinned"}:
            active = self.resolve_active_session_path()
            if active is not None:
                return active
            return None

        sessions_root = self.codex_home / "sessions"
        if not sessions_root.exists():
            return None

        if selector.lower() == "current":
            current = self.current_session_id_from_history()
            if current:
                path = self.session_path_for_id(current)
                if path is not None:
                    return path
            return None

        if selector.lower() in {"latest", "auto"}:
            files = [
                path
                for path in sessions_root.rglob("rollout-*.jsonl")
                if path.is_file() and not self.session_is_foreign_owned(session_path=path)
            ]
            if not files:
                return None
            return max(files, key=lambda path: path.stat().st_mtime).resolve()

        if UUID_RE.match(selector):
            path = self.session_path_for_id(selector)
            if path is not None:
                return path

        return None

    def session_path_for_id(self, session_id: str) -> Path | None:
        if not UUID_RE.match(session_id):
            return None
        sessions_root = self.codex_home / "sessions"
        matches = list(sessions_root.rglob(f"rollout-*{session_id}.jsonl")) if sessions_root.exists() else []
        if matches:
            return max(matches, key=lambda path: path.stat().st_mtime).resolve()
        return None

    def active_session_pointer_path(self) -> Path:
        return self.codex_home / ACTIVE_SESSION_DIR / ACTIVE_SESSION_FILE

    def resolve_active_session_path(self) -> Path | None:
        return self.resolve_session_pointer_path(self.active_session_pointer_path())

    def resolve_session_pointer_path(self, pointer: Path) -> Path | None:
        if not pointer.is_file():
            return None
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None

        path_value = data.get("rollout_path") or data.get("session_path")
        if isinstance(path_value, str) and path_value.strip():
            path = Path(path_value).expanduser()
            if path.is_file():
                return path.resolve()

        session_id = data.get("session_id")
        if isinstance(session_id, str) and UUID_RE.match(session_id):
            sessions_root = self.codex_home / "sessions"
            matches = list(sessions_root.rglob(f"rollout-*{session_id}.jsonl")) if sessions_root.exists() else []
            if matches:
                return max(matches, key=lambda path: path.stat().st_mtime).resolve()
        return None

    def session_owners_path(self) -> Path:
        return self.codex_home / ACTIVE_SESSION_DIR / SESSION_OWNERS_FILE

    def load_session_owner_registry(self) -> dict[str, Any]:
        path = self.session_owners_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def load_session_owners(self) -> dict[str, dict[str, Any]]:
        data = self.load_session_owner_registry()
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            return {}
        return {key: value for key, value in sessions.items() if isinstance(key, str) and isinstance(value, dict)}

    def load_pending_session_owners(self) -> list[dict[str, Any]]:
        data = self.load_session_owner_registry()
        pending = data.get("pending")
        if not isinstance(pending, list):
            return []
        now = time.time()
        records = []
        for record in pending:
            if not isinstance(record, dict):
                continue
            try:
                expires_at = float(record.get("expires_at") or 0.0)
            except (TypeError, ValueError):
                expires_at = 0.0
            if expires_at and expires_at < now:
                continue
            records.append(record)
        return records

    def session_owner_record(
        self,
        *,
        session_id: str | None = None,
        session_path: Path | None = None,
    ) -> dict[str, Any]:
        owners = self.load_session_owners()
        resolved_id = session_id or self.session_id_from_path(session_path)
        if resolved_id and resolved_id in owners:
            return owners[resolved_id]

        if session_path is not None:
            try:
                resolved_path = str(session_path.expanduser().resolve())
            except OSError:
                resolved_path = str(session_path.expanduser())
            for record in owners.values():
                if str(record.get("rollout_path") or "") == resolved_path:
                    return record
        return {}

    def session_is_foreign_owned(
        self,
        *,
        session_id: str | None = None,
        session_path: Path | None = None,
    ) -> bool:
        if not self.exclude_foreign_owned or not self.owner_id:
            return False
        record = self.session_owner_record(session_id=session_id, session_path=session_path)
        owner = clean_owner_id(record.get("owner_id") or record.get("pet_id"))
        if owner:
            return owner != self.owner_id
        path = session_path
        if path is None and session_id:
            path = self.session_path_for_id(session_id)
        return bool(path is not None and self.session_matches_foreign_pending_owner(path))

    def session_matches_foreign_pending_owner(self, session_path: Path) -> bool:
        pending = self.load_pending_session_owners()
        if not pending:
            return False
        try:
            stat = session_path.stat()
        except OSError:
            return False
        meta = self.rollout_meta(session_path)
        meta_cwd = meta.get("cwd")
        if not isinstance(meta_cwd, str) or not meta_cwd.strip():
            return False
        session_cwd = Path(meta_cwd).expanduser()
        try:
            resolved_session_path = str(session_path.expanduser().resolve())
        except OSError:
            resolved_session_path = str(session_path.expanduser())
        for record in pending:
            owner = clean_owner_id(record.get("owner_id") or record.get("pet_id"))
            if not owner or owner == self.owner_id:
                continue
            known_rollouts = record.get("known_rollouts")
            if isinstance(known_rollouts, list) and resolved_session_path in {str(item) for item in known_rollouts}:
                continue
            raw_cwd = clean_text(record.get("cwd"))
            if not raw_cwd:
                continue
            try:
                started_at = float(record.get("started_at") or 0.0)
            except (TypeError, ValueError):
                started_at = 0.0
            if started_at and stat.st_mtime < started_at - 5.0:
                continue
            if same_path(session_cwd, Path(raw_cwd)):
                return True
        return False

    def rollout_meta(self, path: Path) -> dict[str, Any]:
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

    def consume_event(self, event: dict[str, Any]) -> None:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return

        event_type = payload.get("type")
        if event_type == "task_started":
            self.active = True
            self.awaiting_user = False
            self.waiting_kind = ""
            self.replyable = False
            self.pending_approval_calls.clear()
            self.current_turn_id = self.clean(payload.get("turn_id"))
            self.current_step = ""
            self.current_action = "Reading the turn and loading context"
            self.last_result = ""
            self.agent_text = ""
            self.status_text = self.current_action
        elif event_type == "task_complete":
            self.active = False
            self.current_action = ""
            last_message = self.clean(payload.get("last_agent_message"), 500)
            if self.looks_like_waiting_for_user(last_message):
                self.awaiting_user = True
                self.waiting_kind = "prompt"
                self.replyable = True
                self.last_result = ""
                self.current_action = "Please respond in Codex to continue."
                self.status_text = "Waiting on your response"
            else:
                self.awaiting_user = False
                self.waiting_kind = ""
                self.replyable = False
                self.pending_approval_calls.clear()
                self.last_result = "Finished the current turn"
                self.status_text = self.last_result
        elif event_type == "user_message":
            self.active = True
            self.awaiting_user = False
            self.waiting_kind = ""
            self.replyable = False
            self.pending_approval_calls.clear()
            self.request_text = self.clean_user_request(payload.get("message"))
            self.current_action = "Understanding the turn"
            self.status_text = self.current_action
        elif event_type == "agent_message":
            message = self.clean(payload.get("message"), 220)
            if self.looks_like_waiting_for_user(message):
                self.active = False
                self.awaiting_user = True
                self.waiting_kind = "prompt"
                self.replyable = True
                self.current_action = "Please respond in Codex to continue."
                self.status_text = "Waiting on your response"
                return
            self.active = True
            self.awaiting_user = False
            self.waiting_kind = ""
            self.replyable = False
            self.pending_approval_calls.clear()
            self.current_action = self.clean(message, 140)
            self.status_text = self.current_action
        elif event_type == "exec_command_end":
            self.consume_exec_end(payload)
        elif event_type == "patch_apply_end":
            self.consume_patch_end(payload)
        elif event_type == "collab_agent_spawn_end":
            nickname = self.clean(payload.get("new_agent_nickname"), 32) or "agent"
            self.active = True
            self.awaiting_user = False
            self.status_text = f"Spawned subagent {nickname}"
        elif event_type == "collab_waiting_end":
            self.consume_collab_wait(payload)
        elif event_type == "error":
            self.active = False
            self.awaiting_user = True
            self.waiting_kind = "error"
            self.replyable = False
            self.pending_approval_calls.clear()
            self.current_action = "Codex hit an error; respond in Codex to continue."
            self.last_result = "Hit an error: " + self.clean(payload.get("message"), 80)
            self.status_text = self.last_result
        elif event_type == "context_compacted":
            self.active = True
            self.awaiting_user = False
            self.waiting_kind = ""
            self.replyable = False
            self.pending_approval_calls.clear()
            self.current_action = "Context compacted; continuing with the task"
            self.status_text = self.current_action
        elif event_type == "turn_aborted":
            self.active = False
            self.awaiting_user = False
            self.waiting_kind = ""
            self.replyable = False
            self.pending_approval_calls.clear()
            self.current_action = ""
            self.last_result = "Turn stopped"
            self.status_text = self.last_result

        response_type = payload.get("type")
        if event.get("type") == "response_item":
            if response_type == "message":
                self.consume_response_message(payload)
            elif response_type == "reasoning":
                self.active = True
                self.awaiting_user = False
                self.waiting_kind = ""
                self.replyable = False
                self.current_action = "Thinking through the next step"
                self.status_text = self.current_action
            elif response_type == "function_call":
                self.consume_function_call(payload)
            elif response_type == "custom_tool_call":
                self.consume_custom_tool_call(payload)
            elif response_type == "custom_tool_call_output":
                self.consume_custom_tool_call_output(payload)
            elif response_type == "function_call_output":
                self.consume_function_call_output(payload)

    def consume_response_message(self, payload: dict[str, Any]) -> None:
        if payload.get("role") != "assistant":
            return
        parts = []
        for item in payload.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "output_text":
                parts.append(str(item.get("text") or ""))
        text = self.clean(" ".join(parts), 110)
        if text:
            if self.looks_like_waiting_for_user(text):
                self.active = False
                self.awaiting_user = True
                self.waiting_kind = "prompt"
                self.replyable = True
                self.current_action = "Please respond in Codex to continue."
                self.status_text = "Waiting on your response"
                return
            self.active = True
            self.awaiting_user = False
            self.waiting_kind = ""
            self.replyable = False
            self.current_action = text
            self.status_text = text

    def consume_function_call(self, payload: dict[str, Any]) -> None:
        name = self.clean(payload.get("name"), 40) or "tool"
        call_id = self.clean(payload.get("call_id"), 80)
        args = self.parse_json_object(payload.get("arguments"))
        summary = self.tool_summary(name, args)
        waiting_kind = self.function_waiting_kind(name, args) if self.function_waits_for_user(name, args) else ""
        if call_id:
            self.pending_calls[call_id] = summary
            if waiting_kind == "approval":
                now = time.monotonic()
                grace_seconds = self.approval_grace_seconds(args)
                self.pending_approval_calls[call_id] = (
                    self.waiting_summary(name, args),
                    now,
                    now + grace_seconds,
                )
        if waiting_kind:
            self.active = False
            self.awaiting_user = True
            self.waiting_kind = waiting_kind
            self.replyable = self.waiting_kind in {"choice", "prompt"}
            self.current_action = self.waiting_summary(name, args)
            self.status_text = self.current_action
            return
        self.active = True
        self.awaiting_user = False
        self.waiting_kind = ""
        self.replyable = False
        self.current_action = summary
        self.status_text = summary

    def consume_function_call_output(self, payload: dict[str, Any]) -> None:
        call_id = self.clean(payload.get("call_id"), 80)
        running = self.pending_calls.get(call_id, "") if call_id else ""
        if call_id:
            self.pending_approval_calls.pop(call_id, None)
        if not running:
            return
        self.active = True
        self.awaiting_user = False
        self.waiting_kind = ""
        self.replyable = False
        self.current_action = running
        self.status_text = running

    def consume_custom_tool_call(self, payload: dict[str, Any]) -> None:
        name = self.clean(payload.get("name"), 40) or "tool"
        call_id = self.clean(payload.get("call_id"), 80)
        summary = "Editing files" if name == "apply_patch" else f"Using {name}"
        if call_id:
            self.pending_calls[call_id] = summary
        self.active = True
        self.awaiting_user = False
        self.waiting_kind = ""
        self.replyable = False
        self.current_action = summary
        self.status_text = summary

    def consume_custom_tool_call_output(self, payload: dict[str, Any]) -> None:
        call_id = self.clean(payload.get("call_id"), 80)
        running = self.pending_calls.pop(call_id, "") if call_id else ""
        if call_id:
            self.pending_approval_calls.pop(call_id, None)
        if not running:
            return
        self.active = True
        self.awaiting_user = False
        self.waiting_kind = ""
        self.replyable = False
        self.last_result = running.removeprefix("Using ") + " finished"
        self.current_action = "Reviewing the result"
        self.status_text = self.last_result

    def consume_exec_end(self, payload: dict[str, Any]) -> None:
        call_id = self.clean(payload.get("call_id"), 80)
        running = self.pending_calls.pop(call_id, "") if call_id else ""
        if call_id:
            self.pending_approval_calls.pop(call_id, None)
        command = payload.get("command") or []
        shell = command[-1] if command else ""
        status = "finished"
        if payload.get("exit_code") not in {None, 0}:
            status = f"exit {payload.get('exit_code')}"
        summary = running or ("Running " + self.command_summary(str(shell)))
        result_label = summary.removeprefix("Running ").removeprefix("Using ")
        self.active = True
        self.awaiting_user = False
        self.waiting_kind = ""
        self.replyable = False
        self.last_result = f"{result_label} {status}"
        if running and self.current_action == running:
            self.current_action = "Reviewing the result"
        self.status_text = self.last_result

    def consume_patch_end(self, payload: dict[str, Any]) -> None:
        call_id = self.clean(payload.get("call_id"), 80)
        if call_id:
            self.pending_calls.pop(call_id, None)
            self.pending_approval_calls.pop(call_id, None)
        changes = payload.get("changes")
        count = len(changes) if isinstance(changes, dict) else 0
        self.active = True
        self.awaiting_user = False
        self.waiting_kind = ""
        self.replyable = False
        self.last_result = f"Applied patch to {count} file" + ("" if count == 1 else "s")
        self.current_action = "Reviewing the edited files"
        self.status_text = self.last_result

    def consume_collab_wait(self, payload: dict[str, Any]) -> None:
        statuses = payload.get("agent_statuses") or []
        names = []
        for status in statuses:
            if isinstance(status, dict):
                name = self.clean(status.get("agent_nickname"), 32)
                if name:
                    names.append(name)
        if names:
            self.agent_text = "Subagent finished: " + ", ".join(names[:3])
        else:
            self.agent_text = "Subagent finished"
        self.status_text = self.agent_text
        self.active = True
        self.awaiting_user = False
        self.waiting_kind = ""
        self.replyable = False

    def pending_approval_summary(self) -> str:
        if not self.pending_approval_calls:
            return ""
        now = time.monotonic()
        ready = [summary for summary, _seen_at, ready_at in self.pending_approval_calls.values() if now >= ready_at]
        if not ready:
            return ""
        return ready[-1]

    def tool_summary(self, name: str, args: dict[str, Any]) -> str:
        if name == "exec_command":
            return "Running " + self.command_summary(str(args.get("cmd") or "shell command"))
        if name == "update_plan":
            plan = args.get("plan") or []
            active = ""
            if isinstance(plan, list):
                for item in plan:
                    if isinstance(item, dict) and item.get("status") == "in_progress":
                        active = self.clean(item.get("step"), 80)
                        break
            if active:
                self.current_step = active
                return "Working on: " + active
            return "Updating the task checklist"
        if name in {"view_image", "screenshot"}:
            return "Inspecting an image"
        if name == "spawn_agent":
            return "Spawning a subagent"
        if name == "wait_agent":
            return "Waiting on subagents"
        return f"Using {name}"

    def command_summary(self, command: str) -> str:
        command = self.clean(command, 140)
        if "launch-companion.sh" in command:
            return "Companion relaunch"
        if "compileall" in command:
            return "pet runtime compilation"
        if "CodexSessionMonitor" in command or "codex_monitor" in command:
            return "the Codex session monitor test"
        if "desktop_pet.py" in command:
            return "the desktop pet runtime"
        if "pet.json" in command:
            return "Companion settings check"
        if "README.md" in command:
            return "the project notes"
        if ".codex/sessions" in command or ".codex/history.jsonl" in command:
            return "local Codex session activity"
        if ".codex/log" in command or "logs_2.sqlite" in command or "state_5.sqlite" in command:
            return "local Codex state inspection"
        if command.startswith("python3 - <<"):
            return "a Python helper"
        if command.startswith("python3 "):
            parts = command.split()
            if len(parts) >= 3 and parts[1] == "run.py" and parts[2] == "run":
                return "checking the pet run command"
            if len(parts) >= 3 and parts[1] == "-m":
                return "python module " + parts[2]
            if len(parts) >= 2:
                return "python " + Path(parts[1]).name
        if command.startswith("sed ") or command.startswith("nl ") or command.startswith("tail "):
            return "project file read"
        if command.startswith("find "):
            return "file search"
        if command.startswith("systemctl "):
            return "pet service check"
        return command

    def function_waits_for_user(self, name: str, args: dict[str, Any]) -> bool:
        if name in {"request_user_input", "ask_user", "confirm"}:
            return True
        if name == "exec_command" and args.get("sandbox_permissions") == "require_escalated":
            return True
        return False

    def function_waiting_kind(self, name: str, args: dict[str, Any]) -> str:
        if name == "exec_command" and args.get("sandbox_permissions") == "require_escalated":
            return "approval"
        if name == "request_user_input":
            return "choice"
        if name in {"ask_user", "confirm"}:
            return "prompt"
        return "prompt"

    def approval_grace_seconds(self, _args: dict[str, Any]) -> float:
        return 0.0

    def waiting_summary(self, name: str, args: dict[str, Any]) -> str:
        if name == "exec_command":
            command = self.command_summary(str(args.get("cmd") or "command"))
            return "Waiting for approval: " + command
        if name == "request_user_input":
            return "Waiting for your choice in Codex."
        return "Waiting for your response in Codex"

    def patch_waiting_summary(self, value: Any) -> str:
        text = str(value or "")
        paths: list[str] = []
        for line in text.splitlines():
            marker = ""
            for candidate in ("*** Update File: ", "*** Add File: ", "*** Delete File: "):
                if line.startswith(candidate):
                    marker = candidate
                    break
            if marker:
                paths.append(line.removeprefix(marker).strip())
        if not paths:
            return "Waiting to approve file edits."
        if len(paths) == 1:
            return "Waiting to approve edits: " + Path(paths[0]).name
        return f"Waiting to approve edits in {len(paths)} files."

    def looks_like_waiting_for_user(self, value: Any) -> bool:
        text = self.wait_detection_text(value)
        if not text:
            return False
        question_starts = [
            "should i proceed",
            "do you want me to",
            "do you want to",
            "would you like me to",
            "would you like to",
        ]
        if any(text.startswith(phrase) for phrase in question_starts):
            return True
        phrases = [
            "please confirm",
            "need your confirmation",
            "waiting for approval",
            "requires approval",
            "respond with",
            "choose one",
            "which option",
            "which would you prefer",
            "tell me which",
            "please approve",
            "need approval",
            "approve the",
        ]
        if any(phrase in text for phrase in phrases):
            return True
        return "?" in text and any(word in text for word in ("you", "your", "proceed", "confirm"))

    def wait_detection_text(self, value: Any) -> str:
        text = self.clean(value, 500)
        if not text:
            return ""
        text = re.sub(r"`[^`]*`", " ", text)
        text = re.sub(r'"[^"]*"', " ", text)
        text = re.sub(r"“[^”]*”", " ", text)
        text = re.sub(r"‘[^’]*’", " ", text)
        return " ".join(text.lower().split())

    def clean_user_request(self, value: Any) -> str:
        text = self.clean(value, 220)
        prefixes = [
            "PLEASE PICKUP WHERE YOU LEFT OFF:",
            "PLEASE PICK UP WHERE YOU LEFT OFF:",
            "pickup where you left off:",
            "pick up where we left off:",
        ]
        lowered = text.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix.lower()):
                text = text[len(prefix) :].strip()
                break
        return self.clean(text, 150)

    def parse_json_object(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, str):
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def current_session_id_from_history(self) -> str | None:
        history = self.codex_home / "history.jsonl"
        if not history.is_file():
            return None
        try:
            with history.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 128_000))
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = event.get("session_id")
            if isinstance(session_id, str) and UUID_RE.match(session_id):
                if self.session_is_foreign_owned(session_id=session_id):
                    continue
                return session_id
        return None

    def latest_history_text_for_session(self, session_id: str | None) -> str:
        if not session_id:
            return ""
        history = self.codex_home / "history.jsonl"
        if not history.is_file():
            return ""
        try:
            with history.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 512_000))
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("session_id") == session_id and isinstance(event.get("text"), str):
                return self.clean_user_request(event["text"])
        return ""

    def clean(self, value: Any, limit: int = 120) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    def session_id_from_path(self, path: Path | None) -> str | None:
        if path is None:
            return None
        stem = path.stem
        maybe = stem.rsplit("-", 5)
        if len(maybe) >= 5:
            candidate = "-".join(maybe[-5:])
            if UUID_RE.match(candidate):
                return candidate
        return None


def write_active_session_pointer(
    *,
    codex_home: Path | None,
    session_path: Path,
    selector: str,
    cwd: Path | None = None,
    owner_id: str | None = None,
    owner_label: str | None = None,
) -> Path:
    home = codex_home or (Path.home() / ".codex")
    resolved_path = session_path.expanduser().resolve()
    monitor = CodexSessionMonitor(selector=str(resolved_path), codex_home=home)
    pointer = monitor.active_session_pointer_path()
    return write_session_pointer(
        pointer_path=pointer,
        codex_home=home,
        session_path=resolved_path,
        selector=selector,
        cwd=cwd,
        owner_id=owner_id,
        owner_label=owner_label,
    )


def write_session_pointer(
    *,
    pointer_path: Path,
    codex_home: Path | None,
    session_path: Path,
    selector: str,
    cwd: Path | None = None,
    owner_id: str | None = None,
    owner_label: str | None = None,
    terminal_state: str | None = None,
    terminal_pid: int | None = None,
    terminal_exit_code: int | None = None,
    terminal_closed_at: float | None = None,
) -> Path:
    home = codex_home or (Path.home() / ".codex")
    resolved_path = session_path.expanduser().resolve()
    monitor = CodexSessionMonitor(selector=str(resolved_path), codex_home=home)
    pointer = pointer_path.expanduser()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    clean_owner = clean_owner_id(owner_id)
    clean_label = " ".join(str(owner_label or "").split())
    payload = {
        "selector": selector,
        "session_id": monitor.session_id_from_path(resolved_path),
        "rollout_path": str(resolved_path),
        "cwd": str((cwd or Path.cwd()).expanduser().resolve()),
    }
    if clean_owner:
        payload["owner_id"] = clean_owner
    if clean_label:
        payload["owner_label"] = clean_label
    if terminal_state:
        payload["terminal_state"] = clean_text(terminal_state)
    if terminal_pid is not None:
        payload["terminal_pid"] = int(terminal_pid)
    if terminal_exit_code is not None:
        payload["terminal_exit_code"] = int(terminal_exit_code)
    if terminal_closed_at is not None:
        payload["terminal_closed_at"] = float(terminal_closed_at)
    pointer.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if clean_owner:
        write_session_owner(
            codex_home=home,
            session_path=resolved_path,
            owner_id=clean_owner,
            owner_label=clean_label,
            pointer_path=pointer,
            selector=selector,
            cwd=cwd,
        )
    return pointer


def write_terminal_pointer_state(
    *,
    pointer_path: Path,
    codex_home: Path | None,
    selector: str,
    cwd: Path | None = None,
    owner_id: str | None = None,
    owner_label: str | None = None,
    terminal_state: str,
    terminal_pid: int | None = None,
    terminal_exit_code: int | None = None,
    terminal_closed_at: float | None = None,
) -> Path:
    pointer = pointer_path.expanduser()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    clean_owner = clean_owner_id(owner_id)
    clean_label = clean_text(owner_label)
    payload: dict[str, Any] = {
        "selector": selector,
        "cwd": str((cwd or Path.cwd()).expanduser().resolve()),
        "terminal_state": clean_text(terminal_state),
    }
    if clean_owner:
        payload["owner_id"] = clean_owner
    if clean_label:
        payload["owner_label"] = clean_label
    if terminal_pid is not None:
        payload["terminal_pid"] = int(terminal_pid)
    if terminal_exit_code is not None:
        payload["terminal_exit_code"] = int(terminal_exit_code)
    if terminal_closed_at is not None:
        payload["terminal_closed_at"] = float(terminal_closed_at)
    pointer.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return pointer


def write_session_owner(
    *,
    codex_home: Path | None,
    session_path: Path,
    owner_id: str,
    owner_label: str | None = None,
    pointer_path: Path | None = None,
    selector: str = "",
    cwd: Path | None = None,
) -> Path:
    home = codex_home or (Path.home() / ".codex")
    resolved_path = session_path.expanduser().resolve()
    monitor = CodexSessionMonitor(selector=str(resolved_path), codex_home=home)
    session_id = monitor.session_id_from_path(resolved_path)
    registry = home / ACTIVE_SESSION_DIR / SESSION_OWNERS_FILE
    registry.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if registry.is_file():
        try:
            parsed = json.loads(registry.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except (OSError, json.JSONDecodeError):
            data = {}

    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    data["sessions"] = sessions

    key = session_id or str(resolved_path)
    record: dict[str, Any] = {
        "owner_id": clean_owner_id(owner_id),
        "rollout_path": str(resolved_path),
        "selector": selector,
        "cwd": str((cwd or Path.cwd()).expanduser().resolve()),
        "updated_at": time.time(),
    }
    if session_id:
        record["session_id"] = session_id
    clean_label = " ".join(str(owner_label or "").split())
    if clean_label:
        record["owner_label"] = clean_label
    if pointer_path is not None:
        record["pointer_path"] = str(pointer_path.expanduser())
    sessions[key] = record

    if len(sessions) > 240:
        sortable = sorted(
            sessions.items(),
            key=lambda item: float(item[1].get("updated_at") or 0.0) if isinstance(item[1], dict) else 0.0,
            reverse=True,
        )
        data["sessions"] = dict(sortable[:240])

    tmp = registry.with_name(registry.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(registry)
    return registry


def write_pending_session_owner(
    *,
    codex_home: Path | None,
    owner_id: str,
    owner_label: str | None = None,
    cwd: Path | None = None,
    pointer_path: Path | None = None,
    selector: str = "terminal",
    ttl_seconds: float = 180.0,
) -> Path:
    home = codex_home or (Path.home() / ".codex")
    registry = home / ACTIVE_SESSION_DIR / SESSION_OWNERS_FILE
    registry.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()

    data: dict[str, Any] = {}
    if registry.is_file():
        try:
            parsed = json.loads(registry.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except (OSError, json.JSONDecodeError):
            data = {}

    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        data["sessions"] = {}

    pending = data.get("pending")
    if not isinstance(pending, list):
        pending = []

    clean_owner = clean_owner_id(owner_id)
    clean_label = clean_text(owner_label)
    resolved_cwd = (cwd or Path.cwd()).expanduser().resolve()
    resolved_pointer = str(pointer_path.expanduser()) if pointer_path is not None else ""
    sessions_root = home / "sessions"
    known_rollouts: list[str] = []
    if sessions_root.exists():
        for path in sessions_root.rglob("rollout-*.jsonl"):
            if not path.is_file():
                continue
            try:
                known_rollouts.append(str(path.resolve()))
            except OSError:
                continue

    record: dict[str, Any] = {
        "owner_id": clean_owner,
        "selector": selector,
        "cwd": str(resolved_cwd),
        "started_at": now,
        "expires_at": now + max(10.0, float(ttl_seconds)),
        "known_rollouts": known_rollouts,
    }
    if clean_label:
        record["owner_label"] = clean_label
    if resolved_pointer:
        record["pointer_path"] = resolved_pointer

    fresh = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        try:
            expires_at = float(item.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at and expires_at < now:
            continue
        if (
            clean_owner_id(item.get("owner_id") or item.get("pet_id")) == clean_owner
            and clean_text(item.get("cwd")) == str(resolved_cwd)
            and clean_text(item.get("pointer_path")) == resolved_pointer
        ):
            continue
        fresh.append(item)
    fresh.append(record)
    data["pending"] = fresh[-80:]

    tmp = registry.with_name(registry.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(registry)
    return registry
