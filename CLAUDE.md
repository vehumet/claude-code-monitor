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
.claude-plugin/marketplace.json          # 마켓플레이스 등록 정보
plugins/claude-code-monitor/
├── .claude-plugin/plugin.json           # 플러그인 메타데이터 (버전 source of truth)
├── src/
│   ├── claude-code-monitor.py           # 메인 오버레이 위젯
│   ├── write-state.py                   # 훅에서 호출하는 상태 기록 스크립트
│   ├── start-monitor.vbs                # Windows 런처
│   └── start.sh                         # Unix 런처
├── commands/                            # /monitor, /update-monitor 슬래시 커맨드
├── hooks/hooks.json                     # 4개 라이프사이클 훅 (UserPromptSubmit, PreToolUse, PostToolUse, Stop)
├── install.py                           # 독립 설치 스크립트
└── uninstall.py                         # 독립 제거 스크립트
scripts/bump-version.py                  # 3곳 버전 동기화 도구
```

## Key Patterns

- **버전 관리**: `plugin.json`, `marketplace.json`, `claude-code-monitor.py` 3곳에 버전이 있다. 반드시 `bump-version.py`로 동기화할 것.
- **버전 규칙**: patch 단위(0.0.1씩)로만 올린다. major/minor 변경은 명시적 요청 시에만.
- **릴리스 절차**: 코드 변경을 커밋한 뒤, 푸시 전에 반드시 (1) `CHANGELOG.md`에 `## [0.0.X] - YYYY-MM-DD` 형식으로 새 버전 섹션을 추가하고 (Keep a Changelog 형식: Added/Changed/Fixed 카테고리 사용) (2) `python scripts/bump-version.py patch`를 실행한 뒤 (3) `chore: bump v0.0.X` 형식의 별도 커밋을 만들어야 한다. 버전 범프·체인지로그 갱신 없이 푸시하지 말 것.
- **상태 흐름**: `write-state.py`가 `~/.claude/monitor/state/{pid}.json`에 상태를 기록하고, 오버레이가 폴링으로 읽는다.
- **훅 변수**: 훅에서 `${CLAUDE_PLUGIN_ROOT}`를 사용해 플러그인 경로를 참조한다.

## CI

GitHub Actions (`validate.yml`): Python 문법 검사 (3.10~3.13), 3곳 버전 일치 검증, JSON 유효성 검사.

## Gotchas

- 사운드/창 포커스 기능은 Windows 전용 (ctypes Win32 API 사용)
- `start-monitor.vbs`는 셸 종료 후에도 모니터 프로세스가 살아남도록 하는 용도
- 상태 파일은 PID 기반이므로 같은 cwd를 공유하는 여러 터미널이 있으면 충돌 가능 (v0.0.2에서 수정됨)
