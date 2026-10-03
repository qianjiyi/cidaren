"""Manual wordbank maintenance, with no task requests or LLM calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .bank_store import BankError, BankStore, LEGACY_FILE


def _ids(text):
    try:
        result = {int(x) for x in text.replace(',', ' ').split()}
        if not result or min(result) <= 0:
            raise ValueError
        return result
    except ValueError as exc:
        raise argparse.ArgumentTypeError('请输入正整数记录ID，如 12,15,20') from exc


def _show(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _confirm(text):
    try:
        return input(text + ' 输入 YES 确认: ').strip() == 'YES'
    except EOFError:
        return False


def parser():
    p = argparse.ArgumentParser(description='词库迁移、预览、手动入库、清理和备份恢复')
    p.add_argument('--db', type=Path, help='默认使用项目 data/lexicon.sqlite3')
    commands = p.add_subparsers(dest='command')
    migrate = commands.add_parser('migrate', help='停止网页和任务后迁移旧 bank.json')
    migrate.add_argument('--source', type=Path, default=LEGACY_FILE)
    commands.add_parser('status', help='统计正式库、缓存和历史数据')
    commands.add_parser('backup', help='将当前源码和完整词库备份到 Git backups 分支')
    preview = commands.add_parser('preview', help='预览新增、重复、冲突、待验证及不可转换记录')
    preview.add_argument('--ids', type=_ids)
    preview.add_argument('--limit', type=int, default=30, help='每类最多显示数量，0 为全部')
    promote = commands.add_parser('promote', help='将已验证缓存手动入库；事务内移除对应缓存')
    selection = promote.add_mutually_exclusive_group(required=True)
    selection.add_argument('--ids', type=_ids)
    selection.add_argument('--all', action='store_true', help='全部已验证记录；待验证记录仍保留')
    clear = commands.add_parser('clear-cache', help='备份后仅清理缓存，正式库和历史数据保留')
    clear.add_argument('--yes', action='store_true', help='明确确认清理，跳过交互输入')
    export = commands.add_parser('export', help='指定路径导出版本JSON；不指定路径则执行 Git 备份')
    export.add_argument('output', type=Path, nargs='?')
    restore = commands.add_parser('restore', help='停止网页和任务后从 git:提交/分支、SQLite或版本JSON恢复')
    restore.add_argument('backup')
    restore.add_argument('--yes', action='store_true', help='明确确认恢复，跳过交互输入')
    return p


def _dispatch(args, bank):
    if args.command == 'migrate':
        _show(bank.migrate(args.source))
        return 0
    if args.command == 'restore':
        if not args.yes and not _confirm(f'将备份当前数据库并从 {args.backup} 恢复，要求网页和任务停止。'):
            print('已取消，未修改词库。')
            return 0
        _show(bank.restore(args.backup))
        return 0
    bank.validate()
    if args.command == 'status':
        _show(bank.status())
    elif args.command == 'preview':
        rows, issues = bank.preview(args.ids), bank.legacy_issues()
        limit = args.limit
        if limit < 0:
            raise BankError('--limit 不能小于0')
        _show({'cache_total':len(rows), 'cache':rows[:limit] if limit else rows,
               'legacy_issues_total':len(issues), 'legacy_issues':issues[:limit] if limit else issues,
               'note':'待验证和已否定记录不能入库；冲突答案将分别保留；--limit 0 显示全部'})
    elif args.command == 'promote':
        _show(bank.promote(args.ids))
    elif args.command == 'clear-cache':
        if not args.yes and not _confirm(f"将清理 {bank.status()['cache']} 条缓存，先备份数据库。"):
            print('已取消，未修改词库。')
            return 0
        _show(bank.clear_cache())
    elif args.command == 'export':
        _show(bank.export(args.output) if args.output else bank.backup())
    elif args.command == 'backup':
        _show(bank.backup())
    return 0


def _menu(p, bank):
    print('词库管理：1 迁移 / 2 状态 / 3 预览 / 4 全部已验证入库 / 5 选择入库 / 6 清理缓存 / 7 Git备份 / 8 恢复 / 9 指定路径导出 / 0 退出')
    try:
        choice = input('请选择: ').strip()
        mapping = {'1':['migrate'], '2':['status'], '3':['preview'], '6':['clear-cache'], '7':['backup']}
        if choice == '4':
            _dispatch(p.parse_args(['preview']), bank)
            if not _confirm('将全部已验证记录入库，冲突保留，待验证记录跳过。'):
                return 0
            command = ['promote','--all']
        elif choice == '5':
            _dispatch(p.parse_args(['preview','--limit','0']), bank)
            command = ['promote','--ids',input('输入记录ID，如 12,15,20: ')]
        elif choice == '8':
            command = ['restore',input('输入 git:backups、git:提交SHA 或备份文件路径: ').strip().strip('"')]
        elif choice == '9':
            output = input('输入导出JSON文件路径: ').strip().strip('"')
            if not output:
                print('未提供导出路径，已取消。')
                return 0
            command = ['export', output]
        elif choice in mapping:
            command = mapping[choice]
        else:
            return 0
        return _dispatch(p.parse_args(command), bank)
    except EOFError:
        return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    p = parser()
    args = p.parse_args(argv)
    bank = BankStore(args.db)
    try:
        return _dispatch(args, bank) if args.command else _menu(p, bank)
    except (BankError, OSError) as exc:
        print(f'词库操作失败: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
