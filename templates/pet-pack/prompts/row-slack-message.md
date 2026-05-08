# Slack Message Row Prompt

```text
Create one horizontal sprite row for the same desktop companion character.

Animation:
slack-message

Action:
The companion gets a Slack message notification and holds up a small Slack-style app badge or message tile. The motion should feel helpful and noticeable.

Required output:
- 8 frames in one horizontal row.
- Each frame is a 192x208 cell.
- Total image size is 1536x208.
- Transparent background, or pure magenta #ff00ff background if transparency is not available.
- No frame numbers, labels, borders, grids, scenery, UI, or watermarks.
- Same character identity, colors, proportions, outline, and accessories in every frame.
- Normal full-body frames should be around 198px visible height and bottom-aligned with 5 to 8px padding.
- Use a small simplified Slack-like multicolor badge as a prop; do not include text.

Frame direction:
1. Companion looks idle and alert.
2. Small message badge pops into one hand.
3. Companion lifts the badge.
4. Badge glows with a small notification sparkle.
5. Companion tilts the badge toward the viewer.
6. Companion gives a short friendly wave with the badge.
7. Badge settles near the body.
8. Companion returns close to frame 1 for a smooth loop.
```

Suggested import:

```bash
python3 run.py import-row-sheet pets/<pet-id> slack-message /path/to/slack-message-row.png --row 15 --frames 8 --fps 4 --target-height 198
```
