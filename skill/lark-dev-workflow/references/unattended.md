# Linux 上定时处理项目

这是可选入口：用户明确要求该 Linux 机器定时处理指定项目时才安装。普通接入、查询或运行 Skill 不安装定时任务。它使用系统 `crontab` 和 Codex CLI，不依赖 Codex App，不需要 webhook 或常驻监听服务。

本文供配置与管理运行器时使用。已由脚本启动的工作轮次直接遵循 [本轮工作规则](unattended-worker.md)，不重复安装。用户只要方案或操作步骤时，只提供说明，不执行后面的安装命令。

默认每 5 分钟读一次绑定项目的记录和评论。有变化才启动 Codex；已有工作还需继续时，也会在下一个合适周期启动。AI 在事项评论中提问，等到后续检查发现回复再推进。每轮继续保存的同一个 Codex 会话，不使用 `--last` 猜会话。

## 准备

- Linux、可用的用户 `crontab`、Python 3.9+ 和 Codex CLI。`.sh` 是统一入口，Python 部分只需标准库，无需 `jq`。
- 选择已有的本人访问方式：CLI 模式需要 Lark CLI 与已核实的用户 profile；网页模式需要 Node.js 22+ 和本机已登录的浏览器，按 [本人网页接入](browser-access.md) 配置。不因缺 profile 自动创建应用、bot 或选用其他项目的应用。
- 在执行 cron 的同一 Linux 用户下完成 Codex 登录，并核实所选飞书途径的本人身份。凭据保留在原 CLI 或浏览器中，不写进项目配置、Skill 或 cron。
- 项目绑定使用格式 2、`scope.mode=project_record`，表、字段和项目关系已按实际结构核实。Base-only 项目可保留 `documents={}`。CLI 可读取已绑定 Docx；首版网页读取不支持非空 Docx 绑定，不能为通过检查删掉原绑定。旧人工局部映射不能只改格式号就用于自动扫描。
- Linux 上已有项目工作目录和所选访问方式需要的工具；CLI 操作复用相应 `lark-*` 技能，网页遵守所用浏览器工具规则。沿用该项目的执行权限与业务授权。脚本不添加全盘访问或绕过沙箱；没有执行权限的动作会失败并留下原因。

首先核对本机 Codex 版本与所选访问方式。Codex 需支持 `exec`、按会话 ID `exec resume`、`--json`、`--output-schema` 和 `--output-last-message`。CLI 协议按 Codex CLI 0.159.2 和 Lark CLI 1.0.94 的接口编写；不依赖实时事件订阅。项目实际可读写范围仍需在目标 Linux 机器核实。

旧反馈表没有“反馈项目”字段时，先按 [旧表适配](project-mapping.md#没有反馈项目的旧反馈表) 检查已有字段。可明确选择只扫描已关联事项；这不包括未归属的新反馈，不能当作完整反馈入口已接通。本地映射或离线测试也不能代替真实本人登录。

自动扫描需要真实项目记录、`scope.project_table_id`、各表的 `id` 与实际 `fields` 映射。非项目表的 `project` 关联必须指向同一项目表。默认 `project_input` 模式接收未归属反馈，还需要顶层 `project_number`、项目表的 `number`，以及反馈表的 `project_input` 和 `status`；`linked_only` 不要求这些分流字段。但配置中已声明的项目编号仍须与实际记录一致；已绑定 Docx 时也必须保留并核对编号，以免漏掉本项目章节的评论。补齐映射时先只读核实字段 ID、类型和关联目标，另存候选配置并比较原项目身份，不机械改键或编造字段。

无人值守的新会话不继承当前聊天。把已经确认的沟通、授权和暂停约束写入本机项目规则，并在项目绑定的 `governance.rules_reference` 中引用。该字段用于无人值守时是本地文件路径：相对路径以 `workdir` 为基准，也接受绝对路径；运行器每轮载入实际内容，引用不可读就停止该轮。原有项目规则继续适用，不把某个项目的禁止评论约定写成所有项目的默认规则。

## 配置并试跑

以下路径是示例，换成 Linux 机器上的实际绝对路径。`TASK_SKILL` 指向安装后的 Skill 文件夹；安装器通常使用 `~/.agents/skills`，旧手动安装也可能在 `~/.codex/skills`，以实际安装输出为准。不要指向本机 Mac 路径。配置和运行状态放到私有目录，避免随项目提交。

下面用于已有 CLI 用户 profile。网页模式使用 [网页初始化命令](browser-access.md#配置与只读试跑)，不填写 `--lark-profile`。两种方式共用后续的查看、暂停和恢复命令；不能直接把旧运行状态改成另一种登录方式。

```sh
TASK_SKILL="$HOME/.agents/skills/lark-dev-workflow"
TASK_CONFIG="$HOME/.local/state/lark-dev-workflow/my-project.json"

sh "$TASK_SKILL/scripts/unattended.sh" init \
  --config "$TASK_CONFIG" \
  --project-config /srv/my-project/.lark-dev-workflow/project.json \
  --workdir /srv/my-project \
  --lark-profile my-profile \
  --interval-minutes 5

sh "$TASK_SKILL/scripts/unattended.sh" check --config "$TASK_CONFIG"
sh "$TASK_SKILL/scripts/unattended.sh" scan --config "$TASK_CONFIG"
```

`init` 只创建本地配置和私有状态目录，不覆盖已有文件、不登录、不创建项目、不安装 cron。CLI 运行配置为格式 1，网页配置为格式 2；它们与项目绑定的格式号不是同一项。需要指定 Codex 配置时在 `init` 加 `--codex-profile`。`check` 只检查本地路径和绑定；`scan` 才实际只读飞书，返回记录／评论数量与覆盖范围，不启动 Codex，也不把事项记为已处理。完整扫描失败时保留原进度。

先在目标机器核实真实记录及完整评论能读到，再在项目已经授权 AI 处理事项的前提下执行一次。网页入口按 [网页接入说明](browser-access.md#完整性与继续运行) 核对当前可见评论和已加载回复，包含已解决索引；未完成这项核实时保持暂停，不因离线测试通过执行 `tick` 或安装 cron。

```sh
sh "$TASK_SKILL/scripts/unattended.sh" tick --config "$TASK_CONFIG"
sh "$TASK_SKILL/scripts/unattended.sh" status --config "$TASK_CONFIG"
```

`tick` 会启动真正的 Codex 执行工作，可能按项目已有授权写记录、回复评论或修改代码。扫描 helper 本身只读，不证明 Worker 的写入和评论途径可用。核实本轮结果与每个实际写入的独立读回，且用户已要求启用周期运行后，才执行：

```sh
sh "$TASK_SKILL/scripts/unattended.sh" cron-preview --config "$TASK_CONFIG"
sh "$TASK_SKILL/scripts/unattended.sh" install-cron --config "$TASK_CONFIG"
```

安装只维护带本配置标记的一行，不覆盖其他 cron 条目；修改前备份，写入后读回。重复安装同一配置不会增加第二条。不要另用 `sudo` 安装到 root；那会换成另一个用户的账号、文件和 crontab。

`interval_minutes` 支持 1、2、3、4、5、6、10、12、15、20、30、60。想改间隔，在本地 JSON 修改后重新执行 `install-cron`。配置保存工具的绝对路径和固定 PATH，避免 cron 找不到登录 shell 中的程序；移动 Skill 或工具后核对配置并重新预览／安装。

## 日常查看、暂停和移除

```sh
sh "$TASK_SKILL/scripts/unattended.sh" status --config "$TASK_CONFIG"
sh "$TASK_SKILL/scripts/unattended.sh" pause --config "$TASK_CONFIG"
sh "$TASK_SKILL/scripts/unattended.sh" resume --config "$TASK_CONFIG"
sh "$TASK_SKILL/scripts/unattended.sh" remove-cron --config "$TASK_CONFIG"
```

`pause` 让后续周期跳过，正在执行的这一轮继续收尾；`resume` 允许之后继续，不立即启动 Codex。`remove-cron` 只移除本配置的调度行，保留日志和进度，也不强杀正在运行的工作。`status` 的 `enabled` 只表示未暂停，不证明 cron 服务正在运行或项目已经完成。

业务上等人回复不需要 `pause`：保留检查，收到新答复才能继续。暂停某一事项只影响那一项；暂停整个运行器后，它不会读取 Base 中的新回复，需在 Linux 上使用 `resume` 恢复。

配置中的 `state_dir` 保存 `state.json`、`cron.log`、评论观察记录和每轮 `runs/<运行编号>/` 下的输入、Codex 事件、错误及结果。CLI 使用 `comments-state.json`，网页使用独立的 `browser-comments-state.json`，不自动混用或迁移。它们含私有业务资料，仅留在 Linux 私有目录，不放公开仓库或飞书表格。首版保留这些文件供恢复使用，长期运行时需按实际保留需求维护日志空间。

## 重复运行与失败恢复

- 同一工作目录一次只运行一个 worker，后来的 cron 周期直接跳过。本地锁不能协调不同 Linux 机器或不同检出目录；同一项目首版只启用一个运行器，不能据此声称跨机器防重。
- 运行成功只登记“开始这一轮时看到的内容”。期间新来的回复留到下一轮处理，不会因为结束时重新扫了一遍而被跳过。这份调度进度不代替共享 Base 的业务查重、正式回写或评论处理确认。
- AI 写回可能多触发一轮；没有新事实时不重复回执或刷状态。同账号发言不能全忽略，否则会漏掉人的回复。连续多轮没有可推进事项却持续出现变化时暂停，保留未处理变化，检查是否在重复写回。
- 默认一轮 Codex 最多 30 分钟，完整采集最多 5 分钟。采集失败不启动 Codex、不推进记录；执行失败、超时或结果不完整时保留会话与日志并暂停，避免未知写入被盲目重做。
- 核对中断轮次的日志和远端实际结果后，执行 `recover --config "$TASK_CONFIG"`。它保留旧进度、继续原会话，并要求先核实已经生效的操作；如果上一轮仍持有锁则拒绝恢复。普通暂停使用 `resume` 即可。

正常 `continue` 会在后续周期推进，即使表格没变；等外部任务时可按结果中的延迟回查。除此之外默认每天做一次业务检查，避免只盯新评论漏掉已有待办。具体 worker 指令见 [每轮工作规则](unattended-worker.md)。

本工具按周期重新读取记录及完整评论，不承诺即时响应；周期内新增又撤回、两次读取之间的短暂变化不一定可见，也不是审计日志。无法定位的评论只提示待核对，不擅自归给当前项目。本人登录失效、CLI／网页读取接口变化或 cron 服务未运行都需按日志处理，不能把安装成功写成长期可用已获验证。
