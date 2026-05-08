# Thought Bubble Assets

Generated with the `hatch-pet` workflow for the default desktop companion thought bubble.

- `cyber-thought-bubble-source.png`: original generated chroma-key source.
- `cyber-thought-bubble-base.png`: transparent source after chroma-key cleanup.
- `cyber-thought-bubble-frame.png`: single 560x300 transparent runtime frame.
- `cyber-thought-bubble-spritesheet.png`: 18-frame horizontal runtime sheet, 560x300 per frame. Frames 0-4 open, 5-12 hold, and 13-17 close.
- `cyber-thought-bubble-preview.gif`: quick animation preview.

Runtime wiring lives in `hatchpet/desktop_pet.py`; pet packs can point `runtime.codexBubble.sprite` at this sheet or provide their own bubble art.
