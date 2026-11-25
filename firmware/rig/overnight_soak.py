"""Overnight media soak with occasional Shelly cold boots (Windows Python, repo root).

The source runs back to back in scored segments (default 20 min):
  media: the Pixel harness plays its 1 kHz WAV as A2DP media (Pixel -> S31 -> I2S -> 32UE
         -> MP43247), with the real route (fault test_off, tone off). This is the default.
  tone:  the S31 -50 dBFS routed_i2s keep-alive (no phone in the path).
If the media start fails twice, the soak falls back to the tone for that segment and says so.

Each segment is scored with the H5 reducer (FIFO, ppm, under/overrun, I2S errors, PCM
continuity), plus heartbeat timeouts, liveness gaps, UART sessions, decode errors and UAC
lost events on both boards. After every --boot-every segments the bench is cold-booted
through cold_boot.py (shelly_power.ps1, 1 s off, S31 reset pulse) and that cycle is scored
too. Keep --boot-every so cycles stay a few per hour at most (the relay is mechanical).

  py -3 firmware/rig/overnight_soak.py OUT_DIR [--hours 7.5] [--segment-min 20]
        [--boot-every 2] [--source media|tone]

Writes OUT_DIR/segments.jsonl (one line per segment or cycle) and OUT_DIR/summary.json.
Exit 0 all pass, 2 some segment failed, 3 unsafe bench state (stopped), 4 reset loop.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import tomllib

root = Path.cwd()
sys.path[:0] = [str(root / 'firmware/rig'), str(root / 'rig/esp'), str(root / 'firmware/tools')]
import arrival  # noqa: E402
import cold_boot  # noqa: E402
import h2h5 as rig  # noqa: E402

ADB = r'C:\Users\user1\AppData\Local\Android\Sdk\platform-tools\adb.exe'
PIXEL = '3C081JEHN14930'
PKG = 'dev.larkbridge.harness'
WAV_SECONDS = 3.0
# The MP43247 runs at maximum volume: media must stay at or below -30 dBFS peak,
# i.e. a sine RMS of 32768 * 10**(-30/20) / sqrt(2) = 733 at the 32UE a2dp_out tap.
MAX_SPEAKER_RMS = 733


class Harness:
    """Pixel harness control over adb, with its LarkHarness logcat lines collected."""

    def __init__(self, out):
        self.lines = []
        self.proc = subprocess.Popen([ADB, '-s', PIXEL, 'logcat', '-T', '1', '-v', 'raw',
                                      '-s', 'LarkHarness:I'], stdout=subprocess.PIPE, text=True,
                                     encoding='utf-8', errors='replace')
        self.sink = (out / 'harness-logcat.jsonl').open('a', encoding='utf-8')
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            raw = raw.strip()
            if raw.startswith('{'):
                try:
                    self.lines.append(json.loads(raw))
                except ValueError:
                    continue
                self.sink.write(raw + '\n')
                self.sink.flush()

    def send(self, action, tag, *extra):
        subprocess.run([ADB, '-s', PIXEL, 'shell', 'am', 'broadcast', '-f', '0x20', '-n',
                        f'{PKG}/.ControlReceiver', '-a', f'{PKG}.{action}', '--es', 'tag', tag,
                        *extra], capture_output=True, text=True, timeout=20)

    def event(self, tag, name, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for line in list(self.lines):
                if line.get('tag') == tag and line.get('event') == name:
                    return line
            time.sleep(0.2)
        return None

    def close(self):
        self.proc.kill()
        self.sink.close()


def stats_pair(bench):
    pair = bench.pair('stats')
    pair['output'].setdefault('stats_period_ms', 1000)
    return pair


def start_source(bench, harness, source, seconds, tag):
    """Start the segment source; returns the source actually used."""
    if source == 'media' and harness is not None:
        for attempt in (1, 2):
            bench.command('s31', 'tone off')
            bench.command('s31', 'fault test_off')
            t = f'{tag}-{attempt}'
            harness.send('PLAY_STIMULUS', t, '--es', 'usage', 'media', '--ei', 'loops',
                         str(int(seconds / WAV_SECONDS) + 40), '--ei', 'progress_s', '60')
            if harness.event(t, 'stimulus_start', 10):
                try:
                    bench.wait_link(20, lambda pair: rig.streaming(bench.command('output', 'stats')))
                    bench.wait(2)
                    level = bench.command('output', 'tap a2dp_out')
                    loudest = max(level.get('rms_l', 0), level.get('rms_r', 0))
                    bench.add(t='level_check', rms=loudest, limit=MAX_SPEAKER_RMS)
                    if loudest <= MAX_SPEAKER_RMS:
                        return 'media'
                    harness.send('STOP_STIMULUS', f'{t}-loud')
                    bench.wait(1)
                    return start_tone(bench, f'media too loud (a2dp_out RMS {loudest})')
                except rig.BenchError:
                    pass
            harness.send('STOP_STIMULUS', f'{t}-stop')
            bench.wait(2)
    return start_tone(bench, None)


def start_tone(bench, why):
    bench.command('s31', 'fault test_a2dp')
    bench.command('s31', 'tone routed_i2s 1000 -50')
    bench.wait_link(30, lambda pair: rig.streaming(bench.command('output', 'stats')))
    return 'tone' if why is None else f'tone ({why})'


def segment(out, limits, harness, source, seconds, index, sink_hook=None):
    bench = rig.Bench(('COM3', 'COM4'), out, arrival._quiet_serial_factory,
                      log_name=f'segment{index:03}.jsonl')
    row = {'segment': index, 'requested': source}
    try:
        bench.wait(1)
        row['versions'] = {r: bench.command(r, 'status')['version'] for r in rig.ROLES}
        row['source'] = start_source(bench, harness, source, seconds, f's{index}')
        bench.wait(3)
        bench.command('output', 'fault fifo_window')
        first = len(bench.records)
        start = stats_pair(bench)
        bench.add(t='start', phase='stream', data=start)
        if sink_hook is None:
            bench.wait(seconds)
        else:
            # The hook owns the window: it must call bench.wait() for `seconds` in total and
            # return a JSON-able dict (e.g. the Pi fake-car recording verdict).
            row['sink'] = sink_hook(bench, seconds, out, index)
        end = stats_pair(bench)
        bench.add(t='end', phase='stream', data=end)
        verdict, metrics, reasons = rig.reduce('H5', dict(limits, stream_seconds=seconds - 1),
                                               bench.records[first:])
        row.update(h5=verdict, metrics=metrics, reasons=reasons)
        for role in rig.ROLES:
            u0, u1 = start[role]['uart'], end[role]['uart']
            row[role] = {k: u1[k] - u0[k] for k in ('timeouts', 'sessions', 'dec_err') if k in u1}
            row[role]['up_ms'] = end[role]['up_ms']
        row['s31']['live_rx'] = end['s31']['uart'].get('live_rx')
        row['s31']['rx_loop_max_ms'] = end['s31']['uart'].get('rx_loop_max_ms')
        row['s31']['uac_lost'] = end['s31'].get('uac', {}).get('lost')
        row['output']['live_tx'] = end['output']['uart'].get('live_tx')
        row['output']['live_write_max_ms'] = end['output']['uart'].get('live_write_max_ms')
    except rig.BenchError as exc:
        row['error'] = str(exc)
        bench.add(t='abort', reason=str(exc))
    finally:
        try:
            if harness is not None and row.get('source') == 'media':
                harness.send('STOP_STIMULUS', f's{index}-end')
            bench.command('s31', 'fault test_a2dp')
            bench.command('s31', 'tone routed_i2s 1000 -50')
        except Exception as exc:  # noqa: BLE001 - keep-alive best effort, never mask the result
            row['keepalive_error'] = f'{type(exc).__name__}: {exc}'
        bench.close()
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out', type=Path)
    ap.add_argument('--hours', type=float, default=7.5)
    ap.add_argument('--segment-min', type=float, default=20)
    ap.add_argument('--boot-every', type=int, default=2, help='segments between cold boots (0=never)')
    ap.add_argument('--source', choices=('media', 'tone'), default='media')
    ap.add_argument('--sink-hook', default=None,
                    help='module:function scoring an extra sink per segment (Pi fake car)')
    a = ap.parse_args()
    sink_hook = None
    if a.sink_hook:
        import importlib
        module, _, name = a.sink_hook.partition(':')
        sink_hook = getattr(importlib.import_module(module), name)
    seconds = a.segment_min * 60
    if a.boot_every and 3600 / (seconds * a.boot_every) > 4:
        sys.exit('more than 4 cold boots per hour: raise --boot-every or --segment-min')
    a.out.mkdir(parents=True, exist_ok=True)
    limits = tomllib.loads((root / 'firmware/qual/gates.toml').read_text())['gates']['H5']
    cold_boot.ports_now()
    harness = Harness(a.out) if a.source == 'media' else None
    log = (a.out / 'segments.jsonl').open('a', encoding='utf-8')
    rows, rc, boots = [], 0, 0
    deadline = time.monotonic() + a.hours * 3600
    index = 0
    try:
        while time.monotonic() + seconds < deadline:
            index += 1
            row = segment(a.out, limits, harness, a.source, seconds, index, sink_hook)
            row['wall'] = time.strftime('%Y-%m-%d %H:%M:%S')
            rows.append(row)
            log.write(json.dumps(row) + '\n')
            log.flush()
            print(json.dumps({k: row.get(k) for k in ('segment', 'wall', 'source', 'h5', 'error')}
                             | {'to': {r: row[r].get('timeouts') for r in rig.ROLES if r in row}}),
                  flush=True)
            if 'loop' in row.get('error', '') or 'identity' in row.get('error', ''):
                rc = 4 if 'loop' in row['error'] else 3
                break
            if row.get('h5') != 'PASS':
                rc = rc or 2
            if a.boot_every and index % a.boot_every == 0 and time.monotonic() + 300 < deadline:
                boots += 1
                try:
                    cyc = cold_boot.one_cycle(boots, a.out / 'coldboot', limits, 60, 1, True)
                except rig.BenchError as exc:
                    cyc = {'cycle': boots, 'error': str(exc)}
                except cold_boot.Stop as exc:
                    cyc = {'cycle': boots, 'stop': str(exc)}
                cyc['wall'] = time.strftime('%Y-%m-%d %H:%M:%S')
                rows.append({'coldboot': cyc})
                log.write(json.dumps({'coldboot': cyc}) + '\n')
                log.flush()
                print(json.dumps({'coldboot': {k: cyc.get(k) for k in
                                               ('cycle', 'marks_s', 'missing', 'h5', 'error', 'stop')}}),
                      flush=True)
                if 'stop' in cyc:
                    rc = 3
                    break
                if 'loop' in cyc.get('error', ''):
                    rc = 4
                    break
                if cyc.get('h5') != 'PASS':
                    rc = rc or 2
    finally:
        if harness is not None:
            harness.close()
        log.close()
        segs = [r for r in rows if 'segment' in r]
        cycs = [r['coldboot'] for r in rows if 'coldboot' in r]
        summary = {
            'segments': len(segs), 'segments_pass': sum(r.get('h5') == 'PASS' for r in segs),
            'media_segments': sum(r.get('source') == 'media' for r in segs),
            'stream_hours': round(sum(r.get('metrics', {}).get('seconds', 0) for r in segs) / 3600, 2),
            'hb_timeouts': {role: sum(r.get(role, {}).get('timeouts', 0) for r in segs)
                            for role in rig.ROLES},
            'cold_boots': len(cycs), 'cold_boots_pass': sum(c.get('h5') == 'PASS' for c in cycs),
            'cold_boot_timeouts': sum(c.get('s31', {}).get('timeouts_window', 0) for c in cycs),
            'rc': rc}
        (a.out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary), flush=True)
    sys.exit(rc)


if __name__ == '__main__':
    main()
