"""부분 스테이징 UI 통합 테스트.

화면에서 고른 줄이 실제로 그 줄만 스테이징되는지, 즉 **표시 좌표와 패치
좌표가 일치하는지**를 검증한다. 둘이 어긋나면 사용자는 엉뚱한 줄이
올라가는 것을 보게 되므로 이 경로의 검증이 특히 중요하다.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest
from PySide6.QtCore import QItemSelectionModel

from gitclient.domain.models import DiffLineKind
from gitclient.ui.main_window import MainWindow
from gitclient.viewmodel.diff_model import DiffRole

SIGNATURE = pygit2.Signature("테스터", "tester@example.com", 1700000000, 540)
TIMEOUT = 10_000


@pytest.fixture
def repo(tmp_path: Path) -> pygit2.Repository:
    r = pygit2.init_repository(str(tmp_path / "partial"), initial_head="main")
    r.config["user.name"] = "테스터"
    r.config["user.email"] = "tester@example.com"
    (Path(r.workdir) / "a.txt").write_text("1\n2\n3\n4\n5\n", encoding="utf-8")
    r.index.add_all()
    r.index.write()
    r.create_commit("HEAD", SIGNATURE, SIGNATURE, "init", r.index.write_tree(), [])
    return r


@pytest.fixture
def window(qtbot, repo: pygit2.Repository):  # noqa: ANN001, ANN201
    w = MainWindow()
    qtbot.addWidget(w)
    errors: list = []
    w._report = errors.append
    w.reported_errors = errors

    # 두 군데를 바꾼다 → 한 헝크 안에 변경 두 쌍
    (Path(repo.workdir) / "a.txt").write_text("1\nTWO\n3\n4\nFIVE\n", encoding="utf-8")

    w.open_repository(str(repo.workdir))
    qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
    qtbot.waitUntil(
        lambda: w._work_panel._unstaged_list.count() == 1, timeout=TIMEOUT
    )
    return w


def show_unstaged_diff(window, qtbot) -> None:  # noqa: ANN001
    window._work_panel.diff_requested.emit("a.txt", False)
    qtbot.waitUntil(lambda: window._diff_model.patch is not None, timeout=TIMEOUT)


def staged_content(repo: pygit2.Repository) -> str:
    repo.index.read(True)
    return repo[repo.index["a.txt"].id].data.decode("utf-8")


def row_of(window, text: str) -> int:  # noqa: ANN001
    model = window._diff_model
    for row in range(model.rowCount()):
        if model.index(row).data() == text:
            return row
    raise AssertionError(f"화면에서 {text!r} 줄을 찾지 못했다")


class TestDiffCoordinates:
    def test_change_rows_carry_patch_positions(self, window, qtbot) -> None:  # noqa: ANN001
        show_unstaged_diff(window, qtbot)
        model = window._diff_model

        positions = [
            model.index(r).data(DiffRole.PATCH_POSITION)
            for r in range(model.rowCount())
        ]
        kinds = [
            model.index(r).data(DiffRole.LINE).kind for r in range(model.rowCount())
        ]

        for kind, position in zip(kinds, positions):
            if kind in (DiffLineKind.ADDITION, DiffLineKind.DELETION):
                assert position is not None, "변경 줄에는 좌표가 있어야 한다"
            else:
                assert position is None, "변경이 아닌 줄에는 좌표가 없어야 한다"

    def test_binary_file_disables_partial_actions(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        (Path(repo.workdir) / "blob.bin").write_bytes(b"\x00\x01" * 64)
        window._refresh_status()
        qtbot.waitUntil(
            lambda: window._work_panel._unstaged_list.count() == 2, timeout=TIMEOUT
        )
        window._work_panel.diff_requested.emit("blob.bin", False)
        qtbot.waitUntil(
            lambda: window._diff_model.patch is not None
            and window._diff_model.patch.is_binary,
            timeout=TIMEOUT,
        )
        assert not window._stage_hunk_button.isEnabled()
        assert not window._stage_lines_button.isEnabled()
        assert "바이너리" in window._partial_hint.text()


class TestStageSelectedLines:
    def test_staging_one_line_pair_leaves_the_other(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        """화면에서 첫 변경 쌍만 골라 올린다 — 이 기능의 존재 이유."""
        show_unstaged_diff(window, qtbot)

        selection = window._diff_view.selectionModel()
        for text in ("2", "TWO"):
            selection.select(
                window._diff_model.index(row_of(window, text)),
                QItemSelectionModel.SelectionFlag.Select,
            )
        assert len(window._selected_positions()) == 2

        window._on_stage_lines()

        qtbot.waitUntil(
            lambda: staged_content(repo) == "1\nTWO\n3\n4\n5\n", timeout=TIMEOUT
        )
        # 워킹 트리는 그대로
        assert (
            Path(repo.workdir) / "a.txt"
        ).read_text(encoding="utf-8") == "1\nTWO\n3\n4\nFIVE\n"
        assert window.reported_errors == []

    def test_file_shows_in_both_lists_after_partial_stage(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        show_unstaged_diff(window, qtbot)
        selection = window._diff_view.selectionModel()
        for text in ("2", "TWO"):
            selection.select(
                window._diff_model.index(row_of(window, text)),
                QItemSelectionModel.SelectionFlag.Select,
            )
        window._on_stage_lines()

        panel = window._work_panel
        qtbot.waitUntil(
            lambda: panel._staged_list.count() == 1 and panel._unstaged_list.count() == 1,
            timeout=TIMEOUT,
        )

    def test_button_disabled_without_selection(self, window, qtbot) -> None:  # noqa: ANN001
        show_unstaged_diff(window, qtbot)
        window._diff_view.selectionModel().clearSelection()
        window._update_partial_actions()
        assert not window._stage_lines_button.isEnabled()


class TestStageHunk:
    def test_hunk_button_stages_whole_hunk(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        show_unstaged_diff(window, qtbot)

        # 커서를 헝크 안 아무 줄에나 둔다
        window._diff_view.setCurrentIndex(
            window._diff_model.index(row_of(window, "TWO"))
        )
        window._on_stage_hunk()

        qtbot.waitUntil(
            lambda: staged_content(repo) == "1\nTWO\n3\n4\nFIVE\n", timeout=TIMEOUT
        )

    def test_hunk_button_works_from_context_line(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        """커서가 컨텍스트 줄에 있어도 그 헝크를 찾아야 한다."""
        show_unstaged_diff(window, qtbot)
        window._diff_view.setCurrentIndex(
            window._diff_model.index(row_of(window, "3"))
        )
        assert window._current_hunk_positions()

        window._on_stage_hunk()
        qtbot.waitUntil(
            lambda: staged_content(repo) == "1\nTWO\n3\n4\nFIVE\n", timeout=TIMEOUT
        )


class TestUnstageFromStagedView:
    def test_buttons_switch_to_unstage_wording(self, window, qtbot) -> None:  # noqa: ANN001
        window._work_panel.stage_requested.emit("a.txt")
        qtbot.waitUntil(
            lambda: window._work_panel._staged_list.count() == 1, timeout=TIMEOUT
        )
        window._work_panel.diff_requested.emit("a.txt", True)
        qtbot.waitUntil(lambda: window._diff_model.patch is not None, timeout=TIMEOUT)

        assert "내리기" in window._stage_hunk_button.text()

    def test_unstaging_selected_lines(self, window, qtbot, repo) -> None:  # noqa: ANN001
        window._work_panel.stage_requested.emit("a.txt")
        qtbot.waitUntil(
            lambda: window._work_panel._staged_list.count() == 1, timeout=TIMEOUT
        )
        window._work_panel.diff_requested.emit("a.txt", True)
        qtbot.waitUntil(lambda: window._diff_model.patch is not None, timeout=TIMEOUT)

        selection = window._diff_view.selectionModel()
        for text in ("5", "FIVE"):
            selection.select(
                window._diff_model.index(row_of(window, text)),
                QItemSelectionModel.SelectionFlag.Select,
            )
        window._on_stage_lines()

        # FIVE 변경만 내려가고 TWO는 인덱스에 남는다
        qtbot.waitUntil(
            lambda: staged_content(repo) == "1\nTWO\n3\n4\n5\n", timeout=TIMEOUT
        )
        assert window.reported_errors == []


class TestCoordinatesRefreshAfterApply:
    """확정된 결함의 회귀 테스트: 적용 후 좌표가 낡으면 다음 적용이 엉뚱한 줄에 꽂힌다."""

    def test_second_apply_targets_the_right_lines(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        show_unstaged_diff(window, qtbot)

        # 1차: 첫 변경 쌍만 올린다
        selection = window._diff_view.selectionModel()
        for text in ("2", "TWO"):
            selection.select(
                window._diff_model.index(row_of(window, text)),
                QItemSelectionModel.SelectionFlag.Select,
            )
        window._on_stage_lines()
        qtbot.waitUntil(
            lambda: staged_content(repo) == "1\nTWO\n3\n4\n5\n", timeout=TIMEOUT
        )

        # 적용 후 diff가 자동으로 다시 읽혀 남은 변경만 남아야 한다
        qtbot.waitUntil(
            lambda: window._diff_model.patch is not None
            and window._diff_model.rowCount() > 0,
            timeout=TIMEOUT,
        )
        # 이미 올린 변경은 더 이상 '변경 줄'이 아니어야 한다.
        # (인덱스에 반영됐으므로 컨텍스트로는 남는다 — 그건 정상이다)
        model = window._diff_model
        changed_texts = {
            model.index(r).data()
            for r in range(model.rowCount())
            if model.index(r).data(DiffRole.PATCH_POSITION) is not None
        }
        assert "TWO" not in changed_texts, "이미 올린 변경이 아직 변경 줄로 남아 있다"
        assert "FIVE" in changed_texts

        # 2차: 남은 변경을 올린다 — 낡은 좌표였다면 엉뚱한 줄에 적용된다
        selection = window._diff_view.selectionModel()
        selection.clearSelection()
        for text in ("5", "FIVE"):
            selection.select(
                window._diff_model.index(row_of(window, text)),
                QItemSelectionModel.SelectionFlag.Select,
            )
        window._on_stage_lines()

        qtbot.waitUntil(
            lambda: staged_content(repo) == "1\nTWO\n3\n4\nFIVE\n", timeout=TIMEOUT
        )
        assert window.reported_errors == []

    def test_rapid_double_apply_is_guarded(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        """연타해도 낡은 좌표로 두 번 적용되면 안 된다."""
        show_unstaged_diff(window, qtbot)
        selection = window._diff_view.selectionModel()
        for text in ("2", "TWO"):
            selection.select(
                window._diff_model.index(row_of(window, text)),
                QItemSelectionModel.SelectionFlag.Select,
            )
        window._on_stage_lines()
        window._on_stage_lines()  # 즉시 재클릭

        qtbot.waitUntil(
            lambda: staged_content(repo) == "1\nTWO\n3\n4\n5\n", timeout=TIMEOUT
        )
        qtbot.wait(300)
        assert staged_content(repo) == "1\nTWO\n3\n4\n5\n"
        assert window.reported_errors == []


class TestHunkCursorResolution:
    """확정된 결함의 회귀 테스트: 커서가 머리글/선행 컨텍스트면 앞 헝크로 새어나갔다."""

    @pytest.fixture
    def multi_hunk_window(self, qtbot, repo, tmp_path):  # noqa: ANN001
        big = "\n".join(str(i) for i in range(1, 41)) + "\n"
        (Path(repo.workdir) / "big.txt").write_text(big, encoding="utf-8")
        repo.index.add("big.txt")
        repo.index.write()
        repo.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "big", repo.index.write_tree(),
            [repo.head.target],
        )
        changed = big.replace("2\n", "TWO\n", 1).replace("38\n", "THIRTY8\n", 1)
        (Path(repo.workdir) / "big.txt").write_text(changed, encoding="utf-8")

        w = MainWindow()
        qtbot.addWidget(w)
        w._report = lambda e: None
        w.open_repository(str(repo.workdir))
        qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
        w._work_panel.diff_requested.emit("big.txt", False)
        qtbot.waitUntil(lambda: w._diff_model.patch is not None, timeout=TIMEOUT)
        assert len(w._diff_model.patch.hunks) == 2
        return w

    def test_cursor_on_second_hunk_header_selects_second_hunk(
        self, multi_hunk_window, qtbot  # noqa: ANN001
    ) -> None:
        window = multi_hunk_window
        model = window._diff_model
        hunk_header_rows = [
            r
            for r in range(model.rowCount())
            if model.index(r).data(DiffRole.LINE).kind is DiffLineKind.HUNK_HEADER
        ]
        assert len(hunk_header_rows) == 2

        window._diff_view.setCurrentIndex(model.index(hunk_header_rows[1]))
        positions = window._current_hunk_positions()

        assert positions, "헝크 머리글에서 버튼이 죽으면 안 된다"
        assert all(hunk == 1 for hunk, _ in positions), "앞 헝크로 새어나갔다"

    def test_cursor_on_leading_context_of_second_hunk(
        self, multi_hunk_window, qtbot, repo  # noqa: ANN001
    ) -> None:
        window = multi_hunk_window
        model = window._diff_model
        # 두 번째 헝크의 선행 컨텍스트('35' 근처)
        target_row = next(
            r
            for r in range(model.rowCount())
            if model.index(r).data() == "35"
        )
        window._diff_view.setCurrentIndex(model.index(target_row))

        positions = window._current_hunk_positions()
        assert all(hunk == 1 for hunk, _ in positions)

        window._on_stage_hunk()
        qtbot.waitUntil(
            lambda: "THIRTY8" in staged_content_of(repo, "big.txt"), timeout=TIMEOUT
        )
        # 첫 헝크는 손대지 않았다
        assert "TWO" not in staged_content_of(repo, "big.txt")


def staged_content_of(repo: pygit2.Repository, path: str) -> str:
    repo.index.read(True)
    return repo[repo.index[path].id].data.decode("utf-8")


class TestCommitDiffHasNoPartialActions:
    def test_commit_diff_disables_partial_staging(self, window, qtbot) -> None:  # noqa: ANN001
        """커밋 diff는 이미 확정된 히스토리라 스테이징 대상이 아니다."""
        window._commit_view.selectRow(0)
        qtbot.waitUntil(
            lambda: window._diff_source is None and window._diff_model.rowCount() > 0,
            timeout=TIMEOUT,
        )
        assert not window._stage_hunk_button.isEnabled()
        assert not window._stage_lines_button.isEnabled()
