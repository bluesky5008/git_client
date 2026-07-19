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
from pygit2.enums import (
    ApplyLocation,
    CheckoutStrategy,
    DeltaStatus,
    DiffOption,
    FileStatus,
    SortMode,
)

from gitclient.domain.errors import (
    EngineError,
    GitClientError,
    RepositoryNotFoundError,
    RepositoryOpenError,
)
from gitclient.domain.patch import (
    FilePatch,
    PatchError,
    PatchHunk,
    PatchLine,
    synthesize_patch,
)
from gitclient.domain.models import (
    ChangeStatus,
    Commit,
    CommitDetail,
    DiffLine,
    DiffLineKind,
    FileChange,
    MergeKind,
    MergePreview,
    Ref,
    RefKind,
    RepositoryInfo,
    Signature,
    WorkAreaStatus,
    WorkingFileChange,
    WorkingTreeStatus,
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


def _peel_to_commit_id(reference: pygit2.Reference) -> pygit2.Oid:
    """참조가 최종적으로 가리키는 커밋 Oid.

    심볼릭 참조(`origin/HEAD`)와 어노테이트 태그를 모두 벗겨낸다 —
    `_branch_oid`가 막았던 것과 같은 함정이다.
    """
    resolved = reference.resolve()
    return resolved.peel(pygit2.Commit).id


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
                is_partial=self._is_partial(repo),
                refs=self.refs() if include_refs else [],
                # 원격 목록은 설정 파일 한 번 읽기라 lite 정보에도 포함한다.
                remotes=[remote.name for remote in repo.remotes],
            )

    @staticmethod
    def _is_partial(repo: pygit2.Repository) -> bool:
        """부분 복제 저장소인가.

        표식은 `remote.<이름>.promisor`다 — 그 원격이 지연된 객체를 나중에
        공급해 준다는 뜻이다.

        (`extensions.partialClone`을 먼저 봤는데 실측한 git 2.42는 그 키를
        쓰지 않았다. 추측 대신 실제 설정을 확인하고 골랐다.)
        """
        # try를 **원격 하나마다** 건다. 루프 전체를 감싸면 promisor가 없는
        # 첫 원격이 KeyError를 던지면서 순회가 끝나버려, 원격이 둘 이상일 때
        # 부분 복제를 놓친다(pygit2는 없는 키에 KeyError를 던진다).
        for remote in repo.remotes:
            try:
                value = repo.config[f"remote.{remote.name}.promisor"]
            except (KeyError, ValueError, AttributeError, pygit2.GitError):
                continue
            if str(value).strip().lower() in ("true", "1", "yes"):
                return True
        return False

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
            if path is not None and patch.delta.new_file.path != path:
                continue
            self._append_patch_lines(lines, patch)
        return lines

    def _append_patch_lines(
        self, lines: list[DiffLine], patch: pygit2.Patch
    ) -> None:
        """패치 하나를 DiffLine 목록으로 풀어 붙인다."""
        lines.append(
            DiffLine(
                kind=DiffLineKind.FILE_HEADER,
                text=self._file_header(patch),
            )
        )

        if patch.delta.is_binary:
            # 바이너리는 내용 diff를 낼 수 없다. 사실을 알리고 넘어간다.
            lines.append(
                DiffLine(
                    kind=DiffLineKind.CONTEXT,
                    text="(바이너리 파일 — 내용 diff를 표시할 수 없습니다)",
                )
            )
            return

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

    # ------------------------------------------------------------------
    # 작업 디렉터리 상태 (Phase 2)
    # ------------------------------------------------------------------

    # (인덱스 플래그, 워킹트리 플래그) → 도메인 상태
    _INDEX_FLAGS = (
        (FileStatus.INDEX_NEW, WorkAreaStatus.NEW),
        (FileStatus.INDEX_MODIFIED, WorkAreaStatus.MODIFIED),
        (FileStatus.INDEX_DELETED, WorkAreaStatus.DELETED),
        (FileStatus.INDEX_RENAMED, WorkAreaStatus.RENAMED),
    )
    _WT_FLAGS = (
        (FileStatus.WT_NEW, WorkAreaStatus.NEW),
        (FileStatus.WT_MODIFIED, WorkAreaStatus.MODIFIED),
        (FileStatus.WT_DELETED, WorkAreaStatus.DELETED),
        (FileStatus.WT_RENAMED, WorkAreaStatus.RENAMED),
    )

    def working_tree_status(self) -> WorkingTreeStatus:
        """스테이징/미스테이징 변경 목록.

        워킹 트리 전체를 스캔하므로 파일 수에 비례한다 — UI 스레드 금지.
        """
        with _translate("작업 디렉터리 상태 조회"):
            status = self._repo.status(untracked_files="all", ignored=False)

        staged: list[WorkingFileChange] = []
        unstaged: list[WorkingFileChange] = []

        for path, flags in sorted(status.items()):
            if flags & FileStatus.CONFLICTED:
                unstaged.append(
                    WorkingFileChange(path, WorkAreaStatus.CONFLICTED, staged=False)
                )
                continue
            for flag, mapped in self._INDEX_FLAGS:
                if flags & flag:
                    staged.append(WorkingFileChange(path, mapped, staged=True))
                    break
            for flag, mapped in self._WT_FLAGS:
                if flags & flag:
                    unstaged.append(WorkingFileChange(path, mapped, staged=False))
                    break

        return WorkingTreeStatus(staged=tuple(staged), unstaged=tuple(unstaged))

    def head_message(self) -> str | None:
        """HEAD 커밋의 메시지. amend 시 메시지 프리필에 쓴다. O(1)."""
        with _translate("HEAD 메시지 조회"):
            if self._repo.head_is_unborn:
                return None
            return self._repo[self._repo.head.target].message

    def workdir_diff_lines(self, path: str, *, staged: bool) -> list[DiffLine]:
        """커밋되지 않은 변경의 diff.

        staged=True 면 HEAD↔인덱스, False 면 인덱스↔워킹트리를 비교한다.

        비교 대상 선택은 `_workdir_diff`가 담당한다 — 부분 스테이징
        (`file_patch`)과 같은 diff를 봐야 화면과 적용 대상이 어긋나지 않는다.
        """
        with _translate("작업 디렉터리 diff 계산"):
            diff = self._workdir_diff(staged=staged)

            lines: list[DiffLine] = []
            for patch in diff:
                if patch.delta.new_file.path != path and (
                    patch.delta.old_file.path != path
                ):
                    continue
                self._append_patch_lines(lines, patch)
            return lines

    # ------------------------------------------------------------------
    # 부분 스테이징 (Phase 2 증분 2)
    # ------------------------------------------------------------------

    def file_patch(self, path: str, *, staged: bool) -> FilePatch:
        """파일 하나의 패치를 도메인 모델로 읽는다.

        표시용 `DiffLine`과 달리 줄 원문을 가공 없이 보존한다 —
        패치 합성에는 줄바꿈과 공백이 그대로 필요하기 때문이다.
        """
        with _translate("패치 읽기"):
            diff = self._workdir_diff(staged=staged)
            for patch in diff:
                if path not in (patch.delta.new_file.path, patch.delta.old_file.path):
                    continue
                return self._to_file_patch(patch)
        raise EngineError(
            f"변경 내용을 찾을 수 없습니다: {path}",
            action="파일이 외부에서 바뀌었을 수 있습니다. 새로 고침(F5) 후 다시 시도해 주세요.",
        )

    def _to_file_patch(self, patch: pygit2.Patch) -> FilePatch:
        delta = patch.delta
        hunks: list[PatchHunk] = []

        if not delta.is_binary:
            for hunk in patch.hunks:
                lines = tuple(
                    PatchLine(
                        origin=line.origin,
                        content=line.content,  # 원문 그대로 — strip 금지
                        old_lineno=_lineno(line.old_lineno),
                        new_lineno=_lineno(line.new_lineno),
                    )
                    for line in hunk.lines
                )
                hunks.append(
                    PatchHunk(
                        old_start=hunk.old_start,
                        old_lines=hunk.old_lines,
                        new_start=hunk.new_start,
                        new_lines=hunk.new_lines,
                        lines=lines,
                    )
                )

        return FilePatch(
            old_path=delta.old_file.path,
            new_path=delta.new_file.path,
            hunks=tuple(hunks),
            is_new=delta.status == DeltaStatus.ADDED
            or delta.status == DeltaStatus.UNTRACKED,
            is_deleted=delta.status == DeltaStatus.DELETED,
            is_binary=delta.is_binary,
            old_mode=f"{delta.old_file.mode:06o}",
            new_mode=f"{delta.new_file.mode:06o}",
        )

    def stage_partial(
        self, path: str, selected: set[tuple[int, int]] | None = None
    ) -> None:
        """선택한 헝크/줄만 스테이징한다.

        `selected`가 None이면 파일의 모든 변경을 스테이징한다(헝크 전체 선택).
        워킹 트리는 건드리지 않는다 — 인덱스에만 적용된다.
        """
        patch = self.file_patch(path, staged=False)
        self._apply_synthesized(patch, selected, reverse=False, action="스테이징")

    def unstage_partial(
        self, path: str, selected: set[tuple[int, int]] | None = None
    ) -> None:
        """선택한 헝크/줄만 스테이징 해제한다.

        스테이징된 diff(HEAD↔인덱스)를 뒤집어 인덱스에 적용한다.
        """
        patch = self.file_patch(path, staged=True)
        self._apply_synthesized(patch, selected, reverse=True, action="스테이징 취소")

    def _apply_synthesized(
        self,
        patch: FilePatch,
        selected: set[tuple[int, int]] | None,
        *,
        reverse: bool,
        action: str,
    ) -> None:
        try:
            patch_text = synthesize_patch(patch, selected, reverse=reverse)
        except PatchError as exc:
            raise EngineError(str(exc)) from exc

        index_path = patch.new_path if reverse else patch.old_path
        previous_mode = self._index_mode(index_path)

        with _translate(f"부분 {action}"):
            try:
                parsed = pygit2.Diff.parse_diff(patch_text)
                self._repo.apply(parsed, location=ApplyLocation.INDEX)
                self._restore_mode(index_path, previous_mode)
            except pygit2.GitError as exc:
                # 합성이 틀리면 libgit2가 여기서 거부한다. 인덱스는 그대로다.
                raise EngineError(
                    f"부분 {action}에 실패했습니다.",
                    detail=f"{exc}\n\n--- 생성된 패치 ---\n{patch_text}",
                    action="파일이 그 사이 변경되었을 수 있습니다. "
                    "새로 고침(F5) 후 다시 시도해 주세요.",
                ) from exc

    def _index_mode(self, path: str) -> int | None:
        index = self._repo.index
        try:
            return index[path].mode
        except KeyError:
            return None

    def _restore_mode(self, path: str, previous_mode: int | None) -> None:
        """부분 적용 후 파일 모드를 되돌린다.

        libgit2의 `apply(INDEX)`는 실행 비트를 보존하지 않아 100755 파일이
        100644로 내려앉는다(실측 확인). 패치 헤더로는 고칠 수 없다 —
        `old mode`/`new mode` 줄을 넣으면 `invalid patch header`로 거부된다.
        부분 스테이징은 내용만 다루므로, 적용 전 모드를 그대로 복원한다.
        """
        if previous_mode is None:
            return
        index = self._repo.index
        try:
            entry = index[path]
        except KeyError:
            return  # 삭제가 적용된 경우 — 복원할 대상이 없다
        if entry.mode == previous_mode:
            return
        index.add(pygit2.IndexEntry(path, entry.id, previous_mode))
        index.write()

    def _workdir_diff(self, *, staged: bool) -> pygit2.Diff:
        """스테이징/미스테이징 diff. 부분 스테이징과 표시가 공유한다."""
        if staged:
            if self._repo.head_is_unborn:
                empty_tree = self._repo[self._repo.TreeBuilder().write()]
                diff = self._repo.diff(empty_tree, cached=True)
            else:
                diff = self._repo.diff("HEAD", cached=True)
        else:
            diff = self._repo.diff(
                flags=(
                    DiffOption.INCLUDE_UNTRACKED
                    | DiffOption.SHOW_UNTRACKED_CONTENT
                    | DiffOption.RECURSE_UNTRACKED_DIRS
                )
            )
        diff.find_similar()
        return diff

    # ------------------------------------------------------------------
    # 쓰기 연산 (Phase 2)
    #
    # 호출 규약: 반드시 WriteQueue를 통해 직렬로 실행할 것 (§3.3 규칙 3).
    # 같은 저장소에 쓰기 두 개가 동시에 들어가면 인덱스가 깨진다.
    # ------------------------------------------------------------------

    def stage_file(self, path: str) -> None:
        """파일 하나를 스테이징한다. 삭제된 파일이면 삭제를 스테이징한다."""
        with _translate("스테이징"):
            index = self._repo.index
            workdir = Path(self._repo.workdir or "")
            if (workdir / path).exists():
                index.add(path)
            else:
                index.remove(path)
            index.write()

    def unstage_file(self, path: str) -> None:
        """스테이징을 취소한다 — 인덱스 엔트리를 HEAD 상태로 되돌린다."""
        with _translate("스테이징 취소"):
            index = self._repo.index
            head_tree = None
            if not self._repo.head_is_unborn:
                head_tree = self._repo.head.peel(pygit2.Tree)

            if head_tree is not None and path in head_tree:
                entry = head_tree / path
                index.add(pygit2.IndexEntry(path, entry.id, entry.filemode))
            else:
                # HEAD에 없던 파일(INDEX_NEW)은 인덱스에서 빼는 것이 취소다.
                index.remove(path)
            index.write()

    def discard_file(self, path: str) -> None:
        """워킹 트리 변경을 버린다. 되돌릴 수 없다 — UI가 반드시 확인을 받을 것.

        복원 기준은 HEAD가 아니라 **인덱스**다. 사용자가 버리겠다고 확인한 것은
        미스테이징 변경뿐이며, 스테이징해 둔 내용은 그 범위 밖이다.
        (초기 구현의 checkout_head는 인덱스까지 HEAD로 되돌려 스테이징된
        변경을 함께 파괴했다 — 리뷰에서 확정된 결함. checkout_index는
        unborn HEAD에서도 동작한다.)

        추적되지 않은 새 파일은 인덱스에 없으므로 직접 지운다.
        """
        with _translate("변경 사항 버리기"):
            flags = self._repo.status_file(path)
            if flags & FileStatus.WT_NEW:
                target = Path(self._repo.workdir or "") / path
                target.unlink(missing_ok=True)
                return
            self._repo.checkout_index(
                paths=[path], strategy=CheckoutStrategy.FORCE
            )

    def create_commit(self, message: str, *, amend: bool = False) -> str:
        """스테이징된 내용으로 커밋한다. 새 커밋의 SHA를 반환한다.

        작성자는 git 설정(user.name/user.email)에서 온다 — 설정이 없으면
        어떤 서명으로 커밋할지 애플리케이션이 지어낼 수 없으므로 오류다.
        """
        if not message.strip():
            raise EngineError(
                "커밋 메시지가 비어 있습니다.",
                action="변경 내용을 설명하는 메시지를 입력해 주세요.",
            )

        try:
            signature = self._repo.default_signature
        except (KeyError, pygit2.GitError) as exc:
            raise EngineError(
                "커밋 작성자 정보가 설정되어 있지 않습니다.",
                detail=str(exc),
                action=(
                    "git config --global user.name \"이름\" 과 "
                    "git config --global user.email \"메일\" 을 설정해 주세요."
                ),
            ) from exc

        with _translate("커밋 생성"):
            index = self._repo.index
            tree = index.write_tree()
            merge_head = self._merge_head()

            if amend:
                if self._repo.head_is_unborn:
                    raise EngineError(
                        "수정할 커밋이 없습니다.",
                        action="첫 커밋은 amend 없이 만들어 주세요.",
                    )
                if merge_head is not None:
                    raise EngineError(
                        "머지 진행 중에는 커밋 수정(amend)을 할 수 없습니다.",
                        action="머지 커밋을 먼저 완성해 주세요.",
                    )
                head_commit = self._repo[self._repo.head.target]
                new_oid = self._repo.amend_commit(
                    head_commit,
                    "HEAD",
                    message=message,
                    tree=tree,
                    committer=signature,
                )
                return str(new_oid)

            # 스테이징된 것이 없는 커밋은 거부한다 — 커밋 버튼 연타로 같은
            # 메시지의 빈 커밋이 쌓이는 것을 막는다 (리뷰에서 확정된 결함).
            # 단, 머지 커밋은 트리가 HEAD와 같아도 유효하다(ours 전략 등).
            if merge_head is None:
                if self._repo.head_is_unborn:
                    if len(index) == 0:
                        raise EngineError(
                            "스테이징된 변경이 없습니다.",
                            action="커밋할 파일을 먼저 스테이징해 주세요.",
                        )
                elif tree == self._repo.head.peel(pygit2.Tree).id:
                    raise EngineError(
                        "스테이징된 변경이 없습니다.",
                        action="커밋할 파일을 먼저 스테이징해 주세요.",
                    )

            parents = (
                [] if self._repo.head_is_unborn else [self._repo.head.target]
            )
            if merge_head is not None:
                # 머지 진행 중의 커밋은 머지 커밋이다 — MERGE_HEAD가 두 번째
                # 부모가 되어야 한다. 빠뜨리면 머지가 일반 커밋으로 둔갑해
                # 히스토리가 손상된다 (리뷰에서 확정된 결함).
                parents.append(merge_head)

            new_oid = self._repo.create_commit(
                "HEAD", signature, signature, message, tree, parents
            )
            if merge_head is not None:
                self._repo.state_cleanup()  # MERGE_HEAD/MERGE_MSG 정리
            return str(new_oid)

    def _merge_head(self) -> pygit2.Oid | None:
        """머지 진행 중이면 MERGE_HEAD의 커밋 Oid."""
        try:
            return self._repo.lookup_reference("MERGE_HEAD").target
        except (KeyError, pygit2.GitError):
            return None

    # ------------------------------------------------------------------
    # 브랜치 연산 (Phase 2)
    # ------------------------------------------------------------------

    def create_branch(self, name: str, *, checkout: bool = False) -> None:
        """HEAD 커밋에서 브랜치를 만든다."""
        with _translate("브랜치 생성"):
            if self._repo.head_is_unborn:
                raise EngineError(
                    "커밋이 없어 브랜치를 만들 수 없습니다.",
                    action="첫 커밋을 만든 뒤 브랜치를 생성해 주세요.",
                )
            if name in self._repo.branches.local:
                raise EngineError(
                    f"브랜치가 이미 있습니다: {name}",
                    action="다른 이름을 사용해 주세요.",
                )
            head_commit = self._repo[self._repo.head.target]
            branch = self._repo.branches.local.create(name, head_commit)
            if checkout:
                self._repo.checkout(branch)

    def checkout_branch(self, name: str) -> None:
        """브랜치를 전환한다. 충돌하는 로컬 변경이 있으면 실패한다."""
        try:
            with _translate("브랜치 전환"):
                self._repo.checkout(self._repo.branches.local[name])
        except EngineError as exc:
            if "conflict" in (exc.detail or "").lower():
                raise EngineError(
                    f"로컬 변경과 충돌해 '{name}' 브랜치로 전환할 수 없습니다.",
                    detail=exc.detail,
                    action="변경 사항을 커밋하거나 stash에 보관한 뒤 다시 시도해 주세요.",
                ) from exc
            raise

    def delete_branch(self, name: str) -> None:
        """로컬 브랜치를 삭제한다. 현재 브랜치는 삭제할 수 없다."""
        with _translate("브랜치 삭제"):
            branch = self._repo.branches.local.get(name)
            if branch is None:
                raise EngineError(f"브랜치를 찾을 수 없습니다: {name}")
            if branch.is_head():
                raise EngineError(
                    "현재 작업 중인 브랜치는 삭제할 수 없습니다.",
                    action="다른 브랜치로 전환한 뒤 삭제해 주세요.",
                )
            branch.delete()

    # ------------------------------------------------------------------
    # 병합 (Phase 3 — pull의 로컬 절반)
    # ------------------------------------------------------------------

    def upstream_of_head(self) -> tuple[str, str] | None:
        """현재 브랜치가 따라가는 (원격 이름, 원격 추적 참조 전체 이름).

        `branch.<name>.remote` / `branch.<name>.merge`를 그대로 읽는다 —
        `<origin>/<브랜치명>` 규약으로 추측하면 안 된다. fork 워크플로처럼
        origin이 아닌 원격을 따라가는 브랜치에서 **엉뚱한 ref를 합치고도
        "이미 최신"이라 답하게 된다.**

        분리된 HEAD와 unborn HEAD에서는 None이다. 특히 분리된 HEAD에서
        pygit2는 `shorthand`로 짧은 sha가 아니라 문자열 `"HEAD"`를 주므로,
        규약으로 조합하면 `refs/remotes/<remote>/HEAD`가 만들어진다 —
        이 참조는 모든 clone에 존재하고 원격 기본 브랜치를 가리키므로,
        bisect 중인 사용자의 HEAD가 조용히 원격 기본 브랜치로 끌려간다.
        """
        with _translate("upstream 조회"):
            if self._repo.head_is_unborn or self._repo.head_is_detached:
                return None
            branch = self._repo.branches.local.get(self._repo.head.shorthand)
            upstream = branch.upstream if branch is not None else None
            if upstream is None:
                return None
            return upstream.remote_name, upstream.name

    def ahead_behind(self, branch: str, upstream_ref: str) -> tuple[int, int] | None:
        """(앞선 커밋 수, 뒤처진 커밋 수). 비교할 수 없으면 None.

        **로컬 질의라 여기(pygit2)에 있다.** 처음에는 `git rev-list --count`로
        RemoteEngine에 두었는데, 네트워크를 한 바이트도 쓰지 않으면서
        프로세스 생성 비용만 냈다 — 실측 19ms 대 pygit2 0.002ms. §2.3의
        엔진 경계(로컬은 pygit2, 네트워크는 CLI)에 어긋났고, UI 스레드에서
        불리므로 G4 예산 50ms의 40%를 이유 없이 태우고 있었다.

        ADR-2가 CLI를 정당화한 근거는 **전송 바이트를 줄이는 수단**
        (protocol v2, 필터, 협상 튜닝)인데 이 질의는 그중 어느 것도 쓰지 않는다.
        """
        with _translate("ahead/behind 계산"):
            local = self._repo.references.get(f"refs/heads/{branch}")
            upstream = self._repo.references.get(upstream_ref)
            if local is None or upstream is None:
                return None
            counts = self._repo.ahead_behind(
                _peel_to_commit_id(local), _peel_to_commit_id(upstream)
            )
            return int(counts[0]), int(counts[1])

    def merge_preview(self, upstream_ref: str) -> MergePreview:
        """합치기 전에 무엇이 필요한지 본다. 저장소를 바꾸지 않는다.

        pull은 네트워크(fetch)와 로컬 쓰기(병합)가 만나는 지점이다. 경계는
        ADR-2 그대로 유지한다 — 전송은 git CLI가, 병합은 pygit2가 맡는다.
        """
        with _translate("병합 분석"):
            reference = self._repo.references.get(upstream_ref)
            if reference is None:
                raise EngineError(
                    f"원격 추적 참조를 찾을 수 없습니다: {upstream_ref}",
                    action="먼저 가져오기(Fetch)를 실행해 주세요.",
                )
            target = _peel_to_commit_id(reference)
            analysis, _preference = self._repo.merge_analysis(target)

        flags = pygit2.enums.MergeAnalysis
        if analysis & flags.UP_TO_DATE:
            return MergePreview(MergeKind.UP_TO_DATE, str(target))
        if analysis & flags.FASTFORWARD:
            return MergePreview(MergeKind.FAST_FORWARD, str(target))
        return MergePreview(MergeKind.MERGE_REQUIRED, str(target))

    def fast_forward(self, upstream_ref: str, expected_branch: str | None = None) -> str:
        """현재 브랜치를 upstream으로 빨리 감는다. 새 커밋을 만들지 않는다.

        **순서가 중요하다.** 참조를 먼저 옮기고 checkout하면 libgit2가 이미
        같아진 HEAD와 비교하게 되어 **워킹트리를 갱신하지 않는다.** 실측에서
        수정이 반영되지 않고 삭제도 적용되지 않은 채 인덱스에 엉뚱한 변경이
        스테이징된 상태가 남았다 — 오류 없이.

        그래서 checkout이 먼저다. 이 순서는 커밋하지 않은 변경이 있을 때
        libgit2가 checkout을 거부하므로 **실패해도 안전하다** — 참조가 아직
        움직이지 않았고 사용자 작업도 그대로다. (reset --hard로 하면 조용히
        덮어쓴다. 그래서 쓰지 않는다.)
        """
        # 진행 중인 병합·rebase 위로 빨리 감으면 MERGE_HEAD가 살아남아, 다음
        # 커밋이 create_commit의 병합 부모 경로를 타고 **위조된 머지 커밋**이
        # 된다. 사용자는 파일 하나짜리 커밋을 의도했는데 히스토리에는 브랜치
        # 전체를 병합한 커밋이 박히고, state_cleanup이 MERGE_HEAD까지 지워
        # 되돌릴 단서도 사라진다. 실제 git도 이 상태를 하드 거부한다
        # ("You have not concluded your merge").
        state = self._repo.state()
        if state != pygit2.enums.RepositoryState.NONE:
            raise EngineError(
                "진행 중인 작업이 끝나지 않아 빨리 감을 수 없습니다.",
                detail=f"저장소 상태: {state!r}",
                action="병합이나 rebase를 마무리하거나 취소한 뒤 "
                "다시 시도해 주세요.",
            )

        head = self._repo.references.get("HEAD")
        head_branch = (
            head.target
            if head is not None
            and head.type == pygit2.enums.ReferenceType.SYMBOLIC
            else None
        )

        # 제출 시점의 브랜치를 고정한다. pull의 네트워크 절반이 도는 동안
        # 사용자가 브랜치를 전환하면 이 작업은 **엉뚱한 브랜치를 원격 위치로
        # 덮어쓴다** — 오류 없이, reflog로만 복구 가능하게. 두 WriteQueue 작업
        # 사이의 경합이라 UI 쪽 가드로는 막을 수 없다.
        if expected_branch is not None and head_branch != f"refs/heads/{expected_branch}":
            raise EngineError(
                f"'{expected_branch}'에 합치려 했지만 현재 브랜치가 바뀌었습니다.",
                detail=f"HEAD={head_branch}",
                action="브랜치를 확인한 뒤 다시 가져와 합치기를 실행해 주세요.",
            )

        preview = self.merge_preview(upstream_ref)
        if preview.kind is MergeKind.UP_TO_DATE:
            return preview.target_sha
        if preview.kind is not MergeKind.FAST_FORWARD:
            raise EngineError(
                "빨리 감을 수 없는 상태입니다.",
                action="양쪽에 서로 다른 커밋이 있어 병합이 필요합니다.",
            )

        target = pygit2.Oid(hex=preview.target_sha)
        commit = self._repo.get(target)

        try:
            with _translate("빨리 감기"):
                # 1) 워킹트리와 인덱스를 먼저 맞춘다 (HEAD는 아직 옛 위치)
                self._repo.checkout_tree(commit)
                # 2) 그 다음 참조를 옮긴다
                if head_branch is not None:
                    existing = self._repo.references.get(head_branch)
                    if existing is None:
                        # unborn HEAD — 브랜치 참조가 아직 없다. 여기서
                        # KeyError가 나면 워킹트리는 이미 원격 내용으로
                        # 덮여 있는데 HEAD는 unborn인 채로 남아, 그 상태에서
                        # 커밋하면 원격과 영영 갈라지는 루트 커밋이 된다.
                        self._repo.create_reference(head_branch, target)
                    else:
                        existing.set_target(target)
                else:
                    self._repo.set_head(target)  # 분리된 HEAD
        except EngineError as exc:
            if "conflict" in (exc.detail or "").lower():
                raise EngineError(
                    "커밋하지 않은 변경이 있어 합칠 수 없습니다.",
                    detail=exc.detail,
                    action="변경 사항을 커밋하거나 stash에 보관한 뒤 "
                    "다시 시도해 주세요.",
                ) from exc
            raise

        return preview.target_sha

    # ------------------------------------------------------------------
    # Stash (Phase 2)
    # ------------------------------------------------------------------

    def stash_save(self, message: str | None = None) -> str:
        """현재 변경을 stash에 보관한다."""
        try:
            signature = self._repo.default_signature
        except (KeyError, pygit2.GitError) as exc:
            raise EngineError(
                "stash 작성자 정보가 설정되어 있지 않습니다.",
                detail=str(exc),
                action="git config --global user.name/user.email 을 설정해 주세요.",
            ) from exc

        try:
            with _translate("stash 보관"):
                oid = self._repo.stash(
                    signature,
                    message=message or "gitclient stash",
                    include_untracked=True,
                )
                return str(oid)
        except EngineError as exc:
            if "nothing to stash" in (exc.detail or "").lower():
                raise EngineError(
                    "보관할 변경 사항이 없습니다.",
                ) from exc
            raise

    def stash_pop(self) -> None:
        """가장 최근 stash를 꺼내 적용한다."""
        with _translate("stash 적용"):
            stashes = self._repo.listall_stashes()
            if not stashes:
                raise EngineError(
                    "보관된 stash가 없습니다.",
                )
            self._repo.stash_pop(0)

    def stash_count(self) -> int:
        with _translate("stash 조회"):
            return len(self._repo.listall_stashes())

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
