# DCR-003: 자격증명 공급을 askpass shim에서 일회성 credential helper로

- 상태: 반영 완료 (승인 2026-07-31 사용자 — 일괄 위임에 따라 구현을 먼저
  진행하고 사후 승인받음. 구현·검증 기록: [record.md](record.md))
- 발견 위치: backlog §3.10 (2026-07-31 macOS·git 2.50.1 실측 — 인증 테스트 20개 실패)
- 현재 설계 기준선: design.md v2.4 (§4.8), ADR-28~33
- 관련 요구사항: G1 (인증이 필요한 원격 협업), §4.8 "저장분 재사용 + 우리 프롬프트"
- 관련 ADR: ADR-28(유지), ADR-29(유지), ADR-31(**대체 대상** — 근거 소멸), ADR-32·33(유지)

## 변경 사유와 증거

git 2.50.1에서 `credential.interactive=false`가 **앱 자신의 GIT_ASKPASS
shim 호출까지 차단한다** — 사용자가 방금 입력한 자격증명을 공급하는 경로가
`fatal: unable to get password from user`로 죽는다 (실측: 같은 명령에서
이 설정만 빼면 성공). §4.8이 서 있던 실측("interactive=false여도 askpass는
동작")이 최신 git에서 무너졌다.

`credential.interactive=false`를 빼는 것은 답이 아니다 — 그 설정이 막는
것은 credential helper(GCM 등)의 자체 UI이고(ADR-28, 실측 12초+ 무한
대기), 재시도 시점엔 helper가 빈손임이 확인된 뒤라 interactive를 열면
helper가 자기 창을 띄울 수 있다.

**해법의 근거가 이미 코드 주석에 있었다**: "interactive=false여도 helper의
**get**은 정상 동작한다" (Windows 실측). 2026-07-31 git 2.50.1 재실측으로
재확인했다:

| 시나리오 | 결과 |
|---|---|
| `credential.interactive=false` + 인라인 helper(get) | **fetch 성공** |
| 체인 재설정(`credential.helper=`) 후 우리 helper만 (저장 안 함 경로) | 성공 |
| 적대적 비밀번호 `a>b&echo pwn^!` | 성공 — 값 그대로 전달 |

재현: `experiments/exp_helper.py`.

## 기존 설계

- 값 공급: `GIT_ASKPASS` → 임시 디렉터리에 쓴 shim 파일(.bat/.sh)이
  환경변수를 읽어 응답 (§4.8.2, ADR-29·31)
- shim 파일이 잠시나마 디스크에 존재 (§4.8.7 잠정 우려 🔶)
- Windows는 cmd.exe 지연 확장으로 메타문자 방어 (ADR-31)

## 제안 설계

자격증명이 있을 때 그 명령에 한해 **일회성 credential helper를 설정으로
주입한다**:

```
-c credential.helper=!f() { [ "$1" = get ] && printf 'username=%s\npassword=%s\n' "$GITCLIENT_ASKPASS_USERNAME" "$GITCLIENT_ASKPASS_PASSWORD"; }; f
```

- **askpass가 아니라 helper의 get이다** — `credential.interactive=false`가
  계속 유효하므로 외부 helper의 자체 UI 차단(ADR-28)이 그대로 선다.
  `GIT_ASKPASS=""`도 그대로 둔다.
- **값은 여전히 환경변수로만** (ADR-29 유지). 명령줄에는 변수 이름만 실린다.
- **파일이 아예 없어진다** — §4.8.7의 "shim 임시 파일 노출" 잠정 우려가
  대상 소멸로 해소된다.
- **셸 경로가 하나가 된다** — `!` helper는 Windows에서도 git 동봉 sh로
  실행되므로 cmd.exe 파싱 위험 자체가 사라진다. ADR-31(지연 확장)은 지킬
  대상이 없어져 대체된다. `printf '%s' "$VAR"`는 메타문자를 재해석하지
  않는다 (실측).
- **체인에서의 위치**: "저장 안 함"이면 `credential.helper=`(재설정) 뒤에
  우리 helper를 더한다 — 조회·저장 모두 외부 helper를 거치지 않는다
  (ADR-32 유지). "저장"이면 기존 체인 뒤에 더해져, 저장된 값 재사용이
  먼저 시도되는 기존 순서가 유지된다.

## 변경 항목

| 위치 | 기존 | 변경 |
|------|------|------|
| askpass.py | write_shim/.bat/.sh 생성 + shim_environment | `credential_helper_config()` + `credential_environment()` — 파일 생성 없음 |
| remote_engine.py | shim 임시 디렉터리 생성·정리 | helper `-c` 주입 (체인 재설정 뒤) |
| 테스트 | shim 파일 내용·cmd 메타문자 검증 | helper 명령 내용·sh 실행 검증으로 치환 (적대적 비밀번호 목록은 유지) |

## 영향 범위

- ADR-28·29·32·33 유지, ADR-31 대체 (ADR-78)
- §4.8.7 잠정 결정(🔶 shim 파일 노출) 해소 — backlog §6에서 제거
- 인증 통합 테스트 20개가 이 경로를 그대로 검증한다

## 마이그레이션 및 롤백

상태·데이터 없음. 롤백은 코드 되돌리기로 충분하다.

## 위험

- **Windows 미검증** — `!` helper의 sh 실행은 Git for Windows 표준 동작이나
  이 환경에 Windows가 없어 실측하지 못했다. Windows에서 스위트 재실행 필요.
- git이 어느 버전부터 askpass를 차단했는지 경계 미확정 — helper 경로는
  구·신 버전 모두에서 동작하므로 경계 확정은 필수가 아니다.

## 검증 방법

- 기존 인증 스위트 20개 (실서버 왕복 포함) 전부 통과 (AC)
- 적대적 비밀번호가 변형·유출 없이 전달, 파일 생성 0건 (AC)
- helper 명령 문자열에 비밀번호가 들어가지 않음 (AC)
