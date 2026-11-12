# scripts/le_pipeline — P3 输入路线与外部依赖描述

日期：2026-11-07（D2 修复：三路线模型）。状态：**P3 实现中**（本目录只读描述/检查；
执行由 `scripts/le_commands.py` 的 `prepare` 编排调用，本任务**不修改** `le_commands.py`）。
依据：[workflow-interface §4 阶段契约](../../docs/contracts/workflow-interface.md)、
[workflow-interface §8 模块落点](../../docs/contracts/workflow-interface.md)、
[workflow.md](../../docs/workflow.md)（流水线操作流程）。

## 1. 这个目录解决什么

三条已支持输入路线（存档 → tool-native wire → BuildSpec；存档 + planner 混合 → BuildSpec；
planner payload → BuildSpec）各自依赖哪些既有脚本、外部运行时、仓库文件与本地配置，以前散落在
`reproduce`/`import` 与文档里。本目录把这些**写成可发现、可检查、无个人绝对路径的数据**：

| 文件 | 职责 |
|---|---|
| `routes.py` | `RouteSpec` 目录（三条路线）、`resolve_route(case)` 路线判定、stage 序列与既有命令/参数模板 |
| `dependencies.py` | 依赖目录（`Dependency`）、`check_routes(case_ref, root)` 检查入口、`needs_input` 语义 |
| `__init__.py` | 稳定公开 API |
| `README.md` | 本文（人工维护） |

约束：只用 stdlib；**不执行任何命令**、不写文件、不联网；`check_*` 失败只返回结构化结果，
不抛未捕获异常；输出只含仓库相对路径或 `<external>/<basename>`。

## 2. 三条路线

| | `save-tool-native-wire` | `save+planner` | `planner-payload` |
|---|---|---|---|
| 适用输入 | `1CHARACTERSLOT_BETA_*` 离线存档（可选 `STASH_*`） | **同一 case 同时有**离线存档与归档 planner payload（BH 案例） | 只有 LE Tools planner payload（无存档） |
| `input_kind` | `character_save` | `character_save+planner_payload` | `planner_payload` |
| case 必需字段 | `source.character_save`（BuildSpec 另有 `source.tool_native_wire` 归档或由 harness 生成） | `source.character_save` + `source.planner_build_response` | `source.planner_build_response` |
| 主技能 | 必需（`case.main_skill.ability_id` 或 `--main-skill`） | 必需（同上） | 必需（同上） |
| `stage_sequence` 长度 | 8（含 `parse_input`） | **8（含 `parse_input`）**——与 prepare 实际执行一致 | 7（`parse_input` 为可选补充） |
| BuildSpec 来源 | tool-native harness 生成的 wire → `le_build import --file` | planner payload → `le_build import --file` | planner payload → `le_build import --file` |
| 存档的用途 | `parse_input`（bd.json/来源覆盖）+ 生成 wire | `parse_input`（bd.json/来源覆盖） | 可选：有存档时补 bd.json |
| 外部运行时 | Node.js + tool-native harness + lastepochsupertools v1.3.1（仅当归档 wire 缺失时需要） | 无必需外部运行时（planner payload 离线导入；harness 仅可选） | 无（离线 payload；不联网） |
| fallback | **never**：缺 harness → `needs_input`，不改成 planner 路线 | **never**：缺任一来源 → 按字段退化为单来源路线时仍须满足该路线依赖，不得伪装 ready | **never**：缺 payload → `needs_input`，不回退存档路线 |
| 判定依据 | `source.tool_native_wire` 存在，或仅 `source.character_save` | `source.character_save` 与 `source.planner_build_response` 同时存在 | `source.planner_build_response` 存在、无存档 |
| 歧义 | `tool_native_wire` 与 `planner_build_response` 同时存在 → `resolve_route` 返回 `None`，由调用方显式指定 `source_kind`/`--save`/`--planner`（不按优先级猜） | 同左 | 同左 |

> 路线 id 稳定：`save-tool-native-wire` / `save+planner` / `planner-payload`。显式 `source_kind`
> 别名：`save`、`character_save`、`save+planner`/`save_planner`/`save-planner`/`mixed`、
> `planner`/`planner_payload`。

### 2.1 stage 序列（与 workflow-interface §4.4 对齐）

| stage_id | save 路线 | save+planner | planner 路线 | 既有入口 |
|---|---|---|---|---|
| `freeze_input` | ✓ | ✓ | ✓ | 编排层（prepare）冻结输入指纹；尚无独立脚本 |
| `parse_input` | ✓ 存档解析 | ✓ 存档 + planner 一起解析出 bd.json | 可选（有存档时补 bd.json） | `python -m le_save_import build --save … [--planner …] [--stash …]` |
| `normalize_build` | ✓ ①外部 harness 生成 wire ②`le_build import --file --offline` | ✓ `le_build import --file <planner payload> --offline` | ✓ 同左 | `tools/save_import/src/le_save_import`、`tools/build_import/src/le_build` |
| `export_calc_input` | ✓ | ✓ | ✓ | `python -m le_build export --build … --revision …` |
| `apply_affix_ranges` | ✓ | ✓ | ✓ | `tools/save_import/scripts/pipeline/run_affix_db_pipeline.py` + `apply_corrections.py` |
| `resolve_skills` | ✓ | ✓ | ✓ | P2 `resolve_build`/analysis-plan 适配器（prepare 接入点；当前无独立 CLI） |
| `prepare_snapshot` | ✓ | ✓ | ✓ | hit：`python -m le_calc hit-snapshot`；event：`tools/save_import/scripts/pipeline/make_snapshot.py` |
| `min_validate` | ✓ | ✓ | ✓ | `python scripts/le_commands.py validate --case …` |
| （`simulate`/`validate_result`/`render_report`/`commit`） | 不在 prepare 范围 | 同左 | 同左 | 由 run/serve 既有链承担 |

命令行模板中的 `{...}` 占位符见 `routes.PLACEHOLDER_TOKENS`；由 prepare 编排代入，
缺失字段必须转成 `needs_input`，**不得用默认值猜**。`le_build import` 的 `--root {root}` 传
仓库根（子进程 cwd 用 `.`，不展开机器绝对路径）。

### 2.2 tool-native harness 的依赖依据（来自实际证据，不写个人路径）

作者本地归档的 tool-native `run-summary.json` 记录了一次已验证运行（该归档证据不随公开版分发）：

- `tool`：`lastepochsupertools_v1.3.1`（第三方本地工具；只取 basename 作标识，不复制其安装路径）；
- `dbFiles`：`data/version150/db/js/*.js`（两个 DB JS，含 `window.itemDB`/`coreDB`）；
- `bootstrap`/`idEncoderSelfCheck`/`wire`/`roundtrip`：harness 在 Node.js `vm` 沙箱中加载
  `planner/js/planner.js` 与上述 DB JS，执行 `pvmBuildWireFromSave` 等纯函数（证据为作者本地归档，不随公开版分发）。

因此 save 路线在**归档 wire 缺失**（新存档输入）时，需要：

1. `external.tool_native.node`：Node.js 运行时（`config.external_tools.node.path` / `LE_NODE` /
   仓库可选 `tools/node/node.exe`）；
2. `external.tool_native.harness`：已验证 harness 脚本（`run_tool_native.js`；路径只来自
   `config.external_tools.tool_native.harness_script` / `LE_TOOL_NATIVE_HARNESS`）；
3. `external.tool_native.install`：`lastepochsupertools v1.3.1` 安装目录
   （`config.external_tools.tool_native.tool_home` / `LE_TOOL_NATIVE_HOME`），需含
   `planner/js/planner.js` 与 `data/<version>/db/js/*.js`（期望文件由 run-summary 的 `tool`/`dbFiles` 推导）。

`wire-from-save.json` 还需把 `blessings[].ir` 从标量 `0` 归一为存档字节 `[9:12]` 的 3 字节数组
（`TOOL-NATIVE.md` §7）；这一步是 P3 必须正式化的必要步骤，**不得把未归一 wire 当 planner payload**
（两者语义不同）。仓库当前**没有**该 harness 与归一脚本（只有证据），因此未配置时检查返回
`undetermined` 并给可操作 hint，而不是 `ok`。

## 3. 依赖目录与状态语义

`dependencies.DEPENDENCIES` 每条：

```text
{id, kind ∈ {python, repo_file, external_tool, local_config},
 required_for ∈ {route_id, stage_id}, label, required, routes, hint,
 resolve(root, config) -> {id, kind, resolved}; check(...) -> {status, detail, hint?}}
```

| id | kind | required_for | save | save+planner | planner | 说明 |
|---|---|---|---|---|---|---|
| `python.interpreter` | python | route | R | R | R | Python ≥ `config.python.min_version`（当前 3.12） |
| `python.module.le_save_import` | python | parse_input | R | R | R | 源码模块可导入（按 `tools/save_import/src` 布局） |
| `python.module.le_build` | python | normalize_build | R | R | R | 同上（`tools/build_import/src`） |
| `python.module.le_calc` | python | prepare_snapshot | R | R | R | 同上（`tools/phase3/src`） |
| `repo.project_config` | repo_file | route | R | R | R | `config/project.json` 存在且可解析 |
| `repo.dataset_pointer` | local_config | export_calc_input | R | R | R | `config.dataset.pointer`（默认 `data/current.json`）存在 |
| `repo.affix_db` | local_config | apply_affix_ranges | R | R | R | `case.affix_db` / `config.affix_db` 的 SQLite 存在 |
| `repo.pipeline_scripts` | repo_file | apply_affix_ranges | R | R | R | 3 个既有 pipeline 脚本 |
| `repo.le_commands` | repo_file | min_validate | R | R | R | `scripts/le_commands.py`（validate 入口） |
| `input.case_config` | local_config | route | R | R | R | case 已加载且为 JSON object |
| `input.main_skill` | local_config | resolve_skills | R | R | R | `case.main_skill.ability_id` 或 `--main-skill` |
| `input.character_save` | local_config | parse_input | **R** | **R** | O | 存档只读快照 |
| `input.stash` | local_config | parse_input | O | O | O | 可选 |
| `input.planner_payload` | local_config | normalize_build | — | **R** | **R** | 归档 planner payload（离线） |
| `input.tool_native_wire` | local_config | normalize_build | O※ | O | — | 归档 wire；声明了就必须存在，缺失由 harness 生成 |
| `input.tool_native_run_summary` | local_config | normalize_build | O | O | — | 可选证据（tool/dbFiles），用于推导 harness 期望产物 |
| `external.tool_native.node` / `.harness` / `.install` | external_tool | normalize_build | **R※** | O | — | save 新输入必需；归档 wire 存在（※）或混合来源时可选，未配置为 `undetermined` 而非 `ok` |

- **R** = required（缺失/无法判定 → `needs_input`，`ready=false`）；**O** = optional（缺失/undetermined 只进 `warnings`）。
- 动态 required：`save` 路线中 `tool_native_wire` 只在 case 声明时才必需；`external.tool_native.*`
  只在 save 路线且**归档 wire 缺失**时必需。`save+planner` 的依赖是 save（18 条）与 planner（14 条）
  的**并集（19 条）**：存档与 planner payload 都必需；harness 相关保持可选（BuildSpec 由 planner payload 提供），
  因此既有 BH 案例不会被判失败。
- 状态：`ok`（已确认可用）；`missing`（确认缺失）；`undetermined`（无法判定，**不得当作 ok**）。

`check_routes(case_ref, root, *, source_kind=None, main_skill=None, config=None)` 返回
（`le-pipeline-route-check-v1`）：

| 字段 | 说明 |
|---|---|
| `route_id` / `route` / `route_reason` | 判定出的路线与短摘要；歧义/无来源时为 null |
| `dependencies[]` | 逐依赖 `{id, kind, required_for, label, required, status, detail, hint?}` |
| `ready` | 路线已判定且所有 required 依赖均 `ok` |
| `needs_input[]` | required 依赖非 ok 的可操作条目：`{code, kind, dependency_ids, message, hint, required}` |
| `warnings[]` | 可选依赖 missing/undetermined（不阻塞 ready） |
| `fallback_policy` | 恒为 `never_fallback` |

`needs_input[].code`：

- `missing_dependency`：确认缺失（按 `hint` 补文件/模块/配置）；
- `undetermined_dependency`：无法判定（如未配置 harness），需配置后重查；
- `main_skill_required`：主技能缺失（`--main-skill` 或 `case.main_skill.ability_id`）；
- `route_selection`：case 无法判定或来源歧义（显式传 `source_kind` / `--save`/`--planner`）；
- `case_unreadable`：case 缺失/不可解析；
- `internal_error`：检查内部错误（记录并检查输入；不得据此换路线）。

`ready` 为假时，调用方必须把 `needs_input` 原样交给用户/上层询问；**不得**自动改用另一条路线
（例如 save 缺 harness 不得改成 planner payload 解析，planner 缺 payload 不得回退存档解析）。
路线由 case.source 的显式字段决定：字段减少时按规则退化为对应单来源路线（并重新检查其依赖），
不会“为了 ready 而换语义”。

## 4. 需要补的配置（本任务不改 `config/project.json`）

当前 `config/project.json` 没有 `external_tools` 段。接收方/Lead 在需要 tool-native 新输入时补：

```json
"external_tools": {
  "node": {"path": "tools/node/node.exe"},
  "tool_native": {
    "harness_script": "<仓库相对或接收方配置的 run_tool_native.js>",
    "tool_home": "<lastepochsupertools v1.3.1 安装目录>"
  }
}
```

也可用环境变量临时覆盖：`LE_NODE`、`LE_TOOL_NATIVE_HARNESS`、`LE_TOOL_NATIVE_HOME`。
路径必须由配置提供，不写个人绝对路径；`tools/node` 不随包分发（`config.project.json` 的
`outputs.package_excluded`），普通计算/测试不需要 Node。

## 5. 给 `scripts/le_commands.py`（kh-bh）的接线 API

公开 API（`scripts/` 在 `sys.path` 时 `import le_pipeline` 可用）：

```python
from le_pipeline import (
    ROUTES,                # tuple[RouteSpec]，三条路线
    list_routes,           # -> list[dict]（JSON 可序列化，含 stage/命令模板）
    get_route,             # route_id -> RouteSpec | None
    resolve_route,         # case(dict|path) -> RouteSpec | None（不猜：歧义/无来源返回 None）
    resolve_route_detail,  # -> {route, reason, candidates}
    check_routes,          # case_ref, root, *, source_kind=None, main_skill=None, config=None -> dict
    dependency_catalog,    # -> list[dict]（doctor 用）
)
```

`prepare` 推荐接线顺序（API 形状不随路线数量变化，kh-bh 无需为 `save+planner` 改 `le_commands`
逻辑，只消费返回的 route 对象）：

```python
report = check_routes(case_path, REPO_ROOT, main_skill=args.main_skill)
if not report["ready"]:
    # 不静默换路线；把 report["needs_input"] 原样交给用户/上层
    return emit_needs_input(report["needs_input"], exit_code=3)

route = get_route(report["route_id"])
for stage_id in route.stage_sequence:      # freeze_input → … → min_validate（混合路线也是 8 段）
    step = route.step(stage_id)
    # 用 step.commands 中的 argv 模板（routes.PLACEHOLDER_TOKENS）代入后执行；
    # availability=adapter_required 的 stage 由 prepare 的领域适配器实现。
```

`doctor` 可直接用 `list_routes()` 列出三条路线；`resolve_route_detail(case)` 的
`route.route_id` 会写入 `prepare-context.json`（示例：带 planner payload 的存档案例判定为 `save+planner`）。

`check_routes` 返回示例（节选）：

```json
{
  "schema": "le-pipeline-route-check-v1",
  "case_ref": "examples/<case>/case.json",
  "case_id": "<case>",
  "route_id": "save+planner",
  "route": {"route_id": "save+planner",
            "input_kind": "character_save+planner_payload",
            "requires_main_skill": true,
            "stage_sequence": ["freeze_input", "parse_input", "normalize_build",
                                "export_calc_input", "apply_affix_ranges", "resolve_skills",
                                "prepare_snapshot", "min_validate"],
            "fallback_policy": "never_fallback"},
  "ready": true,
  "dependencies": [
    {"id": "input.character_save", "kind": "local_config", "required_for": "parse_input",
     "required": true, "status": "ok", "detail": "存档：<save-path>"},
    {"id": "input.planner_payload", "kind": "local_config", "required_for": "normalize_build",
     "required": true, "status": "ok", "detail": "planner payload：examples/<case>/inputs/planner-build-response.json"},
    {"id": "external.tool_native.harness", "kind": "external_tool",
     "required_for": "normalize_build", "required": false, "status": "undetermined",
     "detail": "未配置 tool-native harness 脚本路径（无法判定本机是否有已验证 harness）",
     "hint": "在 config/project.json 增加 … external_tools …"}
  ],
  "needs_input": [],
  "warnings": [{"code": "optional_dependency_undetermined",
                "dependency_id": "external.tool_native.harness", "message": "…"}],
  "fallback_policy": "never_fallback"
}
```

只检查、不执行的独立调用（不依赖当前案例/聊天上下文）：

```python
from le_pipeline import check_routes
report = check_routes({"case_id": "new", "source": {"character_save": "data/raw/saves/x"}},
                      repo_root, source_kind="character_save", main_skill="fr11mv")
```

## 6. 本机实测状态（2026-11-07，D2 修复后；两案例 + fixture）

| 对象 | 路线 | `ready` | 关键状态 |
|---|---|---|---|
| 作者本地 BH 案例（`main_skill="bh2"`；已归档） | **save+planner** | **true** | 19 条依赖并集：输入类全 required 且 ok；harness/install 未配置 → `undetermined`（optional，warning）；wire/run-summary 未提供（optional） |
| 作者本地 BH 案例（不传主技能） | save+planner | **false** | 仅 `main_skill_required`（`--main-skill` 后 ready=true） |
| 作者本地 FR 案例（已归档） | save-tool-native-wire | **true** | 归档 wire + run-summary 存在；harness/install 未配置 → `undetermined`（optional）；stash 未提供（optional） |
| 临时 fixture：save 新输入 + 未配置 harness | save-tool-native-wire | **false** | `external.tool_native.{harness,install}` → `undetermined`，`node` → `missing`；`needs_input` 明确要求配置，**不**回退 planner |
| 临时 fixture：save 新输入 + 已配置 harness/node/tool_home | save-tool-native-wire | **true** | 外部依赖 `ok`；run-summary 的机器路径只用于推导相对文件，不出现在结果中 |
| 临时 fixture：只有 planner payload（无存档） | planner-payload | **true** | 7 段 stage；`input.character_save` 为可选；无外部 harness 依赖 |
| 临时 fixture：wire + planner 同时存在（归档歧义） | —（None） | false | `route_selection`，候选 `save-tool-native-wire`/`planner-payload`；不按优先级猜 |

## 7. 测试

```powershell
.\scripts\le.ps1 test --suite phase3
# 或只跑本组：
$env:PYTHONPATH='tools\phase3\src'
& .\.venv\Scripts\python.exe -m unittest discover -s tools\phase3\tests -p 'test_pipeline_routes.py' -v
```

测试全部使用临时目录与 monkeypatch，不依赖真实外部工具；覆盖：三路线判定（含 BH 真实 case、
混合来源、planner-only、归档歧义、显式 source_kind）、依赖并集、依赖存在/缺失/undetermined、
hint 文案、`needs_input` 不 fallback、无绝对路径、无副作用、JSON 可序列化。

## 8. 未决 / 下一步

1. `resolve_skills` 阶段当前只有契约占位（P2 `resolve_build` 适配器）；prepare 接线时由
   kh-bh 传 adapter。
2. tool-native harness 的 argv 是**契约草案**（具体参数以配置的 harness 脚本为准）；仓库尚无
   harness 与 `blessings[].ir` 归一脚本，需要 Lead 决定归档位置与命令契约后更新本文与
   `routes.py` 模板。
3. `config/project.json` 的 `external_tools` 段尚未加（本任务不改配置）；在补上之前，save 新
   输入的检查会稳定返回 `undetermined` + 可操作 hint。
4. `tools/node` 被 `config.project.json` 的 `outputs.package_excluded` 排除，接收方运行 save 新
   输入需自行配置 node 或 `LE_NODE`（普通计算/测试不需要）。
5. `prepare-context.json` 的“route”字段仍由 `le_commands` 写入；kh-bh 已把 prepare 测试的
   route 断言扩为三条（BH 记 `save+planner`），doctor 实测路线判定已显示 `save+planner`。
   本目录只读返回 route 对象，不写 `prepare-context.json`。
6. 归档歧义（wire + planner payload 同时存在）仍返回 `None` + `route_selection`；若未来需要
   “wire + planner 混合”的第四路线，需要新裁定与 fixture。
