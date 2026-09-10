#!/usr/bin/env bash
# SessionStart — 리터럴 UTF-8 관행을 세션 컨텍스트에 넣는다.
#
# 이 저장소의 전제는 「프롬프트 지시에는 강제력이 없다」이지만, 그렇다고 지시가 무의미한
# 것은 아니다. 발생률을 크게 낮추는 것이 1차 대응이고 훅은 그 다음 관문이다.
# 다만 그 지시의 **설치**까지 사용자 손에 맡기면 앞뒤가 안 맞는다 — 안 넣으면 그냥 없는
# 것이므로, 플러그인이 함께 배포한다. 강제력이 아니라 설치 신뢰도를 올리는 장치다.
#
# 컨텍스트 비용이 매 세션 붙으므로 짧게 유지한다.
[ "$(printf '%s' "${HANGUL_GUARD:-}" | tr '[:upper:]' '[:lower:]')" = "off" ] && exit 0
[ "${HANGUL_GUARD:-}" = "0" ] && exit 0
[ "$(printf '%s' "${HANGUL_GUARD:-}" | tr '[:upper:]' '[:lower:]')" = "false" ] && exit 0

cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "[hangul-corruption-guard]\n한글은 유니코드 이스케이프(\\uXXXX) 없이 **리터럴 UTF-8** 로 쓴다. 도구에 넘기는 값(MCP 파라미터 · Write·Edit 의 내용 · Bash heredoc)과 사용자에게 내는 답변 모두 해당한다.\n긴 한글을 다시 써낼 때 자모 하나가 조용히 바뀐다(합쳐→합쳤, 뜬다→뜼다). 바뀐 결과도 유효한 한글이라 JSON 파싱도 서버 검증도 통과한다. **이미 있는 한글을 옮길 때는 기억으로 다시 쓰지 말고 원본을 그대로 복사한다** — 기준본 파일을 만들 때도 마찬가지다.\n노션·Jira·Confluence 에 긴 한글 본문을 저장하기 전에 /hangul-corruption-guard:stage 절차를 따른다. 요약: 본문을 .claude/hangul-staging/ 아래 파일로 먼저 쓰고, 파라미터에는 그 **폴더 기준 상대경로**로 @@hangul:target/<문서 id>.md@@ 를 보낸다. 차단되거나 조건이 안 맞으면 리터럴 UTF-8 로 보낸다."
  }
}
EOF
