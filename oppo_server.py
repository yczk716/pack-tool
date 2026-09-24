# -*- coding: utf-8 -*-
"""OPPO 批量验证服务端模块（部署在云服务器 /opt/pack-tool/，由 pack_tool.py 挂载）。

登录：Xvfb(:99) + x11vnc(5900) + noVNC(6080) + headed chromium。
用户在浏览器打开 :6080/vnc.html 即可看到服务器上的真实浏览器画面并直接操作，
完成 OPPO 人工登录（滑块/验证码全支持），登录态自动导出 oppo_state.json。

批量：gen_package -> build_apk -> judge_one（纯接口），组内 5 并发，进度全局可轮询。
"""
import csv
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# systemd 服务环境没有 DISPLAY，登录浏览器必须落在 browser-vnc 的 Xvnc :99 上
os.environ.setdefault("DISPLAY", ":99")

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "oppo_state.json")
REPORT_FILE = os.path.join(HERE, "oppo_report.csv")
GROUP_SIZE = 5

# 复用服务器已有的 browser-vnc 套件（systemd 服务）：
#   browser-vnc-xvnc        Xvnc :99 (1920x1080, rfb 5900, VncAuth 密码 /etc/browser-vnc/rfbauth)
#   browser-vnc-openbox     Openbox 窗口管理器（:99）
#   browser-vnc-session     D-Bus + fcitx5 中文输入（:99）
#   browser-vnc-websockify  6080 -> localhost:5900（TLS，https 访问）
VNC_DISPLAY = ":99"
NOVNC_PORT = 6080
VNC_SERVICES = ["browser-vnc-xvnc.service", "browser-vnc-openbox.service",
                "browser-vnc-session.service", "browser-vnc-websockify.service"]

CREATE_URL = "https://open.oppomobile.com/new/mcom/appList/appCreate"

# ---------------- vivo 重名判定（可选模块） ----------------
try:
    import vivo_api
    vivo_api.configure(log_fn=lambda m: _log("[vivo] " + str(m)))
    VIVO_ENABLED = True
except Exception:
    vivo_api = None
    VIVO_ENABLED = False


def _vivo_check(name, package=None):
    """vivo 平台判名包装：模块未部署/未配置 cookie 时返回 none。"""
    if not VIVO_ENABLED:
        return {"vivo_status": "none", "vivo_msg": "vivo 模块未部署"}
    try:
        return vivo_api.check_vivo_name(name, package)
    except Exception as e:
        return {"vivo_status": "unknown", "vivo_msg": "vivo 判定异常: " + str(e)[:80]}

_lock = threading.Lock()
_state = {
    "logged_in": False,       # oppo_state.json 存在且会话探测有效
    "has_cookie": False,      # oppo_state.json 文件存在
    "vnc_running": False,
    "login_running": False,   # 登录浏览器线程活着
    "login_msg": "",
}
class JobCtx(object):
    """单个浏览器会话（pt_sid cookie）的批量任务上下文。

    不同的人各自拥有独立进度/结果/日志/报告，互不可见、互不干扰；
    登录态与探针应用仍是全局共享的。
    """

    def __init__(self, sid):
        self.sid = sid
        self.data = {"running": False, "total": 0, "done": 0, "auto_delete": False,
                     "name_only": False, "apk_files": [],
                     "results": [], "logs": []}
        self.report_file = os.path.join(HERE, "oppo_report_%s.csv" % sid[:12])

    def log(self, msg):
        line = time.strftime("[%H:%M:%S] ") + str(msg)
        with _lock:
            logs = self.data["logs"]
            logs.append(line)
            if len(logs) > 600:
                del logs[:-400]
        if _cfg["log"]:
            try:
                _cfg["log"](line)
            except Exception:
                pass

    def set_result(self, row):
        with _lock:
            for i, r in enumerate(self.data["results"]):
                if r["name"] == row["name"]:
                    self.data["results"][i] = row
                    break
            else:
                self.data["results"].append(row)


_jobs = {}                 # sid -> JobCtx
_jobs_guard = threading.Lock()


def _job_for(sid):
    sid = (sid or "anon").strip()[:64] or "anon"
    with _jobs_guard:
        jc = _jobs.get(sid)
        if jc is None:
            jc = _jobs[sid] = JobCtx(sid)
        # 简单防膨胀：会话数超 200 时清掉所有空闲会话
        if len(_jobs) > 200:
            for k in [k for k, v in _jobs.items() if not v.data["running"]][:-100]:
                _jobs.pop(k, None)
        return jc
_cfg = {"build_apk": None, "gen_package": None, "log": None}


def configure(build_apk_fn, gen_pkg_fn, log_fn=None):
    _cfg["build_apk"] = build_apk_fn
    _cfg["gen_package"] = gen_pkg_fn
    _cfg["log"] = log_fn
    # 服务启动即探活一次：cookie 若仍有效，用户打开页面时徽章直接就是绿的
    def _boot_probe():
        time.sleep(1.0)
        try:
            ensure_login_state()
        except Exception:
            pass
    threading.Thread(target=_boot_probe, daemon=True).start()


def _log(msg):
    """全局系统日志（登录/探针等公共事件），进服务器 stdout，不进任何会话的页面日志。"""
    line = time.strftime("[%H:%M:%S] ") + str(msg)
    if _cfg["log"]:
        try:
            _cfg["log"](line)
        except Exception:
            pass


def _port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- 登录

def start_vnc_stack():
    """确认 browser-vnc 套件在跑（幂等：已 active 则什么都不做）。"""
    try:
        import subprocess
        for svc in VNC_SERVICES:
            r = subprocess.run(["systemctl", "is-active", "--quiet", svc])
            if r.returncode != 0:
                subprocess.run(["sudo", "systemctl", "start", svc], timeout=60)
                _log(f"已启动 {svc}")
    except Exception as e:
        _log("检查 browser-vnc 服务失败: " + str(e)[:120])
    vnc_ok = _port_open(NOVNC_PORT)
    with _lock:
        _state["vnc_running"] = vnc_ok
    return vnc_ok


def _login_worker():
    global _state
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=["--window-position=0,0", "--window-size=1900,1060",
                      "--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"])
            ctx = browser.new_context(viewport={"width": 1880, "height": 1040})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(30000)
            page.goto(CREATE_URL, wait_until="domcontentloaded")
            _log("登录浏览器已打开：请在 noVNC 窗口内完成 OPPO 登录（含滑块验证）")
            with _lock:
                _state["login_msg"] = "等待登录…（在 noVNC 窗口内操作）"
            deadline = time.time() + 30 * 60
            confirmed = 0
            while time.time() < deadline:
                time.sleep(3)
                try:
                    cookies = ctx.cookies()
                except Exception:
                    continue
                hit = any(c["name"] == "sdkLoginToken" and c["value"] for c in cookies)
                if not hit:
                    continue
                try:
                    ctx.storage_state(path=STATE_FILE)
                except Exception as e:
                    _log("导出 cookie 失败: " + str(e)[:100])
                    continue
                with _lock:
                    _state["has_cookie"] = True
                if _probe_session():
                    with _lock:
                        _state["logged_in"] = True
                        _state["login_msg"] = "登录成功，cookie 已保存，可开始批量验证"
                    _log("登录态有效，已保存 " + os.path.basename(STATE_FILE))
                    break
                confirmed += 1
                if confirmed >= 3:
                    with _lock:
                        _state["has_cookie"] = True
                        _state["login_msg"] = "已保存 cookie 但会话探测未通过，请点'我已登录完成'重试"
                    _log("cookie 已保存但会话探测未通过")
                    break
            else:
                with _lock:
                    _state["login_msg"] = "等待登录超时（30 分钟），可重新点击登录"
            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        with _lock:
            _state["login_msg"] = "登录浏览器异常: " + str(e)[:150]
        _log("登录浏览器异常: " + str(e)[:150])
    finally:
        with _lock:
            _state["login_running"] = False


def _probe_state_file(path):
    """探测指定 storage_state 文件的会话有效性（gensign，无副作用）。"""
    try:
        from oppo_api import OppoApi
        api = OppoApi(storage_state=path, log=lambda m: None)
        return bool(api.gensign())
    except Exception as e:
        _log("会话探测失败: " + str(e)[:100])
        return False


def _probe_session():
    """用已存 cookie 调 gensign 探测会话有效性（无副作用）。

    探头选型教训：
    - checkPkgName / first_publish 不校验登录态，cookie 过期也"成功"，不能用；
    - app/list 用 {"page","page_size","fuzzy"} 调用必返 300001（参数不对，误报失效），不能用；
    - gensign.json 校验登录态：有效返回 sign，失效 errno=800003，且无副作用 —— 用它。
    """
    return _probe_state_file(STATE_FILE)


def start_login():
    vnc_ok = start_vnc_stack()
    if not vnc_ok:
        with _lock:
            _state["login_msg"] = "noVNC 未启动成功（检查 xvfb/x11vnc/websockify 是否安装）"
        return _state_snapshot()
    with _lock:
        if _state.get("login_running"):
            return _state_snapshot()
        _state["login_running"] = True
        _state["login_msg"] = "正在启动登录浏览器…"
    threading.Thread(target=_login_worker, daemon=True).start()
    return _state_snapshot()


def check_login():
    """手动触发：重新探测 cookie 会话。"""
    if not os.path.exists(STATE_FILE):
        with _lock:
            _state["has_cookie"] = False
            _state["logged_in"] = False
            _state["login_msg"] = "还没有登录记录，请先点击'打开登录窗口'"
        return _state_snapshot()
    with _lock:
        _state["has_cookie"] = True
    ok = _probe_session()
    if not ok:
        # gensign 偶发瞬时 800003（实测同一会话稍后重探即恢复），失败后二次确认防误报
        time.sleep(1.5)
        ok = _probe_session()
    with _lock:
        _state["logged_in"] = ok
        _state["login_msg"] = ("会话有效，可以直接开始批量验证" if ok
                               else "cookie 存在但已失效，请重新登录")
    return _state_snapshot()


def save_cookie_state(state):
    """保存前端上传的登录文件（storage_state.json 原文 dict）。

    防覆盖校验分两级：
    ① 数量过少或缺关键 token 直接拒绝（未登录浏览器的近空 cookie）；
    ② 候选 cookie 先落临时文件做 gensign 预校验——候选失效且服务器当前有效时拒绝覆盖，
       防止"从另一个未登录浏览器回传，冲掉服务器上原本可用的登录态"。
    """
    if not isinstance(state, dict) or not state.get("cookies"):
        raise ValueError("文件格式不对：应为此前导出的 storage_state.json（含 cookies 字段）")
    cookies = [c for c in state["cookies"] if isinstance(c, dict)]
    names = {c.get("name", "") for c in cookies}
    if len(cookies) < 5 or "sdkLoginToken" not in names:
        raise ValueError("上传的 cookie 仅 %d 条（需 ≥5 且含 sdkLoginToken），"
                         "疑似未登录状态，已拒绝保存（防止覆盖服务器上的有效登录态）" % len(cookies))
    tmp = STATE_FILE + ".candidate"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    cand_ok = _probe_state_file(tmp)
    if not cand_ok:
        # gensign 偶发瞬时 800003，失败后二次确认防误报
        time.sleep(1.5)
        cand_ok = _probe_state_file(tmp)
    if not cand_ok:
        try:
            os.remove(tmp)
        except OSError:
            pass
        # 关键：若候选与服务器当前是同一个会话（sdkLoginToken 相同），
        # 候选预校验失败即说明当前会话已死，必须同步刷新缓存状态，
        # 否则页面徽章会一直显示旧的"已登录"（用户需手动点验证才刷新）。
        try:
            cur = json.load(open(STATE_FILE, encoding="utf-8")) if os.path.exists(STATE_FILE) else {}
        except Exception:
            cur = {}
        cur_token = next((c.get("value") for c in cur.get("cookies", []) if isinstance(c, dict) and c.get("name") == "sdkLoginToken"), None)
        cand_token = next((c.get("value") for c in cookies if c.get("name") == "sdkLoginToken"), None)
        if cur_token is not None and cur_token == cand_token:
            with _lock:
                _state["logged_in"] = False
                _state["login_msg"] = "会话已失效（回传校验未通过），请重新登录"
            raise ValueError("回传的登录态校验未通过（与服务器当前为同一会话，已同步标记失效），请重新登录 OPPO")
        raise ValueError("回传的登录态校验未通过，已拒绝保存（不影响服务器当前登录态），请重新登录 OPPO 后再回传")
    os.replace(tmp, STATE_FILE)
    _log("已保存上传的登录文件（cookie " + str(len(cookies)) + " 条，预校验通过）")


def save_pasted_cookies(text):
    """解析用户从 DevTools 复制的 cookie 请求头/cookie 文本，存为登录态。

    支持格式：单行 "k1=v1; k2=v2"（Network 面板 cookie 请求头），或每行一条。
    会话需要全部 oppomobile cookie（含 HttpOnly 的 OPPOSID/dev_id/openplat/opkey），
    因此必须从 DevTools Network 的请求头复制，document.cookie 拿不全。
    """
    if not text or not text.strip():
        raise ValueError("粘贴内容为空")
    pairs = []
    for chunk in text.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        k, v = k.strip(), v.strip()
        if k:
            pairs.append((k, v))
    if not pairs:
        raise ValueError("未解析到任何 cookie（应为 k=v; k2=v2 形式）")
    # 登录页专用 token 必须保留原 path 限定：若发到业务接口会干扰鉴权
    # （实证：sdkLoginToken 以 path=/ 发给 first_publish 时报 300001 登录失效）
    LOGIN_PATH_TOKENS = {"sdkLoginToken", "firstLoginTokenForTT"}
    cookies = []
    for k, v in pairs:
        if k in LOGIN_PATH_TOKENS:
            cookies.append(dict(name=k, value=v, domain="open.oppomobile.com",
                                path="/public/login"))
        else:
            cookies.append(dict(name=k, value=v, domain=".oppomobile.com", path="/"))
    state = {"cookies": cookies, "origins": []}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    _log("已保存粘贴的 cookie（" + str(len(cookies)) + " 条）")


_probe_gate = threading.Lock()
_probe_ts = [0.0]  # 上次自动探活时间戳
PROBE_RETRY_INTERVAL = 30  # 秒：自动探活最小间隔，避免每次轮询都打接口


def ensure_login_state():
    """cookie 文件存在但 logged_in=False 时，后台自动探活一次会话。

    避免"会话其实仍有效、徽章却显示未登录"误导用户重新登录。
    在 state_snapshot() 里被调用；用独立 _probe_gate 防止重复调度，
    探活在 daemon 线程执行，绝不阻塞 /api/oppo/state 请求。
    """
    if not os.path.exists(STATE_FILE):
        return
    now = time.time()
    with _probe_gate:
        if now - _probe_ts[0] < PROBE_RETRY_INTERVAL:
            return
        _probe_ts[0] = now
    with _lock:
        if _state["logged_in"] or _state["login_running"]:
            return
    def _bg():
        try:
            check_login()
        except Exception as e:
            _log("自动探活异常: " + str(e)[:120])
    threading.Thread(target=_bg, daemon=True).start()


def save_vivo_cookie(text):
    if not VIVO_ENABLED:
        raise RuntimeError("vivo 模块未部署")
    vivo_api.save_pasted_cookie(text)
    return {"ok": True, "has_vivo_cookie": vivo_api.has_cookie()}


def vivo_cookie_present():
    return bool(VIVO_ENABLED and vivo_api.has_cookie())


def state_snapshot():
    _state["has_cookie"] = os.path.exists(STATE_FILE)
    try:
        ensure_login_state()
    except Exception:
        pass
    # vivo 状态独立于 OPPO 登录态：接口可用即 True（实测判定不需要 vivo 登录）
    if VIVO_ENABLED:
        try:
            _state["vivo_ok"] = bool(vivo_api.probe_ok())
        except Exception:
            _state["vivo_ok"] = False
    else:
        _state["vivo_ok"] = None
    return dict(_state)  # GIL 下 dict 拷贝原子；严禁在持有 _lock 时再调加锁快照（不可重入死锁）


def _state_snapshot():
    return dict(_state)


# ---------------------------------------------------------------- 批量

def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _build_one(name, pkg, jc):
    jc.log(f"[{name}] 开始打包（包名 {pkg}）")
    # 注意：set_result 内部自带 _lock，此处严禁再包 with _lock（不可重入死锁）
    jc.set_result(dict(name=name, package=pkg, app_id="", status="building",
                       duplicate=None, message="打包中", deleted=False))
    out = _cfg["build_apk"](name, pkg)
    # build_apk 返回 dict(ok, output, log)；兼容直接返回路径字符串
    if isinstance(out, dict):
        if not out.get("ok") or not out.get("output"):
            tail = "".join(map(str, (out.get("log") or [])[-3:]))[-150:]
            raise RuntimeError("打包失败: " + tail)
        out = out["output"]
    jc.log(f"[{name}] 打包完成 {os.path.basename(str(out))}")
    with _lock:
        jc.data["apk_files"].append(os.path.basename(str(out)))
    return out


def start_batch(names, auto_delete=False, name_only=False, sid=""):
    if not os.path.exists(STATE_FILE):
        return {"ok": False, "need_login": True, "error": "尚未登录：请先完成 OPPO 登录再开始批量验证"}
    if not _probe_session():
        # gensign 偶发瞬时 800003，1.5s 后二次确认防误报
        time.sleep(1.5)
        if not _probe_session():
            with _lock:
                _state["logged_in"] = False
                _state["login_msg"] = "cookie 已失效（验证前预检未通过），请重新登录"
            return {"ok": False, "need_login": True, "error": "cookie 已失效：请重新登录后再试"}
    else:
        with _lock:
            _state["logged_in"] = True
    names = [n for n in names if n]
    if not name_only and not auto_delete and len(names) > 5:
        return {"ok": False,
                "error": "未勾选自动删除时单次最多验证 5 个（避免账号残留未发布应用）；"
                         "如需更多请勾选'验证完成后自动删除'，将按 5 个一批自动删除并继续"}
    if not names:
        return {"ok": False, "error": "名称列表为空"}
    jc = _job_for(sid)
    with _lock:
        if jc.data["running"]:
            return {"ok": False, "error": "你已有一个任务在跑，请等它结束"}
        jc.data.update(running=True, total=len(names), done=0, results=[], logs=[],
                       auto_delete=bool(auto_delete), name_only=bool(name_only),
                       apk_files=[])
        for n in names:
            jc.data["results"].append(dict(name=n, package="", app_id="", status="queued",
                                           duplicate=None, message="排队中", deleted=False))
    mode = _batch_worker_nameonly if name_only else _batch_worker
    threading.Thread(target=mode, args=(list(names), jc), daemon=True).start()
    return {"ok": True}


def _mark_all_failed(names, msg, jc):
    """把未完成的名称标记为 failed（已完成的跳过，避免 done 重复计数）。"""
    jc.log(msg)
    with _lock:
        done_names = {r["name"] for r in jc.data["results"] if r["status"] in ("done", "failed")}
    for n in names:
        if n in done_names:
            continue
        jc.set_result(dict(name=n, package="", app_id="", status="failed",
                           duplicate=None, message=msg, deleted=False))
        with _lock:
            jc.data["done"] += 1


def _batch_worker_nameonly(names, jc):
    """仅名称判定：用常驻探针应用 app_id 逐名调 app/appname，不打包不传包。

    实证（2026-09-20）：appname 不要求应用已传包解析，空应用即可判定
    data.app_name: 1=重复 0=可用。探针应用持久化在 probe_app.json，跨批次复用
    （首次自动创建；这也绕开了账号创建频控对判名的影响）。
    """
    from oppo_api import OppoApi, judge_name_only
    api = OppoApi(storage_state=STATE_FILE, log=lambda m: None)
    try:
        jc.log("===== 仅名称判定模式（不打包/不传包）=====")
        probe_id = _get_probe(api, jc)
        if not probe_id:
            _mark_all_failed(names, "探针应用不可用（自动创建失败），请稍后重试或检查登录态", jc)
            return
        for n in names:
            jc.set_result(dict(name=n, package="", app_id="", status="checking",
                               duplicate=None, message="名称判定中", deleted=False))
            r = judge_name_only(api, n, probe_id,
                                log=lambda m, nn=n: jc.log(f"[{nn}] {m}"))
            v = _vivo_check(n)
            if v["vivo_status"] != "none":
                jc.log(f"[{n}] vivo: {v['vivo_status']} {v['vivo_msg'][:60]}")
            jc.set_result(dict(name=r["name"], package="", app_id="",
                               status="done" if r.get("ok") else "failed",
                               duplicate=r.get("duplicate"),
                               message=r.get("message", ""), deleted=False,
                               vivo=v["vivo_status"], vivo_msg=v["vivo_msg"]))
            with _lock:
                jc.data["done"] += 1
            time.sleep(0.4)  # 轻微间隔，避免连续调用触发限流
        _write_report(jc)
        jc.log("全部完成，报告已生成")
    except Exception as e:
        jc.log("仅名称判定任务异常终止: " + str(e)[:200])
        _mark_all_failed(names, "任务异常: " + str(e)[:100], jc)
    finally:
        with _lock:
            jc.data["running"] = False


PROBE_FILE = os.path.join(os.path.dirname(STATE_FILE), "probe_app.json")


def _load_probe_id():
    try:
        with open(PROBE_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("app_id")
    except Exception:
        return None


def _save_probe_id(app_id):
    with open(PROBE_FILE, "w", encoding="utf-8") as f:
        json.dump({"app_id": app_id}, f)


def _get_probe(api, jc=None):
    """取常驻探针应用 id：优先复用已持久化的；失效/不存在则创建并持久化。"""
    lg = jc.log if jc is not None else _log
    old = _load_probe_id()
    if old:
        try:
            # 探活：任意名称调一次 appname，能返回即说明探针可用
            flag, _d = api.check_appname(old, "探针探活勿提")
            if flag in (0, 1):
                lg("复用常驻探针应用 app_id=" + str(old))
                return old
        except Exception as e:
            lg("探针探活失败(" + str(e)[:80] + ")，尝试重建")
    pkg = "com.probe" + str(int(time.time()))[-6:] + "a"
    app_id, d = api.create_app("重名探针勿审", pkg)
    if not app_id:
        lg("探针应用创建失败: " + ((d.get("message") or "")[:100]))
        return None
    _save_probe_id(app_id)
    lg("已创建常驻探针应用 app_id=" + str(app_id))
    return app_id


def _batch_worker(names, jc):
    from oppo_api import OppoApi, judge_one
    api = OppoApi(storage_state=STATE_FILE, log=lambda m: None)
    try:
        for gi, group in enumerate(_chunks(names, GROUP_SIZE), 1):
            jc.log(f"===== 第 {gi} 组：{'、'.join(group)} =====")
            pkgs = {n: _cfg["gen_package"](n) for n in group}
            apks = {}
            with ThreadPoolExecutor(max_workers=GROUP_SIZE) as ex:
                futs = {ex.submit(_build_one, n, pkgs[n], jc): n for n in group}
                for fu in as_completed(futs):
                    n = futs[fu]
                    try:
                        apks[n] = fu.result()
                    except Exception as e:
                        jc.log(f"[{n}] 打包失败: {str(e)[:150]}")
                        jc.set_result(dict(name=n, package=pkgs[n], app_id="", status="failed",
                                           duplicate=None, message="打包失败: " + str(e)[:100]))
                        with _lock:
                            jc.data["done"] += 1
            with ThreadPoolExecutor(max_workers=GROUP_SIZE) as ex:
                futs = {}
                for n in group:
                    if n not in apks:
                        continue
                    jc.set_result(dict(name=n, package=pkgs[n], app_id="", status="checking",
                                       duplicate=None, message="创建+传包验证中", deleted=False))
                    futs[ex.submit(judge_one, api, n, pkgs[n], apks[n],
                                   (lambda m, nn=n: jc.log(f"[{nn}] {m}")))] = n
                for fu in as_completed(futs):
                    n = futs[fu]
                    try:
                        r = fu.result()
                        deleted = False
                        msg = r.get("message", "")
                        # auto_delete 开启时，凡创建了应用（有 app_id）一律删除，
                        # 避免判定失败/传包失败留下孤儿应用
                        if jc.data.get("auto_delete") and r.get("app_id"):
                            try:
                                dok, d = api.delete_app(r["app_id"])
                                deleted = dok
                                jc.log(f"[{n}] 删除应用({r['app_id']}): "
                                       + ("已删除" if dok else "失败 " + (d.get("message") or "")[:60]))
                                if dok:
                                    msg = (msg + "（应用已删除）")[:200]
                            except Exception as e:
                                jc.log(f"[{n}] 删除异常: {str(e)[:100]}")
                        v = _vivo_check(r["name"], r["package"])
                        jc.set_result(dict(name=r["name"], package=r["package"],
                                           app_id=r.get("app_id") or "",
                                           status="done" if r.get("ok") else "failed",
                                           duplicate=r.get("duplicate"),
                                           message=msg,
                                           deleted=deleted,
                                           vivo=v["vivo_status"], vivo_msg=v["vivo_msg"]))
                    except Exception as e:
                        jc.log(f"[{n}] 验证异常: {str(e)[:150]}")
                        jc.set_result(dict(name=n, package=pkgs[n], app_id="", status="failed",
                                           duplicate=None, message="接口异常: " + str(e)[:100],
                                           deleted=False))
                    with _lock:
                        jc.data["done"] += 1
        _write_report(jc)
        jc.log("全部完成，报告已生成")
    except Exception as e:
        jc.log("批量任务异常终止: " + str(e)[:200])
    finally:
        with _lock:
            jc.data["running"] = False


def _write_report(jc):
    with _lock:
        rows = [dict(r) for r in jc.data["results"]]
    try:
        with open(jc.report_file, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["name", "package", "app_id", "status",
                                              "duplicate", "deleted", "message",
                                              "vivo", "vivo_msg"])
            w.writeheader()
            w.writerows(rows)
    except Exception as e:
        jc.log("写报告失败: " + str(e)[:120])


def progress(sid=None):
    try:
        ensure_login_state()  # 页面 2.5s 轮询此接口；未登录时借轮询节流复检，保证徽章尽快反映真实状态
    except Exception:
        pass
    jc = _job_for(sid)
    vnc = dict(_state)  # state 无锁读（避免嵌套加锁）
    with _lock:
        return {
            "vnc": vnc,
            "running": jc.data["running"],
            "total": jc.data["total"],
            "done": jc.data["done"],
            "name_only": jc.data.get("name_only", False),
            "apk_files": list(jc.data.get("apk_files", [])),
            "results": [dict(r) for r in jc.data["results"]],
            "logs": list(jc.data["logs"][-200:]),
        }


def read_report(sid=None):
    jc = _job_for(sid)
    if not os.path.exists(jc.report_file):
        return None
    with open(jc.report_file, "rb") as f:
        return f.read()
