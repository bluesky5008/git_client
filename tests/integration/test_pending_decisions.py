"""결정 대기였던 둘을 결론까지 가져간다 (ADR-88, backlog 구 §3.5·§3.6).

§3.5 — 없는 객체를 만나면 **무엇이** 없는지와 **어떻게 받는지**를 말한다.
§3.6 — 저장소가 없는 복제의 계측은 **원격 주소**에 귀속시킨다. ADR-26이
       요구한 "실패해도 기록한다"와 §4.9의 "귀속 대상이 없다"가 서로
       다른 말을 하고 있었는데, 저장소는 없어도 주소는 있다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.errors import EngineError
from gitclient.domain.instrumentation import OperationKind, TransferStats
from gitclient.infrastructure.local_engine import _missing_oid, _translate
from tests.integration.remote_harness import AUTHOR_ENV, git

MISSING = "0123456789abcdef0123456789abcdef01234567"


class TestMissingObjectIsNamed:
    def test_oid_is_recognised_from_keyerror(self) -> None:
        assert _missing_oid(KeyError(MISSING)) == MISSING
        assert _missing_oid(KeyError("사람이 읽는 말")) is None

    def test_oid_is_recognised_from_git_error(self) -> None:
        import pygit2

        error = pygit2.GitError(f"object not found - no match for id ({MISSING})")
        assert _missing_oid(error) == MISSING

    def test_unrelated_errors_stay_generic(self) -> None:
        """엉뚱한 오류를 '없는 객체'로 부르면 안내가 거짓말이 된다."""
        with pytest.raises(EngineError) as caught:
            with _translate("무언가"):
                raise ValueError("전혀 다른 문제")

        assert "Git 엔진 오류" in str(caught.value)

    def test_missing_object_says_what_and_how(self) -> None:
        with pytest.raises(EngineError) as caught:
            with _translate("diff 계산"):
                raise KeyError(MISSING)

        error = caught.value
        assert MISSING in (error.detail or ""), "무엇이 없는지 말하지 않았다"
        assert "Fetch" in (error.action or ""), "어떻게 받는지 말하지 않았다"

    def test_partial_clone_gets_its_own_guidance(self) -> None:
        """부분 복제는 원인이 다르다 — 손상이 아니라 아직 안 받은 것이다."""
        with pytest.raises(EngineError) as caught:
            with _translate("diff 계산", partial_hint=True):
                raise KeyError(MISSING)

        assert "부분 복제" in (caught.value.action or "")

    def test_engine_knows_whether_it_is_partial(self, tmp_path: Path) -> None:
        from gitclient.infrastructure.local_engine import LocalGitEngine

        root = tmp_path / "work"
        root.mkdir()
        git("init", "--quiet", "-b", "main", str(root))
        (root / "f.txt").write_text("x\n", encoding="utf-8")
        git("add", "-A", cwd=root)
        git(*AUTHOR_ENV, "commit", "--quiet", "-m", "c", cwd=root)

        assert LocalGitEngine.open(str(root))._partial is False


class TestFailedCloneIsAttributed:
    def test_key_is_the_url_without_credentials(self) -> None:
        from gitclient.application.remote_workers import CloneWorker

        worker = CloneWorker(
            "https://alice:tok3n@example.com/o/r.git", "/tmp/nowhere-xyz"
        )

        key = worker._fallback_key()

        assert key == "url:https://example.com/o/r.git"
        assert "tok3n" not in key, "계측 DB는 비밀을 담는 곳이 아니다"

    def test_failed_clone_records_against_the_url(self, tmp_path: Path) -> None:
        """저장소가 없어도 기록이 남는다 (ADR-26의 무기록 방지)."""
        from gitclient.application.remote_workers import CloneWorker
        from gitclient.infrastructure.stats_store import StatsStore

        stored: list[tuple[str, TransferStats]] = []
        original = StatsStore.record
        StatsStore.record = lambda self, key, stats: stored.append((key, stats))
        try:
            worker = CloneWorker(
                "https://example.com/o/r.git", tmp_path / "never-created"
            )
            worker._record(
                TransferStats(
                    kind=OperationKind.CLONE,
                    remote="origin",
                    duration_ms=10,
                    received_bytes=4096,
                    succeeded=False,
                )
            )
        finally:
            StatsStore.record = original

        assert stored, "실패한 복제의 트래픽이 통째로 사라졌다"
        key, stats = stored[0]
        assert key == "url:https://example.com/o/r.git"
        assert stats.received_bytes == 4096

    def test_other_workers_still_need_a_repository(self, tmp_path: Path) -> None:
        """URL 귀속은 복제의 예외다 — fetch/push는 저장소가 있어야 한다."""
        from gitclient.application.remote_workers import FetchWorker

        assert FetchWorker(tmp_path / "not-a-repo")._fallback_key() is None
