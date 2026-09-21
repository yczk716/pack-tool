const $ = function (id) { return document.getElementById(id); };

async function init() {
  try {
    const [a, b] = await Promise.all([
      chrome.cookies.getAll({ domain: "oppomobile.com" }),
      chrome.cookies.getAll({ domain: "oppo.com" })
    ]);
    const seen = new Set();
    a.concat(b).forEach(function (c) { seen.add(c.domain + "|" + c.path + "|" + c.name); });
    $("count").textContent = seen.size;
  } catch (e) {
    $("count").textContent = "?";
  }
}

$("push").addEventListener("click", async function () {
  const btn = $("push");
  btn.disabled = true;
  const st = $("status");
  st.style.display = "block";
  st.style.background = "#f4f4f5";
  st.textContent = "回传中…";
  try {
    const r = await chrome.runtime.sendMessage({ type: "push_oppo" });
    st.textContent = (r && r.message) ? r.message : "未知状态";
    if (r && r.ok) st.style.background = "#f0f9eb";
    else if (r) st.style.background = "#fef0f0";
  } catch (e) {
    st.textContent = "失败：" + e.message;
    st.style.background = "#fef0f0";
  }
  btn.disabled = false;
  init();
});

init();
