# 维护说明

## 数据流

1. 读取本地完整配置模板、来源清单和自维护规则。
2. 下载启用的原生 QX 分流与重写；保存原文字节及哈希。
3. 给上游分流设置分类默认策略，再按 `overrides/changes.yaml` 顺序应用增删改。
4. 排除被自维护规则精确覆盖的上游条目，报告其他跨来源冲突。
5. 解析重写脚本依赖、下载脚本，合并 hostname。
6. 校验策略、模板、修改命中数量和依赖；全部成功后写入订阅、快照和报告。写入遇到普通 I/O 错误会回退已经修改的文件；不保证系统断电时的多文件事务。

同步下载不是启动手机端脚本。生成器不会执行上游 JavaScript，也不会把其他客户端模块猜测转换成 QX。

## 新增来源

在 `sources/manifest.yaml` 添加唯一 ID，使用 `kind: filter` 或 `rewrite`、`format: quantumultx`、HTTPS URL 和本地许可文件。分流需指定 `policy`，必须是模板里的策略组或 QX 内置策略。重写可选 `scripts: mirror`（默认）或 `external`；`requires` 列出必须同时启用的配套来源 ID。

来源整体停用使用 `enabled: false`。原生资源 URL 的 `#` 解析参数会被明确拒绝；若依赖资源解析器转换，应先提供转换后的 QX 文件。镜像 JavaScript 以完整请求 URL（包含查询参数）的哈希命名，防止不同作者的同名脚本互相覆盖，片段参数保留在生成的脚本引用上。

## 新增自己的规则

```text
host, ads.example.com, reject
host-suffix, example.com, direct
host-keyword, example, proxy
ip-cidr, 203.0.113.0/24, proxy
```

`host` 精确域名，`host-suffix` 域名及子域名，`host-keyword` 字符串匹配；`proxy` 映射为“节点选择”。另外支持 host-wildcard、ip6-cidr、ip-asn、geoip、user-agent。网络选项只接受本项目已支持的官方字段，遇到未知选项会停止，不会悄悄删掉。`final` 属于完整配置，不能放入分类规则。

## 修改第三方规则

`overrides/changes.yaml` 包含 `version: 1` 与 `filters`、`rewrites`、`hostnames` 三个列表；操作 ID 全局唯一。操作作用于生成的资源，原始快照不会被改写。

- `sources`：来源 ID 列表，或字符串 `"*"`；添加操作必须只指定一个来源。
- `action`：分流/重写支持 `add`、`remove`、`replace`；重写另支持 `replace-script`；hostname 支持 `add`、`remove`。
- 分流 `match`：`type`、`value`、`policy`、`options` 的精确匹配，多个字段同时满足才命中。
- 重写 `match`：`pattern`、`operator`、`action`、`argument`、`script_url` 的精确匹配。正则表达式本身也是按文本匹配。
- `expected_matches`：删除和替换预期命中总数，默认 1。多来源时是合计值。上游改动导致命中不符会阻止生成。
- `rule`：添加或替换后的整条原生 QX 规则；`script_url`：替换脚本后的 HTTPS 地址；hostname 的 `value` 为单个域名。

操作顺序为分流、重写、hostname；各列表内部按书写顺序执行。分流匹配的策略是分类默认策略，例如 `advertising` 的上游规则在修改前都使用 `reject`。脚本匹配发生在镜像地址替换之前，填写原脚本 URL。请从上游快照或产物确认实际规则，不要直接启用示例占位符。

重写使用 `正则 url 动作 参数`，正则中的逗号不按分流 CSV 解析。支持脚本、拒绝、重定向和已列出的原生内容修改动作；未支持语法会拒绝。QX 正则并非 Python 正则，构建器不以 Python 编译结果冒充客户端校验。

`rules/rewrite.list` 的个人脚本地址默认保留外部引用。需要镜像自己的脚本时，可将其作为许可明确的 `rewrite` 来源加入清单，配置 `scripts: mirror`。

## 检查构建结果

- `dist/report.html`：可搜索来源，查看修改与警告。
- `dist/changes.json`：本次来源修改、精确覆盖、跨来源冲突（样本最多 200 条）。
- `dist/dependencies.json`：脚本原地址、镜像地址、哈希，以及脚本内检测到的 URL 文本。URL 文本可能只是注释，并不等于实际网络调用；这里只提供审查线索，不递归改写代码。
- `.build/report.json`：下载/快照状态、本次改动文件、相对上次构建的增删数量和最多各 30 条样本。离线重建后这里反映该次离线运行。
- `.build/failure.json`：命令行构建失败信息；属于历史失败记录，成功运行看最新 `report.json`。

同一资源中相同匹配指向不同策略会失败。不同资源之间冲突会报告，不替你选择优先级。QX 的本地/远程、匹配类型和资源插入方式会影响命中；单纯把个人订阅排在前面不能证明所有放行都会生效。应尽量使用来源删除或替换，并按手机端请求记录验证。

更新后某来源规则量比已记录基线下降超过 30% 会阻止发布。核实为上游正常变更后，备份并仅删除 `upstream/lock.json` 中该 ID 的记录，再联网严格构建建立新基线。不要用这个方法跳过错误页、格式或修改命中检查。

## 发布

先运行测试和严格构建，通过离线 `--check` 并检查报告，再提交源码、许可、`upstream/` 与 `dist/`。当前仓库 `main` 的工作流会在提交后自动刷新来源；如果主分支受到保护，需配置允许的发布方式。工作流不包含强制推送，冲突会停止。

更换仓库名时，修改本地 `profiles/config.yaml` 并重新构建。GitHub Actions 会自动使用当前仓库名。私人节点和 MITM 证书只在客户端维护。
