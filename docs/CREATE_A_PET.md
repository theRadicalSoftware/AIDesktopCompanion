# Create A Pet

The runtime can use any pet pack that follows [PET_PACK_SPEC.md](PET_PACK_SPEC.md).

## Basic Workflow

1. Create a character reference image.
2. Generate row strips for the required animations.
3. Import each row into `spritesheet.png`.
4. Update `pet.json`.
5. Run the pet locally.

The included `starter-buddy` pack is deliberately simple. Treat it as a runnable manifest example, not as a visual quality target.

## Generate Rows

Use the prompts in `templates/pet-pack/prompts/` as row-level instructions. Each prompt is written so Codex can hand it to an image generation workflow or a pet-generation skill.

Important constraints:

- Same character identity in every row.
- Same proportions, outline, palette, and accessories in every row.
- `192x208` cells.
- 8 frames per row.
- Magenta or transparent background before import.
- No text, labels, frame numbers, grids, or scenery baked into the row.
- Keep effects attached to the pet silhouette and small enough to read at desktop-pet size.

## Import A Row

```bash
python3 run.py import-row-sheet \
  pets/my-pet \
  codex-attention \
  /path/to/codex-attention-row.png \
  --row 30 \
  --frames 8 \
  --fps 3.2 \
  --target-height 198 \
  --key '#ff00ff' \
  --rows 1 \
  --cols 8
```

The importer normalizes visible height to the project default so new rows do not shrink compared with the main character rows.

## Run A Pet

```bash
python3 run.py run pets/my-pet --scale 1.1 --codex-session current
```

If a pet is outside the repo:

```bash
python3 run.py run /absolute/path/to/my-pet --scale 1.1
```

## Launch As A Service

```bash
PET=pets/my-pet ./launch-companion.sh
```

## Validate

```bash
python3 -m json.tool pets/my-pet/pet.json >/dev/null
python3 -m compileall hatchpet run.py
python3 run.py doctor
python3 run.py check-codex-pet pets/my-pet
```

Then visually inspect the pet while it switches between idle, work, reply, and attention animations. If it appears to grow or shrink, inspect the row bounding boxes and normalize the row source before importing again.

## Export For Codex App

If the pet should also be installable by the official Codex app, export the first 9 rows into the minimal Codex package:

```bash
python3 run.py export-codex-pet pets/my-pet
```

The export writes to `${CODEX_HOME:-~/.codex}/pets/<pet-id>/` unless `--output-dir` is provided. The richer native manifest and extra animation rows stay in the AI Desktop Companion pet folder.
