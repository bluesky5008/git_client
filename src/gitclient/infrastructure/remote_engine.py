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

import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from gitclient.domain.errors import EngineError, GitClientError
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
    # 압축은 회선 속도와 무관하게 최대로 — CPU가 남고 바이트가 비싼 환경이다.
    # (ADR-8) push의 팩 생성에 적용된다.
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

    def __init__(self, repo_path: str | Path, git_binary: str = "git") -> None:
        self._repo_path = str(repo_path)
        self._git = git_binary
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

        return self._run_measured(
            args, kind=OperationKind.FETCH, remote=remote, timeout_s=timeout_s
        )

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
            result = self._run(
                args,
                timeout_s=timeout_s,
                extra_env={"GIT_TRACE2_EVENT": str(trace_path)},
            )
            duration_ms = int((time.perf_counter() - started) * 1000)

            progress = parse_progress(result.stderr)
            trace = self._read_trace(trace_path)

        # 여기까지 왔으면 git은 성공했다 (_run이 0 아닌 종료를 예외로 올린다).
        # 원격이 팩을 광고하지도(`remote: Total ...`) 보내지도(`Receiving
        # objects`) 않았다면 받은 팩이 없다는 뜻이다 — **측정 실패가 아니라
        # 실제 0바이트다.** 일상 사용에서 변경 없는 fetch가 대다수이므로 이
        # 둘을 섞으면 measured_operations가 영영 낮게 나와 "측정 실패" 신호
        # 자체가 상시 오탐이 된다. 반대로 팩이 광고됐는데 바이트가 없으면
        # 그건 진짜 측정 실패이므로 None을 유지한다.
        received_bytes = progress.received_bytes
        received_objects = progress.received_objects
        if received_bytes is None and progress.total_objects is None:
            received_bytes = 0
            received_objects = 0

        return TransferStats(
            kind=kind,
            remote=remote,
            duration_ms=duration_ms,
            received_bytes=received_bytes,
            received_objects=received_objects,
            total_objects=progress.total_objects,
            reused_objects=progress.reused_objects,
            throughput_bytes_per_s=progress.throughput_bytes_per_s,
            negotiation_rounds=trace.negotiation_rounds,
            protocol_version=trace.protocol_version,
            ref_updates=tuple(progress.ref_updates),
            regions=tuple(trace.regions),
            succeeded=True,
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
        # 대화형 입력을 막고 실패로 되돌린다 — 인증 UI는 Phase 3 후속 작업이다.
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

        if self._aborted:
            # 취소로 죽은 프로세스의 0 아닌 종료코드를 사용자 오류로 번역하지
            # 않는다 — 워커의 취소 가드가 이 예외를 삼킨다.
            raise EngineError(
                "원격 작업이 취소되었습니다.", detail=(stderr or "").strip()
            )

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

        if "could not read from remote repository" in lowered or (
            "does not appear to be a git repository" in lowered
        ):
            return EngineError(
                "원격 저장소에 연결할 수 없습니다.",
                detail=stderr,
                action="원격 주소가 맞는지, 접근 권한이 있는지 확인해 주세요.",
            )
        if "authentication failed" in lowered or "terminal prompts disabled" in lowered:
            return EngineError(
                "원격 저장소 인증에 실패했습니다.",
                detail=stderr,
                action="자격증명을 확인해 주세요. "
                "git credential helper 설정이 필요할 수 있습니다.",
            )
        if "couldn't find remote ref" in lowered:
            return EngineError(
                "원격에 해당 참조가 없습니다.",
                detail=stderr,
                action="브랜치 이름을 확인해 주세요.",
            )

        return EngineError(
            f"원격 작업에 실패했습니다 (exit {result.returncode}).",
            detail=stderr or "(출력 없음)",
        )
