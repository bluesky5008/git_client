"""커밋 그래프 레인 배치 알고리즘.

시간 역순(자식이 부모보다 먼저)으로 들어오는 커밋 스트림을 받아,
각 커밋에 레인 번호와 그려야 할 선분(edge) 목록을 부여한다.

doc/design.md §4.1의 설계를 구현한다. 순수 파이썬이며 Qt/pygit2에 의존하지 않는다.

핵심 자료구조는 `_lanes`다. 각 원소는 "그 레인이 다음으로 기다리는 커밋 SHA"이며,
None이면 비어 있는 레인이다. 커밋이 하나 들어올 때마다:

  1. 그 커밋을 기다리던 레인들을 찾는다 (여러 개면 그 지점에서 합류한다)
  2. 없으면 새 레인을 연다 (브랜치 끝점)
  3. 첫 부모는 같은 레인으로 이어지고, 나머지 부모는 새 레인으로 갈라진다

증분 처리를 지원한다. 인스턴스를 유지한 채 `push()`를 계속 호출하면
앞서 계산한 상태에서 이어서 배치한다. 소비 주체는 CommitGraphModel이며,
워커가 밀어넣는 묶음(batch)마다 이어서 계산한다. (doc/design.md §4.1.1.1 —
뷰포트 기반 지연 로딩은 실측으로 기각됐고, 이 증분성은 묶음 단위 push를 위한 것이다)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# 레인 색상 팔레트의 크기. 실제 색상값은 UI 층이 정한다.
# 도메인 층은 색상 인덱스만 다룬다.
PALETTE_SIZE = 8


class EdgeKind(Enum):
    """행 안에서 선분이 그려지는 방식."""

    PASS = "pass"
    """이 행과 무관하게 위에서 아래로 지나가는 선."""

    INCOMING = "incoming"
    """위쪽에서 내려와 이 행의 노드로 합류하는 선."""

    OUTGOING = "outgoing"
    """이 행의 노드에서 아래쪽으로 뻗어나가는 선."""


@dataclass(frozen=True, slots=True)
class Edge:
    """한 행 안에서 그려질 선분 하나.

    `from_lane`은 행의 위쪽 경계에서의 레인 위치,
    `to_lane`은 행의 아래쪽 경계에서의 레인 위치다.
    INCOMING은 노드에서 끝나고 OUTGOING은 노드에서 시작하므로,
    실제 곡선 형태는 UI 층의 델리게이트가 결정한다.
    """

    from_lane: int
    to_lane: int
    color: int
    kind: EdgeKind


@dataclass(frozen=True, slots=True)
class GraphRow:
    """커밋 하나에 대응하는 그래프의 한 행."""

    sha: str
    lane: int
    color: int
    edges: tuple[Edge, ...]
    lane_count: int
    """이 행을 그리는 데 필요한 레인 수. 델리게이트의 폭 계산에 쓴다."""

    is_merge: bool = False


class LaneAllocator:
    """커밋 스트림을 그래프 행으로 변환한다.

    상태를 유지하므로 인스턴스 하나가 저장소 하나(정확히는 하나의 순회)에 대응한다.
    """

    def __init__(self) -> None:
        # 각 레인이 기다리는 커밋 SHA. None이면 빈 레인.
        self._lanes: list[str | None] = []
        # 각 레인의 색상 인덱스. 레인이 살아있는 동안 유지된다.
        self._colors: list[int] = []
        # 새 레인에 부여할 다음 색상. 단조 증가하므로 결정적이다.
        self._next_color = 0
        self._row_count = 0
        # 이미 배치가 끝난 커밋. 부모가 자식보다 먼저 나오는 경우를 걸러낸다.
        self._seen: set[str] = set()

    @property
    def row_count(self) -> int:
        return self._row_count

    def push(self, sha: str, parents: tuple[str, ...]) -> GraphRow:
        """커밋 하나를 배치하고 그래프 행을 반환한다."""
        before = list(self._lanes)

        my_lane, merged_lanes = self._claim_lane(sha)
        parent_lanes = self._assign_parents(my_lane, parents, merged_lanes)

        edges = self._build_edges(sha, my_lane, before, parent_lanes)

        # 색상은 레인 정리 전에 읽어야 한다. 루트 커밋이 마지막 레인에 있으면
        # 그 레인이 여기서 닫히고, 정리 후에는 인덱스가 사라진다.
        my_color = self._colors[my_lane]
        self._trim_trailing_empty_lanes()

        row = GraphRow(
            sha=sha,
            lane=my_lane,
            color=my_color,
            edges=edges,
            # 위/아래 경계 양쪽에서 필요한 레인 수 중 큰 값을 쓴다.
            # 이 행에서 레인이 닫히거나 열려도 선이 잘리지 않게 한다.
            lane_count=max(len(before), len(self._lanes), my_lane + 1),
            is_merge=len(parents) > 1,
        )
        self._row_count += 1
        self._seen.add(sha)
        return row

    def _claim_lane(self, sha: str) -> tuple[int, list[int]]:
        """이 커밋이 차지할 레인을 정한다.

        이 커밋을 기다리던 레인이 여러 개면 가장 왼쪽을 쓰고 나머지는 닫는다.
        (여러 브랜치가 이 커밋에서 합류하는 경우)
        기다리는 레인이 없으면 브랜치의 끝점이므로 새 레인을 연다.
        """
        waiting = [i for i, target in enumerate(self._lanes) if target == sha]

        if not waiting:
            lane = self._allocate_lane()
            return lane, []

        my_lane = waiting[0]
        for lane in waiting:
            self._lanes[lane] = None
        return my_lane, waiting[1:]

    def _assign_parents(
        self,
        my_lane: int,
        parents: tuple[str, ...],
        merged_lanes: list[int],
    ) -> list[int]:
        """부모들을 레인에 예약하고, 각 부모가 배정된 레인 목록을 반환한다.

        첫 부모는 현재 레인을 그대로 이어받아 히스토리가 직선으로 보이게 한다.
        나머지 부모(머지 커밋)는 새 레인으로 갈라진다.
        """
        if not parents:
            # 루트 커밋. 이 레인은 여기서 끝난다.
            self._lanes[my_lane] = None
            return []

        parent_lanes: list[int] = []

        if parents[0] in self._seen:
            # 부모가 이미 위쪽에 그려졌다. 아래로 내려가는 선을 그릴 수 없으므로
            # 이 레인은 여기서 닫는다. 그러지 않으면 영영 닫히지 않는 레인이 남는다.
            self._lanes[my_lane] = None
        else:
            self._lanes[my_lane] = parents[0]
            parent_lanes.append(my_lane)

        for parent in parents[1:]:
            if parent in self._seen:
                continue

            existing = self._find_lane_waiting_for(parent)
            if existing is not None:
                # 다른 레인이 이미 이 부모를 기다리고 있다. 새로 열지 않고 합류시킨다.
                parent_lanes.append(existing)
                continue

            # 방금 닫힌 레인을 우선 재사용하면 그래프가 옆으로 덜 퍼진다.
            lane = merged_lanes.pop(0) if merged_lanes else self._allocate_lane()
            self._lanes[lane] = parent
            parent_lanes.append(lane)

        return parent_lanes

    def _build_edges(
        self,
        sha: str,
        my_lane: int,
        before: list[str | None],
        parent_lanes: list[int],
    ) -> tuple[Edge, ...]:
        """이 행에서 그려야 할 선분들을 만든다."""
        edges: list[Edge] = []

        for lane, target in enumerate(before):
            if target is None:
                continue
            if target == sha:
                # 이 커밋으로 합류하는 선. 노드에서 끝난다.
                edges.append(
                    Edge(
                        from_lane=lane,
                        to_lane=my_lane,
                        color=self._colors[lane],
                        kind=EdgeKind.INCOMING,
                    )
                )
            else:
                # 이 행과 무관한 다른 브랜치의 선. 그대로 통과시킨다.
                edges.append(
                    Edge(
                        from_lane=lane,
                        to_lane=lane,
                        color=self._colors[lane],
                        kind=EdgeKind.PASS,
                    )
                )

        for lane in parent_lanes:
            edges.append(
                Edge(
                    from_lane=my_lane,
                    to_lane=lane,
                    color=self._colors[lane],
                    kind=EdgeKind.OUTGOING,
                )
            )

        return tuple(edges)

    def _allocate_lane(self) -> int:
        """비어 있는 가장 왼쪽 레인을 쓰거나, 없으면 새로 만든다."""
        for i, target in enumerate(self._lanes):
            if target is None:
                self._colors[i] = self._take_color()
                return i

        self._lanes.append(None)
        self._colors.append(self._take_color())
        return len(self._lanes) - 1

    def _find_lane_waiting_for(self, sha: str) -> int | None:
        for i, target in enumerate(self._lanes):
            if target == sha:
                return i
        return None

    def _take_color(self) -> int:
        color = self._next_color % PALETTE_SIZE
        self._next_color += 1
        return color

    def _trim_trailing_empty_lanes(self) -> None:
        """오른쪽 끝의 빈 레인을 정리해 그래프가 불필요하게 넓어지지 않게 한다."""
        while self._lanes and self._lanes[-1] is None:
            self._lanes.pop()
            self._colors.pop()


def layout(commits: list[tuple[str, tuple[str, ...]]]) -> list[GraphRow]:
    """(sha, parents) 목록 전체를 한 번에 배치한다.

    테스트와 소규모 저장소용 편의 함수다. 실제 UI 경로는 `LaneAllocator`를
    직접 들고 청크 단위로 `push()`를 호출한다.
    """
    allocator = LaneAllocator()
    return [allocator.push(sha, parents) for sha, parents in commits]
