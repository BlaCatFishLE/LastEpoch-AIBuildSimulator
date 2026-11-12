"""研究案例只验证当前数据链，禁止借用默认模型。"""
from _common import load_json, sha256_file


def validate_data(case, root):
    checks = []
    def add(name, ok, detail=''):
        checks.append({'name': name, 'status': 'PASS' if ok else 'FAIL', 'detail': str(detail)})
    add('研究模型', case.get('model') == 'research')
    add('无默认规则继承', case.get('ruleset') == {} and case.get('conditional_registry') == {})
    try:
        add('必需数据指针', all(case.get('source', {}).get(k) for k in ('character_save', 'tool_native_wire')) and all(case.get('inputs', {}).get(k) for k in ('bd', 'calculation_input', 'corrections')))
        dataset = load_json(root / case['dataset'])
        add('数据集绑定', all(dataset.get(k) == v for k, v in case['dataset_expected'].items()))
        for key, value in {**case.get('source', {}), **case.get('inputs', {})}.items():
            if value:
                path = root / value
                add('输入指纹 ' + key, path.is_file() and sha256_file(path) == case['input_sha256'].get(key))
        build = case['build']
        path = root / build['build_spec']
        spec = load_json(path)
        meta = spec['meta']['revision']
        add('BuildSpec 指纹', sha256_file(path) == build['build_spec_sha256'])
        add('revision 绑定', meta['revision_id'] == build['revision_id'] and meta['content_sha256'] == build['content_sha256'])
        calc = load_json(root / case['inputs']['calculation_input'])
        add('计算输入构筑绑定', all(calc.get(k) == build.get(k) for k in ('build_id', 'revision_id', 'content_sha256')))
        # 同一校验入口从当前 export 的 build_meta 逐项核对，不扫描任何其它案例。
        from _common import calculation_input_counts
        add('当前结构声明', calculation_input_counts(calc) == case['snapshot_expectations'])
        ability = case['main_skill']['ability_id']
        add('当前主技能专精', any(str((s.get('tree') or {}).get('entity_id', '')).endswith(':' + ability)
            for s in calc.get('skills', {}).get('specializations', [])))
        add('无伤害快照', not case['inputs'].get('snapshot'))
    except (KeyError, OSError, ValueError) as exc:
        add('数据可读', False, exc)
    return checks
