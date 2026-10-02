import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

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
SEASONS = {"봄": 1, "여름": 2, "가을": 3, "겨울": 4}
SEMESTER_RE = re.compile(r"^(\d{2})(봄|여름|가을|겨울)$")
TIMESLOT_RE = re.compile(r"^[월화수목금토일]/\d{4}$")
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
PIN_RE = re.compile(r"^\d{4}$")
KST = ZoneInfo("Asia/Seoul")


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


# ── PIN · 로그인 시도 제한 ─────────────────────────────
def hash_pin(pin, salt=None):
    """PIN 은 그대로 저장하지 않고 해시로 저장"""
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 100_000).hex()
    return {"salt": salt, "hash": digest}


def check_pin(record, pin):
    if not record or not PIN_RE.match(pin or ""):
        return False
    return hmac.compare_digest(hash_pin(pin, record["salt"])["hash"], record["hash"])


@st.cache_resource
def get_attempts():
    """틀린 횟수 기록 (모든 접속자가 공유하는 서버 메모리)"""
    return {}


def locked_seconds(who):
    info = get_attempts().get(who)
    return max(0, int(info["until"] - time.time())) if info else 0


def record_fail(who, max_tries=5, lock_seconds=300):
    info = get_attempts().setdefault(who, {"count": 0, "until": 0})
    info["count"] += 1
    if info["count"] >= max_tries:
        info.update(count=0, until=time.time() + lock_seconds)


def record_ok(who):
    get_attempts().pop(who, None)


def clean(text):
    return " ".join((text or "").split())


def class_key(semester, course, timeslot):
    return f"{semester}|{course}|{timeslot}"


# ── 정렬 ──────────────────────────────────────────────
def semester_sort_key(name):
    """'26가을' → (26, 3). 형식이 다르면 맨 뒤로"""
    m = SEMESTER_RE.match(name)
    return (int(m.group(1)), SEASONS[m.group(2)]) if m else (-1, 0)


def sorted_semesters(classes):
    return sorted({c["semester"] for c in classes.values()}, key=semester_sort_key, reverse=True)


def sorted_courses(classes, semester):
    return sorted({c["course"] for c in classes.values() if c["semester"] == semester})


def sorted_timeslots(classes, semester, course):
    return sorted(
        {c["timeslot"] for c in classes.values() if c["semester"] == semester and c["course"] == course}
    )


# ── 데이터 형식 ──────────────────────────────────────
def normalize(data):
    """학생 명단(students)을 채워 넣기. 예전 형식(프로젝트 안의 PIN)은 학생 명단으로 옮긴다"""
    for cls in data["classes"].values():
        students = cls.setdefault("students", {})
        for name, p in cls.get("projects", {}).items():
            if "pin" in p:
                pin = p.pop("pin")
                students.setdefault(name, {"pin": pin, "created_at": p.get("created_at", now())})
    return data


# ── 집계 ──────────────────────────────────────────────
def valid_ballots(cls):
    """명단에 있는 학생이 낸 투표만, 등록된 프로젝트를 고른 것만"""
    projects = cls["projects"]
    ballots = {}
    for voter, vote in cls["votes"].items():
        if voter not in cls["students"]:
            continue
        ballots[voter] = {
            cat: pick for cat, pick in vote.get("picks", {}).items()
            if cat in CATEGORIES and pick.get("pick") in projects and pick["pick"] != voter
        }
    return ballots


def ranking(scores):
    """{이름: 점수} → [(순위, 이름, 점수)] 공동 순위 처리 (1, 1, 3 ...)"""
    items = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    result = []
    for name, score in items:
        rank = 1 + sum(1 for s in scores.values() if s > score)
        result.append((rank, name, score))
    return result


def md(text):
    """마크다운 표 안에서 깨지지 않게"""
    return text.replace("|", "\\|").replace("*", "\\*").replace("_", "\\_")


def label(cls, name):
    return f"{cls['projects'][name]['title']} · {name}"


# ── 데이터 변경 함수 (store.update 에 넘김) ─────────────
def sign_up(key, name, pin):
    """처음 온 학생: 이름 + PIN 정하기"""
    pin_record = hash_pin(pin)

    def change(data):
        cls = data["classes"].get(key)
        if cls is None:
            raise ValueError("반을 찾을 수 없어요.")
        if name in cls["students"]:
            raise ValueError("이미 있는 이름이에요. '들어가기'에서 PIN을 입력해 주세요. "
                             "같은 이름의 친구가 있다면 '김피클2'처럼 구분해서 정해 주세요.")
        cls["students"][name] = {"pin": pin_record, "created_at": now()}
        return True

    return store.update(lambda d: change(normalize(d)), f"sign up: {key} {name}")


def save_project(key, student, project):
    """로그인한 학생의 프로젝트 등록 또는 수정. 새로 등록하면 False, 고치면 True"""
    def change(data):
        cls = data["classes"].get(key)
        if cls is None:
            raise ValueError("반을 찾을 수 없어요.")
        if not cls.get("registration_open", True):
            raise ValueError("지금은 프로젝트 등록이 닫혀 있어요.")
        if student not in cls["students"]:
            raise ValueError("학생 정보를 찾을 수 없어요. 다시 들어와 주세요.")
        old = cls["projects"].get(student)
        cls["projects"][student] = {
            **project,
            "created_at": (old or {}).get("created_at", now()),
            "updated_at": now(),
        }
        return bool(old)

    return store.update(lambda d: change(normalize(d)), f"project: {key} {student}")


def set_pin(key, student, pin):
    pin_record = hash_pin(pin)

    def change(data):
        cls = data["classes"][key]
        if student not in cls["students"]:
            raise ValueError("학생을 찾을 수 없어요.")
        cls["students"][student]["pin"] = pin_record
        return True

    return store.update(lambda d: change(normalize(d)), f"reset pin: {key} {student}")


def save_vote(key, voter, picks):
    def change(data):
        cls = data["classes"].get(key)
        if cls is None:
            raise ValueError("반을 찾을 수 없어요.")
        if not cls.get("voting_open", True):
            raise ValueError("지금은 투표가 닫혀 있어요.")
        if voter not in cls["students"]:
            raise ValueError("학생 정보를 찾을 수 없어요. 다시 들어와 주세요.")
        for p in picks.values():
            if p["pick"] not in cls["projects"] or p["pick"] == voter:
                raise ValueError("고른 프로젝트를 다시 확인해 주세요.")
        cls["votes"][voter] = {"picks": picks, "voted_at": now()}
        return True

    return store.update(lambda d: change(normalize(d)), f"vote: {key} {voter}")


def set_flag(key, flag, value):
    def change(data):
        data["classes"][key][flag] = value

    store.update(lambda d: change(normalize(d)), f"{flag}={value}: {key}")


def create_class(semester, course, timeslot):
    key = class_key(semester, course, timeslot)

    def change(data):
        if key in data["classes"]:
            raise ValueError("이미 있는 반이에요.")
        data["classes"][key] = {
            "semester": semester, "course": course, "timeslot": timeslot,
            "registration_open": True, "voting_open": True, "revealed": False,
            "created_at": now(), "students": {}, "projects": {}, "votes": {},
        }

    store.update(lambda d: change(normalize(d)), f"create class: {key}")
    return key


def delete_project(key, student):
    """프로젝트만 지우기 (학생과 그 학생의 투표는 남김)"""
    def change(data):
        data["classes"][key]["projects"].pop(student, None)
        return True

    return store.update(lambda d: change(normalize(d)), f"delete project: {key} {student}")


def delete_student(key, student):
    """학생과 그 학생의 프로젝트·투표를 모두 지우기"""
    def change(data):
        cls = data["classes"][key]
        for part in ("students", "projects", "votes"):
            cls[part].pop(student, None)
        return True

    return store.update(lambda d: change(normalize(d)), f"delete student: {key} {student}")


def reset_votes(key):
    def change(data):
        data["classes"][key]["votes"] = {}
        data["classes"][key]["revealed"] = False

    store.update(lambda d: change(normalize(d)), f"reset votes: {key}")


def delete_class(key):
    def change(data):
        data["classes"].pop(key, None)

    store.update(lambda d: change(normalize(d)), f"delete class: {key}")


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


# ── 반 고르기 (사이드바) ─────────────────────────────────
def pick_class(classes):
    """사이드바에서 학기 → 수업 → 시간대를 골라 반 key 반환. 주소의 ?semester=&course=&time= 도 반영"""
    params = st.query_params
    if "picked_from_url" not in st.session_state:
        st.session_state.picked_from_url = True
        for widget, param in (("sel_semester", "semester"), ("sel_course", "course"), ("sel_time", "time")):
            if param in params:
                st.session_state[widget] = params[param]
    if pending := st.session_state.pop("pending_select", None):
        st.session_state.sel_semester, st.session_state.sel_course, st.session_state.sel_time = pending

    def choose(label_text, options, widget):
        if st.session_state.get(widget) not in options:
            st.session_state[widget] = options[0]
        return st.sidebar.selectbox(label_text, options, key=widget)

    st.sidebar.header("📚 반 선택")
    semesters = sorted_semesters(classes)
    if not semesters:
        return None
    semester = choose("학기", semesters, "sel_semester")
    course = choose("수업", sorted_courses(classes, semester), "sel_course")
    timeslot = choose("시간대", sorted_timeslots(classes, semester, course), "sel_time")

    wanted = {"semester": semester, "course": course, "time": timeslot}
    if {k: params.get(k) for k in wanted} != wanted:
        st.query_params.update(wanted)
    return class_key(semester, course, timeslot)


def teacher_login():
    with st.sidebar.expander("🔐 선생님 메뉴", expanded=False):
        password = admin_password()
        if password is None:
            st.warning("ADMIN_PASSWORD 가 설정되지 않아 선생님 메뉴를 쓸 수 없어요. Streamlit secrets 를 확인해 주세요.")
            st.session_state.is_admin = False
        elif st.session_state.get("is_admin"):
            st.success("선생님 모드")
            if st.button("나가기", width="stretch"):
                st.session_state.is_admin = False
                st.rerun()
        else:
            with st.form("login", border=False):
                pw = st.text_input("비밀번호", type="password")
                if st.form_submit_button("들어가기", width="stretch"):
                    who = ("admin",)
                    if wait := locked_seconds(who):
                        st.error(f"비밀번호를 여러 번 틀렸어요. {wait}초 뒤에 다시 해 보세요.")
                    elif hmac.compare_digest(pw.encode(), password.encode()):
                        record_ok(who)
                        st.session_state.is_admin = True
                        st.rerun()
                    else:
                        record_fail(who, max_tries=10, lock_seconds=60)
                        st.error("비밀번호가 달라요.")
    return st.session_state.get("is_admin", False)


# ── 학생 로그인 (이름 + PIN) ───────────────────────────
def current_student(key, cls):
    """이 반에 이름 + PIN 으로 들어온 학생 이름 (없으면 None)"""
    name = st.session_state.setdefault("me", {}).get(key)
    if name and name not in cls["students"]:  # 선생님이 학생을 지운 경우
        st.session_state.me.pop(key, None)
        name = None
    return name


def verify_pin(key, cls, name, pin):
    """PIN 이 맞으면 None, 아니면 보여줄 오류 메시지 (틀린 횟수·잠금 포함)"""
    who = ("student", key, name)
    record = cls["students"].get(name, {}).get("pin")
    if wait := locked_seconds(who):
        return f"PIN을 여러 번 틀렸어요. {wait // 60 + 1}분 뒤에 다시 해 보거나 선생님께 말씀해 주세요."
    if not record:
        return "아직 PIN이 없어요. 선생님께 PIN을 정해 달라고 말씀해 주세요."
    if check_pin(record, (pin or "").strip()):
        record_ok(who)
        return None
    record_fail(who)
    return "PIN이 맞지 않아요. 잊어버렸으면 선생님께 말씀해 주세요."


def student_panel(key, cls):
    """반 화면 맨 위의 학생 로그인. 들어오면 모든 탭에서 그 학생으로 쓰임"""
    me = current_student(key, cls)
    if me:
        c1, c2 = st.columns([5, 1], vertical_alignment="center")
        c1.success(f"👤 **{me}** 이름으로 들어와 있어요.")
        if c2.button("나가기", key=f"logout_{key}", width="stretch"):
            st.session_state.me.pop(key, None)
            st.session_state.edit_ok = {k for k in st.session_state.get("edit_ok", set()) if k[0] != key}
            st.rerun()
        return me

    with st.container(border=True):
        st.markdown("**👤 학생 로그인**")
        st.caption("이름과 PIN으로 들어오면 프로젝트를 등록하고 투표할 수 있어요.")
        modes = ["들어가기", "처음이에요 (이름·PIN 정하기)"]
        mode = st.radio("로그인 방법", modes, index=0 if cls["students"] else 1, horizontal=True,
                        label_visibility="collapsed", key=f"login_mode_{key}")
        if mode == modes[0]:
            if not cls["students"]:
                st.info("아직 들어온 학생이 없어요. '처음이에요'를 눌러 이름과 PIN을 정해 주세요.")
                return None
            with st.form(f"login_{key}"):
                c1, c2 = st.columns(2)
                name = c1.selectbox("내 이름", sorted(cls["students"]), index=None,
                                    placeholder="이름을 골라 주세요", key=f"login_name_{key}")
                pin = c2.text_input("내 PIN (숫자 4자리)", type="password", max_chars=4, key=f"login_pin_{key}")
                submitted = st.form_submit_button("들어가기", type="primary")
            if submitted:
                if not name:
                    st.error("이름을 골라 주세요.")
                elif error := verify_pin(key, cls, name, pin):
                    st.error(error)
                else:
                    st.session_state.me[key] = name
                    st.rerun()
        else:
            with st.form(f"signup_{key}"):
                name = st.text_input("내 이름", max_chars=20, placeholder="예: 김피클", key=f"signup_name_{key}")
                c1, c2 = st.columns(2)
                pin = c1.text_input("PIN 정하기 (숫자 4자리)", type="password", max_chars=4, key=f"signup_pin_{key}",
                                    help="다음에 들어올 때 써요. 친구에게 알려 주지 마세요.")
                pin2 = c2.text_input("PIN 한 번 더", type="password", max_chars=4, key=f"signup_pin2_{key}")
                submitted = st.form_submit_button("시작하기", type="primary")
            if submitted:
                name, pin, pin2 = clean(name), pin.strip(), pin2.strip()
                if not name:
                    st.error("이름을 적어 주세요.")
                elif not PIN_RE.match(pin):
                    st.error("PIN은 숫자 4자리로 정해 주세요.")
                elif pin != pin2:
                    st.error("두 PIN이 서로 달라요. 다시 입력해 주세요.")
                elif run(sign_up, key, name, pin):
                    st.session_state.me[key] = name
                    st.session_state.flash = f"{name} 이름으로 들어왔어요! 정한 PIN을 꼭 기억해 두세요."
                    st.rerun()
    return None


def need_login():
    st.info("위의 **👤 학생 로그인**에서 이름과 PIN으로 먼저 들어와 주세요.")


# ── 탭: 프로젝트 등록 ─────────────────────────────────
def project_form(key, student, old, where="reg"):
    """로그인한 student 의 프로젝트 등록·수정 폼. where: 그려지는 곳 (등록 탭 / 수정 창)"""
    new = not old
    # 입력 칸 key 접두어. 수정 시각을 넣어서 다른 곳에서 고치면 새 내용으로 다시 채워지게
    k = f"{where}_{key}_{student}_{old.get('updated_at', '')}"
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
        updated = run(save_project, key, student, project)
        if updated is not None:
            st.session_state.flash = "프로젝트를 수정했어요!" if updated else "프로젝트를 등록했어요!"
            st.rerun()


def tab_register(key, cls):
    st.subheader("📝 내 프로젝트 등록하기")
    me = current_student(key, cls)
    if not me:
        need_login()
        return
    if not cls.get("registration_open", True):
        st.info("지금은 프로젝트 등록이 닫혀 있어요. 선생님께 말씀해 주세요.")
        return
    old = cls["projects"].get(me, {})
    st.caption("고쳐서 다시 제출하면 새 내용으로 바뀌어요." if old else "내 프로젝트 정보를 적고 등록해 주세요.")
    project_form(key, me, old)


# ── 탭: 프로젝트 둘러보기 ─────────────────────────────
@st.dialog("✏️ 프로젝트 수정", width="large")
def edit_dialog(key, name):
    """프로젝트 보기의 수정 버튼 → 그 학생의 PIN 을 알면 바로 수정"""
    cls = normalize(store.load())["classes"].get(key)
    if not cls or name not in cls["projects"] or name not in cls["students"]:
        st.error("프로젝트를 찾을 수 없어요. 새로고침해 주세요.")
        return
    if not cls.get("registration_open", True):
        st.info("지금은 프로젝트 등록이 닫혀 있어서 고칠 수 없어요. 선생님께 말씀해 주세요.")
        return
    unlocked = st.session_state.setdefault("edit_ok", set())  # 이 창에서 PIN 을 확인한 카드
    if current_student(key, cls) != name and (key, name) not in unlocked:
        st.write(f"**{name}** 학생의 PIN을 입력하면 「{cls['projects'][name]['title']}」을(를) 고칠 수 있어요.")
        with st.form(f"edit_pin_{key}_{name}"):
            pin = st.text_input("PIN (숫자 4자리)", type="password", max_chars=4)
            submitted = st.form_submit_button("확인", type="primary")
        if submitted:
            if error := verify_pin(key, cls, name, pin):
                st.error(error)
            else:
                unlocked.add((key, name))  # 로그인은 그대로 두고 이 카드만 수정 가능하게
                st.rerun(scope="fragment")  # 창은 열어 둔 채 수정 칸 보여주기
        return
    st.caption(f"👤 {name} · 고쳐서 제출하면 새 내용으로 바뀌어요.")
    project_form(key, name, cls["projects"][name], where="dialog")


def tab_projects(key, cls):
    projects = cls["projects"]
    st.subheader(f"🔍 이 반의 프로젝트 ({len(projects)}개)")
    if not projects:
        st.info("아직 등록된 프로젝트가 없어요.")
        return
    cols = st.columns(2)
    for i, name in enumerate(sorted(projects)):
        p = projects[name]
        with cols[i % 2].container(border=True):
            st.markdown(f"#### {p['title']}")
            st.caption(f"👤 {name} · 📅 {p.get('updated_at', '')}")
            st.markdown("**만든 이유**")
            st.write(p["reason"])
            st.markdown("**기능 설명**")
            st.write(p["features"])
            c1, c2 = st.columns([3, 1])
            c1.link_button("🚀 앱 열어 보기", p["url"], width="stretch")
            if c2.button("✏️ 수정", key=f"edit_{key}_{name}", width="stretch",
                         disabled=not cls.get("registration_open", True),
                         help="PIN을 알면 고칠 수 있어요" if cls.get("registration_open", True) else "지금은 등록이 닫혀 있어요"):
                edit_dialog(key, name)


# ── 탭: 투표하기 ──────────────────────────────────────
def tab_vote(key, cls):
    st.subheader("🗳️ 베스트 프로젝트 투표")
    projects = cls["projects"]
    if not cls.get("voting_open", True):
        st.info("지금은 투표가 닫혀 있어요.")
        return
    voter = current_student(key, cls)
    if not voter:
        need_login()
        return
    if not [n for n in projects if n != voter]:
        st.info("투표할 친구 프로젝트가 아직 없어요.")
        return
    st.caption("부문마다 친구 프로젝트 하나씩 골라요. 내 프로젝트는 목록에 나오지 않고, 다시 제출하면 앞의 투표가 새 투표로 바뀌어요.")

    prev = cls["votes"].get(voter, {}).get("picks", {})
    if prev:
        st.info("이미 투표했어요. 다시 제출하면 앞의 투표가 새 투표로 바뀌어요.")

    friends = [n for n in sorted(projects) if n != voter]
    picks = {}
    with st.form(f"vote_form_{key}_{voter}"):
        for cat, desc in CATEGORIES.items():
            st.markdown(f"##### {cat}")
            st.caption(desc)
            before = prev.get(cat, {})
            index = friends.index(before["pick"]) if before.get("pick") in friends else None
            pick = st.radio(cat, friends, index=index, format_func=lambda n: label(cls, n),
                            label_visibility="collapsed", key=f"pick_{key}_{voter}_{cat}")
            reason = st.text_input("고른 이유 한 줄 (친구에게 그대로 전달돼요)", value=before.get("reason", ""),
                                   max_chars=100, key=f"reason_{key}_{voter}_{cat}")
            picks[cat] = {"pick": pick, "reason": clean(reason)}
            st.divider()
        submitted = st.form_submit_button("투표 제출", type="primary")

    if submitted:
        if any(p["pick"] is None for p in picks.values()):
            st.error("세 부문 모두 하나씩 골라 주세요.")
        elif any(not p["reason"] for p in picks.values()):
            st.error("부문마다 고른 이유를 한 줄씩 적어 주세요.")
        elif run(save_vote, key, voter, picks):
            st.session_state.flash = "투표가 제출되었어요!"
            st.session_state.balloons = True
            st.rerun()


# ── 탭: 결과 보기 ─────────────────────────────────────
def tab_results(key, cls, is_admin):
    projects = cls["projects"]
    ballots = valid_ballots(cls)
    done = [v for v, b in ballots.items() if len(b) == len(CATEGORIES)]

    st.subheader("🏆 투표 결과")
    c1, c2 = st.columns([1, 2])
    students = cls["students"]
    c1.metric("투표 완료", f"{len(done)} / {len(students)}명")
    waiting = sorted(set(students) - set(done))
    if waiting:
        c2.caption("아직 투표하지 않은 친구")
        c2.write(", ".join(waiting))

    if not cls.get("revealed"):
        if not is_admin:
            st.info("선생님이 결과를 공개하면 여기에서 볼 수 있어요.")
            return
        st.warning("👀 선생님 미리보기: 학생들에게는 아직 결과가 보이지 않아요.")

    if not projects:
        return

    # 종합 순위: 세 부문 득표 합계
    per_cat = {cat: {n: 0 for n in projects} for cat in CATEGORIES}
    for ballot in ballots.values():
        for cat, p in ballot.items():
            per_cat[cat][p["pick"]] += 1
    total = {n: sum(per_cat[cat][n] for cat in CATEGORIES) for n in projects}

    st.markdown("### 종합 순위")
    st.caption("세 부문에서 받은 표를 모두 더한 순위예요.")
    lines = ["| 순위 | 프로젝트 | 이름 | " + " | ".join(SHORT[c] for c in CATEGORIES) + " | 합계 |",
             "|:--:|---|---|" + "--:|" * len(CATEGORIES) + "--:|"]
    for rank, name, score in ranking(total):
        place = f"{MEDALS[rank]} {rank}위" if rank in MEDALS and score > 0 else f"{rank}위"
        cells = [place, md(projects[name]["title"]), md(name)]
        cells += [str(per_cat[cat][name]) for cat in CATEGORIES] + [f"**{score}**"]
        lines.append("| " + " | ".join(cells) + " |")
    st.markdown("\n".join(lines))

    st.markdown("### 부문별 1위")
    cols = st.columns(len(CATEGORIES))
    max_votes = max(len(cls["students"]) - 1, 1)  # 한 프로젝트가 받을 수 있는 최대 표 (본인 제외)
    for col, cat in zip(cols, CATEGORIES):
        with col.container(border=True):
            st.markdown(f"**{cat}**")
            ranked = ranking(per_cat[cat])
            top = ranked[0][2] if ranked else 0
            winners = [label(cls, n) for r, n, s in ranked if r == 1 and s > 0]
            if winners:
                st.markdown(f"🥇 **{', '.join(winners)}** · {top}표")
            else:
                st.caption("아직 표가 없어요.")
            for rank, name, score in ranked:
                st.progress(min(score / max_votes, 1.0), text=f"{rank}위 {label(cls, name)} · {score}표")

    st.divider()
    st.markdown("### 💌 내가 받은 한마디")
    me = current_student(key, cls)
    if is_admin:
        me = st.selectbox("학생 (선생님은 모두 볼 수 있어요)", sorted(projects), index=None,
                          placeholder="이름을 골라 주세요", key=f"notes_admin_{key}")
    elif not me:
        st.caption("위의 👤 학생 로그인에서 들어오면 친구들이 나에게 남긴 한마디를 볼 수 있어요.")
    elif me not in projects:
        st.caption("프로젝트를 등록하면 친구들이 남긴 한마디를 받을 수 있어요.")
        me = None
    if me:
        notes = [(cat, p["reason"]) for b in ballots.values() for cat, p in b.items()
                 if p["pick"] == me and p.get("reason")]
        if not notes:
            st.write("아직 받은 한마디가 없어요.")
        for cat, reason in sorted(notes, key=lambda x: list(CATEGORIES).index(x[0])):
            st.markdown(f"- **{cat}** · {reason}")


# ── 탭: 선생님 관리 ───────────────────────────────────
def class_creator(defaults=None):
    defaults = defaults or {}
    st.markdown("#### ➕ 새 반 만들기")
    with st.form("create_class", clear_on_submit=False):
        c1, c2, c3 = st.columns(3)
        semester = c1.text_input("학기", value=defaults.get("semester", ""), placeholder="26가을",
                                 help="연도 두 자리 + 봄/여름/가을/겨울 (예: 26가을, 27봄)")
        course = c2.text_input("수업", value=defaults.get("course", ""), placeholder="AI파이썬랩 심화")
        timeslot = c3.text_input("시간대", value="", placeholder="토/1100",
                                 help="요일/시각 네 자리 (예: 토/0900, 수/1930)")
        if st.form_submit_button("반 만들기", type="primary"):
            semester, course, timeslot = clean(semester).replace(" ", ""), clean(course), clean(timeslot).replace(" ", "")
            if not SEMESTER_RE.match(semester):
                st.error("학기는 26가을, 27봄 처럼 적어 주세요.")
            elif not course:
                st.error("수업 이름을 적어 주세요.")
            elif not TIMESLOT_RE.match(timeslot):
                st.error("시간대는 토/0900, 수/1930 처럼 적어 주세요.")
            elif run(create_class, semester, course, timeslot):
                # 사이드바 선택 상자는 이미 그려졌으므로 다음 실행 때 새 반으로 바꾸기
                st.session_state.pending_select = (semester, course, timeslot)
                st.session_state.flash = f"{semester} · {course} · {timeslot} 반을 만들었어요."
                st.rerun()


def tab_admin(key, cls, data):
    st.subheader("⚙️ 선생님 관리")

    st.markdown("#### 🔗 학생에게 보낼 주소")
    try:
        base = st.context.url.split("?")[0]
    except Exception:
        base = ""
    query = f"?semester={quote(cls['semester'])}&course={quote(cls['course'])}&time={quote(cls['timeslot'])}"
    st.code(f"{base}{query}", language=None)
    st.caption("이 주소로 들어오면 이 반이 바로 선택돼요.")

    st.markdown("#### 🎛️ 진행 단계")
    c1, c2, c3 = st.columns(3)
    for col, flag, text in ((c1, "registration_open", "프로젝트 등록 받기"),
                            (c2, "voting_open", "투표 받기"),
                            (c3, "revealed", "결과 공개")):
        current = cls.get(flag, flag != "revealed")
        new = col.toggle(text, value=current, key=f"flag_{key}_{flag}")
        if new != current:
            run(set_flag, key, flag, new)
            st.rerun()

    st.markdown("#### 🧹 정리")
    c1, c2 = st.columns(2)
    with c1.container(border=True):
        target = st.selectbox("프로젝트 삭제", sorted(cls["projects"]), index=None,
                              format_func=lambda n: label(cls, n), placeholder="삭제할 프로젝트")
        if st.button("선택한 프로젝트 삭제", disabled=target is None):
            if run(delete_project, key, target):
                st.session_state.flash = f"{target} 학생의 프로젝트를 지웠어요."
                st.rerun()
        st.divider()
        gone = st.selectbox("학생 삭제", sorted(cls["students"]), index=None,
                            placeholder="잘못 들어온 이름 등", key=f"del_student_{key}",
                            help="그 학생의 프로젝트와 투표도 함께 지워져요.")
        if st.button("선택한 학생 삭제", disabled=gone is None):
            if run(delete_student, key, gone):
                st.session_state.flash = f"{gone} 학생을 프로젝트·투표와 함께 지웠어요."
                st.rerun()
        st.divider()
        who = st.selectbox("PIN 다시 정하기", sorted(cls["students"]), index=None,
                           placeholder="PIN을 잊어버린 학생", key=f"pin_target_{key}")
        new_pin = st.text_input("새 PIN (숫자 4자리)", max_chars=4, key=f"pin_new_{key}")
        if st.button("PIN 바꾸기", disabled=who is None):
            if not PIN_RE.match(new_pin.strip()):
                st.error("PIN은 숫자 4자리로 적어 주세요.")
            elif run(set_pin, key, who, new_pin.strip()) is not None:
                record_ok(("student", key, who))  # 잠긴 상태도 풀어 주기
                st.session_state.flash = f"{who} 학생의 PIN을 바꿨어요. 학생에게 알려 주세요."
                st.rerun()
    with c2.container(border=True):
        sure = st.checkbox("이 반의 투표를 모두 지울게요", key=f"sure_reset_{key}")
        if st.button("투표 초기화", disabled=not sure):
            run(reset_votes, key)
            st.session_state.flash = "투표를 초기화했어요."
            st.rerun()
        sure_del = st.checkbox("이 반을 프로젝트·투표와 함께 지울게요", key=f"sure_del_{key}")
        if st.button("반 삭제", disabled=not sure_del):
            run(delete_class, key)
            st.session_state.flash = "반을 삭제했어요."
            st.rerun()

    st.divider()
    class_creator({"semester": cls["semester"], "course": cls["course"]})

    st.divider()
    st.markdown("#### 💾 백업")
    st.download_button("전체 데이터 내려받기 (JSON)",
                       json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                       file_name=f"vote_data_{datetime.now(KST):%Y%m%d_%H%M}.json", mime="application/json")
    st.caption(f"저장 위치: {store.name}")


# ── 메인 ─────────────────────────────────────────────
def main():
    st.title("🏆 피클 프로젝트 투표")
    is_admin = teacher_login()

    try:
        data = normalize(store.load())
    except Exception as e:
        st.error(f"데이터를 불러오지 못했어요. 새로고침해 주세요. ({e})")
        st.stop()

    if msg := st.session_state.pop("flash", None):
        st.toast(msg, icon="✅")
    if st.session_state.pop("balloons", False):
        st.balloons()

    classes = data["classes"]
    key = pick_class(classes)
    if key is None or key not in classes:
        st.info("아직 만들어진 반이 없어요. 선생님이 반을 만들면 여기에서 고를 수 있어요.")
        if is_admin:
            class_creator()
        return

    cls = classes[key]
    st.markdown(f"#### {cls['semester']} · {cls['course']} · {cls['timeslot']}")
    steps = [
        ("등록 받는 중" if cls.get("registration_open", True) else "등록 마감"),
        ("투표 받는 중" if cls.get("voting_open", True) else "투표 마감"),
        ("결과 공개" if cls.get("revealed") else "결과 비공개"),
    ]
    st.caption(" · ".join(steps))
    student_panel(key, cls)

    names = ["📝 프로젝트 등록", "🔍 프로젝트 보기", "🗳️ 투표하기", "🏆 결과 보기"]
    if is_admin:
        names.append("⚙️ 관리")
    tabs = st.tabs(names, key="main_tabs")  # key: 제출 후 새로고침돼도 보던 탭 유지
    with tabs[0]:
        tab_register(key, cls)
    with tabs[1]:
        tab_projects(key, cls)
    with tabs[2]:
        tab_vote(key, cls)
    with tabs[3]:
        tab_results(key, cls, is_admin)
    if is_admin:
        with tabs[4]:
            tab_admin(key, cls, data)

    st.sidebar.divider()
    if st.sidebar.button("🔄 새로고침", width="stretch"):
        st.rerun()


main()
