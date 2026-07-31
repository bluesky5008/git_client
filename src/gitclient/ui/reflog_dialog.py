"""reflog 탐색 (FR-09·10).

앱의 파괴적 동작들(reset --hard, 건너뛰기, amend)은 전부 "커밋은 git
reflog에 남는다"를 약속해 왔다 — 그런데 그 약속을 확인하려면 터미널로
나가야 했다. 이 화면이 약속의 나머지 절반이다: 잃은 커밋을 찾고,
브랜치를 달아 다시 닿게 만든다.

**여기서 저장소를 바꾸지 않는다.** 브랜치 생성은 쓰기이므로 WriteQueue를
거쳐야 하고(§3.3 규칙 3), 그 제출은 MainWindow의 몫이다 — ConflictPanel과
같은 분업이다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)


class ReflogDialog(QDialog):
    """HEAD가 지나온 자리들. 고르면 브랜치로 되살릴 수 있다."""

    branch_requested = Signal(str, str)
    """(sha, 새 브랜치 이름)."""

    def __init__(self, entries, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("reflog 탐색")
        self.resize(720, 480)

        hint = QLabel(
            "HEAD가 지나온 자리들입니다. reset이나 건너뛰기로 목록에서 "
            "사라진 커밋도 여기 남아 있습니다 — 브랜치를 만들면 다시 "
            "그래프에 나타납니다."
        )
        hint.setWordWrap(True)

        self._list = QListWidget()
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        for index, entry in enumerate(entries):
            when = entry.when.strftime("%m-%d %H:%M")
            item = QListWidgetItem(
                f"HEAD@{{{index}}}  {when}  {entry.sha[:7]}  {entry.message}"
            )
            item.setData(Qt.ItemDataRole.UserRole, entry.sha)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)

        self._branch_button = QPushButton("이 커밋에서 브랜치 만들기...")
        self._branch_button.clicked.connect(self._on_branch)
        copy_button = QPushButton("sha 복사")
        copy_button.clicked.connect(self._on_copy)
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(self._branch_button)
        buttons.addWidget(copy_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self._list, 1)
        layout.addLayout(buttons)

        self._update_buttons()
        self._list.currentItemChanged.connect(lambda *_: self._update_buttons())

    def _selected_sha(self) -> str | None:
        item = self._list.currentItem()
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _update_buttons(self) -> None:
        self._branch_button.setEnabled(self._selected_sha() is not None)

    def _on_copy(self) -> None:
        sha = self._selected_sha()
        if sha is not None:
            QGuiApplication.clipboard().setText(sha)

    def _on_branch(self) -> None:
        sha = self._selected_sha()
        if sha is None:
            return
        name, accepted = QInputDialog.getText(
            self,
            "브랜치 만들기",
            f"{sha[:7]} 커밋에 만들 브랜치 이름:",
            text=f"recovered-{sha[:7]}",
        )
        name = name.strip()
        if not accepted or not name:
            return
        self.branch_requested.emit(sha, name)
        self.accept()
