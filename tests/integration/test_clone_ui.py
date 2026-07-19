"""복제 UI 통합 테스트.

복제는 다른 원격 작업과 달리 **저장소가 열려 있지 않은 상태에서** 쓰인다.
그래서 첫 화면부터 눌릴 수 있어야 하고, 끝나면 방금 받은 저장소를 열어
줘야 한다 — 어디에 받았는지 사용자가 기억하게 만들면 안 된다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog

import gitclient.ui.main_window as module
from gitclient.ui.clone_dialog import CloneDialog, CloneRequest, _repository_name
from gitclient.ui.main_window import MainWindow
from tests.integration.remote_harness import RemoteFixture, git

TIMEOUT = 60_000


@pytest.fixture
def remote(tmp_path: Path) -> RemoteFixture:
    return RemoteFixture(tmp_path / "src").build(commits=4, payload_kb=2)


@pytest.fixture
def window(qtbot):  # noqa: ANN001, ANN201
    w = MainWindow()
    qtbot.addWidget(w)
    errors: list = []
    w._report = errors.append
    w.reported_errors = errors
    return w


def answer_with(monkeypatch, request: CloneRequest | None) -> None:
    """다이얼로그를 띄우지 않고 정해진 요청을 돌려주게 만든다."""

    class Fake:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            pass

        def exec(self) -> int:
            return (
                QDialog.DialogCode.Accepted
                if request is not None
                else QDialog.DialogCode.Rejected
            )

        def request(self) -> CloneRequest:
            assert request is not None
            return request

    monkeypatch.setattr(module, "CloneDialog", Fake)


class TestCloneAvailability:
    def test_enabled_without_an_open_repository(self, window) -> None:  # noqa: ANN001
        """저장소가 없을 때가 복제가 가장 필요한 순간이다."""
        assert window._clone_action.isEnabled()
        assert not window._fetch_action.isEnabled()

    def test_disabled_while_another_remote_op_runs(
        self, window, qtbot, remote: RemoteFixture, monkeypatch  # noqa: ANN001
    ) -> None:
        window.open_repository(str(remote.work))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
        remote.add_and_publish(1, payload_kb=32)

        window._on_fetch()

        assert not window._clone_action.isEnabled()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)


class TestCloneFlow:
    def test_clone_opens_the_new_repository(
        self, window, qtbot, remote: RemoteFixture, tmp_path: Path, monkeypatch  # noqa: ANN001
    ) -> None:
        destination = tmp_path / "fresh"
        answer_with(
            monkeypatch, CloneRequest(url=remote.origin_uri, destination=destination)
        )

        window._on_clone()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        assert window.reported_errors == [], [
            e.message for e in window.reported_errors
        ]
        assert window._repo_path == str(destination)
        assert window._commit_model.rowCount() > 0

    def test_cancelling_the_dialog_does_nothing(
        self, window, qtbot, monkeypatch  # noqa: ANN001
    ) -> None:
        answer_with(monkeypatch, None)

        window._on_clone()

        assert window._fetch_worker is None
        assert window.reported_errors == []

    def test_failed_clone_reports_and_leaves_nothing(
        self, window, qtbot, tmp_path: Path, monkeypatch  # noqa: ANN001
    ) -> None:
        destination = tmp_path / "doomed"
        answer_with(
            monkeypatch,
            CloneRequest(url=str(tmp_path / "nowhere.git"), destination=destination),
        )

        window._on_clone()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)

        assert window.reported_errors, "실패했는데 조용히 넘어갔다"
        assert not destination.exists(), "반쯤 만들어진 복제본이 남았다"

    def test_shallow_clone_warns_about_the_limitation(
        self, window, qtbot, remote: RemoteFixture, tmp_path: Path, monkeypatch  # noqa: ANN001
    ) -> None:
        """고른 시점과 막히는 시점이 멀다 — 받은 직후에 한 번 더 말해 준다."""
        destination = tmp_path / "shallow"
        answer_with(
            monkeypatch,
            CloneRequest(
                url=remote.origin_uri, destination=destination, depth=1
            ),
        )

        window._on_clone()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        assert "히스토리가 잘려" in window.statusBar().currentMessage()

    def test_partial_clone_warns_about_the_limitation(
        self, window, qtbot, remote: RemoteFixture, tmp_path: Path, monkeypatch  # noqa: ANN001
    ) -> None:
        destination = tmp_path / "partial"
        answer_with(
            monkeypatch,
            CloneRequest(
                url=remote.origin_uri,
                destination=destination,
                filter_spec="blob:none",
            ),
        )

        window._on_clone()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        assert "지연 수신" in window.statusBar().currentMessage()

    def test_full_clone_does_not_warn(
        self, window, qtbot, remote: RemoteFixture, tmp_path: Path, monkeypatch  # noqa: ANN001
    ) -> None:
        """제약이 없으면 경고하지 않는다 — 늘 뜨는 경고는 읽히지 않는다."""
        answer_with(
            monkeypatch,
            CloneRequest(url=remote.origin_uri, destination=tmp_path / "full"),
        )

        window._on_clone()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        message = window.statusBar().currentMessage()
        assert "히스토리가 잘려" not in message
        assert "지연 수신" not in message


class TestCloneDialog:
    def test_ok_requires_url_and_destination(self, qtbot) -> None:  # noqa: ANN001
        from PySide6.QtWidgets import QDialogButtonBox

        dialog = CloneDialog()
        qtbot.addWidget(dialog)
        ok = dialog.findChild(QDialogButtonBox).button(
            QDialogButtonBox.StandardButton.Ok
        )

        assert not ok.isEnabled()
        dialog._url.setText("https://example.com/o/r.git")
        assert ok.isEnabled(), "주소를 넣으면 대상이 자동으로 채워져야 한다"

    def test_destination_is_guessed_from_the_url(self, qtbot) -> None:  # noqa: ANN001
        dialog = CloneDialog()
        qtbot.addWidget(dialog)

        dialog._url.setText("https://github.com/org/my-repo.git")

        assert dialog._destination.text().endswith("my-repo")

    def test_each_mode_states_what_it_costs(self, qtbot) -> None:  # noqa: ANN001
        """선택지마다 잃는 것이 적혀 있어야 한다 — 이득만 보여주면 정직하지 않다."""
        dialog = CloneDialog()
        qtbot.addWidget(dialog)

        for index in range(dialog._mode.count()):
            dialog._mode.setCurrentIndex(index)
            assert dialog._cost.text().strip(), f"{index}번 선택지에 설명이 없다"

    def test_default_is_full_clone(self, qtbot) -> None:  # noqa: ANN001
        """기본값은 전체 복제다 (ADR-6) — 누적 전송량이 목적함수이므로."""
        dialog = CloneDialog()
        qtbot.addWidget(dialog)
        dialog._url.setText("https://example.com/o/r.git")

        request = dialog.request()

        assert request.filter_spec is None
        assert request.depth is None


class TestRepositoryNameGuess:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/org/repo.git", "repo"),
            ("https://github.com/org/repo", "repo"),
            ("https://github.com/org/repo/", "repo"),
            ("git@github.com:org/repo.git", "repo"),
            ("file:///c:/src/thing.git", "thing"),
            ("", None),
        ],
    )
    def test_guesses(self, url: str, expected: str | None) -> None:
        assert _repository_name(url) == expected
