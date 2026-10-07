"""Build public Quantumult X profiles and rule subscriptions; no credentials needed."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
BUILTINS = {"direct", "proxy", "reject", "reject-tinygif", "reject-dict", "reject-array", "reject-200"}
TYPES = {"host", "host-suffix", "host-keyword", "host-wildcard", "ip-cidr", "ip6-cidr", "ip-asn", "geoip", "user-agent"}


class BuildError(ValueError):
    pass


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def active(text):
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", ";", "//"))]


def local_path(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise BuildError(f"路径超出项目目录: {value}")
    return path


def public_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BuildError(f"需要 HTTPS 公开资源地址: {value}")
    if parsed.query or parsed.fragment or any(c.isspace() for c in value) or "," in value:
        raise BuildError("公开资源地址不允许查询参数、片段、逗号或空白字符")
    return value


def normalize_rules(text, policy=None, mapping=None, allowed=None, allow_empty=False):
    """Preserve first occurrence and reject conflicting destinations within a resource."""
    comments, rules, seen = [], [], {}
    for number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line:
            continue
        if line.startswith(("#", ";", "//")):
            comments.append(line)
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) not in (3, 4) or parts[0].lower() not in TYPES or not all(parts):
            raise BuildError(f"第 {number} 行不是支持的 QX 分流规则: {line[:120]}")
        kind, target = parts[0].lower(), parts[1]
        if len(parts) == 4 and (parts[3].lower() != "no-resolve" or kind not in {"ip-cidr", "ip6-cidr", "ip-asn"}):
            raise BuildError(f"第 {number} 行包含不支持的规则选项")
        if kind in {"ip-cidr", "ip6-cidr"}:
            try:
                network = ipaddress.ip_network(target, strict=False)
                if network.version != (6 if kind == "ip6-cidr" else 4):
                    raise ValueError("IP family mismatch")
            except ValueError as exc:
                raise BuildError(f"第 {number} 行 IP 网段无效: {target}") from exc
        elif kind.startswith("host"):
            if re.search(r"[\s/<>=]", target) or (kind in {"host", "host-suffix"} and "*" in target):
                raise BuildError(f"第 {number} 行域名无效: {target}")
            target = target.lower()
        resolved = policy or (mapping or {}).get(parts[2], parts[2])
        if allowed is not None and resolved not in allowed:
            raise BuildError(f"第 {number} 行引用不存在的策略: {resolved}")
        key = (kind, target, tuple(parts[3:]))
        if key in seen:
            if seen[key] != resolved:
                raise BuildError(f"同一匹配存在冲突策略: {kind},{target}")
            continue
        seen[key] = resolved
        rules.append(", ".join([kind, target, resolved] + parts[3:]))
    if not rules and not allow_empty:
        raise BuildError("规则内容为空，或返回的不是规则文件")
    return "\n".join(comments + rules) + "\n", len(rules)


def parse_sections(text):
    sections, current = {}, None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            if current in sections:
                raise BuildError(f"重复配置段: {current}")
            sections[current] = []
        elif current and stripped and not stripped.startswith(("#", ";", "//")):
            sections[current].append(stripped)
    return sections


def policies_in(template):
    known, candidates = set(BUILTINS), []
    for line in parse_sections(template).get("policy", []):
        if "=" not in line:
            raise BuildError("策略组缺少等号")
        kind, rest = line.split("=", 1)
        if kind.strip() not in {"static", "available", "round-robin", "dest-hash", "url-latency-benchmark", "ssid"}:
            raise BuildError(f"不支持的策略组类型: {kind}")
        parts = [x.strip() for x in rest.split(",")]
        if not parts[0] or parts[0] in known:
            raise BuildError(f"策略组名称重复或无效: {parts[0]}")
        known.add(parts[0])
        candidates.extend(x for x in parts[1:] if "=" not in x)
    for name in candidates:
        if name not in known:
            raise BuildError(f"策略组引用不存在的策略: {name}")
    return known


def validate_profile(text, known):
    if "{{" in text or "}}" in text:
        raise BuildError("模板占位符未完全替换")
    sections = parse_sections(text)
    for section in ("general", "dns", "policy", "filter_local", "filter_remote"):
        if not sections.get(section):
            raise BuildError(f"配置缺少有效内容: [{section}]")
    filters = sections["filter_local"]
    finals = [i for i, line in enumerate(filters) if line.split(",")[0].strip().lower() == "final"]
    if finals != [len(filters) - 1] or filters[-1].split(",")[-1].strip() not in known:
        raise BuildError("必须且只能在本地分流末尾设置一个有效 final 策略")
    normalize_rules("\n".join(filters[:-1]), allowed=known)
    if sections.get("server_remote") or sections.get("server_local"):
        raise BuildError("公开模板不得包含节点或私人节点订阅，请在客户端添加")
    if any(re.match(r"(?:passphrase|p12)\s*=", line) for line in sections.get("mitm", [])):
        raise BuildError("公开配置不得包含 MITM 私钥或证书密码")


def session():
    client = requests.Session()
    client.headers["User-Agent"] = "QuantumultX-Rules-Builder/1.0"
    client.mount("https://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])))
    return client


def download(client, url):
    response = client.get(url, timeout=(10, 40))
    response.raise_for_status()
    if len(response.content) > 20 * 1024 * 1024:
        raise BuildError("资源超过 20 MiB")
    return response.content.decode("utf-8-sig").replace("\r\n", "\n")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build(root=ROOT, offline=False, strict=False, check=False, fetch=None):
    root = Path(root)
    config = yaml.safe_load((root / "profiles/config.yaml").read_text(encoding="utf-8"))
    repository = os.environ.get("GITHUB_REPOSITORY") or config["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise BuildError("repository 必须为 owner/repo")
    branch = config.get("branch", "main")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", branch):
        raise BuildError("发布分支名称无效")
    prefix = public_url(f"https://raw.githubusercontent.com/{repository}/{branch}/dist")
    template = local_path(root, config["template"]).read_text(encoding="utf-8")
    known = policies_in(template)
    mapping = config.get("policy_map", {})
    lock_path = root / "vendor/sources.lock.json"
    old_lock = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else {}
    writes, lock, report, manifest = {}, {}, [], {}
    original_refs, mirror_refs, ids = [], [], set()

    def register(identifier):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", identifier) or identifier in ids:
            raise BuildError(f"资源 ID 无效或重复: {identifier}")
        ids.add(identifier)

    def reference(url, identifier, policy=None):
        line = f"{url}, tag={identifier}, update-interval=86400, enabled=true"
        return line + (f", force-policy={policy}" if policy else "")

    combined_custom = []
    for item in config.get("custom", []):
        identifier = item["id"]
        register(identifier)
        content = local_path(root, item["file"]).read_text(encoding="utf-8")
        content, count = normalize_rules(content, mapping=mapping, allowed=known, allow_empty=True)
        combined_custom.extend(active(content))
        writes[f"dist/rules/{identifier}.list"] = content
        manifest[identifier] = {"rules": count, "sha256": digest(content), "source": item["file"]}
        if count:
            line = reference(f"{prefix}/rules/{identifier}.list", identifier)
            original_refs.append(line)
            mirror_refs.append(line)
    normalize_rules("\n".join(combined_custom), allowed=known, allow_empty=True)

    with session() as client:
        fetch = fetch or (lambda url: download(client, url))
        for item in config.get("sources", []):
            identifier = item["id"]
            register(identifier)
            name = item.get("name", "")
            if not item.get("url") and not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise BuildError(f"上游规则名称无效: {name}")
            url = public_url(item.get("url") or f"https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/QuantumultX/{name}/{name}.list")
            policy = mapping.get(item["policy"], item["policy"])
            if policy not in known:
                raise BuildError(f"上游规则引用不存在的策略: {policy}")
            cache_name = f"vendor/filters/{identifier}.list"
            cached = root / cache_name
            metadata = old_lock.get(identifier, {})

            def cache():
                if metadata.get("url") != url or not cached.exists():
                    raise BuildError(f"{identifier}: 没有与当前来源匹配的快照")
                text = cached.read_text(encoding="utf-8")
                if digest(text) != metadata.get("sha256"):
                    raise BuildError(f"{identifier}: 快照哈希校验失败")
                return text

            state = "offline" if offline else "downloaded"
            try:
                raw = cache() if offline else fetch(url)
                normalized, count = normalize_rules(raw, policy=policy, allowed=known)
                previous_count = metadata.get("rules", 0) if metadata.get("url") == url else 0
                if previous_count and count < previous_count * 0.7:
                    raise BuildError(f"{identifier}: 规则数从 {previous_count} 降为 {count}，超过 30% 缩减门槛，请人工核查")
            except (requests.RequestException, UnicodeError, BuildError) as exc:
                if offline or strict:
                    raise BuildError(f"{identifier}: {exc}") from exc
                raw = cache()
                normalized, count = normalize_rules(raw, policy=policy, allowed=known)
                state = "cached"
                print(f"WARNING {identifier}: 使用已校验旧快照；{exc}", file=sys.stderr)
            normalized = (f"# Generated by {repository}; policy normalized; exact duplicates removed.\n"
                          f"# Source: {url}\n# Original source and terms: ../../vendor/filters/{identifier}.list and ../../THIRD_PARTY.md\n" + normalized)
            writes[cache_name] = raw
            writes[f"dist/rules/{identifier}.list"] = normalized
            lock[identifier] = {"url": url, "sha256": digest(raw), "rules": count}
            manifest[identifier] = {"source": url, "sha256": digest(normalized), "rules": count, "policy": policy}
            report.append({"id": identifier, "state": state, "rules": count})
            original_refs.append(reference(url, identifier, policy))
            mirror_refs.append(reference(f"{prefix}/rules/{identifier}.list", identifier, policy))

    rewrites = []
    for item in config.get("rewrite_remote", []):
        tag = item["tag"]
        if any(c in tag for c in ",\r\n"):
            raise BuildError("重写 tag 不能包含逗号或换行")
        rewrites.append(reference(public_url(item["url"]), tag))
    hosts = list(dict.fromkeys(part.strip() for line in active((root / "rules/mitm-hosts.list").read_text(encoding="utf-8")) for part in line.split(",") if part.strip()))
    if any(re.search(r"[\s/=]", host) for host in hosts):
        raise BuildError("MITM hostname 格式无效")
    rewrite_local = "\n".join(active((root / "rules/rewrite.list").read_text(encoding="utf-8")))
    for filename, refs in [("QuantumultX.conf", mirror_refs), ("QuantumultX_Upstream.conf", original_refs)]:
        output = template
        for key, value in {"FILTER_REMOTE": "\n".join(refs), "REWRITE_REMOTE": "\n".join(rewrites),
                           "REWRITE_LOCAL": rewrite_local, "MITM_HOSTNAME": "hostname=" + ", ".join(hosts) if hosts else "# hostname 未配置"}.items():
            if output.count("{{" + key + "}}") != 1:
                raise BuildError(f"模板必须包含一个 {key} 占位符")
            output = output.replace("{{" + key + "}}", value)
        validate_profile(output, known)
        writes[f"dist/{filename}"] = output
    writes["vendor/sources.lock.json"] = json_text(lock)
    writes["dist/manifest.json"] = json_text(manifest)
    changed = [name for name, content in writes.items() if not (root / name).exists() or (root / name).read_text(encoding="utf-8") != content]
    if check and changed:
        raise BuildError("生成文件与输入不一致: " + ", ".join(changed))
    if not check:
        # All downloads and validation finish before any tracked output is changed.
        for name in changed:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(writes[name], encoding="utf-8", newline="\n")
            os.replace(temporary, path)
        (root / ".build").mkdir(exist_ok=True)
        (root / ".build/report.json").write_text(json_text(report), encoding="utf-8")
    print(json_text({"changed_files": len(changed), "resources": len(manifest), "rules": sum(x["rules"] for x in manifest.values()), "sources": report}))
    return writes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="只使用已校验的本地快照")
    parser.add_argument("--strict", action="store_true", help="任一下载失败则终止，不回退快照")
    parser.add_argument("--check", action="store_true", help="检查产物一致性，不写文件")
    args = parser.parse_args()
    try:
        build(offline=args.offline, strict=args.strict, check=args.check)
    except (BuildError, OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
