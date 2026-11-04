# OptAgent 本地 Codex 版

本分支把 Reaslab OptAgent 的优化建模、求解和独立核验工作流接入本地 Codex。Codex 负责理解请求与协调；Case 状态、证据读写、求解执行和 Case 导出由仓库内的 Python Core 与本机进程完成。

本地版不依赖 ReasLab 托管平台、manager HTTP、python-execute、Docker 服务或远端求解器。Codex 模型请求仍遵循用户当前 Codex provider 配置；本仓库只保证 Core 与 solver 的执行路径在本机。

## 安装和启动

需要：

- 已安装并登录的 Codex CLI，支持项目 AGENTS.md、`.agents/skills/` 与原生自定义子 Agent。项目级 `.codex/config.toml` 仅在用户信任该项目后加载。
- Python 3.11 或更新版本。
- 用户所选求解器的 Python 接口、系统二进制和许可（若需要）。
- 若要求 PDF，还需要本机 TeX 环境；中文模板通常使用 XeLaTeX，建议安装 latexmk。

在仓库根目录创建或启用适合本项目的 Python 环境，然后安装 Core。Windows PowerShell 示例：

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -e .

macOS/Linux 示例：

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -e .

如需使用 Highs 默认依赖组，可安装 Pyomo、highspy 和 NumPy：

    python -m pip install -e ".[highs]"

在同一个已激活项目环境的 shell 中，从仓库根目录启动 Codex：

    codex

首次在 Codex 中打开仓库时，按本机提示信任该项目；项目未受信任时，Codex 不加载仓库的 `.codex/config.toml`，`opt-verifier` 角色也就不会注册。该文件只注册 `.codex/agents/opt-verifier.toml`，不固定模型、推理强度、审批策略或权限；这些仍使用用户当前 Codex 配置。若从 Codex 桌面端或另一个 shell 启动，应明确使用项目 `.venv` 中的解释器，或该进程已经可用的 `optagent-local` 入口；不要假定通用系统 `python` 已安装本项目 Core。下文的 `python` 命令均指当前选定的项目 Python。

## 处理一个优化问题

将题面和输入文件放在当前项目工作区内，可以使用任意可读文件名和目录。`case_create` 通过 `--capture-file` 显式导入工作区内的 UTF-8 题面文件。当前 Core 初始导入支持 UTF-8 文本、CSV、严格 JSON 和显式 `whitespace_table`；其他格式需由实际 ingestion 实现并核验，安装相关 Python 包本身不代表 Core 已支持该格式。不要求建立固定名称的 `Problem.md`。

典型请求：

    读取 inputs/problem.md 和 data/demand.csv，建立并求解成本最小化模型。
    先向我展示变量、定义域、目标、约束和关键假设，再编写模型代码。
    实际运行求解并独立核对方案、目标值和约束，默认给我 Markdown 报告。
    若生成 PDF，请使用本机 TeX 编译并读取最终 PDF。

首次写建模或求解代码前，OptAgent 会先生成短 model.md，列出变量与定义域、目标方向和单位、约束族、必要符号和假设；随后在对话中展示可点击的文件路径和数学说明，再继续写代码，不等待额外审批。模型语义变化时先更新并再次展示。即使用户只要代码，仍先展示这份短数学说明；完整报告只在用户要求时生成，未指定格式时默认为 Markdown。

材料性题意或数据事实缺失且会改变目标或可行域时，Codex 会提出具体问题。`case_question` 和 `case_answer` 都要求通过 `--capture-file` 显式导入工作区内的 UTF-8 文本文件；实际收到答复后，将答复写入工作区文件，再把该文件提交给 `case_answer`。CLI 会为答复生成并传递 `capture_id`。这类输入的来源标为 `workspace_file`，且 `host_capture_authenticated=false`；它们不表示宿主聊天记录已被自动捕获或认证。

## 本地 Core CLI

Core 操作必须在仓库根目录通过本地 CLI 执行；当前工作目录就是工作根，没有 `--workspace` 参数。未指定 `--session-id` 时，CLI 会在 `.reaslab/optagent/local-session.json` 中为工作区保存一个稳定的本地 ID；同一工作区操作时可以继续省略。跨工作区访问已有 Case 时，从该 Case 状态读取准确的 `owner_session_id` 并显式传入。调用参数以 UTF-8 JSON 请求文件提供。

    python -m optagent_core.local_cli --session-id <owner_session_id> --role root call <operation> --json-file <request.json>

CLI 当前公开 28 个精确 operation 名称；使用 `schema <operation>` 查看单个 operation 的输入 JSON Schema，例如：

    python -m optagent_core.local_cli schema optagent.case_create

`call` 使用 schema 中对应的 operation，例如 `optagent.case_inputs`、`optagent.case_create`、`optagent.case_question`、`optagent.case_answer`、`optagent.run_prepare`、`optagent.run_start`。`--capture-file` 仅用于 `case_create`、`case_question` 和 `case_answer`：前者导入题面；问答操作导入显式的本地 UTF-8 输入文件，答复的 `capture_id` 由 CLI 根据该文件生成并传入 Core。Schema 将这些问答文件标为 `workspace_file`，并明确它们不是经认证的宿主 Capture。其他业务数据按 `data_capture`、`data_normalize` 等公开 operation 的自身 schema 显式录入；不要把这些数据操作的路径字段替换为 `--capture-file`。CLI 不读取宿主聊天轮次或隐式控制消息。CLI 将 JSON envelope 写到 stdout，并以退出码区分调用成功、操作失败和输入错误；本机 worker 的 stdout/stderr 会记录在运行凭据中。运行是否可行、是否最优仍以 Core 记录及独立检查证据为准。

run_prepare 必须显式提供 execution_budget_seconds、relative_gap 和 absolute_gap。Candidate 必须声明 backend_id。Core 会探测本地所选接口并记录实际结果；不能从安装包名称或 solver 注册表推断后端可用，也不自动安装缺失求解器。

独立核验使用本机 Codex 的 `opt-verifier` 子 Agent。它由 `.codex/config.toml` 注册，并加载 `.codex/agents/opt-verifier.toml`；首次使用前须信任该项目。启动新审查时，使用当前客户端支持的原生协作能力；若客户端提供新上下文选项则选择它。只传递 Case ID、`owner_session_id`、核验范围与必要 Source refs。终稿或返工需要继续时，实际唤醒原核验者；客户端不支持继续时，另启核验上下文并标明重审范围。Verifier 与 Owner 对同一 Case 使用相同 `owner_session_id`，CLI 参数将 role 改为 `opt-verifier`。role 和 session 是 Core 的流程身份标签，不构成操作系统级 ACL，也不提供安全隔离。

## Case 与结果

项目级内部状态和不可变证据存放在：

    .reaslab/optagent/

Case 导出的可编辑用户交付存放在 Core 返回的 artifact_dir，常见内容包括 Problem.md、model.md、README.md、code/、data/、report 或 paper/。实际路径与文件以本次 CLI 返回为准，不能从 Case ID 猜路径。

项目内部的 .reaslab/optagent/ 与可编辑 Case 内部的 .optagent/ 用途不同：

- .reaslab/optagent/ 保存 Core Case 状态、来源绑定、Candidate、Run、Result、Review 和 Delivery 证据。
- 可编辑 Case 根目录中的 .optagent/ 保存该 Case 独立 solve/check 所需的运行配置与 helper。复制或移交该 Case 时必须保留。

可编辑 Case 的本机运行方式使用当前选定的项目 Python：

    cd <artifact_dir>
    python run.py solve
    python run.py check

solve 的可读方案与摘要分别位于 results/local/solution.json 和 results/local/summary.md；执行记录位于 results/local/.optagent/execution.json。check 摘要位于 results/check/summary.md，检查记录位于 results/check/.optagent/check.json。修改输入或代码后，应重新运行 solve 和 check；本地 Case 的新结果不会自动改变原 Core Review 的绑定证据。

明确要求可复现 ZIP 时，按导出 README 的说明运行；标准入口为：

    python -I reproduce/run.py --output-dir <新的绝对路径>

## 求解器和许可

backend_id 必须在 Candidate 中明确指定，没有隐式默认求解器。Core 会针对当前运行时探测接口并记录能力、版本、状态和失败原因。一个 solver 出现在 schema、技能参考、pip 包列表或历史日志中，并不证明当前机器可用。

Highs 安装组提供 Pyomo、highspy 和 NumPy。其他后端可能需要额外 Python 包、外部二进制、环境变量或商业许可。Gurobi、CPLEX、Xpress、MOSEK 等商业后端必须使用用户本机合法配置的许可证；不可用时按实际失败记录，不能伪称支持或自动切换到语义不同的模型。

完整报告默认使用 Markdown。用户明确要求 LaTeX/PDF 时使用随 opt-writing 技能提供的中英文模板，在本机安装的 TeX 工具下构建，并核对实际 PDF；存在 .tex 或编译命令成功都不能代替读取 PDF 成品。模板不包含 TeX 系统、字体或外部求解器。

## 本地路径和安全边界

- `.agents/skills/` 存放随仓库提供的 `opt-workflow`、`opt-review`、`opt-report` 和 `opt-writing`；Codex 从此项目技能目录发现技能。
- `.codex/config.toml` 注册专职 `opt-verifier` 角色，`.codex/agents/opt-verifier.toml` 提供其指令。两者都不固定用户模型、effort、sandbox、审批或权限。
- Core 的 role/session 校验是工作流身份管理，不是强安全边界；同一用户可在本机提供其他角色值。
- `.reaslab/optagent/` 与本地凭证、密钥和运行临时文件不应提交到公开仓库。仓库忽略本地 Core 状态、环境文件、虚拟环境、Python 缓存与 pytest 产物。
- 只清理自己在本任务中创建且已确认不再使用的临时材料；保留用户输入、正式代码、Case 运行闭包和证据。

## 许可证

当前仓库没有顶层 LICENSE 文件，也没有声明第一方代码和 skills 的通用再分发许可。没有在本次迁移中添加或推断 MIT、Apache 等许可。Core 内附的第三方 schema 文件及其 LICENSE/NOTICE 按原样保留，并应与第一方代码许可分别理解。
