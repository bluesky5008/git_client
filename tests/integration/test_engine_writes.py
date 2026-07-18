"""LocalGitEngine 쓰기 연산 통합 테스트 (Phase 2).

각 연산 후 실제 저장소 상태(git 오브젝트/인덱스/워킹트리)를 단언한다.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest

from gitclient.domain.errors import EngineError
from gitclient.domain.models import WorkAreaStatus
from gitclient.infrastructure.local_engine import LocalGitEngine

SIGNATURE = pygit2.Signature("테스터", "tester@example.com", 1700000000, 540)


@pytest.fixture
def repo(tmp_path: Path) -> pygit2.Repository:
    """커밋 1개(a.txt, b.txt)를 가진 저장소. user.name/email 설정 포함."""
    r = pygit2.init_repository(str(tmp_path / "w"), initial_head="main")
    r.config["user.name"] = "테스터"
    r.config["user.email"] = "tester@example.com"

    wd = Path(r.workdir)
    (wd / "a.txt").write_text("one\n", encoding="utf-8")
    (wd / "b.txt").write_text("bee\n", encoding="utf-8")
    r.index.add_all()
    r.index.write()
    r.create_commit("HEAD", SIGNATURE, SIGNATURE, "init", r.index.write_tree(), [])
    return r


@pytest.fixture
def engine(repo: pygit2.Repository) -> LocalGitEngine:
    return LocalGitEngine.open(repo.workdir)


def workdir(repo: pygit2.Repository) -> Path:
    return Path(repo.workdir)


class TestStatus:
    def test_clean_tree(self, engine: LocalGitEngine) -> None:
        status = engine.working_tree_status()
        assert status.is_clean

    def test_modified_is_unstaged(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        status = engine.working_tree_status()
        assert [(c.path, c.status) for c in status.unstaged] == [
            ("a.txt", WorkAreaStatus.MODIFIED)
        ]
        assert status.staged == ()

    def test_untracked_is_new(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "new.txt").write_text("x\n", encoding="utf-8")
        status = engine.working_tree_status()
        assert [(c.path, c.status) for c in status.unstaged] == [
            ("new.txt", WorkAreaStatus.NEW)
        ]

    def test_deleted_file(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "b.txt").unlink()
        status = engine.working_tree_status()
        assert [(c.path, c.status) for c in status.unstaged] == [
            ("b.txt", WorkAreaStatus.DELETED)
        ]

    def test_partially_staged_file_appears_in_both(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "a.txt").write_text("staged\n", encoding="utf-8")
        engine.stage_file("a.txt")
        (workdir(repo) / "a.txt").write_text("staged\nand more\n", encoding="utf-8")

        status = engine.working_tree_status()
        assert [c.path for c in status.staged] == ["a.txt"]
        assert [c.path for c in status.unstaged] == ["a.txt"]


class TestStageUnstage:
    def test_stage_modified(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "a.txt").write_text("changed\n", encoding="utf-8")
        engine.stage_file("a.txt")
        status = engine.working_tree_status()
        assert [(c.path, c.status) for c in status.staged] == [
            ("a.txt", WorkAreaStatus.MODIFIED)
        ]
        assert status.unstaged == ()

    def test_stage_deletion(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "b.txt").unlink()
        engine.stage_file("b.txt")
        status = engine.working_tree_status()
        assert [(c.path, c.status) for c in status.staged] == [
            ("b.txt", WorkAreaStatus.DELETED)
        ]

    def test_unstage_restores_index_to_head(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "a.txt").write_text("changed\n", encoding="utf-8")
        engine.stage_file("a.txt")
        engine.unstage_file("a.txt")

        status = engine.working_tree_status()
        assert status.staged == ()
        assert [c.path for c in status.unstaged] == ["a.txt"]

    def test_unstage_new_file_removes_from_index(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "new.txt").write_text("x\n", encoding="utf-8")
        engine.stage_file("new.txt")
        engine.unstage_file("new.txt")

        status = engine.working_tree_status()
        assert status.staged == ()
        assert [(c.path, c.status) for c in status.unstaged] == [
            ("new.txt", WorkAreaStatus.NEW)
        ]


class TestDiscard:
    def test_discard_restores_head_content(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "a.txt").write_text("ruined\n", encoding="utf-8")
        engine.discard_file("a.txt")
        assert (workdir(repo) / "a.txt").read_text(encoding="utf-8") == "one\n"

    def test_discard_untracked_deletes_it(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        target = workdir(repo) / "junk.txt"
        target.write_text("x\n", encoding="utf-8")
        engine.discard_file("junk.txt")
        assert not target.exists()

    def test_discard_preserves_staged_content(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """리뷰 확정 결함의 회귀 테스트: 버리기의 복원 기준은 HEAD가 아니라 인덱스다.

        스테이징(v1) 후 다시 수정(v2)한 파일에서 미스테이징 변경만 버리면
        워킹트리는 v1이어야 한다. 초기 구현은 HEAD(v0)로 되돌려 스테이징된
        v1까지 파괴했다.
        """
        target = workdir(repo) / "a.txt"
        target.write_text("staged v1\n", encoding="utf-8")
        engine.stage_file("a.txt")
        target.write_text("unstaged v2\n", encoding="utf-8")

        engine.discard_file("a.txt")

        assert target.read_text(encoding="utf-8") == "staged v1\n"
        status = engine.working_tree_status()
        assert [c.path for c in status.staged] == ["a.txt"]  # 스테이징은 생존
        assert status.unstaged == ()

    def test_discard_staged_new_file_on_unborn_head(self, tmp_path: Path) -> None:
        """리뷰 확정 결함의 회귀 테스트: unborn HEAD + INDEX_NEW|WT_MODIFIED.

        초기 구현은 checkout_head를 호출해 HEAD가 없는 저장소에서 죽었다.
        """
        r = pygit2.init_repository(str(tmp_path / "unborn-d"), initial_head="main")
        target = Path(r.workdir) / "f.txt"
        target.write_text("staged\n", encoding="utf-8")
        engine = LocalGitEngine.open(r.workdir)
        engine.stage_file("f.txt")
        target.write_text("modified after stage\n", encoding="utf-8")

        engine.discard_file("f.txt")

        assert target.read_text(encoding="utf-8") == "staged\n"


class TestCommit:
    def test_commit_creates_head(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "a.txt").write_text("changed\n", encoding="utf-8")
        engine.stage_file("a.txt")
        sha = engine.create_commit("변경 커밋")

        assert str(repo.head.target) == sha
        assert repo[repo.head.target].message == "변경 커밋"
        assert engine.working_tree_status().is_clean

    def test_empty_message_is_rejected(self, engine: LocalGitEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            engine.create_commit("   ")
        assert excinfo.value.action is not None

    def test_missing_identity_is_actionable(
        self,
        repo: pygit2.Repository,
        engine: LocalGitEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # libgit2는 GIT_CONFIG_GLOBAL 환경변수를 읽지 않아 머신 설정을
        # 우회하기 어렵다. user.name이 없을 때 default_signature가 던지는
        # KeyError를 property 패치로 재현해 오류 분기를 검증한다.
        monkeypatch.setattr(
            pygit2.Repository,
            "default_signature",
            property(lambda self: (_ for _ in ()).throw(KeyError("user.name"))),
        )
        with pytest.raises(EngineError) as excinfo:
            engine.create_commit("메시지")
        assert "user.name" in (excinfo.value.action or "")

    def test_initial_commit_on_unborn_head(self, tmp_path: Path) -> None:
        r = pygit2.init_repository(str(tmp_path / "fresh"), initial_head="main")
        r.config["user.name"] = "t"
        r.config["user.email"] = "t@e.com"
        (Path(r.workdir) / "f.txt").write_text("x\n", encoding="utf-8")

        engine = LocalGitEngine.open(r.workdir)
        engine.stage_file("f.txt")
        sha = engine.create_commit("첫 커밋")

        assert str(r.head.target) == sha
        assert r[r.head.target].parent_ids == []

    def test_amend_replaces_head_without_new_parent(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        original_parents = [str(p) for p in repo[repo.head.target].parent_ids]
        (workdir(repo) / "extra.txt").write_text("x\n", encoding="utf-8")
        engine.stage_file("extra.txt")
        sha = engine.create_commit("고친 메시지", amend=True)

        head = repo[repo.head.target]
        assert str(repo.head.target) == sha
        assert head.message == "고친 메시지"
        assert [str(p) for p in head.parent_ids] == original_parents

    def test_amend_without_commits_is_rejected(self, tmp_path: Path) -> None:
        r = pygit2.init_repository(str(tmp_path / "fresh2"), initial_head="main")
        r.config["user.name"] = "t"
        r.config["user.email"] = "t@e.com"
        engine = LocalGitEngine.open(r.workdir)
        with pytest.raises(EngineError):
            engine.create_commit("x", amend=True)

    def test_commit_with_nothing_staged_is_rejected(
        self, engine: LocalGitEngine
    ) -> None:
        """리뷰 확정 결함의 회귀 테스트: 커밋 연타로 빈 중복 커밋이 쌓이면 안 된다."""
        with pytest.raises(EngineError) as excinfo:
            engine.create_commit("빈 커밋 시도")
        assert "스테이징" in excinfo.value.message

    def test_commit_during_merge_records_merge_head_as_parent(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """리뷰 확정 결함의 회귀 테스트: 머지 중 커밋은 부모가 2개여야 한다.

        MERGE_HEAD를 빼먹으면 머지가 일반 커밋으로 둔갑해 히스토리가 손상된다.
        """
        main_tip = repo.head.target
        engine.create_branch("side", checkout=True)
        (workdir(repo) / "side.txt").write_text("side\n", encoding="utf-8")
        engine.stage_file("side.txt")
        side_sha = engine.create_commit("side 커밋")
        engine.checkout_branch("main")

        # 외부 git이 하듯 머지 상태를 만든다 (충돌 없는 머지)
        repo.merge(pygit2.Oid(hex=side_sha))
        assert repo.lookup_reference("MERGE_HEAD") is not None

        merge_sha = engine.create_commit("Merge branch 'side'")

        merge_commit = repo[pygit2.Oid(hex=merge_sha)]
        parents = [str(p) for p in merge_commit.parent_ids]
        assert parents == [str(main_tip), side_sha]
        # 머지 상태가 정리되어 다음 커밋이 다시 단일 부모가 된다
        with pytest.raises(KeyError):
            repo.lookup_reference("MERGE_HEAD")

    def test_amend_during_merge_is_rejected(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        engine.create_branch("side2", checkout=True)
        (workdir(repo) / "s2.txt").write_text("x\n", encoding="utf-8")
        engine.stage_file("s2.txt")
        side_sha = engine.create_commit("side2 커밋")
        engine.checkout_branch("main")
        repo.merge(pygit2.Oid(hex=side_sha))

        with pytest.raises(EngineError):
            engine.create_commit("고치기", amend=True)
        repo.state_cleanup()


class TestBranchOps:
    def test_create_branch(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        engine.create_branch("feat")
        assert "feat" in repo.branches.local
        assert repo.head.shorthand == "main"  # checkout=False가 기본

    def test_create_and_checkout(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        engine.create_branch("feat", checkout=True)
        assert repo.head.shorthand == "feat"

    def test_duplicate_name_is_rejected(self, engine: LocalGitEngine) -> None:
        engine.create_branch("dup")
        with pytest.raises(EngineError):
            engine.create_branch("dup")

    def test_checkout_switches_head(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        engine.create_branch("feat")
        engine.checkout_branch("feat")
        assert repo.head.shorthand == "feat"

    def test_conflicting_checkout_is_actionable(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        # other 브랜치에서 a.txt를 다르게 커밋해 두고,
        # main의 워킹트리를 더럽힌 채 전환을 시도한다.
        engine.create_branch("other", checkout=True)
        (workdir(repo) / "a.txt").write_text("other version\n", encoding="utf-8")
        engine.stage_file("a.txt")
        engine.create_commit("other a")
        engine.checkout_branch("main")

        (workdir(repo) / "a.txt").write_text("dirty\n", encoding="utf-8")
        with pytest.raises(EngineError) as excinfo:
            engine.checkout_branch("other")
        assert "stash" in (excinfo.value.action or "")

    def test_delete_branch(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        engine.create_branch("gone")
        engine.delete_branch("gone")
        assert "gone" not in repo.branches.local

    def test_deleting_current_branch_is_rejected(
        self, engine: LocalGitEngine
    ) -> None:
        with pytest.raises(EngineError) as excinfo:
            engine.delete_branch("main")
        assert excinfo.value.action is not None

    def test_deleting_missing_branch_is_rejected(
        self, engine: LocalGitEngine
    ) -> None:
        with pytest.raises(EngineError):
            engine.delete_branch("nope")


class TestStash:
    def test_stash_and_pop_roundtrip(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "a.txt").write_text("wip\n", encoding="utf-8")
        engine.stash_save("작업 중")

        assert (workdir(repo) / "a.txt").read_text(encoding="utf-8") == "one\n"
        assert engine.stash_count() == 1

        engine.stash_pop()
        assert (workdir(repo) / "a.txt").read_text(encoding="utf-8") == "wip\n"
        assert engine.stash_count() == 0

    def test_stash_includes_untracked(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "new.txt").write_text("x\n", encoding="utf-8")
        engine.stash_save()
        assert not (workdir(repo) / "new.txt").exists()
        engine.stash_pop()
        assert (workdir(repo) / "new.txt").exists()

    def test_stash_nothing_is_rejected(self, engine: LocalGitEngine) -> None:
        with pytest.raises(EngineError):
            engine.stash_save()

    def test_pop_without_stash_is_rejected(self, engine: LocalGitEngine) -> None:
        with pytest.raises(EngineError):
            engine.stash_pop()


class TestWorkdirDiff:
    def test_unstaged_diff(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "a.txt").write_text("one\nadded\n", encoding="utf-8")
        lines = engine.workdir_diff_lines("a.txt", staged=False)
        texts = [line.text for line in lines]
        assert "added" in texts

    def test_staged_diff(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "a.txt").write_text("one\nstaged!\n", encoding="utf-8")
        engine.stage_file("a.txt")
        lines = engine.workdir_diff_lines("a.txt", staged=True)
        texts = [line.text for line in lines]
        assert "staged!" in texts

    def test_diff_is_scoped_to_requested_path(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (workdir(repo) / "a.txt").write_text("x\n", encoding="utf-8")
        (workdir(repo) / "b.txt").write_text("y\n", encoding="utf-8")
        lines = engine.workdir_diff_lines("b.txt", staged=False)
        from gitclient.domain.models import DiffLineKind

        headers = [
            line.text for line in lines if line.kind is DiffLineKind.FILE_HEADER
        ]
        assert headers == ["b.txt"]

    def test_untracked_file_diff_shows_content(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """리뷰 확정 결함의 회귀 테스트: 미추적 파일 클릭 시 빈 diff가 나왔다."""
        (workdir(repo) / "brand-new.txt").write_text("완전히 새 파일\n", encoding="utf-8")
        lines = engine.workdir_diff_lines("brand-new.txt", staged=False)
        assert any("완전히 새 파일" in line.text for line in lines)

    def test_staged_diff_on_unborn_head_shows_new_file(
        self, tmp_path: Path
    ) -> None:
        """리뷰 확정 결함의 회귀 테스트: 첫 커밋 전 staged diff가 예외를 던졌다."""
        r = pygit2.init_repository(str(tmp_path / "unborn-diff"), initial_head="main")
        (Path(r.workdir) / "f.txt").write_text("첫 내용\n", encoding="utf-8")
        engine = LocalGitEngine.open(r.workdir)
        engine.stage_file("f.txt")

        lines = engine.workdir_diff_lines("f.txt", staged=True)
        assert any("첫 내용" in line.text for line in lines)
