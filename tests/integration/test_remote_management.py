"""원격 관리 — 앱 밖으로 나가지 않고 원격을 다룬다 (F1, AC-01).

ADR-2의 경계에서 원격 관리는 로컬 쓰기다 — 설정과 원격 추적 참조를
만질 뿐 회선에 나가지 않으므로 pygit2(LocalGitEngine)가 맡는다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.errors import EngineError
from gitclient.infrastructure.local_engine import LocalGitEngine
from gitclient.infrastructure.remote_engine import RemoteEngine
from tests.integration.remote_harness import RemoteFixture, git

TIMEOUT = 15_000


@pytest.fixture
def fixture(tmp_path: Path) -> RemoteFixture:
    return RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=8)


class TestEngineRemoteManagement:
    def test_added_remote_actually_fetches(
        self, fixture: RemoteFixture, tmp_path: Path
    ) -> None:
        """추가는 목록에 넣는 것이 아니라 **fetch가 되는 것**이다 (AC-01)."""
        standalone = tmp_path / "standalone"
        standalone.mkdir()
        git("init", "--quiet", "-b", "main", str(standalone))
        engine = LocalGitEngine.open(str(standalone))

        engine.add_remote("upstream", str(fixture.origin))

        assert ("upstream", str(fixture.origin)) in engine.list_remotes_with_urls()
        stats = RemoteEngine(str(standalone)).fetch("upstream")
        assert stats.succeeded
        assert git(
            "rev-parse", "refs/remotes/upstream/main", cwd=standalone
        ).stdout.strip()

    def test_remove_deletes_tracking_refs_but_not_local_work(
        self, fixture: RemoteFixture
    ) -> None:
        work = fixture.work
        head = git("rev-parse", "HEAD", cwd=work).stdout.strip()
        assert git("rev-parse", "refs/remotes/origin/main", cwd=work).stdout.strip()

        LocalGitEngine.open(str(work)).remove_remote("origin")

        listing = git("for-each-ref", "refs/remotes", cwd=work).stdout.strip()
        assert listing == "", "원격 추적 참조가 남았다"
        assert git("rev-parse", "HEAD", cwd=work).stdout.strip() == head

    def test_url_change_points_the_next_fetch_elsewhere(
        self, fixture: RemoteFixture, tmp_path: Path
    ) -> None:
        other = RemoteFixture(tmp_path / "other").build(commits=2, payload_kb=8)
        engine = LocalGitEngine.open(str(fixture.work))

        engine.set_remote_url("origin", str(other.origin))

        assert ("origin", str(other.origin)) in engine.list_remotes_with_urls()
        stats = RemoteEngine(str(fixture.work)).fetch("origin")
        assert stats.succeeded
        assert git(
            "rev-parse", "refs/remotes/origin/main", cwd=fixture.work
        ).stdout.strip() == git(
            "rev-parse", "main", cwd=other.origin
        ).stdout.strip()

    def test_duplicates_and_unknowns_are_refused_with_guidance(
        self, fixture: RemoteFixture
    ) -> None:
        engine = LocalGitEngine.open(str(fixture.work))

        with pytest.raises(EngineError) as caught:
            engine.add_remote("origin", "https://example.com/r.git")
        assert "이미 있습니다" in str(caught.value)

        with pytest.raises(EngineError):
            engine.remove_remote("없는-원격")
        with pytest.raises(EngineError):
            engine.set_remote_url("없는-원격", "https://example.com/r.git")
        with pytest.raises(EngineError):
            engine.add_remote("  ", "https://example.com/r.git")


class TestRemotesDialog:
    def test_requests_carry_the_selection(self, qtbot) -> None:  # noqa: ANN001
        from gitclient.ui.remotes_dialog import RemotesDialog

        dialog = RemotesDialog([("origin", "https://a/r.git")])
        qtbot.addWidget(dialog)
        seen: list[tuple] = []
        dialog.url_change_requested.connect(lambda n, u: seen.append((n, u)))

        assert dialog._selected() == ("origin", "https://a/r.git")
        dialog.url_change_requested.emit(*dialog._selected()[:1], "https://b/r.git")

        assert seen == [("origin", "https://b/r.git")]

    def test_window_wires_add_through_the_write_queue(
        self, qtbot, fixture: RemoteFixture, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """창 경유 종단 검증 — 쓰기가 큐를 지나 실제로 반영된다."""
        from gitclient.ui.main_window import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        window._report = lambda _e: None
        window.open_repository(str(fixture.work))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        window._submit_write(
            "원격 추가: mirror",
            lambda engine: engine.add_remote("mirror", "https://example.com/m.git"),
        )
        qtbot.waitUntil(
            lambda: not window._write_queue.is_busy, timeout=TIMEOUT
        )

        assert (
            "mirror",
            "https://example.com/m.git",
        ) in window._engine.list_remotes_with_urls()
