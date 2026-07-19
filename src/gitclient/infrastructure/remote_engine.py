"""git CLI 기반 원격 엔진.

네트워크 작업 전담이다. 로컬 읽기/쓰기는 pygit2가 맡는다
(doc/design.md §2.3 하이브리드 엔진, ADR-2).

CLI를 쓰는 이유는 전송량을 줄이는 수단이 CLI에만 있기 때문이다 —
protocol v2, 객체 필터, 협상 알고리즘 선택, credential helper.

**설정을 명령 단위로만 준다.** `git -c key=value` 형태로 넘기고 사용자의
전역/저장소 설정은 건드리지 않는다. 앱이 사용자 환경을 오염시키면
CLI로 돌아갔을 때 예상 밖의 동작을 하게 된다. (performance.md §2.1)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

from gitclient.domain.errors import (
    AuthenticationRequired,
    EngineError,
    GitClientError,
)
from gitclient.infrastructure.askpass import (
    Credentials,
    shim_environment,
    write_shim,
)
from gitclient.domain.instrumentation import (
    OperationKind,
    TransferStats,
    parse_progress,
    parse_trace2,
)

# 모든 네트워크 명령에 붙는 설정. 전부 전송 바이트를 줄이거나 측정하기 위한 것이다.
BASE_CONFIG: tuple[str, ...] = (
    # ref 광고량을 줄인다. git 2.26+ 의 기본값이지만 사용자 설정이 덮을 수
    # 있으므로 명령 단위로 강제한다. (performance.md §2.1)
    "protocol.version=2",
    # 압축 **하한선**이다. 최대로 올려서 이득을 보는 게 아니라, 사용자 설정이
    # 0으로 꺼두는 경우를 막는 것이 목적이다. (ADR-8 정정)
    #
    # 실측(파일 120개·커밋 40개 저장소의 push 전송 바이트):
    #   0 → 142,223 / 1 → 67,000 / 6 → 66,591 / 9 → 66,591
    # 0을 벗어나는 것이 2.1배 이득이고, 기본값(6) 위로는 **정확히 0.00%**다.
    # 소요시간도 차이가 없었다. 즉 "CPU를 주고 바이트를 산다"는 거래는 기본값
    # 위에서 성립하지 않는다 — 살 바이트가 없다.
    "core.compression=9",
    "pack.compression=9",
    # 받은 팩을 풀지 않고 보관한다. 두 가지 이득이 겹친다:
    #   1. `Receiving objects: ..., N KiB` 가 항상 나와 전송량을 측정할 수 있다
    #   2. 저장소가 팩된 상태를 유지한다 — 순회가 40배 빠르다 (§4.1.1.3)
    # 대가로 작은 팩이 쌓이므로 유휴 시 repack이 필요하다 (performance.md §6.4).
    "transfer.unpackLimit=1",
)

# 비대화형 강제. `measure=False`인 명령에도 붙어야 하므로 BASE_CONFIG와 분리한다.
#
# GIT_TERMINAL_PROMPT는 git 자체의 터미널 프롬프트만 끈다. credential helper는
# 그와 무관하게 실행되며, GCM 같은 헬퍼는 **GUI 대화상자를 띄우고 사용자가
# 응답할 때까지 git을 붙잡는다** — 워커 스레드가 무기한 정지한다.
# 실측: 401 응답 원격에 fetch → 기본 설정은 GUI 대기로 12초+ 무한, 이 설정을
# 주면 0.6초에 "terminal prompts disabled"로 실패한다.
# 저장된 자격증명을 반환하는 경로는 그대로 살아 있고 UI만 차단된다.
NONINTERACTIVE_CONFIG: tuple[str, ...] = ("credential.interactive=false",)

# 부모 환경에서 걷어낼 변수. 이들은 `-C <repo>`의 저장소 탐색을 이기거나
# 객체/참조의 위치를 바꾼다. 앱을 GIT_DIR이 export된 셸이나 git 훅
# (`git bisect run` 등) 아래에서 띄우면 UI가 연 저장소가 아니라 상속된 변수가
# 가리키는 저장소로 fetch가 나간다 — 예외도 경고도 없이. 계측은 워커가 들고
# 있는 경로에 기록되므로 누적 전송 바이트가 **오귀속**된다. 과소 집계보다
# 나쁘다: 집계표만 봐서는 탐지되지 않는다.
# 설정(GIT_CONFIG_*)은 `-c`가 이기므로 BASE_CONFIG는 대상이 아니다.
INHERITED_ENV_BLOCKLIST: frozenset[str] = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
    }
)

DEFAULT_TIMEOUT_S = 300

# 프로세스 트리를 죽인 뒤 파이프가 닫히기를 기다리는 상한.
# 이 상한이 있어야 timeout_s가 진짜 상한이 된다.
DRAIN_TIMEOUT_S = 5


_URL_IN_MESSAGE = re.compile(r"'((?:https?|ssh|git)://[^'\s]+)'")


def _without_userinfo(url: str) -> str:
    """URL에서 `user:pass@` 부분을 떼어낸다.

    자격증명 저장 키를 만들 때 쓴다. 원격 주소에 비밀번호를 박아둔 사용자가
    있는데, 그 값을 그대로 helper에 넘기면 비밀번호가 키의 일부가 되어
    다음 조회 때 맞지 않는다.
    """
    parsed = urlsplit(url)
    if not parsed.hostname:
        return url
    host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    return f"{parsed.scheme}://{host}{parsed.path}"


def _extract_url(stderr: str) -> str | None:
    """오류 메시지에 박힌 원격 주소.

    다이얼로그가 "무엇에 대한 로그인인가"를 보여주려면 필요하다. git은
    `could not read Username for 'https://example.com'` 형태로 준다.
    """
    match = _URL_IN_MESSAGE.search(stderr)
    return match.group(1) if match else None


def _with_stderr(error: GitClientError, stderr: str) -> GitClientError:
    """실패한 명령의 **원문** stderr를 예외에 붙인다.

    `detail`을 파싱하면 안 된다 — 사용자에게 보여주는 필드라 문구가 바뀔 수
    있고, 일부 경로에서는 "(출력 없음)"으로 대체된다. 계측이 파싱할 원문은
    따로 실어 보낸다.
    """
    error.git_stderr = stderr
    return error


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """git과 그 자손(git-remote-https, ssh, credential helper)을 함께 죽인다.

    직계 자식만 죽이면 안 되는 이유: 자손들이 stderr 파이프를 상속하므로
    git.exe만 죽여도 파이프가 닫히지 않는다. Windows의 `subprocess.run`은
    타임아웃 시 kill 직후 **timeout 없는** `communicate()`를 다시 호출하는데,
    그 read가 반환하지 않아 타임아웃이 상한 노릇을 못 한다.
    실측: `timeout_s=3`인데 손자 수명 40초를 그대로 기다렸고, 손자는 고아로
    남았다.
    """
    if os.name == "nt":
        # Job Object가 정석이지만 pywin32/ctypes를 끌어와야 한다.
        # taskkill /T는 Windows 기본 탑재이고 트리 종료에 충분하다.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
            timeout=DRAIN_TIMEOUT_S,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    proc.kill()  # 트리 종료가 빗나가도 직계 자식은 확실히 죽인다


class RemoteEngine:
    """저장소 하나의 원격 작업.

    호출 규약: 워커 스레드에서 실행할 것. 네트워크 작업은 초 단위로 길어진다.
    """

    def __init__(
        self,
        repo_path: str | Path,
        git_binary: str = "git",
        *,
        credentials: Credentials | None = None,
    ) -> None:
        self._repo_path = str(repo_path)
        self._git = git_binary
        # 사용자가 방금 입력한 자격증명. 저장하지 않고 이 인스턴스가 사는
        # 동안만 들고 있다가 자식 프로세스 환경으로 넘긴다. (ADR-3)
        self._credentials = credentials
        # 지금 작업 중인 원격 이름. 오류를 만들 때 "무엇에 대한 로그인인가"를
        # 서버 출력이 아니라 우리 설정에서 답하기 위해 둔다.
        self._active_remote: str | None = None
        # clone은 아직 저장소가 없어 `remote get-url`을 쓸 수 없다.
        # 로그인 대상 주소를 여기서 직접 들고 간다.
        self._clone_url: str | None = None
        # abort()는 UI 스레드에서, _run()은 워커 스레드에서 돈다.
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._aborted = False

    # ------------------------------------------------------------------
    # 공개 연산
    # ------------------------------------------------------------------

    def abort(self) -> None:
        """진행 중인 원격 명령을 끊는다. 다른 스레드에서 호출해도 된다.

        fetch는 원격 추적 참조만 갱신하므로 중간에 끊어도 저장소가 깨지지
        않고, 받은 객체는 다음 fetch에서 재사용된다.

        트리째 죽이는 이유는 `_kill_process_tree`에 적었다 — git만 죽이면
        파이프가 닫히지 않아 워커가 그대로 멈춘다.
        """
        with self._lock:
            self._aborted = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc)

    def list_remotes(self) -> list[str]:
        result = self._run(["remote"], measure=False)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def fetch(
        self,
        remote: str = "origin",
        *,
        prune: bool = True,
        tags: bool = False,
        refspecs: Sequence[str] = (),
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> TransferStats:
        """원격에서 변경을 가져온다. 계측 결과를 반환한다.

        태그는 기본으로 받지 않는다 — 태그가 수천 개인 저장소에서 매번
        전부 받으면 누적 전송량이 커진다. 태그 뷰를 열 때 따로 가져온다.
        (performance.md §2.2)
        """
        # 서브모듈 재귀를 명시적으로 끈다. git 기본값(fetch.recurseSubmodules=
        # on-demand)이면 같은 명령 안에서 서브모듈 fetch가 돌아 두 가지가 깨진다:
        #   1. 서브모듈의 ref 줄이 같은 stderr에 섞여 상위 저장소의 갱신 수에
        #      잘못 합산된다 (실측: 상위 1개 갱신인데 2개로 보고)
        #   2. 서브모듈 fetch에는 --progress가 전파되지 않아 받은 바이트가
        #      어디에도 기록되지 않는다 (실측: 70KB 전송이 329바이트로 기록)
        # 2번이 특히 나쁘다 — "측정 실패"가 아니라 "측정 성공, 값만 틀림"이라
        # 집계표에서 탐지되지 않는다. 계측의 귀속 단위는 저장소 하나여야 한다.
        # 서브모듈 지원은 각 서브모듈에 엔진을 따로 태워 별도 행으로 남긴다.
        args = ["fetch", "--progress", "--no-recurse-submodules"]
        if prune:
            args.append("--prune")
        args.append("--tags" if tags else "--no-tags")
        # 원격 이름과 refspec은 옵션이 아니다. git의 parse-options는 인자를
        # permute하므로, `-`로 시작하는 원격 이름(악성 .git/config가 심을 수
        # 있다)이 `--upload-pack=...` 같은 옵션으로 해석돼 **임의 명령이
        # 실행된다.** 실측으로 재현했고, `--`를 넣으면 git이 값을 URL/refspec
        # 으로만 읽어 자체 방어("strange hostname blocked")가 대신 걸린다.
        args.append("--")
        args.append(remote)
        args.extend(refspecs)

        self._active_remote = remote
        return self._run_measured(
            args, kind=OperationKind.FETCH, remote=remote, timeout_s=timeout_s
        )

    def push(
        self,
        remote: str = "origin",
        refspecs: Sequence[str] = (),
        *,
        set_upstream: bool = False,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> TransferStats:
        """로컬 커밋을 원격에 올린다. 계측 결과를 반환한다.

        `--force`는 제공하지 않는다. 강제 push는 남의 커밋을 지울 수 있어
        확인 절차가 필요한데, 그 UX는 아직 없다. 없는 편이 낫다.
        """
        # 서브모듈 재귀는 push에서도 막는다. ADR-20의 근거("계측의 귀속
        # 단위는 저장소 하나여야 한다")는 방향과 무관한데, 처음엔 결함이
        # fetch에서 발견돼 fetch에만 넣었다. push도 재귀하면 서브모듈이
        # 보낸 바이트가 상위 저장소 앞으로 기록된다.
        args = ["push", "--progress", "--no-recurse-submodules"]
        if set_upstream:
            args.append("--set-upstream")
        args.append("--")
        args.append(remote)
        args.extend(refspecs)

        self._active_remote = remote
        return self._run_measured(
            args, kind=OperationKind.PUSH, remote=remote, timeout_s=timeout_s
        )

    @classmethod
    def clone(
        cls,
        url: str,
        destination: str | Path,
        *,
        filter_spec: str | None = None,
        depth: int | None = None,
        credentials: Credentials | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> TransferStats:
        """원격을 복제한다. 계측 결과를 반환한다.

        다른 원격 작업과 달리 **시작할 때 저장소가 없다.** 그래서 인스턴스
        메서드가 아니라 클래스 메서드이고, git은 대상의 상위 디렉터리에서
        실행한다.

        `filter_spec`(`blob:none` 등)과 `depth`는 초기 전송량을 줄이지만
        누적 전송량에서는 이득이 불확실하다(ADR-6). 기본값은 둘 다 없음이며,
        고른 경우의 제약은 UI가 알린다.
        """
        target = Path(destination)
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)

        engine = cls(parent, credentials=credentials)
        return engine.clone_with(
            url, target, filter_spec=filter_spec, depth=depth, timeout_s=timeout_s
        )

    def clone_with(
        self,
        url: str,
        destination: str | Path,
        *,
        filter_spec: str | None = None,
        depth: int | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> TransferStats:
        """이 엔진 인스턴스로 복제한다.

        클래스 메서드와 갈라놓은 이유: 취소가 실제 프로세스를 끊으려면
        **실행 중인 엔진을 호출자가 붙잡고 있어야** 한다. 클래스 메서드가
        엔진을 안에서 만들어 버리면 워커가 그 인스턴스를 알 수 없다.
        """
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._active_remote = None
        self._clone_url = url
        args = ["clone", "--progress"]
        if filter_spec:
            args.append(f"--filter={filter_spec}")
        if depth is not None:
            args.extend(["--depth", str(depth)])
        # 원격 주소와 경로는 옵션이 아니다 (ADR: 인자 주입 방어).
        args.append("--")
        args.append(url)
        args.append(str(target))

        # 계측에 남는 원격 이름에서 자격증명을 걷어낸다. URL에 토큰을 박아둔
        # 사용자가 있는데, 그 값이 계측 DB에 평문으로 영구 저장되면 안 된다.
        return self._run_measured(
            args,
            kind=OperationKind.CLONE,
            remote=_without_userinfo(url),
            timeout_s=timeout_s,
        )

    def remote_url(self, remote: str) -> str | None:
        """원격의 주소. 자격증명을 어느 호스트에 묶을지 정하는 데 쓴다."""
        try:
            result = self._run(["remote", "get-url", "--", remote], measure=False)
        except GitClientError:
            return None
        return result.stdout.strip() or None

    def remember_credentials(self, url: str) -> bool:
        """방금 통한 자격증명을 git의 credential helper에 저장하도록 위임한다.

        **우리가 직접 저장하지 않는다** (ADR-3). `git credential approve`를
        태우면 사용자가 이미 설정해둔 helper(OS 키체인, GCM, ...)가 자기
        방식으로 보관한다. 보안 책임을 검증된 구현에 넘기는 것이 요점이고,
        덤으로 CLI에서도 같은 자격증명이 재사용된다.

        helper가 하나도 없으면 git은 조용히 아무 것도 하지 않는다 — 그건
        실패가 아니라 "저장할 곳이 없다"이므로 사용자를 방해하지 않는다.
        """
        credentials = self._credentials
        if credentials is None or not credentials.remember:
            return False

        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.hostname:
            return False

        # **키를 손으로 조립하지 않는다.** `url=`을 주면 git이 자기 규칙으로
        # protocol/host/path를 쪼갠다 — `credential.useHttpPath`처럼 우리가
        # 모르는 설정까지 반영된다. 직접 조립하면 git이 나중에 조회할 때 쓰는
        # 키와 어긋나, 저장은 됐는데 다음 번에 또 묻는 상태가 된다(실측 확인).
        #
        # URL에 사용자 이름이 박혀 있으면 git은 그것을 쓴다. 우리가 입력받은
        # 이름을 덧붙이면 엉뚱한 키로 저장된다.
        payload = [f"url={_without_userinfo(url)}"]
        if not parsed.username:
            payload.append(f"username={credentials.username}")
        else:
            payload.append(f"username={parsed.username}")
        payload += [f"password={credentials.password}", "", ""]
        try:
            self._run_input(["credential", "approve"], "\n".join(payload))
        except GitClientError:
            # 저장 실패가 방금 성공한 fetch/push를 뒤집으면 안 된다.
            logger.info("자격증명 저장 위임에 실패했습니다", exc_info=True)
            return False
        return True

    def ahead_behind(self, branch: str, upstream: str) -> tuple[int, int] | None:
        """(앞선 커밋 수, 뒤처진 커밋 수). 비교할 수 없으면 None.

        push/pull 버튼의 활성 여부와 안내 문구가 이 값에서 나온다.
        네트워크를 타지 않는 로컬 질의라 계측 대상이 아니다.
        """
        try:
            result = self._run(
                ["rev-list", "--left-right", "--count", f"{branch}...{upstream}"],
                measure=False,
            )
        except GitClientError:
            return None  # upstream이 없거나 아직 fetch하지 않았다
        parts = result.stdout.split()
        if len(parts) != 2:
            return None
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # 실행
    # ------------------------------------------------------------------

    def _run_measured(
        self,
        args: Sequence[str],
        *,
        kind: OperationKind,
        remote: str,
        timeout_s: int,
    ) -> TransferStats:
        """명령을 실행하고 stderr progress와 Trace2를 함께 해석한다."""
        with tempfile.TemporaryDirectory(
            prefix="gitclient-trace-",
            # 프로세스 트리를 끊은 직후 Windows가 trace.json 핸들을 늦게 놓는다.
            # 계측 부산물 정리 실패가 fetch를 실패시키면 안 된다.
            ignore_cleanup_errors=True,
        ) as trace_dir:
            trace_path = Path(trace_dir) / "trace.json"
            started = time.perf_counter()
            try:
                result = self._run(
                    args,
                    timeout_s=timeout_s,
                    extra_env={"GIT_TRACE2_EVENT": str(trace_path)},
                )
            except GitClientError as exc:
                # **실패해도 바이트는 이미 회선에 실렸다.** push는 호출 단위가
                # 아니라 ref 단위로 원자적이라, 여러 ref를 올리다 하나만
                # 거부되면 나머지는 실제로 전송되고 원격에 반영된다(exit 1).
                # 여기서 버리면 행 자체가 없어 "측정 실패"로도 잡히지 않는
                # 완전 무기록이 되고, 누적 전송 바이트가 조용히 줄어든다.
                exc.stats = self._build_stats(
                    kind=kind,
                    remote=remote,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    progress=parse_progress(getattr(exc, "git_stderr", "") or ""),
                    trace=self._read_trace(trace_path),
                    succeeded=False,
                )
                raise

            duration_ms = int((time.perf_counter() - started) * 1000)
            progress = parse_progress(result.stderr)
            trace = self._read_trace(trace_path)

        return self._build_stats(
            kind=kind,
            remote=remote,
            duration_ms=duration_ms,
            progress=progress,
            trace=trace,
            succeeded=True,
        )

    def _build_stats(
        self,
        *,
        kind: OperationKind,
        remote: str,
        duration_ms: int,
        progress,  # noqa: ANN001 - ProgressReport
        trace,  # noqa: ANN001 - TraceReport
        succeeded: bool,
    ) -> TransferStats:
        """파싱 결과를 계측 모델로 옮긴다.

        팩이 오가지 않았다면 그건 **측정 실패가 아니라 실제 0바이트다.**
        일상 사용에서 변경 없는 작업이 대다수이므로 이 둘을 섞으면
        measured_operations가 영영 낮게 나와 "측정 실패" 신호 자체가 상시
        오탐이 된다. 반대로 팩이 광고됐는데(total_objects > 0) 바이트가
        없으면 그건 진짜 측정 실패이므로 None을 유지한다.

        판별자가 두 가지인 이유: fetch는 팩이 없으면 `Total` 줄 자체가
        없고(None), push는 `Total 0 (delta 0)`을 내놓는다(0).

        **0 채우기는 성공했을 때만 한다.** 실패 경로에서는 "팩이 필요 없었다"와
        "팩을 보내기 전에 끊겼다"를 구분할 수 없으므로 None(측정 실패)이 맞다.
        """
        no_pack = progress.total_objects in (None, 0)

        # 방향이 다른 두 작업을 한 모델에 담는다. 해당 없는 방향은 None으로
        # 둔다 — 0으로 채우면 "측정된 0바이트"와 구분되지 않는다.
        if kind is OperationKind.PUSH:
            received_bytes = received_objects = None
            sent_bytes = progress.sent_bytes
            sent_objects = progress.sent_objects
            if succeeded and sent_bytes is None and no_pack:
                sent_bytes = sent_objects = 0
        else:
            sent_bytes = sent_objects = None
            received_bytes = progress.received_bytes
            received_objects = progress.received_objects
            if succeeded and received_bytes is None and no_pack:
                received_bytes = received_objects = 0

        return TransferStats(
            kind=kind,
            remote=remote,
            duration_ms=duration_ms,
            received_bytes=received_bytes,
            received_objects=received_objects,
            sent_bytes=sent_bytes,
            sent_objects=sent_objects,
            total_objects=progress.total_objects,
            reused_objects=progress.reused_objects,
            throughput_bytes_per_s=progress.throughput_bytes_per_s,
            negotiation_rounds=trace.negotiation_rounds,
            protocol_version=trace.protocol_version,
            ref_updates=tuple(progress.ref_updates),
            regions=tuple(trace.regions),
            succeeded=succeeded,
        )

    def _read_trace(self, trace_path: Path):  # noqa: ANN201
        """Trace2 파일을 읽는다. 계측 실패가 본 작업을 실패시키지 않는다."""
        from gitclient.domain.instrumentation import TraceReport

        try:
            if trace_path.exists():
                return parse_trace2(
                    trace_path.read_text(encoding="utf-8", errors="replace")
                )
        except OSError:
            pass
        return TraceReport()

    def _run_input(self, args: Sequence[str], payload: str) -> None:
        """stdin으로 값을 넘기는 짧은 git 명령.

        비밀번호를 **인자로 주지 않는 이유**: argv는 같은 머신의 다른
        프로세스에서 보인다. stdin은 그렇지 않다.
        """
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in INHERITED_ENV_BLOCKLIST
        }
        env["LC_ALL"] = "C"
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = subprocess.run(
                [self._git, "-C", self._repo_path, *args],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=DRAIN_TIMEOUT_S * 4,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise EngineError("git 실행에 실패했습니다.", detail=str(exc)) from exc
        if result.returncode != 0:
            raise EngineError(
                f"git {args[0]} 실패 (exit {result.returncode}).",
                detail=(result.stderr or "").strip(),
            )

    def _run(
        self,
        args: Sequence[str],
        *,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        measure: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [self._git, "-C", self._repo_path]
        if measure:
            for setting in BASE_CONFIG:
                command.extend(["-c", setting])
        for setting in NONINTERACTIVE_CONFIG:
            command.extend(["-c", setting])

        # "저장 안 함"을 골랐으면 helper 체인을 이 명령에 한해 비운다.
        # **우리가 approve를 부르지 않는 것만으로는 부족하다** — 인증이
        # 성공하면 git 자신이 helper의 store를 호출한다(실측 확인). 그러면
        # 체크박스를 꺼도 값이 저장되어, 사용자가 명시적으로 거부한 일이
        # 조용히 일어난다.
        #
        # 빈 값은 체인을 **재설정**하므로 조회도 함께 막히는데, 이 경우엔
        # 우리가 값을 직접 공급하므로 문제가 없다.
        if self._credentials is not None and not self._credentials.remember:
            command.extend(["-c", "credential.helper="])

        command.extend(args)

        env = {
            key: value
            for key, value in os.environ.items()
            if key not in INHERITED_ENV_BLOCKLIST
        }
        # 진행률과 오류 메시지를 파싱하므로 로케일을 고정한다. 사용자 로케일에
        # 따라 "Receiving objects"가 번역되면 계측이 통째로 실패한다.
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        # 대화형 입력을 막고 실패로 되돌린다. **프롬프트는 우리가 띄운다** —
        # git이나 credential helper가 자기 UI를 띄우면 워커가 무기한 멈추고,
        # 사용자는 앱과 무관해 보이는 창을 만난다. (§4.8)
        # 이 셋이 각각 다른 경로를 막는다. 하나라도 빠지면 워커가 멈춘다:
        env["GIT_TERMINAL_PROMPT"] = "0"
        # git prompt.c는 GIT_ASKPASS → core.askPass → SSH_ASKPASS를
        # GIT_TERMINAL_PROMPT 검사보다 **먼저** 시도한다. 빈 값을 넣으면
        # 첫 항목에서 걸려(`askpass && *askpass`가 거짓) 나머지 두 경로까지
        # 한 번에 무력화된다 — 401 원격으로 실측 확인했다.
        env["GIT_ASKPASS"] = ""
        # ssh는 GIT_TERMINAL_PROMPT를 보지 않고 프롬프트를 tty에서 직접 읽으므로
        # stdin 차단만으로는 부족하다. BatchMode가 실제 차단이다.
        # StrictHostKeyChecking은 건드리지 않는다 — accept-new로 낮추면 알 수
        # 없는 호스트 키를 조용히 받아들이게 되고, 그건 계측을 위해 살 값이
        # 아니다. 모르는 호스트에서는 매달리지 말고 실패하는 편이 낫다.
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")

        # 사용자가 방금 입력한 값이 있으면 그것만 shim으로 공급한다. 이때도
        # `credential.interactive=false`는 그대로 둔다 — helper는 **저장된**
        # 자격증명을 돌려주는 역할만 하고, 물어보는 것은 우리 몫이다.
        # (실측: interactive=false 여도 helper의 get은 정상 동작한다)
        shim_dir: tempfile.TemporaryDirectory[str] | None = None
        if self._credentials is not None:
            shim_dir = tempfile.TemporaryDirectory(prefix="gitclient-ask-")
            env.update(
                shim_environment(
                    write_shim(Path(shim_dir.name)), self._credentials
                )
            )

        if extra_env:
            env.update(extra_env)

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # 앱의 콘솔을 물려주지 않는다. capture_output은 stdout/stderr만
                # 파이프하고 stdin은 상속하므로, 자식이 부모의 tty를 읽으며 멈출
                # 수 있다.
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                # POSIX: 자손을 한 프로세스 그룹으로 묶어 killpg로 함께 죽인다.
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError as exc:
            if shim_dir is not None:
                shim_dir.cleanup()
            raise EngineError(
                "git을 찾을 수 없습니다.",
                detail=str(exc),
                action="git 2.40 이상을 설치하고 PATH에 등록해 주세요.",
            ) from exc

        with self._lock:
            aborted_before_start = self._aborted
            if not aborted_before_start:
                self._proc = proc

        if aborted_before_start:
            # abort()가 Popen보다 먼저 왔다 — _proc에 실리지 않았으므로
            # 여기서 직접 정리한다.
            _kill_process_tree(proc)
            proc.communicate()
            raise EngineError("원격 작업이 취소되었습니다.")

        try:
            with proc:
                try:
                    stdout, stderr = proc.communicate(timeout=timeout_s)
                except subprocess.TimeoutExpired as exc:
                    _kill_process_tree(proc)
                    try:
                        proc.communicate(timeout=DRAIN_TIMEOUT_S)
                    except subprocess.TimeoutExpired:
                        pass  # 파이프가 안 닫혀도 여기서 예외를 던지므로 무방
                    raise EngineError(
                        f"원격 작업이 {timeout_s}초 안에 끝나지 않았습니다.",
                        detail=str(exc),
                        action="네트워크 상태를 확인하거나 "
                        "잠시 후 다시 시도해 주세요.",
                    ) from exc
        finally:
            with self._lock:
                self._proc = None
            if shim_dir is not None:
                # shim에는 비밀번호가 없지만(환경변수 이름만) 남겨둘 이유도 없다.
                # 타임아웃으로 트리를 끊은 직후엔 Windows가 핸들을 늦게 놓을 수
                # 있으므로 정리 실패는 삼킨다 — 임시 파일 정리가 원격 작업을
                # 실패시키면 안 된다.
                try:
                    shim_dir.cleanup()
                except OSError:
                    logger.debug("askpass shim 정리 실패", exc_info=True)

        if self._aborted:
            # 취소로 죽은 프로세스의 0 아닌 종료코드를 사용자 오류로 번역하지
            # 않는다 — 워커의 취소 가드가 이 예외를 삼킨다.
            cancelled = EngineError(
                "원격 작업이 취소되었습니다.", detail=(stderr or "").strip()
            )
            cancelled.git_stderr = stderr or ""
            raise cancelled

        result = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
        if result.returncode != 0:
            raise self._translate_failure(command, result)
        return result

    def _translate_failure(
        self, command: Sequence[str], result: subprocess.CompletedProcess[str]
    ) -> GitClientError:
        """git의 exit code와 stderr를 도메인 예외로 옮긴다.

        원문은 그대로 보존한다 — git의 영문 메시지가 검색 가능한 1차 자료다.
        (doc/design.md §5.2 원칙 4)
        """
        stderr = (result.stderr or "").strip()
        lowered = stderr.lower()
        return _with_stderr(self._classify_failure(stderr, lowered, result), stderr)

    def _configured_url(self) -> str | None:
        """지금 작업 중인 원격의 설정된 주소. 알 수 없으면 None.

        clone은 저장소가 없어 설정을 읽을 수 없으므로 넘겨받은 주소를 쓴다.
        어느 쪽이든 **서버가 보낸 문구가 아니라 우리가 아는 값**이다.
        """
        if self._clone_url:
            return self._clone_url
        return self.remote_url(self._active_remote) if self._active_remote else None

    def _known_username(self, url: str | None) -> str | None:
        """다이얼로그에 미리 채워 줄 사용자 이름.

        방금 입력받은 값이 있으면 그것(거부돼서 다시 묻는 경우), 없으면
        원격 주소에 박혀 있는 사용자 이름.
        """
        if self._credentials is not None:
            return self._credentials.username
        if url:
            embedded = urlsplit(url).username
            if embedded:
                return embedded
        return None

    def _classify_failure(
        self, stderr: str, lowered: str, result: subprocess.CompletedProcess[str]
    ) -> GitClientError:

        if "could not read from remote repository" in lowered or (
            "does not appear to be a git repository" in lowered
        ):
            return EngineError(
                "원격 저장소에 연결할 수 없습니다.",
                detail=stderr,
                action="원격 주소가 맞는지, 접근 권한이 있는지 확인해 주세요.",
            )
        # 자격증명 관련 실패는 따로 낸다 — 이것만이 "사용자에게 물어보면
        # 해결되는" 실패다. 두 갈래를 구분하는 것도 중요하다: 아직 물어보지
        # 않은 것과, 물어봤는데 거부된 것. 후자에서 같은 값으로 재시도하면
        # 무한 반복이 된다.
        needs_credentials = (
            "could not read username" in lowered
            or "could not read password" in lowered
            or "terminal prompts disabled" in lowered
            or "authentication failed" in lowered
        )
        if needs_credentials:
            rejected = "authentication failed" in lowered
            # 주소는 **우리가 아는 것**을 먼저 쓴다. stderr에서 첫 URL을 뽑으면
            # 서버가 보낸 `remote:` 줄이 앞설 수 있어, 악의적인 원격이
            # 로그인 창에 다른 호스트를 띄우게 만들 수 있다.
            url = self._configured_url() or _extract_url(stderr)
            return AuthenticationRequired(
                "원격 저장소가 로그인을 요구합니다."
                if not rejected
                else "자격증명이 거부되었습니다.",
                url=url,
                username=self._known_username(url),
                rejected=rejected,
                detail=stderr,
                action="사용자 이름과 비밀번호(또는 액세스 토큰)를 입력해 주세요."
                if not rejected
                else "입력한 자격증명이 맞는지 확인해 주세요. "
                "GitHub 등에서는 비밀번호 대신 액세스 토큰이 필요합니다.",
            )
        if "couldn't find remote ref" in lowered:
            return EngineError(
                "원격에 해당 참조가 없습니다.",
                detail=stderr,
                action="브랜치 이름을 확인해 주세요.",
            )
        if "[rejected]" in lowered or "failed to push some refs" in lowered:
            # push에서 가장 흔한 실패다. git의 기본 안내는 "git pull 하세요"인데
            # 우리는 그 버튼을 갖고 있으므로 그쪽을 가리킨다.
            if "fetch first" in lowered or "non-fast-forward" in lowered:
                return EngineError(
                    "원격에 내가 갖고 있지 않은 커밋이 있어 밀어내지 못했습니다.",
                    detail=stderr,
                    action="먼저 '가져와 합치기(Pull)'로 원격 변경을 합친 뒤 "
                    "다시 시도해 주세요.",
                )
            return EngineError(
                "원격이 push를 거부했습니다.",
                detail=stderr,
                action="원격 저장소의 보호 규칙이나 권한을 확인해 주세요.",
            )
        if "protected branch" in lowered or "pre-receive hook declined" in lowered:
            return EngineError(
                "원격 저장소가 이 브랜치로의 push를 막고 있습니다.",
                detail=stderr,
                action="브랜치 보호 규칙을 확인하거나 다른 브랜치로 올려 주세요.",
            )

        return EngineError(
            f"원격 작업에 실패했습니다 (exit {result.returncode}).",
            detail=stderr or "(출력 없음)",
        )
