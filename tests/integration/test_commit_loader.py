"""CommitLoader 통합 테스트.

워커가 실제 저장소를 읽어 묶음으로 넘기는지, 취소가 동작하는지 확인한다.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest
from PySide6.QtCore import QThreadPool

from gitclient.application.commit_loader import CommitLoader
from gitclient.domain.errors import GitClientError

SIGNATURE = pygit2.Signature("테스터", "tester@example.com", 1700000000, 540)


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    path = tmp_path / "sample"
    repo = pygit2.init_repository(str(path), initial_head="main")

    parents: list = []
    blob = repo.create_blob(b"x\n")
    builder = repo.TreeBuilder()
    builder.insert("f.txt", blob, pygit2.enums.FileMode.BLOB)
    tree = builder.write()

    for i in range(5):
        oid = repo.create_commit(
            "refs/heads/main", SIGNATURE, SIGNATURE, f"커밋 {i}", tree, parents
        )
        parents = [oid]
    return path


def run_loader(loader: CommitLoader, qtbot) -> None:  # noqa: ANN001
    pool = QThreadPool()
    with qtbot.waitSignal(loader.signals.finished, timeout=10_000):
        pool.start(loader)
    pool.waitForDone(5000)


class TestCommitLoader:
    def test_emits_all_commits(self, repo_path: Path, qtbot) -> None:  # noqa: ANN001
        loader = CommitLoader(repo_path)
        received: list = []
        loader.signals.batch_ready.connect(received.extend)

        run_loader(loader, qtbot)

        assert len(received) == 5

    def test_reports_total_on_finish(
        self, repo_path: Path, qtbot  # noqa: ANN001
    ) -> None:
        loader = CommitLoader(repo_path)
        with qtbot.waitSignal(loader.signals.finished, timeout=10_000) as blocker:
            QThreadPool.globalInstance().start(loader)
        assert blocker.args == [5]

    def test_commits_are_newest_first(
        self, repo_path: Path, qtbot  # noqa: ANN001
    ) -> None:
        loader = CommitLoader(repo_path)
        received: list = []
        loader.signals.batch_ready.connect(received.extend)

        run_loader(loader, qtbot)

        assert [c.summary for c in received] == [f"커밋 {i}" for i in (4, 3, 2, 1, 0)]

    def test_invalid_path_emits_failure(
        self, tmp_path: Path, qtbot  # noqa: ANN001
    ) -> None:
        loader = CommitLoader(tmp_path / "nope")
        with qtbot.waitSignal(loader.signals.failed, timeout=10_000) as blocker:
            QThreadPool.globalInstance().start(loader)
        assert isinstance(blocker.args[0], GitClientError)

    def test_auto_delete_is_disabled(self, repo_path: Path) -> None:
        # Qt가 run() 직후 C++ 객체를 지우면, 아직 파이썬이 참조 중인 경우
        # 죽은 객체를 건드리게 되고 시그널 방출 도중 sender가 사라진다.
        assert CommitLoader(repo_path).autoDelete() is False

    def test_all_slots_run_even_if_one_drops_the_reference(
        self, repo_path: Path, qtbot  # noqa: ANN001
    ) -> None:
        """앞선 슬롯이 로더 참조를 버려도 뒤 슬롯이 실행되어야 한다.

        시그널 방출 도중 마지막 참조를 놓으면 sender가 파괴되어 이후 슬롯이
        조용히 누락되는 결함의 회귀 테스트다. (현재 MainWindow는 참조를
        유지하는 방식이지만, 이 클래스의 버그는 어느 호출자에서든 재발할 수
        있으므로 로더 계약 수준에서 고정한다.)
        """
        holder = {"loader": CommitLoader(repo_path)}
        calls: list[str] = []

        def first(_total: int) -> None:
            calls.append("first")
            holder["loader"] = None  # 참조를 버린다

        def second(_total: int) -> None:
            calls.append("second")

        holder["loader"].signals.finished.connect(first)
        holder["loader"].signals.finished.connect(second)

        pool = QThreadPool()
        with qtbot.waitSignal(
            holder["loader"].signals.finished, timeout=10_000
        ):
            pool.start(holder["loader"])
        pool.waitForDone(5000)
        qtbot.wait(50)

        assert calls == ["first", "second"]

    def test_cancel_before_start_emits_nothing(
        self, repo_path: Path, qtbot  # noqa: ANN001
    ) -> None:
        loader = CommitLoader(repo_path)
        received: list = []
        loader.signals.batch_ready.connect(received.extend)
        loader.cancel()

        pool = QThreadPool()
        pool.start(loader)
        pool.waitForDone(5000)

        assert received == []
        assert loader.is_cancelled is True
