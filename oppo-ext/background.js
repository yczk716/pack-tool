// OPPO 登录态回传助手 - MV3 service worker（标准 API，兼容 Chrome / Edge）
// 设计：完全被动——不弹通知、不自动定时检测，只在两种情况下动作：
// ① 用户手动触发（popup 按钮 / 工具页⚡扩展回传 / 工具页验证时触发 login_redirect）
// ② 登录页标签打开期间检测到 sdkLoginToken 变化（用户完成登录）→ 自动回传 → 关登录页 → 回工具页续跑验证
const SERVER = "http://101.43.50.231:8000";
const LOGIN_URL = "https://open.oppomobile.com/public/login/login_page.html";
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

// 输出 Playwright storage_state 兼容格式
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
    setTimeout(function () { chrome.action.setBadgeText({ text: "" }); }, 30000);
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

// 回到工具页（可带 autostart=1 让页面自动续跑挂起的验证任务）
async function focusToolPage(path) {
  try {
    const url = SERVER + (path || "/");
    const tabs = await chrome.tabs.query({ url: SERVER + "/*" });
    let tab;
    if (tabs && tabs.length) {
      tab = tabs[0];
      await chrome.tabs.update(tab.id, { active: true, url: url });
    } else {
      tab = await chrome.tabs.create({ url: url });
    }
    await chrome.windows.update(tab.windowId, { focused: true });
  } catch (e) { /* ignore */ }
}

// 收集并回传 cookie；autoOpen=true 时失效自动打开登录页
async function pushNow(autoOpen) {
  try {
    const state = await collectCookies();
    const n = state.cookies.length;
    if (!n) {
      setBadge("!", "#e6a23c");
      if (autoOpen) openLoginTab();
      return { ok: false, count: 0, need_login: true, message: "浏览器里没有 OPPO 相关 cookie，请先登录" };
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
    if (autoOpen) openLoginTab();
    return { ok: false, count: n, need_login: true, message: "登录态校验未通过：" + (data.error || data.login_msg || ("HTTP " + r.status)) };
  } catch (e) {
    setBadge("X", "#f56c6c");
    return { ok: false, count: 0, message: "回传失败：" + e.message };
  }
}

// 网页触发（工具页按钮：push_oppo 手动回传 / login_redirect 打开登录页）
chrome.runtime.onMessageExternal.addListener(function (msg, sender, sendResponse) {
  if (msg && msg.type === "push_oppo") {
    pushNow(!!msg.autoOpen).then(sendResponse);
    return true;
  }
  if (msg && msg.type === "login_redirect") {
    openLoginTab();
    sendResponse({ ok: true, message: "已打开 OPPO 登录页，登录完成后自动返回" });
    return;
  }
  sendResponse({ ok: false, message: "未知消息类型" });
});

// popup 触发
chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (msg && msg.type === "push_oppo") {
    pushNow(!!msg.autoOpen).then(sendResponse);
    return true;
  }
});

// 登录成功检测：登录页标签打开期间 sdkLoginToken 出现/更新 → 自动回传 → 关登录页 → 回工具页续跑
chrome.cookies.onChanged.addListener(function (info) {
  const c = info.cookie;
  if (!c || c.name !== "sdkLoginToken" || !c.value) return;
  if (loginTabId == null) return; // 没有在等待登录
  clearTimeout(loginWaitTimer);
  loginWaitTimer = setTimeout(async function () {
    const tid = loginTabId;
    loginTabId = null;
    const r = await pushNow(false);
    if (r.ok) {
      if (tid != null) {
        try { await chrome.tabs.remove(tid); } catch (e) { /* 已关闭 */ }
      }
      focusToolPage("/?autostart=1");
    } else {
      loginTabId = tid; // 回传未通过，继续等待
    }
  }, 2500); // 等 cookie 全部落定
});
