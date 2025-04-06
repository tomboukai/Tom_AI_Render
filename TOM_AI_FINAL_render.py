# TOM_AI - מערכת מסחר לייב בקריפטו מבוססת אינדיקטור TOM
# קובץ עצמאי שמכיל את כל הלוגיקה: חיבור Binance, איתותים, ניהול פוזיציות והתראות טלגרם
# גרסה מותאמת לשימוש ב-Render עם משתני סביבה

import json
import numpy as np
import math
import os
import time
import threading
import requests
import pandas as pd
import ta
from datetime import datetime, timedelta
from binance.client import Client
from dotenv import load_dotenv

# טעינת משתני סביבה (מקובץ .env בפיתוח מקומי, או מהגדרות Render בהפעלה בענן)
load_dotenv()

# === הגדרת הקבועים הנדרשים מ-binance.enums ===
SIDE_BUY = 'BUY'
SIDE_SELL = 'SELL'
ORDER_TYPE_MARKET = 'MARKET'
ORDER_TYPE_LIMIT = 'LIMIT'
ORDER_TYPE_STOP_MARKET = 'STOP_MARKET'
TIME_IN_FORCE_GTC = 'GTC'

# === קריאת API ממשתני סביבה ===
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# בדיקה שכל המשתנים הדרושים קיימים
required_vars = ["BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]
for var in required_vars:
    if not os.getenv(var):
        raise ValueError(f"חסר משתנה סביבה נדרש: {var}")

client = Client(API_KEY, API_SECRET)

# הגדלת הסכום המינימלי לעסקה כדי להימנע משגיאות
PORTFOLIO_USD = float(os.getenv("PORTFOLIO_USD", "1000"))
# עדכון רשימת המטבעות - ברירת מחדל או מתוך משתנה סביבה
default_symbols = 'BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,APTUSDT'
symbols_str = os.getenv("SYMBOLS", default_symbols)
symbols = symbols_str.split(',')
open_positions = {}

def get_precision(symbol):
    """קבלת דיוק עשרוני עבור המטבע"""
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        return int(abs(round(np.log10(float(f['stepSize'])))))
    except Exception as e:
        print(f"שגיאה בשליפת precision עבור {symbol}: {e}")
        if symbol.startswith("BTC"):
            return 3
        elif symbol.startswith("ETH"):
            return 3
        else:
            return 1  # ברירת מחדל

# === פונקציית טלגרם ===
def send_telegram_message(message):
    """שליחת הודעה לטלגרם"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print(f"שגיאה בשליחת הודעת טלגרם: {e}")

def send_trade_open_notification(symbol, signal_data, quantity, leverage, mark_price):
    """שליחת התראה מפורטת על פתיחת עסקה"""
    direction = signal_data['signal']
    score = signal_data['score']
    tp = signal_data['tp']
    sl = signal_data['sl']
    valid_for = signal_data['valid_for_minutes']
    
    emoji = "🔴" if direction == "SHORT" else "🟢"
    
    message = f"{emoji} *עסקה חדשה נפתחה* {emoji}\n\n"
    message += f"*נכס:* {symbol}\n"
    message += f"*כיוון:* {direction}\n"
    message += f"*ציון איתות:* {score}\n"
    message += f"*שער כניסה:* {mark_price}\n"
    message += f"*כמות:* {quantity}\n"
    message += f"*מינוף:* {leverage}x\n"
    message += f"*Take Profit:* {tp}\n"
    message += f"*Stop Loss:* {sl}\n"
    message += f"*תוקף:* {valid_for} דקות\n"
    message += f"*זמן פתיחה:* {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
    
    send_telegram_message(message)

def setup_order_status_monitor(symbol):
    """הגדרת מעקב אחרי סטטוס הזמנות"""
    try:
        # שמירת מידע על העסקה הנוכחית
        positions = client.futures_position_information(symbol=symbol)
        orders = client.futures_get_open_orders(symbol=symbol)
        
        position_data = None
        for pos in positions:
            if float(pos['positionAmt']) != 0:
                position_data = {
                    'symbol': symbol,
                    'entry_price': float(pos['entryPrice']),
                    'position_amt': float(pos['positionAmt']),
                    'is_long': float(pos['positionAmt']) > 0,
                    'orders': []
                }
                break
        
        if not position_data:
            return
            
        # שמירת המידע על ההזמנות (TP/SL)
        for order in orders:
            if order['symbol'] == symbol:
                position_data['orders'].append({
                    'order_id': order['orderId'],
                    'type': order['type'],
                    'price': float(order['price']) if order['type'] == 'LIMIT' else float(order['stopPrice']),
                    'side': order['side']
                })
        
        # בהפעלה בענן, אין אפשרות לכתוב לקבצים, אז נשמור את הנתונים בזיכרון
        global open_positions
        if 'monitor_data' not in open_positions:
            open_positions['monitor_data'] = {}
        open_positions['monitor_data'][symbol] = position_data
            
        # התחלת תהליך מעקב
        monitor_thread = threading.Thread(
            target=monitor_position_status,
            args=(symbol,)
        )
        monitor_thread.daemon = True
        monitor_thread.start()
        
    except Exception as e:
        print(f"שגיאה בהגדרת מעקב סטטוס: {e}")

def monitor_position_status(symbol):
    """מעקב אחרי סטטוס פוזיציה והתראה על סגירה"""
    try:
        # טעינת נתוני הפוזיציה מהזיכרון
        global open_positions
        if 'monitor_data' not in open_positions or symbol not in open_positions['monitor_data']:
            print(f"לא נמצאו נתוני פוזיציה עבור {symbol}")
            return
            
        position_data = open_positions['monitor_data'][symbol]
        entry_price = position_data['entry_price']
        is_long = position_data['is_long']
        
        # לולאת מעקב - בדיקה כל 30 שניות אם הפוזיציה עדיין קיימת
        while True:
            time.sleep(30)
            
            # בדיקה אם הפוזיציה עדיין קיימת
            positions = client.futures_position_information(symbol=symbol)
            position_exists = False
            
            for pos in positions:
                if float(pos['positionAmt']) != 0:
                    position_exists = True
                    break
            
            if not position_exists:
                # הפוזיציה נסגרה - בדיקה האם דרך TP או SL
                close_reason = "לא ידוע"
                exit_price = 0
                
                # ניסיון לבדוק מתי הפוזיציה נסגרה
                now = datetime.now()
                five_mins_ago = now - timedelta(minutes=5)
                
                trades = client.futures_account_trades(symbol=symbol, startTime=int(five_mins_ago.timestamp() * 1000))
                
                if trades:
                    latest_trade = trades[-1]
                    exit_price = float(latest_trade['price'])
                    
                    # ניסיון לזהות האם זה היה TP או SL
                    if is_long:
                        if exit_price > entry_price:
                            close_reason = "Take Profit"
                        else:
                            close_reason = "Stop Loss"
                    else:
                        if exit_price < entry_price:
                            close_reason = "Take Profit"
                        else:
                            close_reason = "Stop Loss"
                
                # שליחת התראה
                send_position_closed_notification(symbol, entry_price, exit_price, close_reason, is_long)
                
                # מחיקת נתוני המעקב מהזיכרון
                if 'monitor_data' in open_positions and symbol in open_positions['monitor_data']:
                    del open_positions['monitor_data'][symbol]
                    
                break
                
    except Exception as e:
        print(f"שגיאה במעקב אחרי סטטוס פוזיציה {symbol}: {e}")

def send_position_closed_notification(symbol, entry_price, exit_price, close_reason, is_long):
    """שליחת התראה על סגירת פוזיציה"""
    
    pnl_pct = 0
    if is_long:
        pnl_pct = ((exit_price / entry_price) - 1) * 100
    else:
        pnl_pct = ((entry_price / exit_price) - 1) * 100
        
    emoji = "🔴" if pnl_pct < 0 else "🟢"
    reason_emoji = "🎯" if "Take Profit" in close_reason else "🛑"
    
    message = f"{reason_emoji} *עסקה נסגרה* {reason_emoji}\n\n"
    message += f"*נכס:* {symbol}\n"
    message += f"*סיבת סגירה:* {close_reason}\n"
    message += f"*כיוון:* {'LONG' if is_long else 'SHORT'}\n"
    message += f"*שער כניסה:* {entry_price}\n"
    message += f"*שער יציאה:* {exit_price}\n"
    message += f"*רווח/הפסד:* {emoji} {pnl_pct:.2f}%\n"
    message += f"*זמן סגירה:* {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
    
    send_telegram_message(message)

# === אינדיקטור TOM ===
def compute_indicators(df):
    """חישוב האינדיקטורים הטכניים"""
    df['EMA_50'] = df['close'].ewm(span=50).mean()
    df['EMA_200'] = df['close'].ewm(span=200).mean()
    atr = ta.volatility.AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=10).average_true_range()
    df['supertrend'] = df['close'] - atr
    df['RSI'] = ta.momentum.RSIIndicator(close=df['close'], window=14).rsi()
    df['volume_avg'] = df['volume'].rolling(window=20).mean()
    df['volume_spike'] = df['volume'] > (df['volume_avg'] * 1.5)
    df['price_above_emas'] = (df['close'] > df['EMA_50']) & (df['EMA_50'] > df['EMA_200'])
    df['bullish_engulfing'] = (df['close'] > df['open']) & (df['open'] < df['close'].shift(1)) & (df['close'] > df['close'].shift(1))
    return df

def generate_signal(df):
    """יצירת האיתות לפי האינדיקטורים"""
    last = df.iloc[-1]
    long_conditions = [
        last['price_above_emas'],
        last['supertrend'] < last['close'],
        last['RSI'] > 50,
        last['volume_spike'],
        last['bullish_engulfing']
    ]
    short_conditions = [
        not last['price_above_emas'],
        last['supertrend'] > last['close'],
        last['RSI'] < 50,
        last['volume_spike'],
        not last['bullish_engulfing']
    ]
    score = 0
    if sum(long_conditions) >= 1:  # if all(long_conditions):
        signal = 'LONG'
        score = 80 + (last['RSI'] - 50) * 0.5
    elif all(short_conditions):
        signal = 'SHORT'
        score = 80 + (50 - last['RSI']) * 0.5
    else:
        signal = 'NO SIGNAL'
        score = 0
    tp_pct = 0.015 + (score / 1000)
    sl_pct = 0.01 + ((100 - score) / 1000)
    entry_price = last['close']
    tp = entry_price * (1 + tp_pct) if signal == 'LONG' else entry_price * (1 - tp_pct)
    sl = entry_price * (1 - sl_pct) if signal == 'LONG' else entry_price * (1 + sl_pct)
    if score >= 90:
        valid_for = 300
    elif score >= 80:
        valid_for = 180
    elif score >= 70:
        valid_for = 120
    else:
        valid_for = 60
    return {
        'signal': signal,
        'score': round(score, 2),
        'entry_price': round(entry_price, 2),
        'tp': round(tp, 2),
        'sl': round(sl, 2),
        'valid_for_minutes': valid_for
    }

# === לוגיקה של ניהול פוזיציות פתוחות לפי זמן ===
def log_trade(symbol, direction, score, valid_until):
    """תיעוד עסקאות בזיכרון במקום בקובץ"""
    try:
        global open_positions
        if 'trade_log' not in open_positions:
            open_positions['trade_log'] = []
            
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'symbol': symbol,
            'direction': direction,
            'score': score,
            'valid_until': valid_until.isoformat()
        }
        open_positions['trade_log'].append(log_entry)
        print(f"נרשמה לוג עסקה: {log_entry}")
    except Exception as e:
        print(f"שגיאה ברישום לוג עסקה: {e}")

def manage_open_positions(symbol, df, direction, score, valid_for_minutes):
    """ניהול פוזיציות פתוחות"""
    try:
        entry_time = datetime.now()
        valid_until = entry_time + timedelta(minutes=valid_for_minutes)
        log_trade(symbol, direction, score, valid_until)
        open_positions[symbol] = {
            'direction': direction,
            'score': score,
            'valid_until': valid_until
        }
        
        # המתנה עד 5 דקות לפני סיום התוקף
        wait_time = max(0, (valid_until - datetime.now() - timedelta(minutes=5)).total_seconds())
        if wait_time > 0:
            time.sleep(wait_time)
            
        # בדיקה אם יש פוזיציה פתוחה בכלל
        if not is_position_open(symbol):
            print(f"הפוזיציה עבור {symbol} כבר נסגרה, מפסיקים מעקב.")
            if symbol in open_positions:
                del open_positions[symbol]
            return
            
        updated_df = get_klines_df(symbol)
        updated_df = compute_indicators(updated_df)
        new_signal_data = generate_signal(updated_df)
        new_direction = new_signal_data['signal']
        new_score = new_signal_data['score']
        new_valid_for = new_signal_data['valid_for_minutes']
        
        if new_direction == 'NO SIGNAL':
            print(f"אין איתות חדש עבור {symbol}, ממשיכים לעקוב אחר הפוזיציה הקיימת.")
            return
            
        if new_direction != direction:
            send_telegram_message(f"🔄 שינוי כיוון על {symbol}: מ-{direction} ל-{new_direction}")
            log_trade(symbol, new_direction, new_score, datetime.now() + timedelta(minutes=new_valid_for))
            open_positions[symbol] = {
                'direction': new_direction,
                'score': new_score,
                'valid_until': datetime.now() + timedelta(minutes=new_valid_for)
            }
            # סגירת העסקה הקיימת
            close_position(symbol)
            # פתיחת עסקה בכיוון החדש
            open_futures_trade(symbol, new_signal_data)
            manage_open_positions(symbol, updated_df, new_direction, new_score, new_valid_for)
        elif new_direction == direction:
            new_valid_until = datetime.now() + timedelta(minutes=new_valid_for)
            send_telegram_message(f"✅ אישור כיוון קיים ({direction}) על {symbol}, הארכה עד {new_valid_until}")
            log_trade(symbol, direction, new_score, new_valid_until)
            open_positions[symbol]['valid_until'] = new_valid_until
            open_positions[symbol]['score'] = new_score
            manage_open_positions(symbol, updated_df, direction, new_score, new_valid_for)
    except Exception as e:
        print(f"שגיאה בניהול פוזיציה עבור {symbol}: {e}")

def close_position(symbol):
    """סגירת פוזיציה קיימת"""
    try:
        positions = client.futures_position_information(symbol=symbol)
        for pos in positions:
            pos_amt = float(pos['positionAmt'])
            if pos_amt == 0:
                continue
                
            side = SIDE_SELL if pos_amt > 0 else SIDE_BUY
            qty = abs(pos_amt)
            
            print(f"סוגר פוזיציה {symbol}: {side} {qty}")
            client.futures_create_order(
                symbol=symbol,
                side=side,
                type=ORDER_TYPE_MARKET,
                quantity=qty,
                reduceOnly=True
            )
            print(f"פוזיציה נסגרה: {symbol}")
            # ביטול הזמנות פתוחות (TP/SL)
            client.futures_cancel_all_open_orders(symbol=symbol)
    except Exception as e:
        print(f"שגיאה בסגירת פוזיציה {symbol}: {e}")

# === פונקציות למסחר בפועל ===
def is_position_open(symbol):
    """בדיקה אם יש פוזיציה פתוחה"""
    try:
        positions = client.futures_position_information(symbol=symbol)
        for pos in positions:
            if float(pos['positionAmt']) != 0:
                return True
        return False
    except Exception as e:
        print(f"שגיאה בבדיקת סטטוס פוזיציה: {e}")
        return False

def get_position_settings(score):
    """קביעת הגדרות פוזיציה בהתאם לחוזק האיתות"""
    if score >= 90:
        return {'amount_pct': 0.045, 'leverage': 20}
    elif score >= 80:
        return {'amount_pct': 0.030, 'leverage': 15}
    else:
        return {'amount_pct': 0.015, 'leverage': 10}

def setup_tp_sl(symbol, quantity, opposite_side, tp, sl, precision):
    """הגדרת TP/SL בפונקציה נפרדת עם ניסיונות חוזרים"""
    
    # הגדרת TP - עם ניסיונות חוזרים
    for attempt in range(3):  # 3 ניסיונות
        try:
            tp_price = round(tp, precision)
            print(f"מגדיר TP ל-{symbol}: {tp_price} (ניסיון {attempt+1})")
            
            tp_order = client.futures_create_order(
                symbol=symbol, 
                side=opposite_side, 
                type=ORDER_TYPE_LIMIT,
                timeInForce=TIME_IN_FORCE_GTC,
                quantity=quantity, 
                price=tp_price,
                reduceOnly=True
            )
            print(f"הגדרת TP הצליחה: {tp_order}")
            break  # יציאה מהלולאה אם הצליח
            
        except Exception as e:
            print(f"שגיאה בהגדרת TP (ניסיון {attempt+1}): {e}")
            if attempt < 2:  # אם זה לא הניסיון האחרון
                time.sleep(2)  # המתנה לפני ניסיון נוסף
    
    # הגדרת SL - עם ניסיונות חוזרים
    for attempt in range(3):  # 3 ניסיונות
        try:
            sl_price = round(sl, precision)
            print(f"מגדיר SL ל-{symbol}: {sl_price} (ניסיון {attempt+1})")
            
            sl_order = client.futures_create_order(
                symbol=symbol, 
                side=opposite_side, 
                type=ORDER_TYPE_STOP_MARKET,
                timeInForce=TIME_IN_FORCE_GTC,
                stopPrice=sl_price,
                quantity=quantity, 
                reduceOnly=True
            )
            print(f"הגדרת SL הצליחה: {sl_order}")
            break  # יציאה מהלולאה אם הצליח
            
        except Exception as e:
            print(f"שגיאה בהגדרת SL (ניסיון {attempt+1}): {e}")
            if attempt < 2:  # אם זה לא הניסיון האחרון
                time.sleep(2)  # המתנה לפני ניסיון נוסף

def verify_tp_sl_orders(symbol):
    """פונקציה לבדיקה שאכן נוצרו הזמנות TP/SL"""
    try:
        orders = client.futures_get_open_orders(symbol=symbol)
        
        has_tp = False
        has_sl = False
        
        for order in orders:
            if order['type'] == 'LIMIT' and order['reduceOnly']:
                has_tp = True
            elif order['type'] == 'STOP_MARKET' and order['reduceOnly']:
                has_sl = True
        
        if not has_tp or not has_sl:
            print(f"⚠️ חסרות הזמנות TP/SL ל-{symbol}. TP: {has_tp}, SL: {has_sl}")
            
            # קבלת מידע על הפוזיציה הפתוחה
            position = None
            positions = client.futures_position_information(symbol=symbol)
            for pos in positions:
                if float(pos['positionAmt']) != 0:
                    position = pos
                    break
                    
            if position:
                # חישוב מחדש של מחירי TP/SL
                entry_price = float(position['entryPrice'])
                pos_amt = float(position['positionAmt'])
                is_long = pos_amt > 0
                
                # חישוב בסיסי של TP/SL
                tp_pct = 0.015  # רווח של 1.5%
                sl_pct = 0.01   # הפסד של 1%
                
                if is_long:
                    tp = entry_price * (1 + tp_pct)
                    sl = entry_price * (1 - sl_pct)
                    side = SIDE_SELL
                else:
                    tp = entry_price * (1 - tp_pct)
                    sl = entry_price * (1 + sl_pct)
                    side = SIDE_BUY
                
                quantity = abs(pos_amt)
                precision = get_precision(symbol)
                
                # יצירת הזמנות חסרות
                if not has_tp:
                    try:
                        tp_price = round(tp, precision)
                        print(f"יוצר TP חסר ל-{symbol}: {tp_price}")
                        client.futures_create_order(
                            symbol=symbol, 
                            side=side, 
                            type=ORDER_TYPE_LIMIT,
                            timeInForce=TIME_IN_FORCE_GTC,
                            quantity=quantity, 
                            price=tp_price,
                            reduceOnly=True
                        )
                    except Exception as e:
                        print(f"שגיאה ביצירת TP חסר: {e}")
                
                if not has_sl:
                    try:
                        sl_price = round(sl, precision)
                        print(f"יוצר SL חסר ל-{symbol}: {sl_price}")
                        client.futures_create_order(
                            symbol=symbol, 
                            side=side, 
                            type=ORDER_TYPE_STOP_MARKET,
                            timeInForce=TIME_IN_FORCE_GTC,
                            stopPrice=sl_price,
                            quantity=quantity, 
                            reduceOnly=True
                        )
                    except Exception as e:
                        print(f"שגיאה ביצירת SL חסר: {e}")
        
    except Exception as e:
        print(f"שגיאה בבדיקת הזמנות TP/SL: {e}")

def open_futures_trade(symbol, signal_data):
    """פתיחת עסקת פיוצ'רס"""
    try:
        side = SIDE_BUY if signal_data['signal'] == 'LONG' else SIDE_SELL
        opposite_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
        score = signal_data['score']
        price = signal_data['entry_price']
        tp = signal_data['tp']
        sl = signal_data['sl']
        settings = get_position_settings(score)
        amount_usd = PORTFOLIO_USD * float(settings['amount_pct'])
        leverage = settings['leverage']
        
        # בדיקה שערך העסקה מספיק גבוה
        min_trade_value = 25  # מינימום 25 דולר לעסקה
        if amount_usd < min_trade_value:
            print(f"❌ שווי עסקה קטן מ-{min_trade_value}$ ב-{symbol} – נמנעים מפתיחה.")
            return
        
        # הגדרת מינוף ומרג'ין
        try:
            client.futures_change_leverage(symbol=symbol, leverage=leverage)
            print(f"מינוף עודכן: {symbol} - {leverage}x")
        except Exception as e:
            # התעלמות משגיאת מינוף כפול
            if "No need to change leverage" not in str(e):
                print(f"שגיאה בהגדרת מינוף: {e}")
        
        try:
            client.futures_change_margin_type(symbol=symbol, marginType='ISOLATED')
            print(f"סוג מרג'ין עודכן: {symbol} - ISOLATED")
        except Exception as e:
            # התעלמות משגיאת שינוי מרג'ין מיותר
            if "No need to change margin type" not in str(e):
                print(f"שגיאה בהגדרת סוג מרג'ין: {e}")

        # קבלת מחיר עדכני והכמות לקנייה
        try:
            mark_price = float(client.futures_mark_price(symbol=symbol)['markPrice'])
            precision = get_precision(symbol)
            quantity = round(amount_usd * leverage / mark_price, precision)
            
            if quantity <= 0:
                print(f"❌ כמות לא חוקית ל-{symbol} (quantity={quantity}) – נמנעים מפתיחה.")
                return
                
            print(f"פותח עסקה {side} עבור {symbol}, כמות: {quantity}, מינוף: {leverage}x")
            
            # פתיחת הפוזיציה
            order = client.futures_create_order(
                symbol=symbol, 
                side=side, 
                type=ORDER_TYPE_MARKET, 
                quantity=quantity
            )
            
            print(f"פתיחת עסקה בוצעה: {order}")
            
            # שליחת התראה מפורטת לטלגרם
            send_trade_open_notification(symbol, signal_data, quantity, leverage, mark_price)
            
            # המתנה קצרה לוודא שהעסקה התבצעה
            time.sleep(2)
            
            # הגדרת TP/SL
            setup_tp_sl(symbol, quantity, opposite_side, tp, sl, precision)
            
            # בדיקה שאכן נוצרו הזמנות TP/SL
            time.sleep(2)
            verify_tp_sl_orders(symbol)
            
            # הפעלת מנגנון מעקב עבור סטטוס העסקה
            time.sleep(1)  # המתנה להשלמת העסקה והזמנות
            setup_order_status_monitor(symbol)
            
        except Exception as e:
            print(f"שגיאה בפתיחת עסקה: {e}")
            
    except Exception as e:
        print(f"שגיאה כללית בעסקה: {e}")

# === שליפת נתוני מסחר מ-Binance ===
def get_klines_df(symbol, interval='15m', limit=100):
    """שליפת נתוני נרות מ-Binance"""
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])
        df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        return df
    except Exception as e:
        print(f"שגיאה בשליפת נתוני מסחר עבור {symbol}: {e}")
        raise

# === עיבוד עבור מטבע בודד ===
def process_symbol(symbol, df):
    """עיבוד וקבלת החלטות עבור מטבע בודד"""
    try:
        print(f"🔍 בודק את {symbol} בעומק עם אינדיקטור TOM...")
        df = compute_indicators(df)
        signal_data = generate_signal(df)
        print(f"🔁 {symbol} | איתות: {signal_data['signal']} | חוזק: {signal_data['score']}")
        
        if signal_data['signal'] == 'NO SIGNAL':
            print(f"אין איתות עבור {symbol}, ממשיכים.")
            return
            
        msg = f"📡 איתות על {symbol} | כיוון: {signal_data['signal']} | חוזק: {signal_data['score']}"
        send_telegram_message(msg)
        
        if is_position_open(symbol):
            print(f"⚠️ כבר יש פוזיציה פתוחה עבור {symbol}, לא פותחים עסקה חדשה.")
            return
            
        # פתיחת עסקה חדשה
        open_futures_trade(symbol, signal_data)
        
        # תהליך נפרד לניהול הפוזיציה לאורך זמן
        manage_thread = threading.Thread(
            target=manage_open_positions, 
            args=(symbol, df, signal_data['signal'], signal_data['score'], signal_data['valid_for_minutes'])
        )
        manage_thread.daemon = True  # מאפשר לתכנית הראשית להסתיים גם אם התהליך עדיין רץ
        manage_thread.start()
        
    except Exception as e:
        print(f"שגיאה בעיבוד {symbol}: {e}")

# === לולאה: סריקה כל 5 דקות ===
def run_bot():
    """הפעלת הבוט בלולאה"""
    print("🚀 מתחיל הרצת בוט מסחר TOM_AI...")
    print(f"מטבעות במעקב: {', '.join(symbols)}")
    send_telegram_message(f"🤖 בוט TOM_AI הופעל!\nפורטפוליו: {PORTFOLIO_USD} USDT\nמטבעות במעקב: {', '.join(symbols)}")
    
    # בדיקת TP/SL חסרים בפוזיציות קיימות
    for symbol in symbols:
        if is_position_open(symbol):
            verify_tp_sl_orders(symbol)
            setup_order_status_monitor(symbol)
    
    while True:
        try:
            current_time = datetime.now().strftime('%H:%M:%S')
            print(f"⏱️ סריקה {current_time}")
            
            for symbol in symbols:
                try:
                    print(f"בודק {symbol}...")
                    df = get_klines_df(symbol)
                    process_symbol(symbol, df)
                    # השהייה קצרה בין מטבעות למניעת עומס על API
                    time.sleep(1)
                except Exception as e:
                    print(f"שגיאה בסימבול {symbol}: {e}")
                    continue
                    
            wait_time = 300  # 5 דקות
            print(f"💤 ממתין {wait_time} שניות עד הסריקה הבאה...")
            time.sleep(wait_time)
            
        except KeyboardInterrupt:
            print("🛑 עצירת הבוט על ידי המשתמש.")
            send_telegram_message("🛑 בוט TOM_AI הופסק ידנית.")
            break
            
        except Exception as e:
            print(f"שגיאה כללית בהרצת הבוט: {e}")
            time.sleep(60)  # המתנה קצרה במקרה של שגיאה לפני ניסיון נוסף
    
# === התחלת הבוט ===
if __name__ == "__main__":
    run_bot()