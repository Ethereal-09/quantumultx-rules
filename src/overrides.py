"""Replay explicit user modifications against freshly parsed upstream documents."""
from dataclasses import replace
import re

from qx_rules import BuildError, FilterRule, RewriteRule, parse_filter, parse_rewrite, parse_hosts, public_url


def check_keys(value, keys, context):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise BuildError(f"{context}: 类型错误或含未知字段")


def selected(operation, documents):
    scopes = operation.get("sources")
    if scopes == "*":
        return list(documents)
    if not isinstance(scopes, list) or not scopes or len(scopes) != len(set(scopes)):
        raise BuildError("修改操作 sources 必须为非空 ID 列表或 '*'（全部资源）")
    missing = set(scopes) - set(documents)
    if missing:
        raise BuildError(f"修改引用未启用或不存在的来源: {sorted(missing)}")
    return scopes


def matched(line, selector, kind):
    keys = {"type", "value", "policy", "options"} if kind == "filters" else {"pattern", "operator", "action", "argument", "script_url"}
    check_keys(selector, keys, "match")
    if not selector:
        raise BuildError("match 不能为空；不允许误删全部资源")
    for key, value in selector.items():
        attribute = {"type": "kind", "value": "target"}.get(key, key)
        if key == "type":
            value = value.lower()
        if key == "options":
            value = tuple(value)
        if key == "value" and line.kind.startswith("host"):
            value = value.lower()
        if getattr(line, attribute) != value:
            return False
    return True


def apply_changes(data, filters, rewrites, known, mapping):
    check_keys(data, {"version", "filters", "rewrites", "hostnames"}, "overrides")
    if data.get("version") != 1:
        raise BuildError("overrides.version 必须为 1")
    ledger, ids = [], set()
    for kind, documents in [("filters", filters), ("rewrites", rewrites), ("hostnames", rewrites)]:
        operations = data.get(kind, [])
        if not isinstance(operations, list):
            raise BuildError(f"overrides.{kind} 必须为列表")
        for operation in operations:
            check_keys(operation, {"id", "sources", "action", "match", "rule", "value", "script_url", "expected_matches"}, "operation")
            identifier = operation.get("id")
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", identifier) or identifier in ids:
                raise BuildError("修改操作必须具有唯一 id")
            ids.add(identifier)
            scopes, action = selected(operation, documents), operation.get("action")
            if action not in ({"add", "remove"} if kind == "hostnames" else {"add", "remove", "replace", "replace-script"}):
                raise BuildError(f"{identifier}: 不支持的 action")
            if kind == "filters" and action == "replace-script":
                raise BuildError("分流规则不支持 replace-script")
            changes = []
            if action == "add":
                if len(scopes) != 1:
                    raise BuildError("add 必须明确指定一个目标来源")
                target = scopes[0]
                if kind == "hostnames":
                    values = parse_hosts(operation["value"])
                    if len(values) != 1:
                        raise BuildError("hostname 操作每次只处理一个名称")
                    if values[0] in documents[target].hosts:
                        raise BuildError(f"{identifier}: hostname 已存在")
                    documents[target].hosts.append(values[0])
                    changes.append({"source": target, "before": None, "after": values[0]})
                else:
                    rule = parse_filter(operation["rule"], mapping=mapping, allowed=known) if kind == "filters" else parse_rewrite(operation["rule"])
                    if rule in documents[target].rules:
                        raise BuildError(f"{identifier}: 添加的规则已存在，需检查修改是否仍有必要")
                    documents[target].lines.append(rule)
                    changes.append({"source": target, "before": None, "after": rule.render()})
            else:
                candidates = []
                for target in scopes:
                    if kind == "hostnames":
                        value = operation["value"]
                        for index, host in enumerate(documents[target].hosts):
                            if host == value:
                                candidates.append((target, index, host))
                    else:
                        line_type = FilterRule if kind == "filters" else RewriteRule
                        for index, line in enumerate(documents[target].lines):
                            if isinstance(line, line_type) and matched(line, operation.get("match"), kind):
                                candidates.append((target, index, line))
                expected = operation.get("expected_matches", 1)
                if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
                    raise BuildError("expected_matches 必须为正整数")
                if len(candidates) != expected:
                    raise BuildError(f"{identifier}: 预期命中 {expected} 条，实际 {len(candidates)} 条；上游可能已变化")
                for target, index, old in reversed(candidates):
                    if kind == "hostnames":
                        documents[target].hosts.pop(index)
                        new = None
                    elif action == "remove":
                        documents[target].lines.pop(index)
                        new = None
                    else:
                        if action == "replace-script":
                            if not old.script_url:
                                raise BuildError("replace-script 只能匹配脚本重写")
                            new = replace(old, argument=public_url(operation["script_url"]))
                        else:
                            new = parse_filter(operation["rule"], mapping=mapping, allowed=known) if kind == "filters" else parse_rewrite(operation["rule"])
                        documents[target].lines[index] = new
                    changes.append({"source": target, "before": old if isinstance(old, str) else old.render(), "after": new.render() if new else None})
            ledger.append({"id": identifier, "kind": kind, "action": action, "matches": len(changes), "changes": changes})
    return ledger
