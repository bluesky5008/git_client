# 배포 — 환경과 폴더에 관한 결정 기록

버전 0.1.0 배포 패키지를 처음 준비하며 내린 결정과 그 이유다.
설계 원칙(design.md §9)이 "무엇을 만드는가"를 정했다면, 여기는
"어디서 만들고 어디에 두는가"를 정한다.

## 1. 배포 환경 — 어디서 만드는가

### 1.1 macOS는 로컬에서, Windows는 CI에서 만든다

| 산출물 | 만드는 곳 | 왜 |
|--------|----------|-----|
| `gitclient-<버전>-macos-arm64.dmg` | 로컬 (개발 머신, Apple Silicon) | `hdiutil`이 macOS에만 있고, 개발 머신이 macOS다 |
| `gitclient-<버전>-windows-x64-setup.exe` | GitHub Actions `windows-latest` | **개발 머신에서 Windows 바이너리를 만들 수 없다.** PyInstaller는 크로스 컴파일을 지원하지 않는다 — Windows 배포본은 Windows에서 빌드해야 한다 |

교차 빌드(Wine, 크로스 툴체인)는 고려에서 일찍 떨어졌다. PyInstaller가
공식적으로 지원하지 않는 경로이고, "빌드는 됐는데 실제 Windows에서
깨지는" 산출물을 걸러낼 방법이 로컬에 없다. CI의 Windows 러너는
빌드·스모크 테스트(`--version` 실행)·인스톨러 생성까지 실제 Windows에서
수행하므로, 그 산출물을 그대로 배포본으로 쓴다.

**CI 산출물을 배포본으로 쓸 때의 규칙**: 아티팩트를 받기 전에
CI 실행의 커밋(`headSha`)이 배포하려는 커밋과 같은지 확인한다.
이번 배포에서 실제로 이 확인이 일을 했다 — 첫 아티팩트는 버전
메타데이터 수정(§1.3) 이전 커밋의 것이라 버리고 다시 받았다.

### 1.2 아키텍처 — 지금은 arm64(macOS)와 x64(Windows)만

- macOS: GitHub의 `macos-latest` 러너도, 개발 머신도 Apple Silicon이다.
  Intel(x86_64)용 dmg는 **만들 곳이 없어서가 아니라 검증할 곳이 없어서**
  미룬다 — `macos-13`(Intel) 러너를 추가하면 만들 수 있지만, 실행해 볼
  Intel 맥이 없는 채로 내보내는 것은 §1.1에서 배제한 "빌드만 되는
  산출물"과 같다. 수요가 확인되면 러너를 추가한다.
- Windows: `windows-latest`가 x64다. ARM Windows는 x64 에뮬레이션으로
  동작하므로 별도 빌드를 두지 않는다.

### 1.3 배포 환경이 드러낸 결함 — 배포본이 자기 버전을 몰랐다

패키지 준비 중 dmg의 실행 파일이 `--version`에 `0.1.0`이 아니라
`(개발 트리)`를 답했다. 원인은 두 겹이다:

1. `--version`은 `importlib.metadata`로 버전을 읽는데(출처를
   pyproject.toml 한 곳에 두기 위해), PyInstaller는 dist-info
   메타데이터를 기본으로 담지 않는다 → 스펙에 `copy_metadata("gitclient")`
   를 추가했다.
2. `copy_metadata`는 빌드 환경에 패키지가 **설치돼** 있어야 동작한다.
   로컬 venv는 소스 경로 실행 구성이라 메타데이터가 없었다 →
   **배포 빌드 전 `uv pip install -e . --no-deps`가 필수 절차다**
   (CI는 `pip install -e .`를 이미 하므로 추가 조치가 없다).

교훈: 스모크 테스트가 "실행되는가"만 물으면 이런 결함이 통과한다.
CI 스모크가 `--version`의 **출력 내용**까지 확인하도록 강화했다 —
`(개발 트리)`가 나오면 붉어진다.

### 1.4 서명은 배포 절차에 없다 — 의도된 공백

코드서명(macOS Developer ID, Windows 인증서)은 이 저장소가 가질 수
없는 자원이라 배포 절차에서 분리했다. "키가 없으면 배포를 못 한다"가
아니라 "키가 없으면 서명만 빠진다"로 갈라 둔 것이다:

- 서명 없는 dmg: 첫 실행에서 Gatekeeper 경고를 한 번 넘겨야 한다
  (우클릭 → 열기).
- 서명 없는 인스톨러: SmartScreen 경고를 "추가 정보 → 실행"으로 넘긴다.

키를 가진 사람이 이어서 돌릴 명령은 `packaging/make_dmg.sh`와
`packaging/gitclient.iss`의 머리 주석에 있다.

## 2. 배포 폴더 — 어디에 두는가

### 2.1 구조

```
release/
└── 0.1.0/
    ├── gitclient-0.1.0-macos-arm64.dmg
    ├── gitclient-0.1.0-windows-x64-setup.exe
    └── SHA256SUMS.txt
```

- **버전별 하위 폴더** — 다음 버전을 준비할 때 이전 배포본이 덮이지
  않는다. "지금 사용자에게 준 파일이 정확히 무엇이었나"는 배포 후에
  가장 자주 받는 질문이다.
- **파일명에 버전·OS·아키텍처를 전부 넣는다** — 파일은 폴더를 떠나
  돌아다닌다(메일, 메신저, 다운로드 폴더). 파일명만으로 정체가 확인되지
  않으면 "이거 최신 맞아요?"에 답할 수 없다. `gitclient.dmg` 같은
  무명 파일명은 빌드 산출물(`dist/`)에서만 허용한다.
- **SHA256SUMS.txt** — 서명이 없는 지금(§1.4), 받은 파일이 온전한지
  확인할 유일한 수단이다. 검증: `shasum -a 256 -c SHA256SUMS.txt`
  (Windows는 `certutil -hashfile <파일> SHA256`으로 값 비교).

### 2.2 git이 추적한다 (2026-07-31 사용자 결정으로 변경)

처음에는 `.gitignore`로 제외했다 — 바이너리는 히스토리에 넣는 순간
영구히 저장소를 무겁게 하고(80MB 배포본 몇 번이면 clone이 느려진다),
공개 채널로는 GitHub Releases가 맞다는 판단이었다.

**사용자 결정으로 뒤집혔다**: `release/<버전>/`을 저장소가 직접
추적한다. 저장소 하나로 코드와 배포본이 함께 보관되는 단순함을
택한 것이다. 그 대가는 위 문단 그대로 남는다 — 버전이 쌓일수록
clone이 무거워지고, GitHub는 파일당 100MB에서 push를 거부한다
(현재 dmg 45MB·인스톨러 35MB로 한도 안). 무게가 문제가 되는 시점에
Git LFS나 GitHub Releases로 옮기는 선택지는 열려 있다.

### 2.3 dist/와 release/의 구분

| 폴더 | 성격 | 파일명 | git |
|------|------|--------|-----|
| `dist/` | 빌드 작업대 — PyInstaller·hdiutil이 쓰고 지우는 곳 | 무명 (`gitclient.dmg`) | 무시 |
| `release/<버전>/` | 배포 준비대 — 검증 끝난 산출물만 옮겨 담는 곳 | 정식 (`gitclient-0.1.0-macos-arm64.dmg`) | **추적** (§2.2) |

`dist/`에서 `release/`로 옮기는 조건: 해당 커밋의 CI가 전부 녹색이고,
산출물이 `--version`에 제 버전을 답하고, 체크섬을 기록했다.

## 3. 이번 배포(0.1.0)의 절차 기록

재현 가능하도록 실제 수행한 순서 그대로다.

```sh
# 0. 배포 커밋 확정 — CI 전부 녹색인지 확인
gh run list --limit 1        # 커밋과 결론 확인

# 1. macOS (로컬, Apple Silicon)
uv pip install -e . --no-deps --python .venv/bin/python   # §1.3-2
.venv/bin/pyinstaller -y --distpath dist --workpath build packaging/gitclient.spec
./dist/gitclient/gitclient --version    # "gitclient 0.1.0"이어야 한다
sh packaging/make_dmg.sh

# 2. Windows (CI 산출물 회수)
gh run download <run-id> -n gitclient-Windows -D <임시폴더>
#    <run-id>의 headSha == 배포 커밋인지 먼저 확인 (§1.1)

# 3. 준비대에 정식 이름으로 담고 체크섬 기록
mkdir -p release/0.1.0
cp dist/gitclient.dmg release/0.1.0/gitclient-0.1.0-macos-arm64.dmg
cp <임시폴더>/gitclient-setup.exe release/0.1.0/gitclient-0.1.0-windows-x64-setup.exe
(cd release/0.1.0 && shasum -a 256 *.dmg *.exe > SHA256SUMS.txt)

# 4. (선택) 공개 — GitHub Releases
# gh release create v0.1.0 release/0.1.0/* --title "0.1.0"
```

## 4. 서명 — 인증서를 꽂으면 명령 하나로 (2026-07-31 배선 완료)

서명 자체는 인증서가 있어야 하지만, **절차는 미리 코드에 넣어 두었다** —
인증서가 생기는 날 스크립트를 다시 돌리면 끝나도록. 배선은 환경변수·
전처리 플래그로만 켜져서, 없는 환경에서는 같은 파일이 서명 없이 끝까지
돈다.

### 4.1 macOS

**취득**: [Apple Developer Program](https://developer.apple.com/programs/)
가입(연 $99·개인 가능) → Certificates에서 **Developer ID Application**
인증서 발급 → 키체인에 설치 → `security find-identity -v -p codesigning`
으로 신원 문자열 확인. 공증용으로
`xcrun notarytool store-credentials <프로필이름>`(Apple ID + 앱 암호)을
한 번 저장한다.

**실행** (빌드부터 다시 — 공증은 dmg 속 바이너리의 서명을 본다):

```sh
export GITCLIENT_SIGN_IDENTITY="Developer ID Application: <이름> (<팀ID>)"
export GITCLIENT_NOTARY_PROFILE=<프로필이름>
uv pip install -e . --no-deps --python .venv/bin/python
.venv/bin/pyinstaller -y --distpath dist --workpath build packaging/gitclient.spec
sh packaging/make_dmg.sh          # 서명 → 공증 → 스테이플까지 이어서 한다
```

**배선이 실제로 확인된 범위** (2026-07-31, ad-hoc 신원 `-`로 실측):
신원이 수집된 모든 Mach-O에 전파되고 hardened runtime이 붙는 것까지
확인했다 — 공증이 요구하는 형태다. ad-hoc으로는 **실행이 안 되는데**
(hardened runtime의 라이브러리 검증이 Team ID 일치를 요구하고 ad-hoc에는
Team ID가 없다), 실인증서는 모든 바이너리가 같은 Team ID를 가지므로
이 문제가 없다. 즉 끝단 확인(실행·공증 통과)만 실인증서의 몫이다.

### 4.2 Windows

**취득**: 둘 중 하나 —
- **Azure Trusted Signing** (월 ~$10, 개인은 3년 이상 된 신원 필요):
  signtool과 연동되는 클라우드 서명. 2023년 이후 신생 OV 인증서보다
  SmartScreen 평판 축적이 빠른 편이다.
- **OV 코드서명 인증서** (연 $70~$200, Sectigo/Certum 등): 2023-06부터
  하드웨어 토큰(HSM) 의무라 우편으로 토큰을 받는다.

**실행** (인증서가 서명 저장소에 있는 상태에서):

```bat
iscc /DSIGN "/Ssigntool=signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /a $f" packaging\gitclient.iss
```

`/DSIGN`이 없으면 서명 단계가 스크립트에서 통째로 사라진다(전처리) —
CI의 무서명 빌드가 그대로 도는 이유다. 서명 대상은 인스톨러·언인스톨러·
`gitclient.exe` 석 점이다: 인스톨러만 서명하면 SmartScreen은 조용한데
설치된 앱이 실행될 때 다시 경고를 띄우고, 반대로 Qt DLL 수백 장까지
서명하는 것은 시간 대비 이득이 없다(경고를 내는 주체는 실행 파일이다).

### 4.3 서명하면 SHA256SUMS를 다시 만든다

서명은 파일 내용을 바꾼다 — §2.1의 준비대에 서명본을 다시 담고
체크섬도 다시 기록해야 한다. 서명 전 체크섬은 서명본과 일치하지 않는다.
