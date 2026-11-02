import os

def update_booking_html():
    with open('templates/booking.html', 'rb') as f:
        content = f.read().decode('utf-8')

    is_crlf = '\r\n' in content
    nl = '\r\n' if is_crlf else '\n'
    c = content.replace('\r\n', '\n')

    # 1. Add Doctor Phone field in addDoctorPanel
    old_add_doc_fields = """        <label for="addDocEmail"><i data-lucide="mail"></i> Doctor Email (For Login)</label>
        <input id="addDocEmail" type="email" required placeholder="e.g. doctor@primecare.com" />"""

    new_add_doc_fields = """        <label for="addDocEmail"><i data-lucide="mail"></i> Doctor Email (For Login & Invitation)</label>
        <input id="addDocEmail" type="email" required placeholder="e.g. doctor@primecare.com" />

        <label for="addDocPhone"><i data-lucide="phone"></i> Doctor Mobile Number</label>
        <input id="addDocPhone" type="tel" placeholder="e.g. +91 9876543210 or 9876543210" />"""

    if old_add_doc_fields in c:
        c = c.replace(old_add_doc_fields, new_add_doc_fields)
        print("[OK] Added addDocPhone field to booking.html.")
    else:
        print("[WARN] old_add_doc_fields not found.")

    # 2. Add Dedicated Admin WhatsApp Number fields in Settings Panel
    old_whatsapp_settings = """          <div>
            <label for="settings_clinic_whatsapp"><i data-lucide="phone-forwarded"></i> WhatsApp Number (Digits Only)</label>
            <input id="settings_clinic_whatsapp" required placeholder="e.g. 918592031725" pattern="[0-9]+" title="Please enter numbers only without spaces or symbols" />
            <small style="color: #64748b; font-size: 0.78rem; display: block; margin-top: 0.25rem;">Include country code, e.g. 91 for India, with no leading + or zeros.</small>
          </div>
        </div>"""

    new_whatsapp_settings = """          <div>
            <label for="settings_clinic_whatsapp"><i data-lucide="phone-forwarded"></i> WhatsApp Number (Digits Only)</label>
            <input id="settings_clinic_whatsapp" required placeholder="e.g. 918592031725" pattern="[0-9]+" title="Please enter numbers only without spaces or symbols" />
            <small style="color: #64748b; font-size: 0.78rem; display: block; margin-top: 0.25rem;">Include country code, e.g. 91 for India, with no leading + or zeros.</small>
          </div>
        </div>

        <!-- DEDICATED ADMIN WHATSAPP NUMBER -->
        <div style="background: rgba(0, 119, 182, 0.04); border: 1.5px solid rgba(0, 119, 182, 0.15); border-radius: 12px; padding: 1.25rem; margin-bottom: 1.5rem;">
          <h4 style="margin: 0 0 0.5rem 0; color: #0077b6; display: flex; align-items: center; gap: 0.5rem; font-size: 0.95rem;">
            <i data-lucide="shield" style="width: 18px; height: 18px;"></i> Dedicated Admin WhatsApp Number
          </h4>
          <p style="color: #64748b; font-size: 0.8rem; margin: 0 0 1rem 0;">
            This authorized mobile number identifies clinic administration commands for the WhatsApp chatbot system.
          </p>
          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.25rem;">
            <div>
              <label for="settings_admin_whatsapp_phone"><i data-lucide="phone"></i> Admin WhatsApp Number</label>
              <input id="settings_admin_whatsapp_phone" placeholder="e.g. +91 85920 31725 or 8592031725" />
            </div>
            <div>
              <label for="settings_confirm_admin_whatsapp_phone"><i data-lucide="check-check"></i> Confirm Admin WhatsApp Number</label>
              <input id="settings_confirm_admin_whatsapp_phone" placeholder="Re-enter Admin WhatsApp number" />
            </div>
          </div>
        </div>"""

    if old_whatsapp_settings in c:
        c = c.replace(old_whatsapp_settings, new_whatsapp_settings)
        print("[OK] Added dedicated Admin WhatsApp inputs to booking.html.")
    else:
        print("[WARN] old_whatsapp_settings not found.")

    # 3. Update loadClinicSettings in JS
    old_load_line = "document.getElementById('settings_clinic_whatsapp').value = settings.clinic_whatsapp || '';"
    new_load_line = """document.getElementById('settings_clinic_whatsapp').value = settings.clinic_whatsapp || '';
        document.getElementById('settings_admin_whatsapp_phone').value = settings.admin_whatsapp_phone || '';
        document.getElementById('settings_confirm_admin_whatsapp_phone').value = settings.admin_whatsapp_phone || '';"""

    if old_load_line in c:
        c = c.replace(old_load_line, new_load_line)
        print("[OK] Updated loadClinicSettings in booking.html.")
    else:
        print("[WARN] old_load_line not found.")

    # 4. Update saveClinicSettings in JS
    old_save_payload = """        clinic_phone: document.getElementById('settings_clinic_phone').value.trim(),
        clinic_whatsapp: document.getElementById('settings_clinic_whatsapp').value.trim(),"""

    new_save_payload = """        clinic_phone: document.getElementById('settings_clinic_phone').value.trim(),
        clinic_whatsapp: document.getElementById('settings_clinic_whatsapp').value.trim(),
        admin_whatsapp_phone: document.getElementById('settings_admin_whatsapp_phone')?.value.trim() || '',
        confirm_admin_whatsapp_phone: document.getElementById('settings_confirm_admin_whatsapp_phone')?.value.trim() || '',"""

    if old_save_payload in c:
        c = c.replace(old_save_payload, new_save_payload)
        print("[OK] Updated saveClinicSettings payload in booking.html.")
    else:
        print("[WARN] old_save_payload not found.")

    # 5. Update adminAddDoctor submit handler to append phone_number
    old_add_doc_js = "formData.append('days',selectedDays.join(', ')); formData.append('day_times',JSON.stringify(dayTimes));"
    new_add_doc_js = "formData.append('days',selectedDays.join(', ')); formData.append('day_times',JSON.stringify(dayTimes));\n            const docPhone = document.getElementById('addDocPhone')?.value.trim() || ''; if(docPhone) formData.append('phone_number', docPhone);"

    if old_add_doc_js in c:
        c = c.replace(old_add_doc_js, new_add_doc_js)
        print("[OK] Updated adminAddDoctor JS in booking.html.")
    else:
        print("[WARN] old_add_doc_js not found.")

    final_content = c.replace('\n', nl)
    with open('templates/booking.html', 'wb') as f:
        f.write(final_content.encode('utf-8'))
    print("[SUCCESS] booking.html updated successfully.")

if __name__ == '__main__':
    update_booking_html()

