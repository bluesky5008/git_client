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
