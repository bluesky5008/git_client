"""복제 정리 로직의 안전성 — 적대적 리뷰에서 확정된 결함들.

이 파일이 따로 있는 이유는 여기서 나온 결함의 성격 때문이다. 부분 생성물을
**치우려던 코드가 사용자 데이터를 파괴하고 있었다.** 안전장치가 스스로
위험이 된 경우라, 회귀 테스트를 따로 모아 눈에 띄게 둔다.

원래 주석의 근거가 정확히 반대였다: "clone은 비어 있을 때만 허용되므로
비우면 원래 상태와 같다" — git이 비어있지 않은 대상을 **거부하고 손도 대지
않기 때문에**, 그 경우 안의 내용은 전부 사용자 것이다.

규칙은 하나다: **우리가 넣은 것만 치운다.**
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gitclient.application.remote_workers import CloneWorker
from tests.integration.remote_harness import RemoteFixture

TIMEOUT = 60


@pytest.fixture
def remote(tmp_path: Path) -> RemoteFixture:
    return RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=2)


def make_user_files(root: Path) -> dict[Path, str]:
    """지워지면 안 되는 사용자 파일들."""
    (root / "photos").mkdir(parents=True, exist_ok=True)
    files = {
        root / "thesis.docx": "논문 원고\n",
        root / "photos" / "wedding.txt": "결혼사진\n",
    }
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
    return files


class TestNeverDeletesUserData:
    """git이 거부한 대상의 내용은 전부 사용자 것이다."""

    def test_failed_clone_into_nonempty_folder_keeps_everything(
        self, tmp_path: Path
    ) -> None:
        """복제를 **시도만** 하고 실패했는데 기존 폴더가 비워지면 안 된다.

        `shutil.rmtree`는 휴지통을 경유하지 않는다 — 되돌릴 방법이 없다.
        """
        destination = tmp_path / "MyProject"
        destination.mkdir()
        files = make_user_files(destination)

        worker = CloneWorker(str(tmp_path / "nowhere.git"), destination)
        worker.run()

        for path, content in files.items():
            assert path.exists(), f"사용자 파일이 사라졌다: {path.name}"
            assert path.read_text(encoding="utf-8") == content

    def test_successful_looking_path_into_nonempty_folder_keeps_everything(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        """진짜 원격이라도 대상이 차 있으면 git이 거부한다 — 그때도 보존."""
        destination = tmp_path / "MyProject"
        destination.mkdir()
        files = make_user_files(destination)

        worker = CloneWorker(remote.origin_uri, destination)
        worker.run()

        for path in files:
            assert path.exists(), f"사용자 파일이 사라졌다: {path.name}"

    @pytest.mark.skipif(os.name != "nt", reason="정션은 Windows 전용 재현")
    def test_junction_destination_is_never_followed(self, tmp_path: Path) -> None:
        """대상이 링크면 삭제가 **대상 경로 밖**에서 일어난다.

        사용자는 무엇이 지워졌는지 짐작조차 못 한다.
        """
        real = tmp_path / "RealDocuments"
        real.mkdir()
        files = make_user_files(real)
        link = tmp_path / "shortcut"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(real)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:  # pragma: no cover - 환경 제약
            pytest.skip("정션을 만들 수 없는 환경")

        worker = CloneWorker(str(tmp_path / "nowhere.git"), link)
        worker.run()

        for path, content in files.items():
            assert path.exists(), f"링크 밖의 파일이 지워졌다: {path.name}"
            assert path.read_text(encoding="utf-8") == content

    def test_cancel_before_start_does_not_touch_the_destination(
        self, tmp_path: Path
    ) -> None:
        """git을 실행하지도 않았는데 폴더를 비우면 안 된다."""
        destination = tmp_path / "MyProject"
        destination.mkdir()
        files = make_user_files(destination)

        worker = CloneWorker(str(tmp_path / "nowhere.git"), destination)
        worker.cancel()
        worker.run()

        for path in files:
            assert path.exists()


class TestStillCleansOurOwnMess:
    """안전해지느라 정리를 포기하면 안 된다 — 우리가 만든 것은 여전히 치운다."""

    def test_failed_clone_removes_a_directory_we_created(
        self, tmp_path: Path
    ) -> None:
        destination = tmp_path / "brand-new"

        worker = CloneWorker(str(tmp_path / "nowhere.git"), destination)
        worker.run()

        assert not destination.exists()

    def test_failed_clone_empties_a_preexisting_empty_directory(
        self, tmp_path: Path
    ) -> None:
        destination = tmp_path / "prepared"
        destination.mkdir()

        worker = CloneWorker(str(tmp_path / "nowhere.git"), destination)
        worker.run()

        # 폴더 자체는 지워도 무방하다 — 비어 있었으므로 잃을 것이 없다.
        assert not destination.exists() or not any(destination.iterdir())

    def test_successful_clone_is_kept(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        destination = tmp_path / "kept"

        worker = CloneWorker(remote.origin_uri, destination)
        worker.run()

        assert (destination / ".git").exists()


class TestCompleteRepositoryIsNotDiscarded:
    def test_checkout_failure_keeps_the_received_objects(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        """객체를 다 받고 체크아웃만 실패하면 지우면 안 된다.

        이미 치른 전송을 통째로 버리는 것이라 목적함수에 정면으로 어긋난다.
        여기서는 실패를 흉내내 정리 판단만 검증한다.
        """
        destination = tmp_path / "checkout-failed"
        worker = CloneWorker(remote.origin_uri, destination)
        worker.run()
        assert (destination / ".git" / "HEAD").exists()

        # 성공한 복제를 "실패"로 둔갑시켜 정리 판단을 태운다
        worker._succeeded = False
        worker.run()

        assert (destination / ".git").exists(), "다 받은 저장소를 버렸다"


class TestCancelStopsTheProcess:
    def test_cancel_reaches_the_clone_engine(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        """취소가 실제 clone 엔진에 닿아야 프로세스가 죽는다.

        워커가 붙잡은 엔진과 clone을 돌리는 엔진이 다르면, 취소해도 git이
        계속 돌아 스레드풀 슬롯이 묶인다.
        """
        destination = tmp_path / "cancelled"
        worker = CloneWorker(remote.origin_uri, destination)

        aborted: list[bool] = []
        original = worker._operate

        def spy(engine):  # noqa: ANN001, ANN202
            result = original(engine)
            aborted.append(worker._clone_engine is not None)
            return result

        worker._operate = spy  # type: ignore[method-assign]
        worker.run()

        assert aborted == [True], "clone 엔진이 워커에 등록되지 않았다"
