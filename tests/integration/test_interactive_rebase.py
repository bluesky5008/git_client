"""인터랙티브 리베이스 — 계획을 짜서 옮겨 심는다 (FR-16, ADR-86).

시각화 이득이 가장 큰 작업이라 §1.3의 숙련자가 GUI를 쓰는 이유로 꼽힌다.
여기서 지키는 것: 계획이 **그대로** 실행되는가, 실행할 수 없는 계획을
git보다 먼저 알아보는가, 그리고 기존 리베이스 안전장치(충돌 정지,
빈 커밋 판정)가 계획 경로에서도 살아 있는가.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.errors import EngineError
from gitclient.domain.models import (
    HistoryOutcomeKind,
    RebaseAction,
    RebaseStep,
    RepoOperation,
)
from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.integration.remote_harness import AUTHOR_ENV, git

TIMEOUT = 15_000


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


def subjects(repo: Path) -> list[str]:
    return git("log", "--format=%s", cwd=repo).stdout.splitlines()


@pytest.fixture
def topic(tmp_path: Path) -> Path:
    """main 위에 세 커밋이 얹힌 topic 브랜치. 파일이 서로 달라 충돌하지 않는다."""
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    commit_all(root, "base")

    git("checkout", "--quiet", "-b", "topic", cwd=root)
    for name in ("a", "b", "c"):
        (root / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
        commit_all(root, f"커밋 {name}")
    return root


def plan(steps, **actions) -> list[RebaseStep]:
    """요약으로 동작을 지정한 계획을 만든다 (순서는 인자 순)."""
    by_summary = {step.summary: step for step in steps}
    return [
        RebaseStep(sha=by_summary[summary].sha, action=action, summary=summary)
        for summary, action in actions.items()
    ]


class TestTodoListing:
    def test_oldest_first_like_git(self, topic: Path) -> None:
        """git todo와 같은 순서여야 화면의 계획과 실행이 일치한다."""
        steps = LocalGitEngine.open(str(topic)).rebase_todo("refs/heads/main")

        assert [s.summary for s in steps] == ["커밋 a", "커밋 b", "커밋 c"]
        assert all(s.action is RebaseAction.PICK for s in steps)

    def test_nothing_to_move_is_empty(self, topic: Path) -> None:
        git("checkout", "--quiet", "main", cwd=topic)
        assert LocalGitEngine.open(str(topic)).rebase_todo("refs/heads/topic") == ()


class TestPlanIsExecutedAsWritten:
    def test_reorder(self, topic: Path) -> None:
        engine = LocalGitEngine.open(str(topic))
        steps = engine.rebase_todo("refs/heads/main")
        reordered = plan(
            steps,
            **{"커밋 c": RebaseAction.PICK, "커밋 a": RebaseAction.PICK,
               "커밋 b": RebaseAction.PICK},
        )

        outcome = engine.rebase_interactive("refs/heads/main", reordered)

        assert outcome.kind is HistoryOutcomeKind.COMPLETED
        # log는 최신이 먼저 — 계획의 역순이다.
        assert subjects(topic) == ["커밋 b", "커밋 a", "커밋 c", "base"]

    def test_drop_removes_the_commit(self, topic: Path) -> None:
        engine = LocalGitEngine.open(str(topic))
        steps = engine.rebase_todo("refs/heads/main")

        engine.rebase_interactive(
            "refs/heads/main",
            plan(steps, **{"커밋 a": RebaseAction.PICK,
                           "커밋 b": RebaseAction.DROP,
                           "커밋 c": RebaseAction.PICK}),
        )

        assert subjects(topic) == ["커밋 c", "커밋 a", "base"]
        assert not (topic / "b.txt").exists(), "버린 커밋의 변경이 남았다"

    def test_squash_joins_messages(self, topic: Path) -> None:
        engine = LocalGitEngine.open(str(topic))
        steps = engine.rebase_todo("refs/heads/main")

        engine.rebase_interactive(
            "refs/heads/main",
            plan(steps, **{"커밋 a": RebaseAction.PICK,
                           "커밋 b": RebaseAction.SQUASH,
                           "커밋 c": RebaseAction.PICK}),
        )

        assert len(subjects(topic)) == 3  # base + 합쳐진 것 + c
        merged = git(
            "log", "--format=%B", "-1", "HEAD~1", cwd=topic
        ).stdout
        assert "커밋 a" in merged and "커밋 b" in merged
        # 합쳤어도 변경은 둘 다 남는다.
        assert (topic / "a.txt").exists() and (topic / "b.txt").exists()

    def test_fixup_drops_the_message_but_keeps_changes(self, topic: Path) -> None:
        engine = LocalGitEngine.open(str(topic))
        steps = engine.rebase_todo("refs/heads/main")

        engine.rebase_interactive(
            "refs/heads/main",
            plan(steps, **{"커밋 a": RebaseAction.PICK,
                           "커밋 b": RebaseAction.FIXUP,
                           "커밋 c": RebaseAction.PICK}),
        )

        merged = git("log", "--format=%B", "-1", "HEAD~1", cwd=topic).stdout
        assert "커밋 a" in merged
        assert "커밋 b" not in merged, "fixup인데 메시지가 남았다"
        assert (topic / "b.txt").exists()


class TestImpossiblePlansAreRefusedEarly:
    """git보다 먼저 알아본다 — 사용자가 화면에서 이유를 안다."""

    def test_squash_as_first_step(self, topic: Path) -> None:
        engine = LocalGitEngine.open(str(topic))
        steps = engine.rebase_todo("refs/heads/main")

        with pytest.raises(EngineError) as caught:
            engine.rebase_interactive(
                "refs/heads/main",
                plan(steps, **{"커밋 a": RebaseAction.SQUASH,
                               "커밋 b": RebaseAction.PICK,
                               "커밋 c": RebaseAction.PICK}),
            )

        assert "첫 커밋" in str(caught.value)
        assert engine.current_operation() is RepoOperation.NONE

    def test_dropping_everything(self, topic: Path) -> None:
        engine = LocalGitEngine.open(str(topic))
        steps = engine.rebase_todo("refs/heads/main")

        with pytest.raises(EngineError) as caught:
            engine.rebase_interactive(
                "refs/heads/main",
                [RebaseStep(s.sha, RebaseAction.DROP, s.summary) for s in steps],
            )

        assert "reset" in (caught.value.action or "")
        assert subjects(topic)[0] == "커밋 c", "저장소가 손대지지 않아야 한다"


class TestSafetyNetsStillApply:
    def test_conflict_stops_the_sequencer(self, tmp_path: Path) -> None:
        """계획 경로에서도 충돌은 정상 결과다 (ADR-38) — 중단할 길이 있다."""
        root = tmp_path / "work"
        root.mkdir()
        git("init", "--quiet", "-b", "main", str(root))
        (root / "f.txt").write_text("base\n", encoding="utf-8")
        commit_all(root, "base")
        git("checkout", "--quiet", "-b", "topic", cwd=root)
        (root / "f.txt").write_text("topic\n", encoding="utf-8")
        commit_all(root, "topic 수정")
        git("checkout", "--quiet", "main", cwd=root)
        (root / "f.txt").write_text("main\n", encoding="utf-8")
        commit_all(root, "main 수정")
        git("checkout", "--quiet", "topic", cwd=root)

        engine = LocalGitEngine.open(str(root))
        steps = engine.rebase_todo("refs/heads/main")

        outcome = engine.rebase_interactive("refs/heads/main", list(steps))

        assert outcome.kind is HistoryOutcomeKind.CONFLICTED
        assert engine.current_operation() is RepoOperation.REBASE
        engine.abort_operation()
        assert subjects(root)[0] == "topic 수정"

    def test_dirty_worktree_is_refused(self, topic: Path) -> None:
        (topic / "base.txt").write_text("커밋 안 한 수정\n", encoding="utf-8")
        engine = LocalGitEngine.open(str(topic))
        steps = engine.rebase_todo("refs/heads/main")

        with pytest.raises(EngineError):
            engine.rebase_interactive("refs/heads/main", list(steps))


class TestDialog:
    def test_plan_reflects_moves_and_actions(self, qtbot, topic: Path) -> None:  # noqa: ANN001
        from gitclient.ui.rebase_todo_dialog import RebaseTodoDialog

        steps = LocalGitEngine.open(str(topic)).rebase_todo("refs/heads/main")
        dialog = RebaseTodoDialog("main", steps)
        qtbot.addWidget(dialog)

        # 두 번째 줄을 '버리기'로, 세 번째 줄을 맨 위로.
        dialog._tree.itemWidget(dialog._tree.topLevelItem(1), 0).setCurrentIndex(3)
        dialog._tree.setCurrentItem(dialog._tree.topLevelItem(2))
        dialog._move(-1)
        dialog._move(-1)

        result = dialog.plan()
        assert [s.summary for s in result] == ["커밋 c", "커밋 a", "커밋 b"]
        assert [s.action for s in result] == [
            RebaseAction.PICK, RebaseAction.PICK, RebaseAction.DROP
        ], "이동 후에도 각 줄의 동작이 따라가야 한다"

    def test_window_runs_the_plan(self, qtbot, topic: Path) -> None:  # noqa: ANN001
        from gitclient.ui.main_window import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        window._report = lambda _e: None
        window.open_repository(str(topic))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        steps = window._engine.rebase_todo("refs/heads/main")
        window._submit_write(
            "리베이스(계획): main",
            lambda engine: engine.rebase_interactive(
                "refs/heads/main",
                plan(steps, **{"커밋 a": RebaseAction.PICK,
                               "커밋 b": RebaseAction.DROP,
                               "커밋 c": RebaseAction.PICK}),
                expected_branch="topic",
            ),
        )
        qtbot.waitUntil(lambda: not window._write_queue.is_busy, timeout=TIMEOUT)

        assert subjects(topic) == ["커밋 c", "커밋 a", "base"]
