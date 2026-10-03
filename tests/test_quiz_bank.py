from copy import deepcopy
from unittest.mock import MagicMock

import pytest
import requests

from cidaren import a
from tests.test_bank_store import bank, choice
from cidaren.bank_store import BankStore


class FakeQuizClient:
    def __init__(self, topics, answers, responses=None):
        self.topics = deepcopy(topics)
        for i, topic in enumerate(self.topics):
            topic['topic_code'] = f'topic-{i}'
            topic['topic_done_num'] = i
            topic['topic_total'] = len(topics)
        self.answers = answers
        self.responses = iter(responses) if responses is not None else None
        self.index = 0
        self.verified = []
        self.submitted = []
        self.accepted = set()
        self.study_calls = []

    def start_answer(self, *args, **kwargs):
        return {'code':1,'data':self.topics[0]}

    def study_start_answer(self, *args, **kwargs):
        self.study_calls.append(('start',args,kwargs))
        return self.start_answer()

    def verify(self, code, answer):
        self.verified.append((code,answer))
        if self.responses is not None:
            return next(self.responses)
        expected = self.answers[self.index]
        if isinstance(expected,list):
            result = int(answer in expected)
            if result:
                self.accepted.add(answer)
            over = int(len(self.accepted) == len(expected))
        else:
            result, over = int(answer == expected), 1
        return {'code':1,'data':{'answer_result':result,'over_status':over,'topic_code':f'current-{len(self.verified)}'}}

    def study_verify(self, code, answer):
        self.study_calls.append(('verify',code,answer))
        return self.verify(code,answer)

    def submit(self, code, spent):
        self.submitted.append((code,spent))
        self.index += 1
        self.accepted.clear()
        data = self.topics[self.index] if self.index < len(self.topics) else {'topic_done_num':self.index}
        return {'code':1,'data':data}

    def study_submit(self, code, spent):
        self.study_calls.append(('submit',code,spent))
        return self.submit(code,spent)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(a,'_sleep',lambda *args:None)


@pytest.mark.parametrize('words,stem,remark', [
    (('a','vicious','cycle'),'_ _ _','恶性循环'),
    (('derive','from'),'_ ... _ ...','从……获得……'),
    (('attribute','to'),'_ ... _ ...','把……归因于……'),
])
def test_cross_task_phrase_learning_promote_reuse_without_llm(bank, monkeypatch, capsys, words, stem, remark):
    answer = ','.join(words)
    topic = choice(word=stem,mode=32,remark=remark,opts=(*words,'extra'),tags=range(len(words)+1))
    llm = MagicMock(return_value=answer)
    monkeypatch.setattr(a,'_llm_answer',llm)
    first = FakeQuizClient([topic],[answer])
    a.run_quiz(first,1,10,bank=bank)
    llm.assert_called_once()
    assert bank.preview()[0]['verification'] == 'confirmed'
    assert bank.status()['formal'] == 0
    bank.promote()
    second_topic = deepcopy(topic)
    second_topic['options'].reverse()
    second_topic['options'].append({'content':'distractor','answer_tag':501})
    for i, opt in enumerate(second_topic['options'][:-1]):
        opt['answer_tag'] = 20+i*13
    llm.reset_mock()
    llm.side_effect = AssertionError('LLM must not run on a wordbank hit')
    second = FakeQuizClient([second_topic],[answer])
    a.run_quiz(second,2,11,bank=bank)
    llm.assert_not_called()
    assert second.verified[0][1] == answer
    assert '[正式词库]' in capsys.readouterr().out
    assert bank.preview()[0]['source'] == '正式词库'


def test_confirmed_exact_cache_precedes_rules_and_llm(bank, monkeypatch, capsys):
    topic = choice()
    bank.record(topic,42,'validated','confirmed')
    rules, llm = MagicMock(), MagicMock()
    monkeypatch.setattr(a,'_match_answer',rules)
    monkeypatch.setattr(a,'_llm_answer',llm)
    client = FakeQuizClient([topic],[42])
    a.run_quiz(client,1,2,bank=bank)
    assert client.verified == [('topic-0',42)]
    assert '[临时缓存]' in capsys.readouterr().out
    rules.assert_not_called()
    llm.assert_not_called()


def test_historical_candidate_server_confirms_complete_new_cache(tmp_path, monkeypatch, capsys):
    import json
    source = tmp_path/'old.json'
    source.write_text(json.dumps({'22::norm::derive::::vt. 获得|vt. 否认':{'ans':0,'stem':'derive'}}),encoding='utf-8')
    store = BankStore(tmp_path/'data'/'lexicon.sqlite3')
    store.migrate(source)
    llm = MagicMock(side_effect=AssertionError('history match must not call LLM'))
    monkeypatch.setattr(a,'_llm_answer',llm)
    client = FakeQuizClient([choice()],[7])
    a.run_quiz(client,1,2,bank=store)
    assert store.preview()[0]['verification'] == 'confirmed'
    assert store.preview()[0]['source'].startswith('历史')
    assert store.status()['formal'] == 0
    assert store.status()['cache'] == 1
    assert '历史词库候选' in capsys.readouterr().out
    with store.connection() as con:
        topic = json.loads(con.execute("SELECT topic_json FROM records WHERE stage='cache'").fetchone()[0])
        assert [o['answer_tag'] for o in topic['options']] == [7,42]
    llm.assert_not_called()


def test_official_reading_definitions_are_cached_and_reused_by_rule(bank, monkeypatch):
    card = choice(word='bank',opts=('n. 银行','n. 河岸'),mode=0)
    topic = choice(word='bank',opts=('n. 工厂','n. 银行'))
    llm = MagicMock(side_effect=AssertionError('must use official definitions'))
    monkeypatch.setattr(a,'_llm_answer',llm)
    client = FakeQuizClient([card,topic],[None,42])
    a.run_quiz(client,1,2,bank=bank)
    assert client.verified == [('topic-1',42)]
    assert len(client.submitted) == 2
    assert sum(x['verification']=='official' for x in bank.preview()) == 2
    assert any(x['source']=='规则' and x['verification']=='confirmed' for x in bank.preview())
    llm.assert_not_called()


def test_wrong_formal_answer_disabled_and_correction_stays_pending(bank, monkeypatch):
    topic = choice()
    bank.record(topic,7,'old','confirmed')
    bank.promote()
    response = {'code':1,'data':{'answer_result':0,'answer_corrects':[42],'topic_code':'corrected'}}
    client = FakeQuizClient([topic],[42],[response])
    a.run_quiz(client,1,2,bank=bank)
    assert client.verified[0][1] == 7
    assert client.submitted[0][0] == 'corrected'
    assert bank.lookup(topic).answer is None
    assert all(x['verification']=='pending' for x in bank.preview())
    assert bank.promote()['promoted'] == []
    monkeypatch.setattr(a,'_llm_answer',lambda *args:42)
    following = FakeQuizClient([topic],[42])
    a.run_quiz(following,2,3,bank=bank)
    assert following.verified[0][1] == 42
    assert bank.lookup(topic).answer == 42
    bank.promote()
    assert bank.lookup(topic).answer == 42


@pytest.mark.parametrize('corrects', [True, {'answer':42}, [[42]], [99], [True], [42,{'answer':7}]])
def test_malformed_corrections_not_learned(bank, monkeypatch, corrects):
    topic = choice()
    monkeypatch.setattr(a,'_llm_answer',lambda *args:7)
    client = FakeQuizClient([topic],[42],[{'code':1,'data':{'answer_result':0,'answer_corrects':corrects}}])
    a.run_quiz(client,1,2,bank=bank)
    assert not any(x['source']=='服务器纠错' for x in bank.preview())
    assert bank.promote()['promoted'] == []


@pytest.mark.parametrize('response', [None, {'code':403,'data':{'answer_result':1}}, {'code':1,'data':[]},
                                      {'code':1,'data':{'answer_result':True}}, {'code':1,'data':{'answer_result':'1'}}])
def test_unconfirmed_response_cannot_promote(bank, monkeypatch, response):
    monkeypatch.setattr(a,'_llm_answer',lambda *args:7)
    client = FakeQuizClient([choice()],[7],[response])
    a.run_quiz(client,1,2,bank=bank)
    assert bank.preview()[0]['verification'] == 'pending'
    assert bank.promote()['promoted'] == []


def test_transport_failure_keeps_candidate_pending(bank, monkeypatch):
    monkeypatch.setattr(a,'_llm_answer',lambda *args:7)
    client = FakeQuizClient([choice()],[7])
    def failure(*args):
        raise requests.ConnectionError('simulated connection failure')
    client.verify = failure
    with pytest.raises(requests.ConnectionError):
        a.run_quiz(client,1,2,bank=bank)
    assert bank.status()['pending'] == 1
    assert bank.promote()['promoted'] == []


def collocation():
    topic = choice(word='derive',mode=31,remark=[{'relation':'from'},{'relation':'origin'}],
                   opts=('from','origin','other'),tags=(7,42,99))
    topic['answer_num'] = 2
    return topic


def test_multiselect_all_answers_must_be_confirmed(bank):
    client = FakeQuizClient([collocation()],[[7,42]])
    a.run_quiz(client,1,2,bank=bank)
    assert [answer for _,answer in client.verified] == [7,42]
    assert bank.preview()[0]['verification'] == 'confirmed'
    assert len(bank.promote()['promoted']) == 1


def test_multiselect_over_flag_and_corrections_not_enough(bank):
    client = FakeQuizClient([collocation()],[[7,42]],[{'code':1,'data':{
        'answer_result':1,'over_status':1,'answer_corrects':[7,42]}}])
    a.run_quiz(client,1,2,bank=bank)
    assert [answer for _,answer in client.verified] == [7]
    assert all(x['verification']=='pending' for x in bank.preview())
    assert bank.promote()['promoted'] == []


def test_corrected_multiselect_requires_individual_confirmation(bank, monkeypatch):
    topic = collocation()
    topic['stem']['remark'] = [{'relation':'unmatched'}]
    monkeypatch.setattr(a,'_llm_answer',lambda *args:[99])
    responses = [
        {'code':1,'data':{'answer_result':0,'over_status':0,'answer_corrects':[7,42]}},
        {'code':1,'data':{'answer_result':1,'over_status':0}},
        {'code':1,'data':{'answer_result':1,'over_status':1}},
    ]
    client = FakeQuizClient([topic],[[7,42]],responses)
    a.run_quiz(client,1,2,bank=bank)
    assert [answer for _,answer in client.verified] == [99,7,42]
    assert bank.lookup(topic).answer == [7,42]
    assert len(bank.promote()['promoted']) == 1


@pytest.mark.parametrize('study',[False,True])
def test_class_and_study_routes_use_same_bank(bank, monkeypatch, study):
    topic = choice()
    bank.record(topic,7,'test','confirmed')
    llm = MagicMock()
    monkeypatch.setattr(a,'_llm_answer',llm)
    client = FakeQuizClient([topic],[7])
    a.run_quiz(client,1,2,task_kind='study' if study else 'class',course_id='CET4_v2',list_id='list',bank=bank)
    assert client.verified[0][1] == 7
    assert bool(client.study_calls) is study
    assert len(client.submitted) == 1
    llm.assert_not_called()


def test_full_class_task_flow_uses_bank_and_signin(bank, monkeypatch):
    client = FakeQuizClient([choice()],[7])
    client.task_info = MagicMock(return_value={'data':{'task_name':'test'}})
    client.chose_word_list = MagicMock(return_value={'data':{'word_list':[{'word':'derive','score':0,'course_id':'course','list_id':'list'}]}})
    client.submit_chose_word = MagicMock(return_value={'code':1})
    client.signin = MagicMock(return_value={'data':{}})
    bank.record(choice(),7,'test','confirmed')
    a.run_full(client,1,2,bank=bank)
    client.submit_chose_word.assert_called_once_with(1,{'course:list':['derive']})
    client.signin.assert_called_once()
    assert client.verified[0][1] == 7


def test_full_study_task_flow_uses_bank_and_signin(bank):
    client = FakeQuizClient([choice()],[7])
    client.study_task_info = MagicMock(return_value={'data':{'task_name':'test'}})
    client.study_chose_word_list = MagicMock(return_value={'data':{'word_list':[{'word':'derive','score':0}]}})
    client.study_submit_chose_word = MagicMock(return_value={'code':1})
    client.signin = MagicMock(return_value={'data':{}})
    bank.record(choice(),7,'test','confirmed')
    a.run_study_full(client,1,'course','list',bank=bank)
    client.study_submit_chose_word.assert_called_once_with(1,'course','list',{'course:list':['derive']},task_type=3,grade=2)
    client.signin.assert_called_once()
    assert client.verified[0][1] == 7


@pytest.mark.parametrize('mode,text,expected', [(22,'1',42),(51,'1984','1984'),(31,'0,1',[7,42])])
def test_llm_position_maps_to_real_tags_and_fill_keeps_numbers(monkeypatch, mode, text, expected):
    topic = collocation() if mode == 31 else choice(mode=mode)
    if mode == 51:
        topic['stem']['content'] = '{}'
        topic['options'] = []
    monkeypatch.setattr(a,'get_runtime_config',lambda:{'LLM_URL':'https://mock.invalid/v1','LLM_KEY':'test','LLM_MODEL':'test'})
    response = MagicMock()
    response.text = 'json'
    response.json.return_value = {'choices':[{'message':{'content':text}}]}
    monkeypatch.setattr(a.requests,'post',lambda *args,**kwargs:response)
    assert a._llm_answer(topic,{}) == expected


def test_rules_preserve_word_boundaries_negation_and_polysemy():
    topic = choice(word='derive',opts=('vt. 获得','vt. 否认'))
    assert a._match_answer(topic,{'derived':['vt. 获得']}) is None
    assert a._match_answer(topic,{'derive':['vt. 获得','vt. 否认']}) is None
    assert a._match_answer(topic,{'derive':['vt. 获得']}) == 7
