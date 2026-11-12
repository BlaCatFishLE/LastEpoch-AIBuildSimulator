# affix-db：本地规范化词缀 × 装备位置 roll 范围数据库（使用说明）

本目录说明如何使用 `data/affix-db/v1/` 与 `tools/affix_db/`。构建/校验实现与版本记录在
`tools/affix_db/` 与 `data/affix-db/v1/manifest.json`；数据目录内还有一份面向数据消费者的
[`data/affix-db/v1/README.md`](../data/affix-db/v1/README.md)。作者本地的构建报告不随公开仓库分发。

> 再分发边界：`data/affix-db/v1/source-snapshot/`（Tunklab 原始站点快照）与 `data/reference/` **不随仓库分发**；
> 随仓库分发的是规范化结果 `data/affix-db/v1/normalized/affix-db.sqlite` 及其索引 JSON。

## 这是什么

- 来源：<https://lastepoch.tunklab.com> 的静态客户端数据（**数据自报版本 1.5.2**，非本地游戏 1.5.3 原生数据）；
- 解决的问题：同一词缀在不同装备位置/基底上的有效 roll 范围不同。旧数据只有全局 tier（如 Fire Resistance T7 = 61–75%），
  胸甲（Body Armor，`affixEffectModifier=0.5`）实际是 **91.5–112.5%**（游戏显示 **92–112%**）。
- canonical 数据库：`data/affix-db/v1/normalized/affix-db.sqlite`（21,533 条 affix × base × tier × slot 范围行）；
  小体积 JSON 投影在 `normalized/index/`。

## 公式

```
modifier   = (1 + base.affixEffectModifier) / (1 + affix.standardAffixEffectModifier)
effective  = raw roll × modifier
```

## 显示取整（两套，必须显式选）

| 列 | 规则 | 用途 |
| --- | --- | --- |
| `display_min/max` | Unity/C# `Math.Round`：四舍六入五成双（half-even） | 游戏内 tooltip / 构筑模拟（默认） |
| `tunklab_display_min/max` | JS `Math.round`：四舍五入、.5 向上（half-up） | 与 tunklab 网页逐格对照 |

端点恰为 `.5` 时会差 1：Fire Resistance × Body Armor × T7 精确 91.5–112.5 → 游戏 **92–112**，
tunklab 页面 **92–113**。精确十进制值在 `effective_min_decimal/max_decimal`，需要计算时优先用它。

## 常用查询

```bash
# 单点查询（默认游戏侧显示）
python tools/affix_db/run.py query --affix fire_resistance --base "Body Armor" --tier 7
# 与 tunklab 页面取整一致
python tools/affix_db/run.py query --affix 13 --base 1 --tier 7 --rounding tunklab
# 某 affix 所有可 roll 位置
python tools/affix_db/run.py query --affix fire_resistance --all-bases
# JSON（便于程序消费）
python tools/affix_db/run.py query --affix 13 --base 1 --tier 7 --json
# 神像专项
python tools/affix_db/run.py query --affix 1070 --base "Adorned Idol" --tier 1     # 4%
python tools/affix_db/run.py query --affix 111 --omen-sub-base grand_arcane_omen_idol  # derived 20-45%（[未解决]）
```

关键 schema：

- `affixes` / `affix_tiers`：词缀主数据与原始 tier（原始字段保留在 `raw_json`）；
- `equipment_types` / `equipment_sub_bases`：装备基底与具体基底（含 `affix_effectiveness`）；
- `affix_base_ranges`：核心范围表（`raw_*`、`modifier`、`effective_*`、两套 display、`formula`、`source_locator`、`source_version`）；
- `affix_eligible_bases` / `unresolved_can_roll_on`：资格展开与无法解析的伪基底 id；
- `omen_special_ranges`：Omen 神像**衍生**路径，`derived=1` + `eligibility_status=unresolved` + `evidence`。

## 构建 / 校验 / 测试（全部离线）

```bash
python tools/affix_db/run.py build        # 从 frozen snapshot 重建（幂等、无网络）
python tools/affix_db/run.py validate     # 44 项校验（schema/公式/验收值/神像/manifest sha256）
python -m unittest discover -s tools/affix_db/tests -v    # 79 tests
```

仅两个可选联网步骤：`fetch`（重抓 tunklab 快照）与 `render-pages`（headless Chrome 渲染页面证据）；
`verify-display` / `crosscheck-game` 在已有证据上离线运行。公开仓库不含 `source-snapshot/`，因此 `build` 需要使用者
自行提供冻结快照；日常使用直接读取随仓库分发的 `normalized/affix-db.sqlite` 即可。

## 两个神像结论（详见构建报告 §8）

1. **Throne of Ambition（Adorned Idol, baseTypeID 33）All Resistances = 4%**：
   affix 1070 raw 0.008 × (1−0.05)/(1−0.83) = 4.4706% → 4%。旧导入的 1% 是没乘基底倍率
   （raw 0.008 直接取整为 1%）。折射栏位前缀效果 ×1.28 后的 5.12% 属于另一层（栏位加成），不是 affix 本身。
2. **Omen 神像（Grand Arcane Omen Idol, baseTypeID 29 / subTypeID 11）Fire Resistance = raw 20–45%**：
   tunklab 数据含 `affixEffectiveness=1` 与 `omenIdolAffixEffectModifier=0`，但其 `canRollOn` 不含 Grand Idol、
   客户端代码也未实现该路径 → **tunklab 数据无法闭合，标记 [未解决]**。本库在 `omen_special_ranges` 中保存
   明确标注 derived 的 20–45% 行（附外部证据链），**不要**当作 tunklab/游戏事实使用。

## 限制

- 版本边界：tunklab 1.5.2 vs 本地游戏 1.5.3；只对 5 个可本地抽取的 affix 做了 raw tier 交叉检查；
- 显示取整必须显式选择 policy（全库 913 条行两种取整不同）；
- 折射栏位 ×1.14/×1.28 等栏位效果不在本库范围（属于存档导入/构筑计算层）；
- 伪基底 id 11/24 无对应 item base，已记录为 unresolved；
- 数据来源为第三方，Omen 资格等未闭合项已显式标记，不猜测。
