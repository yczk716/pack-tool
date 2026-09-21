# -*- coding: utf-8 -*-
"""OPPO 开放平台纯接口客户端（无浏览器，浏览器只用于人工登录导出 cookie）。
链路（全部为 2026-09-18 抓包实证）:
  1) POST /oresource/officialapi/v1/app/checkPkgName   包名预检（无副作用）
  2) POST /oresource/officialapi/v1/app/first_publish  创建应用 -> app_id
  3) POST /resource/publish/gensign.json               上传签名（空 body）
  4) POST https://api.open.oppomobile.com/api/utility/upload   multipart 传包 -> apk_url
  5) POST /oresource/officialapi/v1/app/verify-task-add        提交解析任务 -> task_id
  6) POST /oresource/officialapi/v1/app/verify-info            轮询（911209=处理中）
登录态: storage_state.json（oppo_bot.py 登录后导出）。
"""
import hashlib
import json
import os
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))

# 分类：一级下拉第一项 -> 二级下拉第一项（2026-09-18 抓包值；站点分类数据更新时需重新抓）
SECOND_CATEGORY_ID = 74
THIRD_CATEGORY_ID = 6654

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

VERIFY_INFO_OK = 0          # 解析成功
VERIFY_INFO_BUSY = 911209   # APK 解析任务处理中

SESSION_EXPIRED_CODE = 300001  # 登录信息已失效


class SessionExpiredError(RuntimeError):
    """cookie 失效（code=300001），需重新登录/上传登录文件。"""


class OppoApi:
    BASE = "https://open.oppomobile.com"
    V1 = BASE + "/oresource/officialapi/v1"

    def __init__(self, storage_state=None, log=None):
        storage_state = storage_state or os.path.join(HERE, "storage_state.json")
        self.log = log or (lambda m: None)
        self.s = requests.Session()
        self.s.headers.update({
            "user-agent": UA,
            "accept": "application/json, text/plain, */*",
            "origin": self.BASE,
            "accept-language": "zh-CN,zh;q=0.9",
        })
        state = json.load(open(storage_state, encoding="utf-8"))
        n = 0
        for c in state.get("cookies", []):
            try:
                self.s.cookies.set(c["name"], c["value"],
                                   domain=c.get("domain", ""), path=c.get("path", "/"))
                n += 1
            except Exception:
                pass
        self.log(f"[api] 已加载 {n} 条 cookie")

    # ---------- 内部
    def _post_json(self, url, payload, referer):
        r = self.s.post(url, json=payload, timeout=30,
                        headers={"referer": referer, "content-type": "application/json"})
        r.raise_for_status()
        d = r.json()
        # 注意：OPPO 把 300001 复用于多种错误（登录失效/record not found/...)，
        # 只有 message 含"登录"才是会话失效，其余原样抛出避免误判
        if d.get("code") == SESSION_EXPIRED_CODE:
            omsg = (d.get("message") or "")
            if "登录" in omsg:
                raise SessionExpiredError(
                    "登录已失效，请重新粘贴 cookie 或上传登录文件")
            raise RuntimeError(f"接口错误 code=300001: {omsg[:100]}")
        return d

    @staticmethod
    def _upload_referer(app_id):
        return (OppoApi.BASE + "/frontendframe/appService/generalApp"
                f"?app_id={app_id}&pkg_symbol=0&type=add&promise=singleResource")

    # ---------- 1) 包名预检（无副作用，可用于批量前过滤）
    def check_pkg(self, pkg_name):
        """返回 (available, raw)。available=True 表示包名未被占用。"""
        d = self._post_json(self.V1 + "/app/checkPkgName", {"pkg_name": pkg_name},
                            self.BASE + "/new/mcom/appList/appCreate")
        data = d.get("data") or {}
        return d.get("code") == 0 and data.get("pkg_name") == 0, d

    # ---------- 2) 创建应用
    def create_app(self, app_name, pkg_name):
        payload = {"app_name": app_name, "pkg_name": pkg_name, "appSignExts": "",
                   "second_category_id": SECOND_CATEGORY_ID,
                   "third_category_id": THIRD_CATEGORY_ID}
        d = self._post_json(self.V1 + "/app/first_publish", payload,
                            self.BASE + "/new/mcom/appList/appCreate")
        if d.get("code") == 0 and (d.get("data") or {}).get("app_id"):
            return str(d["data"]["app_id"]), d
        return None, d

    # ---------- 3) 上传签名
    def gensign(self):
        r = self.s.post(self.BASE + "/resource/publish/gensign.json", data=b"",
                        timeout=30, headers={"referer": self.BASE + "/"})
        r.raise_for_status()
        d = r.json()
        # 正常: {"errno":0,"data":["sign值"]}；异常: {"errno":800003,"data":{"message":...}}
        if d.get("errno") != 0:
            data = d.get("data")
            msg = data.get("message", "")[:80] if isinstance(data, dict) else ""
            raise RuntimeError(f"获取上传签名失败 errno={d.get('errno')} {msg}".strip())
        data = d.get("data")
        if isinstance(data, list):
            return str(data[0]) if data else ""
        if isinstance(data, dict):
            return str(data.get("sign") or "")
        return ""

    # ---------- 4) 传包
    def upload_apk(self, apk_path):
        sign = self.gensign()
        with open(apk_path, "rb") as f:
            content = f.read()
        md5 = hashlib.md5(content).hexdigest()
        files = {"file": (os.path.basename(apk_path), content,
                          "application/vnd.android.package-archive")}
        form = {"sign": sign, "identifier": md5, "type": "apk"}
        r = self.s.post("https://api.open.oppomobile.com/api/utility/upload",
                        files=files, data=form, timeout=180,
                        headers={"referer": self.BASE + "/"})
        r.raise_for_status()
        d = r.json()
        if d.get("errno") != 0:
            return None, d
        return (d.get("data") or {}).get("url"), d

    # ---------- 5) 提交解析任务
    def verify_task_add(self, apk_url, app_id):
        payload = {"apk_url": apk_url, "app_id": int(app_id),
                   "version_device": "1", "version_operation_type": "ADD_APP_CHECK"}
        d = self._post_json(self.V1 + "/app/verify-task-add", payload,
                            self._upload_referer(app_id))
        return (d.get("data") or {}).get("task_id"), d

    # ---------- 7) 删除未发布应用（2026-09-20 抓包实证；仅未完成发布流程的应用可删）
    def delete_app(self, app_id):
        d = self._post_json(self.V1 + "/app/delete-first-publish",
                            {"app_id": int(app_id)},
                            self.BASE + "/new/mcom/app/list/all")
        return d.get("code") == 0, d

    # ---------- 8) 名称重复校验（2026-09-20 抓包实证，前端传包页同款判定口）
    def check_appname(self, app_id, app_name, app_subname=""):
        """返回 (dup_flag, raw)。dup_flag: 1=名称重复 0=可用 None=判定失败。

        关键认知：verify-info 只负责 APK 解析（code=0 仅代表解析通过），
        名称重复由前端在解析完成后调 /app/appname 判定，
        data.app_name=1 时页面提示"名称与其他APP重复，请填写应用副名称"。
        """
        d = self._post_json(self.V1 + "/app/appname",
                            {"app_id": int(app_id), "app_name": app_name,
                             "app_subname": app_subname},
                            self._upload_referer(app_id))
        if d.get("code") != 0:
            return None, d
        data = d.get("data") or {}
        flag = data.get("app_name")
        return (1 if flag in (1, "1", True) else
                0 if flag in (0, "0", False) else None), d

    # ---------- 6) 轮询解析结果
    def verify_poll(self, task_id, app_id, max_seconds=240, interval=3):
        """返回 (finished, last_resp)。code!=911209 即终态（0=成功，其他=错误码）。"""
        deadline = time.time() + max_seconds
        last = None
        while time.time() < deadline:
            last = self._post_json(self.V1 + "/app/verify-info", {"task_id": task_id},
                                   self._upload_referer(app_id))
            if last.get("code") != VERIFY_INFO_BUSY:
                return True, last
            time.sleep(interval)
        return False, last


def judge_one(api, app_name, pkg_name, apk_path, log=None):
    """完整判定单个应用：创建 + 传包 + 解析。返回结果 dict（异常不外抛，保留 app_id）。

    注：应用户要求不做包名预检（checkPkgName 预检结果对判定无帮助，直接走创建）。
    """
    log = log or (lambda m: None)
    r = dict(name=app_name, package=pkg_name, app_id=None,
             duplicate=None, ok=False, message="")

    try:
        app_id, d = api.create_app(app_name, pkg_name)
        if not app_id:
            r["message"] = f"创建失败: {(d.get('message') or json.dumps(d, ensure_ascii=False))[:100]}"
            log(f"[{app_name}] {r['message']}")
            return r
        r["app_id"] = app_id
        log(f"[{app_name}] 创建成功 app_id={app_id}")

        apk_url, d = api.upload_apk(apk_path)
        if not apk_url:
            r["message"] = f"传包失败: {json.dumps(d, ensure_ascii=False)[:120]}"
            log(f"[{app_name}] {r['message']}")
            return r
        log(f"[{app_name}] 传包成功")

        task_id, d = api.verify_task_add(apk_url, app_id)
        if not task_id:
            r["message"] = f"解析任务提交失败: {(d.get('message') or '')[:100]}"
            log(f"[{app_name}] {r['message']}")
            return r
        log(f"[{app_name}] 解析任务已提交 task_id={task_id}")

        finished, resp = api.verify_poll(task_id, app_id)
        code = (resp or {}).get("code")
        msg = (resp or {}).get("message") or ""
        if not finished:
            r["message"] = f"解析轮询超时: {msg[:80]}"
            log(f"[{app_name}] {r['message']}")
            return r
        if code == VERIFY_INFO_OK:
            # verify-info 只做 APK 解析；重名判定必须再调 app/appname（前端同款）
            try:
                dup_flag, ad = api.check_appname(app_id, app_name)
            except Exception as ae:
                dup_flag, ad = None, {"message": str(ae)[:100]}
            r["ok"] = True
            if dup_flag == 1:
                r["duplicate"] = True
                r["message"] = "名称与其他APP重复，请填写应用副名称"
            elif dup_flag == 0:
                r["duplicate"] = False
                r["message"] = "解析成功，名称未被占用"
            else:
                r["duplicate"] = None
                r["message"] = ("解析成功但名称判定失败: "
                                + ((ad.get("message") or "")[:80]))
            log(f"[{app_name}] 判定: duplicate={r['duplicate']} | {r['message']}")
            return r
        # 其他终态错误码（如 910007 包名不符等）
        r["ok"] = True
        r["duplicate"] = ("重复" in msg) or ("名称与其他" in msg)
        r["message"] = f"code={code} {msg[:120]}"
        log(f"[{app_name}] 判定: duplicate={r['duplicate']} | {r['message']}")
        return r
    except Exception as e:
        # 异常也返回结果 dict（app_id 已设则保留，供 auto_delete 清理孤儿应用）
        r["ok"] = False
        r["message"] = "接口异常: " + str(e)[:100]
        log(f"[{app_name}] {r['message']}")
        return r


def judge_name_only(api, app_name, probe_app_id, log=None):
    """仅名称判定：借助探针应用 app_id 直接调 app/appname，不打包不传包。

    实证（2026-09-20）：appname 在未传包的空应用上即可正确判定
    data.app_name: 1=重复 0=可用。返回结果 dict（与 judge_one 同构）。
    """
    log = log or (lambda m: None)
    r = dict(name=app_name, package="", app_id="",
             duplicate=None, ok=False, message="")
    try:
        flag, d = api.check_appname(probe_app_id, app_name)
    except Exception as e:
        r["message"] = "接口异常: " + str(e)[:100]
        log(f"[{app_name}] {r['message']}")
        return r
    if flag == 1:
        r["ok"] = True
        r["duplicate"] = True
        r["message"] = "名称与其他APP重复，请填写应用副名称"
    elif flag == 0:
        r["ok"] = True
        r["duplicate"] = False
        r["message"] = "名称未被占用"
    else:
        r["message"] = "判定失败: " + ((d.get("message") or "")[:80])
    log(f"[{app_name}] 判定: duplicate={r['duplicate']} | {r['message']}")
    return r


if __name__ == "__main__":
    import sys
    buf = []

    def log(m):
        buf.append(m)

    api = OppoApi(log=log)
    out_path = os.path.join(HERE, "oppo-explore", "api_client_result.txt")
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "check":
            avail, d = api.check_pkg(sys.argv[2])
            buf.append(f"check_pkg {sys.argv[2]} -> available={avail}")
            buf.append(json.dumps(d, ensure_ascii=False)[:300])
        elif len(sys.argv) >= 5 and sys.argv[1] == "judge":
            res = judge_one(api, sys.argv[2], sys.argv[3], sys.argv[4], log=log)
            buf.append(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            buf.append(__doc__)
    finally:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(buf) + "\n")
        print("written:", out_path)
