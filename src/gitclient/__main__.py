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

    from gitclient.ui.theme import apply_theme

    apply_theme(app, str(QSettings("gitclient", "gitclient").value("theme", "system")))

    window = MainWindow()
    if len(args) > 1:
        window.open_repository(args[1])
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
