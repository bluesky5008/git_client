"""병합 UI 통합 테스트 (Phase 4 증분 1).

Phase 3까지 pull은 갈라진 상태를 만나면 "git CLI로 해결하라"며 멈췄다.
이 파일이 검증하는 것은 그 공백이 닫혔다는 것, 그리고 **충돌 상태가
사용자에게 보인다는 것**이다.

충돌을 조용히 넘기면 사용자는 워킹 트리에 마커가 든 줄도 모른 채 작업을
이어가고, 그 상태로 커밋하면 마커가 그대로 히스토리에 들어간다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

import gitclient.ui.main_window as module
from gitclient.ui.main_window import MainWindow
from tests.integration.remote_harness import AUTHOR_ENV, RemoteFixture, git

TIMEOUT = 60_000


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


@pytest.fixture
def remote(tmp_path: Path) -> RemoteFixture:
    return RemoteFixture(tmp_path / "src").build(commits=3, payload_kb=1)


@pytest.fixture
def window(qtbot, remote: RemoteFixture):  # noqa: ANN001, ANN201
    w = MainWindow()
    qtbot.addWidget(w)
    errors: list = []
    w._report = errors.append
    w.reported_errors = errors
    # 충돌은 오류가 아니라 정상 상태 전이라 별도 채널로 알린다 (ADR-38).
    notices: list = []
    w._notify = lambda title, message, **kw: notices.append(
        {"title": title, "message": message, **kw}
    )
    w.notices = notices
    w.open_repository(str(remote.work))
    qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
    return w


def settle(window, qtbot) -> None:  # noqa: ANN001
    qtbot.waitUntil(lambda: window._fetch_worker is None, timeout=TIMEOUT)
    qtbot.waitUntil(
        lambda: window._write_queue is None or not window._write_queue.is_busy,
        timeout=TIMEOUT,
    )


def make_conflicting_divergence(remote: RemoteFixture) -> None:
    """원격과 로컬이 같은 파일을 다르게 고친 상태."""
    (remote.seed / "f0.txt").write_text("원격이 고침\n", encoding="utf-8")
    commit_all(remote.seed, "원격 수정")
    remote.publish()
    (remote.work / "f0.txt").write_text("내가 고침\n", encoding="utf-8")
    commit_all(remote.work, "로컬 수정")


class TestPullCompletesTheMerge:
    def test_clean_divergence_produces_a_merge_commit(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        remote.diverge()

        window._on_pull()
        settle(window, qtbot)

        parents = git(
            "rev-list", "--parents", "-n", "1", "HEAD", cwd=remote.work
        ).stdout.split()
        assert len(parents) == 3
        assert window.reported_errors == [], [
            e.message for e in window.reported_errors
        ]

    def test_working_tree_is_clean_after_a_clean_merge(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        remote.diverge()

        window._on_pull()
        settle(window, qtbot)

        assert git("status", "--porcelain", cwd=remote.work).stdout.strip() == ""


class TestConflictIsSurfaced:
    def test_conflict_is_reported_to_the_user(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """조용히 넘어가면 마커가 든 채로 커밋된다."""
        make_conflicting_divergence(remote)

        window._on_pull()
        settle(window, qtbot)
        qtbot.waitUntil(lambda: bool(window.notices), timeout=TIMEOUT)

        notice = window.notices[-1]
        assert "충돌" in notice["message"]
        assert "f0.txt" in notice["detail"]
        assert notice["action"]
        assert window.reported_errors == [], "충돌을 오류로 보고했다"

    def test_abort_action_becomes_available(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """빠져나갈 길이 없으면 충돌을 보여주는 것만으로는 부족하다."""
        assert not window._abort_merge_action.isEnabled()
        make_conflicting_divergence(remote)

        window._on_pull()
        settle(window, qtbot)
        qtbot.waitUntil(
            lambda: window._abort_merge_action.isEnabled(), timeout=TIMEOUT
        )

    def test_conflicted_files_are_remembered(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        make_conflicting_divergence(remote)

        window._on_pull()
        settle(window, qtbot)
        qtbot.waitUntil(lambda: bool(window._merge_conflicts), timeout=TIMEOUT)

        assert [c.path for c in window._merge_conflicts] == ["f0.txt"]


class TestAbortFlow:
    def test_abort_restores_and_clears_state(
        self, window, qtbot, remote: RemoteFixture, monkeypatch  # noqa: ANN001
    ) -> None:
        make_conflicting_divergence(remote)
        mine = (remote.work / "f0.txt").read_text(encoding="utf-8")
        window._on_pull()
        settle(window, qtbot)
        qtbot.waitUntil(lambda: bool(window._merge_conflicts), timeout=TIMEOUT)

        monkeypatch.setattr(
            module.QMessageBox, "warning",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard),
        )
        window._on_abort_merge()
        settle(window, qtbot)

        assert (remote.work / "f0.txt").read_text(encoding="utf-8") == mine
        assert not (remote.work / ".git" / "MERGE_HEAD").exists()
        assert not window._abort_merge_action.isEnabled()

    def test_cancelling_the_confirmation_keeps_the_merge(
        self, window, qtbot, remote: RemoteFixture, monkeypatch  # noqa: ANN001
    ) -> None:
        """되돌릴 수 없는 작업이므로 실수로 눌러도 진행되면 안 된다."""
        make_conflicting_divergence(remote)
        window._on_pull()
        settle(window, qtbot)
        qtbot.waitUntil(lambda: bool(window._merge_conflicts), timeout=TIMEOUT)

        monkeypatch.setattr(
            module.QMessageBox, "warning",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel),
        )
        window._on_abort_merge()
        settle(window, qtbot)

        assert (remote.work / ".git" / "MERGE_HEAD").exists(), "취소했는데 중단됐다"
        assert window._abort_merge_action.isEnabled()


class TestMergeStateSurvivesReopen:
    def test_reopening_a_conflicted_repository_restores_the_abort_action(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """병합은 저장소에 남는다 — 메모리 상태만 믿으면 갇힌다."""
        make_conflicting_divergence(remote)
        window._on_pull()
        settle(window, qtbot)
        qtbot.waitUntil(lambda: bool(window._merge_conflicts), timeout=TIMEOUT)

        # 다른 저장소를 열었다가 돌아온다
        window.open_repository(str(remote.work))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        assert window._abort_merge_action.isEnabled()
        assert [c.path for c in window._merge_conflicts] == ["f0.txt"]

    def test_clean_repository_has_the_action_disabled(self, window) -> None:  # noqa: ANN001
        assert not window._abort_merge_action.isEnabled()
        assert window._merge_conflicts == ()


class TestNoDeadEnds:
    """리뷰에서 확정된 "빠져나갈 길이 없는" 경로들의 회귀 테스트."""

    def test_abort_survives_resolving_every_conflict(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """마지막 충돌을 스테이징하면 목록은 비지만 병합은 아직 진행 중이다.

        충돌 개수로 판단하면 바로 그 순간 중단 메뉴가 꺼져 사용자가 갇힌다.
        """
        make_conflicting_divergence(remote)
        window._on_pull()
        settle(window, qtbot)
        qtbot.waitUntil(lambda: bool(window._merge_conflicts), timeout=TIMEOUT)

        (remote.work / "f0.txt").write_text("직접 정리\n", encoding="utf-8")
        window._on_stage_requested("f0.txt")
        settle(window, qtbot)

        assert window._merge_conflicts == (), "전제가 깨졌다"
        assert window._abort_merge_action.isEnabled(), "빠져나갈 길이 사라졌다"

    def test_remote_actions_are_locked_during_a_merge(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """합칠 수 없는데 fetch를 열어두면 바이트만 쓴다 — 목적 함수 위반."""
        assert window._pull_action.isEnabled()
        make_conflicting_divergence(remote)

        window._on_pull()
        settle(window, qtbot)
        qtbot.waitUntil(lambda: window._merging, timeout=TIMEOUT)

        assert not window._pull_action.isEnabled()
        assert not window._fetch_action.isEnabled()
        assert not window._push_action.isEnabled()

    def test_remote_actions_return_after_abort(
        self, window, qtbot, remote: RemoteFixture, monkeypatch  # noqa: ANN001
    ) -> None:
        make_conflicting_divergence(remote)
        window._on_pull()
        settle(window, qtbot)
        qtbot.waitUntil(lambda: window._merging, timeout=TIMEOUT)

        monkeypatch.setattr(
            module.QMessageBox, "warning",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard),
        )
        window._on_abort_merge()
        settle(window, qtbot)
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        assert window._pull_action.isEnabled(), "중단했는데 잠긴 채로 남았다"
        assert not window._merging


class TestMarkerlessConflictIsExplained:
    def test_binary_conflict_warns_that_theirs_would_be_dropped(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        """마커가 없는 충돌에 "마커를 정리하라"고 하면 상대 변경이 버려진다."""
        (remote.seed / "img.bin").write_bytes(bytes(range(256)) * 8)
        commit_all(remote.seed, "바이너리 추가")
        remote.publish()
        git("pull", "--quiet", cwd=remote.work)
        (remote.seed / "img.bin").write_bytes(bytes(range(255, -1, -1)) * 8)
        commit_all(remote.seed, "원격이 고침")
        remote.publish()
        (remote.work / "img.bin").write_bytes(bytes(range(128)) * 16)
        commit_all(remote.work, "내가 고침")

        window._on_pull()
        settle(window, qtbot)
        qtbot.waitUntil(lambda: bool(window.notices), timeout=TIMEOUT)

        detail = window.notices[-1]["detail"]
        assert "img.bin" in detail
        assert "충돌 마커가 없습니다" in detail
        assert "버려집니다" in detail


class TestMergeEntryPoint:
    """pull 말고도 병합을 시작할 길이 있어야 한다.

    진입점이 pull 하나뿐이면 원격을 따라가지 않는 로컬 기능 브랜치는 앱
    안에서 영영 합칠 수 없다 — 가장 흔한 병합인데도.
    """

    def test_local_branch_offers_merge(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        from gitclient.domain.models import RefKind

        git("branch", "feature", cwd=remote.work)
        window.open_repository(str(remote.work))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        labels = [
            label for label, _ in window._ref_menu_entries(
                RefKind.LOCAL_BRANCH.value, "feature", False
            )
        ]

        assert any("합치기" in label for label in labels), labels

    def test_current_branch_offers_nothing_to_merge_into_itself(
        self, window  # noqa: ANN001
    ) -> None:
        from gitclient.domain.models import RefKind

        assert window._ref_menu_entries(
            RefKind.LOCAL_BRANCH.value, "main", True
        ) == []

    def test_merging_a_local_branch_creates_a_merge_commit(
        self, window, qtbot, remote: RemoteFixture  # noqa: ANN001
    ) -> None:
        git("checkout", "--quiet", "-b", "feature", cwd=remote.work)
        (remote.work / "feature.txt").write_text("기능\n", encoding="utf-8")
        commit_all(remote.work, "기능 작업")
        git("checkout", "--quiet", "main", cwd=remote.work)
        (remote.work / "main.txt").write_text("본선\n", encoding="utf-8")
        commit_all(remote.work, "본선 작업")
        window.open_repository(str(remote.work))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        window._start_merge("feature", is_local=True)
        settle(window, qtbot)

        parents = git(
            "rev-list", "--parents", "-n", "1", "HEAD", cwd=remote.work
        ).stdout.split()
        assert len(parents) == 3
        assert (remote.work / "feature.txt").exists()
        assert (remote.work / "main.txt").exists()
