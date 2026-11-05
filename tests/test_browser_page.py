import json
from pathlib import Path
import shutil
import subprocess
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'skill/lark-dev-workflow/scripts/browser_page.mjs'
NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'Node.js is required for browser page fixtures')
class PageReaderTests(unittest.TestCase):
    def run_page(self, change=None, comments=False):
        program = '''
import {readTable, readComments} from MODULE;
const change = CHANGE;
let tick = 0, reads = 0, idReads = 0;
Date.now = () => tick;
global.setTimeout = (callback, delay) => {
  tick += delay;
  if (change === 'replacement') window.bitableStore = {...window.bitableStore, getCurrentUserId:()=> 'other-user'};
  queueMicrotask(callback); return 1;
};
const fields = {fldText: {id:'fldText',type:1,name:'标题'},
  fldProject: {id:'fldProject',type:18,name:'项目',property:{tableId:'tblProjects'}}};
const table = {
  fieldsToJSON: () => fields,
  getRecordCount: () => change === 'row-order' ? 2 : 1,
  getLoadedRecordCount: () => change === 'partial' ? 0 : change === 'row-order' ? 2 : 1,
  getRecordIds: () => change === 'row-order' ? (++idReads === 1 ? ['rec2','rec1'] : ['rec1','rec2'])
    : change === 'duplicate' ? ['rec1','rec1'] : ['rec1'],
  getRecordDataById: () => {
    const row = {fldText:{value:[{type:'text',text:'示例'}]},fldProject:{value:['recProject']}};
    if (change === 'missing') delete row.fldProject;
    if (change === 'null') row.fldProject = null;
    if (change === 'changed' && ++reads > 1) row.fldText.value[0].text = '修改后的内容';
    if (change === 'key-order' && ++reads > 1) row.fldText.value[0] = {text:'示例',type:'text'};
    return row;
  }
};
global.window = {
  bitableStore: {getCurrentUserId: () => change === 'user' ? 'another-user' : 'fixture-user',
    modelOperator:{getTableById: () => table}},
  commentSDK: {serviceToken:change === 'base' ? 'another-base' : 'FixtureBase',
    getInitReplyListMode: () => 'incremental',
    request:{fetchInitData: async () => ({code:0,httpStatus:200,data:{}})}}
};
const input = {base_token:'FixtureBase',user_id:'fixture-user',table_id:'tblOne',field_ids:['fldText','fldProject']};
const result = await READER(input);
process.stdout.write(JSON.stringify(result));
'''.replace('MODULE', json.dumps(MODULE.as_uri())).replace('CHANGE', json.dumps(change)).replace(
            'READER', 'readComments' if comments else 'readTable')
        result = subprocess.run([NODE, '--input-type=module', '-e', program],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_explicit_null_remains_empty_but_missing_cell_is_not_invented(self):
        result = self.run_page('null')
        self.assertEqual(result['status'], 'complete')
        self.assertIsNone(result['data']['rows'][0]['cells']['fldProject'])
        self.assertEqual(self.run_page('missing')['code'], 'PAGE_INTERFACE_CHANGED')

    def test_partial_duplicate_and_changed_models_do_not_become_snapshots(self):
        for change in ('partial', 'duplicate', 'changed'):
            with self.subTest(change=change):
                self.assertEqual(self.run_page(change)['status'], 'failed')

    def test_record_set_and_object_key_order_are_not_business_changes(self):
        for change in ('row-order', 'key-order'):
            with self.subTest(change=change):
                result = self.run_page(change)
                self.assertEqual(result['status'], 'complete')
        self.assertEqual([r['record_id'] for r in self.run_page('row-order')['data']['rows']], ['rec1', 'rec2'])

    def test_wrong_account_and_base_are_rejected_before_reading(self):
        self.assertEqual(self.run_page('user')['code'], 'ACCOUNT_MISMATCH')
        self.assertEqual(self.run_page('base')['code'], 'RESOURCE_MISMATCH')
        self.assertEqual(self.run_page('replacement')['code'], 'PAGE_CONTEXT_CHANGED')

    def test_incremental_empty_initial_comments_do_not_mean_no_comments(self):
        result = self.run_page(comments=True)
        self.assertEqual(result['status'], 'failed')

    def run_comments(self, change=None, lengths=(2, 1)):
        threads = []
        for index, length in enumerate(lengths):
            replies = [{'replyId': str(9100000000000000000 + index * 100000 + n),
                        'userId': '9000000000000000001', 'content': '合成回复 ' + str(n),
                        'createTime': 1700000000000 + n, 'updateTime': 1700000000000 + n,
                        'deleted': False, 'submitting': False,
                        'extra': {'notify_extra': {'table': 'tblItems', 'record': 'recItem'}}}
                       for n in range(length)]
            threads.append({'commentId': str(9200000000000000000 + index),
                            'finished': bool(index), 'finishUserId': '',
                            'createTime': 1700000000000, 'updateTime': 1700000000000,
                            'replyList': replies})
        program = '''
import {readComments} from MODULE;
import {readFileSync} from 'node:fs';
const {threads, change} = JSON.parse(readFileSync(0, 'utf8'));
const ids = threads.map(x=>x.commentId);
let calls = 0;
const core = {allIdsUrl:'/api/comment/commentid/all',batchUrl:'/api/comment/batch',miniVersion:7,
  allIdsRequestControllInfo:{isLoaded:true,isError:false,requestId:null},
  scheduleControllInfo:{isError:false,loadingIdsCount:0},
  noPermCommentIdsCache:new Set(),noReturnDataCommentIdsCache:new Set(),
  getAllIdsRequestParams:()=>({comment_type_list:[0,1],need_comment_relations:false}),
  getBatchRequestParams:({ids})=>({comment_ids:ids,need_reply:true,need_msg_box_version:true}),
  isAllDataLoaded:()=>true,hasDataToConsume:()=>false,hasAllIdsDataToConsume:()=>false};
const schedule = {core,getAllIds:()=>ids,getScheduleErrorIds:()=>[],getScheduleState:()=> 'LOADED'};
const sdk = {serviceToken:'FixtureBase',hasInit:true,syncV2:{dataSchedule:schedule},
  batchAllComments:async()=>true,data:{getAllComments:()=>threads},getData:()=>({comments:threads})};
global.window = {bitableStore:{getCurrentUserId:()=> 'fixture-user'},commentSDK:sdk};
if(change==='no-permission') core.noPermCommentIdsCache.add('unread-id');
if(change==='no-response') core.noReturnDataCommentIdsCache.add(ids[0]);
if(change==='load-error') core.scheduleControllInfo.isError=true;
if(change==='pending-batch') core.hasDataToConsume=()=>true;
if(change==='pending-index') core.hasAllIdsDataToConsume=()=>true;
if(change==='initializing') sdk.hasInit=false;
if(change==='pending-merge') schedule.batchAllCommentsForMsgBoxResolve=()=>{};
if(change==='index-missing') schedule.getAllIds=()=>null;
if(change==='partial-replies') sdk.getData=()=>({comments:threads.map(t=>({...t,replyList:t.replyList.slice(0,1)}))});
if(change==='partial-thread') sdk.getData=()=>({comments:[]});
if(change==='draft') threads[0].replyList[0].submitting=true;
if(change==='duplicate') threads[0].replyList.push({...threads[0].replyList[0]});
if(change==='identity-change') sdk.batchAllComments=async()=>{window.bitableStore={getCurrentUserId:()=> 'other-user'};return true;};
if(change==='version-change') sdk.getData=()=>{if(++calls>1) core.miniVersion++;return {comments:threads};};
const output=await readComments({base_token:'FixtureBase',user_id:'fixture-user'});
process.stdout.write(JSON.stringify(output));
'''.replace('MODULE', json.dumps(MODULE.as_uri()))
        result = subprocess.run([NODE, '--input-type=module', '-e', program],
                                input=json.dumps({'threads': threads, 'change': change}),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_long_normalized_threads_and_solved_index_entries_are_not_truncated(self):
        result = self.run_comments(lengths=(257, 513))
        self.assertEqual(result['status'], 'complete')
        threads = result['data']['threads']
        self.assertEqual([len(t['replies']) for t in threads], [257, 513])
        self.assertTrue(threads[1]['is_solved'])
        self.assertEqual(threads[0]['replies'][0]['user_id'], '9000000000000000001')

    def test_partial_native_index_reply_export_or_read_denial_blocks_completion(self):
        for change in ('no-permission', 'no-response', 'load-error', 'index-missing',
                       'partial-replies', 'partial-thread', 'draft', 'duplicate',
                       'identity-change', 'version-change', 'pending-batch', 'pending-index',
                       'initializing', 'pending-merge'):
            with self.subTest(change=change):
                self.assertEqual(self.run_comments(change)['status'], 'failed')

    def test_empty_confirmed_index_is_distinct_from_missing_index(self):
        self.assertEqual(self.run_comments(lengths=())['status'], 'complete')
        self.assertEqual(self.run_comments('index-missing', lengths=())['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
