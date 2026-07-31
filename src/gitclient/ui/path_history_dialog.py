"""파일 히스토리 다이얼로그 (ADR-90).

한 경로를 바꾼 커밋 목록(위)과, 고른 커밋에서 그 파일이 어떻게 바뀌었나
(아래 diff). 목록은 PathHistoryLoader가, diff는 DiffLoader의 경로 좁히기
가 워커에서 읽어 온다 — 이 창은 그리기만 한다 (G4).

이름 변경(rename)은 따라가지 않는다 — 개명 시점에서 목록이 끊긴다.
그 한계를 화면이 직접 말한다 (FR-04: 조용한 공백은 "기록이 없다"로
읽힌다).
"""

from __future__ import annotations

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QLabel,
    QListView,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from gitclient.application.diff_loader import DiffLoader
from gitclient.application.path_history_loader import PathHistoryLoader
from gitclient.i18n import tr, trf
from gitclient.ui.delegates import DiffDelegate
from gitclient.ui.late_delivery import drops_late_deliveries
from gitclient.viewmodel.diff_model import DiffModel


class PathHistoryDialog(QDialog):
    def __init__(self, repo_path: str, path: str, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self._repo_path = repo_path
        self._path = path
        self._pool = QThreadPool.globalInstance()
        # 세대 토큰 — 파일 하나짜리 창이지만 diff 요청은 클릭마다 겹칠
        # 수 있다 (DiffLoader와 같은 순서 역전 문제).
        self._generation = 0
        self._loaders: dict[int, object] = {}

        self.setWindowTitle(trf("파일 히스토리 — {path}", path=path))
        self.resize(760, 560)

        self._list = QTreeWidget()
        self._list.setHeaderLabels(
            [tr("요약"), tr("작성자"), tr("날짜"), tr("커밋")]
        )
        self._list.setRootIsDecorated(False)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.itemSelectionChanged.connect(self._on_commit_selected)

        self._diff_model = DiffModel(self)
        diff_view = QListView()
        diff_view.setModel(self._diff_model)
        diff_view.setItemDelegate(DiffDelegate(diff_view))
        diff_view.setUniformItemSizes(True)
        diff_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._list)
        splitter.addWidget(diff_view)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        self._status = QLabel(tr("기록을 읽는 중…"))
        note = QLabel(tr("이름이 바뀐 파일은 바뀐 시점까지만 보입니다."))

        layout = QVBoxLayout(self)
        layout.addWidget(self._status)
        layout.addWidget(splitter, stretch=1)
        layout.addWidget(note)

        self._start_history()

    # ------------------------------------------------------------------
    # 로딩

    def _start_history(self) -> None:
        self._generation += 1
        token = self._generation
        loader = PathHistoryLoader(self._repo_path, self._path, token)
        loader.signals.ready.connect(self._on_history_ready)
        loader.signals.failed.connect(self._on_failed)
        self._loaders[token] = loader
        self._pool.start(loader)

    @drops_late_deliveries
    def _on_history_ready(self, token: int, path: str, entries: list) -> None:
        self._loaders.pop(token, None)
        if token != self._generation:
            return
        if not entries:
            self._status.setText(tr("이 경로를 바꾼 커밋이 없습니다."))
            return
        self._status.setText(trf("커밋 {count}개", count=len(entries)))
        for entry in entries:
            item = QTreeWidgetItem(
                [
                    entry.summary,
                    entry.author,
                    entry.when.astimezone().strftime("%Y-%m-%d %H:%M"),
                    entry.sha[:8],
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, entry.sha)
            self._list.addTopLevelItem(item)
        self._list.setCurrentItem(self._list.topLevelItem(0))

    def _on_commit_selected(self) -> None:
        items = self._list.selectedItems()
        if not items:
            return
        sha = items[0].data(0, Qt.ItemDataRole.UserRole)
        self._generation += 1
        token = self._generation
        loader = DiffLoader(
            self._repo_path, sha, token, path=self._path, include_detail=False
        )
        loader.signals.ready.connect(self._on_diff_ready)
        loader.signals.failed.connect(self._on_failed)
        self._loaders[token] = loader
        self._pool.start(loader)

    @drops_late_deliveries
    def _on_diff_ready(self, token: int, _detail, lines) -> None:  # noqa: ANN001
        self._loaders.pop(token, None)
        if token != self._generation:
            return  # 늦게 도착한 이전 선택의 diff
        self._diff_model.set_lines(lines)

    @drops_late_deliveries
    def _on_failed(self, token: int, *rest) -> None:  # noqa: ANN002
        self._loaders.pop(token, None)
        if token != self._generation:
            return
        error = rest[-1]
        self._status.setText(
            trf("읽지 못했습니다: {message}", message=error.message)
        )

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        # 창의 수명이 로더보다 짧다 — 취소 + disconnect + 입구 가드의
        # 3중이 필요한 이유는 late_delivery 모듈 주석에 있다.
        for loader in self._loaders.values():
            loader.cancel()
            signals = getattr(loader, "signals", None)
            if signals is None:
                continue
            for name in ("ready", "failed"):
                try:
                    getattr(signals, name).disconnect()
                except (RuntimeError, TypeError):
                    pass
        self._loaders.clear()
        super().closeEvent(event)
