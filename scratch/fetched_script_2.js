
    // ─── Lucide icons ───────────────────────────────────────────
    lucide.createIcons();

    // ─── Utility ────────────────────────────────────────────────
    const dayMap = {
      Sunday:0, Monday:1, Tuesday:2, Wednesday:3,
      Thursday:4, Friday:5, Saturday:6
    };
    let doctorsBySpecialization = {};
    let workingDays = [];
    let lastActiveAdminPanel = null; // Track last active admin sub-panel

    function goHome() { window.location.href = '/'; }

    function timeToMinutes(t) {
      if (!t) return 0;
      const [h, m] = t.split(':').map(Number);
      return h * 60 + m;
    }

    function showMessage(divId, msg, color = 'red', timeout = 5000) {
      const div = document.getElementById(divId);
      if (!div) return;
      
      div.style.display = 'block';
      div.style.fontSize = '0.85rem';
      div.style.marginTop = '0.5rem';
      div.style.fontWeight = '500';
      div.style.padding = '0.4rem 0.6rem';
      div.style.borderRadius = '8px';
      
      // Handle semantic colors or raw hex
      const semanticMap = {
          'green': '#16a34a',
          'red': '#ef4444',
          'orange': '#f97316'
      };
      const finalColor = semanticMap[color] || color;

      div.style.color = finalColor;
      div.style.background = `${finalColor}10`; // 10% opacity version of background
      
      if (divId.includes('adminBookDateMsg')) {
          div.style.border = `1px solid ${finalColor}30`;
      }

      div.innerHTML = (color === 'green' 
        ? '<i data-lucide="check-circle" style="width:15px;height:15px;vertical-align:middle;margin-right:8px;"></i> ' 
        : '<i data-lucide="alert-circle" style="width:15px;height:15px;vertical-align:middle;margin-right:8px;"></i> ') + msg;
      
      lucide.createIcons();
      if (timeout > 0) setTimeout(() => { div.style.display = 'none'; div.innerHTML = ''; div.style.background='none'; div.style.border='none'; }, timeout);
    }

    function showToast(msg, type = 'success') {
      const toast = document.getElementById('globalToast');
      if (!toast) return;
      toast.className = 'toast-' + type;
      toast.innerHTML = (type === 'success' 
        ? '<i data-lucide="check-circle" style="width:22px;height:22px;"></i> ' 
        : '<i data-lucide="alert-circle" style="width:22px;height:22px;"></i> ') + msg;
      lucide.createIcons();
      toast.style.display = 'flex';
      setTimeout(() => { toast.style.display = 'none'; }, 5000);
    }

    // map section id → panel card id
    const sectionPanelMap = {
      bookDoctor: 'panelDoctor',
      bookDept:   'panelDept',
      mybookings: 'panelMyBookings',
      admin:      'panelAdmin'
    };

    function showSection(id, clickedEl) {
      document.querySelectorAll('section').forEach(s => s.style.display = 'none');
      document.getElementById(id).style.display = 'block';

      // deactivate all panel cards
      document.querySelectorAll('.panel-card').forEach(c => c.classList.remove('active'));
      // activate the matching panel card
      const panelId = sectionPanelMap[id];
      if (panelId) document.getElementById(panelId)?.classList.add('active');

      if (id === 'admin') {
        checkAdminSession();
      }
      localStorage.setItem('activeSection', id); // Save state
    }

    function switchBookingTab(tab) {
      document.querySelectorAll('.booking-tab').forEach(t => {
          t.classList.remove('active');
          t.style.background = 'transparent';
          t.style.color = '#64748b';
          t.style.fontWeight = '600';
      });
      document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
      
      const activeTab = document.getElementById('tab-' + tab);
      activeTab.classList.add('active');
      activeTab.style.background = 'rgba(0,119,182,0.06)';
      activeTab.style.color = '#0077b6';
      activeTab.style.fontWeight = '700';
      
      document.getElementById('panel-' + tab).style.display = 'block';
      lucide.createIcons();
    }

    window.toggleAdminSidebar = function(open) {
      const sidebar = document.getElementById('adminSidebar');
      const backdrop = document.getElementById('adminSidebarBackdrop');
      if (sidebar && backdrop) {
        if (open) {
          sidebar.classList.add('open');
          backdrop.classList.add('open');
          document.body.style.overflow = 'hidden';
        } else {
          sidebar.classList.remove('open');
          backdrop.classList.remove('open');
          document.body.style.overflow = '';
        }
      }
    };

    async function checkAdminSession() {
      try {
        const res  = await fetch('/check_admin');
        const data = await res.json();
        
        const loginDiv  = document.getElementById('adminLoginDiv');
        const activeDiv = document.getElementById('adminActiveSessionDiv');
        const formDiv   = document.getElementById('adminLoginFormDiv');
        const options   = document.getElementById('adminOptions');

        const headerLogoutBtn = document.getElementById('headerLogoutBtn');

        if (data.logged_in) {
          if (headerLogoutBtn) headerLogoutBtn.style.display = 'inline-flex';
          
          showAdminOptions();
          const activePanel = lastActiveAdminPanel || 'book';
          const panelBtn = document.querySelector(`.admin-sidebar-nav-item[data-panel="${activePanel}"]`);
          showAdminPanel(activePanel, panelBtn);
        } else {
          if (headerLogoutBtn) headerLogoutBtn.style.display = 'none';
          resetAdminLoginUI();
          formDiv.style.display = 'block';
          activeDiv.style.display = 'none';
          options.style.display = 'none';
          loginDiv.style.display = 'block';
          document.getElementById('admin').classList.remove('logged-in');
          const loginFooter = document.getElementById('adminLoginFooter');
          if (loginFooter) {
            loginFooter.style.display = 'block';
          }
          
          document.querySelectorAll('.adminPanel').forEach(p => {
            p.style.display = 'none';
            p.classList.remove('active');
          });
          lastActiveAdminPanel = null;
          localStorage.removeItem('activeAdminPanel');
        }
      } catch(e) { console.error('Session check failed:', e); }
    }

    function resetAdminLoginUI() {
      // Inputs
      const emailField = document.getElementById('adminEmail');
      const otpField = document.getElementById('adminOtp');
      if (emailField) emailField.value = '';
      if (otpField) otpField.value = '';

      // UI Sections
      const otpSection = document.getElementById('otpSection');
      if (otpSection) otpSection.style.display = 'none';

      // Messages
      const preOtpMsg = document.getElementById('adminPreOtpMsg');
      const loginRes = document.getElementById('adminLoginResult');
      if (preOtpMsg) { preOtpMsg.textContent = ''; preOtpMsg.style.display = 'none'; }
      if (loginRes) { loginRes.textContent = ''; loginRes.style.display = 'none'; }

      // Buttons
      const sendBtn = document.getElementById('sendOtpBtn');
      if (sendBtn) {
        sendBtn.disabled = false;
        sendBtn.style.display = 'inline-flex';
        sendBtn.innerHTML = '<i data-lucide="send-horizontal"></i> Send OTP';
      }
      
      const loginBtn = document.querySelector('#adminLoginForm button[type="submit"]');
      if (loginBtn) {
        loginBtn.disabled = false;
        loginBtn.innerHTML = '<i data-lucide="log-in"></i> Login';
      }

      const toggleBtn = document.getElementById('headerAdminSidebarToggle');
      if (toggleBtn) {
        toggleBtn.classList.remove('admin-logged-in');
      }
      lucide.createIcons();
    }

    function showAdminOptions() {
      document.getElementById('adminLoginDiv').style.display = 'none';
      document.getElementById('adminOptions').style.display = 'block';
      document.getElementById('admin').classList.add('logged-in');
      const toggleBtn = document.getElementById('headerAdminSidebarToggle');
      if (toggleBtn) {
        toggleBtn.classList.add('admin-logged-in');
      }
      const loginFooter = document.getElementById('adminLoginFooter');
      if (loginFooter) {
        loginFooter.style.display = 'none';
      }
      loadDoctorPairs();
      lucide.createIcons();
    }

    function goToCurrentDashboard() {
      
        openModal(
          'warning',
          'Login Portal Navigation',
          'You are not logged in as a doctor or User. You will be redirected to the homepage login portal. Do you want to continue?',
          'Continue to Login',
          () => { window.location.href = '/?trigger_login=true&msg=Please login to access your dashboard'; }
        );
      
    }

    const adminPanelMeta = {
      'book': { group: 'Booking Hub', label: 'New Appointment' },
      'view': { group: 'Booking Hub', label: 'Manage Appointments' },
      'cancel': { group: 'Booking Hub', label: 'Cancel Appointments' },
      'ticker': { group: 'Booking Hub', label: 'Queue & Announcements' },
      'add': { group: 'Staff Roster', label: 'Register Doctor' },
      'edit': { group: 'Staff Roster', label: 'Edit Schedules' },
      'leaves': { group: 'Staff Roster', label: 'Temporary Leaves' },
      'delete': { group: 'Staff Roster', label: 'Remove Doctor' },
      'holiday': { group: 'Clinic Operations', label: 'Manage Holidays' },
      'settings': { group: 'Clinic Operations', label: 'System Settings' },
      'security': { group: 'Clinic Operations', label: 'Security & Access' }
    };

    const adminPanelInfo = {
      'book': {
        title: 'New Appointment',
        icon: 'phone-call',
        desc: 'Manually check-in patients, assign token numbers, and book new appointments.',
        mobileDesc: 'Book appointments and check-in patients.'
      },
      'view': {
        title: 'Manage Appointments',
        icon: 'calendar-check-2',
        desc: 'Browse, filter, edit, or cancel appointments. Generate and print active reports.',
        mobileDesc: 'Filter, print, and manage appointments.'
      },
      'cancel': {
        title: 'Cancel Appointments',
        icon: 'calendar-x',
        desc: 'Search for active bookings by doctor and date, and manually cancel patient appointments.',
        mobileDesc: 'Manually cancel patient appointments.'
      },
      'ticker': {
        title: 'Queue & Announcements',
        icon: 'radio',
        desc: 'Broadcast clinic-wide alerts and manage active doctor queues on the home page.',
        mobileDesc: 'Broadcast announcements and queue alerts.'
      },
      'add': {
        title: 'Register Doctor',
        icon: 'user-plus',
        desc: 'Add new doctors to the system, upload profiles, and define default schedules.',
        mobileDesc: 'Add new doctors and set schedules.'
      },
      'edit': {
        title: 'Edit Schedules',
        icon: 'pencil',
        desc: 'Update working days and modify clinic hours for doctors on the active roster.',
        mobileDesc: 'Modify working hours and schedules.'
      },
      'leaves': {
        title: 'Temporary Leaves',
        icon: 'calendar-x',
        desc: 'Schedule and manage temporary leave days for doctors on the clinic roster.',
        mobileDesc: 'Manage temporary leaves for doctors.'
      },
      'delete': {
        title: 'Remove Doctor',
        icon: 'trash-2',
        desc: 'Deactivate doctor accounts and delete them from the active clinic roster.',
        mobileDesc: 'Deactivate and delete doctor accounts.'
      },
      'holiday': {
        title: 'Manage Holidays',
        icon: 'calendar-off',
        desc: 'Block out dates on the clinic calendar for holidays and scheduled closures.',
        mobileDesc: 'Schedule holidays and closures.'
      },
      'settings': {
        title: 'System Settings',
        icon: 'sliders',
        desc: 'Update clinic contact information, WhatsApp numbers, map links, and strip statistics.',
        mobileDesc: 'Configure contact info and site stats.'
      },
      'security': {
        title: 'Security & Access',
        icon: 'key-round',
        desc: 'Manage administrative login passwords and toggle access methods.',
        mobileDesc: 'Manage passwords and login access.'
      }
    };

    function showAdminPanel(type, btn) {
      lastActiveAdminPanel = type; // Save state
      document.querySelectorAll('.adminPanel').forEach(p => p.style.display = 'none');
      document.querySelectorAll('.admin-sidebar-nav-item').forEach(b => b.classList.remove('active'));
      
      if (!btn) {
        btn = document.querySelector(`.admin-sidebar-nav-item[data-panel="${type}"]`);
      }
      if (btn) btn.classList.add('active');
      
      if (type === 'add')    document.getElementById('addDoctorPanel').style.display    = 'block';
      else if (type === 'edit')   document.getElementById('editDoctorPanel').style.display   = 'block';
      else if (type === 'leaves') {
        document.getElementById('leavesPanel').style.display = 'block';
        loadDoctorsForEdit();
        refreshLeaveListForCurrentDoctor();
      }
      else if (type === 'delete') document.getElementById('deleteDoctorPanel').style.display = 'block';
      else if (type === 'view') {
        document.getElementById('viewBookingsPanel').style.display = 'block';
        loadDoctorsForView();
      }
      else if (type === 'cancel') {
        document.getElementById('cancelBookingPanel').style.display = 'block';
        loadDoctorsForEdit();
        fetchCancelBookingsList();
      }
      else if (type === 'book') {
        document.getElementById('bookPatientPanel').style.display = 'block';
        loadDoctorsForAdminBooking();
      }
      else if (type === 'holiday') {
        document.getElementById('holidayPanel').style.display = 'block';
        loadGlobalHolidays();
      }
      else if (type === 'ticker') {
        document.getElementById('tickerPanel').style.display = 'block';
        loadTickerMessages();
      }
      else if (type === 'settings') {
        document.getElementById('settingsPanel').style.display = 'block';
        loadClinicSettings();
      }
      else if (type === 'security') {
        document.getElementById('securityPanel').style.display = 'block';
        checkAdminPasswordStatus();
        loadPasswordLoginSetting();
      }
      
      // Update breadcrumbs
      const meta = adminPanelMeta[type];
      if (meta) {
        const groupEl = document.getElementById('currentBreadcrumbGroup');
        const activeEl = document.getElementById('currentBreadcrumbActive');
        if (groupEl) groupEl.textContent = meta.group;
        if (activeEl) activeEl.textContent = meta.label;
      }

      // Update welcome banner title, description, and icon
      const info = adminPanelInfo[type];
      if (info) {
        const titleEl = document.getElementById('adminWelcomeTitle');
        const descDesktopEl = document.getElementById('adminWelcomeDescDesktop');
        const descMobileEl = document.getElementById('adminWelcomeDescMobile');
        if (titleEl) {
          titleEl.innerHTML = `<i data-lucide="${info.icon}" style="width: 24px; height: 24px; color: #0077b6;"></i> ${info.title}`;
        }
        if (descDesktopEl) {
          descDesktopEl.textContent = info.desc;
        }
        if (descMobileEl) {
          descMobileEl.textContent = info.mobileDesc;
        }
      }
      
      // Close sidebar drawer on mobile
      if (window.innerWidth <= 991) {
        toggleAdminSidebar(false);
      }
      
      localStorage.setItem('activeAdminPanel', type); // Save state
      lucide.createIcons();
    }

    // ─── Admin Security Settings Logic ───────────────────────
    const SEND_OTP_BTN_DEFAULT_HTML = '<i data-lucide="send-horizontal"></i> Send OTP to Admin Email';

    function resetForgotTabState() {
      // Reset step back to 1
      document.getElementById('forgotStep1').style.display = 'block';
      document.getElementById('forgotStep2').style.display = 'none';
      // Fully restore the send-OTP button
      const btn = document.getElementById('sendAdminResetOtpBtn');
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = SEND_OTP_BTN_DEFAULT_HTML;
      }
      // Clear OTP + password fields
      const otpFld = document.getElementById('admin_reset_otp');
      const newFld = document.getElementById('admin_reset_new_pass');
      const conFld = document.getElementById('admin_reset_confirm_pass');
      if (otpFld) otpFld.value = '';
      if (newFld) newFld.value = '';
      if (conFld) conFld.value = '';
      const status = document.getElementById('resetPassMatchStatus');
      if (status) status.innerHTML = '';
      if (window.lucide) lucide.createIcons();
    }

    function switchSecTab(tab) {
      const changeTab  = document.getElementById('secChangeTab');
      const forgotTab  = document.getElementById('secForgotTab');
      const btnChange  = document.getElementById('secTabChange');
      const btnForgot  = document.getElementById('secTabForgot');

      if (tab === 'change') {
        changeTab.style.display = 'block';
        forgotTab.style.display = 'none';
        btnChange.style.color = '#6366f1';
        btnChange.style.borderBottomColor = '#6366f1';
        btnForgot.style.color = '#94a3b8';
        btnForgot.style.borderBottomColor = 'transparent';
      } else {
        changeTab.style.display = 'none';
        forgotTab.style.display = 'block';
        btnForgot.style.color = '#6366f1';
        btnForgot.style.borderBottomColor = '#6366f1';
        btnChange.style.color = '#94a3b8';
        btnChange.style.borderBottomColor = 'transparent';
        // Always fully reset the forgot tab state when opening it
        resetForgotTabState();
      }
    }

    function checkResetPassMatch() {
      const newPass  = (document.getElementById('admin_reset_new_pass')?.value  || '');
      const confPass = (document.getElementById('admin_reset_confirm_pass')?.value || '');
      const statusEl = document.getElementById('resetPassMatchStatus');
      if (!statusEl) return;
      if (!confPass && !newPass) { statusEl.innerHTML = ''; return; }
      if (!confPass) { statusEl.innerHTML = ''; return; }
      if (newPass === confPass) {
        statusEl.innerHTML = '<span style="color:#10b981;">Passwords match</span>';
      } else {
        statusEl.innerHTML = '<span style="color:#ef4444;">Passwords do not match</span>';
      }
    }

    function checkChangePassMatch() {
      const newPass  = (document.getElementById('settings_admin_new_password')?.value  || '');
      const confPass = (document.getElementById('settings_admin_confirm_password')?.value || '');
      const statusEl = document.getElementById('changePassMatchStatus');
      if (!statusEl) return;
      if (!confPass && !newPass) { statusEl.innerHTML = ''; return; }
      if (!confPass) { statusEl.innerHTML = ''; return; }
      if (newPass === confPass) {
        statusEl.innerHTML = '<span style="color:#10b981;">Passwords match</span>';
      } else {
        statusEl.innerHTML = '<span style="color:#ef4444;">Passwords do not match</span>';
      }
    }

    async function checkAdminPasswordStatus() {
      try {
        const res = await fetch('/admin_check_password_status');
        const data = await res.json();
        if (data.is_set) {
          document.getElementById('currentPasswordGroup').style.display = 'block';
          document.getElementById('settings_admin_current_password').required = true;
        } else {
          document.getElementById('currentPasswordGroup').style.display = 'none';
          document.getElementById('settings_admin_current_password').required = false;
        }
      } catch (err) {
        console.error(err);
      }
    }

    async function saveAdminSecurity(e) {
      e.preventDefault();
      const currentPass = document.getElementById('settings_admin_current_password').value;
      const newPass = document.getElementById('settings_admin_new_password').value;
      const confirmPass = document.getElementById('settings_admin_confirm_password').value;

      if (newPass !== confirmPass) {
        showToast('New passwords do not match.', 'error');
        return;
      }

      const btn = document.getElementById('saveSecurityBtn');
      const originalHtml = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<i data-lucide="loader-circle" class="spin-icon"></i> Updating...';
      if (window.lucide) lucide.createIcons();

      try {
        const res = await fetch('/admin_change_password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            current_password: currentPass,
            new_password: newPass
          })
        });
        const data = await res.json();
        if (data.success) {
          showToast(data.msg, 'success');
          document.getElementById('adminSecurityForm').reset();
          const cst = document.getElementById('changePassMatchStatus');
          if (cst) cst.innerHTML = '';
          checkAdminPasswordStatus();
        } else {
          showToast(data.msg, 'error');
        }
      } catch (err) {
        console.error(err);
        showToast('Failed to update password.', 'error');
      } finally {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
        if (window.lucide) lucide.createIcons();
      }
    }

    async function sendAdminResetOtp() {
      const btn = document.getElementById('sendAdminResetOtpBtn');
      const originalHtml = SEND_OTP_BTN_DEFAULT_HTML;
      btn.disabled = true;
      btn.innerHTML = '<i data-lucide="loader-circle" class="spin-icon"></i> Sending...';
      if (window.lucide) lucide.createIcons();
      try {
        const res = await fetch('/admin_send_reset_otp', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
          showToast('OTP sent to admin email.', 'success');
          document.getElementById('forgotStep1').style.display = 'none';
          document.getElementById('forgotStep2').style.display = 'block';
          if (window.lucide) lucide.createIcons();
        } else {
          showToast(data.msg || 'Failed to send OTP.', 'error');
          btn.disabled = false;
          btn.innerHTML = originalHtml;
          if (window.lucide) lucide.createIcons();
        }
      } catch (err) {
        console.error(err);
        showToast('Error sending OTP.', 'error');
        btn.disabled = false;
        btn.innerHTML = originalHtml;
        if (window.lucide) lucide.createIcons();
      }
    }

    async function resendAdminResetOtp() {
      const btn = document.getElementById('resendAdminOtpBtn');
      if (!btn) return;
      const origText = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'Sending...';
      try {
        const res = await fetch('/admin_send_reset_otp', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
          showToast('OTP resent to admin email.', 'success');
          // Clear old OTP field so user knows to enter new code
          const otpFld = document.getElementById('admin_reset_otp');
          if (otpFld) otpFld.value = '';
        } else {
          showToast(data.msg || 'Failed to resend OTP.', 'error');
        }
      } catch (err) {
        console.error(err);
        showToast('Error resending OTP.', 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = origText;
      }
    }

    async function verifyAdminResetOtp() {
      const otp      = document.getElementById('admin_reset_otp').value.trim();
      const newPass  = document.getElementById('admin_reset_new_pass').value;
      const confPass = document.getElementById('admin_reset_confirm_pass').value;

      if (!otp || otp.length < 6) { showToast('Please enter the 6-digit OTP.', 'error'); return; }
      if (!newPass || newPass.length < 6) { showToast('Password must be at least 6 characters.', 'error'); return; }
      if (newPass !== confPass) { showToast('Passwords do not match.', 'error'); return; }

      const btn = document.getElementById('verifyAdminResetBtn');
      const originalHtml = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<i data-lucide="loader-circle" class="spin-icon"></i> Verifying...';
      if (window.lucide) lucide.createIcons();

      try {
        const res = await fetch('/admin_reset_password_otp', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ otp, new_password: newPass })
        });
        const data = await res.json();
        if (data.success) {
          showToast(data.msg, 'success');
          // Fully reset the forgot tab then switch to change-password tab
          resetForgotTabState();
          switchSecTab('change');
          checkAdminPasswordStatus();
        } else {
          showToast(data.msg || 'Failed to reset password.', 'error');
        }
      } catch (err) {
        console.error(err);
        showToast('Error resetting password.', 'error');
      } finally {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
        if (window.lucide) lucide.createIcons();
      }
    }


    // ─── Login Settings (Password Login Toggle) ───────────────────────
    function applyToggleUI(enabled) {
      const track = document.getElementById('toggleTrack');
      const thumb = document.getElementById('toggleThumb');
      const label = document.getElementById('toggleLabel');
      const statusText = document.getElementById('passwordLoginStatusText');
      const checkbox = document.getElementById('passwordLoginToggle');
      if (!track) return;
      if (enabled) {
        track.style.background = '#10b981';
        thumb.style.transform = 'translateX(24px)';
        label.textContent = 'Enabled';
        label.style.color = '#10b981';
        statusText.textContent = 'Admin can log in using email & password from the login modal.';
        checkbox.checked = true;
      } else {
        track.style.background = '#cbd5e1';
        thumb.style.transform = 'translateX(0)';
        label.textContent = 'Disabled';
        label.style.color = '#94a3b8';
        statusText.textContent = 'Password login is disabled. Admin must use OTP-based login.';
        checkbox.checked = false;
      }
    }

    async function loadPasswordLoginSetting() {
      try {
        const res = await fetch('/admin_get_login_setting');
        const data = await res.json();
        applyToggleUI(data.password_login_enabled !== false);
      } catch (err) {
        console.error(err);
      }
    }

    async function savePasswordLoginSetting(enabled) {
      // Optimistically update UI first
      applyToggleUI(enabled);
      try {
        const res = await fetch('/admin_set_login_setting', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password_login_enabled: enabled })
        });
        const data = await res.json();
        if (data.success) {
          showToast(enabled ? 'Password login enabled.' : 'Password login disabled.', 'success');
        } else {
          showToast(data.msg || 'Failed to save setting.', 'error');
          // Revert on failure
          applyToggleUI(!enabled);
        }
      } catch (err) {
        console.error(err);
        showToast('Error saving setting.', 'error');
        applyToggleUI(!enabled);
      }
    }


    // ─── Clinic Settings Management Logic ───────────────────────

    async function uploadSettingImage(fileInput, targetInputId, previewImgId, fallbackIconId) {
      const file = fileInput.files[0];
      if (!file) return;

      const previewImg = document.getElementById(previewImgId);
      const fallbackIcon = document.getElementById(fallbackIconId);
      
      // Temporarily display local preview
      previewImg.src = URL.createObjectURL(file);
      previewImg.style.display = 'block';
      if (fallbackIcon) fallbackIcon.style.display = 'none';

      const formData = new FormData();
      formData.append('file', file);

      try {
        const res = await fetch('/admin_upload_image', {
          method: 'POST',
          body: formData
        });
        const data = await res.json();
        if (data.success) {
          document.getElementById(targetInputId).value = data.url;
          showToast('Image uploaded successfully', 'success');
        } else {
          showToast(data.msg || 'Image upload failed', 'error');
          previewImg.style.display = 'none';
          if (fallbackIcon) fallbackIcon.style.display = 'block';
        }
      } catch (e) {
        console.error('Image upload failed:', e);
        showToast('Error uploading image', 'error');
        previewImg.style.display = 'none';
        if (fallbackIcon) fallbackIcon.style.display = 'block';
      }
    }

    function updatePreviewFromInput(inputId, previewImgId, fallbackIconId) {
      const url = document.getElementById(inputId).value.trim();
      const previewImg = document.getElementById(previewImgId);
      const fallbackIcon = document.getElementById(fallbackIconId);
      if (url) {
        previewImg.src = url;
        previewImg.style.display = 'block';
        if (fallbackIcon) fallbackIcon.style.display = 'none';
      } else {
        previewImg.style.display = 'none';
        if (fallbackIcon) fallbackIcon.style.display = 'block';
      }
    }

    async function loadClinicSettings() {
      try {
        const res = await fetch('/admin_get_settings');
        const data = await res.json();
        if (!data.success) {
          showToast(data.msg || 'Failed to load clinic settings', 'error');
          return;
        }
        
        const settings = data.settings;
        document.getElementById('settings_clinic_phone').value = settings.clinic_phone || '';
        document.getElementById('settings_clinic_whatsapp').value = settings.clinic_whatsapp || '';
        document.getElementById('settings_clinic_address').value = settings.clinic_address || '';
        document.getElementById('settings_clinic_map_link').value = settings.clinic_map_link || '';
        document.getElementById('settings_stat_specialists').value = settings.stat_specialists || '';
        document.getElementById('settings_stat_patients').value = settings.stat_patients || '';
        document.getElementById('settings_stat_since').value = settings.stat_since || '';
        document.getElementById('settings_stat_certified').value = settings.stat_certified || '';

        // Homepage promotional content fields
        document.getElementById('settings_promo_heading').value = settings.promo_heading || '';
        document.getElementById('settings_promo_subheading').value = settings.promo_subheading || '';
        document.getElementById('settings_promo_heritage_heading').value = settings.promo_heritage_heading || '';
        document.getElementById('settings_promo_heritage_text').value = settings.promo_heritage_text || '';
        document.getElementById('settings_promo_award_title').value = settings.promo_award_title || '';
        document.getElementById('settings_promo_award_desc').value = settings.promo_award_desc || '';
        
        // Homepage images and previews
        document.getElementById('settings_promo_heritage_image').value = settings.promo_heritage_image || '';
        updatePreviewFromInput('settings_promo_heritage_image', 'preview_promo_heritage_image', 'icon_promo_heritage_image');
        
        document.getElementById('settings_promo_leader_1_name').value = settings.promo_leader_1_name || '';
        document.getElementById('settings_promo_leader_1_role').value = settings.promo_leader_1_role || '';
        document.getElementById('settings_promo_leader_1_bio').value = settings.promo_leader_1_bio || '';
        document.getElementById('settings_promo_leader_1_image').value = settings.promo_leader_1_image || '';
        updatePreviewFromInput('settings_promo_leader_1_image', 'preview_promo_leader_1_image', 'icon_promo_leader_1_image');

        document.getElementById('settings_promo_leader_2_name').value = settings.promo_leader_2_name || '';
        document.getElementById('settings_promo_leader_2_role').value = settings.promo_leader_2_role || '';
        document.getElementById('settings_promo_leader_2_bio').value = settings.promo_leader_2_bio || '';
        document.getElementById('settings_promo_leader_2_image').value = settings.promo_leader_2_image || '';
        updatePreviewFromInput('settings_promo_leader_2_image', 'preview_promo_leader_2_image', 'icon_promo_leader_2_image');

        document.getElementById('settings_promo_facility_image').value = settings.promo_facility_image || '';
        updatePreviewFromInput('settings_promo_facility_image', 'preview_promo_facility_image', 'icon_promo_facility_image');

        // Setup direct URL input listeners for auto preview
        const setupPreviewListener = (inputId, previewId, iconId) => {
          const inputEl = document.getElementById(inputId);
          if (inputEl) {
            inputEl.oninput = () => updatePreviewFromInput(inputId, previewId, iconId);
          }
        };
        setupPreviewListener('settings_promo_heritage_image', 'preview_promo_heritage_image', 'icon_promo_heritage_image');
        setupPreviewListener('settings_promo_leader_1_image', 'preview_promo_leader_1_image', 'icon_promo_leader_1_image');
        setupPreviewListener('settings_promo_leader_2_image', 'preview_promo_leader_2_image', 'icon_promo_leader_2_image');
        setupPreviewListener('settings_promo_facility_image', 'preview_promo_facility_image', 'icon_promo_facility_image');

      } catch (e) {
        console.error('Failed to load clinic settings:', e);
        showToast('Error fetching settings', 'error');
      }
    }

    async function saveClinicSettings(e) {
      e.preventDefault();
      
      const saveBtn = document.getElementById('saveSettingsBtn');
      const originalHtml = saveBtn.innerHTML;
      saveBtn.disabled = true;
      saveBtn.innerHTML = '<i data-lucide="loader-circle" class="spin-icon"></i> Saving...';
      lucide.createIcons();
      
      const payload = {
        clinic_phone: document.getElementById('settings_clinic_phone').value.trim(),
        clinic_whatsapp: document.getElementById('settings_clinic_whatsapp').value.trim(),
        clinic_address: document.getElementById('settings_clinic_address').value.trim(),
        clinic_map_link: document.getElementById('settings_clinic_map_link').value.trim(),
        stat_specialists: document.getElementById('settings_stat_specialists').value.trim(),
        stat_patients: document.getElementById('settings_stat_patients').value.trim(),
        stat_since: document.getElementById('settings_stat_since').value.trim(),
        stat_certified: document.getElementById('settings_stat_certified').value.trim(),
        
        promo_heading: document.getElementById('settings_promo_heading').value.trim(),
        promo_subheading: document.getElementById('settings_promo_subheading').value.trim(),
        promo_heritage_heading: document.getElementById('settings_promo_heritage_heading').value.trim(),
        promo_heritage_text: document.getElementById('settings_promo_heritage_text').value.trim(),
        promo_award_title: document.getElementById('settings_promo_award_title').value.trim(),
        promo_award_desc: document.getElementById('settings_promo_award_desc').value.trim(),
        promo_heritage_image: document.getElementById('settings_promo_heritage_image').value.trim(),
        promo_leader_1_name: document.getElementById('settings_promo_leader_1_name').value.trim(),
        promo_leader_1_role: document.getElementById('settings_promo_leader_1_role').value.trim(),
        promo_leader_1_bio: document.getElementById('settings_promo_leader_1_bio').value.trim(),
        promo_leader_1_image: document.getElementById('settings_promo_leader_1_image').value.trim(),
        promo_leader_2_name: document.getElementById('settings_promo_leader_2_name').value.trim(),
        promo_leader_2_role: document.getElementById('settings_promo_leader_2_role').value.trim(),
        promo_leader_2_bio: document.getElementById('settings_promo_leader_2_bio').value.trim(),
        promo_leader_2_image: document.getElementById('settings_promo_leader_2_image').value.trim(),
        promo_facility_image: document.getElementById('settings_promo_facility_image').value.trim()
      };
      
      try {
        const res = await fetch('/admin_save_settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.success) {
          showToast('Clinic settings updated successfully!', 'success');
        } else {
          showToast(data.msg || 'Failed to save settings', 'error');
        }
      } catch (e) {
        console.error('Failed to save settings:', e);
        showToast('Error updating settings', 'error');
      } finally {
        saveBtn.disabled = false;
        saveBtn.innerHTML = originalHtml;
        lucide.createIcons();
      }
    }

    // ─── Ticker Management Logic ───────────────────────────────
    async function loadTickerMessages() {
        try {
            const res = await fetch('/admin_get_ticker_messages');
            const data = await res.json();
            if(!data.success) return;

            const list = document.getElementById('activeMessagesList');
            const soloToggle = document.getElementById('tickerSoloToggle');
            const soloStatus = document.getElementById('soloStatus');
            const msgCount = document.getElementById('msgCountCount');
            
            soloToggle.checked = data.solo_mode;
            soloStatus.textContent = data.solo_mode ? "Active" : "Inactive";
            soloStatus.style.background = data.solo_mode ? "#dcfce7" : "#e2e8f0";
            soloStatus.style.color = data.solo_mode ? "#166534" : "#64748b";
            msgCount.textContent = `${data.messages.length} active`;

            if (data.messages.length === 0) {
                list.innerHTML = `
                <div style="text-align:center; padding:2rem 1.5rem; background:rgba(248,250,252,0.5); border:1px dashed rgba(0,0,0,0.1); border-radius:16px;">
                    <i data-lucide="inbox" style="width:32px; height:32px; color:#cbd5e1; margin-bottom:0.75rem;"></i>
                    <div style="color:#94a3b8; font-size:0.85rem;">No active broadcasts. Your ticker will show automated doctor schedules.</div>
                </div>`;
                lucide.createIcons();
                return;
            }

            list.innerHTML = data.messages.map(m => `
                <div class="card" style="display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; padding:1.2rem; background:white; border:1px solid rgba(0,0,0,0.06); border-radius:16px; transition:all 0.3s ease; box-shadow:0 4px 6px -1px rgba(0,0,0,0.02);">
                    <div style="display:flex; gap:0.75rem;">
                        <div style="margin-top:0.2rem; color:#10b981;"><i data-lucide="check-circle-2" style="width:16px;"></i></div>
                        <div style="font-size:0.9rem; color:#1e293b; line-height:1.5; font-weight:500;">${m.content}</div>
                    </div>
                    <button onclick="deleteTickerMsg(${m.id})" style="background:rgba(239,68,68,0.05); border:none; color:#ef4444; cursor:pointer; padding:8px; border-radius:10px; flex-shrink:0; transition:all 0.2s ease;" onmouseover="this.style.background='rgba(239,68,68,0.1)'" onmouseout="this.style.background='rgba(239,68,68,0.05)'">
                        <i data-lucide="trash-2" style="width:16px;"></i>
                    </button>
                </div>
            `).join('');
            lucide.createIcons();
        } catch(e) { console.error("Failed to load messages:", e); }
    }

    async function addTickerMsg() {
        const input = document.getElementById('newTickerMsg');
        const message = input.value.trim();
        if (!message) return;

        try {
            const res = await fetch('/admin_add_ticker_msg', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message })
            });
            const data = await res.json();
            if (data.success) {
                input.value = '';
                loadTickerMessages();
            } else { alert(data.msg); }
        } catch(e) { alert('Failed to add message'); }
    }

    function deleteTickerMsg(id) {
        openUniversalModal('delete', 'Delete Announcement', 'Are you sure you want to permanently delete this broadcast message? This action cannot be revoked.', 'Delete Now', async () => {
            try {
                const res = await fetch('/admin_delete_ticker_msg', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id })
                });
                const data = await res.json();
                if (data.success) {
                    loadTickerMessages();
                    showToast('Announcement deleted successfully', 'success');
                }
            } catch(e) {
                showToast('Failed to delete announcement', 'error');
            }
        }, true);
    }

    async function handleSoloToggle(is_solo) {
        const soloStatus = document.getElementById('soloStatus');
        soloStatus.textContent = is_solo ? "Active" : "Inactive";
        soloStatus.style.background = is_solo ? "#dcfce7" : "#e2e8f0";
        soloStatus.style.color = is_solo ? "#166534" : "#64748b";
        
        try {
            await fetch('/admin_toggle_ticker_solo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ is_solo })
            });
        } catch(e) {}
    }

    // ─── Load doctors ──────────────────────────────────────────
    async function loadDoctors() {
      try {
        console.log("Fetching doctors list...");
        const res = await fetch('/get_doctors');
        if (!res.ok) throw new Error('API request failed');
        const doctors = await res.json();
        const doctorSelect = document.getElementById('doctorSelect');
        const specSelect   = document.getElementById('specSelect');

        if (!doctorSelect || !specSelect) return;

        doctorSelect.innerHTML = "<option value=''>-- Select Doctor --</option>";
        specSelect.innerHTML   = "<option value=''>-- Select Specialization --</option>";
        doctorsBySpecialization = {};

        if (!doctors || doctors.length === 0) {
          console.warn("API returned empty doctor list");
          const opt = document.createElement('option');
          opt.value = ''; opt.textContent = 'No doctors available';
          if (doctorSelect) doctorSelect.appendChild(opt);
          if (specSelect) specSelect.appendChild(opt.cloneNode(true));
          return;
        }

        console.log(`Populating ${doctors.length} doctors into dropdowns.`);
        const specializations = new Set();
        doctors.forEach(d => {
          try {
            if (!d.Name || !d.Specialization) {
              console.warn("Skipping doctor record with missing Name or Specialization:", d);
              return;
            }
            const opt = document.createElement('option');
            opt.value = d.SheetURL || '';
            opt.textContent = `${d.Name} (${d.Specialization})`;
            
            // Normalize Days
            const daysArr = Array.isArray(d.Days) ? d.Days : (d.Days||'').split(',').map(x=>x.trim()).filter(Boolean);
            opt.dataset.days = daysArr.join(',');
            opt.dataset.dayTimes = JSON.stringify(d.DayTimes || {});
            
            doctorSelect.appendChild(opt);
            
            const spec = d.Specialization.trim();
            specializations.add(spec);
            if (!doctorsBySpecialization[spec]) doctorsBySpecialization[spec] = [];
            doctorsBySpecialization[spec].push({ ...d, Days: daysArr, DayTimes: d.DayTimes || {} });
          } catch (e) {
             console.error("Error processing doctor record:", d, e);
          }
        });

        [...specializations].sort().forEach(spec => {
          const opt = document.createElement('option');
          opt.value = spec; opt.textContent = spec;
          specSelect.appendChild(opt);
        });
        console.log("Dropdowns populated successfully.");
        lucide.createIcons();
      } catch (err) {
        console.error("Error in loadDoctors:", err);
      }
    }

    async function loadDoctorPairs() {
      const res = await fetch('/get_doctor_pairs');
      const pairs = await res.json();
      const select = document.getElementById('deleteDoctorSelect');
      if (!select) return;
      select.innerHTML = "<option value=''>-- Select Doctor --</option>";
      if (!pairs || pairs.length === 0) {
        const opt = document.createElement('option');
        opt.value=''; opt.textContent='No doctors available';
        select.appendChild(opt); return;
      }
      pairs.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p; opt.textContent = p;
        select.appendChild(opt);
      });
    }

    async function loadDoctorsForEdit() {
      const select = document.getElementById('editDoctorSelect');
      const viewSelect = document.getElementById('viewBookingDoctorSelect');
      const leavesSelect = document.getElementById('leaveDoctorSelect');
      const cancelSelect = document.getElementById('cancelBookingDoctorSelect');
      if (!select && !viewSelect && !leavesSelect && !cancelSelect) return;

      const prevSelectVal = select ? select.value : '';
      const prevViewVal = viewSelect ? viewSelect.value : '';
      const prevLeavesVal = leavesSelect ? leavesSelect.value : '';
      const prevCancelVal = cancelSelect ? cancelSelect.value : '';

      const elements = [];
      if (select) elements.push(select);
      if (viewSelect) elements.push(viewSelect);
      if (leavesSelect) elements.push(leavesSelect);
      if (cancelSelect) elements.push(cancelSelect);

      console.log("Loading doctors for admin select...");
      elements.forEach(el => el.innerHTML = "<option value=''>-- Loading doctors... --</option>");

      try {
        const res = await fetch('/get_doctors');
        if (!res.ok) { 
          elements.forEach(el => el.innerHTML="<option value=''>-- Could not load doctors --</option>"); 
          return; 
        }
        const doctors = await res.json();
        elements.forEach(el => el.innerHTML = "<option value=''>-- Select Doctor --</option>");

        if (!doctors || doctors.length === 0) {
          elements.forEach(el => {
            const opt=document.createElement('option'); opt.value=''; opt.textContent='No doctors available';
            el.appendChild(opt);
          });
          return;
        }
        doctors.forEach(d => {
          const value = `${d.Name} - ${d.Specialization}`;
          const opt = document.createElement('option');
          opt.value = value; opt.textContent = value;
          const daysArr = Array.isArray(d.Days) ? d.Days : (d.Days||'').split(',').map(x=>x.trim()).filter(Boolean);
          opt.dataset.days = daysArr.join(',');
          opt.dataset.dayTimes = JSON.stringify(d.DayTimes || {});
          opt.dataset.email = d.Email || '';
          
          if (select) select.appendChild(opt.cloneNode(true));
          if (viewSelect) viewSelect.appendChild(opt.cloneNode(true));
          if (leavesSelect) leavesSelect.appendChild(opt.cloneNode(true));
          if (cancelSelect) cancelSelect.appendChild(opt);
        });

        // Restore values
        if (select && prevSelectVal) { select.value = prevSelectVal; select.dispatchEvent(new Event('change')); }
        if (viewSelect && prevViewVal) { viewSelect.value = prevViewVal; viewSelect.dispatchEvent(new Event('change')); }
        if (leavesSelect && prevLeavesVal) { leavesSelect.value = prevLeavesVal; leavesSelect.dispatchEvent(new Event('change')); }
        if (cancelSelect && prevCancelVal) { cancelSelect.value = prevCancelVal; cancelSelect.dispatchEvent(new Event('change')); }
      } catch(err) {
        elements.forEach(el => el.innerHTML="<option value=''>-- Error loading doctors --</option>");
      }
    }

    function loadDoctorsForView() {
      loadDoctorsForEdit(); // shared logic
    }

    // ─── Doctor select → date ──────────────────────────────────
    document.getElementById('doctorSelect')?.addEventListener('change', function () {
      const selectedOption = this.selectedOptions[0];
      const doctorMsgDiv = document.getElementById('doctorMsg');
      const dateInput    = document.getElementById('doctorDate');
      doctorMsgDiv.style.display='none'; doctorMsgDiv.textContent='';
      if (dateInput) {
        const today = new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'});
        dateInput.setAttribute('min', today);
        dateInput.value = '';
      }
      if (!selectedOption || !selectedOption.value) { workingDays=[]; return; }
      const daysStr = selectedOption.dataset.days;
      workingDays = daysStr ? daysStr.split(',').map(day=>dayMap[day.trim()]) : [];
    });

    document.getElementById('doctorDate')?.addEventListener('change', async function () {
      const doctorMsgDiv = document.getElementById('doctorMsg');
      doctorMsgDiv.style.display='none'; doctorMsgDiv.textContent='';
      const doctorSelect = document.getElementById('doctorSelect');
      const selectedDoctorOption = doctorSelect.selectedOptions[0];
      const dateVal = this.value;

      if (!selectedDoctorOption || !selectedDoctorOption.value) {
        showMessage('doctorMsg','Please select a doctor to view availability.');
        this.value=''; return;
      }

      // 1. Check API first for Global Holiday/Leave Priority
      try {
        const res = await fetch(`/api/check_doctor_availability?sheet_url=${encodeURIComponent(selectedDoctorOption.value)}&date=${encodeURIComponent(dateVal)}`);
        const data = await res.json();

        // ─── ABSOLUTE PRIORITY: Clinic Wide Holiday ───
        if (data.holiday) {
          showMessage('doctorMsg', data.reason);
          this.value = ''; return;
        }

        // 2. Individual Doctor Availability Logic (Server-side result)
        if (data.available === false) {
           showMessage('doctorMsg', data.reason || 'Doctor is not available.');
           this.value = ''; return;
        }

        // 3. Local Validations (Past date, working habits, etc.)
        const daysStr = selectedDoctorOption.dataset.days || '';
        const workingDays = daysStr ? daysStr.split(',').map(day=>dayMap[day.trim()]) : [];
        const selectedDate = new Date(dateVal);
        const selectedDay  = selectedDate.getDay();

        if (!workingDays.includes(selectedDay)) {
          showMessage('doctorMsg','This doctor is not available on the selected date. Please choose another.');
          this.value=''; return;
        }

        const todayStr  = new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'});
        const todayDate = new Date(todayStr);
        if (selectedDate < todayDate) {
          showMessage('doctorMsg','Appointment dates cannot be set in the past. Please select a future date.');
          this.value=''; return;
        }

        if (dateVal === todayStr) {
          const dayTimes = selectedDoctorOption.dataset.dayTimes ? JSON.parse(selectedDoctorOption.dataset.dayTimes) : {};
          const weekdayName = Object.keys(dayMap).find(k=>dayMap[k]===selectedDay);
          const timeRange = dayTimes[weekdayName];
          if (timeRange) {
            const parts = timeRange.split('-');
            const endTimeStr = parts[1] ? parts[1].trim() : null;
            if (endTimeStr) {
              const nowStr = new Date().toLocaleTimeString('en-GB',{hour12:false,hour:'2-digit',minute:'2-digit',timeZone:'Asia/Kolkata'});
              if (timeToMinutes(nowStr) > timeToMinutes(endTimeStr)) {
                showMessage('doctorMsg','Bookings for this doctor have closed for today. Please select another date.');
                this.value=''; return;
              }
            }
          }
        }
      } catch (err) { 
        console.error('Availability check failed:', err);
        showMessage('doctorMsg', 'Error checking availability. Please try again.');
        this.value = '';
      }
    });

    // ─── Edit doctor select ────────────────────────────────────
    document.getElementById('editDoctorSelect')?.addEventListener('change', function () {
      const selected = this.selectedOptions[0];
      if (!selected) return;
      document.querySelectorAll('.day-check-edit').forEach(cb => cb.checked=false);
      document.querySelectorAll('.day-time-edit').forEach(div => div.style.display='none');
      document.getElementById('sameTimeAllEdit').checked=false;
      document.getElementById('commonTimeBlockEdit').style.display='none';
      document.getElementById('commonStartEdit').value='';
      document.getElementById('commonEndEdit').value='';
      const selectedDays = (selected.dataset.days||'').split(',').map(d=>d.trim()).filter(Boolean);
      const dayTimes = selected.dataset.dayTimes ? JSON.parse(selected.dataset.dayTimes) : {};
      document.getElementById('editDocEmail').value = selected.dataset.email || '';
      document.querySelectorAll('.day-check-edit').forEach(cb => {
        const day = cb.dataset.day;
        const timeDiv = document.querySelector(`.day-time-edit[data-day-time-edit="${day}"]`);
        const timeRange = dayTimes[day];
        if (selectedDays.includes(day)) {
          cb.checked=true; timeDiv.style.display='inline-block';
          if (timeRange) {
            const parts = timeRange.split('-');
            const start = (parts[0]||'').trim();
            const end   = (parts[1]||'').trim();
            const si = timeDiv.querySelector('.start-time-edit');
            const ei = timeDiv.querySelector('.end-time-edit');
            if (si) si.value=start;
            if (ei) ei.value=end;
          }
        } else {
          cb.checked=false; timeDiv.style.display='none';
        }
      });
      localStorage.setItem('activeDoctor', this.value); // Save selected doctor
      refreshLeaveListForCurrentDoctor();
    });

    // ─── Day/time UI setup ─────────────────────────────────────
    function setupAddDoctorDayTimeUI() {
      document.querySelectorAll('.day-check').forEach(cb => {
        cb.addEventListener('change', () => {
          const day = cb.dataset.day;
          const timeDiv = document.querySelector(`.day-time[data-day-time="${day}"]`);
          const sameTimeAll = document.getElementById('sameTimeAll');
          if (sameTimeAll && sameTimeAll.checked) { timeDiv.style.display='none'; return; }
          timeDiv.style.display = cb.checked ? 'inline-block' : 'none';
        });
      });
      const staCb = document.getElementById('sameTimeAll');
      const ctb   = document.getElementById('commonTimeBlock');
      if (staCb) {
        staCb.addEventListener('change', () => {
          if (staCb.checked) {
            ctb.style.display='block';
            document.querySelectorAll('.day-time').forEach(d => d.style.display='none');
          } else {
            ctb.style.display='none';
            document.querySelectorAll('.day-check').forEach(cb => {
              const day = cb.dataset.day;
              const td = document.querySelector(`.day-time[data-day-time="${day}"]`);
              td.style.display = cb.checked ? 'inline-block' : 'none';
            });
          }
        });
      }
    }

    function setupEditDoctorDayTimeUI() {
      document.querySelectorAll('.day-check-edit').forEach(cb => {
        cb.addEventListener('change', () => {
          const day = cb.dataset.day;
          const timeDiv = document.querySelector(`.day-time-edit[data-day-time-edit="${day}"]`);
          const sameTimeAll = document.getElementById('sameTimeAllEdit');
          if (sameTimeAll && sameTimeAll.checked) { timeDiv.style.display='none'; return; }
          timeDiv.style.display = cb.checked ? 'inline-block' : 'none';
        });
      });
      const staCb = document.getElementById('sameTimeAllEdit');
      const ctb   = document.getElementById('commonTimeBlockEdit');
      if (staCb) {
        staCb.addEventListener('change', () => {
          if (staCb.checked) {
            ctb.style.display='block';
            document.querySelectorAll('.day-time-edit').forEach(d => d.style.display='none');
          } else {
            ctb.style.display='none';
            document.querySelectorAll('.day-check-edit').forEach(cb => {
              const day = cb.dataset.day;
              const td = document.querySelector(`.day-time-edit[data-day-time-edit="${day}"]`);
              td.style.display = cb.checked ? 'inline-block' : 'none';
            });
          }
        });
      }
    }

    // ─── Input formatting ──────────────────────────────────────
    function capitalizeWords(str) {
      return str.replace(/[^a-zA-Z ]/g,'').replace(/\s+/g,' ').trim()
                .split(' ').map(w=>w.charAt(0).toUpperCase()+w.slice(1).toLowerCase()).join(' ');
    }

    document.getElementById('addDocSpec')?.addEventListener('blur', e => { e.target.value = capitalizeWords(e.target.value); });
    document.getElementById('addDocName')?.addEventListener('blur', e => {
      let name = e.target.value.replace(/^Dr\.?\s*/i,'');
      e.target.value = `Dr. ${capitalizeWords(name)}`;
    });
    document.getElementById('depName')?.addEventListener('blur', e => { e.target.value = capitalizeWords(e.target.value); });
    document.getElementById('docName')?.addEventListener('blur', e => { e.target.value = capitalizeWords(e.target.value); });

    function cleanAgeInput(id) {
      const f = document.getElementById(id);
      if (f) { f.value = f.value.trim().replace(/^0+/,''); if (f.value==='') f.value='0'; }
    }

    // ─── Book by Doctor form ───────────────────────────────────
    document.getElementById('doctorBookingForm')?.addEventListener('submit', async e => {
      e.preventDefault();
      cleanAgeInput('docAge');
      const btn = e.target.querySelector("button[type='submit']");
      btn.disabled=true; btn.innerHTML='<i data-lucide="loader-circle" class="spin-icon"></i> Booking...'; lucide.createIcons();
      try {
        const sheetname    = document.getElementById('doctorSelect').value;
        const name         = document.getElementById('docName').value.trim();
        const age          = document.getElementById('docAge').value.trim();
        const gender       = document.getElementById('docGender').value;
        const phone_number   = document.getElementById('docNumber').value.trim();
        const date         = document.getElementById('doctorDate').value;
        if (!date) { showMessage('doctorMsg','Please select a date.'); btn.disabled=false; btn.innerHTML='<i data-lucide="calendar-check"></i> Book Appointment'; lucide.createIcons(); return; }
        const res  = await fetch('/book_doctor',{ method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({sheetname,name,age,gender,phone_number,date}) });
        const data = await res.json();
        if (data.redirect) { window.location.href = data.redirect; }
        else if (data.success) { showMessage('doctorMsg', 'Appointment scheduled successfully. Token: ' + data.token, 'green'); e.target.reset(); }
        else { showMessage('doctorMsg', data.msg || 'Unable to process booking.'); }
      } catch(err) { showMessage('doctorMsg','Communication error. Please try again.'); }
      finally { btn.disabled=false; btn.innerHTML='<i data-lucide="calendar-check"></i> Book Appointment'; lucide.createIcons(); }
    });

    // ─── Global Holiday Management ──────────────────────────────
    async function loadGlobalHolidays() {
        const upcomingTbody = document.getElementById('upcomingHolidaysTbody');
        const pastTbody = document.getElementById('pastHolidaysTbody');
        if(!upcomingTbody || !pastTbody) return;
        
        upcomingTbody.innerHTML = '<tr><td colspan="3" style="text-align:center; padding:1.5rem; color:#94a3b8;">Loading...</td></tr>';
        pastTbody.innerHTML = '<tr><td colspan="3" style="text-align:center; padding:1.5rem; color:#94a3b8;">Loading...</td></tr>';
        
        try {
            const res = await fetch('/admin_get_holidays');
            const data = await res.json();
            if (data.success) {
                const today = new Date();
                today.setHours(0, 0, 0, 0); // Start of today

                const pastHolidays = [];
                const upcomingHolidays = [];

                data.holidays.forEach(h => {
                    const parts = h.Date.split('-');
                    // Format from backend is YYYY-MM-DD
                    if(parts.length === 3) {
                        const hDate = new Date(parts[0], parts[1] - 1, parts[2]);
                        if (hDate <= today) {
                            pastHolidays.push({ ...h, parsedDate: hDate });
                        } else {
                            upcomingHolidays.push({ ...h, parsedDate: hDate });
                        }
                    } else {
                        // Fallback
                        upcomingHolidays.push({ ...h, parsedDate: new Date(2999, 0, 1) });
                    }
                });

                // Sort: Upcoming (Ascending - closest first), Past (Descending - latest past first)
                upcomingHolidays.sort((a, b) => a.parsedDate - b.parsedDate);
                pastHolidays.sort((a, b) => b.parsedDate - a.parsedDate);

                const emptyRow = (msg) => `<tr><td colspan="3" style="text-align:center; padding:2rem; color:#94a3b8;"><i data-lucide="calendar" style="width:24px;height:24px;display:block;margin:0 auto 0.5rem;opacity:0.4;"></i>${msg}</td></tr>`;

                // Render Upcoming
                if (upcomingHolidays.length === 0) {
                    upcomingTbody.innerHTML = emptyRow('No upcoming holidays scheduled.');
                } else {
                    upcomingTbody.innerHTML = upcomingHolidays.map(h => `
                        <tr style="transition: background 0.2s;" onmouseover="this.style.background='#f8fafc'" onmouseout="this.style.background='transparent'">
                            <td style="padding:0.85rem 1rem; border-bottom:1px solid #f1f5f9; font-weight:700; color:#0f172a;">${h.Date}</td>
                            <td style="padding:0.85rem 1rem; border-bottom:1px solid #f1f5f9; color:#475569;">${h.Reason}</td>
                            <td style="padding:0.85rem 1rem; border-bottom:1px solid #f1f5f9; text-align:right;">
                                <button class="btn btn-outline" style="padding:6px 10px; font-size:0.75rem; color:#ef4444; border-color:rgba(239,68,68,0.2); border-radius:6px; background:white; cursor:pointer; font-weight:600;" onclick="deleteGlobalHoliday('${h.Date}')" onmouseover="this.style.background='#fef2f2'" onmouseout="this.style.background='white'">
                                    <i data-lucide="trash-2" style="width:14px;height:14px;display:inline-block;vertical-align:text-bottom;margin-right:2px;"></i> Remove
                                </button>
                            </td>
                        </tr>
                    `).join('');
                }

                // Render Past
                if (pastHolidays.length === 0) {
                    pastTbody.innerHTML = emptyRow('No past holidays.');
                } else {
                    pastTbody.innerHTML = pastHolidays.map(h => `
                        <tr>
                            <td style="padding:0.85rem 1rem; border-bottom:1px solid #e2e8f0; font-weight:600; color:#64748b;">${h.Date}</td>
                            <td style="padding:0.85rem 1rem; border-bottom:1px solid #e2e8f0; color:#64748b;">${h.Reason}</td>
                            <td style="padding:0.85rem 1rem; border-bottom:1px solid #e2e8f0; text-align:right;">
                                <span style="display:inline-block; padding:4px 8px; background:#e2e8f0; color:#475569; font-size:0.7rem; font-weight:700; border-radius:12px; letter-spacing:0.02em; text-transform:uppercase;">
                                    Completed
                                </span>
                            </td>
                        </tr>
                    `).join('');
                }
                
                lucide.createIcons();
            }
        } catch(e) { console.error("Failed to load holidays", e); }
    }

    window.toggleHolidayRange = function() {
        const isRange = document.getElementById('hUseRange').checked;
        document.getElementById('hRangeRow').style.display = isRange ? 'block' : 'none';
        document.getElementById('startLabel').textContent = isRange ? '(Start)' : '';
    }

    window.addGlobalHoliday = async function() {
        const btn = document.getElementById('addHolidayBtn');
        const date = document.getElementById('hDate').value;
        const endDate = document.getElementById('hEndDate').value;
        const useRange = document.getElementById('hUseRange').checked;
        const reason = document.getElementById('hReason').value.trim();
        
        if (!date) { showToast("Please select a date", "error"); return; }
        if (useRange) {
            if (!endDate) { showToast("Please select an end date", "error"); return; }
            if (new Date(endDate) <= new Date(date)) {
                showToast("End date must be after start date", "error");
                return;
            }
        }
        
        btn.disabled = true;
        btn.innerHTML = '<i data-lucide="loader" class="spin-icon"></i> Adding Holiday...';
        lucide.createIcons();

        try {
            const res = await fetch('/admin_add_holiday', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    date, 
                    endDate: useRange ? endDate : null,
                    reason: reason || 'General Holiday' 
                })
            });
            const data = await res.json();
            if (data.success) {
                showToast(data.msg, "success");
                document.getElementById('hReason').value = "";
                if(useRange) {
                    document.getElementById('hUseRange').checked = false;
                    toggleHolidayRange();
                }
                loadGlobalHolidays();
            } else {
                showToast(data.msg, "error");
            }
        } catch(e) { 
            showToast("Network error", "error"); 
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<i data-lucide="plus-circle"></i> Add Holiday';
            lucide.createIcons();
        }
    }

    window.deleteGlobalHoliday = function(date) {
        openModal(
            'warning',
            'Remove Holiday',
            `Are you sure you want to remove the clinic holiday for <strong>${date}</strong>?`,
            'Remove Holiday',
            async () => {
                try {
                    const res = await fetch('/admin_delete_holiday', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ date })
                    });
                    const data = await res.json();
                    if (data.success) {
                        showToast(`Holiday removed: ${date}`, "success");
                        loadGlobalHolidays();
                        closeModal();
                    } else {
                        showToast(data.msg, "error");
                    }
                } catch(e) { 
                    showToast("Network error", "error"); 
                }
            },
            true // isDanger
        );
    }

    // ─── Book by Dept form ─────────────────────────────────────
    document.getElementById('departmentBookingForm')?.addEventListener('submit', async e => {
      e.preventDefault();
      cleanAgeInput('depAge');
      const btn = e.target.querySelector("button[type='submit']");
      btn.disabled=true; btn.innerHTML='<i data-lucide="loader-circle" class="spin-icon"></i> Booking...'; lucide.createIcons();
      try {
        const specialization = document.getElementById('specSelect').value;
        const name           = document.getElementById('depName').value.trim();
        const age            = document.getElementById('depAge').value.trim();
        const gender         = document.getElementById('depGender').value;
        const phone_number   = document.getElementById('depNumber').value.trim();
        const date           = document.getElementById('specDate').value;
        const messageDiv     = document.getElementById('noDoctorMsg');
        if (!date) { messageDiv.textContent='Please select a date for your appointment.'; messageDiv.style.display='block'; btn.disabled=false; btn.innerHTML='<i data-lucide="calendar-check"></i> Book Appointment'; lucide.createIcons(); return; }
        let doctor_sheet_url=null;
        const timeSelect = document.getElementById('depTime');
        if (timeSelect && timeSelect.value) doctor_sheet_url = timeSelect.value;
        messageDiv.textContent=''; messageDiv.style.display='none';
        const res  = await fetch('/book_department',{ method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({specialization,name,age,gender,phone_number,date,doctor_sheet_url}) });
        const data = await res.json();
        if (data.redirect) { window.location.href = data.redirect; }
        else if (data.success) { showToast('Appointment scheduled successfully', 'success'); e.target.reset(); const w=document.getElementById('depTimeWrapper'); if(w) w.style.display='none'; }
        else { messageDiv.textContent = data.msg || 'Unable to complete appointment. Please retry.'; messageDiv.style.display='block'; }
      } catch(err) { showToast('Administrative system error. Please try again.', 'error'); }
      finally { btn.disabled=false; btn.innerHTML='<i data-lucide="calendar-check"></i> Book Appointment'; lucide.createIcons(); }
    });

    document.getElementById('specDate')?.addEventListener('change', async function() {
      const spec         = document.getElementById('specSelect').value;
      const dateVal      = this.value;
      const messageDiv   = document.getElementById('noDoctorMsg');
      messageDiv.textContent=''; messageDiv.style.display='none';
      updateSpecTimeSelect([],null);

      if (!spec || !doctorsBySpecialization[spec]) {
        showMessage('noDoctorMsg','Please select a Specialization before choosing a date.');
        this.value=''; return;
      }

      // 1. Always check API first for Global Holiday Priority
      try {
        const allUrls = doctorsBySpecialization[spec].map(d => d.SheetURL).filter(Boolean);
        const res = await fetch('/api/check_doctor_availability', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({sheet_urls: allUrls, date: dateVal})
        });
        const data = await res.json();
        const results = data.results || {};

        // ─── ABSOLUTE PRIORITY: Clinic Wide Holiday ───
        if (data.holiday) {
          showMessage('noDoctorMsg', data.reason);
          this.value=''; return;
        }

        // 2. Local Validations (Past date, working day, etc.)
        const selectedDate = new Date(dateVal);
        const selectedDay  = selectedDate.getDay();
        const todayStr  = new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'});
        const todayDate = new Date(todayStr);

        if (selectedDate < todayDate) {
          showMessage('noDoctorMsg','Past dates cannot be selected. Please choose a future date.');
          this.value=''; return;
        }

        const weekdayName = Object.keys(dayMap).find(key=>dayMap[key]===selectedDay);
        let availableDoctors = doctorsBySpecialization[spec].filter(doc => {
          const daysArr = Array.isArray(doc.Days)?doc.Days:(doc.Days||'').split(',').map(d=>d.trim()).filter(Boolean);
          return daysArr.includes(weekdayName);
        });

        if (availableDoctors.length===0) {
          showMessage('noDoctorMsg','No doctors available for this specialization on the selected day.');
          this.value=''; return;
        }

        if (dateVal === todayStr) {
          const nowStr = new Date().toLocaleTimeString('en-GB',{hour12:false,hour:'2-digit',minute:'2-digit',timeZone:'Asia/Kolkata'});
          availableDoctors = availableDoctors.filter(doc => {
            const tr = (doc.DayTimes||{})[weekdayName];
            if (!tr) return false;
            const endTimeStr = (tr.split('-')[1]||'').trim();
            if (!endTimeStr) return false;
            return timeToMinutes(nowStr) <= timeToMinutes(endTimeStr);
          });
          if (availableDoctors.length===0) {
            showMessage('noDoctorMsg','All doctors in this specialization have finished for today. Please select another date.');
            this.value=''; return;
          }
        }

        // 3. Filter by individual leaves from the already fetched results
        availableDoctors = availableDoctors.filter(doc => {
          const status = results[doc.SheetURL];
          return status && status.available !== false;
        });

        if (availableDoctors.length === 0) {
          const firstKey = Object.keys(results)[0];
          const errMsg = (firstKey && results[firstKey] && results[firstKey].reason) 
                         ? results[firstKey].reason 
                         : 'All doctors in this specialization are unavailable for the selected date.';
          showMessage('noDoctorMsg', errMsg);
          this.value = ''; return;
        }

        updateSpecTimeSelect(availableDoctors, weekdayName);

      } catch (err) { 
        console.error('Availability check failed:', err);
        showMessage('noDoctorMsg', 'Error checking availability. Please try again.');
      }
    });

    function updateSpecTimeSelect(doctors, weekdayName) {
      const wrapper = document.getElementById('depTimeWrapper');
      const select  = document.getElementById('depTime');
      if (!wrapper||!select) return;
      select.innerHTML='';
      if (!doctors||doctors.length<=1) { wrapper.style.display='none'; return; }
      wrapper.style.display='block';
      const defaultOpt=document.createElement('option'); defaultOpt.value=''; defaultOpt.textContent='Any available time'; select.appendChild(defaultOpt);
      doctors.forEach(doc=>{
        const timeRange = weekdayName ? (doc.DayTimes||{})[weekdayName]||'': '';
        if (!timeRange) return;
        const opt=document.createElement('option'); opt.value=doc.SheetURL||''; opt.textContent=timeRange; select.appendChild(opt);
      });
    }

    // ─── Admin Booking Helpers ─────────────────────────────────
    async function loadDoctorsForAdminBooking() {
      const select = document.getElementById('adminBookDoc');
      if (!select) return;
      
      // Use existing loadDoctors to ensure data is fresh
      await loadDoctors(); 
      
      const mainSelect = document.getElementById('doctorSelect');
      if (!mainSelect) return;
      
      select.innerHTML = "<option value=''>-- Select Doctor --</option>";
      // Copy options from the main doctor select
      Array.from(mainSelect.options).forEach(opt => {
        if (opt.value) {
          const newOpt = opt.cloneNode(true);
          select.appendChild(newOpt);
        }
      });
      
      // Set min date to today
      const dateInput = document.getElementById('adminBookDate');
      if (dateInput) {
        const today = new Date().toLocaleDateString('en-CA', {timeZone: 'Asia/Kolkata'});
        dateInput.setAttribute('min', today);
      }
    }

    function updateAdminBookDates() {
      const select = document.getElementById('adminBookDoc');
      const dateInput = document.getElementById('adminBookDate');
      const msgDiv = document.getElementById('adminBookDateMsg');
      if (!select || !dateInput) return;
      
      dateInput.value = '';
      if (msgDiv) { msgDiv.style.display = 'none'; msgDiv.textContent = ''; }
      const opt = select.options[select.selectedIndex];
      if (!opt || !opt.value) return;
      
      const today = new Date().toLocaleDateString('en-CA', {timeZone: 'Asia/Kolkata'});
      dateInput.setAttribute('min', today);
    }

    document.getElementById('adminBookDate')?.addEventListener('change', async function() {
      const msgDiv = document.getElementById('adminBookDateMsg');
      if (msgDiv) { msgDiv.style.display = 'none'; msgDiv.textContent = ''; }
      
      const docSelect = document.getElementById('adminBookDoc');
      const selectedOpt = docSelect.selectedOptions[0];
      const date = this.value;
      const sheet_url = selectedOpt ? selectedOpt.value : '';

      if (!sheet_url) {
        showMessage('adminBookDateMsg', 'Please select a doctor first.');
        this.value = '';
        return;
      }
      if (!date) return;

      try {
        const res = await fetch(`/api/check_doctor_availability?sheet_url=${encodeURIComponent(sheet_url)}&date=${encodeURIComponent(date)}`);
        const data = await res.json();
        
        if (data.available === false) {
          showMessage('adminBookDateMsg', data.reason || 'Doctor is not available on this date.', 'red');
          this.value = '';
        } else {
          // New: Fetch current booking count specifically
          const statsRes = await fetch(`/api/get_booking_stats?sheet_url=${encodeURIComponent(sheet_url)}&date=${encodeURIComponent(date)}`);
          const stats = await statsRes.json();
          
          if (stats.success) {
            let statusColor = 'green';
            let statusLabel = 'Available: ';
            
            if (stats.count >= 25) {
                statusColor = 'red';
                statusLabel = 'CAPACITY REACHED: ';
            } else if (stats.count >= 20) {
                statusColor = 'orange';
                statusLabel = 'Limited Space: ';
            }
            
            const msg = `${statusLabel} <strong>${stats.count} / ${stats.total}</strong> bookings filled.`;
            showMessage('adminBookDateMsg', msg, statusColor, 0); // Keep message visible
          }
        }
      } catch (err) {
        console.error('Availability/Stats check failed:', err);
      }
    });

    // ─── Admin Call-in Patient Booking ────────────────────────────────
    let activeAdminBookingPayload = null;

    async function submitAdminBooking(payload, force = false) {
      if (force) payload.force = true;
      
      const btn = document.querySelector("#adminBookPatientForm button[type='submit']");
      const RESET_HTML = '<i data-lucide="check-circle" class="btn-icon"></i> Confirm Booking';
      
      // Force loading state
      btn.disabled = true;
      btn.innerHTML = '<i data-lucide="loader-circle" class="spin-icon"></i> Saving...';
      if (window.lucide) lucide.createIcons();
      
      try {
        const res = await fetch('/admin_book_patient', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload)
        });
        
        const data = await res.json();
        
        if (data.success && data.warning) {
          // IMPORTANT: Reset button BEFORE opening modal 
          // so if they cancel, the button is already back to normal.
          btn.disabled = false;
          btn.innerHTML = RESET_HTML;
          if (window.lucide) lucide.createIcons();

          activeAdminBookingPayload = payload;
          openModal(
            'warning',
            'Full Capacity Message', 
            `<div style="text-align:center;">
               <div style="font-size:1.1rem; color:#0f172a; margin-bottom:0.8rem;">${data.msg}</div>
               <div style="font-size:0.9rem; color:#64748b;">As an administrator, you are allowed to exceed the 25-patient limit for urgent call-ins.</div>
             </div>`, 
            'Save Anyway', 
            () => submitAdminBooking(activeAdminBookingPayload, true),
            false
          );
          return;
        }
        
        if (data.success) {
          openModal(
            'success',
            'Booking Successful',
            `Appointment for <strong>${data.name}</strong> with <strong>${data.doctor}</strong> is confirmed.`,
            'Book Another',
            () => { 
                closeModal(); 
                document.getElementById('adminBookPatientForm').reset();
                // Ensure button is back to normal after modal close
                btn.disabled = false;
                btn.innerHTML = RESET_HTML;
                if (window.lucide) lucide.createIcons();
            }
          );

          // Token Card Injection...
          const tokenCard = document.createElement('div');
          tokenCard.className = 'success-token-card';
          tokenCard.innerHTML = `
            <span style="font-size:0.8rem; color:#64748b; text-transform:uppercase; letter-spacing:1px; font-weight:700;">Token Number</span>
            <span class="success-token-value" style="display:block; font-size:2.8rem; font-weight:900; color:#0f172a; margin:0.4rem 0;">${data.token}</span>
            <span style="font-size:0.82rem; color:#0077b6; font-weight:600;">${data.date} | ${data.time}</span>
          `;
          document.getElementById('unvMessage').after(tokenCard);

          // ── WhatsApp Share Injection for Admin Booking (FULL-WIDTH BELOW BUTTONS) ──
          if (data.phone && data.phone !== "-" && data.phone.trim() !== "") {
            const modal = document.getElementById('universalModal');
            const btnsContainer = modal.querySelector('.modal-btns');
            
            // Create WA Block Button if it doesn't exist
            let waBtn = document.getElementById('adminWhatsAppBtn');
            if (!waBtn) {
              waBtn = document.createElement('button');
              waBtn.id = 'adminWhatsAppBtn';
              // Premium Block Style
              Object.assign(waBtn.style, {
                width: '100%',
                marginTop: '1.2rem',
                background: 'linear-gradient(135deg, #25D366 0%, #128C7E 100%)',
                color: 'white',
                border: 'none',
                borderRadius: '16px',
                padding: '1rem',
                fontSize: '0.95rem',
                fontWeight: '700',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '10px',
                cursor: 'pointer',
                boxShadow: '0 8px 15px rgba(37, 211, 102, 0.3)',
                transition: 'all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275)',
              });

              waBtn.onmouseover = () => { 
                waBtn.style.transform = 'translateY(-2px)';
                waBtn.style.boxShadow = '0 12px 20px rgba(37, 211, 102, 0.4)';
              };
              waBtn.onmouseout = () => { 
                waBtn.style.transform = 'translateY(0)';
                waBtn.style.boxShadow = '0 8px 15px rgba(37, 211, 102, 0.3)';
              };
              
              btnsContainer.after(waBtn);
            }
            
            // WhatsApp Icon (SVG) + Lucide Share Icon
            waBtn.innerHTML = `
              <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" style="flex-shrink:0;">
                <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/>
              </svg>
              <span>Share</span>
              <i data-lucide="share-2" style="width:14px;height:14px;"></i>
            `;
            
            waBtn.onclick = (e) => {
                e.stopPropagation();
                const cleanPhone = data.phone.replace(/\D/g, '');
                const rawMsg = `\u{1F3E5} *PrimeCare Clinic - Booking Confirmed!*\n━━━━━━━━━━━━━━\n\u{1F464} *Patient:* ${data.name}\n\u{1F3AB} *Token:* #${data.token}\n\u{1F468}\u{200D}\u{2695}\u{FE0F} *Doctor:* ${data.doctor}\n\u{1F5D3}\u{FE0F} *Date:* ${data.date}\n\u{23F0} *Time:* ${data.time}\n━━━━━━━━━━━━━━\n\u{1F517} *Track Live:* ${window.location.origin}/live-tracking`;
                const waUrl = `https://wa.me/${cleanPhone}/?text=${encodeURIComponent(rawMsg)}`;
                window.open(waUrl, '_blank');
            };
            if (window.lucide) lucide.createIcons();
          }
          
          btn.disabled = false;
          btn.innerHTML = RESET_HTML;
        } else {
          showToast(data.msg || 'Booking failed', 'error');
          btn.disabled = false;
          btn.innerHTML = RESET_HTML;
        }
      } catch (e) {
        showToast('Communication error', 'error');
        btn.disabled = false;
        btn.innerHTML = RESET_HTML;
      }
      if (window.lucide) lucide.createIcons();
    }

    document.getElementById('adminBookPatientForm')?.addEventListener('submit', async e => {
      e.preventDefault();
      const payload = {
        sheet_url: document.getElementById('adminBookDoc').value,
        date: document.getElementById('adminBookDate').value,
        name: document.getElementById('adminBookName').value.trim(),
        age: document.getElementById('adminBookAge').value.trim(),
        gender: document.getElementById('adminBookGender').value,
        phone_number: document.getElementById('adminBookPhone').value.trim()
      };
      
      submitAdminBooking(payload);
    });

    // ─── Admin login ───────────────────────────────────────────

    document.getElementById('sendOtpBtn')?.addEventListener('click', async () => {
      const btn  = document.getElementById('sendOtpBtn');
      const email = document.getElementById('adminEmail')?.value?.trim();
      if (!email) { showToast('Please enter your admin email address.', 'error'); return; }
      btn.disabled = true;
      btn.innerHTML = '<i data-lucide="loader-circle" class="spin-icon"></i> Sending...'; lucide.createIcons();
      try {
        const res  = await fetch('/send_admin_otp',{ method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:`admin_email=${encodeURIComponent(email)}` });
        const data = await res.json();
        if (data.success) {
          showToast('OTP sent successfully', 'success');
          document.getElementById('otpSection').style.display = 'block';
          btn.style.display = 'none';
        } else {
          showToast(data.msg || 'Could not send OTP. Please try again.', 'error');
          btn.disabled = false;
          btn.innerHTML = '<i data-lucide="send-horizontal"></i> Send OTP'; lucide.createIcons();
        }
      } catch(err) {
        showToast('Communication error. Please check your connection.', 'error');
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="send-horizontal"></i> Send OTP'; lucide.createIcons();
      }
    });

    document.getElementById('adminLoginForm')?.addEventListener('submit', async(e) => {
      e.preventDefault();
      const btn = e.target.querySelector("button[type='submit']");
      const otp = document.getElementById('adminOtp').value.trim();
      if (!otp || otp.length < 4) { showToast('Please enter the OTP sent to your email.', 'error'); return; }
      btn.disabled = true;
      btn.innerHTML = '<i data-lucide="loader-circle" class="spin-icon"></i> Verifying...'; lucide.createIcons();
      try {
        const res  = await fetch('/verify_admin_otp',{ method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:`otp=${encodeURIComponent(otp)}` });
        const data = await res.json();
        if (data.success) {
          showToast('OTP verified successfully', 'success');
          setTimeout(() => {
            document.getElementById('adminLoginDiv').style.display = 'none';
            document.getElementById('adminOptions').style.display = 'block';
            
            // Immediate visibility of Logout in header
            const headerLogoutBtn = document.getElementById('headerLogoutBtn');
            if (headerLogoutBtn) headerLogoutBtn.style.display = 'inline-flex';

            loadDoctorPairs();
            loadDoctors();      // Refresh main dropdowns
            loadDoctorsForEdit(); // Refresh edit dropdown
            loadDoctorsForAdminBooking(); // Refresh admin booking dropdown
            lucide.createIcons();
          }, 600);
        } else {
          showToast(data.msg || 'Authentication failed. Please verify the code.', 'error');
          btn.disabled = false;
          btn.innerHTML = '<i data-lucide="log-in"></i> Login'; lucide.createIcons();
        }
      } catch(err) {
        showToast('System error during login. Please retry.', 'error');
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="log-in"></i> Login'; lucide.createIcons();
      }
    });



    window.handleConfirm = function() {
      console.log("Modal confirmation triggered");
      if (activeModalAction) activeModalAction();
      closeModal();
    }

    window.openLogoutModal = function() {
      console.log("Logout modal requested");
      openModal('universalModal', 'Confirm Logout', 'Are you sure you want to end your current administrative session?', 'Logout', confirmLogout, true);
    }
    async function confirmLogout() {
      console.log("Initiating logout...");
      try {
        const res = await fetch('/admin_logout', { method: 'POST' });
        if (!res.ok) throw new Error('Logout failed on server');
        let data = { success: true };
        try { data = await res.json(); } catch(e) {}
        
        if (data.success) {
          // Reset Admin UI State
          if(document.getElementById('adminOptions')) document.getElementById('adminOptions').style.display = 'none';
          if(document.getElementById('adminActiveSessionDiv')) document.getElementById('adminActiveSessionDiv').style.display = 'none';
          if(document.getElementById('adminLoginFormDiv')) document.getElementById('adminLoginFormDiv').style.display = 'block';
          if(document.getElementById('adminLoginDiv')) document.getElementById('adminLoginDiv').style.display = 'block';
          if(document.getElementById('admin')) document.getElementById('admin').classList.remove('logged-in');
          const loginFooter = document.getElementById('adminLoginFooter');
          if (loginFooter) {
            loginFooter.style.display = 'block';
          }
          // 🔥 HARD RESET all admin panels
          document.querySelectorAll('.adminPanel').forEach(p => {
            p.style.display = 'none';
            p.classList.remove('active');
          });
          lastActiveAdminPanel = null;
          localStorage.removeItem('activeAdminPanel');
          if(document.getElementById('adminEmail')) document.getElementById('adminEmail').value = '';
          if(document.getElementById('adminOtp')) document.getElementById('adminOtp').value = '';
          if(document.getElementById('otpSection')) document.getElementById('otpSection').style.display = 'none';
          
          const sendBtn = document.getElementById('sendOtpBtn');
          if (sendBtn) {
            sendBtn.style.display = 'inline-flex';
            sendBtn.disabled = false;
            sendBtn.innerHTML = '<i data-lucide="send-horizontal"></i> Send OTP';
          }
          
          const loginBtn = document.querySelector('#adminLoginForm button[type="submit"]');
          if (loginBtn) {
            loginBtn.disabled = false;
            loginBtn.innerHTML = '<i data-lucide="log-in"></i> Login';
          }
          
          const headerLogoutBtn = document.getElementById('headerLogoutBtn');
          if (headerLogoutBtn) headerLogoutBtn.style.display = 'none';

          // Clear UI State Persistence
          localStorage.removeItem('activeSection');
          localStorage.removeItem('activeAdminPanel');
          localStorage.removeItem('activeDoctor');

          showToast('Logged out successfully', 'success');
          lucide.createIcons();
        } else {
          showToast('Logout notification failed.', 'error');
        }
      } catch(err) {
        console.error("Logout error:", err);
        showToast('Operation failed. Please check your connection.', 'error');
      } finally {
        closeModal();
      }
    }

    // document.getElementById('logoutBtn').addEventListener('click', openLogoutModal); // Fixed: logoutBtn id does not exist, use headerLogoutBtn instead (already handled in DOMContentLoaded)

    // ─── Add doctor ────────────────────────────────────────────
    document.getElementById('adminAddDoctorForm')?.addEventListener('submit', async e => {
      e.preventDefault();
      const btn=e.target.querySelector("button[type='submit']");
      btn.disabled=true; btn.innerHTML='<i data-lucide="loader-circle" class="spin-icon"></i> Adding...'; lucide.createIcons();
      try {
        const name           = document.getElementById('addDocName').value.trim();
        const specialization = document.getElementById('addDocSpec').value.trim();
        const email          = document.getElementById('addDocEmail').value.trim();

        if (!email.includes('@') || !email.includes('.')) {
          showToast('Please provide a valid email format.', 'error');
          btn.disabled = false; btn.innerHTML = '<i data-lucide="plus-circle"></i> Add Doctor'; lucide.createIcons();
          return;
        }

        const imageFile      = document.getElementById('addDocImage').files[0];
        const sameTimeAll    = document.getElementById('sameTimeAll');
        const selectedDays=[]; const dayTimes={};
        if (sameTimeAll && sameTimeAll.checked) {
          const cs=document.getElementById('commonStart').value;
          const ce=document.getElementById('commonEnd').value;
          if (!cs||!ce) { showToast('Please enter start and end times for the common slot.', 'error'); btn.disabled=false; btn.innerHTML='<i data-lucide="plus-circle"></i> Add Doctor'; lucide.createIcons(); return; }
          document.querySelectorAll('.day-check:checked').forEach(cb=>{ const day=cb.dataset.day; selectedDays.push(day); dayTimes[day]=`${cs}-${ce}`; });
        } else {
          let hasError=false;
          document.querySelectorAll('.day-check:checked').forEach(cb=>{
            if(hasError)return;
            const day=cb.dataset.day;
            const container=document.querySelector(`.day-time[data-day-time="${day}"]`);
            const s=container.querySelector('.start-time').value;
            const en=container.querySelector('.end-time').value;
            if(!s||!en) { showToast(`Please enter start and end times for ${day}.`, 'error'); hasError=true; return; }
            selectedDays.push(day); dayTimes[day]=`${s}-${en}`;
          });
          if(hasError) { btn.disabled=false; btn.innerHTML='<i data-lucide="plus-circle"></i> Add Doctor'; lucide.createIcons(); return; }
        }
        if(!selectedDays.length) { showToast('Please select at least one working day.', 'error'); btn.disabled=false; btn.innerHTML='<i data-lucide="plus-circle"></i> Add Doctor'; lucide.createIcons(); return; }
        const formData=new FormData();
        formData.append('name',name); formData.append('email',email); formData.append('specialization',specialization);
        formData.append('days',selectedDays.join(', ')); formData.append('day_times',JSON.stringify(dayTimes));
        if(imageFile) formData.append('image',imageFile);
        const res=await fetch('/admin_add_doctor',{method:'POST',body:formData});
        const rawText=await res.text(); let data;
        try { data=JSON.parse(rawText); } catch(pe) { showToast('Response parsing error: '+rawText, 'error'); return; }
        showToast(data.msg || 'Doctor profile created successfully', data.success ? 'success' : 'error');
        if(data.success) { e.target.reset(); loadDoctors(); loadDoctorPairs(); loadDoctorsForEdit(); }
      } catch(err) { showToast('Unable to complete doctor registration.', 'error'); }
      finally { btn.disabled=false; btn.innerHTML='<i data-lucide="plus-circle"></i> Add Doctor'; lucide.createIcons(); }
    });

    // ─── Edit doctor ───────────────────────────────────────────
    document.getElementById('adminEditDoctorForm')?.addEventListener('submit', async e => {
      e.preventDefault();
      const btn=e.target.querySelector("button[type='submit']");
      btn.disabled=true; btn.innerHTML='<i data-lucide="loader-circle" class="spin-icon"></i> Saving...'; lucide.createIcons();
      try {
        const combined=document.getElementById('editDoctorSelect').value;
        const email=document.getElementById('editDocEmail').value.trim();
        if(!combined) { showToast('Please select a doctor to edit.', 'error'); btn.disabled=false; btn.innerHTML='<i data-lucide="save"></i> Update Doctor'; lucide.createIcons(); return; }
        if(!email) { showToast('Email is required.', 'error'); btn.disabled=false; btn.innerHTML='<i data-lucide="save"></i> Update Doctor'; lucide.createIcons(); return; }
        if(!email.includes('@') || !email.includes('.')) { showToast('Please provide a valid email format', 'error'); btn.disabled=false; btn.innerHTML='<i data-lucide="save"></i> Update Doctor'; lucide.createIcons(); return; }
        const sameTimeAll=document.getElementById('sameTimeAllEdit');
        const selectedDays=[]; const day_times={};
        if(sameTimeAll&&sameTimeAll.checked) {
          const cs=document.getElementById('commonStartEdit').value;
          const ce=document.getElementById('commonEndEdit').value;
          if(!cs||!ce) { showToast('Please enter start and end times for the common slot.', 'error'); btn.disabled=false; btn.innerHTML='<i data-lucide="save"></i> Update Doctor'; lucide.createIcons(); return; }
          document.querySelectorAll('.day-check-edit:checked').forEach(cb=>{ const day=cb.dataset.day; selectedDays.push(day); day_times[day]=`${cs}-${ce}`; });
        } else {
          let hasError=false;
          document.querySelectorAll('.day-check-edit:checked').forEach(cb=>{
            if(hasError)return;
            const day=cb.dataset.day;
            const timeDiv=document.querySelector(`.day-time-edit[data-day-time-edit="${day}"]`);
            const s=timeDiv.querySelector('.start-time-edit').value;
            const en=timeDiv.querySelector('.end-time-edit').value;
            if(!s||!en){ showToast(`Please enter start and end times for ${day}.`, 'error'); hasError=true; return; }
            selectedDays.push(day); day_times[day]=`${s}-${en}`;
          });
          if(hasError){ btn.disabled=false; btn.innerHTML='<i data-lucide="save"></i> Update Doctor'; lucide.createIcons(); return; }
        }
        if(!selectedDays.length){ showToast('At least one working day is required.', 'error'); btn.disabled=false; btn.innerHTML='<i data-lucide="save"></i> Update Doctor'; lucide.createIcons(); return; }
        const res=await fetch('/admin_edit_doctor',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({combined, email, days:selectedDays,day_times}) });
        const data=await res.json();
        showToast(data.msg, data.success ? 'success' : 'error');
        if(data.success){ e.target.reset(); loadDoctors(); loadDoctorPairs(); loadDoctorsForEdit(); }
      } catch(err){ showToast('An error occurred while updating the doctor profile.', 'error'); }
      finally{ btn.disabled=false; btn.innerHTML='<i data-lucide="save"></i> Update Doctor'; lucide.createIcons(); }
    });

    // ─── Delete doctor ─────────────────────────────────────────
    document.getElementById('adminDeleteDoctorForm')?.addEventListener('submit', async e => {
      e.preventDefault();
      const btn=e.target.querySelector("button[type='submit']");
      btn.disabled=true; btn.innerHTML='<i data-lucide="loader-circle" class="spin-icon"></i> Deleting...'; lucide.createIcons();
      try {
        const combined=document.getElementById('deleteDoctorSelect').value;
        if(!combined){ showToast('Select a doctor to remove from the system.', 'error'); btn.disabled=false; btn.innerHTML='<i data-lucide="trash-2"></i> Delete Doctor'; lucide.createIcons(); return; }
        
        openModal(
          'universalModal',
          'Confirm Removal',
          'Are you sure you want to permanently remove this doctor from the registry?',
          'Delete',
          async () => {
            btn.disabled=true; btn.innerHTML='<i data-lucide="loader-circle" class="spin-icon"></i> Deleting...'; lucide.createIcons();
            try {
              const res=await fetch('/admin_delete_doctor',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({combined}) });
              const data=await res.json();
              showToast(data.msg, data.success ? 'success' : 'error');
              if(data.success){ loadDoctors(); loadDoctorPairs(); loadDoctorsForEdit(); }
            } catch(err){ showToast('An error occurred during doctor removal.', 'error'); }
            finally{ btn.disabled=false; btn.innerHTML='<i data-lucide="trash-2"></i> Delete Doctor'; lucide.createIcons(); }
          },
          true
        );
      } catch(err){ showToast('An error occurred during doctor removal.', 'error'); }
      finally{ btn.disabled=false; btn.innerHTML='<i data-lucide="trash-2"></i> Delete Doctor'; lucide.createIcons(); }
    });

    // ─── Leave management ──────────────────────────────────────
    async function refreshLeaveListForCurrentDoctor() {
      const combined=document.getElementById('leaveDoctorSelect').value;
      const tbody=document.getElementById('leaveTbody');
      const msgDiv=document.getElementById('leaveMsg');
      if(!tbody||!msgDiv)return;
      tbody.innerHTML=''; msgDiv.style.display='none'; msgDiv.textContent='';
      
      if(!combined) {
        tbody.innerHTML=`
          <tr>
            <td colspan="3">
              <div style="text-align:center; padding:1.5rem; color:#94a3b8; display:flex; flex-direction:column; align-items:center; gap:0.5rem;">
                <i data-lucide="user-round" style="width:24px; height:24px; color:#cbd5e1;"></i>
                <span>Please select a doctor to manage their leaves.</span>
              </div>
            </td>
          </tr>
        `;
        lucide.createIcons(tbody);
        return;
      }

      // Show loading spinner
      tbody.innerHTML=`
        <tr>
          <td colspan="3">
            <div style="text-align:center; padding:1.5rem; color:#64748b; display:flex; align-items:center; justify-content:center; gap:0.5rem;">
              <i data-lucide="loader-circle" class="spin-icon" style="width:18px;height:18px;color:#0077b6;margin-right:8px;vertical-align:middle;"></i>
              <span>Loading leaves...</span>
            </div>
          </td>
        </tr>
      `;
      lucide.createIcons(tbody);

      try {
        const res=await fetch(`/admin_get_leaves?combined=${encodeURIComponent(combined)}`);
        const data=await res.json();
        tbody.innerHTML=''; // clear loading
        if(!data.success){ showMessage('leaveMsg',data.msg||'Error loading leave dates.'); return; }
        const leaves=data.leaves||[];
        if(leaves.length===0){
          const tr=document.createElement('tr');
          const td=document.createElement('td');
          td.colSpan=3;
          td.innerHTML=`
            <div style="text-align:center; padding:1.5rem; color:#94a3b8; display:flex; flex-direction:column; align-items:center; gap:0.5rem;">
              <i data-lucide="calendar" style="width:24px; height:24px; color:#cbd5e1;"></i>
              <span>No temporary leave dates scheduled for this doctor.</span>
            </div>
          `;
          tr.appendChild(td); tbody.appendChild(tr);
          lucide.createIcons(tbody);
          return;
        }
        leaves.forEach(leave=>{
          const tr=document.createElement('tr');
          const tdDate=document.createElement('td'); tdDate.textContent=leave.date; tr.appendChild(tdDate);
          const tdReason=document.createElement('td'); tdReason.textContent=leave.reason||'-'; tr.appendChild(tdReason);
          const tdAction=document.createElement('td');
          const btn=document.createElement('button');
          btn.innerHTML='<i data-lucide="trash-2"></i> Remove';
          btn.style.cssText='font-size:0.78rem;margin:0;padding:0.28rem 0.7rem;background:linear-gradient(135deg,#ef4444,#dc2626);box-shadow:none;';
          btn.addEventListener('click', ()=>{
            openModal(
              'universalModal',
              'Remove Leave',
              `Are you sure you want to remove the leave entry for ${leave.date}?`,
              'Remove',
              async () => {
                try {
                  const delRes=await fetch('/admin_delete_leave',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({combined,date:leave.date}) });
                  const delData=await delRes.json();
                  if (delData.success) {
                    showToast(`Leave for ${leave.date} has been removed.`, 'success');
                    refreshLeaveListForCurrentDoctor();
                  } else {
                    showToast(delData.msg || 'Error removing leave.', 'error');
                  }
                } catch(err){ showToast("Couldn't remove leave entry. Please try again.", 'error'); }
              },
              true
            );
          });
          lucide.createIcons(btn);
          tdAction.appendChild(btn); tr.appendChild(tdAction);
          tbody.appendChild(tr);
        });
        lucide.createIcons();
      } catch(err){ 
        tbody.innerHTML='';
        showMessage('leaveMsg',"Couldn't load leave dates. Please try again."); 
      }
    }

    document.getElementById('leaveDoctorSelect')?.addEventListener('change', function () {
      localStorage.setItem('activeDoctor', this.value); // Keep in sync with other panels
      refreshLeaveListForCurrentDoctor();
    });

    document.getElementById('addLeaveBtn')?.addEventListener('click', async()=>{
      const combined=document.getElementById('leaveDoctorSelect').value;
      const date=document.getElementById('leaveDate').value;
      const reason=document.getElementById('leaveReason').value.trim();
      if(!combined){ showMessage('leaveMsg','Please select a doctor before adding leave.'); return; }
      if(!date){ showMessage('leaveMsg','Please select a date for leave.'); return; }
      const todayStr=new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'});
      if(date<todayStr){ showMessage('leaveMsg','Leave cannot be set for past dates.'); return; }
      const btn = document.getElementById('addLeaveBtn');
      btn.disabled = true;
      btn.innerHTML = '<i data-lucide="loader-circle" class="spin-icon"></i> Adding...';
      lucide.createIcons();

      try {
        const res=await fetch('/admin_add_leave',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({combined,date,reason}) });
        const data=await res.json();
        if (data.success) {
          const docOnly = combined.split(' - ')[0];
          showToast(`Leave added for ${docOnly} on ${date}.`, 'success');
          document.getElementById('leaveDate').value=''; 
          document.getElementById('leaveReason').value=''; 
          refreshLeaveListForCurrentDoctor(); 
        } else {
          showToast(data.msg || 'Error adding leave.', 'error');
        }
      } catch(err){ showToast("Couldn't add leave. Please try again.", 'error'); }
      finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="plus-circle"></i> Add Leave';
        lucide.createIcons();
      }
    });

    // ─── Booking Viewer logic ──────────────────────────────────
    document.getElementById('fetchBookingsBtn')?.addEventListener('click', async () => {
      const combined = document.getElementById('viewBookingDoctorSelect').value;
      const date = document.getElementById('viewBookingDate').value;
      const btn = document.getElementById('fetchBookingsBtn');
      const tableWrapper = document.getElementById('bookingTableWrapper');
      const tbody = document.getElementById('bookingsTbody');
      const msgDiv = document.getElementById('viewBookingMsg');
      const countBadge = document.getElementById('bookingCountBadge');
      const countNum = document.getElementById('bookingCountNum');

      if (!combined || !date) {
        showToast('Please select both doctor and date.', 'error');
        return;
      }

      btn.disabled = true;
      btn.innerHTML = '<i data-lucide="loader-circle" class="spin-icon"></i> Fetching...';
      msgDiv.style.display = 'none';
      tableWrapper.style.display = 'none';
      countBadge.style.display = 'none';
      lucide.createIcons();

      try {
        const res = await fetch('/manage_bookings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ combined, date })
        });
        const data = await res.json();

        if (!data.success) {
          msgDiv.textContent = data.msg || 'Error fetching bookings.';
          msgDiv.style.display = 'block';
          return;
        }

        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="calendar-days"></i> Fetch Bookings';
        lucide.createIcons();

        const bookings = data.bookings || [];
        if (bookings.length === 0) {
          msgDiv.textContent = `No bookings found for this doctor on ${date}.`;
          msgDiv.style.display = 'block';
          return;
        }

        tbody.innerHTML = '';
        bookings.forEach(b => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><span class="token-badge">#${b.token}</span></td>
            <td><div style="font-weight:700; color:#0f172a;">${b.name}</div></td>
            <td>${b.age || '-'}</td>
            <td><span style="display:flex; align-items:center; gap:0.4rem;">
                <i data-lucide="${b.gender==='Male'?'mars':(b.gender==='Female'?'venus':'circle-user')}" style="width:14px;height:14px;opacity:0.7;"></i>
                ${b.gender || '-'}
            </span></td>
            <td><span style="font-family:monospace; font-size:0.95rem; color:#0077b6;">${b.phone || '-'}</span></td>
            <td class="no-print">
                <button onclick="deleteBooking(${b.token},'${b.name}')" class="btn btn-danger" style="padding: 0.35rem 0.7rem; font-size: 0.75rem; margin: 0; box-shadow:none;">
                    <i data-lucide="trash-2"></i> 
                </button>
            </td>
          `;
          tbody.appendChild(tr);
        });

        tableWrapper.style.display = 'block';
        countNum.textContent = data.count;
        countBadge.style.display = 'inline-flex';
        lucide.createIcons();

      } catch (err) {
        showToast('Failed to fetch bookings. Connection error.', 'error');
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="search"></i> Show Bookings';
        lucide.createIcons();
      }
    });

    window.prepareAndPrint = function() {
      const combined = document.getElementById('viewBookingDoctorSelect').value;
      const date = document.getElementById('viewBookingDate').value;
      const tbody = document.getElementById('bookingsTbody');
      
      if (!combined || !date || !tbody || tbody.rows.length === 0) {
        showToast('No booking data available to print.', 'error');
        return;
      }

      const [docName, docSpec] = combined.split(' - ');
      const totalBookings = tbody.rows.length;

      // Populate Template
      document.getElementById('printDocName').textContent = docName;
      document.getElementById('printDate').textContent = date;
      document.getElementById('printSpec').textContent = docSpec;
      document.getElementById('printTotal').textContent = totalBookings;
      document.getElementById('printTimestampMeta').textContent = `Generated: ${new Date().toLocaleString()}`;

      const printTbody = document.getElementById('printTableBody');
      printTbody.innerHTML = '';

      // Clone table rows (extracting data only)
      Array.from(tbody.rows).forEach(row => {
          const token = row.cells[0].textContent.trim();
          const name = row.cells[1].textContent.trim();
          const age = row.cells[2].textContent.trim();
          const gender = row.cells[3].textContent.trim();
          const phone = row.cells[4].textContent.trim();

          const tr = document.createElement('tr');
          tr.innerHTML = `
              <td class="print-token-cell">${token}</td>
              <td style="font-weight: 700; color: #0f172a;">${name}</td>
              <td>${age}</td>
              <td>${gender}</td>
              <td style="font-family: monospace; font-size: 13px;">${phone}</td>
          `;
          printTbody.appendChild(tr);
      });

      // Set Document Title for PDF Filename
      const originalTitle = document.title;
      document.title = `${docName.replace(/\./g, '')}_${date}`;

      window.print();

      // Restore
      document.title = originalTitle;
    };
    window.deleteBooking = async function(token, patientName) {
      const combined = document.getElementById('viewBookingDoctorSelect').value;
      const date = document.getElementById('viewBookingDate').value;
      
      openModal(
        'universalModal',
        'Cancel Booking',
        `Are you sure you want to cancel the booking for ${patientName} (Token #${token})?`,
        'Cancel Booking',
        async () => {
          try {
            const res = await fetch('/admin_delete_booking', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ combined, date, token })
            });
            const data = await res.json();
            if (data.success) {
              showToast('Booking cancelled successfully.', 'success');
              document.getElementById('fetchBookingsBtn').click(); // Refresh list
            } else {
              showToast(data.msg || 'Error cancelling booking.', 'error');
            }
          } catch (err) {
            showToast('Communication error. Please retry.', 'error');
          }
        },
        true
      );
      
      // Inject the correct token card for confirmation
      const modal = document.getElementById('universalModal');
      const contentP = modal.querySelector('p');
      const tokenCard = document.createElement('div');
      tokenCard.className = 'success-token-card';
      tokenCard.style.borderColor = '#e11d48'; // Red for cancellation
      tokenCard.innerHTML = `
        <span style="font-size:0.8rem; color:#64748b; text-transform:uppercase; letter-spacing:1px; font-weight:700;">Token Number</span>
        <span class="success-token-value" style="color:#e11d48;">${token}</span>
        <span style="font-size:0.82rem; color:#64748b; font-weight:600;">${date} | ${patientName}</span>
      `;
      contentP.after(tokenCard);
    }

    // ─── Cancel Booking Logic ──────────────────────────────────
    let currentCancelBookings = [];

    window.fetchCancelBookingsList = async function() {
      const combined = document.getElementById('cancelBookingDoctorSelect').value;
      const date = document.getElementById('cancelBookingDate').value;
      const tokenSelect = document.getElementById('cancelTokenSelect');
      const tokenInput = document.getElementById('cancelTokenInput');
      const statusDiv = document.getElementById('cancelBookingStatusDiv');
      const detailCard = document.getElementById('cancelBookingDetailCard');

      if (!tokenSelect || !tokenInput || !statusDiv || !detailCard) return;

      // Reset fields
      tokenSelect.innerHTML = '<option value="">-- Select Token --</option>';
      tokenInput.value = '';
      detailCard.style.display = 'none';
      currentCancelBookings = [];

      if (!combined || !date) {
        statusDiv.innerHTML = `
          <div style="text-align:center; padding:1.5rem; color:#94a3b8; display:flex; flex-direction:column; align-items:center; gap:0.5rem; border: 1px dashed rgba(0,0,0,0.1); border-radius: 12px; background: #fafafa;">
            <i data-lucide="info" style="width:24px; height:24px; color:#cbd5e1;"></i>
            <span>Please select both a doctor and date to search bookings.</span>
          </div>
        `;
        lucide.createIcons(statusDiv);
        return;
      }

      if (combined) {
        localStorage.setItem('activeDoctor', combined);
      }

      // Show loading spinner
      statusDiv.innerHTML = `
        <div style="text-align:center; padding:1.5rem; color:#64748b; display:flex; align-items:center; justify-content:center; gap:0.5rem; border: 1px dashed rgba(0,0,0,0.1); border-radius: 12px; background: #fafafa;">
          <i data-lucide="loader-circle" class="spin-icon" style="width:18px;height:18px;color:#0077b6;margin-right:8px;"></i>
          <span>Fetching bookings for ${date}...</span>
        </div>
      `;
      lucide.createIcons(statusDiv);

      try {
        const res = await fetch('/manage_bookings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ combined, date })
        });
        const data = await res.json();

        if (!data.success) {
          statusDiv.innerHTML = `
            <div style="text-align:center; padding:1.5rem; color:#ef4444; display:flex; flex-direction:column; align-items:center; gap:0.5rem; border: 1px dashed rgba(239,68,68,0.2); border-radius: 12px; background: #fef2f2;">
              <i data-lucide="alert-triangle" style="width:24px; height:24px;"></i>
              <span>${data.msg || 'Error loading bookings.'}</span>
            </div>
          `;
          lucide.createIcons(statusDiv);
          return;
        }

        const bookings = data.bookings || [];
        currentCancelBookings = bookings;

        if (bookings.length === 0) {
          statusDiv.innerHTML = `
            <div style="text-align:center; padding:1.5rem; color:#94a3b8; display:flex; flex-direction:column; align-items:center; gap:0.5rem; border: 1px dashed rgba(0,0,0,0.1); border-radius: 12px; background: #fafafa;">
              <i data-lucide="calendar" style="width:24px; height:24px; color:#cbd5e1;"></i>
              <span>No active bookings found for this doctor on ${date}.</span>
            </div>
          `;
          lucide.createIcons(statusDiv);
          return;
        }

        // Populate dropdown
        bookings.forEach(b => {
          const opt = document.createElement('option');
          opt.value = b.token;
          opt.textContent = `Token #${b.token} - ${b.name}`;
          tokenSelect.appendChild(opt);
        });

        statusDiv.innerHTML = `
          <div style="text-align:center; padding:1rem; color:#1e293b; display:flex; align-items:center; justify-content:center; gap:0.5rem; border: 1px solid rgba(0,119,182,0.15); border-radius: 12px; background: rgba(0,119,182,0.02); font-size:0.85rem; font-weight: 500;">
            <i data-lucide="check-circle" style="width:16px; height:16px; color:#0077b6;"></i>
            <span>Found ${bookings.length} active bookings. Select a token below.</span>
          </div>
        `;
        lucide.createIcons(statusDiv);

      } catch (err) {
        statusDiv.innerHTML = `
          <div style="text-align:center; padding:1.5rem; color:#ef4444; display:flex; flex-direction:column; align-items:center; gap:0.5rem; border: 1px dashed rgba(239,68,68,0.2); border-radius: 12px; background: #fef2f2;">
            <i data-lucide="wifi-off" style="width:24px; height:24px;"></i>
            <span>Network error. Please try again.</span>
          </div>
        `;
        lucide.createIcons(statusDiv);
      }
    };

    window.syncCancelTokenInput = function(val) {
      const tokenInput = document.getElementById('cancelTokenInput');
      if (tokenInput) {
        tokenInput.value = val;
      }
      loadCancelBookingDetails();
    };

    window.syncCancelTokenSelect = function(val) {
      const tokenSelect = document.getElementById('cancelTokenSelect');
      if (tokenSelect) {
        // Find if this token is one of the options
        let found = false;
        for (let i = 0; i < tokenSelect.options.length; i++) {
          if (String(tokenSelect.options[i].value) === String(val)) {
            tokenSelect.selectedIndex = i;
            found = true;
            break;
          }
        }
        if (!found) {
          tokenSelect.value = '';
        }
      }
      loadCancelBookingDetails();
    };

    window.loadCancelBookingDetails = function() {
      const tokenInput = document.getElementById('cancelTokenInput');
      const token = tokenInput ? tokenInput.value.trim() : '';
      const detailCard = document.getElementById('cancelBookingDetailCard');
      const statusDiv = document.getElementById('cancelBookingStatusDiv');
      const doctorSelect = document.getElementById('cancelBookingDoctorSelect');
      const dateInput = document.getElementById('cancelBookingDate');

      if (!detailCard || !statusDiv || !doctorSelect || !dateInput) return;

      if (!token) {
        detailCard.style.display = 'none';
        statusDiv.style.display = 'block';
        // Restore summary message if bookings list exists
        if (currentCancelBookings.length > 0) {
          statusDiv.innerHTML = `
            <div style="text-align:center; padding:1rem; color:#1e293b; display:flex; align-items:center; justify-content:center; gap:0.5rem; border: 1px solid rgba(0,119,182,0.15); border-radius: 12px; background: rgba(0,119,182,0.02); font-size:0.85rem; font-weight: 500;">
              <i data-lucide="check-circle" style="width:16px; height:16px; color:#0077b6;"></i>
              <span>Found ${currentCancelBookings.length} active bookings. Select a token below.</span>
            </div>
          `;
          lucide.createIcons(statusDiv);
        } else {
          statusDiv.innerHTML = `
            <div style="text-align:center; padding:1.5rem; color:#94a3b8; display:flex; flex-direction:column; align-items:center; gap:0.5rem; border: 1px dashed rgba(0,0,0,0.1); border-radius: 12px; background: #fafafa;">
              <i data-lucide="info" style="width:24px; height:24px; color:#cbd5e1;"></i>
              <span>Please select both a doctor and date to search bookings.</span>
            </div>
          `;
          lucide.createIcons(statusDiv);
        }
        return;
      }

      const booking = currentCancelBookings.find(b => String(b.token) === String(token));

      if (booking) {
        document.getElementById('detailPatientName').textContent = booking.name;
        document.getElementById('detailAgeGender').textContent = `${booking.age || '-'} / ${booking.gender || '-'}`;
        document.getElementById('detailPhone').textContent = booking.phone || '-';
        document.getElementById('detailDoctorName').textContent = doctorSelect.value;
        document.getElementById('detailDateTime').textContent = dateInput.value;
        document.getElementById('detailStatus').innerHTML = '<span style="color:#16a34a; background:rgba(22,163,74,0.1); padding:0.25rem 0.6rem; border-radius:6px; font-size:0.78rem; display:inline-flex; align-items:center; gap:0.25rem;"><span style="width:6px;height:6px;border-radius:50%;background:#16a34a;display:inline-block;"></span>Confirmed</span>';
        
        statusDiv.style.display = 'none';
        detailCard.style.display = 'block';
        lucide.createIcons(detailCard);
      } else {
        detailCard.style.display = 'none';
        statusDiv.style.display = 'block';
        statusDiv.innerHTML = `
          <div style="text-align:center; padding:1.5rem; color:#f97316; display:flex; flex-direction:column; align-items:center; gap:0.5rem; border: 1px dashed rgba(249,115,22,0.25); border-radius: 12px; background: #fffbeb;">
            <i data-lucide="alert-circle" style="width:24px; height:24px; color:#f97316;"></i>
            <span>No booking found with Token #${token} on this date.</span>
          </div>
        `;
        lucide.createIcons(statusDiv);
      }
    };

    window.confirmCancelBookingFromPanel = function() {
      const doctorSelect = document.getElementById('cancelBookingDoctorSelect');
      const dateInput = document.getElementById('cancelBookingDate');
      const tokenInput = document.getElementById('cancelTokenInput');
      
      const combined = doctorSelect ? doctorSelect.value : '';
      const date = dateInput ? dateInput.value : '';
      const token = tokenInput ? tokenInput.value.trim() : '';

      if (!combined || !date || !token) {
        showToast('Please select doctor, date, and token.', 'error');
        return;
      }

      const booking = currentCancelBookings.find(b => String(b.token) === String(token));
      const patientName = booking ? booking.name : `Token #${token}`;

      openModal(
        'universalModal',
        'Cancel Booking',
        `Are you sure you want to cancel the booking for ${patientName} (Token #${token})?`,
        'Cancel Booking',
        async () => {
          try {
            const res = await fetch('/admin_delete_booking', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ combined, date, token })
            });
            const data = await res.json();
            if (data.success) {
              showToast('Booking cancelled successfully.', 'success');
              // Clear current selection and re-fetch to sync dropdown
              if (tokenInput) tokenInput.value = '';
              const tokenSelect = document.getElementById('cancelTokenSelect');
              if (tokenSelect) tokenSelect.value = '';
              await fetchCancelBookingsList();
            } else {
              showToast(data.msg || 'Error cancelling booking.', 'error');
            }
          } catch (err) {
            showToast('Communication error. Please retry.', 'error');
          }
        },
        true
      );

      // Inject the correct token card for confirmation
      const modal = document.getElementById('universalModal');
      if (modal) {
        const contentP = modal.querySelector('p');
        if (contentP) {
          const tokenCard = document.createElement('div');
          tokenCard.className = 'success-token-card';
          tokenCard.style.borderColor = '#e11d48'; // Red for cancellation
          tokenCard.innerHTML = `
            <span style="font-size:0.8rem; color:#64748b; text-transform:uppercase; letter-spacing:1px; font-weight:700;">Token Number</span>
            <span class="success-token-value" style="color:#e11d48;">${token}</span>
            <span style="font-size:0.82rem; color:#64748b; font-weight:600;">${date} | ${patientName}</span>
          `;
          contentP.after(tokenCard);
        }
      }
    };

    // ─── Init ──────────────────────────────────────────────────
    async function initPage() {
      await loadDoctors();
      await loadDoctorPairs();
      await loadDoctorsForEdit();
      await loadDoctorsForAdminBooking();
      setupAddDoctorDayTimeUI();
      setupEditDoctorDayTimeUI();
      const specDateInput=document.getElementById('specDate');
      if(specDateInput){
        const today=new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'});
        specDateInput.setAttribute('min',today);
      }
      const specSelect=document.getElementById('specSelect');
      if(specSelect){
        specSelect.addEventListener('change',()=>{
          const today=new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'});
          specDateInput.setAttribute('min',today);
          specDateInput.value='';
          const msg=document.getElementById('noDoctorMsg');
          if(msg) msg.textContent='';
        });
      }
      // Check current section and trigger session check if needed
      const currentActiveSection = Array.from(document.querySelectorAll('section')).find(s => s.style.display !== 'none');
      if (currentActiveSection && currentActiveSection.id === 'admin') {
        checkAdminSession();
      }
      lucide.createIcons();
    }

    document.addEventListener('DOMContentLoaded', async ()=> {
      // 1. Determine the appropriate initial section
      const urlParams = new URLSearchParams(window.location.search);
      const isParamAdmin = urlParams.get('view') === 'admin';

      let savedSection = localStorage.getItem('activeSection') || 'bookDoctor';
      
      // Override saved state if Admin Access is explicitly requested
      if (isParamAdmin) {
        savedSection = 'admin';
        localStorage.setItem('activeSection', 'admin');
      }

      showSection(savedSection, null);
      
      const panelId = sectionPanelMap[savedSection];
      if (panelId) document.getElementById(panelId)?.classList.add('active');
      
      // 2. Bind header logout button
      const headerLogoutBtn = document.getElementById('headerLogoutBtn');
      if (headerLogoutBtn) {
        headerLogoutBtn.addEventListener('click', openLogoutModal);
      }

      lucide.createIcons();

      // 3. Then check admin state and perform other inits
      await initPage();

      // 4. Restore last active admin panel if we are in admin section
      if (savedSection === 'admin') {
        const savedAdminPanel = localStorage.getItem('activeAdminPanel') || 'book';
        const adminBtn = document.querySelector(`.admin-sidebar-nav-item[data-panel="${savedAdminPanel}"]`);
        showAdminPanel(savedAdminPanel, adminBtn);
        
        const savedDoctor = localStorage.getItem('activeDoctor');
        if (savedDoctor) {
          const editSelect = document.getElementById('editDoctorSelect');
          const deleteSelect = document.getElementById('deleteDoctorSelect');
          const leavesSelect = document.getElementById('leaveDoctorSelect');
          
          if (editSelect) {
            // Wait for options to be populated if needed, though initPage should have done it
            const setDoctor = () => {
              if (editSelect.options.length > 1) {
                editSelect.value = savedDoctor;
                editSelect.dispatchEvent(new Event('change'));
                if (deleteSelect) deleteSelect.value = savedDoctor;
                if (leavesSelect) {
                  leavesSelect.value = savedDoctor;
                  leavesSelect.dispatchEvent(new Event('change'));
                }
              } else {
                setTimeout(setDoctor, 100);
              }
            };
            setDoctor();
          }
        }
      }
    });
    function generatePDF(element) {
  const clone = element.cloneNode(true);

  clone.style.display = 'block';
  clone.style.position = 'absolute';
  clone.style.left = '-9999px';

  document.body.appendChild(clone); // 🔥 required

  clone.querySelectorAll('.no-print').forEach(el => el.remove());

  html2pdf().set({
    margin: 10,
    filename: 'Bookings.pdf',
    html2canvas: { scale: 2 },
    jsPDF: { format: 'a4' }
  }).from(clone).save().then(() => {
    document.body.removeChild(clone); // cleanup
  });
}
function downloadPDF() {
  const original = document.getElementById('bookingTableWrapper');
  const tableBody = document.getElementById('bookingsTbody');

  if (!tableBody || tableBody.children.length === 0) {
    showToast('No bookings to download', 'error');
    return;
  }

  // Ensure visible
  original.style.display = 'block';

  setTimeout(() => {
    const clone = original.cloneNode(true);

    clone.style.display = 'block';
    clone.style.position = 'absolute';
    clone.style.left = '-9999px';
    clone.style.background = '#ffffff';
    clone.style.overflow = 'visible';

    clone.querySelectorAll('.no-print').forEach(el => el.remove());

    document.body.appendChild(clone);

    html2pdf().set({
      margin: 10,
      filename: 'Bookings_List.pdf',
      html2canvas: { scale: 2 },
      jsPDF: { unit: 'mm', format: 'a4' }
    }).from(clone).save().then(() => {
      document.body.removeChild(clone);
    });

  }, 300);
}

    // ─── Set Date Constraints ───
    (function() {
      const today = new Date();
      const istToday = new Date(today.toLocaleString("en-US", {timeZone: "Asia/Kolkata"}));
      const minDateStr = istToday.toISOString().split('T')[0];
      
      const maxDate = new Date(istToday);
      maxDate.setDate(istToday.getDate() + 15);
      const maxDateStr = maxDate.toISOString().split('T')[0];
      
      const dDateInput = document.getElementById('doctorDate');
      const sDateInput = document.getElementById('specDate');
      
      [dDateInput, sDateInput].forEach(inp => {
        if (!inp) return;
        inp.setAttribute('min', minDateStr);
        inp.setAttribute('max', maxDateStr);
        
        inp.addEventListener('change', () => {
          const selected = inp.value;
          if (selected > maxDateStr) {
            alert('Bookings are only allowed up to 15 days in advance.');
            inp.value = '';
          }
        });
      });
    })();
  
    /**
     * Unified Modal System


    /**
     * Unified Modal System
     * @param {string} id - legacy param or 'success'
     */
    window.openModal = function(id, title, message, actionText, actionFn, isDanger = false) {
      console.log(`[Modal] Opening: ${title}`);
      
      const modal = document.getElementById('universalModal');
      const titleEl = document.getElementById('unvTitle');
      const msgEl = document.getElementById('unvMessage');
      const btn = document.getElementById('unvConfirmBtn');
      const container = document.getElementById('unvIconContainer');

      // Cleanup
      const oldCard = modal.querySelector('.success-token-card');
      if (oldCard) oldCard.remove();

      titleEl.textContent = title;
      msgEl.innerHTML = message;
      
      btn.textContent = actionText;
      btn.style.background = isDanger ? '#e11d48' : '#0077b6';
      btn.style.boxShadow = isDanger ? '0 12px 30px rgba(225, 29, 72, 0.35)' : '0 12px 30px rgba(0, 119, 182, 0.35)';
      
      // Icon Logic
      let iconName = 'help-circle';
      if (id === 'success') {
          iconName = 'check-circle';
          container.style.color = '#10b981';
          container.style.background = 'rgba(16, 185, 129, 0.1)';
      } else if (id === 'warning') {
          iconName = 'alert-circle';
          container.style.color = '#f59e0b'; // Amber-600
          container.style.background = 'rgba(245, 158, 11, 0.1)';
      } else if (isDanger) {
          iconName = 'alert-triangle';
          container.style.color = '#e11d48';
          container.style.background = 'rgba(225, 29, 72, 0.1)';
      } else {
          container.style.color = '#0077b6';
          container.style.background = 'rgba(0, 119, 182, 0.1)';
      }
      
      container.innerHTML = `<i data-lucide="${iconName}" style="width: 32px; height: 32px;"></i>`;
      
      activeModalAction = actionFn;
      modal.style.display = 'flex';
      if (window.lucide) lucide.createIcons();
    };

    window.closeModal = function() {
      const modal = document.getElementById('universalModal');
      if (modal) modal.style.display = 'none';
      activeModalAction = null;
      // Clean up special cards
      const card = modal.querySelector('.success-token-card');
      if (card) card.remove();
      
      const waBtn = document.getElementById('adminWhatsAppBtn');
      if (waBtn) waBtn.remove();
    };
    
    // Alias for backward compatibility or new calls
    window.closeUniversalModal = window.closeModal;
    window.openUniversalModal = window.openModal;

    document.getElementById('unvConfirmBtn')?.addEventListener('click', () => {
      console.log("[Modal] Confirmation clicked");
      if (activeModalAction) activeModalAction();
      closeModal();
    });

    window.openLogoutModal = function() {
      openModal('logout', 'Confirm Logout', 'Are you sure you want to end your current administrative session?', 'Logout', confirmLogout, true);
    };

    // ─── Dashboard Sync Poller ──────────────────────────────────
    async function syncMyBookingsLiveStatus() {
      const myBookingsSection = document.getElementById('mybookings');
      if (!myBookingsSection || myBookingsSection.style.display === 'none') return;
      
      const badges = document.querySelectorAll('.live-sync-badge');
      if (badges.length === 0) return;

      const now = new Date();
      const currentHHMM = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');

      try {
        const res = await fetch('/live_tokens');
        const data = await res.json();
        if (!data.success) return;

        badges.forEach(badge => {
          const docName = badge.dataset.liveDoctor;
          const docSpec = badge.dataset.liveSpec;
          const schedStart = badge.dataset.schedStart || "00:00";
          
          const liveData = data.data.find(d => 
            d.doctor_name.toLowerCase().trim() === docName && 
            d.specialization.toLowerCase().trim() === docSpec
          );

          if (liveData) {
            if (liveData.status === 'active') {
              badge.classList.remove('idle');
              if (!badge.querySelector('.blinking-dot-small')) {
                badge.innerHTML = '<span class="blinking-dot-small"></span><span class="live-text"></span>';
              }
              badge.querySelector('.live-text').textContent = `Live: Calling #${liveData.current_token}`;
            } else if (liveData.status === 'idle') {
              badge.classList.add('idle');
              if (currentHHMM >= schedStart) {
                badge.innerHTML = '<i data-lucide="clock" style="width:11px;height:11px;"></i><span class="live-text">Awaiting Start</span>';
              } else {
                badge.innerHTML = `<i data-lucide="calendar" style="width:11px;height:11px;"></i><span class="live-text">Starts at ${schedStart}</span>`;
              }
              lucide.createIcons();
            } else if (liveData.status === 'completed') {
              badge.classList.add('idle');
              badge.innerHTML = '<i data-lucide="check-circle" style="width:11px;height:11px;"></i><span class="live-text">Completed</span>';
              lucide.createIcons();
            }
          }
        });
      } catch (e) { console.error("Sync failed", e); }
    }

    // ===================== Admin AI Assistant Chat Logic =====================
    let adminAiChatHistory = [];
    let adminAiChatIsOpen = false;
    let adminAiChatFirstOpen = true;
    let adminAiIsTyping = false;

    // Toggle Admin Chat Panel
    window.toggleAdminAiChat = function(e) {
      if (e) e.stopPropagation();
      const panel = document.getElementById('adminAiChatPanel');
      const overlay = document.getElementById('adminAiChatOverlay');
      const badge = document.getElementById('adminAiFabBadge');
      const input = document.getElementById('adminAiChatInput');

      if (!panel) return;
      
      adminAiChatIsOpen = !adminAiChatIsOpen;
      panel.classList.toggle('open', adminAiChatIsOpen);
      if (overlay) overlay.classList.toggle('open', adminAiChatIsOpen);

      if (window.innerWidth <= 520) {
        document.body.style.overflow = adminAiChatIsOpen ? 'hidden' : '';
      }

      if (adminAiChatIsOpen) {
        if (badge) badge.style.display = 'none';
        if (adminAiChatFirstOpen) {
          adminAiChatFirstOpen = false;
          showAdminWelcome();
        }
        setTimeout(() => input?.focus(), 350);
      }
    };

    function showAdminWelcome() {
      const messagesContainer = document.getElementById('adminAiChatMessages');
      if (!messagesContainer) return;
      
      const ts = document.createElement('div');
      ts.className = 'ai-timestamp';
      ts.textContent = 'Today';
      messagesContainer.appendChild(ts);

      const aiBubble = document.createElement('div');
      aiBubble.className = 'chat-bubble ai-msg ai';
      aiBubble.style.alignSelf = 'flex-start';
      aiBubble.style.width = '100%';
      aiBubble.innerHTML = `
        <div class="ai-msg-avatar">
          <svg viewBox="0 0 100 100" style="width:22px;height:22px;"><circle cx="50" cy="50" r="46" stroke="#0077b6" stroke-width="5" fill="none"/><path d="M42 17L58 17L58 42L83 42L83 58L58 58L58 83L42 83L42 58L17 58L17 42L42 42Z" fill="#0077b6"/><polyline points="17,50 29,50 33,35 40,65 47,50 57,50 63,35 70,65 75,50 83,50" stroke="white" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <div class="ai-bubble" style="width: 100%;">
          <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.5rem;color:#0077b6;font-weight:700;">
            Welcome to Admin Assistant
          </div>
          Hello! I am your <strong>PrimeCare Admin Assistant</strong>.<br><br>
          I can help you check <strong>bookings</strong>, view <strong>upcoming holidays</strong>, answer questions about the clinic's setup, and more.<br>
          How can I assist you today?
        </div>
      `;
      messagesContainer.appendChild(aiBubble);
      if (typeof lucide !== 'undefined') lucide.createIcons();
    }

    // Auto-resize textarea
    const adminInput = document.getElementById('adminAiChatInput');
    if (adminInput) {
      adminInput.addEventListener('input', function() {
        this.style.height = 'auto';
        this.style.height = Math.min(this.scrollHeight, 100) + 'px';
      });
    }

    window.adminAiInputKeydown = function(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendAdminAiMessage();
      }
    };

    function escapeHtml(text) {
      if (!text) return '';
      return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    function parseMarkdown(text) {
      if (!text) return '';
      
      // Basic markdown parser
      let html = escapeHtml(text);
      
      // bold: **text** -> <strong>text</strong>
      html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
      
      // inline code: `text` -> <code style="background: rgba(147,51,234,0.08); color: #7c3aed; padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 0.85em;">$1</code>
      html = html.replace(/`(.*?)`/g, '<code style="background: rgba(147,51,234,0.08); color: #7c3aed; padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 0.85em;">$1</code>');
      
      // bullet points: lines starting with "- " or "* " -> list items
      const lines = html.split('\n');
      let inList = false;
      let resultLines = [];
      
      for (let line of lines) {
        let trimmed = line.trim();
        if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
          if (!inList) {
            resultLines.push('<ul style="margin: 0.5rem 0; padding-left: 1.25rem; display: flex; flex-direction: column; gap: 4px; list-style-type: disc;">');
            inList = true;
          }
          let content = trimmed.substring(2);
          resultLines.push(`<li>${content}</li>`);
        } else {
          if (inList) {
            resultLines.push('</ul>');
            inList = false;
          }
          resultLines.push(line);
        }
      }
      if (inList) {
        resultLines.push('</ul>');
      }
      
      html = resultLines.join('\n');
      
      // line breaks
      html = html.replace(/\n/g, '<br>');
      return html;
    }

    async function sendAdminAiMessage(event) {
      if (event) event.preventDefault();
      
      const input = document.getElementById('adminAiChatInput');
      const messagesContainer = document.getElementById('adminAiChatMessages');
      if (!input || !messagesContainer) return;
      
      const text = input.value.trim();
      if (!text) return;
      
      // Clear input
      input.value = '';
      
      // Append user bubble
      const userBubble = document.createElement('div');
      userBubble.className = 'chat-bubble ai-msg user';
      userBubble.style.alignSelf = 'flex-end';
      userBubble.style.flexDirection = 'row-reverse';
      userBubble.innerHTML = `
        <div class="ai-msg-avatar">
          <i data-lucide="user" style="width:16px; height:16px;"></i>
        </div>
        <div class="ai-bubble">
          ${escapeHtml(text)}
        </div>
      `;
      messagesContainer.appendChild(userBubble);
      
      // Add typing indicator
      const typingIndicator = document.createElement('div');
      typingIndicator.id = 'aiChatTypingIndicator';
      typingIndicator.className = 'chat-bubble ai-msg ai ai-typing';
      typingIndicator.style.alignSelf = 'flex-start';
      typingIndicator.innerHTML = `
        <div class="ai-msg-avatar">
          <svg viewBox="0 0 100 100" style="width:22px;height:22px;"><circle cx="50" cy="50" r="46" stroke="#0077b6" stroke-width="5" fill="none"/><path d="M42 17L58 17L58 42L83 42L83 58L58 58L58 83L42 83L42 58L17 58L17 42L42 42Z" fill="#0077b6"/><polyline points="17,50 29,50 33,35 40,65 47,50 57,50 63,35 70,65 75,50 83,50" stroke="white" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <div class="ai-bubble">
          <span class="typing-dot"></span>
          <span class="typing-dot"></span>
          <span class="typing-dot"></span>
        </div>
      `;
      messagesContainer.appendChild(typingIndicator);
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
      
      if (window.lucide) lucide.createIcons();

      try {
        const response = await fetch('/admin_ai_chat', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({
            message: text,
            history: adminAiChatHistory
          })
        });
        
        // Remove typing indicator
        document.getElementById('aiChatTypingIndicator')?.remove();
        
        const data = await response.json();
        
        if (data.success) {
          const aiResponse = data.reply || data.response; // Accept 'reply' based on updated backend
          
          // Append to history
          adminAiChatHistory.push({ role: 'user', text: text });
          adminAiChatHistory.push({ role: 'model', text: aiResponse });
          
          // Limit history size to last 20 messages to prevent bloated requests
          if (adminAiChatHistory.length > 20) {
            adminAiChatHistory = adminAiChatHistory.slice(adminAiChatHistory.length - 20);
          }
          
          // Append AI bubble
          const aiBubble = document.createElement('div');
          aiBubble.className = 'chat-bubble ai-msg ai';
          aiBubble.style.alignSelf = 'flex-start';
          aiBubble.style.width = '100%';
          aiBubble.innerHTML = `
            <div class="ai-msg-avatar">
              <svg viewBox="0 0 100 100" style="width:22px;height:22px;"><circle cx="50" cy="50" r="46" stroke="#0077b6" stroke-width="5" fill="none"/><path d="M42 17L58 17L58 42L83 42L83 58L58 58L58 83L42 83L42 58L17 58L17 42L42 42Z" fill="#0077b6"/><polyline points="17,50 29,50 33,35 40,65 47,50 57,50 63,35 70,65 75,50 83,50" stroke="white" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </div>
            <div class="ai-bubble" style="width: 100%; text-align: left;">
              ${parseMarkdown(aiResponse)}
            </div>
          `;
          messagesContainer.appendChild(aiBubble);
        } else {
          // Friendly Error bubble
          const errorMsg = data.error || 'Something went wrong. Please try again.';
          const errorBubble = document.createElement('div');
          errorBubble.className = 'chat-bubble ai-msg ai error';
          errorBubble.style.alignSelf = 'flex-start';
          errorBubble.style.width = '100%';
          errorBubble.innerHTML = `
            <div class="ai-msg-avatar">
              <i data-lucide="alert-triangle" style="width:16px; height:16px; color:#991b1b;"></i>
            </div>
            <div class="ai-bubble" style="background:#fef2f2; border:1px solid #fee2e2; color:#991b1b;">
              ${escapeHtml(errorMsg)}
            </div>
          `;
          messagesContainer.appendChild(errorBubble);
        }
      } catch (err) {
        document.getElementById('aiChatTypingIndicator')?.remove();
        
        const netErrorBubble = document.createElement('div');
        netErrorBubble.className = 'chat-bubble';
        netErrorBubble.style.alignSelf = 'flex-start';
        netErrorBubble.innerHTML = `
          <div style="width:32px; height:32px; border-radius:50%; background:#ef4444; color:white; display:flex; align-items:center; justify-content:center; flex-shrink:0; box-shadow:0 4px 10px rgba(239,68,68,0.25);">
            <i data-lucide="wifi-off" style="width:16px; height:16px;"></i>
          </div>
          <div style="background:#fef2f2; color:#991b1b; border-radius:18px; border-top-left-radius:4px; padding:0.75rem 1rem; font-size:0.9rem; line-height:1.5; font-weight:500; text-align: left;">
            <strong>Network Error:</strong> Failed to connect to the Admin AI server. Make sure the server is running.
          </div>
        `;
        messagesContainer.appendChild(netErrorBubble);
      }
      
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
      if (window.lucide) lucide.createIcons();
    }

    // Initialize poller
    setInterval(syncMyBookingsLiveStatus, 5000);

  