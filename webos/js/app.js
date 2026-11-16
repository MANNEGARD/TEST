/* SMGD Hem – TV-app för Home Assistant (LG webOS, men fungerar i alla moderna webbläsare). */
(function () {
  'use strict';

  var K = Nav.KEY;
  var STORE = 'smgdhem.';
  var LONG_PRESS_MS = 600;

  /* ---------- Tillstånd ---------- */
  var auth = null;
  var conn = null;
  var fileConfig = null;
  var states = {};
  var entReg = {};
  var devReg = {};
  var areas = {};
  var areaOrder = [];
  var locationName = '';
  var favs = load('favs', []);
  var currentTab = load('tab', null);
  var detailId = null;       // entitet som visas i detaljpanelen
  var panelKind = null;      // 'detail' | 'settings'
  var cardEls = {};
  var hold = {};             // lokala värden för reglage medan användaren justerar
  var renderTimer = null;
  var everConnected = false;
  var passwordMode = true;
  var mfaFlow = null;

  /* ---------- Hjälpfunktioner ---------- */
  function $(id) { return document.getElementById(id); }
  function load(k, def) {
    try { var v = localStorage.getItem(STORE + k); return v === null ? def : JSON.parse(v); } catch (e) { return def; }
  }
  function save(k, v) {
    try { if (v === null || v === undefined) localStorage.removeItem(STORE + k); else localStorage.setItem(STORE + k, JSON.stringify(v)); } catch (e) { /* privat läge */ }
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function domainOf(id) { return id.split('.')[0]; }
  function attr(st, k) { return st && st.attributes ? st.attributes[k] : undefined; }
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function fmtNum(v, dec) {
    var n = Number(v);
    if (isNaN(n)) return String(v);
    if (dec === undefined) dec = Math.abs(n) >= 100 || n % 1 === 0 ? 0 : 1;
    return n.toFixed(dec).replace('.', ',');
  }
  function isNumeric(v) { return v !== '' && v !== null && !isNaN(Number(v)); }
  function absUrl(u) {
    if (!u) return '';
    return /^https?:/i.test(u) ? u : auth.hassUrl + u;
  }
  function ago(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) return '';
    var s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return 'nyss';
    if (s < 3600) return 'för ' + Math.round(s / 60) + ' min sedan';
    if (s < 86400) return 'för ' + Math.round(s / 3600) + ' tim sedan';
    var d = Math.round(s / 86400);
    return 'för ' + d + (d === 1 ? ' dag' : ' dagar') + ' sedan';
  }

  var toastTimer = null;
  function toast(msg, bad) {
    var t = $('toast');
    t.textContent = msg;
    t.className = 'toast' + (bad ? ' bad' : '');
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, bad ? 4500 : 2200);
  }

  function splash(text) {
    var s = $('splash');
    if (text === false) { s.className = 'splash gone'; return; }
    $('splash-text').textContent = text;
    s.className = 'splash';
  }

  /* ---------- Entiteter: namn, tillstånd, ikoner ---------- */
  var SUPPORTED = ['light', 'switch', 'input_boolean', 'fan', 'automation', 'cover', 'lock', 'climate',
    'water_heater', 'humidifier', 'media_player', 'scene', 'script', 'button', 'input_button', 'sensor',
    'binary_sensor', 'person', 'weather', 'camera', 'vacuum', 'alarm_control_panel', 'select',
    'input_select', 'number', 'input_number', 'siren', 'valve', 'lawn_mower'];

  var TOGGLE = ['light', 'switch', 'input_boolean', 'fan', 'automation', 'siren', 'humidifier'];
  var CONTROL_ORDER = ['light', 'switch', 'input_boolean', 'fan', 'cover', 'valve', 'climate', 'water_heater',
    'humidifier', 'media_player', 'lock', 'vacuum', 'lawn_mower', 'alarm_control_panel', 'camera', 'scene',
    'script', 'button', 'input_button', 'select', 'input_select', 'number', 'input_number', 'automation', 'siren'];

  function areaOf(eid) {
    var e = entReg[eid];
    if (!e) return null;
    if (e.area_id) return e.area_id;
    var d = e.device_id && devReg[e.device_id];
    return d ? d.area_id : null;
  }

  function friendly(eid, stripArea) {
    var st = states[eid];
    var n = attr(st, 'friendly_name') || eid.split('.')[1].replace(/_/g, ' ');
    if (stripArea) {
      var a = areas[stripArea];
      if (a && n.toLowerCase().indexOf(a.name.toLowerCase() + ' ') === 0 && n.length > a.name.length + 1) {
        n = n.substr(a.name.length + 1);
        n = n.charAt(0).toUpperCase() + n.substr(1);
      }
    }
    return n;
  }

  function visibleEntity(eid) {
    if (SUPPORTED.indexOf(domainOf(eid)) < 0) return false;
    var e = entReg[eid];
    if (e && (e.hidden_by || e.disabled_by || e.entity_category || e.hidden)) return false;
    return true;
  }

  var BIN_LABELS = {
    door: ['Öppen', 'Stängd'], window: ['Öppet', 'Stängt'], opening: ['Öppen', 'Stängd'], garage_door: ['Öppen', 'Stängd'],
    motion: ['Rörelse', 'Ingen rörelse'], occupancy: ['Upptaget', 'Ledigt'], presence: ['Hemma', 'Borta'],
    moisture: ['Fukt!', 'Torrt'], smoke: ['Rök!', 'OK'], gas: ['Gas!', 'OK'], carbon_monoxide: ['Kolmonoxid!', 'OK'],
    battery: ['Lågt', 'OK'], connectivity: ['Ansluten', 'Frånkopplad'], problem: ['Problem', 'OK'],
    lock: ['Olåst', 'Låst'], plug: ['Inkopplad', 'Urkopplad'], power: ['Ström', 'Ingen ström'],
    running: ['Igång', 'Stoppad'], safety: ['Osäkert', 'Säkert'], sound: ['Ljud', 'Tyst'], vibration: ['Vibration', 'Stilla'],
    tamper: ['Manipulerad', 'OK'], light: ['Ljus', 'Mörkt'], heat: ['Varm', 'Normal'], cold: ['Kall', 'Normal'],
    battery_charging: ['Laddar', 'Laddar inte'], update: ['Uppdatering finns', 'Aktuell']
  };
  var ALERT_BIN = ['moisture', 'smoke', 'gas', 'carbon_monoxide', 'problem', 'safety', 'tamper', 'battery'];

  var WORDS = {
    on: 'På', off: 'Av', unavailable: 'Ej tillgänglig', unknown: 'Okänt',
    open: 'Öppen', closed: 'Stängd', opening: 'Öppnas', closing: 'Stängs',
    locked: 'Låst', unlocked: 'Olåst', locking: 'Låser…', unlocking: 'Låser upp…', jammed: 'Fastnat!', open_lock: 'Öppen',
    playing: 'Spelar', paused: 'Pausad', idle: 'Inaktiv', standby: 'Viloläge', buffering: 'Laddar…',
    home: 'Hemma', not_home: 'Borta',
    heat: 'Värme', cool: 'Kyla', heat_cool: 'Värme/kyla', auto: 'Auto', dry: 'Avfuktning', fan_only: 'Fläkt',
    heating: 'Värmer', cooling: 'Kyler', drying: 'Avfuktar', fan: 'Fläkt', preheating: 'Förvärmer', defrosting: 'Avfrostar',
    cleaning: 'Städar', docked: 'Dockad', returning: 'Åker hem', error: 'Fel', mowing: 'Klipper',
    disarmed: 'Avlarmat', armed_home: 'Larmat hemma', armed_away: 'Larmat borta', armed_night: 'Larmat natt',
    armed_vacation: 'Larmat semester', armed_custom_bypass: 'Larmat', arming: 'Larmar…', disarming: 'Avlarmar…',
    pending: 'Väntar…', triggered: 'LARM!',
    recording: 'Spelar in', streaming: 'Direkt',
    'clear-night': 'Klart', cloudy: 'Mulet', fog: 'Dimma', hail: 'Hagel', lightning: 'Åska', 'lightning-rainy': 'Åskregn',
    partlycloudy: 'Halvklart', pouring: 'Skyfall', rainy: 'Regn', snowy: 'Snö', 'snowy-rainy': 'Snöblandat regn',
    sunny: 'Sol', windy: 'Blåsigt', 'windy-variant': 'Blåsigt', exceptional: 'Extremt väder'
  };
  function word(s) { return WORDS[s] || s; }

  function stateText(eid) {
    var st = states[eid];
    if (!st) return '';
    var s = st.state, d = domainOf(eid), dc = attr(st, 'device_class');
    if (s === 'unavailable' || s === 'unknown') return word(s);
    switch (d) {
      case 'light':
        if (s === 'on' && attr(st, 'brightness') !== undefined && attr(st, 'brightness') !== null) {
          return 'På · ' + Math.round(attr(st, 'brightness') / 2.55) + ' %';
        }
        return word(s);
      case 'fan':
        return s === 'on' && attr(st, 'percentage') ? 'På · ' + attr(st, 'percentage') + ' %' : word(s);
      case 'cover': case 'valve':
        var pos = attr(st, 'current_position');
        return (s === 'open' && pos !== undefined && pos !== 100) ? 'Öppen · ' + pos + ' %' : word(s);
      case 'climate':
        var cur = attr(st, 'current_temperature');
        var action = attr(st, 'hvac_action');
        var label = action && action !== 'idle' && action !== 'off' ? word(action) : word(s);
        return label + (cur !== undefined && cur !== null ? ' · ' + fmtNum(cur, 1) + ' °C' : '');
      case 'water_heater':
        return word(s) + (attr(st, 'temperature') ? ' · ' + fmtNum(attr(st, 'temperature')) + ' °C' : '');
      case 'media_player':
        var title = attr(st, 'media_title');
        if ((s === 'playing' || s === 'paused') && title) return (s === 'paused' ? 'Pausad: ' : '') + title;
        return word(s) + (attr(st, 'app_name') && s !== 'off' ? ' · ' + attr(st, 'app_name') : '');
      case 'scene': return 'Aktivera';
      case 'script': return s === 'on' ? 'Körs…' : 'Kör';
      case 'button': case 'input_button': return 'Tryck';
      case 'binary_sensor':
        var l = BIN_LABELS[dc];
        return l ? (s === 'on' ? l[0] : l[1]) : word(s);
      case 'person': return word(s);
      case 'weather':
        var t = attr(st, 'temperature');
        return word(s) + (t !== undefined ? ' · ' + fmtNum(t) + ' ' + (attr(st, 'temperature_unit') || '°C') : '');
      case 'sensor': case 'number': case 'input_number':
        var u = attr(st, 'unit_of_measurement');
        if (dc === 'timestamp') return ago(s);
        return (isNumeric(s) ? fmtNum(s) : s) + (u ? ' ' + u : '');
      case 'camera': return word(s);
      default: return word(s);
    }
  }

  function isOn(eid) {
    var st = states[eid];
    if (!st) return false;
    var s = st.state, d = domainOf(eid);
    switch (d) {
      case 'media_player': return s === 'playing' || s === 'paused' || s === 'on' || s === 'buffering';
      case 'cover': case 'valve': return s === 'open' || s === 'opening';
      case 'climate': return s !== 'off';
      case 'person': return s === 'home';
      case 'vacuum': return s === 'cleaning' || s === 'returning';
      case 'lawn_mower': return s === 'mowing';
      case 'lock': return s === 'locked';
      case 'alarm_control_panel': return s.indexOf('armed') === 0;
      case 'sensor': case 'weather': case 'scene': case 'button': case 'input_button': case 'camera':
      case 'select': case 'input_select': case 'number': case 'input_number': return false;
      default: return s === 'on';
    }
  }

  function isAlert(eid) {
    var st = states[eid];
    if (!st) return false;
    var d = domainOf(eid);
    if (d === 'lock') return st.state === 'unlocked' || st.state === 'jammed' || st.state === 'open';
    if (d === 'alarm_control_panel') return st.state === 'triggered';
    if (d === 'binary_sensor') return st.state === 'on' && ALERT_BIN.indexOf(attr(st, 'device_class')) >= 0;
    return false;
  }

  var WEATHER_ICON = { sunny: 'sun', 'clear-night': 'moon', cloudy: 'cloud', partlycloudy: 'cloud', fog: 'cloud',
    rainy: 'rain', pouring: 'rain', 'lightning-rainy': 'rain', lightning: 'bolt', hail: 'rain', snowy: 'snow',
    'snowy-rainy': 'snow', windy: 'cloud', 'windy-variant': 'cloud' };

  function iconFor(eid) {
    var st = states[eid], d = domainOf(eid), dc = attr(st, 'device_class');
    switch (d) {
      case 'light': return 'bulb';
      case 'switch': return dc === 'outlet' ? 'plug' : 'power';
      case 'input_boolean': return 'toggle';
      case 'automation': return 'robot';
      case 'fan': return 'fan';
      case 'cover': case 'valve': return dc === 'door' || dc === 'garage' || dc === 'gate' ? 'door' : 'blind';
      case 'lock': return st && st.state === 'locked' ? 'lock' : 'unlock';
      case 'climate': case 'water_heater': return 'thermo';
      case 'humidifier': return 'drop';
      case 'media_player': return 'tv';
      case 'scene': return 'star';
      case 'script': return 'script';
      case 'button': case 'input_button': return 'button';
      case 'camera': return 'camera';
      case 'vacuum': case 'lawn_mower': return 'vacuum';
      case 'alarm_control_panel': return 'shield';
      case 'person': return 'person';
      case 'weather': return WEATHER_ICON[st && st.state] || 'sun';
      case 'binary_sensor':
        if (['door', 'window', 'opening', 'garage_door'].indexOf(dc) >= 0) return 'door';
        if (['motion', 'occupancy', 'presence'].indexOf(dc) >= 0) return 'eye';
        if (dc === 'moisture') return 'drop';
        if (dc === 'battery' || dc === 'battery_charging') return 'battery';
        return 'sensor';
      case 'sensor':
        if (dc === 'temperature') return 'thermo';
        if (dc === 'humidity' || dc === 'moisture') return 'drop';
        if (dc === 'power' || dc === 'energy' || dc === 'voltage' || dc === 'current') return 'bolt';
        if (dc === 'battery') return 'battery';
        if (dc === 'distance') return 'car';
        return 'sensor';
      default: return 'gear';
    }
  }

  /* ---------- Flikar ---------- */
  function sensorOf(classes) {
    return function (eid) {
      var d = domainOf(eid);
      return (d === 'sensor' || d === 'binary_sensor') && classes.indexOf(attr(states[eid], 'device_class')) >= 0;
    };
  }
  var TABS = [
    { id: 'fav', title: 'Favoriter' },
    { id: 'rooms', title: 'Rum' },
    { id: 'lights', title: 'Lampor', domains: ['light'] },
    { id: 'switches', title: 'Reglage', domains: ['switch', 'input_boolean', 'fan', 'select', 'input_select', 'number', 'input_number', 'siren'] },
    { id: 'climate', title: 'Klimat', domains: ['climate', 'water_heater', 'humidifier'], extra: sensorOf(['temperature', 'humidity']) },
    { id: 'media', title: 'Media', domains: ['media_player'] },
    { id: 'security', title: 'Säkerhet', domains: ['lock', 'alarm_control_panel', 'cover', 'valve'],
      extra: sensorOf(['door', 'window', 'opening', 'garage_door', 'motion', 'smoke', 'moisture', 'gas', 'carbon_monoxide', 'tamper']) },
    { id: 'cameras', title: 'Kameror', domains: ['camera'] },
    { id: 'scenes', title: 'Scener', domains: ['scene', 'script', 'button', 'input_button'] },
    { id: 'more', title: 'Mer', domains: ['vacuum', 'lawn_mower', 'person', 'weather', 'automation'] },
    { id: 'sensors', title: 'Sensorer', domains: ['sensor', 'binary_sensor'] }
  ];

  function allVisible() {
    return Object.keys(states).filter(visibleEntity);
  }

  function byName(a, b) { return friendly(a).localeCompare(friendly(b), 'sv'); }
  function byControl(a, b) {
    var ia = CONTROL_ORDER.indexOf(domainOf(a)), ib = CONTROL_ORDER.indexOf(domainOf(b));
    if (ia < 0) ia = 99;
    if (ib < 0) ib = 99;
    return ia - ib || byName(a, b);
  }

  function tabEntities(tab) {
    var all = allVisible();
    if (tab.id === 'fav') return favs.filter(function (e) { return states[e]; });
    return all.filter(function (eid) {
      return (tab.domains && tab.domains.indexOf(domainOf(eid)) >= 0) || (tab.extra && tab.extra(eid));
    }).sort(byName);
  }

  function availableTabs() {
    return TABS.filter(function (t) {
      return t.id === 'fav' || t.id === 'rooms' || tabEntities(t).length > 0;
    });
  }

  function renderTabs() {
    var tabs = availableTabs();
    if (!currentTab || !tabs.some(function (t) { return t.id === currentTab; })) {
      currentTab = favs.length ? 'fav' : 'rooms';
    }
    var html = tabs.map(function (t) {
      return '<button class="tab focusable' + (t.id === currentTab ? ' active' : '') + '" data-action="tab" data-tab="' + t.id + '">' + esc(t.title) + '</button>';
    }).join('');
    html += '<button class="tab focusable" data-action="settings" title="Inställningar">' + Icons.svg('gear', 'tab-ic') + '</button>';
    $('tabs').innerHTML = html;
    var g = $('tabs').querySelector('.tab-ic');
    if (g) { g.style.width = '1.4rem'; g.style.height = '1.4rem'; g.style.fill = 'currentColor'; g.style.verticalAlign = '-.25rem'; }
  }

  function selectTab(id) {
    if (id === currentTab) return;
    currentTab = id;
    save('tab', id);
    var btns = $('tabs').querySelectorAll('[data-tab]');
    for (var i = 0; i < btns.length; i++) btns[i].classList.toggle('active', btns[i].getAttribute('data-tab') === id);
    renderContent();
    $('content').scrollTop = 0;
  }

  /* ---------- Kort ---------- */
  function cardClass(eid) {
    var d = domainOf(eid), st = states[eid];
    var c = 'card focusable';
    if (d === 'camera') c += ' camera';
    else if (d === 'weather' || d === 'media_player' || d === 'climate') c += ' big';
    if (isOn(eid)) c += ' on';
    if (isAlert(eid)) c += ' alert';
    if (st && st.state === 'unavailable') c += ' unavailable';
    return c;
  }

  function cardInner(eid, area) {
    var st = states[eid], d = domainOf(eid);
    var name = esc(friendly(eid, area));
    var fav = favs.indexOf(eid) >= 0 ? Icons.svg('star', 'fav') : '';
    if (d === 'camera') {
      return '<img alt="" src="' + esc(cameraUrl(eid)) + '">' + fav +
        '<div class="caption"><div class="name">' + name + '</div></div>';
    }
    var html = Icons.svg(iconFor(eid), 'ic') + fav;
    if (d === 'climate' || d === 'weather') {
      var t = d === 'climate' ? attr(st, 'current_temperature') : attr(st, 'temperature');
      var sub = d === 'climate'
        ? stateText(eid).split(' · ')[0] + (attr(st, 'temperature') !== undefined && attr(st, 'temperature') !== null ? ' · mål ' + fmtNum(attr(st, 'temperature'), 1) + ' °C' : '')
        : word(st.state);
      return html + '<div class="value">' + (t !== undefined && t !== null ? fmtNum(t, 1) + '<small>°C</small>' : '–') + '</div>' +
        '<div><div class="name">' + name + '</div><div class="state">' + esc(sub) + '</div></div>';
    }
    html += '<div><div class="name">' + name + '</div><div class="state">' + esc(stateText(eid)) + '</div></div>';
    var pct = null;
    if (d === 'light' && st.state === 'on' && attr(st, 'brightness')) pct = attr(st, 'brightness') / 2.55;
    if ((d === 'cover' || d === 'valve') && attr(st, 'current_position') !== undefined) pct = attr(st, 'current_position');
    if (d === 'media_player' && st.state !== 'off' && attr(st, 'volume_level') !== undefined) pct = attr(st, 'volume_level') * 100;
    if (pct !== null) html += '<div class="bar" style="width:' + Math.round(pct) + '%"></div>';
    return html;
  }

  function cardHtml(eid, area) {
    return '<button class="' + cardClass(eid) + '" data-action="card" data-eid="' + esc(eid) + '"' +
      (area ? ' data-area="' + esc(area) + '"' : '') + '>' + cardInner(eid, area) + '</button>';
  }

  function paintCard(el) {
    var eid = el.getAttribute('data-eid');
    if (!states[eid]) return;
    var area = el.getAttribute('data-area');
    var focused = el.classList.contains('focused');
    el.className = cardClass(eid) + (focused ? ' focused' : '');
    if (domainOf(eid) === 'camera') {
      // Byt inte ut bilden vid varje tillståndsändring – den uppdateras på egen klocka.
      var cap = el.querySelector('.caption .name');
      if (cap) cap.textContent = friendly(eid, area);
    } else {
      el.innerHTML = cardInner(eid, area);
    }
    var ic = el.querySelector('.ic');
    var rgb = attr(states[eid], 'rgb_color');
    if (ic && domainOf(eid) === 'light' && states[eid].state === 'on' && rgb) {
      ic.style.fill = 'rgb(' + rgb.join(',') + ')';
    }
  }

  function cameraUrl(eid) {
    var p = attr(states[eid], 'entity_picture');
    if (!p) return '';
    return absUrl(p) + (p.indexOf('?') >= 0 ? '&' : '?') + '_t=' + Math.floor(Date.now() / 5000);
  }

  function gridHtml(list, area) {
    return '<div class="grid">' + list.map(function (e) { return cardHtml(e, area); }).join('') + '</div>';
  }

  function renderContent() {
    var c = $('content');
    var focusedEid = Nav.current() && Nav.current().getAttribute('data-eid');
    var html = '';
    if (currentTab === 'rooms') {
      var all = allVisible();
      var groups = {};
      all.forEach(function (eid) {
        var a = areaOf(eid) || '_none';
        (groups[a] = groups[a] || []).push(eid);
      });
      areaOrder.forEach(function (aid) {
        var list = groups[aid];
        if (!list || !list.length) return;
        html += '<h2 class="section-title">' + esc(areas[aid].name) + '</h2>' + gridHtml(list.sort(byControl), aid);
      });
      var none = (groups._none || []).filter(function (e) {
        var d = domainOf(e);
        return d !== 'sensor' && d !== 'binary_sensor' && d !== 'automation' && d !== 'person' && d !== 'weather';
      });
      if (none.length) html += '<h2 class="section-title">Utan rum</h2>' + gridHtml(none.sort(byControl));
      if (!html) {
        html = '<p class="empty">Inga rum hittades.<br>Placera enheter i områden i Home Assistant (Inställningar → Områden),<br>eller bläddra bland flikarna ovan.</p>';
      }
    } else {
      var tab = TABS.filter(function (t) { return t.id === currentTab; })[0] || TABS[0];
      var ents = tabEntities(tab);
      if (!ents.length) {
        html = tab.id === 'fav'
          ? '<p class="empty">Inga favoriter än.<br>Markera ett kort och tryck på den <b style="color:#f5d90a">gula</b> knappen<br>(eller F på tangentbordet) för att lägga till det här.</p>'
          : '<p class="empty">Här finns inget att visa.</p>';
      } else {
        html = gridHtml(ents);
      }
    }
    c.innerHTML = html;

    cardEls = {};
    var cards = c.querySelectorAll('.card');
    for (var i = 0; i < cards.length; i++) {
      var eid = cards[i].getAttribute('data-eid');
      (cardEls[eid] = cardEls[eid] || []).push(cards[i]);
      paintCard(cards[i]);
    }
    // Behåll fokus på samma entitet om den fortfarande finns (t.ex. efter en ny favorit).
    if (focusedEid && cardEls[focusedEid] && panelKind === null) Nav.focus(cardEls[focusedEid][0]);
  }

  function scheduleRender() {
    clearTimeout(renderTimer);
    renderTimer = setTimeout(function () { renderTabs(); renderContent(); }, 300);
  }

  /* ---------- Sidhuvud ---------- */
  function tickClock() {
    var d = new Date();
    $('clock').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function renderWeather() {
    var w = Object.keys(states).filter(function (e) { return domainOf(e) === 'weather' && visibleEntity(e); })[0];
    var el = $('weather');
    if (!w) { el.innerHTML = ''; return; }
    var st = states[w];
    var t = attr(st, 'temperature');
    el.innerHTML = Icons.svg(WEATHER_ICON[st.state] || 'sun') + '<span>' +
      (t !== undefined ? fmtNum(t) + '°' : '') + ' ' + esc(word(st.state)) + '</span>';
  }

  function setConn(status) {
    var dot = $('conn-dot');
    dot.className = 'dot ' + (status === 'connected' ? 'ok' : status === 'connecting' ? 'wait' : 'bad');
    dot.title = status === 'connected' ? 'Ansluten' : status === 'connecting' ? 'Ansluter…' : 'Frånkopplad – försöker igen';
    if (status === 'disconnected' && everConnected) toast('Anslutningen till Home Assistant bröts – försöker igen…', true);
  }

  /* ---------- Tjänsteanrop ---------- */
  function call(domain, service, data, eid) {
    if (!conn) return Promise.reject(new Error('Inte ansluten'));
    return conn.callService(domain, service, data, eid).then(function (r) { return r; }, function (e) {
      toast('Misslyckades: ' + e.message, true);
      throw e;
    });
  }

  function primaryAction(eid) {
    var d = domainOf(eid), st = states[eid];
    if (!st) return;
    if (st.state === 'unavailable') { openDetail(eid); return; }
    if (TOGGLE.indexOf(d) >= 0) { call(d, 'toggle', {}, eid); return; }
    switch (d) {
      case 'cover': case 'valve':
        call(d, 'toggle', {}, eid); return;
      case 'scene':
        call('scene', 'turn_on', {}, eid).then(function () { toast(friendly(eid) + ' aktiverad'); }); return;
      case 'script':
        call('script', st.state === 'on' ? 'turn_off' : 'turn_on', {}, eid).then(function () {
          if (st.state !== 'on') toast(friendly(eid) + ' startat');
        }); return;
      case 'button': case 'input_button':
        call(d, 'press', {}, eid).then(function () { toast(friendly(eid) + ' – tryckt'); }); return;
      default:
        openDetail(eid);
    }
  }

  function toggleFav(eid) {
    var i = favs.indexOf(eid);
    if (i >= 0) { favs.splice(i, 1); toast('Borttagen från favoriter'); } else { favs.push(eid); toast('Tillagd i favoriter'); }
    save('favs', favs);
    if (currentTab === 'fav') renderContent();
    else (cardEls[eid] || []).forEach(paintCard);
    if (detailId === eid) renderDetail();
  }

  /* ---------- Detaljpanel ---------- */
  function btn(label, opts) {
    opts = opts || {};
    var a = ' data-action="' + (opts.action || 'svc') + '"';
    if (opts.domain) a += ' data-domain="' + opts.domain + '"';
    if (opts.service) a += ' data-service="' + opts.service + '"';
    if (opts.data) a += ' data-json="' + esc(JSON.stringify(opts.data)) + '"';
    if (opts.k) a += ' data-k="' + esc(opts.k) + '"';
    var cls = 'btn focusable' + (opts.cls ? ' ' + opts.cls : '') + (opts.icon && !label ? ' icon-only' : '');
    return '<button class="' + cls + '"' + a + (opts.title ? ' title="' + esc(opts.title) + '"' : '') + '>' +
      (opts.icon ? Icons.svg(opts.icon) : '') + (label ? '<span>' + esc(label) + '</span>' : '') + '</button>';
  }

  function slider(key, label, value, min, max, step, fmt) {
    var h = hold[detailId + ':' + key];
    if (h && h.until > Date.now()) value = h.value;
    var pct = max > min ? (value - min) / (max - min) * 100 : 0;
    return '<div class="row"><div class="row-label">' + esc(label) + '</div>' +
      '<div class="slider focusable" data-action="slider" data-k="' + key + '" data-min="' + min + '" data-max="' + max +
      '" data-step="' + step + '" data-value="' + value + '"' + (fmt ? ' data-fmt="' + fmt + '"' : '') + '>' +
      '<div class="fill" style="width:' + Math.max(0, Math.min(100, pct)) + '%"></div>' +
      '<div class="lbl">' + esc(fmtSlider(value, fmt)) + '</div></div></div>';
  }

  function fmtSlider(v, fmt) {
    if (fmt === 'pct') return Math.round(v) + ' %';
    if (fmt === 'vol') return Math.round(v) + ' %';
    if (fmt === 'temp') return fmtNum(v, 1) + ' °C';
    return fmtNum(v);
  }

  function supportsBrightness(st) {
    var modes = attr(st, 'supported_color_modes') || [];
    if (!modes.length) return attr(st, 'brightness') !== undefined;
    return modes.some(function (m) { return m !== 'onoff'; });
  }
  function supportsColor(st) {
    var modes = attr(st, 'supported_color_modes') || [];
    return modes.some(function (m) { return ['hs', 'xy', 'rgb', 'rgbw', 'rgbww'].indexOf(m) >= 0; });
  }
  function supportsColorTemp(st) {
    return (attr(st, 'supported_color_modes') || []).indexOf('color_temp') >= 0;
  }

  var COLORS = [[255, 60, 40], [255, 140, 0], [255, 220, 60], [80, 220, 80], [0, 200, 220], [50, 90, 255], [160, 70, 255], [255, 80, 180]];

  function attributesTable(st) {
    var skip = ['friendly_name', 'icon', 'entity_picture', 'supported_features', 'unit_of_measurement', 'device_class',
      'state_class', 'access_token', 'entity_id', 'supported_color_modes', 'restored', 'editable', 'id', 'attribution'];
    var rows = Object.keys(st.attributes || {}).filter(function (k) {
      var v = st.attributes[k];
      return skip.indexOf(k) < 0 && v !== null && typeof v !== 'object';
    }).slice(0, 14).map(function (k) {
      var v = st.attributes[k];
      return '<tr><td>' + esc(k.replace(/_/g, ' ')) + '</td><td>' + esc(typeof v === 'number' ? fmtNum(v) : word(String(v))) + '</td></tr>';
    });
    return rows.length ? '<table class="attrs">' + rows.join('') + '</table>' : '';
  }

  function detailBody(eid) {
    var st = states[eid], d = domainOf(eid), s = st.state, h = '';
    var on = s === 'on';

    if (s === 'unavailable') {
      return '<p class="big-state">Ej tillgänglig</p><p class="muted">Home Assistant har ingen kontakt med enheten just nu.</p>';
    }

    switch (d) {
      case 'light':
        h += '<div class="big-state">' + esc(stateText(eid)) + '</div>';
        h += '<div class="row">' + btn('Av', { domain: d, service: 'turn_off', icon: 'power', cls: on ? '' : 'active', k: 'off' }) +
          btn('På', { domain: d, service: 'turn_on', icon: 'bulb', cls: on ? 'active' : '', k: 'on', autofocus: true }) + '</div>';
        if (supportsBrightness(st)) {
          h += slider('brightness', 'Ljusstyrka', on && attr(st, 'brightness') ? Math.round(attr(st, 'brightness') / 2.55) : 0, 0, 100, 10, 'pct');
        }
        if (supportsColorTemp(st)) {
          var minK = attr(st, 'min_color_temp_kelvin') || 2000, maxK = attr(st, 'max_color_temp_kelvin') || 6500;
          h += '<div class="row"><div class="row-label">Vitt ljus</div>' +
            btn('Varmvit', { domain: d, service: 'turn_on', data: { color_temp_kelvin: Math.max(minK, 2700) }, k: 'ct1' }) +
            btn('Neutral', { domain: d, service: 'turn_on', data: { color_temp_kelvin: Math.min(maxK, Math.max(minK, 4000)) }, k: 'ct2' }) +
            btn('Kallvit', { domain: d, service: 'turn_on', data: { color_temp_kelvin: Math.min(maxK, 6000) }, k: 'ct3' }) + '</div>';
        }
        if (supportsColor(st)) {
          h += '<div class="row"><div class="row-label">Färg</div>' + COLORS.map(function (c, i) {
            return '<button class="swatch focusable" style="background:rgb(' + c.join(',') + ')" data-action="svc" data-domain="light" data-service="turn_on" data-json="' +
              esc(JSON.stringify({ rgb_color: c })) + '" data-k="col' + i + '" title="Färg"></button>';
          }).join('') + '</div>';
        }
        break;

      case 'switch': case 'input_boolean': case 'automation': case 'siren': case 'humidifier': case 'fan':
        h += '<div class="big-state">' + esc(stateText(eid)) + '</div>';
        h += '<div class="row">' + btn('Av', { domain: d, service: 'turn_off', icon: 'power', cls: on ? '' : 'active', k: 'off' }) +
          btn('På', { domain: d, service: 'turn_on', icon: 'power', cls: on ? 'active' : '', k: 'on' }) + '</div>';
        if (d === 'fan' && attr(st, 'percentage') !== undefined) {
          h += slider('percentage', 'Hastighet', attr(st, 'percentage') || 0, 0, 100, attr(st, 'percentage_step') || 10, 'pct');
        }
        if (d === 'automation') h += '<div class="row">' + btn('Kör nu', { domain: d, service: 'trigger', icon: 'play', k: 'trig' }) + '</div>';
        break;

      case 'cover': case 'valve':
        h += '<div class="big-state">' + esc(stateText(eid)) + '</div>';
        h += '<div class="row">' + btn('Öppna', { domain: d, service: d === 'cover' ? 'open_cover' : 'open_valve', icon: 'up', k: 'open' }) +
          btn('Stopp', { domain: d, service: d === 'cover' ? 'stop_cover' : 'stop_valve', icon: 'stop', k: 'stop' }) +
          btn('Stäng', { domain: d, service: d === 'cover' ? 'close_cover' : 'close_valve', icon: 'down', k: 'close' }) + '</div>';
        if (attr(st, 'current_position') !== undefined) h += slider('position', 'Position (100 % = helt öppen)', attr(st, 'current_position'), 0, 100, 10, 'pct');
        break;

      case 'lock':
        h += '<div class="big-state">' + esc(word(s)) + '</div>';
        h += '<div class="row">' + btn('Lås', { domain: d, service: 'lock', icon: 'lock', cls: s === 'locked' ? 'active' : '', k: 'lock' }) +
          btn('Lås upp', { domain: d, service: 'unlock', icon: 'unlock', cls: s === 'unlocked' ? 'active' : '', k: 'unlock' }) + '</div>';
        break;

      case 'climate': case 'water_heater':
        var target = attr(st, 'temperature');
        var hk = hold[eid + ':temp'];
        if (hk && hk.until > Date.now()) target = hk.value;
        var cur = attr(st, 'current_temperature');
        h += '<p class="sub">' + esc(stateText(eid).split(' · ')[0]) + (cur !== undefined && cur !== null ? ' · nu ' + fmtNum(cur, 1) + ' °C' : '') + '</p>';
        if (target !== undefined && target !== null) {
          h += '<div class="row">' + btn('', { action: 'temp', icon: 'minus', k: 'tminus', title: 'Sänk', data: { dir: -1 } }) +
            '<div class="temp-big">' + fmtNum(target, 1) + ' °C</div>' +
            btn('', { action: 'temp', icon: 'plus', k: 'tplus', title: 'Höj', data: { dir: 1 } }) + '</div>';
        } else if (attr(st, 'target_temp_low') !== undefined) {
          h += '<div class="big-state">' + fmtNum(attr(st, 'target_temp_low'), 1) + '–' + fmtNum(attr(st, 'target_temp_high'), 1) + ' °C</div>';
        }
        var modes = attr(st, d === 'climate' ? 'hvac_modes' : 'operation_list') || [];
        if (modes.length) {
          h += '<div class="row"><div class="row-label">Läge</div>' + modes.map(function (m) {
            return btn(word(m), {
              domain: d, service: d === 'climate' ? 'set_hvac_mode' : 'set_operation_mode',
              data: d === 'climate' ? { hvac_mode: m } : { operation_mode: m }, cls: s === m ? 'active' : '', k: 'm-' + m
            });
          }).join('') + '</div>';
        }
        var presets = attr(st, 'preset_modes') || [];
        if (presets.length && d === 'climate') {
          h += '<div class="row"><div class="row-label">Förinställning</div>' + presets.map(function (p) {
            return btn(p, { domain: d, service: 'set_preset_mode', data: { preset_mode: p }, cls: attr(st, 'preset_mode') === p ? 'active' : '', k: 'p-' + p });
          }).join('') + '</div>';
        }
        break;

      case 'media_player':
        var pic = attr(st, 'entity_picture');
        if (pic && s !== 'off') h += '<img class="media-art" alt="" src="' + esc(absUrl(pic)) + '">';
        h += '<div class="big-state" style="font-size:2.4rem">' + esc(attr(st, 'media_title') || word(s)) + '</div>';
        var artist = attr(st, 'media_artist') || attr(st, 'media_series_title') || attr(st, 'app_name');
        if (artist) h += '<p class="sub">' + esc(artist) + '</p>';
        if (s !== 'off') {
          h += '<div class="row">' + btn('', { domain: d, service: 'media_previous_track', icon: 'prev', k: 'prev', title: 'Föregående' }) +
            btn('', { domain: d, service: 'media_play_pause', icon: s === 'playing' ? 'pause' : 'play', k: 'pp', title: 'Spela/pausa' }) +
            btn('', { domain: d, service: 'media_next_track', icon: 'next', k: 'next', title: 'Nästa' }) +
            btn('', { domain: d, service: 'volume_down', icon: 'voldown', k: 'vd', title: 'Sänk volym' }) +
            btn('', { domain: d, service: 'volume_mute', icon: 'mute', k: 'mute', title: 'Ljud av', data: { is_volume_muted: !attr(st, 'is_volume_muted') }, cls: attr(st, 'is_volume_muted') ? 'active' : '' }) +
            btn('', { domain: d, service: 'volume_up', icon: 'volup', k: 'vu', title: 'Höj volym' }) + '</div>';
          if (attr(st, 'volume_level') !== undefined) h += slider('volume', 'Volym', Math.round(attr(st, 'volume_level') * 100), 0, 100, 5, 'vol');
        }
        h += '<div class="row" style="clear:both">' + btn(s === 'off' ? 'Slå på' : 'Stäng av', { domain: d, service: s === 'off' ? 'turn_on' : 'turn_off', icon: 'power', k: 'pwr' }) + '</div>';
        var sources = attr(st, 'source_list') || [];
        if (sources.length && s !== 'off') {
          h += '<div class="row"><div class="row-label">Källa</div>' + sources.slice(0, 16).map(function (src, i) {
            return btn(src, { domain: d, service: 'select_source', data: { source: src }, cls: attr(st, 'source') === src ? 'active' : '', k: 'src' + i });
          }).join('') + '</div>';
        }
        break;

      case 'camera':
        h += '<img id="camera-live" class="camera-view" alt="" src="' + esc(cameraUrl(eid)) + '">';
        break;

      case 'vacuum': case 'lawn_mower':
        h += '<div class="big-state">' + esc(word(s)) + '</div>';
        if (attr(st, 'battery_level') !== undefined) h += '<p class="sub">Batteri ' + attr(st, 'battery_level') + ' %</p>';
        h += '<div class="row">' + btn('Starta', { domain: d, service: d === 'vacuum' ? 'start' : 'start_mowing', icon: 'play', k: 'start' }) +
          btn('Pausa', { domain: d, service: 'pause', icon: 'pause', k: 'pause' }) +
          btn('Åk hem', { domain: d, service: d === 'vacuum' ? 'return_to_base' : 'dock', icon: 'home', k: 'dock' }) + '</div>';
        break;

      case 'alarm_control_panel':
        h += '<div class="big-state">' + esc(word(s)) + '</div>';
        if (attr(st, 'code_format')) {
          h += '<p class="muted">Larmet kräver kod och styrs därför i Home Assistant-appen.</p>';
        } else {
          h += '<div class="row">' + btn('Avlarma', { domain: d, service: 'alarm_disarm', icon: 'unlock', cls: s === 'disarmed' ? 'active' : '', k: 'dis' }) +
            btn('Larma hemma', { domain: d, service: 'alarm_arm_home', icon: 'home', cls: s === 'armed_home' ? 'active' : '', k: 'home' }) +
            btn('Larma borta', { domain: d, service: 'alarm_arm_away', icon: 'shield', cls: s === 'armed_away' ? 'active' : '', k: 'away' }) + '</div>';
        }
        break;

      case 'select': case 'input_select':
        h += '<div class="big-state">' + esc(s) + '</div><div class="row">' + (attr(st, 'options') || []).map(function (o, i) {
          return btn(o, { domain: d, service: 'select_option', data: { option: o }, cls: s === o ? 'active' : '', k: 'o' + i });
        }).join('') + '</div>';
        break;

      case 'number': case 'input_number':
        h += slider('value', 'Värde' + (attr(st, 'unit_of_measurement') ? ' (' + attr(st, 'unit_of_measurement') + ')' : ''),
          Number(s), Number(attr(st, 'min') || 0), Number(attr(st, 'max') || 100), Number(attr(st, 'step') || 1));
        break;

      case 'scene': case 'script': case 'button': case 'input_button':
        h += '<div class="row">' + btn(d === 'scene' ? 'Aktivera' : d === 'script' ? (s === 'on' ? 'Stoppa' : 'Kör') : 'Tryck', {
          action: 'primary', icon: 'play', cls: 'primary', k: 'run' }) + '</div>';
        break;

      case 'weather':
        h += '<div class="big-state">' + esc(stateText(eid)) + '</div><div id="forecast" class="row"></div>';
        h += attributesTable(st);
        break;

      default:
        h += '<div class="big-state">' + esc(stateText(eid)) + '</div>';
        h += attributesTable(st);
    }
    return h;
  }

  function renderDetail() {
    var eid = detailId, st = states[eid];
    if (!st) { closePanel(); return; }
    var panel = $('panel');
    var focusK = Nav.current() && Nav.current().getAttribute('data-k');
    var area = areaOf(eid);
    var sub = [];
    if (area && areas[area]) sub.push(areas[area].name);
    sub.push('ändrad ' + ago(st.last_changed));
    var isFav = favs.indexOf(eid) >= 0;
    panel.innerHTML =
      '<h2>' + Icons.svg(iconFor(eid)) + '<span>' + esc(friendly(eid)) + '</span></h2>' +
      '<p class="sub">' + esc(sub.join(' · ')) + '</p>' +
      detailBody(eid) +
      '<div class="row" style="margin-top:1.8rem;clear:both">' +
      btn(isFav ? 'Ta bort favorit' : 'Lägg till favorit', { action: 'fav', icon: 'star', k: 'fav' }) +
      btn('Stäng', { action: 'close', icon: 'close', k: 'close' }) + '</div>';
    var target = focusK && panel.querySelector('[data-k="' + focusK + '"]');
    Nav.reset(target || null);
  }

  var cameraTimer = null;
  function openDetail(eid) {
    if (!states[eid]) return;
    detailId = eid;
    panelKind = 'detail';
    lastCard = Nav.current();
    $('overlay').hidden = false;
    renderDetail();
    var d = domainOf(eid);
    if (d === 'camera') {
      cameraTimer = setInterval(function () {
        var img = $('camera-live');
        if (img) img.src = cameraUrl(eid).replace(/_t=\d+/, '_t=' + Date.now());
      }, 2000);
    }
    if (d === 'weather') loadForecast(eid);
  }

  function loadForecast(eid) {
    if (!conn) return;
    conn.send({ type: 'call_service', domain: 'weather', service: 'get_forecasts', service_data: { type: 'daily' },
      target: { entity_id: eid }, return_response: true }).then(function (r) {
      var fc = r && r.response && r.response[eid] && r.response[eid].forecast;
      var box = $('forecast');
      if (!fc || !box) return;
      var days = ['sön', 'mån', 'tis', 'ons', 'tor', 'fre', 'lör'];
      box.innerHTML = fc.slice(0, 6).map(function (f) {
        var dt = new Date(f.datetime);
        return '<div class="card" style="width:8.5rem;height:auto;cursor:default;margin:0;align-items:center;text-align:center">' +
          '<div class="state">' + days[dt.getDay()] + '</div>' + Icons.svg(WEATHER_ICON[f.condition] || 'sun', 'ic') +
          '<div class="name">' + fmtNum(f.temperature) + '°' + (f.templow !== undefined ? ' / ' + fmtNum(f.templow) + '°' : '') + '</div></div>';
      }).join('');
    }, function () { /* äldre HA eller ingen prognos – visa bara nuläget */ });
  }

  var lastCard = null;
  function closePanel() {
    $('overlay').hidden = true;
    detailId = null;
    panelKind = null;
    clearInterval(cameraTimer);
    cameraTimer = null;
    var back = lastCard && document.body.contains(lastCard) ? lastCard : null;
    if (!back && lastCard && lastCard.getAttribute) {
      var eid = lastCard.getAttribute('data-eid');
      back = eid && cardEls[eid] ? cardEls[eid][0] : null;
    }
    Nav.reset(back);
  }

  function adjustSlider(el, dir, absolute) {
    var min = Number(el.getAttribute('data-min')), max = Number(el.getAttribute('data-max'));
    var step = Number(el.getAttribute('data-step')) || 1;
    var v = absolute !== undefined ? absolute : Number(el.getAttribute('data-value')) + dir * step;
    v = Math.max(min, Math.min(max, Math.round(v / step) * step));
    v = Math.round(v * 1000) / 1000;
    el.setAttribute('data-value', v);
    var pct = max > min ? (v - min) / (max - min) * 100 : 0;
    el.querySelector('.fill').style.width = pct + '%';
    el.querySelector('.lbl').textContent = fmtSlider(v, el.getAttribute('data-fmt'));
    var key = el.getAttribute('data-k');
    var eid = detailId;
    var h = hold[eid + ':' + key] = { value: v, until: Date.now() + 4000 };
    clearTimeout(h.timer);
    h.timer = setTimeout(function () { sendSlider(eid, key, v); }, 450);
  }

  function sendSlider(eid, key, v) {
    var d = domainOf(eid);
    if (key === 'brightness') {
      if (v <= 0) call('light', 'turn_off', {}, eid); else call('light', 'turn_on', { brightness_pct: v }, eid);
    } else if (key === 'percentage') call('fan', 'set_percentage', { percentage: v }, eid);
    else if (key === 'position') call(d, d === 'cover' ? 'set_cover_position' : 'set_valve_position', { position: v }, eid);
    else if (key === 'volume') call('media_player', 'volume_set', { volume_level: v / 100 }, eid);
    else if (key === 'value') call(d, 'set_value', { value: v }, eid);
  }

  function adjustTemp(dir) {
    var eid = detailId, st = states[eid];
    var step = attr(st, 'target_temp_step') || (attr(st, 'temperature_unit') === '°F' ? 1 : 0.5);
    var h = hold[eid + ':temp'];
    var cur = h && h.until > Date.now() ? h.value : Number(attr(st, 'temperature'));
    var v = Math.round((cur + dir * step) / step) * step;
    var min = attr(st, 'min_temp'), max = attr(st, 'max_temp');
    if (min !== undefined) v = Math.max(min, v);
    if (max !== undefined) v = Math.min(max, v);
    var nh = hold[eid + ':temp'] = { value: v, until: Date.now() + 5000 };
    if (h) clearTimeout(h.timer);
    nh.timer = setTimeout(function () { call(domainOf(eid), 'set_temperature', { temperature: v }, eid); }, 700);
    renderDetail();
  }

  /* ---------- Inställningar ---------- */
  function openSettings() {
    panelKind = 'settings';
    lastCard = Nav.current();
    $('overlay').hidden = false;
    var count = allVisible().length;
    $('panel').innerHTML =
      '<h2>' + Icons.svg('gear') + '<span>Inställningar</span></h2>' +
      '<table class="attrs">' +
      '<tr><td>Server</td><td>' + esc(auth.hassUrl) + '</td></tr>' +
      '<tr><td>Home Assistant</td><td>' + esc((conn && conn.haVersion) || '–') + '</td></tr>' +
      '<tr><td>Inloggning</td><td>' + (auth.type === 'oauth' ? 'Användarkonto' : 'Åtkomsttoken') + '</td></tr>' +
      '<tr><td>Entiteter som visas</td><td>' + count + '</td></tr>' +
      '<tr><td>Favoriter</td><td>' + favs.length + '</td></tr>' +
      '</table>' +
      '<div class="row" style="margin-top:1.8rem">' +
      btn('Uppdatera', { action: 'reload', icon: 'refresh', k: 'reload' }) +
      btn('Logga ut', { action: 'logout', icon: 'logout', cls: 'danger', k: 'logout' }) +
      btn('Stäng', { action: 'close', icon: 'close', k: 'close' }) + '</div>' +
      '<p class="muted" style="margin-top:1.4rem">SMGD Hem 1.0.0 · Dolda entiteter och diagnostik-/konfigurationsentiteter visas inte.</p>';
    Nav.reset();
  }

  /* ---------- Inloggningsskärm ---------- */
  function hostedOnHA() {
    return /^https?:/.test(location.protocol) && /\/(local|hacsfiles|api\/hassio_ingress)\//.test(location.pathname);
  }

  function setMode(pw) {
    passwordMode = pw;
    $('mode-password').classList.toggle('active', pw);
    $('mode-token').classList.toggle('active', !pw);
    $('password-fields').hidden = !pw;
    $('token-fields').hidden = pw;
  }

  function showSetup(err) {
    if (conn) { conn.close(); conn = null; }
    splash(false);
    $('main').hidden = true;
    $('overlay').hidden = true;
    panelKind = null;
    detailId = null;
    $('setup').hidden = false;
    var url = (auth && auth.hassUrl) || load('lastUrl', '') || (fileConfig && fileConfig.hassUrl) || (hostedOnHA() ? location.origin : '');
    $('setup-url').value = url;
    if (fileConfig && fileConfig.token && !$('setup-token').value) { $('setup-token').value = fileConfig.token; setMode(false); }
    mfaFlow = null;
    $('mfa-field').hidden = true;
    $('setup-error').textContent = err || '';
    Nav.setScope(function () { return $('setup'); });
    Nav.reset(url ? (passwordMode ? $('setup-user') : $('setup-token')) : $('setup-url'));
  }

  function doConnect() {
    var base = HA.normalizeUrl($('setup-url').value);
    var errEl = $('setup-error');
    errEl.textContent = '';
    if (!base) { errEl.textContent = 'Ange adressen till Home Assistant.'; return; }
    save('lastUrl', base);
    var btnEl = $('setup-connect');
    btnEl.disabled = true;
    btnEl.textContent = 'Ansluter…';
    var done = function () { btnEl.disabled = false; btnEl.textContent = 'Anslut'; };

    var p;
    if (passwordMode) {
      if (mfaFlow) {
        p = HA.submitMfa(base, mfaFlow, $('setup-mfa').value.trim());
      } else {
        var u = $('setup-user').value.trim(), pw = $('setup-pass').value;
        if (!u || !pw) { done(); errEl.textContent = 'Fyll i användarnamn och lösenord.'; return; }
        p = HA.loginWithPassword(base, u, pw);
      }
    } else {
      var tok = $('setup-token').value.trim();
      if (!tok) { done(); errEl.textContent = 'Klistra in en åtkomsttoken.'; return; }
      p = Promise.resolve({ type: 'llat', hassUrl: base, access_token: tok });
    }

    p.then(function (res) {
      if (res && res.mfaRequired) {
        mfaFlow = res.flowId;
        $('mfa-field').hidden = false;
        done();
        errEl.textContent = 'Ange engångskoden från din autentiseringsapp.';
        Nav.reset($('setup-mfa'));
        return;
      }
      save('loggedOut', null);
      $('setup-pass').value = '';
      $('setup-mfa').value = '';
      startWith(res).then(done, function (e) {
        done();
        showSetup(e.message);
      });
    }, function (e) {
      done();
      errEl.textContent = e.message;
    });
  }

  /* ---------- Anslutning och data ---------- */
  function startWith(a) {
    auth = a;
    if (conn) conn.close();
    splash('Ansluter till ' + a.hassUrl + '…');
    $('setup').hidden = true;
    conn = new HA.Connection(a, {
      status: setConn,
      ready: onReady,
      authChanged: function (x) { auth = x; save('auth', x); },
      authInvalid: function (msg) { save('auth', null); showSetup(msg); }
    });
    return conn.connect().then(function () {
      save('auth', a);
    }, function (e) {
      if (!everConnected) {
        if (conn) conn.close();
        throw new Error(e.message + (a.type === 'llat' ? '' : ''));
      }
    });
  }

  function onReady() {
    var c = conn;
    var safe = function (p) { return p.then(function (x) { return x; }, function () { return []; }); };
    Promise.all([
      c.send({ type: 'get_states' }),
      safe(c.send({ type: 'config/area_registry/list' })),
      safe(c.send({ type: 'config/device_registry/list' })),
      safe(c.send({ type: 'config/entity_registry/list' })),
      c.send({ type: 'get_config' }).then(function (x) { return x; }, function () { return {}; })
    ]).then(function (r) {
      if (c !== conn) return;
      states = {};
      r[0].forEach(function (s) { states[s.entity_id] = s; });
      areas = {};
      r[1].forEach(function (a) { areas[a.area_id] = a; });
      areaOrder = r[1].map(function (a) { return a.area_id; }).sort(function (x, y) {
        return areas[x].name.localeCompare(areas[y].name, 'sv');
      });
      devReg = {};
      r[2].forEach(function (d) { devReg[d.id] = d; });
      entReg = {};
      r[3].forEach(function (e) { entReg[e.entity_id] = e; });
      locationName = r[4].location_name || 'Hem';
      return c.subscribeEvents('state_changed', onStateChanged);
    }).then(function () {
      if (c !== conn) return;
      var first = !everConnected;
      everConnected = true;
      $('site-name').textContent = locationName;
      $('setup').hidden = true;
      $('main').hidden = false;
      splash(false);
      renderTabs();
      renderContent();
      renderWeather();
      if (panelKind === 'detail') renderDetail();
      if (first || panelKind === null) {
        Nav.setScope(scope);
        if (first) Nav.reset(firstCard() || $('tabs').querySelector('.tab.active'));
      }
      if (!first) toast('Ansluten igen');
    }, function (e) {
      toast('Kunde inte läsa data: ' + e.message, true);
    });
  }

  function firstCard() { return $('content').querySelector('.card'); }

  function onStateChanged(ev) {
    var d = ev.data, eid = d.entity_id;
    var existed = !!states[eid];
    if (d.new_state) states[eid] = d.new_state; else delete states[eid];
    if (!d.new_state || !existed) { if (visibleEntity(eid)) scheduleRender(); return; }
    (cardEls[eid] || []).forEach(paintCard);
    if (domainOf(eid) === 'weather') renderWeather();
    if (detailId === eid) {
      // Rita inte om medan användaren håller på att dra i ett reglage.
      var cur = Nav.current();
      var k = cur && cur.getAttribute('data-k');
      var h = k && hold[eid + ':' + k];
      if (!(h && h.until > Date.now())) renderDetail();
    }
  }

  function scope() {
    return panelKind ? $('overlay') : $('main');
  }

  function reloadData() {
    closePanel();
    if (conn) onReady();
    toast('Uppdaterar…');
  }

  function logout() {
    var a = auth;
    save('auth', null);
    save('loggedOut', true);
    auth = null;
    everConnected = false;
    if (conn) { conn.close(); conn = null; }
    HA.revokeAuth(a);
    showSetup('Du är utloggad.');
  }

  function exitApp() {
    if (window.webOS && typeof window.webOS.platformBack === 'function') window.webOS.platformBack();
    else window.close();
  }

  /* ---------- Inmatning ---------- */
  var press = null; // pågående OK-tryck på ett kort

  function handleAction(el) {
    var a = el.getAttribute('data-action');
    var eid = el.getAttribute('data-eid');
    switch (a) {
      case 'tab': selectTab(el.getAttribute('data-tab')); break;
      case 'settings': openSettings(); break;
      case 'card': primaryAction(eid); break;
      case 'svc':
        var data = el.getAttribute('data-json');
        call(el.getAttribute('data-domain'), el.getAttribute('data-service'), data ? JSON.parse(data) : {}, detailId);
        break;
      case 'primary': primaryAction(detailId); break;
      case 'temp': adjustTemp(JSON.parse(el.getAttribute('data-json')).dir); break;
      case 'fav': toggleFav(detailId); break;
      case 'close': closePanel(); break;
      case 'reload': reloadData(); break;
      case 'logout': logout(); break;
    }
  }

  document.addEventListener('click', function (e) {
    if (suppressClick) { suppressClick = false; e.preventDefault(); return; }
    var t = e.target;
    if (t.id === 'overlay') { closePanel(); return; }
    while (t && t !== document && !(t.getAttribute && t.getAttribute('data-action'))) t = t.parentNode;
    if (!t || t === document) return;
    if (t.getAttribute('data-action') === 'slider') {
      var r = t.getBoundingClientRect();
      var min = Number(t.getAttribute('data-min')), max = Number(t.getAttribute('data-max'));
      adjustSlider(t, 0, min + (e.clientX - r.left) / r.width * (max - min));
      return;
    }
    handleAction(t);
  });

  // Långt tryck med mus/pekare öppnar detaljer.
  var mouseTimer = null, suppressClick = false;
  function cardFrom(t) {
    while (t && t !== document && !(t.classList && t.classList.contains('card'))) t = t.parentNode;
    return t && t !== document && t.getAttribute('data-eid') ? t : null;
  }
  document.addEventListener('mousedown', function (e) {
    var c = cardFrom(e.target);
    if (!c) return;
    mouseTimer = setTimeout(function () { suppressClick = true; openDetail(c.getAttribute('data-eid')); }, LONG_PRESS_MS);
  });
  document.addEventListener('mouseup', function () { clearTimeout(mouseTimer); });
  document.addEventListener('contextmenu', function (e) {
    var c = cardFrom(e.target);
    if (c) { e.preventDefault(); clearTimeout(mouseTimer); openDetail(c.getAttribute('data-eid')); }
  });
  document.addEventListener('touchstart', function (e) {
    var c = cardFrom(e.target);
    if (!c) return;
    mouseTimer = setTimeout(function () { suppressClick = true; openDetail(c.getAttribute('data-eid')); }, LONG_PRESS_MS);
  }, { passive: true });
  document.addEventListener('touchend', function () { clearTimeout(mouseTimer); });
  document.addEventListener('touchmove', function () { clearTimeout(mouseTimer); }, { passive: true });

  // Uppåt från innehållet till flikraden landar alltid på den aktiva fliken.
  Nav.redirect = function (best, from, dir) {
    if (dir === 'up' && best.classList.contains('tab') && !from.classList.contains('tab')) {
      return $('tabs').querySelector('.tab.active');
    }
    if (dir === 'down' && from.classList.contains('tab') && !best.classList.contains('tab')) {
      return firstCard();
    }
    return null;
  };

  var tabFocusTimer = null;
  Nav.onFocus = function (el) {
    if (el.getAttribute('data-action') === 'tab') {
      clearTimeout(tabFocusTimer);
      tabFocusTimer = setTimeout(function () { selectTab(el.getAttribute('data-tab')); }, 250);
    }
  };

  function onBack() {
    if (!$('setup').hidden) {
      if (auth && everConnected) return; // inget att gå tillbaka till
      exitApp();
      return;
    }
    if (panelKind) { closePanel(); return; }
    var cur = Nav.current();
    if (cur && !cur.classList.contains('tab')) {
      Nav.focus($('tabs').querySelector('.tab.active') || $('tabs').querySelector('.tab'));
      $('content').scrollTop = 0;
      return;
    }
    exitApp();
  }

  document.addEventListener('keydown', function (e) {
    var code = e.keyCode;
    var cur = Nav.current();
    var editing = document.activeElement && document.activeElement.tagName === 'INPUT';

    if (code === K.BACK || code === K.ESC || (code === K.BACKSPACE && !editing)) {
      e.preventDefault();
      if (editing) { document.activeElement.blur(); return; }
      onBack();
      return;
    }

    if (code >= 37 && code <= 40) {
      if (editing && (code === K.LEFT || code === K.RIGHT)) return; // flytta markören i textfältet
      e.preventDefault();
      if (cur && cur.classList.contains('slider') && (code === K.LEFT || code === K.RIGHT)) {
        adjustSlider(cur, code === K.LEFT ? -1 : 1);
        return;
      }
      if (editing) document.activeElement.blur();
      Nav.move({ 37: 'left', 38: 'up', 39: 'right', 40: 'down' }[code]);
      return;
    }

    if (code === K.ENTER) {
      if (!cur) { Nav.reset(); e.preventDefault(); return; }
      if (cur.tagName === 'INPUT') {
        if (!editing) { e.preventDefault(); Nav.focus(cur, { edit: true }); return; }
        // Enter i ett textfält: gå vidare till nästa fält, eller anslut.
        e.preventDefault();
        cur.blur();
        if (cur.id === 'setup-pass' || cur.id === 'setup-token' || cur.id === 'setup-mfa') doConnect();
        else Nav.move('down');
        return;
      }
      e.preventDefault();
      if (cur.classList.contains('card')) {
        if (e.repeat || press) return;
        var eid = cur.getAttribute('data-eid');
        press = { eid: eid, long: false };
        press.timer = setTimeout(function () { press.long = true; openDetail(eid); }, LONG_PRESS_MS);
        return;
      }
      if (e.repeat && !cur.getAttribute('data-action')) return;
      if (cur.id === 'mode-password') { setMode(true); return; }
      if (cur.id === 'mode-token') { setMode(false); return; }
      if (cur.id === 'setup-connect') { doConnect(); return; }
      if (cur.classList.contains('slider')) return;
      if (cur.getAttribute('data-action')) {
        if (e.repeat && cur.getAttribute('data-action') !== 'temp') return;
        handleAction(cur);
      }
      return;
    }

    if (editing) return;

    var focusedEid = cur && cur.getAttribute('data-eid');
    switch (code) {
      case K.YELLOW: case 70: // F
        if (focusedEid) toggleFav(focusedEid);
        else if (detailId) toggleFav(detailId);
        break;
      case K.BLUE: case 82: // R
        reloadData();
        break;
      case K.GREEN: case 83: // S
        if (!panelKind && !$('main').hidden) openSettings();
        break;
      case K.RED: case 73: // I
        if (focusedEid) openDetail(focusedEid);
        break;
      case K.PLAY: case K.PAUSE: case K.PLAYPAUSE:
        var mp = focusedEid || detailId;
        if (mp && domainOf(mp) === 'media_player') call('media_player', 'media_play_pause', {}, mp);
        break;
      case K.FF:
        var mn = focusedEid || detailId;
        if (mn && domainOf(mn) === 'media_player') call('media_player', 'media_next_track', {}, mn);
        break;
      case K.RW:
        var mr = focusedEid || detailId;
        if (mr && domainOf(mr) === 'media_player') call('media_player', 'media_previous_track', {}, mr);
        break;
    }
  });

  document.addEventListener('keyup', function (e) {
    if (e.keyCode !== K.ENTER || !press) return;
    clearTimeout(press.timer);
    var p = press;
    press = null;
    if (!p.long) primaryAction(p.eid);
  });

  $('mode-password').addEventListener('click', function () { setMode(true); });
  $('mode-token').addEventListener('click', function () { setMode(false); });
  $('setup-connect').addEventListener('click', doConnect);

  // Kameror i rutnätet uppdateras var tionde sekund.
  setInterval(function () {
    if (panelKind || $('main').hidden) return;
    Object.keys(cardEls).forEach(function (eid) {
      if (domainOf(eid) !== 'camera') return;
      cardEls[eid].forEach(function (el) { var img = el.querySelector('img'); if (img) img.src = cameraUrl(eid); });
    });
  }, 10000);

  // När TV:n vaknar eller appen visas igen: anslut på nytt direkt om förbindelsen har tappats.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && conn && (!conn.ws || conn.ws.readyState > 1)) {
      conn.retry = 0;
      conn.connect().then(function () {}, function () {});
    }
  });
  document.addEventListener('webOSRelaunch', function () {
    if (conn && (!conn.ws || conn.ws.readyState > 1)) conn.connect().then(function () {}, function () {});
  });

  /* ---------- Start ---------- */
  function loadFileConfig() {
    // Valfri config.json bredvid index.html (bakas in vid paketering, se README).
    return new Promise(function (resolve) {
      try {
        var x = new XMLHttpRequest();
        x.open('GET', 'config.json', true);
        x.onload = function () {
          try { resolve(x.status === 200 || x.status === 0 ? JSON.parse(x.responseText) : null); } catch (e) { resolve(null); }
        };
        x.onerror = function () { resolve(null); };
        x.send();
      } catch (e) { resolve(null); }
    });
  }

  tickClock();
  setInterval(tickClock, 10000);

  loadFileConfig().then(function (cfg) {
    fileConfig = cfg && cfg.hassUrl ? { hassUrl: HA.normalizeUrl(cfg.hassUrl), token: cfg.token || '' } : null;
    var stored = load('auth', null);
    var start = stored;
    if (!start && fileConfig && fileConfig.token && !load('loggedOut', false)) {
      start = { type: 'llat', hassUrl: fileConfig.hassUrl, access_token: fileConfig.token };
    }
    if (!start) { setMode(true); showSetup(''); return; }
    startWith(start).then(function () {}, function (e) { auth = start; showSetup(e.message); });
  });
})();
