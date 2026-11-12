# -*- coding: utf-8 -*-
"""P3 输入路线描述（纯数据，不执行任何命令）。

本模块是 ``scripts/le_pipeline`` 的“路线目录”：把三条已支持输入路线写成
可发现、可 JSON 序列化的 :class:`RouteSpec`：

* ``save-tool-native-wire``：``1CHARACTERSLOT_BETA_*`` 存档（可选 stash）
  → tool-native wire → BuildSpec revision → calculation-input；
* ``save+planner``：同一 case 同时提供存档与 planner payload（混合来源）：
  存档用于 ``parse_input``（bd.json/来源覆盖），planner payload 用于
  ``normalize_build``（BuildSpec revision）→ 后续阶段；BH 案例即此路线；
* ``planner-payload``：只有 LE Tools planner payload（无存档）→ BuildSpec revision
  → calculation-input。

设计约束（与 ``docs/contracts/workflow-interface.md`` §4.4 对齐）：

* 只描述既有脚本入口与参数模板，**不执行**命令、不读写文件、不改任何数据；
* 所有仓库路径写仓库相对形式；个人机器绝对路径不得出现在本文件；
* stage_id 使用契约里的稳定 id：``freeze_input`` → ``parse_input`` →
  ``normalize_build`` → ``export_calc_input`` → ``apply_affix_ranges`` →
  ``resolve_skills`` → ``prepare_snapshot`` → ``min_validate``（后续
  ``simulate``/``validate_result``/``render_report``/``commit`` 不在 prepare 范围）；
* 参数模板中的 ``{...}`` 占位符由调用方（``prepare`` 编排）代入；缺失字段
  **不得**用默认值猜，必须转成 ``needs_input``（见 ``dependencies.check_routes``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 路线 id（稳定；发布后不改名，语义变化用新 id）
SAVE_ROUTE_ID = "save-tool-native-wire"
SAVE_PLANNER_ROUTE_ID = "save+planner"
PLANNER_ROUTE_ID = "planner-payload"
ROUTE_IDS: tuple[str, ...] = (SAVE_ROUTE_ID, SAVE_PLANNER_ROUTE_ID, PLANNER_ROUTE_ID)

#: route fallback 语义：任何输入路线都不得静默改用语义不同的路线
FALLBACK_POLICY_NEVER = "never_fallback"

#: 参数模板占位符说明（供 prepare 实现与文档消费；不参与执行）
PLACEHOLDER_TOKENS: dict[str, str] = {
    "{python}": "Python 解释器（由 config/project.json 的 python 候选/LE_PYTHON 解析）",
    "{case}": "case.json 的仓库相对路径",
    "{root}": "仓库根；子进程 cwd 用 '.'，不展开机器绝对路径",
    "{out_dir}": "本次 run 输出目录（reports/<case_id>/<run_id>）",
    "{steps_dir}": "prepare 中间产物目录（run 目录下的 steps/ 等）",
    "{dataset_id}": "case.dataset_expected.dataset_id",
    "{affix_db}": "case.affix_db（仓库相对）",
    "{build_id}": "case.build.build_id",
    "{revision_id}": "case.build.revision_id",
    "{share_id}": "BuildSpec share_id；新输入由 le_build import 的结果回填 case",
    "{source.character_save}": "case.source.character_save（仓库相对）",
    "{source.stash}": "case.source.stash（可选）",
    "{source.planner_build_response}": "case.source.planner_build_response（planner payload）",
    "{source.tool_native_wire}": "case.source.tool_native_wire（已归档的 tool-native wire）",
    "{wire_input}": "本次 BuildSpec 导入使用的 wire：归档 wire 或 harness 输出经归一后的文件",
    "{node}": "Node.js 运行时（config external_tools.node 或 LE_NODE 或仓库 tools/node/node.exe）",
    "{harness_script}": "tool-native harness 脚本（config external_tools.tool_native.harness_script）",
    "{tool_home}": "lastepochsupertools 安装目录（config external_tools.tool_native.tool_home）",
    "{model}": "case.model；缺省按 event 模型处理",
    "{main_skill}": "主技能 ability id（case.main_skill.ability_id 或 --main-skill）",
    "{snapshot_out}": "snapshot 产物路径",
    "{calc_input_out}": "calculation-input 产物路径",
}

_STAGE_ORDER: tuple[str, ...] = (
    "freeze_input",
    "parse_input",
    "normalize_build",
    "export_calc_input",
    "apply_affix_ranges",
    "resolve_skills",
    "prepare_snapshot",
    "min_validate",
)


def _py(*args: str) -> tuple[str, ...]:
    return ("{python}", *args)


def _script(path: str, *args: str) -> tuple[str, ...]:
    return ("{python}", path, *args)


@dataclass(frozen=True)
class CommandSpec:
    """一条既有脚本调用（参数模板，不执行）。"""

    stage_id: str
    tool: str
    argv: tuple[str, ...]
    pythonpath: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    optional_args: tuple[dict[str, Any], ...] = ()
    external: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "tool": self.tool,
            "argv": list(self.argv),
            "pythonpath": list(self.pythonpath),
            "outputs": list(self.outputs),
            "optional_args": [dict(item) for item in self.optional_args],
            "external": self.external,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class StageStep:
    """一个 stage 的说明与可选命令模板。"""

    stage_id: str
    label: str
    commands: tuple[CommandSpec, ...] = ()
    availability: str = "existing"  # existing | external_tool | adapter_required
    when: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "label": self.label,
            "availability": self.availability,
            "when": self.when,
            "commands": [command.to_dict() for command in self.commands],
            "notes": self.notes,
        }


@dataclass(frozen=True)
class RouteSpec:
    """一条输入路线的完整描述（纯数据）。"""

    route_id: str
    label: str
    input_kind: str  # character_save | planner_payload
    required_input_fields: tuple[str, ...]
    optional_input_fields: tuple[str, ...]
    requires_main_skill: bool
    dependency_ids: tuple[str, ...]
    stage_sequence: tuple[str, ...]
    steps: tuple[StageStep, ...]
    fallback_policy: str = FALLBACK_POLICY_NEVER
    notes: str = ""

    def stage_ids(self) -> tuple[str, ...]:
        return self.stage_sequence

    def step(self, stage_id: str) -> StageStep | None:
        for item in self.steps:
            if item.stage_id == stage_id:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "label": self.label,
            "input_kind": self.input_kind,
            "required_input_fields": list(self.required_input_fields),
            "optional_input_fields": list(self.optional_input_fields),
            "requires_main_skill": self.requires_main_skill,
            "dependency_ids": list(self.dependency_ids),
            "stage_sequence": list(self.stage_sequence),
            "steps": [step.to_dict() for step in self.steps],
            "fallback_policy": self.fallback_policy,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# 三条路线共享的命令模板
# ---------------------------------------------------------------------------

def _export_calc_input_step() -> StageStep:
    return StageStep(
        stage_id="export_calc_input",
        label="固定 BuildSpec revision → calculation-input-v1（le_build export）",
        availability="existing",
        commands=(
            CommandSpec(
                stage_id="export_calc_input",
                tool="le_build",
                argv=_py(
                    "-m", "le_build", "export",
                    "--build", "{build_id}",
                    "--revision", "{revision_id}",
                    "--out", "{steps_dir}/02-calculation-input.json",
                ),
                pythonpath=("tools/build_import/src",),
                outputs=("{steps_dir}/02-calculation-input.json",),
                notes=(
                    "revision 必须来自 case.build 的固定引用；新输入先由 le_build import 发布 revision，"
                    "再把 build_id/revision_id/content_sha256 回填 case。不得自动改用其它 revision。"
                ),
            ),
        ),
        notes="既有入口：tools/build_import/src/le_build/cli.py::cmd_export。",
    )


def _apply_affix_ranges_steps() -> StageStep:
    return StageStep(
        stage_id="apply_affix_ranges",
        label="affix-db 每基底范围重算 + 人工补值（既有 pipeline 脚本）",
        availability="existing",
        commands=(
            CommandSpec(
                stage_id="apply_affix_ranges",
                tool="pipeline_script:run_affix_db_pipeline",
                argv=_script(
                    "tools/save_import/scripts/pipeline/run_affix_db_pipeline.py",
                    "--case", "{case}",
                    "--calc-input", "{steps_dir}/02-calculation-input.json",
                    "--out", "{steps_dir}/03-calculation-input.affix-db.json",
                    "--report", "{steps_dir}/03-affix-db-recompute.json",
                ),
                pythonpath=("tools/phase3/src", "tools/save_import/src"),
                outputs=(
                    "{steps_dir}/03-calculation-input.affix-db.json",
                    "{steps_dir}/03-affix-db-recompute.json",
                ),
                notes="读取 case.affix_db 与 case.inputs.idol_refraction_selection；只重算既有词条，不新增来源。",
            ),
            CommandSpec(
                stage_id="apply_affix_ranges",
                tool="pipeline_script:apply_corrections",
                argv=_script(
                    "tools/save_import/scripts/pipeline/apply_corrections.py",
                    "--case", "{case}",
                    "--input", "{steps_dir}/03-calculation-input.affix-db.json",
                    "--out", "{steps_dir}/04-calculation-input.corrected.json",
                    "--report", "{steps_dir}/04-save-build-corrections.json",
                ),
                pythonpath=("tools/phase3/src", "tools/save_import/src"),
                outputs=(
                    "{steps_dir}/04-calculation-input.corrected.json",
                    "{steps_dir}/04-save-build-corrections.json",
                ),
                notes=(
                    "人工补值只来自当前案例的 case.inputs.corrections；不得复用旧案例补值。"
                    "本案例为空时也应生成同内容 corrected 文件。"
                ),
            ),
        ),
        notes="既有入口：tools/save_import/scripts/pipeline/{run_affix_db_pipeline,apply_corrections}.py。",
    )


def _resolve_skills_step() -> StageStep:
    return StageStep(
        stage_id="resolve_skills",
        label="主技能/节点/机制解析（P2 领域接口 resolve_build 的接入点）",
        availability="adapter_required",
        notes=(
            "prepare 此阶段尚无独立 CLI；由 P2 的 resolve_build / analysis-plan 适配器承担。"
            "主技能必须来自 case.main_skill.ability_id 或 prepare --main-skill 参数，"
            "不得按技能栏位置猜；缺少时返回 needs_input（见 dependencies.check_routes）。"
        ),
    )


def _prepare_snapshot_step() -> StageStep:
    return StageStep(
        stage_id="prepare_snapshot",
        label="生成对应模型的静态快照（hit / event 两种既有入口）",
        availability="existing",
        commands=(
            CommandSpec(
                stage_id="prepare_snapshot",
                tool="le_calc",
                argv=_py(
                    "-m", "le_calc", "hit-snapshot",
                    "--case", "{case}",
                    "--calc-input", "{steps_dir}/04-calculation-input.corrected.json",
                    "--out", "{steps_dir}/05-input-snapshot.hit.json",
                ),
                pythonpath=("tools/phase3/src",),
                outputs=("{steps_dir}/05-input-snapshot.hit.json",),
                notes="case.model == 'hit' 时使用（Flame Reave 案例）。",
            ),
            CommandSpec(
                stage_id="prepare_snapshot",
                tool="pipeline_script:make_snapshot",
                argv=_script(
                    "tools/save_import/scripts/pipeline/make_snapshot.py",
                    "--case", "{case}",
                    "--calc-input", "{steps_dir}/04-calculation-input.corrected.json",
                    "--out", "{steps_dir}/05-input-snapshot.json",
                    "--compare", "{inputs.snapshot}",
                ),
                pythonpath=("tools/phase3/src", "tools/save_import/src"),
                outputs=("{steps_dir}/05-input-snapshot.json",),
                notes=(
                    "event 模型（Black Hole 案例）使用；--compare 引用案例冻结快照。"
                    "case.model 缺省即按 event 处理。"
                ),
            ),
        ),
        notes="按 case.model 选择其一；不得把 hit 模型交给 event 快照生成器或反过来。",
    )


def _min_validate_step() -> StageStep:
    return StageStep(
        stage_id="min_validate",
        label="结构/绑定最小校验（le_commands validate）",
        availability="existing",
        commands=(
            CommandSpec(
                stage_id="min_validate",
                tool="le_commands",
                argv=_py("scripts/le_commands.py", "validate", "--case", "{case}"),
                pythonpath=(),
                outputs=(),
                notes=(
                    "既有入口 scripts/le_commands.py::cmd_validate；"
                    "不要求先填写完整 expected 才能得到初步结果，但绑定/结构必须一致。"
                ),
            ),
        ),
    )


def _freeze_input_step() -> StageStep:
    return StageStep(
        stage_id="freeze_input",
        label="冻结本次输入（指纹/快照；编排层职责）",
        availability="adapter_required",
        notes=(
            "尚无独立脚本；由 prepare/le_commands 在运行开始时冻结 case、原始输入与 BuildSpec revision 的 "
            "sha256 与不可变引用，后续阶段只读冻结输入。"
        ),
    )


# ---------------------------------------------------------------------------
# 路线 1：离线存档 → tool-native wire → BuildSpec
# ---------------------------------------------------------------------------

_SAVE_TOOL_NATIVE = RouteSpec(
    route_id=SAVE_ROUTE_ID,
    label="离线存档 → tool-native wire → BuildSpec revision → calculation-input",
    input_kind="character_save",
    required_input_fields=("source.character_save",),
    optional_input_fields=(
        "source.stash",
        "source.planner_build_response",
        "source.tool_native_wire",
        "source.tool_native_run_summary",
        "inputs.corrections",
        "inputs.idol_refraction_selection",
    ),
    requires_main_skill=True,
    dependency_ids=(
        "python.interpreter",
        "python.module.le_save_import",
        "python.module.le_build",
        "python.module.le_calc",
        "repo.project_config",
        "repo.dataset_pointer",
        "repo.affix_db",
        "repo.pipeline_scripts",
        "repo.le_commands",
        "input.case_config",
        "input.main_skill",
        "input.character_save",
        "input.stash",
        "input.tool_native_wire",
        "input.tool_native_run_summary",
        "external.tool_native.node",
        "external.tool_native.harness",
        "external.tool_native.install",
    ),
    stage_sequence=_STAGE_ORDER,
    steps=(
        _freeze_input_step(),
        StageStep(
            stage_id="parse_input",
            label="存档解析（le_save_import build；可选 planner/stash 合并）",
            availability="existing",
            commands=(
                CommandSpec(
                    stage_id="parse_input",
                    tool="le_save_import",
                    argv=_py(
                        "-m", "le_save_import", "build",
                        "--save", "{source.character_save}",
                        "--out", "{steps_dir}/01-save-import",
                        "--affix-db", "{affix_db}",
                    ),
                    pythonpath=("tools/save_import/src",),
                    outputs=("{steps_dir}/01-save-import/bd.json",),
                    optional_args=(
                        {
                            "when": "case.source.planner_build_response 非空",
                            "args": ["--planner", "{source.planner_build_response}"],
                        },
                        {
                            "when": "case.source.stash 非空",
                            "args": ["--stash", "{source.stash}"],
                        },
                    ),
                    notes=(
                        "既有入口 tools/save_import/src/le_save_import；本路线以存档为主来源，"
                        "planner 只作为可选补充（与 planner 路线语义不同）。"
                    ),
                ),
            ),
            notes="产出 bd.json（BD 文档），用于后续解析/来源覆盖；BuildSpec 仍以 wire 为准。",
        ),
        StageStep(
            stage_id="normalize_build",
            label="tool-native wire 生成/归一 + 发布 BuildSpec revision（le_build import）",
            availability="external_tool",
            commands=(
                CommandSpec(
                    stage_id="normalize_build",
                    tool="tool_native_harness",
                    argv=(
                        "{node}", "{harness_script}",
                        "--save", "{source.character_save}",
                        "--wire-out", "{steps_dir}/01b-tool-native/wire-from-save.json",
                        "--decoded-out", "{steps_dir}/01b-tool-native/decoded-items-tool-native.json",
                    ),
                    pythonpath=(),
                    outputs=(
                        "{steps_dir}/01b-tool-native/wire-from-save.json",
                        "{steps_dir}/01b-tool-native/decoded-items-tool-native.json",
                    ),
                    external=True,
                    notes=(
                        "仅当 case.source.tool_native_wire 缺失时执行（新存档输入）。"
                        "harness 用 Node.js vm 沙箱离线加载外部 lastepochsupertools 工具的 planner.js/db js，"
                        "调用 pvmBuildWireFromSave；该工具不随公开版分发，需接收方自行配置 "
                        "config/project.json 的 external_tools（见 docs/contracts/save-onboarding.md）。"
                        "argv 是 formality 契约草案：具体参数以配置的 harness 脚本为准，"
                        "所有路径来自 config/project.json 的 external_tools，不写个人绝对路径。"
                    ),
                ),
                CommandSpec(
                    stage_id="normalize_build",
                    tool="le_build",
                    argv=_py(
                        "-m", "le_build", "import",
                        "--file", "{wire_input}",
                        "--share-id", "{share_id}",
                        "--offline",
                        "--dataset", "{dataset_id}",
                        "--root", "{root}",
                    ),
                    pythonpath=("tools/build_import/src",),
                    outputs=("data/builds/letools/{share_id}/current.json",),
                    notes=(
                        "wire_input 为归档 wire（case.source.tool_native_wire）或 harness 输出经 "
                        "blessings[].ir 归一后的文件（标量 0 → 存档字节 [9:12] 的 3 字节数组，"
                        "见 case.build.import_source 与 docs/contracts/save-onboarding.md）；"
                        "归一脚本是 P3 需正式化的必要步骤，不得把未归一的 wire 直接当 planner payload。"
                    ),
                ),
            ),
            notes=(
                "tool-native wire 与 planner payload 的语义不同：wire 是存档读出的本地数据，"
                "不经过 planner 网络响应；缺少 harness 时必须 needs_input，不得改为 planner 路线。"
            ),
        ),
        _export_calc_input_step(),
        _apply_affix_ranges_steps(),
        _resolve_skills_step(),
        _prepare_snapshot_step(),
        _min_validate_step(),
    ),
    notes=(
        "适用输入：离线存档 1CHARACTERSLOT_BETA_*；可选 STASH_* 与 planner payload 只用于补充 BD 文档。"
        "当 case.source.tool_native_wire 已归档时不需要 harness 运行时。"
    ),
)


# ---------------------------------------------------------------------------
# 路线 2：离线存档 + planner payload（混合来源）
# ---------------------------------------------------------------------------

_SAVE_PLANNER = RouteSpec(
    route_id=SAVE_PLANNER_ROUTE_ID,
    label="离线存档 + planner payload（混合来源）→ BuildSpec revision → calculation-input",
    input_kind="character_save+planner_payload",
    required_input_fields=("source.character_save", "source.planner_build_response"),
    optional_input_fields=(
        "source.stash",
        "source.tool_native_wire",
        "source.tool_native_run_summary",
        "inputs.corrections",
        "inputs.idol_refraction_selection",
    ),
    requires_main_skill=True,
    dependency_ids=(
        "python.interpreter",
        "python.module.le_save_import",
        "python.module.le_build",
        "python.module.le_calc",
        "repo.project_config",
        "repo.dataset_pointer",
        "repo.affix_db",
        "repo.pipeline_scripts",
        "repo.le_commands",
        "input.case_config",
        "input.main_skill",
        "input.character_save",
        "input.stash",
        "input.planner_payload",
        "input.tool_native_wire",
        "input.tool_native_run_summary",
        "external.tool_native.node",
        "external.tool_native.harness",
        "external.tool_native.install",
    ),
    stage_sequence=_STAGE_ORDER,
    steps=(
        _freeze_input_step(),
        StageStep(
            stage_id="parse_input",
            label="存档解析（le_save_import build --save --planner；可带 stash）",
            availability="existing",
            commands=(
                CommandSpec(
                    stage_id="parse_input",
                    tool="le_save_import",
                    argv=_py(
                        "-m", "le_save_import", "build",
                        "--save", "{source.character_save}",
                        "--out", "{steps_dir}/01-save-import",
                        "--planner", "{source.planner_build_response}",
                        "--affix-db", "{affix_db}",
                    ),
                    pythonpath=("tools/save_import/src",),
                    outputs=("{steps_dir}/01-save-import/bd.json",),
                    optional_args=(
                        {
                            "when": "case.source.stash 非空",
                            "args": ["--stash", "{source.stash}"],
                        },
                    ),
                    notes=(
                        "混合来源：存档 + planner 一起解析出 bd.json（与 BH reproduce 的 1/7 步骤一致）；"
                        "planner payload 是必需参数，不是可选补充。"
                    ),
                ),
            ),
            notes="产出 bd.json（BD 文档/来源覆盖）；BuildSpec 仍以 planner payload 为准。",
        ),
        StageStep(
            stage_id="normalize_build",
            label="planner payload → 发布 BuildSpec revision（le_build import --file --offline）",
            availability="existing",
            commands=(
                CommandSpec(
                    stage_id="normalize_build",
                    tool="le_build",
                    argv=_py(
                        "-m", "le_build", "import",
                        "--file", "{source.planner_build_response}",
                        "--share-id", "{share_id}",
                        "--offline",
                        "--dataset", "{dataset_id}",
                        "--root", "{root}",
                    ),
                    pythonpath=("tools/build_import/src",),
                    outputs=("data/builds/letools/{share_id}/current.json",),
                    notes=(
                        "混合来源路线的 BuildSpec 来自 planner payload（与 BH reproduce 的 pinned revision 一致）；"
                        "存档不参与 BuildSpec 编码，只参与 parse_input 的 BD 文档。"
                    ),
                ),
            ),
        ),
        _export_calc_input_step(),
        _apply_affix_ranges_steps(),
        _resolve_skills_step(),
        _prepare_snapshot_step(),
        _min_validate_step(),
    ),
    notes=(
        "适用输入：同一 case 同时有离线存档与归档 planner payload（BH 案例）；"
        "存档用于 parse_input（bd.json/来源覆盖），planner payload 用于 BuildSpec；"
        "两者缺一不可，不得降级成单来源路线。已归档 tool-native wire/run-summary 只作为可选证据/覆盖。"
    ),
)


# ---------------------------------------------------------------------------
# 路线 3：planner payload → BuildSpec
# ---------------------------------------------------------------------------

_PLANNER_PAYLOAD = RouteSpec(
    route_id=PLANNER_ROUTE_ID,
    label="LE Tools planner payload → BuildSpec revision → calculation-input",
    input_kind="planner_payload",
    required_input_fields=("source.planner_build_response",),
    optional_input_fields=(
        "source.character_save",
        "source.stash",
        "inputs.corrections",
        "inputs.idol_refraction_selection",
    ),
    requires_main_skill=True,
    dependency_ids=(
        "python.interpreter",
        "python.module.le_save_import",
        "python.module.le_build",
        "python.module.le_calc",
        "repo.project_config",
        "repo.dataset_pointer",
        "repo.affix_db",
        "repo.pipeline_scripts",
        "repo.le_commands",
        "input.case_config",
        "input.main_skill",
        "input.character_save",
        "input.stash",
        "input.planner_payload",
    ),
    stage_sequence=(
        "freeze_input",
        "normalize_build",
        "export_calc_input",
        "apply_affix_ranges",
        "resolve_skills",
        "prepare_snapshot",
        "min_validate",
    ),
    steps=(
        _freeze_input_step(),
        StageStep(
            stage_id="parse_input",
            label="可选：存档解析（仅当另提供 character_save，用于 bd.json 文档）",
            availability="existing",
            when="case.source.character_save 非空",
            commands=(
                CommandSpec(
                    stage_id="parse_input",
                    tool="le_save_import",
                    argv=_py(
                        "-m", "le_save_import", "build",
                        "--save", "{source.character_save}",
                        "--out", "{steps_dir}/01-save-import",
                        "--planner", "{source.planner_build_response}",
                        "--affix-db", "{affix_db}",
                    ),
                    pythonpath=("tools/save_import/src",),
                    outputs=("{steps_dir}/01-save-import/bd.json",),
                    optional_args=(
                        {
                            "when": "case.source.stash 非空",
                            "args": ["--stash", "{source.stash}"],
                        },
                    ),
                    notes="只做 BD 文档/来源覆盖；BuildSpec 仍以 planner payload 为准。",
                ),
            ),
            notes="可选阶段：只做 BuildSpec → calculation-input 时可跳过；不改变路线语义。",
        ),
        StageStep(
            stage_id="normalize_build",
            label="planner payload → 发布 BuildSpec revision（le_build import --file --offline）",
            availability="existing",
            commands=(
                CommandSpec(
                    stage_id="normalize_build",
                    tool="le_build",
                    argv=_py(
                        "-m", "le_build", "import",
                        "--file", "{source.planner_build_response}",
                        "--share-id", "{share_id}",
                        "--offline",
                        "--dataset", "{dataset_id}",
                        "--root", "{root}",
                    ),
                    pythonpath=("tools/build_import/src",),
                    outputs=("data/builds/letools/{share_id}/current.json",),
                    notes=(
                        "planner payload 必须是归档的 LE Tools 构建响应；--offline 禁止网络。"
                        "与 save 路线不同：不经过 tool-native harness，也不做 wire→planner 归一。"
                    ),
                ),
            ),
        ),
        _export_calc_input_step(),
        _apply_affix_ranges_steps(),
        _resolve_skills_step(),
        _prepare_snapshot_step(),
        _min_validate_step(),
    ),
    notes=(
        "适用输入：LE Tools planner payload（Black Hole 案例的 source.planner_build_response）。"
        "character_save/stash 只作为可选 BD 文档补充；planner payload 缺失时不得 fallback 到存档路线。"
    ),
)


ROUTES: tuple[RouteSpec, ...] = (_SAVE_TOOL_NATIVE, _SAVE_PLANNER, _PLANNER_PAYLOAD)
_ROUTES_BY_ID: dict[str, RouteSpec] = {route.route_id: route for route in ROUTES}


# ---------------------------------------------------------------------------
# 路线判定
# ---------------------------------------------------------------------------

def _as_case_dict(case: Any) -> dict[str, Any] | None:
    if isinstance(case, dict):
        return case
    if isinstance(case, (str, bytes)) or hasattr(case, "__fspath__"):
        try:
            import json
            from pathlib import Path

            path = Path(case)
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
    return None


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _source_field(case: dict[str, Any], key: str) -> Any:
    source = case.get("source")
    if not isinstance(source, dict):
        return None
    return source.get(key)


_SOURCE_KIND_ALIASES = {
    "save": SAVE_ROUTE_ID,
    "character_save": SAVE_ROUTE_ID,
    "save-tool-native-wire": SAVE_ROUTE_ID,
    "save+planner": SAVE_PLANNER_ROUTE_ID,
    "save_planner": SAVE_PLANNER_ROUTE_ID,
    "save-planner": SAVE_PLANNER_ROUTE_ID,
    "save_and_planner": SAVE_PLANNER_ROUTE_ID,
    "character_save+planner_payload": SAVE_PLANNER_ROUTE_ID,
    "mixed": SAVE_PLANNER_ROUTE_ID,
    "planner": PLANNER_ROUTE_ID,
    "planner_payload": PLANNER_ROUTE_ID,
    "planner-payload": PLANNER_ROUTE_ID,
}


def route_from_source_kind(source_kind: str | None) -> RouteSpec | None:
    """把调用方显式传入的 source kind 映射到路线；未知值返回 None。"""

    if not source_kind:
        return None
    return _ROUTES_BY_ID.get(_SOURCE_KIND_ALIASES.get(str(source_kind).strip(), ""))


def resolve_route_detail(
    case: Any,
    *,
    source_kind: str | None = None,
) -> dict[str, Any]:
    """路线判定 + 原因（供 ``check_routes`` 生成可操作 needs_input）。

    判定规则（显式字段，不猜）：

    1. 显式 ``source_kind`` 参数优先；
    2. ``source.tool_native_wire`` 与 ``source.planner_build_response`` **同时**存在 →
       歧义，返回 None（要求调用方显式选路线，不按优先级猜）；
    3. ``source.character_save`` 与 ``source.planner_build_response`` 同时存在 →
       组合路线 ``save+planner``（BH 案例：save 解析 bd.json + planner 提供 BuildSpec）；
    4. 存在 ``source.tool_native_wire`` → save 路线（已归档 wire，不需要 harness）；
    5. 存在 ``source.planner_build_response``（无存档）→ planner 路线；
    6. 只有 ``source.character_save`` → save 路线（新存档输入，harness 必需）；
    7. 其余 → None（不能判定）。
    """

    route = route_from_source_kind(source_kind)
    if route is not None:
        return {"route": route, "reason": "explicit_source_kind", "candidates": [route.route_id]}

    case_dict = _as_case_dict(case)
    if case_dict is None:
        return {
            "route": None,
            "reason": "case_unreadable_or_not_object",
            "candidates": list(ROUTE_IDS),
        }

    has_wire = _nonempty(_source_field(case_dict, "tool_native_wire"))
    has_planner = _nonempty(_source_field(case_dict, "planner_build_response"))
    has_save = _nonempty(_source_field(case_dict, "character_save"))

    if has_wire and has_planner:
        return {
            "route": None,
            "reason": "ambiguous_archived_sources",
            "candidates": [SAVE_ROUTE_ID, PLANNER_ROUTE_ID],
        }
    if has_save and has_planner:
        return {
            "route": _ROUTES_BY_ID[SAVE_PLANNER_ROUTE_ID],
            "reason": "source.character_save + source.planner_build_response（混合来源）",
            "candidates": [SAVE_PLANNER_ROUTE_ID],
        }
    if has_wire:
        return {
            "route": _ROUTES_BY_ID[SAVE_ROUTE_ID],
            "reason": "source.tool_native_wire",
            "candidates": [SAVE_ROUTE_ID],
        }
    if has_planner:
        return {
            "route": _ROUTES_BY_ID[PLANNER_ROUTE_ID],
            "reason": "source.planner_build_response（无存档）",
            "candidates": [PLANNER_ROUTE_ID],
        }
    if has_save:
        return {
            "route": _ROUTES_BY_ID[SAVE_ROUTE_ID],
            "reason": "source.character_save（新输入；需要 tool-native harness 生成 wire）",
            "candidates": [SAVE_ROUTE_ID],
        }
    return {
        "route": None,
        "reason": "no_supported_source_fields",
        "candidates": list(ROUTE_IDS),
    }


def resolve_route(case: Any, *, source_kind: str | None = None) -> RouteSpec | None:
    """按 case.json 的 ``source`` 字段判定输入路线；不能判定返回 ``None``。

    支持传入 case 字典或 case.json 路径（路径相对当前工作目录）。本函数只读
    JSON，不猜测、不访问网络、不写文件。
    """

    return resolve_route_detail(case, source_kind=source_kind)["route"]


def get_route(route_id: str) -> RouteSpec | None:
    return _ROUTES_BY_ID.get(str(route_id))


def list_routes() -> list[dict[str, Any]]:
    """返回三条路线的 JSON 描述（供 doctor / prepare / 文档消费）。"""

    return [route.to_dict() for route in ROUTES]


def route_to_dict(route: RouteSpec | None) -> dict[str, Any] | None:
    return None if route is None else route.to_dict()


def route_brief(route: RouteSpec | None) -> dict[str, Any] | None:
    """短摘要（放进 check_routes 结果，避免每次返回完整 argv 列表）。"""

    if route is None:
        return None
    return {
        "route_id": route.route_id,
        "label": route.label,
        "input_kind": route.input_kind,
        "requires_main_skill": route.requires_main_skill,
        "required_input_fields": list(route.required_input_fields),
        "stage_sequence": list(route.stage_sequence),
        "fallback_policy": route.fallback_policy,
    }


__all__ = [
    "CommandSpec",
    "FALLBACK_POLICY_NEVER",
    "PLACEHOLDER_TOKENS",
    "PLANNER_ROUTE_ID",
    "ROUTE_IDS",
    "SAVE_PLANNER_ROUTE_ID",
    "ROUTES",
    "RouteSpec",
    "SAVE_ROUTE_ID",
    "StageStep",
    "get_route",
    "list_routes",
    "resolve_route",
    "resolve_route_detail",
    "route_brief",
    "route_from_source_kind",
    "route_to_dict",
]
