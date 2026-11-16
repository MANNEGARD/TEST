/* Rumslig navigering för fjärrkontroll: piltangenterna flyttar fokus till närmaste element
   i den riktningen. Fungerar med webOS fjärrkontroll, tangentbord och Magic Remote (pekare). */
(function (global) {
  'use strict';

  var KEY = {
    LEFT: 37, UP: 38, RIGHT: 39, DOWN: 40, ENTER: 13,
    BACK: 461, ESC: 27, BACKSPACE: 8,
    RED: 403, GREEN: 404, YELLOW: 405, BLUE: 406,
    PLAY: 415, PAUSE: 19, PLAYPAUSE: 10252, STOP: 413, FF: 417, RW: 412
  };

  var current = null;
  var scopeFn = function () { return document.body; };

  function visible(el) {
    if (!el || el.disabled) return false;
    var r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    var n = el;
    while (n && n !== document.body) {
      if (n.hidden) return false;
      n = n.parentNode;
    }
    return true;
  }

  function candidates() {
    var scope = scopeFn();
    var list = scope.querySelectorAll('.focusable');
    var out = [];
    for (var i = 0; i < list.length; i++) if (visible(list[i])) out.push(list[i]);
    return out;
  }

  function focus(el, opts) {
    if (!el) return;
    if (current && current !== el) {
      current.classList.remove('focused');
      if (document.activeElement === current && current.tagName !== 'INPUT') current.blur();
    }
    current = el;
    el.classList.add('focused');
    if (el.tagName === 'INPUT') {
      // På TV:n visas tangentbordet först när användaren trycker OK, inte bara av att fokus hamnar här.
      if (opts && opts.edit) el.focus();
      else if (document.activeElement === el) { /* redan aktivt */ } else if (document.activeElement) document.activeElement.blur();
    }
    scrollIntoView(el);
    if (global.Nav.onFocus) global.Nav.onFocus(el);
  }

  function scrollIntoView(el) {
    var sc = el.parentNode;
    while (sc && sc !== document.body) {
      var st = global.getComputedStyle(sc);
      if (/(auto|scroll)/.test(st.overflowY) && sc.scrollHeight > sc.clientHeight) break;
      sc = sc.parentNode;
    }
    if (!sc || sc === document.body) return;
    var r = el.getBoundingClientRect();
    var s = sc.getBoundingClientRect();
    var margin = 40;
    if (r.top < s.top + margin) sc.scrollTop -= (s.top + margin - r.top);
    else if (r.bottom > s.bottom - margin) sc.scrollTop += (r.bottom - (s.bottom - margin));
  }

  function center(r) { return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }

  function move(dir) {
    var list = candidates();
    if (!list.length) return false;
    if (!current || list.indexOf(current) < 0) { focus(preferred(list)); return true; }

    var from = current.getBoundingClientRect();
    var c = center(from);
    var best = null, bestScore = Infinity;
    var vertical = dir === 'up' || dir === 'down';
    var rows = [];

    list.forEach(function (el) {
      if (el === current) return;
      var r = el.getBoundingClientRect();
      var p = center(r);
      var dx = p.x - c.x, dy = p.y - c.y;
      if (!vertical) {
        if (dir === 'left' ? r.right > from.left + 2 : r.left < from.right - 2) return;
        var overlap = Math.min(r.bottom, from.bottom) - Math.max(r.top, from.top);
        if (overlap <= 0) return; // vänster/höger stannar på samma rad
        var score = Math.abs(dx) + Math.abs(dy) * 0.4;
        if (score < bestScore) { bestScore = score; best = el; }
      } else {
        if (dir === 'up' ? r.bottom > from.top + 2 : r.top < from.bottom - 2) return;
        // Avstånd till raden: närmaste rad vinner, sedan närmast i sidled.
        var gap = dir === 'down' ? r.top - from.bottom : from.top - r.bottom;
        var hOverlap = Math.min(r.right, from.right) - Math.max(r.left, from.left);
        var side = hOverlap > 0 ? 0 : Math.min(Math.abs(r.left - from.right), Math.abs(from.left - r.right));
        rows.push({ el: el, gap: gap, side: side, dx: Math.abs(dx) });
      }
    });

    if (vertical && rows.length) {
      var minGap = Infinity;
      rows.forEach(function (x) { if (x.gap < minGap) minGap = x.gap; });
      rows.forEach(function (x) {
        if (x.gap > minGap + 24) return;
        var score = x.side * 4 + x.dx;
        if (score < bestScore) { bestScore = score; best = x.el; }
      });
    }

    if (best && global.Nav.redirect) best = global.Nav.redirect(best, current, dir) || best;
    if (best) { focus(best); return true; }
    return false;
  }

  function preferred(list) {
    for (var i = 0; i < list.length; i++) if (list[i].getAttribute('data-autofocus') !== null) return list[i];
    return list[0];
  }

  function reset(el) {
    if (current) current.classList.remove('focused');
    current = null;
    var list = candidates();
    if (el && list.indexOf(el) >= 0) focus(el);
    else if (list.length) focus(preferred(list));
  }

  // Muspekare (Magic Remote) flyttar fokus dit den pekar.
  document.addEventListener('mouseover', function (e) {
    var t = e.target;
    while (t && t !== document && !(t.classList && t.classList.contains('focusable'))) t = t.parentNode;
    if (t && t !== document && t !== current && scopeFn().contains(t)) focus(t);
  });

  global.Nav = {
    KEY: KEY,
    focus: focus,
    move: move,
    reset: reset,
    current: function () { return current; },
    setScope: function (fn) { scopeFn = fn; },
    onFocus: null,
    redirect: null
  };
})(window);
