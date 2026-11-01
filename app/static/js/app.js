// ZWOL Sync Client App

function showToast(message, type = 'info', duration = 3500) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 250);
  }, duration);
}

// --------------------------------------------------------------------------
// SYNC CACHES
// --------------------------------------------------------------------------

async function triggerSync(type) {
  showToast(`Synchronisatie voor ${type} gestart...`, 'info');
  try {
    const res = await fetch(`/api/sync/${type}`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      setTimeout(() => location.reload(), 1200);
    } else {
      showToast(`Fout: ${data.error}`, 'error', 5000);
    }
  } catch (err) {
    showToast(`Verbindingsfout: ${err.message}`, 'error');
  }
}

// --------------------------------------------------------------------------
// VELD OVERNEMEN (FIELD SYNC)
// --------------------------------------------------------------------------

async function syncField(target, entityId, fieldName, newVal, oldVal) {
  try {
    const res = await fetch('/api/queue/apply-field', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target: target,
        action: 'UPDATE_FIELD',
        entity_id: entityId,
        field_name: fieldName,
        new_value: newVal,
        old_value: oldVal
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      updateQueueBadge();
    } else {
      showToast(`Fout: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Fout bij verzenden: ${err.message}`, 'error');
  }
}

// --------------------------------------------------------------------------
// BULK SYNC PER BEDRIJF
// --------------------------------------------------------------------------

async function bulkSync(custNo, hsId, direction) {
  const card = document.getElementById(`card-${custNo}`);
  if (!card) return;

  const rows = card.querySelectorAll('.diff-row');
  if (rows.length === 0) return;

  showToast('Bulk update wordt uitgevoerd...', 'info');

  for (const row of rows) {
    const btn = direction === 'TO_HUBSPOT' ? row.querySelector('.btn-to-hs') : row.querySelector('.btn-to-ss');
    if (btn) {
      btn.click();
      await new Promise(r => setTimeout(r, 150));
    }
  }
  showToast('Alle velden succesvol in gang gezet!', 'success');
}

// --------------------------------------------------------------------------
// AANMAKEN IN HUBSPOT
// --------------------------------------------------------------------------

async function createInHubspot(custNo, name) {
  if (!confirm(`Weet je zeker dat je '${name}' wilt aanmaken in HubSpot?`)) return;

  showToast(`Bedrijf '${name}' wordt aangemaakt in HubSpot...`, 'info');
  try {
    const res = await fetch('/api/queue/create-in-hubspot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ customer_number: custNo })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      setTimeout(() => location.reload(), 1500);
    } else {
      showToast(`Fout: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Fout: ${err.message}`, 'error');
  }
}

// --------------------------------------------------------------------------
// CONTACTPERSOON NAAR HUBSPOT PUSHEN
// --------------------------------------------------------------------------

async function addContactToHubspot(companyId, name, email, phone) {
  const parts = name.trim().split(' ');
  const firstName = parts[0] || '';
  const lastName = parts.slice(1).join(' ') || '';

  try {
    const res = await fetch('/api/queue/apply-field', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target: 'HUBSPOT',
        action: 'ADD_CONTACT',
        entity_id: companyId,
        payload: {
          first_name: firstName,
          last_name: lastName,
          email: email,
          phone: phone
        }
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Contactpersoon '${name}' gekoppeld in HubSpot!`, 'success');
    } else {
      showToast(`Fout: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Fout: ${err.message}`, 'error');
  }
}

// --------------------------------------------------------------------------
// HANDMATIG KOPPELEN (MODAL)
// --------------------------------------------------------------------------

let currentModalSsNo = null;

function openManualMatch(ssNo, ssName) {
  currentModalSsNo = ssNo;
  document.getElementById('modal-ss-no').textContent = ssNo;
  document.getElementById('modal-ss-name').textContent = ssName;
  document.getElementById('modal-search-input').value = ssName;
  document.getElementById('manual-match-modal').style.display = 'flex';
  searchHubspotCompanies(ssName);
}

function closeManualMatch() {
  document.getElementById('manual-match-modal').style.display = 'none';
  currentModalSsNo = null;
}

let searchDebounce = null;
function searchHubspotCompanies(query) {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(async () => {
    const list = document.getElementById('modal-search-results');
    list.innerHTML = '<div style="padding:10px;color:#888;">Zoeken...</div>';
    try {
      const res = await fetch(`/api/companies/hubspot-search?q=${encodeURIComponent(query)}`);
      const data = await res.json();
      if (!data.results || data.results.length === 0) {
        list.innerHTML = '<div style="padding:10px;color:#888;">Geen HubSpot bedrijven gevonden.</div>';
        return;
      }
      let html = '';
      data.results.forEach(h => {
        html += `
          <div class="modal-result-item" onclick="confirmManualMatch('${currentModalSsNo}', '${h.hubspot_id}', '${escapeHtml(h.name)}')">
            <strong>${escapeHtml(h.name)}</strong> 
            <small style="color:#64748b;">(ID: ${h.hubspot_id} • ${escapeHtml(h.city || 'Geen plaats')} • KvK: ${escapeHtml(h.kvk_number || '-')})</small>
          </div>
        `;
      });
      list.innerHTML = html;
    } catch (err) {
      list.innerHTML = `<div style="padding:10px;color:red;">Fout bij zoeken: ${err.message}</div>`;
    }
  }, 250);
}

async function confirmManualMatch(ssNo, hsId, hsName) {
  if (!confirm(`Koppel klant #${ssNo} aan HubSpot bedrijf '${hsName}'?`)) return;

  try {
    const res = await fetch('/api/match/manual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ silvasoft_customer_number: ssNo, hubspot_id: hsId })
    });
    const data = await res.json();
    if (data.success) {
      showToast('Koppeling succesvol opgeslagen!', 'success');
      closeManualMatch();
      setTimeout(() => location.reload(), 1000);
    } else {
      showToast(`Fout: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Fout: ${err.message}`, 'error');
  }
}

async function unmatch(ssNo) {
  if (!confirm(`Koppeling voor klant #${ssNo} verbreken?`)) return;

  try {
    const res = await fetch('/api/match/unmatch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ silvasoft_customer_number: ssNo })
    });
    const data = await res.json();
    if (data.success) {
      showToast('Koppeling verbroken.', 'info');
      setTimeout(() => location.reload(), 800);
    } else {
      showToast(`Fout: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Fout: ${err.message}`, 'error');
  }
}

// --------------------------------------------------------------------------
// CLIENT-SIDE FILTERING & SEARCH
// --------------------------------------------------------------------------

function filterCompanies() {
  const query = (document.getElementById('company-search').value || '').toLowerCase().trim();
  const cards = document.querySelectorAll('.company-card');

  cards.forEach(card => {
    const name = card.getAttribute('data-name') || '';
    const city = card.getAttribute('data-city') || '';
    const custno = card.getAttribute('data-custno') || '';
    const kvk = card.getAttribute('data-kvk') || '';

    const match = !query || name.includes(query) || city.includes(query) || custno.includes(query) || kvk.includes(query);
    card.style.display = match ? 'block' : 'none';
  });
}

function setFilter(filter) {
  location.href = `/?filter=${filter}`;
}

// --------------------------------------------------------------------------
// QUEUE STATUS POLLING
// --------------------------------------------------------------------------

async function updateQueueBadge() {
  try {
    const res = await fetch('/api/queue/status');
    const data = await res.json();
    if (data.success && data.queue) {
      const cnt = data.queue.pending_count || 0;
      const navBadge = document.getElementById('nav-queue-badge');
      if (navBadge) {
        navBadge.textContent = cnt;
        navBadge.style.display = cnt > 0 ? 'inline-block' : 'none';
      }
      const statCount = document.getElementById('stat-queue-count');
      if (statCount) {
        statCount.textContent = cnt;
      }
    }
  } catch (err) {
    // Stille fout bij polling
  }
}

setInterval(updateQueueBadge, 15000);
document.addEventListener('DOMContentLoaded', updateQueueBadge);

function escapeHtml(text) {
  if (!text) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
