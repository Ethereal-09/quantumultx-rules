# 第三方来源

默认来源：blackmatrix7/ios_rule_script

- 仓库：https://github.com/blackmatrix7/ios_rule_script
- 上游许可证：https://github.com/blackmatrix7/ios_rule_script/blob/master/LICENSE
- 随仓库附带的许可证全文：[GNU GPL version 2](licenses/GPL-2.0.txt)
- 原始快照：`vendor/filters/*.list`，原样保留下载内容与作者注释。
- 精确来源及 SHA256：`vendor/sources.lock.json`。
- 派生规则：`dist/rules/` 中除 `custom-*` 外的默认分类，继续遵循上游 GPL-2.0 许可。
- 派生修改：统一大小写和空格，将第三列策略替换为本项目策略，移除完全重复的规则，补充来源注释。生成逻辑公开在 `src/build.py`。
- 初始派生版本日期：2026-10-07；后续修改日期可查看对应文件 Git 历史。

构建器、模板及自维护规则使用根目录 MIT 许可证。新增其他来源时，应同时保留相应作者信息和许可文件。

架构参考：suversal/qx-config-sync。参考其“配置清单 + 规则镜像 + Actions 发布”的组织方式，没有复制它的源码或底包。
