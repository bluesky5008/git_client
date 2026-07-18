"""벤치마크 fixture 자체를 검증한다.

fixture가 현실을 대표하지 못하면 그 위에서 나온 모든 측정이 무의미해진다.
실제로 그런 일이 한 번 있었다. (doc/design.md §4.1.1.3)
이 테스트는 그 재발을 막는다.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest

from tests.benchmarks.fixtures import (
    build_repository,
    count_loose_objects,
    count_packs,
    resolve_side_every,
)

COMMITS = 300


@pytest.fixture(scope="module")
def repo_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("bench") / "repo"
    return build_repository(path, COMMITS)


class TestFixtureIsRealistic:
    def test_has_no_loose_objects(self, repo_path: Path) -> None:
        # 이것이 이 파일에서 가장 중요한 단언이다.
        # 느슨한 오브젝트가 남으면 순회가 40배 느려져 측정이 왜곡된다.
        assert count_loose_objects(repo_path) == 0

    def test_objects_are_packed(self, repo_path: Path) -> None:
        assert count_packs(repo_path) >= 1

    def test_commit_graph_is_written(self, repo_path: Path) -> None:
        assert (repo_path / "objects" / "info" / "commit-graph").exists()


class TestFixtureTopology:
    def test_commit_count_is_at_least_requested(self, repo_path: Path) -> None:
        repo = pygit2.Repository(str(repo_path))
        count = sum(1 for _ in repo.walk(repo.head.target))
        assert count >= COMMITS

    def test_contains_merge_commits(self, repo_path: Path) -> None:
        # 선형 히스토리만으로는 레인 배치 비용이 측정되지 않는다.
        repo = pygit2.Repository(str(repo_path))
        merges = [c for c in repo.walk(repo.head.target) if len(c.parent_ids) > 1]
        assert merges, "머지 커밋이 없으면 그래프 렌더링 비용이 과소평가된다"

    def test_has_multiple_branches(self, repo_path: Path) -> None:
        repo = pygit2.Repository(str(repo_path))
        assert len(list(repo.branches.local)) >= 2

    def test_rebuild_is_idempotent(self, repo_path: Path) -> None:
        # 이미 존재하면 재사용해야 한다. 매번 다시 만들면 벤치마크가 느려진다.
        before = count_packs(repo_path)
        build_repository(repo_path, COMMITS)
        assert count_packs(repo_path) == before


class TestBranchInterval:
    """분기 간격은 커밋 수에 비례해야 한다.

    고정값이면 작은 fixture에 머지가 하나도 생기지 않아, 그래프 렌더링
    비용이 측정에서 통째로 빠진다. 실제로 그런 결함이 있었다.
    """

    @pytest.mark.parametrize("commits", [100, 300, 1_000, 20_000, 100_000])
    def test_interval_is_smaller_than_commit_count(self, commits: int) -> None:
        assert resolve_side_every(commits, None) < commits

    def test_explicit_value_is_respected(self) -> None:
        assert resolve_side_every(10_000, 42) == 42

    def test_small_fixture_still_gets_merges(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        path = tmp_path_factory.mktemp("small") / "repo"
        build_repository(path, 100)
        repo = pygit2.Repository(str(path))
        merges = [c for c in repo.walk(repo.head.target) if len(c.parent_ids) > 1]
        assert merges
