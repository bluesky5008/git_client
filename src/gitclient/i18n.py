"""다국어 (FR-15, §5.3).

**원문이 곧 키다.** `tr("저장소 열기...")`처럼 한국어 원문을 그대로 쓰고
카탈로그가 그것을 영어로 바꾼다. `menu.file.open` 같은 키를 쓰면 코드가
읽히지 않고, 키와 문구가 어긋나도 아무도 모른다 — 원문 키는 그 두 문제를
한 번에 없앤다. 카탈로그에 없는 문자열은 **그대로 통과한다**(한국어가
원본 언어다).

**화면 크롬은 자동으로 번역된다.** `install()`이 창이 뜰 때 위젯 트리를
훑어 고정 문구(버튼·라벨·메뉴·제목·툴팁)를 카탈로그로 바꾼다. 350곳을
손으로 감싸는 diff 없이 지금 있는 화면과 앞으로 생길 다이얼로그가 모두
덮인다. **목록·트리의 항목 내용은 건드리지 않는다** — 거기 담기는 것은
브랜치 이름·커밋 요약 같은 사용자 데이터다.

오류·안내 문구는 표현 계층의 출구(`_report`/`_notify`)에서 번역한다 —
도메인·인프라 층은 한국어 원문 그대로 두고(§3.1 의존 방향), 화면에
닿는 순간에만 바뀐다.
"""

from __future__ import annotations

import locale
import logging
import os

logger = logging.getLogger(__name__)

LANGUAGES = ("system", "ko", "en")

_catalog: dict[str, str] = {}
_current = "ko"


def current_language() -> str:
    return _current


def set_language(code: str) -> None:
    """언어를 정한다. `system`은 OS 로케일을 따른다 (한국어면 ko, 그 외 en)."""
    global _catalog, _current
    if code not in LANGUAGES:
        code = "system"
    resolved = _resolve(code)
    _current = resolved
    if resolved == "ko":
        _catalog = {}  # 원본 언어 — 카탈로그가 필요 없다
        return
    try:
        from gitclient.locales.en import CATALOG

        _catalog = CATALOG
    except ImportError:  # pragma: no cover - 배포 누락 방어
        logger.warning("번역 카탈로그를 불러오지 못했습니다: %s", resolved)
        _catalog = {}


def _resolve(code: str) -> str:
    if code != "system":
        return code
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            return "ko" if value.lower().startswith("ko") else "en"
    try:
        language, _encoding = locale.getdefaultlocale()
    except ValueError:  # pragma: no cover - 이상한 로케일 문자열
        language = None
    return "ko" if (language or "ko").lower().startswith("ko") else "en"


def tr(text: str) -> str:
    """카탈로그에 있으면 번역, 없으면 원문 그대로."""
    return _catalog.get(text, text)


# 번역해도 안전한 위젯 — **고정 문구만 담는 것들**이다. 목록·트리 항목은
# 여기 없다: 거기엔 브랜치 이름과 커밋 요약이 들어간다.
_TEXT_SETTERS = ("text", "setText")
_TITLE_SETTERS = ("windowTitle", "setWindowTitle")


def retranslate(widget) -> None:  # noqa: ANN001 - QWidget
    """위젯 트리의 고정 문구를 현재 언어로 바꾼다.

    번역은 **카탈로그에 정확히 있는 문자열에만** 적용된다 — 모르는
    문자열은 손대지 않으므로 사용자 데이터가 바뀔 위험이 없다.
    """
    if not _catalog:
        return
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QAbstractButton,
        QGroupBox,
        QLabel,
        QLineEdit,
        QMenu,
        QWidget,
    )

    def swap(obj, getter: str, setter: str) -> None:  # noqa: ANN001
        try:
            value = getattr(obj, getter)()
        except (AttributeError, RuntimeError):
            return
        if isinstance(value, str) and value in _catalog:
            getattr(obj, setter)(_catalog[value])

    targets = [widget, *widget.findChildren(QWidget)]
    for target in targets:
        if isinstance(target, (QAbstractButton, QLabel, QGroupBox)):
            swap(target, "text", "setText")
        if isinstance(target, QLineEdit):
            swap(target, "placeholderText", "setPlaceholderText")
        swap(target, "windowTitle", "setWindowTitle")
        swap(target, "toolTip", "setToolTip")
    for menu in widget.findChildren(QMenu):
        swap(menu, "title", "setTitle")
    for action in widget.findChildren(QAction):
        swap(action, "text", "setText")
        swap(action, "toolTip", "setToolTip")


def install(app) -> None:  # noqa: ANN001 - QApplication
    """창이 뜰 때마다 자동으로 번역한다.

    다이얼로그마다 호출 지점을 두면 새로 만든 화면에서 빠뜨린다 —
    표시 이벤트 하나에 걸어두면 앞으로 생길 화면도 자동으로 덮인다.
    """
    from PySide6.QtCore import QEvent, QObject

    class _ShowTranslator(QObject):
        def eventFilter(self, obj, event) -> bool:  # noqa: ANN001, N802
            if event.type() == QEvent.Type.Show and hasattr(obj, "findChildren"):
                retranslate(obj)
            return False

    translator = _ShowTranslator(app)
    app.installEventFilter(translator)
    app._gitclient_translator = translator  # 수명 유지


def trf(template: str, **values: object) -> str:
    """값이 끼어드는 문구 — **템플릿이 키다** (FR-15 잔여 해소).

    `f"충돌 {n}개"`는 키가 실행 시점에 정해져 카탈로그로 찾을 수 없다.
    자리표시자를 남긴 템플릿을 키로 쓰면 번역할 수 있고, 언어마다 값의
    위치가 달라져도 포맷이 알아서 맞춘다.

    번역이 없으면 원문 템플릿으로 포맷한다 — 한국어가 원본 언어다.
    """
    return tr(template).format(**values)
