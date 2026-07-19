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


class TestPushUI:
    def test_push_sends_local_commits(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        remote.commit_locally(2, payload_kb=4)

        window._on_push()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)

        assert window.reported_errors == [], [
            e.message for e in window.reported_errors
        ]
        assert remote.origin_branch_head() == remote.work_head()
        assert "올리기 완료" in window._transfer_label.text()

    def test_push_reports_sent_bytes(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """보낸 바이트를 보여준다 — 목적함수는 방향을 가리지 않는다."""
        remote.commit_locally(1, payload_kb=8)

        window._on_push()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)

        assert "전송" in window._transfer_label.text()
        assert "측정 실패" not in window._transfer_label.text()

    def test_nothing_to_push_says_so(self, window, qtbot) -> None:  # noqa: ANN001
        window._on_push()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        assert "올릴 것이 없습니다" in window._transfer_label.text()

    def test_rejected_push_is_reported_with_action(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """가장 흔한 push 실패 — 조치가 함께 나와야 한다."""
        remote.diverge()

        window._on_push()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)

        assert window.reported_errors, "거부됐는데 조용히 넘어갔다"
        assert window.reported_errors[-1].action is not None

    def test_disabled_while_another_remote_op_runs(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """원격 작업 슬롯은 하나다 — fetch 중에 push가 켜져 있으면 안 된다."""
        remote.add_and_publish(1, payload_kb=32)
        window._on_fetch()

        assert not window._push_action.isEnabled()
        assert not window._pull_action.isEnabled()

        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
        assert window._push_action.isEnabled()


class TestPullUI:
    def test_pull_fast_forwards_working_tree(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """pull은 그래프만이 아니라 워킹 트리도 옮겨야 한다."""
        remote.add_and_publish(2, payload_kb=2)
        expected = remote.origin_head()

        window._on_pull()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(
            lambda: remote.work_head() == expected and not window._loading,
            timeout=TIMEOUT,
        )

        assert window.reported_errors == [], [
            e.message for e in window.reported_errors
        ]
        assert git("status", "--porcelain", cwd=remote.work).stdout.strip() == ""

    def test_pull_when_up_to_date_is_quiet(
        self, window, qtbot  # noqa: ANN001
    ) -> None:
        window._on_pull()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)

        assert window.reported_errors == []
        assert "이미 최신" in window._transfer_label.text()

    def test_diverged_pull_keeps_the_fetched_objects_and_explains(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """병합이 필요하면 시작하지 않는다 — 앱 안에서 끝낼 수 없기 때문이다.

        다만 받아온 것은 버리지 않는다. 트래픽을 이미 썼으므로 다시 받게
        만들면 안 된다.
        """
        remote.diverge()
        before_local = remote.work_head()

        window._on_pull()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: bool(window.reported_errors), timeout=TIMEOUT)

        error = window.reported_errors[-1]
        assert error.action is not None
        assert remote.work_head() == before_local, "합치지 않았는데 HEAD가 움직였다"
        # 받아온 커밋은 원격 추적 참조에 남아 있어야 한다
        tracked = git(
            "rev-parse", "refs/remotes/origin/main", cwd=remote.work
        ).stdout.strip()
        assert tracked == remote.origin_head(), "받아온 것을 버렸다"

    def test_pull_with_uncommitted_changes_preserves_them(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """커밋하지 않은 작업을 조용히 덮어쓰면 안 된다."""
        target = remote.work / "f0.txt"
        mine = "내가 작업하던 내용\n"

        (remote.seed / "f0.txt").write_text("원격이 바꾼 내용\n", encoding="utf-8")
        git("add", "-A", cwd=remote.seed)
        git("commit", "--quiet", "-m", "원격 수정", cwd=remote.seed)
        remote.publish()
        target.write_text(mine, encoding="utf-8")

        window._on_pull()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: bool(window.reported_errors), timeout=TIMEOUT)

        assert target.read_text(encoding="utf-8") == mine, "내 작업이 사라졌다"
        assert window.reported_errors[-1].action is not None


class TestPullRefreshesTheView:
    """합칠 것이 없어도 화면은 갱신해야 한다.

    fetch는 이미 원격 추적 참조를 갱신했다. 합치기 여부와 화면 갱신 여부는
    별개인데, 이 둘을 묶으면 받아온 브랜치가 화면에 끝내 나타나지 않는다.
    """

    def test_up_to_date_pull_still_shows_new_remote_branches(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        remote.create_remote_branch("newcomer")

        window._on_pull()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        labels = [
            window._ref_list.item(row).text()
            for row in range(window._ref_list.count())
        ]
        assert any("newcomer" in label for label in labels), labels

    def test_diverged_pull_still_shows_new_remote_branches(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """병합이 필요해 멈추더라도 받아온 것은 보여줘야 한다."""
        remote.diverge()
        remote.create_remote_branch("newcomer")

        window._on_pull()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        labels = [
            window._ref_list.item(row).text()
            for row in range(window._ref_list.count())
        ]
        assert any("newcomer" in label for label in labels), labels


class TestUpstreamAwareness:
    def test_detached_head_does_not_move_on_pull(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """분리된 HEAD는 upstream이 없다 — 조용히 원격 기본 브랜치로 끌려가면 안 된다.

        규약으로 upstream을 조합하면 `refs/remotes/origin/HEAD`가 만들어지는데,
        그 참조는 모든 clone에 존재하고 원격 기본 브랜치를 가리킨다. bisect
        중인 사용자의 HEAD가 사라진다.
        """
        git("checkout", "--quiet", "--detach", "HEAD~1", cwd=remote.work)
        window.open_repository(str(remote.work))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
        before = remote.work_head()
        remote.add_and_publish(1)

        window._on_pull()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
        qtbot.wait(300)

        assert remote.work_head() == before, "분리된 HEAD가 움직였다"

    def test_push_does_not_overwrite_configured_upstream(
        self, window, qtbot, remote: RemoteFixture, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """설정된 추적 대상을 조용히 덮어쓰면 안 된다.

        "ahead/behind를 계산할 수 없다"로 미설정을 판정하면, 아직 fetch하지
        않아 비교만 실패한 경우까지 미설정으로 읽는다.
        """
        canonical = RemoteFixture(tmp_path / "canon").build(commits=2, payload_kb=1)
        git("remote", "add", "upstream", canonical.origin_uri, cwd=remote.work)
        git("fetch", "--quiet", "upstream", cwd=remote.work)
        git("branch", "--set-upstream-to=upstream/main", "main", cwd=remote.work)
        window.open_repository(str(remote.work))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        remote.commit_locally(1)
        window._on_push()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)

        configured = git(
            "config", "--get", "branch.main.remote", cwd=remote.work
        ).stdout.strip()
        assert configured == "upstream", "사용자가 지정한 추적 대상이 바뀌었다"


class TestDivergenceIndicator:
    def test_shows_ahead_and_behind(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """무엇이 필요한지 눌러보기 전에 알 수 있어야 한다."""
        remote.diverge()
        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        message = window.statusBar().currentMessage()
        assert "↑1" in message and "↓1" in message, message

    def test_in_sync_says_so(self, window, qtbot) -> None:  # noqa: ANN001
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
        assert "원격과 동기화됨" in window.statusBar().currentMessage()


class TestFormatBytes:
    @pytest.mark.parametrize(
        ("count", "expected"),
        [(0, "0 B"), (512, "512 B"), (1024, "1.00 KiB"), (1024 * 1024, "1.00 MiB")],
    )
    def test_formats(self, count: int, expected: str) -> None:
        assert _format_bytes(count) == expected
