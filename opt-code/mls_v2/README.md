# MLSv2：GRT 反馈与终点 TNS 驱动的两 HBT 搜索

实现 [优化计划](../mls_v1/OPTIMIZATION.md) 的 A～D 主线，并提供默认关闭的
HBT 重定位、真实 RC 树内部切分和局部替换实验。`mls_v1` 保持原实现。
本版交付代码与接口检查；真实用例的 GRT、官方 DRT/STA 结果由用户运行后分析。

## 运行

在仓库根目录执行。实验使用 `gaocr/3dbench-contest:20260914`，与启动脚本和
通用实验入口的默认标签一致。显式设置环境变量以覆盖终端中可能保留的旧标签。
当前本机该标签的镜像 ID 为 `sha256:8a864301a76c7af524872704eec3e2b418d07149d59ce7400f9b56475676a28f`，
尚未列入通用脚本的镜像白名单，因此下面保留显式放行设置：

```bash
export OPEN3DBENCH_CONTEST_IMAGE=gaocr/3dbench-contest:20260914
export ALLOW_UNVERIFIED_IMAGE=1

# 本版增加了 C++/SWIG 资源导出接口；更换源码或环境后需要重新编译。
./opt-code/scripts/build.sh 8

# 首轮检查：最多接受一个网，最多验证一个候选，另外包含一次无 MLS 基线。
CASE_NAME=bp_fe MLSV2_MAX_NETS=1 MLSV2_MAX_TRIALS=1 \
  ./opt-code/mls_v2/run.sh mls_v2_one
CASE_NAME=bp_fe ./opt-code/mls_v2/evaluate.sh mls_v2_one
```

如使用已在通用脚本白名单中的镜像 ID，可取消 `ALLOW_UNVERIFIED_IMAGE` 设置。
默认搜索最多接受 4 个 MLS 网、执行 12 次候选验证，每轮比较最多 3 个候选：

```bash
CASE_NAME=bp_fe ./opt-code/mls_v2/run.sh mls_v2
CASE_NAME=bp_fe ./opt-code/mls_v2/evaluate.sh mls_v2

# 仅生成同一反馈模型下的无 MLS 基线，可单独官方评估。
CASE_NAME=bp_fe MLSV2_MAX_NETS=0 ./opt-code/mls_v2/run.sh mls_v2_control
CASE_NAME=bp_fe ./opt-code/mls_v2/evaluate.sh mls_v2_control

python3 opt-code/mls_v1/compare_metrics.py \
  reports/bp_fe/mls_v2_control/metrics.json reports/bp_fe/mls_v2/metrics.json
```

每次使用新 label；入口拒绝覆盖已有最终输出。`run.sh` 只进行算法 GRT/STA，
`evaluate.sh` 才调用官方评估器。零候选通过时输出已验证的无 MLS 基线，这是正常结果。
不同 case 和版本请顺序运行，contest 的构建和 flow 工作目录共享；v2 入口之间另有文件锁。

`MLSV2_CONFIG=/absolute/path/config.json` 支持只写需要覆盖的字段；未知字段、
非法数值立即报错。`MLSV2_MAX_NETS`、`MLSV2_MAX_TRIALS` 覆盖配置，
`MLSV2_THREADS` 默认 8、限制 1～32。`OPEN3DBENCH_ROOT`、输入包名称与镜像
环境变量沿用 [通用入口](../README.md)。

## 算法流程

1. 从原始 CTS ODB 重建无 MLS 基线。Bottom/Upper pass 各自导出真实逐层
   FastRoute 资源、wire/via 段和 GRT SPEF，覆盖全部参与布线的背景网。
2. 在提交 ODB 保存之后，使用 finalizer 的内存副本合并 HBT 子网、移除其
   Liberty buffer，合并 SPEF 并给每个 HBT 加一次 `3 Ω + 0.3 fF + 0.3 fF`。
   该时序副本不保存为提交 ODB；提交的 HBT 实例、子网和 guides 保留。
3. 导出全体受约束终点 slack 和原生 TNS/WNS/hold/slew/cap 检查；对有限数量
   的近关键路径建立 `net → driver/sink 分支 → endpoint` 映射。检查逐终点
   负 slack 之和与 STA TNS 一致，记录覆盖率及截断情况。
4. 粗筛使用终点覆盖、真实路由热点和跨度。旧网延迟由实际 GRT RC 树计算，
   新拓扑使用完整源/目标分支、Liberty pin C/drive R、实际 layer/via RC 和
   两个 HBT 估计所有 sink 的变化；驱动 slew 的非线性传播交给实际 STA。
5. 保留最近站点候选，加入完整子树的 Manhattan 中位区域与热点端点；在合法
   晶格中有界扩张，联合枚举两 HBT。粗路径按真实各层容量选择边；资源比较
   释放旧网后再计入新子网，同一子网共享边计一次，不同子网分别计需求。
6. 从同一当前解完整重放少量候选，重跑两 pass、guide 检查和合并 STA。
   以全设计实测综合收益及 TNS/WNS/hold/slew/cap 保护阈值筛选同轮最佳候选。
   失败、超时或拒绝都保留原解；接受后重新导出反馈和计算边际排序。
7. 最终发布已经验证过的 ODB/guides，不再额外重布线。随后由用户运行官方
   evaluator 确认逻辑等价、物理连通、DRC 和最终时序。

各试运行使用独立 label，并从原始 CTS 输入重放已接受操作。回滚不依赖将已失败
router/STA 的内存状态“恢复原状”；先前有效产物始终保留。无有效基线时直接失败，
不会伪造反馈。最终文件通过临时目录复制、哈希核对和原子重命名发布。

## 配置与实验

| 配置 | 默认 | 含义 |
|---|---:|---|
| `max_nets / top_k` | 4 / 24 | 接受 MLS 网数 / 粗筛网数 |
| `max_trials / candidates_per_round` | 12 / 3 | 候选 GRT 验证总数 / 每轮候选数，基线不计候选次数但计总耗时 |
| `max_rounds / max_seconds` | 8 / 7200 | 搜索轮数 / 整体时间预算，单位秒 |
| `trial_timeout_seconds / planner_seconds` | 1800 / 120 | 单次完整 GRT/STA / 每轮规划预算 |
| `max_hbt_fraction` | 0.3 | 含原有 HBT 的保守晶格容量预算 |
| `path_count / endpoint_path_count` | 2000 / 3 | 总导出路径上限 / 每终点近关键路径数 |
| `path_point_budget / slack_margin_ns` | 200000 / 0.1 | 路径点预算 / 纳入采样的正 slack 范围 |
| `sites_per_region / candidate_pairs_per_branch` | 6 / 24 | 每端站点数 / 每分支候选对预算 |
| `max_tns_loss_ns / max_wns_loss_ns` | 0 / 0.02 | 接受方案相对当前解的 TNS/WNS 退化限值 |
| `max_hold_loss_ns` | 0.01 | hold WNS 退化限值 |
| `max_extra_slew_violations / max_extra_capacitance_violations` | 0 / 0 | 不增加对应违例数 |

`tns/wns/congestion/wirelength/hbt/access` 的权重和归一化尺度均在配置中固定，
跨网、同轮及各轮保持一致。`access` 的验证量为全设计 GRT via 层跳数量，粗筛量
是站点邻域压力；这两个量分列保存，不计算相减误差。目标函数是启发式，
并非官方评分公式。默认要求实测综合收益严格大于零且不降低 TNS。

独立实验配置示例：

```json
{
  "enable_route_tree_cuts": true,
  "enable_relocation": false,
  "enable_exchanges": false
}
```

- `enable_regions=false`：仅保留最近站点对照。
- `enable_route_tree_cuts=true`：在检查无环、连通和无重复边的原生 RC 树上，
  增加内部 Steiner 分支切分。几何 RMST 候选仍保留，没有引入 FLUTE 依赖。
- `enable_relocation=true`：对原始单 HBT 3D 网增加保持名称和连接的移动候选；
  释放自己的旧占用再检查新站点，同样完整 GRT/STA 验证。默认最多 2 次。
- `enable_exchanges=true`：为末尾 1→1 / 1→2 替换预留最多 2 次完整验证，
  替换的整体实测收益与当前解比较，不累加旧预测收益。站点冲突由重放提交检查拒绝。

## 反馈口径与当前边界

- `*.resources.tsv` 的 `capacity` 是 FastRoute **已扣减后的可用容量**，`demand`
  是 wire usage；`blockage` 是全部 capacity reduction（含配置调整），仅用于诊断，
  不再次扣除。导出使用原生整数字段，避开 OpenDB 拥塞图的 uint8 截断与混合 usage。
- 逐层资源不包含独立 via 容量。导出中包含实际 via 段；候选 via-stack、pin access、
  cut/PRL 和可布通性仍是近似，最终由 GRT 和官方 DRT 检验。空间 keepout 包括
  宏块与显式 metal10/11 OBS，pin bbox/layer 另存档，不宣称完整 DRC 检查。
- 终点收益按最差竞争路径、跨零截断及正 slack 退化计算，每终点只计一次。
  路径采样不是完整覆盖；未知竞争路径限制正收益外推，并明确标记近似。
  接受判断使用独立的全设计 STA 指标，不用采样 TNS 冒充全设计 TNS。
- 当前支持输入的单 timing scene；多场景 SPEF 会明确失败，避免重复累计场景。
  RC 读取限定 OpenROAD `estimate_parasitics` 的原生无 NAME_MAP、ground-cap
  SPEF；未知格式或断开的 HBT 家族会报错。
- 只对原始 Bottom 普通信号 2D 网新增 MLS；排除 package pin、宏块、多驱动、
  时钟、电源、NDR 和已有 HBT 网。已有 3D HBT 只在独立重定位开关下移动。
- F 阶段仅有本轮路径查询缓存、串行 CPU 预算与完整重跑；未启用跨运行缓存或
  增量 GRT/STA。G 阶段的全层树 DP、历史拥塞协商留待后续研究。
- 没有将预测粗路径写成 guides；保留实际 FastRoute 生成的 guides。GRT 通过
  不等于官方 DRT/DRC 通过；未声明八例性能改善或生产放行。

## 产物和检查

最终产物位于 `output/<case>/<label>/`：

- `5_1_grt.odb`、`route.guide`：提交文件。
- `mls_plan.json`：有效配置、输入/源码哈希、镜像 ID、当前 build 信息、每轮候选、
  接受/拒绝/失败、预测误差、终点覆盖、实际路径重合度、耗时与最终选中状态。
- `mls_plan.tcl`：从原始 CTS ODB 重放最终操作的脚本。
- `mls_timing.tsv`、`mls_hbts.tsv`、可选 `mls_commits.tsv`：最终状态摘要。

完整反馈和每次候选 ODB 位于独立的 `output/<case>/<label>-<run_id>-NNN/`；
日志、每轮排序 JSON、配置与运行报告保存在 `.contest/mls_v2/<case>/<label>-<run_id>/`。
报告记录各路径；失败的子进程日志仍在 `.contest/flow-work/logs/` 的对应 label 下。
源码级报告中的 RSS 分别是主 Python 与其子客户端的峰值，容器 OpenROAD 内存以
flow 日志为准，不将 Docker 客户端内存当作算法峰值。预计算、GRT、导出、STA 和
规划均计入算法时间；evaluator 时间由官方报告独立给出。

```bash
python3 -B -m unittest discover -s opt-code/mls_v2/tests -v
python3 -B -m unittest discover -s opt-code/mls_v1/tests -v
python3 -B -m unittest discover -s opt-code/baseline/flow_scripts/scripts_3D/tests -v
bash -n opt-code/mls_v2/run.sh opt-code/mls_v2/run_pass.sh opt-code/mls_v2/evaluate.sh
```

`tests/api_smoke.tcl` 可在 contest OpenROAD 中检查真实 OpenDB/Liberty API，设置
`MLSV2_TEST_ODB`、`MLSV2_TEST_PLATFORM`、`MLSV2_TEST_SCRATCH` 三个路径后执行。
它只检查准备与导出接口、移动回滚，不运行实际 GRT/DRT。

运行结束后分析所需的主要文件是最终 `mls_plan.json`、官方
`reports/<case>/<label>/metrics.json`，以及报告指向的候选日志和快照。
