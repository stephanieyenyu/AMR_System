let allBindings = [];

async function loadBindings() {
  const tbody = document.getElementById('bindingsTableBody');
  try {
    const resp = await fetch('/admin/line-bindings');
    allBindings = await resp.json();
    renderBindings(allBindings);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" style="color:red">載入失敗：${e.message}</td></tr>`;
  }
}

function renderBindings(bindings) {
  const tbody = document.getElementById('bindingsTableBody');
  const keyword = document.getElementById('unitFilterInput').value.trim();

  if (bindings.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-hint">${keyword ? '找不到符合的門牌' : '目前沒有任何綁定紀錄'}</td></tr>`;
    return;
  }

  tbody.innerHTML = bindings.map(b => {
    const statusLabel = b.status === 'active' ? '生效中' : '已停用';
    const boundAt = b.bound_at ? b.bound_at.replace('T', ' ').slice(0, 16) : '-';
    return `<tr>
      <td>${b.unit}</td>
      <td>${b.name}</td>
      <td><span class="status-badge status-${b.status}">${statusLabel}</span></td>
      <td>${boundAt}</td>
      <td style="text-align:right;">
        <button class="secondary" onclick="openEditBindingModal('${b.line_user_id}', '${b.unit}', '${b.name}')">修改</button>
        <button onclick="deleteBinding(this, '${b.line_user_id}', '${b.unit}', '${b.name}')">刪除</button>
      </td>
    </tr>`;
  }).join('');
}

function filterByUnit() {
  const keyword = document.getElementById('unitFilterInput').value.trim().toLowerCase();
  const countEl = document.getElementById('unitFilterCount');
  if (!keyword) {
    countEl.textContent = '';
    renderBindings(allBindings);
    return;
  }
  const filtered = allBindings.filter(b => b.unit.toLowerCase().includes(keyword));
  countEl.textContent = `符合「${keyword}」共 ${filtered.length} 筆`;
  renderBindings(filtered);
}

function clearUnitFilter() {
  document.getElementById('unitFilterInput').value = '';
  document.getElementById('unitFilterCount').textContent = '';
  renderBindings(allBindings);
}

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('unitFilterInput');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') filterByUnit();
    });
  }
});

function openEditBindingModal(lineUserId, unit, name) {
  document.getElementById('editBindingOverlay').style.display = 'flex';
  document.getElementById('editBindingLineUserId').value = lineUserId;
  document.getElementById('editBindingUnitInput').value = unit;
  document.getElementById('editBindingNameInput').value = name;
  document.getElementById('editBindingMsg').textContent = '';
}

function closeEditBindingModal() {
  document.getElementById('editBindingOverlay').style.display = 'none';
}

async function saveEditBinding() {
  const lineUserId = document.getElementById('editBindingLineUserId').value;
  const unit = document.getElementById('editBindingUnitInput').value.trim();
  const name = document.getElementById('editBindingNameInput').value.trim();
  const msgEl = document.getElementById('editBindingMsg');

  if (!unit || !name) {
    msgEl.style.color = 'red';
    msgEl.textContent = '門牌與姓名都不能是空的';
    return;
  }

  const btn = document.getElementById('editBindingSaveBtn');
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '儲存中...';
  try {
    const resp = await fetch(`/admin/line-bindings/${lineUserId}/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ unit, name }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '修改失敗');
    loadBindings();
    closeEditBindingModal();
  } catch (e) {
    msgEl.style.color = 'red';
    msgEl.textContent = '修改失敗：' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function deleteBinding(btn, lineUserId, unit, name) {
  if (!confirm(`確定要刪除「${unit} ${name}」這筆綁定嗎？此操作無法復原，該LINE帳號之後將不會再收到這個門牌的包裹通知。`)) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '刪除中...';
  try {
    const resp = await fetch(`/admin/line-bindings/${lineUserId}/delete`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '刪除失敗');
    loadBindings();
  } catch (e) {
    alert('刪除失敗：' + e.message);
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

loadBindings();
