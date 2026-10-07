"""Native QX syntax, without guessing cross-client conversions or match priority."""
from __future__ import annotations

from dataclasses import dataclass, replace
import ipaddress
import re
from urllib.parse import urlsplit


class BuildError(ValueError):
    pass


BUILTINS = {"direct", "proxy", "reject", "reject-tinygif", "reject-dict", "reject-array", "reject-200"}
FILTER_TYPES = {"host", "host-suffix", "host-keyword", "host-wildcard", "ip-cidr", "ip6-cidr", "ip-asn", "geoip", "user-agent"}
FILTER_FLAGS = {"force-cellular", "multi-interface", "multi-interface-balance"}
REWRITE_ACTIONS = {"reject", "reject-img", "reject-200", "reject-dict", "reject-array", "302", "307", "request-header", "request-body", "response-header", "response-body", "echo-response", "jsonjq-response-body", "jsonjq-request-body", "script-response-body", "script-response-header", "script-request-body", "script-request-header", "script-echo-response", "script-analyze-echo-response"}


def is_comment(line):
    return not line.strip() or line.lstrip().startswith(("#", ";", "//"))


def public_url(value):
    if not isinstance(value, str) or re.search(r"[\s,]", value):
        raise BuildError("资源 URL 不允许未编码的空白或逗号")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BuildError("资源 URL 必须是无账号密码的 HTTPS 地址")
    # Query parameters and # parser/script parameters have semantics: keep them.
    return value


def policy_name(value, mapping=None):
    value = (mapping or {}).get(value, value)
    return value.lower() if value.lower() in BUILTINS else value


@dataclass(frozen=True)
class FilterRule:
    kind: str
    target: str
    policy: str
    options: tuple[str, ...] = ()

    @property
    def key(self):
        return self.kind, self.target

    def render(self):
        return ", ".join((self.kind, self.target, self.policy, *self.options))


def parse_filter(line, mapping=None, allowed=None, forced_policy=None, allow_final=False):
    parts = [part.strip() for part in line.split(",")]
    kind = parts[0].lower()
    if kind == "final" and allow_final:
        if len(parts) != 2:
            raise BuildError("final 只能包含一个策略")
        rule = FilterRule(kind, "", policy_name(parts[1], mapping))
    else:
        if len(parts) < 3 or kind not in FILTER_TYPES or not all(parts):
            raise BuildError(f"不是支持的原生 QX 分流规则: {line[:160]}")
        target = parts[1]
        if kind in {"ip-cidr", "ip6-cidr"}:
            try:
                network = ipaddress.ip_network(target, strict=False)
                if network.version != (6 if kind == "ip6-cidr" else 4):
                    raise ValueError("地址族不匹配")
            except ValueError as exc:
                raise BuildError(f"无效 IP 网段: {target}") from exc
        elif kind == "ip-asn":
            if not target.isdigit() or not 0 < int(target) < 2**32:
                raise BuildError(f"无效 ASN: {target}")
        elif kind == "geoip":
            if not re.fullmatch(r"[A-Za-z]{2}", target):
                raise BuildError(f"无效 GEOIP 国家码: {target}")
            target = target.lower()
        elif kind.startswith("host"):
            target = target.lower()
            if re.search(r"[\s/<>:=]", target) or (kind in {"host", "host-suffix"} and re.search(r"[*?]", target)):
                raise BuildError(f"无效域名匹配内容: {target}")
        options = tuple(parts[3:])
        for option in options:
            if option not in FILTER_FLAGS and not re.fullmatch(r"via-interface=[^\s,]+", option):
                raise BuildError(f"未支持的 QX 附加参数（不会自动丢弃）: {option}")
        if len(options) != len(set(options)) or sum(option in FILTER_FLAGS for option in options) > 1:
            raise BuildError("分流规则存在重复或互斥的网络接口选项")
        rule = FilterRule(kind, target, policy_name(forced_policy or parts[2], mapping), options)
    if allowed is not None and rule.policy not in allowed:
        raise BuildError(f"引用不存在的策略: {rule.policy}")
    return rule


@dataclass
class FilterDocument:
    lines: list[str | FilterRule]

    @classmethod
    def parse(cls, text, **options):
        lines = []
        for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
            try:
                lines.append(line if is_comment(line) else parse_filter(line, **options))
            except BuildError as exc:
                raise BuildError(f"第 {number} 行: {exc}") from exc
        return cls(lines)

    @property
    def rules(self):
        return [line for line in self.lines if isinstance(line, FilterRule)]

    def render(self):
        return "\n".join(line.render() if isinstance(line, FilterRule) else line for line in self.lines).rstrip() + "\n"

    def validate(self, known, allow_empty=False):
        seen, duplicates, result = {}, 0, []
        for line in self.lines:
            if isinstance(line, FilterRule):
                if line.policy not in known:
                    raise BuildError(f"引用不存在的策略: {line.policy}")
                # Different network options can have different semantics; do not collapse.
                key = (*line.key, line.options)
                if key in seen:
                    if seen[key] != line.policy:
                        raise BuildError(f"资源内存在冲突: {line.kind}, {line.target}")
                    duplicates += 1
                    continue
                seen[key] = line.policy
            result.append(line)
        self.lines = result
        if not self.rules and not allow_empty:
            raise BuildError("分流资源没有有效规则")
        return duplicates


@dataclass(frozen=True)
class RewriteRule:
    pattern: str
    operator: str
    action: str
    argument: str

    def render(self):
        return f"{self.pattern} {self.operator} {self.action}" + (f" {self.argument}" if self.argument else "")

    @property
    def script_url(self):
        if not self.action.startswith("script-"):
            return None
        return public_url(self.argument)


def parse_rewrite(line):
    match = re.fullmatch(r"(.+?)\s+(url|url-and-header)\s+(\S+)(?:\s+(.+))?", line.strip())
    if not match or match[3] not in REWRITE_ACTIONS:
        raise BuildError(f"不是支持的原生 QX 重写: {line[:160]}")
    rule = RewriteRule(match[1], match[2], match[3], match[4] or "")
    if rule.action.startswith("script-"):
        rule.script_url
    elif rule.action in {"302", "307"}:
        if not re.match(r"https?://", rule.argument):
            raise BuildError("重定向缺少 HTTP(S) 目标地址")
    elif not rule.action.startswith("reject") and not rule.argument:
        raise BuildError(f"{rule.action} 缺少参数")
    # QX regex syntax is not Python's regex syntax. Do not use re.compile as a gate.
    return rule


def parse_hosts(value):
    hosts = [part.strip() for part in value.split(",") if part.strip()]
    if any(re.search(r"[\s/=<>]", host) for host in hosts):
        raise BuildError("MITM hostname 中存在无效字符")
    return list(dict.fromkeys(hosts))


@dataclass
class RewriteDocument:
    lines: list[str | RewriteRule]
    hosts: list[str]

    @classmethod
    def parse(cls, text, allow_empty=False):
        lines, hosts = [], []
        for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
            try:
                if is_comment(line):
                    lines.append(line)
                elif re.match(r"hostname\s*=", line, re.I):
                    hosts.extend(parse_hosts(line.split("=", 1)[1]))
                else:
                    lines.append(parse_rewrite(line))
            except BuildError as exc:
                raise BuildError(f"重写第 {number} 行: {exc}") from exc
        document = cls(lines, list(dict.fromkeys(hosts)))
        if not document.rules and not allow_empty:
            raise BuildError("重写资源没有有效规则")
        return document

    @property
    def rules(self):
        return [line for line in self.lines if isinstance(line, RewriteRule)]

    def render(self):
        lines = [line.render() if isinstance(line, RewriteRule) else line for line in self.lines]
        if self.hosts:
            lines.append("hostname = " + ", ".join(self.hosts))
        return "\n".join(lines).rstrip() + "\n"


def parse_sections(text):
    sections, current = {}, None
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\[[A-Za-z_]+\]", stripped):
            current = stripped[1:-1]
            if current in sections:
                raise BuildError(f"重复配置段: {current}")
            sections[current] = []
        elif current and not is_comment(stripped):
            sections[current].append(stripped)
    return sections


def policies_in(template):
    known, candidates, graph = set(BUILTINS), [], {}
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
        graph[parts[0]] = [x for x in parts[1:] if "=" not in x]
        candidates.extend(graph[parts[0]])
    for name in candidates:
        if name not in known:
            raise BuildError(f"策略组引用不存在的策略: {name}")
    visited, visiting = set(), set()
    def walk(name):
        if name in visiting:
            raise BuildError(f"策略组循环引用: {name}")
        if name in visited:
            return
        visiting.add(name)
        for candidate in graph.get(name, []):
            walk(candidate)
        visiting.remove(name)
        visited.add(name)
    for name in graph:
        walk(name)
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
    if finals != [len(filters) - 1]:
        raise BuildError("必须且只能在本地分流末尾设置一个 final 策略")
    for line in filters:
        parse_filter(line, allowed=known, allow_final=True)
    for line in sections.get("rewrite_local", []):
        parse_rewrite(line)
    if sections.get("server_remote") or sections.get("server_local"):
        raise BuildError("公开配置不得包含私人节点，请在客户端添加")
    if any(re.match(r"(?:passphrase|p12)\s*=", line, re.I) for line in sections.get("mitm", [])):
        raise BuildError("公开配置不得包含 MITM 私钥或证书密码")
