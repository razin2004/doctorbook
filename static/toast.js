/* ── AUTOMATIC CSRF FETCH INTERCEPTOR ── */
(function() {
  function getCookie(name) {
    let value = "; " + document.cookie;
    let parts = value.split("; " + name + "=");
    if (parts.length === 2) return parts.pop().split(";").shift();
  }

  const originalFetch = window.fetch;
  window.fetch = function(url, options) {
    options = options || {};
    const method = options.method ? options.method.toUpperCase() : 'GET';
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
      const token = getCookie('csrf_token');
      if (token) {
        options.headers = options.headers || {};
        if (options.headers instanceof Headers) {
          options.headers.set('X-CSRFToken', token);
        } else {
          options.headers['X-CSRFToken'] = token;
        }
      }
    }
    return originalFetch(url, options);
  };
})();

/* ── CENTRAL REUSABLE TOAST MANAGER ── */

class ToastManager {
  constructor() {
    this.toasts = [];      // Currently visible active toasts [{ id, element, timer }]
    this.queue = [];       // Queue of toasts waiting to be displayed
    this.maxVisible = 3;   // Maximum allowed stacked toasts
    this.container = null;
  }

  // Lazy-initialize the toast container element in the body
  initContainer() {
    if (!this.container) {
      this.container = document.getElementById('toast-container');
      if (!this.container) {
        this.container = document.createElement('div');
        this.container.id = 'toast-container';
        document.body.appendChild(this.container);
      }
    }
  }

  // Create a toast request and add it to queue
  create(type, title, message) {
    this.initContainer();

    const id = 'toast-' + Math.random().toString(36).substr(2, 9) + '-' + Date.now();
    const toastData = { id, type, title, message };

    this.queue.push(toastData);
    this.processQueue();
  }

  // Render the next toast if limits permit
  processQueue() {
    if (this.toasts.length >= this.maxVisible || this.queue.length === 0) {
      return;
    }

    const toastData = this.queue.shift();
    this.renderToast(toastData);
  }

  // Build and inject toast HTML
  renderToast(toastData) {
    const card = document.createElement('div');
    card.id = toastData.id;
    card.className = `toast-card toast-${toastData.type} entering`;
    card.setAttribute('role', 'alert');
    card.setAttribute('aria-live', toastData.type === 'error' ? 'assertive' : 'polite');

    // Define SVGs directly inline so that we do not depend on external font loaders
    let iconSvg = '';
    if (toastData.type === 'success') {
      iconSvg = `
        <svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="20 6 9 17 4 12"></polyline>
        </svg>
      `;
    } else if (toastData.type === 'error') {
      iconSvg = `
        <svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      `;
    } else if (toastData.type === 'warning') {
      iconSvg = `
        <svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
          <line x1="12" y1="9" x2="12" y2="13"></line>
          <line x1="12" y1="17" x2="12.01" y2="17"></line>
        </svg>
      `;
    } else { // info / information
      iconSvg = `
        <svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"></circle>
          <line x1="12" y1="16" x2="12" y2="12"></line>
          <line x1="12" y1="8" x2="12.01" y2="8"></line>
        </svg>
      `;
    }

    card.innerHTML = `
      <div class="toast-icon-wrapper">
        ${iconSvg}
      </div>
      <div class="toast-content">
        <div class="toast-title">${toastData.title}</div>
        <div class="toast-message">${toastData.message}</div>
      </div>
      <button class="toast-close-btn" aria-label="Dismiss notification">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    `;

    // Determine auto-dismiss duration (increased display durations)
    let duration = 6000;
    if (toastData.type === 'success') {
      duration = 5000; // Success display increased from 3s to 5s
    } else if (toastData.type === 'information') {
      duration = 6000; // Info display increased from 4s to 6s
    } else if (toastData.type === 'warning') {
      duration = 8000; // Warning display increased from 5s to 8s
    } else if (toastData.type === 'error') {
      duration = 10000; // Error display auto-closes after 10s (was manual-only)
    }

    let timer = null;
    if (duration > 0) {
      const progressBar = document.createElement('div');
      progressBar.className = 'toast-progress-bar';
      card.appendChild(progressBar);

      // Trigger linear width transition on progress bar
      setTimeout(() => {
        progressBar.style.transition = `width ${duration}ms linear`;
        progressBar.style.width = '0%';
      }, 50);

      timer = setTimeout(() => {
        this.dismiss(toastData.id);
      }, duration);
    }

    // Attach click events for close button (desktop dismissal)
    const closeBtn = card.querySelector('.toast-close-btn');
    closeBtn.addEventListener('click', () => {
      if (timer) clearTimeout(timer);
      this.dismiss(toastData.id);
    });

    // Touch gesture swipe dismissal logic on mobile (swipe left, right, or upward)
    let startX = 0;
    let startY = 0;
    let currentX = 0;
    let currentY = 0;
    let isDragging = false;

    card.addEventListener('touchstart', (e) => {
      if (e.touches.length !== 1) return;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
      currentX = startX;
      currentY = startY;
      isDragging = true;
      card.style.transition = 'none'; // Lock transition for real-time tracking response
    }, { passive: true });

    card.addEventListener('touchmove', (e) => {
      if (!isDragging || e.touches.length !== 1) return;
      currentX = e.touches[0].clientX;
      currentY = e.touches[0].clientY;

      const diffX = currentX - startX;
      const diffY = currentY - startY;

      let moveX = 0;
      let moveY = 0;

      // Lock swipe to either horizontal (left/right) or vertical (upward) axis to prevent diagonal movement
      if (Math.abs(diffX) > Math.abs(diffY)) {
        moveX = diffX;
        moveY = 0;
      } else {
        moveX = 0;
        moveY = diffY < 0 ? diffY : 0;
      }

      // Drop opacity gradually as user drags card away
      const dist = Math.max(Math.abs(moveX), Math.abs(moveY));
      const opacity = Math.max(0.1, 1 - dist / 220);

      card.style.transform = `translate(${moveX}px, ${moveY}px) scale(0.98)`;
      card.style.opacity = opacity;
    }, { passive: true });

    card.addEventListener('touchend', () => {
      if (!isDragging) return;
      isDragging = false;

      // Restore transition style for elastic snap back or fly out animation
      card.style.transition = 'transform 0.3s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.3s ease';

      const diffX = currentX - startX;
      const diffY = currentY - startY;
      const swipeThreshold = 75; // Swipe distance required to trigger dismiss

      const isHorizontal = Math.abs(diffX) > Math.abs(diffY);

      if (isHorizontal && Math.abs(diffX) > swipeThreshold) {
        // Cancel auto-dismiss timer
        if (timer) clearTimeout(timer);

        // Fly out in horizontal direction
        card.style.transform = `translateX(${diffX > 0 ? '120%' : '-120%'})`;
        card.style.opacity = '0';

        // Trigger dismiss after transition completes
        setTimeout(() => {
          this.dismiss(toastData.id);
        }, 250);
      } else if (!isHorizontal && diffY < -swipeThreshold) {
        // Cancel auto-dismiss timer
        if (timer) clearTimeout(timer);

        // Fly out in vertical upward direction
        card.style.transform = `translateY(-120%)`;
        card.style.opacity = '0';

        // Trigger dismiss after transition completes
        setTimeout(() => {
          this.dismiss(toastData.id);
        }, 250);
      } else {
        // Snap back to default layout if threshold not met
        card.style.transform = 'translate(0, 0) scale(1)';
        card.style.opacity = '1';
      }
    });

    // Append to container
    this.container.appendChild(card);
    this.toasts.push({ id: toastData.id, element: card, timer });

    // Trigger entrance transition
    setTimeout(() => {
      card.classList.remove('entering');
      card.classList.add('active');
    }, 10);
  }

  // Dismiss toast and collapse layout
  dismiss(id) {
    const activeToast = this.toasts.find(t => t.id === id);
    if (!activeToast) return;

    const card = activeToast.element;
    card.classList.remove('active');
    card.classList.add('leaving');

    if (activeToast.timer) {
      clearTimeout(activeToast.timer);
    }

    // Remove element after exit transition is complete (matches css transition duration)
    setTimeout(() => {
      card.remove();
      this.toasts = this.toasts.filter(t => t.id !== id);
      this.processQueue();
    }, 350);
  }
}

// Instantiate singleton manager
const toastManager = new ToastManager();

// Parse custom or legacy arguments to adapt showToast
function parseToastArguments(a, b, c) {
  let type = 'information';
  let title = 'Information';
  let message = '';
  const validTypes = ['success', 'error', 'warning', 'info', 'information'];

  // Normalization helper
  const getTitle = (t) => {
    const name = t === 'info' ? 'information' : t;
    return name.charAt(0).toUpperCase() + name.slice(1);
  };

  if (c !== undefined) {
    // New signature: showToast(type, title, message)
    type = String(a).toLowerCase();
    title = b;
    message = c;
  } else if (b !== undefined) {
    // Two arguments: showToast(message, type) OR showToast(type, message) OR showToast(title, message)
    const aLower = String(a).toLowerCase();
    const bLower = String(b).toLowerCase();

    if (validTypes.includes(bLower)) {
      // Legacy signature: showToast(message, type)
      type = bLower;
      message = a;
      title = getTitle(type);
    } else if (validTypes.includes(aLower)) {
      // Reverse: showToast(type, message)
      type = aLower;
      message = b;
      title = getTitle(type);
    } else {
      // Fallback: showToast(title, message)
      type = 'information';
      title = a;
      message = b;
    }
  } else if (a !== undefined) {
    // Single argument: showToast(message)
    type = 'information';
    title = 'Information';
    message = a;
  }

  // Canonical type normalization
  if (type === 'info') {
    type = 'information';
  } else if (!validTypes.includes(type)) {
    type = 'information';
  }

  return { type, title, message };
}

// Export showToast globally
window.showToast = function(a, b, c) {
  const parsed = parseToastArguments(a, b, c);
  toastManager.create(parsed.type, parsed.title, parsed.message);
};

// ── GLOBAL LOADING-STATE SYSTEM ──

let lastClickedButton = null;
let actionTimeout = null;

// Track the last clicked action button to associate it with active fetches/requests
// Use capture phase (true) to run before any stopPropagation in page-specific scripts
document.addEventListener('click', (e) => {
  const btn = e.target.closest('button, input[type="submit"], input[type="button"], .btn');
  if (btn && !btn.disabled && btn.dataset.keepLoading !== 'true') {
    // Check if it's a critical action button
    const btnText = btn.textContent.trim().toLowerCase();
    const actionKeywords = [
      'sign up', 'register', 'create', 'book', 'appointment', 'save', 'update', 'edit',
      'delete', 'remove', 'dismiss', 'send', 'verify', 'validate', 'upload', 'login',
      'sign in', 'logout', 'sign out', 'confirm', 'yes', 'complete', 'skip', 'start',
      'consult', 'change password', 'reset password', 'force delete'
    ];
    
    const isAction = actionKeywords.some(keyword => btnText.includes(keyword)) || 
                     btn.getAttribute('type') === 'submit' ||
                     btn.closest('form') !== null;
                     
    if (isAction) {
      lastClickedButton = btn;
      
      // Instantly trigger loading state to block double clicks immediately
      window.setLoadingState(btn, true);
      
      // Auto-restore after a safe delay if no network request is observed
      if (actionTimeout) clearTimeout(actionTimeout);
      actionTimeout = setTimeout(() => {
        if (btn && btn.dataset.loading === 'true' && btn.dataset.keepLoading !== 'true') {
          window.setLoadingState(btn, false);
        }
        if (lastClickedButton === btn) {
          lastClickedButton = null;
        }
      }, 2000);
    }
  }
}, true);

// Listen for standard form submits to instantly trigger loading indicator and prevent double-clicks
// Runs in capture phase (true) to run before page-specific event listeners
document.addEventListener('submit', (e) => {
  const form = e.target;
  const submitBtn = form.querySelector('button[type="submit"], input[type="submit"]');
  if (submitBtn) {
    window.setLoadingState(submitBtn, true);
    
    // If submission is cancelled by page validation scripts, restore button immediately
    setTimeout(() => {
      if (e.defaultPrevented) {
        window.setLoadingState(submitBtn, false);
      }
    }, 0);
  }
}, true);

// Set loading state with custom spinner and contextual action text
window.setLoadingState = function(button, isLoading, actionText) {
  if (!button) return;

  if (isLoading) {
    if (button.dataset.keepLoading === "true") return;
    
    // Keep track of original text and disabled attribute to restore them later
    if (!button.dataset.originalHtml) {
      button.dataset.originalHtml = button.innerHTML;
    }
    button.disabled = true;
    button.dataset.loading = "true";

    // Determine meaningful action-specific loading text
    let loadingText = 'Processing...';
    if (actionText) {
      loadingText = actionText;
    } else {
      const btnText = button.textContent.trim().toLowerCase();
      if (btnText.includes('sign up') || btnText.includes('register') || btnText.includes('create account') || btnText.includes('create patient')) {
        loadingText = 'Creating...';
      } else if (btnText.includes('book') || btnText.includes('appointment')) {
        loadingText = 'Booking...';
      } else if (btnText.includes('save changes') || btnText.includes('save settings') || btnText.includes('save') || btnText.includes('update') || btnText.includes('edit')) {
        loadingText = 'Saving...';
      } else if (btnText.includes('force delete') || btnText.includes('delete') || btnText.includes('remove') || btnText.includes('dismiss')) {
        loadingText = 'Deleting...';
      } else if (btnText.includes('send otp') || btnText.includes('get otp') || btnText.includes('send') || btnText.includes('resend')) {
        loadingText = 'Sending...';
      } else if (btnText.includes('verify otp') || btnText.includes('verify') || btnText.includes('validate')) {
        loadingText = 'Verifying...';
      } else if (btnText.includes('upload')) {
        loadingText = 'Uploading...';
      } else if (btnText.includes('login') || btnText.includes('sign in')) {
        loadingText = 'Logging in...';
      } else if (btnText.includes('logout') || btnText.includes('sign out')) {
        loadingText = 'Logging out...';
      } else if (btnText.includes('change password')) {
        loadingText = 'Changing...';
      } else if (btnText.includes('reset password')) {
        loadingText = 'Resetting...';
      } else if (btnText.includes('cancel')) {
        loadingText = 'Cancelling...';
      } else if (btnText.includes('add')) {
        loadingText = 'Adding...';
      } else if (btnText.includes('start')) {
        loadingText = 'Starting...';
      } else if (btnText.includes('complete')) {
        loadingText = 'Completing...';
      } else if (btnText.includes('skip')) {
        loadingText = 'Skipping...';
      } else if (btnText.includes('consult')) {
        loadingText = 'Consulting...';
      } else if (btnText.includes('confirm') || btnText.includes('yes')) {
        loadingText = 'Confirming...';
      }
    }

    button.innerHTML = `
      <svg class="loading-spinner" viewBox="0 0 50 50">
        <circle cx="25" cy="25" r="20" stroke-dasharray="80, 200" stroke-dashoffset="0"></circle>
      </svg>
      <span>${loadingText}</span>
    `;
  } else {
    if (button.dataset.keepLoading === "true") {
      return; // Never restore if flagged to remain disabled (e.g. successful booking)
    }
    // Restore button back to normal state
    if (button.dataset.originalHtml) {
      button.innerHTML = button.dataset.originalHtml;
      button.removeAttribute('data-original-html');
    }
    button.disabled = false;
    button.removeAttribute('data-loading');
  }
};

// Hook into window.fetch to automatically trigger loading state on action buttons
const originalFetch = window.fetch;
window.fetch = async function(...args) {
  const btn = lastClickedButton;
  
  // Exclude background polls to prevent unwanted loading state triggers
  const url = args[0] ? String(args[0]).toLowerCase() : '';
  const isBackgroundPoll = url.includes('/live_tokens') || url.includes('/my_token_status') || url.includes('/api/doctor_stats') || url.includes('/doctor/my_stats');
  
  if (btn && (!btn.disabled || btn.dataset.loading === 'true') && !isBackgroundPoll) {
    if (actionTimeout) {
      clearTimeout(actionTimeout);
      actionTimeout = null;
    }
    
    window.setLoadingState(btn, true);
    let success = false;
    try {
      const response = await originalFetch(...args);
      try {
        const clone = response.clone();
        const json = await clone.json();
        if (json && json.success !== false) {
          success = true;
        }
      } catch (e) {
        if (response.status >= 200 && response.status < 300) {
          success = true;
        }
      }
      return response;
    } finally {
      const btnText = btn.textContent.trim().toLowerCase();
      const isBooking = btnText.includes('booking');
      
      if (isBooking && success) {
        btn.dataset.keepLoading = "true";
      } else {
        window.setLoadingState(btn, false);
      }
      
      if (lastClickedButton === btn) {
        lastClickedButton = null;
      }
    }
  } else {
    return originalFetch(...args);
  }
};

// Hook into XMLHttpRequest to automatically trigger loading state for older AJAX calls
const originalXHR = window.XMLHttpRequest;
window.XMLHttpRequest = function() {
  const xhr = new originalXHR();
  const btn = lastClickedButton;
  
  let isBackground = false;
  const originalOpen = xhr.open;
  xhr.open = function(method, url, ...rest) {
    const lowerUrl = String(url).toLowerCase();
    isBackground = lowerUrl.includes('/live_tokens') || lowerUrl.includes('/my_token_status') || lowerUrl.includes('/api/doctor_stats') || lowerUrl.includes('/doctor/my_stats');
    return originalOpen.call(xhr, method, url, ...rest);
  };

  const originalSend = xhr.send;
  xhr.send = function(...args) {
    if (btn && (!btn.disabled || btn.dataset.loading === 'true') && !isBackground) {
      if (actionTimeout) {
        clearTimeout(actionTimeout);
        actionTimeout = null;
      }
      
      window.setLoadingState(btn, true);
      const restore = () => {
        let success = false;
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            const json = JSON.parse(xhr.responseText);
            if (json && json.success !== false) {
              success = true;
            }
          } catch(e) {
            success = true;
          }
        }
        
        const btnText = btn.textContent.trim().toLowerCase();
        const isBooking = btnText.includes('booking');
        
        if (isBooking && success) {
          btn.dataset.keepLoading = "true";
        } else {
          window.setLoadingState(btn, false);
        }
        
        if (lastClickedButton === btn) {
          lastClickedButton = null;
        }
      };
      xhr.addEventListener('loadend', restore);
    }
    return originalSend.call(xhr, ...args);
  };
  
  return xhr;
};

