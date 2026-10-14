"""Starts the local NEXUS Arena service and opens the page only when ready."""
from __future__ import annotations
import argparse
import getpass
import os
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent.parent
URL = 'http://127.0.0.1:8000'


def open_when_ready() -> None:
    for _ in range(150):
        try:
            with urlopen(URL + '/api/status', timeout=0.6) as response:
                if response.status == 200:
                    print('\nSUCCESS: NEXUS Arena is ready: ' + URL, flush=True)
                    print('Leave this window open while using the site.', flush=True)
                    if os.getenv('NEXUS_AUTO_OPEN', '1') != '0':
                        webbrowser.open(URL)
                    return
        except Exception:
            time.sleep(0.2)
    print('The site could not pass its start-up health check.', flush=True)


def configure_developer() -> None:
    print('DEVELOPER ACCOUNT - FIRST LAUNCH')
    print('Initial owner setup. Existing accounts must prove ownership with their current password.')
    print('A password is never written to project files or displayed in this console.')
    email = input('Owner email: ').strip().lower()
    if '@' not in email or len(email) < 5:
        sys.exit('ERROR: Enter a valid email address.')
    password = getpass.getpass('Developer password (min. 12 characters): ')
    if len(password) < 12:
        sys.exit('ERROR: Password must contain at least 12 characters.')
    os.environ['ADMIN_EMAIL'] = email
    os.environ['ADMIN_PASSWORD'] = password
    os.environ['ADMIN_NICK'] = 'NEXUS_OWNER'
    print('This password is not stored in a configuration file.')
    print('For an existing player account, you must enter its CURRENT password.')
    print('If this password was shared in a chat, change it after setting up your account.')
    print('Note: this launcher does not reset existing passwords.')


def require_owner(db_path: Path) -> None:
    """Never open a public share link for a database without an OWNER role."""
    if not db_path.is_file():
        sys.exit('ERROR: Owner not configured. First run SETUP_OWNER_AND_START.bat, then stop it.')
    try:
        con = sqlite3.connect(db_path)
        try:
            owner = con.execute("SELECT 1 FROM users WHERE staff_role='OWNER' LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        sys.exit('ERROR: Could not verify owner account: ' + str(exc))
    if not owner:
        sys.exit('ERROR: No OWNER account. Run SETUP_OWNER_AND_START.bat before sharing publicly.')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--developer', action='store_true')
    parser.add_argument('--public', action='store_true', help='Serve locally behind an HTTPS tunnel')
    args = parser.parse_args()
    os.chdir(ROOT)
    # Running a script from tools/ changes sys.path[0]; add the project root
    # so Uvicorn can import the server package.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ.setdefault('NEXUS_DB', str(ROOT / 'server' / 'arena.sqlite3'))
    # --public is intended ONLY with HTTPS termination at a trusted tunnel.
    os.environ['COOKIE_SECURE'] = '1' if args.public else '0'
    os.environ.pop('PUBLIC_ORIGIN', None)
    if args.public:
        require_owner(Path(os.environ['NEXUS_DB']))
        os.environ['NEXUS_AUTO_OPEN'] = '0'
        print('PUBLIC SHARE MODE: HTTPS tunnel is required; do not share localhost.')
        print('Change any password previously shared in chat before proceeding.')
    if args.developer:
        configure_developer()
        # Bootstrap once, then remove the secret from the running server process.
        import importlib
        importlib.import_module('server.app')
        os.environ.pop('ADMIN_PASSWORD', None)
        os.environ.pop('ADMIN_EMAIL', None)
        os.environ.pop('ADMIN_NICK', None)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', 8000)) == 0:
                print('ERROR: Port 8000 is busy. Close the old NEXUS window or the other server.')
                return 1
    except OSError:
        pass
    try:
        import uvicorn
        threading.Thread(target=open_when_ready, daemon=True).start()
        print('Starting NEXUS Arena on ' + URL)
        if args.public:
            print('NEXT: leave this window open and launch START_PUBLIC_LINK.bat in another window.')
        print('Press Ctrl+C in this window to stop.')
        uvicorn.run('server.app:app', host='127.0.0.1', port=8000, log_level='info')
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print('SERVER ERROR: ' + str(exc), flush=True)
        raise
    return 0


if __name__ == '__main__':
    sys.exit(main())
