#!/bin/bash
# 串行采集：研报 → 重试失败 → 行情
#
# 全程幂等，可随时中断，重跑自动从水位续传。
# 每阶段结束打时间戳，便于定位卡顿——曾发生过接口挂起导致
# 进程存活但无进展的静默失效（现已由 socket 全局超时兜底）。

set -u
cd /Users/wwww/Documents/GitHub/CSG
PY=".venv/bin/python -m csg.cli"

stamp() { echo "[$(date +%H:%M:%S)] $*"; }

stamp "=== 阶段 1/3：研报采集（续传）==="
$PY sync research 2>&1 | grep -vE "it/s|^\s*$"
stamp "研报采集结束"

stamp "=== 阶段 2/3：重试失败条目 ==="
$PY sync retry research_report 2>&1 | grep -vE "it/s|^\s*$"
stamp "重试结束"

stamp "=== 阶段 3/3：行情采集（回测算收益必需）==="
$PY sync quotes 2>&1 | grep -vE "it/s|^\s*$"
stamp "行情采集结束"

stamp "=== 全部完成 ==="
.venv/bin/python -c "
import warnings; warnings.filterwarnings('ignore')
from csg.storage import Database
db = Database('data/csg.duckdb', read_only=True)
for t in ['research_report','research_forecast','daily_quote']:
    n = db.query(f'SELECT count(*) n FROM {t}').n.iloc[0]
    c = db.query(f'SELECT count(DISTINCT code) c FROM {t}').c.iloc[0]
    print(f'  {t:<20} {n:>9,} 行 / {c:>4} 只')
db.close()"
