import copy
from pathlib import Path
import tempfile
import unittest

from test_stage_business import Project, business, stage_action


class RequirementFilingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        p = self.project = Project(Path(directory.name), 'requirements')
        self.base = 'https://fixture.feishu.cn/base/FixtureBase'
        p.binding['base'] = {'url': self.base}
        self.label = '来源：请看 REQ-0012 的补充意见'
        self.old = f'[{self.label}](http://{self.label})'
        self.url = self.base + '?table=tblCurrent&record=recOrigin'
        p.row['details'] = self.old
        self.seal = dict(proof=p.proof(), anchor=copy.deepcopy(p.rows[0]), fields=self.values())
        self.target = {'record_id': 'recOrigin', 'values': {
            p.fields['number']['id']: 'REQ-0012', p.fields['project']['id']: [{'id': p.project}]}}
        p.reader.read_record.side_effect = lambda *args: self.target

    def values(self):
        p = self.project
        return business.protected_fields(p.role, p.fields, p.row)

    def file(self, key, value, native_type):
        p = self.project
        before = p.row[key]
        p.row[key] = value
        p.add_change(key, '' if before is None else before, value, '登记人')
        p.rows[0]['field_changes'][0]['field_type'] = native_type

    def check(self):
        p = self.project
        return business.record_history(p, p.role, 'recRow', self.seal, p.row, self.values())[1]

    def test_original_approved_values_survive_filing_and_source_repair(self):
        self.file('confirmation', '指定同事在该记录确认了这个版本。', 'Text')
        self.file('confirmed_revision', self.seal['proof']['business_revision'], 'Number')
        self.file('details', f'[{self.label}]({self.url})', 'Url')
        self.assertEqual(self.check(), self.seal['fields'])
        p = self.project
        p.reader.read_record.assert_called_once_with(
            p.token, p.table, 'recOrigin', [p.fields['number'], p.fields['project']])
        self.file('scope', '扩大这次已确认的范围。', 'Text')
        with self.assertRaises(stage_action.Pending):
            self.check()

    def test_later_edit_wrong_revision_and_mixed_action_still_invalidate(self):
        p = self.project
        self.file('confirmed_revision', 9, 'Number')
        with self.assertRaises(stage_action.Pending):
            self.check()
        p.rows = p.rows[-1:]
        p.row['confirmed_revision'] = None
        self.file('confirmation', '登记已核实的确认。', 'Text')
        p.rows[0]['field_changes'].append({'field_id': p.fields['next_step']['id']})
        with self.assertRaises(stage_action.Pending):
            self.check()
        p.rows[0]['field_changes'].pop()
        self.file('confirmation', '另一条说明。', 'Text')
        with self.assertRaises(stage_action.Pending):
            self.check()

    def test_link_must_preserve_caption_and_identify_original_same_project_row(self):
        p = self.project
        ambiguous = self.label + '以及 REQ-0034'
        usable = 'Source:person@example.org/path REQ-0012'
        for label, old, url in (
                (self.label + '（改）', self.old, self.url),
                (ambiguous, f'[{ambiguous}](http://{ambiguous})', self.url),
                (usable, f'[{usable}](http://{usable})', self.url),
                (self.label, f'[{self.label}](https://example.org/original)', self.url),
                (self.label, self.old, self.url.replace('fixture.feishu.cn', 'other.feishu.cn')),
                (self.label, self.old, self.url.replace('FixtureBase', 'OtherBase')),
                (self.label, self.old, self.url.replace('tblCurrent', 'tblOther')),
                (self.label, self.old, self.url.replace('recOrigin', 'recRow')),
                (self.label, self.old, self.url + '&record=recOther'),
                (self.label, self.old, self.url + '&next=https://example.org')):
            with self.subTest(label=label, old=old, url=url):
                p.rows = p.rows[-1:]
                p.row['details'] = old
                self.file('details', f'[{label}]({url})', 'Url')
                with self.assertRaises(stage_action.Pending):
                    self.check()
        p.rows = p.rows[-1:]
        p.row['details'] = self.old
        self.file('details', f'[{self.label}]({self.url})', 'Url')
        number = p.fields['number']['id']
        self.target['values'][number] = 'REQ-0050'
        with self.assertRaises(stage_action.Pending):
            self.check()
        self.target['values'][number] = 'REQ-0012'
        self.target['values'][p.fields['project']['id']].append({'id': 'recOtherProject'})
        with self.assertRaises(stage_action.Pending):
            self.check()
