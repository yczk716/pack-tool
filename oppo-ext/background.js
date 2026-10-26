// OPPO 登录态回传助手 - MV3 service worker（标准 API，兼容 Chrome / Edge）
// 设计：无通知、无页面打扰。仅三类动作：
// ① 每 30 分钟静默回传一次最新 cookie（跟踪 OPPO 的 token 轮换，失败静默跳过，全程无感）
// ② 手动回传：先问服务器"现在到底是不是失效了"，只有确认失效才打开登录页
// ③ 登录页标签打开期间检测到 sdkLoginToken 变化（用户完成登录）→ 自动回传 → 关登录页 → 回工具页续跑验证
const SERVER = "http://101.43.50.231:8000";
const ALARM = "silentPush";
const LOGIN_URL = "https://open.oppomobile.com/public/login/login_page.html";
const LOGIN_KEY = "loginTabId"; // 存 chrome.storage.session：service worker 会被回收，内存变量不可靠
let loginTabId = null;          // 当前 SW 生命期内的缓存（持久值见 storage.session）
let loginWaitTimer = null;
let loginPushing = false;       // 登录后的回传进行中，避免重复触发

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

// ---- 登录页标签 id 持久化（MV3 SW 空闲 30s 被回收，只用内存变量会"忘了在等登录"）----
async function getLoginTabId() {
  if (loginTabId != null) return loginTabId;
  try {
    const o = await chrome.storage.session.get(LOGIN_KEY);
    const v = o && o[LOGIN_KEY];
    if (typeof v === "number") { loginTabId = v; return v; }
  } catch (e) { /* storage 不可用时退化为内存 */ }
  return null;
}

async function setLoginTabId(id) {
  loginTabId = id;
  try {
    if (id == null) await chrome.storage.session.remove(LOGIN_KEY);
    else await chrome.storage.session.set({ [LOGIN_KEY]: id });
  } catch (e) { /* ignore */ }
}

// 打开（或复用）OPPO 登录页标签
async function openLoginTab() {
  const known = await getLoginTabId();
  if (known != null) {
    try {
      const t = await chrome.tabs.get(known);
      await chrome.tabs.update(known, { active: true });
      await chrome.windows.update(t.windowId, { focused: true });
      return;
    } catch (e) {
      await setLoginTabId(null); // 标签已被关闭
    }
  }
  try {
    const tab = await chrome.tabs.create({ url: LOGIN_URL });
    await setLoginTabId(tab.id);
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

// 向服务器确认：服务器自己保存的登录态现在是否还有效（权威判定，1~3 秒）
// 返回 true / false / null（null = 服务器不可达或返回异常，无法判定）。
// 关键：null 绝不能被当成"失效"，否则服务器抖一下就会弹出登录页。
async function serverSessionAlive() {
  try {
    const r = await fetch(SERVER + "/api/oppo/check_login", { method: "POST" });
    if (!r.ok) return null;
    const d = await r.json();
    if (!d || typeof d.logged_in !== "boolean") return null;
    return d.logged_in;
  } catch (e) {
    return null;
  }
}

// 收集并回传 cookie。
// autoOpen=true 时：只有"浏览器没登录 + 服务器也确认失效"才打开登录页；
// 服务器登录态仍有效时绝不跳登录页（此前只要回传没通过就跳，导致有效会话被误判）。
async function pushNow(autoOpen) {
  try {
    const state = await collectCookies();
    const n = state.cookies.length;

    if (!n) {
      // 本浏览器没有 OPPO cookie：可能只是没在这个浏览器登录过，服务器上却是有效的
      const alive0 = await serverSessionAlive();
      if (alive0 === true) {
        setBadge("OK", "#67c23a");
        return { ok: true, count: 0,
                 message: "服务器登录态仍然有效（本浏览器没有 OPPO cookie，无需回传，也不会跳登录页）" };
      }
      if (alive0 === null) {
        setBadge("!", "#e6a23c");
        return { ok: false, count: 0,
                 message: "本浏览器没有 OPPO cookie，且暂时无法连接验证服务器，请稍后重试（未打开登录页）" };
      }
      setBadge("!", "#e6a23c");
      if (autoOpen) await openLoginTab();
      return { ok: false, count: 0, need_login: true,
               message: "浏览器里没有可用的 OPPO 登录信息，服务器登录态也已失效，请先登录" };
    }

    const r = await fetch(SERVER + "/api/oppo/upload_cookie", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state)
    });
    let data = {};
    try { data = await r.json(); } catch (e) { data = {}; }

    if (r.ok && data.logged_in) {
      setBadge("OK", "#67c23a");
      return { ok: true, count: n, message: "已回传 " + n + " 条 cookie，服务器登录态有效" };
    }

    // 回传被拒：先分清是"真失效"还是"这份 cookie 不可用但服务器另有有效登录态"
    const dead = data && data.session_dead === true;
    if (!dead) {
      const alive = await serverSessionAlive();
      if (alive === true) {
        setBadge("OK", "#67c23a");
        return { ok: true, count: n,
                 message: "服务器登录态有效，本次回传的 cookie 未通过校验已被忽略（不会覆盖现有登录态）" };
      }
      if (alive === null) {
        setBadge("!", "#e6a23c");
        return { ok: false, count: n,
                 message: "本次回传未通过校验，且暂时无法确认服务器登录态（服务器不可达），请稍后重试（未打开登录页）" };
      }
    }

    setBadge("X", "#f56c6c");
    if (autoOpen) await openLoginTab();
    return { ok: false, count: n, need_login: true,
             message: "登录态已失效：" + (data.error || data.login_msg || ("HTTP " + r.status)) };
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

// 登录页被用户关掉：清掉等待标记，避免下次 cookie 变化时误触发回传/跳转
chrome.tabs.onRemoved.addListener(function (tabId) {
  getLoginTabId().then(function (cur) {
    if (cur === tabId) setLoginTabId(null);
  }).catch(function () { });
});

// 登录成功检测：登录页标签存在期间 sdkLoginToken 出现/更新 → 自动回传 → 关登录页 → 回工具页续跑
chrome.cookies.onChanged.addListener(function (info) {
  const c = info.cookie;
  if (!c || c.name !== "sdkLoginToken" || !c.value) return;
  if (loginPushing) return;
  getLoginTabId().then(function (tid) {
    if (tid == null) return; // 没有在等待登录
    clearTimeout(loginWaitTimer);
    loginWaitTimer = setTimeout(async function () {
      if (loginPushing) return;
      loginPushing = true;
      try {
        const r = await pushNow(false);
        if (r.ok) {
          await setLoginTabId(null);
          try { await chrome.tabs.remove(tid); } catch (e) { /* 已关闭 */ }
          focusToolPage("/?autostart=1");
        }
        // 回传未通过：保留 loginTabId，继续等下一次 token 变化
      } finally {
        loginPushing = false;
      }
    }, 2500); // 等 cookie 全部落定
  }).catch(function () { });
});

// 每 30 分钟静默回传：只上传浏览器最新 cookie，跟踪 OPPO 的 token 轮换。
// 不弹通知、不打开页面、不做任何检测判定；无 cookie 或服务器拒绝时静默跳过。
async function pushSilent() {
  try {
    const state = await collectCookies();
    if (!state.cookies.length) return; // 浏览器未登录，静默跳过
    const r = await fetch(SERVER + "/api/oppo/upload_cookie", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state)
    });
    if (r.ok) {
      const data = await r.json();
      if (data.logged_in) {
        try { chrome.action.setBadgeText({ text: "" }); } catch (e) { /* ignore */ }
      }
      // 校验未通过：静默跳过（可能正在登录中，等下一轮或登录检测回传）
    }
  } catch (e) { /* 静默跳过 */ }
}

// 只在没有同类闹钟时创建：SW 每次被唤醒都会执行本文件，无条件 create 会把 30 分钟倒计时
// 不断重置（用户频繁访问 OPPO 时静默回传可能永远不触发）。
chrome.alarms.get(ALARM, function (a) {
  if (!a) chrome.alarms.create(ALARM, { periodInMinutes: 30 });
});
chrome.alarms.onAlarm.addListener(function (a) {
  if (a.name === ALARM) pushSilent();
});
