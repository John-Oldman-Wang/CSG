#!/bin/bash
# 过夜链：等财务采集结束 → 补估值 → 依次跑三个验证 → 结果落库
#
# 每一步都幂等，中断后重跑接得上。
# 日志不经过任何管道——本项目已四次因日志不可见而误判系统状态。
#
# 验证顺序按结论可信度排：
#   ① 红旗规则  事件客观（退市/亏损超净资产/营收腰斩），不依赖人的判断
#   ② 行业周期  描述统计，需估值数据
#   ③ 筛选回测  仅验证机械筛选层，不构成对方法论的验证
set -u
cd /Users/wwww/Documents/GitHub/CSG

export TQDM_DISABLE=1
export PYTHONUNBUFFERED=1
PY=".venv/bin/python -u -m csg.cli"

stamp() { echo ""; echo "[$(date +%H:%M:%S)] $*"; }

# ── 等待财务采集结束 ────────────────────────────────────────
PID=$(pgrep -f "csg.cli|python -u -" | head -1)
if [ -n "${PID:-}" ]; then
  stamp "等待财务采集进程 $PID 结束…"
  while kill -0 "$PID" 2>/dev/null; do sleep 60; done
fi
stamp "财务采集已结束"

.venv/bin/python -u -c "
import warnings; warnings.filterwarnings('ignore')
from csg.storage import Database
db = Database('data/csg.duckdb', read_only=True)
print(db.query('''
  SELECT 'fin_income' t, count(*) 行, count(DISTINCT code) 只 FROM fin_income
  UNION ALL SELECT 'fin_balance', count(*), count(DISTINCT code) FROM fin_balance
  UNION ALL SELECT 'fin_cashflow', count(*), count(DISTINCT code) FROM fin_cashflow
''').to_string(index=False))
db.close()"

# ── 补估值数据（baostock，与东财互不影响）──────────────────
# 前端的市值/PE/PB 展示与验证② 的 PE 分位都依赖它
stamp "=== 采集估值数据（PE/PB/PS）==="
$PY sync valuations 2>&1 | tail -20

# ── 验证① 红旗规则 ────────────────────────────────────────
# 三个验证中结论最可信：暴雷是客观事件，严格 PIT，
# 产出直接用于校准 config/exclusion.yaml 的阈值
stamp "=== 验证① 红旗规则的历史有效性 ==="
$PY validate flags 2>&1 | tail -40

# ── 验证② 行业周期 ────────────────────────────────────────
# 核心问题：用 PE 分位筛周期行业是否系统性在高点买入
stamp "=== 验证② 新能源行业周期规律 ==="
$PY validate cycle --theme new_energy 2>&1 | tail -40

# ── 验证③ 筛选回测 ────────────────────────────────────────
# ⚠️ 仅验证机械筛选层。方法论中人的判断部分无法回测，
#    漂亮的收益率不构成对方法论的验证
stamp "=== 验证③ 筛选条件历史表现 ==="
$PY validate screen 2>&1 | tail -40

stamp "=== 全部完成 ==="
$PY validate runs 2>&1 | tail -15
