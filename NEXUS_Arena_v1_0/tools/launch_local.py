"""Starts the local NEXUS Arena service and opens the page only when ready."""
from __future__ import annotations
import argparse
import getpass
import os
from pathlib import Path
import socket
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--developer', action='store_true')
    args = parser.parse_args()
    os.chdir(ROOT)
    # Running a script from tools/ changes sys.path[0]; add the project root
    # so Uvicorn can import the server package.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    # Local use only: secure cookies are for HTTPS deployments, not localhost.
    os.environ['COOKIE_SECURE'] = '0'
    os.environ.pop('PUBLIC_ORIGIN', None)
    os.environ.setdefault('NEXUS_DB', str(ROOT / 'server' / 'arena.sqlite3'))
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
