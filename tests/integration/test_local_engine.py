"""LocalGitEngine 통합 테스트.

임시 디렉터리에 실제 저장소를 만들어 검증한다. 네트워크에 의존하지 않는다.
(doc/design.md §8)
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pygit2
import pytest

from gitclient.domain.errors import (
    EngineError,
    GitClientError,
    RepositoryNotFoundError,
)
from gitclient.domain.models import ChangeStatus, DiffLineKind, RefKind
from gitclient.infrastructure.local_engine import LocalGitEngine

SIGNATURE = pygit2.Signature("테스터", "tester@example.com", 1700000000, 540)


def commit_file(
    repo: pygit2.Repository,
    path: str,
    content: str,
    message: str,
    parents: list[str] | None = None,
) -> str:
    """파일 하나를 쓰고 커밋한다. 커밋 SHA를 반환한다."""
    file_path = Path(repo.workdir) / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    repo.index.add(path)
    repo.index.write()
    tree = repo.index.write_tree()

    if parents is None:
        parents = [] if repo.head_is_unborn else [str(repo.head.target)]

    oid = repo.create_commit(
        "HEAD" if parents == [] or not repo.head_is_unborn else "HEAD",
        SIGNATURE,
        SIGNATURE,
        message,
        tree,
        [pygit2.Oid(hex=p) for p in parents],
    )
    return str(oid)


@pytest.fixture
def repo(tmp_path: Path) -> pygit2.Repository:
    """커밋 3개짜리 선형 히스토리를 가진 저장소."""
    r = pygit2.init_repository(str(tmp_path / "sample"), initial_head="main")
    commit_file(r, "a.txt", "first\n", "첫 번째 커밋")
    commit_file(r, "b.txt", "second\n", "두 번째 커밋")
    commit_file(r, "a.txt", "first\nchanged\n", "a.txt 수정")
    return r


@pytest.fixture
def engine(repo: pygit2.Repository) -> LocalGitEngine:
    return LocalGitEngine.open(repo.workdir)


class TestOpen:
    def test_opens_from_workdir(self, repo: pygit2.Repository) -> None:
        engine = LocalGitEngine.open(repo.workdir)
        assert engine.info().is_empty is False

    def test_opens_from_subdirectory(self, repo: pygit2.Repository) -> None:
        subdir = Path(repo.workdir) / "nested" / "deep"
        subdir.mkdir(parents=True)
        engine = LocalGitEngine.open(subdir)
        assert engine.info().head_shorthand == "main"

    def test_missing_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RepositoryNotFoundError):
            LocalGitEngine.open(tmp_path / "does-not-exist")

    def test_non_repository_raises(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(RepositoryNotFoundError):
            LocalGitEngine.open(plain)


class TestRepositoryInfo:
    def test_reports_head_branch(self, engine: LocalGitEngine) -> None:
        assert engine.info().head_shorthand == "main"

    def test_lists_local_branch(self, engine: LocalGitEngine) -> None:
        refs = engine.info().refs
        local = [r for r in refs if r.kind is RefKind.LOCAL_BRANCH]
        assert [r.shorthand for r in local] == ["main"]

    def test_marks_head_branch(self, engine: LocalGitEngine) -> None:
        head_refs = [r for r in engine.info().refs if r.is_head]
        assert [r.shorthand for r in head_refs] == ["main"]

    def test_display_name_uses_directory_name(self, engine: LocalGitEngine) -> None:
        assert engine.info().display_name == "sample"


class TestIterCommits:
    def test_returns_commits_newest_first(self, engine: LocalGitEngine) -> None:
        summaries = [c.summary for c in engine.iter_commits()]
        assert summaries == ["a.txt 수정", "두 번째 커밋", "첫 번째 커밋"]

    def test_respects_limit(self, engine: LocalGitEngine) -> None:
        assert len(list(engine.iter_commits(limit=2))) == 2

    def test_parents_are_linked(self, engine: LocalGitEngine) -> None:
        commits = list(engine.iter_commits())
        assert commits[0].parents == (commits[1].sha,)
        assert commits[-1].parents == ()

    def test_signature_is_converted(self, engine: LocalGitEngine) -> None:
        commit = next(iter(engine.iter_commits()))
        assert commit.author.name == "테스터"
        assert commit.author.when.utcoffset() is not None

    def test_empty_repository_yields_nothing(self, tmp_path: Path) -> None:
        pygit2.init_repository(str(tmp_path / "empty"), initial_head="main")
        engine = LocalGitEngine.open(tmp_path / "empty")
        assert list(engine.iter_commits()) == []


class TestBranchesAreIncluded:
    """HEAD에서 닿지 않는 브랜치의 커밋도 그래프에 나와야 한다."""

    def test_commits_on_other_branch_are_walked(
        self, repo: pygit2.Repository
    ) -> None:
        base = str(repo.head.target)
        repo.branches.local.create("side", repo[base])
        repo.checkout(repo.branches.local["side"])
        commit_file(repo, "side.txt", "side\n", "사이드 브랜치 커밋")
        repo.checkout(repo.branches.local["main"])

        engine = LocalGitEngine.open(repo.workdir)
        summaries = [c.summary for c in engine.iter_commits()]
        assert "사이드 브랜치 커밋" in summaries


class TestCommitDetail:
    def test_lists_changed_files(self, engine: LocalGitEngine) -> None:
        head = next(iter(engine.iter_commits()))
        detail = engine.commit_detail(head.sha)
        assert [c.path for c in detail.changes] == ["a.txt"]

    def test_reports_modification_status(self, engine: LocalGitEngine) -> None:
        head = next(iter(engine.iter_commits()))
        detail = engine.commit_detail(head.sha)
        assert detail.changes[0].status is ChangeStatus.MODIFIED

    def test_counts_insertions(self, engine: LocalGitEngine) -> None:
        head = next(iter(engine.iter_commits()))
        detail = engine.commit_detail(head.sha)
        assert detail.total_insertions == 1

    def test_root_commit_shows_added_file(self, engine: LocalGitEngine) -> None:
        root = list(engine.iter_commits())[-1]
        detail = engine.commit_detail(root.sha)
        assert detail.changes[0].status is ChangeStatus.ADDED


class TestDiffLines:
    def test_emits_file_header(self, engine: LocalGitEngine) -> None:
        head = next(iter(engine.iter_commits()))
        lines = engine.diff_lines(head.sha)
        assert lines[0].kind is DiffLineKind.FILE_HEADER
        assert lines[0].text == "a.txt"

    def test_emits_hunk_header(self, engine: LocalGitEngine) -> None:
        head = next(iter(engine.iter_commits()))
        kinds = [line.kind for line in engine.diff_lines(head.sha)]
        assert DiffLineKind.HUNK_HEADER in kinds

    def test_emits_added_line_with_new_lineno(self, engine: LocalGitEngine) -> None:
        head = next(iter(engine.iter_commits()))
        additions = [
            line
            for line in engine.diff_lines(head.sha)
            if line.kind is DiffLineKind.ADDITION
        ]
        assert [line.text for line in additions] == ["changed"]
        assert additions[0].new_lineno == 2
        assert additions[0].old_lineno is None

    def test_filters_by_path(self, repo: pygit2.Repository) -> None:
        Path(repo.workdir, "a.txt").write_text("x\n", encoding="utf-8")
        Path(repo.workdir, "b.txt").write_text("y\n", encoding="utf-8")
        repo.index.add_all()
        repo.index.write()
        tree = repo.index.write_tree()
        repo.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "두 파일 수정", tree, [repo.head.target]
        )

        engine = LocalGitEngine.open(repo.workdir)
        head = next(iter(engine.iter_commits()))

        headers = [
            line.text
            for line in engine.diff_lines(head.sha, path="b.txt")
            if line.kind is DiffLineKind.FILE_HEADER
        ]
        assert headers == ["b.txt"]


class TestDiffMapping:
    """pygit2 delta → 도메인 모델 매핑의 분기별 검증.

    파싱은 libgit2가 한다 — 여기서 검증하는 것은 상태 문자, 이름 변경,
    바이너리 처리 같은 매핑 분기다. (doc/design.md §8)

    COPIED는 find_similar에 copy 탐지 플래그를 주지 않아 도달 불가,
    TYPECHANGE는 심링크가 필요해 Windows 이식성 문제로 제외한다.
    """

    def test_deleted_file_maps_to_deleted_status(
        self, repo: pygit2.Repository
    ) -> None:
        (Path(repo.workdir) / "b.txt").unlink()
        repo.index.remove("b.txt")
        repo.index.write()
        tree = repo.index.write_tree()
        repo.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "b.txt 삭제", tree, [repo.head.target]
        )

        engine = LocalGitEngine.open(repo.workdir)
        head = next(iter(engine.iter_commits()))
        detail = engine.commit_detail(head.sha)

        assert [c.status for c in detail.changes] == [ChangeStatus.DELETED]
        kinds = [line.kind for line in engine.diff_lines(head.sha)]
        assert DiffLineKind.DELETION in kinds

    def test_renamed_file_maps_to_renamed_with_old_path(
        self, repo: pygit2.Repository
    ) -> None:
        # 내용을 그대로 두고 경로만 바꾼다 → 유사도 100%라 탐지가 안정적이다.
        workdir = Path(repo.workdir)
        (workdir / "renamed.txt").write_bytes((workdir / "b.txt").read_bytes())
        (workdir / "b.txt").unlink()
        repo.index.remove("b.txt")
        repo.index.add("renamed.txt")
        repo.index.write()
        tree = repo.index.write_tree()
        repo.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "b.txt 이름 변경", tree, [repo.head.target]
        )

        engine = LocalGitEngine.open(repo.workdir)
        head = next(iter(engine.iter_commits()))
        detail = engine.commit_detail(head.sha)

        change = detail.changes[0]
        assert change.status is ChangeStatus.RENAMED
        assert change.old_path == "b.txt"
        assert change.path == "renamed.txt"
        assert change.display_path == "b.txt -> renamed.txt"

        headers = [
            line.text
            for line in engine.diff_lines(head.sha)
            if line.kind is DiffLineKind.FILE_HEADER
        ]
        assert headers == ["b.txt -> renamed.txt"]

    def test_binary_file_shows_notice_instead_of_hunks(
        self, repo: pygit2.Repository
    ) -> None:
        (Path(repo.workdir) / "blob.bin").write_bytes(b"\x00\x01\x02\xff" * 64)
        repo.index.add("blob.bin")
        repo.index.write()
        tree = repo.index.write_tree()
        repo.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "바이너리 추가", tree, [repo.head.target]
        )

        engine = LocalGitEngine.open(repo.workdir)
        head = next(iter(engine.iter_commits()))
        lines = engine.diff_lines(head.sha, path="blob.bin")

        kinds = [line.kind for line in lines]
        assert DiffLineKind.HUNK_HEADER not in kinds
        assert any("바이너리" in line.text for line in lines)


class TestClonedRepository:
    """clone된 저장소 — 합성 fixture가 오래 놓쳤던 현실 형태.

    clone에는 반드시 심볼릭 참조 `origin/HEAD`가 생긴다. 그 target은 Oid가
    아니라 문자열이라, Oid로 취급하면 커밋 순회가 시작조차 못 하고 죽는다.
    이 테스트 묶음은 그 회귀를 막는다.
    """

    @pytest.fixture
    def clone(self, repo: pygit2.Repository, tmp_path: Path) -> Path:
        dest = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "--quiet", str(repo.workdir), str(dest)],
            check=True,
            capture_output=True,
        )
        return dest

    def test_iter_commits_survives_origin_head(self, clone: Path) -> None:
        engine = LocalGitEngine.open(clone)
        summaries = [c.summary for c in engine.iter_commits()]
        assert "a.txt 수정" in summaries

    def test_refs_hide_symbolic_origin_head(self, clone: Path) -> None:
        engine = LocalGitEngine.open(clone)
        refs = engine.refs()
        shorthands = [r.shorthand for r in refs]
        assert "origin/HEAD" not in shorthands
        assert "origin/main" in shorthands

    def test_all_ref_targets_are_shas(self, clone: Path) -> None:
        # 심볼릭 참조를 Oid로 오인하면 target_sha에 ref 경로 문자열이 들어간다.
        engine = LocalGitEngine.open(clone)
        for ref in engine.refs():
            assert len(ref.target_sha) == 40, f"{ref.shorthand}: {ref.target_sha}"


class TestExceptionBoundary:
    """raw pygit2 예외가 엔진 밖으로 새지 않는지 검증한다. (doc/design.md §7)"""

    def test_missing_object_raises_engine_error(
        self, engine: LocalGitEngine
    ) -> None:
        with pytest.raises(EngineError) as excinfo:
            engine.commit_detail("0" * 40)
        assert excinfo.value.action is not None  # 권장 조치가 실려 있다

    def test_shallow_boundary_diff_raises_domain_error(
        self, repo: pygit2.Repository, tmp_path: Path
    ) -> None:
        """shallow 경계 커밋은 부모 오브젝트가 없다 — diff가 도메인 예외를 내야 한다."""
        shallow = tmp_path / "shallow"
        source = Path(repo.workdir).resolve().as_uri()  # file:// 라야 --depth가 동작
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", source, str(shallow)],
            check=True,
            capture_output=True,
        )

        engine = LocalGitEngine.open(shallow)
        assert engine.info(include_refs=False).is_shallow is True

        boundary = next(iter(engine.iter_commits()))
        if not boundary.parents:
            pytest.skip("git이 경계 커밋의 부모를 잘라냈다 — 이 형태에서는 재현 불가")

        # raw KeyError/GitError가 아니라 GitClientError 계열이어야 UI가 잡는다.
        with pytest.raises(GitClientError):
            engine.diff_lines(boundary.sha)

    def test_tag_to_non_commit_is_skipped_with_log(
        self, repo: pygit2.Repository, caplog: pytest.LogCaptureFixture
    ) -> None:
        """커밋이 아닌 오브젝트를 가리키는 태그는 제외하되 로그를 남긴다. (§7 ADR-13)"""
        blob_oid = repo.create_blob(b"raw data\n")
        repo.references.create("refs/tags/blob-tag", blob_oid)

        engine = LocalGitEngine.open(repo.workdir)
        with caplog.at_level(logging.WARNING, logger="gitclient"):
            refs = engine.refs()

        assert "blob-tag" not in [r.shorthand for r in refs]
        assert any("blob-tag" in record.message for record in caplog.records)


class TestMergeCommit:
    """머지 커밋은 첫 부모 기준으로 diff를 낸다."""

    @pytest.fixture
    def merged(self, repo: pygit2.Repository) -> pygit2.Repository:
        base = repo.head.target
        repo.branches.local.create("side", repo[str(base)])
        repo.checkout(repo.branches.local["side"])
        commit_file(repo, "side.txt", "side\n", "사이드 커밋")
        side_tip = repo.head.target
        repo.checkout(repo.branches.local["main"])

        repo.merge(side_tip)
        tree = repo.index.write_tree()
        repo.create_commit(
            "HEAD",
            SIGNATURE,
            SIGNATURE,
            "Merge branch 'side'",
            tree,
            [repo.head.target, side_tip],
        )
        repo.state_cleanup()
        return repo

    def test_merge_commit_has_two_parents(self, merged: pygit2.Repository) -> None:
        engine = LocalGitEngine.open(merged.workdir)
        head = next(iter(engine.iter_commits()))
        assert len(head.parents) == 2

    def test_merge_commit_is_flagged(self, merged: pygit2.Repository) -> None:
        engine = LocalGitEngine.open(merged.workdir)
        head = next(iter(engine.iter_commits()))
        assert head.is_merge is True

    def test_merge_diff_shows_second_parent_changes(
        self, merged: pygit2.Repository
    ) -> None:
        engine = LocalGitEngine.open(merged.workdir)
        head = next(iter(engine.iter_commits()))
        detail = engine.commit_detail(head.sha)
        # 첫 부모(main) 기준이므로 side 브랜치가 더한 파일이 보인다.
        assert [c.path for c in detail.changes] == ["side.txt"]
