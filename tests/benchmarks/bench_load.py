"""저장소 로딩 성능 측정 (G2 검증용).

사용법:
    python -m tests.benchmarks.bench_load [커밋수]

측정 항목:
  - open_repository()가 UI 스레드를 붙잡는 시간   (G4)
  - 첫 행이 화면에 나타나기까지                    (G2 판정 대상)
  - 전체 로딩 완료까지
  - 그중 UI 스레드가 레인 배치에 쓴 시간

pytest로 돌리지 않는 이유: 실제 QApplication과 창이 필요하고 수십 초가 걸린다.
CI에서 회귀를 감시할 때는 이 스크립트를 별도 잡으로 실행한다. (doc/performance.md §8.3)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtWidgets import QApplication

from tests.benchmarks.fixtures import build_repository, count_loose_objects

DEFAULT_COMMITS = 100_000
G2_TARGET_MS = 1_500
G4_BUDGET_MS = 50


def run(commits: int, repo_root: Path) -> int:
    from gitclient.ui.main_window import MainWindow
    from gitclient.viewmodel.commit_graph_model import CommitGraphModel

    repo_path = repo_root / f"bench-{commits}"
    print(f"fixture 준비 중... ({commits:,} 커밋)")
    t0 = time.perf_counter()
    build_repository(repo_path, commits)
    print(f"  준비 완료 {time.perf_counter() - t0:.1f}s")

    loose = count_loose_objects(repo_path)
    if loose:
        print(f"  경고: 느슨한 오브젝트 {loose:,}개 — 측정이 왜곡됩니다")

    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(1280, 780)
    window.show()

    # UI 스레드가 레인 배치에 쓰는 시간을 누적한다
    # **누적만 재면 G4를 볼 수 없다.** 예산은 "단일 블로킹 구간 ≤ 50ms"라
    # 배치별 최대값이 판정 대상이다. 누적 합계로는 200배치 중 2회 128ms
    # 정지가 평균에 묻혀 보이지 않는다 — 실제로 그렇게 놓쳤다.
    cost = {"append_ms": 0.0, "rows": 0, "max_ms": 0.0, "over_budget": 0}
    original = CommitGraphModel.append_commits

    def timed(self, batch):  # noqa: ANN001, ANN202
        start = time.perf_counter()
        original(self, batch)
        elapsed = (time.perf_counter() - start) * 1000
        cost["append_ms"] += elapsed
        cost["rows"] += len(batch)
        cost["max_ms"] = max(cost["max_ms"], elapsed)
        if elapsed > G4_BUDGET_MS:
            cost["over_budget"] += 1

    CommitGraphModel.append_commits = timed

    timer = QElapsedTimer()
    timer.start()

    t0 = time.perf_counter()
    window.open_repository(str(repo_path))
    ui_ms = (time.perf_counter() - t0) * 1000

    first = {"ms": None}

    def on_batch(_batch) -> None:  # noqa: ANN001
        if first["ms"] is None:
            first["ms"] = timer.elapsed()

    def on_finished(total: int) -> None:
        elapsed = timer.elapsed()
        verdict = "충족" if first["ms"] <= G2_TARGET_MS else "미달"
        print()
        print(f"  커밋 수                   {total:,}")
        print(f"  open_repository() UI 점유 {ui_ms:>8.1f}ms")
        print(f"  첫 행 표시까지            {first['ms']:>8,}ms   G2 {verdict}"
              f" (목표 {G2_TARGET_MS:,}ms)")
        print(f"  전체 로딩 완료            {elapsed:>8,}ms")
        print(f"  그중 레인 배치(UI 스레드) {cost['append_ms']:>8,.0f}ms"
              f" ({cost['append_ms'] / max(1, elapsed) * 100:.0f}%)")
        verdict = "G4 충족" if cost["over_budget"] == 0 else (
            f"**G4 위반 {cost['over_budget']}회**"
        )
        print(f"  단일 배치 최대 정지       {cost['max_ms']:>8,.1f}ms"
              f"  {verdict} (예산 {G4_BUDGET_MS}ms)")
        print(f"  최대 레인 수              {window._commit_model.max_lane_count:>8}")
        app.quit()

    window._loader.signals.batch_ready.connect(on_batch)
    window._loader.signals.finished.connect(on_finished)
    QTimer.singleShot(600_000, app.quit)
    return app.exec()


def main() -> int:
    commits = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_COMMITS
    repo_root = Path(__file__).resolve().parents[2] / ".benchmarks"
    repo_root.mkdir(exist_ok=True)
    return run(commits, repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
