# -*- coding: utf-8 -*-
"""
qimen_cron_worker.py
===================
Фоновый обработчик очереди задач Крона и Отслеживания результатов Даосского Ци Мэнь.
Работает как в облаке (GitHub Actions каждые 5 мин), так и локально на ПК (--once или --daemon):
1. За 2м (10м) / 15м (час) / 20:45 (день) отправляет лаконичный алерт в Telegram.
   Если включен чекбокс «График входа», вместе с алертом отправляет актуальный свечной график Bybit.
2. Через +2ч (10м) / +6ч (час) / +12ч (день) отправляет итоговый свечной график с результатом (% изменения).
3. Автоматически извлекает токен бота из настроек задач, файла .env или переменных окружения.
"""

import sys
import os
import io
import time
import argparse
import requests
import threading
import http.server
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageDraw, ImageFont

DEFAULT_FIREBASE_URL = 'https://constellation-e9fcd-default-rtdb.europe-west1.firebasedatabase.app/qimen_cron_tasks.json'

def load_dotenv():
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_file):
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip("'").strip('"')
                    if k and v and k not in os.environ:
                        os.environ[k] = v

load_dotenv()

def parse_iso_dt(dt_str):
    if not dt_str:
        return None
    try:
        clean_str = dt_str.replace('Z', '+00:00')
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception as e:
        print(f"Date parse error for {dt_str}: {e}")
        return None

def send_telegram_message(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        print(f"Telegram bot token or chat ID missing (token: {'SET' if bot_token else 'EMPTY'}, chat: {chat_id}). Skipping alert.")
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML'
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        res = r.json()
        if res.get('ok'):
            print(f"Telegram text alert sent to {chat_id}")
            return True
        else:
            print(f"Telegram send error: {res}")
            return False
    except Exception as e:
        print(f"Telegram request failed: {e}")
        return False

def send_telegram_photo(bot_token, chat_id, photo_bytes, caption):
    if not bot_token or not chat_id:
        print(f"Telegram bot token or chat ID missing. Skipping photo.")
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    data = {
        'chat_id': chat_id,
        'caption': caption,
        'parse_mode': 'HTML'
    }
    files = {
        'photo': ('chart.jpg', photo_bytes, 'image/jpeg')
    }
    try:
        r = requests.post(url, data=data, files=files, timeout=30)
        res = r.json()
        if res.get('ok'):
            print(f"Telegram photo sent to {chat_id}")
            return True
        else:
            print(f"Telegram sendPhoto error: {res}")
            return False
    except Exception as e:
        print(f"Telegram photo request failed: {e}")
        return False

def fetch_bybit_klines(symbol, interval, start_dt, end_dt):
    coin_clean = symbol.upper().replace('USDT', '')
    symbol_usdt = coin_clean + 'USDT'
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    url = 'https://api.bybit.com/v5/market/kline'
    params = {
        'category': 'linear',
        'symbol': symbol_usdt,
        'interval': interval,
        'start': start_ms,
        'end': end_ms,
        'limit': 1000
    }
    raw_list = []
    try:
        r = requests.get(url, params=params, timeout=12)
        data = r.json()
        raw_list = data.get('result', {}).get('list', [])
        if not raw_list:
            params['category'] = 'spot'
            r = requests.get(url, params=params, timeout=12)
            data = r.json()
            raw_list = data.get('result', {}).get('list', [])
    except Exception as e:
        print(f"Bybit API request failed for {symbol_usdt}: {e}")
        return []

    klines = []
    for item in reversed(raw_list):
        try:
            klines.append({
                'time': int(item[0]),
                'open': float(item[1]),
                'high': float(item[2]),
                'low': float(item[3]),
                'close': float(item[4]),
                'volume': float(item[5])
            })
        except (ValueError, IndexError):
            continue
    return klines

def get_task_tz(task):
    tz_offset_min = task.get('tzOffset')
    if tz_offset_min is not None:
        try:
            return timezone(timedelta(minutes=-int(tz_offset_min)))
        except Exception:
            pass
    return timezone(timedelta(hours=3))

def render_candlestick_chart_bytes(klines, coin, pillar_name, timeframe_name, target_dt, is_entry_chart=False, direction=None, user_tz=None):
    if not klines:
        return None

    if user_tz is None:
        user_tz = timezone(timedelta(hours=3))

    width = 1200
    height = 720
    im = Image.new('RGB', (width, height), color=(15, 23, 42)) # Slate 900
    draw = ImageDraw.Draw(im)

    try:
        font_title = ImageFont.truetype("arial.ttf", 26)
        font_bold = ImageFont.truetype("arialbd.ttf", 20)
        font_main = ImageFont.truetype("arial.ttf", 16)
        font_sm = ImageFont.truetype("arial.ttf", 13)
    except Exception:
        font_title = font_bold = font_main = font_sm = ImageFont.load_default()

    x_left = 40
    x_right = 1080
    y_top = 100
    y_bottom = 660

    chart_w = x_right - x_left
    chart_h = y_bottom - y_top

    highs = [k['high'] for k in klines]
    lows = [k['low'] for k in klines]
    min_p = min(lows)
    max_p = max(highs)
    p_span = max_p - min_p
    if p_span == 0:
        p_span = max_p * 0.01 or 1.0

    p_margin = p_span * 0.05
    y_min_p = min_p - p_margin
    y_max_p = max_p + p_margin
    p_span_total = y_max_p - y_min_p

    def p_to_y(p):
        ratio = (p - y_min_p) / p_span_total
        return int(y_bottom - ratio * chart_h)

    # Горизонтальная ценовая сетка
    grid_steps = 6
    for i in range(grid_steps + 1):
        p_val = y_min_p + (p_span_total / grid_steps) * i
        y_val = p_to_y(p_val)
        draw.line([(x_left, y_val), (x_right, y_val)], fill=(30, 41, 59), width=1)
        p_str = f"{p_val:,.4f}" if p_val < 10 else (f"{p_val:,.2f}" if p_val < 1000 else f"{p_val:,.1f}")
        draw.text((x_right + 10, y_val - 8), p_str, fill=(148, 163, 184), font=font_sm)

    n_candles = len(klines)
    bar_pitch = chart_w / max(n_candles, 1)
    bar_width = max(int(bar_pitch * 0.7), 2)

    target_ms = int(target_dt.timestamp() * 1000)
    target_idx = None
    min_diff = float('inf')

    # Временная сетка
    time_step = max(n_candles // 7, 1)
    for i in range(0, n_candles, time_step):
        k = klines[i]
        x_center = int(x_left + (i + 0.5) * bar_pitch)
        draw.line([(x_center, y_top), (x_center, y_bottom)], fill=(30, 41, 59), width=1)
        dt_k = datetime.fromtimestamp(k['time'] / 1000, tz=user_tz)
        t_str = dt_k.strftime("%H:%M")
        draw.text((x_center - 18, y_bottom + 8), t_str, fill=(148, 163, 184), font=font_sm)

    # Отрисовка свечей
    for i, k in enumerate(klines):
        x_center = int(x_left + (i + 0.5) * bar_pitch)
        y_open = p_to_y(k['open'])
        y_close = p_to_y(k['close'])
        y_high = p_to_y(k['high'])
        y_low = p_to_y(k['low'])

        diff = abs(k['time'] - target_ms)
        if diff < min_diff:
            min_diff = diff
            target_idx = i

        is_bull = k['close'] >= k['open']
        color = (16, 185, 129) if is_bull else (244, 63, 94)

        # Фитиль
        draw.line([(x_center, y_high), (x_center, y_low)], fill=color, width=1)
        # Тело
        b_top = min(y_open, y_close)
        b_bottom = max(y_open, y_close)
        if b_bottom - b_top < 2:
            b_bottom = b_top + 2
        half_w = bar_width // 2
        draw.rectangle([(x_center - half_w, b_top), (x_center + half_w, b_bottom)], fill=color, outline=color)

    # Метка старта столпа
    entry_price = None
    if target_idx is not None:
        target_k = klines[target_idx]
        entry_price = target_k['open']
        x_target = int(x_left + (target_idx + 0.5) * bar_pitch)
        draw.line([(x_target, y_top), (x_target, y_bottom)], fill=(6, 182, 212), width=2)
        local_target = target_dt.astimezone(user_tz) if target_dt.tzinfo else target_dt
        badge_text = f"🎯 СТАРТ: {local_target.strftime('%H:%M')} [{pillar_name}]"
        draw.rectangle([(x_target - 80, y_top - 24), (x_target + 80, y_top - 4)], fill=(6, 182, 212), outline=(14, 116, 144))
        draw.text((x_target - 75, y_top - 22), badge_text, fill=(15, 23, 42), font=font_sm)

    latest_k = klines[-1]
    exit_price = latest_k['close']
    ref_price = entry_price if entry_price else klines[0]['open']
    pct_change = ((exit_price - ref_price) / ref_price) * 100.0

    # Шапка графика
    dir_badge = f" [{direction}]" if direction else ""
    draw.rectangle([(x_left, 20), (x_left + 180, 60)], fill=(30, 41, 59), outline=(51, 65, 85))
    draw.text((x_left + 14, 26), f"{coin} / USDT{dir_badge}", fill=(255, 255, 255), font=font_bold)

    p_badge_w = 220
    draw.rectangle([(x_left + 195, 20), (x_left + 195 + p_badge_w, 60)], fill=(30, 58, 138), outline=(59, 130, 246))
    title_label = "График входа" if is_entry_chart else "Итог"
    draw.text((x_left + 205, 26), f"{title_label}: {pillar_name} ({timeframe_name})", fill=(191, 219, 254), font=font_bold)

    stats_x = x_left + 440
    change_color = (16, 185, 129) if pct_change >= 0 else (244, 63, 94)
    sign = "+" if pct_change >= 0 else ""
    change_str = f"{sign}{pct_change:.2f}%"

    draw.text((stats_x, 18), "Динамика от старта:" if not is_entry_chart else "Текущая цена:", fill=(148, 163, 184), font=font_sm)
    draw.text((stats_x, 34), change_str if not is_entry_chart else f"{exit_price:,.4f}", fill=change_color if not is_entry_chart else (255, 255, 255), font=font_title)

    draw.text((stats_x + 190, 18), "Вход ➔ Итог:", fill=(148, 163, 184), font=font_sm)
    p_info = f"{ref_price:,.2f} ➔ {exit_price:,.2f}" if ref_price > 10 else f"{ref_price:,.4f} ➔ {exit_price:,.4f}"
    draw.text((stats_x + 190, 36), p_info, fill=(241, 245, 249), font=font_bold)

    draw.text((stats_x + 400, 18), "High / Low диапазона:", fill=(148, 163, 184), font=font_sm)
    hl_info = f"H: {max_p:,.2f}  L: {min_p:,.2f}" if max_p > 10 else f"H: {max_p:,.4f}  L: {min_p:,.4f}"
    draw.text((stats_x + 400, 36), hl_info, fill=(203, 213, 225), font=font_bold)

    buf = io.BytesIO()
    im.save(buf, format='JPEG', quality=92)
    buf.seek(0)
    return buf.getvalue(), pct_change, ref_price, exit_price, min_p, max_p

def process_cron_tasks(bot_token, default_chat_id, firebase_url=None):
    if not firebase_url or not str(firebase_url).startswith('http'):
        firebase_url = DEFAULT_FIREBASE_URL
    if not firebase_url.endswith('.json'):
        firebase_url = firebase_url.rstrip('/') + '/qimen_cron_tasks.json'

    print(f"[{datetime.now(timezone.utc).isoformat()}] Fetching tasks from {firebase_url}...")
    try:
        r = requests.get(firebase_url, timeout=12)
        if r.status_code != 200 or not r.text or r.text.strip() == 'null':
            print("No tasks found in Firebase.")
            return 0
        tasks_dict = r.json()
    except Exception as e:
        print(f"Error fetching Firebase tasks: {e}")
        return 0

    if not isinstance(tasks_dict, dict):
        return 0

    # Если бот-токен не передан через аргументы или окружение, ищем его в самих задачах
    if not bot_token:
        for t in tasks_dict.values():
            if isinstance(t, dict) and t.get('botToken'):
                bot_token = t['botToken']
                print(f"Automatically detected Telegram Bot Token from task settings.")
                break

    now_utc = datetime.now(timezone.utc)
    updated_count = 0

    tf_labels = {
        'minute': '10-минутка',
        'hour': '2-часовка',
        'day': 'День'
    }

    base_rtdb = firebase_url.replace('.json', '')

    records_cache = None
    def get_record_tg_link(r_id):
        nonlocal records_cache
        if not r_id:
            return None
        if records_cache is None:
            try:
                rec_url = firebase_url.replace('qimen_cron_tasks.json', 'qimen_records.json')
                r_rec = requests.get(rec_url, timeout=10)
                records_cache = {}
                if r_rec.status_code == 200 and r_rec.text and r_rec.text.strip() != 'null':
                    records_data = r_rec.json()
                    if isinstance(records_data, list):
                        for item in records_data:
                            if isinstance(item, dict) and item.get('id'):
                                records_cache[str(item['id'])] = item.get('tg_post_url')
                    elif isinstance(records_data, dict):
                        for item in records_data.values():
                            if isinstance(item, dict) and item.get('id'):
                                records_cache[str(item['id'])] = item.get('tg_post_url')
            except Exception as e:
                print(f"Warning: could not fetch records for tg_post_url: {e}")
                records_cache = {}
        return records_cache.get(str(r_id))

    for task_id, task in tasks_dict.items():
        if not isinstance(task, dict):
            continue

        coin = task.get('coin', 'BTC')
        direction = task.get('direction', '')
        target_pillar = task.get('targetPillar', '')
        timeframe = task.get('timeframe', 'minute')
        tf_label = tf_labels.get(timeframe, timeframe)
        chat_id = default_chat_id or task.get('chatId')
        cur_bot_token = task.get('botToken') or bot_token
        note = task.get('note', '')

        target_dt = parse_iso_dt(task.get('targetTime'))
        alert_dt = parse_iso_dt(task.get('alertTime'))
        track_dt = parse_iso_dt(task.get('trackTime'))

        if not target_dt:
            continue

        user_tz = get_task_tz(task)
        local_target_dt = target_dt.astimezone(user_tz) if target_dt.tzinfo else target_dt
        target_str = local_target_dt.strftime('%d.%m %H:%M')

        task_patch = {}

        # 1. ПРОВЕРКА АЛЕРТА
        if task.get('alertStatus') == 'pending':
            if alert_dt and now_utc >= alert_dt:
                diff_seconds = (target_dt - now_utc).total_seconds()
                if diff_seconds > 0:
                    diff_min = max(int(diff_seconds / 60), 1)
                    time_info = f"через ~{diff_min} мин"
                else:
                    past_min = int(abs(diff_seconds) / 60)
                    time_info = f"наступил ~{past_min} мин назад" if past_min > 0 else "наступил только что"

                dir_str = f" {direction}" if direction else ""
                lines = [
                    f"🔔 <b>[КРОН СИГНАЛ] {coin}{dir_str}</b>\n",
                    f"🎯 Столп: <b>{target_pillar}</b> ({tf_label})",
                    f"⏰ Время начала: <b>{target_str}</b> ({time_info})"
                ]
                if task.get('isMerge'):
                    orig_stem = task.get('selectedStem', '')
                    lines.append(f"🔄 Слияние: от <b>{orig_stem}</b> к <b>{target_pillar[0]}</b>")
                reading_id = task.get('readingId')
                source_tg_link = task.get('sourceTgLink')
                if not source_tg_link and reading_id:
                    source_tg_link = get_record_tg_link(reading_id)
                if reading_id and source_tg_link:
                    lines.append(f'📋 Расклад: <b>#{reading_id}</b> (<a href="{source_tg_link}">Открыть в TG</a>)')
                elif reading_id:
                    lines.append(f"📋 Расклад: <b>#{reading_id}</b>")
                elif source_tg_link:
                    lines.append(f'🔗 <a href="{source_tg_link}">Открыть исходный расклад в TG</a>')
                if note:
                    lines.append(f"📝 Заметка: <i>{note}</i>")
                alert_text = "\n".join(lines)

                # Проверяем, нужен ли график входа вместе с алертом
                if task.get('entryChartEnabled'):
                    print(f"Generating entry chart for {coin} [{target_pillar}]...")
                    interval = '1' if timeframe == 'minute' else ('5' if timeframe == 'hour' else '15')
                    start_dt = target_dt - timedelta(minutes=45 if timeframe == 'minute' else 120)
                    candles = fetch_bybit_klines(coin, interval, start_dt, now_utc)
                    if candles:
                        chart_res = render_candlestick_chart_bytes(candles, coin, target_pillar, tf_label, target_dt, is_entry_chart=True, direction=direction, user_tz=user_tz)
                        if chart_res:
                            img_bytes = chart_res[0]
                            success = send_telegram_photo(cur_bot_token, chat_id, img_bytes, alert_text)
                            if success:
                                task_patch['alertStatus'] = 'sent'
                                task_patch['entryChartStatus'] = 'sent'
                                task_patch['alertSentAt'] = now_utc.isoformat()
                                updated_count += 1
                        else:
                            success = send_telegram_message(cur_bot_token, chat_id, alert_text)
                            if success:
                                task_patch['alertStatus'] = 'sent'
                                task_patch['alertSentAt'] = now_utc.isoformat()
                                updated_count += 1
                    else:
                        success = send_telegram_message(cur_bot_token, chat_id, alert_text)
                        if success:
                            task_patch['alertStatus'] = 'sent'
                            task_patch['alertSentAt'] = now_utc.isoformat()
                            updated_count += 1
                else:
                    success = send_telegram_message(cur_bot_token, chat_id, alert_text)
                    if success:
                        task_patch['alertStatus'] = 'sent'
                        task_patch['alertSentAt'] = now_utc.isoformat()
                        updated_count += 1

        # 2. ПРОВЕРКА ИТОГОВОГО ГРАФИКА
        if task.get('trackStatus') == 'pending':
            if track_dt and now_utc >= track_dt:
                print(f"Generating post-factum chart for {coin} [{target_pillar}]...")
                if timeframe == 'minute':
                    interval = '1'
                    start_dt = target_dt - timedelta(minutes=30)
                elif timeframe == 'hour':
                    interval = '5'
                    start_dt = target_dt - timedelta(hours=1)
                else:
                    interval = '15'
                    start_dt = target_dt - timedelta(hours=4)

                end_dt = now_utc
                candles = fetch_bybit_klines(coin, interval, start_dt, end_dt)

                if candles:
                    chart_res = render_candlestick_chart_bytes(candles, coin, target_pillar, tf_label, target_dt, is_entry_chart=False, direction=direction, user_tz=user_tz)
                    if chart_res:
                        img_bytes, pct_change, p_entry, p_exit, p_low, p_high = chart_res
                        sign = "+" if pct_change >= 0 else ""
                        target_str = local_target_dt.strftime('%d.%m %H:%M')
                        dir_str = f" {direction}" if direction else ""
                        lines = [
                            f"📊 <b>[ОТСЛЕЖИВАНИЕ РЕЗУЛЬТАТА] {coin}{dir_str}</b>\n",
                            f"🎯 Столп: <b>{target_pillar}</b> ({tf_label})",
                            f"⏰ Время старта: <b>{target_str}</b>",
                            f"📈 Итог: <b>{sign}{pct_change:.2f}%</b> (Вход: {p_entry:,.4f} ➔ Выход: {p_exit:,.4f})",
                            f"↕ High: {p_high:,.4f} | Low: {p_low:,.4f}"
                        ]
                        reading_id = task.get('readingId')
                        source_tg_link = task.get('sourceTgLink')
                        if not source_tg_link and reading_id:
                            source_tg_link = get_record_tg_link(reading_id)
                        if reading_id and source_tg_link:
                            lines.append(f'📋 Расклад: <b>#{reading_id}</b> (<a href="{source_tg_link}">Открыть в TG</a>)')
                        elif reading_id:
                            lines.append(f"📋 Расклад: <b>#{reading_id}</b>")
                        elif source_tg_link:
                            lines.append(f'🔗 <a href="{source_tg_link}">Открыть исходный расклад в TG</a>')
                        if note:
                            lines.append(f"📝 Заметка: <i>{note}</i>")
                        caption = "\n".join(lines)
                        success = send_telegram_photo(cur_bot_token, chat_id, img_bytes, caption)
                        if success:
                            task_patch['trackStatus'] = 'sent'
                            task_patch['trackSentAt'] = now_utc.isoformat()
                            task_patch['trackResultPct'] = pct_change
                            updated_count += 1
                else:
                    print(f"Warning: No candles fetched for {coin}")

        if task_patch:
            patch_url = f"{base_rtdb}/{task_id}.json"
            try:
                requests.patch(patch_url, json=task_patch, timeout=10)
                print(f"Task {task_id} updated in Firebase: {task_patch}")
            except Exception as e:
                print(f"Failed to patch task {task_id} in Firebase: {e}")

    return updated_count

def main():
    parser = argparse.ArgumentParser(description="Qimen Cron Worker for Alerts and Chart Tracking")
    parser.add_argument('--once', action='store_true', help="Run one pass and exit")
    parser.add_argument('--daemon', action='store_true', help="Run in continuous loop")
    parser.add_argument('--web', action='store_true', help="Run web health check server with background daemon loop")
    parser.add_argument('--port', type=int, default=None, help="Port for web server (defaults to PORT env or 10000)")
    parser.add_argument('--interval', type=int, default=30, help="Loop interval in seconds")
    parser.add_argument('--token', type=str, default=None, help="Telegram Bot Token")
    parser.add_argument('--chat-id', type=str, default=None, help="Telegram Cron Chat ID")
    parser.add_argument('--firebase-url', type=str, default=None, help="Firebase RTDB URL")

    args = parser.parse_args()

    bot_token = (args.token or os.environ.get('TELEGRAM_BOT_TOKEN') or '').strip()
    chat_id = (args.chat_id or os.environ.get('CRON_CHAT_ID') or '').strip()
    raw_fb = (args.firebase_url or os.environ.get('FIREBASE_URL') or '').strip()
    firebase_url = raw_fb if (raw_fb and raw_fb.startswith('http')) else DEFAULT_FIREBASE_URL
    if not firebase_url.endswith('.json'):
        firebase_url = firebase_url.rstrip('/') + '/qimen_cron_tasks.json'

    env_port = os.environ.get('PORT')
    is_web_mode = args.web or (env_port is not None and not args.once)

    if args.once:
        print("Qimen Cron Worker initialized. Mode: ONCE")
        processed = process_cron_tasks(bot_token, chat_id, firebase_url)
        print(f"Done. Processed/updated tasks: {processed}")
    elif is_web_mode:
        port = int(env_port or args.port or 10000)
        print(f"Qimen Cron Worker initialized. Mode: WEB SERVICE (Port: {port})")

        class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                html = (
                    "<html><body style='font-family:sans-serif;padding:30px;text-align:center;background:#f8fafc;'>"
                    "<div style='max-width:420px;margin:auto;background:white;padding:24px;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,0.06);'>"
                    "<h2 style='color:#16a34a;margin-top:0;'>🟢 Qimen Cron Worker is Alive</h2>"
                    f"<p style='color:#64748b;font-size:14px;'>Continuous background loop polling Firebase every {args.interval}s.</p>"
                    "</div></body></html>"
                )
                self.wfile.write(html.encode('utf-8'))

            def log_message(self, format, *args_log):
                return

        def daemon_loop():
            print(f"Background daemon loop started. Polling Firebase every {args.interval}s.")
            time.sleep(2)
            while True:
                try:
                    process_cron_tasks(bot_token, chat_id, firebase_url)
                except Exception as e:
                    print(f"Cron loop error: {e}")
                time.sleep(args.interval)

        t = threading.Thread(target=daemon_loop, daemon=True, name="QimenCronDaemonThread")
        t.start()

        server = http.server.HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        print(f"HTTP health server listening on 0.0.0.0:{port}...")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")
    else:
        print(f"Qimen Cron Worker initialized. Mode: DAEMON (Interval: {args.interval}s)")
        print(f"Starting daemon loop every {args.interval} seconds. Press Ctrl+C to stop.")
        while True:
            try:
                process_cron_tasks(bot_token, chat_id, firebase_url)
            except Exception as e:
                print(f"Loop iteration error: {e}")
            time.sleep(args.interval)

if __name__ == '__main__':
    main()
