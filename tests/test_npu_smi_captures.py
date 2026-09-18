"""Regression contracts from real A3/A5 npu-smi captures."""
import hashlib
import json
import unittest
from pathlib import Path

from npu_top.probe import attach_npu_telemetry, is_device_busy
from npu_top import npu_smi


class CapturedNpuSmiTests(unittest.TestCase):
    fixtures = Path(__file__).parent / 'fixtures'

    def test_capture_provenance_and_integrity(self):
        manifest = json.loads((self.fixtures / 'npu-smi-captures.json').read_text())
        self.assertEqual({sample['hardware'] for sample in manifest['samples']}, {'A3', 'A5'})
        for sample in manifest['samples']:
            for command in sample['commands']:
                with self.subTest(hardware=sample['hardware'], command=command['command']):
                    raw = (self.fixtures / command['file']).read_bytes()
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), command['sha256'])

    def test_a3_dual_chip_aggregation_and_process_mapping(self):
        adapter = npu_smi
        info = (self.fixtures / 'a3-106.txt').read_text()
        parsed = adapter.parse_npu(info, (self.fixtures / 'a3-usages.txt').read_text())
        devices = parsed['devices']
        attach_npu_telemetry(devices, info)
        self.assertEqual([d['npu_id'] for d in devices], list(range(8)))
        expected_hbm = [74451, 74368, 74347, 74369, 74368, 74367, 74350, 74365]
        self.assertEqual([d['hbm']['used_mb'] for d in devices], expected_hbm)
        self.assertEqual(sum(d['hbm']['total_mb'] for d in devices), 1048576)
        self.assertEqual(len(parsed['process_records']), 16)
        pids = [(3224097, 3246544), (3268834, 3291129), (3305013, 3309226),
                (3313361, 3317914), (3322118, 3326625), (3345245, 3363635),
                (3381076, 3398615), (3415355, 3431925)]
        for i, d in enumerate(devices):
            with self.subTest(npu=i):
                self.assertEqual(d['name'], 'Ascend910')
                self.assertEqual(d['health'], 'OK')
                self.assertEqual(d['hbm']['total_mb'], 131072)
                self.assertEqual(d['aicore_percent'], 0)
                self.assertEqual([c['chip_id'] for c in d['chips']], [0, 1])
                self.assertEqual([c['phy_id'] for c in d['chips']], [2*i, 2*i+1])
                self.assertEqual([p['pid'] for p in d['processes']], list(pids[i]))
                self.assertEqual([p['chip_id'] for p in d['processes']], [0, 1])
                self.assertTrue(all(p['npu_id'] == i for p in d['processes']))
                self.assertTrue(all(p['npu_process_name'] == 'VLLMWorker_DP' for p in d['processes']))
                self.assertTrue(is_device_busy(d, 8192))
        self.assertEqual(devices[0]['processes'][0]['npu_memory_mb'], 34309)
        self.assertEqual(devices[0]['power_w'], 175.9)
        self.assertEqual(devices[0]['temperature_c'], 33)
        self.assertEqual(devices[0]['chips'][1]['bus_id'], '0000:9F:00.0')
