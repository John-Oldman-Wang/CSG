#!/bin/bash
# 每日链：采集 → 检测事件 → 生成复核任务 → 推送飞书
#
# 由 launchd 调用（com.csg.daily.plist），不要用 cron：
# 目标环境是会合盖休眠的 MacBook，cron 在休眠时会**整个跳过**那次任务
# 且不留痕迹；launchd 的 StartCalendarInterval 会在唤醒后补跑一次。
# 这正是本项目最怕的失效形态——你以为它在盯着，其实它睡了三天。
#
# 每一步都幂等，中断后重跑接得上。
# 日志不经过任何管道——本项目已四次因日志不可见而误判系统状态。

set -u
cd /Users/wwww/Documents/GitHub/CSG || exit 1

export TQDM_DISABLE=1
export PYTHONUNBUFFERED=1
PY=".venv/bin/python -u -m csg.cli"

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily_$(date +%Y%m%d_%H%M%S).log"
STATE="$LOG_DIR/last_success"

exec > >(tee -a "$LOG") 2>&1

stamp() { echo ""; echo "[$(date '+%F %T')] $*"; }
fail()  { echo "[FAIL] $*"; FAILED="${FAILED:-}$*; "; }

FAILED=""

stamp "===== 每日链开始 ====="

# ── 采集 ──────────────────────────────────────────────────
# 顺序有讲究：先股票列表（含退市，供 PIT 股票池），再财报，
# 最后行情。研报量最大放中间，失败也不影响前后两步。
for step in "sync basic" "sync financials" "sync research" "sync quotes"; do
  stamp "--- csg $step ---"
  # shellcheck disable=SC2086
  if ! $PY $step; then
    fail "$step"
  fi
done

# ── 事件闭环 ──────────────────────────────────────────────
# detect 幂等（event_id 为自然键哈希），重扫不产生重复。
# build 只对 is_backfill=FALSE 的事件建任务——即「你开始盯之后」
# 才发生的事。见 events/detector.py::_mark_backfill。
stamp "--- 检测事件 ---"
$PY ev detect || fail "ev detect"

stamp "--- 生成复核任务 ---"
$PY ev build || fail "ev build"

# ── 推送 ──────────────────────────────────────────────────
# --no-dry-run 是真实发送。notify 内部按 notified_at 去重，
# 同一任务不会重复推送。
stamp "--- 推送飞书 ---"
$PY ev notify --no-dry-run || fail "ev notify"

# ── 结果 ──────────────────────────────────────────────────
if [ -n "$FAILED" ]; then
  stamp "===== 结束（有失败步骤）: $FAILED ====="
  # 不写 last_success —— 看门狗据此告警
  exit 1
fi

date +%s > "$STATE"
stamp "===== 全部成功 ====="

# 只保留最近 30 份日志
ls -1t "$LOG_DIR"/daily_*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null
exit 0
