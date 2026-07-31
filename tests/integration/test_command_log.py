"""실행된 git 명령 로그 (FR-11·12, AC-06).

투명성 창구다 — 기록이 비거나, 비밀이 새거나, 표시가 스레드를 어기면
없는 것보다 나쁘다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.command_log import COMMAND_LOG, CommandLog
from gitclient.infrastructure.remote_engine import RemoteEngine
from tests.integration.remote_harness import RemoteFixture

TIMEOUT = 10_000


class TestCommandLogBuffer:
    def test_records_are_masked_and_ordered(self) -> None:
        log = CommandLog(limit=3)
        log.record(["git", "clone", "https://alice:tok3n@host/r.git"],
                   duration_ms=5, returncode=0)

        (entry,) = log.snapshot()
        joined = " ".join(entry.argv)
        assert "tok3n" not in joined, "URL 속 자격증명이 화면 기록에 남았다"
        assert "***@host" in joined
        assert entry.succeeded

    def test_ring_buffer_drops_the_oldest(self) -> None:
        log = CommandLog(limit=2)
        for index in range(3):
            log.record(["git", str(index)], duration_ms=1, returncode=0)

        kept = [r.argv[1] for r in log.snapshot()]
        assert kept == ["1", "2"]

    def test_listener_failure_does_not_break_recording(self) -> None:
        log = CommandLog(limit=2)
        log.subscribe(lambda _r: (_ for _ in ()).throw(RuntimeError("화면이 터졌다")))

        log.record(["git", "fetch"], duration_ms=1, returncode=0)

        assert len(log.snapshot()) == 1


class TestEnginesRecord:
    def test_a_real_fetch_lands_in_the_global_log(self, tmp_path: Path) -> None:
        """AC-06 — fetch 한 번 뒤 명령과 종료 코드가 남는다."""
        fixture = RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=8)
        fixture.add_and_publish(1)
        before = len(COMMAND_LOG.snapshot())

        RemoteEngine(str(fixture.work)).fetch()

        added = COMMAND_LOG.snapshot()[before:]
        fetches = [r for r in added if "fetch" in r.argv]
        assert fetches, "fetch가 기록되지 않았다"
        assert fetches[-1].returncode == 0

    def test_sequencer_commands_are_recorded_too(self, tmp_path: Path) -> None:
        from gitclient.infrastructure.local_engine import LocalGitEngine
        from tests.integration.remote_harness import AUTHOR_ENV, git

        root = tmp_path / "work"
        root.mkdir()
        git("init", "--quiet", "-b", "main", str(root))
        (root / "f.txt").write_text("base\n", encoding="utf-8")
        git("add", "-A", cwd=root)
        git(*AUTHOR_ENV, "commit", "--quiet", "-m", "base", cwd=root)
        sha = git("rev-parse", "HEAD", cwd=root).stdout.strip()
        git("checkout", "--quiet", "-b", "other", cwd=root)
        (root / "g.txt").write_text("more\n", encoding="utf-8")
        git("add", "-A", cwd=root)
        git(*AUTHOR_ENV, "commit", "--quiet", "-m", "more", cwd=root)
        git("checkout", "--quiet", "main", cwd=root)
        before = len(COMMAND_LOG.snapshot())

        LocalGitEngine.open(str(root)).cherry_pick(
            git("rev-parse", "other", cwd=root).stdout.strip()
        )

        added = COMMAND_LOG.snapshot()[before:]
        assert any("cherry-pick" in r.argv for r in added)
        assert sha  # 사용 흔적 — 픽스처 셋업이 최적화로 지워지지 않게


class TestDockPanel:
    def test_rows_appear_and_failures_are_marked(self, qtbot) -> None:  # noqa: ANN001
        from gitclient.ui.command_log_panel import CommandLogDock, _format
        from gitclient.domain.command_log import CommandRecord
        from datetime import datetime

        dock = CommandLogDock()
        qtbot.addWidget(dock)

        ok = CommandRecord(datetime.now(), ("git", "fetch"), 12, 0)
        bad = CommandRecord(datetime.now(), ("git", "push"), 40, 1)
        dead = CommandRecord(datetime.now(), ("git", "fetch"), 99, None)
        assert _format(ok).startswith("✓")
        assert _format(bad).startswith("✗")
        assert "rc=?" in _format(dead)

        dock._append(ok)
        assert "git fetch" in dock._view.toPlainText()
