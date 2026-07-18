"""메인 윈도우.

Phase 1은 읽기 전용이다. 저장소를 열고, 커밋 그래프를 탐색하고,
선택한 커밋의 변경 파일과 diff를 본다. (doc/design.md §10 Phase 1)

레이아웃은 §5.1의 설계를 따른다.

    ┌──────────┬────────────────────────────────┐
    │ 참조 목록 │  커밋 그래프                    │
    │ (브랜치·  ├────────────┬───────────────────┤
    │  태그)   │ 변경 파일   │  diff             │
    └──────────┴────────────┴───────────────────┘
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QSettings, Qt, QThreadPool
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHeaderView,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from gitclient.application.commit_loader import CommitLoader
from gitclient.domain.errors import GitClientError
from gitclient.domain.models import Ref, RefKind, RepositoryInfo
from gitclient.infrastructure.local_engine import LocalGitEngine
from gitclient.ui.delegates import DiffDelegate, GraphDelegate, SummaryDelegate
from gitclient.viewmodel.commit_graph_model import (
    Column,
    CommitGraphModel,
    CommitRole,
)
from gitclient.viewmodel.diff_model import DiffModel

ROW_HEIGHT = 24
GRAPH_COLUMN_PADDING = 12


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Git Client")
        self.resize(1280, 800)

        self._engine: LocalGitEngine | None = None
        self._info: RepositoryInfo | None = None
        self._settings = QSettings("gitclient", "gitclient")
        self._pool = QThreadPool.globalInstance()
        self._loader: CommitLoader | None = None
        self._loading = False

        self._commit_model = CommitGraphModel(self)
        self._diff_model = DiffModel(self)

        self._build_ui()
        self._build_menu()
        self._show_placeholder()

    # ------------------------------------------------------------------
    # UI 구성
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._ref_list = QListWidget()
        self._ref_list.setAlternatingRowColors(False)
        self._ref_list.setMinimumWidth(180)

        self._commit_view = self._build_commit_view()
        self._file_list = QListWidget()
        self._diff_view = self._build_diff_view()

        detail_splitter = QSplitter(Qt.Orientation.Horizontal)
        detail_splitter.addWidget(self._wrap("변경 파일", self._file_list))
        detail_splitter.addWidget(self._wrap("변경 내용", self._diff_view))
        detail_splitter.setStretchFactor(0, 1)
        detail_splitter.setStretchFactor(1, 3)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.addWidget(self._commit_view)
        right_splitter.addWidget(detail_splitter)
        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 2)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.addWidget(self._wrap("참조", self._ref_list))
        main_splitter.addWidget(right_splitter)
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([220, 1060])

        self.setCentralWidget(main_splitter)
        self.statusBar().showMessage("저장소를 열어 주세요 (Ctrl+O)")

    def _wrap(self, title: str, widget: QWidget) -> QWidget:
        """제목 라벨이 붙은 패널로 감싼다."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        label = QLabel(title)
        label.setContentsMargins(6, 4, 6, 2)
        font = QFont(label.font())
        font.setBold(True)
        font.setPointSizeF(max(7.0, font.pointSizeF() - 0.5))
        label.setFont(font)

        layout.addWidget(label)
        layout.addWidget(widget)
        return container

    def _build_commit_view(self) -> QTableView:
        view = QTableView()
        view.setModel(self._commit_model)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setShowGrid(False)
        view.setAlternatingRowColors(True)
        view.verticalHeader().setVisible(False)
        view.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        view.setItemDelegateForColumn(Column.GRAPH, GraphDelegate(view))
        view.setItemDelegateForColumn(Column.SUMMARY, SummaryDelegate(view))

        header = view.horizontalHeader()
        header.setSectionResizeMode(Column.SUMMARY, QHeaderView.ResizeMode.Stretch)
        for column in (Column.AUTHOR, Column.DATE, Column.SHA):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        view.setColumnWidth(Column.AUTHOR, 140)
        view.setColumnWidth(Column.DATE, 110)
        view.setColumnWidth(Column.SHA, 80)

        view.selectionModel().currentRowChanged.connect(self._on_commit_selected)
        return view

    def _build_diff_view(self) -> QListView:
        view = QListView()
        view.setModel(self._diff_model)
        view.setItemDelegate(DiffDelegate(view))
        view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        view.setUniformItemSizes(True)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        view.setFont(self._monospace_font())
        return view

    def _monospace_font(self) -> QFont:
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        return font

    def _build_menu(self) -> None:
        open_action = QAction("저장소 열기...", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._prompt_open_repository)

        reload_action = QAction("새로 고침", self)
        reload_action.setShortcut(QKeySequence.StandardKey.Refresh)
        reload_action.triggered.connect(self._reload)

        quit_action = QAction("종료", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)

        menu = self.menuBar().addMenu("파일")
        menu.addAction(open_action)
        menu.addAction(reload_action)
        menu.addSeparator()
        menu.addAction(quit_action)

        toolbar = self.addToolBar("주요")
        toolbar.setMovable(False)
        toolbar.addAction(open_action)
        toolbar.addAction(reload_action)

    def _show_placeholder(self) -> None:
        self._file_list.clear()
        self._file_list.addItem("커밋을 선택하세요")

    # ------------------------------------------------------------------
    # 저장소 열기
    # ------------------------------------------------------------------

    def _prompt_open_repository(self) -> None:
        last = self._settings.value("last_repository", str(Path.home()))
        directory = QFileDialog.getExistingDirectory(
            self, "Git 저장소 선택", str(last)
        )
        if directory:
            self.open_repository(directory)

    def open_repository(self, path: str | Path) -> None:
        try:
            engine = LocalGitEngine.open(path)
            info = engine.info()
        except GitClientError as exc:
            self._report(exc)
            return

        self._cancel_loading()

        self._engine = engine
        self._info = info
        self._settings.setValue("last_repository", str(path))

        self._commit_model.reset(info.refs)
        self._populate_refs(info.refs)
        self._diff_model.clear()
        self._show_placeholder()

        self.setWindowTitle(f"{info.display_name} — Git Client")
        self._start_loading(path)

    def _reload(self) -> None:
        if self._engine is None:
            return
        self.open_repository(self._settings.value("last_repository"))

    # ------------------------------------------------------------------
    # 백그라운드 로딩
    # ------------------------------------------------------------------

    def _start_loading(self, path: str | Path) -> None:
        """커밋 순회를 워커 스레드에 맡긴다.

        순회 비용은 커밋 수에 비례해 수 초까지 늘어난다. UI 스레드에서 돌리면
        그동안 창이 얼어붙는다. (doc/design.md §3.3, G4)
        """
        loader = CommitLoader(path)
        loader.signals.batch_ready.connect(self._on_batch_ready)
        loader.signals.finished.connect(self._on_loading_finished)
        loader.signals.failed.connect(self._on_loading_failed)

        self._loader = loader
        self._loading = True
        self.statusBar().showMessage("커밋을 읽는 중...")
        self._pool.start(loader)

    def _cancel_loading(self) -> None:
        if self._loader is not None:
            self._loader.cancel()
        self._loading = False

    def _on_batch_ready(self, commits: list) -> None:
        was_empty = self._commit_model.rowCount() == 0
        self._commit_model.append_commits(commits)
        self._resize_graph_column()

        if was_empty and self._commit_model.rowCount() > 0:
            # 첫 묶음이 도착하는 즉시 최신 커밋을 보여준다.
            self._commit_view.selectRow(0)

        self.statusBar().showMessage(
            f"커밋을 읽는 중... {self._commit_model.rowCount()}개"
        )

    def _on_loading_finished(self, _total: int) -> None:
        # 주의: 여기서 self._loader를 버리면 안 된다. 이 슬롯은 워커의 시그널
        # 방출 중에 실행되며, 마지막 참조를 놓으면 방출 도중에 sender가 파괴되어
        # 뒤에 연결된 슬롯들이 실행되지 않는다. 참조는 다음 로딩이 시작될 때
        # 자연스럽게 교체된다.
        self._loading = False
        self._update_status(self._info)
        if self._commit_model.rowCount() == 0:
            self._diff_model.clear()
            self._show_placeholder()

    def _on_loading_failed(self, error: GitClientError) -> None:
        self._loading = False
        self._report(error)

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt 시그니처
        """창을 닫을 때 워커가 살아 있으면 정리한다."""
        self._cancel_loading()
        self._pool.waitForDone(2000)
        super().closeEvent(event)

    def _update_status(self, info: RepositoryInfo) -> None:
        parts = [f"HEAD: {info.head_shorthand or '(unborn)'}"]
        parts.append(f"커밋 {self._commit_model.rowCount()}개")
        if info.is_shallow:
            # shallow 저장소는 기능 제약이 있으므로 명시한다. (doc/design.md §3.3)
            parts.append("shallow 저장소 — 히스토리가 잘려 있습니다")
        self.statusBar().showMessage("   |   ".join(parts))

    def _populate_refs(self, refs: list[Ref]) -> None:
        self._ref_list.clear()
        groups = (
            ("로컬 브랜치", RefKind.LOCAL_BRANCH),
            ("원격 브랜치", RefKind.REMOTE_BRANCH),
            ("태그", RefKind.TAG),
        )
        for title, kind in groups:
            matching = [r for r in refs if r.kind is kind]
            if not matching:
                continue

            header = QListWidgetItem(title)
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            font = QFont(header.font())
            font.setBold(True)
            header.setFont(font)
            self._ref_list.addItem(header)

            for ref in sorted(matching, key=lambda r: r.shorthand):
                label = f"  {'● ' if ref.is_head else ''}{ref.shorthand}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, ref.target_sha)
                self._ref_list.addItem(item)

    def _resize_graph_column(self) -> None:
        """그래프 열 폭을 로드된 범위의 최대 레인 수에 맞춘다."""
        from gitclient.ui.delegates import LANE_WIDTH

        width = LANE_WIDTH * self._commit_model.max_lane_count + GRAPH_COLUMN_PADDING
        self._commit_view.setColumnWidth(Column.GRAPH, width)

    # ------------------------------------------------------------------
    # 커밋 선택
    # ------------------------------------------------------------------

    def _on_commit_selected(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._engine is None or not current.isValid():
            return

        commit = current.data(CommitRole.COMMIT)
        if commit is None:
            return

        try:
            detail = self._engine.commit_detail(commit.sha)
            lines = self._engine.diff_lines(commit.sha)
        except GitClientError as exc:
            self._report(exc)
            return

        self._file_list.clear()
        if not detail.changes:
            self._file_list.addItem("(변경된 파일 없음)")
        for change in detail.changes:
            item = QListWidgetItem(
                f"{change.status.value}  {change.display_path}"
                f"   +{change.insertions} -{change.deletions}"
            )
            item.setData(Qt.ItemDataRole.UserRole, change.path)
            self._file_list.addItem(item)

        self._diff_model.set_lines(lines)
        self._resize_graph_column()

    def _report(self, error: GitClientError) -> None:
        """오류를 원문과 함께 보여준다. (doc/design.md §5.2)"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("오류")
        box.setText(error.message)
        if error.detail:
            box.setDetailedText(error.detail)
        box.exec()
