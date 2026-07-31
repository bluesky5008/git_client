"""충돌 상세 읽기 백그라운드 작업 (DCR-002, ADR-77).

`conflict_detail()`은 인덱스 스테이지의 blob을 통째로 읽는다 — 비용이
파일 크기에 비례한다 (감사 실측: 7.4MB 파일 143ms, 37.8MB 파일 313ms).
초기 구현은 "상한이 파일 하나 크기"라는 주석과 함께 UI 스레드에서 동기
실행했지만, 파일 하나의 크기는 알려진 상한이 아니다 (backlog §3.3).

DiffLoader와 같은 세대 토큰 방식을 쓴다: 사용자가 충돌 목록을 빠르게
훑으면 요청이 겹치고, 순서 역전으로 이전 파일의 내용이 나중에 도착해
화면을 덮으면 사용자가 A의 설명을 읽으며 B를 해결하게 된다 — 이 화면이
막으려는 바로 그 오도다.

편집 여부(`working_edited`)도 여기서 함께 계산한다. 해결 버튼 클릭 시
UI 스레드가 워킹 파일과 blob을 다시 읽지 않도록 — 그 경로는 파일 크기의
약 3배를 UI 스레드에서 썼다 (backlog §3.3).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from gitclient.domain.errors import GitClientError


class ConflictLoaderSignals(QObject):
    ready = Signal(int, str, object, object)
    """(token, 경로, ConflictDetail, 워킹 파일 지문).

    지문은 `(편집됐는가, size, mtime_ns)`다 — 편집 여부 판정을 **여기서
    끝내고** UI는 클릭 시점에 `stat()` 하나로 그 판정이 아직 유효한지만
    본다 (backlog §3.3). 판정 자체는 파일 전체를 읽어야 하는데, 그것을
    클릭 경로에 두면 파일 크기가 UI 스레드 비용이 된다.
    """

    failed = Signal(int, str, object)
    """(token, 경로, GitClientError) — 그 사이 해결됐거나 저장소가 바뀌었다."""


class ConflictLoader(QRunnable):
    """충돌 파일 하나의 양쪽 내용을 읽는다.

    DiffLoader와 같은 이유로 자신만의 엔진 핸들을 연다 — UI 스레드의
    핸들과 공유하면 다른 워커와 같은 libgit2 객체를 동시에 만진다.
    """

    def __init__(self, repo_path: str | Path, path: str, token: int) -> None:
        super().__init__()
        self._repo_path = str(repo_path)
        self._path = path
        self._token = token
        self._cancelled = False
        self.signals = ConflictLoaderSignals()
        # DiffLoader와 동일 — 수명은 파이썬이 소유한다.
        self.setAutoDelete(False)

    @property
    def token(self) -> int:
        return self._token

    def cancel(self) -> None:
        self._cancelled = True

    def _working_fingerprint(self, detail) -> tuple | None:  # noqa: ANN001
        """(편집됐는가, size, mtime_ns). 파일이 없으면 None.

        **마커 유무만으로는 판단할 수 없다** — 바이너리 충돌에는 애초에
        마커가 없으므로, 그것만 보면 이 기능이 존재하는 이유인 경우마다
        확인창이 뜬다. 마커가 남아 있으면 손대지 않은 것이고, 없더라도
        내용이 양쪽 원본 중 하나와 같으면 git이 써둔 그대로다.
        """
        target = Path(self._repo_path) / self._path
        try:
            stat = target.stat()
            data = target.read_bytes()
        except OSError:
            return None
        if b"<<<<<<<" in data and b">>>>>>>" in data:
            edited = False
        else:
            edited = data not in (detail.ours.data, detail.theirs.data)
        return (edited, stat.st_size, stat.st_mtime_ns)

    def run(self) -> None:
        try:
            from gitclient.infrastructure.local_engine import LocalGitEngine

            engine = LocalGitEngine.open(self._repo_path)
            detail = engine.conflict_detail(self._path)
            fingerprint = self._working_fingerprint(detail)
            if self._cancelled:
                return
            self.signals.ready.emit(
                self._token, self._path, detail, fingerprint
            )
        except GitClientError as error:
            if self._cancelled:
                return
            self.signals.failed.emit(self._token, self._path, error)
