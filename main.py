"""
Telegram Username Checker & Sniper Bot  (Checker = BOT TOKEN, konfirmasi final = Keeper)
-----------------------------------------------------------------------------------------
- Akun Checker berupa BOT (token dari @BotFather):
    /addbot 1 123456:ABC...        (1 bot, kode 1)
    /addbot                        (lalu kirim banyak token, satu per baris)
- /pause [kode] & /active [kode] untuk menonaktifkan / mengaktifkan bot
- Username fragment / banned / invalid otomatis di-blacklist (tidak dicek lagi)

Alur cek (REVISI):
    1) Fragment og:title (HTTP)  -> available / buy(=fragment) / taken / unknown
    2) Bot checker TIDAK memanggil server Telegram sama sekali (tanpa resolve), jadi tidak kena limit.
    3) Calon yang lolos Fragment dikonfirmasi akun Keeper lewat account.CheckUsername (hasilnya sama
       dengan Telegram X). Keeper bisa lebih dari 1: kalau satu limit/penuh/mati, otomatis pindah ke
       Keeper lain (/keeper untuk menambah, /akun untuk melihat status):
         free      -> lanjut diklaim
         invalid   -> di-blacklist, TIDAK dilaporkan available
         fragment  -> di-blacklist
         taken     -> dibuang
    3) Kalau Keeper tidak ada / sedang limit, calon dilaporkan sebagai
       "CALON AVAILABLE (belum dikonfirmasi Telegram)", bukan "AVAILABLE".

Catatan: Fragment tidak bisa membedakan username yang kosong-beneran dengan username yang
ditolak Telegram (banned/reserved). Hanya Telegram sendiri yang tahu, makanya Keeper dipakai.

Install: pip install pyrogram tgcrypto httpx
"""

import asyncio
import html
import json
import math
import os
import random
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from pyrogram import Client, filters
from pyrogram.errors import (
    FloodWait,
    MessageNotModified,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    RPCError,
    SessionPasswordNeeded,
)
from pyrogram.raw import functions
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ----------------------------------------------------
# Config
# ----------------------------------------------------
API_ID = int(os.environ.get("API_ID", "123456"))
API_HASH = os.environ.get("API_HASH", "YOUR_API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(exist_ok=True)

PER_ACCOUNT_DELAY = float(os.environ.get("PER_ACCOUNT_DELAY", "1.5"))  # jeda MINIMAL antar-cek per bot (detik)
TARGET_RATE = float(os.environ.get("TARGET_RATE", "5"))
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "7"))  # 7 = WIB
NOTIFY_MIN_FLOOD = int(os.environ.get("NOTIFY_MIN_FLOOD", "60"))
FRAGMENT_CONCURRENCY = int(os.environ.get("FRAGMENT_CONCURRENCY", "3"))
MAX_RETRY = 2
# Catatan "terakhir terlihat dipakai" yang lebih tua dari ini (hari) dibuang supaya file tidak membengkak.
SEEN_TTL_DAYS = int(os.environ.get("SEEN_TTL_DAYS", "180"))

# Konfirmasi final lewat Keeper (account.CheckUsername, sama seperti Telegram X). DEFAULT NYALA.
# Hanya calon yang lolos Fragment yang dicek, jadi panggilannya sedikit. KEEPER_PRECHECK=0 untuk mematikan.
KEEPER_PRECHECK = os.environ.get("KEEPER_PRECHECK", "1") == "1"
# Klaim otomatis oleh Keeper kalau username lolos cek. DEFAULT MATI (0) = Keeper hanya MENGECEK.
# Set KEEPER_CLAIM=1 kalau ingin Keeper langsung mengklaim (membuat channel + memasang username).
# Perintah /keep [usn] selalu mengklaim (manual), tidak terpengaruh pengaturan ini.
KEEPER_CLAIM = os.environ.get("KEEPER_CLAIM", "0") == "1"
# Jeda (detik) antar panggilan Keeper supaya akun Keeper tidak kena limit.
KEEPER_DELAY = float(os.environ.get("KEEPER_DELAY", "1.0"))          # jeda awal per Keeper
KEEPER_DELAY_MIN = float(os.environ.get("KEEPER_DELAY_MIN", "0.4"))  # tidak lebih cepat dari ini
KEEPER_DELAY_MAX = float(os.environ.get("KEEPER_DELAY_MAX", "20"))   # tidak lebih lambat dari ini
KEEPER_DELAY_GROW = 1.5     # jeda dikali ini tiap Keeper kena limit (mundur)
KEEPER_DELAY_SHRINK = 0.97  # jeda dikali ini tiap cek sukses (maju pelan-pelan)

DEAD_SESSION_ERRORS = (
    "AUTH_KEY_UNREGISTERED",
    "USER_DEACTIVATED",
    "SESSION_REVOKED",
    "SESSION_EXPIRED",
    "ACCESS_TOKEN_INVALID",
    "ACCESS_TOKEN_EXPIRED",
    "AccessTokenInvalid",
    "AccessTokenExpired",
)

UNCLAIMABLE_ERRORS = (
    "USERNAME_INVALID",
    "USERNAME_OCCUPIED",
    "USERNAME_PURCHASE_AVAILABLE",
)

TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")

app = Client("sniper_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


# ----------------------------------------------------
# 1. Penyimpanan persisten (JSON)
# ----------------------------------------------------
class JsonStore:
    def __init__(self, filename: str, compact: bool = False):
        self.path = DATA_DIR / filename
        self.compact = compact  # True = tanpa indentasi (lebih kecil & cepat untuk data besar)
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        if self.compact:
            payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        else:
            payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)


sessions_db = JsonStore("sessions.json")
blacklist_db = JsonStore("blacklist.json")  # {uid: {username: "fragment"|"banned"|"invalid"}}
seen_db = JsonStore("seen_taken.json", compact=True)   # {uid: {username: epoch terakhir terlihat dipakai}}
released_db = JsonStore("released.json")               # {uid: [ {u, last, found, ok} ]} riwayat username yang terlepas
wording_db = JsonStore("wordings.json")     # {uid: text}


def user_sessions(uid: int) -> dict:
    us = sessions_db.data.setdefault(str(uid), {"checkers": {}, "keeper": None, "keepers": {}})
    chk = us.get("checkers")
    changed = False
    if not isinstance(us.get("keepers"), dict):
        us["keepers"] = {}
        changed = True
    if us.get("keeper"):  # format lama (1 keeper) -> pindah ke daftar keepers
        code = "1" if "1" not in us["keepers"] else next_free_keeper_code(us)
        us["keepers"][code] = {"session": us["keeper"], "name": "-", "limit_until": 0}
        us["keeper"] = None
        changed = True
    if not isinstance(chk, dict):
        chk = us["checkers"] = {}
        changed = True
    for c in [c for c, a in chk.items() if not isinstance(a, dict) or "token" not in a]:
        del chk[c]
        changed = True
    if changed:
        sessions_db.save()
    return us


def user_blacklist(uid: int) -> dict:
    return blacklist_db.data.setdefault(str(uid), {})


def add_blacklist(uid: int, usn: str, reason: str):
    bl = user_blacklist(uid)
    if usn not in bl:
        bl[usn] = reason
        blacklist_db.save()


def norm_code(raw: str):
    return str(int(raw)) if raw.isdigit() else None


def next_free_code(us: dict) -> str:
    n = 1
    while str(n) in us["checkers"]:
        n += 1
    return str(n)


def sorted_codes(us: dict) -> list:
    return sorted(us["checkers"], key=int)


def next_free_keeper_code(us: dict) -> str:
    n = 1
    while str(n) in us["keepers"]:
        n += 1
    return str(n)


def sorted_keeper_codes(us: dict) -> list:
    return sorted(us["keepers"], key=int)


_keeper_rr: dict = {}  # {uid: urutan round-robin}


def ready_keeper_codes(us: dict, uid: int) -> list:
    """Keeper yang siap dipakai (tidak limit/penuh/mati), diputar bergiliran supaya beban merata."""
    now = time.time()
    ready = [
        c for c in sorted_keeper_codes(us)
        if not us["keepers"][c].get("dead")
        and not us["keepers"][c].get("full")
        and us["keepers"][c].get("limit_until", 0) <= now
    ]
    if ready:
        i = _keeper_rr.get(uid, 0) % len(ready)
        _keeper_rr[uid] = _keeper_rr.get(uid, 0) + 1
        ready = ready[i:] + ready[:i]
    return ready


def keeper_status_text(k: dict) -> str:
    if k.get("dead"):
        return "❌ Sesi mati (login ulang)"
    if k.get("full"):
        return "📦 Penuh (kebanyakan channel/username publik)"
    left = k.get("limit_until", 0) - time.time()
    if left > 0:
        return f"⏳ Limit, sisa {fmt_duration(left)} (pulih {fmt_clock(k['limit_until'])})"
    d = k.get("delay", KEEPER_DELAY)
    n = k.get("last_limit_after")
    extra = f", limit terakhir setelah {n} cek" if n else ""
    return f"✅ Siap (jeda ~{d:.1f}d{extra})"


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}j {m}m"
    if m:
        return f"{m}m {s}d"
    return f"{s}d"


def fmt_hms(sec: float) -> str:
    """Durasi detail, contoh: 4 jam 59 menit 12 detik / 5 menit 8 detik / 42 detik."""
    sec = max(int(sec), 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    parts = []
    if h:
        parts.append(f"{h} jam")
    if h or m:
        parts.append(f"{m} menit")
    parts.append(f"{s} detik")
    return " ".join(parts)


def fmt_full(sec: float) -> str:
    sec = max(int(sec), 0)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d} hari")
    if d or h:
        parts.append(f"{h}j")
    if d or h or m:
        parts.append(f"{m}m")
    parts.append(f"{s}d")
    return " ".join(parts)


def fmt_clock(epoch: float) -> str:
    tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    label = "WIB" if TZ_OFFSET_HOURS == 7 else f"UTC{TZ_OFFSET_HOURS:+d}"
    return datetime.fromtimestamp(epoch, tz).strftime("%d/%m %H:%M:%S") + f" {label}"


def mask_token(t: str) -> str:
    head = t.split(":")[0]
    return f"{head}:•••{t[-3:]}"


def pace_delay(n_active: int) -> float:
    return max(PER_ACCOUNT_DELAY, max(n_active, 1) / TARGET_RATE)


# ----------------------------------------------------
# 2. Generator variasi username
# ----------------------------------------------------
ALPHABET = "abcdefghijklmnopqrstuvwxyz"
USN_RE = re.compile(r"^[a-z][a-z0-9_]{3,30}[a-z0-9]$")  # 5-32 char, mulai huruf, akhir alnum


def is_valid_usn(u: str) -> bool:
    return bool(USN_RE.match(u)) and "__" not in u


def gen_tamdal(base: str) -> list:
    return [base[:i] + c + base[i:] for i in range(1, len(base)) for c in ALPHABET]


def gen_tamping(base: str) -> list:
    return [c + base for c in ALPHABET] + [base + c for c in ALPHABET]


def gen_ganhur(base: str) -> list:
    return [base[:i] + c + base[i + 1:] for i in range(len(base)) for c in ALPHABET if c != base[i]]


def gen_kurhur(base: str) -> list:
    return [base[:i] + base[i + 1:] for i in range(len(base))]


def gen_sop(base: str) -> list:
    return [base[:i] + base[i] + base[i:] for i in range(len(base))]


GENERATORS = {
    "tamping": [gen_tamping],
    "ganhur": [gen_ganhur],
    "mulchar": [gen_tamdal],
    "kurhur": [gen_kurhur],
    "sop": [gen_sop],
    "idol": [gen_tamdal, gen_tamping, gen_kurhur, gen_ganhur, gen_sop],
}


def generate_usernames(category: str, base: str) -> list:
    category, base = category.lower().strip(), base.lower().strip().replace(" ", "")
    out = set()
    for f in GENERATORS.get(category, []):
        out.update(f(base))
    return [u for u in out if is_valid_usn(u)]


# ----------------------------------------------------
# 3. Verifikasi username
# ----------------------------------------------------
async def raw_check(client: Client, usn: str) -> str:
    """
    Cek FINAL lewat akun user (Keeper): account.CheckUsername -> free / taken / fragment / invalid.
    Ini satu-satunya cara andal membedakan username yang benar-benar bisa dipakai dengan yang
    ditolak Telegram (banned/reserved/invalid). Bot tidak boleh memanggil method ini.
    """
    try:
        ok = await client.invoke(functions.account.CheckUsername(username=usn))
        return "free" if ok else "taken"
    except RPCError as e:
        s = str(e)
        if "USERNAME_PURCHASE_AVAILABLE" in s:
            return "fragment"
        if "USERNAME_OCCUPIED" in s:
            return "taken"
        if "USERNAME_INVALID" in s:
            return "invalid"
        raise  # FloodWait dll. dilempar ke atas


_GENERIC_TITLES = ("buy and sell usernames", "just a moment", "fragment")
_OG1 = re.compile(r'<meta[^>]*?property="og:title"[^>]*?content="([^"]*)"', re.I)
_OG2 = re.compile(r'<meta[^>]*?content="([^"]*)"[^>]*?property="og:title"', re.I)
_FRAG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://fragment.com/",
}
FRAG_INTERVAL_MIN = 0.35
FRAG_INTERVAL_MAX = 6.0
FRAG_GROW = 1.8
FRAG_SHRINK = 0.97
FRAG_ATTEMPTS = 6
FRAG_RETRY_MAX_DELAY = 15.0

_http = None
_frag_sem = None
_frag_lock = None
_frag_pace = {"interval": 0.6, "last": 0.0}


def get_http():
    global _http, _frag_sem, _frag_lock
    if _http is None:
        _http = httpx.AsyncClient(timeout=10, follow_redirects=True)
        _frag_sem = asyncio.Semaphore(FRAGMENT_CONCURRENCY)
        _frag_lock = asyncio.Lock()
    return _http, _frag_sem, _frag_lock


def log_candidate(usn: str, title: str):
    """Catat judul Fragment tiap calon available ke data/candidates.log (untuk diagnosa)."""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t{usn}\t{title}"
    print(f"[available-candidate] {usn} | og:title={title!r}")
    try:
        with open(DATA_DIR / "candidates.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _og_title(page: str) -> str:
    m = _OG1.search(page) or _OG2.search(page)
    return html.unescape(m.group(1)).strip() if m else ""


def _is_generic_title(title: str) -> bool:
    t = title.strip().lower()
    return (not t) or any(t == g or g in t for g in _GENERIC_TITLES)


async def fragment_state(usn: str) -> str:
    """Return: available | buy | taken | unknown. Raise RuntimeError kalau Fragment terus kena limit."""
    http, sem, plock = get_http()
    last_title, last_code = "", 0
    for attempt in range(1, FRAG_ATTEMPTS + 1):
        async with sem:
            async with plock:
                wait = _frag_pace["interval"] - (time.monotonic() - _frag_pace["last"])
                if wait > 0:
                    await asyncio.sleep(wait)
                _frag_pace["last"] = time.monotonic()
            try:
                r = await http.get(f"https://fragment.com/username/{usn}", headers=_FRAG_HEADERS)
                code, title = r.status_code, _og_title(r.text)
            except httpx.HTTPError:
                code, title = 0, ""

        generic = code in (0, 429) or code >= 500 or _is_generic_title(title)
        async with plock:
            if generic:
                _frag_pace["interval"] = min(_frag_pace["interval"] * FRAG_GROW, FRAG_INTERVAL_MAX)
            else:
                _frag_pace["interval"] = max(_frag_pace["interval"] * FRAG_SHRINK, FRAG_INTERVAL_MIN)

        if not generic:
            low = title.lower()
            if "auctions for usernames" in low:
                log_candidate(usn, title)
                return "available"
            if low.startswith("buy @"):
                return "buy"
            if "make an offer" in low:
                return "taken"
            print(f"[fragment-unknown] {usn}: og:title={title!r}")
            return "unknown"

        last_title, last_code = title, code
        if attempt < FRAG_ATTEMPTS:
            await asyncio.sleep(min(_frag_pace["interval"] * attempt, FRAG_RETRY_MAX_DELAY))

    raise RuntimeError(f"Fragment halaman umum/limit (HTTP {last_code}, og:title={last_title!r})")


async def verify_username(usn: str) -> str:
    """
    Return: available | taken | fragment | invalid | unknown
    Murni Fragment (HTTP saja). TIDAK ada panggilan ke server Telegram, jadi bot tidak kena limit Telegram.
    "available" = CALON; konfirmasi Telegram dilakukan Keeper di handle_available (raw_check).
    """
    if not is_valid_usn(usn):
        return "invalid"

    st = await fragment_state(usn)
    if st == "buy":
        return "fragment"
    if st == "taken":
        return "taken"
    if st == "unknown":
        return "unknown"
    return "available"


# ----------------------------------------------------
# 4. Manajemen client
# ----------------------------------------------------
_running: dict = {}


async def get_client(token: str) -> Client:
    cl = _running.get(token)
    if cl is None:
        if TOKEN_RE.fullmatch(token):
            cl = Client(
                f"bot_{token.split(':')[0]}",
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=token,
                in_memory=True,
                no_updates=True,
                sleep_threshold=0,
            )
        else:
            cl = Client(
                f"keeper_{abs(hash(token))}",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=token,
                in_memory=True,
                no_updates=True,
                sleep_threshold=0,
            )
        await cl.start()
        _running[token] = cl
    return cl


async def drop_client(token: str):
    cl = _running.pop(token, None)
    if cl:
        try:
            await cl.stop()
        except Exception:
            pass


async def claim_username(keeper: Client, usn: str):
    """Klaim lewat channel baru milik keeper. Return (ok, error)"""
    chat = None
    try:
        chat = await keeper.create_channel(title=usn)
        await keeper.set_chat_username(chat.id, usn)
        return True, None
    except FloodWait:
        raise
    except RPCError as e:
        if chat:
            try:
                await keeper.delete_channel(chat.id)
            except Exception:
                pass
        return False, str(e)


KEEPER_FULL_ERRORS = ("CHANNELS_ADMIN_PUBLIC_TOO_MUCH", "USERNAMES_ACTIVE_TOO_MUCH", "CHANNELS_TOO_MUCH")


async def keeper_try(us: dict, code: str, usn: str, claim: bool = True):
    """
    Coba cek final + klaim `usn` dengan 1 Keeper.
    Return (status, detail):
      not_free(final) | free (lolos cek, tidak diklaim) | claimed | unclaimable(err) | claim_error(err)
      flood(detik) | full(err) | dead(err) | error(err)   <- 4 status terakhir = pindah ke Keeper lain
    """
    k = us["keepers"][code]
    try:
        keeper = await get_client(k["session"])
        if KEEPER_PRECHECK:
            final = await raw_check(keeper, usn)
            k["checks"] = k.get("checks", 0) + 1
            d = k.get("delay", KEEPER_DELAY)
            floor = max(KEEPER_DELAY_MIN, k.get("floor", 0))
            k["delay"] = max(floor, d * KEEPER_DELAY_SHRINK)  # lancar -> percepat pelan, tidak di bawah batas aman
            await asyncio.sleep(d)
        else:
            final = "free"
        if final != "free":
            return "not_free", final
        if not claim:
            return "free", None
        ok, err = await claim_username(keeper, usn)
    except FloodWait as e:
        k["limit_until"] = time.time() + e.value + 1
        d = k.get("delay", KEEPER_DELAY)
        k["floor"] = min(KEEPER_DELAY_MAX, d * 1.3)  # ingat: kecepatan segini memicu limit
        k["delay"] = min(KEEPER_DELAY_MAX, max(d, KEEPER_DELAY_MIN) * KEEPER_DELAY_GROW)
        k["last_limit_after"] = k.get("checks", 0)
        k["checks"] = 0
        sessions_db.save()
        return "flood", e.value
    except Exception as e:
        s = str(e)
        if any(d in s for d in DEAD_SESSION_ERRORS):
            k["dead"] = True
            sessions_db.save()
            await drop_client(k["session"])
            return "dead", s
        return "error", s

    if ok:
        return "claimed", None
    err = err or ""
    if any(x in err for x in KEEPER_FULL_ERRORS):
        k["full"] = True
        sessions_db.save()
        return "full", err
    if any(x in err for x in UNCLAIMABLE_ERRORS):
        return "unclaimable", err
    return "claim_error", err


# ----------------------------------------------------
# 5. State
# ----------------------------------------------------
user_states: dict = {}
login_states: dict = {}


def running(uid: int) -> bool:
    st = user_states.get(uid)
    return bool(st and st.get("step") == "RUNNING")


async def clear_login(uid: int):
    ls = login_states.pop(uid, None)
    if ls and ls.get("client"):
        try:
            await ls["client"].disconnect()
        except Exception:
            pass


# ----------------------------------------------------
# 6. Command handlers
# ----------------------------------------------------
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(_, message: Message):
    await message.reply_text(
        "👋 **Telegram Username Checker & Sniper Bot**\n\n"
        "🤖 `/addbot [kode] [token]` - Tambah bot Checker (bisa banyak token sekaligus, satu per baris)\n"
        "🛡 `/keeper [kode]` - Tambah akun Keeper (akun user) untuk cek final ke Telegram. Bisa lebih dari 1\n"
        "🗑 `/delkeeper [kode|all]` - Hapus Keeper\n"
        "📋 `/akun` - Lihat daftar bot Checker & statusnya\n"
        "⏸️ `/pause [kode]` - Pause bot Checker (`all` = semua)\n"
        "▶️ `/active [kode]` - Aktifkan bot Checker (`all` = semua)\n"
        "🚪 `/logout [kode]` - Hapus 1 bot Checker\n"
        "🗑 `/clearnoktel` - Hapus semua bot Checker\n"
        "📝 `/addcp [teks]` - Set wording jualan\n"
        "🚀 `/check` - Mulai auto-sniper\n"
        "🎯 `/keep [usn]` - Klaim manual via Keeper (selalu mengklaim)\n"
        "📊 `/status` - Lihat statistik pengecekan yang sedang berjalan\n"
        "🧩 `/bases` - Daftar based-on yang sedang berjalan + progresnya\n"
        "🕒 `/rilis [jumlah]` - Riwayat username terlepas + jejak waktunya\n"
        "⏭ `/cancelbase [nomor]` - Batalkan satu/beberapa based-on saja (yang lain tetap jalan)\n"
        "🛑 `/stop` - Hentikan checker\n"
        "🔬 `/diag [usn ...]` - Bandingkan isi halaman Fragment (diagnosa)\n"
        "🚫 `/blacklist` - Daftar fragment/banned/invalid yang dikecualikan\n"
        "🚫 `/invalid [usn ...]` - Tandai username yang ditolak Telegram (masuk blacklist)\n"
        "♻️ `/unblacklist [usn|all]` - Hapus dari blacklist\n"
        "📤 `/exportbl [all|fragment|banned|invalid]` - Kirim isi blacklist sebagai file .txt\n"
        "📥 `/importbl` - Muat blacklist dari file .txt (hasil /exportbl) atau teks\n"
        "🌐 `/ip` - Lihat alamat IP keluar bot ini\n"
        "❎ `/cancel` - Batalkan input yang sedang berjalan\n\n"
        "ℹ️ Pasang Keeper (/keeper, sebaiknya akun cadangan) agar hasil dikonfirmasi Telegram: invalid/taken otomatis tersaring."
    )


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_cmd(_, message: Message):
    uid = message.from_user.id
    if uid in login_states:
        await clear_login(uid)
        await message.reply_text("❎ Input dibatalkan.")
    elif running(uid):
        await message.reply_text("Gunakan /stop untuk menghentikan checker.")
    else:
        user_states.pop(uid, None)
        await message.reply_text("❎ Dibatalkan.")


@app.on_message(filters.command("addcp") & filters.private)
async def addcp_cmd(_, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("❌ Format: `/addcp [teks wording]`")
        return
    wording_db.data[str(message.from_user.id)] = parts[1].strip()
    wording_db.save()
    await message.reply_text("✅ Wording jualan tersimpan!")


# ---------- Tambah bot checker ----------
async def add_bot_tokens(uid: int, tokens: list, message: Message, first_code=None):
    us = user_sessions(uid)
    existing = {a["token"] for a in us["checkers"].values()}
    ok_lines, bad_lines = [], []

    for i, token in enumerate(tokens):
        if token in existing:
            bad_lines.append(f"• `{mask_token(token)}` sudah terdaftar")
            continue
        code = first_code if (i == 0 and first_code) else next_free_code(us)
        try:
            cl = await get_client(token)
            me = await cl.get_me()
            if not me.is_bot:
                raise ValueError("bukan token bot")
        except Exception as e:
            await drop_client(token)
            bad_lines.append(f"• `{mask_token(token)}` gagal: `{str(e)[:60]}`")
            continue
        us["checkers"][code] = {
            "token": token,
            "paused": False,
            "limit_until": 0,
            "name": f"@{me.username}" if me.username else (me.first_name or "-"),
        }
        existing.add(token)
        ok_lines.append(f"✅ kode {code}: @{me.username}")

    sessions_db.save()
    text = []
    if ok_lines:
        text.append("\n".join(ok_lines))
    if bad_lines:
        text.append("⚠️ Dilewati:\n" + "\n".join(bad_lines))
    text.append(f"Total bot Checker: {len(us['checkers'])}. Lihat dengan /akun")
    await message.reply_text("\n\n".join(text))


async def try_delete(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


@app.on_message(filters.command(["addbot", "login"]) & filters.private)
async def addbot_cmd(_, message: Message):
    uid = message.from_user.id
    us = user_sessions(uid)
    code, tokens = None, []

    for a in message.command[1:]:
        if TOKEN_RE.fullmatch(a):
            tokens.append(a)
        elif a.isdigit():
            code = norm_code(a)
        else:
            await message.reply_text("❌ Format: `/addbot [kode] [token]` (token dari @BotFather)")
            return

    if code is not None and code in us["checkers"]:
        await message.reply_text(
            f"⚠️ Kode {code} sudah dipakai. Hapus dulu dengan `/logout {code}` atau pakai kode lain."
        )
        return

    if tokens:
        await try_delete(message)
        await add_bot_tokens(uid, tokens, message, first_code=code)
        return

    await clear_login(uid)
    login_states[uid] = {"step": "BOT_TOKEN", "code": code}
    await message.reply_text(
        "🤖 Kirim token bot dari @BotFather.\n"
        "Boleh banyak sekaligus, **satu token per baris**.\n"
        "Batalkan dengan /cancel."
    )


# ---------- Keeper (akun user) ----------
@app.on_message(filters.command("keeper") & filters.private)
async def keeper_cmd(_, message: Message):
    uid = message.from_user.id
    us = user_sessions(uid)
    kcode = None
    if len(message.command) > 1:
        kcode = norm_code(message.command[1])
        if kcode is None:
            await message.reply_text("❌ Format: `/keeper` atau `/keeper [kode]` (kode berupa angka)")
            return
    await clear_login(uid)  # buang sesi login lama kalau ada
    login_states[uid] = {"step": "PHONE", "kcode": kcode}
    now = f"Keeper terpasang: {len(us['keepers'])}. " if us["keepers"] else ""
    await message.reply_text(
        f"📱 Login akun **Keeper** (akun user, bukan bot). {now}\n"
        "Kirim nomor HP format internasional (contoh `+6281234567890`).\n"
        "Ulangi `/keeper` untuk menambah akun berikutnya. Batalkan dengan /cancel."
    )


@app.on_message(filters.command("delkeeper") & filters.private)
async def delkeeper_cmd(_, message: Message):
    uid = message.from_user.id
    if running(uid):
        await message.reply_text("⚠️ Hentikan checker dulu dengan /stop.")
        return
    if len(message.command) < 2:
        await message.reply_text("❌ Format: `/delkeeper [kode]` atau `/delkeeper all`")
        return
    us = user_sessions(uid)
    arg = message.command[1].lower()
    if arg == "all":
        n = len(us["keepers"])
        for k in us["keepers"].values():
            await drop_client(k["session"])
        us["keepers"] = {}
        sessions_db.save()
        await message.reply_text(f"🗑 {n} Keeper dihapus.")
        return
    code = norm_code(arg)
    k = us["keepers"].get(code) if code else None
    if not k:
        codes = ", ".join(sorted_keeper_codes(us)) or "-"
        await message.reply_text(f"❌ Keeper kode `{arg}` tidak ada. Kode tersedia: {codes}")
        return
    await drop_client(k["session"])
    del us["keepers"][code]
    sessions_db.save()
    await message.reply_text(f"🗑 Keeper kode {code} dihapus.")


def build_akun_page(uid: int):
    us = user_sessions(uid)
    if not us["checkers"] and not us["keepers"]:
        return "📭 Belum ada bot Checker. Mulai dengan `/addbot`", None

    lines = []
    n_ok = n_pause = 0

    for code in sorted_codes(us):
        a = us["checkers"][code]
        head = f"**{code}.** `{a.get('name', '-')}`"
        if a.get("paused"):
            n_pause += 1
            lines.append(f"{head}\n      ⏸️ Pause")
        else:
            n_ok += 1
            lines.append(f"{head}\n      ✅ Aktif")

    header = f"📋 **Bot Checker** — ✅ {n_ok} aktif | ⏸️ {n_pause} pause"
    if us["keepers"]:
        klines = [
            f"**{c}.** `{us['keepers'][c].get('name', '-')}`\n      {keeper_status_text(us['keepers'][c])}"
            for c in sorted_keeper_codes(us)
        ]
        keeper = "\n" + "\n".join(klines)
    else:
        keeper = "belum ❌ (hasil hanya calon, belum dikonfirmasi Telegram)"
    text = f"{header}\n\n" + ("\n".join(lines) or "-") + f"\n\n🛡 **Keeper:** {keeper}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="akun:refresh")]])
    return text, kb


@app.on_message(filters.command("akun") & filters.private)
async def akun_cmd(_, message: Message):
    text, kb = build_akun_page(message.from_user.id)
    await message.reply_text(text, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^akun:"))
async def akun_cb(_, cq: CallbackQuery):
    text, kb = build_akun_page(cq.from_user.id)
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass
    await cq.answer("Diperbarui")


async def set_paused(message: Message, paused: bool):
    uid = message.from_user.id
    us = user_sessions(uid)
    cmd = message.command[0]
    if len(message.command) < 2:
        await message.reply_text(f"❌ Format: `/{cmd} [kode]` atau `/{cmd} all`")
        return

    arg = message.command[1].lower()
    if arg == "all":
        if not us["checkers"]:
            await message.reply_text("📭 Belum ada bot Checker.")
            return
        for a in us["checkers"].values():
            a["paused"] = paused
        sessions_db.save()
        await message.reply_text(("⏸️ Semua bot dipause." if paused else "▶️ Semua bot diaktifkan."))
        return

    code = norm_code(arg)
    acc = us["checkers"].get(code) if code else None
    if not acc:
        codes = ", ".join(sorted_codes(us)) or "-"
        await message.reply_text(f"❌ Bot kode `{arg}` tidak ditemukan. Kode tersedia: {codes}")
        return
    if bool(acc.get("paused")) == paused:
        await message.reply_text(f"ℹ️ Bot kode {code} sudah {'dipause' if paused else 'aktif'}.")
        return

    acc["paused"] = paused
    sessions_db.save()
    if paused:
        await message.reply_text(f"⏸️ Bot Checker kode {code} dipause, tidak ikut pengecekan.")
    else:
        await message.reply_text(f"▶️ Bot Checker kode {code} diaktifkan.")


@app.on_message(filters.command("pause") & filters.private)
async def pause_cmd(_, message: Message):
    await set_paused(message, True)


@app.on_message(filters.command("active") & filters.private)
async def active_cmd(_, message: Message):
    await set_paused(message, False)


@app.on_message(filters.command("logout") & filters.private)
async def logout_cmd(_, message: Message):
    uid = message.from_user.id
    if running(uid):
        await message.reply_text("⚠️ Hentikan checker dulu dengan /stop.")
        return
    if len(message.command) < 2:
        await message.reply_text("❌ Format: `/logout [kode]`")
        return
    us = user_sessions(uid)
    code = norm_code(message.command[1])
    acc = us["checkers"].get(code) if code else None
    if not acc:
        await message.reply_text("❌ Kode tidak ditemukan. Cek dengan /akun")
        return
    await drop_client(acc["token"])
    del us["checkers"][code]
    sessions_db.save()
    await message.reply_text(f"🚪 Bot Checker kode {code} dihapus.")


@app.on_message(filters.command("clearnoktel") & filters.private)
async def clearnoktel_cmd(_, message: Message):
    uid = message.from_user.id
    if running(uid):
        await message.reply_text("⚠️ Hentikan checker dulu dengan /stop.")
        return
    us = user_sessions(uid)
    for a in us["checkers"].values():
        await drop_client(a["token"])
    n = len(us["checkers"])
    us["checkers"] = {}
    sessions_db.save()
    await message.reply_text(f"🗑 {n} bot Checker dihapus. (Keeper tidak ikut dihapus)")


# ---------- Check / stop / keep ----------
@app.on_message(filters.command("check") & filters.private)
async def check_cmd(_, message: Message):
    uid = message.from_user.id
    if running(uid):
        await message.reply_text("⚠️ Checker masih berjalan. /stop dulu.")
        return
    us = user_sessions(uid)
    if not us["checkers"]:
        await message.reply_text("⚠️ Tambah minimal 1 bot Checker dulu: `/addbot`")
        return
    if all(a.get("paused") for a in us["checkers"].values()):
        await message.reply_text("⚠️ Semua bot Checker sedang dipause. Aktifkan dengan `/active [kode]`.")
        return
    user_states[uid] = {"step": "SELECT_MODE"}
    warn = ""
    if not us["keepers"]:
        warn = (
            "\n\n⚠️ Keeper belum dipasang (/keeper). Tanpa Keeper, hasil hanya **calon available** dari Fragment "
            "dan banyak yang bisa ternyata invalid. Pasang Keeper agar dikonfirmasi seperti Telegram X."
        )
    await message.reply_text(
        "🚀 **Pilih mode auto-sniper:**\n\n"
        "1️⃣ Sekali selesai (1 putaran lalu berhenti)\n"
        "2️⃣ Looping terus (sampai /stop)" + warn
    )


@app.on_message(filters.command("stop") & filters.private)
async def stop_cmd(_, message: Message):
    uid = message.from_user.id
    if running(uid):
        user_states[uid]["active"] = False
        await message.reply_text("🛑 Menghentikan... tunggu proses yang sedang berjalan selesai.")
    else:
        await message.reply_text("⚠️ Tidak ada checker yang sedang berjalan.")


@app.on_message(filters.command("keep") & filters.private)
async def keep_cmd(_, message: Message):
    uid = message.from_user.id
    if len(message.command) < 2:
        await message.reply_text("❌ Format: `/keep username`")
        return
    usn = message.command[1].lstrip("@").lower()
    us = user_sessions(uid)
    if not us["keepers"]:
        await message.reply_text("⚠️ Belum ada Keeper. Set dulu dengan /keeper.")
        return

    await message.reply_text(f"🔎 Memverifikasi `{usn}` ...")
    try:
        result = await verify_username(usn)
    except Exception as e:
        await message.reply_text(f"⚠️ Error saat cek: `{e}`")
        return

    if result != "available":
        if result == "fragment":
            add_blacklist(uid, usn, "fragment")
        elif result == "invalid" and is_valid_usn(usn):
            add_blacklist(uid, usn, "invalid")
        await message.reply_text(f"❌ `{usn}` tidak available (status: **{result}**).")
        return

    codes = ready_keeper_codes(us, uid)
    if not codes:
        await message.reply_text("⏳ Semua Keeper sedang limit/penuh/mati. Lihat /akun.")
        return

    for code in codes:
        status, detail = await keeper_try(us, code, usn)
        if status == "not_free":
            if detail in ("fragment", "invalid"):
                add_blacklist(uid, usn, detail)
            await message.reply_text(f"❌ `{usn}` tidak available (cek Telegram: **{detail}**).")
            return
        if status == "claimed":
            await message.reply_text(f"🔥 **BERHASIL DIKLAIM:** @{usn} (Keeper {code})")
            return
        if status == "unclaimable":
            add_blacklist(uid, usn, "banned")
            await message.reply_text(f"🚫 `{usn}` gagal diklaim (kemungkinan banned/reserved), masuk blacklist.")
            return
        if status == "claim_error":
            await message.reply_text(f"⚠️ Gagal klaim: `{detail}`")
            return
        # flood / full / dead / error -> coba Keeper berikutnya
        await message.reply_text(f"↪️ Keeper {code}: {status} ({str(detail)[:80]}), pindah ke Keeper lain...")
    await message.reply_text("⚠️ Semua Keeper gagal/limit. Coba lagi nanti, lihat /akun.")


@app.on_message(filters.command("status") & filters.private)
async def status_cmd(_, message: Message):
    """Statistik pengecekan yang sedang berjalan (hanya memantau, tidak mengubah apa pun)."""
    uid = message.from_user.id
    st = user_states.get(uid)
    if not running(uid) or "ctl" not in st:
        await message.reply_text("⚠️ Tidak ada checker yang sedang berjalan.")
        return
    ctl, stats, per_acc = st["ctl"], st["stats"], st["per_acc"]
    elapsed = max(time.monotonic() - st["started"], 0.001)
    total = sum(per_acc.values())
    rate = total / elapsed
    left = max(ctl["out"], 0)
    round_total = ctl.get("round_total", 0)
    done_round = max(round_total - left, 0)
    pct = f" ({done_round * 100 / round_total:.1f}%)" if round_total else ""
    eta = fmt_hms(left / rate) if rate > 0 and left else "-"
    bs = st.get("bases", {})
    base_line = f"🧩 Based-on aktif: {sum(1 for b in bs.values() if not b['cancelled'])} dari {len(bs)}\n" if bs else ""
    await message.reply_text(
        "📊 **Status pengecekan**\n"
        + base_line +
        f"⏱ Berjalan: {fmt_hms(elapsed)}\n"
        f"🔎 Sudah dicek: {done_round} dari {round_total}{pct}\n"
        f"📦 Sisa putaran ini: {left}\n"
        f"⚡️ Laju: {rate:.2f} cek/detik | total cek: {total}\n"
        f"⏳ Perkiraan selesai: {eta}\n\n"
        f"🎯 Calon/available: {stats['available']}\n"
        f"🔥 Diklaim: {stats['claimed']}\n"
        f"🚫 Fragment: {stats['fragment']} | Banned: {stats['banned']} | Invalid: {stats['invalid']}\n"
        f"❓ Unknown: {stats['unknown']} | ❌ Taken: {stats['taken']} | Error: {stats['error']}"
    )


@app.on_message(filters.command("bases") & filters.private)
async def bases_cmd(_, message: Message):
    uid = message.from_user.id
    st = user_states.get(uid)
    if not running(uid) or "bases" not in st:
        await message.reply_text("⚠️ Tidak ada checker yang sedang berjalan.")
        return
    bases, checked = st["bases"], st["checked"]
    lines, n_cancel = [], 0
    for i, b in bases.items():
        if b["cancelled"]:
            n_cancel += 1
            continue
        total = len(b["usns"])
        done = len(b["usns"] & checked)
        pct = f"{done * 100 / total:.0f}%" if total else "-"
        lines.append(f"{i}. `{b['label']}` — {done}/{total} ({pct})")
    head = f"🧩 **Based-on aktif: {len(lines)}** (dibatalkan: {n_cancel})\nBatalkan dengan `/cancelbase [nomor]`, contoh `/cancelbase 3` atau `/cancelbase 2 5 7`\n"
    if not lines:
        await message.reply_text(head + "\n(semua based-on sudah dibatalkan)")
        return
    chunk, size = [], len(head)
    first = True
    for ln in lines:
        if size + len(ln) > 3500:
            await message.reply_text((head if first else "") + "\n".join(chunk))
            first, chunk, size = False, [], 0
        chunk.append(ln)
        size += len(ln) + 1
    if chunk:
        await message.reply_text((head if first else "") + ("\n" if first else "") + "\n".join(chunk))


@app.on_message(filters.command("cancelbase") & filters.private)
async def cancelbase_cmd(_, message: Message):
    uid = message.from_user.id
    st = user_states.get(uid)
    if not running(uid) or "bases" not in st:
        await message.reply_text("⚠️ Tidak ada checker yang sedang berjalan.")
        return
    tokens = message.command[1:]
    if not tokens:
        await message.reply_text("❌ Format: `/cancelbase [nomor ...]` atau `/cancelbase idol claude`. Lihat nomor dengan /bases.")
        return
    idxs, err = find_bases(st, tokens)
    if err:
        await message.reply_text(f"❌ {err}")
        return
    bases, checked = st["bases"], st["checked"]
    out = []
    for i in idxs:
        b = bases[i]
        if b["cancelled"]:
            out.append(f"ℹ️ {i}. `{b['label']}` sudah dibatalkan sebelumnya.")
            continue
        b["cancelled"] = True
        skip = sum(1 for u in b["usns"] if u not in checked and usn_cancelled(st, u))
        shared = sum(1 for u in b["usns"] if u not in checked and not usn_cancelled(st, u))
        note = f" ({shared} username tetap dicek karena juga milik based-on lain)" if shared else ""
        out.append(f"⏭ {i}. `{b['label']}` dibatalkan: {skip} username belum dicek akan dilewati{note}.")
    left = sum(1 for b in bases.values() if not b["cancelled"])
    out.append(f"🧩 Based-on yang masih jalan: {left}")
    await message.reply_text("\n".join(out))


@app.on_message(filters.command("rilis") & filters.private)
async def rilis_cmd(_, message: Message):
    """Riwayat username yang terlihat kosong, lengkap dengan jejak waktunya."""
    uid = message.from_user.id
    n = 10
    if len(message.command) > 1 and message.command[1].isdigit():
        n = min(max(int(message.command[1]), 1), 50)
    rl = released_db.data.get(str(uid), [])
    if not rl:
        await message.reply_text("📭 Belum ada username terlepas yang tercatat.")
        return
    blocks = []
    for e in reversed(rl[-n:]):
        found, last = e["found"], e.get("last")
        tag = "✅" if e.get("ok") else "🟡"
        if last:
            blocks.append(
                f"{tag} @{e['u']}\n"
                f"   terakhir terlihat dipakai: {fmt_clock(last)}\n"
                f"   pertama terlihat kosong: {fmt_clock(found)}\n"
                f"   selisih: {fmt_hms(found - last)}"
            )
        else:
            blocks.append(
                f"{tag} @{e['u']}\n"
                f"   pertama terlihat kosong: {fmt_clock(found)}\n"
                "   (belum ada catatan sebelumnya)"
            )
    head = f"🕒 **Riwayat username terlepas** ({len(blocks)} terbaru)\n✅ dikonfirmasi Telegram | 🟡 calon\n\n"
    chunk, size, first = [], len(head), True
    for b in blocks:
        if size + len(b) > 3500:
            await message.reply_text((head if first else "") + "\n\n".join(chunk))
            chunk, size, first = [], 0, False
        chunk.append(b)
        size += len(b) + 2
    if chunk:
        await message.reply_text((head if first else "") + "\n\n".join(chunk))


# ---------- Diagnosa: bandingkan isi halaman Fragment ----------
def _visible_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style).*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(page)).strip()


def _meta(page: str, prop: str) -> str:
    m = (re.search(rf'<meta[^>]*?(?:property|name)="{prop}"[^>]*?content="([^"]*)"', page, re.I)
         or re.search(rf'<meta[^>]*?content="([^"]*)"[^>]*?(?:property|name)="{prop}"', page, re.I))
    return html.unescape(m.group(1)).strip() if m else "-"


@app.on_message(filters.command("diag") & filters.private)
async def diag_cmd(_, message: Message):
    names = [a.lstrip("@").lower() for a in message.command[1:]][:6]
    if not names:
        await message.reply_text(
            "❌ Format: `/diag usn1 usn2 ...` (maks 6)\n"
            "Isi dengan campuran: username invalid, username yang benar-benar available, dan 1 yang taken."
        )
        return
    http, _, _ = get_http()
    for usn in names:
        try:
            r = await http.get(f"https://fragment.com/username/{usn}", headers=_FRAG_HEADERS)
            page = r.text
        except Exception as e:
            await message.reply_text(f"⚠️ `{usn}`: gagal ambil halaman: `{e}`")
            continue

        status_cls = sorted(set(re.findall(r'class="([^"]*status[^"]*)"', page)))[:8]
        text = _visible_text(page)
        path = DATA_DIR / f"diag_{usn}.html"
        path.write_text(page, encoding="utf-8")

        report = (
            f"USN: {usn}\n"
            f"HTTP: {r.status_code} | panjang html: {len(page)}\n"
            f"og:title: {_meta(page, 'og:title')}\n"
            f"og:description: {_meta(page, 'og:description')[:200]}\n"
            f"class status: {status_cls}\n"
            f"teks halaman: {text[:700]}"
        ).replace("`", "'")
        await message.reply_text(f"```\n{report}\n```")
        try:
            await message.reply_document(str(path), caption=f"HTML penuh: {usn}")
        except Exception as e:
            print(f"[diag-send-error] {usn}: {e}")
        await asyncio.sleep(1.5)  # jeda supaya Fragment tidak kena limit


# ---------- Blacklist ----------
BL_PER_PAGE = 20
BL_FILTERS = (("all", "Semua"), ("fragment", "Fragment"), ("banned", "Banned"), ("invalid", "Invalid"))


def build_blacklist_page(uid: int, flt: str, page: int):
    bl = user_blacklist(uid)
    if not bl:
        return "📭 Blacklist kosong.", None

    c = Counter(bl.values())
    items = sorted((u, r) for u, r in bl.items() if flt == "all" or r == flt)
    total_pages = max(1, math.ceil(len(items) / BL_PER_PAGE))
    page = min(max(page, 0), total_pages - 1)
    start = page * BL_PER_PAGE
    chunk = items[start:start + BL_PER_PAGE]

    header = (
        f"🚫 **Blacklist** — total {len(bl)}\n"
        f"Fragment {c['fragment']} | Banned {c['banned']} | Invalid {c['invalid']}"
    )
    if chunk:
        body = "\n".join(f"{start + i}. `{u}` ({r})" for i, (u, r) in enumerate(chunk, 1))
    else:
        body = "(tidak ada data untuk filter ini)"
    text = f"{header}\n\n{body}\n\nHalaman {page + 1}/{total_pages}"

    filter_row = [
        InlineKeyboardButton(("✅ " if key == flt else "") + label, callback_data=f"bl:{key}:0")
        for key, label in BL_FILTERS
    ]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"bl:{flt}:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="bl:noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"bl:{flt}:{page + 1}"))
    return text, InlineKeyboardMarkup([filter_row, nav_row])


@app.on_message(filters.command("blacklist") & filters.private)
async def blacklist_cmd(_, message: Message):
    flt = message.command[1].lower() if len(message.command) > 1 else "all"
    if flt not in dict(BL_FILTERS):
        flt = "all"
    text, kb = build_blacklist_page(message.from_user.id, flt, 0)
    await message.reply_text(text, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^bl:"))
async def blacklist_cb(_, cq: CallbackQuery):
    parts = cq.data.split(":")
    if len(parts) != 3 or parts[1] == "noop" or parts[1] not in dict(BL_FILTERS):
        await cq.answer()
        return
    try:
        page = int(parts[2])
    except ValueError:
        await cq.answer()
        return
    text, kb = build_blacklist_page(cq.from_user.id, parts[1], page)
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass
    await cq.answer()


@app.on_message(filters.command("invalid") & filters.private)
async def invalid_cmd(_, message: Message):
    uid = message.from_user.id
    names = [a.lstrip("@").lower() for a in message.command[1:]]
    names = [n for n in names if is_valid_usn(n)]
    if not names:
        await message.reply_text("❌ Format: `/invalid usn1 usn2 ...` (tandai username yang ditolak Telegram)")
        return
    for n in names:
        add_blacklist(uid, n, "invalid")
    await message.reply_text(f"🚫 {len(names)} username masuk blacklist (invalid), tidak dicek lagi.")


@app.on_callback_query(filters.regex(r"^u:(i|o):"))
async def usn_mark_cb(_, cq: CallbackQuery):
    _, kind, usn = cq.data.split(":", 2)
    if kind == "i":
        add_blacklist(cq.from_user.id, usn, "invalid")
        note, toast = "🚫 Ditandai invalid, masuk blacklist (tidak dicek lagi).", "Masuk blacklist"
    else:
        note, toast = "✅ Ditandai sudah diklaim.", "Tercatat"
    try:
        base = cq.message.text.markdown if cq.message.text else ""
        await cq.message.edit_text(f"{base}\n\n{note}")
    except Exception:
        pass
    await cq.answer(toast)


@app.on_message(filters.command("unblacklist") & filters.private)
async def unblacklist_cmd(_, message: Message):
    uid = message.from_user.id
    if len(message.command) < 2:
        await message.reply_text("❌ Format: `/unblacklist username` atau `/unblacklist all`")
        return
    arg = message.command[1].lstrip("@").lower()
    bl = user_blacklist(uid)
    if arg == "all":
        n = len(bl)
        bl.clear()
        blacklist_db.save()
        await message.reply_text(f"♻️ {n} username dihapus dari blacklist.")
    elif arg in bl:
        del bl[arg]
        blacklist_db.save()
        await message.reply_text(f"♻️ `{arg}` dihapus dari blacklist.")
    else:
        await message.reply_text("⚠️ Username itu tidak ada di blacklist.")


# ---------- Export / Import blacklist (hanya data blacklist, tanpa Keeper/token) ----------
BL_REASONS = ("fragment", "banned", "invalid")


def parse_blacklist_text(text: str):
    """
    Format yang dikenali (satu atau banyak per baris, baris diawali # diabaikan):
        username
        username fragment|banned|invalid
        usn1 usn2 usn3
    Alasan default = invalid. Return ({username: alasan}, jumlah_token_tidak_valid)
    """
    out, bad = {}, 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.replace(",", " ").split()
        reason = next((t.lower() for t in tokens if t.lower() in BL_REASONS), "invalid")
        for t in tokens:
            if t.lower() in BL_REASONS:
                continue
            usn = t.lstrip("@").lower()
            if is_valid_usn(usn):
                out[usn] = reason
            else:
                bad += 1
    return out, bad


async def do_import_blacklist(uid: int, text: str, message: Message):
    parsed, bad = parse_blacklist_text(text)
    if not parsed:
        await message.reply_text("❌ Tidak ada username valid di data itu. Format: `username alasan` per baris.")
        return
    bl = user_blacklist(uid)
    added = existed = 0
    for usn, reason in parsed.items():
        if usn in bl:
            existed += 1
        else:
            bl[usn] = reason
            added += 1
    blacklist_db.save()  # simpan sekali saja (cepat walau ribuan baris)
    await message.reply_text(
        f"📥 **Import blacklist selesai**\n"
        f"✅ Ditambahkan: {added}\n"
        f"↩️ Sudah ada sebelumnya: {existed}\n"
        f"⚠️ Dilewati (bukan username valid): {bad}\n"
        f"🚫 Total blacklist sekarang: {len(bl)}"
    )


@app.on_message(filters.command("exportbl") & filters.private)
async def exportbl_cmd(_, message: Message):
    uid = message.from_user.id
    flt = message.command[1].lower() if len(message.command) > 1 else "all"
    if flt not in dict(BL_FILTERS):
        flt = "all"
    bl = user_blacklist(uid)
    items = sorted((u, r) for u, r in bl.items() if flt == "all" or r == flt)
    if not items:
        await message.reply_text("📭 Tidak ada data blacklist untuk diekspor.")
        return
    path = DATA_DIR / f"blacklist_{flt}.txt"
    path.write_text("\n".join(f"{u} {r}" for u, r in items) + "\n", encoding="utf-8")
    c = Counter(r for _, r in items)
    try:
        await message.reply_document(
            str(path),
            caption=(
                f"🚫 Blacklist ({flt}): {len(items)} username\n"
                f"Fragment {c['fragment']} | Banned {c['banned']} | Invalid {c['invalid']}\n"
                "Di bot tujuan jalankan /importbl lalu kirim file ini."
            ),
        )
    finally:
        try:
            path.unlink()
        except OSError:
            pass


@app.on_message(filters.command("importbl") & filters.private)
async def importbl_cmd(_, message: Message):
    uid = message.from_user.id
    if uid in login_states:
        await clear_login(uid)
    login_states[uid] = {"step": "IMPORT_BL"}
    await message.reply_text(
        "📥 Kirim **file .txt** hasil /exportbl (paling praktis untuk data banyak),\n"
        "atau tempel daftar username langsung sebagai teks (maks ~4000 karakter per pesan).\n"
        "Format per baris: `username alasan` (alasan: fragment/banned/invalid, boleh dikosongkan = invalid).\n"
        "Batalkan dengan /cancel."
    )


@app.on_message(filters.private & filters.document)
async def handle_document(_, message: Message):
    uid = message.from_user.id
    ls = login_states.get(uid)
    st = user_states.get(uid)
    is_import = bool(ls and ls.get("step") == "IMPORT_BL")
    is_list = bool(st and st.get("step") == "WAIT_LIST")
    if not (is_import or is_list):
        return
    doc = message.document
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await message.reply_text("❌ File terlalu besar (maks 5 MB).")
        return
    tmp = DATA_DIR / f"upload_{uid}.txt"
    try:
        path = await message.download(file_name=str(tmp))
        text = Path(path).read_text(encoding="utf-8-sig", errors="ignore")
    except Exception as e:
        await message.reply_text(f"❌ Gagal membaca file: `{e}`")
        return
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    if is_import:
        login_states.pop(uid, None)
        await do_import_blacklist(uid, text, message)
    else:
        await start_check(uid, st, message, text)


@app.on_message(filters.command("ip") & filters.private)
async def ip_cmd(_, message: Message):
    """Alamat IP keluar bot ini (untuk membandingkan antar-instance Railway)."""
    http, _, _ = get_http()
    ip = None
    for url in ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"):
        try:
            r = await http.get(url, timeout=8)
            t = r.text.strip()
            if r.status_code == 200 and re.fullmatch(r"[0-9a-fA-F:.]{3,45}", t):
                ip = t
                break
        except Exception:
            continue
    if not ip:
        await message.reply_text("⚠️ Gagal mengambil IP (layanan cek IP tidak terjangkau).")
        return
    await message.reply_text(
        f"🌐 IP keluar bot ini: `{ip}`\n"
        "Jalankan /ip di tiap instance lalu bandingkan. IP bisa berubah setelah restart/deploy."
    )


# ----------------------------------------------------
# 7. Handler input teks
# ----------------------------------------------------
@app.on_message(filters.private & filters.text & ~filters.regex(r"^/"))
async def handle_inputs(_, message: Message):
    uid = message.from_user.id
    text = message.text.strip()

    if uid in login_states:
        await handle_login(uid, login_states[uid], message, text)
        return

    st = user_states.get(uid)
    if not st:
        return

    if st["step"] == "SELECT_MODE":
        if text in ("1", "2"):
            st["mode"] = text
            st["step"] = "WAIT_LIST"
            await message.reply_text(
                "📝 **Kirim list based-on (satu per baris):**\n\n"
                "Contoh:\nidol claude\nmulchar kucing\ntamping claude\n\n"
                "📎 Kalau listnya panjang, kirim sebagai **file .txt** (isi sama, satu baris `kategori base`). "
                "Teks biasa dibatasi Telegram (maks ~4000 karakter).\n\n"
                f"Kategori: {', '.join(GENERATORS)}"
            )
        else:
            await message.reply_text("❌ Ketik 1 atau 2.")
    elif st["step"] == "WAIT_LIST":
        await start_check(uid, st, message, text)


async def handle_login(uid: int, ls: dict, message: Message, text: str):
    step = ls["step"]

    if step == "IMPORT_BL":
        login_states.pop(uid, None)
        await do_import_blacklist(uid, text, message)

    elif step == "BOT_TOKEN":
        tokens = list(dict.fromkeys(TOKEN_RE.findall(text)))
        if not tokens:
            await message.reply_text("❌ Tidak ada token bot yang valid. Kirim ulang, atau /cancel.")
            return
        code = ls.get("code")
        login_states.pop(uid, None)
        await try_delete(message)
        await add_bot_tokens(uid, tokens, message, first_code=code)

    elif step == "PHONE":
        cl = Client(f"login_{uid}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
        try:
            await cl.connect()
            sent = await cl.send_code(text)
        except Exception as e:
            try:
                await cl.disconnect()
            except Exception:
                pass
            login_states.pop(uid, None)
            await message.reply_text(f"❌ Gagal kirim kode: `{e}`")
            return
        ls.update(client=cl, phone=text, hash=sent.phone_code_hash, step="CODE")
        await message.reply_text(
            "📩 Kode dikirim ke Telegram akun tsb.\n"
            "Kirim kodenya **dengan spasi** (contoh `1 2 3 4 5`) supaya tidak diblokir Telegram."
        )

    elif step == "CODE":
        try:
            await ls["client"].sign_in(ls["phone"], ls["hash"], text.replace(" ", ""))
        except SessionPasswordNeeded:
            ls["step"] = "PASSWORD"
            await message.reply_text("🔐 Akun memakai 2FA. Kirim password-nya:")
            return
        except (PhoneCodeInvalid, PhoneCodeExpired):
            await clear_login(uid)
            await message.reply_text("❌ Kode salah/kedaluwarsa. Ulangi dari awal.")
            return
        except Exception as e:
            await clear_login(uid)
            await message.reply_text(f"❌ Login gagal: `{e}`")
            return
        await finish_keeper_login(uid, ls, message)

    elif step == "PASSWORD":
        try:
            await ls["client"].check_password(text)
        except Exception as e:
            await clear_login(uid)
            await message.reply_text(f"❌ Password salah: `{e}`")
            return
        await finish_keeper_login(uid, ls, message)


async def finish_keeper_login(uid: int, ls: dict, message: Message):
    cl: Client = ls["client"]
    me = await cl.get_me()
    session = await cl.export_session_string()
    await clear_login(uid)

    us = user_sessions(uid)
    code = ls.get("kcode") or next_free_keeper_code(us)
    old = us["keepers"].get(code)
    if old:
        await drop_client(old["session"])  # kode sama -> diganti
    us["keepers"][code] = {
        "session": session,
        "name": f"@{me.username}" if me.username else (me.first_name or "-"),
        "limit_until": 0,
    }
    sessions_db.save()
    await message.reply_text(
        f"✅ Keeper kode {code} login: `{me.first_name}` ({me.id})\n"
        f"Total Keeper: {len(us['keepers'])}. Tambah lagi dengan /keeper, lihat status di /akun."
    )


def usn_cancelled(st: dict, usn: str) -> bool:
    """Username dilewati kalau SEMUA based-on yang menghasilkannya sudah dibatalkan."""
    ub = st.get("usn_bases", {}).get(usn)
    if not ub:
        return False
    bases = st["bases"]
    return all(bases[i]["cancelled"] for i in ub)


def find_bases(st: dict, tokens: list):
    """
    Cari based-on dari argumen /cancelbase. Return (daftar_nomor, pesan_error).
    Bisa nomor (`2 5 7`) atau teks (`idol claude` / `claude` kalau hanya cocok satu).
    """
    bases = st["bases"]
    if all(t.isdigit() for t in tokens):
        nums = [int(t) for t in tokens]
        bad = [n for n in nums if n not in bases]
        if bad:
            return [], f"Nomor tidak ada: {', '.join(map(str, bad))} (nomor yang ada 1-{len(bases)})."
        return list(dict.fromkeys(nums)), None
    label = " ".join(" ".join(tokens).lower().split())
    exact = [i for i, b in bases.items() if " ".join(b["label"].lower().split()) == label]
    if exact:
        return exact, None
    by_base = [i for i, b in bases.items() if " ".join(b["label"].lower().split()[1:]) == label]
    if len(by_base) == 1:
        return by_base, None
    if len(by_base) > 1:
        daftar = ", ".join(f"{i} ({bases[i]['label']})" for i in by_base[:8])
        return [], f"Cocok dengan lebih dari satu based-on: {daftar}. Pakai nomornya."
    return [], f"Based-on `{label}` tidak ditemukan. Lihat daftar dengan /bases."


def parse_base_list(text: str):
    """
    Baca list "kategori base" per baris. Toleran terhadap CRLF, tab, spasi ganda, BOM, spasi non-breaking.
    Return (daftar (kategori, base), jumlah baris berformat salah).
    """
    text = text.replace("\ufeff", "").replace("\u00a0", " ").replace("\t", " ")
    pairs, bad = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            bad += 1
            continue
        pairs.append((parts[0], parts[1].strip()))
    return pairs, bad


async def start_check(uid: int, st: dict, message: Message, text: str):
    targets, unknown = set(), []
    bases, seen_keys = {}, set()
    pairs, bad_lines = parse_base_list(text)
    for cat, base in pairs:
        if cat.lower() not in GENERATORS:
            unknown.append(cat)
            continue
        key = (cat.lower(), base.lower().replace(" ", ""))
        if key in seen_keys:  # baris kembar diabaikan
            continue
        seen_keys.add(key)
        usns = set(generate_usernames(cat, base))
        bases[len(bases) + 1] = {"label": f"{cat.lower()} {base}", "usns": usns, "cancelled": False}
        targets.update(usns)

    bl = user_blacklist(uid)
    for b in bases.values():
        b["usns"] = {u for u in b["usns"] if u not in bl}
    skipped = len([u for u in targets if u in bl])
    targets = sorted(u for u in targets if u not in bl)

    if not targets:
        user_states.pop(uid, None)
        msg = "❌ Tidak ada username yang perlu dicek."
        if skipped:
            msg += f"\n({skipped} variasi dilewati karena ada di blacklist)"
        if unknown:
            msg += f"\nKategori tidak dikenal: {', '.join(set(unknown))}"
        if bad_lines:
            msg += f"\nBaris berformat salah (harus `kategori base`): {bad_lines}"
        await message.reply_text(msg)
        return

    mode = st["mode"]
    usn_bases = defaultdict(set)
    for i, b in bases.items():
        for u in b["usns"]:
            usn_bases[u].add(i)
    st.update(step="RUNNING", active=True, bases=bases, usn_bases=usn_bases, checked=set())
    us = user_sessions(uid)
    n_active = len([a for a in us["checkers"].values() if not a.get("paused")])
    nk = len(us["keepers"])
    confirm = (
        f"{nk} Keeper (otomatis pindah kalau limit), "
        + ("klaim otomatis: YA" if KEEPER_CLAIM else "hanya cek, tidak mengklaim")
        if (nk and KEEPER_PRECHECK)
        else "TIDAK ada, hanya calon dari Fragment (pasang /keeper)"
    )
    await message.reply_text(
        f"⚡️ **Pengecekan dimulai!**\n"
        f"🔹 Target dicek: `{len(targets)}` USN\n"
        f"🔹 Dilewati (blacklist fragment/banned/invalid): `{skipped}`\n"
        f"🔹 Bot Checker aktif: `{n_active}`\n"
        f"🔹 Konfirmasi final: {confirm}\n"
        f"🔹 Estimasi kasar: ~{fmt_hms(int(len(targets) * pace_delay(n_active) / max(n_active, 1)) + 1)} "
        f"(paralel, jeda ~{pace_delay(n_active):.1f}s/bot). Pantau dengan /status\n"
        f"🔹 Mode: {'Sekali selesai' if mode == '1' else 'Looping terus'}"
        + (f"\n⚠️ Kategori tidak dikenal: {', '.join(set(unknown))}" if unknown else "")
        + (f"\n⚠️ Baris berformat salah dilewati: {bad_lines}" if bad_lines else "")
        + f"\n🔹 Based-on: {len(bases)} (lihat /bases, batalkan satuan dengan /cancelbase [nomor])"
    )
    asyncio.create_task(run_checker_loop(uid, message, targets, mode))


# ----------------------------------------------------
# 8. Worker utama (paralel: 1 worker per bot checker, antrean bersama)
# ----------------------------------------------------
async def run_checker_loop(uid: int, message: Message, targets: list, mode: str):
    st = user_states[uid]
    us = user_sessions(uid)
    wording = wording_db.data.get(str(uid), "")

    queue: asyncio.Queue = asyncio.Queue()
    finished = asyncio.Event()
    notify_lock = asyncio.Lock()
    keeper_lock = asyncio.Lock()
    stats, per_acc, tries = Counter(), Counter(), Counter()
    done = set()
    ctl = {"out": 0, "keeper_until": 0.0, "no_accounts": False}
    workers: dict = {}
    started = time.monotonic()

    # ---------- pelacak "terakhir terlihat dipakai" ----------
    seen_taken = seen_db.data.setdefault(str(uid), {})
    seen_state = {"dirty": False}

    def mark_taken(usn: str):
        seen_taken[usn] = int(time.time())
        seen_state["dirty"] = True

    def release_info(usn: str, confirmed: bool) -> str:
        """Teks jejak waktu untuk username yang terlihat kosong + catat ke riwayat (/rilis)."""
        now = time.time()
        last = seen_taken.get(usn)
        rl = released_db.data.setdefault(str(uid), [])
        rl.append({"u": usn, "last": last, "found": int(now), "ok": confirmed})
        del rl[:-1000]  # simpan 1000 terbaru
        try:
            released_db.save()
        except OSError as e:
            print(f"[released-save-error] {e}")
        if last:
            return (
                f"🕒 Terakhir terlihat dipakai: {fmt_clock(last)} ({fmt_hms(now - last)} lalu)\n"
                f"🕒 Pertama terlihat kosong: {fmt_clock(now)}\n"
                "↳ Dilepas di antara dua waktu itu (Telegram tidak menyediakan waktu pasti)."
            )
        return (
            f"🕒 Pertama terlihat kosong: {fmt_clock(now)}\n"
            "↳ Belum ada catatan kapan username ini terakhir terlihat dipakai."
        )

    def alive() -> bool:
        return st["active"] and not finished.is_set()

    async def notify(text: str, markup=None):
        async with notify_lock:
            for _ in range(3):
                try:
                    await message.reply_text(text, reply_markup=markup)
                    return
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                except Exception as e:
                    print(f"[notify-error] {e}")
                    return

    # ---------- Keeper bekerja paralel: 1 username per Keeper pada satu waktu ----------
    busy: set = set()

    async def acquire_keeper(tried: set):
        """Ambil Keeper yang siap & sedang menganggur. None kalau tidak ada Keeper siap sama sekali."""
        while alive():
            ready = [c for c in ready_keeper_codes(us, uid) if c not in tried]
            if not ready:
                return None
            free = [c for c in ready if c not in busy]
            if free:
                busy.add(free[0])
                return free[0]
            await asyncio.sleep(0.2)  # semua Keeper siap tapi sedang sibuk, tunggu giliran
        return None

    # ---------- hasil per username ----------
    async def handle_available(usn: str):
        suffix = f"\n\n{wording}" if wording else ""
        keeper_note = ""

        if not us["keepers"]:
            keeper_note = "\n(Belum dikonfirmasi Telegram, bisa saja invalid/banned. Pasang /keeper agar otomatis. Kalau ditolak, tekan 🚫 Invalid.)"
        else:
            tried: set = set()
            while alive():
                code = await acquire_keeper(tried)
                if code is None:
                    break
                try:
                    status, detail = await keeper_try(us, code, usn, claim=KEEPER_CLAIM)
                finally:
                    busy.discard(code)
                tried.add(code)  # kalau Keeper ini gagal, username dicoba di Keeper lain

                if status == "not_free":
                    if detail == "fragment":
                        stats["fragment"] += 1
                        add_blacklist(uid, usn, "fragment")
                    elif detail == "invalid":
                        stats["invalid"] += 1  # ditolak Telegram -> BUKAN available
                        add_blacklist(uid, usn, "invalid")
                    else:
                        stats["taken"] += 1
                        mark_taken(usn)
                    return
                if status == "free":  # lolos cek Telegram, sengaja tidak diklaim
                    done.add(usn)
                    stats["available"] += 1
                    kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🚫 Invalid (blacklist)", callback_data=f"u:i:{usn}"),
                        InlineKeyboardButton("✅ Sudah diklaim", callback_data=f"u:o:{usn}"),
                    ]])
                    info = release_info(usn, True)
                    await notify(
                        f"🎯 **AVAILABLE (dikonfirmasi Telegram):** @{usn}\n{info}\n"
                        f"(Belum diklaim. Klaim manual dengan `/keep {usn}`){suffix}", kb
                    )
                    return
                if status == "claimed":
                    done.add(usn)
                    stats["claimed"] += 1
                    info = release_info(usn, True)
                    await notify(f"🔥 **BERHASIL DIKLAIM:** @{usn} (Keeper {code})\n{info}{suffix}")
                    return
                if status == "unclaimable":
                    add_blacklist(uid, usn, "banned")  # lolos semua cek tapi gagal klaim
                    stats["banned"] += 1
                    return
                if status == "claim_error":
                    print(f"[claim-error] {usn}: {detail}")
                    await notify(f"⚠️ Gagal klaim `{usn}`: `{detail}`")
                    return
                # flood / full / dead / error -> pindah ke Keeper berikutnya
                kd = us["keepers"][code]
                if status == "flood":
                    n = kd.get("last_limit_after")
                    after = f" setelah {n} cek" if n else ""
                    msg = (f"⏳ Keeper {code} kena limit {detail} detik ({fmt_duration(detail)}){after}. "
                           f"Jeda Keeper ini dinaikkan jadi ~{kd.get('delay', 0):.1f}d")
                elif status == "full":
                    msg = f"📦 Keeper {code} penuh (`{str(detail)[:60]}`)"
                elif status == "dead":
                    msg = f"❌ Keeper {code} sesi mati, perlu login ulang"
                else:
                    msg = f"⚠️ Keeper {code} error (`{str(detail)[:60]}`)"
                left = [c for c in ready_keeper_codes(us, uid) if c not in tried]
                await notify(msg + (", dialihkan ke Keeper lain." if left else ". Tidak ada Keeper lain yang siap."))
            # sampai sini = tidak ada Keeper yang berhasil mengonfirmasi
            keeper_note = "\n(Semua Keeper sedang limit/penuh/mati, belum dikonfirmasi Telegram. Coba /keep nanti)"

        done.add(usn)
        stats["available"] += 1
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚫 Invalid (blacklist)", callback_data=f"u:i:{usn}"),
            InlineKeyboardButton("✅ Sudah diklaim", callback_data=f"u:o:{usn}"),
        ]])
        info = release_info(usn, False)
        await notify(f"🎯 **CALON AVAILABLE (belum dikonfirmasi Telegram):** @{usn}\n{info}{keeper_note}{suffix}", kb)

    async def handle_result(usn: str, result: str):
        if result == "fragment":
            stats["fragment"] += 1
            add_blacklist(uid, usn, "fragment")
        elif result == "available":
            await handle_available(usn)
        elif result == "invalid":
            stats["invalid"] += 1
            add_blacklist(uid, usn, "invalid")
        else:
            stats[result] += 1  # taken / unknown
            if result == "taken":
                mark_taken(usn)

    # ---------- worker per bot ----------
    def current_delay() -> float:
        n = len([a for a in us["checkers"].values() if not a.get("paused")])
        return pace_delay(n)

    async def worker(code: str):
        acc = us["checkers"].get(code)
        if not acc:
            return
        await asyncio.sleep(random.uniform(0, current_delay()))

        while alive():
            acc = us["checkers"].get(code)
            if not acc or acc.get("paused"):
                return

            try:
                usn = queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.2)
                continue

            if usn in user_blacklist(uid):
                ctl["out"] -= 1
                continue

            if usn_cancelled(st, usn):  # based-on-nya sudah dibatalkan -> lewati tanpa cek
                stats["cancelled"] += 1
                ctl["out"] -= 1
                continue

            t0 = time.monotonic()
            try:
                result = await verify_username(usn)

            except FloodWait as e:
                queue.put_nowait(usn)  # kembalikan ke antrean, bot ini istirahat (bot lain lanjut)
                if e.value >= NOTIFY_MIN_FLOOD:
                    await notify(f"⏳ Bot kode {code} kena limit {e.value} detik ({fmt_duration(e.value)}), istirahat.")
                await asyncio.sleep(min(e.value, 300) + 1)
                continue

            except Exception as e:
                s = str(e)
                tries[usn] += 1
                if tries[usn] <= MAX_RETRY:
                    queue.put_nowait(usn)
                else:
                    stats["error"] += 1
                    ctl["out"] -= 1
                    print(f"[error] kode {code} / {usn}: {s}")
                await asyncio.sleep(current_delay())
                continue

            per_acc[code] += 1
            await handle_result(usn, result)
            st["checked"].add(usn)
            ctl["out"] -= 1

            gap = current_delay() * random.uniform(0.8, 1.2)
            await asyncio.sleep(max(0.0, gap - (time.monotonic() - t0)))

    async def safe_worker(code: str):
        try:
            await worker(code)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[worker-crash] kode {code}: {e}")
            await asyncio.sleep(2)

    async def supervisor():
        while not finished.is_set():
            for code, a in list(us["checkers"].items()):
                if a.get("paused"):
                    continue
                t = workers.get(code)
                if t is None or t.done():
                    workers[code] = asyncio.create_task(safe_worker(code))
            await asyncio.sleep(1)

    st.update(ctl=ctl, stats=stats, per_acc=per_acc, started=started)
    async def autosave():
        while not finished.is_set():
            await asyncio.sleep(120)
            if seen_state["dirty"]:
                seen_state["dirty"] = False
                try:
                    seen_db.save()
                except OSError as e:
                    print(f"[seen-save-error] {e}")

    sup = asyncio.create_task(supervisor())
    saver = asyncio.create_task(autosave())
    try:
        while alive():
            bl = user_blacklist(uid)
            pending = [u for u in targets if u not in bl and u not in done and not usn_cancelled(st, u)]
            if not pending:
                break

            ctl["out"] = len(pending)
            ctl["round_total"] = len(pending)
            for u in pending:
                queue.put_nowait(u)

            while ctl["out"] > 0 and alive():
                if not any(not a.get("paused") for a in us["checkers"].values()):
                    ctl["no_accounts"] = True
                    break
                await asyncio.sleep(0.5)

            if ctl["no_accounts"] or mode == "1":
                break
    finally:
        finished.set()
        sup.cancel()
        saver.cancel()
        live = [t for t in workers.values() if not t.done()]
        if live:
            _, pending_tasks = await asyncio.wait(live, timeout=30)
            for t in pending_tasks:
                t.cancel()
        user_states.pop(uid, None)
        sessions_db.save()
        cutoff = time.time() - SEEN_TTL_DAYS * 86400
        for u in [u for u, t in seen_taken.items() if t < cutoff]:
            del seen_taken[u]
        try:
            seen_db.save()
        except OSError as e:
            print(f"[seen-save-error] {e}")

        if ctl["no_accounts"]:
            await notify(
                "⏸️ Tidak ada bot Checker yang aktif (semua dipause/gagal). "
                "Aktifkan dengan `/active [kode]` lalu jalankan /check lagi."
            )
        elapsed = max(time.monotonic() - started, 0.001)
        total = sum(per_acc.values())
        per = ", ".join(f"kode {c}: {n}" for c, n in sorted(per_acc.items(), key=lambda x: int(x[0]))) or "-"
        await notify(
            "🏁 **Pengecekan selesai.**\n"
            f"⏱ {fmt_hms(elapsed)} | {total} cek | {total / elapsed:.1f} cek/detik\n"
            f"🔥 Diklaim: {stats['claimed']}\n"
            f"🎯 Available/calon (belum diklaim): {stats['available']}\n"
            f"🚫 Fragment (di-blacklist): {stats['fragment']}\n"
            f"🚫 Banned (di-blacklist): {stats['banned']}\n"
            f"🚫 Invalid (di-blacklist): {stats['invalid']}\n"
            f"❓ Unknown (judul Fragment tak dikenal, tidak dilaporkan): {stats['unknown']}\n"
            f"❌ Taken: {stats['taken']} | Error: {stats['error']}\n"
            f"⏭ Dilewati (based-on dibatalkan): {stats['cancelled']}\n"
            f"📊 Cek per bot → {per}"
        )


if __name__ == "__main__":
    app.run()
