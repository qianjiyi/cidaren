from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from cidaren import bank_tools
from cidaren.bank_store import BankError, BankStore
from cidaren.git_backups import GitBackups, LEGACY, LEXICON, MANIFEST
from tests.test_bank_store import bank, choice


@pytest.mark.parametrize('operation', ['promote', 'clear_cache', 'restore'])
def test_upload_failure_preserves_entire_database(bank, monkeypatch, tmp_path, operation):
    bank.record(choice(), 7, 'test', 'confirmed')
    export = tmp_path / 'explicit.json'
    bank.export(export)
    before = bank._snapshot()
    def fail(*args, **kwargs):
        raise BankError('Git 上传失败')
    monkeypatch.setattr(bank._backend(), 'publish', fail)
    with pytest.raises(BankError, match='上传失败'):
        getattr(bank, operation)(export) if operation == 'restore' else getattr(bank, operation)()
    assert bank._snapshot() == before
    assert not (bank.path.parent / 'backups').exists()
    assert not list(bank.path.parent.glob('.restore-*'))


def test_first_migration_upload_failure_does_not_create_database(tmp_path, memory_backups, monkeypatch):
    source = tmp_path / 'bank.json'
    source.write_text('{}', encoding='utf-8')
    store = BankStore(tmp_path / 'data' / 'lexicon.sqlite3')
    monkeypatch.setattr(memory_backups, 'publish', lambda *a, **k:(_ for _ in ()).throw(BankError('离线')))
    with pytest.raises(BankError, match='离线'):
        store.migrate(source)
    assert not store.path.exists()
    assert source.read_bytes() == b'{}'


@pytest.mark.parametrize('operation', ['promote', 'clear_cache', 'restore'])
def test_task_write_during_upload_aborts_maintenance(bank, monkeypatch, tmp_path, operation):
    bank.record(choice(), 7, 'test', 'confirmed')
    export = tmp_path / 'explicit.json'
    bank.export(export)
    backend = bank._backend()
    publish = backend.publish
    def concurrent_write(*args, **kwargs):
        result = publish(*args, **kwargs)
        # A separate connection commits while the upload is in progress.
        BankStore(bank.path).record(choice(word='new knowledge'), 42, 'concurrent', 'confirmed')
        return result
    monkeypatch.setattr(backend, 'publish', concurrent_write)
    with pytest.raises(BankError, match='新写入'):
        getattr(bank, operation)(export) if operation == 'restore' else getattr(bank, operation)()
    assert bank.status()['cache'] == 2
    assert bank.status()['formal'] == 0
    assert bank.lookup(choice(word='new knowledge')).answer == 42


def test_snapshot_after_clear_restores_deleted_highest_sequence(bank, tmp_path):
    old = bank.record(choice(), 7, 'test', 'confirmed')
    bank.clear_cache()
    backup = bank.backup()
    content = json.loads(bank._backend().snapshots[backup['commit']][LEXICON])
    assert content['tables']['records'] == []
    assert content['sequences']['records'] >= old
    other = BankStore(tmp_path / 'new computer' / 'lexicon.sqlite3')
    other.restore(backup)
    assert other.record(choice(word='new'), 7, 'test', 'confirmed') > old


def test_v1_json_remains_restorable(bank, tmp_path):
    bank.record(choice(), 7, 'test', 'confirmed')
    output = tmp_path / 'old-format.json'
    bank.export(output)
    data = json.loads(output.read_bytes())
    data['version'] = 1
    data.pop('schema_version')
    data.pop('sequences')
    output.write_text(json.dumps(data), encoding='utf-8')
    other = BankStore(tmp_path / 'other.sqlite3')
    other.restore(output)
    assert other.lookup(choice()).answer == 7


@pytest.mark.parametrize('sequence', [-1, True, '2', 0, 9223372036854775808])
def test_corrupt_sequence_rejected_without_replacing_database(bank, tmp_path, sequence):
    bank.record(choice(), 7, 'test', 'confirmed')
    output = tmp_path / 'corrupt.json'
    bank.export(output)
    data = json.loads(output.read_bytes())
    data['sequences']['records'] = sequence
    output.write_text(json.dumps(data), encoding='utf-8')
    before = bank._snapshot()
    with pytest.raises(BankError, match='损坏'):
        bank.restore(output)
    assert bank._snapshot() == before


def test_cli_git_backup_export_alias_and_restore(bank, capsys):
    bank.record(choice(), 7, 'test', 'confirmed')
    assert bank_tools.main(['--db', str(bank.path), 'backup']) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['reference'].startswith('git:')
    assert bank_tools.main(['--db', str(bank.path), 'export']) == 0
    capsys.readouterr()
    bank.record(choice(word='later'), 7, 'test', 'confirmed')
    assert bank_tools.main(['--db', str(bank.path), 'restore', first['reference'], '--yes']) == 0
    assert bank.status()['cache'] == 1
    assert not (bank.path.parent / 'backups').exists()


def _git(root, *args, input=None):
    result = subprocess.run([shutil.which('git'), *args], cwd=root, input=input, capture_output=True)
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    return result.stdout


@pytest.fixture
def repository(tmp_path):
    if not shutil.which('git'):
        pytest.skip('Git not installed')
    remote = tmp_path / 'remote.git'
    root = tmp_path / 'project'
    remote.mkdir()
    root.mkdir()
    _git(remote, 'init', '--bare', '--initial-branch=main')
    _git(root, 'init', '--initial-branch=main')
    _git(root, 'config', 'user.name', 'Backup Tests')
    _git(root, 'config', 'user.email', 'backup-tests@example.invalid')
    (root / 'cidaren').mkdir()
    (root / 'cidaren' / 'bank_store.py').write_text('# source\n', encoding='utf-8')
    (root / 'README.md').write_text('initial\n', encoding='utf-8')
    (root / '.gitignore').write_text('.env\n.capture/\ndata/\n', encoding='utf-8')
    (root / '.env.example').write_text('USERTOKEN=\nLLM_KEY=\n', encoding='utf-8')
    (root / '词库管理.bat').write_bytes('@echo off\r\n'.encode('utf-8'))
    _git(root, 'add', '.')
    _git(root, 'commit', '-m', 'initial source')
    _git(root, 'tag', 'original-backup')
    _git(root, 'remote', 'add', 'origin', str(remote))
    _git(root, 'push', 'origin', 'main', 'original-backup')
    return root, remote, GitBackups(root)


def test_real_git_snapshot_preserves_checkout_main_and_excludes_secrets(repository, tmp_path):
    root, remote, backend = repository
    original_head = _git(root, 'rev-parse', 'HEAD')
    original_index = _git(root, 'ls-files', '--stage')
    (root / 'README.md').write_text('uncommitted source update\n', encoding='utf-8')
    (root / 'cidaren' / 'new.py').write_text('# new source\n', encoding='utf-8')
    (root / '.env').write_text('USERTOKEN=secret-never-publish\n', encoding='utf-8')
    (root / '.env.private').write_text('LLM_KEY=secret-key\n', encoding='utf-8')
    (root / '.capture').mkdir()
    (root / '.capture' / 'private.key').write_text('certificate secret', encoding='utf-8')
    status = _git(root, 'status', '--porcelain')
    store = BankStore(root / 'data' / 'lexicon.sqlite3', backup_backend=backend)
    source = tmp_path / 'input.json'
    source.write_text('{}', encoding='utf-8')
    store.migrate(source)
    store.record(choice(), 7, 'test', 'confirmed')
    first = store.backup()
    second = store.backup()
    assert first['commit'] != second['commit']
    assert _git(remote, 'rev-parse', 'main') == original_head
    assert _git(root, 'rev-parse', 'HEAD') == original_head
    assert _git(root, 'ls-files', '--stage') == original_index
    assert _git(root, 'status', '--porcelain') == status
    assert _git(root, 'rev-parse', 'original-backup') == original_head
    names = _git(root, 'ls-tree', '-r', '--name-only', second['commit']).decode().splitlines()
    assert LEXICON in names and MANIFEST in names and 'cidaren/new.py' in names
    assert not any(n.startswith(('.capture/', '.env.private', 'data/')) or n == '.env' for n in names)
    assert _git(root, 'show', f"{second['commit']}:README.md") == (root / 'README.md').read_bytes()
    assert _git(root, 'show', f"{second['commit']}:词库管理.bat") == (root / '词库管理.bat').read_bytes()
    candidate = tmp_path / 'download'
    candidate.mkdir()
    downloaded = backend.download(first['reference'], candidate)
    assert json.loads(downloaded['path'].read_bytes())['sequences']['records'] == 1
    assert not (root / 'data' / 'backups').exists()


def test_git_restores_first_migration_input_and_old_full_snapshot(repository, tmp_path):
    root, remote, backend = repository
    source = tmp_path / 'legacy.json'
    source.write_text(json.dumps({'22::derive::yes|no':{'ans':0,'stem':'derive'}}), encoding='utf-8')
    store = BankStore(root / 'data' / 'lexicon.sqlite3', backup_backend=backend)
    migration = store.migrate(source)['backup']
    store.record(choice(), 7, 'test', 'confirmed')
    first = store.backup()
    store.record(choice(word='new'), 7, 'test', 'confirmed')
    store.backup()
    fresh = BankStore(tmp_path / 'fresh' / 'lexicon.sqlite3', backup_backend=backend)
    fresh.restore(first)
    assert fresh.status()['cache'] == 1
    fresh.restore(migration)
    assert fresh.status()['cache'] == 0 and fresh.status()['legacy'] == 1


@pytest.mark.parametrize('phase', ['push', 'verify'])
def test_real_git_push_or_verification_failure_prevents_clear(repository, tmp_path, monkeypatch, phase):
    root, remote, backend = repository
    store = BankStore(root / 'data' / 'lexicon.sqlite3', backup_backend=backend)
    source = tmp_path / 'source.json'
    source.write_text('{}', encoding='utf-8')
    store.migrate(source)
    store.record(choice(), 7, 'test', 'confirmed')
    before = store._snapshot()
    if phase == 'push':
        run = backend._run
        def fail(*args, **kwargs):
            if args[0] == 'push':
                raise BankError('推送被拒绝')
            return run(*args, **kwargs)
        monkeypatch.setattr(backend, '_run', fail)
    else:
        monkeypatch.setattr(backend, '_verify_remote', lambda *a:(_ for _ in ()).throw(BankError('远端核验失败')))
    with pytest.raises(BankError):
        store.clear_cache()
    assert store._snapshot() == before


def test_intervening_remote_backup_rejects_push_without_force(repository, monkeypatch):
    root, remote, backend = repository
    artifacts = {LEGACY:b'{}'}
    first = backend.publish(artifacts)
    stale = backend._remote_refs()
    newer = backend.publish(artifacts)
    monkeypatch.setattr(backend, '_remote_refs', lambda:stale)
    with pytest.raises(BankError, match='备份失败'):
        backend.publish(artifacts)
    assert _git(remote, 'rev-parse', 'backups').decode().strip() == newer['commit']
    assert _git(root, 'merge-base', first['commit'], newer['commit']).decode().strip() == first['commit']


def test_remote_manifest_corruption_is_rejected(repository, tmp_path):
    root, remote, backend = repository
    original = backend.publish({LEGACY:b'{}'})
    _git(root, 'checkout', '-b', 'tampered', original['commit'])
    (root / LEGACY).write_text('{"tampered":true}', encoding='utf-8')
    _git(root, 'add', LEGACY)
    _git(root, 'commit', '-m', 'corrupt payload without matching checksum')
    _git(root, 'push', 'origin', 'HEAD:backups')
    output = tmp_path / 'download'
    output.mkdir()
    with pytest.raises(BankError, match='校验值'):
        backend.download('git:backups', output)
    assert list(output.iterdir()) == []


def test_credentials_in_export_or_template_prevent_push(repository):
    root, remote, backend = repository
    with pytest.raises(BankError, match='凭据'):
        backend.publish({LEGACY:b'{"usertoken":"secret"}'})
    (root / '.env.example').write_text('LLM_KEY=do-not-publish\n', encoding='utf-8')
    with pytest.raises(BankError, match='凭据'):
        backend.publish({LEGACY:b'{}'})
    assert _git(remote, 'for-each-ref', 'refs/heads/backups') == b''


@pytest.mark.parametrize('reference', ['git:--all', 'git:../main', 'git:main', 'git:HEAD~1', 'git:a;echo'])
def test_unsafe_or_non_backup_references_rejected(repository, tmp_path, reference):
    backend = repository[2]
    backend.publish({LEGACY:b'{}'})
    with pytest.raises(BankError):
        backend.download(reference, tmp_path)


def test_git_command_timeout_reports_stage(repository, monkeypatch):
    backend = repository[2]
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 60)
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(BankError, match='fetch.*超时'):
        backend._run('fetch', 'origin')


def test_boolean_schema_version_is_rejected(bank, tmp_path):
    output = tmp_path / 'corrupt-version.json'
    bank.export(output)
    data = json.loads(output.read_bytes())
    data['schema_version'] = True
    output.write_text(json.dumps(data), encoding='utf-8')
    before = bank._snapshot()
    with pytest.raises(BankError, match='版本'):
        bank.restore(output)
    assert bank._snapshot() == before
