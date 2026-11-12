import copy
import json
import unittest
from pathlib import Path
from le_calc.workflow.components import collect_components, bind_instances
from le_calc.ruleset import Ruleset
from le_calc import model



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

class ComponentsTest(unittest.TestCase):
    def calc(self):
        return {"skills":{"specializations":[
            {"tree":{"entity_id":"tree:x:bh2"}, "nodes":[{"node_id":"2","points":4}]},
            {"tree":{"entity_id":"tree:x:sb44eQ"}, "nodes":[{"node_id":"9","points":3}]}]},
            "items":[{"instance_id":"a","definition_entity_id":"item:x:211"},
                     {"instance_id":"b","definition_entity_id":"item:x:211"}],
            "mods":[{"mod_id":"a#0","stat":{"stat_id":"stat:x:damage"},"value_numeric":.2},
                    {"mod_id":"b#0","stat":{"stat_id":"stat:x:damage"},"value_numeric":.3}]}

    def test_whole_build_and_duplicate_instances(self):
        rows=collect_components(self.calc())
        self.assertIn("node:sb44eQ:9", {r["selector"] for r in rows})
        bound=bind_instances({"selectors":["stat:x:damage"]},rows)
        self.assertEqual([.2,.3],[r["value"] for r in bound])
        self.assertEqual(2,len(bind_instances({"selectors":["item:x:211"]}, rows)))

    def test_main_skill_change_preserves_shared_sources(self):
        c=self.calc();before=collect_components(c)
        c["skills"]["specializations"][0]["tree"]["entity_id"]="tree:x:new"
        after=collect_components(c)
        for selector in ["item:x:211","node:sb44eQ:9"]:
            entry={"selectors":[selector]}
            self.assertEqual(bind_instances(entry,before),bind_instances(entry,after))
        self.assertNotIn("node:bh2:2", {r["selector"] for r in after})

    def test_changed_values_are_current(self):
        c=self.calc();c["mods"][0]["value_numeric"]=.7
        self.assertEqual(.7,bind_instances({"selectors":["stat:x:damage"]},collect_components(c))[0]["value"])

    def test_node_points_drive_more_without_mutating_rules(self):
        data={"skill_tree_nodes":[{"node_id":"2","effects":[{"stat_key":"damage","operation":"more", "value_per_point":.15,"value_total":.6,"applies_to":["body"]}]}]}
        r=Ruleset(data)
        snapshot=model.Snapshot({}, {}, {}, {}, [],derived={"skill_node_points":{"bh2":{"2":2}}})
        current=r.with_snapshot_points(snapshot)
        self.assertEqual(.3,current.tree_more_effects({"body"})[0]["value"])
        self.assertEqual(.6,r.tree_more_effects({"body"})[0]["value"])
        snapshot.derived["skill_node_points"]["bh2"]={}
        self.assertEqual(0,r.with_snapshot_points(snapshot).tree_more_effects({"body"})[0]["value"])



class ResolverReuseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sys
        cls.root=Path(__file__).resolve().parents[3]
        sys.path.insert(0,str(cls.root/"tools/knowledge/src"))
        from le_calc.workflow import domain
        cls.domain=domain

    def resolve(self, case_ref, case_id, ability):
        from le_calc.workflow.contract import SCHEMA_RESOLVE_BUILD_REQUEST
        import tempfile
        case=json.loads((self.root/case_ref).read_text(encoding="utf-8"))
        case["case_id"]=case_id
        with tempfile.TemporaryDirectory(dir=self.root/".scratch") as tmp:
            ref=Path(tmp)/"case.json"
            ref.write_text(json.dumps(case),encoding="utf-8")
            return self.domain.resolve_build({"schema_version":SCHEMA_RESOLVE_BUILD_REQUEST,
                "case_ref":str(ref),"case_id":case_id,"main_skill":{"ability_id":ability}},root=self.root)

    def test_new_case_reuses_template_without_scope_inheritance(self):
        result=self.resolve("examples/black-hole/case.legacy.json","unseen-bh-case","bh2")
        self.assertIsNotNone(result["analysis_plan"]["skill_template"])
        self.assertIn("bh-node-more-2",{m["mechanism_id"] for m in result["bound_mechanisms"]})
        for m in result["bound_mechanisms"]:
            if m["reuse_mode"]=="shared":
                self.assertNotIn(m["mechanism_id"],{e["id"] for e in result["coverage"]["out_of_scope"]})

    def test_switch_main_skill_retains_equipment_and_support_skill(self):
        old=self.resolve("examples/flame-reave/case.legacy-hit.json","new-build","fr11mv")
        changed=self.resolve("examples/flame-reave/case.legacy-hit.json","new-build","unsupported-main")
        for result in [old,changed]:
            ids={m["mechanism_id"] for m in result["bound_mechanisms"]}
            self.assertIn("fr-effect-army-of-skin-high-health",ids)
            self.assertIn("fr-effect-enchant-weapon",ids)
        self.assertIsNone(changed["analysis_plan"]["skill_template"])
        self.assertEqual("needs_research",changed["readiness"])

    def test_source_absence_does_not_match_equipment_by_slot(self):
        result=self.resolve("examples/black-hole/case.legacy.json","new-build","bh2")
        ids={m["mechanism_id"] for m in result["bound_mechanisms"]}
        self.assertNotIn("fr-effect-army-of-skin-high-health",ids)

    def test_disabled_buff_still_reuses_known_rule(self):
        result=self.resolve("examples/black-hole/case.legacy.json","new-build","bh2")
        self.assertIn("bh-cond-throne-of-ambition",{m["mechanism_id"] for m in result["bound_mechanisms"]})
        # Owning a known buff does not force it on or modify the input scenario.
        scenario=json.loads((self.root/"examples/black-hole/scenario/conditional-scenario.json").read_text(encoding="utf-8"))
        self.assertEqual(0,scenario["states"]["player.ambition_stacks"])


    def test_variant_reuses_fr_nodes(self):
        result=self.resolve("examples/flame-reave-variant/case.json","unseen-fr-variant","fr11mv")
        ids={m["mechanism_id"] for m in result["bound_mechanisms"]}
        self.assertIn("fr-node-ember-infusion",ids)
        self.assertIsNotNone(result["analysis_plan"]["skill_template"])


class ActualDamageBindingTest(unittest.TestCase):
    def test_changed_point_count_changes_body_and_ignite(self):
        from le_calc.ruleset import load_ruleset
        from le_calc.damage import compute_body
        from le_calc.ignite import compute_ignite_core
        root=Path(__file__).resolve().parents[3]
        case=json.loads((root/"examples/black-hole/case.legacy.json").read_text(encoding="utf-8"))
        snapshot=model.Snapshot.from_dict(json.loads((root/case["inputs"]["snapshot"]).read_text(encoding="utf-8")))
        rules=load_ruleset(str(root/"tools/phase3/rules/ruleset-bh-stage1-v4.json"))
        rules.with_snapshot_points(snapshot)
        original=snapshot.derived["skill_node_points"]["bh2"]["2"]
        scenario={"target_is_boss_or_rare": True,"target_within_center_1m": True,"both_stars_cover":True}
        body=compute_body(snapshot,rules,scenario)["totals"]["damage"]
        ignite=compute_ignite_core(snapshot,rules)["per_stack_total"]
        snapshot.derived["skill_node_points"]["bh2"]["2"]=2
        factor=1.3/(1+.15*original)
        self.assertAlmostEqual(compute_body(snapshot,rules,scenario)["totals"]["damage"]/body,factor)
        self.assertAlmostEqual(compute_ignite_core(snapshot,rules)["per_stack_total"]/ignite,factor)


class SnapshotIsolationTest(unittest.TestCase):
    def test_loaded_snapshots_do_not_share_point_state(self):
        root=Path(__file__).resolve().parents[3]
        c=json.loads((root/"examples/black-hole/case.legacy.json").read_text(encoding="utf-8"))
        d=json.loads((root/c["inputs"]["snapshot"]).read_text(encoding="utf-8"))
        a=model.Snapshot.from_dict(d)
        from le_calc.ruleset import load_ruleset
        rules=load_ruleset(str(root/"tools/phase3/rules/ruleset-bh-stage1-v4.json"))
        rules.with_snapshot_points(a)
        expected=a.derived["skill_node_points"]["bh2"]["2"]
        a.derived["skill_node_points"]["bh2"]["2"]=99
        b=model.Snapshot.from_dict(d)
        rules.with_snapshot_points(b)
        self.assertEqual(expected,b.derived["skill_node_points"]["bh2"]["2"])
