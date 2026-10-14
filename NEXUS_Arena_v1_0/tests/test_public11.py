"""NEXUS 1.1 deployment tests. Run separately from older test modules.

pytest -q tests/test_public11.py
"""
import os, tempfile, sys
from pathlib import Path
from fastapi.testclient import TestClient

TEMP=tempfile.TemporaryDirectory()
os.environ['NEXUS_DB']=str(Path(TEMP.name) / 'arena.sqlite3')
os.environ['COOKIE_SECURE']='1'
os.environ.pop('PUBLIC_ORIGIN',None)
os.environ['ADMIN_EMAIL']='owner-public-test@example.test'
os.environ['ADMIN_PASSWORD']='strong-not-real-owner-password-1133'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'server'))
from app import app
from tools.launch_local import require_owner


def test_public_https_session_and_origin():
    require_owner(Path(os.environ['NEXUS_DB']))
    c=TestClient(app)
    assert c.get('/api/status').json()['online'] is True
    assert c.get('/').status_code==200
    auth={'email':'owner-public-test@example.test','password':'strong-not-real-owner-password-1133'}
    host='arena-staging.example.org'
    signed=c.post('/api/auth/login',json=auth,headers={'Host':host,'Origin':'https://'+host})
    assert signed.status_code==200, signed.text
    assert 'Secure' in signed.headers.get('set-cookie','')
    assert signed.json()
    assert c.post('/api/auth/login',json=auth,headers={'Host':host,'Origin':'https://evil.example.org'}).status_code==403
    c.close()


def test_public_origin_override():
    # PUBLIC_ORIGIN pins the host for a domain-based production deployment.
    # Separate process import is required for setting env before app module import.
    from app import PUBLIC_ORIGIN
    assert PUBLIC_ORIGIN==''
