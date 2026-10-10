# OptAgent Codex Bundle

一个在本地 Codex 中运行的优化建模智能体，可以从读取题目和数据开始，完成数学建模、求解代码编写与执行、独立验证和报告生成。

最后更新：**2026-10-10**

## 开始使用

### 1. 准备 Codex

安装并登录 Codex CLI，使用支持项目配置和原生多智能体角色的版本。然后获取本项目并启动：

```bash
git clone https://github.com/reaslab/OptAgent-CodexBundle-Public.git
cd OptAgent-CodexBundle-Public
codex
```

已经下载项目时，也可以从任意位置运行：

```bash
codex -C /path/to/OptAgent-CodexBundle-Public
```

如果 Codex 询问是否信任该目录，请确认信任，并检查项目配置是否已加载。项目中的 `.codex/`、`.agents/` 和 `AGENTS.md` 应当完整保留，它们提供协调流程、角色配置、技能和本地工具。

本项目直接使用本地 Python 和工具运行。普通使用无需部署 ReasLab 平台或 Docker 服务。

### 2. 放入题目和数据

建议将题目保存为项目根目录下的 `Problem.md`，把数据放入 `data/`，例如：

```text
OptAgent-CodexBundle-Public/
├── Problem.md
└── data/
    ├── demand.csv
    ├── capacity.csv
    └── cost.xlsx
```

也可以提供 PDF、Word、Excel、图片或其他文本文件，让 Codex 先读取并整理题目。具体读取能力取决于本地工具，扫描版文件可能需要 OCR。

题目中尽量写明决策目标、必须满足的约束、数据字段含义和单位。不要修改原始数据；生成的代码、结果和报告会保存在对应案例目录中。

仓库已经带有示例题目和历史案例。处理自己的问题时，请明确指定文件，避免将示例数据当成当前任务输入。

### 3. 告诉 Codex 要完成什么

完整解决一个优化问题并生成中文报告：

```text
完整解决 Problem.md 中的优化问题，数据在 data/。
实际运行求解代码，独立检查目标值和约束，使用中文撰写报告并编译最终 PDF。
```

从其他格式的题目开始：

```text
读取 problem.pdf，数据在 data.xlsx。
整理题目和数据，完成优化建模、实际求解、独立验证和最终报告。
```

指定求解器和求解时间：

```text
求解这个生产计划问题，优先使用 PuLP/CBC，单次求解时间上限为 300 秒。
报告目标值、可行性、求解状态，以及是否已经证明最优。
```

先审阅方案再执行：

```text
先分析 Problem.md，给出建模方案、关键假设和求解计划。
等待我确认后再开始建模和运行。
```

只生成 Markdown 报告：

```text
完整求解并验证这个问题，只输出 Markdown 技术报告，不生成 LaTeX 和 PDF。
```

继续修改已有案例：

```text
在 case_001_base 的基础上，将仓库容量提高 20%，重新求解并验证。
比较调整前后的总成本和配送方案。
```

任务中可以继续指定报告语言、求解器、时间限制、精度要求、必须保留的约束和最终文件格式。

## 运行过程

完整任务通常会依次完成：

1. 读取题目和数据，明确目标、约束与必要假设；
2. 检索内置建模知识，建立变量、目标函数和约束；
3. 编写求解代码，检查可用后端并实际运行；
4. 独立复核目标值、约束和模型与题目的对应关系；
5. 根据诊断修正模型或代码，必要时重新求解；
6. 撰写报告、尝试编译 PDF，并检查交付文件。

项目已经配置 Coordinator、Formulator、Solver、Verifier 和 Reporter。Coordinator 管理整体流程，其余角色分别负责建模、求解、独立验证和报告，按依赖顺序工作。用户不需要手动启动这些角色。

普通求解请求默认自动推进到验证和报告交付。如果希望在执行前亲自审核模型、假设或计划，请在任务开始时明确要求等待确认。

OptAgent 会区分可行解、已证明的最优解、时间限制、不可行、无界以及环境或程序错误，并根据实际证据说明验证范围。

## Python 和其他依赖

项目需要可用的本地 Python。建议使用 Python 3.11 或更新版本，并按当前任务安装所需的计算库和求解器。

仓库提供角色配置、工具脚本和求解模板，不包含已经安装好的 Python 环境、商业求解器许可证或完整 TeX 环境。Codex 会检查本地解释器、求解器及其可用性；具体安装方式根据操作系统和现有环境选择。

当前提供自动代码模板的后端包括：

| 后端 | 主要用途 | 环境要求 |
| --- | --- | --- |
| `pulp` | 线性与混合整数规划 | PuLP 及可用的 CBC |
| `gurobipy` | 线性、整数和 Gurobi 支持的其他规划 | Gurobi Python 接口及有效授权 |
| `ortools-linear` | OR-Tools 线性求解接口 | OR-Tools 及适合问题的后端 |
| `ortools-cp-sat` | 整数与约束规划 | OR-Tools |
| `cvxpy` | 凸优化建模 | CVXPY 及适用的求解器 |
| `pyomo-cbc` | 通过 Pyomo 求解线性与整数模型 | Pyomo 和 CBC |
| `pyomo-ipopt` | 通过 Pyomo 求解适用的连续非线性模型 | Pyomo 和 Ipopt |

实际选择取决于模型结构、运行环境和用户要求。注册表中列出的其他求解器不等于已经提供自动生成模板；商业求解器还需检查授权和规模限制。

生成中文报告 PDF 通常需要 XeLaTeX，建议同时安装 `latexmk`，并准备模板所需字体和宏包。英文报告也需要可用的 TeX 编译环境。模板含有 `ctex` 等中文支持，编译时应核对实际使用的引擎。

如果只需要 Markdown 报告，可以在任务开始时明确指定，无需安装 TeX。读取文件、处理数据和绘图可能还需要 pandas、openpyxl、NumPy、Matplotlib 等库，按任务需要配置即可。

## OptGraph 建模参考

OptGraph 是随项目附带的本地优化建模知识图谱，收录问题类别、标准模型、约束模式、变量规则、参考案例、典型错误、修正建议和验证检查项。

建模时，OptAgent 会根据题目特征检索相关结构，作为模型选择和约束检查的参考。对于带有模型蓝图的条目，可以在核对适用条件后绑定当前数据，构建具体模型。

本地检索采用确定性规则，不需要下载向量模型。当本地匹配不足时，检索工具默认尝试从允许的官方来源补充参考案例，并保存缓存；网络不可用时保留本地结果。需要完全离线运行时，可以明确要求关闭网络回退，工具也提供 `--no-network-fallback` 选项。

图谱中的教学案例、官方来源案例和已复现结果有各自的来源与验证标记。找到参考代码或相似模型后，仍需检查它是否覆盖当前题目的约束，并实际求解和验证。

知识结构、来源及检索说明见 [OptGraph README](https://github.com/reaslab/OptAgent-CodexBundle-Public/blob/main/.agents/skills/opt-formulation/knowledge/README.md)。

## 在哪里查看结果

每个任务按案例保存在：

```text
OptAgent-CodexBundle-Public/cases/<案例名称>/
```

默认 LaTeX 报告任务的目录通常包括：

```text
cases/case_001_base/
├── README.md
├── Problem.md
├── code/
│   ├── README.md
│   ├── run.py
│   ├── requirements.txt
│   └── data/
├── outputs/
└── paper/
    ├── main.tex
    ├── sections/
    └── main.pdf
```

其中：

- `README.md`：案例入口、当前结果和文件导航；
- `Problem.md`：案例题目；
- `code/`：可复跑代码、依赖、公开输入快照和适用的模型导出；
- `outputs/`：实际结果表、图表、数据导出和附件；
- `paper/`：LaTeX 源码，以及编译成功后生成的 `main.pdf`。

明确选择 Markdown 时，最终报告保存在 `report/final.md`，无需创建 LaTeX 文稿或 PDF。当前版本默认选择 LaTeX 报告，Markdown 是可选的单格式交付方式。

内部状态、结构化模型、运行日志、验证诊断和决策记录保存在隐藏的 `.optagent/` 目录中。日常查看结果，从对应案例的 `README.md` 进入即可。

求解代码的统一入口为 `code/run.py`。独立复跑前，先阅读该案例的 `code/README.md`，按照依赖、后端、输入和参数说明运行。

继续修改时，直接告诉 Codex 当前案例和要修改的内容，不需要重新开始整个问题。更换题目、调整参数、重新验证或只修订报告，会根据影响范围进入相应流程。

## 常见情况

**Codex 没有加载角色或 skills**

确认启动时进入了本项目目录，或使用了 `codex -C /path/to/OptAgent-CodexBundle-Public`。检查目录信任、Codex 版本，以及 `.codex/config.toml`、`.codex/agents/` 和 `.agents/skills/` 是否完整。

**无法读取 PDF、Word 或 Excel**

把报错交给 Codex，它会检查所需的本地读取工具。扫描版 PDF 和图片可能需要 OCR，复杂表格需要核对提取结果。

**求解器无法运行或提示许可证问题**

让 Codex 检查实际使用的 Python 环境、求解器和授权。必要时选择能保持模型含义的可用后端；安装 Python 接口后仍需确认求解器能够实际运行。

**求解结束但没有证明最优**

查看求解状态、可行解、界限和最优性间隙。可以要求增加时间预算、调整模型或比较其他后端。已有可行解与最优性证明需要分别报告。

**模型被判定不可行**

要求检查数据、单位、变量边界和冲突约束。若允许放宽要求，请明确指出可以调整哪些约束；报告应说明调整及其影响。

**报告没有生成 PDF**

让 Codex 检查 XeLaTeX、`latexmk`、字体、宏包和编译记录，并继续修复。当前版本在编译不可用或失败时可以保留完整 LaTeX 源码并说明状态；文件夹中有源码不代表已经生成 PDF。

**网络不可用或无法补充参考案例**

使用本地 OptGraph 继续建模，必要时明确关闭网络回退。在线参考未获取成功不能当成已核对的证据。

**想比较多个方案或进行敏感性分析**

指出原案例、调整项和比较指标，要求实际运行必要的变体。比较、敏感性、基准和 Pareto 等分析会建立相应案例，表格放在 `outputs/`，解释写入所选报告格式。

## 贡献者

<table>
  <tr>
    <td align="center"><a href="https://github.com/optsuite"><img src="https://github.com/optsuite.png?size=96" width="80" alt="Wen Zaiwen"><br><sub><b>Wen Zaiwen</b></sub><br><sub>@optsuite</sub></a></td>
    <td align="center"><a href="https://github.com/TraFifholz"><img src="https://github.com/TraFifholz.png?size=96" width="80" alt="Zheng Libin"><br><sub><b>Zheng Libin</b></sub><br><sub>@TraFifholz</sub></a></td>
    <td align="center"><a href="https://github.com/ChenYuxuan-maple"><img src="https://github.com/ChenYuxuan-maple.png?size=96" width="80" alt="Chen Yuxuan"><br><sub><b>Chen Yuxuan</b></sub><br><sub>@ChenYuxuan-maple</sub></a></td>
    <td align="center"><a href="https://github.com/glhglhglh6"><img src="https://github.com/glhglhglh6.png?size=96" width="80" alt="Guo Luhong"><br><sub><b>Guo Luhong</b></sub><br><sub>@glhglhglh6</sub></a></td>
  </tr>
</table>

## 来源与许可证

本项目是 [ReasLab OptAgent](https://github.com/reaslab/Reaslab-OptAgent) 工作流的本地 Codex 配置包，包含原生角色、领域技能、工具脚本、求解模板和 OptGraph 建模知识。

OptGraph 中的部分参考案例来自 [Google OR-Tools](https://github.com/google/or-tools) 和 [Gurobi Modeling Examples](https://github.com/Gurobi/modeling-examples)，各条目保留来源与验证信息。第三方软件和材料遵循各自的许可证与授权条款。

当前仓库未附顶层 `LICENSE` 文件，项目许可证以维护者后续发布的声明为准。
