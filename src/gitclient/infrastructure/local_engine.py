"""pygit2 기반 로컬 Git 엔진.

로컬 경로 전담이다. 네트워크 작업은 별도의 git CLI 엔진이 맡는다.
(doc/design.md §2.3 하이브리드 엔진)

**예외가 하나 있다.** 히스토리 재작성(rebase·cherry-pick·revert)만 이 모듈
안에서 git CLI를 부른다 — pygit2에 rebase가 없고, 시퀀서 상태는 git 자신만
완전히 이해하기 때문이다 (ADR-67). 그 구획은 아래에 따로 표시해 두었다.

pygit2 타입은 이 모듈 밖으로 새어나가지 않는다. 바깥에는 domain.models의
순수 파이썬 객체만 전달한다. **예외도 마찬가지다** — raw pygit2 예외가
이 층 밖으로 새면 UI의 `except GitClientError`가 잡지 못해 Qt 이벤트 루프까지
올라간다. pygit2를 만지는 공개 메서드는 `_translate()`로 경계를 세운다.
(doc/design.md §7)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
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

from gitclient.i18n import trf
from gitclient.domain.errors import (
    EngineError,
    GitClientError,
    RepositoryNotFoundError,
    RepositoryOpenError,
)
from gitclient.domain.command_log import COMMAND_LOG
from gitclient.infrastructure.remote_engine import (
    INHERITED_ENV_BLOCKLIST,
    _kill_process_tree,
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
    ConflictChoice,
    ConflictDetail,
    ConflictSide,
    ConflictSideContent,
    ConflictedFile,
    DiffLine,
    DiffLineKind,
    FileChange,
    HistoryOutcome,
    HistoryOutcomeKind,
    MergeKind,
    MergeOutcome,
    MergePreview,
    OperationState,
    PathHistoryEntry,
    RebaseAction,
    RebaseStep,
    Ref,
    RefKind,
    ReflogEntry,
    RepoOperation,
    RepositoryInfo,
    ResetKind,
    Signature,
    WorkAreaStatus,
    WorkingFileChange,
    WorkingTreeStatus,
)

logger = logging.getLogger(__name__)

# pygit2는 존재하지 않는 줄 번호를 -1로 준다. 도메인 모델에서는 None으로 바꾼다.
_NO_LINE = -1


# 저장소에 없는 오브젝트를 만났을 때 pygit2/libgit2가 내는 흔적.
# KeyError는 인자가 OID 문자열이고, GitError는 문구에 그것이 실린다.
_MISSING_OBJECT = re.compile(r"\b([0-9a-f]{7,40})\b")


def _missing_oid(exc: Exception) -> str | None:
    """"없는 오브젝트" 오류라면 그 OID. 아니면 None."""
    if isinstance(exc, KeyError) and exc.args:
        candidate = str(exc.args[0])
        return candidate if _MISSING_OBJECT.fullmatch(candidate) else None
    text = str(exc).lower()
    if "not found" not in text and "does not exist" not in text:
        return None
    match = _MISSING_OBJECT.search(text)
    return match.group(1) if match else None


@contextmanager
def _translate(context: str, *, partial_hint: bool = False):
    """pygit2 예외를 도메인 예외로 변환하는 경계.

    이미 도메인 예외인 것은 그대로 통과시킨다. KeyError를 포함하는 이유는
    pygit2가 존재하지 않는 오브젝트 조회를 KeyError로 던지기 때문이다.

    **없는 오브젝트는 따로 말한다** (ADR-88). 부분 복제(ADR-6의 선택지)는
    blob을 나중에 받으므로, 오프라인이거나 원격이 사라졌으면 파일을 여는
    순간 이 오류가 난다. OID를 버리고 "Git 엔진 오류"로 뭉개면 사용자는
    **무엇이 없는지도, 어떻게 받는지도** 알 수 없다.
    """
    try:
        yield
    except GitClientError:
        raise
    except (pygit2.GitError, pygit2.InvalidSpecError, KeyError, ValueError) as exc:
        oid = _missing_oid(exc)
        if oid is not None:
            raise EngineError(
                "저장소에 없는 객체를 참조했습니다.",
                detail=f"{oid} ({type(exc).__name__}: {exc})",
                action=(
                    "부분 복제(blob 지연 수신) 저장소입니다. 네트워크에 연결한 "
                    "뒤 가져오기(Fetch)를 실행하면 필요한 객체를 받아옵니다."
                    if partial_hint
                    else "저장소가 손상되었거나 참조가 가리키는 객체가 "
                    "없습니다. 가져오기(Fetch)로 받아올 수 있는지 확인해 주세요."
                ),
            ) from exc
        raise EngineError(
            trf("{context} 중 Git 엔진 오류가 발생했습니다.", context=context),
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

# rebase 백엔드가 셋이라 상태 상수도 셋이지만 사용자에게는 하나의 일이다.
_STATE_TO_OPERATION: dict[object, RepoOperation] = {
    pygit2.enums.RepositoryState.NONE: RepoOperation.NONE,
    pygit2.enums.RepositoryState.MERGE: RepoOperation.MERGE,
    pygit2.enums.RepositoryState.REVERT: RepoOperation.REVERT,
    pygit2.enums.RepositoryState.REVERT_SEQUENCE: RepoOperation.REVERT,
    pygit2.enums.RepositoryState.CHERRYPICK: RepoOperation.CHERRY_PICK,
    pygit2.enums.RepositoryState.CHERRYPICK_SEQUENCE: RepoOperation.CHERRY_PICK,
    pygit2.enums.RepositoryState.REBASE: RepoOperation.REBASE,
    pygit2.enums.RepositoryState.REBASE_INTERACTIVE: RepoOperation.REBASE,
    pygit2.enums.RepositoryState.REBASE_MERGE: RepoOperation.REBASE,
}

# `--continue` / `--skip` / `--abort`를 받는 명령. 병합은 여기 없다 —
# 병합을 이어가는 방법은 `--continue`가 아니라 그냥 커밋이다.
_SEQUENCER_COMMAND: dict[RepoOperation, str] = {
    RepoOperation.REBASE: "rebase",
    RepoOperation.CHERRY_PICK: "cherry-pick",
    RepoOperation.REVERT: "revert",
}

_RESET_MODES = {
    ResetKind.SOFT: pygit2.enums.ResetMode.SOFT,
    ResetKind.MIXED: pygit2.enums.ResetMode.MIXED,
    ResetKind.HARD: pygit2.enums.ResetMode.HARD,
}

# 로컬 연산이라 네트워크 같은 정지 판정이 필요 없다. 이 값은 "무한정
# 붙잡히지 않는다"는 보험이지 성능 목표가 아니다 — 실제 rebase는 수백 개
# 커밋도 초 단위로 끝난다.
_HISTORY_TIMEOUT_S = 600

# 원격 경로의 블록리스트는 **저장소 위치** 변수만 걷어낸다. 그쪽은 `-c`로
# 설정을 덮어쓰므로 그것으로 충분했다. 여기는 `-c`를 쓰지 않으므로(사용자
# 설정을 살려야 한다, §4.12.2) 같은 논리가 옮겨오지 않는다.
#
# 실측으로 확인한 두 가지:
#   GIT_CONFIG_COUNT/KEY_n/VALUE_n → 임의 설정 주입. `rebase.backend=apply`가
#     조용히 켜졌다. 사용자 설정을 존중하는 것과 **환경변수로 주입된 설정을
#     존중하는 것은 다르다** — 후자는 앱을 어디서 띄웠느냐로 동작이 바뀐다.
#   GIT_COMMITTER_NAME → 재생된 커밋의 커미터가 그대로 바뀌었다. pygit2
#     경로(create_commit)는 config의 default_signature만 보므로, 같은 앱이
#     병합 커밋과 리베이스 커밋에 **다른 사람 이름을 적게 된다.**
_HISTORY_ENV_BLOCKLIST: frozenset[str] = INHERITED_ENV_BLOCKLIST | frozenset(
    {
        # 둘 다 단독으로 통하는 주입 경로다 (실측). `GIT_CONFIG_KEY_n`/
        # `VALUE_n`은 `COUNT` 없이는 무시되므로 따로 막지 않는다 — 막는
        # 시늉만 하는 코드는 다음 사람이 그 줄을 지웠을 때 아무 일도 일어나지
        # 않아 "안전하구나"로 잘못 읽게 만든다.
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
    }
)


# **백엔드마다 파일 이름이 다르다.** merge 백엔드는 msgnum/end, apply 백엔드는
# next/last를 쓴다 (실측). 한쪽 이름만 알면 다른 백엔드에서 진행 표시가 통째로
# 사라진다 — `rebase.backend=apply`는 설정 한 줄이면 켜지고 상속된
# `GIT_CONFIG_*`로도 켜진다.
_REBASE_PROGRESS_FILES = (
    ("rebase-merge", "msgnum", "end"),
    ("rebase-apply", "next", "last"),
)

# git이 뱉는 ANSI 제어 시퀀스. 진행률을 지우려고 `[K`를 섞어 보내는데
# 그대로 오류 상세에 실으면 사용자에게 깨진 문자로 보인다.
_ANSI = re.compile(r"\[[0-9;?]*[a-zA-Z]")


def _shorthand_of(ref: str | None) -> str | None:
    """`refs/heads/feature/x` → `feature/x`.

    **마지막 슬래시로 자르면 안 된다.** 슬래시가 든 브랜치 이름은 예외가
    아니라 기본에 가깝고(이 저장소부터 `feat/...`다), 자르면 배너가 존재하지
    않는 브랜치를 가리킨다 — "어디로 돌아가는가"가 배너의 존재 이유인데
    그 답이 틀리는 것이다.
    """
    if not ref:
        return None
    for prefix in ("refs/heads/", "refs/remotes/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref if not ref.startswith("refs/") else None


def _read_state_file(base: Path, name: str) -> str | None:
    """`.git/rebase-merge/` 아래 작은 상태 파일 하나. 못 읽으면 None."""
    try:
        return (base / name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _read_state_number(base: Path, name: str) -> int | None:
    raw = _read_state_file(base, name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _message_of(result: subprocess.CompletedProcess[str]) -> str:
    """사용자에게 보여줄 git의 설명.

    stderr를 먼저 본다. git은 진단을 그쪽에 쓰고 stdout에는 진행 상황을
    쓴다. 둘 다 비었을 수 있다 (조용히 성공한 경우).
    """
    for stream in (result.stderr, result.stdout):
        text = _ANSI.sub("", stream or "").strip()
        if text:
            return text
    return ""


def _looks_empty(result: subprocess.CompletedProcess[str]) -> bool:
    """"커밋할 것이 없다"는 git의 거부인가.

    문구로 판정하는 것은 취약하지만 종료 코드가 이 경우를 구분해 주지
    않는다(실측: rc=1, 상태 유지 — 진짜 오류와 같다). 놓치면 일반 오류로
    보여주게 되는데, 그때도 안내가 없을 뿐 데이터는 안전하다.

    `--empty=stop`의 멈춤(실측: "nothing to commit ... git rebase
    --continue")도 여기서 잡는다 — 판정 자체는 충돌 0개 + 진행 중이라는
    저장소 상태가 하고(ADR-69), 이 문구는 진짜 오류와의 마지막 구분선이다.
    """
    text = _message_of(result).lower()
    return ("empty" in text and "skip" in text) or "nothing to commit" in text


# git이 이미 upstream에 있는 커밋을 정당하게 생략할 때의 경고 (실측,
# LC_ALL=C 고정이라 문구가 안정적이다). 이 생략은 손실이 아니지만 —
# 같은 변경이 결과에 이미 있다 — 조용히 지나가면 사용자가 "커밋이
# 사라졌다"와 구분할 수 없다 (ADR-76).
_SKIPPED_APPLIED = re.compile(
    r"skipped previously applied commit ([0-9a-f]{7,40})"
)


def _skipped_commits_of(result: subprocess.CompletedProcess[str]) -> tuple[str, ...]:
    return tuple(_SKIPPED_APPLIED.findall(result.stderr or ""))

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


def _conflict_side(ancestor, ours, theirs) -> ConflictSide:  # noqa: ANN001
    """충돌의 종류를 가른다.

    내용끼리 부딪히는 것만 충돌이 아니다 — 한쪽이 지운 경우도 충돌이고,
    그때는 보여줄 "상대 내용"이 아예 없다. 3-way UI가 이 구분 없이는
    존재하지 않는 쪽을 그리려 든다.
    """
    if ours is None and theirs is None:
        # 이름 변경 대 삭제에서 원래 경로가 이렇게 남는다. 먼저 걸러야
        # 한다 — "우리가 지웠다"로 분류하면 없는 상대 내용을 그리려 든다.
        return ConflictSide.BOTH_DELETED
    if ours is None:
        return ConflictSide.DELETED_BY_US
    if theirs is None:
        return ConflictSide.DELETED_BY_THEM
    if ancestor is None:
        return ConflictSide.BOTH_ADDED
    return ConflictSide.BOTH_MODIFIED


def _peel_to_commit_id(reference: pygit2.Reference) -> pygit2.Oid:
    """참조가 최종적으로 가리키는 커밋 Oid.

    심볼릭 참조(`origin/HEAD`)와 어노테이트 태그를 모두 벗겨낸다 —
    `_branch_oid`가 막았던 것과 같은 함정이다.
    """
    resolved = reference.resolve()
    return resolved.peel(pygit2.Commit).id


class LocalGitEngine:
    """열려 있는 저장소 하나에 대한 읽기 연산을 제공한다."""

    def __init__(self, repo: pygit2.Repository, git_binary: str = "git") -> None:
        self._repo = repo
        # 히스토리 재작성 구획에서만 쓴다 (ADR-67). 이름을 주입받는 이유는
        # 테스트가 가짜 git으로 실패 경로를 만들 수 있게 하기 위해서다.
        self._git = git_binary
        # 없는 객체를 만났을 때의 안내를 가른다 (ADR-88). 설정 조회라
        # 열 때 한 번만 한다 — blob을 읽는 경로마다 다시 물으면 그 자체가
        # 비용이다.
        self._partial = self._is_partial(repo)

    @classmethod
    def open(cls, path: str | Path) -> LocalGitEngine:
        """경로에서 저장소를 찾아 연다. 하위 디렉터리를 줘도 위로 올라가며 찾는다."""
        target = Path(path).expanduser()
        if not target.exists():
            raise RepositoryNotFoundError(
                f"경로를 찾을 수 없습니다: {target}",
                action="경로가 삭제되었거나 이동했을 수 있습니다. 다른 폴더를 선택해 주세요.",
            )

        # **탐색도 던진다.** libgit2는 "못 찾음"만 None으로 주고 권한 거부나
        # I/O 오류는 예외로 올린다. 이 줄이 try 밖에 있으면 raw
        # `pygit2.GitError`가 이 층 밖으로 새고, UI의 `except GitClientError`가
        # 잡지 못해 Qt 이벤트 루프까지 올라간다 (§7이 금지한 그것).
        try:
            discovered = pygit2.discover_repository(str(target))
        except pygit2.GitError as exc:
            raise RepositoryOpenError(
                f"저장소를 찾는 중 오류가 발생했습니다: {target}",
                detail=str(exc),
                action="경로에 접근 권한이 있는지 확인해 주세요.",
            ) from exc
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
        with _translate("커밋 상세 조회", partial_hint=self._partial):
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
        with _translate("diff 계산", partial_hint=self._partial):
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
            trf("변경 내용을 찾을 수 없습니다: {path}", path=path),
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
        self,
        path: str,
        selected: set[tuple[int, int]] | None = None,
        *,
        expected_patch: str | None = None,
    ) -> None:
        """선택한 헝크/줄만 스테이징한다.

        `selected`가 None이면 파일의 모든 변경을 스테이징한다(헝크 전체 선택).
        워킹 트리는 건드리지 않는다 — 인덱스에만 적용된다.
        """
        patch = self.file_patch(path, staged=False)
        self._require_same_patch(patch, expected_patch)
        self._apply_synthesized(patch, selected, reverse=False, action="스테이징")

    def unstage_partial(
        self,
        path: str,
        selected: set[tuple[int, int]] | None = None,
        *,
        expected_patch: str | None = None,
    ) -> None:
        """선택한 헝크/줄만 스테이징 해제한다.

        스테이징된 diff(HEAD↔인덱스)를 뒤집어 인덱스에 적용한다.
        """
        patch = self.file_patch(path, staged=True)
        self._require_same_patch(patch, expected_patch)
        self._apply_synthesized(patch, selected, reverse=True, action="스테이징 취소")

    @staticmethod
    def _require_same_patch(patch, expected_patch: str | None) -> None:  # noqa: ANN001
        """화면이 본 패치와 지금 적용할 패치가 같은지 확인한다.

        선택 좌표는 **사용자가 화면에서 본** 패치 기준인데, 여기서는 적용
        시점에 워킹 트리를 다시 읽는다. 그 사이 파일이 바뀌면 같은 좌표가
        다른 줄을 가리켜 **고르지 않은 내용이 조용히 스테이징된다** —
        오류도 경고도 없이 인덱스가 오염되는 유일한 경로다.

        외부 편집기가 저장했거나 다른 도구가 건드린 경우가 여기 해당한다.
        (`fast_forward`/`merge`의 `expected_branch`와 같은 취지)
        """
        if expected_patch is None:
            return  # 호출자가 화면 상태를 모르는 경우 (테스트, 전체 적용)
        if patch.fingerprint != expected_patch:
            raise EngineError(
                "파일이 바뀌어 선택한 줄을 그대로 적용할 수 없습니다.",
                detail="화면에 표시된 내용과 지금 파일의 내용이 다릅니다.",
                action="변경 내용을 다시 확인한 뒤 선택해 주세요. "
                "(F5로 새로 고칠 수 있습니다)",
            )

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
                    trf("부분 {action}에 실패했습니다.", action=action),
                    detail=trf("{exc}\n\n--- 생성된 패치 ---\n{patch_text}", exc=exc, patch_text=patch_text),
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
            if flags & FileStatus.CONFLICTED:
                # 충돌 중인 파일에 "버리기"는 성립하지 않는다. 인덱스에
                # 스테이지가 셋이라 checkout_index는 병합 이전 내용이 아니라
                # 충돌 마커를 다시 써넣을 뿐이고, 충돌은 그대로 남는다 —
                # 확인 다이얼로그가 약속한 것과 다른 일이 벌어진다.
                # git도 같은 이유로 거부한다(path '...' is unmerged).
                raise EngineError(
                    trf("'{path}'은(는) 충돌 해결 중이라 변경을 버릴 수 없습니다.", path=path),
                    action="충돌 마커를 정리한 뒤 스테이징해 해결하거나, "
                    "'저장소 > 병합 중단'으로 병합 전체를 되돌려 주세요.",
                )
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

        # 충돌이 남은 인덱스로는 트리를 만들 수 없다. 막지 않으면 libgit2
        # 원문("cannot create a tree from a not fully merged index")이 조치
        # 한 줄 없이 그대로 나간다 — 충돌을 해결하고 커밋하라고 안내해 놓고,
        # 그 안내를 따르다 스테이징을 빠뜨린 첫 실수에서 길을 잃게 된다.
        unresolved = self.index_conflicts()
        if unresolved:
            paths = ", ".join(c.path for c in unresolved[:3])
            if len(unresolved) > 3:
                paths += f" 외 {len(unresolved) - 3}개"
            raise EngineError(
                trf("해결되지 않은 충돌 {len_unresolved}개가 남아 커밋할 수 없습니다.", len_unresolved=len(unresolved)),
                detail=trf("충돌한 파일: {paths}", paths=paths),
                action="충돌한 파일을 정리한 뒤 스테이징하면 커밋할 수 "
                "있습니다. 되돌리려면 '저장소 > 병합 중단'을 선택해 주세요.",
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

    def create_branch(
        self, name: str, *, checkout: bool = False, sha: str | None = None
    ) -> None:
        """브랜치를 만든다 — 기본은 HEAD, `sha`를 주면 그 커밋에서.

        `sha` 갈래는 reflog 복구(FR-10)의 실행부다: reset·건너뛰기로
        목록에서 사라진 커밋에 브랜치를 달아 다시 닿게 만든다.
        """
        with _translate("브랜치 생성"):
            if sha is None and self._repo.head_is_unborn:
                raise EngineError(
                    "커밋이 없어 브랜치를 만들 수 없습니다.",
                    action="첫 커밋을 만든 뒤 브랜치를 생성해 주세요.",
                )
            if name in self._repo.branches.local:
                raise EngineError(
                    trf("브랜치가 이미 있습니다: {name}", name=name),
                    action="다른 이름을 사용해 주세요.",
                )
            if sha is None:
                target = self._repo[self._repo.head.target]
            else:
                target = self._lookup_commit(sha)
            branch = self._repo.branches.local.create(name, target)
            if checkout:
                self._repo.checkout(branch)

    def head_reflog(self, limit: int = 200) -> tuple[ReflogEntry, ...]:
        """HEAD가 지나온 자리들 — 최신이 먼저다 (FR-09).

        상한이 있는 읽기 전용 호출이라 UI 스레드에서 불러도 된다
        (200건 파싱은 ms 단위 — §3.3의 "상한이 실제로 정해진 호출").
        """
        with _translate("reflog 조회"):
            if self._repo.head_is_unborn:
                return ()
            entries: list[ReflogEntry] = []
            for entry in self._repo.references["HEAD"].log():
                if len(entries) >= limit:
                    break
                committer = entry.committer
                when = datetime.fromtimestamp(
                    committer.time,
                    tz=timezone(timedelta(minutes=committer.offset)),
                )
                entries.append(
                    ReflogEntry(
                        sha=str(entry.oid_new),
                        message=entry.message or "",
                        when=when,
                    )
                )
            return tuple(entries)

    # -- 원격 관리 (F1, Phase 3 증분 5) --------------------------------
    #
    # 네트워크는 CLI, 로컬 쓰기는 pygit2라는 ADR-2의 경계에서 원격
    # 관리는 **로컬 쓰기다** — 설정과 원격 추적 참조를 만질 뿐 회선에
    # 나가지 않는다. 그래서 RemoteEngine이 아니라 여기 산다.

    def list_remotes_with_urls(self) -> tuple[tuple[str, str], ...]:
        with _translate("원격 목록 조회"):
            return tuple(
                (remote.name, remote.url) for remote in self._repo.remotes
            )

    def add_remote(self, name: str, url: str) -> None:
        name, url = name.strip(), url.strip()
        with _translate("원격 추가"):
            if not name or not url:
                raise EngineError(
                    "원격 이름과 주소를 모두 입력해 주세요.",
                )
            if any(r.name == name for r in self._repo.remotes):
                raise EngineError(
                    trf("원격이 이미 있습니다: {name}", name=name),
                    action="다른 이름을 쓰거나 기존 원격의 주소를 바꿔 주세요.",
                )
            self._repo.remotes.create(name, url)

    def remove_remote(self, name: str) -> None:
        """원격을 지운다. **원격 추적 참조도 함께 사라진다** — 호출 전에
        §5.2의 확인 절차를 거칠 것. 원격 저장소 자체는 건드리지 않는다."""
        with _translate("원격 삭제"):
            if not any(r.name == name for r in self._repo.remotes):
                raise EngineError(
                    trf("원격이 없습니다: {name}", name=name),
                    action="목록을 새로 고친 뒤 다시 시도해 주세요.",
                )
            self._repo.remotes.delete(name)

    def set_remote_url(self, name: str, url: str) -> None:
        url = url.strip()
        with _translate("원격 주소 변경"):
            if not url:
                raise EngineError("원격 주소를 입력해 주세요.")
            if not any(r.name == name for r in self._repo.remotes):
                raise EngineError(
                    trf("원격이 없습니다: {name}", name=name),
                    action="목록을 새로 고친 뒤 다시 시도해 주세요.",
                )
            self._repo.remotes.set_url(name, url)

    def path_history(self, path: str, *, limit: int = 1000) -> list[PathHistoryEntry]:
        """한 경로를 바꾼 커밋들 — 시간 역순 (경로 히스토리, ADR-90).

        **로컬 읽기인데 CLI를 쓴다** — ADR-2의 두 번째 예외다(첫째는
        시퀀서, ADR-67). 이유 둘: ① git의 히스토리 단순화(TREESAME 가지
        치기)와 같은 결과를 pygit2로 다시 만드는 것은 그 자체가 결함
        표면이고, ② commit-graph의 Bloom 필터는 git CLI만 읽는다 —
        실측(1만 커밋)에서 pygit2 순회 366.7ms, CLI 58.7ms, CLI+Bloom
        22.9ms였다. 느린 쪽을 골라 단순화 규칙까지 재구현할 이유가 없다.

        이름 변경(rename)은 따라가지 않는다 — 개명 시점에서 목록이
        끊긴다. `--follow`는 단일 경로 전용에 단순화 규칙도 달라져서,
        화면이 한계를 밝히는 쪽을 골랐다 (FR-04).

        존재한 적 없는 경로는 오류가 아니라 빈 목록이다 — git log의
        의미론 그대로이고, 화면은 "기록 없음"을 그리면 된다.
        """
        result = self._run_git(
            [
                "log",
                f"--max-count={limit}",
                # %x1f(단위 구분자)는 요약에 나올 수 없는 문자다 —
                # 쉼표·탭으로 가르면 요약이 필드를 오염시킨다.
                "--format=%H%x1f%an%x1f%at%x1f%s",
                "--",
                path,
            ],
            context="파일 히스토리",
        )
        entries: list[PathHistoryEntry] = []
        for line in result.stdout.splitlines():
            parts = line.split("\x1f", 3)
            if len(parts) != 4:
                continue  # 형식 밖의 줄은 버린다 — 잘린 마지막 줄 등
            sha, author, timestamp, summary = parts
            entries.append(
                PathHistoryEntry(
                    sha=sha,
                    summary=summary,
                    author=author,
                    when=datetime.fromtimestamp(int(timestamp), tz=timezone.utc),
                )
            )
        return entries

    def idle_repack(self, *, should_abort=None) -> bool:  # noqa: ANN001
        """유휴 시간에 팩을 하나로 정돈한다 (FR-08). 중단·실패는 False.

        `unpackLimit=1` + `maintenance.auto=false`(§4.6.2)가 매 fetch마다
        팩을 하나씩 쌓는다 — 원격 작업 뒤의 `maintenance run --auto`가
        대부분 치우지만, 여기서는 **델타 설정까지 얹어** 전부 다시 싼다.
        ADR-35가 push에서 기각한 `pack.window/depth=250`을 쓰는 유일한
        자리다: push 중의 CPU는 사용자가 기다리는 시간이지만, 유휴의 CPU는
        진짜 공짜다 (§1.4 원칙 1의 예외).

        **조용한 배경 작업이다** — 실패해도 예외를 올리지 않는다(다음
        유휴에 다시 온다). `should_abort`가 참을 돌려주면 0.2초 안에
        프로세스 트리를 끊고 물러난다: 사용자 작업이 큐 뒤에서 기다리게
        하면 안 된다 (NFR-03). git repack은 임시 팩에 쓰고 원자적으로
        교체하므로 중간에 죽여도 저장소는 일관적이다.
        """
        workdir = self._repo.workdir
        if not workdir:
            return False
        if should_abort is not None and should_abort():
            # 큐에서 기다리는 사이 사용자 작업이 이미 왔다 — 프로세스를
            # 띄우지도 않는다. (소형 저장소는 repack이 첫 폴링(0.2초)보다
            # 빨리 끝나므로, 시작 후 판정만으로는 이 신호를 지나친다.)
            return False
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in _HISTORY_ENV_BLOCKLIST
        }
        env["GIT_TERMINAL_PROMPT"] = "0"
        if not self._idle_command(
            [
                self._git, "-C", workdir,
                "-c", "pack.window=250",
                "-c", "pack.depth=250",
                "repack", "-a", "-d", "-q",
            ],
            env=env,
            should_abort=should_abort,
        ):
            return False
        # commit-graph를 Bloom 필터(--changed-paths)와 함께 쓴다 — 경로
        # 히스토리(ADR-90)의 가속 장치다. 실측(1만 커밋): 경로 로그
        # 58.7ms → 22.9ms, 쓰기 비용 137.7ms는 유휴라 공짜다. repack이
        # 팩을 갈아엎은 직후가 그래프도 함께 새로 쓸 자리다.
        return self._idle_command(
            [
                self._git, "-C", workdir,
                "commit-graph", "write", "--reachable", "--changed-paths",
            ],
            env=env,
            should_abort=should_abort,
        )

    def _idle_command(
        self, command: list[str], *, env: dict[str, str], should_abort=None  # noqa: ANN001
    ) -> bool:
        """유휴 단계 하나 — 조용히 돌고, 0.2초 안에 물러날 수 있어야 한다."""
        started_at = time.perf_counter()
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=(os.name != "nt"),
            )
        except OSError:
            logger.debug("유휴 명령 시작 실패", exc_info=True)
            return False
        aborted = False
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if should_abort is not None and should_abort():
                    aborted = True
                    _kill_process_tree(proc)
                    proc.wait()
                    break
        COMMAND_LOG.record(
            command,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            returncode=proc.poll(),
        )
        return not aborted and proc.returncode == 0

    def _require_no_sequencer(self) -> None:
        """시퀀서가 도는 중에 HEAD를 옮기지 못하게 한다.

        `reset`은 이미 막고 있었는데 **브랜치 전환은 열려 있었다** — 같은
        위험이다. 실측: 리베이스 중 전환하면 시퀀서가 기대하는 HEAD가
        어긋나 `--continue`가 엉뚱한 충돌을 내고, 되돌린 뒤 로그에 커밋이
        중복돼 남았다. 병합은 예외다 — git 자신이 병합 중 체크아웃을
        판단해 막거나 허용하고, 그 판단이 우리 것보다 정확하다.
        """
        operation = self.current_operation()
        if operation in _SEQUENCER_COMMAND:
            raise EngineError(
                trf("{operation_label}이(가) 진행 중이라 브랜치를 바꿀 수 없습니다.", operation_label=operation.label),
                detail=trf("저장소 상태: {self__repo_state!r}", self__repo_state=self._repo.state()),
                action="진행 중인 작업을 마치거나 '중단'으로 되돌린 뒤 "
                "다시 시도해 주세요.",
            )

    def checkout_branch(self, name: str) -> None:
        """브랜치를 전환한다. 충돌하는 로컬 변경이 있으면 실패한다."""
        self._require_no_sequencer()
        try:
            with _translate("브랜치 전환"):
                self._repo.checkout(self._repo.branches.local[name])
        except EngineError as exc:
            if "conflict" in (exc.detail or "").lower():
                raise EngineError(
                    trf("로컬 변경과 충돌해 '{name}' 브랜치로 전환할 수 없습니다.", name=name),
                    detail=exc.detail,
                    action="변경 사항을 커밋하거나 stash에 보관한 뒤 다시 시도해 주세요.",
                ) from exc
            raise

    def delete_branch(self, name: str) -> None:
        """로컬 브랜치를 삭제한다. 현재 브랜치는 삭제할 수 없다."""
        with _translate("브랜치 삭제"):
            branch = self._repo.branches.local.get(name)
            if branch is None:
                raise EngineError(trf("브랜치를 찾을 수 없습니다: {name}", name=name))
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
        프로세스 생성 비용(실측 19ms)만 냈다. §2.3의 엔진 경계(로컬은
        pygit2, 네트워크는 CLI)에 어긋났고, UI 스레드에서 불리므로 G4
        예산 50ms의 40%를 이유 없이 태우고 있었다.

        **pygit2 쪽 비용도 공짜가 아니다 — 갈라진 만큼 선형이다** (감사
        실측: 100+100 중앙값 0.41ms, 1만+1만 최악 84.8ms). 한때 "0.002ms"
        라고 적었던 것은 한쪽 1커밋 픽스처의 값이었다. 극단 모노레포
        대응은 backlog §3.4.

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
                    trf("원격 추적 참조를 찾을 수 없습니다: {upstream_ref}", upstream_ref=upstream_ref),
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

    def merge(self, source_ref: str, expected_branch: str | None = None) -> MergeOutcome:
        """`source_ref`를 현재 브랜치에 합친다.

        빨리 감을 수 있으면 빨리 감고, 아니면 병합을 시작한다.
        **충돌은 예외가 아니라 결과다** — git이 할 수 있는 만큼 합쳐두고
        나머지를 사람에게 넘긴 상태이므로, 호출자가 분기할 수 있게 값으로
        돌려준다.

        `expected_branch`는 `fast_forward`와 같은 이유로 받는다: 이 작업이
        큐에서 실행될 때 사용자가 브랜치를 바꿨을 수 있다.
        """
        self._require_quiet_repository()
        head_branch = self._head_branch()
        self._require_expected_branch(head_branch, expected_branch)

        preview = self.merge_preview(source_ref)
        if preview.kind is MergeKind.UP_TO_DATE:
            return MergeOutcome(MergeKind.UP_TO_DATE)
        if preview.kind is MergeKind.FAST_FORWARD:
            sha = self.fast_forward(source_ref, expected_branch)
            return MergeOutcome(MergeKind.FAST_FORWARD, merged_sha=sha)

        target = pygit2.Oid(hex=preview.target_sha)
        with _translate("병합"):
            # 커밋하지 않은 **추적 중인** 변경만 막는다. 병합이 반쯤 진행된
            # 뒤에 막히면 사용자가 손으로 풀어야 하므로 우리가 먼저 본다.
            #
            # 추적되지 않은 파일은 세지 않는다 — git도 막지 않고, 막으면 메모
            # 파일 하나 때문에 pull의 병합 갈래가 통째로 멈춘다. 게다가
            # 우리가 안내하는 stash는 기본값에서 추적되지 않은 파일을 담지
            # 않아, 사용자가 그대로 따라도 상태가 바뀌지 않는다. 병합이 실제로
            # 덮어쓸 파일이 있으면 libgit2가 원자적으로 거부한다.
            status = self.working_tree_status()
            blocking = (
                *status.staged,
                *(c for c in status.unstaged if c.status is not WorkAreaStatus.NEW),
            )
            if blocking:
                raise EngineError(
                    "커밋하지 않은 변경이 있어 병합을 시작할 수 없습니다.",
                    detail=", ".join(c.path for c in blocking[:5]),
                    action="변경 사항을 커밋하거나 stash에 보관한 뒤 "
                    "다시 시도해 주세요.",
                )
            self._repo.merge(target)

        # 병합은 WriteQueue 워커에서 돈다 — 마커 분류(blob 로드)를 여기서
        # 끝내야 UI의 안내문("N개에는 마커가 없습니다")이 UI 스레드에서
        # blob을 읽지 않는다 (ADR-77).
        conflicts = self.merge_conflicts(classify=True)
        if conflicts:
            # 저장소는 병합 진행 중으로 남는다. 워킹 트리에는 충돌 마커가
            # 들어 있고, 충돌하지 않은 변경은 이미 반영돼 있다.
            return MergeOutcome(MergeKind.MERGE_REQUIRED, conflicts=conflicts)

        # 충돌이 없으면 바로 머지 커밋을 만든다. 여기서 멈추면 사용자가
        # "병합했는데 커밋이 없는" 상태를 이해해야 하고, 그건 우리가 떠넘긴
        # 복잡도다.
        message = self._merge_message(source_ref)
        try:
            sha = self.create_commit(message)
        except GitClientError as exc:
            # 여기서 그냥 던지면 저장소는 병합 중인데 충돌은 0개로 남는다 —
            # 사용자에게는 원인 모를 오류만 뜨고, 중단 메뉴를 켤 근거도 없다.
            # 시작 전에 추적 중인 변경이 없음을 확인했으므로 되돌려도 잃을
            # 사용자 작업이 없다.
            self.abort_merge()
            raise EngineError(
                "병합을 시작했지만 머지 커밋을 만들지 못해 되돌렸습니다.",
                detail=str(exc),
                action=getattr(exc, "action", None)
                or "원인을 해결한 뒤 다시 시도해 주세요.",
            ) from exc
        return MergeOutcome(MergeKind.MERGE_REQUIRED, merged_sha=sha)

    def _fresh_index(self) -> pygit2.Index:
        """디스크에서 다시 읽은 인덱스.

        pygit2는 인덱스를 메모리에 캐시한다. 그런데 화면을 그리는 엔진과
        쓰기를 수행하는 엔진은 **서로 다른 인스턴스다**(쓰기는 WriteQueue의
        워커 스레드에서 돈다). 캐시를 그대로 믿으면 사용자가 충돌을 해결해
        스테이징해도 UI는 영원히 "충돌 남음"으로 보고, 중단 메뉴가 꺼지지
        않으며 원격 액션도 잠긴 채로 갇힌다.
        """
        index = self._repo.index
        index.read(False)  # 디스크가 바뀌었을 때만 다시 읽는다
        return index

    # `is_merging()`이 한때 여기 있었다. 판정은 전부 `current_operation()`
    # (MERGE 포함 연산 일반)으로 옮겨가 프로덕션 호출자가 0이 됐다 —
    # 테스트만 지지하던 죽은 코드라 함께 지웠다 (backlog §3.8).

    def index_conflicts(self) -> tuple[ConflictedFile, ...]:
        """저장소 상태와 **무관하게** 인덱스에 남은 충돌 전부.

        충돌은 병합에서만 생기지 않는다 — stash pop, rebase, cherry-pick도
        만든다. 실측: stash pop 충돌은 `state()`가 NONE인데 인덱스에는
        충돌이 있다. 상태로 걸러내면 그 충돌들이 화면에서 통째로 사라지고,
        사용자는 아무 안내 없이 마커가 든 파일을 마주한다 (§13-2).

        `merge_conflicts()`와 나누는 이유: 그쪽은 "병합 중단"처럼 **병합에만**
        의미가 있는 기능이 쓰고, 이쪽은 "충돌을 보여주고 해결한다"처럼
        출처를 가리지 않는 기능이 쓴다.

        **한때 rebase·cherry-pick·revert를 걸러냈다** (ADR-65). 두 가지가
        위험했기 때문이다: 스테이지 2/3의 주체가 rebase에서 뒤집혀 "내 것
        사용"이 사용자 자신의 커밋을 버렸고, `--continue`가 없어 해결해
        놓고도 마무리를 못 했다. 실측에서 커밋이 조용히 사라졌다.

        증분 3에서 둘 다 해소됐다 — 라벨은 `conflict_labels()`가 연산에서
        유도하고(`OperationState.labels`), 마무리는 `continue_operation()`이
        맡는다. 그래서 다시 상태를 가리지 않는다. **이 함수를 상태로 거르는
        방향으로 되돌리려는 사람에게**: 감추는 것은 해결이 아니었다.
        사용자는 마커가 든 파일을 아무 안내 없이 마주할 뿐이었다.

        UI 스레드에서 불리므로 마커 분류는 하지 않는다 — `has_markers`는
        삭제 계열만 즉시 채워지고 나머지는 None이다 (ADR-77).
        """
        with _translate("충돌 목록 조회"):
            return self._collect_conflicts()

    def merge_conflicts(
        self, *, classify: bool = False
    ) -> tuple[ConflictedFile, ...]:
        """지금 병합 중이라면 해결되지 않은 파일들.

        인덱스의 충돌은 rebase·cherry-pick 중에도 생긴다. 상태를 함께
        확인하지 않으면 남의 작업 중 충돌을 "병합 중"으로 오인하고,
        그 오인이 `abort_merge`까지 이어지면 rebase가 파괴된다.
        """
        with _translate("충돌 목록 조회"):
            if self._repo.state() != pygit2.enums.RepositoryState.MERGE:
                return ()
            return self._collect_conflicts(classify=classify)

    def _collect_conflicts(
        self, *, classify: bool = False
    ) -> tuple[ConflictedFile, ...]:
        """인덱스의 충돌을 도메인 객체로 옮긴다.

        **열거와 분류를 나눈다** (ADR-77, ADR-47 대체). 열거는 인덱스만
        읽어 파일 크기와 무관하고(실측: 충돌 1,000개 5.9ms — G4 예산 안),
        마커 분류는 blob 내용을 로드해 크기에 비례한다(실측: 최대 2,957ms —
        예산의 59배). 그래서 분류는 `classify=True`를 준 호출자, 즉 워커
        스레드에서만 한다. UI 스레드 호출자(저장소 열기·쓰기 큐 idle)는
        분류 없이 열거만 받는다 — 그쪽이 실제로 쓰는 것은 경로와 개수뿐이다.

        삭제 계열의 `has_markers=False`는 분류가 아니라 종류에서 나오므로
        (합칠 상대가 없으면 마커도 없다) 언제나 즉시 채워진다.
        """
        conflicts = self._fresh_index().conflicts
        if conflicts is None:
            return ()
        found: list[ConflictedFile] = []
        for ancestor, ours, theirs in conflicts:
            entry = ours or theirs or ancestor
            if entry is None:  # pragma: no cover - 방어적
                continue
            side = _conflict_side(ancestor, ours, theirs)
            if side is not ConflictSide.BOTH_MODIFIED and side is not ConflictSide.BOTH_ADDED:
                has_markers: bool | None = False
            elif classify:
                has_markers = self._text_pair_has_markers(ours, theirs)
            else:
                has_markers = None  # 미분류 — 읽는 쪽이 필요하면 워커에서 채운다
            found.append(
                ConflictedFile(
                    path=entry.path,
                    side=side,
                    has_markers=has_markers,
                )
            )
        return tuple(sorted(found, key=lambda c: c.path))

    def _text_pair_has_markers(self, ours, theirs) -> bool:  # noqa: ANN001
        """양쪽이 있는 충돌의 워킹 트리 파일에 마커가 들어가는가.

        **blob에게 묻는다 — 워킹 트리 파일을 읽지 않는다.** 마커가 없는
        경우는 정확히 "libgit2가 3-way 텍스트 병합을 포기했을 때"이고,
        그 판단 기준이 곧 blob의 바이너리 여부다.

        **`repo.get(id)`은 blob 내용을 ODB에서 로드한다** — 그래서 이
        판정은 파일 크기에 비례하고, UI 스레드에서 부르면 G4를 넘긴다
        (감사 실측, DCR-002). 워커 문맥(`_collect_conflicts(classify=True)`)
        에서만 부를 것.
        """
        for entry in (ours, theirs):
            if entry is None:
                return False
            blob = self._repo.get(entry.id)
            if blob is None or getattr(blob, "is_binary", False):
                return False
        return True

    @staticmethod
    def _conflict_entry(index, path: str):  # noqa: ANN001, ANN205
        """인덱스에서 충돌 항목 하나를 꺼낸다. 없으면 조치를 실은 오류.

        `ConflictCollection`에는 `.get()`이 없어 첨자 접근이 유일한 길이고,
        없는 경로에는 KeyError를 던진다.
        """
        conflicts = index.conflicts
        if conflicts is not None:
            try:
                return conflicts[path]
            except KeyError:
                pass
        raise EngineError(
            trf("'{path}'은(는) 충돌 상태가 아닙니다.", path=path),
            action="목록을 새로 고친 뒤 다시 시도해 주세요.",
        )

    def conflict_detail(self, path: str) -> ConflictDetail:
        """충돌한 파일의 양쪽 내용을 꺼낸다.

        인덱스의 스테이지에서 읽는다 — 워킹 트리가 아니다. 워킹 트리에는
        마커가 섞인 결과물이 있거나(텍스트), 우리 것만 있거나(바이너리),
        아무것도 없을(삭제) 수 있어서 원본 두 개를 복원할 수 없다.
        """
        with _translate("충돌 내용 조회", partial_hint=self._partial):
            ancestor, ours, theirs = self._conflict_entry(
                self._fresh_index(), path
            )
            return ConflictDetail(
                path=path,
                side=_conflict_side(ancestor, ours, theirs),
                ours=self._side_content(ours),
                theirs=self._side_content(theirs),
            )

    def _side_content(self, entry) -> ConflictSideContent:  # noqa: ANN001
        """스테이지 항목 하나의 내용. 없으면 '존재하지 않음'으로 표시한다."""
        if entry is None:
            return ConflictSideContent(exists=False)
        blob = self._repo[entry.id]
        return ConflictSideContent(
            exists=True,
            data=bytes(blob.data),
            is_binary=bool(blob.is_binary),
        )

    def resolve_conflict(self, path: str, choice: ConflictChoice) -> None:
        """충돌 하나를 한쪽 선택으로 해결한다.

        **마커 없는 충돌을 해결할 수 있는 유일한 길이다.** 바이너리 충돌과
        삭제 계열 충돌은 워킹 트리에 마커가 없어서 "편집기로 정리하라"는
        기존 안내가 통하지 않는다 — 이 앱에서 해결할 방법이 없던 상태다.

        고른 쪽이 **없는** 경우(상대가 지웠는데 상대를 골랐다면)는 삭제를
        선택한 것이다. 그때는 충돌 항목만 지우면 인덱스에서 경로가 사라져
        git이 "삭제 스테이징"으로 읽는다 — `index.remove`를 부르면 오히려
        "stage 0에 없다"며 실패한다 (실측).
        """
        with _translate("충돌 해결"):
            index = self._fresh_index()
            _ancestor, ours, theirs = self._conflict_entry(index, path)
            chosen = ours if choice is ConflictChoice.OURS else theirs

            if chosen is not None:
                self._require_plain_file(path, chosen)

            target = Path(self._repo.workdir or "") / path
            del index.conflicts[path]
            if chosen is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bytes(self._repo[chosen.id].data))
                index.add(path)
                # **모드를 되살린다.** `index.add`는 워킹 트리를 다시 읽는데,
                # Windows에는 실행 비트가 없어 100755가 100644로 떨어진다 —
                # 스크립트가 조용히 실행 불가가 된다.
                entry = index[path]
                if entry.mode != chosen.mode:
                    index.remove(path)
                    index.add(pygit2.IndexEntry(path, entry.id, chosen.mode))
            index.write()

    def resolve_conflict_lines(self, path: str, choices: list[str]) -> None:
        """충돌 구획마다 내 것/상대 것/양쪽을 골라 합친다 (F3).

        워킹 트리의 마커 파일을 파싱해 조립한다 — git이 이미 합쳐둔 공통
        부분은 그대로 두고, 사람에게 넘어온 구획만 선택으로 채운다.
        마커가 훼손돼 있으면(사용자가 편집기로 손댐) 거부한다 — 반쯤 합친
        파일을 쓰는 것보다 낫고, 그 경우엔 편집기 경로로 마저 가면 된다.

        인코딩은 utf-8 + surrogateescape로 왕복한다 — 마커 판정에 필요한
        것은 ASCII 마커 줄뿐이고, 나머지 바이트는 그대로 보존된다.
        """
        from gitclient.domain.conflict_text import compose, parse_conflicted

        with _translate("충돌 해결"):
            index = self._fresh_index()
            _ancestor, ours, theirs = self._conflict_entry(index, path)
            keeper = ours or theirs
            if keeper is not None:
                self._require_plain_file(path, keeper)
            target = Path(self._repo.workdir or "") / path
            try:
                raw = target.read_bytes()
            except OSError as exc:
                raise EngineError(
                    trf("'{path}'을(를) 읽지 못했습니다.", path=path), detail=str(exc)
                ) from exc
            try:
                segments = parse_conflicted(
                    raw.decode("utf-8", "surrogateescape")
                )
                composed = compose(segments, choices)
            except ValueError as exc:
                raise EngineError(
                    trf("'{path}'의 충돌 마커를 읽을 수 없습니다.", path=path),
                    detail=str(exc),
                    action="파일을 이미 편집하셨다면 편집기에서 마커를 정리한 뒤 "
                    "스테이징해 주세요.",
                ) from exc
            target.write_bytes(composed.encode("utf-8", "surrogateescape"))
            del index.conflicts[path]
            index.add(path)
            # 모드 되살리기 — resolve_conflict와 같은 이유 (ADR-66).
            if keeper is not None:
                entry = index[path]
                if entry.mode != keeper.mode:
                    index.remove(path)
                    index.add(pygit2.IndexEntry(path, entry.id, keeper.mode))
            index.write()

    @staticmethod
    def _require_plain_file(path: str, entry) -> None:  # noqa: ANN001
        """평범한 파일만 이 방식으로 해결할 수 있다.

        심볼릭 링크(120000)는 blob 내용이 **링크 대상 경로**라 그대로 쓰면
        링크가 아니라 경로가 적힌 파일이 된다. 서브모듈(160000)의 id는
        blob이 아니라 **커밋**이라 `.data`를 읽는 것 자체가 틀렸다.
        조용히 망가뜨리느니 거부하고 무엇을 해야 하는지 알린다.
        """
        mode = entry.mode
        if mode == 0o120000:
            raise EngineError(
                trf("'{path}'은(는) 심볼릭 링크라 여기서 해결할 수 없습니다.", path=path),
                action="git CLI에서 `git checkout --ours/--theirs -- <경로>`로 "
                "해결해 주세요.",
            )
        if mode == 0o160000:
            raise EngineError(
                trf("'{path}'은(는) 서브모듈이라 여기서 해결할 수 없습니다.", path=path),
                action="서브모듈 디렉터리에서 원하는 커밋을 체크아웃한 뒤 "
                "상위 저장소에서 스테이징해 주세요.",
            )

    def abort_merge(self) -> None:
        """진행 중인 병합을 되돌린다.

        **병합이 건드린 경로만** 병합 이전으로 복구한다. 충돌 마커가 든
        파일도, 충돌 없이 이미 반영된 변경도 사라진다 — 그것이 "중단"의
        뜻이다. 하지만 병합과 무관한 파일의 커밋 안 된 작업은 **건드리지
        않는다**: git은 그런 변경을 둔 채로 병합을 시작하도록 허용하고
        `git merge --abort`도 그것을 보존한다(`reset --merge` 의미론).
        전면 `reset --hard`는 중단이 약속한 범위를 넘어 사용자 작업을
        파괴한다 — 리뷰에서 확정된 결함이다.

        **병합일 때만 되돌린다.** rebase·cherry-pick도 충돌로 멈추고
        인덱스에 충돌을 남기지만 그것은 우리가 시작한 병합이 아니다.
        `state_cleanup`으로 `.git/rebase-merge`를 지우면 `git rebase --abort`도
        `--continue`도 불가능해지고 HEAD는 브랜치 밖에 남는다.
        """
        with _translate("병합 중단"):
            state = self._repo.state()
            if state == pygit2.enums.RepositoryState.NONE:
                return  # 진행 중인 병합이 없다
            if state != pygit2.enums.RepositoryState.MERGE:
                # 조용히 넘어가지 않는다 — 파괴적 확인을 받은 직후의 무음
                # no-op은 그 자체로 또 다른 실패다.
                raise EngineError(
                    "진행 중인 작업이 병합이 아니어서 중단할 수 없습니다.",
                    detail=trf("저장소 상태: {state!r}", state=state),
                    action="rebase나 cherry-pick은 git CLI에서 "
                    "`git rebase --abort` 등으로 정리해 주세요.",
                )
            head = self._repo.head.target
            commit = self._repo[head].peel(pygit2.Commit)
            paths = self._merge_touched_paths(commit)
            if paths:
                # 빈 pathspec을 넘기면 libgit2가 "전부"로 해석해 워킹 트리
                # 전체를 덮어쓴다 — 고치려던 그 파괴가 그대로 돌아온다.
                self._repo.checkout_tree(
                    commit,
                    paths=paths,
                    strategy=pygit2.enums.CheckoutStrategy.FORCE,
                )
            self._repo.state_cleanup()
            self._repo.reset(head, pygit2.enums.ResetMode.MIXED)

    def _merge_touched_paths(self, head_commit: pygit2.Commit) -> list[str]:
        """병합이 손댄 경로 = 충돌한 경로 + 인덱스가 HEAD와 달라진 경로.

        git은 인덱스가 HEAD와 다르면 병합을 시작하지 않는다. 따라서 인덱스의
        모든 편차는 병합이 만든 것이고, 이 목록 밖은 전부 사용자 것이다.
        """
        paths: set[str] = set()
        index = self._fresh_index()
        conflicts = index.conflicts
        if conflicts is not None:
            for ancestor, ours, theirs in conflicts:
                entry = ours or theirs or ancestor
                if entry is not None:
                    paths.add(entry.path)
        for patch in head_commit.tree.diff_to_index(index):
            paths.add(patch.delta.new_file.path)
            paths.add(patch.delta.old_file.path)
        return sorted(paths)

    def _merge_message(self, source_ref: str) -> str:
        """git이 쓰는 것과 같은 형태의 머지 커밋 메시지."""
        name = source_ref
        for prefix in ("refs/remotes/", "refs/heads/"):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        head = self._head_branch()
        into = f" into {head}" if head and head != "main" else ""
        return f"Merge {name}{into}"

    def _head_branch(self) -> str | None:
        """현재 브랜치 이름. 분리된 HEAD면 None."""
        if self._repo.head_is_unborn or self._repo.head_is_detached:
            return None
        return self._repo.head.shorthand

    def _require_quiet_repository(self) -> None:
        """다른 작업이 진행 중이면 시작하지 않는다."""
        state = self._repo.state()
        if state != pygit2.enums.RepositoryState.NONE:
            raise EngineError(
                "이미 진행 중인 작업이 있습니다.",
                detail=trf("저장소 상태: {state!r}", state=state),
                action="진행 중인 병합이나 rebase를 마무리하거나 취소한 뒤 "
                "다시 시도해 주세요.",
            )

    def _require_expected_branch(
        self, head_branch: str | None, expected: str | None
    ) -> None:
        """실행 시점의 HEAD가 사용자가 보고 고른 그 브랜치인가.

        큐에 브랜치 전환이 먼저 들어 있으면 어긋난다. 문구가 "합치려"였을
        때는 병합만 이 검사를 썼는데, 지금은 리베이스·cherry-pick·revert·
        reset도 쓴다 — reset이 "합치려 했지만"이라고 말하면 사용자는 자기가
        하지 않은 작업의 오류를 읽는다.
        """
        if expected is None or head_branch == expected:
            return
        if head_branch is None:
            raise EngineError(
                "현재 브랜치가 아닌 곳(분리된 HEAD)에서는 할 수 없는 작업입니다.",
                detail=trf("기대한 브랜치: {expected}", expected=expected),
                action="브랜치를 체크아웃한 뒤 다시 시도해 주세요.",
            )
        raise EngineError(
            trf("'{expected}'에서 시작한 작업인데 현재 브랜치가 바뀌었습니다.", expected=expected),
            detail=trf("현재 브랜치: {head_branch}", head_branch=head_branch),
            action="브랜치를 확인한 뒤 다시 시도해 주세요.",
        )

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
                detail=trf("저장소 상태: {state!r}", state=state),
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
                trf("'{expected_branch}'에 합치려 했지만 현재 브랜치가 바뀌었습니다.", expected_branch=expected_branch),
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
                trf("커밋을 찾을 수 없습니다: {sha__12}", sha__12=sha[:12]),
                detail=str(exc),
                action="저장소가 외부에서 변경되었을 수 있습니다. 새로 고침(F5) 해보세요.",
            ) from exc

        if not isinstance(obj, pygit2.Commit):
            raise EngineError(trf("커밋이 아닙니다: {sha__12}", sha__12=sha[:12]))
        return obj

    # ------------------------------------------------------------------
    # 히스토리 재작성 (Phase 4 증분 3)
    #
    # **이 구획만 git CLI를 쓴다** — 나머지 로컬 경로는 전부 pygit2다.
    # 근거는 ADR-67. 요약하면: pygit2에 rebase가 아예 없고, 시퀀서 상태
    # (`.git/rebase-merge/`, `.git/sequencer/`)는 git 자신만 완전히 이해한다.
    # 시작은 pygit2로 하고 마무리는 CLI로 하는 식의 혼합이 가장 위험하다.
    # 프로세스 기동 20~40ms는 rebase가 하는 일에 비하면 반올림 오차다.
    # ------------------------------------------------------------------

    def current_operation(self) -> RepoOperation:
        """지금 저장소에 진행 중인 히스토리 연산.

        `state()`의 rebase 계열이 셋(REBASE / REBASE_INTERACTIVE /
        REBASE_MERGE)인 이유는 백엔드가 셋이기 때문이지 사용자에게 다른
        일이어서가 아니다. 실측한 git은 평범한 `git rebase <upstream>`에도
        REBASE_INTERACTIVE(8)를 남긴다 — merge 백엔드가 기본이라
        `interactive` 파일을 쓴다. 하나로 접지 않으면 "리베이스 중"을 놓친다.
        """
        with _translate("저장소 상태 조회"):
            return _STATE_TO_OPERATION.get(self._repo.state(), RepoOperation.OTHER)

    def operation_state(
        self, *, conflict_count: int | None = None
    ) -> OperationState:
        """진행 중인 연산의 배너용 요약.

        UI 스레드에서 불린다.

        **`conflict_count`를 받는 이유는 순전히 예산 때문이다.** 호출부가
        방금 `index_conflicts()`를 불렀다면 충돌 목록을 이미 만든 것이고,
        여기서 또 만들면 같은 일을 두 번 한다. 실측에서 충돌 100개짜리
        저장소의 sync 한 번이 71ms였다 — G4(50ms)를 43% 넘긴다. 중복만
        걷어내면 절반이 돌아온다.
        """
        with _translate("진행 중 작업 조회"):
            operation = self.current_operation()
            if operation is RepoOperation.NONE:
                return OperationState()
            step, total, branch = self._rebase_progress()
            if conflict_count is None:
                conflict_count = len(self._collect_conflicts())
            return OperationState(
                operation=operation,
                step=step,
                total=total,
                head_branch=branch or self._head_branch(),
                conflict_count=conflict_count,
            )

    def _rebase_progress(self) -> tuple[int | None, int | None, str | None]:
        """rebase 진행 정보 (현재/전체/원래 브랜치).

        `.git/rebase-merge/`의 msgnum·end·head-name에서 읽는다. rebase는
        HEAD를 분리시키므로 **`repo.head`로는 원래 브랜치를 알 수 없다** —
        배너에 "→ topic"을 띄우려면 이 파일이 유일한 출처다.

        파일이 없거나 읽히지 않으면 조용히 None을 준다. 진행 표시가 없는
        것은 불편할 뿐이지만, 여기서 던지면 배너 자체가 사라진다.
        """
        git_dir = Path(self._repo.path)
        for name, current, total in _REBASE_PROGRESS_FILES:
            base = git_dir / name
            if not base.is_dir():
                continue
            return (
                _read_state_number(base, current),
                _read_state_number(base, total),
                _shorthand_of(_read_state_file(base, "head-name")),
            )
        return None, None, None

    def rebase(
        self, upstream_ref: str, *, expected_branch: str | None = None
    ) -> HistoryOutcome:
        """현재 브랜치의 커밋들을 upstream 위로 옮겨 심는다.

        **커밋 안 된 변경은 git이 지켜준다.** 실측에서 세 연산 모두 더러운
        워킹 트리를 거부하고 파일을 그대로 남겼다. 우리가 앞서 검사를 덧붙이면
        git보다 부정확한 판단을 하나 더 얹는 것일 뿐이라 하지 않는다 —
        거부 사유는 git의 stderr를 그대로 전한다.
        """
        self._require_quiet_repository()
        self._require_expected_branch(self._head_branch(), expected_branch)
        # `--empty=stop`: 적용 시점에 비게 되는 커밋을 git이 조용히 버리는
        # 대신 멈춰 세운다 (ADR-76). 이미 upstream에 그대로 있는 커밋의
        # 생략(clean cherry-pick)은 이 옵션과 무관하게 계속 일어나며,
        # 그쪽은 경고 파싱(`skipped_already_applied`)으로 안내한다.
        return self._sequencer(
            ["rebase", "--empty=stop", "--", upstream_ref],
            context="리베이스",
            expected=RepoOperation.REBASE,
        )

    def rebase_todo(self, upstream_ref: str) -> tuple[RebaseStep, ...]:
        """`upstream_ref` 위로 옮길 커밋들 — **오래된 것부터** (FR-16).

        git의 todo 순서와 같다: 위에서 아래로 적용된다. 화면이 그 순서를
        그대로 보여줘야 사용자가 짠 계획과 git이 실행할 계획이 일치한다.
        """
        with _translate("리베이스 계획 조회"):
            head = self._repo.head.target
            upstream = self._repo.revparse_single(upstream_ref).peel(
                pygit2.Commit
            )
            walker = self._repo.walk(head, pygit2.enums.SortMode.TOPOLOGICAL)
            walker.hide(upstream.id)
            steps = [
                RebaseStep(
                    sha=str(commit.id),
                    action=RebaseAction.PICK,
                    summary=commit.message.splitlines()[0]
                    if commit.message
                    else "",
                )
                for commit in walker
            ]
            steps.reverse()  # git todo와 같은 순서(오래된 것부터)
            return tuple(steps)

    def rebase_interactive(
        self,
        upstream_ref: str,
        steps: list[RebaseStep],
        *,
        expected_branch: str | None = None,
    ) -> HistoryOutcome:
        """사용자가 짠 계획대로 리베이스한다 (ADR-86).

        **계획은 `GIT_SEQUENCE_EDITOR`로 주입한다** — git이 todo 파일을
        열려고 부르는 자리에 "우리 파일을 그 자리에 복사하라"를 넣는다.
        todo를 손으로 `.git/rebase-merge/`에 쓰는 방법도 있지만, 그건 git
        내부 구조를 우리가 흉내 내는 것이라 버전마다 깨진다(픽스처에서
        이미 배운 교훈). 편집기 자리는 git이 공개적으로 약속한 접점이다.

        커밋 메시지 편집기(`GIT_EDITOR`)는 계속 막아둔다 — squash의 합쳐진
        메시지는 git이 양쪽을 이어 붙여 자동으로 만든다(실측). GUI에서
        메시지를 다시 쓰고 싶으면 그건 amend의 일이다.
        """
        self._require_quiet_repository()
        self._require_expected_branch(self._head_branch(), expected_branch)
        kept = [step for step in steps if step.action is not RebaseAction.DROP]
        if not kept:
            raise EngineError(
                "모든 커밋을 버리는 계획입니다.",
                action="적어도 하나는 남겨 주세요. 브랜치를 통째로 되돌리려면 "
                "'되돌리기(reset)'를 쓰는 편이 분명합니다.",
            )
        if kept[0].action in (RebaseAction.SQUASH, RebaseAction.FIXUP):
            # git이 "cannot squash without a previous commit"으로 거부하는
            # 계획이다. 우리가 먼저 막으면 사용자는 무엇이 잘못됐는지
            # 화면에서 바로 안다.
            raise EngineError(
                "첫 커밋은 앞 커밋에 합칠 수 없습니다.",
                action="맨 위 커밋은 '그대로 두기'여야 합니다.",
            )

        todo = "".join(
            f"{step.action.value} {step.sha}\n"
            for step in steps
            if step.action is not RebaseAction.DROP
        )
        with tempfile.TemporaryDirectory(prefix="gitclient-todo-") as tmp:
            todo_path = Path(tmp) / "todo"
            todo_path.write_text(todo, encoding="utf-8")
            # 경로에 공백이 있어도 셸이 한 인자로 읽도록 따옴표로 싼다.
            # git은 이 문자열 뒤에 todo 파일 경로를 붙여 셸로 실행한다.
            posix = str(todo_path).replace("\\", "/")
            return self._sequencer(
                ["rebase", "-i", "--empty=stop", "--", upstream_ref],
                context="리베이스",
                expected=RepoOperation.REBASE,
                extra_env={"GIT_SEQUENCE_EDITOR": f'cp "{posix}"'},
            )

    def cherry_pick(
        self, sha: str, *, expected_branch: str | None = None
    ) -> HistoryOutcome:
        """다른 곳의 커밋 하나를 현재 브랜치 위에 다시 만든다."""
        self._require_quiet_repository()
        self._require_expected_branch(self._head_branch(), expected_branch)
        commit = self._lookup_commit(sha)
        return self._sequencer(
            ["cherry-pick", "--no-edit", str(commit.id)],
            context="커밋 가져오기",
            expected=RepoOperation.CHERRY_PICK,
        )

    def revert(
        self, sha: str, *, expected_branch: str | None = None
    ) -> HistoryOutcome:
        """커밋 하나의 변경을 뒤집는 새 커밋을 만든다.

        히스토리를 고치지 않는다 — 되돌리는 커밋을 **앞에 덧붙인다.**
        이미 공유된 커밋을 무르는 유일한 안전한 방법이다.
        """
        self._require_quiet_repository()
        self._require_expected_branch(self._head_branch(), expected_branch)
        commit = self._lookup_commit(sha)
        return self._sequencer(
            ["revert", "--no-edit", str(commit.id)],
            context="되돌리기",
            expected=RepoOperation.REVERT,
        )

    def continue_operation(self) -> HistoryOutcome:
        """해결을 마친 연산을 이어서 진행한다.

        **먼저 남은 충돌을 확인한다.** git도 거부하긴 하지만 그 메시지는
        영어 한 줄이고, 우리는 몇 개가 남았는지 알고 있다. 사용자가
        "계속"을 눌렀는데 아무 일도 일어나지 않는 것이 최악이다.

        **그다음, 이대로 진행하면 커밋이 사라지는지 확인한다** (ADR-76).
        충돌을 전부 한쪽으로 해결해 스테이징 트리가 HEAD와 같아졌다면,
        `git rebase --continue`는 그 커밋을 **조용히 버리고 성공을 보고한다**
        (실측 — `--empty=stop`도 이 경로는 못 막는다. cherry-pick은 같은
        상황에서 스스로 거부하는데 rebase만 버린다). 트리 비교는 정의상
        참인 판정이라 거짓 경고가 없다: 스테이징된 트리가 HEAD와 같다는
        것이 곧 "재생될 커밋에 남는 변경이 없다"는 뜻이다.
        """
        operation = self._require_continuable()
        remaining = self._collect_conflicts()
        if remaining:
            raise EngineError(
                trf("아직 해결되지 않은 충돌이 {len_remaining}개 있습니다.", len_remaining=len(remaining)),
                detail=", ".join(c.path for c in remaining[:5]),
                action="충돌 목록에서 각 파일을 해결한 뒤 다시 시도해 주세요.",
            )
        if self._staged_tree_equals_head():
            return HistoryOutcome(
                kind=HistoryOutcomeKind.WOULD_BE_EMPTY,
                operation=operation,
                message=self._stalled_commit_summary(),
            )
        return self._sequencer(
            [_SEQUENCER_COMMAND[operation], "--continue"],
            context=f"{operation.label} 계속",
            expected=operation,
        )

    def keep_empty_operation(self) -> HistoryOutcome:
        """비게 된 커밋을 **빈 커밋으로 남기고** 연산을 이어간다 (ADR-76).

        `git commit --allow-empty --no-edit`가 시퀀서가 둔 메시지를 그대로
        집어 커밋을 만든다 (실측 — rebase·cherry-pick·revert 모두).
        cherry-pick·revert는 그 커밋이 연산을 끝내고, rebase는 남은 커밋을
        `--continue`로 이어간다.
        """
        operation = self._require_continuable()
        remaining = self._collect_conflicts()
        if remaining:
            raise EngineError(
                trf("아직 해결되지 않은 충돌이 {len_remaining}개 있습니다.", len_remaining=len(remaining)),
                detail=", ".join(c.path for c in remaining[:5]),
                action="충돌 목록에서 각 파일을 해결한 뒤 다시 시도해 주세요.",
            )
        if not self._staged_tree_equals_head():
            # 남길 변경이 있다면 이 함수가 아니라 '계속'의 일이다. 여기서
            # 커밋을 만들면 같은 변경이 두 커밋으로 갈라진다.
            raise EngineError(
                "이 커밋에는 남길 변경이 있습니다.",
                action="'계속'으로 이어가 주세요.",
            )
        result = self._run_git(
            ["commit", "--allow-empty", "--no-edit"],
            context=f"{operation.label} 빈 커밋 유지",
            check=False,
        )
        if result.returncode != 0:
            raise EngineError(
                "빈 커밋을 만들지 못했습니다.",
                detail=_message_of(result) or f"exit {result.returncode}",
                action="위 내용을 확인한 뒤 다시 시도해 주세요.",
            )
        if not self.current_operation().is_active:
            # cherry-pick·revert는 커밋이 곧 마무리다 (실측: CHERRY_PICK_HEAD가
            # 커밋과 함께 사라진다). `--continue`를 또 부르면 "진행 중인
            # 작업이 없다"는 오류만 돌아온다.
            return HistoryOutcome(
                kind=HistoryOutcomeKind.COMPLETED,
                operation=operation,
                message=_message_of(result),
            )
        return self._sequencer(
            [_SEQUENCER_COMMAND[operation], "--continue"],
            context=f"{operation.label} 계속",
            expected=operation,
        )

    def _staged_tree_equals_head(self) -> bool:
        """스테이징된 트리가 HEAD의 트리와 같은가 — 커밋하면 빈 커밋인가.

        인덱스 크기에 비례하는 로컬 계산이다(밀리초 수준). 워커 스레드
        (WriteQueue)에서만 불린다.
        """
        with _translate("스테이징 상태 확인"):
            index = self._fresh_index()
            if index.conflicts is not None:
                return False  # 충돌이 남은 인덱스는 write_tree가 실패한다
            staged = index.write_tree(self._repo)
            head = self._repo.head.peel(pygit2.Commit).tree_id
            return staged == head

    def skip_operation(self) -> HistoryOutcome:
        """지금 멈춰 있는 커밋을 **버리고** 다음으로 넘어간다.

        파괴적이다 — 재생 중이던 커밋의 변경이 결과에 남지 않는다.
        호출 전에 §5.2의 확인 절차를 거칠 것.
        """
        operation = self._require_continuable()
        return self._sequencer(
            [_SEQUENCER_COMMAND[operation], "--skip"],
            context=f"{operation.label} 건너뛰기",
            expected=operation,
        )

    def abort_operation(self) -> None:
        """진행 중인 연산을 통째로 되돌린다.

        병합은 `abort_merge()`에 맡긴다. 그쪽은 **병합이 건드린 경로만**
        복구해 무관한 작업을 지키는데, 그 조심스러움은 pygit2로 직접
        구현한 것이라 CLI 경로로 대체하면 잃는다.
        """
        operation = self.current_operation()
        if operation is RepoOperation.NONE:
            return
        if operation is RepoOperation.MERGE:
            self.abort_merge()
            return
        if operation not in _SEQUENCER_COMMAND:
            raise EngineError(
                "이 작업은 앱에서 중단할 수 없습니다.",
                detail=trf("저장소 상태: {self__repo_state!r}", self__repo_state=self._repo.state()),
                action="git CLI에서 정리해 주세요.",
            )
        result = self._run_git(
            [_SEQUENCER_COMMAND[operation], "--abort"],
            context=f"{operation.label} 중단",
            check=False,
        )
        if result.returncode != 0:
            # 중단은 다른 실패의 **탈출구**로 안내되는 경로다(타임아웃 메시지가
            # 그렇게 말한다). 그 탈출구가 막혔을 때 조치를 주지 않으면
            # 사용자는 앱 안에서 갈 곳이 없다.
            raise EngineError(
                trf("{operation_label} 중단에 실패했습니다.", operation_label=operation.label),
                detail=_message_of(result) or f"exit {result.returncode}",
                action=trf("다른 git 프로세스가 저장소를 쓰고 있을 수 있습니다. 잠시 후 다시 시도하거나, 계속 실패하면 터미널에서 `git {sequencer_command_op} --abort`로 정리해 주세요.", sequencer_command_op=_SEQUENCER_COMMAND[operation]),
            )

    def reset_to(
        self, sha: str, kind: ResetKind, *, expected_branch: str | None = None
    ) -> None:
        """현재 브랜치를 다른 커밋으로 옮긴다.

        `HARD`는 **커밋하지 않은 작업을 지운다.** 되돌릴 방법이 없으므로
        호출 전에 §5.2의 확인 절차가 필요하다. 커밋 자체는 reflog에 남지만
        워킹 트리는 어디에도 남지 않는다 — 그 비대칭이 이 연산의 위험이다.

        진행 중인 연산 위에서는 거부한다. rebase 도중 reset은 시퀀서가
        기대하는 HEAD를 어긋내 `--continue`도 `--abort`도 못 하게 만든다.

        **브랜치를 대조한다.** 큐에 브랜치 전환이 먼저 들어 있으면 이 작업이
        실행될 때 HEAD가 이미 다른 곳이다 — 확인창은 `main`을 말했는데
        `feature`가 옮겨진다. 다른 파괴적 연산은 되돌릴 여지라도 있지만
        `HARD`는 커밋 안 된 작업을 지우고 그것은 reflog에도 없다.
        """
        self._require_quiet_repository()
        self._require_expected_branch(self._head_branch(), expected_branch)
        with _translate("되돌리기"):
            commit = self._lookup_commit(sha)
            self._repo.reset(commit.id, _RESET_MODES[kind])

    # -- 내부 -----------------------------------------------------------

    def _require_continuable(self) -> RepoOperation:
        operation = self.current_operation()
        if operation not in _SEQUENCER_COMMAND:
            raise EngineError(
                "이어서 진행할 작업이 없습니다.",
                detail=trf("저장소 상태: {self__repo_state!r}", self__repo_state=self._repo.state()),
                action="병합은 '계속'이 아니라 커밋으로 마무리합니다.",
            )
        return operation

    def _sequencer(
        self,
        args: list[str],
        *,
        context: str,
        expected: RepoOperation,
        extra_env: dict[str, str] | None = None,
    ) -> HistoryOutcome:
        """시퀀서 명령 하나를 돌리고 **저장소 상태로** 결과를 판정한다.

        종료 코드만 보면 안 된다. rebase는 충돌로 멈출 때도 1을 주고
        더러운 워킹 트리로 거부할 때도 1을 준다 — 전자는 정상 흐름이고
        후자는 오류다. 둘을 가르는 것은 `state()`다: 멈췄다면 연산이
        진행 중으로 남고, 거부됐다면 NONE 그대로다.
        """
        result = self._run_git(
            args, context=context, check=False, extra_env=extra_env
        )
        state = self.current_operation()

        if state.is_active:
            # 여기는 워커 스레드다(§3.3 규칙 3) — 마커 분류(blob 로드)를
            # 지금 끝내야 UI가 목록을 받았을 때 읽을 것이 없다 (ADR-77).
            conflicts = self._collect_conflicts(classify=True)
            if not conflicts and _looks_empty(result):
                # 충돌 0개인데 멈춰 있다 = 이 커밋에 남길 변경이 없다는
                # 뜻이다. 오류가 아니다 — 버릴지, 빈 커밋으로 남길지,
                # 중단할지는 사용자가 고른다 (ADR-76). 예전에는 여기서
                # 오류를 던져 '건너뛰기'를 안내했는데, 그 길뿐이라면
                # 선택이 아니라 통보다.
                return HistoryOutcome(
                    kind=HistoryOutcomeKind.WOULD_BE_EMPTY,
                    operation=state,
                    message=self._stalled_commit_summary()
                    or _message_of(result),
                    skipped_already_applied=_skipped_commits_of(result),
                )
            return HistoryOutcome(
                kind=HistoryOutcomeKind.CONFLICTED,
                operation=state,
                conflicts=conflicts,
                message=_message_of(result),
                skipped_already_applied=_skipped_commits_of(result),
            )

        if result.returncode != 0:
            raise EngineError(
                trf("{context}에 실패했습니다.", context=context),
                detail=_message_of(result) or f"exit {result.returncode}",
                action="위 내용을 확인한 뒤 다시 시도해 주세요.",
            )
        return HistoryOutcome(
            kind=HistoryOutcomeKind.COMPLETED,
            operation=expected,
            message=_message_of(result),
            skipped_already_applied=_skipped_commits_of(result),
        )

    def _stalled_commit_summary(self) -> str:
        """지금 멈춰 있는 커밋의 메시지 첫 줄. 못 읽으면 빈 문자열.

        "어느 커밋이 비게 되는가"에 답하기 위한 것이다 — rebase는
        `.git/rebase-merge/message`에, cherry-pick·revert는 `.git/MERGE_MSG`에
        재생 중인 커밋의 메시지를 둔다 (실측).
        """
        base = Path(self._repo.path)
        for name in ("rebase-merge/message", "MERGE_MSG"):
            text = _read_state_file(base, name)
            if text:
                return text.splitlines()[0]
        return ""

    def _run_git(
        self,
        args: list[str],
        *,
        context: str,
        check: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """저장소 안에서 git 명령 하나를 돌린다. 워커 스레드 전용.

        **편집기를 환경 변수로 막는다.** `-c core.editor=true`로는 부족하다 —
        GIT_EDITOR가 설정에 우선하므로, 사용자 셸에 `GIT_EDITOR=vim`이 있으면
        `--continue`가 터미널 없는 GUI 프로세스에서 vim을 띄우려다 워커
        스레드째로 멈춘다. 시퀀스 편집기도 같은 이유로 함께 막는다.

        시스템·전역 설정은 **걷어내지 않는다.** 여기서 만들어지는 것은
        사용자의 커밋이라 user.name·user.email이 필요하고,
        merge.conflictStyle이나 rerere도 사용자가 정한 대로 도는 것이 맞다.
        (원격 경로가 설정을 통제하는 것은 계측 기준선을 지키기 위해서다.)
        """
        workdir = self._repo.workdir
        if not workdir:
            raise EngineError(
                "워킹 트리가 없는 저장소에서는 할 수 없는 작업입니다.",
                action="bare 저장소가 아닌 사본에서 시도해 주세요.",
            )
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in _HISTORY_ENV_BLOCKLIST
        }
        env["GIT_EDITOR"] = "true"
        env["GIT_SEQUENCE_EDITOR"] = "true"
        env["GIT_TERMINAL_PROMPT"] = "0"
        # **메시지는 영어로 고정한다.** `_looks_equal`이 아니라 `_looks_empty`가
        # git의 문구로 "커밋할 것이 없다"를 알아본다 — 번역된 git에서는 그
        # 판정이 무너져 안내가 일반 오류로 떨어진다. 원격 경로가 진행률
        # 파싱을 위해 하는 것과 같은 이유다. 로케일은 config가 아니므로
        # "사용자 설정을 살린다"는 결정과 부딪히지 않는다.
        env["LC_ALL"] = "C"
        # (이 줄은 변이 검증으로 지킬 수 없다 — 개발 환경의 git에 번역 카탈로그가
        # 없어 지워도 테스트가 붉어지지 않는다. 근거는 실측이 아니라 원격
        # 경로에서 같은 이유로 이미 확인된 것이다.)
        if extra_env:
            # 인터랙티브 rebase만 이 문을 쓴다 — 사용자가 화면에서 짠 계획을
            # GIT_SEQUENCE_EDITOR로 주입한다 (ADR-86). 위의 편집기 차단을
            # **덮어쓰는 유일한 경로**이며, 값은 우리가 만든 파일 복사
            # 명령뿐이다.
            env.update(extra_env)
        command = [self._git, "-C", workdir, *args]
        started_at = time.perf_counter()
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=_HISTORY_TIMEOUT_S,
            )
            # 투명성 로그 (FR-11) — 원격 경로와 같은 창구에 남긴다.
            COMMAND_LOG.record(
                command,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                returncode=result.returncode,
            )
        except subprocess.TimeoutExpired as exc:
            COMMAND_LOG.record(
                command,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                returncode=None,
            )
            # git은 상태 파일을 원자적으로 쓰므로 죽여도 저장소는 일관적이다.
            # 다만 연산이 진행 중으로 남을 수 있어 빠져나갈 길을 알려준다.
            raise EngineError(
                trf("{context}이(가) {_HISTORY_TIMEOUT_S}초 안에 끝나지 않았습니다.", context=context, _HISTORY_TIMEOUT_S=_HISTORY_TIMEOUT_S),
                detail=str(exc),
                action="작업이 진행 중으로 남아 있다면 '중단'으로 되돌릴 수 있습니다.",
            ) from exc
        except OSError as exc:
            raise EngineError(
                "git 실행에 실패했습니다.",
                detail=str(exc),
                action="git이 설치되어 있고 PATH에 있는지 확인해 주세요.",
            ) from exc

        if check and result.returncode != 0:
            raise EngineError(
                trf("{context}에 실패했습니다.", context=context),
                detail=_message_of(result) or f"exit {result.returncode}",
            )
        return result
