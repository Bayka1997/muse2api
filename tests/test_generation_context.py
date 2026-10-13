"""Run: python tests/test_generation_context.py. No browser, network or accounts."""
import ast
import __future__
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import re

ROOT = Path(__file__).resolve().parents[1]
engine_tree = ast.parse((ROOT / 'engine.py').read_text())
engine_class = next(n for n in engine_tree.body if isinstance(n, ast.ClassDef) and n.name == 'MuseEngine')
method = next(n for n in engine_class.body if isinstance(n, ast.FunctionDef) and n.name == 'generation_context')
ns = {'urlsplit': urlsplit, 'urlunsplit': urlunsplit, 're': re}
exec(compile(ast.Module(body=[method], type_ignores=[]), '<engine-context>', 'exec'), ns)
read_context = ns['generation_context']


class Page:
    def __init__(self, url):
        self.url = url

    def js(self, expression):
        assert expression == 'window.location.href'
        return self.url


worker = SimpleNamespace(_local=threading.local(), page=Page('https://muse.ai/thread/real-id?secret=hidden#fragment'))
assert read_context(worker) == {}  # No link from an old, unsubmitted conversation.
worker._local.generation_submitted = True
assert read_context(worker) == {'muse_url': 'https://muse.ai/thread/real-id'}
worker.page.url = 'https://muse.ai/thread/new'
assert read_context(worker) == {'muse_url': 'https://muse.ai/thread/real-id'}
for value in ('https://muse.ai/thread/new', 'https://muse.ai/login', 'https://evil.test/thread/id', 'javascript:alert(1)'):
    worker._local.generation_url = None
    worker.page.url = value
    assert read_context(worker) == {}

# Separate concurrent worker contexts cannot read each other's URLs.
contexts = []
barrier = threading.Barrier(2)
shared = SimpleNamespace(_local=threading.local())
def concurrent_worker(name):
    shared._local.generation_submitted = True
    shared._local.generation_url = 'https://muse.ai/thread/' + name
    barrier.wait()
    contexts.append(read_context(shared)['muse_url'])
threads = [threading.Thread(target=concurrent_worker, args=(name,)) for name in ('account-a','account-b')]
for thread in threads: thread.start()
for thread in threads: thread.join()
assert set(contexts) == {'https://muse.ai/thread/account-a', 'https://muse.ai/thread/account-b'}

# Exercise the actual app wrapper with a failing generation: context must be saved before cleanup.
class AuthError(RuntimeError): pass
class GenerationError(RuntimeError): pass
account = {'id':'account-a','label':'Muse A','cookies':{'test':'mock'}}
records = []
cleanup = []
class Engine:
    current_acc_id = None
    def start(self): pass
    def generate(self, *args, **kwargs):
        kwargs['on_progress'](25)
        raise GenerationError('Generation timed out')
    def generation_context(self):
        assert not cleanup, 'Context was read after cleanup'
        return {'muse_url':'https://muse.ai/thread/error-thread'}
    def reset_thread(self): cleanup.append('reset')
    def close_current_page(self): cleanup.append('close')
app_tree = ast.parse((ROOT / 'app.py').read_text())
function = next(n for n in app_tree.body if isinstance(n,ast.FunctionDef) and n.name == '_run_generation_locked')
namespace = {
    'time':time,'engine':Engine(),'store':SimpleNamespace(tasks={},mark=lambda *args:None),
    'ACCOUNT_POOL':SimpleNamespace(acquire=lambda **kwargs:account,release=lambda aid:None),
    '_renew_and_persist':lambda *args,**kwargs:account,'_sync_cookies':lambda aid:None,
    'MuseAuthError':AuthError,'MuseGenerationError':GenerationError,
    'log':SimpleNamespace(info=lambda *args:None),
}
exec(compile(ast.Module(body=[function],type_ignores=[]),'<generation-wrapper>','exec', flags=__future__.annotations.compiler_flag),namespace)
try:
    namespace['_run_generation_locked']('prompt','image',30,on_context=records.append)
    raise AssertionError('Expected generation failure')
except GenerationError:
    pass
assert records[0]['account'] == 'account-a'
assert records[0]['account_label'] == 'Muse A'
assert 'muse_url' not in records[0]
assert records[-1]['muse_url'] == 'https://muse.ai/thread/error-thread'
assert records[-1]['account'] == 'account-a'
assert cleanup

# A fallback account that never submits must leave the first thread paired with its owner.
accounts = {
    'account-a': {'id':'account-a','label':'Muse A','cookies':{'test':'a'}},
    'account-b': {'id':'account-b','label':'Muse B','cookies':{'test':'b'}},
}
calls = {'n': 0}
class SwitchingEngine(Engine):
    def generate(self, *args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            kwargs['on_progress'](40)
            raise GenerationError('等待生成超时，未出现新的生成结果')
        raise GenerationError('任务准备超时，已停止发送')
    def generation_context(self):
        if calls['n'] == 1:
            return {'muse_url':'https://muse.ai/thread/owned-by-a'}
        return {}
switched = []
namespace['engine'] = SwitchingEngine()
namespace['ACCOUNT_POOL'] = SimpleNamespace(
    acquire=lambda **kwargs: accounts['account-b'] if kwargs.get('force_rotate') else accounts['account-a'],
    release=lambda aid: None)
try:
    namespace['_run_generation_locked']('prompt','video',30,on_context=switched.append)
    raise AssertionError('Expected the fallback attempt to fail')
except GenerationError as exc:
    assert '任务准备超时' in str(exc)
saved = [item for item in switched if item.get('muse_url')]
assert saved and saved[-1]['muse_url'] == 'https://muse.ai/thread/owned-by-a'
assert saved[-1]['account'] == 'account-a'
assert all(item.get('account') != 'account-b' or item.get('muse_url') for item in switched)
assert not any(item.get('account') == 'account-b' and not item.get('muse_url') for item in switched)
print('PASS: safe conversation URLs, query stripping, confirmed submissions, worker isolation and failure capture before cleanup.')
