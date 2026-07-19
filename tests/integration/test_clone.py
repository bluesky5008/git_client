"""clone 통합 테스트.

clone은 다른 원격 작업과 두 가지가 다르다:
  - 시작할 때 **저장소가 없다** — 계측을 귀속시킬 대상이 아직 없다
  - 실패·취소 시 **부분 생성물이 남을 수 있다** — git은 자기 실패에서는
    정리하지만 강제 종료당하면 반쯤 만든 디렉터리를 남긴다(실측)

두 번째가 특히 중요하다. 남겨두면 사용자는 정체 모를 폴더를 마주하고,
같은 자리에 다시 clone하려 하면 "이미 존재한다"로 막힌다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.application.remote_workers import CloneWorker
from gitclient.domain.errors import EngineError
from gitclient.domain.instrumentation import OperationKind
from gitclient.infrastructure.remote_engine import RemoteEngine
from tests.integration.remote_harness import RemoteFixture, git

TIMEOUT = 60


@pytest.fixture
def remote(tmp_path: Path) -> RemoteFixture:
    return RemoteFixture(tmp_path / "src").build(commits=4, payload_kb=4)


class TestCloneBasics:
    def test_clone_creates_a_working_repository(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        destination = tmp_path / "cloned"

        RemoteEngine.clone(remote.origin_uri, destination, timeout_s=TIMEOUT)

        assert (destination / ".git").exists()
        assert git("rev-parse", "HEAD", cwd=destination).stdout.strip() == (
            remote.origin_head()
        )

    def test_object_count_is_always_measured(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        """작은 clone에서도 최소한 객체 수는 알아야 한다.

        git은 전송이 작으면 크기를 아예 안 붙인다(`Receiving objects: 100%
        (3/3), done.`). 크기를 필수로 파싱하면 그 경우 **객체 수까지 통째로
        놓친다** — 알 수 있는 것마저 버리는 셈이다.
        """
        stats = RemoteEngine.clone(
            remote.origin_uri, tmp_path / "cloned", timeout_s=TIMEOUT
        )

        assert stats.kind is OperationKind.CLONE
        assert stats.received_objects, "객체 수를 놓쳤다"
        assert stats.protocol_version == 2

    def test_bytes_are_measured_when_git_reports_them(
        self, tmp_path: Path
    ) -> None:
        """충분히 큰 전송에서는 바이트도 측정돼야 한다.

        clone은 대개 가장 큰 단일 전송이다 — 여기서 비면 누적 집계의
        가장 큰 항목이 사라진다.

        내용을 **압축되지 않게** 만드는 것이 요점이다. 반복 문자열로 채우면
        900KB짜리 파일도 몇 KB로 줄어들어 git이 크기를 붙이지 않는다 —
        페이로드 크기가 아니라 실제 전송량이 기준이다.
        """
        import random

        origin = tmp_path / "incompressible.git"
        seed = tmp_path / "incompressible-seed"
        git("init", "--bare", "-b", "main", str(origin))
        git("init", "-b", "main", str(seed))
        rng = random.Random(5)
        for index in range(3):
            (seed / f"blob{index}.bin").write_bytes(
                bytes(rng.randrange(256) for _ in range(400_000))
            )
            git("add", "-A", cwd=seed)
            git("commit", "--quiet", "-m", f"blob {index}", cwd=seed)
        git("push", "--quiet", str(origin), "main", cwd=seed)

        stats = RemoteEngine.clone(
            origin.resolve().as_uri(), tmp_path / "big-clone", timeout_s=TIMEOUT
        )

        assert stats.received_bytes is not None and stats.received_bytes > 0

    def test_existing_empty_directory_is_allowed(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        destination = tmp_path / "prepared"
        destination.mkdir()

        RemoteEngine.clone(remote.origin_uri, destination, timeout_s=TIMEOUT)

        assert (destination / ".git").exists()

    def test_nonempty_destination_is_refused_without_touching_it(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        """사용자 파일이 있는 폴더를 덮어쓰면 안 된다."""
        destination = tmp_path / "occupied"
        destination.mkdir()
        mine = destination / "mine.txt"
        mine.write_text("내 파일\n", encoding="utf-8")

        with pytest.raises(EngineError):
            RemoteEngine.clone(remote.origin_uri, destination, timeout_s=TIMEOUT)

        assert mine.read_text(encoding="utf-8") == "내 파일\n"

    def test_missing_remote_is_actionable(self, tmp_path: Path) -> None:
        with pytest.raises(EngineError) as excinfo:
            RemoteEngine.clone(
                str(tmp_path / "nowhere.git"), tmp_path / "dst", timeout_s=TIMEOUT
            )
        assert excinfo.value.detail


class TestCloneOptions:
    """필터와 깊이는 초기 전송량을 줄이지만 제약이 따른다 (ADR-6)."""

    def test_blob_filter_marks_the_repository_as_partial(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        destination = tmp_path / "partial"

        RemoteEngine.clone(
            remote.origin_uri, destination,
            filter_spec="blob:none", timeout_s=TIMEOUT,
        )

        promisor = git(
            "config", "--get", "remote.origin.promisor", cwd=destination
        ).stdout.strip()
        assert promisor == "true", "부분 복제 표시가 없으면 UI가 제약을 알릴 수 없다"

    def test_depth_marks_the_repository_as_shallow(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        destination = tmp_path / "shallow"

        RemoteEngine.clone(
            remote.origin_uri, destination, depth=1, timeout_s=TIMEOUT
        )

        shallow = git(
            "rev-parse", "--is-shallow-repository", cwd=destination
        ).stdout.strip()
        assert shallow == "true"

    def test_depth_transfers_fewer_objects(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        full = RemoteEngine.clone(
            remote.origin_uri, tmp_path / "full", timeout_s=TIMEOUT
        )
        shallow = RemoteEngine.clone(
            remote.origin_uri, tmp_path / "one", depth=1, timeout_s=TIMEOUT
        )

        assert shallow.received_objects < full.received_objects


class TestPartialCloneIsCleanedUp:
    """실패·취소 뒤에 반쯤 만들어진 복제본을 남기면 안 된다."""

    def test_failed_clone_leaves_nothing_behind(self, tmp_path: Path) -> None:
        destination = tmp_path / "will-fail"

        worker = CloneWorker(str(tmp_path / "nowhere.git"), destination)
        worker.run()

        assert not destination.exists(), "실패한 복제본이 남았다"

    def test_failure_preserves_a_preexisting_empty_directory(
        self, tmp_path: Path
    ) -> None:
        """사용자가 미리 만들어 둔 폴더까지 지우면 그것대로 놀랄 일이다."""
        destination = tmp_path / "prepared"
        destination.mkdir()

        worker = CloneWorker(str(tmp_path / "nowhere.git"), destination)
        worker.run()

        assert destination.exists() and destination.is_dir()
        assert list(destination.iterdir()) == [], "내용은 비워져 있어야 한다"

    def test_cancelled_clone_leaves_nothing_behind(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        """취소는 강제 종료다 — git이 스스로 정리하지 못하는 유일한 경로다."""
        destination = tmp_path / "cancelled"
        worker = CloneWorker(remote.origin_uri, destination)
        worker.cancel()  # 시작 전에 취소 — 엔진 생성 전 경로

        worker.run()

        assert not destination.exists()

    def test_successful_clone_is_kept(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        """정리 로직이 성공한 복제본까지 지우면 안 된다."""
        destination = tmp_path / "kept"
        worker = CloneWorker(remote.origin_uri, destination)

        worker.run()

        assert (destination / ".git").exists()
        assert worker._succeeded is True


class TestCloneArgumentInjection:
    def test_dash_leading_url_is_not_an_option(self, tmp_path: Path) -> None:
        """clone에도 `--` 방어가 필요하다 — URL은 사용자가 붙여넣는 값이다."""
        marker = tmp_path / "PWNED.txt"
        script = tmp_path / "payload.bat"
        script.write_text('@echo pwned > "' + str(marker) + '"\n@exit /b 1\n')
        hostile = "--upload-pack=" + str(script).replace("\\", "/")

        with pytest.raises(EngineError):
            RemoteEngine.clone(hostile, tmp_path / "dst", timeout_s=TIMEOUT)

        assert not marker.exists(), "URL이 옵션으로 해석돼 명령이 실행됐다"
