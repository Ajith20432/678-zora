/* ZORA v1.2.1 — live dashboard client; /api/state is the only runtime data source. Source of truth: GET /api/state. */
(() => {
  'use strict';

  const $ = (s, r=document) => r.querySelector(s);
  const $$ = (s, r=document) => [...r.querySelectorAll(s)];
  const money = (v) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  const num = (v,d=2) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toFixed(d);
  const pct = (v) => v == null || !Number.isFinite(Number(v)) ? '—' : `${Number(v).toFixed(2)}%`;
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  let state = null;
  let equityChart = null;

  async function fetchState() {
    const r = await fetch('/api/state', {cache:'no-store'});
    if (!r.ok) throw new Error(`API ${r.status}`);
    return r.json();
  }

  function setKpis(s) {
    const m=s.metrics||{}, sum=s.summary||{};
    const cards=$$('.kpi-card');
    const vals=[
      [money(sum.latest_equity), `${s.config.mode.toUpperCase()} · ${s.config.exchange}`],
      [money(sum.realized_pnl), sum.realized_pnl >= 0 ? 'Realized PnL' : 'Realized loss'],
      [pct(m.win_rate_pct), `${sum.closed_trades||0} closed trades`],
      [m.profit_factor == null && sum.closed_trades ? '∞' : num(m.profit_factor), (m.profit_factor == null && sum.closed_trades) || m.profit_factor >= 1 ? 'Healthy' : 'Below 1.0'],
      [pct(m.max_drawdown_pct), `Peak equity: ${money(m.peak_equity)}`],
      [num(m.sharpe), `Sortino: ${num(m.sortino)}`],
    ];
    cards.slice(0,vals.length).forEach((c,i)=>{
      const value=$('.kpi-value',c), delta=$('.kpi-delta',c);
      if(value) value.textContent=vals[i][0];
      if(delta) delta.textContent=vals[i][1];
      if(delta) delta.className='kpi-delta ' + ((i===1 && Number(sum.realized_pnl)<0)|| (i===4 && Number(m.max_drawdown_pct)>10) ? 'negative' : 'positive');
    });
    const mode=$('.status-badge span:last-child');
    if(mode) mode.textContent=`${s.config.mode==='live'?'LIVE':'Paper'} Trading`;
    const version=$('.version'); if(version) version.textContent='ZORA v1.2.1 · LIVE API';
    const last=$('#last-update-time'); if(last) last.textContent=new Date((s.live?.updated||s.generated)*1000).toLocaleTimeString();
    const title=$('#page-title'); if(title) title.textContent='Overview';
    const priceSymbol=(s.config.symbols||[s.config.symbol])[0];
    const price=s.live?.prices?.[priceSymbol];
    $('#eth-price') && ($('#eth-price').textContent = price ? `${priceSymbol} ${money(price)}` : priceSymbol);
    $('#eth-change') && ($('#eth-change').textContent = s.config.mode.toUpperCase());
  }

  function renderOverview(s) {
    const pos=s.positions||[], decisions=s.decisions||[];
    $('#overview-positions').innerHTML = pos.length ? pos.map(p=>{
      const cp=s.live?.prices?.[p.symbol] ?? p.entry;
      const pnl=(cp-p.entry)*p.size*(p.action==='BUY'?1:-1);
      return `<div class="position-item"><div><span class="pos-symbol">${esc(p.symbol)}</span><span class="pos-side ${p.action.toLowerCase()}">${esc(p.action)}</span></div><span class="pos-pnl ${pnl>=0?'positive':'negative'}">${pnl>=0?'+':''}$${money(pnl)}</span></div>`;
    }).join('') : '<div class="empty">No open positions</div>';

    $('#overview-signals').innerHTML = decisions.length ? decisions.slice(0,8).map(d=>
      `<div class="signal-item"><div><span class="signal-time">${new Date(d.ts*1000).toLocaleTimeString()}</span><strong>${esc(d.symbol)}</strong></div><div><span class="signal-action ${String(d.action).toLowerCase()}">${esc(d.action)}</span><span class="signal-confidence">${num(d.confidence,0)}%</span></div><div class="signal-reason">${esc(d.reason||'')}</div></div>`
    ).join('') : '<div class="empty">No decisions yet</div>';
  }

  function renderPositions(s) {
    const rows=(s.positions||[]).map(p=>{
      const cp=s.live?.prices?.[p.symbol] ?? p.entry;
      const pnl=(cp-p.entry)*p.size*(p.action==='BUY'?1:-1);
      return `<tr><td><strong>${esc(p.symbol)}</strong></td><td>${esc(p.action)}</td><td>${num(p.entry)}</td><td>${num(cp)}</td><td>${num(p.size,6)}</td><td>${money(cp*p.size)}</td><td>${num(p.stop)}</td><td>${num(p.take)}</td><td class="${pnl>=0?'positive':'negative'}">${pnl>=0?'+':''}${money(pnl)}</td><td>${p.opened_ts?new Date(p.opened_ts*1000).toLocaleString():'—'}</td></tr>`;
    });
    $('#positions-table').innerHTML=rows.join('')||'<tr><td colspan="10">No open positions.</td></tr>';
    const badge=$('#positions .badge'); if(badge) badge.textContent=`${rows.length} active`;
  }

  function renderTrades(s) {
    const trades=(s.trades||[]).filter(t=>t.result);
    $('#trades-table').innerHTML=trades.map(t=>
      `<tr><td>${new Date(t.ts*1000).toLocaleString()}</td><td>${esc(t.symbol)}</td><td>${esc(t.action)}</td><td>${num(t.price)}</td><td>—</td><td>${num(t.size,6)}</td><td class="${Number(t.pnl)>=0?'positive':'negative'}">${t.pnl==null?'—':(t.pnl>=0?'+':'')+money(t.pnl)}</td><td>—</td><td>—</td><td>${esc(t.result)}</td></tr>`
    ).join('')||'<tr><td colspan="10">No trade history yet.</td></tr>';
  }

  function renderBrain(s) {
    const ew=s.brain?.expert_weights||{}, fw=s.brain?.factor_weights||{};
    const expert=document.getElementById('expert-chart')?.getContext('2d');
    const factor=document.getElementById('factor-chart')?.getContext('2d');
    if(window.Chart && expert){
      if(equityChart?.destroy) equityChart.destroy();
      // Brain charts are created separately below; keep equityChart reserved for equity.
    }
    const json=$('#brain-state-json'); if(json) json.textContent=JSON.stringify(s.brain||{},null,2);
    const stat=$$('.brain-stats .kpi-card');
    if(stat[0]) $('.kpi-value',stat[0]).textContent=String(s.brain?.trades_learned_from||0);
    if(stat[1]) $('.kpi-value',stat[1]).textContent='Adaptive';
    if(stat[2]) $('.kpi-value',stat[2]).textContent=`${Object.keys(ew).length} experts`;
    if(window.Chart){
      const make=(ctx,labels,data,type='bar')=>new Chart(ctx,{type,data:{labels,datasets:[{label:'Weight',data,borderRadius:6}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}}}});
      if(expert){ if(expert.canvas._zoraChart) expert.canvas._zoraChart.destroy(); expert.canvas._zoraChart=make(expert,Object.keys(ew),Object.values(ew));}
      if(factor){ if(factor.canvas._zoraChart) factor.canvas._zoraChart.destroy(); factor.canvas._zoraChart=make(factor,Object.keys(fw),Object.values(fw),'bar');}
    }
  }

  function renderAI(s) {
    const a=s.ai_safety||{};
    $('#ai-providers').innerHTML=`<div class="ai-card"><div class="ai-card-header"><span class="ai-provider-name">AI Guard</span><span class="ai-status ${a.entry_blocked?'cooldown':'active'}">${a.entry_blocked?'BLOCKED':'READY'}</span></div><div class="ai-verdict"><span class="ai-lean ${a.entry_blocked?'hold':'buy'}">${a.entry_blocked?'HOLD':'READY'}</span><span class="ai-confidence">${a.configured?'Configured':'Technical-only mode'}</span></div><div class="ai-note">${esc(a.reason||'AI capacity available')}</div><div class="ai-latency">Calls 24h: ${num(a.calls_24h,0)} · Limit: ${a.daily_limit||'none'}</div></div>`;
    $('#judge-info').innerHTML=`<div><span class="judge-provider">${a.configured?'Multi-provider AI enabled':'AI disabled'}</span></div><p>${esc(a.reason||'No provider outage reported.')}</p>`;
    $('#ai-verdicts-table').innerHTML='<tr><td colspan="7">Provider-level verdict telemetry is advisory and is not persisted as a trade authority.</td></tr>';
  }

  function renderRisk(s) {
    const c=s.config||{}, rows=[
      [0,'Risk Per Trade',pct(c.risk_per_trade_pct)], [0,'Max Exposure',pct(c.max_exposure_pct)],
      [0,'Current Exposure',pct(s.live?.exposure_pct)], [1,'Max Daily Loss',pct(c.max_daily_loss_pct)],
      [1,'Max Drawdown',pct(c.max_drawdown_pct)], [2,'ATR Stop Multiplier',`${num(c.atr_stop_mult)}x`],
      [2,'ATR Take Profit',`${num(c.atr_take_mult)}x`], [2,'Trailing Stop',c.trailing_stop_enabled?'Enabled':'Disabled'],
      [2,'Breakeven',`${num(c.breakeven_at_r)}R`], [3,'Stale Data Limit',`${num(c.stale_data_max_candles)} candles`],
      [3,'Max Spread',`${num(c.max_spread_bps)} bps`], [3,'Min 24h Volume',money(c.min_quote_volume)],
      [4,'Exchange',c.exchange], [4,'Fee Rate',pct(Number(c.fee_rate)*100)], [4,'Slippage',`${num(c.slippage_bps)} bps`],
      [4,'Native Protection',c.native_protection_required?'Required':'Optional'], [4,'Reconcile on Start',c.reconcile_on_start?'Enabled':'Disabled']
    ];
    const cards=$$('.risk-card');
    rows.forEach(([i,label,value])=>{
      if(cards[i]){
        const match=$$('.risk-row',cards[i]).find(r=>$('.risk-label',r)?.textContent.trim()===label);
        if(match) $('.risk-value',match).textContent=value;
      }
    });
  }

  function renderEquity(s) {
    if(!window.Chart) return;
    const series=(s.equity||[]).map(x=>({x:new Date(x[0]*1000),y:Number(x[1])}));
    const ctx=$('#equity-chart')?.getContext('2d'); if(!ctx) return;
    if(equityChart) equityChart.destroy();
    equityChart=new Chart(ctx,{type:'line',data:{labels:series.map(x=>x.x.toLocaleDateString()),datasets:[{label:'Equity',data:series.map(x=>x.y),tension:.25,pointRadius:0,borderWidth:2}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{maxTicksLimit:10}},y:{beginAtZero:false}}}});
  }

  function bindNavigation(){
    const titles={overview:'Overview',positions:'Open Positions',trades:'Trade History',brain:'Brain Weights',ai:'AI Panel',risk:'Risk Settings'};
    $$('.nav-item').forEach(item=>item.addEventListener('click',e=>{
      e.preventDefault(); const page=item.dataset.page;
      $$('.nav-item').forEach(n=>n.classList.remove('active')); item.classList.add('active');
      $$('.page').forEach(p=>p.classList.remove('active')); $('#'+page)?.classList.add('active');
      $('#page-title').textContent=titles[page]||page;
    }));
  }

  async function render() {
    try {
      state=await fetchState();
      setKpis(state); renderOverview(state); renderPositions(state); renderTrades(state);
      renderBrain(state); renderAI(state); renderRisk(state); renderEquity(state);
      document.title=`ZORA v1.2.1 · ${state.config.mode.toUpperCase()}`;
    } catch(e) {
      const badge=$('.bot-status span:last-child'); if(badge) badge.textContent='API Offline';
      console.error('ZORA dashboard:',e);
    }
  }

  bindNavigation();
  render();
  setInterval(render,10000);
  console.log('ZORA dashboard: live /api/state mode');
})();
