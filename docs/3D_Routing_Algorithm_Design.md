# 3D-IC Metal Layer Sharing 全局绕线算法设计与开发指南

> **用途**：指导“时序驱动与 HB 布局协同的面对面键合 3D-IC 三维全局布线方法”赛题的算法设计、工程拆分、编码和验收。  
> **文档版本**：v1.0（2026-10-26）  
> **依据**：用户提供的《赛题五》PDF、此前讨论，以及 Open3DBench `EDA_contest` 分支公开 README。  
> **重要原则**：明确区分「赛题/评估器的硬约束」与「本文推荐的算法策略」。最终以实际使用版本的评估器及赛题 Q&A 为准。

---

## 0. 核心结论与总体方案

本赛题**不是要求自行实现完整 DRT**，而是要求实现 **Metal Layer Sharing（MLS）候选线网选择、跨 Die 子网切分、增量 HBT 布局与 3D Global Routing（GRT）的联合优化**。参赛程序输出 `5_1_grt.odb`，由官方评估器再调用 TritonRoute 执行 DRT / DRC，并通过 OpenSTA 评价时序。

推荐采用 **“属性超图 + 候选二维 Steiner Tree + 3D Routing Resource Graph + HBT 联合布局 + Die-local FastRoute”** 的分层算法框架：

```text
设计输入（CTS DEF、LEF、Liberty、SDC、RC rules、脚本）
                          |
                          v
[Step 1] 读入设计、执行/复用基线 GRT、建立资源图及拥塞/时序信息
                          |
                          v
[Step 2] 快速筛选 MLS 候选 Net 与候选替换区段
         估计：Wirelength、拥塞收益、Timing、2 个 HBT 的开销
                          |
                          v
[Step 3] 基于 2D Steiner Tree / Baseline Route 生成候选拓扑
         在 3D Resource Graph 中寻找跨 Die 绕线与 HBT Site
         在当前赛题约束下，每个新 MLS 原始 2D Net 恰好使用 2 个 HBT
                          |
                          v
[Step 4] HBT 放置/合法化与跨 Net 冲突协调；必要时局部重规划
                          |
                          v
[Step 5] 将每个新 MLS Net 转换为 Bottom/Top/Bottom 子网
         分 Die 进行 GRT / 增量 Rerouting，合并并校验 Guides
                          |
                          v
[Step 6] 输出最终 5_1_grt.odb（可选 mls_net_map.json）
         调用官方 evaluator：DRT -> DRC / Wirelength -> STA
                          |
                          v
                评分反馈、参数优化与回归测试
```

**角色分工**：

- **属性超图**描述 Pin–Net 连接关系、空间与时序属性；用于挑选共享线网、识别候选分支、确定优化顺序。
- **Steiner Tree**描述一个 Net 的候选连接拓扑；通常从二维 RSMT/FLUTE 或基线 GRT 路径起步。
- **3D Resource Graph**描述 GCell、金属层、Via、候选 HBT 及资源容量；用于给候选 Tree 赋予跨 Die 路径。
- **HBT Site Manager**提供合法位置及放置互斥约束；MLS 方案不能独立于其他 Net 决定。
- **Die-local FastRoute**负责具体全局绕线、拥塞平衡及 Guide 生成；**TritonRoute 不属于参赛算法的交付内容**。

---

## 1. 赛题边界、硬约束及评分

### 1.1 官方硬约束（开发时必须满足）

| 事项 | 实际要求 | 对实现的影响 |
|---|---|---|
| 提交对象 | `5_1_grt.odb`，包含 HBT 放置、Net/Subnet 连通性和 GRT Guides | 需要可靠地修改 OpenDB；不是仅导出内部图 |
| 不可移动的对象 | 不能修改非 HBT 的 Instance 布局及 Package Pins | 必须将其作为 Immutable Data |
| 跨 Die Guide | 每个最终子网归属唯一 Die，Guide 不得跨 Die | 需要严格的 Layer/Die 校验 |
| HBT | 需满足 Pitch、Cut Spacing、制造网格与其他适用规则 | HBT Site 选择必须执行合法化 |
| Netlist | 折叠 HBT 连接后，与原始网络逻辑等价 | 必须检查端点集合和连通性 |
| 新增 HBT 命名 | `LS_HBT_<id>` | 实现分配器和唯一性检查 |
| 子网命名 | 默认 `<original_net>__MLS__S<id>__BOT/TOP` | 自定义名称需提供 `mls_net_map.json` |
| 原始 2D Net 的 MLS | **当前 EDA_contest README 要求恰好两个 HBT：借用另一 Die 后返回** | 不得直接提交单 Net 4-HBT / 多次跨 Die 方案 |
| 原始 3D Net | 当前 README 说明要求一个 HBT | 保持/处理已有跨 Die 网络，不能随意套用 2D MLS 逻辑 |
| Detailed Routing 图形 | **提交中不得包含 `dbWire` 或 `dbSWire`，包括预布的固定 Via** | `5_1_grt.odb` 只能写 GRT 相关结果；HBT 保留为实例 |
| 运行资源 | x86 CPU，内存不小于 64 GB，单次运行不超过 32 线程 | 设计可控内存与并行策略 |
| DRT | 官方 TritonRoute 评估；赛题 PDF 规定最多 3 轮迭代 | 算法要优化 DRT 可布通性而非自己实现 DRT |

> **本节重要修正**：此前讨论过“一个 2D Net 可选 2/4/更多 HBT”的一般化研究模型。**对本次赛题必须收缩为官方 README 的两 HBT 路径模型**；多 HBT 方案只能作为不提交的研究扩展，除非赛题方修改约束。

### 1.2 评分指标

| 指标 | 权重 | 优化方向 | 什么时候能精确测得 |
|---|---:|---|---|
| TNS | 25% | 负总 Slack 的严重程度降低 | DRT 后 STA；优化中只能近似/增量估计 |
| WNS | 20% | 最差 Slack 改善 | DRT 后 STA；优化中只能近似/增量估计 |
| Runtime | 25% | 缩短时间 | 实测，具体计时边界核对 evaluator |
| Wirelength | 10% | 缩短详细绕线后的金属线长 | DRT 后 |
| DRC Count | 10% | 减少 DRC 违规 | TritonRoute DRC Checker |
| HBT Count | 10% | 控制超额数量 | 从最终 HBT 布局统计 |

赛题文件写明 HBT 数量**统计超过全局 HBT 总容量 30% 的部分**。建议内部定义代理惩罚：

\[
P_{\rm HBT}(N)=\max\left(0,N-0.3C_{\rm HBT,total}\right).
\]

但**不可将此代理表达式当作官方最终归一化评分公式**；真正得分、基线为零时如何归一化、Runtime 的范围，都需以 Q&A / 评估脚本为准。

### 1.3 HBT 物理参数（官方 EDA_contest README）

- HBT Pitch：**6.4 μm**。
- 单个 HBT 串联电阻：**3.0 Ω**。
- 总接地电容：**0.6 fF**，在 `metal10` 和 `metal11` 端点各分配 **0.3 fF**。
- Bottom Die 的 GRT 层范围：**`metal2–metal10`**。
- Top Die 的 GRT 层范围：**`metal11–metal20`**。
- README 的基线通过两个隔离的 OpenROAD 进程分别绕上下 Die 的子网，再合并 Guide，写入 `5_1_grt.odb`。

**注意**：HBT 不是“一个可以任意放在 GCell 中心的零面积 Via”。它有独立几何、端点、Pitch 和 RC，必须以当前版本的 LEF/库/评估器转换方式检查真正的放置合法性。

---

## 2. 设计输入与可提取信息

### 2.1 推荐优先读取的文件

竞赛输入包中典型文件如下（以实际解压路径为准）：

```text
cases/<case>/grt_input/4_1_cts.def
cases/<case>/grt_input/4_cts.sdc
cases/<case>/flow_design/config*.mk
cases/<case>/flow_design/fastroute.tcl
cases/<case>/flow_design/*.v / *.sv2v.v
platforms/nangate45_3D/lef*/**/*.lef
platforms/nangate45_3D/lib*/**/*.lib
platforms/nangate45_3D/setRC.tcl
platforms/nangate45_3D/nangate45_3D.rules
platforms/nangate45_3D/make_tracks.tcl
```

推荐以 **OpenROAD/OpenDB** 读入 LEF+DEF+库和必要约束；内部 GRT 状态应与官方基线使用的技术、DBU、Track、Layer 及 RC 模型一致。

### 2.2 信息提取清单

| 信息 | 来源 / 对象 | 对算法的作用 |
|---|---|---|
| Pin/Net 关系 | `dbNet`、`dbITerm`、`dbBTerm` | 构建属性超图 |
| Pin 几何 | MTerm/ITerm 的变换后几何，BPin Shape，层信息 | Tree 端点、Pin Access、Guide 覆盖 |
| Instance 布局 | `dbInst` 位置、朝向、Master | 固定约束；区分 HBT/普通单元 |
| Die / Layer 映射 | Tech Layer、路由配置 | 限定每个子网可使用的金属层 |
| Track / Via / Obstruction | LEF/OpenDB | 建立资源容量与合法性 |
| 已有 HBT | 具有 HBT 命名/对应 Master 的 Instance 及连接 | 识别保留/可调整的 HBT |
| 时序 | SDC+Liberty+RC+OpenSTA | 获取 Criticality、负 Slack 端点 |
| GRT 线段（如果可得） | FastRoute 内部 `GSegment`、`write_global_route_segments` | 提供准确基线 Tree 候选 |
| GRT Guide | `dbGuide` / `.guide` 文件 | GRT 搜索区域与 DRT 输入 |
| GCell 容量/用量 | FastRoute 资源图及路径占用 | Congestion/Overflow 估计 |

**重要区别**：Guide 是一个（矩形、金属层）的引导区域，**不是唯一的精确金属走线路径**。不要通过 Guide 重叠矩形无条件还原出原始 GRT 的唯一 Steiner Tree。若要对拥塞线段进行精确拆分，优先复用 FastRoute 的 `GSegment` 或显式保存 Route Segments。

Pin 通常存在多矩形、多层图形，中心点不一定是合法 Access Point。数据结构要保存真实 Shape，以及可用于 GRT 的候选网格接入点。所有绝对坐标优先保留 **DBU 整数值**，仅在报告/UI 中换算 μm。

### 2.3 建议的数据结构（逻辑定义，非固定 ABI）

```cpp
struct Pin {
    PinId id;
    NetId net_id;
    InstId inst_id;                  // 顶层 Port 可以为空
    DieId physical_die;             // 硬约束：原始 Pin 不能跨 Die 移动
    LayerId access_layer;
    Point2D location_dbu;           // 仅作估算参考点
    std::vector<LayerRect> shapes;  // 真正的几何/覆盖信息
    std::vector<GridPoint> access_points;
    bool is_driver;
};

struct Net {
    NetId id;
    std::vector<PinId> terminals;
    bool original_2d;               // 与已有 3D Net 区分
    double baseline_wirelength;
    double timing_criticality;
    double baseline_congestion;
};

struct ResourceEdge {
    GridNode u, v;                  // (gx, gy, layer, die)
    enum Kind { WIRE, VIA, HBT } kind;
    double length_um;
    double r_ohm, c_ff;
    int capacity, demand;
    double historical_cost;
};

struct HBTSite {
    SiteId id;
    Point2D location_dbu;
    bool geometry_legal;
    bool occupied_or_blocked;
    double bottom_access_cost;
    double top_access_cost;
};

struct MLSCandidate {
    NetId original_net;
    RouteSection replaced_bottom_section;
    SiteId hbt_site_1, hbt_site_2;
    std::vector<ResourceEdgeId> top_borrowed_path;
    double delta_wirelength;
    double estimated_timing_gain;
    double estimated_congestion_gain;
    double estimated_drc_risk;
    double estimated_runtime_cost;
    double utility;
};
```

上述类型只是工程接口草案。实际优先使用现有 OpenROAD/FastRoute 的 `Net`、`Pin`、`GSegment`、Grid/Layer 类型，避免重复实现整套数据库。

---

## 3. 问题建模

### 3.1 Pin–Net 属性超图

构建：

\[
\mathcal H=(V_{\rm pin},\mathcal E_{\rm net},\phi_V,\phi_E).
\]

- **Vertex**：原始 Pin/Port；带有坐标、Pin Shape、所属 Die/Layer、Driver/Sink、时序属性。
- **Hyperedge**：原始 Net；关联该 Net 的全部 Pin；带有 HPWL/RSMT 长度、拥塞、时序敏感度、Fanout 等属性。

超图可以处理带坐标的 Vertex。**不建议把几何关系完全换算为两两 Pin 的普通边权**：多端 Net 的连接是共享线段的 Tree，Clique 的距离之和不等于真实绕线成本；且空间距离不能表达 Obstruction 或容量约束。

**关键限制**：所有 Pin 位于 Bottom Die 的 Net，也可能通过 `Bottom → Top → Bottom` 借用 Top Die。此时并未改变任何 Pin 的物理 Die。因此，**只对 Pin 做二划分无法表达 MLS**。

> 推荐让超图负责候选 Net/局部分支发现，让 Routing Tree 的**部分边/路径**决定 Die 使用；这才是当前任务的核心决策变量。

### 3.2 候选二维 Steiner Tree

对 Net 的二维 Pin 投影生成：

\[
T_n^{2D}=\operatorname{RSMT}(P_n),
\]

可选来源按优先级：

1. 已存在的 FastRoute `GSegment` / Baseline Route（包含实际 GCell 绕行），适合拥塞热点重路由；
2. FLUTE 等 RSMT 生成器（快速、成本较低），适合缺少完整 GRT 线段的起始状态；
3. 只在粗筛时使用 HPWL（低精度下界）。

**2D Steiner Tree 是初始拓扑，不应锁死最终拓扑**。如跨 Die 的备选路径更优，允许在 3D Resource Graph 上局部变更接入点、Steiner Point 与路径。

### 3.3 3D Routing Resource Graph

定义：

\[
G_R=(V_R,E_{\rm wire}\cup E_{\rm via}\cup E_{\rm HBT}),
\quad v=(g_x,g_y,l,d).
\]

- `WIRE`：同层 GCell 相邻节点的容量边；
- `VIA`：同 Die 相邻金属层之间的合法转换；
- `HBT`：Bottom ↔ Top 的**候选合法站点**对应的跨 Die 边。

每个资源边应保存容量 `cap(e)`、用量 `dem(e)` 和预估绕线成本；容量需要考虑 Tracks、方向、Obstruction、现有导线、Via 保留等因素。不能仅用 GCell 宽度除以 Track Pitch 就当作真实可用容量。

全局拥塞代理指标：

\[
\operatorname{overflow}(e)=\max(0,\operatorname{dem}(e)-\operatorname{cap}(e)).
\]

**建图策略**：先用二维 Tree/拥塞区域缩小局部范围；仅在 Top Die 候选走廊以及 Bottom Die 两端的可达区域展开 3D 图，避免在全部 GCell × 20 层上做每 Net 全局搜索。

### 3.4 HBT 的相互排斥关系

令候选站点集合为 `S_HBT`，为每对不能同时使用的站点构建冲突关系。可借助空间 Hash 或 R-tree 在邻域中快速查找。

对于候选站点 `a, b`，先做中心距离过滤：

\[
\|p_a-p_b\|_2\ge 6.4\,\mu\mathrm{m},
\]

但最终仍必须执行符合 LEF/评估器实际几何的 Cut Spacing 和其他规则判定；欧氏距离下界仅用于**预筛**。

选中某 Net 的 HBT 后更新全局站点状态。若两个高收益 Net 冲突，则比较全局收益或执行局部调整，而不是一味先到先得。

---

## 4. Step 1 — 设计加载与基线分析

**目标**：产出可信的 Pin、Net、Layer、GCell、HBT、时序、拥塞与基线绕线信息。

**建议实现**：

1. 复用官方 `OpenROAD-GRT` / FastRoute 环境，统一 DBU、Tech、Track 和 Layer 编码。
2. 加载 CTS DEF、LEF、SDC、Liberty、RC Rules；在 OpenDB 中建立 Pin/Net/Instance 的只读快照。
3. 恢复已有 HBT 及已被切分的 `_BOT` / `_TOP` 子网。
4. 运行官方未启用新增 MLS 的基线 GRT，获取 Route Segments（如果 API 可用）、Guides、GCell 资源用量。
5. 用 OpenSTA 输出必要的时序端点/路径、各 Net 的 Timing Criticality。
6. 生成 Net/Layer/区域的分析报告，并确认它与官方基线结果在合理范围内一致。

**最低产物**：

```text
analysis/
  case_summary.json
  nets.json
  pins.json
  layers.json
  hbt_sites.json
  congestion_grid.bin
  timing_criticality.json
  baseline_segments.bin      # 若可导出
```

**先做正确性断言**：Pin 几何经过 Instance Transform 后层信息正确；各子网所在 Die 正确；禁止修改的 Instances 有完整快照；原始 Net/原始 HBT 对应关系完整。

---

## 5. Step 2 — Candidate Net Selection：快速收益评估

### 5.1 设计原则

这一阶段回答“**哪个 Net 值得共享？**”，而不是“它最终怎样布线？”。不做每 Net 的完整 3D Steiner Tree 搜索，不调用完整 DRT；成本应尽量与 Net 数量近线性相关。

对 Net `n` 生成少量候选替换区段 `s`：例如基线 Tree 中经过拥塞热点的段，或者 Critical Path 上高 RC、高绕行成本的段。

### 5.2 原始 Wirelength

若有 GRT Route Segments：

\[
L_n^0=\sum_{e\in T_n^0}\ell(e).
\]

没有 Route Segments 时，RSMT 近似可用于筛选；Guide **不能自动当作精确线段**。

对于选择的 Bottom Die 区段 `A→B`，在候选区域选择两个 HBT `h1,h2`，借用 Top Die 的长度变化近似为：

\[
\begin{aligned}
\Delta L_n\approx {}&-L_{\rm removed}\; + d_B(A,h_1) \\
 &+ d_T(h_1,h_2)+d_B(h_2,B).
\end{aligned}
\]

其中 `d_B, d_T` 可逐级细化：

1. 低成本：Manhattan 距离（**几何下界**）；
2. 中成本：RSMT/路径走廊估计；
3. 高一些：局部带拥塞代价的 GCell 最短路。

选择哪种精度，取决于 Net 是否已经进入 Top-K 候选。

**关键判断**：原始 Pin 全在 Bottom Die 的情况下，穿到 Top Die 并不能改善无遮挡二维 Manhattan 距离下界；主要收益应来自**规避拥塞引起的绕路、访问较空闲/较优 RC 的层、提升 DRT 可布通性**，而不应预设所有 MLS 都会减少线长。

### 5.3 Congestion Benefit

定义 Net 基线经过的资源边集合为 `R_old`，计划解除占用的底层区域为 `R_removed`，新路径估计占用资源为 `R_new`。

推荐代理：

\[
\widehat B_{\rm cong}=
\sum_{e\in R_{\rm removed}}\!f(\rho_e)
-\sum_{e\in R_{\rm new}}\!f(\rho_e^{\rm after}),
\quad \rho_e=\frac{dem(e)}{\max(1,cap(e))}.
\]

`f` 可采用拥塞接近或超过容量时快速上升的凸函数；也可对 Overflow 改善赋更高权重。必须同时考虑借用 Top Die 的资源消耗，不能只统计 Bottom Die 释放量。

### 5.4 Timing Benefit

利用 `SDC + Liberty + OpenSTA + RC` 获得端点 Slack、关键路径集合及 Net 的 Timing Criticality。初筛时可使用：

\[
\widehat B_{\rm timing}(n,s)
\approx \operatorname{Criticality}(n)\cdot
\left(\widehat D_{\rm old}-\widehat D_{\rm new}\right).
\]

`D_new` 需计入金属 RC、同 Die Via、**两个 HBT** 的端点电容与串联电阻、驱动负载及扇出分支影响。可以先使用 Elmore/线性 RC 近似，重要候选再进行增量 STA。

- **TNS 是端点 Slack 的合计，不是 Net Slack 的简单求和**。
- **WNS 是最差端点 Slack，也不是每个 Net 独立相加**。
- 初筛的 Net 局部收益只能作为排序启发式；最终必须通过全设计时序反馈校正。
- 若完整路径 RC 已包含 HBT RC，就**不能再次单独扣一次 HBT 延迟成本**。

### 5.5 HBT 开销

当前参赛约束下，原始 2D Net 的一次 MLS 候选：

\[
\Delta N_{\rm HBT}=2
\]

（现有 HBT 的原始 3D Net 应单独按基线既有连接处理，不适用该公式）。

定义候选方案成本：

\[
\widehat C_{\rm HBT}=C_{\rm count}
+C_{\rm RC}+C_{\rm placement},
\]

其中：

- `C_count`：根据全局 `HBT_count` 距 30% 容量阈值的余量，计算边际数量惩罚/预算影子价格；
- `C_RC`：穿越 HBT 导致的时序成本；若 Timing Benefit 已完整计入，则设为零以避免双计数；
- `C_placement`：候选区域可用站点数、HBT 间距、两侧接入难度、Via/Cut 密度、局部 DRC 风险。

**阈值内不是物理零成本**：额外 HBT 仍消耗合法站点和 routing resources，并可能恶化 DRT 与 Timing。

### 5.6 候选打分（代理目标，不是官方评分公式）

推荐优先使用**归一化的、相对基线的变化量**：

\[
\begin{aligned}
U(n,s)={}&
\alpha_T\,\widehat B_{\rm timing}
+\alpha_C\,\widehat B_{\rm cong} \\
&-\alpha_L\,\widehat{\Delta L}
-\alpha_H\,\widehat C_{\rm HBT}\\
&-\alpha_D\,\widehat C_{\rm DRCrisk}
-\alpha_R\,\widehat C_{\rm compute}.
\end{aligned}
\]

实践建议：

- 时序指标（TNS 25%、WNS 20%）合计很重要；Runtime 25% 同样重要。
- 先过滤 HBT Site 明显无解、无拥塞/无时序潜力、预计绕行过大的候选。
- 对每个 Net 至多保留 `M` 个候选区段、每区段至多 `P` 组 HBT 近似端点；全局仅保留前 `K` 个 Net 进入 Step 3。
- `K, M, P` 全部配置化，并在公开用例上测量收益/Runtime 曲线，不预设统一最佳阈值。

**Step 2 输出**：`vector<MLSCandidate>`，包含候选区段、HBT 候选区域、预计 Wirelength/Congestion/Timing/DRC/Runtime 以及收益排序。

---

## 6. Step 3 — Routing Tree 规划与跨 Die 切分

### 6.1 为什么不直接对 Pin 做 Hypergraph Bipartition

所有 Pin 都在 Bottom Die 的原始 2D Net 仍可能把其中某段路径迁到 Top Die 并返回：Pin 物理归属完全没有变化。将 Vertex=Pin 做硬性的二划分，既违反原始 Pin Die 固定性，也不能表达“借用另一 Die 的无 Pin 路径”。

所以真正的优化变量应是：

1. 被替换的 Routing Tree 子路径/区段；
2. 两个 HBT Site；
3. Bottom→HBT1、Top 内 HBT1→HBT2、HBT2→Bottom 的资源路径；
4. 后续按 Die 切分出来的子网连接方式。

### 6.2 两阶段拓扑/路径生成

**A. 初始二维树**：获取/生成 `T_n^{2D}`，识别高拥塞、高时序敏感的替换段 `A→B`。如果原来的 GRT Tree 存在共享分支，需要确定切断/重接后所有原始 Pin 仍连通。

**B. 限域 3D 搜索**：对每个候选区段：

1. 在 `A`、`B` 附近和候选走廊内枚举合法站点对 `(h1, h2)`。
2. 在 Bottom Die 资源图上估算/搜索 `A→h1`、`h2→B` 的接入路径。
3. 在 Top Die 资源图上搜索 `h1→h2` 的候选路径（逐层 Layer Assignment + Maze Route）。
4. 使用两条 HBT 垂直边拼合候选三维连通树。
5. 对替换前后线长、Delay、Congestion、HBT Site 冲突、可布通性代理进行差分评价。
6. 保留 Pareto 或得分较高的少量候选；必要时局部改变 Steiner Point 和分支拓扑。

可利用如下近似路径代价：

\[
C_{\rm path}=\sum_{e\in\text{path}}
\bigl(\lambda_\ell\ell_e+
\lambda_c f(\rho_e)+
\lambda_v\mathbb{1}_{\rm via}(e)\bigr)
+\lambda_h N_{\rm HBT}
+\lambda_t \widehat{\Delta Delay}.
\]

**不要把此代理路径成本直接当作真实 Wirelength 或官方 Score**；同时保存原始长度、时序与占用增量。

### 6.3 与“直接在 3D Graph 上求 Steiner Tree”的关系

- 理论模型是寻找连接所有 Pin 的 3D Routing Tree：

  \[
  T_n^* = \arg\min_{T\subseteq G_R,\;P_n\subseteq T} C(T).
  \]

- 直接在全芯片、全部层上搜索接近一般 Steiner Tree 问题，代价高，不宜作为每个 Net 的常规求解方式。
- **推荐**：2D RSMT/基线路由提供初始拓扑 → 限域 3D 局部搜索 → HBT Pair Placement → 局部 Steiner Tree 重构 → FastRoute Refinement。
- 当高价值候选需要改变较大拓扑时，可以将完整三维最短连接作为**精排的可选模式**，而非第一版实现的基础。

### 6.4 从确定的 HBT 选择生成子网

给定已经选择了恰好两个 HBT Edge、且所有 Pin 连通的候选 3D Routing Tree：

\[
H_n=E(T_n)\cap E_{\rm HBT},\quad |H_n|=2.
\]

删除两条 HBT Edge 后：

\[
T'_n=T_n\setminus H_n.
\]

由于 `T_n` 是 Tree，理论上得到 **3 个单 Die 连通分量**（Bottom 源侧 / Top 借用段 / Bottom 目标侧），并由两侧 HBT 端点重新连接。实际网表生成时：

- 依据连通分量分配**原始 Pin**和**新增 HBT Pin**；
- 每个分量创建唯一名称的 ODB `dbNet`；
- 每个新 HBT 的两个端点连接到相邻两 Die 对应子网；
- 检查折叠 HBT 后原始 Net 的全部原始 Pin 恰好属于同一个连通网络；
- 严格限制 `Bottom` 子网的 Guide 在 `metal2–10`，`Top` 子网在 `metal11–20`。

注意：**候选 HBT Edge 不等于已放置 HBT**。只有最终采纳的 HBT Site，才能提交为真实 `LS_HBT_*` 实例。对 HBT 几何和 Netlist 的修改应保持可回滚/事务性。

### 6.5 Step 3 推荐伪代码

```python
def plan_mls_for_net(net, candidates, resource_graph, hbt_sites):
    best = None
    for segment in candidates:                      # Step 2 的热点区段
        for h1, h2 in enumerate_legal_hbt_pairs(segment, hbt_sites):
            bottom_left = route_bottom(segment.A, h1, resource_graph)
            top_path = route_top(h1, h2, resource_graph)
            bottom_right = route_bottom(h2, segment.B, resource_graph)
            if any(p is None for p in [bottom_left, top_path, bottom_right]):
                continue
            proposal = replace_segment_and_reconnect_tree(
                net, segment, bottom_left, h1, top_path, h2, bottom_right
            )
            if not proposal.tree_connected:
                continue
            if proposal.hbt_count != 2:
                continue  # 当前评估接口对原始 2D MLS 的硬约束
            proposal.utility = score_delta(proposal)
            if best is None or proposal.utility > best.utility:
                best = proposal
    return best
```

`route_*` 可先使用成本较低的粗网格最短路，后续再升级为更准确的 Maze Router。`replace_segment_and_reconnect_tree` 要负责去环、保连通、保分支，不能仅把若干路径几何地叠加。

---

## 7. Step 4 — HBT Placement / Legalization 与跨 Net 协调

### 7.1 候选站点生成

- 将可放置区域对齐制造网格和 HBT 几何约束；
- 剔除 OBS、禁布区、原有 HBT 及其间距排斥区；
- 优先考虑 Bottom 被替换区段两端的附近站点，并同时考量 Top Die 的可布通路径；
- 对每个站点缓存 `bottom_access_cost`、`top_access_cost`、局部密度、拥塞/DRC 风险。

### 7.2 全局选择与冲突解决

最简单可落地方案：按 Step 3 的收益降序处理候选，以**事务**方式逐 Net 申请两个站点，放置成功才占用资源；失败则换用第二候选方案/跳过该 Net。

提高质量的办法：

- 冲突时对比“保留哪个 Net 的 HBT 组合”能带来更大的全局收益；
- 维护 HBT Site 的拥塞/稀缺性价格，迭代调整；
- 对相邻几组 HBT 进行有限范围的移动与局部 Reroute；
- 接受新方案前，重算资源需求及受影响 Net 的路由成本。

**不要只在 Step 3 完成分割后就固定 HBT 位置**；HBT Site 是可影响线长、RC、局部 DRC 的主决策变量。

### 7.3 原有 HBT 处理

- 将现有 HBT 列入固定或可调整的 Site 集合；移动资格必须服从输入设计和官方规则。
- 区分“已有原始跨 Die Net 的 1 个 HBT”和“新增 MLS 原始 2D Net 的 2 个 HBT”。
- 避免改变非 HBT Instance/Pin，避免以复用名义破坏原网络的逻辑等价性。

---

## 8. Step 5 — 子网写回、Die-local GRT、Guide 生成

### 8.1 建议复用现有 FastRoute 基线

官方 `OpenROAD-GRT/openroad_src` 已有 Die-by-die Global Routing 支持；GRT 基线严格限制单 Net 的可用层。**第一版应修改/扩展它，而不是重写全部 FastRoute**。

现有官方流程：

1. 按 Die 分类网络；
2. Bottom 使用 `metal2–metal10`；
3. Top 使用 `metal11–metal20`；
4. 两个独立 OpenROAD 进程绕线；
5. 合并 Guides 并写入最终 `5_1_grt.odb`。

在此基础上：

- 在构造 GRT 输入前完成 `LS_HBT_*` 新实例及新子网的网表编辑；
- 对已改变的 Net/子网执行 GRT；若条件允许，优先增量更新而不是全设计重跑；
- 以容量为约束迭代 Reroute 并调整局部 Cost；
- 合并上下 Die Guide 时，检查没有一个子网覆盖到另一 Die 的 Layer。

### 8.2 输出与命名

对于某个原始 2D Net，例如 `n123`，可以创建：

```text
n123__MLS__S0__BOT
n123__MLS__S1__TOP
n123__MLS__S2__BOT
LS_HBT_000001
LS_HBT_000002
```

这是示意命名，`S<id>` 在对应原 Net 的子网内唯一即可。若保留不同名称，需要在 `5_1_grt.odb` **同级目录**提供 `mls_net_map.json`，映射完整的原始 Net → 全部切分后的子网。

### 8.3 交付前自检（失败即拒绝写最终结果）

- [ ] 每个原始 2D MLS Net **恰好两个**新增 HBT，形成合法跨 Die 往返连接；
- [ ] 所有非 HBT Instances 及原 Package Pins 的位置、方向、连接要求不被非法修改；
- [ ] 折叠所有 HBT 后每个原始 Net 的端点集合/连通关系等价；
- [ ] 任何子网的 Guide 都不跨 Die；
- [ ] 每个需要布线的非 Supply、且有两个以上端点的子网获得合适的 Guide 覆盖；
- [ ] HBT 间距、制造网格、LEF Cut Spacing 与相关合法性通过；
- [ ] ODB 无 `dbWire` / `dbSWire`（包括固定 Via 几何）；
- [ ] ODB 能被官方 `contest evaluate` 正常读入并生成规范 DEF/Guide；
- [ ] 失败或超时有安全回退方案（例如该 Net 不做 MLS、恢复基线）。

---

## 9. Step 6 — 评测反馈、优化策略与 Runtime 控制

### 9.1 目标函数是代理；真实指标靠评估器

最终想优化的是六项指标的加权相对基线收益，但在 Step 2/3 进行上万次候选搜索时，不能每次都完整执行 DRT 和 STA。

内部采用：

\[
\begin{aligned}
\mathcal L={}&
\lambda_T C_{\rm timing}
+\lambda_C C_{\rm congestion}
+\lambda_W C_{\rm wirelength}\\
&+\lambda_H C_{\rm HBT}
+\lambda_D C_{\rm DRCrisk}
+\lambda_R C_{\rm runtime}.
\end{aligned}
\]

- `C_timing`：预计端点 Delay/Slack 恶化或改善（必要时增量 STA）；
- `C_congestion`：预计容量不足和 Overflow；
- `C_wirelength`：长度变化，**不把拥塞罚项当长度**；
- `C_HBT`：计数预算、站点稀缺、RC（注意避免双计数）；
- `C_DRCrisk`：Pin Accessibility、Via/Cut Density、局部拥塞代理；
- `C_runtime`：候选数量、搜索节点数、增量 reroute 调用数的开销预算。

正式评分按 evaluator；不得假设这里的 `λ` 就是题目表中的权重。特别是 TNS/WNS 可能为负或很接近零，所谓“提升率”要与官方实现对齐。

### 9.2 实施成本控制

- 快速特征预计算 + Top-K Net，避免对所有 Net 做 3D Maze Search。
- 为 HBT 站点建立空间索引；跨 Net 增量更新局部受影响区域。
- 对中低价值 Net 保持基线，不强行 MLS。
- 快速解：2D 路径局部替换 + 两个 HBT；仅对少数高价值 Net 允许拓扑重构。
- 优先使用基线 GRT 的增量/局部 reroute 能力；缓存路径和代价。
- 按参数预算终止优化：总候选数、搜索节点数、迭代次数、剩余时间；不超过 32 线程。
- 目标应按**全 8 个公开 + 4 个隐藏用例的泛化**衡量，避免把网格尺寸/Net 命名或单例阈值硬编码到算法中。

### 9.3 不可忽略的 DRC 风险

官方会统计 Short、Non-Sufficient Metal、Metal Spacing、PRL Metal Spacing、Minimum Width、Off Grid、Cut Short、Cut Spacing（HBT Pitch 由后者相关规则检查）。

GRT 阶段无法精确复现 DRT 后全部 DRC，必须建立 *DRT-friendly Guide* 的近似指标，并使用完整评估结果定期校准。

---

## 10. 推荐软件模块与代码目录

```text
OpenROAD-GRT/                       # 以官方竞赛 baseline 为主体
  openroad_src/                     # 修改 OpenROAD grt/FastRoute 相关组件
  flow_scripts/

mls/                                # 建议的新增模块（可作为 C++ 子目录）
  design/
    design_loader.*                 # OpenDB/Tech/Pin/Net 读取
    layer_die_map.*                 # metal2-10、11-20 与 HBT 映射
    netlist_snapshot.*              # 保留原始设计用于 equivalence
  graph/
    attributed_hypergraph.*         # 逻辑连接，Net/Pin 属性
    resource_graph.*                # GCell+Layer+Die+capacity
    congestion_model.*
  timing/
    criticality.*                   # OpenSTA 接口、关键性与 RC 估计
  candidate/
    hotspot_detector.*              # 基线 Tree 热点区段识别
    candidate_selector.*            # Step 2 Top-K
    wirelength_estimator.*
  planning/
    steiner_adapter.*               # FLUTE/FastRoute Tree 接口
    mls_planner.*                   # Step 3 2-HBT 局部 3D 路径
    route_delta_cost.*
  hbt/
    site_map.*                      # 站点合法性与空间索引
    hbt_optimizer.*                 # Step 4 协同放置与冲突解决
  netlist/
    subnet_builder.*                # Step 5 切分与 ODB 写回
    equivalence_checker.*           # 折叠 HBT 验证连通性
  routing/
    die_local_router.*              # 调用/扩展 FastRoute
    guide_validator.*
  evaluation/
    metrics_parser.*                # 读取 metrics.json
    report_writer.*
  config/
    mls_options.*

scripts/
  run_baseline.sh
  run_mls.sh
  evaluate_all.sh
  compare_metrics.py

tests/
  unit/
  integration/
  regression/
```

### 建议组件 API

```cpp
class CandidateSelector {
public:
    std::vector<MLSCandidate> rankCandidates(const DesignState& state);
};

class MLSPlanner {
public:
    std::vector<MLSProposal> plan(const Net& net,
                                  const std::vector<MLSCandidate>& hints,
                                  const ResourceGraph& graph,
                                  const HBTSiteMap& sites);
};

class HBTOptimizer {
public:
    std::vector<MLSProposal> selectAndLegalize(
        const std::vector<MLSProposal>& proposals,
        MutableResourceState& resources);
};

class SubnetBuilder {
public:
    bool commitTransaction(const MLSProposal& proposal, odb::dbBlock* block);
    bool verifyEquivalence(const OriginalNetSnapshot& original,
                           odb::dbBlock* block);
};

class GuideValidator {
public:
    ValidationReport validate(odb::dbBlock* block) const;
};
```

实际类名可调整，但建议把 `analysis → proposal → legalization → commit → route → validate` 的接口边界固定，并保留可回滚的中间表示。**不要在候选搜索时频繁对真实 ODB 直接增删 Net/Instance**。

---

## 11. 推荐开发里程碑和验收标准

### Milestone 0 — 建立完全可重复的官方基线

- 克隆 `EDA_contest` 分支，准备统一 Docker/输入包；
- 运行 `contest build`、`contest run-grt`、`contest evaluate`；
- 至少对 `bp_fe` 端到端跑通，能够读出 `metrics.json`；
- 记录工具版本、输入 Archive SHA-256、运行时间和机器配置。

**验收**：不增加任何 MLS 逻辑仍可稳定输出合法 `5_1_grt.odb`，复现实验报告。

### Milestone 1 — OpenDB 信息提取与一致性检查

- 解析 Net/Pin/Die/HBT/Guide/Track/Blockage；
- 建立 Hypergraph 和三维资源图的最小实现；
- 对已有原始/切分 Net 的电气连通关系做检查。

**验收**：基础统计可与 OpenROAD/FastRoute 输出对齐，不误判 Pin 所属 Die。

### Milestone 2 — 最小可行 MLS（MVP）

- 手动指定**一个**合法原始 2D Net；
- 选择两个 HBT Site；
- 构建 Bottom / Top / Bottom 子网；
- 使用已有 Die-local GRT；
- 输出最终 ODB 并通过预检与 DRT 评估。

**验收**：逻辑等价、两个 HBT、合法 Guide、无 `dbWire`，能完成 `contest evaluate`。这是最关键的工程门槛。

### Milestone 3 — 自动候选筛选（Step 2）

- 提取基线 GRT Tree 的热点/时序敏感区段；
- 实现 HPWL/RSMT/GCell 路径成本估计；
- 建立 HBT 密度/合法性近似指标；
- Top-K 候选搜索与可解释的打分日志。

**验收**：对所有公开用例产出合法候选；筛选时间可控。

### Milestone 4 — 3D 局部规划与 HBT 联合放置（Step 3–4）

- 实现受限 GCell 搜索、HBT 站点候选与路径替换；
- 实现 Net 冲突与全局资源更新；
- 在 Tree 拓扑上正确切出子网并回滚失败方案。

**验收**：对多个 MLS Net 的组合方案合法，能够稳定完成 DRT。

### Milestone 5 — 时序/拥塞驱动与反馈优化（Step 5–6）

- 接入 Criticality / Elmore 或增量 STA；
- 合并拥塞变化、线路 RC、HBT 预算和 Runtime 成本；
- 调参时使用完整 DRT + DRC + STA 的结果做离线回归。

**验收**：八个公开用例全部通过，并形成各指标/Runtime 的改善与退化分析；输出稳健参数配置。

### Milestone 6 — 泛化、鲁棒性与提交

- 测试不同规模（尤其大规模 `bp_quad`）的内存、峰值线程、时间上限；
- 设计无收益/不合法时的基线回退；
- 固化运行脚本、日志和版本；
- 检查评估器新增或更新的格式要求。

**验收**：不依赖公开用例名称的特例，所有输入异常和候选失败均可确定性处理。

---

## 12. 核心算法主循环（可直接转化为实现任务）

```python
def optimize_3d_grt(design_files, cfg):
    # 1. 基线与静态分析
    state = load_design_and_technology(design_files)
    original_snapshot = snapshot_immutable_design(state)
    baseline = run_or_import_baseline_grt(state)
    resource_graph = build_resource_graph(state, baseline)
    timing = analyze_timing(state, baseline)
    sites = build_hbt_site_map(state)

    # 2. 快速筛选：只对有限高价值 Net 开销较大的搜索
    candidates = generate_hotspot_segments(state, baseline, timing)
    ranked = rank_candidates(candidates, resource_graph, sites, cfg)
    ranked = ranked[:cfg.top_k]

    # 3. 三维规划：形成 2-HBT MLS proposals（此时不改 ODB）
    proposals = []
    for c in ranked:
        plans = plan_two_hbt_mls(c, resource_graph, sites, cfg)
        proposals.extend(plans[:cfg.max_plans_per_net])

    # 4. 全局协调 HBT/拥塞冲突
    accepted = choose_compatible_proposals(proposals, resource_graph, sites)
    accepted = local_refine_and_legalize(accepted, resource_graph, sites, cfg)

    # 5. 事务式切分和 die-local GRT
    for p in accepted:
        if not verify_proposal_before_commit(p, state):
            continue
        commit_subnets_and_hbts_transactionally(state, p)
    run_die_local_global_routing(state, cfg)
    merge_guides_into_odb(state)

    # 6. 完整硬约束校验和写出
    assert verify_non_hbt_placements(state, original_snapshot)
    assert verify_collapsed_netlist_equivalence(state, original_snapshot)
    assert verify_hbt_legality_and_counts(state)
    assert verify_die_local_guides_and_coverage(state)
    assert verify_no_detailed_wires(state)
    write_odb(state, "5_1_grt.odb")

    return "5_1_grt.odb"
```

> **实现注释**：伪代码强调任务边界与状态更新顺序。真实 OpenROAD 中应通过其正式对象生命周期/API 写网表和 Guides；还需考虑多个候选的互斥、已提交 MLS 对后续 Routing Capacity 的影响、失败事务回滚、跨 Net 重路由的全局收敛。

---

## 13. 风险、未确认事项与待核对清单

| 问题 | 当前结论 | 下一步动作 |
|---|---|---|
| 是否要自己实现 DRT？ | **不需要**。提交 GRT ODB，官方 TritonRoute 执行 DRT | 对齐 evaluator 版本 |
| 是否允许一个新 MLS 原始 2D Net 使用 4+ HBT？ | **当前 EDA_contest README 明确不允许**，要求恰好 2 个 | 限制生成器，加入断言 |
| 输入是否一定有现成 `5_1_grt.odb`？ | **不一定**。标准竞赛输入从 `4_1_cts.def` 等进入；`5_1_grt.odb` 是要求的 GRT 输出格式 | 以官方 `run-grt` 脚本为准 |
| 是否能从 `dbGuide` 得到精确 Baseline Tree？ | 不能保证；Guide 是矩形集合 | 尽量使用 FastRoute GSegment/segments 输出 |
| HBT 精确合法位置集合 | 必须依据实例几何、LEF、Track、Spacing 与评估器 | 先读 HBT Master/特殊脚本，再写 site legality |
| HBT 数量的官方归一化计分细则 | 赛题写明 30% 阈值，但精确改善率公式未在提供的 PDF 中展开 | 查 Q&A / 评分脚本 |
| Runtime 计量口径 | 当前 PDF 只给权重，未展开全部计时边界 | 查官方 evaluator / Q&A |
| 关键路径 RC 与增量 STA 的可调用接口 | OpenROAD 具有 STA/RC 能力，但比赛分支具体 API 需对齐 | 阅读 `OpenROAD-GRT` 的构建和源代码 |
| 原有 HBT 何种重定位可行 | 题目允许调整 HBT 位置；具体约束仍须按 evaluator | 首版先不移动已有 HBT，后续做协同优化 |
| 不同 Net 共享 Top Die 段时的全局资源与物理间距 | 需要统一容量和 DRC 风险模型 | 资源图增量记账 + 官方反馈 |

---

## 14. 参考资料与源信息

### 14.1 赛题原文件

用户上传 PDF：**《赛题五：时序驱动与 HB 布局协同的面对面键合 3D-IC 三维全局布线方法（华为海思）》**。重点：

- **第 2–3 页**：上下 Die 资源失衡、Metal Layer Sharing 原理、HBT Pitch 限制以及子网分配示例。
- **第 3–4 页**：MLS Net 选择与切分、HBT 增量布局、3D Routing 三项任务。
- **第 4–5 页**：六项评分权重与 DRC 类别；子网 Guide 不得跨 Die。
- **第 5–6 页**：TritonRoute / OpenSTA 评估；`5_1_grt.odb` 输出、不得改变非 HBT 布局、资源限制。

### 14.2 官方仓库与技术文档

1. **Open3DBench EDA_contest README**（**竞赛接口与命名/合法性约束的优先依据**）：  
   https://github.com/lamda-bbo/Open3DBench/blob/EDA_contest/README.md
2. **Open3DBench 仓库**：  
   https://github.com/lamda-bbo/Open3DBench
3. **OpenROAD Global Routing**（FastRoute、Guide、Route Segments、Incremental GRT）：  
   https://openroad.readthedocs.io/en/latest/main/src/grt/README.html
4. **OpenDB `dbGuide` API**：  
   https://openroad.readthedocs.io/en/latest/doxygen_output/html/classodb_1_1dbGuide.html
5. **论文**：Pentapati & Lim, *Metal layer sharing: A routing optimization technique for monolithic 3D ICs*, IEEE TVLSI 2022（作为理论参照，不取代本赛题的 F2F/HBT 限制）。
6. **算法参考**：FLUTE/RSMT、FastRoute、CUGR 的 detailed-routability-driven cost 思路，以及 `H3D`（见赛题 PDF 参考文献）。

### 14.3 官方快速运行命令（示意，依当前 README）

```bash
git clone --branch EDA_contest --single-branch \
  https://github.com/lamda-bbo/Open3DBench.git
cd Open3DBench

docker pull gaocr/3dbench-contest:20260914
# 将公开输入包解压到仓库 input/ 下，然后：
./start_contest_docker.sh

# 以下命令在竞赛容器内部执行
contest build 32
INPUT=/workspace/Open3DBench/input/open3dbench_8cases_post_hbt_input_20260724
contest run-grt bp_fe "$INPUT" baseline
contest evaluate bp_fe "$INPUT" \
  /workspace/Open3DBench/output/bp_fe/baseline \
  /workspace/Open3DBench/reports/bp_fe/baseline
cat reports/bp_fe/baseline/metrics.json
```

注意：实际数据包路径、镜像、评估器版本可能更新；记录使用的 commit/hash，避免因工具/输入版本变动造成指标不可比。

---

## 15. 最终工程决策（建议固定在项目 README）

**第一阶段的默认实现路线**：

1. **先保证评估合法性，再追求分数**：MVP 优先只对单个原始 2D Net 做“两 HBT → 三个子网 → GRT Guide”，验证官方 evaluator 能跑通。
2. **不把超图二划分当作主求解器**：以 Net/Pin 属性超图做排序与候选发现；用 Steiner Tree 的路径分段实现真正的 MLS。
3. **Step 2 只估算，Step 3 才搜索**：避免在所有 Net 上执行重型三维图算法。
4. **精度逐层提高**：HPWL/Manhattan → Baseline Tree → 局部 GCell Shortest Path → 少数候选的 3D Maze Search → 最终官方 DRT 反馈。
5. **HBT 两端同等重要**：计数、位置、RC 和 HBT 周围的 DRC/拥塞风险都纳入决策；建立全局 Site Map 防止方案互相冲突。
6. **保留 FastRoute 作为成熟主引擎**：优先对 MLS 策略、子网修改及局部代价模型创新，避免 Runtime 和代码复杂度失控。
7. **必须保留无 MLS 基线回退**：对于预计负收益、HBT 不合法、时序恶化、超时或 GRT 无解的 Net，保留原样更稳健。

**一句话概括**：利用属性超图判断“哪些 Net 值得借用别的 Die”，利用二维 Steiner Tree 找出“哪些路径值得迁移”，利用受约束的三维 Resource Graph 与两 HBT Site 搜索决定“如何迁移”，最后通过 Die-local FastRoute 生成**合法、连通、可详细绕线**的 GRT Guides，并以官方 DRT/STA 结果闭环优化。
