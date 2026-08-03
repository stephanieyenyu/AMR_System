const STATUS_LABEL = {
  voided: '不收（作廢）', rejected_at_door: '拒收', returned_timeout: '逾時未取',
};

let allExceptions = [];
let selectMode = false;
let selectedPackageIds = new Set();

async function loadExceptions() {
  const tbody = document.getElementById('exceptionsTableBody');
  try {
    const resp = await fetch('/admin/packages/exceptions');
    allExceptions = await resp.json();
    renderExceptions(allExceptions);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="${selectMode ? 8 : 7}" style="color:red">載入失敗：${e.message}</td></tr>`;
  }
}

function renderExceptions(packages) {
  const tbody = document.getElementById('exceptionsTableBody');
  const keyword = document.getElementById('unitFilterInput').value.trim();
  const colCount = selectMode ? 8 : 7;

  if (packages.length === 0) {
    tbody.innerHTML = `<tr><td colspan="${colCount}" class="empty-hint">${keyword ? '找不到符合的門牌' : '目前沒有退回/作廢的包裹'}</td></tr>`;
    return;
  }
  tbody.innerHTML = packages.map(p => {
    const checkboxCell = selectMode
      ? `<td><input type="checkbox" class="pkg-select-checkbox" data-id="${p.id}" ${selectedPackageIds.has(p.id) ? 'checked' : ''} onchange="togglePackageSelect('${p.id}', this.checked)" /></td>`
      : '';
    const label = (p.status === 'rejected_at_door' && p.task_type === 'return')
      ? '已取消退貨'
      : (STATUS_LABEL[p.status] || p.status);
    const createdAt = p.created_at ? p.created_at.replace('T', ' ').slice(0, 16) : '-';
    const recipients = p.recipients.map(r => r.name).join('、') || '-';

    const notifiedCell = p.pending_pickup_notified_at
      ? p.pending_pickup_notified_at.replace('T', ' ').slice(0, 16)
      : '-';
    const notifyButton = p.pending_pickup_notified_at
      ? ''
      : `<button class="secondary" onclick="notifyPendingPickup(this, '${p.id}')">通知住戶</button>`;

    let resolvedPill, action;
    if (p.redispatched_at) {
      resolvedPill = '<span class="pill pill-redispatched">已重新派送</span>';
      action = `新包裹 ${p.redispatched_to.slice(0, 8)}...`;
    } else if (!p.resolved) {
      resolvedPill = '<span class="pill pill-waiting">尚未處理</span>';
      action = `<span class="action-buttons">
        <button disabled title="請先在主畫面確認/關門">重新派貨</button>
        ${notifyButton || '<span style="font-size:12px;color:#888;">已通知</span>'}
      </span>`;
    } else {
      resolvedPill = '<span class="pill pill-resolved">已處理</span>';
      action = `<span class="action-buttons">
        <button onclick="redispatch(this, '${p.id}')">重新派貨</button>
        ${notifyButton}
      </span>`;
    }

    const rowClass = (selectMode ? 'selectable-row ' : '') + (p.needs_attention ? 'needs-attention-row' : '');
    const rowClick = selectMode ? ` onclick="handleRowClick(event, '${p.id}')"` : '';
    const attentionBadge = p.needs_attention
      ? '<div style="color:#dc3545;font-size:11px;font-weight:bold;margin-top:2px;">⚠️ 已超過72小時未處理</div>'
      : '';

    return `<tr class="${rowClass}"${rowClick}>
      ${checkboxCell}
      <td>${p.unit}</td>
      <td>${recipients}</td>
      <td><span class="status-badge status-${p.status}">${label}</span></td>
      <td>${createdAt}</td>
      <td>${resolvedPill}${attentionBadge}</td>
      <td>${action}</td>
      <td>${notifiedCell}</td>
    </tr>`;
  }).join('');
}

function toggleSelectMode() {
  if (selectMode) {
    exitSelectMode();
    renderExceptions(allExceptions);
    return;
  }
  selectMode = true;
  document.getElementById('selectColHeader').style.display = 'table-cell';
  document.getElementById('selectModeBtn').textContent = '取消選取';
  document.getElementById('closeSelectedBtn').style.display = 'inline-block';
  updateCloseSelectedButtonState();
  renderExceptions(allExceptions);
}

function exitSelectMode() {
  selectMode = false;
  selectedPackageIds.clear();
  document.getElementById('selectColHeader').style.display = 'none';
  document.getElementById('selectModeBtn').textContent = '選取';
  document.getElementById('closeSelectedBtn').style.display = 'none';
  updateCloseSelectedButtonState();
}

function handleRowClick(event, id) {
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
  updateCloseSelectedButtonState();
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
  updateCloseSelectedButtonState();
}

function updateCloseSelectedButtonState() {
  const btn = document.getElementById('closeSelectedBtn');
  const count = selectedPackageIds.size;
  btn.textContent = `全部銷案（${count}）`;
  btn.disabled = count === 0;
}

async function closeSelectedCases() {
  const ids = Array.from(selectedPackageIds);
  if (ids.length === 0) return;
  if (!confirm(`確定要將選取的 ${ids.length} 筆包裹全部銷案嗎？銷案後這些紀錄會從此頁面移除，主畫面資料不受影響，且無法復原。`)) return;

  const btn = document.getElementById('closeSelectedBtn');
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '銷案中...';
  try {
    const resp = await fetch('/admin/packages/close-case-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ package_ids: ids }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '銷案失敗');

    if (data.skipped && data.skipped.length > 0) {
      const reasons = data.skipped.map(s => `${s.id.slice(0, 8)}...：${s.reason}`).join('\n');
      alert(`已銷案 ${data.closed.length} 筆，${data.skipped.length} 筆無法銷案：\n${reasons}`);
    } else {
      alert(`已銷案 ${data.closed.length} 筆`);
    }
    exitSelectMode();
    loadExceptions();
  } catch (e) {
    alert('銷案失敗：' + e.message);
  } finally {
    updateCloseSelectedButtonState();
  }
}

function filterByUnit() {
  const keyword = document.getElementById('unitFilterInput').value.trim().toLowerCase();
  const countEl = document.getElementById('unitFilterCount');
  if (!keyword) {
    countEl.textContent = '';
    renderExceptions(allExceptions);
    return;
  }
  const filtered = allExceptions.filter(p => p.unit.toLowerCase().includes(keyword));
  countEl.textContent = `符合「${keyword}」共 ${filtered.length} 筆`;
  renderExceptions(filtered);
}

function clearUnitFilter() {
  document.getElementById('unitFilterInput').value = '';
  document.getElementById('unitFilterCount').textContent = '';
  renderExceptions(allExceptions);
}

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('unitFilterInput');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') filterByUnit();
    });
  }
});

async function notifyPendingPickup(btn, packageId) {
  if (!confirm('確定要補發包裹通知給住戶嗎？（只能通知一次，請確認後再送出）')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '通知中...';
  try {
    const resp = await fetch(`/packages/${packageId}/notify-pending-pickup`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '通知失敗');
    if (data.notify_failed_count > 0) {
      alert(`已通知 ${data.notified_count} 位收件人，${data.notify_failed_count} 位通知失敗`);
    } else {
      alert(`已通知 ${data.notified_count} 位收件人`);
    }
    loadExceptions();
  } catch (e) {
    alert('通知失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function redispatch(btn, packageId) {
  if (!confirm('確定要重新派送這筆包裹嗎？將建立一筆新包裹並重新通知住戶。')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '派送中...';
  try {
    const resp = await fetch(`/packages/${packageId}/redispatch`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '重新派送失敗');
    if (data.notify_failed && data.notify_failed.length > 0) {
      alert(`已建立新包裹，但 ${data.notify_failed.join('、')} 通知失敗，請確認LINE綁定`);
    } else {
      alert('已建立新包裹並通知住戶');
    }
    loadExceptions();
  } catch (e) {
    alert('重新派送失敗：' + e.message);
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

loadExceptions();
