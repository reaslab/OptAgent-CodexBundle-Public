# 原生 CpModel builder 接口

本参考适用于本地 Core 运行的 backend_id="cp_sat" 原生分支。公开候选参数仍只有既有 `backend_id`；不添加 `authoring`、`adapter_kind` 或目标标签参数。既有 `Pyomo ConcreteModel` builder 继续有效，Core 按 `build_model(bundle)` 的实际返回类型选择分支。

## 返回对象

`build_model(bundle)` 返回一个只有 `model` 与 `variables` 两项的对象：

```python
return {
    "model": actual_cp_model,
    "variables": {
        "start": {(source_id,): start_var[source_id] for source_id in source_ids},
    },
}
```

`model` 必须是真实 OR-Tools `CpModel`。`variables` 将公开业务身份映射到该模型中的实际 `IntVar`（普通 `BoolVar` 也属于该类型）；Core 只接受变量对象，不接受裸 proto index、affine expression 或 `Not()` 返回的 NegatedBool。Core 核对变量对象属于返回的模型后，从对象取得 proto index，并继续使用原整数映射生成 Witness 和封存证据。不要依赖 variable name、`repr` 或排序推断业务身份。typed key 保留字符串/整数类型和原顺序，布尔值不作为整数 key。不同业务身份可以映射到同一个变量对象，作为合法 alias。此映射只声明身份绑定，不证明该身份在题意上的含义；`extract_solution`、BusinessCheck 与 Verifier 仍负责原域方案恢复和来源核验。

原始 solver response 向量包含模型中的全部 proto variables，包括未映射到业务身份的辅助变量。Core 对完整向量进行机械核验，再按 `variables` 映射生成 typed Witness；不得只返回投影变量、把辅助值补零，或用初值补缺值。builder 不返回 `CpSolver`、solver status、incumbent、目标界或检查结果。

## 目标与支持范围

目标名称、方向和单位来自既有唯一 scalar `objective_plan`，不得在 builder 返回对象中重复声明。scalar 目标应与实际 CpModel 目标一致；feasibility 计划须使用无目标的 CpModel。原生分支不支持 lexicographic 目标，遇到该计划会明确拒绝。Core 按真实整数 proto 目标重算并精确比较目标值；不接受 builder 自报 bound，也不把 direct_python 内部求解器的 bound 升格为Core 验证的 native bound。首版只支持有限整数域与精确整数目标；变量域边界/值及目标活动范围须在 `±(2^53−1)` 内。浮点目标、超出该精度范围或不能无损核验的表达式应按实际 capability-uncovered/unsupported 结果处理，不自动舍入、缩放或换单位。

该入口可以使用 OR-Tools CpModel 表达式及其支持的整数 global constraints。它只表示所构造的模型被提交求解；Source 语义、集合与索引、业务点恢复、目标含义和约束覆盖仍须由业务检查与 Verifier 独立核对。若 Source 是连续模型，而 CP-SAT 方案把变量限制为整数，须分别报告该模型是否等价于 Source，以及是否有证据表明它仅保持最优值；整数最优解存在不等于两个可行域相同。

## ModelCheck 的范围

该原生分支的检查方法为 `core_cp_sat_response`。`pass` 表示同一次 CpSolver 调用接受了该点，并且 Core 机械核对了：

- 完整 int64 response vector；
- proto 变量域；
- 由变量对象归一化出的 typed identity 到 proto index 投影；
- 按整数 proto 目标精确重算的目标值。

这不表示 Core 独立解释或逐条重核了原生 global constraints，也不证明 Source 到模型的映射正确。无点或 `UNKNOWN` 保留 uncovered；向量、域、身份绑定或目标机械核对失败则为 fail。ModelCheck 不替代既有 BusinessCheck 或独立 Verifier；Business fail/uncovered 仍按现有语义处理，不增加原生专属的业务通过门。

## 本地运行与导出

本地 `solve` 会使用当前输入、重建当前 builder，并由 Core 再次调用 CpSolver。check-only 只检查当前保存的业务方案 `S`、输入、BusinessCheck 和目标；不重新求解、不重新提取方案，也不继承旧 solver response 或 bound。没有本地原生 global evaluator 时，check-only 明确说明原生 global constraints 未重检。

原生模型导出为可读的 `.textproto`。模型代码、提取器、业务 checker 和所需依赖仍须随可编辑 Case 闭包提供。`warm_start=true` 当前明确拒绝；builder 若在 CpModel 中嵌入 `solution_hint`，须据实记录其存在，不能称为冷启动。`warm_start=false` 只表示 Core 不请求额外 warm-start 提交，并不证明模型没有内嵌 hint。OR-Tools 运行时版本以实际 worker 读回为准；内部版本标记不是可直接钉入 PyPI `requirements.txt` 的版本号。
