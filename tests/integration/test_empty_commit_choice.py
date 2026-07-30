"""빈 커밋이 될 연산은 멈추고 사용자가 고른다 — DCR-001, ADR-76.

git 실측(2026-07-31, git 2.50.1)이 확인한 소실 경로 셋에 각각 대응한다:

  (a) 이미 upstream에 그대로 반영된 커밋 — git이 정당하게 생략하고
      경고를 남긴다. 손실이 아니므로 막지 않되 완료 보고에 싣는다.
  (b) 적용 시점에 비게 되는 커밋 — 기본값에서 조용히 버려진다.
      `--empty=stop`이 세운다.
  (c) 충돌을 upstream 쪽으로 해결해 비게 된 커밋 — **`--empty=stop`도
      못 막고** `--continue`가 조용히 버린다. rebase만 그렇다(cherry-pick은
      스스로 거부). `--continue` 직전의 트리 비교가 세운다.

여기서 가장 중요한 검증은 **평범한 리베이스가 멈추지 않는 것**이다 —
backlog §3.1이 사후 집계를 기각한 이유가 "평범한 리베이스마다 거짓
경고"였다. 트리 비교는 정의상 참인 판정이라 그 위험이 없어야 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.models import (
    ConflictChoice,
    HistoryOutcomeKind,
    RepoOperation,
)
from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.integration.remote_harness import AUTHOR_ENV, git

MINE = "내가-쓴-줄\n"
THEIRS = "상대가-쓴-줄\n"


def write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


def subjects(repo: Path) -> list[str]:
    return git("log", "--format=%s", cwd=repo).stdout.splitlines()


@pytest.fixture
def diverged(tmp_path: Path) -> Path:
    """main과 topic이 같은 줄을 다르게 고친 저장소. HEAD는 topic."""
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


@pytest.fixture
def subset(tmp_path: Path) -> Path:
    """topic의 유일한 커밋이 main 커밋의 부분집합인 저장소. HEAD는 topic.

    patch-id가 달라 clean cherry-pick 생략에는 걸리지 않고, 적용하면
    충돌 없이 비게 된다 — `--empty=stop`만이 세울 수 있는 경우다.
    """
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    commit_all(root, "base")

    git("checkout", "--quiet", "-b", "topic", cwd=root)
    write(root / "f.txt", "new1\n")
    commit_all(root, "subset-commit")

    git("checkout", "--quiet", "main", cwd=root)
    write(root / "f.txt", "new1\n")
    write(root / "extra.txt", "extra\n")
    commit_all(root, "superset-commit")

    git("checkout", "--quiet", "topic", cwd=root)
    return root


# ----------------------------------------------------------------------
# (c) 충돌 해결로 비게 된 커밋 — continue 직전 판정
# ----------------------------------------------------------------------


def test_continue_stops_before_git_would_drop_the_commit(diverged: Path) -> None:
    """upstream 쪽으로 해결하고 '계속'하면 멈춘다 — 커밋은 아직 있다.

    이 판정이 없으면 `git rebase --continue`는 rc=0 "Successfully rebased"로
    커밋을 조용히 버린다 (실측 — DCR-001 실험 1·5).
    """
    engine = LocalGitEngine.open(diverged)
    outcome = engine.rebase("refs/heads/main")
    assert outcome.kind is HistoryOutcomeKind.CONFLICTED

    engine.resolve_conflict("f.txt", ConflictChoice.OURS)  # rebase의 ours = upstream
    stopped = engine.continue_operation()

    assert stopped.kind is HistoryOutcomeKind.WOULD_BE_EMPTY
    assert stopped.operation is RepoOperation.REBASE
    # 멈췄을 뿐 아무것도 잃지 않았다 — 연산은 진행 중으로 남는다.
    assert engine.current_operation() is RepoOperation.REBASE
    # 어느 커밋이 비게 되는지 말할 수 있다.
    assert "topic-commit" in stopped.message


def test_skip_after_the_stop_drops_exactly_that_commit(diverged: Path) -> None:
    engine = LocalGitEngine.open(diverged)
    engine.rebase("refs/heads/main")
    engine.resolve_conflict("f.txt", ConflictChoice.OURS)
    engine.continue_operation()

    done = engine.skip_operation()

    assert done.kind is HistoryOutcomeKind.COMPLETED
    assert engine.current_operation() is RepoOperation.NONE
    assert subjects(diverged) == ["main-commit", "base"]


def test_keep_empty_preserves_the_commit_message(diverged: Path) -> None:
    """'빈 커밋으로 남기기'는 메시지를 유지한 변경 없는 커밋을 만든다."""
    engine = LocalGitEngine.open(diverged)
    engine.rebase("refs/heads/main")
    engine.resolve_conflict("f.txt", ConflictChoice.OURS)
    engine.continue_operation()

    done = engine.keep_empty_operation()

    assert done.kind is HistoryOutcomeKind.COMPLETED
    assert engine.current_operation() is RepoOperation.NONE
    assert subjects(diverged) == ["topic-commit", "main-commit", "base"]
    # 정말 빈 커밋인가 — 트리가 부모와 같아야 한다.
    trees = git("log", "--format=%T", "-2", cwd=diverged).stdout.splitlines()
    assert trees[0] == trees[1]


def test_keep_empty_refuses_when_there_are_staged_changes(diverged: Path) -> None:
    """남길 변경이 있으면 이 함수의 일이 아니다 — '계속'으로 보낸다.

    여기서 커밋을 만들면 같은 변경이 두 커밋으로 갈라진다.
    """
    from gitclient.domain.errors import EngineError

    engine = LocalGitEngine.open(diverged)
    engine.rebase("refs/heads/main")
    engine.resolve_conflict("f.txt", ConflictChoice.THEIRS)  # 내 커밋 유지 — 비지 않는다

    with pytest.raises(EngineError) as caught:
        engine.keep_empty_operation()
    assert "계속" in (caught.value.action or "")


def test_cherry_pick_gets_the_same_stop(diverged: Path) -> None:
    """같은 판정이 cherry-pick에도 선다 — 연산마다 다른 규칙은 오도다."""
    git("checkout", "--quiet", "main", cwd=diverged)
    engine = LocalGitEngine.open(diverged)
    topic = git("rev-parse", "topic", cwd=diverged).stdout.strip()
    engine.cherry_pick(topic)

    engine.resolve_conflict("f.txt", ConflictChoice.OURS)
    stopped = engine.continue_operation()
    assert stopped.kind is HistoryOutcomeKind.WOULD_BE_EMPTY

    done = engine.keep_empty_operation()
    assert done.kind is HistoryOutcomeKind.COMPLETED
    assert subjects(diverged)[0] == "topic-commit"


# ----------------------------------------------------------------------
# (b) 적용 시점에 비게 되는 커밋 — --empty=stop
# ----------------------------------------------------------------------


def test_rebase_stops_on_a_commit_that_becomes_empty(subset: Path) -> None:
    """부분집합 커밋은 조용히 사라지는 대신 멈춘다 (실험 2·3)."""
    engine = LocalGitEngine.open(subset)
    outcome = engine.rebase("refs/heads/main")

    assert outcome.kind is HistoryOutcomeKind.WOULD_BE_EMPTY
    assert engine.current_operation() is RepoOperation.REBASE
    assert "subset-commit" in outcome.message

    done = engine.keep_empty_operation()
    assert done.kind is HistoryOutcomeKind.COMPLETED
    assert subjects(subset) == ["subset-commit", "superset-commit", "base"]


# ----------------------------------------------------------------------
# (a) 정당한 생략 — 경고를 완료 보고에 싣는다
# ----------------------------------------------------------------------


def test_already_applied_commits_are_reported_not_hidden(tmp_path: Path) -> None:
    """clean cherry-pick 생략은 막지 않는다 — 다만 보고한다.

    이 생략을 `WOULD_BE_EMPTY`로 세우면 backlog §3.1이 우려한 "평범한
    리베이스마다 거짓 경고"가 된다. 세우지 않고, 조용히 넘기지도 않는다.
    """
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    commit_all(root, "base")

    git("checkout", "--quiet", "-b", "topic", cwd=root)
    write(root / "f.txt", "same\n")
    commit_all(root, "identical-change")
    picked = git("rev-parse", "HEAD", cwd=root).stdout.strip()

    # upstream에 **같은 패치가 독립적으로** 들어간 상황 — 메시지가 달라야
    # 한다. cherry-pick으로 만들면 같은 초 안에서 sha까지 동일해져
    # rebase가 "up to date"로 끝나 검증하려는 경로를 지나가지 않는다 (실측).
    git("checkout", "--quiet", "main", cwd=root)
    write(root / "f.txt", "same\n")
    commit_all(root, "same-change-landed-upstream")

    git("checkout", "--quiet", "topic", cwd=root)
    write(root / "g.txt", "more\n")
    commit_all(root, "second")

    outcome = LocalGitEngine.open(root).rebase("refs/heads/main")

    assert outcome.kind is HistoryOutcomeKind.COMPLETED
    assert len(outcome.skipped_already_applied) == 1
    assert picked.startswith(outcome.skipped_already_applied[0][:7])
    assert subjects(root) == ["second", "same-change-landed-upstream", "base"]


def test_an_ordinary_rebase_does_not_stop(tmp_path: Path) -> None:
    """겹치는 것 없는 평범한 리베이스는 한 번에 끝난다 — 거짓 경고 금지.

    이것이 이 설계의 존재 조건이다 (DCR-001 검증 방법).
    """
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    commit_all(root, "base")

    git("checkout", "--quiet", "-b", "topic", cwd=root)
    write(root / "topic.txt", "topic\n")
    commit_all(root, "topic-commit")

    git("checkout", "--quiet", "main", cwd=root)
    write(root / "main.txt", "main\n")
    commit_all(root, "main-commit")

    git("checkout", "--quiet", "topic", cwd=root)
    outcome = LocalGitEngine.open(root).rebase("refs/heads/main")

    assert outcome.kind is HistoryOutcomeKind.COMPLETED
    assert outcome.skipped_already_applied == ()
    assert subjects(root) == ["topic-commit", "main-commit", "base"]
