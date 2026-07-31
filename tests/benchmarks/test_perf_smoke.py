"""성능 회귀의 pytest 초소 — bench_load의 축소판 (backlog 구 §3.9).

`bench_load.py`는 10만 커밋 수동 스크립트다 — §8이 "성능 회귀 방지"라고
불렀지만 pytest가 수집하지 않아 아무도 자동으로 돌리지 않았다. 여기서는
같은 픽스처 생성기(팩된 저장소 — ADR-11)로 **작은 규모를 상시 검증**한다:
스크롤 없는 회귀 감지가 목적이라 예산은 G2·G4를 규모에 비례해 넉넉히
잡는다. 정밀 수치는 여전히 수동 벤치의 몫이다.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.benchmarks.fixtures import build_repository, count_loose_objects

COMMITS = 2_000

TIMEOUT = 60_000


@pytest.fixture(scope="module")
def packed_repo(tmp_path_factory) -> Path:  # noqa: ANN001
    root = tmp_path_factory.mktemp("bench") / f"bench-{COMMITS}"
    build_repository(root, COMMITS)
    return root


class TestFixtureStaysRealistic:
    def test_repository_is_packed(self, packed_repo: Path) -> None:
        """ADR-11 — 느슨한 오브젝트 저장소는 순회가 40배 느려 현실을
        대표하지 못한다. 픽스처가 무너지면 아래 시간 검증도 무의미하다."""
        assert count_loose_objects(packed_repo) == 0


class TestBudgetsHold:
    def test_open_repository_stays_within_g4(self, packed_repo: Path) -> None:
        started = time.perf_counter()
        engine = LocalGitEngine.open(str(packed_repo))
        engine.info(include_refs=False)
        elapsed_ms = (time.perf_counter() - started) * 1000

        assert elapsed_ms < 50, f"저장소 열기 {elapsed_ms:.0f}ms — G4 예산 초과"

    def test_full_load_scales_to_the_manual_benchmark(
        self, qtbot, packed_repo: Path  # noqa: ANN001
    ) -> None:
        """2천 커밋 전체 로딩이 수 초 안에 끝난다.

        수동 벤치(10만 커밋 ≈ 1.4~2.0s)의 50분의 1 규모이므로 1초는
        회귀에만 반응하는 느슨한 상한이다 — CI 머신의 소음에는 안 걸리고,
        로딩 경로가 O(n²)로 미끄러지면 걸린다.
        """
        from gitclient.ui.main_window import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        window._report = lambda _e: None
        started = time.perf_counter()
        window.open_repository(str(packed_repo))
        qtbot.waitUntil(
            lambda: not window._loading
            and window._commit_model.rowCount() >= COMMITS,
            timeout=TIMEOUT,
        )
        elapsed = time.perf_counter() - started

        assert elapsed < 1.0, f"전체 로딩 {elapsed:.2f}s — 회귀 의심"
