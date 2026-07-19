"""적대적 리뷰에서 확정된 병합 결함들의 회귀 테스트 (Phase 4 증분 1).

리뷰가 22건을 확정했고 그 중 둘이 사용자 데이터를 파괴했다.

1. `abort_merge`의 전면 `reset --hard`가 **병합과 무관한 파일의 커밋 안 된
   작업**까지 날렸다. `git merge --abort`는 그것을 보존한다(`reset --merge`
   의미론). 우리 확인 다이얼로그는 "충돌을 해결하던 내용"만 사라진다고
   말했는데, 실제 파괴 범위는 그보다 넓었다.

2. `abort_merge`가 rebase·cherry-pick 상태에서도 실행돼 `.git/rebase-merge`를
   지웠다. 그러면 `git rebase --abort`도 `--continue`도 불가능해지고 HEAD는
   브랜치 밖에 남는다 — reflog 말고는 복구 수단이 없다.

나머지는 "빠져나갈 길이 없는 상태"와 "안내한 대로 따라갔는데 막히는" 경로다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gitclient.domain.errors import EngineError, GitClientError
from gitclient.domain.models import ConflictSide, WorkAreaStatus
from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.integration.remote_harness import AUTHOR_ENV, git


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


def git_raw(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """실패를 허용하는 git 호출 (충돌로 rc=1이 나는 명령에 쓴다)."""
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """shared.txt에서 충돌하고 other.txt는 양쪽 모두 건드리지 않는 저장소."""
    root = tmp_path / "work"
    git("init", "--quiet", "-b", "main", str(root))
    (root / "shared.txt").write_text("base\n", encoding="utf-8")
    (root / "other.txt").write_text("other-base\n", encoding="utf-8")
    commit_all(root, "base")

    git("checkout", "--quiet", "-b", "feature", cwd=root)
    (root / "shared.txt").write_text("feature가 고침\n", encoding="utf-8")
    commit_all(root, "feature")

    git("checkout", "--quiet", "main", cwd=root)
    (root / "shared.txt").write_text("main이 고침\n", encoding="utf-8")
    commit_all(root, "main")
    return root


class TestAbortDoesNotExceedItsPromise:
    """중단은 "병합을 되돌린다"이지 "작업을 전부 날린다"가 아니다."""

    def test_unrelated_uncommitted_work_survives_abort(self, repo: Path) -> None:
        """가장 심각했던 결함 — 병합이 손댄 적 없는 파일의 작업이 사라졌다."""
        mine = "몇 시간 동안 저장 안 한 작업\n"
        (repo / "other.txt").write_text(mine, encoding="utf-8")
        # git은 무관한 파일이 더러워도 병합을 시작한다
        git_raw("merge", "feature", cwd=repo)
        assert (repo / ".git" / "MERGE_HEAD").exists(), "전제가 깨졌다"

        LocalGitEngine.open(str(repo)).abort_merge()

        assert (repo / "other.txt").read_text(encoding="utf-8") == mine, (
            "병합이 건드린 적 없는 파일의 작업을 파괴했다"
        )
        assert not (repo / ".git" / "MERGE_HEAD").exists()

    def test_abort_matches_git_merge_abort(self, repo: Path) -> None:
        """기준은 우리 판단이 아니라 git의 동작이다."""
        (repo / "other.txt").write_text("작업 중\n", encoding="utf-8")
        (repo / "scratch.txt").write_text("메모\n", encoding="utf-8")
        git_raw("merge", "feature", cwd=repo)

        LocalGitEngine.open(str(repo)).abort_merge()

        assert (repo / "other.txt").read_text(encoding="utf-8") == "작업 중\n"
        assert (repo / "scratch.txt").exists(), "추적하지 않는 파일을 지웠다"
        assert (repo / "shared.txt").read_text(encoding="utf-8") == "main이 고침\n"

    def test_merge_introduced_changes_are_still_reverted(self, repo: Path) -> None:
        """무관한 파일을 지키느라 병합 자체를 안 되돌리면 안 된다."""
        git("checkout", "--quiet", "feature", cwd=repo)
        (repo / "from_feature.txt").write_text("상대가 추가\n", encoding="utf-8")
        commit_all(repo, "feature가 파일 추가")
        git("checkout", "--quiet", "main", cwd=repo)
        git_raw("merge", "feature", cwd=repo)
        assert (repo / "from_feature.txt").exists(), "전제가 깨졌다"

        LocalGitEngine.open(str(repo)).abort_merge()

        assert not (repo / "from_feature.txt").exists(), "병합이 가져온 것이 남았다"
        assert "<<<<<<<" not in (repo / "shared.txt").read_text(encoding="utf-8")
        assert git("status", "--porcelain", cwd=repo).stdout.strip() == ""


class TestAbortRefusesOtherOperations:
    """rebase는 우리가 시작한 병합이 아니다 — 건드리면 복구 불가가 된다."""

    def test_rebase_in_progress_is_refused(self, repo: Path) -> None:
        git("checkout", "--quiet", "feature", cwd=repo)
        result = git_raw("rebase", "main", cwd=repo)
        assert result.returncode != 0, "rebase가 충돌로 멈춰야 한다"
        assert (repo / ".git" / "rebase-merge").exists()

        with pytest.raises(EngineError) as excinfo:
            LocalGitEngine.open(str(repo)).abort_merge()

        assert excinfo.value.action is not None
        assert (repo / ".git" / "rebase-merge").exists(), (
            "rebase 상태를 지워 --abort도 --continue도 못 하게 만들었다"
        )
        # git이 여전히 스스로 되돌릴 수 있어야 한다
        assert git_raw("rebase", "--abort", cwd=repo).returncode == 0

    def test_rebase_conflicts_are_not_reported_as_merge_conflicts(
        self, repo: Path
    ) -> None:
        """rebase 충돌도 index.conflicts를 채운다 — 그것으로 판단하면 안 된다."""
        git("checkout", "--quiet", "feature", cwd=repo)
        git_raw("rebase", "main", cwd=repo)

        engine = LocalGitEngine.open(str(repo))

        assert engine.merge_conflicts() == ()
        assert engine.is_merging() is False


class TestUserIsNeverStuck:
    def test_abort_stays_available_after_resolving_every_conflict(
        self, repo: Path
    ) -> None:
        """해결본을 스테이징하면 충돌 목록은 비지만 병합은 아직 안 끝났다.

        충돌 개수로 중단 가능 여부를 판단하면, 마지막 파일을 스테이징하는
        순간 빠져나갈 길이 사라진다.
        """
        engine = LocalGitEngine.open(str(repo))
        engine.merge("refs/heads/feature")
        (repo / "shared.txt").write_text("직접 정리\n", encoding="utf-8")
        engine.stage_file("shared.txt")

        assert engine.merge_conflicts() == (), "전제가 깨졌다"
        assert engine.is_merging() is True, "병합이 끝나지 않았는데 아니라고 한다"

        engine.abort_merge()
        assert not (repo / ".git" / "MERGE_HEAD").exists()

    def test_clean_repository_is_not_merging(self, repo: Path) -> None:
        assert LocalGitEngine.open(str(repo)).is_merging() is False


class TestGuidanceLeadsSomewhere:
    """"해결하고 커밋하라"고 안내했으면 그 길이 실제로 통해야 한다."""

    def test_committing_with_unresolved_conflicts_is_actionable(
        self, repo: Path
    ) -> None:
        """스테이징을 빠뜨린 첫 실수에서 libgit2 원문이 튀어나오면 안 된다."""
        engine = LocalGitEngine.open(str(repo))
        engine.merge("refs/heads/feature")

        with pytest.raises(GitClientError) as excinfo:
            engine.create_commit("충돌 해결")

        assert excinfo.value.action is not None
        assert "shared.txt" in (excinfo.value.detail or "")
        assert "not fully merged" not in excinfo.value.message

    def test_resolving_then_committing_creates_a_merge_commit(
        self, repo: Path
    ) -> None:
        """안내한 경로 전체가 실제로 통하는지 끝까지 걸어본다."""
        engine = LocalGitEngine.open(str(repo))
        engine.merge("refs/heads/feature")
        (repo / "shared.txt").write_text("양쪽을 합침\n", encoding="utf-8")
        engine.stage_file("shared.txt")

        engine.create_commit("충돌 해결")

        parents = git("rev-list", "--parents", "-n", "1", "HEAD", cwd=repo).stdout
        assert len(parents.split()) == 3, "머지 커밋이 아니라 일반 커밋이 됐다"
        assert not (repo / ".git" / "MERGE_HEAD").exists()

    def test_discarding_a_conflicted_file_is_refused(self, repo: Path) -> None:
        """"버리기"는 충돌 파일에 성립하지 않는다 — 마커를 다시 쓸 뿐이다."""
        engine = LocalGitEngine.open(str(repo))
        engine.merge("refs/heads/feature")

        with pytest.raises(EngineError) as excinfo:
            engine.discard_file("shared.txt")

        assert excinfo.value.action is not None
        assert engine.merge_conflicts(), "충돌이 조용히 사라졌다"


class TestMergeStartGuard:
    def test_untracked_files_do_not_block_the_merge(self, repo: Path) -> None:
        """git도 막지 않는다. 막으면 메모 파일 하나로 pull이 통째로 멈춘다.

        게다가 우리가 안내하던 stash는 기본값에서 추적되지 않은 파일을
        담지 않아, 사용자가 그대로 따라도 상태가 바뀌지 않았다.
        """
        (repo / "메모.txt").write_text("나중에 볼 것\n", encoding="utf-8")

        outcome = LocalGitEngine.open(str(repo)).merge("refs/heads/feature")

        assert outcome.is_conflicted, "병합 자체는 진행됐어야 한다"
        assert (repo / "메모.txt").exists(), "추적하지 않는 파일을 건드렸다"

    def test_tracked_modifications_still_block(self, repo: Path) -> None:
        """추적 중인 변경은 계속 막는다 — 이쪽은 안내하는 조치가 실제로 통한다."""
        (repo / "other.txt").write_text("작업 중\n", encoding="utf-8")

        with pytest.raises(EngineError) as excinfo:
            LocalGitEngine.open(str(repo)).merge("refs/heads/feature")

        assert excinfo.value.action is not None
        assert not (repo / ".git" / "MERGE_HEAD").exists()

    def test_failed_merge_commit_does_not_leave_a_half_merge(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """커밋을 못 만들면 되돌린다.

        그러지 않으면 저장소는 병합 중인데 충돌은 0개라, 사용자에게는
        원인 모를 오류만 뜨고 중단할 대상도 보이지 않는다.
        """
        git("checkout", "--quiet", "-b", "clean-side", "HEAD~1", cwd=repo)
        (repo / "clean.txt").write_text("충돌 없음\n", encoding="utf-8")
        commit_all(repo, "충돌 없는 작업")
        git("checkout", "--quiet", "main", cwd=repo)
        engine = LocalGitEngine.open(str(repo))
        monkeypatch.setattr(
            LocalGitEngine, "create_commit",
            lambda *a, **k: (_ for _ in ()).throw(EngineError("서명이 없습니다.")),
        )

        with pytest.raises(GitClientError):
            engine.merge("refs/heads/clean-side")

        assert not (repo / ".git" / "MERGE_HEAD").exists(), (
            "병합 중인데 충돌은 0개인 상태로 남겼다 — 중단 메뉴도 안 켜진다"
        )
        assert git("status", "--porcelain", cwd=repo).stdout.strip() == ""


class TestConflictClassification:
    def test_rename_delete_leaves_no_content_on_either_side(
        self, tmp_path: Path
    ) -> None:
        """이름 변경 대 삭제는 원래 경로에 양쪽 모두 없는 항목을 남긴다."""
        root = tmp_path / "rn"
        git("init", "--quiet", "-b", "main", str(root))
        (root / "a.txt").write_text("내용\n", encoding="utf-8")
        commit_all(root, "base")
        git("checkout", "--quiet", "-b", "feature", cwd=root)
        git("rm", "--quiet", "a.txt", cwd=root)
        commit_all(root, "feature가 삭제")
        git("checkout", "--quiet", "main", cwd=root)
        git("mv", "a.txt", "b.txt", cwd=root)
        commit_all(root, "main이 이름 변경")

        outcome = LocalGitEngine.open(str(root)).merge("refs/heads/feature")

        by_path = {c.path: c for c in outcome.conflicts}
        assert "a.txt" in by_path
        gone = by_path["a.txt"]
        assert gone.side is ConflictSide.BOTH_DELETED
        assert gone.has_our_content is False
        assert gone.has_their_content is False

    def test_binary_conflict_has_no_markers(self, tmp_path: Path) -> None:
        """바이너리에는 마커가 안 들어간다.

        "마커를 정리하고 커밋하라"는 안내를 그대로 따르면 상대 변경이
        조용히 버려진 머지 커밋이 만들어진다 — 그래서 구분해야 한다.
        """
        root = tmp_path / "bin"
        git("init", "--quiet", "-b", "main", str(root))
        (root / "img.bin").write_bytes(bytes(range(256)) * 8)
        commit_all(root, "base")
        git("checkout", "--quiet", "-b", "feature", cwd=root)
        (root / "img.bin").write_bytes(bytes(range(255, -1, -1)) * 8)
        commit_all(root, "feature")
        git("checkout", "--quiet", "main", cwd=root)
        (root / "img.bin").write_bytes(bytes(range(128)) * 16)
        commit_all(root, "main")

        outcome = LocalGitEngine.open(str(root)).merge("refs/heads/feature")

        conflict = outcome.conflicts[0]
        assert conflict.side is ConflictSide.BOTH_MODIFIED
        assert conflict.has_markers is False
        assert b"<<<<<<<" not in (root / "img.bin").read_bytes()

    def test_text_conflict_has_markers(self, repo: Path) -> None:
        outcome = LocalGitEngine.open(str(repo)).merge("refs/heads/feature")

        assert outcome.conflicts[0].has_markers is True
