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
    ready = Signal(list, object)
    """(list[Ref], (앞선 수, 뒤처진 수) | None).

    벌어진 정도를 **여기서 함께 계산해 보낸다** (backlog §3.4). pygit2
    질의라 CLI보다 훨씬 싸지만 공짜는 아니다 — 갈라진 만큼 선형이라
    1만+1만에서 최악 84.8ms로 G4(50ms)를 넘는다(감사 실측). 참조를
    읽는 이 워커가 이미 저장소를 열어 두었으므로 여기가 가장 싼 자리다.
    """

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

    @staticmethod
    def _divergence(engine) -> tuple[int, int] | None:  # noqa: ANN001
        """현재 브랜치가 upstream과 얼마나 벌어졌는가. 없으면 None.

        upstream이 없거나 아직 fetch하지 않은 것은 오류가 아니다 —
        화면 입장에서 "표시할 것이 없음"과 같으므로 None으로 접는다.
        """
        from gitclient.domain.errors import GitClientError as _Error

        try:
            resolved = engine.upstream_of_head()
            if resolved is None:
                return None
            branch = engine.info(include_refs=False).head_shorthand
            if branch is None:
                return None
            return engine.ahead_behind(branch, resolved[1])
        except _Error:
            return None

    def run(self) -> None:
        try:
            from gitclient.infrastructure.local_engine import LocalGitEngine

            engine = LocalGitEngine.open(self._repo_path)
            refs = engine.refs()
            divergence = self._divergence(engine)

            if self._cancelled:
                return
            self.signals.ready.emit(refs, divergence)

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
