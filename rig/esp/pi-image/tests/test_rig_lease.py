"""rig-lease unit tests. Any Linux, temp paths only, no root."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import rig_lease  # noqa: E402
from rig_lease import EXIT_BUSY, EXIT_NOT_HOLDER, Env, Lease, LeaseError  # noqa: E402


class FakeClock:
    def __init__(self):
        self.wall = 1_800_000_000.0
        self.boot = 1000.0
        self.boot_id = "boot-a"
        self.dead: set[int] = set()
        self.tokens = iter(f"tok{i}" for i in range(1000))

    def env(self) -> Env:
        return Env(
            wall=lambda: self.wall,
            boottime=lambda: self.boot,
            boot_id=lambda: self.boot_id,
            host=lambda: "rig",
            pid_alive=lambda pid: pid not in self.dead,
            sleep=self.advance,
            new_token=lambda: next(self.tokens),
        )

    def advance(self, seconds):
        self.wall += seconds
        self.boot += seconds


class LeaseLogicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rig.lease")
        self.clock = FakeClock()
        self.lease = Lease(self.path, self.clock.env())

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_of_missing_file_is_free(self):
        self.assertEqual(self.lease.status()["state"], "free")
        self.assertFalse(self.lease.status()["live"])

    def test_acquire_then_status_is_held(self):
        out = self.lease.acquire("agent-a", ttl=60)
        self.assertEqual(out["action"], "acquired")
        status = self.lease.status()
        self.assertTrue(status["live"])
        self.assertEqual(status["lease"]["owner"], "agent-a")
        self.assertEqual(status["remaining_s"], 60.0)

    def test_second_owner_is_busy(self):
        self.lease.acquire("agent-a", ttl=60)
        with self.assertRaises(LeaseError) as ctx:
            self.lease.acquire("agent-b", ttl=60)
        self.assertEqual(ctx.exception.code, EXIT_BUSY)
        self.assertEqual(ctx.exception.payload["lease"]["owner"], "agent-a")

    def test_same_owner_renews_and_keeps_token(self):
        first = self.lease.acquire("agent-a", ttl=60)
        self.clock.advance(30)
        second = self.lease.acquire("agent-a", ttl=60)
        self.assertEqual(second["action"], "renewed")
        self.assertEqual(second["lease"]["token"], first["lease"]["token"])
        self.assertEqual(self.lease.status()["remaining_s"], 60.0)

    def test_expired_lease_is_taken_over_and_recorded(self):
        self.lease.acquire("agent-a", ttl=60)
        self.clock.advance(61)
        out = self.lease.acquire("agent-b", ttl=60)
        self.assertEqual(out["action"], "took-over")
        self.assertEqual(out["lease"]["previous"], {"owner": "agent-a", "reason": "expired"})

    def test_wall_clock_step_does_not_expire_lease(self):
        self.lease.acquire("agent-a", ttl=60)
        self.clock.wall += 10 * 3600  # NTP sync on an RTC-less Pi
        with self.assertRaises(LeaseError):
            self.lease.acquire("agent-b")

    def test_other_boot_is_stale(self):
        self.lease.acquire("agent-a", ttl=3600)
        self.clock.boot_id = "boot-b"
        self.clock.boot = 5.0
        out = self.lease.acquire("agent-b")
        self.assertEqual(out["lease"]["previous"]["reason"], "rebooted")

    def test_dead_local_pid_is_stale(self):
        self.lease.acquire("agent-a", ttl=3600, pid=4242)
        self.clock.dead.add(4242)
        out = self.lease.acquire("agent-b")
        self.assertEqual(out["lease"]["previous"]["reason"], "holder-exited")

    def test_live_pid_keeps_lease(self):
        self.lease.acquire("agent-a", ttl=3600, pid=4242)
        with self.assertRaises(LeaseError):
            self.lease.acquire("agent-b")

    def test_corrupt_file_is_taken_over(self):
        Path(self.path).write_text("{not json")
        self.assertEqual(self.lease.status()["state"], "corrupt")
        out = self.lease.acquire("agent-b")
        self.assertEqual(out["lease"]["previous"], {"owner": None, "reason": "corrupt"})

    def test_wait_succeeds_when_lease_expires(self):
        self.lease.acquire("agent-a", ttl=5)
        out = self.lease.acquire("agent-b", wait=10)
        self.assertEqual(out["action"], "took-over")

    def test_wait_times_out_while_held(self):
        self.lease.acquire("agent-a", ttl=100)
        with self.assertRaises(LeaseError) as ctx:
            self.lease.acquire("agent-b", wait=3)
        self.assertEqual(ctx.exception.code, EXIT_BUSY)

    def test_release_by_holder_frees(self):
        self.lease.acquire("agent-a")
        self.assertEqual(self.lease.release("agent-a")["action"], "released")
        self.assertEqual(self.lease.status()["state"], "free")

    def test_release_by_other_is_refused(self):
        self.lease.acquire("agent-a")
        with self.assertRaises(LeaseError) as ctx:
            self.lease.release("agent-b")
        self.assertEqual(ctx.exception.code, EXIT_NOT_HOLDER)
        self.assertTrue(self.lease.status()["live"])

    def test_force_release(self):
        self.lease.acquire("agent-a")
        out = self.lease.release("agent-b", force=True)
        self.assertTrue(out["forced"])
        self.assertFalse(self.lease.status()["live"])

    def test_release_of_others_dead_lease_leaves_record(self):
        self.lease.acquire("agent-a", ttl=1)
        self.clock.advance(2)
        self.assertEqual(self.lease.release("agent-b")["action"], "already-free")
        self.assertEqual(self.lease.status()["lease"]["owner"], "agent-a")

    def test_check(self):
        self.lease.acquire("agent-a")
        self.assertTrue(self.lease.check("agent-a")["holder_matches"])
        self.assertFalse(self.lease.check("agent-b")["holder_matches"])

    def test_bad_arguments(self):
        for kwargs in ({"owner": " "}, {"owner": "a", "ttl": 0},
                       {"owner": "a", "ttl": rig_lease.MAX_TTL + 1}):
            with self.assertRaises(LeaseError):
                self.lease.acquire(**kwargs)


class LeaseCliTests(unittest.TestCase):
    """The real CLI with the real clock, boot id and flock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rig.lease")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        proc = subprocess.run(
            [sys.executable, str(HERE / "rig_lease.py"), "--path", self.path, *args],
            capture_output=True, text=True, timeout=30)
        return proc.returncode, json.loads(proc.stdout)

    def test_exit_codes(self):
        self.assertEqual(self.run_cli("acquire", "--owner", "a", "--ttl", "60")[0], 0)
        code, out = self.run_cli("acquire", "--owner", "b")
        self.assertEqual((code, out["error"]), (EXIT_BUSY, "busy"))
        self.assertEqual(self.run_cli("check", "--owner", "b")[0], EXIT_NOT_HOLDER)
        self.assertEqual(self.run_cli("check", "--owner", "a")[0], 0)
        self.assertEqual(self.run_cli("release", "--owner", "b")[0], EXIT_NOT_HOLDER)
        self.assertEqual(self.run_cli("release", "--owner", "a")[0], 0)
        code, out = self.run_cli("status")
        self.assertEqual((code, out["state"]), (0, "free"))

    def test_dead_pid_takeover_with_real_process(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.run_cli("acquire", "--owner", "a", "--pid", str(child.pid))
        code, out = self.run_cli("acquire", "--owner", "b")
        self.assertEqual(code, 0)
        self.assertEqual(out["lease"]["previous"]["reason"], "holder-exited")

    def test_race_has_exactly_one_winner(self):
        script = HERE / "rig_lease.py"
        procs = [subprocess.Popen(
            [sys.executable, str(script), "--path", self.path,
             "acquire", "--owner", f"agent-{i}", "--ttl", "60"],
            stdout=subprocess.PIPE, text=True) for i in range(8)]
        codes = [p.wait(timeout=30) for p in procs]
        for p in procs:
            p.stdout.close()
        self.assertEqual(sorted(codes), [0] + [EXIT_BUSY] * 7)


if __name__ == "__main__":
    unittest.main()
