const $ = (selector) => document.querySelector(selector);
const core = [
  { code: 'sh000001', name: '上证指数', aliases: ['上证综指', '000001.SH'] },
  { code: 'sz399006', name: '创业板指数', aliases: ['创业板指', '399006'] },
  { code: 'sh000688', name: '科创50', aliases: ['000688'] },
  { code: 'sh000698', name: '科创100', aliases: ['000698'] },
];
const cache = new Map();
const form = $('#backtest');
const fields = form.elements;
const dateFormat = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', dateStyle: 'short' });
const clockFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', timeStyle: 'medium' });
const colors = ['#70ead5', '#ffc879', '#89aaff', '#f497c6', '#b4e47d', '#9aaabe'];

let watch = [];
try {
  const saved = JSON.parse(localStorage.getItem('gainmoney-watch') || '[]');
  watch = saved.filter(item => /^(sh|sz|bj)\d{6}$/.test(item.code) && typeof item.name === 'string').slice(0, 26);
} catch {}
let plotted = [];
const quoteIntervalMs = 10_000;
let pollVersion = 0;
let pollTimer;

function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content != null) node.textContent = content;
  if (className) node.className = className;
  return node;
}

function number(value, signed = false) {
  if (value == null) return '—';
  return (signed && value > 0 ? '+' : '') + value.toLocaleString('zh-CN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}

function percent(value, signed = false) {
  return value == null ? '—' : number(value, signed) + '%';
}

function direction(value) {
  return value > 0 ? 'up' : value < 0 ? 'down' : '';
}

function quoteTime(value, full = false) {
  if (!value || !/^\d{14}$/.test(value)) return '—';
  const hour = value.slice(8, 10) + ':' + value.slice(10, 12) + ':' + value.slice(12);
  return full ? value.slice(4, 6) + '-' + value.slice(6, 8) + ' ' + hour : hour;
}

function row(values) {
  const tr = element('tr');
  for (const value of values) tr.append(value instanceof Node ? value : element('td', value));
  return tr;
}

function renderMarket() {
  $('#indices').replaceChildren(...core.map(item => {
    const q = cache.get(item.code);
    const card = element('article', null, 'index');
    const head = element('div', null, 'index-head');
    head.append(element('h3', item.name), element('span', item.code.toUpperCase(), 'code'));
    const meta = element('div', null, 'index-meta');
    meta.append(element('span', q ? number(q.change, true) + '  ' + percent(q.percent, true) : '行情待更新', direction(q?.percent)),
      element('time', quoteTime(q?.time, true)));
    card.append(head, element('strong', number(q?.price), 'price'), meta);
    return card;
  }));

  const rows = watch.map(item => {
    const q = cache.get(item.code);
    const label = element('td');
    label.append(document.createTextNode((q?.name || item.name) + ' '), element('span', item.code.toUpperCase(), 'code'));
    const actions = element('td');
    const remove = element('button', '移除');
    remove.type = 'button';
    remove.setAttribute('aria-label', '移除 ' + item.name);
    remove.onclick = () => {
      watch = watch.filter(entry => item.code !== entry.code);
      saveWatch();
    };
    actions.append(remove);
    return row([label, number(q?.price), element('td', number(q?.change, true), direction(q?.change)),
      element('td', percent(q?.percent, true), direction(q?.percent)), quoteTime(q?.time, true), actions]);
  });
  if (!rows.length) {
    const message = element('td', '暂无自选股', 'empty');
    message.colSpan = 6;
    rows.push(row([message]));
  }
  $('#watch-rows').replaceChildren(...rows);
}

function renderSymbols() {
  const selected = fields.code.value;
  fields.code.replaceChildren(...[...core, ...watch].map(item =>
    new Option(item.name + ' · ' + item.code.toUpperCase(), item.code)));
  if ([...fields.code.options].some(option => option.value === selected)) fields.code.value = selected;
}

async function api(url, options) {
  const response = await fetch(url, { ...options, cache: 'no-store' });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || '请求失败');
  return body;
}

async function poll(version) {
  const started = performance.now();
  const codes = [...core, ...watch].map(item => item.code);
  try {
    const { quotes } = await api('/api/quotes?codes=' + codes.join(','));
    if (version !== pollVersion) return;
    quotes.forEach(quote => cache.set(quote.code, quote));
    const status = quotes.length === codes.length ? '行情已连接' : '部分代码暂无行情';
    $('#connection').textContent = status;
    $('#connection').classList.toggle('ok', quotes.length > 0);
    const sources = [...new Set(quotes.map(quote => quote.source))].join(' / ');
    $('#quote-status').textContent = sources ? sources + ' · 每 10 秒轮询' : '免费在线行情 · 等待有效报价';
    renderMarket();
  } catch {
    if (version !== pollVersion) return;
    $('#connection').textContent = '行情连接失败';
    $('#connection').classList.remove('ok');
    $('#quote-status').textContent = cache.size ? '已保留上次行情 · 正在重试' : '行情接口不可达 · 正在重试';
  }
  if (version === pollVersion) {
    pollTimer = setTimeout(() => poll(version), Math.max(0, quoteIntervalMs - (performance.now() - started)));
  }
}

function saveWatch() {
  try { localStorage.setItem('gainmoney-watch', JSON.stringify(watch)); } catch {}
  renderSymbols();
  renderMarket();
  clearTimeout(pollTimer);
  poll(++pollVersion);
}

function lookup(query) {
  const index = core.find(item => item.code === query || item.name === query || item.aliases.some(alias => alias.toLowerCase() === query));
  if (index) return index;
  if (/^(sh|sz|bj)\d{6}$/.test(query)) return { code: query, name: query.toUpperCase() };
  if (/^\d{6}$/.test(query)) {
    const prefix = /^(60|68|90)/.test(query) ? 'sh' : /^(00|20|30)/.test(query) ? 'sz' : /^(4|8|92)/.test(query) ? 'bj' : '';
    if (prefix) return { code: prefix + query, name: query };
  }
  throw new Error('请输入六位股票代码，或输入 sh、sz、bj 加六位代码。');
}

$('#search').addEventListener('submit', async (event) => {
  event.preventDefault();
  const field = event.currentTarget.elements.query;
  const button = event.currentTarget.querySelector('button');
  const status = $('#search-status');
  try {
    const item = lookup(field.value.trim().toLowerCase());
    if (core.some(entry => entry.code === item.code)) throw new Error('该指数已在核心指数中。');
    if (watch.some(entry => entry.code === item.code)) throw new Error('该股票已在自选中。');
    if (watch.length >= 26) throw new Error('最多可添加 26 个自选代码。');
    button.disabled = true;
    status.textContent = '正在查询…';
    let quote;
    try {
      ({ quotes: [quote] } = await api('/api/quotes?codes=' + item.code));
    } catch {
      watch.push(item);
      saveWatch();
      status.textContent = '已保存代码，行情恢复后会重新查询。';
      field.value = '';
      return;
    }
    if (!quote) throw new Error('未找到此代码的行情，请检查代码和交易所。');
    cache.set(item.code, quote);
    watch.push({ code: item.code, name: quote.name || item.name });
    saveWatch();
    status.textContent = '已添加 ' + (quote.name || item.name);
    field.value = '';
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

function draw() {
  if (!plotted.length) return;
  const canvas = $('#equity');
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext('2d');
  ctx.scale(ratio, ratio);
  const values = plotted.flatMap(line => line.points.map(point => point[1]));
  const min = Math.min(0.995, ...values);
  const max = Math.max(1.005, ...values);
  const dates = plotted[0].points;
  const left = 61;
  const right = width - 14;
  const top = 17;
  const bottom = height - 26;
  const x = (index) => left + (right - left) * index / (dates.length - 1);
  const y = (value) => bottom - (bottom - top) * (value - min) / (max - min);
  ctx.font = '11px monospace';
  ctx.fillStyle = '#a1afc2';
  for (let i = 0; i <= 4; i++) {
    const value = min + (max - min) * i / 4;
    const position = y(value);
    ctx.beginPath();
    ctx.strokeStyle = '#253244';
    ctx.moveTo(left, position);
    ctx.lineTo(right, position);
    ctx.stroke();
    ctx.textAlign = 'right';
    ctx.fillText(percent((value - 1) * 100), left - 6, position + 4);
  }
  ctx.textAlign = 'left';
  ctx.fillText(dates[0][0], left, height - 7);
  ctx.textAlign = 'right';
  ctx.fillText(dates.at(-1)[0], right, height - 7);
  plotted.forEach((line, i) => {
    ctx.strokeStyle = colors[i];
    ctx.lineWidth = 1.8;
    ctx.setLineDash(line.baseline ? [4, 4] : []);
    ctx.beginPath();
    line.points.forEach((point, j) => {
      if (j === 0) ctx.moveTo(x(j), y(point[1]));
      else ctx.lineTo(x(j), y(point[1]));
    });
    ctx.stroke();
  });
}

function renderResult(data, label) {
  $('#result-meta').textContent = label + ' · ' + data.source + ' · ' + data.first + ' 至 ' + data.last + ' · ' + data.days + ' 根日线';
  $('#metrics').replaceChildren(...data.results.map(item => row([
    item.name + (item.open ? '（持仓中）' : ''),
    element('td', percent(item.return, true), direction(item.return)),
    percent(item.drawdown), percent(item.winRate), item.trades.length,
  ])));
  const trades = $('#trade-details');
  trades.replaceChildren();
  for (const item of data.results) {
    trades.append(element('h4', item.name));
    if (!item.trades.length) {
      trades.append(element('p', '区间内无已平仓交易。'));
      continue;
    }
    const scroll = element('div', null, 'table-scroll');
    const table = element('table');
    const thead = table.createTHead();
    const header = thead.insertRow();
    ['买入日期', '买入价', '卖出日期', '卖出价', '净收益'].forEach(text => header.append(element('th', text)));
    table.createTBody().append(...item.trades.map(trade => row([
      trade.entry, number(trade.buy), trade.exit, number(trade.sell), percent(trade.return, true),
    ])));
    scroll.append(table);
    trades.append(scroll);
  }
  const first = data.bars[0][1];
  plotted = data.results.map(item => ({ name: item.name, points: item.equity }));
  plotted.push({ name: '买入持有基准（收盘、无费用）', baseline: true,
    points: data.bars.map(([day, close]) => [day, close / first]) });
  $('#legend').replaceChildren(...plotted.map((line, i) => {
    const label = element('span');
    const dot = element('i', null, 'legend-dot');
    dot.style.backgroundColor = colors[i];
    label.append(dot, document.createTextNode(line.name));
    return label;
  }));
  $('#result-empty').hidden = true;
  $('#result-body').hidden = false;
  draw();
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = form.querySelector('button');
  const status = $('#test-status');
  $('#result-body').hidden = true;
  $('#result-empty').hidden = false;
  $('#result-meta').textContent = '正在准备回测';
  const strategies = [...form.querySelectorAll('input[name=strategy]:checked')].map(field => field.value);
  const data = { code: fields.code.value, start: fields.start.value, end: fields.end.value,
    fee: fields.fee.value, hold: fields.hold.value, rule: fields.rule.value, strategies };
  const label = fields.code.selectedOptions[0].textContent;
  button.disabled = true;
  status.textContent = '正在读取日线并计算…';
  try {
    if (!strategies.length) throw new Error('请至少选择一个策略。');
    const result = await api('/api/backtest', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    renderResult(result, label);
    status.textContent = '';
  } catch (error) {
    $('#result-meta').textContent = '未生成本次回测结果';
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

const now = new Date();
const start = new Date(now.getTime() - 365 * 86400000);
fields.start.value = dateFormat.format(start);
fields.end.value = dateFormat.format(now);
function updateClock() { $('#clock').textContent = clockFormat.format(new Date()); }
updateClock();
setInterval(updateClock, 1000);
new ResizeObserver(draw).observe($('#equity'));
renderSymbols();
renderMarket();
poll(++pollVersion);
