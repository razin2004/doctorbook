import os
import re

def update_home():
    with open('templates/home.html', 'rb') as f:
        content = f.read().decode('utf-8')

    is_crlf = '\r\n' in content
    nl = '\r\n' if is_crlf else '\n'
    c = content.replace('\r\n', '\n')

    # Replace Auth Modal HTML
    old_modal_pattern = r'<!-- PATIENT AUTH MODAL -->\s*<div class="auth-modal-overlay" id="authModal">.*?<!-- DOCTORS SECTION \(Bounded Sticky Scope\) -->'
    
    new_modal = """<!-- PATIENT AUTH MODAL -->
  <div class="auth-modal-overlay" id="authModal">
    <div class="auth-modal" style="position: relative;">
      <!-- Doctor Portal Entry Icon (Subtle stethoscope in top right) -->
      <button class="doctor-entry-btn" id="doctorEntryBtn" onclick="switchAuthView('doctorLogin')" title="Doctor Portal Access" aria-label="Doctor Portal" style="position: absolute; top: 1.1rem; right: 3.5rem; background: none; border: 1px solid #e2e8f0; color: #64748b; cursor: pointer; padding: 6px; border-radius: 8px; transition: all 0.2s; display: flex; align-items: center; justify-content: center;" onmouseover="this.style.color='#0077b6'; this.style.borderColor='#0077b6'; this.style.background='#f0f9ff';" onmouseout="this.style.color='#64748b'; this.style.borderColor='#e2e8f0'; this.style.background='none';">
        <i data-lucide="stethoscope" style="width: 17px; height: 17px;"></i>
      </button>

      <button class="auth-modal-close" onclick="closeAuthModal()" aria-label="Close">
        <i data-lucide="x"></i>
      </button>

      <!-- PATIENT LOGIN VIEW -->
      <div id="authView-login" class="view-section active">
        <h3 class="auth-title">Welcome Back</h3>
        <p class="auth-subtitle">Login to your patient account</p>
        <form onsubmit="handleAuthSubmit(event, 'login')">
          <div class="form-group">
            <label>Mobile Number</label>
            <input type="text" id="login-phone" class="form-control" placeholder="e.g. 9876543210 or +91..." required>
            <small style="color: #64748b; font-size: 0.76rem; display: block; margin-top: 0.25rem;">Existing account? You may also sign in with your email.</small>
          </div>
          <div class="form-group">
            <div style="display:flex; justify-content:space-between;">
              <label>Password</label>
              <a href="#" onclick="switchAuthView('forgot')" style="font-size:0.8rem; color:#0077b6; text-decoration:none;">Forgot?</a>
            </div>
            <input type="password" id="login-password" class="form-control" placeholder="••••••••" required>
          </div>
          <button type="submit" class="auth-btn" id="loginBtnSubmit">Sign In</button>
        </form>
        <div class="auth-switch">Don't have an account? <a onclick="switchAuthView('register')">Sign up</a></div>
      </div>

      <!-- SEPARATE DOCTOR LOGIN VIEW -->
      <div id="authView-doctorLogin" class="view-section">
        <div style="display:flex; align-items:center; gap:0.6rem; margin-bottom:0.25rem;">
          <div style="background:rgba(0,119,182,0.1); width:32px; height:32px; border-radius:8px; display:flex; align-items:center; justify-content:center;">
            <i data-lucide="stethoscope" style="width:18px; height:18px; color:#0077b6;"></i>
          </div>
          <h3 class="auth-title" style="margin:0;">Doctor Portal</h3>
        </div>
        <p class="auth-subtitle">Sign in to your doctor consultation workspace</p>
        <form onsubmit="handleAuthSubmit(event, 'doctorLogin')">
          <div class="form-group">
            <label>Doctor Email Address</label>
            <input type="email" id="doclogin-email" class="form-control" placeholder="doctor@primecare.com" required>
          </div>
          <div class="form-group">
            <label>Password</label>
            <input type="password" id="doclogin-password" class="form-control" placeholder="••••••••" required>
          </div>
          <button type="submit" class="auth-btn" id="docLoginBtnSubmit" style="background:#023e8a;">Doctor Sign In</button>
        </form>
        <div style="margin-top: 1rem; font-size: 0.78rem; color: #64748b; line-height: 1.4; text-align: center;">
          New doctor? Please use the activation link sent to your email by clinic administration.
        </div>
        <div class="auth-switch" style="margin-top: 1rem;"><a onclick="switchAuthView('login')">&larr; Back to Patient Login</a></div>
      </div>

      <!-- PATIENT REGISTER VIEW -->
      <div id="authView-register" class="view-section">
        <h3 class="auth-title">Create Patient Account</h3>
        <p class="auth-subtitle">Register with your mobile number</p>
        <form onsubmit="handleAuthSubmit(event, 'register')">
          <div class="form-group">
            <label>Full Name</label>
            <input type="text" id="reg-name" class="form-control" placeholder="Full Name" required>
          </div>
          <div class="form-group">
            <label>Mobile Number</label>
            <input type="tel" id="reg-phone" class="form-control" placeholder="e.g. 9876543210 or +91..." required>
          </div>
          <div class="form-group">
            <label>Email Address (Optional)</label>
            <input type="email" id="reg-email" class="form-control" placeholder="name@example.com (optional)">
          </div>
          <div class="form-group">
            <label>Password (min 6 chars)</label>
            <input type="password" id="reg-password" class="form-control" placeholder="••••••••" required oninput="validateMatch('register')">
          </div>
          <div class="form-group">
            <label>Confirm Password</label>
            <input type="password" id="reg-confirm-password" class="form-control" placeholder="••••••••" required oninput="validateMatch('register')">
            <div id="register-match-msg" style="font-size:0.75rem; margin-top:0.3rem; font-weight:600; display:none;"></div>
          </div>
          <button type="submit" class="auth-btn" id="regBtnSubmit">Create Account</button>
        </form>
        <div class="auth-switch">Already have an account? <a onclick="switchAuthView('login')">Log in</a></div>
      </div>

      <!-- FORGOT PASSWORD VIEW -->
      <div id="authView-forgot" class="view-section">
        <h3 class="auth-title">Reset Password</h3>
        <p class="auth-subtitle">Enter your registered email or mobile</p>
        <form onsubmit="handleAuthSubmit(event, 'forgot')">
          <div class="form-group">
            <label>Registered Email or Mobile</label>
            <input type="text" id="forgot-email" class="form-control" placeholder="name@example.com or mobile" required>
          </div>
          <button type="submit" class="auth-btn" id="forgotBtnSubmit">Send Code</button>
        </form>
        <div class="auth-switch"><a onclick="switchAuthView('login')">Back to Login</a></div>
      </div>

      <!-- OTP VIEW -->
      <div id="authView-otp" class="view-section">
        <h3 class="auth-title">Enter Verification Code</h3>
        <p class="auth-subtitle">We sent a 6-digit verification code</p>
        <form onsubmit="handleAuthSubmit(event, 'otp')">
          <div class="otp-inputs" id="authOtpInputs">
            <input type="text" maxlength="1" class="otp-input" pattern="[0-9]" required>
            <input type="text" maxlength="1" class="otp-input" pattern="[0-9]" required>
            <input type="text" maxlength="1" class="otp-input" pattern="[0-9]" required>
            <input type="text" maxlength="1" class="otp-input" pattern="[0-9]" required>
            <input type="text" maxlength="1" class="otp-input" pattern="[0-9]" required>
            <input type="text" maxlength="1" class="otp-input" pattern="[0-9]" required>
          </div>
          <button type="submit" class="auth-btn" id="otpBtnSubmit">Verify Code</button>
        </form>
        <div class="auth-switch"><a onclick="switchAuthView('login')">Cancel</a></div>
      </div>

      <!-- RESET PASSWORD VIEW -->
      <div id="authView-reset" class="view-section">
        <h3 class="auth-title">New Password</h3>
        <p class="auth-subtitle">Create a secure new password</p>
        <form onsubmit="handleAuthSubmit(event, 'reset')">
          <div class="form-group">
            <label>New Password (min 6 chars)</label>
            <input type="password" id="reset-password" class="form-control" placeholder="••••••••" required oninput="validateMatch('reset')">
          </div>
          <div class="form-group">
            <label>Confirm New Password</label>
            <input type="password" id="reset-confirm-password" class="form-control" placeholder="••••••••" required oninput="validateMatch('reset')">
            <div id="reset-match-msg" style="font-size:0.75rem; margin-top:0.3rem; font-weight:600; display:none;"></div>
          </div>
          <button type="submit" class="auth-btn" id="resetBtnSubmit">Update Password</button>
        </form>
      </div>

    </div>
  </div>

  <!-- DOCTORS SECTION (Bounded Sticky Scope) -->"""

    match = re.search(old_modal_pattern, c, re.DOTALL)
    if match:
        c = c[:match.start()] + new_modal + c[match.end():]
        print("[OK] Replaced Auth Modal in home.html.")
    else:
        print("[ERROR] Could not match Auth Modal pattern in home.html.")
        return False

    # Replace handleAuthSubmit and switchAuthView logic in JS
    old_js_pattern = r"function switchAuthView\(viewId\) \{.*?async function handleAuthSubmit\(event, flow\) \{.*?(?=async function selectRole)"
    
    new_js = """function switchAuthView(viewId) {
      document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
      const target = document.getElementById('authView-' + viewId);
      if (target) {
        target.classList.add('active');
        if (typeof lucide !== 'undefined' && lucide.createIcons) {
          lucide.createIcons();
        }
      }
    }

    // OTP Input Auto-Advance
    document.querySelectorAll('.otp-input').forEach((input, index, inputs) => {
      input.addEventListener('input', (e) => {
        if (e.target.value && index < inputs.length - 1) inputs[index + 1].focus();
      });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Backspace' && !e.target.value && index > 0) inputs[index - 1].focus();
      });
    });

    function validateMatch(flow) {
      const p1 = document.getElementById(flow === 'register' ? 'reg-password' : 'reset-password').value;
      const p2 = document.getElementById(flow === 'register' ? 'reg-confirm-password' : 'reset-confirm-password').value;
      const msg = document.getElementById(flow === 'register' ? 'register-match-msg' : 'reset-match-msg');
      const btn = document.getElementById(flow === 'register' ? 'regBtnSubmit' : 'resetBtnSubmit');

      if (!p1 && !p2) {
        msg.style.display = 'none';
        return;
      }
      msg.style.display = 'block';

      if (p1 === p2) {
        msg.textContent = 'Passwords match';
        msg.style.color = '#16a34a'; // Success green
      } else {
        msg.textContent = 'Passwords do not match';
        msg.style.color = '#e11d48'; // Danger rose
      }
    }

    async function handleAuthSubmit(event, flow) {
      event.preventDefault();

      if (flow === 'register' || flow === 'reset') {
        const p1 = document.getElementById(flow === 'register' ? 'reg-password' : 'reset-password').value;
        const p2 = document.getElementById(flow === 'register' ? 'reg-confirm-password' : 'reset-confirm-password').value;
        
        if (p1.length < 6) {
          showToast('Password must be at least 6 characters', 'error');
          return;
        }
        if (p1 !== p2) {
          showToast('Passwords do not match', 'error');
          return;
        }
      }

      try {
        let endpoint = '';
        let payload = {};

        if (flow === 'login') {
          endpoint = '/patient_login';
          payload = {
            phone_number: document.getElementById('login-phone').value.trim(),
            password: document.getElementById('login-password').value
          };
        } else if (flow === 'doctorLogin') {
          endpoint = '/doctor_login';
          payload = {
            email: document.getElementById('doclogin-email').value.trim(),
            password: document.getElementById('doclogin-password').value
          };
        } else if (flow === 'register') {
          endpoint = '/patient_register';
          payload = {
            name: document.getElementById('reg-name').value.trim(),
            phone_number: document.getElementById('reg-phone').value.trim(),
            email: document.getElementById('reg-email').value.trim(),
            password: document.getElementById('reg-password').value,
            confirm_password: document.getElementById('reg-confirm-password').value
          };
        } else if (flow === 'otp') {
          endpoint = '/verify_patient_otp';
          const otpStr = Array.from(document.querySelectorAll('.otp-input')).map(i => i.value).join('');
          payload = { otp: otpStr };
        } else if (flow === 'forgot') {
          endpoint = '/send_forgot_otp';
          payload = { email: document.getElementById('forgot-email').value.trim() };
        } else if (flow === 'reset') {
          endpoint = '/reset_password';
          payload = { password: document.getElementById('reset-password').value };
        }

        const res = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();

        if (data.success) {
          showToast(data.msg, 'success');
          if (flow === 'login' || flow === 'doctorLogin' || flow === 'register' || flow === 'reset') {
            const defaultTarget = flow === 'doctorLogin' ? '/doctor_dashboard' : '/patient_dashboard';
            const nextUrl = sessionStorage.getItem('auth_next');
            if (nextUrl && flow !== 'doctorLogin') {
              sessionStorage.removeItem('auth_next');
              setTimeout(() => window.location.href = nextUrl, 800);
            } else {
              setTimeout(() => window.location.href = data.redirect_url || defaultTarget, 800);
            }
          } else if (flow === 'forgot') {
            switchAuthView('otp');
          } else if (flow === 'otp' && data.flow === 'forgot') {
            switchAuthView('reset');
          }
        } else {
          showToast(data.msg || 'An error occurred', 'error');
        }
      } catch (err) {
        showToast('Network error, try again.', 'error');
      }
    }

    """

    match_js = re.search(old_js_pattern, c, re.DOTALL)
    if match_js:
        c = c[:match_js.start()] + new_js + c[match_js.end():]
        print("[OK] Replaced Auth JS logic in home.html.")
    else:
        print("[ERROR] Could not match Auth JS pattern in home.html.")
        return False

    final_content = c.replace('\n', nl)
    with open('templates/home.html', 'wb') as f:
        f.write(final_content.encode('utf-8'))
    print("[SUCCESS] home.html updated successfully.")
    return True

if __name__ == '__main__':
    update_home()

