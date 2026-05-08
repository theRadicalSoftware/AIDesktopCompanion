from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any


APPROVAL_CHOICES = {
    "approve": ((("y",), ("Return",), ("1", "Return")), "approved once"),
    "trust": ((("a",), ("p",), ("Down", "Return"), ("2", "Return")), "approved and trusted"),
    "deny": ((("Escape",), ("Down", "Down", "Return"), ("3", "Return")), "denied"),
}


@dataclass(frozen=True)
class ApprovalBridgeResult:
    ok: bool
    detail: str
    window_id: str = ""
    window_title: str = ""


def current_codex_action_required(*, xdotool_path: str | None = None) -> ApprovalBridgeResult:
    xdotool = xdotool_path or shutil.which("xdotool")
    if not xdotool:
        return ApprovalBridgeResult(False, "Could not find xdotool.")

    window = find_codex_action_required_window(xdotool)
    if window is None:
        return ApprovalBridgeResult(False, "No visible Codex Action Required terminal.")
    window_id, title = window
    return ApprovalBridgeResult(
        True,
        "Codex is waiting for approval in the terminal.",
        window_id=window_id,
        window_title=title,
    )


def send_codex_terminal_approval(choice: str, *, xdotool_path: str | None = None) -> ApprovalBridgeResult:
    selected = APPROVAL_CHOICES.get(choice)
    if selected is None:
        return ApprovalBridgeResult(False, "Unsupported approval choice.")

    strategies, label = selected
    xdotool = xdotool_path or shutil.which("xdotool")
    if not xdotool:
        return ApprovalBridgeResult(
            False,
            "Could not find xdotool. Install xdotool or approve in the Codex terminal.",
        )

    window = find_codex_action_required_window(xdotool)
    if window is None:
        return ApprovalBridgeResult(
            False,
            "Could not find a visible Codex Action Required terminal.",
        )
    window_id, title = window

    last_error = ""
    for keys in strategies:
        result = send_approval_keys(xdotool, window_id, keys)
        if result.returncode != 0:
            last_error = clean_process_error(result)
            continue
        if wait_for_approval_to_clear(xdotool, window_id):
            return ApprovalBridgeResult(True, "Codex approval " + label + ".", window_id=window_id, window_title=title)

    if last_error:
        detail = last_error
    else:
        detail = "Approval keys were sent, but Codex still appears to be waiting."
    return ApprovalBridgeResult(False, detail, window_id=window_id, window_title=title)


def send_approval_keys(xdotool: str, window_id: str, keys: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    for args in (
        [xdotool, "windowactivate", "--sync", window_id],
        [xdotool, "windowfocus", "--sync", window_id],
    ):
        result = run_xdotool(args)
        if result.returncode != 0:
            return result
    time.sleep(0.08)

    for key in keys:
        result = run_xdotool([xdotool, "key", "--clearmodifiers", key])
        if result.returncode != 0:
            return result
        time.sleep(0.08)
    return subprocess.CompletedProcess([xdotool, "key", *keys], 0, "", "")


def wait_for_approval_to_clear(xdotool: str, window_id: str) -> bool:
    for _ in range(8):
        time.sleep(0.18)
        title = window_title(xdotool, window_id)
        if not is_codex_action_required_title(title):
            return True
    return False


def find_codex_action_required_window(xdotool: str) -> tuple[str, str] | None:
    result = run_xdotool([xdotool, "search", "--name", "Action Required"])
    if result.returncode != 0:
        return None

    window_ids = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
    for window_id in reversed(window_ids):
        title = window_title(xdotool, window_id)
        if is_codex_action_required_title(title):
            return window_id, title
    return None


def window_title(xdotool: str, window_id: str) -> str:
    result = run_xdotool([xdotool, "getwindowname", window_id])
    if result.returncode != 0:
        return ""
    return " ".join(result.stdout.split())


def is_codex_action_required_title(title: str) -> bool:
    text = " ".join(str(title or "").split()).lower()
    return "action required" in text


def run_xdotool(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def clean_process_error(result: subprocess.CompletedProcess[Any]) -> str:
    text = " ".join(str(result.stderr or result.stdout or "").split())
    if len(text) > 180:
        return text[:177].rstrip() + "..."
    return text
