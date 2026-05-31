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

      // Allow dragging left, right, or upward. Prevent downward drag.
      const moveX = diffX;
      const moveY = diffY < 0 ? diffY : 0;

      // Drop opacity gradually as user drags card away
      const dist = Math.sqrt(moveX * moveX + moveY * moveY);
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

      if (Math.abs(diffX) > swipeThreshold || diffY < -swipeThreshold) {
        // Cancel auto-dismiss timer
        if (timer) clearTimeout(timer);

        // Fly out in swipe direction
        if (Math.abs(diffX) > swipeThreshold) {
          card.style.transform = `translateX(${diffX > 0 ? '120%' : '-120%'})`;
        } else {
          card.style.transform = `translateY(-120%)`;
        }
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

// Track the last clicked action button to associate it with active fetches
document.addEventListener('click', (e) => {
  const btn = e.target.closest('button, input[type="submit"], input[type="button"], .btn');
  if (btn) {
    lastClickedButton = btn;
    // Auto-clear after a safe delay if no fetch/action follows
    setTimeout(() => {
      if (lastClickedButton === btn) lastClickedButton = null;
    }, 2000);
  }
}, { passive: true });

// Listen for standard form submits to instantly trigger loading indicator and prevent double-clicks
document.addEventListener('submit', (e) => {
  const form = e.target;
  const submitBtn = form.querySelector('button[type="submit"], input[type="submit"]');
  if (submitBtn) {
    window.setLoadingState(submitBtn, true);
  }
});

// Set loading state with custom spinner and contextual action text
window.setLoadingState = function(button, isLoading, actionText) {
  if (!button) return;

  if (isLoading) {
    // Keep track of original text and disabled attribute to restore them later
    if (!button.dataset.originalHtml) {
      button.dataset.originalHtml = button.innerHTML;
    }
    button.disabled = true;

    // Determine meaningful action-specific loading text
    let loadingText = 'Processing...';
    if (actionText) {
      loadingText = actionText;
    } else {
      const btnText = button.textContent.trim().toLowerCase();
      if (btnText.includes('sign up') || btnText.includes('register') || btnText.includes('create account')) {
        loadingText = 'Creating...';
      } else if (btnText.includes('book')) {
        loadingText = 'Booking...';
      } else if (btnText.includes('save') || btnText.includes('update') || btnText.includes('edit')) {
        loadingText = 'Saving...';
      } else if (btnText.includes('delete') || btnText.includes('remove') || btnText.includes('dismiss')) {
        loadingText = 'Deleting...';
      } else if (btnText.includes('send') || btnText.includes('resend')) {
        loadingText = 'Sending...';
      } else if (btnText.includes('verify') || btnText.includes('validate')) {
        loadingText = 'Verifying...';
      } else if (btnText.includes('upload')) {
        loadingText = 'Uploading...';
      } else if (btnText.includes('logout') || btnText.includes('sign out')) {
        loadingText = 'Logging out...';
      } else if (btnText.includes('login') || btnText.includes('sign in')) {
        loadingText = 'Logging in...';
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
    // Restore button back to normal state
    if (button.dataset.originalHtml) {
      button.innerHTML = button.dataset.originalHtml;
      button.removeAttribute('data-original-html');
    }
    button.disabled = false;
  }
};

// Hook into window.fetch to automatically trigger loading state on action buttons
const originalFetch = window.fetch;
window.fetch = async function(...args) {
  const btn = lastClickedButton;
  
  // Exclude background polls to prevent unwanted loading state triggers
  const url = args[0] ? String(args[0]).toLowerCase() : '';
  const isBackgroundPoll = url.includes('/live_tokens') || url.includes('/my_token_status') || url.includes('/api/doctor_stats') || url.includes('/doctor/my_stats');
  
  if (btn && !btn.disabled && !isBackgroundPoll) {
    window.setLoadingState(btn, true);
    try {
      const response = await originalFetch(...args);
      return response;
    } finally {
      window.setLoadingState(btn, false);
      // Clean lastClickedButton state
      if (lastClickedButton === btn) {
        lastClickedButton = null;
      }
    }
  } else {
    return originalFetch(...args);
  }
};

