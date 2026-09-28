"""Deterministic tests for the local "already checked in today" handling.

Local customization (see CUSTOMIZATIONS.md). Upstream answers a duplicate daily
check-in with 4xx + code 10001 (occasionally 200 + "今天已签到，请明天再来"),
which is a settled day, not an error and not a fresh success:

* ``can_checkin()`` reads the credential file, so the day MUST be written back,
  otherwise every scheduler window and every container restart sends another
  duplicate request and reports it as a failure.
* the result must still not be counted or logged as a successful check-in.

No network: ``wb_accounts.urlopen`` is replaced with a recorder.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "ACCOUNTS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "_acc_checkin"),
)
os.environ.setdefault(
    "USAGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_use_checkin")
)

import wb_accounts
import wb_scheduler

ALREADY = {"code": 10001, "msg": "今天已签到，请明天再来"}


class Response(object):
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def read(self, *_args):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = list(accounts)


class CheckinTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nas-checkin-")
        self.calls = []
        original = wb_accounts.urlopen
        self.addCleanup(setattr, wb_accounts, "urlopen", original)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def account(self, realm="cn"):
        """A CN account whose credential file lives in the temp directory."""
        path = os.path.join(self.dir, "u-cn-1.json")
        return wb_accounts.Account(
            {"uid": "u-cn-1", "realm": realm, "accessToken": "dummy", "lastCheckin": None},
            path=path,
        )

    def intercept(self, result):
        """Answer every request with ``result``: a payload, or (status, payload)."""

        def fake(request, timeout=30, proxy=""):
            self.calls.append(request)
            if isinstance(result, tuple):
                status, payload = result
                raise urllib.error.HTTPError(
                    request.full_url, status, "error", {},
                    io.BytesIO(json.dumps(payload).encode("utf-8")),
                )
            return Response(result)

        wb_accounts.urlopen = fake

    def stored(self):
        path = os.path.join(self.dir, "u-cn-1.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    # ---- the two upstream shapes of "today is already settled" ----

    def test_duplicate_as_http_200_is_settled_not_successful(self):
        account = self.account()
        self.intercept(ALREADY)
        result = account.checkin()
        self.assertFalse(result.get("ok"), result)
        self.assertTrue(result.get("already_checked_in"), result)
        self.assertIn("已签到", result.get("msg", ""))
        self.assertFalse(account.can_checkin(), "a duplicate must close the day")
        self.assertTrue(self.stored().get("lastCheckin", "").startswith(
            time.strftime("%Y-%m-%d")), self.stored())

    def test_duplicate_as_4xx_is_settled_and_does_not_retry(self):
        """The shape the CN billing endpoint actually returns."""
        account = self.account()
        self.intercept((400, ALREADY))
        result = account.checkin()
        self.assertFalse(result.get("ok"), result)
        self.assertTrue(result.get("already_checked_in"), result)
        self.assertEqual(len(self.calls), 1, "a 4xx must not be retried")
        self.assertFalse(account.can_checkin())
        self.assertTrue(self.stored().get("lastCheckin", "").startswith(
            time.strftime("%Y-%m-%d")), self.stored())

    def test_the_settled_day_survives_a_restart(self):
        account = self.account()
        self.intercept((400, ALREADY))
        account.checkin()
        reloaded = wb_accounts.Account(self.stored())
        self.assertFalse(reloaded.can_checkin(),
                         "a restarted process must not check in again")

    # ---- real success and real failure keep their old meaning ----

    def test_a_real_checkin_is_reported_as_a_success(self):
        account = self.account()
        self.intercept({"code": 0, "msg": "OK"})
        result = account.checkin()
        self.assertTrue(result.get("ok"), result)
        self.assertNotIn("already_checked_in", result)
        self.assertTrue(self.stored().get("lastCheckin"))

    def test_a_real_failure_leaves_the_day_open_for_a_retry(self):
        account = self.account()
        self.intercept((400, {"code": 500, "msg": "server busy"}))
        result = account.checkin()
        self.assertFalse(result.get("ok"), result)
        self.assertNotIn("already_checked_in", result)
        self.assertFalse(self.stored().get("lastCheckin"), "a failure is not a settled day")
        self.assertTrue(account.can_checkin(), "the same day may still be retried")

    def test_message_only_detection(self):
        self.assertTrue(wb_accounts.Account._is_already_checked_in(-1, "今天已签到，请明天再来"))
        self.assertTrue(wb_accounts.Account._is_already_checked_in("10001", ""))
        self.assertFalse(wb_accounts.Account._is_already_checked_in(0, "OK"))
        self.assertFalse(wb_accounts.Account._is_already_checked_in(-1, ""))

    # ---- the scheduler must not send a duplicate the second time round ----

    def test_the_scheduler_sends_no_duplicate_request(self):
        account = self.account()
        self.intercept((400, ALREADY))
        scheduler = wb_scheduler.Scheduler(FakePool([account]), tick_seconds=30)
        scheduler._run_cycle("第一次窗口", ["checkin"])
        self.assertEqual(len(self.calls), 1)
        scheduler._run_cycle("第二次窗口", ["checkin"])
        self.assertEqual(len(self.calls), 1, "the second window re-sent the request")
        joined = "\n".join(scheduler.logs)
        self.assertIn("今日已签到，本窗口跳过", joined)
        self.assertNotIn("自动签到成功", joined)
        self.assertIn("国内签到 0 个", joined)


if __name__ == "__main__":
    unittest.main()
