"""MainWindow 상호작용 테스트 (pytest-qt, 실제 위젯 대상).

설계 §8의 "UI: 위젯 상호작용, 시그널 전파" 행을 이행한다.
로딩이 전부 비동기이므로 단정은 qtbot.waitUntil로 수렴을 기다린 뒤 한다.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest
from PySide6.QtCore import Qt

from gitclient.domain.errors import GitClientError
from gitclient.domain.models import DiffLineKind
from gitclient.ui.main_window import MainWindow
from gitclient.viewmodel.diff_model import DiffRole

SIGNATURE = pygit2.Signature("테스터", "tester@example.com", 1700000000, 540)

TIMEOUT = 10_000


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    """마지막 커밋이 파일 2개를 바꾸는 저장소. diff 좁히기 검증용."""
    path = tmp_path / "ui-repo"
    repo = pygit2.init_repository(str(path), initial_head="main")

    def commit(files: dict[str, str], message: str) -> None:
        for name, content in files.items():
            (path / name).write_text(content, encoding="utf-8")
            repo.index.add(name)
        repo.index.write()
        tree = repo.index.write_tree()
        parents = [] if repo.head_is_unborn else [repo.head.target]
        repo.create_commit("HEAD", SIGNATURE, SIGNATURE, message, tree, parents)

    commit({"a.txt": "one\n"}, "첫 커밋")
    commit({"a.txt": "one\ntwo\n", "b.txt": "bee\n"}, "두 파일 변경")
    repo.create_tag(
        "v1", repo.head.target, pygit2.enums.ObjectType.COMMIT, SIGNATURE, "v1"
    )
    return path


@pytest.fixture
def window(qtbot, repo_path: Path):  # noqa: ANN001, ANN201
    w = MainWindow()
    qtbot.addWidget(w)  # qtbot이 테스트 종료 시 닫아준다

    errors: list[GitClientError] = []
    w._report = errors.append  # 모달을 띄우는 대신 수집한다
    w.reported_errors = errors

    w.open_repository(str(repo_path))
    # 커밋 로딩 + 자동 선택 + 디바운스 + diff 워커까지 전부 수렴할 때까지.
    qtbot.waitUntil(lambda: w._diff_model.rowCount() > 0, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: w._ref_list.count() > 0, timeout=TIMEOUT)
    return w


def diff_file_headers(window) -> list[str]:  # noqa: ANN001
    model = window._diff_model
    return [
        model.index(i).data(Qt.ItemDataRole.DisplayRole)
        for i in range(model.rowCount())
        if model.index(i).data(DiffRole.LINE).kind is DiffLineKind.FILE_HEADER
    ]


class TestOpenRepository:
    def test_commits_are_loaded(self, window) -> None:  # noqa: ANN001
        assert window._commit_model.rowCount() == 2

    def test_first_row_is_auto_selected(self, window) -> None:  # noqa: ANN001
        assert window._commit_view.currentIndex().row() == 0

    def test_window_title_shows_repo_name(self, window) -> None:  # noqa: ANN001
        assert "ui-repo" in window.windowTitle()

    def test_refs_panel_has_groups_and_entries(self, window) -> None:  # noqa: ANN001
        labels = [window._ref_list.item(i).text() for i in range(window._ref_list.count())]
        assert any("로컬 브랜치" in label for label in labels)
        assert any("main" in label for label in labels)
        assert any("v1" in label for label in labels)

    def test_no_errors_were_reported(self, window) -> None:  # noqa: ANN001
        assert window.reported_errors == []

    def test_loader_reference_survives_completion(self, window, qtbot) -> None:  # noqa: ANN001
        """finished 이후에도 로더 참조를 유지해야 한다.

        참조를 버리면 시그널 방출 도중 sender가 파괴되는 결함(수정 이력 있음)이
        재발한다. 이 단정은 그 리팩터 회귀를 잡는다.
        """
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
        assert window._loader is not None
        assert window._refs_loader is not None


class TestDiffPanel:
    def test_full_diff_covers_both_changed_files(self, window) -> None:  # noqa: ANN001
        assert set(diff_file_headers(window)) == {"a.txt", "b.txt"}

    def test_file_list_offers_whole_and_files(self, window) -> None:  # noqa: ANN001
        texts = [window._file_list.item(i).text() for i in range(window._file_list.count())]
        assert any("전체 변경" in t for t in texts)
        assert any("a.txt" in t for t in texts)
        assert any("b.txt" in t for t in texts)

    def test_selecting_file_narrows_diff(self, window, qtbot) -> None:  # noqa: ANN001
        target_row = next(
            i
            for i in range(window._file_list.count())
            if "b.txt" in window._file_list.item(i).text()
        )
        window._file_list.setCurrentRow(target_row)
        qtbot.waitUntil(
            lambda: diff_file_headers(window) == ["b.txt"], timeout=TIMEOUT
        )

    def test_selecting_whole_restores_full_diff(self, window, qtbot) -> None:  # noqa: ANN001
        narrow = next(
            i
            for i in range(window._file_list.count())
            if "b.txt" in window._file_list.item(i).text()
        )
        window._file_list.setCurrentRow(narrow)
        qtbot.waitUntil(lambda: diff_file_headers(window) == ["b.txt"], timeout=TIMEOUT)

        window._file_list.setCurrentRow(0)  # "전체 변경"
        qtbot.waitUntil(
            lambda: set(diff_file_headers(window)) == {"a.txt", "b.txt"},
            timeout=TIMEOUT,
        )

    def test_switching_commit_repopulates_file_list(self, window, qtbot) -> None:  # noqa: ANN001
        window._commit_view.selectRow(1)  # 첫 커밋 (a.txt만 추가)
        qtbot.waitUntil(
            lambda: diff_file_headers(window) == ["a.txt"], timeout=TIMEOUT
        )
        texts = [window._file_list.item(i).text() for i in range(window._file_list.count())]
        assert not any("b.txt" in t for t in texts)


class TestRefNavigation:
    def test_activating_ref_selects_target_commit(self, window, qtbot) -> None:  # noqa: ANN001
        window._commit_view.selectRow(1)
        qtbot.waitUntil(
            lambda: window._commit_view.currentIndex().row() == 1, timeout=TIMEOUT
        )

        main_item = next(
            window._ref_list.item(i)
            for i in range(window._ref_list.count())
            if "main" in window._ref_list.item(i).text()
            and window._ref_list.item(i).data(Qt.ItemDataRole.UserRole)
        )
        window._on_ref_activated(main_item)
        assert window._commit_view.currentIndex().row() == 0

    def test_header_item_is_ignored(self, window) -> None:  # noqa: ANN001
        header = next(
            window._ref_list.item(i)
            for i in range(window._ref_list.count())
            if window._ref_list.item(i).data(Qt.ItemDataRole.UserRole) is None
        )
        before = window._commit_view.currentIndex().row()
        window._on_ref_activated(header)
        assert window._commit_view.currentIndex().row() == before


class TestErrorPath:
    def test_invalid_path_reports_error(self, qtbot, tmp_path: Path) -> None:  # noqa: ANN001
        w = MainWindow()
        qtbot.addWidget(w)
        errors: list[GitClientError] = []
        w._report = errors.append

        w.open_repository(str(tmp_path / "not-a-repo"))

        assert len(errors) == 1
        assert errors[0].action is not None  # 권장 조치가 실려 있다 (§5.2 원칙 4)


class TestRepositorySwitch:
    def test_stale_results_do_not_leak_into_new_repo(
        self, window, qtbot, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """로딩 중 다른 저장소를 열어도 이전 저장소의 데이터가 섞이지 않는다."""
        other = tmp_path / "other-repo"
        repo = pygit2.init_repository(str(other), initial_head="main")
        (other / "solo.txt").write_text("x", encoding="utf-8")
        repo.index.add("solo.txt")
        repo.index.write()
        tree = repo.index.write_tree()
        repo.create_commit("HEAD", SIGNATURE, SIGNATURE, "단독 커밋", tree, [])

        window.open_repository(str(other))
        qtbot.waitUntil(
            lambda: window._commit_model.rowCount() == 1, timeout=TIMEOUT
        )
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        assert window._commit_model.commit_at(0).summary == "단독 커밋"
        assert "other-repo" in window.windowTitle()

    def test_switching_does_not_query_the_old_head(
        self, window, qtbot, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """이전 저장소의 HEAD로 새 저장소를 조회하던 결함 (실사용 보고).

        파일 목록을 비우는 clear()가 currentRowChanged를 발화시키는
        시점에 _current_sha가 아직 이전 저장소의 것이면, 가드 셋(row·
        sha·경로)이 전부 통과해 새 엔진에 옛 sha를 묻는다 — 사용자에게
        "커밋을 찾을 수 없습니다: <이전 HEAD>"가 떴다. 상태 무효화가
        위젯 정리보다 먼저여야 한다.
        """
        # fixture가 diff까지 로딩했으므로 파일 목록에 current 행이 있고
        # _current_sha는 이 저장소의 HEAD다 — 결함이 요구하는 사용 흔적.
        old_head = window._current_sha
        assert old_head is not None

        # 오류 "보고"를 기다리는 검증은 비결정적이다 — 늦은 실패가 새
        # 요청 뒤에 도착하면 세대 토큰이 조용히 버린다. 결함의 본질은
        # **이전 sha로 조회가 나간다는 것**이므로 요청 자체를 잡는다.
        seen: list[str] = []
        original = window._request_diff

        def recording(sha: str, **kwargs):  # noqa: ANN003, ANN202
            seen.append(sha)
            return original(sha, **kwargs)

        window._request_diff = recording

        other = tmp_path / "second-repo"
        repo = pygit2.init_repository(str(other), initial_head="main")
        (other / "solo.txt").write_text("x", encoding="utf-8")
        repo.index.add("solo.txt")
        repo.index.write()
        tree = repo.index.write_tree()
        repo.create_commit("HEAD", SIGNATURE, SIGNATURE, "단독 커밋", tree, [])

        window.open_repository(str(other))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        assert old_head not in seen, (
            "이전 저장소의 HEAD로 새 저장소에 diff를 요청했다"
        )
        assert [e.message for e in window.reported_errors] == []


class TestLateWorkerResultsAfterClose:
    """창이 닫힌 뒤 도착한 읽기 결과가 파괴된 위젯을 만지면 안 된다.

    `cancel()`은 앞으로의 방출만 막는다 — 이미 이벤트 루프에 실린 결과는
    배달되고, 그때 위젯이 없으면 "Internal C++ object already deleted"로
    터진다 (Windows CI 실측). fetch 워커는 같은 이유로 이미 시그널을
    끊고 있었는데 읽기 워커에는 그 처리가 빠져 있었다.
    """

    def test_close_detaches_reader_signals(self, qtbot, tmp_path) -> None:  # noqa: ANN001
        from tests.integration.remote_harness import AUTHOR_ENV, git

        root = tmp_path / "work"
        root.mkdir()
        git("init", "--quiet", "-b", "main", str(root))
        (root / "f.txt").write_text("x\n", encoding="utf-8")
        git("add", "-A", cwd=root)
        git(*AUTHOR_ENV, "commit", "--quiet", "-m", "c", cwd=root)

        window = MainWindow()
        qtbot.addWidget(window)
        window._report = lambda _e: None
        window.open_repository(str(root))
        qtbot.waitUntil(lambda: not window._loading, timeout=10_000)
        refs_loader = window._refs_loader
        assert refs_loader is not None, "전제가 깨졌다 — 참조 로더가 있어야 한다"

        window.close()

        # 닫힌 뒤 늦게 도착한 결과 — 예외 없이 무시돼야 한다.
        refs_loader.signals.ready.emit([], None)
