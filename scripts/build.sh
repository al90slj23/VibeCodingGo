#!/bin/zsh
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$PROJECT_ROOT/build/VibeCoding Go.app"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources" "$PROJECT_ROOT/build/native-module-cache" "$PROJECT_ROOT/build/compiler-tmp"
export TMPDIR="$PROJECT_ROOT/build/compiler-tmp/"
/usr/bin/swiftc -O -whole-module-optimization -module-cache-path "$PROJECT_ROOT/build/native-module-cache" \
  -framework AppKit -framework Foundation -framework AVFoundation -framework WebKit -framework Metal -framework QuartzCore \
  "$PROJECT_ROOT"/Sources/*.swift -o "$APP_DIR/Contents/MacOS/VCGo"
cp "$PROJECT_ROOT/assets/Info.plist" "$APP_DIR/Contents/Info.plist"
if [[ -f "$PROJECT_ROOT/assets/AppIcon.icns" ]]; then
  cp "$PROJECT_ROOT/assets/AppIcon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"
fi
if [[ -d "$PROJECT_ROOT/assets/Sounds" ]]; then
  /usr/bin/ditto "$PROJECT_ROOT/assets/Sounds" "$APP_DIR/Contents/Resources/Sounds"
fi
if [[ -d "$PROJECT_ROOT/assets/Conversation" ]]; then
  /usr/bin/ditto "$PROJECT_ROOT/assets/Conversation" "$APP_DIR/Contents/Resources/Conversation"
fi
mkdir -p "$APP_DIR/Contents/Resources/CCSwitchIntegration"
cp "$PROJECT_ROOT/integrations/ccswitch/vibecue_quota.rs" "$APP_DIR/Contents/Resources/CCSwitchIntegration/vibecue_quota.rs"
/usr/bin/codesign --force --sign - "$APP_DIR"
/usr/bin/python3 "$PROJECT_ROOT/scripts/check-ui-performance.py"
PYTHONPATH="$PROJECT_ROOT" PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s "$PROJECT_ROOT/tests" -p test_usage_overview.py
PYTHONPATH="$PROJECT_ROOT" PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s "$PROJECT_ROOT/tests" -p 'test_engineering_*.py'
PYTHONPATH="$PROJECT_ROOT" PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s "$PROJECT_ROOT/tests" -p 'test_network_*.py'
printf '%s\n' "$APP_DIR"
