"""diff 계산 백그라운드 작업.

커밋 선택 시 diff 계산은 변경 크기에 비례해 커진다 — 대형 커밋에서 실측
수백 ms. 초기 구현은 이를 UI 스레드에서 동기 실행해 G4 예산을 초과했다.
(doc/design.md §3.3, §10 남은 과제)

세대 토큰(token): 사용자가 방향키로 커밋을 빠르게 훑으면 여러 요청이 동시에
날아간다. 순서 역전으로 이전 커밋의 diff가 나중에 도착해 화면을 덮으면
안 되므로, 발급 시점의 토큰을 결과에 실어 보내고 UI는 최신 토큰만 받는다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from gitclient.domain.errors import GitClientError


class DiffLoaderSignals(QObject):
    ready = Signal(int, object, object)
    """(token, CommitDetail | None, list[DiffLine]).

    detail은 커밋 전체 diff 요청일 때만 실려 온다. path 좁히기 요청은
    파일 목록을 다시 만들 필요가 없으므로 None이다.
    """

    failed = Signal(int, object)
    """(token, GitClientError)."""


class DiffLoader(QRunnable):
    """커밋 하나의 변경 파일 목록과 diff 줄을 계산한다.

    자신만의 엔진 핸들을 연다. UI 스레드의 핸들과 공유하면 다른 워커
    (CommitLoader 등)와 같은 libgit2 객체를 동시에 만지게 된다.
    """

    def __init__(
        self,
        repo_path: str | Path,
        sha: str,
        token: int,
        *,
        path: str | None = None,
        include_detail: bool = True,
    ) -> None:
        super().__init__()
        self._repo_path = str(repo_path)
        self._sha = sha
        self._token = token
        self._path = path
        self._include_detail = include_detail
        self._cancelled = False
        self.signals = DiffLoaderSignals()
        # CommitLoader/RefsLoader와 동일 — 수명은 파이썬이 소유한다.
        self.setAutoDelete(False)

    @property
    def token(self) -> int:
        return self._token

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from gitclient.infrastructure.local_engine import LocalGitEngine

            engine = LocalGitEngine.open(self._repo_path)

            detail = None
            if self._include_detail:
                detail = engine.commit_detail(self._sha)
                if self._cancelled:
                    return

            lines = engine.diff_lines(self._sha, path=self._path)

            if self._cancelled:
                return
            self.signals.ready.emit(self._token, detail, lines)

        except GitClientError as exc:
            if not self._cancelled:
                self.signals.failed.emit(self._token, exc)
        except Exception as exc:  # noqa: BLE001 - 워커에서 새는 예외는 앱을 죽인다
            if not self._cancelled:
                self.signals.failed.emit(
                    self._token,
                    GitClientError(
                        "diff를 계산하는 중 예상치 못한 오류가 발생했습니다.",
                        detail=f"{type(exc).__name__}: {exc}",
                    ),
                )


class WorkdirDiffSignals(QObject):
    ready = Signal(int, object, object, object)
    """(token, FilePatch, list[DiffLine], list[position|None])."""

    failed = Signal(int, object)


class WorkdirDiffLoader(QRunnable):
    """커밋되지 않은 변경(스테이징/미스테이징) 하나의 패치를 읽는다.

    부분 스테이징이 가능하도록 표시용 줄과 **패치 좌표**를 함께 넘긴다.
    화면의 줄과 패치의 줄이 같은 좌표를 공유해야 사용자가 고른 것이
    그대로 적용된다.
    """

    def __init__(
        self,
        repo_path: str | Path,
        path: str,
        token: int,
        *,
        staged: bool,
    ) -> None:
        super().__init__()
        self._repo_path = str(repo_path)
        self._path = path
        self._token = token
        self._staged = staged
        self._cancelled = False
        self.signals = WorkdirDiffSignals()
        self.setAutoDelete(False)

    @property
    def token(self) -> int:
        return self._token

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            from gitclient.domain.models import DiffLine, DiffLineKind
            from gitclient.domain.patch import iter_display_rows
            from gitclient.infrastructure.local_engine import LocalGitEngine

            engine = LocalGitEngine.open(self._repo_path)
            patch = engine.file_patch(self._path, staged=self._staged)

            kinds = {
                "header": DiffLineKind.FILE_HEADER,
                "hunk": DiffLineKind.HUNK_HEADER,
                "add": DiffLineKind.ADDITION,
                "del": DiffLineKind.DELETION,
                "context": DiffLineKind.CONTEXT,
            }
            lines: list = []
            positions: list = []
            for kind, text, old_no, new_no, position in iter_display_rows(patch):
                lines.append(
                    DiffLine(
                        kind=kinds[kind],
                        text=text,
                        old_lineno=old_no,
                        new_lineno=new_no,
                    )
                )
                positions.append(position)

            if self._cancelled:
                return
            self.signals.ready.emit(self._token, patch, lines, positions)

        except GitClientError as exc:
            if not self._cancelled:
                self.signals.failed.emit(self._token, exc)
        except Exception as exc:  # noqa: BLE001 - 워커에서 새는 예외는 앱을 죽인다
            if not self._cancelled:
                self.signals.failed.emit(
                    self._token,
                    GitClientError(
                        "변경 사항 diff를 계산하는 중 오류가 발생했습니다.",
                        detail=f"{type(exc).__name__}: {exc}",
                    ),
                )
