"""인증 통합 테스트 — 진짜 Basic 인증 HTTP 원격을 상대로.

이 앱은 자격증명을 직접 저장하지 않는다(ADR-3). 프롬프트는 우리가 띄우고,
보관은 git의 credential helper에 위임한다. 여기서 확인하는 것:

  1. 자격증명이 없으면 **매달리지 않고** "물어보면 해결되는 실패"로 끝난다
  2. 공급하면 실제로 통과하고, 통과 뒤에도 전송량 계측이 살아 있다
  3. 틀린 자격증명은 "다시 물어봐야 하는 실패"와 구분된다
  4. 저장은 helper에 위임된다
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.errors import AuthenticationRequired, EngineError
from gitclient.infrastructure.askpass import Credentials
from gitclient.infrastructure.remote_engine import RemoteEngine
from tests.integration.auth_harness import (
    PASSWORD,
    USERNAME,
    AuthenticatedRemote,
)
from tests.integration.remote_harness import AUTHOR_ENV, git

TIMEOUT = 60


@pytest.fixture
def remote(tmp_path: Path):  # noqa: ANN201
    server = AuthenticatedRemote(tmp_path / "srv").start(commits=3)
    yield server
    server.stop()


@pytest.fixture
def work(remote: AuthenticatedRemote, tmp_path: Path) -> Path:
    return remote.clone_anonymously(tmp_path / "work")


def good() -> Credentials:
    return Credentials(username=USERNAME, password=PASSWORD)


class TestWithoutCredentials:
    def test_fetch_fails_fast_instead_of_hanging(self, work: Path) -> None:
        """매달리면 워커 슬롯이 잠긴다 — 실패로 끝나야 한다."""
        with pytest.raises(AuthenticationRequired) as excinfo:
            RemoteEngine(work).fetch(timeout_s=TIMEOUT)

        assert excinfo.value.rejected is False, "아직 물어보지도 않았다"
        assert excinfo.value.action is not None

    def test_error_carries_the_url_for_the_dialog(self, work: Path) -> None:
        """다이얼로그가 '무엇에 대한 로그인인가'를 보여줄 수 있어야 한다."""
        with pytest.raises(AuthenticationRequired) as excinfo:
            RemoteEngine(work).fetch(timeout_s=TIMEOUT)

        assert excinfo.value.url is not None
        assert "127.0.0.1" in excinfo.value.url

    def test_push_also_reports_authentication(self, work: Path) -> None:
        (work / "local.txt").write_text("local\n", encoding="utf-8")
        git("add", "-A", cwd=work)
        git(*AUTHOR_ENV, "commit", "--quiet", "-m", "로컬", cwd=work)

        with pytest.raises(AuthenticationRequired):
            RemoteEngine(work).push(refspecs=["main"], timeout_s=TIMEOUT)


class TestWithCredentials:
    def test_fetch_succeeds(self, remote: AuthenticatedRemote, work: Path) -> None:
        remote.add_remote_commit()

        stats = RemoteEngine(work, credentials=good()).fetch(timeout_s=TIMEOUT)

        assert stats.succeeded
        tracked = git(
            "rev-parse", "refs/remotes/origin/main", cwd=work
        ).stdout.strip()
        assert tracked == remote.origin_head()

    def test_measurement_survives_authentication(
        self, remote: AuthenticatedRemote, work: Path
    ) -> None:
        """인증 뒤에도 전송량이 측정돼야 한다.

        askpass shim이 붙으면서 환경이 달라지므로, 계측이 조용히 비지 않는지
        확인한다 — 목적함수가 누적 전송 바이트인 만큼 여기서 비면 인증
        원격의 비용을 영영 알 수 없다.
        """
        remote.add_remote_commit()

        stats = RemoteEngine(work, credentials=good()).fetch(timeout_s=TIMEOUT)

        assert stats.received_bytes is not None and stats.received_bytes > 0
        assert stats.received_objects
        assert stats.protocol_version == 2, "protocol v2 협상도 유지돼야 한다"

    def test_push_succeeds_and_is_measured(
        self, remote: AuthenticatedRemote, work: Path
    ) -> None:
        (work / "local.txt").write_text("local\n" * 50, encoding="utf-8")
        git("add", "-A", cwd=work)
        git(*AUTHOR_ENV, "commit", "--quiet", "-m", "로컬", cwd=work)

        stats = RemoteEngine(work, credentials=good()).push(
            refspecs=["main"], timeout_s=TIMEOUT
        )

        assert stats.sent_bytes is not None and stats.sent_bytes > 0
        assert remote.origin_head() == git(
            "rev-parse", "HEAD", cwd=work
        ).stdout.strip()


class TestRejectedCredentials:
    def test_wrong_password_is_marked_rejected(self, work: Path) -> None:
        """거부는 "아직 안 물어봄"과 달라야 한다.

        구분하지 않으면 같은 값으로 재시도하는 무한 반복이 된다.
        """
        wrong = Credentials(username=USERNAME, password="nope")

        with pytest.raises(AuthenticationRequired) as excinfo:
            RemoteEngine(work, credentials=wrong).fetch(timeout_s=TIMEOUT)

        assert excinfo.value.rejected is True
        assert excinfo.value.username == USERNAME, "다시 물어볼 때 채워 줄 값"

    def test_rejection_action_mentions_token(self, work: Path) -> None:
        """GitHub 등은 비밀번호가 아니라 토큰을 요구한다 — 흔한 함정이다."""
        wrong = Credentials(username=USERNAME, password="nope")

        with pytest.raises(AuthenticationRequired) as excinfo:
            RemoteEngine(work, credentials=wrong).fetch(timeout_s=TIMEOUT)

        assert "토큰" in (excinfo.value.action or "")


class TestSecrets:
    def test_password_is_not_in_the_shim_file(
        self, remote: AuthenticatedRemote, work: Path, tmp_path: Path
    ) -> None:
        """shim 파일에는 비밀번호가 들어가면 안 된다.

        디스크에 남으면 프로세스가 죽었을 때 그대로 남는다. 값은 환경변수로만
        넘긴다.
        """
        from gitclient.infrastructure.askpass import write_shim

        shim = write_shim(tmp_path)
        content = shim.read_text(encoding="utf-8")

        assert PASSWORD not in content
        assert "GITCLIENT_ASKPASS_PASSWORD" in content

    def test_password_is_not_in_repr(self) -> None:
        """트레이스백·로그에 비밀번호가 실려 나가면 안 된다."""
        assert PASSWORD not in repr(good())
        assert "***" in repr(good())

    def test_password_is_not_in_the_error_detail(self, work: Path) -> None:
        """git 원문을 보존하되 비밀번호가 섞여 나가면 안 된다."""
        wrong = Credentials(username=USERNAME, password="hunter2-secret")

        with pytest.raises(AuthenticationRequired) as excinfo:
            RemoteEngine(work, credentials=wrong).fetch(timeout_s=TIMEOUT)

        assert "hunter2-secret" not in (excinfo.value.detail or "")
        assert "hunter2-secret" not in str(excinfo.value)


class TestStorageDelegation:
    """저장은 helper에 위임한다 — 우리가 직접 쓰지 않는다 (ADR-3)."""

    def _helper(self, tmp_path: Path) -> tuple[str, Path]:
        log = tmp_path / "helper.log"
        script = tmp_path / "helper.bat"
        script.write_text(
            "@echo off\n"
            f'@echo %1 >> "{log}"\n'
            "@exit /b 0\n"
        )
        return str(script).replace("\\", "/"), log

    def test_approve_is_delegated_to_the_helper(
        self, work: Path, tmp_path: Path
    ) -> None:
        helper, log = self._helper(tmp_path)
        git("config", "credential.helper", helper, cwd=work)
        engine = RemoteEngine(work, credentials=good())

        stored = engine.remember_credentials("http://127.0.0.1:1234/repo.git")

        assert stored is True
        assert "store" in log.read_text(encoding="utf-8")

    def test_not_remembering_skips_delegation(
        self, work: Path, tmp_path: Path
    ) -> None:
        helper, log = self._helper(tmp_path)
        git("config", "credential.helper", helper, cwd=work)
        engine = RemoteEngine(
            work,
            credentials=Credentials(USERNAME, PASSWORD, remember=False),
        )

        assert engine.remember_credentials("http://127.0.0.1:1234/x.git") is False
        assert not log.exists(), "저장하지 않기로 했는데 helper를 불렀다"

    def test_without_credentials_nothing_is_stored(self, work: Path) -> None:
        assert RemoteEngine(work).remember_credentials("http://x/y.git") is False


class TestStoredCredentialsAreReused:
    def test_helper_supplied_credentials_avoid_the_prompt(
        self, remote: AuthenticatedRemote, work: Path, tmp_path: Path
    ) -> None:
        """helper가 자격증명을 갖고 있으면 물어보지 않아야 한다.

        이것이 설계의 요점이다 — 사용자가 CLI에서 이미 설정해둔 인증을 그대로
        재사용하고, 우리는 없을 때만 묻는다.
        """
        script = tmp_path / "supplier.bat"
        script.write_text(
            "@echo off\n"
            '@if "%1"=="get" (\n'
            f"@echo username={USERNAME}\n"
            f"@echo password={PASSWORD}\n"
            ")\n"
            "@exit /b 0\n"
        )
        git(
            "config", "credential.helper",
            str(script).replace("\\", "/"), cwd=work,
        )
        remote.add_remote_commit()

        # credentials 없이 — helper가 공급해야 한다
        stats = RemoteEngine(work).fetch(timeout_s=TIMEOUT)

        assert stats.succeeded
        assert git(
            "rev-parse", "refs/remotes/origin/main", cwd=work
        ).stdout.strip() == remote.origin_head()


class TestCredentialDialog:
    """다이얼로그 자체의 계약."""

    def test_ok_is_disabled_until_both_fields_are_filled(self, qtbot) -> None:  # noqa: ANN001
        """빈 값을 보내면 git이 또 거부한다 — 막을 수 있는 왕복이다."""
        from PySide6.QtWidgets import QDialogButtonBox

        from gitclient.ui.credential_dialog import CredentialDialog

        dialog = CredentialDialog(url="http://example.com/r.git")
        qtbot.addWidget(dialog)
        ok = dialog.findChild(QDialogButtonBox).button(
            QDialogButtonBox.StandardButton.Ok
        )

        assert not ok.isEnabled()
        dialog._username.setText("alice")
        assert not ok.isEnabled()
        dialog._password.setText("token")
        assert ok.isEnabled()

    def test_whitespace_username_is_not_enough(self, qtbot) -> None:  # noqa: ANN001
        from PySide6.QtWidgets import QDialogButtonBox

        from gitclient.ui.credential_dialog import CredentialDialog

        dialog = CredentialDialog()
        qtbot.addWidget(dialog)
        dialog._username.setText("   ")
        dialog._password.setText("token")

        ok = dialog.findChild(QDialogButtonBox).button(
            QDialogButtonBox.StandardButton.Ok
        )
        assert not ok.isEnabled()

    def test_password_field_is_masked(self, qtbot) -> None:  # noqa: ANN001
        from PySide6.QtWidgets import QLineEdit

        from gitclient.ui.credential_dialog import CredentialDialog

        dialog = CredentialDialog()
        qtbot.addWidget(dialog)
        assert dialog._password.echoMode() == QLineEdit.EchoMode.Password

    def test_rejection_explains_why_it_asks_again(self, qtbot) -> None:  # noqa: ANN001
        """이유 없이 같은 창이 다시 뜨면 사용자는 자기가 잘못 눌렀다고 생각한다."""
        from PySide6.QtWidgets import QLabel

        from gitclient.ui.credential_dialog import CredentialDialog

        dialog = CredentialDialog(rejected=True)
        qtbot.addWidget(dialog)
        texts = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "거부" in texts

    def test_url_credentials_are_redacted(self, qtbot) -> None:  # noqa: ANN001
        """원격 주소에 토큰을 박아둔 사용자가 있다 — 화면에 그대로 띄우면 안 된다."""
        from PySide6.QtWidgets import QLabel

        from gitclient.ui.credential_dialog import CredentialDialog

        dialog = CredentialDialog(
            url="https://alice:ghp_supersecret@github.com/o/r.git"
        )
        qtbot.addWidget(dialog)
        texts = " ".join(label.text() for label in dialog.findChildren(QLabel))

        assert "ghp_supersecret" not in texts
        assert "github.com" in texts


class TestRetryFlow:
    """인증 실패 → 물어봄 → 다시 시도의 UI 흐름."""

    @pytest.fixture
    def window(self, qtbot, work: Path):  # noqa: ANN001, ANN201
        from gitclient.ui.main_window import MainWindow

        w = MainWindow()
        qtbot.addWidget(w)
        errors: list = []
        w._report = errors.append
        w.reported_errors = errors
        w.open_repository(str(work))
        qtbot.waitUntil(lambda: not w._loading, timeout=30_000)
        return w

    def _answer_with(self, monkeypatch, credentials: Credentials | None) -> None:
        """다이얼로그를 띄우지 않고 정해진 답을 돌려주게 만든다."""
        from PySide6.QtWidgets import QDialog

        import gitclient.ui.main_window as module

        class FakeDialog:
            def __init__(self, **kwargs) -> None:  # noqa: ANN003
                self.kwargs = kwargs

            def exec(self) -> int:
                return (
                    QDialog.DialogCode.Accepted
                    if credentials is not None
                    else QDialog.DialogCode.Rejected
                )

            def credentials(self) -> Credentials:
                assert credentials is not None
                return credentials

        monkeypatch.setattr(module, "CredentialDialog", FakeDialog)

    def test_supplying_credentials_completes_the_fetch(
        self, window, qtbot, monkeypatch, remote: AuthenticatedRemote  # noqa: ANN001
    ) -> None:
        remote.add_remote_commit()
        self._answer_with(monkeypatch, good())

        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=60_000)
        qtbot.waitUntil(lambda: not window._loading, timeout=60_000)

        assert window.reported_errors == [], [
            e.message for e in window.reported_errors
        ]
        assert "가져오기 완료" in window._transfer_label.text()

    def test_cancelling_reports_nothing_but_says_so(
        self, window, qtbot, monkeypatch  # noqa: ANN001
    ) -> None:
        """취소는 실패가 아니라 선택이다 — 모달을 또 띄우면 안 된다."""
        self._answer_with(monkeypatch, None)

        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=60_000)

        assert window.reported_errors == []
        assert "취소" in window.statusBar().currentMessage()

    def test_wrong_credentials_do_not_loop_forever(
        self, window, qtbot, monkeypatch  # noqa: ANN001
    ) -> None:
        """되묻기는 한 번만. 아니면 틀린 값으로 무한 반복이 된다."""
        wrong = Credentials(username=USERNAME, password="nope")
        self._answer_with(monkeypatch, wrong)

        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=60_000)
        qtbot.waitUntil(
            lambda: bool(window.reported_errors), timeout=60_000
        )

        assert len(window.reported_errors) == 1
        assert window.reported_errors[0].rejected is True

    def test_credentials_never_reach_the_status_bar(
        self, window, qtbot, monkeypatch  # noqa: ANN001
    ) -> None:
        secret = "hunter2-should-not-leak"
        self._answer_with(
            monkeypatch, Credentials(username=USERNAME, password=secret)
        )

        window._on_fetch()
        qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=60_000)
        qtbot.waitUntil(lambda: bool(window.reported_errors), timeout=60_000)

        assert secret not in window.statusBar().currentMessage()
        assert secret not in window._transfer_label.text()
        for error in window.reported_errors:
            assert secret not in (error.detail or "")
            assert secret not in error.message
