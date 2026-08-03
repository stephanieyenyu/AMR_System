let bindingsData = [];
let packagesById = {};

async function withButtonFeedback(button, fn) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = '更新中...';
  try {
    await fn();
  } finally {
    button.textContent = originalText;
    button.disabled = false;
  }
}

async function loadBindings() {
  const resp = await fetch('/admin/bindings');
  bindingsData = await resp.json();
  const units = [...new Set(bindingsData.map(b => b.unit))];
  const unitSelect = document.getElementById('unitSelect');
  unitSelect.innerHTML = '<option value="">請選擇門牌</option>' +
    units.map(u => `<option value="${u}">${u}</option>`).join('');
}

function updateNameOptions() {
  const unit = document.getElementById('unitSelect').value;
  const nameSelect = document.getElementById('nameSelect');
  const names = bindingsData.filter(b => b.unit === unit);
  nameSelect.innerHTML = '<option value="">請選擇收件人</option>' +
    names.map(b => `<option value="${b.name}">${b.name}</option>`).join('');
}

document.getElementById('unitSelect').addEventListener('change', updateNameOptions);

async function createPackage() {
  const unit = document.getElementById('unitSelect').value;
  const recipient_name = document.getElementById('nameSelect').value;
  const quantity = parseInt(document.getElementById('qtySelect').value, 10);
  const msgEl = document.getElementById('createMsg');
  const btn = document.getElementById('createBtn');
  if (!unit) { msgEl.style.color = 'red'; msgEl.textContent = '請先選擇門牌'; return; }
  if (!recipient_name) { msgEl.style.color = 'red'; msgEl.textContent = '請選擇收件人'; return; }

  btn.disabled = true;
  btn.textContent = '建立中...';
  try {
    const resp = await fetch('/packages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ unit, recipient_name: recipient_name || null, quantity }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '建立失敗');

    if (data.notify_failed && data.notify_failed.length > 0) {
      msgEl.style.color = '#b58105';
      msgEl.textContent = `建立成功，已通知 ${data.notified_count} 位住戶，但 ${data.notify_failed.join('、')} 通知失敗（請確認LINE綁定是否正常）`;
    } else {
      msgEl.style.color = 'green';
      msgEl.textContent = `建立成功，已通知 ${data.notified_count} 位住戶`;
    }

    document.getElementById('qtySelect').value = '1';   // ← 成功後重置件數
    loadPackages();
  } catch (e) {
    msgEl.style.color = 'red';
    msgEl.textContent = '錯誤：' + e.message;
    // 失敗不重置件數，維持原本選的值方便重試
  } finally {
    btn.disabled = false;
    btn.textContent = '建立包裹並通知';
  }
}

const STATUS_LABEL = {
  pending: '待處理', pickup_now: '待派送',
  delivering: '配送中', arrived: '已抵達', completed: '已完成',
  returned_timeout: '逾時（作廢）',
  voided: '不收（作廢）', rejected_at_door: '拒收（作廢）',
};

const PACKAGES_PER_PAGE = 50;
let currentPackagePage = 1;
let packagePageTotal = 0;
let activeUnit = '';
let activeDateFrom = '';
let activeDateTo = '';
let selectMode = false;
let selectedPackageIds = new Set();
let latestDoorStates = [];  // 最近一次機器人回報的艙門狀態，供放置包裹的艙門下拉選單使用

async function refreshAll() {
  // 整頁唯一的重新整理鍵：一次刷新機器人狀態、包裹清單/異常提示框、
  // 建立包裹表單的門牌/收件人下拉選單，取代原本分散在各卡片裡各自的重新整理鍵。
  await Promise.all([loadPackages(), loadRobotStatus(), loadBindings()]);
}

async function loadPackages() {
  // 一併刷新機器人狀態：包裹清單每次重新渲染都會重新畫艙門下拉選單，
  // 如果latestDoorStates沒有跟著同步更新，選單會一直用上一次不知道多久前
  // 的舊艙門狀態判斷可不可選，跟實際包裹清單的新舊對不起來。
  await Promise.all([loadLivePackages(), loadPackageTablePage(), loadRobotStatus()]);
}

async function loadLivePackages() {
  let livePackages;
  try {
    const resp = await fetch('/admin/packages/live');
    livePackages = await resp.json();
    if (!resp.ok) throw new Error(livePackages.detail || '未知錯誤');
  } catch (e) {
    document.getElementById('rejectAlert').style.display = 'block';
    document.getElementById('rejectAlert').innerHTML =
      `<b>機器人狀態/待處理清單載入失敗</b><div style="margin-top:6px;">${e.message}</div>`;
    return;
  }
  for (const p of livePackages) {
    packagesById[p.id] = p;
  }
  renderRejectAlert(livePackages);
  renderReturnPendingAlert(livePackages);
  renderDispatchBatchButton(livePackages);
  updateManualDoorButtonState(livePackages);
  updatePendingRequestHint(livePackages);
}

async function loadPackageTablePage() {
  const params = new URLSearchParams({ page: currentPackagePage, page_size: PACKAGES_PER_PAGE });
  if (activeUnit) params.set('unit', activeUnit);
  if (activeDateFrom) params.set('date_from', activeDateFrom);
  if (activeDateTo) params.set('date_to', activeDateTo);

  let resp, data;
  try {
    resp = await fetch(`/admin/packages?${params.toString()}`);
    data = await resp.json();
  } catch (e) {
    // fetch本身失敗，或後端回傳的不是合法JSON（例如500的原始錯誤文字）
    document.getElementById('packageTableBody').innerHTML =
      `<tr><td colspan="${selectMode ? 7 : 6}" style="color:red">載入失敗：${e.message}</td></tr>`;
    return;
  }
  if (!resp.ok) {
    document.getElementById('packageTableBody').innerHTML =
      `<tr><td colspan="${selectMode ? 7 : 6}" style="color:red">載入失敗：${data.detail || '未知錯誤'}</td></tr>`;
    return;
  }
  packagePageTotal = data.total;

  const totalPages = Math.max(1, Math.ceil(packagePageTotal / PACKAGES_PER_PAGE));
  if (currentPackagePage > totalPages) {
    // 頁碼超出範圍（例如原本在看最後一頁,資料變少了),退回正確的最後一頁重新抓
    currentPackagePage = totalPages;
    return loadPackageTablePage();
  }

  for (const p of data.items) {
    packagesById[p.id] = p;
  }
  renderPackageTable(data.items, totalPages);
}

function applyFilters() {
  const unit = document.getElementById('unitFilterInput').value.trim();
  const from = document.getElementById('packageDateFrom').value;
  const to = document.getElementById('packageDateTo').value;
  const infoEl = document.getElementById('filterInfo');

  if (from && to && from > to) {
    alert('起始日期不能晚於結束日期');
    return;
  }

  activeUnit = unit;
  activeDateFrom = from;
  activeDateTo = to;
  currentPackagePage = 1;

  const parts = [];
  if (unit) parts.push(`門牌「${unit}」`);
  if (from || to) parts.push(`${from || '最早'} 至 ${to || '最新'}`);
  infoEl.textContent = parts.length > 0 ? `篩選：${parts.join('，')}` : '';

  loadPackageTablePage();
}

function clearFilters() {
  document.getElementById('unitFilterInput').value = '';
  document.getElementById('packageDateFrom').value = '';
  document.getElementById('packageDateTo').value = '';
  document.getElementById('filterInfo').textContent = '';
  activeUnit = '';
  activeDateFrom = '';
  activeDateTo = '';
  currentPackagePage = 1;
  loadPackageTablePage();
}

function renderPackageTable(pageItems, totalPages) {
  const tbody = document.getElementById('packageTableBody');
  const infoEl = document.getElementById('packagePagerInfo');
  const prevBtn = document.getElementById('packagePrevBtn');
  const nextBtn = document.getElementById('packageNextBtn');

  const colCount = selectMode ? 7 : 6;

  if (packagePageTotal === 0) {
    tbody.innerHTML = `<tr><td colspan="${colCount}">目前沒有包裹</td></tr>`;
    infoEl.textContent = '';
    setPackagePagerLinkState(prevBtn, true);
    setPackagePagerLinkState(nextBtn, true);
    return;
  }

  const now = new Date();

  tbody.innerHTML = pageItems.map(p => {
    const checkboxCell = selectMode
      ? `<td><input type="checkbox" class="pkg-select-checkbox" data-id="${p.id}" ${selectedPackageIds.has(p.id) ? 'checked' : ''} onchange="togglePackageSelect('${p.id}', this.checked)" /></td>`
      : '';
    const label = (p.status === 'rejected_at_door' && p.task_type === 'return')
      ? '已取消退貨'
      : (STATUS_LABEL[p.status] || p.status);
    const createdAt = p.created_at ? p.created_at.replace('T', ' ').slice(0, 16) : '-';
    const door = p.door_id || '尚未分配';

    // 預約時間到了、但還沒放置（沒有door_id）：這是需要管理員動作的時刻，
    // 用醒目的橘色底色+文字提醒，跟一般預約中（時間還沒到，純文字顯示）做區隔
    let scheduledCell = '-';
    let rowStyle = '';
    if (p.scheduled_pickup_at) {
      const scheduledDate = new Date(p.scheduled_pickup_at);
      const scheduledText = p.scheduled_pickup_at.replace('T', ' ').slice(0, 16);
      const timeArrived = scheduledDate <= now;
      if (timeArrived && !p.door_id && p.status === 'pickup_now') {
        scheduledCell = `<span style="background:#ff9800;color:white;padding:2px 8px;border-radius:10px;font-size:14px;font-weight:bold;">預約時間已到 ${scheduledText}</span>`;
        rowStyle = 'background:#fff3e0;';
      } else {
        scheduledCell = scheduledText;
      }
    }

    // 拒收/逾時/不收這幾個狀態的操作按鈕，已經統一在上面的紅色提示框處理了，
    // 這裡不再重複放按鈕，避免同一筆包裹在畫面上出現兩個功能一樣的按鈕。
    // pickup_now分三種：還沒放置（要按「放置包裹」呼叫機器人開門）、已放置
    // （只顯示「釋放」，一定要先釋放才能重新選門——見releaseDoor）、等批次派送。
    // completed：原本要另外開「手動聯繫住戶」視窗才能通知，現在併入這一欄，
    // 已通知過的直接顯示時間、還沒通知的顯示按鈕，不用再選一次門牌/包裹。
    let action;
    if (p.status === 'pickup_now') {
      if (p.door_id) {
        action = `<button class="secondary" onclick="releaseDoor(this, '${p.id}')" title="呼叫機器人釋放這扇艙門，釋放後才能重新選擇">釋放</button>`;
      } else if (p.scheduled_pickup_at && new Date(p.scheduled_pickup_at) > now) {
        action = `<span style="opacity:0.6;">預約中，未到時間</span>`;
      } else {
        action = buildDoorPlacementSelect(p);
      }
    } else if (p.status === 'completed') {
      if (p.pending_pickup_notified_at) {
        const notifiedText = p.pending_pickup_notified_at.replace('T', ' ').slice(0, 16);
        action = `<span style="font-size:14px;color:#888;">已通知（${notifiedText}）</span>`;
      } else {
        action = `<button class="secondary" onclick="notifyLeftover(this, '${p.id}')" title="通知住戶：這筆任務已完成，但懷疑艙門裡還留有包裹沒被拿走">通知住戶</button>`;
      }
    } else {
      action = '-';
    }

    const taskTypeBadge = p.task_type === 'return'
      ? `<span style="background:#004085;color:white;padding:1px 6px;border-radius:8px;font-size:13px;margin-left:4px;">退貨</span>`
      : '';
    const unitCell = (p.package_count > 1
      ? `${p.unit} <span style="background:#e3f2fd;color:#0d47a1;padding:1px 6px;border-radius:8px;font-size:13px;">${p.package_count}件</span>`
      : p.unit) + taskTypeBadge;

    const rowClass = selectMode ? 'selectable-row' : '';
    const rowClick = selectMode ? ` onclick="handleRowClick(event, '${p.id}')"` : '';

    return `<tr class="${rowClass}" style="${rowStyle}"${rowClick}>
      ${checkboxCell}
      <td>${unitCell}</td>
      <td><span class="status-badge status-${p.status}">${label}</span></td>
      <td>${door}</td><td>${createdAt}</td><td>${scheduledCell}</td><td>${action}</td>
    </tr>`;
  }).join('');

  infoEl.textContent = `共 ${packagePageTotal} 筆，第 ${currentPackagePage} / 共 ${totalPages} 頁`;
  setPackagePagerLinkState(prevBtn, currentPackagePage === 1);
  setPackagePagerLinkState(nextBtn, currentPackagePage === totalPages);
}

async function notifyLeftover(btn, packageId) {
  if (!confirm('確定要發送「3天內未聯繫將作廢」的通知給這筆包裹的收件人嗎？')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '發送中...';
  try {
    const resp = await fetch(`/packages/${packageId}/notify-completed-leftover`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '發送失敗');
    alert(`已通知 ${data.notified_count} 位收件人`);
    loadPackages();
  } catch (e) {
    alert('發送失敗：' + e.message);
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function toggleSelectMode() {
  if (selectMode) {
    exitSelectMode();
    loadPackageTablePage();
    return;
  }
  selectMode = true;
  document.getElementById('selectColHeader').style.display = 'table-cell';
  document.getElementById('selectModeBtn').textContent = '取消選取';
  document.getElementById('deleteSelectedBtn').style.display = 'inline-block';
  updateDeleteButtonState();
  loadPackageTablePage();
}

function exitSelectMode() {
  // 只重設選取狀態本身，不在這裡重新抓資料——呼叫端（取消選取按鈕／刪除完成後）
  // 各自決定要不要重抓，避免同一次操作重複打兩次API
  selectMode = false;
  selectedPackageIds.clear();
  document.getElementById('selectColHeader').style.display = 'none';
  document.getElementById('selectModeBtn').textContent = '選取';
  document.getElementById('deleteSelectedBtn').style.display = 'none';
  updateDeleteButtonState();
}

function handleRowClick(event, id) {
  // 點到checkbox、按鈕這些互動元件本身，交給它們各自的onclick/onchange處理，
  // 這裡不要重複觸發，不然點「放置包裹」會變成同時觸發放置又切換選取
  if (event.target.closest('input, button, a')) return;
  const checkbox = document.querySelector(`.pkg-select-checkbox[data-id="${id}"]`);
  if (!checkbox) return;
  checkbox.checked = !checkbox.checked;
  togglePackageSelect(id, checkbox.checked);
}

function togglePackageSelect(id, checked) {
  if (checked) {
    selectedPackageIds.add(id);
  } else {
    selectedPackageIds.delete(id);
  }
  updateDeleteButtonState();
}

function toggleSelectAll(checkbox) {
  document.querySelectorAll('.pkg-select-checkbox').forEach(cb => {
    cb.checked = checkbox.checked;
    if (checkbox.checked) {
      selectedPackageIds.add(cb.dataset.id);
    } else {
      selectedPackageIds.delete(cb.dataset.id);
    }
  });
  updateDeleteButtonState();
}

function updateDeleteButtonState() {
  const btn = document.getElementById('deleteSelectedBtn');
  const count = selectedPackageIds.size;
  btn.textContent = `刪除已選（${count}）`;
  btn.disabled = count === 0;
}

async function deleteSelectedPackages() {
  const ids = Array.from(selectedPackageIds);
  if (ids.length === 0) return;
  if (!confirm(`確定要刪除選取的 ${ids.length} 筆包裹紀錄嗎？此動作會直接從資料庫移除，無法復原。`)) return;

  const btn = document.getElementById('deleteSelectedBtn');
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '刪除中...';
  try {
    const resp = await fetch('/admin/packages/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ package_ids: ids }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '刪除失敗');

    if (data.skipped && data.skipped.length > 0) {
      const reasons = data.skipped.map(s => `${s.id.slice(0, 8)}...：${s.reason}`).join('\n');
      alert(`已刪除 ${data.deleted.length} 筆，${data.skipped.length} 筆無法刪除：\n${reasons}`);
    } else {
      alert(`已刪除 ${data.deleted.length} 筆包裹紀錄`);
    }
    selectedPackageIds.clear();
    exitSelectMode();
    loadPackages();
  } catch (e) {
    alert('刪除失敗：' + e.message);
  } finally {
    updateDeleteButtonState();
  }
}

function setPackagePagerLinkState(el, disabled) {
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

function prevPackagePage() {
  if (currentPackagePage > 1) {
    currentPackagePage -= 1;
    loadPackageTablePage();
  }
}

function nextPackagePage() {
  const totalPages = Math.max(1, Math.ceil(packagePageTotal / PACKAGES_PER_PAGE));
  if (currentPackagePage < totalPages) {
    currentPackagePage += 1;
    loadPackageTablePage();
  }
}

function renderDispatchBatchButton(packages) {
  const btn = document.getElementById('dispatchBatchBtn');
  const readyCount = packages.filter(p => p.status === 'pickup_now' && p.door_id).length;
  btn.innerHTML = `全部派送（<span id="pendingDispatchCount">${readyCount}</span>）`;
  btn.disabled = readyCount === 0;
}

function updatePendingRequestHint(packages) {
  // 常態提示：住戶按了取貨之後，不管管理員有沒有放置包裹、有沒有按全部派送，
  // 只要狀態還是pickup_now，就代表這個請求還沒真正被送出去，提醒管理員盡速處理。
  // 跟「全部派送(N)」按鈕的計數不同——那個只算「已經放置、等派送」的，
  // 這裡算全部還卡著的請求（含還沒放置的）。
  // 一律顯示，沒有待處理的也顯示「0件」，不隱藏這個提示。
  const hintEl = document.getElementById('pendingRequestHint');
  const pendingCount = packages.filter(p => p.status === 'pickup_now').length;
  hintEl.textContent = `目前有 ${pendingCount} 筆任務尚未處理派送，請盡速處理`;
  hintEl.style.display = 'inline';
}

function updateManualDoorButtonState(packages) {
  // 機器人狀態欄的開/關門鍵：平常白色(secondary)，只要有拒收/逾時退回、
  // 或退貨已放貨完成但還沒確認取出的包裹，就自動變紅色(danger)提醒管理員該去操作了。
  const openBtn = document.getElementById('manualOpenBtn');
  const closeBtn = document.getElementById('manualCloseBtn');
  if (!openBtn || !closeBtn) return;

  const returnPackages = packages.filter(p =>
    (p.status === 'rejected_at_door' || p.status === 'returned_timeout') && !p.door_closed_at
  );
  const pendingReturnItems = packages.filter(p =>
    p.task_type === 'return' && p.status === 'completed' && !p.return_retrieved_at
  );
  const needsOpen = returnPackages.some(p => p.returned_at && !p.return_door_opened_at) || pendingReturnItems.length > 0;
  const needsClose = returnPackages.some(p => p.return_door_opened_at && !p.door_closed_at) || pendingReturnItems.length > 0;

  openBtn.classList.toggle('danger', needsOpen);
  openBtn.classList.toggle('secondary', !needsOpen);
  closeBtn.classList.toggle('danger', needsClose);
  closeBtn.classList.toggle('secondary', !needsClose);
}

function renderRejectAlert(packages) {
  const alertEl = document.getElementById('rejectAlert');
  // 拒收/逾時退回（機器人已送回，等關門）+ 不收/作廢（不需要機器人動作，等管理員確認知悉）
  const pending = packages.filter(p =>
    ((p.status === 'rejected_at_door' || p.status === 'returned_timeout') && !p.door_closed_at)
    || (p.status === 'voided' && !p.acknowledged_at)
  );

  if (pending.length === 0) {
    alertEl.style.display = 'none';
    alertEl.innerHTML = '';
    return;
  }

  const reasonLabel = { rejected_at_door: '拒收', returned_timeout: '逾時未取', voided: '不收（作廢）' };
  const btnStyle = 'background:white;color:#dc3545;border:none;padding:6px 14px;border-radius:6px;font-size:15px;cursor:pointer;';

  // 拒收/逾時退回的開門/關門已經統一移到上面「機器人狀態」欄位的按鈕處理
  // （那邊的按鈕平常白色、有包裹在等待時會自動變紅色提醒），這裡不再重複放
  // 開關門按鈕，只顯示目前卡在哪個階段的狀態文字，並依狀態提示該按哪一顆。
  const returnPending = pending.filter(p => p.status !== 'voided');
  const anyWaitingOpen = returnPending.some(p => p.returned_at && !p.return_door_opened_at);
  const anyWaitingClose = returnPending.some(p => p.return_door_opened_at && !p.door_closed_at);
  let batchActionHtml = '';
  if (anyWaitingOpen) {
    batchActionHtml = `<span style="font-size:15px;opacity:0.9;">請至上方「機器人狀態」欄位按「檢查艙門」開門 ↑</span>`;
  } else if (anyWaitingClose) {
    batchActionHtml = `<span style="font-size:15px;opacity:0.9;">艙門已開啟，請確認清空後至上方「機器人狀態」欄位按「確認關門」↑</span>`;
  } else if (returnPending.length > 0) {
    batchActionHtml = `<span style="font-size:15px;opacity:0.9;">等待機器人返回</span>`;
  }

  alertEl.style.display = 'block';
  alertEl.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:10px;">
      <b>有 ${pending.length} 筆包裹需要處理，請確認</b>
      ${batchActionHtml}
    </div>
    <table>
      <thead><tr><th>門牌</th><th>艙門</th><th>原因</th><th>狀態</th><th></th></tr></thead>
      <tbody>
        ${pending.map(p => {
          let statusText, forceResolveHtml = '';
          if (p.status === 'voided') {
            statusText = `<button style="${btnStyle}" onclick="acknowledgeVoid(this, '${p.id}')">確定</button>`;
          } else if (!p.returned_at) {
            statusText = `<span style="opacity:0.8;">等待機器人返回</span>`;
            forceResolveHtml = `<a href="javascript:void(0)" onclick="forceResolve(this, '${p.id}')" style="font-size:14px;color:white;text-decoration:underline;cursor:pointer;">手動結案</a>`;
          } else if (!p.return_door_opened_at) {
            statusText = `<span style="opacity:0.8;">待開門</span>`;
            forceResolveHtml = `<a href="javascript:void(0)" onclick="forceResolve(this, '${p.id}')" style="font-size:14px;color:white;text-decoration:underline;cursor:pointer;">手動結案</a>`;
          } else {
            statusText = `<span style="opacity:0.8;">待關門</span>`;
            forceResolveHtml = `<a href="javascript:void(0)" onclick="forceResolve(this, '${p.id}')" style="font-size:14px;color:white;text-decoration:underline;cursor:pointer;">手動結案</a>`;
          }
          return `<tr>
          <td>${p.unit}</td>
          <td>${p.door_id || '-'}</td>
          <td>${(p.status === 'rejected_at_door' && p.task_type === 'return') ? '已取消退貨' : (reasonLabel[p.status] || p.status)}</td>
          <td>${statusText}</td>
          <td style="text-align:right;">${forceResolveHtml}</td>
        </tr>`;
        }).join('')}
      </tbody>
    </table>
  `;
}

function renderReturnPendingAlert(packages) {
  // 退貨任務：住戶已經放貨完成（status=completed），機器人應該已經把包裹
  // 帶回管理室，但管理員還沒按「確認取出」——這裡主動提醒，不是等管理員
  // 自己想到要去查包裹清單。跟送貨任務的completed不同，退貨的completed
  // 不代表「這件事結束了」，是代表「輪到管理員動作了」。
  const alertEl = document.getElementById('returnPendingAlert');
  const pending = packages.filter(p => p.task_type === 'return' && p.status === 'completed' && !p.return_retrieved_at);

  if (pending.length === 0) {
    alertEl.style.display = 'none';
    alertEl.innerHTML = '';
    return;
  }

  const btnStyle = 'background:white;color:#004085;border:none;padding:6px 14px;border-radius:6px;font-size:15px;cursor:pointer;';

  alertEl.style.display = 'block';
  alertEl.innerHTML = `
    <b>有 ${pending.length} 筆退貨件已送達管理室，請盡速取出</b>
    <table>
      <thead><tr><th>門牌</th><th>艙門</th><th>建立時間</th><th></th></tr></thead>
      <tbody>
        ${pending.map(p => {
          const createdAt = p.created_at ? p.created_at.replace('T', ' ').slice(0, 16) : '-';
          return `<tr>
          <td>${p.unit}</td>
          <td>${p.door_id || '-'}</td>
          <td>${createdAt}</td>
          <td style="text-align:right;"><button style="${btnStyle}" onclick="confirmReturnRetrieved(this, '${p.id}')">確認取出</button></td>
        </tr>`;
        }).join('')}
      </tbody>
    </table>
    <div style="font-size:14px;opacity:0.85;margin-top:6px;">請先用上方「機器人狀態」欄位的「開啟艙門／關閉艙門」實際取出物品，再按這裡的「確認取出」結案</div>
  `;
}

async function confirmReturnRetrieved(btn, packageId) {
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '處理中...';
  try {
    const resp = await fetch(`/packages/${packageId}/confirm-return-retrieved`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '確認失敗');
    loadPackages();
  } catch (e) {
    alert('確認失敗：' + e.message);
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  ['unitFilterInput', 'packageDateFrom', 'packageDateTo'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') applyFilters();
      });
    }
  });
});

async function placePackage(selectEl, packageId) {
  const doorId = selectEl.value;
  if (!doorId) return;
  selectEl.disabled = true;
  try {
    const resp = await fetch(`/packages/${packageId}/place`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ door_id: doorId }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '放置失敗');
    loadPackages();
  } catch (e) {
    alert('放置失敗：' + e.message);
    selectEl.disabled = false;
    selectEl.value = '';
  }
}

function buildDoorPlacementSelect(p) {
  // 用packagesById（loadLivePackages抓到的所有進行中包裹）算出這一輪裡
  // 每扇門目前被哪個收件人（line_user_id）佔用——door_task_id有值代表
  // 這一輪已經開過，不是舊資料殘留。
  const doorOccupants = {};
  for (const key in packagesById) {
    const other = packagesById[key];
    if (other.status === 'pickup_now' && other.door_id && other.door_task_id && other.id !== p.id) {
      doorOccupants[other.door_id] = other.line_user_id;
    }
  }

  const doorIds = latestDoorStates.length > 0
    ? latestDoorStates.map(d => d.door_number)
    : ['H_01', 'H_02', 'H_03', 'H_04']; // 機器人狀態還沒載入完成時的保底清單

  const options = doorIds.map(doorId => {
    const physical = latestDoorStates.find(d => d.door_number === doorId);
    const occupantLineUserId = doorOccupants[doorId];

    if (occupantLineUserId) {
      if (occupantLineUserId === p.line_user_id) {
        return `<option value="${doorId}">${doorId}（加入本人已開啟）</option>`;
      }
      return `<option value="${doorId}" disabled>${doorId}（其他收件人使用中）</option>`;
    }
    if (physical && (physical.status || '').toUpperCase() !== 'EMPTY') {
      return `<option value="${doorId}" disabled>${doorId}（艙門非空）</option>`;
    }
    return `<option value="${doorId}">${doorId}</option>`;
  }).join('');

  return `<select onchange="placePackage(this, '${p.id}')" style="max-width:190px;">
    <option value="" selected disabled>請選擇艙門</option>
    ${options}
  </select>`;
}

async function releaseDoor(btn, packageId) {
  if (!confirm('確定要釋放這扇艙門嗎？機器人會關閉這扇門並將狀態改回空的，釋放後才能重新選擇艙門。')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '釋放中...';
  try {
    const resp = await fetch(`/packages/${packageId}/release-door`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '釋放失敗');
    loadPackages();
  } catch (e) {
    alert('釋放失敗：' + e.message);
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function dispatchBatch() {
  const btn = document.getElementById('dispatchBatchBtn');
  btn.disabled = true;
  const originalText = btn.innerHTML;
  btn.textContent = '派送中...';
  try {
    const resp = await fetch('/admin/dispatch-batch', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '派送失敗');
    alert(`已派送 ${data.dispatched_count} 筆（共 ${data.total_quantity} 件）包裹`);
    loadPackages();
  } catch (e) {
    alert('派送失敗：' + e.message);
    btn.innerHTML = originalText;
    btn.disabled = false;
  }
}

async function acknowledgeVoid(btn, packageId) {
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '確認中...';
  try {
    const resp = await fetch(`/packages/${packageId}/acknowledge`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '確認失敗');
    loadPackages();
  } catch (e) {
    alert('確認失敗：' + e.message);
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function forceResolve(el, packageId) {
  if (!confirm('確定要手動結案嗎？這不會呼叫機器人，只適用於你已經自己手動處理過機器人艙門實體狀態的情況，操作後這筆包裹會直接從提示框消失。')) return;
  const originalText = el.textContent;
  el.textContent = '處理中...';
  el.style.pointerEvents = 'none';
  try {
    const resp = await fetch(`/packages/${packageId}/force-resolve`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '手動結案失敗');
    loadPackages();
  } catch (e) {
    alert('手動結案失敗：' + e.message);
    el.textContent = originalText;
    el.style.pointerEvents = 'auto';
  }
}

async function manualOpenDoors(btn) {
  if (!confirm('將打開機器人上所有艙門，建議機器人每次返回時都執行檢查。')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '開門中...';
  try {
    const resp = await fetch('/admin/doors/manual-open', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '開門失敗');
    loadPackages();
  } catch (e) {
    alert('開門失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function manualCloseDoors(btn) {
  if (!confirm('請確認所有艙門都已清空再按確認鍵關門。')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '關門中...';
  try {
    const resp = await fetch('/admin/doors/manual-close', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '關門失敗');
    loadPackages();
  } catch (e) {
    alert('關門失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function robotRecall(btn) {
  if (!confirm('叫回機器人會強制中斷機器人正在執行的任何動作，進行中的包裹任務將重置為待派送。')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '叫回中...';
  try {
    const resp = await fetch('/admin/robot/recall', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '叫回失敗');
    if (data.reset_count > 0) {
      alert(`機器人已叫回，${data.reset_count} 筆進行中的任務已重置為待派送，請於機器人回到管理室後開門確認艙門內容`);
    }
    loadRobotStatus();
    loadPackages();
  } catch (e) {
    alert('叫回失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function robotRecharge(btn) {
  if (!confirm('確定要叫機器人回充電站嗎？')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '呼叫中...';
  try {
    const resp = await fetch('/admin/robot/recharge', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '呼叫失敗');
    alert('已通知機器人回充電站');
  } catch (e) {
    alert('呼叫失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function loadRobotStatus() {
  const infoEl = document.getElementById('robotInfo');
  const doorEl = document.getElementById('doorInfo');
  try {
    const resp = await fetch('/admin/robot-status');
    const data = await resp.json();
    if (data.status === 'error') {
      infoEl.innerHTML = `<span style="color:red">${data.detail}</span>`;
      doorEl.innerHTML = '';
      return;
    }
    const payload = data.data;
    const robot = payload.robot_status;
    const doors = payload.door_states;
    latestDoorStates = doors || [];

    // 機器人API目前把真正即時的數值塞在 robot_status.sources.v1/v2.data 裡，
    // 外層的 battery_level / current_location 是機器人那邊還沒同步更新的舊欄位，
    // state則是掛在data這層，不在robot_status裡面。
    // 這裡照實際結構讀，並保留舊路徑當fallback，以後機器人那邊修好了也不用再改。
    const src = (robot.sources && (robot.sources.v1 || robot.sources.v2))
      ? (robot.sources.v1 || robot.sources.v2).data
      : null;

    const state = payload.state || robot.state || robot.move_state || '未知';
    const battery = src?.battery ?? robot.battery ?? robot.battery_level ?? null;
    const mapName = src?.map_name ? src.map_name.replace(/^\d+#\d+#/, '') : null;
    const location = robot.current_location
      || mapName
      || (src?.position ? `(${src.position.x.toFixed(1)}, ${src.position.y.toFixed(1)})` : null);

    infoEl.innerHTML = `
      <div><b>狀態</b>${state}</div>
      <div><b>目前位置</b>${location || '未知'}</div>
      <div><b>電量</b>${battery !== null ? battery + '%' : '未知'}</div>`;

    // 開關艙門只有機器人真的在管理室時才能操作，後端也有同樣的檢查（雙重保險），
    // 這裡先在前端把按鈕disable掉，避免管理員按了才被拒絕
    const atOffice = location === ROBOT_HOME_POINT_NAME;
    const openBtn = document.getElementById('manualOpenBtn');
    const closeBtn = document.getElementById('manualCloseBtn');
    if (openBtn) {
      openBtn.disabled = !atOffice;
      openBtn.title = atOffice ? '打開所有艙門' : `機器人目前不在管理室（${location || '未知'}），無法開關艙門`;
    }
    if (closeBtn) {
      closeBtn.disabled = !atOffice;
      closeBtn.title = atOffice ? '請確認所有艙門皆空再關閉艙門' : `機器人目前不在管理室（${location || '未知'}），無法開關艙門`;
    }

    doorEl.innerHTML = doors.map(d => {
      const pkg = d.package_id ? packagesById[d.package_id] : null;
      // 正常情況顯示門牌；如果packagesById還沒抓到對應資料（例如剛載入頁面時兩個API還沒都回來），
      // 退回顯示package_id前8碼，之後下一次自動更新就會補正確
      const label = pkg ? pkg.unit : (d.package_id ? d.package_id.slice(0, 8) + '...' : '');
      return `
      <div class="door-box door-${(d.status || '').toUpperCase()}">
        <div>${d.door_number}</div><div>${d.status}</div>
        ${label ? `<div style="font-size:13px">${label}</div>` : ''}
      </div>`;
    }).join('');
  } catch (e) {
    infoEl.innerHTML = `<span style="color:red">無法載入：${e.message}</span>`;
  }
}

loadBindings();
loadPackages();
loadRobotStatus();
setInterval(loadPackages, 10000);
setInterval(loadRobotStatus, 10000);
