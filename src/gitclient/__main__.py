"""애플리케이션 진입점.

사용법:
    python -m gitclient [저장소_경로]

경로를 주면 그 저장소를 열고, 주지 않으면 빈 창으로 시작한다.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from gitclient.ui.main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv

    app = QApplication(args)
    app.setApplicationName("Git Client")

    window = MainWindow()
    if len(args) > 1:
        window.open_repository(args[1])
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
