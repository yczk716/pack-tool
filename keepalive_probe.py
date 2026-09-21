# -*- coding: utf-8 -*-
"""保活实验：每 5 分钟用当前登录态 gensign 探测一次，记录失效时刻。
如果会话在高频活跃访问下仍然失效 → 证明固定时效（滑动过期不成立）；
如果能长期存活 → 滑动过期成立，服务器常驻浏览器方案可行。
"""
import time
import traceback

import sys
sys.path.insert(0, "/opt/pack-tool")

from oppo_api import OppoApi

LOG = "/opt/pack-tool/keepalive_probe.log"
INTERVAL = 300  # 5 分钟


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    log("=== 保活实验启动（每 %d 秒探测一次）===" % INTERVAL)
    while True:
        try:
            api = OppoApi(storage_state="/opt/pack-tool/oppo_state.json", log=lambda m: None)
            ok = bool(api.gensign())
            log("probe ok=%s" % ok)
            if not ok:
                time.sleep(90)  # 防 800003 瞬时误报，二次确认
                api = OppoApi(storage_state="/opt/pack-tool/oppo_state.json", log=lambda m: None)
                ok = bool(api.gensign())
                log("probe recheck ok=%s" % ok)
                if not ok:
                    log(">>> 会话确认失效，实验结束")
                    break
        except Exception:
            log("probe error: " + traceback.format_exc(limit=1)[:200])
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
