# -*- coding: utf-8 -*-
"""OPPO 一键登录助手：弹 Edge 登录 → 自动导出 → 自动上传服务器 → 页面自动变绿。

用法：双击 login_oppo.bat（或直接运行本脚本）。登录成功后无需任何手动操作。
"""
import asyncio
import json
import os

import requests
from playwright.async_api import async_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "oppo-profile")   # 复用持久 profile，多数情况免密
OUT_FILE = os.path.join(HERE, "storage_state.json")
SERVER = "http://101.43.50.231:8000"
URL = "https://open.oppomobile.com/new/mcom/appList/appCreate"

# 本机系统代理会拦 8000 端口，显式绕过
SESSION = requests.Session()
SESSION.trust_env = False


async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE, channel="msedge", headless=False,
            viewport={"width": 1280, "height": 850},
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded")
        print(">> 请在弹出的浏览器窗口完成 OPPO 登录（最多等 20 分钟）...", flush=True)

        logged = False
        for _ in range(20 * 60 // 4):
            await asyncio.sleep(4)
            # 判据1：上下文任意 cookie 里出现 sdkLoginToken
            try:
                cookies = await ctx.cookies()
                if any(c["name"] == "sdkLoginToken" for c in cookies):
                    logged = True
                    break
            except Exception:
                pass
            # 判据2：页面停在创建页且没有登录 iframe
            try:
                in_login = any("id.oppo.com" in f.url and "login" in f.url
                               for f in page.frames)
                if "appCreate" in (page.url or "") and not in_login:
                    logged = True
                    break
            except Exception:
                pass

        if not logged:
            print("!! 等待登录超时，未导出", flush=True)
            await ctx.close()
            return

        await asyncio.sleep(3)
        state = await ctx.storage_state()
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        n = len(state.get("cookies", []))
        print(f"[1/2] 登录成功，已导出 {n} 条 cookie", flush=True)
        await ctx.close()

        # 自动上传服务器
        try:
            r = SESSION.post(SERVER + "/api/oppo/upload_cookie",
                             data=json.dumps(state, ensure_ascii=False).encode("utf-8"),
                             headers={"Content-Type": "application/json"}, timeout=30)
            resp = r.json()
            if resp.get("logged_in"):
                print("[2/2] 已自动上传并验证：服务器登录态有效 ✓", flush=True)
                print(">> 回到工具页即可开始批量验证（徽章已自动变绿）", flush=True)
            else:
                print("[2/2] 已上传，但服务器校验未通过：", resp.get("login_msg", ""), flush=True)
        except Exception as e:
            print("[2/2] 自动上传失败（可到工具页手动上传 storage_state.json）:", str(e)[:150],
                  flush=True)


if __name__ == "__main__":
    asyncio.run(main())
