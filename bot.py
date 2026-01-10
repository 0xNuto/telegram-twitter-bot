import asyncio
import re
import sqlite3
from datetime import datetime
import os
from telethon import TelegramClient, events, Button
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# ============ CONFIGURATION ============
TELEGRAM_API_ID = int(os.getenv('TELEGRAM_API_ID', '37901087'))
TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH', '8cee27b5b47189cebcb36c2a3f1079b3')
TELEGRAM_PHONE = os.getenv('TELEGRAM_PHONE', '+2348062550412')
BOT_TOKEN = os.getenv('BOT_TOKEN', '8561851119:AAHH7_fpn4dJgW6F6oqmzwmWxsr9ypGmiE4')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '1356477152'))

# Check interval in seconds
CHECK_INTERVAL = 2  # 2 seconds (safer with browser automation)

# Twitter mirror instances
TWITTER_MIRRORS = [
    'https://xcancel.com',
    
]

# ============ DATABASE SETUP ============
def init_db():
    """Initialize SQLite database"""
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS monitored_accounts
                 (username TEXT PRIMARY KEY, added_at TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS detected_links
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tweet_id TEXT,
                  username TEXT,
                  tweet_text TEXT,
                  telegram_link TEXT,
                  detected_at TEXT,
                  status TEXT,
                  joined_at TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS processed_tweets
                 (tweet_id TEXT PRIMARY KEY, username TEXT, processed_at TEXT)''')
    
    conn.commit()
    conn.close()

# ============ HELPER FUNCTIONS ============
def get_monitored_accounts():
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT username FROM monitored_accounts")
    accounts = [row[0] for row in c.fetchall()]
    conn.close()
    return accounts

def add_monitored_account(username):
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
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM monitored_accounts WHERE username = ?", (username,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def is_tweet_processed(tweet_id):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM processed_tweets WHERE tweet_id = ?", (tweet_id,))
    result = c.fetchone() is not None
    conn.close()
    return result

def mark_tweet_processed(tweet_id, username):
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
    """Extract Telegram links from text"""
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
    
    return list(set(links))

async def join_telegram_group(client, link):
    """Join a Telegram group/channel"""
    try:
        print(f"🔗 Attempting to join: {link}")
        
        if '+' in link:
            invite_hash = link.split('+')[-1]
            print(f"   Joining private group with hash: {invite_hash}")
            await client(ImportChatInviteRequest(invite_hash))
            print(f"   ✅ Successfully joined!")
            return True, None
            
        elif 'joinchat' in link:
            invite_hash = link.split('/')[-1]
            print(f"   Joining group with joinchat hash: {invite_hash}")
            await client(ImportChatInviteRequest(invite_hash))
            print(f"   ✅ Successfully joined!")
            return True, None
            
        else:
            username = link.split('/')[-1].replace('@', '')
            print(f"   Joining public channel: @{username}")
            entity = await client.get_entity(username)
            await client(JoinChannelRequest(entity))
            print(f"   ✅ Successfully joined!")
            return True, None
            
    except Exception as e:
        error_msg = str(e)
        print(f"   ❌ Error joining: {error_msg}")
        return False, error_msg

# ============ PLAYWRIGHT SCRAPER ============
class PlaywrightScraper:
    def __init__(self):
        self.browser = None
        self.context = None
        self.working_instance = TWITTER_MIRRORS[0]
        
    async def init_browser(self):
        """Initialize Playwright browser"""
        print("🌐 Initializing headless browser...")
        playwright = await async_playwright().start()
        self.browser = await playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        print("✅ Browser initialized")
    
    async def close_browser(self):
        """Close browser"""
        if self.browser:
            await self.browser.close()
    
    async def get_tweets(self, username):
        """Scrape tweets using Playwright (handles JavaScript)"""
        for instance in TWITTER_MIRRORS:
            try:
                url = f"{instance}/{username}"
                print(f"   🌐 Loading page: {url}")
                
                page = await self.context.new_page()
                
                # Go to page and wait for content to load
                await page.goto(url, wait_until='networkidle', timeout=30000)
                
                # Wait for tweets to appear (they load via JS)
                try:
                    await page.wait_for_selector('.timeline-item', timeout=10000)
                except:
                    print(f"   ⚠️  No tweets found at {instance}")
                    await page.close()
                    continue
                
                # Get the fully rendered HTML
                content = await page.content()
                await page.close()
                
                # Parse with BeautifulSoup
                soup = BeautifulSoup(content, 'html.parser')
                
                # Find all tweets
                tweets = []
                for item in soup.find_all(class_='timeline-item'):
                    # Extract tweet text
                    tweet_content = item.find(class_='tweet-content')
                    if not tweet_content:
                        continue
                    
                    tweet_text = tweet_content.get_text(strip=True)
                    
                    # Extract tweet ID from link
                    tweet_link = item.find('a', class_='tweet-link')
                    tweet_id = None
                    if tweet_link and 'href' in tweet_link.attrs:
                        href = tweet_link['href']
                        # Extract ID from URL like /username/status/1234567890
                        parts = href.split('/')
                        if 'status' in parts:
                            idx = parts.index('status')
                            if idx + 1 < len(parts):
                                tweet_id = parts[idx + 1]
                    
                    if tweet_text and tweet_id:
                        tweets.append({
                            'id': tweet_id,
                            'text': tweet_text
                        })
                
                if tweets:
                    print(f"   ✅ Found {len(tweets)} tweets from {instance}")
                    self.working_instance = instance
                    return tweets
                else:
                    print(f"   ⚠️  No tweets extracted from {instance}")
                    
            except Exception as e:
                print(f"   ❌ Error with {instance}: {str(e)[:100]}")
                continue
        
        print(f"   ❌ Failed to fetch tweets from all instances")
        return None

# ============ BOT COMMANDS ============
async def start_command(event, bot):
    buttons = [
        [Button.inline("📊 Stats", b"stats"), Button.inline("📋 List", b"list")],
        [Button.inline("➕ Add", b"add_prompt"), Button.inline("➖ Remove", b"remove_prompt")]
    ]
    
    await event.respond(
        "🤖 **Twitter to Telegram Auto-Join Bot**\n"
        "🎭 **PLAYWRIGHT MODE - JavaScript Support!**\n\n"
        "Using headless browser to handle JS-loaded content.\n\n"
        "**Commands:**\n"
        "/add @username - Add account\n"
        "/remove @username - Remove account\n"
        "/list - Show monitored accounts\n"
        "/stats - View statistics\n"
        "/test @username - Test scraping\n"
        "/help - Show this message",
        buttons=buttons
    )

async def add_command(event, bot):
    try:
        username = event.message.text.split()[1].replace('@', '')
        if add_monitored_account(username):
            await event.respond(f"✅ Added @{username}\n🎭 Browser-based monitoring active!")
        else:
            await event.respond(f"⚠️ @{username} is already monitored")
    except IndexError:
        await event.respond("Usage: /add @username")

async def remove_command(event, bot):
    try:
        username = event.message.text.split()[1].replace('@', '')
        if remove_monitored_account(username):
            await event.respond(f"✅ Removed @{username}")
        else:
            await event.respond(f"⚠️ @{username} was not monitored")
    except IndexError:
        await event.respond("Usage: /remove @username")

async def list_command(event, bot):
    accounts = get_monitored_accounts()
    if accounts:
        account_list = '\n'.join([f"• @{acc}" for acc in accounts])
        await event.respond(f"**Monitored Accounts ({len(accounts)}):**\n\n{account_list}")
    else:
        await event.respond("No accounts monitored.")

async def stats_command(event, bot):
    stats = get_stats()
    accounts = get_monitored_accounts()
    monitoring = "🟢 Active" if len(accounts) > 0 else "🔴 Inactive"
    
    await event.respond(
        f"**📊 Statistics**\n\n"
        f"Status: {monitoring}\n"
        f"Monitored: {stats['monitored']}\n"
        f"Links found: {stats['total_links']}\n"
        f"Joined: {stats['joined']}\n"
        f"Failed: {stats['failed']}"
    )

async def test_command(event, bot, scraper):
    try:
        username = event.message.text.split()[1].replace('@', '')
        await event.respond(f"🧪 Testing browser scrape for @{username}...")
        
        tweets = await scraper.get_tweets(username)
        
        if not tweets:
            await event.respond(f"❌ Could not fetch tweets for @{username}")
        else:
            preview = "\n\n".join([f"• {t['text'][:80]}..." for t in tweets[:3]])
            await event.respond(f"✅ Found {len(tweets)} tweets!\n\n{preview}")
            
            # Check for Telegram links
            links_found = []
            for tweet in tweets:
                telegram_links = extract_telegram_links(tweet['text'])
                links_found.extend(telegram_links)
            
            if links_found:
                await event.respond(f"🎯 Found {len(links_found)} Telegram link(s):\n" + "\n".join(links_found))
            else:
                await event.respond(f"ℹ️ No Telegram links in recent tweets")
                
    except IndexError:
        await event.respond("Usage: /test @username")

# ============ MONITORING LOOP ============
async def monitoring_loop(bot, user_client, scraper):
    print("🎭 Monitoring loop started with Playwright!\n")
    
    while True:
        try:
            accounts = get_monitored_accounts()
            
            if len(accounts) == 0:
                await asyncio.sleep(10)
                continue
            
            print(f"\n{'='*60}")
            print(f"🔍 Checking {len(accounts)} account(s) at {datetime.now().strftime('%H:%M:%S')}")
            print(f"{'='*60}")
            
            for username in accounts:
                print(f"\n📡 Checking @{username}...")
                
                tweets = await scraper.get_tweets(username)
                
                if not tweets:
                    continue
                
                for tweet in tweets:
                    tweet_id = tweet['id']
                    tweet_text = tweet['text']
                    
                    if is_tweet_processed(tweet_id):
                        continue
                    
                    print(f"   📝 New tweet: {tweet_text[:100]}...")
                    
                    telegram_links = extract_telegram_links(tweet_text)
                    
                    if telegram_links:
                        print(f"   🎯 Found {len(telegram_links)} Telegram link(s)!")
                        
                        for link in telegram_links:
                            link_id = log_detected_link(tweet_id, username, tweet_text, link)
                            
                            success, error = await join_telegram_group(user_client, link)
                            
                            if success:
                                update_link_status(link_id, 'joined', datetime.now().isoformat())
                                await bot.send_message(
                                    ADMIN_USER_ID,
                                    f"✅ **Joined new group!**\n\nFrom: @{username}\nLink: {link}"
                                )
                            else:
                                update_link_status(link_id, 'failed')
                                await bot.send_message(
                                    ADMIN_USER_ID,
                                    f"❌ **Failed to join**\n\nFrom: @{username}\nError: {error}"
                                )
                            
                            await asyncio.sleep(2)
                    
                    mark_tweet_processed(tweet_id, username)
            
            print(f"\n💤 Sleeping for {CHECK_INTERVAL} seconds...\n")
            await asyncio.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            print(f"❌ Error: {e}")
            await asyncio.sleep(60)

# ============ MAIN ============
async def main():
    print("="*60)
    print("Twitter to Telegram Bot - PLAYWRIGHT MODE")
    print("="*60)
    
    init_db()
    
    bot = TelegramClient('bot_session', TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    
    user_client = TelegramClient('user_session', TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await user_client.start(phone=TELEGRAM_PHONE)
    
    scraper = PlaywrightScraper()
    await scraper.init_browser()
    
    print("\n✓ Bot started!\n")
    
    @bot.on(events.NewMessage(pattern='/start', from_users=ADMIN_USER_ID))
    async def _(e): await start_command(e, bot)
    
    @bot.on(events.NewMessage(pattern='/add', from_users=ADMIN_USER_ID))
    async def _(e): await add_command(e, bot)
    
    @bot.on(events.NewMessage(pattern='/remove', from_users=ADMIN_USER_ID))
    async def _(e): await remove_command(e, bot)
    
    @bot.on(events.NewMessage(pattern='/list', from_users=ADMIN_USER_ID))
    async def _(e): await list_command(e, bot)
    
    @bot.on(events.NewMessage(pattern='/stats', from_users=ADMIN_USER_ID))
    async def _(e): await stats_command(e, bot)
    
    @bot.on(events.NewMessage(pattern='/test', from_users=ADMIN_USER_ID))
    async def _(e): await test_command(e, bot, scraper)
    
    @bot.on(events.NewMessage(pattern='/help', from_users=ADMIN_USER_ID))
    async def _(e): await start_command(e, bot)
    
    @bot.on(events.CallbackQuery())
    async def _(event):
        if event.sender_id != ADMIN_USER_ID:
            return
        data = event.data.decode('utf-8')
        if data == "stats":
            await stats_command(event, bot)
        elif data == "list":
            await list_command(event, bot)
        elif data == "add_prompt":
            await event.respond("Send: /add @username")
        elif data == "remove_prompt":
            await event.respond("Send: /remove @username")
        await event.answer()
    
    monitoring_task = asyncio.create_task(monitoring_loop(bot, user_client, scraper))
    
    await bot.run_until_disconnected()
    await scraper.close_browser()

if __name__ == '__main__':
    asyncio.run(main())
