"""Sync native QX sources, replay personal changes, and build reviewable subscriptions."""
from __future__ import annotations
import argparse
from dataclasses import replace
import html
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urldefrag, quote, urlsplit
import yaml
from overrides import apply_changes, check_keys
from qx_rules import (BuildError, FilterDocument, FilterRule, RewriteDocument, RewriteRule,
                      is_comment, parse_hosts, policies_in, policy_name, public_url, validate_profile)
from resources import SourceStore, http_session, download, json_bytes, local_path, publish, sha256, text_content

ROOT = Path(__file__).resolve().parents[1]


def yaml_file(root, name):
    return yaml.safe_load(local_path(root, name).read_text(encoding="utf-8"))


def script_validator(text):
    if text.lstrip().startswith(("[", "{")) or not re.search(r"\b(?:const|let|var|function|class)\b|\$(?:done|task|request|response|prefs)\b", text):
        raise BuildError("无法确认资源为 JavaScript")
    return 0


def report_html(manifest, changes):
    esc = lambda value: html.escape(str(value), quote=True)
    rows = []
    for identifier, resource in sorted(manifest["resources"].items()):
        source = resource["source"]
        link = f'<a href="{esc(source)}">来源</a>' if source.startswith("https://") else esc(source)
        rows.append(f'<tr><td>{esc(identifier)}</td><td>{esc(resource["kind"])}</td><td>{resource["rules"]}</td><td>{link}</td><td>{esc(resource["sha256"][:16])}</td></tr>')
    operations = []
    for operation in changes["overrides"]:
        details = "\n".join(f'{item["source"]}: {item["before"] or "（新增）"} → {item["after"] or "（删除）"}' for item in operation["changes"])
        operations.append(f'<details><summary>{esc(operation["id"])} · {esc(operation["action"])} · 命中 {operation["matches"]}</summary><pre>{esc(details)}</pre></details>')
    warnings = "".join(f'<li>{esc(value)}</li>' for value in changes["warnings"])
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quantumult X 构建报告</title><style>
body{{font:16px/1.7 system-ui,sans-serif;max-width:1080px;margin:32px auto;padding:0 20px;color:#182333;background:#f5f7fa}}
table{{border-collapse:collapse;width:100%;background:white}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #dce1e8}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:14px}}a{{color:#145bb3}}summary{{cursor:pointer}}input{{font:inherit;padding:8px;width:320px;max-width:90%}}
</style><h1>Quantumult X 构建报告</h1>
<p>分流 {manifest["counts"]["filters"]:,} 条 · 重写 {manifest["counts"]["rewrites"]} 条 · 脚本 {manifest["counts"]["scripts"]} 个。分类间可能存在重复。</p>
<p>生成与依赖检查不代表手机端效果。来源和哈希见 <a href="manifest.json">manifest.json</a>。</p>
<h2>来源与产物</h2><input id="search" placeholder="搜索来源或分类"><table><thead><tr><th>ID</th><th>类型</th><th>规则数</th><th>来源</th><th>SHA256 前缀</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
<h2>个人修改</h2>{"".join(operations) or '<p>当前没有启用来源修改操作。</p>'}
<p>自维护规则排除了上游相同类型、匹配内容、网络选项的规则：{len(changes["custom_shadows"])} 条。</p>
<h2>需检查的信息</h2><ul>{warnings or '<li>未发现额外警告。</li>'}</ul>
<p>跨来源相同匹配指向不同策略：{changes["conflicts"]["count"]} 处。不猜测客户端实际命中结果。</p>
<p><a href="changes.json">完整修改记录</a> · <a href="QuantumultX.conf">含重写配置</a> · <a href="QuantumultX_Basic.conf">基础分流配置</a></p>
<script>document.querySelector('#search').addEventListener('input',e=>{{let q=e.target.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))}})</script></html>'''


def build(root=ROOT, offline=False, strict=True, check=False, fetch=None):
    root = Path(root).resolve()
    config = yaml_file(root, "profiles/config.yaml")
    check_keys(config, {"version", "repository", "branch", "template", "sources", "overrides", "policy_map", "custom", "rewrite_local", "mitm_hosts"}, "profile")
    if config.get("version") != 2:
        raise BuildError("profile.version 必须为 2")
    repository = os.environ.get("GITHUB_REPOSITORY") or config["repository"]
    branch = config.get("branch", "main")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[A-Za-z0-9_.-]+", branch):
        raise BuildError("repository 必须为 owner/repo，branch 必须为有效名称")
    prefix = public_url(f"https://raw.githubusercontent.com/{repository}/{branch}/dist")
    template = local_path(root, config["template"]).read_text(encoding="utf-8")
    known, mapping = policies_in(template), config.get("policy_map", {})
    source_config = yaml_file(root, config["sources"])
    check_keys(source_config, {"version", "sources"}, "sources")
    if source_config.get("version") != 1 or not isinstance(source_config.get("sources"), list):
        raise BuildError("sources 必须包含 version: 1 与 sources 列表")
    writes, filters, rewrites, definitions, custom_ids, resources, warnings = {}, {}, {}, {}, [], {}, []
    def register(identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", identifier) or identifier in definitions or identifier.startswith("script-"):
            raise BuildError(f"来源 ID 无效、重复或使用保留前缀: {identifier}")
    for item in config.get("custom", []):
        check_keys(item, {"id", "file"}, "custom")
        identifier = item["id"]
        register(identifier)
        definitions[identifier] = {"kind": "filter", "source": item["file"], "custom": True}
        document = FilterDocument.parse(local_path(root, item["file"]).read_text(encoding="utf-8"), mapping=mapping, allowed=known)
        document.validate(known, allow_empty=True)
        filters[identifier] = document
        custom_ids.append(identifier)
    with http_session() as client:
        store = SourceStore(root, fetch or (lambda url: download(client, url)), offline, strict)
        for item in source_config["sources"]:
            check_keys(item, {"id", "kind", "format", "url", "policy", "enabled", "license", "requires", "scripts"}, "source")
            if not isinstance(item.get("enabled", True), bool):
                raise BuildError("source.enabled 必须为布尔值")
            if not item.get("enabled", True):
                continue
            identifier = item["id"]
            register(identifier)
            kind, url = item["kind"], public_url(item["url"])
            if kind not in {"filter", "rewrite"} or item.get("format") != "quantumultx":
                raise BuildError(f"{identifier}: 仅支持明确声明的原生 quantumultx 分流/重写")
            if urlsplit(url).fragment:
                raise BuildError("原生资源含客户端解析参数；后端不能默默忽略 # 参数，请提供已转换的 QX 资源")
            if not item.get("license") or not local_path(root, item["license"]).is_file():
                raise BuildError(f"{identifier}: 缺少来源许可文件")
            definitions[identifier] = {**item, "source": url, "custom": False}
            if kind == "filter":
                policy = policy_name(item["policy"], mapping)
                if policy not in known:
                    raise BuildError(f"{identifier}: 策略不存在: {policy}")
                def validate(text, policy=policy):
                    doc = FilterDocument.parse(text, forced_policy=policy)
                    doc.validate(known)
                    return len(doc.rules)
                content = store.get(identifier, url, kind, validate)
                filters[identifier] = FilterDocument.parse(text_content(content), forced_policy=policy)
                filters[identifier].validate(known)
            else:
                if item.get("scripts", "mirror") not in {"mirror", "external"}:
                    raise BuildError("source.scripts 只能为 mirror 或 external")
                content = store.get(identifier, url, kind, lambda text: len(RewriteDocument.parse(text).rules))
                rewrites[identifier] = RewriteDocument.parse(text_content(content))
        for identifier, item in definitions.items():
            if not isinstance(item.get("requires", []), list):
                raise BuildError("source.requires 必须为来源 ID 列表")
            for dependency in item.get("requires", []):
                if dependency not in definitions:
                    raise BuildError(f"{identifier}: 缺少配套来源 {dependency}")
        if "custom-rewrite" in definitions:
            raise BuildError("custom-rewrite 为保留 ID")
        rewrites["custom-rewrite"] = RewriteDocument.parse(local_path(root, config["rewrite_local"]).read_text(encoding="utf-8"), allow_empty=True)
        extra_hosts = parse_hosts(",".join(line for line in local_path(root, config["mitm_hosts"]).read_text(encoding="utf-8").splitlines() if not is_comment(line)))
        rewrites["custom-rewrite"].hosts = list(dict.fromkeys([*rewrites["custom-rewrite"].hosts, *extra_hosts]))
        definitions["custom-rewrite"] = {"kind": "rewrite", "source": config["rewrite_local"], "custom": True, "scripts": "external"}
        ledger = apply_changes(yaml_file(root, config["overrides"]), filters, rewrites, known, mapping)
        custom_rules, shadows = {}, []
        for identifier in custom_ids:
            filters[identifier].validate(known, allow_empty=True)
            for rule in filters[identifier].rules:
                key = (*rule.key, rule.options)
                if key in custom_rules and custom_rules[key].policy != rule.policy:
                    raise BuildError(f"自维护规则相互冲突: {rule.render()}")
                custom_rules[key] = rule
        for identifier, document in filters.items():
            if identifier not in custom_ids:
                lines = []
                for line in document.lines:
                    if isinstance(line, FilterRule) and (*line.key, line.options) in custom_rules:
                        shadows.append({"source": identifier, "before": line.render(), "custom": custom_rules[(*line.key, line.options)].render()})
                    else:
                        lines.append(line)
                document.lines = lines
            document.validate(known, allow_empty=True)
        dependencies, script_ids = [], set()
        for identifier, document in rewrites.items():
            mode = definitions[identifier].get("scripts", "mirror")
            for index, rule in enumerate(document.lines):
                if not isinstance(rule, RewriteRule) or not rule.script_url:
                    continue
                original = rule.script_url
                base, fragment = urldefrag(original)
                dependency = {"resource": identifier, "source": original, "mode": mode}
                if mode == "mirror":
                    script_id = "script-" + sha256(base.encode())[:20]
                    content = store.get(script_id, base, "script", script_validator)
                    path = f"dist/scripts/{script_id}.js"
                    writes[path] = content
                    script_ids.add(script_id)
                    mirrored = f"{prefix}/scripts/{script_id}.js" + ("#" + fragment if fragment else "")
                    document.lines[index] = replace(rule, argument=mirrored)
                    dependency.update(path=path, sha256=sha256(content), output=mirrored)
                    dependency["embedded_urls"] = sorted(set(re.findall(r"https?://[^\s\"'`<>\\]+", text_content(content))))
                else:
                    dependency["output"] = original
                dependencies.append(dependency)
        for item in dependencies:
            if item["mode"] == "external":
                warnings.append(f'{item["resource"]}: 脚本保留外部引用 {item["source"]}')
            elif item["embedded_urls"]:
                warnings.append(f'{item["resource"]}: 脚本内含 {len(item["embedded_urls"])} 个 URL 文本；需审查 dependencies.json，不宣称全部离线')
    seen, conflicts, conflict_count = {}, [], 0
    for identifier, document in filters.items():
        for rule in document.rules:
            key = (*rule.key, rule.options)
            if key in seen and seen[key][1] != rule.policy:
                conflict_count += 1
                if len(conflicts) < 200:
                    conflicts.append({"match": [rule.kind, rule.target], "first_source": seen[key][0], "first_policy": seen[key][1], "source": identifier, "policy": rule.policy})
            else:
                seen[key] = identifier, rule.policy
    if conflict_count:
        warnings.append(f"跨来源存在 {conflict_count} 个相同匹配的不同策略；未自动按排序选取胜者")
    filter_refs, rewrite_refs, hostname_union = [], [], []
    def header(identifier):
        return f'# Generated by {repository}; personal overrides applied.\n# Source: {definitions[identifier]["source"]}\n# Terms and changes: ../../THIRD_PARTY.md and ../changes.json\n'
    def reference(path, identifier):
        # No force-policy, so individual personal policy changes are respected.
        return f"{prefix}/{path}, tag={identifier}, update-interval=86400, opt-parser=false, enabled=true"
    for identifier, document in filters.items():
        path = f"dist/rules/{identifier}.list"
        content = (header(identifier) + document.render()).encode("utf-8")
        writes[path] = content
        resources[identifier] = {"kind": "filter", "source": definitions[identifier]["source"], "path": path, "rules": len(document.rules), "sha256": sha256(content)}
        if document.rules:
            filter_refs.append(reference(f"rules/{identifier}.list", identifier))
    for identifier, document in rewrites.items():
        path = f"dist/rewrites/{identifier}.conf"
        content = (header(identifier) + document.render()).encode("utf-8")
        writes[path] = content
        resources[identifier] = {"kind": "rewrite", "source": definitions[identifier]["source"], "path": path, "rules": len(document.rules), "sha256": sha256(content), "hostnames": document.hosts}
        hostname_union.extend(document.hosts)
        if identifier != "custom-rewrite" and document.rules:
            rewrite_refs.append(reference(f"rewrites/{identifier}.conf", identifier))
    for identifier in sorted(script_ids):
        path = f"dist/scripts/{identifier}.js"
        resources[identifier] = {"kind": "script", "source": store.lock[identifier]["url"], "path": path, "rules": 0, "sha256": sha256(writes[path])}
    hosts = list(dict.fromkeys(hostname_union))
    local_rewrite = "\n".join(rule.render() for rule in rewrites["custom-rewrite"].rules)
    for filename, full in [("QuantumultX.conf", True), ("QuantumultX_Basic.conf", False)]:
        values = {"FILTER_REMOTE": "\n".join(filter_refs), "REWRITE_REMOTE": "\n".join(rewrite_refs) if full else "", "REWRITE_LOCAL": local_rewrite if full else "", "MITM_HOSTNAME": "hostname = " + ", ".join(hosts) if full and hosts else "# hostname 未配置"}
        output = template
        for key, value in values.items():
            placeholder = "{{" + key + "}}"
            if output.count(placeholder) != 1:
                raise BuildError(f"模板必须包含一个 {key} 占位符")
            output = output.replace(placeholder, value)
        validate_profile(output, known)
        writes[f"dist/{filename}"] = output.encode("utf-8")
    bundle = {"filter_remote": filter_refs, "rewrite_remote": rewrite_refs}
    writes["dist/resources.json"] = json_bytes(bundle)
    add_url = "quantumult-x:///add-resource?remote-resource=" + quote(json_bytes(bundle).decode(), safe="")
    writes["dist/add-resources.url"] = (add_url + "\n").encode()
    previous_path = root / "dist/manifest.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
    delta = []
    for identifier, resource in resources.items():
        old = previous.get("resources", {}).get(identifier)
        if old and resource["kind"] in {"filter", "rewrite"}:
            before_path = local_path(root, old["path"])
            if before_path.exists():
                before = {line for line in before_path.read_text(encoding="utf-8").splitlines() if not is_comment(line)}
                after = {line for line in writes[resource["path"]].decode().splitlines() if not is_comment(line)}
                if before != after:
                    delta.append({"id": identifier, "added": len(after - before), "removed": len(before - after), "added_sample": sorted(after - before)[:30], "removed_sample": sorted(before - after)[:30]})
    changes = {"version": 1, "overrides": ledger, "custom_shadows": shadows, "warnings": list(dict.fromkeys(warnings)), "conflicts": {"count": conflict_count, "sample": conflicts}}
    manifest = {"version": 2, "repository": repository, "resources": resources, "counts": {"filters": sum(len(doc.rules) for doc in filters.values()), "rewrites": sum(len(doc.rules) for doc in rewrites.values()), "scripts": len(script_ids)}, "validation": {"native_format": True, "override_expectations": True, "device_tested": False}}
    writes.update({"upstream/lock.json": json_bytes(store.lock), "dist/dependencies.json": json_bytes(dependencies), "dist/changes.json": json_bytes(changes), "dist/manifest.json": json_bytes(manifest), "dist/report.html": report_html(manifest, changes).encode("utf-8")})
    writes.update(store.writes)
    for path in sorted({item["license"] for item in definitions.values() if item.get("license")}):
        writes["dist/licenses/" + Path(path).name] = local_path(root, path).read_bytes()
    writes["dist/artifacts.json"] = json_bytes(sorted([*writes, "dist/artifacts.json"]))
    inventory = root / "dist/artifacts.json"
    obsolete = set(json.loads(inventory.read_text(encoding="utf-8"))) - set(writes) if inventory.exists() else set()
    changed = publish(root, writes, sorted(obsolete), check)
    report = {"changed_files": changed, "counts": manifest["counts"], "sources": store.statuses, "changes_since_previous_build": delta, "warnings": changes["warnings"], "device_tested": False}
    if not check:
        (root / ".build").mkdir(exist_ok=True)
        (root / ".build/report.json").write_bytes(json_bytes(report))
    print(json.dumps({"changed_files": len(changed), "counts": manifest["counts"], "warnings": len(changes["warnings"]), "override_operations": len(ledger)}, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="只使用来源和哈希匹配的快照")
    parser.add_argument("--strict", action="store_true", help="严格更新（默认）；任一失败停止生成")
    parser.add_argument("--allow-stale", action="store_true", help="联网失败时允许使用已校验旧快照")
    parser.add_argument("--check", action="store_true", help="检查一致性，不写文件")
    args = parser.parse_args()
    if args.strict and args.allow_stale:
        parser.error("--strict 与 --allow-stale 不能同时使用")
    try:
        build(offline=args.offline, strict=not args.allow_stale, check=args.check)
    except (BuildError, OSError, KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        if not args.check:
            (ROOT / ".build").mkdir(exist_ok=True)
            (ROOT / ".build/failure.json").write_bytes(json_bytes({"error": str(exc), "published": False}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
