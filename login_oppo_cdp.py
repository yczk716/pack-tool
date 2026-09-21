# -*- coding: utf-8 -*-
"""OPPO 登录助手（标签页模式）：在你日常使用的浏览器里新开标签页完成登录，不双开浏览器。

原理：
  1. 检测本机 9222 调试端口；若未开启且 Edge 正在运行，提示你先关闭所有 Edge 窗口；
  2. 脚本以远程调试端口拉起你默认的 Edge（--restore-last-session 恢复原标签页）；
  3. 通过 CDP 附加到该实例 → 新开一个 OPPO 标签页 → 轮询 cookie；
  4. 检测到 sdkLoginToken（含 HttpOnly 的完整 cookie 可读）→ 导出 → 自动回传服务器；
  5. 只关闭 OPPO 那一个标签页，浏览器原样保留。

备用：老模式（独立 Edge 窗口、独立 profile）仍可用：python export_login.py
"""
import asyncio
import json
import os
import subprocess
import sys

import requests
from playwright.async_api import async_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(HERE, "storage_state.json")
SERVER = "http://101.43.50.231:8000"
URL = "https://open.oppomobile.com/new/mcom/appList/appCreate"
PORT = 9222
DEBUG_URL = "http://127.0.0.1:%d" % PORT

BROWSER_CANDIDATES = [
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
]

# 本机系统代理会拦 8000 端口，显式绕过
SESSION = requests.Session()
SESSION.trust_env = False


def find_browser():
    for p in BROWSER_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def browser_running(exe):
    name = os.path.basename(exe)
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % name],
                       capture_output=True, text=True)
    return name.lower() in (r.stdout or "").lower()


async def try_connect(p):
    try:
        return await p.chromium.connect_over_cdp(DEBUG_URL, timeout=3000)
    except Exception:
        return None


def oppo_cookies(cookies):
    """只保留 oppo 相关域的 cookie（含 HttpOnly 的完整字段）。"""
    return [c for c in cookies if "oppo" in (c.get("domain") or "")]


async def main():
    dry = "--dry-run" in sys.argv
    async with async_playwright() as p:
        browser = await try_connect(p)
        if browser is None:
            exe = find_browser()
            if not exe:
                print("!! 未找到 Edge/Chrome，请先安装 Microsoft Edge")
                return
            if browser_running(exe):
                print(">> 检测到浏览器正在运行，但未开启调试端口。")
                print(">> 请关闭所有浏览器窗口（标签页会话不会丢失），然后——")
                try:
                    input(">> 回到这里按回车继续 ...")
                except EOFError:
                    pass
            print(">> 正在以调试模式启动你的浏览器（自动恢复原有标签页）...")
            subprocess.Popen([exe, "--remote-debugging-port=%d" % PORT,
                              "--restore-last-session"])
            for _ in range(45):
                await asyncio.sleep(1)
                browser = await try_connect(p)
                if browser is not None:
                    break
            if browser is None:
                print("!! 调试端口连接失败，请重试")
                return
        print(">> 已附加到你的浏览器，正在新开 OPPO 标签页 ...")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.goto(URL, wait_until="domcontentloaded")
        except Exception as e:
            print("!! 打开 OPPO 页面失败:", str(e)[:120])
            return
        print(">> 等待 OPPO 登录（若浏览器已记住登录则立即完成，最长 20 分钟）...")
        logged = False
        for _ in range(20 * 60 // 4):
            await asyncio.sleep(4)
            try:
                cookies = await ctx.cookies()
                if any(c["name"] == "sdkLoginToken" for c in cookies):
                    logged = True
                    break
            except Exception:
                pass
        if not logged:
            print("!! 等待登录超时，未导出")
            try:
                await page.close()
            except Exception:
                pass
            return

        await asyncio.sleep(3)
        cookies = await ctx.cookies()
        keep = oppo_cookies(cookies)
        state = {"cookies": keep, "origins": []}
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        print("[1/2] 登录成功，已导出 %d 条 OPPO 相关 cookie" % len(keep))
        try:
            await page.close()  # 只关 OPPO 标签页，浏览器原样保留
        except Exception:
            pass

        if dry:
            print("[dry-run] 跳过上传")
            return
        try:
            r = SESSION.post(SERVER + "/api/oppo/upload_cookie",
                             data=json.dumps(state, ensure_ascii=False).encode("utf-8"),
                             headers={"Content-Type": "application/json"}, timeout=30)
            resp = r.json()
            if resp.get("logged_in"):
                print("[2/2] 已自动上传并验证：服务器登录态有效 ✓")
                print(">> 回到工具页即可开始批量验证（徽章已自动变绿）")
            else:
                print("[2/2] 已上传，但服务器校验未通过：", resp.get("login_msg", ""))
        except Exception as e:
            print("[2/2] 自动上传失败（可到工具页手动上传 storage_state.json）:", str(e)[:150])


if __name__ == "__main__":
    asyncio.run(main())
