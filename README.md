# Quantumult X 规则与懒人订阅

同步第三方 Quantumult X 规则，保留原始快照，重新应用自己的增删改，生成分类订阅和完整配置。暂时只维护 Quantumult X。

## 先看产物

- [构建报告](dist/report.html)：来源、数量、个人修改、冲突和依赖提示，可搜索。
- [含重写的完整配置](dist/QuantumultX.conf)：DNS、策略组、分流、广告拦截重写与脚本去广告。
- [基础分流配置](dist/QuantumultX_Basic.conf)：相同分流，不加载重写和 MITM hostname。
- [详细维护说明](docs/MAINTENANCE.md) · [手机验收步骤](docs/DEVICE_ACCEPTANCE.md)。

当前版本已完成构建与自动检查，手机端效果尚待实测。实际运行的订阅版本以仓库 `main` 分支中的生成文件为准。

## 你平时需要改的文件

| 需求 | 修改位置 |
| --- | --- |
| 新增自己的直连、代理、拦截规则 | `rules/direct.list`、`proxy.list`、`reject.list` |
| 从第三方来源删除某条规则、修改策略、替换脚本 | `overrides/changes.yaml` |
| 添加、关闭或更换第三方来源 | `sources/manifest.yaml` |
| 修改 DNS、策略组、国内直连和兜底 | `templates/QuantumultX.conf` |
| 添加自己的原生 QX 重写、MITM 域名 | `rules/rewrite.list`、`rules/mitm-hosts.list` |
| 修改仓库地址和策略映射 | `profiles/config.yaml` |

例如，放行一个被广告资源拦截的域名：

```yaml
# overrides/changes.yaml 中 filters 的一个操作；域名为示例，需换成上游实际规则。
- id: allow-one-domain
  sources: [advertising]
  action: replace
  match: {type: host-suffix, value: example.com}
  rule: host-suffix, example.com, direct
  expected_matches: 1
```

每次同步都重新执行修改。命中条数不符合预期就停止生成，避免上游改了以后你的修正悄悄失效。当前清单只排除了 8 条引用失效“什么值得买”脚本的重写，不预设个人放行域名。完整示例在 [examples/overrides.example.yaml](examples/overrides.example.yaml)，不会自动启用。

自维护规则与上游具有相同类型、匹配内容、网络选项时，生成器会移除上游对应规则。`host` 与 `host-suffix` 的覆盖范围不同；跨类型重叠或不同资源间的优先级仍需结合客户端检查。输出不设置 `force-policy`，保留个人修改后的逐条策略。

## 本地运行

需要 Python 3.11+，在本目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe src/build.py --strict
.\.venv\Scripts\python.exe src/build.py --offline --check
```

默认和 `--strict` 都是严格联网更新，任一来源或修改校验失败就保留旧产物。`--offline` 使用来源地址及 SHA256 匹配的快照重新生成；`--check` 只检查、不写入；只有显式 `--allow-stale` 才允许下载失败时使用已验证的旧快照。每次运行状态和相对上次的增删摘要在 `.build/report.json`。

`upstream/` 保存原文和锁文件，`dist/` 保存生成结果，均不要手工修改。原始文件字节、作者注释和脚本参数得到保留。关闭来源后，会清理清单中该来源不再使用的产物。

## 在 Quantumult X 中使用

发布后，完整配置地址为：

```text
https://raw.githubusercontent.com/Ethereal-09/quantumultx-rules/main/dist/QuantumultX.conf
https://raw.githubusercontent.com/Ethereal-09/quantumultx-rules/main/dist/QuantumultX_Basic.conf
```

先备份客户端配置，再通过下载配置导入；在客户端添加自己的节点订阅并检查策略组。基础分流不需要 MITM 证书；含重写版本需要你在客户端生成、安装并信任自己的证书。私人节点和证书不进入公开仓库。

分类订阅在 `dist/rules/` 和 `dist/rewrites/`。`dist/add-resources.url` 是追加远程资源的 QX 链接，`dist/resources.json` 是对应资源清单；它们不会创建策略组或导入完整配置，使用前须已有相应策略组。

远程资源的更新间隔为 86400 秒。完整配置模板更新后需重新下载配置，先保存客户端节点和个性化设置。项目不承诺完整配置自动覆盖更新。

## 自动维护

GitHub Actions 支持手动运行、维护文件变更和每天北京时间 06:00 的计划更新，实际时间由 GitHub 调度决定。流程为测试、离线重建校验、联网严格更新、脚本语法检查、再次离线一致性检查，然后提交 `dist/` 与 `upstream/`。

公开规则无需额外 Token 或 Secret，发布使用仓库自带的 `GITHUB_TOKEN`，需允许 Actions 写入仓库。Fork 时构建器使用当前 GitHub 仓库地址；本地构建请更改 `profiles/config.yaml` 的 `repository`。

## 来源与代码结构

默认分流和重写统一来自 [blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script)。重写使用其 QX 原生 `AdvertisingLite` 与 `AdvertisingScript`，分别同步对应 `.list` 配套分流；脚本按上游引用镜像，其中一个来自同作者的 Gist。原作者、许可与派生修改见 [THIRD_PARTY.md](THIRD_PARTY.md)。

构建流程由本项目重新实现：`src/qx_rules.py` 解析和校验，`src/overrides.py` 应用修改，`src/resources.py` 管理下载、快照和写入回退，`src/build.py` 生成配置与报告。组织思路参考 [suversal/qx-config-sync](https://github.com/suversal/qx-config-sync)，语法依据 [QX 官方示例](https://github.com/crossutility/Quantumult-X/blob/master/sample.conf)。
