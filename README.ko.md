# Session Monitor

언어: [English](README.md) | **한국어**

Claude Code와 Codex 세션을 위한 always-on-top 오버레이 위젯입니다.

- 지원되는 터미널과 데스크톱 앱 surface 전반의 live status를 표시합니다.
- Claude Code와 Codex로 만들었고 개인 사용을 위해 관리합니다.
- 주로 Windows에서 테스트합니다. 다른 플랫폼도 일부 동작하지만 주된 대상은 아닙니다.

![demo](docs/cm_demo.webp)

## 표시 항목

각 행은 하나의 Claude Code 또는 Codex 세션입니다.

- Provider 표시: **C** Claude, **G** Codex
- 상태 색상과 상태 라벨
- 프로젝트 폴더와 slot 번호
- 짧은 주제 요약

| 상태 | 의미 |
|---|---|
| 작업중 | Claude 또는 Codex가 응답을 생성하거나 도구를 실행 중 |
| 작업완료 | 응답 완료 |
| 질문있음 | Claude 또는 Codex가 사용자 입력을 기다리는 중 |
| 중단됨 | ESC, 실패, 또는 감지된 중단으로 멈춤 |
| 대기중 | 세션은 열려 있지만 활동 없음 |

- 행 클릭: 해당 터미널 또는 앱 창으로 포커스 이동
- 행 우클릭: 실제 Claude/Codex 세션은 종료하지 않고 현재 모니터 화면에서만 제거
- 제거된 행: 새 turn 또는 rollout 업데이트가 감지되면 다시 표시
- 완료된 app-backed 행: 설정된 TTL 이후 자동 숨김
- macOS 우클릭: 두 손가락/보조 클릭 또는 Ctrl-click
- 위치 이동: 바를 드래그하면 이동하며 위치가 저장됨

## 클라이언트와 surface 지원

| Surface | 상태 | 감지 방식 | 포커스 동작 | 비고 |
|---|---|---|---|---|
| Claude Code CLI | 지원 | `~/.claude/settings.json`의 Claude hooks | 가능하면 소유 터미널/창으로 포커스 | 정확도는 터미널이 유용한 process/window 관계를 노출하는지에 따라 달라집니다. |
| Codex CLI with hooks | 지원 | `~/.codex/config.toml`의 Codex hooks | 가능하면 소유 터미널/창으로 포커스 | 실제 PID가 있는 행이므로 활성 CLI 세션에는 가장 좋은 경로입니다. |
| Codex CLI without hooks | fallback 지원 | `~/.codex/sessions/` 아래 rollout JSONL 파일 | WezTerm pane, session id, cwd, 또는 live `codex.exe` process 기준 best-effort | 실제 hook이 세션을 보기 전까지 PID-less 행입니다. nested/subagent rollout은 무시합니다. |
| Claude Desktop / app-backed Code sessions | best-effort | 가능한 경우 Claude native session 파일 | Claude 앱 창을 앞으로 올림 | Claude Desktop은 기존 Code chat으로 전환하는 문서화된 desktop deep link를 현재 제공하지 않습니다. |
| Codex desktop app | 지원 | Codex rollout stream과 가능한 경우 local Codex state DB title | `codex://threads/{sessionId}`를 열고 Codex 앱 창을 앞으로 올림 | 일부 환경에서는 Windows foreground 정책 때문에 포커스 대신 작업 표시줄이 깜빡일 수 있습니다. |
| WezTerm | 가장 좋은 터미널 포커스 경로 | `wezterm cli list` pane metadata | 일치하는 pane/tab을 활성화한 뒤 WezTerm 창을 앞으로 올림 | `wezterm`이 `PATH`에 있어야 합니다. |
| Windows Terminal / PowerShell / cmd | fallback 지원 | process tree, PID, cwd, window matching | 일치한 터미널 창을 앞으로 올림 | 여러 pane이 같은 cwd를 공유하면 덜 정확할 수 있습니다. |
| 기타 터미널 | best-effort | 일반 process/window matching | 가능성이 높은 창을 앞으로 올림 | 정확도는 창 제목과 process ownership에 따라 달라집니다. |
| macOS/Linux | 부분 지원 | hooks와 rollout 파일은 동작 | window focus, hotkey, sound는 제한적 | 이 프로젝트는 주로 Windows에서 개발 및 테스트합니다. |

## 행 제목 생성 방식

각 행의 주제 라벨은 짧은 채팅 목록 제목 형태로 자동 생성됩니다.

- 트리거: Stop hook으로 세션이 idle 상태가 될 때
- 실행 방식: detached background process로 실행되어 대화 중인 세션을 막지 않음
- 비용: 일반 CLI 호출처럼 Claude 또는 Codex 사용량을 소비하며 각 provider의 가장 저렴한 tier 사용
- 언어: OS locale을 따름. 한국어 시스템은 한국어, 그 외는 영어. Codex 제목은 주요 사용자 요청 언어도 따르려 함
- Debounce: 세션별 5분 cooldown

| Provider | Source | Summarizer | 비고 |
|---|---|---|---|
| Claude | Session JSONL | `claude -p --model claude-haiku-4-5` | 신호 우선순위: ai-title, `/rename` slug, away_summary recap, 첫 사용자 메시지 |
| Codex | Rollout JSONL에서 만든 제한된 digest | `codex exec --ephemeral --ignore-user-config -m gpt-5.4-mini` | 최근 사용자 요청, assistant 진행, final answer, tool call, 변경 파일, plan update를 바탕으로 JSON `title`을 생성합니다. |

Codex 제목 생성 안전장치:

- `--ephemeral`: 제목 생성 호출이 새 rollout을 만들지 않게 합니다.
- `--ignore-user-config`: monitor hook이 재귀 실행되는 것을 막습니다.
- 모델 override: `SESSION_MONITOR_CODEX_SUMMARY_MODEL` 또는 `~/.local/share/session-monitor/config.json`의 `codex_summary_model`

## 설치

Python installer가 유일하게 지원되는 설치 경로입니다.

```powershell
git clone https://github.com/vehumet/claude-code-monitor.git session-monitor
cd session-monitor
python install.py
```

Installer 동작:

- 다시 실행해도 안전합니다.
- Session Monitor를 `~/.local/share/session-monitor/`로 복사합니다.
- `/session-monitor`를 `~/.claude/commands/`에 설치합니다.
- 필요한 Claude hooks를 `~/.claude/settings.json`에 병합합니다.
- `~/.codex/`가 있으면 Codex CLI hooks를 `~/.codex/config.toml`에 설치합니다.
- `$session-monitor` skill을 `~/.codex/skills/`에 설치합니다.
- 기존 `~/.claude/session-monitor/` runtime data를 복사합니다.

유용한 옵션:

```powershell
python install.py --dry-run
python install.py --skip-codex-hooks
```

요구 사항:

- Git
- tkinter가 포함된 Python 3.10+
- 지원되는 클라이언트 중 하나: Claude Code CLI, Codex CLI, Claude Desktop, Codex desktop app
- Hook 기반 CLI 모니터링: 해당 CLI 설치와 설정

### 감지 관련 참고

- Installer가 가능한 경우 Claude와 Codex hooks를 자동 설정합니다.
- Codex hooks가 꺼져 있어도 rollout 파일 fallback으로 표시됩니다. 이 행은 PID-less라 click-to-focus가 덜 정확합니다.
- Claude Desktop은 앱 창을 앞으로 올릴 수 있지만, 기존 Code chat 하나를 정확히 전환하는 것은 안정적으로 지원되지 않습니다.
- Codex desktop 행은 일반 window focus fallback 전에 `codex://threads/{sessionId}`를 엽니다.
- 설치 디렉터리를 옮겼다면 `python uninstall.py` 후 `python install.py`를 다시 실행해 hook 경로를 재생성하세요.

### 업데이트

```powershell
cd session-monitor
git pull
python install.py
```

## 실행

설치 또는 업데이트 후에는 CLI 세션을 재시작해 새 command/skill을 다시 로드하세요.

Claude Code:

```
/session-monitor
```

Codex CLI는 현재 custom slash command를 지원하지 않습니다. 설치된 skill을 사용하세요.

```
$session-monitor
```

또는 직접 실행:

```powershell
# Windows
python "$env:USERPROFILE\.local\share\session-monitor\start-session-monitor.py"
```

```bash
# macOS/Linux
python ~/.local/share/session-monitor/start-session-monitor.py
```

## 설정

선택 사항인 `~/.local/share/session-monitor/config.json`:

```json
{
  "language": "ko",
  "background_opacity": 0.85,
  "summary_max_chars": 12,
  "blink_seconds": 10,
  "app_done_ttl_s": 1800,
  "latest_done_hotkey": "",
  "sound_enabled": true,
  "sound_files": {
    "done": "~/Sounds/done.mp3",
    "question": "~/Sounds/question.mp3",
    "interrupted": "~/Sounds/interrupted.wav",
    "status_restored": "~/Sounds/restored.mp3"
  }
}
```

설정 키:

- `language`: OS locale에서 자동 감지됩니다. 한국어 시스템은 `ko`, 그 외는 `en`. 명시적으로 설정하면 자동 감지를 덮어씁니다.
- `background_opacity`: overlay 불투명도. `1.0`은 완전 불투명입니다. legacy `opacity`도 계속 동작합니다.
- `summary_max_chars`: summary column 폭과 summary prompt cap.
- `app_done_ttl_s`: 완료된 app-backed Codex/Claude Desktop 행을 해당 초 이후 자동으로 숨김. `1800`은 30분, `0`은 무제한입니다.
- `latest_done_hotkey`: 기본적으로 꺼져 있습니다. Windows에서는 `ctrl+shift+space` 같은 값을 설정해 가장 최근 완료된 visible row로 포커스할 수 있습니다.
- `sound_files`: monitor event를 audio file에 매핑합니다. 지원 event: `done`, `question`, `interrupted`, `status_restored`.
- Sound playback: platform의 사용 가능한 audio support로 best-effort 처리하며, Windows에서는 내장 beep로 fallback합니다.

## 제거

```powershell
python uninstall.py
```

## License

[MIT](LICENSE)
