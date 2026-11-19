"""Synthetic readbacks of the official two-step workflow. No Feishu requests or writes."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

from test_unattended import RuntimeFixture, write_json
from test_browser_watch import stage_field
import browser_watch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
import workflow_rule as module
import event_bridge as bridge


class WorkflowRuleTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.actor = {
            "identity": "bot",
            "profile": "fixture-profile",
            "app_id": "cli_demo",
            "open_id": "ou_assistant",
        }
        self.binding["cli_actor"] = self.actor
        self.config.update(format_version=3, event_tenant_key="tenant")
        self.save_config()
        self.service = dict(
            mode="observe",
            app_id="cli_demo",
            tenant_key="tenant",
            bot_open_id="ou_assistant",
            base_token="FixtureBase",
            project_record_id="recProject",
            lark_profile="fixture-profile",
            table_ids=["tblWork"],
            state_dir=str(self.root / "service"),
            runner_config=str(self.config_path),
        )
        Path(self.service["state_dir"]).mkdir(mode=0o700)
        self.service_path = self.root / "bridge.json"
        write_json(self.service_path, self.service)
        self.native = browser_watch.field(stage_field())
        self.native.update(id="fldStage", name="当前步骤")
        self.contract = {
            "steps": [
                dict(name="待确认计划", actor="human", person_field="person", required_fields=["plan"]),
                dict(name="可开始", actor="ai", person_field=None, required_fields=[]),
            ],
            "done": "已交付",
            "cancelled": "已取消",
        }
        self.mapped = self.binding["tables"]["work"]["fields"]
        self.mapped.update(
            status=dict(self.native, stage_contract=self.contract),
            person=dict(id="fldOwner", name="负责人", type="user"),
            plan=dict(id="fldPlan", name="计划", type="text"),
            project=dict(id="fldProject", name="所属项目", type="link", target_table_id="tblProjects"),
        )
        write_json(self.project_path, self.binding)
        self.fields = [
            dict(id="fldMarker", type="text", name="触发标记"),
            self.native,
            dict(id="fldLabel", type="text", name="测试文字"),
            dict(id="fldEvidence", type="text", name="凭证"),
            self.mapped["person"],
            self.mapped["plan"],
            self.mapped["project"],
        ]
        self.names = {f["id"]: f["name"] for f in self.fields}
        self.raw = [
            dict(field_id="fldMarker", type=1),
            dict(field_id="fldStage", type=24, ui_type="Stage"),
            dict(field_id="fldLabel", type=1),
            dict(field_id="fldProject", type=18, property={"table_id": "tblProjects"}),
        ]
        self.rule = dict(
            workflow_id="wkfExample",
            role="work",
            table_id="tblWork",
            marker_field_id="fldMarker",
            stage_field_id="fldStage",
            source=dict(id="optReady", name="可开始"),
            target=dict(id="optDone", name="已交付"),
            guard=dict(kind="text", field_id="fldLabel", value="仅检查配置"),
            required_field_ids=["fldEvidence"],
            owner_field_id=None,
        )
        self.trigger = dict(
            type="SetRecordTrigger",
            id="trigA",
            title="更新时",
            next="actA",
            children={"links": []},
            data=dict(
                table_name="研发项",
                field_watch_info=[{"field_name": "触发标记"}],
                record_watch_info=dict(
                    conjunction="and",
                    conditions=[
                        module.condition("测试文字", "is", [dict(value_type="text", value="仅检查配置")]),
                        module.condition(
                            "当前步骤", "is", [dict(value_type="option", value=dict(self.rule["source"]))]
                        ),
                        module.condition("凭证", "isNotEmpty", []),
                    ],
                ),
                trigger_control_list=["pasteUpdate", "automationBatchUpdate", "openAPIBatchUpdate"],
            ),
        )
        self.action = dict(
            type="SetRecordAction",
            id="actA",
            title="更新此行",
            children={"links": []},
            data=dict(
                table_name="研发项",
                ref_info={"step_id": "trigA"},
                filter_info=None,
                max_set_record_num=100,
                field_values=[
                    dict(
                        field_name="当前步骤",
                        value=[dict(value_type="option", value=dict(self.rule["target"]))],
                    )
                ],
            ),
        )
        self.definition = dict(workflow_id="wkfExample", steps=[self.trigger, self.action], status="enabled")
        self.reader = mock.Mock(profile="fixture-profile")
        self.reader.profile = "fixture-profile"
        self.reader.base.operator = "bot"
        self.reader.base.pages.return_value = [dict(id="tblWork", name="研发项")]
        self.reader.fields.return_value = self.fields
        self.reader.base._envelope.side_effect = lambda response, _: json.loads(response.stdout)["data"]
        self.reader.runner.side_effect = self.read_only
        self.registry = Path(self.service["state_dir"]) / module.REGISTRY
        self.pin()

    def pin(self):
        self.rule["definition_sha256"] = hashlib.sha256(
            json.dumps(self.definition["steps"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        paths = bridge.runner_pins(self.service)
        write_json(
            self.registry,
            dict(
                service=bridge.journal_identity(self.service),
                runner=bridge.writer_identity(str(self.config_path), paths),
                rules={"next": self.rule},
            ),
        )
        self.registry.chmod(0o600)

    def read_only(self, argv, **kwargs):
        self.assertIn("--as", argv)
        self.assertEqual(argv[argv.index("--as") + 1], "bot")
        self.assertEqual(argv[argv.index("--profile") + 1], self.actor["profile"])
        if argv[1:3] == ["api", "GET"]:
            self.assertEqual(argv[3], "/open-apis/bitable/v1/apps/FixtureBase/tables/tblWork/fields")
            data = dict(items=self.raw, total=len(self.raw), has_more=False)
        else:
            self.assertEqual(argv[1:3], ["base", "+workflow-get"])
            self.assertIn("wkfExample", argv)
            data = self.definition
        return subprocess.CompletedProcess(argv, 0, json.dumps(dict(ok=True, identity="bot", data=data)), "")

    def verify(
        self,
    ):
        return module.verify(self.service_path, "next", reader_factory=lambda *a, **kw: self.reader)

    def test_enabled_and_disabled_are_distinct(self):
        for value, enabled in [('enabled', True), ('disabled', False)]:
            self.definition['status'] = value
            result = self.verify()
            self.assertEqual(
                (
                    result['status'],
                    result['definition_matches'],
                    result['enabled'],
                    result['updates_trigger_row_only'],
                    result['dispatch_checked'],
                ),
                ('checked', True, enabled, True, False),
                result,
            )
            self.assertFalse(result['project_scope_verified'] or result['fixed_bot_owner_verified'])
            self.assertNotIn('ready', result)

    def test_branch_http_another_row_bulk_write_weak_condition_and_node_drift(self):
        mutations = [
            lambda t, a: t.update(next="another"),
            lambda t, a: t["children"]["links"].append({"step_id": "third"}),
            lambda t, a: t["data"]["record_watch_info"].update(conjunction="or"),
            lambda t, a: t["data"]["record_watch_info"]["conditions"].pop(),
            lambda t, a: t["data"]["record_watch_info"]["conditions"][1]["value"][0]["value"].update(
                id="optOther"
            ),
            lambda t, a: t["data"]["field_watch_info"].append({"field_name": "凭证"}),
            lambda t, a: a.update(type="HTTPClientAction"),
            lambda t, a: a["data"].update(ref_info={"step_id": "findOther"}),
            lambda t, a: a["data"].update(filter_info={"conditions": []}),
            lambda t, a: a["data"]["field_values"][0]["value"][0]["value"].update(name="已验收"),
            lambda t, a: a["data"]["field_values"].append({"field_name": "计划", "value": []}),
            lambda t, a: a["data"].update(table_name="其他表"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                draft = copy.deepcopy(self.definition)
                mutate(*draft["steps"])
                with self.assertRaises(module.Unverified):
                    module.check_graph(draft, self.rule, "研发项", self.names)

    def test_human_source_cannot_advance_or_skip_an_ai_node(self):
        self.rule.update(
            source={"id": "optWait", "name": "待确认计划"}, target={"id": "optReady", "name": "可开始"}
        )
        with self.assertRaisesRegex(module.Unverified, "AI"):
            module.source_step(self.rule, self.mapped)
        self.rule.update(
            source={"id": "optReady", "name": "可开始"}, target={"id": "optCancel", "name": "已取消"}
        )
        with self.assertRaisesRegex(module.Unverified, "AI"):
            module.source_step(self.rule, self.mapped)

    def test_required_fields_and_owner_cannot_be_dropped_or_treated_as_fixed_bot(self):
        self.contract["steps"][1]["required_fields"] = ["plan"]
        with self.assertRaisesRegex(module.Unverified, "遗漏"):
            module.source_step(self.rule, self.mapped)
        self.contract["steps"][1].update(required_fields=[], person_field="person")
        self.rule["owner_field_id"] = "fldOwner"
        self.trigger["data"]["record_watch_info"]["conditions"].append(
            module.condition("负责人", "isNotEmpty", [])
        )
        write_json(self.project_path, self.binding)
        self.pin()
        result = self.verify()
        self.assertTrue(result['definition_matches'], result)
        self.assertEqual(result['owner_condition'], 'nonempty_only')
        self.assertFalse(result['fixed_bot_owner_verified'])

    def set_project_guard(self):
        self.mapped['project']['link_table'] = {'id': 'tblProjects'}
        self.binding['tables']['projects']['fields']['number'] = {
            'id': 'fldNumber',
            'name': '编号',
            'type': 'auto_number',
        }
        write_json(self.project_path, self.binding)
        self.rule['guard'] = {'kind': 'project_link', 'field_id': 'fldProject', 'value': 'recProject'}
        self.trigger['data']['record_watch_info']['conditions'][0] = module.condition(
            '所属项目', 'is', [dict(value_type='text', value='recProject')]
        )
        self.reader.read_record.side_effect = None
        self.reader.read_record.return_value = {'record_id': 'recProject', 'values': {'fldNumber': 'PRJ-0001'}}
        self.pin()

    def set_assistant_owner(self):
        self.contract['steps'][1]['person_field'] = 'person'
        self.mapped['person']['multiple'] = False
        self.raw.append(dict(field_id='fldOwner', type=11, ui_type='User', property={'multiple': False}))
        self.rule.update(owner_field_id='fldOwner', owner_match='bound_assistant')
        owner_filter = module.condition('负责人', 'is', [
            dict(value_type='user', value=dict(id=self.actor['open_id'], name='示例助手')),
        ])
        self.trigger['data']['record_watch_info']['conditions'].append(owner_filter)
        write_json(self.project_path, self.binding)
        self.pin()
        return owner_filter

    def test_bound_assistant_proves_definition_including_disabled_rule(self):
        owner_filter = self.set_assistant_owner()
        self.set_project_guard()
        self.definition['status'] = 'disabled'
        for label in ('示例助手', '新的显示名', None):
            with self.subTest(label=label):
                person = owner_filter['value'][0]['value']
                if label is None:
                    person.pop('name')
                else:
                    person['name'] = label
                self.pin()
                result = self.verify()
                self.assertTrue(result['definition_matches'] and result['project_scope_verified'], result)
                self.assertTrue(result['fixed_bot_owner_verified'])
                self.assertEqual(result['owner_condition'], 'bound_assistant')
                self.assertFalse(result['enabled'] or result['dispatch_checked'])

    def test_exact_owner_rejects_wrong_identity_type_arity_and_conditions(self):
        correct = self.set_assistant_owner()
        conditions = self.trigger['data']['record_watch_info']['conditions']
        for label, mutate in {
            'same name, different id': lambda f: f['value'][0]['value'].update(id='ou_someone'),
            'name only': lambda f: f['value'][0]['value'].pop('id'),
            'wrong value type': lambda f: f['value'][0].update(value_type='text'),
            'dynamic value': lambda f: f['value'][0].update(step_id='trigA'),
            'dynamic person': lambda f: f['value'][0]['value'].update(field_name='其他人'),
            'multiple people': lambda f: f['value'].append(copy.deepcopy(f['value'][0])),
            'nonempty': lambda f: f.update(operator='isNotEmpty', value=[]),
            'contains': lambda f: f.update(operator='contains'),
            'wrong field': lambda f: f.update(field_name='其他人'),
            'duplicate condition': lambda f: conditions.append(copy.deepcopy(f)),
        }.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(correct)
                before = conditions[:-1]
                conditions[-1] = changed
                mutate(changed)
                self.pin()
                result = self.verify()
                self.assertFalse(result['definition_matches'] or result['fixed_bot_owner_verified'], result)
                conditions[:] = before + [correct]
        self.rule.pop('owner_match')
        self.pin()
        result = self.verify()
        self.assertFalse(result['definition_matches'] or result['fixed_bot_owner_verified'], result)

    def test_exact_owner_requires_single_person_field_and_bound_actor(self):
        self.set_assistant_owner()
        projected = self.mapped['person']
        native = self.raw[-1]
        for field, key, invalid in [
            (projected, 'type', 'text'), (projected, 'multiple', True), (projected, 'multiple', None),
            (native, 'type', 3), (native, 'ui_type', 'SingleSelect'), (native, 'property', {'multiple': True}),
        ]:
            with self.subTest(key=key, invalid=invalid):
                original = field[key]
                field[key] = invalid
                result = self.verify()
                self.assertFalse(result['definition_matches'] or result['fixed_bot_owner_verified'], result)
                field[key] = original
        self.contract['steps'][1]['person_field'] = None
        write_json(self.project_path, self.binding)
        self.assertFalse(self.verify()['fixed_bot_owner_verified'])
        self.contract['steps'][1]['person_field'] = 'person'
        self.actor['open_id'] = 'ou_replacement'
        write_json(self.project_path, self.binding)
        self.assertFalse(self.verify()['fixed_bot_owner_verified'])

    def test_exact_owner_registration_and_diagnostic_boundaries(self):
        self.set_assistant_owner()
        for changed in (dict(owner_match=None), dict(owner_match='display_name'), dict(owner_field_id=None)):
            with self.subTest(changed=changed), self.assertRaises(module.Unverified):
                module.checked_rule(dict(self.rule, **changed))
        self.rule['role'] = 'diagnostic'
        self.pin()
        result = self.verify()
        self.assertTrue(result['fixed_bot_owner_verified'], result)
        self.assertIsNone(result['source_ai_in_project_contract'])
        self.assertFalse(result['dispatch_checked'] or result['project_scope_verified'])

    def test_live_link_and_record_prove_only_the_project_condition(self):
        self.set_project_guard()
        result = self.verify()
        self.assertTrue(result['project_scope_verified'] and result['definition_matches'], result)
        self.action['type'] = 'FindRecordAction'
        self.rule = dict(
            purpose='project_scope',
            workflow_id='wkfExample',
            role='work',
            table_id='tblWork',
            guard=dict(kind='project_link', field_id='fldProject', value='recProject'),
        )
        self.trigger['data']['record_watch_info']['conditions'] = [
            module.condition('所属项目', 'is', [dict(value_type='text', value='recProject')])
        ]
        value = json.loads(self.registry.read_text())
        value['rules']['next'] = self.rule
        write_json(self.registry, value)
        result = self.verify()
        self.assertTrue(result['project_scope_verified'], result)
        self.assertFalse(result['definition_matches'] or result['updates_trigger_row_only'])
        self.assertEqual(
            (result['status'], result['check'], result['action_checked']),
            ('checked', 'trigger_project_scope', False),
        )

    def test_project_id_in_plain_text_wrong_table_or_wrong_row_is_not_project_proof(self):
        self.set_project_guard()
        self.mapped['project']['type'] = 'text'
        self.assertFalse(self.verify()['project_scope_verified'])
        self.mapped['project']['type'] = 'link'
        self.raw[3]['property']['table_id'] = 'tblOther'
        self.assertFalse(self.verify()['project_scope_verified'])
        self.raw[3]['property']['table_id'] = 'tblProjects'
        self.reader.read_record.return_value['record_id'] = 'recOther'
        self.assertFalse(self.verify()['project_scope_verified'])
        self.reader.read_record.return_value['record_id'] = 'recProject'
        self.reader.read_record.return_value['values']['fldNumber'] = 'PRJ-0002'
        self.assertFalse(self.verify()['project_scope_verified'])
        self.trigger['data']['record_watch_info']['conjunction'] = 'or'
        self.assertFalse(self.verify()['project_scope_verified'])

    def test_ordinary_select_is_not_a_native_stage(self):
        self.raw[1].update(type=3, ui_type='SingleSelect')
        self.assertFalse(self.verify()['definition_matches'])

    def test_diagnostic_rule_proves_shape_only(self):
        self.rule['role'] = 'diagnostic'
        self.pin()
        result = self.verify()
        self.assertTrue(result['definition_matches'])
        self.assertIsNone(result['source_ai_in_project_contract'])
        self.assertFalse(result['project_scope_verified'] or result['dispatch_checked'])

    def test_duplicate_field_or_table_name_or_modified_definition_fails(self):
        self.fields.append(dict(id="fldOther", name="触发标记"))
        self.assertFalse(self.verify()["definition_matches"])
        self.fields.pop()
        self.reader.base.pages.return_value.append(dict(id="tblOther", name="研发项"))
        self.assertFalse(self.verify()["definition_matches"])
        self.reader.base.pages.return_value.pop()
        self.trigger["data"]["trigger_control_list"] = ["openAPIBatchUpdate"]
        self.assertIn("固定", self.verify()["reason"])

    def test_rule_enabled_or_changed_during_readback_has_no_positive_result(self):
        original = self.read_only
        calls = 0

        def changed(argv, **kwargs):
            nonlocal calls
            if '+workflow-get' in argv:
                calls += 1
                if calls == 2:
                    self.definition['status'] = 'disabled'
            return original(argv, **kwargs)

        self.reader.runner.side_effect = changed
        result = self.verify()
        self.assertEqual(
            (result['status'], result['enabled'], result['definition_matches']), ('pending', None, False)
        )

    def test_bindings_and_private_registry_cannot_be_swapped(self):
        self.registry.chmod(0o644)
        self.assertFalse(self.verify()["definition_matches"])
        self.registry.chmod(0o600)
        value = json.loads(self.registry.read_text())
        value["service"] = "other"
        write_json(self.registry, value)
        self.assertFalse(self.verify()["definition_matches"])
        self.reader.runner.assert_not_called()
