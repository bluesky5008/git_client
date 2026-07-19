"""복제(clone) 다이얼로그.

**전송량 선택지를 여기서 정직하게 보여준다.** partial clone과 shallow는 초기
전송량을 크게 줄이지만 누적 전송량에서는 이득이 불확실하고(ADR-6), 오프라인
작업에 제약이 생긴다. 이 프로젝트의 목적함수가 누적 바이트이므로 기본값은
전체 복제이고, 다른 선택을 하면 **무엇을 잃는지 함께 적는다.**

이득만 강조하는 화면은 정직하지 않다 (performance.md §8.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True, slots=True)
class CloneRequest:
    url: str
    destination: Path
    filter_spec: str | None = None
    depth: int | None = None


# (표시 이름, filter_spec, depth, 잃는 것)
_MODES: tuple[tuple[str, str | None, int | None, str], ...] = (
    (
        "전체 복제 (권장)",
        None,
        None,
        "히스토리와 모든 파일을 받습니다. 오프라인에서도 과거 파일을 열 수 있습니다.",
    ),
    (
        "큰 파일 지연 (blob:limit=1m)",
        "blob:limit=1m",
        None,
        "1MB 초과 파일은 필요할 때 받습니다. "
        "오프라인에서 그런 파일의 과거 버전을 열 수 없습니다.",
    ),
    (
        "파일 내용 지연 (blob:none)",
        "blob:none",
        None,
        "히스토리 구조만 받습니다. 파일을 열 때마다 네트워크를 씁니다 — "
        "누적 전송량은 오히려 늘 수 있습니다.",
    ),
    (
        "최신 커밋만 (depth=1)",
        None,
        1,
        "히스토리가 잘립니다. blame·과거 비교·일부 병합이 제한되고, "
        "나중에 마저 받으려면 다시 전송해야 합니다.",
    ),
)


class CloneDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("저장소 복제")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._url = QLineEdit()
        self._url.setPlaceholderText("https://github.com/사용자/저장소.git")
        form.addRow("원격 주소", self._url)

        destination_row = QWidget()
        destination_layout = QHBoxLayout(destination_row)
        destination_layout.setContentsMargins(0, 0, 0, 0)
        self._destination = QLineEdit()
        self._destination.setPlaceholderText("복제할 위치")
        browse = QPushButton("찾아보기...")
        browse.clicked.connect(self._choose_directory)
        destination_layout.addWidget(self._destination)
        destination_layout.addWidget(browse)
        form.addRow("대상 폴더", destination_row)

        self._mode = QComboBox()
        for label, _filter, _depth, _cost in _MODES:
            self._mode.addItem(label)
        form.addRow("받을 범위", self._mode)
        layout.addLayout(form)

        self._cost = QLabel()
        self._cost.setWordWrap(True)
        self._cost.setStyleSheet("color: palette(mid);")
        layout.addWidget(self._cost)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText("복제")

        # 기본 상위 폴더. 상대 경로를 대상으로 넘기면 **앱의 프로세스 CWD**에
        # 복제된다 — 사용자가 고르지 않은 곳이다. 항상 절대 경로로 둔다.
        self._parent_directory = Path.home()

        self._url.textChanged.connect(self._on_url_changed)
        self._destination.textChanged.connect(self._sync_ok)
        self._mode.currentIndexChanged.connect(self._sync_cost)
        self._sync_cost()
        self._sync_ok()
        self._url.setFocus()

    # ------------------------------------------------------------------

    def _choose_directory(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "복제할 상위 폴더 선택")
        if not chosen:
            return
        # 고른 폴더는 **상위 폴더**다. 이후 URL을 입력해도 이 선택이
        # 유지되도록 따로 기억한다 — 대상 칸에서 역산하면 한 단계 위로
        # 벗어난다(고른 폴더가 곧 대상일 때 그 부모를 취하게 된다).
        self._parent_directory = Path(chosen)
        name = _repository_name(self._url.text())
        self._destination.setText(
            str(self._parent_directory / name) if name else chosen
        )
        self._sync_ok()

    def _on_url_changed(self) -> None:
        """주소를 넣으면 대상 폴더를 짐작해 채운다.

        사용자가 직접 고친 뒤에는 덮어쓰지 않는다 — 입력을 되돌리는 UI는
        신뢰를 잃는다.
        """
        if not self._destination.isModified():
            name = _repository_name(self._url.text())
            if name:
                self._destination.setText(str(self._parent_directory / name))
        self._sync_ok()

    def _sync_cost(self) -> None:
        self._cost.setText(_MODES[self._mode.currentIndex()][3])

    def _sync_ok(self) -> None:
        self._ok.setEnabled(
            bool(self._url.text().strip()) and bool(self._destination.text().strip())
        )

    def request(self) -> CloneRequest:
        _label, filter_spec, depth, _cost = _MODES[self._mode.currentIndex()]
        destination = Path(self._destination.text().strip()).expanduser()
        if not destination.is_absolute():
            # 상대 경로는 프로세스 CWD 기준이 된다. 사용자가 의도한 적 없는
            # 위치이므로 홈 아래로 붙인다.
            destination = self._parent_directory / destination
        return CloneRequest(
            url=self._url.text().strip(),
            destination=destination,
            filter_spec=filter_spec,
            depth=depth,
        )


def _repository_name(url: str) -> str | None:
    """원격 주소에서 폴더 이름을 짐작한다.

    `https://host/org/repo.git` → `repo`. SCP 형식(`git@host:org/repo.git`)도
    같은 방식으로 잘린다.
    """
    trimmed = url.strip().rstrip("/")
    if not trimmed:
        return None
    tail = trimmed.replace("\\", "/").rsplit("/", 1)[-1]
    if ":" in tail and "/" not in tail:  # git@host:repo.git
        tail = tail.rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail or None
