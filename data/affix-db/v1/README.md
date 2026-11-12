# affix-db v1 -- 本地规范化词缀 / 装备基底 roll 范围数据库

从 tunklab 站点静态客户端数据 chunk 中提取、规范化得到的词缀（affix）与装备基底
（equipment type）roll 范围数据库。**解析过程不执行任何下载的 JavaScript**，构建与
查询均离线运行。

- 数据来源：<https://lastepoch.tunklab.com>（`/affix/<slug>` 页面及其 Turbopack chunks）
- tunklab 数据版本：`1.5.2`（站点数据 chunk 内自报版本）
- schema 版本：1；构建器版本：1.0.0
- 本仓库游戏版本：1.5.3（tunklab 数据为 1.5.2；版本边界见 `reports/affix-db/affix-db-build-report.zh-CN.md`）

## 为什么需要这个库

旧 `phase1-dataset-003` 只记录“全局 T7”词缀范围（火抗 61-75%），而游戏会按装备位置
对词缀数值做缩放的机制（`affixEffectModifier`）。同一词缀在不同基底上的有效范围不同：

```
effective = raw * (1 + base.affixEffectModifier) / (1 + affix.standardAffixEffectModifier)
```

例：Fire Resistance（affixId 13）T7 原始 61-75%，Body Armor 的 `affixEffectModifier`
为 0.5，故有效范围是 91.5-112.5%；Helmet 上仍是 61-75%。

## 文件布局

```
data/affix-db/v1/
  README.md                       本文件
  manifest.json                   所有构建产物的 sha256、计数、公式、来源定位
  source-snapshot/
    inventory.json                抓取清单（URL / sha256 / ETag / 抓取时间）
    chunks/*.js                   冻结的客户端 chunks（数据源，静态解析）
    pages/*.html                  affix 页面 SSR 快照（来源证据）
  normalized/
    affix-db.sqlite               canonical：全量范围表（21,533 行）
    source-extraction.json        每个数据 literal 的精确位置（chunk sha256 + 字符/字节偏移）
    index/affixes.json            affix 主表 JSON 投影（1,156 条）
    index/equipment-types.json    装备基底 JSON 投影（47 条）
    index/affix-eligible-bases.json  affix x canRollOn 基底展开（2,539 条）
    index/affix-tiers.json        affix 原始 tier 数值（1,156 条）
    index/equipment-sub-bases.json  具体基底（含 affixEffectiveness 标记）
    index/equipment-globals.json  装备数据全局常量（含 omenIdolAffixEffectModifier）
    index/omen-special-ranges.json  Omen 神像特殊路径（标 derived/[未解决]，见下）
    index/stat-properties.json    属性显示元数据（决定取整/单位）
    index/summary.json            计数、公式、显示策略摘要
  verification/                   （可选）页面渲染证据与对照结果
    rendered/*.dom.html           浏览器 hydration 后的页面 DOM
    rendered/*.png                关键页面截图（fire_resistance）
    display-comparison.json/.md   页面显示值 vs DB 逐格对照
    game-crosscheck.json          tunklab 1.5.2 raw tier vs 本地游戏 1.5.3 交叉检查
    manifest.json                 verification 产物 sha256
```

**SQLite 为 canonical 数据源**；JSON 只是便于 review 的小型投影（不导出全量范围表）。

## 数据库 schema 摘要

- `affixes`：affix 主表（`affix_id`、`slug`、`name`、`title`、`type`、`special_affix_type`、
  `stat_property`、`tags`、`standard_affix_effect_modifier`、`can_roll_on_json`、
  `specific_reroll_chances_json`、`tiers_json`、`raw_json` 等；原始字段全部保留在 `raw_json`）。
- `equipment_types` / `equipment_sub_bases`：装备基底类型（baseTypeID 0..41）与其子基底
  （1570 个具体基底），含 `affix_effect_modifier`、grid、subItems、implicits 等。
- `affix_eligible_bases`：每个 affix 的每个 `canRollOn` 引用（含无法解析的伪基底 id
  与 tunklab 显示排除列表），便于审计“无资格 base 被排除”。
- `affix_base_ranges`：**affix x baseType x tier x roll_slot** 的有效范围，
  含 `raw_min/raw_max`、`modifier`、`effective_min/effective_max`（浮点与实际意图的十进制字符串）、
  `display_min/display_max`（游戏/Unity 取整：四舍六入五成双）、`tunklab_display_min/max`
  （tunklab 页面 JS `Math.round`：四舍五入）、`source_locator`（chunk sha256 + 字节偏移）。
- `affix_tiers`、`stat_properties`、`player_stat_properties`、`ability_properties`、
  `unresolved_can_roll_on`、`source_datasets`、`meta`；
- `equipment_globals`：装备数据全局常量（含 `omenIdolAffixEffectModifier=0`）；
- `omen_special_ranges`：**衍生/证据型** Omen 神像特殊路径（见下节；非 tunklab 直接给出）。

## 折射神像与 Omen 神像两条实测输入（本库如何解释）

**Throne of Ambition（Adorned Silver Idol，baseTypeID 33）——腐化词缀 +4% All Resistances。**
affixId 1070 `idol_all_resistances_for_you_and_your_minions` 原始 roll 0.008；Adorned Idol 的
`affixEffectModifier=-0.05`，词缀 `standardAffixEffectModifier=-0.83`，倍率
`(1-0.05)/(1-0.83)=95/17≈5.5882`，有效值 4.4706% → 游戏/Unity 取整 **4%**（tunklab 页面同为 4%）。
旧导入误读成 1%，是因为只用原始 roll 0.008（0.8% → 取整 1%）而没有乘基底倍率。详见
`reports/affix-db/affix-db-build-report.zh-CN.md` 的“折射/大神像”章节。

**Grand Arcane Omen Idol（baseTypeID 29 / subTypeID 11）——Omen 特殊路径。**
tunklab 装备数据带有 `subItem.affixEffectiveness=1`、`onlyDropWhenRequiringAffixEffectiveness=true`
以及全局常量 `omenIdolAffixEffectModifier=0`；但 tunklab 客户端代码未实现该路径，且
affix 111（Idol Fire Resistance）的 `canRollOn` 不含 Grand Idol（页面 “Applies To” 也无
Grand Idol）。因此本库：
- 普通 `affix_base_ranges` **不生成** affix 111 x Grand Idol 行；
- 在 `omen_special_ranges` 中保存**明确标注 derived** 的衍生行：
  `modifier=(1+omenIdolAffixEffectModifier)/(1+0)=1.0`，affix 111 有效范围 = raw **20-45%**；
- 每行 `eligibility_status` 标注 `unresolved: tunklab canRollOn does not include baseTypeID 29`，
  `evidence` 记录 tunklab 字段 + 外部证据（lastepochtools planner 语义
  `affixEffectiveness==1 → baseMod:=0`；项目 save-import 中该具体物品确实带 affix 111、
  raw range 0.2-0.45、roll 38%/34%）。**tunklab 数据本身无法闭合该路径，故标 [未解决]，未猜测。**

## CLI（全部离线）

包位于 `tools/affix_db/`，从仓库根目录用 `tools/affix_db/run.py` 启动即可（无需安装）：

```bash
# 从已冻结快照重建（幂等；不会联网）
python tools/affix_db/run.py build --snapshot data/affix-db/v1/source-snapshot --out data/affix-db/v1

# 查询：Fire Resistance（affixId 13）在 Body Armor（baseTypeID 1）上的 T7
python tools/affix_db/run.py query --affix fire_resistance --base "Body Armor" --tier 7
# -> display 92-112%（游戏侧取整）；effective 0.915..1.125

# 同一 affix 在所有装备位置的完整范围
python tools/affix_db/run.py query --affix fire_resistance --all-bases

# 使用 tunklab 页面取整对照（该词缀 Body Armor T7 页面显示 92-113%）
python tools/affix_db/run.py query --affix 13 --base 1 --tier 7 --rounding tunklab

# 折射/神像：Throne of Ambition 的腐化全抗（affix 1070 x Adorned Idol）→ 4%
python tools/affix_db/run.py query --affix 1070 --base "Adorned Idol" --tier 1

# Omen 神像特殊路径（derived / [未解决]）：Grand Arcane Omen Idol x affix 111 → 20-45%
python tools/affix_db/run.py query --affix 111 --omen-sub-base grand_arcane_omen_idol

# 结构化输出
python tools/affix_db/run.py query --affix 13 --base 1 --tier 7 --json

# 校验（schema/计数/公式/已知值/显示策略/manifest sha256）
python tools/affix_db/run.py validate

# 一次性抓取（唯一联网步骤；生成 source-snapshot/）
python tools/affix_db/run.py fetch --out data/affix-db/v1/source-snapshot

# 渲染 tunklab 页面并比对显示值（联网 + 需要本机 Chrome；生成 verification/）
python tools/affix_db/run.py render-pages --out data/affix-db/v1/verification
python tools/affix_db/run.py verify-display

# 与本地游戏 1.5.3 已抽取数据的原始 tier 交叉检查
python tools/affix_db/run.py crosscheck-game

# 测试
python -m unittest discover -s tools/affix_db/tests -v
```

等价写法：`cd tools && python -m affix_db ...`，或把 `tools/` 加入 `PYTHONPATH` 后
`python -m affix_db ...`。也提供 `python -m affix_db` 模块入口。

## 取整策略（重要）

tunklab 页面与游戏工具链的取整规则不同，**端点正好是 `.5` 时结果会差 1**：

| 场景 | 规则 | Fire Resistance / Body Armor / T7 |
| --- | --- | --- |
| tunklab 页面（JS `Math.round`） | 四舍五入（.5 向上） | 92% - 113% |
| 游戏（Unity/C# `Mathf.RoundToInt`/`Math.Round`） | 四舍六入五成双 | 92% - 112% |
| 精确有效范围（未取整） | `raw * modifier` | 91.5% - 112.5% |

数据库同时保存两套 display 列与精确 effective 值；查询默认输出游戏侧（`--rounding game`），
`--rounding tunklab` 用于与 tunklab 页面逐格对照。

## 已知限制

1. tunklab 数据版本为 `1.5.2`，本仓库本地游戏为 `1.5.3`：本库**不是**游戏 1.5.3 原生数据，
   版本差异以 tunklab 数据为准；如需以 1.5.3 游戏数据交叉核对，见报告中的交叉检查章节。
2. tunklab 的 `canRollOn` 引用两个不存在于其 item base 列表的伪基底 id（11、24，
   游戏枚举中的 Crossbow / One-Handed Fist）；tunklab 自己也在页面显示时排除它们，
   本库将其记录在 `unresolved_can_roll_on` 且不生成范围行。
3. 显示元数据（取整/单位）由 tunklab 的 property 定义推导；少数特殊属性（PlayerProperty
   98 / EffectOfAilmentOnYou 120 / IdolAltarProperty 130）按 tunklab 客户端代码中的
   特殊分支处理，详见报告“显示语义”章节。
4. 本库只覆盖 tunklab affix 页面的词缀/基底数据；不含 unique/set 物品、技能树等其它数据。
5. 构建产物可复现：同一 `source-snapshot/` 下重复 `build` 输出逐字节一致（`validate --check-idempotent`）。
6. Omen 神像特殊路径在 tunklab 数据中 **无法闭合**（`canRollOn` 不含 Grand Idol，客户端代码未使用
   `affixEffectiveness`），`omen_special_ranges` 仅为标注 derived 的衍生表，`eligibility_status`
   保留 [未解决]；引用该表时必须同时引用其 `evidence` 字段。
7. 折射栏位的 ×1.14/×1.28 加成属于 Idol Altar / 折射栏位机制（存档导入层），不是 affix 本身范围；
   本库只给 affix 基底范围，不包含该栏位加成。
