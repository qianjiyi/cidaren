"""Unit tests never publish fixtures to the user's real GitHub repository."""
import hashlib
from pathlib import Path

import pytest

from cidaren.bank_store import BankError, BankStore
from cidaren.git_backups import LEGACY, LEXICON


class MemoryBackups:
    def __init__(self):
        self.snapshots = {}
        self.labels = []

    def publish(self, artifacts, *, label='manual'):
        commit = hashlib.sha1(str(len(self.snapshots)).encode() + b''.join(artifacts.values())).hexdigest()
        self.snapshots[commit] = dict(artifacts)
        self.labels.append(label)
        return {'commit':commit, 'reference':f'git:{commit}', 'branch':'backups', 'label':label, 'url':''}

    def download(self, reference, folder):
        commit = str(reference).removeprefix('git:')
        if commit == 'backups':
            commit = next(reversed(self.snapshots))
        if commit not in self.snapshots:
            raise BankError('不存在此备份')
        artifacts = self.snapshots[commit]
        name = LEXICON if LEXICON in artifacts else LEGACY
        path = Path(folder) / name.split('/')[-1]
        path.write_bytes(artifacts[name])
        return {'path':path, 'kind':'lexicon' if name == LEXICON else 'legacy',
                'commit':commit, 'reference':f'git:{commit}'}


@pytest.fixture(autouse=True)
def memory_backups(monkeypatch):
    backend = MemoryBackups()
    monkeypatch.setattr(BankStore, '_backend', lambda self:self._backup_backend or backend)
    return backend
