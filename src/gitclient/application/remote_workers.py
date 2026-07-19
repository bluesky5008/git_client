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

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal

from gitclient.domain.errors import GitClientError

from gitclient.infrastructure.askpass import Credentials

if TYPE_CHECKING:
    from gitclient.domain.instrumentation import TransferStats
    from gitclient.infrastructure.remote_engine import RemoteEngine


class RemoteSignals(QObject):
    finished = Signal(object)
    """TransferStats."""

    failed = Signal(object)
    """GitClientError."""


def _repo_key(repo_path: str) -> str | None:
    """계측 집계 키 — 정규화된 git 디렉터리. 저장소가 아니면 None.

    사용자가 입력한 경로를 그대로 쓰면 같은 저장소를 다른 표기(하위
    디렉터리, 슬래시 방향, 드라이브 문자 대소문자)로 열 때 누적 집계가
    쪼개진다. workdir이 아니라 git 디렉터리를 쓰는 이유는 bare 저장소에
    workdir이 없기 때문이다. (WriteQueue 키와 같은 취지)

    **탐색을 상위로 올리지 않는다.** `discover_repository`는 부모를 거슬러
    올라가므로, 기존 저장소 **안**에 복제하려다 실패하면 그 바이트가 감싸는
    저장소 앞으로 기록된다 — 과소 집계보다 나쁜 오귀속이다(ADR-26).
    존재하지 않는 경로에는 None을 돌려주는데, 그것을 그대로 `Path()`에
    넘기면 TypeError로 워커가 죽었다.
    """
    import pygit2

    target = Path(repo_path)
    if not (target / ".git").exists() and not (target / "HEAD").exists():
        return None
    discovered = pygit2.discover_repository(str(target))
    if not discovered:
        return None
    # 상위로 샜는지 확인한다 — 우리가 지목한 저장소가 아니면 기록하지 않는다.
    resolved = Path(discovered).resolve()
    if target.resolve() not in (resolved.parent, resolved):
        return None
    return str(resolved)


class RemoteWorker(QRunnable):
    """원격 작업 하나를 워커 스레드에서 실행하고 계측을 남긴다.

    하위 클래스는 `_operate`만 구현한다. 취소·계측 저장·예외 봉인은
    전부 공통이며, 이들이 어긋나면 UI가 멈추거나 앱이 죽는다.
    """

    #: 실패 메시지에 쓸 작업 이름. 하위 클래스가 채운다.
    label = "원격 작업"

    def __init__(
        self,
        repo_path: str | Path,
        remote: str = "origin",
        *,
        credentials: Credentials | None = None,
    ) -> None:
        super().__init__()
        self._repo_path = str(repo_path)
        self._remote = remote
        # 사용자가 방금 입력한 값. 워커가 사는 동안만 들고 있다가 엔진에
        # 넘긴다 — 어디에도 저장하지 않는다. (ADR-3)
        self._credentials = credentials
        self._cancelled = False
        self._succeeded = False
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

            engine = RemoteEngine(
                self._repo_path, credentials=self._credentials
            )
            with self._lock:
                cancelled = self._cancelled
                self._engine = engine
            if cancelled:
                # cancel()이 엔진 생성보다 먼저 왔다 — 시작하지 않는다.
                return

            stats = self._operate(engine)
            self._succeeded = True
            self._record(stats)

            # 통한 자격증명만 저장을 위임한다. 실패한 값을 저장하면 다음
            # 시도가 그 값으로 조용히 거부된다. (ADR-3 — 우리가 쓰지 않는다)
            if self._credentials is not None:
                url = self._credential_url(engine)
                if url:
                    engine.remember_credentials(url)

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

    def _credential_url(self, engine: RemoteEngine) -> str | None:
        """자격증명을 묶을 주소.

        기본은 원격 **이름**을 주소로 푸는 것이다. clone은 이름이 아직 없어
        하위 클래스가 주소를 직접 준다 — 이 갈래가 없으면 `remote get-url`이
        URL을 이름으로 받아 실패하고, 저장 위임이 조용히 건너뛰어진다.
        """
        return engine.remote_url(self._remote)

    def _record(self, stats: TransferStats) -> None:
        """계측 저장은 실패해도 본 작업의 성공을 뒤집지 않는다.

        clone은 성공한 뒤에야 저장소가 생기므로, 키를 구하지 못하면 조용히
        건너뛴다 — 실패한 clone에는 귀속시킬 저장소가 없다.
        """
        try:
            from gitclient.infrastructure.stats_store import StatsStore

            key = _repo_key(self._repo_path)
            if key is None:
                return  # 귀속시킬 저장소가 없다 (실패한 복제 등)
            StatsStore(StatsStore.default_path()).record(key, stats)
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
        credentials: Credentials | None = None,
    ) -> None:
        super().__init__(repo_path, remote, credentials=credentials)
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
        credentials: Credentials | None = None,
    ) -> None:
        super().__init__(repo_path, remote, credentials=credentials)
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


class CloneWorker(RemoteWorker):
    """원격을 복제한다.

    다른 워커와 두 가지가 다르다:
      - 시작할 때 **저장소가 없다.** 계측 키도 끝나야 정해진다.
      - 취소·실패 시 **부분 생성물을 우리가 치워야 한다.** git은 자기
        실패에서는 정리하지만, 프로세스를 강제 종료당하면 반쯤 만들어진
        디렉터리를 그대로 남긴다(실측 확인). 남겨두면 사용자는 정체 모를
        폴더를 마주하고, 같은 자리에 다시 clone하려 하면 "이미 존재한다"로
        막힌다.
    """

    label = "복제"

    def __init__(
        self,
        url: str,
        destination: str | Path,
        *,
        filter_spec: str | None = None,
        depth: int | None = None,
        credentials: Credentials | None = None,
    ) -> None:
        # 부모의 repo_path 자리에 대상 경로를 넣는다 — 계측 키는 성공 후에
        # 그 경로에서 구한다.
        super().__init__(destination, url, credentials=credentials)
        self._url = url
        self._destination = Path(destination)
        self._filter = filter_spec
        self._depth = depth
        self._started = False
        self._created_destination = not self._destination.exists()
        # clone은 클래스 메서드라 부모가 만든 엔진을 쓰지 않는다. 취소가
        # 실제 프로세스를 끊으려면 **실행 중인 그 엔진**을 붙잡아야 한다.
        self._clone_engine: RemoteEngine | None = None
        self._cleanup_allowed = self._decide_cleanup_policy()

    def _decide_cleanup_policy(self) -> bool:
        """대상을 정리해도 되는가 — **시작 전에** 한 번만 판단한다.

        규칙은 하나다: **우리가 넣은 것만 치운다.**

        처음에는 "사용자가 미리 만든 폴더면 내용만 비운다"고 했는데, 그 근거가
        정반대였다. git은 비어있지 않은 대상을 **거부하고 손도 대지 않으므로**,
        그 경우 안에 있는 것은 전부 사용자 것이다 — 우리가 만든 부분 생성물은
        하나도 없다. 그 상태에서 "정리"하면 논문이든 사진이든 통째로 지운다.
        실측으로 재현된 데이터 손실이었다.

        정리 시점이 아니라 **시작 시점**에 재는 것도 중요하다. 나중에 재면
        부분 생성물 때문에 항상 "비어있지 않음"이 되어 의도한 정리까지 막힌다.
        """
        try:
            if self._destination.is_symlink():
                # 링크를 따라가면 삭제가 대상 경로 **밖**에서 일어난다.
                # 사용자는 무엇이 지워졌는지 짐작조차 못 한다.
                return False
            if not self._destination.exists():
                return True  # 우리가 만들 것이다
            if not self._destination.is_dir():
                return False
            return not any(self._destination.iterdir())  # 비어 있을 때만
        except OSError:
            return False  # 읽을 수 없으면 손대지 않는다

    def _operate(self, engine: RemoteEngine) -> TransferStats:
        from gitclient.infrastructure.remote_engine import RemoteEngine as Engine

        clone_engine = Engine(
            self._destination.parent, credentials=self._credentials
        )
        with self._lock:
            cancelled = self._cancelled
            self._clone_engine = clone_engine
        if cancelled:
            raise GitClientError("복제가 취소되었습니다.")

        self._started = True
        return clone_engine.clone_with(
            self._url,
            self._destination,
            filter_spec=self._filter,
            depth=self._depth,
        )

    def cancel(self) -> None:
        super().cancel()
        # 부모는 자기가 만든 엔진만 끊는다. clone을 실제로 돌리는 것은
        # 이쪽이므로 함께 끊어야 프로세스가 죽는다.
        with self._lock:
            engine = self._clone_engine
        if engine is not None:
            engine.abort()

    def run(self) -> None:
        super().run()
        if self._succeeded or not self._cleanup_allowed:
            return
        if not self._started:
            # git을 아예 실행하지 않았다 — 치울 부분 생성물이 없다.
            # (대상이 우리가 만들지 않은 폴더라면 더더욱 손대면 안 된다.)
            if self._destination.exists() and self._is_our_empty_creation():
                self._clean_partial_clone()
            return
        if self._destination.exists() and not self._has_complete_repository():
            self._clean_partial_clone()

    def _is_our_empty_creation(self) -> bool:
        """시작도 못 했는데 남은 것이 있다면 그건 우리가 만든 빈 폴더뿐이다."""
        try:
            return self._destination.is_dir() and not any(
                self._destination.iterdir()
            )
        except OSError:
            return False

    def _has_complete_repository(self) -> bool:
        """객체는 다 받고 체크아웃만 실패한 경우인가.

        git은 그때 "Clone succeeded, but checkout failed"라 말하고 저장소를
        남긴다. 여기서 지우면 **이미 치른 전송을 통째로 버리는 것**이라
        목적함수에 정면으로 어긋난다. 사용자가 `git checkout`으로 마저
        복구할 수 있는 상태이므로 남긴다.
        """
        try:
            return (self._destination / ".git" / "HEAD").exists()
        except OSError:
            return False

    def _clean_partial_clone(self) -> None:
        """우리가 만든 부분 생성물을 치운다."""
        import shutil
        import stat as stat_module

        def force_writable(func, path, _exc):  # noqa: ANN001
            # git은 Windows에서 팩 파일을 읽기 전용으로 쓴다. 그대로 두면
            # rmtree가 조용히 실패하고, 같은 자리에 다시 복제할 수 없게 된다.
            try:
                os.chmod(path, stat_module.S_IWRITE)
                func(path)
            except OSError:
                pass

        def remove(path: Path) -> None:
            try:
                shutil.rmtree(path, onexc=force_writable)
            except (OSError, TypeError):
                # onexc는 3.12+. 실패해도 원래 오류를 덮지 않는다.
                shutil.rmtree(path, ignore_errors=True)

        if self._created_destination:
            remove(self._destination)
            return

        # 사용자가 미리 만들어 둔 폴더는 남긴다 — 그들이 만든 것이다.
        # 내용만 비운다. `_cleanup_allowed`가 **시작 시점에 비어 있었고
        # 링크가 아님**을 이미 보장하므로, 지금 안에 있는 것은 전부 우리가
        # 넣은 것이다. 그 보장 없이 이 순회를 돌면 사용자 파일을 지운다.
        try:
            for child in self._destination.iterdir():
                if child.is_dir() and not child.is_symlink():
                    remove(child)
                else:
                    try:
                        os.chmod(child, stat_module.S_IWRITE)
                    except OSError:
                        pass
                    child.unlink(missing_ok=True)
        except OSError:
            pass

    def _credential_url(self, engine: RemoteEngine) -> str | None:
        """복제는 원격 이름이 없다 — 사용자가 입력한 주소가 곧 키다."""
        return self._url

    @property
    def destination(self) -> Path:
        return self._destination


def merge_job(source_ref: str, branch: str) -> Callable[[Any], Any]:
    """WriteQueue에 제출할 병합 작업.

    빨리 감기와 달리 **충돌이 정상 결과**다. 예외로 던지지 않고 결과를
    돌려주므로, 호출부(UI)가 `job_succeeded`에서 충돌 여부를 보고 분기한다.

    `branch`를 함께 실어 보내는 이유는 `fast_forward_job`과 같다 — 큐에서
    실행될 때 사용자가 브랜치를 바꿨을 수 있다.
    """

    def work(engine: Any) -> Any:
        return engine.merge(source_ref, branch)

    return work


def abort_merge_job() -> Callable[[Any], None]:
    """진행 중인 병합을 되돌린다. 워킹 트리가 병합 이전으로 복구된다."""

    def work(engine: Any) -> None:
        engine.abort_merge()

    return work


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
