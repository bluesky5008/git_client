"""파일시스템 감시 — 앱 밖 조작이 F5 없이 반영된다 (F2, ADR-85, AC-02).

두 가지를 지킨다: 밖에서 바뀐 것은 **알아채고**, 앱 자신이 바꾼 것으로는
**두 번 새로 고치지 않는다** — 이중 재로딩은 스크롤·선택을 이유 없이
날린다.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.integration.remote_harness import AUTHOR_ENV, git

TIMEOUT = 20_000


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    commit_all(root, "base")
    return root


@pytest.fixture
def window(qtbot, repo: Path):  # noqa: ANN001, ANN201
    from gitclient.ui.main_window import MainWindow

    w = MainWindow()
    qtbot.addWidget(w)
    w._report = lambda _e: None
    w.open_repository(str(repo))
    qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
    return w


class TestExternalChangesAreNoticed:
    def test_gitdir_signals_are_watched(self, window, repo: Path) -> None:  # noqa: ANN001
        watched = set(
            window._fs_watcher.files() + window._fs_watcher.directories()
        )
        gitdir = repo / ".git"
        assert any(Path(p) == gitdir for p in watched), "gitdir을 보고 있지 않다"
        assert any(
            Path(p) == gitdir / "refs" / "heads" for p in watched
        ), "refs/heads를 보고 있지 않다"

    def test_external_commit_reloads_the_graph(
        self, window, repo: Path, qtbot  # noqa: ANN001
    ) -> None:
        """AC-02의 본체 — 진짜 파일시스템 이벤트로 끝까지 간다."""
        assert window._commit_model.rowCount() == 1
        window._suppress_fs_until = 0.0  # 열기 직후의 도장을 걷어낸다

        (repo / "g.txt").write_text("외부에서\n", encoding="utf-8")
        commit_all(repo, "외부 커밋")  # 앱 밖의 git CLI

        qtbot.waitUntil(
            lambda: window._commit_model.rowCount() == 2
            and not window._loading,
            timeout=TIMEOUT,
        )

    def test_settled_reload_is_direct(self, window, repo: Path, qtbot) -> None:  # noqa: ANN001
        """디바운스가 끝나면 재로딩한다 — 핸들러 직접 검증 (플랫폼 이벤트
        지연과 무관하게 이 경로 자체가 옳은지)."""
        (repo / "g.txt").write_text("외부\n", encoding="utf-8")
        commit_all(repo, "외부 커밋")
        window._suppress_fs_until = 0.0

        window._on_fs_settled()

        qtbot.waitUntil(
            lambda: window._commit_model.rowCount() == 2, timeout=TIMEOUT
        )


class TestSelfChangesAreSuppressed:
    def test_own_write_stamps_the_suppression(self, window, qtbot) -> None:  # noqa: ANN001
        window._suppress_fs_until = 0.0

        window._submit_write("아무 쓰기", lambda engine: None)
        qtbot.waitUntil(
            lambda: not window._write_queue.is_busy, timeout=TIMEOUT
        )

        assert window._suppress_fs_until > time.monotonic() - 1

    def test_settled_does_nothing_inside_the_window(
        self, window, repo: Path  # noqa: ANN001
    ) -> None:
        (repo / "g.txt").write_text("외부\n", encoding="utf-8")
        commit_all(repo, "외부 커밋")
        window._suppress_fs_until = time.monotonic() + 5

        window._on_fs_settled()

        assert window._commit_model.rowCount() == 1, "도장을 무시하고 재로딩했다"

    def test_settled_yields_while_busy(self, window, repo: Path) -> None:  # noqa: ANN001
        (repo / "g.txt").write_text("외부\n", encoding="utf-8")
        commit_all(repo, "외부 커밋")
        window._suppress_fs_until = 0.0
        window._loading = True
        try:
            window._on_fs_settled()
            assert window._commit_model.rowCount() == 1
        finally:
            window._loading = False


class TestActivationRefreshesStatus:
    def test_returning_to_the_window_rereads_the_working_tree(
        self, window, repo: Path, qtbot  # noqa: ANN001
    ) -> None:
        """편집기에서 돌아온 순간이 최신 상태를 기대하는 순간이다."""
        (repo / "f.txt").write_text("편집기에서 고침\n", encoding="utf-8")

        window._refresh_status()  # 활성화 이벤트가 부르는 그 경로

        panel = window._work_panel
        qtbot.waitUntil(
            lambda: any(
                "f.txt" in panel._unstaged_list.item(i).text()
                for i in range(panel._unstaged_list.count())
            ),
            timeout=TIMEOUT,
        )
