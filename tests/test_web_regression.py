from collections import deque
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from cidaren import _runner, web
from tests.test_bank_store import bank


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web,'JOBS',{})
    monkeypatch.setattr(web.CAPTURE,'is_active',lambda:False)
    monkeypatch.setattr(web,'get_runtime_config',lambda:{'USERTOKEN':'test','ABC':'test','AUTH_V':'test','STUDY_GRADE':'2'})
    web.app.config.update(TESTING=True)
    return web.app.test_client()


@pytest.mark.parametrize('source,release', [('class',2),('study','list')])
def test_start_stop_and_logs_continue_working(client,monkeypatch,source,release):
    proc = MagicMock()
    def spawn(source,task_id,release_id,**kwargs):
        web.JOBS[web._job_key(source,task_id,release_id)] = {
            'proc':proc,'done':False,'loop':True,'stopped':False,'logs':deque(['[临时缓存] hit']),
            'exit_code':None,'source':source,'task_id':task_id,'release_id':release_id,
        }
    monkeypatch.setattr(web,'_spawn_job',spawn)
    body = {'source':source,'task_id':1,'release_id':release,'loop':True}
    assert client.post('/api/start',json=body).get_json()['ok']
    assert client.post('/api/start',json=body).status_code == 400
    logs = client.get('/api/logs',query_string=body).get_json()
    assert logs['logs'] == ['[临时缓存] hit']
    assert client.post('/api/stop',json=body).get_json()['ok']
    proc.send_signal.assert_called_once()
    job = web.JOBS[web._job_key(source,1,release)]
    assert job['stopped'] and not job['loop']


@pytest.mark.parametrize('score,stopped,restarts', [(100,False,False),(81,False,True),(81,True,False)])
def test_loop_restart_full_score_and_stop(client,monkeypatch,score,stopped,restarts):
    key = web._job_key('class',1,2)
    proc = MagicMock()
    proc.stdout = BytesIO('[正式词库] hit\n'.encode())
    proc.returncode = 0
    web.JOBS[key] = {'proc':proc,'logs':deque(),'done':False,'exit_code':None,'loop':True,'stopped':stopped,
                     'source':'class','task_id':1,'release_id':2}
    monkeypatch.setattr(web,'_query_score',lambda *args:score)
    monkeypatch.setattr(web.time,'sleep',lambda *args:None)
    spawn = MagicMock()
    monkeypatch.setattr(web,'_spawn_job',spawn)
    web._reader_thread(key,proc)
    assert bool(spawn.call_count) is restarts
    assert web.JOBS[key]['logs'][0] == '[正式词库] hit'
    assert web.JOBS[key]['done']


def test_spawning_uses_cli_runner_and_preserves_logs(client,monkeypatch):
    popen = MagicMock()
    monkeypatch.setattr(web.subprocess,'Popen',popen)
    monkeypatch.setattr(web.threading,'Thread',MagicMock())
    monkeypatch.setattr(web,'build_subprocess_env',lambda config:{'snapshot':'current'})
    web._spawn_job('study',1,'list',loop=True,course_id='course',list_id='list',grade=2)
    args = popen.call_args.args[0]
    assert args[2:5] == ['-m','cidaren._runner','study']
    assert popen.call_args.kwargs['env'] == {'snapshot':'current'}
    key = web._job_key('study',1,'list')
    web.JOBS[key]['logs'].append('previous round')
    web._spawn_job('study',1,'list',loop=True)
    assert web.JOBS[key]['round'] == 2
    assert web.JOBS[key]['logs'][0] == 'previous round'


@pytest.mark.parametrize('source', ['class','study'])
def test_runner_uses_prepared_store_for_entire_task(bank,monkeypatch,source):
    monkeypatch.setattr(_runner,'prepare_default_store',lambda:bank)
    monkeypatch.setattr(_runner,'get_runtime_config',lambda:{'USERTOKEN':'t','ABC':'a','AUTH_V':'v','USER_AGENT':'ua'})
    monkeypatch.setattr(_runner.quiz,'Client',MagicMock())
    call = MagicMock()
    monkeypatch.setattr(_runner.quiz,'run_full' if source=='class' else 'run_study_full',call)
    monkeypatch.setattr(_runner.sys,'argv',['runner',source,'1','2'])
    _runner.main()
    assert call.call_args.kwargs['bank'] is bank
    assert not list((bank.path.parent/'runtime').glob('*.lock'))


def test_web_startup_holds_lease_during_server(client,bank,monkeypatch):
    monkeypatch.setattr(web,'prepare_default_store',lambda:bank)
    monkeypatch.setattr(web.CAPTURE,'recover_stale_proxy',lambda:None)
    monkeypatch.setattr(web.socket,'socket',MagicMock())
    monkeypatch.setenv('CIDAREN_NO_BROWSER','1')
    def serve(**kwargs):
        assert list((bank.path.parent/'runtime').glob('*.lock'))
        assert kwargs['host'] == '127.0.0.1' and kwargs['port'] == 5001
    monkeypatch.setattr(web.app,'run',serve)
    web.main()
    assert not list((bank.path.parent/'runtime').glob('*.lock'))
