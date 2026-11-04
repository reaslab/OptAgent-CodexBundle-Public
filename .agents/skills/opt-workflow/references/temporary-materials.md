# 一次性材料

一次性脚本和中间请求文件放在项目内部目录 .reaslab/optagent/tmp/ 下独立的临时子目录。短小的数值检查可直接使用本地 Python；较长或需复用的检查写成脚本后从项目 shell 执行。不要把临时脚本放入可编辑 Case 或用户输入目录，也不要把凭证写入请求文件。

可编辑 Case 中的 .optagent/ 是随 Case 保留的运行配置与 helper，不是项目临时目录。results/local/.optagent/ 与 results/check/.optagent/ 保存各自阶段的机器记录和原始产物；保留这些文件才能维持该 Case 的 solve/check 入口。

本地 Core 调用统一使用已选定的项目 Python 执行 `-m optagent_core.local_cli`，或使用当前 Codex 进程已安装的 `optagent-local` 入口，并把当前工作目录设为项目根；优先使用 `.venv` 中的项目解释器，不假定通用系统 `python` 已安装 Core，也不在每个任务中重装依赖或搜索源码。CLI 没有 `--workspace` 参数。将请求写为 UTF-8 JSON 文件，放入该 Core 状态目录下的独立临时子目录，再通过 `--json-file` 提交。可用 `schema <operation>` 查看公开操作所需的输入字段。独立的已导出 Case 按其 README 在 Case 根运行。

临时材料只在确认不再被运行、审查或诊断引用后清理。用户输入、正式源码、checker、交付物以及 Case 运行闭包不属于临时材料。
