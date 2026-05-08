# Slack Send Row Prompt

```text
Create one horizontal sprite row for the same desktop companion character.

Animation:
slack-send

Action:
The companion sends a Slack message by tossing or launching a small message badge upward. It should feel like a completed send action, with restrained digital confetti.

Required output:
- 8 frames in one horizontal row.
- Each frame is a 192x208 cell.
- Total image size is 1536x208.
- Transparent background, or pure magenta #ff00ff background if transparency is not available.
- No frame numbers, labels, borders, grids, scenery, UI, or watermarks.
- Same character identity, colors, proportions, outline, and accessories in every frame.
- Normal full-body frames should be around 198px visible height and bottom-aligned with 5 to 8px padding.
- Keep confetti small and close to the pet silhouette so it remains readable in the cell.

Frame direction:
1. Companion holds a small message badge.
2. Companion winds up slightly.
3. Badge starts moving upward.
4. Badge leaves the hand.
5. Badge pops into a small digital sparkle.
6. Companion looks pleased while sparkles fade.
7. Companion lowers hand.
8. Companion returns to ready stance for a smooth loop.
```

Suggested import:

```bash
python3 run.py import-row-sheet pets/<pet-id> slack-send /path/to/slack-send-row.png --row 16 --frames 8 --fps 4 --target-height 198
```
