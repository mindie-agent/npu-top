"""Parser for the single-ID, four-column npu-smi device table (Ascend 950)."""
from __future__ import annotations

import re
from typing import Any


def parse_single_id_table(info: str) -> dict[str, Any] | None:
    if not re.search(r"\|\s*NPU ID\s*\|\s*Name\s*\|\s*Health\s*\|", info):
        return None
    devices: dict[int, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    current = None
    processes = False
    for line in info.splitlines():
        if not line.strip().startswith('|'):
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if 'Process id' in cells:
            processes = True
            current = None
            continue
        if processes:
            if len(cells) >= 4 and cells[0].isdigit() and cells[1].isdigit():
                npu_id = int(cells[0])
                if npu_id not in devices:
                    raise ValueError('NPU process references an unparsed device')
                record = {'npu_id': npu_id, 'chip_id': None, 'pid': int(cells[1]),
                          'npu_process_name': cells[2],
                          'npu_memory_mb': int(cells[3]) if cells[3].isdigit() else None}
                if len(cells) > 4:
                    record['container_pid'] = int(cells[4]) if cells[4].isdigit() else None
                records.append(record)
                devices[npu_id]['processes'].append(record)
            continue
        if len(cells) != 4:
            continue
        if cells[0].isdigit() and cells[1]:
            current = int(cells[0])
            values = cells[3].split()
            devices[current] = {'npu_id': current, 'chip_id': None, 'name': cells[1],
                                'health': cells[2], 'processes': [], 'chips': [],
                                'power_w': float(values[0]), 'temperature_c': float(values[1])}
        elif current is not None and not cells[0] and not cells[1]:
            match = re.fullmatch(r'(\d+(?:\.\d+)?|NA)\s+(\d+)\s*/\s*(\d+)\s+(\d+)\s*/\s*(\d+)', cells[3])
            if not match:
                raise ValueError('Unrecognized single-ID NPU metric row')
            util, mem_used, mem_total, hbm_used, hbm_total = match.groups()
            devices[current].update({'bus_id': None if cells[2] == 'NA' else cells[2],
                                     'aicore_percent': None if util == 'NA' else float(util),
                                     'memory': {'used_mb': int(mem_used), 'total_mb': int(mem_total)},
                                     'hbm': {'used_mb': int(hbm_used), 'total_mb': int(hbm_total)}})
            current = None
    if not devices or any('hbm' not in device for device in devices.values()):
        raise ValueError('Incomplete single-ID NPU device table')
    return {'devices': list(devices.values()), 'process_records': records}
