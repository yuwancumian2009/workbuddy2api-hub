"""Deterministic tests for the local window-based scheduling policy.

Local customization (see CUSTOMIZATIONS.md): upstream fires every task at fixed
wall-clock hours (09:00/21:00/22:00/01:00). This fork keeps the same work but
draws each task's fire time at random inside its own daily window, so the
upstream never sees the same minute twice and the check-in does not look like a
machine. These tests pin the policy down so an upstream rebase cannot silently
drop it back to fixed hours.

Every test drives the scheduler through ``prime(now)`` / ``_tick(now)`` with an
injected clock, so the results never depend on when the suite is run.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "ACCOUNTS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "_acc_sched"),
)
os.environ.setdefault(
    "USAGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_use_sched")
)

import wb_scheduler


def at(day, hour, minute, second=0):
    """A local struct_time on 2026-09-<day>, so the tests never depend on today."""
    return time.struct_time((2026, 9, day, hour, minute, second, 0, 0, -1))


def after(day, offset):
    """A clock a minute past ``offset`` seconds into 2026-09-<day>.

    The random target carries seconds, so a tick exactly on the target's minute
    would be up to 59s early and never fire.
    """
    offset = min(int(offset) + 60, 86399)
    return at(day, offset // 3600, (offset % 3600) // 60, offset % 60)


WINDOWS = {"checkin": [((7, 30), (10, 30))], "cat": [((0, 0), (6, 0))]}


class Probe(wb_scheduler.Scheduler):
    """Records which tasks were dispatched instead of calling the real one."""

    def __init__(self, *args, **kwargs):
        self.calls = []
        super().__init__(*args, **kwargs)

    def prime(self, now):
        """Re-seed the day's random targets as if the process had just started."""
        self._slots_date = None
        self._slots = {}
        self._fired = {}
        self.calls = []
        self._ensure_slots(now)
        self._calc_next_fire(now)
        return self

    def _execute_cycle(self, trigger_reason="周期巡检", tasks=None):
        self.calls.append(sorted(tasks or list(self.windows)))

    def dispatched(self):
        return sorted({task for call in self.calls for task in call})


class WindowTests(unittest.TestCase):
    def probe(self, now=None):
        probe = Probe(None, windows=WINDOWS, tick_seconds=30)
        return probe.prime(now or at(1, 6, 30))

    def test_every_upstream_task_has_a_window(self):
        """Dropping a task here would silently stop doing that work."""
        for task in ("checkin", "travel", "keepalive", "cat", "intl_chat"):
            self.assertIn(task, wb_scheduler.TASK_WINDOWS, task)
            self.assertIn(task, wb_scheduler.TASK_LABELS, task)
        self.assertNotIn("cat", wb_scheduler.CATCHUP_TASKS,
                         "the night task only counts 23:00-08:00 and must not catch up")

    def test_target_is_random_inside_the_window(self):
        targets = set()
        for _ in range(12):
            slot = self.probe()._slots["checkin#0"]
            self.assertGreaterEqual(slot, 7 * 3600 + 30 * 60)
            self.assertLessEqual(slot, 10 * 3600 + 30 * 60)
            targets.add(slot)
        self.assertGreater(len(targets), 1, "the window target is not random: %s" % targets)

    def test_a_window_fires_once_and_only_after_its_target(self):
        probe = self.probe()
        target = probe._slots["checkin#0"]
        self.assertEqual(probe._tick(at(1, 7, 0)), [], "fired before the window opened")
        self.assertEqual(probe._tick(after(1, target)), ["checkin#0"])
        for _ in range(3):
            self.assertEqual(probe._tick(at(1, 23, 0)), [], "fired twice in one day")
        self.assertEqual(probe.dispatched(), ["checkin"])

    def test_the_next_day_gets_a_fresh_random_target(self):
        probe = self.probe()
        probe._tick(after(1, probe._slots["checkin#0"]))
        self.assertEqual(probe.dispatched(), ["checkin"])
        probe._tick(at(2, 6, 30))
        self.assertEqual(probe._tick(after(2, probe._slots["checkin#0"])), ["checkin#0"])

    def test_a_missed_window_catches_up_only_for_catchup_tasks(self):
        probe = self.probe(at(1, 12, 0))
        # 12:00: both windows are over. checkin may catch up, cat may not.
        self.assertEqual(probe._fired.get("cat#0"), "2026-09-01")
        self.assertLessEqual(probe._slots["checkin#0"], 12 * 3600)
        self.assertEqual(probe._tick(at(1, 12, 0)), ["checkin#0"])
        for _ in range(3):
            self.assertEqual(probe._tick(at(1, 13, 0)), [])
        self.assertEqual(probe.dispatched(), ["checkin"])

    def test_a_late_start_lands_inside_what_is_left_of_the_window(self):
        slot = self.probe(at(1, 9, 0))._slots["checkin#0"]
        self.assertGreaterEqual(slot, 9 * 3600)
        self.assertLessEqual(slot, 10 * 3600 + 30 * 60)

    def test_disabled_scheduler_does_nothing(self):
        probe = self.probe(at(1, 9, 0))
        probe.enabled = False
        self.assertEqual(probe._tick(at(1, 9, 0)), [])
        self.assertEqual(probe.dispatched(), [])

    def test_status_reports_windows_and_pending_targets(self):
        status = self.probe().status()
        for key in ("enabled", "mode", "mode_cn", "mode_intl", "last_run_time",
                    "next_run_time", "logs"):
            self.assertIn(key, status, key)
        self.assertIn("窗口内随机", status["mode"])
        self.assertIn("每日签到", status["windows"])
        self.assertIn("07:30-10:30", status["windows"])
        self.assertIn("每日签到", status["today_targets"])
        for label, when in status["today_targets"].items():
            self.assertRegex(when, r"^\d\d:\d\d$", label)

    def test_manual_trigger_runs_every_task(self):
        probe = self.probe()
        result = probe.trigger_now()
        self.assertTrue(result.get("ok"), result)
        deadline = time.time() + 2.0
        while time.time() < deadline and len(probe.dispatched()) < 2:
            time.sleep(0.02)
        self.assertEqual(probe.dispatched(), ["cat", "checkin"])


if __name__ == "__main__":
    unittest.main()
