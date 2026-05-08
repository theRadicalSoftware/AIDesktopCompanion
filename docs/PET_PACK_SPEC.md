# Pet Pack Spec

A pet pack is a folder with a manifest and a fixed-cell sprite atlas.

```text
pets/my-pet/
  pet.json
  source.png
  spritesheet.png
```

You can also run a pet from outside the repo:

```bash
python3 run.py run /absolute/path/to/my-pet
```

## Required Geometry

- `cellWidth`: `192`
- `cellHeight`: `208`
- `frameCount`: `8`
- Atlas width: `1536px`
- Atlas height: `208 * row_count`
- Transparent background

Rows can use fewer active frames, but the atlas should still reserve 8 cells per row.

## Required Animations

Every usable pet should include:

| Animation | Purpose |
| --- | --- |
| `idle` | Default standing loop |
| `running-right` | Walking/moving right |
| `running-left` | Walking/moving left |
| `waving` | Manual menu action |
| `jumping` | Manual menu action and double-click action |
| `failed` | Error or hard failure pose |
| `waiting` | Waiting/thinking fallback |
| `review` | Work/review fallback |

Recommended aliases:

| Animation | Typical Alias |
| --- | --- |
| `happy` | `waiting` or a dedicated happy row |
| `sitting` | `resting` or a dedicated sitting row |
| `resting` | `sitting` or a bed row |
| `work-laptop` | `review` or a dedicated work row |
| `picked-up` | Drag/held pose |

## Optional Capability Rows

These rows make provider integrations feel polished:

| Animation | When It Plays |
| --- | --- |
| `prompting` | Direct Ask Codex/provider work |
| `file-receive` | File/folder drag hover |
| `file-inspect` | Drop confirmation bubble |
| `file-review` | Dropped file/folder review work |
| `phone-reply-start` | Reply composer opening |
| `phone-reply` | Reply composer typing/sending |
| `codex-attention` | Linked Codex session needs a response |
| `github-action` | Pet-launched GitHub action |
| `slack-message` | New Slack message |
| `slack-send` | Sending Slack message |
| `usage-meter` | Optional pet-side usage-meter button action |
| `rest-enter-bed` | Optional one-shot rest entrance |
| `sleeping` | Optional looping bed/rest row |
| `glider-deploy`, `gliding`, `glider-landing` | Optional high-drop rescue |
| `ceiling-grab`, `ceiling-hold`, `ceiling-release` | Optional ceiling hang |

The runtime has fallbacks, so a first custom pet can start with the required rows and add capability rows later.

## Scale Rule

For normal full-body rows, keep the visible character around `198px` tall inside each `192x208` cell, bottom-aligned with about `5px` of bottom padding.

This prevents visible size pulsing when the pet moves between idle, work, reply, and provider animations.

Exceptions:

- Top-anchored ceiling rows can start at `y=0`.
- Wide props, beds, gliders, and large accessories can hit the width cap before reaching `198px`.
- Squash/stretch rows such as hard landing can vary intentionally, but transitions should still feel gradual.

## Manifest Runtime Keys

The starter manifest in `pets/starter-buddy/pet.json` is the best reference. Runtime settings live under:

```json
{
  "runtime": {
    "codexBubble": {},
    "codexUsage": {},
    "aiProviders": {},
    "workDrop": {}
  }
}
```

Do not store API keys, Slack tokens, SSH keys, personal user IDs, or private project names in `pet.json`.
