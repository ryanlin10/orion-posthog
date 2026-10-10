#!/usr/bin/env python3
"""Read a local CrowdCast msgpack segment; produce a PRIVATE browser test fixture.

Requires msgpack. No network, no modification of CrowdCast, no text/screenshots.
Physical key names still reveal typing: never commit the output fixture.
"""
import argparse
import json
from pathlib import Path

import msgpack

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('segment', type=Path)
parser.add_argument('--output', type=Path, default=Path('artifacts/crowdcast-trace.json'))
parser.add_argument('--max-events', type=int, default=20000)
args = parser.parse_args()
if args.max_events < 1:
    parser.error('--max-events must be positive')
with args.segment.open('rb') as f:
    records = msgpack.unpack(f, raw=False)
events = []
origin = None
skipped = 0
for row in records:
    if isinstance(row, dict):
        raw = row.get('raw_timestamp_us')
        item = row['event']
        kind, data = item['type'], item.get('data', {})
    else:
        _, (kind, data), raw = row
    if raw is None:
        skipped += 1
        continue  # Never substitute the quantized video clock.
    if origin is None:
        origin = raw
    event = {'t': (raw - origin) / 1000}
    if kind in ('KeyPress', 'KeyRelease'):
        code = data['name'] if isinstance(data, dict) else data[1]
        event.update(type='keydown' if kind == 'KeyPress' else 'keyup', code=code, location=0, repeat=False)
    elif kind == 'MouseMove':
        dx, dy, x, y = [data[k] for k in ('delta_x', 'delta_y', 'x', 'y')] if isinstance(data, dict) else data
        event.update(type='pointermove', clientX=x, clientY=y, movementX=dx, movementY=dy, pointerId=1, pointerType='mouse')
    elif kind in ('MousePress', 'MouseRelease'):
        button, x, y = [data[k] for k in ('button', 'x', 'y')] if isinstance(data, dict) else data
        event.update(type='pointerdown' if kind == 'MousePress' else 'pointerup', clientX=x, clientY=y,
                     button={'Left': 0, 'Middle': 1, 'Right': 2}.get(str(button), -1), pointerId=1, pointerType='mouse')
    elif kind == 'Redacted':
        event.update(type='boundary', reason='crowdcast-redacted')
    else:
        continue
    events.append(event)
    if len(events) >= args.max_events:
        break
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps({'format': 'crowdcast-browser-test.v1', 'clock': 'raw_timestamp_us',
    'note': 'Synthetic browser fixture; native screen coordinates are not viewport-calibrated. Repeat flags unavailable.',
    'skipped_without_raw_clock': skipped, 'events': events}))
args.output.chmod(0o600)
print(json.dumps({'output': str(args.output), 'events': len(events), 'skipped_without_raw_clock': skipped}))
