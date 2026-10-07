import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from build import ROOT, build
from qx_rules import BuildError, FilterDocument, parse_filter, parse_rewrite, parse_sections, policies_in
from resources import download, publish


class BuilderTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.build/tests'
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ['profiles', 'templates', 'rules', 'licenses', 'overrides']:
            shutil.copytree(ROOT / directory, self.root / directory)
        (self.root / 'sources').mkdir()
        self.sources = [
            dict(id='test-filter', kind='filter', format='quantumultx', url='https://test.example/filter.list', policy='reject', license='licenses/GPL-2.0.txt'),
            dict(id='test-rewrite', kind='rewrite', format='quantumultx', url='https://test.example/rewrite.conf', scripts='mirror', requires=['test-filter'], license='licenses/GPL-2.0.txt')]
        self.save_sources()
        self.overrides()
        env = patch.dict(os.environ, {'GITHUB_REPOSITORY': ''})
        env.start()
        self.addCleanup(env.stop)

    def save_sources(self):
        self.write_yaml('sources/manifest.yaml', {'version': 1, 'sources': self.sources})

    def write_yaml(self, path, data):
        (self.root / path).write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')

    def overrides(self, filters=None, rewrites=None, hostnames=None):
        self.write_yaml('overrides/changes.yaml', dict(version=1, filters=filters or [], rewrites=rewrites or [], hostnames=hostnames or []))

    def fetch(self, url):
        if url.endswith('.list'):
            return b'# attribution\r\nHOST,ad.example,Original\r\nHOST,keep.example,Original\r\n'
        if url.endswith('.conf'):
            return 'hostname = api.example\n^https://api\\.example/a{1,2} url script-response-body https://test.example/code.js?mode=1#personal=yes\n'
        return b'const body = $response.body; $done({body});\n'

    def run_build(self, **options):
        options.setdefault('fetch', self.fetch)
        with contextlib.redirect_stdout(io.StringIO()):
            return build(self.root, **options)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for directory in ['dist', 'upstream'] for p in (self.root / directory).rglob('*') if p.is_file()}

    def test_native_resources_and_deterministic_offline_build(self):
        report = self.run_build()
        self.assertEqual(report['counts']['scripts'], 1)
        text = (self.root / 'dist/QuantumultX.conf').read_text(encoding='utf-8')
        self.assertNotIn('force-policy', text)
        self.assertIn('hostname = api.example', text)
        self.assertNotIn('tag=test-rewrite', (self.root / 'dist/QuantumultX_Basic.conf').read_text(encoding='utf-8'))
        before = self.snapshot()
        self.run_build(offline=True, check=True, fetch=lambda _: self.fail('offline requested network'))
        self.assertEqual(before, self.snapshot())

    def test_override_reapplies_and_raw_bytes_stay_unchanged(self):
        self.overrides(filters=[dict(id='fix', sources=['test-filter'], action='replace', match={'type': 'HOST', 'value': 'ad.example'}, rule='host, ad.example, direct')])
        self.run_build()
        self.assertEqual((self.root / 'upstream/filter/test-filter.list').read_bytes(), self.fetch('filter.list'))
        self.assertIn('host, ad.example, direct', (self.root / 'dist/rules/test-filter.list').read_text(encoding='utf-8'))
        self.run_build(offline=True, check=True)
        changes = json.loads((self.root / 'dist/changes.json').read_text(encoding='utf-8'))
        self.assertEqual(changes['overrides'][0]['matches'], 1)

    def test_missing_override_match_preserves_all_outputs(self):
        self.run_build()
        before = self.snapshot()
        self.overrides(filters=[dict(id='remove', sources=['test-filter'], action='remove', match={'value': 'missing.example'})])
        with self.assertRaisesRegex(BuildError, '实际 0'):
            self.run_build()
        self.assertEqual(before, self.snapshot())

    def test_remove_add_and_hostname_operations(self):
        self.overrides(filters=[dict(id='remove', sources=['test-filter'], action='remove', match={'value': 'ad.example'}), dict(id='add', sources=['test-filter'], action='add', rule='host, new.example, direct')], hostnames=[dict(id='remove-host', sources=['test-rewrite'], action='remove', value='api.example'), dict(id='add-host', sources=['test-rewrite'], action='add', value='new.example')])
        self.run_build()
        text = (self.root / 'dist/rules/test-filter.list').read_text(encoding='utf-8')
        self.assertNotIn('host, ad.example', text)
        self.assertIn('host, new.example, direct', text)
        self.assertIn('hostname = new.example', (self.root / 'dist/rewrites/test-rewrite.conf').read_text(encoding='utf-8'))

    def test_scripts_preserve_parameters_and_distinct_same_filenames(self):
        self.sources.append(dict(self.sources[1], id='second-rewrite', url='https://other.example/rewrite.conf'))
        self.save_sources()
        def fetch(url):
            return self.fetch(url).replace('test.example/code.js', 'other.example/code.js') if url == 'https://other.example/rewrite.conf' else self.fetch(url)
        self.run_build(fetch=fetch)
        deps = json.loads((self.root / 'dist/dependencies.json').read_text(encoding='utf-8'))
        self.assertEqual(len({d['path'] for d in deps}), 2)
        self.assertTrue(all(d['output'].endswith('#personal=yes') for d in deps))
        self.assertTrue(all('?mode=1' in d['source'] for d in deps))

    def test_replace_script_downloads_new_dependency(self):
        self.overrides(rewrites=[dict(id='new-script', sources=['test-rewrite'], action='replace-script', match={'action': 'script-response-body'}, script_url='https://new.example/code.js#setting=1')])
        calls = []
        def fetch(url):
            calls.append(url)
            return self.fetch(url)
        self.run_build(fetch=fetch)
        self.assertIn('https://new.example/code.js', calls)
        self.assertNotIn('https://test.example/code.js?mode=1', calls)
        self.assertIn('#setting=1', (self.root / 'dist/rewrites/test-rewrite.conf').read_text(encoding='utf-8'))

    def test_script_failure_cannot_publish_partial_update(self):
        self.run_build()
        before = self.snapshot()
        def fetch(url):
            if '.js' in url:
                raise requests.Timeout('script unavailable')
            return self.fetch(url)
        with self.assertRaises(BuildError):
            self.run_build(fetch=fetch)
        self.assertEqual(before, self.snapshot())

    def test_upstream_missing_script_patch_excludes_only_eight_references(self):
        change = yaml.safe_load((ROOT / 'overrides/changes.yaml').read_text(encoding='utf-8'))['rewrites'][0]
        change = dict(change, sources=['test-rewrite'])
        self.overrides(rewrites=[change])
        def fetch(url):
            if url.endswith('.conf'):
                bad = '\n'.join(f'^https://api.example/bad{i} url script-response-body {change["match"]["script_url"]}' for i in range(8))
                return self.fetch(url) + bad + '\n'
            self.assertNotEqual(url, change['match']['script_url'])
            return self.fetch(url)
        report = self.run_build(fetch=fetch)
        self.assertEqual(report['counts']['rewrites'], 1)
        self.assertEqual(report['counts']['scripts'], 1)
        self.assertIn('smzdm_remove_ads.js', (self.root / 'upstream/rewrite/test-rewrite.conf').read_text(encoding='utf-8'))
        self.assertNotIn('smzdm_remove_ads.js', (self.root / 'dist/rewrites/test-rewrite.conf').read_text(encoding='utf-8'))

    def test_explicit_stale_mode_and_corrupt_cache(self):
        self.run_build()
        def fail(_):
            raise requests.Timeout('unavailable')
        report = self.run_build(strict=False, fetch=fail)
        self.assertTrue(all(item['state'] == 'cached' for item in report['sources']))
        (self.root / 'upstream/filter/test-filter.list').write_text('HOST,tampered.example,reject')
        with self.assertRaises(BuildError):
            self.run_build(strict=False, fetch=fail)

    def test_bad_first_response_writes_no_outputs(self):
        with self.assertRaises(BuildError):
            self.run_build(fetch=lambda _: '<html>error</html>')
        self.assertEqual(self.snapshot(), {})

    def test_changed_url_cannot_reuse_cache(self):
        self.run_build()
        self.sources[0]['url'] = 'https://new.example/filter.list'
        self.save_sources()
        with self.assertRaises(BuildError):
            self.run_build(offline=True)

    def test_large_rule_drop_is_rejected(self):
        self.run_build(fetch=lambda url: '\n'.join(f'HOST,ad{i}.example,Original' for i in range(10)) if url.endswith('.list') else self.fetch(url))
        with self.assertRaisesRegex(BuildError, '30%'):
            self.run_build()

    def test_custom_exact_matches_exclude_upstream_only(self):
        (self.root / 'rules/direct.list').write_text('host, ad.example, direct\n')
        self.run_build(fetch=lambda url: self.fetch(url) + b'HOST-SUFFIX,ad.example,Original\n' if url.endswith('.list') else self.fetch(url))
        text = (self.root / 'dist/rules/test-filter.list').read_text(encoding='utf-8')
        self.assertNotIn('host, ad.example', text)
        self.assertIn('host-suffix, ad.example, reject', text)
        self.assertEqual(len(json.loads((self.root / 'dist/changes.json').read_text(encoding='utf-8'))['custom_shadows']), 1)

    def test_conflicting_custom_rules_fail(self):
        (self.root / 'rules/reject.list').write_text('host-suffix, github.com, reject\n')
        with self.assertRaisesRegex(BuildError, '自维护规则相互冲突'):
            self.run_build()

    def test_requires_disabled_source_fail(self):
        self.sources[0]['enabled'] = False
        self.save_sources()
        with self.assertRaisesRegex(BuildError, '配套来源'):
            self.run_build()

    def test_disabled_rewrite_prunes_owned_outputs(self):
        self.run_build()
        script = next((self.root / 'dist/scripts').glob('*.js'))
        self.sources[1]['enabled'] = False
        self.save_sources()
        self.run_build(offline=True)
        self.assertFalse(script.exists())
        self.assertFalse((self.root / 'upstream/rewrite/test-rewrite.conf').exists())
        self.assertTrue((self.root / 'upstream/filter/test-filter.list').exists())

    def test_external_scripts_are_reported(self):
        self.sources[1]['scripts'] = 'external'
        self.save_sources()
        report = self.run_build()
        self.assertEqual(report['counts']['scripts'], 0)
        self.assertTrue(report['warnings'])

    def test_custom_hostname_sources_merge(self):
        (self.root / 'rules/rewrite.list').write_text('hostname = one.example\n')
        (self.root / 'rules/mitm-hosts.list').write_text('two.example\n')
        self.run_build()
        self.assertIn('hostname = api.example, one.example, two.example', (self.root / 'dist/QuantumultX.conf').read_text(encoding='utf-8'))

    def test_native_only_and_parser_parameters_rejected(self):
        for update in [{'format': 'surge'}, {'url': 'https://test.example/filter.list#parser=custom'}]:
            with self.subTest(update=update):
                self.sources[0].update(update)
                self.save_sources()
                with self.assertRaises(BuildError):
                    self.run_build()
                self.sources[0].update(format='quantumultx', url='https://test.example/filter.list')

    def test_final_private_nodes_unknown_policy_fail(self):
        path = self.root / 'templates/QuantumultX.conf'
        original = path.read_text(encoding='utf-8')
        for text in [original.replace('final, 兜底分流', ''), original.replace('[server_remote]', '[server_remote]\nhttps://private.example/token'), original.replace('static=AI服务, 节点选择', 'static=AI服务, 不存在')]:
            with self.subTest(text=text[:30]):
                path.write_text(text, encoding='utf-8')
                with self.assertRaises(BuildError):
                    self.run_build()
                self.assertEqual(self.snapshot(), {})


class SyntaxAndPublishTests(unittest.TestCase):
    def test_partial_http_response_is_retried_from_start(self):
        broken, good, client = Mock(), Mock(), Mock()
        broken.iter_content.side_effect = requests.exceptions.ChunkedEncodingError('partial body')
        good.iter_content.return_value = iter([b'complete', b' response'])
        client.get.side_effect = [contextlib.nullcontext(broken), contextlib.nullcontext(good)]
        self.assertEqual(download(client, 'https://test.example/code.js#setting=1'), b'complete response')
        self.assertEqual(client.get.call_count, 2)
        self.assertEqual(client.get.call_args.args[0], 'https://test.example/code.js')

    def test_network_options_are_preserved(self):
        rule = parse_filter('HOST,a.example,proxy,via-interface=utun0')
        self.assertIn('via-interface=utun0', rule.render())
        with self.assertRaises(BuildError):
            parse_filter('HOST,a.example,proxy,no-resolve')

    def test_regex_comma_is_not_csv(self):
        rule = parse_rewrite('^https://a.example/a{1,2} url reject')
        self.assertEqual(rule.pattern, '^https://a.example/a{1,2}')

    def test_policy_cycle_is_rejected(self):
        with self.assertRaisesRegex(BuildError, '循环'):
            policies_in('[policy]\nstatic=A, B\nstatic=B, A')

    def test_dedup_preserves_distinct_network_options(self):
        doc = FilterDocument.parse('host,a.example,direct\nhost,a.example,direct\nhost,a.example,direct,force-cellular')
        self.assertEqual(doc.validate({'direct'}), 1)
        self.assertEqual(len(doc.rules), 2)

    def test_io_failure_rolls_back_replacements(self):
        parent = ROOT / '.build/tests'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            root = Path(temporary)
            (root / 'dist').mkdir()
            (root / 'dist/a').write_bytes(b'old')
            original = os.replace
            calls = []
            def fail_second(source, destination):
                calls.append(destination)
                if len(calls) == 2:
                    raise OSError('disk failure')
                original(source, destination)
            with patch('resources.os.replace', side_effect=fail_second), self.assertRaises(OSError):
                publish(root, {'dist/a': b'new', 'upstream/b': b'new'})
            self.assertEqual((root / 'dist/a').read_bytes(), b'old')
            self.assertFalse((root / 'upstream/b').exists())


if __name__ == '__main__':
    unittest.main()
