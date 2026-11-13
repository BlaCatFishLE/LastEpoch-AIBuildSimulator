"""The affix-db category is not an exclusive crafting group."""
import copy
from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'tools/bd_editor'))
from service import Workspace

class AffixCategoryTests(unittest.TestCase):
    def test_same_category_roll_save_and_real_duplicate_rejection(self):
        with tempfile.TemporaryDirectory(prefix='affix-category-',dir=ROOT/'.scratch') as folder:
            w=Workspace(ROOT,storage=folder)
            try:
                choices={x['id']:x for x in w.affixes(21)}
                self.assertEqual(choices[100]['category_group'],choices[12]['category_group'])
                self.assertEqual(choices[80]['category_group'],choices[13]['category_group'])
                self.assertNotIn('group',choices[100])
                request={'definition':'item:letools:itemList.equippable_21:1','slot':'ring1','class_id':1,'mastery':1,'level':100,'ir':[255],'ur':[],
                         'affixes':[{'id':aid,'tier':5,'channel':'normal','roll':255} for aid in (100,12,80,13)]}
                original=w.make_item(request)
                request['affixes'][0]['roll']=100
                preview=w.preview_item(request);item=preview['item']
                self.assertEqual(len(item['affixes']['normal']),4)
                before=original['affixes']['normal'][0]['value']['values'][0]['value_numeric']
                after=item['affixes']['normal'][0]['value']['values'][0]['value_numeric']
                self.assertLess(after,before)
                self.assertEqual(preview['roll_values']['affixes'][0][100],item['affixes']['normal'][0]['value']['values'][0]['display_value'])
                spec=w.new_spec(1,1,100);spec['equipment']['slots']=[{'slot':'ring1','instance':item}]
                saved=w.save({'name':'category check','build_spec':spec})
                reloaded=w.get(saved['id'])['build_spec']['equipment']['slots'][0]['instance']
                self.assertEqual(reloaded['editor']['affixes'][0]['roll'],100)
                self.assertEqual(reloaded['affixes']['normal'][0]['value']['values'][0]['value_numeric'],after)
                duplicate=copy.deepcopy(request);duplicate['affixes']=[duplicate['affixes'][0],copy.deepcopy(duplicate['affixes'][0])]
                with self.assertRaisesRegex(ValueError,'相同词缀'):w.make_item(duplicate)
                too_many=copy.deepcopy(request);too_many['affixes']=[{'id':aid,'tier':5,'channel':'normal','roll':255} for aid in (100,12,9)]
                with self.assertRaisesRegex(ValueError,'前后缀数量'):w.make_item(too_many)
            finally:w.c.close();w.a.close()

if __name__=='__main__':unittest.main()
