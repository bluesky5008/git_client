"""DiffModel 단위 테스트.

Phase 2의 헝크 스테이징이 이 코드를 건드리기 전에 현재 동작을 고정한다.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QModelIndex, Qt

from gitclient.domain.models import DiffLine, DiffLineKind
from gitclient.viewmodel.diff_model import DiffModel, DiffRole


def sample_lines() -> list[DiffLine]:
    return [
        DiffLine(kind=DiffLineKind.FILE_HEADER, text="a.txt"),
        DiffLine(kind=DiffLineKind.HUNK_HEADER, text="@@ -1,2 +1,2 @@"),
        DiffLine(kind=DiffLineKind.CONTEXT, text="unchanged", old_lineno=1, new_lineno=1),
        DiffLine(kind=DiffLineKind.DELETION, text="old", old_lineno=2),
        DiffLine(kind=DiffLineKind.ADDITION, text="new", new_lineno=2),
    ]


@pytest.fixture
def model(qtbot) -> DiffModel:  # noqa: ANN001 - pytest-qt 픽스처
    m = DiffModel()
    m.set_lines(sample_lines())
    return m


class TestDiffModel:
    def test_row_count(self, model: DiffModel) -> None:
        assert model.rowCount() == 5

    def test_display_role_is_text(self, model: DiffModel) -> None:
        assert model.index(3).data(Qt.ItemDataRole.DisplayRole) == "old"

    def test_line_role_returns_domain_object(self, model: DiffModel) -> None:
        line = model.index(4).data(DiffRole.LINE)
        assert isinstance(line, DiffLine)
        assert line.kind is DiffLineKind.ADDITION

    def test_invalid_index_returns_none(self, model: DiffModel) -> None:
        assert model.data(QModelIndex()) is None

    def test_out_of_range_returns_none(self, model: DiffModel) -> None:
        assert model.index(99).data(Qt.ItemDataRole.DisplayRole) is None

    def test_clear_empties_model(self, model: DiffModel) -> None:
        model.clear()
        assert model.rowCount() == 0

    def test_set_lines_resets_model(self, model: DiffModel, qtbot) -> None:  # noqa: ANN001
        with qtbot.waitSignal(model.modelReset, timeout=1000):
            model.set_lines(sample_lines()[:2])
        assert model.rowCount() == 2
