/* CheckStock — интерфейс без сборки: один файл, ванильный JS, fetch к своему же API.
   Всё состояние lives в объекте state, после каждой мутации данные перезагружаются
   с сервера — клиент ничего не «додумывает» про остатки. */

const state = {
  meta: null,
  stock: null,
  documents: [],
  history: [],
  invariants: null,
  tab: "stock",
};

const KIND_LABELS = {
  receipt: "Приход",
  transfer: "Перемещение",
  fbs: "FBS-заказ",
  fbo: "FBO-поставка",
};

const STATUS_LABELS = {
  draft: "Черновик",
  posted: "Проведён",
  reserved: "Резерв",
  shipped: "В пути",
  received: "Принят",
  delivered: "Доставлен",
  cancelled: "Отменён",
};

const ACTION_PATHS = {
  "receipt:posted": (id) => `/api/receipts/${id}/post`,
  "transfer:shipped": (id) => `/api/transfers/${id}/ship`,
  "transfer:received": (id) => `/api/transfers/${id}/receive`,
  "transfer:cancelled": (id) => `/api/transfers/${id}/cancel`,
  "fbs:shipped": (id) => `/api/fbs/orders/${id}/ship`,
  "fbs:delivered": (id) => `/api/fbs/orders/${id}/deliver`,
  "fbs:cancelled": (id) => `/api/fbs/orders/${id}/cancel`,
  "fbo:shipped": (id) => `/api/fbo/shipments/${id}/ship`,
  "fbo:received": (id) => `/api/fbo/shipments/${id}/receive`,
  "fbo:cancelled": (id) => `/api/fbo/shipments/${id}/cancel`,
};

/* ---------------------------------------------------------------- утилиты */

class ApiError extends Error {
  constructor(payload, status) {
    const err = (payload && payload.error) || {};
    super(err.message || `HTTP ${status}`);
    this.code = err.code || "UNKNOWN";
    this.details = err.details || {};
    this.status = status;
  }
}

async function api(path, { method = "GET", body, key } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (e) {
    data = { error: { message: text } };
  }
  if (!res.ok) throw new ApiError(data, res.status);
  return {
    data,
    status: res.status,
    replayed: res.headers.get("Idempotency-Replayed") === "true",
  };
}

const esc = (value) =>
  String(value === null || value === undefined ? "" : value).replace(
    /[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]
  );

const num = (value) =>
  value ? `<span>${value}</span>` : `<span class="zero">0</span>`;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function toast(message, kind = "ok", timeout = 5000) {
  const box = $("#toasts");
  const node = document.createElement("div");
  node.className = `toast toast-${kind}`;
  node.textContent = message;
  box.appendChild(node);
  setTimeout(() => node.remove(), timeout);
}

function openModal(title, html) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $("#modal").hidden = false;
}

function closeModal() {
  $("#modal").hidden = true;
  $("#modal-body").innerHTML = "";
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function statusPill(status) {
  const cls =
    status === "shipped"
      ? "pill-transit"
      : status === "cancelled"
        ? "pill-danger"
        : status === "draft"
          ? ""
          : "pill-active";
  return `<span class="pill ${cls}">${esc(STATUS_LABELS[status] || status)}</span>`;
}

/* ---------------------------------------------------------------- загрузка */

async function loadMeta() {
  state.meta = (await api("/api/meta")).data;
}

async function loadStock() {
  const search = $("#stock-search").value.trim();
  const archived = $("#show-archived").checked;
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  if (archived) params.set("include_archived", "true");
  state.stock = (await api(`/api/stock?${params}`)).data;
}

async function loadDocuments() {
  const params = new URLSearchParams();
  const kind = $("#docs-kind").value;
  const status = $("#docs-status").value;
  const wh = $("#docs-warehouse").value;
  const sku = $("#docs-sku").value.trim();
  if (kind) params.set("kind", kind);
  if (status) params.set("status", status);
  if (wh) params.set("warehouse_id", wh);
  if (sku) params.set("sku", sku);
  params.set("limit", "100");
  state.documents = (await api(`/api/documents?${params}`)).data;
}

async function loadHistory() {
  const params = new URLSearchParams();
  const kind = $("#hist-kind").value;
  const wh = $("#hist-warehouse").value;
  const sku = $("#hist-sku").value.trim();
  if (kind) params.set("kind", kind);
  if (wh) params.set("warehouse_id", wh);
  if (sku) params.set("sku", sku);
  params.set("limit", $("#hist-limit").value);
  state.history = (await api(`/api/history?${params}`)).data;
}

async function loadInvariants() {
  state.invariants = (await api("/api/invariants")).data;
}

async function refreshAll() {
  try {
    await Promise.all([loadMeta(), loadStock(), loadDocuments(), loadHistory(), loadInvariants()]);
    renderAll();
  } catch (err) {
    toast(`Не удалось загрузить данные: ${err.message}`, "error", 9000);
  }
}

/* ---------------------------------------------------------------- рендер */

function renderAll() {
  renderShop();
  renderInvariants();
  syncSelects();
  renderStock();
  renderWarehouses();
  renderDocuments();
  renderHistory();
}

function renderShop() {
  if (state.meta) $("#shop-name").textContent = `${state.meta.shop.name} · где сейчас товар`;
}

function renderInvariants() {
  const badge = $("#invariant-badge");
  if (!state.invariants) return;
  const ok = state.invariants.ok;
  badge.className = `badge ${ok ? "badge-ok" : "badge-bad"}`;
  badge.textContent = ok ? "✓ Остатки сходятся" : "✗ Учёт расходится!";
}

function optionsHtml(items, selected, placeholder) {
  const head = placeholder ? `<option value="">${esc(placeholder)}</option>` : "";
  return (
    head +
    items
      .map(
        (it) =>
          `<option value="${esc(it.value)}"${String(it.value) === String(selected) ? " selected" : ""}>${esc(it.label)}</option>`
      )
      .join("")
  );
}

function fillSelect(select, items, placeholder) {
  const prev = select.value;
  select.innerHTML = optionsHtml(items, prev, placeholder);
  if (prev && !items.some((it) => String(it.value) === prev)) select.value = "";
}

function syncSelects() {
  if (!state.meta) return;
  const active = state.meta.warehouses.filter((w) => !w.archived);
  const whItems = active.map((w) => ({ value: w.id, label: w.name }));
  const allWhItems = state.meta.warehouses.map((w) => ({
    value: w.id,
    label: w.archived ? `${w.name} (архив)` : w.name,
  }));
  const prodItems = state.meta.products.map((p) => ({ value: p.sku, label: `${p.sku} · ${p.name}` }));

  $$("select[data-warehouses]").forEach((sel) => fillSelect(sel, whItems, null));
  ["#docs-warehouse", "#hist-warehouse"].forEach((sel) =>
    fillSelect($(sel), allWhItems, "все склады")
  );
  fillSelect(
    $("#docs-kind"),
    Object.entries(KIND_LABELS).map(([value, label]) => ({ value, label })),
    "все типы"
  );
  fillSelect(
    $("#hist-kind"),
    Object.entries(KIND_LABELS).map(([value, label]) => ({ value, label })),
    "все типы"
  );
  fillSelect(
    $("#docs-status"),
    Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label })),
    "все статусы"
  );
  $$("select[data-line-sku]").forEach((sel) => fillSelect(sel, prodItems, null));
  $$("[data-lines]").forEach((box) => {
    if (!box.children.length) addLineRow(box);
  });
}

function addLineRow(box) {
  const products = state.meta ? state.meta.products : [];
  const row = document.createElement("div");
  row.className = "line-row";
  row.innerHTML = `
    <select data-line-sku>${optionsHtml(
      products.map((p) => ({ value: p.sku, label: `${p.sku} · ${p.name}` })),
      null,
      null
    )}</select>
    <input type="number" data-line-qty min="1" step="1" value="1" aria-label="Количество">
    <button class="btn btn-ghost btn-sm" type="button" data-action="remove-line" title="Убрать строку">✕</button>`;
  box.appendChild(row);
}

function renderStock() {
  if (!state.stock) return;
  const { warehouses, rows, grand_totals: totals } = state.stock;

  $("#stock-cards").innerHTML = [
    ["Физически на ФФ", totals.on_hand],
    ["Доступно", totals.available],
    ["Резерв FBS", totals.reserved],
    ["В пути", totals.in_transit],
    ["На складе МП", totals.marketplace],
    ["У клиента", totals.delivered],
  ]
    .map(
      ([label, value]) =>
        `<div class="stat"><div class="value">${value}</div><div class="label">${esc(label)}</div></div>`
    )
    .join("");

  const groupHead =
    `<tr class="group"><th colspan="2">Товар</th>` +
    warehouses
      .map(
        (w) =>
          `<th colspan="3" class="sep${w.archived ? " archived-name" : ""}">${esc(w.name)}${w.archived ? " (архив)" : ""}</th>`
      )
      .join("") +
    `<th colspan="3" class="sep">В пути</th><th colspan="2" class="sep">Принято</th>` +
    `<th rowspan="2" class="sep num">Приход всего</th></tr>`;

  const subHead =
    `<tr><th>Артикул</th><th>Название</th>` +
    warehouses
      .map(
        () =>
          `<th class="num sep">Физ.</th><th class="num">Дост.</th><th class="num">Резерв</th>`
      )
      .join("") +
    `<th class="num sep">между ФФ</th><th class="num">на МП (FBO)</th><th class="num">к клиенту (FBS)</th>` +
    `<th class="num sep">Склад МП</th><th class="num">У клиента</th></tr>`;

  const body = rows
    .map((row) => {
      const cells = warehouses
        .map((w) => {
          const cell = row.warehouses.find((c) => c.warehouse_id === w.id) || {
            on_hand: 0,
            available: 0,
            reserved: 0,
          };
          return (
            `<td class="num sep">${num(cell.on_hand)}</td>` +
            `<td class="num">${num(cell.available)}</td>` +
            `<td class="num">${num(cell.reserved)}</td>`
          );
        })
        .join("");
      return (
        `<tr><td><button class="sku-link" data-action="where" data-sku="${esc(row.sku)}">${esc(row.sku)}</button></td>` +
        `<td>${esc(row.name)}</td>${cells}` +
        `<td class="num sep">${num(row.in_transit.between_ff)}</td>` +
        `<td class="num">${num(row.in_transit.to_marketplace)}</td>` +
        `<td class="num">${num(row.in_transit.to_client)}</td>` +
        `<td class="num sep">${num(row.marketplace)}</td>` +
        `<td class="num">${num(row.delivered)}</td>` +
        `<td class="num sep">${num(row.received_total)}</td></tr>`
      );
    })
    .join("");

  const sum = (fn) => rows.reduce((acc, r) => acc + fn(r), 0);
  const foot =
    `<tfoot><tr><td colspan="2">Итого</td>` +
    warehouses
      .map((w) => {
        const get = (r, field) =>
          (r.warehouses.find((c) => c.warehouse_id === w.id) || {})[field] || 0;
        return (
          `<td class="num sep">${sum((r) => get(r, "on_hand"))}</td>` +
          `<td class="num">${sum((r) => get(r, "available"))}</td>` +
          `<td class="num">${sum((r) => get(r, "reserved"))}</td>`
        );
      })
      .join("") +
    `<td class="num sep">${sum((r) => r.in_transit.between_ff)}</td>` +
    `<td class="num">${sum((r) => r.in_transit.to_marketplace)}</td>` +
    `<td class="num">${sum((r) => r.in_transit.to_client)}</td>` +
    `<td class="num sep">${sum((r) => r.marketplace)}</td>` +
    `<td class="num">${sum((r) => r.delivered)}</td>` +
    `<td class="num sep">${sum((r) => r.received_total)}</td></tr></tfoot>`;

  $("#stock-table").innerHTML = `<thead>${groupHead}${subHead}</thead><tbody>${
    body || `<tr><td colspan="20" class="muted">Ничего не найдено</td></tr>`
  }</tbody>${foot}`;
}

function renderWarehouses() {
  if (!state.meta) return;
  const rows = state.meta.warehouses
    .map((w) => {
      const actions = w.archived
        ? `<button class="btn btn-sm" data-action="restore-wh" data-id="${w.id}">Восстановить</button>`
        : `<button class="btn btn-sm" data-action="rename-wh" data-id="${w.id}" data-name="${esc(w.name)}">Переименовать</button>
           <button class="btn btn-sm btn-danger" data-action="archive-wh" data-id="${w.id}" data-name="${esc(w.name)}">Удалить (в архив)</button>`;
      return `<tr>
        <td><b>${esc(w.name)}</b></td>
        <td>${w.archived ? '<span class="pill pill-archived">архив</span>' : '<span class="pill pill-active">активный</span>'}</td>
        <td class="num">${num(w.units_on_hand)}</td>
        <td><button class="btn btn-sm btn-ghost" data-action="wh-stock" data-id="${w.id}" data-name="${esc(w.name)}">что лежит</button></td>
        <td><div class="actions">${actions}</div></td>
      </tr>`;
    })
    .join("");
  $("#warehouses-table").innerHTML =
    `<thead><tr><th>Склад</th><th>Статус</th><th class="num">Единиц на складе</th><th>Остатки</th><th>Действия</th></tr></thead>
     <tbody>${rows || `<tr><td colspan="5" class="muted">Складов нет</td></tr>`}</tbody>`;
}

function routeLabel(doc) {
  const from = doc.from_warehouse ? doc.from_warehouse.name : "—";
  if (doc.kind === "receipt") return `Поставщик → ${doc.to_warehouse ? doc.to_warehouse.name : "—"}`;
  if (doc.kind === "transfer")
    return `${from} → ${doc.to_warehouse ? doc.to_warehouse.name : "—"}`;
  if (doc.kind === "fbo") return `${from} → склад МП`;
  return `${from} → клиент`;
}

function renderDocuments() {
  const rows = state.documents
    .map((doc) => {
      const lines = doc.lines
        .map((l) => `<span class="move-line">${esc(l.sku)} × <b>${l.qty}</b></span>`)
        .join("");
      const actions = doc.actions
        .map(
          (a) =>
            `<button class="btn btn-sm${a.to_status === "cancelled" ? " btn-danger" : " btn-primary"}"
              data-action="doc-action" data-id="${doc.id}" data-kind="${doc.kind}" data-status="${a.to_status}">${esc(a.label)}</button>`
        )
        .join("");
      const doubleBtn = doc.actions.length
        ? `<button class="btn btn-sm btn-ghost" data-action="doc-double" data-id="${doc.id}" data-kind="${doc.kind}" data-status="${doc.actions[0].to_status}"
             title="Отправить подтверждение три раза и показать, что списание одно">Повтор ×2</button>`
        : "";
      return `<tr>
        <td>#${doc.id}</td>
        <td>${esc(doc.kind_label)}${doc.reference ? `<div class="route">${esc(doc.reference)}</div>` : ""}</td>
        <td>${esc(routeLabel(doc))}</td>
        <td>${statusPill(doc.status)}</td>
        <td>${lines}</td>
        <td>${fmtTime(doc.updated_at)}</td>
        <td><div class="actions">${actions}${doubleBtn}</div></td>
      </tr>`;
    })
    .join("");
  $("#documents-table").innerHTML =
    `<thead><tr><th>№</th><th>Тип</th><th>Маршрут</th><th>Статус</th><th>Товары</th><th>Обновлён</th><th>Действия</th></tr></thead>
     <tbody>${rows || `<tr><td colspan="7" class="muted">Документов нет — оформи приход или заказ выше</td></tr>`}</tbody>`;
}

function renderHistory() {
  const rows = state.history
    .map((event) => {
      const moves = event.moves.length
        ? event.moves
            .map(
              (m) =>
                `<tr><td>${esc(m.sku)}</td><td class="num"><b>${m.qty}</b></td><td>${esc(m.from)}</td><td>${esc(m.to)}</td></tr>`
            )
            .join("")
        : `<tr><td colspan="4" class="muted">без движения товара (черновик/отмена черновика)</td></tr>`;
      return `<tr>
        <td>${fmtTime(event.at)}</td>
        <td>#${event.document_id}<div class="route">${esc(event.kind_label)}</div></td>
        <td>${statusPill(event.to_status)}${event.idempotency_key ? `<div class="route" title="Idempotency-Key">key: ${esc(event.idempotency_key)}</div>` : ""}</td>
        <td><table class="grid"><tbody>${moves}</tbody></table></td>
      </tr>`;
    })
    .join("");
  $("#history-table").innerHTML =
    `<thead><tr><th>Время</th><th>Документ</th><th>Операция</th><th>Что куда переместилось</th></tr></thead>
     <tbody>${rows || `<tr><td colspan="4" class="muted">Событий нет</td></tr>`}</tbody>`;
}

/* ---------------------------------------------------------------- сценарии */

async function showWhere(sku) {
  const data = (await api(`/api/stock/${encodeURIComponent(sku)}/where`)).data;
  const rows = data.positions
    .map(
      (p) => `<tr>
        <td>${esc(p.label)}</td>
        <td class="num"><b>${p.qty}</b></td>
        <td>${p.document ? `#${p.document.id} ${esc(p.document.kind_label)} · ${esc(p.document.status_label)}` : "—"}</td>
      </tr>`
    )
    .join("");
  openModal(
    `Где числится ${data.sku} · ${data.name}`,
    `<table class="grid"><thead><tr><th>Место учёта</th><th class="num">Единиц</th><th>Документ</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="3" class="muted">Товар нигде не числится</td></tr>`}</tbody></table>
     <div class="hint">Сумма по всем узлам, включая поставщика (он в минусе на величину прихода),
     равна <b>${data.total_accounted}</b> — это и есть проверка «товар не потерялся и не появился из ниоткуда».</div>`
  );
}

async function showWarehouseStock(id, name) {
  const data = (await api(`/api/warehouses/${id}/stock`)).data;
  const rows = data
    .map(
      (r) =>
        `<tr><td>${esc(r.sku)}</td><td>${esc(r.name)}</td><td>${r.bucket === "free" ? "доступно" : "резерв FBS"}</td><td class="num"><b>${r.qty}</b></td></tr>`
    )
    .join("");
  openModal(
    `Остатки склада «${name}»`,
    `<table class="grid"><thead><tr><th>Артикул</th><th>Товар</th><th>Карман</th><th class="num">Единиц</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="4" class="muted">Пусто</td></tr>`}</tbody></table>`
  );
}

async function confirmArchive(id, name) {
  const data = (await api(`/api/warehouses/${id}/stock`)).data;
  const units = data.reduce((acc, r) => acc + r.qty, 0);
  const rows = data
    .map(
      (r) =>
        `<tr><td>${esc(r.sku)}</td><td>${r.bucket === "free" ? "доступно" : "резерв FBS"}</td><td class="num"><b>${r.qty}</b></td></tr>`
    )
    .join("");
  openModal(
    `Удалить склад «${name}»?`,
    `<p>На складе числится <b>${units}</b> ед. товара.</p>
     ${rows ? `<table class="grid"><tbody>${rows}</tbody></table>` : ""}
     <div class="hint hint-warn">Склад будет отправлен в архив: остатки и история сохранятся,
     склад останется виден в таблице остатков и в «где числится товар». Новые документы на него
     создать нельзя, начатые — можно завершить. Физически строка не удаляется: иначе потерялась бы история.</div>
     <div class="modal-actions">
       <button class="btn btn-danger" data-action="archive-wh-confirm" data-id="${id}">Да, в архив</button>
       <button class="btn" data-action="close-modal">Отмена</button>
     </div>`
  );
}

function showRename(id, name) {
  openModal(
    "Переименовать склад",
    `<label>Новое имя<input id="rename-input" value="${esc(name)}" maxlength="120"></label>
     <div class="modal-actions">
       <button class="btn btn-primary" data-action="rename-wh-confirm" data-id="${id}">Сохранить</button>
       <button class="btn" data-action="close-modal">Отмена</button>
     </div>
     <p class="muted small">Имя подставляется во все таблицы, историю и товар в пути — оно нигде не захардкожено.</p>`
  );
}

function showInvariants() {
  if (!state.invariants) return;
  const rows = state.invariants.checks
    .map(
      (c) => `<tr>
        <td>${c.ok ? "✓" : "✗"}</td>
        <td><b>${esc(c.name)}</b><div class="route">${esc(c.description)}</div></td>
        <td>${c.ok ? '<span class="pill pill-active">ok</span>' : `<span class="pill pill-danger">${c.violations.length} нарушений</span>`}</td>
      </tr>`
    )
    .join("");
  openModal(
    "Проверка сходимости учёта",
    `<table class="grid"><tbody>${rows}</tbody></table>
     <div class="hint">Эти же проверки вызываются тестами после каждого действия и доступны как
     <code>GET /api/invariants</code>.</div>`
  );
}

let pendingShortage = null;

function showShortage(error, retry) {
  const d = error.details || {};
  const others = (d.other_warehouses || [])
    .map((o) => `<tr><td>${esc(o.name)}</td><td class="num">${o.available}</td></tr>`)
    .join("");
  const plan = (d.transfer_plan || [])
    .map(
      (p) =>
        `<li>переместить <b>${p.qty}</b> шт с «${esc(p.from_name)}» на «${esc(p.to_name)}»</li>`
    )
    .join("");
  const split = (d.split_plan || [])
    .map((s) => `<li>${esc(s.name)} — <b>${s.qty}</b> шт</li>`)
    .join("");
  openModal(
    "Не хватает товара на выбранном ФФ",
    `<p>${esc(error.message)}</p>
     <table class="grid"><thead><tr><th>Артикул</th><th class="num">Нужно</th><th class="num">На выбранном ФФ</th><th class="num">Всего по сети</th></tr></thead>
       <tbody><tr><td>${esc(d.sku || "")}</td><td class="num">${d.requested ?? "—"}</td>
       <td class="num">${d.available_on_warehouse ?? "—"}</td><td class="num">${d.total_available ?? "—"}</td></tr></tbody></table>
     ${others ? `<p class="muted small">Доступно на других ФФ:</p><table class="grid"><tbody>${others}</tbody></table>` : ""}
     ${plan ? `<div class="hint"><b>Вариант 1 — закрыть дефицит перемещением:</b><ul>${plan}</ul></div>` : ""}
     ${split ? `<div class="hint"><b>Вариант 2 — разбить отгрузку по складам:</b><ul>${split}</ul></div>` : ""}
     <div class="modal-actions">
       ${plan ? `<button class="btn btn-primary" data-action="shortage-transfer">Переместить и повторить</button>` : ""}
       ${split && retry.allowSplit ? `<button class="btn btn-primary" data-action="shortage-split">Разбить по складам</button>` : ""}
       <button class="btn" data-action="close-modal">Отмена</button>
     </div>`
  );
  pendingShortage = { details: d, retry };
}

async function runPendingTransfer() {
  if (!pendingShortage) return;
  const { details, retry } = pendingShortage;
  for (const step of details.transfer_plan || []) {
    await api("/api/transfers/quick", {
      method: "POST",
      body: {
        from_warehouse_id: step.from_warehouse_id,
        to_warehouse_id: step.to_warehouse_id,
        lines: [{ sku: details.sku, qty: step.qty }],
        reference: `Подпитка под ${retry.label || "заказ"}`,
      },
    });
  }
  closeModal();
  toast("Дефицит закрыт перемещением, повторяю операцию…");
  await retry.run();
}

async function runPendingSplit() {
  if (!pendingShortage) return;
  const { retry } = pendingShortage;
  closeModal();
  await retry.run({ allow_split: true });
}

/* ---------------------------------------------------------------- формы */

function readLines(form) {
  return Array.from(form.querySelectorAll(".line-row"))
    .map((row) => ({
      sku: row.querySelector("[data-line-sku]").value,
      qty: Number(row.querySelector("[data-line-qty]").value),
    }))
    .filter((l) => l.sku && l.qty > 0);
}

function formPayload(form) {
  const data = new FormData(form);
  const payload = { lines: readLines(form) };
  for (const [key, value] of data.entries()) {
    if (key.endsWith("warehouse_id")) payload[key] = Number(value);
    else if (["allow_split", "quick", "auto_post"].includes(key)) continue;
    else if (String(value).trim()) payload[key] = String(value).trim();
  }
  // Чекбоксы читаем напрямую: снятый чекбокс в FormData не попадает вообще.
  ["allow_split", "quick", "auto_post"].forEach((flag) => {
    if (form.elements[flag]) payload[flag] = form.elements[flag].checked;
  });
  return payload;
}

async function submitReceipt(form) {
  const p = formPayload(form);
  const autoPost = p.auto_post === true;
  delete p.auto_post;
  const created = (await api("/api/receipts", { method: "POST", body: p })).data;
  if (autoPost) {
    await api(`/api/receipts/${created.id}/post`, {
      method: "POST",
      key: `ui-receipt-${created.id}-posted`,
    });
    toast(`Приход #${created.id} проведён: товар на складе`);
  } else {
    toast(`Приход #${created.id} создан черновиком — проведи его в списке документов`);
  }
  await refreshAll();
}

async function submitTransfer(form) {
  const p = formPayload(form);
  const quick = p.quick === true;
  delete p.quick;
  if (p.from_warehouse_id === p.to_warehouse_id) {
    toast("Склады отправителя и получателя совпадают", "error");
    return;
  }
  const path = quick ? "/api/transfers/quick" : "/api/transfers";
  const doc = (await api(path, { method: "POST", body: p })).data;
  toast(
    quick
      ? `Перемещение #${doc.id} отправлено и принято`
      : `Перемещение #${doc.id} создано — отправь его в списке документов`
  );
  await refreshAll();
}

async function submitFbs(form, options = {}) {
  const p = formPayload(form);
  if (options.allow_split) p.allow_split = true;
  try {
    const data = (await api("/api/fbs/orders", { method: "POST", body: p })).data;
    toast(
      data.split
        ? `Заказ разбит на ${data.documents.length} отправления: ${data.documents.map((d) => `#${d.id}`).join(", ")}`
        : `Заказ ${p.reference || ""} зарезервирован: документ #${data.documents[0].id}`
    );
    await refreshAll();
  } catch (err) {
    if (err.code === "INSUFFICIENT_STOCK") {
      showShortage(err, {
        label: p.reference || "FBS-заказ",
        allowSplit: true,
        run: (opts) => submitFbs(form, opts || {}),
      });
      return;
    }
    throw err;
  }
}

async function submitFbo(form) {
  const p = formPayload(form);
  try {
    const doc = (await api("/api/fbo/shipments", { method: "POST", body: p })).data;
    toast(`FBO-поставка #${doc.id} создана — отправь её в списке документов`);
    await refreshAll();
  } catch (err) {
    if (err.code === "INSUFFICIENT_STOCK") {
      showShortage(err, { label: p.reference || "FBO-поставку", allowSplit: false, run: () => submitFbo(form) });
      return;
    }
    throw err;
  }
}

/* ---------------------------------------------------------------- действия */

function actionPath(kind, id, status) {
  const build = ACTION_PATHS[`${kind}:${status}`];
  if (!build) throw new Error(`Неизвестное действие ${kind}:${status}`);
  return build(id);
}

async function runDocumentAction(id, kind, status) {
  const path = actionPath(kind, id, status);
  const res = await api(path, { method: "POST", key: `ui-${kind}-${id}-${status}` });
  toast(
    res.replayed
      ? `Документ #${id}: операция уже была выполнена (повтор безопасен)`
      : `Документ #${id}: ${STATUS_LABELS[status] || status}`
  );
  await refreshAll();
}

async function demoDoubleConfirm(id, kind, status) {
  const path = actionPath(kind, id, status);
  const key = `demo-${kind}-${id}-${status}-${Date.now()}`;
  const attempts = [];
  for (const [label, useKey] of [
    ["1-й запрос (с Idempotency-Key)", true],
    ["2-й запрос (тот же ключ)", true],
    ["3-й запрос (без ключа)", false],
  ]) {
    try {
      const res = await api(path, { method: "POST", key: useKey ? key : undefined });
      attempts.push([label, `${res.status}${res.replayed ? " (повтор, ответ из снимка)" : ""}`, "ok"]);
    } catch (err) {
      attempts.push([label, `${err.status} ${err.code}`, err.status === 409 ? "expected" : "bad"]);
    }
  }
  const rows = attempts
    .map(
      ([label, result, kindOf]) =>
        `<tr><td>${esc(label)}</td><td>${esc(result)}</td><td>${
          kindOf === "ok" ? "✓ выполнено" : kindOf === "expected" ? "✓ отклонено, списания нет" : "✗ неожиданно"
        }</td></tr>`
    )
    .join("");
  const stock = (await api("/api/stock")).data;
  const inv = (await api("/api/invariants")).data;
  openModal(
    `Повторное подтверждение документа #${id}`,
    `<table class="grid"><thead><tr><th>Запрос</th><th>Ответ</th><th>Итог</th></tr></thead><tbody>${rows}</tbody></table>
     <div class="hint">Товар списан ровно один раз: движение записано одним событием.
     Проверка сходимости после серии повторов: <b>${inv.ok ? "остатки сходятся ✓" : "РАСХОЖДЕНИЕ ✗"}</b>.
     Физически на ФФ сейчас: <b>${stock.grand_totals.on_hand}</b> ед.</div>
     <div class="modal-actions"><button class="btn" data-action="close-modal">Закрыть</button></div>`
  );
  await refreshAll();
}

/* ---------------------------------------------------------------- события */

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action], .tab");
  if (!target) return;
  if (target.classList.contains("tab")) {
    state.tab = target.dataset.tab;
    $$(".tab").forEach((t) => t.classList.toggle("active", t === target));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${state.tab}`));
    return;
  }
  const action = target.dataset.action;
  const { id, name, sku, kind, status } = target.dataset;
  try {
    switch (action) {
      case "refresh":
        await refreshAll();
        toast("Данные обновлены");
        break;
      case "close-modal":
        closeModal();
        break;
      case "show-invariants":
        showInvariants();
        break;
      case "where":
        await showWhere(sku);
        break;
      case "add-line":
        addLineRow(target.closest(".card").querySelector("[data-lines]"));
        break;
      case "remove-line": {
        const box = target.closest("[data-lines]");
        target.closest(".line-row").remove();
        if (!box.children.length) addLineRow(box);
        break;
      }
      case "wh-stock":
        await showWarehouseStock(id, name);
        break;
      case "rename-wh":
        showRename(id, name);
        break;
      case "rename-wh-confirm": {
        const value = $("#rename-input").value.trim();
        await api(`/api/warehouses/${id}`, { method: "PATCH", body: { name: value } });
        closeModal();
        toast("Склад переименован");
        await refreshAll();
        break;
      }
      case "archive-wh":
        await confirmArchive(id, name);
        break;
      case "archive-wh-confirm": {
        const res = (await api(`/api/warehouses/${id}`, { method: "DELETE" })).data;
        closeModal();
        toast(res.message);
        await refreshAll();
        break;
      }
      case "restore-wh":
        await api(`/api/warehouses/${id}/restore`, { method: "POST" });
        toast("Склад восстановлен");
        await refreshAll();
        break;
      case "doc-action":
        await runDocumentAction(id, kind, status);
        break;
      case "doc-double":
        await demoDoubleConfirm(id, kind, status);
        break;
      case "shortage-transfer":
        await runPendingTransfer();
        break;
      case "shortage-split":
        await runPendingSplit();
        break;
      case "reset-demo":
        openModal(
          "Сбросить демо-данные?",
          `<p>База будет очищена и заново заполнена тестовыми данными из ТЗ
             (SKU-101/102/103, ФФ «Север» и «Юг», проведённый приход).</p>
           <div class="modal-actions">
             <button class="btn btn-danger" data-action="reset-demo-confirm">Да, сбросить</button>
             <button class="btn" data-action="close-modal">Отмена</button>
           </div>`
        );
        break;
      case "reset-demo-confirm":
        await api("/api/demo/reset", { method: "POST" });
        closeModal();
        toast("Демо-данные пересозданы");
        await refreshAll();
        break;
      default:
        break;
    }
  } catch (err) {
    toast(err.message || String(err), "error", 9000);
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target;
  event.preventDefault();
  try {
    if (form.id === "form-warehouse") {
      const name = form.elements.name.value.trim();
      await api("/api/warehouses", { method: "POST", body: { name } });
      form.reset();
      toast(`Склад «${name}» добавлен`);
      await refreshAll();
      return;
    }
    if (!readLines(form).length) {
      toast("Добавь хотя бы одну строку с артикулом и количеством", "error");
      return;
    }
    if (form.id === "form-receipt") await submitReceipt(form);
    else if (form.id === "form-transfer") await submitTransfer(form);
    else if (form.id === "form-fbs") await submitFbs(form);
    else if (form.id === "form-fbo") await submitFbo(form);
  } catch (err) {
    toast(err.message || String(err), "error", 9000);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModal();
});

$("#modal").addEventListener("click", (event) => {
  if (event.target.id === "modal") closeModal();
});

["#stock-search", "#show-archived"].forEach((sel) =>
  $(sel).addEventListener("change", async () => {
    await loadStock();
    renderStock();
  })
);
let searchTimer = null;
$("#stock-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    await loadStock();
    renderStock();
  }, 200);
});

["#docs-kind", "#docs-status", "#docs-warehouse", "#docs-sku"].forEach((sel) =>
  $(sel).addEventListener("change", async () => {
    await loadDocuments();
    renderDocuments();
  })
);
["#hist-kind", "#hist-warehouse", "#hist-sku", "#hist-limit"].forEach((sel) =>
  $(sel).addEventListener("change", async () => {
    await loadHistory();
    renderHistory();
  })
);

refreshAll();
