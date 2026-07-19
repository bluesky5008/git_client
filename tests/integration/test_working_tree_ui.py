"""작업 디렉터리 패널 통합 테스트 — 스테이징→커밋 로컬 개발 사이클.

Phase 2 완료 기준("이 클라이언트만으로 로컬 개발 사이클이 돌아간다")을
UI 수준에서 검증한다.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from gitclient.domain.errors import GitClientError
from gitclient.ui.main_window import MainWindow

SIGNATURE = pygit2.Signature("테스터", "tester@example.com", 1700000000, 540)
TIMEOUT = 10_000


@pytest.fixture
def repo(tmp_path: Path) -> pygit2.Repository:
    r = pygit2.init_repository(str(tmp_path / "wt"), initial_head="main")
    r.config["user.name"] = "테스터"
    r.config["user.email"] = "tester@example.com"
    wd = Path(r.workdir)
    (wd / "a.txt").write_text("one\n", encoding="utf-8")
    r.index.add_all()
    r.index.write()
    r.create_commit("HEAD", SIGNATURE, SIGNATURE, "init", r.index.write_tree(), [])
    return r


@pytest.fixture
def window(qtbot, repo: pygit2.Repository):  # noqa: ANN001, ANN201
    w = MainWindow()
    qtbot.addWidget(w)

    errors: list[GitClientError] = []
    w._report = errors.append
    w.reported_errors = errors

    w.open_repository(str(repo.workdir))
    qtbot.waitUntil(lambda: w._commit_model.rowCount() > 0, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
    return w


def wait_settled(window, qtbot) -> None:  # noqa: ANN001
    """쓰기 큐가 비고 로딩도 끝날 때까지 기다린다.

    `not window._loading` 만으로는 부족하다 — 쓰기가 아직 큐에 있는데도
    로딩 플래그는 내려가 있을 수 있어, 다음 조작이 이전 쓰기와 겹친다.
    """
    qtbot.waitUntil(
        lambda: (window._write_queue is None or not window._write_queue.is_busy)
        and not window._loading,
        timeout=TIMEOUT,
    )


def has_content(path: Path, expected: str) -> bool:
    """파일 내용이 기대값인가. 읽을 수 없으면 '아직 아니다'로 본다.

    `read_text`를 waitUntil 안에서 그대로 쓰면 안 된다. stash/checkout은
    워커 스레드에서 파일을 지웠다 다시 쓰므로, 폴링이 그 찰나에 걸리면
    FileNotFoundError가 waitUntil 밖으로 튀어나와 **대기가 아니라 실패**가
    된다. 실제로 간헐 실패의 원인이었다 — 제품 결함이 아니라 이 술어의
    결함이다.
    """
    try:
        return path.read_text(encoding="utf-8") == expected
    except OSError:
        return False


def unstaged_paths(window) -> list[str]:  # noqa: ANN001
    panel = window._work_panel
    return [
        panel._unstaged_list.item(i).text().split(maxsplit=1)[1]
        for i in range(panel._unstaged_list.count())
    ]


def staged_paths(window) -> list[str]:  # noqa: ANN001
    panel = window._work_panel
    return [
        panel._staged_list.item(i).text().split(maxsplit=1)[1]
        for i in range(panel._staged_list.count())
    ]


class TestStatusDisplay:
    def test_dirty_file_appears_in_unstaged(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        (Path(repo.workdir) / "a.txt").write_text("dirty\n", encoding="utf-8")
        window._refresh_status()
        qtbot.waitUntil(lambda: unstaged_paths(window) == ["a.txt"], timeout=TIMEOUT)

    def test_clean_tree_shows_empty_lists(self, window, qtbot) -> None:  # noqa: ANN001
        window._refresh_status()
        qtbot.wait(300)
        assert unstaged_paths(window) == []
        assert staged_paths(window) == []


class TestStagingCycle:
    def test_stage_moves_file_to_staged_list(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        (Path(repo.workdir) / "a.txt").write_text("dirty\n", encoding="utf-8")
        window._refresh_status()
        qtbot.waitUntil(lambda: unstaged_paths(window) == ["a.txt"], timeout=TIMEOUT)

        window._work_panel.stage_requested.emit("a.txt")
        qtbot.waitUntil(lambda: staged_paths(window) == ["a.txt"], timeout=TIMEOUT)
        qtbot.waitUntil(lambda: unstaged_paths(window) == [], timeout=TIMEOUT)

    def test_unstage_moves_file_back(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        (Path(repo.workdir) / "a.txt").write_text("dirty\n", encoding="utf-8")
        window._work_panel.stage_requested.emit("a.txt")
        qtbot.waitUntil(lambda: staged_paths(window) == ["a.txt"], timeout=TIMEOUT)

        window._work_panel.unstage_requested.emit("a.txt")
        qtbot.waitUntil(lambda: unstaged_paths(window) == ["a.txt"], timeout=TIMEOUT)
        assert staged_paths(window) == []

    def test_commit_lands_in_graph_and_clears_panel(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        (Path(repo.workdir) / "a.txt").write_text("committed!\n", encoding="utf-8")
        window._work_panel.stage_requested.emit("a.txt")
        qtbot.waitUntil(lambda: staged_paths(window) == ["a.txt"], timeout=TIMEOUT)

        window._work_panel._message_edit.setPlainText("UI에서 만든 커밋")
        window._work_panel.commit_requested.emit("UI에서 만든 커밋", False)

        # 커밋 → 그래프 재로딩 → 새 커밋이 맨 위에
        qtbot.waitUntil(
            lambda: window._commit_model.rowCount() == 2
            and window._commit_model.commit_at(0).summary == "UI에서 만든 커밋",
            timeout=TIMEOUT,
        )
        assert str(repo.head.target) == window._commit_model.commit_at(0).sha
        qtbot.waitUntil(
            lambda: window._work_panel.commit_message() == "", timeout=TIMEOUT
        )
        assert window.reported_errors == []

    def test_amend_rewrites_head_summary(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        window._work_panel.commit_requested.emit("고쳐 쓴 첫 커밋", True)
        qtbot.waitUntil(
            lambda: window._commit_model.rowCount() == 1
            and window._commit_model.commit_at(0).summary == "고쳐 쓴 첫 커밋",
            timeout=TIMEOUT,
        )

    def test_write_failure_is_reported_not_fatal(
        self, window, qtbot  # noqa: ANN001
    ) -> None:
        # 스테이징된 것 없이 커밋 → 엔진이 EngineError... 아니, 빈 메시지로 검증
        window._work_panel.commit_requested.emit("   ", False)
        qtbot.waitUntil(lambda: len(window.reported_errors) == 1, timeout=TIMEOUT)
        assert window.reported_errors[0].action is not None


class TestDiscard:
    def test_discard_confirmed_restores_file(
        self, window, qtbot, repo, monkeypatch  # noqa: ANN001
    ) -> None:
        target = Path(repo.workdir) / "a.txt"
        target.write_text("ruined\n", encoding="utf-8")
        monkeypatch.setattr(
            QMessageBox, "warning",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard),
        )
        window._on_discard_requested("a.txt")
        qtbot.waitUntil(
            lambda: has_content(target, "one\n"), timeout=TIMEOUT
        )

    def test_discard_cancelled_keeps_file(
        self, window, qtbot, repo, monkeypatch  # noqa: ANN001
    ) -> None:
        target = Path(repo.workdir) / "a.txt"
        target.write_text("keep me\n", encoding="utf-8")
        monkeypatch.setattr(
            QMessageBox, "warning",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel),
        )
        window._on_discard_requested("a.txt")
        qtbot.wait(300)
        assert target.read_text(encoding="utf-8") == "keep me\n"


class TestBranchAndStash:
    def test_checkout_via_write_queue_switches_head(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        window._submit_write(
            "브랜치 생성: feat",
            lambda engine: engine.create_branch("feat", checkout=True),
            reload_graph=True,
        )
        qtbot.waitUntil(lambda: repo.head.shorthand == "feat", timeout=TIMEOUT)
        # 그래프 재로딩 후 HEAD 표시가 feat로
        qtbot.waitUntil(
            lambda: window._info is not None
            and window._info.head_shorthand == "feat",
            timeout=TIMEOUT,
        )

    def test_stash_save_and_pop_roundtrip(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        target = Path(repo.workdir) / "a.txt"
        target.write_text("wip\n", encoding="utf-8")

        window._on_stash_save()
        qtbot.waitUntil(
            lambda: has_content(target, "one\n"), timeout=TIMEOUT
        )
        wait_settled(window, qtbot)

        window._on_stash_pop()
        qtbot.waitUntil(
            lambda: has_content(target, "wip\n"), timeout=TIMEOUT
        )
        assert window.reported_errors == []


class TestWidgetWiring:
    """실제 위젯 조작(버튼/더블클릭)이 시그널로 이어지는 배선 검증.

    리뷰 확정 공백: 기존 테스트는 시그널을 직접 emit해 패널 내부 배선이
    통째로 미검증이었다. 뮤테이션 테스트로 확인된 구멍이다.
    """

    def test_double_click_unstaged_item_stages_it(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        (Path(repo.workdir) / "a.txt").write_text("dirty\n", encoding="utf-8")
        window._refresh_status()
        qtbot.waitUntil(lambda: unstaged_paths(window) == ["a.txt"], timeout=TIMEOUT)

        panel = window._work_panel
        item = panel._unstaged_list.item(0)
        panel._unstaged_list.itemDoubleClicked.emit(item)  # Qt 더블클릭 경로
        qtbot.waitUntil(lambda: staged_paths(window) == ["a.txt"], timeout=TIMEOUT)

    def test_stage_button_click_stages_selected(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        (Path(repo.workdir) / "a.txt").write_text("dirty2\n", encoding="utf-8")
        window._refresh_status()
        qtbot.waitUntil(lambda: unstaged_paths(window) == ["a.txt"], timeout=TIMEOUT)

        panel = window._work_panel
        panel._unstaged_list.setCurrentRow(0)
        qtbot.mouseClick(panel._stage_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: staged_paths(window) == ["a.txt"], timeout=TIMEOUT)

    def test_commit_button_click_commits(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        (Path(repo.workdir) / "a.txt").write_text("버튼 커밋\n", encoding="utf-8")
        window._work_panel.stage_requested.emit("a.txt")
        qtbot.waitUntil(lambda: staged_paths(window) == ["a.txt"], timeout=TIMEOUT)

        panel = window._work_panel
        panel._message_edit.setPlainText("버튼으로 커밋")
        qtbot.waitUntil(lambda: panel._commit_button.isEnabled(), timeout=TIMEOUT)
        qtbot.mouseClick(panel._commit_button, Qt.MouseButton.LeftButton)

        qtbot.waitUntil(
            lambda: window._commit_model.rowCount() == 2
            and window._commit_model.commit_at(0).summary == "버튼으로 커밋",
            timeout=TIMEOUT,
        )

    def test_commit_button_disabled_without_message(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        (Path(repo.workdir) / "a.txt").write_text("x\n", encoding="utf-8")
        window._work_panel.stage_requested.emit("a.txt")
        qtbot.waitUntil(lambda: staged_paths(window) == ["a.txt"], timeout=TIMEOUT)
        assert not window._work_panel._commit_button.isEnabled()

    def test_rapid_double_commit_creates_single_commit(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        """리뷰 확정 결함의 회귀 테스트: 연타가 빈 중복 커밋을 만들면 안 된다."""
        (Path(repo.workdir) / "a.txt").write_text("연타\n", encoding="utf-8")
        window._work_panel.stage_requested.emit("a.txt")
        qtbot.waitUntil(lambda: staged_paths(window) == ["a.txt"], timeout=TIMEOUT)

        window._work_panel.commit_requested.emit("연타 커밋", False)
        window._work_panel.commit_requested.emit("연타 커밋", False)  # 즉시 재클릭

        qtbot.waitUntil(
            lambda: not window._loading
            and window._commit_model.rowCount() == 2,
            timeout=TIMEOUT,
        )
        qtbot.wait(300)  # 두 번째 커밋이 생겼다면 나타날 시간
        assert window._commit_model.rowCount() == 2
        summaries = [
            window._commit_model.commit_at(i).summary
            for i in range(window._commit_model.rowCount())
        ]
        assert summaries.count("연타 커밋") == 1
        assert window.reported_errors == []


class TestWriteQueueLifecycle:
    """리뷰 확정 결함의 회귀 테스트: 같은 저장소 재열기 시 큐를 재사용해야 한다."""

    def test_reopen_same_repo_reuses_queue(
        self, window, qtbot, repo  # noqa: ANN001
    ) -> None:
        first_queue = window._write_queue
        window.open_repository(str(repo.workdir))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
        assert window._write_queue is first_queue

    def test_switching_repo_replaces_queue(
        self, window, qtbot, tmp_path: Path  # noqa: ANN001
    ) -> None:
        other = pygit2.init_repository(str(tmp_path / "other"), initial_head="main")
        other.config["user.name"] = "t"
        other.config["user.email"] = "t@e.com"
        (Path(other.workdir) / "f.txt").write_text("x\n", encoding="utf-8")
        other.index.add_all()
        other.index.write()
        other.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "other", other.index.write_tree(), []
        )

        first_queue = window._write_queue
        window.open_repository(str(other.workdir))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
        assert window._write_queue is not first_queue
        # pygit2 workdir는 슬래시+후행 구분자 형태라 문자열 비교가 아닌
        # 경로 정규화 비교를 해야 한다.
        assert Path(window._write_queue.repo_path).resolve() == Path(
            other.workdir
        ).resolve()


class TestBareRepository:
    def test_write_ui_is_disabled(self, qtbot, tmp_path: Path) -> None:  # noqa: ANN001
        bare = pygit2.init_repository(str(tmp_path / "bare"), bare=True)
        del bare
        w = MainWindow()
        qtbot.addWidget(w)
        w._report = lambda e: None
        w.open_repository(str(tmp_path / "bare"))
        assert not w._work_panel._message_edit.isEnabled()
        assert w._write_queue is None
