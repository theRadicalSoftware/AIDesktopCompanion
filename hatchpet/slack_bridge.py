from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SLACK_API_BASE = "https://slack.com/api"
REQUEST_TIMEOUT_SECONDS = 10.0
REQUEST_ATTEMPTS = 2
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 200
MAX_TEXT_CHARS = 40_000
MAX_RESPONSE_BYTES = 2_000_000
GET_METHODS = {"conversations.history"}

TOKEN_ENV_VARS = (
    "SLACK_API_TOKEN",
    "SLACK_ACCESS_TOKEN",
    "SLACK_BOT_TOKEN",
    "AI_DESKTOP_COMPANION_SLACK_BOT_TOKEN",
    "DESKTOP_COMPANION_SLACK_TOKEN",
    "SLACK_USER_TOKEN",
    "AI_DESKTOP_COMPANION_SLACK_USER_TOKEN",
    "DESKTOP_COMPANION_SLACK_USER_TOKEN",
)
BOT_TOKEN_ENV_VARS = (
    "SLACK_BOT_TOKEN",
    "AI_DESKTOP_COMPANION_SLACK_BOT_TOKEN",
    "DESKTOP_COMPANION_SLACK_TOKEN",
    "SLACK_API_TOKEN",
    "SLACK_ACCESS_TOKEN",
)
USER_TOKEN_ENV_VARS = (
    "SLACK_USER_TOKEN",
    "AI_DESKTOP_COMPANION_SLACK_USER_TOKEN",
    "DESKTOP_COMPANION_SLACK_USER_TOKEN",
)
SECRET_KEYS = (
    "slackApiToken",
    "slackAccessToken",
    "slackBotToken",
    "aiDesktopCompanionSlackToken",
    "desktopCompanionSlackToken",
    "slackUserToken",
    "aiDesktopCompanionSlackUserToken",
    "desktopCompanionSlackUserToken",
)
BOT_SECRET_KEYS = (
    "slackBotToken",
    "aiDesktopCompanionSlackToken",
    "desktopCompanionSlackToken",
    "slackApiToken",
    "slackAccessToken",
)
USER_SECRET_KEYS = (
    "slackUserToken",
    "aiDesktopCompanionSlackUserToken",
    "desktopCompanionSlackUserToken",
)


class SlackBridgeError(Exception):
    pass


class SlackApiError(SlackBridgeError):
    def __init__(self, detail: str, *, error: str = "", data: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.error = error
        self.data = data or {}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        emit_error("Invalid request JSON.", error=str(exc))
        return 1

    if not isinstance(request, dict):
        emit_error("Slack request must be a JSON object.")
        return 1

    action = clean_text(request.get("action"), 40).lower()
    token_kind = clean_token_kind(request.get("token_kind") or request.get("send_as"))
    token = find_slack_token(token_kind)
    if not token:
        detail = "Slack needs a user token." if token_kind == "user" else "Slack needs an API token."
        emit_error(detail, error="missing_token", token_kind=token_kind)
        return 2

    try:
        if action == "send":
            result = action_send(token, request)
            emit("slack_sent", **result)
            return 0
        if action == "history":
            result = action_history(token, request)
            emit("slack_messages", **result)
            return 0
        if action == "open_dm":
            user_id = clean_text(request.get("user_id"), 120)
            conversation_id = open_direct_conversation(token, user_id)
            emit("slack_messages", action="open_dm", conversation_id=conversation_id, messages=[])
            return 0
        if action == "auth_test":
            result = slack_api_call(token, "auth.test", {})
            emit("slack_messages", action="auth_test", auth=safe_auth_payload(result))
            return 0
    except SlackBridgeError as exc:
        emit_error(str(exc))
        return 1
    except Exception as exc:
        emit_error("Slack request failed.", error=clean_text(exc, 300))
        return 1

    emit_error("Unsupported Slack action.", action=action or "")
    return 1


def action_send(token: str, request: dict[str, Any]) -> dict[str, Any]:
    conversation_id = clean_text(request.get("conversation_id"), 120)
    user_id = clean_text(request.get("user_id"), 120)
    if not conversation_id:
        conversation_id = slack_conversation_id(token, request)
    text = clean_text(request.get("text"), MAX_TEXT_CHARS)
    if not text:
        raise SlackBridgeError("Missing text.")

    try:
        return post_slack_message(token, conversation_id, text)
    except SlackApiError as exc:
        if exc.error == "channel_not_found" and user_id:
            conversation_id = open_direct_conversation(token, user_id)
            return post_slack_message(token, conversation_id, text)
        raise


def post_slack_message(token: str, conversation_id: str, text: str) -> dict[str, Any]:
    result = slack_api_call(
        token,
        "chat.postMessage",
        {
            "channel": conversation_id,
            "text": text,
        },
    )
    return {
        "conversation_id": str(result.get("channel") or conversation_id),
        "message_ts": str(result.get("ts") or ""),
    }


def action_history(token: str, request: dict[str, Any]) -> dict[str, Any]:
    conversation_id = clean_text(request.get("conversation_id"), 120)
    user_id = clean_text(request.get("user_id"), 120)
    if not conversation_id:
        conversation_id = slack_conversation_id(token, request)

    payload: dict[str, Any] = {
        "channel": conversation_id,
        "limit": bounded_limit(request.get("limit")),
    }
    oldest = clean_text(request.get("oldest"), 80)
    if oldest:
        payload["oldest"] = oldest

    try:
        result = slack_api_call(token, "conversations.history", payload)
    except SlackApiError as exc:
        if exc.error == "channel_not_found" and user_id:
            conversation_id = open_direct_conversation(token, user_id)
            payload["channel"] = conversation_id
            result = slack_api_call(token, "conversations.history", payload)
        else:
            raise
    messages = result.get("messages")
    if not isinstance(messages, list):
        messages = []

    return {
        "conversation_id": conversation_id,
        "messages": [safe_message(message) for message in messages if isinstance(message, dict)],
        "has_more": bool(result.get("has_more")),
        "response_metadata": safe_response_metadata(result.get("response_metadata")),
    }


def slack_conversation_id(token: str, request: dict[str, Any]) -> str:
    conversation_id = clean_text(request.get("conversation_id"), 120)
    if conversation_id:
        return conversation_id
    user_id = clean_text(request.get("user_id"), 120)
    if user_id:
        return open_direct_conversation(token, user_id)
    raise SlackBridgeError("Missing conversation_id or user_id.")


def open_direct_conversation(token: str, user_id: str) -> str:
    if not user_id:
        raise SlackBridgeError("Missing user_id.")
    result = slack_api_call(token, "conversations.open", {"users": user_id})
    channel = result.get("channel")
    if not isinstance(channel, dict):
        raise SlackBridgeError("Slack returned no direct message channel.")
    conversation_id = clean_text(channel.get("id"), 120)
    if not conversation_id:
        raise SlackBridgeError("Slack returned no direct message id.")
    return conversation_id


def slack_api_call(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{SLACK_API_BASE}/{method}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "ai-desktop-companion-slack-bridge/1.0",
    }
    request_method = "POST"
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    if method in GET_METHODS:
        query = urllib.parse.urlencode({key: value for key, value in payload.items() if value not in {"", None}})
        if query:
            url += "?" + query
        body = None
        request_method = "GET"
    else:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=request_method,
    )

    for attempt in range(REQUEST_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except urllib.error.HTTPError as exc:
            detail = safe_http_error(exc)
            raise SlackBridgeError(f"Slack HTTP error in {method}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt + 1 < REQUEST_ATTEMPTS:
                time.sleep(0.4)
                continue
            raise SlackBridgeError(f"Slack network error in {method}: {clean_text(exc.reason, 220)}") from exc
        except TimeoutError as exc:
            if attempt + 1 < REQUEST_ATTEMPTS:
                time.sleep(0.4)
                continue
            raise SlackBridgeError(f"Slack request timed out in {method}.") from exc
    else:
        raise SlackBridgeError(f"Slack request failed in {method}.")

    if len(response_body) > MAX_RESPONSE_BYTES:
        raise SlackBridgeError("Slack response was too large.")

    try:
        data = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SlackBridgeError("Slack returned an invalid JSON response.") from exc

    if not isinstance(data, dict):
        raise SlackBridgeError("Slack returned an unexpected response.")
    if not data.get("ok"):
        error = clean_text(data.get("error"), 160) or "unknown_error"
        raise SlackApiError(slack_api_error_detail(data), error=error, data=data)
    return data


def slack_api_error_detail(data: dict[str, Any]) -> str:
    error = clean_text(data.get("error"), 160) or "unknown_error"
    parts = [f"Slack API error: {error}"]
    needed = clean_text(data.get("needed"), 320)
    provided = clean_text(data.get("provided"), 420)
    warning = clean_text(data.get("warning"), 220)
    if needed:
        parts.append(f"needed: {needed}")
    if provided:
        parts.append(f"provided: {provided}")
    if warning:
        parts.append(f"warning: {warning}")
    return "; ".join(parts)


def clean_token_kind(value: Any) -> str:
    token_kind = clean_text(value, 30).lower()
    if token_kind in {"bot", "user"}:
        return token_kind
    return "auto"


def find_slack_token(kind: str = "auto") -> str:
    if kind == "user":
        return find_slack_token_in(USER_TOKEN_ENV_VARS, USER_SECRET_KEYS)
    if kind == "bot":
        return find_slack_token_in(BOT_TOKEN_ENV_VARS, BOT_SECRET_KEYS)
    return find_slack_token_in(TOKEN_ENV_VARS, SECRET_KEYS)


def find_slack_token_in(env_names: tuple[str, ...], secret_keys: tuple[str, ...]) -> str:
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value

    for path in secret_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for key in secret_keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def slack_token_available() -> bool:
    return bool(find_slack_token())


def slack_user_token_available() -> bool:
    return bool(find_slack_token("user"))


def secret_paths() -> list[Path]:
    paths = []
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if config_home:
        paths.append(Path(config_home) / "ai-desktop-companion" / "secrets.json")
    paths.append(Path.home() / ".config" / "ai-desktop-companion" / "secrets.json")
    return paths


def bounded_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_LIMIT
    return max(1, min(limit, MAX_HISTORY_LIMIT))


def clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def safe_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": clean_text(message.get("type"), 40),
        "subtype": clean_text(message.get("subtype"), 80),
        "user": clean_text(message.get("user"), 80),
        "bot_id": clean_text(message.get("bot_id"), 80),
        "ts": clean_text(message.get("ts"), 80),
        "text": clean_text(message.get("text"), MAX_TEXT_CHARS),
        "thread_ts": clean_text(message.get("thread_ts"), 80),
    }


def safe_response_metadata(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    cursor = clean_text(value.get("next_cursor"), 400)
    return {"next_cursor": cursor} if cursor else {}


def safe_auth_payload(value: dict[str, Any]) -> dict[str, str]:
    return {
        "url": clean_text(value.get("url"), 300),
        "team": clean_text(value.get("team"), 160),
        "user": clean_text(value.get("user"), 160),
        "team_id": clean_text(value.get("team_id"), 80),
        "user_id": clean_text(value.get("user_id"), 80),
        "bot_id": clean_text(value.get("bot_id"), 80),
    }


def safe_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read(4096)
    except OSError:
        body = b""
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict):
        error = clean_text(data.get("error"), 160)
        if error:
            return f"{exc.code} {error}"
    return str(exc.code)


def emit(event_type: str, **payload: Any) -> None:
    data = {"type": event_type, **payload}
    print(json.dumps(data, ensure_ascii=True), flush=True)


def emit_error(detail: str, **payload: Any) -> None:
    emit("slack_error", detail=detail, **payload)


if __name__ == "__main__":
    raise SystemExit(main())
