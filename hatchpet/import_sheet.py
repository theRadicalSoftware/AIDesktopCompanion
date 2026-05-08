from __future__ import annotations

import json
import shutil
from pathlib import Path

from PIL import Image

from .hatch import crop_to_visible, despill_chroma_key, fit_to_cell, remove_chroma_key
from .pet_format import CELL_HEIGHT, CELL_WIDTH, FRAME_COUNT, pet_paths


ROW_SPECS = [
    ("running-right", [0, 1, 2, 3, 0, 1, 2, 3]),
    ("running-left", [0, 1, 2, 3, 0, 1, 2, 3]),
    ("happy", [0, 1, 2, 3, 2, 1, 0, 1]),
    ("sitting", [0, 1, 2, 3, 2, 1, 0, 1]),
]

ROW_FILLS = {
    "happy": 0.87,
}

IDLE_PICKUP_ROW_SPECS = [
    ("idle", [0, 1, 2, 3, 2, 1, 0, 1]),
    ("picked-up", [0, 1, 2, 3, 2, 1, 0, 1]),
]

ACTION_ROW_SPECS = [
    ("jumping", [0, 1, 2, 3, 2, 1, 0, 1]),
    ("waving", [0, 1, 2, 3, 2, 1, 0, 1]),
    ("resting", [0, 1, 2, 3, 2, 1, 0, 1]),
]

TARGET_VISIBLE_HEIGHT = 198


def _load_manifest(pet_dir: Path) -> dict:
    manifest_path = pet_dir / "pet.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _save_manifest(pet_dir: Path, manifest: dict) -> None:
    (pet_dir / "pet.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _extract_contact_cell(
    sheet: Image.Image,
    *,
    row: int,
    col: int,
    rows: int,
    cols: int,
    key_color: tuple[int, int, int],
    fill: float,
    target_height: int | None,
    anchor_y: str = "bottom",
    transparent_threshold: int = 62,
    opaque_threshold: int = 225,
    fringe_threshold: int = 150,
) -> Image.Image:
    cell_width = sheet.width // cols
    cell_height = sheet.height // rows
    raw = sheet.crop((col * cell_width, row * cell_height, (col + 1) * cell_width, (row + 1) * cell_height))
    transparent = remove_chroma_key(
        raw,
        key_color,
        transparent_threshold=transparent_threshold,
        opaque_threshold=opaque_threshold,
    )
    transparent = despill_chroma_key(transparent, key_color, fringe_threshold=fringe_threshold)
    if target_height:
        return fit_to_target_height(transparent, target_height, anchor_y=anchor_y)
    return fit_to_cell(transparent, fill=fill)


def fit_to_target_height(image: Image.Image, target_height: int, *, anchor_y: str = "bottom") -> Image.Image:
    cropped = crop_to_visible(image, pad=0)
    scale = target_height / cropped.height
    width = max(1, round(cropped.width * scale))
    height = target_height
    sprite = cropped.resize((width, height), Image.Resampling.LANCZOS)
    if sprite.width > CELL_WIDTH:
        overflow = sprite.width - CELL_WIDTH
        left = overflow // 2
        sprite = sprite.crop((left, 0, left + CELL_WIDTH, sprite.height))

    cell = Image.new("RGBA", (CELL_WIDTH, CELL_HEIGHT), (0, 0, 0, 0))
    x = (CELL_WIDTH - sprite.width) // 2
    if anchor_y == "top":
        y = 0
    elif anchor_y == "center":
        y = (CELL_HEIGHT - sprite.height) // 2
    else:
        y = CELL_HEIGHT - sprite.height - 8
    cell.alpha_composite(sprite, (x, y))
    return cell


def import_pose_sheet(
    pet_dir: Path,
    pose_sheet_path: Path,
    *,
    key: tuple[int, int, int] = (255, 0, 255),
    rows: int = 4,
    cols: int = 4,
    backup: bool = True,
) -> Path:
    return import_contact_sheet(
        pet_dir,
        pose_sheet_path,
        row_specs=ROW_SPECS,
        key=key,
        rows=rows,
        cols=cols,
        backup=backup,
        target_height=TARGET_VISIBLE_HEIGHT,
        manifest_sheet_key="generatedPoseSheet",
    )


def import_idle_pickup_sheet(
    pet_dir: Path,
    pose_sheet_path: Path,
    *,
    key: tuple[int, int, int] = (255, 0, 255),
    rows: int = 2,
    cols: int = 4,
    backup: bool = True,
) -> Path:
    return import_contact_sheet(
        pet_dir,
        pose_sheet_path,
        row_specs=IDLE_PICKUP_ROW_SPECS,
        key=key,
        rows=rows,
        cols=cols,
        backup=backup,
        target_height=TARGET_VISIBLE_HEIGHT,
        manifest_sheet_key="generatedIdlePickupSheet",
    )


def import_action_sheet(
    pet_dir: Path,
    pose_sheet_path: Path,
    *,
    key: tuple[int, int, int] = (255, 0, 255),
    rows: int = 3,
    cols: int = 4,
    backup: bool = True,
) -> Path:
    return import_contact_sheet(
        pet_dir,
        pose_sheet_path,
        row_specs=ACTION_ROW_SPECS,
        key=key,
        rows=rows,
        cols=cols,
        backup=backup,
        target_height=TARGET_VISIBLE_HEIGHT,
        manifest_sheet_key="generatedActionSheet",
    )


def import_named_row_sheet(
    pet_dir: Path,
    pose_sheet_path: Path,
    *,
    animation_name: str,
    target_row: int,
    fps: float,
    key: tuple[int, int, int] = (255, 0, 255),
    rows: int = 1,
    cols: int = 8,
    source_row: int = 0,
    frame_count: int = 8,
    backup: bool = True,
    target_height: int | None = TARGET_VISIBLE_HEIGHT,
    transparent_threshold: int = 62,
    opaque_threshold: int = 225,
    segment_components: bool = False,
    anchor_y: str = "bottom",
) -> Path:
    manifest = _load_manifest(pet_dir)
    sheet_path = pet_dir / manifest["sprite"]
    if backup and sheet_path.exists():
        backup_path = pet_dir / f"{sheet_path.stem}.before-{animation_name}-import{sheet_path.suffix}"
        shutil.copy2(sheet_path, backup_path)

    atlas = Image.open(sheet_path).convert("RGBA")
    required_height = (target_row + 1) * CELL_HEIGHT
    if atlas.height < required_height:
        expanded = Image.new("RGBA", (atlas.width, required_height), (0, 0, 0, 0))
        expanded.alpha_composite(atlas, (0, 0))
        atlas = expanded

    contact = Image.open(pose_sheet_path).convert("RGBA")
    atlas.paste(
        Image.new("RGBA", (CELL_WIDTH * FRAME_COUNT, CELL_HEIGHT), (0, 0, 0, 0)),
        (0, target_row * CELL_HEIGHT),
    )
    if segment_components:
        frames = extract_component_frames(
            contact,
            key=key,
            frame_count=frame_count,
            target_height=target_height,
            transparent_threshold=transparent_threshold,
            opaque_threshold=opaque_threshold,
            anchor_y=anchor_y,
        )
    else:
        frames = []
        for target_col in range(min(frame_count, FRAME_COUNT)):
            source_col = min(target_col, cols - 1)
            frames.append(
                _extract_contact_cell(
                    contact,
                    row=source_row,
                    col=source_col,
                    rows=rows,
                    cols=cols,
                    key_color=key,
                    fill=0.94,
                    target_height=target_height,
                    anchor_y=anchor_y,
                    transparent_threshold=transparent_threshold,
                    opaque_threshold=opaque_threshold,
                    fringe_threshold=max(150, transparent_threshold + 40),
                )
            )

    for target_col, frame in enumerate(frames[:FRAME_COUNT]):
        atlas.alpha_composite(frame, (target_col * CELL_WIDTH, target_row * CELL_HEIGHT))

    atlas.save(sheet_path)
    manifest.setdefault("animations", {})[animation_name] = {
        "row": target_row,
        "frames": frame_count,
        "fps": fps,
    }
    manifest.setdefault("hatch", {}).setdefault("workDrop", {})[animation_name] = {
        "source": str(pose_sheet_path),
        "row": target_row,
        "frames": frame_count,
    }
    _save_manifest(pet_dir, manifest)
    return sheet_path


def extract_component_frames(
    image: Image.Image,
    *,
    key: tuple[int, int, int],
    frame_count: int,
    target_height: int | None,
    transparent_threshold: int,
    opaque_threshold: int,
    anchor_y: str = "bottom",
) -> list[Image.Image]:
    transparent = remove_chroma_key(
        image,
        key,
        transparent_threshold=transparent_threshold,
        opaque_threshold=opaque_threshold,
    )
    transparent = despill_chroma_key(
        transparent,
        key,
        fringe_threshold=max(150, transparent_threshold + 40),
    )
    components = connected_alpha_components(transparent, min_alpha=24, min_area=500)
    largest = sorted(components, key=lambda item: item[0], reverse=True)[:frame_count]
    if len(largest) < frame_count:
        raise ValueError(f"Expected {frame_count} visible components, found {len(largest)}")

    frames = []
    for _area, left, top, right, bottom in sorted(largest, key=lambda item: item[1]):
        crop = transparent.crop((left, top, right, bottom))
        if target_height:
            frames.append(fit_to_target_height(crop, target_height, anchor_y=anchor_y))
        else:
            frames.append(fit_to_cell(crop, fill=0.94))
    return frames


def connected_alpha_components(
    image: Image.Image,
    *,
    min_alpha: int,
    min_area: int,
) -> list[tuple[int, int, int, int, int]]:
    alpha = image.getchannel("A")
    width, height = alpha.size
    pixels = alpha.load()
    seen: set[tuple[int, int]] = set()
    components: list[tuple[int, int, int, int, int]] = []

    for y in range(height):
        for x in range(width):
            if (x, y) in seen or pixels[x, y] <= min_alpha:
                continue
            stack = [(x, y)]
            seen.add((x, y))
            left = right = x
            top = bottom = y
            area = 0
            while stack:
                current_x, current_y = stack.pop()
                area += 1
                left = min(left, current_x)
                right = max(right, current_x)
                top = min(top, current_y)
                bottom = max(bottom, current_y)
                for next_x, next_y in (
                    (current_x + 1, current_y),
                    (current_x - 1, current_y),
                    (current_x, current_y + 1),
                    (current_x, current_y - 1),
                ):
                    if (
                        0 <= next_x < width
                        and 0 <= next_y < height
                        and (next_x, next_y) not in seen
                        and pixels[next_x, next_y] > min_alpha
                    ):
                        seen.add((next_x, next_y))
                        stack.append((next_x, next_y))
            if area >= min_area:
                components.append((area, left, top, right + 1, bottom + 1))
    return components


def import_contact_sheet(
    pet_dir: Path,
    pose_sheet_path: Path,
    *,
    row_specs: list[tuple[str, list[int]]],
    key: tuple[int, int, int],
    rows: int,
    cols: int,
    backup: bool,
    target_height: int | None,
    manifest_sheet_key: str,
) -> Path:
    manifest = _load_manifest(pet_dir)
    sheet_path = pet_dir / manifest["sprite"]
    if backup and sheet_path.exists():
        backup_path = pet_dir / f"{sheet_path.stem}.before-generated-import{sheet_path.suffix}"
        shutil.copy2(sheet_path, backup_path)

    atlas = Image.open(sheet_path).convert("RGBA")
    contact = Image.open(pose_sheet_path).convert("RGBA")

    for source_row, (animation_name, sequence) in enumerate(row_specs):
        if animation_name not in manifest["animations"]:
            continue
        target_row = int(manifest["animations"][animation_name]["row"])
        atlas.paste(
            Image.new("RGBA", (CELL_WIDTH * FRAME_COUNT, CELL_HEIGHT), (0, 0, 0, 0)),
            (0, target_row * CELL_HEIGHT),
        )
        for target_col, source_col in enumerate(sequence):
            frame = _extract_contact_cell(
                contact,
                row=source_row,
                col=source_col,
                rows=rows,
                cols=cols,
                key_color=key,
                fill=ROW_FILLS.get(animation_name, 0.94),
                target_height=target_height,
            )
            atlas.alpha_composite(frame, (target_col * CELL_WIDTH, target_row * CELL_HEIGHT))

    atlas.save(sheet_path)
    normalize_pet_atlas(pet_dir, target_height=TARGET_VISIBLE_HEIGHT)
    shutil.copy2(pose_sheet_path, pet_dir / f"{manifest_sheet_key}.png")

    manifest.setdefault("hatch", {})[manifest_sheet_key] = str(pose_sheet_path)
    manifest["hatch"][f"{manifest_sheet_key}Rows"] = [name for name, _sequence in row_specs]
    _save_manifest(pet_dir, manifest)
    return sheet_path


def normalize_pet_atlas(pet_dir: Path, *, target_height: int = TARGET_VISIBLE_HEIGHT) -> Path:
    manifest = _load_manifest(pet_dir)
    sheet_path = pet_dir / manifest["sprite"]
    atlas = Image.open(sheet_path).convert("RGBA")
    normalized = Image.new("RGBA", atlas.size, (0, 0, 0, 0))

    for animation in manifest["animations"].values():
        row = int(animation["row"])
        for col in range(int(manifest["frameCount"])):
            x0 = col * CELL_WIDTH
            y0 = row * CELL_HEIGHT
            frame = atlas.crop((x0, y0, x0 + CELL_WIDTH, y0 + CELL_HEIGHT))
            if frame.getchannel("A").getbbox():
                frame = fit_to_target_height(frame, target_height)
            normalized.alpha_composite(frame, (x0, y0))

    normalized.save(sheet_path)
    return sheet_path
