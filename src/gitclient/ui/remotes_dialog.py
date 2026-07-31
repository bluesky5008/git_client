"""원격 관리 (F1, Phase 3 증분 5).

저장소를 열어놓고 원격 주소 하나 못 바꿔 CLI로 나가야 했다 — 남아 있던
가장 큰 기능 공백이다.

쓰기는 하지 않는다 — 요청만 시그널로 내보내고, WriteQueue 제출과 확인
절차는 MainWindow의 몫이다 (ConflictPanel·ReflogDialog와 같은 분업).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)


class RemotesDialog(QDialog):
    add_requested = Signal(str, str)
    """(이름, 주소)."""
    remove_requested = Signal(str)
    url_change_requested = Signal(str, str)
    """(이름, 새 주소)."""

    def __init__(self, remotes, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("원격 관리")
        self.resize(560, 320)

        self._list = QListWidget()
        self.set_remotes(remotes)

        add_button = QPushButton("추가...")
        add_button.clicked.connect(self._on_add)
        self._url_button = QPushButton("주소 변경...")
        self._url_button.clicked.connect(self._on_change_url)
        self._remove_button = QPushButton("삭제...")
        self._remove_button.clicked.connect(self._on_remove)
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(add_button)
        buttons.addWidget(self._url_button)
        buttons.addWidget(self._remove_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)

        hint = QLabel(
            "원격 저장소 자체는 건드리지 않습니다 — 이 저장소가 어디를 "
            "바라보는지만 바꿉니다."
        )
        hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self._list, 1)
        layout.addWidget(hint)
        layout.addLayout(buttons)

        self._list.currentItemChanged.connect(lambda *_: self._update_buttons())
        self._update_buttons()

    def set_remotes(self, remotes) -> None:  # noqa: ANN001
        """목록을 갱신한다 — 쓰기가 끝난 뒤 MainWindow가 다시 부른다."""
        self._list.clear()
        for name, url in remotes:
            item = QListWidgetItem(f"{name}  →  {url}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setData(Qt.ItemDataRole.UserRole + 1, url)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)

    def _selected(self) -> tuple[str, str] | None:
        item = self._list.currentItem()
        if item is None:
            return None
        return (
            item.data(Qt.ItemDataRole.UserRole),
            item.data(Qt.ItemDataRole.UserRole + 1),
        )

    def _update_buttons(self) -> None:
        has_selection = self._selected() is not None
        self._url_button.setEnabled(has_selection)
        self._remove_button.setEnabled(has_selection)

    def _on_add(self) -> None:
        name, ok = QInputDialog.getText(self, "원격 추가", "원격 이름:", text="origin")
        name = name.strip()
        if not ok or not name:
            return
        url, ok = QInputDialog.getText(self, "원격 추가", f"'{name}'의 주소:")
        url = url.strip()
        if not ok or not url:
            return
        self.add_requested.emit(name, url)

    def _on_change_url(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        name, current = selected
        url, ok = QInputDialog.getText(
            self, "주소 변경", f"'{name}'의 새 주소:", text=current
        )
        url = url.strip()
        if not ok or not url or url == current:
            return
        self.url_change_requested.emit(name, url)

    def _on_remove(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        name, _url = selected
        # §5.2 원칙 2 — 무엇이 사라지는지 말한다. 원격 추적 참조가 함께
        # 지워지고, 로컬에만 있던 그 참조의 표시는 그래프에서 사라진다.
        answer = QMessageBox.warning(
            self,
            "원격 삭제",
            f"'{name}' 원격을 지웁니다.\n\n"
            "이 저장소의 원격 추적 참조(remotes/…)도 함께 지워집니다.\n"
            "원격 저장소 자체와 로컬 브랜치는 그대로 남습니다.",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Discard:
            self.remove_requested.emit(name)
