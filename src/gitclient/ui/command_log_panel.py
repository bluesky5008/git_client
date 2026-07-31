"""실행된 git 명령 로그 도크 (FR-11·12, §5.2 원칙 3).

GUI는 터미널을 숨기는 도구가 아니라 대신 눌러주는 도구다 — 무엇을
눌렀는지는 보여줘야 한다. 문제를 보고하거나 같은 일을 CLI로 재현할 때
정확한 명령이 여기서 나온다.

기록은 워커 스레드에서 온다 — `CommandLog`의 콜백을 큐잉 시그널로
옮겨 UI 스레드에서만 위젯을 만진다.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QDockWidget, QPlainTextEdit

from gitclient.domain.command_log import COMMAND_LOG, CommandRecord


class _Bridge(QObject):
    """워커 스레드의 기록 콜백을 UI 스레드 시그널로 옮긴다."""

    arrived = Signal(object)


class CommandLogDock(QDockWidget):
    def __init__(self, parent=None) -> None:  # noqa: ANN001
        super().__init__("실행된 git 명령", parent)
        self.setObjectName("command-log-dock")
        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._view.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.setWidget(self._view)

        self._bridge = _Bridge()
        # 큐잉 연결이 이 위젯의 존재 이유다 — 기록은 워커 스레드에서 온다.
        self._bridge.arrived.connect(
            self._append, Qt.ConnectionType.QueuedConnection
        )
        for record in COMMAND_LOG.snapshot():
            self._append(record)
        COMMAND_LOG.subscribe(self._bridge.arrived.emit)

    def _append(self, record: CommandRecord) -> None:
        self._view.appendPlainText(_format(record))
        bar = self._view.verticalScrollBar()
        bar.setValue(bar.maximum())


def _format(record: CommandRecord) -> str:
    mark = "✓" if record.succeeded else "✗"
    rc = "?" if record.returncode is None else str(record.returncode)
    when = record.when.strftime("%H:%M:%S")
    return (
        f"{mark} {when}  rc={rc:<3} {record.duration_ms:>6}ms  "
        + " ".join(record.argv)
    )
