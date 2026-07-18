"""계측 저장소 테스트.

핵심 계약: 계측 실패가 본 작업을 실패시키지 않는다. 그리고 "측정하지 못함"과
"0바이트"를 구분한다 — 섞으면 누적 전송량이 조용히 과소 집계된다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gitclient.domain.instrumentation import OperationKind, TransferStats
from gitclient.infrastructure.stats_store import StatsStore


def stats(**overrides) -> TransferStats:  # noqa: ANN003
    base = {
        "kind": OperationKind.FETCH,
        "remote": "origin",
        "duration_ms": 120,
        "received_bytes": 1024,
        "received_objects": 10,
        "total_objects": 10,
    }
    base.update(overrides)
    return TransferStats(**base)


@pytest.fixture
def store(tmp_path: Path) -> StatsStore:
    return StatsStore(tmp_path / "stats.sqlite3")


class TestRecording:
    def test_records_and_summarizes(self, store: StatsStore) -> None:
        store.record("repo-a", stats())
        store.record("repo-a", stats(received_bytes=2048, received_objects=5))

        summary = store.summarize("repo-a")
        assert summary.operations == 2
        assert summary.total_bytes == 3072
        assert summary.total_objects == 15

    def test_repos_are_isolated(self, store: StatsStore) -> None:
        store.record("repo-a", stats())
        store.record("repo-b", stats(received_bytes=9999))

        assert store.summarize("repo-a").total_bytes == 1024
        assert store.summarize("repo-b").total_bytes == 9999

    def test_unknown_repo_is_empty(self, store: StatsStore) -> None:
        summary = store.summarize("never-seen")
        assert summary.operations == 0
        assert summary.total_bytes == 0

    def test_recent_returns_newest_first(self, store: StatsStore) -> None:
        store.record("repo-a", stats(duration_ms=1))
        store.record("repo-a", stats(duration_ms=2))
        rows = store.recent("repo-a")
        assert [row["duration_ms"] for row in rows] == [2, 1]


class TestMeasurementGaps:
    """측정 실패와 0바이트는 다르다."""

    def test_unmeasured_operations_are_counted_separately(
        self, store: StatsStore
    ) -> None:
        store.record("repo-a", stats(received_bytes=500))
        store.record("repo-a", stats(received_bytes=None, received_objects=None))

        summary = store.summarize("repo-a")
        assert summary.operations == 2
        assert summary.measured_operations == 1
        assert summary.fully_measured is False
        assert summary.total_bytes == 500  # 측정된 것만 합산

    def test_all_measured_is_flagged(self, store: StatsStore) -> None:
        store.record("repo-a", stats())
        assert store.summarize("repo-a").fully_measured is True


class TestTimeWindow:
    def test_since_filters_older_rows(self, store: StatsStore) -> None:
        store.record("repo-a", stats())
        future = datetime.now(timezone.utc) + timedelta(days=1)
        assert store.summarize("repo-a", since=future).operations == 0

        past = datetime.now(timezone.utc) - timedelta(days=1)
        assert store.summarize("repo-a", since=past).operations == 1


class TestRolling:
    def test_old_rows_are_trimmed(self, tmp_path: Path) -> None:
        store = StatsStore(tmp_path / "s.sqlite3", keep_per_repo=3)
        for index in range(10):
            store.record("repo-a", stats(duration_ms=index))

        rows = store.recent("repo-a", limit=100)
        assert len(rows) == 3
        assert [row["duration_ms"] for row in rows] == [9, 8, 7]

    def test_trim_is_per_repo(self, tmp_path: Path) -> None:
        store = StatsStore(tmp_path / "s.sqlite3", keep_per_repo=2)
        for index in range(5):
            store.record("repo-a", stats(duration_ms=index))
        store.record("repo-b", stats(duration_ms=99))

        assert len(store.recent("repo-a", limit=100)) == 2
        assert len(store.recent("repo-b", limit=100)) == 1


class TestResilience:
    """계측 실패가 본 작업을 실패시키면 안 된다."""

    def test_unwritable_path_does_not_raise(self, tmp_path: Path) -> None:
        # 파일이 있어야 할 자리에 디렉터리를 두어 쓰기를 실패시킨다
        blocked = tmp_path / "blocked.sqlite3"
        blocked.mkdir()

        store = StatsStore(blocked)  # 생성자도 조용히 넘어가야 한다
        store.record("repo-a", stats())  # 예외 없이

        assert store.summarize("repo-a").operations == 0

    def test_reads_survive_missing_file(self, tmp_path: Path) -> None:
        store = StatsStore(tmp_path / "nested" / "deep" / "s.sqlite3")
        assert store.summarize("repo-a").operations == 0
        assert store.recent("repo-a") == []


class TestPersistence:
    def test_data_survives_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "s.sqlite3"
        StatsStore(path).record("repo-a", stats(received_bytes=777))

        reopened = StatsStore(path)
        assert reopened.summarize("repo-a").total_bytes == 777

    def test_failed_operations_are_recorded(self, store: StatsStore) -> None:
        store.record("repo-a", stats(succeeded=False, received_bytes=None))
        rows = store.recent("repo-a")
        assert rows[0]["succeeded"] == 0
