from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PETS_DIR = ROOT / "pets"

CELL_WIDTH = 192
CELL_HEIGHT = 208
FRAME_COUNT = 8

ANIMATION_ROWS = [
    "idle",
    "running-right",
    "running-left",
    "happy",
    "sitting",
    "picked-up",
    "resting",
    "waving",
    "jumping",
    "failed",
    "waiting",
    "running",
    "review",
]

DEFAULT_FPS = {
    "idle": 6,
    "running-right": 5,
    "running-left": 5,
    "happy": 5,
    "sitting": 5,
    "picked-up": 5,
    "resting": 4,
    "waving": 8,
    "jumping": 10,
    "failed": 4,
    "waiting": 6,
    "running": 5,
    "review": 6,
}


@dataclass(frozen=True)
class PetPaths:
    root: Path
    manifest: Path
    source: Path
    spritesheet: Path


def pet_paths(pets_dir: Path, pet_id: str) -> PetPaths:
    root = pets_dir / pet_id
    return PetPaths(
        root=root,
        manifest=root / "pet.json",
        source=root / "source.png",
        spritesheet=root / "spritesheet.png",
    )
