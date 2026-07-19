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
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from gitclient.application.commit_loader import CommitLoader
from gitclient.application.diff_loader import DiffLoader, WorkdirDiffLoader
from gitclient.application.remote_workers import (
    FetchWorker,
    PullWorker,
    PushWorker,
    RemoteWorker,
    fast_forward_job,
)
from gitclient.application.refs_loader import RefsLoader
from gitclient.application.status_loader import StatusLoader
from gitclient.application.write_queue import WriteQueue
from gitclient.domain.errors import AuthenticationRequired, GitClientError
from gitclient.domain.instrumentation import OperationKind
from gitclient.domain.models import MergeKind, Ref, RefKind, RepositoryInfo
from gitclient.infrastructure.local_engine import LocalGitEngine
from gitclient.ui.credential_dialog import CredentialDialog
from gitclient.ui.delegates import DiffDelegate, GraphDelegate, SummaryDelegate
from gitclient.ui.working_tree_panel import WorkingTreePanel
from gitclient.viewmodel.commit_graph_model import (
    Column,
    CommitGraphModel,
    CommitRole,
)
from gitclient.viewmodel.diff_model import DiffModel, DiffRole

ROW_HEIGHT = 24
GRAPH_COLUMN_PADDING = 12


def _format_bytes(count: int) -> str:
    """전송량을 사람이 읽는 형태로. git과 같은 이진 접두사를 쓴다."""
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GiB"


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
        self._status_loader: StatusLoader | None = None
        self._loading = False

        # 쓰기 경로 (Phase 2). 저장소별 직렬화는 WriteQueue가 보장한다.
        self._write_queue: WriteQueue | None = None
        self._graph_reload_pending = False

        # 원격 작업 (Phase 3). 한 번에 하나만 — fetch/push/pull이 같은 슬롯을
        # 공유한다. 동시에 돌면 서로의 참조 갱신을 덮어쓸 수 있고, 무엇보다
        # 사용자가 무슨 일이 벌어지는지 알 수 없다.
        self._fetch_worker: RemoteWorker | None = None
        # 인증 실패 시 같은 작업을 자격증명과 함께 다시 만드는 함수.
        # 한 번 쓰면 비운다 — 무한히 되묻는 고리를 막는다.
        self._remote_retry = None

        # diff 비동기 상태 (§3.3 — 세대 토큰으로 순서 역전을 막는다)
        self._current_sha: str | None = None
        self._diff_source: tuple[str, bool] | None = None
        """지금 보고 있는 워킹트리 diff의 (경로, staged 여부). 커밋 diff면 None."""
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
        self._ref_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._ref_list.customContextMenuRequested.connect(self._on_ref_context_menu)

        self._commit_view = self._build_commit_view()
        self._file_list = QListWidget()
        self._file_list.currentRowChanged.connect(self._on_file_selected)
        self._diff_view = self._build_diff_view()

        self._work_panel = WorkingTreePanel()
        self._work_panel.stage_requested.connect(self._on_stage_requested)
        self._work_panel.unstage_requested.connect(self._on_unstage_requested)
        self._work_panel.discard_requested.connect(self._on_discard_requested)
        self._work_panel.commit_requested.connect(self._on_commit_requested)
        self._work_panel.diff_requested.connect(self._on_workdir_diff_requested)

        detail_splitter = QSplitter(Qt.Orientation.Horizontal)
        detail_splitter.addWidget(self._wrap("변경 파일", self._file_list))
        detail_splitter.addWidget(self._wrap("변경 내용", self._build_diff_panel()))
        detail_splitter.setStretchFactor(0, 1)
        detail_splitter.setStretchFactor(1, 3)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.addWidget(self._commit_view)
        right_splitter.addWidget(detail_splitter)
        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 2)

        left_splitter = QSplitter(Qt.Orientation.Vertical)
        left_splitter.addWidget(self._wrap("작업 디렉터리", self._work_panel))
        left_splitter.addWidget(self._wrap("참조", self._ref_list))
        left_splitter.setStretchFactor(0, 1)
        left_splitter.setStretchFactor(1, 1)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.addWidget(left_splitter)
        main_splitter.addWidget(right_splitter)
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([260, 1020])

        self.setCentralWidget(main_splitter)

        # 전송량은 임시 메시지로 띄우면 곧바로 다른 메시지에 덮인다 — 특히
        # fetch 직후의 그래프 재로딩에. 목적함수가 누적 전송 바이트인 만큼
        # 사용자가 비용을 계속 볼 수 있어야 하므로 고정 위젯에 둔다.
        # (performance.md §8.4)
        self._transfer_label = QLabel("")
        self._transfer_label.setToolTip("마지막 원격 작업의 전송량")
        self.statusBar().addPermanentWidget(self._transfer_label)

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
        # 부분 스테이징을 위해 여러 줄을 고를 수 있어야 한다 (Phase 2 증분 2).
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        view.setUniformItemSizes(True)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        view.setFont(self._monospace_font())
        view.selectionModel().selectionChanged.connect(
            lambda *_: self._update_partial_actions()
        )
        return view

    def _build_diff_panel(self) -> QWidget:
        """diff 뷰 + 부분 스테이징 도구 모음."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 0, 4, 0)
        self._stage_hunk_button = QPushButton("헝크 올리기")
        self._stage_hunk_button.setToolTip(
            "커서가 있는 헝크 전체를 스테이징합니다"
        )
        self._stage_hunk_button.clicked.connect(self._on_stage_hunk)
        bar.addWidget(self._stage_hunk_button)

        self._stage_lines_button = QPushButton("선택 줄 올리기")
        self._stage_lines_button.setToolTip(
            "선택한 줄만 스테이징합니다 (여러 줄 선택 가능)"
        )
        self._stage_lines_button.clicked.connect(self._on_stage_lines)
        bar.addWidget(self._stage_lines_button)

        bar.addStretch(1)
        self._partial_hint = QLabel("")
        bar.addWidget(self._partial_hint)
        layout.addLayout(bar)

        layout.addWidget(self._diff_view)
        return container

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

        self._fetch_action = QAction("가져오기 (Fetch)", self)
        self._fetch_action.setToolTip(
            "원격의 변경을 가져옵니다 (워킹 트리는 건드리지 않습니다)"
        )
        self._fetch_action.triggered.connect(self._on_fetch)

        self._pull_action = QAction("가져와 합치기 (Pull)", self)
        self._pull_action.setToolTip("원격의 변경을 가져와 현재 브랜치에 합칩니다")
        self._pull_action.triggered.connect(self._on_pull)

        self._push_action = QAction("올리기 (Push)", self)
        self._push_action.setToolTip("로컬 커밋을 원격에 올립니다")
        self._push_action.triggered.connect(self._on_push)

        self._branch_action = QAction("새 브랜치...", self)
        self._branch_action.triggered.connect(self._prompt_new_branch)
        self._stash_action = QAction("Stash 보관", self)
        self._stash_action.triggered.connect(self._on_stash_save)
        self._stash_pop_action = QAction("Stash 꺼내기", self)
        self._stash_pop_action.triggered.connect(self._on_stash_pop)

        repo_menu = self.menuBar().addMenu("저장소")
        repo_menu.addAction(self._fetch_action)
        repo_menu.addAction(self._pull_action)
        repo_menu.addAction(self._push_action)
        repo_menu.addSeparator()
        repo_menu.addAction(self._branch_action)
        repo_menu.addSeparator()
        repo_menu.addAction(self._stash_action)
        repo_menu.addAction(self._stash_pop_action)

        toolbar = self.addToolBar("주요")
        toolbar.setMovable(False)
        toolbar.addAction(open_action)
        toolbar.addAction(reload_action)
        toolbar.addSeparator()
        toolbar.addAction(self._fetch_action)
        toolbar.addAction(self._pull_action)
        toolbar.addAction(self._push_action)
        toolbar.addSeparator()
        toolbar.addAction(self._branch_action)
        toolbar.addAction(self._stash_action)
        toolbar.addAction(self._stash_pop_action)

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

        # fetch 워커는 저장소에 묶여 있다. 저장소가 바뀌면 여기서 놓아줘야
        # _on_fetch_finished의 정체 가드가 비로소 성립한다 — 그러지 않으면
        # 이전 저장소의 전송량이 새 저장소 상태바에 찍히고, 새 저장소가
        # 통째로 재로딩되어 선택과 스크롤이 초기화된다.
        # _repo_path를 덮어쓰기 전에 비교해야 하므로 순서가 중요하다.
        if self._fetch_worker is not None and str(path) != self._repo_path:
            self._fetch_worker.cancel()
            self._fetch_worker = None

        self._engine = engine
        self._info = info
        self._repo_path = str(path)
        self._settings.setValue("last_repository", str(path))

        self._commit_model.reset([])
        self._ref_list.clear()
        self._diff_model.clear()
        self._show_placeholder()
        self._current_sha = None

        # 쓰기 경로. bare 저장소는 워킹 트리가 없어 쓰기 UI를 껐다.
        #
        # 같은 저장소를 다시 여는 경우(F5, 커밋 후 그래프 재로딩)에는 기존 큐를
        # **재사용**한다. 진행 중인 쓰기가 있는데 새 큐를 만들면 같은 저장소에
        # 쓰기 스트림이 두 개 생겨 §3.3 규칙 3이 깨진다 (리뷰에서 확정된 결함).
        has_workdir = info.workdir is not None
        # 큐 키는 사용자가 입력한 경로가 아니라 pygit2가 정규화한 workdir다.
        # 같은 저장소를 다른 표기(하위 디렉터리, 슬래시 방향)로 열어도
        # 재사용 판정이 어긋나면 안 된다 — 어긋나면 직렬화가 깨진다.
        queue_key = (
            str(Path(info.workdir).resolve()) if has_workdir else None
        )
        same_repo_queue = (
            self._write_queue is not None
            and queue_key is not None
            and self._write_queue.repo_path == queue_key
        )
        if has_workdir and not same_repo_queue:
            self._dispose_write_queue()
            queue = WriteQueue(queue_key, self._pool, parent=self)
            # 정체 캡처: 교체된 구 큐의 늦은 시그널이 새 저장소 UI를 건드리지 못한다.
            queue.job_failed.connect(
                lambda _jid, _name, error, q=queue: self._on_write_failed(q, error)
            )
            queue.idle.connect(lambda q=queue: self._on_write_queue_idle(q))
            self._write_queue = queue
        elif not has_workdir:
            self._dispose_write_queue()

        # 재사용된 큐에 재로딩을 요청한 쓰기가 아직 남아 있을 수 있다 —
        # 그 경우 플래그를 보존해야 큐가 빈 뒤 그래프가 갱신된다.
        if self._write_queue is None or not self._write_queue.is_busy:
            self._graph_reload_pending = False

        self._work_panel.set_enabled_for_repo(has_workdir)
        for action in (self._branch_action, self._stash_action, self._stash_pop_action):
            action.setEnabled(has_workdir)

        self._update_remote_actions()

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
        self._refresh_status()

    def _refresh_status(self) -> None:
        """작업 디렉터리 상태를 워커로 다시 읽는다. bare 저장소는 대상 아님."""
        if self._repo_path is None or (
            self._info is not None and self._info.workdir is None
        ):
            return
        if self._status_loader is not None:
            self._status_loader.cancel()

        status_loader = StatusLoader(self._repo_path)
        status_loader.signals.ready.connect(
            lambda status, head, l=status_loader: self._on_status_ready(
                l, status, head
            )
        )
        status_loader.signals.failed.connect(
            lambda error, l=status_loader: self._on_status_failed(l, error)
        )
        self._status_loader = status_loader
        self._pool.start(status_loader)

    def _on_status_ready(
        self, loader: StatusLoader, status, head_message  # noqa: ANN001
    ) -> None:
        if loader is not self._status_loader:
            return
        self._work_panel.show_status(status, head_message)

    def _on_status_failed(self, loader: StatusLoader, error: GitClientError) -> None:
        if loader is not self._status_loader:
            return
        self.statusBar().showMessage(
            f"작업 디렉터리 상태를 읽지 못했습니다: {error.message}"
        )

    def _cancel_loading(self) -> None:
        if self._loader is not None:
            self._loader.cancel()
        if self._refs_loader is not None:
            self._refs_loader.cancel()
        if self._status_loader is not None:
            self._status_loader.cancel()
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
        # fetch는 네트워크에 매달려 있어 스스로 끝나기를 기다릴 수 없다.
        # 취소가 git 프로세스 트리를 끊으므로 전역 스레드풀 슬롯이 곧 풀린다.
        # 그러지 않으면 창이 사라진 뒤에도 프로세스가 최대 5분 남아, 사용자가
        # 앱을 다시 띄우면 두 인스턴스가 같은 계측 DB에 쓰게 된다.
        if self._fetch_worker is not None:
            self._fetch_worker.cancel()
            self._fetch_worker = None
        # 큐를 분리해 두면 닫힌 뒤 도착하는 늦은 idle/실패 시그널을
        # 정체 가드가 걸러낸다 — 파괴된 위젯을 건드리지 않는다.
        self._dispose_write_queue()
        self._pool.waitForDone(2000)
        super().closeEvent(event)

    def _update_status(self, info: RepositoryInfo) -> None:
        parts = [f"HEAD: {info.head_shorthand or '(unborn)'}"]
        parts.append(f"커밋 {self._commit_model.rowCount()}개")
        divergence = self._describe_divergence()
        if divergence:
            parts.append(divergence)
        if info.is_shallow:
            # shallow 저장소는 기능 제약이 있으므로 명시한다. (doc/performance.md §3.3)
            parts.append("shallow 저장소 — 히스토리가 잘려 있습니다")
        self.statusBar().showMessage("   |   ".join(parts))

    def _describe_divergence(self) -> str | None:
        """원격과 얼마나 벌어져 있는가.

        이 값이 없으면 사용자는 pull과 push 중 무엇이 필요한지 눌러보기
        전까지 알 수 없다. 트래픽이 비싼 환경에서 "눌러서 확인"은 비용이다.
        """
        counts = self._ahead_behind()
        if counts is None:
            return None
        ahead, behind = counts
        if not ahead and not behind:
            return "원격과 동기화됨"
        pieces = []
        if ahead:
            pieces.append(f"↑{ahead}")
        if behind:
            pieces.append(f"↓{behind}")
        return "원격 대비 " + " ".join(pieces)

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
                item.setData(Qt.ItemDataRole.UserRole + 1, ref.kind.value)
                item.setData(Qt.ItemDataRole.UserRole + 2, ref.shorthand)
                item.setData(Qt.ItemDataRole.UserRole + 3, ref.is_head)
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
        # 커밋 diff는 부분 스테이징 대상이 아니다.
        self._diff_source = None
        # 새 요청이 발급되는 순간 진행 중인 이전 요청들은 전부 무의미해진다.
        # cancel된 로더는 시그널을 내지 않아 dict에서 안 빠지므로 여기서 비운다
        # (리뷰에서 확정된 누수). 워커가 실행 중이어도 자기 프레임이 self를
        # 붙잡고 있어 방출 중 파괴는 일어나지 않는다.
        for stale in self._diff_loaders.values():
            stale.cancel()
        self._diff_loaders.clear()

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
        self._update_partial_actions()

    def _on_workdir_diff_ready(self, token: int, patch, lines, positions) -> None:  # noqa: ANN001
        """커밋되지 않은 변경의 diff — 부분 스테이징 좌표를 함께 받는다."""
        self._diff_loaders.pop(token, None)
        if token != self._diff_generation:
            return
        self._diff_model.set_lines(lines, positions, patch)
        self._update_partial_actions()

    def _on_diff_failed(self, token: int, error: GitClientError) -> None:
        self._diff_loaders.pop(token, None)
        if token != self._diff_generation:
            return
        if self._diff_source is not None:
            # 부분 적용으로 그 파일의 변경이 모두 사라진 경우다 — 오류가 아니라
            # "더 볼 것이 없음"이므로 모달을 띄우지 않고 조용히 비운다.
            self._diff_model.clear()
            self._diff_source = None
            self._update_partial_actions()
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

    # ------------------------------------------------------------------
    # 쓰기 연산 (Phase 2) — 전부 WriteQueue를 경유한다 (§3.3 규칙 3)
    # ------------------------------------------------------------------

    def _dispose_write_queue(self) -> None:
        """큐를 교체/제거하기 전 구 큐를 정리한다.

        시그널은 정체 캡처 가드가 걸러내므로 남은 작업이 끝나기를 기다렸다가
        스스로 지워지게 한다. 진행 중인 쓰기를 강제로 죽이면 인덱스가 깨진다.
        """
        old = self._write_queue
        if old is None:
            return
        self._write_queue = None
        if old.is_busy:
            old.idle.connect(old.deleteLater)
        else:
            old.deleteLater()

    def _submit_write(
        self,
        name: str,
        work,  # noqa: ANN001 - Callable[[LocalGitEngine], Any]
        *,
        reload_graph: bool = False,
        on_success=None,  # noqa: ANN001
    ) -> None:
        queue = self._write_queue
        if queue is None:
            return

        self.statusBar().showMessage(f"{name}...")
        job_id = queue.submit(name, work)
        # submit 직후 connect해도 안전하다 — 완료 시그널은 UI 스레드 이벤트 루프로
        # 큐잉되므로 이 메서드가 반환하기 전에는 배달되지 않는다.

        def _cleanup() -> None:
            for signal, slot in ((queue.job_succeeded, _ok), (queue.job_failed, _fail)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass

        def _ok(jid: int, _done_name: str, result: object) -> None:
            if jid != job_id:
                return
            _cleanup()
            if reload_graph:
                # 성공했을 때만 재로딩한다. 제출 시점에 플래그를 세우면
                # 실패한 쓰기도 전체 재로딩을 유발한다 (리뷰에서 확정된 결함).
                self._graph_reload_pending = True
            if on_success is not None:
                on_success(result)

        def _fail(jid: int, _done_name: str, _error: object) -> None:
            if jid != job_id:
                return
            _cleanup()

        queue.job_succeeded.connect(_ok)
        queue.job_failed.connect(_fail)

    def _on_write_failed(self, queue: WriteQueue, error: GitClientError) -> None:
        if queue is not self._write_queue:
            return  # 교체된 저장소의 늦은 실패 — 현재 화면과 무관하다
        self._report(error)

    def _on_write_queue_idle(self, queue: WriteQueue) -> None:
        """쓰기 묶음이 끝났다 — 상태를 새로 읽고, 필요하면 그래프도 다시 만든다."""
        if queue is not self._write_queue:
            return  # 교체된 저장소의 늦은 idle
        if self._graph_reload_pending and self._repo_path is not None:
            self._graph_reload_pending = False
            self.open_repository(self._repo_path)
            return
        self._refresh_status()
        self._update_status(self._info)

    def _on_stage_requested(self, path: str) -> None:
        self._submit_write(
            f"스테이징: {path}", lambda engine: engine.stage_file(path)
        )

    def _on_unstage_requested(self, path: str) -> None:
        self._submit_write(
            f"스테이징 취소: {path}", lambda engine: engine.unstage_file(path)
        )

    def _on_discard_requested(self, path: str) -> None:
        # §5.2 원칙 2: 파괴적 작업은 무엇이 사라지는지 명시하고 확인받는다.
        answer = QMessageBox.warning(
            self,
            "변경 사항 버리기",
            f"{path} 의 커밋되지 않은 변경을 버립니다.\n"
            "이 작업은 되돌릴 수 없습니다. 계속할까요?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is not QMessageBox.StandardButton.Discard:
            return
        self._submit_write(
            f"버리기: {path}", lambda engine: engine.discard_file(path)
        )

    def _on_commit_requested(self, message: str, amend: bool) -> None:
        # 연타 가드: 앞선 커밋이 아직 큐에 있으면 무시한다. 엔진의 빈 커밋
        # 거부와 이중 방어다 (리뷰에서 확정된 결함 — 연타 시 중복 커밋).
        if self._write_queue is None or self._write_queue.is_busy:
            return
        name = "커밋 수정" if amend else "커밋"
        self._submit_write(
            name,
            lambda engine: engine.create_commit(message, amend=amend),
            reload_graph=True,
            on_success=lambda _sha: self._work_panel.clear_message(),
        )

    def _on_workdir_diff_requested(self, path: str, staged: bool) -> None:
        if self._repo_path is None:
            return
        # 이 요청이 보류 중인 커밋 diff 의도를 대체한다. 디바운스를 멈추지 않으면
        # 나중에 만료돼 이전 커밋 diff가 최신 토큰을 받아 이 결과를 덮는다
        # (리뷰에서 확정된 순서 역전).
        self._diff_debounce.stop()
        for stale in self._diff_loaders.values():
            stale.cancel()
        self._diff_loaders.clear()

        self._diff_generation += 1
        token = self._diff_generation
        self._diff_source = (path, staged)
        loader = WorkdirDiffLoader(self._repo_path, path, token, staged=staged)
        loader.signals.ready.connect(self._on_workdir_diff_ready)
        loader.signals.failed.connect(self._on_diff_failed)
        self._diff_loaders[token] = loader
        self._pool.start(loader)

    # ------------------------------------------------------------------
    # 부분 스테이징 (Phase 2 증분 2)
    # ------------------------------------------------------------------

    def _selected_positions(self) -> set[tuple[int, int]]:
        """diff 뷰에서 선택된 줄 중 변경 줄의 좌표."""
        positions = set()
        for index in self._diff_view.selectionModel().selectedIndexes():
            position = index.data(DiffRole.PATCH_POSITION)
            if position is not None:
                positions.add(tuple(position))
        return positions

    def _current_hunk_positions(self) -> set[tuple[int, int]]:
        """커서가 있는 헝크의 모든 변경 줄 좌표."""
        current = self._diff_view.currentIndex()
        if not current.isValid():
            return set()

        # 행이 어느 헝크에 속하는지는 모델에 직접 묻는다. 좌표를 위로 훑어
        # 추측하면 헝크 머리글이나 선행 컨텍스트에서 앞 헝크로 새어 나가,
        # 사용자가 보던 것과 다른 헝크가 올라간다 (확정된 결함).
        hunk_index = self._diff_model.hunk_at(current.row())
        if hunk_index is None:
            return set()
        return self._diff_model.positions_in_hunk(hunk_index)

    def _update_partial_actions(self) -> None:
        """부분 스테이징 버튼의 활성 상태와 안내 문구를 갱신한다."""
        patch = self._diff_model.patch
        source = self._diff_source
        can_partial = (
            self._write_queue is not None
            and patch is not None
            and patch.can_stage_partially
            and source is not None
        )

        if not can_partial:
            self._stage_hunk_button.setEnabled(False)
            self._stage_lines_button.setEnabled(False)
            hint = ""
            if patch is not None and patch.is_binary:
                hint = "바이너리 파일은 부분 스테이징할 수 없습니다"
            self._partial_hint.setText(hint)
            return

        staged = source[1]
        verb = "내리기" if staged else "올리기"
        self._stage_hunk_button.setText(f"헝크 {verb}")
        self._stage_lines_button.setText(f"선택 줄 {verb}")

        selected = self._selected_positions()
        self._stage_hunk_button.setEnabled(bool(self._current_hunk_positions()))
        self._stage_lines_button.setEnabled(bool(selected))
        self._partial_hint.setText(
            f"{len(selected)}줄 선택됨" if selected else "줄을 선택하면 그 줄만 적용합니다"
        )

    def _on_stage_hunk(self) -> None:
        self._apply_partial(self._current_hunk_positions(), scope="헝크")

    def _on_stage_lines(self) -> None:
        self._apply_partial(self._selected_positions(), scope="선택 줄")

    def _apply_partial(self, positions: set[tuple[int, int]], *, scope: str) -> None:
        if not positions or self._diff_source is None:
            return

        # 연타 가드: 앞선 부분 적용이 아직 큐에 있으면 화면 좌표는 이미 낡았다.
        # 그대로 제출하면 엔진이 새로 읽은 패치의 엉뚱한 줄에 적용된다.
        if self._write_queue is None or self._write_queue.is_busy:
            return

        path, staged = self._diff_source
        verb = "내리기" if staged else "올리기"

        if staged:
            work = lambda engine: engine.unstage_partial(path, positions)  # noqa: E731
        else:
            work = lambda engine: engine.stage_partial(path, positions)  # noqa: E731

        self._submit_write(
            f"{scope} {verb}: {path}",
            work,
            on_success=lambda _r: self._reload_after_partial(path, staged),
        )

    def _reload_after_partial(self, path: str, staged: bool) -> None:
        """부분 적용 뒤 diff를 다시 읽어 화면 좌표를 인덱스와 맞춘다.

        갱신하지 않으면 화면의 (헝크, 줄) 좌표가 낡은 채 남는다. 엔진은 적용할
        때 패치를 새로 읽으므로, 다음 적용이 **다른 줄에 꽂혀** 인덱스가 조용히
        오염된다. 확정된 결함의 수정이다.
        """
        if self._diff_source != (path, staged):
            return  # 그 사이 사용자가 다른 변경을 골랐다
        self._diff_model.clear()  # 낡은 좌표와 선택을 즉시 버린다
        self._update_partial_actions()
        self._on_workdir_diff_requested(path, staged)

    # ------------------------------------------------------------------
    # 브랜치 / Stash (Phase 2)
    # ------------------------------------------------------------------

    def _prompt_new_branch(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, "새 브랜치", "브랜치 이름:")
        name = name.strip()
        if not ok or not name:
            return
        self._submit_write(
            f"브랜치 생성: {name}",
            lambda engine: engine.create_branch(name, checkout=True),
            reload_graph=True,
        )

    def _on_stash_save(self) -> None:
        self._submit_write(
            "Stash 보관",
            lambda engine: engine.stash_save(),
            reload_graph=True,
        )

    def _on_stash_pop(self) -> None:
        self._submit_write(
            "Stash 꺼내기",
            lambda engine: engine.stash_pop(),
            reload_graph=True,
        )

    # ------------------------------------------------------------------
    # 원격 작업 (Phase 3)
    # ------------------------------------------------------------------

    def _fetch_remote(self) -> str | None:
        """fetch 대상 원격. git과 같이 origin을 우선하고, 없으면 첫 번째를 쓴다.

        버튼 활성 조건과 실행 대상이 같은 규칙에서 나와야 한다. "원격이 하나
        라도 있으면 켠다"면서 항상 origin을 fetch하면, `upstream` 하나만 있는
        저장소에서 버튼은 켜지고 누르면 "origin을 찾을 수 없다"가 뜬다 —
        원격 주소도 권한도 멀쩡한데 엉뚱한 조치를 안내하게 된다.
        """
        if self._info is None or not self._info.remotes:
            return None
        remotes = self._info.remotes
        return "origin" if "origin" in remotes else remotes[0]

    def _has_remotes(self) -> bool:
        """fetch할 원격이 있는가. 활성 판정과 실행 대상이 어긋나지 않게 한다."""
        return self._fetch_remote() is not None

    def _current_branch(self) -> str | None:
        return self._info.head_shorthand if self._info is not None else None

    def _upstream(self) -> tuple[str, str] | None:
        """현재 브랜치가 따라가는 (원격 이름, 원격 추적 참조).

        규약(`<origin>/<브랜치명>`)으로 **추측하지 않고** 설정을 읽는다.
        추측하면 두 가지가 조용히 깨진다:
          - fork 워크플로(origin=내 fork, upstream=원본)에서 엉뚱한 ref를
            대상으로 삼고 "이미 최신"이라 답한다
          - 분리된 HEAD에서 pygit2가 `shorthand`로 `"HEAD"`를 주므로
            `refs/remotes/<remote>/HEAD`가 만들어진다. 이 참조는 모든 clone에
            존재하고 원격 기본 브랜치를 가리키므로, bisect 중인 사용자의
            HEAD가 조용히 그쪽으로 끌려간다.
        """
        return self._engine.upstream_of_head() if self._engine is not None else None

    def _upstream_ref(self) -> str | None:
        resolved = self._upstream()
        return resolved[1] if resolved is not None else None

    # -- 공통 실행 경로 -------------------------------------------------

    def _start_remote(self, worker, message: str, *, retry=None) -> None:  # noqa: ANN001
        """원격 작업 하나를 시작한다.

        `retry`는 자격증명을 받아 같은 작업을 다시 만드는 함수다. 인증은
        "물어보면 해결되는" 유일한 실패라, 실패 시 되풀이할 방법을 여기서
        기억해 둔다. None이면 다시 시도하지 않는다.
        """
        self._remote_retry = retry
        worker.signals.finished.connect(
            lambda stats, w=worker: self._on_remote_finished(w, stats)
        )
        worker.signals.failed.connect(
            lambda error, w=worker: self._on_remote_failed(w, error)
        )
        self._fetch_worker = worker
        self._update_remote_actions()
        self.statusBar().showMessage(message)
        self._pool.start(worker)

    def _update_remote_actions(self) -> None:
        """원격 액션의 활성 상태를 한 곳에서 정한다.

        판정이 흩어지면 "켜져 있는데 눌러도 아무 일 없는 버튼"이 생긴다 —
        실제로 겪은 결함이다.
        """
        idle = self._fetch_worker is None
        available = self._has_remotes() and idle
        self._fetch_action.setEnabled(available)
        self._pull_action.setEnabled(available)
        # push는 워킹 트리가 있어야 의미가 있다(bare 저장소는 올릴 것이 없다).
        has_workdir = self._info is not None and self._info.workdir is not None
        self._push_action.setEnabled(available and has_workdir)

    def _on_remote_finished(self, worker, stats) -> None:  # noqa: ANN001
        if worker is not self._fetch_worker:
            return  # 이전 저장소의 늦은 결과
        self._fetch_worker = None
        self._update_remote_actions()
        self._transfer_label.setText(self._describe_transfer(stats))

        # pull은 여기서 끝나지 않는다 — 받은 것을 합쳐야 한다.
        if isinstance(worker, PullWorker):
            self._finish_pull()
            return

        # transferred_anything(팩을 옮겼는가)이 아니라 changed_anything으로
        # 판단한다. 객체를 하나도 옮기지 않고 참조만 바뀌는 작업이 있다 —
        # 이미 가진 커밋을 가리키는 새 브랜치, prune 삭제. 전자를 쓰면 .git에
        # 있는 브랜치가 화면에 끝내 안 나타나고, 이미 지워진 원격 브랜치가
        # 계속 남는다.
        if stats.changed_anything and self._repo_path is not None:
            # 전송량 표시는 고정 위젯이라 이 재로딩에 덮이지 않는다.
            self.open_repository(self._repo_path)
        else:
            # 재로딩이 없으면 "가져오는 중..." 임시 메시지를 아무도 지우지
            # 않아, 끝난 작업이 계속 진행 중인 것처럼 보인다.
            self._update_status(self._info)

    def _on_remote_failed(self, worker, error: GitClientError) -> None:  # noqa: ANN001
        if worker is not self._fetch_worker:
            return
        self._fetch_worker = None
        self._update_remote_actions()
        self._update_status(self._info)

        if isinstance(error, AuthenticationRequired) and self._ask_and_retry(error):
            return
        self._report(error)

    def _ask_and_retry(self, error: AuthenticationRequired) -> bool:
        """자격증명을 받아 같은 작업을 한 번 더 시도한다.

        `True`면 재시도를 시작했으므로 오류를 보고하지 않는다 — 사용자가
        아직 결과를 모르는 상태에서 실패 모달을 띄우면 혼란스럽다.

        **한 번만 되풀이한다.** `retry`를 소비하고 비우므로, 두 번째 실패는
        그대로 보고된다. 그러지 않으면 틀린 자격증명으로 무한히 되묻는
        고리가 생긴다.
        """
        retry, self._remote_retry = self._remote_retry, None
        if retry is None or self._repo_path is None:
            return False

        dialog = CredentialDialog(
            url=error.url,
            username=error.username,
            rejected=error.rejected,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            # 취소는 실패가 아니라 사용자의 선택이다. 모달을 한 번 더 띄우지
            # 않고 상태바로만 알린다.
            self.statusBar().showMessage("로그인을 취소했습니다.")
            return True

        worker = retry(dialog.credentials())
        if worker is None:
            return False
        self._start_remote(worker, "자격증명으로 다시 시도하는 중...")
        return True

    # -- fetch ----------------------------------------------------------

    def _on_fetch(self) -> None:
        """원격에서 변경을 가져온다. 계측 결과를 상태바에 보고한다."""
        if self._repo_path is None or self._fetch_worker is not None:
            return  # 이미 진행 중 — 중복 실행을 막는다

        remote = self._fetch_remote()
        if remote is None:
            return  # 원격이 없으면 액션이 꺼져 있어야 한다

        path = self._repo_path
        self._start_remote(
            FetchWorker(path, remote),
            "원격에서 가져오는 중...",
            retry=lambda creds: FetchWorker(path, remote, credentials=creds),
        )

    # -- push -----------------------------------------------------------

    def _on_push(self) -> None:
        """로컬 커밋을 원격에 올린다."""
        if self._repo_path is None or self._fetch_worker is not None:
            return

        branch = self._current_branch()
        resolved = self._upstream()
        remote = resolved[0] if resolved is not None else self._fetch_remote()
        if remote is None or branch is None:
            return

        # upstream이 **설정돼 있지 않을 때만** 함께 설정한다.
        # "ahead/behind를 계산할 수 없다"로 판정하면 안 된다 — 아직 fetch하지
        # 않아 비교만 실패한 경우까지 미설정으로 읽어, 사용자가 지정해 둔
        # 추적 대상(fork 워크플로의 upstream/main 등)을 조용히 덮어쓴다.
        needs_upstream = resolved is None
        path = self._repo_path
        self._start_remote(
            PushWorker(path, remote, branch, set_upstream=needs_upstream),
            f"{remote}로 올리는 중...",
            retry=lambda creds: PushWorker(
                path, remote, branch,
                set_upstream=needs_upstream, credentials=creds,
            ),
        )

    # -- pull -----------------------------------------------------------

    def _on_pull(self) -> None:
        """가져온 뒤 합친다. 네트워크와 로컬 쓰기가 만나는 지점이다.

        앞 절반(fetch)은 워커가, 뒤 절반(빨리 감기)은 WriteQueue가 맡는다.
        워커가 직접 합치면 같은 저장소에 쓰기 스트림이 둘 생겨 §3.3 규칙 3이
        깨진다.
        """
        if self._repo_path is None or self._fetch_worker is not None:
            return

        # 브랜치가 실제로 따라가는 원격에서 가져온다. 규약으로 추측한 원격에서
        # 가져오면 합치기 대상은 맞는데 데이터가 낡는다.
        resolved = self._upstream()
        remote = resolved[0] if resolved is not None else self._fetch_remote()
        if remote is None:
            return

        path = self._repo_path
        self._start_remote(
            PullWorker(path, remote),
            "가져와 합치는 중...",
            retry=lambda creds: PullWorker(path, remote, credentials=creds),
        )

    def _finish_pull(self) -> None:
        """fetch가 끝난 뒤의 합치기. 필요한 경우에만 큐에 제출한다.

        **어느 갈래로 빠지든 화면을 다시 그린다.** fetch는 이미 원격 추적
        참조를 갱신했으므로, 합칠 것이 없다고 재로딩을 건너뛰면 받아온 브랜치·
        태그가 화면에 끝내 나타나지 않는다. 합치기 여부와 화면 갱신 여부는
        별개다.
        """
        upstream, branch = self._upstream_ref(), self._current_branch()

        def reload_and(report: GitClientError | None = None) -> None:
            if self._repo_path is not None:
                self.open_repository(self._repo_path)
            else:
                self._update_status(self._info)
            if report is not None:
                self._report(report)

        if upstream is None or branch is None or self._engine is None:
            # upstream이 없는 브랜치(아직 push한 적 없음)나 분리된 HEAD.
            # 가져온 것은 반영하되 합칠 대상이 없다는 것만 알린다.
            reload_and()
            return

        try:
            preview = self._engine.merge_preview(upstream)
        except GitClientError as exc:
            reload_and(exc)
            return

        if preview.kind is MergeKind.UP_TO_DATE:
            reload_and()
            return

        if preview.kind is MergeKind.MERGE_REQUIRED:
            # 병합 커밋과 충돌 해결 UI는 Phase 4다. 여기서 병합을 시작하면
            # 사용자가 앱 안에서 끝낼 수 없는 상태에 갇힌다 — 받은 것은
            # 이미 저장했으니 그래프에는 반영하고, 할 수 없는 일은 말한다.
            reload_and(
                GitClientError(
                    "양쪽에 서로 다른 커밋이 있어 자동으로 합칠 수 없습니다.",
                    detail="원격 변경은 이미 가져왔습니다. "
                    "합치기(merge)는 아직 지원하지 않습니다.",
                    action="지금은 git CLI에서 merge 또는 rebase로 "
                    "해결해 주세요. 앱 안에서의 병합은 Phase 4에서 추가됩니다.",
                )
            )
            return

        if self._write_queue is None:
            reload_and()
            return
        # upstream을 계산한 것과 같은 스냅샷의 브랜치를 고정해 보낸다 —
        # 참조 이름과 브랜치 정체가 어긋날 수 없게.
        self._write_queue.submit(
            "가져와 합치기", fast_forward_job(upstream, branch)
        )
        self._graph_reload_pending = True

    def _ahead_behind(self) -> tuple[int, int] | None:
        """(앞선 커밋 수, 뒤처진 커밋 수). upstream이 없으면 None.

        UI 스레드에서 부른다 — `git rev-list --count`는 로컬 질의라 빠르지만,
        거대 저장소에서 느려지면 워커로 옮겨야 한다. (G4 예산 50ms)
        """
        resolved, branch = self._upstream(), self._current_branch()
        if resolved is None or branch is None or self._repo_path is None:
            return None
        from gitclient.infrastructure.remote_engine import RemoteEngine

        return RemoteEngine(self._repo_path).ahead_behind(branch, resolved[1])

    def _describe_transfer(self, stats) -> str:  # noqa: ANN001
        """계측 결과를 사람이 읽을 문장으로.

        전송량을 매번 보여주는 이유는 이 프로젝트의 목적함수가 누적 전송
        바이트이기 때문이다 — 사용자가 비용을 볼 수 있어야 판단할 수 있다.
        (performance.md §8.4)
        """
        sending = stats.kind is OperationKind.PUSH
        title = "올리기 완료" if sending else "가져오기 완료"
        idle_text = "올릴 것이 없습니다" if sending else "이미 최신입니다"

        if not stats.changed_anything:
            return f"{idle_text} ({stats.duration_ms}ms)"

        moved_bytes = stats.sent_bytes if sending else stats.received_bytes
        moved_objects = stats.sent_objects if sending else stats.received_objects

        parts = [f"{len(stats.ref_updates)}개 참조 갱신"]
        if moved_bytes:
            parts.append(f"{_format_bytes(moved_bytes)} 전송")
        elif moved_bytes == 0:
            # 참조만 바뀐 경우. 0바이트는 측정 실패가 아니라 측정된 사실이다.
            parts.append("객체 전송 없음")
        else:
            parts.append("전송량 측정 실패")
        if moved_objects:
            parts.append(f"객체 {moved_objects}개")
        parts.append(f"{stats.duration_ms}ms")
        return f"{title} — " + ", ".join(parts)

    def _on_ref_context_menu(self, pos) -> None:  # noqa: ANN001 - Qt 시그니처
        item = self._ref_list.itemAt(pos)
        if item is None:
            return
        kind = item.data(Qt.ItemDataRole.UserRole + 1)
        shorthand = item.data(Qt.ItemDataRole.UserRole + 2)
        is_head = bool(item.data(Qt.ItemDataRole.UserRole + 3))
        if kind != RefKind.LOCAL_BRANCH.value or shorthand is None:
            return

        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        if not is_head:
            checkout = menu.addAction(f"'{shorthand}' 브랜치로 전환")
            checkout.triggered.connect(
                lambda: self._submit_write(
                    f"브랜치 전환: {shorthand}",
                    lambda engine: engine.checkout_branch(shorthand),
                    reload_graph=True,
                )
            )
            delete = menu.addAction(f"'{shorthand}' 삭제...")
            delete.triggered.connect(lambda: self._confirm_delete_branch(shorthand))
        else:
            current = menu.addAction("현재 브랜치")
            current.setEnabled(False)
        menu.exec(self._ref_list.mapToGlobal(pos))

    def _confirm_delete_branch(self, shorthand: str) -> None:
        # §5.2 원칙 2: 무엇이 사라지는지 명시. 커밋 자체는 reflog에 남는다.
        answer = QMessageBox.warning(
            self,
            "브랜치 삭제",
            f"'{shorthand}' 브랜치를 삭제합니다.\n"
            "브랜치가 가리키던 커밋은 사라지지 않지만, 다른 브랜치에 속하지\n"
            "않은 커밋은 목록에서 보이지 않게 됩니다 (git reflog로 복구 가능).\n"
            "계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._submit_write(
            f"브랜치 삭제: {shorthand}",
            lambda engine: engine.delete_branch(shorthand),
            reload_graph=True,
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
