"""Simultaneous cold boot of the whole bench through the Shelly plug (Windows Python).

Each cycle cuts hub power for 1 s with shelly_power.ps1 (the plug turns itself back on),
then times, from power-on: COM3/COM4 enumeration, S31 and 32UE application start,
UART link-up, Pixel HFP+A2DP reconnect, speaker connection and -50 dBFS stream start.
It then scores a short H5 window and reads the heartbeat counters and gap telemetry.

Usage (from the repo root):
  py -3 firmware/rig/cold_boot.py OUT_DIR [--cycles 5] [--window 60] [--settle 0]

Stops on an unknown or missing USB serial device, a reset loop, or a crash. The relay is
mechanical: keep the total to dozens of cycles.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import tomllib

root = Path.cwd()
sys.path[:0] = [str(root / 'rig/esp'), str(root / 'firmware/tools')]
import arrival  # noqa: E402
import h2h5 as rig  # noqa: E402
from serial.tools import list_ports  # noqa: E402

SHELLY = root / 'firmware/rig/shelly_power.ps1'
POWERSHELL = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
EXPECTED = {'COM3': ('303A:1001', '1C:29:04:D0:61:5E'),
            'COM4': ('10C4:EA60', '5C62C6474CBDF011B3D1167148E9DE0F')}
ALLOWED = set(EXPECTED) | {'COM1'}
SPEAKER = 'c9:5c:fd:6e:28:46'


def allowed_peers(expected_peer=None):
    """Return allowed lowercase BT sink addresses (or None if 'any')."""
    if expected_peer is not None:
        val = expected_peer.strip().lower()
        return None if val == 'any' else {val}
    peers = {SPEAKER}
    inv_path = root / 'rig/inventory.toml'
    if inv_path.is_file():
        try:
            inv = tomllib.loads(inv_path.read_text(encoding='utf-8'))
            rig_bt = inv.get('rig_bt_addr')
            if isinstance(rig_bt, str) and rig_bt.strip():
                peers.add(rig_bt.strip().lower())
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return peers


class Stop(RuntimeError):
    """Abort the whole run (unsafe or unexpected bench state)."""


def shelly(action, off=1):
    args = [POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SHELLY), action]
    if action == 'cycle':
        args += ['-OffSeconds', str(off)]
    done = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if done.returncode != 0:
        raise Stop(f'shelly {action} failed: {done.stderr.strip()[:300]}')
    return json.loads(done.stdout) if done.stdout.strip().startswith('{') else done.stdout.strip()


def ports_now():
    found = {}
    for p in list_ports.comports():
        if p.device not in ALLOWED:
            raise Stop(f'unknown USB serial device: {p.device} {p.hwid}')
        found[p.device] = p.hwid
    for dev, hwid in found.items():
        if dev in EXPECTED and not all(part in hwid for part in EXPECTED[dev]):
            raise Stop(f'unexpected identity on {dev}: {hwid}')
    return found


def wait_ports(deadline, t_on):
    seen = {}
    while time.monotonic() < deadline:
        found = ports_now()
        for dev in EXPECTED:
            if dev in found and dev not in seen:
                seen[dev] = round(time.monotonic() - t_on, 2)
        if len(seen) == len(EXPECTED):
            return seen
        time.sleep(0.2)
    raise Stop(f'ports did not return within the deadline: {seen}')


def open_bench(cyc, deadline):
    import serial
    while True:
        time.sleep(0.5)
        try:
            return rig.Bench(('COM3', 'COM4'), cyc, arrival._quiet_serial_factory)
        except (OSError, serial.SerialException, rig.BenchError) as exc:
            if time.monotonic() > deadline:
                raise Stop(f'could not open consoles: {exc}') from exc
            ports_now()


def pixel_up(peers):
    bt = peers['bt']
    return bt.get('peer_bound') and bt.get('hfp_state', 0) >= 3 and bt.get('a2dp_state', 0) >= 2


def s31_reset():
    # The S31 is not on the Shelly hub, so pulse its reset over USB-Serial-JTAG (warm reset).
    return subprocess.Popen([sys.executable, '-m', 'esptool', '--chip', 'esp32s31', '--port', 'COM3',
                             '--no-stub', '--after', 'hard-reset', 'chip-id'],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def one_cycle(n, out, limits, window, off, reset_s31=False, expected_peer=None):
    cyc = out / f'cycle{n}'
    cyc.mkdir(parents=True, exist_ok=True)
    peers_allowed = allowed_peers(expected_peer)
    result = {'cycle': n}
    before = shelly('status')
    t_cmd = time.monotonic()
    shelly('cycle', off)
    t_on = t_cmd + off  # plug turns back on off seconds after the command lands
    time.sleep(max(0, t_on + 0.3 - time.monotonic()))
    result['ports_s'] = wait_ports(t_on + 45, t_on)
    if reset_s31:
        for attempt in range(8):
            time.sleep(0.5)
            proc = s31_reset()
            log, _ = proc.communicate(timeout=60)
            if proc.returncode == 0:
                break
        (cyc / 's31-reset.log').write_text(log)
        result['s31_reset'] = {'rc': proc.returncode, 'attempts': attempt + 1,
                               'done_s': round(time.monotonic() - t_on, 2)}
        if proc.returncode != 0:
            raise Stop(f'S31 reset failed: {log.strip()[-300:]}')
    bench = open_bench(cyc, t_on + 60)
    result['bench_open_s'] = round(time.monotonic() - t_on, 2)
    marks = {}

    def mark(name):
        if name not in marks:
            marks[name] = round(time.monotonic() - t_on, 2)
            bench.add(t='mark', name=name, since_power_on=marks[name])

    try:
        tone_sent = False
        deadline = t_on + 120
        while time.monotonic() < deadline:
            bench.wait(0.4)
            pair = {}
            for role in rig.ROLES:
                try:
                    pair[role] = bench.command(role, 'status', timeout=1.5)
                except rig.BenchError as exc:
                    if 'did not answer' not in str(exc):
                        raise
            for role, st in pair.items():
                key = f'{role}_app_start'
                if key not in marks:
                    marks[key] = round(st['host'] - st['up_ms'] / 1000 - t_on, 2)
                    result[f'{role}_reset_reason'] = st.get('reset')
            if 's31' in pair and not tone_sent:
                bench.command('s31', 'fault test_a2dp')
                bench.command('s31', 'tone routed_i2s 1000 -50')
                tone_sent = True
            if len(pair) == 2 and all(rig.alive(pair[r]) for r in rig.ROLES):
                if 'uart_link' not in marks:
                    mark('uart_link')
                    bench.command('s31', 'fault test_a2dp')
                    bench.command('s31', 'tone routed_i2s 1000 -50')
            if 's31' in pair and 'pixel' not in marks and pixel_up(bench.command('s31', 'peers')):
                mark('pixel')
            if 'output' in pair:
                bt = bench.command('output', 'peers')['bt']
                if bt['connected'] and 'speaker_connected' not in marks:
                    peer_addr = bt['peer'].lower()
                    if peers_allowed is not None and peer_addr not in peers_allowed:
                        raise rig.BenchError(
                            f'unexpected speaker peer {bt} (allowed: {sorted(peers_allowed)})')
                    result['speaker_peer'] = peer_addr
                    mark('speaker_connected')
                if 'uart_link' in marks and rig.streaming(bench.command('output', 'stats')):
                    mark('stream')
            if {'uart_link', 'pixel', 'stream'} <= set(marks):
                break
        result['marks_s'] = marks
        missing = {'uart_link', 'pixel', 'speaker_connected', 'stream'} - set(marks)
        if missing:
            result['missing'] = sorted(missing)
        if 'stream' in marks:
            bench.wait(3)
            bench.command('output', 'fault fifo_window')
            first = len(bench.records)
            start = bench.pair('stats')
            start['output'].setdefault('stats_period_ms', 1000)
            bench.add(t='start', phase='stream', data=start)
            bench.wait(window)
            end = bench.pair('stats')
            end['output'].setdefault('stats_period_ms', 1000)
            bench.add(t='end', phase='stream', data=end)
            verdict, metrics, reasons = rig.reduce('H5', dict(limits, stream_seconds=window - 1),
                                                   bench.records[first:])
            result.update(h5=verdict, h5_metrics=metrics, h5_reasons=reasons)
            for role in rig.ROLES:
                u0, u1 = start[role]['uart'], end[role]['uart']
                result[role] = {'timeouts_total': u1['timeouts'],
                                'timeouts_window': u1['timeouts'] - u0['timeouts'],
                                'sessions': u1['sessions'], 'dec_err': u1['dec_err']}
            result['s31']['live_rx'] = end['s31']['uart'].get('live_rx')
            result['s31']['rx_loop_max_ms'] = end['s31']['uart'].get('rx_loop_max_ms')
            result['output']['live_tx'] = end['output']['uart'].get('live_tx')
            result['output']['live_write_max_ms'] = end['output']['uart'].get('live_write_max_ms')
        result['boots_seen'] = dict(bench.boots)
        result['plug_before_w'] = before.get('apower') if isinstance(before, dict) else None
    finally:
        bench.close()
    (cyc / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out', type=Path)
    ap.add_argument('--cycles', type=int, default=5)
    ap.add_argument('--window', type=int, default=60)
    ap.add_argument('--off', type=int, default=1, choices=(1, 2))
    ap.add_argument('--s31-reset', action='store_true',
                    help='also hard-reset the S31 (not on the plug) as power returns')
    ap.add_argument('--settle', type=float, default=0, help='seconds idle between cycles')
    ap.add_argument('--expected-peer', default=None,
                    help='expected 32UE BT sink address (default: MP43247 or rig_bt_addr; or "any")')
    a = ap.parse_args()
    if not 1 <= a.cycles <= 40:
        sys.exit('cycles must be 1..40 (mechanical relay)')
    a.out.mkdir(parents=True, exist_ok=True)
    limits = tomllib.loads((root / 'firmware/qual/gates.toml').read_text())['gates']['H5']
    ports_now()
    results, rc = [], 0
    for n in range(1, a.cycles + 1):
        try:
            r = one_cycle(n, a.out, limits, a.window, a.off, a.s31_reset, a.expected_peer)
        except rig.BenchError as exc:
            r, rc = {'cycle': n, 'error': str(exc)}, 4 if 'loop' in str(exc) else 2
        except Stop as exc:
            r, rc = {'cycle': n, 'stop': str(exc)}, 3
        results.append(r)
        print(json.dumps({k: r.get(k) for k in ('cycle', 'marks_s', 'missing', 'h5', 'error', 'stop')}
                         | {'to': {x: r[x]['timeouts_window'] for x in rig.ROLES if x in r}}),
              flush=True)
        if rc in (3, 4):
            break
        if r.get('h5') != 'PASS':
            rc = rc or 2
        if n < a.cycles and a.settle:
            time.sleep(a.settle)
    (a.out / 'summary.json').write_text(json.dumps(results, indent=2) + '\n')
    try:
        st = shelly('status')
        if isinstance(st, dict) and st.get('output') is not True:
            shelly('on')
            print('plug was off after the run; turned it on', flush=True)
    except Stop as exc:
        print('final plug check failed:', exc, flush=True)
        rc = rc or 3
    sys.exit(rc)


if __name__ == '__main__':
    main()
