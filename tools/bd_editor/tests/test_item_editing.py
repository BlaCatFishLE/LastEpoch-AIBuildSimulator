"""Targeted item-edit persistence and canonical slider value checks."""
from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'tools/bd_editor'))
from service import Workspace

class ItemEditingTests(unittest.TestCase):
    def test_tier_roll_roundtrip_and_display_values(self):
        with tempfile.TemporaryDirectory(prefix='item-edit-',dir=ROOT/'.scratch') as folder:
            workspace=Workspace(ROOT,storage=folder)
            try:
                request={'definition':'item:letools:uniqueList.uniques:316','slot':'head','class_id':1,'mastery':1,'level':100,'sub_type':23,'ir':[255,255,255],'ur':[244,77,35,234,167,211,133,119],'affixes':[{'id':13,'channel':'normal','tier':6,'roll':100}]}
                preview=workspace.preview_item(request)
                self.assertEqual(preview['item']['definition']['name'],'Apex of Thought')
                actual=preview['item']['affixes']['normal'][0]['value']['values'][0]['display_value']
                self.assertEqual(preview['roll_values']['affixes'][0][100],actual)
                for raw in (0,255):
                    request['affixes'][0]['roll']=raw;item=workspace.make_item(request)
                    self.assertEqual(preview['roll_values']['affixes'][0][raw],item['affixes']['normal'][0]['value']['values'][0]['display_value'])
                spec=workspace.new_spec(1,1,100);spec['equipment']['slots']=[{'slot':'head','instance':preview['item']}]
                saved=workspace.save({'name':'item persistence check','build_spec':spec},True)
                loaded=workspace.get(saved['id']);head=loaded['build_spec']['equipment']['slots'][0]['instance']
                self.assertEqual(head['editor']['affixes'][0]['tier'],6);self.assertEqual(head['editor']['affixes'][0]['roll'],100)
                self.assertEqual(head['affixes']['normal'][0]['value']['values'][0]['display_value'],actual)
                request['affixes'][0]['roll']=256
                with self.assertRaises(ValueError):workspace.preview_item(request)
            finally:workspace.c.close();workspace.a.close()

if __name__=='__main__':unittest.main()
