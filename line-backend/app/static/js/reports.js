// 包裹狀態的中文說法，與後端 STATUS_LABELS 一致
const STATUS_TEXT = {
  pending: '待住戶回應',
  pickup_now: '待放置',
  delivering: '配送中',
  arrived: '已抵達門牌',
  completed: '已完成',
  rejected_at_door: '住戶當面拒收',
  returned_timeout: '逾時未取',
  voided: '已作廢',
};
const statusText = s => STATUS_TEXT[s] || s;

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// 預設帶入今天日期，方便直接查詢
const today = new Date();
const yyyy = today.getFullYear();
const mm = String(today.getMonth() + 1).padStart(2, '0');
const dd = String(today.getDate()).padStart(2, '0');
document.getElementById('reportDate').value = `${yyyy}-${mm}-${dd}`;

let logGroups = [];       // [{ packageId, logs }]，每個元素是一個包裹的所有紀錄
let currentGroupIndex = 0;
let packagesById = {};

async function queryReport() {
  const btn = document.getElementById('queryBtn');
  const date = document.getElementById('reportDate').value;
  if (!date) { alert('請先選擇日期'); return; }

  btn.disabled = true;
  btn.textContent = '查詢中...';
  try {
    const resp = await fetch(`/admin/reports/daily?date=${date}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '查詢失敗');

    renderSummary(data.package_status_summary, data.package_count);

    packagesById = Object.fromEntries((data.packages || []).map(p => [p.id, p]));
    logGroups = groupLogsByPackage(data.task_logs);
    currentGroupIndex = logGroups.length > 0 ? logGroups.length - 1 : 0;
    renderLogGroup();
  } catch (e) {
    alert('查詢失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '查詢';
  }
}

function renderSummary(summary, total) {
  const el = document.getElementById('summaryGrid');
  const keys = Object.keys(summary || {});
  if (keys.length === 0) {
    el.innerHTML = '<div class="empty-hint">這天沒有包裹狀態異動紀錄</div>';
    return;
  }
  el.innerHTML = `
    <div class="summary-box"><b>${total}</b><span>當日異動總數</span></div>
    ${keys.map(k => `<div class="summary-box"><b>${summary[k]}</b><span>${statusText(k)}</span></div>`).join('')}
  `;
}

function groupLogsByPackage(logs) {
  // 依package_id分組，保留原本的時間順序（第一次出現該package_id的順序）；
  // package_id是null的紀錄（例如沒有對應特定包裹的系統事件）另外歸成一組
  const order = [];
  const map = {};
  (logs || []).forEach(log => {
    const key = log.package_id || '__no_package__';
    if (!map[key]) {
      map[key] = { packageId: log.package_id, logs: [] };
      order.push(key);
    }
    map[key].logs.push(log);
  });
  return order.map(key => map[key]);
}

function setPagerLinkState(el, disabled) {
  if (disabled) {
    el.style.color = '#ccc';
    el.style.pointerEvents = 'none';
    el.style.cursor = 'default';
  } else {
    el.style.color = '#444';
    el.style.pointerEvents = 'auto';
    el.style.cursor = 'pointer';
  }
}

function renderLogGroup() {
  const tbody = document.getElementById('logTableBody');
  const infoEl = document.getElementById('logPagerInfo');
  const countEl = document.getElementById('logPagerCount');
  const prevBtn = document.getElementById('logPrevBtn');
  const nextBtn = document.getElementById('logNextBtn');

  if (logGroups.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-hint">這天沒有任務紀錄</td></tr>';
    infoEl.textContent = '';
    countEl.textContent = '';
    setPagerLinkState(prevBtn, true);
    setPagerLinkState(nextBtn, true);
    return;
  }

  const group = logGroups[currentGroupIndex];
  const pkg = group.packageId ? packagesById[group.packageId] : null;

  if (group.packageId) {
    infoEl.textContent = pkg
      ? `門牌：${pkg.unit}　狀態：${statusText(pkg.status)}　包裹ID：${group.packageId}`
      : `包裹ID：${group.packageId}（非當天建立/更新的包裹，門牌資訊未顯示）`;
  } else {
    infoEl.textContent = '系統事件（無對應特定包裹）';
  }

  countEl.textContent = `第 ${currentGroupIndex + 1} / 共 ${logGroups.length} 筆包裹`;
  setPagerLinkState(prevBtn, currentGroupIndex === 0);
  setPagerLinkState(nextBtn, currentGroupIndex === logGroups.length - 1);

  tbody.innerHTML = group.logs.map(log => {
    const time = log.created_at ? log.created_at.replace('T', ' ').slice(0, 19) : '-';
    // label/note 由後端提供；舊資料或未定義的事件退回顯示原始代碼，不會空白
    const label = escapeHtml(log.label || log.event_type);
    const note = log.note ? `<div class="log-note">${escapeHtml(log.note)}</div>` : '';
    // detail 是給工程排查用的原始訊息，預設收合
    const detail = log.detail
      ? `<details class="log-detail"><summary>技術細節</summary><code>${escapeHtml(log.detail)}</code></details>`
      : '';
    const flag = log.needs_action ? '<span class="log-flag">需處理</span>' : '';
    return `
    <tr class="${log.needs_action ? 'row-action' : ''}">
      <td>${time}</td>
      <td class="level-${log.level}">${log.level}</td>
      <td><b>${label}</b>${flag}</td>
      <td>${note}${detail}</td>
    </tr>`;
  }).join('');
}

function prevLogGroup() {
  if (currentGroupIndex > 0) {
    currentGroupIndex -= 1;
    renderLogGroup();
  }
}

function nextLogGroup() {
  if (currentGroupIndex < logGroups.length - 1) {
    currentGroupIndex += 1;
    renderLogGroup();
  }
}

queryReport();
