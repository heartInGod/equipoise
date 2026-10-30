#!/bin/bash
# [已弃用] 早期固定坐标脚本，已被 tools/act.py 取代；仅在 act.py 不可用时临时使用。
# ADB 游戏操作助手（WePoker 提速版）
# 坐标均为手机原始分辨率 1440x3200。
#
# 观察：
#   ./adb_act.sh look [name]     截图并缩成 540 宽 JPG（约 60KB），打印路径 —— 决策用这个
#   ./adb_act.sh shot [name]     原图 PNG（约 4MB），只在需要看清细节时用
#   ./adb_act.sh app             当前前台包名/Activity
# 快捷动作（点击后等待 0.8s，自动 look 一张，一次调用完成"动作+确认"）：
#   ./adb_act.sh fold            弃牌（左红键）
#   ./adb_act.sh check           让牌 / 跟注（右绿键，面对下注时该位置是"跟注"）
#   ./adb_act.sh bet             中间蓝键（下注 / 加注）
#   ./adb_act.sh p13|p12|p23|p1|p12x   下注尺度预设：1/3、1/2、2/3、1 倍、1.2 倍底池
#   ./adb_act.sh sit             点击底部空座位坐下
# 通用：
#   ./adb_act.sh tap X Y [sleep] / swipe X1 Y1 X2 Y2 [ms] / key BACK / text "..." / raw "<cmd>"
#
# 注意：按钮坐标来自 22:35 的翻牌截图（Hero 行动回合）。面对下注时中/右键的文字会变
# （加注/跟注），位置是否完全一致尚未验证；预设尺度点下去是直接下注还是需再点蓝键确认，也未验证。
set -euo pipefail
export PATH="/opt/homebrew/bin:$PATH"
DEV="${ADB_DEVICE:-59a4a919}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
SHOTS="$ROOT/model_test/manual"
mkdir -p "$SHOTS"

look() {
  local name="${1:-look_$(date +%H%M%S)}"
  local png="$SHOTS/.tmp_$$.png"
  adb -s "$DEV" exec-out screencap -p > "$png"
  sips -s format jpeg -s formatOptions 70 --resampleWidth 540 "$png" --out "$SHOTS/$name.jpg" >/dev/null
  rm -f "$png"
  echo "$SHOTS/$name.jpg"
}

act() { # act X Y label
  adb -s "$DEV" shell input tap "$1" "$2"
  sleep 0.8
  look "after_$3_$(date +%H%M%S)"
}

case "${1:-}" in
  look)  look "${2:-}" ;;
  shot)
    name="${2:-shot_$(date +%H%M%S)}"
    adb -s "$DEV" exec-out screencap -p > "$SHOTS/$name.png"
    echo "$SHOTS/$name.png"
    ;;
  fold)  act 374 2728 fold ;;
  check|call) act 1064 2728 check ;;
  bet|raise)  act 720 2624 bet ;;
  p13)   act 304 2384 p13 ;;
  p12)   act 512 2288 p12 ;;
  p23)   act 720 2280 p23 ;;
  p1)    act 928 2288 p1 ;;
  p12x)  act 1136 2384 p12x ;;
  sit)   act 720 2624 sit ;;
  tap)
    adb -s "$DEV" shell input tap "$2" "$3"
    sleep "${4:-0.8}"
    ;;
  swipe)
    adb -s "$DEV" shell input swipe "$2" "$3" "$4" "$5" "${6:-300}"
    sleep 0.8
    ;;
  key)
    k="$2"
    case "$k" in
      [0-9]*) adb -s "$DEV" shell input keyevent "$k" ;;
      *)      adb -s "$DEV" shell input keyevent "KEYCODE_$k" ;;
    esac
    sleep 0.8
    ;;
  text)  adb -s "$DEV" shell input text "$2"; sleep 0.8 ;;
  app)   adb -s "$DEV" shell dumpsys window 2>/dev/null | grep -E "mCurrentFocus|mFocusedApp" | head -3 ;;
  raw)   shift; adb -s "$DEV" shell "$@" ;;
  *)     sed -n '2,21p' "$0"; exit 1 ;;
esac
