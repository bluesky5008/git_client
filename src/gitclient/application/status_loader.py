"""작업 디렉터리 상태 조회 백그라운드 작업.

status는 워킹 트리 전체를 스캔하므로 파일 수에 비례한다 — UI 스레드 금지.
(doc/design.md §3.3)
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from gitclient.domain.errors import GitClientError


class StatusLoaderSignals(QObject):
    ready = Signal(object, object)
    """(WorkingTreeStatus, head_message: str | None)."""

    failed = Signal(object)
    """GitClientError."""


class StatusLoader(QRunnable):
    def __init__(self, repo_path: str | Path) -> None:
        super().__init__()
        self._repo_path = str(repo_path)
        self._cancelled = False
        self.signals = StatusLoaderSignals()
        self.setAutoDelete(False)  # 수명은 파이썬이 소유 (시그널 수명 결함 방지)

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from gitclient.infrastructure.local_engine import LocalGitEngine

            engine = LocalGitEngine.open(self._repo_path)
            status = engine.working_tree_status()
            head_message = engine.head_message()

            if self._cancelled:
                return
            self.signals.ready.emit(status, head_message)

        except GitClientError as exc:
            if not self._cancelled:
                self.signals.failed.emit(exc)
        except Exception as exc:  # noqa: BLE001 - 워커에서 새는 예외는 앱을 죽인다
            if not self._cancelled:
                self.signals.failed.emit(
                    GitClientError(
                        "작업 디렉터리 상태를 읽는 중 오류가 발생했습니다.",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
