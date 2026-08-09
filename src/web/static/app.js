const $ = (id) => document.getElementById(id);

// --------------------------------------------------------------------------- //
// 登入（USER_SYSTEM_PLAN Part I）
// 全站需登入：開機先打 /api/me，401 → 只顯示登入遮罩、不啟動任何資料載入。
// 共用 401 攔截：包住 window.fetch，任何 API 回 401（session 過期、被停用）
// 一律回到登入畫面——既有 21 個 fetch 呼叫點零修改。登入成功後整頁重載＝
// 乾淨重啟（對話、chatSessionId、輪詢全部歸零，沿用換板即重置的精神）。
// --------------------------------------------------------------------------- //
const AUTH = { user: null };              // /api/me 的結果（role 供 admin 區塊）

const _rawFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const r = await _rawFetch(input, init);
  const url = typeof input === "string" ? input : ((input && input.url) || "");
  if (r.status === 401 && !url.startsWith("/api/login")) showLogin(r.clone());
  return r;
};

async function showLogin(resp) {
  const ov = $("login-overlay");
  if (!ov || !ov.hidden) return;             // 已顯示就不重複觸發
  ov.hidden = false;
  const u = $("login-username");
  if (u) u.focus();
  // bootstrap（計劃 I.4）：全新安裝一個帳號都沒有 → 顯示 create_admin 引導
  try {
    const d = resp ? await resp.json() : null;
    if (d && d.users_exist === false) $("login-bootstrap").hidden = false;
  } catch (e) { /* 非 JSON 回應（極端情況）——引導區塊維持隱藏 */ }
}

async function doLogin() {
  const btn = $("login-submit");
  if (btn.disabled) return;                  // 進行中就不重送（Enter 連發/自動重複
                                             // 會白白吃掉 5 次/60 秒的嘗試額度）
  const u = ($("login-username").value || "").trim();
  const p = $("login-password").value || "";
  const err = $("login-error");
  err.textContent = "";
  if (!u || !p) { err.textContent = "請輸入帳號與密碼"; return; }
  btn.disabled = true;
  try {
    const r = await _rawFetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {                             // 401 帳密錯 / 429 鎖定中
      err.textContent = d.error || ("登入失敗（HTTP " + r.status + "）");
      return;
    }
    location.reload();
  } catch (e) {
    err.textContent = "無法連線伺服器，請稍後再試";
  } finally {
    btn.disabled = false;
  }
}

// 設定頁「帳號」pane 與 admin 左欄入口（/api/me 結果進來後渲染）
function renderAccount() {
  if (!AUTH.user) return;
  const name = $("acct-name");
  if (name) name.textContent = AUTH.user.display_name || AUTH.user.username;
  const det = $("acct-detail");
  if (det) det.textContent = AUTH.user.username +
    (AUTH.user.role === "admin" ? "（admin）" : "（一般使用者）");
  // admin-only 左欄入口（伺服器端另有 403 把關，這裡只是 UI 開關）。
  // /api/me 可能比使用者打開設定頁晚——解鎖後補載一次（沒開著就 no-op）
  const nav = $("set-nav-users");
  if (nav) nav.hidden = AUTH.user.role !== "admin";
  umLoad();
}

function initLogin() {
  const sub = $("login-submit");
  if (sub) sub.addEventListener("click", doLogin);
  ["login-username", "login-password"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) doLogin();
    });
  });
  const lo = $("logout-btn");                 // 登出鈕在設定頁「帳號」pane
  if (lo) lo.addEventListener("click", async () => {
    try { await fetch("/api/logout", { method: "POST" }); } catch (e) { /* 連線失敗也照樣重載回登入頁 */ }
    location.reload();
  });
}

// 目前選定的目標板子；每個請求都會帶上它（無狀態後端依此載入對應 /data）
let currentBoard = null;

// Agent 對話的伺服器端 session id（後端首次回傳後保存；換板時清掉開新對話）
let chatSessionId = null;

// --------------------------------------------------------------------------- //
// 板子選擇 — 啟動時向 /api/boards 取得 data/boards/ 下自動偵測到的板子清單
// --------------------------------------------------------------------------- //
let boardNames = {};                      // slug -> display name（board.yaml）

async function loadBoards(selectSlug) {
  const sel = $("board-select");
  if (!sel) return;
  try {
    const r = await fetch("/api/boards");
    const d = await r.json();
    const boards = (d.boards && d.boards.length) ? d.boards : [d.default];
    boardNames = d.names || {};
    currentBoard = (selectSlug && boards.includes(selectSlug))
      ? selectSlug : (d.default || boards[0]);
    sel.innerHTML = "";
    boards.forEach((b) => {
      const opt = document.createElement("option");
      opt.value = b;
      opt.textContent = boardNames[b] || b;     // display name（缺 manifest 退回 id）
      if (b === currentBoard) opt.selected = true;
      sel.appendChild(opt);
    });
    // 上傳功能移入設定頁：can_create 控制設定頁「新增板子」區塊的顯示
    const up = $("set-upload-section");
    if (up) up.hidden = !d.can_create;
    if (!sel._bcBound) {                        // 重載時避免重複掛 listener
      sel.addEventListener("change", onBoardChange);
      sel._bcBound = true;
    }
    updateIntroBoard();
    ovLoad();                                   // 設定頁開著時同步覆寫表（隱藏時 no-op）
  } catch (e) {
    // 取不到清單（舊後端 / 失敗）-> 隱藏選單，沿用後端預設板
    sel.style.display = "none";
    updateIntroBoard();
  }
}

// 開場的目前板子提示＋header 設定鈕上的板名：顯示現在對話針對哪塊板
//（display name，缺 manifest 退回 slug）。板子清單載入與每次換板都會更新。
function updateIntroBoard() {
  const name = currentBoard ? (boardNames[currentBoard] || currentBoard) : "";
  const hint = $("intro-board");
  if (!hint) return;
  if (!currentBoard) { hint.hidden = true; return; }
  hint.innerHTML = "目前板子：<b>" + escapeHtml(name) + "</b>";
  hint.hidden = false;
}

function clearChat() {
  // 清空對話、只留開場 intro——換板後畫面上不殘留上一塊板的 plan/驗證卡片
  //（那些卡片的按鈕與輪詢都綁舊板，留著會誤導）。進行中的背景輪詢會因
  // 目標節點已脫離 DOM 而靜默失效，fingerprint 對賬也擋掉錯板渲染。
  const box = $("messages");
  if (!box) return;
  Array.from(box.children).forEach((el) => {
    if (!el.classList.contains("intro")) el.remove();
  });
}

function onBoardChange(e) {
  currentBoard = e.target.value;
  // 換板等於開新話題：丟掉 Agent 對話 session，避免拿新板的 Σ/DTS
  // 去回答舊板的問題（後端 session 綁定某塊板的對話歷史）；
  // 同時清空畫面上的對話（不同板的對話不互相影響）。
  chatSessionId = null;
  clearChat();
  updateIntroBoard();
  initDts();                                  // DTS 生成可用性依板而定
  ovLoad();                                   // 設定頁開著：換板重載該板的覆寫表
}

// --------------------------------------------------------------------------- //
// 上傳新板子（EXTRACTOR_MERGE_PLAN M4）：彈窗 → 上傳 → 等待 → REVIEW → 落地
// --------------------------------------------------------------------------- //
const BC_STAGE_LABEL = {
  unpack: "解包上傳檔…",
  manual: "解析手冊（LLM 抽取，數分鐘～數十分鐘）…",
  dts: "解析 DTS、生成知識庫…",
  board_yaml: "寫入板子設定…",
  lint: "知識庫進場檢查…",
  landing: "落地中…",
};
let bcPolling = false;

function bcShow(section) {                    // form | progress | review
  $("bc-overlay").hidden = false;
  ["bc-form", "bc-progress", "bc-review"].forEach((id) => {
    $(id).hidden = (id !== "bc-" + section);
  });
}

async function bcOpen() {
  // 有進行中的 job 就直接接上，否則開空白表單
  try {
    const s = await (await fetch("/api/boards/create/status")).json();
    if (s.running || s.stage === "failed") {
      bcShow("progress"); bcRenderProgress(s); bcPoll(); return;
    }
  } catch (e) { /* 端點不可用 → 照常開表單，送出時再報錯 */ }
  $("bc-form-error").textContent = "";
  bcShow("form");
}

function bcClose() { $("bc-overlay").hidden = true; }

// DTS 選檔後偵測板檔候選：多個 .dts 時要求使用者指定 baseline（M7 發現的
// 真實 gap——kernel 樹常含同 SoC 多塊板的板檔，無法自動決定）
function bcDetectBaseline() {
  const files = Array.from($("bc-dts").files || []);
  const cands = files.map((f) => f.webkitRelativePath || f.name)
    .filter((p) => p.endsWith(".dts"))
    .map((p) => p.split("/").pop());
  const field = $("bc-baseline-field");
  const sel = $("bc-baseline");
  sel.innerHTML = "";
  if (cands.length > 1) {
    // 不給預設值——2026-07-25 事故：預設字母序第一個讓使用者誤選 IOT2050
    // 的 24 行覆蓋殼當 baseline，整份知識庫空洞化。必須明選。
    const ph = document.createElement("option");
    ph.value = ""; ph.textContent = "—— 請選擇（通常含 base-board / evm / evb 字樣）——";
    ph.disabled = true; ph.selected = true;
    sel.appendChild(ph);
    cands.sort().forEach((c) => {
      const o = document.createElement("option");
      o.value = c; o.textContent = c;
      sel.appendChild(o);
    });
    field.hidden = false;
  } else {
    field.hidden = true;                    // 0/1 個候選：後端自動決定
  }
}

// --------------------------------------------------------------------------- //
// 上傳表單：安全保留腳位群組（選填）——PMIC 等 secure/bootloader 持有匯流排。
// 上傳當下該板 af_table 尚未誕生（抽取的產物），所以這裡只做格式 sanity；
// 「腳位存在／訊號可達→AF 回填」由 knowledge_extract.reserved_groups 在
// 抽取後期驗證，查不到的列剔除並在完成頁 boot 判定表警告。
// --------------------------------------------------------------------------- //
function bcRgAddGroup() {
  const wrap = document.createElement("div");
  wrap.className = "bc-rg-group";
  const head = document.createElement("div");
  head.className = "bc-rg-head";
  head.innerHTML =
    '<input class="rg-name" placeholder="群組名＝instance 名，例：I2C7" maxlength="32">' +
    '<input class="rg-role" placeholder="標題，例：電源管理 PMIC (STPMIC25)" maxlength="120">' +
    '<input class="rg-owner" placeholder="持有者，例：TF-A/OP-TEE (secure)" maxlength="120">';
  const del = document.createElement("button");
  del.type = "button";
  del.className = "ov-clear";
  del.textContent = "移除";
  del.addEventListener("click", () => wrap.remove());
  head.appendChild(del);
  wrap.appendChild(head);
  const rows = document.createElement("div");
  rows.className = "bc-rg-rows";
  wrap.appendChild(rows);
  const addRow = () => {
    const row = document.createElement("div");
    row.className = "bc-rg-row";
    row.innerHTML =
      '<input class="rg-sig" placeholder="訊號，例：I2C7_SCL" maxlength="48">' +
      '<input class="rg-pin" placeholder="腳位，例：PD15" maxlength="16">';
    const rdel = document.createElement("button");
    rdel.type = "button";
    rdel.className = "ov-clear";
    rdel.textContent = "刪";
    rdel.addEventListener("click", () => row.remove());
    row.appendChild(rdel);
    rows.appendChild(row);
  };
  addRow(); addRow();                       // 預設兩列（SCL/SDA 的常見形狀）
  const more = document.createElement("button");
  more.type = "button";
  more.className = "ov-clear";
  more.textContent = "＋ 腳位";
  more.addEventListener("click", addRow);
  wrap.appendChild(more);
  $("bc-rg-list").appendChild(wrap);
}

// 收集＋格式檢查（寬鬆 sanity，同覆寫表；存在性由後端 af_table 驗證）。
// 回 {list} 或 {error}；全空的組／列自動忽略。
function bcRgCollect() {
  const RE = {
    name: /^[A-Za-z0-9_]{2,32}$/,
    sig: /^[A-Za-z0-9_\/]{2,48}$/,
    pin: /^[A-Za-z0-9_.]{2,16}$/,
  };
  const list = [];
  for (const gEl of document.querySelectorAll("#bc-rg-list .bc-rg-group")) {
    const val = (cls) => {
      const el = gEl.querySelector("input." + cls);
      return el ? el.value.trim() : "";
    };
    const name = val("rg-name");
    const rows = [];
    let bad = null;
    gEl.querySelectorAll(".bc-rg-row").forEach((r) => {
      const sig = r.querySelector(".rg-sig").value.trim();
      const pin = r.querySelector(".rg-pin").value.trim();
      if (!sig && !pin) return;             // 空列忽略
      if (!RE.sig.test(sig)) bad = bad || ("訊號格式不正確（" + (sig || "空") + "）");
      if (!RE.pin.test(pin)) bad = bad || ("腳位格式不正確（" + (pin || "空") + "）");
      rows.push({ signal: sig, pin: pin });
    });
    if (!name && !rows.length) continue;    // 整組全空：忽略
    if (!RE.name.test(name)) {
      return { error: "保留群組的群組名格式不正確（例：I2C7）" };
    }
    if (bad) return { error: "保留群組 " + name + "：" + bad };
    list.push({ name: name, role: val("rg-role"), owner: val("rg-owner"),
                pins: rows });
  }
  return { list: list };
}

async function bcSubmit() {
  const name = $("bc-name").value.trim();
  const pdf = $("bc-pdf").files[0];
  const dts = $("bc-dts").files;
  const err = $("bc-form-error");
  if (!name) { err.textContent = "請輸入板子名稱"; return; }
  if (!pdf) { err.textContent = "請選擇手冊 PDF"; return; }
  if (!dts.length) { err.textContent = "請選擇 DTS 資料夾（或 zip）"; return; }
  if (!$("bc-baseline-field").hidden && !$("bc-baseline").value) {
    err.textContent = "偵測到多個 .dts——請選擇這塊板的 baseline 板檔";
    return;
  }
  const rg = bcRgCollect();                 // 安全保留群組（選填）
  if (rg.error) { err.textContent = rg.error; return; }
  const fd = new FormData();
  fd.append("name", name);
  fd.append("validate", $("bc-validate").checked ? "1" : "0");
  if (!$("bc-baseline-field").hidden) {
    fd.append("baseline", $("bc-baseline").value);
  }
  if (rg.list.length) {
    fd.append("reserved_groups", JSON.stringify(rg.list));
  }
  fd.append("pdf", pdf, pdf.name);
  for (const f of dts) {
    fd.append("dts", f, f.webkitRelativePath || f.name);  // 保留資料夾相對路徑
  }
  $("bc-submit").disabled = true;
  err.textContent = "";
  try {
    const r = await fetch("/api/boards/create", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.error || ("HTTP " + r.status); return; }
    bcMetricsStop();                           // 新 job：計時歸零重新起錶
    bcShow("progress");
    bcRenderProgress({ stage: "unpack" });     // 立即起錶，每秒平順跳動
    bcPoll();
  } catch (e) {
    err.textContent = "上傳失敗：" + e.message;
  } finally {
    $("bc-submit").disabled = false;
  }
}

// 等待畫面的執行指標（灰字）：本地單一起錶點（startAt）每秒平順跳動——
// 不隨輪詢重設基準（整數秒重設＋本地取整疊加會卡頓跳拍）；只有明顯漂移
// （頁面重載後接回進行中的 job）才用伺服器 elapsed 校正一次。
// tokens 每次輪詢更新（LLM 手冊抽取的即時消耗）。
let bcMetrics = { startAt: null, tokens: 0, timer: null };

function bcMetricsText() {
  if (bcMetrics.startAt == null) return "";
  const sec = Math.max(0, Math.floor((Date.now() - bcMetrics.startAt) / 1000));
  const m = Math.floor(sec / 60), s = sec % 60;
  const t = (bcMetrics.tokens || 0).toLocaleString();
  return `已思考 ${m ? m + " 分 " : ""}${s} 秒 ・ 已消耗 ${t} tokens`;
}

function bcMetricsTick(s) {
  if (bcMetrics.startAt == null) bcMetrics.startAt = Date.now();  // 即刻起錶
  if (s && s.elapsed != null) {
    const serverStart = Date.now() - s.elapsed * 1000;
    if (Math.abs(serverStart - bcMetrics.startAt) > 2000) {
      bcMetrics.startAt = serverStart;        // 漂移校正（重載/背景恢復）
    }
    bcMetrics.tokens = s.tokens || 0;
  }
  if ($("bc-metrics")) $("bc-metrics").textContent = bcMetricsText();
  if (!bcMetrics.timer) {
    bcMetrics.timer = setInterval(() => {
      if ($("bc-metrics")) $("bc-metrics").textContent = bcMetricsText();
    }, 1000);
  }
}

function bcMetricsStop() {
  if (bcMetrics.timer) { clearInterval(bcMetrics.timer); bcMetrics.timer = null; }
  bcMetrics.startAt = null;                   // 下一個 job 重新起錶
}

// 最終指標（伺服器凍結的 elapsed/tokens）——完成與失敗畫面用
function bcFinalMetricsText(s) {
  if (!s || s.elapsed == null) return "";
  const m = Math.floor(s.elapsed / 60), sec = s.elapsed % 60;
  const t = (s.tokens || 0).toLocaleString();
  return `共思考 ${m ? m + " 分 " : ""}${sec} 秒 ・ 消耗 ${t} tokens`;
}

function bcRenderProgress(s) {
  const failed = s.stage === "failed";
  $("bc-stage-label").textContent = failed
    ? "生成失敗" : (BC_STAGE_LABEL[s.stage] || "處理中…");
  const sp = document.querySelector("#bc-progress .spinner");
  if (sp) sp.style.display = failed ? "none" : "";
  $("bc-progress-error").textContent = s.error || "";
  $("bc-artifacts").hidden = !failed;
  $("bc-abort").hidden = !failed;
  if (failed) {
    bcMetricsStop();
    $("bc-metrics").textContent = bcFinalMetricsText(s);   // 凍結顯示最終值
  } else {
    bcMetricsTick(s);
  }
}

async function bcPoll() {
  if (bcPolling) return;
  bcPolling = true;
  try {
    while (true) {
      await new Promise((res) => setTimeout(res, 4000));
      const s = await (await fetch("/api/boards/create/status")).json();
      if (s.stage === "done") {                 // lint 綠已自動落地
        bcMetricsStop();
        await bcRenderDone(s);
        return;
      }
      if (s.stage === "failed") { bcRenderProgress(s); return; }
      if (s.stage === null) { bcMetricsStop(); return; }
      bcRenderProgress(s);
    }
  } catch (e) { /* 網路抖動：使用者可重開彈窗續看 */ }
  finally { bcPolling = false; }
}

// 落地完成畫面：唯讀 boot 判定表＋警示；選單即刻刷新並選中新板
async function bcRenderDone(s) {
  const rv = s.review || {};
  $("bc-done-metrics").textContent = bcFinalMetricsText(s);
  $("bc-done-note").textContent = s.note || "";
  const tbody = $("bc-boot-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const [g, spec] of Object.entries(rv.boot_groups || {})) {
    const tr = document.createElement("tr");
    const act = spec.action === "emit_fixed_assignment"
      ? "emit（plan 自動帶上）" : "reserve（保留不排）";
    tr.innerHTML = `<td>${escapeHtml(g)}</td><td>${escapeHtml(act)}</td>` +
      `<td>${spec.pins ?? ""}</td><td>${escapeHtml(spec.basis || "")}</td>`;
    tbody.appendChild(tr);
  }
  $("bc-emit-warn").hidden = !rv.needs_emit_confirm;
  $("bc-review-md").textContent = rv.review_md || "（無 REVIEW.md）";
  await loadBoards(s.landed);                   // 重載選單並選中新板
  chatSessionId = null;
  clearChat();                                  // 自動切到新板＝換板，同樣清對話
  initDts();
  if (!$("bc-overlay").hidden) bcShow("review");
}

async function bcAbort() {
  try {
    await fetch("/api/boards/create", { method: "DELETE" });
  } catch (e) { /* 忽略 */ }
  bcClose();
}

function initBoardCreate() {
  if (!$("bc-overlay")) return;
  $("bc-dts").addEventListener("change", bcDetectBaseline);
  $("bc-cancel").addEventListener("click", bcClose);
  $("bc-hide").addEventListener("click", bcClose);
  $("bc-submit").addEventListener("click", bcSubmit);
  $("bc-done-close").addEventListener("click", bcClose);
  $("bc-abort").addEventListener("click", bcAbort);
  $("bc-artifacts").addEventListener("click", () => {
    window.location.href = "/api/boards/create/artifacts";
  });
  const rgAdd = $("bc-rg-add");
  if (rgAdd) rgAdd.addEventListener("click", bcRgAddGroup);
}

// --------------------------------------------------------------------------- //
// 設定頁 —— ①目標板子（board-select 移入此頁）②上傳新板子 ③開機/PMIC 腳位覆寫
//
// 腳位覆寫（USER_SYSTEM_PLAN Part III）：客製板可能與官方板同 SoC，但開機/
// PMIC 腳位被改（例：PMIC 的 I2C7_SCL 從 PD15 挪到別腳）。修改存進資料庫、
// scope 到登入使用者（每人一組，互不影響）；真實知識庫永不被修改，求解時
// 由後端把差異疊加在 require.json 之上。
//
// 後端契約（src/web/overrides_api.py）：
//   GET /api/boards/<board>/pin-overrides
//     -> { entries: [ { role, group, signal, default_pin, af,
//                       override_group, override_signal, override_pin } ],
//          set: {id,name,version}|null, base_version, source: "db"|"official",
//          stale: [對不上官方表的孤兒覆寫] }
//        group/signal＝官方名（對齊錨點）；override_*＝null 表示未覆寫。
//   PUT /api/boards/<board>/pin-overrides
//     body: { base_version, entries: [ { official_group, official_signal,
//             group, signal, pin } ] }   送整張表的目前值；後端與 require.json
//     逐欄比對只存差異，並依序驗證（格式／腳位存在／訊號可達／instance
//     rename／撞腳／樂觀鎖）。400＝驗證未過（details 逐列）；409＝版本衝突。
// --------------------------------------------------------------------------- //
let ovEntries = [];        // 目前板的覆寫表（GET 結果）
let ovBaseVersion = 0;     // 樂觀鎖：PUT 帶回，後端不符回 409

// 左欄導覽切頁：腳位覆寫/使用者管理是資料頁，切到才載入（懶載入）
const SET_PANES = ["account", "board", "pins", "users"];
function setShowPane(pane) {
  document.querySelectorAll(".set-nav-item").forEach((b) => {
    b.classList.toggle("active", b.dataset.pane === pane);
  });
  SET_PANES.forEach((p) => {
    const el = $("set-pane-" + p);
    if (el) el.hidden = p !== pane;
  });
  if (pane === "pins") ovLoad();
  if (pane === "users") umLoad();
}
function setOpen() {
  $("set-overlay").hidden = false;
  setShowPane("account");
}
function setClose() { $("set-overlay").hidden = true; }

// --------------------------------------------------------------------------- //
// 使用者管理（Part II；admin-only）：清單＋建立一般 user＋重設密碼＋停用/啟用。
// admin 帳號在表中唯讀（web 不能管 admin——只能 CLI）。
// --------------------------------------------------------------------------- //
async function umLoad() {
  const overlay = $("set-overlay");
  if (!overlay || overlay.hidden) return;     // 設定頁沒開就不載（同 ovLoad）
  if (!AUTH.user || AUTH.user.role !== "admin") return;   // 非 admin 不打端點
  const err = $("um-error");
  err.textContent = "";
  try {
    const r = await fetch("/api/users");
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
    umRender(d.users || []);
  } catch (e) {
    err.textContent = "載入使用者清單失敗：" + (e && e.message ? e.message : e);
  }
}

function umRender(users) {
  const tbody = $("um-table").querySelector("tbody");
  tbody.innerHTML = "";
  users.forEach((u) => {
    const tr = document.createElement("tr");
    if (!u.is_active) tr.className = "um-inactive";
    tr.appendChild(el("td", "mono", escapeHtml(u.username)));
    tr.appendChild(el("td", "", escapeHtml(u.display_name || "")));
    tr.appendChild(el("td", "", u.role === "admin" ? "<b>admin</b>" : "user"));
    tr.appendChild(el("td", "", u.is_active ? "啟用" : "已停用"));
    tr.appendChild(el("td", "", escapeHtml((u.last_login_at || "—").replace("T", " "))));
    const tdA = el("td", "um-actions", "");
    if (u.role === "admin") {
      tdA.appendChild(el("span", "note", "CLI 管理"));
    } else {
      const reset = document.createElement("button");
      reset.className = "ov-clear";
      reset.textContent = "重設密碼";
      reset.addEventListener("click", async () => {
        const pw = prompt("輸入「" + u.username + "」的新密碼（至少 6 字元）：");
        if (pw === null) return;
        if (pw.length < 6) {                  // 後端也會擋；這裡先給清楚訊息
          $("um-error").textContent = "密碼至少 6 個字元——未變更。";
          return;
        }
        await umPatch(u.id, { password: pw });
      });
      tdA.appendChild(reset);
      const tog = document.createElement("button");
      tog.className = "ov-clear";
      tog.textContent = u.is_active ? "停用" : "啟用";
      tog.addEventListener("click", async () => {
        if (u.is_active &&
            !confirm("停用「" + u.username + "」？其下一個請求即登出。")) return;
        await umPatch(u.id, { is_active: !u.is_active });
      });
      tdA.appendChild(tog);
    }
    tr.appendChild(tdA);
    tbody.appendChild(tr);
  });
}

async function umPatch(uid, body) {
  const err = $("um-error");
  err.textContent = "";
  try {
    const r = await fetch("/api/users/" + uid, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ("HTTP " + r.status));
    umLoad();
  } catch (e) {
    err.textContent = "更新失敗：" + (e && e.message ? e.message : e);
  }
}

async function umCreate() {
  const err = $("um-error");
  err.textContent = "";
  const username = ($("um-username").value || "").trim();
  const display = ($("um-display").value || "").trim();
  const password = $("um-password").value || "";
  if (!username) { err.textContent = "請填帳號"; return; }
  if (password.length < 6) { err.textContent = "初始密碼至少 6 個字元"; return; }
  const btn = $("um-create-btn");
  btn.disabled = true;
  try {
    const r = await fetch("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, display_name: display, password }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ("HTTP " + r.status));
    $("um-username").value = "";
    $("um-display").value = "";
    $("um-password").value = "";
    umLoad();
  } catch (e) {
    err.textContent = "建立失敗：" + (e && e.message ? e.message : e);
  } finally {
    btn.disabled = false;
  }
}

// 載入目前板的覆寫表；設定頁沒開就不打（換板時被 onBoardChange 呼叫）
async function ovLoad() {
  const overlay = $("set-overlay");
  if (!overlay || overlay.hidden) return;
  const status = $("ov-status");
  status.className = "note ov-status";
  status.textContent = "載入中…";
  try {
    const r = await fetch("/api/boards/" + encodeURIComponent(currentBoard || "")
                          + "/pin-overrides");
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
    ovEntries = d.entries || [];
    ovBaseVersion = d.base_version || 0;
    let txt = "";
    if (!ovEntries.length) {
      txt = "此板沒有開機/PMIC 腳位資料。";
    } else if (d.source === "db") {
      txt = "✓ 已套用你的個人覆寫（第 " + ovBaseVersion +
            " 版）——只影響你自己的求解，不動真實知識庫。";
    } else {
      txt = "目前全部為官方值；修改後儲存即成為你的個人覆寫。";
    }
    if (d.stale && d.stale.length) {
      status.className = "note ov-status demo";
      txt += "⚠ 有 " + d.stale.length +
             " 筆舊覆寫對不上目前知識庫（已忽略）：" + d.stale.join("、");
    }
    status.textContent = txt;
  } catch (e) {
    ovEntries = [];
    ovBaseVersion = 0;
    status.className = "note ov-status demo";
    status.textContent = "⚠ 覆寫表載入失敗：" + (e && e.message ? e.message : e);
  }
  // 三層值：official=官方（對照用）、loaded=載入時（dirty 基準）、cur=編輯中
  ovEntries.forEach((en) => {
    en.official = { group: en.group, signal: en.signal, pin: en.default_pin };
    en.loaded = {
      group: en.override_group || en.group,
      signal: en.override_signal || en.signal,
      pin: en.override_pin || en.default_pin,
    };
    en.cur = { ...en.loaded };
  });
  ovRender();
}

// 欄位格式：只做寬鬆 sanity——命名慣例不寫死（TI 板訊號含小寫、pad 命名
// 各家不同）；「腳位/訊號是否存在於此板、是否撞腳」由後端以該板 af_table
// 驗證（那才是真正的守門，這裡只是 UX 提示）
const OV_FIELD_RE = {
  group: /^[A-Za-z0-9_]{2,}$/,            // I2C7 / SDMMC1 …
  signal: /^[A-Za-z0-9_\/]{2,}$/,         // I2C7_SCL / UART0_CTSn …
  pin: /^[A-Za-z0-9_.]{2,}$/,             // PA0 / PD15 …（各板 pad 命名不同）
};

// 行內編輯格：平常長得像一般文字（無框），點擊聚焦才浮出輸入框樣式。
// 值即時寫回 en.cur；dirty=與載入值不同、invalid=格式不合。
function ovMakeCell(en, i, field, widthCls) {
  const input = document.createElement("input");
  input.className = "ov-edit " + widthCls;
  input.value = en.cur[field];
  input.dataset.idx = String(i);
  input.dataset.field = field;
  input.addEventListener("input", ovInputChanged);
  return input;
}

// 依 group 合併「角色／介面」欄（rowspan）；介面格編輯會同步整組（改介面名
// ＝這條匯流排整個換 instance，例：PMIC 從 I2C7 移到 I2C6）
function ovRender() {
  const tbody = $("ov-table").querySelector("tbody");
  tbody.innerHTML = "";
  $("ov-error").textContent = "";
  const spans = {};                       // 官方 group -> 列數（rowspan 基準）
  ovEntries.forEach((en) => {
    spans[en.official.group] = (spans[en.official.group] || 0) + 1;
  });
  const seen = new Set();
  ovEntries.forEach((en, i) => {
    const tr = document.createElement("tr");
    if (!seen.has(en.official.group)) {
      seen.add(en.official.group);
      tr.appendChild(el("td", "ov-role", "<b>" + escapeHtml(en.role || "") + "</b>"));
      tr.lastChild.rowSpan = spans[en.official.group];
      const tdG = document.createElement("td");
      tdG.rowSpan = spans[en.official.group];
      tdG.appendChild(ovMakeCell(en, i, "group", "w-group"));
      tr.appendChild(tdG);
    }
    const tdS = document.createElement("td");
    tdS.appendChild(ovMakeCell(en, i, "signal", "w-signal"));
    tr.appendChild(tdS);
    const tdP = document.createElement("td");
    tdP.appendChild(ovMakeCell(en, i, "pin", "w-pin"));
    const note = el("span", "ov-official", "");
    note.hidden = true;
    tdP.appendChild(note);
    tr.appendChild(tdP);
    const tdClr = document.createElement("td");
    const clr = document.createElement("button");
    clr.className = "ov-clear";
    clr.textContent = "還原";
    clr.title = "此列回到官方值（介面/訊號/腳位）";
    clr.addEventListener("click", () => {
      en.cur = { ...en.official };
      ovEntries.filter((x) => x.official.group === en.official.group)
        .forEach((x) => { x.cur.group = en.official.group; });  // 介面名整組同步
      ovRender();
    });
    tdClr.appendChild(clr);
    tr.appendChild(tdClr);
    tbody.appendChild(tr);
    ovRefreshRowState(tr, en, i);
  });
}

function ovInputChanged(e) {
  const input = e.target;
  const i = Number(input.dataset.idx);
  const field = input.dataset.field;
  const en = ovEntries[i];
  // 大小寫跟隨官方值的慣例：官方全大寫（ST）才自動轉大寫；含小寫的板
  //（TI 的 UART0_CTSn…）保留原樣，硬大寫會對不上該板 Σ
  let val = input.value.trim();
  const official = String(en.official[field] || "");
  if (official && official === official.toUpperCase()) val = val.toUpperCase();
  en.cur[field] = val;
  if (field === "group") {                // 介面格是整組共用的（rowspan）
    ovEntries.filter((x) => x.official.group === en.official.group)
      .forEach((x) => { x.cur.group = val; });
  }
  ovRefreshRowState(input.closest("tr"), en, i);
}

// 重算一列的 dirty/invalid 標記與「官方值」對照小字
function ovRefreshRowState(tr, en, i) {
  tr.querySelectorAll("input.ov-edit").forEach((input) => {
    const f = input.dataset.field;
    const x = ovEntries[Number(input.dataset.idx)];
    const val = x.cur[f];
    input.classList.toggle("dirty", val !== x.loaded[f]);
    input.classList.toggle("invalid", !OV_FIELD_RE[f].test(val));
  });
  const note = tr.querySelector(".ov-official");
  if (note) {
    const diffs = [];
    if (en.cur.pin !== en.official.pin) diffs.push("官方 " + en.official.pin);
    note.textContent = diffs.join("・");
    note.hidden = !diffs.length;
  }
}

async function ovSave() {
  const err = $("ov-error");
  err.textContent = "";
  const bad = ovEntries.filter((en) =>
    !OV_FIELD_RE.group.test(en.cur.group) ||
    !OV_FIELD_RE.signal.test(en.cur.signal) ||
    !OV_FIELD_RE.pin.test(en.cur.pin));
  if (bad.length) {
    err.textContent = "格式不正確（介面例：I2C7；訊號例：I2C7_SCL；腳位例：PD15）：" +
      bad.map((en) => en.official.signal).join("、");
    return;
  }
  const btn = $("ov-save");
  btn.disabled = true;
  try {
    const r = await fetch("/api/boards/" + encodeURIComponent(currentBoard || "")
                          + "/pin-overrides", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_version: ovBaseVersion,           // 樂觀鎖：與伺服器不符回 409
        entries: ovEntries.map((en) => ({
          official_group: en.official.group,   // 對齊錨點（改名/改腳都靠它定位）
          official_signal: en.official.signal,
          group: en.cur.group,
          signal: en.cur.signal,
          pin: en.cur.pin,
        })),
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.status === 400 && d.details) {       // 逐列驗證錯誤（後端資料驅動檢查）
      err.textContent = d.error + "\n" + d.details.join("\n");
      return;
    }
    if (!r.ok || !d.ok) throw new Error(d.error || ("HTTP " + r.status));
    await ovLoad();                            // 以後端落地結果為準重新渲染
    // 覆寫改變了這塊板的求解常數——進行中的對話綁的是舊 _Board 推理歷史，
    // 沿用「換板即重置」模式自動開新話題，下一則訊息即用新覆寫（III.3）。
    chatSessionId = null;
    clearChat();
    const status = $("ov-status");
    status.className = "note ov-status ov-saved";
    status.textContent = (d.source === "db")
      ? "✓ 已儲存（第 " + d.version + " 版）——已開新話題，下一次求解即生效。"
      : "✓ 已儲存：與官方相同，無覆寫（回到官方值）。已開新話題。";
  } catch (e2) {
    err.textContent = "儲存失敗：" + (e2 && e2.message ? e2.message : e2);
  } finally {
    btn.disabled = false;
  }
}

function initSettings() {
  if (!$("set-overlay")) return;
  $("settings-btn").addEventListener("click", setOpen);
  $("set-close").addEventListener("click", setClose);
  document.querySelectorAll(".set-nav-item").forEach((b) => {
    b.addEventListener("click", () => setShowPane(b.dataset.pane));
  });
  $("set-upload-btn").addEventListener("click", bcOpen);
  const umBtn = $("um-create-btn");
  if (umBtn) umBtn.addEventListener("click", umCreate);
  $("ov-save").addEventListener("click", ovSave);
  $("ov-revert").addEventListener("click", () => {      // 丟掉未儲存修改
    ovEntries.forEach((en) => { en.cur = { ...en.loaded }; });
    ovRender();
  });
  // 點背景關閉（點到內容不關）；上傳彈窗疊在上層，互不影響
  $("set-overlay").addEventListener("click", (e) => {
    if (e.target === $("set-overlay")) setClose();
  });
}

// --------------------------------------------------------------------------- //
// DTS patch 生成（兩段式流程第二段）——該板具備 baseline/ + dts_generation/
// 知識庫才在 plan 表格工具列顯示「產生 DTS」按鈕。
// --------------------------------------------------------------------------- //
let dtsAvailable = false;

async function initDts() {
  try {
    const q = currentBoard ? "?board=" + encodeURIComponent(currentBoard) : "";
    const d = await (await fetch("/api/dts/status" + q)).json();
    dtsAvailable = !!d.available;
  } catch (e) { dtsAvailable = false; }        // 舊後端沒有此端點：隱藏功能
}

// 後端每個回應都會回帶 board；以它為準同步前端狀態（反問來回維持同一塊板）
function syncBoard(board) {
  if (!board || board === currentBoard) return;
  currentBoard = board;
  const sel = $("board-select");
  if (sel) sel.value = board;
  updateIntroBoard();
  initDts();                                  // DTS 生成可用性依板而定
}

function setBoardEnabled(on) {
  const sel = $("board-select");
  if (sel) sel.disabled = !on;
}

// --------------------------------------------------------------------------- //
// DOM helpers — 對話以 append 方式累積，永遠不覆寫先前訊息
// --------------------------------------------------------------------------- //
function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}

function scrollBottom() {
  const chat = $("chat");
  chat.scrollTop = chat.scrollHeight;
}

function appendUser(text) {
  const msg = el("div", "msg user");
  const bubble = el("div", "bubble");
  bubble.textContent = text;                 // textContent：安全且保留換行
  msg.appendChild(bubble);
  $("messages").appendChild(msg);
  scrollBottom();
}

// 建一則 assistant 訊息，回傳它的 .body 供後續填內容（先放一個轉圈圈載入占位）。
// 等待標籤一律「思考中…」——一般使用者不需要分辨求解/驗證等內部階段，
// 階段細節由即時指標行（秒數/tokens）與最終輸出呈現。

// 等待中的即時指標行（灰字，chat 求解與 DTS 生成共用）：本地起錶每秒平順
// 跳動（同 bcMetrics 的決策——不隨輪詢重設基準）；progressUrl 每 3 秒輪詢
// 一次取 tokens（chat 用 /api/chat/progress、DTS 由呼叫端以 update(s) 餵入
// /api/dts/status 的欄位，不另開輪詢）。完成時呼叫 stop() 收掉計時器。
function attachLiveMetrics(progressUrl) {
  const line = el("div", "live-metrics");
  const startAt = Date.now();
  let tokens = 0;
  const render = () => {
    const sec = Math.max(0, Math.floor((Date.now() - startAt) / 1000));
    const m = Math.floor(sec / 60), s = sec % 60;
    line.textContent =
      `已思考 ${m ? m + " 分 " : ""}${s} 秒 ・ 已消耗 ${tokens.toLocaleString()} tokens`;
  };
  render();
  const tick = setInterval(render, 1000);
  let poll = null;
  if (progressUrl) {
    poll = setInterval(async () => {
      try {
        const d = await (await fetch(progressUrl)).json();
        if (d && d.tokens != null) tokens = d.tokens;
      } catch (e) { /* 網路抖動：略過，下一輪再試 */ }
    }, 3000);
  }
  return {
    el: line,
    update: (s) => { if (s && s.tokens != null) tokens = s.tokens; },
    // LLM 段結束後仍要繼續計秒（驗證階段）時：只停 tokens 輪詢、凍結 token 值
    stopPoll: () => { if (poll) { clearInterval(poll); poll = null; } },
    elapsedSec: () => Math.max(0, Math.floor((Date.now() - startAt) / 1000)),
    stop: () => {
      clearInterval(tick);
      if (poll) clearInterval(poll);
    },
  };
}

// 最終指標行（完成輸出用）：seconds/tokens 由伺服器凍結值提供
function finalMetricsLine(seconds, tokens) {
  if (seconds == null && tokens == null) return null;
  const sec = Math.round(seconds || 0);
  const m = Math.floor(sec / 60), s = sec % 60;
  return el("div", "live-metrics",
    `共思考 ${m ? m + " 分 " : ""}${s} 秒 ・ 消耗 ${(tokens || 0).toLocaleString()} tokens`);
}

function appendAssistant(label) {
  const msg = el("div", "msg assistant");
  msg.appendChild(el("div", "avatar", "P"));
  const body = el("div", "body");
  msg.appendChild(body);
  $("messages").appendChild(msg);

  if (label) {
    const load = el("div", "loading");
    load.appendChild(el("span", "spinner"));
    load.appendChild(el("span", "loading-label", label));
    body.appendChild(load);
  }
  // 求解等待的即時指標（每秒跳秒數；tokens 由 /api/chat/progress 輪詢）。
  // metrics 掛在 body 上：renderChat 進驗證階段時接手續跑（秒數不歸零）。
  const metrics = label ? attachLiveMetrics("/api/chat/progress") : null;
  if (metrics) body.appendChild(metrics.el);
  body._metrics = metrics;
  body._stopLoading = () => { if (metrics) metrics.stop(); };
  scrollBottom();
  return body;
}

function setStatus(body, text, kind) {
  if (body._stopLoading) body._stopLoading();
  body.innerHTML = "";
  const p = el("p", "status" + (kind ? " " + kind : ""));
  p.textContent = text;                       // textContent：error 訊息可能含 < > 等，勿當 HTML
  body.appendChild(p);
  scrollBottom();
}

// 一般對話式訊息（不支援的週邊、無解…）—— 中性文字，不用紅色錯誤樣式。
// LLM 文字常帶 markdown → 走 renderMarkdown（先跳脫再還原白名單，安全）。
function setNote(body, text) {
  body.innerHTML = "";
  const p = el("div", "reply note");
  p.innerHTML = renderMarkdown(text);
  body.appendChild(p);
  scrollBottom();
}

// --------------------------------------------------------------------------- //
// 網路
// --------------------------------------------------------------------------- //
async function postChat(message) {
  const payload = { message, board: currentBoard };
  if (chatSessionId) payload.session_id = chatSessionId;
  const r = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return { r, d: await r.json() };
}

// --------------------------------------------------------------------------- //
// 送出一則使用者訊息（Agent 對話）
// --------------------------------------------------------------------------- //
async function send() {
  const ta = $("q");
  const text = ta.value.trim();
  if (!text) return;                          // 空字串／純空白不送

  ta.value = "";                              // 清空輸入框
  ta.style.height = "auto";
  ta.focus();                                 // 保持 focus，方便繼續輸入

  appendUser(text);
  const body = appendAssistant("思考中…");
  $("go").disabled = true;
  setBoardEnabled(false);                      // 進行中鎖住板子選單，避免中途切板
  try {
    const { r, d } = await postChat(text);
    renderChat(r, d, body);
  } catch (e) {
    setStatus(body, "請求失敗：" + e.message, "err");
  } finally {
    $("go").disabled = false;
    setBoardEnabled(true);
  }
}

// --------------------------------------------------------------------------- //
// Agent 模式：渲染 orchestrator 的回覆（人話 + 一句為什麼）＋（若有）新的 plan 表格
//
// 有新 plan 時（伺服器已自動排入背景 CubeMX 驗證）：先「只」顯示思考中轉圈，
// 等驗證完成再一次輸出 LLM 回覆＋plan 表格＋驗證卡片——避免使用者先看到
// 未經驗證的結果。無 plan（clarify / unsat / 純回覆）或本輪模型已顯式驗過
// （d.validator）則立即完整渲染。
// --------------------------------------------------------------------------- //
function renderChat(r, d, body) {
  // 「思考中」的即時指標先不停——若接著進驗證階段要續跑（秒數不歸零，
  // 使用者不必分辨哪段有用 LLM）；不進驗證的路徑在下面各自收掉。
  const live = body._metrics || null;
  const stopLive = () => { if (live) live.stop(); body._metrics = null; };

  body.innerHTML = "";
  if (!r.ok) {
    stopLive();
    setNote(body, (d && d.error) || ("發生問題（HTTP " + r.status + "）"));
    return;
  }
  if (d.session_id) chatSessionId = d.session_id;   // 保存伺服器端 session
  if (d.board) syncBoard(d.board);

  const waitFp = (d.plan && d.plan.length && !d.validator && d.plan_fingerprint)
    ? d.plan_fingerprint : null;
  if (!waitFp) { stopLive(); renderChatContent(d, body, null); return; }

  // 背景驗證期間維持「思考中…」（使用者不需分辨內部階段）；完成（或被
  // 更新的 plan 取代／逾時）後一次輸出全部內容。
  // 指標行續跑：秒數延續本輪起錶點；tokens 凍結在 LLM 段的最終值。
  const load = el("div", "loading");
  load.appendChild(el("span", "spinner"));
  load.appendChild(el("span", "loading-label", "思考中…"));
  body.appendChild(load);
  if (live) {
    live.stopPoll();
    live.update({ tokens: d.usage_tokens });
    body.appendChild(live.el);
  }
  scrollBottom();
  waitForValidation(waitFp).then((result) => {
    // 最終秒數＝含驗證的總耗時（凍結即時指標最後的值）；沒有續跑的
    // 指標（重載等邊界）退回後端凍結的 LLM 耗時。
    const totalSec = live ? live.elapsedSec() : null;
    stopLive();
    body.innerHTML = "";
    renderChatContent(d, body, result, totalSec);   // result=null → 附表格時掛回輪詢備援
  });
}

// 一次渲染完整內容。autoResult = 背景自動驗證的 result（指紋已對上）或 null；
// totalSec = 含驗證階段的總耗時（renderChat 續跑的指標凍結值；null＝用後端
// 凍結的 LLM 耗時 d.seconds）。
function renderChatContent(d, body, autoResult, totalSec) {
  const reply = el("div", "reply");
  reply.innerHTML = renderMarkdown(d.reply || "（沒有回覆）");
  body.appendChild(reply);

  // CubeMX 驗證卡片：本輪模型顯式驗過（d.validator）或背景自動驗證完成
  const v = d.validator || autoResult;
  if (v) {
    body.appendChild(buildValidatorCard(v));
    setValidatorBadge(v.status);
  }

  // 只有「這一輪真的求解出新 plan」時才附表格（後端只在本輪 SAT 才回非空 plan）。
  // 驗證已完成（v）→ 不再輪詢；逾時/被取代（autoResult=null）→ 掛輪詢備援。
  if (d.plan && d.plan.length) {
    body.appendChild(buildResultBlock(
      d.plan, v ? null : d.plan_fingerprint, d.plan_fingerprint || null,
      d.usage_tokens));
  }

  // 驗證過的修改建議（G6）：卡片可一鍵採納
  if (d.suggestions && d.suggestions.length) {
    body.appendChild(buildSuggestionCards(d.suggestions));
  }

  // 待答歧義（clarify）：選項按鈕（與確定性模式同一套 UI）。選項來自
  // solve_pinmux 的 question.options（合法候選），點選即以文字送回對話，
  // 由編排模型把選擇折回 intent 重解。
  if (d.clarify && d.clarify.options && d.clarify.options.length) {
    body.appendChild(buildChatClarify(d.clarify));
  }

  // 最終指標：本輪求解（含驗證）的思考時間與 token 消耗。有 plan 表格時
  // 插在 result-block 內、DTS 反問之前——之後的 DTS 生成內容都掛在
  // result-block 尾端，這樣 plan 的指標留在 plan 輸出下方，不會跟 DTS
  // 卡片自己的指標擠在一起。
  const fm = finalMetricsLine(totalSec != null ? totalSec : d.seconds, d.usage_tokens);
  if (fm) {
    const ask = body.querySelector(".dts-ask");
    if (ask) ask.parentNode.insertBefore(fm, ask);
    else body.appendChild(fm);
  }
  scrollBottom();
}

// 等背景自動驗證完成：result 指紋 == fp 才回傳 result；被更新的 plan 取代或
// 逾時（8 分鐘）回 null（呼叫端照常渲染內容，表格處掛輪詢備援）。
async function waitForValidation(fp) {
  const deadline = Date.now() + 8 * 60 * 1000;
  let delay = 3000;                              // 第一次快查，之後 8 秒一輪
  while (Date.now() < deadline) {
    await new Promise((res) => setTimeout(res, delay));
    delay = 8000;
    try {
      const d = await (await fetch("/api/validator/status")).json();
      const got = d.exists && d.result && d.result.validated
        ? d.result.validated.fingerprint : null;
      if (!d.running && got === fp) return d.result;
      if (!d.running && got && got !== fp) return null;   // 已被更新的 plan 取代
    } catch (e) { /* 網路抖動：下一輪再試 */ }
  }
  return null;
}

function buildChatClarify(q) {
  const opts = el("div", "opts");
  q.options.forEach((o) => {
    const btn = el("button", "opt");
    btn.appendChild(el("span", "opt-label", escapeHtml(o.label)));
    if (o.note) btn.appendChild(el("span", "opt-note", escapeHtml(o.note)));
    btn.addEventListener("click", () => chooseChatOption(o, btn, opts));
    opts.appendChild(btn);
  });
  return opts;
}

async function chooseChatOption(option, btn, optsEl) {
  // 鎖住這組選項並標記被選的那顆（保留在對話紀錄中）
  optsEl.querySelectorAll("button").forEach((b) => (b.disabled = true));
  btn.classList.add("chosen");

  const text = "選擇：" + option.label + (option.note ? "（" + option.note + "）" : "");
  appendUser(text);
  const body = appendAssistant("思考中…");
  $("go").disabled = true;
  setBoardEnabled(false);
  try {
    const { r, d } = await postChat(text);
    renderChat(r, d, body);
  } catch (e) {
    setStatus(body, "請求失敗：" + e.message, "err");
  } finally {
    $("go").disabled = false;
    setBoardEnabled(true);
  }
}

// --------------------------------------------------------------------------- //
// validator（G5/G7）：徽章 + 衝突清單
// --------------------------------------------------------------------------- //
// header 徽章唯一的用途 = CubeMX 環境異常提示。status 為 "error"（找不到／
// 損壞的執行檔、逾時等環境問題）時亮「CubeMX 離線」，其餘一律隱藏——pass /
// fail / 未驗證都不佔用 header（衝突細節仍由訊息內的 val-card 呈現）。
function setValidatorBadge(status) {
  const badge = $("validator-badge");
  if (!badge) return;
  badge.hidden = status !== "error";
}

// 啟動時以伺服器上最近一次結果初始化徽章（覆寫制的 result.json）：只有上次
// 驗證因環境錯誤失敗才亮離線，pass/fail 不從磁碟狀態帶進 header。
async function initValidatorBadge() {
  try {
    const r = await fetch("/api/validator/status");
    const d = await r.json();
    setValidatorBadge(d.exists && d.result ? d.result.status : "none");
  } catch (e) { /* 舊後端沒有此端點：維持隱藏 */ }
}

// devicetree 區塊（result.devicetree，附加產物）→ 一行摘要；無 DT 時不顯示
function dtSummaryLine(v) {
  const dt = v.devicetree;
  if (!dt || dt.status !== "ok" || !(dt.files || []).length) return null;
  const dirs = [...new Set(dt.files.map((f) => f.split("/")[0]))].sort();
  return el("p", "note",
    `含 CubeMX 生成的 device tree（${dirs.join(" / ")}，共 ${dt.files.length} 檔）——由「⤓ CubeMX 驗證結果」下載`);
}

function buildValidatorCard(v) {
  const ok = v.status === "pass";
  const card = el("div", ok ? "val-card pass" : "val-card fail");
  // 驗證對象（result.validated）：讓每份報告可對外指認「這次驗了誰」
  const who = (v.validated && v.validated.instances || []).join("、");
  if (ok) {
    card.appendChild(el("p", "val-head",
      `✓ STM32CubeMX 驗證通過（${who ? who + " — " : ""}${v.checked_pins ?? "?"} 支腳，無衝突）`));
    const dt = dtSummaryLine(v);
    if (dt) card.appendChild(dt);
    return card;
  }
  if (v.status === "skipped") {
    // 該板 manifest 未啟用官方驗證（board.yaml validation.enabled=false）——
    // 中性呈現：不是錯誤也不是失敗，腳位正確性由 solver 知識庫保證。
    card.className = "val-card skip";
    card.appendChild(el("p", "val-head",
      `◦ 此板未啟用官方驗證（${who ? who + " — " : ""}${v.checked_pins ?? "?"} 支腳，依知識庫求解）`));
    return card;
  }
  if (v.status === "error") {
    card.className = "val-card err";
    card.appendChild(el("p", "val-head", "⚠ CubeMX 驗證未完成"));
    const p = el("p", "note");
    p.textContent = v.message || "未知原因";
    card.appendChild(p);
    return card;
  }
  card.appendChild(el("p", "val-head",
    `✗ STM32CubeMX 驗證發現 ${(v.conflicts || []).length} 項衝突`));
  const ul = el("ul");
  (v.conflicts || []).forEach((c) => {
    ul.appendChild(el("li", "mv",
      `<b>${escapeHtml(c.signal)}</b>@<b>${escapeHtml(c.pin)}</b> — ${escapeHtml(c.message || "")}`));
  });
  card.appendChild(ul);
  return card;
}

// --------------------------------------------------------------------------- //
// 修改建議卡片（G6）：點擊 = 把驗證過的 intent 送回 /api/chat 一鍵採納
// --------------------------------------------------------------------------- //
function buildSuggestionCards(suggestions) {
  const wrap = el("div", "sugg-cards");
  wrap.appendChild(el("p", "sugg-head", "驗證過的替代方案（點擊採納）"));
  suggestions.forEach((s) => {
    const btn = el("button", "sugg");
    btn.appendChild(el("span", "sugg-label", escapeHtml(s.summary)));
    btn.appendChild(el("span", "sugg-note", "✓ 已通過求解器驗證 · 點擊採納並重新求解"));
    btn.addEventListener("click", () => adoptSuggestion(s, btn, wrap));
    wrap.appendChild(btn);
  });
  return wrap;
}

async function adoptSuggestion(s, btn, wrap) {
  wrap.querySelectorAll("button").forEach((b) => (b.disabled = true));
  btn.classList.add("chosen");

  appendUser("採納建議：" + s.summary);
  const body = appendAssistant("思考中…");
  $("go").disabled = true;
  setBoardEnabled(false);
  try {
    const payload = { adopt: s, board: currentBoard };
    if (chatSessionId) payload.session_id = chatSessionId;
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    renderChat(r, d, body);
  } catch (e) {
    setStatus(body, "請求失敗：" + e.message, "err");
  } finally {
    $("go").disabled = false;
    setBoardEnabled(true);
  }
}

// markdown -> 安全 HTML：先整段跳脫，再只還原白名單語法（標題、清單、圍欄
// 程式碼、引用、分隔線、表格、粗斜體、行內 code、http(s) 連結）。不還原任何
// 使用者提供的標籤；連結 URL 禁含引號（escapeHtml 不跳脫引號→自防屬性注入）。
function renderMarkdown(src) {
  const esc = escapeHtml(src);
  const lines = esc.split("\n");

  // 行內語法。code span 先切出來保護，其餘段落才套粗斜體/連結。
  const inline = (s) => {
    const parts = s.split(/(`[^`]+`)/);
    return parts.map((seg) => {
      if (/^`[^`]+`$/.test(seg)) return "<code>" + seg.slice(1, -1) + "</code>";
      return seg
        .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)"']+)\)/g,
                 '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    }).join("");
  };

  let html = "", list = null;                   // list: "ul" | "ol" | null
  const closeList = () => { if (list) { html += "</" + list + ">"; list = null; } };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();

    // ``` 圍欄程式碼：收到下一個 ``` 為止，內容原樣（已跳脫）保留
    if (/^```/.test(line)) {
      closeList();
      const buf = [];
      for (i++; i < lines.length && !/^```/.test(lines[i].trim()); i++) buf.push(lines[i]);
      html += "<pre><code>" + buf.join("\n") + "</code></pre>";
      continue;
    }
    // GFM 表格：| 開頭連續行，第二行是 |---|--- 分隔列
    if (/^\|.*\|$/.test(line) && i + 1 < lines.length &&
        /^\|[\s:|-]+\|$/.test(lines[i + 1].trim())) {
      closeList();
      const cells = (l) => l.replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      html += "<table><thead><tr>" +
        cells(line).map((c) => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>";
      for (i += 2; i < lines.length && /^\|.*\|$/.test(lines[i].trim()); i++) {
        html += "<tr>" + cells(lines[i].trim()).map((c) => "<td>" + c + "</td>").join("") + "</tr>";
      }
      i--;
      html += "</tbody></table>";
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { closeList(); html += `<h${h[1].length}>` + inline(h[2]) + `</h${h[1].length}>`; continue; }
    if (/^(-{3,}|\*{3,})$/.test(line)) { closeList(); html += "<hr>"; continue; }
    if (/^&gt;\s?/.test(line)) {
      closeList();
      html += "<blockquote><p>" + inline(line.replace(/^&gt;\s?/, "")) + "</p></blockquote>";
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
      html += "<li>" + inline(line.replace(/^[-*]\s+/, "")) + "</li>";
      continue;
    }
    const ol = line.match(/^(\d+)[.)]\s+(.*)$/);
    if (ol) {
      if (list !== "ol") {
        closeList();
        html += ol[1] === "1" ? "<ol>" : `<ol start="${ol[1]}">`;
        list = "ol";
      }
      html += "<li>" + inline(ol[2]) + "</li>";
      continue;
    }
    closeList();
    if (line) html += "<p>" + inline(line) + "</p>";
  }
  closeList();
  return html || "<p></p>";
}

// 把 assignment 依 peripheral 分組（保留出現順序），供 rowspan 合併用
function groupByPeripheral(rows) {
  const order = [], map = {};
  rows.forEach((a) => {
    if (!(a.peripheral in map)) { map[a.peripheral] = []; order.push(a.peripheral); }
    map[a.peripheral].push(a);
  });
  return order.map((p) => ({ peripheral: p, rows: map[p] }));
}

// 工具列(複製) + 表格(peripheral 合併成一大格) + 下載 CSV/XLSX/CubeMX 驗證結果
// planFp = 這份 plan 的指紋（「產生 DTS」用它向伺服器指認畫面上的 plan；
// watchFp 只在還沒驗完時非空，兩者語意不同所以分開帶）。
function buildResultBlock(rows, watchFp, planFp, seedTokens) {
  const block = el("div", "result-block");

  // 有任何 row 帶板上外部 IC 才顯示 ic 欄（G7；無 IC 的板子表格維持四欄）
  const hasIc = rows.some((a) => a.ic);
  const table = el("table", "result");
  let html = "<thead><tr><th>peripheral</th><th>signal</th><th>pin</th><th>af</th>" +
             (hasIc ? "<th>ic</th>" : "") + "</tr></thead><tbody>";
  groupByPeripheral(rows).forEach((g) => {
    g.rows.forEach((a, i) => {
      html += "<tr>";
      if (i === 0) {
        // function 徽章：使用者對此介面的稱呼（RS232/RS485…），一個周邊標一次
        const fn = g.rows[0].function
          ? `<br><span class="badge-fn" style="margin-left:0" title="使用者需求中對此介面的稱呼">${escapeHtml(g.rows[0].function)}</span>`
          : "";
        html += `<td class="periph" rowspan="${g.rows.length}">${escapeHtml(g.peripheral)}${fn}</td>`;
      }
      const badge = a.boot_reserved
        ? ' <span class="badge-boot" title="require.json 自動保留的開機必要 signal">boot</span>'
        : (a.official_default
            ? ' <span class="badge-official" title="官方預設週邊，自動保留並鎖定官方腳位">官方</span>'
            : "");
      const af = a.af == null ? "" : a.af;
      html += `<td>${escapeHtml(a.signal)}${badge}</td><td class="pin">${escapeHtml(a.pin)}</td><td>${escapeHtml(af)}</td>`;
      if (hasIc) {
        // 同 peripheral 的 ic 相同：合併成一大格（與 peripheral 欄同 rowspan）
        if (i === 0) {
          html += `<td class="ic" rowspan="${g.rows.length}" title="板上外部 IC（board_components.json）">${escapeHtml(g.rows[0].ic || "")}</td>`;
        }
      }
      html += "</tr>";
    });
  });
  table.innerHTML = html + "</tbody>";
  block.appendChild(table);

  const dl = el("div", "downloads");
  const csvBtn = el("button", "icon-btn", "⤓ 下載 CSV");
  const xlsxBtn = el("button", "icon-btn", "⤓ 下載 XLSX");
  const copyBtn = el("button", "icon-btn", "⧉ 複製");
  csvBtn.addEventListener("click", () => downloadExport(rows, "csv", csvBtn));
  xlsxBtn.addEventListener("click", () => downloadExport(rows, "xlsx", xlsxBtn));
  copyBtn.addEventListener("click", () => copyRows(rows, copyBtn));
  dl.appendChild(csvBtn);
  dl.appendChild(xlsxBtn);
  dl.appendChild(copyBtn);
  const vBtn = buildValidatorDownloadBtn();
  dl.appendChild(vBtn);
  block.appendChild(dl);

  // 產生 DTS 的行內反問（是/否，clarify 同款 UI）：僅該板具備第二段知識庫
  // 且這份結果帶 plan 指紋時顯示。
  if (dtsAvailable && planFp) block.appendChild(buildDtsAsk(block, planFp));

  // 每個新 plan 伺服器都會自動排入背景 CubeMX 驗證：這裡輪詢直到「result 的
  // 指紋 == 這份 plan 的指紋」，把驗證卡片掛在表格下方、亮起下載鈕。
  if (watchFp) watchAutoValidation(block, vBtn, watchFp, seedTokens);

  return block;
}

// 輪詢背景自動驗證（fingerprint 對上才算「驗的是這份 plan」）。
// 使用者連續求解時，舊 plan 的 watcher 會在看到「閒置且指紋屬於別份 plan」時
// 靜默退場——永遠只有最新 plan 的卡片會出現。
async function watchAutoValidation(block, vBtn, watchFp, seedTokens) {
  const line = el("div", "loading");
  line.appendChild(el("span", "spinner"));
  line.appendChild(el("span", "loading-label", "思考中…"));
  block.appendChild(line);
  // 驗證中也持續顯示指標（秒數續算；tokens 凍結在 LLM 段的值——驗證不耗
  // token，但使用者不必分辨哪段有用 LLM）
  const metrics = attachLiveMetrics(null);
  if (seedTokens != null) metrics.update({ tokens: seedTokens });
  block.appendChild(metrics.el);
  const stopMetrics = () => { metrics.stop(); metrics.el.remove(); };

  const deadline = Date.now() + 8 * 60 * 1000;
  while (Date.now() < deadline) {
    try {
      const d = await (await fetch("/api/validator/status")).json();
      const fp = d.exists && d.result && d.result.validated
        ? d.result.validated.fingerprint : null;
      if (!d.running && fp === watchFp) {          // 驗完，而且驗的就是這份
        stopMetrics();
        line.remove();
        block.appendChild(buildValidatorCard(d.result));
        setValidatorBadge(d.result.status);
        vBtn.disabled = !d.downloadable;
        if (d.downloadable) vBtn.title = VDL_TITLE_READY;
        scrollBottom();
        return;
      }
      if (!d.running && fp && fp !== watchFp) {    // 已被更新的 plan 取代
        stopMetrics();
        line.remove();
        return;
      }
    } catch (e) { /* 網路抖動：下一輪再試 */ }
    await new Promise((r) => setTimeout(r, 8000));
  }
  stopMetrics();
  line.querySelector(".loading-label").textContent =
    "（驗證仍在進行——完成後可由「⤓ CubeMX 驗證結果」下載）";
  line.querySelector(".spinner").remove();
}

// 「下載 STM32CubeMX 編譯結果」：打包 output/validator/ 的 zip；無產物時 disabled
const VDL_TITLE_READY =
  "下載最近一次 STM32CubeMX 驗證的完整產物（log / pinout.csv / result.json / devicetree：kernel・u-boot・tf-a・optee-os）";
const VDL_TITLE_EMPTY =
  "尚無驗證產物——在對話中要求「驗證」跑一次 CubeMX 後即可下載";

function buildValidatorDownloadBtn() {
  const btn = el("button", "icon-btn", "⤓ CubeMX 驗證結果");
  btn.disabled = true;
  btn.title = VDL_TITLE_EMPTY;
  fetch("/api/validator/status")
    .then((r) => r.json())
    .then((d) => {
      btn.disabled = !d.downloadable;
      btn.title = d.downloadable ? VDL_TITLE_READY : VDL_TITLE_EMPTY;
    })
    .catch(() => {});
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const r = await fetch("/api/validator/download");
      if (!r.ok) throw new Error("no artifacts");
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "stm32cubemx_validation.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      const orig = btn.innerHTML;
      btn.innerHTML = "尚無驗證產物";
      setTimeout(() => (btn.innerHTML = orig), 1400);
    } finally {
      btn.disabled = false;
    }
  });
  return btn;
}

// 產生 DTS 的行內確認——與 clarify（指定 pin 反問）同一套 UI，取代舊的
// 工具列按鈕＋confirm() 彈窗：點「是」直接開跑、點「否」停在 plan 階段。
// 與 clarify 相同的互動慣例：選後鎖定整組、被選項標 .chosen、留在對話紀錄；
// 「是」在請求被接受前就失敗（409／網路錯誤）時解鎖，讓使用者可重試。
function buildDtsAsk(block, planFp) {
  const wrap = el("div", "dts-ask");
  // 主問句與流程提示拆成兩行（提示縮小、行距獨立調整，見 index.html .dts-ask 樣式）
  wrap.appendChild(el("p", "ask", "要接著產生 kernel DTS patch 嗎？"));
  wrap.appendChild(el("p", "ask-sub",
    "定位 → 生成 → 驗證 → 修復，約 1–5 分鐘，必要時會呼叫 LLM"));
  const opts = el("div", "opts");
  const mk = (label, note) => {
    const b = el("button", "opt");
    b.appendChild(el("span", "opt-label", label));
    b.appendChild(el("span", "opt-note", note));
    opts.appendChild(b);
    return b;
  };
  const yes = mk("是，產生 DTS patch", "以這份 plan 繼續（伺服器只認最新的解）");
  const no = mk("否，先到這裡", "停在 plan 階段；之後重新求解可再次選擇");
  const lock = (chosen) => {
    opts.querySelectorAll("button").forEach((b) => (b.disabled = true));
    chosen.classList.add("chosen");
  };
  const unlock = () => opts.querySelectorAll("button").forEach((b) => {
    b.disabled = false;
    b.classList.remove("chosen");
  });
  yes.addEventListener("click", () => {
    lock(yes);
    runDtsGeneration(block, planFp, unlock);
  });
  no.addEventListener("click", () => lock(no));
  wrap.appendChild(opts);
  return wrap;
}

// 開跑 DTS 生成：POST /api/dts/generate（只帶指紋，rows 一律由伺服器保存的
// 解供給——防偽）→ 輪詢 /api/dts/status → 掛結果卡片與下載。
// onEarlyFail：請求未被接受（409／網路錯誤）時呼叫，用來解鎖反問按鈕。
async function runDtsGeneration(block, planFp, onEarlyFail) {
  let d;
  try {
    const r = await fetch("/api/dts/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fingerprint: planFp }),
    });
    d = await r.json();
    if (!r.ok) {
      // textContent：d.error 可能含伺服器例外文字（< > 等），不可當 HTML
      const p = el("p", "note");
      p.textContent = "無法產生 DTS：" + (d.error || "HTTP " + r.status);
      block.appendChild(p);
      if (onEarlyFail) onEarlyFail();
      scrollBottom();
      return;
    }
  } catch (e) {
    const p = el("p", "note");
    p.textContent = "請求失敗：" + e.message;   // textContent：勿當 HTML
    block.appendChild(p);
    if (onEarlyFail) onEarlyFail();
    scrollBottom();
    return;
  }

  const line = el("div", "loading validating dts-progress");
  line.appendChild(el("span", "spinner"));
  line.appendChild(el("span", "loading-label",
    "DTS patch 生成中…（定位 → 生成 → 驗證 → 修復，約 1–5 分鐘）"));
  block.appendChild(line);
  // DTS 生成的即時指標：tokens 直接取自既有的 /api/dts/status 輪詢（update），
  // 不另開輪詢；秒數本地每秒平順跳動。
  const metrics = attachLiveMetrics(null);
  block.appendChild(metrics.el);
  scrollBottom();

  const deadline = Date.now() + 15 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((res) => setTimeout(res, 5000));
    try {
      const s = await (await fetch("/api/dts/status")).json();
      metrics.update(s);
      if (!s.running && s.result && s.result.fingerprint === planFp) {
        // 最終秒數＝凍結即時計數器（程式執行時間也算思考時間——cache 命中
        // 幾乎不耗時、tokens 0，但秒數仍如實反映使用者等待的時間）
        const totalSec = metrics.elapsedSec();
        metrics.stop(); metrics.el.remove();
        line.remove();
        block.appendChild(buildDtsCard(s.result, totalSec));
        scrollBottom();
        return;
      }
      if (!s.running && (!s.result || s.result.fingerprint !== planFp)) {
        metrics.stop(); metrics.el.remove();
        line.remove();
        const p = el("p", "note");
        p.textContent = s.result
          ? "生成結果屬於另一份 plan——請重新求解後再試一次。"
          : "伺服器可能已重啟，這次生成的狀態遺失——請重新求解後再產生一次。";
        block.appendChild(p);
        return;
      }
    } catch (e) { /* 網路抖動：下一輪再試 */ }
  }
  // 逾時（15 分鐘）：停止輪詢，但附上常駐下載鈕——完成後仍可取得產物。
  metrics.stop(); metrics.el.remove();
  line.querySelector(".loading-label").textContent =
    "（生成仍在進行或狀態已遺失——完成後可用下方按鈕下載產物）";
  line.querySelector(".spinner").remove();
  block.appendChild(buildDtsDownloadBtn());
}

// DTS 生成結果卡片。三種收尾：
// passed → 無外框、綠色標題＋產物展開／下載；needs-human（locator_blocked /
// needs_info / boot_conflict）→ 黃卡列出待補資訊；其他 → 紅卡附摘要。
function buildDtsCard(res, totalSec) {
  const NEEDS_HUMAN = ["locator_blocked", "needs_info", "boot_conflict"];

  // 成功：不用 val-card 綠框，只保留綠色標題文字。
  if (res.passed) {
    const done = el("div", "dts-done");
    // no_changes：plan 與官方預設一致——不需要 patch，仍展示完整 device tree
    if (res.no_changes) {
      done.appendChild(el("p", "dts-done-head",
        "✓ 不需要 patch——官方預設 device tree 已涵蓋這份 plan"));
      done.appendChild(el("p", "note",
        "plan 中的週邊在官方預設中都已啟用且腳位一致，無需任何修改；" +
        "完整產物（含各項 report）由下方按鈕下載。"));
    } else {
      done.appendChild(el("p", "dts-done-head",
        `✓ DTS patch 生成完成（修復 ${res.repair_rounds} 輪` +
        (res.compiled ? "，dtc 編譯通過" : "，未編譯——本機無 dtc/gcc") + "）"));
      // 完成說明：緊接標題（下載鈕在區塊最下方）
      done.appendChild(el("p", "note", "完整產物（含各項 report）由下方按鈕下載。"));
    }
    const pers = res.peripherals || [];
    if (pers.length) {
      const ul = el("ul");
      pers.forEach((p) => {
        ul.appendChild(el("li", "mv",
          `<b>${escapeHtml(p.peripheral)}</b> — ${escapeHtml(p.action)}` +
          (p.cache_hit ? "［cache］" : "")));
      });
      done.appendChild(ul);
    }
    // 產生的 device tree 介紹 + 產物檔的行內展開檢視（按鈕樣式同
    // 「是，產生 DTS patch」；點擊展開程式碼區塊、再點收合）。
    // no_changes 時沒有 generated.patch，只展示完整 .dts（＝官方預設）。
    done.appendChild(el("p", "note dts-intro", res.no_changes
      ? "以下是這塊板的完整 device tree（與官方預設一致，未做修改）："
      : "以下是根據你的需求週邊所產生、對應的 device tree —— " +
        "generated.patch 為 kernel DT patch，.dts 為套用後的完整 device tree，" +
        "點開按鈕即可檢視完整內容："));
    const files = el("div", "dts-files");
    if (!res.no_changes) files.appendChild(buildDtsFileToggle("generated.patch"));
    // 產物 .dts 檔名隨板變（<board>.generated.dts）——從 result.artifacts 找，
    // 找不到（極舊結果）退回以 result.board 拼名。
    const dtsName = (res.artifacts || []).find((f) => f.endsWith(".generated.dts"))
      || ((res.board || "stm32mp257f-ev1") + ".generated.dts");
    files.appendChild(buildDtsFileToggle(dtsName.split("/").pop()));
    done.appendChild(files);
    done.appendChild(buildDtsDownloadBtn());
    const fm = finalMetricsLine(totalSec != null ? totalSec : res.seconds, res.tokens);
    if (fm) done.appendChild(fm);
    return done;
  }

  const card = el("div",
    NEEDS_HUMAN.includes(res.stop_reason) ? "val-card err" : "val-card fail");
  if (NEEDS_HUMAN.includes(res.stop_reason)) {
    card.appendChild(el("p", "val-head",
      `⚠ 需要人工介入（${escapeHtml(res.stop_reason)}）`));
    const ul = el("ul");
    (res.ask_user || []).forEach((a) => {
      ul.appendChild(el("li", "mv", escapeHtml(
        typeof a === "string" ? a : JSON.stringify(a))));
    });
    card.appendChild(ul);
    card.appendChild(el("p", "note",
      "請依上述訊息調整需求或補充資訊後重新求解，再產生 DTS。"));
    return card;
  }

  card.appendChild(el("p", "val-head",
    `✗ DTS patch 生成失敗（${escapeHtml(res.stop_reason || "未知原因")}）`));
  if (res.error) {
    const p = el("p", "note");
    p.textContent = res.error;
    card.appendChild(p);
  }
  if (res.summary) {
    const pre = el("pre", "note");
    pre.textContent = res.summary;
    card.appendChild(pre);
  }
  const fm = finalMetricsLine(totalSec != null ? totalSec : res.seconds, res.tokens);
  if (fm) card.appendChild(fm);
  return card;
}

// 單一產物檔的行內展開檢視：按鈕（樣式同「是，產生 DTS patch」的 .opt）＋
// 收合的程式碼區塊。點擊切換展開/收合；內容懶載入一次（fetch 純文字）。
function buildDtsFileToggle(name) {
  const wrap = el("div", "dts-file");
  const btn = el("button", "opt dts-view");
  btn.appendChild(el("span", "opt-label", name));
  btn.appendChild(el("span", "opt-note", "點擊展開／收合完整內容"));
  const holder = el("div", "code-holder");
  holder.hidden = true;
  let loaded = false;
  btn.addEventListener("click", async () => {
    if (!holder.hidden) {                         // 已展開 → 收合
      holder.hidden = true;
      btn.classList.remove("chosen");
      return;
    }
    if (!loaded) {                                // 首次展開先載入內容
      btn.disabled = true;
      try {
        const r = await fetch("/api/dts/file?name=" + encodeURIComponent(name));
        if (!r.ok) throw new Error("HTTP " + r.status);
        holder.appendChild(buildCodeBlock(name, await r.text()));
      } catch (e) {
        const p = el("p", "note");
        p.textContent = "讀取失敗：" + e.message;   // textContent：勿當 HTML
        holder.appendChild(p);
      } finally {
        btn.disabled = false;
        loaded = true;
      }
    }
    holder.hidden = false;
    btn.classList.add("chosen");
    scrollBottom();
  });
  wrap.appendChild(btn);
  wrap.appendChild(holder);
  return wrap;
}

// ChatGPT 風格程式碼區塊：標題列（檔名＋複製鈕）＋行號 gutter＋程式碼。
// 不設高度上限：內容多就讓整頁往下延伸（無區塊內垂直捲軸）；只有超長行才
// 由 .code-scroll 水平捲動。行號與程式碼共用同一套字級/行高 → 逐行對齊。
function buildCodeBlock(name, text) {
  const block = el("div", "code-block");

  const head = el("div", "code-head");
  head.appendChild(el("span", "code-name", escapeHtml(name)));
  const copy = el("button", "code-copy", "複製");
  copy.addEventListener("click", () => copyText(text, copy, "複製"));
  head.appendChild(copy);
  block.appendChild(head);

  const body = el("div", "code-body");
  const shown = text.replace(/\n$/, "");          // 去掉單一尾端換行，免多一個空行號
  const nlines = shown.split("\n").length;
  const gutter = el("div", "code-gutter");
  gutter.textContent = Array.from({ length: nlines }, (_, i) => i + 1).join("\n");
  const scroll = el("div", "code-scroll");
  const pre = el("pre", "code-pre");
  pre.textContent = shown;                        // textContent：程式碼勿當 HTML
  scroll.appendChild(pre);
  body.appendChild(gutter);
  body.appendChild(scroll);
  block.appendChild(body);
  return block;
}

// 複製純文字（複製整個檔案的原始內容），成功後短暫回饋。
function copyText(text, btn, restore) {
  const flash = () => {
    btn.textContent = "✓ 已複製";
    setTimeout(() => (btn.textContent = restore), 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(flash).catch(() => fallbackCopy(text, flash));
  } else {
    fallbackCopy(text, flash);
  }
}

function buildDtsDownloadBtn() {
  const btn = el("button", "icon-btn dts-dl", "⤓ DTS patch 產物");
  btn.title = "下載 output/generated/ 全部產物（generated.patch、generated.dts、各 report）";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const r = await fetch("/api/dts/download");
      if (!r.ok) throw new Error("no artifacts");
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "dts_patch_artifacts.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      const orig = btn.innerHTML;
      btn.innerHTML = "尚無產物";
      setTimeout(() => (btn.innerHTML = orig), 1400);
    } finally {
      btn.disabled = false;
    }
  });
  return btn;
}

function copyRows(rows, btn) {
  const hasIc = rows.some((a) => a.ic);
  const hasFn = rows.some((a) => a.function);
  const head = "peripheral\tsignal\tpin\taf" + (hasIc ? "\tic" : "") + (hasFn ? "\tfunction" : "");
  const fnDone = new Set();   // function 欄與 CSV 同規則：一個周邊只標第一列
  const text = [head,
    ...rows.map((a) => {
      let fn = "";
      if (hasFn && !fnDone.has(a.peripheral)) {
        fn = a.function || "";
        fnDone.add(a.peripheral);
      }
      return `${a.peripheral}\t${a.signal}\t${a.pin}\t${a.af == null ? "" : a.af}` +
             (hasIc ? `\t${a.ic || ""}` : "") + (hasFn ? `\t${fn}` : "");
    })].join("\n");
  const flash = () => {
    const orig = btn.innerHTML;
    btn.innerHTML = "✓ 已複製";
    setTimeout(() => (btn.innerHTML = orig), 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(flash).catch(() => fallbackCopy(text, flash));
  } else {
    fallbackCopy(text, flash);
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { /* ignore */ }
  ta.remove();
}

async function downloadExport(rows, fmt, btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  try {
    const r = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows, format: fmt }),
    });
    if (!r.ok) throw new Error("export failed");
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "plan." + fmt;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    btn.innerHTML = "下載失敗";
    setTimeout(() => (btn.innerHTML = orig), 1400);
  } finally {
    btn.disabled = false;
  }
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// --------------------------------------------------------------------------- //
// 事件
// --------------------------------------------------------------------------- //
$("go").addEventListener("click", send);

$("q").addEventListener("keydown", (e) => {
  // Enter 送出；Shift+Enter 換行；輸入法組字中（IME）不送出
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    send();
  }
});

// 輸入框隨內容自動長高
$("q").addEventListener("input", function () {
  this.style.height = "auto";
  this.style.height = Math.min(this.scrollHeight, 180) + "px";
});

// 啟動：先過登入閘（/api/me），401 由 fetch 攔截顯示登入畫面且不載入任何
// 資料；通過後才載板子清單、CubeMX 徽章、DTS 可用性。
async function boot() {
  try {
    const r = await fetch("/api/me");
    if (!r.ok) {
      if (r.status !== 401) {             // 401 → showLogin 已被攔截觸發；
        showLogin(null);                  // 其他錯誤（DB 損毀 5xx…）也不能
        const err = $("login-error");     // 留白畫面——停在登入頁並說明原因
        if (err) err.textContent = "伺服器異常（HTTP " + r.status +
                                   "），請稍後重新整理再試。";
      }
      return;
    }
    AUTH.user = await r.json();
  } catch (e) {
    showLogin(null);                      // 連不上伺服器：也停在登入畫面
    return;
  }
  renderAccount();
  loadBoards().then(initDts);
  initValidatorBadge();
}

initBoardCreate();
initSettings();
initLogin();
boot();
