---
name: opt-review
description: 独立依据原始需求、数据和结果证据核对优化候选。
---

## 本地 Core CLI

Review 操作通过项目根目录的本地 CLI 完成：
python -m optagent_core.local_cli --session-id <owner_session_id> --role opt-verifier call <operation> --json-file <request.json>
命令使用 `opt-workflow` 所述的本项目 Python：优先项目 `.venv` 解释器或当前 Codex 进程已安装的 `optagent-local` 入口；示例中的 `python` 只有在指向该环境时才适用。Verifier 与 Owner 使用同一个 owner_session_id，仅将 role 改为 opt-verifier。公开操作名使用精确名称，例如 optagent.review_begin。Core 角色只用于流程校验，不提供操作系统级权限隔离。Verifier 必须使用 Case owner_session_id；role=opt-verifier 是本地独立审查的身份标记。


## 核查顺序

角色 ABI 和 checker 的精确字段见[模型与 checker 参考](../opt-workflow/references/model-authoring.md)。当主张涉及 Review revision/CAS、正式 Proof、batch 或 lexicographic 结果、ready/needs_changes、selection、answer_ref 或诊断观测时，按需参阅[证据合同参考](references/evidence-contracts.md)中的精确参数与证据规则。

审查中确需一次性脚本或中间文件时，落点与清理按[临时材料约定](../opt-workflow/references/temporary-materials.md)执行。

正向数学主张必须有当前绑定的独立 raw-first review/evidence。可复用检查须匹配当前数学 Source、Candidate、claim scope 和已审前提；语义改变时只复核受影响范围。每条 claim 说明原题到 Candidate 的实质转移及适用条件；Worker 自检不能证明来源忠实性。

1. Owner 通过当前 Codex 客户端支持的原生协作能力启动 opt-verifier；如客户端提供新上下文选项，启动时选择新上下文。只交接 Case ID、owner_session_id、明确的核验范围和必要的原始 Source refs。报告定稿、登记 Proof 或返工后，需要继续时，实际唤醒原核验者并只补交本次所需材料；若客户端不能继续该 Agent，则启动新的 opt-verifier 上下文并标明重审范围。不要对不会被唤醒的已结束 Agent 发送消息后盲目等待。
2. Verifier 用 `optagent.review_begin` 读取原始来源和短 provenance 投影。提交 `optagent.review_note` 前，只读实际返回的原始 Source 字节与投影；从完整叙述、规则、定义、表格/字段说明、单位和有效用户答复中提取会影响可行性、目标值或重要决策的义务、事实与缺失条件。复用 Core 或 binding 返回的完整 FileRefs，不重复计算已有引用的 hash/byte_len。note 前不查看 Candidate、Result 或报告路径内容；ACK 不证明数学语义。若通过 `prior_review_id` 链接一个满足条件的已关闭 Review，`review_begin` 会返回继承的 note 并打开本次 Candidate/evidence；读取返回的 note 和实际 evidence 后继续，不要再次提交 `review_note`。具体条件见[证据合同参考](references/evidence-contracts.md)。
3. Verifier 读取响应中实际绑定的 Candidate、代码和相关 Run/Result refs，并按需核对数学主张和报告。报告可作为待核查的主张来源，但不能替代原始证据。提交 `ready` 前，Verifier 必须读取 Owner 提供的最终 `report_path` 并核对其中主张；报告尚未定稿时可先完成其他核验，待 Owner 通知后复读。若 Owner 在审查中修改报告，须通知 Verifier 重新读取。LaTeX/PDF 报告必须读取实际绑定的 PDF 全文，可用 `pdftotext -layout <report_path> -` 并核对覆盖全部页面；若返回被截断，继续分段补读。只读 `.tex` 或 Markdown 源码不能替代对 PDF 成品的读取。文字抽取无法显示的图形含义须依据其可读图注、随附数据和可用证据核验；不得声称目视检查了页面，除非确实具备图像输入并逐页查看。`ready` 时 Core 会重新读取该路径并绑定当下字节，故最终版本须保持稳定到提交完成。
   一般优化交付应是可读数学说明；用户明确只要求代码时，可直接把可见代码文件作为成品，不额外造报告。可读报告、数学说明或Proof正文均可作为成品，但不能代替原始来源、用户答复或数值/证明证据。普通成品改动不改变Candidate binding；Source/Candidate改动仍按现有规则要求fresh review。原题叙述、规则及表格/字段语义须完整读取；数值记录按原定义由程序遍历全域，核对形状、原索引和相关义务，无须逐值打印。语义文本截断时补读；数据样本或截断展示不能替代全域核验，完整Ref也不代替内容核验。
4. 逐项核对原源要求、Candidate 实际采用的定义与字段、材料性差异及其已核验影响，并确认当前解释的依据。不得用常见建模惯例、报告措辞、候选自述或 benchmark 习惯改写明文义务。材料性改义须由原源或真实用户答复确认；候选更容易求解或结果更好不构成确认。
5. 核对原题到实际代码及输出的关键数学转移。对自编算法或 Proof，辨认关键命题、前提及实现。核验依据须不预设待证命题，并覆盖所声称的范围；按原定义核原域点、独立核验适用的全域证书，可以构成完整依据，无须为独立性另解一遍。只对尚未闭合的具体命题补证。重复同一关键算法不单独证明等价性或全局性；抽样失败可反驳全称命题，抽样通过不能提升为全域结论。核对原始定义到业务点和目标的映射，避免同一派生遗漏自证。语义成立后，可信 native 点与界按既有容差合同核验，无须为形式另求精确 Proof；未核命题按实际支持范围收窄 claim。

   对照当前 scope 下的实际模型代码、绑定数据、最终成品及匹配的 Result/Seal，核实 Source 与输入如何映射到 Candidate、业务决策和报告量，尤其是单位、集合、变量域、目标与关键约束。语义映射及检查覆盖成立后，可复用与其 method/scope 相符的可信 ModelCheck 和适用 BusinessCheck；其中原生 CpModel 的 `core_cp_sat_response` pass 表示同次求解器接受点及 Core 对完整 response vector、proto 域、由模型变量对象规范化出的 typed index 投影和整数目标的机械核对，不表示 Core 独立逐条重核原生 global constraints。`direct_python` 没有 ModelCheck，须核对其 Result 锚定的 BusinessCheck 完整通过及对应 Witness；不默认完整重求或逐值重复核算。Owner 的 builder 与 checker 可能共享题意错误，PASS 不单独证明来源忠实性。成品中实质结论的对象、量词、条件与证据范围须相符；区分当前方案、受限模型与原问题。全称结论须有覆盖该 scope 的依据，优先寻找能推翻关键结论的小反例；未核影响按实际范围收窄 claim，`unresolved` 与 `refuted` 都不表示全局不可行。数值 Run 的 claim 必须匹配当前 binding、scope、单位及 checked Result/Seal；数值 `optimality` 须用 `conditional` 且有非空 `conditions`，并由同一 claim 的 checked Result、有效全局界与数值证据支持。按证据需要读取[证据合同参考](references/evidence-contracts.md)：正式 Proof 可支持不依赖数值 Run 的纯数学路径；可信 native 结果足够时无需另求精确 Proof。不要伪造 Run、Result 或 Seal。
6. 有可复核缺陷时用 `optagent.review_submit_issue` 登记证据。按证据合同用 `optagent.review_submit_outcome` 提交裁决；只有返回 `ok:true` 才交回实际 decision。若返回 `ok:false`，按错误修正并在同一 open Review 重提。Owner 需要返工时将最新所需材料交给同一 Verifier 子 Agent；登记新增 Proof 时交接完整 `record_ref`。ready 必须批准真实可见的最终成品及其完整范围、条件与限制。

## 交付边界

- Verifier 提交本轮裁决并将 Review 定位交回 Owner；Owner 按裁决返工或交付。Owner 最终交付由数值求解支持的方案（包括可行但未最优）前，须通过 `optagent.deliver_select` 选择当前 ready Review 的完整证据；继续迭代、纯数学答复、负面或未决结论、状态报告不因此要求 selection。Owner 须交付该 Review 批准的完整可见成品，不过滤或增强其 claim。selection 参数、成品绑定与 freshness 细节见[证据合同参考](references/evidence-contracts.md)。
- 若完整 Review 含 refuted 或未解决的正向数学 claim，不作支持性交付；Owner 交付原 Review 批准的文件并明确原题尚未解决。准确的失败或反例报告也可作为本次成品。

## 判断边界

- 来源忠实性、候选数学正确性与求解器状态是不同主张。
- 可行解不等于全局最优；限时记录不能提升为最优；界须方向正确并绑定实际候选和单位。
- 数学参考只提供线索及出处，不能改写题面或证明本题正确。
- `opt-verifier` 依据当前会话提供的工具与证据工作，不宣称宿主提供强制文件隔离、角色 ACL 或统计独立性。
