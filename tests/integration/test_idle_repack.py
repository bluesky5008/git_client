"""유휴 repack — 사용자가 기다리지 않는 CPU만 쓴다 (FR-08, AC-04).

`unpackLimit=1`이 쌓는 팩을 델타 설정(ADR-35의 250/250 — push에서는
기각됐고 여기가 그 값을 쓸 유일한 자리다)으로 다시 싼다. 검증 대상은
둘이다: 정돈이 실제로 되는가, 그리고 **사용자 작업이 오면 즉시
물러나는가** — 배경 작업의 첫 번째 계약은 방해하지 않는 것이다.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.integration.remote_harness import AUTHOR_ENV, git

TIMEOUT = 30_000


def pack_files(repo: Path) -> list[Path]:
    pack_dir = repo / ".git" / "objects" / "pack"
    return sorted(pack_dir.glob("*.pack")) if pack_dir.exists() else []


@pytest.fixture
def multipack(tmp_path: Path) -> Path:
    """팩이 여러 개 쌓인 저장소 — unpackLimit=1 환경의 일상.

    `git repack -q` 반복은 Windows git에서 팩을 쌓지 않았다(첫 CI 실측 —
    한 개로 계속 합쳐졌다). 커밋 범위마다 `pack-objects`로 팩을 **직접**
    만들면 플랫폼·버전과 무관하게 결정적이다.
    """
    import subprocess

    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    previous: str | None = None
    for index in range(3):
        (root / "f.txt").write_text(("x" * 64 + "\n") * 200 + f"{index}\n")
        git("add", "-A", cwd=root)
        git(*AUTHOR_ENV, "commit", "--quiet", "-m", f"c{index}", cwd=root)
        head = git("rev-parse", "HEAD", cwd=root).stdout.strip()
        rev_range = head if previous is None else f"{previous}..{head}"
        result = subprocess.run(
            ["git", "pack-objects", "--revs", "-q",
             str(root / ".git" / "objects" / "pack" / "pack")],
            input=rev_range, text=True, capture_output=True, cwd=root,
        )
        assert result.returncode == 0, result.stderr
        previous = head
    git("prune-packed", cwd=root)
    assert len(pack_files(root)) >= 3, "전제가 깨졌다 — 팩이 쌓여 있어야 한다"
    return root


class TestEngineRepack:
    def test_packs_collapse_into_one(self, multipack: Path) -> None:
        done = LocalGitEngine.open(str(multipack)).idle_repack()

        assert done is True
        assert len(pack_files(multipack)) == 1, "팩이 하나로 정돈되지 않았다"
        # 정돈 뒤에도 히스토리는 온전하다.
        assert git("rev-parse", "HEAD", cwd=multipack).returncode == 0

    def test_abort_yields_quickly_and_leaves_the_repo_intact(
        self, multipack: Path
    ) -> None:
        """양보는 빨라야 하고(0.2초 폴링), 중단해도 저장소는 일관적이어야 한다."""
        before = pack_files(multipack)
        started = time.monotonic()

        done = LocalGitEngine.open(str(multipack)).idle_repack(
            should_abort=lambda: True
        )

        assert done is False
        assert time.monotonic() - started < 5, "양보가 느리다"
        assert git("rev-parse", "HEAD", cwd=multipack).returncode == 0
        # 원자 교체 전에 죽였으므로 기존 팩이 그대로거나, 이미 교체가
        # 끝났다면 정돈된 상태다 — 어느 쪽이든 깨진 중간은 없다.
        assert pack_files(multipack), f"팩이 사라졌다 (이전: {len(before)}개)"

    def test_bare_repository_is_skipped(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare.git"
        git("init", "--quiet", "--bare", "-b", "main", str(bare))
        assert LocalGitEngine.open(str(bare)).idle_repack() is False


class TestWindowSchedulesIt:
    @pytest.fixture
    def window(self, qtbot, multipack: Path):  # noqa: ANN001, ANN201
        from gitclient.ui.main_window import MainWindow

        w = MainWindow()
        qtbot.addWidget(w)
        w._report = lambda _e: None
        w.open_repository(str(multipack))
        qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
        w.repo = multipack
        return w

    def test_idle_triggers_a_repack_once(self, window, qtbot) -> None:  # noqa: ANN001
        window._last_user_activity = time.monotonic() - 3600  # 유휴 척

        window._maybe_idle_repack()

        assert window._idle_repack_done is True
        qtbot.waitUntil(
            lambda: not window._write_queue.is_busy, timeout=TIMEOUT
        )
        assert len(pack_files(window.repo)) == 1

        # 같은 유휴 구간에서는 다시 돌지 않는다.
        window._maybe_idle_repack()
        assert not window._write_queue.is_busy

    def test_user_work_arms_the_abort_and_rewinds_the_clock(
        self, window, qtbot  # noqa: ANN001
    ) -> None:
        """사용자 작업 제출이 유휴 시계를 되감고 배경 repack을 물린다."""
        window._last_user_activity = time.monotonic() - 3600
        window._maybe_idle_repack()
        abort = window._repack_abort
        assert abort is not None and not abort.is_set()

        window._submit_write("아무 쓰기", lambda engine: None)

        assert abort.is_set(), "사용자 작업이 배경 repack을 물리지 않았다"
        assert time.monotonic() - window._last_user_activity < 60
        assert window._idle_repack_done is False
        qtbot.waitUntil(
            lambda: not window._write_queue.is_busy, timeout=TIMEOUT
        )

    def test_not_idle_enough_means_no_repack(self, window) -> None:  # noqa: ANN001
        window._last_user_activity = time.monotonic()  # 방금 활동했다

        window._maybe_idle_repack()

        assert window._idle_repack_done is False
        assert not window._write_queue.is_busy
