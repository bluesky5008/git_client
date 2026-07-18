"""참조(브랜치/태그) 열거 백그라운드 작업.

ref 열거는 ref 수에 비례해 느려진다 — 실측 ref당 약 1.3ms, 5천 ref면 6초를
넘는다. 초기 구현은 open_repository()의 info()가 이를 UI 스레드에서 동기
실행해 G4 예산(§3.3, 단일 블록 ≤ 50ms)을 크게 초과했다. 이 워커가 그 경로를
대체한다.

CommitLoader와 같은 패턴이다: 자신만의 엔진 핸들을 열고(스레드 간 핸들 공유
금지), setAutoDelete(False)로 수명은 파이썬이 소유한다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from gitclient.domain.errors import GitClientError


class RefsLoaderSignals(QObject):
    ready = Signal(list)
    """list[Ref] — 로컬/원격 브랜치와 태그 전체."""

    failed = Signal(object)
    """GitClientError."""


class RefsLoader(QRunnable):
    def __init__(self, repo_path: str | Path) -> None:
        super().__init__()
        self._repo_path = str(repo_path)
        self._cancelled = False
        self.signals = RefsLoaderSignals()
        # 수명은 파이썬이 소유한다 — run() 직후 Qt가 지우면
        # 시그널 방출 도중 sender가 파괴된다. (CommitLoader와 동일한 이유)
        self.setAutoDelete(False)

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from gitclient.infrastructure.local_engine import LocalGitEngine

            engine = LocalGitEngine.open(self._repo_path)
            refs = engine.refs()

            if self._cancelled:
                return
            self.signals.ready.emit(refs)

        except GitClientError as exc:
            if not self._cancelled:
                self.signals.failed.emit(exc)
        except Exception as exc:  # noqa: BLE001 - 워커에서 새는 예외는 앱을 죽인다
            if not self._cancelled:
                self.signals.failed.emit(
                    GitClientError(
                        "참조 목록을 읽는 중 예상치 못한 오류가 발생했습니다.",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
