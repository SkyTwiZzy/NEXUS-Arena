"""NEXUS 0.9 role permissions, sponsor configuration, owner bootstrapping.
Run separately: pytest -q tests/test_roles_sponsorship09.py
"""
import os,sys,tempfile
from pathlib import Path
from fastapi.testclient import TestClient
TMP=tempfile.TemporaryDirectory()
os.environ['NEXUS_DB']=str(Path(TMP.name)/'test09.sqlite3')
os.environ['ADMIN_EMAIL']='testowner09@example.test'
os.environ['ADMIN_PASSWORD']='TestOwner09secure!'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from app import app,db,initialize,check_password

def account(i):
    c=TestClient(app)
    pw='TestPassword09long!'
    r=c.post('/api/auth/register',json={'email':f'player{i}@example.test','password':pw,'confirm':pw,'accept_terms':True})
    assert r.status_code==200,r.text
    assert c.patch('/api/me',json={'nickname':f'Tester{i}','mlbb_id':str(333000+i),'server_id':'18','game_role':'Mid'}).status_code==200
    return c

def owner():
    c=TestClient(app)
    r=c.post('/api/auth/login',json={'email':'testowner09@example.test','password':'TestOwner09secure!'})
    assert r.status_code==200,r.text
    return c

def test_owner_and_roles():
    o=owner();u1=account(1);u2=account(2);u3=account(3)
    assert o.get('/api/me').json()['staff_role']=='OWNER'
    assert u1.get('/api/me').json()['staff_role']=='PLAYER'
    uid1=u1.get('/api/me').json()['id'];uid2=u2.get('/api/me').json()['id'];uid3=u3.get('/api/me').json()['id'];oid=o.get('/api/me').json()['id']
    assert u1.post(f'/api/admin/users/{uid1}/role',json={'role':'DEVELOPER'}).status_code==403
    assert o.post(f'/api/admin/users/{oid}/role',json={'role':'PLAYER'}).status_code==403
    assert o.post(f'/api/admin/users/{uid1}/role',json={'role':'OWNER'}).status_code==400
    assert o.post(f'/api/admin/users/{uid1}/role',json={'role':'DEVELOPER'}).status_code==200
    assert u1.get('/api/me').status_code==401 # sessions invalidated after promotion
    assert u1.post('/api/auth/login',json={'email':'player1@example.test','password':'TestPassword09long!'}).status_code==200
    assert u1.get('/api/me').json()['staff_role']=='DEVELOPER'
    assert u1.post(f'/api/admin/users/{uid2}/role',json={'role':'ADMIN'}).status_code==200
    assert u1.post(f'/api/admin/users/{uid3}/role',json={'role':'MODERATOR'}).status_code==200
    assert u1.post(f'/api/admin/users/{uid2}/role',json={'role':'DEVELOPER'}).status_code==403
    assert u2.post('/api/auth/login',json={'email':'player2@example.test','password':'TestPassword09long!'}).status_code==200
    assert u2.get('/api/me').json()['staff_role']=='ADMIN'
    assert u2.get('/api/admin/overview').status_code==200
    assert u2.post(f'/api/admin/users/{uid3}/role',json={'role':'DEVELOPER'}).status_code==403
    assert u3.post('/api/auth/login',json={'email':'player3@example.test','password':'TestPassword09long!'}).status_code==200
    assert u3.get('/api/me').json()['staff_role']=='MODERATOR'
    assert u3.get('/api/admin/matches').status_code==200
    assert u3.get('/api/admin/overview').status_code==403
    assert u1.post(f'/api/admin/users/{oid}/role',json={'role':'PLAYER'}).status_code==403
    audit=o.get('/api/admin/roles/audit')
    assert audit.status_code==200 and len(audit.json())>=3
    with db() as con:
        result=con.execute('SELECT password_hash FROM users WHERE id=?',(oid,)).fetchone()
        assert check_password('TestOwner09secure!',result['password_hash'])
        assert 'TestOwner09secure!' not in result['password_hash']
    for c in (o,u1,u2,u3):c.close()

def test_sponsor_owner_only_and_validation():
    o=owner();u=account(12)
    assert u.put('/api/admin/sponsor',json={'checkout_url':'https://example.com/support','note':'support'}).status_code==403
    assert o.put('/api/admin/sponsor',json={'checkout_url':'http://example.com','note':'A'}).status_code==400
    assert o.put('/api/admin/sponsor',json={'checkout_url':'https://127.0.0.1/pay','note':'A'}).status_code==400
    assert o.put('/api/admin/sponsor',json={'checkout_url':'javascript:alert(1)','note':'A'}).status_code==400
    assert o.put('/api/admin/sponsor',json={'checkout_url':'https://example.com/support','note':'Добровольное спонсирование'}).status_code==200
    d=u.get('/api/fund');assert d.status_code==200 and d.json()['payments_enabled']
    assert d.json()['sponsor_url']=='https://example.com/support'
    assert o.put('/api/admin/sponsor',json={'checkout_url':'','note':'Пока закрыто'}).status_code==200
    assert u.get('/api/fund').json()['payments_enabled'] is False
    o.close();u.close()

def test_claim_existing_account_with_verified_password():
    # Simulate first-registration followed by owner setup for that same email.
    c=account(50)
    with db(True) as con:
        p=con.execute('SELECT email,password_hash FROM users WHERE email=?',('player50@example.test',)).fetchone()
        assert p and check_password('TestPassword09long!',p['password_hash'])
    # The full bootstrapping verification requires a fresh isolated DB; the
    # function is separately exercised in test_bootstrap_owner09.py.
    c.close()


def test_secure_password_rotation():
    c=account(44)
    assert c.post('/api/me/change-password',json={'current_password':'wrong','new_password':'UpdatedTestPassword09!','confirm':'UpdatedTestPassword09!'}).status_code==403
    assert c.post('/api/me/change-password',json={'current_password':'TestPassword09long!','new_password':'UpdatedTestPassword09!','confirm':'UpdatedTestPassword09!'}).status_code==200
    assert c.get('/api/me').status_code==401
    assert c.post('/api/auth/login',json={'email':'player44@example.test','password':'TestPassword09long!'}).status_code==401
    assert c.post('/api/auth/login',json={'email':'player44@example.test','password':'UpdatedTestPassword09!'}).status_code==200
    c.close()
