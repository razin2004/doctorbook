import os
import re

new_html_modal = """  <!-- Universal Confirmation Modal -->
  <div class="modal-overlay" id="universalModal">
    <div class="modal-content">
      <div class="modal-icon-container" id="unvIconContainer">
        <i data-lucide="help-circle"></i>
      </div>
      <h3 id="unvTitle">Confirm Action</h3>
      <p id="unvMessage">Are you sure you want to proceed with this action?</p>
      <div class="modal-btns">
        <button class="btn btn-outline" onclick="closeUniversalModal()">Cancel</button>
        <button id="unvConfirmBtn" class="btn btn-primary">Confirm</button>
      </div>
    </div>
  </div>"""

js_unified = """    // ─── Unified Modal System ───
    let activeModalAction = null;
    
    window.openUniversalModal = function(titleOrId, messageOrTitle, actionTextOrMessage, actionFnOrText, isDangerOrFn, isDanger) {
      const modal = document.getElementById('universalModal');
      if (!modal) return;
      
      let id = 'info';
      let title, message, actionText, actionFn, isDangerVal = false;
      
      // Handle variable signatures
      if (['success', 'warning', 'danger', 'info', 'universalModal', 'delete', 'logout'].includes(titleOrId)) {
        id = titleOrId;
        title = messageOrTitle;
        message = actionTextOrMessage;
        actionText = actionFnOrText;
        actionFn = isDangerOrFn;
        isDangerVal = isDanger || false;
      } else {
        title = titleOrId;
        message = messageOrTitle;
        actionText = actionTextOrMessage;
        actionFn = actionFnOrText;
        isDangerVal = isDangerOrFn || false;
      }
      
      // Cleanup legacy token card or WhatsApp buttons if present
      const oldCard = modal.querySelector('.success-token-card');
      if (oldCard) oldCard.remove();
      const waBtn = document.getElementById('adminWhatsAppBtn');
      if (waBtn) waBtn.remove();
      
      // Update Title & Message
      const titleEl = document.getElementById('unvTitle') || modal.querySelector('h3');
      const msgEl = document.getElementById('unvMessage') || modal.querySelector('p');
      if (titleEl) titleEl.textContent = title;
      if (msgEl) msgEl.innerHTML = message;
      
      // Update Confirm Button
      const btn = document.getElementById('unvConfirmBtn') || modal.querySelector('.confirm-btn');
      if (btn) {
        btn.textContent = actionText;
        btn.style.background = isDangerVal ? '#e11d48' : '#0077b6';
        btn.style.boxShadow = isDangerVal ? '0 12px 30px rgba(225, 29, 72, 0.35)' : '0 12px 30px rgba(0, 119, 182, 0.35)';
      }
      
      // Update Icon
      const container = document.getElementById('unvIconContainer') || modal.querySelector('.modal-icon-container');
      if (container) {
        let iconName = 'help-circle';
        if (id === 'success') {
          iconName = 'check-circle';
          container.style.color = '#10b981';
          container.style.background = 'rgba(16, 185, 129, 0.1)';
        } else if (id === 'warning') {
          iconName = 'alert-circle';
          container.style.color = '#f59e0b';
          container.style.background = 'rgba(245, 158, 11, 0.1)';
        } else if (isDangerVal) {
          iconName = 'alert-triangle';
          container.style.color = '#e11d48';
          container.style.background = 'rgba(225, 29, 72, 0.1)';
        } else {
          container.style.color = '#0077b6';
          container.style.background = 'rgba(0, 119, 182, 0.1)';
        }
        container.innerHTML = `<i data-lucide="${iconName}"></i>`;
      }
      
      activeModalAction = actionFn;
      modal.style.display = 'flex';
      if (window.lucide) lucide.createIcons();
    };
    
    window.closeUniversalModal = function() {
      const modal = document.getElementById('universalModal');
      if (modal) modal.style.display = 'none';
      activeModalAction = null;
      
      // Cleanup legacy token card or WhatsApp buttons if present
      const oldCard = modal?.querySelector('.success-token-card');
      if (oldCard) oldCard.remove();
      const waBtn = document.getElementById('adminWhatsAppBtn');
      if (waBtn) waBtn.remove();
    };
    
    // Setup aliases
    window.openModal = window.openUniversalModal;
    window.closeModal = window.closeUniversalModal;
    window.handleConfirm = function() {
      if (activeModalAction) activeModalAction();
      closeUniversalModal();
    };
    
    // Register event listener for confirmation button
    document.addEventListener('DOMContentLoaded', () => {
      const btn = document.getElementById('unvConfirmBtn') || document.querySelector('#universalModal .confirm-btn');
      if (btn) {
        btn.addEventListener('click', () => {
          if (activeModalAction) activeModalAction();
          closeUniversalModal();
        });
      }
    });"""

def find_and_replace_modal_html(content, new_html):
    start_idx = content.find('id="universalModal"')
    if start_idx == -1:
        start_idx = content.find("id='universalModal'")
    if start_idx == -1:
        return content, False
    
    div_start = content.rfind('<div', 0, start_idx)
    open_divs = 0
    pos = div_start
    div_end = -1
    while pos < len(content):
        if content[pos:pos+4] == '<div':
            open_divs += 1
            pos += 4
        elif content[pos:pos+5] == '</div':
            open_divs -= 1
            if open_divs == 0:
                div_end = content.find('>', pos) + 1
                break
            pos += 5
        else:
            pos += 1
            
    if div_end != -1:
        new_content = content[:div_start] + new_html + content[div_end:]
        return new_content, True
    return content, False

# 1. Update templates/booking.html
path = 'templates/booking.html'
content = open(path, encoding='utf-8').read()
content, ok_html = find_and_replace_modal_html(content, new_html_modal)
content = re.sub(r'/\*\*\s*\*\s*Unified Modal System.*?(?=window\.openLogoutModal\s*=)', js_unified + "\n\n    ", content, flags=re.DOTALL)
# Replace alerts
content = content.replace("} else { alert(data.msg); }", "} else { showToast(data.msg, 'error'); }")
content = content.replace("} catch(e) { alert('Failed to add message'); }", "} catch(e) { showToast('Failed to add message', 'error'); }")
content = content.replace("alert('Bookings are only allowed up to 15 days in advance.');", "showToast('Bookings are only allowed up to 15 days in advance.', 'error');")
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print(f"Updated {path} HTML modal: {ok_html}")

# 2. Update templates/confirmation.html
path = 'templates/confirmation.html'
content = open(path, encoding='utf-8').read()
content, ok_html = find_and_replace_modal_html(content, new_html_modal)
content = re.sub(r'let activeModalAction\s*=\s*null;.*?closeModal\(\);\s*}(?=\s*(?:function\s+|window\.)openLogoutModal\b)', js_unified, content, flags=re.DOTALL)
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print(f"Updated {path} HTML modal: {ok_html}")

# 3. Update templates/doctor_dashboard.html
path = 'templates/doctor_dashboard.html'
content = open(path, encoding='utf-8').read()
content, ok_html = find_and_replace_modal_html(content, new_html_modal)
content = re.sub(r'// ── Universal Modal Logic ──.*?(?=// ── Referral Modal Logic ──)', js_unified + "\n\n    ", content, flags=re.DOTALL)
# Replace complete session synchronous confirm
old_complete = """    window.triggerCompleteAction = async function() {
        if (!confirm("Are you sure you want to complete this session? No more bookings can be made for today after completion.")) {
            return;
        }
        const completeSessionBtn = document.getElementById('completeSessionBtn');
        completeSessionBtn.disabled = true;
        const ogContent = completeSessionBtn.innerHTML;
        completeSessionBtn.innerHTML = '<i data-lucide="loader" class="spin-icon"></i> Completing...';
        lucide.createIcons();

        try {
            const res = await fetch('/complete_session', { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                showToast('Session completed successfully.', 'success');
                await fetchStatsAndSync();
            } else {
                showToast(data.msg, 'error');
            }
        } catch (e) {
            showToast('Network error', 'error');
        } finally {
            completeSessionBtn.disabled = false;
            completeSessionBtn.innerHTML = ogContent;
            lucide.createIcons();
        }
    };"""

new_complete = """    window.triggerCompleteAction = function() {
        openUniversalModal('Complete Session', 'Are you sure you want to complete this session? No more bookings can be made for today after completion.', 'Complete Now', async () => {
            const completeSessionBtn = document.getElementById('completeSessionBtn');
            completeSessionBtn.disabled = true;
            const ogContent = completeSessionBtn.innerHTML;
            completeSessionBtn.innerHTML = '<i data-lucide="loader" class="spin-icon"></i> Completing...';
            lucide.createIcons();

            try {
                const res = await fetch('/complete_session', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast('Session completed successfully.', 'success');
                    await fetchStatsAndSync();
                } else {
                    showToast(data.msg, 'error');
                }
            } catch (e) {
                showToast('Network error', 'error');
            } finally {
                completeSessionBtn.disabled = false;
                completeSessionBtn.innerHTML = ogContent;
                lucide.createIcons();
            }
        }, true);
    };"""

content = content.replace(old_complete, new_complete)
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print(f"Updated {path} HTML modal: {ok_html}")

# 4. Update templates/home.html
path = 'templates/home.html'
content = open(path, encoding='utf-8').read()
content, ok_html = find_and_replace_modal_html(content, new_html_modal)
content = re.sub(r'let activeModalAction\s*=\s*null;.*?closeModal\(\);\s*}(?=\s*(?:function\s+|window\.)openLogoutModal\b)', js_unified, content, flags=re.DOTALL)
content = content.replace('alert("Tap the Share icon \\u2197 in Safari\'s bottom bar, then \'Add to Home Screen\'.");', 'openModal(\'PWA Install Guide\', "Tap the Share icon \\u2197 in Safari\'s bottom bar, then choose \'Add to Home Screen\'.", \'Got it\', null, false);')
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print(f"Updated {path} HTML modal: {ok_html}")

# 5. Update templates/patient_dashboard.html
path = 'templates/patient_dashboard.html'
content = open(path, encoding='utf-8').read()
content, ok_html = find_and_replace_modal_html(content, new_html_modal)
content = re.sub(r'// ── Universal Modal ──.*?(?=// ── Toast ──)', js_unified + "\n\n    ", content, flags=re.DOTALL)
content = content.replace('alert("Tap the Share icon \\u2197 in Safari\'s bottom bar, then choose \'Add to Home Screen\'.");', 'openUniversalModal(\'PWA Install Guide\', "Tap the Share icon \\u2197 in Safari\'s bottom bar, then choose \'Add to Home Screen\'.", \'Got it\', null, false);')
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print(f"Updated {path} HTML modal: {ok_html}")

# 6. Update templates/admin_analytics.html
path = 'templates/admin_analytics.html'
content = open(path, encoding='utf-8').read()

# Insert HTML Modal right before </body>
body_end = content.find('</body>')
if body_end != -1:
    content = content[:body_end] + "\n" + new_html_modal + "\n" + content[body_end:]

# Replace admin logout with custom modal logout function
old_logout = """    function openLogoutModal() {
      if (confirm('Are you sure you want to log out from the admin session?')) {
        fetch('/admin_logout', { method: 'POST' })
          .then(() => {
            window.location.href = '/booking';
          });
      }
    }"""

new_logout = """    function openLogoutModal() {
      openUniversalModal('Confirm Logout', 'Are you sure you want to log out from the admin session?', 'Logout', () => {
        fetch('/admin_logout', { method: 'POST' })
          .then(() => {
            window.location.href = '/booking';
          });
      }, true);
    }"""

content = content.replace(old_logout, new_logout)

# Append JS code to the end of the script tag block (right before </script>)
script_end = content.find('</script>', body_end if body_end != -1 else 0)
if script_end != -1:
    # Let's insert the unified JS code block just before the </script> tag
    content = content[:script_end] + "\n\n" + js_unified + "\n" + content[script_end:]

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print(f"Updated {path} with unified HTML/JS modal system.")
