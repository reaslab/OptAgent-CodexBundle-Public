# OptAgent 本地 Codex 工作区

本仓库把优化建模与验证工作流接入本地 Codex。只有用户请求优化建模、求解、验证或相关报告时才使用 Case 工作流；代码维护、安装帮助、一般解释和其他任务继续按普通 Codex 方式处理。优化 Case 状态、证据、求解与导出由仓库内的 Python Core 和本机进程完成，不依赖 ReasLab 托管平台、python-execute 或 manager HTTP。

## 工作方式

- 读取原题和实际数据，保留真实来源、索引、单位与必要假设。只在材料性事实缺失且影响目标或可行域时向用户提问。
- 在首次写或实质修改建模/求解代码前，先写短 model.md，列全变量与定义域、目标方向与单位、约束族和必要假设。随后在对话中展示可点击文件路径与数学说明内容，确认用户可见后即可继续写代码，不等待额外批准；模型语义变化时先更新并再次展示。
- 按 .agents/skills/opt-workflow 推进 Candidate、Run、Review 与交付；数学条件、来源忠实性、求解状态和最优性分别核验。
- 数值求解和 Case/Core 状态操作使用本地 Python CLI；求解子进程由本机环境运行。记录实际 backend、终止状态、界和检查结果，不声称未检查的环境能力。
- 只在用户要求时撰写完整报告或生成 PDF。默认报告格式为 Markdown；LaTeX/PDF 使用本机安装的 TeX 工具编译并核对实际 PDF。

## 本地 Core CLI

所有 Core 命令都从仓库根目录运行；当前工作目录就是工作根，不传 `--workspace`。使用已经为本项目选定的 Python：优先项目 `.venv` 解释器，或当前 Codex 进程已能调用的 `optagent-local` 入口；不要假定通用系统 `python` 已装好 Core，也不要每个任务重复安装依赖或搜索包源码。下方 `python -m` 示例仅在 `python` 指向该项目环境时适用。未提供 session_id 时 CLI 会在 `.reaslab/optagent/local-session.json` 中为当前工作区保存稳定 ID；同一工作区继续使用该 ID。跨工作区访问已有 Case 时，从该 Case state 读取 owner_session_id 并显式传入。Owner 与 Verifier 对同一 Case 使用同一 ID。

python -m optagent_core.local_cli --session-id <owner_session_id> --role root call <operation> --json-file <request.json>

公开的 28 个 operation 可通过 `python -m optagent_core.local_cli schema <operation>` 查看输入 schema。`call` 使用精确 operation 名称，例如 `optagent.case_inputs`、`optagent.case_create`、`optagent.case_question`、`optagent.case_answer`、`optagent.run_prepare` 和 `optagent.run_start`。`--capture-file` 仅用于 `case_create`、`case_question` 和 `case_answer`，并显式导入位于当前工作区内的 UTF-8 文件；`case_answer` 的 `capture_id` 由 CLI 从该输入文件生成并传入 Core。问答文件的来源标记为 `workspace_file`，不是经过认证的宿主 Capture。其他数据通过各自公开 operation 的 schema 和路径字段录入。本地流程不自动读取宿主聊天记录或隐式用户消息；只有实际提供并经公开操作录入的内容才是 Source。

run_prepare 必须显式提供 execution_budget_seconds、relative_gap 和 absolute_gap。Candidate 必须指定 backend_id；prepare 只对冻结配置做静态校验。worker 在自己的执行环境中对所选接口执行定向可用性检查并记录结果：`False` 表示检查失败并作为结构化失败记录，`None` 表示未知。检查通过不保证许可证、模型构造或求解可用。每个 Prepared 仅启动一次；未知执行状态不能当作失败后盲目重跑。

Verifier 使用 Codex 原生 opt-verifier 协作能力和同一 owner_session_id，但 CLI 参数改为 --role opt-verifier。仅使用当前客户端实际支持的协作参数；新审查优先使用新上下文。终稿或返工需要继续审查时，须实际唤醒原核验者，否则启动新的核验上下文并说明重审范围，不能向已结束的 Agent 发送消息后盲目等待。角色/session 字段是业务流程身份，不提供用户或进程级 ACL；不要把它描述成安全隔离。

## 路径与边界

- `.agents/skills/` 是当前项目的 Codex 技能目录；按技能相对链接阅读参考、示例和模板。
- .reaslab/optagent/ 保存项目级内部 Core 状态和证据。
- cases/<case>/ 保存 Core 返回的可编辑用户交付；该目录中的 .optagent/ 是可复现 Case 自带的运行配置与 helper。两种隐藏目录用途不同，不要互相替换或混用。
- 一次性脚本和工具请求放在 .reaslab/optagent/tmp/ 下独立的临时子目录；仅清理自己创建且已不再使用的材料。不要覆盖或删除用户文件。
- 可编辑 Case 的 solve/check 由本地 Python 在 Case 根目录运行。使用本机实际安装的求解器、TeX 和文件查看能力。
- `.codex/config.toml` 将 `opt-verifier` 注册到项目级 Codex 子 Agent；`.codex/agents/opt-verifier.toml` 提供角色指令。项目未受信任时 Codex 不加载项目配置。模型、推理强度、审批和权限继承用户当前 Codex 配置；Agent 角色不构成权限沙箱。
- 不把凭证、私钥或本地配置带入生成的 Case、报告、日志或提交内容。
