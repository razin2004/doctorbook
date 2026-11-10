(function () {
  var d = document, root = d.documentElement;
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  // 1. Header shadow after scrolling
  var bar = d.querySelector('.top-bar'), tick = false;
  function onScroll() { if (tick) return; tick = true; requestAnimationFrame(function () { if (bar) bar.classList.toggle('is-scrolled', window.scrollY > 8); tick = false; }); }
  window.addEventListener('scroll', onScroll, { passive: true }); onScroll();
  // 2. Decorative icons hidden from screen readers (after lucide has drawn them)
  window.addEventListener('load', function () { d.querySelectorAll('svg.lucide').forEach(function (s) { s.setAttribute('aria-hidden', 'true'); }); });
  if (reduce || !('IntersectionObserver' in window)) return;   // no hidden start states at all
  root.classList.add('pc-js');
  // 3. Scroll reveals, once per element, 80ms stagger among siblings (maximum 400ms)
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('is-in'); io.unobserve(e.target); } });
  }, { threshold: 0.15, rootMargin: '0px 0px -8% 0px' });
  d.querySelectorAll('.pc-reveal').forEach(function (el) {
    var i = Array.prototype.indexOf.call(el.parentNode.children, el);
    el.style.setProperty('--pc-d', Math.min(i * 80, 400) + 'ms'); io.observe(el);
  });
  // 4. Count-up on the trust numbers (skips years and non-numbers such as ISO)
  var cio = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return; cio.unobserve(e.target);
      var el = e.target, txt = el.getAttribute('data-final'), m = /^(\d[\d,]*)(.*)$/.exec(txt);
      var n = m ? parseInt(m[1].replace(/,/g, ''), 10) : 0, hasComma = m && m[1].indexOf(',') > -1;
      if (!m || (n >= 1900 && n <= 2100)) return;
      var t0 = null;
      (function step(t) { if (!t0) t0 = t; var p = Math.min((t - t0) / 1200, 1), v = Math.round(n * (1 - Math.pow(1 - p, 3)));
        el.textContent = (hasComma ? v.toLocaleString('en-US') : v) + m[2]; if (p < 1) requestAnimationFrame(step); else el.textContent = txt; })(performance.now());
    });
  }, { threshold: 0.6 });
  d.querySelectorAll('.trust-val').forEach(function (el) {
    var txt = el.textContent.trim(); el.setAttribute('data-final', txt); el.setAttribute('aria-label', txt); cio.observe(el);
  });
  // 5. One gentle nudge of the assistant button after 5 seconds, only if it was never opened
  var fab = d.getElementById('aiChatFab');
  if (fab) setTimeout(function () { var p = d.getElementById('aiChatPanel'); if (!p || p.classList.contains('open')) return;
    fab.classList.add('pc-nudge'); fab.addEventListener('animationend', function () { fab.classList.remove('pc-nudge'); }, { once: true }); }, 5000);
})();

