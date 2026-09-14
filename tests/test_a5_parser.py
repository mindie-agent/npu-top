import unittest
from pathlib import Path
from vaws_top import npu_smi
from vaws_top.probe import is_device_busy


class A5ParserTests(unittest.TestCase):
    def setUp(self):
        self.adapter = npu_smi
        self.fixtures = Path(__file__).parent / 'fixtures'

    def test_four_captured_hosts(self):
        for host in ('13', '21', '22', '23'):
            with self.subTest(host=host):
                parsed = self.adapter.parse_npu((self.fixtures / f'a5-{host}.txt').read_text(), (self.fixtures / 'a5-usages.txt').read_text())
                devices = parsed['devices']
                self.assertEqual([d['npu_id'] for d in devices], list(range(8)))
                self.assertEqual(sum(d['hbm']['total_mb'] for d in devices), 786432)
                for d in devices:
                    self.assertEqual(d['name'], 'Ascend950DT')
                    self.assertEqual(d['health'], 'Warning' if host in ('21', '22') else 'OK')
                    self.assertEqual(d['aicore_percent'], 0)
                    self.assertGreater(d['hbm']['used_mb'], 4700)
                    self.assertGreater(d['power_w'], 400)
                    self.assertGreater(d['temperature_c'], 30)
                    self.assertIsNone(d['bus_id'])
                self.assertEqual(sum(is_device_busy(d, 8192) for d in devices), int(host == '23'))
                if host == '23':
                    self.assertEqual(devices[0]['processes'][0]['pid'], 3252331)
                    self.assertEqual(devices[0]['processes'][0]['npu_memory_mb'], 341)
                    self.assertEqual(devices[0]['processes'][0]['npu_process_name'], 'udma_d2h_demo')

    def test_truncated_table_fails(self):
        text = (self.fixtures / 'a5-13.txt').read_text()
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            self.adapter.parse_npu(text[:text.index('|        |                  | NA')], '')

    def test_unknown_output_fails(self):
        with self.assertRaisesRegex(ValueError, 'no recognizable'):
            self.adapter.parse_npu('npu-smi future format', '')

    def test_nonzero_utilization(self):
        text = (self.fixtures / 'a5-13.txt').read_text().replace('0                     0     / 0', '72.5                  0     / 0')
        self.assertEqual(self.adapter.parse_npu(text, '')['devices'][0]['aicore_percent'], 72.5)
