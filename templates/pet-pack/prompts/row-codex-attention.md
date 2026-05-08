# Codex Attention Row Prompt

```text
Create one horizontal sprite row for the same desktop companion character.

Animation:
codex-attention

Action:
The companion notices that Codex needs the user's response. It should politely get attention without looking panicked or blocking the screen.

Required output:
- 8 frames in one horizontal row.
- Each frame is a 192x208 cell.
- Total image size is 1536x208.
- Transparent background, or pure magenta #ff00ff background if transparency is not available.
- No frame numbers, labels, borders, grids, scenery, UI, or watermarks.
- Same character identity, colors, proportions, outline, and accessories in every frame.
- Normal full-body frames should be around 198px visible height and bottom-aligned with 5 to 8px padding.

Frame direction:
1. Neutral attentive stance, eyes toward the viewer.
2. One paw or hand lifts slightly.
3. Paw lifts higher with a small glow or alert mark near the hand.
4. Companion leans forward, alert mark brighter.
5. Companion points upward as if saying "your turn".
6. Small bounce, alert mark starts to fade.
7. Paw lowers with friendly expression.
8. Returns close to frame 1 for a smooth loop.
```

Suggested import:

```bash
python3 run.py import-row-sheet pets/<pet-id> codex-attention /path/to/codex-attention-row.png --row 13 --frames 8 --fps 3.2 --target-height 198
```
