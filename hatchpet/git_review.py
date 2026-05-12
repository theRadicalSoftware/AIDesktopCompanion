from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .codex_monitor import ACTIVE_SESSION_DIR


class GitReviewError(Exception):
    pass


@dataclass(frozen=True)
class ReviewFile:
    path: str
    code: str
    staged: bool = False
    unstaged: bool = False
    untracked: bool = False
    deleted: bool = False
    renamed_from: str = ""


@dataclass(frozen=True)
class DiffHunk:
    index: int
    header: str
    patch: str
    old_start: int = 0
    new_start: int = 0


@dataclass(frozen=True)
class ReviewState:
    root: Path
    branch: str
    head: str
    detached: bool
    dirty: bool
    staged_count: int
    unstaged_count: int
    untracked_count: int
    files: list[ReviewFile]
    diff_stat: str
    staged_diff_stat: str


def git_run(
    root: Path,
    args: list[str],
    *,
    input_text: str | None = None,
    timeout: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise GitReviewError("Git is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitReviewError("Git command timed out: git " + " ".join(args)) from exc
    if check and result.returncode != 0:
        detail = clean_output(result.stderr or result.stdout)
        raise GitReviewError(detail or f"git {' '.join(args)} exited {result.returncode}")
    return result


def git_stdout(root: Path, args: list[str], *, timeout: float = 120.0) -> str:
    return git_run(root, args, timeout=timeout).stdout.strip()


def clean_output(value: object, limit: int = 4000) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def review_state(root: Path) -> ReviewState:
    root = root.expanduser().resolve()
    branch = git_stdout(root, ["branch", "--show-current"], timeout=10.0)
    head = git_stdout(root, ["rev-parse", "--short", "HEAD"], timeout=10.0)
    files = status_files(root)
    diff_stat = git_run(root, ["diff", "--stat", "HEAD"], timeout=30.0, check=False).stdout.strip()
    staged_diff_stat = git_run(root, ["diff", "--cached", "--stat"], timeout=30.0, check=False).stdout.strip()
    return ReviewState(
        root=root,
        branch=branch,
        head=head,
        detached=not bool(branch),
        dirty=bool(files),
        staged_count=sum(1 for item in files if item.staged),
        unstaged_count=sum(1 for item in files if item.unstaged),
        untracked_count=sum(1 for item in files if item.untracked),
        files=files,
        diff_stat=diff_stat,
        staged_diff_stat=staged_diff_stat,
    )


def status_files(root: Path) -> list[ReviewFile]:
    output = git_stdout(root, ["status", "--porcelain"], timeout=20.0)
    files: list[ReviewFile] = []
    for line in output.splitlines():
        if not line:
            continue
        code = line[:2]
        raw_path = line[3:] if len(line) > 3 else ""
        renamed_from = ""
        path = raw_path
        if " -> " in raw_path:
            renamed_from, path = raw_path.split(" -> ", 1)
        x = code[0] if code else " "
        y = code[1] if len(code) > 1 else " "
        untracked = code == "??"
        deleted = "D" in code
        files.append(
            ReviewFile(
                path=path,
                code=code,
                staged=not untracked and x not in {" ", "?"},
                unstaged=not untracked and y != " ",
                untracked=untracked,
                deleted=deleted,
                renamed_from=renamed_from,
            )
        )
    files.sort(key=lambda item: item.path.lower())
    return files


def diff_for_file(root: Path, path: str, *, scope: str = "all") -> str:
    if not path:
        return combined_diff(root, scope=scope)
    file_status = next((item for item in status_files(root) if item.path == path), None)
    if file_status and file_status.untracked:
        return synthetic_untracked_diff(root / path, path)
    if scope == "staged":
        return git_run(root, ["diff", "--cached", "--", path], timeout=30.0, check=False).stdout
    if scope == "unstaged":
        return git_run(root, ["diff", "--", path], timeout=30.0, check=False).stdout
    staged = git_run(root, ["diff", "--cached", "--", path], timeout=30.0, check=False).stdout
    unstaged = git_run(root, ["diff", "--", path], timeout=30.0, check=False).stdout
    return join_diffs(staged, unstaged)


def combined_diff(root: Path, *, scope: str = "all") -> str:
    if scope == "staged":
        return git_run(root, ["diff", "--cached"], timeout=30.0, check=False).stdout
    if scope == "unstaged":
        return git_run(root, ["diff"], timeout=30.0, check=False).stdout
    tracked = git_run(root, ["diff", "HEAD"], timeout=30.0, check=False).stdout
    untracked = "\n".join(synthetic_untracked_diff(root / item.path, item.path) for item in status_files(root) if item.untracked)
    return join_diffs(tracked, untracked)


def synthetic_untracked_diff(path: Path, display_path: str) -> str:
    if not path.is_file():
        return f"Untracked path: {display_path}\n"
    try:
        data = path.read_bytes()
    except OSError as exc:
        return f"Could not read untracked path {display_path}: {exc}\n"
    if b"\0" in data[:8192]:
        return f"Binary untracked file: {display_path}\n"
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    header = [
        f"diff --git a/{display_path} b/{display_path}",
        "new file mode 100644",
        "index 0000000..0000000",
        "--- /dev/null",
        f"+++ b/{display_path}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    return "\n".join([*header, *(f"+{line}" for line in lines)]) + "\n"


def join_diffs(*parts: str) -> str:
    return "\n".join(part.strip("\n") for part in parts if part and part.strip()).strip() + ("\n" if any(part and part.strip() for part in parts) else "")


def parse_hunks(diff_text: str) -> list[DiffHunk]:
    lines = diff_text.splitlines(keepends=True)
    header: list[str] = []
    hunks: list[DiffHunk] = []
    current: list[str] = []
    current_header = ""
    old_start = 0
    new_start = 0

    for line in lines:
        if line.startswith("@@"):
            if current:
                hunks.append(
                    DiffHunk(
                        index=len(hunks),
                        header=current_header.strip(),
                        patch="".join([*header, *current]),
                        old_start=old_start,
                        new_start=new_start,
                    )
                )
            current = [line]
            current_header = line
            old_start, new_start = hunk_starts(line)
            continue
        if current:
            current.append(line)
        else:
            header.append(line)

    if current:
        hunks.append(
            DiffHunk(
                index=len(hunks),
                header=current_header.strip(),
                patch="".join([*header, *current]),
                old_start=old_start,
                new_start=new_start,
            )
        )
    return hunks


def hunk_starts(header: str) -> tuple[int, int]:
    match = re.search(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", header)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def stage_file(root: Path, path: str) -> str:
    git_run(root, ["add", "--", path])
    return f"Staged {path}."


def unstage_file(root: Path, path: str) -> str:
    git_run(root, ["restore", "--staged", "--", path])
    return f"Unstaged {path}."


def revert_file(root: Path, path: str) -> str:
    file_status = next((item for item in status_files(root) if item.path == path), None)
    if file_status and file_status.untracked:
        target = (root / path).resolve()
        if not is_within(root, target):
            raise GitReviewError("Refusing to remove a path outside the worktree.")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        return f"Removed untracked {path}."
    git_run(root, ["restore", "--staged", "--worktree", "--", path])
    return f"Reverted {path}."


def stage_all(root: Path) -> str:
    git_run(root, ["add", "-A"])
    return "Staged all changes."


def unstage_all(root: Path) -> str:
    git_run(root, ["restore", "--staged", ":/"])
    return "Unstaged all changes."


def stage_hunk(root: Path, path: str, hunk_index: int) -> str:
    diff_text = diff_for_file(root, path, scope="unstaged")
    hunks = parse_hunks(diff_text)
    if not 0 <= hunk_index < len(hunks):
        raise GitReviewError("Select a valid unstaged hunk first.")
    git_run(root, ["apply", "--cached", "--recount", "-"], input_text=hunks[hunk_index].patch)
    return f"Staged hunk {hunk_index + 1} from {path}."


def revert_hunk(root: Path, path: str, hunk_index: int) -> str:
    diff_text = diff_for_file(root, path, scope="unstaged")
    hunks = parse_hunks(diff_text)
    if not 0 <= hunk_index < len(hunks):
        raise GitReviewError("Select a valid unstaged hunk first.")
    git_run(root, ["apply", "--reverse", "--recount", "-"], input_text=hunks[hunk_index].patch)
    return f"Reverted hunk {hunk_index + 1} from {path}."


def unstage_hunk(root: Path, path: str, hunk_index: int) -> str:
    diff_text = diff_for_file(root, path, scope="staged")
    hunks = parse_hunks(diff_text)
    if not 0 <= hunk_index < len(hunks):
        raise GitReviewError("Select a valid staged hunk first.")
    git_run(root, ["apply", "--cached", "--reverse", "--recount", "-"], input_text=hunks[hunk_index].patch)
    return f"Unstaged hunk {hunk_index + 1} from {path}."


def create_branch(root: Path, branch: str) -> str:
    branch = clean_branch_name(branch)
    git_run(root, ["switch", "-c", branch], timeout=120.0)
    return branch


def clean_branch_name(branch: str) -> str:
    value = " ".join(str(branch or "").split())
    if not value:
        raise GitReviewError("Branch name is required.")
    if value.startswith("-") or ".." in value or any(part in value for part in (" ", "~", "^", ":", "?", "*", "[", "\\")):
        raise GitReviewError("Branch name contains unsupported characters.")
    return value


def commit_staged(root: Path, message: str) -> str:
    message = str(message or "").strip()
    if not message:
        raise GitReviewError("Commit message is required.")
    if not git_run(root, ["diff", "--cached", "--quiet"], check=False).returncode:
        raise GitReviewError("Stage at least one change before committing.")
    git_run(root, ["commit", "-m", message], timeout=120.0)
    return git_stdout(root, ["rev-parse", "--short", "HEAD"], timeout=10.0)


def current_branch(root: Path) -> str:
    return git_stdout(root, ["branch", "--show-current"], timeout=10.0)


def push_branch(root: Path, remote: str = "origin") -> str:
    branch = current_branch(root)
    if not branch:
        raise GitReviewError("Create a branch before pushing this worktree task.")
    git_run(root, ["push", "-u", remote, "HEAD"], timeout=180.0)
    return f"Pushed {branch} to {remote}."


def github_compare_url(root: Path, remote: str = "origin", base_branch: str = "main") -> str:
    branch = current_branch(root)
    if not branch:
        raise GitReviewError("Create a branch before opening a pull request.")
    remote_url = git_stdout(root, ["remote", "get-url", remote], timeout=10.0)
    match = re.match(r"git@github\.com:([^/]+)/(.+?)(?:\.git)?$", remote_url)
    if not match:
        match = re.match(r"ssh://git@github\.com/([^/]+)/(.+?)(?:\.git)?$", remote_url)
    if not match:
        match = re.match(r"https://github\.com/([^/]+)/(.+?)(?:\.git)?$", remote_url)
    if not match:
        raise GitReviewError("This remote is not a recognized GitHub remote.")
    owner, repo = match.group(1), match.group(2).rstrip("/")
    return f"https://github.com/{owner}/{repo}/compare/{base_branch}...{branch}?expand=1"


def open_pull_request(root: Path, remote: str = "origin", base_branch: str = "main") -> str:
    branch = current_branch(root)
    if not branch:
        raise GitReviewError("Create a branch before opening a pull request.")
    gh = shutil.which("gh")
    if gh:
        result = subprocess.run(
            [gh, "pr", "create", "--fill", "--web", "--base", base_branch],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return clean_output(result.stdout or result.stderr) or "Opened GitHub pull request flow."
    return github_compare_url(root, remote=remote, base_branch=base_branch)


def handoff_staged_to_local(worktree: Path, local_root: Path, message: str) -> str:
    local_root = local_root.expanduser().resolve()
    worktree = worktree.expanduser().resolve()
    if not git_run(local_root, ["status", "--porcelain"], timeout=20.0).stdout.strip() == "":
        raise GitReviewError("Local checkout has uncommitted changes. Commit, stash, or clear them before handoff.")
    if not git_run(worktree, ["diff", "--cached", "--quiet"], check=False).returncode:
        raise GitReviewError("Stage the changes to hand off first.")
    backup_patch(worktree, local_root)
    commit_sha = commit_staged(worktree, message or "Handoff worktree task")
    git_run(local_root, ["cherry-pick", commit_sha], timeout=180.0)
    return commit_sha


def backup_patch(worktree: Path, local_root: Path) -> Path:
    target = local_root / ".git" / f"{ACTIVE_SESSION_DIR}-review-backups"
    target.mkdir(parents=True, exist_ok=True)
    patch = git_run(worktree, ["diff", "--cached", "--binary"], timeout=30.0, check=False).stdout
    path = target / (time.strftime("handoff-%Y%m%d-%H%M%S") + ".patch")
    path.write_text(patch, encoding="utf-8")
    return path


def is_within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root.resolve())
        return True
    except ValueError:
        return False
