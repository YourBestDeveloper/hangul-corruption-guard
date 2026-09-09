#!/usr/bin/env python3
r"""
PreToolUse — 문서 저장 MCP 도구로 보내는 한글의 표류를 잡는다.

문제:
  긴 한글을 재생산할 때 자모 하나가 표류한다(합쳐→합쳤, 뜬→뜼, 힌→친).
  이스케이프로 쓰든 글자 그대로 쓰든 똑같이 발생하고, 바뀐 결과가 유효한
  한글이라 JSON 파싱도 서버 검증도 통과한다. 훅이 받는 값은 이미 디코드된
  상태라 "틀렸다"는 판정 자체가 불가능하다 — 의도를 모르기 때문이다.

해법:
  의도를 파일(기준본)로 물질화해 두고 대조한다. 다만 **막다른 길을 만들지
  않는 것**이 대조 정확도보다 중요하다. 그래서 판정을 셋으로 나눈다.

    기준본에 그대로 있다        -> 통과 (조용히)
    기준본의 어떤 줄과 매우 닮았다 -> 표류로 보고 차단 + 틀린 글자 지목
                                  (글자를 고치면 payload 가 바뀌어 다시 통과한다)
    닮은 줄이 없다 = 새 내용     -> 에코 후 확인 (같은 값 재전송 시 통과)

  마지막 경로가 항상 열려 있어야 한다. 기준본에 있을 수 없는 일회성 텍스트
  (새 문서·코멘트)가 영구 차단되면 훅이 작업을 불가능하게 만든다.
"""
import difflib
import time
import hashlib
import unicodedata
import json
import os
import re
import sys

MIN_ECHO_HANGUL = 8    # 이보다 짧은 새 줄은 에코로 확인받지 않는다 (라벨 수준의 소음)
MAX_DRIFT_CHARS = 3    # 짧은 줄에서 허용하는 최대 불일치. 긴 줄은 길이에 비례해 늘어난다
OLD_STR_RATIO = 0.85   # old_str 은 기존 본문을 그대로 옮긴 것이어야 한다. 이만큼 닮았는데
#                        정확히 다르면 손상이다 — new_str 과 달리 '새 내용' 이라는 여지가 없다


def drift_budget(length):
    """같은 길이의 두 줄이 몇 글자까지 달라도 '같은 줄이 손상된 것' 으로 볼지.

    절대값만 쓰면 손상이 심할수록 판정이 약해진다 — 200자 줄에서 5글자가 틀리면
    명백한 손상인데 문턱을 넘어 에코로 떨어진다. 반대로 10자 줄에서 4글자가 다르면
    애초에 다른 줄일 가능성이 크다. 그래서 길이에 비례시킨다.
    """
    return max(MAX_DRIFT_CHARS, length // 10)
#   자모 치환은 길이를 바꾸지 않는다(합쳐→합쳤). 문구 수정은 길이가 변한다.
#   길이 동일 + 소수 글자 차이 = 표류의 서명. 이 조건이 아니면 새 내용으로 보고
#   에코 경로로 보낸다 — 정상 수정을 표류로 오판해 원문을 되돌리게 하면 안 된다.
EDIT_RATIO = 0.60      # 이 이상 닮은 줄이 new_str 에 있으면 삭제가 아니라 수정이다
ECHO_CAP = 4000
STALE_HOURS = 6        # 기준본이 이보다 오래되면 한 번 확인을 받는다

# NFD(자모 분해) 도 세어야 한다 — [가-힣] 만 보면 분해형이 "한글 0자" 가 되어 훅이 통째로 꺼진다
HANGUL = re.compile(r"[가-힣\u1100-\u11FF\u3130-\u318F\uA960-\uA97F\uD7B0-\uD7FF]")
MARKUP = re.compile(r"(\*\*|__|[*_`~]|^[\s\-•\d.]+|\[([^\]]*)\]\([^)]*\))")
SPACES = re.compile(r"\s+")
# 파일 참조 마커 — 문자열 전체가 이 형태일 때만 파일 내용으로 치환한다.
# 본문 중간 삽입은 경계가 모호해 지원하지 않는다.
# @@hangul:<경로>@@            파일 전체
# @@hangul:<경로>#L12@@        12번째 줄       (1부터)
# @@hangul:<경로>#L12-L15@@    12~15번째 줄
# 줄 지정이 필요한 이유: old_str 처럼 기존 본문의 **조각**을 여러 개 보내야 할 때,
# 파일 전체만 넣을 수 있으면 조각마다 파일을 만들어야 한다. 그러면 결국 손으로
# 옮겨 적게 되고 — 그 순간 표류가 다시 들어온다.
MARKER = re.compile(r"^@@hangul:([^@#]+)(?:#L(\d+)(?:-L(\d+))?)?@@$")

CACHE = os.path.expanduser("~/.claude/hooks/.hangul-echo-cache")

# 문서 본문을 저장하는 도구만 검사한다. 이름 휴리스틱은 오검이 커서 쓰지 않는다
# (browseros·chrome-devtools 의 한글 폼 입력, 한글 법령 질의까지 걸린다).
GATED = (
    "notion-update-page", "notion-create-pages", "notion-create-comment",
    "notion-update-data-source", "notion-duplicate-page",
    "editJiraIssue", "createJiraIssue", "addCommentToJiraIssue",
    "createConfluencePage", "updateConfluencePage",
    "createConfluenceFooterComment", "createConfluenceInlineComment",
)


def normalize(line):
    """비교용 정규화 — 유니코드 합성형·들여쓰기·불릿·강조 마크업·연속 공백을 흡수한다."""
    text = unicodedata.normalize("NFC", line)
    text = MARKUP.sub(lambda m: m.group(2) or "", text)
    return SPACES.sub(" ", text).strip()


def hangul_len(text):
    return len(HANGUL.findall(unicodedata.normalize("NFC", text)))


def staging_dirs(cwd):
    override = os.environ.get("HANGUL_STAGING")
    if override:
        return [os.path.expanduser(override)]
    dirs = []
    if cwd:
        dirs.append(os.path.join(cwd, ".claude", "hangul-staging"))
    dirs.append(os.path.expanduser("~/.claude/hangul-staging"))
    return dirs


def doc_id(tool_input):
    """대상 문서 식별자 — 기준본을 문서에 묶는다.
    없으면 기준본이 '아무 문자열이나 통과시키는 만능 통행증' 이 된다."""
    keys = ("page_id", "issueIdOrKey", "pageId", "id")
    for key in keys:                                   # 최상위
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    for container in ("parent", "pages"):              # notion-create-pages 는 중첩이다
        node = tool_input.get(container)
        if isinstance(node, list):
            node = node[0] if node else None
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, str) and value:
                    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return None


def doc_lines(cwd, ident, role):
    """이 문서의 기준본 줄 목록. 역할이 둘로 갈린다.

    current/<문서id>.md — 서버에서 fetch 한 현재 본문. **내가 타이핑하지 않은 사본**이라
        표류 대조의 독립 기준이 된다. 안 바꾼 줄을 다시 타이핑했을 때 글자가 어긋나면
        여기서 잡힌다. 삭제 판정에는 쓰지 않는다 — 현재 본문에서 항목을 빼는 것은
        정당한 편집이기 때문이다.
    target/<문서id>.md  — 내가 의도한 최종 본문. 삭제·커버리지 판정의 근거다.
        (하위 호환: <문서id>.md 도 target 으로 본다)
    """
    if not ident:
        return []
    names = [os.path.join(role, f"{ident}.md")]
    if role == "target":
        names.append(f"{ident}.md")
    for base in staging_dirs(cwd):
      for name in names:
        path = os.path.join(base, name)
        if os.path.isfile(path):
            try:
                text = open(path, encoding="utf-8").read()
            except Exception:
                continue
            return [(normalize(l), l.strip()) for l in text.splitlines()
                    if hangul_len(l)]
    return []


def staged_lines(cwd):
    """기준본의 한글 줄 목록 — 정규화본과 원본을 함께 들고 있는다."""
    out = []
    for base in staging_dirs(cwd):
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for name in files:
                try:
                    text = open(os.path.join(root, name), encoding="utf-8").read()
                except Exception:
                    continue
                for line in text.splitlines():
                    if hangul_len(line):
                        out.append((normalize(line), line.strip()))
    return out


def collect(tool_input):
    """검사 대상 (경로, 값, old_str 여부). 길이 판정은 호출 전체 합계로 한다 —
    문자열마다 재면 19자 조각으로 나눠 보내는 것만으로 훅이 통째로 꺼진다."""
    found = []

    def walk(node, path, in_old):
        if isinstance(node, str):
            if HANGUL.search(unicodedata.normalize("NFC", node)):
                found.append((path, node, in_old))
        elif isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and HANGUL.search(unicodedata.normalize("NFC", key)):
                    found.append((f"{path}.<키>", key, False))
                walk(value, f"{path}.{key}", in_old or key == "old_str")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]", in_old)

    walk(tool_input, "tool_input", False)
    return found


def resolve_marker(cwd, rel, first=None, last=None):
    """staging 안의 파일만 허용한다. 경로 탈출은 거부.
    줄 번호가 주어지면 그 구간만 돌려준다(1부터, 끝 줄 포함)."""
    for base in staging_dirs(cwd):
        root = os.path.realpath(base)
        path = os.path.realpath(os.path.join(root, rel))
        if not ((path == root or path.startswith(root + os.sep)) and os.path.isfile(path)):
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except Exception:
            return path, None, "read"
        if first is None:
            return path, text, None
        lines = text.splitlines()
        start = first - 1
        end = (last if last is not None else first)
        if start < 0 or end > len(lines) or start >= end:
            # 범위 밖 — 조용히 통과시키지 않는다. 사유를 구분해야 진단이 엉뚱한 곳을 짚지 않는다
            return path, None, f"range:{len(lines)}"
        return path, "\n".join(lines[start:end]), None
    return None, None, "path"


def expand_markers(node, cwd, misses, used=None, ranged=None, path="tool_input"):
    """@@hangul:경로@@ 를 파일 내용으로 치환한 사본을 돌려준다.

    이유는 두 가지다. (1) 본문이 파라미터 경로에서는 모델 토큰을 거치지 않으므로
    그 구간의 재전송 표류가 사라진다(기준본을 쓰는 단계는 남는다). (2) 같은 본문을
    파일과 파라미터에 두 번 쓰지 않으므로 토큰이 준다(이스케이프 대비 실측 80%,
    리터럴 대비로는 거의 본전 — 이득은 반복 전송에서 난다).
    """
    if isinstance(node, str):
        hit = MARKER.match(node.strip())
        if not hit:
            return node, False
        rel = hit.group(1).strip()
        first = int(hit.group(2)) if hit.group(2) else None
        last = int(hit.group(3)) if hit.group(3) else None
        found, text, reason = resolve_marker(cwd, rel, first, last)
        span = "" if first is None else f"#L{first}" + (f"-L{last}" if last else "")
        if text is None:
            misses.append((rel + span, reason, found))
            return node, False
        if used is not None:
            used.append((found, os.path.getmtime(found), len(text)))
        if ranged is not None and first is not None:
            # 통짜 교체 필드에 줄 범위를 쓰면 나머지 본문이 조용히 삭제된다 — main 에서 막는다
            ranged.append((path, rel + span))
        return text.rstrip("\n"), True
    if isinstance(node, dict):
        out, changed = {}, False
        for key, value in node.items():
            out[key], hit = expand_markers(value, cwd, misses, used, ranged, f"{path}.{key}")
            changed = changed or hit
        return out, changed
    if isinstance(node, list):
        out, changed = [], False
        for index, value in enumerate(node):
            item, hit = expand_markers(value, cwd, misses, used, ranged, f"{path}[{index}]")
            out.append(item); changed = changed or hit
        return out, changed
    return node, False


def leftover_markers(node):
    """치환되지 않고 남은 마커 문자열. 이대로 나가면 본문 대신 마커가 저장된다.
    MARKER 는 ^...$ 앵커라 형태가 조금만 어긋나도 치환되지 않고 조용히 통과한다."""
    out = []
    if isinstance(node, str):
        if "@@hangul:" in node:
            out.append(node.strip()[:120])
    elif isinstance(node, dict):
        for value in node.values():
            out += leftover_markers(value)
    elif isinstance(node, list):
        for value in node:
            out += leftover_markers(value)
    return out


def emit_updated(tool_input):
    """Claude Code 형식. Cursor 는 호환 레이어가 updatedInput -> updated_input 으로
    매핑하고, Codex 는 같은 형식을 쓴다(단 permissionDecision:allow 가 반드시 함께)."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": tool_input,
    }}, ensure_ascii=False))
    sys.exit(0)


def diff_marks(reference, sent):
    """길이가 달라도 어긋난 구간을 짚는다. zip 으로는 삽입·삭제 뒤가 전부 밀린다."""
    marks = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, reference, sent).get_opcodes():
        if tag == "equal":
            continue
        before, after = reference[i1:i2], sent[j1:j2]
        marks.append(f"    위치 {i1}: 기준 「{before or '(없음)'}」  →  보낸 값 「{after or '(없음)'}」")
    return marks


def deny(message):
    sys.stderr.write(message)
    sys.exit(2)


def extract_call(data):
    """도구별 입력 스키마를 (도구명, 인자, cwd, 세션) 으로 정규화한다.

    Claude Code / Codex CLI / Cursor preToolUse : {tool_name, tool_input, cwd, session_id}
    Cursor beforeMCPExecution                   : 위와 같고 mcp_server_name 이 더 붙는다
    Windsurf pre_mcp_tool_use                   : {agent_action_name, trajectory_id,
                                                  tool_info:{mcp_server_name,
                                                             mcp_tool_name,
                                                             mcp_tool_arguments}}
    """
    info = data.get("tool_info")
    if isinstance(info, dict) and "mcp_tool_name" in info:      # Windsurf
        server = info.get("mcp_server_name") or ""
        name = info.get("mcp_tool_name") or ""
        return (f"{server}__{name}" if server else name,
                info.get("mcp_tool_arguments") or {},
                data.get("cwd") or info.get("working_directory") or os.getcwd(),
                data.get("trajectory_id") or "nosession")

    name = data.get("tool_name") or ""
    server = data.get("mcp_server_name")                         # Cursor beforeMCPExecution
    if server and server not in name:
        name = f"{server}__{name}"
    return (name,
            data.get("tool_input") or {},
            data.get("cwd") or os.getcwd(),
            data.get("session_id") or "nosession")


def match_survivors(expected, payload):
    """expected 의 각 줄이 payload 에서 살아남았는지 **일대일로** 짝지어 확인한다.

    payload 줄 하나가 여러 expected 줄의 알리바이가 되면 안 된다. 비슷한 항목이
    늘어선 문서(정책 1, 정책 2 …)에서 한 줄을 지우면, 남은 형제 줄이 지워진 줄과
    닮았다는 이유로 삭제가 가려진다. 실측: 400줄 문서에서 1줄을 지워도 검출 실패.

    그래서 한 번 짝지어진 payload 줄은 소진하고, 짝을 못 찾은 expected 줄만 돌려준다.
    """
    used = [False] * len(payload)

    # 1단계 — 그대로 살아 있는 줄부터 소진한다. 이걸 먼저 하지 않으면 탐욕 매칭이
    # 연쇄로 밀려, 실제로 지워진 줄 대신 끝줄이 사라진 것으로 보고된다.
    remaining = []
    for norm, original in expected:
        matched = False
        for index, candidate in enumerate(payload):
            if not used[index] and candidate == norm:
                used[index] = True
                matched = True
                break
        if not matched:
            remaining.append((norm, original))

    # 2단계 — 남은 줄만 근사 매칭. 같은 자리를 고쳐 쓴 것은 삭제가 아니다
    lost = []
    for norm, original in remaining:
        best_index, best_ratio = -1, 0.0
        for index, candidate in enumerate(payload):
            if used[index]:
                continue
            ratio = difflib.SequenceMatcher(None, norm, candidate).ratio()
            if ratio > best_ratio:
                best_index, best_ratio = index, ratio
        if best_ratio >= EDIT_RATIO:
            used[best_index] = True
            continue
        lost.append(original)
    return lost


def check_silent_deletion(tool_input, staged):
    """old_str 이 삼킨 항목이 new_str 에 없고 기준본에는 남아 있으면 사라진다."""
    staged_norm = {n for n, _o in staged}
    lost = []
    for update in tool_input.get("content_updates") or []:
        if not isinstance(update, dict):
            continue
        old_value, new_value = update.get("old_str") or "", update.get("new_str") or ""
        if not old_value:
            continue
        new_norm = [normalize(l) for l in new_value.splitlines() if l.strip()]
        expected = [(normalize(l), l.strip()) for l in old_value.splitlines()
                    if hangul_len(l) and normalize(l) in staged_norm]
        lost.extend(match_survivors(expected, new_norm))
    return lost


def full_body_values(tool_name, tool_input):
    """본문을 통째로 갈아끼우는 값들 — 여기서 빠진 줄은 그대로 삭제된다.
    Jira 사고 6건이 전부 이 경로(description 통짜 교체)였다."""
    out = []
    if tool_input.get("command") in ("replace_content",):
        if tool_input.get("new_str"):
            out.append(("tool_input.new_str", tool_input["new_str"]))
    fields = tool_input.get("fields")
    if isinstance(fields, dict) and isinstance(fields.get("description"), str):
        out.append(("tool_input.fields.description", fields["description"]))
    if isinstance(tool_input.get("body"), str):        # Confluence
        out.append(("tool_input.body", tool_input["body"]))
    return out


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if os.environ.get("HANGUL_GUARD", "").lower() in ("off", "0", "false"):
        sys.exit(0)   # 오탐으로 막혔을 때 빠져나갈 문. 없으면 작업이 멈춘다

    tool_name, tool_input, cwd, session = extract_call(data)
    if not any(marker in tool_name for marker in GATED):
        # 화이트리스트 밖에서는 치환이 돌지 않는다. 마커를 보냈다면 그대로 저장되므로 막는다
        stray = leftover_markers(tool_input)
        if stray:
            deny(f"{tool_name} 은 이 훅의 검사 대상이 아니라 마커가 치환되지 않습니다:\n"
                 + "\n".join(f"  {v}" for v in stray)
                 + "\n\n이대로 보내면 본문 대신 마커 문자열이 저장됩니다."
                   " 본문을 직접 보내세요.\n")
        sys.exit(0)

    # 마커 치환을 먼저 한다 — 이후 모든 검사는 치환된 본문에 대해 돌아야
    # 치환이 검사를 우회하는 구멍이 되지 않는다
    misses, used_files, ranged = [], [], []
    expanded, did_expand = expand_markers(tool_input, cwd, misses, used_files, ranged)
    for spec, reason, found in misses:
        if reason and reason.startswith("range:"):
            deny(f"마커의 줄 범위가 파일을 벗어납니다: @@hangul:{spec}@@\n"
                 f"  {found} 는 {reason.split(':')[1]}줄입니다.\n\n"
                 "줄 번호는 1부터이고 끝 줄을 포함합니다. `grep -n` 으로 확인하세요.\n")
        if reason == "read":
            deny(f"기준본 파일을 읽지 못했습니다: {found}\n"
                 "  UTF-8 로 저장돼 있는지 확인하세요.\n")
    if misses:
        deny("기준본 파일을 찾을 수 없습니다: "
             + ", ".join(f"@@hangul:{m}@@" for m, _r, _f in misses)
             + f"\n  찾은 위치: {staging_dirs(cwd)[0]}\n"
               "  경로는 staging 폴더 기준 상대경로여야 하고, 폴더 밖은 거부합니다.\n")

    # 형태가 어긋나 치환되지 않은 마커. 그대로 나가면 본문 대신 마커가 저장된다
    stray = leftover_markers(expanded)
    if stray:
        deny("치환되지 않은 마커가 남아 있습니다:\n"
             + "\n".join(f"  {v}" for v in stray)
             + "\n\n마커는 문자열 **전체**가 @@hangul:<경로>@@ 형태일 때만 치환합니다.\n"
               "  줄 지정은 #L12 · #L12-L15 형태입니다 — #L12-15 · #12 는 인식하지 않습니다.\n"
               "  앞뒤에 다른 글자가 붙거나 본문 중간에 끼워 넣은 것도 치환하지 않습니다.\n"
               "이대로 보내면 본문 대신 마커 문자열이 저장됩니다.\n")

    # 통짜 교체 필드에 줄 범위 마커를 쓰면 지정한 줄만 남고 나머지가 조용히 사라진다
    if ranged:
        whole = {path for path, _v in full_body_values(tool_name, expanded)}
        for path, spec in ranged:
            if path in whole:
                deny(f"[{path}] 본문을 통째로 바꾸는 필드에는 줄 지정 마커를 쓸 수 없습니다"
                     f" (@@hangul:{spec}@@).\n\n"
                     "지정한 줄만 남고 나머지 본문은 에러 없이 삭제됩니다.\n"
                     "보낼 본문 전체를 파일 하나로 만들어 파일 전체 마커로 보내세요"
                     " — @@hangul:target/<문서 id>.md@@\n")
    # 치환 자체를 지원하지 않는 호스트라면 다른 진단보다 먼저 알려야 한다 —
    # stale 을 먼저 띄우면 파일을 갱신하고 나서야 진짜 사유를 보게 된다
    if did_expand and isinstance(data.get("tool_info"), dict):
        deny("이 도구는 훅이 입력을 치환하는 기능을 지원하지 않습니다"
             " (Windsurf 는 허용/차단만 가능).\n마커 대신 본문을 직접 보내세요.\n")

    # 마커가 가리키는 파일이 오래됐으면 한 번 확인을 받는다.
    # 훅은 서버 본문을 볼 수 없어 "내용이 최신인가" 는 판정할 수 없다 — 나이만 알 수 있다.
    if used_files:
        stale = [(p, (time.time() - m) / 3600, n) for p, m, n in used_files
                 if (time.time() - m) / 3600 >= STALE_HOURS]
        if stale:
            digest = hashlib.sha256(
                ("stale\x00" + session + "\x00"
                 + "\x00".join(f"{p}:{m}" for p, m, _n in used_files)).encode("utf-8")
            ).hexdigest()[:32]
            marker_path = os.path.join(CACHE, digest)
            if not os.path.exists(marker_path):
                os.makedirs(CACHE, exist_ok=True)
                open(marker_path, "w").close()
                deny("마커가 가리키는 기준본이 오래됐습니다:\n"
                     + "\n".join(f"  {p}\n    {h:.0f}시간 전 · {n:,}자" for p, h, n in stale)
                     + "\n\n그 사이 서버 본문이 바뀌었다면 남의 수정을 덮어씁니다."
                       " 훅은 서버를 볼 수 없어 내용이 최신인지는 판정하지 못합니다.\n"
                       "다시 fetch 해서 갱신하거나, 그대로 보낼 것이면 같은 호출을 한 번 더 하세요.\n")

    tool_input = expanded

    targets = collect(tool_input)
    if not targets:
        emit_updated(tool_input) if did_expand else sys.exit(0)

    ident = doc_id(tool_input)
    target = doc_lines(cwd, ident, "target")     # 내 의도 — 삭제·커버리지 판정
    current = doc_lines(cwd, ident, "current")   # 서버 fetch본 — 표류 대조의 독립 기준
    known = current + target                     # 이 문서에서 이미 확인된 줄
    known_norm = {n for n, _o in known}
    # 표류 대조는 **이 문서의** 기준본으로만 한다. 폴더 전체로 넓히면 무관한 문서의
    # 줄과 우연히 닮았다는 이유로 차단되고, 오류 메시지가 남의 문장을 정답으로 내민다.
    reference = known

    # (1a) 통짜 교체 커버리지 — 이 문서 기준본의 줄이 페이로드에 하나도 없으면 삭제된다
    if target:
        for path, value in full_body_values(tool_name, tool_input):
            payload = [normalize(l) for l in value.splitlines() if l.strip()]
            missing = match_survivors(target, payload)
            if missing:
                deny(f"[{path}] 본문을 통째로 바꾸는데 기준본의 항목이 빠졌습니다:\n"
                     + "\n".join(f"  - {l}" for l in missing)
                     + "\n\n이대로 보내면 이 항목들이 사라집니다."
                       " 되돌려 놓거나, 정말 지울 생각이면 기준본에서 먼저 지우세요.\n")

    # (1b) 조용한 삭제 — 기준본이 있을 때만 판정할 수 있다
    # 삭제 판정은 target 만 — current 에서 항목을 빼는 것은 정당한 편집이다
    if target:
        lost = check_silent_deletion(tool_input, target)
        if lost:
            deny("old_str 이 삼킨 항목이 new_str 에 없는데, 기준본에는 남아 있습니다:\n"
                 + "\n".join(f"  - {l}" for l in lost)
                 + "\n\n이대로 보내면 이 항목들이 에러 없이 삭제됩니다.\n"
                   "되돌려 놓거나, 정말 지울 생각이면 기준본에서 먼저 지우세요.\n")

    # (2) 줄 단위 판정 — 기준본과 같으면 통과, 매우 닮았는데 다르면 표류, 그 외는 새 내용
    current_norm = [(n, o) for n, o in current]

    def check_old_str(path, line, norm):
        """old_str 은 서버 본문 사본(current/)과 정확히 일치해야 한다."""
        if not current_norm or norm in {n for n, _o in current_norm}:
            return
        best = max(current_norm,
                   key=lambda c: difflib.SequenceMatcher(None, c[0], norm).ratio())
        ratio = difflib.SequenceMatcher(None, best[0], norm).ratio()
        if ratio < OLD_STR_RATIO:
            return
        deny(f"[{path}] old_str 이 서버 본문 사본과 다릅니다 (유사도 {ratio:.0%}).\n\n"
             f"  기준본:  {best[1]}\n  보낸 값: {line.strip()}\n\n"
             + "\n".join(diff_marks(best[0], norm))
             + "\n\nold_str 은 페이지에 있는 그대로여야 합니다 — 옮기는 중에 글자가 어긋난 것으로"
               " 보입니다.\n"
               f"옮겨 적지 말고 줄 지정 마커로 보내세요: @@hangul:current/{ident}.md#L<줄>@@\n"
               "  (줄 번호는 그 파일에서 `grep -n` 으로 확인합니다)\n"
               "마커를 쓸 수 없으면 기준본 쪽 문자열을 그대로 보내세요."
               " 서버 본문이 그 사이 바뀐 것이라면 current/ 를 다시 받아 두세요.\n")

    new_content = []
    for path, value, is_old in targets:
        for line in value.splitlines() or [value]:
            # 표류 검사는 짧은 줄에도 돌린다 — 조각내서 보내는 것만으로 우회되면 안 된다.
            # 기준본에 거의 같은 줄이 있을 때만 발화하므로 새 문장에는 소음이 없다.
            if not hangul_len(line):
                continue
            norm = normalize(line)
            if is_old:
                check_old_str(path, line, norm)
            # 조용히 통과시키는 것은 **이 문서의 기준본**에 있는 줄뿐이다.
            # 다른 파일은 표류 대조용으로만 쓴다 — 아무 파일에나 있으면 통과시키면
            # 한 번 스테이징한 문장이 모든 문서에 대한 영구 통행증이 된다.
            if norm in known_norm:
                continue
            for cand_norm, cand_orig in reference:
                if len(cand_norm) != len(norm):
                    continue
                diffs = [(i, a, b) for i, (a, b) in enumerate(zip(cand_norm, norm)) if a != b]
                if not diffs or len(diffs) > drift_budget(len(norm)):
                    continue
                marks = [f"    위치 {i}: 기준 '{a}' U+{ord(a):04X}  →  보낸 값 '{b}' U+{ord(b):04X}"
                         for i, a, b in diffs]
                deny(f"[{path}] 기준본과 길이가 같은데 {len(diffs)}글자가 다릅니다 — 표류로 보입니다.\n\n"
                     f"  기준본:  {cand_orig}\n  보낸 값: {line.strip()}\n\n"
                     + "\n".join(marks)
                     + "\n\n이 값 전체가 기준본 파일의 한 구간과 같아질 수 있으면, 옮겨 적지 말고"
                       " 마커로 보내세요\n"
                       "  (파일 전체 @@hangul:current/<문서 id>.md@@"
                       " · 줄 지정 @@hangul:current/<문서 id>.md#L12-L15@@).\n"
                       "그렇지 않으면 기준본 쪽 문자열을 그대로 보내세요."
                       " 의도한 수정이라면 기준본을 먼저 고치세요.\n"
                       "  ⚠️ 길이가 같은 정상 수정(가능하다→불가하다)도 여기 걸립니다."
                       " 오탐이면 기준본을 고치거나 HANGUL_GUARD=off 로 끄세요.\n")
            # 에코는 충분히 긴 줄만 — 짧은 라벨까지 확인시키면 소음이 된다
            # old_str 은 기존 본문이라 기준본에 없는 게 정상이므로 에코에서 뺀다
            if not is_old and hangul_len(line) >= MIN_ECHO_HANGUL:
                new_content.append((path, line.strip()))

    if not new_content:
        # 전부 기준본과 일치
        emit_updated(tool_input) if did_expand else sys.exit(0)

    # (3) 새 내용 — 에코 후 확인. 이 경로는 항상 열려 있어야 한다
    # 승인은 (세션 · 도구 · 문서 · 값) 에 묶는다 — 한 문서에서 받은 승인이
    # 다른 문서·다른 도구로 전용되면 안 된다
    digest = hashlib.sha256(
        "\x00".join([session, tool_name, ident or "-"]
                    + [v for _p, v in new_content]).encode("utf-8")
    ).hexdigest()[:32]
    marker = os.path.join(CACHE, digest)
    if os.path.exists(marker):
        # 치환이 있었으면 승인 히트에서도 updatedInput 을 내야 한다 —
        # 여기서 그냥 exit 0 하면 호스트가 원본(=마커 문자열)을 그대로 전송한다
        emit_updated(tool_input) if did_expand else sys.exit(0)

    body = "\n".join(f"  {v}" for _p, v in new_content)
    note = ""
    if len(body) > ECHO_CAP:
        # 잘라서라도 보여주고 재전송은 열어 둔다. 여기서 마커를 만들지 않으면
        # 긴 새 본문이 영구 차단돼 「막다른 길을 만들지 않는다」는 원칙이 깨진다.
        head, tail = body[: ECHO_CAP // 2], body[-ECHO_CAP // 2 :]
        note = (f"\n\n⚠️ {len(body)}자라 가운데를 생략했습니다. 전체를 확인하려면"
                f" {staging_dirs(cwd)[0]} 에 기준본을 써 두세요 — 그러면 문자 단위로 대조합니다.\n")
        body = head + "\n\n  … (가운데 생략) …\n\n" + tail

    os.makedirs(CACHE, exist_ok=True)
    open(marker, "w").close()

    # 기존 문서를 고치는데 current/ 가 비어 있으면, 안 바꾸고 그대로 옮긴 줄까지
    # 전부 "새 한글" 로 걸린다. 그 경우 서버 본문을 받아 두는 것이 답이다.
    hint = ""
    if ident and not current:
        hint = (f"\n기존 문서를 고치는 중이고 위 줄이 **원래 본문을 그대로 옮긴 것**이라면,\n"
                f"서버 본문을 받아 {os.path.join(staging_dirs(cwd)[0], 'current', ident + '.md')} 에\n"
                f"저장해 두세요. 그러면 이 확인이 사라지고 문자 단위로 대조됩니다.\n")

    deny("기준본에 없는 새 한글입니다. 긴 한글은 재생산 과정에서 자모 하나가 "
         "표류해도(뜬→뜼) 어떤 검사에도 안 걸리므로 저장 전에 한 번 봐야 합니다.\n"
         "이상 없으면 같은 값을 그대로 다시 보내면 통과합니다 — 다만 그 재전송도 재생산이라\n"
         "표류가 한 번 더 들어올 수 있고, 대조할 기준본이 없어 기계적으로는 검출되지 않습니다.\n"
         "확실하게 보내려면 이 본문을 staging 폴더의 파일로 쓰고 마커로 보내세요"
         " — 재생산이 0회가 됩니다.\n\n" + body + note + "\n" + hint)


main()
