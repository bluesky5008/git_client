"""git에 자격증명을 공급하는 일회성 credential helper (DCR-003, ADR-78).

**왜 askpass가 아니라 credential helper인가.** 원래는 `GIT_ASKPASS`로 작은
shim 파일을 그때그때 써서 값을 돌려줬다. 그런데 최신 git(2.50 실측)은
`credential.interactive=false`가 **askpass 호출까지 차단한다** — 우리가
프롬프트를 대신하려고 세운 설정이 우리 공급 경로를 죽였다. helper의
`get`은 그 설정 아래에서도 동작한다(구·신 git 모두 실측) — 그 설정이
막으려는 것은 "묻기"이고 helper의 get은 "돌려주기"이기 때문이다.

**왜 값을 파일에 쓰지 않는가** (ADR-29, 유지). 비밀번호가 디스크에 남으면
프로세스가 죽었을 때 그대로 남는다. helper 명령에는 환경변수 **이름**만
들어가고 값은 자식 프로세스의 환경으로만 흐른다. (같은 사용자의 다른
프로세스가 환경을 볼 수 있다는 한계는 자격증명 자체와 같은 신뢰 경계다.)

**셸 경로가 하나다.** `!`로 시작하는 helper는 Windows에서도 git 동봉 sh로
실행된다 — cmd.exe의 퍼센트 확장이 비밀번호의 메타문자를 재해석하던 위험
(ADR-31이 막던 것)은 지킬 대상 자체가 사라졌다. `printf '%s' "$VAR"`는
값을 문자 그대로 내보낸다 (적대적 비밀번호 실측).
"""

from __future__ import annotations

from dataclasses import dataclass

USERNAME_ENV = "GITCLIENT_ASKPASS_USERNAME"
PASSWORD_ENV = "GITCLIENT_ASKPASS_PASSWORD"


@dataclass(frozen=True, slots=True)
class Credentials:
    """사용자가 다이얼로그에 입력한 값.

    **저장하지 않는다.** 이 객체는 한 번의 작업 동안만 살아 있고, 저장은
    git의 credential helper에 위임한다(ADR-3). `remember`는 그 위임을
    할지 말지의 표시일 뿐 우리가 어딘가에 쓰겠다는 뜻이 아니다.
    """

    username: str
    password: str
    remember: bool = True

    def __repr__(self) -> str:  # pragma: no cover - 방어적
        # 비밀번호가 로그·트레이스백에 실려 나가지 않게 한다.
        return f"Credentials(username={self.username!r}, password=***)"


# get 요청에만 응답한다. store/erase는 조용히 무시한다 — 저장 위임은
# 우리가 `git credential approve`로 명시적으로 한다 (ADR-32·33).
CREDENTIAL_HELPER = (
    "!f() { "
    'if [ "$1" = get ]; then '
    "printf 'username=%s\\npassword=%s\\n' "
    f'"${USERNAME_ENV}" "${PASSWORD_ENV}"; '
    "fi; }; f"
)


def credential_helper_config() -> list[str]:
    """이 명령에 한해 우리 helper를 체인에 더하는 `-c` 인자.

    호출자가 "저장 안 함" 재설정(`credential.helper=`) **뒤에** 붙여야
    한다 — 설정은 명령줄 순서대로 쌓이고, 빈 값이 체인을 재설정한다.
    """
    return ["-c", f"credential.helper={CREDENTIAL_HELPER}"]


def credential_environment(credentials: Credentials) -> dict[str, str]:
    """helper가 읽을 환경변수. 값은 여기로만 흐른다."""
    return {
        USERNAME_ENV: credentials.username,
        PASSWORD_ENV: credentials.password,
    }
