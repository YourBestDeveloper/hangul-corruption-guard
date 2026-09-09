# 어댑터

코어(`core/hangul_corruption_guard.py`)는 도구에 의존하지 않는다. 하는 일은 이게 전부다.

```
stdin  ← 호출 정보 JSON
exit 0 → 통과
exit 2 → 차단 (stderr 가 사유)
```

조사 결과 이 관례는 네 도구가 공유한다. 달라지는 건 **설정 파일 위치**와 **입력 JSON 스키마** 둘뿐이고,
스키마는 코어의 `extract_call()` 이 흡수한다.

| 도구 | 설정 위치 | 이벤트 | 입력 스키마 | 검증 |
|---|---|---|---|---|
| Claude Code | 플러그인 (`hooks/hooks.json`) | `PreToolUse` | `tool_name` · `tool_input` · `cwd` | 실제 실행 ✅ |
| Cursor | `.cursor/hooks.json` | `beforeMCPExecution` · `preToolUse` | 위와 같음 + `mcp_server_name` | 스키마만 ✅ |
| Windsurf | `.windsurf/hooks.json` | `pre_mcp_tool_use` | `tool_info` 중첩 | 스키마만 ✅ |
| Codex CLI | `~/.codex/hooks.json` | `preToolUse` | Claude Code 와 동일 | 스키마만 ✅ |

**「스키마만」이 무슨 뜻인지 정확히 적는다.** `tests/test_guard.py` 가 각 도구 문서가 정의한 페이로드
모양을 그대로 훅에 넣어, `extract_call()` 이 같은 판정에 도달하는지 확인한다(정상 통과 · 표류 차단 ·
Windsurf 는 마커 거부). 설정 파일도 이벤트명과 스크립트 경로를 검사한다.

**확인하지 못한 것은 실제 실행이다.** 그 도구를 띄워 훅이 정말 발화하는지, 설정 파일 위치와 형식이
문서대로인지는 해당 환경이 있어야 한다. 문서가 그 사이 바뀌었을 수도 있다.

## 도구별 참고

- **Cursor** — 훅이 죽거나 타임아웃되면 기본이 fail-open 이라 그냥 통과한다. `failClosed: true` 를 넣어야
  차단으로 처리된다. `preToolUse` + matcher `MCP:<도구명>` 경로도 있지만 `beforeMCPExecution` 이 더 직접적이다.
- **Windsurf** — 차단이 종료 코드 전용이다(stdout JSON 으로 allow/deny 를 반환하는 경로가 없다).
  fail-closed 옵션이 문서에 없어 훅 자체가 죽으면 통과된다.
- **Codex CLI** — Claude Code 훅 규약을 그대로 모델링했다. `~/.codex/config.toml` 의 `[hooks]` 로도 쓸 수 있다.

경로에 `<이 저장소 절대경로>` 자리표시자가 있다. 상대경로는 도구마다 기준이 달라 쓰지 않는다.
