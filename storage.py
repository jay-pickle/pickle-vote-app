"""투표 앱 데이터 저장소.

모든 데이터는 JSON 파일 하나에 모아 저장합니다.
- 로컬 실행: data/vote_data.json 파일
- Streamlit Cloud: secrets 에 [github] 설정이 있으면 비공개 GitHub 저장소의 JSON 파일
  (Streamlit Cloud 는 앱이 재시작되면 로컬 파일이 지워지기 때문)

데이터 구조
{
  "classes": {
    "26가을|AI파이썬랩 심화|토/1100": {
      "semester": "26가을", "course": "AI파이썬랩 심화", "timeslot": "토/1100",
      "registration_open": true, "voting_open": true, "revealed": false,
      "projects": {"학생 이름": {"title", "reason", "features", "url", "pin": {"salt", "hash"}, "updated_at"}},
      "votes": {"투표한 학생": {"voted_at": "...", "picks": {"부문": {"pick": "고른 학생", "reason": "고른 이유"}}}}
    }
  }
}
"""

import base64
import copy
import json
import threading
import time
from pathlib import Path

import requests


def empty_data():
    return {"version": 1, "classes": {}}


class LocalStore:
    """내 컴퓨터의 JSON 파일에 저장"""

    name = "로컬 파일"

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def load(self):
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return empty_data()

    def update(self, change, message="update"):
        """최신 데이터를 읽고 change(data) 로 고친 뒤 저장. change 가 ValueError 를 내면 저장하지 않음"""
        with self.lock:
            data = self.load()
            result = change(data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return result


class GitHubStore:
    """GitHub 저장소의 JSON 파일에 저장 (Contents API)"""

    name = "GitHub"
    CACHE_SECONDS = 5

    def __init__(self, token, repo, path="vote_data.json", branch="main"):
        self.url = f"https://api.github.com/repos/{repo}/contents/{path}"
        self.branch = branch
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.lock = threading.Lock()
        self._data = None
        self._sha = None
        self._loaded_at = 0.0

    def _fetch(self):
        res = requests.get(self.url, headers=self.headers, params={"ref": self.branch}, timeout=15)
        if res.status_code == 404:
            return empty_data(), None
        res.raise_for_status()
        info = res.json()
        if info.get("content"):
            text = base64.b64decode(info["content"]).decode("utf-8")
        else:  # 1MB 가 넘는 파일은 content 가 비어 있어서 원본을 따로 받기
            raw = requests.get(
                self.url,
                headers={**self.headers, "Accept": "application/vnd.github.raw+json"},
                params={"ref": self.branch},
                timeout=30,
            )
            raw.raise_for_status()
            text = raw.text
        return json.loads(text), info["sha"]

    def _refresh(self, force=False):
        if force or self._data is None or time.time() - self._loaded_at > self.CACHE_SECONDS:
            self._data, self._sha = self._fetch()
            self._loaded_at = time.time()

    def load(self):
        with self.lock:
            self._refresh()
            return copy.deepcopy(self._data)

    def update(self, change, message="update"):
        with self.lock:
            for attempt in range(4):
                self._refresh(force=attempt > 0)
                data = copy.deepcopy(self._data)
                result = change(data)
                body = {
                    "message": message,
                    "content": base64.b64encode(
                        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                    ).decode("ascii"),
                    "branch": self.branch,
                }
                if self._sha:
                    body["sha"] = self._sha
                res = requests.put(self.url, headers=self.headers, json=body, timeout=20)
                if res.status_code in (409, 422):  # 다른 곳에서 먼저 저장함 → 다시 읽고 재시도
                    time.sleep(0.5 * (attempt + 1))
                    continue
                res.raise_for_status()
                self._data = data
                self._sha = res.json()["content"]["sha"]
                self._loaded_at = time.time()
                return result
            raise RuntimeError("저장이 계속 충돌했어요. 잠시 후 다시 시도해 주세요.")


def make_store(secrets):
    """secrets 에 [github] 설정이 있으면 GitHub, 없으면 로컬 파일"""
    try:
        gh = dict(secrets["github"])
    except Exception:
        gh = None
    if gh and gh.get("token") and gh.get("repo"):
        return GitHubStore(
            token=gh["token"],
            repo=gh["repo"],
            path=gh.get("path", "vote_data.json"),
            branch=gh.get("branch", "main"),
        )
    return LocalStore(Path(__file__).parent / "data" / "vote_data.json")
