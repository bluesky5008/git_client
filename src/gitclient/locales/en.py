"""English catalog. Keys are the Korean source strings (see gitclient.i18n).

Two groups: window chrome (menus, buttons, labels, tooltips — applied by the
show-event translator) and user-facing errors/notices (applied at the
presentation boundary, `_report`/`_notify`).

Strings that interpolate values are not here yet — see doc/backlog.md §5.
The completeness test (tests/unit/test_i18n.py) keeps this file honest: it
fails when a chrome string exists in the UI without an entry here, and when
an entry here no longer appears in the source.
"""

from __future__ import annotations

CATALOG: dict[str, str] = {
    # -- 창 크롬: 메뉴 · 툴바 --------------------------------------------
    "파일": "File",
    "보기": "View",
    "저장소": "Repository",
    "주요": "Main",
    "저장소 열기...": "Open Repository...",
    "새로 고침": "Refresh",
    "설정": "Settings",
    "설정...": "Settings...",
    "종료": "Quit",
    "복제 (Clone)...": "Clone...",
    "복제": "Clone",
    "가져오기 (Fetch)": "Fetch",
    "가져와 합치기 (Pull)": "Pull",
    "올리기 (Push)": "Push",
    "새 브랜치...": "New Branch...",
    "reflog 탐색": "Browse reflog",
    "reflog 탐색...": "Browse reflog...",
    "원격 관리": "Remotes",
    "원격 관리...": "Manage Remotes...",
    "커밋 검색...": "Find Commit...",
    "배경에서 미리 가져오기": "Prefetch in Background",
    "진행 중인 작업 중단": "Abort Operation in Progress",
    "Stash 보관": "Stash Changes",
    "Stash 꺼내기": "Pop Stash",
    # -- 창 크롬: 버튼 · 라벨 -------------------------------------------
    "계속": "Continue",
    "중단": "Abort",
    "닫기": "Close",
    "다음": "Next",
    "이전": "Previous",
    "추가...": "Add...",
    "삭제...": "Remove...",
    "주소 변경...": "Change URL...",
    "찾아보기...": "Browse...",
    "sha 복사": "Copy SHA",
    "↑ 위로": "↑ Move Up",
    "↓ 아래로": "↓ Move Down",
    "올리기 ↑": "Stage ↑",
    "내리기 ↓": "Unstage ↓",
    "선택 줄 올리기": "Stage Selected Lines",
    "헝크 올리기": "Stage Hunk",
    "리베이스 시작": "Start Rebase",
    "이대로 합치기": "Apply This Combination",
    "줄 단위로 고르기...": "Choose Line by Line...",
    "이 커밋 건너뛰기": "Skip This Commit",
    "버리기": "Drop",
    "이 커밋에서 브랜치 만들기...": "Create Branch Here...",
    "커밋": "Commit",
    "커밋 메시지": "Commit message",
    "마지막 커밋 수정": "Amend last commit",
    "커밋을 선택하세요": "Select a commit",
    "충돌한 파일": "Conflicted files",
    "현재 브랜치": "Current branch",
    "동작": "Action",
    "요약": "Summary",
    "일반": "General",
    "테마": "Theme",
    "언어": "Language",
    "언어는 앱을 다시 시작해야 모든 화면에 적용됩니다.": (
        "The language applies to every screen after a restart."
    ),
    "단축키": "Shortcuts",
    "분": " min",
    "유휴 정리(repack)까지": "Idle repack after",
    "배경에서 미리 가져오기 (ADR-7)": "Prefetch in background (ADR-7)",
    "(변경된 파일 없음)": "(no changed files)",
    "대상 폴더": "Target folder",
    "받을 범위": "What to fetch",
    "복제할 위치": "Clone into",
    "원격 주소": "Remote URL",
    "저장소 복제": "Clone Repository",
    "사용자 이름": "Username",
    "비밀번호 / 토큰": "Password / token",
    "원격 저장소 로그인": "Remote Repository Login",
    "오류": "Error",
    "https://github.com/사용자/저장소.git": "https://github.com/user/repo.git",
    "메시지 · sha · 작성자 검색": "Search message · sha · author",
    "마지막 원격 작업의 전송량": "Bytes transferred by the last remote operation",
    # -- 창 크롬: 툴팁 · 안내문 -----------------------------------------
    "원격 저장소를 복제해 옵니다": "Clone a remote repository",
    "원격의 변경을 가져옵니다 (워킹 트리는 건드리지 않습니다)": (
        "Fetch remote changes (the working tree is left alone)"
    ),
    "원격의 변경을 가져와 현재 브랜치에 합칩니다": (
        "Fetch remote changes and merge them into the current branch"
    ),
    "로컬 커밋을 원격에 올립니다": "Push local commits to the remote",
    "기다리지 않는 동안 미리 받아둡니다 — 누를 때 전송이 이미 끝나 있습니다": (
        "Fetches ahead of time while you are not waiting — by the time you "
        "click, the transfer is already done"
    ),
    "진행 중인 원격 작업을 멈춥니다": "Stop the remote operation in progress",
    "진행 중인 작업을 시작 이전으로 되돌립니다": (
        "Roll the operation in progress back to where it started"
    ),
    "진행 중인 병합을 되돌립니다 (워킹 트리가 병합 이전으로 복구됩니다)": (
        "Roll back the merge in progress (the working tree is restored)"
    ),
    "충돌을 모두 해결한 뒤 이어서 진행합니다": (
        "Continue once every conflict is resolved"
    ),
    "멈춰 있는 커밋을 버리고 다음으로 넘어갑니다": (
        "Drop the stopped commit and move on to the next one"
    ),
    "원격 추가 · 삭제 · 주소 변경": "Add, remove, or re-point remotes",
    "HEAD가 지나온 자리들 — reset·건너뛰기로 잃은 커밋을 되찾습니다": (
        "Where HEAD has been — recover commits lost to reset or skip"
    ),
    "최근에 연 저장소로 전환": "Switch to a recently opened repository",
    "선택한 파일을 스테이징합니다": "Stage the selected files",
    "선택한 파일의 스테이징을 취소합니다": "Unstage the selected files",
    "선택한 파일의 변경을 버립니다 (되돌릴 수 없음)": (
        "Discard changes in the selected files (cannot be undone)"
    ),
    "선택한 줄만 스테이징합니다 (여러 줄 선택 가능)": (
        "Stage only the selected lines (multiple selection allowed)"
    ),
    "커서가 있는 헝크 전체를 스테이징합니다": (
        "Stage the whole hunk under the cursor"
    ),
    "해결할 충돌이 없습니다.": "No conflicts to resolve.",
    "직접 편집해 해결하려면 워킹 트리의 파일을 고친 뒤 스테이징하면 됩니다.": (
        "To resolve by hand, edit the file in the working tree and stage it."
    ),
    "바이너리 파일이라 내용을 나란히 볼 수 없습니다. 어느 쪽을 남길지 골라 주세요.": (
        "Binary file — the two sides cannot be shown side by side. Choose "
        "which one to keep."
    ),
    "HEAD가 지나온 자리들입니다. reset이나 건너뛰기로 목록에서 사라진 커밋도 "
    "여기 남아 있습니다 — 브랜치를 만들면 다시 그래프에 나타납니다.": (
        "Everywhere HEAD has been. Commits that reset or skip removed from "
        "the list are still here — create a branch and they reappear in the "
        "graph."
    ),
    "원격 저장소 자체는 건드리지 않습니다 — 이 저장소가 어디를 바라보는지만 "
    "바꿉니다.": (
        "The remote repository itself is untouched — this only changes where "
        "this repository points."
    ),
    "위에 있는 커밋이 먼저 적용됩니다. '앞 커밋에 합치기'는 바로 위커밋과 "
    "하나가 되고, '버리기'는 커밋을 결과에서 없앱니다 (reflog로만 되찾을 수 "
    "있습니다).": (
        "Commits at the top are applied first. \"Squash into previous\" "
        "merges a commit into the one above it; \"Drop\" removes it from the "
        "result (recoverable only through the reflog)."
    ),
    "고르지 않은 부분(공통 줄)은 git이 이미 합쳐둔 그대로 남습니다. 적용하면 "
    "파일이 조립되고 스테이징됩니다.": (
        "The parts you do not choose (common lines) stay exactly as git "
        "merged them. Applying assembles the file and stages it."
    ),
    "라이트/다크 고정은 즉시 적용됩니다. '시스템 설정 따르기'로 되돌릴 때는 "
    "앱을 다시 시작해야 플랫폼 스타일이 복원됩니다.": (
        "Forcing light or dark applies immediately. Switching back to "
        "\"Follow system\" needs a restart to restore the platform style."
    ),
    "줄을 선택하면 그 줄만 적용합니다": (
        "Select lines to apply only those lines"
    ),
    "원격 저장소가 로그인을 요구합니다.": (
        "The remote repository requires a login."
    ),
    "자격증명이 거부되었습니다.": "The credentials were rejected.",
    "사용자 이름과 비밀번호(또는 액세스 토큰)를 입력해 주세요.": (
        "Enter your username and password (or access token)."
    ),
    "입력한 자격증명이 맞는지 확인해 주세요. GitHub 등에서는 비밀번호 대신 "
    "액세스 토큰이 필요합니다.": (
        "Check the credentials you entered. GitHub and others need an access "
        "token instead of a password."
    ),
    "이 원격 저장소는 로그인이 필요합니다.": (
        "This remote repository requires a login."
    ),
    "자격증명이 거부되었습니다. 다시 입력해 주세요.": (
        "The credentials were rejected. Please enter them again."
    ),
    "GitHub·GitLab 등은 비밀번호 대신 <b>액세스 토큰</b>을 요구합니다.": (
        "GitHub, GitLab and others require an <b>access token</b> instead of "
        "a password."
    ),
    "이 자격증명 저장 (시스템 자격증명 관리자에 위임)": (
        "Remember these credentials (delegated to the system credential "
        "manager)"
    ),
    "앱이 직접 저장하지 않고 git의 credential helper에 맡깁니다. helper가 "
    "설정돼 있지 않으면 저장되지 않습니다.": (
        "The app never stores them itself — it delegates to git's credential "
        "helper. Nothing is saved if no helper is configured."
    ),
    # -- 오류 · 안내 (표현 계층 출구에서 번역) ---------------------------
    "git을 찾을 수 없습니다.": "git could not be found.",
    "git 실행에 실패했습니다.": "Failed to run git.",
    "git 2.40 이상을 설치하고 PATH에 등록해 주세요.": (
        "Install git 2.40 or newer and put it on your PATH."
    ),
    "git이 설치되어 있고 PATH에 있는지 확인해 주세요.": (
        "Check that git is installed and on your PATH."
    ),
    "원격 저장소에 연결할 수 없습니다.": "Could not reach the remote repository.",
    "원격 주소가 맞는지, 접근 권한이 있는지 확인해 주세요.": (
        "Check that the remote URL is correct and that you have access."
    ),
    "원격 작업이 취소되었습니다.": "The remote operation was cancelled.",
    "원격 작업이 비정상적으로 오래 걸려 중단했습니다.": (
        "The remote operation took abnormally long and was stopped."
    ),
    "원격 서버 상태를 확인해 주세요.": "Check the state of the remote server.",
    "네트워크 연결과 원격 서버 상태를 확인한 뒤 다시 시도해 주세요. 받다 만 "
    "팩은 보존되지 않아 재시도는 처음부터 다시 받으므로, 회선이 안정된 뒤 "
    "시도하는 편이 낫습니다.": (
        "Check your network and the remote server, then try again. A "
        "half-received pack is not kept, so a retry starts from zero — it is "
        "better to wait for a stable connection."
    ),
    "원격에 해당 참조가 없습니다.": "The remote has no such reference.",
    "브랜치 이름을 확인해 주세요.": "Check the branch name.",
    "원격이 push를 거부했습니다.": "The remote rejected the push.",
    "원격에 내가 갖고 있지 않은 커밋이 있어 밀어내지 못했습니다.": (
        "The remote has commits you do not have, so the push was rejected."
    ),
    "먼저 '가져와 합치기(Pull)'로 원격 변경을 합친 뒤 다시 시도해 주세요.": (
        "Pull the remote changes first, then try again."
    ),
    "원격 저장소가 이 브랜치로의 push를 막고 있습니다.": (
        "The remote repository blocks pushes to this branch."
    ),
    "브랜치 보호 규칙을 확인하거나 다른 브랜치로 올려 주세요.": (
        "Check the branch protection rules, or push to a different branch."
    ),
    "원격 저장소의 보호 규칙이나 권한을 확인해 주세요.": (
        "Check the remote's protection rules and your permissions."
    ),
    "복제가 취소되었습니다.": "The clone was cancelled.",
    "원격 이름과 주소를 모두 입력해 주세요.": (
        "Enter both a remote name and a URL."
    ),
    "원격 주소를 입력해 주세요.": "Enter a remote URL.",
    "다른 이름을 쓰거나 기존 원격의 주소를 바꿔 주세요.": (
        "Use a different name, or change the existing remote's URL."
    ),
    "목록을 새로 고친 뒤 다시 시도해 주세요.": "Refresh the list and try again.",
    "다른 이름을 사용해 주세요.": "Use a different name.",
    "커밋 메시지가 비어 있습니다.": "The commit message is empty.",
    "변경 내용을 설명하는 메시지를 입력해 주세요.": (
        "Write a message describing the change."
    ),
    "스테이징된 변경이 없습니다.": "Nothing is staged.",
    "커밋할 파일을 먼저 스테이징해 주세요.": "Stage the files you want to commit.",
    "커밋 작성자 정보가 설정되어 있지 않습니다.": (
        "No commit author identity is configured."
    ),
    "stash 작성자 정보가 설정되어 있지 않습니다.": (
        "No stash author identity is configured."
    ),
    'git config --global user.name "이름" 과 git config --global user.email '
    '"메일" 을 설정해 주세요.': (
        'Set git config --global user.name "Your Name" and git config '
        '--global user.email "you@example.com".'
    ),
    "git config --global user.name/user.email 을 설정해 주세요.": (
        "Set git config --global user.name and user.email."
    ),
    "수정할 커밋이 없습니다.": "There is no commit to amend.",
    "첫 커밋은 amend 없이 만들어 주세요.": (
        "Create the first commit without amending."
    ),
    "머지 진행 중에는 커밋 수정(amend)을 할 수 없습니다.": (
        "You cannot amend while a merge is in progress."
    ),
    "머지 커밋을 먼저 완성해 주세요.": "Finish the merge commit first.",
    "보관된 stash가 없습니다.": "There is no stash to pop.",
    "보관할 변경 사항이 없습니다.": "There are no changes to stash.",
    "커밋이 없어 브랜치를 만들 수 없습니다.": (
        "There are no commits, so a branch cannot be created."
    ),
    "첫 커밋을 만든 뒤 브랜치를 생성해 주세요.": (
        "Make the first commit, then create the branch."
    ),
    "현재 작업 중인 브랜치는 삭제할 수 없습니다.": (
        "You cannot delete the branch you are on."
    ),
    "다른 브랜치로 전환한 뒤 삭제해 주세요.": (
        "Switch to another branch, then delete it."
    ),
    "현재 브랜치가 아닌 곳(분리된 HEAD)에서는 할 수 없는 작업입니다.": (
        "This is not possible on a detached HEAD."
    ),
    "브랜치를 체크아웃한 뒤 다시 시도해 주세요.": (
        "Check out a branch and try again."
    ),
    "브랜치를 확인한 뒤 다시 시도해 주세요.": "Check the branch and try again.",
    "브랜치를 확인한 뒤 다시 가져와 합치기를 실행해 주세요.": (
        "Check the branch, then pull again."
    ),
    "먼저 가져오기(Fetch)를 실행해 주세요.": "Run a fetch first.",
    "양쪽에 서로 다른 커밋이 있어 병합이 필요합니다.": (
        "Both sides have their own commits, so a merge is needed."
    ),
    "빨리 감을 수 없는 상태입니다.": "A fast-forward is not possible here.",
    "진행 중인 작업이 끝나지 않아 빨리 감을 수 없습니다.": (
        "An operation is still in progress, so a fast-forward is not possible."
    ),
    "커밋하지 않은 변경이 있어 병합을 시작할 수 없습니다.": (
        "There are uncommitted changes, so the merge cannot start."
    ),
    "커밋하지 않은 변경이 있어 합칠 수 없습니다.": (
        "There are uncommitted changes, so this cannot be merged."
    ),
    "변경 사항을 커밋하거나 stash에 보관한 뒤 다시 시도해 주세요.": (
        "Commit or stash your changes, then try again."
    ),
    "병합을 시작했지만 머지 커밋을 만들지 못해 되돌렸습니다.": (
        "The merge started but the merge commit could not be created, so it "
        "was rolled back."
    ),
    "진행 중인 작업이 병합이 아니어서 중단할 수 없습니다.": (
        "The operation in progress is not a merge, so it cannot be aborted "
        "this way."
    ),
    "rebase나 cherry-pick은 git CLI에서 `git rebase --abort` 등으로 정리해 "
    "주세요.": (
        "For a rebase or cherry-pick, clean up with `git rebase --abort` in "
        "the git CLI."
    ),
    "이미 진행 중인 작업이 있습니다.": "An operation is already in progress.",
    "병합이나 rebase를 마무리하거나 취소한 뒤 다시 시도해 주세요.": (
        "Finish or cancel the merge or rebase, then try again."
    ),
    "진행 중인 병합이나 rebase를 마무리하거나 취소한 뒤 다시 시도해 주세요.": (
        "Finish or cancel the merge or rebase in progress, then try again."
    ),
    "진행 중인 작업을 마치거나 '중단'으로 되돌린 뒤 다시 시도해 주세요.": (
        "Finish the operation in progress or roll it back with \"Abort\", "
        "then try again."
    ),
    "이 작업은 앱에서 중단할 수 없습니다.": (
        "This operation cannot be aborted from the app."
    ),
    "git CLI에서 정리해 주세요.": "Clean it up in the git CLI.",
    "이어서 진행할 작업이 없습니다.": "There is no operation to continue.",
    "병합은 '계속'이 아니라 커밋으로 마무리합니다.": (
        "A merge is finished by committing, not by \"Continue\"."
    ),
    "충돌 목록에서 각 파일을 해결한 뒤 다시 시도해 주세요.": (
        "Resolve each file in the conflict list, then try again."
    ),
    "충돌 해결 필요": "Conflicts need resolving",
    "충돌한 파일을 정리한 뒤 스테이징하면 커밋할 수 있습니다. 되돌리려면 "
    "'저장소 > 병합 중단'을 선택해 주세요.": (
        "Clean up the conflicted files and stage them to commit. To roll "
        "back, choose Repository > Abort merge."
    ),
    "충돌 마커를 정리한 뒤 스테이징해 해결하거나, '저장소 > 병합 중단'으로 "
    "병합 전체를 되돌려 주세요.": (
        "Clean up the conflict markers and stage them, or roll the whole "
        "merge back with Repository > Abort merge."
    ),
    "git CLI에서 `git checkout --ours/--theirs -- <경로>`로 해결해 주세요.": (
        "Resolve it with `git checkout --ours/--theirs -- <path>` in the git "
        "CLI."
    ),
    "줄 단위 선택 불가": "Line-by-line choice unavailable",
    "고를 구획이 없습니다": "There is nothing to choose",
    "이미 정리된 파일이면 스테이징만 하면 됩니다.": (
        "If the file is already clean, just stage it."
    ),
    "파일을 이미 편집하셨다면 편집기에서 마커를 정리한 뒤 스테이징해 주세요.": (
        "If you have already edited the file, clean up the markers in your "
        "editor and stage it."
    ),
    "이 커밋에는 남길 변경이 있습니다.": "This commit still has changes to keep.",
    "'계속'으로 이어가 주세요.": "Continue with \"Continue\".",
    "빈 커밋을 만들지 못했습니다.": "The empty commit could not be created.",
    "이미 반영된 커밋 생략": "Already-applied commits were skipped",
    "이 커밋에 남길 변경이 없습니다": "This commit has nothing left to keep",
    "같은 변경이 결과에 이미 들어 있습니다 — 잃은 것은 없습니다.": (
        "The same change is already in the result — nothing was lost."
    ),
    "모든 커밋을 버리는 계획입니다.": "This plan drops every commit.",
    "적어도 하나는 남겨 주세요. 브랜치를 통째로 되돌리려면 '되돌리기(reset)'를 "
    "쓰는 편이 분명합니다.": (
        "Keep at least one. To move the whole branch back, a reset says it "
        "more clearly."
    ),
    "첫 커밋은 앞 커밋에 합칠 수 없습니다.": (
        "The first commit cannot be squashed into a previous one."
    ),
    "맨 위 커밋은 '그대로 두기'여야 합니다.": (
        "The topmost commit must be \"Keep as is\"."
    ),
    "옮길 커밋이 없습니다": "There are no commits to move",
    "이미 그 위에 있거나 뒤처져 있습니다 — 가져오기(Pull)를 생각해 보세요.": (
        "You are already on top of it, or behind it — consider pulling."
    ),
    "위 내용을 확인한 뒤 다시 시도해 주세요.": (
        "Check the message above and try again."
    ),
    "작업이 진행 중으로 남아 있다면 '중단'으로 되돌릴 수 있습니다.": (
        "If the operation is left in progress, \"Abort\" rolls it back."
    ),
    "저장소에 없는 객체를 참조했습니다.": (
        "A referenced object is missing from this repository."
    ),
    "부분 복제(blob 지연 수신) 저장소입니다. 네트워크에 연결한 뒤 "
    "가져오기(Fetch)를 실행하면 필요한 객체를 받아옵니다.": (
        "This is a partial clone (blobs arrive on demand). Connect to the "
        "network and run a fetch to bring the missing objects in."
    ),
    "저장소가 손상되었거나 참조가 가리키는 객체가 없습니다. "
    "가져오기(Fetch)로 받아올 수 있는지 확인해 주세요.": (
        "The repository may be damaged, or the reference points at an object "
        "that is not here. Check whether a fetch can bring it in."
    ),
    "워킹 트리가 없는 저장소에서는 할 수 없는 작업입니다.": (
        "This is not possible in a repository without a working tree."
    ),
    "bare 저장소가 아닌 사본에서 시도해 주세요.": (
        "Try it in a non-bare clone."
    ),
    "서브모듈 디렉터리에서 원하는 커밋을 체크아웃한 뒤 상위 저장소에서 "
    "스테이징해 주세요.": (
        "Check out the commit you want inside the submodule, then stage it "
        "from the parent repository."
    ),
    "화면에 표시된 내용과 지금 파일의 내용이 다릅니다.": (
        "What is on screen no longer matches the file."
    ),
    "파일이 바뀌어 선택한 줄을 그대로 적용할 수 없습니다.": (
        "The file changed, so the selected lines cannot be applied as they are."
    ),
    "변경 내용을 다시 확인한 뒤 선택해 주세요. (F5로 새로 고칠 수 있습니다)": (
        "Review the changes and choose again (F5 refreshes)."
    ),
    "파일이 그 사이 변경되었을 수 있습니다. 새로 고침(F5) 후 다시 시도해 "
    "주세요.": (
        "The file may have changed in the meantime. Refresh (F5) and try "
        "again."
    ),
    "파일이 외부에서 바뀌었을 수 있습니다. 새로 고침(F5) 후 다시 시도해 "
    "주세요.": (
        "The file may have been changed outside the app. Refresh (F5) and try "
        "again."
    ),
    "저장소가 외부에서 변경되었을 수 있습니다. 새로 고침(F5) 해보세요.": (
        "The repository may have changed outside the app. Try refreshing (F5)."
    ),
    "커밋을 읽는 중 예상치 못한 오류가 발생했습니다.": (
        "An unexpected error occurred while reading commits."
    ),
    "참조 목록을 읽는 중 예상치 못한 오류가 발생했습니다.": (
        "An unexpected error occurred while reading references."
    ),
    "diff를 계산하는 중 예상치 못한 오류가 발생했습니다.": (
        "An unexpected error occurred while computing the diff."
    ),
    "변경 사항 diff를 계산하는 중 오류가 발생했습니다.": (
        "An error occurred while computing the diff of the changes."
    ),
    "작업 디렉터리 상태를 읽는 중 오류가 발생했습니다.": (
        "An error occurred while reading the working directory status."
    ),

    # -- 값이 끼어드는 문구 (템플릿이 키다 — i18n.trf) -----------------
    "'{branch}'에 '{upstream}'보다 새로운 커밋이 없습니다.": "'{branch}' has no commits newer than '{upstream}'.",
    "'{expected_branch}'에 합치려 했지만 현재 브랜치가 바뀌었습니다.": "The merge targeted '{expected_branch}', but the current branch changed.",
    "'{expected}'에서 시작한 작업인데 현재 브랜치가 바뀌었습니다.": "The operation started on '{expected}', but the current branch changed.",
    "'{path}'에는 충돌 마커가 남아 있지 않습니다.": "'{path}' has no conflict markers left.",
    "'{path}'은(는) 서브모듈이라 여기서 해결할 수 없습니다.": "'{path}' is a submodule and cannot be resolved here.",
    "'{path}'은(는) 심볼릭 링크라 여기서 해결할 수 없습니다.": "'{path}' is a symbolic link and cannot be resolved here.",
    "'{path}'은(는) 충돌 상태가 아닙니다.": "'{path}' is not in conflict.",
    "'{path}'은(는) 충돌 해결 중이라 변경을 버릴 수 없습니다.": "'{path}' is being resolved, so its changes cannot be discarded.",
    "'{path}'을(를) 읽지 못했습니다.": "Could not read '{path}'.",
    "'{path}'의 내용을 읽지 못했습니다. 목록을 새로 고쳐 주세요.": "Could not read the contents of '{path}'. Refresh the list.",
    "'{path}'의 충돌 마커를 읽을 수 없습니다.": "The conflict markers in '{path}' cannot be read.",
    "'{path}'의 내용을 읽는 중...": "Reading '{path}'...",
    "'{side}' 쪽에는 이 파일이 없습니다. '{side} 사용'을 고르면 파일이 삭제됩니다.": '\'{side}\' does not have this file. Choosing "Use {side}" deletes it.',
    "'{upstream}' 위로 리베이스 — 계획": "Rebase onto '{upstream}' — plan",
    'git {args_0} 실패 (exit {result_returncode}).': 'git {args_0} failed (exit {result_returncode}).',
    '{context} 중 Git 엔진 오류가 발생했습니다.': 'A Git engine error occurred during {context}.',
    '{context}에 실패했습니다.': '{context} failed.',
    '{context}이(가) {_HISTORY_TIMEOUT_S}초 안에 끝나지 않았습니다.': '{context} did not finish within {_HISTORY_TIMEOUT_S} seconds.',
    '{exc}\n\n--- 생성된 패치 ---\n{patch_text}': '{exc}\n\n--- generated patch ---\n{patch_text}',
    "{finish} 되돌리려면 위쪽 '중단'을 눌러 주세요.": '{finish} To roll back, press "Abort" above.',
    '{job_name} 중 예상치 못한 오류가 발생했습니다.': 'An unexpected error occurred during {job_name}.',
    '{side} 사용': 'Use {side}',
    '{operation_label} 중단에 실패했습니다.': 'Aborting the {operation_label} failed.',
    '{operation_label}이(가) 진행 중이라 브랜치를 바꿀 수 없습니다.': 'A {operation_label} is in progress, so the branch cannot be switched.',
    '{self_label} 중 예상치 못한 오류가 발생했습니다.': 'An unexpected error occurred during {self_label}.',
    '{subject}의 변경이 이미 반영되어 있어, 이대로 진행하면 이 커밋은 결과에 남지 않습니다.': 'The changes in {subject} are already applied, so continuing would leave this commit out of the result.',
    '구획 {number}': 'Hunk {number}',
    '기대한 브랜치: {expected}': 'Expected branch: {expected}',
    '다른 git 프로세스가 저장소를 쓰고 있을 수 있습니다. 잠시 후 다시 시도하거나, 계속 실패하면 터미널에서 `git {sequencer_command_op} --abort`로 정리해 주세요.': 'Another git process may be using the repository. Try again shortly, or clean up with `git {sequencer_command_op} --abort` in a terminal if it keeps failing.',
    "로컬 변경과 충돌해 '{name}' 브랜치로 전환할 수 없습니다.": "Local changes conflict, so the branch cannot be switched to '{name}'.",
    '변경 내용을 찾을 수 없습니다: {path}': 'No changes found for: {path}',
    '부분 {action}에 실패했습니다.': 'Partial {action} failed.',
    '브랜치가 이미 있습니다: {name}': 'The branch already exists: {name}',
    '브랜치를 찾을 수 없습니다: {name}': 'Branch not found: {name}',
    '생략된 커밋: {shas}': 'Skipped commits: {shas}',
    '선택 줄 {verb}': '{verb} selected lines',
    '아직 해결되지 않은 충돌이 {len_remaining}개 있습니다.': '{len_remaining} conflicts are still unresolved.',
    '원격 작업에 실패했습니다 (exit {result_returncode}).': 'The remote operation failed (exit {result_returncode}).',
    '원격 추적 참조를 찾을 수 없습니다: {upstream_ref}': 'Remote-tracking reference not found: {upstream_ref}',
    '원격에서 {int_idle_s}초 동안 아무 응답이 없어 중단했습니다.': 'Stopped after {int_idle_s} seconds of silence from the remote.',
    '원격이 없습니다: {name}': 'No such remote: {name}',
    '원격이 이미 있습니다: {name}': 'The remote already exists: {name}',
    '저장소 상태: {self__repo_state!r}': 'Repository state: {self__repo_state!r}',
    '저장소 상태: {state!r}': 'Repository state: {state!r}',
    '절대 상한 {ABSOLUTE_TIMEOUT_S}초를 넘겼습니다.': 'Exceeded the absolute limit of {ABSOLUTE_TIMEOUT_S} seconds.',
    '줄 단위로 고르기 — {path}': 'Choose line by line — {path}',
    '진행 없음 기준: {stall_timeout_s}초. 전송이 느린 것은 중단 사유가 아니며, 진행이 멈춘 경우에만 끊습니다.': 'No-progress threshold: {stall_timeout_s} seconds. A slow transfer is not a reason to stop — only a stalled one is.',
    '충돌 {len_outcome_conflict}개를 해결해야 합니다.': '{len_outcome_conflict} conflicts need resolving.',
    '충돌한 파일: {paths}': 'Conflicted files: {paths}',
    '커밋 {len_result_skipped_a}개는 이미 upstream에 같은 변경이 있어 생략했습니다.': '{len_result_skipped_a} commits were skipped — upstream already has the same changes.',
    '커밋을 찾을 수 없습니다: {sha__12}': 'Commit not found: {sha__12}',
    '커밋이 아닙니다: {sha__12}': 'Not a commit: {sha__12}',
    '해결되지 않은 충돌 {len_unresolved}개가 남아 커밋할 수 없습니다.': '{len_unresolved} unresolved conflicts remain, so this cannot be committed.',
    '헝크 {verb}': '{verb} hunk',
    '현재 브랜치: {head_branch}': 'Current branch: {head_branch}',
}
