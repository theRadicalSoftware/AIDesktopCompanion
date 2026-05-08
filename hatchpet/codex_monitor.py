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
        tail_bytes: int = 256_000,
    ) -> None:
        self.selector = selector
        self.codex_home = codex_home or (Path.home() / ".codex")
        self.tail_bytes = tail_bytes
        self.session_path: Path | None = None
        self.offset = 0
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
                if self.offset:
                    handle.readline()
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

        candidate = Path(selector).expanduser()
        if candidate.is_file():
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
                matches = list(sessions_root.rglob(f"rollout-*{current}.jsonl"))
                if matches:
                    return max(matches, key=lambda path: path.stat().st_mtime).resolve()
            return None

        if selector.lower() in {"latest", "auto"}:
            files = [path for path in sessions_root.rglob("rollout-*.jsonl") if path.is_file()]
            if not files:
                return None
            return max(files, key=lambda path: path.stat().st_mtime).resolve()

        if UUID_RE.match(selector):
            matches = list(sessions_root.rglob(f"rollout-*{selector}.jsonl"))
            if matches:
                return max(matches, key=lambda path: path.stat().st_mtime).resolve()

        return None

    def active_session_pointer_path(self) -> Path:
        return self.codex_home / ACTIVE_SESSION_DIR / ACTIVE_SESSION_FILE

    def resolve_active_session_path(self) -> Path | None:
        pointer = self.active_session_pointer_path()
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
                self.pending_approval_calls.clear()
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
        if call_id:
            self.pending_calls[call_id] = summary
            if name == "exec_command":
                now = time.monotonic()
                grace_seconds = self.approval_grace_seconds(args)
                self.pending_approval_calls[call_id] = (
                    self.waiting_summary(name, args),
                    now,
                    now + grace_seconds,
                )
        if self.function_waits_for_user(name, args):
            self.active = False
            self.awaiting_user = True
            self.waiting_kind = self.function_waiting_kind(name, args)
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
            if name == "apply_patch":
                now = time.monotonic()
                self.pending_approval_calls[call_id] = (
                    self.patch_waiting_summary(payload.get("input")),
                    now,
                    now + 0.8,
                )
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

    def approval_grace_seconds(self, args: dict[str, Any]) -> float:
        if args.get("sandbox_permissions") == "require_escalated":
            return 0.0
        try:
            yield_ms = float(args.get("yield_time_ms", 1000))
        except (TypeError, ValueError):
            yield_ms = 1000.0
        if not 0 <= yield_ms <= 60_000:
            yield_ms = 1000.0
        return max(2.5, min((yield_ms / 1000.0) + 1.0, 15.0))

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
) -> Path:
    home = codex_home or (Path.home() / ".codex")
    resolved_path = session_path.expanduser().resolve()
    monitor = CodexSessionMonitor(selector=str(resolved_path), codex_home=home)
    pointer = monitor.active_session_pointer_path()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "selector": selector,
        "session_id": monitor.session_id_from_path(resolved_path),
        "rollout_path": str(resolved_path),
        "cwd": str((cwd or Path.cwd()).expanduser().resolve()),
    }
    pointer.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return pointer
