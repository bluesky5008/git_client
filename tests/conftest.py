"""테스트 공통 설정.

Qt를 쓰는 테스트가 디스플레이 없는 환경(CI)에서도 돌도록 offscreen을 기본값으로
한다. 이미 지정돼 있으면 존중한다.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# ---------------------------------------------------------------------------
# git 환경 격리
# ---------------------------------------------------------------------------
#
# **테스트가 개발자의 실제 git 설정과 자격증명 저장소를 건드리면 안 된다.**
# 적대적 리뷰에서 확인된 문제: 이 머신의 시스템 설정에 `credential.helper=
# manager`(GCM)가 켜져 있어, 인증 하네스를 상대로 도는 테스트가 진짜 GCM을
# 태우고 실제 키체인에 쓰고 있었다. 전역 설정(user.name, 별칭, safe.directory,
# 훅 경로)도 같은 경로로 새어들어 테스트 결과가 개발자 환경에 따라 달라진다.
#
# 프로덕션 코드(`RemoteEngine._run`)는 `os.environ`을 복사해 쓰므로, 여기서
# 프로세스 환경에 심어두면 하네스와 프로덕션 경로가 **함께** 격리된다.
# 프로덕션 자체는 사용자 설정을 그대로 써야 하므로 코드에는 넣지 않는다.
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ.pop("GIT_CONFIG", None)

# 전역 설정을 끊으면 작성자 정보도 사라진다. 프로덕션 경로(pygit2/CLI)가
# 커밋하는 테스트를 위해 프로세스 환경에도 결정적인 값을 심는다.
os.environ.setdefault("GIT_AUTHOR_NAME", "테스터")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "tester@example.com")
os.environ.setdefault("GIT_COMMITTER_NAME", "테스터")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "tester@example.com")
