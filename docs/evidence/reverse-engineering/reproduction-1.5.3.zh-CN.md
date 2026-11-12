# 1.5.3 只读逆向复现说明（命令 + 哈希 + 定位）

本文件汇总 phase-3 逆向的证据复现方式。所有操作均为**只读**：没有修改任何游戏文件；下载的第三方工具只放在 `<author-archive>/rev/`（不随仓库交付）。

## 1. 环境与文件指纹

| 项 | 值 |
|---|---|
| 游戏安装 | `<game-install>` |
| 游戏版本 / Steam buildid | 1.5.3 / `26087601` |
| Unity | `6000.4.8f1` |
| `GameAssembly.dll` | 97,256,960 B，sha256 `323e9183f3bee600b6c9e1c469218b9de926e9044f2ca3d44fae8a0ec9a91464` |
| `global-metadata.dat` | 36,457,932 B，sha256 `2a4e8440606c420345934158b6784d8f5d8b0184f32fd91af0e9e0903ba94f83` |
| metadata 版本 / 类型 / 方法指针 | v39 / 40,789 type defs / 272,489 method pointers |

工具（下载到 `<author-archive>/rev/`，未随仓库交付）：

| 工具 | 结果 | sha256 |
|---|---|---|
| Il2CppDumper c01ns v108.0 | 成功导出 dump.cs/script.json/il2cpp.h/DummyDll（metadata v39） | zip `4c47399ab14ca7260418bd272b9b94af36dbaf4fee34a60bdf05bbd4fe6e7364` |
| Cpp2IL 2022.1.0-pre-release.21 | 可解析 metadata v39 / DummyDll；IL body 为空 | exe `663fb432433b4371fd1ee0ebc321a8fff2a9aac5ac4230c843f9e03ddee4e04c`，插件 `2cc4f8c66541f18b4daed5410149ee7859c3190454d30bda5ccd0895d2989ee6` |
| Il2CppDumper perfare 6.7.46 | 失败（不支持 metadata v39） | zip `d1117d66b9f8a130fd4933ae99f8eb9a0ecbbdb79d1fc494eb738ad910ff765a` |
| ILSpy 9.1.0.7988 | 可加载反编译库，但 IL body 为空故未用于本次结论 | zip `1e925a4ce4c79d68139a486da66c682e2191e51215188816f613866c7276fc55` |
| capstone / UnityPy | 5.0.7 / 1.25.4 | pip 安装 |

## 2. 基础命令

```powershell
# 反汇编某个方法：fn <类名> <文件内 offset>
PYTHONPATH=<author-archive>/rev .venv\Scripts\python.exe -X utf8 <author-archive>\rev\analyze.py fn <Class> <offset>

# 解析 BlackHoleTree.updateMutator 的 switch 标签与各 case 效果
.venv\Scripts\python.exe -X utf8 <author-archive>\rev\analyze_tree.py
.venv\Scripts\python.exe -X utf8 <author-archive>\rev\analyze_bh_tree.py

# 解析 script.json 的 ScriptString（node effect label -> 字符串）
.venv\Scripts\python.exe -X utf8 <author-archive>\rev\parse_scriptstring.py

# 扫描全部 assets_*.bundle，定位包含 bh2 节点名的 bundle
.venv\Scripts\python.exe -X utf8 <author-archive>\rev\scan_node_bundles.py
```

`analyze.py` 使用与 Il2CppDumper 相同的 PE 映射（RVA→文件 offset）。下面给出的 offset 均可用上命令直接复现。

## 3. 定位片段索引

| 结论 | 方法（类::方法） | RVA | 文件 offset | 输出/证据 |
|---|---|---|---|---|
| 本体每跳/销毁不补余量 | `RepeatedlyDamageEnemiesWithinRadius::OnUpdateTick` / `OnDestroy` | 0x1015430 / 0x10151e0 | 0x1013e30 / 0x1013be0 | `code-findings-1.5.3-round1.json`，README §7.1 |
| Heat Wave 光环 0.5s/首判 | `BlackHoleMutator::Mutate` | 0x182f6e0 | 0x182e0e0 | `code-findings-1.5.3-round1.json`，README §7.2 |
| Red Giant / Binary 结构转换 | `BlackHoleMutator::Mutate`（0x18300e0–0x1830220 区域） | 0x182f6e0 | 0x182e0e0 | README §7.3 |
| bh2 技能树节点数值 | `SkillTreeNode` 数据（UnityPy，`assets_5557fd063dd9c5ac.bundle`） | — | — | `bh2-skilltree-nodes-1.5.3.json` |
| 节点实际效果（含 Binary/Singularity 的 more） | `BlackHoleTree::updateMutator` | 0x24bd660 | 0x24bc060 | `bh-tree-mutator-effects-1.5.3.json`，README §9 |
| Stats.Stat 参数含义 | `Stats.Stat::ctor` | 0x16a5360 | 0x16a3d60 | README §9 |
| baseDamage 加法语义 | `DamageStatsHolder::addBaseDamage/getBaseDamage` | 0x2372e80 / 0x2373fa0 | 0x2371880 / 0x23729a0 | `bh-tree-mutator-effects-1.5.3.json` |
| Recurrence/充能速率 | `BlackHoleMutator::getChargeRegen` | 0x1831320 | 0x182fd20 | `cooldown-charge-1.5.3.json`，README §10 |
| 冷却公式结构 | `AbilityMutator::GetCooldown` / `getChargeRegen` | 0x2405960 / 0x2408400 | 0x2404360 / 0x2406e00 | `cooldown-charge-1.5.3.json` |
| 多黑洞生命周期 | `BlackHoleMutator::Mutate` / `onStartedAbility` / `onStoppedAbility` | 0x182f6e0 / 0x18319f0 / 0x18322c0 | 0x182e0e0 / 0x18303f0 / 0x1830cc0 | `multi-black-hole-lifecycle-1.5.3.json`，README §12 |
| Ignite 继承路径（ApplyAilment 取 ability mutator 的伤害统计） | `AilmentReceiver::ApplyAilment` / `DealDamageForAilment` | 0x2832d40 / 0x2834c00 | 0x2831740 / 0x2833600 | README §10/待用户实测确认 Binary 继承 |

## 4. 数据侧命令

```powershell
# bh2 节点数据（UnityPy）与 Ability 字段
PYTHONPATH=<author-archive>/rev .venv\Scripts\python.exe -X utf8 <author-archive>\rev\extract_game_data.py
# 输出：<author-archive>/rev/game-data/*.json（示例：bh2-skilltree-nodes.json / permaload-*.json）
```

证据 JSON 与本文档同目录；哈希可由 `Get-FileHash <file> -Algorithm SHA256` 复核。
