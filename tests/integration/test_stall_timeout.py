"""진행 없음(stall) 기준 타임아웃과 비동기 취소.

**고정 벽시계 상한은 이 앱의 목적 함수를 정면으로 거슬렀다.** 느린 회선에서
큰 저장소를 받으면 300초를 넘기는 것이 예외가 아니라 기본에 가까운데, 넘기는
순간 그때까지 실어 나른 바이트를 통째로 버리고 재시도는 0바이트부터 다시
시작했다(git은 재개가 없다). 회선이 느릴수록 확률이 올라가니 §1.3이 지목한
대상 사용자에게 가장 자주 터지는 결함이었다.

죽여야 할 것은 "오래 걸리는 전송"이 아니라 **멈춘 전송**이다.

여기서 검증하는 것:
  1. 오래 걸려도 **진행 중이면** 살아남는가
  2. 진행이 멈추면 끊는가
  3. 끊더라도 이미 받은 것을 보존하는가
  4. 취소가 UI 스레드를 막지 않는가 (G4: 50ms)
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from gitclient.domain.errors import EngineError
from gitclient.infrastructure.remote_engine import (
    DRAIN_TIMEOUT_S,
    STALL_TIMEOUT_S,
    RemoteEngine,
    _PipePump,
)
from tests.integration.remote_harness import RemoteFixture, git


def spawn(script: str) -> subprocess.Popen[bytes]:
    """지정한 방식으로 출력을 내보내는 가짜 자식 프로세스.

    진짜 git으로 stall을 만들려면 응답하지 않는 서버가 필요한데, 그것은
    이 테스트가 검증하려는 대상(대기 로직)이 아니라 환경이다.

    **바이너리 파이프로 띄운다 — 실제 실행 경로와 같아야 한다.** 텍스트
    모드로 띄우면 read(n)이 n글자까지 막혀 스트리밍 자체가 성립하지 않는데,
    그 차이를 테스트가 덮으면 검증하는 대상이 실물과 달라진다.
    """
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        # 실행 경로와 같은 프로세스 그룹 배치. 없으면 POSIX에서
        # os.killpg(os.getpgid(child))가 pytest 자신의 그룹을 가리킨다.
        start_new_session=(os.name != "nt"),
    )


class TestPumpTracksActivity:
    """대기 판정의 근거가 되는 '마지막 수신 시각'이 실제로 움직이는가."""

    def test_idle_grows_while_silent(self) -> None:
        proc = spawn("import time; time.sleep(2)")
        try:
            pump = _PipePump(proc)
            time.sleep(0.6)
            assert pump.idle_seconds >= 0.5
        finally:
            proc.kill()
            proc.wait()

    def test_idle_resets_on_output(self) -> None:
        """진행률이 오면 침묵 시계가 0으로 돌아가야 한다."""
        proc = spawn(
            """
            import sys, time
            for _ in range(6):
                sys.stderr.write("Receiving objects:  50%\\r")
                sys.stderr.flush()
                time.sleep(0.2)
            """
        )
        try:
            pump = _PipePump(proc)
            time.sleep(0.9)
            assert pump.idle_seconds < 0.5, "출력이 오는데 침묵으로 봤다"
        finally:
            proc.kill()
            proc.wait()

    def test_carriage_return_output_is_seen(self) -> None:
        """git 진행률은 \\n이 아니라 \\r로 끊긴다 — readline이면 못 본다."""
        proc = spawn(
            """
            import sys, time
            sys.stderr.write("Receiving objects:  10%\\r")
            sys.stderr.flush()
            time.sleep(1.5)
            """
        )
        try:
            pump = _PipePump(proc)
            time.sleep(0.5)
            assert "Receiving objects" in pump.collected()[1]
        finally:
            proc.kill()
            proc.wait()

    def test_output_is_collected_in_order(self) -> None:
        proc = spawn(
            """
            import sys
            sys.stderr.write("first\\rsecond\\rthird\\r")
            sys.stdout.write("out")
            """
        )
        pump = _PipePump(proc)
        proc.wait()
        pump.join(5)
        stdout, stderr = pump.collected()
        assert stderr == "first\rsecond\rthird\r"
        assert stdout == "out"


class TestSlowButHealthyTransferSurvives:
    """가장 중요한 성질 — 느린 것은 중단 사유가 아니다."""

    def test_long_transfer_with_progress_is_not_killed(
        self, tmp_path: Path
    ) -> None:
        """stall 기준보다 오래 걸려도 진행 중이면 살아남는다.

        벽시계 기준이었다면 여기서 죽었다.
        """
        engine = RemoteEngine(str(tmp_path))
        proc = spawn(
            """
            import sys, time
            for _ in range(12):
                sys.stderr.write("Receiving objects:  50%\\r")
                sys.stderr.flush()
                time.sleep(0.1)
            """
        )
        try:
            # 침묵 허용치(0.5초)보다 전체 소요(1.2초)가 훨씬 길다
            stdout, stderr = engine._wait_for(proc, stall_timeout_s=1)
            assert "Receiving objects" in stderr
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_stall_timeout_is_generous_by_default(self) -> None:
        """기본값이 git의 진행률 주기(실측 0.3초)보다 충분히 커야 한다."""
        assert STALL_TIMEOUT_S >= 60


class TestStalledTransferIsCut:
    def test_silent_process_is_killed(self, tmp_path: Path) -> None:
        engine = RemoteEngine(str(tmp_path))
        proc = spawn("import time; time.sleep(60)")
        try:
            start = time.monotonic()
            with pytest.raises(EngineError) as excinfo:
                engine._wait_for(proc, stall_timeout_s=1)
            elapsed = time.monotonic() - start

            assert elapsed < 10, "침묵 기준을 한참 넘겨 기다렸다"
            assert excinfo.value.action is not None
            assert proc.poll() is not None, "프로세스를 죽이지 않았다"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_message_says_no_response_not_too_slow(self, tmp_path: Path) -> None:
        """문구가 원인을 바꿔 말하면 사용자가 엉뚱한 조치를 고른다.

        느린 회선은 기다리면 되지만 멈춘 연결은 기다려도 안 된다.
        """
        engine = RemoteEngine(str(tmp_path))
        proc = spawn("import time; time.sleep(60)")
        try:
            with pytest.raises(EngineError) as excinfo:
                engine._wait_for(proc, stall_timeout_s=1)

            error = excinfo.value
            assert "응답이 없어" in error.message
            # 재시도 비용을 **정확히** 말해야 한다. 예전 문구는 "받은 것은
            # 재사용된다"고 했는데 실측으로 거짓이었다 — 완결되지 않은 팩은
            # 임시 파일로만 존재하고 버려진다. 거짓 안심은 잘못된 판단을 부른다.
            assert "처음부터" in (error.action or "")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_partial_output_survives_the_kill(self, tmp_path: Path) -> None:
        """끊더라도 그때까지의 진행률은 남아야 계측이 바이트를 기록한다."""
        engine = RemoteEngine(str(tmp_path))
        proc = spawn(
            """
            import sys, time
            sys.stderr.write("Receiving objects: 100% (5/5), 1.00 MiB\\r")
            sys.stderr.flush()
            time.sleep(60)
            """
        )
        try:
            with pytest.raises(EngineError) as excinfo:
                engine._wait_for(proc, stall_timeout_s=1)

            assert "Receiving objects" in getattr(
                excinfo.value, "git_stderr", ""
            ), "죽이면서 이미 받은 진행률을 버렸다"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class TestFetchedBytesArePreserved:
    def test_objects_from_an_interrupted_fetch_remain(
        self, tmp_path: Path
    ) -> None:
        """fetch가 끊겨도 받은 객체는 저장소에 남아 다음에 재사용된다.

        오류 문구가 사용자에게 약속하는 내용이다 — 지켜지는지 확인한다.
        """
        fixture = RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=8)
        fixture.add_and_publish(1)
        before = _object_count(fixture.work)

        RemoteEngine(str(fixture.work)).fetch()

        assert _object_count(fixture.work) > before


def _object_count(repo: Path) -> int:
    out = git("count-objects", "-v", cwd=repo).stdout
    total = 0
    for line in out.splitlines():
        if line.startswith(("count:", "in-pack:")):
            total += int(line.split(":")[1])
    return total


class TestAbortDoesNotBlockTheCaller:
    """§3.3 G4 — 단일 블로킹 구간 50ms. 실측 taskkill은 109ms였다."""

    def test_abort_returns_immediately(self, tmp_path: Path) -> None:
        engine = RemoteEngine(str(tmp_path))
        proc = spawn("import time; time.sleep(30)")
        engine._proc = proc
        try:
            start = time.perf_counter()
            engine.abort()
            elapsed_ms = (time.perf_counter() - start) * 1000

            assert elapsed_ms < 50, (
                f"취소가 호출자를 {elapsed_ms:.0f}ms 막았다 (G4 예산 50ms)"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_abort_still_kills_the_process(self, tmp_path: Path) -> None:
        """빠르다고 안 죽이면 의미가 없다 — 비동기일 뿐 취소는 실제로 된다."""
        engine = RemoteEngine(str(tmp_path))
        proc = spawn("import time; time.sleep(30)")
        engine._proc = proc
        try:
            engine.abort()

            deadline = time.monotonic() + 10
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert proc.poll() is not None, "취소했는데 프로세스가 살아 있다"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_abort_flag_is_set_synchronously(self, tmp_path: Path) -> None:
        """종료는 비동기지만 '취소됨'이라는 사실은 즉시 보여야 한다."""
        engine = RemoteEngine(str(tmp_path))
        proc = spawn("import time; time.sleep(30)")
        engine._proc = proc
        try:
            engine.abort()
            assert engine._aborted is True
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_concurrent_aborts_are_harmless(self, tmp_path: Path) -> None:
        engine = RemoteEngine(str(tmp_path))
        proc = spawn("import time; time.sleep(30)")
        engine._proc = proc
        try:
            threads = [threading.Thread(target=engine.abort) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)

            deadline = time.monotonic() + 10
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert proc.poll() is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class TestCloseDoesNotWaitForTheAsyncKill:
    """비동기 취소의 대가가 닫기 경로로 돌아오지 않는지 확인한다.

    종료를 별도 스레드로 넘긴 직후, `closeEvent`의 `waitForDone(2000)`이
    그 스레드가 끝나기를 기다리느라 실측 197ms를 먹었다 — UI 스레드에서
    뺀 이득이 그대로 닫기 경로로 돌아온 셈이었다.

    **실제 fetch로는 재현되지 않는다.** 로컬 file:// 원격은 너무 빨리 끝나
    스레드풀이 이미 비어 있다. 그래서 "느린 워커가 풀에 남아 있을 때"라는
    조건을 직접 만든다 — 검증하려는 계약이 정확히 그것이다.
    """

    def test_close_does_not_block_on_a_lingering_worker(
        self, qtbot, tmp_path: Path  # noqa: ANN001
    ) -> None:
        from PySide6.QtCore import QRunnable
        from gitclient.ui.main_window import MainWindow

        fixture = RemoteFixture(tmp_path / "src").build(commits=2, payload_kb=4)
        window = MainWindow()
        qtbot.addWidget(window)
        window._report = lambda _e: None
        window.open_repository(str(fixture.work))
        qtbot.waitUntil(lambda: not window._loading, timeout=60_000)

        released = threading.Event()

        class Lingering(QRunnable):
            def run(self) -> None:  # noqa: D102
                released.wait(10)

        window._pool.start(Lingering())
        time.sleep(0.1)  # 워커가 풀 슬롯을 실제로 잡을 시간

        try:
            start = time.perf_counter()
            window.close()
            elapsed_ms = (time.perf_counter() - start) * 1000

            assert elapsed_ms < 150, (
                f"창 닫기가 {elapsed_ms:.0f}ms 막았다 — 남은 워커를 기다리고 있다"
            )
        finally:
            released.set()
            window._pool.waitForDone(10_000)


def _slow_git(tmp_path: Path) -> str:
    """오래 매달리는 가짜 git. 실제 `_run` 경로를 태우기 위한 것이다."""
    body = f'"{sys.executable}" -c "import time; time.sleep(60)"'
    if os.name == "nt":
        script = tmp_path / "slow_git.bat"
        script.write_text(f"@echo off\n{body}\n", encoding="utf-8")
    else:
        script = tmp_path / "slow_git.sh"
        script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
    return str(script)


class TestAbortReachesTheRealCommand:
    """`_proc` 인계를 검증한다.

    기존 취소 테스트는 전부 `engine._proc`을 손으로 넣는다. 그러면 `_run`이
    실제로 `self._proc = proc`을 하는지는 아무도 확인하지 않아, 그 한 줄을
    지워도 전부 통과한다 — 취소가 조용히 무동작이 된다.
    """

    def test_abort_stops_a_command_started_through_run(
        self, tmp_path: Path
    ) -> None:
        engine = RemoteEngine(str(tmp_path), git_binary=_slow_git(tmp_path))
        result: dict = {}

        def run_fetch() -> None:
            try:
                engine.fetch()
            except Exception as exc:  # noqa: BLE001 - 무엇이 오든 기록한다
                result["error"] = exc

        worker = threading.Thread(target=run_fetch, daemon=True)
        worker.start()

        deadline = time.monotonic() + 10
        while engine._proc is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert engine._proc is not None, "_run이 _proc에 프로세스를 싣지 않았다"

        engine.abort()
        worker.join(20)

        assert not worker.is_alive(), "취소했는데 명령이 끝나지 않았다"
        assert "error" in result

    def test_abort_before_start_is_honoured(self, tmp_path: Path) -> None:
        """Popen보다 취소가 먼저 와도 명령이 흘러나가면 안 된다."""
        engine = RemoteEngine(str(tmp_path), git_binary=_slow_git(tmp_path))
        engine.abort()

        with pytest.raises(EngineError):
            engine.fetch()


class TestThresholdIsNotSilentlyCapped:
    def test_stall_is_not_triggered_before_the_configured_threshold(
        self, tmp_path: Path
    ) -> None:
        """기준보다 **일찍** 끊으면 안 된다.

        상한만 검사하면 `min(stall_timeout_s, 1)` 같은 은근한 상한 고정이
        통째로 통과한다. 그러면 STALL_TIMEOUT_S=120이 조용히 몇 초로 줄어,
        서버가 모노레포를 열거하는 동안 끊기고 받은 바이트를 전부 버린다 —
        이 증분이 없애려던 실패 그 자체다. 위아래를 함께 고정한다.
        """
        engine = RemoteEngine(str(tmp_path))
        proc = spawn("import time; time.sleep(60)")
        try:
            start = time.monotonic()
            with pytest.raises(EngineError):
                engine._wait_for(proc, stall_timeout_s=3)
            elapsed = time.monotonic() - start

            assert elapsed >= 2.5, (
                f"기준 3초인데 {elapsed:.1f}초에 끊었다 — 상한이 고정돼 있다"
            )
            assert elapsed < 15, f"기준을 한참 넘겨 {elapsed:.1f}초 기다렸다"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


@pytest.mark.skipif(os.name != "nt", reason="가짜 git 스크립트가 Windows 전용")
class TestPipesDoNotDeadlockTheWorker:
    """`DRAIN_TIMEOUT_S`가 **진짜 상한**인지 확인한다.

    `Popen.__exit__`(`with proc:`)는 파이프를 닫으며 그 스트림을 읽는 스레드가
    쥔 버퍼 락을 **timeout 없이** 기다린다. git이 정상 종료해도 자손(ssh
    다중화기, credential helper, 트리 종료가 놓친 손자)이 상속받은 파이프를
    붙잡고 있으면 EOF가 오지 않아, 파이프를 닫는 행위 자체가 자손의 수명만큼
    워커를 정지시킨다.

    실측: git이 1초에 rc=0으로 끝났는데 fetch()가 45초(=손자 수명) 뒤에야
    반환했고, 그동안 finally가 돌지 않아 _proc과 임시 디렉터리가 남았다.
    §4.6.2가 `communicate()`에 대해 고쳤다고 적은 결함의 다른 얼굴이다.
    """

    def _fake_git_with_lingering_grandchild(self, tmp_path: Path) -> str:
        grandchild = tmp_path / "grandchild.py"
        grandchild.write_text("import time; time.sleep(45)", encoding="utf-8")
        script = tmp_path / "fake_git.bat"
        script.write_text(
            "@echo off\r\n"
            f'start /B "" "{sys.executable}" "{grandchild}"\r\n'
            "echo Receiving objects: 100%% (10/10), 2.00 KiB\r\n"
            "exit /b 0\r\n",
            encoding="utf-8",
        )
        return str(script)

    def test_lingering_grandchild_does_not_hang_the_worker(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        subprocess.run(
            ["git", "init", "--quiet", str(repo)], capture_output=True, check=True
        )
        engine = RemoteEngine(
            str(repo), git_binary=self._fake_git_with_lingering_grandchild(tmp_path)
        )
        outcome: dict = {}

        def run() -> None:
            started = time.monotonic()
            try:
                engine.fetch()
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = exc
            outcome["elapsed"] = time.monotonic() - started

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(30)

        assert not worker.is_alive(), (
            "손자가 파이프를 쥔 채 남아 워커가 정지했다 — "
            "DRAIN_TIMEOUT_S가 상한 노릇을 못 한다"
        )
        assert outcome["elapsed"] < DRAIN_TIMEOUT_S + 10
        assert engine._proc is None, "finally가 돌지 않아 _proc이 남았다"
