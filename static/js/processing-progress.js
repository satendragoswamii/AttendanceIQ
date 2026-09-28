/* Processing progress overlay.
 *
 * A tool's form POST can take a while on a slow connection, and a page that
 * sits there doing nothing reads as broken. Any form carrying
 * data-progress="<tool>" gets an overlay naming the stage the work is in.
 *
 * Honesty note: the browser cannot see inside the request, so stage timings
 * are estimates scaled by how much is being uploaded. The overlay therefore
 * never claims the job finished - the last stage keeps running until the
 * server's response replaces the page. If the estimate runs out first, it says
 * so rather than pretending.
 */
(function () {
  'use strict';

  var STAGES = {
    'attendance-upload': {
      title: 'Uploading Zoom logs',
      steps: [
        'Uploading your file(s)',
        'Checking the file structure',
        'Detecting session times',
        'Opening the session setup'
      ]
    },
    'attendance-process': {
      title: 'Generating attendance',
      steps: [
        'Sending your session settings',
        'Reading join and leave rows',
        'Merging duplicate entries',
        'Calculating time in each session',
        'Marking present and absent',
        'Building the attendance workbook'
      ]
    },
    'smart-matcher': {
      title: 'Matching participants',
      steps: [
        'Uploading your file(s)',
        'Reading the master list',
        'Reading raw participant names',
        'Matching names against the list',
        'Building the matched workbook'
      ]
    },
    'final-excel': {
      title: 'Generating session reports',
      steps: [
        'Uploading your file(s)',
        'Reading the attendance sheet',
        'Splitting the sessions',
        'Writing one report per session'
      ]
    },
    'raw-excel': {
      title: 'Generating Excel',
      steps: [
        'Uploading your file(s)',
        'Reading the raw log',
        'Extracting participant rows',
        'Building the Excel file'
      ]
    },
    'summary': {
      title: 'Building meeting summary',
      steps: [
        'Uploading your file(s)',
        'Reading Subject, Meeting ID and End Time',
        'Counting participants',
        'Building the summary report'
      ]
    }
  };

  // Rough pacing. Real duration depends on the file, so these are scaled below.
  var BASE_STEP_MS = 1100;
  var MIN_STEP_MS = 500;
  var MAX_STEP_MS = 6000;

  /* Settings override the stage text, the wording around it, and whether the
   * overlay runs at all. The block above stays as the fallback so a missing or
   * broken setting leaves a working overlay rather than a blank one. */
  var CONFIG = window.PROCESSING_PROGRESS_CONFIG || {};
  var TEXT = {
    subtitle: CONFIG.subtitle || 'Keep this tab open until it finishes.',
    slowNotice: CONFIG.slow_notice ||
      'This is taking longer than usual. The file is still being processed — ' +
      'a large file or a slow connection can stretch this out.'
  };

  if (CONFIG.tools) {
    Object.keys(CONFIG.tools).forEach(function (key) {
      var tool = CONFIG.tools[key];
      if (!tool) { return; }
      var steps = Array.isArray(tool.steps) ? tool.steps.filter(Boolean) : null;
      if (!steps || !steps.length) { return; }
      STAGES[key] = { title: tool.title || (STAGES[key] && STAGES[key].title) || 'Processing', steps: steps };
    });
  }

  function isEnabled() {
    return CONFIG.enabled !== false;
  }

  var overlay = null;
  var timers = [];
  var elapsedTimer = null;

  function build() {
    if (overlay) { return overlay; }
    overlay = document.createElement('div');
    overlay.className = 'pp-overlay';
    overlay.setAttribute('role', 'status');
    overlay.setAttribute('aria-live', 'polite');
    overlay.innerHTML =
      '<div class="pp-panel">' +
        '<h2 class="pp-title" id="ppTitle">Processing</h2>' +
        '<p class="pp-sub" id="ppSub"></p>' +
        '<ul class="pp-steps" id="ppSteps"></ul>' +
        '<p class="pp-note" id="ppNote"></p>' +
        '<div class="pp-foot" id="ppFoot">' +
          '<span>Working&hellip;</span>' +
          '<span class="pp-elapsed" id="ppElapsed">0:00</span>' +
        '</div>' +
      '</div>';
    // textContent, not innerHTML: this copy is editable in Settings and must
    // never be able to put markup into the page.
    overlay.querySelector('#ppSub').textContent = TEXT.subtitle;
    overlay.querySelector('#ppNote').textContent = TEXT.slowNotice;
    if (CONFIG.show_elapsed === false) {
      overlay.querySelector('#ppFoot').style.display = 'none';
    }
    document.body.appendChild(overlay);
    return overlay;
  }

  function uploadWeight(form) {
    /* Total megabytes queued in this form's file inputs, so a 40 MB upload
       paces its stages more slowly than a 200 KB one. */
    var bytes = 0;
    var inputs = form.querySelectorAll('input[type="file"]');
    for (var i = 0; i < inputs.length; i++) {
      var files = inputs[i].files;
      if (!files) { continue; }
      for (var j = 0; j < files.length; j++) {
        bytes += files[j].size || 0;
      }
    }
    return bytes / (1024 * 1024);
  }

  function stepDuration(megabytes) {
    // ~1.1s baseline plus roughly a second per megabyte, clamped both ways.
    var ms = BASE_STEP_MS + megabytes * 950;
    return Math.max(MIN_STEP_MS, Math.min(MAX_STEP_MS, ms));
  }

  function clearTimers() {
    timers.forEach(clearTimeout);
    timers = [];
    if (elapsedTimer) {
      clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
  }

  function show(form, key) {
    var config = STAGES[key];
    if (!config) { return; }

    build();
    clearTimers();

    document.getElementById('ppTitle').textContent = config.title;
    var note = document.getElementById('ppNote');
    note.classList.remove('is-shown');

    var list = document.getElementById('ppSteps');
    list.innerHTML = '';
    config.steps.forEach(function (label) {
      var item = document.createElement('li');
      item.className = 'pp-step';
      item.innerHTML =
        '<span class="pp-marker"><span class="pp-dot"></span></span>' +
        '<span class="pp-label"></span>';
      item.querySelector('.pp-label').textContent = label;
      list.appendChild(item);
    });

    var items = list.querySelectorAll('.pp-step');
    var perStep = stepDuration(uploadWeight(form));

    // First stage starts immediately; the rest follow on the estimate. The
    // final stage is never marked done - only the server's reply ends it.
    items[0].classList.add('is-active');
    for (var i = 1; i < items.length; i++) {
      (function (index) {
        timers.push(setTimeout(function () {
          items[index - 1].classList.remove('is-active');
          items[index - 1].classList.add('is-done');
          items[index].classList.add('is-active');
        }, perStep * index));
      })(i);
    }

    // Once the estimate is spent, say so instead of implying it is stuck.
    timers.push(setTimeout(function () {
      note.classList.add('is-shown');
    }, perStep * items.length + 4000));

    var started = Date.now();
    var elapsed = document.getElementById('ppElapsed');
    elapsedTimer = setInterval(function () {
      var seconds = Math.floor((Date.now() - started) / 1000);
      var minutes = Math.floor(seconds / 60);
      var rest = seconds % 60;
      elapsed.textContent = minutes + ':' + (rest < 10 ? '0' : '') + rest;
    }, 1000);

    overlay.classList.add('is-open');
  }

  function hide() {
    clearTimers();
    if (overlay) {
      overlay.classList.remove('is-open');
    }
  }

  /* One delegated listener on the document rather than one per form.
   *
   * The home page builds its split view by assigning toolPanel.innerHTML, which
   * replaces the tool's markup with brand new nodes. Listeners bound to the
   * forms at page load do not survive that, which is why Smart Matcher and the
   * Session Report Generator showed nothing while the full-page Configure step
   * worked. Delegation covers forms that appear at any point.
   *
   * Bubble phase is deliberate: a form's own submit handler runs first, so
   * defaultPrevented below correctly reflects a cancelled submission.
   */
  function onSubmit(event) {
    var form = event.target;
    if (!form || form.nodeName !== 'FORM') { return; }

    // Switched off in Settings.
    if (!isEnabled()) { return; }

    var key = form.getAttribute('data-progress');
    if (!key || !STAGES[key]) { return; }

    // Someone else cancelled it - failed validation, or an anonymous visitor
    // being sent to the login page. Nothing is going to the server.
    if (event.defaultPrevented) { return; }

    // A form the browser itself rejected never reaches the server either.
    if (typeof form.checkValidity === 'function' && !form.checkValidity()) {
      return;
    }

    show(form, key);
  }

  document.addEventListener('submit', onSubmit);

  // Coming back via the browser's back button restores the page from cache
  // with the overlay still up; clear it so the form is usable again.
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
      hide();
    }
  });

  window.ProcessingProgress = { show: show, hide: hide };
})();
