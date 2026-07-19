"""저장소 쓰기 작업 직렬화 큐.

§3.3 규칙 3: 쓰기 작업은 저장소 단위로 직렬화한다. 같은 저장소에
쓰기 두 개가 동시에 들어가면 인덱스가 깨진다.

동작: 작업을 FIFO로 쌓고, 한 번에 하나씩 워커 스레드에서 실행한다.
앞 작업이 실패해도 큐는 멈추지 않는다 — 실패는 해당 작업의 실패일 뿐,
뒤 작업(예: 사용자가 이어서 누른 스테이징)을 인질로 잡을 이유가 없다.

각 작업은 자신만의 엔진 핸들로 실행된다 (스레드 간 핸들 공유 금지).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from gitclient.domain.errors import GitClientError


@dataclass(frozen=True)
class _Job:
    job_id: int
    """이 큐 안에서 유일한 식별자. 이름은 중복될 수 있어 대조에 못 쓴다."""

    name: str
    """사용자 표시용 작업 이름 (예: "스테이징: a.txt")."""

    work: Callable[[Any], Any]
    """워커 스레드에서 실행된다. 인자로 그 작업 전용 엔진을 받는다."""


class _JobSignals(QObject):
    succeeded = Signal(int, str, object)
    failed = Signal(int, str, object)


class _JobRunnable(QRunnable):
    def __init__(self, repo_path: str, job: _Job) -> None:
        super().__init__()
        self._repo_path = repo_path
        self._job = job
        self.signals = _JobSignals()
        self.setAutoDelete(False)  # 수명은 파이썬이 소유 (시그널 수명 결함 방지)

    def run(self) -> None:
        job = self._job
        try:
            from gitclient.infrastructure.local_engine import LocalGitEngine

            engine = LocalGitEngine.open(self._repo_path)
            result = job.work(engine)
            self.signals.succeeded.emit(job.job_id, job.name, result)
        except GitClientError as exc:
            self.signals.failed.emit(job.job_id, job.name, exc)
        except Exception as exc:  # noqa: BLE001 - 워커에서 새는 예외는 앱을 죽인다
            self.signals.failed.emit(
                job.job_id,
                job.name,
                GitClientError(
                    f"{job.name} 중 예상치 못한 오류가 발생했습니다.",
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )


class WriteQueue(QObject):
    """저장소 하나의 쓰기 작업을 순서대로 실행한다.

    시그널은 모두 UI 스레드로 큐 연결된다.
    - job_succeeded(job_id, name, result)
    - job_failed(job_id, name, GitClientError)
    - idle() — 큐가 비었을 때. 상태 새로 고침 트리거로 쓴다.

    같은 이름의 작업이 연달아 제출될 수 있으므로(커밋 연타 등) 개별 작업의
    완료를 기다리는 쪽은 이름이 아니라 job_id로 대조해야 한다.
    """

    job_succeeded = Signal(int, str, object)
    job_failed = Signal(int, str, object)
    idle = Signal()

    def __init__(
        self,
        repo_path: str,
        pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._repo_path = repo_path
        self._pool = pool or QThreadPool.globalInstance()
        self._pending: deque[_Job] = deque()
        self._active: _JobRunnable | None = None
        self._next_id = 1

    @property
    def repo_path(self) -> str:
        return self._repo_path

    @property
    def is_busy(self) -> bool:
        return self._active is not None or bool(self._pending)

    def submit(self, name: str, work: Callable[[Any], Any]) -> int:
        """작업을 큐에 넣고 job_id를 반환한다. 큐가 놀고 있으면 즉시 시작한다."""
        job_id = self._next_id
        self._next_id += 1
        self._pending.append(_Job(job_id, name, work))
        self._start_next_if_idle()
        return job_id

    def _start_next_if_idle(self) -> None:
        if self._active is not None or not self._pending:
            return

        job = self._pending.popleft()
        runnable = _JobRunnable(self._repo_path, job)
        runnable.signals.succeeded.connect(
            lambda jid, name, result, r=runnable: self._on_done(
                r, jid, name, result, None
            )
        )
        runnable.signals.failed.connect(
            lambda jid, name, error, r=runnable: self._on_done(
                r, jid, name, None, error
            )
        )
        self._active = runnable
        self._pool.start(runnable)

    def _on_done(
        self,
        runnable: _JobRunnable,
        job_id: int,
        name: str,
        result: object,
        error: object,
    ) -> None:
        if runnable is not self._active:
            return  # 방어적 — 정상 흐름에서는 항상 일치한다

        self._active = None

        if error is not None:
            self.job_failed.emit(job_id, name, error)
        else:
            self.job_succeeded.emit(job_id, name, result)

        # 위 방출은 직접 연결이다 — 슬롯이 모달을 띄우면 그 중첩 이벤트 루프
        # 안에서 새 작업이 제출되고 `_active`가 None이라 **즉시 시작될 수**
        # 있다. 그때 `self._pending`만 보고 idle을 내면 워킹트리를 쓰는 중에
        # UI가 전체 재로딩을 시작하고, 정작 그 작업이 끝났을 때의 idle은
        # 재로딩 플래그가 이미 소비돼 아무 일도 하지 못한다.
        #
        # 그래서 방출 **이후**의 상태를 다시 읽는다. 재진입으로 시작된 작업의
        # idle은 그 작업의 _on_done이 제 차례에 낸다.
        self._start_next_if_idle()
        if not self.is_busy:
            self.idle.emit()
