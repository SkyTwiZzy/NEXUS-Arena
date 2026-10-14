"""Create an online-safe SQLite backup of the local NEXUS Arena database.

Usage: python tools/backup_database.py
"""
from datetime import datetime
from pathlib import Path
import os
import sqlite3

root=Path(__file__).resolve().parents[1]
src=Path(os.environ.get('NEXUS_DB',str(root/'server'/'arena.sqlite3')))
if not src.is_file():
    raise SystemExit('Database not found. Run NEXUS Arena once and retry.')
backup_dir=root/'backups'
backup_dir.mkdir(parents=True,exist_ok=True)
dest=backup_dir/('arena_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.sqlite3')
with sqlite3.connect(src) as con_src, sqlite3.connect(dest) as con_dest:
    con_src.backup(con_dest)
print('Backup created at:',dest)
print('Keep this private. Never upload backups to GitHub or a public web folder.')
