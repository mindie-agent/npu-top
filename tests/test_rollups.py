import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from vaws_top.db import Database

class RollupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'db', 16 * 1024 * 1024)
        self.db.initialize()
        self.db.upsert_server(dict(id='s', name='s', host='s', port=22, username='root'))
        self.now = int(time.time()) // 900 * 900
    def tearDown(self):
        self.db.connection().close()
        self.temp.cleanup()
    def add(self, timestamp, cpu, device=50):
        self.db.record_success('s', dict(collected_at=timestamp, summary=dict(cpu_percent=cpu), devices=[dict(npu_id=0, aicore_percent=device, hbm={})], secret_detail='large detail'), True)
    def test_weighted_aggregation_and_nulls(self):
        self.add(self.now, 0)
        self.add(self.now + 1, 30)
        self.add(self.now - 900, 90)
        self.add(self.now + 2, None, None)
        point = self.db.history('s', self.now - 900, 86400 * 100000)[0]
        self.assertEqual(point['cpu_percent'], 40)
        points = self.db.history_heatmap('s', self.now - 900)
        self.assertEqual(sum(p['sample_count'] for p in points), 4)
        self.assertEqual(self.db.connection().execute('select count(*) from host_rollups').fetchone()[0], 2)
        self.assertEqual(self.db.connection().execute('select count(*) from sqlite_master where name in ("host_samples","collection_events")').fetchone()[0], 0)
    def test_retention_and_latest(self):
        self.add(self.now - 100 * 86400, 10)
        self.add(self.now, 20)
        self.db.prune(90)
        self.assertEqual(self.db.connection().execute('select sum(sample_count) from host_rollups').fetchone()[0], 1)
        self.assertEqual(self.db.latest_persisted(), {'s': self.now})
    def test_hard_cap(self):
        c = self.db.connection()
        self.assertEqual(c.execute('pragma max_page_count').fetchone()[0] * c.execute('pragma page_size').fetchone()[0], 16 * 1024 * 1024)
        c.execute('create table fill(data blob)')
        with self.assertRaises(sqlite3.OperationalError):
            with c:
                c.execute('insert into fill values(zeroblob(17000000))')
        self.assertLessEqual(self.db.path.stat().st_size, self.db.max_bytes)
    def test_pressure_discards_oldest(self):
        self.add(self.now - 86400, 10)
        self.add(self.now, 20)
        self.db.max_bytes = 1
        self.db.prune(90)
        self.assertEqual(self.db.connection().execute('select count(*) from host_rollups').fetchone()[0], 0)
        self.assertEqual(self.db.connection().execute('select count(*) from device_rollups').fetchone()[0], 0)
    def test_legacy_requires_explicit_migration(self):
        self.db.connection().execute('create table host_samples(id integer)')
        with self.assertRaisesRegex(RuntimeError, 'offline'):
            self.db.initialize()

    def test_history_concurrency_limit(self):
        self.db._history_slots.acquire()
        self.db._history_slots.acquire()
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'busy'):
                self.db.history('s', 0, 900)
        finally:
            self.db._history_slots.release()
            self.db._history_slots.release()
        self.assertEqual(self.db.history('s', 0, 900), [])
