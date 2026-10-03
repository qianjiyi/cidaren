"""Source and wordbank snapshots on a dedicated Git branch.

An isolated index builds each commit; the checkout, normal index and main branch
are never changed. Only verified remote commits count as completed backups.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

from .bank_store import BankError, ROOT
from .config import _parse_env_value

MANIFEST = 'backup/manifest.json'
LEXICON = 'backup/lexicon.json'
LEGACY = 'backup/legacy-input.json'
ARTIFACTS = {LEXICON, LEGACY}
SECRET_FIELDS = {'usertoken', 'abc', 'auth_v', 'authorization', 'authorization-v',
                 'llm_key', 'api_key', 'apikey', 'access_token', 'refresh_token', 'password'}
PRIVATE_SCREENSHOTS = frozenset({
    'docs/image.png',
    'docs/image copy.png',
    'docs/image copy 2.png',
    'docs/image copy 3.png',
    'docs/image copy 4.png',
})


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _private_payload(value):
    if isinstance(value, dict):
        return any(str(k).casefold() in SECRET_FIELDS and v not in (None, '') or _private_payload(v)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(_private_payload(v) for v in value)
    # SQLite stores some structured values as JSON text.
    if isinstance(value, str) and value.startswith(('{', '[')):
        try:
            return _private_payload(json.loads(value))
        except (ValueError, RecursionError):
            pass
    return False


def _excluded(name):
    parts = PurePosixPath(name).parts
    base = parts[-1].casefold()
    if any(p.casefold() in {'data', 'backup', 'backups', '.capture', '.git', '.venv',
                           '__pycache__', '.pytest_cache', '.test-cache', '.test-runs',
                           '.test-tmp', '.pytest-tmp', 'htmlcov'} or p.startswith('.test-') for p in parts):
        return True
    if base == '.env' or base.startswith('.env.') and base != '.env.example':
        return True
    return base.endswith(('.pem', '.key', '.pfx', '.p12', '.cer', '.crt', '.log', '.sqlite3',
                          '.sqlite', '.db', '.zip', '.bundle', '.pyc', '.pyo', '.tmp'))


def _upload_excluded(name):
    """Apply current upload rules without invalidating older backup manifests."""
    parts = PurePosixPath(name).parts
    base = parts[-1].casefold()
    return (_excluded(name) or name.casefold() in PRIVATE_SCREENSHOTS
            or base.startswith('.env') and base != '.env.example'
            or any(p.casefold().endswith('.egg-info') for p in parts)
            or base.startswith('.coverage')
            or base in {'coverage.xml', 'proxy-recovery.json', 'last-proxy-recovery.json'}
            or base.endswith(('.lock', '.sqlite3-wal', '.sqlite3-shm', '.sqlite3-journal',
                              '.sqlite-wal', '.sqlite-shm', '.sqlite-journal',
                              '.db-wal', '.db-shm', '.db-journal')))


class GitBackups:
    def __init__(self, root=ROOT, *, remote='origin', branch='backups', timeout=60):
        self.root = Path(root).resolve()
        self.remote, self.branch, self.timeout = remote, branch, timeout
        if not re.fullmatch(r'[A-Za-z0-9_-]+', remote) or not re.fullmatch(r'[A-Za-z0-9_-]+', branch):
            raise BankError('Git 远端或备份分支名称不合法')
        self.git = shutil.which('git')
        if not self.git and os.name == 'nt':
            for folder in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
                candidate = Path(os.environ.get(folder, '')) / 'Git' / 'cmd' / 'git.exe'
                if candidate.is_file():
                    self.git = str(candidate)
                    break
        if not self.git:
            raise BankError('Git 未安装或不在 PATH 中；请安装 Git 后再执行词库维护')
        top = Path(self._run('rev-parse', '--show-toplevel').decode().strip()).resolve()
        if top != self.root:
            raise BankError('项目必须是独立 Git 仓库；请按 README 克隆完整项目')
        self.remote_url = self._run('remote', 'get-url', self.remote).decode().strip()
        parsed = urlsplit(self.remote_url)
        if parsed.scheme in ('http', 'https') and (parsed.username or parsed.password or parsed.query):
            raise BankError('Git 远端地址不能包含凭据；请使用 Git Credential Manager 登录')

    def _run(self, *args, input=None, env_extra=None):
        env = os.environ.copy()
        for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_TRACE', 'GIT_TRACE_PACKET',
                     'GIT_TRACE_CURL', 'GIT_CURL_VERBOSE', 'GIT_CONFIG_PARAMETERS'):
            env.pop(name, None)
        env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
        if env_extra:
            env.update(env_extra)
        command = [self.git]
        if os.name == 'nt':
            # Windows sandbox Schannel may not have a credential handle. This is
            # a per-command TLS backend choice; certificate verification stays on.
            command += ['-c', 'http.sslBackend=openssl', '-c', 'http.version=HTTP/1.1']
        try:
            result = subprocess.run(command + list(args), cwd=self.root, input=input,
                                    capture_output=True, env=env, timeout=self.timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BankError(f'Git {args[0]} 命令无法完成或已超时，原词库未被修改') from exc
        if result.returncode:
            detail = result.stderr.decode('utf-8', 'replace').strip() or result.stdout.decode('utf-8', 'replace').strip()
            detail = re.sub(r'(https?://)[^/@\s]+@', r'\1***@', detail)
            detail = re.sub(r'(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)', '***', detail)
            raise BankError('Git 备份失败；请检查网络、Git 登录及推送权限。' + detail[:1000])
        return result.stdout

    def _remote_refs(self):
        raw = self._run('ls-remote', self.remote, 'refs/heads/main', f'refs/heads/{self.branch}')
        return {name: sha for sha, name in (line.split() for line in raw.decode().splitlines())}

    def _fetch(self, commit):
        self._run('fetch', '--no-tags', '--no-write-fetch-head', self.remote, commit)

    def _source_files(self):
        names = self._run('ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
        files = {}
        for raw in names:
            if not raw:
                continue
            name = raw.decode('utf-8')
            parts = PurePosixPath(name).parts
            if name.startswith('/') or '..' in parts or _upload_excluded(name):
                continue
            path = self.root.joinpath(*parts)
            if not path.exists():  # A locally deleted tracked file stays deleted.
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(self.root) or not path.is_file():
                raise BankError('源码备份包含符号链接或项目外文件，已取消')
            content = path.read_bytes()
            if name == '.env.example':
                for line in content.decode('utf-8-sig').splitlines():
                    key, sep, value = line.partition('=')
                    if sep and key.strip().casefold() in SECRET_FIELDS and _parse_env_value(value):
                        raise BankError('.env.example 含有实际凭据，请清空后再备份')
            files[name] = content
        if not files or 'cidaren/bank_store.py' not in files:
            raise BankError('未找到完整项目源码，Git 备份已取消')
        return files

    def _url(self, commit):
        if self.remote_url.startswith('https://github.com/'):
            return self.remote_url.removesuffix('.git') + '/tree/' + commit
        return ''

    def publish(self, artifacts, *, label='manual'):
        if not artifacts or not set(artifacts) <= ARTIFACTS:
            raise BankError('Git 备份内容类型不合法')
        for content in artifacts.values():
            try:
                if _private_payload(json.loads(content)):
                    raise BankError('词库快照含有凭据字段，已取消上传')
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise BankError('词库快照不是合法 JSON') from exc
        refs = self._remote_refs()
        parent = refs.get(f'refs/heads/{self.branch}') or refs.get('refs/heads/main')
        if parent:
            self._fetch(parent)
        else:
            parent = self._run('rev-parse', 'HEAD').decode().strip()
        files = self._source_files()
        files.update(artifacts)
        manifest = {'format':'cidaren-git-backup', 'version':1, 'label':label,
                    'created_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'source_head':self._run('rev-parse', 'HEAD').decode().strip(),
                    'artifacts':sorted(artifacts),
                    'files':{name:hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())}}
        files[MANIFEST] = _json_bytes(manifest)
        with tempfile.TemporaryDirectory(prefix='cidaren-git-') as temp:
            index = {'GIT_INDEX_FILE':str(Path(temp) / 'index')}
            self._run('read-tree', '--empty', env_extra=index)
            for name, content in sorted(files.items()):
                blob = self._run('hash-object', '-w', '--stdin', input=content).decode().strip()
                self._run('update-index', '--add', '--cacheinfo', '100644', blob, name, env_extra=index)
            tree = self._run('write-tree', env_extra=index).decode().strip()
            commit = self._run('commit-tree', tree, '-p', parent,
                               input=f'Backup source and wordbank: {label}\n'.encode()).decode().strip()
        # No force push: another computer's intervening backup must cause a
        # failure rather than replacing its history.
        self._run('push', '--porcelain', self.remote, f'{commit}:refs/heads/{self.branch}')
        self._verify_remote(commit)
        return {'commit':commit, 'reference':f'git:{commit}', 'branch':self.branch,
                'url':self._url(commit), 'label':label}

    def _verify_remote(self, commit):
        refs = self._remote_refs()
        head = refs.get(f'refs/heads/{self.branch}')
        if not head:
            raise BankError('远端未找到备份分支，原词库未被修改')
        self._fetch(head)
        # A newer backup is acceptable only if our commit remains in its history.
        self._run('merge-base', '--is-ancestor', commit, head)
        self._verified_files(commit)

    def _verified_files(self, commit):
        try:
            manifest = json.loads(self._run('show', f'{commit}:{MANIFEST}'))
            if (not isinstance(manifest, dict) or manifest.get('format') != 'cidaren-git-backup'
                    or type(manifest.get('version')) is not int or manifest['version'] != 1
                    or not isinstance(manifest.get('files'), dict)
                    or manifest.get('artifacts') not in ([LEGACY], [LEXICON], sorted(ARTIFACTS))):
                raise ValueError('备份清单格式不兼容')
            tree_names = {x.decode('utf-8') for x in self._run('ls-tree', '-r', '--name-only', '-z', commit).split(b'\0') if x}
            if tree_names != set(manifest['files']) | {MANIFEST}:
                raise ValueError('备份文件清单不一致')
            artifacts = {}
            for name, digest in manifest['files'].items():
                parts = PurePosixPath(name).parts
                if name.startswith('/') or '..' in parts or not re.fullmatch(r'[a-f0-9]{64}', digest):
                    raise ValueError('备份路径或校验值不合法')
                # Upload-only exclusions must not block restoring historical
                # wordbank snapshots that included task screenshots.
                if name not in ARTIFACTS and _excluded(name):
                    raise ValueError('备份含有禁止上传的文件')
                content = self._run('show', f'{commit}:{name}')
                if hashlib.sha256(content).hexdigest() != digest:
                    raise ValueError('备份校验值不一致')
                if name in manifest['artifacts']:
                    if _private_payload(json.loads(content)):
                        raise ValueError('备份含有凭据字段')
                    artifacts[name] = content
            return manifest, artifacts
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise BankError(f'Git 备份损坏或不兼容: {exc}') from exc

    def download(self, reference, folder):
        ref = str(reference).removeprefix('git:')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', ref) or '..' in ref or ref.endswith('/'):
            raise BankError('恢复引用必须是备份提交 SHA 或分支名')
        refs = self._remote_refs()
        head = refs.get(f'refs/heads/{self.branch}')
        if not head:
            raise BankError('远端没有 backups 分支')
        self._fetch(head)
        if ref in (self.branch, f'origin/{self.branch}', f'refs/heads/{self.branch}'):
            commit = head
        elif re.fullmatch(r'[a-fA-F0-9]{7,40}', ref):
            commit = self._run('rev-parse', '--verify', f'{ref}^{{commit}}').decode().strip()
            self._run('merge-base', '--is-ancestor', commit, head)
        else:
            raise BankError('只能恢复 backups 分支或该分支历史中的提交')
        manifest, artifacts = self._verified_files(commit)
        name = LEXICON if LEXICON in artifacts else LEGACY
        target = Path(folder) / PurePosixPath(name).name
        target.write_bytes(artifacts[name])
        return {'path':target, 'kind':'lexicon' if name == LEXICON else 'legacy', 'commit':commit,
                'reference':f'git:{commit}', 'manifest':manifest}
