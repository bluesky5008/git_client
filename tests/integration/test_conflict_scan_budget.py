"""충돌 목록은 열거와 분류가 분리된다 — DCR-002, ADR-77 (ADR-47 대체).

ADR-47의 전제("blob에게 물으면 예산 안")는 감사 실측으로 무너졌다 —
`repo.get(id)`은 blob 내용을 ODB에서 로드하므로 읽는 자리를 옮겼을 뿐
크기 의존성은 그대로였다 (충돌 200개 × 20,000줄에서 2,957ms, G4의 59배).

새 계약:
  - UI 스레드의 열거(`index_conflicts`)는 blob을 열지 않는다 —
    텍스트 쌍의 `has_markers`는 None(미분류)이고, 삭제 계열만 종류에서
    즉시 False가 나온다.
  - 분류는 워커 문맥의 결과(`MergeOutcome`·`HistoryOutcome`)에만 실려 온다.
    (그쪽 검증은 test_merge_hardening.py가 이미 한다.)
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from gitclient.domain.models import ConflictSide
from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.integration.remote_harness import AUTHOR_ENV, git

G4_BUDGET_S = 0.050


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


def merge_expecting_conflict(repo: Path, ref: str) -> None:
    """충돌로 끝나야 하는 병합 — 하네스 git()은 실패에 raise하므로 직접 돈다."""
    result = subprocess.run(
        ["git", "merge", ref], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode != 0, "충돌이 나야 하는 픽스처다"


@pytest.fixture
def wide_conflict(tmp_path: Path) -> Path:
    """충돌 200개, 파일마다 수천 줄 — 분류가 blob을 열면 반드시 느린 규모."""
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    body = ("x" * 60 + "\n") * 2000
    for i in range(200):
        (root / f"f{i}.txt").write_text("base\n" + body, encoding="utf-8")
    commit_all(root, "base")

    git("checkout", "--quiet", "-b", "feature", cwd=root)
    for i in range(200):
        (root / f"f{i}.txt").write_text("feature\n" + body, encoding="utf-8")
    commit_all(root, "feature")

    git("checkout", "--quiet", "main", cwd=root)
    for i in range(200):
        (root / f"f{i}.txt").write_text("main\n" + body, encoding="utf-8")
    commit_all(root, "main")

    merge_expecting_conflict(root, "feature")
    return root


def test_enumeration_leaves_text_pairs_unclassified(wide_conflict: Path) -> None:
    """UI 스레드 경로는 미분류(None)를 돌려준다 — blob을 연 적이 없다는 뜻.

    이 구조 검증이 시간 검증보다 강하다: 시간은 머신에 따라 흔들리지만,
    None은 분류 코드가 아예 돌지 않았음을 뜻한다.
    """
    conflicts = LocalGitEngine.open(str(wide_conflict)).index_conflicts()
    assert len(conflicts) == 200
    assert all(c.side is ConflictSide.BOTH_MODIFIED for c in conflicts)
    assert all(c.has_markers is None for c in conflicts)


def test_enumeration_fits_the_g4_budget(wide_conflict: Path) -> None:
    """충돌 200개 × 수천 줄에서도 열거는 50ms 안이다 (AC — DCR-002).

    같은 픽스처의 분류 포함 스캔이 수백 ms였다 (실측 26~2,957ms).
    열거는 인덱스만 읽으므로 파일 크기와 무관해야 한다.
    """
    engine = LocalGitEngine.open(str(wide_conflict))
    engine.index_conflicts()  # 인덱스 첫 로드는 측정 밖 — 열기 비용이다

    started = time.perf_counter()
    conflicts = engine.index_conflicts()
    elapsed = time.perf_counter() - started

    assert len(conflicts) == 200
    assert elapsed < G4_BUDGET_S, f"열거가 {elapsed * 1000:.0f}ms — G4 초과"


def test_deletion_conflicts_are_classified_by_kind_alone(tmp_path: Path) -> None:
    """삭제 계열의 False는 분류가 아니라 종류에서 나온다 — 항상 즉시 안다."""
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    (root / "gone.txt").write_text("base\n", encoding="utf-8")
    commit_all(root, "base")

    git("checkout", "--quiet", "-b", "feature", cwd=root)
    (root / "gone.txt").unlink()
    commit_all(root, "feature-deletes")

    git("checkout", "--quiet", "main", cwd=root)
    (root / "gone.txt").write_text("edited\n", encoding="utf-8")
    commit_all(root, "main-edits")

    merge_expecting_conflict(root, "feature")

    conflicts = LocalGitEngine.open(str(root)).index_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].side is ConflictSide.DELETED_BY_THEM
    assert conflicts[0].has_markers is False
