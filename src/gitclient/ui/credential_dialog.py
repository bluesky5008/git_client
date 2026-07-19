"""자격증명 입력 다이얼로그.

**우리가 직접 묻는 이유** (§4.8): git이나 credential helper가 자기 프롬프트를
띄우게 두면 두 가지가 깨진다 — 워커 스레드가 응답까지 무기한 멈추고,
사용자는 앱과 무관해 보이는 창을 만난다. 그래서 비대화형을 강제하고
프롬프트만 가져온다.

**입력값을 저장하지 않는다** (ADR-3). 여기서 받은 값은 한 번의 작업 동안만
살아 있고, 보관은 git의 credential helper에 위임한다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from gitclient.infrastructure.askpass import Credentials


class CredentialDialog(QDialog):
    """사용자 이름과 비밀번호(또는 액세스 토큰)를 받는다."""

    def __init__(
        self,
        *,
        url: str | None = None,
        username: str | None = None,
        rejected: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("원격 저장소 로그인")
        self.setModal(True)

        layout = QVBoxLayout(self)

        if rejected:
            # 왜 다시 묻는지 말해준다. 같은 창이 이유 없이 다시 뜨면
            # 사용자는 자기가 잘못 눌렀다고 생각한다.
            headline = QLabel("자격증명이 거부되었습니다. 다시 입력해 주세요.")
        else:
            headline = QLabel("이 원격 저장소는 로그인이 필요합니다.")
        headline.setWordWrap(True)
        layout.addWidget(headline)

        if url:
            target = QLabel(_redact(url))
            target.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            target.setStyleSheet("color: palette(mid);")
            target.setWordWrap(True)
            layout.addWidget(target)

        form = QFormLayout()
        self._username = QLineEdit(username or "")
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("사용자 이름", self._username)
        form.addRow("비밀번호 / 토큰", self._password)
        layout.addLayout(form)

        hint = QLabel(
            "GitHub·GitLab 등은 비밀번호 대신 <b>액세스 토큰</b>을 요구합니다."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(hint)

        self._remember = QCheckBox("이 자격증명 저장 (시스템 자격증명 관리자에 위임)")
        self._remember.setChecked(True)
        self._remember.setToolTip(
            "앱이 직접 저장하지 않고 git의 credential helper에 맡깁니다. "
            "helper가 설정돼 있지 않으면 저장되지 않습니다."
        )
        layout.addWidget(self._remember)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._username.textChanged.connect(self._sync_ok)
        self._password.textChanged.connect(self._sync_ok)
        self._sync_ok()

        # 이미 아는 사용자 이름이 있으면 비밀번호로 바로 간다.
        if username:
            self._password.setFocus()
        else:
            self._username.setFocus()

    def _sync_ok(self) -> None:
        """빈 값으로는 확인할 수 없다.

        빈 자격증명을 보내면 git이 또 거부하고, 사용자는 같은 창을 한 번 더
        보게 된다 — 막을 수 있는 왕복이다.
        """
        self._ok.setEnabled(
            bool(self._username.text().strip()) and bool(self._password.text())
        )

    def credentials(self) -> Credentials:
        return Credentials(
            username=self._username.text().strip(),
            password=self._password.text(),
            remember=self._remember.isChecked(),
        )


def _redact(url: str) -> str:
    """URL에 박힌 자격증명을 가린다.

    `https://user:token@host/repo.git` 형태로 원격을 설정해둔 사용자가 있고,
    그 값이 화면에 그대로 뜨면 어깨너머로 토큰이 노출된다.
    """
    scheme, separator, rest = url.partition("://")
    if not separator or "@" not in rest:
        return url
    userinfo, _, host = rest.rpartition("@")
    name = userinfo.split(":", 1)[0]
    return f"{scheme}://{name}:***@{host}" if name else f"{scheme}://{host}"
