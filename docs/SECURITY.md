# Security

AI Desktop Companion is a local desktop tool. Treat pet packs, dropped files, and provider config as untrusted unless you created them.

## Do Not Commit Secrets

Never commit:

- API keys
- Slack bot/user tokens
- SSH private keys
- personal Slack IDs or private channel IDs
- local transcripts
- runtime work outputs
- `.env` files

Use environment variables or:

```text
~/.config/ai-desktop-companion/secrets.json
```

## Codex Work Drop

Dropped files are sent to Codex as prompt data, not shell-interpolated command strings. Review/explain/summarize defaults to read-only. Custom work can use `workspace-write` only when the user explicitly asks for edits.

## Codex Approvals

The approval bridge answers the visible Codex terminal approval UI through X11/`xdotool`. It does not send approval waits as ordinary text replies.

## GitHub Actions

GitHub actions run through local Git/SSH and block common unsafe states:

- no Git repository
- detached HEAD
- dirty worktree
- non-GitHub SSH remote
- missing SSH key access

The bridge uses `GIT_TERMINAL_PROMPT=0` and noninteractive SSH options so it does not hang on hidden credential prompts.

## Public Pet Packs

Public pet packs should include only assets you are allowed to redistribute. Keep personal character assets in a private repo or ignored local folder.
