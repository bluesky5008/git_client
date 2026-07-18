"""pygit2 기반 로컬 Git 엔진.

로컬 읽기 경로 전담이다. 네트워크 작업은 Phase 3에서 별도의 git CLI 엔진이 맡는다.
(doc/design.md §2.3 하이브리드 엔진)

pygit2 타입은 이 모듈 밖으로 새어나가지 않는다. 바깥에는 domain.models의
순수 파이썬 객체만 전달한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pygit2
from pygit2.enums import SortMode

from gitclient.domain.errors import RepositoryNotFoundError, RepositoryOpenError
from gitclient.domain.models import (
    ChangeStatus,
    Commit,
    CommitDetail,
    DiffLine,
    DiffLineKind,
    FileChange,
    Ref,
    RefKind,
    RepositoryInfo,
    Signature,
)

# pygit2는 존재하지 않는 줄 번호를 -1로 준다. 도메인 모델에서는 None으로 바꾼다.
_NO_LINE = -1

_STATUS_MAP = {
    "A": ChangeStatus.ADDED,
    "M": ChangeStatus.MODIFIED,
    "D": ChangeStatus.DELETED,
    "R": ChangeStatus.RENAMED,
    "C": ChangeStatus.COPIED,
    "T": ChangeStatus.TYPECHANGE,
}

_ORIGIN_MAP = {
    "+": DiffLineKind.ADDITION,
    "-": DiffLineKind.DELETION,
    " ": DiffLineKind.CONTEXT,
}


def _to_datetime(signature: pygit2.Signature) -> datetime:
    """pygit2의 (epoch, offset minutes)를 시간대가 붙은 datetime으로 바꾼다."""
    tz = timezone(timedelta(minutes=signature.offset))
    return datetime.fromtimestamp(signature.time, tz)


def _to_signature(signature: pygit2.Signature) -> Signature:
    return Signature(
        name=signature.name,
        email=signature.email,
        when=_to_datetime(signature),
    )


def _lineno(value: int) -> int | None:
    return None if value == _NO_LINE else value


class LocalGitEngine:
    """열려 있는 저장소 하나에 대한 읽기 연산을 제공한다."""

    def __init__(self, repo: pygit2.Repository) -> None:
        self._repo = repo

    @classmethod
    def open(cls, path: str | Path) -> LocalGitEngine:
        """경로에서 저장소를 찾아 연다. 하위 디렉터리를 줘도 위로 올라가며 찾는다."""
        target = Path(path).expanduser()
        if not target.exists():
            raise RepositoryNotFoundError(
                f"경로를 찾을 수 없습니다: {target}",
            )

        discovered = pygit2.discover_repository(str(target))
        if discovered is None:
            raise RepositoryNotFoundError(
                f"Git 저장소가 아닙니다: {target}",
                detail="이 경로와 상위 디렉터리에서 .git을 찾지 못했습니다.",
            )

        try:
            return cls(pygit2.Repository(discovered))
        except pygit2.GitError as exc:
            raise RepositoryOpenError(
                f"저장소를 열지 못했습니다: {target}",
                detail=str(exc),
            ) from exc

    # ------------------------------------------------------------------
    # 저장소 정보
    # ------------------------------------------------------------------

    def info(self) -> RepositoryInfo:
        repo = self._repo
        head_shorthand = None
        if not repo.head_is_unborn:
            head_shorthand = repo.head.shorthand

        return RepositoryInfo(
            path=repo.path,
            workdir=repo.workdir,
            head_shorthand=head_shorthand,
            is_empty=repo.is_empty,
            is_bare=repo.is_bare,
            is_shallow=repo.is_shallow,
            refs=self.refs(),
        )

    def refs(self) -> list[Ref]:
        """로컬/원격 브랜치와 태그를 모은다."""
        repo = self._repo
        result: list[Ref] = []

        for name in repo.branches.local:
            branch = repo.branches.local[name]
            if branch.target is None:
                continue
            result.append(
                Ref(
                    name=branch.name,
                    shorthand=branch.shorthand,
                    kind=RefKind.LOCAL_BRANCH,
                    target_sha=str(branch.target),
                    is_head=branch.is_head(),
                )
            )

        for name in repo.branches.remote:
            branch = repo.branches.remote[name]
            if branch.target is None:
                continue
            result.append(
                Ref(
                    name=branch.name,
                    shorthand=branch.shorthand,
                    kind=RefKind.REMOTE_BRANCH,
                    target_sha=str(branch.target),
                )
            )

        for ref_name in repo.references:
            if not ref_name.startswith("refs/tags/"):
                continue
            target = self._peel_to_commit_sha(ref_name)
            if target is None:
                continue
            result.append(
                Ref(
                    name=ref_name,
                    shorthand=ref_name.removeprefix("refs/tags/"),
                    kind=RefKind.TAG,
                    target_sha=target,
                )
            )

        return result

    def _peel_to_commit_sha(self, ref_name: str) -> str | None:
        """참조가 가리키는 커밋을 찾는다. 어노테이트 태그는 한 겹 벗겨야 한다."""
        try:
            ref = self._repo.references[ref_name]
            return str(ref.peel(pygit2.Commit).id)
        except (KeyError, pygit2.GitError, pygit2.InvalidSpecError):
            return None

    # ------------------------------------------------------------------
    # 커밋 순회
    # ------------------------------------------------------------------

    def iter_commits(self, *, limit: int | None = None) -> Iterator[Commit]:
        """모든 브랜치 끝점에서 출발해 커밋을 시간 역순으로 순회한다.

        제너레이터이므로 호출자가 필요한 만큼만 소비할 수 있다.
        뷰포트 단위 청크 로딩이 이 위에서 동작한다. (doc/design.md §4.1)
        """
        repo = self._repo
        if repo.is_empty or repo.head_is_unborn:
            return

        tips = self._collect_tips()
        if not tips:
            return

        walker = repo.walk(tips[0], SortMode.TOPOLOGICAL | SortMode.TIME)
        for tip in tips[1:]:
            walker.push(tip)

        for index, commit in enumerate(walker):
            if limit is not None and index >= limit:
                return
            yield self._to_commit(commit)

    def _collect_tips(self) -> list[pygit2.Oid]:
        """순회 시작점 목록. HEAD를 맨 앞에 두어 현재 브랜치가 우선 보이게 한다."""
        repo = self._repo
        tips: list[pygit2.Oid] = []
        seen: set[str] = set()

        def add(oid: pygit2.Oid | None) -> None:
            if oid is None:
                return
            key = str(oid)
            if key in seen:
                return
            seen.add(key)
            tips.append(oid)

        if not repo.head_is_unborn:
            add(repo.head.target)

        for collection in (repo.branches.local, repo.branches.remote):
            for name in collection:
                branch = collection[name]
                add(branch.target)

        return tips

    def _to_commit(self, commit: pygit2.Commit) -> Commit:
        return Commit(
            sha=str(commit.id),
            parents=tuple(str(p) for p in commit.parent_ids),
            author=_to_signature(commit.author),
            committer=_to_signature(commit.committer),
            message=commit.message,
        )

    # ------------------------------------------------------------------
    # 커밋 상세 및 diff
    # ------------------------------------------------------------------

    def commit_detail(self, sha: str) -> CommitDetail:
        """커밋 하나와 그 커밋이 바꾼 파일 목록."""
        commit = self._lookup_commit(sha)
        diff = self._diff_for(commit)

        changes: list[FileChange] = []
        for patch in diff:
            delta = patch.delta
            _, additions, deletions = patch.line_stats
            status = _STATUS_MAP.get(delta.status_char(), ChangeStatus.UNKNOWN)
            changes.append(
                FileChange(
                    path=delta.new_file.path,
                    status=status,
                    old_path=(
                        delta.old_file.path
                        if status is ChangeStatus.RENAMED
                        else None
                    ),
                    insertions=additions,
                    deletions=deletions,
                )
            )

        return CommitDetail(commit=self._to_commit(commit), changes=tuple(changes))

    def diff_lines(self, sha: str, path: str | None = None) -> list[DiffLine]:
        """diff 뷰가 그릴 줄 목록.

        `path`를 주면 해당 파일만, 주지 않으면 커밋 전체의 diff를 반환한다.
        """
        commit = self._lookup_commit(sha)
        diff = self._diff_for(commit)

        lines: list[DiffLine] = []
        for patch in diff:
            delta = patch.delta
            if path is not None and delta.new_file.path != path:
                continue

            lines.append(
                DiffLine(
                    kind=DiffLineKind.FILE_HEADER,
                    text=self._file_header(patch),
                )
            )

            if delta.is_binary:
                # 바이너리는 내용 diff를 낼 수 없다. 사실을 알리고 넘어간다.
                lines.append(
                    DiffLine(
                        kind=DiffLineKind.CONTEXT,
                        text="(바이너리 파일 — 내용 diff를 표시할 수 없습니다)",
                    )
                )
                continue

            for hunk in patch.hunks:
                lines.append(
                    DiffLine(
                        kind=DiffLineKind.HUNK_HEADER,
                        text=hunk.header.rstrip("\n"),
                    )
                )
                for line in hunk.lines:
                    lines.append(
                        DiffLine(
                            kind=_ORIGIN_MAP.get(line.origin, DiffLineKind.CONTEXT),
                            text=line.content.rstrip("\n"),
                            old_lineno=_lineno(line.old_lineno),
                            new_lineno=_lineno(line.new_lineno),
                        )
                    )

        return lines

    def _file_header(self, patch: pygit2.Patch) -> str:
        delta = patch.delta
        status = _STATUS_MAP.get(delta.status_char(), ChangeStatus.UNKNOWN)
        if status is ChangeStatus.RENAMED:
            return f"{delta.old_file.path} -> {delta.new_file.path}"
        return delta.new_file.path

    def _diff_for(self, commit: pygit2.Commit) -> pygit2.Diff:
        """커밋의 diff. 머지 커밋은 첫 부모와 비교한다.

        머지 커밋을 모든 부모와 비교하면 대부분 잡음이 된다. git의 기본 동작과
        동일하게 첫 부모 기준으로 보여준다.
        """
        if commit.parents:
            diff = self._repo.diff(commit.parents[0], commit)
        else:
            # 루트 커밋은 빈 트리와 비교한다.
            diff = commit.tree.diff_to_tree(swap=True)

        diff.find_similar()  # 이름 변경 탐지
        return diff

    def _lookup_commit(self, sha: str) -> pygit2.Commit:
        try:
            obj = self._repo[sha]
        except (KeyError, ValueError) as exc:
            raise RepositoryOpenError(
                f"커밋을 찾을 수 없습니다: {sha[:12]}",
                detail=str(exc),
            ) from exc

        if not isinstance(obj, pygit2.Commit):
            raise RepositoryOpenError(f"커밋이 아닙니다: {sha[:12]}")
        return obj
