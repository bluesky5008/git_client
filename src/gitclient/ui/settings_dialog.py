"""설정 화면 (U4, FR-14).

설정은 이미 QSettings에 흩어져 살고 있었다(프리페치 토글, repack 주기) —
이 화면은 새 저장소를 만드는 게 아니라 **있던 손잡이를 한곳에 모은다**
(ADR-12: 설정은 QSettings).

값의 적용은 호출자(MainWindow)의 몫이다: 테마·단축키는 앱 전역이라
다이얼로그가 직접 만지면 적용 경로가 두 갈래가 된다.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QKeySequenceEdit,
    QSpinBox,
    QVBoxLayout,
)

# (설정 키, 표시 이름, 기본 단축키). 단축키는 QSettings "shortcuts/<키>"로
# 덮어쓸 수 있다 (U5). Ctrl은 macOS에서 Cmd로 옮겨진다 (Qt 이식 표기).
SHORTCUT_SPECS: tuple[tuple[str, str, str], ...] = (
    ("search", "커밋 검색", "Ctrl+F"),
    ("fetch", "가져오기 (Fetch)", "Ctrl+Shift+F"),
    ("pull", "가져와 합치기 (Pull)", "Ctrl+Shift+L"),
    ("push", "올리기 (Push)", "Ctrl+Shift+P"),
    ("branch", "새 브랜치", "Ctrl+B"),
    ("reflog", "reflog 탐색", "Ctrl+Shift+R"),
)

_LANGUAGE_CHOICES = (
    ("system", "시스템 설정 따르기"),
    ("ko", "한국어"),
    ("en", "English"),
)

_THEME_CHOICES = (
    ("system", "시스템 설정 따르기"),
    ("light", "라이트 고정"),
    ("dark", "다크 고정"),
)


def shortcut_for(settings: QSettings, name: str) -> QKeySequence:
    default = next(d for key, _label, d in SHORTCUT_SPECS if key == name)
    return QKeySequence(
        str(settings.value(f"shortcuts/{name}", default))
    )


class SettingsDialog(QDialog):
    """읽고, 보여주고, 저장한다 — 적용은 하지 않는다."""

    def __init__(self, settings: QSettings, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("설정")
        self._settings = settings

        self._theme = QComboBox()
        for value, label in _THEME_CHOICES:
            self._theme.addItem(label, value)
        current = str(settings.value("theme", "system"))
        self._theme.setCurrentIndex(
            max(0, next(
                (i for i, (v, _l) in enumerate(_THEME_CHOICES) if v == current),
                0,
            ))
        )
        # 라이트/다크 강제는 Fusion 스타일로 바꾸므로 되돌리려면 재시작이
        # 필요하다 — 조용히 반만 적용하느니 미리 말한다.
        self._theme.setToolTip(
            "라이트/다크 고정은 즉시 적용됩니다. '시스템 설정 따르기'로 "
            "되돌릴 때는 앱을 다시 시작해야 플랫폼 스타일이 복원됩니다."
        )

        self._prefetch = QCheckBox("배경에서 미리 가져오기 (ADR-7)")
        self._prefetch.setChecked(
            settings.value("prefetch_enabled", True, type=bool)
        )
        self._repack_minutes = QSpinBox()
        self._repack_minutes.setRange(1, 240)
        self._repack_minutes.setSuffix("분")
        self._repack_minutes.setValue(
            settings.value("idle_repack_minutes", 10, type=int)
        )

        self._language = QComboBox()
        for value, label in _LANGUAGE_CHOICES:
            self._language.addItem(label, value)
        current_language = str(settings.value("language", "system"))
        self._language.setCurrentIndex(
            next(
                (i for i, (v, _l) in enumerate(_LANGUAGE_CHOICES)
                 if v == current_language),
                0,
            )
        )
        # 언어는 창을 만들 때 문구가 정해지므로 이미 떠 있는 화면에는
        # 반쯤만 적용된다 — 조용히 반만 바꾸느니 미리 말한다.
        self._language.setToolTip(
            "언어는 앱을 다시 시작해야 모든 화면에 적용됩니다."
        )

        general = QGroupBox("일반")
        form = QFormLayout(general)
        form.addRow("언어", self._language)
        form.addRow("테마", self._theme)
        form.addRow(self._prefetch)
        form.addRow("유휴 정리(repack)까지", self._repack_minutes)

        shortcuts = QGroupBox("단축키")
        shortcut_form = QFormLayout(shortcuts)
        self._shortcut_edits: dict[str, QKeySequenceEdit] = {}
        for name, label, _default in SHORTCUT_SPECS:
            edit = QKeySequenceEdit(shortcut_for(settings, name))
            self._shortcut_edits[name] = edit
            shortcut_form.addRow(label, edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(general)
        layout.addWidget(shortcuts)
        layout.addWidget(buttons)

    def accept(self) -> None:
        settings = self._settings
        settings.setValue("language", self._language.currentData())
        settings.setValue("theme", self._theme.currentData())
        settings.setValue("prefetch_enabled", self._prefetch.isChecked())
        settings.setValue("idle_repack_minutes", self._repack_minutes.value())
        for name, edit in self._shortcut_edits.items():
            sequence = edit.keySequence().toString()
            if sequence:
                settings.setValue(f"shortcuts/{name}", sequence)
            else:
                settings.remove(f"shortcuts/{name}")  # 비우면 기본값으로
        super().accept()
