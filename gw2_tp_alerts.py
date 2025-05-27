#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Guild Wars 2 Trading Post Alert System - Enhanced Version

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import shutil
from datetime import datetime
import urllib.parse
import urllib.request
from urllib.error import URLError, HTTPError

# Configuration
CHECK_INTERVAL = 300  # 5 minutes (300 seconds)
RATE_LIMIT_RETRY = 1200  # 20 minutes (1200 seconds)

# Get script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Default paths (relative to script location)
CONFIG_FILE = os.path.join(SCRIPT_DIR, '.gw2_tp_alerts_config.json')
LOG_DIR = os.path.join(SCRIPT_DIR, 'logs')
DB_FILE = os.path.join(SCRIPT_DIR, 'gw2_tp_alerts.sqlite3')

class RateLimitHitException(Exception):
    pass

class DB:
    stateVars = ['last_run', 'limit_until', 'custom_dir']

    def __init__(self):
        # Check if custom directory is set
        config = ConfigManager.load_config()
        custom_dir = config.get('CUSTOM_DATA_DIR')
        
        if custom_dir and os.path.exists(custom_dir):
            self.__dict__['file'] = os.path.join(custom_dir, 'gw2_tp_alerts.sqlite3')
            log_dir = os.path.join(custom_dir, 'logs')
        else:
            self.__dict__['file'] = DB_FILE
            log_dir = LOG_DIR
            
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        os.makedirs(log_dir, exist_ok=True)
        
        if not os.path.exists(self.file):
            with open(self.file, 'w') as fh:
                fh.close()
        self.__dict__['conn'] = sqlite3.connect(self.file)
        self.conn.row_factory = sqlite3.Row
        self.__migrate()

    def __migrate(self):
        # Create tables with all columns to avoid migration issues
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS `items` (
                `id`    INTEGER NOT NULL UNIQUE,
                `name`  TEXT,
                `buy_threshold` INTEGER NOT NULL,
                `sell_threshold` INTEGER NOT NULL,
                `lowest_seen`   INTEGER DEFAULT 999999999,
                `last_buy_price` INTEGER DEFAULT 0,
                `last_sell_price` INTEGER DEFAULT 0,
                `icon_url` TEXT,
                PRIMARY KEY(`id`)
            );
        ''')
        self.conn.execute('CREATE TABLE IF NOT EXISTS state (last_run INT, limit_until INT, custom_dir TEXT);')
        
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM state;')
        if cursor.fetchone()[0] < 1:
            self.conn.execute('INSERT INTO state (last_run, limit_until, custom_dir) VALUES (0, 0, NULL);')
        self.conn.commit()

        # Migration system to handle future updates
        userVersion = self.conn.execute('PRAGMA user_version;').fetchone()[0]
        
        if userVersion < 1:
            # Migration for version 1 (icon_url)
            cursor.execute("PRAGMA table_info(items);")
            columns = [column[1] for column in cursor.fetchall()]
            if 'icon_url' not in columns:
                self.conn.execute('ALTER TABLE `items` ADD COLUMN `icon_url` TEXT;')
            self.conn.execute('PRAGMA user_version=1;')
        
        if userVersion < 2:
            # Migration for version 2 (price tracking)
            cursor.execute("PRAGMA table_info(items);")
            columns = [column[1] for column in cursor.fetchall()]
            if 'last_buy_price' not in columns:
                self.conn.execute('ALTER TABLE `items` ADD COLUMN `last_buy_price` INTEGER DEFAULT 0;')
            if 'last_sell_price' not in columns:
                self.conn.execute('ALTER TABLE `items` ADD COLUMN `last_sell_price` INTEGER DEFAULT 0;')
            self.conn.execute('PRAGMA user_version=2;')
        
        if userVersion < 3:
            # Migration for version 3 (custom_dir in state table)
            cursor.execute("PRAGMA table_info(state);")
            columns = [column[1] for column in cursor.fetchall()]
            if 'custom_dir' not in columns:
                self.conn.execute('ALTER TABLE state ADD COLUMN custom_dir TEXT;')
            self.conn.execute('PRAGMA user_version=3;')
            
        if userVersion < 4:
            # Migration for version 4 (split thresholds)
            cursor.execute("PRAGMA table_info(items);")
            columns = [column[1] for column in cursor.fetchall()]
            if 'sell_threshold' not in columns:
                # Rename old threshold to buy_threshold and add sell_threshold
                self.conn.execute('ALTER TABLE items RENAME COLUMN threshold TO buy_threshold;')
                self.conn.execute('ALTER TABLE items ADD COLUMN sell_threshold INTEGER NOT NULL DEFAULT 0;')
                # Update existing rows to set sell_threshold = buy_threshold
                self.conn.execute('UPDATE items SET sell_threshold = buy_threshold;')
            self.conn.execute('PRAGMA user_version=4;')
        
        self.conn.commit()

    def __getattr__(self, name):
        if name not in self.stateVars:
            raise AttributeError(f'state table has no column named {name}')
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM state;')
        return cursor.fetchone()[name]

    def __setattr__(self, name, value):
        if name not in self.stateVars:
            raise AttributeError(f'state table has no column named {name}')
        cursor = self.conn.cursor()
        cursor.execute(f'UPDATE state SET {name} = ?;', (value,))
        self.conn.commit()

    def items(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM items;')
        return {each['id']: each for each in cursor.fetchall()}

    def update_prices(self, itemId, buy_price, sell_price, lowest_seen=None):
        cursor = self.conn.cursor()
        if lowest_seen is not None:
            cursor.execute('''
                UPDATE items 
                SET last_buy_price = ?, last_sell_price = ?, lowest_seen = ?
                WHERE id = ?;
            ''', (buy_price, sell_price, lowest_seen, itemId))
        else:
            cursor.execute('''
                UPDATE items 
                SET last_buy_price = ?, last_sell_price = ?
                WHERE id = ?;
            ''', (buy_price, sell_price, itemId))
        self.conn.commit()
        return cursor.rowcount == 1

    def itemAdd(self, id, name, iconUrl, buy_threshold, sell_threshold):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM items WHERE id = ? OR name = ?;', (id, name))
        if cursor.fetchone()[0] > 0:
            raise ValueError(f'Item with ID={id} or name={name} already exists')
        
        self.conn.execute('''
            INSERT INTO items (id, name, buy_threshold, sell_threshold, icon_url, last_buy_price, last_sell_price) 
            VALUES (?, ?, ?, ?, ?, 0, 0);
        ''', (id, name, buy_threshold, sell_threshold, iconUrl))
        self.conn.commit()

    def itemDel(self, id):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM items WHERE id = ?;', (id,))
        self.conn.commit()
        return cursor.rowcount == 1

    def itemThreshold(self, id, buy_threshold=None, sell_threshold=None):
        cursor = self.conn.cursor()
        if buy_threshold is not None and sell_threshold is not None:
            cursor.execute('UPDATE items SET buy_threshold = ?, sell_threshold = ?, lowest_seen = 999999999 WHERE id = ?;', 
                          (buy_threshold, sell_threshold, id))
        elif buy_threshold is not None:
            cursor.execute('UPDATE items SET buy_threshold = ?, lowest_seen = 999999999 WHERE id = ?;', 
                          (buy_threshold, id))
        elif sell_threshold is not None:
            cursor.execute('UPDATE items SET sell_threshold = ?, lowest_seen = 999999999 WHERE id = ?;', 
                          (sell_threshold, id))
        self.conn.commit()
        return cursor.rowcount == 1

class GW2API:
    baseUrl = 'https://api.guildwars2.com'
    itemsFragment = '/v2/items?ids='
    listingsFragment = '/v2/commerce/listings?ids='
    token = None
    headers = {}

    def __init__(self, token=None):
        self.token = token
        if token:
            self.headers = {'Authorization': f'Bearer {token}'}

    def items(self, idList):
        items = {}
        for i in self.__fetch(self.itemsFragment + ','.join(idList)):
            items[i['id']] = {'name': i['name'], 'icon': i['icon']}
        return items

    def listings(self, idList):
        listings = {}
        for i in self.__fetch(self.listingsFragment + ','.join(idList)):
            # Get most recent buy and sell orders (first in list)
            buy_order = i['buys'][0] if i['buys'] else {'unit_price': 0, 'quantity': 0}
            sell_order = i['sells'][0] if i['sells'] else {'unit_price': 0, 'quantity': 0}
            
            listings[i['id']] = {
                'buy': {
                    'unit_price': buy_order['unit_price'],
                    'quantity': buy_order['quantity']
                },
                'sell': {
                    'unit_price': sell_order['unit_price'],
                    'quantity': sell_order['quantity']
                }
            }
        return listings

    def __fetch(self, urlFragment):
        targetUrl = self.baseUrl + urlFragment
        request = urllib.request.Request(targetUrl, None, self.headers)
        
        try:
            response = urllib.request.urlopen(request)
            # Reset rate limit tracking on successful request
            db = DB()
            db.limit_until = 0
            return json.loads(response.read().decode('utf-8'))
            
        except HTTPError as e:
            if e.code == 429:
                db = DB()
                db.limit_until = int(time.time()) + RATE_LIMIT_RETRY
                print(f"\n\033[91m⚠️ RATE LIMIT EXCEEDED⚠️\n"
                      f"Waiting {RATE_LIMIT_RETRY//60} minutes before next attempt\n"
                      f"Next try at: {time.strftime('%H:%M:%S', time.localtime(db.limit_until))}\033[0m")
                raise RateLimitHitException(f'Rate limit: {e.reason}')
            raise e

class DiscordNotify:
    def __init__(self, webhook_url):
        if not webhook_url.startswith('https://discord.com/api/webhooks/'):
            raise ValueError("Invalid webhook URL. Must start with 'https://discord.com/api/webhooks/'")
        self.webhook_url = webhook_url
    
    def send_alert(self, alert_items):
        if not alert_items:
            return False

        try:
            # Group alerts by type
            buy_alerts = [item for item in alert_items if item['trigger'] == 'buy']
            sell_alerts = [item for item in alert_items if item['trigger'] == 'sell']

            embeds = []
            
            # Create embed for buy alerts (sell opportunities)
            if buy_alerts:
                items_list = "\n".join(
                    f"• {item['name']} - {format_price(item['price'])} (Available: {item['quantity']})"
                    for item in buy_alerts
                )
                
                buy_embed = {
                    "title": "💰 SELL Opportunity",
                    "description": items_list,
                    "color": 0x00ff00,  # Green
                    "thumbnail": {"url": buy_alerts[0]['icon']},
                    "footer": {
                        "text": "GW2 TP Alert - Buy price above threshold",
                        "icon_url": "https://wiki.guildwars2.com/images/thumb/d/df/Guild_Wars_2_logo.png/300px-Guild_Wars_2_logo.png"
                    },
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                }
                embeds.append(buy_embed)

            # Create embed for sell alerts (buy opportunities)
            if sell_alerts:
                items_list = "\n".join(
                    f"• {item['name']} - {format_price(item['price'])} (Available: {item['quantity']})"
                    for item in sell_alerts
                )
                
                sell_embed = {
                    "title": "🛒 BUY Opportunity",
                    "description": items_list,
                    "color": 0xff9900,  # Orange
                    "thumbnail": {"url": sell_alerts[0]['icon']},
                    "footer": {
                        "text": "GW2 TP Alert - Sell price below threshold",
                        "icon_url": "https://wiki.guildwars2.com/images/thumb/d/df/Guild_Wars_2_logo.png/300px-Guild_Wars_2_logo.png"
                    },
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                }
                embeds.append(sell_embed)

            payload = {
                "username": "GW2 TP Bot",
                "embeds": embeds
            }

            headers = {
                'Content-Type': 'application/json',
                'User-Agent': 'GW2-TP-Alert/1.0'
            }

            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload).encode('utf-8'),
                headers=headers,
                method='POST'
            )

            with urllib.request.urlopen(req) as response:
                if response.status != 204:
                    print(f"Unexpected Discord response: {response.status}")
                return True

        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = e.headers.get('Retry-After', 5)
                print(f"Discord rate limit reached. Retrying in {retry_after} seconds...")
                time.sleep(float(retry_after))
                return self.send_alert(alert_items)
            print(f"HTTP Error {e.code}: {e.reason}")
        except Exception as e:
            print(f"Error sending to Discord: {str(e)}")
        
        return False

def ensure_log_dir():
    db = DB()
    config = ConfigManager.load_config()
    
    if config.get('CUSTOM_DATA_DIR') and os.path.exists(config['CUSTOM_DATA_DIR']):
        log_dir = os.path.join(config['CUSTOM_DATA_DIR'], 'logs')
    else:
        log_dir = LOG_DIR
    
    os.makedirs(log_dir, exist_ok=True)
    return log_dir

def get_log_file():
    log_dir = ensure_log_dir()
    today = datetime.now().strftime('%Y-%m-%d')
    return os.path.join(log_dir, f"{today}_alerts.log")

def log_unusual_price(item_name, price, threshold, quantity, trigger_type):
    log_file = get_log_file()
    with open(log_file, 'a') as f:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if trigger_type == 'buy':
            f.write(f"{timestamp} - BUY ALERT: {item_name} - Buy Price: {format_price(price)} > Threshold: {format_price(threshold)} (Qty: {quantity})\n")
        else:
            f.write(f"{timestamp} - SELL ALERT: {item_name} - Sell Price: {format_price(price)} < Threshold: {format_price(threshold)} (Qty: {quantity})\n")

def view_logs():
    log_dir = ensure_log_dir()
    logs = [f for f in os.listdir(log_dir) if f.endswith('.log')]
    
    if not logs:
        print("No logs available.")
        return
    
    print("\nAvailable logs:")
    for i, log in enumerate(sorted(logs), 1):
        print(f"{i}. {log}")
    
    choice = input("\nSelect a log to view (0 to cancel): ").strip()
    try:
        choice = int(choice)
        if choice == 0:
            return
        selected_log = logs[choice-1]
        
        with open(os.path.join(log_dir, selected_log), 'r') as f:
            print(f"\nContents of {selected_log}:")
            print("="*90)
            print(f.read())
            print("="*90)
    except (ValueError, IndexError):
        print("Invalid selection.")

def format_price(copper):
    copper = int(copper)
    gold = copper // 10000
    silver = (copper % 10000) // 100
    copper = copper % 100
    return f"{gold}g{silver:02d}s{copper:02d}c"

def parse_price_input(price_str):
    price_str = price_str.strip().lower()
    if not re.match(r'^(\d+g)?(\d+s)?(\d+c)?$', price_str):
        raise ValueError("Invalid price format. Use formats like: 1g23s45c, 50s, 100c, 2g")
    
    copper = 0
    # Extract gold
    gold_match = re.search(r'(\d+)g', price_str)
    if gold_match:
        copper += int(gold_match.group(1)) * 10000
    # Extract silver
    silver_match = re.search(r'(\d+)s', price_str)
    if silver_match:
        copper += int(silver_match.group(1)) * 100
    # Extract copper
    copper_match = re.search(r'(\d+)c', price_str)
    if copper_match:
        copper += int(copper_match.group(1))
    
    return copper

def calculate_percentage_change(current, previous):
    if previous == 0:
        return "N/A"
    change = ((current - previous) / previous) * 100
    return f"{change:+.2f}%"

def print_status(items, prices, alert_items):
    print(f"\n{'='*120}")
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - Monitoring Mode")
    print(f"{'Item':<30} | {'Buy Threshold':>12} | {'Buy Price':>12} | {'Buy Qty':>8} | {'Buy %':>8} | {'Sell Threshold':>12} | {'Sell Price':>12} | {'Sell Qty':>8} | {'Sell %':>8} | {'Status':>10}")
    print("-"*120)
    
    for id, props in items.items():
        current_buy = prices[id]['buy']['unit_price']
        buy_qty = prices[id]['buy']['quantity']
        buy_pct = calculate_percentage_change(current_buy, props['last_buy_price'])
        
        current_sell = prices[id]['sell']['unit_price']
        sell_qty = prices[id]['sell']['quantity']
        sell_pct = calculate_percentage_change(current_sell, props['last_sell_price'])
        
        # Determine status
        status = []
        if current_buy > props['buy_threshold']:
            status.append("SELL")
        if current_sell < props['sell_threshold']:
            status.append("BUY")
        
        status_text = "/".join(status) if status else "WAIT"
        
        # Color coding
        color_code = ""
        if "SELL" in status and "BUY" in status:
            color_code = "\033[95m"  # Purple
        elif "SELL" in status:
            color_code = "\033[92m"  # Green
        elif "BUY" in status:
            color_code = "\033[93m"  # Yellow
        
        print(f"{props['name']:<30} | {format_price(props['buy_threshold']):>12} | "
              f"{format_price(current_buy):>12} | {buy_qty:>8} | "
              f"{buy_pct:>8} | {format_price(props['sell_threshold']):>12} | "
              f"{format_price(current_sell):>12} | {sell_qty:>8} | "
              f"{sell_pct:>8} | {color_code}{status_text:>10}\033[0m")
    
    print(f"\nAlert items: {len(alert_items)}")
    print("="*120)

def monitor_mode():
    print("\nMonitoring Mode activated")
    print("Notifications will be sent when:")
    print("- Buy prices ABOVE threshold (good time to SELL)")
    print("- Sell prices BELOW threshold (good time to BUY)")
    print("Press Ctrl+C to stop\n")
    
    discord_webhook = ConfigManager.get_env_var('DISCORD_WEBHOOK_URL')
    discord = DiscordNotify(discord_webhook) if discord_webhook else None
    
    while True:
        try:
            db = DB()
            
            if time.time() < db.limit_until:
                wait_time = db.limit_until - time.time()
                print(f"\n\033[93mWaiting {int(wait_time//60)}m {int(wait_time%60)}s due to rate limit...\033[0m")
                time.sleep(min(wait_time, CHECK_INTERVAL))
                continue
                
            items = db.items()
            if not items:
                print("No items in database. Add items first.")
                time.sleep(10)
                continue

            api = GW2API(ConfigManager.get_env_var('GW2_API_TOKEN'))
            
            try:
                prices = api.listings([str(id) for id in items.keys()])
            except RateLimitHitException:
                time.sleep(CHECK_INTERVAL)
                continue
            except Exception as e:
                print(f"Error fetching prices: {str(e)}")
                time.sleep(CHECK_INTERVAL)
                continue

            alert_items = []
            
            for id, props in items.items():
                current_buy = prices[id]['buy']['unit_price']
                buy_qty = prices[id]['buy']['quantity']
                current_sell = prices[id]['sell']['unit_price']
                sell_qty = prices[id]['sell']['quantity']
                
                db.update_prices(id, current_buy, current_sell)
                
                # Check buy price (sell opportunity)
                if current_buy > props['buy_threshold']:
                    alert_items.append({
                        'name': props['name'],
                        'price': current_buy,
                        'quantity': buy_qty,
                        'threshold': props['buy_threshold'],
                        'icon': props['icon_url'],
                        'trigger': 'buy'
                    })
                    log_unusual_price(props['name'], current_buy, props['buy_threshold'], buy_qty, 'buy')
                
                # Check sell price (buy opportunity)
                if current_sell < props['sell_threshold']:
                    alert_items.append({
                        'name': props['name'],
                        'price': current_sell,
                        'quantity': sell_qty,
                        'threshold': props['sell_threshold'],
                        'icon': props['icon_url'],
                        'trigger': 'sell'
                    })
                    log_unusual_price(props['name'], current_sell, props['sell_threshold'], sell_qty, 'sell')

            if alert_items and discord:
                discord.send_alert(alert_items)
            
            print_status(items, prices, alert_items)
            time.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            print("\nStopping monitor...")
            break
        except Exception as e:
            print(f"Error: {str(e)}")
            time.sleep(CHECK_INTERVAL)

def actionList():
    db = DB()
    items = db.items()
    
    if not items:
        print("No items in database.")
        return

    print("\nTracked Items:")
    print("="*90)
    print(f"{'Item':<30} | {'Buy Threshold':>12} | {'Sell Threshold':>12}")
    print("-"*90)
    for id, props in items.items():
        print(f"{props['name']:<30} | {format_price(props['buy_threshold']):>12} | {format_price(props['sell_threshold']):>12}")
    print("="*90)

def actionAdd(id, buy_threshold, sell_threshold):
    db = DB()
    api = GW2API(ConfigManager.get_env_var('GW2_API_TOKEN'))

    try:
        items = api.items([str(id)])
    except RateLimitHitException as e:
        print("\033[91mRate limit hit. Please try again later.\033[0m")
        return
    except Exception as e:
        print(f"Error fetching item info: {str(e)}")
        return

    try:
        for item_id, item in items.items():
            try:
                buy_copper = parse_price_input(buy_threshold)
                sell_copper = parse_price_input(sell_threshold)
                db.itemAdd(item_id, item['name'], item['icon'], buy_copper, sell_copper)
                print(f"Item added successfully: {item['name']}")
                print(f"Buy Threshold: {format_price(buy_copper)}")
                print(f"Sell Threshold: {format_price(sell_copper)}")
            except ValueError as e:
                print(f"Invalid threshold format: {str(e)}")
                return
    except ValueError as e:
        print(str(e))
        return

def actionDel(id):
    db = DB()
    if not db.itemDel(id):
        print("Failed to delete item. Check ID.")
        return
    print('Item deleted successfully.')

def actionThreshold(id, buy_threshold=None, sell_threshold=None):
    db = DB()
    try:
        buy_copper = parse_price_input(buy_threshold) if buy_threshold else None
        sell_copper = parse_price_input(sell_threshold) if sell_threshold else None
        
        if not db.itemThreshold(id, buy_copper, sell_copper):
            print("Failed to update threshold. Check ID.")
            return
        
        if buy_copper is not None:
            print(f'Buy Threshold updated successfully to {format_price(buy_copper)}')
        if sell_copper is not None:
            print(f'Sell Threshold updated successfully to {format_price(sell_copper)}')
    except ValueError as e:
        print(f"Invalid threshold format: {str(e)}")

def clean_all_data():
    """Clean all data but don't delete files"""
    try:
        db = DB()
        
        # Clear items table
        db.conn.execute('DELETE FROM items;')
        
        # Reset state table
        db.conn.execute('UPDATE state SET last_run = 0, limit_until = 0;')
        db.conn.commit()
        
        # Clear log files
        log_dir = ensure_log_dir()
        for filename in os.listdir(log_dir):
            if filename.endswith('.log'):
                file_path = os.path.join(log_dir, filename)
                try:
                    with open(file_path, 'w') as f:
                        f.write('')
                    print(f"Cleared log file: {filename}")
                except Exception as e:
                    print(f"Error clearing log file {filename}: {str(e)}")
        
        print("All data has been cleared successfully.")
        return True
    
    except Exception as e:
        print(f"Error cleaning data: {str(e)}")
        return False

class ConfigManager:
    @staticmethod
    def load_config():
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        return {}

    @staticmethod
    def save_config(config):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f)

    @staticmethod
    def get_env_var(name):
        config = ConfigManager.load_config()
        return config.get(name, os.environ.get(name))

def configure_settings():
    config = ConfigManager.load_config()
    
    while True:
        print("\nCurrent Configuration:")
        print(f"1. GW2 API Token: {'*****' if config.get('GW2_API_TOKEN') else 'Not set'}")
        print(f"2. Discord Webhook URL: {'*****' if config.get('DISCORD_WEBHOOK_URL') else 'Not set'}")
        print(f"3. Custom Data Directory: {config.get('CUSTOM_DATA_DIR', 'Not set (using script directory)')}")
        print("4. Back to main menu")
        
        choice = input("\nSelect an option (1-4): ").strip()
        
        if choice == "1":
            token = input("Enter GW2 API Token (leave blank to remove): ").strip()
            if token:
                config['GW2_API_TOKEN'] = token
            else:
                config.pop('GW2_API_TOKEN', None)
            ConfigManager.save_config(config)
            print("API Token updated.")
        elif choice == "2":
            url = input("Enter Discord Webhook URL (leave blank to remove): ").strip()
            if url:
                if url.startswith('https://discord.com/api/webhooks/'):
                    config['DISCORD_WEBHOOK_URL'] = url
                else:
                    print("Invalid webhook URL. Must start with 'https://discord.com/api/webhooks/'")
                    continue
            else:
                config.pop('DISCORD_WEBHOOK_URL', None)
            ConfigManager.save_config(config)
            print("Webhook URL updated.")
        elif choice == "3":
            path = input("Enter custom directory path (leave blank to use script directory): ").strip()
            if path:
                if os.path.exists(path):
                    config['CUSTOM_DATA_DIR'] = path
                    # Create logs subdirectory
                    os.makedirs(os.path.join(path, 'logs'), exist_ok=True)
                    print(f"Custom directory set to: {path}")
                else:
                    print("Directory does not exist. Please create it first.")
                    continue
            else:
                config.pop('CUSTOM_DATA_DIR', None)
                print("Using script directory for data storage.")
            ConfigManager.save_config(config)
        elif choice == "4":
            return
        else:
            print("Invalid option.")

def show_help():
    print("\n" + "="*50)
    print("HOW TO USE THIS SCRIPT AND CREDITS")
    print("="*50)
    print("1. Start Monitoring: Runs the main monitoring process")
    print("2. List tracked items: Shows all items being monitored")
    print("3. Add item: Add a new item to monitor with buy/sell thresholds")
    print("4. Delete item: Remove an item from monitoring")
    print("5. Change thresholds: Modify buy/sell thresholds for an item")
    print("6. Configure Settings: Set API token, Discord webhook and data directory")
    print("7. View historical logs: Check past price alerts")
    print("8. Clean all data: Reset all items and logs (keeps settings)")
    print("9. How to use: This help menu")
    print("0. Exit: Quit the program")
    print("\nCREDITS:")
    print("Original script by https://gist.github.com/beporter")
    print("Updated and adapted by KeKee.7683")
    print("\"She will outlive me, as she should. She will face horrors")
    print("that I will not. And in those moments, I hope she remembers")
    print("my strength, not my weakness\" said Snaff (Edge of Destiny, p202)")
    print("="*50)
    input("\nPress Enter to return to main menu...")

def show_menu():
    print("\n" + "="*50)
    print("GUILD WARS 2 TRADING POST ALERT SYSTEM")
    print("="*50)
    print("1. Start Monitoring")
    print("2. List tracked items")
    print("3. Add item")
    print("4. Delete item")
    print("5. Change thresholds")
    print("6. Configure Settings (API, Webhook, Data Dir)")
    print("7. View historical logs")
    print("8. CLEAN ALL DATA (reset everything)")
    print("9. How to use this script and credits")
    print("0. Exit")
    print("="*50)

def main_menu():
    # Ensure directories exist
    ensure_log_dir()
    
    while True:
        show_menu()
        choice = input("\nSelect an option (0-9): ").strip()
        
        if choice == "1":
            monitor_mode()
        elif choice == "2":
            actionList()
        elif choice == "3":
            try:
                id = int(input("Enter item ID: ").strip())
                buy_threshold = input("Enter BUY threshold: ").strip()
                sell_threshold = input("Enter SELL threshold: ").strip()
                actionAdd(id, buy_threshold, sell_threshold)
            except ValueError as e:
                print(f"Error: {str(e)}")
        elif choice == "4":
            try:
                id = int(input("Enter item ID to delete: ").strip())
                actionDel(id)
            except ValueError:
                print("ID must be a number. Try again.")
        elif choice == "5":
            try:
                id = int(input("Enter item ID: ").strip())
                change = input("Change which threshold? (1=Buy, 2=Sell, 3=Both): ").strip()
                
                if change == '1':
                    buy_threshold = input("Enter new BUY threshold: ").strip()
                    actionThreshold(id, buy_threshold=buy_threshold)
                elif change == '2':
                    sell_threshold = input("Enter new SELL threshold: ").strip()
                    actionThreshold(id, sell_threshold=sell_threshold)
                elif change == '3':
                    buy_threshold = input("Enter new BUY threshold: ").strip()
                    sell_threshold = input("Enter new SELL threshold: ").strip()
                    actionThreshold(id, buy_threshold, sell_threshold)
                else:
                    print("Invalid option.")
            except ValueError as e:
                print(f"Error: {str(e)}")
        elif choice == "6":
            configure_settings()
        elif choice == "7":
            view_logs()
        elif choice == "8":
            confirm = input("WARNING: This will clear ALL data including items and logs (but keep settings). Continue? (y/n): ").strip().lower()
            if confirm == 'y':
                clean_all_data()
        elif choice == "9":
            show_help()
            continue
        elif choice == "0":
            print("Exiting program...")
            break
        else:
            print("Invalid option. Try again.")
        
        input("\nPress Enter to continue...")

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "menu":
        main_menu()
    else:
        parser = argparse.ArgumentParser(description='GW2 Trading Post Alert System')
        subparsers = parser.add_subparsers(dest='command')
        
        run_parser = subparsers.add_parser('run', help='Start monitoring')
        run_parser.set_defaults(func=lambda args: monitor_mode())
        
        list_parser = subparsers.add_parser('list', help='List tracked items')
        list_parser.set_defaults(func=lambda x: actionList())
        
        add_parser = subparsers.add_parser('add', help='Add new item')
        add_parser.add_argument('id', type=int, help='Item ID')
        add_parser.add_argument('buy_threshold', type=str, help='Buy threshold (formats: 1g23s45c, 50s, 100c, 2g)')
        add_parser.add_argument('sell_threshold', type=str, help='Sell threshold (formats: 1g23s45c, 50s, 100c, 2g)')
        add_parser.set_defaults(func=lambda args: actionAdd(args.id, args.buy_threshold, args.sell_threshold))
        
        del_parser = subparsers.add_parser('del', help='Delete item')
        del_parser.add_argument('id', type=int, help='Item ID to delete')
        del_parser.set_defaults(func=lambda args: actionDel(args.id))
        
        thresh_parser = subparsers.add_parser('threshold', help='Change thresholds')
        thresh_parser.add_argument('id', type=int, help='Item ID')
        thresh_parser.add_argument('--buy', type=str, help='New buy threshold (formats: 1g23s45c, 50s, 100c, 2g)', required=False)
        thresh_parser.add_argument('--sell', type=str, help='New sell threshold (formats: 1g23s45c, 50s, 100c, 2g)', required=False)
        thresh_parser.set_defaults(func=lambda args: actionThreshold(args.id, args.buy, args.sell))
        
        config_parser = subparsers.add_parser('config', help='Configure settings')
        config_parser.set_defaults(func=lambda x: configure_settings())
        
        log_parser = subparsers.add_parser('logs', help='View historical logs')
        log_parser.set_defaults(func=lambda x: view_logs())
        
        clean_parser = subparsers.add_parser('clean', help='Clean all data')
        clean_parser.set_defaults(func=lambda x: clean_all_data())
        
        help_parser = subparsers.add_parser('help', help='Show usage instructions')
        help_parser.set_defaults(func=lambda x: show_help())
        
        args = parser.parse_args()
        if hasattr(args, 'func'):
            args.func(args)
        else:
            main_menu()