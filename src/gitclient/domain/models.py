"""도메인 모델.

이 모듈은 순수 파이썬만 사용한다. Qt와 pygit2에 의존하지 않는다.
(doc/design.md §3.1 의존 방향 규칙)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


@dataclass(frozen=True, slots=True)
class Signature:
    """커밋의 작성자 또는 커미터 정보."""

    name: str
    email: str
    when: datetime

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"


@dataclass(frozen=True, slots=True)
class Commit:
    """단일 커밋.

    `parents`는 부모 커밋의 SHA 목록이다. 첫 번째 항목이 첫 부모이며,
    그래프 레인 배치에서 직선으로 이어지는 쪽이다.
    """

    sha: str
    parents: tuple[str, ...]
    author: Signature
    committer: Signature
    message: str

    @property
    def short_sha(self) -> str:
        return self.sha[:7]

    @property
    def summary(self) -> str:
        """커밋 메시지의 첫 줄."""
        return self.message.split("\n", 1)[0].strip()

    @property
    def body(self) -> str:
        """커밋 메시지에서 첫 줄을 제외한 나머지."""
        parts = self.message.split("\n", 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


class RefKind(Enum):
    LOCAL_BRANCH = "local_branch"
    REMOTE_BRANCH = "remote_branch"
    TAG = "tag"


@dataclass(frozen=True, slots=True)
class Ref:
    """브랜치 또는 태그.

    `shorthand`는 화면 표시용 짧은 이름이다.
    (refs/heads/main -> main, refs/remotes/origin/main -> origin/main)
    """

    name: str
    shorthand: str
    kind: RefKind
    target_sha: str
    is_head: bool = False


class ChangeStatus(Enum):
    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"
    RENAMED = "R"
    COPIED = "C"
    TYPECHANGE = "T"
    UNKNOWN = "?"


@dataclass(frozen=True, slots=True)
class FileChange:
    """커밋에 포함된 파일 하나의 변경."""

    path: str
    status: ChangeStatus
    old_path: str | None = None
    insertions: int = 0
    deletions: int = 0

    @property
    def display_path(self) -> str:
        if self.status is ChangeStatus.RENAMED and self.old_path:
            return f"{self.old_path} -> {self.path}"
        return self.path


class DiffLineKind(Enum):
    CONTEXT = "context"
    ADDITION = "addition"
    DELETION = "deletion"
    HUNK_HEADER = "hunk_header"
    FILE_HEADER = "file_header"


@dataclass(frozen=True, slots=True)
class DiffLine:
    """diff 뷰가 그리는 한 줄.

    `old_lineno`/`new_lineno`가 None이면 해당 쪽에 존재하지 않는 줄이다.
    """

    kind: DiffLineKind
    text: str
    old_lineno: int | None = None
    new_lineno: int | None = None


@dataclass(frozen=True, slots=True)
class CommitDetail:
    """커밋 하나의 상세 정보. 커밋을 선택했을 때 화면 아래쪽에 표시된다."""

    commit: Commit
    changes: tuple[FileChange, ...] = ()

    @property
    def total_insertions(self) -> int:
        return sum(c.insertions for c in self.changes)

    @property
    def total_deletions(self) -> int:
        return sum(c.deletions for c in self.changes)


class WorkAreaStatus(Enum):
    """워킹 트리/인덱스에서 파일 하나의 상태."""

    NEW = "A"
    MODIFIED = "M"
    DELETED = "D"
    RENAMED = "R"
    CONFLICTED = "!"


@dataclass(frozen=True, slots=True)
class WorkingFileChange:
    """커밋되지 않은 변경 하나.

    같은 파일이 staged와 unstaged 양쪽에 동시에 나타날 수 있다
    (일부만 스테이징한 뒤 또 수정한 경우). 이때는 항목이 두 개 생긴다.
    """

    path: str
    status: WorkAreaStatus
    staged: bool


@dataclass(frozen=True, slots=True)
class WorkingTreeStatus:
    """작업 디렉터리 전체 상태."""

    staged: tuple[WorkingFileChange, ...] = ()
    unstaged: tuple[WorkingFileChange, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not self.staged and not self.unstaged


@dataclass(slots=True)
class RepositoryInfo:
    """열려 있는 저장소의 요약 정보."""

    path: str
    workdir: str | None
    head_shorthand: str | None
    is_empty: bool = False
    is_bare: bool = False
    is_shallow: bool = False
    is_partial: bool = False
    """부분 복제(blob 지연 수신) 저장소인가.

    shallow와 마찬가지로 **지속되는 제약**이다 — 오프라인에서 과거 파일을
    열 수 없다. 고른 시점과 막히는 시점이 멀어서 상시 표시가 필요하다.
    """

    refs: list[Ref] = field(default_factory=list)
    remotes: list[str] = field(default_factory=list)
    """설정된 원격 이름들. 비어 있으면 원격 작업이 불가능하다."""

    @property
    def display_name(self) -> str:
        """제목 표시줄에 쓸 저장소 이름."""
        base = self.workdir or self.path
        return base.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


class MergeKind(Enum):
    """upstream을 합칠 때 무엇이 필요한가."""

    UP_TO_DATE = "up_to_date"
    """이미 최신 — 할 일이 없다."""

    FAST_FORWARD = "fast_forward"
    """로컬에 고유 커밋이 없어 참조만 앞으로 옮기면 된다."""

    MERGE_REQUIRED = "merge_required"
    """양쪽에 서로 다른 커밋이 있어 병합 커밋이 필요하다."""


@dataclass(frozen=True, slots=True)
class MergePreview:
    """합치기 전 분석 결과. 저장소를 바꾸지 않고 얻는다."""

    kind: MergeKind
    target_sha: str


class ConflictSide(Enum):
    """충돌한 파일이 양쪽에서 어떤 상태인가.

    내용끼리 부딪히는 것만 충돌이 아니다. 한쪽이 지우고 한쪽이 고친 경우도
    충돌이며, 이때는 보여줄 "상대 내용"이 아예 없다 — UI가 3-way를 그리려면
    이 구분이 필요하다.
    """

    BOTH_MODIFIED = "both_modified"
    """양쪽이 내용을 고쳤다.

    텍스트라면 워킹 트리에 충돌 마커가 들어가지만 **바이너리면 들어가지
    않는다** — libgit2가 3-way 텍스트 병합을 포기하고 우리 것을 그대로
    남긴다. 마커 유무는 side가 아니라 `has_markers`로 판단할 것.
    """

    DELETED_BY_THEM = "deleted_by_them"
    """상대가 지웠고 우리가 고쳤다."""

    DELETED_BY_US = "deleted_by_us"
    """우리가 지웠고 상대가 고쳤다."""

    BOTH_ADDED = "both_added"
    """양쪽이 같은 경로에 서로 다른 파일을 새로 만들었다 (공통 조상 없음)."""

    BOTH_DELETED = "both_deleted"
    """양쪽 모두 이 경로를 지웠다 — 보통 이름 변경 대 삭제에서 원래 경로가
    이렇게 남는다. 어느 쪽에도 보여줄 내용이 없다 (git의 DD)."""


@dataclass(frozen=True, slots=True)
class ConflictedFile:
    """병합이 자동으로 해결하지 못한 파일 하나."""

    path: str
    side: ConflictSide
    has_markers: bool = True
    """워킹 트리 파일에 충돌 마커가 들어 있는가.

    바이너리 충돌과 삭제 계열 충돌에는 마커가 없다. 이때 워킹 트리에는
    우리 것만 남아 있어서, "마커를 정리하고 커밋하라"는 안내를 그대로
    따르면 **상대 변경이 조용히 버려진** 머지 커밋이 만들어진다.
    """

    @property
    def has_our_content(self) -> bool:
        return self.side not in (
            ConflictSide.DELETED_BY_US,
            ConflictSide.BOTH_DELETED,
        )

    @property
    def has_their_content(self) -> bool:
        return self.side not in (
            ConflictSide.DELETED_BY_THEM,
            ConflictSide.BOTH_DELETED,
        )


class RepoOperation(Enum):
    """저장소에 **진행 중인** 히스토리 연산.

    충돌이 났을 때 무엇을 보여주고 무엇을 제안할지가 여기서 갈린다.
    특히 rebase는 스테이지 2/3의 주체가 병합과 **반대다** — 그 구분을
    잃으면 "내 것 사용"이 사용자 자신의 커밋을 버린다 (ADR-65).
    """

    NONE = "none"
    MERGE = "merge"
    REBASE = "rebase"
    CHERRY_PICK = "cherry_pick"
    REVERT = "revert"
    OTHER = "other"
    """bisect 등 우리가 다루지 않는 상태. 손대지 않는다."""

    @property
    def is_active(self) -> bool:
        return self is not RepoOperation.NONE

    @property
    def label(self) -> str:
        return _OPERATION_LABELS.get(self, "진행 중인 작업")

    @property
    def can_continue(self) -> bool:
        """충돌을 다 풀면 이어서 진행할 수 있는 연산인가.

        병합은 예외다 — 이어가는 방법이 `--continue`가 아니라 그냥
        커밋이므로 기존 커밋 경로를 쓴다.
        """
        return self in (
            RepoOperation.REBASE,
            RepoOperation.CHERRY_PICK,
            RepoOperation.REVERT,
        )

    @property
    def can_abort(self) -> bool:
        """앱이 되돌릴 수 있는 연산인가.

        `OTHER`(bisect 등)는 아니다. 엔진은 거부하지만, 그렇다고 버튼을
        내주면 **누를 때마다 오류만 돌아오는 버튼**이 된다 — 빠져나갈 길처럼
        보이는데 아닌 것이 길이 아예 없는 것보다 나쁘다.
        """
        return self is RepoOperation.MERGE or self.can_continue


_OPERATION_LABELS = {
    RepoOperation.MERGE: "병합",
    RepoOperation.REBASE: "리베이스",
    RepoOperation.CHERRY_PICK: "커밋 가져오기(cherry-pick)",
    RepoOperation.REVERT: "되돌리기(revert)",
    RepoOperation.OTHER: "진행 중인 작업",
}


class ResetKind(Enum):
    """reset이 무엇까지 되돌리는가 — 위험도가 다르다."""

    SOFT = "soft"
    """커밋만 되돌린다. 인덱스와 워킹 트리는 그대로."""

    MIXED = "mixed"
    """커밋과 인덱스를 되돌린다. 워킹 트리는 그대로 — 변경이 미스테이징으로 남는다."""

    HARD = "hard"
    """**워킹 트리까지 되돌린다.** 커밋하지 않은 작업이 사라진다."""

    @property
    def discards_working_tree(self) -> bool:
        return self is ResetKind.HARD


class ConflictChoice(Enum):
    """충돌을 해결할 때 어느 쪽을 남길 것인가."""

    OURS = "ours"
    """현재 브랜치의 것."""

    THEIRS = "theirs"
    """합치려는 쪽의 것."""


@dataclass(frozen=True, slots=True)
class ConflictSideContent:
    """충돌한 파일의 한쪽 내용.

    **없을 수도 있다.** 한쪽이 지운 충돌에서는 그쪽 내용이 존재하지 않는다 —
    빈 파일과 다르다. `exists`가 그 구분이다.
    """

    exists: bool
    data: bytes = b""
    is_binary: bool = False

    @property
    def text(self) -> str:
        """화면에 그릴 문자열. 바이너리면 빈 문자열."""
        if not self.exists or self.is_binary:
            return ""
        return self.data.decode("utf-8", "replace")


@dataclass(frozen=True, slots=True)
class ConflictDetail:
    """충돌한 파일 하나의 양쪽 내용. 3-way 화면이 그릴 재료다."""

    path: str
    side: ConflictSide
    ours: ConflictSideContent
    theirs: ConflictSideContent

    @property
    def is_binary(self) -> bool:
        return self.ours.is_binary or self.theirs.is_binary

    @property
    def can_show_text(self) -> bool:
        """내용을 나란히 보여줄 수 있는가.

        바이너리는 줄 개념이 없어 비교할 수 없다. 그때도 **한쪽 선택은
        가능해야 한다** — 그것이 이 화면이 없으면 해결할 수 없던 경우다.
        """
        return not self.is_binary


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """병합 시도의 결과.

    **충돌은 실패가 아니다.** git이 할 수 있는 만큼 합쳐두고 나머지를 사람에게
    넘긴 상태이며, 저장소는 병합 진행 중(MERGE_HEAD 존재)으로 남는다.
    그래서 예외가 아니라 결과 값으로 돌려준다 — 호출자가 "무엇을 해야 하는가"를
    분기해야 하기 때문이다.
    """

    kind: MergeKind
    """UP_TO_DATE / FAST_FORWARD / MERGE_REQUIRED 중 실제로 수행된 것."""

    conflicts: tuple[ConflictedFile, ...] = ()
    merged_sha: str | None = None
    """만들어진 머지 커밋. 충돌이 있으면 None (아직 커밋 전이다)."""

    @property
    def is_conflicted(self) -> bool:
        return bool(self.conflicts)


@dataclass(frozen=True, slots=True)
class ConflictLabels:
    """충돌한 양쪽을 화면에서 뭐라고 부를 것인가.

    **이름이 틀리면 데이터가 사라진다.** 인덱스의 스테이지 2/3은 연산에
    상관없이 각각 "ours"/"theirs"지만, 그 자리에 누가 오는지는 연산마다
    다르다. rebase는 stage 2가 올라탈 곳이고 stage 3이 사용자 자신의
    커밋이라, 병합 기준 라벨을 그대로 쓰면 "내 것 사용"이 사용자의 커밋을
    버린다 (ADR-65에서 실측으로 확인).

    그래서 라벨은 UI가 정하지 않는다 — 연산에서 유도한다.
    """

    ours: str
    """스테이지 2. git이 부르는 이름은 언제나 "ours"다."""

    theirs: str
    """스테이지 3. git이 부르는 이름은 언제나 "theirs"다."""

    note: str = ""
    """이 연산에서 방향이 헷갈릴 때 덧붙일 한 줄. 없으면 빈 문자열."""


# **여기가 유일한 출처다.** 스테이지 번호는 고정이지만(2=ours, 3=theirs) 그
# 자리에 누가 오는지는 연산마다 다르다 — 실측한 값은 다음과 같다.
#
#   연산          stage 2            stage 3
#   ----------    ---------------    ------------------
#   merge         현재 브랜치        합치는 쪽
#   rebase        올라탈 곳          재생 중인 내 커밋      ← 유일하게 뒤집힌다
#   cherry-pick   현재 브랜치        가져오는 커밋
#   revert        현재 브랜치        되돌린 결과
#
# 이 표를 요약한 `swaps_sides` 불리언을 한때 두었는데 **아무도 읽지 않았다** —
# 변이 검증에서 그 값을 뒤집어도 테스트가 전부 통과했다. 권위 있어 보이는
# 죽은 코드는 없는 것보다 나쁘다: 다음 사람이 그것을 고치고 아무것도 바뀌지
# 않았다는 사실을 모른 채 넘어간다. 사실은 그것을 쓰는 자리에만 둔다.
_CONFLICT_LABELS = {
    RepoOperation.MERGE: ConflictLabels(
        ours="내 것 (현재 브랜치)",
        theirs="상대 것 (합치는 쪽)",
    ),
    RepoOperation.REBASE: ConflictLabels(
        ours="올라탈 곳 (upstream)",
        theirs="재생 중인 내 커밋",
        note="리베이스에서는 방향이 병합과 반대입니다 — "
        "내가 쓴 변경은 오른쪽입니다.",
    ),
    RepoOperation.CHERRY_PICK: ConflictLabels(
        ours="현재 브랜치",
        theirs="가져오는 커밋",
    ),
    RepoOperation.REVERT: ConflictLabels(
        ours="현재 브랜치",
        theirs="되돌린 결과",
    ),
}

_DEFAULT_LABELS = ConflictLabels(ours="내 것", theirs="상대 것")


def conflict_labels(operation: RepoOperation) -> ConflictLabels:
    """진행 중인 연산에 맞는 충돌 양쪽의 이름.

    모르는 상태(stash pop처럼 `state()`가 NONE인 충돌 포함)에서는 중립적인
    기본값을 쓴다. 틀린 이름보다 밋밋한 이름이 낫다.
    """
    return _CONFLICT_LABELS.get(operation, _DEFAULT_LABELS)


@dataclass(frozen=True, slots=True)
class OperationState:
    """저장소에 진행 중인 연산의 현재 모습.

    화면 위쪽 배너가 그리는 재료다. 사용자가 "지금 무슨 상태이고 어디서
    빠져나가는가"에 답할 수 있어야 한다 — 그 답이 없으면 충돌 화면은
    막다른 길이다.
    """

    operation: RepoOperation = RepoOperation.NONE
    step: int | None = None
    """rebase의 현재 커밋 순번 (1부터). 알 수 없으면 None."""

    total: int | None = None
    """rebase가 재생할 커밋 총 개수. 알 수 없으면 None."""

    head_branch: str | None = None
    """연산이 끝나면 돌아갈 브랜치. rebase 중에는 HEAD가 분리돼 있다."""

    conflict_count: int = 0

    @property
    def is_active(self) -> bool:
        return self.operation.is_active

    @property
    def has_conflicts(self) -> bool:
        return self.conflict_count > 0

    @property
    def labels(self) -> ConflictLabels:
        return conflict_labels(self.operation)

    @property
    def progress_text(self) -> str:
        """"1/3" 형태. 알 수 없으면 빈 문자열."""
        if self.step is None or self.total is None or self.total <= 1:
            return ""
        return f"{self.step}/{self.total}"

    def summary(self) -> str:
        """배너 한 줄."""
        if not self.is_active:
            return ""
        parts = [self.operation.label]
        progress = self.progress_text
        if progress:
            parts.append(progress)
        if self.head_branch:
            parts.append(f"→ {self.head_branch}")
        head = " ".join(parts)
        if self.has_conflicts:
            return f"{head} · 충돌 {self.conflict_count}개"
        if not self.operation.can_abort:
            # 우리가 손댈 수 없는 상태다. "진행 중"만 적으면 사용자는 앱에서
            # 끝낼 방법을 찾다가 시간을 버린다 — 어디로 가야 하는지 말한다.
            return f"{head} · 앱에서 다룰 수 없는 상태입니다 (git CLI에서 정리)"
        return f"{head} · 진행 중"


class HistoryOutcomeKind(Enum):
    """히스토리 연산 하나가 어떻게 끝났는가."""

    COMPLETED = "completed"
    """끝까지 갔다. 진행 중 상태가 남지 않는다."""

    CONFLICTED = "conflicted"
    """충돌로 멈췄다. **실패가 아니다** — 사람이 이어받을 차례다.

    충돌 목록이 비어 있는 채로 이 값이 오는 경우는 없다. 남길 변경이 없어
    멈춘 상태는 엔진이 조치를 실은 오류로 바꾼다 — 화면이 "충돌 0개를
    해결해야 합니다"라는 말이 안 되는 안내를 띄우지 않도록.
    """

    # `NOTHING_TO_DO`가 한때 여기 있었다. **아무도 만들지 않았다** —
    # 빈 결과는 전부 오류 경로로 가고 "이미 최신"은 `MergeKind`가 답한다.
    # 쓰이지 않는 값은 다음 사람이 "이 경우도 처리했구나"로 잘못 읽는다.


@dataclass(frozen=True, slots=True)
class HistoryOutcome:
    """rebase·cherry-pick·revert 시도의 결과.

    `MergeOutcome`과 같은 이유로 충돌을 예외가 아니라 값으로 돌려준다.
    """

    kind: HistoryOutcomeKind
    operation: RepoOperation = RepoOperation.NONE
    conflicts: tuple[ConflictedFile, ...] = ()
    message: str = ""
    """사용자에게 그대로 보여줄 수 있는 git의 설명. 없으면 빈 문자열."""

    @property
    def is_conflicted(self) -> bool:
        return self.kind is HistoryOutcomeKind.CONFLICTED
