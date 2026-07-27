/* Copy Trade Engine — Dashboard v2 */

(function () {
  'use strict';

  var $ = function(id) { return document.getElementById(id); };
  var qa = function(s, c) { return (c || document).querySelectorAll(s); };
  var q = function(s, c) { return (c || document).querySelector(s); };

  var T = function(n) {
    if (n == null || n === '') return '\u2014';
    var num = +n; if (isNaN(num)) return '\u2014';
    return num.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
  };
  var fmt = function(n, d) {
    if (n == null || n === '') return '\u2014';
    var num = +n; if (isNaN(num)) return '\u2014';
    if (Math.abs(num) >= 1e6) return (num/1e6).toFixed(d||2)+'M';
    if (Math.abs(num) >= 1e3) return (num/1e3).toFixed(d||1)+'K';
    return num.toFixed(d||2);
  };
  var pnlCls = function(v) { return v > 0 ? 'positive' : (v < 0 ? 'negative' : ''); };
  var pnlSign = function(v) { return v > 0 ? '+' : ''; };
  var fmtTime = function(ts) { return ts ? new Date(ts*1000).toLocaleTimeString() : '\u2014'; };
  var fmtDur = function(s) { return s > 0 ? (s > 3600 ? (s/3600).toFixed(1)+'h' : (s > 60 ? Math.floor(s/60)+'m' : Math.floor(s)+'s')) : '0s'; };
  var currSym = function(c) { return ({USD:'$', USC:'\u20B5', EUR:'\u20AC', GBP:'\u00A3', JPY:'\u00A5', AUD:'A$', CAD:'C$', CHF:'Fr', NZD:'NZ$'}[c]||c+' '); };

  // ── Toast ──────────────────────────────────────────────
  var tt;
  function toast(msg, type) {
    var el = $('toast'); el.textContent = msg; el.className = 'toast '+(type||'success');
    clearTimeout(tt); void el.offsetWidth; el.classList.add('show');
    tt = setTimeout(function() { el.classList.remove('show'); }, 3000);
  }

  // ── Tabs ───────────────────────────────────────────────
  qa('.nav-item').forEach(function(item) {
    item.addEventListener('click', function() {
      qa('.nav-item').forEach(function(t) { t.classList.remove('active'); });
      qa('.tab-content').forEach(function(tc) { tc.classList.remove('active'); });
      item.classList.add('active');
      $('tab-'+item.dataset.tab).classList.add('active');
      if (item.dataset.tab === 'agents') loadAgents();
      if (item.dataset.tab === 'accounts') loadAccounts();
      if (item.dataset.tab === 'activity') loadActivity();
      if (item.dataset.tab === 'settings') loadConfig();
    });
  });

  // ── Event Delegation ──────────────────────────────────
  document.addEventListener('click', function(e) {
    var toggle = e.target ? e.target.closest('[data-toggle]') : null;
    if (!toggle) return;

    var action = toggle.getAttribute('data-toggle');
    var card = toggle.closest('.agent-card');
    var name = card ? card.getAttribute('data-acc') : toggle.getAttribute('data-fname') || '';

    if (action === 'positions') {
      var safe = name.replace(/[^a-zA-Z0-9_-]/g, '_');
      var panel = document.getElementById('pos-'+safe);
      if (panel) {
        panel.classList.toggle('open');
        var icon = toggle.querySelector('.expand-icon');
        if (icon) icon.textContent = panel.classList.contains('open') ? '▾' : '▸';
      }
    } else if (action === 'ping') {
      pingAgent(name);
    } else if (action === 'edit-follower') {
      editFollower(name);
    } else if (action === 'download-agent') {
      downloadAgent(name);
    } else if (action === 'delete-follower') {
      deleteFollower(name);
    }
  });

  // ── Data Stream ────────────────────────────────────────
  var usePoll = false, latestData = null, latestPortfolio = null;

  function connectSSE() {
    var src = new EventSource('/api/stream');
    src.onmessage = function(e) {
      try {
        var d = JSON.parse(e.data);
        latestData = d; latestPortfolio = d.portfolio;
        renderDashboard(d);
        if ($('tab-agents').classList.contains('active')) loadAgents();
        if ($('tab-accounts').classList.contains('active')) loadAccounts();
      } catch(_) {}
    };
    src.onerror = function() { src.close(); usePoll = true; };
  }
  function startPoll() {
    setInterval(async function() {
      try { var d = await(await fetch('/api/status')).json(); latestData = d; latestPortfolio = d.portfolio; renderDashboard(d); } catch(_) {}
    }, 2000);
  }
  connectSSE();
  setTimeout(function() { if (usePoll) startPoll(); }, 5000);

  // ── Equity Chart ───────────────────────────────────────
  var chartData = [];
  async function loadChart() {
    try { var r = await fetch('/api/equity-history?limit=200'); var d = await r.json(); chartData = d.points || []; drawChart(); } catch(_) {}
  }
  setInterval(async function() {
    try { var r = await fetch('/api/equity-history?limit=200'); var d = await r.json(); chartData = d.points || []; drawChart(); } catch(_) {}
  }, 30000);
  setTimeout(loadChart, 1000);

  function drawChart() {
    var canvas = $('equity-chart');
    if (!canvas || !chartData.length) return;
    var ctx = canvas.getContext('2d');
    var rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width - 40; canvas.height = 220;

    var W = canvas.width, H = canvas.height;
    var pts = chartData.slice(-120);
    if (pts.length < 2) { ctx.clearRect(0,0,W,H); return; }

    var values = pts.map(function(p) { return p.e; });
    var min = Math.min.apply(null, values), max = Math.max.apply(null, values);
    var range = max - min || 1;
    var pad = 20;
    var xStep = (W - pad*2) / (pts.length - 1);

    ctx.clearRect(0,0,W,H);
    ctx.strokeStyle = 'rgba(30,34,53,0.6)'; ctx.lineWidth = 1;
    for (var i = 0; i < 4; i++) {
      var y = pad + (H - pad*2) * i / 3;
      ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(W-pad, y); ctx.stroke();
    }

    ctx.beginPath();
    pts.forEach(function(p, i) {
      var y = pad + (H - pad*2) * (1 - (p.e - min)/range);
      if (i === 0) ctx.moveTo(pad, y); else ctx.lineTo(pad + i*xStep, y);
    });
    ctx.lineTo(pad + (pts.length-1)*xStep, H - pad);
    ctx.lineTo(pad, H - pad);
    ctx.closePath();
    var grad = ctx.createLinearGradient(0,pad,0,H-pad);
    grad.addColorStop(0,'rgba(99,102,241,0.25)'); grad.addColorStop(1,'rgba(99,102,241,0.01)');
    ctx.fillStyle = grad; ctx.fill();

    ctx.beginPath();
    pts.forEach(function(p, i) {
      var y = pad + (H - pad*2) * (1 - (p.e - min)/range);
      if (i === 0) ctx.moveTo(pad, y); else ctx.lineTo(pad + i*xStep, y);
    });
    ctx.strokeStyle = '#6366f1'; ctx.lineWidth = 2.5; ctx.stroke();

    ctx.shadowColor = 'rgba(99,102,241,0.3)'; ctx.shadowBlur = 8;
    ctx.strokeStyle = 'rgba(99,102,241,0.15)'; ctx.lineWidth = 6;
    ctx.beginPath();
    pts.forEach(function(p, i) {
      var y = pad + (H - pad*2) * (1 - (p.e - min)/range);
      if (i === 0) ctx.moveTo(pad, y); else ctx.lineTo(pad + i*xStep, y);
    });
    ctx.stroke();
    ctx.shadowBlur = 0;

    ctx.fillStyle = '#5a5f7a'; ctx.font = '11px -apple-system,sans-serif';
    ctx.fillText(fmt(min), 4, H-6);
    ctx.fillText(fmt(max), 4, 14);
    var first = new Date(pts[0].t*1000), last = new Date(pts[pts.length-1].t*1000);
    ctx.textAlign = 'left'; ctx.fillText(first.toLocaleTimeString(), pad, H-4);
    ctx.textAlign = 'right'; ctx.fillText(last.toLocaleTimeString(), W-pad, H-4);
  }

  // ── Position Table Builder ─────────────────────────────
  function buildPositionTable(positions, currency) {
    if (!positions || !positions.length) return '';
    var sym = currSym(currency);
    var h = '<table class="pos-table"><thead><tr>';
    h += '<th>Symbol</th><th>Type</th><th>Vol</th><th>Open</th><th>Current</th><th>SL</th><th>TP</th><th>Profit</th><th>Duration</th>';
    h += '</tr></thead><tbody>';
    for (var i = 0; i < positions.length; i++) {
      var p = positions[i];
      var profit = p.profit || p.floating_profit || 0;
      var openTime = p.time || p.open_time || 0;
      var dur = openTime ? (Date.now()/1000 - openTime) : 0;
      var typeStr = '\u2014';
      if (p.type === 0) typeStr = 'BUY';
      else if (p.type === 1) typeStr = 'SELL';
      else if (p.type === 2) typeStr = 'BUY LIMIT';
      else if (p.type === 3) typeStr = 'SELL LIMIT';
      else if (p.type === 4) typeStr = 'BUY STOP';
      else if (p.type === 5) typeStr = 'SELL STOP';
      var vol = p.volume || p.lots || 0;
      var typeCls = typeStr.toLowerCase().replace(/ /g,'-');
      h += '<tr>';
      h += '<td><strong>'+(p.symbol || '\u2014')+'</strong></td>';
      h += '<td class="type-'+typeCls+'">'+typeStr+'</td>';
      h += '<td>'+vol+'</td>';
      h += '<td>'+(p.price_open || p.open_price || '\u2014')+'</td>';
      h += '<td>'+(p.price_current || p.current_price || '\u2014')+'</td>';
      h += '<td>'+(p.sl || p.stop_loss || '\u2014')+'</td>';
      h += '<td>'+(p.tp || p.take_profit || '\u2014')+'</td>';
      h += '<td class="'+pnlCls(profit)+'">'+pnlSign(profit)+T(profit)+' '+sym+'</td>';
      h += '<td>'+fmtDur(dur)+'</td>';
      h += '</tr>';
    }
    h += '</tbody></table>';
    return h;
  }

  // ── Account Detail Builder ──────────────────────────────
  function buildAccountCard(acc, isMaster) {
    var sym = currSym(acc.currency || 'USD');
    var posCount = acc.position_count || (acc.positions || []).length;
    var pnl = acc.unrealized_pnl || 0;
    var ml = acc.margin_level ? acc.margin_level.toFixed(1)+'%' : '\u2014';
    var posHtml = acc.positions && acc.positions.length ? buildPositionTable(acc.positions, acc.currency) : '';
    var safeId = acc.name.replace(/[^a-zA-Z0-9_-]/g, '_');

    var h = '<div class="agent-card" data-acc="'+acc.name.replace(/"/g,'&quot;')+'">';
    h += '<div class="agent-main" data-toggle="positions">';
    h += '<div class="agent-left">';
    h += '<span class="dot '+(acc.connected?'green':'red')+'"></span>';
    h += '<div><strong>'+escHtml(acc.name)+'</strong>';
    h += '<span class="agent-sub">'+(isMaster?'MASTER':'FOLLOWER');
    if (acc.server) h += ' &middot; '+escHtml(acc.server);
    h += ' &middot; '+(acc.account_login||'')+'</span></div></div>';
    h += '<div class="agent-metrics">';
    h += '<div class="metric"><div class="mv">'+sym+fmt(acc.balance)+'</div><div class="ml">Balance</div></div>';
    h += '<div class="metric"><div class="mv">'+sym+fmt(acc.equity)+'</div><div class="ml">Equity</div></div>';
    h += '<div class="metric"><div class="mv '+pnlCls(pnl)+'">'+pnlSign(pnl)+fmt(pnl)+'</div><div class="ml">PnL</div></div>';
    h += '<div class="metric"><div class="mv">'+ml+'</div><div class="ml">Margin</div></div>';
    h += '<div class="metric"><div class="mv">'+posCount+'</div><div class="ml">Positions <span class="expand-icon">'+(posHtml?'\u25B8':'')+'</span></div></div>';
    if (!isMaster) {
      var latCls = acc.latency_ms < 100 ? 'ok' : (acc.latency_ms < 300 ? 'warn' : 'bad');
      h += '<div class="metric"><div class="mv latency-'+latCls+'">'+(acc.latency_ms||'\u2014')+'ms</div>'+
           '<div class="ml">Latency</div></div>';
      h += '<div class="metric"><button class="btn btn-sm" data-toggle="ping">Ping</button></div>';
    }
    h += '</div></div>';
    if (posHtml) {
      h += '<div class="positions-panel" id="pos-'+safeId+'">'+posHtml+'</div>';
    }
    h += '</div>';
    return h;
  }

  function escHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ── Dashboard Render ───────────────────────────────────
  var _firstRender = true;
  function renderDashboard(data) {
    if (!data) return;
    var accounts = data.accounts, portfolio = data.portfolio, stats = data.stats;
    if (!accounts) return;

    var accNames = {};
    for (var i = 0; i < accounts.length; i++) { accNames[accounts[i].name] = accounts[i]; }

    // ── Update sidebar ──
    $('sidebar-status').textContent = stats.master_connected ? (portfolio.connected_agents+' agents') : 'offline';
    $('sidebar-dot').className = 'dot ' + (stats.master_connected ? 'green' : 'red');
    $('dashboard-sub').textContent = portfolio.connected_agents + ' agents ' + fmt(stats.cycles,0) + ' cycles ' + fmtDur(stats.uptime);

    // ── Update portfolio grid ──
    var pfHtml =
      '<div class="pf-card" data-pf="balance"><div class="lbl">Total Balance</div><div class="val accent">$'+fmt(portfolio.total_balance)+'</div><div class="sub">all accounts</div><div class="mini-bar" style="width:100%"></div></div>'+
      '<div class="pf-card" data-pf="equity"><div class="lbl">Total Equity</div><div class="val">$'+fmt(portfolio.total_equity)+'</div><div class="sub">with floating PnL</div><div class="mini-bar" style="width:'+Math.min(100,portfolio.total_margin_free/portfolio.total_equity*100||0)+'%;background:var(--green)"></div></div>'+
      '<div class="pf-card" data-pf="pnl"><div class="lbl">Floating PnL</div><div class="val '+pnlCls(portfolio.total_floating_pnl)+'">'+pnlSign(portfolio.total_floating_pnl)+'$'+fmt(portfolio.total_floating_pnl)+'</div><div class="sub">unrealized</div><div class="mini-bar" style="width:50%;background:'+(portfolio.total_floating_pnl>=0?'var(--green)':'var(--red)')+'"></div></div>'+
      '<div class="pf-card" data-pf="margin"><div class="lbl">Free Margin</div><div class="val">$'+fmt(portfolio.total_margin_free)+'</div><div class="sub">equity - margin</div><div class="mini-bar" style="width:'+Math.min(100,portfolio.total_margin_free/(portfolio.total_equity||1)*100)+'%;background:var(--yellow)"></div></div>'+
      '<div class="pf-card" data-pf="positions"><div class="lbl">Total Positions</div><div class="val">'+portfolio.total_positions+'</div><div class="sub">'+portfolio.connected_agents+' agents connected</div><div class="mini-bar" style="width:'+Math.min(100,portfolio.total_positions*5)+'%;background:var(--accent)"></div></div>';

    // Value-only update for portfolio
    var curPf = $('portfolio-grid');
    if (_firstRender || curPf.children.length === 0) {
      curPf.innerHTML = pfHtml;
    } else {
      setVal(curPf, 'balance', '$'+fmt(portfolio.total_balance));
      setVal(curPf, 'equity', '$'+fmt(portfolio.total_equity));
      setVal(curPf, 'pnl', pnlSign(portfolio.total_floating_pnl)+'$'+fmt(portfolio.total_floating_pnl));
      setVal(curPf, 'margin', '$'+fmt(portfolio.total_margin_free));
      setVal(curPf, 'positions', portfolio.total_positions);
    }

    // ── Update stats row (value-only after first render) ──
    var statsHtml =
      '<div class="stat-card" data-stat="uptime"><div class="lbl">Uptime</div><div class="val">'+fmtDur(stats.uptime)+'</div></div>'+
      '<div class="stat-card" data-stat="cycles"><div class="lbl">Cycles</div><div class="val">'+fmt(stats.cycles,0)+'</div></div>'+
      '<div class="stat-card" data-stat="events"><div class="lbl">Events</div><div class="val">'+fmt(stats.events_detected,0)+'</div></div>'+
      '<div class="stat-card" data-stat="tickets"><div class="lbl">Tickets</div><div class="val">'+fmt(stats.known_tickets,0)+'</div></div>'+
      '<div class="stat-card" data-stat="errors"><div class="lbl">Errors</div><div class="val" style="color:'+(stats.errors>0?'var(--red)':'')+'">'+stats.errors+'</div></div>';
    var curStats = $('stats-row');
    if (_firstRender || curStats.children.length === 0) {
      curStats.innerHTML = statsHtml;
    } else {
      setStat(curStats, 'uptime', fmtDur(stats.uptime));
      setStat(curStats, 'cycles', fmt(stats.cycles,0));
      setStat(curStats, 'events', fmt(stats.events_detected,0));
      setStat(curStats, 'tickets', fmt(stats.known_tickets,0));
      setStat(curStats, 'errors', stats.errors);
    }

    // ── Account summary ──
    var totalPos = 0;
    for (var i = 0; i < accounts.length; i++) { totalPos += (accounts[i].position_count || 0); }
    $('acc-summary').textContent = totalPos+' positions across '+accounts.length+' accounts';

    // ── Account cards (rebuild but preserve expanded state) ──
    var expandedAccounts = {};
    var existingCards = $('account-list').children;
    for (var i = 0; i < existingCards.length; i++) {
      var name = existingCards[i].getAttribute('data-acc');
      if (name) {
        var panel = existingCards[i].querySelector('.positions-panel');
        if (panel && panel.classList.contains('open')) {
          expandedAccounts[name] = true;
        }
      }
    }

    var accHtml = '';
    for (var i = 0; i < accounts.length; i++) {
      accHtml += buildAccountCard(accounts[i], accounts[i].type === 'master');
    }
    $('account-list').innerHTML = accHtml;

    // Restore expanded state after rebuild
    for (var i = 0; i < $('account-list').children.length; i++) {
      var card = $('account-list').children[i];
      var name = card.getAttribute('data-acc');
      if (name && expandedAccounts[name]) {
        var panel = card.querySelector('.positions-panel');
        var toggle = card.querySelector('[data-toggle="positions"]');
        if (panel && toggle) {
          panel.classList.add('open');
          var icon = toggle.querySelector('.expand-icon');
          if (icon) icon.textContent = '\u25BE';
        }
      }
    }

    _firstRender = false;
  }

  function setVal(container, key, val) {
    var el = container.querySelector('[data-pf="'+key+'"] .val');
    if (el) el.textContent = val;
  }
  function setStat(container, key, val) {
    var el = container.querySelector('[data-stat="'+key+'"] .val');
    if (el) el.textContent = val;
  }

  // ── Agents Tab ─────────────────────────────────────────
  function loadAgents() {
    var grid = $('agent-grid');
    if (!latestData) { grid.innerHTML = '<p style="color:var(--text-dim);text-align:center;padding:40px 0">Waiting for data...</p>'; return; }
    var accounts = latestData.accounts;

    // Preserve expanded state
    var expanded = {};
    for (var i = 0; i < grid.children.length; i++) {
      var c = grid.children[i];
      var name = c.getAttribute('data-acc');
      if (name) {
        var panel = c.querySelector('.positions-panel');
        if (panel && panel.classList.contains("open")) {
          expanded[name] = true;
        }
      }
    }

    var html = '';
    for (var i = 0; i < accounts.length; i++) {
      html += buildAccountCard(accounts[i], accounts[i].type === 'master');
    }
    grid.innerHTML = html;
    $('agent-count').textContent = '(' + accounts.length + ' total)';

    // Restore expanded state
    for (var i = 0; i < grid.children.length; i++) {
      var c = grid.children[i];
      var name = c.getAttribute('data-acc');
      if (name && expanded[name]) {
        var panel = c.querySelector('.positions-panel');
        var toggle = c.querySelector('[data-toggle="positions"]');
        if (panel && toggle) {
          panel.classList.add("open");
          var icon = toggle.querySelector('.expand-icon');
          if (icon) icon.textContent = '\u25BE';
        }
      }
    }
  }

  async function pingAgent(name) {
    try {
      var r = await(await fetch('/api/agents/'+encodeURIComponent(name)+'/ping',{method:'POST'})).json();
      toast(name+': '+r.latency_ms+'ms');
    } catch(e) { toast('Ping failed','error'); }
  }

  // ── Activity Tab ───────────────────────────────────────
  var activityFilter = '';

  window.filterActivity = function(btn) {
    qa('.active-filter').forEach(function(b) { b.classList.remove('active-filter'); });
    btn.classList.add('active-filter');
    activityFilter = btn.dataset.filter;
    loadActivity();
  };

  async function loadAccounts() {
    try {
      var r = await (await fetch('/api/status')).json();
      if (!r || !r.accounts) return;
      var accounts = r.accounts;
      var portfolio = r.portfolio || {};
      $('acc-page-count').textContent = '\u2014 '+accounts.length+' account'+(accounts.length!==1?'s':'')+', '+portfolio.total_positions+' positions';

      var tbody = $('acc-table-body');
      var existingRows = {};
      for (var i = 0; i < tbody.rows.length; i++) {
        var r = tbody.rows[i];
        if (r.getAttribute('data-acc')) existingRows[r.getAttribute('data-acc')] = r;
      }

      var expandedDetail = {};
      for (var i = 0; i < tbody.rows.length; i++) {
        var r = tbody.rows[i];
        if (r.id && r.id.indexOf('acc-detail-') === 0 && r.style.display !== 'none') {
          expandedDetail[r.getAttribute('data-acc')] = true;
        }
      }

      var h = '';
      for (var i = 0; i < accounts.length; i++) {
        var a = accounts[i];
        var sym = currSym(a.currency || 'USD');
        var typeLabel = a.type === 'master' ? 'MASTER' : 'FOLLOWER';
        var typeCls = a.type === 'master' ? 'master' : '';
        var pnlCl = (a.unrealized_pnl||0) >= 0 ? 'positive' : 'negative';
        var safeName = a.name.replace(/[^a-zA-Z0-9_-]/g, '_');
        var wasExpanded = expandedDetail[a.name];

        h += '<tr data-idx="'+i+'" data-acc="'+escHtml(a.name)+'">';
        h += '<td><span class="acc-type-badge '+typeCls+'">'+typeLabel+'</span></td>';
        h += '<td class="acc-name">'+(a.name||'\u2014')+'</td>';
        h += '<td>'+(a.server||'\u2014')+'</td>';
        h += '<td>'+(a.login || a.account_login || '\u2014')+'</td>';
        h += '<td data-cel="bal">'+sym+fmt(a.balance||0)+'</td>';
        h += '<td data-cel="eq">'+sym+fmt(a.equity||0)+'</td>';
        h += '<td data-cel="pnl" class="'+pnlCl+'">'+sym+fmt(a.unrealized_pnl||0)+'</td>';
        h += '<td data-cel="mar">'+sym+fmt(a.margin||0)+'</td>';
        h += '<td data-cel="fm">'+sym+fmt(a.margin_free||0)+'</td>';
        h += '<td data-cel="ml">'+(a.margin_level ? fmt(a.margin_level,1)+'%' : '\u2014')+'</td>';
        h += '<td data-cel="lev">'+(a.leverage ? '1:'+a.leverage : '\u2014')+'</td>';
        h += '<td data-cel="posc">'+(a.position_count||0)+'</td>';
        h += '<td data-cel="lat">'+(a.latency_ms != null ? fmt(a.latency_ms,0)+'ms' : '\u2014')+'</td>';
        h += '<td><span class="btn-icon toggle-acc-positions" data-idx="'+i+'" title="Toggle positions">\u25B8</span></td>';
        h += '</tr>';

        h += '<tr class="acc-detail-row" id="acc-detail-'+i+'" style="display:'+(wasExpanded?'table-row':'none')+'" data-acc="'+escHtml(a.name)+'">';
        h += '<td colspan="14" style="padding:0">';
        h += await buildAccDetail(a, i, sym);
        h += '</td></tr>';
      }
      tbody.innerHTML = h;
    } catch(e) { console.error('loadAccounts:',e); }
  }

  async function buildAccDetail(acc, idx, sym) {
    var pnl = acc.unrealized_pnl || 0;
    var ml = acc.margin_level ? acc.margin_level.toFixed(1)+'%' : '\u2014';
    var posHtml = acc.positions && acc.positions.length ? buildPositionTable(acc.positions, acc.currency) : '<div class="no-pos">No open positions</div>';
    var connected = acc.connected !== false;
    var dur = acc.uptime ? fmtDur(acc.uptime) : '\u2014';

    var h = '<div class="acc-detail">';
    h += '<div class="ad-header">';
    h += '<div><span class="dot '+(connected?'green':'red')+'"></span> <strong>'+escHtml(acc.name)+'</strong> &middot; '+(acc.server||'\u2014')+' &middot; Login '+(acc.login||acc.account_login||'\u2014')+'</div>';
    h += '<span style="font-size:12px;color:var(--text-dim)">Currency: '+(sym||acc.currency||'USD')+' | Leverage: '+(acc.leverage ? '1:'+acc.leverage : '\u2014')+' | Uptime: '+dur+'</span>';
    h += '</div>';
    h += '<div class="ad-metrics">';
    h += '<div class="adm"><div class="l">Balance</div><div class="v">'+sym+fmt(acc.balance||0)+'</div></div>';
    h += '<div class="adm"><div class="l">Equity</div><div class="v">'+sym+fmt(acc.equity||0)+'</div></div>';
    h += '<div class="adm"><div class="l">Floating PnL</div><div class="v '+pnlCls(pnl)+'">'+sym+fmt(pnl)+'</div></div>';
    h += '<div class="adm"><div class="l">Margin</div><div class="v">'+sym+fmt(acc.margin||0)+'</div></div>';
    h += '<div class="adm"><div class="l">Free Margin</div><div class="v">'+sym+fmt(acc.margin_free||0)+'</div></div>';
    h += '<div class="adm"><div class="l">Margin Level</div><div class="v">'+ml+'</div></div>';
    h += '<div class="adm"><div class="l">Latency</div><div class="v">'+(acc.latency_ms != null ? fmt(acc.latency_ms,0)+'ms' : '\u2014')+'</div></div>';
    h += '</div>';
    h += '<div class="ad-table-wrap">'+posHtml+'</div>';
    h += '</div>';
    return h;
  }

  // Table click delegation for accounts tab
  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.toggle-acc-positions');
    if (!btn) return;
    var idx = parseInt(btn.dataset.idx);
    var detailRow = $('acc-detail-'+idx);
    if (!detailRow) return;
    var isHidden = detailRow.style.display === 'none';
    detailRow.style.display = isHidden ? 'table-row' : 'none';
    btn.classList.toggle('expanded', isHidden);
    // highlight parent row
    var parentRow = btn.closest('tr');
    if (parentRow) parentRow.classList.toggle('selected', isHidden);
  });

  async function loadActivity() {
    try {
      var url = '/api/activity?limit=150' + (activityFilter ? '&type='+activityFilter : '');
      var r = await(await fetch(url)).json();
      var events = r.events || [];
      var list = $('activity-list');
      if (!events.length) { list.innerHTML = '<p style="color:var(--text-dim);text-align:center;padding:40px 0">No events yet</p>'; return; }
      var html = '';
      for (var i = events.length - 1; i >= 0; i--) {
        var e = events[i];
        html += '<div class="activity-entry"><span class="at">'+fmtTime(e.t)+'</span><span class="tag '+e.type+'">'+e.type+'</span><span class="msg">'+escHtml(e.msg)+'</span></div>';
      }
      list.innerHTML = html;
    } catch(_) {}
  }

  // ── Settings ───────────────────────────────────────────
  var currentConfig = null;
  var editingFollowerName = null;

  async function loadConfig() {
    try {
      var r = await(await fetch('/api/config')).json();
      currentConfig = r;
      $('cfg-master-path').value = r.master.path || '';
      $('cfg-master-port').value = r.master.port || 15555;
      $('cfg-server-host').value = r.server.host || '';
      $('cfg-server-port').value = r.server.port || 5000;
      $('cfg-poll-ms').value = r.poll_interval_ms || 300;
      renderFollowers(r.followers);
    } catch(e) { toast('Config load failed','error'); }
  }

  window.saveMasterConfig = async function() {
    try {
      await fetch('/api/config/master', {method:'PUT', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({path:$('cfg-master-path').value, port:+$('cfg-master-port').value}) });
      toast('Master config saved');
    } catch(e) { toast(e.message,'error'); }
  };

  window.saveServerConfig = async function() {
    try {
      await fetch('/api/config/server', {method:'PUT', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({host:$('cfg-server-host').value, port:+$('cfg-server-port').value, poll_interval_ms:+$('cfg-poll-ms').value}) });
      toast('Server config saved');
    } catch(e) { toast(e.message,'error'); }
  };

  function renderFollowers(followers) {
    var c = $('followers-list');
    if (!followers || !followers.length) { c.innerHTML = '<p style="color:var(--text-dim);padding:12px 0">No followers configured.</p>'; return; }
    var html = '';
    for (var i = 0; i < followers.length; i++) {
      var f = followers[i];
      var name = escHtml(f.name);
      html += '<div class="follower-card"><div class="fh"><h4>'+name+'</h4><div class="fa">'+
        '<button class="btn btn-sm" data-toggle="edit-follower" data-fname="'+name+'">Edit</button>'+
        '<button class="btn btn-sm" data-toggle="download-agent" data-fname="'+name+'">Download</button>'+
        '<button class="btn btn-sm btn-danger" data-toggle="delete-follower" data-fname="'+name+'">Delete</button>'+
        '</div></div><div class="fd">'+
        '<span>Login: '+(f.login||'\u2014')+'</span><span>Server: '+(f.server||'\u2014')+'</span>'+
        '<span>Lot: '+(f.lot_multiplier||1)+'x</span><span>Port: '+(f.port||'\u2014')+'</span>'+
        '<span>'+(f.has_password?'OK pw':'NO pw')+'</span></div></div>';
    }
    c.innerHTML = html;
  }

  window.showAddFollower = function() {
    editingFollowerName = null;
    var c = $('followers-list');
    var div = document.createElement('div');
    div.className = 'follower-card';
    div.style.borderColor = 'var(--accent)';
    div.id = 'follower-form';
    div.innerHTML =
      '<div class="fh"><h4 id="ff-title">New Follower</h4></div>'+
      '<div class="form-row"><div class="form-group"><label>Name</label><input id="ff-name"></div><div class="form-group"><label>Server</label><input id="ff-server"></div></div>'+
      '<div class="form-row"><div class="form-group"><label>Path</label><input id="ff-path"></div><div class="form-group"><label>Port</label><input type="number" id="ff-port"></div></div>'+
      '<div class="form-row-3"><div class="form-group"><label>Login</label><input type="number" id="ff-login"></div>'+
      '<div class="form-group"><label>Password</label><input type="password" id="ff-password"></div>'+
      '<div class="form-group"><label>Lot</label><input type="number" step="0.1" id="ff-lot"></div></div>'+
      '<div class="form-row-3"><div class="form-group"><label>Max Lot</label><input type="number" step="0.01" id="ff-max-lot"></div>'+
      '<div class="form-group"><label>Min Lot</label><input type="number" step="0.01" id="ff-min-lot"></div>'+
      '<div class="form-group"><label>Deviation</label><input type="number" id="ff-deviation"></div></div>'+
      '<div style="display:flex;gap:8px;margin-top:12px"><button class="btn btn-primary" id="ff-save-btn">Save</button><button class="btn" onclick="cancelFollowerForm()">Cancel</button></div>';
    c.insertBefore(div, c.firstChild);
    $('ff-path').value = $('cfg-master-path').value || 'C:/Program Files/MetaTrader 5/terminal64.exe';
    $('ff-port').value = 15556; $('ff-lot').value = 1.0;
    $('ff-max-lot').value = 10.0; $('ff-min-lot').value = 0.01; $('ff-deviation').value = 20;
    $('ff-save-btn').onclick = saveNewFollower;
  };

  function editFollower(name) {
    var f = currentConfig.followers.find(function(x) { return x.name === name; });
    if (!f) return;
    window.showAddFollower();
    editingFollowerName = name;
    $('ff-title').textContent = 'Edit Follower';
    $('ff-name').value = f.name;
    $('ff-server').value = f.server;
    $('ff-path').value = f.path;
    $('ff-port').value = f.port;
    $('ff-login').value = f.login;
    $('ff-password').placeholder = '(unchanged)';
    $('ff-password').value = '';
    $('ff-lot').value = f.lot_multiplier;
    $('ff-max-lot').value = f.max_lot;
    $('ff-min-lot').value = f.min_lot;
    $('ff-deviation').value = f.deviation;
    $('ff-save-btn').onclick = saveEditFollower;
  }

  window.cancelFollowerForm = function() {
    var f = $('follower-form');
    if (f) f.remove();
    editingFollowerName = null;
  };

  function gatherFF() {
    return {
      name: $('ff-name').value,
      server: $('ff-server').value,
      path: $('ff-path').value,
      port: +$('ff-port').value||15556,
      login: +$('ff-login').value||0,
      password: $('ff-password').value||undefined,
      lot_multiplier: +$('ff-lot').value||1.0,
      max_lot: +$('ff-max-lot').value||10.0,
      min_lot: +$('ff-min-lot').value||0.01,
      deviation: +$('ff-deviation').value||20
    };
  }

  async function saveNewFollower() {
    var d = gatherFF();
    if (!d.name) { toast('Name required','error'); return; }
    if (!d.login) { toast('Login required','error'); return; }
    if (!d.password) { toast('Password required','error'); return; }
    try {
      var r = await(await fetch('/api/config/followers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})).json();
      if (r.status === 'ok') { toast('Follower added'); cancelFollowerForm(); loadConfig(); }
      else toast(r.message||'Error','error');
    } catch(e) { toast(e.message,'error'); }
  }

  async function saveEditFollower() {
    var d = gatherFF();
    try {
      var r = await(await fetch('/api/config/followers/'+encodeURIComponent(editingFollowerName),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})).json();
      if (r.status === 'ok') { toast('Follower updated'); cancelFollowerForm(); loadConfig(); }
      else toast(r.message||'Error','error');
    } catch(e) { toast(e.message,'error'); }
  }

  function deleteFollower(name) {
    if (!confirm('Delete "'+name+'"?')) return;
    fetch('/api/config/followers/'+encodeURIComponent(name),{method:'DELETE'}).then(function() {
      toast('Deleted'); loadConfig();
    });
  }

  function downloadAgent(name) {
    fetch('/api/config/export-agent?name='+encodeURIComponent(name)).then(function(r) {
      if (!r.ok) { r.json().then(function(j) { toast(j.message,'error'); }); return; }
      return r.text();
    }).then(function(text) {
      var blob = new Blob([text],{type:'text/yaml'});
      var a = document.createElement('a'); a.href=URL.createObjectURL(blob);
      a.download=name+'_agent.yaml'; a.click(); URL.revokeObjectURL(a.href);
      toast('Downloaded '+name+'_agent.yaml');
    });
  }

  // ── Backup/Restore ─────────────────────────────────────
  window.backupConfig = async function() {
    try {
      var r = await fetch('/api/config/backup');
      var blob = new Blob([await r.text()],{type:'text/yaml'});
      var a = document.createElement('a'); a.href=URL.createObjectURL(blob);
      a.download='config_backup_'+new Date().toISOString().slice(0,10)+'.yaml';
      a.click(); URL.revokeObjectURL(a.href);
      toast('Backup downloaded');
    } catch(e) { toast('Backup failed','error'); }
  };

  window.restoreConfig = async function(event) {
    var file = event.target.files[0];
    if (!file) return;
    try {
      var text = await file.text();
      var r = await(await fetch('/api/config/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:text})})).json();
      if (r.status === 'ok') { toast(r.message); loadConfig(); }
      else toast(r.error||'Restore failed','error');
    } catch(e) { toast(e.message,'error'); }
    event.target.value = '';
  };

  // ── Notifications ──────────────────────────────────────
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }

  var prevErrors = 0;
  setInterval(function() {
    if (!latestData || !latestData.stats) return;
    var errs = latestData.stats.errors;
    if (errs > prevErrors && prevErrors > 0 && Notification.permission === 'granted') {
      new Notification('Copy Trade Engine', { body: (errs - prevErrors) + ' new error(s)' });
    }
    prevErrors = errs;
  }, 5000);

  // Redraw chart on tab show
  var observer = new MutationObserver(function() {
    if ($('tab-dashboard').classList.contains('active') && chartData.length) drawChart();
  });
  qa('.nav-item').forEach(function(item) {
    observer.observe(item, { attributes: true, attributeFilter: ['class'] });
  });

})();
