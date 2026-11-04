# 编写候选入口、Pyomo 模型与原生 CpModel

本地 OptAgent 依据必填 backend_id 选择 direct_python 算法或受支持的原生求解适配器。Core 从普通工作区文件捕获题目和数据，登记并冻结当前 Candidate 与输入，再通过本机 Python worker 执行、检查和记录 Run 证据；Core 的公开业务合同仍需由实际 CLI 响应与本地证据落实。用户输入路径可以不同，不要求固定文件名或额外 IR。当前模型的数学先行说明、默认 model.md 路径和展示顺序按工作流程完成；完整报告及 Review 定稿按 opt-report 执行。模型代码和内部记录不能代替用户可读说明。

建模时按[工作流程的“选择数学表达与证据”](../SKILL.md#选择数学表达与证据)选用入口关系；完整前提、恢复与边界见[数学建模参考](formulation-reference.md)。

Owner 的案例全规模数值求解（包括称为“诊断”的全规模求解试跑）和真实错误诊断按[工作流程](../SKILL.md)执行。


direct_python 的 solve(bundle) 每次从当前绑定输入运行登记的算法或构造；算法可持续搜索，并在返回时提交当前 best-known 的完整原域可行点，不必等待全局最优性闭合。若交付声明该 Case 可在数据修改后重新优化，应保留并按既有 local_module_paths 流程登记真实搜索代码及 helper；若任务只核验指定点，可合法回放该点并按实际范围命名和报告。

## 候选入口

登记 `business_checker_path` 后，native 与 direct_python 的模型模块都须提供可调用的 `extract_solution(bundle, witness)`，checker 模块提供 `check_solution(bundle, solution)`；结构与返回值见[后文 ABI 段](#业务方案提取与-checker-abi)。

### direct_python

选择 `backend_id="direct_python"` 后，Core 从该 backend 推断候选入口类型；公开候选参数不传 `authoring`。登记的模型模块须能通过冻结闭包导入 `solve(bundle)`；函数可以从已登记 helper 导入，不限制为模型文件中的 top-level `def`。`bundle` 是已按当前输入绑定、规范化并切到当前实例的 `DataBundle`。该入口首版只支持 `scalar` 与 `feasibility` 目标计划，不自动分解或衔接 lexicographic 层。

`solve(bundle)` 返回 `None` 表示无方案；否则只返回含 `variables` 与 `objective_values` 的映射。变量行恰含 `component`、`key`、`domain`、`value`；键保留字符串/整数类型和原顺序（标量用空数组），domain 为字符串或 null，value 为有限数。scalar 目标恰有一行，组件、方向、单位与冻结 objective_plan 一致；feasibility 目标数组为空。运行时添加 origin、输入摘要、实例键和绑定身份。空 variables 和 objective_values 可用于 feasibility 目标的空决策形状，但仍须经真实业务 checker 核验。

以下 scalar ABI 示例假设当前绑定的 DataBundle 中含有标量对象 minimum_quantity；目标计划的 component、sense 和 unit 分别为 total_quantity、min 和 quantity。这仅演示 ABI，实际对象和值来自本次输入。

~~~python
def solve(bundle):
    quantity = float(bundle.scalar("minimum_quantity"))
    return {
        "variables": [
            {
                "component": "quantity",
                "key": [],
                "domain": "Reals",
                "value": quantity,
            }
        ],
        "objective_values": [
            {
                "component": "total_quantity",
                "sense": "min",
                "unit": "quantity",
                "value": quantity,
            }
        ],
    }
~~~


direct Result 的 `check` 指向 BusinessCheck；不存在 ModelCheck PASS、native bound 或 native gap。`direct_python` 通过 `solve(bundle)` 执行自身算法，不经过 OptAgent 原生适配器，函数内部可自行调用所需求解器库；Result 的 solver_invoked 只表示本地原生适配器调用，不表示内部库是否被调用。通过条件要求存在 Witness，BusinessCheck 为 `pass` 且 `violations`、`uncovered` 都为空，并且其目标值与 Witness 一致。这只核验方案及声明的业务义务，不证明全局最优或原题覆盖；Verifier 仍须检查 Source 与真实 checker 是否覆盖原题。`execution_budget_seconds` 和取消机制照常适用。`relative_gap` 与 `absolute_gap` 仍按通用 Run schema 冻结，但 direct 没有 global bound，不能据此形成最优性闭合。solver 原生时限、线程、随机种子、warm start、日志和模型导出设置对 direct 入口不适用，非默认请求会被拒绝。

### Pyomo builder

native 候选模块必须提供 `build_model(bundle)`，接收运行时已经规范化并绑定到当前实例的 `DataBundle`，并返回已经构造好的 Pyomo `ConcreteModel`。新案例默认将 builder 保存为 `code/optimization_model.py`；需要业务方案时默认使用 `code/solution_check.py`。它们只是便于识别的默认文件名。可以登记其他符合模块路径规则的文件名，已有用户文件不重命名。不要返回字典或自定义 build 描述，不要在候选里调用求解器或 `SolverFactory`，也不要重新打开原始文件、访问网络、重读或再次规范化输入。本地 worker 根据登记的 backend_id 和目标计划调用对应 solver 适配器；本参考中的[后端支持边界](#后端支持边界)列出当前实际接线的接口族与模型范围。


构造时可以给 Pyomo 变量设置 `.value`。若要通过受支持的 adapter 提交这些值，在 `optagent.run_prepare` 的 `solve_options.warm_start` 中显式设为 `true`；赋值本身不会开启提交。更换 backend 或创建新的 Prepared 时，只按本次冻结的选项判断，不沿袭旧假设。省略或 `false` 只表示 Core 不请求显式 warm-start 提交，不清除变量初值，也不保证所有 backend 都从冷点开始（例如 IPOPT 的 NL 写入仍可能包括初值）。`true` 表示提交请求；requested/submitted、native accepted/effective 与已检查可行点是不同事实，适配器可以拒绝请求，solver 也可能接受、修复、忽略或拒绝初值。初值可以不完整或不可行，不能用来填补求解器未返回的活动决策变量。数值和可行性结论以本次 Run 的 Result、ModelCheck 及适用业务检查为准；builder 当前 `.value` 不是结果证据，LP 松弛的非整数向量不能作为 MIP 方案。固定值和合法确定性补全仍可按既有候选点流程核验；error/no-point Run 也可能有 Result 记录，应按实际状态和检查结果说明。`warm_start=true` 不会自动开启构造启发式或运行前业务检查。

若 builder 依赖本地 Python helper，先将其纳入可见 `code/`，并在冻结候选前把实际工作区路径登记到 `local_module_paths`。冻结导入器按登记路径保留相对目录及包内相对导入；例如 `models/optimization_model.py` 可用 `from .helpers import build_seed` 导入同目录 `models/helpers.py`，并登记该 helper 的真实路径。裸导入 `from helpers import build_seed` 会查找顶层模块；本地诊断中临时调整 `sys.path` 后可导入，不证明冻结闭包能解析该依赖。目录和文件名可按用户需要选择，但候选须使用 `.py` 路径，包目录和模块名须符合 Python 标识符规则。必要 helper 导入或构造失败须报告实际错误；若可选构造失败后仍继续，应说明未获得有效起点。模型通过本案例的捕获、候选登记、输入冻结和本地 Core 运行流程复用。报告遵循 [opt-report](../../opt-report/SKILL.md)；builder/checker 是Core worker 的调用入口，不能仅凭 `.py` 文件描述成独立运行程序，除非确有真实独立入口。本地 Case 的可见路径、运行入口和依赖边界见下一节。

若题面已给出完整系数、不需要外部数据文件，候选使用 `data_id=null`，也不登记 `instance_parameter_object_id`。实际 loader 调用 `load_data_bundle(reader, None, candidate_binding_digest=...)` 返回同类型的空 `DataBundle`；每个真实单实例再经 `for_instance(None, ())` 得到空 key、空 parameters 的 `instance_context`，并把该 bundle 交给选定入口 `build_model(bundle)` 或 `solve(bundle)`。不要伪造空数据文件、data ID 或 manifest。

例如，下面模型中的常数必须逐项来自当前题面；示例数字只用于说明 builder 形状：

```python
from pyomo.environ import ConcreteModel, Constraint, NonNegativeReals, Objective, Var, minimize


def build_model(bundle):
    model = ConcreteModel()
    model.quantity = Var(domain=NonNegativeReals)
    model.meets_minimum = Constraint(expr=model.quantity >= 2)
    model.total_cost = Objective(expr=3 * model.quantity, sense=minimize)
    return model
```

若外部数据确有一个 scalar 对象 `minimum_quantity`，则按其真实登记 ID 读取：

```python
from pyomo.environ import ConcreteModel, Constraint, Objective, Reals, Var, minimize


def build_model(bundle):
    minimum = bundle.scalar("minimum_quantity")

    model = ConcreteModel()
    model.quantity = Var(domain=Reals)
    model.meets_minimum = Constraint(expr=model.quantity >= minimum)
    model.total_quantity = Objective(expr=model.quantity, sense=minimize)
    return model
```

这个例子只说明入口和读取方式，没有在原生求解器中运行，也不构成数学验收或题意忠实性结论。目标计划必须与模型中真实存在的 Pyomo Objective 组件相符。

### 原生 CpModel builder

`backend_id="cp_sat"` 也接受新合同中的实际 OR-Tools `CpModel` 对象。`build_model(bundle)` 返回真实模型，并在 `variables` 中将 typed identity 映射到该模型的实际变量对象（包括普通 `BoolVar`）；Core 核对对象归属后，再规范化为内部 proto index。公共候选仍只使用 `backend_id`，不添加 `authoring` 或 `adapter_kind` 参数。完整返回形状、目标限制、检查范围及本地行为见[原生 CpModel 接口参考](native-cp-model-authoring.md)。既有 Pyomo builder 继续有效，Core 根据实际返回对象分流；不要在 Owner builder 中创建或返回 `CpSolver`、状态、目标界或伪造响应。

## 后端支持边界

当前接线 19 个原生适配器 ID，另有 `direct_python` 入口。登记某个 ID 只选择对应执行入口，不保证特定任务有依赖、商业许可或适用模型表达式；实际失败按 worker 返回值处理，不自动安装或切换接口。

| `backend_id` | 执行入口 | 当前接入的模型族与边界 |
|---|---|---|
| `highs`, `cplex`, `scip`, `glpk`, `cbc`, `xpress`, `gurobi`, `mosek`, `ipopt`, `cp_optimizer` | Pyomo 对应插件 | 按插件实际接受的表达式与状态能力处理。IPOPT 为连续局部 NLP，不提供全局最优性证明；CP Optimizer 当前只接普通 Var、线性约束和单目标有限整数子集。 |
| `cvxpy_copt` | Pyomo sparse-repn → CVXPY convex QP/QC bridge | 连续/整数 LP 与已核实的凸二次模型族；连续 COPT 路径当前不声明可信数值界。 |
| `cvxpy_clarabel`, `cvxpy_cvxopt`, `cvxpy_scs` | 同一 CVXPY bridge | 连续凸 QP/QC，须通过 CVXPY DCP 检查；不扩展为一般 SOC、SDP 或指数锥 API。 |
| `cvxpy_osqp` | 同一 CVXPY bridge | 连续凸 QP 与仿射约束，不接二次约束。 |
| `cvxpy_glop`, `cvxpy_pdlp` | 同一 CVXPY bridge | LP；拒绝整数变量，不把松弛结果当成 MIP 解。 |
| `ortools_clp` | OR-Tools MPSolver | LP；保留变量域、约束两侧、目标常数与方向，拒绝整数域。 |
| `cp_sat` | Pyomo 线性整数分支，或由本地 Core 处理真实 OR-Tools `CpModel` 的原生分支 | Pyomo 分支仍限有限域整数线性子集；原生分支接受有限整数域、OR-Tools `Model.Validate` 接受的整数约束（含其 global constraints），以及精确整数 scalar objective 或 feasibility；不支持 lex。原生 ModelCheck 依据同次 solver response 做完整整数向量、proto 域、typed index 投影和整数目标的机械核对，不表示 Core 独立重算每条 global constraint。两条分支均不自动舍入、缩放或改写用户数学。 |

下表列出当前适配器实际传递的原生设置。表中没有列出的显式 time/thread/seed 请求会按能力拒绝；Core gap 与 checker 容差仍按其各自定义处理，未列出的原生 gap 不会传入求解器。

| `backend_id` | 原生时间 | 原生线程 / 种子 | 原生 gap |
|---|---|---|---|
| `highs` | `time_limit` 秒 | `threads` / `random_seed` | `mip_rel_gap` / `mip_abs_gap` |
| `cplex` | `timelimit` 秒 | `threads` / `randomseed` | `mip_tolerances_mipgap` / `mip_tolerances_absmipgap` |
| `scip` | `limits/time` 秒 | `parallel/maxnthreads` / `randomization/randomseedshift` | `limits/gap` / `limits/absgap` |
| `glpk` | `tmlim` 整秒，向上取整 | 不支持 | `mipgap`（仅相对） |
| `cbc` | `seconds` 秒 | `threads` / `randomSeed` 与 `randomCbcSeed` | `ratioGap` / `allowableGap`；请求 seed 0 会被拒绝，因为 CBC 将 0 解释为按时刻播种 |
| `xpress` | `maxtime` 负整秒，按秒向上取整 | `threads` / `randomseed` | `miprelstop` / `mipabsstop` |
| `gurobi` | `TimeLimit` 秒 | `Threads` / `Seed` | `MIPGap` / `MIPGapAbs` |
| `mosek` | `dparam.optimizer_max_time` 秒 | `iparam.num_threads` / `iparam.mio_seed` | `dparam.mio_tol_rel_gap` / `dparam.mio_tol_abs_gap` |
| `ipopt` | 本适配器无原生 wall-time 映射 | 不支持 | 不支持 |
| `cp_optimizer` | `TimeLimit` 秒并显式设 `TimeMode=ElapsedTime` | `Workers` / `RandomSeed` | `RelativeOptimalityTolerance`；仅当 Core 绝对 gap 大于 0 时传 `OptimalityTolerance`，其原生约束要求正值 |
| `cvxpy_copt` | `TimeLimit` 秒 | `Threads` / 不支持 seed | `RelGap` / `AbsGap` |
| `cvxpy_clarabel` | `time_limit` 秒 | `max_threads` / 不支持 seed | `tol_gap_rel` / `tol_gap_abs` |
| `cvxpy_cvxopt` | 不支持 | 不支持 | `reltol`；仅 Core absolute gap 大于 0 时传 `abstol`，为 0 时省略该 native 参数；Core 闭合目标仍保留原值 |
| `cvxpy_scs` | `time_limit_secs` 秒 | 不支持 | 不支持；`eps_abs`/`eps_rel` 是残差精度 |
| `cvxpy_osqp` | `time_limit` 秒 | 不支持 | 不支持；`eps_abs`/`eps_rel` 是残差精度 |
| `cvxpy_glop` | `time_limit_sec`，按毫秒向上取整 | `GlopParameters.num_omp_threads` / `GlopParameters.random_seed` | 不支持原生 objective gap |
| `cvxpy_pdlp` | `time_limit_sec` 秒 | `PrimalDualHybridGradientParams.num_threads` / 不支持 seed | `termination_criteria.simple_optimality_criteria.eps_optimal_relative` / `.eps_optimal_absolute` |
| `ortools_clp` | `SetTimeLimit` 毫秒，向上取整 | 不支持 | 不支持原生 objective gap |
| `cp_sat` | `max_time_in_seconds` 秒 | `num_search_workers` / `random_seed` | `relative_gap_limit` / `absolute_gap_limit` |

Pyomo 插件直接使用其原生参数名；CVXPY 适配器按所选 wrapper 传入直接 keyword 或专用 protobuf，不提供通用原始参数入口。GLPK 与 Xpress 的时间控制是整数秒，按秒向上取整；Xpress 使用负 `maxtime`，以便在尚无 incumbent 时也按经过时间停止。GLOP 与 OR-Tools CLP 接口接收整数毫秒，按毫秒向上取整。DOcplex 的 `TimeLimit` 默认可按 CPU 时间计量，因此适配器同时指定 elapsed `TimeMode`。IPOPT 的 `max_cpu_time` 不作为 wall-time 上限。CBC 的 seed 0 会触发按时间播种，因此显式值 0 不可映射为固定 seed。

若所选适配器拒绝可选的 `warm_start`，Owner 可在用户的 backend、成本和资源限制内，根据模型适用性、目标精度及所需结论，选择不请求该 hint 并继续使用合适的 backend，或显式改选适用的 backend；普通求解委托已授权 Owner 作此选择，不必机械先运行一次不请求该 hint 的求解。不得自动 fallback 或静默换 solver。改选时用 `optagent.case_revise_candidate` 登记新的 `backend_id`，并在 `closure_note` 简述选择理由；该说明只是 Owner 的决策记录，不证明 backend 能力、许可或运行成功。若 Source、模型和数据未变，无需重新捕获；准备并启动新 backend 的运行。换 backend 或可选 flag 不证明旧 job 已停止；旧 Run 仍为 `unknown` 或 `requested` 时，遵循现有状态和显式 supersede 合同，不因可选 flag 盲目并行启动。若改动改变变量域、目标、约束或题意解释（例如 MIP 改成松弛模型），须明确登记并复核相应数学变化；若只是结论要求收窄，可保留模型，但 claim 必须按实际证据收窄，不能把局部结论写成全局结论。新 Candidate 不会自动继承旧 Candidate 的 ready Review 或 Ref；仍须建立当前有效的绑定并核验新 Result 与适用的 bound，适用的独立数学审查内容可定向复用。`attempt_reason` 只用于现有 `requested`/`unknown` Run 的显式 supersede，不用于普通 backend 改选。

CP-SAT 的已配置 `SatParameters` protobuf 是提交给 `CpSolver.Solve` 的请求对象；从同一对象读取字段只是回显，不是 native solver 的独立读回。因此本适配器使用 `submitted_no_readback` 记录“已提交、未读回”，effective 原生字段保持 null；该状态不表示请求未传入，也不证明求解器已确认或应用。一般而言，`requested_options` 表示请求，适配器映射并提交的参数不证明当前 native build 已接受或生效；`effective_options` 只记录独立读回的值，否则保持 null。所选接口没有受支持映射时应拒绝该显式请求。

`core_criterion_only` 说明该 gap 字段是 Core 验收标准的来源，不表示同值的原生 gap 没有传入后端；表中确有映射时，原生设置另记于 `requested_options.native.parameters`。原生 gap 只影响求解器的停止设置，不构成 Core 闭合证明。Core 始终使用独立核验的原始目标与可信 global bound，并按下方冻结的 Core 标准复算。求解器对相对 gap 的分母可能不同于 Core；例如 COPT 按 primal/dual bound 绝对值的较大者归一化，CVXPY bridge 也会去掉目标常数。因此，当目标接近零或存在较大常数平移时，原生相对 gap 与 Core 结果可能不同；不得把原生 gap 读回值或 `optimal` 状态说成 Core 已闭合。

Pyomo sparse repn 是 CVXPY bridge 的输入；所有返回点仍回到原 Pyomo 模型，经同一个 ModelCheck 和业务 checker 独立核验。凸性不满足时不修补表达式。缺少可信 native global bound 的接口可以提供经检查的 incumbent，但 Result 的 bound/gap 为 null，依赖量化闭合的 lex 后续层停止。商业许可与规模上限须由本次真实运行身份支持；小型测试成功不能证明任意规模或正式任务许可。

## 读取统一数据

builder 只通过传入的 `DataBundle` 读取规范化数据。当前接口包括：

| 方法或字段 | 当前含义 |
|---|---|
| `bundle.scalar(object_id)` | 读取 scalar 对象的值 |
| `bundle.set(object_id)` | 读取 set 对象，返回 tuple |
| `bundle.relation(object_id)` | 读取 relation 对象，返回 tuple-of-tuples |
| `bundle.json(object_id)` | 读取 `kind="json"` 的完整选定节点；每次返回独立的普通 dict/list/标量/null 树 |
| `bundle.table(object_id)` | 对 `kind="table"` 返回只读映射：键是按 `key_fields` 顺序组成的 typed tuple，值是含全部列名和值的只读 mapping。外层/行字段均不可变。 |
| `bundle.rows(object_id)` | 按规范化顺序返回 tuple；table/relation 的每项是列名到值的 mapping，scalar 是单项值 tuple，set 是成员值 tuple。JSON 对象使用 `bundle.json()`，不作表格行猜测。 |
| `bundle.object(object_id)` | 读取规范化对象记录 |
| `bundle.instance_context` | 当前实例上下文；包含 typed `instance_key`、参数对象 ID、当前参数行 mapping 及绑定摘要 |

在下面的 `jobs` 示例中，`bundle.rows("jobs")` 的形状为 `({"job_id": 10, "demand": 2.0}, {"job_id": 20, "demand": 1.0})`，保持规范化输入顺序；每行仍是只读 mapping。table 的行键由 `bundle.table()` 的 typed tuple 表达，不需要从行内容自行猜测主键。

一个规范化 table 的 `inputs.json` 对象形状如下。规范化 table 和 relation 必须在 `key_fields` 中声明至少一个已声明列名作为主键；每行的键值必须是非空字符串或整数，不能是布尔值、null 或浮点数，tuple 顺序保持与 `key_fields` 一致。重复键、缺列或不匹配的值类型由 loader 拒绝。

```json
{
  "schema_version": 1,
  "instance_parameter_object_id": null,
  "objects": [
    {
      "object_id": "jobs",
      "kind": "table",
      "key_fields": ["job_id"],
      "columns": [
        {"name": "job_id", "type": "integer", "unit": null, "nullable": false},
        {"name": "demand", "type": "number", "unit": "kg", "nullable": false}
      ],
      "values": [
        {"job_id": 10, "demand": 2.0},
        {"job_id": 20, "demand": 1.0}
      ]
    }
  ]
}
```

对此对象，`bundle.table("jobs")` 的读取形状为 `{(10,): {"job_id": 10, "demand": 2.0}, (20,): {"job_id": 20, "demand": 1.0}}`。最小 builder 读取与建模例子：

```python
from pyomo.environ import ConcreteModel, Constraint, NonNegativeReals, Objective, Set, Var, minimize


def build_model(bundle):
    jobs = bundle.table("jobs")
    model = ConcreteModel()
    model.jobs = Set(initialize=[job_id for (job_id,) in jobs], ordered=True)
    model.quantity = Var(model.jobs, domain=NonNegativeReals)
    model.meets_demand = Constraint(
        model.jobs,
        rule=lambda m, job_id: m.quantity[job_id] >= jobs[(job_id,)]["demand"],
    )
    model.total_quantity = Objective(
        expr=sum(model.quantity[job_id] for job_id in model.jobs),
        sense=minimize,
    )
    return model
```

需要保留 JSON 原生嵌套结构时，使用 `format="json"`、已有 RFC6901 `layout.pointer` 和 `kind="json"`。该块的 `key_fields`、`columns` 及其目标 conversion 必须为空；归一化与 `inputs.json` 都用单元素 `values` 数组包住选定节点：

```json
{
  "object_id": "network",
  "kind": "json",
  "key_fields": [],
  "columns": [],
  "values": [{
    "nodes": ["A", "A", "B"],
    "cost": [[0, null], [9007199254740993, 0]],
    "metadata": {}
  }]
}
```

`bundle.json("network")` 返回 `values[0]`，保留数组次序和重复项、null、空容器、对象键及整数值；每次调用都会生成可独立修改的普通 JSON 树，不会改动 checker 后续读取的数据。该对象不支持 `rows/table/scalar/set/relation`，也不能作为 `instance_parameter_object_id`；需要按实例运行时，仍须使用显式 table。JSON 导入只选择一个确定节点，不做 reshape、join、筛选或单位换算。

native worker 输出中变量 witness 用真实 Pyomo 组件名和索引键：`component="quantity"`，`key=[10]` 或 `[20]`。direct_python 也按相同 typed-key 约定表示变量；标量变量用 `key=[]`，复合索引按原顺序保留，例如 `(10, "east")` 写成 `[10, "east"]`，不压成显示字符串或删掉有业务含义的索引。键元素仅支持字符串或整数。

worker 为每个运行实例传入对应的 bundle 视图。使用参数表时，可从 `bundle.instance_context.parameters` 读取当前行；不要自行遍历原始参数文件来重建实例。scalar 示例不依赖实例参数表。

要区分两种 table 用法：普通的一张长键值表可以是**一个模型实例中的多项参数**，builder 用 `bundle.table(object_id)` 读取整张表；只有用户明确要“每个主键行分别建模/运行”时，才将该 table 的 ID 登记为 `instance_parameter_object_id`。此时每行形成一个 typed `instance_key`，`bundle.instance_context.parameters` 是当前行，而 `bundle.table(object_id)` 仍能读取完整规范化表。文件名（包括 `parameters.csv`）和行数都不会自动决定 batch。

## 登记目标和模块

通过 `optagent.case_revise_candidate` 登记模型路径、数据绑定、所选 `backend_id`、`local_module_paths` 和 `objective_plan`；候选入口类型由 backend 决定，不传 `authoring`。该函数名已固定 `revise` 与 `candidate`，参数中不传根 `op` 或 `kind`；其余必需业务字段按 schema 提供。native backend 按模型变量域、线性/二次结构、凸性和所需界选择；direct_python 用于由普通 Python 算法直接产生方案的场景。许可或模型族不满足时使用真实失败状态，不静默换用另一个引擎。native 目标计划可为 `scalar`、`feasibility` 或 `lexicographic`；direct_python 只支持前两者。`scalar` 使用 `objective_component`、`sense`、`unit` 和 `preference_note`；`feasibility` 只登记 `preference_note`；`lexicographic` 以 `layers` 列出各层的 `objective_component`、`sense`、`unit`、`absolute_tolerance` 和 `relative_tolerance`，并登记 `preference_note`。例如上面的模型可用以下 scalar 计划，`objective_component` 必须是模型里的组件名：

```json
{
  "mode": "scalar",
  "objective_component": "total_quantity",
  "sense": "min",
  "unit": "quantity",
  "preference_note": "满足最低数量后最小化总数量"
}
```

不要给模型附加 schema 中不存在的 `NativeBuild` 或其他元数据，也不要把目标方向、单位或层级藏在自造字段中；按当前 `objective_plan` 登记。目标计划只定义求解目标，不替代来源中的业务约束。

optagent.run_prepare 必须显式提供有限正数 execution_budget_seconds、relative_gap 和 absolute_gap；本地 Core 不读取 ReasLab 会话偏好，也不为这些值填入隐式配置。Owner 根据用户资源与精度要求、模型规模和所需结论选择总预算及 gap；用户已给定数值时按原值传入。Core 用该预算覆盖本地 worker 的求解、检查和证据封存，并分别记录求解器原生时限及适配器实际生效选项。求解器的 optimal 标签或提交的 gap 值本身不是最优性证明。

relative_gap 和 absolute_gap 是 Core 的闭合目标，run_prepare 必须显式传入两者。按用户要求的精度选择；用户未指定时可用 relative 1e-6、absolute 0 作为执行请求，但只有独立检查通过且存在方向正确、适用的可信 global bound 时才能称为达到闭合标准。feasibility_tolerance 和 integrality_tolerance 是独立模型检查标准，不表示后端采用了相同容差。not_closed 表示该 Result 未达到冻结标准，或缺少可信全局界/完整 checked point；若同一 Result 已核验可行 incumbent 和适用 global bound，可分别报告可行性与界。

若要核验一个指定候选点，可用 direct_python 直接构造该点并让 BusinessCheck 核验，或在已登记的 Pyomo builder 中对每个需要取定的决策变量调用 `m.var.fix(value)`；两者都按正常流程登记候选、运行 `optagent.run_prepare` 并对该项调用一次 `optagent.run_start`，最后用 `optagent.run_inspect` 检查 Run 与 Result。Pyomo 的 `initialize=` 只提供求解初值，不构成 witness；固定变量后 native ModelCheck 通过，只能支持该点对当前模型约束的可行性，不能证明原问题最优或提供原问题的 global bound。direct_python 的 BusinessCheck 核验原题业务义务，但不生成 ModelCheck 或全局最优性证据。`execution_budget_seconds` 覆盖 worker 运行、检查及结果封存等阶段；native 情况还应在 solver 原生时限之外为其余阶段留出余量。系统不会自动收紧 native 时限来适配总预算，也不应把某个固定内存上限写成通用政策。

## 本地 Case 与依赖

只有 Core 返回 editable_case.status=generated 时，才能说明可编辑 Case 已生成。生成的 Case 使用实际 artifact_dir，包含可复跑的 run.py、requirements.txt、code/、data/ 与结果摘要；真实输入位置以本次返回的 inputs_path 为准。项目内部状态保存在 .reaslab/optagent/，Case 根中的 .optagent/ 保存该 Case 的运行配置与 helper。首次写模型代码前须创建并向用户展示 model.md；完整报告仅在用户要求时生成，未指定格式时默认 Markdown。实际 Core Run、Review、Delivery 状态和文件路径以本次响应为准。

`README.md` 是用户维护的说明文件：缺失时 Core 才生成默认内容；已有用户文件或用户修改后的字节原样保留且不重新认领，即使它恰好与默认建议相同。此前由 Core 生成且所有权 Ref 仍与当前字节一致的默认 README 可更新。若 README 路径属于当前或此前 Source、或是 Review 批准的主成品路径，即使旧导出曾声称拥有它也必须保留，并在新所有权记录中 relinquish。成功生成其他本地运行闭包时，receipt 的 `readme_preserved=true` 表示 README 已保留。该规则不跳过 `run.py`、模型代码、输入或 Case 根 `.optagent/` 运行依赖；这些必需路径若与未拥有文件冲突，仍拒绝投影，不拼接混合版本。

`deliver.select` 成功与可编辑 Case 投影成功是两件事。Owner 只有在返回的 `editable_case.status=generated` 时才能说本地可编辑案例已生成；`not_generated` 或 `partial` 应按实际状态说明，不由 selected 推断。

新 Owner 从 `code/` 创建模型、业务 checker 和必要模块，并登记真实可见路径；不要选后再移动源码或改 Prepared pin。已有 Candidate 声明的外部源码闭包在本地 Case 中快照一份到 `code/`，保持相对目录和逻辑导入映射。原始源码不搬动，两份源码不双向同步。导出返回的 `inputs_path` 指明唯一的本地执行输入，默认是 `data/inputs.json`；输入保留 DataBundle 对象结构，原始 CSV/PDF 等仍是来源，修改来源不自动更新它。

根 `run.py` 根据自身位置识别 Case 根，通过 schema 1 的 `.optagent/run.json` 读取实际输入路径与执行选项，并从 `.optagent/runtime.zip` 导入八个必需的纯 Python Core helper。用户可在 Case 根执行 python run.py solve；人读方案与摘要位于 `results/local/solution.json` 和 `results/local/summary.md`，执行记录及 raw artifacts 位于 `results/local/.optagent/`。随后执行 python run.py check，摘要位于 `results/check/summary.md`，检查记录及所需 artifacts 位于 `results/check/.optagent/`。这些 `.optagent` 子目录包含运行闭包或原始机器记录，复制 Case 或清理临时材料时应保留；可见摘要和方案不移入其中。无机器点的 selected 文件可被 check 明确报告为无可核对点，不会生成或补造解。Pyomo native check 按当前 builder 重建并检查保存点；原生 CpModel check-only 只核验当前保存的业务方案 `S`、当前输入、BusinessCheck 与目标，不调用 solver 或 extractor，也不独立重检原生 global constraints；direct_python check 同样只核验指定保存点和当前 BusinessCheck，不调用 `solve`。本地 solve/check 均不继承初始 Review；需要新的 Core 数值结果时，重新捕获变更、登记候选并运行。实际输入路径和运行配置都使用 schema 1。整份 Case 复制或改名后仍可本地运行；目录改名不会迁移Core 登记的 `artifact_dir`。

model.md 是首次编写当前模型建模或求解代码前展示的简短数学说明；数学语义实质变化时先更新并向用户展示，再改代码。若该说明本身就是用户要求的最终模型说明，可将其用作 Review 的 report_path。需要完整最终报告且用户未指定格式时默认使用 Markdown；报告语言遵循当前请求，未指定时按题目材料和用户交流语言决定。Markdown 默认使用 report.md；PDF 报告以 paper/main.pdf 为最终主成品。最终成品路径由当前 Review 返回的 answer_ref 指定。ready 后不要改写绑定成品；local solve/check 不覆盖成品，也不把本地结果说成沿用了旧 Review。

工作区根 `requirements.txt` 按 `run_prepare` 时的原字节冻结并进入交付。Owner 应在该文件声明 direct 算法、业务 checker 和 native 适配器实际需要的 Python 包。Result 记录实际运行时所选 backend 接口包版本；direct_python 还按同一冻结文件中可识别的标准分发包名称记录当前环境中已安装版本，包括带 marker 的声明；这只是名称级环境观测，不声称包一定被算法导入或 marker 条件一定生效。导出保留声明原文，并对无版本约束的已观测依赖添加运行版本 pin；带约束、marker 或 URL 的声明保持原样，实际版本作为注释随包提供，不改写成可能冲突或改变条件语义的安装约束。没有 Result 或未观测到的版本不会被猜测。Core 仅在文件不存在或仍与此前 Core-owned Ref 完全一致时生成/更新默认内容；Owner 已有或改过的文件按原字节保留且不重新认领。Core 不展开 `-r`/`-c` include；`run_prepare` 遇到时会拒绝。Core 不为模型自动安装 `requirements.txt`；用户本地复现可用 `pip install -r requirements.txt` 配置已声明的 Python 包，Python 解释器、Pyomo、native solver 可执行文件、系统运行库与许可仍按实际环境准备。

## 业务方案提取与 checker ABI

`extract_solution(bundle, witness)` 的 `bundle` 是当前实例 `DataBundle`；`witness` 的顶层字段固定为 `input_digest`、`instance_key`、`instance_binding_digest`、`variables`、`objective_values`。每条 witness variable 包含 `component`、`key`、`domain`、`value`、`origin`；Pyomo native `component` 是 Pyomo 变量组件名，原生 CpModel 的 `component` 与 typed `key` 必须由 builder 显式映射到模型变量，再由 Core 转为内部 proto-index 投影；direct_python 则由算法声明稳定的变量名；`key` 是 typed key 数组，元素只能是字符串或整数（布尔值不算整数），标量变量使用空数组。复合和其他业务索引按原顺序逐项保留，例如 `(10, "east")` 对应 `[10, "east"]`；不要把 key 扁平化成字符串或替换成无业务依据的序号。`domain` 是字符串或 `null`，`value` 是有限数值且不能是布尔值；native `origin` 为 `fixed_model_value`、`solver_primal` 或 `deterministic_completion`，direct_python 的 `origin` 由运行时固定为 `direct_python`。Pyomo native 中，受支持 integer/binary 域的 `solver_primal` 若距最近整数不超过既定 integrality tolerance，ModelCheck 会将其规范化为最近整数；Witness、目标、变量界和全部约束行均核验同一规范化点。原始求解器向量另存于 `raw-primals.jsonl`，`origin` 表示值来源；超出容差仍报告整数核验失败。`instance_key` 同样保留字符串/整数类型及原顺序。

`extract_solution` 按用户明确给出的字段、索引和省略规则，从当前输入与已检查 witness 构造并区分业务方案 `S` 中的决策。`solution_schema` 可以是描述性约定，不要假定它符合标准 JSON Schema，也不要让 Core 猜测字段或变量族。在交给 checker 前，模型代码应完成变量映射、索引、轮次和有限 JSON 值处理。若另指定输出文件，应写出同一投影并读回实际文件核对；checker 核验传入的实际 `S`，不能用另一内存映射或内部 witness 代替。worker 会把这份 `S` 交给冻结 checker 的 `check_solution(bundle, S)`；checker 只核验传入方案，不得返回另一个 `solution`。Core 将同一份预检查 `S` 放入既有 `BusinessCheck.solution`，无论检查通过、失败还是未覆盖。若 checker 抛错或改动 `S`，该次检查不会通过，Core 保留调用前的有效方案。方案可以是 null、有限数字、字符串、数组或仅含字符串键的对象；循环引用、非有限数和其他 Python 对象不属于有效 JSON。

`check_solution` 必须返回恰含 `status`、`checked_obligations`、`objective_values`、`violations`、`uncovered` 的对象。`status` 为 `pass`、`fail` 或 `uncovered`；Core 根据本次冻结输入和当前实例补上既有 BusinessCheck 身份字段。`checked_obligations` 的每项字段为 `label`、`source_locations`、`evidence`，其中每个 source location 恰含 `source_id` 与 `locator`；`violations` 和 `uncovered` 是字符串数组。每个 `source_id` 应指向本候选实际捕获的 Source 记录；`locator` 尽可能写明路径、行、字段或相应位置。Core 只校验字段形状，不证明 Source 可达或覆盖原题义务；Verifier 需核实来源和覆盖，无法定位或支持的义务标为 `uncovered`。

本地 Case 从 `data/inputs.json` 重建 `DataBundle`，不带导入定位信息，因此 `source_locations` 合法为空数组；checker 不应硬编码Core 捕获的 Source ID。

`objective_values` 是按顺序排列的行数组，每行恰含 `component`、`sense`、`unit`、`value`，数值必须有限且不能是布尔值。checker 应按 Source 明示的原始业务域与约束核验传入的实际 `S`，并从 `bundle` 与 `S` 独立重算目标值。Candidate 或算法因变量族、候选表示或搜索范围造成的额外限制不构成 Source 违例；checker 未核验的原域范围须记录在 `uncovered`。不要原样回传 witness 或旧 Result 的目标值。native/direct Run 只有在业务检查通过且重算值与当前机器点目标一致时才记为通过；原生 CpModel 的整数目标按真实 proto 精确重算并对账。check-only 会对保存的当前 `S` 重算，不用旧 Result 目标值否决或替代这次业务检查，也不调用 extractor、solve 或求解器。当前 ModelCheck、历史 Review、native bound、全局最优性与当前 `S` 的业务可行性是分开事实；本地 check 不会把旧证据转授给用户编辑后的方案，全局最优性保持 `not_checked`。既有 `rel_tol=1e-8`、`abs_tol=1e-8` 只适用于通用数值目标比较，不用于原生整数目标、bound、gap 或 lexicographic lock。

下面用前述 `jobs` table 和 `quantity` 变量说明两段函数：提取器把 witness 转成业务方案；checker 从当前 table 与传入方案重建需求及目标，并返回检查记录。`objective_values` 的组件名、方向和单位须与登记的目标一致；`evidence` 应写入本次核对的具体数值，来源位置沿用真实导入记录。

```python
def extract_solution(bundle, witness):
    quantities = {}
    for variable in witness["variables"]:
        if variable["component"] == "quantity":
            (job_id,) = variable["key"]
            quantities[str(job_id)] = variable["value"]
    return {"quantity_by_job": quantities}


def check_solution(bundle, solution):
    jobs = bundle.table("jobs")
    quantities = solution["quantity_by_job"]
    violations = []
    evidence = []
    for (job_id,), row in jobs.items():
        amount = quantities[str(job_id)]
        demand = row["demand"]
        evidence.append(f"任务 {job_id}：安排 {amount:g} kg，需求 {demand:g} kg")
        if amount < demand:
            violations.append(f"任务 {job_id} 的安排低于需求")

    total_quantity = sum(quantities[str(job_id)] for (job_id,) in jobs)
    return {
        "status": "fail" if violations else "pass",
        "checked_obligations": [{
            "label": "每项安排满足对应需求",
            "source_locations": [dict(location) for location in bundle.object("jobs").source_locations],
            "evidence": "; ".join(evidence),
        }],
        "objective_values": [{
            "component": "total_quantity",
            "sense": "min",
            "unit": "kg",
            "value": total_quantity,
        }],
        "violations": violations,
        "uncovered": [],
    }
```

`label` 用来标识实际核对的业务义务，`evidence` 应呈现可读方案或具体检查数值；仅写“已检查需求”不足。没有完整业务方案时不要拼出成功结果；保留真实违例，或将不能支持的义务记在 `uncovered`。模型提取方案与 checker 核验方案分工明确；不在 checker 中从 witness 另造或替换方案。

## 可读业务结果

本地求解摘要复用本次 worker 已产生的模型检查、业务核对状态与 `business_check.checked_obligations[].label/evidence`；Core 保存的结构化方案位于 `BusinessCheck.solution`，摘要只说明方案已随结果 JSON 保存，不展开大 payload。渲染不额外建模、不再次调用 checker 或 LLM。Core 不猜变量名来还原路线、排班或物理量。因此，若用户需要可执行的业务方案，模型应在 `extract_solution` 中从 `bundle` 与 witness 恢复业务决策，checker 再核对同一方案并在相关义务 `evidence` 中写明关键事实。仅写“已重算容量”或“已检查排班”等方法说明不足以让人复现方案；无需在每条义务中重复完整计划。

只把实际核对过的业务事实放入对应义务；不可行、缺少 witness 或无法重构时保留真实 `violations`/`uncovered`，不要生成看似完整的成功计划。辅助下界、上界和松弛量不是物理测量值，除非按实际决策作了有依据的重构。业务证据使用现有字符串字段，不增加 `format_solution`、`artifacts` 等回调或新的展示 ABI。
