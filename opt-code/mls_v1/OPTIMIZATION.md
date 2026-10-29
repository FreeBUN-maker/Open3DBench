# MLSv1 的优化空间

建议先提高“预测收益与真实 DRT/STA 收益的一致性”，再增加搜索复杂度。
候选更多或 HBT 更多不等于得分更高。以下优先级是针对本实现的工程判断；
对应研究依据及链接见 [REFERENCES.md](REFERENCES.md)。

已有实测说明了这个优先级：`bp_fe` 的四网方案完整评估合法，TNS 严重程度下降
约 1.44%、WNS 严重程度下降约 4.48%，但 DRC 增加约 1.36%。单网方案反而
有更好的 WNS、DRC 和线长。完整数字和实验边界见 [验证记录](VALIDATION.md)。

| 优先级 | 当前瓶颈 | 实施方案 | 主要观察指标 |
|---|---|---|---|
| P0 | 用 RMST/L 路径估计需求，聚合容量掩盖 M2/M3 热点 | 从 FastRoute 导出 GSegment、逐层 capacity/demand/overflow；候选只围绕真实热点区段展开 | 预测拥塞收益与实际 overflow/DRC 的相关性 |
| P0 | 固定等效 RC、有限路径样本，代理不能表示全设计 WNS/TNS | 读取驱动/负载 Liberty、各层及 via RC；对高价值候选执行增量 STA，包含 slew、所有受影响 sinks 和 hold 风险 | DRT 后 TNS/WNS；代理误差及候选误接受率 |
| P0 | 选中的粗走廊未约束最终 FastRoute 路径 | 在 FastRoute 内实现被替换区段和接入约束，或使用原生增量路由接口引导局部 reroute；保持 die-local 硬限制 | 实际跨层资源使用、实际线长、最终 DRC |
| P1 | 初始 RMST 不是共享线段的 Steiner 拓扑，切一条端点边会漏掉热点树枝 | 优先采用真实 baseline route tree；无路由时接入 FLUTE。支持内部 Steiner 点附近的两端接入及局部拓扑重构 | 同预算候选质量、实际 WL/DRC、拓扑构造时间 |
| P1 | 站点先到先得，已有 HBT 固定 | 候选对冲突图、局部交换、稀缺站点影子价格；选中后联合移动两 HBT 并局部重布线 | 每对新增 HBT 的真实收益、冲突拒绝率 |
| P1 | 精确 pin/via access 和 cut 风险不足 | 将 LEF cut spacing、OBS、真实 pin shape 和接入 via 栈纳入 site/edge 合法性；参考 CUGR 概率资源/可布通性成本 | Cut Spacing、Short、Off Grid 以及物理连通性 |
| P1 | 每次从头执行两次全设计 GRT | 复用基线；只更新受影响子网及拥塞邻域；缓存静态 track/obstruction 和站点索引 | 算法+GRT wall time、峰值内存、退化个例 |
| P2 | 单次贪心容易陷入局部解 | 类 PathFinder 的当前/历史拥塞价格，迭代候选替换与 HBT 重定位；只对热点邻域精排 | 迭代收益曲线、收敛时间 |
| P2 | 两个聚合平面不能表达层异质性 | 局部 `(gx,gy,layer,die)` 图，加 wire/via/HBT 边；借鉴 H3D 的异构网格和树上 DP，限制每网恰好两条 HBT 边 | via/HBT 数、时序、搜索时间 |
| P2 | 权重和阈值尚未跨用例校准 | 用官方 evaluator 做消融和分规模调参，采用留一用例验证；记录完整失败结果，不只记录成功网 | 八例通过率、六项指标分布、隐藏用例稳健性 |

## 两个值得具体建模的问题

**站点组合不能简单拆成两次最近邻或普通二分匹配。** 一个方案必须同时占用两个
站点，还可能与其他方案通过 pitch 邻域或 routing capacity 冲突。可以将每个
候选对设为二元变量 `x_p`，最大化 `Σ U_p x_p`；对每网设 `Σ x_p ≤ 1`，
对每个冲突 clique 设 `Σ x_p ≤ 1`，并限制 `2Σ x_p` 的 HBT 数量。
先用小区域整数规划/最大权独立集，规模大时保留贪心并加 1↔2 交换。
独立的单 HBT assignment 子问题才适合直接采用最小费用流或 Hungarian。

**树上 DP 必须编码赛题的跨 die 次数。** 可以在每个子树状态中保存节点所属 die、
已使用 HBT 数量 `h ∈ {0,1,2}` 和连通模式。合并时要求总体恰好两 HBT、全部
原始端点仍位于原 die，并形成一次借道后返回的链；不能把通用 H3D 或 monolithic
3D 论文中的任意跨层连接数直接用于本赛题。

## 建议实验顺序

1. 用 `MLSV1_MAX_NETS=0` 验证同镜像、同 overlay 的无 MLS 对照；固定输入、
   FastRoute 参数、线程数、DRT 迭代数和镜像 ID。
2. 在 `bp_fe` 上从 1 个 net 开始，观察 `mls_plan.json` 中代理预测与 evaluator
   实际 TNS/WNS/WL/DRC 是否同向；出现退化时先改模型，避免盲目扩大数量。
3. 分别开启真实拥塞、实际 RC、Steiner、站点交换，做逐项消融。每次同时保留
   GRT 时间与 evaluator 时间，不用 `evaluator_runtime_seconds` 代替总 Runtime。
4. 扩展到八个公开用例，尤其检查 `bp_quad` 的内存和时间；对每个 case 保留独立
   基线。最多 32 线程，当前 Python 规划器为单线程。
5. 把完整官方评估加入回归：合法性和物理连通性必须先通过，才讨论评分指标。
   30% HBT 是数量评分阈值，初版将其作为保守预算；跨过阈值的方案也可研究，
   但要用真实收益补偿新增数量惩罚，不能把阈值理解成赛题禁止超过的硬约束。

FastGR/InstantGR 的 GPU 路线可作研究参考。本赛题给定 x86 CPU 和线程上限，
正式实现优先借鉴任务依赖管理、pattern routing 批处理与 CPU 并行调度，不默认
运行环境有 GPU。
