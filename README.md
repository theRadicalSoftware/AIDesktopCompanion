# AI Desktop Companion

A Linux-first local desktop companion runtime for AI-assisted work.

Inspired by Codex Pets. Not affiliated with OpenAI. Built as a local Linux-first exploration of AI desktop companions.

Early experimental release. Tested primarily on Pop!_OS/X11. Wayland support is not the focus yet.

The app runs a transparent, always-on-top PyQt6 companion on X11. It can walk around the desktop, react to drag/drop, show Codex session status in an animated thought bubble, accept replies, and run optional provider bridges for Codex, Claude, Slack, and GitHub.

This public repo is the reusable engine and template kit. It intentionally ships with a neutral `starter-buddy` demo pet instead of a personal character pack.

## Quick Start

```bash
git clone https://github.com/theRadicalSoftware/AIDesktopCompanion.git
cd AIDesktopCompanion
python3 -m pip install -r requirements.txt
python3 run.py run starter-buddy --scale 1.1 --codex-session current
```

On Pop OS or other systemd-based Linux desktops, launch it as a user service:

```bash
./launch-companion.sh
```

Stop it with:

```bash
./stop-companion.sh
```

## Requirements

- Linux desktop
- Python 3
- PyQt6
- Pillow
- X11 recommended
- `xdotool` for Codex approval buttons from the thought bubble
- Optional: Codex CLI for Codex work/session status
- Optional: Anthropic API key for Claude provider support
- Optional: Slack token for Slack messaging support
- Optional: Git + SSH key for GitHub actions

Check local dependencies:

```bash
python3 run.py doctor
```

## What It Does

- Runs a local desktop companion pet from a modular pet pack.
- Supports fixed-cell `192x208` sprite atlases.
- Shows a thought bubble for Codex work, waiting replies, and provider status.
- Lets you reply to known waiting Codex sessions from the bubble.
- Can answer visible Codex terminal approval prompts through X11/`xdotool`.
- Accepts file/folder drops for safe Codex work.
- Can stream Claude responses into the thought bubble when configured.
- Can send and poll Slack messages when configured.
- Can run guarded GitHub actions such as push and merge/push through local Git/SSH.

## Run A Different Pet

Use either a bundled pet id or an absolute path to a pet folder:

```bash
python3 run.py run starter-buddy --scale 1.1
python3 run.py run /path/to/my-pet-pack --scale 1.1 --codex-session current
```

The launcher uses `PET=starter-buddy` by default:

```bash
PET=/path/to/my-pet-pack ./launch-companion.sh
PET=starter-buddy CODEX_SESSION=off ./launch-companion.sh
```

## Create Your Own Pet

Start with these docs:

- [Give This Repo To Codex](docs/CODEX_START_HERE.md)
- [Pet Pack Spec](docs/PET_PACK_SPEC.md)
- [Create A Pet](docs/CREATE_A_PET.md)
- [Animation System](docs/ANIMATION_SYSTEM.md)

The fastest path is to give this repo to Codex and ask it to follow [CODEX_START_HERE.md](docs/CODEX_START_HERE.md). The template prompts under `templates/pet-pack/prompts/` describe the required and optional animation rows.

Useful starting files:

- `pets/starter-buddy/pet.json`: runnable demo manifest
- `templates/pet-pack/pet.template.json`: fuller manifest template with provider rows
- `examples/secrets.example.json`: local secret-file shape
- `examples/slack-contact.example.json`: Slack contact configuration shape

## Provider Config

Provider secrets should be environment variables or local ignored config. Do not put keys in `pet.json`.

Claude:

```bash
export ANTHROPIC_API_KEY='<anthropic-api-key>'
```

Slack:

```bash
export SLACK_BOT_TOKEN='<slack-bot-token>'
export SLACK_USER_TOKEN='<slack-user-token>' # optional
```

GitHub:

```bash
ssh-add ~/.ssh/id_ed25519
git remote set-url origin git@github.com:owner/repo.git
```

See [Provider Config](docs/PROVIDER_CONFIG.md) and [Security](docs/SECURITY.md).

## Public Repo Boundary

This repo should stay reusable:

- Include engine code, docs, templates, and permissive demo assets.
- Do not commit personal pet packs, private character art, API keys, Slack IDs, runtime logs, or local transcripts.
- Keep real/custom pets in a separate private repo or an ignored local folder when needed.

## License

MIT. See [LICENSE](LICENSE).
