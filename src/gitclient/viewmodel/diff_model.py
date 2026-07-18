"""diff 줄 목록 모델.

diff도 커밋 목록과 같은 이유로 가상화한다. 10만 줄짜리 diff여도
화면에 보이는 줄만 그린다. (doc/design.md §4.2)
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt

from gitclient.domain.models import DiffLine, DiffLineKind
from gitclient.domain.patch import FilePatch


class DiffRole:
    LINE = Qt.ItemDataRole.UserRole + 1
    PATCH_POSITION = Qt.ItemDataRole.UserRole + 2
    """(헝크 인덱스, 줄 인덱스) — 부분 스테이징의 선택 좌표. 없으면 None."""


class DiffModel(QAbstractListModel):
    """diff 표시 모델.

    표시용 `DiffLine` 목록과 별개로, 부분 스테이징에 쓸 좌표를 함께 들고 있다.
    화면의 줄과 패치의 줄이 1:1로 대응해야 사용자가 고른 것이 그대로 적용된다.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lines: list[DiffLine] = []
        self._positions: list[tuple[int, int] | None] = []
        self._hunk_of_row: list[int | None] = []
        self._patch: FilePatch | None = None

    def set_lines(
        self,
        lines: list[DiffLine],
        positions: list[tuple[int, int] | None] | None = None,
        patch: FilePatch | None = None,
    ) -> None:
        self.beginResetModel()
        self._lines = lines
        self._positions = positions or [None] * len(lines)
        self._hunk_of_row = self._map_rows_to_hunks(lines)
        self._patch = patch
        self.endResetModel()

    @staticmethod
    def _map_rows_to_hunks(lines: list[DiffLine]) -> list[int | None]:
        """각 행이 속한 헝크 인덱스. 헝크 머리글이 그 헝크의 시작이다."""
        out: list[int | None] = []
        current = -1
        for line in lines:
            if line.kind is DiffLineKind.HUNK_HEADER:
                current += 1
            out.append(current if current >= 0 else None)
        return out

    def hunk_at(self, row: int) -> int | None:
        """행이 속한 헝크 인덱스 — 머리글과 컨텍스트 줄에도 답한다.

        좌표(`position_at`)는 변경 줄에만 붙으므로, 좌표를 위로 훑어 헝크를
        추측하면 머리글이나 선행 컨텍스트에서 **앞 헝크**로 새어 나간다.
        사용자가 보고 있는 것과 다른 헝크가 조용히 올라가게 된다.
        """
        if 0 <= row < len(self._hunk_of_row):
            return self._hunk_of_row[row]
        return None

    def clear(self) -> None:
        self.set_lines([])

    @property
    def patch(self) -> FilePatch | None:
        """지금 보고 있는 diff의 패치. 부분 스테이징이 가능한지 판단에 쓴다."""
        return self._patch

    def position_at(self, row: int) -> tuple[int, int] | None:
        if 0 <= row < len(self._positions):
            return self._positions[row]
        return None

    def positions_in_hunk(self, hunk_index: int) -> set[tuple[int, int]]:
        """헝크 하나의 모든 변경 줄 좌표."""
        return {
            pos
            for pos in self._positions
            if pos is not None and pos[0] == hunk_index
        }

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
        if role == DiffRole.PATCH_POSITION:
            return self._positions[row]
        if role == Qt.ItemDataRole.DisplayRole:
            return line.text
        return None
