from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from .hatch import slugify
from .pet_format import CELL_HEIGHT, CELL_WIDTH, FRAME_COUNT


OFFICIAL_ROWS = [
    ("idle", 6),
    ("running-right", 8),
    ("running-left", 8),
    ("waving", 4),
    ("jumping", 5),
    ("failed", 8),
    ("waiting", 6),
    ("running", 6),
    ("review", 6),
]

OFFICIAL_ROW_COUNT = len(OFFICIAL_ROWS)
OFFICIAL_WIDTH = CELL_WIDTH * FRAME_COUNT
OFFICIAL_HEIGHT = CELL_HEIGHT * OFFICIAL_ROW_COUNT


@dataclass(frozen=True)
class CodexPetCompatibility:
    compatible: bool
    format_name: str
    pet_id: str
    display_name: str
    sprite_path: Path | None
    issues: tuple[str, ...]
    warnings: tuple[str, ...]


def codex_home_path(value: str | Path | None = None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def official_pet_package_dir(pet: str | Path, *, codex_home: str | Path | None = None) -> Path:
    candidate = Path(pet).expanduser()
    if candidate.exists():
        if candidate.is_file() and candidate.name == "pet.json":
            return candidate.parent.resolve()
        return candidate.resolve()
    return codex_home_path(codex_home) / "pets" / str(pet)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object.")
    return data


def official_manifest_path(package_dir: Path) -> Path:
    return package_dir / "pet.json"


def is_official_codex_manifest(manifest: dict[str, Any]) -> bool:
    return "spritesheetPath" in manifest and ("displayName" in manifest or "id" in manifest)


def official_sprite_path(package_dir: Path, manifest: dict[str, Any]) -> Path:
    raw = str(manifest.get("spritesheetPath") or "spritesheet.webp")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = package_dir / path
    return path


def rich_sprite_path(pet_dir: Path, manifest: dict[str, Any]) -> Path:
    raw = str(manifest.get("sprite") or "spritesheet.png")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = pet_dir / path
    return path


def rich_display_name(manifest: dict[str, Any], fallback: str) -> str:
    return str(manifest.get("name") or manifest.get("displayName") or fallback).strip() or fallback


def official_display_name(manifest: dict[str, Any], fallback: str) -> str:
    return str(manifest.get("displayName") or manifest.get("name") or fallback).strip() or fallback


def alpha_rect_is_empty(image: Image.Image, box: tuple[int, int, int, int]) -> bool:
    alpha = image.crop(box).getchannel("A")
    return alpha.getbbox() is None


def unused_cell_warnings(image: Image.Image, *, strict: bool) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    warnings: list[str] = []
    for row_index, (row_name, used_columns) in enumerate(OFFICIAL_ROWS):
        for column in range(used_columns, FRAME_COUNT):
            box = (
                column * CELL_WIDTH,
                row_index * CELL_HEIGHT,
                (column + 1) * CELL_WIDTH,
                (row_index + 1) * CELL_HEIGHT,
            )
            if alpha_rect_is_empty(image, box):
                continue
            message = f"{row_name} row column {column} is not transparent."
            if strict:
                issues.append(message)
            else:
                warnings.append(message + " Export will clear it.")
    return issues, warnings


def analyze_rich_pet(pet_dir: Path) -> CodexPetCompatibility:
    manifest_path = pet_dir / "pet.json"
    manifest = load_json(manifest_path)
    pet_id = str(manifest.get("id") or pet_dir.name).strip() or pet_dir.name
    display_name = rich_display_name(manifest, pet_id)
    sprite = rich_sprite_path(pet_dir, manifest)
    issues: list[str] = []
    warnings: list[str] = []

    cell_width = int(manifest.get("cellWidth") or 0)
    cell_height = int(manifest.get("cellHeight") or 0)
    frame_count = int(manifest.get("frameCount") or 0)
    if cell_width != CELL_WIDTH:
        issues.append(f"cellWidth is {cell_width}, expected {CELL_WIDTH}.")
    if cell_height != CELL_HEIGHT:
        issues.append(f"cellHeight is {cell_height}, expected {CELL_HEIGHT}.")
    if frame_count != FRAME_COUNT:
        issues.append(f"frameCount is {frame_count}, expected {FRAME_COUNT}.")
    if not sprite.is_file():
        issues.append(f"sprite file is missing: {sprite}")
        return CodexPetCompatibility(False, "rich", pet_id, display_name, sprite, tuple(issues), tuple(warnings))

    try:
        with Image.open(sprite) as opened:
            image = opened.convert("RGBA")
    except OSError as exc:
        issues.append(f"sprite could not be opened: {exc}")
        return CodexPetCompatibility(False, "rich", pet_id, display_name, sprite, tuple(issues), tuple(warnings))

    if image.width < OFFICIAL_WIDTH:
        issues.append(f"sprite width is {image.width}, expected at least {OFFICIAL_WIDTH}.")
    elif image.width > OFFICIAL_WIDTH:
        warnings.append(f"sprite width is {image.width}; export will crop to {OFFICIAL_WIDTH}.")
    if image.height < OFFICIAL_HEIGHT:
        issues.append(f"sprite height is {image.height}, expected at least {OFFICIAL_HEIGHT}.")
    elif image.height > OFFICIAL_HEIGHT:
        warnings.append("native runtime has extra rows; official export will keep only the first 9 rows.")

    animations = manifest.get("animations")
    if isinstance(animations, dict):
        for row_index, (name, _used_columns) in enumerate(OFFICIAL_ROWS):
            record = animations.get(name)
            if not isinstance(record, dict):
                warnings.append(f"animation {name!r} is missing from the rich manifest.")
                continue
            if int(record.get("row", -1)) != row_index:
                warnings.append(f"animation {name!r} is row {record.get('row')}, expected row {row_index}.")
    else:
        warnings.append("rich manifest has no animations object; export will rely on physical first 9 rows.")

    if image.width >= OFFICIAL_WIDTH and image.height >= OFFICIAL_HEIGHT:
        cropped = image.crop((0, 0, OFFICIAL_WIDTH, OFFICIAL_HEIGHT))
        _issues, unused_warnings = unused_cell_warnings(cropped, strict=False)
        warnings.extend(unused_warnings)

    return CodexPetCompatibility(not issues, "rich", pet_id, display_name, sprite, tuple(issues), tuple(warnings))


def analyze_official_pet(package_dir: Path) -> CodexPetCompatibility:
    manifest_path = official_manifest_path(package_dir)
    issues: list[str] = []
    warnings: list[str] = []
    if not manifest_path.is_file():
        return CodexPetCompatibility(
            False,
            "official",
            package_dir.name,
            package_dir.name,
            None,
            (f"official pet.json is missing: {manifest_path}",),
            (),
        )
    manifest = load_json(manifest_path)
    pet_id = str(manifest.get("id") or package_dir.name).strip() or package_dir.name
    display_name = official_display_name(manifest, pet_id)
    if not is_official_codex_manifest(manifest):
        issues.append("pet.json does not match the minimal Codex pet manifest shape.")
    sprite = official_sprite_path(package_dir, manifest)
    if not sprite.is_file():
        issues.append(f"spritesheet is missing: {sprite}")
        return CodexPetCompatibility(False, "official", pet_id, display_name, sprite, tuple(issues), tuple(warnings))

    try:
        with Image.open(sprite) as opened:
            image = opened.convert("RGBA")
    except OSError as exc:
        issues.append(f"spritesheet could not be opened: {exc}")
        return CodexPetCompatibility(False, "official", pet_id, display_name, sprite, tuple(issues), tuple(warnings))

    if (image.width, image.height) != (OFFICIAL_WIDTH, OFFICIAL_HEIGHT):
        issues.append(f"spritesheet is {image.width}x{image.height}, expected {OFFICIAL_WIDTH}x{OFFICIAL_HEIGHT}.")
    if (image.width, image.height) == (OFFICIAL_WIDTH, OFFICIAL_HEIGHT):
        unused_issues, _warnings = unused_cell_warnings(image, strict=True)
        issues.extend(unused_issues)
    return CodexPetCompatibility(not issues, "official", pet_id, display_name, sprite, tuple(issues), tuple(warnings))


def analyze_pet_path(path: Path) -> CodexPetCompatibility:
    manifest = load_json(path / "pet.json")
    if is_official_codex_manifest(manifest):
        return analyze_official_pet(path)
    return analyze_rich_pet(path)


def default_codex_description(display_name: str, manifest: dict[str, Any]) -> str:
    existing = str(manifest.get("description") or "").strip()
    if existing:
        return existing
    return f"{display_name} desktop pet."


def clear_official_unused_cells(image: Image.Image) -> Image.Image:
    output = image.copy()
    transparent = Image.new("RGBA", (CELL_WIDTH, CELL_HEIGHT), (0, 0, 0, 0))
    for row_index, (_name, used_columns) in enumerate(OFFICIAL_ROWS):
        for column in range(used_columns, FRAME_COUNT):
            output.paste(transparent, (column * CELL_WIDTH, row_index * CELL_HEIGHT))
    return output


def export_codex_pet(
    pet_dir: Path,
    *,
    output_dir: Path | None = None,
    codex_home: str | Path | None = None,
    pet_id: str | None = None,
    display_name: str | None = None,
    description: str | None = None,
    overwrite: bool = False,
) -> Path:
    manifest = load_json(pet_dir / "pet.json")
    source_id = str(manifest.get("id") or pet_dir.name).strip() or pet_dir.name
    export_id = slugify(pet_id or source_id)
    if not export_id:
        raise ValueError("Could not determine an official Codex pet id.")
    export_name = str(display_name or rich_display_name(manifest, export_id)).strip() or export_id
    export_description = str(description or default_codex_description(export_name, manifest)).strip()
    target_dir = output_dir.expanduser().resolve() if output_dir else codex_home_path(codex_home) / "pets" / export_id
    if target_dir.exists() and any(target_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"{target_dir} already exists. Use --overwrite to replace it.")
    target_dir.mkdir(parents=True, exist_ok=True)

    sprite = rich_sprite_path(pet_dir, manifest)
    compatibility = analyze_rich_pet(pet_dir)
    if compatibility.issues:
        raise ValueError("Pet is not exportable as a Codex app pet: " + "; ".join(compatibility.issues))

    with Image.open(sprite) as opened:
        image = opened.convert("RGBA")
    cropped = image.crop((0, 0, OFFICIAL_WIDTH, OFFICIAL_HEIGHT))
    official = clear_official_unused_cells(cropped)
    official.save(target_dir / "spritesheet.webp", format="WEBP", lossless=True, quality=100, method=6)

    payload = {
        "id": export_id,
        "displayName": export_name,
        "description": export_description,
        "spritesheetPath": "spritesheet.webp",
    }
    (target_dir / "pet.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target_dir


def default_rich_animations() -> dict[str, dict[str, Any]]:
    rows = {
        "idle": {"row": 0, "frames": 6, "fps": 4},
        "running-right": {"row": 1, "frames": 8, "fps": 5},
        "running-left": {"row": 2, "frames": 8, "fps": 5},
        "waving": {"row": 3, "frames": 4, "fps": 5},
        "jumping": {"row": 4, "frames": 5, "fps": 7, "loop": False},
        "failed": {"row": 5, "frames": 8, "fps": 4, "loop": False},
        "waiting": {"row": 6, "frames": 6, "fps": 4},
        "running": {"row": 7, "frames": 6, "fps": 5},
        "review": {"row": 8, "frames": 6, "fps": 3},
    }
    rows.update(
        {
            "happy": dict(rows["waiting"]),
            "sitting": dict(rows["idle"]),
            "resting": dict(rows["idle"]),
            "work-laptop": dict(rows["review"]),
            "picked-up": dict(rows["idle"]),
            "falling": {"row": 4, "frames": 5, "fps": 7, "loop": False},
            "landing-soft": {"row": 4, "frames": 5, "fps": 7, "loop": False},
            "landing-hard": dict(rows["failed"]),
            "codex-attention": dict(rows["waiting"]),
            "prompting": dict(rows["review"]),
            "file-receive": dict(rows["waiting"]),
            "file-inspect": dict(rows["review"]),
            "file-review": dict(rows["review"]),
            "phone-reply-start": dict(rows["waving"]),
            "phone-reply": dict(rows["review"]),
            "github-action": dict(rows["review"]),
        }
    )
    return rows


def default_rich_runtime() -> dict[str, Any]:
    return {
        "defaultScale": 1.0,
        "walkSpeed": 0.9,
        "groundPadding": 24,
        "dragAnchor": {"xRatio": 0.5, "yRatio": 0.12},
        "falling": {
            "gravity": 1.2,
            "maxVelocity": 18.0,
            "hardDropHeight": 300,
            "softLandingTicks": 50,
            "hardLandingTicks": 76,
            "landingHoldTicks": 12,
            "postLandingCooldownTicks": 24,
            "gliderEnabled": False,
        },
        "ceilingHold": {"enabled": False},
        "codexBubble": {
            "enabled": True,
            "session": "current",
            "sprite": "assets/thought-bubble/cyber-thought-bubble-spritesheet.png",
            "frames": 18,
            "openFrames": 5,
            "loopFrames": 8,
            "closeFrames": 5,
            "fps": 9,
            "width": 560,
            "height": 300,
            "attentionAnimation": "waiting",
            "approvalBridgeEnabled": True,
            "exitOnTerminalClose": False,
            "minimizedDotsHeadroom": 34,
        },
        "codexUsage": {
            "enabled": True,
            "session": "current",
            "sprite": "assets/usage-meter/cyber-usage-sign-spritesheet.png",
            "baseFrame": "assets/usage-meter/cyber-usage-sign-frame.png",
            "frames": 18,
            "openFrames": 5,
            "loopFrames": 8,
            "closeFrames": 5,
            "fps": 9,
            "openSeconds": 1.2,
            "closeSeconds": 1.15,
            "width": 620,
            "height": 420,
            "activationDelayMs": 520,
            "intervalMinutes": 30,
            "startupDelaySeconds": 90,
            "visibleSeconds": 12,
        },
        "aiProviders": {
            "claude": {
                "enabled": False,
                "model": "claude-sonnet-4-6",
                "maxTokens": 4096,
            },
            "github": {
                "enabled": True,
                "remote": "origin",
                "mainBranch": "main",
                "requireCleanTree": True,
                "actionAnimation": "review",
            },
            "slack": {
                "enabled": False,
                "sendAs": "bot",
                "transcriptEnabled": True,
                "transcriptDir": "runtime-slack",
                "contacts": [],
                "pollEnabled": False,
            },
        },
        "workDrop": {
            "enabled": True,
            "defaultProvider": "codex",
            "defaultSandbox": "read-only",
            "bubbleReplySandbox": "workspace-write",
            "promptAnimation": "review",
            "receiveAnimation": "waiting",
            "inspectAnimation": "review",
            "reviewAnimation": "review",
            "laptopAnimation": "review",
            "replyStartAnimation": "waving",
            "replyAnimation": "review",
            "bubbleReplyEnabled": True,
            "restEnterAnimation": "resting",
            "restAnimation": "resting",
            "outputDir": "runtime-work",
            "statusHoldSeconds": 28,
        },
        "worktreeTasks": {
            "enabled": True,
            "baseRef": "HEAD",
            "companionId": "",
            "terminal": "auto",
            "terminalTitlePrefix": "Codex Worktree",
        },
    }


def import_codex_pet(
    package_dir: Path,
    *,
    pets_dir: Path,
    pet_id: str | None = None,
    display_name: str | None = None,
    overwrite: bool = False,
) -> Path:
    manifest = load_json(package_dir / "pet.json")
    if not is_official_codex_manifest(manifest):
        raise ValueError(f"{package_dir} is not an official Codex pet package.")
    source_id = str(manifest.get("id") or package_dir.name).strip() or package_dir.name
    rich_id = slugify(pet_id or source_id)
    rich_name = str(display_name or official_display_name(manifest, rich_id)).strip() or rich_id
    target_dir = pets_dir.expanduser().resolve() / rich_id
    if target_dir.exists():
        if not overwrite:
            raise FileExistsError(f"{target_dir} already exists. Use --overwrite to replace it.")
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    compatibility = analyze_official_pet(package_dir)
    if compatibility.issues:
        raise ValueError("Official Codex pet package is not importable: " + "; ".join(compatibility.issues))

    sprite = official_sprite_path(package_dir, manifest)
    with Image.open(sprite) as opened:
        image = opened.convert("RGBA")
    image.save(target_dir / "spritesheet.png")
    image.crop((0, 0, CELL_WIDTH, CELL_HEIGHT)).save(target_dir / "source.png")

    rich_manifest = {
        "id": rich_id,
        "name": rich_name,
        "version": 1,
        "cellWidth": CELL_WIDTH,
        "cellHeight": CELL_HEIGHT,
        "frameCount": FRAME_COUNT,
        "sprite": "spritesheet.png",
        "source": "source.png",
        "animations": default_rich_animations(),
        "runtime": default_rich_runtime(),
        "codexApp": {
            "imported": True,
            "sourcePackage": str(package_dir),
            "sourceId": source_id,
        },
    }
    (target_dir / "pet.json").write_text(json.dumps(rich_manifest, indent=2) + "\n", encoding="utf-8")
    return target_dir


def codex_pet_cache_dir(value: str | Path | None = None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser()
    return root / "ai-desktop-companion" / "codex-pets"
