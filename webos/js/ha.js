/* Home Assistant-klient: inloggning (lösenord eller token) och WebSocket-API.
   Skriven utan async/await och ?. så att den går på webOS 4 (Chromium 53). */
(function (global) {
  'use strict';

  function normalizeUrl(url) {
    url = String(url || '').trim();
    if (!url) return '';
    if (!/^https?:\/\//i.test(url)) url = 'http://' + url;
    return url.replace(/\/+$/, '');
  }

  function formPost(url, params) {
    var body = Object.keys(params).map(function (k) {
      return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]);
    }).join('&');
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var err = new Error(data.error_description || data.error || ('HTTP ' + r.status));
          err.status = r.status;
          throw err;
        }
        return data;
      });
    });
  }

  function jsonPost(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var err = new Error(data.message || ('HTTP ' + r.status));
          err.status = r.status;
          throw err;
        }
        return data;
      });
    });
  }

  function networkError(e) {
    if (e && e.status) return e;
    var err = new Error('Kunde inte nå Home Assistant. Kontrollera adressen, eller använd en åtkomsttoken ' +
      '(inloggning med lösenord kräver att Home Assistant tillåter appens ursprung, se README).');
    err.network = true;
    return err;
  }

  /* ---------- Inloggning med användarnamn och lösenord (samma flöde som Home Assistants egen inloggning) ---------- */

  // Klient-id och omdirigering ligger på Home Assistants egen adress, så HA godkänner dem utan extra inställningar.
  function clientIds(base) {
    return { clientId: base + '/', redirectUri: base + '/?auth_callback=1' };
  }

  function exchangeCode(base, code) {
    var ids = clientIds(base);
    return formPost(base + '/auth/token', {
      grant_type: 'authorization_code',
      code: code,
      client_id: ids.clientId
    }).then(function (t) {
      return {
        type: 'oauth',
        hassUrl: base,
        clientId: ids.clientId,
        access_token: t.access_token,
        refresh_token: t.refresh_token,
        expires: Date.now() + (t.expires_in || 1800) * 1000
      };
    });
  }

  function handleFlowStep(base, step) {
    if (step.type === 'create_entry') return exchangeCode(base, step.result);
    if (step.type === 'form') {
      var errs = step.errors || {};
      if (errs.base === 'invalid_auth') throw new Error('Fel användarnamn eller lösenord.');
      if (errs.base === 'invalid_code') throw new Error('Fel engångskod.');
      if (step.step_id === 'mfa') {
        return { mfaRequired: true, flowId: step.flow_id };
      }
      if (errs.base) throw new Error('Inloggningen misslyckades (' + errs.base + ').');
    }
    if (step.type === 'abort') throw new Error('Inloggningen avbröts (' + (step.reason || 'okänd orsak') + ').');
    throw new Error('Oväntat svar från Home Assistant vid inloggning.');
  }

  function loginWithPassword(base, username, password) {
    var ids = clientIds(base);
    return jsonPost(base + '/auth/login_flow', {
      client_id: ids.clientId,
      handler: ['homeassistant', null],
      redirect_uri: ids.redirectUri
    }).then(function (flow) {
      if (!flow.flow_id) throw new Error('Home Assistant erbjuder inte inloggning med lösenord.');
      return jsonPost(base + '/auth/login_flow/' + flow.flow_id, {
        client_id: ids.clientId,
        username: username,
        password: password
      });
    }).then(function (step) {
      return handleFlowStep(base, step);
    }, function (e) { throw networkError(e); });
  }

  function submitMfa(base, flowId, code) {
    var ids = clientIds(base);
    return jsonPost(base + '/auth/login_flow/' + flowId, { client_id: ids.clientId, code: code })
      .then(function (step) { return handleFlowStep(base, step); }, function (e) { throw networkError(e); });
  }

  function refreshAuth(auth) {
    return formPost(auth.hassUrl + '/auth/token', {
      grant_type: 'refresh_token',
      refresh_token: auth.refresh_token,
      client_id: auth.clientId
    }).then(function (t) {
      auth.access_token = t.access_token;
      auth.expires = Date.now() + (t.expires_in || 1800) * 1000;
      return auth;
    });
  }

  function revokeAuth(auth) {
    if (!auth || auth.type !== 'oauth' || !auth.refresh_token) return Promise.resolve();
    return formPost(auth.hassUrl + '/auth/revoke', { token: auth.refresh_token }).catch(function () {});
  }

  /* ---------- WebSocket-anslutning ---------- */

  function Connection(auth, handlers) {
    this.auth = auth;
    this.h = handlers || {};
    this.ws = null;
    this.nextId = 1;
    this.pending = {};
    this.subs = {};
    this.closedByUser = false;
    this.retry = 0;
    this.pingTimer = null;
    this.retryTimer = null;
  }

  Connection.prototype._emit = function (name, arg) {
    if (typeof this.h[name] === 'function') this.h[name](arg);
  };

  Connection.prototype._ensureToken = function () {
    var a = this.auth;
    if (a.type === 'oauth' && a.expires - Date.now() < 60000) {
      var self = this;
      return refreshAuth(a).then(function (x) { self._emit('authChanged', x); return x; });
    }
    return Promise.resolve(a);
  };

  Connection.prototype.connect = function () {
    var self = this;
    this.closedByUser = false;
    this._emit('status', 'connecting');
    return this._ensureToken().then(function () {
      return self._open();
    }, function (e) {
      if (e && (e.status === 400 || e.status === 401 || e.status === 403)) {
        self._emit('authInvalid', 'Inloggningen har gått ut. Logga in igen.');
        throw e;
      }
      self._scheduleReconnect();
      throw e;
    });
  };

  Connection.prototype._open = function () {
    var self = this;
    return new Promise(function (resolve, reject) {
      var url = self.auth.hassUrl.replace(/^http/i, 'ws') + '/api/websocket';
      var ws;
      try { ws = new WebSocket(url); } catch (e) { reject(e); return; }
      self.ws = ws;
      self.subs = {}; // prenumerationer görs om efter varje ny anslutning ('ready')
      var settled = false;

      ws.onmessage = function (ev) {
        var msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        var list = Array.isArray(msg) ? msg : [msg];
        list.forEach(function (m) { self._onMessage(m, function (ok, err) {
          if (settled) return;
          settled = true;
          if (ok) resolve(); else reject(err);
        }); });
      };
      ws.onclose = function () {
        self._stopPing();
        Object.keys(self.pending).forEach(function (id) {
          self.pending[id].reject(new Error('Anslutningen bröts'));
        });
        self.pending = {};
        if (!settled) { settled = true; reject(new Error('Kunde inte ansluta till ' + url)); }
        if (!self.closedByUser) {
          self._emit('status', 'disconnected');
          self._scheduleReconnect();
        }
      };
      ws.onerror = function () { /* onclose följer */ };
    });
  };

  Connection.prototype._onMessage = function (m, settle) {
    var self = this;
    switch (m.type) {
      case 'auth_required':
        this.ws.send(JSON.stringify({ type: 'auth', access_token: this.auth.access_token }));
        break;
      case 'auth_ok':
        this.retry = 0;
        this.haVersion = m.ha_version;
        // Be om att få flera meddelanden i samma ram (snabbare på svaga TV-processorer).
        this.ws.send(JSON.stringify({ id: this.nextId++, type: 'supported_features', features: { coalesce_messages: 1 } }));
        this._startPing();
        this._emit('status', 'connected');
        settle(true);
        this._emit('ready');
        break;
      case 'auth_invalid':
        this.closedByUser = true;
        this.ws.close();
        if (this.auth.type === 'oauth') {
          // Försök med en ny token en gång innan användaren får logga in igen.
          refreshAuth(this.auth).then(function (a) {
            self._emit('authChanged', a);
            self.connect();
          }, function () { self._emit('authInvalid', 'Inloggningen har gått ut. Logga in igen.'); });
        } else {
          this._emit('authInvalid', 'Åtkomsttoken godtogs inte: ' + (m.message || ''));
        }
        settle(false, new Error('Inloggningen godtogs inte.'));
        break;
      case 'result':
        var p = this.pending[m.id];
        if (p) {
          delete this.pending[m.id];
          if (m.success) p.resolve(m.result);
          else p.reject(new Error((m.error && m.error.message) || 'Kommandot misslyckades'));
        }
        break;
      case 'pong':
        var q = this.pending[m.id];
        if (q) { delete this.pending[m.id]; q.resolve(); }
        break;
      case 'event':
        var cb = this.subs[m.id];
        if (cb) cb(m.event);
        break;
    }
  };

  Connection.prototype._startPing = function () {
    var self = this;
    this._stopPing();
    this.pingTimer = setInterval(function () {
      var timeout = setTimeout(function () { if (self.ws) self.ws.close(); }, 10000);
      self.send({ type: 'ping' }).then(function () { clearTimeout(timeout); }, function () { clearTimeout(timeout); });
    }, 30000);
  };

  Connection.prototype._stopPing = function () {
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.pingTimer = null;
  };

  Connection.prototype._scheduleReconnect = function () {
    var self = this;
    if (this.retryTimer || this.closedByUser) return;
    var delay = Math.min(30000, 1000 * Math.pow(2, this.retry++));
    this.retryTimer = setTimeout(function () {
      self.retryTimer = null;
      self.connect().then(function () {}, function () {});
    }, delay);
  };

  Connection.prototype.send = function (msg) {
    var self = this;
    return new Promise(function (resolve, reject) {
      if (!self.ws || self.ws.readyState !== 1) { reject(new Error('Inte ansluten')); return; }
      var id = self.nextId++;
      msg.id = id;
      self.pending[id] = { resolve: resolve, reject: reject };
      self.ws.send(JSON.stringify(msg));
    });
  };

  Connection.prototype.subscribeEvents = function (eventType, cb) {
    var self = this;
    var id = this.nextId; // send() använder nästa id
    this.subs[id] = cb;
    return this.send({ type: 'subscribe_events', event_type: eventType }).then(function () { return id; }, function (e) {
      delete self.subs[id];
      throw e;
    });
  };

  Connection.prototype.callService = function (domain, service, data, entityId) {
    var msg = { type: 'call_service', domain: domain, service: service, service_data: data || {} };
    if (entityId) msg.target = { entity_id: entityId };
    return this.send(msg);
  };

  Connection.prototype.close = function () {
    this.closedByUser = true;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this._stopPing();
    if (this.ws) this.ws.close();
  };

  global.HA = {
    normalizeUrl: normalizeUrl,
    loginWithPassword: loginWithPassword,
    submitMfa: submitMfa,
    revokeAuth: revokeAuth,
    Connection: Connection
  };
})(window);
