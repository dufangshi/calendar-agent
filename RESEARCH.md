# 日历 Agent 调研（2026-10-20）

目标：Agent 通过 Google Calendar API 写入日程 → iPhone/Mac 系统日历通过已添加的 Google 账户同步显示。

## 同步链路
- iOS/macOS「设置 > 日历 > 账户」添加 Google 账户即为 CalDAV 双向同步。
- Google 账户在 Apple 端是 fetch（拉取）而非即时推送：iPhone 最快每 15 分钟，Mac 可设每 5 分钟；手动下拉可立即刷新。
- 备选：直接用 CalDAV 写 iCloud（caldav.icloud.com + Apple ID 专用密码），省去 Google 中转，Apple 端推送更及时，但失去 Google 生态。

## 可直接复用的成熟项目
| 项目 | 类型 | 说明 |
| --- | --- | --- |
| [nspady/google-calendar-mcp](https://github.com/nspady/google-calendar-mcp) | MCP server (TS, MIT) | ~1.2k★，最主流的 GCal MCP；多账户、多日历、批量创建、循环事件修改、free/busy、冲突检测 |
| [taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp) | MCP server (Python) | ~1.7k★，覆盖整个 Workspace（Gmail/Calendar/Drive/Tasks…），带 CLI，Desktop OAuth 客户端配置简单 |
| [gcalcli](https://github.com/insanum/gcalcli) | CLI (Python) | 老牌命令行；agenda/search/quick/add/edit/delete/import ics，适合作为 Agent 的 shell 工具 |
| [gcallm](https://pypi.org/project/gcallm/) | CLI | 用 Claude 把自然语言转成 GCal 事件 |
| [ccal](https://pypi.org/project/ccal/) | CLI | 自然语言或图片（截图/海报）→ 日历事件 |
| [O1af/gcalthing](https://github.com/O1af/gcalthing) | Web 聊天 | 带「先审批再写入」模式的 GCal 助手 |
| [ParthSareen/CalendarGPT](https://github.com/ParthSareen/CalendarGPT) | 短信 Agent | Twilio 短信 + GCal API |
| [derekantrican/GAS-ICS-Sync](https://github.com/derekantrican/GAS-ICS-Sync) | Apps Script | 把外部 ICS 订阅以 15 分钟频率写入 GCal（GCal 原生订阅 >24h 才刷新） |
| iCloud CalDAV MCP（如 [zwrong/iCalendar](https://github.com/zwrong/iCalendar)） | MCP / skill | 直写 iCloud 的备选路线 |

## 关键坑
- OAuth 同意屏幕在 Testing 状态时，calendar scope 的 refresh token 7 天过期；个人自用应切到 In Production（未验证应用也可用，仅有警告页）。
- Scope 最小化：只写事件用 `calendar.events`，只读用 `calendar.readonly`。
- 幂等/去重：给 Agent 创建的事件写 `extendedProperties.private`（如来源 ID + hash），更新时按它查找，避免重复。
- 时区：显式写 `timeZone`（如 Asia/Shanghai），避免 Apple 端显示漂移。
- 建议单独建一个「Agent」日历，方便整体回滚/隐藏。

## 补充（2026-10-28）
### gcalcli vs nspady/google-calendar-mcp
- gcalcli `add` 支持 `--reminder`、全天，但不支持循环规则；循环需拼 ics 走 `import`。输出是给人看的文本。
- nspady MCP 的 create/update-event 直接支持 reminders、recurrence (RRULE/EXDATE)、colorId、参会人；有 free/busy、冲突检测、批量创建、循环事件"仅此次/此后"修改，结构化 JSON 返回。→ 作为 Agent 工具层选 nspady。

### 提醒同步到 Apple
- GCal 弹窗提醒（`reminders.overrides` method=popup，API 最多 5 个）经 CalDAV 映射为 Apple 日历"提醒/Alert"，双向同步；email 提醒不会同步。
- Apple 日历无"同一提醒重复响 N 次"，做法是设多个提醒时间。iOS 编辑界面只显示 2 个提醒，超过 2 个的实际表现待实测。
- 注意 iOS 可对 Google 日历设默认提醒，可能与 Agent 写入的叠加。

### 待办（Task）
- Apple 待办在「提醒事项」App；iOS 13 起新版提醒事项不再走 CalDAV，第三方无法远程写入。
- Google Tasks 不同步到 iOS 提醒事项（iOS 添加 Google 账户没有 Tasks 选项）。
- 可行方案：常开的 Mac 上用 EventKit CLI（remindctl、BRO3886/rem、ekctl、eventkit-rs 带 MCP），写入后经 iCloud 同步到所有设备；或第三方 gtsync App 桥接 Google Tasks ↔ 提醒事项。

### 补充：不需要 Mac 的提醒事项开源方案（逆向 iCloud CloudKit）
- 核心库 [timlaing/pyicloud](https://github.com/timlaing/pyicloud)（picklepete/pyicloud 的活跃 fork，PyPI `pyicloud`，2026 年仍在发版）：`api.reminders` 走 icloud.com 同款 CloudKit 私有接口（ckdatabasews），支持增删改查、完成、截止时间、提前提醒 `set_early_reminders`、Urgent 闹钟、循环、标签、子任务、地点触发、URL 附件、增量变更游标。Linux 无头可跑。
- 上层项目：[nnabilsayed/icloud-mcp](https://github.com/nnabilsayed/icloud-mcp)（日历 CalDAV + 提醒 pyicloud，MCP）、[rafaelreverberi/Apple-Reminders-MCP](https://github.com/rafaelreverberi/Apple-Reminders-MCP)、[Ferhatk23/reminders-bridge](https://github.com/Ferhatk23/reminders-bridge)（Docker REST + Web UI）。都很新、star 很少，可参考不宜直接依赖。
- 认证：必须 Apple ID 主密码 + 2FA（CloudKit 拒绝 App 专用密码），`trust_session()` 后缓存 session，约 2 个月需重新 2FA。
- 风险：私有接口，Apple 改了就挂；开启「高级数据保护」需允许「网页访问 iCloud 数据」；国区 iCloud 需 china_mainland 选项（待验证）。
- 由此可选全 Apple 架构：日历走 iCloud CalDAV（专用密码，Apple 端即时推送）+ 提醒走 pyicloud，可完全不经过 Google。
