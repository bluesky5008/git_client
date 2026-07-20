"""히스토리 재작성 — rebase / cherry-pick / revert / reset (Phase 4 증분 3).

**이 증분의 존재 이유는 ADR-65다.** 증분 2는 rebase·cherry-pick 충돌을
일부러 감췄다. 스테이지 2/3의 주체가 rebase에서 병합과 반대라, 병합 기준
라벨을 그대로 쓰면 "내 것 사용"이 사용자 자신의 커밋을 버렸기 때문이다 —
그리고 `rebase --continue`가 **성공으로 끝나** 손실이 조용했다.

여기서 검증하는 것:
  1. 연산마다 스테이지 2/3에 실제로 누가 오는가 (라벨의 근거)
  2. 그 라벨이 실제 내용과 맞는가 — **손실을 재현하는 회귀 테스트**
  3. 멈춤·계속·건너뛰기·중단이 저장소를 어디에 남기는가
  4. 종료 코드가 아니라 상태로 "멈춤"과 "거부"를 가르는가
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.errors import EngineError
from gitclient.domain.models import (
    ConflictChoice,
    HistoryOutcomeKind,
    RepoOperation,
    ResetKind,
)
from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.integration.remote_harness import AUTHOR_ENV, git

ESC = chr(27)
"""ANSI 이스케이프의 시작 바이트. 소스에 직접 적으면 편집 도구를 거치며
망가지기 쉬워 코드로 만든다."""

MINE = "내가-쓴-줄\n"
THEIRS = "상대가-쓴-줄\n"


def write(path: Path, text: str) -> None:
    """줄바꿈을 번역하지 않고 쓴다.

    `Path.write_text`는 윈도우에서 LF를 CRLF로 바꾼다. 그러면 git이 CRLF를
    그대로 저장하고, 인덱스 스테이지에서 꺼낸 내용과 테스트가 적은 기대값이
    달라진다 — 실제 결함이 아니라 **테스트 하네스가 만든 차이**다.
    """
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


@pytest.fixture
def diverged(tmp_path: Path) -> Path:
    """main과 topic이 같은 줄을 다르게 고친 저장소. HEAD는 topic.

    topic의 커밋이 **사용자 자신의 것**이고 main의 커밋이 남의 것이다.
    이 구분이 아래 모든 판정의 기준이다.
    """
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    commit_all(root, "base")

    git("checkout", "--quiet", "-b", "topic", cwd=root)
    write(root / "f.txt", MINE)
    commit_all(root, "topic-commit")

    git("checkout", "--quiet", "main", cwd=root)
    write(root / "f.txt", THEIRS)
    commit_all(root, "main-commit")

    git("checkout", "--quiet", "topic", cwd=root)
    return root


def subjects(repo: Path) -> list[str]:
    return git("log", "--format=%s", cwd=repo).stdout.splitlines()


# ----------------------------------------------------------------------
# 1. 라벨의 근거 — 스테이지 2/3에 실제로 누가 오는가
# ----------------------------------------------------------------------


def test_rebase_stages_are_inverted_relative_to_merge(diverged: Path) -> None:
    """rebase의 stage 2는 **올라탈 곳**, stage 3이 **내 커밋**이다.

    이것이 ADR-65의 사실 근거다. 이 관계가 뒤집히면 라벨도 뒤집혀야 하므로
    가정 자체를 못 박아 둔다.
    """
    engine = LocalGitEngine.open(diverged)
    outcome = engine.rebase("main")

    assert outcome.kind is HistoryOutcomeKind.CONFLICTED
    detail = engine.conflict_detail("f.txt")
    assert detail.ours.text == THEIRS, "stage 2는 올라탈 곳(main)이어야 한다"
    assert detail.theirs.text == MINE, "stage 3은 재생 중인 내 커밋이어야 한다"


def test_cherry_pick_stages_follow_merge_orientation(diverged: Path) -> None:
    """cherry-pick은 뒤집히지 않는다 — stage 2가 현재 브랜치다.

    "충돌은 다 같다"고 뭉뚱그리면 rebase에 맞춰 전부 뒤집는 잘못된 수정을
    하게 된다. 뒤집히는 것은 rebase 하나뿐이다.
    """
    git("checkout", "--quiet", "main", cwd=diverged)
    engine = LocalGitEngine.open(diverged)
    topic = git("rev-parse", "topic", cwd=diverged).stdout.strip()

    outcome = engine.cherry_pick(topic)

    assert outcome.kind is HistoryOutcomeKind.CONFLICTED
    detail = engine.conflict_detail("f.txt")
    assert detail.ours.text == THEIRS, "stage 2는 현재 브랜치(main)"
    assert detail.theirs.text == MINE, "stage 3은 가져오는 커밋(topic)"


def test_labels_match_the_content_they_name(diverged: Path) -> None:
    """**회귀 테스트.** 라벨이 가리키는 쪽에 정말 그 내용이 있는가.

    증분 2의 손실은 이 한 줄로 요약된다: 화면이 "내 것"이라고 부른 쪽에
    사용자의 것이 없었다. 라벨과 내용을 함께 확인하지 않으면 같은 결함이
    다른 모습으로 돌아온다.
    """
    engine = LocalGitEngine.open(diverged)
    engine.rebase("main")

    state = engine.operation_state()
    detail = engine.conflict_detail("f.txt")

    assert state.operation is RepoOperation.REBASE
    assert "내 커밋" in state.labels.theirs
    assert detail.theirs.text == MINE, (
        "'재생 중인 내 커밋'이라고 부른 쪽에 사용자의 내용이 있어야 한다"
    )
    assert "upstream" in state.labels.ours
    assert detail.ours.text == THEIRS
    assert state.labels.note, "방향이 반대라는 사실을 화면이 말해야 한다"


def test_taking_the_labelled_side_keeps_that_content(diverged: Path) -> None:
    """라벨대로 고르면 그 내용이 남는가 — 손실 시나리오를 끝까지 재현한다.

    사용자가 "재생 중인 내 커밋"(=theirs) 쪽을 고르고 이어가면 결과 커밋에
    자신의 내용이 있어야 한다. 옛 구현에서는 화면이 그것을 "상대 것"이라
    불러 사용자가 반대쪽을 골랐고, 커밋이 조용히 사라졌다.
    """
    engine = LocalGitEngine.open(diverged)
    engine.rebase("main")

    engine.resolve_conflict("f.txt", ConflictChoice.THEIRS)
    outcome = engine.continue_operation()

    assert outcome.kind is HistoryOutcomeKind.COMPLETED
    assert engine.current_operation() is RepoOperation.NONE
    assert (diverged / "f.txt").read_text(encoding="utf-8") == MINE
    assert subjects(diverged) == ["topic-commit", "main-commit", "base"]


# ----------------------------------------------------------------------
# 2. 충돌을 감추지 않는가 (ADR-65 해제)
# ----------------------------------------------------------------------


def test_rebase_conflicts_are_visible(diverged: Path) -> None:
    """rebase 충돌이 목록에 나온다. 옛 구현은 빈 튜플을 줬다."""
    engine = LocalGitEngine.open(diverged)
    engine.rebase("main")

    assert [c.path for c in engine.index_conflicts()] == ["f.txt"]
    assert engine.merge_conflicts() == (), "병합 전용 경로는 여전히 병합만 본다"


def test_abort_merge_still_refuses_a_rebase(diverged: Path) -> None:
    """`abort_merge()`는 rebase를 건드리지 않는다.

    상태 확인을 지우면 `state_cleanup`이 `.git/rebase-merge`를 날려
    `--continue`도 `--abort`도 불가능해지고 HEAD가 브랜치 밖에 남는다.
    """
    engine = LocalGitEngine.open(diverged)
    engine.rebase("main")

    with pytest.raises(EngineError):
        engine.abort_merge()
    assert engine.current_operation() is RepoOperation.REBASE


# ----------------------------------------------------------------------
# 3. 멈춤 / 계속 / 건너뛰기 / 중단
# ----------------------------------------------------------------------


def test_abort_returns_to_the_original_branch(diverged: Path) -> None:
    """중단하면 rebase 이전으로 — 분리된 HEAD로 남지 않는다."""
    engine = LocalGitEngine.open(diverged)
    engine.rebase("main")
    assert engine.operation_state().head_branch == "topic"

    engine.abort_operation()

    assert engine.current_operation() is RepoOperation.NONE
    assert engine.info().head_shorthand == "topic"
    assert subjects(diverged) == ["topic-commit", "base"]


def test_skip_discards_the_replayed_commit(diverged: Path) -> None:
    """건너뛰기는 재생 중이던 커밋을 버린다 — 파괴적이라는 뜻이다."""
    engine = LocalGitEngine.open(diverged)
    engine.rebase("main")

    outcome = engine.skip_operation()

    assert outcome.kind is HistoryOutcomeKind.COMPLETED
    assert subjects(diverged) == ["main-commit", "base"]


def test_continue_refuses_while_conflicts_remain(diverged: Path) -> None:
    """해결하지 않고 계속하면 몇 개가 남았는지 말해 준다.

    git도 거부하지만 영어 한 줄이고, 아무 일도 일어나지 않는 것처럼 보인다.
    """
    engine = LocalGitEngine.open(diverged)
    engine.rebase("main")

    with pytest.raises(EngineError) as caught:
        engine.continue_operation()

    assert "1개" in str(caught.value)
    assert engine.current_operation() is RepoOperation.REBASE


def test_continue_reports_an_empty_result_with_a_way_out(diverged: Path) -> None:
    """해결 결과가 이미 있는 내용과 같으면 커밋할 것이 없다.

    git은 rc=1에 상태를 유지해 진짜 오류와 구분되지 않는다. 안내가 없으면
    사용자는 "계속"과 오류 사이를 무한히 오간다.
    """
    git("checkout", "--quiet", "main", cwd=diverged)
    engine = LocalGitEngine.open(diverged)
    topic = git("rev-parse", "topic", cwd=diverged).stdout.strip()
    engine.cherry_pick(topic)

    engine.resolve_conflict("f.txt", ConflictChoice.OURS)  # 현재 브랜치 그대로
    with pytest.raises(EngineError) as caught:
        engine.continue_operation()

    assert "건너뛰기" in (caught.value.action or "")


def test_continue_has_nothing_to_do_outside_a_sequencer(diverged: Path) -> None:
    """병합은 '계속'으로 마무리하지 않는다 — 그냥 커밋한다."""
    engine = LocalGitEngine.open(diverged)
    with pytest.raises(EngineError) as caught:
        engine.continue_operation()
    assert "커밋" in (caught.value.action or "")


# ----------------------------------------------------------------------
# 4. 종료 코드가 아니라 상태로 판정하는가
# ----------------------------------------------------------------------


def test_dirty_worktree_is_an_error_not_a_conflict(diverged: Path) -> None:
    """더러운 워킹 트리 거부는 오류다 — 둘 다 rc=1이지만 상태가 다르다.

    "rc가 1이면 충돌"로 판정하면 시작조차 못 한 rebase를 "충돌 중"으로
    보여주고, 사용자는 존재하지 않는 충돌을 해결하려 든다.
    """
    write(diverged / "f.txt", "커밋 안 한 수정\n")
    engine = LocalGitEngine.open(diverged)

    with pytest.raises(EngineError):
        engine.rebase("main")

    assert engine.current_operation() is RepoOperation.NONE
    assert (diverged / "f.txt").read_text(encoding="utf-8") == "커밋 안 한 수정\n", (
        "거부된 rebase는 사용자 작업을 건드리지 않아야 한다"
    )


def test_clean_rebase_completes(diverged: Path) -> None:
    """서로 다른 파일을 고쳤으면 충돌 없이 끝난다.

    `diverged`의 topic은 main과 **같은 파일**을 고쳐 반드시 충돌하므로,
    여기서는 base에서 갈라진 새 브랜치로 겹치지 않는 변경을 만든다.
    """
    base = git("rev-parse", "topic~1", cwd=diverged).stdout.strip()
    git("checkout", "--quiet", "-b", "side", base, cwd=diverged)
    write(diverged / "other.txt", "겹치지 않는 파일\n")
    commit_all(diverged, "side-commit")

    engine = LocalGitEngine.open(diverged)
    outcome = engine.rebase("main")

    assert outcome.kind is HistoryOutcomeKind.COMPLETED
    assert outcome.conflicts == ()
    assert engine.current_operation() is RepoOperation.NONE
    assert subjects(diverged) == ["side-commit", "main-commit", "base"]


def test_revert_creates_a_new_commit(diverged: Path) -> None:
    """revert는 히스토리를 고치지 않고 앞에 덧붙인다."""
    engine = LocalGitEngine.open(diverged)
    head = git("rev-parse", "HEAD", cwd=diverged).stdout.strip()

    outcome = engine.revert(head)

    assert outcome.kind is HistoryOutcomeKind.COMPLETED
    assert engine.current_operation() is RepoOperation.NONE
    assert len(subjects(diverged)) == 3, "커밋이 사라지지 않고 하나 늘어야 한다"


def test_operation_state_is_empty_when_quiet(diverged: Path) -> None:
    engine = LocalGitEngine.open(diverged)
    state = engine.operation_state()
    assert not state.is_active
    assert state.summary() == ""


# ----------------------------------------------------------------------
# 5. reset
# ----------------------------------------------------------------------


def test_hard_reset_discards_working_tree(diverged: Path) -> None:
    engine = LocalGitEngine.open(diverged)
    (diverged / "f.txt").write_text("버려질 작업\n", encoding="utf-8")
    base = git("rev-parse", "HEAD~1", cwd=diverged).stdout.strip()

    engine.reset_to(base, ResetKind.HARD)

    assert (diverged / "f.txt").read_text(encoding="utf-8") == "base\n"
    assert subjects(diverged) == ["base"]


def test_mixed_reset_keeps_working_tree(diverged: Path) -> None:
    """MIXED는 커밋만 되돌리고 파일 내용은 남긴다 — 미스테이징 상태로."""
    engine = LocalGitEngine.open(diverged)
    base = git("rev-parse", "HEAD~1", cwd=diverged).stdout.strip()

    engine.reset_to(base, ResetKind.MIXED)

    assert (diverged / "f.txt").read_text(encoding="utf-8") == MINE
    assert subjects(diverged) == ["base"]
    assert not engine.working_tree_status().is_clean


def test_reset_refuses_during_an_operation(diverged: Path) -> None:
    """rebase 도중 reset은 시퀀서가 기대하는 HEAD를 어긋낸다.

    허용하면 `--continue`도 `--abort`도 못 하는 상태에 사용자를 가둔다.
    """
    engine = LocalGitEngine.open(diverged)
    engine.rebase("main")
    base = git("rev-parse", "HEAD", cwd=diverged).stdout.strip()

    with pytest.raises(EngineError):
        engine.reset_to(base, ResetKind.HARD)

    assert engine.current_operation() is RepoOperation.REBASE


# ----------------------------------------------------------------------
# 6. 우리가 다루지 않는 상태 (bisect 등)
# ----------------------------------------------------------------------


@pytest.fixture
def bisecting(diverged: Path) -> Path:
    """bisect 진행 중인 저장소 — 우리가 시작하지도, 끝내지도 않는 상태."""
    git("bisect", "start", cwd=diverged)
    git("bisect", "bad", cwd=diverged)
    git("bisect", "good", "topic~1", cwd=diverged)
    return diverged


def test_unknown_states_map_to_other(bisecting: Path) -> None:
    """모르는 상태를 NONE으로 뭉개면 그 위에 rebase를 얹게 된다."""
    assert LocalGitEngine.open(bisecting).current_operation() is RepoOperation.OTHER


def test_unknown_states_never_leak_a_raw_exception(bisecting: Path) -> None:
    """세 경로 모두 도메인 오류로 거부해야 한다.

    raw 예외가 이 층 밖으로 새면 UI의 `except GitClientError`가 잡지 못해
    Qt 이벤트 루프까지 올라간다 (design.md §7).
    """
    engine = LocalGitEngine.open(bisecting)
    for call in (
        engine.abort_operation,
        engine.continue_operation,
        engine.skip_operation,
    ):
        with pytest.raises(EngineError):
            call()


def test_unknown_states_are_not_offered_an_abort(bisecting: Path) -> None:
    """되돌릴 수 없는 것에 '중단'을 내주면 오류만 돌아오는 버튼이 된다."""
    state = LocalGitEngine.open(bisecting).operation_state()

    assert state.is_active, "진행 중이라는 사실 자체는 보여야 한다"
    assert not state.operation.can_abort
    assert not state.operation.can_continue
    assert "git CLI" in state.summary(), "어디로 가야 하는지 말해야 한다"


def test_starting_an_operation_is_refused_during_bisect(bisecting: Path) -> None:
    """모르는 상태 위에 새 연산을 얹지 않는다."""
    engine = LocalGitEngine.open(bisecting)
    with pytest.raises(EngineError):
        engine.rebase("main")


# ----------------------------------------------------------------------
# 7. 큐가 지연시키는 동안 브랜치가 바뀌면 (리뷰에서 확정된 결함)
# ----------------------------------------------------------------------


def test_reset_refuses_when_the_branch_moved_under_it(diverged: Path) -> None:
    """**가장 위험한 경합이다.**

    브랜치 전환과 reset은 같은 큐를 쓰고, 화면의 브랜치 이름은 큐가 빌 때까지
    갱신되지 않는다. 전환이 먼저 실행되면 확인창이 말한 브랜치가 아니라 다른
    브랜치가 옮겨진다 — 그리고 `HARD`가 지운 커밋 안 된 작업은 reflog에도
    남지 않는다.
    """
    engine = LocalGitEngine.open(diverged)
    base = git("rev-parse", "HEAD~1", cwd=diverged).stdout.strip()
    git("checkout", "--quiet", "main", cwd=diverged)  # 큐가 도는 사이의 전환

    with pytest.raises(EngineError) as caught:
        engine.reset_to(base, ResetKind.HARD, expected_branch="topic")

    assert "topic" in str(caught.value)
    assert "합치" not in str(caught.value), "reset이 병합 문구를 쓰면 안 된다"
    assert git("rev-parse", "main", cwd=diverged).stdout.strip() != base


def test_cherry_pick_and_revert_refuse_a_moved_branch(diverged: Path) -> None:
    """가드는 reset 전용이 아니다 — 커밋을 만드는 경로 전부에 필요하다."""
    engine = LocalGitEngine.open(diverged)
    head = git("rev-parse", "HEAD", cwd=diverged).stdout.strip()
    git("checkout", "--quiet", "main", cwd=diverged)

    for call in (
        lambda: engine.cherry_pick(head, expected_branch="topic"),
        lambda: engine.revert(head, expected_branch="topic"),
    ):
        with pytest.raises(EngineError):
            call()
    assert engine.current_operation() is RepoOperation.NONE


def test_detached_head_gets_its_own_message(diverged: Path) -> None:
    """분리된 HEAD에 "브랜치가 바뀌었습니다"는 답이 아니다."""
    engine = LocalGitEngine.open(diverged)
    head = git("rev-parse", "HEAD", cwd=diverged).stdout.strip()
    git("checkout", "--quiet", "--detach", cwd=diverged)

    with pytest.raises(EngineError) as caught:
        engine.cherry_pick(head, expected_branch="topic")

    assert "분리된 HEAD" in str(caught.value)


# ----------------------------------------------------------------------
# 8. 환경 격리 — 앱을 어디서 띄웠느냐로 결과가 바뀌지 않는다
# ----------------------------------------------------------------------


def test_inherited_identity_env_does_not_reach_the_commit(
    diverged: Path, monkeypatch
) -> None:
    """상속된 `GIT_COMMITTER_*`가 재생된 커밋의 커미터를 바꾸면 안 된다.

    실측: git CLI는 이 변수를 따르고 pygit2의 `default_signature`는 무시한다.
    걷어내지 않으면 **같은 앱이 병합 커밋과 리베이스 커밋에 다른 사람 이름을
    적는다** — 그것도 앱을 어느 셸에서 띄웠느냐에 따라.
    """
    monkeypatch.setenv("GIT_COMMITTER_NAME", "탈취된이름")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "hijack@example.com")
    engine = LocalGitEngine.open(diverged)

    engine.rebase("main")
    engine.resolve_conflict("f.txt", ConflictChoice.THEIRS)
    engine.continue_operation()

    committer = git("log", "-1", "--format=%cn <%ce>", cwd=diverged).stdout.strip()
    assert "탈취된이름" not in committer, committer


def test_inherited_config_env_cannot_inject_settings(
    diverged: Path, monkeypatch
) -> None:
    """`GIT_CONFIG_*`로 주입된 설정은 사용자 설정이 아니다.

    실측에서 `rebase.backend=apply`가 조용히 켜졌다. 사용자 설정을 존중하는
    것과 환경변수로 주입된 설정을 존중하는 것은 다르다 — 후자는 앱을 어디서
    띄웠느냐로 동작이 바뀐다는 뜻이다.
    """
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'rebase.backend=apply'")
    engine = LocalGitEngine.open(diverged)

    engine.rebase("main")

    assert (Path(engine._repo.path) / "rebase-merge").is_dir(), (
        "주입된 설정이 백엔드를 바꿨다"
    )


def test_progress_is_read_for_both_rebase_backends(diverged: Path) -> None:
    """apply 백엔드는 next/last를, merge 백엔드는 msgnum/end를 쓴다.

    한쪽 이름만 알면 다른 백엔드에서 진행 표시가 통째로 사라진다.
    """
    git("config", "rebase.backend", "apply", cwd=diverged)
    engine = LocalGitEngine.open(diverged)
    engine.rebase("main")

    state = engine.operation_state()
    assert state.operation is RepoOperation.REBASE
    assert state.step == 1 and state.total == 1, (
        f"apply 백엔드의 진행을 못 읽었다: {state.step}/{state.total}"
    )
    assert state.head_branch == "topic"


def test_branch_names_with_slashes_survive(tmp_path: Path) -> None:
    """`feat/x`를 `x`로 잘라 보여주면 배너가 없는 브랜치를 가리킨다.

    슬래시 든 브랜치명은 예외가 아니라 기본에 가깝다.
    """
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    write(root / "f.txt", "base\n")
    commit_all(root, "base")
    git("checkout", "--quiet", "-b", "feat/deep/topic", cwd=root)
    write(root / "f.txt", MINE)
    commit_all(root, "topic-commit")
    git("checkout", "--quiet", "main", cwd=root)
    write(root / "f.txt", THEIRS)
    commit_all(root, "main-commit")
    git("checkout", "--quiet", "feat/deep/topic", cwd=root)

    engine = LocalGitEngine.open(root)
    engine.rebase("main")

    assert engine.operation_state().head_branch == "feat/deep/topic"


def test_git_messages_carry_no_ansi_escapes(diverged: Path) -> None:
    """git이 진행률을 지우려고 섞는 제어 문자가 사용자에게 보이면 안 된다.

    **완료 시점을 본다.** 실측에서 제어 문자가 붙는 곳은 거기다
    (완료 stderr = ESC + "[KSuccessfully rebased..."). 충돌 시점만 확인하면
    통과하지만 아무것도 지키지 못한다 — 변이 검증이 그것을 잡았다.
    """
    engine = LocalGitEngine.open(diverged)
    engine.rebase("main")
    engine.resolve_conflict("f.txt", ConflictChoice.THEIRS)

    outcome = engine.continue_operation()

    assert ESC not in outcome.message, repr(outcome.message)
    assert "Successfully" in outcome.message, "메시지 자체는 남아야 한다"
