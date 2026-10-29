# MLSv1：两 HBT 的三维全局布线初版

依据 [算法设计指导](../../docs/3D_Routing_Algorithm_Design.md) 和赛题 PDF 实现。
在共享 FastRoute 基线上新增自动 MLS 决策，不修改已有 `timing1c`，
不依赖某个 case 或 net 的名称。输出仍为竞赛要求的 `5_1_grt.odb`。

## 实现范围

```text
CTS ODB + SDC + Liberty + 技术 LEF
    ↓ prepare.tcl：提取端点、物理层归属、HBT、宏块、track pitch、STA 关键性
    ↓ planner.py：Manhattan MST → Top-K → 树边切分 → 两 HBT 联合选址
    ↓ 两个 Die 聚合资源平面上的拥塞代价和局部 A* 搜索 + RC 树 Elmore 比较
    ↓ 逐网重算当前资源收益，接受正收益方案，预留站点并更新资源需求
    ↓ prepare.tcl：事务式生成 BOT / TOP / BOT 三个子网和两个 HBT
    ↓ baseline：隔离的 Bottom/Top FastRoute → 合并并校验 guides
5_1_grt.odb → 官方 evaluator → DRT / DRC / OpenSTA
```

原始 Pin–Net 关联直接保存在 `Net.pins`，构成带坐标和时序权重的超边表示。
初版使用 Prim 算法构造 Manhattan MST（RMST），不是自行实现的 FLUTE/RSMT。
FastRoute 后端使用其已有拓扑生成和拥塞重布线能力。

每个候选是一条 RMST 树边的切分：含驱动端的连通分量留在源侧 BOT 子网，
另一分量的全部 sinks 留在目标侧 BOT 子网，中间 TOP 子网只有两个 HBT 端点。
因此支持多 sink 分支，不只移动单个 sink；所有原始 pin 的物理位置和 die 均保持。
初版只对普通 bottom 2D signal nets 新增 MLS，排除 package pins、宏块端点、
时钟/电源、多驱动网和已有 HBT 网络。原始 3D 网络及既有 HBT 保持原样。

规划阶段不修改 ODB。写回时使用标准 `__MLS__S0__BOT / S1__TOP / S2__BOT`
命名和不冲突的 `LS_HBT_<id>`；不需要额外的 `mls_net_map.json`。
每网提交检查端点集合和两 HBT 链，失败则撤销该事务并终止流程；提交后断言
非 HBT 实例位置/方向、package pin 几何未变。没有正收益或预算不足时不新增 MLS。
不创建 `dbWire` / `dbSWire`，guide 的 die 归属、覆盖和连通性检查复用 baseline。

## 运行

复用 [现有编译和镜像环境](../README.md)。默认镜像仍由通用脚本指定；
需要当前 checkout 的 GRT overlay 已编译，且有官方输入包。

```bash
./opt-code/scripts/build.sh 8
CASE_NAME=bp_fe ./opt-code/mls_v1/run.sh mls_v1
CASE_NAME=bp_fe ./opt-code/mls_v1/evaluate.sh mls_v1

python3 opt-code/mls_v1/compare_metrics.py \
  reports/bp_fe/baseline/metrics.json reports/bp_fe/mls_v1/metrics.json
```

首次试验可用 `MLSV1_MAX_NETS=1`。本地 `bp_fe` 单网和四网方案都已完成官方
评估，单网的 WNS/DRC/线长更好，四网的 TNS 更好；默认数量并非最优参数结论。
完整指标、镜像差异和复现命令见 [验证记录](VALIDATION.md)。

脚本接受其他 case 名称，由镜像内 `contest` 决定支持的输入；未沿用 `timing1c`
入口的 `bp_fe` 限制。当前验证范围见 [验证记录](VALIDATION.md)，不能将接口通用
等同于全部用例已经通过评测。批量实验建议逐 case 执行，避免共享流程工作目录冲突：

```bash
for case in ariane133 ariane136 bp bp_fe bp_be bp_multi swerv_wrapper bp_quad; do
  CASE_NAME="$case" ./opt-code/mls_v1/run.sh mls_v1
  CASE_NAME="$case" ./opt-code/mls_v1/evaluate.sh mls_v1
done
```

配置示例：

```bash
MLSV1_MAX_NETS=1 MLSV1_THREADS=8 ./opt-code/mls_v1/run.sh mls_v1_one
MLSV1_MAX_NETS=0 ./opt-code/mls_v1/run.sh mls_v1_no_mls
MLSV1_CONFIG=/absolute/path/to/my_config.json ./opt-code/mls_v1/run.sh mls_v1_custom
```

`MLSV1_MAX_NETS` 覆盖 JSON 中的数量上限；`MLSV1_THREADS` 范围 1–32，默认 8。
自定义 JSON 允许只列需要覆盖的字段，未知字段和非法数值立即报错。
`OPEN3DBENCH_ROOT`、`OPEN3DBENCH_INPUT_NAME`、`OPEN3DBENCH_CONTEST_IMAGE`
沿用通用脚本。使用不同镜像的操作和校验要求见上级 README。

## 核心参数与目标

| 参数 | 默认 | 作用 |
|---|---:|---|
| `max_nets` / `top_k` | 4 / 24 | 最多接受的网数 / 精排网数 |
| `branches_per_net` | 3 | 每网考察的树边数 |
| `sites_per_endpoint` / `site_radius` | 3 / 3 | 两端各至多 3 个站点，最多 9 对；站点搜索半径为 3 个 HBT pitch |
| `min_span_um` / `max_fanout` | 50 / 32 | 过滤短区段和高扇出候选 |
| `grid_pitch_um` / `max_grid_axis` | 25.6 / 100 | 粗网格粒度及单轴尺寸上限 |
| `max_expansions_per_path` / `max_total_expansions` | 3000 / 200000 | 单次与总 A* 展开预算 |
| `max_hbt_fraction` | 0.3 | 保守策略：新增后总 HBT 不超过容量代理的 30%；可配置 |
| `min_utility` | 0 | 只接受严格高于阈值的代理收益 |

对资源边定义 `F(e,d) = (d/cap)² + 8·max(0,d/cap−1)²`。
一个 net 的重叠粗边只计一次用量；候选比较先移除该网旧占用，再加入 BOT、TOP、
BOT 新占用。已接受方案的资源变化会影响后续网的规划。

```text
U = timing_weight × (Dold − Dnew) / Dold
  + congestion_weight × (Fold − Fnew) / old_marginal_cost
  − wirelength_weight × (Lnew − Lold) / Lold
  − hbt_weight × 2 / HBT_budget
```

分母有非零保护。默认权重为 `0.45 / 0.35 / 0.1 / 0.1`，只是可调启发式；
**不是官方六项评分公式**。`D` 是按端点 criticality 加权后的最大 Elmore 代理，
不是 WNS 或 TNS。时序 criticality 来自 placement parasitics 下最多 1000 条
负 slack 路径；提取失败时退化为几何、拥塞和统一 RC 排序，并在日志说明。

RC 树使用完整源/目标分支及 sink 负载，包含驱动电阻、金属分布电容和两个
HBT。每个 HBT 的 `3 Ω + 两端各 0.3 fF` 只计一次；另有配置化的接入 via-stack
等效 RC。默认金属、驱动、负载及接入 RC 都是**未校准的启发式参数**，
并非自动从 Liberty/`setRC.tcl` 提取的实际值。

## 产物与复现

`output/<case>/<label>/` 保存 `5_1_grt.odb`、`route.guide`、
`mls_plan.json`（参数、候选得失、搜索量、站点、粗走廊）和
`mls_commits.tsv`（实际提交的网、子网、HBT；无提交时可不存在）。
流程工作目录还保留 `mls_design.tsv` 与 `mls_plan.tcl`。

`compare_metrics.py` 比较合法结果的 TNS、WNS、线长、DRC、HBT 和 evaluator
运行时间；对于零基线返回空的相对提升值，不拼接自创总分。evaluator 时间不含
算法/GRT 时间；规划器自己的耗时另记在 `mls_plan.json`。

```bash
python3 -B -m unittest discover -s opt-code/mls_v1/tests -v
python3 -B -m unittest discover -s opt-code/baseline/flow_scripts/scripts_3D/tests -v
```

## 当前近似与下一步

- 资源图只有两个聚合平面，track 容量未扣除完整 OBS、pin access、via 和实际
  FastRoute blockage；需求来自 RMST 的 L 形路径，不是基线 GRT 的精确资源用量。
  包含 package pin、跨 die 异常网及非 signal 的背景需求未纳入该代理。
- 原始 ITerm 用变换后的 `getAvgXY` 估计坐标，层归属由实际 MTerm shape 判断；
  不把平均点当成最终可用 pin access。HBT 用其放置原点作粗规划参考点。
- HBT 做制造网格、晶格、矩形边界、宏块避让和保守的 L∞ pitch 检查；完整
  cut/PRL/OBS 等物理规则仍由官方合法性检查和 DRT 验证。
- A* 返回的粗走廊参与代价评估，**没有作为强制路径/guide 注入 FastRoute**。
  最终 guides 由 FastRoute 自由生成，因此预测收益可能不会兑现。
- 接受顺序是快速优先级排序后的逐网贪心；未做候选集合的最优组合或已选方案交换。
- GRT 仍执行全设计两个 pass，没有增量 GRT、已有 HBT 移动、全层 3D DP 或 DRT
  失败后的自动重试。事务失败会停止输出；不能保证每个参数组合都能通过 DRT。

具体优化方案见 [优化空间](OPTIMIZATION.md)，论文和仓库见 [参考资料](REFERENCES.md)。
