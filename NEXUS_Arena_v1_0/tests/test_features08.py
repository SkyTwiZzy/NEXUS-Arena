"""NEXUS 0.8 workflow. Run separately from other test modules (isolated DB)."""
import base64,io,os,sys,tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
TMP=tempfile.TemporaryDirectory()
os.environ['NEXUS_DB']=str(Path(TMP.name)/'nexus08.sqlite3')
os.environ['ADMIN_EMAIL']='owner08@example.test'
os.environ['ADMIN_PASSWORD']='OwnerPassword08long!'
sys.path.insert(0,str(ROOT/'server'))
from app import app,db

def user(i):
 c=TestClient(app)
 assert c.post('/api/auth/register',json={'email':f'test08-{i}@example.test','password':'ExamplePassword12345','confirm':'ExamplePassword12345','accept_terms':True}).status_code==200
 assert c.patch('/api/me',json={'nickname':f'Verify{i}','mlbb_id':str(12340000+i),'server_id':'2233','game_role':'Mid'}).status_code==200
 return c

def owner():
 c=TestClient(app)
 r=c.post('/api/auth/login',json={'email':'owner08@example.test','password':'OwnerPassword08long!'})
 assert r.status_code==200,r.text
 return c

def img():
 image=Image.new('RGB',(800,440),'#54228d');b=io.BytesIO();image.save(b,format='PNG')
 return 'data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()

def ready_match(start):
 clients=[user(i+start) for i in range(10)]
 for c in clients:
  assert c.post('/api/lobby',json={}).status_code==200
  assert c.post('/api/lobby/search',json={}).status_code==200
 m=clients[0].get('/api/matches').json()[0]; assert m['status']=='READY_CHECK'
 for c in clients: assert c.post(f'/api/matches/{m["id"]}/ready',json={'ready':True}).status_code==200
 return clients,clients[0].get('/api/matches').json()[0]

def captain_for(players,match,side):
 leader=next(p for p in match['players'] if p['captain'] and p['side']==side)
 for c in players:
  if c.get('/api/me').json()['id']==leader['id']:return c
 assert False

def test_two_evidences_manual_review_kda_security():
 cs,m=ready_match(1);mid=m['id'];o=owner();cap_a= captain_for(cs,m,0);cap_b= captain_for(cs,m,1)
 other=next(c for c in cs if c not in (cap_a,cap_b))
 assert other.post(f'/api/matches/{mid}/evidence',json={'winning_side':0,'image_data':img()}).status_code==403
 assert cap_a.post(f'/api/matches/{mid}/evidence',json={'winning_side':0,'image_data':'data:image/png;base64,QUJD'}).status_code==400
 assert cap_a.post(f'/api/matches/{mid}/report',json={'winning_side':0}).status_code==409
 assert o.post(f'/api/admin/matches/{mid}/resolve',json={'winning_side':0}).status_code==409
 a=cap_a.post(f'/api/matches/{mid}/evidence',json={'winning_side':0,'image_data':img()})
 assert a.status_code==200,a.text
 assert a.json()['status']=='PENDING'
 assert other.get(f'/api/matches/{mid}/evidence/0').status_code==200
 stranger=user(200)
 assert stranger.get(f'/api/matches/{mid}/evidence/0').status_code==403
 assert o.post(f'/api/admin/matches/{mid}/resolve',json={'winning_side':0}).status_code==409
 b=cap_b.post(f'/api/matches/{mid}/evidence',json={'winning_side':0,'image_data':img()})
 assert b.status_code==200,b.text
 assert b.json()['status']=='REVIEW'
 assert cs[0].get('/api/matches').json()[0]['status']=='REVIEW'
 assert all(c.get('/api/me').json()['wins']==0 for c in cs)
 pid=m['players'][0]['id']
 assert other.put(f'/api/admin/matches/{mid}/players/{pid}/kda',json={'kills':4,'deaths':2,'assists':8}).status_code==403
 assert o.put(f'/api/admin/matches/{mid}/players/{pid}/kda',json={'kills':4,'deaths':2,'assists':8}).status_code==200
 assert o.put(f'/api/admin/matches/{mid}/players/{pid}/kda',json={'kills':-1,'deaths':0,'assists':0}).status_code==422
 assert o.put(f'/api/admin/matches/{mid}/players/{pid}/kda',json={'kills':4,'deaths':2,'assists':8}).status_code==200
 assert other.post(f'/api/admin/matches/{mid}/resolve',json={'winning_side':0}).status_code==403
 fin=o.post(f'/api/admin/matches/{mid}/resolve',json={'winning_side':0,'note':'Проверены оба изображения'})
 assert fin.status_code==200,fin.text
 assert fin.json()['loser']==1
 assert o.post(f'/api/admin/matches/{mid}/resolve',json={'winning_side':0}).status_code==409
 completed=cs[0].get('/api/matches').json()[0]
 assert completed['status']=='COMPLETED'
 assert completed['review']['note']=='Проверены оба изображения'
 for c in cs:
  me=c.get('/api/me').json();side=next(p['side'] for p in m['players'] if p['id']==me['id'])
  assert (me['wins'],me['losses'])==((1,0) if side==0 else (0,1))
  assert me['elo']==(1025 if side==0 else 975)
 for c in cs:
  if c.get('/api/me').json()['id']==pid:
   assert c.get('/api/me').json()['kda']=={'kills':4,'deaths':2,'assists':8}
   assert next(p for p in c.get('/api/leaderboard').json() if p['id']==pid)['kda']['kills']==4
 assert o.get('/api/admin/matches').json()[0]['players']
 # Deleting one participant must not wipe verified outcomes for everyone else.
 assert cs[0].post('/api/account/delete',json={'password':'ExamplePassword12345'}).status_code==200
 assert cs[1].get('/api/matches').json()[0]['status']=='COMPLETED'
 assert cs[1].get('/api/me').json()['wins']+cs[1].get('/api/me').json()['losses']==1
 for c in cs+[o,stranger]:c.close()

def test_moderator_role_conflicting_reports_and_cancel():
 cs,m=ready_match(300);mid=m['id'];o=owner();cap_a= captain_for(cs,m,0);cap_b= captain_for(cs,m,1)
 mod=cs[3];uid=mod.get('/api/me').json()['id']
 assert mod.post(f'/api/admin/users/{uid}/moderator',json={'enabled':True}).status_code==403
 assert o.post(f'/api/admin/users/{uid}/moderator',json={'enabled':True}).status_code==200
 assert mod.get('/api/me').status_code==401  # role change invalidates sessions
 assert mod.post('/api/auth/login',json={'email':'test08-303@example.test','password':'ExamplePassword12345'}).status_code==200
 assert mod.get('/api/me').json()['staff_role']=='MODERATOR'
 assert mod.get('/api/admin/matches').status_code==200
 assert mod.get('/api/admin/overview').status_code==403
 assert cap_a.post(f'/api/matches/{mid}/evidence',json={'winning_side':0,'image_data':img()}).status_code==200
 assert cap_b.post(f'/api/matches/{mid}/evidence',json={'winning_side':1,'image_data':img()}).json()['status']=='DISPUTED'
 assert mod.post(f'/api/admin/matches/{mid}/resolve',json={'winning_side':1,'note':'Проверены оба скриншота'}).status_code==200
 assert cs[0].get('/api/matches').json()[0]['winning_side']==1
 cs2,m2=ready_match(500)
 assert mod.post(f'/api/admin/matches/{m2["id"]}/cancel',json={'reason':'Соревновательная игра не состоялась'}).status_code==200
 assert cs2[0].get('/api/matches').json()[0]['status']=='CANCELLED'
 assert o.post(f'/api/admin/users/{uid}/moderator',json={'enabled':False}).status_code==200
 assert mod.get('/api/admin/matches').status_code==401
 assert mod.post('/api/auth/login',json={'email':'test08-303@example.test','password':'ExamplePassword12345'}).status_code==200
 assert mod.get('/api/admin/matches').status_code==403
 for c in cs+cs2+[o]:c.close()
