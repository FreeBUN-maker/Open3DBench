# 相关算法、论文与开源实现

检索核对日期：2026-10-29。优先列论文作者页面、出版商及项目仓库。
“已使用”区分新增规划器和复用后端；“可优化”是可借鉴的思想，不表示已经接入。

## 初版实际使用的算法与基础设施

| 资料 | 对应实现与用途 |
|---|---|
| R. C. Prim, **Shortest Connection Networks and Some Generalizations**, Bell System Technical Journal 36(6):1389–1401, 1957。[Bell Labs 页面](https://www.nokia.com/bell-labs/publications-and-media/publications/shortest-connection-networks-and-some-generalizations/)，[DOI](https://doi.org/10.1002/j.1538-7305.1957.tb01515.x) | `planner.py::rmst`：用 Manhattan 边权构造 MST；初版的初始连接树，不是 RSMT。 |
| P. E. Hart, N. J. Nilsson, B. Raphael, **A Formal Basis for the Heuristic Determination of Minimum Cost Paths**, IEEE Transactions on Systems Science and Cybernetics 4(2):100–107, 1968。[作者 PDF](https://ai.stanford.edu/~nilsson/OnlinePubs-Nils/PublishedPapers/astar.pdf)，[DOI](https://doi.org/10.1109/TSSC.1968.300136) | `Grid.maze`：带 Manhattan 下界启发函数的限域 A*。搜索有预算，只对局部粗图求路径。 |
| W. C. Elmore, **The Transient Response of Damped Linear Networks with Particular Regard to Wideband Amplifiers**, Journal of Applied Physics 19:55–63, 1948。[论文 PDF](https://courses.ece.ucsb.edu/ECE125/125_W11Banerjee/Lectures/JApplPhys_19_55.pdf)，[DOI](https://doi.org/10.1063/1.1697872) | `elmore`：RC 树一阶延迟代理，比较切分前后所有 sinks。实际 TNS/WNS 仍由 OpenSTA 给出。 |
| M. Pan, Y. Xu, Y. Zhang, C. Chu, **FastRoute: An Efficient and High-Quality Global Router**, VLSI Design, Article 608362, 2012。[DOI](https://doi.org/10.1155/2012/608362)，[FastRoute 源码与文献说明](https://github.com/ieee-ceda-datc/OpenROAD-Research/blob/master/src/grt/src/fastroute/README.md)，[OpenROAD GRT 文档](https://openroad.readthedocs.io/en/latest/main/src/grt/README.html) | 复用 `opt-code/baseline`，负责真实逐层 GRT、拥塞重布线与 guide 生成；不是本次重写的 router。 |
| C. Chu, Y.-C. Wong, **FLUTE: Fast Lookup Table Based Rectilinear Steiner Minimal Tree Algorithm for VLSI Design**, IEEE TCAD 27(1):70–83, 2008。[开源实现](https://github.com/The-OpenROAD-Project-Attic/flute3) | FastRoute 体系中的 Steiner 基础；新增 Python 规划器尚未接入，可用它升级 RMST。独立旧库与当前 OpenROAD 集成版本需注意 API 差异。 |
| [OpenSTA 官方仓库](https://github.com/The-OpenROAD-Project/OpenSTA) | `prepare.tcl::criticalities` 使用 OpenROAD 集成的 STA 接口提取有限的负 slack 路径；不自行实现完整 STA。 |
| A. B. Kahng, L. Wang, B. Xu, **TritonRoute: The Open-Source Detailed Router**, IEEE TCAD 40(3):547–559, 2021（2020 online）。[DOI](https://doi.org/10.1109/TCAD.2020.3003234)，[当前 OpenROAD DRT 代码](https://github.com/The-OpenROAD-Project/OpenROAD/tree/master/src/drt)，[官方说明](https://openroad.readthedocs.io/en/latest/main/src/drt/README.html) | 官方 evaluator 使用的 DRT/DRC 基础，便于研究 pin access、via、guide 与最终可布通性的关系；不是本次新增算法的一部分。旧独立 TritonRoute 仓库已归档。 |
| Y. Shi et al., **Open3DBench: Open-Source Benchmark for 3D-IC Backend Implementation and PPA Evaluation**, arXiv:2503.12946, 2025，v2 于 2026 更新。[论文](https://arxiv.org/abs/2503.12946)，[竞赛分支](https://github.com/lamda-bbo/Open3DBench/tree/EDA_contest) | 赛题输入、两 Die GRT 和固定评估接口。竞赛硬约束以实际输入版本、README、Q&A 和 evaluator 为准。 |

## MLS、HBT 布局和可采用的优化算法

| 资料 | 建议借鉴什么 | 迁移边界 |
|---|---|---|
| S. Pentapati, S. K. Lim, **Metal Layer Sharing: A Routing Optimization Technique for Monolithic 3D ICs**, IEEE TVLSI 30(9):1355–1367, 2022。[作者 PDF](https://gtcad.gatech.edu/www/papers/gtcad-tvlsi22-a.pdf)，[DOI](https://doi.org/10.1109/TVLSI.2022.3181185) | MLS 的资源失衡、时序与垂直互连代价分析，理解为什么选网与 HBT 数量必须协同。 | 论文讨论 monolithic 3D；本赛题是 F2F，HBT pitch/RC 和恰好两 HBT 的约束需重新施加。 |
| S. Pentapati, S. K. Lim, **Routing Layer Sharing: A New Opportunity for Routing Optimization in Monolithic 3D ICs**, ISPD 2022。[作者 PDF](https://gtcad.gatech.edu/www/papers/Sai-ISPD22.pdf) | MLS 的早期问题建模、共享层配置与跨层连接数量控制。 | 可与上述扩展期刊论文连读，不直接照搬工艺与性能收益。 |
| Y. Zhao, F. Gu, S. Liu, P. Liao, B. Yu, **H3D: Heterogeneous Resources Aware Global Router for Face-to-Face Bonded 3D ICs**, ICCAD 2025。[作者 PDF](https://www.cse.cuhk.edu.hk/~byu/papers/C297-ICCAD2025-3DRoute.pdf)，[DOI](https://doi.org/10.1109/ICCAD66269.2025.11240944) | 最贴近本题：异构 routing/HBT 网格、3D Steiner tree、HBT 数量优化与合法位置重定位的动态规划。 | 尚未确认作者公开代码仓库；提供已核实的论文。DP 必须加入本题的两 HBT 状态和原始端点 die 固定约束。 |
| S. Liu et al., **Routing-aware Legal Hybrid Bonding Terminal Assignment for 3D Face-to-Face Stacked ICs**, ISPD 2024；扩展版 TODAES 31(3), Article 48, 2026。[作者文献目录](https://www.cse.cuhk.edu.hk/~byu/publications.html) | 把 HBT 合法分配与 routing 成本联合考虑，改进独立最近站点选址。 | 单 HBT assignment 与本题成对新增 HBT 的组合约束不同，需要拓展为候选对或分解子问题。 |
| J. Liu, C.-W. Pui, F. Wang, E. F. Y. Young, **CUGR: Detailed-Routability-Driven 3D Global Routing with Probabilistic Resource Model**, DAC 2020。[作者项目仓库](https://github.com/cuhk-eda/cu-gr) | 概率资源模型、3D pattern routing、多层次 maze routing、面向详细布线的 guide 质量。 | 此处“3D”主要指金属层维度，跨 die/HBT 的异构容量和切分约束仍需自建。 |
| L. McMurchie, C. Ebeling, **PathFinder: A Negotiation-Based Performance-Driven Router for FPGAs**, FPGA 1995:111–117。[DOI](https://doi.org/10.1145/201310.201328)，[作者项目页](https://homes.cs.washington.edu/~ebeling/) | 当前拥塞 + 历史拥塞价格、迭代协商，减少逐网贪心的局部最优。 | 来源是 FPGA routing，只借鉴价格更新机制；不能当作可直接替换本题的 ASIC router。 |
| S. Liu et al., **FastGR: Global Routing on CPU–GPU With Heterogeneous Task Graph Scheduler**, IEEE TCAD 42(7):2317–2330, 2023。[作者文献目录](https://www.cse.cuhk.edu.hk/~byu/publications.html)，[DOI](https://doi.org/10.1109/TCAD.2022.3217668) | pattern routing 批处理和任务依赖调度，减少 runtime。 | 未确认原论文的独立公开仓库；赛题只保证 CPU 环境，不将 GPU 加速作为正式运行依赖。 |
| **InstantGR: Scalable GPU Parallelization for Global Routing**。[作者项目仓库](https://github.com/cuhk-eda/InstantGR)，[论文 PDF](https://shijulin.github.io/files/1239_Final_Manuscript.pdf) | 可实际阅读的 GPU global routing 开源实现，研究批处理和并行分解。 | 研究参考，不能直接假定符合本题 CPU/线程和 HBT 约束。 |

建议阅读顺序：**赛题/本地 baseline → FastRoute/FLUTE → MLS → H3D → HBT assignment → CUGR → 时序和运行时间优化**。
其中 `H3D` 与 `CUGR` 的方法简介来自原论文/官方项目；对本代码的迁移建议是本次实现的工程判断。
