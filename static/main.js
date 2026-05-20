/* ============================================================
   main.js — Base JavaScript for Jindal Yard Manager
   Initializes: Lucide icons, AOS, Nav toggle, Theme picker,
   Activity tracker, Global Footer Marquee, and Micro-interactions
============================================================ */

/* ── 1. Lucide Icons Init ── */
document.addEventListener('DOMContentLoaded', () => {
  if (window.lucide) lucide.createIcons();
});

/* ── 2. AOS Staggered Waterfall & Init ── */
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.bento-grid').forEach(grid => {
    Array.from(grid.children).forEach((child, index) => {
      if (child.classList.contains('bento-card') && !child.hasAttribute('data-aos')) {
        child.setAttribute('data-aos', 'fade-up');
        child.setAttribute('data-aos-delay', Math.min(index * 80, 600).toString());
      }
    });
  });

  if (window.AOS) {
    AOS.init({
      duration: 560,
      easing: 'ease-out-cubic',
      once: true,
      offset: 40,
    });
  }
});

/* ── 3. Micro-Interactions: Magnetic Buttons & Spotlight Cards ── */
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.magnetic').forEach(btn => {
    btn.addEventListener('mousemove', (e) => {
      const rect = btn.getBoundingClientRect();
      const x = (e.clientX - rect.left - rect.width / 2) * 0.3;
      const y = (e.clientY - rect.top - rect.height / 2) * 0.3;
      btn.style.transform = `translate(${x}px, ${y}px)`;
    });
    btn.addEventListener('mouseleave', () => {
      btn.style.transform = '';
    });
  });

  const mainContent = document.getElementById('mainContent');
  if (mainContent) {
    mainContent.addEventListener('mousemove', e => {
      const cards = document.querySelectorAll('.bento-card');
      for (const card of cards) {
        const rect = card.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        card.style.setProperty('--mouse-x', `${x}px`);
        card.style.setProperty('--mouse-y', `${y}px`);
      }
    });
  }
});

/* ── 4. Nav Collapse Toggle ── */
(function () {
  const btn       = document.getElementById('navToggle');
  const body      = document.body;
  const STORE_KEY = 'navCollapsed';

  function updateToggleIcon() {
    const ico = btn.querySelector('i');
    if (!ico) return;
    const isCollapsed = body.classList.contains('nav-collapsed');
    ico.setAttribute('data-lucide', isCollapsed ? 'panel-left-open' : 'panel-left-close');
    if (window.lucide) lucide.createIcons({ nodes: [ico] });
  }

  if (localStorage.getItem(STORE_KEY) === '1') {
    body.classList.add('nav-collapsed');
  }
  updateToggleIcon();

  btn.addEventListener('click', () => {
    body.classList.toggle('nav-collapsed');
    localStorage.setItem(STORE_KEY, body.classList.contains('nav-collapsed') ? '1' : '0');
    updateToggleIcon();
  });
})();

/* ── 4b. Mobile / Tablet Nav Toggle ── */
(function () {
  const mobileBtn = document.getElementById('mobileNavToggle');
  const overlay   = document.getElementById('navOverlay');
  const nav       = document.getElementById('mainNav');
  const MOBILE_MAX_WIDTH = 1023;

  if (!mobileBtn || !overlay || !nav) return;

  function isMobileOrTablet() {
    return window.innerWidth <= MOBILE_MAX_WIDTH;
  }

  function setMobileNavState(isOpen) {
    nav.classList.toggle('mobile-open', isOpen);
    overlay.classList.toggle('active', isOpen);
    document.body.classList.toggle('mobile-nav-open', isOpen);
    mobileBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    mobileBtn.setAttribute('aria-label', isOpen ? 'Close navigation' : 'Open navigation');
    mobileBtn.setAttribute('title', isOpen ? 'Close Menu' : 'Open Menu');
    overlay.setAttribute('aria-hidden', isOpen ? 'false' : 'true');

    const ico = mobileBtn.querySelector('i');
    if (ico) {
      ico.setAttribute('data-lucide', isOpen ? 'x' : 'menu');
      if (window.lucide) lucide.createIcons({ nodes: [ico] });
    }
  }

  function openMobileNav() {
    if (!isMobileOrTablet()) return;
    setMobileNavState(true);
  }

  function closeMobileNav() {
    setMobileNavState(false);
  }

  mobileBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    nav.classList.contains('mobile-open') ? closeMobileNav() : openMobileNav();
  });

  overlay.addEventListener('click', closeMobileNav);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && nav.classList.contains('mobile-open')) {
      closeMobileNav();
    }
  });

  // Close mobile/tablet nav on link click. This keeps same-page links clean too.
  nav.querySelectorAll('a').forEach(a => {
    a.addEventListener('click', () => {
      if (isMobileOrTablet()) closeMobileNav();
    });
  });

  window.addEventListener('resize', () => {
    if (!isMobileOrTablet()) closeMobileNav();
  });
})();

(function () {
  const html      = document.documentElement;
  const STORE_KEY = 'themePreference';

  /* ── Theme definitions ── */
  const THEMES = [
    {
      id:    'light',
      label: 'Base Light',
      dot:   '#3B82F6',
      icon:  'sun',
    },
    {
      id:    'dark',
      label: 'Base Dark',
      dot:   '#1e3a5f',
      icon:  'moon',
    },
    {
      id:    'industrial-neo',
      label: 'Industrial Neo',
      dot:   '#FF5722',
      icon:  'cpu',
    },
    {
      id:    'industrial-neo-dark',
      label: 'Industrial Dark',
      dot:   '#FFD600',
      icon:  'terminal',
    },
    {
      id:    'frosted-glass',
      label: 'Frosted Glass',
      dot:   '#7C3AED',
      icon:  'sparkles',
    },
    {
      id:    'frosted-glass-dark',
      label: 'Frosted Dark',
      dot:   '#818cf8',
      icon:  'moon-star',
    },
    {
      id:    'material3',
      label: 'Material 3.0',
      dot:   '#0B57D0',
      icon:  'layers',
    },
    {
      id:    'material3-dark',
      label: 'Material Dark',
      dot:   '#A8C7FA',
      icon:  'layers-2',
    },
  ];

  /* Time-of-day mesh tints — only for base light */
  function applyTimeOfDayTints(themeId) {
    const root = document.documentElement;
    if (themeId === 'light') {
      const hour = new Date().getHours();
      if (hour >= 5 && hour <= 8) {
        root.style.setProperty('--mesh-1', '#ffe4d6');
        root.style.setProperty('--mesh-2', '#ffd1b3');
      } else if (hour >= 17 && hour <= 19) {
        root.style.setProperty('--mesh-1', '#fcd5ce');
        root.style.setProperty('--mesh-2', '#f8edeb');
      } else {
        root.style.removeProperty('--mesh-1');
        root.style.removeProperty('--mesh-2');
      }
    } else {
      root.style.removeProperty('--mesh-1');
      root.style.removeProperty('--mesh-2');
    }
  }

  /* ── Apply theme to <html> ── */
  function applyTheme(themeId) {
    html.setAttribute('data-theme', themeId);
    applyTimeOfDayTints(themeId);

    // Sync icon in picker button
    const themeObj = THEMES.find(t => t.id === themeId) || THEMES[0];
    const btnLabel = document.getElementById('themeBtnLabel');
    const btnIcon  = document.getElementById('themeBtnIcon');

    if (btnLabel) btnLabel.textContent = themeObj.label;
    if (btnIcon) {
      btnIcon.innerHTML = `<i data-lucide="${themeObj.icon}"></i>`;
      if (window.lucide) lucide.createIcons({ nodes: [btnIcon] });
    }

    // Mark active in dropdown
    document.querySelectorAll('.theme-option').forEach(opt => {
      opt.classList.toggle('active-theme', opt.dataset.themeId === themeId);
    });
  }

  /* ── Build dropdown options ── */
  function buildDropdown() {
    const dropdown = document.getElementById('themeDropdown');
    if (!dropdown) return;
    dropdown.innerHTML = '';
    THEMES.forEach(t => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'theme-option';
      btn.dataset.themeId = t.id;
      btn.innerHTML = `
        <span class="theme-dot" style="background:${t.dot};"></span>
        <span>${t.label}</span>
      `;
      btn.addEventListener('click', () => {
        applyTheme(t.id);
        localStorage.setItem(STORE_KEY, t.id);
        closeDropdown();
      });
      dropdown.appendChild(btn);
    });
  }

  /* ── Dropdown open/close ── */
  function openDropdown() {
    const dropdown  = document.getElementById('themeDropdown');
    const pickerBtn = document.getElementById('themePickerBtn');
    if (dropdown) dropdown.classList.add('open');
    if (pickerBtn) pickerBtn.classList.add('open');
  }

  function closeDropdown() {
    const dropdown  = document.getElementById('themeDropdown');
    const pickerBtn = document.getElementById('themePickerBtn');
    if (dropdown) dropdown.classList.remove('open');
    if (pickerBtn) pickerBtn.classList.remove('open');
  }

  /* ── Init ── */
  document.addEventListener('DOMContentLoaded', () => {
    buildDropdown();

    const pickerBtn = document.getElementById('themePickerBtn');
    if (pickerBtn) {
      pickerBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const dropdown = document.getElementById('themeDropdown');
        if (dropdown && dropdown.classList.contains('open')) {
          closeDropdown();
        } else {
          openDropdown();
        }
      });
    }

    // Close on outside click
    document.addEventListener('click', () => closeDropdown());
    const dropdown = document.getElementById('themeDropdown');
    if (dropdown) dropdown.addEventListener('click', e => e.stopPropagation());

    // Apply saved or system theme
    const saved  = localStorage.getItem(STORE_KEY);
    const system = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    applyTheme(saved || system);

    // Auto-update if system pref changes and user hasn't picked manually
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
      if (!localStorage.getItem(STORE_KEY)) {
        applyTheme(e.matches ? 'dark' : 'light');
      }
    });
  });
})();

/* ── 5b. Session Persistence ──
   Users must stay logged in across tab switches, refreshes, and browser restarts.
   Logout is handled only by the explicit Logout button in base.html.
*/

/* ── 6. Activity Tracker ── */
setInterval(() => {
  fetch('/api/track_time', { method: 'POST' }).catch(() => {});
}, 30000);

/* ── 7. Global Footer Marquee with Animated Counters ── */
function __getBayFromBin(bin) {
  const b = String(bin || '').toUpperCase().trim();
  if (!b) return 'UNK';
  const m = b.match(/^[A-Z]+/);
  return (m && m[0]) ? m[0] : 'UNK';
}

function __num(x) {
  const n = Number(x);
  return Number.isFinite(n) ? n : 0;
}

function __fmt2(n) {
  return (__num(n)).toFixed(2);
}

function __buildBayWiseHTML(label, map, headingClass) {
  const bays = Object.keys(map || {}).sort();
  if (!bays.length) return `<span><span class="marq-h ${headingClass}">${label}:</span> 0 (0.00)</span>`;
  const parts = bays.map(b => `${b} ${Math.round(map[b].qty)} (${__fmt2(map[b].wt)})`);
  return `<span><span class="marq-h ${headingClass}">${label}:</span> ${parts.join(' | ')}</span>`;
}

function __setMarqueeDuration() {
  const inner = document.getElementById('marqueeInner');
  if (!inner) return;
  const totalWidth = inner.scrollWidth;
  if (!totalWidth) return;
  const speedPxPerSec = 90;
  const oneSegmentWidth = totalWidth / 2;
  const durationSec = Math.max(18, Math.ceil(oneSegmentWidth / speedPxPerSec));
  inner.style.setProperty('--marquee-duration', `${durationSec}s`);
}

let __globalMarqueeDataCache = [];

async function fetchGlobalMarqueeData() {
  if (window.itemMeshes && Array.isArray(window.itemMeshes) && window.itemMeshes.length > 0) {
    return window.itemMeshes.map(obj => obj && obj.mesh ? obj.mesh.userData : null).filter(Boolean);
  }
  try {
    const res = await fetch('/api/bins');
    if (res.ok) {
      const data = await res.json();
      const assigned = data.assigned_bins || {};
      let items = [];
      for (const binCode in assigned) {
        const binArray = assigned[binCode];
        if (Array.isArray(binArray)) {
          binArray.forEach(it => {
            if (it) { it.bin = it.bin || binCode; items.push(it); }
          });
        }
      }
      __globalMarqueeDataCache = items;
      return items;
    }
  } catch (e) {
    console.warn("Marquee fetch failed, using cached data.", e);
  }
  return __globalMarqueeDataCache;
}

let __currentAcc = { WIP: {}, FG: {}, FG_FI: {}, PLATES: {}, COILS: {}, TOTAL: {} };

class DataAnimator {
  constructor(startState, endState, duration, onUpdate) {
    this.startState = JSON.parse(JSON.stringify(startState));
    this.endState   = JSON.parse(JSON.stringify(endState));
    this.duration   = duration;
    this.onUpdate   = onUpdate;
    this.startTime  = null;
    this.frameId    = null;
    this.loop = this.loop.bind(this);
  }

  start() {
    this.startTime = performance.now();
    this.frameId   = requestAnimationFrame(this.loop);
  }

  loop(currentTime) {
    let elapsed  = currentTime - this.startTime;
    let progress = Math.min(elapsed / this.duration, 1);
    let ease     = 1 - Math.pow(1 - progress, 3);

    let interpolatedState = { WIP: {}, FG: {}, FG_FI: {}, PLATES: {}, COILS: {}, TOTAL: {} };

    const cats = Object.keys(this.endState);
    for (const cat of cats) {
      const endBays   = this.endState[cat];
      const startBays = this.startState[cat] || {};

      for (const bay in endBays) {
        const targetQty = endBays[bay].qty;
        const targetWt  = endBays[bay].wt;
        const startQty  = startBays[bay] ? startBays[bay].qty : 0;
        const startWt   = startBays[bay] ? startBays[bay].wt  : 0;

        interpolatedState[cat][bay] = {
          qty: startQty + (targetQty - startQty) * ease,
          wt:  startWt  + (targetWt  - startWt)  * ease
        };
      }
    }

    this.onUpdate(interpolatedState);

    if (progress < 1) {
      this.frameId = requestAnimationFrame(this.loop);
    } else {
      __currentAcc = this.endState;
    }
  }
}

async function updateGlobalFooterMarquee() {
  const itemsToCount = await fetchGlobalMarqueeData();
  const targetAcc = { WIP: {}, FG: {}, FG_FI: {}, PLATES: {}, COILS: {}, TOTAL: {} };

  function add(map, bay, wt) {
    if (!map[bay]) map[bay] = { qty: 0, wt: 0 };
    map[bay].qty += 1;
    map[bay].wt  += __num(wt);
  }

  for (const u of itemsToCount) {
    const bin    = u.bin || '';
    const bay    = __getBayFromBin(bin);
    const type   = String(u.type   || '').toUpperCase();
    const status = String(u.status || '').toUpperCase();
    const fiText = String(u.FI_Rel_text || u.fi_rel_text || '').toUpperCase();
    const wt     = u.weight;

    add(targetAcc.TOTAL, bay, wt);
    if (status.includes('WIP'))   add(targetAcc.WIP,    bay, wt);
    if (status.includes('FG'))    add(targetAcc.FG,     bay, wt);
    if (type.includes('PLATE'))   add(targetAcc.PLATES, bay, wt);
    if (type.includes('COIL'))    add(targetAcc.COILS,  bay, wt);
    if (status.includes('FG') && (fiText.includes('FI') || fiText.includes('RELEASED (2)'))) {
      add(targetAcc.FG_FI, bay, wt);
    }
  }

  const animator = new DataAnimator(__currentAcc, targetAcc, 1200, (interpolatedData) => {
    const seg   = document.getElementById('marqueeSegment');
    const clone = document.getElementById('marqueeSegmentClone');
    if (!seg) return;

    const htmlParts = [
      __buildBayWiseHTML('Total WIP Qty (Wt)',                      interpolatedData.WIP,    'h-wip'),
      __buildBayWiseHTML('Total FG Qty (Wt)',                       interpolatedData.FG,     'h-fg'),
      __buildBayWiseHTML('FG with FI & FI Released (2) Qty (Wt)',   interpolatedData.FG_FI,  'h-fi'),
      __buildBayWiseHTML('Total Plates Qty (Wt)',                   interpolatedData.PLATES, 'h-pl'),
      __buildBayWiseHTML('Total Coils Qty (Wt)',                    interpolatedData.COILS,  'h-co'),
      __buildBayWiseHTML('Total Items Qty (Wt)',                    interpolatedData.TOTAL,  'h-ti'),
      `<span class="marq-madeby">Made by Swetabh Shekhar Sinha</span>`
    ];

    seg.innerHTML = htmlParts.join(` <span class="marq-sep">•</span> `);
    if (clone) clone.innerHTML = seg.innerHTML;
  });

  animator.start();
  setTimeout(__setMarqueeDuration, 1300);
}

document.addEventListener('DOMContentLoaded', () => {
  updateGlobalFooterMarquee();
  setInterval(updateGlobalFooterMarquee, 15000);
});

window.addEventListener('resize', () => {
  setTimeout(__setMarqueeDuration, 100);
  // On resize to mobile, marquee starts at left:0 (handled by CSS media queries)
});