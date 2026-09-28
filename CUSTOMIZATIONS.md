# 本地定制说明（本 fork 相对于上游的改动）

上游：<https://github.com/ardeyouxipianyi/workbuddy2api-hub>
本 fork：`nas` 分支 = 上游某个 release tag + 下面的定制提交。

**目标**：上游更新时，只需 `git rebase` + 跑测试 + push，NAS 上 `docker compose pull` 即可，
不需要再在 NAS 上编译镜像。

---

## 一、定制清单

定制只有 **2 个文件**，各自独立一个提交，其余文件与上游逐字节一致。

| 提交 | 文件 | 内容 |
|---|---|---|
| `feat(scheduler): …window…` | `wb_scheduler.py` | 排程从"固定整点"改为"窗口内随机" |
| `fix(accounts): …settled…` | `wb_accounts.py` | "今天已签到"当作当天终态处理，落盘且不计成功 |
| `test: …` | `tests/_test_nas_*.py` | 上述两项的回归测试（CI 会跑） |
| `ci: …` | `.github/workflows/image.yml` | 自动构建并推送镜像到 GHCR |
| `chore(deploy): …` | `deploy/`, `scripts/` | NAS 拉取式部署与上游同步脚本 |

### 1. 窗口内随机排程（`wb_scheduler.py`）

上游：每个任务固定整点触发 —— `09:00/21:00` 签到与猫咪旅行、`22:00` 保活、`01:00` 夜猫。

本 fork：每个任务有自己的窗口，实际触发时刻在窗口内**随机**（含秒），因此每天不同、
不会落在整点整分，避免形成机械化的固定打卡指纹。

```python
TASK_WINDOWS = {
    "checkin":   [((7, 30), (10, 30))],
    "travel":    [((7, 30), (10, 30)), ((19, 0), (22, 30))],   # 早晚各一次，维持"派出→领奖"闭环
    "keepalive": [((21, 0), (23, 30))],
    "cat":       [((0, 0), (6, 0))],
    "intl_chat": [((7, 30), (10, 30))],
}
```

配套行为：

* **每窗口每天只触发一次**（`_fired` 按窗口键记当天日期）；
* **错过窗口**：`CATCHUP_TASKS`（签到/旅行/保活/国际活跃）在进程启动晚于窗口时**立即补跑一次**；
  夜猫子不在其中（它只在 23:00-08:00 计数，补跑无意义，任务内部也会自检时段）；
* **窗口进行中启动**：在"当前时刻 → 窗口结束"之间随机，不会立刻触发；
* `status()` 额外返回 `today_targets`（今天各任务的随机时刻）与 `windows`（窗口摘要），看板直接展示。

改窗口只改 `TASK_WINDOWS` 即可（值为 `(时, 分)`，可配多个窗口）。

### 2. "今天已签到"是终态（`wb_accounts.py`）

上游对重复签到会回 **4xx + `code 10001`**（或 `200` + 文案"今天已签到，请明天再来"），
而 `lastCheckin` 只在 200 分支里落盘 —— 于是 4xx 这条路径上：

1. 当天签到**从不落盘** → `can_checkin()` 一直为 `True`；
2. 于是每个调度窗口、每次容器重启都会**再发一次重复请求**，而且被记成"签到失败"。

本 fork 的修法：

* `_is_already_checked_in(code, msg)` 识别这个终态（`10001` 或文案含"已签到"/"明天再来"）；
* `_mark_checked_in()` 把当天日期落盘（保证跨重启不再重复请求）；
* `checkin()` 返回 `{"ok": False, "already_checked_in": True, "msg": "今天已签到，请明天再来"}` ——
  **不是失败，也不算签到成功**，面板与调度器都按上游原文展示；
* 真正的失败（如 500）**不落盘**，当天仍可重试；
* 登录完成、导入桌面凭证这两处"自动补签到"也加了 `can_checkin()` 前置判断。

> 上游 v1.6.4 仍然是旧行为（`wb_accounts.py` 的 `checkin()` 未改），
> 所以这不是过时的补丁，而是上游尚未修的真问题。

---

## 二、更新上游版本（日常操作）

```bash
cd <本仓库>
./scripts/sync-upstream.sh            # 默认 rebase 到上游最新 release tag
./scripts/sync-upstream.sh v1.6.5     # 或指定版本
```

脚本会：拉取上游 → `git rebase` 到目标版本 → 冲突时停下并提示改哪些文件 →
跑 `python tests/run_all.py` → 打印后续步骤。

冲突几乎只会出现在这两个文件上，用 `CUSTOMIZATIONS.md`（本文）里的语义对齐即可：
**上游新增的任务 → 在 `TASK_WINDOWS`/`TASK_LABELS` 加一项并在 `_run_cycle` 里加 `do_xxx` 分支**。

改完推送（`nas` 分支或 `nas-*` tag）→ GitHub Actions 自动跑测试 + 构建镜像 → NAS 上拉取。

---

## 三、NAS 拉取式部署

```bash
cd /vol1/1000/docker/wbapi-hub
./update.sh            # 内部就是 docker compose pull && docker compose up -d
```

`deploy/docker-compose.yml` 用 `image:` 而不是 `build:`：
NAS 不再编译，只下载现成镜像（`ghcr.io/yuwancumian2009/workbuddy2api-hub:TAG`）。

升级 = 改 `TAG` 这一处 + `./update.sh`；回滚 = 改回旧 `TAG` + `./update.sh`。

---

## 四、验证

```bash
python tests/run_all.py                       # 全套（含本 fork 的两个定制用例）
python tests/run_all.py nas                   # 只跑定制用例
```

定制用例：

* `tests/_test_nas_scheduler_windows.py` —— 窗口随机、每天一次、补跑策略、状态字段；
* `tests/_test_nas_checkin_already.py` —— 重复签到的两种响应形态、落盘、真失败不落盘、
  调度器第二轮**不再发请求**。

---

## 五、部署现状（迁移记录）

| 项 | 值 |
|---|---|
| NAS 上跑的版本 | `nas-v1.6.4-nas1`（2026-09-28 由 v1.5.4 本地编译版切过来） |
| 切换前镜像（回滚用） | `wb-local-build:20260926-before-pull`（本地编译产物，仍在 NAS 上） |
| 拉取式 compose | `docker-compose.pull.yml`（与旧的 `docker-compose.yml` 并存，互不干扰） |
| 部署后复验 | `./verify.sh` |

回滚：

```bash
docker compose -f docker-compose.yml up -d   # 旧 compose，image 已在本地，不会重新编译
```

### 上游 v1.6.x 带来的行为变化（不是本 fork 的改动）

1. **LAN 模式默认开启 API Key 校验**：`Dockerfile` 的 `CMD` 带 `--lan`，首次运行会生成
   `launcher_key` 写进 `accounts/settings.json`，并把它当作 `/v1` 的 API Key。
   v1.5.4 时 `/health` 的 `api_key_required` 为 `false`，现在为 `true` —— 不带 key 直接调
   `/v1/*` 的客户端会收到 401。要恢复旧的免鉴权行为：在面板里关掉鉴权（写
   `settings.json` 的 `auth_disabled: true`）。
2. `/health` 返回字段变少（不再含 `uid`/`domain`/`issuer`/`credential_file`/`expires_at`）。

排查时注意：`wb.o.hhxin.top` 是**另一个实例**（不是 NAS 这台），两边 `/health` 字段不同，别混淆。

