import hmac
import html
import io
import json
import re
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import segno
import streamlit as st

from storage import make_store

st.set_page_config(page_title="피클 프로젝트 투표", page_icon="🏆", layout="wide")

# ── 설정 ──────────────────────────────────────────────
CATEGORIES = {
    "가장 써 보고 싶은 서비스": "내 생활에서 자주 쓰고 싶은 서비스",
    "아이디어가 돋보인 서비스": "불편을 새로운 방법으로 풀어낸 서비스",
    "완성도가 높은 서비스": "오류 없이 동작하고 화면이 보기 좋은 서비스",
}
SHORT = {"가장 써 보고 싶은 서비스": "써 보고 싶은", "아이디어가 돋보인 서비스": "아이디어", "완성도가 높은 서비스": "완성도"}

# 발표회는 이 순서대로 한 단계씩 진행
STAGES = ["ready", "register", "vote", "result"]
STAGE_NAMES = {"ready": "준비", "register": "프로젝트 등록", "vote": "투표", "result": "결과 공개"}
NEXT_LABEL = {"ready": "프로젝트 등록 시작", "register": "등록 마감하고 투표 시작", "vote": "투표 마감하고 결과 공개"}
STAGE_HELP = {
    "ready": "학생은 이름을 골라 들어온 뒤 기다려요.",
    "register": "학생이 자기 프로젝트를 등록하고 고칠 수 있어요.",
    "vote": "학생이 친구 프로젝트를 둘러보고 투표해요. 프로젝트는 선생님만 고칠 수 있어요.",
    "result": "학생 화면에 순위와 내가 받은 한마디가 보여요.",
}

CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 헷갈리는 0 O 1 I L 은 뺌
CODE_LEN = 4
KEEP_DAYS = 90  # 만든 지 이만큼 지난 발표회는 자동으로 지움
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
KST = ZoneInfo("Asia/Seoul")


class NoChange(Exception):
    """store.update 안에서 바꿀 게 없을 때 저장하지 않고 빠져나오기"""


@st.cache_resource
def get_store():
    return make_store(st.secrets)


store = get_store()


def now():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def is_local():
    """내 컴퓨터(localhost)에서 실행 중인지"""
    try:
        host = urlparse(st.context.url).hostname or ""
    except Exception:
        host = ""
    return host in ("localhost", "127.0.0.1", "::1")


def admin_password():
    """secrets 의 ADMIN_PASSWORD. 설정이 없으면 내 컴퓨터에서만 테스트용 'dlab' 허용, 배포 환경에서는 선생님 메뉴를 끔"""
    try:
        pw = str(st.secrets["ADMIN_PASSWORD"])
        if pw:
            return pw
    except Exception:
        pass
    return "dlab" if is_local() else None


def app_url():
    try:
        return st.context.url.split("?")[0]
    except Exception:
        return ""


def clean(text):
    return " ".join((text or "").split())


def nospace(text):
    return "".join((text or "").split())


def md(text):
    """마크다운 표 안에서 깨지지 않게"""
    return text.replace("|", "\\|").replace("*", "\\*").replace("_", "\\_")


def label(s, name):
    return f"{s['projects'][name]['title']} · {name}"


# ── 잘못된 시도 제한 (모든 접속자가 공유하는 서버 메모리) ────
@st.cache_resource
def get_attempts():
    return {}


def admin_locked():
    info = get_attempts().get("admin")
    return max(0, int(info["until"] - time.time())) if info else 0


def admin_fail():
    info = get_attempts().setdefault("admin", {"count": 0, "until": 0})
    info["count"] += 1
    if info["count"] >= 10:
        info.update(count=0, until=time.time() + 60)


def code_blocked():
    """1분 안에 틀린 입장 코드가 20번 넘게 들어오면 잠시 막기 (코드 맞히기 방지)"""
    fails = get_attempts().setdefault("code_fails", [])
    cutoff = time.time() - 60
    fails[:] = [t for t in fails if t > cutoff]
    return len(fails) >= 20


def code_fail():
    get_attempts().setdefault("code_fails", []).append(time.time())


# ── 데이터 형식 ──────────────────────────────────────
def new_code(existing):
    while True:
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(CODE_LEN))
        if code not in existing:
            return code


def timeslot_text(ts):
    """'토/1100' → '토 11:00'"""
    m = re.match(r"^(.)/(\d{2})(\d{2})$", ts or "")
    return f"{m.group(1)} {m.group(2)}:{m.group(3)}" if m else (ts or "")


def migrate(data):
    """예전 형식(학기·수업·시간대 반 + 학생 PIN)을 발표회 형식으로 바꾸기. 바꾼 게 있으면 True"""
    changed = False
    if "sessions" not in data:
        data["sessions"] = {}
        changed = True
    old = data.pop("classes", None)
    if old:
        changed = True
        for c in sorted(old.values(), key=lambda c: c.get("created_at", "")):
            students = {name: {"added_at": s.get("created_at", now())} for name, s in c.get("students", {}).items()}
            projects = {}
            for name, p in c.get("projects", {}).items():
                students.setdefault(name, {"added_at": p.get("created_at", now())})
                projects[name] = {k: v for k, v in p.items() if k != "pin"}  # PIN 은 더 이상 쓰지 않음
            if c.get("revealed"):
                stage = "result"
            elif c.get("registration_open", True):
                stage = "register"
            elif c.get("voting_open", True):
                stage = "vote"
            else:
                stage = "ready"
            code = new_code(data["sessions"])
            data["sessions"][code] = {
                "name": clean(f"{c.get('semester', '')} {c.get('course', '')} {timeslot_text(c.get('timeslot'))}"),
                "stage": stage,
                "created_at": c.get("created_at", now()),
                "students": students,
                "projects": projects,
                "votes": c.get("votes", {}),
            }
    if data.get("version") != 2:
        data["version"] = 2
        changed = True
    return changed


def load_data():
    data = store.load()
    if "classes" in data or data.get("version") != 2:
        def change(d):
            if not migrate(d):
                raise NoChange

        try:
            store.update(change, "migrate: 반 → 발표회 (입장 코드)")
        except NoChange:
            pass
        data = store.load()
    return data


def expire_date(s):
    try:
        made = datetime.strptime(s.get("created_at", ""), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return made + timedelta(days=KEEP_DAYS)


def purge_old(sessions):
    """만든 지 KEEP_DAYS 일이 지난 발표회 지우기 (선생님이 들어올 때, 한 시간에 한 번만 확인)"""
    marks = get_attempts()
    if time.time() - marks.get("purged_at", 0) < 3600:
        return
    marks["purged_at"] = time.time()
    today = datetime.now(KST).replace(tzinfo=None)
    if not any((d := expire_date(s)) and d < today for s in sessions.values()):
        return

    def change(data):
        old = [c for c, s in data["sessions"].items() if (d := expire_date(s)) and d < today]
        if not old:
            raise NoChange
        for c in old:
            del data["sessions"][c]
        return old

    try:
        removed = store.update(change, "auto delete old sessions")
        st.session_state.flash = f"만든 지 {KEEP_DAYS}일이 지난 발표회 {len(removed)}개를 자동으로 지웠어요."
        st.rerun()
    except NoChange:
        pass


# ── 집계 ──────────────────────────────────────────────
def valid_ballots(s):
    """명단에 있는 학생이 낸 투표만, 등록된 프로젝트를 고른 것만"""
    projects = s["projects"]
    ballots = {}
    for voter, vote in s["votes"].items():
        if voter not in s["students"]:
            continue
        ballots[voter] = {
            cat: pick for cat, pick in vote.get("picks", {}).items()
            if cat in CATEGORIES and pick.get("pick") in projects and pick["pick"] != voter
        }
    return ballots


def ranking(scores):
    """{이름: 점수} → [(순위, 이름, 점수)] 공동 순위 처리 (1, 1, 3 ...)"""
    items = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(1 + sum(1 for v in scores.values() if v > score), name, score) for name, score in items]


# ── 데이터 변경 함수 (store.update 에 넘김) ─────────────
def get_session(data, code):
    s = data["sessions"].get(code)
    if s is None:
        raise ValueError("발표회를 찾을 수 없어요. 새로고침해 주세요.")
    return s


def parse_names(text):
    """한 줄에 한 명. 띄어쓰기만 다른 이름은 하나로"""
    names, seen = [], set()
    for line in (text or "").splitlines():
        name = clean(line)
        if name and nospace(name) not in seen:
            seen.add(nospace(name))
            names.append(name)
    return names


def create_session(name, names):
    def change(data):
        code = new_code(data["sessions"])
        data["sessions"][code] = {
            "name": name, "stage": "ready", "created_at": now(),
            "students": {n: {"added_at": now()} for n in names},
            "projects": {}, "votes": {},
        }
        return code

    return store.update(change, f"create session: {name}")


def add_students(code, names):
    def change(data):
        s = get_session(data, code)
        existing = {nospace(n) for n in s["students"]}
        added = []
        for name in names:
            if nospace(name) not in existing:
                s["students"][name] = {"added_at": now()}
                existing.add(nospace(name))
                added.append(name)
        return added

    return store.update(change, f"add students: {code} {len(names)}")


def delete_students(code, names):
    """학생과 그 학생의 프로젝트·투표를 모두 지우기"""
    def change(data):
        s = get_session(data, code)
        removed = []
        for name in names:
            if s["students"].pop(name, None) is not None:
                removed.append(name)
            s["projects"].pop(name, None)
            s["votes"].pop(name, None)
        return removed

    return store.update(change, f"delete students: {code} {', '.join(names)}")


def set_stage(code, stage):
    def change(data):
        get_session(data, code)["stage"] = stage
        return stage

    return store.update(change, f"stage={stage}: {code}")


def save_project(code, student, project, force=False):
    """새로 등록하면 False, 고치면 True. force: 선생님은 단계와 상관없이 수정"""
    def change(data):
        s = get_session(data, code)
        if not force and s.get("stage") != "register":
            raise ValueError("지금은 프로젝트 등록 시간이 아니에요.")
        if student not in s["students"]:
            raise ValueError("명단에서 이름을 찾을 수 없어요. 다시 들어와 주세요.")
        old = s["projects"].get(student)
        s["projects"][student] = {**project, "created_at": (old or {}).get("created_at", now()), "updated_at": now()}
        return bool(old)

    return store.update(change, f"project: {code} {student}")


def delete_project(code, name, by_student=None):
    """프로젝트만 지우기. by_student: 학생이 자기 프로젝트를 지울 때 (등록 시간에만)"""
    def change(data):
        s = get_session(data, code)
        if by_student is not None:
            if name != by_student:
                raise ValueError("내 프로젝트만 지울 수 있어요.")
            if s.get("stage") != "register":
                raise ValueError("지금은 프로젝트를 지울 수 없어요. 선생님께 말씀해 주세요.")
        return s["projects"].pop(name, None) is not None

    return store.update(change, f"delete project by {by_student or 'teacher'}: {code} {name}")


def save_vote(code, voter, picks):
    def change(data):
        s = get_session(data, code)
        if s.get("stage") != "vote":
            raise ValueError("지금은 투표 시간이 아니에요.")
        if voter not in s["students"]:
            raise ValueError("명단에서 이름을 찾을 수 없어요. 다시 들어와 주세요.")
        for p in picks.values():
            if p["pick"] not in s["projects"] or p["pick"] == voter:
                raise ValueError("고른 프로젝트를 다시 확인해 주세요.")
        s["votes"][voter] = {"picks": picks, "voted_at": now()}
        return True

    return store.update(change, f"vote: {code} {voter}")


def delete_session(code):
    def change(data):
        return data["sessions"].pop(code, None) is not None

    return store.update(change, f"delete session: {code}")


def run(action, *args):
    """저장 실행 + 오류 메시지 표시. 성공하면 결과, 실패하면 None"""
    try:
        with st.spinner("저장하는 중이에요. 친구들이 한꺼번에 제출하면 몇 초 걸릴 수 있어요..."):
            return action(*args)
    except ValueError as e:
        st.error(str(e))
    except Exception as e:  # 네트워크 오류 등
        st.error(f"저장하지 못했어요. 잠시 후 다시 시도해 주세요. ({e})")
    return None


# ── 공통 화면 조각 ─────────────────────────────────────
def stage_steps(stage):
    """준비 → 등록 → 투표 → 결과 공개 (지금 단계 강조)"""
    now_i = STAGES.index(stage)
    chips = []
    for i, key in enumerate(STAGES):
        if i == now_i:
            style = "background:#ef5a50;color:#fff;font-weight:700"
        elif i < now_i:
            style = "background:#fde3e1;color:#b8423b"
        else:
            style = "background:#f1f2f5;color:#9aa0a6"
        chips.append(f"<span style='{style};padding:4px 12px;border-radius:14px;font-size:0.95rem'>"
                     f"{i + 1}. {STAGE_NAMES[key]}</span>")
    st.markdown("<div style='display:flex;gap:6px;align-items:center;flex-wrap:wrap'>"
                + "<span style='color:#c0c4cc'>›</span>".join(chips) + "</div>", unsafe_allow_html=True)


def qr_png(url, scale=6):
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=scale, border=2)
    return buf.getvalue()


def entry_url(code):
    return f"{app_url().rstrip('/')}/?code={quote(code)}"


def project_form(code, student, old, where="reg", force=False):
    """프로젝트 등록·수정 폼. where: 그려지는 곳, force: 선생님 수정"""
    new = not old
    k = f"{where}_{code}_{student}_{old.get('updated_at', '')}"  # 다른 곳에서 고치면 새 내용으로 다시 채워지게
    with st.form(f"form_{k}"):
        title = st.text_input("프로젝트 이름", value=old.get("title", ""), max_chars=40, key=f"title_{k}")
        reason = st.text_area("만든 이유", value=old.get("reason", ""), height=100,
                              placeholder="어떤 불편을 해결하고 싶었나요?", key=f"reason_{k}")
        features = st.text_area("기능 설명", value=old.get("features", ""), height=120,
                                placeholder="- 입력하면 ...\n- 버튼을 누르면 ...", key=f"features_{k}")
        url = st.text_input("배포한 URL", value=old.get("url", ""), placeholder="https://내앱.streamlit.app", key=f"url_{k}")
        submitted = st.form_submit_button("등록하기" if new else "수정하기", type="primary")

    if not submitted:
        return
    title, url = clean(title), url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    fields = [("프로젝트 이름", title), ("만든 이유", reason.strip()), ("기능 설명", features.strip()), ("배포한 URL", url)]
    missing = [n for n, v in fields if not v]
    if missing:
        st.error(f"{', '.join(missing)} 칸을 채워 주세요.")
    elif "." not in url or " " in url:
        st.error("배포한 URL을 다시 확인해 주세요.")
    else:
        project = {"title": title, "reason": reason.strip(), "features": features.strip(), "url": url}
        updated = run(save_project, code, student, project, force)
        if updated is not None:
            st.session_state.flash = "프로젝트를 수정했어요!" if updated else "프로젝트를 등록했어요!"
            st.rerun()


@st.dialog("✏️ 프로젝트 수정", width="large")
def edit_dialog(code, name):
    """선생님 전용: 단계와 상관없이 수정"""
    s = store.load()["sessions"].get(code)
    if not s or name not in s["projects"]:
        st.error("프로젝트를 찾을 수 없어요. 새로고침해 주세요.")
        return
    st.caption(f"👤 {name} · 고쳐서 제출하면 새 내용으로 바뀌어요.")
    project_form(code, name, s["projects"][name], where="dialog", force=True)


@st.dialog("🗑️ 프로젝트 삭제")
def delete_dialog(code, name):
    s = store.load()["sessions"].get(code)
    if not s or name not in s["projects"]:
        st.error("프로젝트를 찾을 수 없어요. 새로고침해 주세요.")
        return
    st.write(f"**{name}** 학생의 「{s['projects'][name]['title']}」 프로젝트를 지울까요?")
    st.warning("지우면 되돌릴 수 없어요. 이 프로젝트가 받은 표와 한마디도 결과에서 빠져요.")
    c1, c2 = st.columns(2)
    if c1.button("삭제할게요", type="primary", width="stretch"):
        if run(delete_project, code, name):
            st.session_state.flash = f"{name} 학생의 프로젝트를 지웠어요."
            st.rerun()
    if c2.button("취소", width="stretch"):
        st.rerun()


def project_cards(code, s, names, teacher=False, me=None):
    cols = st.columns(2)
    for i, name in enumerate(names):
        p = s["projects"][name]
        with cols[i % 2].container(border=True):
            st.markdown(f"#### {p['title']}" + (" · 내 프로젝트" if name == me else ""))
            st.caption(f"👤 {name}")
            st.write(p["reason"])
            with st.expander("기능 설명"):
                st.write(p["features"])
            if teacher:
                c1, c2, c3 = st.columns([2, 1, 1])
                c1.link_button("🚀 앱 열어 보기", p["url"], width="stretch")
                if c2.button("✏️ 수정", key=f"edit_{code}_{name}", width="stretch"):
                    edit_dialog(code, name)
                if c3.button("🗑️ 삭제", key=f"del_{code}_{name}", width="stretch"):
                    delete_dialog(code, name)
            else:
                st.link_button("🚀 앱 열어 보기", p["url"], width="stretch")


def results(code, s, me=None, teacher=False, private=False):
    """투표 결과. private: 표 수와 모든 학생의 한마디까지 (선생님 전용)"""
    projects = s["projects"]
    ballots = valid_ballots(s)
    done = [v for v, b in ballots.items() if len(b) == len(CATEGORIES)]
    students = s["students"]

    c1, c2 = st.columns([1, 2])
    c1.metric("투표 완료", f"{len(done)} / {len(students)}명")
    waiting = sorted(set(students) - set(done))
    if waiting:
        c2.caption("아직 투표하지 않은 친구")
        c2.write(", ".join(waiting))
    if not projects:
        st.info("아직 등록된 프로젝트가 없어요.")
        return

    per_cat = {cat: {n: 0 for n in projects} for cat in CATEGORIES}
    for ballot in ballots.values():
        for cat, p in ballot.items():
            per_cat[cat][p["pick"]] += 1
    total = {n: sum(per_cat[cat][n] for cat in CATEGORIES) for n in projects}

    st.markdown("### 종합 순위")
    st.caption("세 부문에서 받은 표를 모두 더한 순위예요.")
    if private:
        lines = ["| 순위 | 프로젝트 | 이름 | " + " | ".join(SHORT[c] for c in CATEGORIES) + " | 합계 |",
                 "|:--:|---|---|" + "--:|" * len(CATEGORIES) + "--:|"]
    else:
        lines = ["| 순위 | 프로젝트 | 이름 |", "|:--:|---|---|"]
    for rank, name, score in ranking(total):
        place = f"{MEDALS[rank]} {rank}위" if rank in MEDALS and score > 0 else f"{rank}위"
        cells = [place, md(projects[name]["title"]), md(name)]
        if private:
            cells += [str(per_cat[cat][name]) for cat in CATEGORIES] + [f"**{score}**"]
        lines.append("| " + " | ".join(cells) + " |")
    st.markdown("\n".join(lines))

    st.markdown("### 부문별 순위")
    cols = st.columns(len(CATEGORIES))
    max_votes = max(len(students) - 1, 1)
    for col, cat in zip(cols, CATEGORIES):
        with col.container(border=True):
            st.markdown(f"**{cat}**")
            ranked = ranking(per_cat[cat])
            top = ranked[0][2] if ranked else 0
            winners = [label(s, n) for r, n, v in ranked if r == 1 and v > 0]
            if winners:
                st.markdown(f"🥇 **{', '.join(winners)}**" + (f" · {top}표" if private else ""))
            else:
                st.caption("아직 표가 없어요.")
            if private:
                for rank, name, score in ranked:
                    st.progress(min(score / max_votes, 1.0), text=f"{rank}위 {label(s, name)} · {score}표")
            else:
                st.markdown("  \n".join(f"{rank}위 · {md(label(s, name))}" for rank, name, score in ranked))

    st.divider()
    st.markdown("### 💌 내가 받은 한마디")
    if teacher:
        if not private:
            st.caption("위의 '선생님만 볼 정보 보이기'를 켜면 학생별 한마디를 볼 수 있어요.")
            return
        me = st.selectbox("학생", sorted(projects), index=None, placeholder="이름을 골라 주세요", key=f"notes_{code}")
    elif me not in projects:
        st.caption("프로젝트를 등록한 친구만 한마디를 받을 수 있어요.")
        return
    if me:
        notes = [(cat, p["reason"]) for b in ballots.values() for cat, p in b.items()
                 if p["pick"] == me and p.get("reason")]
        if not notes:
            st.write("아직 받은 한마디가 없어요.")
        for cat, reason in sorted(notes, key=lambda x: list(CATEGORIES).index(x[0])):
            st.markdown(f"- **{cat}** · {reason}")


# ── 학생 화면 ─────────────────────────────────────────
def entry_page(sessions):
    """입장 코드(또는 QR 주소) → 명단에서 내 이름 고르기"""
    st.markdown("### 👤 들어가기")
    code = (st.query_params.get("code") or "").strip().upper()
    if code and (code_blocked() or code not in sessions):
        if code not in sessions:
            code_fail()
        st.session_state.code_error = "blocked" if code_blocked() else "notfound"
        del st.query_params["code"]
        st.rerun()

    if code:
        s = sessions[code]
        with st.container(border=True):
            st.markdown(f"#### {s['name']}")
            if not s["students"]:
                st.info("선생님이 아직 명단을 넣지 않았어요. 잠시 뒤 새로고침해 주세요.")
            else:
                with st.form("pick_name"):
                    name = st.selectbox("내 이름", sorted(s["students"]), index=None, placeholder="이름 고르기")
                    ok = st.form_submit_button("들어가기", type="primary")
                st.caption("내 이름이 없으면 선생님께 말씀해 주세요.")
                if ok:
                    if not name:
                        st.error("이름을 골라 주세요.")
                    else:
                        st.session_state.student_login = (code, name)
                        st.rerun()
        if st.button("다른 코드로 들어가기"):
            del st.query_params["code"]
            st.rerun()
        return

    if error := st.session_state.pop("code_error", None):
        st.error("잘못된 코드가 너무 많이 들어왔어요. 1분 뒤에 다시 해 보세요." if error == "blocked"
                 else "입장 코드를 찾을 수 없어요. 선생님 화면의 코드를 다시 확인해 주세요.")
    with st.container(border=True):
        st.caption("선생님 화면의 QR을 찍거나 입장 코드 4자리를 넣어 주세요.")
        with st.form("code_form", border=False):
            typed = st.text_input("입장 코드", max_chars=CODE_LEN, placeholder="예: K7Q2")
            ok = st.form_submit_button("다음", type="primary")
    if ok:
        typed = nospace(typed).upper()
        if code_blocked():
            st.error("잘못된 코드가 너무 많이 들어왔어요. 1분 뒤에 다시 해 보세요.")
        elif typed in sessions:
            st.query_params["code"] = typed
            st.rerun()
        else:
            code_fail()
            st.error("입장 코드를 찾을 수 없어요. 선생님 화면의 코드를 다시 확인해 주세요.")


def current_login(sessions):
    """로그인한 (코드, 이름). 발표회가 지워졌거나 명단에서 빠졌으면 로그아웃"""
    login = st.session_state.get("student_login")
    if not login:
        return None
    code, name = login
    s = sessions.get(code)
    if not s or name not in s["students"]:
        st.session_state.student_login = None
        return None
    return code, name


@st.fragment(run_every="5s")
def stage_watch(code, stage):
    """선생님이 단계를 넘기면 학생 화면이 저절로 바뀌게"""
    try:
        current = store.load()["sessions"].get(code, {}).get("stage")
    except Exception:
        return
    if current != stage:
        st.rerun()


def student_view(code, s, me):
    box = st.sidebar.container(border=True)
    box.success(f"👤 **{me}**")
    box.caption(s["name"])
    if box.button("나가기", width="stretch", help="다른 학생이 이 기기를 쓰려면 눌러 주세요"):
        st.session_state.student_login = None
        st.rerun()

    stage = s.get("stage", "ready")
    st.markdown(f"#### {s['name']}")
    stage_steps(stage)
    st.write("")
    stage_watch(code, stage)

    if stage == "ready":
        st.info("곧 시작해요! 선생님이 시작하면 이 화면이 저절로 프로젝트 등록 화면으로 바뀌어요.")
    elif stage == "register":
        st.subheader("📝 내 프로젝트 등록")
        old = s["projects"].get(me, {})
        st.caption("고쳐서 다시 제출하면 새 내용으로 바뀌어요." if old else "내 프로젝트 정보를 적고 등록해 주세요.")
        project_form(code, me, old)
        if old:
            with st.expander("🗑️ 내 프로젝트 삭제"):
                st.caption("지우면 되돌릴 수 없어요. 지운 뒤 다시 등록할 수 있어요.")
                sure = st.checkbox("내 프로젝트를 지울게요", key=f"sure_own_{code}_{old.get('updated_at', '')}")
                if st.button("내 프로젝트 삭제", disabled=not sure):
                    if run(delete_project, code, me, me):
                        st.session_state.flash = "내 프로젝트를 지웠어요."
                        st.rerun()
        st.caption(f"우리 반 등록 {len(s['projects'])} / {len(s['students'])}명")
    elif stage == "vote":
        vote_section(code, s, me)
    else:
        st.subheader("🏆 투표 결과")
        results(code, s, me=me)


def vote_section(code, s, voter):
    st.subheader("🗳️ 친구 프로젝트 투표")
    friends = [n for n in sorted(s["projects"]) if n != voter]
    if not friends:
        st.info("투표할 친구 프로젝트가 아직 없어요.")
        return
    st.markdown("##### 1. 친구 프로젝트 둘러보기")
    project_cards(code, s, friends)

    st.markdown("##### 2. 부문마다 한 명씩 고르기")
    st.caption("내 프로젝트는 목록에 나오지 않아요. 다시 제출하면 앞의 투표가 새 투표로 바뀌어요.")
    prev = s["votes"].get(voter, {}).get("picks", {})
    if prev:
        st.info("이미 투표했어요. 다시 제출하면 앞의 투표가 새 투표로 바뀌어요.")
    picks = {}
    with st.form(f"vote_form_{code}_{voter}"):
        for cat, desc in CATEGORIES.items():
            st.markdown(f"**{cat}**")
            st.caption(desc)
            before = prev.get(cat, {})
            index = friends.index(before["pick"]) if before.get("pick") in friends else None
            pick = st.radio(cat, friends, index=index, format_func=lambda n: label(s, n),
                            label_visibility="collapsed", key=f"pick_{code}_{voter}_{cat}")
            reason = st.text_input("고른 이유 한 줄 (친구에게 그대로 전달돼요)", value=before.get("reason", ""),
                                   max_chars=100, key=f"reason_{code}_{voter}_{cat}")
            picks[cat] = {"pick": pick, "reason": clean(reason)}
            st.divider()
        submitted = st.form_submit_button("투표 제출", type="primary")
    if submitted:
        if any(p["pick"] is None for p in picks.values()):
            st.error("세 부문 모두 하나씩 골라 주세요.")
        elif any(not p["reason"] for p in picks.values()):
            st.error("부문마다 고른 이유를 한 줄씩 적어 주세요.")
        elif run(save_vote, code, voter, picks):
            st.session_state.flash = "투표가 제출되었어요!"
            st.session_state.balloons = True
            st.rerun()


# ── 선생님 화면 ───────────────────────────────────────
def teacher_login():
    with st.sidebar.expander("🔐 선생님 메뉴", expanded=False):
        password = admin_password()
        if password is None:
            st.warning("ADMIN_PASSWORD 가 설정되지 않아 선생님 메뉴를 쓸 수 없어요. Streamlit secrets 를 확인해 주세요.")
            st.session_state.is_admin = False
        elif st.session_state.get("is_admin"):
            st.success("선생님 모드")
            if st.button("나가기 (학생 화면 보기)", width="stretch"):
                st.session_state.is_admin = False
                st.rerun()
        else:
            with st.form("login", border=False):
                pw = st.text_input("비밀번호", type="password")
                if st.form_submit_button("들어가기", width="stretch"):
                    if wait := admin_locked():
                        st.error(f"비밀번호를 여러 번 틀렸어요. {wait}초 뒤에 다시 해 보세요.")
                    elif hmac.compare_digest(pw.encode(), password.encode()):
                        get_attempts().pop("admin", None)
                        st.session_state.is_admin = True
                        st.rerun()
                    else:
                        admin_fail()
                        st.error("비밀번호가 달라요.")
    return st.session_state.get("is_admin", False)


NEW = "__new__"


def pick_session(sessions):
    """사이드바에서 발표회 고르기. NEW 면 새로 만들기"""
    order = sorted(sessions, key=lambda c: sessions[c].get("created_at", ""), reverse=True)
    options = order + [NEW]
    if pending := st.session_state.pop("pending_select", None):
        st.session_state.sel_session = pending
    if st.session_state.get("sel_session") not in options:
        url_code = (st.query_params.get("code") or "").upper()
        st.session_state.sel_session = url_code if url_code in sessions else (order[0] if order else NEW)
    st.sidebar.markdown("#### 📚 발표회")
    code = st.sidebar.selectbox(
        "발표회", options, key="sel_session", label_visibility="collapsed",
        format_func=lambda c: "➕ 새 발표회 만들기" if c == NEW else sessions[c]["name"])
    if code != NEW and st.query_params.get("code") != code:
        st.query_params["code"] = code  # 선생님 메뉴에서 나가면 바로 이 발표회 학생 화면이 보이게
    return code


def create_form():
    st.subheader("➕ 새 발표회 만들기")
    st.caption("반마다 하나씩 만들어요. 만들면 입장 코드가 자동으로 생겨요.")
    with st.form("create_session"):
        name = st.text_input("발표회 이름", max_chars=40, placeholder="예: 26가을 AI파이썬랩 심화 토 11:00")
        roster = st.text_area("학생 명단 (한 줄에 한 명)", height=180, placeholder="김피클\n이코딩\n박파이")
        ok = st.form_submit_button("만들기", type="primary")
    if ok:
        name, names = clean(name), parse_names(roster)
        if not name:
            st.error("발표회 이름을 적어 주세요.")
        elif not names:
            st.error("학생 이름을 한 명 이상 넣어 주세요.")
        elif code := run(create_session, name, names):
            st.session_state.pending_select = code
            st.session_state.flash = f"'{name}' 발표회를 만들었어요. 입장 코드는 {code} 예요."
            st.rerun()


@st.dialog("📺 입장 안내", width="large")
def entry_dialog(code, name):
    url = entry_url(code)
    st.markdown(f"<div style='text-align:center'><div style='font-size:1.5rem;font-weight:600'>{html.escape(name)}</div>"
                f"<div style='font-size:1.1rem;color:#6b7280;margin-top:8px'>입장 코드</div>"
                f"<div style='font-size:6rem;font-weight:800;letter-spacing:0.3em;line-height:1.1'>{code}</div></div>",
                unsafe_allow_html=True)
    c1, c2, c3 = st.columns([3, 2, 3])
    c2.image(qr_png(url, scale=10), width="stretch")
    st.markdown(f"<div style='text-align:center;color:#6b7280'>QR을 찍거나 {html.escape(app_url())} 에서 코드를 넣어요</div>",
                unsafe_allow_html=True)


@st.fragment(run_every="5s")
def status_table(code):
    """학생별 등록·투표 현황 (5초마다 저절로 새로고침)"""
    try:
        s = store.load()["sessions"].get(code)
    except Exception:
        return
    if not s:
        return
    students = s["students"]
    if not students:
        st.info("명단이 비어 있어요. 관리 탭에서 학생 이름을 넣어 주세요.")
        return
    voted = {v for v, b in valid_ballots(s).items() if len(b) == len(CATEGORIES)}
    rows = ["| 이름 | 프로젝트 | 투표 |", "|---|:--:|:--:|"]
    for name in sorted(students):
        rows.append(f"| {md(name)} | {'✅' if name in s['projects'] else '-'} | {'✅' if name in voted else '-'} |")
    st.markdown("\n".join(rows))
    st.caption(f"{len(students)}명 · 프로젝트 {len(s['projects'])}개 · 투표 완료 {len(voted)}명 · 5초마다 저절로 새로고침돼요.")


def tab_progress(code, s):
    stage = s.get("stage", "ready")
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("##### 진행 단계")
        stage_steps(stage)
        st.caption(f"지금: {STAGE_HELP[stage]}")
        i = STAGES.index(stage)
        if i == len(STAGES) - 1:
            st.success("모든 단계가 끝났어요. 학생 화면에 결과가 보이고 있어요.")
        b1, b2 = st.columns([3, 2])
        if i < len(STAGES) - 1 and b1.button(f"▶ {NEXT_LABEL[stage]}", type="primary", width="stretch"):
            if run(set_stage, code, STAGES[i + 1]):
                st.session_state.flash = f"'{STAGE_NAMES[STAGES[i + 1]]}' 단계로 넘어갔어요."
                st.rerun()
        if i > 0 and b2.button(f"◀ 이전 단계로 ({STAGE_NAMES[STAGES[i - 1]]})", width="stretch"):
            if run(set_stage, code, STAGES[i - 1]):
                st.session_state.flash = f"'{STAGE_NAMES[STAGES[i - 1]]}' 단계로 되돌렸어요."
                st.rerun()
    with right:
        with st.container(border=True):
            st.markdown("##### 학생 입장")
            c1, c2 = st.columns([1, 1])
            c1.caption("입장 코드")
            c1.markdown(f"<div style='font-size:2.6rem;font-weight:800;letter-spacing:0.2em'>{code}</div>",
                        unsafe_allow_html=True)
            c2.image(qr_png(entry_url(code)), width=130)
            if st.button("📺 크게 띄우기", width="stretch"):
                entry_dialog(code, s["name"])
            st.caption("학생 주소 (복사해서 보내도 돼요)")
            st.code(entry_url(code), language=None)

    st.divider()
    st.markdown("##### 학생 현황")
    status_table(code)

    st.divider()
    st.markdown(f"##### 프로젝트 ({len(s['projects'])}개)")
    st.caption("발표할 때 '앱 열어 보기'로 띄우세요. 선생님은 언제든 고치거나 지울 수 있어요.")
    if s["projects"]:
        project_cards(code, s, sorted(s["projects"]), teacher=True)
    else:
        st.caption("아직 등록된 프로젝트가 없어요.")


def tab_results(code, s):
    if s.get("stage") != "result":
        st.warning("👀 미리보기예요. 학생에게는 '결과 공개' 단계가 되어야 보여요.")
    private = st.toggle("🔐 선생님만 볼 정보 보이기 (표 수 · 모든 학생의 한마디)", value=False, key=f"private_{code}",
                        help="교실 화면에 띄울 때는 꺼 두세요. 꺼져 있으면 학생 화면과 똑같이 순위만 보여요.")
    results(code, s, teacher=True, private=private)


def tab_manage(code, s, data):
    with st.expander("👥 학생 명단", expanded=True):
        names = sorted(s["students"])
        st.write(", ".join(names) if names else "아직 명단이 없어요.")
        with st.form(f"roster_{code}", clear_on_submit=True):
            text = st.text_area("이름 추가 (한 줄에 한 명)", height=100, placeholder="김피클\n이코딩")
            if st.form_submit_button("명단에 추가"):
                new = parse_names(text)
                if not new:
                    st.error("추가할 이름을 적어 주세요.")
                elif (added := run(add_students, code, new)) is not None:
                    skipped = len(new) - len(added)
                    st.session_state.flash = f"{len(added)}명을 넣었어요." + (f" (이미 있는 이름 {skipped}명은 건너뜀)" if skipped else "")
                    st.rerun()
        if names:
            n = st.session_state.get("del_nonce", 0)
            picked = st.multiselect("학생 삭제 (그 학생의 프로젝트와 투표도 함께 지워져요)", names,
                                    placeholder="지울 학생 고르기", key=f"del_pick_{code}_{n}")
            sure = st.checkbox("고른 학생을 지울게요", key=f"del_sure_{code}_{n}")
            if st.button(f"학생 {len(picked)}명 삭제" if picked else "학생 삭제", disabled=not (picked and sure)):
                if (removed := run(delete_students, code, picked)) is not None:
                    st.session_state.del_nonce = n + 1
                    st.session_state.flash = f"학생 {len(removed)}명을 지웠어요."
                    st.rerun()

    with st.expander("💾 백업"):
        st.caption("모든 발표회의 명단·프로젝트·투표를 JSON 파일 하나로 내려받아요. 학생 이름이 들어 있으니 선생님 컴퓨터에만 보관하세요.")
        st.download_button("전체 데이터 내려받기 (JSON)",
                           json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                           file_name=f"vote_data_{datetime.now(KST):%Y%m%d_%H%M}.json", mime="application/json")

    with st.expander("🗑️ 발표회 삭제"):
        st.caption(f"'{s['name']}' 발표회의 명단·프로젝트·투표가 모두 지워져요. 되돌릴 수 없어요.")
        sure = st.checkbox("이 발표회를 지울게요", key=f"sure_session_{code}")
        if st.button("발표회 삭제", disabled=not sure, type="primary"):
            if run(delete_session, code):
                st.session_state.pop("sel_session", None)
                st.query_params.pop("code", None)
                st.session_state.flash = f"'{s['name']}' 발표회를 지웠어요."
                st.rerun()

    if d := expire_date(s):
        st.caption(f"🗓️ 발표회는 만든 지 {KEEP_DAYS}일이 지나면 자동으로 지워져요. 이 발표회는 {d:%Y-%m-%d} 이후에 지워져요.")


def teacher_view(code, s, data):
    st.markdown(f"#### {s['name']}")
    st.caption(f"입장 코드 {code} · 만든 날 {s.get('created_at', '')[:10]}")
    tabs = st.tabs(["▶️ 진행", "🏆 결과", "⚙️ 관리"], key="teacher_tabs")
    with tabs[0]:
        tab_progress(code, s)
    with tabs[1]:
        tab_results(code, s)
    with tabs[2]:
        tab_manage(code, s, data)


# ── 메인 ─────────────────────────────────────────────
def main():
    st.title("🏆 피클 프로젝트 투표")
    is_admin = teacher_login()

    try:
        data = load_data()
    except Exception as e:
        st.error(f"데이터를 불러오지 못했어요. 새로고침해 주세요. ({e})")
        st.stop()

    if msg := st.session_state.pop("flash", None):
        st.toast(msg, icon="✅")
    if st.session_state.pop("balloons", False):
        st.balloons()

    sessions = data["sessions"]
    if is_admin:
        purge_old(sessions)
        code = pick_session(sessions)
        if code == NEW:
            create_form()
        else:
            teacher_view(code, sessions[code], data)
    else:
        login = current_login(sessions)
        if login:
            student_view(login[0], sessions[login[0]], login[1])
        else:
            entry_page(sessions)

    st.sidebar.divider()
    if st.sidebar.button("🔄 새로고침", width="stretch"):
        st.rerun()


main()
