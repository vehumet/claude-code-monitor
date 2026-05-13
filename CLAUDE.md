# Claude Code Monitor

Windows용 실시간 오버레이 위젯. 모든 활성 Claude Code 인스턴스의 상태를 동시에 모니터링한다.

## Commands

```bash
# 버전 올리기 (항상 patch 단위, 0.0.1씩)
python scripts/bump-version.py patch

# CI 로컬 확인
python -m py_compile plugins/claude-code-monitor/src/claude-code-monitor.py
```

## Architecture

Pure Python (3.10+), tkinter + ctypes. 외부 의존성 없음.

```
install.py                               # 루트 설치 래퍼
uninstall.py                             # 루트 제거 래퍼
plugins/claude-code-monitor/
├── src/
│   ├── claude-code-monitor.py           # 메인 오버레이 위젯
│   ├── write-state.py                   # 훅에서 호출하는 상태 기록 스크립트 (Claude/Codex 공용)
│   ├── codex_rollout_poller.py          # Codex hooks 미설정 시 ~/.codex/sessions/ 폴백 폴러
│   ├── start-monitor.vbs                # Windows 런처
│   └── start.sh                         # Unix 런처
├── commands/                            # /monitor 슬래시 커맨드
├── install.py                           # 실제 설치 스크립트 + Codex config.toml 자동 주입
└── uninstall.py                         # 실제 제거 스크립트
scripts/bump-version.py                  # claude-code-monitor.py 버전 갱신 도구
```

## Key Patterns

- **설치 방식**: 공식 지원 설치는 `python install.py` 하나뿐이다. Claude 플러그인 marketplace 메타데이터/설치 경로는 폐기했다.
- **버전 관리**: `plugins/claude-code-monitor/src/claude-code-monitor.py`의 `__version__`이 source of truth다. 반드시 `bump-version.py`로 갱신할 것.
- **버전 규칙**: patch 단위(0.0.1씩)로만 올린다. major/minor 변경은 명시적 요청 시에만.
- **릴리스 절차**: 코드 변경을 커밋한 뒤, 푸시 전에 반드시 (1) `CHANGELOG.md`에 `## [0.0.X] - YYYY-MM-DD` 형식으로 새 버전 섹션을 추가하고 (Keep a Changelog 형식: Added/Changed/Fixed 카테고리 사용) (2) `python scripts/bump-version.py patch`를 실행한 뒤 (3) `chore: bump v0.0.X` 형식의 별도 커밋을 만들어야 한다. 버전 범프·체인지로그 갱신 없이 푸시하지 말 것.
- **상태 흐름**: `write-state.py`가 `~/.claude/monitor/state/{pid}.json`에 상태를 기록하고, 오버레이가 폴링으로 읽는다.
- **훅 경로**: Python installer가 `write-state.py`를 `~/.claude/hooks/`에 복사하고 Claude/Codex 설정에는 설치된 절대/홈 경로를 기록한다.
- **Provider 식별**: state JSON에 `provider`(`"claude"`/`"codex"`) 필드가 있고 첫 write에서 결정된 후 `_PRESERVED_FIELDS`로 고정된다. `write-state.py`는 `--provider claude|codex` CLI 인자 또는 stdin payload의 `hook_event_name` 키(Codex 전용) 또는 ancestor 프로세스 트리에서 자동 감지한다.
- **Codex 폴백**: `[features] hooks = true`(0.128+ 새 키, 이전엔 `codex_hooks`)이 꺼진 환경에서도 `codex_rollout_poller.py`가 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`을 watch해서 PID-less 가상 행(`codex-<sid8>`)으로 표시. hook이 잡은 sessionId는 실제 PID 행으로 승격되고, 폴러의 가상 파일은 제거됨. state 추론은 rollout JSONL의 `event_msg` payload `task_started`/`task_complete` 카운트 비교로 정확히 함 (mtime은 마커 등장 전의 fallback).
- **Codex 요약**: Codex 세션의 Stop hook도 Claude처럼 background summarizer를 spawn한다. `codex exec --ephemeral --ignore-user-config -m <model>`로 호출 — `--ephemeral`이 별도 rollout 안 만들어 우리 폴러가 자식 세션을 못 잡고, `--ignore-user-config`이 우리 hook을 못 봐서 재귀 차단. default 모델 `gpt-5.4-mini` (env `CLAUDE_MONITOR_CODEX_SUMMARY_MODEL` 또는 config.json `codex_summary_model`로 override). transcript는 rollout JSONL의 `event_msg/user_message` 페이로드만 추림.
- **TOML 자동 주입**: `install.py`가 `~/.codex/config.toml`을 in-place 편집할 때, Codex hook 4종(`SessionStart`/`UserPromptSubmit`/`PermissionRequest`/`Stop`)은 marker로 감싼 블록을 파일 끝에 append하고, `[features] hooks = true`는 기존 섹션 내에 한 줄(태그 주석 포함)로 삽입한다. 이중 `[features]` 헤더는 TOML redefinition 에러이므로 절대 넣지 않는다.

## CI

GitHub Actions (`validate.yml`): Python 문법 검사 (3.10~3.13), 유닛 테스트, 버전 형식 검증, JSON 유효성 검사.

## Gotchas

- 사운드/창 포커스 기능은 Windows 전용 (ctypes Win32 API 사용)
- `start-monitor.vbs`는 셸 종료 후에도 모니터 프로세스가 살아남도록 하는 용도
- 상태 파일은 PID 기반이므로 같은 cwd를 공유하는 여러 터미널이 있으면 충돌 가능 (v0.0.2에서 수정됨)
- Codex 폴러가 만드는 PID-less 행은 클릭해도 터미널 창이 안 떠짐 (PID를 모름). hook이 잡은 Codex 행은 정상 동작.
- monitor 디렉터리 이동 시 `~/.codex/config.toml`의 hook command가 절대경로라 깨진다. `uninstall.py` → 디렉터리 이동 → `install.py` 순서로 재설치할 것.
