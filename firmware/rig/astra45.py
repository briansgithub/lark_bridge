"""Short hardware runs for Astra review cases 4 and 5 (test images with df8ffd1 hooks).

Case 4: the output drops the next A2DP DISCONNECTED event (simulated queue overflow), then
its link to the speaker drops (bt_disconnect stands in for sink power loss). Observe for
--observe seconds whether the worker notices and reconnects or keeps stale connected flags.
The 32UE is rebooted afterwards if it stays stale.

Case 5: the S31 fails the next 5 AVRCP release submissions, then taps STOP five times (the
output's retry budget), taps PAUSE (a different key) and STOP again. Each tap's result and
pending-release state is read from the S31 console; Pixel logcat lines that mention AVRCP
passthrough or media keys are kept. STOP/PAUSE only: no key here can start media or
change volume.

  py -3 firmware/rig/astra45.py OUT_DIR [--case 4|5|both] [--observe 60]
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

root = Path.cwd()
sys.path[:0] = [str(root / 'rig/esp'), str(root / 'firmware/tools')]
import arrival  # noqa: E402
import h2h5 as rig  # noqa: E402

ADB = r'C:\Users\user1\AppData\Local\Android\Sdk\platform-tools\adb.exe'
PIXEL = '3C081JEHN14930'
TAP = re.compile(r'TEST: avrcp tap cmd=(0x[0-9a-f]+) result=(\w+) release_pending=(\d)')


def console(bench, start, role, needle):
    return [r['line'] for r in bench.records[start:]
            if r.get('role') == role and r['t'] == 'console' and needle in r.get('line', '')]


def output_bt(bench):
    stats = bench.command('output', 'stats')
    return {'connected': stats['bt']['connected'], 'streaming': stats['bt']['streaming'],
            'audio': stats['audio'], 'fifo': stats['fifo']['level'], 'up_ms': stats['up_ms']}


def case4(bench, observe):
    out = {}
    bench.wait_link(30, lambda pair: rig.streaming(bench.command('output', 'stats')))
    out['before'] = output_bt(bench)
    start = len(bench.records)
    bench.command('output', 'fault bt_drop_disc')
    bench.command('output', 'fault bt_disconnect')
    t0 = time.monotonic()
    samples = []
    while time.monotonic() - t0 < observe:
        bench.wait(2)
        s = output_bt(bench)
        s['t'] = round(time.monotonic() - t0, 1)
        samples.append(s)
    out['samples'] = samples
    out['console'] = [line[:200] for line in console(bench, start, 'output', '')
                      if re.search(r'TEST|disconnect|connect|discover|stale|overflow|timed out',
                                   line, re.I)][:40]
    reconnected = any(not s['connected'] for s in samples) and samples[-1]['connected'] \
        and samples[-1]['streaming']
    stale = all(s['connected'] for s in samples) and not samples[-1]['audio']
    out['result'] = ('reconnected' if reconnected else
                     'stale connected flags (Astra 4 reproduced)' if stale else 'other')
    if not (samples[-1]['connected'] and samples[-1]['streaming']):
        bench.restart = 'output'
        bench.boots['output'] = 0
        bench.command('output', 'fault reboot')
        bench.wait(4)
        bench.wait_link(45, lambda pair: rig.streaming(bench.command('output', 'stats')))
        bench.restart = None
        out['recovered_by'] = 'output reboot'
    bench.command('s31', 'fault test_a2dp')
    bench.command('s31', 'tone routed_i2s 1000 -50')
    return out


def case5(bench, logcat_lines):
    out = {'taps': []}
    peers = bench.command('s31', 'peers')['bt']
    if not peers.get('avrcp'):
        return {'skipped': f'AVRCP not connected: {peers}'}
    del logcat_lines[:]

    def tap(kind):
        start = len(bench.records)
        bench.command('s31', f'fault {kind}')
        bench.wait(0.6)
        lines = console(bench, start, 's31', 'TEST: avrcp tap')
        m = TAP.search(lines[-1]) if lines else None
        row = {'tap': kind, 'cmd': m and m.group(1), 'result': m and m.group(2),
               'release_pending': m and int(m.group(3))}
        out['taps'].append(row)
        return row

    bench.command('s31', 'fault avrcp_relfail')
    for _ in range(5):
        tap('avrcp_tap_stop')
    out['different_key_after_exhaustion'] = tap('avrcp_tap_pause')
    out['same_key_again'] = tap('avrcp_tap_stop')
    out['different_key_after_recovery'] = tap('avrcp_tap_pause')
    bench.wait(2)
    out['pixel_logcat'] = list(logcat_lines)[:60]
    blocked = out['different_key_after_exhaustion']['result'] not in ('ESP_OK', None)
    out['result'] = ('different key blocked while release pending (Astra 5 reproduced)'
                     if blocked else 'different key accepted')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out', type=Path)
    ap.add_argument('--case', choices=('4', '5', 'both'), default='both')
    ap.add_argument('--observe', type=int, default=60)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    logcat_lines = []
    proc = None
    if a.case in ('5', 'both'):
        subprocess.run([ADB, '-s', PIXEL, 'logcat', '-c'], timeout=15)
        proc = subprocess.Popen([ADB, '-s', PIXEL, 'logcat', '-v', 'time'], stdout=subprocess.PIPE,
                                text=True, encoding='utf-8', errors='replace')
        pat = re.compile(r'avrc|passthrough|media_?key|keycode_media|KEYCODE_MEDIA|MediaButton', re.I)

        def reader():
            with (a.out / 'pixel-logcat-avrcp.log').open('w', encoding='utf-8') as sink:
                for line in proc.stdout:
                    if pat.search(line):
                        logcat_lines.append(line.strip()[:220])
                        sink.write(line)
        threading.Thread(target=reader, daemon=True).start()
    bench = rig.Bench(('COM3', 'COM4'), a.out, arrival._quiet_serial_factory)
    summary = {}
    try:
        bench.wait(1)
        summary['versions'] = {r: bench.command(r, 'status')['version'] for r in rig.ROLES}
        if a.case in ('5', 'both'):
            summary['case5'] = case5(bench, logcat_lines)
            print('case5', json.dumps({k: v for k, v in summary['case5'].items()
                                       if k != 'pixel_logcat'}), flush=True)
        if a.case in ('4', 'both'):
            summary['case4'] = case4(bench, a.observe)
            print('case4', json.dumps({k: summary['case4'].get(k) for k in
                                       ('before', 'result', 'recovered_by')}), flush=True)
    except (rig.BenchError, KeyError, OSError) as exc:
        summary['error'] = f'{type(exc).__name__}: {exc}'
        bench.add(t='abort', reason=str(exc))
        print('error', summary['error'], flush=True)
    finally:
        bench.close()
        if proc is not None:
            proc.kill()
        (a.out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    sys.exit(1 if 'error' in summary else 0)


if __name__ == '__main__':
    main()
