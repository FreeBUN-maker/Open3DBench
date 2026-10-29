# 竞赛布线优化与辅助脚本

新增的自动 MLS 初版位于 [`mls_v1/`](mls_v1/README.md)：支持候选筛选、
两 HBT 选址、分支子网切分及现有 FastRoute 流程；另附
[优化空间](mls_v1/OPTIMIZATION.md)、[论文与开源参考](mls_v1/REFERENCES.md)
和[实际验证范围](mls_v1/VALIDATION.md)。下面保留原迁入工具的使用说明。

从 `3DIC-routing` 迁入的竞赛 GRT 工具。原目录名是 `algorithms/`，
其中的优化算法只有 `timing1c`。算法实现及其运行入口集中在同名目录；
通用编译、baseline 运行和评估入口位于 `scripts/`。

```text
opt-code/
├── baseline/                  # 两个 Die 分别布线的共享 OpenROAD GRT overlay
│   ├── openroad_src/          # GRT C++/Tcl/SWIG 源码覆盖文件
│   └── flow_scripts/          # 布线流程、guide 合并及合法性诊断脚本和原有测试
├── timing1c/                  # 时序驱动 Metal Layer Sharing (MLS)
│   ├── prepare_timing_mls.tcl
│   └── run_timing1c.sh
└── scripts/
    ├── common.sh              # 路径解析、镜像校验、Docker 挂载
    ├── build.sh
    ├── run_baseline.sh
    └── evaluate.sh
```

`baseline/` 来自原仓库的 `OpenROAD-GRT/`，是运行这两种方法必需的依赖。
`timing1c` 在载入设计后选取时序关键分支，放置两个合法 HBT，借用上层
金属，再由共享 baseline 完成两个 Die 的布线和 guide 合并。迁移保留
原算法和默认参数：`TMLS_MAX_NETS=1`、`TMLS_TARGET_NET=net1388`。
原项目只验证了 `bp_fe`，运行与评估入口继续保留此限制。

## 运行

在 Open3DBench 仓库根目录执行：

```bash
./opt-code/scripts/build.sh 32
./opt-code/scripts/run_baseline.sh baseline_recheck
./opt-code/scripts/evaluate.sh baseline_recheck

./opt-code/timing1c/run_timing1c.sh timing1c_recheck
./opt-code/scripts/evaluate.sh timing1c_recheck
```

首次运行前需要编译迁入的 GRT overlay；修改 `baseline/openroad_src/` 后
也需要重新编译。Tcl/Python 流程文件和算法准备脚本在下一次运行时生效。
编译默认使用 32 个任务，也可通过位置参数或 `BUILD_JOBS` 设置。

所有入口根据脚本位置解析路径，因此也可从其他工作目录使用绝对路径调用。
默认不必设置 `OPEN3DBENCH_ROOT`，数据和生成文件位于本仓库：

```text
Open3DBench/
├── input/open3dbench_8cases_post_hbt_input_20260724/
├── .contest/                  # 构建缓存和流程工作目录
├── output/bp_fe/<label>/5_1_grt.odb
└── reports/bp_fe/<label>/metrics.json
```

仍可通过 `OPEN3DBENCH_ROOT=/absolute/path/to/workspace` 指向其他数据
工作区；`OPEN3DBENCH_INPUT_NAME` 可覆盖 `input/` 下的输入包目录名。
外部工作区同样使用此处 `baseline/` 和 `timing1c/` 的代码。

容器内工作区固定为 `/workspace/Open3DBench`。共享 overlay 只读挂载到
`/workspace/Open3DBench/OpenROAD-GRT-HBT`，以匹配竞赛镜像的接口；
算法目录额外只读挂载到 `/workspace/Open3DBench/opt-code/timing1c`，
`GRT_PREPARE_TCL` 指向其中的 `prepare_timing_mls.tcl`。
算法候选报告继续通过流程提供的 `RESULTS_DIR` 写入
`timing_mls_candidates.tsv`，不依赖原仓库路径。

## 镜像依赖

保留原项目的镜像版本和校验规则，默认使用
`gaocr/3dbench-contest:20260914`，要求本机 Docker 可访问且镜像已加载。
输入和镜像需来自与原项目匹配的 2026-09-15 离线包。允许的镜像 ID 为：

```text
sha256:eb877c5e1f4d94881992b55d474ab189ab3308dac3f38208c5c8b0dd7544bd98
sha256:ac5b1f7476f5d05485a67a0d39e031e9e34410158d875912a0568b2b51126623
```

镜像标签可通过 `OPEN3DBENCH_CONTEST_IMAGE` 覆盖。同名标签不保证镜像
内容相同；若有意使用其他镜像 ID，需显式设置 `ALLOW_UNVERIFIED_IMAGE=1`，
并重新验证结果。迁移本身不更新镜像，也不沿用原项目的性能结果作为本地
运行结果。评估完成后应检查 `metrics.json` 中的 `legal` 字段。

## 检查迁入的流程

```bash
python3 -B -m unittest discover \
  -s opt-code/baseline/flow_scripts/scripts_3D/tests -v
```
