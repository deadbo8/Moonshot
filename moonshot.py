import os
from dotenv import load_dotenv

# Load environment variables from the .env file when running locally
load_dotenv()

import upstox_client
from upstox_client.rest import ApiException
import requests

# --- Configuration via Environment Variables ---
ACCESS_TOKEN = os.getenv('UPSTOX_ACCESS_TOKEN')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

INDEX_KEY = 'NSE_INDEX|Nifty 50'
EXPIRY_DATE = '2026-06-09'
MIN_IV = 0.20              # REMINDER: Change back to your strategy minimums for live scanning
MIN_VEGA = 2.7           # REMINDER: Change back to your strategy minimums for live scanning
MIN_STRIKE_DISTANCE = 1000  # REMINDER: Change back to your strategy minimums for live scanning
# -----------------------------------------------

def send_telegram_alert(message):
    """Sends a formatted markdown message to your Telegram chat."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            print(f"Failed to send Telegram alert: {response.text}")
    except Exception as e:
        print(f"Error sending Telegram alert: {e}")

def get_option_chain(api_client, index_key, expiry_date):
    options_api = upstox_client.OptionsApi(api_client)
    try:
        response = options_api.get_put_call_option_chain(instrument_key=index_key, expiry_date=expiry_date)
        return response.data
    except ApiException as e:
        error_details = str(e)
        print(f"Exception fetching option chain: {error_details}")
        send_telegram_alert(f"⚠️ *API Error:* Upstox rejected the option chain request.\n\n*Details:*\n`{error_details[:150]}`")
        return None

def get_option_greeks(api_client, instrument_keys):
    market_quote_api = upstox_client.MarketQuoteV3Api(api_client)
    all_greeks_data = {}
    chunk_size = 50  
    
    for i in range(0, len(instrument_keys), chunk_size):
        chunk = instrument_keys[i:i + chunk_size]
        keys_string = ",".join(chunk)
        
        try:
            response = market_quote_api.get_market_quote_option_greek(instrument_key=keys_string)
            if response.data:
                # We simply load the raw data exactly as Upstox hands it to us
                all_greeks_data.update(response.data)
        except ApiException as e:
            print(f"Exception fetching Option Greeks for chunk {i} to {i + chunk_size}: {e}")
            
    return all_greeks_data

def scan_for_iv_spikes():
    if not all([ACCESS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        print("Error: Missing required environment variables.")
        return

    configuration = upstox_client.Configuration()
    configuration.access_token = ACCESS_TOKEN
    api_client = upstox_client.ApiClient(configuration)
    
    print("\n--- PIPELINE DIAGNOSTICS ---")
    
    # STEP 1: Fetch Chain
    chain_data = get_option_chain(api_client, INDEX_KEY, EXPIRY_DATE)
    if not chain_data:
        print("[FAIL] Could not fetch Option Chain.")
        return
    print(f"[OK] Fetched Option Chain. Total Contracts: {len(chain_data)}")

    # STEP 2: Spot Price
    spot_price = chain_data[0].underlying_spot_price
    print(f"[OK] Nifty Spot Price read as: {spot_price}")

    # STEP 3: Filter OTM Puts
    deep_otm_puts = []
    put_instrument_keys = []
    
    for contract in chain_data:
        strike_price = contract.strike_price
        distance = spot_price - strike_price
        
        if contract.put_options and distance >= MIN_STRIKE_DISTANCE:
            # Store as tuple: (Integer Strike, Contract Object)
            deep_otm_puts.append((int(strike_price), contract.put_options))
            put_instrument_keys.append(contract.put_options.instrument_key)

    print(f"[OK] Contracts meeting MIN_STRIKE_DISTANCE ({MIN_STRIKE_DISTANCE} pts): {len(put_instrument_keys)}")

    if not put_instrument_keys:
        print("[FAIL] Pipeline stopped. No contracts are far enough away from Spot Price.")
        return

    # STEP 4: Fetch Greeks
    greeks_data = get_option_greeks(api_client, put_instrument_keys)
    if not greeks_data:
         print("[FAIL] Pipeline stopped. Greeks API returned empty or failed.")
         return
    print(f"[OK] Greeks successfully retrieved for {len(greeks_data)} contracts.")
    print("----------------------------\n")

    # STEP 5: The Math Logic
    alert_messages = []
    
    print("--- DATA SNOOP (First 3 Contracts) ---")
    debug_count = 0
    
    for strike, put_contract in deep_otm_puts:
        
        # Find the Upstox key that ends with our exact Strike + "PE"
        target_suffix = f"{strike}PE"
        matching_key = next((key for key in greeks_data.keys() if key.endswith(target_suffix)), None)
        
        if matching_key:
            greeks = greeks_data[matching_key]
            
            # THE FINAL FIX: Using .iv instead of .implied_volatility
            iv = greeks.iv if greeks.iv else 0
            vega = greeks.vega if greeks.vega else 0
            
            if debug_count < 3:
                print(f"Strike {strike} PE -> Raw IV from Upstox: {iv} | Raw Vega: {vega}")
                debug_count += 1
            
            if iv >= MIN_IV and vega >= MIN_VEGA:
                ltp = put_contract.market_data.ltp if put_contract.market_data else 0
                
                msg = (
                    f"🚨 *IV Spike Detected!* 🚨\n"
                    f"• *Strike:* Nifty {strike} PE\n"
                    f"• *LTP:* ₹{ltp}\n"
                    f"• *IV:* {iv * 100:.2f}%\n"
                    f"• *Vega:* {vega:.4f}\n"
                    f"• *Spot Price:* {spot_price:.2f}"
                )
                alert_messages.append(msg)
                
    if alert_messages:
        # Safety Valve: Prevent Telegram 400 Error by capping at 5 alerts
        if len(alert_messages) > 5:
            full_notification = "\n\n---\n\n".join(alert_messages[:5])
            full_notification += f"\n\n---\n⚠️ *...and {len(alert_messages) - 5} more contracts met the criteria!*"
        else:
            full_notification = "\n\n---\n\n".join(alert_messages)
            
        send_telegram_alert(full_notification)
        print(f"\nSuccess! Found {len(alert_messages)} contracts and sent to Telegram.")
    else:
        print(f"\nScan completed: Evaluated {len(greeks_data)} valid contracts, but none met IV ({MIN_IV}) and Vega ({MIN_VEGA}) thresholds.")
        send_telegram_alert("✅ *Scan Completed:* No high IV/Vega contracts found.")

if __name__ == '__main__':
    scan_for_iv_spikes()