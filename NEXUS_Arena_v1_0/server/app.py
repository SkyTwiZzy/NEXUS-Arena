"""NEXUS Arena — networked FastAPI service, SQLite persistence, same-origin cookie auth.
Skill competitions have no deposits, stakes, payments or payout processing.
"""
import hashlib
import base64
import binascii
import io
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, UnidentifiedImageError

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
DB_FILE = Path(os.getenv('NEXUS_DB', str(ROOT / 'server' / 'arena.sqlite3')))
PUBLIC_ORIGIN = os.getenv('PUBLIC_ORIGIN', '').rstrip('/')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', '0') == '1'
COOKIE_NAME = 'nexus_session'
SESSION_SECONDS = 60 * 60 * 24 * 14
MAX_BODY = 20000
AVATAR_MAX_BODY = 420000
EVIDENCE_MAX_BODY = 1_650_000
READY_TIMEOUT_SECONDS = 90
RANKS = [
 ('Искра',0,849,'✦','#9CA3AF'),('Разведчик',850,999,'⌖','#A3E635'),
 ('Страж',1000,1149,'⬟','#4ADE80'),('Охотник',1150,1299,'➶','#22D3EE'),
 ('Фантом',1300,1449,'◈','#60A5FA'),('Владыка',1450,1599,'♜','#C4B5FD'),
 ('Астрал',1600,1799,'☄','#D8B4FE'),('Вознесённый',1800,2049,'✧','#F9A8D4'),
 ('Парагон',2050,2349,'♛','#FCD34D'),('Нексус',2350,None,'✹','#F9FAFB')]
LOGIN_ATTEMPTS = {}
LOGIN_LOCK = Lock()


# Tournament-free quarterly competitive seasons. Matches retain their lifetime ELO.
# The server defines the calendar, so every device sees the same season.
# UTC+05:00 is used for Kazakhstan (including Oral/Uralsk) in 2026.
SEASON_ZONE = timezone(timedelta(hours=5))
SEASON_FIRST_YEAR = 2026
SEASON_FIRST_QUARTER = 3  # October 2026: Season 01


def season_metadata(number: int, reference: datetime | None = None) -> dict:
    if number < 1:
        raise ValueError('Season number must be positive')
    year, quarter = divmod(SEASON_FIRST_YEAR * 4 + SEASON_FIRST_QUARTER + number - 1, 4)
    month = quarter * 3 + 1
    begins = datetime(year, month, 1, tzinfo=SEASON_ZONE)
    next_year, next_quarter = divmod(year * 4 + quarter + 1, 4)
    ends_exclusive = datetime(next_year, next_quarter * 3 + 1, 1, tzinfo=SEASON_ZONE)
    current = reference or datetime.now(SEASON_ZONE)
    title = f'SEASON {number:02d}'
    return {
        'number': number, 'title': title,
        'starts_at': int(begins.timestamp()),
        'ends_at': int(ends_exclusive.timestamp()),
        'start_date': begins.strftime('%Y-%m-%d'),
        'end_date': (ends_exclusive - timedelta(days=1)).strftime('%Y-%m-%d'),
        'next_start_date': ends_exclusive.strftime('%Y-%m-%d'),
        'time_zone': '+05:00',
        'status': 'upcoming' if current < begins else 'ended' if current >= ends_exclusive else 'active',
        'seconds_left': max(0, int((ends_exclusive - current).total_seconds())),
    }


def season_number(reference: datetime | None = None) -> int:
    current = (reference or datetime.now(SEASON_ZONE)).astimezone(SEASON_ZONE)
    quarter_index = current.year * 4 + (current.month - 1) // 3
    return max(1, quarter_index - (SEASON_FIRST_YEAR * 4 + SEASON_FIRST_QUARTER) + 1)


def season_catalog(reference: datetime | None = None) -> dict:
    moment = (reference or datetime.now(SEASON_ZONE)).astimezone(SEASON_ZONE)
    number = season_number(moment)
    current = season_metadata(number, moment)
    return {
        'current': current,
        'next': season_metadata(number + 1, moment),
        'history': [season_metadata(i, moment) for i in range(max(1, number - 7), number + 1)][::-1],
        'rule': 'Каждый сезон длится три календарных месяца: октябрь–декабрь, январь–март, апрель–июнь, июль–сентябрь.',
        'rating_policy': 'Ранги и ELO не сбрасываются при смене сезона.',
    }


def now(): return int(time.time())


def fail(status: int, message: str):
    raise HTTPException(status_code=status, detail=message)


PLACEMENT_GAMES = 10
# Legacy users.elo/wins/losses is the canonical 5v5 rating from releases <= 0.9.
# New modes are persisted separately and never affect the old rating.
GAME_MODES = {'1v1': 1, '2v2': 2, '5v5': 5}

def mode_size(mode):
    if mode not in GAME_MODES: fail(400, 'Неизвестный игровой режим')
    return GAME_MODES[mode]

def mode_rating(con, user_id, mode):
    mode_size(mode)
    if mode == '5v5':
        data = record(con, 'SELECT elo,wins,losses FROM users WHERE id=?', (user_id,))
    else:
        data = record(con,'SELECT elo,wins,losses FROM mode_ratings WHERE user_id=? AND mode=?',(user_id,mode))
    if data is None: data = {'elo':1000,'wins':0,'losses':0}
    return {**data, 'mode':mode,'rank':rank(data['elo'],data['wins']+data['losses'])}

def all_ratings(con,user_id):
    return {mode:mode_rating(con,user_id,mode) for mode in GAME_MODES}


def rank(elo, games_played=PLACEMENT_GAMES):
    if games_played < PLACEMENT_GAMES:
        return {'name':'Калибровка','division':None,'symbol':'◎','color':'#A78BFA',
                'elo':None,'nextElo':None,'is_calibrating':True,
                'games_played':games_played,'games_required':PLACEMENT_GAMES,
                'remaining':PLACEMENT_GAMES-games_played}
    row = [r for r in RANKS if elo >= r[1]][-1]
    name, low, high, glyph, color = row
    division = None if high is None else ('III' if elo-low < (high-low+1)/3 else 'II' if elo-low < 2*(high-low+1)/3 else 'I')
    return {'name':name, 'division':division, 'symbol':glyph,'color':color,'elo':elo,
            'nextElo':None if high is None else high+1,
            'is_calibrating':False,'games_played':games_played,'games_required':PLACEMENT_GAMES,
            'remaining':0}


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex()+':'+derived.hex()


def check_password(password, stored):
    try:
        salt_hex, digest = stored.split(':',1)
        return hmac.compare_digest(password_hash(password, bytes.fromhex(salt_hex)).split(':',1)[1], digest)
    except (ValueError, TypeError): return False


def connect():
    con = sqlite3.connect(DB_FILE, timeout=15, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=10000')
    return con


@contextmanager
def db(write=False):
    con = connect()
    try:
        if write: con.execute('BEGIN IMMEDIATE')
        yield con
        if write: con.commit()
    except Exception:
        if write: con.rollback()
        raise
    finally: con.close()


def record(con, sql, params=()):
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def initialize():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with db(True) as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL, nickname TEXT UNIQUE, mlbb_id TEXT,
          server_id TEXT, game_role TEXT DEFAULT 'Универсал', elo INTEGER DEFAULT 1000,
          wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
          is_admin INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0,
          created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS clans(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          tag TEXT NOT NULL UNIQUE COLLATE NOCASE, description TEXT NOT NULL DEFAULT '',
          leader_id INTEGER NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS clan_members(
          clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
          role TEXT NOT NULL DEFAULT 'MEMBER', joined_at INTEGER NOT NULL,
          PRIMARY KEY(clan_id,user_id));
        CREATE TABLE IF NOT EXISTS clan_requests(
          id INTEGER PRIMARY KEY AUTOINCREMENT, clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id), status TEXT DEFAULT 'PENDING',
          created_at INTEGER NOT NULL, UNIQUE(clan_id,user_id));
        CREATE TABLE IF NOT EXISTS clan_messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
          sender_id INTEGER NOT NULL REFERENCES users(id), body TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS lobbies(
          id INTEGER PRIMARY KEY AUTOINCREMENT, leader_id INTEGER NOT NULL REFERENCES users(id),
          status TEXT NOT NULL DEFAULT 'OPEN', created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS lobby_members(
          lobby_id INTEGER NOT NULL REFERENCES lobbies(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
          PRIMARY KEY(lobby_id,user_id));
        CREATE TABLE IF NOT EXISTS lobby_invites(
          id INTEGER PRIMARY KEY AUTOINCREMENT, lobby_id INTEGER NOT NULL REFERENCES lobbies(id) ON DELETE CASCADE,
          invitee_id INTEGER NOT NULL REFERENCES users(id), inviter_id INTEGER NOT NULL REFERENCES users(id),
          status TEXT NOT NULL DEFAULT 'PENDING', created_at INTEGER NOT NULL,
          UNIQUE(lobby_id,invitee_id));
        CREATE TABLE IF NOT EXISTS matches(
          id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL DEFAULT 'PENDING',
          winning_side INTEGER, report_a INTEGER, report_b INTEGER, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS match_members(
          match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id), side INTEGER NOT NULL,
          captain INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(match_id,user_id));
        CREATE TABLE IF NOT EXISTS tournaments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER NOT NULL REFERENCES users(id),
          title TEXT NOT NULL, mode TEXT NOT NULL, slots INTEGER NOT NULL,
          prize_kzt INTEGER NOT NULL DEFAULT 0, starts_at TEXT NOT NULL, rules TEXT NOT NULL,
          official INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'OPEN',
          created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tournament_entries(
          tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id), joined_at INTEGER NOT NULL,
          PRIMARY KEY(tournament_id,user_id));
        CREATE TABLE IF NOT EXISTS announcements(
          id INTEGER PRIMARY KEY AUTOINCREMENT, author_id INTEGER NOT NULL REFERENCES users(id),
          title TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS clan_chat_idx ON clan_messages(clan_id,id);
        CREATE INDEX IF NOT EXISTS lobby_queue_idx ON lobbies(status,created_at);
        CREATE TABLE IF NOT EXISTS blocked_users(
          blocker_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          blocked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          PRIMARY KEY(blocker_id,blocked_id));
        CREATE TABLE IF NOT EXISTS message_reports(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id INTEGER NOT NULL REFERENCES clan_messages(id) ON DELETE CASCADE,
          reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
          created_at INTEGER NOT NULL, UNIQUE(message_id,reporter_id));
        ''')
        # Additive migrations; preserve the original 5v5 rating in users.
        lobby_columns = [r['name'] for r in con.execute('PRAGMA table_info(lobbies)')]
        if 'mode' not in lobby_columns:
            con.execute("ALTER TABLE lobbies ADD COLUMN mode TEXT NOT NULL DEFAULT '5v5'")
        match_columns_modes = [r['name'] for r in con.execute('PRAGMA table_info(matches)')]
        if 'mode' not in match_columns_modes:
            con.execute("ALTER TABLE matches ADD COLUMN mode TEXT NOT NULL DEFAULT '5v5'")
        con.execute('''CREATE TABLE IF NOT EXISTS mode_ratings(
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            mode TEXT NOT NULL CHECK(mode IN ('1v1','2v2')),
            elo INTEGER NOT NULL DEFAULT 1000,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id,mode))''')
        if 'deleted' not in [r['name'] for r in con.execute('PRAGMA table_info(clan_messages)')]:
            con.execute('ALTER TABLE clan_messages ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0')
        tournament_columns = [r['name'] for r in con.execute('PRAGMA table_info(tournaments)')]
        if 'registration_closes_at' not in tournament_columns:
            con.execute("ALTER TABLE tournaments ADD COLUMN registration_closes_at TEXT NOT NULL DEFAULT ''")
        if 'time_zone' not in tournament_columns:
            con.execute("ALTER TABLE tournaments ADD COLUMN time_zone TEXT NOT NULL DEFAULT '+05:00'")
        user_columns = [r['name'] for r in con.execute('PRAGMA table_info(users)')]
        if 'is_moderator' not in user_columns:
            con.execute('ALTER TABLE users ADD COLUMN is_moderator INTEGER NOT NULL DEFAULT 0')
        if 'staff_role' not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN staff_role TEXT NOT NULL DEFAULT 'PLAYER'")
            # Existing developer and match-moderator roles are preserved.
            con.execute("UPDATE users SET staff_role='DEVELOPER' WHERE is_admin=1")
            con.execute("UPDATE users SET staff_role='MODERATOR' WHERE is_moderator=1 AND is_admin=0")
        if 'avatar_updated_at' not in user_columns:
            con.execute('ALTER TABLE users ADD COLUMN avatar_updated_at INTEGER NOT NULL DEFAULT 0')
        match_columns = [r['name'] for r in con.execute('PRAGMA table_info(matches)')]
        if 'ready_deadline' not in match_columns:
            con.execute('ALTER TABLE matches ADD COLUMN ready_deadline INTEGER NOT NULL DEFAULT 0')
        if 'host_user_id' not in match_columns:
            con.execute('ALTER TABLE matches ADD COLUMN host_user_id INTEGER')
        if 'cancel_reason' not in match_columns:
            con.execute("ALTER TABLE matches ADD COLUMN cancel_reason TEXT NOT NULL DEFAULT ''")
        if 'cancelled_by' not in match_columns:
            con.execute('ALTER TABLE matches ADD COLUMN cancelled_by INTEGER')
        member_columns = [r['name'] for r in con.execute('PRAGMA table_info(match_members)')]
        if 'ready' not in member_columns:
            con.execute('ALTER TABLE match_members ADD COLUMN ready INTEGER NOT NULL DEFAULT 0')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS avatars (
          user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          image BLOB NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS feedback (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          author_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
          rating INTEGER, match_id INTEGER REFERENCES matches(id) ON DELETE SET NULL,
          status TEXT NOT NULL DEFAULT 'NEW', admin_reply TEXT NOT NULL DEFAULT '',
          public INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS feedback_author_idx ON feedback(author_id,id);
        CREATE TABLE IF NOT EXISTS project_fund (
          id INTEGER PRIMARY KEY CHECK (id=1), headline TEXT NOT NULL DEFAULT 'Развитие NEXUS Arena',
          description TEXT NOT NULL DEFAULT 'Помогайте предложениями, отзывами и тестированием новых функций.',
          goal_kzt INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO project_fund(id) VALUES(1);
        ''')
        con.executescript("""
        CREATE TABLE IF NOT EXISTS match_evidence(
          match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
          side INTEGER NOT NULL CHECK(side IN (0,1)),
          captain_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          winning_side INTEGER NOT NULL CHECK(winning_side IN (0,1)),
          image BLOB NOT NULL, submitted_at INTEGER NOT NULL,
          PRIMARY KEY(match_id, side));
        CREATE TABLE IF NOT EXISTS match_player_stats(
          match_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
          kills INTEGER NOT NULL DEFAULT 0, deaths INTEGER NOT NULL DEFAULT 0,
          assists INTEGER NOT NULL DEFAULT 0, updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          updated_at INTEGER NOT NULL, PRIMARY KEY(match_id,user_id),
          FOREIGN KEY(match_id,user_id) REFERENCES match_members(match_id,user_id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS match_stat_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
          user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          editor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          before_json TEXT,after_json TEXT NOT NULL,changed_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS match_decisions(
          match_id INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
          reviewer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          winning_side INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', reviewed_at INTEGER NOT NULL);
        """)
        con.executescript('''
        CREATE TABLE IF NOT EXISTS role_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          target_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          previous_role TEXT NOT NULL,
          new_role TEXT NOT NULL,
          changed_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sponsor_config(
          id INTEGER PRIMARY KEY CHECK(id=1),
          checkout_url TEXT NOT NULL DEFAULT '',
          note TEXT NOT NULL DEFAULT 'Поддержать развитие проекта можно через проверенную страницу партнёра.',
          updated_at INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO sponsor_config(id) VALUES(1);
        ''')
        # A developer account is provisioned by deployment environment, never by client flags.
        admin_email = os.getenv('ADMIN_EMAIL','').strip().lower()
        admin_pass = os.getenv('ADMIN_PASSWORD','')
        if admin_email and admin_pass:
            if len(admin_pass) < 12: raise RuntimeError('ADMIN_PASSWORD must contain at least 12 characters')
            existing = record(con,'SELECT id,password_hash,staff_role FROM users WHERE email=?',(admin_email,))
            current_owner = record(con,"SELECT id,email FROM users WHERE staff_role='OWNER' LIMIT 1")
            if current_owner and current_owner['email'] != admin_email:
                raise RuntimeError('Owner is already configured. You cannot create another owner.')
            if existing:
                # Claiming an existing account requires its actual password, even on local launch.
                if not check_password(admin_pass,existing['password_hash']):
                    raise RuntimeError('Existing account password is incorrect. The role was not changed.')
                if existing['staff_role'] != 'OWNER':
                    con.execute("UPDATE users SET staff_role='OWNER',is_admin=1,is_moderator=1 WHERE id=?",(existing['id'],))
                    con.execute('INSERT INTO role_audit(actor_id,target_id,previous_role,new_role,changed_at) VALUES(?,?,?,?,?)',
                                (existing['id'],existing['id'],existing['staff_role'],'OWNER',now()))
            elif current_owner is None:
                con.execute("INSERT INTO users(email,password_hash,nickname,is_admin,is_moderator,staff_role,created_at) VALUES(?,?,?,?,?,?,?)",
                            (admin_email,password_hash(admin_pass),os.getenv('ADMIN_NICK','NEXUS_OWNER'),1,1,'OWNER',now()))


app = FastAPI(title='NEXUS Arena API', version='1.3.0', docs_url=None, redoc_url=None)
initialize()


@app.middleware('http')
async def security(request: Request, call_next):
    if request.url.path.startswith('/api/') and request.method in ('POST','PUT','PATCH','DELETE'):
        origin = request.headers.get('origin')
        # The app can run behind Caddy, Render or a Cloudflare HTTPS tunnel.
        # Proxies may forward HTTP internally, so derive the public scheme from
        # COOKIE_SECURE when no explicit PUBLIC_ORIGIN has been configured.
        # The browser Origin must always match the request Host (or pinned origin).
        host = request.headers.get('host', '')
        allowed = PUBLIC_ORIGIN or ('https://' if COOKIE_SECURE else 'http://') + host
        if origin and origin.rstrip('/') != allowed: return JSONResponse({'detail':'Недопустимый источник запроса'}, status_code=403)
        length = request.headers.get('content-length')
        max_size = (AVATAR_MAX_BODY if request.url.path == '/api/me/avatar' else
                    EVIDENCE_MAX_BODY if request.url.path.endswith('/evidence') else MAX_BODY)
        if length and int(length) > max_size: return JSONResponse({'detail':'Слишком большой запрос'},status_code=413)
        if not request.headers.get('content-type','').lower().startswith('application/json'):
            return JSONResponse({'detail':'Ожидается JSON'},status_code=415)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
    if COOKIE_SECURE: response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'no-cache'
    return response


class Auth(BaseModel):
    email: str
    password: str


class Register(Auth):
    confirm: str
    accept_terms: bool = False

class PasswordChange(BaseModel):
    current_password: str
    new_password: str
    confirm: str


class Profile(BaseModel):
    nickname: str
    mlbb_id: str
    server_id: str
    game_role: str = 'Универсал'


class ClanInput(BaseModel):
    name: str
    tag: str
    description: str = ''


class Message(BaseModel):
    body: str


class Invite(BaseModel):
    nickname: str


class TournamentInput(BaseModel):
    title: str
    mode: str
    slots: int
    prize_kzt: int = 0
    starts_at: str
    rules: str
    registration_closes_at: str = ''
    time_zone: str = '+05:00'


class RegistrationState(BaseModel):
    open: bool


class AnnouncementInput(BaseModel):
    title: str
    body: str


class MatchReport(BaseModel):
    winning_side: int

class MatchEvidenceInput(BaseModel):
    winning_side: int
    image_data: str

class MatchDecision(BaseModel):
    winning_side: int
    note: str = ''

class PlayerKDA(BaseModel):
    kills: int = Field(ge=0,le=999)
    deaths: int = Field(ge=0,le=999)
    assists: int = Field(ge=0,le=999)

class ModeratorToggle(BaseModel):
    enabled: bool

class RoleChange(BaseModel):
    role: str

class SponsorSettings(BaseModel):
    checkout_url: str = ''
    note: str = ''

class ReadyResponse(BaseModel):
    ready: bool

class LobbyMode(BaseModel):
    mode: str


class AvatarUpload(BaseModel):
    image_data: str

class FeedbackInput(BaseModel):
    kind: str
    subject: str
    body: str
    rating: int | None = None
    match_id: int | None = None

class FeedbackUpdate(BaseModel):
    status: str
    admin_reply: str = ''
    public: bool = False

class MatchCancel(BaseModel):
    reason: str

class FundUpdate(BaseModel):
    headline: str
    description: str
    goal_kzt: int = 0


def clean(s): return s.strip()


def user_for(request, con, allow_incomplete=False):
    token = request.cookies.get(COOKIE_NAME,'')
    if not token: fail(401,'Войдите в аккаунт')
    u=record(con,'SELECT u.id,u.email,u.nickname,u.mlbb_id,u.server_id,u.game_role,u.elo,u.wins,u.losses,u.is_admin,u.is_moderator,u.staff_role,u.is_banned,u.avatar_updated_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?',
             (hashlib.sha256(token.encode()).hexdigest(),now()))
    if not u: fail(401,'Сессия истекла. Войдите снова')
    if u['is_banned']: fail(403,'Аккаунт заблокирован')
    if not allow_incomplete and not u['nickname']: fail(409,'Сначала завершите профиль')
    u['ratings']=all_ratings(con,u['id'])
    u['rank']=u['ratings']['5v5']['rank']
    u['kda']=total_kda(con,u['id'])
    u['kd']=kd_ratio(u['kda'])
    return u


def kd_ratio(totals):
    # Deaths == 0 has no mathematically defined K/D. Do not fabricate infinity.
    deaths = totals['deaths']
    return {'kills':totals['kills'], 'deaths':deaths,
            'ratio':round(totals['kills']/deaths,2) if deaths else None}


def total_kda(con,user_id):
    # Only admin-approved matches count toward career statistics.
    return record(con,"""SELECT COALESCE(SUM(s.kills),0) AS kills,COALESCE(SUM(s.deaths),0) AS deaths,
          COALESCE(SUM(s.assists),0) AS assists FROM match_player_stats s
          JOIN matches m ON m.id=s.match_id WHERE s.user_id=? AND m.status='COMPLETED'""",(user_id,))


def staff_for(request,con):
    u=user_for(request,con)
    if not (u['is_admin'] or u['is_moderator']): fail(403,'Нужны права модератора или разработчика')
    return u


def admin_for(request, con):
    u = user_for(request,con)
    if not u['is_admin']: fail(403,'Нужны права разработчика')
    return u


def create_session(con,u_id):
    token=secrets.token_urlsafe(40)
    con.execute('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',
                (hashlib.sha256(token.encode()).hexdigest(),u_id,now()+SESSION_SECONDS))
    return token


def attach_cookie(resp,token):
    resp.set_cookie(COOKIE_NAME,token,httponly=True,secure=COOKIE_SECURE,samesite='strict',max_age=SESSION_SECONDS,path='/')
    return resp


def throttle(request, identity):
    ip = request.client.host if request.client else 'unknown'
    ip = ip + ':' + clean(identity).lower()[:120]
    with LOGIN_LOCK:
        t=now()
        hits=[n for n in LOGIN_ATTEMPTS.get(ip,[]) if t-n<600]
        if len(hits)>=16: fail(429,'Слишком много попыток. Повторите позже')
        hits.append(t)
        LOGIN_ATTEMPTS[ip]=hits


@app.post('/api/auth/register')
def register(form:Register,request:Request):
    throttle(request,form.email)
    email=clean(form.email).lower()
    if not re.fullmatch(r'[^\s@]{1,80}@[^\s@]{1,80}\.[^\s@]{2,30}',email): fail(400,'Некорректный email')
    if len(form.password)<10 or len(form.password)>128: fail(400,'Пароль: от 10 до 128 символов')
    if form.password != form.confirm: fail(400,'Пароли не совпадают')
    if not form.accept_terms: fail(400,'Необходимо принять правила платформы')
    with db(True) as con:
        if record(con,'SELECT id FROM users WHERE email=?',(email,)): fail(409,'Email уже зарегистрирован')
        cur=con.execute('INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?)',(email,password_hash(form.password),now()))
        token=create_session(con,cur.lastrowid)
        u=user_for_token(con,cur.lastrowid)
    return attach_cookie(JSONResponse({'user':u}),token)


def user_for_token(con,id):
    u=record(con,'SELECT id,email,nickname,mlbb_id,server_id,game_role,elo,wins,losses,is_admin,is_moderator,staff_role,avatar_updated_at FROM users WHERE id=?',(id,))
    u['ratings']=all_ratings(con,id)
    u['rank']=u['ratings']['5v5']['rank']
    u['kda']=total_kda(con,id)
    u['kd']=kd_ratio(u['kda'])
    return u


@app.post('/api/auth/login')
def login(form:Auth,request:Request):
    throttle(request,form.email)
    with db(True) as con:
        u=record(con,'SELECT * FROM users WHERE email=?',(clean(form.email).lower(),))
        if not u or not check_password(form.password,u['password_hash']): fail(401,'Неверный email или пароль')
        if u['is_banned']: fail(403,'Аккаунт заблокирован')
        token=create_session(con,u['id'])
        result=user_for_token(con,u['id'])
    return attach_cookie(JSONResponse({'user':result}),token)


@app.post('/api/auth/logout')
def logout(request:Request):
    token=request.cookies.get(COOKIE_NAME,'')
    if token:
        with db(True) as con: con.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
    resp=JSONResponse({'ok':True})
    resp.delete_cookie(COOKIE_NAME,path='/')
    return resp


@app.get('/api/me')
def me(request:Request):
    with db() as con: return user_for(request,con,True)


@app.get('/api/analytics/me')
def player_analytics(request: Request):
    """Read-only competitive insights derived solely from moderator-approved matches.

    Do not invent hero picks, damage or gold: those require an authorized MLBB feed
    (or dedicated, clearly-labelled manual statistics fields) and aren't stored here.
    """
    with db() as con:
        user = user_for(request, con)
        items = rows(con, """SELECT m.id, m.mode, m.created_at, m.winning_side,
                    mm.side, ps.kills, ps.deaths, ps.assists
                  FROM match_members mm
                  JOIN matches m ON m.id=mm.match_id
                  LEFT JOIN match_player_stats ps
                    ON ps.match_id=m.id AND ps.user_id=mm.user_id
                  WHERE mm.user_id=? AND m.status='COMPLETED'
                  ORDER BY m.id DESC""", (user['id'],))
        modes = {}
        for mode in GAME_MODES:
            subset = [m for m in items if (m.get('mode') or '5v5') == mode]
            recorded = [m for m in subset if m['kills'] is not None]
            kills = sum(int(m['kills']) for m in recorded)
            deaths = sum(int(m['deaths']) for m in recorded)
            assists = sum(int(m['assists']) for m in recorded)
            wins = sum(m['side'] == m['winning_side'] for m in subset)
            modes[mode] = {
                'matches': len(subset), 'wins': wins, 'losses': len(subset)-wins,
                'win_rate': round(100*wins/len(subset), 1) if subset else None,
                'kda_matches': len(recorded),
                'kills': kills, 'deaths': deaths, 'assists': assists,
                'kd': round(kills/deaths, 2) if deaths else None,
                'elo': user['ratings'][mode]['elo'],
                'rank': user['ratings'][mode]['rank'],
            }
        history = [{
            'id': m['id'], 'mode': m.get('mode') or '5v5',
            'created_at': m['created_at'],
            'result': 'WIN' if m['side'] == m['winning_side'] else 'LOSS',
            'elo_delta': 25 if m['side'] == m['winning_side'] else -25,
            'kills': m['kills'], 'deaths': m['deaths'], 'assists': m['assists'],
            'stats_recorded': m['kills'] is not None,
        } for m in items[:30]]
        return {
            'total_confirmed_matches': len(items),
            'modes': modes,
            'history': history,
            'hero_stats_available': False,
            'data_source': 'Подтверждённые администрацией результаты NEXUS Arena',
        }


@app.post('/api/me/change-password')
def change_password(form:PasswordChange,request:Request):
    if not 12<=len(form.new_password)<=128 or form.new_password!=form.confirm:
        fail(400,'Новый пароль: 12–128 символов, повторение должно совпадать')
    with db(True) as con:
        u=user_for(request,con,True)
        saved=record(con,'SELECT password_hash FROM users WHERE id=?',(u['id'],))
        if not check_password(form.current_password,saved['password_hash']):
            fail(403,'Неверный текущий пароль')
        if form.current_password==form.new_password:
            fail(400,'Новый пароль должен отличаться от предыдущего')
        con.execute('UPDATE users SET password_hash=? WHERE id=?',(password_hash(form.new_password),u['id']))
        con.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],))
    response=JSONResponse({'ok':True,'login_required':True})
    response.delete_cookie(COOKIE_NAME,path='/')
    return response


@app.patch('/api/me')
def profile(form:Profile,request:Request):
    nickname=clean(form.nickname)
    if not (2<=len(nickname)<=24) or not re.fullmatch(r'[\w\- .А-Яа-яЁё]{2,24}',nickname,re.UNICODE): fail(400,'Ник: 2–24 буквы, цифры и пробелы')
    if not re.fullmatch(r'\d{4,20}',clean(form.mlbb_id)) or not re.fullmatch(r'\d{1,12}',clean(form.server_id)): fail(400,'Проверьте MLBB Player ID и Server ID')
    if form.game_role not in ('Универсал','EXP','Jungle','Mid','Gold','Roam'): fail(400,'Некорректная роль')
    with db(True) as con:
        u=user_for(request,con,True)
        exists=record(con,'SELECT id FROM users WHERE nickname=? COLLATE NOCASE',(nickname,))
        if exists and exists['id']!=u['id']: fail(409,'Ник уже занят')
        con.execute('UPDATE users SET nickname=?,mlbb_id=?,server_id=?,game_role=? WHERE id=?',
                    (nickname,clean(form.mlbb_id),clean(form.server_id),form.game_role,u['id']))
        return user_for_token(con,u['id'])


@app.post('/api/me/avatar')
def upload_avatar(form:AvatarUpload,request:Request):
    value=form.image_data
    if len(value)>300000 or not re.match(r'^data:image/(?:jpeg|png|webp);base64,',value):
        fail(400,'Поддерживаются PNG, JPG и WebP до 200 КБ')
    try:
        raw=base64.b64decode(value.split(',',1)[1],validate=True)
        if len(raw)>200000: fail(413,'Изображение слишком большое')
        with Image.open(io.BytesIO(raw)) as im:
            if im.format not in ('JPEG','PNG','WEBP') or im.width>5000 or im.height>5000:
                fail(400,'Некорректный формат или размер изображения')
            im=ImageOps.exif_transpose(im)
            im.thumbnail((320,320))
            im=im.convert('RGB')
            buf=io.BytesIO()
            im.save(buf,'JPEG',quality=82,optimize=True)
            safe_bytes=buf.getvalue()
    except (ValueError,UnidentifiedImageError,OSError,binascii.Error):
        fail(400,'Не удалось прочитать изображение')
    with db(True) as con:
        u=user_for(request,con)
        stamp=now()
        con.execute('INSERT INTO avatars(user_id,image,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET image=excluded.image,updated_at=excluded.updated_at',
                    (u['id'],safe_bytes,stamp))
        con.execute('UPDATE users SET avatar_updated_at=? WHERE id=?',(stamp,u['id']))
        return {'ok':True,'avatar_updated_at':stamp}

@app.delete('/api/me/avatar')
def remove_avatar(request:Request):
    with db(True) as con:
        u=user_for(request,con)
        con.execute('DELETE FROM avatars WHERE user_id=?',(u['id'],))
        con.execute('UPDATE users SET avatar_updated_at=0 WHERE id=?',(u['id'],))
    return {'ok':True}

@app.get('/api/users/{user_id}/avatar')
def avatar(user_id:int,request:Request):
    with db() as con:
        user_for(request,con)
        img=record(con,'SELECT image FROM avatars WHERE user_id=?',(user_id,))
        if not img: fail(404,'Аватарка не установлена')
        return Response(img['image'],media_type='image/jpeg',headers={'Cache-Control':'private, max-age=90','X-Content-Type-Options':'nosniff'})

def my_clan(con,uid):
    return record(con,'''SELECT c.*,cm.role AS my_role,(SELECT COUNT(*) FROM clan_members WHERE clan_id=c.id) AS members_count,
        u.nickname AS leader FROM clan_members cm JOIN clans c ON c.id=cm.clan_id
        JOIN users u ON u.id=c.leader_id WHERE cm.user_id=?''',(uid,))


@app.get('/api/clans')
def clans(request:Request,q:str=''):
    with db() as con:
        user_for(request,con)
        pattern='%'+q.strip()[:40]+'%'
        return rows(con,'''SELECT c.id,c.name,c.tag,c.description,c.leader_id,u.nickname AS leader,
          (SELECT COUNT(*) FROM clan_members WHERE clan_id=c.id) AS members_count FROM clans c JOIN users u ON u.id=c.leader_id
          WHERE c.name LIKE ? OR c.tag LIKE ? ORDER BY members_count DESC,c.id DESC LIMIT 60''',(pattern,pattern))


@app.get('/api/clans/mine')
def clan_mine(request:Request):
    with db() as con:
        u=user_for(request,con)
        clan=my_clan(con,u['id'])
        if not clan: return {'clan':None,'members':[],'requests':[],'pending':rows(con,'''SELECT cr.id,cr.status,c.name,c.tag FROM clan_requests cr JOIN clans c ON c.id=cr.clan_id WHERE cr.user_id=? AND cr.status='PENDING' ''',(u['id'],))}
        members=rows(con,'''SELECT u.id,u.nickname,u.elo,u.wins,u.losses,u.avatar_updated_at,cm.role FROM clan_members cm JOIN users u ON u.id=cm.user_id WHERE cm.clan_id=? ORDER BY cm.role DESC,cm.joined_at''',(clan['id'],))
        requests=rows(con,'''SELECT cr.id,u.nickname,u.elo,u.wins,u.losses,cr.created_at FROM clan_requests cr JOIN users u ON u.id=cr.user_id WHERE cr.clan_id=? AND cr.status='PENDING' ORDER BY cr.created_at''',(clan['id'],)) if clan['my_role'] in ('LEADER','OFFICER') else []
        return {'clan':clan,'members':members,'requests':requests,'pending':[]}


@app.post('/api/clans')
def clan_create(form:ClanInput,request:Request):
    name=clean(form.name);tag=clean(form.tag).upper();desc=clean(form.description)
    if not 3<=len(name)<=36 or not re.fullmatch(r'[\w\- .А-Яа-яЁё]{3,36}',name,re.UNICODE): fail(400,'Название клана: от 3 до 36 символов')
    if not re.fullmatch(r'[A-Z0-9]{2,5}',tag): fail(400,'Тег: 2–5 латинских букв или цифр')
    if len(desc)>300: fail(400,'Описание слишком длинное')
    with db(True) as con:
        u=user_for(request,con)
        if my_clan(con,u['id']): fail(409,'Вы уже состоите в клане')
        if record(con,'SELECT id FROM clans WHERE name=? COLLATE NOCASE OR tag=? COLLATE NOCASE',(name,tag)): fail(409,'Название или тег занят')
        cid=con.execute('INSERT INTO clans(name,tag,description,leader_id,created_at) VALUES(?,?,?,?,?)',(name,tag,desc,u['id'],now())).lastrowid
        con.execute('INSERT INTO clan_members(clan_id,user_id,role,joined_at) VALUES(?,?,?,?)',(cid,u['id'],'LEADER',now()))
        return {'id':cid,'name':name,'tag':tag}


@app.post('/api/clans/{clan_id}/request')
def clan_apply(clan_id:int,request:Request):
    with db(True) as con:
        u=user_for(request,con)
        if my_clan(con,u['id']): fail(409,'Сначала выйдите из текущего клана')
        if not record(con,'SELECT id FROM clans WHERE id=?',(clan_id,)): fail(404,'Клан не найден')
        existing=record(con,'SELECT id,status FROM clan_requests WHERE clan_id=? AND user_id=?',(clan_id,u['id']))
        if existing and existing['status']=='PENDING': fail(409,'Заявка уже отправлена')
        if existing:
            con.execute("UPDATE clan_requests SET status='PENDING',created_at=? WHERE id=?",(now(),existing['id']))
        else: con.execute('INSERT INTO clan_requests(clan_id,user_id,created_at) VALUES(?,?,?)',(clan_id,u['id'],now()))
        return {'ok':True,'status':'PENDING'}


@app.post('/api/clans/requests/{request_id}/{decision}')
def clan_decide(request_id:int,decision:str,request:Request):
    if decision not in ('approve','reject'): fail(404,'Неизвестное действие')
    with db(True) as con:
        u=user_for(request,con)
        clan=my_clan(con,u['id'])
        req=record(con,'SELECT * FROM clan_requests WHERE id=? AND status=?',(request_id,'PENDING'))
        if not clan or clan['my_role'] not in ('LEADER','OFFICER') or not req or req['clan_id']!=clan['id']:
            fail(403,'Недостаточно прав или заявка уже обработана')
        if decision=='approve':
            if record(con,'SELECT clan_id FROM clan_members WHERE user_id=?',(req['user_id'],)):
                con.execute("UPDATE clan_requests SET status='REJECTED' WHERE id=?",(request_id,))
                fail(409,'Игрок уже вступил в другой клан')
            if clan['members_count']>=100: fail(409,'В клане нет свободных мест')
            con.execute('INSERT INTO clan_members(clan_id,user_id,joined_at) VALUES(?,?,?)',(clan['id'],req['user_id'],now()))
        con.execute('UPDATE clan_requests SET status=? WHERE id=?',('APPROVED' if decision=='approve' else 'REJECTED',request_id))
        return {'ok':True}


@app.post('/api/clans/leave')
def clan_leave(request:Request):
    with db(True) as con:
        u=user_for(request,con)
        clan=my_clan(con,u['id'])
        if not clan: fail(404,'Вы не в клане')
        if clan['my_role']=='LEADER':
            if clan['members_count']!=1: fail(409,'Сначала передайте лидерство другому участнику')
            con.execute('DELETE FROM clans WHERE id=?',(clan['id'],))
        else: con.execute('DELETE FROM clan_members WHERE user_id=?',(u['id'],))
        return {'ok':True}


@app.post('/api/clans/transfer/{user_id}')
def clan_transfer(user_id:int,request:Request):
    with db(True) as con:
        u=user_for(request,con);clan=my_clan(con,u['id'])
        if not clan or clan['my_role']!='LEADER': fail(403,'Только лидер может передать лидерство')
        if user_id==u['id']: fail(400,'Вы уже лидер')
        member=record(con,'SELECT * FROM clan_members WHERE user_id=? AND clan_id=?',(user_id,clan['id']))
        if not member: fail(404,'Этот пользователь не состоит в клане')
        con.execute('UPDATE clans SET leader_id=? WHERE id=?',(user_id,clan['id']))
        con.execute("UPDATE clan_members SET role='MEMBER' WHERE user_id=?",(u['id'],))
        con.execute("UPDATE clan_members SET role='LEADER' WHERE user_id=?",(user_id,))
        return {'ok':True}


@app.get('/api/clans/chat')
def clan_chat(request:Request,after:int=0):
    with db() as con:
        u=user_for(request,con);clan=my_clan(con,u['id'])
        if not clan: fail(403,'Чат доступен только участникам клана')
        if after>0:
            messages=rows(con,'''SELECT m.id,m.body,m.created_at,u.nickname,m.sender_id FROM clan_messages m JOIN users u ON u.id=m.sender_id
                     WHERE m.clan_id=? AND m.id>? AND m.deleted=0 AND m.sender_id NOT IN (SELECT blocked_id FROM blocked_users WHERE blocker_id=?) ORDER BY m.id ASC LIMIT 100''',(clan['id'],after,u['id']))
        else:
            messages=rows(con,'''SELECT m.id,m.body,m.created_at,u.nickname,m.sender_id FROM clan_messages m JOIN users u ON u.id=m.sender_id
                     WHERE m.clan_id=? AND m.deleted=0 AND m.sender_id NOT IN (SELECT blocked_id FROM blocked_users WHERE blocker_id=?) ORDER BY m.id DESC LIMIT 100''',(clan['id'],u['id']))
            messages.reverse()
        return messages


@app.post('/api/clans/chat')
def clan_send(form:Message,request:Request):
    body=clean(form.body)
    if not 1<=len(body)<=600: fail(400,'Сообщение: 1–600 символов')
    with db(True) as con:
        u=user_for(request,con);clan=my_clan(con,u['id'])
        if not clan: fail(403,'Чат доступен только участникам клана')
        # Maximum of 8 messages per user in any 10-second window.
        cnt=record(con,'SELECT COUNT(*) as n FROM clan_messages WHERE sender_id=? AND created_at>=?',(u['id'],now()-10))
        if cnt['n']>=8: fail(429,'Слишком много сообщений')
        id=con.execute('INSERT INTO clan_messages(clan_id,sender_id,body,created_at) VALUES(?,?,?,?)',
                       (clan['id'],u['id'],body,now())).lastrowid
        return {'id':id,'body':body,'nickname':u['nickname'],'sender_id':u['id'],'created_at':now()}


def lobby_for(con,uid):
    return record(con,'''SELECT l.* FROM lobby_members lm JOIN lobbies l ON l.id=lm.lobby_id WHERE lm.user_id=?''',(uid,))


def lobby_view(con,uid):
    lobby=lobby_for(con,uid)
    invites=rows(con,'''SELECT i.id,i.lobby_id,u.nickname AS inviter_nick,i.created_at FROM lobby_invites i JOIN users u ON u.id=i.inviter_id WHERE i.invitee_id=? AND i.status='PENDING' ORDER BY i.id DESC''',(uid,))
    if not lobby: return {'lobby':None,'members':[],'invites':invites}
    members=rows(con,'''SELECT u.id,u.nickname,u.elo,u.wins,u.losses FROM lobby_members lm JOIN users u ON u.id=lm.user_id WHERE lm.lobby_id=? ORDER BY lm.user_id''',(lobby['id'],))
    for person in members:
        rating=mode_rating(con,person['id'],lobby['mode'])
        person.update({'elo':rating['elo'],'wins':rating['wins'],'losses':rating['losses'], 'rank':rating['rank']})
    return {'lobby':lobby,'members':members,'invites':invites}


@app.get('/api/lobby')
def get_lobby(request:Request):
    with db() as con: return lobby_view(con,user_for(request,con)['id'])


@app.post('/api/lobby')
def make_lobby(request:Request):
    with db(True) as con:
        u=user_for(request,con)
        if lobby_for(con,u['id']): return lobby_view(con,u['id'])
        id=con.execute("INSERT INTO lobbies(leader_id,created_at,mode) VALUES(?,?, '5v5')",(u['id'],now())).lastrowid
        con.execute('INSERT INTO lobby_members(lobby_id,user_id) VALUES(?,?)',(id,u['id']))
        return lobby_view(con,u['id'])


@app.post('/api/lobby/mode')
def lobby_change_mode(form:LobbyMode,request:Request):
    mode_size(form.mode)
    with db(True) as con:
        u=user_for(request,con)
        lobby=lobby_for(con,u['id'])
        if not lobby or lobby['leader_id']!=u['id'] or lobby['status']!='OPEN':
            fail(403,'Сменить режим может только лидер открытого лобби')
        count=record(con,'SELECT COUNT(*) AS n FROM lobby_members WHERE lobby_id=?',(lobby['id'],))['n']
        if count > mode_size(form.mode): fail(409,'Слишком много участников для выбранного режима')
        con.execute('UPDATE lobbies SET mode=? WHERE id=?',(form.mode,lobby['id']))
        # Old invites must not slip into a lobby with a newly reduced capacity.
        con.execute("UPDATE lobby_invites SET status='DECLINED' WHERE lobby_id=? AND status='PENDING'",(lobby['id'],))
        return lobby_view(con,u['id'])


@app.post('/api/lobby/invite')
def lobby_invite(form:Invite,request:Request):
    with db(True) as con:
        u=user_for(request,con);lobby=lobby_for(con,u['id'])
        if not lobby or lobby['leader_id']!=u['id'] or lobby['status']!='OPEN': fail(403,'Приглашать может лидер открытого лобби')
        cnt=record(con,'SELECT count(*) as n FROM lobby_members WHERE lobby_id=?',(lobby['id'],))['n']
        if cnt>=mode_size(lobby['mode']): fail(409,'Лобби заполнено')
        target=record(con,'SELECT id FROM users WHERE nickname=? COLLATE NOCASE',(clean(form.nickname),))
        if not target or target['id']==u['id']: fail(404,'Игрок не найден')
        if lobby_for(con,target['id']): fail(409,'Игрок уже находится в другом лобби')
        old=record(con,'SELECT id FROM lobby_invites WHERE lobby_id=? AND invitee_id=?',(lobby['id'],target['id']))
        if old: con.execute("UPDATE lobby_invites SET status='PENDING',created_at=? WHERE id=?",(now(),old['id']))
        else: con.execute('INSERT INTO lobby_invites(lobby_id,invitee_id,inviter_id,created_at) VALUES(?,?,?,?)',
                          (lobby['id'],target['id'],u['id'],now()))
        return {'ok':True}


@app.post('/api/lobby/invites/{invite_id}/{decision}')
def lobby_answer(invite_id:int,decision:str,request:Request):
    if decision not in ('accept','decline'): fail(404,'Неизвестное действие')
    with db(True) as con:
        u=user_for(request,con)
        inv=record(con,"SELECT * FROM lobby_invites WHERE id=? AND status='PENDING' AND invitee_id=?",(invite_id,u['id']))
        if not inv: fail(404,'Приглашение недействительно')
        if decision=='accept':
            if lobby_for(con,u['id']): fail(409,'Сначала покиньте другое лобби')
            lobby=record(con,'SELECT * FROM lobbies WHERE id=?',(inv['lobby_id'],))
            if not lobby or lobby['status']!='OPEN': fail(409,'Лобби уже недоступно')
            if record(con,'SELECT count(*) as n FROM lobby_members WHERE lobby_id=?',(lobby['id'],))['n']>=mode_size(lobby['mode']): fail(409,'Лобби заполнено')
            con.execute('INSERT INTO lobby_members(lobby_id,user_id) VALUES(?,?)',(lobby['id'],u['id']))
        con.execute('UPDATE lobby_invites SET status=? WHERE id=?',('ACCEPTED' if decision=='accept' else 'DECLINED',invite_id))
        return {'ok':True}


@app.post('/api/lobby/leave')
def lobby_leave(request:Request):
    with db(True) as con:
        u=user_for(request,con);lobby=lobby_for(con,u['id'])
        if not lobby: fail(404,'Нет лобби')
        if lobby['status']=='SEARCHING': fail(409,'Сначала отмените поиск')
        if lobby['leader_id']==u['id']: con.execute('DELETE FROM lobbies WHERE id=?',(lobby['id'],))
        else: con.execute('DELETE FROM lobby_members WHERE user_id=?',(u['id'],))
        return {'ok':True}


def ready_lobbies(con,mode):
    ls=rows(con,"SELECT id FROM lobbies WHERE status='SEARCHING' AND mode=? ORDER BY created_at,id LIMIT 50",(mode,))
    return [(row['id'], rows(con,'SELECT user_id FROM lobby_members WHERE lobby_id=?',(row['id'],))) for row in ls]


def match_queued(con,mode='5v5'):
    # Mode-specific queues. Keep premade parties together, with exactly 1/2/5 per side.
    side_size=mode_size(mode)
    entries=ready_lobbies(con,mode)
    def find_group(start,need,excluded):
        if need==0: return []
        for pos in range(start,len(entries)):
            lid,people=entries[pos]
            if lid in excluded or len(people)>need: continue
            rest=find_group(pos+1,need-len(people),excluded|{lid})
            if rest is not None: return [pos]+rest
        return None
    a=find_group(0,side_size,set())
    if a is None:return None
    b=find_group(0,side_size,{entries[i][0] for i in a})
    if b is None:return None
    mid=con.execute("INSERT INTO matches(status,created_at,ready_deadline,mode) VALUES('READY_CHECK',?,?,?)",
                    (now(),now()+READY_TIMEOUT_SECONDS,mode)).lastrowid
    for side,indices in ((0,a),(1,b)):
        captain_set=False
        for i in indices:
            lid,people=entries[i]
            leader=record(con,'SELECT leader_id FROM lobbies WHERE id=?',(lid,))['leader_id']
            for member in people:
                uid=member['user_id'];is_captain=int(not captain_set and uid==leader)
                con.execute('INSERT INTO match_members(match_id,user_id,side,captain) VALUES(?,?,?,?)',
                            (mid,uid,side,is_captain))
                if is_captain:
                    captain_set=True
                    if side==0: con.execute('UPDATE matches SET host_user_id=? WHERE id=?',(uid,mid))
            con.execute("UPDATE lobbies SET status='MATCHED' WHERE id=?",(lid,))
    return mid


def cancel_found_match(con,match_id,reason,cancelled_by=None):
    m=record(con,'SELECT * FROM matches WHERE id=?',(match_id,))
    if not m or m['status'] not in ('READY_CHECK','PENDING','REVIEW','DISPUTED'):
        fail(409,'Матч уже завершён или отменён')
    con.execute("UPDATE matches SET status='CANCELLED',cancel_reason=?,cancelled_by=? WHERE id=?",
                (reason,cancelled_by,match_id))
    # Release all matched parties without deleting their membership.
    lids=rows(con,'''SELECT DISTINCT lm.lobby_id FROM lobby_members lm
       JOIN match_members mm ON mm.user_id=lm.user_id WHERE mm.match_id=?''',(match_id,))
    for l in lids:con.execute("UPDATE lobbies SET status='OPEN' WHERE id=? AND status='MATCHED'",(l['lobby_id'],))


def expire_ready_checks(con):
    ids=rows(con,"SELECT id FROM matches WHERE status='READY_CHECK' AND ready_deadline<=?",(now(),))
    for m in ids:cancel_found_match(con,m['id'],'Время подтверждения готовности истекло')

@app.post('/api/lobby/search')
def lobby_search(request:Request):
    with db(True) as con:
        u=user_for(request,con);lobby=lobby_for(con,u['id'])
        if not lobby or lobby['leader_id']!=u['id']: fail(403,'Начать поиск может только лидер лобби')
        if lobby['status'] not in ('OPEN','SEARCHING'): fail(409,'Это лобби уже участвовало в матче')
        nxt='OPEN' if lobby['status']=='SEARCHING' else 'SEARCHING'
        con.execute('UPDATE lobbies SET status=? WHERE id=?',(nxt,lobby['id']))
        if nxt=='SEARCHING': match_queued(con,lobby['mode'])
        return lobby_view(con,u['id'])


@app.get('/api/matches')
def matches(request:Request):
    with db(True) as con:
        u=user_for(request,con)
        expire_ready_checks(con)
        mine=rows(con,'''SELECT m.*,mm.side,mm.captain,mm.ready AS my_ready FROM matches m JOIN match_members mm ON mm.match_id=m.id WHERE mm.user_id=? ORDER BY m.id DESC LIMIT 30''',(u['id'],))
        for m in mine:
            m['required_ready']=mode_size(m['mode'] or '5v5')*2
            m['players']=rows(con,'''SELECT mm.user_id AS id,mm.side,mm.captain,mm.ready,u.nickname,u.elo,u.avatar_updated_at
                FROM match_members mm JOIN users u ON mm.user_id=u.id WHERE mm.match_id=? ORDER BY side,nickname''',(m['id'],))
            m['ready_count']=sum(int(p['ready']) for p in m['players'])
            m['host']=record(con,'SELECT nickname,mlbb_id,server_id FROM users WHERE id=?',(m['host_user_id'],)) if m['host_user_id'] else None
            m['evidence']=rows(con,'SELECT side,captain_id,winning_side,submitted_at FROM match_evidence WHERE match_id=? ORDER BY side',(m['id'],))
            m['kda']=rows(con,'SELECT user_id,kills,deaths,assists FROM match_player_stats WHERE match_id=? ORDER BY user_id',(m['id'],))
            m['review']=record(con,'SELECT winning_side,note,reviewed_at FROM match_decisions WHERE match_id=?',(m['id'],))
        return mine


@app.post('/api/matches/{match_id}/ready')
def match_ready(match_id:int,form:ReadyResponse,request:Request):
    with db(True) as con:
        u=user_for(request,con)
        expire_ready_checks(con)
        m=record(con,'SELECT * FROM matches WHERE id=?',(match_id,))
        if not m:fail(404,'Матч не найден')
        player=record(con,'SELECT user_id,ready FROM match_members WHERE match_id=? AND user_id=?',(match_id,u['id']))
        if not player:fail(403,'Вы не участник этого матча')
        if m['status']!='READY_CHECK':fail(409,'Подтверждение готовности уже закрыто')
        if not form.ready:
            cancel_found_match(con,match_id,'Один из игроков отказался от матча',u['id'])
            return {'status':'CANCELLED','ready_count':0}
        con.execute('UPDATE match_members SET ready=1 WHERE match_id=? AND user_id=?',(match_id,u['id']))
        ready_count=record(con,'SELECT COUNT(*) AS n FROM match_members WHERE match_id=? AND ready=1',(match_id,))['n']
        total=mode_size(m['mode'])*2
        if ready_count==total:
            con.execute("UPDATE matches SET status='PENDING' WHERE id=?",(match_id,))
        return {'status':'PENDING' if ready_count==total else 'READY_CHECK','ready_count':ready_count,'total':total}

# The old captain text-report endpoint must not settle any match.
@app.post('/api/matches/{match_id}/report')
def legacy_report(match_id:int,form:MatchReport,request:Request):
    with db() as con: user_for(request,con)
    fail(409,'Для результата нужен скриншот. Используйте загрузку доказательства.')


def validated_match_image(value):
    if len(value)>1_400_000 or not re.fullmatch(r'data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+',value):
        fail(400,'Нужен скриншот JPG, PNG или WebP до 1 МБ')
    try:
        raw=base64.b64decode(value.split(',',1)[1],validate=True)
        if len(raw)>1_000_000:fail(413,'Скриншот слишком большой')
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ('JPEG','PNG','WEBP') or source.width<200 or source.height<120 or source.width*source.height>12_000_000:
                fail(400,'Некорректный размер скриншота')
            im=ImageOps.exif_transpose(source)
            im.thumbnail((1920,1920))
            buf=io.BytesIO(); im.convert('RGB').save(buf,format='JPEG',quality=83,optimize=True)
            return buf.getvalue()
    except (binascii.Error,ValueError,UnidentifiedImageError,OSError):
        fail(400,'Файл изображения повреждён')


@app.post('/api/matches/{match_id}/evidence')
def captain_evidence(match_id:int,form:MatchEvidenceInput,request:Request):
    if form.winning_side not in (0,1):fail(400,'Некорректная победившая сторона')
    # Decode outside the write transaction to limit SQLite lock time.
    picture=validated_match_image(form.image_data)
    with db(True) as con:
        u=user_for(request,con)
        m=record(con,'SELECT status FROM matches WHERE id=?',(match_id,))
        if not m:fail(404,'Матч не найден')
        c=record(con,'SELECT side FROM match_members WHERE match_id=? AND user_id=? AND captain=1',(match_id,u['id']))
        if not c:fail(403,'Доказательство загружает только капитан команды')
        if m['status'] not in ('PENDING','REVIEW','DISPUTED'): fail(409,'Отчёты по этому матчу закрыты')
        con.execute('''INSERT INTO match_evidence(match_id,side,captain_id,winning_side,image,submitted_at)
              VALUES(?,?,?,?,?,?) ON CONFLICT(match_id,side) DO UPDATE SET
              captain_id=excluded.captain_id,winning_side=excluded.winning_side,
              image=excluded.image,submitted_at=excluded.submitted_at''',
              (match_id,c['side'],u['id'],form.winning_side,picture,now()))
        reports=rows(con,'SELECT side,winning_side FROM match_evidence WHERE match_id=? ORDER BY side',(match_id,))
        new_status='PENDING' if len(reports)<2 else ('REVIEW' if reports[0]['winning_side']==reports[1]['winning_side'] else 'DISPUTED')
        con.execute('UPDATE matches SET status=?,report_a=?,report_b=? WHERE id=?',
                   (new_status,next((x['winning_side'] for x in reports if x['side']==0),None),
                    next((x['winning_side'] for x in reports if x['side']==1),None),match_id))
        return {'status':new_status,'submitted':len(reports),'required':2}


@app.get('/api/matches/{match_id}/evidence/{side}')
def read_evidence(match_id:int,side:int,request:Request):
    if side not in (0,1): fail(404,'Изображение не найдено')
    with db() as con:
        u=user_for(request,con)
        if not (u['is_admin'] or u['is_moderator'] or record(con,'SELECT 1 FROM match_members WHERE match_id=? AND user_id=?',(match_id,u['id']))):
            fail(403,'Доказательство доступно только участникам и администрации')
        item=record(con,'SELECT image FROM match_evidence WHERE match_id=? AND side=?',(match_id,side))
        if not item:fail(404,'Скриншот ещё не загружен')
        return Response(content=item['image'],media_type='image/jpeg',headers={'Cache-Control':'private,no-store'})


def finalize_match(con,match_id,winning_side):
    current=record(con,'SELECT status,mode FROM matches WHERE id=?',(match_id,))
    if current['status'] not in ('REVIEW','DISPUTED'): fail(409,'Матч не ожидает проверки')
    for p in rows(con,'SELECT user_id,side FROM match_members WHERE match_id=?',(match_id,)):
        win=p['side']==winning_side
        delta=25 if win else -25
        if current['mode']=='5v5':
            con.execute('UPDATE users SET elo=MAX(0,elo+?),wins=wins+?,losses=losses+? WHERE id=?',
                        (delta,int(win),int(not win),p['user_id']))
        else:
            con.execute('''INSERT INTO mode_ratings(user_id,mode,elo,wins,losses) VALUES(?,?,?,?,?)
                      ON CONFLICT(user_id,mode) DO UPDATE SET
                      elo=MAX(0,elo+excluded.elo-1000),wins=wins+excluded.wins,losses=losses+excluded.losses''',
                      (p['user_id'],current['mode'],1000+delta,int(win),int(not win)))
    con.execute("UPDATE matches SET status='COMPLETED',winning_side=? WHERE id=?",(winning_side,match_id))
    # All prior party lobbies release membership for a new session.
    lids=rows(con,'SELECT DISTINCT lobby_id FROM lobby_members WHERE user_id IN (SELECT user_id FROM match_members WHERE match_id=?)',(match_id,))
    for l in lids: con.execute('DELETE FROM lobbies WHERE id=?',(l['lobby_id'],))


@app.get('/api/leaderboard')
def leaderboard(request:Request,mode:str='5v5'):
    mode_size(mode)
    with db() as con:
        user_for(request,con)
        if mode=='5v5':
            out=rows(con,"""SELECT id,nickname,elo,wins,losses,avatar_updated_at,staff_role
                        FROM users WHERE nickname IS NOT NULL AND is_banned=0
                        ORDER BY (wins+losses>=10) DESC,elo DESC,wins DESC LIMIT 100""")
        else:
            out=rows(con,"""SELECT u.id,u.nickname,COALESCE(r.elo,1000) AS elo,
                        COALESCE(r.wins,0) AS wins,COALESCE(r.losses,0) AS losses,
                        u.avatar_updated_at,u.staff_role FROM users u
                        LEFT JOIN mode_ratings r ON r.user_id=u.id AND r.mode=?
                        WHERE u.nickname IS NOT NULL AND u.is_banned=0
                        ORDER BY ((COALESCE(r.wins,0)+COALESCE(r.losses,0))>=10) DESC,
                        COALESCE(r.elo,1000) DESC,COALESCE(r.wins,0) DESC LIMIT 100""",(mode,))
        for u in out:
            u['mode']=mode
            u['rank']=rank(u['elo'],u['wins']+u['losses'])
            u['kda']=total_kda(con,u['id'])
            u['kd']=kd_ratio(u['kda'])
        return out


# Store local calendar times with an explicit UTC offset (organizer-selected).
# All comparisons are performed using timezone-aware datetime values.
def tournament_time(value:str, offset:str):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', value):
        fail(400,'Укажите дату и время в формате ГГГГ-ММ-ДДTЧЧ:ММ')
    if not re.fullmatch(r'[+-]\d{2}:\d{2}',offset):
        fail(400,'Некорректный часовой пояс')
    sign=1 if offset[0]=='+' else -1
    hours,minutes=map(int,offset[1:].split(':'))
    total=sign*(hours*60+minutes)
    if total < -12*60 or total > 14*60 or minutes not in (0,30,45):
        fail(400,'Некорректный часовой пояс')
    try:
        parsed=datetime.strptime(value,'%Y-%m-%dT%H:%M')
        return parsed.replace(tzinfo=timezone(timedelta(minutes=total)))
    except ValueError: fail(400,'Некорректные дата или время')


def tournament_registration_open(t):
    if t['status']!='OPEN': return False
    offset=t.get('time_zone') or '+05:00'
    limit=t.get('registration_closes_at') or t['starts_at']
    return datetime.now(timezone.utc)<tournament_time(limit,offset).astimezone(timezone.utc) and datetime.now(timezone.utc)<tournament_time(t['starts_at'],offset).astimezone(timezone.utc)


def validate_tournament(form,participants=0):
    title=clean(form.title);rules=clean(form.rules)
    if not 4<=len(title)<=70 or not 5<=len(rules)<=2000: fail(400,'Проверьте название и правила')
    if form.mode not in ('solo','team'): fail(400,'Некорректный формат')
    if form.slots not in (4,8,16,32,64): fail(400,'Места: 4, 8, 16, 32 или 64')
    if form.slots<participants: fail(409,'Число мест меньше уже зарегистрированных участников')
    if not 0<=form.prize_kzt<=1_000_000_000: fail(400,'Некорректная сумма призового фонда')
    offset=form.time_zone
    begins=tournament_time(clean(form.starts_at),offset)
    if begins<=datetime.now(timezone.utc): fail(400,'Дата и время начала должны быть в будущем')
    registration=clean(form.registration_closes_at)
    if registration:
        closes=tournament_time(registration,offset)
        if closes>begins: fail(400,'Набор должен закрываться до начала турнира')
    return {'title':title,'mode':form.mode,'slots':form.slots,'prize_kzt':form.prize_kzt,
            'starts_at':clean(form.starts_at),'rules':rules,
            'registration_closes_at':registration,'time_zone':offset}


def tournament_can_manage(t,u):
    return t['creator_id']==u['id'] or bool(u['is_admin'])


@app.get('/api/tournaments')
def tournaments(request:Request):
    with db() as con:
        u=user_for(request,con)
        out=rows(con,'''SELECT t.*,u.nickname AS organizer,(SELECT COUNT(*) FROM tournament_entries e WHERE e.tournament_id=t.id) AS participants
           FROM tournaments t JOIN users u ON u.id=t.creator_id ORDER BY t.official DESC,t.starts_at ASC LIMIT 150''')
        for t in out:
            t['joined']=bool(record(con,'SELECT 1 FROM tournament_entries WHERE user_id=? AND tournament_id=?',(u['id'],t['id'])))
            t['can_manage']=tournament_can_manage(t,u)
            t['registration_open']=tournament_registration_open(t) and t['participants']<t['slots']
        return out


def new_tournament(con,u,form,official):
    fields=validate_tournament(form)
    id=con.execute('''INSERT INTO tournaments(creator_id,title,mode,slots,prize_kzt,starts_at,rules,official,created_at,registration_closes_at,time_zone)
       VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(u['id'],fields['title'],fields['mode'],fields['slots'],fields['prize_kzt'],fields['starts_at'],fields['rules'],int(official),now(),fields['registration_closes_at'],fields['time_zone'])).lastrowid
    return {'id':id,'official':official}


@app.post('/api/tournaments')
def tournament_create(form:TournamentInput,request:Request):
    with db(True) as con: return new_tournament(con,user_for(request,con),form,False)


@app.patch('/api/tournaments/{tour_id}')
def tournament_edit(tour_id:int,form:TournamentInput,request:Request):
    with db(True) as con:
        u=user_for(request,con);t=record(con,'SELECT * FROM tournaments WHERE id=?',(tour_id,))
        if not t: fail(404,'Турнир не найден')
        if not tournament_can_manage(t,u): fail(403,'Только организатор или разработчик может менять турнир')
        if tournament_time(t['starts_at'],t['time_zone'])<=datetime.now(timezone.utc): fail(409,'Нельзя редактировать начавшийся турнир')
        participants=record(con,'SELECT COUNT(*) AS n FROM tournament_entries WHERE tournament_id=?',(tour_id,))['n']
        data=validate_tournament(form,participants)
        # Existing participants must not be silently switched to another format.
        if participants and data['mode']!=t['mode']: fail(409,'Нельзя менять формат после начала регистрации участников')
        con.execute('''UPDATE tournaments SET title=?,mode=?,slots=?,prize_kzt=?,starts_at=?,rules=?,registration_closes_at=?,time_zone=? WHERE id=?''',
                    (data['title'],data['mode'],data['slots'],data['prize_kzt'],data['starts_at'],data['rules'],data['registration_closes_at'],data['time_zone'],tour_id))
        return {'ok':True}


@app.post('/api/tournaments/{tour_id}/registration')
def tournament_registration(tour_id:int,form:RegistrationState,request:Request):
    with db(True) as con:
        u=user_for(request,con);t=record(con,'SELECT * FROM tournaments WHERE id=?',(tour_id,))
        if not t: fail(404,'Турнир не найден')
        if not tournament_can_manage(t,u): fail(403,'Только организатор или разработчик управляет набором')
        if form.open and not tournament_registration_open({**t,'status':'OPEN'}):
            fail(409,'Время набора уже истекло — измените время в настройках')
        con.execute('UPDATE tournaments SET status=? WHERE id=?',('OPEN' if form.open else 'CLOSED',tour_id))
        return {'ok':True,'status':'OPEN' if form.open else 'CLOSED'}


@app.post('/api/tournaments/{tour_id}/join')
def tournament_join(tour_id:int,request:Request):
    with db(True) as con:
        u=user_for(request,con);t=record(con,'SELECT * FROM tournaments WHERE id=?',(tour_id,))
        if not t: fail(404,'Турнир не найден')
        if not tournament_registration_open(t): fail(409,'Регистрация на турнир закрыта')
        if t['mode']=='team' and not my_clan(con,u['id']): fail(409,'Для командного турнира необходимо состоять в клане')
        if record(con,'SELECT 1 FROM tournament_entries WHERE tournament_id=? AND user_id=?',(tour_id,u['id'])): fail(409,'Вы уже зарегистрированы')
        if record(con,'SELECT count(*) as n FROM tournament_entries WHERE tournament_id=?',(tour_id,))['n']>=t['slots']: fail(409,'Места закончились')
        con.execute('INSERT INTO tournament_entries(tournament_id,user_id,joined_at) VALUES(?,?,?)',(tour_id,u['id'],now()))
        return {'ok':True}


@app.post('/api/tournaments/{tour_id}/withdraw')
def tournament_withdraw(tour_id:int,request:Request):
    with db(True) as con:
        u=user_for(request,con);t=record(con,'SELECT * FROM tournaments WHERE id=?',(tour_id,))
        if not t: fail(404,'Турнир не найден')
        if tournament_time(t['starts_at'],t['time_zone'])<=datetime.now(timezone.utc): fail(409,'Турнир уже начался')
        con.execute('DELETE FROM tournament_entries WHERE tournament_id=? AND user_id=?',(tour_id,u['id']))
        return {'ok':True}


@app.get('/api/announcements')
def news(request:Request):
    with db() as con:
        user_for(request,con)
        return rows(con,'SELECT a.id,a.title,a.body,a.created_at,u.nickname AS author FROM announcements a JOIN users u ON u.id=a.author_id ORDER BY a.id DESC LIMIT 20')


@app.get('/api/admin/overview')
def admin_overview(request:Request):
    with db() as con:
        admin_for(request,con)
        stats={x:record(con,f'SELECT COUNT(*) AS count FROM {x}')['count'] for x in ('users','clans','clan_messages','tournaments','matches')}
        users=rows(con,'SELECT id,email,nickname,elo,is_admin,is_moderator,staff_role,is_banned FROM users ORDER BY id DESC LIMIT 100')
        clans=rows(con,'SELECT c.id,c.name,c.tag,c.description,c.leader_id,u.nickname AS leader FROM clans c JOIN users u ON u.id=c.leader_id ORDER BY c.id DESC LIMIT 100')
        return {'stats':stats,'users':users,'clans':clans}


@app.post('/api/admin/tournaments')
def official_tournament(form:TournamentInput,request:Request):
    with db(True) as con: return new_tournament(con,admin_for(request,con),form,True)


@app.post('/api/admin/announcements')
def admin_announcement(form:AnnouncementInput,request:Request):
    title=clean(form.title);body=clean(form.body)
    if not 3<=len(title)<=100 or not 3<=len(body)<=2000: fail(400,'Проверьте текст новости')
    with db(True) as con:
        u=admin_for(request,con)
        id=con.execute('INSERT INTO announcements(author_id,title,body,created_at) VALUES(?,?,?,?)',(u['id'],title,body,now())).lastrowid
        return {'id':id}


@app.post('/api/admin/users/{user_id}/ban')
def admin_ban(user_id:int,request:Request):
    with db(True) as con:
        admin=admin_for(request,con)
        target=record(con,'SELECT id,is_admin,is_banned,staff_role FROM users WHERE id=?',(user_id,))
        if not target: fail(404,'Игрок не найден')
        if target['staff_role'] in ('OWNER','DEVELOPER','ADMIN'): fail(403,'Нельзя блокировать администрацию этим действием')
        con.execute('UPDATE users SET is_banned=? WHERE id=?',(int(not target['is_banned']),user_id))
        con.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
        return {'banned':not bool(target['is_banned'])}


@app.post('/api/admin/clans/{clan_id}/delete')
def admin_delete_clan(clan_id:int,request:Request):
    with db(True) as con:
        admin_for(request,con)
        c=con.execute('DELETE FROM clans WHERE id=?',(clan_id,))
        if not c.rowcount: fail(404,'Клан не найден')
        return {'ok':True}


STAFF_ROLES = ('PLAYER','MODERATOR','ADMIN','DEVELOPER','OWNER')


def change_role(con, actor, user_id, new_role):
    if new_role not in STAFF_ROLES or new_role == 'OWNER':
        fail(400, 'Недопустимая роль')
    actor_role = actor['staff_role']
    if actor_role not in ('OWNER','DEVELOPER'):
        fail(403,'Назначение ролей разрешено только владельцу и разработчику')
    target = record(con,'SELECT id,staff_role FROM users WHERE id=?',(user_id,))
    if not target: fail(404,'Пользователь не найден')
    if user_id == actor['id'] or target['staff_role'] == 'OWNER':
        fail(403,'Нельзя изменять права владельца или собственную роль')
    if actor_role == 'DEVELOPER' and (new_role == 'DEVELOPER' or target['staff_role'] == 'DEVELOPER'):
        fail(403,'Только владелец может назначать и снимать права разработчиков')
    old_role = target['staff_role']
    if old_role == new_role: return {'ok':True,'role':new_role}
    con.execute('UPDATE users SET staff_role=?, is_admin=?, is_moderator=? WHERE id=?',
                (new_role,int(new_role in ('OWNER','DEVELOPER','ADMIN')),
                 int(new_role in ('OWNER','DEVELOPER','ADMIN','MODERATOR')),user_id))
    con.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
    con.execute('INSERT INTO role_audit(actor_id,target_id,previous_role,new_role,changed_at) VALUES(?,?,?,?,?)',
                (actor['id'],user_id,old_role,new_role,now()))
    return {'ok':True,'role':new_role}


@app.post('/api/admin/users/{user_id}/role')
def set_staff_role(user_id:int,form:RoleChange,request:Request):
    with db(True) as con:
        return change_role(con, user_for(request,con), user_id, form.role)


@app.post('/api/admin/users/{user_id}/moderator')
def set_moderator(user_id:int,form:ModeratorToggle,request:Request):
    with db(True) as con:
        return change_role(con, user_for(request,con), user_id, 'MODERATOR' if form.enabled else 'PLAYER')


@app.get('/api/admin/roles/audit')
def roles_audit(request:Request):
    with db() as con:
        u=user_for(request,con)
        if u['staff_role'] not in ('OWNER','DEVELOPER'):fail(403,'Недостаточно прав')
        return rows(con,"SELECT id,actor_id,target_id,previous_role,new_role,changed_at FROM role_audit ORDER BY id DESC LIMIT 100")


@app.post('/api/admin/matches/{match_id}/resolve')
def admin_resolve(match_id:int,form:MatchDecision,request:Request):
    if form.winning_side not in (0,1): fail(400,'Некорректная сторона')
    if len(form.note.strip())>500:fail(400,'Примечание слишком длинное')
    with db(True) as con:
        staff=staff_for(request,con)
        m=record(con,'SELECT status FROM matches WHERE id=?',(match_id,))
        if not m or m['status'] not in ('REVIEW','DISPUTED'): fail(409,'Матч ещё не готов к проверке или уже закрыт')
        if record(con,'SELECT count(*) AS n FROM match_evidence WHERE match_id=?',(match_id,))['n']!=2:
            fail(409,'Нужны скриншоты обоих капитанов')
        # One decision settles the two sides in a single atomic transaction.
        con.execute('INSERT INTO match_decisions(match_id,reviewer_id,winning_side,note,reviewed_at) VALUES(?,?,?,?,?)',
                   (match_id,staff['id'],form.winning_side,form.note.strip(),now()))
        finalize_match(con,match_id,form.winning_side)
        return {'ok':True,'status':'COMPLETED','winner':form.winning_side,'loser':1-form.winning_side}


@app.put('/api/admin/matches/{match_id}/players/{user_id}/kda')
def staff_player_kda(match_id:int,user_id:int,form:PlayerKDA,request:Request):
    with db(True) as con:
        staff=staff_for(request,con)
        match=record(con,'SELECT status FROM matches WHERE id=?',(match_id,))
        if not match:fail(404,'Матч не найден')
        if match['status'] not in ('PENDING','REVIEW','DISPUTED','COMPLETED'):fail(409,'Матч не проходил: статистика недоступна')
        if not record(con,'SELECT 1 FROM match_members WHERE match_id=? AND user_id=?',(match_id,user_id)):
            fail(404,'Игрок не участвовал в этом матче')
        before=record(con,'SELECT kills,deaths,assists FROM match_player_stats WHERE match_id=? AND user_id=?',(match_id,user_id))
        new={'kills':form.kills,'deaths':form.deaths,'assists':form.assists}
        con.execute('''INSERT INTO match_player_stats(match_id,user_id,kills,deaths,assists,updated_by,updated_at)
                     VALUES(?,?,?,?,?,?,?) ON CONFLICT(match_id,user_id) DO UPDATE SET kills=excluded.kills,
                     deaths=excluded.deaths,assists=excluded.assists,updated_by=excluded.updated_by,updated_at=excluded.updated_at''',
                    (match_id,user_id,form.kills,form.deaths,form.assists,staff['id'],now()))
        con.execute('INSERT INTO match_stat_audit(match_id,user_id,editor_id,before_json,after_json,changed_at) VALUES(?,?,?,?,?,?)',
                   (match_id,user_id,staff['id'],json.dumps(before),json.dumps(new),now()))
        return {'ok':True,'player_id':user_id,**new}


@app.post('/api/admin/matches/{match_id}/cancel')
def admin_cancel_match(match_id:int,form:MatchCancel,request:Request):
    reason=clean(form.reason)
    if not 5<=len(reason)<=300:fail(400,'Укажите причину отмены от 5 до 300 символов')
    with db(True) as con:
        staff=staff_for(request,con)
        cancel_found_match(con,match_id,reason,staff['id'])
    return {'ok':True,'status':'CANCELLED'}


@app.get('/api/admin/matches')
def admin_matches(request:Request):
    with db(True) as con:
        staff_for(request,con)
        expire_ready_checks(con)
        data=rows(con,'SELECT id,mode,status,created_at,ready_deadline,host_user_id,cancel_reason,winning_side FROM matches ORDER BY id DESC LIMIT 100')
        for m in data:
            m['players']=rows(con,'''SELECT mm.user_id AS id,mm.side,mm.captain,u.nickname,
                       s.kills,s.deaths,s.assists FROM match_members mm JOIN users u ON u.id=mm.user_id
                       LEFT JOIN match_player_stats s ON s.match_id=mm.match_id AND s.user_id=mm.user_id
                       WHERE mm.match_id=? ORDER BY mm.side,mm.captain DESC,u.nickname''',(m['id'],))
            m['evidence']=rows(con,'SELECT side,winning_side,submitted_at FROM match_evidence WHERE match_id=? ORDER BY side',(m['id'],))
            m['review']=record(con,'SELECT winning_side,note,reviewed_at FROM match_decisions WHERE match_id=?',(m['id'],))
        return data

@app.get('/api/fund')
def fund_info(request:Request):
    with db() as con:
        user_for(request,con)
        x=record(con,'SELECT headline,description,goal_kzt,updated_at FROM project_fund WHERE id=1')
        settings=record(con,'SELECT checkout_url,note,updated_at FROM sponsor_config WHERE id=1')
        x.update({'payments_enabled':bool(settings['checkout_url']),
                  'sponsor_url':settings['checkout_url'],
                  'sponsor_note':settings['note'],
                  'collected_kzt':None})
        return x

@app.put('/api/admin/fund')
def admin_fund(form:FundUpdate,request:Request):
    headline=clean(form.headline);description=clean(form.description)
    if not 5<=len(headline)<=90 or not 10<=len(description)<=2000 or not 0<=form.goal_kzt<=100000000:
        fail(400,'Проверьте название, описание и цель проекта')
    with db(True) as con:
        admin_for(request,con)
        con.execute('UPDATE project_fund SET headline=?,description=?,goal_kzt=?,updated_at=? WHERE id=1',
                    (headline,description,form.goal_kzt,now()))
    return {'ok':True}

@app.put('/api/admin/sponsor')
def sponsor_settings(form:SponsorSettings,request:Request):
    from urllib.parse import urlparse
    url=clean(form.checkout_url)
    note=clean(form.note)
    if len(url)>500 or len(note)>350:fail(400,'Слишком длинный адрес или описание')
    if url:
        parsed=urlparse(url)
        # No arbitrary custom URI, login info, IP-literal, or non-HTTPS payment destination.
        import ipaddress
        try:
            ipaddress.ip_address(parsed.hostname or '')
            fail(400,'IP-адрес вместо платёжного домена запрещён')
        except ValueError:
            pass
        if (parsed.scheme != 'https' or not parsed.hostname or '.' not in parsed.hostname
                or parsed.username or parsed.password or parsed.port not in (None,443)
                or any(c.isspace() for c in url)):
            fail(400,'Нужна проверенная HTTPS-страница платёжного сервиса')
    with db(True) as con:
        u=user_for(request,con)
        if u['staff_role'] != 'OWNER':fail(403,'Платёжную ссылку может изменять только владелец')
        con.execute('UPDATE sponsor_config SET checkout_url=?,note=?,updated_at=? WHERE id=1',
                    (url,note or 'Добровольная поддержка развития NEXUS Arena.',now()))
    return {'ok':True,'payments_enabled':bool(url)}


@app.post('/api/feedback')
def send_feedback(form:FeedbackInput,request:Request):
    subject=clean(form.subject);body=clean(form.body)
    if form.kind not in ('REVIEW','BUG','SUPPORT'):fail(400,'Некорректный тип обращения')
    if not 4<=len(subject)<=100 or not 10<=len(body)<=2000:fail(400,'Укажите заголовок и описание')
    if form.kind=='REVIEW' and (form.rating is None or form.rating not in range(1,6)):
        fail(400,'Выберите оценку от 1 до 5')
    if form.kind!='REVIEW' and form.rating is not None:fail(400,'Оценка доступна только в отзыве')
    with db(True) as con:
        u=user_for(request,con)
        attempts=record(con,'SELECT count(*) AS n FROM feedback WHERE author_id=? AND created_at>?',(u['id'],now()-3600))['n']
        if attempts>=5:fail(429,'Не более 5 обращений в час')
        if form.match_id is not None:
            m=record(con,'SELECT 1 FROM match_members WHERE match_id=? AND user_id=?',(form.match_id,u['id']))
            if not m:fail(403,'Можно пожаловаться только на свой матч')
        id=con.execute('INSERT INTO feedback(author_id,kind,subject,body,rating,match_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                       (u['id'],form.kind,subject,body,form.rating,form.match_id,now(),now())).lastrowid
    return {'id':id,'status':'NEW'}

@app.get('/api/feedback/mine')
def my_feedback(request:Request):
    with db() as con:
        u=user_for(request,con)
        return rows(con,'SELECT id,kind,subject,body,rating,match_id,status,admin_reply,public,created_at FROM feedback WHERE author_id=? ORDER BY id DESC LIMIT 100',(u['id'],))

@app.get('/api/reviews')
def public_reviews(request:Request):
    with db() as con:
        user_for(request,con)
        return rows(con,'''SELECT f.id,f.subject,f.body,f.rating,f.created_at,u.nickname AS author
                  FROM feedback f JOIN users u ON u.id=f.author_id
                  WHERE f.kind='REVIEW' AND f.public=1 AND u.is_banned=0 ORDER BY f.id DESC LIMIT 50''')

@app.get('/api/admin/feedback')
def admin_feedback(request:Request):
    with db() as con:
        admin_for(request,con)
        return rows(con,'''SELECT f.*,u.nickname AS author,u.email AS author_email FROM feedback f
               JOIN users u ON u.id=f.author_id ORDER BY (f.status='NEW') DESC,f.id DESC LIMIT 150''')

@app.patch('/api/admin/feedback/{feedback_id}')
def admin_update_feedback(feedback_id:int,form:FeedbackUpdate,request:Request):
    if form.status not in ('NEW','IN_PROGRESS','FIXED','RESOLVED','REJECTED'):
        fail(400,'Некорректный статус')
    reply=clean(form.admin_reply)
    if len(reply)>2000:fail(400,'Ответ слишком длинный')
    with db(True) as con:
        admin_for(request,con)
        f=record(con,'SELECT id,kind FROM feedback WHERE id=?',(feedback_id,))
        if not f:fail(404,'Обращение не найдено')
        con.execute('UPDATE feedback SET status=?,admin_reply=?,public=?,updated_at=? WHERE id=?',
                    (form.status,reply,int(bool(form.public) and f['kind']=='REVIEW'),now(),feedback_id))
    return {'ok':True}


class DeleteAccount(BaseModel):
    password: str


class ReportMessage(BaseModel):
    reason: str


@app.post('/api/clans/chat/{message_id}/report')
def report_message(message_id:int,form:ReportMessage,request:Request):
    reason=clean(form.reason)
    if not 3<=len(reason)<=300: fail(400,'Укажите причину жалобы')
    with db(True) as con:
        u=user_for(request,con);clan=my_clan(con,u['id'])
        msg=record(con,'SELECT clan_id,sender_id,deleted FROM clan_messages WHERE id=?',(message_id,))
        if not clan or not msg or msg['clan_id']!=clan['id'] or msg['deleted']: fail(403,'Сообщение недоступно')
        if msg['sender_id']==u['id']: fail(400,'Нельзя пожаловаться на своё сообщение')
        if record(con,'SELECT id FROM message_reports WHERE reporter_id=? AND message_id=?',(u['id'],message_id)):
            fail(409,'Жалоба уже отправлена')
        con.execute('INSERT INTO message_reports(message_id,reporter_id,reason,created_at) VALUES(?,?,?,?)',
                    (message_id,u['id'],reason,now()))
        return {'ok':True}


@app.post('/api/users/{user_id}/block')
def block_user(user_id:int,request:Request):
    with db(True) as con:
        u=user_for(request,con)
        if user_id==u['id']: fail(400,'Нельзя заблокировать себя')
        if not record(con,'SELECT id FROM users WHERE id=?',(user_id,)): fail(404,'Игрок не найден')
        existing=record(con,'SELECT 1 AS yes FROM blocked_users WHERE blocker_id=? AND blocked_id=?',(u['id'],user_id))
        if existing:
            con.execute('DELETE FROM blocked_users WHERE blocker_id=? AND blocked_id=?',(u['id'],user_id))
        else:
            con.execute('INSERT INTO blocked_users(blocker_id,blocked_id) VALUES(?,?)',(u['id'],user_id))
        return {'blocked':not bool(existing)}


@app.get('/api/users/blocked')
def blocked_users(request:Request):
    with db() as con:
        u=user_for(request,con)
        return rows(con,'SELECT u.id,u.nickname FROM blocked_users b JOIN users u ON u.id=b.blocked_id WHERE b.blocker_id=?',(u['id'],))


@app.get('/api/admin/reports')
def reports(request:Request):
    with db() as con:
        admin_for(request,con)
        return rows(con,'''SELECT r.id,r.message_id,r.reason,r.created_at,r.status,m.body,
          reporter.nickname AS reporter,sender.nickname AS sender
          FROM message_reports r JOIN clan_messages m ON m.id=r.message_id
          JOIN users reporter ON reporter.id=r.reporter_id
          JOIN users sender ON sender.id=m.sender_id
          WHERE r.status='PENDING' ORDER BY r.id DESC LIMIT 100''')


@app.post('/api/admin/reports/{report_id}/{decision}')
def review_report(report_id:int,decision:str,request:Request):
    if decision not in ('remove','dismiss'): fail(404,'Неизвестное действие')
    with db(True) as con:
        admin_for(request,con)
        report=record(con,"SELECT * FROM message_reports WHERE id=? AND status='PENDING'",(report_id,))
        if not report: fail(404,'Жалоба не найдена')
        if decision=='remove':
            con.execute('UPDATE clan_messages SET deleted=1 WHERE id=?',(report['message_id'],))
            con.execute("UPDATE message_reports SET status='REMOVED' WHERE message_id=?",(report['message_id'],))
        else: con.execute("UPDATE message_reports SET status='DISMISSED' WHERE id=?",(report_id,))
        return {'ok':True}


@app.post('/api/account/delete')
def delete_account(form:DeleteAccount,request:Request):
    with db(True) as con:
        u=user_for(request,con,True)
        pw=record(con,'SELECT password_hash FROM users WHERE id=?',(u['id'],))
        if not check_password(form.password,pw['password_hash']): fail(401,'Неверный пароль')
        clan=my_clan(con,u['id'])
        if clan and clan['my_role']=='LEADER' and clan['members_count']>1:
            fail(409,'Сначала передайте лидерство в клане')
        if clan and clan['my_role']=='LEADER': con.execute('DELETE FROM clans WHERE id=?',(clan['id'],))
        con.execute('DELETE FROM lobbies WHERE leader_id=?',(u['id'],))
        con.execute('DELETE FROM feedback WHERE author_id=?',(u['id'],))
        con.execute('DELETE FROM avatars WHERE user_id=?',(u['id'],))
        # Preserve other players' verified match history when a user deletes their account.
        # Open matches are cancelled and participants can queue again; archived completed
        # records remain, while this user's personal associations are removed.
        impacted=rows(con,'SELECT DISTINCT m.id,m.status FROM matches m JOIN match_members mm ON mm.match_id=m.id WHERE mm.user_id=?',(u['id'],))
        for item in impacted:
            if item['status'] in ('READY_CHECK','PENDING','REVIEW','DISPUTED'):
                cancel_found_match(con,item['id'],'Участник удалил аккаунт',u['id'])
        con.execute('DELETE FROM match_evidence WHERE captain_id=?',(u['id'],))
        con.execute('DELETE FROM match_stat_audit WHERE user_id=?',(u['id'],))
        for tbl,col in [('clan_requests','user_id'),('clan_messages','sender_id'),
                        ('clan_members','user_id'),('lobby_invites','invitee_id'),
                        ('lobby_invites','inviter_id'),('lobby_members','user_id'),
                        ('match_members','user_id'),('tournament_entries','user_id'),
                        ('tournaments','creator_id'),('announcements','author_id')]:
            con.execute(f'DELETE FROM {tbl} WHERE {col}=?',(u['id'],))
        con.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],))
        con.execute('DELETE FROM users WHERE id=?',(u['id'],))
    resp=JSONResponse({'ok':True})
    resp.delete_cookie(COOKIE_NAME,path='/')
    return resp



@app.get('/api/seasons')
def seasons():
    return season_catalog()


@app.get('/api/seasons/me')
def seasons_me(request: Request):
    active = season_catalog()['current']
    with db() as con:
        user = user_for(request, con)
        results = rows(con, '''
            SELECT m.mode,
                   COUNT(*) AS played,
                   COALESCE(SUM(CASE WHEN mm.side = d.winning_side THEN 1 ELSE 0 END),0) AS wins,
                   COALESCE(SUM(CASE WHEN mm.side <> d.winning_side THEN 1 ELSE 0 END),0) AS losses
              FROM match_members mm
              JOIN matches m ON m.id=mm.match_id AND m.status='COMPLETED'
              JOIN match_decisions d ON d.match_id=m.id
             WHERE mm.user_id=? AND d.reviewed_at>=? AND d.reviewed_at<?
          GROUP BY m.mode
        ''', (user['id'], active['starts_at'], active['ends_at']))
    by_mode = {mode: {'played': 0, 'wins': 0, 'losses': 0} for mode in GAME_MODES}
    for row in results:
        if row['mode'] in by_mode:
            by_mode[row['mode']] = {key: int(row[key]) for key in ('played', 'wins', 'losses')}
    return {'season': active, 'modes': by_mode}


@app.get('/api/seasons/leaderboard')
def seasons_leaderboard(request: Request, mode: str = '5v5', season: int | None = None):
    mode_size(mode)
    number = season_number() if season is None else season
    if number < 1 or number > season_number():
        fail(400, 'Такой сезон ещё не начался')
    bounds = season_metadata(number)
    with db() as con:
        user_for(request, con)
        return rows(con, '''
            SELECT u.id, u.nickname, u.avatar_updated_at,
                   COUNT(*) AS played,
                   SUM(CASE WHEN mm.side=d.winning_side THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN mm.side<>d.winning_side THEN 1 ELSE 0 END) AS losses
              FROM match_members mm
              JOIN users u ON u.id=mm.user_id
              JOIN matches m ON m.id=mm.match_id AND m.status='COMPLETED' AND m.mode=?
              JOIN match_decisions d ON d.match_id=m.id
             WHERE d.reviewed_at>=? AND d.reviewed_at<?
                   AND u.nickname IS NOT NULL AND u.is_banned=0
          GROUP BY u.id
          ORDER BY wins DESC, played DESC, u.nickname ASC
             LIMIT 30
        ''', (mode, bounds['starts_at'], bounds['ends_at']))


@app.get('/api/status')
def status(): return {'service':'NEXUS Arena','online':True,'version':'1.3'}


@app.get('/terms')
def terms(): return FileResponse(WEB/'terms.html')


@app.get('/privacy')
def privacy(): return FileResponse(WEB/'privacy.html')


@app.get('/account-deletion')
def account_deletion(): return FileResponse(WEB/'account-deletion.html')


@app.get('/')
def index(): return FileResponse(WEB/'index.html')


app.mount('/',StaticFiles(directory=str(WEB),html=True),name='frontend')
