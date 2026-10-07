import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from build import ROOT, BuildError, build, normalize_rules, parse_sections, policies_in, validate_profile


class BuilderTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / ".build/tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for directory in ["profiles", "templates", "rules"]:
            shutil.copytree(ROOT / directory, self.root / directory)
        self.environment = patch.dict(os.environ, {"GITHUB_REPOSITORY": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def run_build(self, **options):
        options.setdefault("fetch", lambda url: "# upstream attribution\nHOST-SUFFIX,example.com,Upstream\n")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return build(self.root, **options)

    def config(self, mutate):
        path = self.root / "profiles/config.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def test_order_mapping_and_stable_offline_build(self):
        self.run_build()
        text = (self.root / "dist/QuantumultX.conf").read_text(encoding="utf-8")
        refs = parse_sections(text)["filter_remote"]
        self.assertIn("custom-direct", refs[0])
        self.assertIn("custom-proxy", refs[1])
        self.assertIn("direct-upstream", refs[3])
        self.assertIn("advertising", refs[4])
        self.assertIn("github.com, 节点选择", (self.root / "dist/rules/custom-proxy.list").read_text(encoding="utf-8"))
        self.run_build(offline=True, check=True, fetch=lambda url: self.fail("offline must not fetch"))

    def test_strict_failure_preserves_all_published_files(self):
        self.run_build()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and ".build" not in p.relative_to(self.root).parts}
        calls = []
        def fetch(url):
            calls.append(url)
            if len(calls) == 2:
                raise requests.Timeout("simulated failure")
            return "HOST,new.example,Upstream\n"
        with self.assertRaises(BuildError):
            self.run_build(strict=True, fetch=fetch)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and ".build" not in p.relative_to(self.root).parts}
        self.assertEqual(before, after)

    def test_download_failure_uses_verified_snapshot(self):
        self.run_build()
        before = (self.root / "dist/QuantumultX.conf").read_bytes()
        def fail(url):
            raise requests.Timeout("simulated failure")
        self.run_build(fetch=fail)
        self.assertEqual(before, (self.root / "dist/QuantumultX.conf").read_bytes())
        report = json.loads((self.root / ".build/report.json").read_text(encoding="utf-8"))
        self.assertTrue(all(item["state"] == "cached" for item in report))

    def test_first_build_failure_writes_no_profile(self):
        with self.assertRaises(BuildError):
            self.run_build(fetch=lambda url: "<html>service unavailable</html>")
        self.assertFalse((self.root / "dist").exists())

    def test_changed_url_cannot_reuse_old_cache(self):
        self.run_build()
        self.config(lambda data: data["sources"][0].update(url="https://example.com/other.list"))
        with self.assertRaises(BuildError):
            self.run_build(offline=True)

    def test_corrupted_cache_is_rejected(self):
        self.run_build()
        (self.root / "vendor/filters/direct-upstream.list").write_text("HOST,tampered.example,direct\n")
        with self.assertRaises(BuildError):
            self.run_build(offline=True)

    def test_same_remote_filename_uses_distinct_ids(self):
        self.config(lambda data: data.update(sources=[
            {"id": "source-a", "url": "https://a.example/common.list", "policy": "direct"},
            {"id": "source-b", "url": "https://b.example/common.list", "policy": "reject"}]))
        self.run_build(fetch=lambda url: f"HOST,{url.split('/')[2]},Original\n")
        self.assertIn("a.example", (self.root / "vendor/filters/source-a.list").read_text())
        self.assertIn("b.example", (self.root / "vendor/filters/source-b.list").read_text())

    def test_large_rule_drop_blocks_publication(self):
        self.run_build(fetch=lambda url: "\n".join(f"HOST,host{i}.example,Original" for i in range(10)))
        with self.assertRaises(BuildError):
            self.run_build(strict=True)

    def test_conflicting_custom_rules_fail(self):
        (self.root / "rules/reject.list").write_text("host-suffix,github.com,reject\n")
        with self.assertRaises(BuildError):
            self.run_build()

    def test_unknown_policy_fails(self):
        self.config(lambda data: data["sources"][0].update(policy="missing-policy"))
        with self.assertRaises(BuildError):
            self.run_build()

    def test_invalid_template_cannot_replace_profile(self):
        self.run_build()
        path = self.root / "templates/QuantumultX.conf"
        path.write_text(path.read_text(encoding="utf-8").replace("final, 兜底分流", ""), encoding="utf-8")
        before = (self.root / "dist/QuantumultX.conf").read_bytes()
        with self.assertRaises(BuildError):
            self.run_build(offline=True)
        self.assertEqual(before, (self.root / "dist/QuantumultX.conf").read_bytes())

    def test_missing_local_file_fails(self):
        self.config(lambda data: data["custom"][0].update(file="rules/not-found.list"))
        with self.assertRaises(FileNotFoundError):
            self.run_build()

    def test_private_nodes_are_not_publishable(self):
        template = (self.root / "templates/QuantumultX.conf").read_text(encoding="utf-8")
        template = template.replace("[server_remote]", "[server_remote]\nhttps://private.example/sub?token=secret")
        (self.root / "templates/QuantumultX.conf").write_text(template, encoding="utf-8")
        with self.assertRaises(BuildError):
            self.run_build()

    def test_empty_repository_env_falls_back_to_config(self):
        self.run_build()
        self.assertIn("Ethereal-09/quantumultx-rules/main/dist", (self.root / "dist/QuantumultX.conf").read_text(encoding="utf-8"))

    def test_empty_custom_resource_is_not_referenced(self):
        (self.root / "rules/reject.list").write_text("# intentionally empty\n")
        self.run_build()
        self.assertNotIn("tag=custom-reject", (self.root / "dist/QuantumultX.conf").read_text(encoding="utf-8"))

    def test_policy_mapping_handles_spaces_and_line_end(self):
        text, count = normalize_rules("HOST,a.example,proxy\nhost, b.example, proxy\nhost,a.example,proxy", mapping={"proxy": "Group"}, allowed={"Group"})
        self.assertEqual(count, 2)
        self.assertEqual(active_lines(text), ["host, a.example, Group", "host, b.example, Group"])


def active_lines(text):
    return [line for line in text.splitlines() if line and not line.startswith("#")]


if __name__ == "__main__":
    unittest.main()
