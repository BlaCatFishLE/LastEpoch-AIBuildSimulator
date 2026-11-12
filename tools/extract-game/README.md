# extract-game：本地 Last Epoch 资源只读检查工具

第一阶段工作包 W1/W5 的最小工具集。只读访问本机游戏安装，不写游戏目录、不改存档、不上传任何游戏文件。
所有输出写到项目内路径（`data/raw/game/<snapshot>/`）。

游戏安装：`<game-install>`（可用 `--game-root` 覆盖，或设置环境变量 `LE_GAME_ROOT`）。
提取时的版本证据：Steam buildid `26087601`，Unity `6000.4.8f1`，游戏版本 `1.5.3`。详见
作者本地归档的 `game-assets.md` 与 `data/mappings/game-build.json`。

## 环境

```powershell
# 项目根目录 <repo>
# Python 3.12.14（本机 dsh runtime 解释器）
& '<python>' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r tools\extract-game\requirements.txt

# 实际锁定版本
.\.venv\Scripts\python.exe -m pip show UnityPy    # Version: 1.25.4
```

`UnityPy==1.25.4` 是当前 PyPI 最新版。无需安装 Il2CppDumper/Cpp2IL：游戏的 `*.bundle` 内嵌
serialized type tree，`MonoBehaviour` 字段可直接读取（见下文限制）。

## 命令

以下命令均已在本机实际运行（2026-11-01，UnityPy 1.25.4）。

```powershell
$py = '.\.venv\Scripts\python.exe'
$game = '<game-install>'
$data = "$game\Last Epoch_Data"
$bundles = "$data\StreamingAssets\LEAssetBundles"
$out = 'data\raw\game\game-build-26087601'

# 1) 序列化文件头：Unity 版本 / 序列化格式 / 对象数 / 是否含 type tree
& $py tools\extract-game\inspect_assets.py info --container "$data\globalgamemanagers" --out "$out\globalgamemanagers.info.json"
& $py tools\extract-game\inspect_assets.py info --container "$data\resources.assets"     --out "$out\resources.assets.info.json"
& $py tools\extract-game\inspect_assets.py info --container "$bundles\monoscripts.bundle" --out "$out\monoscripts.info.json"

# 2) 对象类型计数 Top-N
& $py tools\extract-game\inspect_assets.py types --container "$data\resources.assets" --out "$out\resources.assets.types.json"

# 3) 游戏版本/品牌信息（PlayerSettings；strict read 在 Unity 6000.4 上差 4 字节，工具会自动退化为 check_read=False 并记录错误原文）
& $py tools\extract-game\inspect_assets.py settings --out "$out\playersettings.read.json"

# 4) 全部脚本类名（从游戏共享的 monoscripts.bundle 读取 MonoScript；这是定位数据对象的索引）
& $py tools\extract-game\inspect_assets.py monoscripts --out "$out\monoscripts.classes.json"
& $py tools\extract-game\inspect_assets.py monoscripts --filter "SkillTree" --out "$out\monoscripts.skilltree-classes.json"

# 5) 某个 bundle 内 MonoBehaviour 的脚本类归属计数（已自动注册 CAB-monoscripts，可为空结果提供诊断）
& $py tools\extract-game\inspect_assets.py scripts --bundle "$bundles\PermaLoad.bundle" --top 80 --out "$out\PermaLoad.scripts.json"

# 6) 单个对象导出：容器路径、path_id、类型、脚本类、完整 type tree（字段值）、原始字节
& $py tools\extract-game\inspect_assets.py sample --bundle "$bundles\PermaLoad.bundle" --path-id 1223590356862614629 --out "$out\sample-ability-fireball.json"
& $py tools\extract-game\inspect_assets.py sample --bundle "$bundles\assets_dbb8e576c5c2397b.bundle" --path-id 241487822650745262 --out "$out\sample-skilltreenode-poison-tipped.json"

# 7) 函数：扫描全部 bundle，按脚本类名正则找候选数据对象（26k bundle，只读；本机 17,151 个/210s）
& $py tools\extract-game\scan_bundles.py --pattern "SkillTree|SkillTreeNode|AffixList|CharacterClass" --stop-after 30 --out "$out\bundle-class-scan.json"

# 8) TextAsset 清单（只导出元数据+预览；加 --dump DIR 才落盘原文）
& $py tools\extract-game\inspect_assets.py textassets --container "$data\resources.assets" --limit 200 --out "$out\textassets.summary.json"
```

`inspect_assets.py` 每个子命令都支持 `--game-root`；`--out` 写 JSON，同时默认打印到 stdout。

## 能做什么 / 不能做什么（实测结论）

- 能：打开 UnityFS AssetBundle 与 player serialized file；枚举对象与类型；读取内置 Unity 类字段；
  读取 `TextAsset` 原文；读取 `MonoScript`（类名/命名空间/程序集）；在注册 `CAB-monoscripts` 后把
  bundle 内每个 `MonoBehaviour` 解析到脚本类；对 bundle 内 `MonoBehaviour` 读取完整字段
  （游戏 bundle 的 `enable_type_tree = true`）。
- 不能：读取 player build 内嵌 `*.assets`（`resources.assets`、`globalgamemanagers.assets` 等）里
  `MonoBehaviour` 的字段（`enable_type_tree = false`，无 type tree）；`PlayerSettings` 严格读取在
  Unity 6000.4 上差 4 字节（非严格读取可用）。
- 工具版本记录：Python 3.12.14 + UnityPy 1.25.4（依赖 tpk_ar 0.2.4 等，见
  `data/raw/game/game-build-26087601/manifest.json`）。

## 失败点原文（供对照）

- `ValueError: Expected to read 268 bytes, but only read 60 bytes`
  —— `resources.assets` 的 MonoBehaviour 无 type tree，UnityPy 回退到基类节点后读取失败。
- `ValueError: MonoBehaviour detected, but no typetree_generator set to the environment!`
  —— 未安装/未配置 `TypeTreeGeneratorAPI` 时显式调用生成器路径。
- `FileNotFoundError: File cab-monoscripts not found in ...Last Epoch_Data`
  —— 未加载 `monoscripts.bundle` 时解析 bundle 的 `m_Script` 引用。
- `ValueError: Expected to read 916 bytes, but only read 912 bytes`
  —— Unity 6000.4 的 PlayerSettings typetree 与 UnityPy 内置 typetree 有 4 字节偏差。

## 边界

- 绝不修改游戏目录或存档；本工具只以读方式 `open` 游戏文件，输出只写项目内路径。
- 不安装 Il2CppDumper/Cpp2IL；不反编译 `GameAssembly.dll`；不执行游戏代码。
- 原始样本只保存当前数据任务所需的少量对象；完整 26k bundle 的全量导出不在 W1 范围。
