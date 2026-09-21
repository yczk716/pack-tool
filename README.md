# APK 打包与 OPPO/vivo 重名验证工具

输入应用名称即可批量完成：**APK 打包**（安卓 WebView 壳应用）、**OPPO/vivo 平台重名判定**、**完整验证流程**（创建应用→传包→判定→自动删除）。部署于云服务器，浏览器直接使用。

## 功能

- ✅ **OPPO 重名验证**（仅名称判定）：用常驻探针应用直接调 `app/appname` 判定接口，秒级返回名称是否被占用，不打包、不创建、零残留。结果表同时给出 **vivo 列**（调 vivo 开放平台 `verify-app-cn-name`，实测无需登录态）。
- 🔍 **完整验证**：每 5 个一批「创建应用 → 打包上传 → 判定 → 自动删除」，数量不限，账号零残留。
- 📦 **打包下载**：每行一个名称（支持 `名称|自定义包名`），5 个一组并行打包，一键下载本次打包 APK（zip）。
- 登录态：OPPO 会话按浏览器会话隔离显示；vivo 判定无需登录。
- 服务器 APK 仅保留最近 10 个，超出自动清理。

## 多人使用

批量任务的进度/结果/日志/报告**按浏览器会话隔离**（`pt_sid` cookie），不同的人互不干扰；OPPO 登录态与探针应用全局共享。

## 登录方式

OPPO 判定需要开发者 cookie（HttpOnly，网页无法直接获取）：

1. 页面下载「登录助手」zip → 解压双击 `login_oppo.bat` → 弹出 Edge 登录 → 自动回传服务器（脚本自动补齐依赖，复用登录记录多数情况免密）
2. 备用：F12 复制 cookie 整行粘贴，或上传 `storage_state.json`

## 部署（服务器 101.43.50.231:8000）

```bash
# 文件位于 /opt/pack-tool/，systemd 服务 packtool.service
sudo systemctl restart packtool.service
```

依赖：Python 3.9+，`requests`、`playwright`（服务器端可选，登录助手本地用）。

## 文件

| 文件 | 说明 |
|---|---|
| `pack_tool.py` | HTTP 服务 + 前端页面（单文件） |
| `oppo_server.py` | OPPO 批量任务（会话隔离、探针、报告） |
| `oppo_api.py` | OPPO 接口封装（gensign/appname/judge） |
| `vivo_api.py` | vivo 判名接口封装 |
| `export_login.py` | 本地登录助手（Playwright Edge 自动回传） |
| `login_oppo_dist.bat` | 登录助手启动脚本（自动找 python/装依赖） |

## 关键实现备注

- OPPO 重名判定真实链：`app/verify-info`（仅 APK 校验）→ 前端语义的 `app/appname`：`data.app_name` 1=重复 0=可用；探针应用持久化于 `probe_app.json`，跨批复用。
- vivo 判名：`GET /webapi/app/verify-app-cn-name?mainTitle=&packageName=`，`code 0`=可用、`20219`=占用（实测无需登录）。
- OPPO `300001` 语义复用（登录失效/record not found/包名重复），仅 message 含"登录"按会话失效处理；gensign 探测有瞬时 800003 抖动，失败需二次确认。
