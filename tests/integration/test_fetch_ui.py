"""fetch UI 통합 테스트 — 실제 원격에서 가져와 그래프까지 반영되는지."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.errors import GitClientError
from gitclient.ui.main_window import MainWindow, _format_bytes
from tests.integration.remote_harness import RemoteFixture, git

TIMEOUT = 30_000


@pytest.fixture
def remote(tmp_path: Path) -> RemoteFixture:
    return RemoteFixture(tmp_path).build(commits=4, payload_kb=2)


@pytest.fixture
def window(qtbot, remote: RemoteFixture):  # noqa: ANN001, ANN201
    w = MainWindow()
    qtbot.addWidget(w)
    errors: list[GitClientError] = []
    w._report = errors.append
    w.reported_errors = errors

    w.open_repository(str(remote.work))
    qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
    return w


class TestFetchAvailability:
    def test_enabled_when_remote_exists(self, window) -> None:  # noqa: ANN001
        assert window._fetch_action.isEnabled()

    def test_disabled_without_remote(self, qtbot, tmp_path: Path) -> None:  # noqa: ANN001
        import pygit2

        solo = tmp_path / "solo"
        repo = pygit2.init_repository(str(solo), initial_head="main")
        repo.config["user.name"] = "t"
        repo.config["user.email"] = "t@e.com"
        (solo / "f.txt").write_text("x", encoding="utf-8")
        repo.index.add_all()
        repo.index.write()
        sig = pygit2.Signature("t", "t@e.com", 1700000000, 540)
        repo.create_commit("HEAD", sig, sig, "init", repo.index.write_tree(), [])

        w = MainWindow()
        qtbot.addWidget(w)
        w._report = lambda e: None
        w.open_repository(str(solo))
        qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)

        assert not w._fetch_action.isEnabled()


class TestFetchCycle:
    def test_fetch_brings_commits_into_graph(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        before = window._commit_model.rowCount()
        remote.add_and_publish(3, payload_kb=2)

        window._on_fetch()

        qtbot.waitUntil(
            lambda: window._commit_model.rowCount() > before, timeout=TIMEOUT
        )
        assert window.reported_errors == []

    def test_status_bar_reports_transfer(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """전송량을 사용자에게 보여준다 — 목적함수가 누적 바이트이므로."""
        remote.add_and_publish(2, payload_kb=4)
        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)

        message = window._transfer_label.text()
        assert "가져오기 완료" in message
        assert "전송" in message

    def test_transfer_report_survives_graph_reload(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """fetch 후 그래프 재로딩이 전송량 표시를 덮으면 안 된다.

        임시 메시지로 띄우면 "커밋을 읽는 중..."에 즉시 가려져 사용자가
        비용을 볼 수 없다 — 목적함수를 보여주는 것이 요점이므로 결함이다.
        """
        remote.add_and_publish(2, payload_kb=4)
        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        assert "전송" in window._transfer_label.text()

    def test_up_to_date_fetch_says_so(self, window, qtbot) -> None:  # noqa: ANN001
        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        assert "최신" in window._transfer_label.text()

    def test_progress_message_is_cleared_when_nothing_changed(
        self, window, qtbot  # noqa: ANN001
    ) -> None:
        """변경이 없으면 재로딩도 없다 — 그 경로에서 임시 메시지가 남으면
        끝난 작업이 계속 진행 중인 것처럼 보인다."""
        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        assert "가져오는 중" not in window.statusBar().currentMessage()

    def test_action_is_disabled_while_running(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        remote.add_and_publish(1, payload_kb=1)
        window._on_fetch()
        assert not window._fetch_action.isEnabled()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        assert window._fetch_action.isEnabled()

    def test_double_click_does_not_start_two_fetches(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        remote.add_and_publish(1, payload_kb=1)
        window._on_fetch()
        first = window._fetch_worker
        window._on_fetch()  # 즉시 재클릭
        assert window._fetch_worker is first

        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        assert window.reported_errors == []


class TestRefOnlyChanges:
    """객체 전송이 없어도 저장소가 바뀌는 fetch — 화면에 반영돼야 한다."""

    def test_new_branch_without_objects_updates_refs(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """이미 가진 커밋을 가리키는 새 브랜치.

        "받은 객체 수"로 변경을 판단하면 팩이 오지 않아 "이미 최신"이 뜨고,
        .git에 실재하는 origin/side가 참조 목록에 끝내 나타나지 않는다.
        """
        remote.create_remote_branch("side")

        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        assert "이미 최신" not in window._transfer_label.text()
        labels = [
            window._ref_list.item(row).text()
            for row in range(window._ref_list.count())
        ]
        assert any("side" in label for label in labels), labels

    def test_zero_bytes_is_not_reported_as_measurement_failure(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """0바이트는 측정된 사실이다 — '측정 실패'로 표시하면 안 된다."""
        remote.create_remote_branch("side")
        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)

        assert "측정 실패" not in window._transfer_label.text()


class TestRemoteSelection:
    def test_non_origin_remote_is_used(self, qtbot, tmp_path: Path) -> None:  # noqa: ANN001
        """원격 이름이 origin이 아니어도 동작해야 한다.

        버튼 활성은 "원격이 하나라도 있으면"인데 실행은 항상 origin이었다.
        upstream 하나만 있는 저장소에서 버튼은 켜지고, 누르면 "origin을 찾을
        수 없다"가 떠서 원격 주소를 의심하게 만든다 — 멀쩡한데도.
        """
        fixture = RemoteFixture(tmp_path / "up").build(commits=3, payload_kb=1)
        git("remote", "rename", "origin", "upstream", cwd=fixture.work)
        fixture.add_and_publish(2, payload_kb=1)

        w = MainWindow()
        qtbot.addWidget(w)
        errors: list[GitClientError] = []
        w._report = errors.append
        w.open_repository(str(fixture.work))
        qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)

        assert w._fetch_action.isEnabled()
        w._on_fetch()
        qtbot.waitUntil(lambda: w._fetch_worker is None, timeout=TIMEOUT)

        assert errors == [], [e.message for e in errors]
        assert "가져오기 완료" in w._transfer_label.text()


class TestWorkerLifetime:
    def test_repo_switch_releases_fetch_worker(
        self, window, qtbot, remote: RemoteFixture, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """fetch 중 저장소를 바꾸면 이전 결과가 새 저장소 UI로 새면 안 된다.

        놓아주지 않으면 정체 가드(`worker is self._fetch_worker`)가 성립할 수
        없다 — 여전히 같은 객체이므로 통과해버려, A의 전송량이 B의 상태바에
        찍히고 B가 통째로 재로딩된다.
        """
        other = RemoteFixture(tmp_path / "other").build(commits=2, payload_kb=1)
        remote.add_and_publish(2, payload_kb=64)

        window._on_fetch()
        assert window._fetch_worker is not None
        window.open_repository(str(other.work))

        assert window._fetch_worker is None, "저장소가 바뀌면 워커를 놓아야 한다"
        assert window._fetch_action.isEnabled(), "죽은 버튼이 되면 안 된다"

        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
        qtbot.wait(600)  # 이전 워커가 끝날 시간을 준다
        assert window._transfer_label.text() == "", "이전 저장소의 결과가 샜다"

    def test_close_cancels_running_fetch(
        self, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """창을 닫으면 fetch를 취소한다.

        취소하지 않으면 전역 스레드풀 슬롯이 원격 응답까지 붙잡히고, 창이
        사라진 뒤에도 프로세스가 남는다.
        """
        w = MainWindow()
        qtbot.addWidget(w)
        w._report = lambda _e: None
        w.open_repository(str(remote.work))
        qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)

        remote.add_and_publish(2, payload_kb=64)
        w._on_fetch()
        worker = w._fetch_worker
        assert worker is not None

        w.close()

        assert w._fetch_worker is None
        assert worker._cancelled is True


class TestFormatBytes:
    @pytest.mark.parametrize(
        ("count", "expected"),
        [(0, "0 B"), (512, "512 B"), (1024, "1.00 KiB"), (1024 * 1024, "1.00 MiB")],
    )
    def test_formats(self, count: int, expected: str) -> None:
        assert _format_bytes(count) == expected
