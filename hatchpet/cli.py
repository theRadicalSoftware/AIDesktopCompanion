from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .hatch import hatch_pet, slugify
from .codex_pet_compat import (
    analyze_pet_path,
    codex_pet_cache_dir,
    export_codex_pet,
    import_codex_pet,
    official_pet_package_dir,
)
from .import_sheet import import_action_sheet, import_idle_pickup_sheet, import_named_row_sheet, import_pose_sheet
from .pet_format import DEFAULT_PETS_DIR, pet_paths
from .worktree_tasks import (
    WorktreeTaskError,
    branch_worktree_task,
    create_worktree_task,
    format_task_report,
    get_worktree_task,
    launch_worktree_task_terminal,
    list_worktree_tasks,
    remove_worktree_task,
    summarize_tasks,
    task_status_report,
)


def existing_pet_dir(pets_dir: Path, pet: str) -> Path:
    candidate = Path(pet)
    if candidate.exists() and (candidate / "pet.json").exists():
        return candidate.resolve()
    paths = pet_paths(pets_dir, pet)
    if paths.manifest.exists():
        return paths.root.resolve()
    raise FileNotFoundError(f"Could not find pet {pet!r} in {pets_dir}")


def existing_rich_or_codex_pet_dir(pets_dir: Path, pet: str, *, codex_home: str | None = None) -> Path:
    candidate = Path(pet).expanduser()
    if candidate.exists() and (candidate / "pet.json").exists():
        return candidate.resolve()
    try:
        return existing_pet_dir(pets_dir, pet)
    except FileNotFoundError:
        package = official_pet_package_dir(pet, codex_home=codex_home)
        if (package / "pet.json").is_file():
            return package.resolve()
    raise FileNotFoundError(f"Could not find rich or official Codex pet {pet!r}")


def cmd_hatch(args: argparse.Namespace) -> int:
    image_path = Path(args.image).expanduser().resolve()
    if not image_path.exists():
        raise FileNotFoundError(image_path)

    name = args.name or image_path.stem.replace("_", " ").replace("-", " ").title()
    pet_id = args.pet_id or slugify(name)
    root = hatch_pet(
        image_path,
        pet_id=pet_id,
        name=name,
        pets_dir=Path(args.pets_dir).expanduser().resolve(),
        key=args.key,
        background_tolerance=args.background_tolerance,
        overwrite=args.overwrite,
    )
    print(root)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    pets_dir = Path(args.pets_dir).expanduser().resolve()
    if not pets_dir.exists():
        print(f"No pets directory found at {pets_dir}")
        return 0
    for manifest_path in sorted(pets_dir.glob("*/pet.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        print(f"{manifest['id']}\t{manifest['name']}\t{manifest_path.parent}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .desktop_pet import run_pet

    pet_dir = existing_pet_dir(Path(args.pets_dir).expanduser().resolve(), args.pet)
    print(f"Running pet {pet_dir}", flush=True)
    return run_pet(pet_dir, scale=args.scale, speed=args.speed, codex_session=args.codex_session)


def cmd_check_codex_pet(args: argparse.Namespace) -> int:
    pets_dir = Path(args.pets_dir).expanduser().resolve()
    pet_dir = existing_rich_or_codex_pet_dir(pets_dir, args.pet, codex_home=args.codex_home)
    report = analyze_pet_path(pet_dir)
    if args.json:
        payload = {
            "compatible": report.compatible,
            "format": report.format_name,
            "id": report.pet_id,
            "displayName": report.display_name,
            "spritePath": str(report.sprite_path) if report.sprite_path else "",
            "issues": list(report.issues),
            "warnings": list(report.warnings),
        }
        print(json.dumps(payload, indent=2))
        return 0 if report.compatible else 1

    print(f"Codex app compatible: {'yes' if report.compatible else 'no'}")
    print(f"Format: {report.format_name}")
    print(f"Pet: {report.display_name} ({report.pet_id})")
    if report.sprite_path:
        print(f"Sprite: {report.sprite_path}")
    for issue in report.issues:
        print(f"Issue: {issue}")
    for warning in report.warnings:
        print(f"Warning: {warning}")
    return 0 if report.compatible else 1


def cmd_export_codex_pet(args: argparse.Namespace) -> int:
    pet_dir = existing_pet_dir(Path(args.pets_dir).expanduser().resolve(), args.pet)
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    target = export_codex_pet(
        pet_dir,
        output_dir=output_dir,
        codex_home=args.codex_home,
        pet_id=args.id,
        display_name=args.name,
        description=args.description,
        overwrite=args.overwrite,
    )
    print(target)
    return 0


def cmd_import_codex_pet(args: argparse.Namespace) -> int:
    package = official_pet_package_dir(args.pet, codex_home=args.codex_home)
    target = import_codex_pet(
        package,
        pets_dir=Path(args.pets_dir).expanduser().resolve(),
        pet_id=args.id,
        display_name=args.name,
        overwrite=args.overwrite,
    )
    print(target)
    return 0


def cmd_run_codex_pet(args: argparse.Namespace) -> int:
    from .desktop_pet import run_pet

    package = official_pet_package_dir(args.pet, codex_home=args.codex_home)
    cache_dir = codex_pet_cache_dir(args.cache_dir)
    target = import_codex_pet(
        package,
        pets_dir=cache_dir,
        pet_id=args.id,
        display_name=args.name,
        overwrite=True,
    )
    print(f"Running imported Codex pet {target}", flush=True)
    return run_pet(target, scale=args.scale, speed=args.speed, codex_session=args.codex_session)


def cli_codex_home(value: str | None) -> Path | None:
    return Path(value).expanduser().resolve() if value else None


def cmd_worktree_task_create(args: argparse.Namespace) -> int:
    codex_home = cli_codex_home(args.codex_home)
    task = create_worktree_task(
        cwd=Path(args.cwd).expanduser().resolve(),
        label=args.label or args.prompt,
        prompt=args.prompt,
        base_ref=args.base_ref,
        branch=args.branch,
        owner_id=args.owner_id,
        owner_label=args.owner_label,
        codex_home=codex_home,
        worktrees_root=Path(args.worktrees_dir).expanduser().resolve() if args.worktrees_dir else None,
    )
    if args.open_terminal:
        ok, detail = launch_worktree_task_terminal(
            task,
            owner_id=args.owner_id,
            owner_label=args.owner_label,
            codex_home=codex_home,
            terminal_name=args.terminal,
            title=args.title,
            prompt=args.prompt,
        )
        if not ok:
            raise WorktreeTaskError(detail)
        task = get_worktree_task(task.task_id, codex_home=codex_home)
    if args.json:
        print(json.dumps(task_status_report(task), indent=2))
    else:
        print(f"Created worktree task: {task.task_id}")
        print(f"Worktree: {task.worktree_path}")
        if task.local_dirty_at_create:
            print("Warning: local checkout had uncommitted changes; the worktree starts from committed HEAD.")
    return 0


def cmd_worktree_task_list(args: argparse.Namespace) -> int:
    codex_home = cli_codex_home(args.codex_home)
    tasks = list_worktree_tasks(codex_home=codex_home, include_removed=args.all)
    if args.json:
        print(json.dumps([task_status_report(task) for task in tasks], indent=2))
    else:
        print(summarize_tasks(tasks, limit=args.limit))
    return 0


def cmd_worktree_task_status(args: argparse.Namespace) -> int:
    task = get_worktree_task(args.task_id, codex_home=cli_codex_home(args.codex_home))
    if args.json:
        print(json.dumps(task_status_report(task), indent=2))
    else:
        print(format_task_report(task))
    return 0


def cmd_worktree_task_terminal(args: argparse.Namespace) -> int:
    codex_home = cli_codex_home(args.codex_home)
    task = get_worktree_task(args.task_id, codex_home=codex_home)
    ok, detail = launch_worktree_task_terminal(
        task,
        owner_id=args.owner_id,
        owner_label=args.owner_label,
        codex_home=codex_home,
        terminal_name=args.terminal,
        title=args.title,
        prompt=args.prompt,
    )
    if not ok:
        raise WorktreeTaskError(detail)
    print(detail)
    return 0


def cmd_worktree_task_branch(args: argparse.Namespace) -> int:
    task = branch_worktree_task(args.task_id, args.branch, codex_home=cli_codex_home(args.codex_home))
    print(format_task_report(task))
    return 0


def cmd_worktree_task_remove(args: argparse.Namespace) -> int:
    task = remove_worktree_task(args.task_id, codex_home=cli_codex_home(args.codex_home), force=args.force)
    print(f"Removed worktree task: {task.task_id}")
    return 0


def cmd_import_sheet(args: argparse.Namespace) -> int:
    pet_dir = existing_pet_dir(Path(args.pets_dir).expanduser().resolve(), args.pet)
    sheet_path = Path(args.sheet).expanduser().resolve()
    if not sheet_path.exists():
        raise FileNotFoundError(sheet_path)
    key = tuple(int(args.key.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    output = import_pose_sheet(pet_dir, sheet_path, key=key, rows=args.rows, cols=args.cols, backup=not args.no_backup)
    print(output)
    return 0


def cmd_import_idle_pickup_sheet(args: argparse.Namespace) -> int:
    pet_dir = existing_pet_dir(Path(args.pets_dir).expanduser().resolve(), args.pet)
    sheet_path = Path(args.sheet).expanduser().resolve()
    if not sheet_path.exists():
        raise FileNotFoundError(sheet_path)
    key = tuple(int(args.key.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    output = import_idle_pickup_sheet(pet_dir, sheet_path, key=key, rows=args.rows, cols=args.cols, backup=not args.no_backup)
    print(output)
    return 0


def cmd_import_action_sheet(args: argparse.Namespace) -> int:
    pet_dir = existing_pet_dir(Path(args.pets_dir).expanduser().resolve(), args.pet)
    sheet_path = Path(args.sheet).expanduser().resolve()
    if not sheet_path.exists():
        raise FileNotFoundError(sheet_path)
    key = tuple(int(args.key.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    output = import_action_sheet(pet_dir, sheet_path, key=key, rows=args.rows, cols=args.cols, backup=not args.no_backup)
    print(output)
    return 0


def cmd_import_row_sheet(args: argparse.Namespace) -> int:
    pet_dir = existing_pet_dir(Path(args.pets_dir).expanduser().resolve(), args.pet)
    sheet_path = Path(args.sheet).expanduser().resolve()
    if not sheet_path.exists():
        raise FileNotFoundError(sheet_path)
    key = tuple(int(args.key.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    output = import_named_row_sheet(
        pet_dir,
        sheet_path,
        animation_name=args.animation,
        target_row=args.row,
        fps=args.fps,
        key=key,
        rows=args.rows,
        cols=args.cols,
        source_row=args.source_row,
        frame_count=args.frames,
        backup=not args.no_backup,
        target_height=args.target_height,
        transparent_threshold=args.transparent_threshold,
        opaque_threshold=args.opaque_threshold,
        segment_components=args.segment_components,
        anchor_y=args.anchor_y,
    )
    print(output)
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    checks = []
    checks.append(("DISPLAY", os.environ.get("DISPLAY") or "missing"))
    checks.append(("XDG_SESSION_TYPE", os.environ.get("XDG_SESSION_TYPE") or "unknown"))
    try:
        import PyQt6  # noqa: F401

        checks.append(("PyQt6", "ok"))
    except Exception as exc:  # pragma: no cover
        checks.append(("PyQt6", f"missing: {exc}"))
    try:
        import PIL  # noqa: F401

        checks.append(("Pillow", "ok"))
    except Exception as exc:  # pragma: no cover
        checks.append(("Pillow", f"missing: {exc}"))
    checks.append(("xdotool", shutil.which("xdotool") or "missing"))
    try:
        from .claude_bridge import claude_api_key_available

        checks.append(("Claude API key", "configured" if claude_api_key_available() else "missing"))
    except Exception as exc:  # pragma: no cover
        checks.append(("Claude API key", f"check failed: {exc}"))
    try:
        from .slack_bridge import slack_token_available, slack_user_token_available

        checks.append(("Slack API token", "configured" if slack_token_available() else "missing"))
        checks.append(("Slack user token", "configured" if slack_user_token_available() else "missing (optional)"))
    except Exception as exc:  # pragma: no cover
        checks.append(("Slack API token", f"check failed: {exc}"))

    for name, value in checks:
        print(f"{name}: {value}")
    return 0


def cmd_link_session(args: argparse.Namespace) -> int:
    from .codex_monitor import CodexSessionMonitor, write_active_session_pointer

    codex_home = Path(args.codex_home).expanduser().resolve() if args.codex_home else None
    monitor = CodexSessionMonitor(selector=args.selector, codex_home=codex_home)
    session_path = monitor.resolve_session_path()
    if session_path is None:
        raise FileNotFoundError(f"Could not resolve Codex session selector {args.selector!r}")
    pointer = write_active_session_pointer(
        codex_home=codex_home,
        session_path=session_path,
        selector=args.selector,
        cwd=Path.cwd(),
    )
    print(pointer)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-desktop-companion",
        description="Run and customize a local Linux AI desktop companion.",
    )
    parser.add_argument("--pets-dir", default=str(DEFAULT_PETS_DIR), help="Directory containing pet folders.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    hatch = subparsers.add_parser("hatch", help="Create a pet from a source image.")
    hatch.add_argument("image", help="Source image path.")
    hatch.add_argument("--id", dest="pet_id", help="Pet id/folder name. Defaults to a slug from the name.")
    hatch.add_argument("--name", help="Display name.")
    hatch.add_argument("--key", help="Chroma key color to remove, for example #ff00ff.")
    hatch.add_argument("--background-tolerance", type=int, default=38)
    hatch.add_argument("--overwrite", action="store_true")
    hatch.set_defaults(func=cmd_hatch)

    list_cmd = subparsers.add_parser("list", help="List available pets.")
    list_cmd.set_defaults(func=cmd_list)

    run = subparsers.add_parser("run", help="Run a pet on the desktop.")
    run.add_argument("pet", help="Pet id or path to a pet folder.")
    run.add_argument("--scale", type=float, help="Render scale. 1.0 is the atlas native size.")
    run.add_argument("--speed", type=float, help="Horizontal walking speed in pixels per tick.")
    run.add_argument(
        "--codex-session",
        help=(
            "Show a Codex status thought bubble. Use latest/current, off, a thread id, "
            "or an absolute rollout JSONL path."
        ),
    )
    run.set_defaults(func=cmd_run)

    check_codex = subparsers.add_parser(
        "check-codex-pet",
        help="Report whether a rich or official pet can be used as an official Codex app pet.",
    )
    check_codex.add_argument("pet", help="Rich pet id/path or official Codex pet id/path.")
    check_codex.add_argument("--codex-home", help="Override Codex home for official packages. Defaults to ~/.codex.")
    check_codex.add_argument("--json", action="store_true", help="Print a machine-readable report.")
    check_codex.set_defaults(func=cmd_check_codex_pet)

    export_codex = subparsers.add_parser(
        "export-codex-pet",
        help="Export a rich runtime pet as an official Codex app pet package.",
    )
    export_codex.add_argument("pet", help="Rich pet id or path to a rich pet folder.")
    export_codex.add_argument("--codex-home", help="Override Codex home. Defaults to ~/.codex.")
    export_codex.add_argument("--output-dir", help="Write the official package to this directory instead of CODEX_HOME/pets/<id>.")
    export_codex.add_argument("--id", help="Official Codex pet id. Defaults to the rich pet id.")
    export_codex.add_argument("--name", help="Official displayName. Defaults to the rich pet name.")
    export_codex.add_argument("--description", help="Official description. Defaults to the rich manifest description or a short fallback.")
    export_codex.add_argument("--overwrite", action="store_true", help="Replace files in an existing output directory.")
    export_codex.set_defaults(func=cmd_export_codex_pet)

    import_codex = subparsers.add_parser(
        "import-codex-pet",
        help="Import an official Codex app pet package into the richer runtime pet format.",
    )
    import_codex.add_argument("pet", help="Official Codex pet id, package directory, or package pet.json path.")
    import_codex.add_argument("--codex-home", help="Override Codex home. Defaults to ~/.codex.")
    import_codex.add_argument("--id", help="Rich runtime pet id. Defaults to the official id.")
    import_codex.add_argument("--name", help="Rich runtime display name. Defaults to the official displayName.")
    import_codex.add_argument("--overwrite", action="store_true", help="Replace an existing rich runtime pet folder.")
    import_codex.set_defaults(func=cmd_import_codex_pet)

    run_codex = subparsers.add_parser(
        "run-codex-pet",
        help="Run an official Codex app pet package through the richer desktop runtime.",
    )
    run_codex.add_argument("pet", help="Official Codex pet id, package directory, or package pet.json path.")
    run_codex.add_argument("--codex-home", help="Override Codex home. Defaults to ~/.codex.")
    run_codex.add_argument("--cache-dir", help="Runtime import cache. Defaults to XDG_CACHE_HOME/ai-desktop-companion/codex-pets.")
    run_codex.add_argument("--id", help="Cached runtime pet id. Defaults to the official id.")
    run_codex.add_argument("--name", help="Cached runtime display name. Defaults to the official displayName.")
    run_codex.add_argument("--scale", type=float, help="Render scale. 1.0 is the atlas native size.")
    run_codex.add_argument("--speed", type=float, help="Horizontal walking speed in pixels per tick.")
    run_codex.add_argument("--codex-session", help="Codex status selector for the thought bubble.")
    run_codex.set_defaults(func=cmd_run_codex_pet)

    wt_create = subparsers.add_parser(
        "worktree-task-create",
        help="Create a Git worktree-backed Codex task.",
    )
    wt_create.add_argument("prompt", nargs="?", default="", help="Optional prompt to start in the Codex terminal.")
    wt_create.add_argument("--cwd", default=".", help="Git repository path. Defaults to the current directory.")
    wt_create.add_argument("--label", help="Task label. Defaults to the prompt or current ref.")
    wt_create.add_argument("--base-ref", default="HEAD", help="Git ref to base the worktree on. Defaults to HEAD.")
    wt_create.add_argument("--branch", default="", help="Create the worktree on a new branch instead of detached HEAD.")
    wt_create.add_argument("--owner-id", default="", help="Pet id that owns this task.")
    wt_create.add_argument("--owner-label", default="", help="Display label for the owning pet.")
    wt_create.add_argument("--codex-home", help="Override Codex home. Defaults to ~/.codex.")
    wt_create.add_argument("--worktrees-dir", help="Override the directory used to store generated worktrees.")
    wt_create.add_argument("--open-terminal", action="store_true", help="Launch a Codex terminal scoped to the new worktree.")
    wt_create.add_argument("--terminal", default="auto", help="Terminal emulator to use when opening a terminal.")
    wt_create.add_argument("--title", default="", help="Terminal title when --open-terminal is used.")
    wt_create.add_argument("--json", action="store_true", help="Print a machine-readable task report.")
    wt_create.set_defaults(func=cmd_worktree_task_create)

    wt_list = subparsers.add_parser("worktree-task-list", help="List worktree-backed Codex tasks.")
    wt_list.add_argument("--codex-home", help="Override Codex home. Defaults to ~/.codex.")
    wt_list.add_argument("--all", action="store_true", help="Include removed tasks.")
    wt_list.add_argument("--limit", type=int, default=8, help="Maximum text rows to show in summary mode.")
    wt_list.add_argument("--json", action="store_true", help="Print machine-readable task reports.")
    wt_list.set_defaults(func=cmd_worktree_task_list)

    wt_status = subparsers.add_parser("worktree-task-status", help="Show one worktree task's status and diff summary.")
    wt_status.add_argument("task_id", help="Worktree task id.")
    wt_status.add_argument("--codex-home", help="Override Codex home. Defaults to ~/.codex.")
    wt_status.add_argument("--json", action="store_true", help="Print a machine-readable task report.")
    wt_status.set_defaults(func=cmd_worktree_task_status)

    wt_terminal = subparsers.add_parser("worktree-task-terminal", help="Open a Codex terminal for an existing worktree task.")
    wt_terminal.add_argument("task_id", help="Worktree task id.")
    wt_terminal.add_argument("prompt", nargs="?", default="", help="Optional prompt to start in the Codex terminal.")
    wt_terminal.add_argument("--owner-id", default="", help="Pet id that owns this terminal.")
    wt_terminal.add_argument("--owner-label", default="", help="Display label for the owning pet.")
    wt_terminal.add_argument("--codex-home", help="Override Codex home. Defaults to ~/.codex.")
    wt_terminal.add_argument("--terminal", default="auto", help="Terminal emulator to use.")
    wt_terminal.add_argument("--title", default="", help="Terminal title.")
    wt_terminal.set_defaults(func=cmd_worktree_task_terminal)

    wt_branch = subparsers.add_parser("worktree-task-branch", help="Create a branch in an existing detached task worktree.")
    wt_branch.add_argument("task_id", help="Worktree task id.")
    wt_branch.add_argument("branch", help="New branch name.")
    wt_branch.add_argument("--codex-home", help="Override Codex home. Defaults to ~/.codex.")
    wt_branch.set_defaults(func=cmd_worktree_task_branch)

    wt_remove = subparsers.add_parser("worktree-task-remove", help="Remove a worktree task if it is clean.")
    wt_remove.add_argument("task_id", help="Worktree task id.")
    wt_remove.add_argument("--codex-home", help="Override Codex home. Defaults to ~/.codex.")
    wt_remove.add_argument("--force", action="store_true", help="Force removal even when the worktree has changes.")
    wt_remove.set_defaults(func=cmd_worktree_task_remove)

    import_sheet = subparsers.add_parser("import-sheet", help="Import generated pose-sheet rows into a pet atlas.")
    import_sheet.add_argument("pet", help="Pet id or path to a pet folder.")
    import_sheet.add_argument("sheet", help="4x4 generated pose sheet path.")
    import_sheet.add_argument("--key", default="#ff00ff", help="Chroma key color to remove.")
    import_sheet.add_argument("--rows", type=int, default=4)
    import_sheet.add_argument("--cols", type=int, default=4)
    import_sheet.add_argument("--no-backup", action="store_true")
    import_sheet.set_defaults(func=cmd_import_sheet)

    import_idle_pickup = subparsers.add_parser("import-idle-pickup-sheet", help="Import idle and picked-up generated rows.")
    import_idle_pickup.add_argument("pet", help="Pet id or path to a pet folder.")
    import_idle_pickup.add_argument("sheet", help="2x4 generated idle/picked-up pose sheet path.")
    import_idle_pickup.add_argument("--key", default="#ff00ff", help="Chroma key color to remove.")
    import_idle_pickup.add_argument("--rows", type=int, default=2)
    import_idle_pickup.add_argument("--cols", type=int, default=4)
    import_idle_pickup.add_argument("--no-backup", action="store_true")
    import_idle_pickup.set_defaults(func=cmd_import_idle_pickup_sheet)

    import_action = subparsers.add_parser("import-action-sheet", help="Import jump, wave, and resting generated rows.")
    import_action.add_argument("pet", help="Pet id or path to a pet folder.")
    import_action.add_argument("sheet", help="3x4 generated jump/wave/rest pose sheet path.")
    import_action.add_argument("--key", default="#ff00ff", help="Chroma key color to remove.")
    import_action.add_argument("--rows", type=int, default=3)
    import_action.add_argument("--cols", type=int, default=4)
    import_action.add_argument("--no-backup", action="store_true")
    import_action.set_defaults(func=cmd_import_action_sheet)

    import_row = subparsers.add_parser("import-row-sheet", help="Import one generated row into a named animation row.")
    import_row.add_argument("pet", help="Pet id or path to a pet folder.")
    import_row.add_argument("animation", help="Animation name to create or replace.")
    import_row.add_argument("sheet", help="Generated single-row sheet path.")
    import_row.add_argument("--row", type=int, required=True, help="Target atlas row index.")
    import_row.add_argument("--frames", type=int, default=8)
    import_row.add_argument("--fps", type=float, default=4.0)
    import_row.add_argument("--key", default="#ff00ff", help="Chroma key color to remove.")
    import_row.add_argument("--rows", type=int, default=1)
    import_row.add_argument("--cols", type=int, default=8)
    import_row.add_argument("--source-row", type=int, default=0)
    import_row.add_argument("--target-height", type=int, default=198)
    import_row.add_argument(
        "--anchor-y",
        choices=("bottom", "top", "center"),
        default="bottom",
        help="Vertical placement inside each 192x208 cell after target-height fitting.",
    )
    import_row.add_argument("--transparent-threshold", type=int, default=62)
    import_row.add_argument("--opaque-threshold", type=int, default=225)
    import_row.add_argument(
        "--segment-components",
        action="store_true",
        help="Extract the largest visible components instead of splitting by columns.",
    )
    import_row.add_argument("--no-backup", action="store_true")
    import_row.set_defaults(func=cmd_import_row_sheet)

    doctor = subparsers.add_parser("doctor", help="Check local runtime dependencies.")
    doctor.set_defaults(func=cmd_doctor)

    link_session = subparsers.add_parser(
        "link-session",
        help="Pin the thought bubble to an explicit Codex rollout for CODEX_SESSION=active.",
    )
    link_session.add_argument(
        "selector",
        nargs="?",
        default="current",
        help="current, latest, thread id, or an absolute rollout JSONL path.",
    )
    link_session.add_argument("--codex-home", help="Override the Codex home directory. Defaults to ~/.codex.")
    link_session.set_defaults(func=cmd_link_session)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
