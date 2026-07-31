"""다국어 (FR-15) — 카탈로그를 정직하게 유지하는 장치.

**"반쯤 한 i18n"이 숨을 수 없어야 한다**는 것이 이 파일의 목적이다.
번역은 두 갈래로 적용된다(창 크롬 자동 번역 + 오류 출구 번역)라, 어느
쪽이든 문구가 늘었는데 카탈로그가 안 늘면 영어 화면에 한국어가 섞인다 —
그 순간을 테스트가 잡는다. 남아 있는 공백(값이 끼어드는 문구)도 숫자로
고정해 두어, 줄어들면 갱신하고 늘어나면 실패한다.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from gitclient.i18n import current_language, retranslate, set_language, tr
from gitclient.locales.en import CATALOG

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "gitclient"

CHROME_SETTERS = {
    "setText", "setWindowTitle", "setToolTip", "setPlaceholderText",
    "setHeaderLabels", "addAction", "addMenu", "addToolBar",
    # 폼 라벨·콤보 항목·단위·안내문도 화면에 그대로 나간다.
    "addRow", "addItem", "setSuffix", "setPlainText",
    # 표현 계층이 직접 감싼 것 (오류 출구 등)
    "tr",
}
CHROME_CTORS = {
    "QAction", "QPushButton", "QLabel", "QCheckBox", "QGroupBox",
    "QRadioButton", "QMenu", "QDockWidget",
}
ERROR_CTORS = {
    "EngineError", "GitClientError", "AuthenticationRequired",
    "_notify", "_report",
}


def _korean(value: str) -> bool:
    return any("가" <= ch <= "힣" for ch in value)


def _literals(paths, names) -> set[str]:  # noqa: ANN001
    """지정한 호출의 인자로 직접 실린 한국어 리터럴."""
    found: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(
                func, "id", None
            )
            if name not in names:
                continue
            arguments = list(node.args) + [
                keyword.value
                for keyword in node.keywords
                if keyword.arg in {"action", "detail"}
            ]
            for argument in arguments:
                if isinstance(argument, ast.List):
                    items = list(argument.elts)
                elif isinstance(argument, ast.IfExp):
                    # `action=A if 조건 else B` — 양쪽 다 화면에 나간다.
                    items = [argument.body, argument.orelse]
                else:
                    items = [argument]
                for item in items:
                    if (
                        isinstance(item, ast.Constant)
                        and isinstance(item.value, str)
                        and _korean(item.value)
                    ):
                        found.add(item.value)
    return found


def chrome_strings() -> set[str]:
    return _literals(
        sorted((SRC / "ui").rglob("*.py")), CHROME_SETTERS | CHROME_CTORS
    )


def error_strings() -> set[str]:
    return _literals(sorted(SRC.rglob("*.py")), ERROR_CTORS)


@pytest.fixture(autouse=True)
def _restore_language():  # noqa: ANN202
    before = current_language()
    yield
    set_language(before)


class TestLookup:
    def test_unknown_strings_pass_through(self) -> None:
        """카탈로그에 없으면 원문 그대로 — 한국어가 원본 언어다."""
        set_language("en")
        assert tr("이 문구는 카탈로그에 없다") == "이 문구는 카탈로그에 없다"

    def test_korean_needs_no_catalog(self) -> None:
        set_language("ko")
        assert tr("저장소 열기...") == "저장소 열기..."

    def test_english_translates(self) -> None:
        set_language("en")
        assert tr("저장소 열기...") == "Open Repository..."

    def test_system_resolves_from_locale(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("LC_ALL", "ko_KR.UTF-8")
        set_language("system")
        assert current_language() == "ko"
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        set_language("system")
        assert current_language() == "en"


class TestCatalogHonesty:
    """카탈로그가 코드와 어긋나면 여기서 붉어진다."""

    def test_every_chrome_string_is_translated(self) -> None:
        missing = sorted(chrome_strings() - set(CATALOG))
        assert not missing, (
            "화면 문구가 영어 카탈로그에 없다 — 영어 화면에 한국어가 섞인다:\n"
            + "\n".join(f"  {s!r}" for s in missing)
        )

    def test_every_static_error_string_is_translated(self) -> None:
        missing = sorted(error_strings() - set(CATALOG))
        assert not missing, (
            "오류·안내 문구가 영어 카탈로그에 없다:\n"
            + "\n".join(f"  {s!r}" for s in missing)
        )

    def test_no_stale_entries(self) -> None:
        """지워진 문구의 번역이 남아 있으면 다음 사람이 근거로 삼는다."""
        used = chrome_strings() | error_strings()
        stale = sorted(set(CATALOG) - used)
        assert not stale, (
            "코드에 없는 문구의 번역이 남아 있다:\n"
            + "\n".join(f"  {s!r}" for s in stale)
        )

    def test_translations_are_not_the_source(self) -> None:
        untranslated = [k for k, v in CATALOG.items() if k == v]
        assert not untranslated, f"번역이 원문 그대로다: {untranslated}"


class TestKnownGap:
    """**값이 끼어드는 문구는 아직 번역되지 않는다** (backlog §5).

    `f"충돌 {n}개를 해결해야 합니다."`처럼 값을 품은 문구는 원문 키가
    실행 시점에 정해져 카탈로그로 찾을 수 없다. 템플릿으로 바꾸는 회차가
    따로 필요하다 — 그때까지 **공백의 크기를 숫자로 고정해** 조용히
    커지지 못하게 한다.
    """

    LIMIT = 70

    def test_interpolated_strings_do_not_grow(self) -> None:
        count = 0
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(
                    func, "id", None
                )
                if name not in ERROR_CTORS | CHROME_SETTERS | CHROME_CTORS:
                    continue
                for argument in node.args:
                    if isinstance(argument, ast.JoinedStr) and any(
                        isinstance(v, ast.Constant)
                        and isinstance(v.value, str)
                        and _korean(v.value)
                        for v in argument.values
                    ):
                        count += 1
        assert count <= self.LIMIT, (
            f"번역되지 않는 보간 문구가 {count}개로 늘었다 (상한 {self.LIMIT}) — "
            "새 문구는 템플릿+포맷으로 쓰거나 이 상한과 함께 근거를 갱신할 것"
        )


class TestRetranslateIsSafe:
    def test_only_catalog_strings_change(self, qtbot) -> None:  # noqa: ANN001
        from PySide6.QtWidgets import QLabel, QPushButton, QWidget

        set_language("en")
        root = QWidget()
        qtbot.addWidget(root)
        known = QPushButton("닫기", root)
        user_data = QLabel("feature/내-브랜치", root)

        retranslate(root)

        assert known.text() == "Close"
        assert user_data.text() == "feature/내-브랜치", "사용자 데이터가 바뀌었다"

    def test_korean_mode_leaves_everything_alone(self, qtbot) -> None:  # noqa: ANN001
        from PySide6.QtWidgets import QPushButton, QWidget

        set_language("ko")
        root = QWidget()
        qtbot.addWidget(root)
        button = QPushButton("닫기", root)

        retranslate(root)

        assert button.text() == "닫기"
