from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import (
    bot,
    calendar_caldav,
    health,
    items,
    plugin,
    reminders_icloud,
    rules,
    store,
)
from .config import CONFIG_FILE, SECRETS_FILE, SESSION_DIR, Config, Secrets


def cmd_auth(args: argparse.Namespace) -> int:
    cfg = Config.load()
    secrets = Secrets.load()
    if not cfg.apple_id:
        cfg.apple_id = input("Apple ID (邮箱): ").strip()
        cfg.save()
    both = not (args.caldav or args.icloud)

    if args.caldav or both:
        print(
            "\n[1/2] 日历（CalDAV）: 到 https://account.apple.com → 登录与安全 → App 专用密码，生成一个。"
        )
        secrets.caldav_app_password = getpass.getpass(
            "App 专用密码 (xxxx-xxxx-xxxx-xxxx): "
        ).strip()
        principal = calendar_caldav.connect(cfg.apple_id, secrets.caldav_app_password)
        calendar = calendar_caldav.get_or_create_calendar(principal, cfg.calendar_name)
        secrets.save()
        print(f"  ✓ CalDAV 可用，写入日历: {calendar.get_display_name()}")

    if args.icloud or both:
        print(
            "\n[2/2] 提醒事项（iCloud 网页接口）: 需要 Apple ID 主密码 + 双重认证验证码。"
        )
        password = reminders_icloud.prompt_password()
        api = reminders_icloud.login_interactive(cfg, password)
        if args.remember_password:
            secrets.apple_password = password
            print("  已保存密码（0600），会话过期时若 Apple 仍信任本机可自动续期。")
        else:
            secrets.apple_password = ""
        secrets.save()
        lst = reminders_icloud.pick_list(api, cfg.reminders_list)
        print(f"  ✓ 会话已受信任，提醒事项列表: {lst.title}")

    print(f"\n配置: {CONFIG_FILE}\n密钥: {SECRETS_FILE}\n会话: {SESSION_DIR}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = Config.load()
    if not cfg.apple_id:
        print("Not configured. Run: calendar-agent auth", file=sys.stderr)
        return 2
    report, principal = health.run_check(cfg, Secrets.load())
    out = health.report_dict(report)
    if report.needs_reauth and args.notify:
        out["notified"] = health.notify(cfg, report, principal)
    elif report.exit_code == 0:
        health.clear_notice(principal, cfg)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return report.exit_code


def cmd_smoke(args: argparse.Namespace) -> int:
    """Write one calendar event and one reminder with alerts, then clean up."""
    cfg = Config.load()
    secrets = Secrets.load()
    tz = ZoneInfo(cfg.timezone)
    start = (datetime.now(tz) + timedelta(hours=args.in_hours)).replace(
        second=0, microsecond=0
    )

    principal = calendar_caldav.connect(cfg.apple_id, secrets.caldav_app_password)
    calendar = calendar_caldav.get_or_create_calendar(principal, cfg.calendar_name)
    uid, href = calendar_caldav.add_event(
        calendar,
        calendar_caldav.EventSpec(
            summary="calendar-agent 测试日程",
            start=start,
            end=start + timedelta(minutes=30),
            description="smoke test",
            alerts_minutes=(60, 10),
            source="smoke",
        ),
        tz,
    )
    print(
        f"✓ 日程已写入 {cfg.calendar_name}: {start:%Y-%m-%d %H:%M}，提前 60/10 分钟提醒 (uid={uid})"
    )

    api, probe = reminders_icloud.open_service(cfg, secrets)
    if api is None:
        print(f"✗ 提醒事项不可用: {probe.status} {probe.reason}", file=sys.stderr)
        return 2
    lst = reminders_icloud.pick_list(api, cfg.reminders_list)
    reminder = reminders_icloud.create_reminder(
        api,
        lst.id,
        "calendar-agent 测试待办",
        start,
        cfg.timezone,
        notes="smoke test",
        early_hours=(1,),
    )
    print(
        f"✓ 待办已写入「{lst.title}」: 截止 {start:%H:%M}，提前 1 小时提醒 (id={reminder.id})"
    )

    if args.keep:
        print("--keep: 保留测试数据，去 iPhone/Mac 上确认后手动删除。")
        return 0
    input("去设备上确认后按回车删除测试数据…")
    calendar_caldav.delete_event(calendar, href)
    print("✓ 已删除测试日程")
    api.reminders.delete(api.reminders.get(reminder.id))
    print("✓ 已删除测试待办")
    return 0


EDITABLE = (
    "timezone",
    "calendar_name",
    "reminders_list",
    "model_connection_id",
    "model",
)


def cmd_propose(args: argparse.Namespace) -> int:
    """Agent thread: send confirmation cards. Items JSON on stdin."""
    cfg = Config.load()
    secrets = Secrets.load()
    if not bot.configured(cfg, secrets):
        print("Telegram 未配置", file=sys.stderr)
        return 2
    data = json.loads(sys.stdin.read())
    raw = data.get("items", data) if isinstance(data, dict) else data
    results = bot.send_cards(
        store.connect(),
        bot.bot_for(secrets),
        cfg,
        raw,
        "agent",
        data.get("note", "") if isinstance(data, dict) else "",
    )
    print(json.dumps(results, ensure_ascii=False))
    return 0 if all(r["ok"] for r in results) else 1


def cmd_upcoming(args: argparse.Namespace) -> int:
    cfg = Config.load()
    tz = ZoneInfo(cfg.timezone)
    start = datetime.now(tz)
    principal = calendar_caldav.connect(
        cfg.apple_id, Secrets.load().caldav_app_password
    )
    for e in calendar_caldav.upcoming(
        principal, start, start + timedelta(days=args.days)
    ):
        when = e["start"][:10] + (
            " 全天" if e["allDay"] else f" {e['start'][11:16]}–{e['end'][11:16]}"
        )
        print(f"{when}  {e['title']}  [{e['calendar']}]")
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    cfg = Config.load()
    tz = ZoneInfo(cfg.timezone)
    conn = store.connect()
    rows = conn.execute(
        "SELECT * FROM proposals WHERE status IN ('pending','editing','failed') ORDER BY created_at"
    ).fetchall()
    for row in rows:
        item = json.loads(row["item"])
        print(
            f"{row['id']}  {row['status']}  {item['title']}  {items.when_text(item, tz)}"
        )
    if not rows:
        print("没有待确认的卡片")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    current = rules.load()
    if args.op == "add":
        current.append(" ".join(args.text))
        rules.save(current)
    elif args.op == "remove":
        index = int(args.text[0]) - 1
        if not 0 <= index < len(current):
            print("没有这一条", file=sys.stderr)
            return 1
        current.pop(index)
        rules.save(current)
    for i, rule in enumerate(rules.load(), 1):
        print(f"{i}. {rule}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = Config.load()
    if args.op == "set":
        if args.key not in EDITABLE:
            print(f"只能修改: {', '.join(EDITABLE)}", file=sys.stderr)
            return 1
        value = args.value or ""
        if args.key == "telegram_owner_id":
            value = int(value)
        setattr(cfg, args.key, value)
        cfg.save()
    for key in (
        "apple_id",
        *EDITABLE,
        "telegram_owner_id",
        "agent_provider",
        "agent_id",
        "agent_model",
    ):
        print(f"{key} = {getattr(cfg, key)}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Conversation-driven setup; secrets come on stdin and are verified first."""
    from . import setup

    try:
        if args.op == "missing":
            gaps = setup.missing()
            if not gaps:
                print("都配置好了")
            for gap in gaps:
                print(f"- {gap['item']}: {gap['how']}")
            return 0
        if args.op == "caldav":
            print(setup.set_caldav_password(sys.stdin.read()))
        elif args.op == "telegram-token":
            print(setup.set_telegram_token(sys.stdin.read()))
        elif args.op == "reminders-account":
            print(f"已识别并保存：{setup.detect_reminders_account()}")
        elif args.op == "icloud-start":
            print(
                json.dumps(
                    setup.start_icloud_login(sys.stdin.read()), ensure_ascii=False
                )
            )
        elif args.op == "icloud-code":
            print(
                json.dumps(
                    setup.submit_icloud_code(args.value or ""), ensure_ascii=False
                )
            )
        elif args.op == "icloud-status":
            print(json.dumps(setup.auth_status(), ensure_ascii=False))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_slack(args: argparse.Namespace) -> int:
    """Slack (read-only) through Slack's MCP server."""
    from . import slack

    try:
        if args.op == "auth-url":
            print(slack.auth_url())
        elif args.op == "auth-finish":
            print(json.dumps(slack.auth_finish(sys.stdin.read()), ensure_ascii=False))
        elif args.op == "status":
            print(json.dumps(slack.status(), ensure_ascii=False))
        elif args.op == "tools":
            for tool in slack.Mcp().tools():
                ro = (tool.get("annotations") or {}).get("readOnlyHint") is True
                print(
                    f"{'R' if ro else 'W'} {tool['name']}: {(tool.get('description') or '')[:100]}"
                )
        elif args.op == "call":
            arguments = json.loads(sys.stdin.read() or "{}")
            print(
                json.dumps(slack.Mcp().call(args.tool, arguments), ensure_ascii=False)
            )
    except slack.SlackError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    out = plugin.status(Config.load())
    print(out["summary"])
    for section in out["sections"]:
        print(f"\n{section['title']}")
        for item in section["items"]:
            print(
                f"  {item['label']}: {item['statusText']}"
                + (f"（{item['detail']}）" if item["detail"] else "")
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="calendar-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("auth", help="授权 iCloud 日历 (CalDAV) 和提醒事项 (CloudKit)")
    p.add_argument("--caldav", action="store_true", help="只重设 App 专用密码")
    p.add_argument("--icloud", action="store_true", help="只重新登录提醒事项")
    p.add_argument(
        "--remember-password",
        action="store_true",
        help="保存 Apple ID 密码以便会话过期时静默续期（明文 0600 文件）",
    )
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser(
        "check", help="检查授权；exit 0=正常 1=暂时性错误 2=需要重新授权"
    )
    p.add_argument("--notify", action="store_true", help="需要重新授权时往日历发提醒")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("smoke", help="写入测试日程和待办")
    p.add_argument("--in-hours", type=float, default=2.0)
    p.add_argument("--keep", action="store_true")
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser(
        "propose", help="（对话线程用）发确认卡片，items JSON 从 stdin 读"
    )
    p.set_defaults(func=cmd_propose)
    p = sub.add_parser("upcoming", help="列出所有 iCloud 日历未来的日程")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_upcoming)
    p = sub.add_parser("pending", help="待确认的卡片")
    p.set_defaults(func=cmd_pending)
    p = sub.add_parser("rules", help="长期规则 show/add/remove")
    p.add_argument("op", choices=["show", "add", "remove"])
    p.add_argument("text", nargs="*")
    p.set_defaults(func=cmd_rules)
    p = sub.add_parser("config", help="查看或修改设置")
    p.add_argument("op", choices=["show", "set"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_config)
    p = sub.add_parser("setup", help="（对话线程用）检查缺失配置、保存并验证凭据")
    p.add_argument(
        "op",
        choices=[
            "missing",
            "caldav",
            "telegram-token",
            "reminders-account",
            "icloud-start",
            "icloud-code",
            "icloud-status",
        ],
    )
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser("slack", help="Slack 授权与只读查询")
    p.add_argument("op", choices=["auth-url", "auth-finish", "status", "tools", "call"])
    p.add_argument("tool", nargs="?")
    p.set_defaults(func=cmd_slack)
    p = sub.add_parser("status", help="各连接状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("plugin", help="Pockymoe 插件入口（stdin JSON → stdout JSON）")
    p.set_defaults(func=lambda _args: plugin.main())

    args = parser.parse_args()
    return args.func(args)
