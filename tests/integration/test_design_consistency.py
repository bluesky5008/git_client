"""설계 정합성 감사에서 확정된 결함의 회귀 테스트.

감사는 "문서가 말하는 것"과 "코드가 하는 것"을 대조했다. 대부분은 문서가
낡은 것이었지만, 아래 넷은 **구현이 설계 원칙을 어긴 것**이라 코드를 고쳤다.

이 테스트들이 지키는 것은 기능이 아니라 **원칙**이다 — 기능은 멀쩡히
동작하면서도 원칙을 어길 수 있고, 그때가 가장 발견하기 어렵다.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from gitclient.application import remote_workers
from gitclient.application.remote_workers import CloneWorker, _repo_key
from gitclient.infrastructure.local_engine import LocalGitEngine
from gitclient.infrastructure.remote_engine import RemoteEngine
from tests.integration.remote_harness import RemoteFixture, git

TIMEOUT = 60


@pytest.fixture
def remote(tmp_path: Path) -> RemoteFixture:
    return RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=1)


class TestEngineBoundary:
    """§2.3/ADR-2: 로컬 읽기는 pygit2, 네트워크만 git CLI.

    ahead/behind는 네트워크를 한 바이트도 쓰지 않는데 CLI subprocess로
    구현돼 있었다 — 실측 19ms 대 pygit2 0.002ms. UI 스레드에서 불리므로
    G4 예산(50ms)의 40%를 이유 없이 태우고 있었다.
    """

    def test_ahead_behind_lives_on_the_local_engine(
        self, remote: RemoteFixture
    ) -> None:
        engine = LocalGitEngine.open(str(remote.work))

        assert engine.ahead_behind("main", "refs/remotes/origin/main") == (0, 0)

    def test_ahead_behind_counts_divergence(self, remote: RemoteFixture) -> None:
        remote.diverge()
        RemoteEngine(remote.work).fetch(stall_timeout_s=TIMEOUT)

        engine = LocalGitEngine.open(str(remote.work))

        assert engine.ahead_behind("main", "refs/remotes/origin/main") == (1, 1)

    def test_unknown_refs_return_none(self, remote: RemoteFixture) -> None:
        engine = LocalGitEngine.open(str(remote.work))
        assert engine.ahead_behind("main", "refs/remotes/origin/nope") is None

    def test_ui_does_not_compute_divergence_itself(
        self, qtbot, remote: RemoteFixture, monkeypatch
    ) -> None:  # noqa: ANN001
        """벌어진 정도를 **UI 스레드가 세면 안 된다** (backlog §3.4).

        이 검사는 두 번 강해졌다. 처음엔 소스 문자열에 "RemoteEngine"이
        없는지만 봐서 `subprocess.run(["git","rev-list",...])` 인라인을
        놓쳤고, 그다음엔 subprocess를 봉인해 프로세스 기동을 막았다.
        이제는 계산 자체가 워커로 갔으므로 **엔진 질의까지 봉인**한다 —
        그래도 값이 나온다면 워커가 계산해 온 것을 쓰고 있다는 뜻이다.
        """
        import subprocess as subprocess_module

        from gitclient.ui.main_window import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        window._report = lambda _e: None
        window.open_repository(str(remote.work))
        qtbot.waitUntil(
            lambda: not window._loading and window._divergence is not None,
            timeout=30_000,
        )

        def forbidden(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("UI 스레드가 벌어진 정도를 직접 셌다")

        monkeypatch.setattr(subprocess_module, "run", forbidden)
        monkeypatch.setattr(subprocess_module, "Popen", forbidden)
        monkeypatch.setattr(LocalGitEngine, "ahead_behind", forbidden)

        assert window._divergence == (0, 0)
        assert window._describe_divergence() == "원격과 동기화됨"


class TestCredentialDelegationForClone:
    """ADR-3: 저장은 helper에 위임한다 — 복제에서도.

    CloneWorker는 부모의 `remote` 자리에 원격 **이름이 아니라 URL**을 넣는다.
    그래서 `git remote get-url <URL>`이 실패하고, 위임이 조용히 건너뛰어졌다.
    """

    def test_clone_resolves_its_credential_url(self, tmp_path: Path) -> None:
        worker = CloneWorker("https://example.com/o/r.git", tmp_path / "dst")

        # 엔진을 태우지 않고도 주소를 알아야 한다 — 원격 이름이 없기 때문이다.
        assert worker._credential_url(engine=None) == "https://example.com/o/r.git"

    def test_base_worker_still_resolves_by_remote_name(
        self, remote: RemoteFixture
    ) -> None:
        from gitclient.application.remote_workers import FetchWorker

        worker = FetchWorker(remote.work, "origin")
        engine = RemoteEngine(remote.work)

        assert worker._credential_url(engine) == remote.origin_uri


class TestInstrumentationAttribution:
    """ADR-26: 계측의 귀속 단위는 저장소 하나. 오귀속은 과소 집계보다 나쁘다."""

    def test_key_does_not_walk_up_to_the_enclosing_repository(
        self, remote: RemoteFixture, tmp_path: Path
    ) -> None:
        """기존 저장소 **안**의 경로는 그 저장소로 귀속되면 안 된다.

        저장소 안에 복제하려다 실패하면, 쓴 바이트가 감싸는 저장소 앞으로
        기록된다 — 집계표만 봐서는 탐지되지 않는 오귀속이다.
        """
        inside = Path(remote.work) / "vendor" / "libfoo"
        inside.mkdir(parents=True)

        assert _repo_key(str(inside)) is None

    def test_missing_path_does_not_crash(self, tmp_path: Path) -> None:
        """없는 경로에 discover_repository는 None을 준다 — Path(None)은 죽는다."""
        assert _repo_key(str(tmp_path / "nowhere")) is None

    def test_real_repository_still_resolves(self, remote: RemoteFixture) -> None:
        key = _repo_key(str(remote.work))

        assert key is not None
        assert key.endswith(".git")

    def test_record_falls_back_to_the_url_for_clones(
        self, tmp_path: Path, monkeypatch
    ) -> None:  # noqa: ANN001
        """저장소가 없어도 **주소가 있다** — 복제는 거기 귀속시킨다 (ADR-88).

        예전 판은 만들어 놓고 아무 데도 넘기지 않은 목록에 대고
        `assert recorded == []`를 했다 — 무조건 참이라 아무것도 지키지
        않았다 (감사 확정). 그 뒤 실검증으로 바꿨고, 이제는 결론이
        바뀌었다: 버리면 ADR-26이 막으려던 무기록이 된다.
        """
        from gitclient.infrastructure.stats_store import StatsStore

        stored: list = []
        monkeypatch.setattr(remote_workers, "_repo_key", lambda path: None)
        monkeypatch.setattr(
            StatsStore,
            "record",
            lambda self, key, stats: stored.append((key, stats)),
        )
        worker = CloneWorker("https://x/y.git", tmp_path / "dst")

        worker._record(object())  # 예외 없이 지나가야 한다

        assert [key for key, _stats in stored] == ["url:https://x/y.git"]


class TestSubmoduleRecursionIsOffEverywhere:
    """ADR-20의 근거는 방향과 무관하다 — push도 재귀하면 안 된다."""

    def test_push_disables_submodule_recursion(self, remote: RemoteFixture) -> None:
        captured: list[list[str]] = []
        engine = RemoteEngine(remote.work)
        original = engine._run_measured

        def spy(args, **kwargs):  # noqa: ANN001, ANN202
            captured.append(list(args))
            return original(args, **kwargs)

        engine._run_measured = spy  # type: ignore[method-assign]
        remote.commit_locally(1)
        engine.push(refspecs=["main"], stall_timeout_s=TIMEOUT)

        assert captured, "push가 실행되지 않았다"
        assert "--no-recurse-submodules" in captured[0]

    def test_fetch_still_disables_it(self, remote: RemoteFixture) -> None:
        captured: list[list[str]] = []
        engine = RemoteEngine(remote.work)
        original = engine._run_measured

        def spy(args, **kwargs):  # noqa: ANN001, ANN202
            captured.append(list(args))
            return original(args, **kwargs)

        engine._run_measured = spy  # type: ignore[method-assign]
        engine.fetch(stall_timeout_s=TIMEOUT)

        assert "--no-recurse-submodules" in captured[0]
