import asyncio
import os
import re
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait,
    UsernameInvalid,
    UsernameOccupied,
    RPCError,
    SessionPasswordNeeded,
    PhoneCodeInvalid,
    PasswordHashInvalid
)

# Environment Variables
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Telegram Bot Client
app = Client(
    "sniper_bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# In-memory Storage
checkers = {}     # {user_id: [Client1, Client2, ...]}
keepers = {}      # {user_id: Client_Keeper}
wordings = {}     # {user_id: text}
user_states = {}  # {user_id: {"step": ..., "data": ...}}

# ----------------------------------------------------
# 1. Generator Variasi Username
# ----------------------------------------------------
def gen_tamdal(base: str) -> list:
    res = []
    for i in range(1, len(base)):
        for c in "abcdefghijklmnopqrstuvwxyz":
            res.append(base[:i] + c + base[i:])
    return res

def gen_tamping(base: str) -> list:
    res = []
    for c in "abcdefghijklmnopqrstuvwxyz":
        res.append(c + base)
        res.append(base + c)
    return res

def gen_ganhur(base: str) -> list:
    res = []
    for i in range(len(base)):
        for c in "abcdefghijklmnopqrstuvwxyz":
            if c != base[i]:
                res.append(base[:i] + c + base[i+1:])
    return res

def gen_kurhur(base: str) -> list:
    res = []
    for i in range(len(base)):
        res.append(base[:i] + base[i+1:])
    return res

def gen_sop(base: str) -> list:
    res = []
    for i in range(len(base)):
        res.append(base[:i] + base[i] + base[i:])
    return res

def generate_usernames(category: str, base: str) -> list:
    category = category.lower().strip()
    base = base.lower().strip()
    
    variations = []
    if category == "tamping":
        variations = gen_tamping(base)
    elif category == "ganhur":
        variations = gen_ganhur(base)
    elif category == "mulchar":
        variations = gen_tamdal(base)
    elif category == "sop":
        variations = gen_sop(base)
    elif category == "idol":
        variations = (
            gen_tamdal(base) + 
            gen_tamping(base) + 
            gen_kurhur(base) + 
            gen_ganhur(base) + 
            gen_sop(base)
        )
    
    valid_usns = []
    for u in set(variations):
        if len(u) >= 5 and re.match(r"^[a-zA-Z0-9_]+$", u):
            valid_usns.append(u)
            
    return valid_usns

# ----------------------------------------------------
# 2. Handlers Command Utama
# ----------------------------------------------------
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    await message.reply_text(
        "👋 **Welcome to Telegram Username Checker & Sniper Bot**\n\n"
        "Available Commands:\n"
        "📱 `/login` - Add Checker Account\n"
        "🛡 `/keeper` - Add Keeper Account (For Claiming)\n"
        "📝 `/addcp [Text]` - Set Wording Jualan\n"
        "🗑 `/clearnoktel` - Clear Phone Numbers\n"
        "🚀 `/check` - Start Auto-Sniper (Once / Loop)\n"
        "🎯 `/keep [usn]` - Manual Claim via Keeper\n"
        "🛑 `/stop` - Pause Checker"
    )

@app.on_message(filters.command("login") & filters.private)
async def login_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    user_states[user_id] = {"step": "LOGIN_PHONE", "type": "checker"}
    await message.reply_text("📱 **Tambah Akun Checker**\nSilakan kirimkan nomor telepon akun (Format: `+628xxx`):")

@app.on_message(filters.command("keeper") & filters.private)
async def keeper_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    user_states[user_id] = {"step": "LOGIN_PHONE", "type": "keeper"}
    await message.reply_text("🛡 **Tambah Akun Keeper**\nSilakan kirimkan nomor telepon akun Keeper (Format: `+628xxx`):")

@app.on_message(filters.command("addcp") & filters.private)
async def addcp_cmd(client: Client, message: Message):
    text = message.text.split(" ", 1)
    if len(text) < 2:
        await message.reply_text("❌ Usage: `/addcp [Your wording text]`")
        return
    wordings[message.from_user.id] = text[1]
    await message.reply_text("✅ Wording jualan successfully saved!")

@app.on_message(filters.command("check") & filters.private)
async def check_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in checkers or not checkers[user_id]:
        await message.reply_text("⚠️ Please login at least 1 Checker account first using `/login`!")
        return

    user_states[user_id] = {"step": "SELECT_MODE"}
    await message.reply_text(
        "🚀 **PILIH MODE AUTO-SNIPER:**\n\n"
        "Ketik angka pilihanmu:\n"
        "1️⃣ Sekali Selesai (Scan 1 putaran lalu berhenti)\n"
        "2️⃣ Looping Terus (Scan berputar terus menerus sampai /stop)"
    )

@app.on_message(filters.command("stop") & filters.private)
async def stop_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in user_states:
        user_states[user_id]["active"] = False
        await message.reply_text("🛑 Auto-sniper paused/stopped.")
    else:
        await message.reply_text("⚠️ Tidak ada proses checker yang sedang berjalan.")

# ----------------------------------------------------
# 3. Interactive Login & Process Input Handler
# ----------------------------------------------------
@app.on_message(filters.text & filters.private & ~filters.command(["start", "addcp", "check", "stop", "login", "keeper", "keep", "clearnoktel"]))
async def handle_inputs(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in user_states:
        return

    state_info = user_states[user_id]
    step = state_info.get("step")

    # --- LOGIN STEP 1: PHONE NUMBER ---
    if step == "LOGIN_PHONE":
        phone = message.text.strip().replace(" ", "")
        user_type = state_info.get("type", "checker")
        
        user_client = Client(
            f"user_{user_id}_{phone.replace('+', '')}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True
        )
        await user_client.connect()
        
        try:
            sent_code = await user_client.send_code(phone)
            user_states[user_id] = {
                "step": "LOGIN_OTP",
                "phone": phone,
                "phone_code_hash": sent_code.phone_code_hash,
                "client": user_client,
                "type": user_type
            }
            await message.reply_text("📩 Kode OTP telah dikirim ke Telegram kamu.\nKirimkan kode OTP ke sini (Contoh: `12345`):")
        except Exception as e:
            await user_client.disconnect()
            user_states.pop(user_id, None)
            await message.reply_text(f"❌ Gagal mengirim OTP: {e}")

    # --- LOGIN STEP 2: OTP CODE ---
    elif step == "LOGIN_OTP":
        otp = message.text.strip().replace(" ", "")
        user_client = state_info["client"]
        phone = state_info["phone"]
        phone_code_hash = state_info["phone_code_hash"]
        user_type = state_info["type"]

        try:
            await user_client.sign_in(phone, phone_code_hash, otp)
            await finalize_login(user_id, user_client, user_type, message)
        except SessionPasswordNeeded:
            user_states[user_id]["step"] = "LOGIN_2FA"
            await message.reply_text("🔐 Akun ini menggunakan Verifikasi 2-Langkah (2FA).\nSilakan kirimkan password 2FA kamu:")
        except PhoneCodeInvalid:
            await message.reply_text("❌ Kode OTP salah! Silakan coba ketik ulang kode OTP:")
        except Exception as e:
            await user_client.disconnect()
            user_states.pop(user_id, None)
            await message.reply_text(f"❌ Gagal login: {e}")

    # --- LOGIN STEP 3: 2FA PASSWORD ---
    elif step == "LOGIN_2FA":
        password = message.text.strip()
        user_client = state_info["client"]
        user_type = state_info["type"]

        try:
            await user_client.check_password(password)
            await finalize_login(user_id, user_client, user_type, message)
        except PasswordHashInvalid:
            await message.reply_text("❌ Password 2FA salah! Silakan masukan password yang benar:")
        except Exception as e:
            await user_client.disconnect()
            user_states.pop(user_id, None)
            await message.reply_text(f"❌ Gagal login: {e}")

    # --- CHECKER STEP: MODE & TARGETS ---
    elif step == "SELECT_MODE":
        choice = message.text.strip()
        if choice in ["1", "2"]:
            user_states[user_id]["mode"] = choice
            user_states[user_id]["step"] = "WAIT_LIST"
            await message.reply_text(
                "📝 **Kirim list based-on kamu (Enter per baris):**\n\n"
                "Contoh:\n"
                "idol jaehyun\n"
                "mulchar kucing\n"
                "tamping claude"
            )
        else:
            await message.reply_text("❌ Input tidak valid. Ketik 1 atau 2.")

    elif step == "WAIT_LIST":
        lines = message.text.strip().split("\n")
        targets = []

        for line in lines:
            parts = line.strip().split(" ", 1)
            if len(parts) == 2:
                cat, base = parts[0], parts[1]
                generated = generate_usernames(cat, base)
                targets.extend(generated)

        targets = list(set(targets))
        if not targets:
            await message.reply_text("❌ Tidak ada username valid yang berhasil di-generate.")
            user_states.pop(user_id, None)
            return

        mode = user_states[user_id]["mode"]
        user_states[user_id]["step"] = "RUNNING"
        user_states[user_id]["active"] = True

        await message.reply_text(
            f"⚡️ **Pengecekan Dimulai!**\n"
            f"🔹 Total Target Variasi: `{len(targets)}` USN\n"
            f"🔹 Mode: {'Sekali Selesai' if mode == '1' else 'Looping Terus'}"
        )

        asyncio.create_task(run_checker_loop(user_id, message, targets, mode))

async def finalize_login(user_id: int, user_client: Client, user_type: str, message: Message):
    me = await user_client.get_me()
    if user_type == "checker":
        if user_id not in checkers:
            checkers[user_id] = []
        checkers[user_id].append(user_client)
        await message.reply_text(f"✅ Berhasil menambah Akun Checker: **{me.first_name}** (`@{me.username}`)!")
    else:
        keepers[user_id] = user_client
        await message.reply_text(f"✅ Berhasil menambah Akun Keeper: **{me.first_name}** (`@{me.username}`)!")
    
    user_states.pop(user_id, None)

# ----------------------------------------------------
# 4. Main Checker & Sniper Worker Loop
# ----------------------------------------------------
async def run_checker_loop(user_id: int, message: Message, targets: list, mode: str):
    user_checkers = checkers.get(user_id, [])
    keeper_client = keepers.get(user_id)
    
    checker_idx = 0
    
    while user_states.get(user_id, {}).get("active", False):
        for usn in targets:
            if not user_states.get(user_id, {}).get("active", False):
                break

            current_checker = user_checkers[checker_idx]
            checker_idx = (checker_idx + 1) % len(user_checkers)

            try:
                is_available = await current_checker.check_username(usn)
                if is_available:
                    await message.reply_text(f"🎯 **USERNAME AVAILABLE:** @{usn}")
                    
                    if keeper_client:
                        try:
                            await keeper_client.set_username(usn)
                            await message.reply_text(f"🔥 **SUCCESSFULLY CLAIMED:** @{usn} via Keeper!")
                        except RPCError as claim_err:
                            await message.reply_text(f"⚠️ Gagal claim @{usn}: {claim_err}")
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                pass

            await asyncio.sleep(1.5)

        if mode == "1":
            break

    user_states.pop(user_id, None)
    await message.reply_text("🏁 **Pengecekan Selesai.**")

if __name__ == "__main__":
    app.run()
