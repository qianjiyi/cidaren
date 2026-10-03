from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

from cidaren.bank_store import (
    BankError, BankStore, LEGACY_FILE, encode_answer, exact_key, map_answer,
    norm, parse_legacy, semantic_key,
)
from cidaren.git_backups import LEGACY, LEXICON


def choice(word='derive', opts=('vt. 获得', 'vt. 否认'), mode=22, tags=(7, 42), remark=''):
    return {'topic_mode':mode, 'stem':{'content':word, 'remark':remark},
            'options':[{'content':text, 'answer_tag':tag} for text,tag in zip(opts,tags)]}


@pytest.fixture
def bank(tmp_path):
    source = tmp_path / 'bank.json'
    source.write_text('{}', encoding='utf-8')
    store = BankStore(tmp_path / 'data' / 'lexicon.sqlite3')
    store.migrate(source)
    return store


def _process_write(path, offset):
    store = BankStore(path)
    for i in range(6):
        store.record(choice(word=f'word {offset+i}'), 7, 'process', 'confirmed')


@pytest.mark.parametrize('key', [
    '22::derive::vt. 获得|vt. 否认',
    '22::derive::::vt. 获得|vt. 否认',
    '22::norm::derive::::vt. 获得|vt. 否认',
])
def test_three_old_formats_preserve_raw_and_remap(tmp_path, key):
    data = {key:{'ans':0, 'stem':'derive', 'extra':'must retain'}}
    source = tmp_path / 'old.json'
    source.write_text(json.dumps(data), encoding='utf-8')
    store = BankStore(tmp_path / 'data' / 'lexicon.sqlite3')
    result = store.migrate(source)
    assert result['legacy'] == result['legacy_candidates'] == 1
    assert store.lookup(choice()).answer == 7
    with store.connection() as con:
        assert json.loads(con.execute('SELECT value_json FROM legacy').fetchone()[0]) == data[key]
    assert json.loads(store._backend().snapshots[result['backup']['commit']][LEGACY]) == data
    assert not (store.path.parent / 'backups').exists()
    source.write_text('{}', encoding='utf-8')
    assert store.migrate(source)['already_migrated']
    assert store.status()['legacy'] == 1


def test_legacy_selected_truncated_and_unknown_key_preserved(tmp_path):
    data = {'22::norm::derive::::'+'x'*20:{'ans':0,'stem':'derive'},
            'unrecognizable':{'ans':'x','stem':'x'}}
    source = tmp_path / 'old.json'
    source.write_text(json.dumps(data),encoding='utf-8')
    store = BankStore(tmp_path / 'data' / 'lexicon.sqlite3')
    result = store.migrate(source)
    assert result['legacy'] == result['legacy_issues'] == 2
    assert len(store.legacy_issues()) == 2
    assert store.lookup(choice(opts=('x'*20+' suffix','other'))).answer is None


def test_legacy_missing_sentence_context_cannot_fill_new_context(tmp_path):
    source = tmp_path / 'old.json'
    source.write_text(json.dumps({'11::i {derive} it.::vt. 获得|vt. 否认':{'ans':0,'stem':'I {derive} it.'}}),encoding='utf-8')
    store = BankStore(tmp_path / 'data' / 'lexicon.sqlite3')
    store.migrate(source)
    assert store.lookup(choice(word='I {derive} it.',mode=11,remark='我获得它')).answer is None


@pytest.mark.parametrize('tags,can_match', [([0],False),([0,1],True)])
def test_historical_multiselect_requires_current_complete_count(tmp_path, tags, can_match):
    remark = [{'relation':'from'},{'relation':'origin'}]
    key = '31::coll::derive::'+json.dumps(remark,ensure_ascii=False,sort_keys=True)+'::from|origin|other'
    source = tmp_path/'old.json'
    source.write_text(json.dumps({key:{'ans':tags,'stem':'derive'}}),encoding='utf-8')
    store = BankStore(tmp_path/'data'/'lexicon.sqlite3')
    store.migrate(source)
    topic = choice(word='derive',mode=31,remark=remark,opts=('other','origin','from'),tags=(99,42,7))
    topic['answer_num'] = 2
    answer = store.lookup(topic).answer
    assert (answer is not None) is can_match
    if can_match:
        assert set(answer) == {7,42}


def test_real_bank_all_records_and_backup_recoverable(tmp_path):
    before = hashlib.sha256(LEGACY_FILE.read_bytes()).hexdigest()
    store = BankStore(tmp_path / 'data' / 'lexicon.sqlite3')
    result = store.migrate()
    assert result['legacy'] == 7612
    assert result['legacy_candidates'] == 7601
    assert result['legacy_issues'] == 11
    assert hashlib.sha256(store._backend().snapshots[result['backup']['commit']][LEGACY]).hexdigest() == before
    assert hashlib.sha256(LEGACY_FILE.read_bytes()).hexdigest() == before
    original = json.loads(LEGACY_FILE.read_bytes())
    with store.connection() as con:
        assert {row[0]:json.loads(row[1]) for row in con.execute('SELECT key,value_json FROM legacy')} == original
    store.validate()


def test_exact_key_full_options_and_case_sensitive_media():
    topic = choice(opts=('x'*20+' one','other'))
    changed = deepcopy(topic)
    changed['options'][0]['content'] = 'x'*20+' two'
    assert exact_key(topic) != exact_key(changed)
    topic['audio_id'] = 'AbC'
    changed = deepcopy(topic)
    changed['audio_id'] = 'abc'
    assert exact_key(topic) != exact_key(changed)
    assert semantic_key(topic) is None


def test_snapshot_full_information_without_task_credentials(bank):
    topic = choice()
    topic.update(topic_code='transient',task_id=1,usertoken='do not persist',audio_id='A1')
    topic['stem']['image_id'] = 'B2'
    identifier = bank.record(topic,42,'test','confirmed')
    with bank.connection() as con:
        row = con.execute('SELECT * FROM records WHERE id=?',(identifier,)).fetchone()
        saved = json.loads(row['topic_json'])
        assert saved['options'] == topic['options']
        assert saved['audio_id'] == 'A1' and saved['stem']['image_id'] == 'B2'
        assert not {'topic_code','task_id','usertoken'} & saved.keys()
        assert json.loads(row['raw_answer_json']) == 42 and row['verified_at']


def test_actual_tags_not_option_indexes(bank):
    topic = choice()
    with pytest.raises(BankError):
        encode_answer(topic,0)
    bank.record(topic,42,'server','confirmed')
    assert bank.lookup(topic).answer == 42
    bank.promote()
    changed = choice(opts=('vt. 否认','vt. 获得'),tags=(91,88))
    assert bank.lookup(changed).answer == 91
    assert bank.lookup(changed).source == '正式词库'


def test_query_order_and_rejection_overrides_old_formal(bank):
    topic = choice()
    bank.record(topic,7,'test','confirmed')
    bank.promote()
    bank.record(topic,42,'new','confirmed')
    assert bank.lookup(topic).source == '精确题库'
    bank.reject(topic,7)
    assert bank.lookup(topic).answer == 42
    assert bank.lookup(topic).source == '临时缓存'
    assert bank.status()['rejections'] == 1
    bank.clear_cache()
    assert bank.lookup(topic).answer is None
    assert bank.status()['formal'] == 1


def test_duplicate_text_or_conflicting_answers_ambiguous(bank):
    topic = choice(opts=('same','same'))
    bank.record(topic,7,'test','confirmed')
    assert bank.lookup(topic).answer is None
    topic = choice()
    bank.record(topic,7,'test','confirmed')
    bank.record(topic,42,'test','confirmed')
    bank.promote()
    assert bank.lookup(topic).answer is None
    assert '无法消歧' in bank.lookup(topic).reason
    assert bank.status()['formal'] == 3


def test_multiple_meanings_preserved_and_disambiguated_by_options(bank):
    card = choice(word='bank',opts=('n. 银行','n. 河岸'),mode=0)
    bank.record_definitions(card)
    result = bank.promote()
    assert len(result['promoted']) == 2
    assert bank.lookup(choice(word='bank',opts=('n. 银行','n. 工厂'))).answer == 7
    assert bank.lookup(choice(word='bank',opts=('n. 商店','n. 河岸'))).answer == 42
    assert bank.lookup(choice(word='bank',opts=('n. 银行','n. 河岸'))).answer is None
    assert bank.lookup(choice(word='bank',mode=22,remark='河边语境',opts=('n. 银行','n. 河岸'))).answer is None
    assert bank.lookup(choice(word='n. 河岸',mode=17,opts=('bank','factory'))).answer == 7


def test_official_definitions_retain_original_complete_topic(bank):
    card = choice(word='bank',opts=('n. 银行','n. 河岸'),mode=0,tags=(0,0))
    ids = bank.record_definitions(card)
    assert len(ids) == 2
    with bank.connection() as con:
        for row in con.execute("SELECT topic_json FROM records WHERE source='official_definitions'"):
            assert json.loads(row[0])['options'] == card['options']


@pytest.mark.parametrize('word,other', [('ice cream','icecream'), ('not good','good'), ('derive','derived'), ('n. bank','v. bank')])
def test_conservative_normalization_does_not_merge(word, other):
    assert norm(word) != norm(other)


def test_sentence_context_and_unknown_mode_only_exact(bank):
    topic = choice(word='I {derive} this from data.',mode=11,remark='来源')
    bank.record(topic,7,'test','confirmed')
    bank.promote()
    changed = deepcopy(topic)
    changed['options'].reverse()
    assert bank.lookup(changed).answer == 7
    changed['stem']['content'] = 'I do not {derive} this from data.'
    assert bank.lookup(changed).answer is None
    unknown = choice(mode=999)
    bank.record(unknown,7,'test','confirmed')
    bank.promote()
    assert bank.lookup(unknown).answer == 7
    unknown['options'].reverse()
    assert bank.lookup(unknown).answer is None


def test_fill_templates_cannot_reuse_full_phrase_or_other_template(bank):
    topic = choice(word='{} from',mode=51,opts=(),remark='从……获得……')
    bank.record(topic,'derive','test','confirmed')
    bank.promote()
    assert bank.lookup(topic).answer == 'derive'
    changed = deepcopy(topic)
    changed['stem']['content'] = '{}'
    assert bank.lookup(changed).answer is None
    assert semantic_key(topic) != semantic_key(choice(word='_ _',mode=32,remark='从……获得……'))


def test_word_order_and_count(bank):
    topic = choice(word='_ _ _',mode=32,opts=('a','vicious','cycle','extra'),tags=(7,42,99,101),remark='恶性循环')
    bank.record(topic,'a,vicious,cycle','test','confirmed')
    bank.promote()
    topic['options'].reverse()
    assert bank.lookup(topic).answer == 'a,vicious,cycle'
    changed = deepcopy(topic)
    changed['stem']['content'] = '_ _'
    assert bank.lookup(changed).answer is None
    with pytest.raises(BankError):
        encode_answer(topic,'a,a,cycle')
    with pytest.raises(BankError,match='词数'):
        encode_answer(topic,'a,cycle')
    assert map_answer(topic,{'kind':'words','items':['a','cycle','vicious']}) == 'a,cycle,vicious'


def test_multi_only_complete_verified_set_promotes(bank):
    topic = choice(word='derive',mode=31,remark=[{'relation':'from'},{'relation':'origin'}],
                   opts=('from','origin','other'),tags=(7,42,99))
    topic['answer_num'] = 2
    partial = bank.record(topic,[7],'test','confirmed',complete=False)
    assert partial in bank.promote()['skipped']
    assert bank.lookup(topic).answer is None
    full = bank.record(topic,[7,42],'test','confirmed')
    assert full in bank.promote()['promoted']
    changed = deepcopy(topic)
    changed['options'].reverse()
    assert set(bank.lookup(changed).answer) == {7,42}


def test_promote_idempotent_and_selection(bank):
    first = bank.record(choice(),7,'test','confirmed')
    second = bank.record(choice(word='other'),42,'test','confirmed')
    assert bank.promote({first})['promoted'] == [first]
    assert bank.status()['formal'] == bank.status()['cache'] == 1
    assert bank.promote({first})['promoted'] == []
    duplicate = bank.record(choice(),7,'test','confirmed')
    assert next(x for x in bank.preview() if x['id']==duplicate)['classification'] == '重复'
    bank.promote({duplicate})
    assert bank.status()['formal'] == 1
    assert [x['id'] for x in bank.preview()] == [second]


def test_conflicts_reported_and_retained(bank):
    bank.record(choice(),7,'test','confirmed')
    bank.promote()
    identifier = bank.record(choice(),42,'test','confirmed')
    assert bank.preview()[0]['classification'].startswith('冲突')
    result = bank.promote()
    assert result['conflicts'] == [identifier]
    assert bank.status()['formal'] == 2
    assert bank.lookup(choice()).answer is None


def test_promote_rolls_back_formal_and_cache_on_failure(bank, monkeypatch):
    identifier = bank.record(choice(),7,'test','confirmed')
    original_index = bank._index
    def fail(con, record_id):
        original_index(con,record_id)
        raise RuntimeError('simulate disk failure')
    monkeypatch.setattr(bank,'_index',fail)
    with pytest.raises(RuntimeError):
        bank.promote()
    assert bank.status()['formal'] == bank.status()['knowledge'] == 0
    assert bank.preview()[0]['id'] == identifier


def test_migration_rolls_back_and_retry_imports_once(tmp_path, monkeypatch):
    source = tmp_path / 'old.json'
    source.write_text(json.dumps({'22::derive::yes|no':{'ans':0,'stem':'derive'},
                                  '22::other::yes|no':{'ans':1,'stem':'other'}}),encoding='utf-8')
    store = BankStore(tmp_path / 'data' / 'lexicon.sqlite3')
    original = store._index
    monkeypatch.setattr(store,'_index',lambda *args:(_ for _ in ()).throw(RuntimeError('fail')))
    with pytest.raises(RuntimeError):
        store.migrate(source)
    with store.connection() as con:
        assert con.execute('SELECT COUNT(*) FROM records').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM legacy').fetchone()[0] == 0
    with pytest.raises(BankError,match='未完成'):
        store.validate()
    monkeypatch.setattr(store,'_index',original)
    assert store.migrate(source)['legacy'] == 2
    assert store.migrate(source)['already_migrated']


def test_simultaneous_runtime_registration_not_treated_as_maintenance(bank):
    import time
    def run(i):
        with bank.runtime():
            time.sleep(.02)
            return i
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert list(pool.map(run,range(10))) == list(range(10))


def test_concurrent_writes_live_reads_and_unique_records(bank):
    def write(i):
        store = BankStore(bank.path)
        store.record(choice(word=f'word {i}'),7,'thread','confirmed')
        store.record(choice(),7,'shared','confirmed')
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(write,range(15)))
    assert bank.status()['cache'] == 16
    assert bank.lookup(choice(word='word 14')).answer == 7
    # Each worker starts a separate interpreter with no memory snapshot to merge.
    context = multiprocessing.get_context('spawn')
    processes = [context.Process(target=_process_write,args=(str(bank.path),100+i*10)) for i in range(2)]
    for proc in processes:
        proc.start()
    for proc in processes:
        proc.join(30)
        assert proc.exitcode == 0
    assert bank.status()['cache'] == 28
    bank.validate()


def test_clear_cache_backs_up_and_preserves_formal_history_rejections(bank):
    bank.record(choice(),7,'test','confirmed')
    bank.promote()
    bank.reject(choice(),7)
    bank.record(choice(word='new'),7,'test','confirmed')
    result = bank.clear_cache()
    assert result['cleared'] == 1
    before = json.loads(bank._backend().snapshots[result['backup']['commit']][LEXICON])
    assert sum(r['stage'] == 'cache' for r in before['tables']['records']) == 1
    assert not (bank.path.parent / 'backups').exists()
    assert bank.status()['formal'] == bank.status()['rejections'] == 1


def test_record_ids_not_reused_after_clearing_cache(bank):
    old = bank.record(choice(),7,'test','confirmed')
    bank.clear_cache()
    fresh = bank.record(choice(word='new'),7,'test','confirmed')
    assert fresh > old
    assert bank.promote({old})['promoted'] == []


@pytest.mark.parametrize('format', ['git','sqlite','json'])
def test_backup_export_and_restore_complete_state(bank, tmp_path, format):
    bank.record(choice(),7,'test','confirmed')
    bank.promote()
    if format == 'git':
        backup = bank.backup()
    elif format == 'sqlite':
        import sqlite3
        backup = tmp_path / 'explicit.sqlite3'
        with bank.connection() as src:
            with sqlite3.connect(backup) as dest:
                src.backup(dest)
    else:
        backup = tmp_path / 'export.json'
        bank.export(backup)
        assert json.loads(backup.read_bytes())['version'] == 2
    bank.record(choice(word='new'),7,'test','confirmed')
    result = bank.restore(backup)
    assert result['previous_backup']['commit'] in bank._backend().snapshots
    assert bank.status()['cache'] == 0 and bank.status()['formal'] == 1
    other = BankStore(tmp_path / 'other' / 'lexicon.sqlite3')
    other.restore(backup)
    assert other.lookup(choice()).answer == 7


def test_offline_migration_and_restore_runtime_lock(bank, tmp_path):
    backup = bank.backup()
    with bank.runtime():
        with pytest.raises(BankError,match='停止'):
            bank.restore(backup)
        with bank.connection() as con:
            con.execute("DELETE FROM metadata WHERE key='migration'")
            con.commit()
        with pytest.raises(BankError,match='停止'):
            bank.migrate(tmp_path / 'missing.json')


def test_corrupt_input_or_db_not_silently_replaced(bank, tmp_path):
    before = bank.path.read_bytes()
    corrupt = tmp_path / 'corrupt.json'
    corrupt.write_text('{broken',encoding='utf-8')
    with pytest.raises(BankError):
        bank.restore(corrupt)
    assert bank.path.read_bytes() == before
    source = tmp_path / 'old.json'
    source.write_text('[]',encoding='utf-8')
    other = BankStore(tmp_path / 'other' / 'lexicon.sqlite3')
    with pytest.raises(BankError):
        other.migrate(source)
    assert not other.path.exists()
    other.path.parent.mkdir(exist_ok=True)
    other.path.write_bytes(b'bad sqlite')
    with pytest.raises(BankError):
        other.migrate(source)
    assert other.path.read_bytes() == b'bad sqlite'


def test_corrupt_export_record_rejected_before_replacement(bank, tmp_path):
    bank.record(choice(),7,'test','confirmed')
    export = tmp_path/'export.json'
    bank.export(export)
    data = json.loads(export.read_bytes())
    data['tables']['records'][0]['answer_json'] = '{"kind":"choice"}'
    export.write_text(json.dumps(data),encoding='utf-8')
    before = bank.path.read_bytes()
    with pytest.raises(BankError,match='损坏'):
        bank.restore(export)
    assert bank.path.read_bytes() == before


def test_version_rejected_without_mutating(bank):
    with bank.connection() as con:
        con.execute('PRAGMA user_version=999')
    with pytest.raises(BankError,match='版本'):
        bank.validate()
