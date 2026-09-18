#!/usr/bin/env python3
"""Offline migration: retain the old DB until the operator verifies the service."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from npu_top.db import Database

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--max-mb', type=int, default=1024)
    args = parser.parse_args()
    path = args.database.resolve()
    pending = path.with_name(path.name + '.rollup-building')
    backup = path.with_name(path.name + '.legacy')
    if pending.exists() or backup.exists():
        raise SystemExit('Migration staging/backup exists; inspect before retrying')
    # The caller must stop all service processes first.
    source = sqlite3.connect(path)
    source.row_factory = sqlite3.Row
    if not source.execute("SELECT 1 FROM sqlite_master WHERE name='host_samples'").fetchone():
        raise SystemExit('Already migrated; no action needed')
    checkpoint = source.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    if checkpoint[0]:
        raise SystemExit('Database has an active reader; stop the service first')
    dest = Database(pending, args.max_mb * 1024 * 1024)
    dest.initialize()
    connection = dest.connection()
    for row in source.execute('SELECT * FROM servers'):
        with connection:
            connection.execute(f"INSERT INTO servers ({','.join(row.keys())}) VALUES ({','.join('?' for _ in row)})", tuple(row))
    count = source.execute('SELECT count(*) FROM host_samples').fetchone()[0]
    done = 0
    last = 0
    start = time.monotonic()
    while True:
        rows = source.execute('SELECT id,server_id,collected_at,payload_json FROM host_samples WHERE id>? ORDER BY id LIMIT 250', (last,)).fetchall()
        if not rows:
            break
        with connection:
            for row in rows:
                snapshot = json.loads(row['payload_json'])
                snapshot['collected_at'] = row['collected_at']
                dest.aggregate_snapshot(row['server_id'], snapshot)
        done += len(rows)
        last = rows[-1]['id']
        if done % 10000 == 0:
            print(json.dumps(dict(converted=done,total=count,elapsed=round(time.monotonic()-start))), flush=True)
    converted = connection.execute('SELECT SUM(sample_count) FROM host_rollups').fetchone()[0] or 0
    if converted != count or done != count:
        raise RuntimeError('Migration sample count mismatch')
    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchall():
        raise RuntimeError('Migration database verification failed')
    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    connection.close()
    source.close()
    for suffix in ('-wal', '-shm'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    with pending.open('rb') as handle:
        os.fsync(handle.fileno())
    path.rename(backup)
    pending.rename(path)
    print(json.dumps(dict(status='migrated',samples=done,old_bytes=backup.stat().st_size,new_bytes=path.stat().st_size,rollback=str(backup))), flush=True)

if __name__ == '__main__':
    main()
