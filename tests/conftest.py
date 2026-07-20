"""테스트 공통 설정.

Qt를 쓰는 테스트가 디스플레이 없는 환경(CI)에서도 돌도록 offscreen을 기본값으로
한다. 이미 지정돼 있으면 존중한다.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

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
os.environ.pop("GIT_CONFIG", None)

# 전역 설정을 끊으면 작성자 정보도 사라진다. 한때 `GIT_CONFIG_GLOBAL=/dev/null`
# 로 끊고 `GIT_AUTHOR_*` 환경변수로 신원을 심었는데, **그 방법은 두 엔진 중
# 하나에만 통한다.** 실측: 같은 환경에서 git CLI는 환경변수를 따르고
# pygit2의 `default_signature`는 무시하고 config만 본다.
#
# 그래서 신원만 담은 **진짜 전역 설정 파일**을 만들어 가리킨다. 격리 의도는
# 그대로다(개발자의 별칭·credential.helper·safe.directory는 여전히 차단된다).
# 게다가 이것이 실제 사용자의 모양이기도 하다 — 신원은 config에 있다.
_GITCONFIG = Path(tempfile.gettempdir()) / "gitclient-tests.gitconfig"
_GITCONFIG.write_text(
    "[user]\n\tname = 테스터\n\temail = tester@example.com\n",
    encoding="utf-8",
)
os.environ["GIT_CONFIG_GLOBAL"] = str(_GITCONFIG)

# 하네스가 직접 부르는 git에도 같은 값을 준다. 프로덕션 경로는 이 변수들을
# 걷어내므로(local_engine._HISTORY_ENV_BLOCKLIST) 여기서 심어도 프로덕션의
# 신원 결정에는 영향이 없다.
os.environ.setdefault("GIT_AUTHOR_NAME", "테스터")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "tester@example.com")
os.environ.setdefault("GIT_COMMITTER_NAME", "테스터")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "tester@example.com")
