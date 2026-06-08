/* Shared C3 control helper for poledash (window.poleControl).
 *
 * One place to fire a C3 write command and then *verify* it actually took
 * effect, so the dashboard and the Battery&Charging panel don't each
 * re-implement it. Mirrors map.js: a plain IIFE, descriptive names only (the
 * date-fns CDN bundle squats one-letter globals — see the project conventions).
 *
 * Flow per button: POST /api/control → "sending" → for verifiable commands,
 * poll GET /api/control/verify a few times with backoff → ✓ verified /
 * ✗ not confirmed; unverifiable commands (set_target_soc, flash, …) settle on
 * "sent — can't verify". All user-facing words come from window.POLE_CONTROL_I18N
 * (set by the including partial via t()), so this file stays English-literal-free.
 */
(function () {
  var I18N = window.POLE_CONTROL_I18N || {};
  // Verify backoff: the car needs a few seconds to physically actuate (windows
  // ~10 s). Each tick forces one fresh C3 read (shared rate-limit bucket), so
  // keep it to three, never a tight loop.
  var VERIFY_DELAYS_MS = [3000, 6000, 9000];

  function txt(key) { return I18N[key] || key; }

  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Collect every [data-pkey] input inside a panel into a params object. Extra
  // params are harmless — each command builder reads only the keys it needs.
  function collectParams(root) {
    var params = {};
    if (!root) return params;
    root.querySelectorAll('[data-pkey]').forEach(function (el) {
      var key = el.getAttribute('data-pkey');
      var raw = el.value;
      var num = Number(raw);
      params[key] = (raw !== '' && !isNaN(num)) ? num : raw;
    });
    return params;
  }

  function setStatus(statusEl, html) {
    if (statusEl) statusEl.innerHTML = html;
  }

  function showVerdict(statusEl, label, verified) {
    if (verified === true) {
      setStatus(statusEl, '<span class="accent">' + escHtml(label) + ' · ' +
        escHtml(txt('verified')) + '</span>');
    } else if (verified === false) {
      setStatus(statusEl, '<span style="color:var(--ujh-alert)">' +
        escHtml(label) + ' · ' + escHtml(txt('notConfirmed')) + '</span>');
    } else {
      setStatus(statusEl, escHtml(label) + ' · ' + escHtml(txt('sentCantVerify')));
    }
  }

  function verify(command, params, statusEl, label) {
    var attempt = 0;
    var last = null; // best-known verified value across attempts
    function tick() {
      var qs = '?command=' + encodeURIComponent(command);
      if (params && Object.keys(params).length) {
        qs += '&params=' + encodeURIComponent(JSON.stringify(params));
      }
      fetch('/api/control/verify' + qs).then(function (r) { return r.json(); })
        .then(function (j) {
          if (j && j.verified === true) { showVerdict(statusEl, label, true); return; }
          if (j && j.verified === false) { last = false; }
          else if (j && j.busy) { /* throttled — retry on schedule */ }
          else if (j && j.verified === null) { last = (last === false) ? false : null; }
          next();
        }).catch(function () { next(); });
    }
    function next() {
      if (attempt >= VERIFY_DELAYS_MS.length) { showVerdict(statusEl, label, last); return; }
      var d = VERIFY_DELAYS_MS[attempt++];
      setStatus(statusEl, escHtml(label) + ' · ' + escHtml(txt('verifying')));
      setTimeout(tick, d);
    }
    next();
  }

  // Fire one command. opts: {statusEl, label, verifiable, onDone}.
  function send(command, params, opts) {
    opts = opts || {};
    var statusEl = opts.statusEl;
    var label = opts.label || command;
    setStatus(statusEl, escHtml(label) + ' · ' + escHtml(txt('sending')));
    return fetch('/api/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: command, params: params || {} })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (!j || !j.ok) {
        setStatus(statusEl, '<span style="color:var(--ujh-alert)">' +
          escHtml(label) + ' · ' + escHtml(txt('failed')) + ' · ' +
          escHtml((j && (j.status || j.error)) || '') + '</span>');
        return j;
      }
      if (typeof opts.onDone === 'function') opts.onDone(j);
      if (opts.verifiable === false) {
        showVerdict(statusEl, label, null);
      } else {
        verify(command, params, statusEl, label);
      }
      return j;
    }).catch(function (e) {
      setStatus(statusEl, escHtml(txt('neterr')) + escHtml(e));
    });
  }

  // Wire up every [data-cmd] button inside `root`. The panel's shared status
  // line is [data-cmd-status]. A button may carry data-confirm and
  // data-verifiable="false". onDone (optional) runs after a successful POST —
  // the dashboard uses it to repaint the car diagram.
  function bind(root, onDone) {
    if (!root) return;
    var statusEl = root.querySelector('[data-cmd-status]');
    root.querySelectorAll('[data-cmd]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var command = btn.getAttribute('data-cmd');
        var label = (btn.getAttribute('data-label') || btn.textContent || command).trim();
        if (btn.hasAttribute('data-confirm')) {
          if (!window.confirm(txt('confirmRun') + ': ' + label + '?')) return;
        }
        var params = collectParams(root);
        send(command, params, {
          statusEl: statusEl,
          label: label,
          verifiable: btn.getAttribute('data-verifiable') !== 'false',
          onDone: onDone
        });
      });
    });
  }

  window.poleControl = { send: send, verify: verify, bind: bind, collectParams: collectParams };
})();
