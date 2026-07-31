"""충돌 구획 선택 다이얼로그 (F3).

git이 사람에게 넘긴 구획마다 내 것/상대 것/양쪽을 고른다 — 편집기 없이
양쪽에서 일부씩 가져오는 길이다. 라벨은 연산에서 유도된 것을 받는다
(ADR-68 — 리베이스에서 "내 것"이 반대로 붙으면 커밋이 사라진다).

쓰기는 하지 않는다 — 선택만 시그널로 내보낸다 (ConflictPanel과 같은 분업).
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gitclient.domain.conflict_text import (
    CHOICE_BOTH,
    CHOICE_OURS,
    CHOICE_THEIRS,
    ConflictHunk,
)

_PREVIEW_MAX_LINES = 12


class ConflictLinesDialog(QDialog):
    apply_requested = Signal(list)
    """(구획별 선택 목록 — conflict_text.CHOICES 값들)."""

    def __init__(self, path: str, hunks, labels, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle(f"줄 단위로 고르기 — {path}")
        self.resize(760, 560)
        self._groups: list[QButtonGroup] = []

        column = QWidget()
        column_layout = QVBoxLayout(column)
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

        for number, hunk in enumerate(hunks, start=1):
            box = QGroupBox(f"구획 {number}")
            box_layout = QVBoxLayout(box)

            panes = QHBoxLayout()
            for title, lines in (
                (labels.ours, hunk.ours),
                (labels.theirs, hunk.theirs),
            ):
                pane = QVBoxLayout()
                pane.addWidget(QLabel(title))
                view = QPlainTextEdit()
                view.setReadOnly(True)
                view.setFont(fixed)
                view.setPlainText(_preview("".join(lines)))
                view.setFixedHeight(120)
                pane.addWidget(view)
                panes.addLayout(pane)
            box_layout.addLayout(panes)

            group = QButtonGroup(self)
            row = QHBoxLayout()
            for choice, label in (
                (CHOICE_OURS, f"{labels.ours} 사용"),
                (CHOICE_THEIRS, f"{labels.theirs} 사용"),
                (CHOICE_BOTH, "양쪽 모두 (내 것 먼저)"),
            ):
                radio = QRadioButton(label)
                radio.setProperty("choice", choice)
                group.addButton(radio)
                row.addWidget(radio)
            group.buttons()[0].setChecked(True)
            row.addStretch(1)
            box_layout.addLayout(row)

            self._groups.append(group)
            column_layout.addWidget(box)
        column_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(column)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText(
            "이대로 합치기"
        )
        buttons.clicked.connect(self._on_clicked)
        buttons.rejected.connect(self.reject)

        hint = QLabel(
            "고르지 않은 부분(공통 줄)은 git이 이미 합쳐둔 그대로 남습니다. "
            "적용하면 파일이 조립되고 스테이징됩니다."
        )
        hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def choices(self) -> list[str]:
        picked: list[str] = []
        for group in self._groups:
            button = group.checkedButton()
            picked.append(button.property("choice"))
        return picked

    def _on_clicked(self, button) -> None:  # noqa: ANN001
        from PySide6.QtWidgets import QDialogButtonBox as Box

        role = self.sender().buttonRole(button)
        if role == Box.ButtonRole.ApplyRole:
            self.apply_requested.emit(self.choices())
            self.accept()


def _preview(text: str) -> str:
    lines = text.splitlines()
    if len(lines) > _PREVIEW_MAX_LINES:
        hidden = len(lines) - _PREVIEW_MAX_LINES
        lines = lines[:_PREVIEW_MAX_LINES] + [f"… ({hidden}줄 더)"]
    return "\n".join(lines) if lines else "(이 쪽은 비어 있습니다)"
