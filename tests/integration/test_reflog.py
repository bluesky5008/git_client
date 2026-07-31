"""reflog 탐색 — 잃은 커밋을 앱 안에서 되찾는다 (FR-09·10, AC-05).

파괴적 동작들의 안내문은 전부 "git reflog에 남는다"를 약속한다. 이
테스트는 그 약속의 전체 경로를 검증한다: 잃는다 → 찾는다 → 되찾는다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.integration.remote_harness import AUTHOR_ENV, git

TIMEOUT = 10_000


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    for index in range(3):
        (root / "f.txt").write_text(f"{index}\n", encoding="utf-8")
        commit_all(root, f"c{index}")
    return root


class TestEngineReflog:
    def test_entries_are_newest_first_with_the_action_message(
        self, repo: Path
    ) -> None:
        entries = LocalGitEngine.open(str(repo)).head_reflog()

        assert len(entries) == 3
        head = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        assert entries[0].sha == head
        assert "commit" in entries[0].message

    def test_lost_commit_is_recoverable_as_a_branch(self, repo: Path) -> None:
        """reset --hard로 잃는다 → reflog에서 찾는다 → 브랜치로 되찾는다."""
        lost = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        git("reset", "--hard", "HEAD~1", cwd=repo)
        assert lost not in git("log", "--format=%H", cwd=repo).stdout

        engine = LocalGitEngine.open(str(repo))
        entries = engine.head_reflog()
        assert any(e.sha == lost for e in entries), "잃은 커밋이 reflog에 없다"

        engine.create_branch("recovered", sha=lost)

        assert (
            git("rev-parse", "recovered", cwd=repo).stdout.strip() == lost
        ), "브랜치가 잃은 커밋을 가리키지 않는다"

    def test_limit_caps_the_walk(self, repo: Path) -> None:
        entries = LocalGitEngine.open(str(repo)).head_reflog(limit=2)
        assert len(entries) == 2

    def test_unborn_head_returns_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()
        git("init", "--quiet", "-b", "main", str(root))
        assert LocalGitEngine.open(str(root)).head_reflog() == ()


class TestReflogDialog:
    def test_rows_show_time_sha_and_message(self, qtbot, repo: Path) -> None:  # noqa: ANN001
        from gitclient.ui.reflog_dialog import ReflogDialog

        entries = LocalGitEngine.open(str(repo)).head_reflog()
        dialog = ReflogDialog(entries)
        qtbot.addWidget(dialog)

        assert dialog._list.count() == 3
        first = dialog._list.item(0).text()
        assert entries[0].sha[:7] in first
        assert "HEAD@{0}" in first

    def test_branch_request_carries_the_selected_sha(
        self, qtbot, repo: Path, monkeypatch  # noqa: ANN001
    ) -> None:
        from gitclient.ui import reflog_dialog as module

        entries = LocalGitEngine.open(str(repo)).head_reflog()
        dialog = module.ReflogDialog(entries)
        qtbot.addWidget(dialog)
        dialog._list.setCurrentRow(1)
        monkeypatch.setattr(
            module.QInputDialog,
            "getText",
            staticmethod(lambda *a, **k: ("rescue", True)),
        )
        requested: list[tuple[str, str]] = []
        dialog.branch_requested.connect(lambda sha, name: requested.append((sha, name)))

        dialog._on_branch()

        assert requested == [(entries[1].sha, "rescue")]

    def test_end_to_end_recovery_through_the_window(
        self, qtbot, repo: Path, monkeypatch  # noqa: ANN001
    ) -> None:
        """창에서 제출까지 — 쓰기가 WriteQueue를 거쳐 브랜치가 생긴다."""
        from gitclient.ui.main_window import MainWindow

        lost = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        git("reset", "--hard", "HEAD~1", cwd=repo)

        window = MainWindow()
        qtbot.addWidget(window)
        window._report = lambda _e: None
        window.open_repository(str(repo))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        window._on_reflog_branch(lost, "recovered")
        qtbot.waitUntil(
            lambda: window._write_queue is not None
            and not window._write_queue.is_busy,
            timeout=TIMEOUT,
        )

        assert git("rev-parse", "recovered", cwd=repo).stdout.strip() == lost
