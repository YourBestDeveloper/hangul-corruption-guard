#!/usr/bin/env python3
"""회귀 테스트 — 의존성 없이 `python3 tests/test_guard.py` 로 돌린다.

훅을 서브프로세스로 띄워 stdin/종료코드 계약을 그대로 검증한다.
0 = 통과, 2 = 차단.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "core", "hangul_corruption_guard.py")

NOTION = "mcp__plugin_Notion_notion__notion-update-page"
NOTION_NEW = "mcp__plugin_Notion_notion__notion-create-pages"
JIRA = "mcp__claude_ai_Atlassian_Rovo__editJiraIssue"
CONFLUENCE = "mcp__claude_ai_Atlassian_Rovo__updateConfluencePage"

BASE = "기능 권한을 하나로 합쳐야 한다. 사용자에게는 서버 에러만 뜬다."
DRIFT = BASE.replace("뜬다", "뜼다")
A = "- 정책 A 구상권 요율의 최소값은 1이며 복합키는 지역과 주체다"
B = "- 정책 B 미배정 구간에 폴백을 두지 않으며 기존 값으로 처리한다"

PASS, BLOCK = 0, 2
results = []


class Env:
    """임시 프로젝트 + 기준본 폴더. 에코 캐시는 매 호출 비운다."""

    def __init__(self):
        self.proj = tempfile.mkdtemp()
        self.staging = os.path.join(self.proj, ".claude", "hangul-staging")
        self.cache = tempfile.mkdtemp()
        os.makedirs(self.staging)

    def stage(self, **files):
        shutil.rmtree(self.staging, ignore_errors=True)
        os.makedirs(self.staging)
        for name, body in files.items():
            path = os.path.join(self.staging, name.replace("__", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            self.last = path

    def raw(self, payload, fresh=True):
        """도구별 원본 페이로드를 그대로 보낸다 — 스키마 정규화 검증용."""
        if fresh:
            shutil.rmtree(self.cache, ignore_errors=True)
        env = dict(os.environ)
        env["HOME"] = self.cache
        env.pop("HANGUL_STAGING", None)
        env.pop("HANGUL_GUARD", None)
        payload = {"cwd": self.proj, **payload}
        proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)
        return proc.returncode, proc.stderr.strip()

    def call(self, tool, tool_input, fresh=True, guard=None, session="S"):
        if fresh:
            shutil.rmtree(self.cache, ignore_errors=True)
        env = dict(os.environ)
        env["HOME"] = self.cache          # 에코 캐시를 테스트별로 격리
        env.pop("HANGUL_STAGING", None)
        env.pop("HANGUL_GUARD", None)
        if guard:
            env["HANGUL_GUARD"] = guard
        payload = {"session_id": session, "cwd": self.proj,
                   "tool_name": tool, "tool_input": tool_input}
        proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)
        return proc.returncode, proc.stderr.strip()

    def cleanup(self):
        shutil.rmtree(self.proj, ignore_errors=True)
        shutil.rmtree(self.cache, ignore_errors=True)


def check(label, expected, got):
    ok = got == expected
    results.append((label, ok, expected, got))


def upd(**kw):
    return {"page_id": "p", "command": "update_content", **kw}


def run():
    env = Env()
    try:
        # --- 표류 검출 ---
        env.stage(**{"current__p.md": BASE + "\n"})
        check("표류 1글자", BLOCK, env.call(NOTION, upd(
            content_updates=[{"old_str": "x", "new_str": DRIFT}]))[0])
        check("기준본과 일치", PASS, env.call(NOTION, upd(
            content_updates=[{"old_str": "x", "new_str": BASE}]))[0])
        check("NFD 자모분해 표류", BLOCK, env.call(NOTION, upd(
            content_updates=[{"old_str": "x",
                              "new_str": unicodedata.normalize("NFD", DRIFT)}]))[0])
        check("old_str 표류", BLOCK, env.call(NOTION, upd(
            content_updates=[{"old_str": DRIFT, "new_str": "짧게 줄여 다시 쓴다"}]))[0])
        check("properties 키 표류", BLOCK, env.call(NOTION, {
            "page_id": "p", "command": "update_properties", "properties": {DRIFT: "v"}})[0])

        env.stage(**{"current__p.md": "기능 권한을 하나로 합쳐야 한다\n사용자에게는 서버 에러만 뜬다\n"})
        frag = lambda *v: upd(content_updates=[{"old_str": "x", "new_str": s} for s in v])
        check("조각내기로 우회 불가", BLOCK, env.call(NOTION, frag(
            "기능 권한을 하나로 합쳤야 한다", "사용자에게는 서버 에러만 뜬다"))[0])
        check("조각내기 정상", PASS, env.call(NOTION, frag(
            "기능 권한을 하나로 합쳐야 한다", "사용자에게는 서버 에러만 뜬다"))[0])

        # --- 조용한 삭제 ---
        env.stage(**{"target__p.md": A + "\n" + B + "\n",
                     "target__C.md": A + "\n" + B + "\n",
                     "current__C.md": A + "\n" + B + "\n"})
        check("replace_content 항목 삭제", BLOCK, env.call(NOTION, {
            "page_id": "p", "command": "replace_content", "new_str": A})[0])
        check("Jira description 항목 삭제", BLOCK, env.call(JIRA, {
            "issueIdOrKey": "C", "fields": {"description": A}})[0])
        check("Confluence body 항목 삭제", BLOCK, env.call(CONFLUENCE, {
            "pageId": "C", "body": A})[0])
        check("한 줄 old_str 삭제", BLOCK, env.call(NOTION, upd(
            content_updates=[{"old_str": B, "new_str": ""}]))[0])
        check("통짜 교체 전량 유지", PASS, env.call(NOTION, {
            "page_id": "p", "command": "replace_content", "new_str": A + "\n" + B})[0])

        # --- 마커 치환 ---
        check("마커 치환", PASS, env.call(JIRA, {
            "issueIdOrKey": "C", "fields": {"description": "@@hangul:current/C.md@@"}})[0])
        check("마커 경로 탈출 거부", BLOCK, env.call(JIRA, {
            "issueIdOrKey": "C", "fields": {"description": "@@hangul:../../x@@"}})[0])
        check("마커가 없는 파일", BLOCK, env.call(JIRA, {
            "issueIdOrKey": "C", "fields": {"description": "@@hangul:current/none.md@@"}})[0])
        old = time.time() - 20 * 3600
        os.utime(os.path.join(env.staging, "current", "C.md"), (old, old))
        code, msg = env.call(JIRA, {"issueIdOrKey": "C",
                                    "fields": {"description": "@@hangul:current/C.md@@"}})
        check("오래된 기준본 확인", BLOCK, code)
        check("  확인 후 재전송 통과", PASS, env.call(JIRA, {
            "issueIdOrKey": "C", "fields": {"description": "@@hangul:current/C.md@@"}},
            fresh=False)[0])

        # --- 문서 정체성 ---
        env.stage(**{"target__p.md": A + "\n"})
        check("타 문서 기준본은 통과시키지 않음", BLOCK, env.call(JIRA, {
            "issueIdOrKey": "C", "fields": {"description": A}})[0])
        env.stage(**{"target__PARENT.md": A + "\n"})
        check("중첩 page_id 인식", PASS, env.call(NOTION_NEW, {
            "parent": {"page_id": "PARENT"}, "pages": [{"content": A}]})[0])

        # --- 막다른 길이 없어야 한다 ---
        env.stage()
        new = upd(content_updates=[{"old_str": "x",
                                    "new_str": "이번 스프린트에서 지역 상태 노출 범위를 확장한다"}])
        env.call(NOTION, new)
        check("새 내용 재전송 통과", PASS, env.call(NOTION, new, fresh=False)[0])
        big = {"page_id": "p", "command": "replace_content",
               "new_str": "\n".join(f"- 정책 {i} {A[6:]}" for i in range(120))}
        env.call(NOTION, big)
        check("4000자 초과 재전송 통과", PASS, env.call(NOTION, big, fresh=False)[0])

        # --- 검사 대상 밖 ---
        env.stage(**{"current__p.md": BASE + "\n"})
        check("킬 스위치 off", PASS, env.call(JIRA, {
            "issueIdOrKey": "p", "fields": {"description": DRIFT}}, guard="off")[0])
        check("브라우저 폼 입력", PASS, env.call("mcp__browseros__act", {"text": DRIFT})[0])
        check("한글 검색어", PASS, env.call(
            "mcp__plugin_Notion_notion__notion-search", {"query": DRIFT})[0])
        check("MCP 아닌 도구", PASS, env.call("Bash", {"command": DRIFT})[0])
        check("짧은 한글", PASS, env.call(NOTION, upd(
            content_updates=[{"old_str": "x", "new_str": "딜마"}]))[0])

        # --- 줄 지정 마커 ---
        # old_str 처럼 기존 본문의 조각을 여러 개 보내야 할 때, 파일 전체만 넣을 수
        # 있으면 조각마다 파일을 만들어야 한다. 그러면 결국 손으로 옮겨 적게 된다.
        rows = ["첫째 줄은 어디까지 저장됐는지를 모달에 남긴다",
                "둘째 줄은 해제가 이 스텝 흐름을 타지 않는다",
                "셋째 줄은 딜마 매핑만 중지하고 설정은 남긴다"]
        env.stage(**{"current__L.md": "\n".join(rows) + "\n",
                     "target__L.md": "\n".join(rows) + "\n"})
        def sent(marker):
            # old_str·new_str 을 같은 마커로 둔다 — 삭제 검사를 건드리지 않고
            # 치환 결과만 본다
            payload = {"page_id": "L", "command": "update_content",
                       "content_updates": [{"old_str": marker, "new_str": marker}]}
            shutil.rmtree(env.cache, ignore_errors=True)
            e = dict(os.environ); e["HOME"] = env.cache
            e.pop("HANGUL_STAGING", None); e.pop("HANGUL_GUARD", None)
            proc = subprocess.run(
                [sys.executable, HOOK],
                input=json.dumps({"session_id": "S", "cwd": env.proj,
                                  "tool_name": NOTION, "tool_input": payload}),
                capture_output=True, text=True, env=e)
            if proc.returncode or not proc.stdout:
                return None
            out = json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]
            return out["content_updates"][0]["old_str"]
        check("한 줄 지정", rows[1], sent("@@hangul:current/L.md#L2@@"))
        check("여러 줄 지정", rows[0] + "\n" + rows[1],
              sent("@@hangul:current/L.md#L1-L2@@"))
        check("파일 전체는 그대로", "\n".join(rows), sent("@@hangul:current/L.md@@"))
        check("범위 밖은 차단", None, sent("@@hangul:current/L.md#L9@@"))
        check("역순 범위는 차단", None, sent("@@hangul:current/L.md#L3-L1@@"))

        # --- 마커가 조용히 새어 나가는 경로 (전부 실측 재현으로 확인한 결함) ---
        def run_raw(tool, tool_input, session="S", fresh=True):
            """(종료코드, stdout) — 치환 결과까지 봐야 하는 검사용."""
            if fresh:
                shutil.rmtree(env.cache, ignore_errors=True)
            e = dict(os.environ); e["HOME"] = env.cache
            e.pop("HANGUL_STAGING", None); e.pop("HANGUL_GUARD", None)
            proc = subprocess.run(
                [sys.executable, HOOK],
                input=json.dumps({"session_id": session, "cwd": env.proj,
                                  "tool_name": tool, "tool_input": tool_input}),
                capture_output=True, text=True, env=e)
            return proc.returncode, proc.stdout

        # T1 — 신규 문서는 doc_id 가 없어 늘 에코를 한 번 거친다. 그 승인 히트에서
        # updatedInput 을 내지 않으면 호스트가 원본(=마커 문자열)을 그대로 저장한다.
        body = "\n".join(rows)
        env.stage(**{"target__새문서.md": body + "\n"})
        new_doc = {"projectKey": "CORE", "summary": "t",
                   "description": "@@hangul:target/새문서.md@@"}
        first, _ = run_raw("mcp__x__createJiraIssue", new_doc, session="N")
        second, out = run_raw("mcp__x__createJiraIssue", new_doc, session="N", fresh=False)
        check("신규 문서 마커 1회차는 에코", BLOCK, first)
        check("신규 문서 마커 재전송이 치환을 낸다", body,
              (json.loads(out)["hookSpecificOutput"]["updatedInput"]["description"]
               if second == PASS and out else None))

        # T2 — 형태가 어긋난 마커는 치환되지 않는다. 한글이 0자라 수집도 안 돼
        # 그대로 통과하면 본문 대신 마커 문자열이 저장된다.
        env.stage(**{"current__L.md": body + "\n"})
        for label, bad in [("끝 L 누락", "@@hangul:current/L.md#L1-2@@"),
                           ("L 접두 누락", "@@hangul:current/L.md#1@@"),
                           ("앞에 다른 글자", "## 제목\n@@hangul:current/L.md@@")]:
            check(f"어긋난 마커 차단 — {label}", BLOCK,
                  run_raw(JIRA, {"issueIdOrKey": "L", "fields": {"summary": bad}})[0])
        check("정상 마커는 통과", PASS,
              run_raw(JIRA, {"issueIdOrKey": "L",
                             "fields": {"summary": "@@hangul:current/L.md#L1@@"}})[0])
        check("검사 대상 밖 도구의 마커 차단", BLOCK,
              env.call("mcp__browseros__act", {"text": "@@hangul:current/L.md@@"})[0])

        # T3 — 통짜 교체 필드에 줄 범위를 쓰면 지정한 줄만 남고 나머지가 삭제된다
        check("통짜 필드 + 줄 범위 마커 차단", BLOCK,
              run_raw(JIRA, {"issueIdOrKey": "L",
                             "fields": {"description": "@@hangul:current/L.md#L1-L2@@"}})[0])
        check("통짜 필드 + 파일 전체 마커는 통과", PASS,
              run_raw(JIRA, {"issueIdOrKey": "L",
                             "fields": {"description": "@@hangul:current/L.md@@"}})[0])

        # C6 — 실패 사유가 갈려야 진단이 엉뚱한 곳을 짚지 않는다
        rc, msg = env.call(JIRA, {"issueIdOrKey": "L",
                                  "fields": {"summary": "@@hangul:current/L.md#L99@@"}})
        check("범위 밖은 경로 문제로 진단하지 않는다", True,
              rc == BLOCK and "줄 범위가 파일을 벗어납니다" in msg)
        rc, msg = env.call(JIRA, {"issueIdOrKey": "L",
                                  "fields": {"summary": "@@hangul:없는파일.md@@"}})
        check("없는 파일은 경로로 진단한다", True,
              rc == BLOCK and "찾을 수 없습니다" in msg)

        # C7 — 차단 메시지가 재생산 말고 마커를 권해야 한다
        env.stage(**{"current__p.md": BASE + "\n"})
        _rc, msg = env.call(NOTION, upd(content_updates=[{"old_str": "x", "new_str": DRIFT}]))
        check("표류 메시지가 마커를 권한다", True, "@@hangul:" in msg)
        _rc, msg = env.call(NOTION, upd(content_updates=[{"old_str": DRIFT, "new_str": "짧게"}]))
        check("old_str 메시지가 마커를 권한다", True, "@@hangul:current/p.md#L" in msg)
        _rc, msg = env.call(NOTION, upd(content_updates=[
            {"old_str": "x", "new_str": "완전히 새로운 문장이라 기준본에 없는 내용이다"}]))
        check("에코 메시지가 마커를 권한다", True, "@@hangul:" not in msg or "마커" in msg)

        # --- old_str 은 서버 본문 그대로여야 한다 ---
        # 실사용 사례: 「금액 축이다」→「금액 용로 이다」. 길이가 달라 표류 서명에서
        # 빠졌고 노션 400 이 대신 잡았다. old_str 은 '새 내용' 이라는 여지가 없으므로
        # new_str 보다 엄한 기준(유사도)을 쓴다.
        page = "설정 단위는 지역과 주문 관리 주체다. 배달대행사는 키가 아니라 금액 축이다"
        rewritten = "설정 단위는 지역과 주문 관리 주체이며 금액 축은 배달대행사별로 둔다"
        env.stage(**{"current__O.md": page + "\n", "target__O.md": rewritten + "\n"})
        edit = lambda old: env.call(NOTION, {
            "page_id": "O", "command": "update_content",
            "content_updates": [{"old_str": old, "new_str": rewritten}]})
        check("old_str 정상은 통과", PASS, edit(page)[0])
        code, msg = edit(page.replace("금액 축이다", "금액 용로 이다"))
        check("길이 바뀐 old_str 손상을 잡는다", BLOCK, code)
        check("  어긋난 구간을 짚는다", True, "위치 39" in msg and "「축」" in msg)
        check("길이 같은 old_str 손상도 잡는다", BLOCK,
              edit(page.replace("축이다", "축이당"))[0])
        check("기준본에 없는 앵커는 통과", PASS,
              edit("이 문서에 없는 전혀 다른 문장을 앵커로 쓴다")[0])

        # --- 비슷한 줄이 늘어선 문서에서도 삭제를 잡는가 ---
        # 형제 줄이 알리바이가 되어 삭제가 가려지던 문제. 400줄에서 1줄을 지워도
        # 검출되지 않았고, 검출되더라도 엉뚱한 줄을 지목했다.
        siblings = [f"- 정책 {i} 구상권 요율의 최소값은 1이며 복합키는 지역과 주체다"
                    for i in range(8)]
        env.stage(**{"target__S.md": "\n".join(siblings) + "\n"})
        dropped = "\n".join(l for i, l in enumerate(siblings) if i != 3)
        code, msg = env.call(JIRA, {"issueIdOrKey": "S",
                                    "fields": {"description": dropped}})
        check("형제 줄 사이의 삭제를 잡는다", BLOCK, code)
        check("  지워진 줄을 정확히 지목", True, "정책 3 " in msg)
        check("  엉뚱한 줄은 지목하지 않음", True, "정책 7 " not in msg)
        check("전량 유지는 통과", PASS, env.call(JIRA, {
            "issueIdOrKey": "S", "fields": {"description": "\n".join(siblings)}})[0])

        # --- 문턱 값들이 의도대로 갈리는가 ---
        short = "구상권 요율 설정안"                       # 한글 9자
        long_line = ("구상권 요율의 최소값은 1이며 복합키는 지역과 주체이고 "
                     "딜마는 그 키에 들어가지 않는다")      # 한글 40자 남짓
        def corrupt(s, n):
            out, done = list(s), 0
            for i, ch in enumerate(out):
                if done >= n:
                    break
                if "가" <= ch <= "힣":
                    out[i] = chr(ord(ch) + 1)
                    done += 1
            return "".join(out)

        env.stage(**{"current__T.md": short + "\n" + long_line + "\n"})
        jira = lambda body: env.call(JIRA, {"issueIdOrKey": "T",
                                            "fields": {"description": body}})
        # 짧은 줄도 대조 대상이다 — 호출 총량 문턱을 없앤 결과
        check("짧은 줄 표류도 잡는다", BLOCK, jira(corrupt(short, 1))[0])
        check("짧은 줄 정상은 통과", PASS, jira(short)[0])
        # 표류 예산은 길이에 비례한다 (긴 줄은 3글자를 넘어도 표류로 본다)
        code, msg = jira(corrupt(long_line, 4))
        check("긴 줄 4글자 손상도 표류", BLOCK, code)
        check("  표류로 판정(에코 아님)", True, "글자가 다릅니다" in msg)
        # 너무 많이 다르면 다른 줄일 수 있으므로 표류로 단정하지 않는다
        _, msg = jira(corrupt(long_line, 20))
        check("20글자 손상은 표류 단정 안 함", True, "글자가 다릅니다" not in msg)
        # 에코 문턱은 유지 — 짧은 새 줄은 확인받지 않는다
        check("7자 새 줄은 에코 없음", PASS, jira(long_line + "\n가나다라마바사")[0])
        check("8자 새 줄은 에코", BLOCK, jira(long_line + "\n가나다라마바사아")[0])

        # --- 기존 본문을 그대로 옮긴 줄은 current/ 가 흡수한다 ---
        anchor = "원래 본문에 있던 앵커 문장을 그대로 옮겨 왔다"
        body = anchor + "\n새로 쓴 정책 문장은 여기에 있다"
        env.stage(**{"target__PROJ-9.md": "새로 쓴 정책 문장은 여기에 있다\n"})
        code, msg = env.call(JIRA, {"issueIdOrKey": "PROJ-9",
                                    "fields": {"description": body}})
        check("옮겨 온 앵커가 에코에 걸림", BLOCK, code)
        check("  current/ 를 채우라고 안내", True, "current/PROJ-9.md" in msg)
        env.stage(**{"target__PROJ-9.md": "새로 쓴 정책 문장은 여기에 있다\n",
                     "current__PROJ-9.md": anchor + "\n"})
        check("  current/ 채우면 통과", PASS, env.call(JIRA, {
            "issueIdOrKey": "PROJ-9", "fields": {"description": body}})[0])

        # --- 도구별 입력 스키마 정규화 ---
        # 실제 도구에서 돌려보지는 못한다. 각 도구 문서가 정의한 페이로드 모양을
        # 그대로 넣어 extract_call() 이 같은 판정에 도달하는지만 확인한다.
        env.stage(**{"current__K.md": BASE + "\n"})
        ok = {"issueIdOrKey": "K", "fields": {"description": BASE}}
        bad = {"issueIdOrKey": "K", "fields": {"description": DRIFT}}

        check("Claude Code 스키마 · 정상", PASS, env.raw({
            "session_id": "s", "hook_event_name": "PreToolUse",
            "tool_name": JIRA, "tool_input": ok})[0])
        check("Claude Code 스키마 · 표류", BLOCK, env.raw({
            "session_id": "s", "hook_event_name": "PreToolUse",
            "tool_name": JIRA, "tool_input": bad})[0])

        check("Codex 스키마 · 표류", BLOCK, env.raw({
            "session_id": "s", "hook_event_name": "PreToolUse", "turn_id": "t1",
            "permission_mode": "auto", "tool_use_id": "u1",
            "tool_name": JIRA, "tool_input": bad})[0])

        check("Cursor beforeMCPExecution · 정상", PASS, env.raw({
            "session_id": "s", "mcp_server_name": "atlassian",
            "tool_name": "editJiraIssue", "tool_input": ok})[0])
        check("Cursor beforeMCPExecution · 표류", BLOCK, env.raw({
            "session_id": "s", "mcp_server_name": "atlassian",
            "tool_name": "editJiraIssue", "tool_input": bad})[0])
        check("Cursor preToolUse (MCP: 접두어)", BLOCK, env.raw({
            "session_id": "s", "tool_use_id": "u1", "model": "x",
            "tool_name": "MCP:editJiraIssue", "tool_input": bad})[0])

        windsurf = lambda args: {
            "trajectory_id": "t", "execution_id": "e",
            "agent_action_name": "pre_mcp_tool_use",
            "tool_info": {"mcp_server_name": "atlassian",
                          "mcp_tool_name": "editJiraIssue",
                          "mcp_tool_arguments": args}}
        check("Windsurf pre_mcp_tool_use · 정상", PASS, env.raw(windsurf(ok))[0])
        check("Windsurf pre_mcp_tool_use · 표류", BLOCK, env.raw(windsurf(bad))[0])
        code, msg = env.raw(windsurf({"issueIdOrKey": "K", "fields": {
            "description": "@@hangul:current/K.md@@"}}))
        check("Windsurf 는 마커를 거부", BLOCK, code)
        check("  거부 사유가 치환 미지원", True, "치환하는 기능을 지원하지 않" in msg)

        check("Windsurf 브라우저 도구는 통과", PASS, env.raw({
            "trajectory_id": "t", "agent_action_name": "pre_mcp_tool_use",
            "tool_info": {"mcp_server_name": "browseros", "mcp_tool_name": "act",
                          "mcp_tool_arguments": {"text": DRIFT}}})[0])

        # --- staging 탐색은 cwd 에서 위로 (실측 함정: cd 한 번에 마커가 전량 거부됐다) ---
        def at_raw(cwd, tool_input, tool=JIRA, staging=None, home=None,
                   fresh=True, session="W"):
            """cwd·HOME·세션을 바꿔 가며 부른다 — Env.call 은 프로젝트 루트로 고정돼 있다."""
            if fresh:
                shutil.rmtree(env.cache, ignore_errors=True)
            e = dict(os.environ); e["HOME"] = home or env.cache
            e.pop("HANGUL_STAGING", None); e.pop("HANGUL_GUARD", None)
            if staging:
                e["HANGUL_STAGING"] = staging
            return subprocess.run(
                [sys.executable, HOOK],
                input=json.dumps({"session_id": session, "cwd": cwd,
                                  "tool_name": tool, "tool_input": tool_input}),
                capture_output=True, text=True, env=e)

        def at(*a, **kw):
            proc = at_raw(*a, **kw)
            return proc.returncode, proc.stderr.strip()

        def mkgit(path):
            """**진짜** 저장소를 만든다 — git_tracked 가 fail-closed 라 가짜 .git 으로는
            쓰기 경로가 열리지 않는다(rc≠0 이면 「모른다」로 보고 건드리지 않는다)."""
            os.makedirs(path, exist_ok=True)
            subprocess.run(["git", "-C", path, "init", "-q"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        env.stage(**{"current__K.md": BASE + "\n"})
        mark = {"issueIdOrKey": "K", "fields": {"description": "@@hangul:current/K.md@@"}}
        sub = os.path.join(env.proj, "src", "app")
        os.makedirs(sub, exist_ok=True)

        # 저장소 밖에서 위로 올라가면 /tmp 같은 world-writable 상위가 후보가 된다 — 안 올라간다
        check("저장소 밖에서는 상향 탐색 안 함", BLOCK, at(sub, mark)[0])

        mkgit(env.proj)                                              # 저장소 루트 표시
        check("하위 폴더에서도 마커가 산다", PASS, at(sub, mark)[0])
        check("staging 폴더 안에서도 마커가 산다", PASS, at(env.staging, mark)[0])
        code, msg = at(sub, {"issueIdOrKey": "K", "fields": {"description": DRIFT}})
        check("하위 폴더에서도 표류를 잡는다", BLOCK, code)
        # 종료코드만 보면 항진명제다 — 기준본을 못 찾아도 에코로 똑같이 2 가 난다
        check("  에코가 아니라 표류로 잡는다", True, "표류" in msg and "U+" in msg)

        # 서브모듈처럼 자기 .git 을 가진 하위 저장소 안에서도 바깥 기준본이 보여야 한다
        vendor = os.path.join(env.proj, "vendor", "lib")
        os.makedirs(vendor, exist_ok=True)
        with open(os.path.join(vendor, ".git"), "w", encoding="utf-8") as fh:
            fh.write("gitdir: ../../.git/modules/lib\n")
        check("중첩 저장소 안에서도 바깥 기준본이 보인다", PASS, at(vendor, mark)[0])

        # 저장소 루트 위는 보지 않는다 — 남의 기준본이 통행증이 되면 안 된다
        outer = os.path.abspath(os.path.join(
            env.proj, "..", "hcg-outer-" + os.path.basename(env.proj)))
        os.makedirs(os.path.join(outer, "inner", ".git"), exist_ok=True)
        os.makedirs(os.path.join(outer, ".claude", "hangul-staging", "current"), exist_ok=True)
        with open(os.path.join(outer, ".claude", "hangul-staging", "current", "K.md"),
                  "w", encoding="utf-8") as fh:
            fh.write(BASE + "\n")
        try:
            code, msg = at(os.path.join(outer, "inner"), mark)
            check("저장소 루트 위의 기준본은 안 쓴다", BLOCK, code)
            check("  찾은 위치를 전부 보여준다", True, "찾은 위치" in msg and "inner" in msg)
        finally:
            shutil.rmtree(outer, ignore_errors=True)

        # 남이 쓸 수 있는 폴더는 기준본으로 인정하지 않는다 (조용히 빼지 말고 알려준다)
        ww = os.path.join(env.proj, "ww")
        wws = os.path.join(ww, ".claude", "hangul-staging", "current")
        os.makedirs(wws, exist_ok=True)
        with open(os.path.join(wws, "K.md"), "w", encoding="utf-8") as fh:
            fh.write(BASE + "\n")
        os.chmod(os.path.join(ww, ".claude", "hangul-staging"), 0o777)
        env.stage(**{"current__Z.md": BASE + "\n"})      # 프로젝트 쪽에는 K.md 가 없다
        code, msg = at(ww, mark)
        check("남이 쓸 수 있는 폴더는 기준본으로 안 쓴다", BLOCK, code)
        check("  건너뛴 사실을 알려준다", True, "건너뜀" in msg)

        # --- staging 이 소비 프로젝트의 git 에 새지 않는다 ---
        env.stage(**{"current__K.md": BASE + "\n"})
        ignore = os.path.join(env.staging, ".gitignore")
        at(env.proj, mark)
        check("훅이 .gitignore 를 놓는다", True, os.path.isfile(ignore))
        # 파일이 없을 때 여기서 크래시하면 뒤 검사가 통째로 안 돈다 — 실패는 FAIL 로 남겨야 한다
        check("  자기 자신까지 무시한다", True, os.path.isfile(ignore) and
              "*" in open(ignore, encoding="utf-8").read().split("\n"))

        with open(ignore, "w", encoding="utf-8") as fh:
            fh.write("# 사람이 고친 것\n")
        at(env.proj, mark)
        check("이미 있으면 덮어쓰지 않는다", "# 사람이 고친 것\n",
              open(ignore, encoding="utf-8").read() if os.path.isfile(ignore) else None)

        # 심볼릭 링크를 따라가면 링크 대상 파일을 대신 만들어 주는 꼴이 된다
        victim = os.path.join(env.proj, "victim.txt")
        if os.path.lexists(ignore):
            os.remove(ignore)
        os.symlink(victim, ignore)
        at(env.proj, mark)
        check("심볼릭 링크는 따라가지 않는다", False, os.path.exists(victim))
        if os.path.lexists(ignore):
            os.remove(ignore)

        # git 워크트리 밖에서는 .gitignore 가 할 일이 없다 — 순수 부작용이므로 쓰지 않는다
        outside = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(outside, ".claude", "hangul-staging"), exist_ok=True)
            at(outside, {"issueIdOrKey": "K", "fields": {"description": BASE}})
            check("워크트리 밖에는 안 쓴다", False, os.path.exists(
                os.path.join(outside, ".claude", "hangul-staging", ".gitignore")))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

        # 사용자가 직접 지정한 폴더의 git 정책까지 대신 정하지 않는다
        at(env.proj, mark, staging=env.staging)
        check("HANGUL_STAGING 폴더에는 안 쓴다", False, os.path.exists(ignore))

        # 워크트리 **안**인데 폴더가 없는 경우라야 「만들지 않는다」가 검사된다
        empty = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(empty, ".git"), exist_ok=True)
            at(empty, {"issueIdOrKey": "K", "fields": {"description": BASE}})
            check("없는 staging 폴더를 만들지 않는다", False,
                  os.path.exists(os.path.join(empty, ".claude")))
        finally:
            shutil.rmtree(empty, ignore_errors=True)

        # --- 후보 자격은 홈 폴백에도 똑같이 적용된다 ---
        def plant(root, mode=None, link_to=None):
            """<root>/.claude/hangul-staging/current/K.md 를 심는다. 만든 staging 경로를 준다."""
            st = os.path.join(root, ".claude", "hangul-staging")
            if link_to:
                os.makedirs(os.path.join(link_to, "current"), exist_ok=True)
                with open(os.path.join(link_to, "current", "K.md"), "w", encoding="utf-8") as fh:
                    fh.write(BASE + "\n")
                os.makedirs(os.path.dirname(st), exist_ok=True)
                os.symlink(link_to, st)
                return st
            os.makedirs(os.path.join(st, "current"), exist_ok=True)
            with open(os.path.join(st, "current", "K.md"), "w", encoding="utf-8") as fh:
                fh.write(BASE + "\n")
            if mode is not None:
                os.chmod(st, mode)
            return st

        # 홈 폴백만 검사를 건너뛰면, 늘 존재하는 그 전역 폴더가 무검증 통로가 된다
        alt_home = tempfile.mkdtemp()
        loose = tempfile.mkdtemp()
        try:
            plant(alt_home, mode=0o777)
            code, msg = at(loose, mark, home=alt_home)
            check("홈 폴백도 자격 검사를 받는다", BLOCK, code)
            check("  사유가 권한이라고 나온다", True, "남이 쓸 수 있음" in msg)
        finally:
            shutil.rmtree(alt_home, ignore_errors=True)
            shutil.rmtree(loose, ignore_errors=True)

        # macOS staff 그룹은 로컬 계정 전원이다 — 그룹 쓰기도 world-writable 과 같다
        grp_home = tempfile.mkdtemp(); grp_cwd = tempfile.mkdtemp()
        try:
            plant(grp_home, mode=0o775)
            check("그룹 쓰기 폴더도 건너뛴다", BLOCK, at(grp_cwd, mark, home=grp_home)[0])
        finally:
            shutil.rmtree(grp_home, ignore_errors=True)
            shutil.rmtree(grp_cwd, ignore_errors=True)

        # staging 폴더 **자체**가 링크면 O_NOFOLLOW 가 못 지킨다 (마지막 요소만 지키므로)
        lnk_root = tempfile.mkdtemp(); victim_dir = tempfile.mkdtemp()
        try:
            mkgit(lnk_root)
            plant(lnk_root, link_to=victim_dir)
            code, msg = at(lnk_root, mark)
            check("staging 폴더가 링크면 안 쓴다", BLOCK, code)
            check("  사유가 링크라고 나온다", True, "심볼릭 링크" in msg)
            check("  링크 대상에 .gitignore 를 심지 않는다", False,
                  os.path.exists(os.path.join(victim_dir, ".gitignore")))
        finally:
            shutil.rmtree(lnk_root, ignore_errors=True)
            shutil.rmtree(victim_dir, ignore_errors=True)

        # 기준본 읽기에도 마커와 같은 봉쇄가 걸려야 한다 — 링크로 폴더 밖을 끌어오면 안 된다
        esc_root = tempfile.mkdtemp(); esc_out = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(esc_root, ".git"), exist_ok=True)
            cur = os.path.join(esc_root, ".claude", "hangul-staging", "current")
            os.makedirs(cur, exist_ok=True)
            outside = os.path.join(esc_out, "evil.md")
            with open(outside, "w", encoding="utf-8") as fh:
                fh.write(BASE + "\n")
            os.symlink(outside, os.path.join(cur, "K.md"))
            check("기준본 읽기도 폴더 밖 링크를 거부한다", BLOCK, at(esc_root, {
                "issueIdOrKey": "K", "fields": {"description": BASE}})[0])
        finally:
            shutil.rmtree(esc_root, ignore_errors=True)
            shutil.rmtree(esc_out, ignore_errors=True)

        # 이미 올리기로 하고 추적 중인 폴더에 * 를 심으면 새 기준본이 조용히 커밋에서 빠진다.
        # 인덱스에만 있어도(=add -A) 다음 커밋에 그대로 들어가므로 같이 막는다 — 「커밋된 것만
        # 본다(ls-tree HEAD)」로 좁히면 이 계약이 깨진다.
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        for label, commit in (("인덱스에만 있어도", False), ("커밋된 폴더에는", True)):
            tracked = tempfile.mkdtemp()
            try:
                mkgit(tracked)
                st = plant(tracked)
                subprocess.run(["git", "-C", tracked, "add", "-A"], **quiet)
                if commit:
                    subprocess.run(["git", "-C", tracked, "-c", "user.email=t@t",
                                    "-c", "user.name=t", "commit", "-qm", "x"], **quiet)
                at(tracked, mark)
                check(f"{label} 안 쓴다", False, os.path.exists(os.path.join(st, ".gitignore")))
            finally:
                shutil.rmtree(tracked, ignore_errors=True)

        # 커밋이 하나도 없는 저장소(unborn HEAD)는 추적 중인 것이 없으므로 써야 한다.
        # ls-tree HEAD 로 바꾸면 rc=128 이 되어 여기서 걸린다.
        unborn = tempfile.mkdtemp()
        try:
            mkgit(unborn); st = plant(unborn)
            at(unborn, mark)
            check("커밋 없는 저장소에는 쓴다", True, os.path.exists(os.path.join(st, ".gitignore")))
        finally:
            shutil.rmtree(unborn, ignore_errors=True)

        # git 이 대답을 못 하면 「모른다」다 — 빈 출력을 「추적 없음」으로 읽으면 커밋된
        # 기준본이 있는 저장소에도 심게 된다(실측으로 재현했던 fail-open)
        broken = tempfile.mkdtemp()
        try:
            mkgit(broken); st = plant(broken)
            with open(os.path.join(broken, ".git", "index"), "w") as fh:
                fh.write("garbage")
            at(broken, mark)
            check("git 이 실패하면 안 쓴다", False, os.path.exists(os.path.join(st, ".gitignore")))
        finally:
            shutil.rmtree(broken, ignore_errors=True)

        # 홈이 git 저장소인 사람(dotfiles)의 전역 폴백 폴더까지 훅이 정할 일은 아니다
        gh = tempfile.mkdtemp()
        try:
            mkgit(gh); st = plant(gh)
            at(gh, mark, home=gh)
            check("홈 저장소에는 안 쓴다", False, os.path.exists(os.path.join(st, ".gitignore")))
        finally:
            shutil.rmtree(gh, ignore_errors=True)

        # 안 쓰기로 했으면 조용히 넘어가지 말고 세션에 한 번 알린다
        told = tempfile.mkdtemp()
        try:
            mkgit(told); plant(told)
            subprocess.run(["git", "-C", told, "add", "-A"], **quiet)
            plain = {"issueIdOrKey": "K", "fields": {"description": "plain ascii body"}}
            first = at_raw(told, plain, session="N1")
            check("추적 중이면 알린다", True, "추적 중" in first.stdout)
            check("  차단하지는 않는다", PASS, first.returncode)
            # ⚠️ 치환할 것이 없는데 permissionDecision 을 실으면 그 호출의 권한 프롬프트가
            #    자동 승인된다 — 알리려다 사용자의 확인 절차를 없애는 꼴이 된다
            check("  알림만 낼 때 permissionDecision 을 싣지 않는다", False,
                  "permissionDecision" in first.stdout)
            again = at_raw(told, plain, session="N1", fresh=False)
            check("  같은 세션에서 두 번 알리지 않는다", False, "추적 중" in again.stdout)
            # 반대로 치환이 있는 호출에서는 allow 가 반드시 함께 나가야 한다(치환의 전제)
            withmark = at_raw(told, mark, session="N2", fresh=False)
            check("  치환 호출에는 allow 가 나간다", True,
                  '"permissionDecision": "allow"' in withmark.stdout)
            # 판정을 캐시하므로 세션이 달라도 git 을 다시 묻지 않는다(그래서 알림도 안 뜬다)
            check("  판정은 캐시된다", False, "추적 중" in withmark.stdout)
        finally:
            shutil.rmtree(told, ignore_errors=True)

        # 게이트 밖 MCP 호출에서도 정리는 돈다 — 게이트 쓰기가 0회인 세션이 실측 36 중 21이었다
        outside_gate = tempfile.mkdtemp()
        try:
            mkgit(outside_gate); st = plant(outside_gate)
            at(outside_gate, {"query": "x"}, tool="mcp__plugin_Notion_notion__notion-fetch")
            check("게이트 밖 호출에서도 .gitignore 를 놓는다", True,
                  os.path.exists(os.path.join(st, ".gitignore")))
        finally:
            shutil.rmtree(outside_gate, ignore_errors=True)

    finally:
        env.cleanup()


def check_configs():
    """매니페스트와 어댑터 설정이 코드·규격과 어긋나지 않는지."""
    import re as _re
    plugin = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
    market = json.load(open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")))
    check("plugin.json 필수 필드", True,
          all(k in plugin for k in ("name", "description", "version")))
    check("marketplace name 이 kebab-case", True,
          bool(_re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", market["name"])))
    reserved = {"claude-code-marketplace", "claude-code-plugins", "claude-plugins-official",
                "claude-plugins-community", "claude-community", "anthropic-marketplace",
                "anthropic-plugins", "agent-skills", "first-party-plugins",
                "org", "org-provisioned", "unknown"}
    check("marketplace name 이 예약어 아님", True, market["name"] not in reserved)
    check("marketplace 가 이 플러그인을 담음", True,
          any(p["name"] == plugin["name"] for p in market["plugins"]))

    hooks = json.load(open(os.path.join(ROOT, "hooks", "hooks.json")))
    entry = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    check("훅이 CLAUDE_PLUGIN_ROOT 를 쓴다", True, "${CLAUDE_PLUGIN_ROOT}" in entry)
    check("훅이 가리키는 스크립트가 존재", True,
          os.path.isfile(os.path.join(ROOT, "core", "hangul_corruption_guard.py")))

    events = {"cursor": "beforeMCPExecution", "windsurf": "pre_mcp_tool_use",
              "codex": "preToolUse"}
    for tool, event in events.items():
        path = os.path.join(ROOT, "adapters", tool, "hooks.json")
        conf = json.load(open(path))
        blob = json.dumps(conf, ensure_ascii=False)
        check(f"{tool} 어댑터 이벤트명", True, event in conf["hooks"])
        check(f"{tool} 어댑터가 코어를 가리킴", True,
              "core/hangul_corruption_guard.py" in blob)


run()
check_configs()
failed = [r for r in results if not r[1]]
for label, ok, expected, got in results:
    mark = "PASS" if ok else "FAIL"
    detail = "" if ok else f"  (기대 {expected}, 실제 {got})"
    print(f"  [{mark}] {label}{detail}")
print(f"\n{len(results) - len(failed)}/{len(results)} 통과")
sys.exit(1 if failed else 0)
