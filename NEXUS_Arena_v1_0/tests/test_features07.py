"""Security and feature integration tests for NEXUS 0.7."""
import base64
import io
import os
import sys
import tempfile
from pathlib import Path
from PIL import Image
from fastapi.testclient import TestClient
import pytest

ROOT=Path(__file__).resolve().parents[1]
TEMPDIR=tempfile.TemporaryDirectory()
os.environ['NEXUS_DB']=str(Path(TEMPDIR.name)/'new.sqlite3')
os.environ['ADMIN_EMAIL']='v07dev@example.org'
os.environ['ADMIN_PASSWORD']='SafeTestOnlyPass123!'
sys.path.insert(0,str(ROOT/'server'))
# This test file is launched separately to isolate DB from the old test module.
from app import app, db, now


def player(i):
    client=TestClient(app)
    rv=client.post('/api/auth/register',json={'email':f'new{i}@example.org','password':'ExampleTestPassword123','confirm':'ExampleTestPassword123','accept_terms':True})
    assert rv.status_code==200,rv.text
    rv=client.patch('/api/me',json={'nickname':f'TeamNexus{i}','mlbb_id':f'98765{i:03d}','server_id':'2345','game_role':'Roam'})
    assert rv.status_code==200,rv.text
    return client


def get_admin():
    c=TestClient(app)
    assert c.post('/api/auth/login',json={'email':'v07dev@example.org','password':'SafeTestOnlyPass123!'}).status_code==200
    return c


def ten_players(n):
    cs=[player(i+n) for i in range(10)]
    for c in cs:
        assert c.post('/api/lobby',json={}).status_code==200
        assert c.post('/api/lobby/search',json={}).status_code==200
    m=cs[0].get('/api/matches').json()[0]
    assert m['ready_count']==0 and m['status']=='READY_CHECK'
    assert m['host']['mlbb_id'].startswith('98765')
    return cs,m


def test_ready_checks_cancel_admin():
    cs,m=ten_players(100)
    mid=m['id']
    assert cs[0].post(f'/api/matches/{mid}/ready',json={'ready':True}).json()['ready_count']==1
    assert cs[0].post(f'/api/matches/{mid}/ready',json={'ready':True}).json()['ready_count']==1
    stranger=player(399)
    assert stranger.post(f'/api/matches/{mid}/ready',json={'ready':True}).status_code==403
    assert cs[1].post(f'/api/matches/{mid}/ready',json={'ready':False}).status_code==200
    x=cs[0].get('/api/matches').json()[0]
    assert x['status']=='CANCELLED' and 'отказался' in x['cancel_reason']
    assert cs[0].get('/api/me').json()['wins']==0
    assert cs[0].get('/api/lobby').json()['lobby']['status']=='OPEN'
    # A second match; only admin cancels it with explicit reason.
    cs2,m2=ten_players(200)
    adm=get_admin()
    assert cs2[0].post(f'/api/admin/matches/{m2["id"]}/cancel',json={'reason':'Ошибка сервера'}).status_code==403
    assert adm.post(f'/api/admin/matches/{m2["id"]}/cancel',json={'reason':'Ошибка сервера на матче'}).status_code==200
    assert cs2[0].get('/api/matches').json()[0]['status']=='CANCELLED'
    assert adm.post(f'/api/admin/matches/{m2["id"]}/cancel',json={'reason':'Ещё одна отмена'}).status_code==409
    for c in cs+cs2+[stranger,adm]:c.close()


def test_all_ready_and_expiry():
    cs,m=ten_players(500)
    mid=m['id']
    for c in cs:
        x=c.post(f'/api/matches/{mid}/ready',json={'ready':True})
        assert x.status_code==200,x.text
    m=cs[0].get('/api/matches').json()[0]
    assert m['ready_count']==10 and m['status']=='PENDING'
    assert cs[0].post(f'/api/matches/{mid}/ready',json={'ready':False}).status_code==409
    admin=get_admin()
    assert admin.post(f'/api/admin/matches/{mid}/cancel',json={'reason':'Нет связи с хостом'}).status_code==200
    cs2,m2=ten_players(600)
    with db(True) as con:
        con.execute('UPDATE matches SET ready_deadline=? WHERE id=?',(now()-1,m2['id']))
    assert cs2[0].get('/api/matches').json()[0]['status']=='CANCELLED'
    assert cs2[0].get('/api/lobby').json()['lobby']['status']=='OPEN'
    for c in cs+cs2+[admin]:c.close()


def test_avatar_feedback_and_fund():
    u=player(999)
    admin=get_admin()
    im=Image.new('RGB',(640,480),'#8735da')
    bio=io.BytesIO();im.save(bio,format='PNG')
    uri='data:image/png;base64,'+base64.b64encode(bio.getvalue()).decode()
    r=u.post('/api/me/avatar',json={'image_data':uri})
    assert r.status_code==200,r.text
    me=u.get('/api/me').json()
    assert me['avatar_updated_at']>0
    assert u.get(f'/api/users/{me["id"]}/avatar').headers['content-type']=='image/jpeg'
    assert u.post('/api/me/avatar',json={'image_data':'data:image/svg+xml;base64,PHN2Zz4='}).status_code==400
    assert u.post('/api/feedback',json={'kind':'REVIEW','subject':'Классный интерфейс','body':'Интерфейс удобный и понятный, продолжайте','rating':5}).status_code==200
    assert u.post('/api/feedback',json={'kind':'BUG','subject':'Баг в поиске','body':'Во время теста завис поиск команды'}).status_code==200
    reviews=admin.get('/api/admin/feedback').json()
    rev=next(x for x in reviews if x['kind']=='REVIEW')
    bug=next(x for x in reviews if x['kind']=='BUG')
    assert u.patch(f'/api/admin/feedback/{rev["id"]}',json={'status':'FIXED','public':True,'admin_reply':'Спасибо!'}).status_code==403
    assert not u.get('/api/reviews').json()
    assert admin.patch(f'/api/admin/feedback/{rev["id"]}',json={'status':'RESOLVED','public':True,'admin_reply':'Спасибо за отзыв!'}).status_code==200
    assert len(u.get('/api/reviews').json())==1
    assert admin.patch(f'/api/admin/feedback/{bug["id"]}',json={'status':'FIXED','public':True,'admin_reply':'Исправили, спасибо'}).status_code==200
    assert not any(x['id']==bug['id'] for x in u.get('/api/reviews').json())
    assert any(x['status']=='FIXED' for x in u.get('/api/feedback/mine').json())
    assert u.put('/api/admin/fund',json={'headline':'Развитие','description':'Тестовая информация о планах','goal_kzt':30000}).status_code==403
    assert admin.put('/api/admin/fund',json={'headline':'Развитие NEXUS','description':'Добавим кланы и статистику','goal_kzt':30000}).status_code==200
    fund=u.get('/api/fund').json()
    assert fund['goal_kzt']==30000 and fund['payments_enabled'] is False and fund['collected_kzt'] is None
    assert u.request('DELETE','/api/me/avatar',json={}).status_code==200
    assert u.get(f'/api/users/{me["id"]}/avatar').status_code==404
    u.close();admin.close()
