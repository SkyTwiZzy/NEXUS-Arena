"""NEXUS 1.0: 1v1/2v2 independent Elo, calibration and career K/D.
Run separately because older test modules set their own DB at import.
"""
import base64, io, os, sys, tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
TMP=tempfile.TemporaryDirectory()
os.environ['NEXUS_DB']=str(Path(TMP.name)/'mode10.sqlite3')
os.environ['ADMIN_EMAIL']='owner-mode10@example.test'
os.environ['ADMIN_PASSWORD']='ExampleSafePassword10!'
sys.path.insert(0,str(ROOT/'server'))
from app import app, db, mode_rating

def player(i):
    c=TestClient(app)
    assert c.post('/api/auth/register', json={'email':f'mode10-{i}@example.test','password':'ExamplePassword12345','confirm':'ExamplePassword12345','accept_terms':True}).status_code==200
    assert c.patch('/api/me',json={'nickname':f'ModePlayer{i}','mlbb_id':str(40000+i),'server_id':'1234','game_role':'Mid'}).status_code==200
    return c

def owner():
    c=TestClient(app)
    assert c.post('/api/auth/login',json={'email':'owner-mode10@example.test','password':'ExampleSafePassword10!'}).status_code==200
    return c

def img():
    b=io.BytesIO(); Image.new('RGB',(400,240),'#39175f').save(b,format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()

def queue_group(clients,mode):
    leader=clients[0]
    assert leader.post('/api/lobby',json={}).status_code==200
    assert leader.post('/api/lobby/mode',json={'mode':mode}).status_code==200
    for other in clients[1:]:
        name=other.get('/api/me').json()['nickname']
        assert leader.post('/api/lobby/invite',json={'nickname':name}).status_code==200
        inv=other.get('/api/lobby').json()['invites'][0]
        assert other.post(f'/api/lobby/invites/{inv["id"]}/accept',json={}).status_code==200
    assert leader.post('/api/lobby/search',json={}).status_code==200
    return leader

def match_and_approve(clients,mode,winning_side=0):
    size={'1v1':1,'2v2':2,'5v5':5}[mode]
    queue_group(clients[:size],mode)
    queue_group(clients[size:],mode)
    m=clients[0].get('/api/matches').json()[0]
    assert m['mode']==mode and m['status']=='READY_CHECK'
    assert m['required_ready']==size*2 and len(m['players'])==size*2
    for c in clients:
        r=c.post(f'/api/matches/{m["id"]}/ready',json={'ready':True})
        assert r.status_code==200,r.text
    assert r.json()['ready_count']==2*size and r.json()['status']=='PENDING'
    for side in (0,1):
        captain_id=next(p['id'] for p in m['players'] if p['side']==side and p['captain'])
        captain=next(c for c in clients if c.get('/api/me').json()['id']==captain_id)
        result=captain.post(f'/api/matches/{m["id"]}/evidence',json={'winning_side':winning_side,'image_data':img()})
        assert result.status_code==200,result.text
    return m

def test_1v1_calibration_and_kd():
    a,b=player(1),player(2)
    staff=owner()
    assert a.get('/api/me').json()['ratings']['1v1']['rank']['games_played']==0
    assert a.post('/api/lobby/mode',json={'mode':'1v1'}).status_code==403
    for n in range(10):
        m=match_and_approve([a,b],'1v1')
        if n==0:
            pid=a.get('/api/me').json()['id']
            assert a.put(f'/api/admin/matches/{m["id"]}/players/{pid}/kda',json={'kills':9,'deaths':3,'assists':5}).status_code==403
            assert staff.put(f'/api/admin/matches/{m["id"]}/players/{pid}/kda',json={'kills':9,'deaths':3,'assists':5}).status_code==200
        r=staff.post(f'/api/admin/matches/{m["id"]}/resolve',json={'winning_side':0})
        assert r.status_code==200,r.text
        assert staff.post(f'/api/admin/matches/{m["id"]}/resolve',json={'winning_side':0}).status_code==409
        profile=a.get('/api/me').json()
        assert profile['wins']==0 and profile['elo']==1000  # never touches 5v5
        assert profile['ratings']['2v2']['elo']==1000 and profile['ratings']['2v2']['wins']==0
        assert profile['ratings']['1v1']['elo']==1000+(n+1)*25
        assert profile['ratings']['1v1']['rank']['is_calibrating']==(n<9)
        assert profile['ratings']['1v1']['rank']['games_played']==n+1
    assert profile['ratings']['1v1']['rank']['name']!='Калибровка'
    assert profile['kd']=={'kills':9,'deaths':3,'ratio':3.0}
    leaderboard=a.get('/api/leaderboard?mode=1v1').json()
    self_rating=next(x for x in leaderboard if x['id']==profile['id'])
    assert self_rating['elo']==1250 and self_rating['kd']['ratio']==3.0
    assert a.get('/api/leaderboard?mode=5v5').json()[0]['rank']['is_calibrating']
    assert a.get('/api/leaderboard?mode=INVALID').status_code==400


def test_2v2_party_and_capacity_guards():
    cs=[player(i) for i in (20,21,22,23)]
    m=match_and_approve(cs,'2v2',winning_side=1)
    staff=owner()
    assert staff.post(f'/api/admin/matches/{m["id"]}/resolve',json={'winning_side':1}).status_code==200
    for c in cs:
        profile=c.get('/api/me').json()
        assert profile['elo']==1000 and profile['wins']==0 and profile['losses']==0
        assert profile['ratings']['2v2']['wins']+profile['ratings']['2v2']['losses']==1
        assert profile['ratings']['1v1']['rank']['games_played']==0
    assert cs[0].post('/api/lobby',json={}).status_code==200
    assert cs[0].post('/api/lobby/mode',json={'mode':'1v1'}).status_code==200
    assert cs[0].post('/api/lobby/invite',json={'nickname':cs[1].get('/api/me').json()['nickname']}).status_code==403 or cs[0].post('/api/lobby/invite',json={'nickname':cs[1].get('/api/me').json()['nickname']}).status_code==409
    assert cs[0].post('/api/lobby/mode',json={'mode':'spoof'}).status_code==400
    # No approved stats -> zero deaths => undefined KD, not a fabricated number.
    assert cs[0].get('/api/me').json()['kd']['ratio'] is None
