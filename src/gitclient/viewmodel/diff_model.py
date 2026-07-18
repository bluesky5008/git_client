"""diff 줄 목록 모델.

diff도 커밋 목록과 같은 이유로 가상화한다. 10만 줄짜리 diff여도
화면에 보이는 줄만 그린다. (doc/design.md §4.2)
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt

from gitclient.domain.models import DiffLine


class DiffRole:
    LINE = Qt.ItemDataRole.UserRole + 1


class DiffModel(QAbstractListModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lines: list[DiffLine] = []

    def set_lines(self, lines: list[DiffLine]) -> None:
        self.beginResetModel()
        self._lines = lines
        self.endResetModel()

    def clear(self) -> None:
        self.set_lines([])

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._lines)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if not (0 <= row < len(self._lines)):
            return None

        line = self._lines[row]
        if role == DiffRole.LINE:
            return line
        if role == Qt.ItemDataRole.DisplayRole:
            return line.text
        return None
