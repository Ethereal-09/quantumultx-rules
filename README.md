# Quantumult X 规则与懒人配置

维护自己的 Quantumult X 分流规则，同时提供可以导入的完整配置。暂时只支持 Quantumult X。

## 订阅地址

- [完整配置（规则使用本仓库镜像）](https://raw.githubusercontent.com/Ethereal-09/quantumultx-rules/main/dist/QuantumultX.conf)
- [完整配置（分流规则使用上游地址）](https://raw.githubusercontent.com/Ethereal-09/quantumultx-rules/main/dist/QuantumultX_Upstream.conf)
- [自维护直连规则](https://raw.githubusercontent.com/Ethereal-09/quantumultx-rules/main/dist/rules/custom-direct.list)
- [自维护代理规则](https://raw.githubusercontent.com/Ethereal-09/quantumultx-rules/main/dist/rules/custom-proxy.list)
- [自维护拦截规则](https://raw.githubusercontent.com/Ethereal-09/quantumultx-rules/main/dist/rules/custom-reject.list)
- [规则来源、数量与 SHA256](dist/manifest.json)

其他分类规则位于 `dist/rules/`，也可以单独订阅。导入到已有配置时，如果没有“节点选择”“苹果服务”“AI服务”这些策略组，请通过 `force-policy` 指定自己的策略。

## 使用

1. 在 Quantumult X 中备份现有配置，再通过“下载配置”导入上面的完整配置链接。
2. 在客户端添加自己的节点订阅，然后检查“节点选择”和“AI服务”等策略。项目不提供节点，也不保存你的私人订阅链接。
3. 默认包括自维护规则、广告拦截、Apple、OpenAI、GitHub、Telegram、国外分流，以及国内 IP 直连和最终兜底。
4. 默认不启用重写、不包含 MITM 证书。基础域名拦截不需要安装证书。

远程规则的更新间隔为 86400 秒；完整配置模板的变化需要重新下载配置。重新导入完整配置前，先保存客户端添加的节点及个性化设置。当前版本已进行构建和规则检查，手机端效果仍需实际验证。

## 平时改哪里

| 需求 | 文件 |
| --- | --- |
| 放行误拦截域名 | `rules/direct.list` |
| 指定域名走代理 | `rules/proxy.list` |
| 添加拦截域名 | `rules/reject.list` |
| 调整上游来源、分类和顺序 | `profiles/config.yaml` |
| 修改 DNS、策略组、兜底 | `templates/QuantumultX.conf` |
| 添加原生 QX 重写 | `rules/rewrite.list` |
| 添加重写所需 hostname | `rules/mitm-hosts.list` |

规则示例：

```text
host-suffix, example.com, direct
host-suffix, example.org, proxy
host, ads.example.net, reject
```

`proxy` 会在构建时映射为“节点选择”。支持 host、host-suffix、host-keyword、host-wildcard、ip-cidr、ip6-cidr、ip-asn、geoip、user-agent；不做 Surge、Clash 或 Loon 格式转换。自维护的规则输出排在上游资源之前，多个自维护文件中同一匹配指向不同策略会阻止构建。

不要手工编辑 `dist/` 和 `vendor/`：前者是生成结果，后者是上游快照。修改应放在 `rules/`、`profiles/` 或 `templates/`。

## 本地构建

需要 Python 3.11+：

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python src/build.py --strict
python src/build.py --offline --check
```

- `--strict`：实际更新上游，任一来源失败或校验不通过就停止发布。
- 无参数：实际更新上游；失败时允许使用来源 URL、SHA256 都匹配的本地快照，并打印警告。
- `--offline`：只用本地快照重新生成。
- `--check`：仅检查生成文件是否与输入一致，不写文件。

详细下载状态在 `.build/report.json`，不提交 Git。规则减少超过上次有效数量的 30% 时构建会停止；核实为正常调整后，可仅删除锁文件中对应来源记录，重新在线构建以建立基线。

## 自动更新

GitHub Actions 每天北京时间 06:00 计划运行，也支持手动运行和维护文件变更触发。定时任务的实际开始时间由 GitHub 调度决定。

流程：测试 → 离线构建检查 → 下载上游 → 校验 → 提交 `dist/` 和 `vendor/`。上游失败、空文件、HTML 错误页、策略不存在、缺少 final 或数量异常时，不会提交新订阅。每个来源使用唯一 ID 保存，不会因 URL 文件名相同相互覆盖。

不需要配置 Token、Telegram 或仓库地址 Secret。Actions 使用当前仓库地址，本地使用 `profiles/config.yaml` 中的 `repository`。Fork 后还需要自行更新本文的订阅链接。

仓库公开产物中不允许节点订阅或 MITM 私钥。私人设置请在客户端维护，或放在 Git 忽略的 `private/` 目录（构建器不会自动读取）。

## 重写的边界

`rewrite_remote` 可以添加原生 QX 重写资源；第一版保持外部引用，不下载重写及其依赖脚本。它们不属于已镜像的分流规则。重写脚本、MITM 域名与手机端兼容性需要单独验证，不应把其他客户端插件直接作为 QX 资源使用。

## 结构与参考

```text
profiles/       来源清单与策略映射
templates/      自维护完整配置模板
rules/          自维护规则
src/build.py    构建与校验
tests/          故障回退、顺序、完整性等检查
vendor/         上游原始快照及来源锁文件
dist/           对外订阅配置、规则和清单
```

实现思路参考 [suversal/qx-config-sync](https://github.com/suversal/qx-config-sync)，本仓库重新实现构建流程，使用本地模板，不依赖远程完整底包。

QX 配置语法参考 [Quantumult X 官方示例](https://github.com/crossutility/Quantumult-X/blob/master/sample.conf)。默认上游分流来源为 [blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script)，保留文件内作者及来源注释。第三方规则及其派生规则遵循上游 GPL-2.0 许可，项目代码的 MIT 许可不覆盖第三方规则，详见 [第三方来源与修改说明](THIRD_PARTY.md)。
