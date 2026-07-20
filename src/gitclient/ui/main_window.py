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

import gc
import logging
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QElapsedTimer,
    QEventLoop,
    QModelIndex,
    QSettings,
    Qt,
    QThreadPool,
    QTimer,
)
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QProgressBar,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QInputDialog,
    QMenu,
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
    PrefetchWorker,
    CloneWorker,
    FetchWorker,
    PullWorker,
    PushWorker,
    RemoteWorker,
    abort_merge_job,
    abort_operation_job,
    cherry_pick_job,
    continue_operation_job,
    resolve_conflict_job,
    fast_forward_job,
    rebase_job,
    reset_job,
    revert_job,
    skip_operation_job,
    merge_job,
)
from gitclient.application.refs_loader import RefsLoader
from gitclient.application.status_loader import StatusLoader
from gitclient.application.write_queue import WriteQueue
from gitclient.domain.errors import AuthenticationRequired, GitClientError
from gitclient.domain.instrumentation import OperationKind, TransferPhase
from gitclient.domain.models import (
    HistoryOutcome,
    OperationState,
    ResetKind,
    ConflictChoice,
    ConflictSide,
    MergeKind,
    MergeOutcome,
    Ref,
    RefKind,
    RepositoryInfo,
)
from gitclient.infrastructure.local_engine import LocalGitEngine
from gitclient.ui.clone_dialog import CloneDialog
from gitclient.ui.conflict_panel import ConflictPanel
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


# 창을 닫을 때 스레드풀을 기다리는 상한.
# 워커는 시그널이 끊겨 있어 이 위젯을 건드릴 수 없으므로 길게 잡을 이유가
# 없다. G4(50ms) 안에 들어가도록 짧게 둔다.
logger = logging.getLogger(__name__)

_CLOSE_DRAIN_MS = 20

# 배경으로 미리 받아오는 주기. 요금이 없으므로(ADR-56) 자주 받아도 비용은
# 회선 점유뿐이고, 그 대가로 사용자가 누를 때의 전송이 통째로 사라진다
# (실측: prefetch 뒤의 fetch는 받을 객체가 0개다).
#
# 5분은 "눌렀을 때 대개 이미 받아져 있다"와 "느린 회선을 계속 물고 있지
# 않는다" 사이의 값이다. git 자신의 maintenance prefetch는 1시간인데,
# 그쪽은 사용자가 지켜보지 않는 서버 작업이라 기준이 다르다.
_PREFETCH_INTERVAL_MS = 5 * 60 * 1000

# 저장소를 연 직후의 첫 실행. 열자마자 회선을 물면 사용자가 지금 하려는
# 일과 다투므로 조금 늦춘다.
_PREFETCH_FIRST_DELAY_MS = 30 * 1000

# 단계 이름은 사용자의 말로 옮긴다. "Resolving deltas"를 그대로 두면
# 무엇을 기다리는지 알 수 없고, 회선 탓인지 아닌지도 구분되지 않는다.
# **누가 일하고 있는지까지 말해야 한다.** 같은 단계라도 fetch와 push에서
# 주체가 반대다 — push의 Counting은 우리 CPU이고, `remote: Resolving deltas`는
# 서버다. 주체를 틀리면 "왜 느린가"에 대한 답이 정반대가 된다.
_PHASE_LABELS = {
    (TransferPhase.PREPARING, True): "원격이 준비하는 중",
    (TransferPhase.PREPARING, False): "보낼 것을 준비하는 중",
    (TransferPhase.RECEIVING, False): "받는 중",
    (TransferPhase.RECEIVING, True): "받는 중",
    (TransferPhase.SENDING, False): "보내는 중",
    (TransferPhase.SENDING, True): "보내는 중",
    (TransferPhase.APPLYING, True): "원격이 반영하는 중",
    (TransferPhase.APPLYING, False): "적용하는 중",
}



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
        # 진행 중인 병합의 충돌 목록. 비어 있으면 병합 중이 아니다.
        self._gc_saved: tuple[int, int, int] | None = None
        # 배경 prefetch. 사용자 작업 슬롯(_fetch_worker)과 **분리한다** —
        # 같이 쓰면 배경 작업이 도는 동안 사용자의 버튼이 잠긴다.
        self._prefetch_worker = None
        self._prefetch_timer = QTimer(self)
        self._prefetch_timer.timeout.connect(self._maybe_prefetch)
        self._merge_conflicts: tuple = ()
        # 진행 중인 히스토리 연산. 예전에는 `_merging` 불리언이었는데, 그
        # 표현으로는 리베이스 중인 저장소가 "아무 일도 없음"으로 보였다 —
        # pull도 열려 있고 충돌 라벨도 병합 기준이었다 (ADR-65).
        self._operation = OperationState()


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
        # 저장소가 없는 첫 화면에서도 복제는 눌러야 한다 — 오히려 그때
        # 가장 필요하다. 나머지 원격 액션은 저장소가 열려야 켜진다.
        self._update_remote_actions()
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

        # 충돌 패널은 **충돌이 있을 때만** 나타난다. 평소에 자리를 차지하면
        # 대부분의 시간 동안 쓸모없는 공간이 된다.
        self._conflict_panel = ConflictPanel()
        self._conflict_panel.resolve_requested.connect(self._on_resolve_conflict)
        self._conflict_panel.detail_requested.connect(self._on_conflict_selected)
        self._conflict_box = self._wrap("충돌 해결", self._conflict_panel)
        self._conflict_box.hide()

        # **진행 중인 연산 배너.** 저장소가 리베이스 중이라는 사실은
        # 저장소에 있는데, 예전에는 화면 어디에도 없었다. 무엇이 진행 중인지와
        # 빠져나가는 방법을 함께 보여주지 않으면 충돌 화면은 막다른 길이다.
        self._operation_label = QLabel("")
        self._operation_label.setWordWrap(True)
        self._continue_button = QPushButton("계속")
        self._continue_button.setToolTip("충돌을 모두 해결한 뒤 이어서 진행합니다")
        self._continue_button.clicked.connect(self._on_continue_operation)
        self._skip_button = QPushButton("이 커밋 건너뛰기")
        self._skip_button.setToolTip("멈춰 있는 커밋을 버리고 다음으로 넘어갑니다")
        self._skip_button.clicked.connect(self._on_skip_operation)
        self._abort_button = QPushButton("중단")
        self._abort_button.setToolTip("진행 중인 작업을 시작 이전으로 되돌립니다")
        self._abort_button.clicked.connect(self._on_abort_operation)

        banner_row = QHBoxLayout()
        banner_row.addWidget(self._operation_label, 1)
        for button in (self._continue_button, self._skip_button, self._abort_button):
            banner_row.addWidget(button)
        self._operation_banner = QWidget()
        self._operation_banner.setLayout(banner_row)
        self._operation_banner.hide()

        left_splitter = QSplitter(Qt.Orientation.Vertical)
        left_splitter.addWidget(self._conflict_box)
        left_splitter.addWidget(self._wrap("작업 디렉터리", self._work_panel))
        left_splitter.addWidget(self._wrap("참조", self._ref_list))
        left_splitter.setStretchFactor(0, 2)
        left_splitter.setStretchFactor(1, 1)
        left_splitter.setStretchFactor(2, 1)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.addWidget(left_splitter)
        main_splitter.addWidget(right_splitter)
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([260, 1020])

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.addWidget(self._operation_banner)
        central_layout.addWidget(main_splitter, 1)
        self.setCentralWidget(central)

        # 진행 표시. **느린 회선에서 이게 없으면 앱이 멎은 것처럼 보인다** —
        # 목적 함수가 "사용자가 기다리는 시간"이므로 얼마나 더 기다려야
        # 하는지를 보여주는 것이 곧 그 함수를 직접 겨냥한 기능이다.
        # (performance.md §8.4)
        self._progress_bar = QProgressBar()
        self._progress_bar.setMaximumWidth(160)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.hide()
        self.statusBar().addPermanentWidget(self._progress_bar)

        # **빠져나갈 길.** 대기 시간이 목적 함수인데(ADR-56) 진행 중인 작업을
        # 멈출 방법이 없으면, 잘못 누른 큰 clone 앞에서 사용자가 할 수 있는
        # 일은 앱을 강제 종료하는 것뿐이다. 취소 경로는 이미 있었고(저장소
        # 전환·창 닫기가 쓴다) 버튼만 없었다.
        self._cancel_button = QPushButton("중단")
        self._cancel_button.setToolTip("진행 중인 원격 작업을 멈춥니다")
        self._cancel_button.clicked.connect(self._on_cancel_remote)
        self._cancel_button.hide()
        self.statusBar().addPermanentWidget(self._cancel_button)

        # 전송량은 임시 메시지로 띄우면 곧바로 다른 메시지에 덮인다 — 특히
        # fetch 직후의 그래프 재로딩에. 사용자가 방금 치른 대기의 규모를
        # 계속 볼 수 있어야 하므로 고정 위젯에 둔다. (performance.md §8.4)
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
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(self._on_commit_context_menu)
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

        self._clone_action = QAction("복제 (Clone)...", self)
        self._clone_action.setToolTip("원격 저장소를 복제해 옵니다")
        self._clone_action.triggered.connect(self._on_clone)

        self._prefetch_action = QAction("배경에서 미리 가져오기", self)
        self._prefetch_action.setCheckable(True)
        self._prefetch_action.setToolTip(
            "기다리지 않는 동안 미리 받아둡니다 — 누를 때 전송이 이미 끝나 있습니다"
        )
        self._prefetch_action.setChecked(
            self._settings.value("prefetch_enabled", True, type=bool)
        )
        self._prefetch_action.toggled.connect(self._set_prefetch_enabled)

        self._abort_operation_action = QAction("진행 중인 작업 중단", self)
        self._abort_operation_action.setToolTip(
            "진행 중인 병합을 되돌립니다 (워킹 트리가 병합 이전으로 복구됩니다)"
        )
        self._abort_operation_action.triggered.connect(self._on_abort_operation)
        self._abort_operation_action.setEnabled(False)

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
        repo_menu.addAction(self._clone_action)
        repo_menu.addSeparator()
        repo_menu.addAction(self._fetch_action)
        repo_menu.addAction(self._pull_action)
        repo_menu.addAction(self._push_action)
        repo_menu.addSeparator()
        repo_menu.addAction(self._prefetch_action)
        repo_menu.addSeparator()
        repo_menu.addAction(self._abort_operation_action)
        repo_menu.addSeparator()
        repo_menu.addAction(self._branch_action)
        repo_menu.addSeparator()
        repo_menu.addAction(self._stash_action)
        repo_menu.addAction(self._stash_pop_action)

        toolbar = self.addToolBar("주요")
        toolbar.setMovable(False)
        toolbar.addAction(open_action)
        toolbar.addAction(reload_action)
        toolbar.addAction(self._clone_action)
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
        previous_path = self._repo_path
        if str(path) != self._repo_path:
            # 배경 prefetch는 사용자 작업이 없어도 저장소에 묶여 있다.
            # 놓아주지 않으면 이전 저장소를 향한 작업이 계속 돌며 풀 슬롯을
            # 잡고, 늦은 결과가 바뀐 화면에 도착한다.
            self._cancel_prefetch()

        if self._fetch_worker is not None and str(path) != self._repo_path:
            self._fetch_worker.cancel()
            self._fetch_worker = None
            # 워커를 놓아주면 정체 가드가 이후의 늦은 신호를 전부 걸러낸다 —
            # 막대를 치울 주체가 사라지므로 여기서 직접 치운다. 그러지 않으면
            # 이전 저장소의 진행 막대가 다음 원격 작업 때까지 남는다.
            self._reset_progress()

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
            # 병합은 **충돌해도 성공으로 끝난다** — 결과 값을 봐야 안다.
            queue.job_succeeded.connect(
                lambda _jid, name, result, q=queue: self._on_write_succeeded(
                    q, name, result
                )
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
        self._sync_operation_state()

        # 배경 미리 가져오기를 건다. 열자마자가 아니라 조금 뒤에 시작해
        # 사용자가 지금 하려는 일과 회선을 두고 다투지 않게 한다.
        # **같은 저장소를 다시 열 때는 일정을 건드리지 않는다.** fetch 후
        # 재로딩·F5가 open_repository를 반복 호출하는데, 매번 주기를 리셋하고
        # 취소 불가능한 일회성 타이머를 쌓으면 예약이 누적된다.
        if str(path) != previous_path or not self._prefetch_timer.isActive():
            self._prefetch_timer.stop()
            if self._has_remotes() and self._prefetch_enabled():
                self._prefetch_timer.start(_PREFETCH_INTERVAL_MS)
                # 일회성 타이머도 자식으로 만들어 창과 함께 죽게 한다.
                first = QTimer(self)
                first.setSingleShot(True)
                first.timeout.connect(self._maybe_prefetch)
                first.start(_PREFETCH_FIRST_DELAY_MS)

        self.setWindowTitle(f"{info.display_name} — Git Client")
        self._start_loading(path)

    def _reload(self) -> None:
        if self._engine is None:
            return
        self.open_repository(self._settings.value("last_repository"))

    # ------------------------------------------------------------------
    # 백그라운드 로딩
    # ------------------------------------------------------------------

    def _defer_gc(self) -> None:
        """로딩 동안 gen-2 수집을 미룬다.

        10만 커밋을 올리면 Commit/GraphRow가 대량으로 쌓이는데, 그때 파이썬의
        세대 2 GC가 **UI 스레드를 실측 128ms 멈춘다** — G4 예산(50ms)의 2.5배다.
        200배치 중 2회, 배치 번호까지 재현된다.

        레인 배치를 워커로 옮겨도 해결되지 않는다: GIL 아래에서 gen-2 수집은
        어느 스레드가 촉발하든 UI를 멈추고, 객체는 어느 스레드에서 만들어도
        모델에서 도달 가능하다.

        미루면 정지가 사라질 뿐 아니라 **로딩 자체도 빨라진다**(실측 약 20%) —
        수집 자체가 없어지므로. 정정된 목적 함수에서 트레이드오프가 아니다.
        """
        if self._gc_saved is not None:
            return  # 이미 미루는 중
        self._gc_saved = gc.get_threshold()
        gc.set_threshold(self._gc_saved[0], self._gc_saved[1], 1_000_000)

    def _restore_gc(self) -> None:
        """미뤄둔 gen-2 수집을 되돌린다.

        **모든 종료 경로에서 불러야 한다.** 하나라도 빠지면 gen-2가 영구
        비활성으로 남아 메모리가 무한히 늘어난다.
        """
        if self._gc_saved is None:
            return
        gc.set_threshold(*self._gc_saved)
        self._gc_saved = None
        gc.collect(1)  # 젊은 세대만 — 실측 1ms 미만으로 유계다

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
        self._defer_gc()

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
        self._restore_gc()
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
        self._restore_gc()
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
        self._restore_gc()
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
        self._prefetch_timer.stop()
        self._cancel_prefetch()
        # fetch는 네트워크에 매달려 있어 스스로 끝나기를 기다릴 수 없다.
        # 취소가 git 프로세스 트리를 끊으므로 전역 스레드풀 슬롯이 곧 풀린다.
        # 그러지 않으면 창이 사라진 뒤에도 프로세스가 최대 5분 남아, 사용자가
        # 앱을 다시 띄우면 두 인스턴스가 같은 계측 DB에 쓰게 된다.
        if self._fetch_worker is not None:
            worker = self._fetch_worker
            self._fetch_worker = None
            # **먼저 끊고 나서 취소한다.** 프로세스 종료가 비동기가 된 뒤로는
            # (§4.6.4) 워커가 곧바로 끝나지 않는다. 시그널이 붙어 있으면
            # 파괴 중인 위젯으로 늦은 결과가 날아오므로, 기다리는 대신
            # 연결을 끊어 그 위험 자체를 없앤다.
            # closeEvent는 **어떤 경우에도 예외를 내면 안 된다** — 여기서
            # 던지면 창이 닫히지 않고 사용자가 앱을 종료할 수 없다.
            try:
                signals = worker.signals
                for signal in (
                    signals.finished, signals.failed, signals.progressed
                ):
                    try:
                        signal.disconnect()
                    except (RuntimeError, TypeError):
                        pass  # 이미 끊겼거나 연결이 없었다
                worker.cancel()
            except Exception:  # noqa: BLE001
                logger.debug("원격 워커 정리 실패", exc_info=True)

        # 쓰기는 취소하지 않는다 (§3.3 규칙 5). 하지만 창이 먼저 사라지면
        # 그 작업의 성패를 보고할 곳도 없어진다 — 병합 중단이 실패해도
        # 아무도 모른 채 저장소가 중간 상태로 남는다. 그래서 짧게 기다려
        # 결과가 보고될 기회를 준다.
        #
        # **닫기를 무기한 미루지는 않는다.** event.ignore()로 붙잡으면 창이
        # 안 닫히는 것처럼 보이고, 그 사이 위젯이 밖에서 파괴되면 늦은
        # 시그널이 삭제된 객체에 꽂힌다. 기다림에 마감을 두는 편이 낫다.
        self._drain_writes(deadline_ms=3000)

        # 큐를 분리해 두면 닫힌 뒤 도착하는 늦은 idle/실패 시그널을
        # 정체 가드가 걸러낸다 — 파괴된 위젯을 건드리지 않는다.
        self._dispose_write_queue()
        # 여기서 오래 기다리지 않는다. 남은 워커는 시그널이 끊겼거나 정체
        # 가드에 걸려 이 위젯을 건드리지 못하고, git 프로세스는 별도 스레드가
        # 이미 끊고 있다. 2초를 걸어두면 비동기 종료가 끝나기를 기다리느라
        # 닫기가 실측 197ms 멈췄다 — 취소를 UI 스레드에서 뺀 이득이 닫기
        # 경로에서 그대로 돌아온 셈이었다.
        self._pool.waitForDone(_CLOSE_DRAIN_MS)
        super().closeEvent(event)

    def _update_status(self, info: RepositoryInfo | None) -> None:
        # 복제는 저장소가 열려 있지 않아도 실행된다 — 그 실패 경로에서
        # info가 None으로 들어온다. 원격 작업 중 처음 생긴 경우다.
        if info is None:
            self.statusBar().showMessage("저장소를 열어 주세요 (Ctrl+O)")
            return

        parts = [f"HEAD: {info.head_shorthand or '(unborn)'}"]
        parts.append(f"커밋 {self._commit_model.rowCount()}개")
        divergence = self._describe_divergence()
        if divergence:
            parts.append(divergence)
        # 아래 둘은 **지속되는 제약**이라 상시 표시한다. 임시 메시지로 한 번만
        # 띄우면 그래프 로딩 메시지에 덮이고, 정작 blame이 막히는 시점에는
        # 아무 단서도 남지 않는다. (전송량 표시에서 이미 겪은 문제)
        if info.is_shallow:
            parts.append("히스토리가 잘려 있습니다 (shallow)")
        if info.is_partial:
            parts.append("파일 내용을 지연 수신합니다 (오프라인 제약)")
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
            if queue is not self._write_queue:
                # 저장소를 바꾼 뒤 도착한 이전 저장소의 결과다. job_id만
                # 대조하면 통과해 버리고, `reload_graph`가 **지금 보고 있는**
                # 저장소의 재로딩 플래그를 세운다 — 다음 쓰기가 끝날 때
                # 화면이 통째로 다시 그려지며 선택과 스크롤이 초기화된다.
                _cleanup()
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
            if queue is not self._write_queue:
                _cleanup()
                return
            _cleanup()

        queue.job_succeeded.connect(_ok)
        queue.job_failed.connect(_fail)

    def _on_write_succeeded(
        self, queue: WriteQueue, name: str, result: object
    ) -> None:
        """쓰기 작업이 끝났다. 충돌을 남긴 결과만 들여다본다.

        **충돌은 실패 시그널로 오지 않는다** — git이 할 수 있는 만큼 합친
        정상 결과이기 때문이다. 그래서 여기서 값을 보고 분기한다.

        병합과 히스토리 연산이 서로 다른 결과 타입을 쓰는 이유는 마무리하는
        방법이 다르기 때문이다: 병합은 커밋으로, 나머지는 '계속'으로 끝난다.
        """
        if queue is not self._write_queue:
            return  # 교체된 저장소의 늦은 결과
        if isinstance(result, (MergeOutcome, HistoryOutcome)) and result.is_conflicted:
            self._enter_conflict_mode(result)

    def _enter_conflict_mode(self, outcome) -> None:  # noqa: ANN001
        """충돌 상태를 화면에 드러낸다.

        여기서 조용히 넘어가면 사용자는 워킹 트리에 충돌 마커가 든 줄도
        모른 채 작업을 이어간다 — 그 상태로 커밋하면 마커가 그대로 들어간다.
        """
        self._merge_conflicts = outcome.conflicts
        # **저장소에게 다시 묻는다.** 결과 객체에 연산 종류가 실려 오지만,
        # 배너·라벨·버튼을 세우려면 진행 정보(몇 번째 커밋인지)도 필요하고
        # 그것은 저장소에만 있다. 무엇보다 라벨을 여기서 손으로 정하면
        # 병합 기준 이름이 리베이스 화면에 남는 그 결함이 돌아온다.
        if self._engine is not None:
            try:
                self._operation = self._engine.operation_state(
                    conflict_count=len(self._merge_conflicts)
                )
            except GitClientError:
                # **조용히 넘어가면 안 된다.** 옛 값(대개 NONE)이 남으면
                # 충돌 패널은 뜨는데 배너는 숨고, 라벨은 중립 기본값으로
                # 떨어진다 — 리베이스 화면에서 "내 것 사용"이 다시 사용자
                # 커밋을 가리키고(ADR-65) 빠져나갈 버튼도 함께 사라진다.
                # 이 증분이 존재하는 이유 두 가지가 한 번에 무너지는 조합이라,
                # 결과 객체가 아는 만큼이라도 세워 둔다.
                self._operation = OperationState(
                    operation=outcome.operation,
                    conflict_count=len(self._merge_conflicts),
                )
        self._apply_operation_state()
        summary = ", ".join(c.path for c in outcome.conflicts[:3])
        if len(outcome.conflicts) > 3:
            summary += f" 외 {len(outcome.conflicts) - 3}개"

        # 마커가 없는 충돌(바이너리, 한쪽 삭제)에 "마커를 정리하라"고 하면
        # 사용자는 있지도 않은 것을 찾는다. 그대로 커밋하면 워킹 트리에 남은
        # 우리 것만 들어가 **상대 변경이 조용히 버려진다** — 그 파일들은
        # 어느 쪽을 남길지 직접 골라야 한다.
        markerless = [c.path for c in outcome.conflicts if not c.has_markers]
        detail = f"충돌한 파일: {summary}"
        if len(markerless) < len(outcome.conflicts):
            detail += (
                "\n\n워킹 트리의 해당 파일에 충돌 마커(<<<<<<< / >>>>>>>)가 "
                "들어 있습니다."
            )
        if markerless:
            names = ", ".join(markerless[:3])
            detail += (
                f"\n\n{names}에는 충돌 마커가 없습니다(바이너리이거나 한쪽이 "
                "삭제한 파일). 지금 워킹 트리에는 우리 쪽 내용만 있으므로, "
                "그대로 커밋하면 상대 변경이 버려집니다."
            )
        # 오류 경로로 보내지 않는다 — 충돌은 실패가 아니라 다음 할 일이 있는
        # 정상 상태다 (§4.10.1, ADR-38). 경고 아이콘에 "오류" 제목으로 띄우면
        # 사용자는 되돌려야 할 사고로 읽는다.
        # 마무리하는 방법이 연산마다 다르다. 병합에 맞춘 "커밋하면 완료"를
        # 리베이스에서 그대로 말하면 사용자는 커밋을 만들어 시퀀서를 어긋내고,
        # 그 다음에는 `--continue`도 통하지 않는다.
        if self._operation.operation.can_continue:
            finish = (
                f"파일을 정리한 뒤 스테이징하고 위쪽 '계속'을 누르면 "
                f"{self._operation.operation.label}이(가) 이어집니다."
            )
        else:
            finish = "파일을 정리한 뒤 스테이징하고 커밋하면 병합이 완료됩니다."
        self._notify(
            "충돌 해결 필요",
            f"충돌 {len(outcome.conflicts)}개를 해결해야 합니다.",
            detail=detail,
            action=f"{finish} 되돌리려면 위쪽 '중단'을 눌러 주세요.",
        )

    def _drain_writes(self, *, deadline_ms: int) -> None:
        """진행 중인 쓰기가 끝날 때까지 마감 시각까지만 기다린다.

        결과 시그널은 이벤트 루프로 배달되므로 그냥 블로킹하면 영영 오지
        않는다 — 이벤트를 돌리면서 기다려야 실패가 보고된다.
        """
        queue = self._write_queue
        if queue is None or not queue.is_busy:
            return
        self.statusBar().showMessage("진행 중인 작업을 마치는 중입니다...")
        timer = QElapsedTimer()
        timer.start()
        while queue.is_busy and timer.elapsed() < deadline_ms:
            QCoreApplication.processEvents(
                QEventLoop.ProcessEventsFlag.AllEvents, 50
            )

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
            # 여기서 진행 중 상태도 다시 읽는다. 다만 `open_repository`가
            # 초입에서 실패하면(저장소 삭제, index.lock 점유) 거기까지 가지
            # 못하고 플래그는 이미 소비돼 배너가 직전 상태로 굳는다.
            self.open_repository(self._repo_path)
            if self._engine is not None:
                return
            self._sync_operation_state()
            return
        # 쓰기 하나로 병합이 끝났을 수 있다(마지막 충돌을 해결한 커밋). 저장소에
        # 다시 물어야 중단 메뉴와 원격 액션이 실제 상태와 어긋나지 않는다.
        self._sync_operation_state()
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

        # 화면이 본 패치를 함께 넘긴다. 좌표는 이 패치 기준인데 엔진은 적용
        # 시점에 파일을 다시 읽으므로, 그 사이 외부 편집이 있었다면 같은
        # 좌표가 다른 줄을 가리킨다 — 고르지 않은 내용이 조용히 올라간다.
        seen = self._diff_model.patch
        expected = seen.fingerprint if seen is not None else None
        if staged:
            work = lambda engine: engine.unstage_partial(  # noqa: E731
                path, positions, expected_patch=expected
            )
        else:
            work = lambda engine: engine.stage_partial(  # noqa: E731
                path, positions, expected_patch=expected
            )

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
        # 사용자의 작업이 먼저다 — 배경 prefetch가 회선을 물고 있으면 놓는다.
        self._cancel_prefetch()

        self._remote_retry = retry
        worker.signals.finished.connect(
            lambda stats, w=worker: self._on_remote_finished(w, stats)
        )
        worker.signals.failed.connect(
            lambda error, w=worker: self._on_remote_failed(w, error)
        )
        worker.signals.progressed.connect(
            lambda snapshot, w=worker: self._on_remote_progress(w, snapshot)
        )
        self._fetch_worker = worker
        self._reset_progress()
        self._cancel_button.show()
        self._update_remote_actions()
        self.statusBar().showMessage(message)
        self._pool.start(worker)

    def _on_remote_progress(self, worker, snapshot) -> None:  # noqa: ANN001
        """진행 상황을 상태바에 그린다.

        **정체 가드가 필수다.** 진행률은 작업당 여러 번 오므로, 이전 작업의
        늦은 신호가 새 작업이 시작된 화면에 끼어들 수 있다 — finished와 달리
        한 번 걸러내는 것으로 끝나지 않는다.
        """
        if worker is not self._fetch_worker:
            return
        self._progress_bar.setVisible(True)
        if snapshot.percent is None or self._waiting_on_server(snapshot):
            # 총계를 모르거나(Enumerating) 서버 응답을 기다리는 구간 —
            # 확정 막대를 세워두면 멈춘 것처럼 보이므로 불확정으로 둔다.
            self._progress_bar.setRange(0, 0)
        else:
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(snapshot.percent)
        self.statusBar().showMessage(self._describe_progress(snapshot))

    @staticmethod
    def _waiting_on_server(snapshot) -> bool:  # noqa: ANN001
        """팩을 다 올리고 서버 응답을 기다리는 중인가.

        push의 가장 긴 대기가 여기다 — 마지막 바이트를 보낸 뒤 서버가
        연결성 검사와 훅을 도는 동안 git은 아무것도 내보내지 않는다(§4.6.3).
        그동안 "보내는 중 100%"를 세워두면 다 됐는데 멈춘 것처럼 보인다.
        """
        return (
            snapshot.phase is TransferPhase.SENDING and snapshot.percent == 100
        )

    def _describe_progress(self, snapshot) -> str:  # noqa: ANN001
        """진행 상태를 한 줄로. 아는 것만 말하고 모르는 것은 지어내지 않는다."""
        if self._waiting_on_server(snapshot):
            return "원격이 처리하는 중  (다 보냈습니다)"
        label = _PHASE_LABELS.get(
            (snapshot.phase, snapshot.remote_side), "진행 중"
        )
        parts = [label]
        if snapshot.percent is not None:
            parts.append(f"{snapshot.percent}%")
        elif snapshot.current is not None:
            parts.append(f"{snapshot.current}개")
        if snapshot.bytes_so_far:
            parts.append(_format_bytes(snapshot.bytes_so_far))
        if snapshot.bytes_per_s:
            parts.append(f"{_format_bytes(snapshot.bytes_per_s)}/s")
        return "  ".join(parts)

    def _on_cancel_remote(self) -> None:
        """진행 중인 원격 작업을 멈춘다.

        **확인을 받지 않는다.** 중단은 되돌릴 수 없는 작업이 아니다 —
        받다 만 팩은 버려지지만 저장소는 그대로고, 다시 누르면 된다.
        멈추고 싶어 누른 버튼에 다시 묻는 것은 그 자체로 방해다.
        """
        worker = self._fetch_worker
        if worker is None:
            return
        # 워커를 먼저 놓아준다 — 그래야 늦게 오는 결과가 정체 가드에 걸린다.
        self._fetch_worker = None
        self._remote_retry = None  # 취소했는데 인증을 되물으면 안 된다
        self._reset_progress()
        self._update_remote_actions()
        self.statusBar().showMessage("중단하는 중...")
        worker.cancel()

    def _reset_progress(self) -> None:
        """진행 표시를 치운다. 새 작업 시작과 작업 종료 양쪽에서 부른다."""
        self._cancel_button.hide()
        self._progress_bar.hide()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)

    # -- 배경 미리 가져오기 (ADR-7) --------------------------------------

    def _prefetch_enabled(self) -> bool:
        """배경 미리 가져오기를 켤 것인가.

        **기본은 켬이다** (ADR-7 정정) — 요금이 없으므로 미리 받는 것은
        총량이 늘어도 이득이다. 다만 기본으로 켜는 기능에는 **끌 수단이
        반드시 있어야 한다.** 회선을 아껴 써야 하는 순간(테더링, 화상회의
        중)이 사용자에게는 있고, 우리는 그것을 알 수 없다.
        """
        return self._settings.value("prefetch_enabled", True, type=bool)

    def _set_prefetch_enabled(self, enabled: bool) -> None:
        self._settings.setValue("prefetch_enabled", enabled)
        if enabled:
            if self._repo_path is not None and self._has_remotes():
                self._prefetch_timer.start(_PREFETCH_INTERVAL_MS)
        else:
            self._prefetch_timer.stop()
            self._cancel_prefetch()

    def _maybe_prefetch(self) -> None:
        """조건이 맞으면 배경으로 미리 받아온다.

        **사용자가 하는 일을 방해하지 않는 것이 첫 조건이다.** 원격 작업이
        진행 중이거나 쓰기가 걸려 있으면 회선과 저장소를 두고 다투게 되므로
        그냥 건너뛴다 — 다음 주기에 다시 온다.
        """
        if not self._prefetch_enabled():
            return
        if self._repo_path is None or not self._has_remotes():
            return
        if self._fetch_worker is not None or self._prefetch_worker is not None:
            return  # 사용자 작업 중이거나 이전 prefetch가 아직 돈다
        if self._write_queue is not None and self._write_queue.is_busy:
            return
        if self._operation.is_active:
            return  # 병합 중에는 저장소를 조용히 두는 편이 낫다

        remote = self._fetch_remote()
        if remote is None:
            return

        # 저장된 자격증명은 git의 helper가 공급한다. 배경 작업은 **묻지
        # 않으므로**(사용자를 방해하지 않는다) 저장된 값이 없으면 조용히
        # 실패하고 다음 주기에 다시 시도한다.
        worker = PrefetchWorker(self._repo_path, remote)
        self._prefetch_worker = worker
        # **결과를 화면에 알리지 않는다.** 사용자가 시작한 일이 아니므로
        # 성공해도 조용하고, 실패해도 모달을 띄우지 않는다 — 배경 작업이
        # 화면을 가로채면 그 자체가 방해다.
        worker.signals.finished.connect(
            lambda _stats, w=worker: self._on_prefetch_done(w)
        )
        worker.signals.failed.connect(
            lambda error, w=worker: self._on_prefetch_done(w, error)
        )
        worker.signals.retired.connect(
            lambda w=worker: self._on_prefetch_retired(w)
        )
        self._pool.start(worker)

    def _on_prefetch_done(self, worker, error=None) -> None:  # noqa: ANN001
        if worker is not self._prefetch_worker:
            return  # 저장소가 바뀐 뒤 도착한 늦은 결과
        # **여기서 슬롯을 놓지 않는다.** `finished`는 보고 시점이고 워커는
        # 아직 저장소 정리를 돈다. 지금 None으로 만들면 `_cancel_prefetch()`가
        # 부를 대상을 잃어 그 구간이 통째로 취소 불가가 된다 — 사용자의
        # fetch가 repack 위에서 돌고, 창을 닫아도 git이 살아남는다.
        if error is not None:
            # 조용히 삼킨다. 사용자가 요청하지 않은 작업의 실패로 흐름을
            # 끊지 않는다 — 다음 주기에 다시 시도한다.
            logger.debug("배경 prefetch 실패: %s", error)

    def _on_prefetch_retired(self, worker) -> None:  # noqa: ANN001
        """워커가 정리까지 마쳤다 — 이제야 슬롯을 놓는다."""
        if worker is self._prefetch_worker:
            self._prefetch_worker = None

    def _cancel_prefetch(self) -> None:
        """배경 작업을 즉시 양보시킨다.

        사용자가 원격 작업을 시작했거나 저장소를 바꿨을 때 부른다.
        **배경 작업 때문에 사용자가 기다리는 일은 없어야 한다.**
        투기적으로 받던 팩을 버리게 되지만, 애초에 없어도 되는 것이었다.
        """
        worker = self._prefetch_worker
        if worker is None:
            return
        self._prefetch_worker = None
        worker.cancel()

    def _update_remote_actions(self) -> None:
        """원격 액션의 활성 상태를 한 곳에서 정한다.

        판정이 흩어지면 "켜져 있는데 눌러도 아무 일 없는 버튼"이 생긴다 —
        실제로 겪은 결함이다.
        """
        idle = self._fetch_worker is None
        # 복제는 저장소가 열려 있지 않아도 된다 — 오히려 그때 가장 필요하다.
        # 다만 원격 작업 슬롯은 공유하므로 진행 중이면 잠근다.
        self._clone_action.setEnabled(idle)
        # **fetch는 병합 중에도 열어둔다** (v1.6 정정, ADR-43). 초안은 "헛된
        # 바이트"를 막으려 함께 잠갔는데, 요금이 없으므로 그건 비용이 아니고
        # (ADR-56) 오히려 사용자가 충돌을 푸는 동안 받아두면 나중 대기가
        # 줄어든다. pull·push는 저장소 상태가 거부하므로 계속 잠근다.
        available = self._has_remotes() and idle
        self._fetch_action.setEnabled(available)
        self._pull_action.setEnabled(available and not self._operation.is_active)
        # push는 워킹 트리가 있어야 의미가 있다(bare 저장소는 올릴 것이 없다).
        has_workdir = self._info is not None and self._info.workdir is not None
        self._push_action.setEnabled(
            available and has_workdir and not self._operation.is_active
        )

    def _on_remote_finished(self, worker, stats) -> None:  # noqa: ANN001
        if worker is not self._fetch_worker:
            return  # 이전 저장소의 늦은 결과
        self._fetch_worker = None
        self._reset_progress()
        self._update_remote_actions()
        self._transfer_label.setText(self._describe_transfer(stats))

        # pull은 여기서 끝나지 않는다 — 받은 것을 합쳐야 한다.
        if isinstance(worker, PullWorker):
            self._finish_pull()
            return

        # clone은 방금 만든 저장소를 열어 준다.
        if isinstance(worker, CloneWorker):
            self._finish_clone(worker)
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
        self._reset_progress()
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
        # **`_repo_path`를 요구하지 않는다.** 복제는 저장소가 열려 있지 않은
        # 상태에서 시작하는 유일한 원격 작업인데, 여기서 막으면 비공개
        # 저장소를 복제할 때 로그인 창이 아예 뜨지 않는다 — 인증이 필요한
        # 첫 순간에 기능이 통째로 사라진다.
        if retry is None:
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

    def _start_merge(self, shorthand: str, is_local: bool) -> None:
        """참조 하나를 현재 브랜치에 합친다.

        네트워크를 쓰지 않는다 — 이미 가진 것끼리 합치는 순수 로컬 작업이다.
        """
        branch = self._current_branch()
        if self._write_queue is None or branch is None:
            return
        prefix = "refs/heads/" if is_local else "refs/remotes/"
        self._write_queue.submit(
            f"병합: {shorthand}", merge_job(f"{prefix}{shorthand}", branch)
        )
        self._graph_reload_pending = True

    def _on_conflict_selected(self, path: str) -> None:
        """고른 파일의 양쪽 내용을 채운다.

        인덱스 스테이지를 읽는 짧은 작업이라 UI 스레드에서 한다 —
        상한이 파일 하나 크기로 알려져 있다 (§3.3).
        """
        if self._engine is None:
            return
        try:
            self._conflict_panel.show_detail(self._engine.conflict_detail(path))
        except GitClientError:
            # 그 사이 해결됐거나 저장소가 바뀌었다. **조용히 넘어가지 않는다** —
            # 그러면 이전 파일의 내용과 안내가 그대로 남아, 사용자가 A의
            # 설명을 읽으며 B를 해결하게 된다 (ADR-65와 같은 부류의 오도).
            self._conflict_panel.show_unavailable(path)

    def _on_resolve_conflict(self, path: str, choice) -> None:  # noqa: ANN001
        """한쪽을 골라 충돌을 해결한다.

        워킹 트리와 인덱스를 모두 건드리므로 WriteQueue를 거친다
        (§3.3 규칙 3).
        """
        if self._write_queue is None or self._engine is None:
            return

        # **손으로 고친 내용을 말없이 덮지 않는다.** 패널이 "직접 편집해
        # 해결할 수도 있다"고 안내해 놓고 버튼이 그 결과를 조용히 지우면,
        # 우리가 권한 방법을 우리가 파괴하는 셈이다.
        if self._working_copy_edited(path):
            answer = QMessageBox.warning(
                self,
                "편집한 내용 버리기",
                f"{path} 을(를) 직접 고치신 것 같습니다.\n\n"
                "한쪽을 고르면 그 편집 내용이 사라집니다. 계속할까요?",
                QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer is not QMessageBox.StandardButton.Discard:
                self._sync_operation_state()  # 잠가둔 버튼을 되살린다
                return

        label = "내 것" if choice is ConflictChoice.OURS else "상대 것"
        self._write_queue.submit(
            f"충돌 해결({label}): {path}",
            resolve_conflict_job(path, choice),
        )

    def _working_copy_edited(self, path: str) -> bool:
        """워킹 트리 파일이 충돌 직후 상태에서 벗어났는가.

        **마커 유무만으로는 판단할 수 없다.** 바이너리 충돌에는 애초에
        마커가 들어가지 않으므로, 그것만 보면 이 기능이 존재하는 이유인
        경우마다 확인창이 뜬다.

        그래서 두 가지를 함께 본다: 마커가 남아 있으면 손대지 않은 것이고,
        마커가 없더라도 내용이 양쪽 원본 중 하나와 같으면 git이 써둔 그대로다.
        둘 다 아닐 때만 사용자가 손을 댄 것으로 본다.
        """
        if self._repo_path is None or self._engine is None:
            return False
        try:
            target = Path(self._repo_path) / path
            if not target.exists():
                return False
            data = target.read_bytes()
        except OSError:
            return False
        if b"<<<<<<<" in data and b">>>>>>>" in data:
            return False  # 아직 마커가 그대로다
        try:
            detail = self._engine.conflict_detail(path)
        except GitClientError:
            return False
        return data not in (detail.ours.data, detail.theirs.data)

    def _sync_operation_state(self) -> None:
        """저장소에 진행 중인 연산이 있는지 확인해 UI 전체에 반영한다.

        앱을 껐다 켜거나 다른 저장소를 열어도 리베이스는 **저장소에 남아
        있다** — 메모리 상태만 믿으면 배너가 뜨지 않고 중단 메뉴도 꺼진 채로
        갇힌다. 그래서 저장소에게 묻는다.
        """
        if self._engine is None:
            self._merge_conflicts = ()
            self._operation = OperationState()
            self._apply_operation_state()
            return
        try:
            # **출처를 가리지 않는다.** 충돌은 병합에서만 생기지 않는다 —
            # stash pop도 만든다(실측: state는 NONE인데 인덱스에는 충돌).
            # 상태로 걸러내면 그 충돌이 화면에서 사라져, 사용자는 아무 안내
            # 없이 마커가 든 파일을 마주한다 (§13-2).
            self._merge_conflicts = self._engine.index_conflicts()
            # 충돌을 이미 셌으므로 넘겨준다 — 엔진이 다시 세면 UI 스레드에서
            # 같은 일을 두 번 하게 되고, 충돌이 많은 저장소에서 G4를 넘긴다.
            self._operation = self._engine.operation_state(
                conflict_count=len(self._merge_conflicts)
            )
        except GitClientError:
            self._merge_conflicts = ()
            self._operation = OperationState()
        self._apply_operation_state()

    def _apply_operation_state(self) -> None:
        """`self._operation`을 화면에 옮긴다. 이 함수만 위젯을 만진다."""
        state = self._operation
        active = state.is_active

        # **쓰기가 도는 동안에는 전부 잠근다.** 확인은 제출 **전에** 받는데
        # 제출 후에도 버튼이 살아 있으면 같은 확인을 두 번 통과할 수 있다.
        # `git rebase --skip`은 idempotent가 아니라서, 두 번 누르면 커밋이
        # **두 개** 사라진다 — 두 번째 확인창은 첫 번째와 같은 커밋을
        # 설명하지만 실제로 버려지는 것은 그 다음 커밋이다 (리뷰 실측).
        # 계속은 파괴적이진 않지만 성공 직후에 "이어서 진행할 작업이
        # 없습니다" 오류를 띄워 리베이스가 실패한 것처럼 보이게 한다.
        busy = self._write_queue is not None and self._write_queue.is_busy

        # 충돌 목록이 아니라 **저장소 상태**로 판단한다. 사용자가 마지막
        # 충돌을 해결해 스테이징하면 목록은 비지만 작업은 아직 진행 중이고,
        # 그때도 중단할 수 있어야 한다.
        # **중단할 수 있는 것만 중단을 내준다.** bisect처럼 우리가 다루지
        # 않는 상태에서 버튼을 내주면 누를 때마다 오류만 돌아온다.
        abortable = state.operation.can_abort
        self._abort_operation_action.setEnabled(abortable and not busy)
        self._abort_operation_action.setText(
            f"{state.operation.label} 중단" if active else "진행 중인 작업 중단"
        )
        self._abort_button.setVisible(abortable)

        self._operation_label.setText(state.summary())
        self._operation_banner.setVisible(active)

        # 병합은 `--continue`가 아니라 커밋으로 마무리한다. 버튼을 내주면
        # 누를 때마다 "이어서 진행할 작업이 없습니다"만 돌아온다.
        self._continue_button.setVisible(state.operation.can_continue)
        self._skip_button.setVisible(state.operation.can_continue)
        # 남은 충돌이 있으면 '계속'은 반드시 거부당한다 — 눌리게 두는 것은
        # 실패를 한 번 더 겪게 하는 것일 뿐이다.
        self._continue_button.setEnabled(not state.has_conflicts and not busy)
        self._skip_button.setEnabled(not busy)
        self._abort_button.setEnabled(not busy)

        # **라벨이 먼저다.** 목록보다 먼저 세워야 사용자가 첫 화면에서부터
        # 올바른 이름을 본다 (ADR-65).
        self._conflict_panel.set_labels(state.labels)
        self._conflict_panel.set_conflicts(self._merge_conflicts)
        self._conflict_box.setVisible(bool(self._merge_conflicts))
        self._update_remote_actions()

    # -- clone ----------------------------------------------------------

    def _on_clone(self) -> None:
        """원격 저장소를 복제한다.

        저장소가 열려 있지 않아도 되는 유일한 원격 작업이다 — 다른 작업들과
        달리 `_repo_path`를 요구하지 않는다. 다만 슬롯은 공유하므로 진행 중인
        원격 작업이 있으면 시작하지 않는다.
        """
        if self._fetch_worker is not None:
            return

        dialog = CloneDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        request = dialog.request()

        def build(credentials=None):  # noqa: ANN001, ANN202
            return CloneWorker(
                request.url,
                request.destination,
                filter_spec=request.filter_spec,
                depth=request.depth,
                credentials=credentials,
            )

        self._start_remote(
            build(),
            f"{request.destination.name}(으)로 복제하는 중...",
            retry=build,
        )

    def _finish_clone(self, worker) -> None:  # noqa: ANN001
        """복제가 끝나면 그 저장소를 연다.

        복제해 놓고 열지 않으면 사용자가 다시 "저장소 열기"를 눌러야 한다 —
        방금 어디에 받았는지 기억해야 하는 것도 부담이다.
        """
        destination = str(worker.destination)
        try:
            self.open_repository(destination)
        except GitClientError as exc:  # pragma: no cover - 방어적
            self._report(exc)

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
            # Phase 4: 이제 앱 안에서 합친다. 충돌하면 그 상태를 보여주고
            # 사용자가 해결하거나 중단할 수 있게 한다 — CLI로 내보내지 않는다.
            if self._write_queue is None:
                reload_and()
                return
            self._write_queue.submit("가져와 합치기", merge_job(upstream, branch))
            self._graph_reload_pending = True
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

        UI 스레드에서 부른다. pygit2 네이티브라 실측 0.002ms로 G4 예산
        (50ms)에 견줘 무시할 수 있다 — CLI subprocess로 하던 때는 19ms였다.
        """
        resolved, branch = self._upstream(), self._current_branch()
        if resolved is None or branch is None or self._engine is None:
            return None
        try:
            return self._engine.ahead_behind(branch, resolved[1])
        except GitClientError:
            return None

    def _describe_transfer(self, stats) -> str:  # noqa: ANN001
        """계측 결과를 사람이 읽을 문장으로.

        전송량을 매번 보여주는 이유는 이 프로젝트의 목적함수가 누적 전송
        바이트이기 때문이다 — 사용자가 비용을 볼 수 있어야 판단할 수 있다.
        (performance.md §8.4)
        """
        sending = stats.kind is OperationKind.PUSH
        cloning = stats.kind is OperationKind.CLONE
        title = "복제 완료" if cloning else ("올리기 완료" if sending else "가져오기 완료")
        idle_text = "올릴 것이 없습니다" if sending else "이미 최신입니다"

        if not stats.changed_anything and not cloning:
            return f"{idle_text} ({stats.duration_ms}ms)"

        moved_bytes = stats.sent_bytes if sending else stats.received_bytes
        moved_objects = stats.sent_objects if sending else stats.received_objects

        # 복제는 참조 갱신 줄을 fetch와 같은 형태로 내지 않는다 —
        # "0개 참조 갱신"은 사실이 아니라 파싱의 부재다. 말하지 않는다.
        parts = [] if cloning else [f"{len(stats.ref_updates)}개 참조 갱신"]
        if moved_bytes:
            parts.append(f"{_format_bytes(moved_bytes)} 전송")
        elif moved_bytes == 0:
            # 참조만 바뀐 경우. 0바이트는 측정 실패가 아니라 측정된 사실이다.
            parts.append("객체 전송 없음")
        elif moved_objects:
            # 객체는 왔는데 크기가 없다 — git이 작은 전송에는 크기를 붙이지
            # 않기 때문이다. "측정 실패"라고 하면 앱이 고장 난 것처럼 들리는데,
            # 실제로는 git이 말해주지 않은 것이다. 집계에는 여전히 미측정으로
            # 남지만 화면에서는 사실대로 적는다.
            parts.append("전송량 미보고")
        else:
            parts.append("전송량 측정 실패")
        if moved_objects:
            parts.append(f"객체 {moved_objects}개")
        parts.append(f"{stats.duration_ms}ms")
        return f"{title} — " + ", ".join(parts)

    def _ref_menu_entries(
        self, kind, shorthand: str, is_head: bool
    ) -> list[tuple[str, object]]:  # noqa: ANN001
        """참조 하나에 붙일 (라벨, 실행) 목록.

        메뉴 구성을 표시와 분리해 둔다 — 모달을 띄우지 않고 "무엇을 할 수
        있는가"를 확인할 수 있어야 테스트가 실제 사용자 경로를 검증한다.
        빈 목록이면 메뉴를 띄우지 않는다.
        """
        is_local = kind == RefKind.LOCAL_BRANCH.value
        is_remote = kind == RefKind.REMOTE_BRANCH.value
        if not (is_local or is_remote) or is_head:
            return []

        entries: list[tuple[str, object]] = []
        if is_local:
            entries.append((
                f"'{shorthand}' 브랜치로 전환",
                lambda: self._submit_write(
                    f"브랜치 전환: {shorthand}",
                    lambda engine: engine.checkout_branch(shorthand),
                    reload_graph=True,
                ),
            ))
        # 병합을 시작할 길이 pull 하나뿐이면, 원격을 따라가지 않는 로컬
        # 기능 브랜치는 앱 안에서 영영 합칠 수 없다 — 가장 흔한 병합인데도
        # 그렇다 (정합성 감사에서 확정된 기능 공백).
        entries.append((
            f"'{shorthand}'을(를) 현재 브랜치에 합치기",
            lambda: self._start_merge(shorthand, is_local),
        ))
        entries.append((
            f"'{shorthand}' 위로 현재 브랜치 리베이스...",
            lambda: self._start_rebase(shorthand, is_local),
        ))
        if is_local:
            entries.append((
                f"'{shorthand}' 삭제...",
                lambda: self._confirm_delete_branch(shorthand),
            ))
        return entries

    def _on_ref_context_menu(self, pos) -> None:  # noqa: ANN001 - Qt 시그니처
        item = self._ref_list.itemAt(pos)
        if item is None:
            return
        kind = item.data(Qt.ItemDataRole.UserRole + 1)
        shorthand = item.data(Qt.ItemDataRole.UserRole + 2)
        is_head = bool(item.data(Qt.ItemDataRole.UserRole + 3))
        if shorthand is None:
            return

        entries = self._ref_menu_entries(kind, shorthand, is_head)
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if not entries:
            if is_head and kind == RefKind.LOCAL_BRANCH.value:
                menu.addAction("현재 브랜치").setEnabled(False)
            else:
                return
        for label, run in entries:
            action = menu.addAction(label)
            if "합치기" in label or "리베이스" in label:
                action.setEnabled(
                    not self._busy_with_history()
                    and self._write_queue is not None
                )
            action.triggered.connect(run)
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

    def _notify(
        self, title: str, message: str, *, detail: str = "", action: str = ""
    ) -> None:
        """오류가 아닌 상태 전이를 알린다.

        충돌이 대표적이다 — git이 할 수 있는 만큼 합쳐 둔 **정상 결과**이지
        실패가 아니다 (§4.10.1, ADR-38). 경고 아이콘에 제목 "오류"로 띄우면
        사용자는 무언가 잘못됐다고 읽고, 실제로는 다음 할 일이 있을 뿐인데
        되돌리려 든다. 오류 경로와 채널을 나눠 그 오독을 막는다.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(title)
        box.setText(message)
        if action:
            box.setInformativeText(action)
        if detail:
            box.setDetailedText(detail)
        box.exec()

    # -- 진행 중인 히스토리 연산 (Phase 4 증분 3) -----------------------

    def _start_rebase(self, upstream: str, is_local: bool = True) -> None:
        """현재 브랜치를 다른 브랜치 위로 옮겨 심는다.

        §5.2 원칙 2에 따라 무엇이 벌어지는지 먼저 말한다. 병합과 달리
        **기존 커밋이 새 커밋으로 대체된다** — 이미 공유한 브랜치에서
        하면 다른 사람의 히스토리와 어긋난다.
        """
        branch = self._current_branch()
        if self._write_queue is None or branch is None:
            return
        answer = QMessageBox.question(
            self,
            "리베이스",
            f"'{branch}'의 커밋들을 '{upstream}' 위로 옮겨 심습니다.\n\n"
            "기존 커밋은 같은 내용의 새 커밋으로 대체됩니다. 이미 원격에\n"
            "올린 브랜치라면 다른 사람의 히스토리와 어긋나므로 강제 푸시가\n"
            "필요해집니다.\n\n계속할까요?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        # **완전한 참조를 넘긴다.** `_start_merge`와 같은 이유다 — shorthand는
        # 같은 이름의 태그가 있으면 모호하게 해석된다. 사용자가 브랜치를
        # 골랐는데 태그 위로 옮겨 심는 일이 있어서는 안 된다.
        prefix = "refs/heads/" if is_local else "refs/remotes/"
        self._submit_write(
            f"리베이스: {upstream}",
            rebase_job(f"{prefix}{upstream}", branch),
            reload_graph=True,
        )

    def _on_cherry_pick(self, sha: str) -> None:
        if self._write_queue is None:
            return
        self._submit_write(
            f"커밋 가져오기: {sha[:7]}",
            cherry_pick_job(sha, self._current_branch()),
            reload_graph=True,
        )

    def _on_revert(self, sha: str) -> None:
        if self._write_queue is None:
            return
        self._submit_write(
            f"되돌리는 커밋: {sha[:7]}",
            revert_job(sha, self._current_branch()),
            reload_graph=True,
        )

    def _on_reset(self, sha: str) -> None:
        """현재 브랜치를 고른 커밋으로 옮긴다.

        **세 방식의 위험도가 다르다.** 그 차이를 버튼 이름이 아니라 설명으로
        말한다 — soft/mixed/hard는 git을 아는 사람에게만 뜻이 통한다
        (§5.2 원칙 2).
        """
        if self._write_queue is None:
            return
        # **라벨 문자열을 키로 되받지 않는다.** 번역이나 오타 수정으로 라벨이
        # 한 글자만 달라져도 KeyError가 나고, 슬롯에서 난 예외는 트레이스백만
        # 찍힌 채 삼켜져 사용자에게는 "눌렀는데 아무 일도 안 일어남"이 된다.
        choices = (
            ("커밋만 되돌리기 (변경은 스테이징된 채로 남김)", ResetKind.SOFT),
            ("커밋과 스테이징 되돌리기 (파일 내용은 그대로)", ResetKind.MIXED),
            ("전부 버리기 (파일 내용까지 되돌림 — 되돌릴 수 없음)", ResetKind.HARD),
        )
        label, accepted = QInputDialog.getItem(
            self,
            "커밋으로 되돌리기",
            f"현재 브랜치를 {sha[:7]}(으)로 옮깁니다.\n어디까지 되돌릴까요?",
            [text for text, _ in choices],
            0,
            False,
        )
        if not accepted:
            return
        kind = next((k for text, k in choices if text == label), ResetKind.MIXED)
        if kind.discards_working_tree:
            # 커밋은 reflog에 남지만 **워킹 트리는 어디에도 남지 않는다.**
            # 그 비대칭이 이 연산의 진짜 위험이라 그것만 따로 말한다.
            answer = QMessageBox.warning(
                self,
                "전부 버리기",
                "커밋하지 않은 변경이 모두 사라집니다.\n\n"
                "커밋 자체는 git reflog로 되찾을 수 있지만, 커밋하지 않은\n"
                "작업은 어디에도 남지 않습니다. 이 작업은 되돌릴 수 없습니다.",
                QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Discard:
                return
        self._submit_write(
            f"되돌리기: {sha[:7]}",
            reset_job(sha, kind, self._current_branch()),
            reload_graph=True,
        )

    def _on_continue_operation(self) -> None:
        if self._write_queue is None:
            return
        self._submit_write(
            f"{self._operation.operation.label} 계속",
            continue_operation_job(),
            reload_graph=True,
        )
        self._apply_operation_state()  # 제출 즉시 버튼을 잠근다

    def _on_skip_operation(self) -> None:
        """멈춰 있는 커밋을 버리고 다음으로 넘어간다. 파괴적이라 확인한다."""
        if self._write_queue is None:
            return
        answer = QMessageBox.warning(
            self,
            "이 커밋 건너뛰기",
            "지금 멈춰 있는 커밋을 버리고 다음으로 넘어갑니다.\n\n"
            "그 커밋의 변경은 결과에 남지 않습니다.\n"
            "커밋 자체는 `git reflog`에 남아 터미널에서 되찾을 수 있습니다.",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Discard:
            return
        self._submit_write(
            f"{self._operation.operation.label} 건너뛰기",
            skip_operation_job(),
            reload_graph=True,
        )
        self._apply_operation_state()  # 제출 즉시 버튼을 잠근다

    def _on_abort_operation(self) -> None:
        """진행 중인 연산을 통째로 되돌린다.

        **되돌릴 수 없는 작업이다** — 충돌을 해결하던 내용도, 충돌 없이
        이미 반영된 변경도 사라진다. 그래서 확인을 받는다.
        """
        if self._write_queue is None:
            return
        operation = self._operation.operation
        if not operation.is_active:
            return
        answer = QMessageBox.warning(
            self,
            f"{operation.label} 중단",
            f"진행 중인 {operation.label}을(를) 되돌립니다.\n\n"
            "워킹 트리가 시작 이전 상태로 복구되며, 충돌을 해결하던 내용은 "
            "사라집니다.\n\n"
            "이미 만들어진 커밋은 `git reflog`에 남지만, 충돌을 풀며 편집한 "
            "내용은 커밋 전이라 어디에도 남지 않습니다.",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Discard:
            return
        self._merge_conflicts = ()
        self._submit_write(
            f"{operation.label} 중단", abort_operation_job(), reload_graph=True
        )
        # **위젯을 손으로 만지지 않는다.** 예전에는 메뉴 액션만 끄고 배너
        # 버튼과 충돌 패널은 그대로 뒀는데, 그러면 방금 비운 `_merge_conflicts`와
        # 화면의 목록이 어긋나 사용자가 유령 행에서 "내 것 사용"을 눌러
        # 중단 뒤에 해결 작업을 하나 더 큐에 넣을 수 있었다.
        self._apply_operation_state()

    def _busy_with_history(self) -> bool:
        """지금 새 히스토리 연산을 시작하면 안 되는가.

        **`_operation`만으로는 부족하다.** 그 값은 `_sync_operation_state()`가
        돌 때만 갱신되는데, 리베이스를 제출하고 서브프로세스가 도는 동안에는
        아직 한 번도 돌지 않았다 — 저장소는 이미 리베이스 중인데 화면의
        기록은 `NONE`이라 메뉴가 cherry-pick을 그대로 내줬다 (리뷰 실측).
        엔진이 결국 거부하므로 저장소는 안전하지만, 사용자에게는 "메뉴가
        있어서 눌렀는데 오류"가 된다.
        """
        if self._operation.is_active:
            return True
        return self._write_queue is not None and self._write_queue.is_busy

    def _commit_menu_entries(self, sha: str) -> list[tuple[str, object]]:
        """커밋 하나에 붙일 (라벨, 실행) 목록.

        `_ref_menu_entries`와 같은 이유로 표시와 분리한다 — 모달 없이
        "무엇을 할 수 있는가"를 확인할 수 있어야 테스트가 실제 경로를 본다.
        """
        if self._write_queue is None or self._busy_with_history():
            # 진행 중인 연산 위에 또 다른 연산을 얹으면 git이 거부하거나
            # (좋은 경우) 시퀀서 상태가 어긋난다. 아예 내주지 않는다.
            return []
        return [
            (f"'{sha[:7]}' 커밋 가져오기 (cherry-pick)",
             lambda: self._on_cherry_pick(sha)),
            (f"'{sha[:7]}' 되돌리는 커밋 만들기 (revert)",
             lambda: self._on_revert(sha)),
            (f"'{sha[:7]}'(으)로 되돌리기 (reset)...",
             lambda: self._on_reset(sha)),
        ]

    def _on_commit_context_menu(self, pos) -> None:  # noqa: ANN001 - Qt 시그니처
        index = self._commit_view.indexAt(pos)
        if not index.isValid():
            return
        commit = index.data(CommitRole.COMMIT)
        if commit is None:
            return
        sha = commit.sha
        entries = self._commit_menu_entries(sha)
        if not entries:
            return
        menu = QMenu(self)
        # **닫히면 스스로 사라지게 한다.** 그러지 않으면 우클릭 한 번마다
        # QMenu와 QAction이 MainWindow의 자식으로 쌓인다 (실측: 20회 우클릭에
        # 메뉴 20개, 액션 103개). 커밋 목록은 가장 자주 우클릭하는 곳이다.
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        for label, run in entries:
            menu.addAction(label).triggered.connect(run)
        menu.exec(self._commit_view.viewport().mapToGlobal(pos))
