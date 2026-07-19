"""배경 미리 가져오기 (ADR-7 정정).

**이 기능은 전제 정정으로 되살아났다.** 초안은 "사용자가 요청하지 않은
트래픽을 동의 없이 소비한다"는 이유로 옵트인이었는데, 그 반론은 요금제가
있을 때만 성립한다. 요금이 없으면 사용자가 기다리지 않는 시간에 미리 받는
것은 총 전송량이 늘어도 이득이다 — 대기를 임계 경로 밖으로 빼는 것이 곧
목적 함수다 (ADR-56, §1.4 원칙 4).

실측 근거: prefetch 뒤의 진짜 fetch는 **받을 객체가 0개다.** 전송이 통째로
사라지고 협상 왕복만 남는다. 느린 회선에서 대기의 대부분이 전송이므로
그 전부가 배경으로 옮겨간다.

여기서 검증하는 것:
  1. 사용자가 보는 것을 바꾸지 않는가 (배경 작업의 첫 번째 조건)
  2. 다음 fetch가 실제로 전송을 아끼는가 (기능의 존재 근거)
  3. 사용자 작업에 양보하는가 — 배경 때문에 기다리면 안 된다
  4. 조용한가 — 실패해도 사용자를 방해하지 않는다
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.instrumentation import OperationKind
from gitclient.infrastructure.remote_engine import RemoteEngine
from tests.integration.remote_harness import RemoteFixture, git

TIMEOUT = 60_000


def refs_under(repo: Path, prefix: str) -> list[str]:
    out = git("for-each-ref", "--format=%(refname)", prefix, cwd=repo).stdout
    return sorted(line for line in out.splitlines() if line.strip())


class TestPrefetchLeavesTheUserViewAlone:
    """배경 작업이 화면을 조용히 바꾸면 그 자체가 방해다."""

    def test_remote_tracking_refs_do_not_move(self, tmp_path: Path) -> None:
        fixture = RemoteFixture(tmp_path / "src").build(commits=4, payload_kb=32)
        fixture.add_and_publish(3)
        before = {
            ref: git("rev-parse", ref, cwd=fixture.work).stdout.strip()
            for ref in refs_under(fixture.work, "refs/remotes/")
        }

        RemoteEngine(str(fixture.work)).prefetch()

        after = {
            ref: git("rev-parse", ref, cwd=fixture.work).stdout.strip()
            for ref in refs_under(fixture.work, "refs/remotes/")
        }
        assert after == before, "배경 작업이 사용자가 보는 참조를 움직였다"

    def test_prefetch_writes_to_its_own_namespace(self, tmp_path: Path) -> None:
        fixture = RemoteFixture(tmp_path / "src").build(commits=4, payload_kb=32)
        fixture.add_and_publish(2)

        RemoteEngine(str(fixture.work)).prefetch()

        assert refs_under(fixture.work, "refs/prefetch/"), (
            "받아둔 것을 붙잡아 둘 참조가 없으면 gc가 회수해 의미가 없다"
        )

    def test_head_does_not_move(self, tmp_path: Path) -> None:
        fixture = RemoteFixture(tmp_path / "src").build(commits=4, payload_kb=32)
        fixture.add_and_publish(2)
        before = git("rev-parse", "HEAD", cwd=fixture.work).stdout.strip()

        RemoteEngine(str(fixture.work)).prefetch()

        assert git("rev-parse", "HEAD", cwd=fixture.work).stdout.strip() == before


class TestPrefetchRemovesTheTransfer:
    """기능의 존재 근거 — 아끼지 못하면 회선만 쓴 셈이다."""

    def test_following_fetch_receives_nothing(self, tmp_path: Path) -> None:
        fixture = RemoteFixture(tmp_path / "src").build(commits=5, payload_kb=64)
        fixture.add_and_publish(4)
        engine = RemoteEngine(str(fixture.work))

        engine.prefetch()
        stats = engine.fetch()

        assert not stats.received_bytes, (
            f"미리 받아뒀는데 다시 {stats.received_bytes}바이트를 받았다"
        )

    def test_without_prefetch_the_fetch_does_transfer(
        self, tmp_path: Path
    ) -> None:
        """대조군 — 절감이 prefetch 덕분임을 보인다."""
        fixture = RemoteFixture(tmp_path / "src").build(commits=5, payload_kb=64)
        fixture.add_and_publish(4)

        stats = RemoteEngine(str(fixture.work)).fetch()

        assert stats.received_objects, "대조군이 아무것도 받지 않았다 — 전제가 깨졌다"

    def test_fetch_after_prefetch_still_updates_the_view(
        self, tmp_path: Path
    ) -> None:
        """전송은 없어도 참조는 갱신돼야 한다 — 아니면 아낀 의미가 없다."""
        fixture = RemoteFixture(tmp_path / "src").build(commits=4, payload_kb=32)
        fixture.add_and_publish(3)
        engine = RemoteEngine(str(fixture.work))
        engine.prefetch()

        engine.fetch()

        assert git(
            "rev-parse", "refs/remotes/origin/main", cwd=fixture.work
        ).stdout.strip() == fixture.origin_head()


class TestPrefetchIsRecordedSeparately:
    def test_prefetch_is_not_counted_as_user_wait(self, tmp_path: Path) -> None:
        """사용자가 기다린 시간과 배경에서 치른 시간을 섞으면 안 된다.

        대기 시간이 목적 함수인데 둘을 합치면 가장 중요한 수치가 흐려진다.
        """
        fixture = RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=16)
        fixture.add_and_publish(2)

        stats = RemoteEngine(str(fixture.work)).prefetch()

        assert stats.kind is OperationKind.PREFETCH


class TestPrefetchYieldsToTheUser:
    @pytest.fixture
    def window(self, qtbot, tmp_path: Path):  # noqa: ANN001, ANN201
        from gitclient.ui.main_window import MainWindow

        fixture = RemoteFixture(tmp_path / "src").build(commits=4, payload_kb=32)
        w = MainWindow()
        qtbot.addWidget(w)
        w._report = lambda _e: None
        w.open_repository(str(fixture.work))
        qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
        w.fixture = fixture
        return w

    def test_user_fetch_cancels_a_running_prefetch(self, window) -> None:  # noqa: ANN001
        """**배경 작업 때문에 사용자가 기다리는 일은 없어야 한다.**"""
        window.fixture.add_and_publish(2)
        window._maybe_prefetch()
        assert window._prefetch_worker is not None, "전제가 깨졌다"

        window._on_fetch()

        assert window._prefetch_worker is None, "사용자 작업이 배경에 밀렸다"

    def test_prefetch_skips_while_a_user_operation_runs(self, window) -> None:  # noqa: ANN001
        """회선과 저장소를 두고 다투지 않는다 — 다음 주기에 다시 온다."""
        window.fixture.add_and_publish(1)
        window._on_fetch()

        window._maybe_prefetch()

        assert window._prefetch_worker is None

    def test_prefetch_skips_during_a_merge(self, window) -> None:  # noqa: ANN001
        window._merging = True

        window._maybe_prefetch()

        assert window._prefetch_worker is None

    def test_prefetch_does_not_touch_the_progress_bar(
        self, window, qtbot  # noqa: ANN001
    ) -> None:
        """배경 작업이 상태 표시를 가로채면 사용자가 자기 작업으로 오해한다."""
        window.fixture.add_and_publish(2)

        window._maybe_prefetch()
        qtbot.waitUntil(
            lambda: window._prefetch_worker is None, timeout=TIMEOUT
        )

        assert window._progress_bar.isHidden()

    def test_prefetch_failure_is_silent(self, window, qtbot) -> None:  # noqa: ANN001
        """사용자가 요청하지 않은 작업의 실패로 흐름을 끊지 않는다."""
        reported: list = []
        window._report = reported.append
        git("remote", "set-url", "origin", str(window.fixture.work / "nowhere"),
            cwd=window.fixture.work)

        window._maybe_prefetch()
        qtbot.waitUntil(
            lambda: window._prefetch_worker is None, timeout=TIMEOUT
        )

        assert reported == [], "배경 작업 실패로 사용자를 방해했다"

    def test_repository_switch_cancels_prefetch(
        self, window, tmp_path: Path  # noqa: ANN001
    ) -> None:
        window.fixture.add_and_publish(2)
        window._maybe_prefetch()
        other = RemoteFixture(tmp_path / "other").build(commits=2, payload_kb=8)

        window.open_repository(str(other.work))

        assert window._prefetch_worker is None


class TestPrefetchDoesNotTouchUserState:
    """사용자가 만든 상태를 배경 작업이 덮으면 안 된다."""

    def test_fetch_head_is_preserved(self, tmp_path: Path) -> None:
        """`git fetch <브랜치>`가 남긴 FETCH_HEAD를 지우면 안 된다.

        git 자신의 maintenance prefetch도 `--no-write-fetch-head`를 쓴다.
        """
        fixture = RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=8)
        git("fetch", "origin", "main", cwd=fixture.work)
        before = (fixture.work / ".git" / "FETCH_HEAD").read_text(encoding="utf-8")

        RemoteEngine(str(fixture.work)).prefetch()

        after = (fixture.work / ".git" / "FETCH_HEAD").read_text(encoding="utf-8")
        assert after == before, "배경 작업이 사용자의 FETCH_HEAD를 덮어썼다"

    def test_deleted_branches_are_pruned(self, tmp_path: Path) -> None:
        """치우는 사람이 없으면 refs/prefetch/*가 영원히 쌓인다."""
        fixture = RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=8)
        fixture.create_remote_branch("temporary")
        engine = RemoteEngine(str(fixture.work))
        engine.prefetch()
        assert any(
            "temporary" in ref for ref in refs_under(fixture.work, "refs/prefetch/")
        ), "전제가 깨졌다"

        fixture.delete_remote_branch("temporary")
        engine.prefetch()

        assert not any(
            "temporary" in ref for ref in refs_under(fixture.work, "refs/prefetch/")
        ), "원격에서 지운 브랜치가 배경 참조에 남았다"


class TestPrefetchStatsDoNotEvictUserStats:
    def test_background_rows_have_their_own_ring(self, tmp_path: Path) -> None:
        """배경 기록이 사용자 기록을 밀어내면 안 된다.

        하나의 링에 섞으면 5분마다 도는 배경 작업이 사용자가 실제로
        기다린 기록을 먼저 지운다 — 대기 시간이 목적 함수인데 그 근거가
        사라지는 셈이다.
        """
        from gitclient.domain.instrumentation import TransferStats
        from gitclient.infrastructure.stats_store import StatsStore

        def stats(kind: OperationKind) -> TransferStats:
            return TransferStats(kind=kind, remote="origin", duration_ms=1)

        store = StatsStore(tmp_path / "stats.db", keep_per_repo=3)
        for _ in range(3):
            store.record("repo", stats(OperationKind.FETCH))
        for _ in range(10):
            store.record("repo", stats(OperationKind.PREFETCH))

        summary = store.summarize("repo")

        assert summary.operations >= 6, (
            f"배경 기록이 사용자 기록을 밀어냈다 (남은 행 {summary.operations}개)"
        )


class TestPrefetchCanBeTurnedOff:
    """기본으로 켜는 기능에는 끌 수단이 반드시 있어야 한다.

    요금은 없지만 회선을 아껴 써야 하는 순간(테더링, 화상회의 중)이
    사용자에게는 있고, 우리는 그것을 알 수 없다.
    """

    @pytest.fixture
    def window(self, qtbot, tmp_path: Path):  # noqa: ANN001, ANN201
        from gitclient.ui.main_window import MainWindow

        fixture = RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=8)
        w = MainWindow()
        qtbot.addWidget(w)
        w._report = lambda _e: None
        w.open_repository(str(fixture.work))
        qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
        w.fixture = fixture
        return w

    def test_default_is_on(self, window) -> None:  # noqa: ANN001
        assert window._prefetch_enabled() is True

    def test_disabling_stops_the_schedule(self, window) -> None:  # noqa: ANN001
        window._set_prefetch_enabled(False)
        try:
            assert not window._prefetch_timer.isActive()

            window._maybe_prefetch()

            assert window._prefetch_worker is None, "껐는데 배경 작업이 돌았다"
        finally:
            window._set_prefetch_enabled(True)

    def test_menu_action_reflects_the_setting(self, window) -> None:  # noqa: ANN001
        assert window._prefetch_action.isCheckable()
        assert window._prefetch_action.isChecked() == window._prefetch_enabled()
