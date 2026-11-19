import argparse
import json
import math
import re
import threading
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SINA_QUOTES = "https://hq.sinajs.cn"
EASTMONEY_QUOTES = "https://push2.eastmoney.com"
EASTMONEY_HISTORY = "https://push2his.eastmoney.com"
SHANGHAI = ZoneInfo("Asia/Shanghai")
SYMBOL = re.compile(r"(?:sh|sz|bj)\d{6}\Z")
STRATEGIES = {
    "ma": ("均线交叉", "MA5 上穿 MA20 买入，下穿卖出"),
    "macd": ("MACD", "DIF 上穿 DEA 买入，下穿卖出"),
    "rsi": ("RSI 反转", "RSI14 上穿 30 买入，上穿 70 卖出"),
    "boll": ("布林带反转", "收盘价回到下轨上方买入，上穿中轨卖出"),
    "custom": ("自定义规则", ""),
}
QUOTE_CACHE_SECONDS = 9
quote_lock = threading.Lock()
quote_cache = {}


def fetch(url, referer):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    with urlopen(request, timeout=6) as response:
        return response.read()


def symbol(value):
    value = value.strip().lower()
    if SYMBOL.fullmatch(value):
        return value
    if re.fullmatch(r"\d{6}", value):
        if value.startswith(("60", "68", "90")):
            return "sh" + value
        if value.startswith(("00", "20", "30")):
            return "sz" + value
        if value.startswith(("4", "8", "92")):
            return "bj" + value
    raise ValueError("请输入六位股票代码，或使用 sh、sz、bj 加六位代码")


def sina_quotes(codes):
    body = fetch(f"{SINA_QUOTES}/list={','.join(codes)}", "https://finance.sina.com.cn/").decode(
        "gb18030", errors="replace")
    found = {}
    rows = re.findall(r'var hq_str_((?:sh|sz|bj)\d{6})="([^"]*)"', body)
    if not rows:
        raise ValueError("新浪财经返回了无法识别的报价")
    for code, line in rows:
        fields = line.split(",")
        if len(fields) < 32:
            continue
        try:
            previous, price = float(fields[2]), float(fields[3])
            high, low = float(fields[4]), float(fields[5])
            if price <= 0 or previous <= 0 or not all(map(math.isfinite, (price, previous, high, low))):
                continue
            timestamp = datetime.fromisoformat(f"{fields[30]}T{fields[31]}").strftime("%Y%m%d%H%M%S")
        except ValueError:
            continue
        change = price - previous
        found[code] = dict(code=code, name=fields[0], price=price, previous=previous,
                           time=timestamp, change=change, percent=change / previous * 100,
                           high=high, low=low, source="新浪财经")
    return found


def eastmoney_id(code):
    return ("1." if code.startswith("sh") else "0.") + code[2:]


def eastmoney_quotes(codes):
    params = urlencode({"secids": ",".join(map(eastmoney_id, codes)), "fltt": 2,
                        "fields": "f2,f3,f4,f12,f13,f14,f15,f16,f18,f124"})
    data = fetch(f"{EASTMONEY_QUOTES}/api/qt/ulist.np/get?{params}", "https://quote.eastmoney.com/")
    payload = json.loads(data)
    if payload.get("rc") != 0:
        raise ValueError("东方财富报价接口返回错误")
    rows = (payload.get("data") or {}).get("diff") or []
    if isinstance(rows, dict):
        rows = rows.values()
    by_id = {eastmoney_id(code): code for code in codes}
    found = {}
    for item in rows:
        code = by_id.get(f"{item.get('f13')}.{item.get('f12')}")
        if code is None:
            continue
        try:
            price, change, percent, high, low, previous = (
                float(item[key]) for key in ("f2", "f4", "f3", "f15", "f16", "f18"))
            if price <= 0 or not all(map(math.isfinite, (price, change, percent, high, low, previous))):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        stamp = item.get("f124")
        try:
            timestamp = datetime.fromtimestamp(float(stamp), SHANGHAI).strftime("%Y%m%d%H%M%S") if stamp else ""
        except (OSError, OverflowError, TypeError, ValueError):
            timestamp = ""
        found[code] = dict(code=code, name=item.get("f14") or code.upper(), price=price,
                           previous=previous, time=timestamp, change=change, percent=percent,
                           high=high, low=low, source="东方财富")
    return found


def quotes(codes):
    with quote_lock:
        now = time.monotonic()
        needed = [code for code in codes
                  if now - quote_cache.get(code, (float("-inf"), None))[0] >= QUOTE_CACHE_SECONDS]
        if not needed:
            return [quote_cache[code][1] for code in codes if quote_cache[code][1]]
        found = {}
        available = False
        for provider in (sina_quotes, eastmoney_quotes):
            remaining = [code for code in needed if code not in found]
            if not remaining:
                break
            try:
                found.update(provider(remaining))
                available = True
            except (OSError, ValueError):
                continue
        if not available:
            raise ConnectionError("新浪财经和东方财富报价暂不可用，请检查网络连接")
        refreshed = time.monotonic()
        for code in needed:
            quote_cache[code] = (refreshed, found.get(code))
        return [quote_cache[code][1] for code in codes if quote_cache[code][1]]


def parse_bar(day, opening, closing, high, low):
    day = date.fromisoformat(day).isoformat()
    values = tuple(map(float, (opening, closing, high, low)))
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise ValueError("K 线价格必须是正数")
    return (day, *values)


@lru_cache(maxsize=128)
def online_history(code, start, end, refresh_bucket):
    if end < start:
        return []
    index_codes = {"sh000001", "sz399006", "sh000688", "sh000698"}
    params = urlencode({
        "secid": eastmoney_id(code), "klt": 101, "fqt": 0 if code in index_codes else 1,
        "beg": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"), "lmt": 4000,
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55",
    })
    try:
        payload = json.loads(fetch(f"{EASTMONEY_HISTORY}/api/qt/stock/kline/get?{params}",
                                   "https://quote.eastmoney.com/"))
        if payload.get("rc") != 0:
            raise ValueError("日线接口返回错误")
        rows = (payload.get("data") or {}).get("klines") or []
        bars = [parse_bar(*row.split(",")[:5]) for row in rows]
    except (OSError, TypeError, ValueError) as exc:
        raise ConnectionError("东方财富日线暂不可用，请检查网络后重试") from exc
    return sorted(bar for bar in bars if start.isoformat() <= bar[0] <= end.isoformat())


def average(values, period):
    return [None if i < period - 1 else sum(values[i-period+1:i+1]) / period
            for i in range(len(values))]


def ema(values, period):
    result = [values[0]]
    for value in values[1:]:
        result.append(result[-1] + 2 / (period + 1) * (value - result[-1]))
    return result


def rsi(values):
    result = [None] * len(values)
    up = down = 0
    for i in range(1, len(values)):
        delta = values[i] - values[i-1]
        gain, loss = max(delta, 0), max(-delta, 0)
        if i <= 14:
            up += gain / 14
            down += loss / 14
        else:
            up = (up * 13 + gain) / 14
            down = (down * 13 + loss) / 14
        if i >= 14:
            result[i] = 100 * up / (up + down) if up + down else 50
    return result


def cross(a, b, i, direction=1):
    return i > 0 and None not in (a[i-1], b[i-1], a[i], b[i]) and (
        direction * (a[i-1] - b[i-1]) <= 0 < direction * (a[i] - b[i])
    )


def custom_rule(rule):
    rule = re.sub(r"\s+", "", rule.strip()).replace("根", "").replace("天", "")
    match = re.fullmatch(r"(?:连续)?([一二两三四五六七八九十]|[1-9]\d?)连?([阳阴])(?:线)?(?:后)?(?:开始)?买入[。！!]?", rule)
    if not match:
        raise ValueError("支持输入「三连阳开始买入」或「连续 3 根阴线后买入」")
    number, side = match.groups()
    mapping = dict(zip("一二两三四五六七八九十", (1, 2, 2, 3, 4, 5, 6, 7, 8, 9, 10)))
    return int(number) if number.isdigit() else mapping[number], side


def signals(strategy, bars, rule):
    closes = [bar[2] for bar in bars]
    size = len(closes)
    if strategy == "ma":
        fast, slow = average(closes, 5), average(closes, 20)
        return [(cross(fast, slow, i), cross(fast, slow, i, -1)) for i in range(size)]
    if strategy == "macd":
        dif = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
        dea = ema(dif, 9)
        return [(i >= 34 and cross(dif, dea, i), i >= 34 and cross(dif, dea, i, -1)) for i in range(size)]
    if strategy == "rsi":
        series = rsi(closes)
        oversold, overbought = [30] * size, [70] * size
        return [(cross(series, oversold, i), cross(series, overbought, i)) for i in range(size)]
    if strategy == "boll":
        middle = average(closes, 20)
        lower = [None if mean is None else mean - 2 * math.sqrt(
            sum((value - mean) ** 2 for value in closes[i-19:i+1]) / 20)
            for i, mean in enumerate(middle)]
        return [(cross(closes, lower, i), cross(closes, middle, i)) for i in range(size)]
    count, side = rule
    streak = 0
    result = []
    for _, opening, closing, *_ in bars:
        matches = closing > opening if side == "阳" else closing < opening
        streak = streak + 1 if matches else 0
        result.append((streak == count, False))
    return result


def simulate(bars, start, end, strategy, fee, rule=None, hold=5):
    orders = signals(strategy, bars, rule)
    cash, shares, peak, drawdown = 1.0, 0.0, 1.0, 0.0
    entry = pending = None
    trades, equity = [], []
    for i, (day, opening, closing, *_rest) in enumerate(bars):
        if day < start:
            continue
        if day > end:
            break
        if pending == "buy":
            entry = (day, opening, cash, i)
            shares, cash = cash / (opening * (1 + fee)), 0.0
        elif pending == "sell":
            cash = shares * opening * (1 - fee)
            trades.append({"entry": entry[0], "exit": day, "buy": entry[1],
                           "sell": opening, "return": (cash / entry[2] - 1) * 100})
            shares, entry = 0.0, None
        nav = cash if entry is None else shares * closing
        peak = max(peak, nav)
        drawdown = max(drawdown, (1 - nav / peak) * 100)
        equity.append([day, round(nav, 6)])
        buy, sell = orders[i]
        pending = None
        if entry is None and buy:
            pending = "buy"
        if entry and (sell or (strategy == "custom" and i - entry[3] + 1 >= hold)):
            pending = "sell"
    wins = sum(trade["return"] > 0 for trade in trades)
    return {"id": strategy, "name": STRATEGIES[strategy][0], "equity": equity,
            "return": (nav - 1) * 100, "drawdown": drawdown, "trades": trades,
            "winRate": 100 * wins / len(trades) if trades else None, "open": entry is not None}


def backtest(body):
    code = symbol(body["code"])
    start, end = (date.fromisoformat(body[key]) for key in ("start", "end"))
    if end <= start or (end - start).days > 3653:
        raise ValueError("请选择有效的起止日期，区间最长约十年")
    fee = float(body.get("fee", 0.1)) / 100
    if not 0 <= fee <= .05:
        raise ValueError("单边费率须在 0% 到 5% 之间")
    strategies = list(dict.fromkeys(body["strategies"]))
    if not strategies or any(item not in STRATEGIES for item in strategies):
        raise ValueError("请选择已支持的策略")
    rule = custom_rule(body.get("rule", "")) if "custom" in strategies else None
    hold = int(body.get("hold", 5))
    if not 1 <= hold <= 250:
        raise ValueError("持有天数须在 1 到 250 之间")
    shanghai = datetime.now(SHANGHAI)
    last_complete = shanghai.date() - timedelta(days=int(shanghai.hour < 15))
    bars = online_history(code, start - timedelta(days=200), min(end, last_complete),
                          int(time.time() // 60))
    bars = [bar for bar in bars if bar[0] <= end.isoformat()]
    window = [bar for bar in bars if bar[0] >= start.isoformat()]
    if len(window) < 2:
        raise ValueError("所选区间的日线不足两条，无法回测")
    results = [simulate(bars, start.isoformat(), end.isoformat(), strategy, fee, rule, hold)
               for strategy in strategies]
    return {"results": results, "source": "东方财富日线",
            "days": len(window), "first": window[0][0], "last": window[-1][0],
            "bars": [[bar[0], bar[2]] for bar in window]}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT / "public", **kwargs)

    def send_json(self, result, status=200):
        content = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != "/api/quotes":
            return super().do_GET()
        try:
            values = parse_qs(url.query).get("codes", [""])[0].split(",")
            if not 1 <= len(values) <= 30:
                raise ValueError("一次最多查询 30 个代码")
            codes = list(dict.fromkeys(map(symbol, values)))
            self.send_json({"quotes": quotes(codes)})
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except ConnectionError as exc:
            self.send_json({"error": str(exc)}, 502)

    def do_POST(self):
        if self.path != "/api/backtest":
            return self.send_json({"error": "接口不存在"}, 404)
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 8192:
                return self.send_json({"error": "请求为空或请求体过大"}, 413)
            self.send_json(backtest(json.loads(self.rfile.read(size))))
        except (ValueError, KeyError, TypeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except ConnectionError as exc:
            self.send_json({"error": str(exc)}, 502)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    print(f"GainMoney · http://{args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
