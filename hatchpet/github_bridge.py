from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_bridge import clean_text


DEFAULT_REMOTE = "origin"
DEFAULT_MAIN_BRANCH = "main"
MAX_DETAIL_CHARS = 1600
SSH_GITHUB_RE = re.compile(r"^(git@github\.com:[^/]+/[^/]+|ssh://git@github\.com/[^/]+/[^/]+)(?:\.git)?/?$")


class GitHubBridgeError(Exception):
    pass


@dataclass(frozen=True)
class RepoContext:
    root: Path
    remote: str
    main_branch: str
    current_branch: str
    remote_url: str
    require_clean: bool = True


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        emit_error("Invalid GitHub request JSON.", detail=str(exc))
        return 1

    if not isinstance(request, dict):
        emit_error("GitHub request must be a JSON object.")
        return 1

    action = clean_action(request.get("action"))
    if action not in {"check", "push", "merge_main_push", "merge_to_main"}:
        emit_error("Unsupported GitHub action.", detail=action or "unknown action")
        return 1
    emit("ai_started", headline=action_headline(action), detail="Checking repository and GitHub SSH access.")

    try:
        context = preflight(request, require_clean=action != "check")
        if action == "check":
            emit(
                "ai_done",
                headline="GitHub is ready",
                detail=(
                    f"{context.root.name} is on {context.current_branch}; "
                    f"{context.remote} uses GitHub SSH and an SSH key is available."
                ),
            )
            return 0
        if action == "push":
            push_current_branch(context)
            return 0
        if action == "merge_main_push":
            merge_main_into_current_and_push(context)
            return 0
        if action == "merge_to_main":
            merge_current_into_main_and_push(context)
            return 0
    except GitHubBridgeError as exc:
        emit_error("GitHub action blocked", detail=str(exc))
        return 1
    except Exception as exc:
        emit_error("GitHub action failed", detail=clean_text(exc, 400))
        return 1

    return 1


def clean_action(value: object) -> str:
    action = str(value or "").strip().lower().replace("-", "_")
    if action in {"status", "auth", "preflight"}:
        return "check"
    if action in {"push_current", "push_branch"}:
        return "push"
    if action in {"merge_main", "merge_main_and_push", "pull_main_push"}:
        return "merge_main_push"
    if action in {"merge_to_main_push", "publish_main"}:
        return "merge_to_main"
    return action


def action_headline(action: str) -> str:
    if action == "check":
        return "Checking GitHub access"
    if action == "push":
        return "Pushing to GitHub"
    if action == "merge_main_push":
        return "Merging main, then pushing"
    if action == "merge_to_main":
        return "Merging into main"
    return "Running GitHub action"


def preflight(request: dict[str, Any], *, require_clean: bool) -> RepoContext:
    cwd = Path(str(request.get("cwd") or ".")).expanduser()
    root = resolve_git_root(cwd)
    if root is None:
        raise GitHubBridgeError(
            "This folder is not inside a Git repository. Open or link a Codex session in a project repo first."
        )

    remote = clean_text(request.get("remote"), 80) or DEFAULT_REMOTE
    main_branch = clean_text(request.get("main_branch") or request.get("mainBranch"), 80) or DEFAULT_MAIN_BRANCH
    configured_require_clean = bool(request.get("require_clean", request.get("requireCleanTree", True)))
    require_clean = require_clean and configured_require_clean

    current_branch = git_stdout(root, ["branch", "--show-current"])
    if not current_branch:
        raise GitHubBridgeError("GitHub actions need a normal branch. This repository is in detached HEAD state.")

    try:
        remote_url = git_stdout(root, ["remote", "get-url", remote])
    except GitHubBridgeError as exc:
        raise GitHubBridgeError(f"No {remote!r} remote is configured for this repository.") from exc
    if not remote_url:
        raise GitHubBridgeError(f"No {remote!r} remote is configured for this repository.")
    if not is_github_ssh_remote(remote_url):
        raise GitHubBridgeError(
            f"{remote!r} is not a GitHub SSH remote. Set it to git@github.com:owner/repo.git, then try again."
        )

    if require_clean and is_dirty(root):
        raise GitHubBridgeError(
            "This repository has uncommitted changes. Commit, stash, or discard them before Companion pushes or merges."
        )

    verify_ssh_access()
    return RepoContext(
        root=root,
        remote=remote,
        main_branch=main_branch,
        current_branch=current_branch,
        remote_url=remote_url,
        require_clean=require_clean,
    )


def push_current_branch(context: RepoContext) -> None:
    emit(
        "ai_delta",
        headline="Pushing to GitHub",
        detail=f"Pushing {context.current_branch} to {context.remote}.",
    )
    git_run(context.root, ["push", context.remote, "HEAD"])
    emit(
        "ai_done",
        headline="GitHub push finished",
        detail=f"Pushed {context.current_branch} to {context.remote}.",
    )


def merge_main_into_current_and_push(context: RepoContext) -> None:
    emit(
        "ai_delta",
        headline="Fetching main",
        detail=f"Fetching {context.remote}/{context.main_branch}.",
    )
    git_run(context.root, ["fetch", context.remote, context.main_branch])
    emit(
        "ai_delta",
        headline="Merging main",
        detail=f"Merging {context.remote}/{context.main_branch} into {context.current_branch}.",
    )
    git_run(context.root, ["merge", "--no-edit", f"{context.remote}/{context.main_branch}"])
    emit(
        "ai_delta",
        headline="Pushing to GitHub",
        detail=f"Pushing {context.current_branch} after the main merge.",
    )
    git_run(context.root, ["push", context.remote, "HEAD"])
    emit(
        "ai_done",
        headline="GitHub merge finished",
        detail=f"Merged {context.remote}/{context.main_branch} into {context.current_branch} and pushed.",
    )


def merge_current_into_main_and_push(context: RepoContext) -> None:
    source_branch = context.current_branch
    target = context.main_branch
    emit("ai_delta", headline="Fetching main", detail=f"Fetching {context.remote}/{target}.")
    git_run(context.root, ["fetch", context.remote, target])

    switched = False
    try:
        if source_branch != target:
            emit("ai_delta", headline="Switching branch", detail=f"Switching to {target}.")
            git_run(context.root, ["checkout", target])
            switched = True

        emit("ai_delta", headline="Updating main", detail=f"Fast-forwarding {target} from GitHub.")
        git_run(context.root, ["pull", "--ff-only", context.remote, target])

        if source_branch != target:
            emit("ai_delta", headline="Merging branch", detail=f"Merging {source_branch} into {target}.")
            git_run(context.root, ["merge", "--no-edit", source_branch])

        emit("ai_delta", headline="Pushing main", detail=f"Pushing {target} to {context.remote}.")
        git_run(context.root, ["push", context.remote, target])
    except GitHubBridgeError:
        raise
    finally:
        if switched and is_clean_enough_to_return(context.root):
            try:
                git_run(context.root, ["checkout", source_branch], emit_failure=False)
            except GitHubBridgeError:
                pass

    emit(
        "ai_done",
        headline="GitHub main updated",
        detail=f"Merged {source_branch} into {target} and pushed {target} to GitHub.",
    )


def is_github_ssh_remote(remote_url: str) -> bool:
    return bool(SSH_GITHUB_RE.match(remote_url.strip()))


def resolve_git_root(cwd: Path) -> Path | None:
    candidate = cwd.expanduser()
    if candidate.is_file():
        candidate = candidate.parent
    if not candidate.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root).expanduser().resolve() if root else None


def is_dirty(root: Path) -> bool:
    return bool(git_stdout(root, ["status", "--porcelain"]))


def is_clean_enough_to_return(root: Path) -> bool:
    try:
        return not is_dirty(root)
    except GitHubBridgeError:
        return False


def verify_ssh_access() -> None:
    if not local_ssh_key_available():
        raise GitHubBridgeError(
            "GitHub needs an SSH key. Add a GitHub SSH key to ~/.ssh or load one into ssh-agent, then try again."
        )

    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=8",
                "-T",
                "git@github.com",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except FileNotFoundError as exc:
        raise GitHubBridgeError("OpenSSH is not installed or ssh is not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubBridgeError("GitHub SSH verification timed out. Check the network and SSH key.") from exc
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    lowered = output.lower()
    if "successfully authenticated" in lowered:
        return
    if any(
        marker in lowered
        for marker in (
            "could not resolve hostname",
            "temporary failure in name resolution",
            "network is unreachable",
            "connection timed out",
            "connection refused",
            "no route to host",
        )
    ):
        raise GitHubBridgeError("Could not reach GitHub over SSH: " + clean_detail(output))
    if "permission denied" in lowered:
        raise GitHubBridgeError(
            "GitHub SSH authentication failed. Add this SSH public key to GitHub or load the right key into ssh-agent."
        )
    if result.returncode == 0:
        return
    raise GitHubBridgeError("Could not verify GitHub SSH access: " + clean_detail(output or result.returncode))


def local_ssh_key_available() -> bool:
    try:
        result = subprocess.run(
            ["ssh-add", "-l"],
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass

    ssh_dir = Path.home() / ".ssh"
    key_names = ("id_ed25519", "id_rsa", "id_ecdsa", "id_dsa")
    return any((ssh_dir / name).is_file() for name in key_names)


def git_stdout(root: Path, args: list[str]) -> str:
    result = git_run(root, args, emit_step=False, emit_failure=False)
    return result.stdout.strip()


def git_run(
    root: Path,
    args: list[str],
    *,
    emit_step: bool = True,
    emit_failure: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except FileNotFoundError as exc:
        raise GitHubBridgeError("Git is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubBridgeError("Git command timed out: git " + " ".join(args)) from exc

    if emit_step and result.returncode == 0:
        detail = clean_detail(result.stdout or result.stderr)
        if detail:
            emit("ai_delta", headline="GitHub progress", detail=detail)

    if result.returncode != 0:
        detail = clean_detail(result.stderr or result.stdout)
        if emit_failure:
            emit_error("Git command failed", detail=detail or ("git " + " ".join(args)))
        raise GitHubBridgeError(detail or ("git " + " ".join(args) + f" exited {result.returncode}"))
    return result


def clean_detail(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_DETAIL_CHARS:
        text = text[: MAX_DETAIL_CHARS - 3].rstrip() + "..."
    return text


def emit(event_type: str, **payload: object) -> None:
    data = {"type": event_type, **payload}
    print(json.dumps(data, ensure_ascii=True), flush=True)


def emit_error(headline: str, *, detail: object = "", **payload: object) -> None:
    emit("ai_error", headline=headline, detail=clean_detail(detail) or headline, **payload)


if __name__ == "__main__":
    raise SystemExit(main())
