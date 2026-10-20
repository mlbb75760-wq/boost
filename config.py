import os
import sys
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        sys.exit(f"❌ Переменная окружения {name} не задана. Укажите её в .env или в настройках хостинга.")
    return value


def _int_env(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"❌ Переменная {name} должна быть числом, а сейчас там: {raw!r}")


BOT_TOKEN = _require("BOT_TOKEN")
DATABASE_URL = _require("DATABASE_URL")
if DATABASE_URL.startswith("postgres://"):
    # Railway иногда выдаёт старый формат схемы, asyncpg требует postgresql://
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Чат админа: заявки на пополнение, обращения в поддержку, админ-панель.
ADMIN_CHAT_ID = _int_env("ADMIN_CHAT_ID", 0)

# Необязательно: список user_id админов через запятую. Если задан — управлять ботом
# смогут только они (даже если ADMIN_CHAT_ID — группа, где есть посторонние).
ADMIN_USER_IDS = {
    int(x) for x in (os.getenv("ADMIN_USER_IDS") or "").replace(" ", "").split(",")
    if x.lstrip("-").isdigit()
}

# Канал/чат, куда падают новые заявки (вместо личного чата с админом).
# Бот должен быть админом канала с правом отправки сообщений.
ORDERS_CHAT_ID = _int_env("ORDERS_CHAT_ID", -1004485065097)

# Реквизиты обязательны: раньше при пустой переменной клиентам показывалась карта 0000 0000 0000 0000
CLICK_CARD = _require("CLICK_CARD")
PAYNET_CARD = _require("PAYNET_CARD")

SITE_URL = "https://xaruem.github.io/BoostCent/"

# Часовой пояс для отображения времени пользователям/админам (Узбекистан = UTC+5).
# В базе всё хранится в UTC.
TZ_OFFSET_HOURS = _int_env("TZ_OFFSET_HOURS", 5)

# Защита от спама некорректными ссылками/числами
INVALID_WINDOW_MINUTES = 10      # окно времени для подсчёта ошибок
INVALID_LIMIT = 5                # сколько ошибок за окно приводит к бану
SOFT_BAN_MINUTES = 60            # первый бан — временный (минуты)
# второй бан подряд (после истечения первого) — постоянный (is_permanent=True)

MIN_TOPUP_AMOUNT = 100
MAX_PENDING_TOPUPS = 3           # сколько непроверенных чеков одновременно может быть у одного клиента
HELP_COOLDOWN_SECONDS = 30       # пауза между сообщениями в поддержку

# Redis для хранения состояний диалогов (необязательно; без него состояния сбрасываются при рестарте)
REDIS_URL = (os.getenv("REDIS_URL") or "").strip()
# Сбрасывать ли накопившиеся за время простоя сообщения при старте (по умолчанию НЕТ — чеки не теряются)
DROP_PENDING_UPDATES = (os.getenv("DROP_PENDING_UPDATES") or "").strip().lower() in ("1", "true", "yes")
