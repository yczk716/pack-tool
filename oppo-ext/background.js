// OPPO 登录态回传助手 - MV3 service worker
// 触发方式：①工具页按钮（onMessageExternal）②popup 按钮（onMessage）③每 30 分钟自动（alarms）
const SERVER = "http://101.43.50.231:8000";
const ALARM = "autoPush";

function mapSameSite(ss) {
  if (ss === "no_restriction") return "None";
  if (ss === "strict") return "Strict";
  return "Lax"; // lax / unspecified
}

// 合并 oppomobile.com 与 oppo.com 两个域的 cookie，按 domain|path|name 去重
async function getAllRaw() {
  const [a, b] = await Promise.all([
    chrome.cookies.getAll({ domain: "oppomobile.com" }),
    chrome.cookies.getAll({ domain: "oppo.com" })
  ]);
  const seen = new Map();
  for (const c of a.concat(b)) {
    const key = c.domain + "|" + c.path + "|" + c.name;
    if (!seen.has(key)) seen.set(key, c);
  }
  return Array.from(seen.values());
}

// 输出 Playwright storage_state 兼容格式（与 export_login.py / login_oppo_cdp.py 一致）
async function collectCookies() {
  const raw = await getAllRaw();
  const cookies = raw.map(function (c) {
    return {
      name: c.name,
      value: c.value,
      domain: c.domain,
      path: c.path,
      expires: typeof c.expirationDate === "number" ? c.expirationDate : -1,
      httpOnly: !!c.httpOnly,
      secure: !!c.secure,
      sameSite: mapSameSite(c.sameSite)
    };
  });
  return { cookies: cookies, origins: [] };
}

function setBadge(text, color) {
  try {
    chrome.action.setBadgeText({ text: String(text) });
    chrome.action.setBadgeBackgroundColor({ color: color || "#409eff" });
    setTimeout(function () { chrome.action.setBadgeText({ text: "" }); }, 60000);
  } catch (e) { /* ignore */ }
}

async function pushNow(reason) {
  try {
    const state = await collectCookies();
    const n = state.cookies.length;
    if (!n) {
      setBadge("!", "#e6a23c");
      return { ok: false, count: 0, message: "浏览器里没有 OPPO 相关 cookie，请先登录 open.oppomobile.com" };
    }
    const r = await fetch(SERVER + "/api/oppo/upload_cookie", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state)
    });
    const data = await r.json();
    if (r.ok && data.logged_in) {
      setBadge("OK", "#67c23a");
      return { ok: true, count: n, message: "已回传 " + n + " 条 cookie，服务器登录态有效" };
    }
    setBadge("X", "#f56c6c");
    return { ok: false, count: n, message: "已回传 " + n + " 条，但服务器校验未通过：" + (data.error || data.login_msg || ("HTTP " + r.status)) };
  } catch (e) {
    setBadge("X", "#f56c6c");
    return { ok: false, count: 0, message: "回传失败：" + e.message };
  }
}

// 工具页（http://101.43.50.231:8000/*）网页按钮直接触发
chrome.runtime.onMessageExternal.addListener(function (msg, sender, sendResponse) {
  if (msg && msg.type === "push_oppo") {
    pushNow("web").then(sendResponse);
    return true; // 异步应答
  }
  sendResponse({ ok: false, message: "未知消息类型" });
});

// popup 按钮触发
chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (msg && msg.type === "push_oppo") {
    pushNow("popup").then(sendResponse);
    return true;
  }
});

// 每 30 分钟自动回传一次（登录态长期有效则完全免操作）
chrome.alarms.create(ALARM, { periodInMinutes: 30 });
chrome.alarms.onAlarm.addListener(function (a) {
  if (a.name === ALARM) pushNow("auto");
});
