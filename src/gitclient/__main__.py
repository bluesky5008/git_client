"""애플리케이션 진입점.

사용법:
    python -m gitclient [저장소_경로]

경로를 주면 그 저장소를 열고, 주지 않으면 빈 창으로 시작한다.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from gitclient.ui.main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv

    # 패키징 스모크 테스트의 발판이다 — 창 없이 "실행 파일이 살아 있는가"에
    # 답할 유일한 경로. CI가 빌드 산출물에 이것을 묻는다 (§9).
    if "--version" in args[1:]:
        from importlib.metadata import PackageNotFoundError, version

        try:
            print(f"gitclient {version('gitclient')}")
        except PackageNotFoundError:
            print("gitclient (개발 트리)")
        return 0

    # 엔진이 남기는 진단(제외된 태그 등)이 보이도록 한다. (doc/design.md §7)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = QApplication(args)
    app.setApplicationName("Git Client")

    # 테마는 앱 전역이라 창을 만들기 전에 입힌다 (U4, §5.3). "system"은
    # 플랫폼 스타일이 OS 다크 모드를 스스로 따라가므로 손대지 않는다.
    from PySide6.QtCore import QSettings

    from gitclient.i18n import install as install_i18n, set_language
    from gitclient.ui.theme import apply_theme

    settings = QSettings("gitclient", "gitclient")
    apply_theme(app, str(settings.value("theme", "system")))
    # 언어는 창을 만들기 전에 정하고, 표시 이벤트 필터가 이후 모든 화면을
    # 자동으로 번역한다 (FR-15).
    set_language(str(settings.value("language", "system")))
    install_i18n(app)

    window = MainWindow()
    if len(args) > 1:
        window.open_repository(args[1])
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
