"""fetch 백그라운드 작업.

네트워크 작업은 초 단위로 길어지므로 UI 스레드에서 실행하면 안 된다
(doc/design.md §3.3). 계측 결과를 함께 실어 보낸다.

WriteQueue를 쓰지 않는 이유: fetch는 워킹 트리와 인덱스를 건드리지 않고
원격 추적 참조만 갱신하므로, §3.3 규칙 3이 막으려는 인덱스 경합 대상이
아니다. 대신 같은 저장소에 fetch가 중복으로 뜨지 않도록 UI가 막는다.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QRunnable, Signal

from gitclient.domain.errors import GitClientError

if TYPE_CHECKING:
    from gitclient.infrastructure.remote_engine import RemoteEngine


class FetchSignals(QObject):
    finished = Signal(object)
    """TransferStats."""

    failed = Signal(object)
    """GitClientError."""


class FetchWorker(QRunnable):
    def __init__(
        self,
        repo_path: str | Path,
        remote: str = "origin",
        *,
        tags: bool = False,
    ) -> None:
        super().__init__()
        self._repo_path = str(repo_path)
        self._remote = remote
        self._tags = tags
        self._cancelled = False
        self._engine: RemoteEngine | None = None
        self._lock = threading.Lock()
        self.signals = FetchSignals()
        # 수명은 파이썬이 소유한다 — run() 직후 Qt가 지우면 시그널 방출 중
        # sender가 파괴된다. (다른 워커들과 같은 이유)
        self.setAutoDelete(False)

    def cancel(self) -> None:
        """결과를 버리고 진행 중인 git 프로세스를 끊는다.

        프로세스를 실제로 죽여야 하는 이유: 죽이지 않으면 전역 스레드풀 슬롯이
        원격 응답까지(최대 `DEFAULT_TIMEOUT_S`) 붙잡힌다. 창을 닫은 뒤에도
        프로세스가 남아, 사용자가 앱을 다시 띄우면 두 인스턴스가 같은 계측
        DB에 쓰게 된다.

        fetch는 원격 추적 참조만 갱신하므로 중간에 끊어도 저장소가 깨지지
        않고, 받은 객체는 다음 fetch에서 재사용된다.
        """
        with self._lock:
            self._cancelled = True
            engine = self._engine
        if engine is not None:
            engine.abort()

    def run(self) -> None:
        try:
            from gitclient.infrastructure.remote_engine import RemoteEngine
            from gitclient.infrastructure.stats_store import StatsStore

            engine = RemoteEngine(self._repo_path)
            with self._lock:
                cancelled = self._cancelled
                self._engine = engine
            if cancelled:
                # cancel()이 엔진 생성보다 먼저 왔다 — 시작하지 않는다.
                return

            stats = engine.fetch(self._remote, tags=self._tags)

            # 계측 저장은 실패해도 fetch의 성공을 뒤집지 않는다.
            try:
                import pygit2

                # 집계 키는 사용자가 입력한 경로가 아니라 정규화된 git
                # 디렉터리다. 같은 저장소를 다른 표기(하위 디렉터리, 슬래시
                # 방향, 드라이브 문자 대소문자)로 열어도 누적 집계가 쪼개지면
                # 안 된다 — 쪼개지면 목적함수인 누적 전송 바이트가 과소
                # 집계되고 롤링 보관도 키마다 따로 걸린다.
                # workdir이 아니라 git 디렉터리를 쓰는 이유: bare 저장소는
                # workdir이 없다. (WriteQueue 키와 같은 취지)
                repo_key = str(
                    Path(pygit2.discover_repository(self._repo_path)).resolve()
                )
                StatsStore(StatsStore.default_path()).record(repo_key, stats)
            except Exception:  # noqa: BLE001 - 계측은 부가 기능이다
                pass

            if self._cancelled:
                return
            self.signals.finished.emit(stats)

        except GitClientError as exc:
            if not self._cancelled:
                self.signals.failed.emit(exc)
        except Exception as exc:  # noqa: BLE001 - 워커에서 새는 예외는 앱을 죽인다
            if not self._cancelled:
                self.signals.failed.emit(
                    GitClientError(
                        "가져오기 중 예상치 못한 오류가 발생했습니다.",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
