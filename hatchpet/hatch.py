from __future__ import annotations

import json
import math
import re
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps

from .pet_format import (
    ANIMATION_ROWS,
    CELL_HEIGHT,
    CELL_WIDTH,
    DEFAULT_FPS,
    DEFAULT_PETS_DIR,
    FRAME_COUNT,
    pet_paths,
)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug or "pet"


def parse_hex_color(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    text = value.strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6:
        raise ValueError(f"Expected a 6-digit color like #ff00ff, got {value!r}")
    return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))


def remove_chroma_key(
    image: Image.Image,
    key: tuple[int, int, int],
    transparent_threshold: int = 34,
    opaque_threshold: int = 150,
) -> Image.Image:
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size

    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            distance = math.sqrt((r - key[0]) ** 2 + (g - key[1]) ** 2 + (b - key[2]) ** 2)
            if distance <= transparent_threshold:
                pixels[x, y] = (r, g, b, 0)
            elif distance < opaque_threshold:
                alpha = int(255 * ((distance - transparent_threshold) / (opaque_threshold - transparent_threshold)))
                pixels[x, y] = (r, g, b, min(a, max(0, alpha)))

    return rgba


def despill_chroma_key(
    image: Image.Image,
    key: tuple[int, int, int],
    *,
    fringe_threshold: int = 110,
) -> Image.Image:
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    key_r, key_g, key_b = key

    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a == 0:
                continue

            distance = math.sqrt((r - key_r) ** 2 + (g - key_g) ** 2 + (b - key_b) ** 2)
            magenta_like = key_r > 200 and key_b > 200 and key_g < 80 and r > 105 and b > 105 and g < 150
            if magenta_like and distance < fringe_threshold:
                pixels[x, y] = (r, g, b, 0)
            elif magenta_like and a < 230:
                pixels[x, y] = (min(r, 85), max(g, 35), min(b, 100), max(0, a - 80))

    return rgba


def remove_corner_background(image: Image.Image, tolerance: int = 38) -> Image.Image:
    """Remove a flat-ish background connected to the corners.

    This is useful for black-background reference art where a global color
    threshold would destroy dark character details.
    """
    rgba = image.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()
    corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
    keys = [pixels[x, y][:3] for x, y in corners]
    seen: set[tuple[int, int]] = set()
    stack = corners[:]

    def close_to_any_key(color: tuple[int, int, int]) -> bool:
        return any(sum(abs(color[i] - key[i]) for i in range(3)) <= tolerance for key in keys)

    while stack:
        x, y = stack.pop()
        if (x, y) in seen or x < 0 or y < 0 or x >= width or y >= height:
            continue
        seen.add((x, y))
        r, g, b, a = pixels[x, y]
        if a == 0 or not close_to_any_key((r, g, b)):
            continue
        pixels[x, y] = (r, g, b, 0)
        stack.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])

    return rgba


def crop_to_visible(image: Image.Image, pad: int = 16) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        raise ValueError("Could not find visible pixels after background removal")

    left, top, right, bottom = bbox
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(rgba.width, right + pad)
    bottom = min(rgba.height, bottom + pad)
    return rgba.crop((left, top, right, bottom))


def fit_to_cell(image: Image.Image, fill: float = 0.80) -> Image.Image:
    sprite = image.convert("RGBA")
    max_width = int(CELL_WIDTH * fill)
    max_height = int(CELL_HEIGHT * fill)
    scale = min(max_width / sprite.width, max_height / sprite.height)
    size = (max(1, int(sprite.width * scale)), max(1, int(sprite.height * scale)))
    sprite = sprite.resize(size, Image.Resampling.LANCZOS)

    cell = Image.new("RGBA", (CELL_WIDTH, CELL_HEIGHT), (0, 0, 0, 0))
    x = (CELL_WIDTH - sprite.width) // 2
    y = CELL_HEIGHT - sprite.height - 8
    cell.alpha_composite(sprite, (x, y))
    return cell


def neon_boost(image: Image.Image, amount: float) -> Image.Image:
    if amount <= 0:
        return image
    boosted = ImageEnhance.Color(image).enhance(1.0 + amount)
    boosted = ImageEnhance.Contrast(boosted).enhance(1.0 + amount * 0.35)
    return boosted


def tint_status(image: Image.Image, color: tuple[int, int, int], strength: float) -> Image.Image:
    alpha = image.getchannel("A")
    overlay = Image.new("RGBA", image.size, (*color, 0))
    overlay.putalpha(alpha.point(lambda value: int(value * strength)))
    return Image.alpha_composite(image, overlay)


def transform_frame(
    source_cell: Image.Image,
    frame_index: int,
    *,
    dx: int = 0,
    dy: int = 0,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    rotation: float = 0.0,
    flip: bool = False,
    color_boost: float = 0.0,
    tint: tuple[tuple[int, int, int], float] | None = None,
) -> Image.Image:
    sprite = source_cell.convert("RGBA")
    if flip:
        sprite = ImageOps.mirror(sprite)
    if color_boost:
        sprite = neon_boost(sprite, color_boost)
    if tint:
        sprite = tint_status(sprite, tint[0], tint[1])

    if scale_x != 1.0 or scale_y != 1.0:
        sprite = sprite.resize(
            (max(1, int(CELL_WIDTH * scale_x)), max(1, int(CELL_HEIGHT * scale_y))),
            Image.Resampling.LANCZOS,
        )
    if rotation:
        sprite = sprite.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=True)

    cell = Image.new("RGBA", (CELL_WIDTH, CELL_HEIGHT), (0, 0, 0, 0))
    x = (CELL_WIDTH - sprite.width) // 2 + dx
    y = (CELL_HEIGHT - sprite.height) // 2 + dy
    cell.alpha_composite(sprite, (x, y))
    return cell


def make_frame(source_cell: Image.Image, row_name: str, frame_index: int) -> Image.Image:
    phase = (frame_index / FRAME_COUNT) * math.tau
    bob = int(round(math.sin(phase) * 3))
    wobble = math.sin(phase)

    if row_name == "idle":
        return transform_frame(source_cell, frame_index)

    if row_name == "running-right":
        return transform_frame(
            source_cell,
            frame_index,
            dx=int(round(math.sin(phase) * 4)),
            dy=abs(int(round(math.sin(phase) * 5))) - 3,
            scale_x=0.99 + 0.02 * abs(wobble),
            scale_y=1.01 - 0.025 * abs(wobble),
            rotation=-4 + 8 * abs(math.sin(phase + 0.8)),
        )

    if row_name == "running-left":
        return transform_frame(
            source_cell,
            frame_index,
            dx=-int(round(math.sin(phase) * 4)),
            dy=abs(int(round(math.sin(phase) * 5))) - 3,
            scale_x=0.99 + 0.02 * abs(wobble),
            scale_y=1.01 - 0.025 * abs(wobble),
            rotation=4 - 8 * abs(math.sin(phase + 0.8)),
            flip=True,
        )

    if row_name == "happy":
        return transform_frame(
            source_cell,
            frame_index,
            dy=-2 + int(round(math.sin(phase) * 4)),
            rotation=math.sin(phase * 2) * 4,
            scale_x=1.0 + 0.018 * math.sin(phase),
            scale_y=1.0 - 0.018 * math.sin(phase),
            color_boost=0.25,
        )

    if row_name == "sitting":
        return transform_frame(
            source_cell,
            frame_index,
            dy=11 + int(round(math.sin(phase) * 2)),
            scale_x=1.05,
            scale_y=0.91,
            color_boost=0.06,
        )

    if row_name == "picked-up":
        return transform_frame(
            source_cell,
            frame_index,
            dy=-5 + int(round(math.sin(phase) * 2)),
            scale_x=0.98,
            scale_y=0.96,
            color_boost=0.08,
        )

    if row_name == "resting":
        return transform_frame(
            source_cell,
            frame_index,
            dy=10,
            scale_x=1.03,
            scale_y=0.92,
            color_boost=0.08,
        )

    if row_name == "waving":
        return transform_frame(
            source_cell,
            frame_index,
            dy=int(round(math.sin(phase) * 2)),
            rotation=math.sin(phase * 2) * 6,
            color_boost=0.18,
        )

    if row_name == "jumping":
        jump = -int(round(abs(math.sin(phase)) * 22))
        squash = 1.0 + (0.05 if frame_index in (0, 7) else -0.025)
        return transform_frame(source_cell, frame_index, dy=jump, scale_x=squash, scale_y=2.0 - squash)

    if row_name == "failed":
        return transform_frame(
            source_cell,
            frame_index,
            dy=2 + bob,
            rotation=-4 if frame_index % 2 == 0 else 4,
            tint=((200, 40, 70), 0.09),
        )

    if row_name == "waiting":
        return transform_frame(
            source_cell,
            frame_index,
            dy=int(round(math.sin(phase) * 4)),
            scale_x=1.0 + 0.015 * math.sin(phase),
            scale_y=1.0 - 0.015 * math.sin(phase),
            color_boost=0.24,
        )

    if row_name == "running":
        return transform_frame(
            source_cell,
            frame_index,
            dx=int(round(math.sin(phase) * 3)),
            dy=abs(int(round(math.sin(phase) * 5))) - 3,
            rotation=math.sin(phase) * 5,
            color_boost=0.1,
        )

    if row_name == "review":
        return transform_frame(
            source_cell,
            frame_index,
            dy=bob,
            color_boost=0.2 if frame_index % 2 == 0 else 0.05,
            tint=((60, 180, 255), 0.05),
        )

    return source_cell


def build_spritesheet(source_cell: Image.Image) -> Image.Image:
    sheet = Image.new("RGBA", (CELL_WIDTH * FRAME_COUNT, CELL_HEIGHT * len(ANIMATION_ROWS)), (0, 0, 0, 0))
    for row, row_name in enumerate(ANIMATION_ROWS):
        for frame in range(FRAME_COUNT):
            sheet.alpha_composite(make_frame(source_cell, row_name, frame), (frame * CELL_WIDTH, row * CELL_HEIGHT))
    return sheet


def hatch_pet(
    image_path: Path,
    *,
    pet_id: str,
    name: str,
    pets_dir: Path = DEFAULT_PETS_DIR,
    key: str | None = None,
    background_tolerance: int = 38,
    overwrite: bool = False,
) -> Path:
    paths = pet_paths(pets_dir, pet_id)
    if paths.root.exists() and not overwrite:
        raise FileExistsError(f"{paths.root} already exists. Pass --overwrite to replace this pet.")

    image = Image.open(image_path)
    key_color = parse_hex_color(key)
    if key_color:
        transparent = despill_chroma_key(remove_chroma_key(image, key_color, transparent_threshold=50, opaque_threshold=210), key_color)
    else:
        transparent = remove_corner_background(image, tolerance=background_tolerance)

    source = crop_to_visible(transparent)
    source_cell = fit_to_cell(source, fill=0.80)
    spritesheet = build_spritesheet(source_cell)

    paths.root.mkdir(parents=True, exist_ok=True)
    source_cell.save(paths.source)
    spritesheet.save(paths.spritesheet)

    manifest = {
        "id": pet_id,
        "name": name,
        "version": 1,
        "cellWidth": CELL_WIDTH,
        "cellHeight": CELL_HEIGHT,
        "frameCount": FRAME_COUNT,
        "sprite": "spritesheet.png",
        "source": "source.png",
        "animations": {
            row_name: {
                "row": row_index,
                "frames": FRAME_COUNT,
                "fps": DEFAULT_FPS[row_name],
            }
            for row_index, row_name in enumerate(ANIMATION_ROWS)
        },
        "runtime": {
            "defaultScale": 1.0,
            "walkSpeed": 1.2,
            "groundPadding": 24,
        },
        "hatch": {
            "input": str(image_path),
            "backgroundKey": key,
            "backgroundTolerance": background_tolerance,
            "sourceFill": 0.80,
            "pipeline": "single-source-transform-atlas",
        },
    }
    paths.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return paths.root
