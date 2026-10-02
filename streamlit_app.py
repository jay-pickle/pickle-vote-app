import json
import re
from datetime import datetime
from urllib.parse import quote
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
KST = ZoneInfo("Asia/Seoul")


@st.cache_resource
def get_store():
    return make_store(st.secrets)


store = get_store()


def now():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def admin_password():
    try:
        return st.secrets["ADMIN_PASSWORD"]
    except Exception:
        return "dlab"  # 로컬 테스트용 기본값


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


# ── 집계 ──────────────────────────────────────────────
def valid_ballots(cls):
    """등록된 학생이 낸 투표만, 등록된 프로젝트를 고른 것만"""
    projects = cls["projects"]
    ballots = {}
    for voter, vote in cls["votes"].items():
        if voter not in projects:
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
def save_project(key, student, project):
    def change(data):
        cls = data["classes"].get(key)
        if cls is None:
            raise ValueError("반을 찾을 수 없어요.")
        if not cls.get("registration_open", True):
            raise ValueError("지금은 프로젝트 등록이 닫혀 있어요.")
        old = cls["projects"].get(student, {})
        cls["projects"][student] = {**project, "created_at": old.get("created_at", now()), "updated_at": now()}
        return bool(old)

    return store.update(change, f"project: {key} {student}")


def save_vote(key, voter, picks):
    def change(data):
        cls = data["classes"].get(key)
        if cls is None:
            raise ValueError("반을 찾을 수 없어요.")
        if not cls.get("voting_open", True):
            raise ValueError("지금은 투표가 닫혀 있어요.")
        if voter not in cls["projects"]:
            raise ValueError("프로젝트를 등록한 학생만 투표할 수 있어요.")
        for p in picks.values():
            if p["pick"] not in cls["projects"] or p["pick"] == voter:
                raise ValueError("고른 프로젝트를 다시 확인해 주세요.")
        cls["votes"][voter] = {"picks": picks, "voted_at": now()}
        return True

    return store.update(change, f"vote: {key} {voter}")


def set_flag(key, flag, value):
    def change(data):
        data["classes"][key][flag] = value

    store.update(change, f"{flag}={value}: {key}")


def create_class(semester, course, timeslot):
    key = class_key(semester, course, timeslot)

    def change(data):
        if key in data["classes"]:
            raise ValueError("이미 있는 반이에요.")
        data["classes"][key] = {
            "semester": semester, "course": course, "timeslot": timeslot,
            "registration_open": True, "voting_open": True, "revealed": False,
            "created_at": now(), "projects": {}, "votes": {},
        }

    store.update(change, f"create class: {key}")
    return key


def delete_project(key, student):
    def change(data):
        cls = data["classes"][key]
        cls["projects"].pop(student, None)
        cls["votes"].pop(student, None)

    store.update(change, f"delete project: {key} {student}")


def reset_votes(key):
    def change(data):
        data["classes"][key]["votes"] = {}
        data["classes"][key]["revealed"] = False

    store.update(change, f"reset votes: {key}")


def delete_class(key):
    def change(data):
        data["classes"].pop(key, None)

    store.update(change, f"delete class: {key}")


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
        if st.session_state.get("is_admin"):
            st.success("선생님 모드")
            if st.button("나가기", width="stretch"):
                st.session_state.is_admin = False
                st.rerun()
        else:
            with st.form("login", border=False):
                pw = st.text_input("비밀번호", type="password")
                if st.form_submit_button("들어가기", width="stretch"):
                    if pw == admin_password():
                        st.session_state.is_admin = True
                        st.rerun()
                    else:
                        st.error("비밀번호가 달라요.")
    return st.session_state.get("is_admin", False)


# ── 탭: 프로젝트 등록 ─────────────────────────────────
def tab_register(key, cls):
    st.subheader("📝 내 프로젝트 등록하기")
    if not cls.get("registration_open", True):
        st.info("지금은 프로젝트 등록이 닫혀 있어요. 선생님께 말씀해 주세요.")
        return

    student = clean(st.text_input("내 이름", key=f"reg_name_{key}", placeholder="예: 김피클"))
    if not student:
        st.caption("이름을 입력하면 프로젝트 정보를 적는 칸이 나와요. 등록한 뒤에도 같은 이름으로 다시 들어오면 고칠 수 있어요.")
        return

    old = cls["projects"].get(student, {})
    if old:
        st.info(f"**{student}** 이름으로 등록한 프로젝트가 있어요. 고쳐서 다시 제출하면 새 내용으로 바뀌어요.")

    with st.form(f"reg_form_{key}_{student}"):
        title = st.text_input("프로젝트 이름", value=old.get("title", ""), max_chars=40)
        reason = st.text_area("만든 이유", value=old.get("reason", ""), height=100,
                              placeholder="어떤 불편을 해결하고 싶었나요?")
        features = st.text_area("기능 설명", value=old.get("features", ""), height=120,
                                placeholder="- 입력하면 ...\n- 버튼을 누르면 ...")
        url = st.text_input("배포한 URL", value=old.get("url", ""), placeholder="https://내앱.streamlit.app")
        submitted = st.form_submit_button("등록하기" if not old else "수정하기", type="primary")

    if submitted:
        title, url = clean(title), url.strip()
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        missing = [n for n, v in (("프로젝트 이름", title), ("만든 이유", reason.strip()),
                                  ("기능 설명", features.strip()), ("배포한 URL", url)) if not v]
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


# ── 탭: 프로젝트 둘러보기 ─────────────────────────────
def tab_projects(cls):
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
            st.link_button("🚀 앱 열어 보기", p["url"], width="stretch")


# ── 탭: 투표하기 ──────────────────────────────────────
def tab_vote(key, cls):
    st.subheader("🗳️ 베스트 프로젝트 투표")
    projects = cls["projects"]
    if not cls.get("voting_open", True):
        st.info("지금은 투표가 닫혀 있어요.")
        return
    if len(projects) < 2:
        st.info("프로젝트가 2개 이상 등록되면 투표할 수 있어요.")
        return

    st.caption("부문마다 친구 프로젝트 하나씩 골라요. 내 프로젝트는 목록에 나오지 않고, 다시 제출하면 앞의 투표가 새 투표로 바뀌어요.")
    voter = st.selectbox("내 이름", sorted(projects), index=None, placeholder="이름을 골라 주세요",
                         key=f"voter_{key}")
    if not voter:
        st.caption("프로젝트를 등록한 학생만 이름 목록에 나와요.")
        return

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
def tab_results(cls, is_admin):
    projects = cls["projects"]
    ballots = valid_ballots(cls)
    done = [v for v, b in ballots.items() if len(b) == len(CATEGORIES)]

    st.subheader("🏆 투표 결과")
    c1, c2 = st.columns([1, 2])
    c1.metric("투표 완료", f"{len(done)} / {len(projects)}명")
    waiting = sorted(set(projects) - set(done))
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
    max_votes = max(len(projects) - 1, 1)
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
                st.progress(score / max_votes, text=f"{rank}위 {label(cls, name)} · {score}표")

    st.divider()
    st.markdown("### 💌 내가 받은 한마디")
    me = st.selectbox("이름", sorted(projects), index=None, placeholder="이름을 골라 주세요",
                      key=f"me_{cls['semester']}_{cls['course']}_{cls['timeslot']}")
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
            run(delete_project, key, target)
            st.session_state.flash = f"{target} 학생의 프로젝트와 투표를 지웠어요."
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
        data = store.load()
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

    names = ["📝 프로젝트 등록", "🔍 프로젝트 보기", "🗳️ 투표하기", "🏆 결과 보기"]
    if is_admin:
        names.append("⚙️ 관리")
    tabs = st.tabs(names, key="main_tabs")  # key: 제출 후 새로고침돼도 보던 탭 유지
    with tabs[0]:
        tab_register(key, cls)
    with tabs[1]:
        tab_projects(cls)
    with tabs[2]:
        tab_vote(key, cls)
    with tabs[3]:
        tab_results(cls, is_admin)
    if is_admin:
        with tabs[4]:
            tab_admin(key, cls, data)

    st.sidebar.divider()
    if st.sidebar.button("🔄 새로고침", width="stretch"):
        st.rerun()


main()
