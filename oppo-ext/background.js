// OPPO 登录态回传助手 - MV3 service worker（标准 API，兼容 Chrome / Edge 等 Chromium 浏览器）
// 触发方式：①工具页按钮/popup（可带 autoOpen）②每 10 分钟自动（alarms）
// 失效处理：手动回传若服务器判定失效 → 自动打开 OPPO 登录页 → 登录成功后自动回传并关闭登录页
const SERVER = "http://101.43.50.231:8000";
const ALARM = "autoPush";
const LOGIN_URL = "https://open.oppomobile.com/public/login/login_page.html";
const NOTIF_ID = "oppo-expired";
let loginTabId = null;
let loginWaitTimer = null;

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

// 输出 Playwright storage_state 兼容格式（与脚本回传一致）
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

function notify(title, message) {
  try {
    chrome.notifications.create(NOTIF_ID, {
      type: "basic",
      iconUrl: "icon128.png",
      title: title,
      message: message,
      priority: 2,
      requireInteraction: true
    });
  } catch (e) { /* ignore */ }
}

function clearExpiredFlag() {
  try {
    chrome.storage.local.get({ expired: 0 }, function (v) {
      if (v.expired) {
        chrome.storage.local.remove("expired");
        chrome.notifications.clear(NOTIF_ID);
      }
    });
  } catch (e) { /* ignore */ }
}

// 打开（或复用）OPPO 登录页标签
async function openLoginTab() {
  if (loginTabId != null) {
    try {
      const t = await chrome.tabs.get(loginTabId);
      await chrome.tabs.update(loginTabId, { active: true });
      await chrome.windows.update(t.windowId, { focused: true });
      return;
    } catch (e) {
      loginTabId = null; // 标签已被关闭
    }
  }
  try {
    const tab = await chrome.tabs.create({ url: LOGIN_URL });
    loginTabId = tab.id;
  } catch (e) { /* ignore */ }
}

// 登录恢复成功后：激活/新建工具页标签并聚焦，让用户直接回到工具页
async function focusToolPage() {
  try {
    const tabs = await chrome.tabs.query({ url: SERVER + "/*" });
    let tab;
    if (tabs && tabs.length) {
      tab = tabs[0];
      await chrome.tabs.update(tab.id, { active: true });
    } else {
      tab = await chrome.tabs.create({ url: SERVER + "/" });
    }
    await chrome.windows.update(tab.windowId, { focused: true });
  } catch (e) { /* ignore */ }
}

async function pushNow(reason, autoOpen) {
  try {
    const state = await collectCookies();
    const n = state.cookies.length;
    if (!n) {
      setBadge("!", "#e6a23c");
      notify("OPPO 登录态缺失", "浏览器里没有 OPPO cookie，请先登录 open.oppomobile.com");
      if (autoOpen) openLoginTab();
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
      clearExpiredFlag();
      return { ok: true, count: n, message: "已回传 " + n + " 条 cookie，服务器登录态有效" };
    }
    setBadge("X", "#f56c6c");
    const detail = data.error || data.login_msg || ("HTTP " + r.status);
    if (autoOpen) {
      notify("OPPO 登录态已失效", "已自动打开 OPPO 登录页，请完成登录（浏览器会自动填充账号），成功后自动回传并返回工具页");
      openLoginTab();
      return { ok: false, count: n, message: "登录态已失效，已自动打开 OPPO 登录页（登录后自动回传并返回工具页）：" + detail };
    }
    notify("OPPO 登录态已失效", "点击本通知打开 OPPO 登录页重新登录；登录后扩展会自动回传并返回工具页");
    return { ok: false, count: n, message: "已回传 " + n + " 条，但服务器校验未通过：" + detail };
  } catch (e) {
    setBadge("X", "#f56c6c");
    return { ok: false, count: 0, message: "回传失败：" + e.message };
  }
}

// 网页按钮 / popup 触发
chrome.runtime.onMessageExternal.addListener(function (msg, sender, sendResponse) {
  if (msg && msg.type === "push_oppo") {
    pushNow("web", !!msg.autoOpen).then(sendResponse);
    return true; // 异步应答
  }
  sendResponse({ ok: false, message: "未知消息类型" });
});

chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (msg && msg.type === "push_oppo") {
    pushNow("popup", !!msg.autoOpen).then(sendResponse);
    return true;
  }
});

// 通知点击 → 直达 OPPO 登录页
chrome.notifications.onClicked.addListener(function (id) {
  if (id === NOTIF_ID) {
    openLoginTab();
    chrome.notifications.clear(NOTIF_ID);
  }
});

// 登录成功检测：sdkLoginToken 一旦出现/更新（说明用户刚完成登录）→ 自动回传 → 关登录页 → 返回工具页
chrome.cookies.onChanged.addListener(function (info) {
  const c = info.cookie;
  if (!c || c.name !== "sdkLoginToken" || !c.value) return;
  if (loginTabId == null) return; // 没有在等待登录
  clearTimeout(loginWaitTimer);
  loginWaitTimer = setTimeout(async function () {
    const tid = loginTabId;
    loginTabId = null;
    const r = await pushNow("after-login", false);
    if (r.ok) {
      notify("OPPO 登录态已恢复", "新登录态已自动回传，正在返回工具页");
      if (tid != null) {
        try { await chrome.tabs.remove(tid); } catch (e) { /* 已关闭 */ }
      }
      focusToolPage();
    } else {
      loginTabId = tid; // 回传未通过，继续等待用户操作
    }
  }, 2500); // 等 cookie 全部落定
});

// 每 10 分钟自动回传一次（失效可尽快发现并提醒；登录后自动恢复）
chrome.alarms.create(ALARM, { periodInMinutes: 10 });
chrome.alarms.onAlarm.addListener(function (a) {
  if (a.name === ALARM) pushNow("auto", false);
});
