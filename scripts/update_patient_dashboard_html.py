import os
import re

def update_dashboard():
    with open('templates/patient_dashboard.html', 'rb') as f:
        content = f.read().decode('utf-8')

    is_crlf = '\r\n' in content
    nl = '\r\n' if is_crlf else '\n'
    c = content.replace('\r\n', '\n')

    # 1. Update Welcome Banner and add Phone Upgrade Alert Banner
    old_banner = """        <!-- Premium Welcome Banner -->
        <div class="workspace-welcome-banner">
          <div class="welcome-greet" id="greetingText">Welcome back,</div>
          <h1 class="welcome-title">{{ session.get('user_name', 'User') }}</h1>
          <p class="welcome-subtitle">
            <span class="sidebar-user-badge" style="background: rgba(255,255,255,0.2); color:white;"><i data-lucide="shield-check" style="width:12px;height:12px;"></i>Verified User Account</span>
            &nbsp;·&nbsp;{{ session.get('user_email') }}
          </p>
        </div>"""

    new_banner = """        <!-- Premium Welcome Banner -->
        <div class="workspace-welcome-banner">
          <div class="welcome-greet" id="greetingText">Welcome back,</div>
          <h1 class="welcome-title">{{ session.get('user_name', 'User') }}</h1>
          <p class="welcome-subtitle" style="display:flex; align-items:center; flex-wrap:wrap; gap:0.5rem; margin-top:0.4rem;">
            {% if user and user.phone_number %}
              <span class="sidebar-user-badge" style="background: rgba(255,255,255,0.25); color:white;">
                <i data-lucide="phone" style="width:12px;height:12px;"></i> {{ user.phone_number }}
              </span>
              {% if user.phone_verified %}
                <span class="sidebar-user-badge" style="background: #10b981; color:white;">
                  <i data-lucide="shield-check" style="width:12px;height:12px;"></i> WhatsApp Verified
                </span>
              {% else %}
                <span class="sidebar-user-badge" style="background: rgba(255,255,255,0.18); color:#fef08a;">
                  <i data-lucide="clock" style="width:12px;height:12px;"></i> Verification Pending
                </span>
              {% endif %}
            {% endif %}
            {% if user and user.email %}
              <span style="opacity: 0.9; font-size: 0.85rem;">{{ user.email }}</span>
            {% endif %}
          </p>
        </div>

        {% if user and not user.phone_number %}
        <!-- MIGRATION BANNER FOR LEGACY EMAIL ACCOUNTS -->
        <div id="migrationPhoneAlert" style="background: linear-gradient(135deg, #eff6ff, #dbeafe); border: 1.5px solid #3b82f6; border-radius: 16px; padding: 1.25rem 1.5rem; margin-bottom: 1.5rem; display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap;">
          <div style="display: flex; align-items: center; gap: 0.85rem;">
            <div style="background: #2563eb; color: white; width: 42px; height: 42px; border-radius: 12px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;">
              <i data-lucide="phone-call" style="width: 22px; height: 22px;"></i>
            </div>
            <div>
              <h4 style="margin: 0; font-size: 1rem; color: #1e3a8a; font-weight: 700;">Add Your Mobile Number</h4>
              <p style="margin: 0.25rem 0 0 0; font-size: 0.85rem; color: #1d4ed8;">Upgrade your account to enable WhatsApp appointment alerts and instant booking management.</p>
            </div>
          </div>
          <button onclick="promptUpdatePhone()" style="background: #2563eb; color: white; border: none; border-radius: 10px; padding: 0.65rem 1.4rem; font-weight: 700; font-size: 0.88rem; cursor: pointer; display: flex; align-items: center; gap: 0.4rem; box-shadow: 0 4px 10px rgba(37,99,235,0.25);">
            <i data-lucide="plus" style="width: 16px; height: 16px;"></i> Add Mobile Number
          </button>
        </div>
        {% endif %}"""

    if old_banner in c:
        c = c.replace(old_banner, new_banner)
        print("[OK] Updated Welcome Banner & Migration Alert in patient_dashboard.html.")
    else:
        print("[WARN] old_banner not found.")

    # 2. Add Relationship field in Patient Modal
    old_modal_inputs = """        <div class="form-group" style="display: flex; flex-direction: column; gap: 0.35rem; width: 100%; text-align: left;">
          <label style="font-size: 0.8rem; font-weight: 700; color: #475569; display: flex; align-items: center; gap: 0.3rem;"><i data-lucide="phone" style="width: 14px; height: 14px;"></i> Phone Number</label>
          <input type="tel" id="patientModalPhone" pattern="[0-9]{10}" maxlength="10" required placeholder="10-digit phone number" style="width: 100%; border: 1px solid #cbd5e1; border-radius: 10px; padding: 0.6rem; font-size: 0.9rem;" />
        </div>"""

    new_modal_inputs = """        <div class="form-group" style="display: flex; flex-direction: column; gap: 0.35rem; width: 100%; text-align: left;">
          <label style="font-size: 0.8rem; font-weight: 700; color: #475569; display: flex; align-items: center; gap: 0.3rem;"><i data-lucide="phone" style="width: 14px; height: 14px;"></i> Phone Number</label>
          <input type="tel" id="patientModalPhone" required placeholder="Phone number (e.g. 9876543210 or +91...)" style="width: 100%; border: 1px solid #cbd5e1; border-radius: 10px; padding: 0.6rem; font-size: 0.9rem;" />
        </div>
        
        <div class="form-group" style="display: flex; flex-direction: column; gap: 0.35rem; width: 100%; text-align: left;">
          <label style="font-size: 0.8rem; font-weight: 700; color: #475569; display: flex; align-items: center; gap: 0.3rem;"><i data-lucide="users" style="width: 14px; height: 14px;"></i> Relationship</label>
          <select id="patientModalRelationship" style="width: 100%; border: 1px solid #cbd5e1; border-radius: 10px; padding: 0.6rem; font-size: 0.9rem; background: white;">
            <option value="Self">Self</option>
            <option value="Spouse">Spouse</option>
            <option value="Child">Child</option>
            <option value="Parent">Parent</option>
            <option value="Sibling">Sibling</option>
            <option value="Other">Other</option>
          </select>
        </div>"""

    if old_modal_inputs in c:
        c = c.replace(old_modal_inputs, new_modal_inputs)
        print("[OK] Added relationship input in patientModal.")
    else:
        print("[WARN] old_modal_inputs not found.")

    # 3. Update Saved Patient Card rendering to show relationship badge
    old_card_header = """            <h4 style="margin: 0; font-size: 1.05rem; font-weight: 800; color: #0f172a;">${p.name}</h4>
          </div>"""

    new_card_header = """            <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
              <h4 style="margin: 0; font-size: 1.05rem; font-weight: 800; color: #0f172a;">${p.name}</h4>
              <span style="background: #e0f2fe; color: #0369a1; padding: 2px 8px; border-radius: 6px; font-size: 0.72rem; font-weight: 700;">${p.relationship || 'Self'}</span>
            </div>
          </div>"""

    if old_card_header in c:
        c = c.replace(old_card_header, new_card_header)
        print("[OK] Added relationship badge to card header.")
    else:
        print("[WARN] old_card_header not found.")

    # 4. Update JS functions openPatientModal, editPatientProfile, handlePatientSubmit, and promptUpdatePhone
    old_modal_func = "function openPatientModal(id = null, name = '', age = '', gender = '', phone = '') {"
    new_modal_func = "function openPatientModal(id = null, name = '', age = '', gender = '', phone = '', relationship = 'Self') {"

    if old_modal_func in c:
        c = c.replace(old_modal_func, new_modal_func)
        c = c.replace("modalPhone.value = phone;", "modalPhone.value = phone;\n      const modalRel = document.getElementById('patientModalRelationship'); if (modalRel) modalRel.value = relationship || 'Self';")
        print("[OK] Updated openPatientModal signature and assignment.")

    old_edit_call = "editPatientProfile(${p.id}, '${p.name.replace(/'/g, \"\\\\'\")}', '${p.age}', '${p.gender}', '${p.phone_number}')"
    new_edit_call = "editPatientProfile(${p.id}, '${p.name.replace(/'/g, \"\\\\'\")}', '${p.age}', '${p.gender}', '${p.phone_number}', '${(p.relationship || 'Self').replace(/'/g, \"\\\\'\")}')"

    if old_edit_call in c:
        c = c.replace(old_edit_call, new_edit_call)
        print("[OK] Updated editPatientProfile button call.")

    old_edit_def = "window.editPatientProfile = function(id, name, age, gender, phone) {"
    new_edit_def = "window.editPatientProfile = function(id, name, age, gender, phone, relationship = 'Self') {\n      openPatientModal(id, name, age, gender, phone, relationship);\n    };\n    window.oldEdit = function(id, name, age, gender, phone) {"

    if old_edit_def in c:
        c = c.replace(old_edit_def, new_edit_def)
        print("[OK] Updated window.editPatientProfile function.")

    old_submit_body = "body: JSON.stringify({ name, age, gender, phone_number: phone })"
    new_submit_body = """const rel = document.getElementById('patientModalRelationship')?.value || 'Self';
          const bodyPayload = { name, age, gender, phone_number: phone, relationship: rel };
          const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(bodyPayload)
          });"""

    old_submit_block = """        const res = await fetch(url, {
          method: method,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, age, gender, phone_number: phone })
        });"""

    if old_submit_block in c:
        c = c.replace(old_submit_block, new_submit_body)
        print("[OK] Updated handlePatientSubmit body.")

    # 5. Add promptUpdatePhone function at end of script
    phone_update_js = """
    // Prompt update phone for legacy patient accounts
    window.promptUpdatePhone = async function() {
      const input = prompt("Please enter your mobile number (e.g. 9876543210 or +91 9876543210):");
      if (!input || !input.trim()) return;
      try {
        const res = await fetch('/api/patient/update_phone', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ phone_number: input.trim() })
        });
        const data = await res.json();
        if (data.success) {
          showToast(data.msg || "Mobile number updated!", "success");
          setTimeout(() => window.location.reload(), 1000);
        } else {
          showToast(data.msg || "Failed to update mobile number", "error");
        }
      } catch (e) {
        showToast("Network error. Please try again.", "error");
      }
    };
    """
    c = c.replace("</script>", phone_update_js + "\n</script>")
    print("[OK] Injected promptUpdatePhone function.")

    final_content = c.replace('\n', nl)
    with open('templates/patient_dashboard.html', 'wb') as f:
        f.write(final_content.encode('utf-8'))
    print("[SUCCESS] patient_dashboard.html updated successfully.")

if __name__ == '__main__':
    update_dashboard()

