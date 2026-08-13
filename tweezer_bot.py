"""
TWEEZER BOLLINGER BOT
======================
Theo dõi các cặp coin trên Binance, phát hiện mô hình nến:
  - Tweezer Top   (2 nến: XANH -> ĐỎ, đỉnh bằng nhau, chạm/xuyên Dải Trên Bollinger)
  - Tweezer Bottom(2 nến: ĐỎ -> XANH, đáy bằng nhau, chạm/xuyên Dải Dưới Bollinger)
Khi phát hiện -> gửi cảnh báo ngay lập tức qua Telegram Bot.

CÁCH CHẠY
---------
1. pip install -r requirements.txt
2. Điền TELEGRAM_BOT_TOKEN và TELEGRAM_CHAT_ID bên dưới (xem README.md để biết cách lấy)
3. python tweezer_bot.py
"""

import time
import json
import os
import sys
import requests
from datetime import datetime, timezone

# ============================================================
# CẤU HÌNH - CHỈNH SỬA CÁC GIÁ TRỊ NÀY
# ============================================================

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "DÁN_TOKEN_BOT_CỦA_BẠN_VÀO_ĐÂY")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "DÁN_CHAT_ID_CỦA_BẠN_VÀO_ĐÂY")

# --- Danh sách coin theo dõi (cặp với USDT) ---
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "NEARUSDT",
    "LINKUSDT", "DOTUSDT", "DOGEUSDT", "FETUSDT", "TRXUSDT",
    "ADAUSDT", "ICPUSDT", "ATOMUSDT",
]

# --- Khung thời gian theo dõi (định dạng Binance) ---
INTERVALS = ["15m", "30m", "1h", "4h"]

# --- Cấu hình Bollinger Bands ---
BB_PERIOD = 20     # số nến dùng để tính SMA/độ lệch chuẩn
BB_STD = 2         # số lần độ lệch chuẩn

# --- Dung sai để coi THÂN 2 nến "có độ dài tương đương nhau" ---
# So sánh THÂN NẾN (|Close - Open|), đúng như mô tả trực quan của mô hình Tweezer — KHÔNG dùng
# bóng nến (wick) để so sánh, vì 1 nến doji (thân gần như bằng 0, chỉ có bóng dài) không được tính
# là "tương đương" với 1 nến thân đặc dù bóng nến của nó có thể dài tương đương.
# 0.35 = thân nến 2 được phép lệch tối đa 35% so với thân nến 1.
BODY_LENGTH_TOLERANCE = 0.35

# --- Thân nến tối thiểu để được tính là nến "có thân" (loại doji/nến gần như không thân) ---
# 0.0004 = 0.04% giá. Nến có thân nhỏ hơn mức này (gần như chữ thập +) sẽ KHÔNG được tính vào mô hình Tweezer.
MIN_BODY_RATIO = 0.0004

# --- Dung sai để coi 2 đỉnh/đáy là "bằng nhau" ---
# 0.0015 = 0.15% chênh lệch giá vẫn được tính là bằng nhau (do giá coin biến động liên tục,
# rất hiếm khi đỉnh/đáy trùng khớp tuyệt đối)
WICK_EQUAL_TOLERANCE = 0.0015

# --- Vùng đệm quanh dải Bollinger để tính là "chạm/xuyên dải" ---
# 0.00005 = 0.005%: gần như bắt buộc nến phải thực sự chạm hoặc xuyên qua dải mới được tính,
# chỉ chừa sai số cực nhỏ cho làm tròn số thập phân. (Trước đây để 0.1% khiến nến chưa thực sự
# chạm dải — chỉ ở gần — vẫn được tính là "chạm", gây báo hiệu sai.)
BAND_TOUCH_BUFFER = 0.00005

# --- Khoảng cách tối thiểu bắt buộc giữa Entry (giá đóng cửa nến xác nhận) và TP (Middle Band) ---
# Đảm bảo tại THỜI ĐIỂM cảnh báo, giá vẫn còn nằm ở phía dải đã chạm (chưa hồi/bật ngược lên gần
# hoặc vượt qua Middle Band). Nếu không có điều kiện này, nến xác nhận có thể đã đóng cửa rất gần
# hoặc vượt qua Middle Band trước khi cảnh báo được gửi đi — khiến TP hiển thị sai phía so với Entry.
# 0.0008 = 0.08%: TP phải cách Entry tối thiểu 0.08% theo đúng hướng kỳ vọng.
MIN_TP_DISTANCE_PCT = 0.0008

# --- Chu kỳ quét lặp lại (giây) ---
POLL_INTERVAL_SECONDS = 30

# --- File lưu trạng thái để tránh gửi trùng cảnh báo khi restart script ---
STATE_FILE = "alert_state.json"

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# ============================================================
# TIỆN ÍCH
# ============================================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send_telegram_alert(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, data=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[LỖI TELEGRAM] {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[LỖI TELEGRAM] {e}")


def fetch_klines(symbol: str, interval: str, limit: int = 60):
    """Lấy dữ liệu nến từ Binance. Trả về list các dict đã parse."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
    resp.raise_for_status()
    raw = resp.json()

    candles = []
    for k in raw:
        candles.append({
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "close_time": k[6],
        })
    return candles


def compute_bollinger(closes):
    """Tính SMA và dải Bollinger cho danh sách giá đóng cửa (list float)."""
    n = len(closes)
    if n < BB_PERIOD:
        return None, None, None
    window = closes[-BB_PERIOD:]
    sma = sum(window) / BB_PERIOD
    variance = sum((c - sma) ** 2 for c in window) / BB_PERIOD
    std = variance ** 0.5
    upper = sma + BB_STD * std
    lower = sma - BB_STD * std
    return upper, sma, lower


def is_close_enough(a, b, tolerance):
    ref = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / ref <= tolerance


def body_len(candle):
    return abs(candle["close"] - candle["open"])


def candle_range(candle):
    """Toàn bộ chiều dài nến, từ đỉnh bóng trên đến đáy bóng dưới."""
    return candle["high"] - candle["low"]


def is_green(candle):
    return candle["close"] > candle["open"]


def is_red(candle):
    return candle["close"] < candle["open"]


# ============================================================
# LOGIC PHÁT HIỆN MÔ HÌNH
# ============================================================

def check_tweezer_patterns(symbol, interval, candles):
    """
    candles: danh sách nến đã ĐÓNG (không gồm nến đang chạy), sắp xếp tăng dần theo thời gian.
    Kiểm tra 2 nến cuối cùng (candle1 = nến trước, candle2 = nến gần nhất đã đóng).
    Trả về (pattern_name, candle2) nếu phát hiện, ngược lại None.
    """
    if len(candles) < BB_PERIOD + 2:
        return None

    candle1 = candles[-2]
    candle2 = candles[-1]

    closes_up_to_candle2 = [c["close"] for c in candles]
    upper, mid, lower = compute_bollinger(closes_up_to_candle2)
    if upper is None:
        return None

    b1 = body_len(candle1)
    b2 = body_len(candle2)

    # Loại bỏ nến doji / gần như không có thân (chỉ có bóng dài) khỏi mô hình
    ref_price = candle2["close"]
    if b1 < ref_price * MIN_BODY_RATIO or b2 < ref_price * MIN_BODY_RATIO:
        return None

    bodies_equal = is_close_enough(b1, b2, BODY_LENGTH_TOLERANCE)

    # --- TWEEZER TOP: nến1 XANH, nến2 ĐỎ, đỉnh bằng nhau, chạm dải trên ---
    # ĐIỀU KIỆN BẮT BUỘC: (1) thân 2 nến dài tương đương nhau  (2) chạm dải trên
    # (3) giá đóng cửa nến xác nhận vẫn còn cách Middle Band đủ xa (chưa hồi về giữa dải)
    if is_green(candle1) and is_red(candle2):
        highs_equal = is_close_enough(candle1["high"], candle2["high"], WICK_EQUAL_TOLERANCE)
        touches_upper = max(candle1["high"], candle2["high"]) >= upper * (1 - BAND_TOUCH_BUFFER)
        entry = candle2["close"]
        tp_distance_ok = (entry - mid) / entry >= MIN_TP_DISTANCE_PCT  # entry còn cao hơn mid đủ xa
        if highs_equal and bodies_equal and touches_upper and tp_distance_ok:
            return "TWEEZER TOP", candle1, candle2, upper, mid, lower

    # --- TWEEZER BOTTOM: nến1 ĐỎ, nến2 XANH, đáy bằng nhau, chạm dải dưới ---
    # ĐIỀU KIỆN BẮT BUỘC: (1) thân 2 nến dài tương đương nhau  (2) chạm dải dưới
    # (3) giá đóng cửa nến xác nhận vẫn còn cách Middle Band đủ xa (chưa hồi về giữa dải)
    if is_red(candle1) and is_green(candle2):
        lows_equal = is_close_enough(candle1["low"], candle2["low"], WICK_EQUAL_TOLERANCE)
        touches_lower = min(candle1["low"], candle2["low"]) <= lower * (1 + BAND_TOUCH_BUFFER)
        entry = candle2["close"]
        tp_distance_ok = (mid - entry) / entry >= MIN_TP_DISTANCE_PCT  # entry còn thấp hơn mid đủ xa
        if lows_equal and bodies_equal and touches_lower and tp_distance_ok:
            return "TWEEZER BOTTOM", candle1, candle2, upper, mid, lower

    return None


def format_alert(symbol, interval, pattern_name, candle1, candle2, upper, mid, lower):
    ts = datetime.fromtimestamp(candle2["close_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    entry = candle2["close"]
    # Chốt lời mục tiêu đầu tiên = Dải giữa (Middle Band / SMA20)
    tp = mid
    tp_pct = (tp - entry) / entry * 100

    if pattern_name == "TWEEZER TOP":
        emoji = "🔴📉"
        signal = "ĐẢO CHIỀU TĂNG → GIẢM (Cân nhắc SHORT/BÁN)"
        band_label = f"Dải trên (Upper Band): {upper:.4f}"
        # Short: TP nằm dưới entry -> % thay đổi phải ra số âm (lãi cho lệnh short)
        sl = max(candle1["high"], candle2["high"])
    else:
        emoji = "🟢📈"
        signal = "ĐẢO CHIỀU GIẢM → TĂNG (Cân nhắc LONG/MUA)"
        band_label = f"Dải dưới (Lower Band): {lower:.4f}"
        sl = min(candle1["low"], candle2["low"])

    msg = (
        f"{emoji} <b>{pattern_name}</b> phát hiện!\n\n"
        f"<b>Coin:</b> {symbol}\n"
        f"<b>Khung thời gian:</b> {interval}\n"
        f"<b>Thời gian nến đóng:</b> {ts}\n\n"
        f"<b>Nến 1:</b> O={candle1['open']:.4f} H={candle1['high']:.4f} L={candle1['low']:.4f} C={candle1['close']:.4f}\n"
        f"<b>Nến 2:</b> O={candle2['open']:.4f} H={candle2['high']:.4f} L={candle2['low']:.4f} C={candle2['close']:.4f}\n\n"
        f"{band_label}\n"
        f"Dải giữa (SMA{BB_PERIOD}): {mid:.4f}\n\n"
        f"<b>Tín hiệu:</b> {signal}\n\n"
        f"📍 <b>Điểm vào lệnh (Entry):</b> {entry:.4f}\n"
        f"🎯 <b>Chốt lời (TP) - tại Middle Band:</b> {tp:.4f} ({tp_pct:+.2f}%)\n"
        f"🛡 <b>Dừng lỗ tham khảo (SL) - đỉnh/đáy tweezer:</b> {sl:.4f}\n\n"
        f"<i>* TP tại Middle Band là mục tiêu chốt lời ĐẦU TIÊN. Nếu giá phá qua Middle Band "
        f"với lực mạnh, có thể dời TP tiếp theo về phía dải đối diện. SL đặt tham khảo ngay "
        f"ngoài đỉnh/đáy của mô hình tweezer.</i>"
    )
    return msg


# ============================================================
# VÒNG LẶP CHÍNH
# ============================================================

def main():
    if "DÁN_TOKEN" in TELEGRAM_BOT_TOKEN or "DÁN_CHAT_ID" in TELEGRAM_CHAT_ID:
        print("⚠️  Bạn chưa cấu hình TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
        print("    Xem README.md để biết cách lấy token và chat_id.")

def run_one_scan_pass(state):
    """Quét 1 lượt toàn bộ SYMBOLS x INTERVALS, gửi cảnh báo cho tín hiệu mới, cập nhật state."""
    for symbol in SYMBOLS:
        for interval in INTERVALS:
            key = f"{symbol}_{interval}"
            try:
                candles = fetch_klines(symbol, interval, limit=BB_PERIOD + 10)
            except Exception as e:
                print(f"[LỖI FETCH] {symbol} {interval}: {e}")
                continue

            if len(candles) < 3:
                continue

            # Nến cuối cùng trả về có thể đang chạy (chưa đóng) -> bỏ qua
            now_ms = int(time.time() * 1000)
            closed_candles = [c for c in candles if c["close_time"] <= now_ms]

            if len(closed_candles) < BB_PERIOD + 2:
                continue

            result = check_tweezer_patterns(symbol, interval, closed_candles)
            if result is None:
                continue

            pattern_name, candle1, candle2, upper, mid, lower = result
            alert_key = f"{key}_{pattern_name}"
            last_alerted_time = state.get(alert_key)

            # Chỉ gửi cảnh báo nếu đây là nến mới, chưa từng cảnh báo trước đó
            if last_alerted_time == candle2["open_time"]:
                continue

            msg = format_alert(symbol, interval, pattern_name, candle1, candle2, upper, mid, lower)
            send_telegram_alert(msg)
            print(f"[CẢNH BÁO ĐÃ GỬI] {symbol} {interval} - {pattern_name}")

            state[alert_key] = candle2["open_time"]
            save_state(state)


def main():
    if "DÁN_TOKEN" in TELEGRAM_BOT_TOKEN or "DÁN_CHAT_ID" in TELEGRAM_CHAT_ID:
        print("⚠️  Bạn chưa cấu hình TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
        print("    Xem README.md để biết cách lấy token và chat_id.")

    state = load_state()

    # SINGLE_RUN=1 (hoặc tham số --once): quét đúng 1 lượt rồi thoát — dùng cho GitHub Actions
    # (chạy miễn phí theo lịch cron, không cần máy/VPS bật liên tục).
    single_run = os.environ.get("SINGLE_RUN") == "1" or "--once" in sys.argv

    if single_run:
        print(f"[SINGLE_RUN] Quét 1 lượt {len(SYMBOLS)} coin x {len(INTERVALS)} khung thời gian…")
        run_one_scan_pass(state)
        print("[SINGLE_RUN] Hoàn tất.")
        return

    print(f"Bắt đầu theo dõi {len(SYMBOLS)} coin trên {len(INTERVALS)} khung thời gian...")
    print(f"Coins: {', '.join(SYMBOLS)}")
    print(f"Intervals: {', '.join(INTERVALS)}")

    while True:
        run_one_scan_pass(state)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
