"""실시간 진행 표시 (Phase 3 후속).

목적 함수가 "사용자가 기다리는 시간"이 된 이상(ADR-56), **얼마나 더 기다려야
하는지 보여주는 것 자체가 그 함수를 직접 겨냥한 기능이다.** 느린 회선에서
몇 분 걸리는 작업에 아무 표시가 없으면 사용자는 앱이 멎었다고 판단한다.

지난 증분에서 진행률을 실시간으로 읽는 구조(`_PipePump`)를 만들어 놓고
화면에는 정적 문구만 띄우고 있었다 — 데이터는 있는데 배선이 없었다.

여기서 검증하는 것:
  1. 진행이 **끝난 뒤가 아니라 진행 중에** 온다 (핵심)
  2. 단계가 사용자의 말로 옮겨진다
  3. 취소·종료 후에 옛 진행률이 화면에 남지 않는다
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from gitclient.domain.instrumentation import ProgressSnapshot, TransferPhase
from gitclient.infrastructure.remote_engine import RemoteEngine
from tests.integration.remote_harness import RemoteFixture

TIMEOUT = 60_000


class TestEngineReportsWhileRunning:
    """가장 중요한 성질 — 끝난 뒤 한 번 몰아서 오면 진행 표시가 아니다."""

    def test_progress_arrives_before_the_command_finishes(
        self, tmp_path: Path
    ) -> None:
        script = textwrap.dedent(
            """
            import sys, time
            for pct in (10, 40, 70, 100):
                sys.stderr.write(
                    f"Receiving objects: {pct}% ({pct}/100), "
                    f"{pct / 10:.2f} MiB | 1.00 MiB/s\\r"
                )
                sys.stderr.flush()
                time.sleep(0.25)
            """
        )
        fake_git = _fake_git(tmp_path, script)
        seen: list[tuple[float, ProgressSnapshot]] = []
        started = time.monotonic()
        engine = RemoteEngine(
            str(tmp_path),
            git_binary=fake_git,
            on_progress=lambda s: seen.append((time.monotonic() - started, s)),
        )

        try:
            engine.fetch()
        except Exception:  # noqa: BLE001 - 가짜 git의 종료 코드는 관심 밖
            pass

        assert seen, "진행 보고가 한 번도 오지 않았다"
        elapsed_total = time.monotonic() - started
        first_at = seen[0][0]
        assert first_at < elapsed_total * 0.7, (
            f"첫 보고가 {first_at:.2f}초(전체 {elapsed_total:.2f}초)에야 왔다 — "
            "실시간이 아니라 종료 후 몰아치기다"
        )

    def test_percent_increases_over_time(self, tmp_path: Path) -> None:
        script = textwrap.dedent(
            """
            import sys, time
            for pct in (10, 50, 90):
                sys.stderr.write(f"Receiving objects: {pct}% ({pct}/100)\\r")
                sys.stderr.flush()
                time.sleep(0.3)
            """
        )
        seen: list[ProgressSnapshot] = []
        engine = RemoteEngine(
            str(tmp_path),
            git_binary=_fake_git(tmp_path, script),
            on_progress=seen.append,
        )

        try:
            engine.fetch()
        except Exception:  # noqa: BLE001
            pass

        percents = [s.percent for s in seen if s.percent is not None]
        assert percents == sorted(percents), f"비율이 뒤로 갔다: {percents}"
        assert len(set(percents)) > 1, "값이 한 번도 갱신되지 않았다"

    def test_phases_are_reported_in_order(self, tmp_path: Path) -> None:
        script = textwrap.dedent(
            """
            import sys, time
            for line in (
                "remote: Counting objects: 100% (16/16)",
                "Receiving objects: 50% (8/16), 1.00 MiB | 1.00 MiB/s",
                "Resolving deltas: 100% (7/7)",
            ):
                sys.stderr.write(line + "\\r")
                sys.stderr.flush()
                time.sleep(0.3)
            """
        )
        seen: list[ProgressSnapshot] = []
        engine = RemoteEngine(
            str(tmp_path),
            git_binary=_fake_git(tmp_path, script),
            on_progress=seen.append,
        )

        try:
            engine.fetch()
        except Exception:  # noqa: BLE001
            pass

        phases = []
        for snapshot in seen:
            if not phases or phases[-1] is not snapshot.phase:
                phases.append(snapshot.phase)
        assert phases == [
            TransferPhase.PREPARING,
            TransferPhase.RECEIVING,
            TransferPhase.APPLYING,
        ], phases

    def test_progress_failure_does_not_kill_the_transfer(
        self, tmp_path: Path
    ) -> None:
        """화면 갱신 실패가 전송을 중단시키면 안 된다."""
        fixture = RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=8)
        fixture.add_and_publish(1)

        def explode(_snapshot) -> None:  # noqa: ANN001
            raise RuntimeError("화면이 터졌다")

        stats = RemoteEngine(
            str(fixture.work), on_progress=explode
        ).fetch()

        assert stats is not None

    def test_real_fetch_reports_progress(self, tmp_path: Path) -> None:
        """가짜 git이 아니라 진짜 전송에서도 오는가."""
        fixture = RemoteFixture(tmp_path / "src").build(commits=6, payload_kb=200)
        fixture.add_and_publish(3)
        seen: list[ProgressSnapshot] = []

        RemoteEngine(str(fixture.work), on_progress=seen.append).fetch()

        assert seen, "실제 fetch에서 진행 보고가 오지 않았다"


class TestWorkerSignalsProgress:
    def test_worker_emits_progress(self, qtbot, tmp_path: Path) -> None:  # noqa: ANN001
        from gitclient.application.remote_workers import FetchWorker

        fixture = RemoteFixture(tmp_path / "src").build(commits=6, payload_kb=200)
        fixture.add_and_publish(3)
        worker = FetchWorker(str(fixture.work), "origin")
        seen: list[ProgressSnapshot] = []
        worker.signals.progressed.connect(seen.append)

        with qtbot.waitSignal(worker.signals.finished, timeout=TIMEOUT):
            worker.run()

        assert seen

    def test_cancelled_worker_stops_reporting(
        self, qtbot, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """취소한 사용자에게 진행률이 계속 올라가면 안 된다."""
        from gitclient.application.remote_workers import FetchWorker

        fixture = RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=8)
        worker = FetchWorker(str(fixture.work), "origin")
        seen: list[ProgressSnapshot] = []
        worker.signals.progressed.connect(seen.append)
        worker.cancel()

        worker.run()

        assert seen == []


class TestStatusBarShowsProgress:
    @pytest.fixture
    def window(self, qtbot, tmp_path: Path):  # noqa: ANN001, ANN201
        from gitclient.ui.main_window import MainWindow

        fixture = RemoteFixture(tmp_path / "src").build(commits=4, payload_kb=64)
        w = MainWindow()
        qtbot.addWidget(w)
        w._report = lambda _e: None
        w.open_repository(str(fixture.work))
        qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
        w.fixture = fixture
        return w

    def test_bar_is_hidden_before_any_operation(self, window) -> None:  # noqa: ANN001
        assert window._progress_bar.isHidden()

    def test_bar_appears_and_message_names_the_phase(self, window) -> None:  # noqa: ANN001
        snapshot = ProgressSnapshot(
            phase=TransferPhase.RECEIVING,
            percent=42,
            bytes_so_far=5 * 1024**2,
            bytes_per_s=1024**2,
        )
        window._fetch_worker = _StubWorker()  # 정체 가드를 통과시킬 현재 워커

        window._on_remote_progress(window._fetch_worker, snapshot)

        assert window._progress_bar.value() == 42
        message = window.statusBar().currentMessage()
        assert "받는 중" in message
        assert "42%" in message
        assert "5.0MiB" in message or "MiB" in message, message

    def test_unknown_total_uses_an_indeterminate_bar(self, window) -> None:  # noqa: ANN001
        """총계를 모르는 단계에서 0%로 굳어 있으면 멈춘 것처럼 보인다."""
        window._fetch_worker = _StubWorker()

        window._on_remote_progress(
            window._fetch_worker,
            ProgressSnapshot(phase=TransferPhase.PREPARING, current=13),
        )

        assert window._progress_bar.maximum() == 0, "불확정 막대가 아니다"

    def test_stale_worker_progress_is_ignored(self, window) -> None:  # noqa: ANN001
        """진행률은 여러 번 오므로 정체 가드가 더 중요하다."""
        window._fetch_worker = _StubWorker()
        window._on_remote_progress(
            window._fetch_worker,
            ProgressSnapshot(phase=TransferPhase.RECEIVING, percent=30),
        )

        window._on_remote_progress(
            _StubWorker(),  # 이전 작업의 늦은 신호
            ProgressSnapshot(phase=TransferPhase.RECEIVING, percent=90),
        )

        assert window._progress_bar.value() == 30, "옛 작업이 화면을 덮었다"

    def test_bar_is_cleared_when_the_operation_ends(
        self, window, qtbot  # noqa: ANN001
    ) -> None:
        window.fixture.add_and_publish(1)
        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)

        assert window._progress_bar.isHidden(), "끝났는데 막대가 남았다"


class _StubWorker:
    """현재 워커 자리를 채우는 대역.

    창을 닫을 때 워커의 시그널을 끊으므로 그 형태를 갖춰야 한다 — 실제
    워커와 다른 모양을 쓰면 테스트가 통과해도 종료 경로가 깨진다.
    """

    def __init__(self) -> None:
        from gitclient.application.remote_workers import RemoteSignals

        self.signals = RemoteSignals()

    def cancel(self) -> None:
        pass


def _fake_git(tmp_path: Path, script: str) -> str:
    """지정한 진행률을 흘려보내는 가짜 git.

    진짜 git으로는 "느린 전송"을 만들 수 없어 타이밍을 검증할 수 없다.
    """
    body = script.replace("\n", "\n")
    source = tmp_path / "fake_git_body.py"
    source.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        launcher = tmp_path / "fake_git.bat"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{source}"\r\n', encoding="utf-8"
        )
    else:
        launcher = tmp_path / "fake_git.sh"
        launcher.write_text(
            f'#!/bin/sh\n"{sys.executable}" "{source}"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)
    return str(launcher)


class TestPhaseAttribution:
    """**누가** 일하는지를 틀리면 "왜 느린가"의 답이 정반대가 된다.

    `remote:` 접두어가 유일한 판별 정보인데, fetch와 push에서 정반대로 붙는다.
    이 구분을 잃으면 사용자 CPU가 돌 때 서버를 지목하고, 서버가 몇 분 걸릴 때
    사용자 디스크를 지목한다.
    """

    def test_fetch_shapes_are_attributed_to_the_server(self) -> None:
        from gitclient.domain.instrumentation import parse_progress_snapshot

        snapshot = parse_progress_snapshot("remote: Counting objects:  62% (10/16)\r")

        assert snapshot.remote_side is True

    def test_push_local_work_is_not_blamed_on_the_server(self) -> None:
        """push의 Counting/Compressing은 **우리 CPU**다 — 접두어가 없다."""
        from gitclient.domain.instrumentation import parse_progress_snapshot

        snapshot = parse_progress_snapshot("Counting objects:  10% (213/2129)\r")

        assert snapshot.remote_side is False

    def test_push_server_side_resolution_is_not_blamed_on_our_disk(self) -> None:
        """push의 `remote: Resolving deltas`는 서버가 하는 일이다."""
        from gitclient.domain.instrumentation import parse_progress_snapshot

        snapshot = parse_progress_snapshot("remote: Resolving deltas:  33% (1/3)\r")

        assert snapshot.remote_side is True

    def test_label_names_the_actor(self) -> None:
        from gitclient.ui.main_window import _PHASE_LABELS

        assert _PHASE_LABELS[(TransferPhase.PREPARING, True)] != (
            _PHASE_LABELS[(TransferPhase.PREPARING, False)]
        ), "주체가 달라도 같은 문구면 구분한 의미가 없다"
        assert "원격" in _PHASE_LABELS[(TransferPhase.APPLYING, True)]
        assert "원격" not in _PHASE_LABELS[(TransferPhase.APPLYING, False)]


class TestCloneReportsProgress:
    """clone은 **이 앱에서 대기가 가장 긴 작업**이다 — 여기에 표시가 없으면
    기능이 가장 필요한 곳에서 통째로 빠진 것이다.

    `file://` URL을 써야 한다. 로컬 경로 clone은 git이 객체를 하드링크해
    폴링 루프를 한 번도 돌지 않으므로, 수정 전후가 똑같이 0회로 나와
    테스트가 결함을 잡지 못한다.
    """

    def test_clone_worker_emits_progress(self, qtbot, tmp_path: Path) -> None:  # noqa: ANN001
        from gitclient.application.remote_workers import CloneWorker

        source = RemoteFixture(tmp_path / "src").build(commits=10, payload_kb=700)
        url = "file:///" + str(source.origin).replace("\\", "/")
        destination = tmp_path / "cloned"
        worker = CloneWorker(url, str(destination))
        seen: list[ProgressSnapshot] = []
        worker.signals.progressed.connect(seen.append)

        with qtbot.waitSignal(worker.signals.finished, timeout=TIMEOUT):
            worker.run()

        assert seen, "복제가 진행률을 한 번도 내보내지 않았다"
        assert (destination / ".git").exists()


class TestPushWaitOnServerIsHonest:
    """push의 가장 긴 대기 — 다 보낸 뒤 서버가 훅을 도는 구간.

    그때 "보내는 중 100%"를 세워두면 다 됐는데 멈춘 것처럼 보인다.
    """

    def test_finished_upload_switches_to_indeterminate(self, qtbot, tmp_path):  # noqa: ANN001
        from gitclient.ui.main_window import MainWindow

        fixture = RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=8)
        window = MainWindow()
        qtbot.addWidget(window)
        window._report = lambda _e: None
        window.open_repository(str(fixture.work))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
        window._fetch_worker = _StubWorker()

        window._on_remote_progress(
            window._fetch_worker,
            ProgressSnapshot(
                phase=TransferPhase.SENDING, percent=100,
                bytes_so_far=8 * 1024**2, bytes_per_s=1024**2,
            ),
        )

        assert window._progress_bar.maximum() == 0, "100%로 굳어 멈춘 것처럼 보인다"
        assert "원격이 처리하는 중" in window.statusBar().currentMessage()


class TestRepositorySwitchClearsProgress:
    def test_bar_does_not_survive_a_repository_change(self, qtbot, tmp_path):  # noqa: ANN001
        """전송 중 저장소를 갈아타면 옛 막대가 남으면 안 된다.

        워커를 놓아주면 정체 가드가 이후 신호를 전부 걸러내므로, 막대를
        치울 주체가 사라진다.
        """
        from gitclient.ui.main_window import MainWindow

        first = RemoteFixture(tmp_path / "a").build(commits=3, payload_kb=8)
        second = RemoteFixture(tmp_path / "b").build(commits=2, payload_kb=8)
        window = MainWindow()
        qtbot.addWidget(window)
        window._report = lambda _e: None
        window.open_repository(str(first.work))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
        window._fetch_worker = _StubWorker()
        window._on_remote_progress(
            window._fetch_worker,
            ProgressSnapshot(phase=TransferPhase.RECEIVING, percent=42),
        )

        window.open_repository(str(second.work))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        assert window._progress_bar.isHidden(), "저장소를 갈아탔는데 옛 막대가 남았다"
