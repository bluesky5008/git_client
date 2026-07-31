"""인터랙티브 리베이스 계획 화면 (FR-16).

**시각화 이득이 가장 큰 작업이다** — §1.3의 숙련자가 GUI를 쓰는 이유로
꼽은 것이 정확히 이것이다. 터미널의 todo 파일은 편집기 안에서 순서를
외워가며 고쳐야 하지만, 여기서는 커밋 요약을 보면서 끌어 올리고 내린다.

쓰기는 하지 않는다 — 계획만 시그널로 내보낸다 (다른 다이얼로그와 같은
분업). 순서는 git의 todo와 같다: **위가 먼저 적용된다.**
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from gitclient.i18n import trf
from gitclient.domain.models import RebaseAction, RebaseStep

_ACTION_LABELS: tuple[tuple[RebaseAction, str], ...] = (
    (RebaseAction.PICK, "그대로 두기"),
    (RebaseAction.SQUASH, "앞 커밋에 합치기"),
    (RebaseAction.FIXUP, "앞에 합치고 메시지 버리기"),
    (RebaseAction.DROP, "버리기"),
)


class RebaseTodoDialog(QDialog):
    plan_ready = Signal(list)
    """(RebaseStep 목록 — 위에서 아래 순서가 곧 적용 순서)."""

    def __init__(self, upstream: str, steps, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle(trf("'{upstream}' 위로 리베이스 — 계획", upstream=upstream))
        self.resize(760, 480)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["동작", "커밋", "요약"])
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        for step in steps:
            item = QTreeWidgetItem(["", step.sha[:7], step.summary])
            item.setFont(1, fixed)
            item.setData(0, Qt.ItemDataRole.UserRole, step.sha)
            self._tree.addTopLevelItem(item)
            self._tree.setItemWidget(item, 0, self._action_box(item))
        self._tree.setColumnWidth(0, 200)
        self._tree.setColumnWidth(1, 90)
        if self._tree.topLevelItemCount():
            self._tree.setCurrentItem(self._tree.topLevelItem(0))

        self._up = QPushButton("↑ 위로")
        self._up.clicked.connect(lambda: self._move(-1))
        self._down = QPushButton("↓ 아래로")
        self._down.clicked.connect(lambda: self._move(1))

        moves = QHBoxLayout()
        moves.addWidget(self._up)
        moves.addWidget(self._down)
        moves.addStretch(1)

        hint = QLabel(
            "위에 있는 커밋이 먼저 적용됩니다. '앞 커밋에 합치기'는 바로 위"
            "커밋과 하나가 되고, '버리기'는 커밋을 결과에서 없앱니다 "
            "(reflog로만 되찾을 수 있습니다)."
        )
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("리베이스 시작")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._tree, 1)
        layout.addLayout(moves)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def _action_box(self, item: QTreeWidgetItem) -> QComboBox:
        box = QComboBox()
        for action, label in _ACTION_LABELS:
            box.addItem(label, action)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, box)
        return box

    def _move(self, delta: int) -> None:
        """선택한 줄을 위/아래로 옮긴다.

        combo 위젯은 항목에 붙어 있지 않고 트리가 들고 있으므로, 옮길 때
        **선택값을 읽어 새 위젯에 다시 심는다** — 위젯만 옮기면 Qt가
        소유권을 놓아 빈 칸이 남는다.
        """
        index = self._tree.indexOfTopLevelItem(self._tree.currentItem())
        target = index + delta
        if index < 0 or not (0 <= target < self._tree.topLevelItemCount()):
            return
        actions = self._actions()
        item = self._tree.takeTopLevelItem(index)
        moved = actions.pop(index)
        self._tree.insertTopLevelItem(target, item)
        actions.insert(target, moved)
        for row in range(self._tree.topLevelItemCount()):
            row_item = self._tree.topLevelItem(row)
            box = self._action_box(row_item)
            box.setCurrentIndex(
                next(
                    i for i, (action, _l) in enumerate(_ACTION_LABELS)
                    if action is actions[row]
                )
            )
            self._tree.setItemWidget(row_item, 0, box)
        self._tree.setCurrentItem(self._tree.topLevelItem(target))

    def _actions(self) -> list[RebaseAction]:
        picked: list[RebaseAction] = []
        for row in range(self._tree.topLevelItemCount()):
            box = self._tree.itemWidget(self._tree.topLevelItem(row), 0)
            picked.append(box.currentData())
        return picked

    def plan(self) -> list[RebaseStep]:
        actions = self._actions()
        return [
            RebaseStep(
                sha=self._tree.topLevelItem(row).data(0, Qt.ItemDataRole.UserRole),
                action=actions[row],
                summary=self._tree.topLevelItem(row).text(2),
            )
            for row in range(self._tree.topLevelItemCount())
        ]

    def _on_accept(self) -> None:
        self.plan_ready.emit(self.plan())
        self.accept()
