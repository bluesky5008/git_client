"""목적 함수 최적화의 계약 — backlog §4 증분 P1 (ADR-80·81).

이득의 크기는 experiments/exp_negotiation.py 실측이 근거다. 여기서는
설정이 실제 명령에 실리는지, 플랫폼 분기가 옳은지를 지킨다 — 설정 한
줄은 지워져도 테스트가 붉지 않으면 조용히 사라진다 (ADR-8이 겪은 일).
"""

from __future__ import annotations

from gitclient.infrastructure.remote_engine import BASE_CONFIG, _ssh_command


class TestNegotiationAlgorithm:
    def test_skipping_is_pinned_in_base_config(self) -> None:
        """실측(5왕복→1왕복, 전송 동일)으로 채택된 설정이다 (ADR-80)."""
        assert "fetch.negotiationAlgorithm=skipping" in BASE_CONFIG


class TestSshMultiplexing:
    def test_posix_gets_control_master(self) -> None:
        command = _ssh_command(windows=False)
        assert "ControlMaster=auto" in command
        assert "ControlPersist" in command
        assert "BatchMode=yes" in command, "멀티플렉싱이 비대화형 차단을 밀어내면 안 된다"

    def test_windows_does_not(self) -> None:
        """Windows OpenSSH는 ControlMaster를 지원하지 않는다 — 주면 연결이 깨진다."""
        command = _ssh_command(windows=True)
        assert "ControlMaster" not in command
        assert "BatchMode=yes" in command

    def test_control_path_is_short_enough_for_unix_sockets(self) -> None:
        """유닉스 소켓 경로는 104바이트 상한이 있다 — %C(40자) 확장을 감안한다."""
        command = _ssh_command(windows=False)
        path = next(
            part.split("=", 1)[1]
            for part in command.split()
            if part.startswith("ControlPath=")
        )
        assert len(path.replace("%C", "C" * 40)) < 104
