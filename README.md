# 飞书研发工作流

一份可复制的飞书研发管理模板，以及操作它的 `lark-dev-workflow` Skill。默认一个 Base 管理多个项目，每次操作使用“06 项目”中的真实项目 ID。

| 交付物 | 位置 | 用途 |
|---|---|---|
| 通用 Skill | `skill/lark-dev-workflow/` | 理解反馈、确认修订、判断证据与流转、核对范围并回写 |
| 飞书模板 | `template/` | 项目与业务表、反馈表单、状态选项、原生关联、日常视图和文档容器 |
| 项目配置 | 目标项目的 `.lark-dev-workflow/project.json` | 绑定共享 Base 中本项目的真实记录、字段、文档章节和维护约定 |

模板提供结构和入口，Skill 负责业务判断。具体边界见 [模板与 Skill 职责](模板与Skill职责.md)。

## 安装

安装到 Codex 的全局 Skill 目录：

```bash
npx skills add jarlor/lark-dev-workflow --skill lark-dev-workflow --agent codex -g
```

去掉 `-g` 可安装到当前项目；其他 Agent 按安装器提示选择。当前安装器会把 Codex 全局 Skill 放在 `~/.agents/skills/lark-dev-workflow`，以实际安装输出为准；已有手动安装也可以保留自己的位置。[安装器说明](https://github.com/vercel-labs/skills/blob/main/docs/commands.md)

固定使用本版本时，可从标签路径安装：

```bash
npx skills add https://github.com/jarlor/lark-dev-workflow/tree/v1.2.0/skill/lark-dev-workflow --agent codex -g
```

运行前准备：

- `skills` 安装器需要 Node.js；当前官方要求 Node.js 22.20 或更高。
- 本包两个辅助脚本需要 Python 3.9 或更高，仅使用标准库。
- 安装并登录官方 [Lark CLI](https://github.com/larksuite/cli)，并提供 `lark-base`、`lark-doc`、`lark-drive` 及其共享认证技能 `lark-shared`。每位使用者通过自己的 user 身份完成登录与资源授权。本版本实际验证使用 Lark CLI 1.0.94、Python 3.9.6。

`skills` 只安装 Skill 子目录。首次绑定还需下载 [v1.2.0 完整发布包](https://github.com/jarlor/lark-dev-workflow/releases/download/v1.2.0/lark-dev-workflow-v1.2.0.zip)，解压后保留其中的 `template/`；将实际 `template/schema.json` 路径交给 Agent，作为 `onboard.py --contract` 的输入。仓库克隆也包含同一目录。模板独立保存，安装 Skill 不会自动登录飞书或生成项目配置。

## 开始使用

1. 有访问权限时，打开[模板母版](https://qa2vxy8qipi.feishu.cn/base/O2Tjbsrt7a1XorsQ9O1cKbtJnWg)，复制为自己的研发工作区。也可让 Agent 按发布包的 `template/schema.json` 和两份 XML 在你有权限的空间创建空模板。一个副本可管理多个项目；母版保持无业务记录，仓库发布不会改变飞书资源权限。
2. 按上面的安装方式安装 Skill，保留独立的 `template` 目录。飞书操作使用已有的 `lark-base`、`lark-doc` 和 `lark-drive` 工具能力；凭据不写入项目文件。
3. 把副本链接交给 Codex，说明本次创建或接入哪个项目，以及已有维护约定。已有项目直接绑定 06 中的真实记录；新项目在已授权的接入范围内初始化。

可直接附上实际副本链接并使用：

> 使用 $lark-dev-workflow 接入这个研发工作区，在其中创建“新产品研发”项目。这个 Base 以后还会管理其他项目。本项目由我确认范围，正式记录由我授权的当前 Codex 维护，独立验收按实际执行者关系判断。完成项目记录、两份项目文档章节和反馈项目选项的初始化，将本项目绑定保存到当前工作区；保留其他项目。

初始化先创建 06 项目行，读回真实 ID 和自动编号，再建立两份项目章节、回填真实章节链接，并同步“反馈项目”选项。Agent 从选定项目行定位文档，不凭 Base 中同名文档猜测。已明确的接入授权无需重复确认；只读检查不会初始化远端资源。

日常查询按 project ID 过滤，并核对关联链和目标版本。其他项目的合法视图可以共存，不要求每个项目都先建一套专属视图。07 版本按项目可选；短需求、短测试用表格和原始证据即可，长范围、多截图或复杂证据才写项目文档章节。

## 评论与多人接手

评论处理从本项目的真实记录和绑定文档项目节开始，读取完整线程与回复分页；首次全面对账包含已解决评论，后续对账同源编辑和解决状态变化。先核实作者、项目和适用修订，再正式回写并回读；原线程回复按已有授权执行并去重。评论解决和业务完成分别判断，本地采集记录不替代共享 Base 查重。详见 [评论闭环](skill/lark-dev-workflow/references/comments.md)。

多人协作复用 03 的单用户负责人、状态和进展。项目允许自领或已有分配适用时才接手，其他人占用不覆盖，阻塞／暂停不自动释放，也不按更新时间抢占。交接保留双方真实身份、决定、产物与剩余边界，确认后更换负责人，原执行者停止推进交出的范围。本人操作显式使用已核实的 user 身份；不把项目负责人、代录人或测试者自动当作工作负责人。详见 [轻量协作](skill/lark-dev-workflow/references/collaboration.md)。

两项流程沿用项目原有治理和已授权范围，不新增统一单写、自动监听或定时调度。

## 只读绑定与实际验证

`onboard.py` 只读发现资源，通过 `--contract` 接收独立模板的 `schema.json`，生成项目配置和检查报告；当前参数见 `--help`。它不创建项目、不改表或视图、不启用表单。初始化由 Agent 在用户已授权的范围内使用飞书工具完成，然后运行只读检查。

报告的 `ready` 表示结构绑定检查通过，`validation_level=structure_binding`、`write_access_verified=false` 不证明真实写入或完整业务流程已通过。离线快照的检查结果只适用于该快照，远端写入前仍须核实相关当前结构。

母版反馈表单保持停用。副本先检查共享状态；在已授权的独立副本演练中，先设邀请访问、要求登录、禁止匿名，最后启用。`onboard.py` 只报告这些状态，不会自动更改。提交有演练标识的反馈后，必须回读实际记录核对内容；提交成功回执不替代回读。

## 模板与升级

本包模板规范为 `1.2.0`，绑定格式为 `2`；在线母版位置及发布信息见 `template/release.json`。旧格式 `1` 的项目配置继续可读，不要求迁移现有台账。

升级先核对差异。Skill 不按新模板自动改造已有项目，不更改现有维护约定。刷新只更新可发现字段并保留人工扩展；资源身份、类型或维护责任冲突时，保留原配置并报告差异。

更多用法见 [模板使用说明](template/使用说明.md) 和 [首次使用示例](examples/首次使用示例.md)。

## 验证工具

在本目录运行 `python3 -m unittest discover -s tests -p 'test_*.py'`。离线测试检查发现、字段、关联、项目范围、分页和配置保护，不访问业务台账。

实际模板与副本验证记录见 `验证结果.md`。示例数据只承载演练，不作为真实产品验收或生产发布依据；结构、读流程和写流程分别报告。

原有结构与绑定测试不自动覆盖新增评论处理或多人接手。新增检查分别说明采集、去重、回写与交接的实际覆盖；单账号演练不能宣称两个真实账号的并发接手已经验证。
