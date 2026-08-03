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
    ${keys.map(k => `<div class="summary-box"><b>${summary[k]}</b><span>${k}</span></div>`).join('')}
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
    el.style.color = '#E2231A';
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
      ? `門牌：${pkg.unit}　狀態：${pkg.status}　包裹ID：${group.packageId}`
      : `包裹ID：${group.packageId}（非當天建立/更新的包裹，門牌資訊未顯示）`;
  } else {
    infoEl.textContent = '系統事件（無對應特定包裹）';
  }

  countEl.textContent = `第 ${currentGroupIndex + 1} / 共 ${logGroups.length} 筆包裹`;
  setPagerLinkState(prevBtn, currentGroupIndex === 0);
  setPagerLinkState(nextBtn, currentGroupIndex === logGroups.length - 1);

  tbody.innerHTML = group.logs.map(log => `
    <tr>
      <td>${log.created_at ? log.created_at.replace('T', ' ').slice(0, 19) : '-'}</td>
      <td class="level-${log.level}">${log.level}</td>
      <td>${log.event_type}</td>
      <td>${log.detail || ''}</td>
    </tr>
  `).join('');
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
