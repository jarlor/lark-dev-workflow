# 在 Linux 上接飞书，叫醒本机的 Codex

这是独立服务，不是新 Skill。连接、收件、去重、失败重试和定期补查由服务负责；工作流 Skill 保留项目范围、拆分与验收规则。服务不会把评论正文当作终端命令，也不打开交互式 Codex 或另建业务会话。正式模式仅调用项目已配置的单轮执行命令。不要再为它安装第二条 cron。

## 准备

1. 在项目所属组织建立或选择专用企业自建应用，开启机器人能力，设置名称及参与人可用范围。在同一 Linux 用户下配置专用 CLI profile。独立助手使用 `--as bot`；不要修改其他项目的 profile。云端表与应用的租户、应用 ID、机器人 open ID、测试表身份均须核对。只在非业务的飞书测试副本先联调。
2. 服务从 Linux 主动连接飞书，无须开放入站端口。在开发者后台选择「使用长连接接收事件」，添加下表两种事件并发布应用版本。如果用下面的 `pair` 首次接入，等它退出后再启动 `observe`。同一个应用同时只运行一个接收端。整个接入使用已有飞书免费能力。[飞书长连接设置](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)
3. 后台选好事件后，还要用助手身份订阅。先在专用测试副本验证，再对实际项目操作。

| 希望收到什么 | 谁来订阅、订阅哪里 |
| --- | --- |
| 有人改了测试表的记录 | 机器人对该 Base 调用 `POST /open-apis/drive/v1/files/{base_token}/subscribe?file_type=bitable`，需要拥有或管理这份副本。文件订阅使用 `docs:event:subscribe`。按记录事件文档在应用身份与用户身份两栏核对多维表格权限 `bitable:app`。此处由机器人发请求。[记录变更事件](https://open.feishu.cn/document/docs/bitable-v1/events/bitable_record_changed) · [文件订阅](https://open.feishu.cn/document/server-docs/docs/drive-v1/event/subscribe) |
| 有人 @ 助手或回复它的评论 | 给应用开通 `docs:document.comment:read`。执行 `lark-cli drive user subscription --profile <助手配置名> --as bot --data '{"event_type":"drive.notice.comment_add_v1"}'`。其他普通评论仍靠服务定期回表补读。[评论订阅](https://github.com/larksuite/cli/blob/v1.0.100/skills/lark-event/references/lark-event-drive.md) |

如果由助手从公开模板建测试副本，复制时选择「仅复制结构」，需要应用身份的 `base:app:copy`。也可以由负责人先复制，再把测试副本交给助手管理。第一次请人确认前，核对这位确认人对测试副本有编辑权限，并能从记录直达链接打开「状态」，看到可点的「完成节点」。核对时把按钮留给确认人本人点。

## 安装、观察

在可信的固定源码目录安装此服务。下面假定完整包位于 `/opt/lark-dev-workflow`，Linux Python 3.10 或以上。用该目录创建隔离环境，不替换系统 Python：

```sh
python3 -m venv /opt/lark-dev-workflow/venv
/opt/lark-dev-workflow/venv/bin/pip install -r /opt/lark-dev-workflow/service/requirements.txt
```

第一次接入时，把新应用的 App Secret 交给 Linux 上的接收服务，放在仅运行账号可读的凭据文件。飞书 CLI 扫码建应用后，Python SDK 仍需要这份凭据。管理员可以从开发者后台该应用的「凭证与基础信息」取得它。

还不知道组织标识 `tenant_key` 时，可用以下命令接收一次测试表事件。替换应用、测试 Base 和表的 ID，示例中的 Linux 账号也要对应实际服务。等待期间在飞书后台保存长连接、发布事件并订阅测试副本，然后手动编辑测试表的一行。收到该应用、该副本、该表的记录事件后，命令打印 `tenant_key` 并退出。超时可以重试。这一步不会启动 Codex。

```sh
systemd-run --wait --pipe --collect --property=User=lark-workflow \
  --property=LoadCredential=feishu_app_secret:/etc/lark-dev-workflow/demo/app-secret \
  /opt/lark-dev-workflow/venv/bin/python /opt/lark-dev-workflow/service/event_bridge.py pair \
  --app-id YOUR_APP_ID --base-token YOUR_TEST_BASE_TOKEN --table-id YOUR_TEST_TABLE_ID --timeout 600
```

配置不放在 Git 里。示例 `/etc/lark-dev-workflow/demo/bridge.json` 如下，替换为真实测试值。状态目录为本项目独用，权限 0700，不与业务执行器的状态目录共用；凭据只放受保护的 `app-secret` 文件，由 systemd LoadCredential 交给接收端，不填进 JSON、仓库、聊天或日志。

```json
{
  "mode": "observe",
  "app_id": "YOUR_APP_ID",
  "tenant_key": "YOUR_TENANT_KEY",
  "bot_open_id": "YOUR_BOT_OPEN_ID",
  "base_token": "YOUR_TEST_BASE_TOKEN",
  "table_ids": ["YOUR_TEST_TABLE_ID"],
  "project_record_id": "YOUR_TEST_PROJECT_RECORD_ID",
  "lark_profile": "dedicated-assistant",
  "state_dir": "/var/lib/lark-dev-workflow/demo/events",
  "reconcile_seconds": 600,
  "debounce_seconds": 5,
  "retry_seconds": 60
}
```

运行 `venv/bin/python service/event_bridge.py check --config /etc/lark-dev-workflow/demo/bridge.json` 仅验证配置。用 [systemd 单元](linux/lark-dev-workflow@.service)运行 `listen`（示例实例名 demo）。单元默认账号是 `lark-workflow`：已有执行器时**用它原来的 Linux 账号**，通过该实例的 systemd drop-in 设 User；核实目录、Codex 登录及程序路径都对应此账号。不要因为新账号的 crontab 是空的就断言旧调度已停。observe 只接通知和记计数，不启动 Codex；用 `status --config …` 看各类事件的变化，进程“正在运行”不能证明飞书已推送。

在测试副本逐项核对新增记录、真人推进节点、@ 助手、回复助手、普通评论和助手自己的评论。检查收到的事件、目标记录和实际收件人。对于原生流程字段，还要验证助手能读到**谁确认了哪一版计划**。单独看到「可开始」或收到一条记录变更事件，均不足以放行。这个条件缺失时可以继续测试通知，正式执行器仍使用原来的调度。

人能点「完成节点」后，还要测试助手负责的下一步。飞书 CLI 能读到流程字段的状态名称，但会忽略对这个字段的写入。即使同一请求成功写入了其他字段，也要读回状态。[飞书 CLI 维护者说明](https://github.com/larksuite/cli/issues/1917#issuecomment-5252737214)

飞书自带的「修改记录」自动化可以改变流程状态。测试时，当前节点的必填项还空着，自动化也推进了记录。若项目采用这条路径，在触发条件中明确检查当前是 AI 负责的节点、这一行所需的资料已填齐，再只更新触发这次变化的记录。用缺资料、填齐和重复触发三种情况试跑，并核对记录历史。自动化写下的状态只能证明规则运行过；人的确认仍需核对确认人亲自做的操作。

如需核对是谁点了节点，先给独立助手执行器配置 `event_tenant_key`（初始化参数 `--event-tenant-key`），填前面配对得到的值。让服务的 `runner_config` 指向该执行器，先成功运行一次只读 `scan`。现有接收器会为扫描已确认属于该项目的需求、Bug 和研发项，保存最近 256 笔状态或修订变化，包括操作者的应用内 ID、记录和字段 ID、事件 ID、编辑时间、表版本与能识别的前后值。评论和计划正文不进入这份记录。普通 observe 仍只记计数；配好这项功能的 observe 可留存核对材料，两种 observe 均不启动 Codex。两次完整扫描中一直属于该项目的记录，延迟送达的变更仍可核对，最多回看 24 小时。首次看到这条记录、改过项目后重新看到它、首次启动本次监听时，精确到同一秒的变更只作提示。选项改名后按本次扫描读到的选项核对，实际确认是原生流程字段时，前后值有时只有两个节点编号。会保留这两个编号，交给执行器查这条记录的历史。只有前后编号、查不到历史对应的节点名称或操作人时，保留待确认；其他未知格式只作提示。用服务状态里的 `retained_gate_changes` 查看现存几笔材料。

专用助手能读到流程状态名称，但读不到节点权限和必填规则时，可在 Linux 上运行：

```sh
sh /path/to/lark-dev-workflow/scripts/unattended.sh scan --config /path/to/runner.json --inspect-native-stages
```

这会读全项目记录和评论，核对原生字段类型、可见状态和绑定时的顺序。结果单独保存在执行器状态目录的 `native-stage-inspection/`。已配对组织时，这次检查也会为本项目的记录登记事件范围。读到一条人点过的记录后，再按下文核实是谁点的、确认了哪一版。身份交接、绑定迁移和自动派工仍使用完整检查。

希望同一个 Linux 执行器先处理反馈、写计划时，在已配对组织的专用助手运行配置中设置 `"native_stage_mode": "prepare"`。把本项目的服务配置和下方确认工具的实际路径写进已有项目规则，方便执行器核对人的点击。沿用同一个服务和 `tick` 入口。它会把这类运行的会话和处理进度单独保存。项目原来的只读或暂停要求继续生效。可处理到哪一步，见 [只处理反馈和计划](../skill/lark-dev-workflow/references/assistant-identity.md#只处理反馈和计划)。

用一条测试研发项验证计划确认。助手填入第 1 版计划，指定的人点「完成节点」。用助手身份对这条记录运行 `lark-cli base +record-history-list`，核对记录、前后状态、实际操作人和当时的计划。单条历史需要 `base:history:read`。历史只返回姓名时，按[执行器的核对方法](../skill/lark-dev-workflow/references/unattended-worker.md)对照本次事件和现有修订；缺少确切依据或计划已改时继续等待。测试时分别验证真人点节点和自动化改节点，区分谁完成了哪一步。[飞书记录历史命令](https://github.com/larksuite/cli/blob/v1.0.98/shortcuts/base/record_history_list.go) · [飞书记录变更事件](https://open.feishu.cn/document/docs/bitable-v1/events/bitable_record_changed)

### 核对刚收到的一次确认

使用运行长连接的 Linux 账号，在安装包根目录运行下面的只读命令。用实际服务配置和记录编号替换示例。`requirements` 和 `bugs` 用于范围确认，`work` 用于研发计划。

```sh
python3 service/verify_confirmation.py --config /etc/lark-dev-workflow/demo/bridge.json --role work --record-id recExample
```

工具从该服务的事件日志取操作者，用同一助手从飞书通讯录核对人员账号，再读这一行和它的修改历史。它核对具体修改、指定确认人、范围或计划和业务修订。如果事件只含两个原生节点编号，就只用这行、这个字段、同一表版本和同一秒的唯一历史变化解释这一次操作。同秒有多笔可能的操作、确认人变了、改过计划或历史不全时，返回 `pending` 并说明原因。通讯录不可见时也返回待核实。

`verified` 表示这条记录的这一版已由指定人员账号完成该步。工具只核对本行字段。范围或计划引用外部说明时，须另查当时和现在的文档版本。安排开发前仍需查上游范围、Bug 复现、项目约定和依赖。如果原事项已经保存了同一版本的可靠确认依据，可以继续使用。接收器保留新事件的时间有限，记录过期不会撤销已有确认，也不用请人再点一次。

### 核对 AI 更新节点的规则

服务器可用专用助手读回飞书里已保存的新版工作流。先运行 `python3 service/workflow_rule.py --config /etc/lark-dev-workflow/demo/bridge.json --show-binding`，将输出的 `service` 和 `runner` 保存在该服务状态目录的 `ai-node-rules.json`，权限设为 0600。在同一文件的 `rules` 中按名称登记要检查的规则。下面的编号仅示意格式，安装时使用本副本的真实编号。

```json
{
  "service": "本服务的指纹",
  "runner": "本执行器的指纹",
  "rules": {
    "项目条件": {
      "purpose": "project_scope", "workflow_id": "wkfExample", "role": "requirements", "table_id": "tblReq",
      "guard": {"kind": "project_link", "field_id": "fldProject", "value": "recProject"}
    },
    "AI节点": {
      "workflow_id": "wkfUpdate", "role": "diagnostic", "table_id": "tblExample",
      "stage_field_id": "fldStage", "marker_field_id": "fldMarker",
      "source": {"id": "optFirst", "name": "AI 核验"}, "target": {"id": "optNext", "name": "人工确认"},
      "guard": {"kind": "text", "field_id": "fldTitle", "value": "仅核对配置"},
      "required_field_ids": ["fldEvidence"], "owner_field_id": null
    }
  }
}
```

运行 `python3 service/workflow_rule.py --config /etc/lark-dev-workflow/demo/bridge.json --rule 项目条件`。专查项目条件时，工具核对关联字段指向的项目表，再读回指定项目行。即使规则后面只是“查找记录”，也能单独验证这项触发条件。

检查更新规则时，`enabled` 显示飞书里的启停状态，`updates_trigger_row_only` 表示唯一动作只更新触发行的状态。若已有固定定义，可把上次核实的 `definition_sha256` 填入该项，后续步骤变化就会报告不匹配。正式项目按表使用 `requirements`、`bugs` 或 `work`，同时核对原绑定中的 AI 节点、下一步和必填条件。独立技术表用 `diagnostic`。普通文字匹配不能证明只影响一个项目。负责人仅设为“非空”时，`fixed_bot_owner_verified` 为 false。工具只报告规则配置，不写触发标记，也不判断业务是否可派工。

## 启动同一个执行器

测试执行器要有自己的工作区、状态目录和项目配置。绑定 `cli_actor`（identity=bot、专用 profile、app_id、open_id）；`unattended.sh init --access bot --lark-profile …` 生成版本 3 的执行器配置。先测试扫描读全项目，再写一条测试计划并确认机器人署名、@ 到人和原生节点操作。不要用个人网页登录补做机器人被拒绝的操作。私信只在明确授权且收件人可核实的项目启用。

一条反馈拆成一条需求、这条需求只需一个研发项时，Codex 可以按已确认的范围和验收标准写计划。已获项目写入授权的执行器用 `service/plan_action.py` 完成写计划和发提醒。运行前从本地项目配置和该事项核对服务配置、需求记录编号、需求修订号和计划确认人。这四个参数由执行端指定。Codex 在标准输入提供 JSON，填写 `plan`（研发计划）、`scope`（工作范围）、`deliverable`（交付物）和 `verification_plan`（验证方法）。

```sh
python3 service/plan_action.py --config "$bridge_config" --requirement "$requirement_id" \
  --revision "$requirement_revision" --confirmer "$plan_confirmer_open_id" < plan.json
```

未核实本人确认时返回 `waiting`。只有 `notice=notified` 才表示已在这条研发项下读回给指定人员的提醒。中断后用相同参数加 `--reconcile`，省去 `< plan.json`。它查回原研发项和已有提醒，缺少可靠依据时保留待核实的结果。

将测试服务切为 `dispatch`，保留上述固定身份，增加：

```json
{
  "runner_config": "/srv/demo/assistant-runner.json",
  "tick_argv": ["/bin/sh", "/opt/lark-dev-workflow/skill/lark-dev-workflow/scripts/unattended.sh", "tick", "--config", "{runner_config}"]
}
```

`tick_argv` 来自服务管理员的本地配置，无 shell 展开；也可使用经验证的同机项目包装程序，返回原 tick 的 JSON 状态。需要特定恢复规则的项目应保留原有保护：可能已经发生写入、故障原因未知、手动暂停时，不能靠定时重试直接清除暂停或重新执行。启动、事件到达、断线或安静期间的补查统一走这个入口；没变化时不应启动模型。可联网时做真实断线补查、忙时合并和人手动放行验证。

## 替代已有 cron 与回退

1. 测试已通过且完成正式发版后，为正式副本另配相应 ID；模板母版不在范围内。核对旧执行器工作区、绑定、暂停状态、未完成事项、当前会话和实际 cron 所有者。用候选配置执行一次只读 scan，与原身份的可见记录和线程比较。历史确认、失败测试、旧 @ 评论仍须接续。
2. 备份原配置及 crontab，保留该项目原始 cron 行（包含 PATH、包装脚本和标记）。按 [身份交接工具](identity_handoff.py) 的 `--help` 先预检，再在原工作区锁下交接。工具会先保存本次核对摘要，再依次暂停两个执行器和保存交接状态。中途断开时，用原来的两份配置和同一份摘要重跑 `--apply`。如果有人已手动暂停或恢复、原记录发生变化，先核实当前情况。保留状态目录里的交接文件。收到“正在运行”、可见范围缩小或不确定写入时停止切换；不要杀 worker 或手改状态文件。旧调度和候选都暂停后，按原 config 的精确标记移除那一条旧 cron；重新读取确认其他任务仍在。
3. `/etc/lark-dev-workflow/demo/previous-runners.json` 列出原 config 的绝对路径，例如 `["/srv/demo/old-runner.json"]`。全新测试执行器用 `[]`。systemd 在 dispatch 前检查本用户 crontab 上旧/新配置的路径或标记；还须人工核对自定义 wrapper、其他用户、systemd timer 没有第二个入口。
4. 只恢复候选执行器的交接暂停，启用单一服务并读回状态。确认一次人放行只产生一次执行；没有新通知时服务仍能补查或继续已获准工作。升级时先有序停止服务，等在途 tick 结束，再更换完整固定版本。
5. 回退先停止服务、等在途工作收尾；核对切换后是否已有新写入、未完成操作或新的人工决定。将新增情况对账后再恢复原会话及原配置。只恢复捕获的那一条 cron，不能用整份旧 crontab 覆盖别人刚加的任务。只有确认旧调度单独在跑后才报告回退完成。
