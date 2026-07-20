"""히스토리 재작성 UI (Phase 4 증분 3).

엔진 테스트(test_history_ops.py)가 "저장소가 옳게 바뀌는가"를 본다면
여기서는 **사용자가 옳은 것을 보는가**를 본다. ADR-65의 손실은 저장소가
아니라 화면에서 일어났다 — git은 정확히 시킨 대로 했고, 화면이 잘못
시키게 만들었다.

여기서 검증하는 것:
  1. 리베이스 중 충돌 화면의 라벨이 실제 내용과 맞는가
  2. 진행 중인 연산이 화면에 드러나는가 — 앱을 다시 열어도
  3. 빠져나갈 길(계속·건너뛰기·중단)이 상태에 맞게 열리고 닫히는가
  4. 진행 중에 다른 연산을 시작할 수 없는가
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.models import RepoOperation
from gitclient.infrastructure.local_engine import LocalGitEngine
from gitclient.ui.main_window import MainWindow
from tests.integration.remote_harness import AUTHOR_ENV, git

TIMEOUT = 60_000

MINE = "내가-쓴-줄\n"
THEIRS = "상대가-쓴-줄\n"


def write(path: Path, text: str) -> None:
    """줄바꿈 번역 없이 쓴다 (윈도우의 `write_text`는 LF를 CRLF로 바꾼다)."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main과 topic이 같은 줄을 다르게 고친 저장소. HEAD는 topic."""
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    write(root / "f.txt", "base\n")
    commit_all(root, "base")
    git("checkout", "--quiet", "-b", "topic", cwd=root)
    write(root / "f.txt", MINE)
    commit_all(root, "topic-commit")
    git("checkout", "--quiet", "main", cwd=root)
    write(root / "f.txt", THEIRS)
    commit_all(root, "main-commit")
    git("checkout", "--quiet", "topic", cwd=root)
    return root


@pytest.fixture
def window(qtbot, repo: Path):  # noqa: ANN001, ANN201
    w = MainWindow()
    qtbot.addWidget(w)
    errors: list = []
    w._report = errors.append
    w.reported_errors = errors
    notices: list = []
    w._notify = lambda title, message, **kw: notices.append(
        {"title": title, "message": message, **kw}
    )
    w.notices = notices
    w.open_repository(str(repo))
    qtbot.waitUntil(lambda: not w._loading, timeout=TIMEOUT)
    return w


def settle(window, qtbot) -> None:  # noqa: ANN001
    qtbot.waitUntil(
        lambda: window._write_queue is None or not window._write_queue.is_busy,
        timeout=TIMEOUT,
    )


def start_merge(repo: Path) -> None:
    """저장소를 병합 충돌 상태로 만든다.

    하네스의 `git()`은 실패를 예외로 올리는데 충돌 병합은 종료 코드 1이라
    쓸 수 없다. 엔진을 쓰면 충돌이 **정상 결과**로 돌아온다 (ADR-38).
    """
    git("checkout", "--quiet", "main", cwd=repo)
    LocalGitEngine.open(repo).merge("refs/heads/topic", "main")


def start_rebase(repo: Path) -> None:
    """저장소를 리베이스 충돌 상태로 만든다 — UI 밖에서."""
    LocalGitEngine.open(repo).rebase("main")


# ----------------------------------------------------------------------
# 1. 라벨 — ADR-65가 남긴 결함의 화면 쪽
# ----------------------------------------------------------------------


def test_conflict_panel_labels_follow_the_operation(window, repo: Path, qtbot) -> None:
    """리베이스 충돌 화면은 병합 기준 이름을 쓰지 않는다.

    **이것이 증분 2에서 데이터를 잃게 한 그 지점이다.** 화면이 "내 것"이라
    부른 쪽에 사용자의 것이 없었고, 그대로 고르면 자기 커밋이 사라졌다.
    """
    start_rebase(repo)
    window._sync_operation_state()

    panel = window._conflict_panel
    assert "upstream" in panel._ours_title.text()
    assert "내 커밋" in panel._theirs_title.text()
    assert panel._note.text(), "방향이 병합과 반대라는 사실을 말해야 한다"


def test_labelled_button_takes_the_content_it_names(window, repo: Path, qtbot) -> None:
    """버튼 이름과 실제로 남는 내용이 일치하는가 — 끝까지 확인한다.

    라벨만 고치고 버튼이 반대쪽을 집으면 결함은 그대로다. 화면 문구가
    아니라 **파일 내용**으로 판정한다.
    """
    start_rebase(repo)
    window._sync_operation_state()
    panel = window._conflict_panel

    mine_button = panel._take_theirs  # '재생 중인 내 커밋 사용'
    assert "내 커밋" in mine_button.text()

    window._on_conflict_selected("f.txt")
    qtbot.waitUntil(lambda: panel._current_path == "f.txt", timeout=TIMEOUT)
    mine_button.click()
    settle(window, qtbot)

    assert (repo / "f.txt").read_text(encoding="utf-8") == MINE


def test_merge_labels_stay_merge_shaped(window, repo: Path, qtbot) -> None:
    """리베이스에 맞춰 전부 뒤집지 않았는지 — 병합은 그대로여야 한다."""
    start_merge(repo)
    window._sync_operation_state()

    assert window._operation.operation is RepoOperation.MERGE
    assert "내 것" in window._conflict_panel._ours_title.text()


# ----------------------------------------------------------------------
# 2. 배너 — 진행 중이라는 사실이 화면에 있는가
# ----------------------------------------------------------------------


def test_banner_is_hidden_when_nothing_is_running(window) -> None:
    assert window._operation_banner.isHidden()
    assert not window._operation.is_active


def test_banner_appears_for_a_rebase_started_outside_the_app(
    window, repo: Path
) -> None:
    """앱을 껐다 켜도 리베이스는 저장소에 남아 있다.

    메모리 상태만 믿으면 배너가 뜨지 않아 사용자는 자기 저장소가 리베이스
    중인 줄도 모른 채 커밋한다 — 그러면 시퀀서가 어긋난다.
    """
    start_rebase(repo)
    window._sync_operation_state()

    assert not window._operation_banner.isHidden()
    assert "리베이스" in window._operation_label.text()
    assert "topic" in window._operation_label.text(), "돌아갈 브랜치를 보여야 한다"
    assert "충돌 1개" in window._operation_label.text()


def test_continue_is_blocked_until_conflicts_are_resolved(
    window, repo: Path, qtbot
) -> None:
    """남은 충돌이 있으면 '계속'은 반드시 거부당한다 — 눌리게 두지 않는다."""
    start_rebase(repo)
    window._sync_operation_state()
    assert not window._continue_button.isEnabled()

    window._on_conflict_selected("f.txt")
    qtbot.waitUntil(
        lambda: window._conflict_panel._current_path == "f.txt", timeout=TIMEOUT
    )
    window._conflict_panel._take_theirs.click()
    settle(window, qtbot)
    window._sync_operation_state()

    assert window._continue_button.isEnabled()


def test_merge_gets_no_continue_button(window, repo: Path) -> None:
    """병합은 '계속'이 아니라 커밋으로 마무리한다.

    버튼을 내주면 누를 때마다 "이어서 진행할 작업이 없습니다"만 돌아온다.
    """
    start_merge(repo)
    window._sync_operation_state()

    assert window._continue_button.isHidden()
    assert window._skip_button.isHidden()
    assert not window._operation_banner.isHidden(), "중단 경로는 남아야 한다"


def test_abort_action_names_the_operation(window, repo: Path) -> None:
    """"병합 중단"이라는 이름이 리베이스에 붙어 있으면 안 된다."""
    start_rebase(repo)
    window._sync_operation_state()

    assert window._abort_operation_action.isEnabled()
    assert "리베이스" in window._abort_operation_action.text()


# ----------------------------------------------------------------------
# 3. 진행 중에는 새 연산을 시작할 수 없다
# ----------------------------------------------------------------------


def test_commit_menu_is_empty_during_an_operation(window, repo: Path) -> None:
    """리베이스 중 cherry-pick을 내주면 git이 거부하거나 상태가 어긋난다."""
    head = git("rev-parse", "main", cwd=repo).stdout.strip()
    assert window._commit_menu_entries(head), "평소에는 메뉴가 있어야 한다"

    start_rebase(repo)
    window._sync_operation_state()

    assert window._commit_menu_entries(head) == []


def test_remote_actions_are_locked_during_a_rebase(window, repo: Path) -> None:
    """리베이스 중 pull은 병합 중 pull과 똑같이 위험하다.

    예전 `_merging` 불리언은 리베이스를 "아무 일도 없음"으로 봐서 pull이
    열려 있었다.
    """
    start_rebase(repo)
    window._sync_operation_state()

    assert not window._pull_action.isEnabled()


def test_ref_menu_offers_rebase(window, repo: Path) -> None:
    """리베이스를 시작할 길이 화면에 있는가."""
    entries = window._ref_menu_entries("local_branch", "main", False)
    assert any("리베이스" in label for label, _ in entries)


# ----------------------------------------------------------------------
# 4. 끝까지 — 충돌 해결 후 계속하면 저장소가 정리되는가
# ----------------------------------------------------------------------


def test_continue_finishes_the_rebase(window, repo: Path, qtbot) -> None:
    start_rebase(repo)
    window._sync_operation_state()

    window._on_conflict_selected("f.txt")
    qtbot.waitUntil(
        lambda: window._conflict_panel._current_path == "f.txt", timeout=TIMEOUT
    )
    window._conflict_panel._take_theirs.click()
    settle(window, qtbot)

    window._on_continue_operation()
    settle(window, qtbot)
    window._sync_operation_state()

    assert window.reported_errors == []
    assert window._operation.operation is RepoOperation.NONE
    assert window._operation_banner.isHidden()
    assert git("log", "--format=%s", cwd=repo).stdout.splitlines() == [
        "topic-commit",
        "main-commit",
        "base",
    ]


# ----------------------------------------------------------------------
# 5. 두 번 눌러도 한 번만 일어나야 한다 (UI 리뷰에서 확정된 결함)
# ----------------------------------------------------------------------


@pytest.fixture
def confirmed(monkeypatch):
    """파괴적 확인창을 '진행'으로 자동 응답한다.

    헤드리스에서 모달을 그대로 두면 `exec()`가 영영 돌아오지 않아 테스트가
    멈춘다. 확인 절차 자체는 별도 테스트가 본다.
    """
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Discard
    )
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Ok
    )
    return monkeypatch


def test_skip_cannot_be_pressed_twice(window, repo: Path, qtbot, confirmed) -> None:
    """**두 번 누르면 커밋이 두 개 사라졌다.**

    확인창은 제출 **전에** 뜨는데 제출 후 버튼이 잠기지 않았다.
    `git rebase --skip`은 idempotent가 아니라서, 두 번째 확인창이 설명하는
    커밋(첫 번째와 같은 것)과 실제로 버려지는 커밋(그 다음 것)이 다르다.
    """
    start_rebase(repo)
    window._sync_operation_state()
    assert window._skip_button.isEnabled()

    window._on_skip_operation()

    assert not window._skip_button.isEnabled(), (
        "제출 직후에도 눌린다면 확인을 두 번 통과할 수 있다"
    )
    assert not window._continue_button.isEnabled()
    assert not window._abort_button.isEnabled()

    settle(window, qtbot)
    window._sync_operation_state()
    assert window._operation.operation is RepoOperation.NONE


def test_banner_buttons_unlock_after_the_write_finishes(
    window, repo: Path, qtbot
) -> None:
    """잠금이 풀리지 않으면 그것대로 막다른 길이다."""
    start_rebase(repo)
    window._sync_operation_state()
    window._on_conflict_selected("f.txt")
    qtbot.waitUntil(
        lambda: window._conflict_panel._current_path == "f.txt", timeout=TIMEOUT
    )

    window._conflict_panel._take_theirs.click()
    settle(window, qtbot)
    window._sync_operation_state()

    assert window._continue_button.isEnabled()
    assert window._abort_button.isEnabled()


def test_conflict_mode_keeps_the_operation_when_state_lookup_fails(
    window, repo: Path
) -> None:
    """상태 조회가 실패해도 라벨과 배너를 잃지 않는다.

    조용히 넘어가면 `_operation`이 NONE으로 남아 **충돌 패널은 뜨는데 배너는
    숨고 라벨은 중립 기본값으로 떨어진다** — 리베이스 화면에서 "내 것 사용"이
    다시 사용자 커밋을 가리키고(ADR-65) 빠져나갈 버튼도 사라진다.
    """
    from gitclient.domain.errors import EngineError
    from gitclient.domain.models import HistoryOutcome, HistoryOutcomeKind

    from gitclient.domain.models import OperationState

    start_rebase(repo)
    window._sync_operation_state()
    conflicts = window._merge_conflicts
    # **여기가 핵심이다.** `_operation`을 비워 두지 않으면 "조용히 넘어가도
    # 앞서 sync가 채운 값이 남아" 테스트가 통과한다 — 변이 검증이 그것을
    # 잡았다. 실제 위험한 순간은 sync가 아직 돌지 않았을 때다.
    window._operation = OperationState()

    def boom(**_kwargs):
        raise EngineError("상태 조회 실패")

    window._engine.operation_state = boom
    window._enter_conflict_mode(
        HistoryOutcome(
            kind=HistoryOutcomeKind.CONFLICTED,
            operation=RepoOperation.REBASE,
            conflicts=conflicts,
        )
    )

    assert window._operation.operation is RepoOperation.REBASE
    assert not window._operation_banner.isHidden(), "빠져나갈 길이 남아야 한다"
    assert "내 커밋" in window._conflict_panel._theirs_title.text()


def test_menus_close_while_a_history_write_is_in_flight(
    window, repo: Path, qtbot
) -> None:
    """제출은 했는데 아직 sync가 돌지 않은 구간에도 메뉴를 내주면 안 된다.

    `_operation`은 sync 때만 갱신되므로, 리베이스 서브프로세스가 도는 동안
    저장소는 이미 리베이스 중인데 화면의 기록은 NONE이다.
    """
    head = git("rev-parse", "main", cwd=repo).stdout.strip()
    assert window._commit_menu_entries(head)

    window._submit_write("느린 작업", lambda engine: git(
        "rev-parse", "HEAD", cwd=repo
    ))

    assert window._commit_menu_entries(head) == [], (
        "큐가 도는 동안에는 새 연산을 내주지 않는다"
    )
    settle(window, qtbot)


def test_abort_clears_the_conflict_panel_immediately(
    window, repo: Path, qtbot, confirmed
) -> None:
    """중단을 제출하면 화면이 곧바로 그 사실을 반영해야 한다.

    필드만 비우고 위젯을 그대로 두면 사용자가 유령 행에서 "사용"을 눌러
    중단 뒤에 해결 작업을 하나 더 큐에 넣는다.
    """
    start_rebase(repo)
    window._sync_operation_state()
    assert window._conflict_panel._list.count() == 1

    window._on_abort_operation()

    assert window._conflict_panel._list.count() == 0, "유령 행이 남았다"
    assert not window._abort_operation_action.isEnabled()
    settle(window, qtbot)


def test_rebase_is_given_a_full_ref(window, repo: Path, confirmed) -> None:
    """shorthand는 같은 이름의 태그가 있으면 모호하다.

    `_start_rebase`가 만든 job을 가짜 엔진에 태워 **엔진이 실제로 받는 문자열**을
    본다. 제출 이름만 보면 shorthand를 넘겨도 통과한다.
    """
    captured: list = []
    confirmed.setattr(
        window, "_submit_write",
        lambda name, work, **kw: captured.append(work),
    )
    window._start_rebase("main", True)

    assert captured, "제출되지 않았다"

    class Spy:
        def rebase(self, upstream_ref, *, expected_branch=None):
            self.seen = upstream_ref
            return None

    spy = Spy()
    captured[0](spy)
    assert spy.seen == "refs/heads/main", spy.seen


# ----------------------------------------------------------------------
# 6. 자동 동기화 — 손으로 sync를 부르지 않는다
# ----------------------------------------------------------------------
#
# **위의 테스트 대부분은 조작 뒤에 `_sync_operation_state()`를 직접 부른다.**
# 그래서 "상태를 읽으면 화면이 맞다"만 검증하고, **읽는 일이 저절로
# 일어나는가**는 검증하지 않는다 — 그 연결이 끊어져도 전부 초록으로 남는다.
# 아래 둘은 그 연결만 본다. (UI 리뷰에서 지적된 공백)


def test_banner_clears_without_a_manual_sync(window, repo: Path, qtbot) -> None:
    """리베이스를 끝내면 배너가 **저절로** 걷힌다.

    경로: `_ok` → `_graph_reload_pending` → 큐 idle → `open_repository`
    → `_sync_operation_state`. 이 사슬 중 하나만 끊겨도 사용자는 이미 끝난
    작업의 배너를 계속 보게 된다.
    """
    start_rebase(repo)
    window._sync_operation_state()  # 앱 밖에서 만든 상태를 한 번 들여온다
    window._on_conflict_selected("f.txt")
    qtbot.waitUntil(
        lambda: window._conflict_panel._current_path == "f.txt", timeout=TIMEOUT
    )
    window._conflict_panel._take_theirs.click()
    settle(window, qtbot)

    window._on_continue_operation()

    # 여기서부터 **손으로 sync를 부르지 않는다.** 다만 기다리는 대상은
    # 배너가 아니라 "작업이 끝났는가"다 — 배너를 기다리면 작업이 실패해
    # 배너가 영영 남는 경우에 60초 타임아웃만 보고 끝나, 정작 원인인
    # `reported_errors`를 아무도 읽지 않는다.
    settle(window, qtbot)
    qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

    assert window.reported_errors == []
    assert window._operation.operation is RepoOperation.NONE
    assert window._operation_banner.isHidden(), (
        "작업이 끝났는데 배너가 남았다 — 자동 동기화 사슬이 끊겼다"
    )


def test_banner_appears_without_a_manual_sync(
    window, repo: Path, qtbot, confirmed
) -> None:
    """앱 안에서 시작한 리베이스가 충돌하면 배너가 저절로 뜬다."""
    window._start_rebase("main", True)

    qtbot.waitUntil(
        lambda: not window._operation_banner.isHidden(), timeout=TIMEOUT
    )
    assert window._operation.operation is RepoOperation.REBASE
    assert "내 커밋" in window._conflict_panel._theirs_title.text()
    assert window.notices, "충돌은 사용자에게 알려야 한다"


def test_failed_detail_does_not_leave_the_previous_file_on_screen(
    window, repo: Path, qtbot
) -> None:
    """내용을 못 읽으면 그 사실을 말한다 — 이전 파일을 남기지 않는다.

    조용히 넘어가면 선택은 B인데 화면은 A의 내용·A의 안내·A 기준으로 켜진
    버튼을 유지한다. 사용자는 A용 설명을 읽고 **B를** 해결한다.
    """
    from gitclient.domain.errors import EngineError

    start_rebase(repo)
    window._sync_operation_state()
    window._on_conflict_selected("f.txt")
    qtbot.waitUntil(
        lambda: window._conflict_panel._current_path == "f.txt", timeout=TIMEOUT
    )
    panel = window._conflict_panel
    assert panel._ours.toPlainText(), "먼저 내용이 채워져 있어야 한다"

    def boom(_path):
        raise EngineError("읽기 실패")

    window._engine.conflict_detail = boom
    window._on_conflict_selected("f.txt")

    assert panel._ours.toPlainText() == "", "이전 내용이 남았다"
    assert not panel._take_ours.isEnabled()
    assert not panel._take_theirs.isEnabled()
    assert "읽지 못했습니다" in panel._hint.text()


def test_list_row_names_the_right_actor(window, repo: Path, qtbot) -> None:
    """**목록 행의 주체 이름도 연산을 따른다.**

    버튼 라벨만 고치고 목록 라벨을 상수 표에 남겨 뒀을 때, 리베이스에서
    "upstream이 지운" 파일을 "내가 지움"이라 불렀다 — 사실과 정반대이고,
    그것을 믿고 고르면 ADR-65와 같은 경로로 사용자 커밋이 사라진다.
    감사에서 실측으로 재현된 결함이다.
    """
    # 삭제/수정 충돌을 만들려면 **공통 조상에 파일이 있어야 한다.**
    # 그러지 않으면 한쪽의 단순 추가라 충돌이 나지 않는다.
    root = repo.parent / "delmod"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    write(root / "gone.txt", "base\n")
    commit_all(root, "base")
    git("checkout", "--quiet", "-b", "topic", cwd=root)
    write(root / "gone.txt", "내가 고친 내용\n")
    commit_all(root, "내가 고침")
    git("checkout", "--quiet", "main", cwd=root)
    git("rm", "--quiet", "gone.txt", cwd=root)
    commit_all(root, "upstream이 지움")
    git("checkout", "--quiet", "topic", cwd=root)

    window.open_repository(str(root))
    qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)
    LocalGitEngine.open(root).rebase("main")
    window._sync_operation_state()

    rows = [
        window._conflict_panel._list.item(i).text()
        for i in range(window._conflict_panel._list.count())
    ]
    gone = next(r for r in rows if "gone.txt" in r)

    assert "upstream 쪽이 지움" in gone, gone
    assert "내 커밋 쪽이 고침" in gone, gone
    assert "내가 지움" not in gone, "병합 기준 주체가 리베이스 목록에 남았다"


def test_merge_list_rows_keep_merge_actors(window, repo: Path) -> None:
    """리베이스에 맞춰 전부 뒤집지 않았는지 — 병합 목록은 그대로여야 한다."""
    start_merge(repo)
    window._sync_operation_state()

    rows = [
        window._conflict_panel._list.item(i).text()
        for i in range(window._conflict_panel._list.count())
    ]
    assert rows, "충돌 행이 있어야 한다"
    assert all("upstream" not in r for r in rows), rows


def test_panel_survives_either_call_order(qtbot) -> None:
    """`set_labels`와 `set_conflicts`의 **순서에 의존하지 않는다.**

    지금 유일한 호출자는 라벨을 먼저 세우지만, 공개 API가 반대 순서를
    허용하는 한 다음 호출자가 반대로 부를 수 있다. 그때 목록 행만 옛 주체
    이름으로 남으면 화면 안에서 두 문장이 서로를 반박한다 — H1이 정확히
    그 모습이었다.
    """
    from gitclient.domain.models import (
        ConflictedFile,
        ConflictSide,
        RepoOperation,
        conflict_labels,
    )
    from gitclient.ui.conflict_panel import ConflictPanel

    panel = ConflictPanel()
    qtbot.addWidget(panel)
    conflicts = (
        ConflictedFile(path="x.txt", side=ConflictSide.DELETED_BY_US),
    )

    # 반대 순서: 목록을 먼저 채우고 라벨을 나중에 바꾼다
    panel.set_conflicts(conflicts)
    panel.set_labels(conflict_labels(RepoOperation.REBASE))

    row = panel._list.item(0).text()
    assert "upstream 쪽이 지움" in row, row
    assert "내가 지움" not in row, "옛 주체 이름이 목록에 남았다"


# ----------------------------------------------------------------------
# 7. 감사에서 확정된 UI 결함들
# ----------------------------------------------------------------------


def test_broken_upstream_config_does_not_kill_the_app(window, repo: Path) -> None:
    """**앱이 죽던 경로다.**

    `_upstream()`이 던지면 호출부 넷이 try 밖이라 Qt 슬롯까지 올라가고,
    이 앱에는 `sys.excepthook`이 없어 프로세스가 그대로 끝난다. 상태바
    갱신 경로라 새로 고침마다 지나간다 (감사 실측).
    """
    from gitclient.domain.errors import EngineError

    def boom():
        raise EngineError("upstream 조회 실패")

    window._engine.upstream_of_head = boom

    assert window._ahead_behind() is None
    window._describe_divergence()   # 던지면 여기서 실패한다
    window._update_remote_actions()
    window._refresh_status()


def test_commit_is_locked_during_a_sequencer(window, repo: Path) -> None:
    """시퀀서 중 커밋은 막다른 길로 이어진다.

    git은 받아 주지만 이후 '계속'이 "가져올 변경이 없으니 건너뛰라"며
    방금 만든 커밋을 버리라고 안내한다 (감사 실측, design.md §4.12.4).
    """
    panel = window._work_panel
    start_rebase(repo)
    window._sync_operation_state()

    assert not panel._commit_allowed, "리베이스 중 커밋이 열려 있다"
    assert panel._unstaged_list.isEnabled(), "스테이징까지 막으면 해결을 못 한다"


def test_commit_stays_open_during_a_merge(window, repo: Path) -> None:
    """병합은 반대다 — 커밋이 **유일한** 마무리 수단이라 열려 있어야 한다."""
    start_merge(repo)
    window._sync_operation_state()

    assert window._work_panel._commit_allowed


def test_branch_switch_menu_is_locked_during_a_sequencer(
    window, repo: Path
) -> None:
    """엔진이 거부하는 것을 메뉴가 내주면 "눌렀는데 오류"가 된다."""
    entries = window._ref_menu_entries("local_branch", "main", False)
    assert any("전환" in label for label, _ in entries), "평소에는 있어야 한다"

    start_rebase(repo)
    window._sync_operation_state()

    # 메뉴 항목 자체는 남지만 비활성이어야 한다 — 그 판단은
    # `_busy_with_history()`가 한다.
    assert window._busy_with_history()


def test_amend_asks_before_replacing_the_commit(
    window, repo: Path, monkeypatch
) -> None:
    """amend는 히스토리 재작성인데 확인창이 없었다 (§5.2 원칙 2).

    체크박스 하나와 버튼 하나, 두 번의 클릭으로 HEAD가 교체됐다.
    """
    from PySide6.QtWidgets import QMessageBox

    shown: list = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda *a, **k: (shown.append(a[2]), QMessageBox.StandardButton.Cancel)[1],
    )
    before = git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    window._on_commit_requested("고친 메시지", True)

    assert shown, "확인창이 뜨지 않았다"
    assert "reflog" in shown[0], "되찾는 방법을 말해야 한다"
    assert git("rev-parse", "HEAD", cwd=repo).stdout.strip() == before, (
        "취소했는데 커밋이 바뀌었다"
    )


def test_plain_commit_is_not_interrupted(window, repo: Path, monkeypatch) -> None:
    """평범한 커밋까지 물으면 확인창이 소음이 된다."""
    from PySide6.QtWidgets import QMessageBox

    shown: list = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a))
    write(repo / "new.txt", "내용\n")
    LocalGitEngine.open(repo).stage_file("new.txt")

    window._on_commit_requested("새 커밋", False)

    assert shown == []
