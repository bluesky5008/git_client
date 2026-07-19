"""pull의 로컬 절반(병합) 통합 테스트.

전송은 git CLI가, 병합은 pygit2가 맡는다(ADR-2). 여기서는 pygit2 쪽만
검증한다 — 원격에서 받아온 뒤 무엇을 어떻게 합치는가.

**이 파일이 존재하는 이유**: 프로브에서 빨리 감기의 순서를 틀렸을 때
libgit2가 오류를 내지 않고 워킹트리를 조용히 어긋난 상태로 남겼다.
수정이 반영되지 않고 삭제도 적용되지 않은 채 인덱스에는 엉뚱한 변경이
스테이징돼 있었다. 오류가 없으므로 테스트만이 이걸 잡는다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gitclient.domain.errors import EngineError
from gitclient.domain.models import MergeKind
from gitclient.infrastructure.local_engine import LocalGitEngine
from gitclient.infrastructure.remote_engine import RemoteEngine
from tests.integration.remote_harness import AUTHOR_ENV, RemoteFixture, git

UPSTREAM = "refs/remotes/origin/main"


@pytest.fixture
def remote(tmp_path: Path) -> RemoteFixture:
    return RemoteFixture(tmp_path).build(commits=3, payload_kb=1)


@pytest.fixture
def engine(remote: RemoteFixture) -> LocalGitEngine:
    return LocalGitEngine.open(str(remote.work))


def status(work: Path) -> str:
    return git("status", "--porcelain", cwd=work).stdout.strip()


class TestMergePreview:
    def test_up_to_date(self, engine: LocalGitEngine) -> None:
        assert engine.merge_preview(UPSTREAM).kind is MergeKind.UP_TO_DATE

    def test_fast_forward_when_only_remote_moved(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        remote.add_and_publish(2)
        RemoteEngine(remote.work).fetch()

        assert engine.merge_preview(UPSTREAM).kind is MergeKind.FAST_FORWARD

    def test_merge_required_when_both_moved(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        remote.diverge()
        RemoteEngine(remote.work).fetch()

        assert engine.merge_preview(UPSTREAM).kind is MergeKind.MERGE_REQUIRED

    def test_preview_does_not_touch_the_repository(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        remote.add_and_publish(1)
        RemoteEngine(remote.work).fetch()
        before = remote.work_head()

        engine.merge_preview(UPSTREAM)

        assert remote.work_head() == before
        assert status(remote.work) == ""

    def test_missing_upstream_is_actionable(self, engine: LocalGitEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            engine.merge_preview("refs/remotes/origin/nonexistent")
        assert excinfo.value.action is not None


class TestFastForward:
    def test_moves_head_to_upstream(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        remote.add_and_publish(2)
        RemoteEngine(remote.work).fetch()

        engine.fast_forward(UPSTREAM)

        assert remote.work_head() == remote.origin_head()

    def test_working_tree_actually_reflects_the_new_commit(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        """확정된 결함의 회귀 테스트: 순서를 틀리면 참조만 움직인다.

        참조를 먼저 옮기고 checkout하면 libgit2가 이미 같아진 HEAD와
        비교하게 되어 워킹트리를 갱신하지 않는다 — **오류 없이**.
        """
        remote.add_and_publish(2, payload_kb=2)
        RemoteEngine(remote.work).fetch()
        expected = remote.origin_head()

        engine.fast_forward(UPSTREAM)

        assert remote.work_head() == expected
        assert status(remote.work) == "", "워킹트리와 인덱스가 HEAD와 어긋났다"

    def test_applies_additions_and_deletions(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        """수정만이 아니라 추가·삭제도 워킹트리에 반영돼야 한다."""
        (remote.seed / "doomed.txt").write_text("사라질 파일\n", encoding="utf-8")
        git("add", "-A", cwd=remote.seed)
        git("commit", "--quiet", "-m", "파일 추가", cwd=remote.seed)
        remote.publish()
        RemoteEngine(remote.work).fetch()
        engine.fast_forward(UPSTREAM)
        assert (remote.work / "doomed.txt").exists()

        (remote.seed / "doomed.txt").unlink()
        (remote.seed / "fresh.txt").write_text("새 파일\n", encoding="utf-8")
        git("add", "-A", cwd=remote.seed)
        git("commit", "--quiet", "-m", "삭제와 추가", cwd=remote.seed)
        remote.publish()
        RemoteEngine(remote.work).fetch()

        engine.fast_forward(UPSTREAM)

        assert not (remote.work / "doomed.txt").exists(), "삭제가 적용되지 않았다"
        assert (remote.work / "fresh.txt").exists(), "추가가 적용되지 않았다"
        assert status(remote.work) == ""

    def test_up_to_date_is_a_no_op(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        before = remote.work_head()
        engine.fast_forward(UPSTREAM)
        assert remote.work_head() == before

    def test_refuses_when_merge_is_required(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        remote.diverge()
        RemoteEngine(remote.work).fetch()
        before = remote.work_head()

        with pytest.raises(EngineError):
            engine.fast_forward(UPSTREAM)

        assert remote.work_head() == before


class TestRepositoryStateGuards:
    """저장소가 특별한 상태일 때 빨리 감기를 시작하면 안 된다.

    전부 조용히 망가지는 유형이다 — 예외도 경고도 없이 히스토리나 워킹트리가
    어긋나므로 테스트만이 잡을 수 있다.
    """

    def test_refuses_while_a_merge_is_in_progress(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        """진행 중인 병합 위로 빨리 감으면 다음 커밋이 위조된 머지 커밋이 된다.

        MERGE_HEAD가 살아남은 채 HEAD만 앞으로 가면, 이후 사용자가 전혀
        무관한 파일 하나를 커밋할 때 create_commit이 그 MERGE_HEAD를 두 번째
        부모로 붙인다. 히스토리에는 브랜치 전체를 병합한 커밋이 박히고
        state_cleanup이 흔적까지 지운다. 실제 git도 이 상태를 거부한다.
        """
        # 병합을 시작해 두고 커밋하지 않은 상태를 만든다.
        # --no-commit 이 핵심이다: MERGE_HEAD는 남고 HEAD는 제자리이므로,
        # 원격이 앞서가면 merge_analysis가 FASTFORWARD를 낸다. 로컬 main을
        # 먼저 진행시키면 MERGE_REQUIRED로 빠져 **가드와 무관한 이유로**
        # 통과해버린다 — 실제로 처음 작성한 테스트가 그랬다.
        git("checkout", "--quiet", "-b", "feature", cwd=remote.work)
        (remote.work / "side.txt").write_text("feature\n", encoding="utf-8")
        git("add", "-A", cwd=remote.work)
        git(*AUTHOR_ENV, "commit", "--quiet", "-m", "feature", cwd=remote.work)
        git("checkout", "--quiet", "main", cwd=remote.work)
        subprocess.run(
            ["git", "merge", "--no-ff", "--no-commit", "feature"],
            cwd=str(remote.work), capture_output=True, text=True,
        )
        merge_head = Path(remote.work) / ".git" / "MERGE_HEAD"
        assert merge_head.exists(), "MERGE_HEAD가 없으면 이 테스트는 무의미하다"

        before = remote.work_head()
        remote.add_and_publish(1)
        RemoteEngine(remote.work).fetch()
        # 가드가 없으면 정말로 빨리 감기가 되는 상황인지 확인한다
        assert engine.merge_preview(UPSTREAM).kind is MergeKind.FAST_FORWARD

        with pytest.raises(EngineError) as excinfo:
            engine.fast_forward(UPSTREAM)

        assert excinfo.value.action is not None
        assert remote.work_head() == before, "병합 중인데 HEAD가 움직였다"
        assert merge_head.exists(), "되돌릴 단서가 지워졌다"

    def test_unborn_head_creates_the_branch(self, tmp_path: Path) -> None:
        """첫 커밋 전 저장소에 pull하면 브랜치가 만들어져야 한다.

        참조 생성에 실패하면 워킹트리는 이미 원격 내용으로 덮여 있는데 HEAD는
        unborn인 채라, 그 상태에서 커밋하면 원격과 영영 갈라지는 루트 커밋이
        된다.
        """
        source = RemoteFixture(tmp_path / "src").build(commits=2, payload_kb=1)
        empty = tmp_path / "empty"
        empty.mkdir()
        git("init", "--quiet", "-b", "main", str(empty))
        git("remote", "add", "origin", source.origin_uri, cwd=empty)
        RemoteEngine(empty).fetch()

        LocalGitEngine.open(str(empty)).fast_forward(UPSTREAM)

        assert git("rev-parse", "HEAD", cwd=empty).stdout.strip() == (
            source.origin_head()
        )
        assert git("status", "--porcelain", cwd=empty).stdout.strip() == ""

    def test_refuses_when_the_branch_changed_under_it(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        """제출 시점과 실행 시점의 브랜치가 다르면 거부해야 한다.

        pull의 네트워크 절반이 도는 동안 사용자가 브랜치를 전환하면, 고정하지
        않은 작업은 엉뚱한 브랜치를 원격 위치로 덮어쓴다 — 오류 없이,
        reflog로만 복구 가능하게.
        """
        remote.add_and_publish(2)
        RemoteEngine(remote.work).fetch()
        git("checkout", "--quiet", "-b", "elsewhere", cwd=remote.work)
        before = remote.work_head()

        with pytest.raises(EngineError) as excinfo:
            engine.fast_forward(UPSTREAM, "main")

        assert "main" in excinfo.value.message
        assert remote.work_head() == before, "엉뚱한 브랜치가 움직였다"

    def test_accepts_the_pinned_branch(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        remote.add_and_publish(1)
        RemoteEngine(remote.work).fetch()

        engine.fast_forward(UPSTREAM, "main")

        assert remote.work_head() == remote.origin_head()


class TestUpstreamResolution:
    """추적 대상은 설정에서 읽는다 — 규약으로 추측하면 조용히 틀린다."""

    def test_reads_configured_upstream(self, engine: LocalGitEngine) -> None:
        assert engine.upstream_of_head() == ("origin", "refs/remotes/origin/main")

    def test_detached_head_has_no_upstream(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        """분리된 HEAD에서 규약으로 조합하면 refs/remotes/origin/HEAD가 된다.

        그 참조는 모든 clone에 존재하고 원격 기본 브랜치를 가리키므로,
        bisect 중인 사용자의 HEAD가 조용히 그쪽으로 끌려간다.
        """
        git("checkout", "--quiet", "--detach", "HEAD~1", cwd=remote.work)

        assert LocalGitEngine.open(str(remote.work)).upstream_of_head() is None

    def test_branch_without_upstream(self, remote: RemoteFixture) -> None:
        git("checkout", "--quiet", "-b", "brand-new", cwd=remote.work)
        assert LocalGitEngine.open(str(remote.work)).upstream_of_head() is None

    def test_non_origin_upstream_is_respected(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        """fork 워크플로: origin은 내 fork, upstream은 원본.

        규약으로 추측하면 fork의 뒤처진 브랜치를 대상으로 삼고 "이미 최신"이라
        답한다.
        """
        canonical = RemoteFixture(tmp_path / "canon").build(commits=2, payload_kb=1)
        git("remote", "add", "upstream", canonical.origin_uri, cwd=remote.work)
        git("fetch", "--quiet", "upstream", cwd=remote.work)
        git("branch", "--set-upstream-to=upstream/main", "main", cwd=remote.work)

        resolved = LocalGitEngine.open(str(remote.work)).upstream_of_head()

        assert resolved == ("upstream", "refs/remotes/upstream/main")


class TestUncommittedWorkIsSafe:
    """커밋하지 않은 작업을 조용히 덮어쓰면 안 된다.

    reset --hard로 빨리 감으면 정확히 그 일이 일어난다(프로브에서 확인).
    checkout 우선 순서는 실패해도 안전하다.
    """

    def test_dirty_file_blocks_fast_forward(
        self, remote: RemoteFixture, engine: LocalGitEngine
    ) -> None:
        target = remote.work / "f0.txt"
        assert target.exists()
        remote.add_and_publish(1)
        RemoteEngine(remote.work).fetch()

        # 같은 파일을 원격에서도 바꾸게 만든다
        (remote.seed / "f0.txt").write_text("원격이 바꾼 내용\n", encoding="utf-8")
        git("add", "-A", cwd=remote.seed)
        git("commit", "--quiet", "-m", "원격 수정", cwd=remote.seed)
        remote.publish()
        RemoteEngine(remote.work).fetch()

        mine = "내가 작업하던 내용\n"
        target.write_text(mine, encoding="utf-8")
        before = remote.work_head()

        with pytest.raises(EngineError) as excinfo:
            engine.fast_forward(UPSTREAM)

        assert target.read_text(encoding="utf-8") == mine, "내 작업이 사라졌다"
        assert remote.work_head() == before, "실패했는데 참조가 움직였다"
        assert excinfo.value.action is not None
