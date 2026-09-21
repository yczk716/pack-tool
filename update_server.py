# -*- coding: utf-8 -*-
"""仅更新服务器上的 pack_tool.py 并重启服务（区别于全量部署 deploy_remote.py）。"""
import os
import sys
import io
import time
import paramiko

HOST = "101.43.50.231"
PEM = r"D:\Downloads\work.pem"
FILES = ["pack_tool.py", "oppo_server.py", "oppo_api.py", "vivo_api.py",
         "oppo-ext/manifest.json", "oppo-ext/background.js",
         "oppo-ext/popup.html", "oppo-ext/popup.js"]  # 相对 pack-tool/，上传到 /opt/pack-tool/
PORT = 8000


def sh(ssh, cmd, timeout=120):
    print(f"\n$ {cmd}", flush=True)
    transport = ssh.get_transport()
    chan = transport.open_session()
    chan.settimeout(timeout)
    chan.exec_command(cmd)
    buf = b""
    last = time.time()
    while True:
        if chan.recv_ready():
            buf += chan.recv(4096)
            last = time.time()
        if chan.recv_stderr_ready():
            buf += chan.recv_stderr(4096)
            last = time.time()
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        if time.time() - last > 60:
            break
        time.sleep(0.2)
    while chan.recv_ready():
        buf += chan.recv(4096)
    while chan.recv_stderr_ready():
        buf += chan.recv_stderr(4096)
    text = buf.decode("utf-8", errors="replace")
    if text.strip():
        print(text[-2000:], flush=True)
    code = chan.recv_exit_status()
    print(f"[exit {code}]", flush=True)
    return code, text


def precheck_pack_tool():
    """上传前校验 pack_tool.py：①Python 语法 ②PAGE 内 JS 语法（node --check）。

    背景：PAGE 是非原始三引号字符串，JS 里的 \\n 等转义要写双反斜杠；
    写错会导致浏览器端 SyntaxError、整个页面脚本失效（tabs/按钮全部无反应）。
    """
    import ast
    import re
    import subprocess
    import tempfile
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pack_tool.py")
    tree = ast.parse(io.open(local, encoding="utf-8").read())
    page = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "PAGE":
            page = node.value.value
            break
    if page:
        m = re.search(r"<script>(.*)</script>", page, re.S)
        if m:
            node_exe = r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
            if os.path.exists(node_exe):
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
                    tf.write(m.group(1))
                    tmp = tf.name
                r = subprocess.run([node_exe, "--check", tmp], capture_output=True, text=True)
                os.unlink(tmp)
                if r.returncode != 0:
                    sys.exit("PAGE 内 JS 语法校验失败，已阻止部署：\n" + r.stderr[:3000])
                print("PAGE 内 JS 语法校验通过 (node --check)", flush=True)


def main():
    if not os.path.exists(PEM):
        sys.exit(f"私钥不存在: {PEM}")
    precheck_pack_tool()
    key = paramiko.RSAKey.from_private_key_file(PEM)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    user = None
    for u in ("root", "ubuntu", "lighthouse"):
        try:
            print(f"尝试登录 {u}@{HOST} ...", flush=True)
            ssh.connect(HOST, 22, username=u, pkey=key, timeout=15, banner_timeout=15)
            user = u
            break
        except paramiko.AuthenticationException:
            continue
        except Exception as e:
            print(f"{u}: 连接失败 {e}", flush=True)
    if not user:
        sys.exit("所有用户名认证失败")
    print(f"登录成功: {user}@{HOST}", flush=True)

    sftp = ssh.open_sftp()
    here = os.path.dirname(os.path.abspath(__file__))
    for fname in FILES:
        local = os.path.join(here, fname)
        if not os.path.exists(local):
            print(f"!! 跳过（本地不存在）: {fname}", flush=True)
            continue
        remote = "/opt/pack-tool/" + fname
        rdir = os.path.dirname(remote)
        try:
            sftp.stat(rdir)
        except FileNotFoundError:
            sftp.mkdir(rdir)
            print(f"已创建远端目录 {rdir}", flush=True)
        sftp.put(local, remote)
        print(f"已上传 {fname}", flush=True)
    sftp.close()

    sudo = "sudo " if user != "root" else ""
    sh(ssh, f"{sudo}systemctl restart packtool.service")
    time.sleep(2)
    sh(ssh, f"{sudo}systemctl is-active packtool.service")
    sh(ssh, f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{PORT}/ && echo ''")
    sh(ssh, f"curl -s http://127.0.0.1:{PORT}/ | grep -c 'OPPO 批量验证' || true")
    sh(ssh, f"curl -s http://127.0.0.1:{PORT}/api/oppo/state && echo ''")
    print("=== 更新完成 ===", flush=True)
    ssh.close()


if __name__ == "__main__":
    main()
