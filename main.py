import os
import json
import time
import threading
import pickle
import logging
import asyncio
import gdown
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- CONFIGURATION ---
BOT_TOKEN = "8598041078:AAFfACv-qzcwNOOGtB-azgVRvqewXrMttHM"  # आपका टोकन
ADMIN_ID_PLACEHOLDER = 0 # Admin ID यहाँ सेव होगा

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

SECRETS, LINKS, TITLE, DESC, HASHTAGS, INTERVAL, DELETE_CONFIRM = range(7)

CONFIG_FILE = 'config.json'
STATE_FILE = 'state.json'
CREDENTIALS_FILE = 'yt_credentials.pickle'
LINKS_FILE = 'links.txt'

upload_thread = None
stop_event = threading.Event()
# Global Application Instance
application = None

# --- Helper Functions (Same as before) ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_config(data):
    current = load_config()
    current.update(data)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(current, f, indent=4)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {"last_index": -1}

def save_state(index):
    with open(STATE_FILE, 'w') as f:
        json.dump({"last_index": index}, f)

def get_admin_id():
    conf = load_config()
    return conf.get('admin_id', ADMIN_ID_PLACEHOLDER)

# Decorator to check if the user is the admin
def check_admin(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        admin_id = get_admin_id()
        if admin_id == ADMIN_ID_PLACEHOLDER:
             # If admin_id is not set, allow the first user to run /start
             if func.__name__ == 'start':
                return await func(update, context, *args, **kwargs)
        
        if user_id == admin_id:
            return await func(update, context, *args, **kwargs)
        else:
            await update.message.reply_text("⛔ Unauthorized access.")
            return ConversationHandler.END
    return wrapper


# --- YouTube Auth Logic (RENDER Optimized) ---

def get_authenticated_service():
    """Handles YouTube OAuth using InstalledAppFlow's Local Server/Browser flow, reading secrets from ENV."""
    credentials = None
    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE, 'rb') as token:
            credentials = pickle.load(token)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            secrets_data = os.environ.get('CLIENT_SECRETS_JSON')
            if not secrets_data:
                raise EnvironmentError("Missing CLIENT_SECRETS_JSON environment variable. Please set it in Render dashboard.")

            secrets_dict = json.loads(secrets_data)
            
            flow = InstalledAppFlow.from_client_secrets_config(
                secrets_dict,
                scopes=['https://www.googleapis.com/auth/youtube.upload']
            )
            
            print("\n" + "="*50)
            print("Action Required: A browser window will open for authorization.")
            print("="*50 + "\n")
            
            credentials = flow.run_local_server(port=0) 

        with open(CREDENTIALS_FILE, 'wb') as token:
            pickle.dump(credentials, token)

    return build('youtube', 'v3', credentials=credentials)

# --- Worker Job ---

async def worker_job(application: Application):
    logger.info("Worker thread started.")
    # This job runs in the background thread created by start_upload
    
    config = load_config()
    admin_id = config.get('admin_id')
    bot = application.bot
    
    while not stop_event.is_set():
        try:
            config = load_config()
            state = load_state()
            current_index = state['last_index'] + 1
            
            if not os.path.exists(LINKS_FILE):
                await bot.send_message(admin_id, "❌ Error: links.txt not found.")
                break
                
            with open(LINKS_FILE, 'r') as f:
                links = [l.strip() for l in f.readlines() if l.strip()]
            
            if current_index >= len(links):
                await bot.send_message(admin_id, "🎉 All videos uploaded! Stopping worker.")
                break

            url = links[current_index]
            
            await bot.send_message(admin_id, f"📥 Downloading Video #{current_index + 1}...")
            # Blocking call (gdown) should be run in a separate thread if possible, but for simplicity, we keep it here.
            file_path = download_video(url, current_index + 1) 
            
            if not file_path:
                await bot.send_message(admin_id, f"❌ Failed to download Video #{current_index + 1}. Retrying in 5 mins.")
                # Use stop_event to check interruption during sleep
                await asyncio.sleep(300) 
                continue

            await bot.send_message(admin_id, f"📤 Uploading to YouTube...")
            
            title = config.get('title_tmpl', 'Short #{num}').replace('{num}', str(current_index + 1))
            desc = config.get('desc_tmpl', 'Video #{num}').replace('{num}', str(current_index + 1))
            tags = config.get('hashtags', '').split()
            
            # Blocking call (upload_to_youtube) should be run in a separate thread
            vid_id = upload_to_youtube(file_path, title, desc, tags)
            
            link = f"https://youtube.com/shorts/{vid_id}"
            await bot.send_message(admin_id, f"✅ Success! Uploaded: {link}")
            
            save_state(current_index)
            
            if config.get('delete_after', False):
                if os.path.exists(file_path):
                    os.remove(file_path)
            
            interval_hours = float(config.get('interval', 12))
            logger.info(f"Sleeping for {interval_hours} hours...")
            
            sleep_seconds = interval_hours * 3600
            slept = 0
            while slept < sleep_seconds and not stop_event.is_set():
                await asyncio.sleep(10)
                slept += 10
                
        except Exception as e:
            logger.error(f"Worker Error: {e}")
            await bot.send_message(admin_id, f"⚠️ Error in worker: {str(e)}\nRetrying in 5 mins.")
            # Use stop_event to check interruption during sleep
            await asyncio.sleep(300) 

# Note: Download and upload functions remain synchronous helpers for simplicity
def download_video(url, index):
    try:
        output_filename = f"video_{index}.mp4"
        file_path = gdown.download(url, output_filename, quiet=False, fuzzy=True)
        return file_path
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None

def upload_to_youtube(file_path, title, desc, tags):
    youtube = get_authenticated_service()
    # ... (rest of the upload logic is the same)
    
    body = {
        'snippet': {
            'title': title,
            'description': desc,
            'tags': tags,
            'categoryId': '22'
        },
        'status': {
            'privacyStatus': 'public',
            'selfDeclaredMadeForKids': False
        }
    }

    media = MediaFileUpload(file_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
    
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Uploaded {int(status.progress() * 100)}%")
    
    return response.get('id')


# --- Telegram Handlers (v20+ Async) ---

@check_admin
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    config = load_config()
    
    if 'admin_id' not in config:
        save_config({'admin_id': user_id})
        await update.message.reply_text(f"👑 Admin ID set to {user_id}.")
    
    await update.message.reply_text(
        "⚙️ **Setup Wizard**\nUpload `client_secrets.json` now (Though data is read from Render ENV).",
        parse_mode='Markdown'
    )
    return SECRETS

@check_admin
async def receive_secrets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # This step is just for conversation flow continuity
    await update.message.reply_text("✅ Secrets received! (Make sure to set CLIENT_SECRETS_JSON in Render ENV)\n\nNow upload `links.txt` (One link per line).")
    return LINKS

@check_admin
async def receive_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    file = await update.message.document.get_file()
    await file.download_to_drive(LINKS_FILE)
    await update.message.reply_text("✅ Links saved!\n\nEnter Title Template (use {num} for numbering).")
    return TITLE

@check_admin
async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    save_config({'title_tmpl': update.message.text})
    await update.message.reply_text("Enter Description Template (use {num} for numbering):")
    return DESC

@check_admin
async def receive_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    save_config({'desc_tmpl': update.message.text})
    await update.message.reply_text("Enter Hashtags (space separated):")
    return HASHTAGS

@check_admin
async def receive_hashtags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    save_config({'hashtags': update.message.text})
    await update.message.reply_text("Enter Upload Interval (hours):")
    return INTERVAL

@check_admin
async def receive_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        val = float(update.message.text)
        save_config({'interval': val})
        await update.message.reply_text("Delete file after upload? (yes/no):")
        return DELETE_CONFIRM
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return INTERVAL

@check_admin
async def receive_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.lower()
    save_config({'delete_after': 'yes' in text})
    await update.message.reply_text("✅ Setup Complete! Use /start_upload to begin.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Setup canceled.")
    return ConversationHandler.END

@check_admin
async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global stop_event
    
    # Check if worker is already running (v20+ uses background tasks)
    tasks = application.limiter.running_tasks
    if any("worker_job" in task.get_name() for task in tasks):
         await update.message.reply_text("⚠️ Worker is already running.")
         return

    stop_event.clear()
    
    try:
        await update.message.reply_text("🔑 Checking YouTube Authentication... A browser will open for authorization.")
        # Running the blocking Auth function in a thread to keep the bot responsive
        await application.loop.run_in_executor(None, get_authenticated_service)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Auth Error: {e}\nCheck the Render logs!")
        return

    # Start the worker job as a persistent background task (this is how the scheduler runs 24/7)
    application.create_task(worker_job(application), name="worker_job")
    await update.message.reply_text("🚀 Upload Worker Started!")

@check_admin
async def stop_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global stop_event
    stop_event.set()
    await update.message.reply_text("🛑 Stopping worker...")

async def post_init(application: Application):
    # This runs once after initialization
    global ADMIN_ID_PLACEHOLDER
    ADMIN_ID_PLACEHOLDER = get_admin_id()


def main():
    global application
    
    # Use ApplicationBuilder for v20+
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SECRETS: [MessageHandler(filters.Document.ALL, receive_secrets)],
            LINKS: [MessageHandler(filters.Document.ALL, receive_links)],
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_desc)],
            HASHTAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_hashtags)],
            INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_interval)],
            DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_delete)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('start_upload', start_upload))
    application.add_handler(CommandHandler('stop_upload', stop_upload))

    print(f"Bot Started on Token: {BOT_TOKEN[:10]}...")
    
    # Run the bot
    application.run_polling()

if __name__ == '__main__':
    main()