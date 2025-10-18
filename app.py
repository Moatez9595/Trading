import os
import hmac
import hashlib
import time
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
from collections import defaultdict
import threading

app = Flask(__name__)

# Configuration
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'your_testnet_api_key')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', 'your_testnet_secret_key')
BINANCE_BASE_URL = 'https://testnet.binancefuture.com'

# Store alerts temporarily (in-memory storage)
# Format: {symbol: {position_type: [(timestamp, alert_data), ...]}}
alert_storage = defaultdict(lambda: defaultdict(list))
alert_lock = threading.Lock()

# Configuration for alerts
ALERT_TIME_WINDOW = 10  # seconds - alerts must arrive within this window
REQUIRED_ALERTS = 3  # number of alerts needed to trigger a trade
LEVERAGE = 50
POSITION_SIZE_PERCENT = 50  # Use 50% of available balance

def generate_signature(query_string):
    """Generate HMAC SHA256 signature for Binance API"""
    return hmac.new(
        BINANCE_SECRET_KEY.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

def binance_request(method, endpoint, params=None):
    """Make authenticated request to Binance API"""
    if params is None:
        params = {}
    
    params['timestamp'] = int(time.time() * 1000)
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = generate_signature(query_string)
    
    url = f"{BINANCE_BASE_URL}{endpoint}?{query_string}&signature={signature}"
    headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
    
    if method == 'GET':
        response = requests.get(url, headers=headers)
    elif method == 'POST':
        response = requests.post(url, headers=headers)
    elif method == 'DELETE':
        response = requests.delete(url, headers=headers)
    
    return response.json()

def set_leverage(symbol, leverage):
    """Set leverage for a symbol"""
    try:
        result = binance_request('POST', '/fapi/v1/leverage', {
            'symbol': symbol,
            'leverage': leverage
        })
        print(f"Leverage set for {symbol}: {result}")
        return result
    except Exception as e:
        print(f"Error setting leverage: {e}")
        return None

def get_account_balance():
    """Get account balance"""
    try:
        result = binance_request('GET', '/fapi/v2/balance')
        usdt_balance = next((item for item in result if item['asset'] == 'USDT'), None)
        if usdt_balance:
            return float(usdt_balance['availableBalance'])
        return 0
    except Exception as e:
        print(f"Error getting balance: {e}")
        return 0

def get_symbol_price(symbol):
    """Get current price for symbol"""
    try:
        response = requests.get(f"{BINANCE_BASE_URL}/fapi/v1/ticker/price?symbol={symbol}")
        return float(response.json()['price'])
    except Exception as e:
        print(f"Error getting price: {e}")
        return 0

def calculate_quantity(symbol, balance, leverage, position_percent):
    """Calculate quantity based on balance and leverage"""
    price = get_symbol_price(symbol)
    if price == 0:
        return 0
    
    # Use position_percent of balance with leverage
    position_value = (balance * position_percent / 100) * leverage
    quantity = position_value / price
    
    # Round to appropriate precision (adjust based on symbol requirements)
    if symbol == 'BNBUSDT':
        quantity = round(quantity, 2)
    elif symbol == 'DOGEUSDT':
        quantity = round(quantity, 0)
    
    return quantity

def open_position(symbol, side, quantity):
    """Open a futures position"""
    try:
        # Set leverage first
        set_leverage(symbol, LEVERAGE)
        
        # Open position
        params = {
            'symbol': symbol,
            'side': side,  # 'BUY' for long, 'SELL' for short
            'type': 'MARKET',
            'quantity': quantity
        }
        
        result = binance_request('POST', '/fapi/v1/order', params)
        print(f"Position opened: {result}")
        return result
    except Exception as e:
        print(f"Error opening position: {e}")
        return None

def check_and_execute_trade(symbol, position_type):
    """Check if we have 3 alerts within time window and execute trade"""
    with alert_lock:
        alerts = alert_storage[symbol][position_type]
        
        if len(alerts) < REQUIRED_ALERTS:
            return False
        
        # Get the timestamps of the last 3 alerts
        recent_alerts = alerts[-REQUIRED_ALERTS:]
        timestamps = [alert[0] for alert in recent_alerts]
        
        # Check if all alerts are within the time window
        time_diff = max(timestamps) - min(timestamps)
        
        if time_diff <= ALERT_TIME_WINDOW:
            print(f"✅ All {REQUIRED_ALERTS} alerts received within {ALERT_TIME_WINDOW}s window!")
            print(f"Time difference: {time_diff}s")
            print(f"Executing {position_type.upper()} trade for {symbol}")
            
            # Get balance and calculate quantity
            balance = get_account_balance()
            quantity = calculate_quantity(symbol, balance, LEVERAGE, POSITION_SIZE_PERCENT)
            
            if quantity > 0:
                # BUY for LONG, SELL for SHORT
                side = 'BUY' if position_type == 'long' else 'SELL'
                result = open_position(symbol, side, quantity)
                
                # Clear alerts after successful trade
                alert_storage[symbol][position_type].clear()
                
                return True
            else:
                print("Error: Invalid quantity calculated")
                return False
        else:
            print(f"⏱️ Alerts time difference ({time_diff}s) exceeds window ({ALERT_TIME_WINDOW}s)")
            return False

def cleanup_old_alerts():
    """Remove alerts older than time window"""
    current_time = time.time()
    with alert_lock:
        for symbol in alert_storage:
            for position_type in alert_storage[symbol]:
                alert_storage[symbol][position_type] = [
                    alert for alert in alert_storage[symbol][position_type]
                    if current_time - alert[0] <= ALERT_TIME_WINDOW * 2
                ]

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive webhook from TradingView"""
    try:
        data = request.json
        print(f"Received alert: {data}")
        
        # Extract data from TradingView alert
        symbol = data.get('symbol', '').upper()
        position_type = data.get('position', '').lower()  # 'long' or 'short'
        alert_number = data.get('alert_number', 0)
        
        # Validate data
        if symbol not in ['BNBUSDT', 'DOGEUSDT']:
            return jsonify({'error': 'Invalid symbol'}), 400
        
        if position_type not in ['long', 'short']:
            return jsonify({'error': 'Invalid position type'}), 400
        
        # Store alert with timestamp
        current_time = time.time()
        with alert_lock:
            alert_storage[symbol][position_type].append((current_time, data))
        
        print(f"📩 Alert {alert_number} stored for {symbol} {position_type}")
        print(f"Total alerts for {symbol} {position_type}: {len(alert_storage[symbol][position_type])}")
        
        # Clean up old alerts
        cleanup_old_alerts()
        
        # Check if we should execute trade
        trade_executed = check_and_execute_trade(symbol, position_type)
        
        return jsonify({
            'status': 'success',
            'message': 'Alert received',
            'trade_executed': trade_executed,
            'total_alerts': len(alert_storage[symbol][position_type])
        }), 200
        
    except Exception as e:
        print(f"Error processing webhook: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/status', methods=['GET'])
def status():
    """Check bot status and current alerts"""
    with alert_lock:
        status_data = {
            'status': 'running',
            'alerts': {}
        }
        
        for symbol in alert_storage:
            status_data['alerts'][symbol] = {}
            for position_type in alert_storage[symbol]:
                alerts = alert_storage[symbol][position_type]
                status_data['alerts'][symbol][position_type] = {
                    'count': len(alerts),
                    'timestamps': [alert[0] for alert in alerts]
                }
        
        return jsonify(status_data), 200

@app.route('/clear', methods=['POST'])
def clear_alerts():
    """Clear all stored alerts"""
    with alert_lock:
        alert_storage.clear()
    return jsonify({'status': 'alerts cleared'}), 200

if __name__ == '__main__':
    print("🚀 Trading Bot Starting...")
    print(f"Binance Testnet URL: {BINANCE_BASE_URL}")
    print(f"Required alerts: {REQUIRED_ALERTS}")
    print(f"Time window: {ALERT_TIME_WINDOW} seconds")
    print(f"Leverage: {LEVERAGE}x")
    print(f"Position size: {POSITION_SIZE_PERCENT}% of balance")
    
    # Run Flask app
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
