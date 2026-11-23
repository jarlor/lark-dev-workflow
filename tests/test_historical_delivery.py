import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import historical_delivery as old
import stage_action
from prepare_plan import digest
from test_stage_business import Project


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        p = self.p = Project(Path('/unused'), 'work'); p.key = 'work-deliver'
        actor = p.row['plan_confirmer'][0]; p.row['owner'] = [actor]
        p.row['status'] = ['已交付']; self.anchor = copy.deepcopy(p.rows[0]); t = self.anchor['create_time']
        self.plan = dict(proof=dict(check='archived_runner_history', operator_open_id=actor['id'],
            table_revision=self.anchor['rev'], update_time=t, business_revision=p.row['plan_revision']),
            anchor=self.anchor, source_report_sha256=digest('independent plan archive'))
        self.steps = []
        for a,b in [('可开始','进行中'),('进行中','已交付')]:
            p.add_change('status',a,b,actor['name']); evt=p.rows[0]
            self.steps.append(dict(history_sha256=digest(evt), revision=evt['rev'],time=evt['create_time'],
                run_id='00000001-0000-0000-0000-000000000001'))
        commit='1'*40
        self.test=dict(key='selftest', build=commit,executor='Old project worker', executed_at='original date', purpose=['研发自测'],
                       method=['自动化测试'],environment=['本地'],result=['通过'],criteria_snapshot='Check actual recovery',
                       summary='Recovery passed',evidence='original run',work=[{'id':'recDep'}],project=[{'id':p.project}])
        self.proof = dict(consumer_work_id='recConsumer', project_id=p.project,table_id=p.table,record_id='recDep',
            plan_revision=p.row['plan_revision'],plan_sha256=digest(p.row['plan']),stage_field_id=p.fields['status']['id'],
            account_open_id=actor['id'], executor_type='enrolled_browser_worker',granted_actions=['work-start','work-deliver'],
            authorized_at=t-10, original_approval_sha256=self.plan['source_report_sha256'], start=self.steps[0],
            delivery=self.steps[1],test_id='recTest',commit=commit,test_row_sha256=digest(self.test), report_sha256=digest('report'))
        self.business=mock.Mock(key='work-start'); self.business.links.return_value=['recDep']; self.business.timestamp.return_value=(t+1)*1000
        self.business.row.side_effect=lambda role,record: self.test if role=='verification' else p.row

    def verify(self,dependent='recConsumer'):
        with mock.patch('stage_action.Project',return_value=self.p),mock.patch('stage_business.archived_click',return_value=self.plan):
            return old.verify(self.business,self.p,'recDep','work-deliver',dependent,self.proof)

    def test_confirmed_dependency_with_matching_delivery_and_selftest(self):
        self.assertEqual(self.verify()[0],self.steps[1]['revision'])

    def test_no_substitution_for_own_delivery_acceptance_or_unlinked_dependency(self):
        for dependent in (None,'recOther','recDep'):
            with self.subTest(dependent=dependent),self.assertRaises(stage_action.Pending):self.verify(dependent)
        self.business.key='req-accept'
        with self.assertRaises(stage_action.Pending):self.verify()
        self.business.key='work-start';self.business.links.return_value=[]
        with self.assertRaises(stage_action.Pending):self.verify()

    def test_wrong_account_grant_version_receipt_or_build_cannot_unlock(self):
        baseline=copy.deepcopy(self.proof)
        for key,bad in [('account_open_id','ou_other'),('plan_revision',True),('plan_sha256',digest('new plan')),
            ('authorized_at',self.steps[0]['time']),('granted_actions',['work-deliver']),('original_approval_sha256',digest('AI note')),
            ('commit','2'*40),('start',self.steps[1]),('test_row_sha256',digest('different test'))]:
            self.proof=dict(baseline,**{key:bad})
            with self.subTest(key=key),self.assertRaises(stage_action.Pending):self.verify()
        self.proof=baseline;self.plan['proof']['check']='machine_filing'
        with self.assertRaises(stage_action.Pending):self.verify()

    def test_changed_then_restored_plan_or_owner_invalidates_archived_delivery(self):
        for key in ('owner','plan','plan_revision','dependencies','verification_plan','plan_confirmer'):
            rows=copy.deepcopy(self.p.rows)
            self.p.add_change(key,'old','another','person');self.p.add_change(key,'another','old','person')
            with self.subTest(key=key),self.assertRaises(stage_action.Pending):self.verify()
            self.p.rows=rows

    def test_other_author_mixed_field_or_rolled_back_stage_is_not_the_machine_step(self):
        for change in ('other_actor','mixed_step','late_reopen'):
            rows=copy.deepcopy(self.p.rows)
            if change=='other_actor':self.p.rows[0]['operator']='Other'
            if change=='mixed_step':self.p.rows[0]['field_changes']*=2
            if change=='late_reopen':self.p.add_change('status','已交付','进行中','负责的同事')
            with self.subTest(change=change),self.assertRaises(stage_action.Pending):self.verify()
            self.p.rows=rows

    def test_failed_or_repurposed_test_remains_invalid_even_with_new_row_digest(self):
        for k,bad in [('result',['未通过']),('purpose',['需求验收']),('method',['人工核验']),('environment',['生产'])]:
            old_value=self.test[k]; self.test[k]=bad; self.proof['test_row_sha256']=digest(self.test)
            with self.subTest(k=k),self.assertRaises(stage_action.Pending):self.verify()
            self.test[k]=old_value

    def test_report_cannot_drop_reuse_or_change_a_source(self):
        src={k:digest(k) for k in old.SOURCES}; other=digest('additional raw run'); item=dict(source_hashes=src,auxiliary_sha256=[other],commit='1'*40)
        item['report_sha256']=digest(item); hashes={*src.values(),item['report_sha256'],other};old.check_sources(item,hashes)
        for h in (src['owner_instruction'],src['start_before'],src['test_run'],src['ci'],src['artifact'],other):
            with self.subTest(missing=h),self.assertRaises(stage_action.Pending):old.check_sources(item,hashes-{h})
        item['commit']='2'*40
        with self.assertRaises(stage_action.Pending):old.check_sources(item,hashes)
        item['source_hashes']['owner_instruction']=src['delivery_command'];item.pop('report_sha256');item['report_sha256']=digest(item)
        with self.assertRaises(stage_action.Pending):old.check_sources(item,hashes|{item['report_sha256']})
