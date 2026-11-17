// Webbappen: inloggning, lista med mappar/taggar/sök, live-inspelning, visning och redigering.
// Ingen byggkedja – vanliga ES-moduler. All text sätts med textContent (inget innerHTML med data).

// ---------------------------------------------------------------- hjälpfunktioner
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const $app = () => document.getElementById("app");

function mount(...nodes) {
  const app = $app();
  app.replaceChildren(...nodes);
}

let toastTimer;
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Requested-With": "transcribe" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError(0, "Ingen kontakt med servern.");
  }
  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) data = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/login") && !path.startsWith("/api/me")) {
      state.me = null;
      go("#/logga-in");
    }
    throw new ApiError(res.status, (data && data.error) || `Fel ${res.status}`);
  }
  return data;
}

function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) || "null");
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    return null;
  }
}

function fmtTime(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const hh = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return hh > 0 ? `${hh}:${mm}:${ss}` : `${mm}:${ss}`;
}

function fmtDuration(sec) {
  const m = Math.round((sec || 0) / 60);
  if (m < 1) return `${Math.round(sec || 0)} s`;
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

function fmtDate(iso) {
  const d = new Date(iso);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
  if (sameDay) return `I dag ${time}`;
  return `${d.toLocaleDateString("sv-SE", { day: "numeric", month: "short", year: d.getFullYear() === today.getFullYear() ? undefined : "numeric" })} ${time}`;
}

function speakerName(speakers, id) {
  const n = (speakers && speakers[id]) || "";
  return n.trim() ? n.trim() : `Talare ${Number(id) + 1}`;
}

const spClass = (id) => `sp${Number(id) % 8}`;

const STATUS = {
  recording: "Spelas in",
  processing: "Bearbetas",
  ready: "Klar",
  failed: "Misslyckades",
};

function badge(status) {
  return h("span", { class: `badge ${status}` }, STATUS[status] || status);
}

/** Text med «träffar» från sökningen → <mark>. */
function snippetNode(text) {
  const out = h("div", { class: "snippet" });
  const parts = String(text).split(/[«»]/);
  parts.forEach((p, i) => out.append(i % 2 ? h("mark", {}, p) : p));
  return out;
}

function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function confirmDialog(title, text, okLabel = "OK", danger = false) {
  return new Promise((resolve) => {
    const dlg = h(
      "dialog",
      {},
      h("h2", {}, title),
      h("p", {}, text),
      h(
        "div",
        { class: "row" },
        h("span", { class: "spacer" }),
        h("button", { class: "btn", onclick: () => dlg.close("no") }, "Avbryt"),
        h("button", { class: `btn ${danger ? "rec" : "primary"}`, onclick: () => dlg.close("ok") }, okLabel),
      ),
    );
    dlg.addEventListener("close", () => {
      dlg.remove();
      resolve(dlg.returnValue === "ok");
    });
    document.body.append(dlg);
    dlg.showModal();
  });
}

function promptDialog(title, label, value = "", okLabel = "Spara") {
  return new Promise((resolve) => {
    const input = h("input", { type: "text", value, maxlength: 60 });
    const form = h(
      "form",
      { method: "dialog" },
      h("h2", {}, title),
      h("label", {}, label),
      input,
      h(
        "div",
        { class: "row", style: "margin-top:16px" },
        h("span", { class: "spacer" }),
        h("button", { class: "btn", value: "no", type: "button", onclick: () => dlg.close("no") }, "Avbryt"),
        h("button", { class: "btn primary", value: "ok" }, okLabel),
      ),
    );
    const dlg = h("dialog", {}, form);
    dlg.addEventListener("close", () => {
      dlg.remove();
      resolve(dlg.returnValue === "ok" ? input.value.trim() : null);
    });
    document.body.append(dlg);
    dlg.showModal();
    input.select();
  });
}

// ---------------------------------------------------------------- tillstånd
const state = {
  me: null,
  folders: [],
  tags: [],
  filter: Object.assign({ folder: "all", tag: "", q: "", sort: "created", order: "desc" }, store("filter") || {}),
  recorder: null,
};

async function loadMe() {
  try {
    state.me = await api("GET", "/api/me");
  } catch {
    state.me = null;
  }
}

async function loadSidebarData() {
  const [folders, tags] = await Promise.all([api("GET", "/api/folders"), api("GET", "/api/tags")]);
  state.folders = folders;
  state.tags = tags;
}

// ---------------------------------------------------------------- ramverk
function topbar() {
  const menuPop = h(
    "div",
    { class: "card menu-pop", hidden: true },
    h("div", { class: "who" }, state.me.user.name || state.me.user.email),
    h("button", { class: "nav-item", onclick: () => go("#/installningar") }, "Inställningar"),
    state.me.user.role === "admin" ? h("button", { class: "nav-item", onclick: () => go("#/admin") }, "Administration") : null,
    h(
      "button",
      {
        class: "nav-item",
        onclick: async () => {
          await api("POST", "/api/logout").catch(() => {});
          state.me = null;
          go("#/logga-in");
        },
      },
      "Logga ut",
    ),
  );
  const close = (e) => {
    if (!menu.contains(e.target)) {
      menuPop.hidden = true;
      document.removeEventListener("click", close);
    }
  };
  const menu = h(
    "div",
    { class: "menu" },
    h(
      "button",
      {
        class: "btn ghost",
        "aria-label": "Kontomeny",
        onclick: () => {
          menuPop.hidden = !menuPop.hidden;
          if (!menuPop.hidden) setTimeout(() => document.addEventListener("click", close), 0);
        },
      },
      h("span", { class: "hide-mobile" }, state.me.user.name || state.me.user.email),
      " ▾",
    ),
    menuPop,
  );
  return h(
    "header",
    { class: "topbar" },
    h("a", { class: "brand", href: "#/" }, h("img", { src: "/favicon.svg", alt: "" }), h("span", {}, "Transkribering")),
    h("span", { class: "spacer" }),
    h("a", { class: "btn rec", href: "#/ny" }, "● ", h("span", {}, "Ny inspelning")),
    menu,
  );
}

// ---------------------------------------------------------------- inloggning
function renderLogin() {
  const err = h("div", { class: "error", hidden: true });
  const email = h("input", { type: "email", autocomplete: "username", required: true, autofocus: true });
  const pw = h("input", { type: "password", autocomplete: "current-password", required: true });
  const btn = h("button", { class: "btn primary", type: "submit" }, "Logga in");
  const form = h(
    "form",
    {
      onsubmit: async (e) => {
        e.preventDefault();
        btn.disabled = true;
        err.hidden = true;
        try {
          await api("POST", "/api/login", { email: email.value, password: pw.value });
          await loadMe();
          go("#/");
        } catch (ex) {
          err.textContent = ex.message;
          err.hidden = false;
        } finally {
          btn.disabled = false;
        }
      },
    },
    h("label", {}, "E-post"),
    email,
    h("label", {}, "Lösenord"),
    pw,
    err,
    btn,
  );
  mount(
    h(
      "div",
      { class: "auth" },
      h(
        "div",
        { class: "card" },
        h("div", { class: "brand", style: "margin-bottom:18px" }, h("img", { src: "/favicon.svg", alt: "" }), "Transkribering"),
        h("h1", {}, "Logga in"),
        h("p", { class: "muted", style: "margin:0" }, "Konton skapas via inbjudan från en administratör."),
        form,
      ),
    ),
  );
}

async function renderInvite(token) {
  mount(h("p", { class: "loading" }, "Kontrollerar inbjudan …"));
  let info;
  try {
    info = await api("GET", `/api/invites/${encodeURIComponent(token)}`);
  } catch (ex) {
    mount(h("div", { class: "auth" }, h("div", { class: "card" }, h("h1", {}, "Inbjudan"), h("div", { class: "error" }, ex.message), h("p", {}, h("a", { href: "#/logga-in" }, "Till inloggningen")))));
    return;
  }
  const err = h("div", { class: "error", hidden: true });
  const name = h("input", { type: "text", autocomplete: "name", maxlength: 80 });
  const email = h("input", { type: "email", autocomplete: "username", required: true });
  const pw = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const pw2 = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const btn = h("button", { class: "btn primary", type: "submit" }, "Skapa konto");
  mount(
    h(
      "div",
      { class: "auth" },
      h(
        "div",
        { class: "card" },
        h("h1", {}, info.role === "admin" ? "Skapa administratörskonto" : "Skapa ditt konto"),
        h(
          "form",
          {
            onsubmit: async (e) => {
              e.preventDefault();
              err.hidden = true;
              if (pw.value !== pw2.value) {
                err.textContent = "Lösenorden matchar inte.";
                err.hidden = false;
                return;
              }
              btn.disabled = true;
              try {
                await api("POST", `/api/invites/${encodeURIComponent(token)}`, { name: name.value, email: email.value, password: pw.value });
                await loadMe();
                go("#/");
              } catch (ex) {
                err.textContent = ex.message;
                err.hidden = false;
              } finally {
                btn.disabled = false;
              }
            },
          },
          h("label", {}, "Namn"),
          name,
          h("label", {}, "E-post"),
          email,
          h("label", {}, "Lösenord (minst 10 tecken)"),
          pw,
          h("label", {}, "Upprepa lösenordet"),
          pw2,
          err,
          btn,
        ),
      ),
    ),
  );
}

// ---------------------------------------------------------------- lista
function sidebar(onChange) {
  const f = state.filter;
  const item = (label, active, onclick, count, extra) =>
    h("button", { class: `nav-item${active ? " active" : ""}`, onclick }, h("span", {}, label), extra, count !== undefined ? h("span", { class: "count" }, count) : null);
  const setFolder = (v) => {
    f.folder = v;
    f.tag = "";
    onChange();
  };
  const folders = state.folders.map((fo) =>
    item(
      fo.name,
      f.folder === String(fo.id),
      () => setFolder(String(fo.id)),
      fo.count,
      h(
        "span",
        {
          class: "edit",
          title: "Byt namn eller ta bort",
          onclick: async (e) => {
            e.stopPropagation();
            const name = await promptDialog("Mapp", "Namn (töm fältet för att ta bort mappen)", fo.name);
            if (name === null) return;
            try {
              if (name === "") {
                if (!(await confirmDialog("Ta bort mappen?", `Transkriberingarna i "${fo.name}" flyttas till Utan mapp.`, "Ta bort", true))) return;
                await api("DELETE", `/api/folders/${fo.id}`);
                if (f.folder === String(fo.id)) f.folder = "all";
              } else {
                await api("PATCH", `/api/folders/${fo.id}`, { name });
              }
              await loadSidebarData();
              onChange();
            } catch (ex) {
              toast(ex.message);
            }
          },
        },
        "✎",
      ),
    ),
  );
  return h(
    "nav",
    { class: "sidebar" },
    h("h3", {}, "Mappar"),
    item("Alla", f.folder === "all" && !f.tag, () => setFolder("all")),
    item("Utan mapp", f.folder === "none", () => setFolder("none")),
    folders,
    h(
      "button",
      {
        class: "nav-item muted",
        onclick: async () => {
          const name = await promptDialog("Ny mapp", "Namn", "", "Skapa");
          if (!name) return;
          try {
            const fo = await api("POST", "/api/folders", { name });
            await loadSidebarData();
            setFolder(String(fo.id));
          } catch (ex) {
            toast(ex.message);
          }
        },
      },
      "+ Ny mapp",
    ),
    state.tags.length ? h("h3", {}, "Taggar") : null,
    state.tags.map((t) =>
      item(
        `# ${t.tag}`,
        f.tag === t.tag,
        () => {
          f.tag = f.tag === t.tag ? "" : t.tag;
          f.folder = "all";
          onChange();
        },
        t.count,
      ),
    ),
  );
}

async function renderList() {
  await loadSidebarData();
  const f = state.filter;
  const listEl = h("div", { class: "list" });
  const layout = h("div", { class: "layout" });
  let seq = 0;

  const refresh = async () => {
    store("filter", f);
    layout.firstChild?.replaceWith(sidebar(refresh));
    const params = new URLSearchParams({ folder: f.folder, sort: f.sort, order: f.order });
    if (f.tag) params.set("tag", f.tag);
    if (f.q.trim()) params.set("q", f.q.trim());
    const my = ++seq;
    let items;
    try {
      items = await api("GET", `/api/transcripts?${params}`);
    } catch (ex) {
      listEl.replaceChildren(h("div", { class: "error" }, ex.message));
      return;
    }
    if (my !== seq) return;
    const folderName = (id) => state.folders.find((x) => x.id === id)?.name;
    if (!items.length) {
      listEl.replaceChildren(
        h(
          "div",
          { class: "empty card" },
          f.q || f.tag || f.folder !== "all"
            ? "Inga transkriberingar matchar."
            : h("div", {}, h("p", {}, "Inga transkriberingar än."), h("a", { class: "btn rec", href: "#/ny" }, "● Starta din första inspelning")),
        ),
      );
      return;
    }
    listEl.replaceChildren(
      ...items.map((t) =>
        h(
          "a",
          { class: "card item", href: `#/t/${t.id}` },
          h("div", { class: "title" }, t.title),
          h(
            "div",
            { class: "meta" },
            h("span", {}, fmtDate(t.createdAt)),
            t.durationSec ? h("span", {}, fmtDuration(t.durationSec)) : null,
            t.status !== "ready" ? badge(t.status) : null,
            t.folderId && f.folder === "all" ? h("span", {}, `📁 ${folderName(t.folderId) || ""}`) : null,
            t.tags.map((tag) => h("span", { class: "tag" }, `#${tag}`)),
          ),
          t.snippet ? snippetNode(t.snippet) : null,
        ),
      ),
    );
  };

  let debounce;
  const search = h("input", {
    type: "search",
    placeholder: "Sök i titlar och text …",
    value: f.q,
    oninput: (e) => {
      f.q = e.target.value;
      clearTimeout(debounce);
      debounce = setTimeout(refresh, 250);
    },
  });
  const sort = h(
    "select",
    {
      "aria-label": "Sortering",
      onchange: (e) => {
        const [s, o] = e.target.value.split(":");
        f.sort = s;
        f.order = o;
        refresh();
      },
    },
    [
      ["created:desc", "Nyast först"],
      ["created:asc", "Äldst först"],
      ["updated:desc", "Senast ändrad"],
      ["title:asc", "Titel A–Ö"],
      ["title:desc", "Titel Ö–A"],
      ["duration:desc", "Längst först"],
      ["duration:asc", "Kortast först"],
    ].map(([v, l]) => h("option", { value: v, selected: `${f.sort}:${f.order}` === v }, l)),
  );

  layout.append(sidebar(refresh), h("main", {}, getStartedRow(), h("div", { class: "toolbar" }, search, sort), listEl));
  mount(topbar(), layout);
  await refresh();
}

// ---------------------------------------------------------------- segment
/**
 * Visar stycken. I redigeringsläge kan text, talare och tid användas; onChange anropas med
 * hela listan när något ändrats.
 */
function segmentRow(seg, i, speakers, opts = {}) {
  const who = speakerName(speakers, seg.speaker);
  let whoEl;
  if (opts.editable && opts.onSpeaker) {
    const ids = opts.speakerIds();
    const maxId = ids.reduce((m, x) => Math.max(m, Number(x)), -1);
    whoEl = h(
      "select",
      {
        class: `who ${spClass(seg.speaker)}`,
        "aria-label": "Talare",
        onchange: (e) => opts.onSpeaker(i, e.target.value),
      },
      ids.map((id) => h("option", { value: id, selected: id === seg.speaker }, speakerName(speakers, id))),
      h("option", { value: String(maxId + 1) }, "+ Ny talare"),
    );
  } else {
    whoEl = h("span", { class: `who ${spClass(seg.speaker)}` }, who);
  }
  const text = h("div", { class: "text" }, seg.text);
  if (opts.editable) {
    text.setAttribute("contenteditable", "true");
    text.setAttribute("spellcheck", "true");
    text.addEventListener("blur", () => {
      const v = text.innerText.replace(/\s+/g, " ").trim();
      if (v !== seg.text) opts.onText(i, v);
    });
    text.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        text.blur();
      }
      if (e.key === "Escape") {
        text.textContent = seg.text;
        text.blur();
      }
    });
    text.addEventListener("paste", (e) => {
      e.preventDefault();
      const t = (e.clipboardData || window.clipboardData).getData("text/plain");
      document.execCommand("insertText", false, t);
    });
  }
  const time = opts.onSeek
    ? h("button", { class: "time", title: "Spela upp härifrån", onclick: () => opts.onSeek(seg.start) }, fmtTime(seg.start))
    : h("span", { class: "time" }, fmtTime(seg.start));
  return h("div", { class: "seg", "data-i": i }, h("div", { class: "gutter" }, time), whoEl, text);
}

// ---------------------------------------------------------------- inspelning
class Recorder {
  constructor({ id, source, onStatus, onSegments, onInterim, onNotice, onLevel, onStopped }) {
    Object.assign(this, { id, source, onStatus, onSegments, onInterim, onNotice, onLevel, onStopped });
    this.segments = [];
    this.pending = []; // ljud som väntar på (åter)anslutning
    this.pendingBytes = 0;
    this.paused = false;
    this.stopping = false;
    this.stopped = false;
    this.seconds = 0;
    this.offset = 0;
    this.retries = 0;
    this.streams = [];
  }

  async start() {
    const constraints = { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 };
    let display = null;
    if (this.source === "tab") {
      if (!navigator.mediaDevices.getDisplayMedia) throw new Error("Webbläsaren kan inte dela ljud från flikar. Använd Chrome eller Edge på dator.");
      display = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
        systemAudio: "include",
        selfBrowserSurface: "exclude",
        preferCurrentTab: false,
      });
      if (display.getAudioTracks().length === 0) {
        display.getTracks().forEach((t) => t.stop());
        throw new Error('Inget ljud delades. Välj en flik och kryssa i "Dela även flikens ljud".');
      }
      display.getVideoTracks().forEach((t) => t.stop());
      this.streams.push(display);
      display.getAudioTracks()[0].addEventListener("ended", () => this.onNotice("Delningen av flikens ljud avslutades – mikrofonen spelas fortfarande in."));
    }
    let mic = null;
    try {
      mic = await navigator.mediaDevices.getUserMedia({ audio: constraints });
      this.streams.push(mic);
    } catch (e) {
      if (!display) throw new Error("Ingen åtkomst till mikrofonen. Tillåt mikrofonen för sidan i webbläsarens inställningar.");
      this.onNotice("Mikrofonen kunde inte användas – bara flikens ljud spelas in.");
    }

    this.ctx = new AudioContext();
    await this.ctx.audioWorklet.addModule("/pcm-worklet.js?v=1");
    const mix = this.ctx.createGain();
    mix.channelCount = 1;
    mix.channelCountMode = "explicit";
    mix.channelInterpretation = "speakers";
    for (const s of this.streams) this.ctx.createMediaStreamSource(s).connect(mix);
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    mix.connect(this.analyser);
    this.node = new AudioWorkletNode(this.ctx, "pcm-downsampler", { channelCount: 1, channelCountMode: "explicit", numberOfOutputs: 0 });
    mix.connect(this.node);
    this.node.port.onmessage = (e) => this.onPcm(e.data);
    this.levelLoop();

    try {
      this.wakeLock = await navigator.wakeLock?.request("screen");
    } catch {
      /* inte viktigt */
    }
    this.connect();
  }

  levelLoop() {
    const buf = new Float32Array(this.analyser.fftSize);
    const tick = () => {
      if (this.stopped) return;
      this.analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += v * v;
      const rms = Math.sqrt(sum / buf.length);
      this.onLevel(this.paused ? 0 : Math.min(1, rms * 4));
      this.raf = requestAnimationFrame(tick);
    };
    tick();
  }

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/live/${this.id}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    this.ready = false;
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      switch (msg.type) {
        case "ready":
          this.ready = true;
          this.retries = 0;
          this.offset = msg.offset;
          this.liveEnabled = msg.live;
          if (!this.segments.length && msg.segments?.length) {
            this.segments = msg.segments;
            this.onSegments(this.segments, 0);
          }
          for (const b of this.pending) ws.send(b);
          this.pending = [];
          this.pendingBytes = 0;
          this.onStatus(msg.live ? "live" : "nolive");
          if (this.stopping) ws.send(JSON.stringify({ type: "stop" }));
          break;
        case "segments":
          this.segments.splice(msg.from, Infinity, ...msg.segments);
          this.onSegments(this.segments, msg.from);
          break;
        case "interim":
          this.onInterim(msg.words);
          break;
        case "notice":
          this.onNotice(msg.message);
          break;
        case "stopping":
          this.onStatus("stopping");
          break;
        case "stopped":
          this.stopped = true;
          this.cleanup();
          this.onStopped(this.id);
          break;
      }
    };
    ws.onclose = (e) => {
      if (this.stopped) return;
      if ([4401, 4404, 4409, 4429].includes(e.code)) {
        this.stopped = true;
        this.cleanup();
        this.onNotice(e.reason || "Inspelningen kunde inte fortsätta.");
        this.onStatus("error");
        return;
      }
      if (e.code === 4000) return; // övertagen av annat fönster
      this.onStatus("reconnecting");
      const delay = Math.min(15000, 1000 * 2 ** this.retries++);
      setTimeout(() => !this.stopped && this.connect(), delay);
    };
  }

  onPcm(buf) {
    if (this.paused || this.stopped) return;
    this.seconds += buf.byteLength / 32000;
    if (this.ws && this.ws.readyState === WebSocket.OPEN && this.ready && !this.stopping) {
      this.ws.send(buf);
    } else if (this.pendingBytes < 32000 * 60 * 20) {
      // Upp till 20 min ljud sparas i minnet medan anslutningen är nere.
      this.pending.push(buf);
      this.pendingBytes += buf.byteLength;
    }
  }

  setPaused(p) {
    this.paused = p;
    this.node?.port.postMessage({ type: "pause", paused: p });
  }

  stop() {
    if (this.stopping) return;
    this.stopping = true;
    this.node?.port.postMessage({ type: "pause", paused: true });
    // Mikrofonen släpps direkt; buffrat ljud skickas innan stopp.
    this.streams.forEach((s) => s.getTracks().forEach((t) => t.stop()));
    if (this.ws && this.ws.readyState === WebSocket.OPEN && this.ready) {
      for (const b of this.pending) this.ws.send(b);
      this.pending = [];
      this.ws.send(JSON.stringify({ type: "stop" }));
    }
  }

  cleanup() {
    cancelAnimationFrame(this.raf);
    this.streams.forEach((s) => s.getTracks().forEach((t) => t.stop()));
    try {
      this.ctx?.close();
    } catch {
      /* ignorera */
    }
    this.wakeLock?.release?.().catch(() => {});
    if (state.recorder === this) state.recorder = null;
  }
}

async function renderNew(resumeId) {
  if (state.recorder) {
    renderRecording(state.recorder);
    return;
  }
  await loadSidebarData();
  let source = store("source") || "mic";
  const title = h("input", { type: "text", maxlength: 200, placeholder: "T.ex. Styrelsemöte, intervju med …" });
  const folder = h(
    "select",
    {},
    h("option", { value: "" }, "Utan mapp"),
    state.folders.map((f) => h("option", { value: f.id, selected: state.filter.folder === String(f.id) }, f.name)),
  );
  const context = h("textarea", {
    rows: 2,
    maxlength: 1000,
    placeholder: "T.ex. Anna Lindqvist, Kvarnholmen, SMGD, budgetprognos",
  });
  const err = h("div", { class: "error", hidden: true });
  const srcBtn = (id, label, desc) =>
    h(
      "button",
      {
        class: "source",
        type: "button",
        "aria-pressed": String(source === id),
        onclick: (e) => {
          source = id;
          store("source", id);
          for (const b of e.currentTarget.parentElement.children) b.setAttribute("aria-pressed", String(b === e.currentTarget));
        },
      },
      h("strong", {}, label),
      h("span", {}, desc),
    );
  const tabSupported = !!navigator.mediaDevices?.getDisplayMedia && !/Android|iPhone|iPad/i.test(navigator.userAgent);
  const startBtn = h("button", { class: "btn rec big", type: "submit" }, "● Starta inspelning");

  const startRecording = async (id, src) => {
    const rec = new Recorder({ id, source: src });
    state.recorder = rec;
    renderRecording(rec);
    try {
      await rec.start();
    } catch (ex) {
      rec.stopped = true;
      rec.cleanup();
      state.recorder = null;
      // Ingen inspelning blev av: ta bort den tomma posten.
      if (!resumeId) await api("DELETE", `/api/transcripts/${id}`).catch(() => {});
      throw ex;
    }
  };

  if (resumeId) {
    let t;
    try {
      t = await api("GET", `/api/transcripts/${resumeId}`);
    } catch (ex) {
      toast(ex.message);
      return go("#/");
    }
    mount(
      topbar(),
      h(
        "div",
        { class: "page" },
        h(
          "div",
          { class: "card recorder" },
          h("h1", { style: "margin-top:0" }, "Fortsätt inspelningen"),
          h("p", {}, `"${t.title}" – ${fmtDuration(t.durationSec)} inspelat hittills. Ljudet läggs till på slutet.`),
          err,
          h(
            "div",
            { class: "row", style: "margin-top:16px" },
            srcBtn("mic", "Mikrofon", ""),
            tabSupported ? srcBtn("tab", "Flik + mikrofon", "") : null,
          ),
          h(
            "div",
            { class: "row", style: "margin-top:20px" },
            h(
              "button",
              {
                class: "btn rec big",
                onclick: async () => {
                  try {
                    await startRecording(resumeId, source);
                  } catch (ex) {
                    renderNew(resumeId).then(() => toast(ex.message));
                  }
                },
              },
              "● Fortsätt",
            ),
            h("a", { class: "btn", href: `#/t/${resumeId}` }, "Avbryt"),
          ),
        ),
      ),
    );
    return;
  }

  mount(
    topbar(),
    h(
      "div",
      { class: "page" },
      h(
        "form",
        {
          class: "card recorder",
          onsubmit: async (e) => {
            e.preventDefault();
            err.hidden = true;
            startBtn.disabled = true;
            try {
              const { id } = await api("POST", "/api/transcripts", {
                title: title.value,
                source,
                context: context.value,
                folderId: folder.value || null,
              });
              await startRecording(id, source);
            } catch (ex) {
              err.textContent = ex.message;
              err.hidden = false;
              startBtn.disabled = false;
              if (!state.recorder && location.hash.startsWith("#/ny")) {
                // Formuläret visas igen om inspelningen inte kunde starta.
                renderNew().then(() => toast(ex.message));
              }
            }
          },
        },
        h("h1", { style: "margin-top:0" }, "Ny inspelning"),
        serviceWarning(),
        h("label", {}, "Ljudkälla"),
        h(
          "div",
          { class: "sources" },
          srcBtn("mic", "🎙 Mikrofon", "Möten i rummet, diktering, intervjuer"),
          tabSupported
            ? srcBtn("tab", "🖥 Flik + mikrofon", "Teams, Meet, Zoom i webbläsaren – du väljer fliken och kryssar i ”Dela ljud”")
            : h("div", { class: "source", "aria-disabled": "true" }, h("strong", {}, "🖥 Flik + mikrofon"), h("span", {}, "Kräver Chrome eller Edge på dator")),
        ),
        h("div", { class: "detail-grid" }, h("div", {}, h("label", {}, "Titel"), title), h("div", {}, h("label", {}, "Mapp"), folder)),
        h("label", {}, "Namn och facktermer (valfritt)"),
        context,
        h("p", { class: "hint" }, "Kommaseparerade ord som förekommer i samtalet. Ger rätt stavning av namn och begrepp i slutversionen."),
        err,
        h("div", { class: "row", style: "margin-top:20px" }, startBtn, h("span", { class: "hint" }, `Max ${state.me.limits.maxRecordingMinutes} min. Live-texten är ett utkast – en exakt slutversion skapas när du stoppar.`)),
      ),
    ),
  );
}

function renderRecording(rec) {
  const timer = h("div", { class: "timer" }, "00:00");
  const dot = h("div", { class: "dot" });
  const meterBar = h("div");
  const meter = h("div", { class: "meter", title: "Ljudnivå" }, meterBar);
  const status = h("span", { class: "muted" }, "Ansluter …");
  const notices = h("div");
  const segsEl = h("div", { class: "transcript" });
  const interim = h("div", { class: "seg interim", hidden: true }, h("div", { class: "gutter" }), h("span", { class: "who" }), h("div", { class: "text" }));
  const pauseBtn = h("button", { class: "btn" }, "❚❚ Paus");
  const stopBtn = h("button", { class: "btn rec" }, "■ Stoppa");
  let follow = true;

  const scroll = () => {
    if (follow) window.scrollTo({ top: document.body.scrollHeight });
  };
  window.addEventListener(
    "scroll",
    () => {
      follow = window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
    },
    { passive: true },
  );

  rec.onLevel = (v) => (meterBar.style.width = `${Math.round(v * 100)}%`);
  rec.onStatus = (s) => {
    status.textContent = {
      live: "Live-text på",
      nolive: "Spelar in (live-text av)",
      reconnecting: "Anslutningen bröts – ljudet sparas lokalt och skickas när den är tillbaka …",
      stopping: "Avslutar …",
      error: "Avbruten",
    }[s];
  };
  rec.onNotice = (m) => notices.append(h("div", { class: "banner warn" }, m));
  rec.onSegments = (segs, from) => {
    // Ersätt från och med det första ändrade stycket.
    while (segsEl.children.length > from) segsEl.lastChild.remove();
    for (let i = from; i < segs.length; i++) segsEl.append(segmentRow(segs[i], i, {}));
    interim.hidden = true;
    segsEl.append(interim);
    scroll();
  };
  rec.onInterim = (words) => {
    if (!words.length) {
      interim.hidden = true;
      return;
    }
    const sp = words[0].speaker ?? "0";
    interim.children[1].className = `who ${spClass(sp)}`;
    interim.children[1].textContent = speakerName({}, sp);
    interim.children[2].textContent = words.map((w) => w.text).join(" ");
    interim.hidden = false;
    segsEl.append(interim);
    scroll();
  };
  rec.onStopped = (id) => {
    clearInterval(tick);
    window.removeEventListener("beforeunload", beforeUnload);
    toast("Inspelningen är sparad – slutversionen skapas nu.");
    go(`#/t/${id}`);
  };
  pauseBtn.onclick = () => {
    rec.setPaused(!rec.paused);
    pauseBtn.textContent = rec.paused ? "▶ Fortsätt" : "❚❚ Paus";
    dot.classList.toggle("paused", rec.paused);
  };
  stopBtn.onclick = () => {
    stopBtn.disabled = true;
    pauseBtn.disabled = true;
    status.textContent = "Avslutar …";
    rec.stop();
  };
  const beforeUnload = (e) => {
    e.preventDefault();
    e.returnValue = "";
  };
  window.addEventListener("beforeunload", beforeUnload);
  const tick = setInterval(() => {
    if (!state.recorder && rec.stopped) clearInterval(tick);
    timer.textContent = fmtTime(rec.offset + rec.seconds);
  }, 250);

  if (rec.segments.length) rec.onSegments(rec.segments, 0);

  mount(
    topbar(),
    h(
      "div",
      { class: "page" },
      h(
        "div",
        { class: "card recorder" },
        h("div", { class: "rec-head" }, dot, timer, meter, pauseBtn, stopBtn),
        h("div", { style: "margin-top:8px" }, status),
        notices,
      ),
      segsEl,
    ),
  );
}

// ---------------------------------------------------------------- en transkribering
async function renderTranscript(id) {
  let t;
  try {
    t = await api("GET", `/api/transcripts/${id}`);
  } catch (ex) {
    mount(topbar(), h("div", { class: "page" }, h("div", { class: "error" }, ex.message), h("p", {}, h("a", { href: "#/" }, "← Till listan"))));
    return;
  }
  await loadSidebarData();
  const view = { version: t.segments.length ? "final" : "live" };
  let pollTimer;

  const save = async (patch) => {
    try {
      t = await api("PATCH", `/api/transcripts/${id}`, patch);
      toast("Sparat");
    } catch (ex) {
      toast(ex.message);
    }
  };

  const titleInput = h("input", {
    class: "title-input",
    value: t.title,
    maxlength: 200,
    "aria-label": "Titel",
    onchange: (e) => e.target.value.trim() && save({ title: e.target.value.trim() }),
    onkeydown: (e) => e.key === "Enter" && e.target.blur(),
  });

  const folderSel = h(
    "select",
    { "aria-label": "Mapp", onchange: (e) => save({ folderId: e.target.value || null }) },
    h("option", { value: "" }, "Utan mapp"),
    state.folders.map((f) => h("option", { value: f.id, selected: f.id === t.folderId }, f.name)),
  );

  const tagsBox = h("div", { class: "tags-edit" });
  const renderTags = () => {
    const input = h("input", {
      type: "text",
      placeholder: t.tags.length ? "" : "Lägg till tagg …",
      list: "tag-suggestions",
      onkeydown: (e) => {
        if ((e.key === "Enter" || e.key === ",") && input.value.trim()) {
          e.preventDefault();
          const v = input.value.trim().toLowerCase();
          if (!t.tags.includes(v)) save({ tags: [...t.tags, v] }).then(renderTags);
          input.value = "";
        } else if (e.key === "Backspace" && !input.value && t.tags.length) {
          save({ tags: t.tags.slice(0, -1) }).then(renderTags);
        }
      },
      onblur: () => {
        const v = input.value.trim().toLowerCase();
        if (v && !t.tags.includes(v)) save({ tags: [...t.tags, v] }).then(renderTags);
      },
    });
    tagsBox.replaceChildren(
      ...t.tags.map((tag) =>
        h("span", { class: "tag" }, `#${tag}`, h("button", { "aria-label": `Ta bort ${tag}`, onclick: () => save({ tags: t.tags.filter((x) => x !== tag) }).then(renderTags) }, "×")),
      ),
      input,
      h("datalist", { id: "tag-suggestions" }, state.tags.map((x) => h("option", { value: x.tag }))),
    );
  };
  renderTags();

  const banners = h("div");
  const speakersEl = h("div", { class: "speakers" });
  const segsEl = h("div", { class: "transcript" });
  const tabsEl = h("div");
  let audio = null;

  const currentSegs = () => (view.version === "final" ? t.segments : t.liveSegments);
  const editable = () => t.status === "ready" || t.status === "failed";

  const renderSpeakers = () => {
    const ids = [...new Set(currentSegs().map((s) => s.speaker))].sort((a, b) => Number(a) - Number(b));
    speakersEl.replaceChildren(
      ...ids.map((sid) =>
        h(
          "div",
          { class: "row" },
          h("span", { class: `spdot ${spClass(sid)}` }),
          h("input", {
            type: "text",
            value: t.speakers[sid] || "",
            placeholder: `Talare ${Number(sid) + 1}`,
            maxlength: 60,
            "aria-label": `Namn på talare ${Number(sid) + 1}`,
            onchange: async (e) => {
              await save({ speakers: { ...t.speakers, [sid]: e.target.value } });
              renderSegs();
            },
          }),
        ),
      ),
    );
  };

  const saveSegs = async (segs) => {
    await save({ segments: segs });
    renderSpeakers();
  };

  const renderSegs = () => {
    const segs = currentSegs();
    if (!segs.length) {
      segsEl.replaceChildren(
        h("div", { class: "empty" }, t.status === "processing" ? "Texten visas här när den är klar." : "Ingen text."),
      );
      return;
    }
    const ids = () => [...new Set(currentSegs().map((s) => s.speaker))].sort((a, b) => Number(a) - Number(b));
    segsEl.replaceChildren(
      ...segs.map((s, i) =>
        segmentRow(s, i, t.speakers, {
          editable: editable() && (view.version === "final" || !t.segments.length),
          speakerIds: ids,
          onSeek: audio
            ? (sec) => {
                audio.currentTime = sec;
                audio.play().catch(() => {});
              }
            : null,
          onText: (idx, text) => {
            const copy = currentSegs().map((x) => ({ ...x }));
            if (!text) copy.splice(idx, 1);
            else copy[idx].text = text;
            saveSegs(copy).then(renderSegs);
          },
          onSpeaker: (idx, sp) => {
            const copy = currentSegs().map((x) => ({ ...x }));
            copy[idx].speaker = sp;
            saveSegs(copy).then(renderSegs);
          },
        }),
      ),
    );
  };

  const renderTabs = () => {
    if (!(t.segments.length && t.liveSegments.length)) {
      tabsEl.replaceChildren();
      return;
    }
    const b = (v, label) =>
      h(
        "button",
        {
          "aria-pressed": String(view.version === v),
          onclick: () => {
            view.version = v;
            view.userPicked = true;
            renderTabs();
            renderMeta();
            renderBanners();
            renderSpeakers();
            renderSegs();
          },
        },
        label,
      );
    tabsEl.replaceChildren(h("div", { class: "tabs" }, b("final", "Slutversion"), b("live", "Live-utkast")));
  };

  const renderBanners = () => {
    const out = [];
    if (t.status === "processing") {
      const detail = t.statusDetail || "Bearbetas";
      const q = t.queuePosition > 0 ? ` – ${t.queuePosition} före i kön` : "";
      out.push(h("div", { class: "banner info" }, h("span", { class: "spinner" }), `Slutversionen skapas: ${detail}${q}. Du kan lämna sidan.`));
    } else if (t.status === "recording" && !t.live) {
      out.push(
        h(
          "div",
          { class: "banner warn" },
          "Inspelningen avbröts innan den stoppades.",
          h("span", { class: "spacer" }),
          h("a", { class: "btn small", href: `#/ny/${id}` }, "Fortsätt spela in"),
          h(
            "button",
            {
              class: "btn small primary",
              onclick: async () => {
                try {
                  t = await api("POST", `/api/transcripts/${id}/finish`);
                  renderAll();
                } catch (ex) {
                  toast(ex.message);
                }
              },
            },
            "Avsluta och skapa slutversion",
          ),
        ),
      );
    } else if (t.status === "recording") {
      out.push(h("div", { class: "banner info" }, h("span", { class: "spinner" }), "Spelas in just nu i ett annat fönster."));
    }
    if (t.status === "failed") out.push(h("div", { class: "banner err" }, `Slutversionen kunde inte skapas: ${t.error}`));
    else if (t.error) out.push(h("div", { class: "banner warn" }, t.error));
    if (view.version === "live" && t.status === "ready" && !t.segments.length) {
      out.push(h("div", { class: "banner warn" }, "Visar live-utkastet."));
    }
    banners.replaceChildren(...out);
  };

  const exportDialog = () => {
    const ts = h("input", { type: "checkbox", checked: store("exp-ts") ?? true });
    const sp = h("input", { type: "checkbox", checked: store("exp-sp") ?? true });
    const dlg = h(
      "dialog",
      {},
      h("h2", {}, "Exportera som textfil"),
      h("label", { class: "check" }, ts, "Tidsstämplar"),
      h("label", { class: "check" }, sp, "Talarnamn"),
      t.segments.length && t.liveSegments.length ? h("p", { class: "hint" }, `Exporterar ${view.version === "final" ? "slutversionen" : "live-utkastet"}.`) : null,
      h(
        "div",
        { class: "row", style: "margin-top:16px" },
        h("span", { class: "spacer" }),
        h("button", { class: "btn", onclick: () => dlg.close() }, "Avbryt"),
        h(
          "button",
          {
            class: "btn primary",
            onclick: () => {
              store("exp-ts", ts.checked);
              store("exp-sp", sp.checked);
              const p = new URLSearchParams({ timestamps: ts.checked ? "1" : "0", speakers: sp.checked ? "1" : "0", version: view.version });
              const fname = `${t.title.replace(/[\\/:*?"<>|]+/g, "").trim() || "transkribering"}.txt`;
              const a = h("a", { href: `/api/transcripts/${id}/export.txt?${p}`, download: fname });
              document.body.append(a);
              a.click();
              a.remove();
              dlg.close();
            },
          },
          "⤓ Ladda ner .txt",
        ),
      ),
    );
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
  };

  const copyText = async () => {
    const segs = currentSegs();
    const txt = segs.map((s) => `${speakerName(t.speakers, s.speaker)}: ${s.text}`).join("\n\n");
    try {
      await navigator.clipboard.writeText(txt);
      toast("Texten är kopierad");
    } catch {
      toast("Kunde inte kopiera");
    }
  };

  const actions = h("div", { class: "row" });
  const renderActions = () => {
    const busy = t.status === "processing" || t.status === "recording";
    actions.replaceChildren(
      h("button", { class: "btn primary", onclick: exportDialog, disabled: !currentSegs().length }, "⤓ Exportera .txt"),
      h("button", { class: "btn", onclick: copyText, disabled: !currentSegs().length }, "Kopiera text"),
      h(
        "button",
        {
          class: "btn",
          disabled: busy || !t.hasAudio,
          title: "Kör om slutversionen (t.ex. efter att ha ändrat namn och facktermer)",
          onclick: async () => {
            const edited = t.segments.length > 0;
            if (edited && !(await confirmDialog("Skapa ny slutversion?", "Den nuvarande slutversionen och dina ändringar i den ersätts.", "Kör om"))) return;
            try {
              t = await api("POST", `/api/transcripts/${id}/reprocess`);
              renderAll();
            } catch (ex) {
              toast(ex.message);
            }
          },
        },
        "↻ Kör om",
      ),
      h("span", { class: "spacer" }),
      h(
        "button",
        {
          class: "btn danger",
          onclick: async () => {
            if (!(await confirmDialog("Ta bort transkriberingen?", `"${t.title}" och ljudet raderas permanent.`, "Ta bort", true))) return;
            try {
              await api("DELETE", `/api/transcripts/${id}`);
              toast("Borttagen");
              go("#/");
            } catch (ex) {
              toast(ex.message);
            }
          },
        },
        "Ta bort",
      ),
    );
  };

  const contextInput = h("textarea", {
    rows: 2,
    maxlength: 1000,
    value: t.context,
    placeholder: "Namn och facktermer, kommaseparerade",
    onchange: (e) => save({ context: e.target.value }),
  });

  const playerBox = h("div");
  const renderPlayer = () => {
    if (!t.hasAudio) {
      playerBox.replaceChildren();
      audio = null;
      return;
    }
    if (audio) return;
    audio = h("audio", { controls: true, preload: "metadata", src: `/api/transcripts/${id}/audio` });
    const rate = h(
      "select",
      { "aria-label": "Hastighet", onchange: (e) => (audio.playbackRate = Number(e.target.value)) },
      ["0.75", "1", "1.25", "1.5", "2"].map((r) => h("option", { value: r, selected: r === "1" }, `${r.replace(".", ",")}×`)),
    );
    let followPlay = true;
    const followBox = h("input", { type: "checkbox", checked: true, onchange: (e) => (followPlay = e.target.checked) });
    let lastIdx = -1;
    audio.addEventListener("timeupdate", () => {
      const segs = currentSegs();
      const ct = audio.currentTime;
      let idx = -1;
      for (let i = 0; i < segs.length; i++) {
        if (segs[i].start <= ct + 0.05) idx = i;
        else break;
      }
      if (idx === lastIdx) return;
      segsEl.querySelector(".seg.now")?.classList.remove("now");
      const el = segsEl.querySelector(`.seg[data-i="${idx}"]`);
      if (el) {
        el.classList.add("now");
        if (followPlay && !audio.paused && !el.contains(document.activeElement)) el.scrollIntoView({ block: "center", behavior: "smooth" });
      }
      lastIdx = idx;
    });
    // Mellanslag spelar/pausar när man inte skriver.
    playerBox.replaceChildren(h("div", { class: "card player" }, audio, rate, h("label", { class: "check", style: "margin:0" }, followBox, "Följ texten")));
  };

  const meta = h("div", { class: "detail-meta" });
  const renderMeta = () => {
    const engines = { "whisper+deepgram": "Whisper + Deepgram", whisper: "Whisper", deepgram: "Deepgram" };
    meta.replaceChildren(
      h("span", {}, new Date(t.createdAt).toLocaleString("sv-SE", { dateStyle: "long", timeStyle: "short" })),
      t.durationSec ? h("span", {}, fmtDuration(t.durationSec)) : null,
      badge(t.status),
      t.finalEngine && view.version === "final" ? h("span", { class: "muted" }, engines[t.finalEngine] || t.finalEngine) : null,
    );
  };

  function renderAll() {
    if (t.segments.length && view.version === "live" && !view.userPicked) view.version = "final";
    renderMeta();
    renderBanners();
    renderTabs();
    renderPlayer();
    renderSpeakers();
    renderSegs();
    renderActions();
    clearTimeout(pollTimer);
    if (t.status === "processing" || t.status === "recording") {
      const poll = async () => {
        if (location.hash !== `#/t/${id}`) return;
        try {
          const fresh = await api("GET", `/api/transcripts/${id}`);
          const changed =
            fresh.status !== t.status ||
            fresh.statusDetail !== t.statusDetail ||
            fresh.live !== t.live ||
            fresh.queuePosition !== t.queuePosition;
          t = fresh;
          if (changed) return renderAll();
        } catch {
          /* tillfälligt fel – försök igen */
        }
        pollTimer = setTimeout(poll, 3000);
      };
      pollTimer = setTimeout(poll, 3000);
    }
  }

  mount(
    topbar(),
    h(
      "div",
      { class: "page" },
      h("p", { style: "margin:0 0 6px" }, h("a", { href: "#/" }, "← Alla transkriberingar")),
      titleInput,
      meta,
      banners,
      h("div", { class: "detail-grid" }, h("div", {}, h("label", {}, "Mapp"), folderSel), h("div", {}, h("label", {}, "Taggar"), tagsBox)),
      h("details", { style: "margin-top:12px" }, h("summary", { class: "muted", style: "cursor:pointer" }, "Namn och facktermer för omkörning"), contextInput, h("p", { class: "hint" }, "Används när slutversionen skapas. Ändra och välj sedan Kör om.")),
      h("div", { style: "margin-top:16px" }, actions),
      playerBox,
      h("div", { class: "row", style: "margin-top:12px" }, h("h2", { class: "section", style: "margin:0" }, "Talare"), h("span", { class: "spacer" }), tabsEl),
      speakersEl,
      segsEl,
    ),
  );
  renderAll();
}

// ---------------------------------------------------------------- inställningar
function renderSettings() {
  const name = h("input", { type: "text", value: state.me.user.name, maxlength: 80 });
  const cur = h("input", { type: "password", autocomplete: "current-password" });
  const next = h("input", { type: "password", autocomplete: "new-password", minlength: 10 });
  const err = h("div", { class: "error", hidden: true });
  mount(
    topbar(),
    h(
      "div",
      { class: "page", style: "max-width:520px" },
      h("h1", {}, "Inställningar"),
      h(
        "div",
        { class: "card", style: "padding:20px" },
        h("label", { style: "margin-top:0" }, "Namn"),
        name,
        h(
          "button",
          {
            class: "btn",
            style: "margin-top:10px",
            onclick: async () => {
              try {
                await api("PATCH", "/api/me", { name: name.value });
                await loadMe();
                toast("Sparat");
              } catch (ex) {
                toast(ex.message);
              }
            },
          },
          "Spara namn",
        ),
        h("p", { class: "hint" }, `E-post: ${state.me.user.email}`),
      ),
      h("h2", { class: "section" }, "Byt lösenord"),
      h(
        "form",
        {
          class: "card",
          style: "padding:20px",
          onsubmit: async (e) => {
            e.preventDefault();
            err.hidden = true;
            try {
              await api("POST", "/api/password", { current: cur.value, next: next.value });
              cur.value = next.value = "";
              toast("Lösenordet är bytt. Andra enheter har loggats ut.");
            } catch (ex) {
              err.textContent = ex.message;
              err.hidden = false;
            }
          },
        },
        h("label", { style: "margin-top:0" }, "Nuvarande lösenord"),
        cur,
        h("label", {}, "Nytt lösenord (minst 10 tecken)"),
        next,
        err,
        h("button", { class: "btn primary", style: "margin-top:14px" }, "Byt lösenord"),
      ),
    ),
  );
}

// ---------------------------------------------------------------- nycklar
// Nyckelvärdet finns bara i inmatningsfältet tills det skickas. Det sparas aldrig i webbläsaren,
// läggs aldrig i adressen och skrivs aldrig ut. Servern skickar bara tillbaka de fyra sista tecknen.
const KEY_INFO = {
  deepgram: {
    what: "Live-text under inspelningen och uppdelning på talare.",
    link: "https://console.deepgram.com",
    missing: "Utan den blir det ingen live-text och ingen uppdelning på talare.",
  },
  whisper: {
    what: "Slutversionens text (Whisper large-v3).",
    link: "https://console.groq.com/keys",
    missing: "Utan den görs slutversionen bara med Deepgram, om den nyckeln finns, och texten blir mindre exakt.",
  },
};

// Besked på korten (aldrig nyckeln själv) – ligger kvar om sidan ritas om.
let keyNotes = {};

const isGroq = (k) => /(^|\.)groq\.com$/i.test(k.host || "");
const serviceName = (k) => (k.name === "deepgram" ? "Deepgram" : isGroq(k) ? "Groq" : "Whisper-servern");
const sentence = (s) => (s && !/[.!?]$/.test(s.trim()) ? `${s.trim()}.` : (s || "").trim());

/** Samma regler som servern: 8–512 synliga tecken utan mellanslag. */
function keyFormatProblem(v) {
  if (!v) return "Klistra in nyckeln först.";
  if (v.length < 8) return "Nyckeln är för kort. Kontrollera att du fått med hela.";
  if (v.length > 512) return "Nyckeln är för lång. Kontrollera att du bara klistrat in nyckeln.";
  if (!/^[\x21-\x7e]+$/.test(v)) return "Nyckeln innehåller tecken som inte kan finnas i en nyckel, till exempel mellanslag. Kopiera den igen.";
  return "";
}

/** Läget för en nyckel i klartext: etikett, ton och förklaring. */
function keyView(k) {
  const missing = KEY_INFO[k.name].missing;
  const ends = k.hint ? `Slutar på …${k.hint}.` : null;
  if (k.unreadable) {
    return {
      tone: "err",
      label: "Oläslig",
      body: [
        h(
          "div",
          { class: "banner err" },
          h(
            "span",
            {},
            "Den sparade nyckeln går inte att läsa på servern. Lägg in den igen.",
            k.source === "env" ? " Under tiden används nyckeln i serverns inställningsfil (.env)." : k.required ? ` ${missing}` : "",
          ),
        ),
      ],
    };
  }
  if (k.source === "ui") {
    const when = k.updatedAt ? fmtDate(k.updatedAt) : "";
    return {
      tone: "ok",
      label: "Sparad",
      body: [
        h("p", {}, "Sparad här", when ? ` ${when.charAt(0).toLowerCase()}${when.slice(1)}` : "", k.updatedBy ? ` av ${k.updatedBy}` : "", "."),
        ends ? h("p", { class: "hint" }, ends) : null,
        k.envSet ? h("p", { class: "hint" }, "Den används i stället för nyckeln i serverns inställningsfil (.env).") : null,
      ],
    };
  }
  if (k.source === "env") {
    return {
      tone: "ok",
      label: "Från .env",
      body: [
        h("p", {}, "Nyckeln kommer från serverns inställningsfil (.env).", ends ? ` ${ends}` : ""),
        h("p", { class: "hint" }, "Lägger du in en nyckel här används den i stället."),
      ],
    };
  }
  if (!k.required) {
    return { tone: "neutral", label: "Behövs inte", body: [h("p", {}, `Den inställda Whisper-servern (${k.host}) kräver ingen nyckel.`)] };
  }
  return {
    tone: "warn",
    label: "Saknas",
    body: [h("div", { class: "banner warn" }, h("span", {}, h("strong", {}, "Ingen nyckel är inlagd. "), missing))],
  };
}

function keyCard(initial) {
  let k = initial;
  const name = k.name;
  const info = KEY_INFO[name];
  const ids = { title: `nyckel-${name}-rubrik`, input: `nyckel-${name}`, help: `nyckel-${name}-hjalp` };
  const titleEl = h("h3", { id: ids.title });
  const whatEl = h("p", { class: "keycard-what" });
  const pill = h("span", { class: "pill" });
  const statusEl = h("div", { class: "keycard-status" });
  const noteEl = h("div", { class: "keycard-note" });
  const labelEl = h("label", { for: ids.input });
  const helpEl = h("p", { class: "hint", id: ids.help });
  const input = h("input", {
    type: "password",
    id: ids.input,
    name: `nyckel-${name}`,
    autocomplete: "off",
    spellcheck: "false",
    autocapitalize: "off",
    autocorrect: "off",
    maxlength: 600,
    "aria-describedby": ids.help,
    "data-secret": true,
    "data-lpignore": "true",
    "data-1p-ignore": true,
    "data-bwignore": true,
  });
  const saveBtn = h("button", { class: "btn primary", type: "submit" }, "Spara");
  const testBtn = h("button", { class: "btn", type: "button", onclick: () => test() }, "Testa");
  const delBtn = h("button", { class: "btn danger", type: "button", onclick: () => remove() }, "Ta bort");
  const link = h(
    "a",
    { class: "keycard-link", href: info.link, target: "_blank", rel: "noopener noreferrer" },
    "Skaffa en nyckel",
    h("span", { class: "sr-only" }, " (öppnas i en ny flik)"),
    h("span", { "aria-hidden": "true" }, " ↗"),
  );
  const form = h(
    "form",
    { class: "keycard-form", onsubmit: (e) => save(e) },
    labelEl,
    h("div", { class: "key-entry" }, input, saveBtn),
    helpEl,
    h("div", { class: "keycard-actions" }, testBtn, delBtn, link),
  );
  const card = h(
    "section",
    { class: "card keycard", "aria-labelledby": ids.title },
    h("div", { class: "keycard-head" }, h("div", {}, titleEl, whatEl), pill),
    statusEl,
    noteEl,
    form,
  );

  let busy = false;
  const setNote = (tone, text) => (keyNotes[name] = { tone, text });

  function paint() {
    const svc = serviceName(k);
    const v = keyView(k);
    titleEl.textContent = svc;
    whatEl.textContent = name === "whisper" && !isGroq(k) ? `Slutversionens text, via ${k.host}.` : info.what;
    pill.className = `pill ${v.tone}`;
    pill.textContent = v.label;
    statusEl.replaceChildren(...v.body.filter(Boolean));
    const note = keyNotes[name];
    noteEl.replaceChildren(...(note ? [h("div", { class: `banner ${note.tone}` }, h("span", {}, note.tone === "ok" ? "✓ " : "", note.text))] : []));
    labelEl.textContent = k.set ? "Byt till en ny nyckel" : k.required ? `Klistra in nyckeln till ${svc}` : "Nyckel (behövs bara om servern kräver det)";
    helpEl.textContent = `Nyckeln kontrolleras hos ${k.host} innan den sparas.`;
    testBtn.hidden = !k.set;
    delBtn.hidden = k.source !== "ui";
    link.hidden = name === "whisper" && !isGroq(k);
    card.classList.toggle("attention", v.tone === "warn" || v.tone === "err");
  }

  function setBusy(on, btn, label) {
    busy = on;
    card.setAttribute("aria-busy", String(on));
    input.readOnly = on;
    for (const b of [saveBtn, testBtn, delBtn]) b.disabled = on;
    if (on) {
      btn.dataset.label = btn.textContent;
      btn.textContent = label;
    } else if (btn.dataset.label) {
      btn.textContent = btn.dataset.label;
      delete btn.dataset.label;
    }
  }

  async function afterChange() {
    // Resten av appen (t.ex. Ny inspelning) ska se det nya läget direkt.
    await loadMe();
  }

  async function save(e) {
    e.preventDefault();
    if (busy) return;
    const value = input.value.trim();
    const problem = keyFormatProblem(value);
    if (problem) {
      setNote("err", problem);
      paint();
      input.focus();
      return;
    }
    setBusy(true, saveBtn, "Kontrollerar …");
    try {
      const res = await api("PUT", `/api/admin/keys/${name}`, { key: value });
      input.value = "";
      k = res.key;
      if (res.check.result === "ok") setNote("ok", "Nyckeln fungerar och är sparad.");
      else setNote("warn", `Nyckeln är sparad, men den kunde inte kontrolleras just nu. ${sentence(res.check.detail)} Tryck på Testa om en stund.`);
      await afterChange();
    } catch (ex) {
      setNote("err", ex.message);
    } finally {
      setBusy(false, saveBtn);
      paint();
      saveBtn.focus();
    }
  }

  async function test() {
    if (busy) return;
    const svc = serviceName(k);
    setBusy(true, testBtn, "Testar …");
    try {
      const { check } = await api("POST", `/api/admin/keys/${name}/test`);
      if (check.result === "ok") setNote("ok", "Nyckeln fungerar.");
      else if (check.result === "denied") setNote("err", `${svc} godkände inte nyckeln. Lägg in en ny.`);
      else if (check.result === "missing") setNote("warn", sentence(check.detail) || "Ingen nyckel är inlagd.");
      else setNote("warn", `Nyckeln kunde inte kontrolleras just nu. ${sentence(check.detail)}`);
    } catch (ex) {
      setNote("err", ex.message);
    } finally {
      setBusy(false, testBtn);
      paint();
      (testBtn.hidden ? input : testBtn).focus();
    }
  }

  async function remove() {
    if (busy) return;
    const svc = serviceName(k);
    const then = k.envSet ? "Då används nyckeln i serverns inställningsfil (.env) igen." : `Då har tjänsten ingen nyckel till ${svc}.`;
    if (!(await confirmDialog("Ta bort nyckeln?", `Nyckeln till ${svc} som sparats här tas bort. ${then}`, "Ta bort", true))) return;
    setBusy(true, delBtn, "Tar bort …");
    try {
      const res = await api("DELETE", `/api/admin/keys/${name}`);
      k = res.key;
      setNote("info", "Nyckeln är borttagen.");
      await afterChange();
    } catch (ex) {
      setNote("err", ex.message);
    } finally {
      setBusy(false, delBtn);
      paint();
      input.focus();
    }
  }

  paint();
  return card;
}

function keysSection(keys) {
  return [
    h("h2", { class: "section" }, "API-nycklar"),
    h(
      "p",
      { class: "keys-intro" },
      "Nycklarna behövs för att tjänsten ska kunna transkribera. De sparas krypterat på servern och visas aldrig igen, inte ens här.",
    ),
    keys.error
      ? h("div", { class: "error" }, `Nycklarna kunde inte hämtas: ${keys.error}`)
      : h("div", { class: "keys" }, keys.keys.map(keyCard)),
  ];
}

/** Varning på Ny inspelning när en tjänst saknar nyckel. */
function serviceWarning() {
  const f = state.me.features;
  const admin = state.me.user.role === "admin";
  const adminLink = (text) => h("a", { href: "#/admin" }, text);
  if (!f.live && !f.final) {
    return h(
      "div",
      { class: "banner warn" },
      h(
        "span",
        {},
        h("strong", {}, "Nycklarna till transkriberingen är inte inlagda. "),
        "Inspelningen sparas, men den kan inte transkriberas förrän nycklarna är inlagda. ",
        admin ? adminLink("Lägg in dem under Administration.") : "Be administratören lägga in dem.",
      ),
    );
  }
  if (!f.live) {
    return h(
      "div",
      { class: "banner warn" },
      h(
        "span",
        {},
        "Live-text är avstängd eftersom nyckeln till Deepgram saknas. Ljudet spelas in och texten skapas när du stoppar. ",
        admin ? adminLink("Lägg in nyckeln under Administration.") : "Be administratören lägga in nyckeln.",
      ),
    );
  }
  if (!f.final) {
    return h(
      "div",
      { class: "banner warn" },
      h(
        "span",
        {},
        "Nyckeln till Groq saknas, så slutversionen görs bara med Deepgram och blir mindre exakt. ",
        admin ? adminLink("Lägg in nyckeln under Administration.") : "Be administratören lägga in nyckeln.",
      ),
    );
  }
  return null;
}

/** Diskret rad överst på startsidan för administratören så länge en nyckel saknas. */
function getStartedRow() {
  const f = state.me.features;
  if (state.me.user.role !== "admin" || (f.live && f.final)) return null;
  const text =
    !f.live && !f.final
      ? "Kom igång: lägg in nycklarna till Deepgram och Groq under Administration."
      : !f.live
        ? "Kom igång: lägg in nyckeln till Deepgram under Administration för live-text och talare."
        : "Kom igång: lägg in nyckeln till Groq under Administration för en exaktare slutversion.";
  return h("a", { class: "getstarted", href: "#/admin" }, h("span", {}, text), h("span", { "aria-hidden": "true" }, "→"));
}

// ---------------------------------------------------------------- administration
async function renderAdmin() {
  if (state.me.user.role !== "admin") return go("#/");
  const [users, invites, usage, keys] = await Promise.all([
    api("GET", "/api/admin/users"),
    api("GET", "/api/admin/invites"),
    api("GET", "/api/admin/usage"),
    api("GET", "/api/admin/keys").catch((ex) => {
      if (ex.status === 401) throw ex;
      return { error: ex.message };
    }),
  ]);
  const inviteOut = h("div");
  const role = h("select", { style: "width:auto" }, h("option", { value: "user" }, "Användare"), h("option", { value: "admin" }, "Administratör"));
  const note = h("input", { type: "text", placeholder: "Anteckning, t.ex. vem den är till", maxlength: 200 });
  const engineName = { "deepgram-live": "Deepgram live", "deepgram-fil": "Deepgram talare", whisper: "Whisper" };

  const usageByMonth = {};
  for (const u of usage) (usageByMonth[u.month] ||= {})[u.engine] = u.seconds;

  mount(
    topbar(),
    h(
      "div",
      { class: "page" },
      h("h1", {}, "Administration"),
      keysSection(keys),
      h("h2", { class: "section" }, "Bjud in"),
      h(
        "div",
        { class: "card", style: "padding:16px" },
        h("div", { class: "row" }, role, h("div", { style: "flex:1;min-width:200px" }, note)),
        h(
          "button",
          {
            class: "btn primary",
            style: "margin-top:10px",
            onclick: async () => {
              try {
                const { url } = await api("POST", "/api/admin/invites", { role: role.value, note: note.value });
                const input = h("input", { type: "text", value: url, readonly: true });
                inviteOut.replaceChildren(
                  h("p", { class: "hint" }, "Skicka länken till personen. Den gäller i 7 dagar och kan bara användas en gång."),
                  h(
                    "div",
                    { class: "copy-box" },
                    input,
                    h(
                      "button",
                      {
                        class: "btn",
                        onclick: () => {
                          input.select();
                          navigator.clipboard?.writeText(url).then(() => toast("Kopierad"), () => document.execCommand("copy"));
                        },
                      },
                      "Kopiera",
                    ),
                  ),
                );
                note.value = "";
              } catch (ex) {
                toast(ex.message);
              }
            },
          },
          "Skapa inbjudningslänk",
        ),
        inviteOut,
      ),
      h("h2", { class: "section" }, "Konton"),
      h(
        "div",
        { class: "card table-wrap" },
        h(
          "table",
          {},
          h("thead", {}, h("tr", {}, h("th", {}, "Konto"), h("th", {}, "Roll"), h("th", {}, "Transkr."), h("th", {}, "Denna månad"), h("th", {}, ""))),
          h(
            "tbody",
            {},
            users.map((u) =>
              h(
                "tr",
                {},
                h("td", {}, h("div", {}, u.name || u.email), u.name ? h("div", { class: "hint" }, u.email) : null, u.disabled ? h("span", { class: "badge failed" }, "Avstängd") : null),
                h("td", {}, u.role === "admin" ? "Admin" : "Användare"),
                h("td", {}, u.transcripts),
                h("td", {}, `${fmtDuration(u.liveSec)} live · ${fmtDuration(u.whisperSec)} slutv.`),
                h(
                  "td",
                  {},
                  u.id === state.me.user.id
                    ? h("span", { class: "muted" }, "Du")
                    : h(
                        "div",
                        { class: "row" },
                        h(
                          "button",
                          {
                            class: "btn small",
                            onclick: async () => {
                              await api("PATCH", `/api/admin/users/${u.id}`, { disabled: !u.disabled }).catch((ex) => toast(ex.message));
                              renderAdmin();
                            },
                          },
                          u.disabled ? "Aktivera" : "Stäng av",
                        ),
                        h(
                          "button",
                          {
                            class: "btn small",
                            onclick: async () => {
                              await api("PATCH", `/api/admin/users/${u.id}`, { role: u.role === "admin" ? "user" : "admin" }).catch((ex) => toast(ex.message));
                              renderAdmin();
                            },
                          },
                          u.role === "admin" ? "Gör till användare" : "Gör till admin",
                        ),
                        h(
                          "button",
                          {
                            class: "btn small danger",
                            onclick: async () => {
                              if (!(await confirmDialog("Ta bort kontot?", `${u.email} och alla dess ${u.transcripts} transkriberingar raderas permanent.`, "Ta bort", true))) return;
                              await api("DELETE", `/api/admin/users/${u.id}`).catch((ex) => toast(ex.message));
                              renderAdmin();
                            },
                          },
                          "Ta bort",
                        ),
                      ),
                ),
              ),
            ),
          ),
        ),
      ),
      h("h2", { class: "section" }, "Inbjudningar"),
      invites.length
        ? h(
            "div",
            { class: "card table-wrap" },
            h(
              "table",
              {},
              h("thead", {}, h("tr", {}, h("th", {}, "Skapad"), h("th", {}, "Roll"), h("th", {}, "Anteckning"), h("th", {}, "Läge"), h("th", {}, ""))),
              h(
                "tbody",
                {},
                invites.map((i) => {
                  const expired = !i.usedAt && new Date(i.expiresAt) < new Date();
                  return h(
                    "tr",
                    {},
                    h("td", {}, fmtDate(i.createdAt)),
                    h("td", {}, i.role === "admin" ? "Admin" : "Användare"),
                    h("td", {}, i.note),
                    h("td", {}, i.usedAt ? `Använd av ${i.usedBy || "?"}` : expired ? "Utgången" : `Gäller till ${fmtDate(i.expiresAt)}`),
                    h(
                      "td",
                      {},
                      !i.usedAt
                        ? h(
                            "button",
                            {
                              class: "btn small",
                              onclick: async () => {
                                await api("DELETE", `/api/admin/invites/${i.id}`).catch((ex) => toast(ex.message));
                                renderAdmin();
                              },
                            },
                            "Återkalla",
                          )
                        : null,
                    ),
                  );
                }),
              ),
            ),
          )
        : h("p", { class: "muted" }, "Inga inbjudningar."),
      h("h2", { class: "section" }, "Förbrukning per månad"),
      h("p", { class: "hint" }, "Jämför med gratiskvoterna: Deepgram har en engångskredit, Groq har gränser per timme och dygn."),
      Object.keys(usageByMonth).length
        ? h(
            "div",
            { class: "card table-wrap" },
            h(
              "table",
              {},
              h("thead", {}, h("tr", {}, h("th", {}, "Månad"), Object.values(engineName).map((n) => h("th", {}, n)))),
              h(
                "tbody",
                {},
                Object.entries(usageByMonth).map(([m, e]) => h("tr", {}, h("td", {}, m), Object.keys(engineName).map((k) => h("td", {}, fmtDuration(e[k] || 0))))),
              ),
            ),
          )
        : h("p", { class: "muted" }, "Ingen förbrukning än."),
    ),
  );
}

// ---------------------------------------------------------------- routning
let lastHash = location.hash;
async function render() {
  const hash = location.hash || "#/";
  const prev = lastHash;
  lastHash = hash;
  clearSecrets();
  if (hash !== "#/admin") keyNotes = {};

  // Pågående inspelning: varna innan man lämnar inspelningsvyn.
  if (state.recorder && !state.recorder.stopped && !hash.startsWith("#/ny") && prev.startsWith("#/ny")) {
    const ok = await confirmDialog("Inspelningen pågår", "Vill du stoppa inspelningen och skapa slutversionen?", "Stoppa", true);
    if (!ok) {
      lastHash = prev;
      history.replaceState(null, "", prev);
      return;
    }
    state.recorder.stop();
    return; // onStopped navigerar till transkriberingen
  }

  const invite = /^#\/inbjudan\/([A-Za-z0-9_-]+)$/.exec(hash);
  if (invite) return renderInvite(invite[1]);
  if (!state.me) await loadMe();
  if (!state.me) return renderLogin();
  if (hash === "#/logga-in") return go("#/");

  let m;
  try {
    if (hash === "#/" || hash === "#") return await renderList();
    if (hash === "#/ny") return await renderNew();
    if ((m = /^#\/ny\/([A-Za-z0-9_-]+)$/.exec(hash))) return await renderNew(m[1]);
    if ((m = /^#\/t\/([A-Za-z0-9_-]+)$/.exec(hash))) return await renderTranscript(m[1]);
    if (hash === "#/installningar") return renderSettings();
    if (hash === "#/admin") return await renderAdmin();
    go("#/");
  } catch (ex) {
    if (ex instanceof ApiError && ex.status === 401) return;
    mount(topbar(), h("div", { class: "page" }, h("div", { class: "error" }, ex.message)));
  }
}

/** Tömmer nyckelfälten när man lämnar sidan, så att inget ligger kvar i webbläsaren. */
function clearSecrets() {
  for (const el of document.querySelectorAll("input[data-secret]")) el.value = "";
}

window.addEventListener("hashchange", render);
window.addEventListener("pagehide", clearSecrets);
render();
