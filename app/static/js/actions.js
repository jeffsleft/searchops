/* Declarative UI actions (replaces inline onclick/onchange handlers).
 *
 * The Content-Security-Policy allows scripts only from /static and nonce'd
 * <script> blocks, so inline event handlers can't run. Elements declare what
 * they do with data- attributes and this one document-level listener acts on
 * them, including content htmx swaps in later.
 *
 *   data-href="/job/12"          click navigates (row links); clicks on links,
 *                                buttons, form controls or [data-stop] don't
 *   data-stop                    clicks inside don't trigger an outer data-href
 *                                or data-call row action
 *   data-call="openJobDrawer"    click calls an allowed window function, with
 *   data-call-arg="12"             a string argument, or
 *   data-call-with="self|row"      the element itself / its closest <tr>
 *   data-toggle-display="#id"    click toggles an element between hidden and
 *   data-display="flex"            the given display value (default block)
 *   data-details-open="#id"      click opens (or data-details-close closes) a <details>
 *   data-submit-on-change        change submits the element's form
 *   data-check-toggles=".qrow|answered"  checkbox toggles a class on its ancestor
 *   data-open-window="/url"      click opens the URL in a new tab
 *   data-remove-parent-on-self-click  click on this exact element (a backdrop)
 *                                removes its parent
 *   data-hide-closest=".sel"     click hides the closest ancestor matching .sel
 *   data-hide-related=".anc|.el" click hides .el inside the closest .anc ancestor
 *   data-hide="#id"              click hides that element
 */
(function () {
  // Only these globals can be called from markup.
  var CALLABLE = [
    'openJobDrawer', 'closeJobDrawer', 'openRecruitersPanel', 'closeRecruitersPanel',
    'openOutreachPanel', 'closeOutreachPanel', 'openTargetsPanel', 'closeTargetsPanel',
    'openDiscoveredPanel', 'closeDiscoveredPanel', 'showScoringToast', 'toggleSection', 'print'
  ];
  var INTERACTIVE = 'a, button, input, select, textarea, label, form, [data-stop]';

  function callAction(el) {
    var name = el.dataset.call;
    if (CALLABLE.indexOf(name) === -1 || typeof window[name] !== 'function') return;
    var with_ = el.dataset.callWith;
    var arg = with_ === 'self' ? el : with_ === 'row' ? el.closest('tr') : el.dataset.callArg;
    if (arg === undefined) window[name](); else window[name](arg);
  }

  function onClick(e) {
    var t = e.target;

    var backdrop = t.closest('[data-remove-parent-on-self-click]');
    if (backdrop && e.target === backdrop && window.htmx) { htmx.remove(backdrop.parentElement); return; }

    var hide = t.closest('[data-hide-closest]');
    if (hide) {
      var box = hide.closest(hide.dataset.hideClosest);
      if (box) box.style.display = 'none';
    }
    var rel = t.closest('[data-hide-related]');
    if (rel) {
      var p = rel.dataset.hideRelated.split('|');
      var anc = rel.closest(p[0]);
      var el = anc && anc.querySelector(p[1]);
      if (el) el.style.display = 'none';
    }
    var hideId = t.closest('[data-hide]');
    if (hideId) { var h = document.querySelector(hideId.dataset.hide); if (h) h.style.display = 'none'; }

    var tog = t.closest('[data-toggle-display]');
    if (tog) {
      var target = document.querySelector(tog.dataset.toggleDisplay);
      if (target) target.style.display = target.style.display === 'none' ? (tog.dataset.display || 'block') : 'none';
    }

    var open = t.closest('[data-details-open]');
    if (open) { var d1 = document.querySelector(open.dataset.detailsOpen); if (d1) d1.setAttribute('open', ''); }
    var close = t.closest('[data-details-close]');
    if (close) { var d2 = document.querySelector(close.dataset.detailsClose); if (d2) d2.removeAttribute('open'); }

    var win = t.closest('[data-open-window]');
    if (win) { window.open(win.dataset.openWindow, '_blank'); return; }

    var caller = t.closest('[data-call]');
    if (caller) {
      // A row-level action shouldn't fire for clicks on controls inside the row,
      // unless the control itself is the one carrying data-call.
      var inner = t.closest(INTERACTIVE);
      if (inner && inner !== caller && caller.contains(inner)) {
        // fall through: the inner control handles its own click
      } else {
        if (caller.hasAttribute('data-stop')) e.stopPropagation();
        callAction(caller);
        return;
      }
    }

    // Navigate unless the click landed on a control *inside* the link element
    // (the link element may itself be a button, e.g. a palette row).
    var link = t.closest('[data-href]');
    if (link) {
      var ctl = t.closest(INTERACTIVE);
      if (!ctl || ctl === link || !link.contains(ctl)) window.location = link.dataset.href;
    }
  }
  document.addEventListener('click', onClick);

  // [data-stop] must stop the click at the element itself, like the old inline
  // event.stopPropagation(): htmx listens on the row (<tr hx-get>), which a
  // document-level listener would only see after the row already fired. The
  // element's own actions run here first, since the document never gets the click.
  function guardStops(root) {
    var nodes = root.querySelectorAll ? root.querySelectorAll('[data-stop]') : [];
    var list = Array.prototype.slice.call(nodes);
    if (root.matches && root.matches('[data-stop]')) list.push(root);
    list.forEach(function (n) {
      if (n.__stopGuard) return;
      n.__stopGuard = true;
      n.addEventListener('click', function (e) { onClick(e); e.stopPropagation(); });
    });
  }
  if (window.htmx) htmx.onLoad(guardStops);  // initial page and every swap
  else document.addEventListener('DOMContentLoaded', function () { guardStops(document); });

  document.addEventListener('change', function (e) {
    var t = e.target;
    if (t.matches('[data-submit-on-change]')) {
      var form = t.closest('form');
      if (form) form.requestSubmit();
    }
    if (t.matches('[data-check-toggles]')) {
      var parts = t.dataset.checkToggles.split('|');
      var anc = t.closest(parts[0]);
      if (anc) anc.classList.toggle(parts[1], t.checked);
    }
  });
})();
