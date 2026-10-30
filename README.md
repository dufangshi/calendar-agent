# calendar-agent

把日程和待办写进 iCloud（苹果日历 + 提醒事项），苹果账户是最终的权威数据源。

- 日历：iCloud CalDAV + App 专用密码（Apple 公开接口，长期有效）
- 提醒事项：iCloud CloudKit 网页私有接口（[pyicloud](https://github.com/timlaing/pyicloud)），需要 Apple ID 密码 + 双重认证，会话会定期过期

## 授权

```bash
uv run calendar-agent auth            # 两项都配置
uv run calendar-agent auth --caldav   # 只重设 App 专用密码
uv run calendar-agent auth --icloud   # 只重新登录提醒事项（密码 + 2FA）
```

授权前的准备：
1. 在 https://account.apple.com → 登录与安全 → App 专用密码，生成一个（给日历用）。
2. 如果开了「高级数据保护」，需要在 iPhone 设置 → Apple 账户 → iCloud 打开「在网页上访问 iCloud 数据」。
3. 加 `--remember-password` 会把 Apple ID 密码存进 0600 文件，这样网页会话过期时，只要 Apple 仍信任本机就能静默续期；不加就每次过期都提醒你重新登录。

所有凭据都存在仓库外：`~/.config/calendar-agent/`（配置、密钥）、`~/.local/share/calendar-agent/`（iCloud 会话、状态）。

## 检查与重新授权提醒

```bash
uv run calendar-agent check --notify
```

退出码：0 = 正常，1 = 暂时性错误（网络等，不提醒），2 = 需要重新授权。
提醒事项会话失效时，会通过 CalDAV 在「Agent」日历里写一条 2 分钟后开始、带提醒的日程，所有苹果设备都会响；同一种失效 12 小时内只提醒一次，恢复正常后自动删除这条提醒日程。
CalDAV 本身失效时无法写日历，可以设置环境变量 `CALENDAR_AGENT_NOTIFY_COMMAND` 作为备用通知（消息内容在 `$CALENDAR_AGENT_MESSAGE` 里）。

适合放进 cron，例如每小时一次：

```cron
0 * * * * cd ~/dev/remoteCodex/calendar-agent && ~/.local/bin/uv run calendar-agent check --notify >/dev/null
```

## 写入测试

```bash
uv run calendar-agent smoke          # 写一条日程 + 一条待办，在设备上确认后按回车删除
uv run calendar-agent smoke --keep   # 保留测试数据
```

## 作为 Pockymoe 插件运行

需要 Pockymoe ≥ 0.13.41。

```bash
pockymoe plugin install <本仓库的 git 地址或本地路径>
pockymoe plugin enable dufangshi.calendar-agent
```

- 每 60 秒运行一次 `tick`：长轮询 Telegram（只接受 `telegram_owner_id` 的私聊），处理按钮，转发对话线程的回复；每 30 分钟检查一次 iCloud 授权。
- Settings → 插件 → 日程管家：只读显示 `status` 的连接状态和最近运行。

### Telegram

- bot token 存在 `~/.config/calendar-agent/secrets.json`（0600），`telegram_owner_id` 在 `config.toml`。
- 发一句话 → 轻量模型（Pockymoe 已保存的连接，`model_connection_id`）直接出确认卡片；
  规划、查日程、改设置等交给对话线程。没配 `model_connection_id` 时全部交给对话线程。
- 卡片按钮：✅ 添加（写入 iCloud）· ✏️ 修改（回复卡片说怎么改）· ✖️ 不要；添加后可 ↩️ 撤销。
- `/pending` 重发待确认的卡片，`/help` 用法。

### 对话线程

插件用 `workspaceEnsure` 在 `~/.local/share/calendar-agent/agent` 建工作区，按
`agent_provider` / `agent_id` / `agent_model`（默认 DeepSeek Harness）建一个「日程管家」线程。
线程读同目录下的 AGENTS.md（来自 `src/calendar_agent/agent_skill.md`），通过这些命令工作：
`propose`、`upcoming`、`pending`、`rules`、`config`、`status`。它每轮的最后一条回复会发回 Telegram。

## 开发

```bash
uv run pytest -q
uvx ruff check src && uvx ruff format --check src
```
