# Row Strip Prompt Template

Use this template for any animation row.

```text
Create one horizontal sprite row for the same desktop companion character.

Animation:
<animation-name>

Action:
<describe the pose sequence across 8 frames>

Required output:
- 8 frames in one horizontal row.
- Each frame is a 192x208 cell.
- Total image size is 1536x208.
- Transparent background, or pure magenta #ff00ff background if transparency is not available.
- No frame numbers, labels, borders, grids, scenery, UI, or watermarks.
- Same character identity, colors, proportions, outline, and accessories in every frame.
- Normal full-body frames should be around 198px visible height and bottom-aligned with 5 to 8px padding.
- Motion should read clearly at small desktop-pet size.

Frame direction:
1. <frame 1>
2. <frame 2>
3. <frame 3>
4. <frame 4>
5. <frame 5>
6. <frame 6>
7. <frame 7>
8. <frame 8>
```

Import the row with:

```bash
python3 run.py import-row-sheet pets/<pet-id> <animation-name> /path/to/row.png --row <row-index> --frames 8 --fps <fps> --target-height 198
```
