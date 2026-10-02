# 🏆 피클 프로젝트 투표 앱

AI파이썬랩 심화 11주차 「나만의 서비스 발표회」에서 쓰는 Streamlit 투표 웹앱입니다.

## 구조

- **학기** (예: 26여름, 26가을, 27봄): 최신 학기부터 (연도 → 봄·여름·가을·겨울 내림차순)
- **수업** (예: AI파이썬랩 기본, AI파이썬랩 심화): 가나다/알파벳 오름차순
- **시간대** (예: 토/0900, 토/1100, 수/1930): 가나다/알파벳 오름차순
- 학기 + 수업 + 시간대 = **반**. 학생은 자기 반을 고르고 그 반 안에서만 등록·투표합니다.

## 사용 흐름

| 탭 | 하는 일 |
|---|---|
| 📝 프로젝트 등록 | 이름, PIN 4자리, 프로젝트 이름, 만든 이유, 기능 설명, 배포한 URL 입력. 고칠 때는 이름 + PIN으로 들어오기 |
| 🔍 프로젝트 보기 | 반 친구들의 프로젝트 카드와 앱 바로가기. 카드의 ✏️ 수정 버튼을 누르고 그 학생의 PIN을 입력하면 바로 수정 |
| 🗳️ 투표하기 | 이름 + PIN으로 들어온 뒤 세 부문(써 보고 싶은 / 아이디어 / 완성도)마다 친구 프로젝트 하나 + 고른 이유. 내 프로젝트는 목록에 없고, 다시 제출하면 새 투표로 바뀜 |
| 🏆 결과 보기 | 투표 진행 현황, 선생님이 공개하면 종합 순위·부문별 순위. 내가 받은 한마디는 이름 + PIN으로 들어와야 보임 |
| ⚙️ 관리 (선생님) | 반 만들기, 등록/투표/결과 공개 켜고 끄기, 프로젝트 삭제, 학생 PIN 다시 정하기, 투표 초기화, 반 삭제, 학생용 주소, JSON 백업 |

사이드바의 **🔐 선생님 메뉴**에서 비밀번호(`ADMIN_PASSWORD`)를 넣으면 관리 탭이 나옵니다.

## 보안

- 학생 PIN은 해시(PBKDF2)로만 저장하고, 5번 틀리면 그 학생은 5분 동안 잠깁니다. 선생님이 관리 탭에서 PIN을 다시 정하면 잠금도 풀립니다.
- 선생님 비밀번호는 10번 틀리면 1분 동안 잠깁니다.
학생용 주소(`?semester=26가을&course=...&time=토/1100`)로 들어오면 반이 바로 선택됩니다.

## 데이터 저장

모든 데이터는 JSON 파일 하나(`vote_data.json`)에 모입니다.

- **로컬 실행**: `data/vote_data.json`
- **Streamlit Cloud**: 비공개 저장소 `jay-pickle/pickle-vote-data` 의 `vote_data.json`
  (Streamlit Cloud 는 앱이 잠들거나 재시작되면 로컬 파일이 지워지기 때문)

## 실행

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

`ADMIN_PASSWORD` 가 없으면 내 컴퓨터(localhost)에서만 테스트용 비밀번호 `dlab` 이 동작하고, 배포 환경에서는 선생님 메뉴가 꺼집니다.

## Streamlit Cloud secrets

```toml
ADMIN_PASSWORD = "선생님 비밀번호"

[github]
token = "github_pat_..."          # pickle-vote-data 에 Contents 읽기/쓰기 권한만 준 fine-grained 토큰
repo = "jay-pickle/pickle-vote-data"
path = "vote_data.json"
branch = "main"
```
