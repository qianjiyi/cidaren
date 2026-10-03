import json

import pytest

from cidaren import bank_tools
from cidaren.bank_store import BankStore
from tests.test_bank_store import bank, choice


def test_cli_preview_and_promote_selected(bank, capsys):
    identifier = bank.record(choice(),7,'test','confirmed')
    assert bank_tools.main(['--db',str(bank.path),'preview','--limit','0']) == 0
    assert json.loads(capsys.readouterr().out)['cache'][0]['id'] == identifier
    assert bank_tools.main(['--db',str(bank.path),'promote','--ids',str(identifier)]) == 0
    assert bank.status()['formal'] == 1


def test_cli_clear_cache_requires_confirmation_then_backup(bank, monkeypatch, capsys):
    bank.record(choice(),7,'test','confirmed')
    monkeypatch.setattr('builtins.input',lambda *args:'no')
    assert bank_tools.main(['--db',str(bank.path),'clear-cache']) == 0
    assert bank.status()['cache'] == 1
    monkeypatch.setattr('builtins.input',lambda *args:'YES')
    assert bank_tools.main(['--db',str(bank.path),'clear-cache']) == 0
    assert bank.status()['cache'] == 0
    assert bank._backend().labels[-1] == 'before-clear'
    assert not (bank.path.parent/'backups').exists()


def test_cli_restore_new_computer_without_existing_db(bank, tmp_path, capsys):
    bank.record(choice(),7,'test','confirmed')
    exported = tmp_path/'export.json'
    bank.export(exported)
    target = tmp_path/'new computer'/'lexicon.sqlite3'
    assert bank_tools.main(['--db',str(target),'restore',str(exported),'--yes']) == 0
    assert BankStore(target).status()['cache'] == 1


def test_cli_missing_or_corrupt_library_error_not_empty(tmp_path, capsys):
    target = tmp_path/'missing.sqlite3'
    assert bank_tools.main(['--db',str(target),'status']) == 1
    assert '尚未迁移' in capsys.readouterr().err
    assert not target.exists()
    target.write_bytes(b'invalid')
    assert bank_tools.main(['--db',str(target),'status']) == 1
    assert target.read_bytes() == b'invalid'


def test_promote_requires_explicit_selection(bank):
    with pytest.raises(SystemExit) as error:
        bank_tools.main(['--db',str(bank.path),'promote'])
    assert error.value.code == 2


def test_ids_are_positive_integers():
    assert bank_tools._ids('1,2 3') == {1,2,3}
    with pytest.raises(Exception):
        bank_tools._ids('0,one')
