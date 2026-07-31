"""Phase 5 다듬기 — 검색·전환·설정·테마·단축키·DnD (U3~U5, FR-13~15).

다국어는 이번 증분에서 제외됐다(별도 카탈로그 회차 필요 — record.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.remote_harness import AUTHOR_ENV, git

TIMEOUT = 15_000


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    for name in ("사과", "바나나", "당근"):
        (root / "f.txt").write_text(name, encoding="utf-8")
        commit_all(root, f"{name} 커밋")
    return root


@pytest.fixture
def window(qtbot, repo: Path):  # noqa: ANN001, ANN201
    from gitclient.ui.main_window import MainWindow

    w = MainWindow()
    qtbot.addWidget(w)
    w._report = lambda _e: None
    w.open_repository(str(repo))
    qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
    return w


class TestCommitSearch:
    """U3 — 필터가 아니라 점프다. 그래프는 전체 맥락이 있어야 읽힌다."""

    def test_jump_selects_the_matching_row(self, window) -> None:  # noqa: ANN001
        window._show_search()
        assert not window._search_bar.isHidden()
        window._search_edit.setText("바나나")

        assert window._find_commit() is True

        commit = window._commit_view.currentIndex().data(_commit_role())
        assert "바나나" in commit.message
        # 행은 숨겨지지 않았다 — 점프지 필터가 아니다.
        assert window._commit_model.rowCount() == 3

    def test_search_wraps_and_walks_both_ways(self, window) -> None:  # noqa: ANN001
        window._search_edit.setText("커밋")  # 셋 다 일치

        window._find_commit()
        first = window._commit_view.currentIndex().row()
        window._find_commit()
        second = window._commit_view.currentIndex().row()
        window._find_commit(backwards=True)

        assert second != first
        assert window._commit_view.currentIndex().row() == first

    def test_sha_prefix_matches(self, window, repo: Path) -> None:  # noqa: ANN001
        head = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        window._search_edit.setText(head[:7])

        assert window._find_commit() is True
        assert window._commit_view.currentIndex().data(_commit_role()).sha == head

    def test_no_match_reports_instead_of_moving(self, window) -> None:  # noqa: ANN001
        window._commit_view.selectRow(1)
        window._search_edit.setText("존재하지-않는-문자열")

        assert window._find_commit() is False
        assert window._commit_view.currentIndex().row() == 1


class TestRecentRepositories:
    """U3 — 최근 저장소 드롭다운."""

    def test_opened_repository_lands_in_the_combo(self, window, repo: Path) -> None:  # noqa: ANN001
        paths = [
            window._recent_combo.itemData(i)
            for i in range(window._recent_combo.count())
        ]
        assert str(repo) in paths

    def test_most_recent_comes_first_without_duplicates(
        self, window, repo: Path  # noqa: ANN001
    ) -> None:
        window._remember_repository("/tmp/other")
        window._remember_repository(str(repo))  # 재방문 — 중복 없이 맨 앞

        recents = window._recent_repositories()
        assert recents[0] == str(repo)
        assert recents.count(str(repo)) == 1


class TestThemeAndSettings:
    """U4 — 테마는 앱 전역, 적용 경로는 한 갈래."""

    def test_dark_theme_flips_the_palette(self, qtbot) -> None:  # noqa: ANN001
        from PySide6.QtGui import QPalette
        from PySide6.QtWidgets import QApplication

        from gitclient.ui.theme import apply_theme, relative_luminance

        app = QApplication.instance()
        original = app.palette()
        try:
            apply_theme(app, "dark")
            window_color = app.palette().color(QPalette.ColorRole.Window)
            assert relative_luminance(window_color) < 0.2, "다크가 아니다"
        finally:
            app.setPalette(original)

    def test_unknown_mode_is_treated_as_system(self, qtbot) -> None:  # noqa: ANN001
        from PySide6.QtWidgets import QApplication

        from gitclient.ui.theme import apply_theme

        app = QApplication.instance()
        before = app.palette()
        apply_theme(app, "이상한-값")  # system으로 접힌다 — 팔레트 불변
        assert app.palette() == before

    def test_dialog_round_trips_the_settings(self, qtbot, window) -> None:  # noqa: ANN001
        from gitclient.ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(window._settings, window)
        qtbot.addWidget(dialog)
        dialog._repack_minutes.setValue(42)
        dialog._prefetch.setChecked(False)

        dialog.accept()

        assert window._settings.value("idle_repack_minutes", type=int) == 42
        assert window._settings.value("prefetch_enabled", type=bool) is False
        # 되돌린다 — QSettings는 프로세스 밖에 남는다.
        window._settings.setValue("prefetch_enabled", True)
        window._settings.setValue("idle_repack_minutes", 10)


class TestShortcuts:
    """U5 — 단축키는 설정으로 덮어쓸 수 있고, 비우면 기본값이다."""

    def test_defaults_are_applied_to_actions(self, window) -> None:  # noqa: ANN001
        assert window._search_action.shortcut().toString() == "Ctrl+F"
        assert window._fetch_action.shortcut().toString() == "Ctrl+Shift+F"

    def test_override_wins_until_cleared(self, window) -> None:  # noqa: ANN001
        window._settings.setValue("shortcuts/fetch", "Ctrl+9")
        try:
            window._apply_shortcuts()
            assert window._fetch_action.shortcut().toString() == "Ctrl+9"
        finally:
            window._settings.remove("shortcuts/fetch")
            window._apply_shortcuts()
        assert window._fetch_action.shortcut().toString() == "Ctrl+Shift+F"


class TestBranchDrop:
    """U5 — 브랜치를 그래프에 떨어뜨리면 merge/rebase를 내민다."""

    def test_ref_list_mime_carries_the_ref(self, window, repo: Path, qtbot) -> None:  # noqa: ANN001
        git("branch", "feature", cwd=repo)
        window.open_repository(str(repo))
        qtbot.waitUntil(
            lambda: _ref_item(window, "feature") is not None, timeout=TIMEOUT
        )

        item = _ref_item(window, "feature")
        mime = window._ref_list.mimeData([item])

        from gitclient.ui.main_window import _REF_MIME

        payload = json.loads(bytes(mime.data(_REF_MIME)).decode("utf-8"))
        assert payload["shorthand"] == "feature"
        assert payload["is_head"] is False

    def test_drop_offers_merge_and_rebase_only(self, window) -> None:  # noqa: ANN001
        from gitclient.domain.models import RefKind

        entries = window._dnd_entries(
            {"shorthand": "feature", "kind": RefKind.LOCAL_BRANCH.value,
             "is_head": False}
        )

        labels = [label for label, _run in entries]
        assert any("합치기" in l for l in labels)
        assert any("리베이스" in l for l in labels)
        assert not any("삭제" in l or "전환" in l for l in labels)

    def test_head_and_tags_offer_nothing(self, window) -> None:  # noqa: ANN001
        from gitclient.domain.models import RefKind

        assert window._dnd_entries(
            {"shorthand": "main", "kind": RefKind.LOCAL_BRANCH.value,
             "is_head": True}
        ) == []
        assert window._dnd_entries(
            {"shorthand": "v1", "kind": RefKind.TAG.value, "is_head": False}
        ) == []


def _commit_role():
    from gitclient.viewmodel.commit_graph_model import CommitRole

    return CommitRole.COMMIT


def _ref_item(window, shorthand: str):  # noqa: ANN001
    from PySide6.QtCore import Qt

    for row in range(window._ref_list.count()):
        item = window._ref_list.item(row)
        if item.data(Qt.ItemDataRole.UserRole + 2) == shorthand:
            return item
    return None
