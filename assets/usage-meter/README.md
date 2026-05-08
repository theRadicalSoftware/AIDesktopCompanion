# Usage Meter Assets

Generated with the `hatch-pet` asset workflow for the default Codex usage meter overlay.

- `cyber-usage-sign-spritesheet.png`: 18-frame electric portal sign with open, loop, and close phases.
- `cyber-usage-sign-preview.gif`: quick preview of the sign animation.
- `cyber-usage-sign-frame.png`: still frame used for runtime render checks.

The sign image intentionally contains no baked-in usage numbers. Runtime text is drawn by `UsageMeterOverlay` from local Codex rate-limit data.

At runtime, the cleaned sign sheet is painted in layers: the lower portal opens first on the ground next to the pet, then the sign panel reveals upward from the portal. Closing reverses that motion before hiding the portal.
