"""원격 작업 백그라운드 워커 — fetch / push / pull.

네트워크 작업은 초 단위로 길어지므로 UI 스레드에서 실행하면 안 된다
(doc/design.md §3.3). 계측 결과를 함께 실어 보낸다.

**WriteQueue를 쓰지 않는 이유** (ADR-17): fetch와 push는 워킹 트리와 인덱스를
건드리지 않는다 — fetch는 원격 추적 참조만, push는 아무것도 바꾸지 않는다.
§3.3 규칙 3이 막으려는 인덱스 경합 대상이 아니다. 대신 같은 저장소에 원격
작업이 중복으로 뜨지 않도록 UI가 막는다.

**pull은 예외다.** 뒤쪽 절반(빨리 감기)이 워킹 트리와 인덱스를 실제로 바꾸므로
그 부분은 반드시 WriteQueue를 거쳐야 한다. 그래서 PullWorker는 네트워크
절반만 담당하고, 합치기는 UI가 큐에 제출한다 — 경계가 워커 안이 아니라
워커와 큐 사이에 있다. (§4.7)
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal

from gitclient.domain.errors import GitClientError

if TYPE_CHECKING:
    from gitclient.domain.instrumentation import TransferStats
    from gitclient.infrastructure.remote_engine import RemoteEngine


class RemoteSignals(QObject):
    finished = Signal(object)
    """TransferStats."""

    failed = Signal(object)
    """GitClientError."""


def _repo_key(repo_path: str) -> str:
    """계측 집계 키 — 정규화된 git 디렉터리.

    사용자가 입력한 경로를 그대로 쓰면 같은 저장소를 다른 표기(하위
    디렉터리, 슬래시 방향, 드라이브 문자 대소문자)로 열 때 누적 집계가
    쪼개진다. workdir이 아니라 git 디렉터리를 쓰는 이유는 bare 저장소에
    workdir이 없기 때문이다. (WriteQueue 키와 같은 취지)
    """
    import pygit2

    return str(Path(pygit2.discover_repository(repo_path)).resolve())


class RemoteWorker(QRunnable):
    """원격 작업 하나를 워커 스레드에서 실행하고 계측을 남긴다.

    하위 클래스는 `_operate`만 구현한다. 취소·계측 저장·예외 봉인은
    전부 공통이며, 이들이 어긋나면 UI가 멈추거나 앱이 죽는다.
    """

    #: 실패 메시지에 쓸 작업 이름. 하위 클래스가 채운다.
    label = "원격 작업"

    def __init__(self, repo_path: str | Path, remote: str = "origin") -> None:
        super().__init__()
        self._repo_path = str(repo_path)
        self._remote = remote
        self._cancelled = False
        self._engine: RemoteEngine | None = None
        self._lock = threading.Lock()
        self.signals = RemoteSignals()
        # 수명은 파이썬이 소유한다 — run() 직후 Qt가 지우면 시그널 방출 중
        # sender가 파괴된다. (다른 워커들과 같은 이유)
        self.setAutoDelete(False)

    @property
    def remote(self) -> str:
        return self._remote

    def cancel(self) -> None:
        """결과를 버리고 진행 중인 git 프로세스를 끊는다.

        프로세스를 실제로 죽여야 하는 이유: 죽이지 않으면 전역 스레드풀 슬롯이
        원격 응답까지(최대 `DEFAULT_TIMEOUT_S`) 붙잡힌다. 창을 닫은 뒤에도
        프로세스가 남아, 사용자가 앱을 다시 띄우면 두 인스턴스가 같은 계측
        DB에 쓰게 된다.

        중간에 끊어도 저장소가 깨지지 않는다 — fetch는 원격 추적 참조만
        갱신하고, 받은 객체는 다음 작업에서 재사용된다. push는 원자적이라
        받아들여졌거나 아니거나 둘 중 하나다.
        """
        with self._lock:
            self._cancelled = True
            engine = self._engine
        if engine is not None:
            engine.abort()

    def _operate(self, engine: RemoteEngine) -> TransferStats:
        raise NotImplementedError

    def run(self) -> None:
        try:
            from gitclient.infrastructure.remote_engine import RemoteEngine

            engine = RemoteEngine(self._repo_path)
            with self._lock:
                cancelled = self._cancelled
                self._engine = engine
            if cancelled:
                # cancel()이 엔진 생성보다 먼저 왔다 — 시작하지 않는다.
                return

            stats = self._operate(engine)
            self._record(stats)

            if self._cancelled:
                return
            self.signals.finished.emit(stats)

        except GitClientError as exc:
            # 실패해도 바이트는 이미 나갔을 수 있다 — 여러 ref를 올리다 하나만
            # 거부되면 나머지는 실제로 전송된다. 취소 가드보다 **먼저** 기록한다:
            # 사용자가 취소했더라도 소비한 트래픽은 목적함수에서 빠지면 안 된다.
            stats = getattr(exc, "stats", None)
            if stats is not None:
                self._record(stats)
            if not self._cancelled:
                self.signals.failed.emit(exc)
        except Exception as exc:  # noqa: BLE001 - 워커에서 새는 예외는 앱을 죽인다
            if not self._cancelled:
                self.signals.failed.emit(
                    GitClientError(
                        f"{self.label} 중 예상치 못한 오류가 발생했습니다.",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )

    def _record(self, stats: TransferStats) -> None:
        """계측 저장은 실패해도 본 작업의 성공을 뒤집지 않는다."""
        try:
            from gitclient.infrastructure.stats_store import StatsStore

            StatsStore(StatsStore.default_path()).record(
                _repo_key(self._repo_path), stats
            )
        except Exception:  # noqa: BLE001 - 계측은 부가 기능이다
            pass


class FetchWorker(RemoteWorker):
    label = "가져오기"

    def __init__(
        self,
        repo_path: str | Path,
        remote: str = "origin",
        *,
        tags: bool = False,
    ) -> None:
        super().__init__(repo_path, remote)
        self._tags = tags

    def _operate(self, engine: RemoteEngine) -> TransferStats:
        return engine.fetch(self._remote, tags=self._tags)


class PushWorker(RemoteWorker):
    label = "올리기"

    def __init__(
        self,
        repo_path: str | Path,
        remote: str = "origin",
        branch: str | None = None,
        *,
        set_upstream: bool = False,
    ) -> None:
        super().__init__(repo_path, remote)
        self._branch = branch
        self._set_upstream = set_upstream

    def _operate(self, engine: RemoteEngine) -> TransferStats:
        refspecs = [self._branch] if self._branch else []
        return engine.push(
            self._remote, refspecs, set_upstream=self._set_upstream
        )


class PullWorker(RemoteWorker):
    """pull의 **네트워크 절반**만 담당한다.

    합치기는 워킹 트리를 바꾸므로 WriteQueue를 거쳐야 하고, 그 제출은 UI가
    한다. 워커가 직접 합치면 쓰기 스트림이 둘이 되어 §3.3 규칙 3이 깨진다.
    """

    label = "가져와 합치기"

    def _operate(self, engine: RemoteEngine) -> TransferStats:
        return engine.fetch(self._remote)


def fast_forward_job(upstream_ref: str, branch: str) -> Callable[[Any], str]:
    """WriteQueue에 제출할 빨리 감기 작업.

    큐가 자기 스레드에서 엔진을 열어 넘겨주므로, 여기서는 그 엔진을 받아
    쓰기만 한다.

    **브랜치를 함께 실어 보낸다.** 네트워크 절반이 도는 동안 사용자가 브랜치를
    전환하면 이 작업이 엉뚱한 브랜치를 원격 위치로 덮어쓴다 — 엔진이 실행
    시점에 HEAD를 대조해 거부한다.
    """

    def work(engine: Any) -> str:
        return engine.fast_forward(upstream_ref, branch)

    return work
