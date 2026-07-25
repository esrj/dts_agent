const $ = (id) => document.getElementById(id);

// 目前選定的目標板子；每個請求都會帶上它（無狀態後端依此載入對應 /data）
let currentBoard = null;

// Agent 對話的伺服器端 session id（後端首次回傳後保存；換板時清掉開新對話）
let chatSessionId = null;

// --------------------------------------------------------------------------- //
// 板子選擇 — 啟動時向 /api/boards 取得 data/boards/ 下自動偵測到的板子清單
// --------------------------------------------------------------------------- //
async function loadBoards() {
  const sel = $("board-select");
  if (!sel) return;
  try {
    const r = await fetch("/api/boards");
    const d = await r.json();
    const boards = (d.boards && d.boards.length) ? d.boards : [d.default];
    currentBoard = d.default || boards[0];
    sel.innerHTML = "";
    boards.forEach((b) => {
      const opt = document.createElement("option");
      opt.value = b;
      opt.textContent = b;
      if (b === currentBoard) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", onBoardChange);
  } catch (e) {
    // 取不到清單（舊後端 / 失敗）-> 隱藏選單，沿用後端預設板
    sel.style.display = "none";
  }
}

function onBoardChange(e) {
  currentBoard = e.target.value;
  // 換板等於開新話題：丟掉 Agent 對話 session，避免拿新板的 Σ/DTS
  // 去回答舊板的問題（後端 session 綁定某塊板的對話歷史）。
  chatSessionId = null;
  initDts();                                  // DTS 生成可用性依板而定
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
// validatePhase=true：請求超過閾值仍未回 → 幾乎必是在跑 CubeMX
// （系統中唯一的慢操作，1–3 分鐘）→ 把標籤切成「CubeMX 驗證中」。回應到達時
// 由 renderChat/setStatus 呼叫 body._stopLoading() 收掉計時器。
const VALIDATE_HINT_MS = 6000;
function appendAssistant(label, { validatePhase = false } = {}) {
  const msg = el("div", "msg assistant");
  msg.appendChild(el("div", "avatar", "P"));
  const body = el("div", "body");
  msg.appendChild(body);
  $("messages").appendChild(msg);

  let timer = null;
  if (label) {
    const load = el("div", "loading");
    load.appendChild(el("span", "spinner"));
    const lab = el("span", "loading-label", label);
    load.appendChild(lab);
    body.appendChild(load);
    if (validatePhase) {
      timer = setTimeout(() => {
        lab.textContent = "CubeMX 驗證中…（約 1–3 分鐘）";
        load.classList.add("validating");
      }, VALIDATE_HINT_MS);
    }
  }
  body._stopLoading = () => { if (timer) { clearTimeout(timer); timer = null; } };
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

// 一般對話式訊息（不支援的週邊、無解…）—— 中性文字，不用紅色錯誤樣式
function setNote(body, text) {
  body.innerHTML = "";
  const p = el("p", "note");
  p.textContent = text;
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
  const body = appendAssistant("思考中…", { validatePhase: true });
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
// 有新 plan 時（伺服器已自動排入背景 CubeMX 驗證）：先「只」顯示驗證中轉圈，
// 等驗證完成再一次輸出 LLM 回覆＋plan 表格＋驗證卡片——避免使用者先看到
// 未經驗證的結果。無 plan（clarify / unsat / 純回覆）或本輪模型已顯式驗過
// （d.validator）則立即完整渲染。
// --------------------------------------------------------------------------- //
function renderChat(r, d, body) {
  if (body._stopLoading) body._stopLoading();
  body.innerHTML = "";
  if (!r.ok) { setNote(body, (d && d.error) || ("發生問題（HTTP " + r.status + "）")); return; }
  if (d.session_id) chatSessionId = d.session_id;   // 保存伺服器端 session
  if (d.board) syncBoard(d.board);

  const waitFp = (d.plan && d.plan.length && !d.validator && d.plan_fingerprint)
    ? d.plan_fingerprint : null;
  if (!waitFp) { renderChatContent(d, body, null); return; }

  // 只顯示驗證中；完成（或被更新的 plan 取代／逾時）後一次輸出全部內容
  const load = el("div", "loading validating");
  load.appendChild(el("span", "spinner"));
  load.appendChild(el("span", "loading-label", "CubeMX 驗證中…（約 1–3 分鐘）"));
  body.appendChild(load);
  scrollBottom();
  waitForValidation(waitFp).then((result) => {
    body.innerHTML = "";
    renderChatContent(d, body, result);   // result=null → 附表格時掛回輪詢備援
  });
}

// 一次渲染完整內容。autoResult = 背景自動驗證的 result（指紋已對上）或 null。
function renderChatContent(d, body, autoResult) {
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
      d.plan, v ? null : d.plan_fingerprint, d.plan_fingerprint || null));
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
  const body = appendAssistant("思考中…", { validatePhase: true });
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
  const body = appendAssistant("依採納的方案重新求解中…", { validatePhase: true });
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

// 極簡 markdown -> 安全 HTML：先跳脫，再還原 **粗體**、`code`、- 清單、段落換行。
// 只允許這幾種，避免 XSS（不還原任何標籤）。
function renderMarkdown(src) {
  const esc = escapeHtml(src);
  const lines = esc.split("\n");
  let html = "", inList = false;
  const inline = (s) =>
    s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
     .replace(/`([^`]+)`/g, "<code>$1</code>");
  for (let raw of lines) {
    const line = raw.trim();
    if (/^[-*]\s+/.test(line)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += "<li>" + inline(line.replace(/^[-*]\s+/, "")) + "</li>";
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      if (line) html += "<p>" + inline(line) + "</p>";
    }
  }
  if (inList) html += "</ul>";
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
function buildResultBlock(rows, watchFp, planFp) {
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
        html += `<td class="periph" rowspan="${g.rows.length}">${escapeHtml(g.peripheral)}</td>`;
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
  if (watchFp) watchAutoValidation(block, vBtn, watchFp);

  return block;
}

// 輪詢背景自動驗證（fingerprint 對上才算「驗的是這份 plan」）。
// 使用者連續求解時，舊 plan 的 watcher 會在看到「閒置且指紋屬於別份 plan」時
// 靜默退場——永遠只有最新 plan 的卡片會出現。
async function watchAutoValidation(block, vBtn, watchFp) {
  const line = el("div", "loading");
  line.appendChild(el("span", "spinner"));
  line.appendChild(el("span", "loading-label", "CubeMX 自動驗證中…（約 1–3 分鐘）"));
  block.appendChild(line);

  const deadline = Date.now() + 8 * 60 * 1000;
  while (Date.now() < deadline) {
    try {
      const d = await (await fetch("/api/validator/status")).json();
      const fp = d.exists && d.result && d.result.validated
        ? d.result.validated.fingerprint : null;
      if (!d.running && fp === watchFp) {          // 驗完，而且驗的就是這份
        line.remove();
        block.appendChild(buildValidatorCard(d.result));
        setValidatorBadge(d.result.status);
        vBtn.disabled = !d.downloadable;
        if (d.downloadable) vBtn.title = VDL_TITLE_READY;
        scrollBottom();
        return;
      }
      if (!d.running && fp && fp !== watchFp) {    // 已被更新的 plan 取代
        line.remove();
        return;
      }
    } catch (e) { /* 網路抖動：下一輪再試 */ }
    await new Promise((r) => setTimeout(r, 8000));
  }
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
  scrollBottom();

  const deadline = Date.now() + 15 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((res) => setTimeout(res, 5000));
    try {
      const s = await (await fetch("/api/dts/status")).json();
      if (!s.running && s.result && s.result.fingerprint === planFp) {
        line.remove();
        block.appendChild(buildDtsCard(s.result));
        scrollBottom();
        return;
      }
      if (!s.running && (!s.result || s.result.fingerprint !== planFp)) {
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
  line.querySelector(".loading-label").textContent =
    "（生成仍在進行或狀態已遺失——完成後可用下方按鈕下載產物）";
  line.querySelector(".spinner").remove();
  block.appendChild(buildDtsDownloadBtn());
}

// DTS 生成結果卡片。三種收尾：
// passed → 無外框、綠色標題＋產物展開／下載；needs-human（locator_blocked /
// needs_info / boot_conflict）→ 黃卡列出待補資訊；其他 → 紅卡附摘要。
function buildDtsCard(res) {
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
  const head = "peripheral\tsignal\tpin\taf" + (hasIc ? "\tic" : "");
  const text = [head,
    ...rows.map((a) => `${a.peripheral}\t${a.signal}\t${a.pin}\t${a.af == null ? "" : a.af}` +
                       (hasIc ? `\t${a.ic || ""}` : ""))].join("\n");
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

// 範例 chip：點一下就填入並送出
document.querySelectorAll(".examples .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    $("q").value = chip.textContent;
    send();
  });
});

// 啟動：載入可選板子清單 + 以伺服器最近一次驗證結果初始化 CubeMX 徽章
// + 查詢 DTS 生成可用性（板子清單載完才知道 currentBoard）
loadBoards().then(initDts);
initValidatorBadge();
