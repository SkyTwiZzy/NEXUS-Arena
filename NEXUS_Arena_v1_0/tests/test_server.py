"""Run: python -m pytest -q tests/test_server.py (requires pip install -r server/requirements.txt)."""
import os
import base64
import io
from PIL import Image
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TMP = tempfile.TemporaryDirectory()
os.environ['NEXUS_DB'] = str(Path(TMP.name)/'tests.sqlite3')
os.environ['ADMIN_EMAIL'] = 'developer@example.org'
os.environ['ADMIN_PASSWORD'] = 'TestOnlySuperSecret123'
sys.path.insert(0,str(Path(__file__).parents[1]/'server'))
from app import app


def make_account(email,nickname):
    client=TestClient(app)
    r=client.post('/api/auth/register',json={'email':email,'password':'SecurePassword123','confirm':'SecurePassword123','accept_terms':True})
    assert r.status_code==200,r.text
    r=client.patch('/api/me',json={'nickname':nickname,'mlbb_id':'1234567','server_id':'1234','game_role':'Mid'})
    assert r.status_code==200,r.text
    return client


def test_auth_clan_admin_tournament_and_chat():
    alice=make_account('alice@example.org','Alice')
    bob=make_account('bob@example.org','Bob')
    r=alice.post('/api/clans',json={'name':'Sky Fighters','tag':'SKF','description':'Team MLBB'})
    assert r.status_code==200,r.text
    cid=r.json()['id']
    assert alice.get('/api/clans/mine').json()['clan']['name']=='Sky Fighters'
    # Clan messages must be private, even if Bob knows the endpoint.
    assert bob.get('/api/clans/chat').status_code==403
    assert bob.post('/api/clans/chat',json={'body':'not yet member'}).status_code==403
    assert bob.post(f'/api/clans/{cid}/request',json={}).status_code==200
    reqs=alice.get('/api/clans/mine').json()['requests']
    assert len(reqs)==1
    assert bob.get('/api/clans/chat').status_code==403
    assert alice.post(f'/api/clans/requests/{reqs[0]["id"]}/approve',json={}).status_code==200
    assert bob.post('/api/clans/chat',json={'body':'Привет, клан!'}).status_code==200
    assert 'Привет' in alice.get('/api/clans/chat').json()[0]['body']
    assert bob.get('/api/clans/mine').json()['clan']['name']=='Sky Fighters'
    # No fake developer access by guessing endpoint.
    assert alice.get('/api/admin/overview').status_code==403
    assert alice.post('/api/admin/tournaments',json={'title':'Official','mode':'solo','slots':16,'prize_kzt':1000,'starts_at':'2027-02-01T15:00','rules':'Free entry'}).status_code==403
    assert alice.post('/api/tournaments',json={'title':'Community Event','mode':'solo','slots':16,'prize_kzt':2000,'starts_at':'2027-02-01T15:00','rules':'Free entry'}).status_code==200
    assert bob.get('/api/tournaments').json()[0]['official']==0
    dev=TestClient(app)
    assert dev.post('/api/auth/login',json={'email':'developer@example.org','password':'TestOnlySuperSecret123'}).status_code==200
    assert dev.get('/api/admin/overview').status_code==200
    assert dev.post('/api/admin/tournaments',json={'title':'NEXUS Open','mode':'solo','slots':16,'prize_kzt':0,'starts_at':'2027-04-01T12:00','rules':'Free entry'}).status_code==200
    assert any(t['official']==1 for t in bob.get('/api/tournaments').json())
    alice.close();bob.close();dev.close()


def test_lobby_real_invites_and_matchmaking():
    players=[make_account(f'testmatch{i}@example.org',f'Ranked{i}') for i in range(10)]
    a,b=players[0],players[1]
    assert a.post('/api/lobby',json={}).status_code==200
    assert a.post('/api/lobby/invite',json={'nickname':'Ranked1'}).status_code==200
    assert b.get('/api/lobby').json()['lobby'] is None
    invite=b.get('/api/lobby').json()['invites'][0]
    assert b.post(f'/api/lobby/invites/{invite["id"]}/accept',json={}).status_code==200
    assert len(a.get('/api/lobby').json()['members'])==2
    assert players[2].post('/api/lobby',json={}).status_code==200
    # two in one party, rest single, exactly 10 players.
    for user in players[3:]:assert user.post('/api/lobby',json={}).status_code==200
    assert a.post('/api/lobby/search',json={}).status_code==200
    for user in players[2:]:assert user.post('/api/lobby/search',json={}).status_code==200
    matches=a.get('/api/matches').json()
    assert len(matches)==1
    match=matches[0]
    assert match['status']=='READY_CHECK'
    assert len(match['players'])==10
    assert {p['side'] for p in match['players']}=={0,1}
    assert match['ready_count']==0
    assert match['host']['nickname'].startswith('Ranked')
    for c in players:
        assert c.post(f'/api/matches/{match["id"]}/ready',json={'ready':True}).status_code==200
    assert players[0].get('/api/matches').json()[0]['status']=='PENDING'
    captain_a=next(i for i,p in enumerate(players) if any(m['nickname']==f'Ranked{i}' and m['side']==0 and m['captain'] for m in match['players']))
    captain_b=next(i for i,p in enumerate(players) if any(m['nickname']==f'Ranked{i}' and m['side']==1 and m['captain'] for m in match['players']))
    image=Image.new('RGB',(800,400),'#56119a')
    img=io.BytesIO();image.save(img,format='PNG')
    encoded='data:image/png;base64,'+base64.b64encode(img.getvalue()).decode()
    assert players[captain_a].post(f'/api/matches/{match["id"]}/evidence',json={'winning_side':0,'image_data':encoded}).status_code==200
    assert players[captain_b].post(f'/api/matches/{match["id"]}/evidence',json={'winning_side':0,'image_data':encoded}).status_code==200
    assert a.get('/api/matches').json()[0]['status']=='REVIEW'
    dev=TestClient(app)
    assert dev.post('/api/auth/login',json={'email':'developer@example.org','password':'TestOnlySuperSecret123'}).status_code==200
    assert dev.post(f'/api/admin/matches/{match["id"]}/resolve',json={'winning_side':0}).status_code==200
    dev.close()
    assert a.get('/api/matches').json()[0]['status']=='COMPLETED'
    assert len(players[0].get('/api/leaderboard').json())>=10
    for c in players:c.close()


def test_auth_validation():
    stranger=TestClient(app)
    assert stranger.get('/api/me').status_code==401
    assert stranger.get('/api/clans').status_code==401
    assert stranger.post('/api/auth/login',json={'email':'alice@example.org','password':'wrongpassword'}).status_code==401
    stranger.close()


def test_reports_blocking_and_account_deletion():
    ca=make_account('moderator-test@example.org','ClanOwner')
    cb=make_account('chat-test@example.org','ChatGuest')
    assert ca.post('/api/clans',json={'name':'Test Moderation','tag':'MOD','description':''}).status_code==200
    cid=ca.get('/api/clans/mine').json()['clan']['id']
    assert cb.post(f'/api/clans/{cid}/request',json={}).status_code==200
    rid=ca.get('/api/clans/mine').json()['requests'][0]['id']
    assert ca.post(f'/api/clans/requests/{rid}/approve',json={}).status_code==200
    mid=cb.post('/api/clans/chat',json={'body':'Spam message test'}).json()['id']
    assert ca.post(f'/api/clans/chat/{mid}/report',json={'reason':'Спам в клановом чате'}).status_code==200
    admin=TestClient(app)
    assert admin.post('/api/auth/login',json={'email':'developer@example.org','password':'TestOnlySuperSecret123'}).status_code==200
    reports=admin.get('/api/admin/reports').json()
    assert any(x['message_id']==mid for x in reports)
    report_id=next(x['id'] for x in reports if x['message_id']==mid)
    assert admin.post(f'/api/admin/reports/{report_id}/remove',json={}).status_code==200
    assert ca.get('/api/clans/chat').json()==[]
    assert ca.post(f'/api/users/{cb.get("/api/me").json()["id"]}/block',json={}).status_code==200
    assert len(ca.get('/api/users/blocked').json())==1
    assert cb.post('/api/account/delete',json={'password':'SecurePassword123'}).status_code==200
    assert cb.get('/api/me').status_code==401
    ca.close();cb.close();admin.close()


def test_tournament_organizer_can_edit_time_and_close_registration():
    from datetime import datetime, timedelta, timezone
    owner=make_account('tour-owner@example.org','TourOwner')
    guest=make_account('tour-guest@example.org','TourGuest')
    other=make_account('tour-outsider@example.org','TourOutsider')
    future=(datetime.now(timezone.utc)+timedelta(days=14)).astimezone(timezone(timedelta(hours=5)))
    close=future-timedelta(days=1)
    payload={'title':'NEXUS Cup Autumn','mode':'solo','slots':4,'prize_kzt':2000,
             'starts_at':future.strftime('%Y-%m-%dT%H:%M'),
             'registration_closes_at':close.strftime('%Y-%m-%dT%H:%M'),
             'time_zone':'+05:00','rules':'Без взносов и ставок.'}
    created=owner.post('/api/tournaments',json=payload)
    assert created.status_code==200,created.text
    tid=created.json()['id']
    t=next(x for x in owner.get('/api/tournaments').json() if x['id']==tid)
    assert t['registration_open'] and t['can_manage'] and t['time_zone']=='+05:00'
    assert not next(x for x in guest.get('/api/tournaments').json() if x['id']==tid)['can_manage']
    assert guest.patch(f'/api/tournaments/{tid}',json=payload).status_code==403
    assert other.post(f'/api/tournaments/{tid}/registration',json={'open':False}).status_code==403
    assert guest.post(f'/api/tournaments/{tid}/join',json={}).status_code==200
    reduced={**payload,'slots':4,'title':'NEXUS Cup Edited','prize_kzt':3000}
    assert owner.patch(f'/api/tournaments/{tid}',json=reduced).status_code==200
    assert next(t for t in guest.get('/api/tournaments').json() if t['id']==tid)['title']=='NEXUS Cup Edited'
    # A different tournament with 2+ enrolled players cannot reduce its capacity below their count.
    assert owner.post(f'/api/tournaments/{tid}/registration',json={'open':False}).status_code==200
    t=next(t for t in guest.get('/api/tournaments').json() if t['id']==tid)
    assert t['status']=='CLOSED' and not t['registration_open'] and t['participants']==1
    assert other.post(f'/api/tournaments/{tid}/join',json={}).status_code==409
    assert owner.post(f'/api/tournaments/{tid}/registration',json={'open':True}).status_code==200
    assert other.post(f'/api/tournaments/{tid}/join',json={}).status_code==200
    assert owner.patch(f'/api/tournaments/{tid}',json={**reduced,'mode':'team'}).status_code==409
    assert owner.patch(f'/api/tournaments/{tid}',json={**reduced,'registration_closes_at':
               (future+timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')}).status_code==400
    # Already elapsed deadlines shut registration even without manual closure.
    past_deadline=(datetime.now(timezone.utc)-timedelta(minutes=5)).astimezone(timezone(timedelta(hours=5)))
    assert owner.patch(f'/api/tournaments/{tid}',json={**reduced,
        'registration_closes_at':past_deadline.strftime('%Y-%m-%dT%H:%M')}).status_code==200
    assert not next(t for t in guest.get('/api/tournaments').json() if t['id']==tid)['registration_open']
    assert owner.post(f'/api/tournaments/{tid}/registration',json={'open':True}).status_code==409
    for c in (owner,guest,other):c.close()


def test_rank_only_after_ten_confirmed_matches():
    from app import db, finalize_match, now
    players=[make_account(f'calib{i}@example.org',f'Calib{i}') for i in range(10)]
    initial=players[0].get('/api/me').json()
    assert initial['rank']['is_calibrating'] and initial['rank']['remaining']==10
    user_ids=[p.get('/api/me').json()['id'] for p in players]
    with db(True) as con:
        for match_index in range(10):
            match_id=con.execute("INSERT INTO matches(status,created_at) VALUES('REVIEW',?)",(now(),)).lastrowid
            for i,uid in enumerate(user_ids):
                con.execute('INSERT INTO match_members(match_id,user_id,side,captain) VALUES(?,?,?,?)',
                            (match_id,uid,0 if i<5 else 1,1 if i in (0,5) else 0))
            # Test only the ELO/calibration calculation with pre-reviewed synthetic matches.
            # The public resolve API still requires both captain screenshots.
            finalize_match(con,match_id,0)
            if match_index==8:
                stats=con.execute('SELECT wins,losses,elo FROM users WHERE id=?',(user_ids[0],)).fetchone()
                assert stats['wins']+stats['losses']==9
    result=players[0].get('/api/me').json()
    assert result['rank']['is_calibrating'] is False
    assert result['rank']['remaining']==0
    assert result['rank']['name'] in ('Страж','Охотник')
    assert result['wins']+result['losses']==10
    for client in players:client.close()
