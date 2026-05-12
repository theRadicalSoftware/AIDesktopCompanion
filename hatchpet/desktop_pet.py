from __future__ import annotations

import json
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QPoint, QProcess, QProcessEnvironment, QRect, QRectF, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QTextOption
from PyQt6.QtWidgets import QApplication, QFileDialog, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton, QTextEdit, QWidget

from .codex_approval import current_codex_action_required, send_codex_terminal_approval
from .codex_bridge import (
    ACTION_CUSTOM,
    ACTION_EXPLAIN,
    ACTION_LABELS,
    ACTION_REVIEW,
    ACTION_SUMMARIZE,
    AI_PROVIDER_CLAUDE,
    AI_PROVIDER_CODEX,
    AI_PROVIDERS,
    SAFE_SANDBOXES,
    WorkRequest,
    build_codex_exec_args,
    build_codex_exec_resume_args,
    build_work_prompt,
    codex_candidate_paths,
    codex_process_path,
    default_cwd_for_paths,
    detect_git_root,
    find_codex_executable,
    progress_from_json_line,
    short_work_title,
)
from .codex_monitor import CodexSessionMonitor, CodexStatus, write_pending_session_owner
from .codex_usage import CodexUsageMonitor, CodexUsageStatus, UsageWindow
from .pet_format import DEFAULT_PETS_DIR
from .task_review import TaskReviewDialog
from .worktree_tasks import (
    WorktreeTask,
    WorktreeTaskError,
    create_worktree_task,
    format_task_report,
    get_worktree_task,
    launch_worktree_task_terminal,
    list_worktree_tasks,
    remove_worktree_task,
    summarize_tasks,
    update_worktree_task,
)


AI_PROVIDER_SLACK = "slack"
AI_PROVIDER_GITHUB = "github"
REPLY_PROVIDERS = (*AI_PROVIDERS, AI_PROVIDER_SLACK)


class BubbleReplyEdit(QTextEdit):
    submitted = pyqtSignal()
    cancelled = pyqtSignal()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.submitted.emit()
                event.accept()
                return
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ThoughtBubble(QWidget):
    reply_requested = pyqtSignal()
    reply_submitted = pyqtSignal(str)
    reply_cancelled = pyqtSignal()
    approval_requested = pyqtSignal(str)
    expand_requested = pyqtSignal()

    def __init__(
        self,
        *,
        always_on_top: bool = True,
        sprite_path: Path | None = None,
        sprite_frames: int = 1,
        open_frames: int = 0,
        loop_frames: int = 0,
        close_frames: int = 0,
        sprite_fps: float = 6.0,
        display_width: int | None = None,
        display_height: int | None = None,
    ) -> None:
        super().__init__()
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.headline = "Codex link"
        self.detail = "Watching Codex"
        self.meta = ""
        self.reply_placeholder = "Type a reply to Codex..."
        self.reply_headline = "Reply to Codex"
        self.reply_meta = ""
        self.active = False
        self.waiting_for_user = False
        self.reply_available = False
        self.approval_available = False
        self.reply_mode = False
        self.reply_restore_expanded = False
        self.minimized = False
        self.sprite_sheet = QPixmap(str(sprite_path)) if sprite_path else QPixmap()
        self.sprite_frames = max(1, int(sprite_frames))
        if self.sprite_sheet.isNull():
            self.sprite_frames = 1
            self.sprite_frame_width = 500
            self.sprite_frame_height = 220
        else:
            self.sprite_frame_width = max(1, self.sprite_sheet.width() // self.sprite_frames)
            self.sprite_frame_height = max(1, self.sprite_sheet.height())
        self.open_frames = max(0, min(int(open_frames), self.sprite_frames))
        remaining_frames = max(0, self.sprite_frames - self.open_frames)
        self.close_frames = max(0, min(int(close_frames), remaining_frames))
        remaining_frames = max(1, self.sprite_frames - self.open_frames - self.close_frames)
        self.loop_frames = max(1, int(loop_frames) if loop_frames else remaining_frames)
        self.loop_frames = min(self.loop_frames, remaining_frames)
        self.phase = "hidden"
        self.phase_frame = 0
        self.loop_frame = 0
        self.sprite_fps = max(1.0, float(sprite_fps))
        self.collapsed_size = (display_width or self.sprite_frame_width, display_height or self.sprite_frame_height)
        self.expanded_size = (
            max(self.collapsed_size[0], int(round(self.collapsed_size[0] * 1.42))),
            max(self.collapsed_size[1], int(round(self.collapsed_size[1] * 1.72))),
        )
        self.expanded = False
        self.resize(*self.collapsed_size)
        self.hide()

        self.reply_button = QPushButton("Reply", self)
        self.expand_button = QPushButton("More", self)
        self.detail_view = QTextEdit(self)
        self.reply_edit = BubbleReplyEdit(self)
        self.reply_send_button = QPushButton("Send", self)
        self.reply_cancel_button = QPushButton("Cancel", self)
        self.approval_approve_button = QPushButton("Approve", self)
        self.approval_trust_button = QPushButton("Trust", self)
        self.approval_deny_button = QPushButton("Deny", self)
        self.configure_reply_widgets()

        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self.advance_frame)
        self.animation_timer.start(self.frame_interval(active=False))

    def configure_reply_widgets(self) -> None:
        button_style = """
            QPushButton {
                background: rgba(5, 18, 19, 235);
                border: 1px solid rgba(71, 255, 190, 225);
                border-radius: 6px;
                color: rgb(226, 255, 232);
                font-weight: 700;
                padding: 3px 10px;
            }
            QPushButton:hover {
                background: rgba(13, 48, 44, 245);
                border-color: rgba(123, 255, 204, 255);
            }
            QPushButton:pressed {
                background: rgba(38, 255, 175, 55);
            }
        """
        self.reply_button.setStyleSheet(button_style)
        self.expand_button.setStyleSheet(button_style)
        self.reply_send_button.setStyleSheet(button_style)
        self.reply_cancel_button.setStyleSheet(button_style)
        self.approval_approve_button.setStyleSheet(button_style)
        self.approval_trust_button.setStyleSheet(button_style)
        self.approval_deny_button.setStyleSheet(
            button_style
            + """
            QPushButton {
                border-color: rgba(255, 128, 128, 210);
                color: rgb(255, 224, 224);
            }
            QPushButton:hover {
                border-color: rgba(255, 170, 170, 255);
                background: rgba(72, 24, 26, 230);
            }
            """
        )
        self.reply_edit.setStyleSheet(
            """
            QTextEdit {
                background: rgba(3, 12, 15, 232);
                border: 1px solid rgba(55, 255, 190, 235);
                border-radius: 7px;
                color: rgb(230, 255, 235);
                selection-background-color: rgba(40, 255, 180, 120);
                padding: 8px;
            }
            """
        )
        self.reply_edit.setPlaceholderText(self.reply_placeholder)
        self.reply_edit.setAcceptRichText(False)
        self.detail_view.setReadOnly(True)
        self.detail_view.setAcceptRichText(False)
        self.detail_view.setFrameShape(QTextEdit.Shape.NoFrame)
        self.detail_view.setStyleSheet(
            """
            QTextEdit {
                background: rgba(3, 12, 15, 176);
                border: 1px solid rgba(55, 255, 190, 145);
                border-radius: 7px;
                color: rgb(232, 255, 238);
                selection-background-color: rgba(40, 255, 180, 105);
                padding: 8px;
            }
            QScrollBar:vertical {
                background: rgba(2, 10, 12, 145);
                width: 9px;
                margin: 3px 1px 3px 1px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: rgba(77, 255, 182, 190);
                border-radius: 4px;
                min-height: 28px;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
            }
            """
        )
        self.reply_button.clicked.connect(self.request_reply_from_button)
        self.expand_button.clicked.connect(self.expand_requested.emit)
        self.reply_send_button.clicked.connect(self.submit_reply)
        self.reply_cancel_button.clicked.connect(self.cancel_reply)
        self.approval_approve_button.clicked.connect(lambda: self.approval_requested.emit("approve"))
        self.approval_trust_button.clicked.connect(lambda: self.approval_requested.emit("trust"))
        self.approval_deny_button.clicked.connect(lambda: self.approval_requested.emit("deny"))
        self.approval_approve_button.setToolTip("Approve this Codex command once.")
        self.approval_trust_button.setToolTip("Approve and let Codex trust this command prefix.")
        self.approval_deny_button.setToolTip("Deny this Codex command.")
        self.reply_edit.submitted.connect(self.submit_reply)
        self.reply_edit.cancelled.connect(self.cancel_reply)
        self.layout_reply_widgets()
        self.sync_reply_widgets()

    def frame_interval(self, *, active: bool) -> int:
        fps = self.sprite_fps if active else max(1.0, self.sprite_fps * 0.45)
        return max(33, int(round(1000 / fps)))

    def advance_frame(self) -> None:
        if not self.isVisible() or self.sprite_frames <= 1:
            return
        if self.phase == "opening":
            if self.phase_frame < max(0, self.open_frames - 1):
                self.phase_frame += 1
            else:
                self.phase = "open"
                self.phase_frame = 0
                self.loop_frame = 0
        elif self.phase == "open":
            self.loop_frame = (self.loop_frame + 1) % self.loop_frames
        elif self.phase == "closing":
            if self.phase_frame < max(0, self.close_frames - 1):
                self.phase_frame += 1
            else:
                self.phase = "hidden"
                self.hide()
                self.sync_reply_widgets()
                return
        self.update()

    def begin_opening(self) -> None:
        if self.minimized:
            self.hide()
            self.sync_reply_widgets()
            return
        if self.open_frames > 0:
            self.phase = "opening"
        else:
            self.phase = "open"
        self.phase_frame = 0
        self.loop_frame = 0
        self.show()
        self.sync_reply_widgets()

    def begin_closing(self) -> None:
        if self.reply_mode:
            return
        if self.phase == "hidden":
            self.hide()
            self.sync_reply_widgets()
            return
        if self.close_frames <= 0:
            self.phase = "hidden"
            self.hide()
            self.sync_reply_widgets()
            return
        if self.phase != "closing":
            self.phase = "closing"
            self.phase_frame = 0
        self.sync_reply_widgets()
        self.update()

    def sheet_frame(self) -> int:
        if self.phase == "opening" and self.open_frames > 0:
            return min(self.phase_frame, self.open_frames - 1)
        if self.phase == "closing" and self.close_frames > 0:
            start = self.open_frames + self.loop_frames
            return min(self.sprite_frames - 1, start + self.phase_frame)
        start = self.open_frames
        return min(self.sprite_frames - 1, start + (self.loop_frame % self.loop_frames))

    def content_opacity(self) -> float:
        if self.phase == "opening" and self.open_frames > 1:
            return max(0.0, min(1.0, (self.phase_frame - 1) / max(1, self.open_frames - 2)))
        if self.phase == "closing" and self.close_frames > 1:
            return max(0.0, min(1.0, 1 - (self.phase_frame / max(1, self.close_frames - 1))))
        return 1.0

    def set_status(
        self,
        text: str,
        *,
        active: bool,
        visible: bool = True,
        waiting_for_user: bool = False,
        headline: str | None = None,
        detail: str | None = None,
        meta: str | None = None,
    ) -> None:
        compact_text = " ".join(text.split())
        if not visible or not compact_text:
            if not self.reply_mode:
                self.begin_closing()
                return
            compact_text = self.detail or self.reply_placeholder
        self.headline = " ".join((headline or "Codex").split())
        detail_text = detail if detail is not None else compact_text
        self.detail = self.normalize_detail_text(detail_text)
        self.meta = " ".join((meta or "").split())
        self.update_detail_view_text()
        self.active = active
        self.waiting_for_user = waiting_for_user
        interval = self.frame_interval(active=active or waiting_for_user)
        if self.animation_timer.interval() != interval:
            self.animation_timer.setInterval(interval)
        if self.minimized:
            self.hide()
            self.sync_reply_widgets()
            self.update()
            return
        if self.phase in {"hidden", "closing"}:
            self.begin_opening()
        elif not self.isVisible():
            self.show()
        self.sync_reply_widgets()
        self.update()

    def set_minimized(self, minimized: bool) -> None:
        self.minimized = minimized
        if minimized:
            self.hide()
            self.sync_reply_widgets()
            self.update()
            return
        self.sync_reply_widgets()
        self.update()

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self.resize(*(self.expanded_size if expanded else self.collapsed_size))
        self.layout_reply_widgets()
        self.sync_reply_widgets()
        self.update_detail_view_text(force_scroll=expanded)
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.layout_reply_widgets()

    def set_reply_available(self, available: bool) -> None:
        self.reply_available = available
        self.sync_reply_widgets()

    def set_approval_available(self, available: bool) -> None:
        self.approval_available = available
        self.sync_reply_widgets()

    def set_reply_context(self, *, headline: str, placeholder: str, meta: str = "") -> None:
        self.reply_headline = " ".join(headline.split()) or "Reply"
        self.reply_placeholder = " ".join(placeholder.split()) or "Type a reply..."
        self.reply_meta = " ".join(meta.split())
        self.reply_edit.setPlaceholderText(self.reply_placeholder)
        if self.reply_mode:
            self.headline = self.reply_headline
            self.detail = ""
            self.meta = self.reply_meta
            self.update_detail_view_text()
            self.update()

    def normalize_detail_text(self, text: str) -> str:
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            return ""
        lines: list[str] = []
        blank_count = 0
        for line in raw.split("\n"):
            cleaned = line.rstrip()
            if cleaned.strip():
                blank_count = 0
                lines.append(cleaned)
            else:
                blank_count += 1
                if blank_count <= 1:
                    lines.append("")
        return "\n".join(lines).strip()

    def detail_has_more(self) -> bool:
        if self.expanded:
            return True
        if self.approval_available:
            return False
        return len(self.detail) > 185 or self.detail.count("\n") >= 2

    def update_detail_view_text(self, *, force_scroll: bool = False) -> None:
        if not hasattr(self, "detail_view"):
            return
        text = self.detail
        scroll = self.detail_view.verticalScrollBar()
        near_bottom = scroll.value() >= max(0, scroll.maximum() - 8)
        if self.detail_view.toPlainText() != text:
            self.detail_view.setPlainText(text)
        if force_scroll or near_bottom or self.active:
            scroll.setValue(scroll.maximum())

    def request_reply_from_button(self) -> None:
        self.reply_requested.emit()

    def begin_reply(self, initial_text: str = "") -> None:
        if not self.reply_mode:
            self.reply_restore_expanded = self.expanded
        self.reply_mode = True
        self.reply_available = True
        self.headline = self.reply_headline
        self.detail = ""
        self.meta = self.reply_meta
        self.reply_edit.setPlaceholderText(self.reply_placeholder)
        self.set_minimized(False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.set_expanded(True)
        if initial_text:
            self.reply_edit.setPlainText(initial_text)
        if self.phase in {"hidden", "closing"}:
            self.begin_opening()
        elif not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()
        self.reply_edit.setFocus(Qt.FocusReason.MouseFocusReason)
        self.sync_reply_widgets()

    def finish_reply(self, *, clear: bool = True) -> None:
        self.reply_mode = False
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        if clear:
            self.reply_edit.clear()
        self.set_expanded(self.reply_restore_expanded)
        self.sync_reply_widgets()

    def cancel_reply(self) -> None:
        self.finish_reply()
        self.reply_cancelled.emit()

    def submit_reply(self) -> None:
        text = self.reply_edit.toPlainText().strip()
        if not text:
            self.reply_edit.setFocus(Qt.FocusReason.MouseFocusReason)
            return
        self.reply_submitted.emit(text)

    def layout_reply_widgets(self) -> None:
        sx = max(0.75, self.width() / max(1, self.sprite_frame_width))
        sy = max(0.75, self.height() / max(1, self.sprite_frame_height))
        button_width = max(68, int(round(72 * sx)))
        button_height = max(24, int(round(24 * sy)))
        self.reply_button.setGeometry(
            self.width() - int(round(118 * sx)),
            int(round(69 * sy)),
            button_width,
            button_height,
        )
        expand_width = max(68, int(round(76 * sx)))
        expand_x = self.width() - int(round(118 * sx)) - expand_width - int(round(8 * sx))
        self.expand_button.setGeometry(expand_x, int(round(69 * sy)), expand_width, button_height)
        left = int(round(84 * sx))
        right = self.width() - int(round(72 * sx))
        top = int(round(112 * sy))
        edit_height = int(round(78 * sy))
        self.reply_edit.setGeometry(left, top, max(180, right - left), edit_height)
        control_y = top + edit_height + int(round(8 * sy))
        control_width = max(76, int(round(86 * sx)))
        gap = int(round(8 * sx))
        self.reply_send_button.setGeometry(right - control_width, control_y, control_width, button_height)
        self.reply_cancel_button.setGeometry(
            right - control_width * 2 - gap,
            control_y,
            control_width,
            button_height,
        )
        approval_y = int(round(159 * sy))
        deny_width = max(58, int(round(62 * sx)))
        trust_width = max(62, int(round(66 * sx)))
        approve_width = max(82, int(round(92 * sx)))
        approval_gap = int(round(7 * sx))
        approval_right = self.width() - int(round(72 * sx))
        self.approval_deny_button.setGeometry(
            approval_right - deny_width,
            approval_y,
            deny_width,
            button_height,
        )
        self.approval_trust_button.setGeometry(
            approval_right - deny_width - approval_gap - trust_width,
            approval_y,
            trust_width,
            button_height,
        )
        self.approval_approve_button.setGeometry(
            approval_right - deny_width - approval_gap - trust_width - approval_gap - approve_width,
            approval_y,
            approve_width,
            button_height,
        )
        reader_top = int(round(102 * sy))
        reader_bottom = self.height() - int(round(38 * sy))
        if self.meta:
            reader_bottom -= int(round(24 * sy))
        if self.approval_available:
            reader_bottom = min(reader_bottom, approval_y - int(round(9 * sy)))
        self.detail_view.setGeometry(
            left,
            reader_top,
            max(180, right - left),
            max(46, reader_bottom - reader_top),
        )

    def sync_reply_widgets(self) -> None:
        visible = self.isVisible() and self.phase != "hidden"
        show_reader = visible and self.expanded and not self.reply_mode
        self.detail_view.setVisible(show_reader)
        self.expand_button.setText("Less" if self.expanded else "More")
        self.expand_button.setVisible(visible and not self.reply_mode and self.detail_has_more())
        self.reply_button.setVisible(visible and self.reply_available and not self.reply_mode)
        self.reply_edit.setVisible(visible and self.reply_mode)
        self.reply_send_button.setVisible(visible and self.reply_mode)
        self.reply_cancel_button.setVisible(visible and self.reply_mode)
        show_approval = visible and self.approval_available and not self.reply_mode
        self.approval_approve_button.setVisible(show_approval)
        self.approval_trust_button.setVisible(show_approval)
        self.approval_deny_button.setVisible(show_approval)
        if show_reader:
            self.detail_view.raise_()
        if self.expand_button.isVisible():
            self.expand_button.raise_()
        if self.reply_button.isVisible():
            self.reply_button.raise_()

    def cloud_path(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(QRectF(54, 40, self.width() - 108, self.height() - 94), 30, 30)
        path.addEllipse(QRectF(20, 70, 82, 72))
        path.addEllipse(QRectF(72, 20, 120, 82))
        path.addEllipse(QRectF(176, 14, 142, 90))
        path.addEllipse(QRectF(304, 28, 118, 78))
        path.addEllipse(QRectF(self.width() - 104, 68, 84, 72))
        path.addEllipse(QRectF(106, self.height() - 92, 128, 58))
        path.addEllipse(QRectF(226, self.height() - 96, 144, 62))
        return path

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if not self.sprite_sheet.isNull():
            self.paint_sprite_bubble(painter)
            return

        self.paint_fallback_bubble(painter)

    def paint_sprite_bubble(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        frame = self.sheet_frame()
        source_rect = QRect(
            frame * self.sprite_frame_width,
            0,
            self.sprite_frame_width,
            self.sprite_frame_height,
        )
        if not (self.active or self.waiting_for_user):
            painter.setOpacity(0.86)
        painter.drawPixmap(self.rect(), self.sprite_sheet, source_rect)
        painter.setOpacity(1.0)

        sx = self.width() / self.sprite_frame_width
        sy = self.height() / self.sprite_frame_height

        def rect(x: float, y: float, width: float, height: float) -> QRectF:
            return QRectF(x * sx, y * sy, width * sx, height * sy)

        if self.waiting_for_user:
            dot = QColor(255, 226, 109)
        elif self.active:
            dot = QColor(92, 255, 146)
        else:
            dot = QColor(99, 130, 138)
        text_opacity = self.content_opacity()
        if text_opacity <= 0.02:
            return
        painter.setOpacity(text_opacity)
        painter.setBrush(dot)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(rect(76, 79, 12, 12))

        title_font = QFont()
        title_font.setPointSize(max(8, int(round(10 * sy))))
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(222, 255, 226))
        painter.drawText(rect(98, 74, 390, 25), self.headline)

        if self.reply_mode:
            painter.setOpacity(1.0)
            return
        if self.expanded:
            if self.meta:
                meta_font = QFont()
                meta_font.setPointSize(max(7, int(round(8 * sy))))
                painter.setFont(meta_font)
                painter.setPen(QColor(151, 238, 191))
                option = QTextOption()
                option.setWrapMode(QTextOption.WrapMode.WordWrap)
                painter.drawText(rect(84, 183, 404, 24), self.meta, option)
            painter.setOpacity(1.0)
            return

        text_font = QFont()
        text_font.setPointSize(max(8, int(round(10 * sy))))
        painter.setFont(text_font)
        painter.setPen(QColor(232, 255, 238))
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        detail_height = 58 if self.approval_available else 82
        painter.drawText(rect(84, 102, 404, detail_height), self.detail, option)

        if self.meta:
            meta_font = QFont()
            meta_font.setPointSize(max(7, int(round(8 * sy))))
            painter.setFont(meta_font)
            painter.setPen(QColor(151, 238, 191))
            painter.drawText(rect(84, 183, 404, 24), self.meta, option)
        painter.setOpacity(1.0)

    def paint_fallback_bubble(self, painter: QPainter) -> None:
        path = self.cloud_path()
        glow = QColor(67, 255, 125, 58 if self.active else 34)
        painter.setPen(QPen(glow, 8))
        painter.drawPath(path)

        painter.fillPath(path, QColor(8, 13, 17, 235))
        painter.setPen(QPen(QColor(85, 255, 139, 150), 2.2))
        painter.drawPath(path)
        painter.setPen(QPen(QColor(0, 226, 255, 82), 1.1))
        painter.drawPath(path)

        panel = QRectF(40, 54, self.width() - 80, self.height() - 108)
        panel_path = QPainterPath()
        panel_path.addRoundedRect(panel, 15, 15)
        painter.fillPath(panel_path, QColor(5, 9, 12, 224))
        painter.setPen(QPen(QColor(67, 255, 131, 215), 1.8))
        painter.drawPath(panel_path)
        painter.setPen(QPen(QColor(0, 227, 255, 90), 1.0))
        painter.drawRoundedRect(panel, 15, 15)

        painter.save()
        painter.setClipPath(panel_path)
        painter.setPen(QPen(QColor(72, 255, 142, 24), 1))
        for x in range(52, self.width() - 48, 36):
            painter.drawLine(x, int(panel.top()), x, int(panel.bottom()))
        for y in range(int(panel.top()) + 22, int(panel.bottom()), 26):
            painter.drawLine(int(panel.left()), y, int(panel.right()), y)
        painter.setPen(QPen(QColor(0, 229, 255, 80), 1.4))
        painter.drawLine(self.width() - 118, 68, self.width() - 78, 68)
        painter.drawLine(self.width() - 78, 68, self.width() - 78, 92)
        painter.drawLine(62, self.height() - 92, 108, self.height() - 92)
        painter.drawLine(108, self.height() - 92, 108, self.height() - 72)
        painter.restore()

        painter.setPen(QPen(QColor(86, 255, 151, 210), 2))
        painter.setBrush(QColor(12, 21, 24, 234))
        painter.drawEllipse(QRectF(self.width() / 2 - 13, self.height() - 42, 26, 18))
        painter.setPen(QPen(QColor(0, 226, 255, 160), 1.4))
        painter.drawEllipse(QRectF(self.width() / 2 + 18, self.height() - 26, 14, 10))
        painter.drawEllipse(QRectF(self.width() / 2 + 38, self.height() - 15, 8, 6))

        dot = QColor(87, 255, 135) if self.active else QColor(111, 133, 145)
        painter.setBrush(dot)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(56, 66, 10, 10)

        title_font = QFont()
        title_font.setPointSize(9)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(214, 255, 223))
        painter.drawText(QRectF(74, 59, self.width() - 148, 24), self.headline)

        if self.reply_mode:
            return
        if self.expanded:
            if self.meta:
                meta_font = QFont()
                meta_font.setPointSize(8)
                painter.setFont(meta_font)
                painter.setPen(QColor(151, 238, 191))
                option = QTextOption()
                option.setWrapMode(QTextOption.WrapMode.WordWrap)
                painter.drawText(QRectF(56, 171, self.width() - 112, 22), self.meta, option)
            return

        text_font = QFont()
        text_font.setPointSize(10)
        painter.setFont(text_font)
        painter.setPen(QColor(232, 255, 238))
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        detail_height = 62 if self.approval_available else 84
        painter.drawText(QRectF(56, 86, self.width() - 112, detail_height), self.detail, option)

        if self.meta:
            meta_font = QFont()
            meta_font.setPointSize(8)
            painter.setFont(meta_font)
            painter.setPen(QColor(151, 238, 191))
            painter.drawText(QRectF(56, 171, self.width() - 112, 22), self.meta, option)


class WorkDropBubble(QWidget):
    submitted = pyqtSignal(object)
    cancelled = pyqtSignal()

    def __init__(
        self,
        *,
        always_on_top: bool = True,
        sprite_path: Path | None = None,
        sprite_frames: int = 1,
        open_frames: int = 0,
        loop_frames: int = 0,
        close_frames: int = 0,
        sprite_fps: float = 6.0,
        display_width: int = 640,
        display_height: int = 360,
    ) -> None:
        super().__init__()
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.paths: tuple[Path, ...] = ()
        self.cwd_path = Path.home()
        self.action_value = ACTION_SUMMARIZE
        self.sandbox_value = "read-only"
        self.provider_value = AI_PROVIDER_CODEX
        self.sprite_sheet = QPixmap(str(sprite_path)) if sprite_path else QPixmap()
        self.sprite_frames = max(1, int(sprite_frames))
        if self.sprite_sheet.isNull():
            self.sprite_frames = 1
            self.sprite_frame_width = display_width
            self.sprite_frame_height = display_height
        else:
            self.sprite_frame_width = max(1, self.sprite_sheet.width() // self.sprite_frames)
            self.sprite_frame_height = max(1, self.sprite_sheet.height())
        self.open_frames = max(0, min(int(open_frames), self.sprite_frames))
        remaining = max(0, self.sprite_frames - self.open_frames)
        self.close_frames = max(0, min(int(close_frames), remaining))
        remaining = max(1, self.sprite_frames - self.open_frames - self.close_frames)
        self.loop_frames = max(1, int(loop_frames) if loop_frames else remaining)
        self.loop_frames = min(self.loop_frames, remaining)
        self.sprite_fps = max(1.0, float(sprite_fps))
        self.phase = "hidden"
        self.phase_frame = 0
        self.loop_frame = 0
        self.resize(display_width, display_height)
        self.hide()

        self.path_label = QLabel(self)
        self.cwd_label = QLabel(self)
        self.browse_button = QPushButton("Change", self)
        self.prompt = QTextEdit(self)
        self.run_button = QPushButton("Run", self)
        self.cancel_button = QPushButton("Cancel", self)
        self.action_buttons: dict[str, QPushButton] = {}
        self.sandbox_buttons: dict[str, QPushButton] = {}
        self.provider_buttons: dict[str, QPushButton] = {}
        self.configure_widgets()

        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self.advance_frame)
        self.animation_timer.start(max(33, int(round(1000 / self.sprite_fps))))

    def configure_widgets(self) -> None:
        label_style = """
            QLabel {
                background: rgba(4, 18, 20, 155);
                border: 1px solid rgba(74, 255, 190, 130);
                border-radius: 8px;
                color: rgb(229, 255, 235);
                padding: 6px 8px;
            }
        """
        notes_style = """
            QTextEdit {
                background: rgba(2, 12, 14, 135);
                border: 1px solid rgba(74, 255, 190, 110);
                border-radius: 8px;
                color: rgb(230, 255, 235);
                selection-background-color: rgba(40, 255, 180, 120);
                padding: 7px;
            }
            QTextEdit:focus {
                border-color: rgba(147, 255, 206, 205);
                background: rgba(2, 12, 14, 180);
            }
        """
        button_style = """
            QPushButton {
                background: rgba(5, 22, 20, 120);
                border: 1px solid rgba(71, 255, 190, 130);
                border-radius: 8px;
                color: rgb(221, 255, 231);
                font-weight: 700;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background: rgba(21, 58, 50, 190);
                border-color: rgba(123, 255, 204, 220);
            }
            QPushButton:pressed {
                background: rgba(38, 255, 175, 55);
            }
        """
        chip_style = """
            QPushButton {
                background: rgba(4, 17, 19, 105);
                border: 1px solid rgba(75, 255, 190, 95);
                border-radius: 10px;
                color: rgb(198, 244, 213);
                font-weight: 700;
                padding: 5px 8px;
            }
            QPushButton:hover {
                border-color: rgba(129, 255, 207, 185);
                background: rgba(16, 44, 40, 155);
            }
            QPushButton:checked {
                background: rgba(74, 255, 168, 44);
                border-color: rgba(154, 255, 207, 235);
                color: rgb(237, 255, 238);
            }
        """
        self.path_label.setWordWrap(True)
        self.cwd_label.setWordWrap(False)
        self.path_label.setStyleSheet(label_style)
        self.cwd_label.setStyleSheet(label_style)
        self.prompt.setStyleSheet(notes_style)
        for button in (self.browse_button, self.run_button, self.cancel_button):
            button.setStyleSheet(button_style)
        for key in (ACTION_SUMMARIZE, ACTION_EXPLAIN, ACTION_REVIEW, ACTION_CUSTOM):
            button = QPushButton("Custom" if key == ACTION_CUSTOM else ACTION_LABELS[key], self)
            button.setToolTip(ACTION_LABELS[key])
            button.setCheckable(True)
            button.setStyleSheet(chip_style)
            button.clicked.connect(lambda _checked=False, action=key: self.set_action(action))
            self.action_buttons[key] = button
        for sandbox in SAFE_SANDBOXES:
            button = QPushButton(sandbox, self)
            button.setCheckable(True)
            button.setStyleSheet(chip_style)
            button.clicked.connect(lambda _checked=False, value=sandbox: self.set_sandbox(value))
            self.sandbox_buttons[sandbox] = button
        for provider in AI_PROVIDERS:
            label = "Codex" if provider == AI_PROVIDER_CODEX else "Claude"
            button = QPushButton(label, self)
            button.setCheckable(True)
            button.setStyleSheet(chip_style)
            button.clicked.connect(lambda _checked=False, value=provider: self.set_provider(value))
            self.provider_buttons[provider] = button
        self.browse_button.clicked.connect(self.choose_cwd)
        self.run_button.clicked.connect(self.submit)
        self.cancel_button.clicked.connect(self.cancel)
        self.layout_widgets()
        self.update_chip_state()
        self.update_prompt_state()

    def layout_widgets(self) -> None:
        margin_x = 38
        top = 84
        width = self.width() - margin_x * 2
        self.path_label.setGeometry(margin_x, top, width, 58)
        row_y = top + 74
        chip_gap = 8
        chip_w = (width - chip_gap * 3) // 4
        for index, key in enumerate((ACTION_SUMMARIZE, ACTION_EXPLAIN, ACTION_REVIEW, ACTION_CUSTOM)):
            self.action_buttons[key].setGeometry(margin_x + index * (chip_w + chip_gap), row_y, chip_w, 32)
        row_y += 46
        provider_w = 92
        for index, provider in enumerate(AI_PROVIDERS):
            self.provider_buttons[provider].setGeometry(margin_x + index * (provider_w + chip_gap), row_y, provider_w, 30)
        row_y += 36
        sandbox_w = 118
        for index, sandbox in enumerate(SAFE_SANDBOXES):
            self.sandbox_buttons[sandbox].setGeometry(margin_x + index * (sandbox_w + chip_gap), row_y, sandbox_w, 30)
        self.cwd_label.setGeometry(margin_x + sandbox_w * 2 + chip_gap * 2 + 8, row_y, width - sandbox_w * 2 - chip_gap * 3 - 88, 30)
        self.browse_button.setGeometry(self.width() - margin_x - 78, row_y, 78, 30)
        row_y += 36
        self.prompt.setGeometry(margin_x, row_y, width, 36)
        button_y = self.height() - 44
        button_w = 82
        gap = 10
        self.cancel_button.setGeometry(self.width() - margin_x - button_w * 2 - gap, button_y, button_w, 34)
        self.run_button.setGeometry(self.width() - margin_x - button_w, button_y, button_w, 34)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.layout_widgets()

    def set_request(
        self,
        *,
        paths: list[Path],
        fallback_cwd: Path,
        default_sandbox: str,
        default_provider: str = AI_PROVIDER_CODEX,
    ) -> None:
        self.paths = tuple(path.expanduser().resolve() for path in paths)
        default_cwd = default_cwd_for_paths(self.paths, fallback_cwd)
        self.cwd_path = default_cwd
        self.path_label.setText(self.path_summary())
        self.path_label.setToolTip("\n".join(str(path) for path in self.paths))
        self.cwd_label.setText(self.compact_path(self.cwd_path))
        self.cwd_label.setToolTip(str(self.cwd_path))
        self.action_value = ACTION_SUMMARIZE
        self.sandbox_value = default_sandbox if default_sandbox in SAFE_SANDBOXES else "read-only"
        self.provider_value = default_provider if default_provider in AI_PROVIDERS else AI_PROVIDER_CODEX
        self.prompt.clear()
        self.update_chip_state()
        self.update_prompt_state()

    def path_summary(self) -> str:
        if not self.paths:
            return "No dropped paths."
        if len(self.paths) == 1:
            path = self.paths[0]
            return f"{path.name}\n{path}"
        names = ", ".join(path.name for path in self.paths[:3])
        if len(self.paths) > 3:
            names += f", +{len(self.paths) - 3} more"
        return f"{len(self.paths)} dropped items\n{names}"

    def compact_path(self, path: Path) -> str:
        text = str(path)
        if len(text) <= 42:
            return text
        return "..." + text[-39:]

    def set_action(self, action: str) -> None:
        self.action_value = action
        self.update_chip_state()
        self.update_prompt_state()

    def set_sandbox(self, sandbox: str) -> None:
        self.sandbox_value = sandbox if sandbox in SAFE_SANDBOXES else "read-only"
        self.update_chip_state()

    def set_provider(self, provider: str) -> None:
        self.provider_value = provider if provider in AI_PROVIDERS else AI_PROVIDER_CODEX
        self.update_chip_state()
        self.update_prompt_state()

    def update_chip_state(self) -> None:
        for key, button in self.action_buttons.items():
            button.setChecked(key == self.action_value)
        for key, button in self.sandbox_buttons.items():
            button.setChecked(key == self.sandbox_value)
        for key, button in self.provider_buttons.items():
            button.setChecked(key == self.provider_value)

    def update_prompt_state(self) -> None:
        is_custom = self.action_value == ACTION_CUSTOM
        self.prompt.setPlaceholderText(
            f"Type the exact request for {self.provider_label()}..."
            if is_custom
            else "Optional note for this run..."
        )

    def provider_label(self) -> str:
        return "Claude" if self.provider_value == AI_PROVIDER_CLAUDE else "Codex"

    def choose_cwd(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose working folder", str(self.cwd_path))
        if selected:
            self.cwd_path = Path(selected).expanduser().resolve()
            self.cwd_label.setText(self.compact_path(self.cwd_path))
            self.cwd_label.setToolTip(str(self.cwd_path))

    def submit(self) -> None:
        cwd = self.cwd_path.expanduser()
        if not cwd.exists() or not cwd.is_dir():
            QMessageBox.warning(self, "Companion Work Drop", "Choose an existing working folder.")
            return
        action = self.action_value
        prompt = self.prompt.toPlainText().strip()
        if action == ACTION_CUSTOM and not prompt:
            QMessageBox.warning(self, "Companion Work Drop", "Type a custom request before running.")
            return
        sandbox = self.sandbox_value
        if sandbox == "workspace-write" and not self.confirm_workspace_write():
            return
        self.submitted.emit(
            WorkRequest(
                action=action,
                prompt=prompt,
                paths=self.paths,
                cwd=cwd,
                sandbox=sandbox,
                provider=self.provider_value,
            )
        )

    def confirm_workspace_write(self) -> bool:
        answer = QMessageBox.question(
            self,
            "Confirm workspace-write",
            "workspace-write allows Codex to edit files in the selected working folder. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def cancel(self) -> None:
        self.cancelled.emit()

    def begin_opening(self) -> None:
        self.phase = "opening" if self.open_frames > 0 else "open"
        self.phase_frame = 0
        self.loop_frame = 0
        self.show()
        self.raise_()
        self.activateWindow()
        self.prompt.setFocus(Qt.FocusReason.MouseFocusReason)
        self.update()

    def begin_closing(self) -> None:
        if self.phase == "hidden":
            self.hide()
            return
        if self.close_frames <= 0:
            self.phase = "hidden"
            self.hide()
            return
        self.phase = "closing"
        self.phase_frame = 0
        self.update()

    def advance_frame(self) -> None:
        if not self.isVisible() or self.sprite_frames <= 1:
            return
        if self.phase == "opening":
            if self.phase_frame < max(0, self.open_frames - 1):
                self.phase_frame += 1
            else:
                self.phase = "open"
                self.phase_frame = 0
                self.loop_frame = 0
        elif self.phase == "open":
            self.loop_frame = (self.loop_frame + 1) % self.loop_frames
        elif self.phase == "closing":
            if self.phase_frame < max(0, self.close_frames - 1):
                self.phase_frame += 1
            else:
                self.phase = "hidden"
                self.hide()
                return
        self.update()

    def sheet_frame(self) -> int:
        if self.phase == "opening" and self.open_frames > 0:
            return min(self.phase_frame, self.open_frames - 1)
        if self.phase == "closing" and self.close_frames > 0:
            start = self.open_frames + self.loop_frames
            return min(self.sprite_frames - 1, start + self.phase_frame)
        start = self.open_frames
        return min(self.sprite_frames - 1, start + (self.loop_frame % self.loop_frames))

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        panel = QPainterPath()
        panel.addRoundedRect(QRectF(18, 16, self.width() - 36, self.height() - 42), 24, 24)
        panel.addEllipse(QRectF(self.width() * 0.48, self.height() - 34, 22, 14))
        panel.addEllipse(QRectF(self.width() * 0.52, self.height() - 19, 12, 8))
        painter.fillPath(panel, QColor(4, 12, 15, 224))
        painter.setPen(QPen(QColor(70, 255, 180, 105), 2.0))
        painter.drawPath(panel)
        painter.setPen(QPen(QColor(140, 255, 202, 54), 6.0))
        painter.drawPath(panel)

        painter.setPen(QColor(229, 255, 235))
        title_font = QFont()
        title_font.setPointSize(13)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.drawText(QRectF(38, 30, self.width() - 76, 24), "Companion received a drop")

        subtitle_font = QFont()
        subtitle_font.setPointSize(8)
        painter.setFont(subtitle_font)
        painter.setPen(QColor(151, 238, 191))
        painter.drawText(QRectF(38, 55, self.width() - 76, 20), "Confirm the target and choose the first Codex action.")


class UsageMeterOverlay(QWidget):
    def __init__(
        self,
        *,
        always_on_top: bool = True,
        sprite_path: Path | None = None,
        sprite_frames: int = 1,
        open_frames: int = 0,
        loop_frames: int = 0,
        close_frames: int = 0,
        sprite_fps: float = 6.0,
        base_frame_path: Path | None = None,
        open_seconds: float | None = None,
        close_seconds: float | None = None,
        display_width: int | None = None,
        display_height: int | None = None,
    ) -> None:
        super().__init__()
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.sprite_sheet = QPixmap(str(sprite_path)) if sprite_path else QPixmap()
        self.sprite_frames = max(1, int(sprite_frames))
        if self.sprite_sheet.isNull():
            self.sprite_frames = 1
            self.sprite_frame_width = 620
            self.sprite_frame_height = 420
        else:
            self.sprite_frame_width = max(1, self.sprite_sheet.width() // self.sprite_frames)
            self.sprite_frame_height = max(1, self.sprite_sheet.height())
        self.open_frames = max(0, min(int(open_frames), self.sprite_frames))
        remaining = max(0, self.sprite_frames - self.open_frames)
        self.close_frames = max(0, min(int(close_frames), remaining))
        remaining = max(1, self.sprite_frames - self.open_frames - self.close_frames)
        self.loop_frames = max(1, int(loop_frames) if loop_frames else remaining)
        self.loop_frames = min(self.loop_frames, remaining)
        self.art_frame = QPixmap(str(base_frame_path)) if base_frame_path else QPixmap()
        if self.art_frame.isNull() and not self.sprite_sheet.isNull():
            frame_index = min(self.sprite_frames - 1, self.open_frames + min(2, self.loop_frames - 1))
            self.art_frame = self.sprite_sheet.copy(
                frame_index * self.sprite_frame_width,
                0,
                self.sprite_frame_width,
                self.sprite_frame_height,
            )
        if not self.art_frame.isNull():
            self.sprite_frame_width = self.art_frame.width()
            self.sprite_frame_height = self.art_frame.height()
        self.phase = "hidden"
        self.phase_frame = 0
        self.loop_frame = 0
        self.usage = CodexUsageStatus(False, reason="Usage has not been checked yet.")
        self.sprite_fps = max(1.0, float(sprite_fps))
        open_duration = float(open_seconds) if open_seconds is not None else max(0.9, self.open_frames / self.sprite_fps)
        close_duration = (
            float(close_seconds) if close_seconds is not None else max(0.9, self.close_frames / self.sprite_fps)
        )
        self.open_ticks = max(1, int(round(open_duration * 1000 / 33)))
        self.close_ticks = max(1, int(round(close_duration * 1000 / 33)))
        self.resize(display_width or self.sprite_frame_width, display_height or self.sprite_frame_height)
        self.hide()

        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self.advance_frame)
        self.animation_timer.start(33)
        self.close_timer = QTimer(self)
        self.close_timer.setSingleShot(True)
        self.close_timer.timeout.connect(self.begin_closing)

    def advance_frame(self) -> None:
        if not self.isVisible():
            return
        if self.phase == "opening":
            if self.phase_frame < self.open_ticks:
                self.phase_frame += 1
            else:
                self.phase = "open"
                self.phase_frame = 0
                self.loop_frame = 0
        elif self.phase == "open":
            self.loop_frame = (self.loop_frame + 1) % max(1, self.loop_frames * 8)
        elif self.phase == "closing":
            if self.phase_frame < self.close_ticks:
                self.phase_frame += 1
            else:
                self.phase = "hidden"
                self.hide()
                return
        self.update()

    def begin_opening(self) -> None:
        self.phase = "opening" if self.open_frames > 0 else "open"
        self.phase_frame = 0
        self.loop_frame = 0
        self.show()

    def begin_closing(self) -> None:
        if self.phase == "hidden":
            self.hide()
            return
        if self.close_frames <= 0:
            self.phase = "hidden"
            self.hide()
            return
        if self.phase != "closing":
            self.phase = "closing"
            self.phase_frame = 0
        self.update()

    def sheet_frame(self) -> int:
        if self.phase == "opening" and self.open_frames > 0:
            return min(self.phase_frame, self.open_frames - 1)
        if self.phase == "closing" and self.close_frames > 0:
            return min(self.sprite_frames - 1, self.open_frames + self.loop_frames + self.phase_frame)
        return min(self.sprite_frames - 1, self.open_frames + (self.loop_frame % self.loop_frames))

    def content_opacity(self) -> float:
        return self.sign_progress()

    def phase_ratio(self, ticks: int) -> float:
        return max(0.0, min(1.0, self.phase_frame / max(1, ticks)))

    def ease(self, value: float) -> float:
        value = max(0.0, min(1.0, value))
        return value * value * (3 - 2 * value)

    def portal_progress(self) -> float:
        if self.phase == "hidden":
            return 0.0
        if self.phase == "opening":
            return self.ease(self.phase_ratio(self.open_ticks) / 0.34)
        if self.phase == "closing":
            closing = self.phase_ratio(self.close_ticks)
            return 1.0 - self.ease((closing - 0.70) / 0.30)
        return 1.0

    def sign_progress(self) -> float:
        if self.phase == "hidden":
            return 0.0
        if self.phase == "opening":
            opening = self.phase_ratio(self.open_ticks)
            return self.ease((opening - 0.26) / 0.74)
        if self.phase == "closing":
            closing = self.phase_ratio(self.close_ticks)
            return 1.0 - self.ease(closing / 0.72)
        return 1.0

    def show_usage(self, usage: CodexUsageStatus, *, hold_ms: int) -> None:
        self.usage = usage
        if self.phase in {"hidden", "closing"}:
            self.begin_opening()
        elif not self.isVisible():
            self.show()
        self.close_timer.start(max(1000, int(hold_ms)))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if not self.art_frame.isNull():
            self.paint_choreographed_usage(painter)
            return

        if not self.sprite_sheet.isNull():
            frame = self.sheet_frame()
            source_rect = QRect(
                frame * self.sprite_frame_width,
                0,
                self.sprite_frame_width,
                self.sprite_frame_height,
            )
            painter.drawPixmap(self.rect(), self.sprite_sheet, source_rect)
        if self.content_opacity() <= 0.02:
            return
        painter.setOpacity(self.content_opacity())
        self.paint_usage_text(painter)
        painter.setOpacity(1.0)

    def paint_choreographed_usage(self, painter: QPainter) -> None:
        sx = self.width() / max(1, self.sprite_frame_width)
        sy = self.height() / max(1, self.sprite_frame_height)
        painter.save()
        painter.scale(sx, sy)

        portal = self.portal_progress()
        sign = self.sign_progress()
        width = float(self.sprite_frame_width)
        height = float(self.sprite_frame_height)
        portal_top = height * 0.70
        portal_source = QRectF(0, portal_top, width, height - portal_top)
        portal_bottom = height - 3
        portal_scale_x = (0.24 + 0.76 * portal) * (1.0 + 0.018 * math.sin(self.loop_frame * 0.55))
        portal_scale_y = 0.44 + 0.56 * portal
        portal_width = width * portal_scale_x
        portal_height = (height - portal_top) * portal_scale_y
        portal_dest = QRectF((width - portal_width) / 2, portal_bottom - portal_height, portal_width, portal_height)

        sign_bottom = height * 0.76
        sign_offset = (1.0 - sign) * height * 0.13
        visible_height = sign_bottom * sign
        clip_top = sign_bottom - visible_height

        if sign > 0.01:
            painter.save()
            painter.setClipRect(QRectF(0, clip_top, width, visible_height + 2))
            painter.setOpacity(0.12 + 0.88 * sign)
            painter.drawPixmap(
                QRectF(0, sign_offset, width, sign_bottom),
                self.art_frame,
                QRectF(0, 0, width, sign_bottom),
            )
            text = self.ease((sign - 0.50) / 0.50)
            if text > 0.01:
                painter.setOpacity(text)
                painter.translate(0, sign_offset)
                self.paint_usage_text(painter, source_space=True)
            painter.restore()

        if portal > 0.01:
            painter.save()
            painter.setOpacity(portal)
            painter.drawPixmap(portal_dest, self.art_frame, portal_source)
            painter.restore()

        painter.restore()

    def paint_usage_text(self, painter: QPainter, *, source_space: bool = False) -> None:
        sx = 1.0 if source_space else self.width() / self.sprite_frame_width
        sy = 1.0 if source_space else self.height() / self.sprite_frame_height

        def rect(x: float, y: float, width: float, height: float) -> QRectF:
            return QRectF(x * sx, y * sy, width * sx, height * sy)

        title_font = QFont()
        title_font.setPointSize(max(9, int(round(13 * sy))))
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(226, 255, 230))
        plan = f" - {self.usage.plan_type.upper()}" if self.usage.available and self.usage.plan_type else ""
        painter.drawText(rect(92, 78, 250, 28), "Codex limits" + plan)

        if not self.usage.available:
            detail_font = QFont()
            detail_font.setPointSize(max(8, int(round(11 * sy))))
            painter.setFont(detail_font)
            painter.setPen(QColor(232, 255, 238))
            painter.drawText(rect(92, 120, 440, 34), "Local usage data is not exposed yet.")
            painter.setPen(QColor(151, 238, 191))
            painter.drawText(rect(92, 154, 440, 28), self.usage.reason or "Waiting for a token-count event.")
            return

        credit_text = "Credits n/a"
        if self.usage.credits:
            if self.usage.credits.unlimited:
                credit_text = "Credits unlimited"
            elif self.usage.credits.balance:
                credit_text = "Credits " + self.format_credits(self.usage.credits.balance)
        credit_font = QFont()
        credit_font.setPointSize(max(8, int(round(10 * sy))))
        credit_font.setBold(True)
        painter.setFont(credit_font)
        painter.setPen(QColor(214, 255, 223))
        painter.drawText(
            rect(346, 80, 176, 24),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            credit_text,
        )

        self.paint_window(painter, rect, 112, "5h", self.usage.primary)
        self.paint_window(painter, rect, 178, "Weekly", self.usage.secondary)

    def paint_window(self, painter: QPainter, rect, y: int, label: str, window: UsageWindow | None) -> None:
        title_font = QFont()
        title_font.setPointSize(10)
        title_font.setBold(True)
        painter.setFont(title_font)
        if window is None or window.left_percent is None:
            painter.setPen(QColor(232, 255, 238))
            painter.drawText(rect(92, y, 430, 24), f"{label}: not reported")
            return

        left = window.left_percent
        painter.setPen(QColor(232, 255, 238))
        painter.drawText(rect(92, y, 210, 24), f"{label}: {left:.0f}% left")
        painter.setPen(QColor(151, 238, 191))
        painter.drawText(rect(350, y, 180, 24), self.reset_text(window))

        bar = rect(92, y + 29, 430, 18)
        painter.setPen(QPen(QColor(0, 229, 255, 160), 1.2))
        painter.setBrush(QColor(4, 10, 13, 210))
        painter.drawRoundedRect(bar, 7, 7)
        fill = QRectF(bar.left() + 3, bar.top() + 3, max(0.0, (bar.width() - 6) * (left / 100)), bar.height() - 6)
        color = QColor(91, 255, 132) if left > 35 else QColor(255, 214, 96) if left > 12 else QColor(255, 96, 116)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(fill, 5, 5)

    def reset_text(self, window: UsageWindow) -> str:
        if not window.resets_at:
            return ""
        remaining = max(0, int(window.resets_at - time.time()))
        days, remainder = divmod(remaining, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        if days:
            return f"resets {days}d {hours}h"
        if hours:
            return f"resets {hours}h {minutes}m"
        return f"resets {minutes}m"

    def format_credits(self, value: str) -> str:
        try:
            return f"{float(value):.2f}"
        except ValueError:
            return value


class DesktopPet(QWidget):
    def __init__(
        self,
        pet_dir: Path,
        *,
        scale: float | None = None,
        speed: float | None = None,
        codex_session: str | None = None,
        always_on_top: bool = True,
    ) -> None:
        super().__init__()
        self.pet_dir = pet_dir
        self.repo_root = Path(__file__).resolve().parents[1]
        self.manifest = json.loads((pet_dir / "pet.json").read_text(encoding="utf-8"))
        self.pet_id = str(self.manifest.get("id") or pet_dir.name).strip() or pet_dir.name
        self.pet_name = str(self.manifest.get("name") or self.pet_id).strip() or self.pet_id
        self.cell_width = int(self.manifest["cellWidth"])
        self.cell_height = int(self.manifest["cellHeight"])
        self.frame_count = int(self.manifest["frameCount"])
        self.animations = self.manifest["animations"]
        runtime = self.manifest.get("runtime", {})
        self.scale = float(scale or runtime.get("defaultScale", 1.0))
        self.speed = float(speed or runtime.get("walkSpeed", 2.2))
        self.ground_padding = int(runtime.get("groundPadding", 24))
        drag_anchor = runtime.get("dragAnchor", {})
        self.drag_anchor_x_ratio = float(drag_anchor.get("xRatio", 0.5))
        self.drag_anchor_y_ratio = float(drag_anchor.get("yRatio", 0.12))
        falling = runtime.get("falling", {})
        self.fall_gravity = float(falling.get("gravity", 1.2))
        self.fall_max_velocity = float(falling.get("maxVelocity", 18.0))
        self.hard_drop_height = float(falling.get("hardDropHeight", 300))
        self.soft_landing_ticks = int(falling.get("softLandingTicks", 24))
        self.hard_landing_ticks = int(falling.get("hardLandingTicks", 58))
        self.landing_hold_ticks = int(falling.get("landingHoldTicks", 12))
        self.post_landing_cooldown_ticks = int(falling.get("postLandingCooldownTicks", 18))
        self.glider_enabled = bool(falling.get("gliderEnabled", True))
        self.glider_min_drop_height = float(falling.get("gliderMinDropHeight", self.hard_drop_height))
        self.glider_deploy_animation = str(falling.get("gliderDeployAnimation", "glider-deploy"))
        self.glider_animation = str(falling.get("gliderAnimation", "gliding"))
        self.glider_landing_animation = str(falling.get("gliderLandingAnimation", "glider-landing"))
        self.glider_descent_speed = max(0.8, float(falling.get("gliderDescentSpeed", 3.1)))
        self.glider_deploy_descent_speed = max(
            0.4,
            float(falling.get("gliderDeployDescentSpeed", self.glider_descent_speed * 0.45)),
        )
        self.glider_sway_pixels = max(0.0, float(falling.get("gliderSwayPixels", 6.0)))
        ceiling_hold = runtime.get("ceilingHold", {})
        self.ceiling_hold_enabled = bool(ceiling_hold.get("enabled", True))
        self.ceiling_trigger_margin = int(ceiling_hold.get("triggerMargin", 22))
        self.ceiling_top_padding = int(ceiling_hold.get("topPadding", 0))
        self.ceiling_grab_animation = str(ceiling_hold.get("grabAnimation", "ceiling-grab"))
        self.ceiling_hold_animation = str(ceiling_hold.get("holdAnimation", "ceiling-hold"))
        self.ceiling_release_animation = str(ceiling_hold.get("releaseAnimation", "ceiling-release"))
        self.ceiling_min_hold_ticks = max(30, int(ceiling_hold.get("minHoldTicks", 130)))
        self.ceiling_max_hold_ticks = max(self.ceiling_min_hold_ticks, int(ceiling_hold.get("maxHoldTicks", 230)))
        self.ceiling_release_drop_offset = int(ceiling_hold.get("releaseDropOffset", 18))
        self.ceiling_post_release_cooldown_ticks = max(0, int(ceiling_hold.get("postReleaseCooldownTicks", 60)))

        self.spritesheet = QPixmap(str(pet_dir / self.manifest["sprite"]))
        if self.spritesheet.isNull():
            raise RuntimeError(f"Could not load spritesheet for {pet_dir}")

        self.current_animation = "idle"
        self.current_frame = 0
        self.motion = "idle"
        self.direction = random.choice([-1, 1])
        self.velocity_x = self.speed * self.direction
        self.paused = False
        self.resting = False
        self.drag_offset: QPoint | None = None
        self.action_ticks = 0
        self.frame_accumulator = 0.0
        self.fall_y = 0.0
        self.fall_start_y = 0.0
        self.fall_total_ticks = 1
        self.fall_ticks_elapsed = 0
        self.fall_velocity_y = 0.0
        self.drop_height = 0.0
        self.glider_rescue_active = False
        self.glider_origin_x = 0
        self.glider_sway_phase = 0.0
        self.ceiling_hold_active = False
        self.ceiling_hold_origin_x = 0
        self.ceiling_cooldown_ticks = 0
        self.post_action_cooldown_ticks = 0
        self.energy = random.uniform(78, 100)
        codex_bubble = runtime.get("codexBubble", {})
        codex_bubble_enabled = bool(codex_bubble.get("enabled", True))
        self.codex_attention_animation = str(codex_bubble.get("attentionAnimation", "codex-attention"))
        self.codex_approval_bridge_enabled = bool(codex_bubble.get("approvalBridgeEnabled", True))
        self.exit_on_terminal_close = bool(codex_bubble.get("exitOnTerminalClose", False))
        self.terminal_close_pointer_path: Path | None = None
        self.terminal_close_timer: QTimer | None = None
        self.thought_dot_headroom = max(0, int(round(float(codex_bubble.get("minimizedDotsHeadroom", 34)) * self.scale)))
        codex_usage = runtime.get("codexUsage", {})
        codex_usage_enabled = bool(codex_usage.get("enabled", True))
        idle_mixins = runtime.get("idleMixins", {})
        self.idle_pocket_animation = str(idle_mixins.get("pocketAnimation", "idle-pocket"))
        self.idle_pocket_chance = max(0.0, min(1.0, float(idle_mixins.get("pocketChance", 0.08))))
        self.idle_pocket_min_ticks = max(20, int(idle_mixins.get("pocketMinTicks", 70)))
        self.idle_pocket_max_ticks = max(self.idle_pocket_min_ticks, int(idle_mixins.get("pocketMaxTicks", 110)))
        work_drop = runtime.get("workDrop", {})
        self.work_drop_enabled = bool(work_drop.get("enabled", True))
        companions = runtime.get("companions", {})
        if not isinstance(companions, dict):
            companions = {}
        self.companion_menu_label = str(companions.get("menuLabel", "Companions")).strip() or "Companions"
        self.companion_entries = self.parse_companion_entries(companions)
        worktree_tasks = runtime.get("worktreeTasks", {})
        if not isinstance(worktree_tasks, dict):
            worktree_tasks = {}
        self.worktree_tasks_enabled = bool(worktree_tasks.get("enabled", True))
        self.worktree_task_base_ref = str(worktree_tasks.get("baseRef", "HEAD")).strip() or "HEAD"
        self.worktree_task_companion_id = str(worktree_tasks.get("companionId", "")).strip()
        self.worktree_task_terminal_name = str(worktree_tasks.get("terminal", "auto")).strip() or "auto"
        self.worktree_task_title_prefix = str(worktree_tasks.get("terminalTitlePrefix", "Codex Worktree")).strip()
        if not self.worktree_task_title_prefix:
            self.worktree_task_title_prefix = "Codex Worktree"
        raw_worktrees_dir = str(worktree_tasks.get("worktreesDir", "")).strip()
        self.worktree_task_worktrees_dir: Path | None = None
        if raw_worktrees_dir:
            worktrees_dir = Path(raw_worktrees_dir).expanduser()
            if not worktrees_dir.is_absolute():
                worktrees_dir = self.repo_root / worktrees_dir
            self.worktree_task_worktrees_dir = worktrees_dir
        providers = runtime.get("aiProviders", {})
        claude_provider = providers.get("claude", {}) if isinstance(providers.get("claude"), dict) else {}
        self.claude_enabled = bool(claude_provider.get("enabled", True))
        self.claude_model = str(claude_provider.get("model", "claude-sonnet-4-6"))
        self.claude_max_tokens = max(256, int(claude_provider.get("maxTokens", 4096)))
        slack_provider = providers.get("slack", {}) if isinstance(providers.get("slack"), dict) else {}
        self.slack_enabled = bool(slack_provider.get("enabled", False))
        self.slack_default_send_as = self.clean_slack_send_as(slack_provider.get("sendAs", "bot"))
        self.slack_send_as = self.slack_default_send_as
        self.slack_transcript_enabled = bool(slack_provider.get("transcriptEnabled", True))
        self.slack_transcript_dir_name = str(slack_provider.get("transcriptDir", "runtime-slack")).strip()
        if not self.slack_transcript_dir_name:
            self.slack_transcript_dir_name = "runtime-slack"
        self.slack_contacts = self.parse_slack_contacts(slack_provider)
        self.slack_active_contact_id = ""
        default_slack_contact = self.slack_contacts[0] if self.slack_contacts else {}
        self.slack_conversation_id = default_slack_contact.get("conversation_id", "")
        self.slack_user_id = default_slack_contact.get("user_id", "")
        self.slack_conversation_label = default_slack_contact.get("label", "Slack") or "Slack"
        self.slack_active_contact_id = default_slack_contact.get("id", "")
        self.slack_send_as = default_slack_contact.get("send_as", self.slack_default_send_as)
        self.slack_poll_enabled = bool(slack_provider.get("pollEnabled", True))
        self.slack_poll_seconds = max(10.0, float(slack_provider.get("pollSeconds", 30)))
        self.slack_history_limit = max(1, min(50, int(slack_provider.get("historyLimit", 8))))
        self.slack_status_hold_seconds = max(10.0, float(slack_provider.get("statusHoldSeconds", 90)))
        self.slack_message_animation = str(slack_provider.get("messageAnimation", "slack-message"))
        self.slack_send_animation = str(slack_provider.get("sendAnimation", "slack-send"))
        github_provider = providers.get("github", {}) if isinstance(providers.get("github"), dict) else {}
        self.github_enabled = bool(github_provider.get("enabled", False))
        self.github_remote = str(github_provider.get("remote", "origin")).strip() or "origin"
        self.github_main_branch = str(github_provider.get("mainBranch", "main")).strip() or "main"
        self.github_require_clean = bool(github_provider.get("requireCleanTree", True))
        self.github_action_animation = str(github_provider.get("actionAnimation", "github-action"))
        self.work_default_provider = str(work_drop.get("defaultProvider", AI_PROVIDER_CODEX))
        if self.work_default_provider not in AI_PROVIDERS:
            self.work_default_provider = AI_PROVIDER_CODEX
        self.work_default_sandbox = str(work_drop.get("defaultSandbox", "read-only"))
        if self.work_default_sandbox not in SAFE_SANDBOXES:
            self.work_default_sandbox = "read-only"
        self.bubble_reply_sandbox = str(work_drop.get("bubbleReplySandbox", "workspace-write"))
        if self.bubble_reply_sandbox not in SAFE_SANDBOXES:
            self.bubble_reply_sandbox = self.work_default_sandbox
        self.work_status_hold_seconds = max(4.0, float(work_drop.get("statusHoldSeconds", 28)))
        self.work_output_dir_name = str(work_drop.get("outputDir", "runtime-work"))
        self.work_prompt_animation = str(work_drop.get("promptAnimation", "prompting"))
        self.work_review_animation = str(work_drop.get("reviewAnimation", "file-review"))
        self.work_receive_animation = str(work_drop.get("receiveAnimation", "file-receive"))
        self.work_inspect_animation = str(work_drop.get("inspectAnimation", "file-inspect"))
        self.work_laptop_animation = str(work_drop.get("laptopAnimation", "work-laptop"))
        self.work_reply_start_animation = str(work_drop.get("replyStartAnimation", "phone-reply-start"))
        self.work_reply_animation = str(work_drop.get("replyAnimation", "phone-reply"))
        self.bubble_reply_enabled = bool(work_drop.get("bubbleReplyEnabled", True))
        self.rest_enter_animation = str(work_drop.get("restEnterAnimation", "rest-enter-bed"))
        self.rest_animation = str(work_drop.get("restAnimation", "sleeping"))
        self.sleep_pulse_enabled = bool(work_drop.get("sleepPulse", True))
        self.sleep_snooze_marks_enabled = bool(work_drop.get("sleepSnoozeMarks", True))
        self.rest_on_limit_exhausted = bool(work_drop.get("restOnLimitExhausted", True))
        self.limit_rest_active = False
        self.work_process: QProcess | None = None
        self.work_request: WorkRequest | None = None
        self.work_provider_override = ""
        self.work_output_path: Path | None = None
        self.work_stdout_buffer = ""
        self.work_stderr = ""
        self.work_cancelled = False
        self.work_headline = ""
        self.work_detail = ""
        self.work_status_until = 0.0
        self.work_history: list[str] = []
        self.work_bubble_expanded = False
        self.work_bubble_minimized = False
        self.thought_bubble_expanded = False
        self.thought_bubble_minimized = False
        self.last_codex_status = None
        self.bubble_reply_active = False
        self.bubble_reply_session_id = ""
        self.bubble_reply_provider = AI_PROVIDER_CODEX
        self.claude_conversation_active = False
        self.claude_conversation_messages: list[dict[str, str]] = []
        self.slack_poll_timer: QTimer | None = None
        self.slack_poll_process: QProcess | None = None
        self.slack_poll_stdout = ""
        self.slack_poll_stderr = ""
        self.slack_poll_initialized = False
        self.slack_latest_ts = ""
        self.slack_last_sent_ts = ""
        self.slack_status_headline = ""
        self.slack_status_detail = ""
        self.slack_status_meta = ""
        self.slack_status_until = 0.0
        self.bubble_reply_slack_contact: dict[str, str] | None = None
        self.pending_slack_message: tuple[str, dict[str, str]] | None = None
        self.work_slack_text = ""
        self.work_slack_contact: dict[str, str] | None = None
        self.work_slack_logged_ts = ""
        self.work_github_action = ""
        self.work_github_cwd: Path | None = None
        self.worktree_review_windows: list[TaskReviewDialog] = []
        self.pending_reply_request: WorkRequest | None = None
        self.pending_resume_reply: tuple[str, str] | None = None
        self.work_resume_session_id = ""
        self.drop_hover_active = False
        self.drop_dialog_active = False
        self.codex_session = (
            codex_session
            if codex_session is not None
            else str(codex_bubble.get("session", "off"))
        )
        self.codex_monitor: CodexSessionMonitor | None = None
        self.thought_bubble: ThoughtBubble | None = None
        self.work_drop_bubble: WorkDropBubble | None = None
        usage_session = (
            codex_session
            if codex_session is not None
            else str(codex_usage.get("session", self.codex_session or "current"))
        )
        self.usage_monitor: CodexUsageMonitor | None = None
        self.usage_meter: UsageMeterOverlay | None = None
        self.usage_visible_seconds = max(3.0, float(codex_usage.get("visibleSeconds", 12)))
        self.usage_activation_delay_ms = max(0, int(codex_usage.get("activationDelayMs", 520)))
        self.usage_pending = False
        self.usage_timer: QTimer | None = None
        project_root = Path(__file__).resolve().parent.parent

        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self.setAcceptDrops(self.work_drop_enabled)
        self.resize(self.render_width, self.window_height)
        self.move_to_start()

        self.frame_timer = QTimer(self)
        self.frame_timer.timeout.connect(self.tick_animation)
        self.frame_timer.start(33)

        self.motion_timer = QTimer(self)
        self.motion_timer.timeout.connect(self.tick_motion)
        self.motion_timer.start(33)

        self.behavior_timer = QTimer(self)
        self.behavior_timer.timeout.connect(self.choose_next_behavior)
        self.behavior_timer.start(2800)

        self.state_file = self.pet_dir / "runtime-state.json"
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.timeout.connect(self.write_heartbeat)
        self.heartbeat_timer.start(2000)
        self.write_heartbeat()

        codex_session_enabled = self.codex_session.lower() not in {
            "off",
            "none",
            "disabled",
            "false",
            "0",
        }
        if codex_bubble_enabled and (codex_session_enabled or self.work_drop_enabled):
            bubble_sprite = Path(str(codex_bubble.get("sprite", ""))).expanduser()
            if not bubble_sprite.is_absolute():
                bubble_sprite = project_root / bubble_sprite
            self.thought_bubble = ThoughtBubble(
                always_on_top=always_on_top,
                sprite_path=bubble_sprite,
                sprite_frames=int(codex_bubble.get("frames", 1)),
                open_frames=int(codex_bubble.get("openFrames", 0)),
                loop_frames=int(codex_bubble.get("loopFrames", 0)),
                close_frames=int(codex_bubble.get("closeFrames", 0)),
                sprite_fps=float(codex_bubble.get("fps", 6.0)),
                display_width=int(codex_bubble["width"]) if "width" in codex_bubble else None,
                display_height=int(codex_bubble["height"]) if "height" in codex_bubble else None,
            )
            self.thought_bubble.set_reply_available(self.work_drop_enabled and self.bubble_reply_enabled)
            self.thought_bubble.reply_requested.connect(self.open_reply_from_bubble_button)
            self.thought_bubble.reply_submitted.connect(self.submit_bubble_reply)
            self.thought_bubble.reply_cancelled.connect(self.cancel_bubble_reply)
            self.thought_bubble.approval_requested.connect(self.submit_codex_approval)
            self.thought_bubble.expand_requested.connect(self.toggle_thought_bubble_expanded)
            if codex_session_enabled:
                self.codex_monitor = CodexSessionMonitor(selector=self.codex_session, owner_id=self.pet_id)
            self.codex_timer = QTimer(self)
            self.codex_timer.timeout.connect(self.tick_codex_status)
            self.codex_timer.start(1200)
            self.tick_codex_status()
            if self.exit_on_terminal_close:
                self.terminal_close_pointer_path = self.pointer_path_from_selector(self.codex_session)
                if self.terminal_close_pointer_path is not None:
                    self.terminal_close_timer = QTimer(self)
                    self.terminal_close_timer.timeout.connect(self.tick_terminal_lifecycle)
                    self.terminal_close_timer.start(1200)

        if self.work_drop_enabled:
            bubble_sprite = Path(str(codex_bubble.get("sprite", ""))).expanduser()
            if not bubble_sprite.is_absolute():
                bubble_sprite = project_root / bubble_sprite
            self.work_drop_bubble = WorkDropBubble(
                always_on_top=always_on_top,
                sprite_path=bubble_sprite,
                sprite_frames=int(codex_bubble.get("frames", 1)),
                open_frames=int(codex_bubble.get("openFrames", 0)),
                loop_frames=int(codex_bubble.get("loopFrames", 0)),
                close_frames=int(codex_bubble.get("closeFrames", 0)),
                sprite_fps=float(codex_bubble.get("fps", 6.0)),
            )
            self.work_drop_bubble.submitted.connect(self.submit_work_drop_request)
            self.work_drop_bubble.cancelled.connect(self.cancel_work_drop_bubble)

        if self.slack_enabled and self.slack_poll_enabled and self.slack_target_configured():
            self.slack_poll_timer = QTimer(self)
            self.slack_poll_timer.timeout.connect(self.poll_slack_messages)
            self.slack_poll_timer.start(int(round(self.slack_poll_seconds * 1000)))
            QTimer.singleShot(2500, self.poll_slack_messages)

        if codex_usage_enabled and usage_session.lower() not in {
            "off",
            "none",
            "disabled",
            "false",
            "0",
        }:
            usage_sprite = Path(str(codex_usage.get("sprite", ""))).expanduser()
            if not usage_sprite.is_absolute():
                usage_sprite = project_root / usage_sprite
            usage_base_frame: Path | None = None
            usage_base_frame_value = str(codex_usage.get("baseFrame", "")).strip()
            if usage_base_frame_value:
                usage_base_frame = Path(usage_base_frame_value).expanduser()
                if not usage_base_frame.is_absolute():
                    usage_base_frame = project_root / usage_base_frame
            self.usage_monitor = CodexUsageMonitor(selector=usage_session)
            self.usage_meter = UsageMeterOverlay(
                always_on_top=always_on_top,
                sprite_path=usage_sprite,
                sprite_frames=int(codex_usage.get("frames", 1)),
                open_frames=int(codex_usage.get("openFrames", 0)),
                loop_frames=int(codex_usage.get("loopFrames", 0)),
                close_frames=int(codex_usage.get("closeFrames", 0)),
                sprite_fps=float(codex_usage.get("fps", 6.0)),
                base_frame_path=usage_base_frame,
                open_seconds=float(codex_usage["openSeconds"]) if "openSeconds" in codex_usage else None,
                close_seconds=float(codex_usage["closeSeconds"]) if "closeSeconds" in codex_usage else None,
                display_width=int(codex_usage["width"]) if "width" in codex_usage else None,
                display_height=int(codex_usage["height"]) if "height" in codex_usage else None,
            )
            interval_minutes = max(1.0, float(codex_usage.get("intervalMinutes", 30)))
            self.usage_timer = QTimer(self)
            self.usage_timer.timeout.connect(lambda: self.show_usage_meter(periodic=True))
            self.usage_timer.start(int(round(interval_minutes * 60_000)))
            startup_delay_ms = max(0, int(round(float(codex_usage.get("startupDelaySeconds", 90)) * 1000)))
            if startup_delay_ms:
                QTimer.singleShot(startup_delay_ms, lambda: self.show_usage_meter(periodic=True))

    @property
    def render_width(self) -> int:
        return int(self.cell_width * self.scale)

    @property
    def render_height(self) -> int:
        return int(self.cell_height * self.scale)

    @property
    def window_height(self) -> int:
        return self.thought_dot_headroom + self.render_height

    def sprite_rect(self) -> QRectF:
        return QRectF(0, self.thought_dot_headroom, self.render_width, self.render_height)

    def pet_visual_top(self, *, y: int | None = None) -> int:
        return (self.y() if y is None else y) + self.thought_dot_headroom

    def pet_visual_bottom(self, *, y: int | None = None) -> int:
        return self.pet_visual_top(y=y) + self.render_height

    def window_anchor_point(self, *, x: int | None = None, y: int | None = None) -> QPoint:
        return QPoint(
            (self.x() if x is None else x) + self.render_width // 2,
            self.pet_visual_top(y=y) + self.render_height // 2,
        )

    def nearest_screen_rect(self, point: QPoint) -> QRect:
        screen = QApplication.screenAt(point)
        if screen is not None:
            return screen.availableGeometry()
        screens = QApplication.screens()
        if not screens:
            primary = QApplication.primaryScreen()
            return primary.availableGeometry() if primary is not None else QRect(0, 0, 1920, 1080)

        def distance_to_rect(rect: QRect) -> int:
            dx = 0
            if point.x() < rect.left():
                dx = rect.left() - point.x()
            elif point.x() > rect.right():
                dx = point.x() - rect.right()
            dy = 0
            if point.y() < rect.top():
                dy = rect.top() - point.y()
            elif point.y() > rect.bottom():
                dy = point.y() - rect.bottom()
            return dx * dx + dy * dy

        return min((screen.availableGeometry() for screen in screens), key=distance_to_rect)

    def screen_rect(self, *, x: int | None = None, y: int | None = None) -> QRect:
        return self.nearest_screen_rect(self.window_anchor_point(x=x, y=y))

    def virtual_screen_rect(self) -> QRect:
        screens = QApplication.screens()
        if not screens:
            return self.screen_rect()
        rect = QRect(screens[0].availableGeometry())
        for screen in screens[1:]:
            rect = rect.united(screen.availableGeometry())
        return rect

    def window_anchor_is_on_screen(self, *, x: int | None = None, y: int | None = None) -> bool:
        return QApplication.screenAt(self.window_anchor_point(x=x, y=y)) is not None

    def ground_y(self, rect: QRect | None = None) -> int:
        rect = rect or self.screen_rect()
        return rect.bottom() - self.window_height - self.ground_padding

    def ceiling_y(self, rect: QRect | None = None) -> int:
        rect = rect or self.screen_rect()
        return rect.top() + self.ceiling_top_padding - self.thought_dot_headroom

    def drag_anchor_offset(self) -> QPoint:
        return QPoint(
            int(round(self.render_width * self.drag_anchor_x_ratio)),
            self.thought_dot_headroom + int(round(self.render_height * self.drag_anchor_y_ratio)),
        )

    def move_to_start(self) -> None:
        rect = self.screen_rect()
        x = rect.left() + random.randint(40, max(41, rect.width() - self.render_width - 40))
        self.move(x, self.ground_y(rect))
        self.update_bubble_position()

    def update_bubble_position(self) -> None:
        rect = self.screen_rect()
        if self.thought_bubble is not None and self.thought_bubble.isVisible():
            bubble = self.thought_bubble
            x = self.x() + (self.render_width - bubble.width()) // 2
            y = self.pet_visual_top() - bubble.height() - 8
            x = max(rect.left() + 8, min(x, rect.right() - bubble.width() - 8))
            if y < rect.top() + 8:
                y = self.pet_visual_bottom() + 8
            y = max(rect.top() + 8, min(y, rect.bottom() - bubble.height() - 8))
            bubble.move(x, y)
        if self.work_drop_bubble is not None and self.work_drop_bubble.isVisible():
            self.update_work_drop_bubble_position(rect)
        self.update_usage_meter_position(rect)

    def update_work_drop_bubble_position(self, rect: QRect | None = None) -> None:
        if self.work_drop_bubble is None or not self.work_drop_bubble.isVisible():
            return
        rect = rect or self.screen_rect()
        bubble = self.work_drop_bubble
        x = self.x() + (self.render_width - bubble.width()) // 2
        y = self.pet_visual_top() - bubble.height() - 8
        x = max(rect.left() + 8, min(x, rect.right() - bubble.width() - 8))
        if y < rect.top() + 8:
            y = max(rect.top() + 8, min(self.pet_visual_bottom() + 8, rect.bottom() - bubble.height() - 8))
        bubble.move(x, y)

    def update_usage_meter_position(self, rect: QRect | None = None) -> None:
        if self.usage_meter is None or not self.usage_meter.isVisible():
            return
        rect = rect or self.screen_rect()
        meter = self.usage_meter
        gap = 12
        right_x = self.x() + self.render_width + gap
        left_x = self.x() - meter.width() - gap
        right_fits = right_x + meter.width() <= rect.right() - 8
        left_fits = left_x >= rect.left() + 8
        if right_fits and left_fits:
            x = right_x if self.direction >= 0 else left_x
        elif right_fits:
            x = right_x
        elif left_fits:
            x = left_x
        else:
            right_space = rect.right() - (self.x() + self.render_width)
            left_space = self.x() - rect.left()
            x = rect.right() - meter.width() - 8 if right_space >= left_space else rect.left() + 8

        ground_bottom = min(rect.bottom() - 2, self.pet_visual_bottom() + 2)
        y = ground_bottom - meter.height()
        if y < rect.top() + 8:
            y = rect.top() + 8
        meter.move(x, y)

    def tick_codex_status(self) -> None:
        if self.thought_bubble is None:
            return
        can_reply = self.work_drop_enabled and self.bubble_reply_enabled
        self.thought_bubble.set_minimized(self.thought_bubble_minimized)
        if self.bubble_reply_active:
            self.thought_bubble.set_reply_available(True)
            self.thought_bubble.set_approval_available(False)
            self.thought_bubble.set_status(
                self.active_reply_headline(),
                active=False,
                visible=True,
                waiting_for_user=True,
                headline=self.active_reply_headline(),
                detail=self.active_reply_detail(),
                meta=self.active_reply_meta(),
            )
            self.update_bubble_position()
            return
        if self.work_status_visible():
            self.thought_bubble.set_reply_available(can_reply)
            self.thought_bubble.set_approval_available(False)
            self.thought_bubble.set_status(
                self.work_detail,
                active=self.work_process is not None,
                visible=True,
                waiting_for_user=False,
                headline=self.work_headline or "Companion work",
                detail=self.work_bubble_detail(),
                meta=self.work_bubble_meta(),
            )
            self.update_bubble_position()
            return
        status = None
        if self.codex_monitor is not None:
            status = self.codex_monitor.poll()
            status = self.status_with_terminal_approval(status)
            self.last_codex_status = status
            self.sync_pet_with_codex_status(status)
        elif self.codex_approval_bridge_enabled:
            status = self.status_with_terminal_approval(None)
            if status is not None:
                self.last_codex_status = status
                self.sync_pet_with_codex_status(status)

        if status is not None and (status.active or status.waiting_for_user):
            approval_available = (
                self.codex_approval_bridge_enabled
                and status.waiting_kind == "approval"
            )
            self.thought_bubble.set_reply_available(can_reply and status.replyable and bool(status.session_id))
            self.thought_bubble.set_approval_available(approval_available)
            self.thought_bubble.set_status(
                status.text,
                active=status.active,
                visible=status.visible,
                waiting_for_user=status.waiting_for_user,
                headline=status.headline,
                detail=self.codex_bubble_detail(status),
                meta=self.codex_bubble_meta(status),
            )
            self.update_bubble_position()
            return

        if self.slack_status_visible():
            self.thought_bubble.set_reply_available(can_reply and self.slack_target_configured())
            self.thought_bubble.set_approval_available(False)
            self.thought_bubble.set_reply_context(
                headline="Reply on Slack",
                placeholder="Type a Slack reply...",
                meta=self.slack_bubble_meta(),
            )
            self.thought_bubble.set_status(
                self.slack_status_detail,
                active=False,
                visible=True,
                waiting_for_user=True,
                headline=self.slack_status_headline or "Slack message",
                detail=self.slack_status_detail,
                meta=self.slack_status_meta or self.slack_bubble_meta(),
            )
            self.update_bubble_position()
            return

        if self.claude_conversation_active:
            self.thought_bubble.set_reply_available(can_reply)
            self.thought_bubble.set_approval_available(False)
            self.thought_bubble.set_reply_context(
                headline="Talking to Claude",
                placeholder="Type a message for Claude...",
                meta=self.claude_conversation_meta(),
            )
            self.thought_bubble.set_status(
                "Claude conversation",
                active=False,
                visible=True,
                waiting_for_user=False,
                headline="Claude is listening",
                detail=self.claude_conversation_detail(),
                meta=self.claude_conversation_meta(),
            )
            self.update_bubble_position()
            return

        if status is None:
            self.thought_bubble.set_reply_available(False)
            self.thought_bubble.set_approval_available(False)
            self.thought_bubble.set_status("", active=False, visible=False)
            self.update_bubble_position()
            return

        self.thought_bubble.set_reply_available(False)
        self.thought_bubble.set_approval_available(False)
        self.thought_bubble.set_status(
            status.text,
            active=status.active,
            visible=status.visible,
            waiting_for_user=status.waiting_for_user,
            headline=status.headline,
            detail=self.codex_bubble_detail(status),
            meta=self.codex_bubble_meta(status),
        )
        self.update_bubble_position()

    def status_with_terminal_approval(self, status: CodexStatus | None) -> CodexStatus | None:
        if not self.codex_approval_bridge_enabled:
            return status
        terminal = current_codex_action_required()
        if not terminal.ok:
            return status
        if not self.terminal_approval_can_override_status(status):
            return status

        session_id = status.session_id if status is not None else None
        session_path = status.session_path if status is not None else None
        detail = (
            "Waiting for approval in the Codex terminal.\n"
            "Approve = option 1, Trust = option 2, Deny = option 3."
        )
        title = terminal.window_title.strip()
        if title:
            detail += "\nTerminal: " + title
        return CodexStatus(
            "Action required " + detail.replace("\n", " "),
            active=False,
            session_id=session_id,
            session_path=session_path,
            headline="Action required",
            detail=detail,
            meta="approval bridge ready",
            visible=True,
            waiting_for_user=True,
            waiting_kind="approval",
            replyable=False,
        )

    def terminal_approval_can_override_status(self, status: CodexStatus | None) -> bool:
        if status is None:
            return False
        if status.waiting_kind == "approval":
            return True
        monitor = self.codex_monitor
        if monitor is not None and monitor.pending_approval_summary():
            return True
        if status.waiting_for_user:
            return False
        return not status.active

    def pointer_path_from_selector(self, selector: str) -> Path | None:
        text = str(selector or "").strip()
        if not text.startswith("pointer:"):
            return None
        pointer = Path(text[len("pointer:") :]).expanduser()
        if not pointer.is_absolute():
            pointer = self.repo_root / pointer
        return pointer

    def tick_terminal_lifecycle(self) -> None:
        pointer = self.terminal_close_pointer_path
        if pointer is None or not pointer.is_file():
            return
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        state = str(data.get("terminal_state") or "").strip().lower()
        if state not in {"closed", "exited", "terminated"}:
            return
        if self.terminal_close_timer is not None:
            self.terminal_close_timer.stop()
            self.terminal_close_timer = None
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def codex_bubble_detail(self, status) -> str:
        detail = status.detail
        if status.waiting_for_user and status.replyable:
            suffix = "Reply here to keep Codex moving."
            if suffix not in detail:
                detail = (detail + "\n" + suffix).strip()
        elif status.waiting_kind == "approval":
            suffix = (
                "Approve or deny from this bubble, or use the Codex terminal."
                if self.codex_approval_bridge_enabled
                else "Open the Codex terminal to approve this command."
            )
            if suffix not in detail:
                detail = (detail + "\n" + suffix).strip()
        if not self.thought_bubble_expanded:
            return detail
        parts = [detail] if detail else []
        if status.session_id:
            parts.append("Thread: " + status.session_id[:8])
        if status.session_path:
            parts.append("Source: " + status.session_path.name)
        return "\n".join(parts)

    def codex_bubble_meta(self, status) -> str:
        if status.meta:
            return status.meta
        if status.replyable and status.session_id:
            return "reply to session " + status.session_id[:8]
        if status.waiting_kind == "approval":
            return "approval bridge ready" if self.codex_approval_bridge_enabled else "approval required in Codex"
        if status.session_id:
            return "session " + status.session_id[:8]
        return ""

    def default_work_cwd(self) -> Path:
        return default_cwd_for_paths((), self.pet_dir.parent.parent)

    def github_work_cwd(self) -> Path:
        for candidate in self.github_cwd_candidates():
            if not candidate:
                continue
            try:
                resolved = candidate.expanduser().resolve()
            except OSError:
                continue
            root = detect_git_root(resolved)
            if root is not None:
                return root
            if resolved.is_dir():
                return resolved
        return self.default_work_cwd()

    def github_cwd_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        if self.work_request is not None:
            candidates.append(self.work_request.cwd)
        status = self.last_codex_status
        if status is not None and status.session_path is not None:
            candidates.extend(self.rollout_cwd_candidates(status.session_path))
        pointer = self.codex_active_pointer_cwd()
        if pointer is not None:
            candidates.append(pointer)
        candidates.append(self.default_work_cwd())
        return candidates

    def rollout_cwd_candidates(self, session_path: Path) -> list[Path]:
        try:
            with session_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 512_000))
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return []

        candidates: list[Path] = []
        for raw_line in reversed(text.splitlines()):
            if '"cwd"' not in raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            for source in (payload, event):
                if not isinstance(source, dict):
                    continue
                cwd = source.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    path = Path(cwd).expanduser()
                    if path not in candidates:
                        candidates.append(path)
            if len(candidates) >= 4:
                break
        return candidates

    def codex_active_pointer_cwd(self) -> Path | None:
        monitor = self.codex_monitor or self.usage_monitor
        pointer = None
        if isinstance(monitor, CodexSessionMonitor):
            pointer = monitor.active_session_pointer_path()
        else:
            pointer = Path.home() / ".codex" / "ai-desktop-companion" / "active-session.json"
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        cwd = data.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            return Path(cwd).expanduser()
        return None

    def work_status_visible(self) -> bool:
        return self.work_process is not None or time.time() < self.work_status_until

    def work_bubble_detail(self) -> str:
        return self.work_detail or "Preparing the request."

    def work_bubble_meta(self) -> str:
        if self.work_resume_session_id:
            return "replying to session " + self.work_resume_session_id[:8]
        if self.work_provider_override == AI_PROVIDER_SLACK:
            return self.slack_bubble_meta()
        if self.work_provider_override == AI_PROVIDER_GITHUB:
            cwd = self.work_github_cwd or self.github_work_cwd()
            return f"{self.github_action_label(self.work_github_action)} in {cwd.name or str(cwd)}"
        if self.work_request is None:
            return ""
        provider = self.work_provider_label(self.work_request)
        if self.work_request.provider == AI_PROVIDER_CLAUDE:
            return f"{provider} in {self.work_request.cwd.name or str(self.work_request.cwd)}"
        sandbox = self.work_request.sandbox
        cwd = self.work_request.cwd.name or str(self.work_request.cwd)
        return f"{provider} {sandbox} in {cwd}"

    def provider_name(self, provider: str) -> str:
        if provider == AI_PROVIDER_CLAUDE:
            return "Claude"
        if provider == AI_PROVIDER_SLACK:
            return "Slack"
        if provider == AI_PROVIDER_GITHUB:
            return "GitHub"
        return "Codex"

    def github_action_label(self, action: str) -> str:
        if action == "check":
            return "Check GitHub access"
        if action == "push":
            return "Push current branch"
        if action == "merge_main_push":
            return "Merge main, then push"
        if action == "merge_to_main":
            return "Merge into main, then push"
        return "GitHub action"

    def work_provider_label(self, request: WorkRequest | None) -> str:
        if request is None:
            return self.provider_name(self.work_provider_override or AI_PROVIDER_CODEX)
        return self.provider_name(request.provider)

    def bubble_reply_meta(self, provider: str) -> str:
        if provider == AI_PROVIDER_CLAUDE:
            return f"Claude {self.claude_model} in {self.default_work_cwd().name}"
        if provider == AI_PROVIDER_SLACK:
            return self.slack_bubble_meta(self.bubble_reply_slack_contact)
        return f"{self.bubble_reply_sandbox} in {self.default_work_cwd().name}"

    def clean_slack_send_as(self, value: object) -> str:
        mode = str(value or "").strip().lower()
        if mode in {"user", "rory", "me"}:
            return "user"
        return "bot"

    def parse_slack_contacts(self, slack_provider: dict) -> list[dict[str, str]]:
        contacts: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add_contact(raw: dict, fallback_label: str = "") -> None:
            conversation_id = str(
                raw.get("conversationId")
                or raw.get("defaultConversationId")
                or raw.get("channelId")
                or ""
            ).strip()
            bot_conversation_id = str(raw.get("botConversationId") or "").strip()
            user_conversation_id = str(raw.get("userConversationId") or "").strip()
            user_id = str(raw.get("userId") or raw.get("memberId") or "").strip()
            if not conversation_id and not bot_conversation_id and not user_conversation_id and not user_id:
                return
            label = str(
                raw.get("label")
                or raw.get("name")
                or raw.get("conversationLabel")
                or fallback_label
                or conversation_id
                or user_id
            ).strip()
            label = label or "Slack"
            contact_id = str(raw.get("id") or label.lower().replace(" ", "-") or conversation_id or user_id).strip()
            send_as = self.clean_slack_send_as(
                raw.get("sendAs")
                or raw.get("send_as")
                or self.slack_default_send_as
            )
            if conversation_id:
                if send_as == "user" and not user_conversation_id:
                    user_conversation_id = conversation_id
                elif not bot_conversation_id:
                    bot_conversation_id = conversation_id
            key = (conversation_id, user_id)
            if key in seen:
                return
            seen.add(key)
            contacts.append(
                {
                    "id": contact_id,
                    "label": label,
                    "conversation_id": conversation_id,
                    "bot_conversation_id": bot_conversation_id,
                    "user_conversation_id": user_conversation_id,
                    "user_id": user_id,
                    "send_as": send_as,
                }
            )

        raw_contacts = slack_provider.get("contacts")
        if isinstance(raw_contacts, list):
            for item in raw_contacts:
                if isinstance(item, dict):
                    add_contact(item)
        add_contact(slack_provider, str(slack_provider.get("conversationLabel") or ""))
        return contacts

    def current_slack_contact(self) -> dict[str, str]:
        return {
            "id": self.slack_active_contact_id,
            "label": self.slack_conversation_label,
            "conversation_id": self.slack_conversation_id,
            "bot_conversation_id": self.slack_conversation_id if self.slack_send_as == "bot" else "",
            "user_conversation_id": self.slack_conversation_id if self.slack_send_as == "user" else "",
            "user_id": self.slack_user_id,
            "send_as": self.slack_send_as,
        }

    def slack_contact_for_identity(self, contact: dict[str, str], send_as: str) -> dict[str, str]:
        mode = self.clean_slack_send_as(send_as)
        selected = ""
        if mode == "user":
            selected = str(contact.get("user_conversation_id") or "").strip()
        else:
            selected = str(contact.get("bot_conversation_id") or contact.get("conversation_id") or "").strip()
        selected_contact = dict(contact)
        selected_contact["send_as"] = mode
        selected_contact["conversation_id"] = selected
        return selected_contact

    def set_slack_target(self, contact: dict[str, str], *, reset_poll: bool = True) -> None:
        send_as = self.clean_slack_send_as(contact.get("send_as") or self.slack_default_send_as)
        target_contact = self.slack_contact_for_identity(contact, send_as)
        conversation_id = str(target_contact.get("conversation_id") or "").strip()
        user_id = str(contact.get("user_id") or "").strip()
        label = str(contact.get("label") or "Slack").strip() or "Slack"
        contact_id = str(contact.get("id") or label.lower().replace(" ", "-")).strip()
        changed = (
            conversation_id != self.slack_conversation_id
            or user_id != self.slack_user_id
            or contact_id != self.slack_active_contact_id
            or send_as != self.slack_send_as
        )
        self.slack_conversation_id = conversation_id
        self.slack_user_id = user_id
        self.slack_conversation_label = label
        self.slack_active_contact_id = contact_id
        self.slack_send_as = send_as
        if changed and reset_poll:
            self.slack_poll_initialized = False
            self.slack_latest_ts = ""
            self.slack_last_sent_ts = ""
            self.slack_status_until = 0.0

    def remember_slack_conversation_id(self, conversation_id: str) -> None:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return
        self.slack_conversation_id = conversation_id
        for contact in self.slack_contacts:
            if contact.get("id") == self.slack_active_contact_id:
                if self.slack_send_as == "user":
                    contact["user_conversation_id"] = conversation_id
                else:
                    contact["bot_conversation_id"] = conversation_id
                    contact["conversation_id"] = conversation_id
                break

    def slack_target_configured(self, contact: dict[str, str] | None = None) -> bool:
        target = contact or self.current_slack_contact()
        return bool(target.get("conversation_id") or target.get("user_id"))

    def slack_bubble_meta(self, contact: dict[str, str] | None = None) -> str:
        target_contact = contact or self.current_slack_contact()
        target = target_contact.get("label") or "Slack"
        sender = self.slack_send_as_label(target_contact)
        if target_contact.get("conversation_id"):
            return f"Slack - {target} - as {sender}"
        if target_contact.get("user_id"):
            return f"Slack DM - {target} - as {sender}"
        return "Slack not configured"

    def slack_contact_send_as(self, contact: dict[str, str] | None = None) -> str:
        target = contact or self.current_slack_contact()
        return self.clean_slack_send_as(target.get("send_as") or self.slack_send_as)

    def slack_send_as_label(self, contact: dict[str, str] | None = None) -> str:
        return "You" if self.slack_contact_send_as(contact) == "user" else "Companion"

    def slack_transcript_dir(self) -> Path:
        return self.pet_dir / self.slack_transcript_dir_name

    def slack_contact_slug(self, contact: dict[str, str] | None = None) -> str:
        target = contact or self.current_slack_contact()
        raw = (
            target.get("id")
            or target.get("label")
            or target.get("conversation_id")
            or target.get("user_id")
            or "slack"
        )
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw).strip().lower()).strip("-._")
        return (slug or "slack")[:80]

    def slack_transcript_path(self, contact: dict[str, str] | None = None) -> Path:
        return self.slack_transcript_dir() / f"{self.slack_contact_slug(contact)}.md"

    def ensure_slack_transcript(self, contact: dict[str, str] | None = None) -> Path:
        path = self.slack_transcript_path(contact)
        if path.exists() and path.stat().st_size > 0:
            return path
        target = contact or self.current_slack_contact()
        label = target.get("label") or "Slack"
        target_id = target.get("conversation_id") or target.get("user_id") or "unconfigured"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Slack Conversation - {label}\n\n"
            "Local transcript written by AI Desktop Companion.\n\n"
            f"- Target: `{target_id}`\n"
            "- Senders: Companion and You when both Slack identities are used.\n\n"
        )
        path.write_text(header, encoding="utf-8")
        return path

    def append_slack_transcript(
        self,
        contact: dict[str, str] | None,
        speaker: str,
        text: str,
        *,
        ts: str = "",
    ) -> None:
        if not self.slack_transcript_enabled:
            return
        body = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not body:
            body = "_(empty message)_"
        try:
            path = self.ensure_slack_transcript(contact)
            timestamp = self.slack_transcript_timestamp(ts)
            ts_line = f"\nSlack ts: `{ts}`\n" if ts else ""
            entry = f"\n## {timestamp}\n\n**{speaker}**  \n{ts_line}\n{body}\n"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(entry)
        except OSError as exc:
            self.push_work_history("Slack transcript failed: " + self.clean_final_work_text(str(exc)))

    def slack_transcript_timestamp(self, ts: str = "") -> str:
        value = self.slack_ts_value(ts)
        if value > 0:
            moment = datetime.fromtimestamp(value).astimezone()
        else:
            moment = datetime.now().astimezone()
        return moment.strftime("%Y-%m-%d %H:%M:%S %Z")

    def open_slack_transcript(self, contact: dict[str, str] | None = None) -> None:
        if not self.slack_transcript_enabled:
            return
        target_contact = contact or self.current_slack_contact()
        try:
            path = self.ensure_slack_transcript(target_contact)
        except OSError as exc:
            self.set_work_status("Slack log unavailable", str(exc), active=False, hold=True)
            return
        result = QProcess.startDetached("xdg-open", [str(path)])
        started = result[0] if isinstance(result, tuple) else bool(result)
        if not started:
            self.set_work_status("Slack log ready", str(path), active=False, hold=True)

    def slack_status_visible(self) -> bool:
        return bool(self.slack_status_detail and time.time() < self.slack_status_until)

    def claude_conversation_meta(self) -> str:
        turns = sum(1 for item in self.claude_conversation_messages if item.get("role") == "user")
        suffix = f"{turns} turn" if turns == 1 else f"{turns} turns"
        return f"Claude {self.claude_model} - {suffix}"

    def claude_conversation_detail(self) -> str:
        for item in reversed(self.claude_conversation_messages):
            if item.get("role") == "assistant" and item.get("content"):
                response = self.bubble_output_text(str(item["content"]))
                return (
                    response
                    + "\n\nReply here to continue the Claude conversation. "
                    + "End it from Companion's menu when you want a fresh thread."
                )
        return "Reply here to continue the Claude conversation. End it from Companion's menu when you want a fresh thread."

    def active_reply_headline(self) -> str:
        if self.bubble_reply_session_id:
            return "Reply to Codex"
        if self.bubble_reply_provider == AI_PROVIDER_CLAUDE:
            return "Talking to Claude"
        if self.bubble_reply_provider == AI_PROVIDER_SLACK:
            contact = self.bubble_reply_slack_contact or self.current_slack_contact()
            label = contact.get("label") or "Slack"
            return f"Message {label} as {self.slack_send_as_label(contact)}"
        return "Ask Codex"

    def active_reply_meta(self) -> str:
        if self.bubble_reply_session_id:
            return "reply to session " + self.bubble_reply_session_id[:8]
        if self.bubble_reply_provider == AI_PROVIDER_CLAUDE:
            return self.claude_conversation_meta() if self.claude_conversation_active else self.bubble_reply_meta(AI_PROVIDER_CLAUDE)
        if self.bubble_reply_provider == AI_PROVIDER_SLACK:
            return self.slack_bubble_meta(self.bubble_reply_slack_contact)
        return self.bubble_reply_meta(AI_PROVIDER_CODEX)

    def active_reply_detail(self) -> str:
        if self.bubble_reply_session_id:
            return "Type your response for the waiting Codex session. Enter sends. Shift+Enter starts a new line."
        if self.bubble_reply_provider == AI_PROVIDER_CLAUDE:
            return "Type a message for Claude. Enter sends. Shift+Enter starts a new line."
        if self.bubble_reply_provider == AI_PROVIDER_SLACK:
            contact = self.bubble_reply_slack_contact or self.current_slack_contact()
            return (
                f"Type a Slack message as {self.slack_send_as_label(contact)}. "
                "Enter sends. Shift+Enter starts a new line."
            )
        return "Type a request for Codex. Enter sends. Shift+Enter starts a new line."

    def set_work_status(
        self,
        headline: str,
        detail: str,
        *,
        active: bool = True,
        hold: bool = False,
    ) -> None:
        self.work_headline = headline
        self.work_detail = self.bubble_output_text(detail)
        if hold:
            self.work_status_until = time.time() + self.work_status_hold_seconds
        elif active:
            self.work_status_until = 0.0
        if self.thought_bubble is not None:
            self.tick_codex_status()

    def bubble_output_text(self, text: str, limit: int = 100_000) -> str:
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            return ""
        if len(raw) > limit:
            raw = raw[:limit].rstrip() + "\n\n[Bubble preview truncated; full output is saved in the runtime-work file.]"
        lines: list[str] = []
        blank_count = 0
        for line in raw.split("\n"):
            cleaned = line.rstrip()
            if cleaned.strip():
                blank_count = 0
                lines.append(cleaned)
            else:
                blank_count += 1
                if blank_count <= 1:
                    lines.append("")
        return "\n".join(lines).strip()

    def push_work_history(self, item: str) -> None:
        cleaned = " ".join(item.split())
        if not cleaned:
            return
        self.work_history.append(cleaned)
        self.work_history = self.work_history[-8:]

    def parse_companion_entries(self, companions: dict) -> list[dict[str, object]]:
        if not companions.get("enabled", False):
            return []
        raw_entries = companions.get("entries", [])
        if not isinstance(raw_entries, list):
            return []

        entries: list[dict[str, object]] = []
        for index, raw in enumerate(raw_entries, start=1):
            if not isinstance(raw, dict) or raw.get("enabled", True) is False:
                continue
            pet = str(raw.get("pet") or raw.get("petId") or "").strip()
            if not pet:
                continue
            label = str(raw.get("label") or raw.get("name") or pet).strip() or pet
            codex_session = str(raw.get("codexSession", raw.get("session", "off"))).strip() or "off"
            terminal_config = raw.get("codexTerminal", {})
            if not isinstance(terminal_config, dict):
                terminal_config = {}
            launch_terminal = bool(raw.get("launchCodexTerminal", False) or terminal_config.get("enabled", False))
            if codex_session.lower() in {"on", "terminal", "new", "spawn", "spawn-terminal"}:
                launch_terminal = True
            entry: dict[str, object] = {
                "id": str(raw.get("id") or f"companion-{index}").strip() or f"companion-{index}",
                "label": label,
                "pet": pet,
                "codex_session": "terminal" if launch_terminal else codex_session,
                "launch_codex_terminal": launch_terminal,
                "codex_terminal": terminal_config,
            }
            for key in ("scale", "speed"):
                if raw.get(key) is None:
                    continue
                try:
                    number = float(raw[key])
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number) and number > 0:
                    entry[key] = number
            entries.append(entry)
        return entries

    def companion_pet_arg(self, pet: object) -> str:
        pet_text = str(pet or "").strip()
        if not pet_text:
            return ""
        pet_path = Path(pet_text).expanduser()
        if pet_path.is_absolute():
            return str(pet_path)
        if "/" in pet_text:
            return str((self.repo_root / pet_path).resolve())
        return pet_text

    def companion_manifest_path(self, pet: object) -> Path | None:
        pet_arg = self.companion_pet_arg(pet)
        if not pet_arg:
            return None
        direct = Path(pet_arg)
        if direct.exists() and (direct / "pet.json").exists():
            return direct / "pet.json"
        bundled = DEFAULT_PETS_DIR / pet_arg / "pet.json"
        if bundled.exists():
            return bundled
        return None

    def companion_pointer_path(self, entry: dict[str, object], manifest_path: Path) -> Path:
        entry_id = str(entry.get("id") or entry.get("pet") or "companion").strip() or "companion"
        return manifest_path.parent / "runtime-codex" / f"{entry_id}-session.json"

    def companion_manifest_identity(self, manifest_path: Path) -> tuple[str, str]:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        pet_id = str(manifest.get("id") or manifest_path.parent.name).strip() or manifest_path.parent.name
        pet_name = str(manifest.get("name") or pet_id).strip() or pet_id
        return pet_id, pet_name

    def companion_terminal_cwd(self, entry: dict[str, object], manifest_path: Path) -> Path:
        terminal = entry.get("codex_terminal") if isinstance(entry.get("codex_terminal"), dict) else {}
        raw_cwd = terminal.get("cwd") or entry.get("cwd") or self.default_work_cwd()
        cwd = Path(str(raw_cwd)).expanduser()
        if not cwd.is_absolute():
            cwd = self.repo_root / cwd
        try:
            cwd = cwd.resolve()
        except OSError:
            cwd = self.repo_root
        if not cwd.is_dir():
            return self.repo_root
        return cwd

    def companion_codex_command(self, entry: dict[str, object], cwd: Path) -> list[str]:
        terminal = entry.get("codex_terminal") if isinstance(entry.get("codex_terminal"), dict) else {}
        raw_args = terminal.get("codexArgs")
        if isinstance(raw_args, list) and raw_args:
            return [str(part) for part in raw_args]

        codex = (
            os.environ.get("CODEX_BINARY")
            or os.environ.get("CODEX_CLI")
            or os.environ.get("CODEX_CLI_PATH")
            or shutil.which("codex")
            or "codex"
        )
        sandbox = str(terminal.get("sandbox", "workspace-write")).strip() or "workspace-write"
        approval = str(terminal.get("approvalPolicy", "untrusted")).strip() or "untrusted"
        command = [codex, "--cd", str(cwd), "--sandbox", sandbox, "--ask-for-approval", approval]
        if bool(terminal.get("noAltScreen", True)):
            command.append("--no-alt-screen")
        model = str(terminal.get("model", "")).strip()
        if model:
            command.extend(["--model", model])
        profile = str(terminal.get("profile", "")).strip()
        if profile:
            command.extend(["--profile", profile])
        prompt = str(terminal.get("prompt", "")).strip()
        if prompt:
            command.append(prompt)
        return command

    def terminal_launcher_args(self, title: str, command: list[str], terminal_name: str = "auto") -> list[str] | None:
        candidates = [terminal_name] if terminal_name and terminal_name != "auto" else [
            "gnome-terminal",
            "x-terminal-emulator",
            "kgx",
            "konsole",
            "xfce4-terminal",
            "xterm",
        ]
        for candidate in candidates:
            program = shutil.which(candidate)
            if not program:
                continue
            name = Path(program).name
            if name == "gnome-terminal":
                return [program, "--title", title, "--", *command]
            if name == "kgx":
                return [program, "--title", title, "--", *command]
            if name == "konsole":
                return [program, "--new-tab", "-p", f"tabtitle={title}", "-e", *command]
            if name == "xfce4-terminal":
                return [program, "--title", title, "--command", " ".join(shlex.quote(part) for part in command)]
            if name in {"x-terminal-emulator", "xterm"}:
                return [program, "-T", title, "-e", *command]
        return None

    def launch_companion_codex_terminal(
        self,
        entry: dict[str, object],
        *,
        manifest_path: Path,
        pointer_path: Path,
    ) -> tuple[bool, str]:
        terminal = entry.get("codex_terminal") if isinstance(entry.get("codex_terminal"), dict) else {}
        cwd = self.companion_terminal_cwd(entry, manifest_path)
        title = str(terminal.get("title") or f"{entry.get('label') or 'Companion'} Codex").strip()
        owner_id, owner_label = self.companion_manifest_identity(manifest_path)
        write_pending_session_owner(
            codex_home=None,
            owner_id=owner_id,
            owner_label=owner_label,
            cwd=cwd,
            pointer_path=pointer_path,
            selector="terminal",
        )
        bridge = [
            sys.executable,
            "-m",
            "hatchpet.codex_terminal_bridge",
            "--pointer",
            str(pointer_path),
            "--cwd",
            str(cwd),
            "--owner-id",
            owner_id,
            "--owner-label",
            owner_label,
        ]
        worktree_task_id = str(entry.get("worktree_task_id") or terminal.get("worktreeTaskId") or "").strip()
        if worktree_task_id:
            bridge.extend(["--worktree-task-id", worktree_task_id])
        bridge.extend(["--", *self.companion_codex_command(entry, cwd)])
        terminal_args = self.terminal_launcher_args(title, bridge, str(terminal.get("terminal", "auto")))
        if terminal_args is None:
            return False, "Could not find a supported terminal emulator."
        try:
            subprocess.Popen(
                terminal_args,
                cwd=str(self.repo_root),
                env=os.environ.copy(),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            return False, str(exc)
        if worktree_task_id:
            try:
                update_worktree_task(
                    worktree_task_id,
                    pointer_path=pointer_path,
                    owner_id=owner_id,
                    owner_label=owner_label,
                    terminal_state="launching",
                    status="terminal-launching",
                )
            except WorktreeTaskError:
                pass
        return True, f"Opened {title} in {cwd}."

    def spawn_companion(self, entry: dict[str, object]) -> None:
        pet_arg = self.companion_pet_arg(entry.get("pet"))
        label = str(entry.get("label") or pet_arg or "Companion")
        manifest_path = self.companion_manifest_path(pet_arg)
        if not pet_arg or manifest_path is None:
            self.set_work_status(
                "Companion unavailable",
                f"Could not find the pet pack for {label}.",
                active=False,
                hold=True,
            )
            return

        args = [str(self.repo_root / "run.py"), "run", pet_arg]
        scale = entry.get("scale", self.scale)
        speed = entry.get("speed", self.speed)
        codex_session = str(entry.get("codex_session") or "off")
        pointer_path = None
        if entry.get("launch_codex_terminal"):
            pointer_path = self.companion_pointer_path(entry, manifest_path)
            codex_session = "pointer:" + str(pointer_path)
        if scale:
            args.extend(["--scale", f"{float(scale):g}"])
        if speed:
            args.extend(["--speed", f"{float(speed):g}"])
        args.extend(["--codex-session", codex_session])

        log_dir = self.pet_dir / "runtime-companions"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            log_path = log_dir / f"{str(entry.get('id') or 'companion')}-{stamp}.log"
            with log_path.open("ab") as log_file:
                subprocess.Popen(
                    [sys.executable, *args],
                    cwd=str(self.repo_root),
                    env=os.environ.copy(),
                    stdout=log_file,
                    stderr=log_file,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            self.set_work_status("Companion failed to launch", str(exc), active=False, hold=True)
            return

        terminal_detail = ""
        if pointer_path is not None:
            ok, terminal_detail = self.launch_companion_codex_terminal(
                entry,
                manifest_path=manifest_path,
                pointer_path=pointer_path,
            )
            if not ok:
                self.set_work_status(
                    "Companion launched, terminal failed",
                    f"{label} is running, but Codex terminal launch failed: {terminal_detail}",
                    active=False,
                    hold=True,
                )
                return

        detail = f"{label} launched with Codex session selector `{codex_session}`."
        if terminal_detail:
            detail += "\n" + terminal_detail
        self.push_work_history(detail)
        self.work_status_until = 0.0

    def worktree_task_cwd(self) -> Path:
        return self.github_work_cwd()

    def selected_worktree_companion_entry(self) -> dict[str, object] | None:
        if not self.companion_entries:
            return None
        if self.worktree_task_companion_id:
            for entry in self.companion_entries:
                if str(entry.get("id") or "").strip() == self.worktree_task_companion_id:
                    return dict(entry)
        return dict(self.companion_entries[0])

    def prompt_for_worktree_task(self) -> str | None:
        prompt, ok = QInputDialog.getMultiLineText(
            self,
            "New Worktree Task",
            "Task prompt for Codex:",
            "",
        )
        if not ok:
            return None
        return str(prompt or "").strip()

    def start_worktree_task(self) -> None:
        if not self.worktree_tasks_enabled:
            self.set_work_status(
                "Worktree tasks disabled",
                "Enable runtime.worktreeTasks in the pet manifest to use worktree-backed tasks.",
                active=False,
                hold=True,
            )
            return
        prompt = self.prompt_for_worktree_task()
        if prompt is None:
            return
        cwd = self.worktree_task_cwd()
        label = " ".join(prompt.split())[:72].rstrip() if prompt else f"Codex task from {cwd.name}"
        try:
            task = create_worktree_task(
                cwd=cwd,
                label=label,
                prompt=prompt,
                base_ref=self.worktree_task_base_ref,
                owner_id=self.pet_id,
                owner_label=self.pet_name,
                worktrees_root=self.worktree_task_worktrees_dir,
            )
        except WorktreeTaskError as exc:
            self.set_work_status("Worktree task blocked", str(exc), active=False, hold=True)
            return

        companion_entry = self.selected_worktree_companion_entry()
        if companion_entry is not None:
            self.spawn_worktree_task_companion(task, companion_entry)
        else:
            self.launch_worktree_task_terminal_for_current_pet(task, prompt=prompt)

    def spawn_worktree_task_companion(self, task: WorktreeTask, entry: dict[str, object]) -> None:
        terminal = dict(entry.get("codex_terminal") if isinstance(entry.get("codex_terminal"), dict) else {})
        title_suffix = task.task_id[:11]
        terminal.update(
            {
                "enabled": True,
                "cwd": str(task.worktree_path),
                "title": f"{self.worktree_task_title_prefix} {title_suffix}",
                "sandbox": terminal.get("sandbox", "workspace-write"),
                "approvalPolicy": terminal.get("approvalPolicy", "untrusted"),
                "prompt": task.prompt,
                "worktreeTaskId": task.task_id,
                "terminal": terminal.get("terminal", self.worktree_task_terminal_name),
            }
        )
        label = str(entry.get("label") or entry.get("pet") or "Companion")
        dynamic_entry = {
            **entry,
            "id": f"worktree-{task.task_id}",
            "label": f"{label} - {title_suffix}",
            "codex_session": "terminal",
            "launch_codex_terminal": True,
            "codex_terminal": terminal,
            "worktree_task_id": task.task_id,
        }
        self.spawn_companion(dynamic_entry)
        detail = f"Created {task.task_id} at {task.worktree_path}."
        if task.local_dirty_at_create:
            detail += "\nLocal checkout had uncommitted changes; the worktree starts from committed HEAD."
        self.set_work_status("Worktree task launched", detail, active=False, hold=True)

    def launch_worktree_task_terminal_for_current_pet(self, task: WorktreeTask, *, prompt: str = "") -> None:
        ok, detail = launch_worktree_task_terminal(
            task,
            owner_id=self.pet_id,
            owner_label=self.pet_name,
            terminal_name=self.worktree_task_terminal_name,
            title=f"{self.worktree_task_title_prefix} {task.task_id[:11]}",
            prompt=prompt,
        )
        if ok:
            if task.local_dirty_at_create:
                detail += "\nLocal checkout had uncommitted changes; the worktree starts from committed HEAD."
            self.push_work_history(detail)
            self.set_work_status("Worktree task launched", detail, active=False, hold=True)
            return
        self.set_work_status("Worktree terminal failed", detail, active=False, hold=True)

    def show_worktree_task_summary(self) -> None:
        try:
            tasks = list_worktree_tasks()
            detail = summarize_tasks(tasks, limit=8)
        except WorktreeTaskError as exc:
            detail = str(exc)
        self.set_work_status("Worktree tasks", detail, active=False, hold=True)

    def show_worktree_task_status(self, task_id: str) -> None:
        try:
            task = get_worktree_task(task_id)
            detail = format_task_report(task)
        except WorktreeTaskError as exc:
            detail = str(exc)
        self.set_work_status("Worktree task status", detail, active=False, hold=True)

    def review_worktree_task(self, task_id: str) -> None:
        try:
            task = get_worktree_task(task_id)
        except WorktreeTaskError as exc:
            self.set_work_status("Worktree task missing", str(exc), active=False, hold=True)
            return
        dialog = TaskReviewDialog(
            task,
            parent=self,
            title_prefix="Companion Worktree Review",
            remote=self.github_remote,
            main_branch=self.github_main_branch,
            open_terminal=self.open_worktree_task_terminal,
            open_folder=self.open_worktree_task_folder,
            accent="cyan",
        )
        dialog.finished.connect(lambda _result, dialog=dialog: self.forget_worktree_review_window(dialog))
        self.worktree_review_windows.append(dialog)
        dialog.show()
        self.set_work_status("Worktree review opened", task.label or task.task_id, active=False, hold=True)

    def forget_worktree_review_window(self, dialog: TaskReviewDialog) -> None:
        self.worktree_review_windows = [item for item in self.worktree_review_windows if item is not dialog]

    def worktree_task_display_label(self, task: WorktreeTask, *, max_label: int = 44) -> str:
        label = " ".join((task.label or task.task_id).split()) or task.task_id
        if len(label) > max_label:
            label = label[: max_label - 3].rstrip() + "..."
        return f"{label} ({task.task_id[:11]})"

    def choose_review_worktree_task(self) -> None:
        try:
            tasks = list_worktree_tasks()
        except WorktreeTaskError as exc:
            self.set_work_status("Worktree tasks unavailable", str(exc), active=False, hold=True)
            return
        if not tasks:
            self.set_work_status(
                "No worktree tasks",
                "Create one from Worktree Tasks -> New Worktree Task..., then reopen Review / Handoff.",
                active=False,
                hold=True,
            )
            return
        if len(tasks) == 1:
            self.review_worktree_task(tasks[0].task_id)
            return
        choices = [self.worktree_task_display_label(task) for task in tasks]
        selected, ok = QInputDialog.getItem(
            self,
            "Review / Handoff",
            "Choose a worktree task:",
            choices,
            0,
            False,
        )
        if not ok or not selected:
            return
        task_by_choice = dict(zip(choices, tasks))
        self.review_worktree_task(task_by_choice[selected].task_id)

    def open_worktree_task_terminal(self, task_id: str) -> None:
        try:
            task = get_worktree_task(task_id)
        except WorktreeTaskError as exc:
            self.set_work_status("Worktree task missing", str(exc), active=False, hold=True)
            return
        self.launch_worktree_task_terminal_for_current_pet(task)

    def open_worktree_task_folder(self, task_id: str) -> None:
        try:
            task = get_worktree_task(task_id)
        except WorktreeTaskError as exc:
            self.set_work_status("Worktree task missing", str(exc), active=False, hold=True)
            return
        result = QProcess.startDetached("xdg-open", [str(task.worktree_path)])
        started = result[0] if isinstance(result, tuple) else bool(result)
        if not started:
            self.set_work_status("Worktree folder", str(task.worktree_path), active=False, hold=True)

    def remove_clean_worktree_task(self, task_id: str) -> None:
        answer = QMessageBox.question(
            self,
            "Remove Worktree Task",
            "Remove this worktree task if it has no uncommitted changes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            task = remove_worktree_task(task_id)
        except WorktreeTaskError as exc:
            self.set_work_status("Worktree cleanup blocked", str(exc), active=False, hold=True)
            return
        self.set_work_status("Worktree task removed", task.task_id, active=False, hold=True)

    def add_worktree_tasks_menu(self, menu: QMenu) -> None:
        if not self.worktree_tasks_enabled:
            return
        worktree_menu = menu.addMenu("Worktree Tasks")
        new_worktree_action = QAction("New Worktree Task...", self)
        new_worktree_action.triggered.connect(self.start_worktree_task)
        worktree_menu.addAction(new_worktree_action)

        review_any_action = QAction("Review / Handoff...", self)
        review_any_action.triggered.connect(self.choose_review_worktree_task)
        worktree_menu.addAction(review_any_action)

        summary_action = QAction("Show Task Summary", self)
        summary_action.triggered.connect(self.show_worktree_task_summary)
        worktree_menu.addAction(summary_action)

        try:
            tasks = list_worktree_tasks()
        except WorktreeTaskError as exc:
            tasks = []
            worktree_menu.addSeparator()
            error_action = QAction(f"Tasks unavailable: {exc}", self)
            error_action.setEnabled(False)
            worktree_menu.addAction(error_action)
        if not tasks:
            worktree_menu.addSeparator()
            empty_action = QAction("No active worktree tasks", self)
            empty_action.setEnabled(False)
            worktree_menu.addAction(empty_action)
            return

        worktree_menu.addSeparator()
        for task in tasks[:5]:
            task_menu = worktree_menu.addMenu(self.worktree_task_display_label(task, max_label=34))

            review_action = QAction("Review / Handoff", self)
            review_action.triggered.connect(
                lambda _checked=False, task_id=task.task_id: self.review_worktree_task(task_id)
            )
            task_menu.addAction(review_action)

            status_action = QAction("Show Status", self)
            status_action.triggered.connect(
                lambda _checked=False, task_id=task.task_id: self.show_worktree_task_status(task_id)
            )
            task_menu.addAction(status_action)

            terminal_action = QAction("Open Terminal", self)
            terminal_action.triggered.connect(
                lambda _checked=False, task_id=task.task_id: self.open_worktree_task_terminal(task_id)
            )
            task_menu.addAction(terminal_action)

            folder_action = QAction("Open Folder", self)
            folder_action.triggered.connect(
                lambda _checked=False, task_id=task.task_id: self.open_worktree_task_folder(task_id)
            )
            task_menu.addAction(folder_action)

            remove_action = QAction("Remove If Clean", self)
            remove_action.triggered.connect(
                lambda _checked=False, task_id=task.task_id: self.remove_clean_worktree_task(task_id)
            )
            task_menu.addAction(remove_action)

    def open_ask_dialog(self) -> None:
        self.open_bubble_reply()

    def open_claude_dialog(self) -> None:
        self.open_bubble_reply(provider=AI_PROVIDER_CLAUDE, prefer_session=False)

    def open_slack_dialog(self, contact: dict[str, str] | None = None, *, send_as: str | None = None) -> None:
        selected_contact = contact or self.current_slack_contact()
        selected_identity = send_as or selected_contact.get("send_as") or self.slack_send_as
        selected_contact = self.slack_contact_for_identity(selected_contact, selected_identity)
        self.set_slack_target(selected_contact)
        self.bubble_reply_slack_contact = self.current_slack_contact()
        self.open_bubble_reply(provider=AI_PROVIDER_SLACK, prefer_session=False)

    def open_reply_from_bubble_button(self) -> None:
        if self.replyable_session_id():
            self.open_bubble_reply(provider=AI_PROVIDER_CODEX, prefer_session=True)
        elif self.slack_status_visible():
            self.open_bubble_reply(provider=AI_PROVIDER_SLACK, prefer_session=False)
        elif self.claude_conversation_active:
            self.open_bubble_reply(provider=AI_PROVIDER_CLAUDE, prefer_session=False)
        else:
            self.open_bubble_reply(provider=AI_PROVIDER_CODEX, prefer_session=False)

    def open_bubble_reply(self, provider: str = AI_PROVIDER_CODEX, *, prefer_session: bool = True) -> None:
        if not self.work_drop_enabled or not self.bubble_reply_enabled or self.thought_bubble is None:
            return
        session_id = self.replyable_session_id() if prefer_session else ""
        provider = provider if provider in REPLY_PROVIDERS else AI_PROVIDER_CODEX
        if session_id:
            provider = AI_PROVIDER_CODEX
        provider_label = self.provider_name(provider)
        slack_contact = self.bubble_reply_slack_contact or self.current_slack_contact()
        slack_label = slack_contact.get("label") or "Slack"
        slack_identity = self.slack_send_as_label(slack_contact)
        reply_headline = (
            "Reply to Codex"
            if session_id
            else "Talking to Claude"
            if provider == AI_PROVIDER_CLAUDE
            else f"Message {slack_label} as {slack_identity}"
            if provider == AI_PROVIDER_SLACK
            else f"Ask {provider_label}"
        )
        reply_meta = "reply to session " + session_id[:8] if session_id else self.bubble_reply_meta(provider)
        reply_placeholder = (
            "Type your response for the waiting Codex session..."
            if session_id
            else "Type a message for Claude..."
            if provider == AI_PROVIDER_CLAUDE
            else f"Type a Slack message to {slack_label} as {slack_identity}..."
            if provider == AI_PROVIDER_SLACK
            else f"Type a request for {provider_label}..."
        )
        self.set_thought_bubble_minimized(False)
        self.thought_bubble.set_reply_available(True)
        self.thought_bubble.set_reply_context(
            headline=reply_headline,
            placeholder=reply_placeholder,
            meta=reply_meta,
        )
        self.thought_bubble.set_status(
            reply_headline,
            active=False,
            visible=True,
            waiting_for_user=True,
            headline=reply_headline,
            detail=(
                "Type your response for the waiting Codex session. Enter sends. Shift+Enter starts a new line."
                if session_id
                else "Type a message for Claude. Enter sends. Shift+Enter starts a new line."
                if provider == AI_PROVIDER_CLAUDE
                else f"Type a Slack message to {slack_label} as {slack_identity}. Enter sends. Shift+Enter starts a new line."
                if provider == AI_PROVIDER_SLACK
                else f"Type a request for {provider_label}. Enter sends. Shift+Enter starts a new line."
            ),
            meta=reply_meta,
        )
        self.thought_bubble.begin_reply()
        self.begin_bubble_reply(prefer_session=bool(session_id), provider=provider)
        self.update_bubble_position()

    def replyable_session_id(self) -> str:
        status = self.last_codex_status
        if status and status.replyable and status.session_id:
            return str(status.session_id)
        return ""

    def begin_bubble_reply(self, *, prefer_session: bool = True, provider: str = AI_PROVIDER_CODEX) -> None:
        if not self.work_drop_enabled or not self.bubble_reply_enabled:
            return
        self.bubble_reply_active = True
        self.bubble_reply_session_id = self.replyable_session_id() if prefer_session else ""
        self.bubble_reply_provider = provider if provider in REPLY_PROVIDERS else AI_PROVIDER_CODEX
        if self.bubble_reply_session_id:
            self.bubble_reply_provider = AI_PROVIDER_CODEX
        if self.thought_bubble is not None:
            self.thought_bubble.set_reply_context(
                headline=self.active_reply_headline(),
                placeholder=(
                    "Type your response for the waiting Codex session..."
                    if self.bubble_reply_session_id
                    else "Type a message for Claude..."
                    if self.bubble_reply_provider == AI_PROVIDER_CLAUDE
                    else (
                        f"Type a Slack message to {(self.bubble_reply_slack_contact or self.current_slack_contact()).get('label') or 'Slack'} "
                        f"as {self.slack_send_as_label(self.bubble_reply_slack_contact or self.current_slack_contact())}..."
                    )
                    if self.bubble_reply_provider == AI_PROVIDER_SLACK
                    else "Type a request for Codex..."
                ),
                meta=self.active_reply_meta(),
            )
        self.set_thought_bubble_minimized(False)
        self.play_reply_animation()
        self.update_bubble_position()

    def cancel_bubble_reply(self) -> None:
        self.bubble_reply_active = False
        self.bubble_reply_session_id = ""
        self.bubble_reply_provider = AI_PROVIDER_CODEX
        self.bubble_reply_slack_contact = None
        if self.motion in {
            self.work_reply_start_animation,
            self.work_reply_animation,
            "phone-reply-start",
            "phone-reply",
        } and self.work_process is None and not self.sequence_locked():
            self.motion = "idle"
            self.action_ticks = 0
            self.set_animation("idle")

    def submit_codex_approval(self, choice: str) -> None:
        if not self.codex_approval_bridge_enabled:
            self.set_work_status(
                "Approval bridge disabled",
                "Approve or deny this action in the Codex terminal.",
                active=False,
                hold=True,
            )
            return
        status = self.last_codex_status
        if not status or status.waiting_kind != "approval":
            self.set_work_status(
                "No approval pending",
                "Codex no longer appears to be waiting for approval.",
                active=False,
                hold=True,
            )
            return
        result = send_codex_terminal_approval(choice)
        if result.ok:
            self.push_work_history(result.detail)
            self.set_work_status("Companion answered Codex", result.detail, active=False, hold=True)
            self.work_status_until = time.time() + 2.5
            QTimer.singleShot(2800, self.tick_codex_status)
            return
        self.push_work_history("Approval bridge failed: " + result.detail)
        self.set_work_status("Approval bridge needs the terminal", result.detail, active=False, hold=True)
        self.work_status_until = time.time() + 4.0
        QTimer.singleShot(4300, self.tick_codex_status)

    def submit_bubble_reply(self, text: str) -> None:
        session_id = self.bubble_reply_session_id
        provider = self.bubble_reply_provider
        slack_contact = self.bubble_reply_slack_contact or self.current_slack_contact()
        self.bubble_reply_session_id = ""
        self.bubble_reply_provider = AI_PROVIDER_CODEX
        self.bubble_reply_slack_contact = None
        if provider == AI_PROVIDER_SLACK:
            if self.thought_bubble is not None:
                self.thought_bubble.finish_reply()
            self.bubble_reply_active = False
            if self.work_process is not None:
                self.pending_slack_message = (text, dict(slack_contact))
                self.push_work_history("Queued Slack message: " + self.clean_final_work_text(text))
                self.set_work_status(
                    "Companion queued Slack",
                    "It will post the message after the current request finishes.",
                    active=True,
                )
                return
            self.start_slack_message(text, contact=slack_contact)
            return
        request = WorkRequest(
            action=ACTION_CUSTOM,
            prompt=text,
            paths=(),
            cwd=self.reply_work_cwd(),
            sandbox=self.bubble_reply_sandbox,
            provider=provider,
            conversation=provider == AI_PROVIDER_CLAUDE,
        )
        if self.thought_bubble is not None:
            self.thought_bubble.finish_reply()
        self.bubble_reply_active = False
        if session_id:
            if self.work_process is not None:
                self.pending_resume_reply = (session_id, text)
                self.push_work_history("Queued session reply: " + self.clean_final_work_text(text))
                self.set_work_status(
                    "Companion queued your reply",
                    "It will send to the waiting Codex session after the current request finishes.",
                    active=True,
                )
                return
            self.start_resume_reply(session_id, text)
            return
        if self.work_process is not None:
            self.pending_reply_request = request
            self.push_work_history("Queued reply: " + self.clean_final_work_text(text))
            provider_name = self.provider_name(provider)
            self.set_work_status(
                "Companion queued your reply",
                f"It will send to {provider_name} after the current request finishes.",
                active=True,
            )
            return
        self.start_work_request(request)

    def reply_work_cwd(self) -> Path:
        if self.work_request is not None:
            return self.work_request.cwd
        return self.default_work_cwd()

    def reply_start_motion_name(self) -> str | None:
        return self.optional_animation(self.work_reply_start_animation, "phone-reply-start")

    def reply_motion_name(self) -> str:
        if self.bubble_reply_provider == AI_PROVIDER_SLACK:
            return self.slack_reply_motion_name()
        return self.first_available_animation(
            self.work_reply_animation,
            "phone-reply",
            self.work_prompt_animation,
            "prompting",
            "review",
            "waiting",
            "idle",
        )

    def slack_reply_motion_name(self) -> str:
        return self.first_available_animation(
            self.slack_send_animation,
            "slack-send",
            self.slack_message_animation,
            "slack-message",
            self.work_reply_animation,
            "phone-reply",
            self.work_prompt_animation,
            "prompting",
            "idle",
        )

    def play_reply_animation(self) -> None:
        if self.sequence_locked() or self.drag_offset is not None:
            return
        self.resting = False
        start = self.reply_start_motion_name()
        if start:
            self.motion = start
            self.action_ticks = self.animation_duration_ticks(start)
        else:
            self.motion = self.reply_motion_name()
            self.action_ticks = 120
        self.set_animation(self.motion)

    def open_drop_dialog(self, paths: list[Path]) -> None:
        if not self.work_drop_enabled or not paths:
            return
        if self.work_process is not None:
            self.set_work_status(
                "Companion is already working",
                "Finish or stop the current Codex request before starting another drop.",
                active=True,
            )
            return
        if self.work_drop_bubble is None:
            return
        self.play_drop_dialog_animation()
        self.work_drop_bubble.set_request(
            paths=paths,
            fallback_cwd=self.default_work_cwd(),
            default_sandbox=self.work_default_sandbox,
            default_provider=self.work_default_provider,
        )
        self.work_drop_bubble.begin_opening()
        self.update_work_drop_bubble_position()

    def submit_work_drop_request(self, request: WorkRequest) -> None:
        if self.work_drop_bubble is not None:
            self.work_drop_bubble.begin_closing()
        self.stop_drop_dialog_animation()
        if self.work_process is not None:
            self.set_work_status(
                "Companion is already working",
                "Finish or stop the current Codex request before starting another drop.",
                active=True,
            )
            return
        self.start_work_request(request)

    def cancel_work_drop_bubble(self) -> None:
        if self.work_drop_bubble is not None:
            self.work_drop_bubble.begin_closing()
        self.stop_drop_dialog_animation()

    def work_output_dir(self) -> Path:
        path = self.pet_dir / self.work_output_dir_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def start_work_request(self, request: WorkRequest) -> None:
        if self.work_process is not None:
            return
        if request.provider == AI_PROVIDER_CLAUDE:
            self.start_claude_request(request)
            return

        output_path = self.work_output_dir() / (
            time.strftime("codex-work-%Y%m%d-%H%M%S") + f"-{os.getpid()}.txt"
        )
        prompt = build_work_prompt(request)
        args = build_codex_exec_args(request, output_path)
        codex_executable = find_codex_executable()
        if codex_executable is None:
            tried = ", ".join(str(path) for path in codex_candidate_paths()[:6])
            detail = "Could not find the Codex CLI executable."
            if tried:
                detail += f" Checked: {tried}"
            detail += " Set CODEX_BINARY=/absolute/path/to/codex or launch Companion from a shell with codex on PATH."
            self.fail_current_work(detail)
            return

        process = QProcess(self)
        process.setProgram(str(codex_executable))
        process.setArguments(args)
        process.setWorkingDirectory(str(request.cwd))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PATH", codex_process_path(codex_executable))
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(self.read_work_stdout)
        process.readyReadStandardError.connect(self.read_work_stderr)
        process.finished.connect(self.finish_work_request)
        process.errorOccurred.connect(self.fail_work_request)

        self.work_process = process
        self.work_request = request
        self.work_output_path = output_path
        self.work_stdout_buffer = ""
        self.work_stderr = ""
        self.work_cancelled = False
        self.drop_hover_active = False
        self.drop_dialog_active = False
        self.set_work_animation(active=True)
        title = short_work_title(request)
        self.push_work_history("Started: " + title)
        self.set_work_status("Companion sent it to Codex", title, active=True)

        process.start()
        if not process.waitForStarted(2500):
            if self.work_process is process:
                self.fail_current_work("Could not start codex exec.")
            return
        process.write(prompt.encode("utf-8"))
        process.closeWriteChannel()

    def start_claude_request(self, request: WorkRequest) -> None:
        if self.work_process is not None:
            return

        output_path = self.work_output_dir() / (
            time.strftime("claude-work-%Y%m%d-%H%M%S") + f"-{os.getpid()}.txt"
        )
        payload = {
            "action": request.action,
            "prompt": request.prompt,
            "paths": [str(path) for path in request.paths],
            "cwd": str(request.cwd),
            "model": self.claude_model,
            "max_tokens": self.claude_max_tokens,
            "output_path": str(output_path),
        }
        if request.conversation:
            self.claude_conversation_active = True
            payload["messages"] = [
                *self.claude_conversation_messages,
                {"role": "user", "content": request.prompt},
            ]

        process = QProcess(self)
        process.setProgram(sys.executable or "/usr/bin/python3")
        process.setArguments(["-m", "hatchpet.claude_bridge"])
        process.setWorkingDirectory(str(request.cwd))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", str(self.pet_dir.parent.parent))
        env.insert("AI_DESKTOP_COMPANION_CLAUDE_MODEL", self.claude_model)
        env.insert("AI_DESKTOP_COMPANION_CLAUDE_MAX_TOKENS", str(self.claude_max_tokens))
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(self.read_work_stdout)
        process.readyReadStandardError.connect(self.read_work_stderr)
        process.finished.connect(self.finish_work_request)
        process.errorOccurred.connect(self.fail_work_request)

        self.work_process = process
        self.work_request = request
        self.work_output_path = output_path
        self.work_stdout_buffer = ""
        self.work_stderr = ""
        self.work_cancelled = False
        self.drop_hover_active = False
        self.drop_dialog_active = False
        self.set_work_animation(active=True)
        title = short_work_title(request)
        self.push_work_history("Started Claude: " + title)
        headline = "Talking to Claude" if request.conversation else "Companion sent it to Claude"
        self.set_work_status(headline, title, active=True)

        process.start()
        if not process.waitForStarted(2500):
            if self.work_process is process:
                self.fail_current_work("Could not start Claude bridge.")
            return
        process.write((json.dumps(payload) + "\n").encode("utf-8"))
        process.closeWriteChannel()

    def start_slack_message(self, text: str, *, contact: dict[str, str] | None = None) -> None:
        slack_contact = contact or self.current_slack_contact()
        if self.work_process is not None:
            self.pending_slack_message = (text, dict(slack_contact))
            return
        if not self.slack_target_configured(slack_contact):
            self.set_work_status(
                "Slack needs a target",
                "Add a contact with conversationId or userId under runtime.aiProviders.slack.contacts.",
                active=False,
                hold=True,
            )
            self.set_work_animation(failed=True)
            return
        self.set_slack_target(slack_contact)

        payload = {
            "action": "send",
            "conversation_id": slack_contact.get("conversation_id", ""),
            "user_id": slack_contact.get("user_id", ""),
            "token_kind": self.slack_contact_send_as(slack_contact),
            "text": text,
        }
        process = QProcess(self)
        process.setProgram(sys.executable or "/usr/bin/python3")
        process.setArguments(["-m", "hatchpet.slack_bridge"])
        process.setWorkingDirectory(str(self.default_work_cwd()))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", str(self.pet_dir.parent.parent))
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(self.read_work_stdout)
        process.readyReadStandardError.connect(self.read_work_stderr)
        process.finished.connect(self.finish_work_request)
        process.errorOccurred.connect(self.fail_work_request)

        self.work_process = process
        self.work_request = None
        self.work_provider_override = AI_PROVIDER_SLACK
        self.work_output_path = None
        self.work_stdout_buffer = ""
        self.work_stderr = ""
        self.work_cancelled = False
        self.work_slack_text = text
        self.work_slack_contact = dict(slack_contact)
        self.work_slack_logged_ts = ""
        self.drop_hover_active = False
        self.drop_dialog_active = False
        self.set_work_animation(active=True)
        self.push_work_history("Started Slack: " + self.clean_final_work_text(text))
        self.set_work_status(
            "Sending Slack message",
            f"Posting to {self.slack_conversation_label} as {self.slack_send_as_label(slack_contact)}.",
            active=True,
        )

        process.start()
        if not process.waitForStarted(2500):
            if self.work_process is process:
                self.fail_current_work("Could not start Slack bridge.")
            return
        process.write((json.dumps(payload) + "\n").encode("utf-8"))
        process.closeWriteChannel()

    def start_github_action(self, action: str) -> None:
        if self.work_process is not None:
            self.set_work_status(
                "GitHub is waiting",
                "Finish the current Companion work before starting a GitHub action.",
                active=False,
                hold=True,
            )
            return
        if not self.github_enabled:
            self.set_work_status(
                "GitHub is disabled",
                "Enable runtime.aiProviders.github in the pet manifest to use GitHub actions.",
                active=False,
                hold=True,
            )
            return

        cwd = self.github_work_cwd()
        payload = {
            "action": action,
            "cwd": str(cwd),
            "remote": self.github_remote,
            "mainBranch": self.github_main_branch,
            "requireCleanTree": self.github_require_clean,
        }
        process = QProcess(self)
        process.setProgram(sys.executable or "/usr/bin/python3")
        process.setArguments(["-m", "hatchpet.github_bridge"])
        process.setWorkingDirectory(str(cwd))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", str(self.pet_dir.parent.parent))
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(self.read_work_stdout)
        process.readyReadStandardError.connect(self.read_work_stderr)
        process.finished.connect(self.finish_work_request)
        process.errorOccurred.connect(self.fail_work_request)

        self.work_process = process
        self.work_request = None
        self.work_provider_override = AI_PROVIDER_GITHUB
        self.work_output_path = None
        self.work_stdout_buffer = ""
        self.work_stderr = ""
        self.work_cancelled = False
        self.work_github_action = action
        self.work_github_cwd = cwd
        self.drop_hover_active = False
        self.drop_dialog_active = False
        self.set_work_animation(active=True)
        title = self.github_action_label(action)
        self.push_work_history("Started GitHub: " + title)
        self.set_work_status("Starting GitHub action", title, active=True)

        process.start()
        if not process.waitForStarted(2500):
            if self.work_process is process:
                self.fail_current_work("Could not start GitHub bridge.")
            return
        process.write((json.dumps(payload) + "\n").encode("utf-8"))
        process.closeWriteChannel()

    def poll_slack_messages(self) -> None:
        if not self.slack_enabled or not self.slack_poll_enabled or not self.slack_target_configured():
            return
        if self.slack_poll_process is not None:
            return

        payload = {
            "action": "history",
            "conversation_id": self.slack_conversation_id,
            "user_id": self.slack_user_id,
            "token_kind": self.slack_send_as,
            "limit": self.slack_history_limit,
        }
        if self.slack_poll_initialized and self.slack_latest_ts:
            payload["oldest"] = self.slack_latest_ts

        process = QProcess(self)
        process.setProgram(sys.executable or "/usr/bin/python3")
        process.setArguments(["-m", "hatchpet.slack_bridge"])
        process.setWorkingDirectory(str(self.default_work_cwd()))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", str(self.pet_dir.parent.parent))
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(self.read_slack_poll_stdout)
        process.readyReadStandardError.connect(self.read_slack_poll_stderr)
        process.finished.connect(self.finish_slack_poll)

        self.slack_poll_process = process
        self.slack_poll_stdout = ""
        self.slack_poll_stderr = ""
        process.start()
        if not process.waitForStarted(1000):
            if self.slack_poll_process is process:
                self.slack_poll_process = None
                process.deleteLater()
            return
        process.write((json.dumps(payload) + "\n").encode("utf-8"))
        process.closeWriteChannel()

    def read_slack_poll_stdout(self) -> None:
        process = self.slack_poll_process
        if process is None:
            return
        chunk = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if chunk:
            self.slack_poll_stdout += chunk

    def read_slack_poll_stderr(self) -> None:
        process = self.slack_poll_process
        if process is None:
            return
        chunk = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        if chunk:
            self.slack_poll_stderr = (self.slack_poll_stderr + "\n" + chunk).strip()[-1200:]

    def finish_slack_poll(self, _exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        process = self.slack_poll_process
        self.slack_poll_process = None
        if process is not None:
            process.deleteLater()

        for raw_line in self.slack_poll_stdout.splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "slack_messages":
                conversation_id = str(event.get("conversation_id") or "").strip()
                if conversation_id:
                    self.remember_slack_conversation_id(conversation_id)
                messages = event.get("messages")
                if isinstance(messages, list):
                    self.handle_slack_messages([item for item in messages if isinstance(item, dict)])

    def handle_slack_messages(self, messages: list[dict]) -> None:
        if not messages:
            self.slack_poll_initialized = True
            return

        latest_seen = self.slack_latest_ts
        new_messages: list[dict] = []
        for message in messages:
            ts = str(message.get("ts") or "").strip()
            if not ts:
                continue
            if self.slack_ts_after(ts, latest_seen):
                latest_seen = ts
            if not self.slack_poll_initialized:
                continue
            if not self.slack_ts_after(ts, self.slack_latest_ts):
                continue
            if ts == self.slack_last_sent_ts:
                continue
            text = str(message.get("text") or "").strip()
            if text:
                new_messages.append(message)

        self.slack_latest_ts = latest_seen
        self.slack_poll_initialized = True
        if not new_messages:
            return

        new_messages.sort(key=lambda item: self.slack_ts_value(str(item.get("ts") or "")))
        for item in new_messages:
            item_text = self.bubble_output_text(str(item.get("text") or ""))
            if not item_text:
                continue
            item_sender = self.slack_sender_label(item) or "Slack"
            self.append_slack_transcript(
                self.current_slack_contact(),
                f"{item_sender} to Companion",
                item_text,
                ts=str(item.get("ts") or "").strip(),
            )
        message = new_messages[-1]
        sender = self.slack_sender_label(message)
        text = self.bubble_output_text(str(message.get("text") or ""))
        if not text:
            return
        self.slack_status_headline = f"Slack from {sender}" if sender else "Slack message"
        self.slack_status_detail = text
        self.slack_status_meta = self.slack_bubble_meta()
        self.slack_status_until = time.time() + self.slack_status_hold_seconds
        self.play_slack_attention_animation()
        if self.thought_bubble is not None:
            self.tick_codex_status()

    def slack_sender_label(self, message: dict) -> str:
        user = str(message.get("user") or "").strip()
        bot = str(message.get("bot_id") or "").strip()
        if user:
            for contact in self.slack_contacts:
                if user == contact.get("user_id"):
                    return contact.get("label") or user
            return user
        if bot:
            return "Slack app"
        return ""

    def slack_ts_value(self, value: str) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def slack_ts_after(self, candidate: str, baseline: str) -> bool:
        return self.slack_ts_value(candidate) > self.slack_ts_value(baseline)

    def play_slack_attention_animation(self) -> None:
        if self.sequence_locked() or self.drag_offset is not None or self.work_process is not None:
            return
        self.resting = False
        self.motion = self.first_available_animation(
            self.slack_message_animation,
            "slack-message",
            self.codex_attention_animation,
            "codex-attention",
            "waving",
            "waiting",
            "idle",
        )
        self.action_ticks = max(90, self.animation_duration_ticks(self.motion))
        self.set_animation(self.motion)

    def start_resume_reply(self, session_id: str, text: str) -> None:
        if self.work_process is not None:
            return

        output_path = self.work_output_dir() / (
            time.strftime("codex-resume-%Y%m%d-%H%M%S") + f"-{os.getpid()}.txt"
        )
        args = build_codex_exec_resume_args(session_id, output_path)
        codex_executable = find_codex_executable()
        if codex_executable is None:
            tried = ", ".join(str(path) for path in codex_candidate_paths()[:6])
            detail = "Could not find the Codex CLI executable."
            if tried:
                detail += f" Checked: {tried}"
            detail += " Set CODEX_BINARY=/absolute/path/to/codex or launch Companion from a shell with codex on PATH."
            self.fail_current_work(detail)
            return

        process = QProcess(self)
        process.setProgram(str(codex_executable))
        process.setArguments(args)
        process.setWorkingDirectory(str(self.default_work_cwd()))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PATH", codex_process_path(codex_executable))
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(self.read_work_stdout)
        process.readyReadStandardError.connect(self.read_work_stderr)
        process.finished.connect(self.finish_work_request)
        process.errorOccurred.connect(self.fail_work_request)

        self.work_process = process
        self.work_request = None
        self.work_resume_session_id = session_id
        self.work_output_path = output_path
        self.work_stdout_buffer = ""
        self.work_stderr = ""
        self.work_cancelled = False
        self.drop_hover_active = False
        self.drop_dialog_active = False
        self.set_work_animation(active=True)
        title = "Reply to session " + session_id[:8]
        self.push_work_history("Started: " + title)
        self.set_work_status("Companion replied to Codex", title, active=True)

        process.start()
        if not process.waitForStarted(2500):
            if self.work_process is process:
                self.fail_current_work("Could not start codex exec resume.")
            return
        payload = text if text.endswith("\n") else text + "\n"
        process.write(payload.encode("utf-8"))
        process.closeWriteChannel()

    def read_work_stdout(self) -> None:
        process = self.work_process
        if process is None:
            return
        chunk = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if not chunk:
            return
        self.work_stdout_buffer += chunk
        lines = self.work_stdout_buffer.splitlines(keepends=True)
        self.work_stdout_buffer = ""
        for line in lines:
            if line.endswith("\n") or line.endswith("\r"):
                self.consume_work_output_line(line.strip())
            else:
                self.work_stdout_buffer = line

    def read_work_stderr(self) -> None:
        process = self.work_process
        if process is None:
            return
        chunk = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        if chunk:
            self.work_stderr = (self.work_stderr + "\n" + chunk).strip()[-1200:]

    def remember_slack_work_event(self, line: str) -> None:
        if self.work_provider_override != AI_PROVIDER_SLACK:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        if event.get("type") != "slack_sent":
            return
        message_ts = str(event.get("message_ts") or "").strip()
        conversation_id = str(event.get("conversation_id") or "").strip()
        if conversation_id:
            self.remember_slack_conversation_id(conversation_id)
        if message_ts:
            self.slack_last_sent_ts = message_ts
            if self.slack_ts_after(message_ts, self.slack_latest_ts):
                self.slack_latest_ts = message_ts
                self.slack_poll_initialized = True
            if self.work_slack_text and message_ts != self.work_slack_logged_ts:
                contact = self.work_slack_contact or self.current_slack_contact()
                self.append_slack_transcript(
                    contact,
                    f"{self.slack_send_as_label(contact)} to {contact.get('label') or 'Slack'}",
                    self.work_slack_text,
                    ts=message_ts,
                )
                self.work_slack_logged_ts = message_ts

    def consume_work_output_line(self, line: str) -> None:
        self.remember_slack_work_event(line)
        progress = progress_from_json_line(line)
        if progress is None:
            return
        self.set_work_status(progress.headline, progress.detail, active=not progress.done, hold=progress.done)
        if progress.done:
            self.push_work_history(("Failed: " if progress.failed else "Finished: ") + progress.detail)

    def finish_work_request(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        request = self.work_request
        resume_session_id = self.work_resume_session_id
        output_path = self.work_output_path
        process = self.work_process
        provider_override = self.work_provider_override
        pending_reply = self.pending_reply_request
        pending_resume_reply = self.pending_resume_reply
        pending_slack_message = self.pending_slack_message
        was_cancelled = self.work_cancelled
        self.work_process = None
        self.work_resume_session_id = ""
        self.pending_reply_request = None
        self.pending_resume_reply = None
        self.pending_slack_message = None
        self.work_slack_text = ""
        self.work_slack_contact = None
        self.work_slack_logged_ts = ""
        github_action = self.work_github_action
        self.work_github_action = ""
        self.work_github_cwd = None
        final_text = ""
        if output_path is not None and output_path.exists():
            try:
                final_text = output_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                final_text = ""

        if self.work_cancelled:
            provider_name = self.work_provider_label(request)
            self.set_work_status(
                "Companion stopped the work",
                f"The {provider_name} request was cancelled.",
                active=False,
                hold=True,
            )
            self.set_work_animation(failed=True)
        elif exit_code == 0:
            if provider_override == AI_PROVIDER_SLACK:
                title = "Slack message"
            elif provider_override == AI_PROVIDER_GITHUB:
                title = self.github_action_label(github_action)
            else:
                title = short_work_title(request) if request else "Reply to session " + resume_session_id[:8]
            provider_name = self.work_provider_label(request)
            if provider_override == AI_PROVIDER_SLACK:
                detail = self.work_detail or f"Message posted to {self.slack_conversation_label}."
            elif provider_override == AI_PROVIDER_GITHUB:
                detail = self.work_detail or "GitHub completed the action."
            else:
                detail = self.bubble_output_text(final_text) or f"{provider_name} completed the request."
            self.push_work_history("Completed: " + title)
            if request and request.provider == AI_PROVIDER_CLAUDE and request.conversation:
                self.remember_claude_exchange(request.prompt, final_text)
            if resume_session_id:
                headline = "Companion replied to Codex"
            elif provider_override == AI_PROVIDER_SLACK:
                headline = "Slack message sent"
            elif provider_override == AI_PROVIDER_GITHUB:
                headline = "GitHub action finished"
            elif request and request.provider == AI_PROVIDER_CLAUDE:
                headline = "Claude finished"
            else:
                headline = "Companion finished"
            self.set_work_status(headline, detail, active=False, hold=True)
            self.set_work_animation(done=True)
        else:
            provider_name = self.work_provider_label(request)
            if provider_override in {AI_PROVIDER_SLACK, AI_PROVIDER_GITHUB} and self.work_detail:
                detail = self.work_detail
            else:
                detail = self.clean_final_work_text(final_text or self.work_stderr) or f"{provider_name} exited with {exit_code}."
            self.push_work_history("Failed: " + detail)
            self.set_work_status(f"{provider_name} hit an error", detail, active=False, hold=True)
            self.set_work_animation(failed=True)

        if process is not None:
            process.deleteLater()
        self.work_provider_override = ""
        if pending_resume_reply is not None and not was_cancelled:
            session_id, text = pending_resume_reply
            QTimer.singleShot(350, lambda session_id=session_id, text=text: self.start_resume_reply(session_id, text))
        elif pending_reply is not None and not was_cancelled:
            QTimer.singleShot(350, lambda request=pending_reply: self.start_work_request(request))
        elif pending_slack_message is not None and not was_cancelled:
            text, contact = pending_slack_message
            QTimer.singleShot(350, lambda text=text, contact=contact: self.start_slack_message(text, contact=contact))

    def fail_work_request(self, _error: QProcess.ProcessError) -> None:
        if self.work_process is None:
            return
        self.fail_current_work(self.work_process.errorString() or "codex exec could not continue.")

    def fail_current_work(self, detail: str) -> None:
        process = self.work_process
        self.work_process = None
        self.work_resume_session_id = ""
        self.work_provider_override = ""
        self.work_slack_text = ""
        self.work_slack_contact = None
        self.work_slack_logged_ts = ""
        self.work_github_action = ""
        self.work_github_cwd = None
        self.push_work_history("Failed: " + detail)
        self.set_work_status("Companion hit an error", detail, active=False, hold=True)
        self.set_work_animation(failed=True)
        if process is not None:
            process.deleteLater()

    def cancel_work_request(self) -> None:
        if self.work_process is None:
            return
        provider_name = self.work_provider_label(self.work_request)
        self.work_cancelled = True
        self.work_process.kill()
        self.push_work_history("Stopped by user")
        self.set_work_status("Companion stopped the work", f"The {provider_name} request was cancelled.", active=False, hold=True)
        self.set_work_animation(failed=True)

    def remember_claude_exchange(self, user_text: str, assistant_text: str) -> None:
        user_text = user_text.strip()
        assistant_text = assistant_text.strip()
        if not user_text:
            return
        self.claude_conversation_active = True
        self.claude_conversation_messages.append({"role": "user", "content": user_text})
        if assistant_text:
            self.claude_conversation_messages.append({"role": "assistant", "content": assistant_text})
        self.trim_claude_conversation()

    def trim_claude_conversation(self, max_messages: int = 24, max_chars: int = 80_000) -> None:
        messages = self.claude_conversation_messages[-max_messages:]
        total = 0
        kept: list[dict[str, str]] = []
        for item in reversed(messages):
            content = str(item.get("content") or "")
            if kept and total + len(content) > max_chars:
                break
            role = str(item.get("role") or "")
            if role in {"user", "assistant"} and content:
                kept.append({"role": role, "content": content})
                total += len(content)
        kept.reverse()
        while kept and kept[0]["role"] != "user":
            kept.pop(0)
        self.claude_conversation_messages = kept

    def end_claude_conversation(self) -> None:
        self.claude_conversation_active = False
        self.claude_conversation_messages = []
        if self.bubble_reply_active and self.bubble_reply_provider == AI_PROVIDER_CLAUDE:
            self.cancel_bubble_reply()
            if self.thought_bubble is not None:
                self.thought_bubble.finish_reply()
        self.set_work_status("Claude conversation ended", "New Claude messages will start a fresh conversation.", active=False, hold=True)

    def clean_final_work_text(self, text: str) -> str:
        cleaned = " ".join(text.split())
        if len(cleaned) <= 260:
            return cleaned
        return cleaned[:257].rstrip() + "..."

    def set_work_animation(self, *, active: bool = False, done: bool = False, failed: bool = False) -> None:
        if self.sequence_locked() or self.drag_offset is not None:
            return
        if failed and "failed" in self.animations:
            self.force_behavior("failed", 90)
        elif done and "happy" in self.animations:
            self.force_behavior("happy", 70)
        elif active:
            if self.work_provider_override == AI_PROVIDER_SLACK:
                motion = self.slack_reply_motion_name()
            elif self.work_provider_override == AI_PROVIDER_GITHUB:
                motion = self.first_available_animation(
                    self.github_action_animation,
                    "github-action",
                    self.work_laptop_animation,
                    "work-laptop",
                    "review",
                    "waiting",
                )
            elif self.work_request is not None and self.work_request.paths:
                motion = self.first_available_animation(self.work_review_animation, "file-review", "review", "waiting")
            elif (self.work_request is not None and not self.work_request.paths) or self.work_resume_session_id:
                motion = self.reply_motion_name()
            else:
                motion = self.first_available_animation("review", "waiting", "idle")
            self.resting = False
            self.motion = motion
            self.action_ticks = 80
            self.set_animation(motion)

    def first_available_animation(self, *names: str) -> str:
        for name in names:
            if name in self.animations:
                return name
        return "idle"

    def optional_animation(self, *names: str) -> str | None:
        for name in names:
            if name in self.animations:
                return name
        return None

    def rest_motion_name(self) -> str:
        return self.first_available_animation(self.rest_animation, "sleeping", "resting", "sitting", "idle")

    def rest_enter_motion_name(self) -> str | None:
        return self.optional_animation(self.rest_enter_animation, "rest-enter-bed")

    def laptop_motion_name(self) -> str:
        return self.first_available_animation(self.work_laptop_animation, "work-laptop", "sitting", "review", "idle")

    def codex_attention_motion_name(self) -> str:
        return self.first_available_animation(self.codex_attention_animation, "codex-attention", "waiting", "idle")

    def idle_pocket_motion_name(self) -> str | None:
        return self.optional_animation(self.idle_pocket_animation, "idle-pocket")

    def ceiling_grab_motion_name(self) -> str | None:
        return self.optional_animation(self.ceiling_grab_animation, "ceiling-grab")

    def ceiling_hold_motion_name(self) -> str:
        return self.first_available_animation(self.ceiling_hold_animation, "ceiling-hold", "picked-up", "idle")

    def ceiling_release_motion_name(self) -> str | None:
        return self.optional_animation(self.ceiling_release_animation, "ceiling-release")

    def ceiling_hold_duration_ticks(self) -> int:
        return random.randint(self.ceiling_min_hold_ticks, self.ceiling_max_hold_ticks)

    def ceiling_x(self, x: int, rect: QRect | None = None) -> int:
        rect = rect or self.screen_rect()
        return max(rect.left(), min(rect.right() - self.render_width, x))

    def can_attach_to_ceiling(self, y: int | None = None) -> bool:
        if not self.ceiling_hold_enabled or self.ceiling_cooldown_ticks > 0:
            return False
        if self.drop_dialog_active or self.sequence_locked():
            return False
        rect = self.screen_rect()
        candidate_y = self.y() if y is None else y
        return self.pet_visual_top(y=candidate_y) <= rect.top() + self.ceiling_trigger_margin

    def start_ceiling_hold(self, *, x: int | None = None) -> None:
        rect = self.screen_rect()
        self.drag_offset = None
        self.resting = False
        self.ceiling_hold_active = True
        self.ceiling_hold_origin_x = self.ceiling_x(self.x() if x is None else x, rect)
        self.glider_rescue_active = False
        self.fall_velocity_y = 0.0
        self.drop_height = 0.0
        self.move(self.ceiling_hold_origin_x, self.ceiling_y())
        grab = self.ceiling_grab_motion_name()
        if grab:
            self.motion = grab
            self.action_ticks = self.animation_duration_ticks(grab)
        else:
            self.motion = self.ceiling_hold_motion_name()
            self.action_ticks = self.ceiling_hold_duration_ticks()
        self.set_animation(self.motion)
        self.update_bubble_position()

    def begin_ceiling_release(self) -> None:
        release = self.ceiling_release_motion_name()
        if release:
            self.motion = release
            self.action_ticks = self.animation_duration_ticks(release)
            self.set_animation(release)
        else:
            self.begin_ceiling_fall()

    def begin_ceiling_fall(self) -> None:
        self.ceiling_hold_active = False
        self.ceiling_cooldown_ticks = self.ceiling_post_release_cooldown_ticks
        ground = self.ground_y()
        start_y = min(float(ground), float(self.y() + self.ceiling_release_drop_offset))
        self.motion = "falling"
        self.action_ticks = 0
        self.fall_y = start_y
        self.fall_start_y = start_y
        self.fall_velocity_y = 0.0
        self.drop_height = max(0.0, float(ground - start_y))
        self.glider_rescue_active = False
        self.glider_origin_x = self.x()
        self.glider_sway_phase = 0.0
        self.fall_total_ticks = self.fall_duration_ticks(self.drop_height)
        self.fall_ticks_elapsed = 0
        self.post_action_cooldown_ticks = 0
        self.move(self.x(), int(round(self.fall_y)))
        self.set_animation("falling" if "falling" in self.animations else "jumping")
        self.update_bubble_position()

    def glider_deploy_motion_name(self) -> str | None:
        return self.optional_animation(self.glider_deploy_animation, "glider-deploy")

    def glider_motion_name(self) -> str:
        return self.first_available_animation(self.glider_animation, "gliding", "falling", "jumping", "idle")

    def glider_landing_motion_name(self) -> str | None:
        return self.optional_animation(self.glider_landing_animation, "glider-landing")

    def can_start_glider_rescue(self) -> bool:
        return (
            self.glider_enabled
            and not self.glider_rescue_active
            and self.motion == "falling"
            and self.drop_height >= self.glider_min_drop_height
            and self.fall_y < self.ground_y() - 18
            and self.drag_offset is None
            and not self.drop_dialog_active
        )

    def start_glider_rescue(self) -> None:
        deploy = self.glider_deploy_motion_name()
        self.resting = False
        self.glider_rescue_active = True
        self.glider_origin_x = self.x()
        self.glider_sway_phase = 0.0
        self.fall_velocity_y = 0.0
        self.motion = deploy or self.glider_motion_name()
        self.action_ticks = self.animation_duration_ticks(deploy) if deploy else 0
        self.set_animation(self.motion)

    def begin_glider_landing(self) -> None:
        ground = self.ground_y()
        self.fall_y = float(ground)
        self.fall_velocity_y = 0.0
        landing = self.glider_landing_motion_name()
        if landing:
            self.motion = landing
            self.action_ticks = self.animation_duration_ticks(landing) + self.landing_hold_ticks
            self.set_animation(landing)
        else:
            self.glider_rescue_active = False
            self.begin_landing("landing-soft", self.soft_landing_ticks, "idle")

    def enter_rest_mode(self, *, reason: str = "") -> None:
        if self.sequence_locked() or self.drag_offset is not None:
            return
        self.limit_rest_active = bool(reason)
        self.paused = False
        self.resting = True
        rest_enter = self.rest_enter_motion_name()
        self.motion = rest_enter or self.rest_motion_name()
        self.action_ticks = self.animation_duration_ticks(rest_enter) if rest_enter else 0
        self.set_animation(self.motion)
        if reason:
            self.set_work_status("Companion is resting", reason, active=False, hold=True)

    def leave_rest_mode(self) -> None:
        self.limit_rest_active = False
        self.resting = False
        self.motion = "idle"
        self.action_ticks = 0
        self.set_animation("idle")

    def usage_limits_exhausted(self, usage: CodexUsageStatus) -> bool:
        if not usage.available:
            return False
        windows = [usage.primary, usage.secondary]
        return any(window is not None and window.left_percent is not None and window.left_percent <= 0.5 for window in windows)

    def sync_pet_with_codex_status(self, status) -> None:
        if (
            self.work_process is not None
            or self.drop_hover_active
            or self.drop_dialog_active
            or self.bubble_reply_active
            or self.resting
            or self.sequence_locked()
            or self.drag_offset is not None
            or self.action_ticks > 0
        ):
            return
        if status.waiting_for_user:
            self.resting = False
            self.motion = self.codex_attention_motion_name()
            self.action_ticks = 120
            self.set_animation(self.motion)
        elif status.active:
            self.resting = False
            self.motion = self.laptop_motion_name()
            self.action_ticks = 120
            self.set_animation(self.motion)

    def play_drop_hover_animation(self) -> None:
        if self.sequence_locked() or self.drag_offset is not None or self.work_process is not None:
            return
        motion = self.first_available_animation(
            self.work_receive_animation,
            "file-receive",
            self.work_review_animation,
            "file-review",
            "review",
            "waiting",
        )
        self.drop_hover_active = True
        self.resting = False
        self.motion = motion
        self.action_ticks = 36
        self.set_animation(motion)

    def stop_drop_hover_animation(self) -> None:
        if not self.drop_hover_active:
            return
        self.drop_hover_active = False
        if not self.drop_dialog_active and self.work_process is None and not self.sequence_locked():
            self.motion = "idle"
            self.action_ticks = 0
            self.set_animation("idle")

    def play_drop_dialog_animation(self) -> None:
        if self.sequence_locked() or self.drag_offset is not None or self.work_process is not None:
            return
        motion = self.first_available_animation(
            self.work_inspect_animation,
            "file-inspect",
            self.work_review_animation,
            "file-review",
            "review",
            "waiting",
        )
        self.drop_dialog_active = True
        self.drop_hover_active = False
        self.resting = False
        self.motion = motion
        self.action_ticks = 999_999
        self.set_animation(motion)

    def stop_drop_dialog_animation(self) -> None:
        if not self.drop_dialog_active:
            return
        self.drop_dialog_active = False
        if self.work_process is None and not self.sequence_locked():
            self.motion = "idle"
            self.action_ticks = 0
            self.set_animation("idle")

    def dropped_paths_from_event(self, event) -> list[Path]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        paths = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile()).expanduser()
            if path.exists():
                paths.append(path.resolve())
        return paths

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self.work_drop_enabled and self.dropped_paths_from_event(event):
            self.play_drop_hover_animation()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self.work_drop_enabled and self.dropped_paths_from_event(event):
            if not self.drop_hover_active and not self.drop_dialog_active:
                self.play_drop_hover_animation()
            event.acceptProposedAction()
        else:
            self.stop_drop_hover_animation()
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.stop_drop_hover_animation()
        event.accept()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self.dropped_paths_from_event(event)
        if paths:
            event.acceptProposedAction()
            self.open_drop_dialog(paths)
        else:
            event.ignore()

    def show_usage_meter(self, *, periodic: bool = False) -> None:
        if self.usage_monitor is None or self.usage_meter is None:
            return
        if self.drag_offset is not None or self.sequence_locked():
            return
        if periodic and (self.usage_meter.isVisible() or self.usage_pending):
            return
        hold_ms = int(round(self.usage_visible_seconds * 1000))
        self.usage_pending = True
        self.play_usage_meter_animation(hold_ms + self.usage_activation_delay_ms)
        QTimer.singleShot(
            self.usage_activation_delay_ms,
            lambda: self.open_usage_meter_after_button(hold_ms),
        )

    def open_usage_meter_after_button(self, hold_ms: int) -> None:
        self.usage_pending = False
        if self.usage_monitor is None or self.usage_meter is None:
            return
        if self.drag_offset is not None or self.sequence_locked():
            return
        usage = self.usage_monitor.poll()
        self.usage_meter.show_usage(usage, hold_ms=hold_ms)
        self.update_usage_meter_position()
        if self.rest_on_limit_exhausted and self.usage_limits_exhausted(usage):
            self.enter_rest_mode(reason="Codex usage limit reached")

    def play_usage_meter_animation(self, hold_ms: int) -> None:
        if "usage-meter" not in self.animations or self.sequence_locked() or self.drag_offset is not None:
            return
        animation_ticks = self.animation_duration_ticks("usage-meter")
        hold_ticks = max(animation_ticks, int(round(hold_ms / 33)))
        self.resting = False
        self.motion = "usage-meter"
        self.action_ticks = hold_ticks
        self.post_action_cooldown_ticks = max(self.post_action_cooldown_ticks, 18)
        self.set_animation("usage-meter")

    def set_animation(self, name: str) -> None:
        if name == self.current_animation or name not in self.animations:
            return
        self.current_animation = name
        self.current_frame = 0
        self.frame_accumulator = 0.0
        self.update()

    def animation_duration_ticks(self, name: str) -> int:
        animation = self.animations.get(name)
        if not animation:
            return 1
        fps = max(1.0, float(animation.get("fps", 8)))
        frames = max(1, int(animation.get("frames", 1)))
        return max(1, math.ceil((frames / fps) / 0.033))

    def landing_duration_ticks(self, name: str, configured_ticks: int) -> int:
        return max(configured_ticks, self.animation_duration_ticks(name) + self.landing_hold_ticks)

    def estimated_physics_fall_ticks(self, distance: float) -> int:
        y = 0.0
        velocity = 0.0
        ticks = 0
        while y < distance and ticks < 240:
            velocity = min(self.fall_max_velocity, velocity + self.fall_gravity)
            y += velocity
            ticks += 1
        return max(1, ticks)

    def fall_duration_ticks(self, distance: float) -> int:
        return max(
            self.estimated_physics_fall_ticks(distance),
            self.animation_duration_ticks("falling"),
        )

    def sequence_locked(self) -> bool:
        return self.ceiling_hold_active or self.glider_rescue_active or self.motion in {
            "falling",
            "landing-soft",
            "landing-hard",
            self.rest_enter_animation,
            self.glider_deploy_animation,
            self.glider_animation,
            self.glider_landing_animation,
            self.ceiling_grab_animation,
            self.ceiling_hold_animation,
            self.ceiling_release_animation,
        }

    def begin_landing(self, name: str, configured_ticks: int, fallback: str) -> None:
        self.glider_rescue_active = False
        self.ceiling_hold_active = False
        self.motion = name
        self.action_ticks = self.landing_duration_ticks(name, configured_ticks)
        self.fall_velocity_y = 0.0
        self.set_animation(name if name in self.animations else fallback)

    def tick_ceiling_hold(self, rect: QRect, x: int) -> bool:
        if not self.ceiling_hold_active:
            return False

        ceiling_x = self.ceiling_x(self.ceiling_hold_origin_x, rect)
        ceiling_y = self.ceiling_y()
        grab = self.ceiling_grab_motion_name()
        hold = self.ceiling_hold_motion_name()
        release = self.ceiling_release_motion_name()

        self.energy = max(0.0, self.energy - 0.025)
        self.move(ceiling_x, ceiling_y)
        self.update_bubble_position()

        if release and self.motion == release:
            self.set_animation(release)
            if self.action_ticks <= 0:
                self.begin_ceiling_fall()
            return True

        if grab and self.motion == grab:
            self.set_animation(grab)
            if self.action_ticks <= 0:
                self.motion = hold
                self.action_ticks = self.ceiling_hold_duration_ticks()
                self.set_animation(hold)
            return True

        self.motion = hold
        self.set_animation(hold)
        if self.action_ticks <= 0:
            self.begin_ceiling_release()
        return True

    def tick_glider_rescue(self, rect: QRect, x: int) -> bool:
        if not self.glider_rescue_active:
            return False

        ground = self.ground_y()
        deploy = self.glider_deploy_motion_name()
        landing = self.glider_landing_motion_name()

        if landing and self.motion == landing:
            self.set_animation(landing)
            self.move(x, ground)
            self.update_bubble_position()
            if self.action_ticks <= 0:
                self.glider_rescue_active = False
                self.motion = "idle"
                self.post_action_cooldown_ticks = self.post_landing_cooldown_ticks
                self.set_animation("idle")
            return True

        self.energy = max(0.0, self.energy - 0.015)

        if deploy and self.motion == deploy:
            descent = self.glider_deploy_descent_speed
            self.set_animation(deploy)
            if self.action_ticks <= 0:
                self.motion = self.glider_motion_name()
                self.set_animation(self.motion)
        else:
            descent = self.glider_descent_speed
            self.motion = self.glider_motion_name()
            self.set_animation(self.motion)
            self.glider_sway_phase += 0.09
            sway = int(round(math.sin(self.glider_sway_phase) * self.glider_sway_pixels))
            x = max(rect.left(), min(rect.right() - self.render_width, self.glider_origin_x + sway))

        next_y = min(float(ground), self.fall_y + descent)
        self.fall_velocity_y = next_y - self.fall_y
        self.fall_y = next_y
        self.move(x, int(round(self.fall_y)))
        self.update_bubble_position()
        if self.fall_y >= ground:
            self.begin_glider_landing()
        return True

    def tick_animation(self) -> None:
        if self.paused:
            return
        animation = self.animations[self.current_animation]
        fps = float(animation.get("fps", 8))
        frames = int(animation["frames"])
        should_loop = bool(animation.get("loop", True))
        self.frame_accumulator += 33 / 1000
        frame_interval = 1 / fps
        while self.frame_accumulator >= frame_interval:
            self.frame_accumulator -= frame_interval
            if should_loop:
                self.current_frame = (self.current_frame + 1) % frames
            elif self.current_frame < frames - 1:
                self.current_frame += 1
            else:
                self.frame_accumulator = 0.0
                break
        self.update()

    def tick_motion(self) -> None:
        if self.paused or self.drag_offset is not None:
            return

        rect = self.screen_rect()
        x = self.x()
        y = self.y()
        self.post_action_cooldown_ticks = max(0, self.post_action_cooldown_ticks - 1)
        self.ceiling_cooldown_ticks = max(0, self.ceiling_cooldown_ticks - 1)

        if self.resting:
            self.energy = min(100.0, self.energy + 0.08)
            rest_enter = self.rest_enter_motion_name()
            if rest_enter and self.motion == rest_enter:
                self.action_ticks = max(0, self.action_ticks - 1)
                self.set_animation(rest_enter)
                if self.action_ticks <= 0:
                    self.motion = self.rest_motion_name()
                    self.set_animation(self.motion)
            else:
                self.motion = self.rest_motion_name()
                self.set_animation(self.motion)
            self.move(x, self.ground_y())
            self.update_bubble_position()
            return

        self.action_ticks = max(0, self.action_ticks - 1)

        if self.bubble_reply_active:
            self.energy = min(100.0, self.energy + 0.02)
            start = self.reply_start_motion_name()
            if start and self.motion == start and self.action_ticks > 0:
                self.set_animation(start)
            else:
                self.motion = self.reply_motion_name()
                self.set_animation(self.motion)
            self.move(x, self.ground_y())
            self.update_bubble_position()
            return

        if self.motion == "walk":
            self.energy = max(0.0, self.energy - 0.03)
            proposed_x = x + int(round(self.velocity_x))
            desktop_rect = self.virtual_screen_rect()
            if proposed_x <= desktop_rect.left():
                x = desktop_rect.left()
                self.direction = 1
                self.velocity_x = self.speed
            elif proposed_x + self.render_width >= desktop_rect.right():
                x = desktop_rect.right() - self.render_width
                self.direction = -1
                self.velocity_x = -self.speed
            elif self.window_anchor_is_on_screen(x=proposed_x, y=y):
                x = proposed_x
            else:
                self.direction *= -1
                self.velocity_x = self.speed * self.direction

            rect = self.screen_rect(x=x, y=y)
            ground = self.ground_y(rect)
            y = ground
            self.set_animation("running-right" if self.direction > 0 else "running-left")
            self.move(x, y)
            self.update_bubble_position()
            return

        if self.motion == "jump":
            self.energy = max(0.0, self.energy - 0.12)
            progress = 1 - (self.action_ticks / 34)
            hop = -int(math.sin(max(0, min(1, progress)) * math.pi) * 42)
            self.set_animation("jumping")
            self.move(x, self.ground_y() + hop)
            self.update_bubble_position()
            if self.action_ticks <= 0:
                self.motion = "idle"
            return

        if self.tick_ceiling_hold(rect, x):
            return

        if self.tick_glider_rescue(rect, x):
            return

        if self.motion == "falling":
            self.energy = max(0.0, self.energy - 0.04)
            ground = self.ground_y()
            self.fall_ticks_elapsed += 1
            if self.fall_ticks_elapsed >= self.fall_total_ticks:
                self.fall_y = float(ground)
                if self.drop_height >= self.hard_drop_height:
                    self.begin_landing("landing-hard", self.hard_landing_ticks, "failed")
                else:
                    self.begin_landing("landing-soft", self.soft_landing_ticks, "idle")
            else:
                progress = max(0.0, min(1.0, self.fall_ticks_elapsed / self.fall_total_ticks))
                eased = progress * progress
                next_y = self.fall_start_y + (ground - self.fall_start_y) * eased
                self.fall_velocity_y = next_y - self.fall_y
                self.fall_y = next_y
                self.set_animation("falling" if "falling" in self.animations else "jumping")
            self.move(x, int(round(self.fall_y)))
            self.update_bubble_position()
            return

        if self.motion in {"landing-soft", "landing-hard"}:
            self.set_animation(self.motion if self.motion in self.animations else "idle")
            self.move(x, self.ground_y())
            self.update_bubble_position()
            if self.action_ticks <= 0:
                self.motion = "idle"
                self.post_action_cooldown_ticks = self.post_landing_cooldown_ticks
                self.set_animation("idle")
            return

        if self.motion in {"sitting", "work-laptop"}:
            self.energy = min(100.0, self.energy + 0.16)
        elif self.motion == "happy":
            self.energy = min(100.0, self.energy + 0.04)
        elif self.motion == "idle":
            self.energy = min(100.0, self.energy + 0.03)

        self.set_animation(self.motion if self.motion in self.animations else "idle")
        self.move(x, self.ground_y())
        self.update_bubble_position()

    def choose_next_behavior(self) -> None:
        if (
            self.paused
            or self.drag_offset is not None
            or self.action_ticks > 0
            or self.post_action_cooldown_ticks > 0
            or self.sequence_locked()
            or self.work_process is not None
            or self.bubble_reply_active
            or self.pending_reply_request is not None
            or self.pending_slack_message is not None
            or self.drop_hover_active
            or self.drop_dialog_active
        ):
            return
        if self.resting:
            return

        if self.slack_status_visible():
            self.play_slack_attention_animation()
            return

        if self.energy < 30:
            self.motion = self.rest_motion_name()
            self.action_ticks = random.randint(115, 175)
            return

        pocket_motion = self.idle_pocket_motion_name()
        if pocket_motion and random.random() < self.idle_pocket_chance:
            self.motion = pocket_motion
            self.action_ticks = random.randint(self.idle_pocket_min_ticks, self.idle_pocket_max_ticks)
            return

        roll = random.random()
        if roll < 0.26:
            self.motion = "walk"
            self.direction = random.choice([-1, 1])
            self.velocity_x = self.speed * self.direction
        elif roll < 0.54:
            self.motion = "idle"
        elif roll < 0.61:
            self.motion = "happy"
            self.action_ticks = 45
        elif roll < 0.75:
            self.motion = "waving"
            self.action_ticks = 55
        elif roll < 0.88:
            self.motion = "waiting"
            self.action_ticks = 65
        elif roll < 0.95:
            self.motion = "review"
            self.action_ticks = 65
        elif roll < 0.985:
            self.motion = self.laptop_motion_name()
            self.action_ticks = 110
        else:
            self.motion = "jump"
            self.action_ticks = 34

    def paintEvent(self, _event) -> None:  # noqa: N802
        animation = self.animations[self.current_animation]
        row = int(animation["row"])
        frame = self.current_frame % int(animation["frames"])
        source_rect = QRect(frame * self.cell_width, row * self.cell_height, self.cell_width, self.cell_height)
        target_rect = self.sprite_rect()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if self.current_animation == self.rest_motion_name() and self.sleep_pulse_enabled:
            phase = math.sin(time.time() * math.tau * 0.42)
            scale = 1.0 + 0.018 * phase
            target = QRectF(target_rect)
            dx = target.width() * (scale - 1.0) / 2
            dy = target.height() * (scale - 1.0) / 2
            target.adjust(-dx, -dy, dx, dy)
            painter.drawPixmap(target, self.spritesheet, QRectF(source_rect))
            if self.sleep_snooze_marks_enabled:
                self.draw_sleep_marks(painter)
        else:
            painter.drawPixmap(target_rect, self.spritesheet, QRectF(source_rect))
        if self.thought_dots_visible():
            self.draw_thought_dots(painter)

    def draw_sleep_marks(self, painter: QPainter) -> None:
        phase = (time.time() * 0.7) % 1.0
        scale = max(0.75, min(self.render_width / self.cell_width, self.render_height / self.cell_height))
        base_x = self.width() * 0.61
        base_y = self.thought_dot_headroom + self.render_height * 0.30
        for index, glyph in enumerate(("z", "Z", "Z")):
            drift = (phase + index * 0.27) % 1.0
            alpha = int(185 * (1.0 - abs(drift - 0.5) * 1.55))
            if alpha <= 24:
                continue
            font = QFont("Sans Serif")
            font.setBold(True)
            font.setPointSize(max(7, int(round((8 + index * 2) * scale))))
            x = base_x + (index * 14 + drift * 10) * scale
            y = base_y - (index * 13 + drift * 18) * scale
            path = QPainterPath()
            path.addText(x, y, font, glyph)
            painter.strokePath(path, QPen(QColor(15, 22, 20, min(220, alpha + 35)), max(1.0, 1.6 * scale)))
            painter.fillPath(path, QColor(191, 255, 87, alpha))

    def thought_dots_visible(self) -> bool:
        if not self.thought_bubble_minimized:
            return False
        if (
            self.bubble_reply_active
            or self.pending_reply_request is not None
            or self.pending_slack_message is not None
            or self.work_process is not None
            or self.claude_conversation_active
            or self.slack_status_visible()
        ):
            return True
        status = self.last_codex_status
        return bool(status and (status.active or status.waiting_for_user))

    def draw_thought_dots(self, painter: QPainter) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scale = max(0.75, min(self.render_width / self.cell_width, self.render_height / self.cell_height))
        center_x = self.width() * 0.5
        if self.thought_dot_headroom > 0:
            base_y = max(6.5 * scale, min(self.thought_dot_headroom - 5.0 * scale, self.thought_dot_headroom * 0.48))
        else:
            base_y = max(9.0 * scale, self.render_height * 0.08)
        spacing = 14.0 * scale
        radius = 3.4 * scale
        phase = time.time() * math.tau * 0.62
        for index in range(3):
            lift = (math.sin(phase - index * 0.72) + 1.0) * 0.5
            alpha = int(112 + 118 * lift)
            x = center_x + (index - 1) * spacing
            y = base_y - lift * 4.0 * scale
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(111, 255, 167, int(alpha * 0.28)))
            painter.drawEllipse(QRectF(x - radius * 2.2, y - radius * 2.2, radius * 4.4, radius * 4.4))
            painter.setBrush(QColor(196, 255, 111, alpha))
            painter.drawEllipse(QRectF(x - radius, y - radius, radius * 2.0, radius * 2.0))
        painter.restore()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self.can_start_glider_rescue():
                self.start_glider_rescue()
                return
            if self.sequence_locked() or self.drop_dialog_active:
                return
            self.resting = False
            self.drag_offset = self.drag_anchor_offset()
            self.motion = "picked-up"
            self.action_ticks = 0
            self.set_animation("picked-up")
        elif event.button() == Qt.MouseButton.RightButton:
            self.show_context_menu(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.drag_offset is not None:
            target = event.globalPosition().toPoint() - self.drag_offset
            if self.can_attach_to_ceiling(target.y()):
                self.start_ceiling_hold(x=target.x())
                return
            self.set_animation("picked-up")
            self.move(target)
            self.update_bubble_position()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self.drag_offset is None:
                return
            if self.can_attach_to_ceiling():
                self.start_ceiling_hold()
                return
            self.drag_offset = None
            ground = self.ground_y()
            if self.y() < ground - 4:
                self.motion = "falling"
                self.action_ticks = 0
                self.fall_y = float(self.y())
                self.fall_start_y = self.fall_y
                self.fall_velocity_y = 0.0
                self.drop_height = max(0.0, float(ground - self.y()))
                self.glider_rescue_active = False
                self.ceiling_hold_active = False
                self.glider_origin_x = self.x()
                self.glider_sway_phase = 0.0
                self.fall_total_ticks = self.fall_duration_ticks(self.drop_height)
                self.fall_ticks_elapsed = 0
                self.post_action_cooldown_ticks = 0
                self.set_animation("falling" if "falling" in self.animations else "jumping")
            else:
                self.move(self.x(), ground)
                self.motion = "idle"
                self.action_ticks = 0
                self.set_animation("idle")

    def mouseDoubleClickEvent(self, _event) -> None:  # noqa: N802
        if self.sequence_locked():
            return
        if self.work_drop_enabled and abs(self.y() - self.ground_y()) <= 8:
            self.open_ask_dialog()
            return
        self.motion = "jump"
        self.action_ticks = 34

    def show_context_menu(self, point: QPoint) -> None:
        menu = QMenu(self)

        if self.work_drop_enabled:
            ask_action = QAction("Ask Codex...", self)
            ask_action.triggered.connect(self.open_ask_dialog)
            menu.addAction(ask_action)

            if self.claude_enabled:
                claude_action = QAction("Ask Claude...", self)
                claude_action.triggered.connect(self.open_claude_dialog)
                menu.addAction(claude_action)
                claude_conversation_busy = (
                    self.work_process is not None
                    and self.work_request is not None
                    and self.work_request.provider == AI_PROVIDER_CLAUDE
                    and self.work_request.conversation
                )
                if self.claude_conversation_active and not claude_conversation_busy:
                    end_claude_action = QAction("End Claude Conversation", self)
                    end_claude_action.triggered.connect(self.end_claude_conversation)
                    menu.addAction(end_claude_action)

            if self.slack_enabled:
                slack_menu = menu.addMenu("Slack")
                if self.slack_contacts:
                    for contact in self.slack_contacts:
                        label = contact.get("label") or "Slack contact"
                        contact_menu = slack_menu.addMenu(label)
                        bot_action = QAction("Message as Companion", self)
                        bot_action.triggered.connect(
                            lambda _checked=False, contact=dict(contact): self.open_slack_dialog(
                                contact,
                                send_as="bot",
                            )
                        )
                        contact_menu.addAction(bot_action)
                        user_action = QAction("Message as You", self)
                        user_action.triggered.connect(
                            lambda _checked=False, contact=dict(contact): self.open_slack_dialog(
                                contact,
                                send_as="user",
                            )
                        )
                        contact_menu.addAction(user_action)
                        if self.slack_transcript_enabled:
                            contact_menu.addSeparator()
                            contact_log_action = QAction("Open Conversation Log", self)
                            contact_log_action.triggered.connect(
                                lambda _checked=False, contact=dict(contact): self.open_slack_transcript(contact)
                            )
                            contact_menu.addAction(contact_log_action)
                else:
                    missing_action = QAction("No contacts configured", self)
                    missing_action.setEnabled(False)
                    slack_menu.addAction(missing_action)
                if self.slack_transcript_enabled:
                    slack_menu.addSeparator()
                    log_action = QAction("Open Active Conversation Log", self)
                    log_action.triggered.connect(self.open_slack_transcript)
                    slack_menu.addAction(log_action)

            if self.github_enabled:
                github_menu = menu.addMenu("GitHub")

                check_github_action = QAction("Check Repo Access", self)
                check_github_action.triggered.connect(lambda: self.start_github_action("check"))
                github_menu.addAction(check_github_action)

                push_github_action = QAction("Push Current Branch", self)
                push_github_action.triggered.connect(lambda: self.start_github_action("push"))
                github_menu.addAction(push_github_action)

                merge_main_action = QAction(f"Merge {self.github_remote}/{self.github_main_branch}, Then Push", self)
                merge_main_action.triggered.connect(lambda: self.start_github_action("merge_main_push"))
                github_menu.addAction(merge_main_action)

                publish_main_action = QAction(f"Merge Current Branch Into {self.github_main_branch}, Then Push", self)
                publish_main_action.triggered.connect(lambda: self.start_github_action("merge_to_main"))
                github_menu.addAction(publish_main_action)

            if self.work_process is not None:
                stop_label = "Stop Work"
                if self.work_request is not None:
                    stop_label = "Stop " + self.work_provider_label(self.work_request) + " Work"
                stop_action = QAction(stop_label, self)
                stop_action.triggered.connect(self.cancel_work_request)
                menu.addAction(stop_action)

            menu.addSeparator()

        self.add_worktree_tasks_menu(menu)

        if self.thought_bubble_controls_available():
            bubble_action = QAction(
                "Show Thought Bubble" if self.thought_bubble_minimized else "Minimize Thought Bubble",
                self,
            )
            bubble_action.triggered.connect(self.toggle_thought_bubble_minimized)
            menu.addAction(bubble_action)

            expand_action = QAction(
                "Collapse Thought Bubble" if self.thought_bubble_expanded else "Expand Thought Bubble",
                self,
            )
            expand_action.triggered.connect(self.toggle_thought_bubble_expanded)
            menu.addAction(expand_action)
            menu.addSeparator()

        if self.companion_entries:
            companions_menu = menu.addMenu(self.companion_menu_label)
            for entry in self.companion_entries:
                label = str(entry.get("label") or entry.get("pet") or "Companion")
                companion_action = QAction(f"Spawn {label}", self)
                companion_action.triggered.connect(
                    lambda _checked=False, entry=dict(entry): self.spawn_companion(entry)
                )
                companions_menu.addAction(companion_action)
            menu.addSeparator()

        pet_actions_menu = menu.addMenu("Pet Actions")

        pause_action = QAction("Wake" if self.resting else "Rest", self)
        pause_action.triggered.connect(self.toggle_pause)
        pet_actions_menu.addAction(pause_action)

        wave_action = QAction("Wave", self)
        wave_action.triggered.connect(lambda: self.force_behavior("waving", 60))
        pet_actions_menu.addAction(wave_action)

        happy_action = QAction("Happy", self)
        happy_action.triggered.connect(lambda: self.force_behavior("happy", 60))
        pet_actions_menu.addAction(happy_action)

        sit_action = QAction("Sit", self)
        sit_action.triggered.connect(lambda: self.force_behavior("sitting", 120))
        pet_actions_menu.addAction(sit_action)

        jump_action = QAction("Jump", self)
        jump_action.triggered.connect(lambda: self.force_behavior("jump", 34))
        pet_actions_menu.addAction(jump_action)

        if self.usage_monitor is not None and self.usage_meter is not None:
            usage_action = QAction("Codex Limits", self)
            usage_action.triggered.connect(lambda: self.show_usage_meter(periodic=False))
            menu.addAction(usage_action)

        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(quit_action)
        menu.exec(point)

    def thought_bubble_controls_available(self) -> bool:
        if self.thought_bubble is None:
            return False
        if self.thought_bubble.reply_mode or self.bubble_reply_active:
            return True
        if self.work_status_visible() or self.work_process is not None:
            return True
        if self.slack_status_visible():
            return True
        status = self.last_codex_status
        return bool(status and (status.visible or status.active or status.waiting_for_user))

    def set_thought_bubble_minimized(self, minimized: bool) -> None:
        self.thought_bubble_minimized = minimized
        self.work_bubble_minimized = minimized
        if self.thought_bubble is not None:
            self.thought_bubble.set_minimized(minimized)
        self.update()

    def toggle_thought_bubble_minimized(self) -> None:
        self.set_thought_bubble_minimized(not self.thought_bubble_minimized)
        self.tick_codex_status()

    def toggle_thought_bubble_expanded(self) -> None:
        self.thought_bubble_expanded = not self.thought_bubble_expanded
        self.work_bubble_expanded = self.thought_bubble_expanded
        if self.thought_bubble is not None:
            self.thought_bubble.set_expanded(self.thought_bubble_expanded or self.thought_bubble.reply_mode)
        self.tick_codex_status()

    def toggle_work_bubble_minimized(self) -> None:
        self.toggle_thought_bubble_minimized()

    def toggle_work_bubble_expanded(self) -> None:
        self.toggle_thought_bubble_expanded()

    def force_behavior(self, motion: str, ticks: int) -> None:
        if self.sequence_locked():
            return
        self.resting = motion in {"resting", self.rest_enter_animation, self.rest_animation, "sleeping"}
        if self.resting:
            self.motion = self.rest_motion_name()
            self.action_ticks = 0
        else:
            self.motion = motion
            self.action_ticks = ticks
        if self.motion in self.animations:
            self.set_animation(self.motion)

    def toggle_pause(self) -> None:
        if self.sequence_locked():
            return
        self.paused = False
        if self.resting:
            self.leave_rest_mode()
        else:
            self.enter_rest_mode()

    def write_heartbeat(self) -> None:
        state = {
            "pid": os.getpid(),
            "time": time.time(),
            "animation": self.current_animation,
            "motion": self.motion,
            "x": self.x(),
            "y": self.y(),
            "paused": self.paused,
            "resting": self.resting,
            "energy": round(self.energy, 2),
            "dropHeight": round(self.drop_height, 2),
            "fallVelocityY": round(self.fall_velocity_y, 2),
            "gliderRescueActive": self.glider_rescue_active,
            "ceilingHoldActive": self.ceiling_hold_active,
            "bubbleReplyActive": self.bubble_reply_active,
            "bubbleReplyProvider": self.bubble_reply_provider,
            "claudeConversationActive": self.claude_conversation_active,
            "claudeConversationTurns": sum(
                1 for item in self.claude_conversation_messages if item.get("role") == "user"
            ),
            "slackEnabled": self.slack_enabled,
            "slackTargetConfigured": self.slack_target_configured(),
            "slackSendAs": self.slack_send_as,
            "slackTranscriptEnabled": self.slack_transcript_enabled,
            "slackStatusVisible": self.slack_status_visible(),
            "slackPollActive": self.slack_poll_process is not None,
            "pendingReply": self.pending_reply_request is not None,
            "pendingSlackMessage": self.pending_slack_message is not None,
            "pendingResumeReply": self.pending_resume_reply is not None,
            "resumeSessionId": self.work_resume_session_id,
            "thoughtBubbleMinimized": self.thought_bubble_minimized,
            "thoughtBubbleExpanded": self.thought_bubble_expanded,
            "ceilingCooldownTicks": self.ceiling_cooldown_ticks,
            "fallTicksElapsed": self.fall_ticks_elapsed,
            "fallTotalTicks": self.fall_total_ticks,
            "postActionCooldownTicks": self.post_action_cooldown_ticks,
        }
        self.state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.work_process is not None:
            self.work_process.kill()
            self.work_process = None
        if self.slack_poll_process is not None:
            self.slack_poll_process.kill()
            self.slack_poll_process = None
        if self.work_drop_bubble is not None:
            self.work_drop_bubble.close()
        if self.thought_bubble is not None:
            self.thought_bubble.close()
        if self.usage_meter is not None:
            self.usage_meter.close()
        super().closeEvent(event)


def run_pet(
    pet_dir: Path,
    *,
    scale: float | None = None,
    speed: float | None = None,
    codex_session: str | None = None,
) -> int:
    app = QApplication.instance() or QApplication([])
    window = DesktopPet(pet_dir, scale=scale, speed=speed, codex_session=codex_session)
    window.show()
    return app.exec()
