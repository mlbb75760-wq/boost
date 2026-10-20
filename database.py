import datetime

import asyncpg

from config import DATABASE_URL, INVALID_WINDOW_MINUTES, INVALID_LIMIT, SOFT_BAN_MINUTES

pool: asyncpg.Pool | None = None

FINAL_STATUSES = ("Сделано", "Отменено")
# Все даты в БД — UTC. NOW() без приведения даёт время часового пояса сессии БД,
# из-за чего сравнения с датами из Python могли «уезжать».
UTC_NOW = "(NOW() AT TIME ZONE 'UTC')"


class InsufficientFunds(Exception):
    """На балансе не хватает средств для списания."""


class UserNotFound(Exception):
    """Пользователя нет в базе (и создавать его для этой операции нельзя)."""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as con:
        await con.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            balance BIGINT NOT NULL DEFAULT 0,
            is_banned BOOLEAN NOT NULL DEFAULT FALSE,
            ban_permanent BOOLEAN NOT NULL DEFAULT FALSE,
            ban_until TIMESTAMP,
            ban_count INT NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT {UTC_NOW}
        );

        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            platform TEXT NOT NULL,
            service_key TEXT NOT NULL,
            variant TEXT NOT NULL,
            amount INT NOT NULL,
            link TEXT NOT NULL,
            price BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'В ожидании',
            created_at TIMESTAMP NOT NULL DEFAULT {UTC_NOW}
        );

        CREATE TABLE IF NOT EXISTS topups (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            method TEXT NOT NULL,
            file_id TEXT,
            amount BIGINT,
            status TEXT NOT NULL DEFAULT 'В ожидании',
            created_at TIMESTAMP NOT NULL DEFAULT {UTC_NOW}
        );

        CREATE TABLE IF NOT EXISTS balance_logs (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            admin_id BIGINT,
            amount BIGINT NOT NULL,
            balance_after BIGINT NOT NULL,
            reason TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT {UTC_NOW}
        );

        CREATE TABLE IF NOT EXISTS free_usage (
            user_id BIGINT NOT NULL,
            service_key TEXT NOT NULL,
            last_used_at TIMESTAMP NOT NULL,
            PRIMARY KEY (user_id, service_key)
        );

        CREATE TABLE IF NOT EXISTS fsm_states (
            key TEXT PRIMARY KEY,
            state TEXT,
            data TEXT NOT NULL DEFAULT '{{}}',
            updated_at TIMESTAMP NOT NULL DEFAULT {UTC_NOW}
        );

        CREATE TABLE IF NOT EXISTS invalid_events (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT {UTC_NOW}
        );

        -- миграции для уже существующих баз (идемпотентны)
        ALTER TABLE balance_logs ADD COLUMN IF NOT EXISTS kind TEXT;
        ALTER TABLE users ALTER COLUMN created_at SET DEFAULT {UTC_NOW};
        ALTER TABLE orders ALTER COLUMN created_at SET DEFAULT {UTC_NOW};
        ALTER TABLE topups ALTER COLUMN created_at SET DEFAULT {UTC_NOW};
        ALTER TABLE balance_logs ALTER COLUMN created_at SET DEFAULT {UTC_NOW};
        ALTER TABLE invalid_events ALTER COLUMN created_at SET DEFAULT {UTC_NOW};

        -- тип операции нужен, чтобы возвраты и ручные правки не считались «пополнениями» в статистике
        UPDATE balance_logs SET kind = CASE
            WHEN reason LIKE 'Пополнение%' AND amount > 0 THEN 'topup'
            WHEN reason LIKE 'Возврат%' THEN 'refund'
            WHEN reason LIKE 'Заказ:%' THEN 'order'
            ELSE 'adjust'
        END
        WHERE kind IS NULL;

        DELETE FROM fsm_states WHERE updated_at < {UTC_NOW} - INTERVAL '7 days';

        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders (user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
        CREATE INDEX IF NOT EXISTS idx_orders_platform_service ON orders (platform, service_key);
        CREATE INDEX IF NOT EXISTS idx_topups_status ON topups (status);
        CREATE INDEX IF NOT EXISTS idx_balance_logs_kind ON balance_logs (kind);
        CREATE INDEX IF NOT EXISTS idx_invalid_user_time ON invalid_events (user_id, created_at);
        """)


async def close_db():
    if pool:
        await pool.close()


# ---------------------------------------------------------------- users
async def ensure_user(user_id: int, username: str | None) -> bool:
    """Создаёт пользователя или обновляет username. Возвращает True, если пользователь новый.
    Один атомарный запрос (раньше SELECT + INSERT могли столкнуться при двух одновременных /start)."""
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "INSERT INTO users (user_id, username) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username "
            "RETURNING (xmax = 0) AS inserted",
            user_id, username,
        )
        return bool(row["inserted"])


async def get_balance(user_id: int) -> int:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT balance FROM users WHERE user_id=$1", user_id)
        return row["balance"] if row else 0


async def _apply_balance(con, user_id: int, delta: int, admin_id: int | None, reason: str,
                         kind: str, create_if_missing: bool = False) -> int:
    """Меняет баланс и пишет лог. Вызывать внутри транзакции.
    Баланс не может уйти в минус (InsufficientFunds). Несуществующий пользователь не создаётся
    молча (UserNotFound), если не разрешено create_if_missing — иначе опечатка в ID при
    ручном пополнении создавала «призрака» с деньгами."""
    row = await con.fetchrow(
        "UPDATE users SET balance = balance + $2 "
        "WHERE user_id = $1 AND balance + $2 >= 0 RETURNING balance",
        user_id, delta,
    )
    if row is None:
        exists = await con.fetchval("SELECT 1 FROM users WHERE user_id=$1", user_id)
        if exists:
            raise InsufficientFunds()
        if delta < 0 or not create_if_missing:
            raise UserNotFound()
        row = await con.fetchrow(
            "INSERT INTO users (user_id, balance) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET balance = users.balance + EXCLUDED.balance "
            "RETURNING balance",
            user_id, delta,
        )
    new_balance = row["balance"]
    await con.execute(
        "INSERT INTO balance_logs (user_id, admin_id, amount, balance_after, reason, kind) "
        "VALUES ($1,$2,$3,$4,$5,$6)",
        user_id, admin_id, delta, new_balance, reason, kind,
    )
    return new_balance


async def change_balance(user_id: int, delta: int, admin_id: int | None = None, reason: str = "",
                         kind: str = "adjust", create_if_missing: bool = False) -> int:
    async with pool.acquire() as con:
        async with con.transaction():
            return await _apply_balance(con, user_id, delta, admin_id, reason, kind, create_if_missing)


async def get_balance_logs(limit: int = 20):
    async with pool.acquire() as con:
        return await con.fetch(
            "SELECT * FROM balance_logs ORDER BY created_at DESC LIMIT $1", limit
        )


# ---------------------------------------------------------------- бан / спам-защита
async def register_invalid_event(user_id: int) -> dict:
    """Логирует некорректный ввод, проверяет частоту и банит при превышении.
    Возвращает {"banned": bool, "permanent": bool, "until": datetime|None, "count": int}"""
    now = _utcnow()
    async with pool.acquire() as con:
        async with con.transaction():
            # строка пользователя нужна, чтобы UPDATE бана реально сработал
            await con.execute(
                "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id
            )
            # таблица не должна расти бесконечно
            await con.execute(
                "DELETE FROM invalid_events WHERE created_at < $1", now - datetime.timedelta(days=1)
            )
            await con.execute(
                "INSERT INTO invalid_events (user_id, created_at) VALUES ($1, $2)", user_id, now
            )
            since = now - datetime.timedelta(minutes=INVALID_WINDOW_MINUTES)
            count = await con.fetchval(
                "SELECT COUNT(*) FROM invalid_events WHERE user_id=$1 AND created_at >= $2",
                user_id, since,
            )
            if count < INVALID_LIMIT:
                return {"banned": False, "permanent": False, "until": None, "count": count}

            prev_bans = await con.fetchval("SELECT ban_count FROM users WHERE user_id=$1", user_id) or 0
            await con.execute("DELETE FROM invalid_events WHERE user_id=$1", user_id)

            if prev_bans == 0:
                until = now + datetime.timedelta(minutes=SOFT_BAN_MINUTES)
                await con.execute(
                    "UPDATE users SET is_banned=TRUE, ban_permanent=FALSE, ban_until=$2, ban_count=ban_count+1 "
                    "WHERE user_id=$1",
                    user_id, until,
                )
                return {"banned": True, "permanent": False, "until": until, "count": count}
            await con.execute(
                "UPDATE users SET is_banned=TRUE, ban_permanent=TRUE, ban_until=NULL, ban_count=ban_count+1 "
                "WHERE user_id=$1",
                user_id,
            )
            return {"banned": True, "permanent": True, "until": None, "count": count}


async def is_banned(user_id: int) -> bool:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT is_banned, ban_permanent, ban_until FROM users WHERE user_id=$1", user_id
        )
        if not row or not row["is_banned"]:
            return False
        if row["ban_permanent"]:
            return True
        if row["ban_until"] and row["ban_until"] > _utcnow():
            return True
        # временный бан истёк — снимаем автоматически
        await con.execute(
            "UPDATE users SET is_banned=FALSE, ban_until=NULL WHERE user_id=$1", user_id
        )
        return False


async def unban_user(user_id: int):
    """Полный сброс: снимает бан, обнуляет счётчик банов и накопленные ошибки —
    иначе после разбана первая же опечатка могла сразу вернуть постоянный бан."""
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                "UPDATE users SET is_banned=FALSE, ban_permanent=FALSE, ban_until=NULL, ban_count=0 "
                "WHERE user_id=$1",
                user_id,
            )
            await con.execute("DELETE FROM invalid_events WHERE user_id=$1", user_id)


# ---------------------------------------------------------------- бесплатное продвижение (cooldown)
async def check_free_cooldown(user_id: int, service_key: str, cooldown_days: int) -> tuple[bool, datetime.datetime | None]:
    """Возвращает (доступно_ли, когда_станет_доступно). Время — UTC."""
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT last_used_at FROM free_usage WHERE user_id=$1 AND service_key=$2",
            user_id, service_key,
        )
        if not row:
            return True, None
        next_available = row["last_used_at"] + datetime.timedelta(days=cooldown_days)
        if _utcnow() >= next_available:
            return True, None
        return False, next_available


# ---------------------------------------------------------------- заказы
async def create_order_atomic(user_id: int, platform: str, service_key: str, variant: str,
                              amount: int, link: str, price: int, cooldown_days: int | None,
                              reason: str) -> tuple[int | None, datetime.datetime | None]:
    """Списание денег (или проверка/отметка бесплатного лимита) и создание заказа —
    в ОДНОЙ транзакции: либо всё, либо ничего.
    Возвращает (order_id, None) при успехе или (None, когда_станет_доступно), если
    бесплатный лимит ещё не истёк. При нехватке средств бросает InsufficientFunds."""
    async with pool.acquire() as con:
        async with con.transaction():
            # заказы одного пользователя оформляются строго по очереди
            await con.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            if price > 0:
                await _apply_balance(con, user_id, -price, None, reason, kind="order")
            elif cooldown_days:
                last = await con.fetchval(
                    "SELECT last_used_at FROM free_usage WHERE user_id=$1 AND service_key=$2",
                    user_id, service_key,
                )
                if last is not None:
                    next_available = last + datetime.timedelta(days=cooldown_days)
                    if _utcnow() < next_available:
                        return None, next_available
                await con.execute(
                    f"INSERT INTO free_usage (user_id, service_key, last_used_at) VALUES ($1,$2,{UTC_NOW}) "
                    f"ON CONFLICT (user_id, service_key) DO UPDATE SET last_used_at={UTC_NOW}",
                    user_id, service_key,
                )
            row = await con.fetchrow(
                "INSERT INTO orders (user_id, platform, service_key, variant, amount, link, price) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
                user_id, platform, service_key, variant, amount, link, price,
            )
            return row["id"], None


async def get_order(order_id: int):
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)


async def get_user_orders(user_id: int, limit: int = 20):
    async with pool.acquire() as con:
        return await con.fetch(
            "SELECT * FROM orders WHERE user_id=$1 ORDER BY id DESC LIMIT $2", user_id, limit
        )


async def set_order_status(order_id: int, status: str) -> bool:
    """Меняет статус, только если заказ ещё не в финальном статусе (Сделано/Отменено).
    Раньше устаревшая кнопка «Сделано» могла перезаписать «Отменено» уже после возврата денег."""
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "UPDATE orders SET status=$2 WHERE id=$1 AND status NOT IN ('Сделано','Отменено') RETURNING id",
            order_id, status,
        )
        return row is not None


async def cancel_order_with_refund(order_id: int, admin_id: int | None, reason: str):
    """Отмена заказа и возврат денег в ОДНОЙ транзакции (раньше это были два отдельных
    запроса — сбой между ними оставлял заказ отменённым без возврата).
    Возвращает строку заказа, если отмена выполнена этим вызовом, иначе None
    (заказ не найден или уже в финальном статусе)."""
    async with pool.acquire() as con:
        async with con.transaction():
            order = await con.fetchrow(
                "UPDATE orders SET status='Отменено' "
                "WHERE id=$1 AND status NOT IN ('Сделано','Отменено') RETURNING *",
                order_id,
            )
            if order is None:
                return None
            if order["price"] > 0:
                await _apply_balance(
                    con, order["user_id"], order["price"], admin_id,
                    f"Возврат за отменённый заказ №{order_id}: {reason}",
                    kind="refund", create_if_missing=True,
                )
            return order


def _orders_where(status: str | None, platform: str | None, service_key: str | None):
    conditions = []
    params = []
    if status:
        params.append(status)
        conditions.append(f"status=${len(params)}")
    if platform:
        params.append(platform)
        conditions.append(f"platform=${len(params)}")
    if service_key:
        params.append(service_key)
        conditions.append(f"service_key=${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


async def get_orders_page(offset: int = 0, limit: int = 10, status: str | None = None,
                           platform: str | None = None, service_key: str | None = None):
    where, params = _orders_where(status, platform, service_key)
    async with pool.acquire() as con:
        params_full = params + [limit, offset]
        query = f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT ${len(params)+1} OFFSET ${len(params)+2}"
        return await con.fetch(query, *params_full)


async def count_orders(status: str | None = None, platform: str | None = None,
                        service_key: str | None = None) -> int:
    where, params = _orders_where(status, platform, service_key)
    async with pool.acquire() as con:
        query = f"SELECT COUNT(*) FROM orders {where}"
        return await con.fetchval(query, *params)


# ---------------------------------------------------------------- пополнения
async def create_topup(user_id: int, method: str, file_id: str) -> int:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "INSERT INTO topups (user_id, method, file_id) VALUES ($1,$2,$3) RETURNING id",
            user_id, method, file_id,
        )
        return row["id"]


async def get_topup(topup_id: int):
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM topups WHERE id=$1", topup_id)


async def set_topup_status(topup_id: int, status: str, amount: int | None = None) -> bool:
    """Обновляет статус заявки, только если она ещё 'В ожидании' (защита от повторной обработки).
    Возвращает True, если обновление прошло."""
    async with pool.acquire() as con:
        if amount is not None:
            row = await con.fetchrow(
                "UPDATE topups SET status=$2, amount=$3 "
                "WHERE id=$1 AND status='В ожидании' RETURNING id",
                topup_id, status, amount,
            )
        else:
            row = await con.fetchrow(
                "UPDATE topups SET status=$2 "
                "WHERE id=$1 AND status='В ожидании' RETURNING id",
                topup_id, status,
            )
        return row is not None


async def approve_topup(topup_id: int, amount: int, admin_id: int) -> int | None:
    """Принятие чека: смена статуса и зачисление денег в одной транзакции.
    Возвращает новый баланс или None, если заявка уже обработана."""
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "UPDATE topups SET status='Принято', amount=$2 "
                "WHERE id=$1 AND status='В ожидании' RETURNING user_id",
                topup_id, amount,
            )
            if row is None:
                return None
            return await _apply_balance(
                con, row["user_id"], amount, admin_id, f"Пополнение по чеку №{topup_id}",
                kind="topup", create_if_missing=True,
            )


async def get_pending_topups(limit: int = 10):
    async with pool.acquire() as con:
        return await con.fetch(
            "SELECT * FROM topups WHERE status='В ожидании' ORDER BY id ASC LIMIT $1", limit
        )


async def count_pending_topups() -> int:
    async with pool.acquire() as con:
        return await con.fetchval("SELECT COUNT(*) FROM topups WHERE status='В ожидании'")


async def count_user_pending_topups(user_id: int) -> int:
    async with pool.acquire() as con:
        return await con.fetchval(
            "SELECT COUNT(*) FROM topups WHERE user_id=$1 AND status='В ожидании'", user_id
        )


# ---------------------------------------------------------------- рассылка
async def get_all_user_ids():
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]


# ---------------------------------------------------------------- статистика
async def stats_users():
    async with pool.acquire() as con:
        total = await con.fetchval("SELECT COUNT(*) FROM users")
        today = await con.fetchval(
            f"SELECT COUNT(*) FROM users WHERE created_at >= {UTC_NOW} - INTERVAL '1 day'"
        )
        banned = await con.fetchval("SELECT COUNT(*) FROM users WHERE is_banned=TRUE")
        return {"total": total, "today": today, "banned": banned}


async def stats_orders():
    async with pool.acquire() as con:
        total = await con.fetchval("SELECT COUNT(*) FROM orders")
        pending = await con.fetchval("SELECT COUNT(*) FROM orders WHERE status='В ожидании'")
        done = await con.fetchval("SELECT COUNT(*) FROM orders WHERE status='Сделано'")
        cancelled = await con.fetchval("SELECT COUNT(*) FROM orders WHERE status='Отменено'")
        return {"total": total, "pending": pending, "done": done, "cancelled": cancelled}


async def stats_turnover():
    async with pool.acquire() as con:
        spent = await con.fetchval(
            "SELECT COALESCE(SUM(price),0)::BIGINT FROM orders WHERE status != 'Отменено'"
        )
        # только настоящие пополнения: возвраты и ручные правки сюда больше не попадают
        topped_up = await con.fetchval(
            "SELECT COALESCE(SUM(amount),0)::BIGINT FROM balance_logs WHERE kind='topup' AND amount > 0"
        )
        refunded = await con.fetchval(
            "SELECT COALESCE(SUM(amount),0)::BIGINT FROM balance_logs WHERE kind='refund' AND amount > 0"
        )
        return {"spent": spent, "topped_up": topped_up, "refunded": refunded}


async def stats_top_services(limit: int = 5):
    async with pool.acquire() as con:
        return await con.fetch(
            "SELECT service_key, COUNT(*) as cnt FROM orders GROUP BY service_key "
            "ORDER BY cnt DESC LIMIT $1", limit
        )


# ---------------------------------------------------------------- состояния диалогов (FSM) и временные данные
# Хранятся в Postgres, поэтому не теряются при перезапуске/редеплое бота.
async def fsm_get(key: str):
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT state, data FROM fsm_states WHERE key=$1", key)


async def fsm_set_state(key: str, state: str | None):
    async with pool.acquire() as con:
        await con.execute(
            f"INSERT INTO fsm_states (key, state, updated_at) VALUES ($1, $2, {UTC_NOW}) "
            f"ON CONFLICT (key) DO UPDATE SET state=EXCLUDED.state, updated_at={UTC_NOW}",
            key, state,
        )
        # пустая запись (нет состояния и данных) не нужна
        await con.execute("DELETE FROM fsm_states WHERE key=$1 AND state IS NULL AND data='{}'", key)


async def fsm_set_data(key: str, data_json: str):
    async with pool.acquire() as con:
        await con.execute(
            f"INSERT INTO fsm_states (key, data, updated_at) VALUES ($1, $2, {UTC_NOW}) "
            f"ON CONFLICT (key) DO UPDATE SET data=EXCLUDED.data, updated_at={UTC_NOW}",
            key, data_json,
        )
        await con.execute("DELETE FROM fsm_states WHERE key=$1 AND state IS NULL AND data='{}'", key)


async def fsm_delete(key: str):
    async with pool.acquire() as con:
        await con.execute("DELETE FROM fsm_states WHERE key=$1", key)
