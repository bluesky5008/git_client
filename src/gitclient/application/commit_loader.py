"""커밋 로딩 백그라운드 작업.

측정 결과 커밋 순회 비용은 커밋당 약 0.2ms다. 2만 커밋이면 약 4.7초,
10만 커밋이면 20초를 넘는다. 이 시간 동안 UI가 멈추면 안 된다.
(doc/design.md §3.3 동시성 모델, G4)

중요: 이 작업은 자신만의 LocalGitEngine을 연다. libgit2의 Repository 핸들을
UI 스레드와 공유하면 사용자가 커밋을 선택해 diff를 계산하는 동안 같은 핸들을
동시에 쓰게 되어 안전하지 않다. 핸들을 분리하면 그 문제가 생기지 않는다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from gitclient.domain.errors import GitClientError
from gitclient.domain.models import Commit

BATCH_SIZE = 500
"""한 번에 UI로 넘길 커밋 수.

너무 작으면 시그널 왕복이 잦아지고, 너무 크면 첫 행이 늦게 보인다.
"""


class CommitLoaderSignals(QObject):
    batch_ready = Signal(list)
    """list[Commit] — 순서가 보장된 다음 묶음."""

    finished = Signal(int)
    """총 커밋 수."""

    failed = Signal(object)
    """GitClientError."""


class CommitLoader(QRunnable):
    """저장소의 커밋을 순회해 묶음 단위로 UI에 전달한다."""

    def __init__(self, repo_path: str | Path) -> None:
        super().__init__()
        self._repo_path = str(repo_path)
        self._cancelled = False
        self.signals = CommitLoaderSignals()

        # 수명은 파이썬이 소유한다.
        # 기본값(True)이면 run()이 끝나는 즉시 Qt가 C++ 객체를 지우는데,
        # 파이썬 쪽에서 아직 참조 중이면 죽은 객체를 건드리게 된다.
        self.setAutoDelete(False)

    def cancel(self) -> None:
        """다음 커밋 경계에서 중단한다.

        다른 저장소를 열거나 창을 닫을 때 호출한다. 이미 진행 중인 순회를
        즉시 죽일 수는 없지만, 커밋 하나 단위로 확인하므로 반응은 즉각적이다.
        """
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:
        try:
            # UI 스레드와 핸들을 공유하지 않도록 이 스레드 전용으로 연다.
            from gitclient.infrastructure.local_engine import LocalGitEngine

            engine = LocalGitEngine.open(self._repo_path)

            batch: list[Commit] = []
            total = 0

            for commit in engine.iter_commits():
                if self._cancelled:
                    return

                batch.append(commit)
                total += 1

                if len(batch) >= BATCH_SIZE:
                    self.signals.batch_ready.emit(batch)
                    batch = []

            if self._cancelled:
                return

            if batch:
                self.signals.batch_ready.emit(batch)

            self.signals.finished.emit(total)

        except GitClientError as exc:
            if not self._cancelled:
                self.signals.failed.emit(exc)
        except Exception as exc:  # noqa: BLE001 - 워커 스레드에서 예외가 새면 앱이 죽는다
            if not self._cancelled:
                self.signals.failed.emit(
                    GitClientError(
                        "커밋을 읽는 중 예상치 못한 오류가 발생했습니다.",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
