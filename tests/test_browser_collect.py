import json
from pathlib import Path
import shutil
import subprocess
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts/browser_collect.mjs'
NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'Node.js is required for browser collector fixtures')
class CollectorTests(unittest.TestCase):
    def collect(self, change=None):
        program = '''
import {collect} from MODULE;
const change=CHANGE;
let comments=0, navigations=0, reads=0;
const result=await collect({base:{token:'FixtureBase',url:'https://fixture.feishu.cn/base/FixtureBase'},
  cdp_url:'http://127.0.0.1:9222',user_id:'fixture-user',
  tables:[{id:'tblOne',field_ids:['fldProject']},{id:'tblTwo',field_ids:['fldProject']}]},
  async (options, callback)=>callback({navigate:async()=>{navigations++;},
    evaluate:async(fn,input)=>{
      if(fn.name==='readTable') {
        reads++;
        if(change==='second-read-failed'&&comments) return {status:'failed',code:'RECORDS_INCOMPLETE'};
        const row={record_id:'recA',project:change==='moved'&&comments?'B':'A',text:['one','two']};
        if(change==='key-order'&&comments) return {status:'complete',base_token:input.base_token,user_id:input.user_id,
          table_id:input.table_id,data:{rows:[{text:['one','two'],project:'A',record_id:'recA'}],stable:true}};
        if(change==='text-order'&&comments) row.text.reverse();
        return {status:'complete',base_token:input.base_token,user_id:input.user_id,
          table_id:input.table_id,data:{stable:true,rows:[row]}};
      }
      comments++;
      return {status:'complete',data:{threads:[{text:change==='new-reply'&&comments===2?'new':'old'}]}};
    }}));
process.stdout.write(JSON.stringify({result,comments,navigations,reads}));
'''.replace('MODULE', json.dumps(MODULE.as_uri())).replace('CHANGE', json.dumps(change))
        result = subprocess.run([NODE, '--input-type=module', '-e', program],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_stable_read_reloads_all_tables_and_comments_before_accepting(self):
        result = self.collect()
        self.assertEqual(result['result']['consistency'], 'two_complete_passes_equal')
        self.assertEqual((result['reads'], result['comments'], result['navigations']), (4, 2, 3))

    def test_moved_record_and_new_reply_reject_whole_scan(self):
        for change in ('moved', 'new-reply', 'text-order'):
            with self.subTest(change=change):
                result = self.collect(change)['result']
                self.assertEqual(result, {'status': 'failed', 'code': 'SNAPSHOT_CHANGED'})

    def test_object_key_order_is_not_a_content_change(self):
        self.assertEqual(self.collect('key-order')['result']['status'], 'complete')

    def test_second_pass_failure_does_not_expose_first_pass_as_complete(self):
        self.assertEqual(self.collect('second-read-failed')['result'],
                         {'status': 'failed', 'code': 'RECORDS_INCOMPLETE'})


if __name__ == '__main__':
    unittest.main()
