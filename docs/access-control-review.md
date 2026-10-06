# 앱 입장 제한 검토 (보류)

- 작성: 2026-10-02
- 상태: **보류**. 지금은 아래 "현재 적용된 보호"만 쓰고, 필요해지면 이 문서에서 다시 시작한다.

## 배경

앱 주소만 알면 누구나 학생 이름, 프로젝트 정보, 공개된 투표 결과를 볼 수 있다.
수업과 관계없는 사람이 학생 정보를 보지 못하게 앱 입장 자체를 제한하는 방법을 검토했다.

## 현재 적용된 보호

- 학생 로그인(반 + 이름 + PIN 4자리): 로그인 전에는 반 내용이 보이지 않음. 선생님이 넣은 반별 명단의 이름만 처음에 PIN 을 정하고 들어올 수 있음. PIN 은 해시(PBKDF2)로만 저장, 5번 틀리면 5분 잠금, 선생님이 🧹 정리 탭에서 바꾸거나 지울 수 있음
- 선생님 비밀번호: Streamlit secrets 의 `ADMIN_PASSWORD`. 10번 틀리면 1분 잠금
- 배포 환경에서는 기본 비밀번호(`dlab`)가 동작하지 않음 (localhost 에서만 허용)
- 데이터는 비공개 저장소 `jay-pickle/pickle-vote-data` 에 저장

명단에 있지만 아직 PIN 을 정하지 않은 이름은 그 이름을 아는 다른 사람이 먼저 PIN 을 정할 수 있다 (수업 시간에 다 같이 정하면 거의 문제 없음).

## 검토한 방안

### 1. 수업 입장 코드

선생님이 수업 중에 반별 4자리 코드를 만들어 화면에 띄우고, 학생은 처음 화면에서 코드를 입력해야 자기 반에 들어간다.

- 반마다 코드를 따로 두고, 코드를 넣으면 그 반이 바로 열림 (학기·수업·시간대 목록도 숨김)
- 관리 탭에서 "입장 열기/닫기", 일정 시간(예: 3시간) 뒤 자동 만료
- 코드는 주소에 넣지 않음 (링크로 퍼지지 않게). 새로고침하면 다시 입력
- 틀린 시도가 많으면 잠시 잠금

장점: 외부 서비스·계정 설정 없이 바로 만들 수 있음. 다른 반 데이터도 차단.
한계: 반 학생은 모두 코드를 알기 때문에 밖으로 알려 주면 못 막음 (문 잠금 수준). 입장을 닫으면 집에서 결과를 다시 볼 수 없음.

### 2. Streamlit Community Cloud 비공개 앱 + 이메일 초대

앱 설정 → Sharing → "Only specific people can view this app" 에서 학생 이메일을 초대.
Google 계정이면 Google 로, 아니면 이메일로 받은 1회용 링크로 로그인.

**권장하지 않음.**

- 초대된 사람은 선생님 워크스페이스의 모든 공개 앱 통계를 볼 수 있고 다른 사람을 초대할 수 있음. 통계 화면에 초대된 사람 모두의 이메일이 보여서 학생 이메일이 서로 노출됨
- Streamlit 1.42 부터는 `[auth]` 설정 없이 `st.user` 로 접속자 이메일을 알 수 없음 → 앱이 누가 들어왔는지 모르므로 반 구분·본인 확인은 여전히 PIN 필요
- 반 단위 제한 불가 (앱 전체 입장만)
- 무료 계정은 비공개 앱을 한 번에 하나만 둘 수 있음

### 3. GitHub 로그인 + 반별 명단 (계정 방식 중 추천)

학생들은 모두 GitHub 계정으로 Streamlit Cloud 에 앱을 배포해 본 경험이 있다.

- 선생님이 관리 탭에서 반마다 "이름 ↔ GitHub 아이디" 명단을 등록 (학생 앱 저장소 주소 `github.com/아이디/...` 에서 확인 가능)
- 학생은 "GitHub 로 로그인" → 명단에 있으면 자기 반으로 바로 입장, 이름 자동 지정. 없으면 아무것도 볼 수 없음
- PIN 이 필요 없어짐 (계정이 본인 확인)

구현 메모:

- `st.login()` 은 OIDC 만 지원하고 일반 OAuth 는 못 씀. GitHub 로그인은 OIDC 가 아니므로 Auth0 같은 중개 서비스의 GitHub 소셜 연결을 거쳐야 함
- 설정할 것: Auth0 테넌트 + Regular Web Application, GitHub OAuth App (콜백: `https://<tenant>.auth0.com/login/callback`), Auth0 Allowed Callback URL `https://pickle-vote.streamlit.app/oauth2callback`, Streamlit secrets `[auth]` (`redirect_uri`, `cookie_secret`, `client_id`, `client_secret`, `server_metadata_url`)
- GitHub 이메일은 비공개일 수 있으니 명단은 이메일이 아니라 GitHub 아이디(`nickname`) 또는 고유 ID(`sub`) 로 대조
- 선생님은 지금처럼 비밀번호로 관리자 진입 (또는 선생님 GitHub 아이디를 관리자로 지정)

감수할 점:

- 로그인을 설정한 앱은 Community Cloud 의 비공개 앱 1개 자리를 차지함 (현재 다른 앱은 모두 공개라 자리는 비어 있음)
- 로그인 쿠키는 30일 유지, 앱에서 로그아웃해도 GitHub 로그인은 남음 → 공용 PC 라면 수업 끝에 GitHub 로그아웃 안내 필요
- 학생 GitHub 아이디·이메일이 외부 서비스(Auth0)를 거침 → 학부모 안내가 필요한지 학원 방침 확인

Google 로그인: Auth0 없이 Google 에 바로 연결할 수 있어 설정은 더 쉽지만, 만 14세 미만은 보호자 동의가 있어야 Google 계정을 만들 수 있어 계정이 없는 학생이 있을 수 있다. Auth0 를 쓰면 GitHub 와 Google 을 함께 켤 수 있다.

## 비교

| | 입장 코드 | 비공개 앱 + 이메일 초대 | GitHub 로그인 (Auth0) |
|---|---|---|---|
| 외부인 차단 | 수업 시간 동안 (코드 공유 시 뚫림) | 완전 | 완전 |
| 반 단위 제한 | 가능 | 불가 | 가능 |
| 본인 확인 | PIN 유지 | PIN 유지 | 계정으로 대체 |
| 학생 이메일 노출 | 없음 | 학생끼리 노출 | 없음 |
| 외부 서비스·설정 | 없음 | Streamlit 설정만 | Auth0 + GitHub OAuth App |
| 코드 수정 | 필요 | 없음 | 필요 |

## 다시 시작할 때 정할 것

1. 방안 선택: 입장 코드 / GitHub 로그인
2. 입장 코드라면: 코드 길이(4자리 또는 6자리), 자동 만료 시간(기본 3시간 제안)
3. GitHub 로그인이라면: 선생님 GitHub 계정(jay-pickle)으로 Auth0 가입·GitHub OAuth App 생성 여부, Google 로그인 함께 켤지, 반별 학생 GitHub 아이디 명단

## 참고

- Streamlit 앱 공유 (비공개 앱, 이메일 초대): https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app
- Streamlit 사용자 인증 (`st.login`, OIDC): https://docs.streamlit.io/develop/concepts/connections/authentication
- `st.user` (Community Cloud 이메일, 비공개 앱 1개 계산): https://docs.streamlit.io/develop/api-reference/user/st.user
- Streamlit + Auth0 예시: https://github.com/andfanilo/streamlit-auth0-test
