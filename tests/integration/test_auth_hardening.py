"""인증 경로의 회귀 테스트 — 적대적 리뷰에서 확정된 결함들.

원래 인증 테스트(test_auth.py)는 24개가 전부 통과하고 있었다. 그런데도
아래 결함들이 살아 있었다. 이 파일이 따로 있는 이유는 그 대비를 남기기
위해서다 — **통과하는 입력만 검증하면 통과하는 것만 알게 된다.**

가장 뼈아픈 예: 하네스 비밀번호가 `s3cret-token`이라 셸 메타문자가 하나도
없었고, 그래서 "메타문자가 든 비밀번호는 셸이 재해석해 사용자 워킹 트리에
파일을 만들고 명령까지 실행한다"는 critical 결함이 24개 테스트를 전부
통과한 채로 남아 있었다.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gitclient.domain.errors import AuthenticationRequired
from gitclient.infrastructure.askpass import (
    Credentials,
    shim_environment,
    write_shim,
)
from gitclient.infrastructure.remote_engine import RemoteEngine, _without_userinfo
from tests.integration.auth_harness import (
    PASSWORD,
    USERNAME,
    AuthenticatedRemote,
)
from tests.integration.remote_harness import git

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


HOSTILE = [
    pytest.param("tok&whoami", id="ampersand"),
    pytest.param("tok|more", id="pipe"),
    pytest.param("a>b-secret", id="redirect"),
    pytest.param("tok^caret", id="caret"),
    pytest.param("tok)paren(", id="parens"),
    pytest.param("tok%PATH%", id="percent"),
    pytest.param("tok!bang!", id="bang"),
    pytest.param("tok with spaces", id="spaces"),
]


def ask_shim(tmp_path: Path, secret: str, prompt: str, *, user: str = "alice") -> str:
    """shim을 git이 부르는 방식 그대로 실행하고 stdout을 돌려준다."""
    shim = write_shim(tmp_path)
    env = dict(os.environ)
    env.update(shim_environment(shim, Credentials(username=user, password=secret)))
    result = subprocess.run(
        [str(shim), prompt],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    return result.stdout.rstrip("\r\n")


class TestShimHandlesHostileSecrets:
    """비밀번호에 셸 메타문자가 들어와도 안전해야 한다 (확정 critical).

    Windows 셸은 `%VAR%`를 특수문자 파싱 **앞** 단계에서 확장하고 결과를 다시
    파싱한다. 실측에서 비밀번호 `a>b-secret`이 사용자 워킹 트리에 `b-secret`
    파일을 만들고 그 안에 `a`를 남겼다 — 비밀번호가 파일로 흘렀고, `&` 형태로는
    임의 명령까지 실행됐다.
    """

    @pytest.mark.parametrize("secret", HOSTILE)
    def test_password_survives_the_shell_intact(
        self, tmp_path: Path, secret: str
    ) -> None:
        answer = ask_shim(tmp_path, secret, "Password for 'https://x': ")
        assert answer == secret, f"셸이 비밀번호를 변형했다: {answer!r}"

    @pytest.mark.parametrize("secret", HOSTILE)
    def test_nothing_is_written_to_disk(self, tmp_path: Path, secret: str) -> None:
        """리다이렉션이 살아 있으면 비밀번호 조각이 파일로 떨어진다."""
        before = set(tmp_path.iterdir())

        ask_shim(tmp_path, secret, "Password for 'https://x': ")

        created = {p.name for p in tmp_path.iterdir()} - {p.name for p in before}
        # shim 파일 자체는 생긴다 — 그 외에는 아무것도 생기면 안 된다.
        assert not (created - {"gitclient-askpass.bat", "gitclient-askpass.sh"}), (
            f"셸이 파일을 만들었다: {created}"
        )

    def test_command_injection_does_not_execute(self, tmp_path: Path) -> None:
        marker = tmp_path / "PWNED.txt"

        ask_shim(
            tmp_path,
            'tok&echo owned>"' + str(marker) + '"',
            "Password for 'https://x': ",
        )

        assert not marker.exists(), "비밀번호 안의 명령이 실행됐다"

    def test_username_prompt_is_equally_safe(self, tmp_path: Path) -> None:
        answer = ask_shim(
            tmp_path, "whatever", "Username for 'https://x': ", user="al&ice"
        )
        assert answer == "al&ice"

    def test_hostile_password_authenticates_for_real(self, tmp_path: Path) -> None:
        """끝까지 확인한다 — 메타문자가 든 비밀번호로 실제 인증이 통과하는가.

        shim만 고쳐도 git까지 온전히 전달되지 않으면 소용이 없다.
        """
        secret = "s3cr&t>tok^en|x"
        server = AuthenticatedRemote(tmp_path / "srv2", password=secret).start(
            commits=2
        )
        try:
            work = server.clone_anonymously(tmp_path / "w2")
            server.add_remote_commit()

            stats = RemoteEngine(
                work, credentials=Credentials(USERNAME, secret)
            ).fetch(timeout_s=TIMEOUT)

            assert stats.succeeded
            tracked = git(
                "rev-parse", "refs/remotes/origin/main", cwd=work
            ).stdout.strip()
            assert tracked == server.origin_head()
        finally:
            server.stop()


class TestRememberIsHonoured:
    """"저장 안 함"을 고르면 정말로 저장되면 안 된다 (확정 결함).

    우리가 approve를 부르지 않아도 **git 자신이** 인증 성공 후 helper의
    store를 호출한다. 체크박스를 꺼도 값이 저장되어, 사용자가 명시적으로
    거부한 일이 조용히 일어났다.
    """

    def _logging_helper(self, work: Path, tmp_path: Path) -> Path:
        log = tmp_path / "store.log"
        script = tmp_path / "logger.bat"
        script.write_text('@echo off\n@echo %1 >> "' + str(log) + '"\n@exit /b 0\n')
        git(
            "config",
            "credential.helper",
            str(script).replace("\\", "/"),
            cwd=work,
        )
        return log

    def test_unchecking_prevents_storage(
        self, remote: AuthenticatedRemote, work: Path, tmp_path: Path
    ) -> None:
        log = self._logging_helper(work, tmp_path)
        remote.add_remote_commit()

        RemoteEngine(
            work, credentials=Credentials(USERNAME, PASSWORD, remember=False)
        ).fetch(timeout_s=TIMEOUT)

        stored = log.read_text(encoding="utf-8") if log.exists() else ""
        assert "store" not in stored, "저장하지 않기로 했는데 저장됐다"

    def test_checking_does_store(
        self, remote: AuthenticatedRemote, work: Path, tmp_path: Path
    ) -> None:
        log = self._logging_helper(work, tmp_path)
        remote.add_remote_commit()

        RemoteEngine(
            work, credentials=Credentials(USERNAME, PASSWORD, remember=True)
        ).fetch(timeout_s=TIMEOUT)

        assert "store" in log.read_text(encoding="utf-8")


class TestCredentialKeyRoundTrip:
    """저장 키는 git이 조회할 때 쓰는 것과 같아야 한다.

    손으로 조립하면 저장은 되는데 다음 번에 또 묻는 상태가 된다 — 사용자
    입장에서는 "저장을 눌렀는데 매번 묻는다".
    """

    def test_stored_credentials_are_found_again(
        self, remote: AuthenticatedRemote, work: Path, tmp_path: Path
    ) -> None:
        store_file = tmp_path / "creds.txt"
        helper = "store --file=" + str(store_file).replace("\\", "/")
        git("config", "credential.helper", helper, cwd=work)
        remote.add_remote_commit()

        # 1) 자격증명을 주고 성공 → 저장 위임
        RemoteEngine(work, credentials=good()).fetch(timeout_s=TIMEOUT)
        assert store_file.exists(), "저장 자체가 안 됐다"

        # 2) 이제 자격증명 없이 — helper가 찾아내야 한다
        remote.add_remote_commit()
        stats = RemoteEngine(work).fetch(timeout_s=TIMEOUT)

        assert stats.succeeded, "저장한 자격증명을 git이 다시 찾지 못했다"

    def test_embedded_password_is_not_part_of_the_key(self) -> None:
        cleaned = _without_userinfo("https://alice:tok@example.com:8443/o/r.git")

        assert "tok" not in cleaned
        assert "alice" not in cleaned
        assert cleaned == "https://example.com:8443/o/r.git"


class TestDialogTargetIsTrusted:
    def test_login_target_comes_from_config_not_server_output(
        self, work: Path
    ) -> None:
        """서버가 보낸 문구로 로그인 대상을 정하면 안 된다.

        악의적 원격이 `remote: see 'https://evil/'` 같은 줄을 앞세우면 로그인
        창에 다른 호스트가 뜨게 만들 수 있다.
        """
        with pytest.raises(AuthenticationRequired) as excinfo:
            RemoteEngine(work).fetch(timeout_s=TIMEOUT)

        configured = git("remote", "get-url", "origin", cwd=work).stdout.strip()
        assert excinfo.value.url == configured


class TestPosixShim:
    """POSIX 분기도 검증한다 — Windows에서 돌아도 생성 결과는 확인할 수 있다."""

    def test_posix_shim_quotes_the_variables(
        self, tmp_path: Path, monkeypatch
    ) -> None:  # noqa: ANN001
        import gitclient.infrastructure.askpass as module

        monkeypatch.setattr(module.os, "name", "posix")
        monkeypatch.setattr(
            module.Path, "chmod", lambda self, mode: None, raising=False
        )

        shim = module.write_shim(tmp_path)
        content = shim.read_text(encoding="utf-8")

        assert shim.suffix == ".sh"
        # 따옴표 없이 확장하면 공백·글로브가 든 비밀번호가 깨진다.
        assert '"$GITCLIENT_ASKPASS_PASSWORD"' in content
        assert '"$GITCLIENT_ASKPASS_USERNAME"' in content
        assert PASSWORD not in content


class TestRetryStateIsRevalidated:
    """모달이 열려 있는 동안 상태가 바뀔 수 있다."""

    def test_repository_change_during_dialog_cancels_the_retry(
        self, qtbot, remote: AuthenticatedRemote, work: Path, tmp_path: Path
    ) -> None:  # noqa: ANN001
        """다이얼로그가 떠 있는 사이 다른 저장소를 열면 재시도하면 안 된다.

        모달은 중첩 이벤트 루프를 돌리므로 그동안 UI 상태가 얼마든지 바뀐다.
        모달 이전의 판단으로 워커를 띄우면 엉뚱한 저장소에 붙는다.
        """
        from PySide6.QtWidgets import QDialog

        import gitclient.ui.main_window as module
        from gitclient.ui.main_window import MainWindow
        from tests.integration.remote_harness import RemoteFixture

        other = RemoteFixture(tmp_path / "other").build(commits=2, payload_kb=1)

        window = MainWindow()
        qtbot.addWidget(window)
        errors: list = []
        window._report = errors.append
        window.open_repository(str(work))
        qtbot.waitUntil(lambda: not window._loading, timeout=30_000)

        class SwitchingDialog:
            """다이얼로그가 떠 있는 동안 저장소가 바뀌는 상황을 흉내낸다."""

            def __init__(self, **kwargs) -> None:  # noqa: ANN003
                pass

            def exec(self) -> int:
                window.open_repository(str(other.work))
                return QDialog.DialogCode.Accepted

            def credentials(self) -> Credentials:
                return good()

        monkeypatch_target = module.CredentialDialog
        module.CredentialDialog = SwitchingDialog
        try:
            window._on_fetch()
            qtbot.waitUntil(
                lambda: window._fetch_worker is None, timeout=60_000
            )
            qtbot.wait(500)
        finally:
            module.CredentialDialog = monkeypatch_target

        assert window._repo_path == str(other.work)
