// 小团前端逻辑
const $ = (s, r = document) => r.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
const esc = s => (s == null ? '' : String(s)).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const img = p => p ? (p.startsWith('/') || p.startsWith('http') ? p : `/static/${p}`) : '/static/placeholder.png';
const brief = (s, n = 56) => {
  const text = String(s || '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  return text.length > n ? text.slice(0, n - 1) + '…' : text;
};
const cleanText = s => String(s || '').replace(/\s+/g, ' ').trim();
const fmt = obj => JSON.stringify(obj || {}, null, 2);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

const feed = $('#feed'), timeline = $('#timeline'), welcome = $('#welcome');
const input = $('#input'), sendBtn = $('#send'), voiceBtn = $('#voice');
const techLog = $('#tech-log'), techStage = $('#tech-stage');
const drawer = $('#drawer'), drawerMask = $('#drawer-mask');

let state = { active: false, intake: false, done: false, segments: [], curIndex: 0, busy: false, suppressLiveUntil: 0 };
let voiceState = { recognition: null, listening: false, holdMode: false, longPress: false, pressTimer: null };

// ---------------- SSE ----------------
const STAGE_LABEL = {
  locate: '定位', intent: '理解需求', plan: '编排行程', search: '检索候选',
  context: '上下文整理', skill: '技能内化', reason: '推荐决策', chat: '对话', plan_ready: '生成方案', execute: '执行下单',
};
function connectSSE() {
  const es = new EventSource('/api/events');
  es.onmessage = e => { try { handleEvent(JSON.parse(e.data)); } catch (_) {} };
  es.onerror = () => { /* 浏览器自动重连 */ };
}
function handleEvent(ev, fromReplay = false) {
  if (!fromReplay && state.suppressLiveUntil && Date.now() < state.suppressLiveUntil) return;
  switch (ev.type) {
    case 'stage':
      techStage.textContent = STAGE_LABEL[ev.stage] || ev.stage;
      pushTech('ev-stage', 'STEP', `${STAGE_LABEL[ev.stage] || ev.stage}`, ev.detail || '', ev.ts);
      break;
    case 'user_action':
      techStage.textContent = '用户操作';
      pushTech('ev-action', 'USER', ev.text || '用户操作', ev.target || '', ev.ts);
      break;
    case 'thinking': pushTech('ev-think', 'THOUGHT', '模型独白', ev.text, ev.ts); break;
    case 'tool_call':
      pushTech('ev-tool', 'TOOL IN', ev.name, fmt(ev.args), ev.ts); break;
    case 'tool_result':
      pushTech('ev-result', 'TOOL OUT', ev.name, fmt(ev.result), ev.ts); break;
    case 'constraints':
      pushTech('ev-think', 'PARSE', '结构化约束', fmt(ev.data), ev.ts); break;
    case 'self_heal':
      pushTech('ev-heal', 'ADJUST', '检测到执行约束',
        ev.kind === 'conflict' ? `时间冲突 ${ev.from}→${ev.to}：${ev.poi}` : `${ev.poi} ${healLabel(ev.kind)}`, ev.ts);
      break;
    case 'self_heal_ok': pushTech('ev-heal', 'ADJUST', '已自动调整', ev.note, ev.ts); break;
    case 'card': pushTech('ev-card', 'CARD', '产出推荐卡', ev.card?.poi?.name || '', ev.ts); break;
    case 'plan_ready':
      pushTech('ev-card', 'PLAN', '完整方案就绪',
        (ev.plan?.stops || []).map(s => s.name).join(' → '), ev.ts);
      break;
    case 'execute_done':
      pushTech('ev-card', 'DONE', ev.ok ? '执行完成' : '执行需确认',
        (ev.results || []).map(r => `${r.label} · ${r.name}`).join('\n'), ev.ts);
      break;
    case 'segment_plan': pushTech('ev-stage', '规划', '分段完成',
      (ev.segments || []).map(s => s.label).join(' → '), ev.ts); break;
  }
}
function healLabel(k) {
  return {
    no_table: '无可订座位',
    sold_out: '库存不足',
    conflict: '时间需要错开',
    auto_replace: '实时可用性不足',
  }[k] || k;
}
function pushTech(cls, tag, title, body, ts) {
  const e = el('div', `ev ${cls}`);
  e.innerHTML = `<div class="ev-h"><span class="tg">${esc(tag)}</span>${esc(title)}
    <span class="ev-ts">${ts != null ? ts + 's' : ''}</span></div>
    ${body ? `<div class="ev-b">${esc(body)}</div>` : ''}`;
  techLog.appendChild(e);
  techLog.scrollTop = techLog.scrollHeight;
}

async function replayAgentEvents(events, delayMs, opts = {}) {
  if (!events || !events.length) return;
  if (opts.clear) techLog.innerHTML = '';
  techStage.textContent = opts.stage || '同步中';
  state.suppressLiveUntil = Date.now() + Math.max(4000, events.length * delayMs + 1200);
  for (const ev of events) {
    handleEvent(ev, true);
    const eventDelay = Math.max(0, Number(ev.delay_ms || 0));
    await sleep(eventDelay || delayMs);
  }
  state.suppressLiveUntil = 0;
}

// ---------------- 启动会话 ----------------
async function start(query, opts = {}) {
  if (state.busy) return;
  const source = opts.source || 'custom';
  state.busy = true; setComposer(false);
  welcome.style.display = 'none';
  addUserBubble(query);
  const thinking = addThinking(source === 'preset' ? '正在思考中…' : '正在理解你的需求…');
  try {
    const r = await fetch('/api/start', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ query, source, preset_id: opts.presetId }),
    });
    const d = await r.json();
    if (d.error) { thinking.remove(); addThinking('出错了：' + d.error); return; }
    if (source === 'preset' && d.agent_events) {
      await replayAgentEvents(d.agent_events, d.agent_delay_ms || 220, { clear: true, stage: '启动中' });
    }
    thinking.remove();
    if (d.needs_more_info && !d.card) {
      if (d.reply) addAssistantBubble(d.reply);
      renderIntakeOptions(d.intake_options);
      state.active = true; state.intake = true; state.done = false;
      state.segments = []; state.curIndex = 0;
      $('#loc').textContent = '📍 ' + (d.location?.business_area || '南京');
      input.placeholder = '告诉我同行人、时间、区域或想吃/想玩的方向…';
      return;
    }
    state.active = true; state.intake = false; state.done = false;
    state.segments = d.segments; state.curIndex = d.current_index;
    $('#loc').textContent = '📍 ' + (d.location?.business_area || '南京');
    renderNarrative(d.narrative, d.segments, d.current_index);
    if (d.card) renderCard(d.card);
    input.placeholder = '想换换口味 / 补充需求都能跟我说…';
  } catch (e) { thinking.remove(); addThinking('网络异常：' + e.message); }
  finally { state.busy = false; setComposer(true); }
}

// ---------------- 卡片操作 ----------------
async function act(path, body) {
  if (state.busy) return; state.busy = true; setComposer(false);
  state.suppressLiveUntil = Date.now() + 6000;
  const thinking = addThinking('稍等，我想想…');
  try {
    const r = await fetch(path, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const d = await r.json();
    if (d.agent_events) {
      await replayAgentEvents(d.agent_events, d.agent_delay_ms || 120, { clear: false, stage: '同步中' });
    }
    thinking.remove();
    state.curIndex = d.current_index ?? state.curIndex;
    if (d.plan) updateSegPills(d.plan);
    if (d.done) { state.done = true; renderPlan(d.plan); }
    else if (d.card) renderCard(d.card);
    return d;
  } catch (e) { state.suppressLiveUntil = 0; thinking.remove(); addThinking('出错：' + e.message); }
  finally { state.busy = false; setComposer(true); }
}
const accept = () => act('/api/accept');
const reject = () => act('/api/reject');

async function sendChat(msg) {
  if (state.busy) return; state.busy = true; setComposer(false);
  addUserBubble(msg);
  const thinking = addThinking('在想怎么帮你调整…');
  try {
    const r = await fetch('/api/chat', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ message: msg }),
    });
    const d = await r.json();
    if (d.agent_events) {
      await replayAgentEvents(d.agent_events, d.agent_delay_ms || 120, { clear: false, stage: '对话' });
    }
    thinking.remove();
    if (d.reply) addAssistantBubble(d.reply);
    if (d.needs_more_info && !d.card) {
      state.intake = true;
      renderIntakeOptions(d.intake_options);
      input.placeholder = '继续补充区域、时间、同行人或偏好…';
      return;
    }
    if (d.segments && d.segments.length) {
      clearIntakeOptions();
      state.intake = false;
      state.active = true;
      state.done = false;
      state.segments = d.segments;
      state.curIndex = d.current_index || 0;
      renderNarrative(d.narrative, d.segments, d.current_index || 0);
      input.placeholder = '想换换口味 / 补充需求都能跟我说…';
    }
    if (d.card) renderCard(d.card);
  } catch (e) { thinking.remove(); addThinking('出错：' + e.message); }
  finally { state.busy = false; setComposer(true); }
}

// ---------------- 渲染 ----------------
function addUserBubble(t) { clearIntakeOptions(); const b = el('div', 'bubble-user', esc(t)); timeline.appendChild(b); scroll(); }
function addAssistantBubble(t) {
  const b = el('div', 'assistant-dialog', `<div class="avatar">团</div>
    <div class="say"><div class="say-body">${esc(t)}</div></div>`);
  timeline.appendChild(b); scroll();
}
function clearIntakeOptions() {
  timeline.querySelectorAll('.intake-options').forEach(n => n.remove());
}
function renderIntakeOptions(groups) {
  clearIntakeOptions();
  if (!groups || !groups.length) return;
  const wrap = el('div', 'intake-options');
  groups.forEach(group => {
    const card = el('div', 'intake-card');
    card.appendChild(el('div', 'intake-title', esc(group.title || '补充一下')));
    const choices = el('div', 'intake-choices');
    (group.options || []).forEach(opt => {
      const btn = el('button', 'intake-choice', esc(opt.label || opt.value || '选择'));
      btn.type = 'button';
      btn.onclick = () => sendChat(opt.value);
      choices.appendChild(btn);
    });
    card.appendChild(choices);
    wrap.appendChild(card);
  });
  timeline.appendChild(wrap); scroll();
}
function addThinking(t) {
  const b = el('div', 'thinking', `<div class="spin"></div><div class="tx">${esc(t)}</div>`);
  timeline.appendChild(b); scroll(); return b;
}
function renderNarrative(narr, segs, cur) {
  if (narr) { const n = el('div', 'tl-narrative', esc(narr)); timeline.appendChild(n); }
  const pills = el('div', 'seg-pills'); pills.id = 'seg-pills';
  renderPills(pills, segs, cur, []); timeline.appendChild(pills); scroll();
}
function renderPills(box, segs, cur, accepted) {
  box.innerHTML = '';
  segs.forEach((s, i) => {
    let cls = 'seg-pill';
    if (accepted.includes(i)) cls += ' done';
    else if (i === cur) cls += ' cur';
    box.appendChild(el('span', cls, esc(s.label)));
  });
}
function updateSegPills(plan) {
  const box = $('#seg-pills'); if (!box) return;
  const accepted = [];
  (plan.stops || []).forEach((st, i) => { if (st.accepted) accepted.push(i); });
  renderPills(box, state.segments, state.curIndex, accepted);
}

function renderCard(card) {
  const poi = card.poi || {};
  const c = el('div', 'card');
  const g = card.groupon;
  const bookingExecution = card.booking_execution;
  const bookingIcon = bookingExecution?.status === 'pending' ? '待' : '✓';
  renderRecommendationSpeech(card);
  const meta = [
    poi.category ? `<span class="cat">${esc(poi.category)}</span>` : '<span>-</span>',
    poi.rating ? `<span>⭐ <b>${esc(poi.rating)}</b></span>` : '<span>-</span>',
    poi.price_per_person ? `<span>¥${esc(poi.price_per_person)}/人</span>` : '<span>免费</span>',
    poi.distance_km != null ? `<span>📍 ${esc(poi.distance_km)}km</span>` : '<span>-</span>',
  ].join('');
  c.innerHTML = `
    <div class="ph"><img src="${img(poi.image)}" alt=""></div>
    <div class="body">
      <div class="nm">${esc(poi.name || '')}</div>
      <div class="meta">${meta}</div>
      ${g ? `<div class="groupon"><div class="g-t"><span>🎫 ${esc(g.title)}</span>
        <span class="price">¥${esc(g.price)}</span></div>
        ${card.groupon_reason ? `<div class="g-r">${esc(brief(card.groupon_reason, 34))}</div>` : ''}</div>` : ''}
      ${bookingExecution ? `<div class="booking-exec">
        <div class="be-icon">${bookingIcon}</div>
        <div class="be-copy"><div class="be-title">${esc(bookingExecution.title)}</div>
          <div class="be-sub">${esc(bookingExecution.subtitle)}</div></div>
        ${bookingExecution.wait_min != null ? `<div class="be-wait">${esc(bookingExecution.wait_min)}min</div>` : ''}
      </div>` : ''}
      <div class="actions">
        <button class="btn btn-accept">就这家，下一步</button>
        <button class="btn btn-reject">换一个</button>
        <button class="btn btn-detail">详情</button>
      </div>
    </div>`;
  c.querySelector('.btn-accept').onclick = () => { lockCard(c, '已选 ✓'); accept(); };
  c.querySelector('.btn-reject').onclick = () => { c.remove(); reject(); };
  c.querySelector('.btn-detail').onclick = () => openDrawer(poi.id);
  timeline.appendChild(c); scroll();
}
function renderRecommendationSpeech(card) {
  const lines = [];
  if (card.summary) lines.push(cleanText(card.summary));
  if (card.suggestion) lines.push(cleanText(card.suggestion));
  if (!lines.length) return;
  addAssistantBubble(lines.join('\n\n'));
}
function lockCard(c, label) {
  const a = c.querySelector('.actions');
  if (a) a.innerHTML = `<div style="flex:1;text-align:center;color:var(--green-d);font-weight:700;padding:8px">${label}</div>`;
}

function renderPlan(plan) {
  const wrap = el('div', 'plan');
  const stops = (plan.stops || []).map(s => `
    <div class="stop">
      <div class="time">${esc(s.start_time || '')}</div>
      <img class="si" src="${img(s.image)}" alt="">
      <div class="info">
        <div class="n">${esc(s.label)} · ${esc(s.name)}</div>
        <div class="s">${esc(s.summary || '')}</div>
        ${s.groupon ? `<div class="g">🎫 ${esc(s.groupon.title)} ¥${esc(s.groupon.price)}</div>` : ''}
      </div>
    </div>`).join('');
  wrap.innerHTML = `
    <div class="ph"><h3>🎉 下午方案已就绪</h3><p>共 ${plan.stops.length} 站 · 小团已为你规划好动线与团购</p></div>
    ${stops}
    <div class="exec-wrap"><button class="btn-exec">⚡ 一键下单执行</button>
      <div class="exec-res" id="exec-res"></div></div>`;
  wrap.querySelector('.btn-exec').onclick = e => { e.target.disabled = true; e.target.textContent = '执行中…'; execute(e.target); };
  timeline.appendChild(wrap); scroll();
}

async function execute(btn) {
  const box = $('#exec-res');
  try {
    state.suppressLiveUntil = Date.now() + 8000;
    const r = await fetch('/api/execute', { method: 'POST' });
    const d = await r.json();
    if (d.agent_events) {
      await replayAgentEvents(d.agent_events, d.agent_delay_ms || 120, { clear: false, stage: '执行下单' });
    }
    (d.results || []).forEach((res, i) => {
      setTimeout(() => {
        const row = el('div', 'exec-row' + (res.healed ? ' healed' : ''));
        row.innerHTML = `<div class="ico">${res.healed ? '↻' : '✓'}</div>
          <div class="ex-main"><div class="t">${esc(res.label)} · ${esc(res.name)}</div>
            <div class="d">${esc(res.confirm || res.status)} ${res.eta_min ? `· 约${res.eta_min}分钟` : ''}</div></div>
          ${res.healed ? `<span class="heal-badge">已调整</span>` : ''}`;
        box.appendChild(row); box.scrollIntoView({ block: 'end', behavior: 'smooth' });
      }, i * 600);
    });
    setTimeout(() => {
      box.appendChild(el('div', 'exec-done', d.ok ? '✅ 全部预订完成，出发吧！' : '⚠️ 部分项目需到店确认'));
      if (btn) btn.textContent = '✅ 已执行';
    }, (d.results || []).length * 600 + 200);
  } catch (e) { state.suppressLiveUntil = 0; box.appendChild(el('div', 'exec-done', '执行出错：' + e.message)); }
}

// ---------------- 详情抽屉 ----------------
async function openDrawer(poiId) {
  drawer.innerHTML = `<div class="d-body"><div class="thinking"><div class="spin"></div>
    <div class="tx">加载详情…</div></div></div>`;
  drawerMask.classList.add('show'); drawer.classList.add('show');
  try {
    const r = await fetch('/api/detail/' + encodeURIComponent(poiId));
    const d = await r.json();
    renderDrawer(d);
  } catch (e) { drawer.innerHTML = `<div class="d-body">加载失败：${esc(e.message)}</div>`; }
}
function closeDrawer() { drawerMask.classList.remove('show'); drawer.classList.remove('show'); }
drawerMask.onclick = closeDrawer;

function stars(n) { return '★'.repeat(Math.round(n || 5)) + '☆'.repeat(5 - Math.round(n || 5)); }
function renderDrawer(d) {
  const notes = (d.dianping_notes || []).map(n => `
    <div class="note"><img src="${img((n.images && n.images[0]) || n.image || d.image)}" alt="">
      <div class="nb"><div class="nu">${esc(n.author || '点评用户')} · ${esc(n.title || '')}</div>
        <div class="nc">${esc(brief(n.content || n.body || '', 84))}</div></div></div>`).join('') || '<div class="nc" style="color:#9ca3af;font-size:12px">暂无种草帖</div>';
  const gps = (d.groupon || []).map(g => `
    <div class="gp"><div class="gt"><span>${esc(g.title)}</span><span class="gpr">¥${esc(g.price)}</span></div>
      ${g.desc ? `<div class="gd">${esc(g.desc)}</div>` : ''}</div>`).join('') || '';
  const rvs = (d.reviews || []).slice(0, 4).map(rv => `
    <div class="rv"><div class="ru"><span>${esc(rv.author || '用户')}</span>
      <span class="stars">${rv.rating ? stars(rv.rating) : '有用 ' + esc(rv.useful || 0)}</span></div>
      <div class="rc">${esc(brief(rv.content || rv.text || rv.body || '', 62))}</div></div>`).join('') || '';
  const alts = (d.alternatives || []).map(a => `
    <div class="alt" data-id="${esc(a.id)}"><img src="${img(a.image)}" alt="">
      <div class="ab"><div class="an">${esc(a.name)}</div>
        <div class="am">⭐${esc(a.rating)} · ¥${esc(a.price_per_person)}/人 · ${esc(a.distance_km)}km</div></div>
      <span class="pick">改选</span></div>`).join('');
  drawer.innerHTML = `
    <div class="d-hero"><div class="d-grip"></div><img src="${img(d.image)}" alt="">
      <button class="d-close">✕</button></div>
    <div class="d-body">
      <div class="d-nm">${esc(d.name)}</div>
      <div class="d-meta"><span class="cat">${esc(d.category)}</span>
        <span>⭐ <b>${esc(d.rating)}</b>（${esc(d.review_count || 0)}条）</span>
        <span>¥${esc(d.price_per_person)}/人</span><span>📍 ${esc(d.distance_km)}km</span></div>
      ${d.address ? `<div class="d-address">🏠 ${esc(d.address)}</div>` : ''}
      ${gps ? `<div class="d-sec"><h4>🎫 团购套餐</h4>${gps}</div>` : ''}
      <div class="d-sec"><h4>🔥 网友种草</h4>${notes}</div>
      ${rvs ? `<div class="d-sec"><h4>💬 精选评价</h4>${rvs}</div>` : ''}
      ${alts ? `<div class="d-sec"><h4>🔄 同段其它选择（不满意可改选）</h4>${alts}
        <div style="font-size:11px;color:#9ca3af;margin-top:4px">点任意一家可手动改选这一段</div></div>` : ''}
    </div>`;
  drawer.querySelector('.d-close').onclick = closeDrawer;
  drawer.querySelectorAll('.alt').forEach(a => a.onclick = () => {
    const id = a.dataset.id; closeDrawer();
    // 把当前未确认卡片替换掉
    const cards = timeline.querySelectorAll('.card');
    if (cards.length) cards[cards.length - 1].remove();
    act('/api/switch', { poi_id: id });
  });
}

// ---------------- 输入 ----------------
function defaultPlaceholder() {
  return state.active ? '想换换口味 / 补充需求都能跟我说…' : '说说你想怎么过这个下午…';
}
function setComposer(on) {
  input.disabled = !on; sendBtn.disabled = !on;
  if (voiceBtn) voiceBtn.disabled = !on;
  if (!on) stopVoice(true);
}
function scroll() { feed.scrollTop = feed.scrollHeight; }
function submit() {
  const v = input.value.trim(); if (!v || state.busy) return;
  input.value = '';
  if (!state.active) start(v, { source: 'custom' }); else sendChat(v);
}
sendBtn.onclick = submit;
input.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
function getRecognition() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) return null;
  if (voiceState.recognition) return voiceState.recognition;
  const rec = new Recognition();
  rec.lang = 'zh-CN';
  rec.interimResults = true;
  rec.continuous = false;
  rec.maxAlternatives = 1;
  rec.onstart = () => setVoiceListening(true);
  rec.onresult = e => {
    let interim = '', finalText = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const text = e.results[i][0]?.transcript || '';
      if (e.results[i].isFinal) finalText += text;
      else interim += text;
    }
    if (finalText.trim()) appendVoiceText(finalText);
    else if (interim.trim()) input.placeholder = '正在听：' + brief(interim, 16);
  };
  rec.onerror = e => {
    setVoiceListening(false);
    input.placeholder = e.error === 'not-allowed' ? '允许麦克风权限后可语音输入' : defaultPlaceholder();
  };
  rec.onend = () => setVoiceListening(false);
  voiceState.recognition = rec;
  return rec;
}
function appendVoiceText(text) {
  const next = text.trim();
  if (!next) return;
  input.value = input.value.trim()
    ? `${input.value.trim()} ${next}`
    : next;
  input.focus();
}
function setVoiceListening(on) {
  voiceState.listening = on;
  if (!voiceBtn) return;
  voiceBtn.classList.toggle('listening', on);
  voiceBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  voiceBtn.title = on ? '正在听，松开或再次点击停止' : '短按开始/停止，长按按住说话';
  input.placeholder = on ? (voiceState.holdMode ? '按住说话中…' : '正在听…') : defaultPlaceholder();
  if (!on) voiceState.holdMode = false;
}
function startVoice(holdMode) {
  if (state.busy || !voiceBtn || voiceBtn.disabled) return;
  const rec = getRecognition();
  if (!rec) {
    input.placeholder = '当前浏览器不支持语音输入';
    input.focus();
    return;
  }
  voiceState.holdMode = !!holdMode;
  try { rec.start(); }
  catch (_) { stopVoice(true); }
}
function stopVoice(silent) {
  clearTimeout(voiceState.pressTimer);
  voiceState.pressTimer = null;
  if (voiceState.recognition && voiceState.listening) {
    try { voiceState.recognition.stop(); } catch (_) {}
  }
  setVoiceListening(false);
  if (!silent) input.focus();
}
if (voiceBtn) {
  voiceBtn.addEventListener('pointerdown', e => {
    if (voiceBtn.disabled) return;
    e.preventDefault();
    voiceBtn.setPointerCapture?.(e.pointerId);
    voiceState.longPress = false;
    clearTimeout(voiceState.pressTimer);
    voiceState.pressTimer = setTimeout(() => {
      voiceState.longPress = true;
      startVoice(true);
    }, 360);
  });
  voiceBtn.addEventListener('pointerup', e => {
    if (voiceBtn.disabled) return;
    e.preventDefault();
    clearTimeout(voiceState.pressTimer);
    voiceState.pressTimer = null;
    if (voiceState.longPress) stopVoice();
    else if (voiceState.listening) stopVoice();
    else startVoice(false);
  });
  ['pointercancel', 'pointerleave'].forEach(type => voiceBtn.addEventListener(type, () => {
    clearTimeout(voiceState.pressTimer);
    voiceState.pressTimer = null;
    if (voiceState.longPress || voiceState.holdMode) stopVoice();
  }));
  voiceBtn.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    if (voiceState.listening) stopVoice(); else startVoice(false);
  });
}
$('#chips').addEventListener('click', e => {
  const b = e.target.closest('.chip');
  if (b) start(b.dataset.q, { source: 'preset', presetId: b.dataset.presetId });
});

connectSSE();
