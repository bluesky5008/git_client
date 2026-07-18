"""pygit2 기반 로컬 Git 엔진.

로컬 읽기 경로 전담이다. 네트워크 작업은 Phase 3에서 별도의 git CLI 엔진이 맡는다.
(doc/design.md §2.3 하이브리드 엔진)

pygit2 타입은 이 모듈 밖으로 새어나가지 않는다. 바깥에는 domain.models의
순수 파이썬 객체만 전달한다. **예외도 마찬가지다** — raw pygit2 예외가
이 층 밖으로 새면 UI의 `except GitClientError`가 잡지 못해 Qt 이벤트 루프까지
올라간다. pygit2를 만지는 공개 메서드는 `_translate()`로 경계를 세운다.
(doc/design.md §7)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pygit2
from pygit2.enums import SortMode

from gitclient.domain.errors import (
    EngineError,
    GitClientError,
    RepositoryNotFoundError,
    RepositoryOpenError,
)
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

logger = logging.getLogger(__name__)

# pygit2는 존재하지 않는 줄 번호를 -1로 준다. 도메인 모델에서는 None으로 바꾼다.
_NO_LINE = -1


@contextmanager
def _translate(context: str):
    """pygit2 예외를 도메인 예외로 변환하는 경계.

    이미 도메인 예외인 것은 그대로 통과시킨다. KeyError를 포함하는 이유는
    pygit2가 존재하지 않는 오브젝트 조회를 KeyError로 던지기 때문이다.
    """
    try:
        yield
    except GitClientError:
        raise
    except (pygit2.GitError, pygit2.InvalidSpecError, KeyError, ValueError) as exc:
        raise EngineError(
            f"{context} 중 Git 엔진 오류가 발생했습니다.",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

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


def _branch_oid(branch: pygit2.Branch) -> pygit2.Oid | None:
    """브랜치가 가리키는 커밋 Oid. 심볼릭 참조는 None.

    clone된 저장소에는 반드시 `origin/HEAD`가 있고, 이것의 target은 Oid가
    아니라 문자열("refs/remotes/origin/main")이다. 이를 Oid로 취급하면
    walker.push()가 죽는다 — 합성 fixture에는 remote가 없어 테스트가 오래
    놓쳤던, 실제 저장소에서는 100% 재현되는 결함이었다.
    """
    target = branch.target
    if isinstance(target, pygit2.Oid):
        return target
    return None


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
                action="경로가 삭제되었거나 이동했을 수 있습니다. 다른 폴더를 선택해 주세요.",
            )

        discovered = pygit2.discover_repository(str(target))
        if discovered is None:
            raise RepositoryNotFoundError(
                f"Git 저장소가 아닙니다: {target}",
                detail="이 경로와 상위 디렉터리에서 .git을 찾지 못했습니다.",
                action="저장소 루트 또는 그 하위 폴더를 선택해 주세요. "
                "새 저장소가 필요하면 git init으로 만들 수 있습니다.",
            )

        try:
            return cls(pygit2.Repository(discovered))
        except pygit2.GitError as exc:
            raise RepositoryOpenError(
                f"저장소를 열지 못했습니다: {target}",
                detail=str(exc),
                action="저장소가 손상되었을 수 있습니다. git fsck로 상태를 확인해 보세요.",
            ) from exc

    # ------------------------------------------------------------------
    # 저장소 정보
    # ------------------------------------------------------------------

    def info(self, *, include_refs: bool = True) -> RepositoryInfo:
        """저장소 요약 정보.

        `include_refs=False`면 ref 열거를 건너뛴다. ref 열거는 ref 수에 비례해
        느려지므로(실측 ref당 ~1.3ms) UI 스레드에서는 lite 버전을 쓰고
        refs는 워커(RefsLoader)가 따로 가져온다. (doc/design.md §3.3)
        """
        with _translate("저장소 정보 조회"):
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
                refs=self.refs() if include_refs else [],
            )

    def refs(self) -> list[Ref]:
        """로컬/원격 브랜치와 태그를 모은다.

        ref 수에 비례하는 비용이 든다 — UI 스레드에서 부르지 말 것.
        """
        with _translate("참조 목록 조회"):
            return self._refs()

    def _refs(self) -> list[Ref]:
        repo = self._repo
        result: list[Ref] = []

        for name in repo.branches.local:
            branch = repo.branches.local[name]
            oid = _branch_oid(branch)
            if oid is None:
                continue
            result.append(
                Ref(
                    name=branch.name,
                    shorthand=branch.shorthand,
                    kind=RefKind.LOCAL_BRANCH,
                    target_sha=str(oid),
                    is_head=branch.is_head(),
                )
            )

        for name in repo.branches.remote:
            branch = repo.branches.remote[name]
            oid = _branch_oid(branch)
            if oid is None:
                # origin/HEAD 같은 심볼릭 별칭. 실체 브랜치가 따로 목록에
                # 들어가므로 표시하지 않는다. (GitKraken도 숨긴다)
                continue
            result.append(
                Ref(
                    name=branch.name,
                    shorthand=branch.shorthand,
                    kind=RefKind.REMOTE_BRANCH,
                    target_sha=str(oid),
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
        """참조가 가리키는 커밋을 찾는다. 어노테이트 태그는 한 겹 벗겨야 한다.

        커밋이 아닌 대상(blob 태그 등)이나 dangling 참조는 목록에서 제외하되
        반드시 로그를 남긴다 — 조용한 스킵은 디버깅이 불가능하다. (§7 ADR-13)
        """
        try:
            ref = self._repo.references[ref_name]
            return str(ref.peel(pygit2.Commit).id)
        except (KeyError, pygit2.GitError, pygit2.InvalidSpecError) as exc:
            logger.warning(
                "참조 %s 를 커밋으로 해석할 수 없어 목록에서 제외: %s", ref_name, exc
            )
            return None

    # ------------------------------------------------------------------
    # 커밋 순회
    # ------------------------------------------------------------------

    def iter_commits(self, *, limit: int | None = None) -> Iterator[Commit]:
        """모든 브랜치 끝점에서 출발해 커밋을 시간 역순으로 순회한다.

        제너레이터다. 소비 주체는 워커 스레드의 CommitLoader이며, 묶음 단위로
        UI에 밀어넣는다(push). 정렬된 순회는 비용의 대부분을 첫 커밋 이전에
        치르므로 뷰포트 기반 지연 로딩은 성립하지 않는다. (doc/design.md §4.1.1.1)
        """
        with _translate("커밋 순회 시작"):
            repo = self._repo
            if repo.is_empty or repo.head_is_unborn:
                return

            tips = self._collect_tips()
            if not tips:
                return

            walker = repo.walk(tips[0], SortMode.TOPOLOGICAL | SortMode.TIME)
            for tip in tips[1:]:
                walker.push(tip)

        index = 0
        while True:
            with _translate("커밋 순회"):
                commit = next(walker, None)
            if commit is None or (limit is not None and index >= limit):
                return
            yield self._to_commit(commit)
            index += 1

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
                add(_branch_oid(branch))  # 심볼릭(origin/HEAD)은 None → 제외

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
        with _translate("커밋 상세 조회"):
            return self._commit_detail(sha)

    def _commit_detail(self, sha: str) -> CommitDetail:
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
        with _translate("diff 계산"):
            return self._diff_lines(sha, path)

    def _diff_lines(self, sha: str, path: str | None) -> list[DiffLine]:
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
        except (KeyError, ValueError, pygit2.GitError) as exc:
            # 초안은 RepositoryOpenError를 던졌으나 이는 오라벨이다 —
            # 저장소는 열려 있고 특정 오브젝트 조회가 실패한 것이므로 EngineError.
            raise EngineError(
                f"커밋을 찾을 수 없습니다: {sha[:12]}",
                detail=str(exc),
                action="저장소가 외부에서 변경되었을 수 있습니다. 새로 고침(F5) 해보세요.",
            ) from exc

        if not isinstance(obj, pygit2.Commit):
            raise EngineError(f"커밋이 아닙니다: {sha[:12]}")
        return obj
