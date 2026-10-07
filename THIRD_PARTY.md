# 第三方来源与修改说明

本项目维护同步和个人修改，不把上游规则、脚本标为自己的原创内容。

| 来源作者 | 默认内容 | 许可 |
| --- | --- | --- |
| [blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script) | Direct、AdvertisingLite、Apple、OpenAI、GitHub、Telegram、Proxy 分类分流；AdvertisingLite 与 AdvertisingScript 的 QX 重写及配套分流 | [GPL-2.0 原文](licenses/GPL-2.0.txt) |

上游许可见 [blackmatrix7 LICENSE](https://github.com/blackmatrix7/ios_rule_script/blob/master/LICENSE)，订阅包内也附带 `dist/licenses/`。有效脚本依赖按上游配置的原始地址下载，包括作者仓库中的 startup 脚本和同作者 Gist 中的 zheye 脚本；具体地址与文件内作者声明保留在快照及依赖清单。2026-10-07 核实上游 smzdm 脚本地址及目录均返回 404，因此通过修改清单排除了 8 条相关脚本重写，不镜像失效依赖；修改前后记录见 `dist/changes.json`。规则由上游汇总生成，其 README 列出的其他贡献作者与来源应一并尊重，不能将所有内容声称为单人原创。

`upstream/filter/`、`upstream/rewrite/`、`upstream/script/` 原样保存下载的文件字节，包括作者与来源注释。准确下载地址、数量和 SHA256 记录在 `upstream/lock.json`；`dist/manifest.json` 记录派生产物。

派生修改包括：规范化分流类型和空格、根据分类设置策略、移除完全重复规则、排除自维护规则的精确匹配、重放个人增删改、将重写的脚本地址改为本仓库镜像。脚本镜像保留下载字节，脚本地址的查询和片段参数不被静默丢弃。具体来源修改见 `dist/changes.json`，脚本依赖见 `dist/dependencies.json`。

首次派生修改日期为 2026-10-07；后续日期和变更可查看 Git 历史。GPL 来源及派生规则继续遵循上游 GPL-2.0，MIT 来源保留 MIT 许可和作者声明。根目录 MIT 许可证只覆盖本项目原创代码、模板及自维护内容，不覆盖第三方版权。

架构参考 [suversal/qx-config-sync](https://github.com/suversal/qx-config-sync) 的清单、镜像与 Actions 组织方式，本项目没有复制其构建源码或完整底包。新增来源须保存作者、许可和准确来源地址。
