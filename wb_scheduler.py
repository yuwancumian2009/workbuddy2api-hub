"""wb_scheduler.py —— 后台定时调度器 (Scheduler)

负责常驻后台自动执行：
1. Token 保活 (Keepalive)：定期检查 Token 剩余寿命，不足 2 小时自动调用 Refresh Token。
2. 每日签到 (Daily Checkin)：每日为所有国内版账号自动签到领积分。
3. 猫猫旅行与日常结算 (Cat Travel & Welfare)：自动派出猫猫旅行或领取归来奖励。
4. 国际版每日活跃对话 (Daily Chat)：为国际版账号领取每日活跃积分。
5. 状态持久化与看板展示：暴露状态、执行记录、支持手动立即触发与开关切换。

排程方式：窗口内随机（本地定制，见 CUSTOMIZATIONS.md）
------------------------------------------------------------------
每个任务每天有一个或多个时间窗口，实际触发时刻在窗口内随机生成，
因此每天不同、且不会落在整点整分，避免形成机械化的固定打卡指纹。
窗口结束前保证执行一次；若进程启动时窗口已过且当天尚未执行，
仅对"全天任意时刻都有效"的任务（签到/旅行/保活/国际活跃）做一次补跑，
夜猫子任务只在 23:00-08:00 有计数意义，错过窗口不补跑（任务内部也会自检时段）。
时区跟随容器 TZ（默认 Asia/Shanghai）。
"""
import random
import threading
import time

import wb_tasks
from wb_tasks import do_cat_travel

# ---- LOCAL CUSTOMIZATION (窗口内随机排程) --------------------------------
# task -> [(起, 止), ...]，值为 (时, 分)。列出的每个窗口每天各触发一次。
TASK_WINDOWS = {
    "checkin":   [((7, 30), (10, 30))],
    # 猫猫旅行是状态驱动的（idle 派出 / arrived 领奖），早晚各一个窗口才能维持
    # "早上派出、晚上领奖"的闭环；只留一个窗口会让派出与领奖隔天交替。
    "travel":    [((7, 30), (10, 30)), ((19, 0), (22, 30))],
    "keepalive": [((21, 0), (23, 30))],
    "cat":       [((0, 0), (6, 0))],
    "intl_chat": [((7, 30), (10, 30))],
}
TASK_LABELS = {
    "checkin": "每日签到",
    "travel": "猫猫旅行",
    "keepalive": "Token保活",
    "cat": "夜猫子任务",
    "intl_chat": "国际活跃",
}
# 错过窗口后允许当日补跑的任务（夜猫子任务有时段意义，不补跑）
CATCHUP_TASKS = {"checkin", "travel", "keepalive", "intl_chat"}
# 巡检心跳间隔（秒）
TICK_SECONDS = 30


def _to_secs(hour, minute):
    return int(hour) * 3600 + int(minute) * 60


def _fmt_hhmm(secs):
    secs = int(secs) % 86400
    return "%02d:%02d" % (secs // 3600, (secs % 3600) // 60)
# ---- END LOCAL CUSTOMIZATION ---------------------------------------------


class Scheduler:
    def __init__(self, pool, windows=None, tick_seconds=None):
        self.pool = pool
        self.windows = windows if windows is not None else TASK_WINDOWS
        self.tick_seconds = int(tick_seconds or TICK_SECONDS)
        self.enabled = True
        self._stop_event = threading.Event()
        self._thread = None
        self.last_run_time = None
        self.next_run_time = None
        self.logs = []
        # Guards against overlapping runs: trigger_now() spawns a thread per
        # click, and a manual trigger can also land on top of the scheduled job.
        self._run_lock = threading.Lock()
        # 今日每个窗口的随机目标时刻 / 已执行日期
        self._slots = {}        # slot_key -> 目标秒数
        self._fired = {}        # slot_key -> 已执行日期 "YYYY-MM-DD"
        self._slots_date = None
        self._ensure_slots()
        self._calc_next_fire()
        # Surface task-level failures (dead endpoints, upstream shape changes)
        # in the same log the panel shows.
        wb_tasks.set_logger(self.log)

    def log(self, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.logs.append(entry)
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]
        # 同步打到容器 stdout：面板日志之外，`docker logs wb-proxy` 也能直接看到排程活动
        try:
            print(f"[调度器] {msg}", flush=True)
        except Exception:
            pass
        try:
            import wb_proxy
            wb_proxy.add_log_entry(f"[调度器] {msg}", tag="scheduler")
        except Exception:
            pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.log("后台定时调度器已启动（窗口内随机排程）")

    def stop(self):
        self._stop_event.set()
        self.log("后台定时调度器已暂停")

    def _run_loop(self):
        # 启动后先休眠 10 秒等待主服务就绪，然后做一次 Token 保活巡检
        time.sleep(10)
        try:
            self._execute_cycle("启动初次巡检（Token 保活与凭证核对）", ["keepalive"])
        except Exception as exc:
            self.log(f"初次巡检异常: {exc}")

        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                self.log(f"排程循环异常: {exc}")
            self._stop_event.wait(self.tick_seconds)

    # ---------------- LOCAL CUSTOMIZATION: 排程计算 ----------------

    def _tick(self, now=None):
        """单次心跳：生成/命中窗口并执行到期任务，返回本次命中的窗口键。

        ``now`` 可注入（测试用），缺省取当前本地时间。
        """
        if not self.enabled:
            return []
        now = now or time.localtime()
        self._ensure_slots(now)
        due = self._pending(now)
        if not due:
            self._calc_next_fire(now)
            return []
        tasks = []
        for key in due:
            task = key.split("#", 1)[0]
            if task not in tasks:
                tasks.append(task)
            # 先落标记再执行：执行较慢或失败也不会在同一窗口重复触发
            self._fired[key] = self._slots_date
        detail = "、".join(
            "%s %s" % (TASK_LABELS.get(k.split("#", 1)[0], k), _fmt_hhmm(self._slots[k]))
            for k in due
        )
        self._execute_cycle(
            "窗口随机命中 (%s，当前 %02d:%02d)" % (detail, now.tm_hour, now.tm_min),
            tasks,
        )
        self._calc_next_fire(now)
        return due

    def _ensure_slots(self, now=None):
        """为新的一天/新窗口生成随机目标时刻；处理错过窗口的补跑与放弃。"""
        now = now or time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        if self._slots_date != today:
            self._slots_date = today
            self._slots = {}
            self._fired = {}
        now_s = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
        for task, win_list in self.windows.items():
            label = TASK_LABELS.get(task, task)
            for idx, (start, end) in enumerate(win_list):
                key = "%s#%d" % (task, idx)
                if key in self._slots or self._fired.get(key) == today:
                    continue
                lo, hi = _to_secs(*start), _to_secs(*end)
                if hi < lo:      # 跨零点窗口（当前无，留作扩展）
                    hi += 86400
                if now_s <= lo:
                    # 还没到窗口：整个窗口内随机
                    self._slots[key] = random.randint(lo, hi)
                elif now_s <= hi:
                    # 窗口进行中（多为进程启动较晚）：剩余窗口内随机
                    self._slots[key] = random.randint(min(now_s, hi), hi)
                elif task in CATCHUP_TASKS:
                    self._slots[key] = now_s
                    self.log(f"「{label}」今日窗口 {_fmt_hhmm(lo)}-{_fmt_hhmm(hi)} 已过且未执行，立即补跑一次")
                else:
                    self._fired[key] = today
                    self.log(f"「{label}」今日窗口 {_fmt_hhmm(lo)}-{_fmt_hhmm(hi)} 已过，跳过（该任务不补跑）")

    def _pending(self, now=None):
        """返回当前到点且今日未执行的窗口键。"""
        now = now or time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        now_s = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
        return [
            key
            for key, target in sorted(self._slots.items(), key=lambda kv: kv[1])
            if self._fired.get(key) != today and now_s >= target
        ]

    def _calc_next_fire(self, now=None):
        now = now or time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        now_s = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
        pending = [
            target
            for key, target in self._slots.items()
            if self._fired.get(key) != today and target >= now_s
        ]
        if pending:
            self.next_run_time = "%s %s 前后（窗口内随机）" % (today, _fmt_hhmm(min(pending)))
            return
        first = min(_to_secs(*(win_list[0][0])) for win_list in self.windows.values() if win_list)
        self.next_run_time = "明日 %s 起窗口内随机" % _fmt_hhmm(first)

    def _windows_summary(self):
        parts = []
        for task, win_list in self.windows.items():
            label = TASK_LABELS.get(task, task)
            spans = "/".join(
                "%s-%s" % (_fmt_hhmm(_to_secs(*s)), _fmt_hhmm(_to_secs(*e))) for s, e in win_list
            )
            parts.append("%s %s" % (label, spans))
        return " · ".join(parts)

    # ---------------- END LOCAL CUSTOMIZATION ----------------

    def trigger_now(self):
        """手动立即触发一次调度检查。"""
        if self._run_lock.locked():
            return {"ok": False, "msg": "已有巡检正在执行，请稍候再试"}
        threading.Thread(
            target=self._execute_cycle,
            args=("手动立即触发", list(self.windows)),
            daemon=True,
        ).start()
        return {"ok": True, "msg": "已触发后台调度执行"}

    def _execute_cycle(self, trigger_reason="周期巡检", tasks=None):
        if not self._run_lock.acquire(blocking=False):
            self.log(f"跳过本次巡检 ({trigger_reason})：上一轮仍在执行")
            return
        try:
            self._run_cycle(trigger_reason, tasks if tasks is not None else list(self.windows))
        finally:
            self._run_lock.release()

    def _run_cycle(self, trigger_reason="周期巡检", tasks=None):
        if tasks is None:
            tasks = list(self.windows)
        self.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log(f"开始执行任务 ({trigger_reason})...")
        if not self.pool or not self.pool.accounts:
            self.log("暂无可用的活跃账号，跳过本次巡检")
            return

        do_keepalive = "keepalive" in tasks
        do_checkin = "checkin" in tasks
        do_travel = "travel" in tasks
        do_cat = "cat" in tasks
        do_intl_chat = "intl_chat" in tasks

        refreshed_count = 0
        checkin_count = 0
        travel_count = 0
        daily_chat_count = 0

        for acc in list(self.pool.accounts):
            uid8 = acc.uid[:8] if acc.uid else "?"
            # 1. 检查 Token 剩余寿命 (小于 2 小时自动刷新保活)
            if do_keepalive:
                exp = acc.expires_at or 0
                if exp and (exp - time.time()) < 7200:
                    self.log(f"账号 [{uid8}] Token 即将到期，执行主动保活刷新...")
                    if acc.refresh():
                        refreshed_count += 1
                        self.log(f"✓ 账号 [{uid8}] Token 自动保活刷新成功")
                    else:
                        self.log(f"! 账号 [{uid8}] Token 保活刷新失败: {acc.last_error}")

            # 2. 如果是国内版账号，检查每日签到与猫猫旅行
            if acc.realm == "cn":
                if do_checkin:
                    # can_checkin() 以落盘的 lastCheckin 为准：当天已签到就不再
                    # 发请求，这是"避免自动重复签到"的第一道闸。
                    if not acc.can_checkin():
                        self.log(f"· 账号 [{uid8}] 今日已签到，本窗口跳过（未发出请求）")
                    else:
                        self.log(f"检测到国内版账号 [{uid8}] 今日尚未签到，执行自动签到...")
                        res = acc.checkin()
                        if res.get("already_checked_in"):
                            # 上游判定"今天已签到"：不是签到成功，也不算失败
                            self.log(f"· 账号 [{uid8}] {res.get('msg') or '今天已签到，请明天再来'}")
                        elif res.get("ok"):
                            checkin_count += 1
                            self.log(f"✓ 账号 [{uid8}] 自动签到成功: {res.get('msg')}")
                        else:
                            self.log(f"! 账号 [{uid8}] 自动签到未成功: {res.get('error') or res.get('msg')}")
                        time.sleep(1.0)

                # 检查猫猫旅行 (状态驱动: idle 派出 / arrived 领奖)
                if do_travel:
                    tr = do_cat_travel(acc)
                    if tr.get("action") in ("claim", "depart"):
                        travel_count += 1
                        self.log(f"🐱 账号 [{uid8}] 猫猫日常处理: {tr.get('msg')}")
                    time.sleep(1.0)

                # 夜猫子专属任务: black_cat 只在 23:00-08:00 上报计数，
                # 由 00:00-06:00 的窗口随机触发一次；任务内部也会自检时段。
                if do_cat:
                    night = wb_tasks.run_night_growth(acc)
                    for line in night.get("logs", []):
                        self.log(f"🌙 {line}")
                    time.sleep(1.0)

            # 3. 如果是国际版账号，检查每日活跃对话 (送 30/50 积分福利)
            if acc.realm == "intl" and do_intl_chat:
                if acc.can_daily_chat():
                    self.log(f"检测到国际版账号 [{uid8}] 今日尚未活跃，执行每日活跃打卡对话...")
                    res = acc.daily_chat()
                    if res.get("ok"):
                        daily_chat_count += 1
                        self.log(f"✓ 账号 [{uid8}] 每日活跃对话成功")
                    else:
                        self.log(f"! 账号 [{uid8}] 每日活跃对话失败: {res.get('error') or res.get('msg')}")
                    time.sleep(1.5)

        self.log(f"巡检完成：Token保活 {refreshed_count} 个，国内签到 {checkin_count} 个，猫猫日常 {travel_count} 个，国际活跃 {daily_chat_count} 个")

    def status(self):
        summary = self._windows_summary()
        today = time.strftime("%Y-%m-%d")
        targets = {
            TASK_LABELS.get(key.split("#", 1)[0], key): _fmt_hhmm(target)
            for key, target in sorted(self._slots.items(), key=lambda kv: kv[1])
            if self._fired.get(key) != today
        }
        return {
            "enabled": self.enabled,
            "mode": f"窗口内随机排程 ({summary})",
            "mode_cn": f"窗口内随机排程 ({summary})",
            "mode_intl": "账号 Token 自动保活与凭证常驻 (每日 21:00-23:30 窗口内随机巡检)",
            "last_run_time": self.last_run_time or "尚未运行",
            "next_run_time": self.next_run_time or "待调度",
            "today_targets": targets,
            "windows": summary,
            "logs": self.logs[-20:],
        }
