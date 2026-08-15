#!/bin/bash
# 等采集结束 → 数据统计 → 研报预测力回测 → 结果落库
set -u
cd /Users/wwww/Documents/GitHub/CSG
stamp() { echo "[$(date +%H:%M:%S)] $*"; }

PID=$(pgrep -f "python -u" | head -1)
if [ -n "${PID:-}" ]; then
  stamp "等待采集进程 $PID 结束…"
  while kill -0 "$PID" 2>/dev/null; do sleep 15; done
fi
stamp "采集已结束"

stamp "=== 数据总览 ==="
.venv/bin/python -u -c "
import warnings; warnings.filterwarnings('ignore')
from csg.storage import Database
db = Database('data/csg.duckdb', read_only=True)
print(db.query('''
  SELECT '研报' AS 数据, count(*) AS 行数, count(DISTINCT code) AS 股票数,
         min(publish_date) AS 起, max(publish_date) AS 止 FROM research_report
  UNION ALL
  SELECT '行情', count(*), count(DISTINCT code), min(trade_date), max(trade_date) FROM daily_quote
''').to_string(index=False))
# 研报与行情的交集决定回测样本量
print()
print(db.query('''
  SELECT count(*) AS 可回测研报数 FROM research_report r
  WHERE EXISTS (SELECT 1 FROM daily_quote q WHERE q.code = r.code)
''').to_string(index=False))
db.close()"

stamp "=== 研报预测力回测（七个维度 × 两期） ==="
.venv/bin/python -u -m csg.cli validate research 2>&1 | grep --line-buffered -vE "^\s*$"
stamp "=== 完成 ==="
