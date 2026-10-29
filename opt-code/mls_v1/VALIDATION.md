# 初版验证记录

用户时区日期：2026-10-29。当前真实设计验证范围为 `bp_fe`；未宣称八个公开用例
均已跑通，也未宣称代理收益必然转化为真实评分收益。

## 环境

- 实际可用镜像：`gaocr/3dbench-contest:20260724`。
- 镜像 ID：`sha256:8a864301a76c7af524872704eec3e2b418d07149d59ce7400f9b56475676a28f`。
- README 默认的 `20260914` 标签本机不存在，拉取也未成功。本次通过既有通用脚本
  的显式覆盖接口选择本地镜像，没有修改默认镜像和白名单。
- OpenROAD base：`305d3ba2ddfd00591924cc586ad408179f566afe`。
- GRT public source SHA-256：`7802da53f800b02d0a88d66405c3ed1aad4caa0fb128aa6e17c22ba27f6c6d14`。
- 使用 `opt-code/baseline` overlay 重新编译，8 个编译任务。详细布线沿用镜像内
  固定 evaluator，日志显示最多 32 线程。未修改 evaluator 或 DRT 迭代设置。
- `bp_fe/4_1_cts.def` SHA-256：`bad3bce096f17afa91df2c76c91532d91da7e5202ffb226f47502158a8ff1f67`。

本机复现需要显式选择同一镜像；其他镜像应重新验证，不能把旧镜像的结果视为新
评估器的保证。下面的覆盖仅用于复现此次环境，日常默认仍遵循上级 README。

```bash
export OPEN3DBENCH_CONTEST_IMAGE=gaocr/3dbench-contest:20260724
export ALLOW_UNVERIFIED_IMAGE=1
./opt-code/scripts/build.sh 8
MLSV1_MAX_NETS=1 ./opt-code/mls_v1/run.sh mls_v1_smoke
./opt-code/mls_v1/evaluate.sh mls_v1_smoke
./opt-code/mls_v1/run.sh mls_v1
./opt-code/mls_v1/evaluate.sh mls_v1
MLSV1_MAX_NETS=0 ./opt-code/mls_v1/run.sh mls_v1_control
```

## 已完成的代码与流程检查

- 16 个新增 Python 测试通过，包含树边切分、所有端点恰好分配一次、共享分支
  的 sink 负载、HBT π RC 不双计数、A* 绕开高代价走廊、搜索预算、站点冲突、
  确定性、多 sink 分支及负 slack/零基线的指标比较。
- 现有 baseline 的 5 个测试通过；两个新增 shell 入口通过 `bash -n`。
- 真实 OpenDB 事务测试通过：在第二个 HBT 创建时注入失败，检查所有原网名与
  ITerm 连接完整恢复、无新增实例泄漏、非 HBT 和 package pin 几何不变；随后
  成功提交同一方案，恰好新增两个 HBT、两个净新增子网。
- `mls_v1_control` 没有新增 MLS；生成的 `route.guide` 与已有 baseline 逐字节
  相同，二者 SHA-256 均为 `05ad3dd4acc253edc124c2e6d27d033f6398c1724f0571e201c2877cb86964f5`。
- 单网和默认四网方案都完成两个 Die 的 GRT、guide 合并、层归属/覆盖/连通性
  检查，并生成 `5_1_grt.odb`。官方提交预检均 `legal=true`、零错误，包含逻辑
  等价性、HBT 合法性/数量和无详细导线等检查。

真实事务测试的复现入口是 `tests/transaction.tcl`，需在 contest 容器内设置：

```bash
export MLSV1_TEST_ODB="$CONTEST_ROOT/.contest/flow-work/results/nangate45_3D/bp_fe/mls_v1_smoke/4_cts.odb"
export MLSV1_TEST_PLAN="$CONTEST_ROOT/.contest/flow-work/results/nangate45_3D/bp_fe/mls_v1_smoke/mls_plan.tcl"
export MLSV1_TEST_SCRATCH="$CONTEST_ROOT/reports/bp_fe/mls_v1_smoke/transaction"
"$CONTEST_ROOT/.contest/openroad-build/bin/openroad" -exit -no_init \
  "$CONTEST_ROOT/opt-code/mls_v1/tests/transaction.tcl"
```

出现 `MLSV1_TRANSACTION_TEST_PASS` 表示断言通过。该测试不加载 Liberty，时序提取
会明确退化；它只验证真实数据库事务与回滚，不用其时序值评估算法。

## 规划结果

| 项目 | 单网 `mls_v1_smoke` | 默认 `mls_v1` |
|---|---:|---:|
| 经过长度/扇出/类型筛选的 eligible nets | 3530 | 3530 |
| 精排并接受的网数 | 1 | 4 |
| 新增 HBT | 2 | 8 |
| 最终 HBT | 1151 | 1157 |
| HBT 总容量 / 30% 阈值 | 5766 / 1729 | 5766 / 1729 |
| 粗网格 | 25 × 17 | 25 × 17 |
| A* 累计展开节点 | 139 | 414 |
| Python 规划器耗时（秒） | 1.325 | 1.376 |

默认四网方案选择 `icache_1/lce/_04935_`、`icache_1/lce/_05389_`、
`icache_1/lce/_06050_`、`net2571`；最后一个包含 3 个 sink 的分支。
这些名称是本次输入的自动选择结果，没有写进算法配置。
上表耗时仅包含 Python 规划器，不包含设计加载、Tcl/STA 信息提取、GRT、DRT。

## 完整评估

**单网和四网方案均完成官方 DRT、合并后 DRC、RC 提取与 STA，最终
`metrics.json` 均为 `legal=true`，`legality_error_count=0`。** 这里的合法性表示
评估通过，不表示 DRC 数为零；DRC 数本身也是评分指标。

| 指标 | 已有 baseline | 单网 `mls_v1_smoke` | 四网 `mls_v1` |
|---|---:|---:|---:|
| TNS（ns，越接近 0 越好） | -8828.74 | -8783.38 | -8701.39 |
| WNS（ns，越接近 0 越好） | -4.00944 | -3.64110 | -3.82985 |
| DRT 线长（μm） | 1378685.12 | 1373636.62 | 1376796.74 |
| DRC count | 4257 | 4093 | 4315 |
| HBT count | 1149 | 1151 | 1157 |
| 超过 30% 的 HBT 数 | 0 | 0 | 0 |
| evaluator runtime（秒） | 411 | 458 | 444 |
| 完整评估 legal | true | true | true |

相对于已有 baseline 的变化：

- **单网**：TNS 严重程度下降约 **0.514%**，WNS 严重程度下降约 **9.187%**，
  详细线长下降约 **0.366%**，DRC 下降约 **3.852%**；增加两个 HBT，仍在阈值内。
- **四网**：TNS 严重程度下降约 **1.442%**，WNS 严重程度下降约 **4.479%**，
  详细线长下降约 **0.137%**，但 DRC **增加约 1.362%**；增加八个 HBT，仍在阈值内。
- 两次 evaluator 时间均高于已有基线记录。两次新评估部分并行，运行负载也未与
  历史基线控制一致，**不能据此推导算法/GRT 的总运行时间或正式 Runtime 得分**。
  目前也没有证据宣称 Runtime 改善。表中只如实保留 evaluator 原始时间。

对照指标读取自本地已有 `reports/bp_fe/baseline/metrics.json`；此次重跑的是
无 MLS 的 GRT 对照，其 guide 与已有 baseline 完全一致，没有重复执行基线 DRT。
上述相对变化按基线绝对值计算，便于读数，**未计算官方加权总分**。

`max_nets=4` 保留为多网协同的初版默认配置，不是已证明最佳的参数。
首次试验建议 `MLSV1_MAX_NETS=1`；在本次 `bp_fe` 上它的 WNS、DRC、线长和
HBT 数比四网方案更好，四网方案的 TNS 更好。尚不能把单个用例的趋势推广到
其余用例。这也说明当前粗拥塞代理仍会误判 DRT 风险，后续应优先接入真实逐层
资源与时序反馈，再扩大选网数量。

原始产物位于 `output/bp_fe/{mls_v1_smoke,mls_v1,mls_v1_control}/`，日志、
`metrics.json`、`legality.json`、`comparison.json` 位于 `reports/bp_fe/<label>/`。
这些运行产物沿用仓库忽略规则，不提交到源码；上表保留在本记录中用于源码侧查阅。
