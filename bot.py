import asyncio
import re
import sqlite3 
from datetime import datetime
import os
from telethon import TelegramClient, events, Button
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest
import feedparser
import requests
from bs4 import BeautifulSoup

# ============ CONFIGURATION ============
# Telegram API credentials - Can be set via environment variables or directly
TELEGRAM_API_ID = int(os.getenv('TELEGRAM_API_ID', '37901087'))
TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH', '8cee27b5b47189cebcb36c2a3f1079b3')
TELEGRAM_PHONE = os.getenv('TELEGRAM_PHONE', '+2348062550412')

# Telegram BOT credentials
BOT_TOKEN = os.getenv('BOT_TOKEN', '8561851119:AAHH7_fpn4dJgW6F6oqmzwmWxsr9ypGmiE4')

# Your Telegram user ID
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '1356477152'))

# Check interval in seconds (RSS checks)
CHECK_INTERVAL = 2  # Check every 2 seconds - BLAZING FAST!

# Nitter instances (Twitter mirrors that provide RSS)
NITTER_INSTANCES = [
    'https://xcancel.com',
]

# ============ DATABASE SETUP ============
def init_db():
    """Initialize SQLite database"""
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    
    # Monitored accounts table
    c.execute('''CREATE TABLE IF NOT EXISTS monitored_accounts
                 (username TEXT PRIMARY KEY, added_at TEXT)''')
    
    # Detected links table
    c.execute('''CREATE TABLE IF NOT EXISTS detected_links
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tweet_id TEXT,
                  username TEXT,
                  tweet_text TEXT,
                  telegram_link TEXT,
                  detected_at TEXT,
                  status TEXT,
                  joined_at TEXT)''')
    
    # Processed tweets table (to avoid duplicates)
    c.execute('''CREATE TABLE IF NOT EXISTS processed_tweets
                 (tweet_id TEXT PRIMARY KEY, username TEXT, processed_at TEXT)''')
    
    conn.commit()
    conn.close()

# ============ HELPER FUNCTIONS ============
def get_monitored_accounts():
    """Get list of monitored Twitter accounts"""
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT username FROM monitored_accounts")
    accounts = [row[0] for row in c.fetchall()]
    conn.close()
    return accounts

def add_monitored_account(username):
    """Add Twitter account to monitoring list"""
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    try:
        c.execute("INSERT INTO monitored_accounts VALUES (?, ?)",
                 (username, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False

def remove_monitored_account(username):
    """Remove Twitter account from monitoring"""
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM monitored_accounts WHERE username = ?", (username,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def is_tweet_processed(tweet_id):
    """Check if tweet was already processed"""
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM processed_tweets WHERE tweet_id = ?", (tweet_id,))
    result = c.fetchone() is not None
    conn.close()
    return result

def mark_tweet_processed(tweet_id, username):
    """Mark tweet as processed"""
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    try:
        c.execute("INSERT INTO processed_tweets VALUES (?, ?, ?)",
                 (tweet_id, username, datetime.now().isoformat()))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

def log_detected_link(tweet_id, username, tweet_text, telegram_link):
    """Log detected Telegram link"""
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("""INSERT INTO detected_links 
                 (tweet_id, username, tweet_text, telegram_link, detected_at, status)
                 VALUES (?, ?, ?, ?, ?, ?)""",
             (tweet_id, username, tweet_text, telegram_link, 
              datetime.now().isoformat(), 'pending'))
    link_id = c.lastrowid
    conn.commit()
    conn.close()
    return link_id

def update_link_status(link_id, status, joined_at=None):
    """Update status of detected link"""
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    if joined_at:
        c.execute("UPDATE detected_links SET status = ?, joined_at = ? WHERE id = ?",
                 (status, joined_at, link_id))
    else:
        c.execute("UPDATE detected_links SET status = ? WHERE id = ?", (status, link_id))
    conn.commit()
    conn.close()

def get_stats():
    """Get statistics"""
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM monitored_accounts")
    monitored = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM detected_links")
    total_links = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM detected_links WHERE status = 'joined'")
    joined = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM detected_links WHERE status = 'failed'")
    failed = c.fetchone()[0]
    
    conn.close()
    return {
        'monitored': monitored,
        'total_links': total_links,
        'joined': joined,
        'failed': failed
    }

def extract_telegram_links(text):
    """Extract Telegram group/channel links from text"""
    if not text:
        return []
    
    patterns = [
        r'https?://t\.me/\+[a-zA-Z0-9_-]+',
        r'https?://t\.me/joinchat/[a-zA-Z0-9_-]+',
        r'https?://t\.me/[a-zA-Z0-9_]+',
        r't\.me/\+[a-zA-Z0-9_-]+',
        r't\.me/joinchat/[a-zA-Z0-9_-]+',
        r't\.me/[a-zA-Z0-9_]+',
    ]
    
    links = []
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            if not match.startswith('http'):
                match = 'https://' + match
            links.append(match)
    
    unique_links = list(set(links))
    valid_links = [link for link in unique_links if len(link.split('/')[-1]) > 0]
    
    return valid_links

async def join_telegram_group(client, link):
    """Join a Telegram group/channel"""
    try:
        print(f"🔗 Attempting to join: {link}")
        
        if '+' in link:
            invite_hash = link.split('+')[-1]
            print(f"   Joining private group with hash: {invite_hash}")
            result = await client(ImportChatInviteRequest(invite_hash))
            print(f"   ✅ Successfully joined!")
            return True, None
            
        elif 'joinchat' in link:
            invite_hash = link.split('/')[-1]
            print(f"   Joining group with joinchat hash: {invite_hash}")
            result = await client(ImportChatInviteRequest(invite_hash))
            print(f"   ✅ Successfully joined!")
            return True, None
            
        else:
            username = link.split('/')[-1].replace('@', '')
            print(f"   Joining public channel: @{username}")
            entity = await client.get_entity(username)
            result = await client(JoinChannelRequest(entity))
            print(f"   ✅ Successfully joined!")
            return True, None
            
    except Exception as e:
        error_msg = str(e)
        print(f"   ❌ Error joining: {error_msg}")
        return False, error_msg

# ============ RSS FEED MONITOR ============
class RSSMonitor:
    def __init__(self):
        self.working_instance = None
        self.find_working_instance()
    
    def find_working_instance(self):
        """Find a working Nitter instance with detailed diagnostics"""
        print("\n🔍 Finding working Nitter instance...")
        print(f"   Testing {len(NITTER_INSTANCES)} instances...\n")
        
        for i, instance in enumerate(NITTER_INSTANCES, 1):
            try:
                print(f"   [{i}/{len(NITTER_INSTANCES)}] Trying: {instance}")
                response = requests.get(
                    f"{instance}/elonmusk", 
                    timeout=10, 
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
                    allow_redirects=True
                )
                
                # Check response status
                if response.status_code == 200:
                    # Verify it's actually working by checking content
                    if 'tweets' in response.text.lower() or 'timeline' in response.text.lower():
                        self.working_instance = instance
                        print(f"   ✅ SUCCESS: {instance} is working!\n")
                        return
                    else:
                        print(f"   ⚠️  {instance} returned 200 but content looks wrong (might be blocked)\n")
                elif response.status_code == 403:
                    print(f"   🚫 BLOCKED: {instance} returned 403 Forbidden (you may be IP blocked)\n")
                elif response.status_code == 429:
                    print(f"   ⏱️  RATE LIMITED: {instance} returned 429 Too Many Requests\n")
                elif response.status_code == 503:
                    print(f"   ⚠️  UNAVAILABLE: {instance} returned 503 Service Unavailable (instance down)\n")
                elif response.status_code == 404:
                    print(f"   ❌ NOT FOUND: {instance} returned 404 (instance might be dead)\n")
                else:
                    print(f"   ❌ FAILED: {instance} returned status {response.status_code}\n")
                    
            except requests.exceptions.Timeout:
                print(f"   ⏱️  TIMEOUT: {instance} took too long to respond (>10 seconds)\n")
            except requests.exceptions.ConnectionError as e:
                error_str = str(e)
                if 'refused' in error_str.lower():
                    print(f"   🚫 CONNECTION REFUSED: {instance} rejected the connection\n")
                elif 'timeout' in error_str.lower():
                    print(f"   ⏱️  CONNECTION TIMEOUT: {instance} didn't respond in time\n")
                elif 'name or service not known' in error_str.lower():
                    print(f"   ❌ DNS ERROR: {instance} domain doesn't exist or can't be resolved\n")
                else:
                    print(f"   ❌ CONNECTION ERROR: {instance} - {str(e)[:80]}\n")
            except requests.exceptions.SSLError:
                print(f"   🔒 SSL ERROR: {instance} has SSL/certificate issues\n")
            except Exception as e:
                print(f"   ❌ UNKNOWN ERROR: {instance} - {type(e).__name__}: {str(e)[:80]}\n")
                continue
        
        print("="*60)
        print("❌ NO WORKING NITTER INSTANCE FOUND")
        print("="*60)
        print("\n📋 Possible reasons:")
        print("   1. All Nitter instances are currently down")
        print("   2. Your IP might be blocked by Nitter instances")
        print("   3. Your internet connection has restrictions")
        print("   4. Cloudflare or other protection is blocking requests")
        print("\n💡 Suggestions:")
        print("   • Try again in 10-15 minutes")
        print("   • Check if you can access any Nitter site in your browser")
        print("   • Consider using a VPN if you're blocked")
        print("   • Switch to Twitter API mode for more reliability")
        print("="*60 + "\n")
    
    def get_rss_feed(self, username):
        """Get RSS feed for a Twitter user via Nitter with detailed diagnostics"""
        if not self.working_instance:
            print("   ⚠️  No working instance available, searching for one...")
            self.find_working_instance()
            if not self.working_instance:
                return None
        
        # Try current instance first, then others if it fails
        instances_to_try = [self.working_instance] + [i for i in NITTER_INSTANCES if i != self.working_instance]
        
        for instance in instances_to_try[:3]:  # Try up to 3 instances
            try:
                rss_url = f"{instance}/{username}/rss"
                print(f"   📡 Fetching RSS: {rss_url}")
                
                response = requests.get(
                    rss_url, 
                    timeout=15,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Accept': 'application/rss+xml, application/xml, text/xml, */*',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Accept-Encoding': 'gzip, deflate, br',
                        'Connection': 'keep-alive',
                        'Referer': f'{instance}/'
                    }
                )
                
                if response.status_code == 200:
                    print(f"   ✅ Got response (200 OK), parsing feed...")
                    
                    # Try to parse the feed
                    feed = feedparser.parse(response.content)
                    
                    if feed.bozo and feed.bozo_exception:
                        print(f"   ⚠️  RSS parsing error: {feed.bozo_exception}")
                        continue
                    
                    if not feed.entries:
                        print(f"   ⚠️  Feed has no entries")
                        print(f"   💡 Possible reasons: private account, no tweets, or blocked")
                        
                        # Check if the response looks like a valid RSS feed
                        if b'<?xml' not in response.content[:100]:
                            print(f"   ⚠️  Response doesn't look like XML, might be blocked")
                            continue
                        else:
                            # Valid XML but no tweets - account might be empty or private
                            return []
                    
                    # Success!
                    print(f"   ✅ Successfully fetched {len(feed.entries)} tweets from {instance}")
                    self.working_instance = instance  # Update working instance
                    return feed.entries
                    
                elif response.status_code == 403:
                    print(f"   🚫 BLOCKED (403) at {instance}")
                    continue
                elif response.status_code == 404:
                    print(f"   ❌ NOT FOUND (404): User @{username} might not exist")
                    return None
                elif response.status_code == 429:
                    print(f"   ⏱️  RATE LIMITED (429) at {instance}")
                    continue
                elif response.status_code == 503:
                    print(f"   ⚠️  SERVICE UNAVAILABLE (503) at {instance}")
                    continue
                else:
                    print(f"   ❌ HTTP {response.status_code} from {instance}")
                    continue
                    
            except requests.exceptions.Timeout:
                print(f"   ⏱️  TIMEOUT at {instance}")
                continue
            except Exception as e:
                print(f"   ❌ ERROR with {instance}: {type(e).__name__}: {str(e)[:100]}")
                continue
        
        print(f"   ❌ Failed to fetch RSS from all attempted instances")
        print(f"   💡 Suggestion: Try a different Twitter account or wait a few minutes")
        return None
    
    def extract_tweet_id(self, link):
        """Extract tweet ID from link"""
        try:
            # Link format: https://nitter.net/username/status/1234567890
            return link.split('/')[-1].split('#')[0]
        except:
            return None

# ============ BOT COMMANDS ============
async def start_command(event, bot):
    """Handle /start command"""
    buttons = [
        [Button.inline("📊 View Stats", b"stats"), Button.inline("📋 List Accounts", b"list")],
        [Button.inline("➕ Add Account", b"add_prompt"), Button.inline("➖ Remove Account", b"remove_prompt")],
        [Button.inline("❓ Help", b"help")]
    ]
    
    await event.respond(
        "🤖 **Twitter to Telegram Auto-Join Bot**\n"
        "🌐 **RSS MODE - No API Required!**\n\n"
        "I monitor Twitter accounts via RSS and automatically join Telegram groups they post.\n\n"
        "✨ **Benefits:**\n"
        "• No Twitter API needed\n"
        "• No rate limits\n"
        "• Completely free\n"
        "• Checks every minute\n\n"
        "**Commands:**\n"
        "/add @username - Add account\n"
        "/remove @username - Remove account\n"
        "/list - Show monitored accounts\n"
        "/stats - View statistics\n"
        "/test @username - Test RSS for an account\n"
        "/help - Show this message",
        buttons=buttons
    )

async def add_command(event, bot):
    """Handle /add command"""
    try:
        username = event.message.text.split()[1].replace('@', '')
        
        if add_monitored_account(username):
            await event.respond(
                f"✅ Added @{username} to monitoring list\n"
                f"🌐 RSS monitoring active!\n\n"
                f"I'll check for new tweets every minute."
            )
        else:
            await event.respond(f"⚠️ @{username} is already being monitored")
    except IndexError:
        await event.respond("Usage: /add @username\n\nExample: /add elonmusk")
    except Exception as e:
        await event.respond(f"❌ Error: {str(e)}")

async def remove_command(event, bot):
    """Handle /remove command"""
    try:
        username = event.message.text.split()[1].replace('@', '')
        if remove_monitored_account(username):
            accounts = get_monitored_accounts()
            if accounts:
                await event.respond(f"✅ Removed @{username} from monitoring")
            else:
                await event.respond(f"✅ Removed @{username}\n🔴 No accounts left")
        else:
            await event.respond(f"⚠️ @{username} was not being monitored")
    except IndexError:
        await event.respond("Usage: /remove @username")

async def list_command(event, bot):
    """Handle /list command"""
    accounts = get_monitored_accounts()
    if accounts:
        account_list = '\n'.join([f"• @{acc}" for acc in accounts])
        await event.respond(
            f"**Monitored Accounts ({len(accounts)}):**\n"
            f"🌐 RSS monitoring active\n\n{account_list}"
        )
    else:
        await event.respond("No accounts being monitored. Use /add @username to start.")

async def test_command(event, bot, rss_monitor):
    """Test RSS fetching for a specific account"""
    try:
        username = event.message.text.split()[1].replace('@', '')
        
        await event.respond(f"🧪 Testing RSS fetch for @{username}...\n\nCheck your terminal for detailed logs!")
        
        print(f"\n{'='*60}")
        print(f"🧪 MANUAL TEST FOR @{username}")
        print(f"{'='*60}")
        
        entries = rss_monitor.get_rss_feed(username)
        
        if entries is None:
            await event.respond(f"❌ Failed to fetch RSS for @{username}\n\nCheck terminal for details.")
        elif len(entries) == 0:
            await event.respond(f"⚠️ RSS feed is empty for @{username}\n\nAccount might be private or have no tweets.")
        else:
            # Show first few tweets
            tweet_preview = "\n\n".join([f"• {entry.title[:100]}" for entry in entries[:3]])
            await event.respond(
                f"✅ Successfully fetched {len(entries)} tweets!\n\n"
                f"**First 3 tweets:**\n{tweet_preview}\n\n"
                f"Scanning for Telegram links..."
            )
            
            # Check for Telegram links
            links_found = []
            for entry in entries[:10]:
                tweet_text = entry.title + " " + entry.description
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(tweet_text, 'html.parser')
                tweet_text = soup.get_text()
                
                telegram_links = extract_telegram_links(tweet_text)
                if telegram_links:
                    links_found.extend(telegram_links)
            
            if links_found:
                links_text = "\n".join([f"• {link}" for link in links_found])
                await event.respond(f"🎯 Found {len(links_found)} Telegram link(s):\n\n{links_text}")
            else:
                await event.respond(f"ℹ️ No Telegram links found in recent tweets from @{username}")
        
        print(f"{'='*60}\n")
        
    except IndexError:
        await event.respond("Usage: /test @username\n\nExample: /test elonmusk")
    except Exception as e:
        await event.respond(f"❌ Error: {str(e)}")
    """Handle /stats command"""
    stats = get_stats()
    accounts = get_monitored_accounts()
    monitoring = "🟢 Active (RSS)" if len(accounts) > 0 else "🔴 Inactive"
    
    await event.respond(
        f"**📊 Statistics**\n\n"
        f"Status: {monitoring}\n"
        f"Monitored accounts: {stats['monitored']}\n"
        f"Total links found: {stats['total_links']}\n"
        f"Groups joined: {stats['joined']}\n"
        f"Failed joins: {stats['failed']}\n\n"
        f"🌐 RSS mode = No API limits!"
    )

# ============ MONITORING LOOP ============
async def monitoring_loop(bot, user_client, rss_monitor):
    """Main monitoring loop using RSS feeds"""
    print("🌐 RSS monitoring loop started!\n")
    
    while True:
        try:
            accounts = get_monitored_accounts()
            
            if len(accounts) == 0:
                await asyncio.sleep(10)
                continue
            
            print(f"\n{'='*60}")
            print(f"🔍 Checking {len(accounts)} account(s) via RSS at {datetime.now().strftime('%H:%M:%S')}")
            print(f"{'='*60}")
            
            for username in accounts:
                print(f"\n📡 Checking @{username}...")
                
                # Get RSS feed
                entries = rss_monitor.get_rss_feed(username)
                
                if not entries:
                    print(f"   ⚠️ Could not fetch RSS feed")
                    continue
                
                print(f"   Found {len(entries)} recent tweets")
                
                # Process each entry
                for entry in entries[:10]:  # Check last 10 tweets
                    tweet_id = rss_monitor.extract_tweet_id(entry.link)
                    
                    if not tweet_id or is_tweet_processed(tweet_id):
                        continue
                    
                    # Get tweet content
                    tweet_text = entry.title + " " + entry.description
                    
                    # Remove HTML tags
                    soup = BeautifulSoup(tweet_text, 'html.parser')
                    tweet_text = soup.get_text()
                    
                    print(f"   📝 New tweet: {tweet_text[:100]}...")
                    
                    # Extract Telegram links
                    telegram_links = extract_telegram_links(tweet_text)
                    
                    if telegram_links:
                        print(f"   🎯 Found {len(telegram_links)} Telegram link(s)!")
                        
                        for link in telegram_links:
                            print(f"\n   📍 Processing: {link}")
                            link_id = log_detected_link(tweet_id, username, tweet_text, link)
                            
                            # Join the group
                            success, error = await join_telegram_group(user_client, link)
                            
                            if success:
                                update_link_status(link_id, 'joined', datetime.now().isoformat())
                                await bot.send_message(
                                    ADMIN_USER_ID,
                                    f"✅ **Joined new group!**\n\n"
                                    f"From: @{username}\n"
                                    f"Link: {link}\n"
                                    f"Tweet: {tweet_text[:150]}..."
                                )
                            else:
                                update_link_status(link_id, 'failed')
                                await bot.send_message(
                                    ADMIN_USER_ID,
                                    f"❌ **Failed to join group**\n\n"
                                    f"From: @{username}\n"
                                    f"Link: {link}\n"
                                    f"Error: {error}"
                                )
                            
                            await asyncio.sleep(2)  # Rate limiting
                    
                    # Mark as processed
                    mark_tweet_processed(tweet_id, username)
            
            print(f"\n💤 Sleeping for {CHECK_INTERVAL} seconds...\n")
            await asyncio.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            print(f"❌ Error in monitoring loop: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(60)

# ============ MAIN FUNCTION ============
async def main():
    print("="*60)
    print("Twitter to Telegram Auto-Join Bot")
    print("🌐 RSS MODE - No API Required!")
    print("="*60)
    
    print("\nInitializing database...")
    init_db()
    print("✓ Database initialized")
    
    print("\nStarting Telegram bot...")
    bot = TelegramClient('bot_session', TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    print("✓ Telegram bot connected")
    
    user_client = TelegramClient('user_session', TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await user_client.start(phone=TELEGRAM_PHONE)
    print("✓ Telegram user client connected")
    
    print("\nInitializing RSS monitor...")
    rss_monitor = RSSMonitor()
    
    # Install required packages reminder
    try:
        import feedparser
        import bs4
    except ImportError:
        print("\n⚠️ Missing required packages!")
        print("Run: pip install feedparser beautifulsoup4 requests")
        return
    
    print("\n" + "="*60)
    print("✓ Bot started successfully!")
    print("="*60)
    print("\n📱 Message your bot to begin!\n")
    
    # Register command handlers
    @bot.on(events.NewMessage(pattern='/start', from_users=ADMIN_USER_ID))
    async def start_handler(event):
        await start_command(event, bot)
    
    @bot.on(events.NewMessage(pattern='/add', from_users=ADMIN_USER_ID))
    async def add_handler(event):
        await add_command(event, bot)
    
    @bot.on(events.NewMessage(pattern='/remove', from_users=ADMIN_USER_ID))
    async def remove_handler(event):
        await remove_command(event, bot)
    
    @bot.on(events.NewMessage(pattern='/list', from_users=ADMIN_USER_ID))
    async def list_handler(event):
        await list_command(event, bot)
    
    @bot.on(events.NewMessage(pattern='/stats', from_users=ADMIN_USER_ID))
    async def stats_handler(event):
        await stats_command(event, bot)
    
    @bot.on(events.NewMessage(pattern='/test', from_users=ADMIN_USER_ID))
    async def test_handler(event):
        await test_command(event, bot, rss_monitor)
    
    @bot.on(events.NewMessage(pattern='/help', from_users=ADMIN_USER_ID))
    async def help_handler(event):
        await start_command(event, bot)
    
    # Register callback handler
    @bot.on(events.CallbackQuery())
    async def callback_handler(event):
        # Only respond to the admin user
        if event.sender_id != ADMIN_USER_ID:
            return
            
        data = event.data.decode('utf-8')
        
        if data == "stats":
            await stats_command(event, bot)
            await event.answer()
        elif data == "list":
            await list_command(event, bot)
            await event.answer()
        elif data == "help":
            await start_command(event, bot)
            await event.answer()
        elif data == "add_prompt":
            await event.answer()
            await event.respond("Send me the Twitter username to add:\n\nExample: /add elonmusk")
        elif data == "remove_prompt":
            await event.answer()
            await event.respond("Send me the Twitter username to remove:\n\nExample: /remove elonmusk")
    
    # Start monitoring loop
    monitoring_task = asyncio.create_task(monitoring_loop(bot, user_client, rss_monitor))
    
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
