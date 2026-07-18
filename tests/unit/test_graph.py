"""커밋 그래프 레인 배치 알고리즘 테스트.

doc/design.md §8: 직선 히스토리, 단순 머지, 옥토퍼스 머지, 고아 브랜치,
교차 머지 등 각 형태별 기대 레인 배치를 픽스처로 고정한다.

주의: 커밋은 시간 역순(자식이 부모보다 먼저)으로 들어온다.
"""

from __future__ import annotations

import pytest

from gitclient.domain.graph import (
    Edge,
    EdgeKind,
    GraphRow,
    LaneAllocator,
    layout,
)


def lanes_of(rows: list[GraphRow]) -> list[int]:
    return [row.lane for row in rows]


def edges_of(row: GraphRow, kind: EdgeKind) -> list[Edge]:
    return [e for e in row.edges if e.kind is kind]


class TestLinearHistory:
    """C -> B -> A 직선 히스토리는 전부 레인 0에 놓인다."""

    @pytest.fixture
    def rows(self) -> list[GraphRow]:
        return layout([("C", ("B",)), ("B", ("A",)), ("A", ())])

    def test_all_commits_share_lane_zero(self, rows: list[GraphRow]) -> None:
        assert lanes_of(rows) == [0, 0, 0]

    def test_lane_count_is_one(self, rows: list[GraphRow]) -> None:
        assert [r.lane_count for r in rows] == [1, 1, 1]

    def test_color_is_stable_along_the_line(self, rows: list[GraphRow]) -> None:
        assert len({r.color for r in rows}) == 1

    def test_middle_commit_has_incoming_and_outgoing(
        self, rows: list[GraphRow]
    ) -> None:
        middle = rows[1]
        assert len(edges_of(middle, EdgeKind.INCOMING)) == 1
        assert len(edges_of(middle, EdgeKind.OUTGOING)) == 1

    def test_root_commit_has_no_outgoing_edge(self, rows: list[GraphRow]) -> None:
        assert edges_of(rows[-1], EdgeKind.OUTGOING) == []

    def test_tip_commit_has_no_incoming_edge(self, rows: list[GraphRow]) -> None:
        assert edges_of(rows[0], EdgeKind.INCOMING) == []


class TestSimpleMerge:
    """머지 커밋에서 갈라졌다가 공통 조상에서 다시 합쳐지는 형태.

        M       머지 커밋 (부모: F, S)
        |\\
        F |     첫 부모 쪽
        | S     두 번째 부모 쪽 (갈라진 브랜치)
        |/
        B       공통 조상
    """

    @pytest.fixture
    def rows(self) -> list[GraphRow]:
        return layout(
            [
                ("M", ("F", "S")),
                ("F", ("B",)),
                ("S", ("B",)),
                ("B", ()),
            ]
        )

    def test_first_parent_stays_on_merge_lane(self, rows: list[GraphRow]) -> None:
        merge, first_parent = rows[0], rows[1]
        assert first_parent.lane == merge.lane

    def test_second_parent_branches_to_new_lane(self, rows: list[GraphRow]) -> None:
        merge, second_parent = rows[0], rows[2]
        assert second_parent.lane != merge.lane

    def test_merge_commit_is_flagged(self, rows: list[GraphRow]) -> None:
        assert rows[0].is_merge is True
        assert all(not r.is_merge for r in rows[1:])

    def test_merge_emits_two_outgoing_edges(self, rows: list[GraphRow]) -> None:
        assert len(edges_of(rows[0], EdgeKind.OUTGOING)) == 2

    def test_branches_rejoin_at_common_ancestor(self, rows: list[GraphRow]) -> None:
        # 두 브랜치가 B로 합류하므로 B 행에는 INCOMING이 2개다.
        assert len(edges_of(rows[3], EdgeKind.INCOMING)) == 2

    def test_lane_is_reused_after_rejoin(self, rows: list[GraphRow]) -> None:
        # 합류 이후에는 다시 한 줄이므로 레인 하나면 충분하다.
        assert rows[3].lane == 0

    def test_second_parent_lane_color_differs(self, rows: list[GraphRow]) -> None:
        assert rows[2].color != rows[0].color


class TestOctopusMerge:
    """부모가 3개 이상인 머지."""

    @pytest.fixture
    def rows(self) -> list[GraphRow]:
        return layout(
            [
                ("O", ("A", "B", "C")),
                ("A", ()),
                ("B", ()),
                ("C", ()),
            ]
        )

    def test_each_parent_gets_its_own_lane(self, rows: list[GraphRow]) -> None:
        parent_lanes = {row.lane for row in rows[1:]}
        assert len(parent_lanes) == 3

    def test_octopus_emits_one_outgoing_edge_per_parent(
        self, rows: list[GraphRow]
    ) -> None:
        assert len(edges_of(rows[0], EdgeKind.OUTGOING)) == 3

    def test_lane_count_covers_all_parents(self, rows: list[GraphRow]) -> None:
        assert rows[0].lane_count >= 3


class TestOrphanBranch:
    """공통 조상이 없는 독립 히스토리 두 개."""

    @pytest.fixture
    def rows(self) -> list[GraphRow]:
        return layout(
            [
                ("A2", ("A1",)),
                ("A1", ()),
                ("B2", ("B1",)),
                ("B1", ()),
            ]
        )

    def test_orphan_reuses_freed_lane(self, rows: list[GraphRow]) -> None:
        # 첫 히스토리가 A1에서 끝나 레인이 비므로, 두 번째 히스토리가 이를 재사용한다.
        assert lanes_of(rows) == [0, 0, 0, 0]

    def test_orphan_gets_a_fresh_color(self, rows: list[GraphRow]) -> None:
        # 레인은 재사용해도 색은 새로 부여되어야 별개의 히스토리로 보인다.
        assert rows[2].color != rows[0].color

    def test_no_edges_cross_between_histories(self, rows: list[GraphRow]) -> None:
        # A1은 루트라 나가는 선이 없고, B2는 새 시작이라 들어오는 선이 없다.
        assert edges_of(rows[1], EdgeKind.OUTGOING) == []
        assert edges_of(rows[2], EdgeKind.INCOMING) == []


class TestCrossMerge:
    """서로 다른 시점에 갈라지고 합쳐지는, 레인이 여러 개 살아있는 형태.

        M2      부모: M1, T
        |\\
        | T     장기 브랜치의 커밋
        M1 |    부모: X, Y
        |\\ |
        X | |
        | Y |
        |/ /
        R
    """

    @pytest.fixture
    def rows(self) -> list[GraphRow]:
        return layout(
            [
                ("M2", ("M1", "T")),
                ("T", ("R",)),
                ("M1", ("X", "Y")),
                ("X", ("R",)),
                ("Y", ("R",)),
                ("R", ()),
            ]
        )

    def test_three_lanes_are_live_at_the_widest_point(
        self, rows: list[GraphRow]
    ) -> None:
        assert max(r.lane_count for r in rows) == 3

    def test_all_branches_converge_on_root(self, rows: list[GraphRow]) -> None:
        root = rows[-1]
        # T, X, Y 세 갈래가 R로 합류한다.
        assert len(edges_of(root, EdgeKind.INCOMING)) == 3

    def test_pass_through_edges_are_emitted(self, rows: list[GraphRow]) -> None:
        # M1 행에서는 T가 속한 레인이 이 행과 무관하게 지나가야 한다.
        m1 = rows[2]
        assert len(edges_of(m1, EdgeKind.PASS)) >= 1

    def test_lanes_never_collide(self, rows: list[GraphRow]) -> None:
        # 같은 행 안에서 두 선이 같은 위치에서 시작해 다른 곳으로 가면 안 된다.
        for row in rows:
            passing = [e for e in row.edges if e.kind is EdgeKind.PASS]
            starts = [e.from_lane for e in passing]
            assert len(starts) == len(set(starts))


class TestSharedParentDeduplication:
    """머지의 두 부모가 같은 커밋을 가리키는 경우 레인을 중복 생성하지 않는다."""

    def test_duplicate_parent_reuses_existing_lane(self) -> None:
        rows = layout([("M", ("P", "P")), ("P", ())])
        merge = rows[0]
        outgoing = edges_of(merge, EdgeKind.OUTGOING)
        # 같은 부모이므로 도착 레인이 동일해야 한다.
        assert len({e.to_lane for e in outgoing}) == 1


class TestIncrementalLayout:
    """묶음(batch) 단위 증분 계산이 일괄 계산과 같은 결과를 내야 한다.

    워커가 커밋을 묶음으로 밀어넣을 때(doc/design.md §4.1.1.1) 묶음 경계에서
    레인 배치가 끊기지 않음을 보장하는 테스트다.
    """

    COMMITS = [
        ("M", ("F", "S")),
        ("F", ("B",)),
        ("S", ("B",)),
        ("B", ("A",)),
        ("A", ()),
    ]

    def test_chunked_push_matches_batch_layout(self) -> None:
        batch = layout(self.COMMITS)

        allocator = LaneAllocator()
        chunked: list[GraphRow] = []
        for sha, parents in self.COMMITS[:2]:
            chunked.append(allocator.push(sha, parents))
        for sha, parents in self.COMMITS[2:]:
            chunked.append(allocator.push(sha, parents))

        assert chunked == batch

    def test_row_count_tracks_pushes(self) -> None:
        allocator = LaneAllocator()
        assert allocator.row_count == 0
        for sha, parents in self.COMMITS:
            allocator.push(sha, parents)
        assert allocator.row_count == len(self.COMMITS)


class TestUnknownParents:
    """shallow clone처럼 부모가 순회 범위 밖에 있는 경우에도 죽지 않아야 한다."""

    def test_missing_parent_does_not_raise(self) -> None:
        rows = layout([("C", ("MISSING",))])
        assert len(rows) == 1
        assert rows[0].lane == 0

    def test_missing_parent_still_emits_outgoing_edge(self) -> None:
        # 경계 너머로 이어지는 선은 그려져야 히스토리가 끊긴 게 아님을 보여준다.
        rows = layout([("C", ("MISSING",))])
        assert len(edges_of(rows[0], EdgeKind.OUTGOING)) == 1


class TestOutOfOrderParents:
    """부모가 자식보다 먼저 나오는 경우에도 레인이 새지 않아야 한다.

    시간순 정렬(SortMode.TIME)은 스트리밍이라 첫 결과가 빠르지만, 커밋 시각이
    뒤틀린 경우(시계 오차, rebase) 부모가 자식보다 먼저 나올 수 있다.
    이때 이미 지나간 부모를 기다리는 레인을 새로 열면 그 레인은 영원히 닫히지
    않고, 이후 모든 행이 한 칸씩 밀린다.
    """

    def test_already_seen_parent_does_not_leak_a_lane(self) -> None:
        # P가 C보다 먼저 나온다. C는 P를 부모로 갖는다.
        rows = layout([("P", ()), ("C", ("P",)), ("N", ())])
        # P의 레인은 P에서 닫혔고, C는 이미 지나간 부모를 기다릴 수 없다.
        # 따라서 N은 레인 0을 재사용해야 한다.
        assert rows[2].lane == 0

    def test_already_seen_parent_emits_no_dangling_edge(self) -> None:
        rows = layout([("P", ()), ("C", ("P",))])
        # 위로 거슬러 올라가는 선은 그릴 수 없으므로 나가는 선이 없어야 한다.
        assert edges_of(rows[1], EdgeKind.OUTGOING) == []

    def test_lane_count_does_not_grow(self) -> None:
        rows = layout([("P", ()), ("C", ("P",)), ("N", ())])
        assert max(r.lane_count for r in rows) == 1


class TestEdgeGeometry:
    """델리게이트가 선을 그리는 데 필요한 좌표가 일관되게 나오는지 확인한다."""

    def test_incoming_edges_end_at_the_node(self) -> None:
        rows = layout([("M", ("F", "S")), ("F", ("B",)), ("S", ("B",)), ("B", ())])
        for row in rows:
            for edge in edges_of(row, EdgeKind.INCOMING):
                assert edge.to_lane == row.lane

    def test_outgoing_edges_start_at_the_node(self) -> None:
        rows = layout([("M", ("F", "S")), ("F", ("B",)), ("S", ("B",)), ("B", ())])
        for row in rows:
            for edge in edges_of(row, EdgeKind.OUTGOING):
                assert edge.from_lane == row.lane

    def test_pass_edges_are_vertical(self) -> None:
        rows = layout([("M", ("F", "S")), ("F", ("B",)), ("S", ("B",)), ("B", ())])
        for row in rows:
            for edge in edges_of(row, EdgeKind.PASS):
                assert edge.from_lane == edge.to_lane

    def test_every_edge_stays_within_lane_count(self) -> None:
        rows = layout(
            [
                ("M2", ("M1", "T")),
                ("T", ("R",)),
                ("M1", ("X", "Y")),
                ("X", ("R",)),
                ("Y", ("R",)),
                ("R", ()),
            ]
        )
        for row in rows:
            for edge in row.edges:
                assert 0 <= edge.from_lane < row.lane_count
                assert 0 <= edge.to_lane < row.lane_count
