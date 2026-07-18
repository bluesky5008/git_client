"""RemoteEngine 통합 테스트 — 실제 git으로 fetch하고 계측을 확인한다.

단위 테스트(test_instrumentation.py)는 "이런 출력을 이렇게 해석한다"를
보장한다. 여기서는 **git이 실제로 그런 출력을 내는가**를 확인한다 —
git 버전이나 설정이 바뀌어 형식이 달라지면 계측이 조용히 비게 되므로,
이 층의 검증이 없으면 측정값을 믿을 수 없다.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from gitclient.domain.errors import EngineError
from gitclient.domain.instrumentation import OperationKind
from gitclient.infrastructure.remote_engine import RemoteEngine
from tests.integration.remote_harness import RemoteFixture, git


@pytest.fixture
def remote(tmp_path: Path) -> RemoteFixture:
    return RemoteFixture(tmp_path).build(commits=5, payload_kb=4)


@pytest.fixture
def engine(remote: RemoteFixture) -> RemoteEngine:
    return RemoteEngine(remote.work)


class TestFetchBasics:
    def test_lists_remotes(self, engine: RemoteEngine) -> None:
        assert engine.list_remotes() == ["origin"]

    def test_fetch_brings_new_commits(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        remote.add_and_publish(3, payload_kb=4)
        before = remote.work_remote_head()

        engine.fetch()

        assert remote.work_remote_head() != before
        assert remote.work_remote_head() == remote.origin_head()

    def test_fetch_reports_ref_updates(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        remote.add_and_publish(2, payload_kb=4)
        stats = engine.fetch()

        assert stats.ref_updates
        update = stats.ref_updates[0]
        assert update.local_ref == "origin/main"
        assert update.remote_ref == "main"

    def test_no_op_fetch_transfers_nothing(self, engine: RemoteEngine) -> None:
        stats = engine.fetch()
        assert stats.transferred_anything is False
        assert stats.succeeded is True

    def test_no_op_fetch_records_zero_not_unmeasured(
        self, engine: RemoteEngine
    ) -> None:
        """변경 없는 fetch는 '측정 실패'가 아니라 '0바이트'다.

        일상 사용에서 이쪽이 대다수라, 미측정으로 세면 "일부 작업을 측정하지
        못했다"는 신호가 상시 오탐이 되어 진짜 측정 공백을 가린다.
        """
        stats = engine.fetch()
        assert stats.received_bytes == 0
        assert stats.transferred_anything is False
        assert stats.changed_anything is False

    def test_unmeasurable_fetch_stays_none(
        self, remote: RemoteFixture, engine: RemoteEngine, monkeypatch
    ) -> None:  # noqa: ANN001
        """팩이 풀려 측정에 실패하면 0이 아니라 None이어야 한다.

        0으로 접으면 진짜 측정 실패가 "0바이트 전송"으로 둔갑해 누적 집계가
        과소 보고된다 — 위 테스트와 정확히 반대 방향의 오류다.
        """
        from gitclient.infrastructure import remote_engine as module

        monkeypatch.setattr(
            module, "BASE_CONFIG", ("protocol.version=2", "transfer.unpackLimit=100")
        )
        remote.add_and_publish(1, payload_kb=4)
        assert engine.fetch().received_bytes is None

    def test_ref_only_fetch_is_a_change(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        """이미 가진 커밋을 가리키는 새 브랜치 — 팩은 안 오지만 변경은 맞다."""
        remote.create_remote_branch("side")

        stats = engine.fetch()

        assert stats.transferred_anything is False  # 받을 객체가 없다
        assert stats.changed_anything is True  # 그래도 origin/side가 생겼다
        assert any(u.local_ref == "origin/side" for u in stats.ref_updates)

    def test_pruned_branch_is_reported(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        """prune 삭제를 놓치면 이미 사라진 원격 브랜치가 화면에 계속 남는다."""
        remote.create_remote_branch("doomed")
        engine.fetch()
        remote.delete_remote_branch("doomed")

        stats = engine.fetch()

        assert stats.changed_anything is True
        deleted = [u for u in stats.ref_updates if u.deleted]
        assert [u.local_ref for u in deleted] == ["origin/doomed"]

    def test_forced_update_is_counted(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        """force-push된 원격에서 참조 갱신이 0건으로 보고되면 안 된다."""
        remote.force_push_rewrite()

        stats = engine.fetch()

        assert [u.local_ref for u in stats.ref_updates] == ["origin/main"]
        assert stats.changed_anything is True


class TestInstrumentation:
    """계측이 실제로 채워지는지 — 이 테스트들이 Phase 3 최적화의 근거가 된다."""

    def test_received_bytes_are_measured(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        """전송 바이트가 측정돼야 한다.

        기본 설정(transfer.unpackLimit=100)이면 작은 fetch가 통째로 풀려
        이 값이 None이 된다. BASE_CONFIG의 unpackLimit=1이 그것을 막는다.
        """
        remote.add_and_publish(3, payload_kb=8)
        stats = engine.fetch()

        assert stats.received_bytes is not None, "전송 바이트를 측정하지 못했다"
        assert stats.received_bytes > 0

    def test_object_counts_are_measured(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        remote.add_and_publish(3, payload_kb=4)
        stats = engine.fetch()

        assert stats.received_objects is not None
        assert stats.total_objects is not None
        assert stats.received_objects > 0

    def test_protocol_v2_is_negotiated(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        """ref 광고량을 줄이는 v2가 실제로 쓰이는지 확인한다 (performance.md §2.1)."""
        remote.add_and_publish(1, payload_kb=1)
        stats = engine.fetch()
        assert stats.protocol_version == 2

    def test_negotiation_rounds_are_measured(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        """왕복 횟수는 RTT가 큰 회선에서 협상 알고리즘 판단 근거가 된다."""
        remote.add_and_publish(2, payload_kb=1)
        stats = engine.fetch()
        assert stats.negotiation_rounds is not None
        assert stats.negotiation_rounds >= 1

    def test_regions_are_captured(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        """구간별 소요시간 — 협상과 전송 중 어디가 느린지 가른다."""
        remote.add_and_publish(2, payload_kb=4)
        stats = engine.fetch()

        labels = {label for label, _ in stats.regions}
        assert "fetch.remote_refs" in labels
        assert stats.region_ms("fetch.remote_refs") is not None

    def test_duration_is_measured(self, engine: RemoteEngine) -> None:
        stats = engine.fetch()
        assert stats.duration_ms >= 0

    def test_kind_and_remote_are_recorded(self, engine: RemoteEngine) -> None:
        stats = engine.fetch()
        assert stats.kind is OperationKind.FETCH
        assert stats.remote == "origin"


class TestTransferReduction:
    """전송량을 줄이는 설정이 실제로 효과가 있는지 — 주장이 아니라 측정으로."""

    def test_no_tags_avoids_tag_transfer(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        """태그를 기본으로 받지 않는다 (performance.md §2.2).

        태그가 수천 개인 저장소에서 매번 전부 받으면 누적 전송량이 커진다.
        """
        for index in range(5):
            remote.create_remote_tag(f"v{index}.0")

        engine.fetch(tags=False)
        fetched_tags = git("tag", "--list", cwd=remote.work).stdout.split()
        assert fetched_tags == [], "태그를 받지 않아야 하는데 받았다"

        engine.fetch(tags=True)
        fetched_tags = git("tag", "--list", cwd=remote.work).stdout.split()
        assert len(fetched_tags) == 5, "명시적으로 요청하면 받아와야 한다"

    def test_refspec_limits_what_is_fetched(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        """필요한 ref만 지정하면 나머지는 가져오지 않는다."""
        remote.create_remote_branch("side")
        remote.add_and_publish(1, payload_kb=1)

        engine.fetch(refspecs=["+refs/heads/main:refs/remotes/origin/main"])

        refs = git("branch", "-r", cwd=remote.work).stdout
        assert "origin/main" in refs
        assert "origin/side" not in refs


class TestFailures:
    def test_unknown_remote_is_actionable(self, engine: RemoteEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            engine.fetch("nonexistent")
        assert excinfo.value.action is not None
        assert excinfo.value.detail  # git 원문을 보존한다

    def test_missing_repository_is_reported(self, tmp_path: Path) -> None:
        broken = RemoteEngine(tmp_path / "nowhere")
        with pytest.raises(EngineError):
            broken.fetch()

    def test_timeout_is_actionable(self, remote: RemoteFixture) -> None:
        engine = RemoteEngine(remote.work)
        with pytest.raises(EngineError) as excinfo:
            engine.fetch(timeout_s=0)
        assert "초 안에" in excinfo.value.message or excinfo.value.action

    @pytest.mark.skipif(os.name != "nt", reason="느린 자식 스크립트가 Windows 전용")
    def test_timeout_bounds_a_slow_child(self, remote: RemoteFixture) -> None:
        """timeout이 **실제 상한**이어야 한다 — 느린 자손이 붙어 있어도.

        위의 timeout_s=0 테스트로는 이걸 보장할 수 없다. 살아 있는 자손이
        없으므로 항상 즉시 통과하기 때문이다. 실제로는 자손(git-remote-https,
        ssh, credential helper)이 stderr 파이프를 상속하므로, git만 죽이면
        파이프가 닫히지 않아 Windows의 kill-후-communicate가 반환하지 않는다.
        수정 전 실측: timeout_s=3인데 자손 수명 40초를 그대로 기다렸다.
        """
        slow = remote.root / "slow_upload_pack.bat"
        slow.write_text("@ping -n 30 127.0.0.1 > nul\n@git upload-pack %*\n")
        git(
            "config",
            "remote.origin.uploadpack",
            str(slow).replace("\\", "/"),
            cwd=remote.work,
        )

        engine = RemoteEngine(remote.work)
        started = time.perf_counter()
        with pytest.raises(EngineError):
            engine.fetch(timeout_s=3)
        elapsed = time.perf_counter() - started

        # 상한 3초 + 트리 종료와 드레인 여유. 수정 전에는 30초까지 갔다.
        assert elapsed < 15, f"timeout이 상한 노릇을 못 했다: {elapsed:.1f}초"


class TestArgumentInjection:
    """원격 이름과 refspec은 옵션이 아니다.

    git의 parse-options는 인자를 permute하므로, `-`로 시작하는 값이 옵션으로
    해석된다. `--upload-pack=<경로>`가 먹히면 **임의 명령이 실행된다** — 악성
    .git/config가 그런 이름의 원격을 심을 수 있고, `git remote`는 그 이름을
    그대로 나열한다. `--` 뒤에서는 git이 값을 URL/refspec으로만 읽는다.
    """

    def test_dash_leading_remote_name_is_not_an_option(
        self, engine: RemoteEngine
    ) -> None:
        with pytest.raises(EngineError) as excinfo:
            engine.fetch("--upload-pack=echo")
        # 옵션으로 먹히면 fetch가 성공해버린다. git이 거부해야 정상이다.
        assert excinfo.value.detail

    def test_dash_leading_refspec_is_not_an_option(
        self, engine: RemoteEngine
    ) -> None:
        with pytest.raises(EngineError):
            engine.fetch(refspecs=["--upload-pack=echo"])


class TestEnvironmentIsolation:
    """상속된 환경변수가 대상 저장소를 바꾸면 안 된다."""

    def test_inherited_git_dir_does_not_redirect_fetch(
        self, remote: RemoteFixture, tmp_path: Path, monkeypatch
    ) -> None:  # noqa: ANN001
        """GIT_DIR은 `-C <repo>`를 이긴다 — 걸러내지 않으면 엉뚱한 저장소를 받는다.

        앱을 GIT_DIR이 export된 셸이나 git 훅 아래에서 띄우면 발생한다.
        예외도 경고도 없이 다른 저장소가 갱신되고, 계측은 이 저장소 앞으로
        기록되어 누적 전송 바이트가 오귀속된다.
        """
        other = tmp_path / "other"
        git("clone", "--quiet", remote.origin_uri, str(other))
        before_other = git("rev-parse", "refs/remotes/origin/main", cwd=other).stdout
        before_work = remote.work_remote_head()

        remote.add_and_publish(1, payload_kb=2)
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))

        RemoteEngine(remote.work).fetch()

        # 확인은 GIT_DIR을 걷어낸 뒤에 한다 — 하네스의 git도 같은 환경을
        # 물려받으므로, 그대로 두면 검증 자체가 엉뚱한 저장소를 읽는다.
        monkeypatch.delenv("GIT_DIR")

        assert remote.work_remote_head() != before_work, "대상 저장소가 갱신돼야 한다"
        after_other = git("rev-parse", "refs/remotes/origin/main", cwd=other).stdout
        assert after_other == before_other, "엉뚱한 저장소가 갱신됐다"


class TestConfigIsolation:
    """앱 설정이 사용자 환경을 오염시키면 안 된다 (performance.md §2.1)."""

    def test_repo_config_is_untouched(
        self, remote: RemoteFixture, engine: RemoteEngine
    ) -> None:
        remote.add_and_publish(1, payload_kb=1)
        engine.fetch()

        config = git("config", "--local", "--list", cwd=remote.work).stdout
        assert "protocol.version" not in config
        assert "transfer.unpacklimit" not in config.lower()
        assert "pack.compression" not in config
