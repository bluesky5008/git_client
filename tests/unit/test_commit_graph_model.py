"""CommitGraphModel 테스트.

모델은 밀어넣기 방식이다. 워커가 읽어온 묶음을 순서대로 받아 행을 늘린다.
묶음 경계를 넘어서도 레인 배치가 이어져야 한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QModelIndex, Qt

from gitclient.domain.models import Commit, Ref, RefKind, Signature
from gitclient.viewmodel.commit_graph_model import (
    Column,
    CommitGraphModel,
    CommitRole,
    format_relative,
)

WHEN = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def make_commit(index: int, parents: tuple[str, ...] = ()) -> Commit:
    signature = Signature("테스터", "tester@example.com", WHEN)
    return Commit(
        sha=f"{index:040x}",
        parents=parents,
        author=signature,
        committer=signature,
        message=f"커밋 {index}\n\n본문 {index}",
    )


def linear_commits(count: int) -> list[Commit]:
    """최신이 앞에 오는 선형 히스토리."""
    commits = []
    for i in range(count):
        parents = (f"{i + 1:040x}",) if i + 1 < count else ()
        commits.append(make_commit(i, parents))
    return commits


def merge_history() -> list[Commit]:
    """머지가 하나 있는 히스토리. 레인이 2개로 벌어진다."""
    signature = Signature("t", "t@e.com", WHEN)

    def commit(sha: str, parents: tuple[str, ...]) -> Commit:
        return Commit(sha, parents, signature, signature, "m")

    return [
        commit("M", ("F", "S")),
        commit("F", ("B",)),
        commit("S", ("B",)),
        commit("B", ()),
    ]


@pytest.fixture
def model(qtbot) -> CommitGraphModel:  # noqa: ANN001 - pytest-qt 픽스처
    return CommitGraphModel()


class TestBatchLoading:
    def test_starts_empty(self, model: CommitGraphModel) -> None:
        model.reset([])
        assert model.rowCount() == 0

    def test_append_adds_rows(self, model: CommitGraphModel) -> None:
        model.reset([])
        model.append_commits(linear_commits(3))
        assert model.rowCount() == 3

    def test_successive_batches_accumulate(self, model: CommitGraphModel) -> None:
        commits = linear_commits(10)
        model.reset([])
        model.append_commits(commits[:4])
        model.append_commits(commits[4:])
        assert model.rowCount() == 10

    def test_empty_batch_is_ignored(self, model: CommitGraphModel) -> None:
        model.reset([])
        model.append_commits([])
        assert model.rowCount() == 0

    def test_reset_clears_previous_repository(self, model: CommitGraphModel) -> None:
        model.reset([])
        model.append_commits(linear_commits(10))
        model.reset([])
        assert model.rowCount() == 0

    def test_rows_inserted_signal_is_emitted(
        self,
        model: CommitGraphModel,
        qtbot,  # noqa: ANN001
    ) -> None:
        model.reset([])
        with qtbot.waitSignal(model.rowsInserted, timeout=1000):
            model.append_commits(linear_commits(3))


class TestLaneContinuityAcrossBatches:
    """묶음 경계에서 레인 배치가 끊기면 그래프의 선이 어긋난다."""

    def test_split_batches_match_single_batch(
        self, model: CommitGraphModel
    ) -> None:
        commits = merge_history()

        whole = CommitGraphModel()
        whole.reset([])
        whole.append_commits(list(commits))
        expected = [
            whole.index(r, 0).data(CommitRole.GRAPH_ROW)
            for r in range(whole.rowCount())
        ]

        model.reset([])
        model.append_commits(commits[:2])
        model.append_commits(commits[2:])
        actual = [
            model.index(r, 0).data(CommitRole.GRAPH_ROW)
            for r in range(model.rowCount())
        ]

        assert actual == expected


class TestData:
    @pytest.fixture
    def loaded(self, model: CommitGraphModel) -> CommitGraphModel:
        model.reset([])
        model.append_commits(linear_commits(3))
        return model

    def test_summary_column_shows_first_line(self, loaded: CommitGraphModel) -> None:
        index = loaded.index(0, Column.SUMMARY)
        assert index.data(Qt.ItemDataRole.DisplayRole) == "커밋 0"

    def test_author_column(self, loaded: CommitGraphModel) -> None:
        index = loaded.index(0, Column.AUTHOR)
        assert index.data(Qt.ItemDataRole.DisplayRole) == "테스터"

    def test_sha_column_is_short(self, loaded: CommitGraphModel) -> None:
        index = loaded.index(0, Column.SHA)
        assert len(index.data(Qt.ItemDataRole.DisplayRole)) == 7

    def test_commit_role_returns_domain_object(
        self, loaded: CommitGraphModel
    ) -> None:
        commit = loaded.index(0, Column.GRAPH).data(CommitRole.COMMIT)
        assert isinstance(commit, Commit)

    def test_graph_row_role_is_populated(self, loaded: CommitGraphModel) -> None:
        graph_row = loaded.index(0, Column.GRAPH).data(CommitRole.GRAPH_ROW)
        assert graph_row.lane == 0

    def test_tooltip_contains_full_sha(self, loaded: CommitGraphModel) -> None:
        tooltip = loaded.index(0, Column.SUMMARY).data(Qt.ItemDataRole.ToolTipRole)
        assert loaded.commit_at(0).sha in tooltip

    def test_invalid_index_returns_none(self, loaded: CommitGraphModel) -> None:
        assert loaded.data(QModelIndex()) is None


class TestRefs:
    def test_refs_are_mapped_to_their_commit(self, model: CommitGraphModel) -> None:
        commits = linear_commits(3)
        ref = Ref(
            name="refs/heads/main",
            shorthand="main",
            kind=RefKind.LOCAL_BRANCH,
            target_sha=commits[0].sha,
            is_head=True,
        )
        model.reset([ref])
        model.append_commits(commits)

        assert model.index(0, Column.SUMMARY).data(CommitRole.REFS) == [ref]
        assert model.index(1, Column.SUMMARY).data(CommitRole.REFS) == []


class TestLaneWidth:
    def test_tracks_widest_row(self, model: CommitGraphModel) -> None:
        model.reset([])
        model.append_commits(merge_history())
        assert model.max_lane_count == 2


class TestFormatRelative:
    def test_just_now(self) -> None:
        assert format_relative(datetime.now(timezone.utc)) == "방금"

    def test_minutes(self) -> None:
        when = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert format_relative(when) == "5분 전"

    def test_hours(self) -> None:
        when = datetime.now(timezone.utc) - timedelta(hours=3)
        assert format_relative(when) == "3시간 전"

    def test_days(self) -> None:
        when = datetime.now(timezone.utc) - timedelta(days=2)
        assert format_relative(when) == "2일 전"

    def test_old_commits_use_absolute_date(self) -> None:
        when = datetime.now(timezone.utc) - timedelta(days=90)
        assert format_relative(when) == when.strftime("%Y-%m-%d")

    def test_future_timestamp_falls_back_to_absolute(self) -> None:
        # 시계 오차나 조작된 커밋 시각. "-3분 전" 같은 표시가 나오면 안 된다.
        when = datetime.now(timezone.utc) + timedelta(hours=2)
        assert format_relative(when) == when.strftime("%Y-%m-%d %H:%M")
