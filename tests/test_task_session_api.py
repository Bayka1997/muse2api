"""Run: python tests/test_task_session_api.py. ASGI mocks only; no Muse or Chromium."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_async_images import Client
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

async def check(module):
    account=module.store.add_account({'hatch_sess':'test-only'},'Account B')
    task=module.store.create_task('video','Test video')
    tid=task['id']
    module.store.update_task(tid,status='failed',account=account['id'],account_label='Account B',muse_url='https://muse.ai/thread/test-error')
    sessions={tid:{'account_id':account['id'],'active':False,'can_scroll':True,'expires_in':900}}
    screenshots=[];scrolls=[];opened=[]
    def info(task_id):
        if task_id not in sessions:raise FileNotFoundError()
        return sessions[task_id]
    def screenshot(task_id):
        screenshots.append(task_id);return b'\xff\xd8jpeg\xff\xd9'
    def scroll(task_id,delta):
        if sessions[task_id]['active']:raise PermissionError()
        scrolls.append((task_id,delta))
    def reopen(task_id,acc,url):
        opened.append((task_id,acc['id'],url))
        sessions[task_id]={'account_id':acc['id'],'active':False,'can_scroll':True,'expires_in':900}
    module.engine.task_views=SimpleNamespace(info=info,screenshot=screenshot,scroll=scroll)
    module.engine.reopen_task_view=reopen
    root='/admin/tasks/'+tid+'/session'
    async with Client(module.app,{'Authorization':'Bearer task-view-test'}) as client:
        for suffix,method,payload in (('', 'GET',None),('/screenshot','GET',None),('/scroll','POST',{'delta':500}),('/reopen','POST',None)):
            response=await client.request(method,root+suffix,json_data=payload,headers={'Authorization':'Bearer wrong'})
            assert response.status_code==401,(suffix,response.body)
        response=await client.get(root)
        assert response.status_code==200
        assert response.json()['account_label']=='Account B'
        assert 'target_id' not in response.json() and 'context_id' not in response.json()
        response=await client.get(root+'/screenshot')
        assert response.body==b'\xff\xd8jpeg\xff\xd9' and screenshots==[tid]
        response=await client.request('POST',root+'/scroll',json_data={'delta':50000})
        assert response.status_code==200 and scrolls==[(tid,1200)]
        sessions[tid]['active']=True;sessions[tid]['can_scroll']=False
        response=await client.request('POST',root+'/scroll',json_data={'delta':500})
        assert response.status_code==409
        sessions[tid]['account_id']='wrong-account'
        response=await client.get(root+'/screenshot')
        assert response.status_code==409 and screenshots==[tid]
        sessions.clear()
        response=await client.get(root)
        assert response.json()['available'] is False and response.json()['can_reopen'] is True
        response=await client.get(root+'/screenshot')
        assert response.status_code==410
        response=await client.request('POST',root+'/reopen')
        assert response.status_code==200,(response.status_code,response.body)
        assert opened==[(tid,account['id'],'https://muse.ai/thread/test-error')]
        assert response.json()['reopened'] is True
        response=await client.request('POST',root+'/reopen')
        assert response.status_code==200 and len(opened)==1
        sessions.clear();module.store.update_task(tid,status='processing')
        response=await client.request('POST',root+'/reopen')
        assert response.status_code==409 and len(opened)==1
        response=await client.get('/admin/tasks/missing/session')
        assert response.status_code==404
    print('PASS: viewer Bearer auth, task/account binding, JPEG response, scroll restrictions, expired sessions and correct-account reopening without duplicate views.')

if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='muse-view-api-') as directory:
        os.environ.update(MUSE2API_HOME=directory,MUSE2API_PROFILE_ROOT=directory,MUSE2API_KEY='task-view-test')
        import app
        asyncio.run(check(app))
