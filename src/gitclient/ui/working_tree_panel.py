"""작업 디렉터리 패널 (Phase 2).

스테이징/미스테이징 변경 목록과 커밋 작성 UI. 로직은 갖지 않는다 —
사용자 의도를 시그널로 알리고, 상태는 MainWindow가 밀어넣는다.

    ┌─ 작업 디렉터리 ─────────────┐
    │ 스테이징됨            [내리기] │
    │  M a.txt                    │
    │ 변경 사항       [올리기][버리기] │
    │  M b.txt                    │
    │ ┌─────────────────────────┐ │
    │ │ 커밋 메시지…             │ │
    │ └─────────────────────────┘ │
    │ □ 마지막 커밋 수정   [커밋]   │
    └─────────────────────────────┘
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gitclient.domain.models import WorkingFileChange, WorkingTreeStatus


class WorkingTreePanel(QWidget):
    stage_requested = Signal(str)
    unstage_requested = Signal(str)
    discard_requested = Signal(str)
    commit_requested = Signal(str, bool)
    """(message, amend)."""

    diff_requested = Signal(str, bool)
    """(path, staged) — 선택한 변경의 diff를 보여 달라."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._head_message: str | None = None
        # 시퀀서(리베이스·cherry-pick·revert)가 도는 동안 False가 된다.
        self._commit_allowed = True
        self._build()
        self.set_enabled_for_repo(False)

    # ------------------------------------------------------------------
    # 구성
    # ------------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 4)
        layout.setSpacing(3)

        staged_header = QHBoxLayout()
        staged_header.addWidget(self._section_label("스테이징됨"))
        staged_header.addStretch(1)
        self._unstage_button = QPushButton("내리기 ↓")
        self._unstage_button.setToolTip("선택한 파일의 스테이징을 취소합니다")
        self._unstage_button.clicked.connect(self._emit_unstage)
        staged_header.addWidget(self._unstage_button)
        layout.addLayout(staged_header)

        self._staged_list = QListWidget()
        self._staged_list.itemDoubleClicked.connect(
            lambda item: self.unstage_requested.emit(self._path_of(item))
        )
        self._staged_list.currentItemChanged.connect(
            lambda current, _prev: self._on_selection(current, staged=True)
        )
        layout.addWidget(self._staged_list, 1)

        unstaged_header = QHBoxLayout()
        unstaged_header.addWidget(self._section_label("변경 사항"))
        unstaged_header.addStretch(1)
        self._stage_button = QPushButton("올리기 ↑")
        self._stage_button.setToolTip("선택한 파일을 스테이징합니다")
        self._stage_button.clicked.connect(self._emit_stage)
        unstaged_header.addWidget(self._stage_button)
        self._discard_button = QPushButton("버리기")
        self._discard_button.setToolTip("선택한 파일의 변경을 버립니다 (되돌릴 수 없음)")
        self._discard_button.clicked.connect(self._emit_discard)
        unstaged_header.addWidget(self._discard_button)
        layout.addLayout(unstaged_header)

        self._unstaged_list = QListWidget()
        self._unstaged_list.itemDoubleClicked.connect(
            lambda item: self.stage_requested.emit(self._path_of(item))
        )
        self._unstaged_list.currentItemChanged.connect(
            lambda current, _prev: self._on_selection(current, staged=False)
        )
        layout.addWidget(self._unstaged_list, 1)

        self._message_edit = QPlainTextEdit()
        self._message_edit.setPlaceholderText("커밋 메시지")
        self._message_edit.setMaximumHeight(72)
        self._message_edit.textChanged.connect(self._update_commit_button)
        layout.addWidget(self._message_edit)

        commit_row = QHBoxLayout()
        self._amend_check = QCheckBox("마지막 커밋 수정")
        self._amend_check.toggled.connect(self._on_amend_toggled)
        commit_row.addWidget(self._amend_check)
        commit_row.addStretch(1)
        self._commit_button = QPushButton("커밋")
        self._commit_button.clicked.connect(self._emit_commit)
        commit_row.addWidget(self._commit_button)
        layout.addLayout(commit_row)

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        font = QFont(label.font())
        font.setBold(True)
        font.setPointSizeF(max(7.0, font.pointSizeF() - 0.5))
        label.setFont(font)
        return label

    # ------------------------------------------------------------------
    # 상태 주입 (MainWindow → 패널)
    # ------------------------------------------------------------------

    def set_commit_enabled(self, enabled: bool) -> None:
        """커밋으로 마무리할 수 있는 상태인가.

        스테이징은 그대로 열어 둔다 — 리베이스 충돌을 해결하려면 파일을
        스테이징해야 하고, 막는 것은 **마무리 수단**뿐이다. 시퀀서가 도는
        중에 커밋하면 git이 받아 주기는 하지만(실측) 이후 `--continue`가
        "가져올 변경이 없으니 건너뛰라"며 방금 만든 커밋을 버리라고
        안내하는 막다른 길이 된다 (design.md §4.12.4).
        """
        self._commit_allowed = enabled
        self._update_commit_button()

    def set_enabled_for_repo(self, enabled: bool) -> None:
        """저장소가 열려 있고 워킹 트리가 있을 때만 조작 가능하다."""
        for widget in (
            self._staged_list,
            self._unstaged_list,
            self._message_edit,
            self._amend_check,
        ):
            widget.setEnabled(enabled)
        self._update_buttons()

    def show_status(self, status: WorkingTreeStatus, head_message: str | None) -> None:
        """워커가 읽어온 상태로 목록을 다시 그린다. 선택은 경로 기준으로 보존한다."""
        self._head_message = head_message

        for widget, changes in (
            (self._staged_list, status.staged),
            (self._unstaged_list, status.unstaged),
        ):
            selected = self._path_of(widget.currentItem())
            widget.blockSignals(True)
            try:
                widget.clear()
                for change in changes:
                    item = QListWidgetItem(f"{change.status.value}  {change.path}")
                    item.setData(Qt.ItemDataRole.UserRole, change.path)
                    widget.addItem(item)
                    if change.path == selected:
                        widget.setCurrentItem(item)
            finally:
                widget.blockSignals(False)

        self._update_buttons()

    def commit_message(self) -> str:
        return self._message_edit.toPlainText()

    def clear_message(self) -> None:
        self._message_edit.clear()
        self._amend_check.setChecked(False)

    def selected_change(self) -> tuple[str, bool] | None:
        """(path, staged) — 지금 선택된 변경. 없으면 None."""
        item = self._staged_list.currentItem()
        if item is not None:
            return (self._path_of(item), True)
        item = self._unstaged_list.currentItem()
        if item is not None:
            return (self._path_of(item), False)
        return None

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    @staticmethod
    def _path_of(item: QListWidgetItem | None) -> str | None:
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_selection(self, current: QListWidgetItem | None, *, staged: bool) -> None:
        path = self._path_of(current)
        if path is None:
            self._update_buttons()
            return

        # 반대쪽 목록의 선택을 지워 "지금 무엇의 diff인가"를 명확히 한다.
        other = self._unstaged_list if staged else self._staged_list
        other.blockSignals(True)
        other.setCurrentItem(None)
        other.blockSignals(False)

        self._update_buttons()
        self.diff_requested.emit(path, staged)

    def _emit_stage(self) -> None:
        path = self._path_of(self._unstaged_list.currentItem())
        if path is not None:
            self.stage_requested.emit(path)

    def _emit_unstage(self) -> None:
        path = self._path_of(self._staged_list.currentItem())
        if path is not None:
            self.unstage_requested.emit(path)

    def _emit_discard(self) -> None:
        path = self._path_of(self._unstaged_list.currentItem())
        if path is not None:
            self.discard_requested.emit(path)

    def _emit_commit(self) -> None:
        self.commit_requested.emit(
            self.commit_message(), self._amend_check.isChecked()
        )

    def _on_amend_toggled(self, checked: bool) -> None:
        # amend를 켰는데 메시지가 비어 있으면 HEAD 메시지를 프리필한다.
        if checked and not self.commit_message().strip() and self._head_message:
            self._message_edit.setPlainText(self._head_message.rstrip("\n"))
        self._update_commit_button()

    def _update_buttons(self) -> None:
        self._stage_button.setEnabled(
            self._unstaged_list.isEnabled()
            and self._unstaged_list.currentItem() is not None
        )
        self._discard_button.setEnabled(self._stage_button.isEnabled())
        self._unstage_button.setEnabled(
            self._staged_list.isEnabled()
            and self._staged_list.currentItem() is not None
        )
        self._update_commit_button()

    def _update_commit_button(self) -> None:
        # 스테이징된 것이 없어도 amend(메시지만 수정)는 가능하다.
        has_message = bool(self.commit_message().strip())
        has_staged = self._staged_list.count() > 0
        amend = self._amend_check.isChecked()
        self._commit_button.setEnabled(
            self._message_edit.isEnabled()
            and self._commit_allowed
            and has_message
            and (has_staged or amend)
        )