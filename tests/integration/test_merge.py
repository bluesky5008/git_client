"""병합 통합 테스트 (Phase 4 증분 1).

**워킹 트리를 파괴적으로 건드리는 첫 작업이다.** 그래서 "합쳐지는가"보다
"합쳐지지 않아야 할 때 멈추는가"와 "중단하면 원래대로 돌아오는가"를 먼저
고정한다 — 잘못되면 사용자 코드가 사라진다.

충돌은 **실패가 아니라 결과다.** git이 할 수 있는 만큼 합쳐두고 나머지를
사람에게 넘긴 상태이므로 예외가 아니라 값으로 돌려준다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.errors import EngineError
from gitclient.domain.models import ConflictSide, MergeKind
from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.integration.remote_harness import AUTHOR_ENV, git


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main과 feature가 갈라진 저장소."""
    root = tmp_path / "work"
    git("init", "--quiet", "-b", "main", str(root))
    (root / "shared.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (root / "untouched.txt").write_text("그대로\n", encoding="utf-8")
    commit_all(root, "base")

    git("checkout", "--quiet", "-b", "feature", cwd=root)
    (root / "from_feature.txt").write_text("feature가 추가\n", encoding="utf-8")
    commit_all(root, "feature 작업")

    git("checkout", "--quiet", "main", cwd=root)
    (root / "from_main.txt").write_text("main이 추가\n", encoding="utf-8")
    commit_all(root, "main 작업")
    return root


def make_conflict(repo: Path) -> None:
    """양쪽이 같은 줄을 다르게 고쳐 충돌을 만든다."""
    git("checkout", "--quiet", "feature", cwd=repo)
    (repo / "shared.txt").write_text("line1\nFEATURE\nline3\n", encoding="utf-8")
    commit_all(repo, "feature가 수정")
    git("checkout", "--quiet", "main", cwd=repo)
    (repo / "shared.txt").write_text("line1\nMAIN\nline3\n", encoding="utf-8")
    commit_all(repo, "main이 수정")


class TestCleanMerge:
    def test_merge_creates_a_commit_with_two_parents(self, repo: Path) -> None:
        outcome = LocalGitEngine.open(str(repo)).merge("refs/heads/feature")

        assert not outcome.is_conflicted
        assert outcome.merged_sha
        parents = git("rev-list", "--parents", "-n", "1", "HEAD", cwd=repo).stdout
        assert len(parents.split()) == 3, "머지 커밋의 부모가 둘이어야 한다"

    def test_both_sides_are_present_afterwards(self, repo: Path) -> None:
        LocalGitEngine.open(str(repo)).merge("refs/heads/feature")

        assert (repo / "from_main.txt").exists()
        assert (repo / "from_feature.txt").exists()

    def test_working_tree_is_clean_after_merge(self, repo: Path) -> None:
        LocalGitEngine.open(str(repo)).merge("refs/heads/feature")

        assert git("status", "--porcelain", cwd=repo).stdout.strip() == ""

    def test_already_merged_is_up_to_date(self, repo: Path) -> None:
        engine = LocalGitEngine.open(str(repo))
        engine.merge("refs/heads/feature")

        outcome = engine.merge("refs/heads/feature")

        assert outcome.kind is MergeKind.UP_TO_DATE

    def test_fast_forward_is_used_when_possible(self, tmp_path: Path) -> None:
        """빨리 감을 수 있으면 머지 커밋을 만들지 않는다."""
        root = tmp_path / "ff"
        git("init", "--quiet", "-b", "main", str(root))
        (root / "a.txt").write_text("1\n", encoding="utf-8")
        commit_all(root, "base")
        git("checkout", "--quiet", "-b", "feature", cwd=root)
        (root / "a.txt").write_text("2\n", encoding="utf-8")
        commit_all(root, "앞서감")
        git("checkout", "--quiet", "main", cwd=root)

        outcome = LocalGitEngine.open(str(root)).merge("refs/heads/feature")

        assert outcome.kind is MergeKind.FAST_FORWARD
        parents = git("rev-list", "--parents", "-n", "1", "HEAD", cwd=root).stdout
        assert len(parents.split()) == 2, "빨리 감기인데 머지 커밋을 만들었다"


class TestConflictDetection:
    def test_conflict_is_a_result_not_an_exception(self, repo: Path) -> None:
        make_conflict(repo)

        outcome = LocalGitEngine.open(str(repo)).merge("refs/heads/feature")

        assert outcome.is_conflicted
        assert outcome.merged_sha is None, "충돌 상태에서 커밋을 만들면 안 된다"
        assert [c.path for c in outcome.conflicts] == ["shared.txt"]

    def test_conflict_side_is_classified(self, repo: Path) -> None:
        make_conflict(repo)

        outcome = LocalGitEngine.open(str(repo)).merge("refs/heads/feature")

        assert outcome.conflicts[0].side is ConflictSide.BOTH_MODIFIED

    def test_repository_stays_in_merge_state(self, repo: Path) -> None:
        """충돌은 중간 상태다 — 사용자가 이어서 해결할 수 있어야 한다."""
        make_conflict(repo)

        LocalGitEngine.open(str(repo)).merge("refs/heads/feature")

        assert (repo / ".git" / "MERGE_HEAD").exists()

    def test_conflict_markers_are_in_the_working_tree(self, repo: Path) -> None:
        make_conflict(repo)

        LocalGitEngine.open(str(repo)).merge("refs/heads/feature")

        content = (repo / "shared.txt").read_text(encoding="utf-8")
        assert "<<<<<<<" in content and ">>>>>>>" in content

    def test_delete_modify_conflict_is_distinguished(self, tmp_path: Path) -> None:
        """한쪽이 지운 충돌은 보여줄 '상대 내용'이 없다 — UI가 구분해야 한다."""
        root = tmp_path / "dm"
        git("init", "--quiet", "-b", "main", str(root))
        (root / "doomed.txt").write_text("원본\n", encoding="utf-8")
        commit_all(root, "base")
        git("checkout", "--quiet", "-b", "feature", cwd=root)
        (root / "doomed.txt").unlink()
        commit_all(root, "삭제")
        git("checkout", "--quiet", "main", cwd=root)
        (root / "doomed.txt").write_text("수정됨\n", encoding="utf-8")
        commit_all(root, "수정")

        outcome = LocalGitEngine.open(str(root)).merge("refs/heads/feature")

        conflict = outcome.conflicts[0]
        assert conflict.side is ConflictSide.DELETED_BY_THEM
        assert conflict.has_our_content is True
        assert conflict.has_their_content is False


class TestAbort:
    def test_abort_restores_the_working_tree(self, repo: Path) -> None:
        make_conflict(repo)
        before = (repo / "shared.txt").read_text(encoding="utf-8")
        head_before = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        engine = LocalGitEngine.open(str(repo))
        engine.merge("refs/heads/feature")

        engine.abort_merge()

        assert (repo / "shared.txt").read_text(encoding="utf-8") == before
        assert git("rev-parse", "HEAD", cwd=repo).stdout.strip() == head_before
        assert git("status", "--porcelain", cwd=repo).stdout.strip() == ""

    def test_abort_clears_the_merge_state(self, repo: Path) -> None:
        make_conflict(repo)
        engine = LocalGitEngine.open(str(repo))
        engine.merge("refs/heads/feature")

        engine.abort_merge()

        assert not (repo / ".git" / "MERGE_HEAD").exists()
        assert engine.merge_conflicts() == ()

    def test_abort_without_a_merge_is_harmless(self, repo: Path) -> None:
        head = git("rev-parse", "HEAD", cwd=repo).stdout.strip()

        LocalGitEngine.open(str(repo)).abort_merge()

        assert git("rev-parse", "HEAD", cwd=repo).stdout.strip() == head


class TestGuards:
    """합쳐지지 않아야 할 때 멈추는가 — 파괴적 작업이라 여기가 먼저다."""

    def test_dirty_working_tree_blocks_the_merge(self, repo: Path) -> None:
        make_conflict(repo)
        mine = "내가 작업하던 내용\n"
        (repo / "untouched.txt").write_text(mine, encoding="utf-8")
        head = git("rev-parse", "HEAD", cwd=repo).stdout.strip()

        with pytest.raises(EngineError) as excinfo:
            LocalGitEngine.open(str(repo)).merge("refs/heads/feature")

        assert excinfo.value.action is not None
        assert (repo / "untouched.txt").read_text(encoding="utf-8") == mine
        assert git("rev-parse", "HEAD", cwd=repo).stdout.strip() == head
        assert not (repo / ".git" / "MERGE_HEAD").exists(), "병합이 시작됐다"

    def test_merge_in_progress_blocks_another_merge(self, repo: Path) -> None:
        make_conflict(repo)
        engine = LocalGitEngine.open(str(repo))
        engine.merge("refs/heads/feature")

        with pytest.raises(EngineError) as excinfo:
            engine.merge("refs/heads/feature")

        assert excinfo.value.action is not None
        # 충돌 난 트리는 더럽기도 해서 더러운-트리 가드도 EngineError를 낸다.
        # 둘을 구분하지 않으면 상태 가드를 통째로 지워도 이 테스트는 통과한다.
        assert "저장소 상태:" in (excinfo.value.detail or ""), (
            "더러운 워킹 트리가 아니라 저장소 상태가 병합을 막아야 한다"
        )

    def test_branch_switch_under_it_is_refused(self, repo: Path) -> None:
        """큐에서 실행될 때 사용자가 브랜치를 바꿨을 수 있다."""
        engine = LocalGitEngine.open(str(repo))

        with pytest.raises(EngineError) as excinfo:
            engine.merge("refs/heads/feature", expected_branch="other")

        assert "other" in excinfo.value.message

    def test_unknown_source_is_actionable(self, repo: Path) -> None:
        with pytest.raises(EngineError) as excinfo:
            LocalGitEngine.open(str(repo)).merge("refs/heads/nonexistent")

        assert excinfo.value.action is not None
