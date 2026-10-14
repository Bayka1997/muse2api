import sys,importlib.util,json
from types import SimpleNamespace
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
spec=importlib.util.spec_from_file_location('candidate',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
clock=[0.0]
m.time=SimpleNamespace(time=lambda:clock[0],sleep=lambda s:clock.__setitem__(0,clock[0]+s))
engine=object.__new__(m.MuseEngine)
engine._scroll_bottom=lambda:None
engine.page=SimpleNamespace(js=lambda _:json.dumps({'tail':'Connecting...','cnt':0,'txt':'','stop':True}))
engine.attachments=lambda:[] if clock[0]<20 else [{'src':'generated','tid':'image','w':100,'h':100}]
try:
 result=engine._wait_attachment('',30,'image')
 print('RESULT',result['src'],'elapsed',round(clock[0],1))
 assert result['src']=='generated'
except m.MuseGenerationError as e:
 print('RESULT rejected_before_generated elapsed',round(clock[0],1),'error',str(e))
 if '--assert' in sys.argv:raise

clock[0] = 0.0
engine.attachments = lambda: []
engine.page = SimpleNamespace(js=lambda _: json.dumps({
    'cnt': 1, 'txt': 'Your call:', 'stop': False,
    'choices': ['Re-roll take 2', 'Keep take 1 as-is', 'Skip the scene'],
}))
try:
    engine._wait_attachment('', 30, 'video')
except m.MuseGenerationError as exc:
    assert 'MUSE_ACTION_REQUIRED:' in str(exc)
    assert clock[0] < 2
else:
    raise AssertionError('Interactive choice must release the generation slot promptly')
print('PASS: interactive choice ends the wait without waiting for timeout')

clock[0] = 0.0
clicks = [0]
def reroll_page(script):
    if 'button.click()' in script:
        clicks[0] += 1
        return True
    return json.dumps({'cnt': 1, 'txt': 'Your call:', 'stop': False,
                       'choices': ['Re-roll take 2', 'Keep take 1 as-is']})
engine.page = SimpleNamespace(js=reroll_page)
engine.attachments = lambda: ([{'src': 'result.mp4', 'vSrc': 'result.mp4', 'hasVideo': True, 'w': 100, 'h': 100}]
                              if clicks[0] and clock[0] >= 2 else [])
result = engine._wait_attachment('', 30, 'video')
assert result['vSrc'] == 'result.mp4'
assert clicks[0] == 1
print('PASS: Re-roll is selected once and the resulting video is collected')
