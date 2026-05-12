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
| `running` | Busy work/in-progress loop |
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

## Hatch-Pet Row Semantics

When generating rows with the current OpenAI `hatch-pet` skill, keep the row meanings distinct:

- `idle` should stay calm and low-distraction: subtle breathing, blink, or head/body bob only.
- `running-right` and `running-left` are directional travel loops.
- `running` is non-directional active work, as if the pet is busy running a task. Do not make it literal foot-running, jogging, sprinting, or travel.

## Official Codex App Package

The official Codex app custom-pet package is intentionally smaller than this runtime's native pet pack:

```text
${CODEX_HOME:-~/.codex}/pets/<pet-id>/
  pet.json
  spritesheet.webp
```

The official manifest shape is:

```json
{
  "id": "pet-id",
  "displayName": "Pet Name",
  "description": "One short sentence.",
  "spritesheetPath": "spritesheet.webp"
}
```

The official atlas is always `1536x1872`: 8 columns, 9 rows, `192x208` cells. The rows are the required rows above in order: `idle`, `running-right`, `running-left`, `waving`, `jumping`, `failed`, `waiting`, `running`, and `review`. Unused cells after each row's official frame count must be fully transparent.

AI Desktop Companion keeps its richer manifest as the native format. Use the compatibility commands to bridge between the two:

```bash
python3 run.py check-codex-pet <pet-id-or-path>
python3 run.py export-codex-pet <rich-pet-id-or-path>
python3 run.py import-codex-pet <official-pet-id-or-path>
python3 run.py run-codex-pet <official-pet-id-or-path>
```

`export-codex-pet` crops the native sprite to the first 9 rows, clears official unused cells, writes `spritesheet.webp`, and writes the minimal official `pet.json`. Extra rows such as `phone-reply`, `github-action`, `slack-message`, glider, ceiling, usage-meter, or sleep rows remain native AI Desktop Companion extensions.

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
    "workDrop": {},
    "companions": {},
    "worktreeTasks": {}
  }
}
```

`runtime.companions` is optional. When enabled, it adds a right-click submenu that launches additional pet packs as detached processes:

```json
{
  "runtime": {
    "companions": {
      "enabled": true,
      "menuLabel": "Companions",
      "entries": [
        {
          "id": "sidekick",
          "label": "Sidekick",
          "pet": "sidekick",
          "scale": 1.0,
          "speed": 0.9,
          "codexSession": "terminal",
          "codexTerminal": {
            "enabled": true,
            "title": "Sidekick Codex",
            "cwd": ".",
            "sandbox": "workspace-write",
            "approvalPolicy": "untrusted",
            "noAltScreen": true
          }
        }
      ]
    }
  }
}
```

Use a separate pet folder for each companion so runtime state and output files do not collide. `codexSession: "off"` is the safe default for independent companion work without a visible terminal. `codexSession: "terminal"` launches a new Codex terminal and points the spawned pet at a private `pointer:` file under that pet's runtime folder. Use an explicit session id or rollout path when one companion should watch one known existing Codex terminal.

`runtime.worktreeTasks` enables Codex task terminals backed by Git worktrees:

```json
{
  "runtime": {
    "worktreeTasks": {
      "enabled": true,
      "baseRef": "HEAD",
      "companionId": "",
      "terminal": "auto",
      "terminalTitlePrefix": "Codex Worktree",
      "worktreesDir": ""
    }
  }
}
```

The default creates detached worktrees under `~/.codex/worktrees/ai-desktop-companion/`, records task state in `~/.codex/ai-desktop-companion/worktree-tasks.json`, and launches Codex with `workspace-write` plus `untrusted` approvals inside the generated checkout. Set `companionId` to one of the configured companion entries when worktree tasks should spawn a companion pet as the visible task owner.

Each task can also open a `Review / Handoff` pane. The pane shows changed files, full diffs, staged/unstaged views, per-file actions, hunk staging/reverting, commit controls, branch creation, push, pull-request opening, and safe handoff to the main checkout. The matching CLI commands are `worktree-task-diff`, `worktree-task-stage`, `worktree-task-unstage`, `worktree-task-revert`, `worktree-task-commit`, `worktree-task-push`, `worktree-task-pr`, and `worktree-task-handoff`.

Do not store API keys, Slack tokens, SSH keys, personal user IDs, or private project names in `pet.json`.
