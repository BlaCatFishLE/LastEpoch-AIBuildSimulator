"""正式 tool-native 存档适配器；外部依赖显式配置，输出不依赖案例名称。"""
from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path

from _common import PipelineError, load_json, relative, resolve, write_json


def dependencies(root, config, *, tool_home=None):
    external = config.get('external_tools') or {}
    native = external.get('tool_native') or {}
    node_config = external.get('node') or {}
    home = tool_home or os.environ.get('LE_TOOL_NATIVE_HOME') or native.get('tool_home')
    node = os.environ.get('LE_NODE') or node_config.get('path')
    if not node and (root / 'tools/node/node.exe').is_file():
        node = 'tools/node/node.exe'
    if not node:
        node = shutil.which('node')
    if not home or not node:
        raise PipelineError('needs_input: 新存档解码需要 LE_TOOL_NATIVE_HOME/--tool-home 和 LE_NODE（或 PATH 中的 node）')
    home, node = resolve(root, home), resolve(root, node)
    if not node.is_file() or not (home / 'planner/js/planner.js').is_file():
        raise PipelineError('needs_input: tool-native 目录或 Node 不可用；请检查 --tool-home / LE_NODE')
    return node, home


def normalize_wire(wire, save):
    """工具 blessing 缺省 ir 用同一存档 v6 原始隐式 roll 补齐。"""
    wire = copy.deepcopy(wire)
    mapping = {33: '1', 34: '2', 35: '3',
               36: '4', 37: '5', 38: '6',
               39: '7', 43: '8', 44: '9', 45: '10'}
    seen = set()
    for item in save.get('savedItems') or []:
        key = mapping.get(item.get('containerID'))
        if not key or key not in (wire.get('blessings') or {}):
            continue
        if key in seen:
            raise PipelineError(f'同一 blessing 容器重复：{key}')
        seen.add(key)
        data = item.get('data') or []
        if len(data) < 12 or data[0] != 6:
            raise PipelineError(f'不支持的 blessing ItemData 布局：{key}')
        wire['blessings'][key]['ir'] = data[9:12]
    bio = wire.get('bio') or {}
    version = wire.get('dataVersion')
    if not version or not wire.get('skillTrees'):
        raise PipelineError('tool-native wire 缺失版本或技能树')
    return {'data': wire, 'data_version': version, 'created_for_build': version,
            'level': bio.get('level'), 'class': bio.get('characterClass'),
            'mastery': bio.get('chosenMastery'), 'tags': None, 'guide': None}


def decode(api, save_path, out_dir, *, tool_home=None):
    root = api.ROOT
    node, home = dependencies(root, api.load_config(), tool_home=tool_home)
    out_dir.mkdir(parents=True, exist_ok=False)
    harness = root / 'tools/save_import/scripts/tool_native/run_tool_native.js'
    result = api.run_child([str(node), str(harness), '--save', str(save_path),
                            '--tool-home', str(home), '--out', str(out_dir)])
    if result.returncode:
        raise PipelineError(f'tool-native 解码失败：exit {result.returncode}')
    summary = load_json(out_dir / 'run-summary.json')
    if (summary.get('decoded') or {}).get('errors'):
        raise PipelineError('tool-native 部分物品解码失败；详情见 run-summary.json')
    if any(not v.get('ok') for v in (summary.get('idEncoderSelfCheck') or {}).values()):
        raise PipelineError('tool-native ID 编码自检失败')
    raw = save_path.read_text(encoding='utf-8-sig')
    if not raw.startswith('EPOCH'):
        raise PipelineError('角色存档缺少 EPOCH 头')
    normalized = normalize_wire(load_json(out_dir / 'wire-from-save.json'), json.loads(raw[5:]))
    path = write_json(out_dir / 'wire-normalized.json', normalized)
    # 只归一生成的诊断文本，不改原始存档或 wire 数据。
    from le_calc.workflow.portable import sanitize_directory
    sanitize_directory(out_dir, root)
    return path
