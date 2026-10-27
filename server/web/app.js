/* 自动传单派发 · 管理台前端逻辑（原生 JS，无框架，多用户版） */
"use strict";

const TOKEN_KEY = "flyer_token";
let token = localStorage.getItem(TOKEN_KEY) || "";
let me = { username: "", role: "user", target: "", users: [] };  // /api/me 结果
let activeTab = "status";
let statusTimer = null;
let logTimer = null;
let bindTimer = null;
let logOffset = 0;
let logFile = "";
let logFollow = true;
let imagesCache = [];          // [{name, size, in_list}]
let editingGid = null;         // 群弹层编辑中的群号
let categoriesCache = {};      // 分类缓存 {id: {name, enabled, ...}}
let editingCatId = null;       // 分类弹层编辑中的分类 ID

/* ─────────── 通用 API 封装 ─────────── */
async function api(path, { method = "GET", body = null, isForm = false } = {}) {
  const headers = {};
  if (token) headers["Authorization"] = "Bearer " + token;
  if (body && !isForm) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, {
    method,
    headers,
    body: isForm ? body : (body ? JSON.stringify(body) : undefined),
  });
  if (resp.status === 401) {
    token = "";
    localStorage.removeItem(TOKEN_KEY);
    showLogin();
    throw new Error("登录已过期，请重新登录");
  }
  let data = {};
  try { data = await resp.json(); } catch (e) { /* 非 JSON 响应 */ }
  if (!resp.ok) {
    const err = new Error(data.error || ("请求失败（HTTP " + resp.status + "）"));
    err.status = resp.status;
    throw err;
  }
  return data;
}

/* ─────────── Toast ─────────── */
let toastTimer = null;
function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
}

/* ─────────── 视图切换 ─────────── */
function showLogin() {
  document.getElementById("login-view").classList.remove("hidden");
  document.getElementById("main-view").classList.add("hidden");
  stopTimers();
}
function showMain() {
  document.getElementById("login-view").classList.add("hidden");
  document.getElementById("main-view").classList.remove("hidden");
  // 登录后先绑定 QQ（未绑定不能管理）；admin 可跳过进入管理台
  if (!me.bound) {
    showBindGuide(me.role === "admin");
  } else {
    showDashboard();
  }
}
function showDashboard() {
  document.getElementById("bind-guide").classList.add("hidden");
  document.querySelector(".tabs").classList.remove("hidden");
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("hidden"));
  switchTab("status");
}
function showBindGuide(canSkip) {
  document.querySelector(".tabs").classList.add("hidden");
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.add("hidden"));
  document.getElementById("bind-guide").classList.remove("hidden");
  document.getElementById("bind-guide-skip").classList.toggle("hidden", !canSkip);
  document.getElementById("bind-guide-body").classList.remove("hidden");
  document.getElementById("bind-guide-url").classList.add("hidden");
  document.getElementById("bind-guide-status").textContent = "";
  document.getElementById("bind-guide-error").textContent = "";
}

/* ─────────── 用户信息与角色感知 ─────────── */
async function loadMe() {
  me = await api("/api/me");
  renderUserUI();
}
function renderUserUI() {
  const isAdmin = me.role === "admin";
  // 用户管理 tab 仅管理员可见
  const usersTab = document.querySelector('.tab[data-tab="users"]');
  if (usersTab) usersTab.classList.toggle("hidden", !isAdmin);
  // topbar 当前用户
  document.getElementById("user-info").textContent =
    `当前用户：${me.username}` + (isAdmin ? "（管理员）" : "");
  // 切换器（admin 专属）：查看目标用户
  const sel = document.getElementById("user-switcher");
  if (isAdmin && Array.isArray(me.users)) {
    sel.classList.remove("hidden");
    const prev = sel.value;
    sel.innerHTML = "";
    const meOpt = document.createElement("option");
    meOpt.value = me.username;
    meOpt.textContent = "查看：本人（admin）";
    sel.appendChild(meOpt);
    for (const u of me.users) {
      if (u.username === me.username) continue;
      const o = document.createElement("option");
      o.value = u.username;
      o.textContent = "查看：" + u.username + (u.online ? "（在线）" : "");
      sel.appendChild(o);
    }
    if (me.target && [...sel.options].some(o => o.value === me.target)) {
      sel.value = me.target;
    }
    sel.onchange = switchUser;      // 覆盖式绑定，避免重复监听
  } else {
    sel.classList.add("hidden");
    sel.innerHTML = "";
  }
}
async function switchUser() {
  const sel = document.getElementById("user-switcher");
  const target = sel.value;
  if (!target || target === me.target) return;
  try {
    await api("/api/admin/switch", { method: "POST", body: { username: target } });
    me.target = target;
    toast("已切换至用户 " + target + "，以下管理操作均作用于该用户");
    switchTab(activeTab);
    loadMe();                       // 刷新在线状态
  } catch (e) {
    toast(e.message, true);
  }
}

/* ─────────── Tab 切换 ─────────── */
function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll(".tab").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach(p =>
    p.classList.toggle("active", p.id === "tab-" + tab));
  stopTimers();
  if (tab === "status") {
    refreshStatus();
    statusTimer = setInterval(refreshStatus, 3000);
  } else if (tab === "groups") {
    loadConfig();
  } else if (tab === "content") {
    loadConfig();
    loadImages();
  } else if (tab === "qzone") {
    loadQzoneConfig();
    refreshStatus();                    // 状态区（凭证/待执行/记录）3 秒自动刷新
    statusTimer = setInterval(refreshStatus, 3000);
  } else if (tab === "users") {
    loadAdminUsers();
  } else if (tab === "logs") {
    loadLogFiles();
  }
}
function stopTimers() {
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
  if (bindTimer) { clearInterval(bindTimer); bindTimer = null; }
}

/* ─────────── 时间格式化 ─────────── */
function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const now = Date.now();
  const diff = now - ts * 1000;
  if (diff < 60 * 60 * 1000 && diff > 0) {
    return Math.floor(diff / 60000) + " 分钟前";
  }
  const pad = n => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/* ═══════════ Tab 1: 状态监控 ═══════════ */
async function refreshStatus() {
  try {
    const st = await api("/api/status");
    const online = !!st.server_online;
    const engineOff = st.engine_disabled === true;
    setBadge("badge-server", engineOff ? "引擎已关闭" : (online ? "核心服务在线" : "核心服务离线"), engineOff ? "off" : (online ? "ok" : "bad"));
    setBadge("badge-napcat", engineOff ? "—" : (st.napcat_online ? "NapCat 在线" : "NapCat 离线"), engineOff ? "off" : (st.napcat_online ? "ok" : "bad"));
    // QQ 真实在线状态（区别于 NapCat WebSocket 连接状态）
    const qqOnline = st.qq_online !== false;  // 向下兼容旧版 status.json 无 qq_online 字段
    setBadge("badge-qq", engineOff ? "—" : (qqOnline ? "QQ 在线" : "QQ 离线（被踢）"), engineOff ? "off" : (qqOnline ? "ok" : "bad"));
    document.getElementById("server-down-banner").classList.toggle("hidden", online || engineOff);
    // NapCat 离线时显示「重新扫码」入口（已绑定用户重新登录 QQ 用）
    document.getElementById("rescan-btn").classList.toggle("hidden", !!st.napcat_online);
    // 已绑定用户显示「QQ 登出」按钮（NapCat 在线时可用）
    const qqLogoutBtn = document.getElementById("qq-logout-btn");
    if (qqLogoutBtn) qqLogoutBtn.classList.toggle("hidden", !st.napcat_online || !me.bound);
    // 引擎开关按钮
    updateEngineToggleBtn(st);
    groupsCache = st.groups || {};      // 群名映射（发送记录表格用）
    renderStatusTable(st);
    renderHistory(st.history || []);
    renderDetected(st.detected_groups || {});
    renderQzoneStatus(st.qzone);
  } catch (e) {
    if (e.message.includes("登录")) return;
    setBadge("badge-server", "核心服务离线", false);
    document.getElementById("server-down-banner").classList.remove("hidden");
  }
}
function setBadge(id, text, state) {
  const el = document.getElementById(id);
  el.textContent = text;
  // state: "ok" / "bad" / "off"（引擎关闭时灰色）
  const cls = (state === "ok" || state === "bad" || state === "off") ? state : (state ? "ok" : "bad");
  el.className = "badge " + cls;
}
function renderStatusTable(st) {
  const tbody = document.querySelector("#status-table tbody");
  tbody.innerHTML = "";
  const groups = st.groups || {};
  if (!Object.keys(groups).length) {
    tbody.innerHTML = '<tr><td colspan="6" class="hint">暂无目标群，请到「群管理」添加</td></tr>';
    return;
  }
  const now = Date.now() / 1000;
  for (const [gid, g] of Object.entries(groups)) {
    const tr = document.createElement("tr");
    const ratio = g.threshold ? Math.min(g.message_count / g.threshold, 1) : 0;
    const barClass = ratio < 0.5 ? "p-low" : ratio < 0.85 ? "p-mid" : "p-high";
    const inError = g.last_error_time && now - g.last_error_time < 300;
    let stateText = "正常";
    let stateClass = "state-normal";
    if (g.enabled === false) { stateText = "已停用"; stateClass = "state-disabled"; }
    else if (g.sending) { stateText = "发送中…"; stateClass = "state-sending"; }
    else if (inError) { stateText = "错误退避中"; stateClass = "state-error"; }
    tr.innerHTML = `
      <td>${gid}</td>
      <td>${escapeHtml(g.name || "")}</td>
      <td><div class="progress"><div class="${barClass}" style="width:${(ratio * 100).toFixed(0)}%"></div></div>
          <span class="hint">${g.message_count}/${g.threshold}</span></td>
      <td>${fmtTime(g.last_send_time)}</td>
      <td class="${stateClass}">${stateText}</td>
      <td><button class="btn small trigger-btn" data-gid="${gid}">手动触发</button></td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll(".trigger-btn").forEach(btn => {
    btn.addEventListener("click", () => triggerGroup(btn.dataset.gid));
  });
}

/* ═══════════ Tab 4: 发送记录 ═══════════ */
function renderHistory(history) {
  const tbody = document.querySelector("#history-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!history.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="hint">暂无发送记录（触发发送后这里会显示成功/失败详情）</td></tr>';
    return;
  }
  for (const h of history) {
    const tr = document.createElement("tr");
    const timeStr = fmtTime(h.time);
    const resultText = h.success ? "✅ 成功" : "❌ 失败";
    const resultClass = h.success ? "state-normal" : "state-error";
    const err = h.error ? escapeHtml(shorten(h.error, 60)) : "";
    tr.innerHTML = `
      <td>${timeStr}</td>
      <td>${h.group_id}</td>
      <td>${escapeHtml((groupsCache[h.group_id] || {}).name || "")}</td>
      <td class="${resultClass}">${resultText}</td>
      <td>${h.message_id ?? "—"}</td>
      <td>${escapeHtml(h.content || "") || "（仅图片）"}</td>
      <td class="state-error">${err}</td>`;
    tbody.appendChild(tr);
  }
}
let groupsCache = {};

/* ═══════════ 群管理：自动检测到的群 ═══════════ */
function renderDetected(detected) {
  const tbody = document.querySelector("#detected-table tbody");
  if (!tbody) return;
  const configured = groupsCache && Object.keys(groupsCache).length
    ? Object.keys(groupsCache)
    : [];
  const fresh = Object.entries(detected).filter(([gid]) => !configured.includes(gid));
  tbody.innerHTML = "";
  if (!fresh.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="hint">暂无检测到的群（NapCat 所在群的群消息出现在事件流后自动列出）</td></tr>';
    return;
  }
  for (const [gid, d] of fresh) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${gid}</td>
      <td>${escapeHtml(d.name || "（群名未知，添加后可在编辑里改）")}</td>
      <td>${fmtTime(d.first_seen)}</td>
      <td>${fmtTime(d.last_seen)}</td>
      <td><button class="btn small primary add-detected-btn" data-gid="${gid}" data-name="${escapeHtml(d.name || "")}">+ 添加</button></td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll(".add-detected-btn").forEach(b =>
    b.addEventListener("click", () => addDetectedGroup(b.dataset.gid, b.dataset.name)));
}
async function addDetectedGroup(gid, name) {
  try {
    await api("/api/groups", { method: "POST", body: {
      group_id: gid, name: name || `群 ${gid}`, enabled: true,
    }});
    toast(`群 ${gid} 已添加，正在打开编辑——可设置该群专属消息`);
    loadConfig();
    openGroupModal(gid);        // 直接打开编辑弹层：设独立模板/图片/间隔/开关
  } catch (e) {
    toast(e.message, true);
  }
}

/* ═══════════ Tab: 空间监控 ═══════════ */
let qzoneCfg = null;   // 最近一次加载的 qzone 配置

function renderQzoneStatus(qz) {
  const box = document.getElementById("qzone-status-box");
  if (!box) return;
  if (!qz) {
    box.textContent = "空间监控未启用或核心服务离线";
    renderQzonePending([]);
    const tbody = document.querySelector("#qzone-history-table tbody");
    if (tbody) tbody.innerHTML = "";
    return;
  }
  const parts = [
    qz.enabled ? "🟢 已启用" : "⚪ 未启用",
    (qz.manual_mode ? "手动确认" : "自动执行"),
    "监控 " + (qz.monitor_count || 0) + " 人",
    qz.cookies_ok ? "凭证有效" : "无凭证",
    "检查 " + (qz.total_checked || 0) + " / 匹配 " + (qz.total_matched || 0),
    "转发 " + (qz.total_forwarded || 0) + " / 点赞 " + (qz.total_liked || 0) + " / 评论 " + (qz.total_commented || 0),
    qz.last_check_time ? "最近检查 " + qz.last_check_time : "尚未检查",
  ];
  if (qz.error) parts.push("⚠️ " + qz.error);
  box.innerHTML = parts.join(" · ");
  renderQzonePending(qz.pending || []);

  const tbody = document.querySelector("#qzone-history-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const hist = qz.history || [];
  if (!hist.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="hint">暂无空间动态处理记录</td></tr>';
    return;
  }
  for (const h of hist) {
    const acts = h.actions || {};
    const bits = [];
    if (acts.forward) bits.push("📤" + (acts.forward.success ? "✓" : "✗") + (acts.forward.sent ? "×" + acts.forward.sent : ""));
    if (acts.like) bits.push("👍" + (acts.like.success ? "✓" : "✗"));
    if (acts.comment) bits.push("💬" + (acts.comment.success ? "✓" : "✗"));
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTime(h.time)}</td>
      <td>${escapeHtml(h.nickname || h.uin || "")}</td>
      <td>${escapeHtml(shorten(h.content || "", 40))}</td>
      <td>${bits.length ? bits.join(" ") : "—"}</td>`;
    tbody.appendChild(tr);
  }
}

function renderQzonePending(pending) {
  const box = document.getElementById("qzone-pending-box");
  const tbody = document.querySelector("#qzone-pending-table tbody");
  if (!box || !tbody) return;
  document.getElementById("qzone-pending-count").textContent =
    pending.length ? `（${pending.length} 条匹配动态待执行）` : "";
  const execBtn = document.getElementById("qz-exec-all-btn");
  if (execBtn) execBtn.classList.toggle("hidden", !pending.length);
  tbody.innerHTML = "";
  if (!pending.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="hint">暂无待执行动态（手动模式下匹配的动态会出现在这里）</td></tr>';
    return;
  }
  for (const p of pending) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(p.nickname || p.uin || "")}</td>
      <td>${escapeHtml(shorten(p.content || "（无文字）", 50))}</td>
      <td>${escapeHtml(p.create_time || "")}</td>
      <td><button class="btn small primary qz-exec-btn" data-tid="${escapeHtml(p.tid || "")}">执行</button></td>`;
    tr.querySelector(".qz-exec-btn").addEventListener("click", () => executeQzone(p.tid));
    tbody.appendChild(tr);
  }
}

async function executeQzone(tid) {
  const msg = tid ? "确认执行该条动态的设定动作（转发/点赞/评论）？" : "确认执行全部待执行动态（动作间有随机间隔，耗时较长）？";
  if (!confirm(msg)) return;
  try {
    const r = await api("/api/qzone/execute", { method: "POST", body: tid ? { tid } : {} });
    toast(r.message || "已下发执行指令");
  } catch (e) {
    toast(e.message, true);
  }
}

/* ═══════════ Qzone 完整凭证扫码登录 ═══════════ */
let qzlTimer = null;

async function refreshQzoneLoginStatus() {
  try {
    const r = await api("/api/qzone/login/status");
    const el = document.getElementById("qzl-status");
    if (r.logged_in) {
      el.textContent = "✅ " + (r.message || "已登录");
      el.className = "hint";
      document.getElementById("qzl-start-btn").classList.add("hidden");
    } else {
      el.textContent = "⚠️ " + (r.message || "未登录");
      el.className = "hint";
      document.getElementById("qzl-start-btn").classList.remove("hidden");
    }
  } catch (e) {
    /* 忽略 */
  }
}

async function startQzoneLogin() {
  const btn = document.getElementById("qzl-start-btn");
  btn.disabled = true;
  btn.textContent = "获取二维码中...";
  try {
    const r = await api("/api/qzone/login/qrcode", { method: "POST" });
    if (!r.success) throw new Error(r.error || "获取二维码失败");
    document.getElementById("qzl-qr-img").src = r.qrcode;
    document.getElementById("qzl-qr-box").classList.remove("hidden");
    document.getElementById("qzl-refresh-btn").classList.remove("hidden");
    document.getElementById("qzl-start-btn").classList.add("hidden");
    setQzlTip("等待扫码...", "");
    stopQzlTimer();
    qzlTimer = setInterval(pollQzoneLogin, 2000);
  } catch (e) {
    setQzlTip("❌ " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "📱 扫码登录";
  }
}

function setQzlTip(text, cls) {
  const el = document.getElementById("qzl-qr-tip");
  el.textContent = text;
  el.className = "hint" + (cls ? " " + cls : "");
}

function stopQzlTimer() {
  if (qzlTimer) { clearInterval(qzlTimer); qzlTimer = null; }
}

async function pollQzoneLogin() {
  try {
    const r = await api("/api/qzone/login/poll");
    if (r.logged_in) {
      stopQzlTimer();
      setQzlTip("✅ 登录成功，完整凭证已保存", "success");
      toast("Qzone 完整凭证已保存");
      setTimeout(() => {
        document.getElementById("qzl-qr-box").classList.add("hidden");
        document.getElementById("qzl-refresh-btn").classList.add("hidden");
        refreshQzoneLoginStatus();
      }, 1200);
      return;
    }
    const map = {
      waiting: "等待扫码...",
      scanned: "已扫码，请在手机上确认",
      expired: "二维码已过期，请点「刷新二维码」",
      error: "登录出错: " + (r.message || ""),
      unknown: "状态异常，请刷新二维码",
    };
    const text = map[r.status] || r.message || r.status;
    setQzlTip(text, (r.status === "expired" || r.status === "error") ? "error" : "");
    if (r.status === "expired" || r.status === "error") stopQzlTimer();
  } catch (e) {
    setQzlTip("❌ 轮询失败: " + e.message, "error");
    stopQzlTimer();
  }
}

async function loadQzoneConfig() {
  try {
    qzoneCfg = await api("/api/qzone");
  } catch (e) { toast(e.message, true); return; }
  document.getElementById("qz-enabled").checked = !!qzoneCfg.enabled;
  document.getElementById("qz-interval").value = qzoneCfg.poll_interval || 60;
  document.getElementById("qz-max-posts").value = qzoneCfg.max_posts || 100;
  document.getElementById("qz-manual-mode").checked = qzoneCfg.manual_mode !== false;
  document.getElementById("qz-delay-min").value = (qzoneCfg.action_delay || {}).min || 2;
  document.getElementById("qz-delay-max").value = (qzoneCfg.action_delay || {}).max || 8;
  document.getElementById("qz-keywords").value = (qzoneCfg.keywords || []).join("\n");
  document.getElementById("qz-act-forward").checked = qzoneCfg.actions.forward !== false;
  document.getElementById("qz-act-like").checked = !!qzoneCfg.actions.like;
  document.getElementById("qz-act-comment").checked = !!qzoneCfg.actions.comment;
  document.getElementById("qz-comment-mode").value = qzoneCfg.actions.comment_mode || "template";
  document.getElementById("qz-templates").value = (qzoneCfg.comment_templates || [])
    .map(t => `${t.content}|${t.weight}`).join("\n");
  document.getElementById("qz-ai-url").value = qzoneCfg.ai.api_url || "";
  document.getElementById("qz-ai-key").value = qzoneCfg.ai.api_key || "";
  document.getElementById("qz-ai-model").value = qzoneCfg.ai.model || "";
  document.getElementById("qz-ai-prompt").value = qzoneCfg.ai.system_prompt || "";
  renderQzoneMonitors();
  renderQzoneForwards();
  toggleQzoneCommentMode();
}

function renderQzoneMonitors() {
  const box = document.getElementById("qz-monitor-list");
  if (!box) return;
  box.innerHTML = "";
  const list = (qzoneCfg && qzoneCfg.monitor_list) || [];
  if (!list.length) {
    box.innerHTML = '<div class="hint">尚未添加监控目标</div>';
    return;
  }
  list.forEach((m, i) => {
    const row = document.createElement("div");
    row.className = "list-item";
    row.innerHTML = `
      <input class="qz-monitor-name" placeholder="备注名" value="${escapeHtml(m.name || "")}">
      <input class="qz-monitor-uin" placeholder="QQ号" value="${escapeHtml(m.uin || "")}">
      <label class="inline-check"><input type="checkbox" class="qz-monitor-enabled" ${m.enabled === false ? "" : "checked"}> 启用</label>
      <button class="btn small ghost qz-monitor-del" title="删除">✕</button>`;
    row.querySelector(".qz-monitor-del").addEventListener("click", () => {
      qzoneCfg.monitor_list.splice(i, 1);
      renderQzoneMonitors();
    });
    box.appendChild(row);
  });
}

function renderQzoneForwards() {
  const box = document.getElementById("qz-forward-list");
  if (!box) return;
  box.innerHTML = "";
  const list = (qzoneCfg && qzoneCfg.forward_list) || [];
  if (!list.length) {
    box.innerHTML = '<div class="hint">尚未添加转发目标（匹配的动态将不会转发）</div>';
    return;
  }
  list.forEach((f, i) => {
    const row = document.createElement("div");
    row.className = "list-item";
    row.innerHTML = `
      <select class="qz-fwd-type">
        <option value="group" ${f.type === "group" ? "selected" : ""}>群聊</option>
        <option value="private" ${f.type === "private" ? "selected" : ""}>好友</option>
      </select>
      <input class="qz-fwd-id" placeholder="群号/QQ号" value="${escapeHtml(f.id || "")}">
      <input class="qz-fwd-name" placeholder="备注名" value="${escapeHtml(f.name || "")}">
      <label class="inline-check"><input type="checkbox" class="qz-fwd-enabled" ${f.enabled === false ? "" : "checked"}> 启用</label>
      <button class="btn small ghost qz-fwd-del" title="删除">✕</button>`;
    row.querySelector(".qz-fwd-del").addEventListener("click", () => {
      qzoneCfg.forward_list.splice(i, 1);
      renderQzoneForwards();
    });
    box.appendChild(row);
  });
}

function toggleQzoneCommentMode() {
  const mode = document.getElementById("qz-comment-mode").value;
  document.getElementById("qz-template-box").classList.toggle("hidden", mode === "ai");
  document.getElementById("qz-ai-box").classList.toggle("hidden", mode !== "ai");
}

async function saveQzoneConfig() {
  if (!qzoneCfg) return;
  qzoneCfg.enabled = document.getElementById("qz-enabled").checked;
  qzoneCfg.poll_interval = parseInt(document.getElementById("qz-interval").value) || 60;
  qzoneCfg.max_posts = parseInt(document.getElementById("qz-max-posts").value) || 100;
  qzoneCfg.manual_mode = document.getElementById("qz-manual-mode").checked;
  qzoneCfg.action_delay = {
    min: parseFloat(document.getElementById("qz-delay-min").value) || 2,
    max: parseFloat(document.getElementById("qz-delay-max").value) || 8,
  };
  qzoneCfg.keywords = document.getElementById("qz-keywords").value.split("\n")
    .map(s => s.trim()).filter(s => s.length > 0);
  qzoneCfg.monitor_list = [...document.querySelectorAll("#qz-monitor-list .list-item")].map(row => ({
    name: row.querySelector(".qz-monitor-name").value.trim(),
    uin: row.querySelector(".qz-monitor-uin").value.trim(),
    enabled: row.querySelector(".qz-monitor-enabled").checked,
  }));
  qzoneCfg.forward_list = [...document.querySelectorAll("#qz-forward-list .list-item")].map(row => ({
    type: row.querySelector(".qz-fwd-type").value,
    id: row.querySelector(".qz-fwd-id").value.trim(),
    name: row.querySelector(".qz-fwd-name").value.trim(),
    enabled: row.querySelector(".qz-fwd-enabled").checked,
  }));
  qzoneCfg.actions = {
    forward: document.getElementById("qz-act-forward").checked,
    like: document.getElementById("qz-act-like").checked,
    comment: document.getElementById("qz-act-comment").checked,
    comment_mode: document.getElementById("qz-comment-mode").value,
  };
  qzoneCfg.comment_templates = document.getElementById("qz-templates").value.split("\n")
    .map(s => s.trim()).filter(s => s.length > 0).map(line => {
      const parts = line.split("|");
      return { content: parts[0].trim(), weight: parseInt(parts[1]) || 1 };
    });
  qzoneCfg.ai = {
    enabled: document.getElementById("qz-comment-mode").value === "ai",
    api_url: document.getElementById("qz-ai-url").value.trim(),
    api_key: document.getElementById("qz-ai-key").value.trim(),
    model: document.getElementById("qz-ai-model").value.trim(),
    system_prompt: document.getElementById("qz-ai-prompt").value,
  };
  try {
    await api("/api/qzone", { method: "PUT", body: qzoneCfg });
    document.getElementById("qz-result").textContent = "✅ 已保存，约 10 秒内自动生效";
    document.getElementById("qz-result").className = "hint";
  } catch (e) {
    document.getElementById("qz-result").textContent = "❌ " + e.message;
    document.getElementById("qz-result").className = "hint";
    toast(e.message, true);
  }
}

async function testQzone() {
  const resultEl = document.getElementById("qz-result");
  resultEl.textContent = "测试中…";
  try {
    await api("/api/qzone/test", { method: "POST" });
  } catch (e) {
    resultEl.textContent = "❌ " + e.message;
    return;
  }
  for (let i = 0; i < 12; i++) {
    await new Promise(res => setTimeout(res, 1000));
    try {
      const r = await api("/api/qzone/test");
      if (!r.pending) {
        resultEl.textContent = (r.success ? "✅ " : "❌ ") + r.message;
        return;
      }
    } catch (e) { /* 继续轮询 */ }
  }
  resultEl.textContent = "⏳ 测试超时，请稍后重试";
}

function initQzoneTab() {
  const addMonitor = document.getElementById("qz-add-monitor-btn");
  const addForward = document.getElementById("qz-add-forward-btn");
  const saveBtn = document.getElementById("qz-save-btn");
  const testBtn = document.getElementById("qz-test-btn");
  const modeSel = document.getElementById("qz-comment-mode");
  const execAllBtn = document.getElementById("qz-exec-all-btn");
  if (execAllBtn) execAllBtn.addEventListener("click", () => executeQzone(""));
  if (addMonitor) addMonitor.addEventListener("click", () => {
    qzoneCfg = qzoneCfg || { monitor_list: [] };
    qzoneCfg.monitor_list.push({ name: "", uin: "", enabled: true });
    renderQzoneMonitors();
  });
  if (addForward) addForward.addEventListener("click", () => {
    qzoneCfg = qzoneCfg || { forward_list: [] };
    qzoneCfg.forward_list.push({ type: "group", id: "", name: "", enabled: true });
    renderQzoneForwards();
  });
  if (saveBtn) saveBtn.addEventListener("click", saveQzoneConfig);
  if (testBtn) testBtn.addEventListener("click", testQzone);
  if (modeSel) modeSel.addEventListener("change", toggleQzoneCommentMode);
  const qzlStart = document.getElementById("qzl-start-btn");
  const qzlRefresh = document.getElementById("qzl-refresh-btn");
  if (qzlStart) qzlStart.addEventListener("click", startQzoneLogin);
  if (qzlRefresh) qzlRefresh.addEventListener("click", startQzoneLogin);
  refreshQzoneLoginStatus();
}

async function triggerGroup(gid) {
  if (!confirm(`确认手动触发群 ${gid} 发送传单？`)) return;
  try {
    const r = await api("/api/trigger", { method: "POST", body: { group_id: gid } });
    toast(r.message || "已下发触发指令");
  } catch (e) {
    toast(e.message, true);
  }
}

/* ═══════════ 引擎开关 ═══════════ */
function updateEngineToggleBtn(st) {
  const btn = document.getElementById("engine-toggle-btn");
  if (!btn) return;
  const disabled = st.engine_disabled === true;
  if (disabled) {
    btn.textContent = "启动引擎";
    btn.className = "btn small primary";
    btn.title = "引擎已关闭，点击启动";
    btn.classList.remove("hidden");
  } else if (st.server_online) {
    btn.textContent = "关闭引擎";
    btn.className = "btn small ghost";
    btn.title = "关闭引擎，停止监听群消息和发送";
    btn.style.borderColor = "#e04";
    btn.style.color = "#e04";
    btn.classList.remove("hidden");
  } else {
    // 引擎运行中但核心服务离线（NapCat 断连等异常情况）——不显示开关按钮，避免混淆
    btn.classList.add("hidden");
  }
}
async function toggleEngine() {
  const btn = document.getElementById("engine-toggle-btn");
  if (!btn) return;
  const isDisabled = btn.textContent.includes("启动");
  if (isDisabled) {
    // 启动引擎
    try {
      const r = await api("/api/engine/toggle", { method: "POST", body: { disabled: false } });
      toast(r.message || "引擎已启动");
      refreshStatus();
    } catch (e) {
      toast(e.message, true);
    }
  } else {
    // 关闭引擎
    if (!confirm("确认关闭引擎？关闭后不再监听群消息，也不会自动发送传单。可在「群发送状态」随时重新启动。")) return;
    try {
      const r = await api("/api/engine/toggle", { method: "POST", body: { disabled: true } });
      toast(r.message || "引擎已关闭");
      refreshStatus();
    } catch (e) {
      toast(e.message, true);
    }
  }
}

/* ═══════════ Tab 2: 群管理 ═══════════ */
async function loadConfig() {
  try {
    const cfg = await api("/api/config");
    document.getElementById("g-threshold").value = cfg.global.message_threshold || "";
    // 间隔配置以秒存储，界面以分钟显示/输入
    document.getElementById("g-interval").value = cfg.global.send_interval
      ? Math.round(cfg.global.send_interval / 60) : "";
    document.getElementById("template-input").value = cfg.message.template || "";
    categoriesCache = cfg.categories || {};
    renderGroups(cfg.groups);
    renderCategories(cfg.groups);
    renderDefaultImage(cfg.message.default_image, cfg.message.images || []);
    fillImageSelect(cfg.message.images || []);
    fillCategorySelect(cfg.categories || {});
  } catch (e) {
    toast(e.message, true);
  }
}
function renderGroups(groups) {
  const tbody = document.querySelector("#groups-table tbody");
  tbody.innerHTML = "";
  for (const [gid, g] of Object.entries(groups)) {
    const enabled = g.enabled !== false;
    const tr = document.createElement("tr");
    tr.classList.toggle("row-disabled", !enabled);
    // 查找分类名称
    const catName = g.category && categoriesCache[g.category]
      ? categoriesCache[g.category].name : "";
    const catBadge = catName
      ? `<span class="cat-badge" title="${escapeHtml(catName)}">${escapeHtml(catName)}</span>`
      : "—";
    tr.innerHTML = `
      <td>${gid}</td>
      <td>${escapeHtml(g.name || "")}</td>
      <td>${catBadge}</td>
      <td>${g.message_threshold || "全局"}</td>
      <td>${g.send_interval ? Math.round(g.send_interval / 60) + " 分" : "全局"}</td>
      <td>${g.template ? escapeHtml(shorten(g.template, 20)) : "全局"}</td>
      <td>${g.image || "默认"}</td>
      <td>
        <label class="switch"><input type="checkbox" class="enable-toggle" data-gid="${gid}" ${enabled ? "checked" : ""}><span></span></label>
      </td>
      <td>
        <button class="btn small edit-btn" data-gid="${gid}">编辑</button>
        <button class="btn small danger del-btn" data-gid="${gid}">删除</button>
      </td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll(".edit-btn").forEach(b =>
    b.addEventListener("click", () => openGroupModal(b.dataset.gid)));
  tbody.querySelectorAll(".del-btn").forEach(b =>
    b.addEventListener("click", () => deleteGroup(b.dataset.gid)));
  tbody.querySelectorAll(".enable-toggle").forEach(cb =>
    cb.addEventListener("change", () => toggleGroupEnabled(cb.dataset.gid, cb.checked)));
}
async function toggleGroupEnabled(gid, enabled) {
  try {
    await api("/api/groups/" + gid, { method: "PUT", body: { enabled } });
    toast(`群 ${gid} 已${enabled ? "启用" : "停用"}发送（服务器约 10 秒内生效）`);
  } catch (e) {
    toast(e.message, true);
    loadConfig();
  }
}
function renderCategories(groups) {
  const container = document.getElementById("category-list");
  if (!container) return;
  container.innerHTML = "";
  const entries = Object.entries(categoriesCache);
  if (!entries.length) {
    container.innerHTML = '<span class="hint">暂无分类，点击「新增分类」创建</span>';
    return;
  }
  // 优先使用传入的 groups 数据，否则从 groupsCache 取
  const groupsData = groups || groupsCache || {};
  for (const [catId, cat] of entries) {
    // 统计该分类下群数量
    const groupCount = Object.entries(groupsData)
      .filter(([_, g]) => g.category === catId).length;

    const card = document.createElement("div");
    card.className = "category-card";
    const enabled = cat.enabled !== false;
    const thresholdText = cat.message_threshold ? "阈值 " + cat.message_threshold : "无默认阈值";
    const intervalText = cat.send_interval
      ? "间隔 " + Math.round(cat.send_interval / 60) + " 分" : "无默认间隔";
    card.innerHTML = `
      <div class="cat-header">
        <span class="cat-name">${escapeHtml(cat.name)}</span>
        <label class="switch"><input type="checkbox" class="cat-toggle" data-catid="${escapeHtml(catId)}" ${enabled ? "checked" : ""}><span></span></label>
      </div>
      <div class="cat-meta">${groupCount} 个群 · ${thresholdText} · ${intervalText}</div>
      <div class="cat-actions">
        <button class="btn small cat-apply-btn" data-catid="${escapeHtml(catId)}">应用设置</button>
        <button class="btn small cat-edit-btn" data-catid="${escapeHtml(catId)}">编辑</button>
        <button class="btn small danger cat-del-btn" data-catid="${escapeHtml(catId)}">删除</button>
      </div>`;
    container.appendChild(card);
  }
  // 绑定事件
  container.querySelectorAll(".cat-toggle").forEach(cb =>
    cb.addEventListener("change", () => toggleCategory(cb.dataset.catid, cb.checked)));
  container.querySelectorAll(".cat-apply-btn").forEach(b =>
    b.addEventListener("click", () => applyCategory(b.dataset.catid)));
  container.querySelectorAll(".cat-edit-btn").forEach(b =>
    b.addEventListener("click", () => openCategoryModal(b.dataset.catid)));
  container.querySelectorAll(".cat-del-btn").forEach(b =>
    b.addEventListener("click", () => deleteCategory(b.dataset.catid)));
}
function fillCategorySelect(categories) {
  const sel = document.getElementById("gm-category");
  if (!sel) return;
  sel.innerHTML = '<option value="">（无分类）</option>';
  for (const [catId, cat] of Object.entries(categories)) {
    const opt = document.createElement("option");
    opt.value = catId;
    opt.textContent = cat.name;
    sel.appendChild(opt);
  }
}
function shorten(s, n) { return s.length > n ? s.slice(0, n) + "…" : s; }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fillImageSelect(images) {
  const sel = document.getElementById("gm-image");
  sel.innerHTML = '<option value="">（默认）</option>';
  for (const name of images) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  }
}
function openGroupModal(gid = null) {
  editingGid = gid;
  document.getElementById("group-modal-title").textContent = gid ? `编辑群 ${gid}` : "新增群";
  document.getElementById("gm-gid").value = gid || "";
  document.getElementById("gm-gid").disabled = !!gid;
  ["gm-name", "gm-threshold", "gm-interval", "gm-template", "gm-image", "gm-error"].forEach(
    id => document.getElementById(id).value = "");
  document.getElementById("gm-enabled").checked = true;   // 新增默认启用
  document.getElementById("gm-error").textContent = "";
  // 填充分类下拉框
  fillCategorySelect(categoriesCache);
  if (gid) {
    api("/api/config").then(cfg => {
      const g = cfg.groups[gid] || {};
      document.getElementById("gm-name").value = g.name || "";
      document.getElementById("gm-threshold").value = g.message_threshold ?? "";
      document.getElementById("gm-interval").value = g.send_interval
        ? Math.round(g.send_interval / 60) : "";
      document.getElementById("gm-template").value = g.template || "";
      document.getElementById("gm-image").value = g.image || "";
      document.getElementById("gm-enabled").checked = g.enabled !== false;
      document.getElementById("gm-category").value = g.category || "";
    }).catch(e => toast(e.message, true));
  }
  document.getElementById("group-modal").classList.remove("hidden");
}
function closeGroupModal() {
  document.getElementById("group-modal").classList.add("hidden");
}
async function saveGroup() {
  const body = {
    group_id: document.getElementById("gm-gid").value.trim(),
    name: document.getElementById("gm-name").value.trim(),
    message_threshold: document.getElementById("gm-threshold").value || null,
    // 界面输入分钟，存储用秒
    send_interval: document.getElementById("gm-interval").value
      ? Number(document.getElementById("gm-interval").value) * 60 : null,
    template: document.getElementById("gm-template").value || null,
    image: document.getElementById("gm-image").value || null,
    enabled: document.getElementById("gm-enabled").checked,
    category: document.getElementById("gm-category").value || null,
  };
  if (!body.group_id || !body.name) {
    document.getElementById("gm-error").textContent = "群号和群名必填";
    return;
  }
  try {
    if (editingGid) {
      await api("/api/groups/" + editingGid, { method: "PUT", body });
      toast("群已更新，服务器约 10 秒内自动加载");
    } else {
      await api("/api/groups", { method: "POST", body });
      toast("群已添加，服务器约 10 秒内自动加载");
    }
    closeGroupModal();
    loadConfig();
  } catch (e) {
    document.getElementById("gm-error").textContent = e.message;
  }
}
async function deleteGroup(gid) {
  if (!confirm(`确认删除群 ${gid}？`)) return;
  try {
    await api("/api/groups/" + gid, { method: "DELETE" });
    toast("群已删除");
    loadConfig();
  } catch (e) {
    toast(e.message, true);
  }
}
async function saveGlobal() {
  try {
    await api("/api/config", { method: "PUT", body: {
      global: {
        message_threshold: document.getElementById("g-threshold").value || undefined,
        // 界面输入分钟，存储用秒
        send_interval: document.getElementById("g-interval").value
          ? Number(document.getElementById("g-interval").value) * 60 : undefined,
      },
    }});
    toast("全局设置已保存，服务器约 10 秒内自动加载");
  } catch (e) {
    toast(e.message, true);
  }
}

/* ═══════════ 分类管理 ═══════════ */

function openCategoryModal(catId = null) {
  editingCatId = catId;
  document.getElementById("category-modal-title").textContent = catId ? "编辑分类" : "新增分类";
  document.getElementById("cm-name").value = "";
  document.getElementById("cm-threshold").value = "";
  document.getElementById("cm-interval").value = "";
  document.getElementById("cm-enabled").checked = true;
  document.getElementById("cm-error").textContent = "";
  if (catId && categoriesCache[catId]) {
    const cat = categoriesCache[catId];
    document.getElementById("cm-name").value = cat.name || "";
    document.getElementById("cm-threshold").value = cat.message_threshold ?? "";
    document.getElementById("cm-interval").value = cat.send_interval
      ? Math.round(cat.send_interval / 60) : "";
    document.getElementById("cm-enabled").checked = cat.enabled !== false;
  }
  document.getElementById("category-modal").classList.remove("hidden");
}

function closeCategoryModal() {
  document.getElementById("category-modal").classList.add("hidden");
}

async function saveCategory() {
  const name = document.getElementById("cm-name").value.trim();
  if (!name) {
    document.getElementById("cm-error").textContent = "分类名称不能为空";
    return;
  }
  const body = {
    name,
    message_threshold: document.getElementById("cm-threshold").value || null,
    send_interval: document.getElementById("cm-interval").value
      ? Number(document.getElementById("cm-interval").value) * 60 : null,
    enabled: document.getElementById("cm-enabled").checked,
  };
  try {
    if (editingCatId) {
      await api("/api/categories/" + encodeURIComponent(editingCatId), { method: "PUT", body });
      toast("分类已更新");
    } else {
      await api("/api/categories", { method: "POST", body });
      toast("分类已创建");
    }
    closeCategoryModal();
    loadConfig();
  } catch (e) {
    document.getElementById("cm-error").textContent = e.message;
  }
}

async function toggleCategory(catId, enabled) {
  try {
    const r = await api("/api/categories/" + encodeURIComponent(catId) + "/toggle",
      { method: "POST", body: { enabled } });
    toast(r.message || "分类已切换");
    loadConfig();
  } catch (e) {
    toast(e.message, true);
    loadConfig();  // 回退界面状态
  }
}

async function applyCategory(catId) {
  const cat = categoriesCache[catId];
  if (!cat) return;
  if (!confirm(`确认将分类「${cat.name}」的默认设置（阈值 ${cat.message_threshold || "—"}，间隔 ${cat.send_interval ? Math.round(cat.send_interval / 60) + " 分" : "—"}）应用到该分类下所有群？`)) return;
  try {
    const r = await api("/api/categories/" + encodeURIComponent(catId) + "/apply",
      { method: "POST" });
    toast(r.message || "分类设置已应用到群");
    loadConfig();
  } catch (e) {
    toast(e.message, true);
  }
}

async function deleteCategory(catId) {
  const cat = categoriesCache[catId];
  if (!cat) return;
  if (!confirm(`确认删除分类「${cat.name}」？该分类下的群将变为「无分类」，群本身不会被删除。`)) return;
  try {
    await api("/api/categories/" + encodeURIComponent(catId), { method: "DELETE" });
    toast("分类已删除");
    loadConfig();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ═══════════ Tab 3: 内容与图片 ═══════════ */
function renderDefaultImage(defaultImage, images) {
  const box = document.getElementById("default-image-options");
  box.innerHTML = "";
  const mk = (label, value) => {
    const lab = document.createElement("label");
    lab.innerHTML = `<input type="radio" name="default-image" value="${escapeHtml(value || "")}"
      ${(defaultImage || "") === (value || "") ? "checked" : ""}> ${escapeHtml(label)}`;
    box.appendChild(lab);
  };
  mk("随机（从图片库任选一张）", "");
  for (const name of images) mk("固定：" + name, name);
  box.querySelectorAll('input[name="default-image"]').forEach(r =>
    r.addEventListener("change", saveDefaultImage));
}
async function saveDefaultImage() {
  const val = document.querySelector('input[name="default-image"]:checked').value;
  try {
    await api("/api/config", { method: "PUT", body: { message: { default_image: val || null } } });
    toast("默认图片已更新");
  } catch (e) {
    toast(e.message, true);
  }
}
async function loadImages() {
  try {
    const r = await api("/api/images");
    imagesCache = r.images || [];
    const grid = document.getElementById("image-grid");
    if (!grid) return;
    grid.innerHTML = "";
    if (!imagesCache.length) {
      grid.innerHTML = '<span class="hint">暂无图片，请上传</span>';
      return;
    }
    for (const img of imagesCache) {
      const card = document.createElement("div");
      card.className = "image-card";
      const info = `${escapeHtml(img.name)}<br>${img.size ? (img.size / 1024).toFixed(0) + " KB" : "?"}`;
      card.innerHTML = `
        <img data-name="${escapeHtml(img.name)}" alt="">
        <div class="img-info">${info}</div>
        <div class="img-actions">
          <button class="btn small danger img-del" data-name="${escapeHtml(img.name)}">删除</button>
        </div>`;
      grid.appendChild(card);
      // blob 预览（避免把 token 放进 URL）
      fetch("/api/images/" + encodeURIComponent(img.name), {
        headers: { Authorization: "Bearer " + token },
      }).then(r => r.blob()).then(b => {
        card.querySelector("img").src = URL.createObjectURL(b);
      }).catch(() => {});
    }
    grid.querySelectorAll(".img-del").forEach(b =>
      b.addEventListener("click", () => deleteImage(b.dataset.name)));
  } catch (e) {
    toast(e.message, true);
  }
}
async function deleteImage(name) {
  if (!confirm(`确认删除图片「${name}」？`)) return;
  try {
    await api("/api/images/" + encodeURIComponent(name), { method: "DELETE" });
    toast("已删除");
    loadImages();
    loadConfig();
  } catch (e) {
    toast(e.message, true);
  }
}
async function uploadImages() {
  const input = document.getElementById("image-file-input");
  const files = [...(input.files || [])];
  if (!files.length) { toast("请先选择图片", true); return; }
  const status = document.getElementById("upload-status");
  let okCount = 0;
  let failCount = 0;
  for (const file of files) {
    status.textContent = "上传中：" + file.name + "…";
    const fd = new FormData();
    fd.append("file", file);
    try {
      await api("/api/images", { method: "POST", body: fd, isForm: true });
      okCount++;
    } catch (e) {
      failCount++;
      toast(file.name + "：" + e.message, true);
    }
  }
  status.textContent = "";
  input.value = "";
  if (okCount > 0) toast(`上传完成：${okCount} 张成功${failCount > 0 ? `，${failCount} 张失败` : ""}`);
  if (okCount > 0) { loadImages(); loadConfig(); }
}
async function saveTemplate() {
  try {
    await api("/api/config", { method: "PUT", body: {
      message: { template: document.getElementById("template-input").value },
    }});
    toast("模板已保存，客户端下次启动时自动同步");
  } catch (e) {
    toast(e.message, true);
  }
}

/* ═══════════ Tab 5: 用户管理（仅管理员） ═══════════ */
async function loadAdminUsers() {
  try {
    const r = await api("/api/admin/users");
    renderUsers(r.users || []);
    loadInvites();
  } catch (e) {
    toast(e.message, true);
  }
}
function renderUsers(users) {
  const tbody = document.querySelector("#users-table tbody");
  tbody.innerHTML = "";
  if (!users.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="hint">暂无用户</td></tr>';
    return;
  }
  for (const u of users) {
    const isSelf = u.username === me.username;
    const bindText = u.bound
      ? `<span class="state-normal">已绑定${u.qq_number ? "（" + u.qq_number + "）" : ""}</span>`
      : '<span class="state-disabled">未绑定</span>';
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(u.username)}${isSelf ? ' <span class="hint">（本人）</span>' : ""}</td>
      <td>${u.role === "admin" ? "管理员" : "普通"}</td>
      <td>${u.qq_number ?? "—"}</td>
      <td class="${u.online ? "state-normal" : "state-disabled"}">${u.online ? "在线" : "离线"}</td>
      <td>${u.webui_port ?? "—"}</td>
      <td>${bindText}</td>
      <td>
        ${u.bound
          ? `<button class="btn small unbind-btn" data-u="${u.username}">解绑</button>`
          : `<button class="btn small primary bind-btn" data-u="${u.username}">绑定</button>`}
        ${u.bound ? `<button class="btn small danger qq-logout-btn" data-u="${u.username}">QQ 登出</button>` : ""}
        <button class="btn small pwd-btn" data-u="${u.username}">重置密码</button>
        ${isSelf ? "" : `<button class="btn small danger del-btn" data-u="${u.username}">删除</button>`}
      </td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll(".bind-btn").forEach(b =>
    b.addEventListener("click", () => startBind(b.dataset.u)));
  tbody.querySelectorAll(".unbind-btn").forEach(b =>
    b.addEventListener("click", () => unbindUser(b.dataset.u)));
  tbody.querySelectorAll(".qq-logout-btn").forEach(b =>
    b.addEventListener("click", () => qqLogout(b.dataset.u)));
  tbody.querySelectorAll(".del-btn").forEach(b =>
    b.addEventListener("click", () => deleteUser(b.dataset.u)));
  tbody.querySelectorAll(".pwd-btn").forEach(b =>
    b.addEventListener("click", () => resetPassword(b.dataset.u)));
}
function openUserModal() {
  document.getElementById("user-modal").classList.remove("hidden");
  document.getElementById("um-username").value = "";
  document.getElementById("um-password").value = "";
  document.getElementById("um-role").value = "user";
  document.getElementById("um-error").textContent = "";
}
function closeUserModal() {
  document.getElementById("user-modal").classList.add("hidden");
}
async function saveUser() {
  const username = document.getElementById("um-username").value.trim();
  const password = document.getElementById("um-password").value;
  const role = document.getElementById("um-role").value;
  if (!username || !password) {
    document.getElementById("um-error").textContent = "用户名和密码必填";
    return;
  }
  try {
    await api("/api/admin/users", { method: "POST", body: { username, password, role } });
    toast("用户已创建，服务器约 10 秒内自动启动其引擎");
    closeUserModal();
    loadAdminUsers();
    loadMe();
  } catch (e) {
    document.getElementById("um-error").textContent = e.message;
  }
}
async function deleteUser(username) {
  if (!confirm(`确认删除用户 ${username}？将删除其 NapCat 容器、配置、图片与发送记录，不可恢复！`)) return;
  try {
    await api("/api/admin/users/" + encodeURIComponent(username), { method: "DELETE" });
    toast(`用户 ${username} 已删除`);
    loadAdminUsers();
    loadMe();
  } catch (e) {
    toast(e.message, true);
  }
}
async function resetPassword(username) {
  const pw = prompt(`为用户 ${username} 设置新密码（至少 6 位）：`);
  if (pw === null) return;
  if (pw.length < 6) { toast("密码至少 6 位", true); return; }
  try {
    await api(`/api/admin/users/${encodeURIComponent(username)}/password`,
      { method: "POST", body: { password: pw } });
    toast("密码已更新");
  } catch (e) {
    toast(e.message, true);
  }
}
async function unbindUser(username) {
  if (!confirm(`确认解绑用户 ${username}？将删除其 NapCat 容器（QQ 登录态保留在卷中，可随时重新绑定）。`)) return;
  try {
    await api(`/api/admin/users/${encodeURIComponent(username)}/unbind`,
      { method: "POST" });
    toast("已解绑");
    loadAdminUsers();
  } catch (e) {
    toast(e.message, true);
  }
}
async function qqLogout(username) {
  const label = username || "当前账号";
  if (!confirm(`确认登出 ${label} 的 QQ？将清除登录态并禁用自动登录，容器重启后需重新扫码登录。`)) return;
  try {
    const url = username
      ? `/api/admin/users/${encodeURIComponent(username)}/qq-logout`
      : "/api/qq-logout";
    const r = await api(url, { method: "POST" });
    toast(r.message || "QQ 已登出");
    if (username) loadAdminUsers();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ─────────── 绑定流程（弹窗 + 轮询） ─────────── */
async function startBind(username) {
  try {
    const r = await api(`/api/admin/users/${encodeURIComponent(username)}/bind`,
      { method: "POST" });
    document.getElementById("bind-username").textContent = username;
    const statusEl = document.getElementById("bind-status");
    statusEl.className = "bind-status";
    statusEl.textContent = "⏳ " + (r.message || "请扫码登录…");
    document.getElementById("bind-error").textContent = "";
    document.getElementById("bind-modal").classList.remove("hidden");
    // 直接展示容器内二维码（绕开 NapCat WebUI 端口冲突）
    document.getElementById("bind-modal-qr").classList.remove("hidden");
    setQrTip("bind-modal-qr-tip", "");
    bindModalUsername = username;
    pollBindQr(`/api/admin/users/${encodeURIComponent(username)}/qrcode`,
      "bind-modal-qr-img", "bind-modal-qr-tip");
    pollBind(username);
  } catch (e) {
    toast(e.message, true);
  }
}
function pollBind(username) {
  if (bindTimer) clearInterval(bindTimer);
  bindTimer = setInterval(async () => {
    try {
      const st = await api(`/api/admin/users/${encodeURIComponent(username)}/bind`);
      const statusEl = document.getElementById("bind-status");
      if (st.status === "success") {
        clearInterval(bindTimer);
        stopBindQrTimers();
        statusEl.textContent = `✅ 绑定成功${st.qq_number ? "（QQ " + st.qq_number + "）" : ""}，NapCat 已重启生效`;
        statusEl.className = "bind-status ok";
        loadAdminUsers();
      } else if (st.status === "timeout" || st.status === "error") {
        clearInterval(bindTimer);
        stopBindQrTimers();
        statusEl.textContent = "❌ " + (st.error || "绑定失败");
        statusEl.className = "bind-status err";
      } else {
        statusEl.textContent = "⏳ " + (st.message || "等待扫码登录…");
        statusEl.className = "bind-status";
      }
    } catch (e) { /* 网络抖动：继续轮询 */ }
  }, 3000);
}

/* ═══════════ Tab 6: 日志 ═══════════ */
async function loadLogFiles() {
  try {
    const r = await api("/api/logs");
    const sel = document.getElementById("log-file-select");
    sel.innerHTML = "";
    for (const f of r.files) {
      const opt = document.createElement("option");
      opt.value = f;
      opt.textContent = f;
      sel.appendChild(opt);
    }
    if (r.files.length) {
      logFile = r.files[0];
      sel.value = logFile;
      logOffset = 0;
      document.getElementById("log-output").textContent = "";
      pollLog();
      if (logTimer) clearInterval(logTimer);
      logTimer = setInterval(pollLog, 4000);
    }
  } catch (e) {
    toast(e.message, true);
  }
}
async function pollLog() {
  if (!logFile) return;
  try {
    const r = await api(`/api/logs?file=${encodeURIComponent(logFile)}&offset=${logOffset}`);
    const out = document.getElementById("log-output");
    const follow = document.getElementById("log-follow").checked;
    if (r.next_offset <= logOffset && r.content === "") return;  // 无新内容
    if (logOffset === 0 || r.next_offset < logOffset) {
      // 首次或文件轮转：截断重取
      out.textContent = r.content;
    } else {
      out.textContent += r.content;
    }
    logOffset = r.next_offset;
    if (follow) out.scrollTop = out.scrollHeight;
  } catch (e) { /* 静默：下次重试 */ }
}

/* ═══════════ 重新扫码（已绑定但 NapCat 离线的用户） ═══════════ */
function rescanSelf() {
  // 直接显示当前容器二维码并轮询在线状态（不走绑定流程，避免干扰）
  showBindGuide(false);
  document.getElementById("bind-guide-body").classList.add("hidden");
  document.getElementById("bind-guide-qr").classList.remove("hidden");
  setBindGuideStatus("⏳ 请用手机 QQ 扫描下方二维码（确认是本人账号）…", "");
  pollBindQr("/api/bind/qrcode", "bind-guide-qr-img", "bind-guide-qr-tip");
  if (bindTimer) clearInterval(bindTimer);
  bindTimer = setInterval(async () => {
    try {
      const st = await api("/api/status");
      if (st.napcat_online) {
        clearInterval(bindTimer);
        stopBindQrTimers();
        setBindGuideStatus("✅ NapCat 已在线！", "ok");
        await loadMe();
        showDashboard();
        toast("NapCat 已重新上线");
      }
    } catch (e) { /* 网络抖动：继续轮询 */ }
  }, 3000);
}

/* ═══════════ 注册（邮箱 + 邀请码 + 验证码） ═══════════ */
let codeCountdown = null;
function openRegister() {
  document.getElementById("register-modal").classList.remove("hidden");
  ["rg-email", "rg-password", "rg-password2", "rg-invite", "rg-code"].forEach(
    id => document.getElementById(id).value = "");
  const errEl = document.getElementById("rg-error");
  errEl.textContent = "";
  errEl.style.color = "";
  const btn = document.getElementById("rg-send-code");
  btn.disabled = false;
  btn.textContent = "发送验证码";
  if (codeCountdown) { clearInterval(codeCountdown); codeCountdown = null; }
  document.getElementById("register-modal").querySelector(".modal-actions").style.display = "";
}
function closeRegister() {
  document.getElementById("register-modal").classList.add("hidden");
  if (codeCountdown) { clearInterval(codeCountdown); codeCountdown = null; }
}
async function sendCode() {
  const email = document.getElementById("rg-email").value.trim();
  const invite_code = document.getElementById("rg-invite").value.trim();
  const errEl = document.getElementById("rg-error");
  errEl.style.color = "";
  if (!email) { errEl.textContent = "请先填写邮箱"; return; }
  if (!invite_code) { errEl.textContent = "请先填写邀请码"; return; }
  try {
    await api("/api/register/send-code", { method: "POST", body: { email, invite_code } });
    toast("验证码已发送至 " + email + "（10 分钟内有效）");
    errEl.textContent = "验证码已发送，请查收邮箱（含垃圾箱）";
    errEl.style.color = "#067647";
    // 60 秒倒计时
    const btn = document.getElementById("rg-send-code");
    let remain = 60;
    btn.disabled = true;
    btn.textContent = remain + "s 后重发";
    if (codeCountdown) clearInterval(codeCountdown);
    codeCountdown = setInterval(() => {
      remain--;
      if (remain <= 0) {
        clearInterval(codeCountdown);
        codeCountdown = null;
        btn.disabled = false;
        btn.textContent = "发送验证码";
      } else {
        btn.textContent = remain + "s 后重发";
      }
    }, 1000);
  } catch (e) {
    errEl.textContent = e.message;
  }
}
async function submitRegister() {
  const email = document.getElementById("rg-email").value.trim();
  const code = document.getElementById("rg-code").value.trim();
  const password = document.getElementById("rg-password").value;
  const password2 = document.getElementById("rg-password2").value;
  const invite_code = document.getElementById("rg-invite").value.trim();
  const errEl = document.getElementById("rg-error");
  errEl.style.color = "";
  if (!email || !password || !invite_code || !code) {
    errEl.textContent = "邮箱、验证码、密码和邀请码必填";
    return;
  }
  if (password !== password2) {
    errEl.textContent = "两次输入的密码不一致";
    return;
  }
  try {
    const r = await api("/api/register", { method: "POST", body: { email, password, invite_code, code } });
    // 注册成功：自动返回登录页，预填邮箱，提示直接登录
    closeRegister();
    document.getElementById("login-username").value = email;
    document.getElementById("login-password").value = "";
    const loginErr = document.getElementById("login-error");
    loginErr.textContent = "✅ 注册成功，请使用刚才的密码登录";
    loginErr.style.color = "#067647";
    document.getElementById("login-password").focus();
  } catch (e) {
    errEl.textContent = e.message;
  }
}

/* ═══════════ 绑定引导（登录后未绑定用户的首页） ═══════════ */
let bindQrTimers = {};          // imgId -> timer（二维码轮询）
let lastQrData = {};            // imgId -> 上次拉取的 base64（判断是否换新）
let bindModalUsername = "";     // admin 绑定弹窗当前绑定的用户
function stopBindQrTimers() {
  for (const t of Object.values(bindQrTimers)) clearInterval(t);
  bindQrTimers = {};
}
function setBindGuideStatus(text, cls) {
  const el = document.getElementById("bind-guide-status");
  el.textContent = text;
  el.className = "bind-status" + (cls ? " " + cls : "");
}
function pollBindQr(url, imgId, tipId) {
  const img = document.getElementById(imgId);
  if (!img) return;
  const fetchQr = async () => {
    try {
      const r = await api(url);
      if (r.qr) {
        const changed = lastQrData[imgId] !== r.qr;
        lastQrData[imgId] = r.qr;
        img.src = r.qr;
        setQrTip(tipId, changed
          ? "✅ 已获取新二维码（" + new Date().toLocaleTimeString() + "），请扫码"
          : "🔄 二维码已刷新（" + new Date().toLocaleTimeString() + "）；NapCat 约 2 分钟换新，换新后自动显示新码");
      } else {
        setQrTip(tipId, "⏳ 二维码更新中，请稍候…");
      }
    } catch (e) {
      setQrTip(tipId, "⏳ 二维码更新中，请稍候…");
    }
  };
  fetchQr();
  bindQrTimers[imgId] = setInterval(fetchQr, 30000);   // 二维码每 30 秒刷新一次
}
function setQrTip(tipId, text) {
  const tip = document.getElementById(tipId);
  if (tip) tip.textContent = text;
}
async function refreshQr(url, imgId, tipId, refreshUrl, btnId) {
  const btn = btnId ? document.getElementById(btnId) : null;
  if (btn) { btn.disabled = true; btn.textContent = "刷新中…"; }
  try {
    // 调 NapCat RefreshQRcode 接口立即换新码
    setQrTip(tipId, "🔄 正在刷新二维码…");
    await api(refreshUrl, { method: "POST" });
    // 等 NapCat 生成新码后拉取（约 2 秒），随后交给 30 秒轮询兜底
    setTimeout(async () => {
      try {
        const r = await api(url);
        if (r.qr) {
          lastQrData[imgId] = r.qr;
          document.getElementById(imgId).src = r.qr;
          setQrTip(tipId, "✅ 新二维码已生成（" + new Date().toLocaleTimeString() + "），请扫码");
        } else {
          setQrTip(tipId, "⏳ 新码生成中，30 秒内自动更新…");
        }
      } catch (e) { /* 轮询兜底 */ }
    }, 2000);
  } catch (e) {
    setQrTip(tipId, "⚠️ " + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "🔄 刷新二维码"; }
  }
}
async function startBindSelf() {
  document.getElementById("bind-guide-error").textContent = "";
  try {
    const r = await api("/api/bind", { method: "POST" });
    document.getElementById("bind-guide-body").classList.add("hidden");
    document.getElementById("bind-guide-qr").classList.remove("hidden");
    setBindGuideStatus("⏳ 请用手机 QQ 扫描下方二维码（确认是本人账号）…", "");
    pollBindQr("/api/bind/qrcode", "bind-guide-qr-img", "bind-guide-qr-tip");
    pollBindSelf();
  } catch (e) {
    document.getElementById("bind-guide-error").textContent = e.message;
  }
}
function pollBindSelf() {
  if (bindTimer) clearInterval(bindTimer);
  bindTimer = setInterval(async () => {
    try {
      const st = await api("/api/bind");
      if (st.status === "success") {
        clearInterval(bindTimer);
        stopBindQrTimers();
        setBindGuideStatus(`✅ 绑定成功${st.qq_number ? "（QQ " + st.qq_number + "）" : ""}，正在进入管理台…`, "ok");
        await loadMe();
        showDashboard();
        toast("绑定成功，欢迎使用");
      } else if (st.status === "timeout" || st.status === "error") {
        clearInterval(bindTimer);
        stopBindQrTimers();
        setBindGuideStatus("❌ " + (st.error || "绑定失败"), "err");
        document.getElementById("bind-guide-body").classList.remove("hidden");
        document.getElementById("bind-guide-qr").classList.add("hidden");
      } else {
        setBindGuideStatus("⏳ " + (st.message || "等待扫码登录…"), "");
      }
    } catch (e) { /* 网络抖动：继续轮询 */ }
  }, 3000);
}

/* ═══════════ 邀请码管理（admin） ═══════════ */
async function loadInvites() {
  try {
    const r = await api("/api/admin/invites");
    renderInvites(r.invites || {});
  } catch (e) {
    toast(e.message, true);
  }
}
function renderInvites(invites) {
  const tbody = document.querySelector("#invites-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const entries = Object.entries(invites);
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="hint">暂无邀请码，点击「生成邀请码」创建</td></tr>';
    return;
  }
  for (const [code, inv] of entries) {
    const used = !!inv.used_by;
    const expires = inv.expires_at ? fmtTime(inv.expires_at) : "永久";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${escapeHtml(code)}</code></td>
      <td>${fmtTime(inv.created_at)}</td>
      <td>${expires}</td>
      <td class="${used ? "state-disabled" : "state-normal"}">${used ? "已使用（" + escapeHtml(inv.used_by) + "）" : "未使用"}</td>
      <td><button class="btn small danger inv-del" data-code="${escapeHtml(code)}">删除</button></td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll(".inv-del").forEach(b =>
    b.addEventListener("click", () => deleteInvite(b.dataset.code)));
}
async function genInvite() {
  const days = parseInt(document.getElementById("inv-days").value || "0", 10);
  try {
    const r = await api("/api/admin/invites", { method: "POST", body: { valid_days: days } });
    toast("邀请码：" + r.code + "（请复制发给目标用户）");
    loadInvites();
  } catch (e) {
    toast(e.message, true);
  }
}
async function deleteInvite(code) {
  if (!confirm(`确认删除邀请码 ${code}？`)) return;
  try {
    await api("/api/admin/invites/" + encodeURIComponent(code), { method: "DELETE" });
    toast("已删除");
    loadInvites();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ═══════════ 登录 / 事件绑定 ═══════════ */
async function doLogin() {
  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value;
  document.getElementById("login-error").textContent = "";
  if (!username) {
    document.getElementById("login-error").textContent = "请输入用户名";
    return;
  }
  try {
    const r = await api("/api/login", { method: "POST", body: { username, password } });
    token = r.token;
    localStorage.setItem(TOKEN_KEY, token);
    document.getElementById("login-password").value = "";
    await loadMe();               // 取角色与用户列表
    showMain();
    toast("登录成功");
  } catch (e) {
    document.getElementById("login-error").textContent = e.message;
  }
}
function doLogout() {
  token = "";
  localStorage.removeItem(TOKEN_KEY);
  showLogin();
}

/* 初始化：校验 token 是否有效 */
async function init() {
  document.getElementById("login-btn").addEventListener("click", doLogin);
  document.getElementById("login-password").addEventListener("keydown",
    e => { if (e.key === "Enter") doLogin(); });
  document.getElementById("login-username").addEventListener("keydown",
    e => { if (e.key === "Enter") doLogin(); });
  document.getElementById("logout-btn").addEventListener("click", doLogout);
  document.getElementById("register-link").addEventListener("click", openRegister);
  document.getElementById("rg-cancel").addEventListener("click", closeRegister);
  document.getElementById("rg-submit").addEventListener("click", submitRegister);
  document.getElementById("rg-send-code").addEventListener("click", sendCode);
  document.getElementById("bind-start-btn").addEventListener("click", startBindSelf);
  document.getElementById("bind-skip-btn").addEventListener("click", showDashboard);
  document.getElementById("rescan-btn").addEventListener("click", rescanSelf);
  document.getElementById("qq-logout-btn").addEventListener("click", () => qqLogout(""));
  document.getElementById("engine-toggle-btn").addEventListener("click", toggleEngine);
  document.getElementById("bind-guide-qr-refresh").addEventListener("click",
    () => refreshQr("/api/bind/qrcode", "bind-guide-qr-img", "bind-guide-qr-tip",
      "/api/bind/qrcode/refresh", "bind-guide-qr-refresh"));
  document.getElementById("bind-modal-qr-refresh").addEventListener("click",
    () => refreshQr(`/api/admin/users/${encodeURIComponent(bindModalUsername)}/qrcode`,
      "bind-modal-qr-img", "bind-modal-qr-tip",
      `/api/admin/users/${encodeURIComponent(bindModalUsername)}/qrcode/refresh`,
      "bind-modal-qr-refresh"));
  document.getElementById("gen-invite-btn").addEventListener("click", genInvite);
  document.querySelectorAll(".tab").forEach(b =>
    b.addEventListener("click", () => switchTab(b.dataset.tab)));
  document.getElementById("add-group-btn").addEventListener("click", () => openGroupModal());
  document.getElementById("gm-cancel").addEventListener("click", closeGroupModal);
  document.getElementById("gm-save").addEventListener("click", saveGroup);
  document.getElementById("add-category-btn").addEventListener("click", () => openCategoryModal());
  document.getElementById("cm-cancel").addEventListener("click", closeCategoryModal);
  document.getElementById("cm-save").addEventListener("click", saveCategory);
  document.getElementById("save-global-btn").addEventListener("click", saveGlobal);
  document.getElementById("save-template-btn").addEventListener("click", saveTemplate);
  document.getElementById("upload-btn").addEventListener("click", uploadImages);
  initQzoneTab();
  document.getElementById("log-file-select").addEventListener("change",
    e => { logFile = e.target.value; logOffset = 0;
           document.getElementById("log-output").textContent = ""; pollLog(); });
  document.getElementById("log-clear-btn").addEventListener("click",
    () => document.getElementById("log-output").textContent = "");
  document.getElementById("add-user-btn").addEventListener("click", openUserModal);
  document.getElementById("um-cancel").addEventListener("click", closeUserModal);
  document.getElementById("um-save").addEventListener("click", saveUser);
  document.getElementById("bind-close").addEventListener("click", () => {
    if (bindTimer) { clearInterval(bindTimer); bindTimer = null; }
    stopBindQrTimers();
    document.getElementById("bind-modal").classList.add("hidden");
  });

  if (!token) { showLogin(); return; }
  try {
    await loadMe();               // 校验 token 并取用户信息
    showMain();
  } catch (e) {
    showLogin();
  }
}

init();
