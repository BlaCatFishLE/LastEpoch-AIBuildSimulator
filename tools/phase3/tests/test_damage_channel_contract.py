"""共享伤害通道契约：技能基础元素不限制异类点伤，倍率按最终分量匹配。"""
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))
from le_calc import dps_components as dc, model, v4_execution, plan_executor_v2, plan_v4
from le_calc.mechanics import execution as mechanics


def row(identity, stat, value, tags, operation="added"):
    return model.Contribution(contribution_id=identity, source_kind="equipment", source_label=identity,
        source_path="synthetic", stat_name=stat, stat_key=stat, value=value, operation=operation,
        unit="flat" if operation=="added" else "percent", damage_tags=tags, included=True)


def rows():
    return [row("cold-flat", "added_damage", 20, ["Cold", "Bow"]),
            row("lightning-flat", "added_damage", 10, ["Lightning", "Bow"]),
            row("physical-flat", "added_damage", 5, ["Physical", "Bow"]),
            row("fire-inc", "damage", 1, ["Fire"], "increased"),
            row("cold-inc", "damage", 2, ["Cold"], "increased"),
            row("elemental-inc", "damage", .5, ["Elemental"], "increased"),
            row("wrong-attack", "added_damage", 999, ["Cold", "Spell"])]


SKILL={"base_damage":{"Fire":50},"added_damage_scaling":1,"crit_chance":0,"crit_multiplier":2}
KWARGS={"skill":SKILL,"component":{"tags":["Bow","Fire"],"adaptive_damage_type":"Fire","crit_capable":False},
        "crit_rules":{},"target":{},"states":{},"target_info":{"monster_damage_reduction":0}}
EXPECTED={"Fire":125.,"Cold":70.,"Lightning":15.,"Physical":5.}


class DamageChannelContractTest(unittest.TestCase):
    def test_default_allows_other_added_types_but_scales_each_own_channel(self):
        components, apps=dc.build_hit_components(contributions=rows(), **KWARGS)
        self.assertEqual({c["damage_type"]:c["damage"] for c in components},EXPECTED)
        cold=next(c for c in components if c["damage_type"]=="Cold")
        self.assertNotIn("Fire",cold["tags"])
        self.assertNotIn("fire-inc",cold["source_ids"])
        self.assertTrue(all(a["status"]!="applied" for a in apps if a["contribution_id"]=="wrong-attack"))

    def test_all_seven_types_receive_only_their_own_typed_increased(self):
        contributions=[]
        expected={}
        for index,dtype in enumerate(dc.DAMAGE_TYPES,1):
            contributions.append(row("flat:"+dtype,"added_damage",10,[dtype,"Bow"]))
            contributions.append(row("inc:"+dtype,"damage",index/10,[dtype],"increased"))
            expected[dtype]=(10+(50 if dtype=="Fire" else 0))*(1+index/10)
        components,apps=dc.build_hit_components(contributions=contributions,**KWARGS)
        self.assertEqual({c["damage_type"]:c["damage"] for c in components},expected)
        for c in components:
            typed=[identity for identity in c["source_ids"] if identity.startswith("inc:")]
            self.assertEqual(typed,["inc:"+c["damage_type"]])

    def test_actor_and_owner_share_damage_rules_with_scope_separation(self):
        scoped=rows()
        for r in scoped:r.raw={"scope":"actor"}
        scoped.append(row("owner-only","added_damage",1000,["Cold"]))
        scoped[-1].raw={"scope":"owner"}
        comps,apps=dc.build_hit_components(contributions=scoped,unit_scope="actor",**KWARGS)
        self.assertEqual({c["damage_type"]:c["damage"] for c in comps},EXPECTED)
        self.assertTrue(all(a["status"]=="not_applicable" for a in apps if a["contribution_id"]=="owner-only"))

    def test_native_and_legacy_multi_hit_expand_channels_without_extra_hit_events(self):
        for module in (v4_execution,plan_executor_v2):
            with self.subTest(module=module.__name__):
                plan={"components":[{"component_id":"event:shot","kind":"hit","damage_type":"Fire",
                    "tags":["Bow","Fire"],"crit_capable":False,"adaptive_damage_type":"Fire",
                    "hits_per_cast":2,"damage_weights":[1,.5]}]}
                components,apps=module.build_hit_components_v2(plan=plan,skill_parameters=SKILL,
                    contributions=rows(),target={},states={},target_info={"monster_damage_reduction":0},crit_rules={})
                self.assertEqual({c["damage_type"]:c["damage"] for c in components},EXPECTED)
                totals=module.component_hit_totals(components)
                self.assertEqual(totals["single_hit"],215)
                self.assertEqual(totals["per_cast_hit"],322.5)
                self.assertEqual(totals["hits_per_cast"],2)
                self.assertEqual(totals["effective_hits_per_cast"],1.5)
                known={c["component_id"] for c in components}
                self.assertTrue(all(a["component_id"] in known for a in apps))

    def test_component_tags_cannot_leak_stale_skill_elements(self):
        for dtype in dc.DAMAGE_TYPES:
            with self.subTest(dtype=dtype):
                tags=dc.component_tags({"tags":["Fire","Cold","Bow","Elemental"]},dtype)
                self.assertEqual(set(tags)&set(dc.DAMAGE_TYPES),{dtype})
                self.assertEqual("Elemental" in tags,dtype in {"Fire","Cold","Lightning"})
                self.assertIn("Bow",tags)

    def test_generic_settlement_requires_both_attack_and_damage_type(self):
        effects=[{"effect_id":"fire-inc","emits_contribution":True,"included_static":True,
                 "operation":"increased","value":1,"damage_tags":["Fire","Bow"]}]
        comps=[{"component_id":"cold","damage_type":"Cold","damage_channel":"cold","tags":["Fire","Bow"]},
               {"component_id":"fire-spell","damage_type":"Fire","tags":["Fire","Spell"]},
               {"component_id":"fire-bow","damage_type":"Fire","tags":["Fire","Bow"]}]
        settled=mechanics.settle_execution_effects(effects=effects,conditions={},states={},components=comps,target={},runtime=None)
        self.assertEqual(settled["rows"][0]["component_ids"],["fire-bow"])

    def test_public_execution_slice_does_not_match_fire_by_shared_bow_tag(self):
        from test_step5_v4_execution_slice import sample
        execution=sample()
        execution["components"][0]["tags"]=["Fire","Bow"]
        execution["target"]={"resistances":{"cold":0},"damage_reduction":0}
        execution["effects"].append({"effect_id":"fire-only","source_id":"source:1",
            "channel_tags":["Fire","Bow"],"scope":"body","operation":"increased","value":100})
        result=mechanics.execute_execution_slice(execution)
        cold=next(c for c in result["components"] if c["component_id"]=="skill:generic-hit")
        self.assertEqual(cold["damage"],150)

    def test_generated_component_refs_need_declared_parent_and_valid_type(self):
        known={"exact":{"event:shot"},"prefixes":{},"dot_suffixes":set()}
        self.assertTrue(plan_v4._assembly_is_known_component_ref(known,"event:shot:Cold"))
        self.assertFalse(plan_v4._assembly_is_known_component_ref(known,"unknown:Cold"))
        self.assertFalse(plan_v4._assembly_is_known_component_ref(known,"event:shot:Imaginary"))


if __name__=="__main__":unittest.main()
