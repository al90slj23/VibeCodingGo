#!/bin/zsh
set -euo pipefail
VIBECUE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VIBECUE_CODEX=""
for VIBECUE_CANDIDATE in \
  '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex' \
  '/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex'; do
  if [[ -x "$VIBECUE_CANDIDATE" ]]; then
    VIBECUE_CODEX="$VIBECUE_CANDIDATE"
    break
  fi
done
if [[ -z "$VIBECUE_CODEX" ]]; then
  VIBECUE_CODEX="$(command -v codex || true)"
fi
if [[ -z "$VIBECUE_CODEX" ]]; then
  printf '%s\n' '找不到 Codex CLI。请先安装或打开官方桌面 App。' >&2
  exit 1
fi
if [[ "${1:-}" == '--print' ]]; then
  printf '%s\n' "$VIBECUE_CODEX"
  exit 0
fi
printf '%s\n' '进入 Codex CLI 后输入 /hooks，审阅并信任指向 VibeCue/vibecuectl hook 的提醒项目。'
exec "$VIBECUE_CODEX" --no-daemon --cd "$VIBECUE_ROOT"
