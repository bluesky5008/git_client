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

from PySide6.QtCore import QModelIndex, QSettings, Qt, QThreadPool, QTimer
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
from gitclient.application.diff_loader import DiffLoader
from gitclient.application.refs_loader import RefsLoader
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
        self._repo_path: str | None = None
        self._settings = QSettings("gitclient", "gitclient")
        self._pool = QThreadPool.globalInstance()
        self._loader: CommitLoader | None = None
        self._refs_loader: RefsLoader | None = None
        self._loading = False

        # diff 비동기 상태 (§3.3 — 세대 토큰으로 순서 역전을 막는다)
        self._current_sha: str | None = None
        self._diff_generation = 0
        self._diff_loaders: dict[int, DiffLoader] = {}
        self._diff_debounce = QTimer(self)
        self._diff_debounce.setSingleShot(True)
        self._diff_debounce.setInterval(50)
        self._diff_debounce.timeout.connect(self._dispatch_pending_diff)

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
        self._ref_list.itemActivated.connect(self._on_ref_activated)

        self._commit_view = self._build_commit_view()
        self._file_list = QListWidget()
        self._file_list.currentRowChanged.connect(self._on_file_selected)
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
            # include_refs=False: ref 열거는 ref 수에 비례해 느리다(실측 ref당
            # ~1.3ms). UI 스레드에서는 lite 정보만 얻고 refs는 워커가 가져온다.
            info = engine.info(include_refs=False)
        except GitClientError as exc:
            self._report(exc)
            return

        self._cancel_loading()

        self._engine = engine
        self._info = info
        self._repo_path = str(path)
        self._settings.setValue("last_repository", str(path))

        self._commit_model.reset([])
        self._ref_list.clear()
        self._diff_model.clear()
        self._show_placeholder()
        self._current_sha = None

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
        """커밋 순회와 ref 열거를 워커 스레드에 맡긴다.

        두 작업 모두 입력 크기에 비례해 커진다 (doc/design.md §3.3).
        반드시 병렬로 시작한다 — refs가 커밋 첫 행 표시(G2)를 지연시키면 안 된다.

        각 슬롯은 람다로 loader 정체를 캡처해 자신이 최신인지 확인한다.
        취소된 이전 로더가 이미 큐에 넣어둔 시그널이 새 저장소의 모델에
        섞여 들어오는 경합을 막는다.
        """
        loader = CommitLoader(path)
        loader.signals.batch_ready.connect(
            lambda commits, l=loader: self._on_batch_ready(l, commits)
        )
        loader.signals.finished.connect(
            lambda total, l=loader: self._on_loading_finished(l, total)
        )
        loader.signals.failed.connect(
            lambda error, l=loader: self._on_loading_failed(l, error)
        )
        self._loader = loader
        self._loading = True

        refs_loader = RefsLoader(path)
        refs_loader.signals.ready.connect(
            lambda refs, l=refs_loader: self._on_refs_ready(l, refs)
        )
        refs_loader.signals.failed.connect(
            lambda error, l=refs_loader: self._on_refs_failed(l, error)
        )
        self._refs_loader = refs_loader

        self.statusBar().showMessage("커밋을 읽는 중...")
        self._pool.start(loader)
        self._pool.start(refs_loader)

    def _cancel_loading(self) -> None:
        if self._loader is not None:
            self._loader.cancel()
        if self._refs_loader is not None:
            self._refs_loader.cancel()
        for diff_loader in self._diff_loaders.values():
            diff_loader.cancel()
        self._loading = False

    def _on_batch_ready(self, loader: CommitLoader, commits: list) -> None:
        if loader is not self._loader:
            return  # 이전 저장소의 늦은 묶음

        was_empty = self._commit_model.rowCount() == 0
        self._commit_model.append_commits(commits)
        self._resize_graph_column()

        if was_empty and self._commit_model.rowCount() > 0:
            # 첫 묶음이 도착하는 즉시 최신 커밋을 보여준다.
            self._commit_view.selectRow(0)

        self.statusBar().showMessage(
            f"커밋을 읽는 중... {self._commit_model.rowCount()}개"
        )

    def _on_loading_finished(self, loader: CommitLoader, _total: int) -> None:
        if loader is not self._loader:
            return
        # 주의: 여기서 self._loader를 버리면 안 된다. 이 슬롯은 워커의 시그널
        # 방출 중에 실행되며, 마지막 참조를 놓으면 방출 도중에 sender가 파괴되어
        # 뒤에 연결된 슬롯들이 실행되지 않는다. 참조는 다음 로딩이 시작될 때
        # 자연스럽게 교체된다.
        self._loading = False
        self._update_status(self._info)
        if self._commit_model.rowCount() == 0:
            self._diff_model.clear()
            self._show_placeholder()

    def _on_loading_failed(self, loader: CommitLoader, error: GitClientError) -> None:
        if loader is not self._loader:
            return
        self._loading = False
        self._report(error)

    def _on_refs_ready(self, loader: RefsLoader, refs: list) -> None:
        if loader is not self._refs_loader:
            return  # 이전 저장소의 늦은 결과
        self._populate_refs(refs)
        self._commit_model.set_refs(refs)

    def _on_refs_failed(self, loader: RefsLoader, error: GitClientError) -> None:
        if loader is not self._refs_loader:
            return
        # refs 실패는 치명적이지 않다 — 그래프는 이미 뜨고 있다.
        # 모달로 흐름을 끊는 대신 상태바로 알린다. (doc/design.md §7)
        self.statusBar().showMessage(f"참조 목록을 읽지 못했습니다: {error.message}")

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt 시그니처
        """창을 닫을 때 워커가 살아 있으면 정리한다."""
        self._cancel_loading()
        self._pool.waitForDone(2000)
        super().closeEvent(event)

    def _update_status(self, info: RepositoryInfo) -> None:
        parts = [f"HEAD: {info.head_shorthand or '(unborn)'}"]
        parts.append(f"커밋 {self._commit_model.rowCount()}개")
        if info.is_shallow:
            # shallow 저장소는 기능 제약이 있으므로 명시한다. (doc/performance.md §3.3)
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
    # 커밋 선택 → diff (비동기)
    # ------------------------------------------------------------------
    #
    # diff 계산은 변경 크기에 비례해 커지므로 워커(DiffLoader)가 수행한다.
    # (doc/design.md §3.3) 방향키로 커밋을 빠르게 훑는 경우를 위해:
    #   - 디바운스(50ms): 연속 이동 중에는 요청을 만들지 않는다.
    #   - 세대 토큰: 순서 역전으로 이전 커밋의 diff가 화면을 덮지 않게 한다.

    def _on_commit_selected(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._engine is None or not current.isValid():
            return

        commit = current.data(CommitRole.COMMIT)
        if commit is None:
            return

        self._current_sha = commit.sha
        self._diff_debounce.start()  # 재시작 — 마지막 선택만 살아남는다

    def _dispatch_pending_diff(self) -> None:
        """디바운스가 끝난 시점의 현재 커밋에 대해 전체 diff를 요청한다."""
        if self._current_sha is None or self._repo_path is None:
            return
        self._request_diff(self._current_sha, path=None, include_detail=True)

    def _request_diff(
        self, sha: str, *, path: str | None, include_detail: bool
    ) -> None:
        # 새 요청이 발급되는 순간 진행 중인 이전 요청들은 전부 무의미해진다.
        for stale in self._diff_loaders.values():
            stale.cancel()

        self._diff_generation += 1
        token = self._diff_generation

        loader = DiffLoader(
            self._repo_path,
            sha,
            token,
            path=path,
            include_detail=include_detail,
        )
        loader.signals.ready.connect(self._on_diff_ready)
        loader.signals.failed.connect(self._on_diff_failed)
        self._diff_loaders[token] = loader
        self._pool.start(loader)

    def _on_diff_ready(self, token: int, detail, lines) -> None:  # noqa: ANN001
        self._diff_loaders.pop(token, None)
        if token != self._diff_generation:
            return  # 늦게 도착한 이전 세대의 결과

        if detail is not None:
            self._populate_file_list(detail)
        self._diff_model.set_lines(lines)

    def _on_diff_failed(self, token: int, error: GitClientError) -> None:
        self._diff_loaders.pop(token, None)
        if token != self._diff_generation:
            return
        self._report(error)

    def _populate_file_list(self, detail) -> None:  # noqa: ANN001
        """변경 파일 목록을 다시 만든다.

        재구성 중 발생하는 선택 변경 시그널은 막는다 — 사용자가 파일을 고른 게
        아니라 목록이 바뀐 것이므로, diff 재요청을 유발하면 안 된다.
        """
        self._file_list.blockSignals(True)
        try:
            self._file_list.clear()

            if not detail.changes:
                self._file_list.addItem("(변경된 파일 없음)")
                return

            whole = QListWidgetItem(
                f"전체 변경   +{detail.total_insertions} -{detail.total_deletions}"
            )
            whole.setData(Qt.ItemDataRole.UserRole, None)
            self._file_list.addItem(whole)

            for change in detail.changes:
                item = QListWidgetItem(
                    f"{change.status.value}  {change.display_path}"
                    f"   +{change.insertions} -{change.deletions}"
                )
                item.setData(Qt.ItemDataRole.UserRole, change.path)
                self._file_list.addItem(item)

            self._file_list.setCurrentRow(0)
        finally:
            self._file_list.blockSignals(False)

    def _on_file_selected(self, row: int) -> None:
        """변경 파일을 고르면 diff를 그 파일로 좁힌다. '전체 변경'은 전체 diff."""
        if row < 0 or self._current_sha is None or self._repo_path is None:
            return
        item = self._file_list.item(row)
        if item is None:
            return

        path = item.data(Qt.ItemDataRole.UserRole)
        # include_detail=False: 파일 목록은 그대로 두고 diff만 갈아끼운다.
        self._request_diff(self._current_sha, path=path, include_detail=False)

    def _on_ref_activated(self, item: QListWidgetItem) -> None:
        """참조를 더블클릭/Enter 하면 해당 커밋으로 이동한다."""
        target_sha = item.data(Qt.ItemDataRole.UserRole)
        if not target_sha:
            return  # 그룹 헤더

        row = self._commit_model.row_for_sha(target_sha)
        if row is None:
            self.statusBar().showMessage(
                "해당 커밋이 아직 로드되지 않았습니다.", 3000
            )
            return

        self._commit_view.selectRow(row)
        self._commit_view.scrollTo(
            self._commit_model.index(row, Column.SUMMARY),
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )

    def _report(self, error: GitClientError) -> None:
        """오류를 표시한다. (doc/design.md §5.2 원칙 4, §7)

        메시지 본문 아래에 권장 조치(action)를 바로 보이게 놓고,
        엔진 원문(detail)은 접히는 상세 영역에 보존한다.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("오류")
        box.setText(error.message)
        if error.action:
            box.setInformativeText(error.action)
        if error.detail:
            box.setDetailedText(error.detail)
        box.exec()
