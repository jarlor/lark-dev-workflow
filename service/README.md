# 在 Linux 上接飞书，叫醒本机的 Codex

这是独立服务，不是新 Skill。连接、收件、去重、失败重试和定期补查由服务负责；工作流 Skill 保留项目范围、拆分与验收规则。服务不会把评论正文当作终端命令，也不打开交互式 Codex 或另建业务会话。正式模式仅调用项目已配置的单轮执行命令。不要再为它安装第二条 cron。

## 准备

1. 在项目所属组织建立或选择专用企业自建应用，开启机器人能力，设置名称及参与人可用范围。在同一 Linux 用户下配置专用 CLI profile。独立助手使用 `--as bot`；不要修改其他项目的 profile。云端表与应用的租户、应用 ID、机器人 open ID、测试表身份均须核对。只在非业务的飞书测试副本先联调。
2. 服务从 Linux 主动连接飞书，无须开放入站端口。在开发者后台选择「使用长连接接收事件」，添加下表两种事件并发布应用版本。如果用下面的 `pair` 首次接入，等它退出后再启动 `observe`。同一个应用同时只运行一个接收端。整个接入使用已有飞书免费能力。[飞书长连接设置](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)
3. 后台选好事件后，还要订阅具体文件。先在专用测试副本验证，再按文件管理人选定的方式接入正式表。

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

这会读全项目记录和评论，核对原生字段类型、可见状态和绑定时的顺序。结果单独保存在执行器状态目录的 `native-stage-inspection/`。已配对组织时，这次检查也会为本项目的记录登记事件范围。读到一条人点过的记录后，再按下文核实是谁点的、确认了哪一版。这个只读结果可用于定位问题。办理已登记的六个动作，还需逐条核对线上规则和本项目的确认、构建及测试依据。

希望同一个 Linux 执行器先处理反馈、写计划时，在已配对组织的专用助手运行配置中设置 `"native_stage_mode": "prepare"`。把本项目的服务配置和下方确认工具的实际路径写进已有项目规则，方便执行器核对人的点击。沿用同一个服务和 `tick` 入口。它会把这类运行的会话和处理进度单独保存。项目原来的只读或暂停要求继续生效。可处理到哪一步，见 [只处理反馈和计划](../skill/lark-dev-workflow/references/assistant-identity.md#只处理反馈和计划)。

用一条测试研发项验证计划确认。助手填入第 1 版计划，指定的人点「完成节点」。用助手身份对这条记录运行 `lark-cli base +record-history-list`，核对记录、前后状态、实际操作人和当时的计划。单条历史需要 `base:history:read`。历史只返回姓名时，按[执行器的核对方法](../skill/lark-dev-workflow/references/unattended-worker.md)对照本次事件和现有修订；缺少确切依据或计划已改时继续等待。测试时分别验证真人点节点和自动化改节点，区分谁完成了哪一步。[飞书记录历史命令](https://github.com/larksuite/cli/blob/v1.0.98/shortcuts/base/record_history_list.go) · [飞书记录变更事件](https://open.feishu.cn/document/docs/bitable-v1/events/bitable_record_changed)

### 核对刚收到的一次确认

使用运行长连接的 Linux 账号，在安装包根目录运行下面的只读命令。用实际服务配置和记录编号替换示例。`requirements` 和 `bugs` 用于范围确认，`work` 用于研发计划。

```sh
python3 service/verify_confirmation.py --config /etc/lark-dev-workflow/demo/bridge.json --role work --record-id recExample
```

工具从该服务的事件日志取操作者，用同一助手从飞书通讯录核对人员账号，再读这一行和它的修改历史。它核对具体修改、指定确认人、范围或计划和业务修订。如果事件只含两个原生节点编号，就只用这行、这个字段、同一表版本和同一秒的唯一历史变化解释这一次操作。同秒有多笔可能的操作、确认人变了、改过计划或历史不全时，返回 `pending` 并说明原因。通讯录不可见时也返回待核实。

`verified` 表示这条记录的这一版已由指定人员账号完成该步。工具只核对本行字段。范围或计划引用外部说明时，须另查当时和现在的文档版本。安排开发前仍需查上游范围、Bug 复现、项目约定和依赖。如果原事项已经保存了同一版本的可靠确认依据，可以继续使用。接收器保留新事件的时间有限，记录过期不会撤销已有确认，也不用请人再点一次。

要核对需求或 Bug 的最后一次验收点击，请指定送验时的业务修订号。例如，下面的命令核对第 4 版需求。Bug 使用 `--role bugs`。

```sh
python3 service/verify_confirmation.py --config "$bridge_config" \
  --role requirements --record-id "$record_id" --acceptance-click 4
```

`click_verified` 表示该行指定的验收人亲自把这一版事项从「待验收」推进到「已验收」。它核对本行的修改通知、人员账号、修订号和记录历史。缺少通知、当前修订不同、同次或随后改过验收人或交付资料时，返回 `pending`。核实具体构建和测试环境、独立复测结果、正式发布或关联反馈的闭环情况，需要另查对应的交付与验收记录。

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

检查更新规则时，`enabled` 显示飞书里的启停状态，`updates_trigger_row_only` 表示唯一动作只更新触发行的状态。若已有固定定义，可把上次核实的 `definition_sha256` 填入该项，后续步骤变化就会报告不匹配。正式项目按表使用 `requirements`、`bugs` 或 `work`，同时核对原绑定中的 AI 节点、下一步和必填条件。独立技术表用 `diagnostic`。正式项目用真实的项目关联字段和项目记录限制范围。

若该节点由本行负责人办理，在飞书把条件设为“负责人等于研发助手”，并在本地这条规则里填写 `owner_field_id` 和 `"owner_match": "bound_assistant"`。检查器会核对它是单人字段，并用已绑定的助手账号 ID 比对规则。完全匹配时返回 `fixed_bot_owner_verified=true`。只要求“负责人不为空”的旧规则返回 false。规则检查为只读。验证实际效果时，在测试副本分别用助手、同事、无人负责的记录试一次，再重放一次助手的触发。核对只有助手那条进入下一步，并在该行历史里查到一次状态变化。

读取规则需要应用权限 `base:workflow:read`。使用飞书 CLI 修改、启用或停用已有规则还需要 `base:workflow:update`。安装 Skill 本身不授予这项权限。

#### 核对是谁提交了助手请求

当一张表用「助手请求」触发流程时，每条规则单独登记 `"purpose": "assistant_stage_request"`。用上面的 `--rule` 命令逐条检查。检查器读取飞书保存的定义、实际字段和绑定项目，两次读回一致才给出结果。它只接受下表中的六种请求。

| 登记键（`key`） | 「助手请求」填什么 | 哪一步到哪一步 | 哪些字段必须有值 |
|---|---|---|---|
| `req-start` | 需求开始研发 | 可研发 → 研发中 | 关联研发项 |
| `req-accept` | 需求发起验收 | 研发中 → 待验收 | 关联研发项、验收人、验收记录 |
| `bug-start` | Bug开始修复 | 可研发 → 研发中 | 修复研发项、复现基线、复现测试 |
| `bug-accept` | Bug发起验收 | 研发中 → 待验收 | 修复研发项、验收人、修复后复测 |
| `work-start` | 研发项开始执行 | 可开始 → 进行中 | 研发计划、工作范围、约定交付物、验证方式 |
| `work-deliver` | 研发项交付 | 进行中 → 已交付 | 交付与进展 |

需求或 Bug 当初确认范围时没填验收人，送验前可以这样查。运行 `python3 service/acceptor_enrollment.py --config "$bridge_config" --role requirements --record-id "$record_id"`，Bug 用 `--role bugs`。工具只读飞书，会逐条核对关联反馈，确认每条反馈都是原范围确认人亲自提交的。

- `ready_to_fill` 表示可以补登。由项目唯一的执行器在写入前重查记录版本和空栏，只填写返回的验收人，再读回核对。
- `already_assigned` 表示这位验收人已经填好。上次写入没收到回执时，用原参数核对这行和修改历史。结果仍不清楚就先等待，避免重复写入。
- `pending` 会说明哪里还没核实。有人代录、出现多位反馈人、原范围变了或已有其他验收人时，先保留原记录。

补登只确定由谁验收。验收人按约定检查并点「已验收」后，人工验收才完成。

需求原计划明确允许用隔离数据复测时，可逐条登记这项独立测试。登记依据包括已确认的计划、本地实际测过的代码、测试服部署版本，以及两边实际运行的制品。人工验收另建「未执行」记录，等验收人在约定的测试环境试用。要求在测试服完成的集成检查和 Bug 复测继续使用原环境。

登记项沿用上面的工作流、表和字段编号，另外填写 `key`、工作流的 `title`、`table_name`、`marker_text`，并把提交请求的助手 open_id 填入 `authored_by`。`guard` 使用 `project_link` 并固定本项目的记录编号。可选的 `project_filter_mode` 只接受 `trigger_project_id`。

`nature_field_id` 和 `processing_field_id` 分别指向记录性质和处理情况，`nature_option` 和 `processing_option` 分别填写「当前工作」和「正常」的真实 `id`、`name`。`source` 和 `target` 填写该副本实际核实的节点编号和名称，`required_field_ids` 填上表对应的本表字段编号。研发项使用 `owner_match=bound_assistant` 和单人负责人字段。需求和 Bug 使用 `owner_match=none`、`owner_field_id=null`。

在「助手请求」的字段监听条件（`field_watch_info`）中选择「等于」，填该动作的完整文字。它核对本次新填的值。`record_watch_info` 比较修改前的值，只用来筛选项目、起始步骤、记录性质、处理情况、负责人和该步必填项。把动作文字放在那里会让从空白填入请求时漏触发，清空旧请求时反而可能推进。旧版按这种写法得到的核对结果不能用于启用规则。实际执行时，一次只写「助手请求」这一列，避免同时改变筛选依据。

只有当这次改动来自绑定助手，才能把触发行推进一步。其他人填写同样的文字时，另一条分支只等待一分钟就结束。`new_marker_value_verified` 与 `trigger_modifier_verified` 分别表示已核对新填的文字条件和修改人判断。`project_condition_verified` 表示规则比较了当前绑定的项目编号；若项目字段允许多选，还要用同时关联两个项目的记录验证行为。此时 `project_scope_verified` 保持 false。

字段有值只能证明有人填过。发起请求前，执行器仍须核实本版的人工确认、关联事项的实际进度，以及适用构建的复现、自测和验收依据。停用规则也可以检查，`enabled` 单独表示是否启用。`dispatch_checked` 保持 false。

测试副本和正式表共用一个飞书应用时，切换前先停测试接收器。飞书会把每个事件分给其中一个连接。两个服务各自只处理自己的表时，事件可能分到另一边。切换后，用正式表的一次真实变化核对服务确实收到了通知。[飞书长连接说明](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)

### 让同一个 Codex 办理六个动作

在测试副本走通六步后，管理员为要接入的项目保存六份已核对的规则，按上表登记在本项目的 `ai-node-rules.json`。每条记录都填写从飞书读回的 `definition_sha256`。正式接手时，逐条核实规则已启用，保留原项目的负责人、暂停事项和人工确认记录。

在专用助手的 `runner.json` 中设置 `native_stage_mode` 为 `registered_requests`，将 `stage_service_config` 设为同一项目 `bridge.json` 的绝对路径。继续使用项目原有的 Codex 和工作目录。运行 `unattended.sh scan --config "$runner_config"`，它会读回本项目的记录、评论和三张表可见的流程节点。办理具体一步时，执行器使用下面的入口。

```sh
python3 service/stage_action.py --config "$bridge_config" --action work-start --record-id "$record_id"
```

返回 `completed` 表示已从这行的飞书历史核实节点推进。`already_completed` 表示同一个动作之前已经完成。返回 `pending` 时先看原因。上一次写请求没有收到应答时，用原参数重查。服务会把飞书发来的助手写入通知与本行历史逐项核对。若当时没收到这条通知，事项保持待核实，请勿重发同一请求。工具只为分给这个助手的研发项开工或交付。需求和 Bug 送验前，还会核对关联研发项、同一构建的测试结果和留给人的验收记录。

测试结果来自项目已有的运行报告、制品或部署检查。服务管理员把本项目的 `read_evidence.py` 和 `registration.json` 安装在服务配置同级的 `stage-proof-source/` 中。登记读取器的 SHA256、允许读取的证据目录以及本服务和执行器的标识。读取器由管理员维护，服务账号只需读权限。接口定义见 [stage_source.py](stage_source.py)，业务核对见 [stage_business.py](stage_business.py)。接入时用本项目实际产生的报告验证。某一项缺少测试或部署材料时，助手会在那一步等待。范围确认、计划确认和最后验收继续由指定的人在事项里点「完成节点」。

如果已确认的研发计划明确允许隔离复测，项目可为这条需求的指定测试记录登记该约定。助手会核对原计划、实际测试的代码版本、测试人员和执行时间，并逐个比对本地与测试服的制品。测试服还须通过部署与健康检查。要求在测试服运行的集成检查仍须在那里完成，最终由指定验收人在测试服验收。Bug 修复后复测沿用项目原有的环境要求。 证据读取器应把从固定构建和测试服分别取得的文件放入同一目录下的 `local/` 和 `deployed/`，使用相同文件名，同时登记原始比对报告。

人已经确认计划、研发项还在「可开始」时，如果项目明确授权把这条事项交给研发助手，可以保留原确认。项目的证据读取器通过 `owner_handoff` 提供针对这条事项的授权，以及飞书单独修改负责人后的操作回执。读取器须核实改前和改后都只有一位负责人，并核实原负责人和助手的飞书 ID。服务会对照这次变更的飞书历史、项目、计划版本和原确认人。无法核实时，事项保持等待。

交接前的人工确认也可以沿用。旧执行器须留有当时的点击、指定确认人和审批记录。如果新旧系统给同一人显示两种编号，服务可以通过同一条飞书回复核对作者、时间和正文。谁点了哪个节点、当时确认了哪一版，仍以原来的点击和正式表历史为准。

### 由表管理人一次订阅正式表

如果项目选择由人订阅，用同一个研发助手应用在飞书后台给**用户身份**开通 `docs:event:subscribe`，在用户身份的事件列表中添加多维表格记录变更事件，发布后核实已生效。保留原服务使用的应用身份事件。让这张表的管理人在独立的 CLI 配置中登录该应用，授权页应显示正确的组织和应用名。

在服务器上，用这个独立配置对指定的正式表执行下面两条命令。`manager_profile` 是此次授权的管理人配置，`base_token` 是正式表的文件标识。若用了独立的 CLI 配置目录，两条命令须使用同一目录。

```sh
lark-cli api POST "/open-apis/drive/v1/files/${base_token}/subscribe" --params '{"file_type":"bitable"}' --profile "$manager_profile" --as user
lark-cli api GET "/open-apis/drive/v1/files/${base_token}/get_subscribe" --params '{"file_type":"bitable"}' --profile "$manager_profile" --as user
```

第二条命令返回 `is_subscribe: true` 后，再从正式表的一次真实改动核对 Linux 收到的文件、表、记录和修订号。如果服务器没有收到，按飞书保存的修改时间到开发者后台查「事件日志」。只有订阅结果、没有对应投递时，保留原调度。服务平时用研发助手读取事项、发表评论、提醒同事。管理人的授权只用于这张表的订阅。若项目选择让机器人自己订阅，机器人需要拥有或管理这份文件。两种方式都应保留原调度，直到新服务通过切换检查。[订阅文件事件](https://open.feishu.cn/document/server-docs/docs/drive-v1/event/subscribe) · [查询订阅状态](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/drive-v1/file/get_subscribe)


## 启动同一个执行器

测试执行器要有自己的工作区、状态目录和项目配置。绑定 `cli_actor`（identity=bot、专用 profile、app_id、open_id）；`unattended.sh init --access bot --lark-profile …` 生成版本 3 的执行器配置。先测试扫描读全项目，再写一条测试计划并确认机器人署名、@ 到人和原生节点操作。不要用个人网页登录补做机器人被拒绝的操作。私信只在明确授权且收件人可核实的项目启用。

一条反馈拆成一条需求、这条需求只需一个研发项时，Codex 可以按已确认的范围和验收标准写计划。已获项目写入授权的执行器用 `service/plan_action.py` 完成写计划和发提醒。运行前从本地项目配置和该事项核对服务配置、需求记录编号、需求修订号和计划确认人。这四个参数由执行端指定。Codex 在标准输入提供 JSON，填写 `plan`（研发计划）、`scope`（工作范围）、`deliverable`（交付物）和 `verification_plan`（验证方法）。

```sh
python3 service/plan_action.py --config "$bridge_config" --requirement "$requirement_id" \
  --revision "$requirement_revision" --confirmer "$plan_confirmer_open_id" < plan.json
```

未核实本人确认时返回 `waiting`。只有 `notice=notified` 才表示已在这条研发项下读回给指定人员的提醒。中断后用相同参数加 `--reconcile`，省去 `< plan.json`。它查回原研发项和已有提醒，缺少可靠依据时保留待核实的结果。

新建研发项后，服务先把它纳入监听，再提醒计划确认人。这样，对方收到提醒就点「完成节点」，服务也能识别这条记录。若需要等下次读表，服务会保存这条待发提醒，读表后自动补发，无需再叫醒 Codex。期间计划、确认人或项目规则有变化时，先重新核对。已经尝试发送但没有收到飞书回执时，先到原研发项查评论。

如果用 MCP 工具把这个命令交给无人值守的 Codex，先用测试副本检查它能否实际写入。CLI 提示「工具需要审批」时，需求确认仍然有效。由项目负责人决定是否单独授权这个写计划工具。授权后恢复原会话，先查已有研发项和评论。人的计划确认仍在飞书完成。[Codex 工具权限配置](https://developers.openai.com/codex/config-schema.json)

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
   如果原本人网页执行器最后一次报“网页记录尚未加载完整”，用 `python3 service/reconcile_scan_error.py --source /原路径/runner.json` 重新读取同一个表。看到 `preview` 后，用相同命令加 `--apply 核对编号` 保存复查结果。工具核对原会话的完成回执，并保留原错误、检查点和刚出现的待办。原账号仍读不全时，先恢复它的读取能力。

2. 备份原配置及 crontab，保留该项目原始 cron 行（包含 PATH、包装脚本和标记）。按 [身份交接工具](identity_handoff.py) 的 `--help` 先预检，再在原工作区锁下交接。工具会先保存本次核对摘要，再依次暂停两个执行器和保存交接状态。中途断开时，用原来的两份配置和同一份摘要重跑 `--apply`。如果有人已手动暂停或恢复、原记录发生变化，先核实当前情况。保留状态目录里的交接文件。收到“正在运行”、可见范围缩小或不确定写入时停止切换；不要杀 worker 或手改状态文件。旧调度和候选都暂停后，按原 config 的精确标记移除那一条旧 cron；重新读取确认其他任务仍在。
3. `/etc/lark-dev-workflow/demo/previous-runners.json` 列出原 config 的绝对路径，例如 `["/srv/demo/old-runner.json"]`。全新测试执行器用 `[]`。systemd 在 dispatch 前检查本用户 crontab 上旧/新配置的路径或标记；还须人工核对自定义 wrapper、其他用户、systemd timer 没有第二个入口。
4. 只恢复候选执行器的交接暂停，启用单一服务并读回状态。确认一次人放行只产生一次执行；没有新通知时服务仍能补查或继续已获准工作。升级时先有序停止服务，等在途 tick 结束，再更换完整固定版本。
5. 回退先停止服务、等在途工作收尾；核对切换后是否已有新写入、未完成操作或新的人工决定。将新增情况对账后再恢复原会话及原配置。只恢复捕获的那一条 cron，不能用整份旧 crontab 覆盖别人刚加的任务。只有确认旧调度单独在跑后才报告回退完成。
