# Provider Config

Provider support is optional. The companion runs without API keys.

## Codex

Codex support uses the local Codex CLI and local Codex session files.

Run with session watching:

```bash
python3 run.py run starter-buddy --codex-session current
```

Useful selectors:

- `current`: last local Codex session recorded by the Codex CLI
- `latest`: most recently updated rollout file
- `off`: no Codex session bubble
- a Codex session id
- an absolute `rollout-*.jsonl` path

## Claude

Claude support uses the Anthropic Messages API.

Set one of:

```bash
export ANTHROPIC_API_KEY='<anthropic-api-key>'
export CLAUDE_API_KEY='<anthropic-api-key>'
export AI_DESKTOP_COMPANION_CLAUDE_KEY='<anthropic-api-key>'
```

Or use a local ignored secret file:

```json
{
  "anthropicApiKey": "<anthropic-api-key>"
}
```

stored at:

```text
~/.config/ai-desktop-companion/secrets.json
```

Use `examples/secrets.example.json` as the shape for local config. Keep the real file outside the repo.

Enable Claude in a pet manifest:

```json
{
  "runtime": {
    "aiProviders": {
      "claude": {
        "enabled": true,
        "model": "claude-sonnet-4-6",
        "maxTokens": 4096
      }
    }
  }
}
```

## Slack

Slack support can post messages and poll a configured channel, group DM, or DM target.

Set tokens with environment variables:

```bash
export SLACK_BOT_TOKEN='<slack-bot-token>'
export SLACK_USER_TOKEN='<slack-user-token>' # optional, for sending as the user
```

Configure contacts in `pet.json`:

```json
{
  "runtime": {
    "aiProviders": {
      "slack": {
        "enabled": true,
        "sendAs": "bot",
        "contacts": [
          {
            "id": "teammate",
            "label": "Teammate",
            "userId": "U1234567890",
            "sendAs": "bot"
          }
        ],
        "pollEnabled": true
      }
    }
  }
}
```

Do not commit real Slack IDs or tokens into a public pet pack.

Use `examples/slack-contact.example.json` as the shape for Slack contacts.

## GitHub

GitHub support uses local Git and SSH, not a GitHub API token.

Requirements:

- project is a Git repo
- normal branch, not detached HEAD
- clean worktree for mutating actions
- GitHub SSH remote such as `git@github.com:owner/repo.git`
- noninteractive SSH key access through `ssh-agent` or `~/.ssh`

The GitHub submenu can:

- check repo access
- push the current branch
- merge remote main into the current branch, then push
- merge current branch into main, then push

The launcher forwards `SSH_AUTH_SOCK` and `GIT_SSH_COMMAND` when available.
