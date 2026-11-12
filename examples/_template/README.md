# 新案例模板（examples/_template）

本目录只说明 `le-case-v1` 案例配置的字段与新建流程，**不是可运行案例**：
`examples/_template/case.json` 里的路径与数值都是占位/空值，直接运行会失败。
公开版不附带任何真实案例、存档、构筑 revision 或历史报告。

## 推荐流程：让 `le.ps1 import` 生成真实案例

```powershell
# 先获取 dataset 并自检（见仓库 README）
.\scripts\fetch_dataset.ps1 -Repo BlaCatFishLE/LastEpoch-AIBuildSimulator
.\scripts\le.ps1 doctor

# 用你自己的离线存档副本创建案例目录；<case> 只允许字母/数字/下划线/短横线
.\scripts\le.ps1 import --case examples\<case>\case.json --save '<存档路径>'
```

骨架导入会生成：

- `examples/<case>/case.json`：本模板同结构的案例配置；
- `examples/<case>/inputs/bd.json`：clean-room 解析存档得到的 BD 文档（供查看/后续准备）。

需要自动生成可计算的 BuildSpec / calculation-input / snapshot（完整首接）时，需额外提供
主技能与模型，并自备可选外部工具 Node.js + lastepochsupertools（不随仓库分发）：

```powershell
.\scripts\le.ps1 import --case examples\<case>\case.json --save '<存档路径>' `
    --main-skill bh2 --model black-hole --tool-home '<lastepochsupertools 安装目录>'
```

`--tool-home` 也可用环境变量 `LE_TOOL_NATIVE_HOME` 提供；Node.js 用 `LE_NODE` 或 PATH 中的
`node`。缺少这两项时 `import`/`prepare` 会明确返回 `needs_input`（不会静默换路线），
这说明完整首接尚不具备条件，而不是案例配置写错。支持范围与限制见仓库 README 与 docs。

## 也可以复制模板后手工补齐

把本目录 `case.json` 复制到 `examples\<case>\case.json`，至少修改：

| 字段 | 说明 |
|---|---|
| `case_id` | 稳定的小写短横线 ID，必须与目录名一致（工具按目录名发布/输出） |
| `title` / `description` / `created` | 本次构筑与场景说明 |
| `build` | 引用的 BuildSpec `build_dir` / `revision_id` / `content_sha256` / `build_spec` 等；由 `le_build import` 发布后填写 |
| `ruleset` / `conditional_registry` | 本次计算使用的规则包路径与 `sha256`（规则文件随公开版分发，路径在 `tools/phase3/rules/`） |
| `inputs.*` | `bd` / `calculation_input` / `snapshot` / `scenario` 等实际文件路径（相对仓库根） |
| `source.*` | 存档或 planner payload 的冻结副本路径；与 `input_sha256` 一致 |
| `input_sha256` | 每个冻结输入文件的 sha256，validate 会核对 |
| `snapshot_expectations` | 本案例计算输入的结构计数（items/mods/passive_nodes/…）；换 BD 必须重新声明 |
| `output.root` | 本地输出目录，例如 `reports/<case>`（用户产物，不入库） |
| `model` / `main_skill` / `calculation` | 仅当该案例进入统一计算时填写；由 `import`/`prepare` 按当前支持范围生成 |

注意：

- 不得复用其它案例的 `inputs/corrections.json`、折射选择、场景或 `expected-summary.json`。
- `reports/`、`data/builds/`、`data/raw/` 都是本地产物目录（已 gitignore），不要提交。
- 字段语义与校验规则见 `docs/contracts/workflow-interface.md`、`docs/contracts/save-onboarding.md`。
