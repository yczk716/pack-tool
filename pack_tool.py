# -*- coding: utf-8 -*-
"""
快速打包工具 pack_tool.py
========================
输入应用名称（可选一个网址），自动生成包名，秒级打包出一个可安装的 APK。

- 命令行: python pack_tool.py --cli --name "我的应用" [--url https://xx] [--package com.a.b]
- 图形界面: python pack_tool.py   (自动打开浏览器, 本地网页操作)

依赖（首次运行自动布置到 bin/）: JDK17 + Android build-tools + platform android.jar
"""
import os
import re
import sys
import json
import random
import shutil
import struct
import subprocess
import tempfile
import uuid
import time
import zipfile
import zlib
try:
    from http.server import ThreadingHTTPServer
except ImportError:  # python 3.6
    from http.server import HTTPServer as ThreadingHTTPServer
from http.server import BaseHTTPRequestHandler

IS_WIN = sys.platform == "win32"
EXE = ".exe" if IS_WIN else ""

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "bin")
OUTPUT_DIR = os.path.join(HERE, "output")
KEYSTORE = os.path.join(HERE, "keystore", "debug.keystore")
KS_PASS = "android"

MIN_SDK = 21
TARGET_SDK = 34
VERSION_CODE = "1"
VERSION_NAME = "1.0"

APK_KEEP = 10  # 服务器 output 目录最多保留的 APK 数，超出删最旧的


def cleanup_apk_dir(keep=APK_KEEP):
    """output 目录只保留最近 keep 个 .apk，其余删除（防占用服务器空间）。"""
    try:
        files = [f for f in os.listdir(OUTPUT_DIR) if f.lower().endswith(".apk")]
    except OSError:
        return 0
    files.sort(key=lambda f: -os.path.getmtime(os.path.join(OUTPUT_DIR, f)))
    removed = 0
    for f in files[keep:]:
        try:
            os.remove(os.path.join(OUTPUT_DIR, f))
            removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------- 模板源码

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{{PACKAGE}}">

    <application
        android:label="@string/app_name"
        android:icon="@mipmap/ic_launcher"
        android:theme="@android:style/Theme.Material.Light.NoActionBar">

        <activity android:name="com.packshell.MainActivity" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>
    </application>
</manifest>
"""

STRINGS = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">{{APP_NAME}}</string>
</resources>
"""

MAIN_JAVA = """package com.packshell;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.TextView;

public class MainActivity extends Activity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        TextView tv = new TextView(this);
        int id = getResources().getIdentifier("app_name", "string", getPackageName());
        tv.setText(id != 0 ? getString(id) : getPackageName());
        tv.setTextSize(28);
        tv.setGravity(Gravity.CENTER);
        tv.setTextColor(Color.rgb(33, 33, 33));
        setContentView(tv);
    }
}
"""


# ---------------------------------------------------------------- 依赖定位

def _unzip_once(archive_path, dest_parent):
    """解压 zip/tar.gz 到 dest_parent（幂等：已有 .extracted 标记则跳过）。"""
    marker = archive_path + ".extracted"
    if os.path.exists(marker):
        return
    print("解压:", os.path.basename(archive_path), flush=True)
    base = archive_path[:-5] if archive_path.endswith(".done") else archive_path
    if base.endswith((".tar.gz", ".tgz")):
        import tarfile
        with tarfile.open(archive_path) as t:
            t.extractall(dest_parent)
    else:
        with zipfile.ZipFile(archive_path) as z:
            z.extractall(dest_parent)
    open(marker, "w").close()


_TOOLS = None


def locate_tools():
    """返回 dict: java / aapt2 / zipalign / d8_jar / apksigner_jar / android_jar（带缓存）"""
    global _TOOLS
    if _TOOLS:
        return _TOOLS
    cache_file = os.path.join(BIN, ".tools.json")
    if os.path.exists(cache_file):
        try:
            c = json.load(open(cache_file, encoding="utf-8"))
            if isinstance(c, dict) and all(os.path.exists(v) for v in c.values()):
                _TOOLS = c
                return _TOOLS
        except Exception:
            pass
    dl = os.path.join(HERE, "downloads")
    jdk_name = "jdk.zip.done" if IS_WIN else "jdk.tar.gz.done"
    bt_name = "build-tools.zip.done"
    plat_name = "platform-34.zip.done"
    for f in (jdk_name, bt_name, plat_name):
        p = os.path.join(dl, f)
        if not os.path.exists(p):
            sys.exit(f"缺少依赖包 {f}，请先运行: python download_deps.py")
    _unzip_once(os.path.join(dl, jdk_name), BIN)
    _unzip_once(os.path.join(dl, bt_name), BIN)
    _unzip_once(os.path.join(dl, plat_name), BIN)

    java_exe = None
    for root, dirs, files in os.walk(BIN):
        if ("java" + EXE) in files and os.path.basename(root) == "bin":
            java_exe = os.path.join(root, "java" + EXE)
            break
    aapt2 = zipalign = None
    d8_jar = apksigner_jar = android_jar = None
    for root, dirs, files in os.walk(BIN):
        if aapt2 is None and ("aapt2" + EXE) in files:
            aapt2 = os.path.join(root, "aapt2" + EXE)
            zipalign = os.path.join(root, "zipalign" + EXE)
        if d8_jar is None and "d8.jar" in files:
            d8_jar = os.path.join(root, "d8.jar")
            apksigner_jar = os.path.join(root, "apksigner.jar")
        if android_jar is None and "android.jar" in files:
            android_jar = os.path.join(root, "android.jar")
    missing = [n for n, v in [("java", java_exe), ("aapt2", aapt2), ("zipalign", zipalign),
                              ("d8.jar", d8_jar), ("apksigner.jar", apksigner_jar),
                              ("android.jar", android_jar)] if not v]
    if missing:
        sys.exit("依赖不完整: " + ", ".join(missing))
    if not IS_WIN:  # zipfile 解压不保留执行位，补上
        for p in (java_exe, aapt2, zipalign):
            try:
                os.chmod(p, 0o755)
            except OSError:
                pass
    _TOOLS = dict(java=java_exe, aapt2=aapt2, zipalign=zipalign, d8=d8_jar,
                  apksigner=apksigner_jar, android_jar=android_jar)
    try:
        json.dump(_TOOLS, open(cache_file, "w", encoding="utf-8"))
    except Exception:
        pass
    return _TOOLS


def run(cmd, log, cwd=None, env=None):
    log.append("$ " + " ".join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd)))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, cwd=cwd, env=env, errors="replace", shell=False)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "")[-1500:]
        raise RuntimeError(f"命令失败({p.returncode}): {cmd[0]}\n{tail}")
    if p.stdout.strip():
        log.append(p.stdout.strip()[-800:])
    return p


def ensure_keystore(tools, log):
    if os.path.exists(KEYSTORE):
        return KEYSTORE
    os.makedirs(os.path.dirname(KEYSTORE), exist_ok=True)
    keytool = os.path.join(os.path.dirname(tools["java"]), "keytool" + EXE)
    run([keytool, "-genkeypair", "-keystore", KEYSTORE, "-alias", "androiddebugkey",
         "-storepass", KS_PASS, "-keypass", KS_PASS, "-keyalg", "RSA",
         "-keysize", "2048", "-validity", "10000",
         "-dname", "CN=Android Debug,O=Android,C=US"], log)
    return KEYSTORE


# ---------------------------------------------------------------- 内置图标

_ICON_PNG = None


def make_icon_png(size=192):
    """标准库生成一个简洁图标 PNG（绿色圆角底 + 白色圆）。内容固定，进程内缓存。"""
    global _ICON_PNG
    if _ICON_PNG:
        return _ICON_PNG
    import math
    px = bytearray()
    for y in range(size):
        px.append(0)  # filter none
        for x in range(size):
            r, g, b, a = 76, 175, 80, 255
            # 圆角遮罩
            corner = 28
            in_corner = ((x < corner and y < corner) or (x >= size - corner and y < corner)
                         or (x < corner and y >= size - corner) or (x >= size - corner and y >= size - corner))
            if in_corner:
                cx = corner if x < size / 2 else x - (size - corner - 1)
                cy = corner if y < size / 2 else y - (size - corner - 1)
                if math.hypot(cx - corner + 0.5, cy - corner + 0.5) > corner:
                    a = 0
            # 白色圆
            if a and math.hypot(x - size / 2 + 0.5, y - size / 2 + 0.5) < size * 0.26:
                r, g, b = 255, 255, 255
            px += bytes((r, g, b, a))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    _ICON_PNG = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                 + chunk(b"IDAT", zlib.compress(bytes(px), 9)) + chunk(b"IEND", b""))
    return _ICON_PNG


# ---------------------------------------------------------------- 构建引擎

def gen_package(app_name):
    ascii_part = re.sub(r"[^a-zA-Z0-9]", "", app_name).lower()
    if len(ascii_part) < 2:
        ascii_part = "app"
    ascii_part = ascii_part[:12]
    return "com.{}{}{}".format(ascii_part, time.strftime("%m%d"), random.randint(100, 999))


def safe_filename(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_")[:40] or "app"


SHELL_PKG = "com.packshell"
SHELL_DEX = os.path.join(BIN, "shell.dex")


def ensure_shell_dex(tools, log):
    """壳代码 dex：内容固定（不依赖包名/应用名），首次编译后永久缓存复用。"""
    if os.path.exists(SHELL_DEX):
        return SHELL_DEX
    log.append("首次运行: 预编译壳 dex（仅一次，之后复用）")
    work = tempfile.mkdtemp(prefix="shell_")
    try:
        src = os.path.join(work, "MainActivity.java")
        open(src, "w", encoding="utf-8").write(MAIN_JAVA)
        classes = os.path.join(work, "classes")
        os.makedirs(classes)
        javac = os.path.join(os.path.dirname(tools["java"]), "javac" + EXE)
        run([javac, "--release", "8", "-nowarn", "-classpath", tools["android_jar"],
             "-d", classes, src], log)
        dex_dir = os.path.join(work, "dex")
        os.makedirs(dex_dir)
        class_files = [os.path.join(root, f) for root, _, fs in os.walk(classes)
                       for f in fs if f.endswith(".class")]
        run([tools["java"], "-cp", tools["d8"], "com.android.tools.r8.D8",
             "--release", "--min-api", str(MIN_SDK), "--lib", tools["android_jar"],
             "--output", dex_dir] + class_files, log)
        os.makedirs(BIN, exist_ok=True)
        shutil.copy(os.path.join(dex_dir, "classes.dex"), SHELL_DEX)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return SHELL_DEX


def build_apk(app_name, package, out_dir=None, log=None):
    log = log if log is not None else []
    t0 = time.time()
    tools = locate_tools()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_dir = out_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    work = tempfile.mkdtemp(prefix="pack_")
    try:
        log.append(f"== 开始打包: {app_name}  包名: {package}")
        res_vals = os.path.join(work, "res", "values")
        os.makedirs(res_vals)
        res_icon = os.path.join(work, "res", "mipmap")
        os.makedirs(res_icon)

        # 1. 渲染资源与清单（壳 dex 与包名无关，无需生成 java）
        open(os.path.join(work, "AndroidManifest.xml"), "w", encoding="utf-8").write(
            MANIFEST.replace("{{PACKAGE}}", package))
        open(os.path.join(res_vals, "strings.xml"), "w", encoding="utf-8").write(
            STRINGS.replace("{{APP_NAME}}", app_name.replace("&", "&amp;").replace("<", "&lt;")))
        open(os.path.join(res_icon, "ic_launcher.png"), "wb").write(make_icon_png())

        env = dict(os.environ)
        env["PATH"] = os.path.dirname(tools["java"]) + os.pathsep + env.get("PATH", "")
        env["JAVA_HOME"] = os.path.dirname(os.path.dirname(tools["java"]))

        # 2. aapt2 compile + link
        run([tools["aapt2"], "compile", "--dir", os.path.join(work, "res"),
             "-o", os.path.join(work, "res.zip")], log)
        unsigned = os.path.join(work, "unsigned.apk")
        run([tools["aapt2"], "link", "-o", unsigned, "-I", tools["android_jar"],
             "--manifest", os.path.join(work, "AndroidManifest.xml"),
             "--min-sdk-version", str(MIN_SDK), "--target-sdk-version", str(TARGET_SDK),
             "--version-code", VERSION_CODE, "--version-name", VERSION_NAME,
             "--auto-add-overlay",
             os.path.join(work, "res.zip")], log)
        log.append("资源编译链接完成")

        # 3. 复用预编译壳 dex
        shell_dex = ensure_shell_dex(tools, log)

        # 4. classes.dex 并入 APK（保留原压缩方式）
        merged = os.path.join(work, "merged.apk")
        with zipfile.ZipFile(unsigned) as zin, zipfile.ZipFile(merged, "w") as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename), compress_type=item.compress_type)
            zout.write(shell_dex, "classes.dex", zipfile.ZIP_DEFLATED)

        # 5. zipalign 4 字节对齐
        aligned = os.path.join(work, "aligned.apk")
        run([tools["zipalign"], "-f", "4", merged, aligned], log)

        # 6. apksigner 签名
        ks = ensure_keystore(tools, log)
        out_name = f"{safe_filename(app_name)}_{package}.apk"
        out_path = os.path.join(out_dir, out_name)
        if os.path.exists(out_path):
            os.remove(out_path)
        run([tools["java"], "-jar", tools["apksigner"], "sign",
             "--ks", ks, "--ks-pass", "pass:" + KS_PASS,
             "--key-pass", "pass:" + KS_PASS, "--ks-key-alias", "androiddebugkey",
             "--out", out_path, aligned], log)
        verify = subprocess.run([tools["java"], "-jar", tools["apksigner"], "verify", out_path],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, errors="replace")
        size_mb = os.path.getsize(out_path) / 1048576
        log.append(f"签名校验: {'通过' if verify.returncode == 0 else '失败 ' + (verify.stderr or '')[:200]}")
        log.append(f"== 完成: {out_path} ({size_mb:.2f}MB, 耗时 {time.time()-t0:.1f}s)")
        return dict(ok=verify.returncode == 0, output=out_path, log=log)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- OPPO 批量验证（服务器端模块，可选）
OPPO_ENABLED = False
try:
    import oppo_server
    oppo_server.configure(build_apk, gen_package,
                          log_fn=lambda m: print(m, flush=True))
    OPPO_ENABLED = True
except Exception:
    pass

# ---------------------------------------------------------------- 网页 GUI

PAGE = """
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>APK 打包与 OPPO 重名验证</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    --bg:#f4f5f0; --card:#ffffff; --ink:#1f261e; --sub:#6f7d6c; --line:#e2e7dc;
    --brand:#2f9e44; --brand-dark:#237a35; --brand-soft:#e9f5eb;
    --warn:#e8590c; --err:#c92a2a; --info:#1971c2; --code:#f0f2ea;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;background:var(--bg);
    color:var(--ink);display:flex;justify-content:center;padding:36px 16px 64px}
  .wrap{width:660px;max-width:100%}
  header.top{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:4px}
  h1{font-size:21px;letter-spacing:.5px}
  h1 b{color:var(--brand)}
  .sub{color:var(--sub);font-size:13px;margin-bottom:18px}
  .tabs{display:flex;border:1px solid var(--line);border-radius:11px;overflow:hidden;margin-bottom:18px;background:var(--card)}
  .tabs label{flex:1;text-align:center;padding:12px 0;cursor:pointer;font-size:14px;color:var(--sub);
    transition:background .15s ease-out,color .15s ease-out;margin:0}
  .tabs label:has(input:checked){background:var(--brand);color:#fff;font-weight:600}
  .tabs label input{display:none}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:26px}
  label{font-size:13px;color:var(--sub);display:block;margin:14px 0 6px}
  input[type=text],textarea{width:100%;border:1px solid var(--line);border-radius:9px;padding:10px 12px;
    font-size:14px;outline:none;background:#fff;transition:border-color .15s ease-out;font-family:inherit}
  input:focus,textarea:focus{border-color:var(--brand)}
  textarea{resize:vertical}
  .row{display:flex;gap:8px;align-items:center}
  button{cursor:pointer;border:none;border-radius:9px;font-size:14px;font-family:inherit;
    transition:background .15s ease-out,opacity .15s ease-out}
  .mini{background:#eef0ea;color:var(--ink);padding:9px 14px;white-space:nowrap}
  .mini:hover{background:#e2e6dc}
  .go{width:100%;margin-top:20px;background:var(--brand);color:#fff;padding:13px;font-size:15px;
    font-weight:600;border-radius:10px}
  .go:hover{background:var(--brand-dark)}
  .go:disabled{opacity:.55;cursor:not-allowed}
  .step{display:flex;align-items:center;gap:9px;font-size:13.5px;font-weight:600;margin:24px 0 10px}
  .step .n{flex:none;width:21px;height:21px;border-radius:50%;background:var(--brand-soft);
    color:var(--brand-dark);display:inline-flex;align-items:center;justify-content:center;
    font-size:12px;font-weight:700}
  .step small{font-weight:400;color:var(--sub);margin-left:auto;font-size:12px}
  .pill{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:999px;
    font-size:12px;border:1px solid;white-space:nowrap}
  .pill .dot{width:7px;height:7px;border-radius:50%;background:currentColor}
  .pill.ok{color:#237a35;background:var(--brand-soft);border-color:#bfe3c6}
  .pill.no{color:#5c6b59;background:#eef0ea;border-color:var(--line)}
  .pill.warn{color:var(--warn);background:#fdf0e7;border-color:#f5d3b8}
  .pill.run .dot{animation:pulse 1.1s infinite}
  @keyframes pulse{50%{opacity:.25}}
  .hint{font-size:12.5px;color:var(--sub);margin-top:6px;line-height:1.7}
  .hint b{color:var(--ink)}
  .callout{background:var(--code);border:1px solid var(--line);border-radius:10px;
    padding:10px 12px;font-size:12.5px;line-height:1.7;color:var(--ink)}
  details{margin-top:10px;border:1px solid var(--line);border-radius:10px;padding:10px 14px}
  summary{cursor:pointer;font-size:13px;color:var(--sub);user-select:none}
  details[open] summary{margin-bottom:8px}
  table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
  th{color:var(--sub);text-align:left;font-weight:400;padding:5px 4px}
  td{border-top:1px solid var(--line);padding:7px 4px;vertical-align:top}
  a{color:var(--info);text-decoration:none}
  a:hover{text-decoration:underline}
  .logbox{display:none;margin-top:14px;background:#232b24;color:#b2e59a;
    font:12px/1.65 Consolas,monospace;border-radius:10px;padding:12px;max-height:280px;
    overflow:auto;white-space:pre-wrap;word-break:break-all}
  .result{display:none;margin-top:14px;padding:12px 14px;border-radius:10px;
    background:var(--brand-soft);border:1px solid #bfe3c6;font-size:13px;line-height:1.8}
  .btn{display:inline-block;background:var(--brand);color:#fff;padding:8px 16px;
    border-radius:8px;text-decoration:none;font-size:13px}
  .btn:hover{background:var(--brand-dark);text-decoration:none}
  .btn.blue{background:var(--info)}
  .btn.blue:hover{background:#1558a0}
  .mono{font-family:Consolas,monospace}
  #ores td{font-size:12.5px}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>APK 打包 <b>&amp;</b> OPPO 重名验证</h1>
    <span id="head-badge" class="pill no"><span class="dot"></span>未登录</span>
  </header>
  <div class="sub">重名验证：仅名称判定（秒级）｜ 完整验证：创建→验证→自动删除 ｜ 打包下载：输入名称一键出 APK</div>

  <div class="tabs">
    <label><input type="radio" name="mode" value="oppo" checked onchange="switchMode()">✅ OPPO 重名验证</label>
    <label><input type="radio" name="mode" value="full" onchange="switchMode()">🔍 完整验证</label>
    <label><input type="radio" name="mode" value="pack" onchange="switchMode()">📦 打包下载</label>
  </div>

  <!-- ============ OPPO 重名验证 ============ -->
  <div class="panel" id="oppo-area">
    <div class="step"><span class="n">1</span>登录 OPPO 开放平台 <small>会话约 1~2 小时，失效重登即可</small></div>
    <div class="row" style="flex-wrap:wrap">
      <span id="login-badge" class="pill no"><span class="dot"></span>OPPO 检查中…</span>
      <span id="vivo-badge" class="pill no"><span class="dot"></span>vivo 检查中…</span>
      <button class="mini" onclick="window.open('https://open.oppomobile.com/','_blank')">打开 OPPO 登录页</button>
      <a class="mini" href="/api/oppo/login_helper.zip" download
         style="text-decoration:none;display:inline-flex;align-items:center">⬇ 下载登录助手</a>
    </div>
    <div class="hint" style="margin-top:6px">下载解压后双击 <b>login_oppo.bat</b>：在你日常使用的浏览器里<b>新开标签页</b>登录 OPPO（不双开浏览器），登录态自动回传，本页徽章自动变绿（vivo 列无需登录）。</div>
        <details>
      <summary>备用方式：粘贴 Cookie / 上传登录文件</summary>
      <div class="hint">粘贴法：在 OPPO 标签页 <b>F12 → Network → 刷新 → 点第一个请求 → Request Headers → 复制 cookie: 整行</b>，粘贴到下面提交。</div>
      <textarea id="cookietext" rows="2" style="margin-top:6px;font:12px/1.5 Consolas,monospace"
        placeholder="OPPOSID=xxx; dev_id=xxx; OPENPLATLOGIN=xxx; ..."></textarea>
      <div class="row" style="margin-top:8px">
        <button class="mini" onclick="pasteCookie()" style="padding:8px 16px">提交登录态</button>
        <button class="mini" onclick="checkLogin()" style="padding:8px 16px">验证登录态</button>
        <label class="mini" style="display:inline-block;padding:8px 16px;cursor:pointer">上传 storage_state.json
          <input type="file" id="cookiefile" accept=".json" style="display:none" onchange="uploadCookie(this)">
        </label>
      </div>
      <div class="hint" style="margin-top:12px">vivo 判名 Cookie（用于结果表 vivo 列）：在 <b>dev.vivo.com.cn</b> 登录后 F12 → Network → 刷新 → 任意请求 → 复制 cookie: 整行。</div>
      <textarea id="vivocookie" rows="2" style="margin-top:6px;font:12px/1.5 Consolas,monospace"
        placeholder="b_account_token=xxx; JSESSIONID=yyy; ..."></textarea>
      <button class="mini" style="margin-top:6px;padding:8px 16px" onclick="pasteVivoCookie()">提交 vivo Cookie</button>
      <span id="vivo-msg" class="hint" style="margin-left:8px"></span>
    </details>
    <div id="login-msg" class="hint">未登录：优先用上面的傻瓜式登录；或展开备用方式手动提交登录态。</div>

    <div class="step"><span class="n">2</span>待验证名称 <small>每行一个</small></div>
    <textarea id="onames" rows="5" placeholder="例如：&#10;星空阅读&#10;极速清理大师&#10;全能工具箱"></textarea>
    <div class="hint" style="margin-top:12px">本页为<b>仅名称判定</b>（秒级、零残留）：用常驻探针应用直接调判定接口，不打包、不创建、不传包。如需<b>打包上传并自动删除</b>的完整验证，请用「🔍 完整验证」页签。</div>
    <button class="go" id="gooppo" onclick="doOppoBatch()">开始验证</button>

    <div class="step" id="ostep3" style="display:none"><span class="n">3</span>实时进度</div>
    <table id="ores" style="display:none"></table>
    <div id="olog" class="logbox"></div>
    <div id="oresult" class="result"></div>

  </div>
<!-- ============ 完整验证（创建-验证-删除） ============ -->
  <div class="panel" id="full-area" style="display:none">
    <div class="step"><span class="n">1</span>登录状态 <small>与「✅ OPPO 重名验证」共用同一登录态</small></div>
    <div class="row" style="flex-wrap:wrap">
      <span id="full-badge" class="pill no"><span class="dot"></span>检查中…</span>
      <span class="hint" id="full-login-hint" style="margin:0">未登录时请先到第一个页签「✅ OPPO 重名验证」完成 OPPO 登录（vivo 判定不受影响，无需登录）。</span>
    </div>

    <div class="step" style="margin-top:14px"><span class="n">2</span>待验证名称 <small>每行一个 · 数量不限</small></div>
    <textarea id="fnames" rows="5" placeholder="例如：&#10;星空阅读&#10;极速清理大师&#10;全能工具箱"></textarea>
    <div class="hint">每 5 个一批：<b>创建应用 → 打包上传 → 判定 → 自动删除</b>，删完继续下一批直到全部完成，账号零残留。APK 保留在下方列表（仅最近 10 个）。</div>
    <button class="go" id="gofull" onclick="doFullBatch()">开始完整验证</button>

    <div class="step" id="fstep3" style="display:none"><span class="n">3</span>实时进度</div>
    <table id="fres" style="display:none"></table>
    <div id="flog" class="logbox"></div>
    <div id="fresult" class="result"></div>

    <div class="step"><span class="n">4</span>服务器上的 APK <small>仅保留最近 10 个</small></div>
    <div id="apk-full" class="hint" style="line-height:2">加载中…</div>
    <button class="mini" onclick="loadApkList()" style="margin-top:8px;padding:7px 14px">刷新列表</button>
  </div>

<!-- ============ 打包下载 ============ -->
  <div class="panel" id="pack-area" style="display:none">
    <div class="step"><span class="n">1</span>填写应用名称 <small>每行一个 · 单行即单个打包</small></div>
    <textarea id="names" rows="6"
      placeholder="每行一条，格式：&#10;应用名称&#10;应用名称|自定义包名&#10;例如：&#10;星空阅读&#10;极速清理大师|com.my.cleaner"></textarea>
    <div class="hint">包名默认自动生成；需要指定时用 <b>名称|包名</b> 写法。多个名称自动 5 个一组并行打包。</div>
    <button class="go" id="gob" onclick="doPack()">开始打包</button>
    <div id="blog" class="logbox"></div>
    <div id="bresult" style="display:none"></div>
    <div id="packzip" style="display:none;margin-top:12px"></div>

    <div class="step"><span class="n">2</span>服务器上的 APK <small>仅保留最近 10 个，超出自动清理</small></div>
    <div id="apk-pack" class="hint" style="line-height:2">加载中…</div>
    <button class="mini" onclick="loadApkList()" style="margin-top:8px;padding:7px 14px">刷新列表</button>
  </div>
</div>

  <script>
const $ = id => document.getElementById(id);
function switchMode(){
  const mode = document.querySelector('input[name=mode]:checked').value;
  $('pack-area').style.display = mode === 'pack' ? 'block' : 'none';
  $('oppo-area').style.display = mode === 'oppo' ? 'block' : 'none';
  $('full-area').style.display = mode === 'full' ? 'block' : 'none';
  if(mode === 'oppo' || mode === 'full') startOppoPoll();
}
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function copyText(btn, value){
  if(!value) return;
  const done = () => { btn.textContent = '已复制'; setTimeout(()=>btn.textContent='复制路径', 1200); };
  if(navigator.clipboard) navigator.clipboard.writeText(value).then(done).catch(()=>fallbackCopy(value, done));
  else fallbackCopy(value, done);
}
function fallbackCopy(text, done){
  const ta = document.createElement('textarea');
  ta.value = text; document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); done(); } catch(e){}
  document.body.removeChild(ta);
}
async function loadApkList(){
  let html;
  try{
    const d = await fetch('/api/apk/list').then(r=>r.json());
    const files = d.files || [];
    html = '';
    if(d.cleaned) html += '<div style="color:var(--warn)">已自动清理 '+d.cleaned+' 个旧 APK（仅保留最近 10 个）</div>';
    if(!files.length){ html += '暂无已打包的 APK（打包完成后出现在这里）'; }
    else {
      html += files.slice(0,20).map(f =>
        '<a href="/api/apk/download?file='+encodeURIComponent(f.filename)+'" download>'+esc(f.filename)+'</a>（'+f.size_mb+' MB）'
      ).join('<br>') +
      (files.length > 20 ? '<br>… 共 '+files.length+' 个' : '');
    }
  }catch(e){ html = '列表加载失败: ' + e; }
  ['apk-pack','apk-full'].forEach(id => { const el = $(id); if(el) el.innerHTML = html; });
}

/* ================= 打包下载 ================= */
const GROUP = 5;
function chunk(arr, n){ const out=[]; for(let i=0;i<arr.length;i+=n) out.push(arr.slice(i,i+n)); return out; }
async function buildOne(name, pkg){
  const resp = await fetch('/api/build', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: name, pkg: pkg})
  });
  return await resp.json();
}
let lastPackFiles = [];
function renderPackRows(rows){
  const resEl = $('bresult');
  resEl.style.display='block';
  resEl.innerHTML = '<table><tr><th>名称</th><th>包名</th><th>状态</th><th>APK</th></tr>' +
    rows.map(r => `<tr>
      <td>${esc(r.name)}</td>
      <td class="mono">${esc(r.pkg)}</td>
      <td>${r.ok ? '<span style="color:var(--brand-dark)">✓ '+r.size+' MB</span>' : '<span style="color:var(--err)">✗ 失败</span>'}</td>
      <td>${r.ok ? '<a href="/api/apk/download?file='+encodeURIComponent(r.file)+'" download>下载</a>' : '-'}</td></tr>`).join('') + '</table>';
}
async function doPack(){
  const lines = $('names').value.split('\\n').map(s=>s.trim()).filter(Boolean);
  if(!lines.length){ alert('请填写应用名称（每行一个）'); return; }
  const go = $('gob'), logEl = $('blog');
  go.disabled = true; logEl.style.display='block';
  $('bresult').style.display='none'; $('packzip').style.display='none';
  const rows = [];
  lastPackFiles = [];
  const groups = chunk(lines, GROUP);
  for(let g=0; g<groups.length; g++){
    logEl.textContent = `第 ${g+1}/${groups.length} 组打包中（组内 ${groups[g].length} 个并行）…`;
    await Promise.all(groups[g].map(async (line) => {
      const name = line.split('|')[0].trim();
      const customPkg = line.includes('|') ? line.split('|')[1].trim() : '';
      try {
        let pkg = customPkg;
        if(!pkg) pkg = await fetch('/api/gen_pkg').then(r=>r.json()).then(d=>d.pkg);
        const d = await buildOne(name, pkg);
        const file = d.ok ? d.filename : '';
        if(file) lastPackFiles.push(file);
        rows.push({name, pkg, ok: !!d.ok, size: d.size_mb, file});
      } catch(e) {
        rows.push({name, pkg: customPkg || '-', ok: false, size: '', file: ''});
      }
      renderPackRows(rows);
    }));
  }
  const okN = rows.filter(r=>r.ok).length;
  logEl.textContent = `全部完成：${okN}/${rows.length} 成功`;
  go.disabled = false;
  renderPackRows(rows);
  if(lastPackFiles.length){
    const z = $('packzip');
    z.style.display='block';
    z.innerHTML = '<a class="btn" href="/api/apk/download?files='+encodeURIComponent(lastPackFiles.join(','))+'">⬇ 下载本次打包 APK（'+lastPackFiles.length+' 个，zip）</a>';
  }
  loadApkList();
}

/* ================= OPPO 重名验证 ================= */
let oppoTimer = null, lastLogLen = 0, oppoDone = false;
function oppoPoll(){
  fetch('/api/oppo/progress').then(r=>r.json()).then(d=>{
    renderLogin(d.vnc || {});
    renderOppo(d);
  }).catch(()=>{});
}
function startOppoPoll(){
  if(oppoTimer) return;
  oppoTimer = setInterval(oppoPoll, 2500);
  oppoPoll();
}
function renderLogin(v){
  const set = (el, cls, txt) => { if(el){ el.className = cls; el.innerHTML = '<span class="dot"></span>'+txt; } };
  const st = v.logged_in ? ['pill ok','OPPO 已登录'] : (v.has_cookie ? ['pill warn','OPPO 待验证'] : ['pill no','OPPO 未登录']);
  ['login-badge','full-badge'].forEach(id => set($(id), st[0], st[1]));
  set($('head-badge'), v.logged_in ? 'pill ok' : st[0], v.logged_in ? 'OPPO 已登录' : st[1]);
  const vs = v.vivo_ok;
  if(vs === true) set($('vivo-badge'), 'pill ok', 'vivo 正常');
  else if(vs === false) set($('vivo-badge'), 'pill warn', 'vivo 异常');
  else set($('vivo-badge'), 'pill no', 'vivo 未启用');
  if(v.login_msg) $('login-msg').textContent = v.login_msg;
  else if(!v.has_cookie) $('login-msg').textContent = 'OPPO 未登录：展开备用方式提交登录态（vivo 列不受影响，无需 vivo 登录）。';
  const fh = $('full-login-hint');
  if(fh) fh.style.display = v.logged_in ? 'none' : 'inline';
}
function pasteCookie(){
  const text = $('cookietext').value.trim();
  if(!text){ alert('请先粘贴 cookie 内容'); return; }
  $('login-msg').textContent = '正在验证粘贴的登录态…';
  fetch('/api/oppo/paste_cookie', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({text: text})})
    .then(r=>r.json()).then(v=>{
      renderLogin(v);
      $('cookietext').value = '';
    })
    .catch(e=>{ $('login-msg').textContent = '提交失败: ' + e; });
}
async function pasteVivoCookie(){
  const text = $('vivocookie').value.trim();
  if(!text){ $('vivo-msg').textContent = '请先粘贴 vivo cookie 内容'; return; }
  $('vivo-msg').textContent = '正在提交…';
  try {
    const r = await fetch('/api/vivo/paste_cookie', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text: text})}).then(r=>r.json());
    if(r.error){ $('vivo-msg').textContent = '失败: ' + r.error; }
    else { $('vivo-msg').textContent = '✓ 已保存，vivo 列已启用'; $('vivocookie').value = ''; }
  } catch(e){ $('vivo-msg').textContent = '请求失败: ' + e; }
}
function checkLogin(){
  $('login-msg').textContent = '正在验证登录态…';
  fetch('/api/oppo/check_login', {method:'POST'}).then(r=>r.json()).then(v=>renderLogin(v));
}
function uploadCookie(inp){
  const f = inp.files[0];
  if(!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    $('login-msg').textContent = '正在上传并验证登录文件…';
    fetch('/api/oppo/upload_cookie', {method:'POST', headers:{'Content-Type':'application/json'}, body: rd.result})
      .then(r=>r.json()).then(v=>renderLogin(v))
      .catch(e=>{ $('login-msg').textContent = '上传失败: ' + e; });
  };
  rd.readAsText(f);
}
function renderOppo(d){
  const rows = d.results || [];
  if(!rows.length) return;
  const vivoCell = r => {
    const v = r.vivo_status;
    if(!v || v === 'none') return '<span style="color:var(--sub)">' + esc(r.vivo_msg || '-') + '</span>';
    if(v === 'ok') return '<span style="color:var(--brand-dark);font-weight:600">可用</span>';
    if(v === 'dup') return '<span style="color:var(--err);font-weight:600">占用</span><div class="hint">'+esc(r.vivo_msg||'')+'</div>';
    return '<span style="color:var(--warn)">'+esc(r.vivo_msg||'未知')+'</span>';
  };
  const stTxt = r => {
    const del = r.deleted ? ' <span style="color:var(--brand-dark);font-size:11px">已删除</span>' : '';
    if(r.status === 'queued') return '<span style="color:var(--sub)">排队中</span>';
    if(r.status === 'building') return '<span style="color:var(--info)">打包中</span>';
    if(r.status === 'checking') return '<span style="color:var(--info)">验证中</span>';
    if(r.status === 'failed') return '<span style="color:var(--err)">失败</span>';
    if(r.duplicate === true) return '<span style="color:var(--err);font-weight:600">重复</span>' + del;
    if(r.duplicate === false) return '<span style="color:var(--brand-dark);font-weight:600">唯一</span>' + del;
    return '<span style="color:var(--warn)">' + esc(r.message||'未判定') + '</span>';
  };
  const dlLink = r => (r.package && (r.status === 'done' || r.status === 'failed'))
    ? '<a href="/api/apk/download?file='+encodeURIComponent(r.name+'_'+r.package+'.apk')+'">APK</a>' : '-';
  const resHTML = '<tr><th>名称</th><th>包名</th><th>app_id</th><th>结果</th><th>vivo</th><th>APK</th></tr>' +
    rows.map(r => `<tr>
      <td>${esc(r.name)}</td>
      <td class="mono">${esc(r.package||'-')}</td>
      <td class="mono">${esc(r.app_id||'-')}</td>
      <td>${stTxt(r)}${r.message && r.duplicate !== null ? '<div class="hint">'+esc(r.message)+'</div>' : ''}</td>
      <td>${vivoCell(r)}</td>
      <td>${dlLink(r)}</td>
    </tr>`).join('');
  const logs = d.logs || [];
  const logText = logs.join('\\n');
  const fin = !d.running && d.total > 0;
  let finHTML = '';
  if(fin && !oppoDone){
    oppoDone = true;
    loadApkList();
    const uniq = rows.filter(r=>r.duplicate===false).length;
    const dup = rows.filter(r=>r.duplicate===true).length;
    const apkBtn = (d.apk_files && d.apk_files.length)
      ? ` <a class="btn blue" style="margin-left:8px" href="/api/apk/download?files=${encodeURIComponent(d.apk_files.join(','))}">⬇ 本次验证 APK（${d.apk_files.length} 个）</a>` : '';
    finHTML = `完成：${d.done}/${d.total} ｜ 唯一 ${uniq} ｜ 重复 ${dup} ｜ 失败 ${d.done-uniq-dup}`+
      ` <a class="btn" style="margin-left:8px" href="/api/oppo/report">⬇ 报告 CSV</a>` + apkBtn;
  }
  if(d.running && oppoDone){ oppoDone = false; }
  ['o','f'].forEach(pre => {
    const step = $(pre+'step3'); if(!step) return;
    step.style.display = 'flex';
    const res = $(pre+'res');
    res.style.display = 'table'; res.innerHTML = resHTML;
    const logEl = $(pre+'log');
    if(logs.length){
      logEl.style.display = 'block';
      if(logs.length !== lastLogLen){ logEl.textContent = logText; logEl.scrollTop = logEl.scrollHeight; }
    }
    const rEl = $(pre+'result');
    if(finHTML){ rEl.style.display='block'; rEl.innerHTML = finHTML; }
    if(d.running && rEl) rEl.style.display='none';
  });
  if(logs.length !== lastLogLen){ lastLogLen = logs.length; }
}
async function doOppoBatch(){
  const lines = $('onames').value.split('\\n').map(s=>s.trim()).filter(Boolean);
  if(!lines.length){ alert('请填写名称列表（每行一个）'); return; }
  if(!confirm(`【仅名称判定】对 ${lines.length} 个名称直接判重（秒级）。\n使用常驻探针应用调判定接口（首次自动创建并保留），不打包、不传包、无残留。\n确定继续？`)) return;
  const go = $('gooppo');
  go.disabled = true; go.textContent = '任务已提交…';
  $('oresult').style.display = 'none'; oppoDone = false;
  try {
    const r = await fetch('/api/oppo/batch', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({names: lines, auto_delete: false, name_only: true})}).then(r=>r.json());
    if(!r.ok){ alert(r.error || '提交失败'); }
  } catch(e){ alert('请求失败: ' + e); }
  go.disabled = false; go.textContent = '开始验证';
}
async function doFullBatch(){
  const lines = $('fnames').value.split('\\n').map(s=>s.trim()).filter(Boolean);
  if(!lines.length){ alert('请填写名称列表（每行一个）'); return; }
  if(!confirm(`【完整验证】对 ${lines.length} 个名称执行：打包 → 创建应用 → 传包 → 判定 → 自动删除。\n每 5 个一批，验证并删除后继续下一批，全部完成账号零残留。\n确定继续？`)) return;
  const go = $('gofull');
  go.disabled = true; go.textContent = '任务已提交…';
  $('fresult').style.display = 'none'; oppoDone = false;
  try {
    const r = await fetch('/api/oppo/batch', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({names: lines, auto_delete: true, name_only: false})}).then(r=>r.json());
    if(!r.ok){ alert(r.error || '提交失败'); }
  } catch(e){ alert('请求失败: ' + e); }
  go.disabled = false; go.textContent = '开始完整验证';
}

/* 初始化必须放在所有 let/const 声明之后（TDZ：提前调用会 ReferenceError 中断整个脚本，
   曾导致徽章卡"检查中"、轮询不启动、打包按钮也不工作） */
loadApkList();
startOppoPoll();
</script>
</body>
</html>

"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 动态内容一律禁缓存，避免浏览器拿旧页面/旧进度
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        # 首次访问下发会话 id（批量任务按此隔离，互不可见）
        if getattr(self, "_set_sid", None):
            self.send_header("Set-Cookie", "pt_sid=%s; Path=/; Max-Age=31536000; SameSite=Lax" % self._set_sid)
        self.end_headers()
        self.wfile.write(data)

    def _session_id(self):
        """从 Cookie 取 pt_sid；没有则生成并通过 _send 下发。"""
        sid = None
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "pt_sid" and v.strip():
                sid = v.strip()[:64]
                break
        if not sid:
            sid = uuid.uuid4().hex
            self._set_sid = sid
        return sid

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/gen_pkg"):
            self._send(200, {"pkg": gen_package("app")})
        elif self.path.startswith("/api/open"):
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            path = qs.get("path", [""])[0]
            if IS_WIN and os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            self._send(200, {"ok": True})
        elif self.path.startswith("/api/apk/list"):
            removed = cleanup_apk_dir()
            files = []
            try:
                files = [f for f in os.listdir(OUTPUT_DIR) if f.lower().endswith(".apk")]
            except OSError:
                pass
            items = []
            for f in files:
                p = os.path.join(OUTPUT_DIR, f)
                try:
                    items.append({"filename": f,
                                  "size_mb": round(os.path.getsize(p) / 1048576, 2),
                                  "mtime": int(os.path.getmtime(p))})
                except OSError:
                    pass
            items.sort(key=lambda x: -x["mtime"])
            self._send(200, {"ok": True, "files": items, "cleaned": removed})
        elif self.path.startswith("/api/apk/download"):
            import urllib.parse
            import zipfile
            import io
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fname = os.path.basename(qs.get("file", [""])[0])
            files_param = qs.get("files", [""])[0]
            if files_param:
                # 打包指定文件列表（本次批量产生的 APK）
                wanted = [os.path.basename(x) for x in files_param.split(",") if x.strip()]
                exist = []
                for x in wanted:
                    p = os.path.join(OUTPUT_DIR, x)
                    if os.path.isfile(p):
                        exist.append((x, p))
                if not exist:
                    return self._send(404, {"error": "指定的 APK 均不存在"})
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for x, p in exist:
                        z.write(p, x)
                data = buf.getvalue()
                out_name = "apks_" + time.strftime("%Y%m%d_%H%M") + ".zip"
                ctype = "application/zip"
            elif fname == "__all__":
                cleanup_apk_dir()
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in sorted(os.listdir(OUTPUT_DIR)):
                        if f.lower().endswith(".apk"):
                            z.write(os.path.join(OUTPUT_DIR, f), f)
                data = buf.getvalue()
                out_name = "apks_" + time.strftime("%Y%m%d_%H%M") + ".zip"
                ctype = "application/zip"
            else:
                p = os.path.join(OUTPUT_DIR, fname)
                if not (fname and os.path.isfile(p)):
                    return self._send(404, {"error": "文件不存在: " + fname})
                with open(p, "rb") as f:
                    data = f.read()
                out_name = fname
                ctype = "application/vnd.android.package-archive"
            encoded = urllib.parse.quote(out_name)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"{encoded}\"; filename*=UTF-8''{encoded}")
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/api/oppo/state"):
            if not OPPO_ENABLED:
                return self._send(500, {"error": "OPPO 模块不可用"})
            self._send(200, oppo_server.state_snapshot())
        elif self.path.startswith("/api/oppo/progress"):
            if not OPPO_ENABLED:
                return self._send(200, {"vnc": {}, "running": False, "total": 0, "done": 0,
                                        "results": [], "logs": ["OPPO 模块未部署（缺 oppo_server.py 或 playwright）"]})
            self._send(200, oppo_server.progress(self._session_id()))
        elif self.path.startswith("/api/oppo/login_helper.zip"):
            import io as _io
            import zipfile as _zf
            import urllib.parse as _up
            here = os.path.dirname(os.path.abspath(__file__))
            bat_p = os.path.join(here, "login_oppo_dist.bat")
            py_p = os.path.join(here, "export_login.py")
            if not (os.path.exists(bat_p) and os.path.exists(py_p)):
                return self._send(404, {"error": "登录助手文件未部署"})
            readme = ("OPPO 登录助手使用说明（标签页模式）\n"
                      "======================================\n"
                      "1. 解压本压缩包到任意文件夹\n"
                      "2. 双击 login_oppo.bat（脚本自动补齐依赖）\n"
                      "3. 脚本会在你日常使用的浏览器里新开一个 OPPO 标签页（不是双开浏览器）；"
                      "若浏览器已记住 OPPO 登录，会直接自动回传，无需任何操作\n"
                      "4. 登录完成后只关闭 OPPO 那个标签页，浏览器原样保留，工具页徽章自动变绿\n\n"
                      "注意：首次运行若提示需要关闭浏览器，请关掉所有浏览器窗口后按回车，"
                      "脚本会以调试模式重新拉起并恢复原有标签页。\n"
                      "备用：老模式（独立窗口）可运行 python export_login.py\n")
            buf = _io.BytesIO()
            with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as z:
                cdp_p = os.path.join(here, "login_oppo_cdp.py")
                z.writestr("OPPO登录助手/login_oppo.bat", open(bat_p, "rb").read())
                z.writestr("OPPO登录助手/login_oppo_cdp.py", open(cdp_p, "rb").read())
                z.writestr("OPPO登录助手/export_login.py", open(py_p, "rb").read())
                z.writestr("OPPO登录助手/使用说明.txt", readme.encode("utf-8-sig"))
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            encoded = _up.quote("OPPO登录助手.zip")
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"login_helper.zip\"; filename*=UTF-8''{encoded}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/api/oppo/report"):
            data = oppo_server.read_report(self._session_id()) if OPPO_ENABLED else None
            if data is None:
                return self._send(404, {"error": "报告尚未生成"})
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", "attachment; filename=oppo_report.csv")
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/api/download"):
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            path = qs.get("path", [""])[0]
            real_out = os.path.realpath(OUTPUT_DIR)
            real_path = os.path.realpath(path)
            if not (os.path.isfile(real_path) and real_path.startswith(real_out)):
                return self._send(403, {"error": "forbidden"})
            fname = os.path.basename(real_path)
            with open(real_path, "rb") as f:
                data = f.read()
            encoded = urllib.parse.quote(fname)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"{encoded}\"; filename*=UTF-8''{encoded}")
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send(404, {"error": "not found"})

    def _api_build(self, data):
        try:
            import urllib.parse
            name = (data.get("name") or "").strip()
            pkg = (data.get("pkg") or "").strip() or gen_package(name)
            if not name:
                return self._send(400, {"ok": False, "log": ["应用名称不能为空"]})
            if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", pkg):
                return self._send(400, {"ok": False,
                                        "log": [f"包名不合法: {pkg}（应为小写字母开头的 a.b.c 形式）"]})
            result = build_apk(name, pkg)
            result["size_mb"] = round(os.path.getsize(result["output"]) / 1048576, 2)
            dl_name = os.path.basename(result["output"])
            result["download_url"] = "/api/download?path=" + urllib.parse.quote(result["output"])
            result["filename"] = dl_name
            result["is_local"] = IS_WIN
            cleanup_apk_dir()
            self._send(200, result)
        except Exception as e:
            self._send(200, {"ok": False, "log": [str(e)]})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            return self._send(400, {"error": "bad request"})

        if self.path == "/api/build":
            return self._api_build(data)

        if self.path in ("/api/oppo/start_login", "/api/oppo/check_login",
                         "/api/oppo/upload_cookie", "/api/oppo/batch",
                         "/api/oppo/paste_cookie"):
            if not OPPO_ENABLED:
                return self._send(500, {"error": "OPPO 模块不可用（服务器缺 oppo_server.py 或 playwright）"})
            if self.path == "/api/oppo/start_login":
                return self._send(200, oppo_server.start_login())
            if self.path == "/api/oppo/check_login":
                return self._send(200, oppo_server.check_login())
            if self.path == "/api/oppo/upload_cookie":
                try:
                    oppo_server.save_cookie_state(data)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, oppo_server.check_login())
            if self.path == "/api/oppo/paste_cookie":
                try:
                    oppo_server.save_pasted_cookies(str(data.get("text") or ""))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, oppo_server.check_login())
            if self.path == "/api/vivo/paste_cookie":
                try:
                    return self._send(200, oppo_server.save_vivo_cookie(str(data.get("text") or "")))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:
                    return self._send(500, {"error": str(e)[:150]})
            names = [str(n).strip() for n in (data.get("names") or []) if str(n).strip()]
            if not names:
                return self._send(400, {"ok": False, "error": "名称列表为空"})
            return self._send(200, oppo_server.start_batch(names, bool(data.get("auto_delete")),
                                                           bool(data.get("name_only")),
                                                           self._session_id()))

        return self._send(404, {"error": "not found"})


def start_gui(host="127.0.0.1", port=0):
    import threading
    import webbrowser
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{server.server_address[1]}"
    print(f"打包工具已启动: {url}  (Ctrl+C 退出)", flush=True)
    if IS_WIN or host == "127.0.0.1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    args = sys.argv[1:]
    if "--cli" in args:
        import argparse
        ap = argparse.ArgumentParser(description="快速打包 APK")
        ap.add_argument("--name", required=True, help="应用名称")
        ap.add_argument("--package", default="", help="包名(默认自动生成)")
        ap.add_argument("--out", default="", help="输出目录")
        a = ap.parse_args(args[args.index("--cli") + 1:])
        pkg = a.package or gen_package(a.name)
        r = build_apk(a.name, pkg, out_dir=a.out or None)
        print("\n".join(r["log"]))
        sys.exit(0 if r["ok"] else 1)
    import argparse
    ap = argparse.ArgumentParser(description="快速打包工具 (网页 GUI)")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址(服务器部署用 0.0.0.0)")
    ap.add_argument("--port", type=int, default=0, help="监听端口(默认随机)")
    a = ap.parse_args(args)
    start_gui(host=a.host, port=a.port)


if __name__ == "__main__":
    main()
