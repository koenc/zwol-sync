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

  const rows = card.querySelectorAll('.diff-highlight');
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
            <small style="color:#64748b;">(ID: ${h.hubspot_id} â€¢ ${escapeHtml(h.city || 'Geen plaats')} â€¢ KvK: ${escapeHtml(h.kvk_number || '-')})</small>
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


// --------------------------------------------------------------------------
// CONTACTPERSOON ACTIES & SYNCHRONISATIE
// --------------------------------------------------------------------------

async function syncContactField(target, entityId, field, newVal, oldVal, contactGuid) {
  const fieldLabels = {
    'phone': 'telefoonnummer',
    'email': 'e-mailadres',
    'name': 'naam'
  };
  const actionMap = {
    'phone': 'UPDATE_CONTACT_PHONE',
    'email': 'UPDATE_CONTACT_EMAIL',
    'name': 'UPDATE_CONTACT_NAME'
  };
  const label = fieldLabels[field] || field;
  const action = actionMap[field] || `UPDATE_CONTACT_${field.toUpperCase()}`;

  if (target === 'HUBSPOT') {
    if (!confirm(`Wil je contactpersoon ${label} in HubSpot bijwerken naar '${newVal || '(leeg)'}'?`)) return;
    try {
      const res = await fetch('/api/queue/apply-field', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target: 'HUBSPOT',
          action: action,
          entity_id: entityId,
          field_name: field,
          new_value: newVal,
          old_value: oldVal
        })
      });
      const data = await res.json();
      if (data.success) {
        showToast(`Contactpersoon ${label} direct bijgewerkt in HubSpot!`, 'success');
        setTimeout(() => location.reload(), 800);
      } else {
        showToast(`Fout: ${data.error}`, 'error');
      }
    } catch (err) {
      showToast(`Fout: ${err.message}`, 'error');
    }
  } else if (target === 'SILVASOFT') {
    if (!confirm(`Wil je contactpersoon ${label} '${newVal}' in Silvasoft plaatsen (via de wachtrij)?`)) return;
    try {
      const res = await fetch('/api/queue/apply-field', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target: 'SILVASOFT',
          action: action,
          entity_id: entityId,
          field_name: field,
          new_value: newVal,
          old_value: oldVal,
          payload: { contact_guid: contactGuid }
        })
      });
      const data = await res.json();
      if (data.success) {
        showToast('In de Silvasoft-wachtrij geplaatst!', 'success');
        setTimeout(() => location.reload(), 800);
      } else {
        showToast(`Fout: ${data.error}`, 'error');
      }
    } catch (err) {
      showToast(`Fout: ${err.message}`, 'error');
    }
  }
}

function handleSyncContactFieldClick(btn) {
  const d = btn.dataset;
  syncContactField(d.target, d.entityId, d.field, d.newVal, d.oldVal, d.cguid);
}

async function syncContactPhone(target, entityId, newVal, oldVal, contactGuid) {
  return syncContactField(target, entityId, 'phone', newVal, oldVal, contactGuid);
}

async function addContactToSilvasoft(custNo, name, email, phone) {
  if (!confirm(`Contactpersoon '${name}' toevoegen aan Silvasoft (via de wachtrij)?`)) return;
  try {
    const res = await fetch('/api/queue/apply-field', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target: 'SILVASOFT',
        action: 'ADD_CONTACT',
        entity_id: custNo,
        payload: {
          name: name,
          email: email,
          phone: phone
        }
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Contactpersoon '${name}' toegevoegd aan Silvasoft wachtrij!`, 'success');
      setTimeout(() => location.reload(), 800);
    } else {
      showToast(`Fout: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Fout: ${err.message}`, 'error');
  }
}

async function ignoreContact(custNo, hsId, contactId, name, email) {
  if (!confirm(`Wil je contactpersoon '${name}' uitsluiten van synchronisatie? Het telt dan niet meer als verschil.`)) return;
  try {
    const res = await fetch('/api/contacts/ignore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        customer_number: custNo,
        hubspot_id: hsId,
        contact_id: contactId,
        contact_name: name,
        contact_email: email
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Contactpersoon '${name}' genegeerd voor synchronisatie.`, 'info');
      setTimeout(() => location.reload(), 600);
    } else {
      showToast(`Fout: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Fout: ${err.message}`, 'error');
  }
}

async function unignoreContact(custNo, contactId, name) {
  if (!confirm(`Wil je contactpersoon '${name}' weer meenemen in de synchronisatie?`)) return;
  try {
    const res = await fetch('/api/contacts/unignore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        customer_number: custNo,
        contact_id: contactId
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Contactpersoon '${name}' hersteld!`, 'success');
      setTimeout(() => location.reload(), 600);
    } else {
      showToast(`Fout: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Fout: ${err.message}`, 'error');
  }
}

async function ignoreCompany(custNo, name) {
  if (!confirm(`Weet je zeker dat je '${name}' (#${custNo}) wilt negeren?\n\nHet bedrijf wordt dan niet gesynchroniseerd naar HubSpot en verdwijnt uit de lijst 'Nieuw in Silvasoft'.`)) return;
  try {
    const res = await fetch('/api/companies/ignore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        customer_number: custNo,
        name: name
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Bedrijf '${name}' genegeerd voor synchronisatie.`, 'info');
      setTimeout(() => location.reload(), 600);
    } else {
      showToast(`Fout: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Fout: ${err.message}`, 'error');
  }
}

async function unignoreCompany(custNo, name) {
  if (!confirm(`Wil je '${name}' (#${custNo}) herstellen?\n\nHet bedrijf verschijnt dan weer bij 'Nieuw in Silvasoft'.`)) return;
  try {
    const res = await fetch('/api/companies/unignore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        customer_number: custNo
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast(`Bedrijf '${name}' hersteld!`, 'success');
      setTimeout(() => location.reload(), 600);
    } else {
      showToast(`Fout: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`Fout: ${err.message}`, 'error');
  }
}

function toggleAllFields(custNo) {
  const rows = document.querySelectorAll('.row-identical-' + custNo);
  const btn = document.getElementById('btn-toggle-fields-' + custNo);
  if (!rows || rows.length === 0) return;

  const isHidden = (rows[0].style.display === 'none');
  rows.forEach(r => {
    r.style.display = isHidden ? 'table-row' : 'none';
  });

  if (btn) {
    if (isHidden) {
      btn.innerHTML = '👁️ Verberg gelijke velden';
      btn.classList.add('btn-primary');
      btn.classList.remove('btn-outline');
    } else {
      btn.innerHTML = '👁️ Toon alle velden (' + rows.length + ' gelijk)';
      btn.classList.remove('btn-primary');
      btn.classList.add('btn-outline');
    }
  }
}


// --------------------------------------------------------------------------
// HANDMATIG WAARDE INVOEREN (MODAL)
// --------------------------------------------------------------------------

let currentCustomValData = null;

function openCustomValueModal(custNo, hsId, field, label, ssVal, hsVal, companyName) {
  currentCustomValData = {
    isContactPhone: false,
    custNo: custNo,
    hsId: hsId,
    field: field,
    label: label,
    ssVal: ssVal,
    hsVal: hsVal,
    companyName: companyName
  };

  document.getElementById('custom-val-title').innerText = `${label} aanpassen`;
  document.getElementById('custom-val-subtitle').innerText = `${companyName || 'Bedrijf'} (#${custNo} • HS #${hsId})`;
  document.getElementById('custom-val-curr-ss').innerText = ssVal || '(leeg)';
  document.getElementById('custom-val-curr-hs').innerText = hsVal || '(leeg)';
  
  const input = document.getElementById('custom-val-input');
  input.value = (ssVal !== '(leeg)' ? ssVal : (hsVal !== '(leeg)' ? hsVal : ''));
  
  const modal = document.getElementById('custom-val-modal');
  modal.style.display = 'flex';
  setTimeout(() => {
    input.focus();
    input.select();
  }, 50);
}

function openCustomContactFieldModal(custNo, hsId, hsContactId, ssContactGuid, contactName, field, ssVal, hsVal, companyName) {
  const fieldLabels = {
    'phone': 'Telefoonnummer',
    'email': 'E-mailadres',
    'name': 'Naam'
  };
  const labelText = fieldLabels[field] || field;

  currentCustomValData = {
    isContactField: true,
    contactField: field,
    custNo: custNo,
    hsId: hsId,
    hsContactId: hsContactId,
    ssContactGuid: ssContactGuid,
    field: field,
    label: `${labelText}: ${contactName}`,
    ssVal: ssVal,
    hsVal: hsVal,
    companyName: companyName
  };

  document.getElementById('custom-val-title').innerText = `${labelText} aanpassen: ${contactName}`;
  document.getElementById('custom-val-subtitle').innerText = `${companyName || 'Bedrijf'} (#${custNo})`;
  document.getElementById('custom-val-curr-ss').innerText = ssVal || '(leeg)';
  document.getElementById('custom-val-curr-hs').innerText = hsVal || '(leeg)';

  const input = document.getElementById('custom-val-input');
  input.value = (ssVal !== '(leeg)' ? ssVal : (hsVal !== '(leeg)' ? hsVal : ''));

  const modal = document.getElementById('custom-val-modal');
  modal.style.display = 'flex';
  setTimeout(() => {
    input.focus();
    input.select();
  }, 50);
}

function openCustomContactPhoneModal(custNo, hsId, hsContactId, ssContactGuid, contactName, ssVal, hsVal, companyName) {
  return openCustomContactFieldModal(custNo, hsId, hsContactId, ssContactGuid, contactName, 'phone', ssVal, hsVal, companyName);
}

function closeCustomValueModal() {
  const modal = document.getElementById('custom-val-modal');
  if (modal) modal.style.display = 'none';
  currentCustomValData = null;
}

async function submitCustomValue(target) {
  if (!currentCustomValData) return;
  const input = document.getElementById('custom-val-input');
  const newVal = (input.value || '').trim();

  if (!newVal) {
    if (!confirm('Weet je zeker dat je dit veld leeg wilt maken?')) return;
  }

  const { isContactPhone, isContactField, contactField, custNo, hsId, hsContactId, ssContactGuid, field, label } = currentCustomValData;
  const isContact = isContactPhone || isContactField;
  const fType = contactField || (isContactPhone ? 'phone' : field);
  let contactAction = 'UPDATE_CONTACT_PHONE';
  if (fType === 'email') contactAction = 'UPDATE_CONTACT_EMAIL';
  else if (fType === 'name') contactAction = 'UPDATE_CONTACT_NAME';

  showToast('Waarde wordt opgeslagen...', 'info');

  try {
    if (target === 'BOTH' || target === 'HUBSPOT') {
      const hsRes = await fetch('/api/queue/apply-field', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target: 'HUBSPOT',
          action: isContact ? contactAction : 'UPDATE_FIELD',
          entity_id: isContact ? hsContactId : hsId,
          field_name: field,
          new_value: newVal,
          old_value: currentCustomValData.hsVal
        })
      });
      const hsData = await hsRes.json();
      if (!hsData.success) {
        showToast(`Fout bij HubSpot update: ${hsData.error}`, 'error');
        return;
      }
    }

    if (target === 'BOTH' || target === 'SILVASOFT') {
      const ssPayload = isContact ? { contact_guid: ssContactGuid } : {};
      const ssRes = await fetch('/api/queue/apply-field', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target: 'SILVASOFT',
          action: isContact ? contactAction : 'UPDATE_FIELD',
          entity_id: custNo,
          field_name: field,
          new_value: newVal,
          old_value: currentCustomValData.ssVal,
          payload: ssPayload
        })
      });
      const ssData = await ssRes.json();
      if (!ssData.success) {
        showToast(`Fout bij Silvasoft update: ${ssData.error}`, 'error');
        return;
      }
    }

    closeCustomValueModal();
    if (target === 'BOTH') {
      showToast(`'${label}' opgeslagen in HubSpot en ingepland voor Silvasoft!`, 'success');
    } else if (target === 'HUBSPOT') {
      showToast(`'${label}' direct bijgewerkt in HubSpot!`, 'success');
    } else {
      showToast(`'${label}' ingepland voor Silvasoft!`, 'success');
    }

    setTimeout(() => location.reload(), 700);
  } catch (err) {
    showToast(`Fout: ${err.message}`, 'error');
  }
}

function handleSyncFieldClick(btn) {
  const d = btn.dataset;
  syncField(d.target, d.entityId, d.field, d.newVal, d.oldVal);
}

function handleCustomValueClick(btn) {
  const d = btn.dataset;
  openCustomValueModal(d.custNo, d.hsId, d.field, d.label, d.ssVal, d.hsVal, d.compName);
}

function handleCustomContactFieldClick(btn) {
  const d = btn.dataset;
  openCustomContactFieldModal(d.custNo, d.hsId, d.hsCid, d.ssCguid, d.cname, d.field, d.ssVal, d.hsVal, d.compName);
}

function handleCustomContactPhoneClick(btn) {
  return handleCustomContactFieldClick(btn);
}
