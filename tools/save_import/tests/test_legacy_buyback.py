"""旧商店回购不能阻断当前装备；构筑物品继续严格校验。"""
import unittest
from le_save_import import items

class LegacyBuybackTests(unittest.TestCase):
    def test_explicit_scope_excludes_only_v2_buyback_and_preserves_index(self):
        v6=[6,0,0,127,255,34,116,0,0,255,17,0,0,0]
        doc={'savedItems':[{'containerID':32,'data':[2,16,0]}, {'containerID':33,'data':v6}]}
        result=items.decode_save_items(doc,exclude_legacy_buyback=True)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['index'],1)
        self.assertEqual(doc['savedItems'][0]['data'],[2,16,0])

    def test_legacy_equipped_item_still_fails(self):
        for cid in (2,4,29,33):
            with self.subTest(cid=cid), self.assertRaises(items.DecodeError):
                items.decode_save_items({'savedItems':[{'containerID':cid,'data':[2,16,0]}]},exclude_legacy_buyback=True)

    def test_unknown_buyback_version_still_fails(self):
        with self.assertRaises(items.DecodeError):
            items.decode_save_items({'savedItems':[{'containerID':32,'data':[99,16,0]}]},exclude_legacy_buyback=True)
