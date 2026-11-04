# Review 证据合同参考

仅在当前 claim 涉及这些证据时查阅本文件；固定 prompt 与主要核查流程不重复全部细节。

## Review / Proof

- 每次 Review 更新使用最近响应中的 `review.review_revision`。note 后登记 Proof 不增加该修订号。
- 正式证明由 Owner 登记；即使已有可行 Run，但其 Result 没有有效 global bound，若另有独立解析证明，Owner 仍可用 `optagent.review_submit_proof`（`kind=analytic`）按公开 schema 登记正文（`body_path`）、前提（`premises`）和实际依赖。Verifier 读取返回的 `Proof.record_ref`、完整 Proof record、证明正文与 premises；正文 Ref 单独不足。若 Proof 依赖历史 Result，只需在 `evidence_refs` 提交精确 Result Ref；登记时会核验该 Ref 属于本 Case 已登记 Run 的原 Seal，并自动派生去重的内部依赖清单。最优性 claim 以匹配 binding 与 scope 的 Proof record 为主锚；Run 的数值可行性另立 claim，并由对应 Run/Result 与检查支持，不把无有效全局界的 Result 混入最优性 claim。普通数值 Run 的独立重算留在 Review evidence，不登记 Case Proof。若 Core 因 optimality claim 中某个数值 Result 未独立支持而拒绝，匹配 Proof 不能覆盖它：保留 Proof 作为最优性主锚；仅将检查确实支持可行性的 Result 放入独立 feasibility claim，然后在同一 open Review 重提。保留在任何数值 claim 中的每个 Result 仍须逐个满足该 claim 的支持条件。
- `optagent.review_submit_outcome` 的根参数不传 `record_kind`，以一个 `decision` 枚举处理两种正常结果。`needs_changes` 必须带非空 `feedback` 和空 `claims`，保持同一 Review open 并返回实际 ID/revision；Owner 修订后由同一 Verifier 复读最终字节并继续该 Review。`ready` 必须 `feedback=[]` 且 `report_path` 非 null，核验完整 claims，并通过该路径绑定真实普通可见成品；schema 保持 nullable 是为了 `needs_changes` 可以不带成品，不能据此让 ready 缺成品。一般交付使用可读数学说明/报告，proof-only 使用可读证明；用户明确只要求代码时，可直接批准可见代码文件；成品 Ref 存在 Review 顶层 `answer_ref`，不重复附到逐条 claims。`claims[].evidence_paths` 只承载 Source、Candidate、Result、Proof 等证据主锚；`remaining` 仅写未决数学范围，不能代替报告返工反馈。`ready` 批准文件当前表达的完整范围与条件，不代表原题已无条件求解。`review_submit_issue` 仍用于登记具体问题；negative/refuted 成品可以被批准并直接交付，但不可从其完整 claims 中筛选正向子集做支持性交付。`deliver_select` 只接收一个 `review_id` 和当前业务证据 ID，从 Review 机械继承完整 claims 与 primary anchors；答案 Ref 的 freshness 会贯穿 selection、当前有效性和导出。摘要与 ZIP 是证据导出，不替代已批成品。

## Run / Result / Batch

- 只读取与当前 case、Source/Candidate binding 和 claim scope 匹配的真实 Run、Seal、Result、ModelCheck/BusinessCheck 与 backend observation。Result 必须属于其 Run 的 `results` 与对应 Seal。历史 Result 只有在正式 Proof 明确绑定时才可作为历史依赖。
- feasibility、optimality、local optimality 与 bound claim 要按实际 Result 核验。native feasibility 的 checked Result 应有通过的 ModelCheck 和匹配的 BusinessCheck；direct_python feasibility 没有 ModelCheck，须由同一 Result 锚定的 BusinessCheck 完整 pass（status=pass 且 violations、uncovered 均为空）及对应 Witness 支持。含数值 Run Result 的 `optimality` 必须以 `conditional` 支持并给出非空条件，还需有效 global bound 和闭合数值证据；direct_python 不提供 native bound 或 gap。仅由正式 Proof 支持时可按实际证明使用 supported 或 conditional。bound 的方向必须与 min/max 一致，scope 为 global 才能支持全局结论。求解器 `optimal` 状态不能单独证明忠实性或全局最优。
- 报告的实例总数、Result Ref 数与 batch 层状态是不同量。`run.inspect.total` 计有 Result 的不同实例；`result_total` 计 Result Refs。读取并核对同一真实 batch summary 中的 planned、started、有 Result 实例、层是否全部尝试、partial、remaining 与 stop reason。所有目标层均尝试不表示成功或最优；`partial > 0` 时即使 `remaining = 0` 也不能称批次完整。
- 不能从另一 Result、自由文本或“较好”的 batch member 补出当前实例的数字。diagnostic 只陈述本地 Run 的实际进程执行或停止状态。diagnostic 与 tradeoff 的主锚可用当前绑定的 Run record、Core 生成的 local_process receipt 或匹配 Proof。无 Result 的 unknown、未 dispatch 或取消/停止观察只支持实际观测，不据此推断数值或求解终态。纯状态事实无需正式 Review 或 selection。报告中的数学/数值对账随相应数学 claim 和成品一起核验，仍须真实依据支持。解析证明也不能转写成伪造的 solver Result。

## 复核范围

证据检查只做到支持当前 claim 所需的范围。若 binding、scope 或语义发生变化，复核受影响的 Source/Candidate/Result；无需重复无关字段和已由 Core 可靠验证的身份检查。

## 已关闭 Review 后更新报告成品

已 ready/closed 的 Review 及其 `answer_ref` 是已发布快照；不能将它重新打开来替换成品，也不能把新 PDF/Markdown 当作旧 `answer_ref` 已批准。

若用户后来要求扩写或更新报告，而本次数学绑定与已审 provenance scope 仍适用，使用 `optagent.review_begin` 的 `prior_review_id` 建立一个新的 linked Review：

- `prior_review_id` 填已发布旧 Review 的 ID；Case、Candidate、Proof IDs、Run IDs 仍填本次实际绑定值。Core 会检查 prior Review 快照、Candidate/数学 Source/Proof/Run binding、provenance scope 和可读取的 prior raw-first note 是否匹配。不匹配时按实际 binding 差异处理，不能宣称已复用审查。
- 成功后，Core 在新 Review 中保存 `prior_review_ref`，继承 prior `note`、设置 `candidate_opened=true` 并 pin 本次 binding evidence。Verifier 应读取 begin 返回的 raw Source/provenance、继承 note 和实际 Candidate/Result/evidence，再审查本次新成品及新增或改变的 claims。**不再调用 `review_note`**，也不重跑 Run。
- Verifier 仍须按正常合同读取新 `report_path` 的最终成品，再在新 Review 提交 `ready`。对 PDF，须全文读取绑定 PDF；对 Markdown，须完整读取绑定 Markdown。继承的数学 note 不能自动支持未曾审查的新内容。
- `deliver_select` 选择新的 Review ID，Core 绑定新 `answer_ref`；旧 Review 和旧 Delivery 保持原样。不能只在旧 Review 上改路径、对旧 ready 做补注，或把新成品复制进旧交付后声称已审。

若用户要求改变数学语义或本次 Source/Candidate/Proof/Run binding 不再匹配，不走本分支的“只复用成品审查”；按正常核查合同建立与变化范围相称的 Review。保持原有 Run 仅在其确实仍绑定且适用时复用。
