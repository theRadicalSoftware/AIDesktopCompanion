# Animation System

AI Desktop Companion uses a fixed-cell sprite atlas and a small PyQt6 state machine.

## Atlas Layout

Each pet pack points to its atlas from `pet.json`:

```json
{
  "cellWidth": 192,
  "cellHeight": 208,
  "frameCount": 8,
  "sprite": "spritesheet.png"
}
```

Each animation entry maps a name to a row:

```json
{
  "animations": {
    "idle": {
      "row": 0,
      "frames": 8,
      "fps": 4
    }
  }
}
```

## Scale Consistency

Normal full-body rows should use a visible character height of about `198px` inside each `192x208` cell. Keep the pet centered and bottom-aligned with roughly `5px` bottom padding.

The importer default is set to that target height. This keeps rows imported later, such as reply or GitHub rows, from looking smaller than the base idle/walk rows.

## One-Shot Rows

Set `"loop": false` for one-shot sequences:

- `jumping`
- `failed`
- `falling`
- `landing-soft`
- `landing-hard`
- optional `glider-deploy`
- optional `glider-landing`
- optional `ceiling-grab`
- optional `ceiling-release`
- optional `rest-enter-bed`

The runtime holds the final frame until the motion state exits.

## Fallbacks

You do not need every optional row on day one. The runtime falls back through related rows. For example, if `github-action` is missing, GitHub work can use `work-laptop`, `review`, or `waiting`.

For a polished pet, add dedicated rows for the workflows your users will actually use.
