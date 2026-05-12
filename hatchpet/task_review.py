from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .git_review import (
    GitReviewError,
    create_branch,
    diff_for_file,
    handoff_staged_to_local,
    open_pull_request,
    parse_hunks,
    push_branch,
    review_state,
    revert_file,
    revert_hunk,
    stage_all,
    stage_file,
    stage_hunk,
    unstage_all,
    unstage_file,
    unstage_hunk,
)
from .worktree_tasks import WorktreeTask, update_worktree_task


class TaskReviewDialog(QDialog):
    def __init__(
        self,
        task: WorktreeTask,
        *,
        parent: QWidget | None = None,
        title_prefix: str = "Worktree Review",
        remote: str = "origin",
        main_branch: str = "main",
        open_terminal: Callable[[str], None] | None = None,
        open_folder: Callable[[str], None] | None = None,
        accent: str = "cyan",
    ) -> None:
        super().__init__(parent)
        self.task = task
        self.remote = remote
        self.main_branch = main_branch
        self.open_terminal_callback = open_terminal
        self.open_folder_callback = open_folder
        self.hunks = []
        self.accent = accent
        self.setWindowTitle(f"{title_prefix} - {task.label or task.task_id}")
        self.resize(1180, 760)
        self.setMinimumSize(920, 620)
        self.build_ui()
        self.apply_style()
        self.refresh()

    def build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        self.title_label = QLabel(self.task.label or self.task.task_id)
        self.title_label.setObjectName("TitleLabel")
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("MetaLabel")
        header.addWidget(self.title_label, 1)
        header.addWidget(self.meta_label, 2)
        root.addLayout(header)

        toolbar = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.terminal_button = QPushButton("Terminal")
        self.folder_button = QPushButton("Folder")
        self.scope = QComboBox()
        self.scope.addItem("All Changes", "all")
        self.scope.addItem("Unstaged", "unstaged")
        self.scope.addItem("Staged", "staged")
        toolbar.addWidget(self.refresh_button)
        toolbar.addWidget(self.terminal_button)
        toolbar.addWidget(self.folder_button)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("Diff"))
        toolbar.addWidget(self.scope)
        root.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.files = QListWidget()
        self.files.setMinimumWidth(260)
        self.files.setMaximumWidth(420)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        self.diff = QTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.diff.setObjectName("DiffView")
        right_layout.addWidget(self.diff, 1)

        hunk_row = QHBoxLayout()
        self.hunks_list = QListWidget()
        self.hunks_list.setMaximumHeight(98)
        self.hunks_list.setObjectName("HunkList")
        hunk_buttons = QVBoxLayout()
        self.stage_hunk_button = QPushButton("Stage Hunk")
        self.unstage_hunk_button = QPushButton("Unstage Hunk")
        self.revert_hunk_button = QPushButton("Revert Hunk")
        hunk_buttons.addWidget(self.stage_hunk_button)
        hunk_buttons.addWidget(self.unstage_hunk_button)
        hunk_buttons.addWidget(self.revert_hunk_button)
        hunk_buttons.addStretch(1)
        hunk_row.addWidget(self.hunks_list, 1)
        hunk_row.addLayout(hunk_buttons)
        right_layout.addLayout(hunk_row)

        splitter.addWidget(self.files)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        file_actions = QHBoxLayout()
        self.stage_file_button = QPushButton("Stage File")
        self.unstage_file_button = QPushButton("Unstage File")
        self.revert_file_button = QPushButton("Revert File")
        self.stage_all_button = QPushButton("Stage All")
        self.unstage_all_button = QPushButton("Unstage All")
        for button in (
            self.stage_file_button,
            self.unstage_file_button,
            self.revert_file_button,
            self.stage_all_button,
            self.unstage_all_button,
        ):
            file_actions.addWidget(button)
        file_actions.addStretch(1)
        root.addLayout(file_actions)

        handoff = QHBoxLayout()
        self.branch_input = QLineEdit()
        self.branch_input.setPlaceholderText("review/task-branch")
        self.branch_button = QPushButton("Create Branch")
        self.commit_input = QLineEdit()
        self.commit_input.setPlaceholderText("Commit message")
        self.commit_button = QPushButton("Commit Staged")
        self.push_button = QPushButton("Push Branch")
        self.pr_button = QPushButton("Open PR")
        self.handoff_button = QPushButton("Hand Off Staged")
        handoff.addWidget(self.branch_input, 2)
        handoff.addWidget(self.branch_button)
        handoff.addWidget(self.commit_input, 3)
        handoff.addWidget(self.commit_button)
        handoff.addWidget(self.push_button)
        handoff.addWidget(self.pr_button)
        handoff.addWidget(self.handoff_button)
        root.addLayout(handoff)

        self.status_label = QLabel("")
        self.status_label.setObjectName("StatusLabel")
        root.addWidget(self.status_label)

        self.refresh_button.clicked.connect(self.refresh)
        self.terminal_button.clicked.connect(self.open_terminal)
        self.folder_button.clicked.connect(self.open_folder)
        self.scope.currentIndexChanged.connect(self.update_diff)
        self.files.currentItemChanged.connect(lambda _current, _previous: self.update_diff())
        self.stage_file_button.clicked.connect(self.stage_selected_file)
        self.unstage_file_button.clicked.connect(self.unstage_selected_file)
        self.revert_file_button.clicked.connect(self.revert_selected_file)
        self.stage_all_button.clicked.connect(lambda: self.run_action(lambda: stage_all(self.task.worktree_path)))
        self.unstage_all_button.clicked.connect(lambda: self.run_action(lambda: unstage_all(self.task.worktree_path)))
        self.stage_hunk_button.clicked.connect(self.stage_selected_hunk)
        self.unstage_hunk_button.clicked.connect(self.unstage_selected_hunk)
        self.revert_hunk_button.clicked.connect(self.revert_selected_hunk)
        self.branch_button.clicked.connect(self.create_task_branch)
        self.commit_button.clicked.connect(self.commit_staged)
        self.push_button.clicked.connect(self.push_task_branch)
        self.pr_button.clicked.connect(self.open_task_pr)
        self.handoff_button.clicked.connect(self.handoff_task)

    def apply_style(self) -> None:
        accent = "58, 235, 255" if self.accent == "cyan" else "255, 63, 207"
        self.setStyleSheet(
            f"""
            QDialog {{
                background: rgb(8, 16, 18);
                color: rgb(225, 245, 239);
            }}
            QLabel#TitleLabel {{
                color: rgb({accent});
                font-size: 18px;
                font-weight: 800;
            }}
            QLabel#MetaLabel, QLabel#StatusLabel {{
                color: rgb(161, 194, 188);
            }}
            QListWidget, QTextEdit, QLineEdit, QComboBox {{
                background: rgb(3, 12, 15);
                border: 1px solid rgba({accent}, 150);
                border-radius: 6px;
                color: rgb(230, 255, 244);
                selection-background-color: rgba({accent}, 70);
                padding: 6px;
            }}
            QTextEdit#DiffView {{
                font-family: monospace;
                font-size: 12px;
            }}
            QListWidget#HunkList {{
                font-family: monospace;
            }}
            QPushButton {{
                background: rgba(7, 24, 26, 235);
                border: 1px solid rgba({accent}, 190);
                border-radius: 6px;
                color: rgb(230, 255, 244);
                font-weight: 700;
                padding: 6px 10px;
            }}
            QPushButton:hover {{
                background: rgba({accent}, 42);
                border-color: rgba({accent}, 255);
            }}
            QPushButton:disabled {{
                color: rgb(100, 120, 118);
                border-color: rgba(90, 110, 108, 120);
            }}
            """
        )

    def refresh(self) -> None:
        try:
            self.state = review_state(self.task.worktree_path)
        except GitReviewError as exc:
            self.show_error(exc)
            return
        branch = self.state.branch or "detached"
        self.meta_label.setText(
            f"{self.state.files.__len__()} files | {self.state.staged_count} staged | "
            f"{self.state.unstaged_count} unstaged | {self.state.untracked_count} untracked | {branch} @ {self.state.head}"
        )
        if not self.branch_input.text().strip():
            self.branch_input.setText(self.state.branch or f"review/{self.task.task_id[:16]}")
        if not self.commit_input.text().strip():
            self.commit_input.setText(self.task.label or f"Review {self.task.task_id}")

        selected_path = self.selected_path()
        self.files.blockSignals(True)
        self.files.clear()
        for item in self.state.files:
            label = f"{item.code}  {item.path}"
            if item.renamed_from:
                label += f"  from {item.renamed_from}"
            widget_item = QListWidgetItem(label)
            widget_item.setData(Qt.ItemDataRole.UserRole, item.path)
            self.files.addItem(widget_item)
            if selected_path and item.path == selected_path:
                self.files.setCurrentItem(widget_item)
        self.files.blockSignals(False)
        if self.files.count() and self.files.currentRow() < 0:
            self.files.setCurrentRow(0)
        self.update_diff()
        self.set_status("Review refreshed.")

    def update_diff(self) -> None:
        path = self.selected_path()
        scope = self.current_scope()
        try:
            diff_text = diff_for_file(self.task.worktree_path, path or "", scope=scope)
        except GitReviewError as exc:
            self.show_error(exc)
            return
        if not diff_text.strip():
            diff_text = "No changes in this scope."
        self.diff.setHtml(diff_to_html(diff_text))
        self.populate_hunks(diff_text, scope)

    def populate_hunks(self, diff_text: str, scope: str) -> None:
        self.hunks = parse_hunks(diff_text) if scope in {"staged", "unstaged"} else []
        self.hunks_list.clear()
        for hunk in self.hunks:
            label = hunk.header or f"Hunk {hunk.index + 1}"
            self.hunks_list.addItem(QListWidgetItem(label))
        has_hunks = bool(self.hunks)
        self.stage_hunk_button.setEnabled(scope == "unstaged" and has_hunks)
        self.revert_hunk_button.setEnabled(scope == "unstaged" and has_hunks)
        self.unstage_hunk_button.setEnabled(scope == "staged" and has_hunks)
        if has_hunks:
            self.hunks_list.setCurrentRow(0)

    def selected_path(self) -> str:
        item = self.files.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""

    def selected_hunk_index(self) -> int:
        return self.hunks_list.currentRow()

    def current_scope(self) -> str:
        return str(self.scope.currentData() or "all")

    def run_action(self, action: Callable[[], str], *, confirm: str = "") -> None:
        if confirm:
            answer = QMessageBox.question(
                self,
                "Confirm Review Action",
                confirm,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            detail = action()
        except GitReviewError as exc:
            self.show_error(exc)
            return
        self.set_status(detail)
        self.refresh()

    def stage_selected_file(self) -> None:
        path = self.selected_path()
        if path:
            self.run_action(lambda: stage_file(self.task.worktree_path, path))

    def unstage_selected_file(self) -> None:
        path = self.selected_path()
        if path:
            self.run_action(lambda: unstage_file(self.task.worktree_path, path))

    def revert_selected_file(self) -> None:
        path = self.selected_path()
        if path:
            self.run_action(
                lambda: revert_file(self.task.worktree_path, path),
                confirm=f"Revert all changes in {path}? This cannot be undone from the review pane.",
            )

    def stage_selected_hunk(self) -> None:
        path = self.selected_path()
        index = self.selected_hunk_index()
        if path and index >= 0:
            self.run_action(lambda: stage_hunk(self.task.worktree_path, path, index))

    def unstage_selected_hunk(self) -> None:
        path = self.selected_path()
        index = self.selected_hunk_index()
        if path and index >= 0:
            self.run_action(lambda: unstage_hunk(self.task.worktree_path, path, index))

    def revert_selected_hunk(self) -> None:
        path = self.selected_path()
        index = self.selected_hunk_index()
        if path and index >= 0:
            self.run_action(
                lambda: revert_hunk(self.task.worktree_path, path, index),
                confirm="Revert the selected hunk? This changes the worktree file.",
            )

    def create_task_branch(self) -> None:
        branch = self.branch_input.text().strip()
        def action() -> str:
            created = create_branch(self.task.worktree_path, branch)
            update_worktree_task(self.task.task_id, branch=created, mode="branch", status="branched")
            return f"Created branch {created}."
        self.run_action(action)

    def commit_staged(self) -> None:
        message = self.commit_input.text().strip()
        def action() -> str:
            from .git_review import commit_staged as commit_staged_changes

            sha = commit_staged_changes(self.task.worktree_path, message)
            update_worktree_task(self.task.task_id, status="committed", last_commit=sha, committed_at=time.time())
            return f"Committed staged changes as {sha}."
        self.run_action(action)

    def push_task_branch(self) -> None:
        def action() -> str:
            detail = push_branch(self.task.worktree_path, self.remote)
            update_worktree_task(self.task.task_id, status="pushed", pushed_at=time.time())
            return detail
        self.run_action(action)

    def open_task_pr(self) -> None:
        try:
            detail = open_pull_request(self.task.worktree_path, remote=self.remote, base_branch=self.main_branch)
        except GitReviewError as exc:
            self.show_error(exc)
            return
        update_worktree_task(self.task.task_id, status="pr-ready", pr_detail=detail, pr_opened_at=time.time())
        if detail.startswith("https://"):
            from PyQt6.QtCore import QProcess

            QProcess.startDetached("xdg-open", [detail])
        self.set_status(detail)

    def handoff_task(self) -> None:
        message = self.commit_input.text().strip() or self.task.label or "Handoff worktree task"
        self.run_action(
            lambda: self.finish_handoff(message),
            confirm=(
                "Hand off staged changes to the main checkout?\n\n"
                "The local checkout must be clean. The review pane will create a checkpoint commit "
                "in the worktree, save a patch backup, then cherry-pick that commit locally."
            ),
        )

    def finish_handoff(self, message: str) -> str:
        sha = handoff_staged_to_local(self.task.worktree_path, self.task.repo_root, message)
        update_worktree_task(self.task.task_id, status="handed-off", handoff_commit=sha, handed_off_at=time.time())
        return f"Handed off {sha} to {self.task.repo_root}."

    def open_terminal(self) -> None:
        if self.open_terminal_callback:
            self.open_terminal_callback(self.task.task_id)

    def open_folder(self) -> None:
        if self.open_folder_callback:
            self.open_folder_callback(self.task.task_id)

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def show_error(self, exc: Exception) -> None:
        self.status_label.setText(str(exc))
        QMessageBox.warning(self, "Worktree Review", str(exc))


def diff_to_html(diff_text: str) -> str:
    rows = []
    for line in diff_text.splitlines():
        escaped = html.escape(line)
        color = "#d8f7ed"
        weight = "400"
        if line.startswith("+") and not line.startswith("+++"):
            color = "#7dffb2"
        elif line.startswith("-") and not line.startswith("---"):
            color = "#ff8f8f"
        elif line.startswith("@@"):
            color = "#5eeaff"
            weight = "700"
        elif line.startswith("diff --git") or line.startswith("index ") or line.startswith("---") or line.startswith("+++"):
            color = "#d7ff73"
            weight = "700"
        rows.append(f'<pre style="margin:0;color:{color};font-weight:{weight};">{escaped}</pre>')
    return "<html><body style=\"background:#030c0f;\">" + "\n".join(rows) + "</body></html>"
