"""도메인 예외.

Infrastructure 층이 pygit2/git CLI의 오류를 이 예외들로 변환하고,
UI 층은 이를 사용자 표시용 메시지로 매핑한다. (doc/design.md §7)
"""

from __future__ import annotations


class GitClientError(Exception):
    """이 애플리케이션이 발생시키는 모든 예외의 최상위 타입."""

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        """git이 출력한 원문 등, 사용자에게 함께 보여줄 부가 정보."""
        self.action = action
        """사용자가 다음에 할 수 있는 조치. UI가 본문 아래에 노출한다. (§5.2 원칙 4)"""


class RepositoryNotFoundError(GitClientError):
    """지정한 경로가 Git 저장소가 아니거나 접근할 수 없다."""


class RepositoryOpenError(GitClientError):
    """저장소를 찾았지만 여는 데 실패했다."""


class EngineError(GitClientError):
    """Git 엔진 호출이 실패했다."""


class AuthenticationRequired(EngineError):
    """원격이 자격증명을 요구했고, 저장된 것으로는 통과하지 못했다.

    **다른 실패와 구분하는 이유**: 이것만이 "사용자에게 물어보면 해결되는"
    실패다. 나머지는 물어봐야 소용이 없다. UI는 이 타입을 보고 자격증명
    다이얼로그를 띄운 뒤 같은 작업을 한 번 다시 시도한다.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        username: str | None = None,
        rejected: bool = False,
        detail: str | None = None,
        action: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail, action=action)
        self.url = url
        """자격증명을 요구한 원격 주소. 다이얼로그에 무엇에 대한 인증인지 보여준다."""

        self.username = username
        """git이 알고 있던 사용자 이름(URL에 박혀 있는 경우). 미리 채워 준다."""

        self.rejected = rejected
        """제출한 자격증명이 **거부**된 것인가.

        "아직 물어보지 않았다"와 "물어봤는데 틀렸다"는 다르다. 후자에서
        같은 값으로 재시도하면 무한 반복이 된다.
        """
