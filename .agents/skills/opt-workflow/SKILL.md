---
name: opt-workflow
description: 将优化任务从真实来源文件推进到模型和有证据支持的结果。
---

## 本地 Core CLI

所有 OptAgent Core 操作都在项目根目录通过同一个本地 CLI 执行，保持本 Case 的 owner_session_id；当前工作目录就是工作根，不传 `--workspace`。使用本项目已选定的 Python 环境：优先项目 `.venv` 的解释器，或当前 Codex 进程已安装且可调用的 `optagent-local` 入口。不要假定通用系统 `python` 已安装 Core，也不要每个任务重装依赖或重新搜索源码；以下 `python -m` 示例仅在它指向该项目环境时使用。精确公开操作名作为 `call` 参数，例如 `optagent.case_inputs`；公开 operation 的输入 schema 可用 `schema <operation>` 查看。不再调用 ReasLab 平台工具。调用参数写入位于 `.reaslab/optagent/tmp/` 的 UTF-8 JSON 文件，并用 `--json-file` 提交。按 stdout 中的 JSON envelope 和 CLI 退出码判断 Core 操作结果；求解子进程的 stdout/stderr 保存在本地执行回执中。

python -m optagent_core.local_cli --session-id <owner_session_id> --role root call <operation> --json-file <request.json>

`case_create` 必须在 `call` 命令上提供 `--capture-file <workspace-relative UTF-8 input path>`，显式导入题面。`case_question` 和 `case_answer` 也要求 `--capture-file`，导入工作区内的 UTF-8 文本；答复必须来自实际收到的用户内容，写入文件后再提交给 `case_answer`，其 `capture_id` 由 CLI 从文件生成并传入 Core。这些问答输入的 provenance 是 `workspace_file`，不构成认证宿主聊天记录。`--capture-file` 仅用于这三个操作；其他 Source/数据按各自公开操作的 schema 和路径字段录入。本地流程不会自动读取宿主聊天记录。Verifier 通过项目 Codex 配置注册的 `opt-verifier` 子 Agent 工作，并复用同一 `owner_session_id`，将 CLI role 改为 `opt-verifier`。项目需先受信任，以便 Codex 加载 `.codex/config.toml`。role 与 session 是 Core 的流程标签，不是操作系统 ACL。


## 操作指引

- 先理解用户目标、原始题面和已有数据；文件名与目录可任意。材料性事实缺失时，用 `optagent.case_question` 提出具体问题，仅将之后真实收到的用户消息作为答复。`case_question`、`case_answer` 的本地 CLI 调用需要显式 `--capture-file`；实际用户答复先保存在当前工作区的 UTF-8 文件中再导入。记录 provenance 为 `workspace_file`，不可描述为已认证的宿主消息。无关工作可继续。

### 选择数学表达与证据

先将原题的业务对象、角色、阶段及允许/禁止行为落实为变量域和约束；从拟用表达实际允许的行为反向核对源义务与例外。随后比较变量、约束、非零项及输出恢复规模，选择可检查的表达或直接算法。首次编写当前采用模型的建模或求解代码前，建模者先把已选数学表达写入当前 Case 的短 Markdown 说明，列出变量及定义域、目标方向与单位、定义模型所需的全部约束族及索引/量词、必要符号和假设；可合并同类约束紧凑表达，不省略必要关系。默认用实际 `artifact_dir/model.md`，已有同用途说明时沿用原路径。Owner 在编写模型代码前通过对话展示该文件的可点击路径和数学说明内容，确保用户先看到后再写代码；无需等待批准。已有模型先读取核对，补写或更新短稿并展示后再修改模型代码或求解；数学语义实质变化时先更新短稿并重新展示，再改模型代码。不改变数学表达的接口修复无需重示。下表关系仅用于相关结构；完整前提、恢复与边界见[数学参考](references/formulation-reference.md)对应节。按原索引恢复题目要求的业务输出，在原单位核对目标、约束及界。

| 结构 | 可直接使用的关系与资格 | 参考 |
|---|---|---|
| 消元、换单位、辅助变量或删选项 | 交付点的恢复x=R(z)须回原域，目标满足F(R(z))=a·f(z)+b，a>0保排序；等价/保最优还须证原解覆盖或等值。未证则按真实松弛/限制给点界：min时松弛最优值≤原最优值≤限制最优值，max反向。整数消元保原格点。 | F01/F02/F06 |
| 独立块或同型对象 | 若X=∏Xᵢ且F=ΣFᵢ，原最优值等于各块最优值之和。聚合另须目标、域与所有关联约束完全可互换；保存成员映射并按原索引恢复。共享资源留在整体约束中。 | F05/F09 |
| LP、单商品网络或指派 | 比较primal/dual的行列及非零项规模。min cᵀx、Ax≥b、x≥0时，y≥0且Aᵀy≤c给下界bᵀy；需要原域方案时另行恢复；仅求最优值须按F07给适用的最优性证据。完整矩阵TU、整数RHS/界，且LP可行并在顶点取有限最优时，存在整数最优顶点；取最优基本解或有证整数恢复，再核原域。 | F04/F07；[F04·瓶颈/稳定匹配](references/formulation-reference.md#f04-matching) |
| 加性收益、共享资源上限 | max Σvᵢ(xᵢ)、Σrᵢ(xᵢ)≤B时，λ≥0给上界U(λ)=λᵀB+Σsupₓᵢ∈Xᵢ[vᵢ−λᵀrᵢ]。Xᵢ须覆盖原解局部选项；各sup须精确或用有效上界替代，不能用局部可行值。可放松其余耦合，保持重数与单位；配原域点按所需标准闭合。 | F07/F09 |
| 启用成本或可避免损失 | 若x⁰可行，且已证对全部原题解F(x)≤F(x⁰)+Σzᵢ(Bᵢ−Cᵢ)，zᵢ为启用指示、Bᵢ≤Cᵢ，则x⁰为最大化最优。Bᵢ须覆盖全部收益，成本与共享收益各计一次；最小化用相应下界。 | F05/F07 |
| max/min、绝对值、分段和乘积 | min t且t≥fᵢ(x)在目标压紧时给t=maxᵢfᵢ；方向或绑定不同须另证。有限有效界下二元×连续可精确线性化；两个连续变量的McCormick通常仅是松弛。 | F03 |
| 覆盖、路径、调度或服务能力 | 至少/恰好/至多覆盖分别用≥/=/≤。若分派仅是各需求i从合法邻项N_i中独立选d_i个互异启用项（d_i非负整数，启用y_j二元），且目标不依赖分派，可删分派变量，以Σ_{j∈N_i}y_j≥d_i替代该组分派约束；逐i选d_i个已启用邻项恢复。有共享容量或其他分派约束时不能独立投影。度/流平衡不保证连通；完整往返、时段及共享容量按原定义核。 | F04 |
| 凸约束、互补或均衡 | 在相应凸性及次微分规则下，满足完整KKT的原可行点可证全局；乘子存在需相应资格，非凸时满足KKT一般不证全局。凸可行集上，VI在算子为可微凸势函数梯度时等价于全局最小化。 | F08 |
| 有限阶段或巨大列集 | DP状态须保留决定未来合法动作与成本的充分信息，递推取遍合法动作并保存策略。分解保留耦合；精确pricing停止只证完整LP列空间，整数最优仍须相应证据。 | F09/F12 |
| 不确定性和分阶段决策 | robust要求对全部u∈U成立，期望目标按源概率求和；相同已知历史的决策相同。有限采样本身不等于整个U的保证。 | F10 |
| 优先级、软约束、低秩与数值 | 两层严格加权优先需主目标正间隔Δ和次目标范围R满足WΔ>R，否则按优先级锁定。软约束须获授权或有exact-penalty证明；低秩因子化一般非凸。正缩放后恢复原点、目标、界与容差单位，执行能力查实际后端接口。 | F11/F13/F14 |

所需原域方案与有效界达到任务标准时，完成对应Run或Proof证据及既有交付。由程序直接计算出的机器方案走当前 Candidate 的 direct_python Run；依赖 solver 状态或 native bound 时走相应本地 solver Run。纯解析界或数学证明走已有 Proof。未闭合时保留已核点和适用界。没有可证结构时采用普通完整表达。

当前执行能力只以[模型编写说明](references/model-authoring.md#后端支持边界)的“后端支持边界”为准；理论表达式、solver 名称或 log 字段不构成接口承诺。

- 继续已有 Case 时，先用 `optagent.case_list` / `optagent.case_get` 核对完整 ID、当前状态与 owner；需要接手时按 `optagent.case_revise_handoff` 的现有检查完成交接。
- 普通可见工作区中的原题和业务输入用 `optagent.data_capture` 保存完整原字节；`optagent.data_normalize` 按对象块的真实格式和布局规范化；`optagent.data_inspect` 仅提供有界样本。内部记录不可编辑。

- `optagent.case_create` 返回的 `artifact_dir` 是预留的可见生成物目录，按用户指定路径优先；默认数学说明为 `<artifact_dir>/model.md`。需要完整最终报告时，默认 Markdown 报告为 `<artifact_dir>/report.md`，复用并展开短稿中的数学内容；用户明确只要求代码时无需另写完整报告。builder 为 `<artifact_dir>/code/optimization_model.py`，需要业务 checker 时为 `<artifact_dir>/code/solution_check.py`。已有同用途说明沿用原路径；这些只是默认路径，不要求重命名已有文件，也不要额外建空目录或搬动输入、旧代码。
- 当本案需要登记候选模型时，从普通可见源码登记真实路径与绑定；具体 builder/checker ABI、数据和 backend 规则见[模型编写说明](references/model-authoring.md)。参数已在题面给全时使用 `data_id=null`；只有明确登记实例参数对象时才按行形成多个实例。原题或业务数据变化时，重新捕获受影响内容、按需规范化，再登记新候选；仅候选代码变化时，直接按真实路径重新登记候选。旧 Review 不会自动覆盖新绑定。
- Owner 按所需结论选择证据路径：依赖 solver 状态或 native global bound 时，从当前候选走本地 Core Run/Result；由程序直接计算的机器方案走 `direct_python` Run，并由真实 BusinessCheck 核验；纯解析界或数学证明走已有 Proof。保存原始输入、可运行计算/核验程序、完整原题业务方案、按原题定义的目标值和证明依赖；自报 status 不构成检查证据，不伪造 Run、Result 或 Seal。报告及用户可见交付按既有 Review/Delivery 规则完成。普通建模、单位、规模、初值结构检查或小型歧义反例按实际问题核对其范围，不替代结论所需证据；可能耗时较长的诊断或复现，开始前明确预算与停止条件。
- 返工先按原定义独立核实具体反例；反例成立时，修正造成它的模型、算法、恢复映射或 checker，并在修订后的实际行为上低成本重放该反例，再决定是否扩大计算；初值、报告或 scope 变化不能代替可行域与结论的修复。可用可核依据驳回误审；未决歧义保留条件或 partial，不把明文约束条件化。
- 仅在已发生接口或执行错误时做隔离复现，优先缩小输入；缩小输入不能复现时，再复现必要的真实规模。记录实际错误、退出码、solver 终止状态和异常信息。无可行点或想换 solver 不构成接口错误；区分观测与尚未证实的性能原因。
- 一次性脚本和中间材料按[临时材料约定](references/temporary-materials.md)处理。正式源码、输入、checker 和必要 helper 保持可见；完整本地 Case 路径、依赖和 solve/check 操作见[本地 Case 与依赖](references/model-authoring.md#本地-case-与依赖)。
- 多案例先判断它们是共享资源、互斥情景、共享第一阶段决策还是彼此独立，再采用联合、情景连接或独立建模。多个来源对同一字段冲突时，先依据原题或真实用户答复解决，不静默选择。
- 每个数值 Run 都先用 `optagent.run_prepare` 准备本次运行输入，每个准备项至多调用一次 `optagent.run_start`。仅当最新 Run 为 `requested` 或 `unknown` 且 Owner 明确决定继续时，才准备新输入并在新 start 引用精确的最新 Run ID 与非空 `attempt_reason`。这不证明旧执行已停止。最新 Run 为 `response_received`（包括 Worker `invalid`）时，修复或重新登记候选后正常运行，不传 supersede ID 或 reason。
- 若执行入口明确不可用，或相同受限诊断不再提供新信息，保留真实失败/未知状态并停止自主重复尝试；有新状态或恢复证据时，先核对当前 Case 与 Run。无新证据时不盲目重发或反复探测。本地 CLI 返回错误时，先读取关联 Case 与 Run 的真实状态和 execution evidence；区分模型不可行、求解限时、进程或接口失败和未知状态，不因外层 CLI 错误盲目重跑。
- 无 Result 的 unknown Run 只能按真实 transport/status observation refs 描述实际观测，不能推断数值、终态或 solver 结论。纯运行状态无需正式数学 Review；需要独立审查数学结论或带锚诊断时，按[opt-review](../opt-review/SKILL.md)启动 fresh `opt-verifier` 并遵循其中的完整核查与定稿合同。
- 若 Run 有 checked incumbent 与适用 global bound、但 `numerical.optimality.status=not_closed`，分别呈现可行性和界；这不等于运行失败。不要只为去掉标签或放宽 gap 阈值重复求解；仅在确需更好 incumbent 或更紧界时评估继续执行。

不使用固定阶段轮数。何时继续由实际依赖和证据决定；只在真实用户事实阻塞相关工作时等待该事实。
