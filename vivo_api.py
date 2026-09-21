# -*- coding: utf-8 -*-
"""vivo 开放平台重名判定（逆向实证 2026-09-21）。

接口：GET https://dev.vivo.com.cn/webapi/app/verify-app-cn-name
      params: mainTitle=<应用名>&packageName=<包名>
来源：appCreate 页面 store action app/verifyAppName
      → ajax.get(VERIFY_APP_NAME, {params:{mainTitle, packageName}})
页面语义：code===0 → 名称可用；code!==0 → 表单报错 errorText=msg
实测：20219 = 已存在同名（msg 带 vivo 工单同名申诉链接）
登录态：**实测不需要**（2026-09-21：无 cookie/垃圾 cookie 调本接口，
      微信/淘宝仍正确返回 20219 占用）——cookie 仅在有值时附带，用于可能的
      自有应用豁免等个性化语义；没有也照常判定，vivo 列永不受登录影响。
"""
import json
import os
import random
import re as _re
import string

import requests

VIVO_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vivo_state.json")
VERIFY_URL = "https://dev.vivo.com.cn/webapi/app/verify-app-cn-name"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_log = lambda m: None


def configure(log_fn=None):
    global _log
    _log = log_fn or _log


def load_cookie():
    try:
        with open(VIVO_STATE_FILE, encoding="utf-8") as f:
            return (json.load(f).get("cookie") or "").strip()
    except Exception:
        return ""


def has_cookie():
    return bool(load_cookie())


def save_pasted_cookie(text):
    """解析粘贴的 cookie（k=v; k2=v2，分号/换行分隔均可）并存盘。"""
    if not text or "=" not in str(text):
        raise ValueError("内容不像 cookie：应包含 k=v 形式，例如 JSESSIONID=xxx; b_account_token=yyy")
    parts = []
    for line in str(text).replace("\n", ";").split(";"):
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip():
                parts.append(k.strip() + "=" + v.strip())
    if not parts:
        raise ValueError("未解析到任何 cookie 键值对")
    with open(VIVO_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"cookie": "; ".join(parts)}, f, ensure_ascii=False)
    _log("已保存 vivo cookie（%d 项）" % len(parts))


_probe_cache = {"ts": 0.0, "ok": False}


def probe_ok(force=False):
    """探测 vivo 判名接口可用性（60s 缓存）。返回 True/False。

    探针：用任意名 + 随机包名调一次判定；HTTP 200 且 code in (0,20219) 即可用。
    """
    import time
    now = time.time()
    if not force and now - _probe_cache["ts"] < 60:
        return _probe_cache["ok"]
    ok = False
    try:
        resp = requests.get(
            VERIFY_URL,
            params={"mainTitle": "vivo判名探针勿提", "packageName": _random_pkg()},
            headers={
                "user-agent": UA,
                "referer": "https://dev.vivo.com.cn/appCreate",
                "accept": "application/json, text/plain, */*",
            },
            timeout=10,
        )
        code = resp.json().get("code")
        ok = (resp.status_code == 200 and code in (0, 20219))
    except Exception:
        ok = False
    _probe_cache["ts"] = time.time()
    _probe_cache["ok"] = ok
    return ok


def _random_pkg():
    return "com.vivocheck." + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


def check_vivo_name(name, package=None):
    """判定 vivo 平台应用名占用（无副作用，不创建任何东西）。

    返回 dict(vivo_status, vivo_msg)：
      ok      → 名称可用
      dup     → 名称被占用
      unknown → 无法判定（异常/非 20219 的错误码，msg 带原文）
      none    → 未配置 vivo cookie
    """
    if not probe_ok():
        return {"vivo_status": "none", "vivo_msg": "vivo 接口不可达，已跳过"}
    cookie = load_cookie()  # 可选：有则附带（兼容个性化语义），没有也照常判定
    pkg = (package or "").strip() or _random_pkg()
    try:
        headers = {
            "user-agent": UA,
            "referer": "https://dev.vivo.com.cn/appCreate",
            "x-requested-with": "XMLHttpRequest",
            "accept": "application/json, text/plain, */*",
        }
        if cookie:
            headers["cookie"] = cookie
        resp = requests.get(
            VERIFY_URL,
            params={"mainTitle": name, "packageName": pkg},
            headers=headers,
            timeout=15,
        )
        data = resp.json()
    except Exception as e:
        return {"vivo_status": "unknown", "vivo_msg": "请求异常: " + str(e)[:80]}
    code = data.get("code")
    msg = str(data.get("msg") or "")
    if code == 0:
        return {"vivo_status": "ok", "vivo_msg": "可用"}
    if code == 20219:
        clean = _re.sub(r"<[^>]+>", "", msg)
        return {"vivo_status": "dup", "vivo_msg": (clean[:120] or "名称已被占用")}
    if code in (401, 403) or "登录" in msg:
        return {"vivo_status": "unknown", "vivo_msg": "vivo 返回登录相关错误（code=%s %s）" % (code, msg[:60])}
    return {"vivo_status": "unknown", "vivo_msg": "code=%s %s" % (code, msg[:80])}
