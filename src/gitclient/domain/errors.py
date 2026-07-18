"""도메인 예외.

Infrastructure 층이 pygit2/git CLI의 오류를 이 예외들로 변환하고,
UI 층은 이를 사용자 표시용 메시지로 매핑한다. (doc/design.md §7)
"""

from __future__ import annotations


class GitClientError(Exception):
    """이 애플리케이션이 발생시키는 모든 예외의 최상위 타입."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        """git이 출력한 원문 등, 사용자에게 함께 보여줄 부가 정보."""


class RepositoryNotFoundError(GitClientError):
    """지정한 경로가 Git 저장소가 아니거나 접근할 수 없다."""


class RepositoryOpenError(GitClientError):
    """저장소를 찾았지만 여는 데 실패했다."""


class EngineError(GitClientError):
    """Git 엔진 호출이 실패했다."""
