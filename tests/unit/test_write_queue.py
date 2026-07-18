"""WriteQueue 직렬화 테스트 (§3.3 규칙 3)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pygit2
import pytest

from gitclient.application.write_queue import WriteQueue
from gitclient.domain.errors import EngineError

SIGNATURE = pygit2.Signature("t", "t@e.com", 1700000000, 540)


@pytest.fixture
def repo_path(tmp_path: Path) -> str:
    r = pygit2.init_repository(str(tmp_path / "q"), initial_head="main")
    (Path(r.workdir) / "f.txt").write_text("x\n", encoding="utf-8")
    r.index.add_all()
    r.index.write()
    r.create_commit("HEAD", SIGNATURE, SIGNATURE, "init", r.index.write_tree(), [])
    return str(r.workdir)


class TestSerialization:
    def test_jobs_run_one_at_a_time_in_fifo_order(
        self, repo_path: str, qtbot  # noqa: ANN001
    ) -> None:
        queue = WriteQueue(repo_path)
        events: list[str] = []
        lock = threading.Lock()
        concurrent = {"now": 0, "max": 0}

        def job(tag: str, delay: float):
            def work(_engine):
                with lock:
                    concurrent["now"] += 1
                    concurrent["max"] = max(concurrent["max"], concurrent["now"])
                events.append(f"start:{tag}")
                time.sleep(delay)
                events.append(f"end:{tag}")
                with lock:
                    concurrent["now"] -= 1
                return tag

            return work

        with qtbot.waitSignal(queue.idle, timeout=10_000):
            queue.submit("job-a", job("a", 0.05))
            queue.submit("job-b", job("b", 0.01))
            queue.submit("job-c", job("c", 0.01))

        assert concurrent["max"] == 1, "쓰기 두 개가 동시에 실행됐다 — 직렬화 위반"
        assert events == [
            "start:a", "end:a", "start:b", "end:b", "start:c", "end:c",
        ]

    def test_failure_does_not_block_queue(
        self, repo_path: str, qtbot  # noqa: ANN001
    ) -> None:
        queue = WriteQueue(repo_path)
        outcomes: list[tuple[str, str]] = []
        queue.job_succeeded.connect(
            lambda _jid, name, _r: outcomes.append(("ok", name))
        )
        queue.job_failed.connect(
            lambda _jid, name, _e: outcomes.append(("fail", name))
        )

        def boom(_engine):
            raise EngineError("고의 실패")

        def fine(_engine):
            return 1

        with qtbot.waitSignal(queue.idle, timeout=10_000):
            queue.submit("boom", boom)
            queue.submit("fine", fine)

        assert outcomes == [("fail", "boom"), ("ok", "fine")]

    def test_unexpected_exception_is_wrapped(
        self, repo_path: str, qtbot  # noqa: ANN001
    ) -> None:
        queue = WriteQueue(repo_path)
        errors: list[object] = []
        queue.job_failed.connect(lambda _jid, _n, e: errors.append(e))

        def crash(_engine):
            raise RuntimeError("raw")

        with qtbot.waitSignal(queue.idle, timeout=10_000):
            queue.submit("crash", crash)

        assert len(errors) == 1
        assert "RuntimeError" in (errors[0].detail or "")

    def test_jobs_receive_a_working_engine(
        self, repo_path: str, qtbot  # noqa: ANN001
    ) -> None:
        queue = WriteQueue(repo_path)
        results: list[object] = []
        queue.job_succeeded.connect(lambda _jid, _n, r: results.append(r))

        with qtbot.waitSignal(queue.idle, timeout=10_000):
            queue.submit(
                "status", lambda engine: engine.working_tree_status().is_clean
            )

        assert results == [True]

    def test_job_ids_are_unique_even_for_same_name(
        self, repo_path: str, qtbot  # noqa: ANN001
    ) -> None:
        """커밋 연타처럼 같은 이름이 연달아 와도 job_id로 구분돼야 한다."""
        queue = WriteQueue(repo_path)
        ids: list[int] = []
        queue.job_succeeded.connect(lambda jid, _n, _r: ids.append(jid))

        with qtbot.waitSignal(queue.idle, timeout=10_000):
            first = queue.submit("커밋", lambda _e: 1)
            second = queue.submit("커밋", lambda _e: 2)

        assert first != second
        assert ids == [first, second]

    def test_is_busy_reflects_pending_work(
        self, repo_path: str, qtbot  # noqa: ANN001
    ) -> None:
        queue = WriteQueue(repo_path)
        assert queue.is_busy is False

        with qtbot.waitSignal(queue.idle, timeout=10_000):
            queue.submit("slow", lambda _e: time.sleep(0.05))
            assert queue.is_busy is True

        assert queue.is_busy is False
