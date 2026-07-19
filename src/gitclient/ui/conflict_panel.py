"""충돌 해결 패널 (Phase 4 증분 2).

**마커 없는 충돌을 해결할 수 있는 유일한 화면이다.** 바이너리 충돌과 삭제
계열 충돌은 워킹 트리에 마커가 들어가지 않아, 지금까지 안내하던 "편집기로
마커를 정리하라"가 통하지 않았다 (design.md §4.10.7).

**상시 표시다.** 이전에는 병합이 충돌로 끝나는 순간 모달을 한 번 띄우고
말았다. 앱을 다시 켜면 저장소는 여전히 병합 중인데 화면에는 아무 흔적이
없었다 — 상태는 저장소에 있는데 안내는 메모리에만 있었다 (§13-3).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from gitclient.domain.models import ConflictChoice, ConflictSide

#: 충돌 종류를 사용자의 말로. git 원문(both modified)으로는 무엇을 골라야
#: 하는지 알 수 없다 — 특히 한쪽이 지운 경우가 그렇다.
_SIDE_LABELS = {
    ConflictSide.BOTH_MODIFIED: "양쪽이 고침",
    ConflictSide.BOTH_ADDED: "양쪽이 새로 만듦",
    ConflictSide.DELETED_BY_THEM: "상대가 지움 / 내가 고침",
    ConflictSide.DELETED_BY_US: "내가 지움 / 상대가 고침",
    ConflictSide.BOTH_DELETED: "양쪽이 지움",
}


class ConflictPanel(QWidget):
    """충돌 목록과 양쪽 내용, 그리고 한쪽을 고르는 버튼.

    선택만 신호로 내보내고 해결은 하지 않는다 — 쓰기는 WriteQueue를 거쳐야
    하므로(§3.3 규칙 3) 그 제출은 MainWindow의 몫이다.
    """

    resolve_requested = Signal(str, object)
    """(경로, ConflictChoice)"""

    detail_requested = Signal(str)
    """(경로) — 선택한 파일의 양쪽 내용을 채워 달라는 요청."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_path: str | None = None

        self._list = QListWidget()
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._list.currentItemChanged.connect(self._on_selection_changed)

        self._ours = QPlainTextEdit()
        self._theirs = QPlainTextEdit()
        for view in (self._ours, self._theirs):
            view.setReadOnly(True)
            view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self._take_ours = QPushButton("내 것 사용")
        self._take_theirs = QPushButton("상대 것 사용")
        self._take_ours.clicked.connect(
            lambda: self._request(ConflictChoice.OURS)
        )
        self._take_theirs.clicked.connect(
            lambda: self._request(ConflictChoice.THEIRS)
        )

        self._hint = QLabel("")
        self._hint.setWordWrap(True)

        panes = QSplitter(Qt.Orientation.Horizontal)
        panes.addWidget(self._wrap("내 것 (현재 브랜치)", self._ours))
        panes.addWidget(self._wrap("상대 것 (합치려는 쪽)", self._theirs))
        panes.setSizes([1, 1])

        buttons = QHBoxLayout()
        buttons.addWidget(self._take_ours)
        buttons.addWidget(self._take_theirs)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("충돌한 파일"))
        layout.addWidget(self._list, 1)
        layout.addWidget(self._hint)
        layout.addWidget(panes, 3)
        layout.addLayout(buttons)
        self.set_conflicts(())

    @staticmethod
    def _wrap(title: str, widget: QWidget) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(title))
        layout.addWidget(widget)
        return box

    def set_conflicts(self, conflicts) -> None:  # noqa: ANN001
        """충돌 목록을 갱신한다. 비어 있으면 패널 자체가 쓸모없다."""
        previous = self._current_path
        self._list.blockSignals(True)
        self._list.clear()
        for conflict in conflicts:
            label = _SIDE_LABELS.get(conflict.side, "충돌")
            item = QListWidgetItem(f"{conflict.path}  —  {label}")
            item.setData(Qt.ItemDataRole.UserRole, conflict.path)
            self._list.addItem(item)
        self._list.blockSignals(False)

        # 고르고 있던 파일이 아직 남아 있으면 선택을 유지한다 — 하나 해결할
        # 때마다 선택이 처음으로 튀면 여러 개를 처리하기 어렵다.
        for row in range(self._list.count()):
            if self._list.item(row).data(Qt.ItemDataRole.UserRole) == previous:
                self._list.setCurrentRow(row)
                return
        if self._list.count():
            self._list.setCurrentRow(0)
        else:
            self._current_path = None
            self._show_empty()

    def show_detail(self, detail) -> None:  # noqa: ANN001 - ConflictDetail
        """선택한 파일의 양쪽 내용을 그린다."""
        if detail.path != self._current_path:
            return  # 그 사이 사용자가 다른 파일을 골랐다
        self._take_ours.setEnabled(True)
        self._take_theirs.setEnabled(True)

        if not detail.can_show_text:
            # **여기가 이 화면의 존재 이유다.** 내용을 비교할 수는 없어도
            # 한쪽을 고를 수는 있어야 한다 — 그러지 않으면 앱 안에서
            # 해결할 방법이 없다.
            self._hint.setText(
                "바이너리 파일이라 내용을 나란히 볼 수 없습니다. "
                "어느 쪽을 남길지 골라 주세요."
            )
        elif not detail.theirs.exists:
            self._hint.setText(
                "상대가 이 파일을 지웠습니다. '상대 것 사용'을 고르면 "
                "파일이 삭제됩니다."
            )
        elif not detail.ours.exists:
            self._hint.setText(
                "내가 이 파일을 지웠습니다. '내 것 사용'을 고르면 파일이 "
                "삭제됩니다."
            )
        else:
            self._hint.setText(
                "직접 편집해 해결하려면 워킹 트리의 파일을 고친 뒤 "
                "스테이징하면 됩니다."
            )

        self._ours.setPlainText(self._render(detail.ours, detail))
        self._theirs.setPlainText(self._render(detail.theirs, detail))

    @staticmethod
    def _render(side, detail) -> str:  # noqa: ANN001
        """한쪽 내용을 화면 문자열로. **없음과 빈 파일을 구분한다.**"""
        if not side.exists:
            return "(이 쪽에는 파일이 없습니다 — 지워졌습니다)"
        if detail.is_binary:
            return f"(바이너리, {len(side.data):,}바이트)"
        return side.text

    def _show_empty(self) -> None:
        self._hint.setText("해결할 충돌이 없습니다.")
        self._ours.setPlainText("")
        self._theirs.setPlainText("")
        self._take_ours.setEnabled(False)
        self._take_theirs.setEnabled(False)

    def _on_selection_changed(self, current, _previous) -> None:  # noqa: ANN001
        if current is None:
            self._current_path = None
            self._show_empty()
            return
        self._current_path = current.data(Qt.ItemDataRole.UserRole)
        # 내용은 저장소를 읽어야 나온다 — 그 일은 UI 스레드 밖에서.
        self.detail_requested.emit(self._current_path)

    def _request(self, choice: ConflictChoice) -> None:
        if self._current_path is None:
            return
        # 연타로 같은 파일을 두 번 제출하지 않도록 즉시 잠근다. 큐가 끝나면
        # 목록이 갱신되며 다시 열린다.
        self._take_ours.setEnabled(False)
        self._take_theirs.setEnabled(False)
        self.resolve_requested.emit(self._current_path, choice)
