from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_monitor import CodexSessionMonitor


@dataclass(frozen=True)
class UsageWindow:
    used_percent: float | None
    window_minutes: int | None
    resets_at: int | None

    @property
    def left_percent(self) -> float | None:
        if self.used_percent is None:
            return None
        return max(0.0, min(100.0, 100.0 - self.used_percent))


@dataclass(frozen=True)
class UsageCredits:
    has_credits: bool
    unlimited: bool
    balance: str | None


@dataclass(frozen=True)
class CodexUsageStatus:
    available: bool
    primary: UsageWindow | None = None
    secondary: UsageWindow | None = None
    credits: UsageCredits | None = None
    plan_type: str | None = None
    rate_limit_reached_type: str | None = None
    total_tokens: int | None = None
    updated_at: float | None = None
    source_path: Path | None = None
    limit_id: str | None = None
    limit_name: str | None = None
    selection_note: str = ""
    reason: str = ""


@dataclass(frozen=True)
class UsageSnapshot:
    payload: dict[str, Any]
    source_path: Path
    fallback_credits: UsageCredits | None
    modified_at: float
    score: float
    note: str


class CodexUsageMonitor:
    def __init__(
        self,
        *,
        selector: str = "current",
        codex_home: Path | None = None,
        tail_bytes: int = 64_000_000,
    ) -> None:
        self.selector = selector
        self.codex_home = codex_home or (Path.home() / ".codex")
        self.tail_bytes = tail_bytes

    def poll(self) -> CodexUsageStatus:
        candidates = self.candidate_paths()
        if not candidates:
            return CodexUsageStatus(False, reason="No Codex rollout files found.")

        snapshots: list[UsageSnapshot] = []
        for path in candidates:
            payload, fallback_credits = self.latest_usage_snapshot(path)
            if payload:
                snapshots.append(self.snapshot_from_payload(payload, path, fallback_credits))
        if snapshots:
            best = self.best_snapshot(snapshots)
            return self.status_from_payload(
                best.payload,
                best.source_path,
                fallback_credits=best.fallback_credits,
                updated_at=best.modified_at,
                selection_note=best.note,
            )
        return CodexUsageStatus(False, reason="No local rate-limit event found yet.")

    def candidate_paths(self) -> list[Path]:
        paths: list[Path] = []
        monitor = CodexSessionMonitor(selector=self.selector, codex_home=self.codex_home)
        current = monitor.resolve_session_path()
        if current:
            paths.append(current)

        sessions_root = self.codex_home / "sessions"
        if sessions_root.exists():
            latest = sorted(
                (path for path in sessions_root.rglob("rollout-*.jsonl") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            paths.extend(latest[:48])

        deduped: list[Path] = []
        seen: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                deduped.append(resolved)
        return deduped

    def read_tail_lines(self, path: Path) -> list[str]:
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - self.tail_bytes))
                return handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return []

    def latest_token_count(self, path: Path) -> dict[str, Any] | None:
        payload, _credits = self.latest_usage_snapshot(path)
        return payload

    def latest_usage_snapshot(self, path: Path) -> tuple[dict[str, Any] | None, UsageCredits | None]:
        latest_payload = None
        latest_general_payload = None
        latest_explicit_credits = None
        lines = self.read_tail_lines(path)
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            rate_limits = payload.get("rate_limits")
            if isinstance(rate_limits, dict):
                if latest_payload is None:
                    latest_payload = payload
                if latest_general_payload is None and str(rate_limits.get("limit_id") or "") == "codex":
                    latest_general_payload = payload
                credits = self.credits_from_payload(rate_limits.get("credits"))
                if credits is not None and (credits.unlimited or credits.balance is not None):
                    latest_explicit_credits = credits
                if latest_general_payload is not None and latest_explicit_credits is not None:
                    break
        return latest_general_payload or latest_payload, latest_explicit_credits

    def status_from_payload(
        self,
        payload: dict[str, Any],
        source_path: Path,
        *,
        fallback_credits: UsageCredits | None = None,
        updated_at: float | None = None,
        selection_note: str = "",
    ) -> CodexUsageStatus:
        rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total_usage = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
        credits = self.resolve_credits(rate_limits.get("credits"), fallback_credits)

        return CodexUsageStatus(
            available=True,
            primary=self.window_from_payload(rate_limits.get("primary")),
            secondary=self.window_from_payload(rate_limits.get("secondary")),
            credits=credits,
            plan_type=str(rate_limits.get("plan_type")) if rate_limits.get("plan_type") else None,
            rate_limit_reached_type=(
                str(rate_limits.get("rate_limit_reached_type"))
                if rate_limits.get("rate_limit_reached_type")
                else None
            ),
            total_tokens=self.int_or_none(total_usage.get("total_tokens")),
            updated_at=updated_at or time.time(),
            source_path=source_path,
            limit_id=str(rate_limits.get("limit_id")) if rate_limits.get("limit_id") else None,
            limit_name=str(rate_limits.get("limit_name")) if rate_limits.get("limit_name") else None,
            selection_note=selection_note,
        )

    def snapshot_from_payload(
        self,
        payload: dict[str, Any],
        source_path: Path,
        fallback_credits: UsageCredits | None,
    ) -> UsageSnapshot:
        try:
            modified_at = source_path.stat().st_mtime
        except OSError:
            modified_at = time.time()
        score, note = self.score_payload(payload, modified_at)
        return UsageSnapshot(payload, source_path, fallback_credits, modified_at, score, note)

    def best_snapshot(self, snapshots: list[UsageSnapshot]) -> UsageSnapshot:
        active_general = [
            snapshot
            for snapshot in snapshots
            if self.snapshot_primary_active(snapshot) and self.snapshot_limit_id(snapshot) == "codex"
        ]
        if active_general:
            return max(active_general, key=lambda snapshot: snapshot.score)

        active = [snapshot for snapshot in snapshots if self.snapshot_primary_active(snapshot)]
        if active:
            return max(active, key=lambda snapshot: snapshot.modified_at)

        return max(snapshots, key=lambda snapshot: snapshot.score)

    def snapshot_primary_active(self, snapshot: UsageSnapshot) -> bool:
        rate_limits = snapshot.payload.get("rate_limits")
        if not isinstance(rate_limits, dict):
            return False
        primary = self.window_from_payload(rate_limits.get("primary"))
        return bool(primary and primary.resets_at and primary.resets_at > time.time())

    def snapshot_limit_id(self, snapshot: UsageSnapshot) -> str:
        rate_limits = snapshot.payload.get("rate_limits")
        if not isinstance(rate_limits, dict):
            return ""
        return str(rate_limits.get("limit_id") or "")

    def score_payload(self, payload: dict[str, Any], modified_at: float) -> tuple[float, str]:
        rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total_usage = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
        limit_id = str(rate_limits.get("limit_id") or "")
        limit_name = str(rate_limits.get("limit_name") or "")
        primary = self.window_from_payload(rate_limits.get("primary"))
        secondary = self.window_from_payload(rate_limits.get("secondary"))
        now = time.time()
        age_seconds = max(0.0, now - modified_at)

        score = max(0.0, 80.0 - age_seconds / 90.0)
        notes = []
        if primary and primary.resets_at and primary.resets_at > now:
            score += 80.0
        elif primary and primary.resets_at:
            score -= 260.0
            notes.append("expired 5h window")
        if secondary and secondary.resets_at and secondary.resets_at > now:
            score += 35.0

        if limit_id == "codex":
            score += 120.0
            notes.append("general Codex quota")
        elif limit_id.startswith("codex_"):
            score -= 35.0
            notes.append("model-specific quota")

        reported_usage = [
            window.used_percent
            for window in (primary, secondary)
            if window is not None and window.used_percent is not None
        ]
        if any(value > 0.0 for value in reported_usage):
            score += 25.0
            notes.append("nonzero usage")

        total_tokens = self.int_or_none(total_usage.get("total_tokens"))
        if (
            total_tokens is not None
            and total_tokens >= 1_000_000
            and reported_usage
            and all(value == 0.0 for value in reported_usage)
            and limit_id != "codex"
        ):
            score -= 95.0
            notes.append("suspicious zero usage")

        if limit_name:
            notes.append(limit_name)
        return score, ", ".join(notes)

    def credits_from_payload(self, value: Any) -> UsageCredits | None:
        if not isinstance(value, dict):
            return None
        return UsageCredits(
            has_credits=bool(value.get("has_credits")),
            unlimited=bool(value.get("unlimited")),
            balance=str(value.get("balance")) if value.get("balance") is not None else None,
        )

    def resolve_credits(self, value: Any, fallback_credits: UsageCredits | None) -> UsageCredits | None:
        credits = self.credits_from_payload(value)
        if credits is None:
            return fallback_credits
        if credits.unlimited or credits.balance is not None:
            return credits
        if not credits.has_credits:
            return UsageCredits(has_credits=False, unlimited=False, balance=None)
        return fallback_credits or credits

    def window_from_payload(self, value: Any) -> UsageWindow | None:
        if not isinstance(value, dict):
            return None
        return UsageWindow(
            used_percent=self.float_or_none(value.get("used_percent")),
            window_minutes=self.int_or_none(value.get("window_minutes")),
            resets_at=self.int_or_none(value.get("resets_at")),
        )

    def float_or_none(self, value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    def int_or_none(self, value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value) if isinstance(value, str) else value
            if isinstance(parsed, float) and not math.isfinite(parsed):
                return None
            return int(parsed)
        except (TypeError, ValueError, OverflowError):
            return None
