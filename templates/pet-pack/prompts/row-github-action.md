# GitHub Action Row Prompt

```text
Create one horizontal sprite row for the same desktop companion character.

Animation:
github-action

Action:
The companion performs a GitHub action by pressing a small round button with the GitHub mark on it. The action should feel deliberate and satisfying, like the pet is helping push or merge code.

Required output:
- 8 frames in one horizontal row.
- Each frame is a 192x208 cell.
- Total image size is 1536x208.
- Transparent background, or pure magenta #ff00ff background if transparency is not available.
- No frame numbers, labels, borders, grids, scenery, UI, or watermarks.
- Same character identity, colors, proportions, outline, and accessories in every frame.
- Normal full-body frames should be around 198px visible height and bottom-aligned with 5 to 8px padding.
- The GitHub mark should be small, readable, and treated as a prop, not a copied app screenshot.

Frame direction:
1. Companion stands beside a small glowing GitHub button.
2. Companion raises a paw or hand toward the button.
3. Paw hovers over the button.
4. Paw presses the button.
5. Button glows or emits small pixel sparks.
6. Companion holds the press for a beat.
7. Companion releases, button glow fades.
8. Companion returns to ready stance for a smooth loop.
```

Suggested import:

```bash
python3 run.py import-row-sheet pets/<pet-id> github-action /path/to/github-action-row.png --row 14 --frames 8 --fps 4 --target-height 198
```
