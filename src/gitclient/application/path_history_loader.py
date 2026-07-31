"""경로 히스토리 읽기 백그라운드 작업 (ADR-90).

`path_history()`는 git CLI를 부른다 — 프로세스 기동에 저장소 크기
비례 순회까지, UI 스레드에 둘 수 없는 비용이다 (G4). DiffLoader와
같은 이유로 자신만의 엔진 핸들을 연다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from gitclient.domain.errors import GitClientError


class PathHistoryLoaderSignals(QObject):
    ready = Signal(int, str, list)
    """(token, 경로, list[PathHistoryEntry])."""

    failed = Signal(int, str, object)
    """(token, 경로, GitClientError)."""


class PathHistoryLoader(QRunnable):
    def __init__(self, repo_path: str | Path, path: str, token: int) -> None:
        super().__init__()
        self._repo_path = str(repo_path)
        self._path = path
        self._token = token
        self._cancelled = False
        self.signals = PathHistoryLoaderSignals()
        # 다른 로더와 동일 — 수명은 파이썬이 소유한다.
        self.setAutoDelete(False)

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from gitclient.infrastructure.local_engine import LocalGitEngine

            engine = LocalGitEngine.open(self._repo_path)
            entries = engine.path_history(self._path)
            if self._cancelled:
                return
            self.signals.ready.emit(self._token, self._path, entries)
        except GitClientError as error:
            if self._cancelled:
                return
            self.signals.failed.emit(self._token, self._path, error)
        except Exception as exc:  # noqa: BLE001 - 워커에서 새는 예외는 앱을 죽인다
            if self._cancelled:
                return
            self.signals.failed.emit(
                self._token,
                self._path,
                GitClientError(
                    "파일 히스토리를 읽는 중 예상치 못한 오류가 발생했습니다.",
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
