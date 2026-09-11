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
import stat
import subprocess
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


MAX_WALK = 128         # 상향 탐색의 안전판 — 실제 경로 깊이로는 닿지 않는 값이어야 한다.
#                        25 로 두면 깊은 저장소에서 사슬이 잘려 후보가 cwd 하나로 붕괴하고,
#                        「저장소 밖」과 구분이 안 돼 진단이 엉뚱한 곳을 짚는다


def project_dirs(cwd):
    """cwd 에서 **가장 바깥 저장소 루트까지**. 저장소 밖이면 cwd 한 곳뿐이다.

    양쪽으로 다 틀릴 수 있어서 둘 다 막는다.

    - **너무 일찍 멈추면** 서브모듈·vendor 처럼 자기 `.git` 을 가진 하위 저장소 안에서
      상위 프로젝트의 기준본이 후보에서 빠진다 — 고치려던 「마커 전량 거부」가 거기서는
      그대로 남는다. 그래서 처음 만난 `.git` 에서 멈추지 않고 바깥 루트까지 올라간다.
    - **너무 멀리 가면** 루트가 없는 cwd 에서 `/` 까지 올라가 `/tmp/.claude/hangul-staging`
      같은 **남이 쓸 수 있는 폴더**가 기준본 후보가 된다. 마커가 남의 파일 내용으로 치환돼
      그대로 저장되고 대조 기준까지 그 파일이 된다 — 검사가 뒤집힌다. 그래서 저장소를
      못 찾으면 상향 탐색 자체를 하지 않는다.

    `.claude` 를 루트 표식으로 치지 않는 이유: 기준본 폴더가 있다는 것은 곧 `.claude` 가
    있다는 뜻이라, 그것을 표식으로 삼으면 「심어 둔 폴더가 스스로 자기 자리를 정당화한다」.
    """
    home = os.path.realpath(os.path.expanduser("~"))
    chain, path = [], os.path.realpath(cwd)
    while len(chain) < MAX_WALK:
        chain.append(path)
        if path == home:
            break                      # 홈 위로는 올라가지 않는다
        parent = os.path.dirname(path)
        if parent == path:
            break                      # 파일시스템 루트
        path = parent
    outermost = -1
    for index, path in enumerate(chain):
        if os.path.exists(os.path.join(path, ".git")):
            outermost = index          # worktree·서브모듈은 .git 이 파일이다
    return chain[: outermost + 1] if outermost >= 0 else chain[:1]


def dir_reason(path):
    """기준본 폴더로 쓸 수 없는 사유. 쓸 수 있으면 None.

    사유를 나눠 두는 이유: `chmod` 로 고칠 수 있는 것과 아닌 것이 한 문구로 뭉치면 안내가 틀린다.

    - **링크면 거부한다.** `os.stat` 은 링크를 따라가므로 링크 **자체**는 검사되지 않는다.
      staging 폴더가 링크면 이후 모든 검사가 대상 폴더 기준이 되고, `.gitignore` 도 대상
      폴더에 심긴다 — `O_NOFOLLOW` 는 **마지막 경로 요소**만 지키기 때문이다.
    - **그룹 쓰기도 막는다.** macOS 의 기본 그룹 `staff` 에는 로컬 계정 전원이 들어가고,
      umask 002 인 서버에서는 `mkdir` 이 그냥 0775 를 만든다 — world-writable 과 같다.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return "읽을 수 없음"
    if stat.S_ISLNK(st.st_mode):
        return "심볼릭 링크"
    if st.st_mode & 0o022:
        return "남이 쓸 수 있음 (chmod go-w)"
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return "내 소유가 아님"
    return None


def own_dir(path):
    return dir_reason(path) is None


def inside(base, path):
    """realpath 기준으로 path 가 base 안인가.

    마커 경로만 막고 기준본 읽기를 안 막으면, staging 안에 폴더 밖을 가리키는 심볼릭 링크
    하나로 남의 파일이 「이미 확인된 줄」이 되어 표류 검사를 통과시킨다. 두 경로가 같은
    판정을 써야 한다.
    """
    root = os.path.realpath(base)
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def staging_scan(cwd):
    """(쓸 수 있는 후보, 건너뛴 후보 [(경로, 사유)]) — 둘 다 가까운 곳부터.

    홈 폴백도 **같은 관문**을 지난다. 예외로 두면 늘 존재하는 그 폴더만 검사 없이 통과해,
    남이 쓸 수 있는 전역 폴더의 내용이 그대로 기준본이 된다.
    건너뛴 것을 따로 돌려주는 이유: 조용히 빼면 「마커가 왜 거부되는지」를 알 수 없다.
    """
    override = os.environ.get("HANGUL_STAGING")
    if override:
        return [os.path.expanduser(override)], []     # 사용자가 직접 지정한 곳은 그대로 믿는다
    candidates = [os.path.join(base, ".claude", "hangul-staging")
                  for base in (project_dirs(cwd) if cwd else [])]
    candidates.append(os.path.expanduser("~/.claude/hangul-staging"))
    usable, skipped, seen = [], [], set()
    for path in candidates:
        key = os.path.realpath(path)      # 문자열 비교로는 링크·표기 차이를 못 걸러 중복된다
        if key in seen:
            continue
        seen.add(key)
        reason = dir_reason(path) if os.path.isdir(path) else None
        if reason:
            skipped.append((path, reason))
        else:
            usable.append(path)
    return usable, skipped


def staging_dirs(cwd):
    return staging_scan(cwd)[0]


def primary_staging(cwd):
    """안내문에 쓸 **한** 경로 — 실제로 쓸 수 있는 곳이어야 한다.

    스캔이 건너뛴 폴더를 안내하면 시키는 대로 파일을 써도 상태가 변하지 않는 막다른 길이 된다.
    홈 폴백을 「이미 있는 폴더」로 먼저 치지도 않는다 — `~/.claude/hangul-staging` 은 이 훅을
    한 번 써 본 사용자에게 늘 존재하므로, 정작 안내가 필요한 경우(=프로젝트에 기준본이 아직
    없다)마다 전역 폴더를 가리키게 된다. 거기 쌓인 기준본은 문서 id 만 같으면 이 머신의
    **모든** 저장소에서 통행증이 되고, 폴더 안 `.gitignore` 도 무의미해진다.
    """
    override = os.environ.get("HANGUL_STAGING")
    if override:
        return os.path.expanduser(override)
    home = os.path.expanduser("~/.claude/hangul-staging")
    local = [p for p in staging_dirs(cwd) if p != home]
    for path in local:
        if os.path.isdir(path):
            return path
    for path in reversed(local):          # 없으면 만들 곳 — 가장 바깥 루트부터, 쓸 수 있는 곳으로
        if os.access(os.path.dirname(os.path.dirname(path)), os.W_OK):
            return path
    return home


IGNORE_BODY = ("# hangul-corruption-guard 기준본 — 서버 본문 사본이라 저장소에 올리지 않는다\n"
               "# 이 파일 자신을 포함해 전부 무시한다\n"
               "*\n")


def git_tracked(base):
    """이 폴더에 이미 추적 중인 파일이 있나. **판단하지 못하면 True** 로 본다.

    커밋뿐 아니라 **인덱스까지** 보는 것이 이 판정의 핵심이다. `git add` 된 경로는 다음 커밋에
    그대로 들어가므로, 거기에 `*` 를 심으면 (a) 스테이지된 기준본은 그대로 커밋되고 (b) 심은
    `.gitignore` 는 자기 `*` 에 걸려 끝내 커밋되지 않는다. 그 뒤로는 팀 전원의 새 기준본이
    `git status` 에도 `git add -A` 에도 안 잡히고 `--ignored` 로만 보인다.

    「커밋된 것만 보자」(`git ls-tree HEAD`)는 이 방어를 커밋 직전 구간에서만 골라 끄는 것이라
    채택하지 않았다. `git checkout --orphan` 처럼 인덱스는 추적 파일로 가득한데 HEAD 가 unborn
    인 평범한 상태에서도 같은 사고가 난다. 보이는 실패(status 오염)를 안 보이는 실패(조용한
    누락)와 맞바꾸는 방향이라 이 프로젝트의 다른 결정과도 어긋난다.
    """
    # `-C base` + 상대 pathspec 이라 base 가 속한 저장소가 스스로 답한다. base 가 상위
    # 저장소일 수도 있어서(서브모듈), 고정된 repo 경로로 물으면 pathspec 이 범위를 벗어난다.
    try:
        done = subprocess.run(["git", "-C", base, "ls-files", "--",
                               os.path.join(".claude", "hangul-staging")],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return True                       # git 이 없거나 느리면 판단 불가 — 건드리지 않는다
    if done.returncode != 0:
        # 저장소가 아니거나 인덱스가 깨졌거나 소유권 거부다. 빈 출력을 「추적 없음」으로 읽으면
        # 커밋된 기준본이 있는 저장소에도 심게 된다 — 실측으로 재현한 fail-open 이었다.
        return True
    return bool(done.stdout.strip())


def ensure_staging_ignored(cwd, session=""):
    """기준본 폴더가 소비 프로젝트의 git 에 잡히지 않게, 폴더 안에 자기 자신을 무시하는
    `.gitignore` 를 놓는다.

    폴더를 만드는 것은 `/stage` 를 따르는 에이전트이고 **프롬프트 지시에는 강제력이 없다** —
    이 훅의 존재 이유와 같은 이유로, 문서에만 적어 두면 지켜지지 않는다. 실제로 소비
    프로젝트에 서버 본문 사본 4개가 untracked 로 남아 있었다.

    남의 저장소에 파일을 쓰는 일이라 조건을 좁게 잡는다 — 폴더를 **만들지 않고**(없는 폴더를
    훅이 만들어내면 아무 프로젝트에나 빈 폴더가 생긴다), git 워크트리 **안**에서만 쓰고
    (밖에서는 할 일이 없는 순수 부작용이다), 폴더가 링크거나 남이 쓸 수 있으면 건너뛰고,
    **이미 추적 중인 파일이 있으면 건드리지 않고**, `HANGUL_STAGING` 으로 직접 지정한 폴더도
    건드리지 않는다(그 폴더의 git 정책까지 대신 정할 일은 아니다). 홈도 건너뛴다 — 홈이 git
    저장소인 사람(dotfiles)의 전역 폴백 폴더까지 이 훅이 정할 일은 아니다.

    **안 쓰기로 한 판정은 캐시한다.** 추적 중이면 `.gitignore` 가 끝내 안 생기므로 `lexists`
    빠른 탈출이 영영 안 걸리고 매 호출 `git ls-files` 를 띄우게 된다(실측 43~52ms). 이 함수는
    게이트 밖 MCP 호출에서도 도므로 그 낭비가 호출 전체로 번진다. TTL 을 둬서 추적이 풀리면
    다시 보게 하고, 안 쓴 사실은 **세션에 한 번 알린다** — 조용한 무동작은 원인을 알 길이 없다.
    """
    if os.environ.get("HANGUL_STAGING") or not cwd:
        return
    roots = project_dirs(cwd)
    if not any(os.path.exists(os.path.join(base, ".git")) for base in roots):
        return
    home = os.path.realpath(os.path.expanduser("~"))
    for base in roots:
        if os.path.realpath(base) == home:
            continue
        path = os.path.join(base, ".claude", "hangul-staging")
        target = os.path.join(path, ".gitignore")
        if not os.path.isdir(path) or not own_dir(path) or os.path.lexists(target):
            continue
        skip = _cache_marker("skip-", os.path.realpath(path))
        if marker_is_fresh(skip, IGNORE_VERDICT_TTL):
            continue
        if git_tracked(base):
            remember(skip)
            told = _cache_marker("told-", session + "\x00" + os.path.realpath(path))
            if not os.path.exists(told):
                remember(told)
                notice("[hangul-corruption-guard] 기준본 폴더가 이미 git 에 추적 중이라"
                       " 무시 설정을 넣지 않았습니다:\n"
                       f"  {path}\n"
                       "  git status 오염을 없애려면:  git rm -r --cached"
                       " .claude/hangul-staging\n"
                       "  팀과 공유할 생각이면 그대로 두세요.")
            continue
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o644)
        except OSError:
            continue                      # 이미 있거나(O_EXCL) 링크이거나(O_NOFOLLOW) 못 쓴다
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(IGNORE_BODY)
        except Exception:
            # 내용 없는 .gitignore 가 남으면 다음 호출은 「이미 있다」로 넘어가 영영 복구되지 않는다
            try:
                os.unlink(target)
            except OSError:
                pass


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
        if os.path.isfile(path) and inside(base, path):
            try:
                text = open(path, encoding="utf-8").read()
            except Exception:
                continue
            return [(normalize(l), l.strip()) for l in text.splitlines()
                    if hangul_len(l)]
    return []


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
        path = os.path.realpath(os.path.join(os.path.realpath(base), rel))
        if not (inside(base, path) and os.path.isfile(path)):
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


IGNORE_VERDICT_TTL = 3600   # 초. 추적이 풀리면(git rm --cached) 이 안에 다시 본다

NOTICES = []                # 차단이 아니라 「알리기만」 하는 메시지. 종료 직전에 한 번 실린다


def notice(text):
    NOTICES.append(text)


def _cache_marker(prefix, key):
    return os.path.join(CACHE, prefix + hashlib.sha256(key.encode("utf-8")).hexdigest())


def marker_is_fresh(path, ttl):
    try:
        return time.time() - os.path.getmtime(path) < ttl
    except OSError:
        return False


def remember(path):
    try:
        os.makedirs(CACHE, exist_ok=True)
        open(path, "w").close()
    except Exception:
        pass                              # 캐시를 못 써도 동작은 같다 — 판정을 더 자주 할 뿐이다


def passthru(tool_input=None, did_expand=False):
    """통과 — 밀린 알림이 있으면 그것만 실어 보낸다.

    ⚠️ 알림만 낼 때 `permissionDecision: "allow"` 를 **같이 실으면 안 된다.** 그러면 그 호출의
    권한 프롬프트까지 자동 승인해 버린다 — 알리려다 사용자의 확인 절차를 없애는 꼴이 된다.
    """
    if did_expand:
        emit_updated(tool_input)          # 치환 결과에 알림을 얹어 나간다 (emit_updated 가 처리)
    if NOTICES:
        print(json.dumps({"systemMessage": "\n".join(NOTICES)}, ensure_ascii=False))
    sys.exit(0)


def emit_updated(tool_input):
    """Claude Code 형식. Cursor 는 호환 레이어가 updatedInput -> updated_input 으로
    매핑하고, Codex 는 같은 형식을 쓴다(단 permissionDecision:allow 가 반드시 함께)."""
    payload = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": tool_input,
    }}
    if NOTICES:
        payload["systemMessage"] = "\n".join(NOTICES)
    print(json.dumps(payload, ensure_ascii=False))
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
    if NOTICES:
        message = "\n".join(NOTICES) + "\n" + message
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

    # 게이트 **앞**에서 돈다. 게이트 쓰기가 한 번도 없는 세션에서도 기준본 폴더를 덮기
    # 위해서다 — 실측으로 MCP 를 쓴 36개 세션 중 21개가 게이트 호출 0회였고 그 세션들에서는
    # 이 정리가 아예 돌지 않았다. (창을 좁히는 효과는 없다: 실제 트랜스크립트에서 폴더 생성과
    # 첫 게이트 쓰기 사이의 비게이트 MCP 는 0건이었다.) 빠른 경로는 0.1ms 대다.
    ensure_staging_ignored(cwd, session or "")

    if not any(marker in tool_name for marker in GATED):
        # 화이트리스트 밖에서는 치환이 돌지 않는다. 마커를 보냈다면 그대로 저장되므로 막는다
        stray = leftover_markers(tool_input)
        if stray:
            deny(f"{tool_name} 은 이 훅의 검사 대상이 아니라 마커가 치환되지 않습니다:\n"
                 + "\n".join(f"  {v}" for v in stray)
                 + "\n\n이대로 보내면 본문 대신 마커 문자열이 저장됩니다."
                   " 본문을 직접 보내세요.\n")
        passthru()

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
        usable, skipped = staging_scan(cwd)
        deny("기준본 파일을 찾을 수 없습니다: "
             + ", ".join(f"@@hangul:{m}@@" for m, _r, _f in misses)
             + "\n  찾은 위치 (가까운 곳부터):\n"
             + "".join(f"    {p}\n" for p in usable)
             + "".join(f"    {p}  ← 건너뜀: {why}\n" for p, why in skipped)
             + "  경로는 staging 폴더 기준 상대경로여야 하고, 폴더 밖은 거부합니다.\n"
               "  위 경로가 예상과 다르면 셸의 현재 작업 디렉토리가 엉뚱한 곳입니다"
               " — 프로젝트 루트에서 다시 부르세요.\n"
               "  ⚠️ staging 폴더는 폴더 안 .gitignore 때문에 ripgrep 계열 검색"
               "(에이전트의 Grep·Glob 포함)에 잡히지 않습니다.\n"
               "     「없다」로 보이는 것은 무시된 것일 수 있으니, 경로를 직접 주고"
               " 셸 `grep -n` 이나 `ls` 로 확인하세요.\n")

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
        passthru(tool_input, did_expand)

    ident = doc_id(tool_input)
    target = doc_lines(cwd, ident, "target")     # 내 의도 — 삭제·커버리지 판정
    current = doc_lines(cwd, ident, "current")   # 서버 fetch본 — 표류 대조의 독립 기준
    known = current + target                     # 이 문서에서 이미 확인된 줄
    known_norm = {n for n, _o in known}
    # 표류 대조는 **이 문서의** 기준본으로만 한다. 폴더 전체로 넓히면 무관한 문서의
    # 줄과 우연히 닮았다는 이유로 차단되고, 오류 메시지가 남의 문장을 정답으로 내민다.
    # 어느 파일에서 온 줄인지 함께 들고 있어야 차단 메시지가 **올바른 마커**를 낼 수 있다 —
    # target/ 의 줄에 걸렸는데 current/ 마커를 권하면 편집이 통째로 no-op 이 된다.
    reference = ([(n, o, "current") for n, o in current]
                 + [(n, o, "target") for n, o in target])

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
            # old_str 은 **서버 본문을 옮긴 것**이므로 current/ 하고만 대조한다. target/(내
            # 최종본)까지 후보에 넣으면, 정확히 옮긴 앵커가 내 수정문과 길이·글자가 닮았다는
            # 이유로 차단되고 메시지가 「기준본」이라며 **내 수정문을 정답으로 내민다.**
            cands = [c for c in reference if c[2] == "current"] if is_old else reference
            # 조용히 통과시키는 것은 **이 문서의 기준본**에 있는 줄뿐이다.
            # 다른 파일은 표류 대조용으로만 쓴다 — 아무 파일에나 있으면 통과시키면
            # 한 번 스테이징한 문장이 모든 문서에 대한 영구 통행증이 된다.
            if norm in known_norm:
                continue
            for cand_norm, cand_orig, role in cands:
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
                     + f"\n\n걸린 기준본은 `{role}/{ident}.md` 입니다."
                       " 이 값 전체가 그 파일의 한 구간과 같아질 수 있으면, 옮겨 적지 말고"
                       " 마커로 보내세요\n"
                       f"  (파일 전체 @@hangul:{role}/{ident}.md@@"
                       f" · 줄 지정 @@hangul:{role}/{ident}.md#L12-L15@@).\n"
                     + ("의도한 수정이라면 **고친 본문을 target/" + str(ident) + ".md 에 먼저 쓰세요** —"
                        " current/ 는 서버의 현재 본문이라 이미 손상이 들어 있을 수 있고,\n"
                        "  그대로 다시 보내면 손상을 한 번 더 저장하게 됩니다.\n"
                        if role == "current" and not target else "")
                     + "그렇지 않으면 기준본 쪽 문자열을 그대로 보내세요."
                       " 의도한 수정이라면 기준본을 먼저 고치세요.\n"
                       "  ⚠️ 길이가 같은 정상 수정(가능하다→불가하다)도 여기 걸립니다."
                       " 오탐이면 기준본을 고치거나 HANGUL_GUARD=off 로 끄세요.\n")
            # 에코는 충분히 긴 줄만 — 짧은 라벨까지 확인시키면 소음이 된다
            # old_str 은 기존 본문이라 기준본에 없는 게 정상이므로 에코에서 뺀다
            if not is_old and hangul_len(line) >= MIN_ECHO_HANGUL:
                new_content.append((path, line.strip()))

    if not new_content:
        # 전부 기준본과 일치
        passthru(tool_input, did_expand)

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
        passthru(tool_input, did_expand)

    body = "\n".join(f"  {v}" for _p, v in new_content)
    note = ""
    if len(body) > ECHO_CAP:
        # 잘라서라도 보여주고 재전송은 열어 둔다. 여기서 마커를 만들지 않으면
        # 긴 새 본문이 영구 차단돼 「막다른 길을 만들지 않는다」는 원칙이 깨진다.
        head, tail = body[: ECHO_CAP // 2], body[-ECHO_CAP // 2 :]
        note = (f"\n\n⚠️ {len(body)}자라 가운데를 생략했습니다. 전체를 확인하려면"
                f" {primary_staging(cwd)} 에 기준본을 써 두세요 — 그러면 문자 단위로 대조합니다.\n")
        body = head + "\n\n  … (가운데 생략) …\n\n" + tail

    os.makedirs(CACHE, exist_ok=True)
    open(marker, "w").close()

    # 기존 문서를 고치는데 current/ 가 비어 있으면, 안 바꾸고 그대로 옮긴 줄까지
    # 전부 "새 한글" 로 걸린다. 그 경우 서버 본문을 받아 두는 것이 답이다.
    hint = ""
    if ident and not current:
        hint = (f"\n기존 문서를 고치는 중이고 위 줄이 **원래 본문을 그대로 옮긴 것**이라면,\n"
                f"서버 본문을 받아 {os.path.join(primary_staging(cwd), 'current', ident + '.md')} 에\n"
                f"저장해 두세요. 그러면 이 확인이 사라지고 문자 단위로 대조됩니다.\n")

    deny("기준본에 없는 새 한글입니다. 긴 한글은 재생산 과정에서 자모 하나가 "
         "표류해도(뜬→뜼) 어떤 검사에도 안 걸리므로 저장 전에 한 번 봐야 합니다.\n"
         "이상 없으면 같은 값을 그대로 다시 보내면 통과합니다 — 다만 그 재전송도 재생산이라\n"
         "표류가 한 번 더 들어올 수 있고, 대조할 기준본이 없어 기계적으로는 검출되지 않습니다.\n"
         "확실하게 보내려면 이 본문을 staging 폴더의 파일로 쓰고 마커로 보내세요"
         " — 재생산이 0회가 됩니다.\n\n" + body + note + "\n" + hint)


main()
