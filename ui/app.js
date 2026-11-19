/* Midas Companion window. Data only via textContent (no HTML from data). */
(function () {
  "use strict";
  var TOKEN = document.querySelector('meta[name="midas-token"]').content;
  var LANG = document.documentElement.lang === "de" ? "de" : "en";

  var T = {
    de: {
      send: "Jetzt senden", sending: "Wird gesendet …",
      setup: "Einrichtung", setupOk: "Alles bereit", setupTodo: "{n} offen",
      wowOk: "World-of-Warcraft-Ordner gefunden", wowNo: "World-of-Warcraft-Ordner nicht gefunden",
      wowNoD: "Wähle den Ordner „World of Warcraft“ (der Ordner mit _classic_era_, _forever_ oder _anniversary_).",
      midasOk: "Midas 1.3.0 oder neuer gefunden", midasNo: "Midas 1.3.0 oder neuer fehlt",
      midasNoD: "Midas über CurseForge installieren oder aktualisieren.",
      midasV: "Midas {v}", midasOld: "Midas {v} – bitte auf 1.3.0 aktualisieren", midasNone: "Midas nicht installiert",
      shareOk: "Teilen im Spiel an", shareNo: "Teilen im Spiel aus",
      shareNoD: "Im Spiel {c} eingeben oder in Midas unter DATALINK „Share my scans“ einschalten. Die App sieht die Einstellung nach dem nächsten /reload oder Logout.",
      upOk: "Letzter Upload: {t}", upNo: "Noch kein Upload",
      upNoD: "Nach einem Midas-Scan und /reload sendet die App automatisch.",
      upCount: "{n} Scans gesendet",
      kindFull: "voller Scan", kindPrices: "Preis-Scan",
      upLastFull: "Letzter voller Scan: {t}", upLastPrices: "Letzter Preis-Scan: {t}",
      prOk: "Server-Preise geschrieben", prOkD: "Im Spiel /reload, damit Midas die Preise lädt.",
      prEmpty: "Preisdatei geschrieben, noch ohne Preise",
      prEmptyD: "Preise kommen, sobald für deinen Markt Scans vorliegen. Danach im Spiel /reload.",
      prNo: "Noch keine Server-Preise geschrieben", prNoD: "Benötigt Midas 1.3.0. Die App prüft alle 30 Minuten.",
      prOff: "Server-Preise ausgeschaltet", prOffD: "Unter Aktionen wieder einschalten.",
      prLast: "Letzte Abfrage: {t}",
      folderT: "Ordnerpfad eingeben oder auswählen:", folderSet: "Übernehmen", folderPick: "Ordner wählen …",
      folderBad: "In diesem Ordner wurde kein World of Warcraft gefunden.", folderOk: "Übernommen: {p}",
      markets: "Märkte", marketsSub: "aus Midas, dem Spielordner oder der Serverliste",
      srcSeen: "aus Midas", srcWtf: "aus Spielordner erkannt", srcManual: "von Hand hinzugefügt",
      srcList: "aus Serverliste (Forever)",
      fList: "{f}: {n} Märkte aus der Serverliste (Region {r})",
      fWtf: "{f}: {n} Realms aus dem Spielordner erkannt (Region {r}), beide Fraktionen",
      fNoRegion: "{f}: Region im Spielordner unbekannt (WTF\\Config.wtf) – Markt unten von Hand hinzufügen.",
      fNumbered: "{f}: Die Serverliste hat für Region {r} noch keinen Markt – Markt unten von Hand hinzufügen.",
      addT: "Markt fehlt? Spielversion, Region, Realm (Name genau wie im Spiel) und Fraktion wählen:",
      addB: "Markt hinzufügen", addRealm: "Realm-Name", addOk: "Hinzugefügt. Die App fragt die Server-Preise gleich ab.",
      addErr: { realm: "Realm-Name fehlt oder ist ungültig.", dup: "Dieser Markt ist schon eingetragen.",
                full: "Höchstens {n} Märkte von Hand.", other: "Eingabe prüfen." },
      facA: "Allianz", facH: "Horde", facB: "Beide Fraktionen", del: "Entfernen",
      thM: "Markt", thSeen: "Im Spiel", thUp: "Letzter Upload", thSrv: "Server-Preise",
      marketsEmpty: "Noch keine Märkte. Einmal im Spiel einloggen – oder unten einen Markt hinzufügen.",
      items: "{n} Items", none: "–",
      actions: "Aktionen", swPrices: "Server-Preise laden", swAuto: "Autostart beim Windows-Start",
      bLog: "Log öffnen", bData: "Datenordner öffnen", bPick: "WoW-Ordner wählen",
      fVer: "Version", fFolder: "WoW-Ordner", fSent: "Gesendet",
      log: "Live-Log", logSub: "aktualisiert sich automatisch",
      run: "Läuft", busy: "Arbeitet", offline: "App nicht erreichbar",
      infoK: "Über diese App", infoT: "Begleit-App zum World-of-Warcraft-Addon Midas.",
      infoL: "Midas Market ist ein Fanprojekt und nicht mit Blizzard Entertainment verbunden. World of Warcraft ist eine Marke von Blizzard Entertainment.",
      cidL: "Installations-Kennung:", cidCopy: "Kopieren", cidDone: "Kopiert",
      cidT: "Für Auskunft oder Löschung diese Kennung an support@midasmarket.app senden.",
      ago: { s: "gerade eben", m: "vor {n} Min.", h: "vor {n} Std.", d: "vor {n} Tagen", d1: "gestern" }
    },
    en: {
      send: "Send now", sending: "Sending …",
      setup: "Setup", setupOk: "All set", setupTodo: "{n} open",
      wowOk: "World of Warcraft folder found", wowNo: "World of Warcraft folder not found",
      wowNoD: "Choose the “World of Warcraft” folder (the one with _classic_era_, _forever_ or _anniversary_).",
      midasOk: "Midas 1.3.0 or newer found", midasNo: "Midas 1.3.0 or newer missing",
      midasNoD: "Install or update Midas from CurseForge.",
      midasV: "Midas {v}", midasOld: "Midas {v} – please update to 1.3.0", midasNone: "Midas not installed",
      shareOk: "Sharing is on in the game", shareNo: "Sharing is off in the game",
      shareNoD: "Type {c} in the game or turn on “Share my scans” under DATALINK in Midas. The app sees the setting after the next /reload or logout.",
      upOk: "Last upload: {t}", upNo: "No upload yet",
      upNoD: "After a Midas scan and /reload the app sends it automatically.",
      upCount: "{n} scans sent",
      kindFull: "full scan", kindPrices: "price scan",
      upLastFull: "Last full scan: {t}", upLastPrices: "Last price scan: {t}",
      prOk: "Server prices written", prOkD: "Type /reload in the game so Midas loads them.",
      prEmpty: "Price file written, no prices yet",
      prEmptyD: "Prices arrive as soon as there are scans for your market. Then /reload in the game.",
      prNo: "No server prices written yet", prNoD: "Needs Midas 1.3.0. The app checks every 30 minutes.",
      prOff: "Server prices are off", prOffD: "Turn them on again under Actions.",
      prLast: "Last check: {t}",
      folderT: "Enter or choose the folder:", folderSet: "Apply", folderPick: "Choose folder …",
      folderBad: "No World of Warcraft found in this folder.", folderOk: "Added: {p}",
      markets: "Markets", marketsSub: "from Midas, the game folder or the server list",
      srcSeen: "from Midas", srcWtf: "found in the game folder", srcManual: "added by hand",
      srcList: "from server list (Forever)",
      fList: "{f}: {n} markets from the server list (region {r})",
      fWtf: "{f}: {n} realms found in the game folder (region {r}), both factions",
      fNoRegion: "{f}: region unknown in the game folder (WTF\\Config.wtf) – add the market by hand below.",
      fNumbered: "{f}: the server list has no market for region {r} yet – add the market by hand below.",
      addT: "Market missing? Choose game version, region, realm (name exactly as in the game) and faction:",
      addB: "Add market", addRealm: "Realm name", addOk: "Added. The app asks for the server prices right away.",
      addErr: { realm: "Realm name missing or not valid.", dup: "This market is already listed.",
                full: "At most {n} markets by hand.", other: "Please check the input." },
      facA: "Alliance", facH: "Horde", facB: "Both factions", del: "Remove",
      thM: "Market", thSeen: "In game", thUp: "Last upload", thSrv: "Server prices",
      marketsEmpty: "No markets yet. Log in once in the game – or add a market below.",
      items: "{n} items", none: "–",
      actions: "Actions", swPrices: "Load server prices", swAuto: "Start with Windows",
      bLog: "Open log", bData: "Open data folder", bPick: "Choose WoW folder",
      fVer: "Version", fFolder: "WoW folder", fSent: "Sent",
      log: "Live log", logSub: "updates automatically",
      run: "Running", busy: "Working", offline: "App not reachable",
      infoK: "About this app", infoT: "Companion app for the World of Warcraft addon Midas.",
      infoL: "Midas Market is a fan project and is not affiliated with Blizzard Entertainment. World of Warcraft is a trademark of Blizzard Entertainment.",
      cidL: "Installation ID:", cidCopy: "Copy", cidDone: "Copied",
      cidT: "For access or deletion requests, send this ID to support@midasmarket.app.",
      ago: { s: "just now", m: "{n} min ago", h: "{n} h ago", d: "{n} days ago", d1: "yesterday" }
    }
  }[LANG];

  var CLIENT = { forever: "Forever", era: "Era", anniversary: "Anniversary", hardcore: "Hardcore" };

  function $(id) { return document.getElementById(id); }
  function fmt(s, o) { return s.replace(/\{(\w+)\}/g, function (_, k) { return o[k] == null ? "" : String(o[k]); }); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  var dtf = new Intl.DateTimeFormat(LANG === "de" ? "de-DE" : "en-GB",
    { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  var tmf = new Intl.DateTimeFormat(LANG === "de" ? "de-DE" : "en-GB",
    { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  function when(ts) {
    if (!ts) return T.none;
    var d = Date.now() / 1000 - ts, a = T.ago, rel;
    if (d < 90) rel = a.s;
    else if (d < 3600) rel = fmt(a.m, { n: Math.round(d / 60) });
    else if (d < 86400) rel = fmt(a.h, { n: Math.round(d / 3600) });
    else if (d < 2 * 86400) rel = a.d1;
    else rel = fmt(a.d, { n: Math.floor(d / 86400) });
    return rel + " · " + dtf.format(new Date(ts * 1000));
  }

  function api(path, body) {
    var opt = { credentials: "same-origin", cache: "no-store" };
    if (body !== undefined) {
      opt.method = "POST";
      opt.headers = { "Content-Type": "application/json", "X-Midas-Token": TOKEN };
      opt.body = JSON.stringify(body);
    }
    return fetch(path, opt).then(function (r) {
      if (!r.ok && r.status !== 500) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  // ---- static texts --------------------------------------------------------
  function texts() {
    $("b-send").textContent = T.send;
    $("h-setup").textContent = T.setup;
    $("folder-t").textContent = T.folderT;
    $("b-folder-set").textContent = T.folderSet;
    $("b-folder-pick").textContent = T.folderPick;
    $("h-markets").textContent = T.markets;
    $("markets-sub").textContent = T.marketsSub;
    $("th-m").textContent = T.thM; $("th-seen").textContent = T.thSeen;
    $("th-up").textContent = T.thUp; $("th-srv").textContent = T.thSrv;
    $("markets-empty").textContent = T.marketsEmpty;
    $("addm-t").textContent = T.addT; $("b-addm").textContent = T.addB;
    $("addm-realm").placeholder = T.addRealm;
    function opts(id, list) {
      var s = $(id);
      list.forEach(function (o) { var e = el("option", null, o[1]); e.value = o[0]; s.appendChild(e); });
    }
    opts("addm-flavor", [["_classic_era_", "Classic Era"], ["_anniversary_", "Anniversary"], ["_forever_", "Forever"]]);
    opts("addm-region", [["EU", "EU"], ["US", "US"], ["KR", "KR"], ["TW", "TW"], ["CN", "CN"]]);
    opts("addm-faction", [["Both", T.facB], ["Alliance", T.facA], ["Horde", T.facH]]);
    $("h-actions").textContent = T.actions;
    $("sw-prices-t").textContent = T.swPrices;
    $("sw-auto-t").textContent = T.swAuto;
    $("b-log").textContent = T.bLog; $("b-data").textContent = T.bData; $("b-pick2").textContent = T.bPick;
    $("h-log").textContent = T.log; $("log-sub").textContent = T.logSub;
    $("info-k").textContent = T.infoK; $("info-t").textContent = T.infoT; $("info-legal").textContent = T.infoL;
    $("cid-l").textContent = T.cidL; $("b-cid").textContent = T.cidCopy; $("cid-t").textContent = T.cidT;
  }

  // ---- checklist -------------------------------------------------------------
  function check(list, cls, title, lines) {
    var li = el("li", cls);
    li.appendChild(el("span", "ic", cls === "ok" ? "✓" : cls === "bad" ? "!" : cls === "off" ? "–" : "•"));
    var box = el("div");
    box.appendChild(el("div", "t", title));
    var d = el("div", "d");
    (lines || []).forEach(function (x) {
      if (x == null) return;
      if (typeof x === "string") d.appendChild(el("div", null, x));
      else d.appendChild(x);
    });
    box.appendChild(d);
    li.appendChild(box);
    list.appendChild(li);
    return cls === "ok" || cls === "off" ? 0 : 1;
  }
  function withCode(text, code) {
    var div = el("div"), parts = text.split("{c}");
    div.appendChild(document.createTextNode(parts[0]));
    div.appendChild(el("code", null, code));
    div.appendChild(document.createTextNode(parts[1] || ""));
    return div;
  }

  function renderChecks(s) {
    var ol = $("checks"), open = 0;
    ol.textContent = "";
    // 1. WoW folder
    if (s.wow.found) open += check(ol, "ok", T.wowOk, s.wow.roots.map(function (r) { return el("div", "sub", r); }));
    else open += check(ol, "bad", T.wowNo, [T.wowNoD]);
    $("folder").hidden = s.wow.found || s.only_wow;
    // 2. Midas >= 1.3.0 per game version
    var fl = s.flavors.filter(function (f) { return f.midas; });
    var lines = fl.map(function (f) {
      var t = !f.midas ? T.midasNone : f.midas_ok ? fmt(T.midasV, { v: f.midas }) : fmt(T.midasOld, { v: f.midas });
      return el("div", f.midas_ok ? "sub" : null, f.label + ": " + t);
    });
    if (!s.midas_ok) lines.push(T.midasNoD);
    if (s.wow.found) open += check(ol, s.midas_ok ? "ok" : "todo", s.midas_ok ? T.midasOk : T.midasNo, lines);
    // 3. sharing
    open += check(ol, s.share_on ? "ok" : "todo", s.share_on ? T.shareOk : T.shareNo,
      s.share_on ? [] : [withCode(T.shareNoD, "/midas datalink on")]);
    // 4. last upload
    if (s.last_upload) {
      var kind = s.last_kind === "prices" ? T.kindPrices : s.last_kind === "full" ? T.kindFull : null;
      var upLines = [fmt(T.upCount, { n: s.uploads })];
      // the other kind, when there was one: "last full scan ..." under a price scan and vice versa
      if (s.last_kind === "prices" && s.last_full) upLines.push(fmt(T.upLastFull, { t: when(s.last_full) }));
      if (s.last_kind === "full" && s.last_prices) upLines.push(fmt(T.upLastPrices, { t: when(s.last_prices) }));
      check(ol, "ok", fmt(T.upOk, { t: when(s.last_upload) + (kind ? " (" + kind + ")" : "") }), upLines);
    }
    else open += check(ol, "todo", T.upNo, [T.upNoD]);
    // 5. server prices
    var pl = s.prices_last ? fmt(T.prLast, { t: when(s.prices_last) }) : null;
    if (!s.prices_enabled) check(ol, "off", T.prOff, [T.prOffD]);
    else if (s.prices_written && s.prices_with_data) check(ol, "ok", T.prOk, [withCode(T.prOkD.replace("/reload", "{c}"), "/reload"), pl]);
    else if (s.prices_written) open += check(ol, "todo", T.prEmpty, [T.prEmptyD, pl].concat(foundLines(s)));
    else open += check(ol, "todo", T.prNo, [T.prNoD, pl].concat(foundLines(s)));
    $("setup-sub").textContent = open ? fmt(T.setupTodo, { n: open }) : T.setupOk;
  }

  // markets taken from the game folder (before the first logout with Midas)
  function foundLines(s) {
    var out = [];
    s.flavors.forEach(function (f) {
      var n = f.found;
      if (!n) return;
      var has = s.markets.some(function (m) { return m.flavor === f.flavor; });
      if (n.realms) out.push(el("div", "sub", fmt(T.fWtf, { f: f.label, n: n.realms, r: n.region })));
      if (n.listed) out.push(el("div", "sub", fmt(T.fList, { f: f.label, n: n.listed, r: n.region })));
      else if (n.no_region && !has) out.push(el("div", null, fmt(T.fNoRegion, { f: f.label })));
      else if (n.numbered && !n.realms && !has) out.push(el("div", null, fmt(T.fNumbered, { f: f.label, r: n.region })));
    });
    return out;
  }

  // ---- markets -----------------------------------------------------------------
  var manualSig = null;
  function renderMarkets(s) {
    var tb = $("markets").tBodies[0];
    tb.textContent = "";
    $("markets-empty").hidden = s.markets.length > 0;
    $("markets").hidden = s.markets.length === 0;
    s.markets.forEach(function (m) {
      var tr = el("tr");
      var c1 = el("td");
      c1.appendChild(el("div", "mk", m.realm || (CLIENT[m.client] || m.client) + " " + (m.ruleset || "")));
      var sub = el("div", "s");
      sub.appendChild(document.createTextNode((CLIENT[m.client] || m.client) + " · " + m.region +
        (m.ruleset ? " · " + m.ruleset : "") + " · "));
      sub.appendChild(el("span", "fa-" + String(m.faction).toLowerCase(), m.faction));
      c1.appendChild(sub);
      c1.appendChild(el("div", "src", m.src === "wtf" ? T.srcWtf : m.src === "manual" ? T.srcManual : m.src === "list" ? T.srcList : T.srcSeen));
      tr.appendChild(c1);
      tr.appendChild(el("td", null, when(m.src === "wtf" ? m.folder_at : m.last_seen)));
      var cu = el("td");
      cu.appendChild(el("div", null, when(m.upload_at)));
      if (m.upload_at && m.upload_kind) cu.appendChild(el("div", "s", m.upload_kind === "prices" ? T.kindPrices : T.kindFull));
      tr.appendChild(cu);
      var c4 = el("td");
      if (m.server_ts) {
        c4.appendChild(el("div", null, when(m.server_ts)));
        c4.appendChild(el("div", "s", fmt(T.items, { n: m.items.toLocaleString(LANG) })));
      } else c4.textContent = T.none;
      tr.appendChild(c4);
      tb.appendChild(tr);
    });
    $("b-addm").disabled = (s.manual || []).length >= (s.manual_max || 10);
    var sig = JSON.stringify(s.manual || []);
    if (sig === manualSig) return;                // keep the buttons while nothing changed
    manualSig = sig;
    var ul = $("manual");
    ul.textContent = "";
    (s.manual || []).forEach(function (m) {
      var li = el("li");
      li.appendChild(el("span", null, m.label + " · " + m.region + " · " + m.realm + " · " +
        (m.faction === "Both" ? T.facB : m.faction === "Horde" ? T.facH : T.facA)));
      var b = el("button", "btn sm", T.del);
      b.type = "button";
      b.addEventListener("click", function () { api("api/market_del", { id: m.id }).then(poll); });
      li.appendChild(b);
      ul.appendChild(li);
    });
  }

  function renderFacts(s) {
    var dl = $("facts");
    dl.textContent = "";
    [[T.fVer, s.app.name + " " + s.app.version], [T.fFolder, s.wow.roots.join("; ") || T.none],
     [T.fSent, String(s.uploads)]].forEach(function (p) {
      dl.appendChild(el("dt", null, p[0]));
      dl.appendChild(el("dd", null, p[1]));
    });
  }

  var lastStatus = null;
  function render(s) {
    lastStatus = s;
    $("ver").textContent = "v" + s.app.version;
    var st = $("state");
    st.className = "pill" + (s.wow.found ? "" : " warn");
    $("state-t").textContent = s.wow.found ? T.run : T.wowNo;
    st.title = s.status || "";
    renderChecks(s);
    renderMarkets(s);
    renderFacts(s);
    if (document.activeElement !== $("sw-prices")) $("sw-prices").checked = !!s.prices_enabled;
    if (document.activeElement !== $("sw-auto")) $("sw-auto").checked = !!s.autostart;
    $("b-pick2").hidden = !!s.only_wow;
    if ($("cid").textContent !== (s.client_id || "")) $("cid").textContent = s.client_id || "";
  }

  function poll() {
    api("api/status").then(render).catch(function () {
      $("state").className = "pill err";
      $("state-t").textContent = T.offline;
    });
  }

  // ---- live log ----------------------------------------------------------------
  var logAfter = 0;
  function pollLog() {
    api("api/log?after=" + logAfter).then(function (r) {
      if (r.last < logAfter) logAfter = 0;               // app restarted
      var box = $("log"), atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
      r.lines.forEach(function (x) {
        var d = el("div", /fail|error|cannot|retry|redirect/i.test(x.m) ? "e" : /^sent |wrote /.test(x.m) ? "g" : null);
        d.appendChild(el("span", "tm", tmf.format(new Date(x.t * 1000))));
        d.appendChild(document.createTextNode(x.m));
        box.appendChild(d);
        logAfter = Math.max(logAfter, x.n);
      });
      while (box.childNodes.length > 400) box.removeChild(box.firstChild);
      if (atEnd && r.lines.length) box.scrollTop = box.scrollHeight;
    }).catch(function () {});
  }

  // ---- actions ------------------------------------------------------------------
  function folderMsg(r) {
    var m = $("folder-msg");
    if (r.ok) { m.className = "msg"; m.textContent = fmt(T.folderOk, { p: r.root }); setTimeout(poll, 1500); }
    else if (r.error === "no_wow") { m.className = "msg err"; m.textContent = T.folderBad; }
    else { m.className = "msg"; m.textContent = ""; }
    if (r.ok || r.error === "no_wow") $("folder").hidden = false;
  }
  $("b-send").addEventListener("click", function () {
    var b = this;
    b.disabled = true; b.textContent = T.sending;
    api("api/send", {}).finally(function () {
      setTimeout(function () { b.disabled = false; b.textContent = T.send; poll(); }, 2500);
    });
  });
  $("sw-prices").addEventListener("change", function () { api("api/prices", { on: this.checked }).then(poll); });
  $("sw-auto").addEventListener("change", function () { api("api/autostart", { on: this.checked }).then(poll); });
  $("b-log").addEventListener("click", function () { api("api/open", { what: "log" }); });
  $("b-data").addEventListener("click", function () { api("api/open", { what: "folder" }); });
  $("l-cf").addEventListener("click", function () { api("api/open", { what: "cf" }); });
  $("l-web").addEventListener("click", function () { api("api/open", { what: "web" }); });
  $("b-cid").addEventListener("click", function () {
    var b = this, id = $("cid").textContent;
    if (!id) return;
    function done() { b.textContent = T.cidDone; setTimeout(function () { b.textContent = T.cidCopy; }, 1500); }
    function fallback() {                      // select the ID; copy via execCommand if allowed
      var r = document.createRange(); r.selectNodeContents($("cid"));
      var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
      try { if (document.execCommand("copy")) done(); } catch (e) { /* stays selected */ }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(id).then(done, fallback);
    else fallback();
  });
  function pick() { api("api/pick", {}).then(folderMsg); }
  $("b-folder-pick").addEventListener("click", pick);
  $("b-pick2").addEventListener("click", pick);
  $("b-folder-set").addEventListener("click", function () {
    api("api/folder", { path: $("folder-in").value.trim() }).then(folderMsg);
  });
  $("b-addm").addEventListener("click", function () {
    var msg = $("addm-msg");
    api("api/market_add", { flavor: $("addm-flavor").value, region: $("addm-region").value,
                            realm: $("addm-realm").value.trim(), faction: $("addm-faction").value })
      .then(function (r) {
        if (r.ok) { msg.className = "msg"; msg.textContent = T.addOk; $("addm-realm").value = ""; }
        else {
          msg.className = "msg err";
          msg.textContent = fmt(T.addErr[r.error] || T.addErr.other, { n: lastStatus ? lastStatus.manual_max : 10 });
        }
        poll();
      });
  });
  $("addm-realm").addEventListener("keydown", function (e) { if (e.key === "Enter") $("b-addm").click(); });
  $("folder-in").addEventListener("keydown", function (e) { if (e.key === "Enter") $("b-folder-set").click(); });

  texts();
  poll(); pollLog();
  setInterval(poll, 3000);
  setInterval(pollLog, 1500);
})();
