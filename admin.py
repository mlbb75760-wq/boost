import asyncio
import html
import json
import re

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
import database as db
from config import ADMIN_CHAT_ID, ORDERS_CHAT_ID, MIN_TOPUP_AMOUNT, ADMIN_USER_IDS
from keyboards import (
    admin_order_kb, admin_panel_kb,
    admin_cancel_confirm_kb,
    broadcast_target_kb, broadcast_confirm_kb,
    admin_pending_topups_kb, admin_topup_confirm_kb,
    admin_order_platforms_kb, admin_order_services_kb, admin_order_list_kb, admin_order_detail_kb,
)
from services import PLATFORM_NAMES, SERVICE_LABELS
from states import BroadcastStates, AdminTopupStates, AdminOrderStates
from utils import parse_uint, parse_int, fmt_local

router = Router()

FINAL = db.FINAL_STATUSES

# ORDERS_CHAT_ID — обычно КАНАЛ. Тексты, написанные в канал, приходят боту как channel_post,
# а у channel_post нет отправителя (user_id=None), поэтому aiogram не может подключить к нему
# FSM-состояние (проверено: resolve_context возвращает None) — прежние обработчики
# @router.channel_post(..., AdminOrderStates...) никогда не срабатывали.
# Для канала ожидаемая причина отмены хранится отдельно (в БД, переживает рестарт): ключ по chat_id.
def _cc_key(chat_id: int) -> str:
    return f"chcancel:{chat_id}"


# ----------------------------------------------------------------- авторизация
def _user_ok(user) -> bool:
    """Если задан ADMIN_USER_IDS — управлять могут только перечисленные пользователи."""
    return not ADMIN_USER_IDS or (user is not None and user.id in ADMIN_USER_IDS)


def _is_admin_chat(chat_id: int, user=None) -> bool:
    return bool(ADMIN_CHAT_ID) and chat_id == ADMIN_CHAT_ID and _user_ok(user)


def _is_order_action_chat(chat_id: int, user=None) -> bool:
    """Кнопки статуса заказа (⏳/✅/🚫) показываются в ORDERS_CHAT_ID (канал заявок),
    а не только в ADMIN_CHAT_ID — управлять заказом можно из обоих чатов."""
    if _is_admin_chat(chat_id, user):
        return True
    return bool(ORDERS_CHAT_ID) and chat_id == ORDERS_CHAT_ID and _user_ok(user)


def _cb_admin(callback: CallbackQuery) -> bool:
    return _is_admin_chat(callback.message.chat.id, callback.from_user)


# ================================================================= команды
@router.message(Command("panel"))
@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not _is_admin_chat(message.chat.id, message.from_user):
        return
    await message.answer("🛠 Админ-панель:", reply_markup=admin_panel_kb())


@router.message(Command("topup"))
async def admin_topup(message: Message, bot: Bot):
    if not _is_admin_chat(message.chat.id, message.from_user):
        return
    parts = message.text.split()
    user_id = parse_uint(parts[1]) if len(parts) == 3 else None
    amount = parse_int(parts[2]) if len(parts) == 3 else None
    if user_id is None or not amount:
        await message.answer("Использование: /topup user_id сумма  (сумма может быть отрицательной для списания)")
        return
    try:
        new_balance = await db.change_balance(
            user_id, amount, admin_id=message.from_user.id,
            reason="Пополнение по чеку" if amount > 0 else "Ручная корректировка",
            kind="topup" if amount > 0 else "adjust",
        )
    except db.UserNotFound:
        # раньше здесь молча создавался пользователь-«призрак» с балансом
        await message.answer(f"❌ Пользователь {user_id} не найден (он не запускал бота). Проверьте ID.")
        return
    except db.InsufficientFunds:
        await message.answer("❌ На балансе пользователя недостаточно средств для такого списания.")
        return
    await message.answer(f"✅ Изменение баланса на {amount} so'm для пользователя {user_id}. Баланс: {new_balance} so'm")
    if amount > 0:
        try:
            await bot.send_message(
                user_id,
                f"✅ Ваш баланс пополнен на {amount} so'm.\nТекущий баланс: {new_balance} so'm",
            )
        except Exception:
            pass


@router.message(Command("reply"))
async def admin_reply(message: Message, bot: Bot):
    if not _is_admin_chat(message.chat.id, message.from_user):
        return
    parts = message.text.split(maxsplit=2)
    user_id = parse_uint(parts[1]) if len(parts) == 3 else None
    if user_id is None:
        await message.answer("Использование: /reply user_id текст")
        return
    try:
        # parse_mode=None: ответ уходит как обычный текст, «<» и «&» не ломают отправку
        await bot.send_message(user_id, f"💬 Ответ от поддержки:\n\n{parts[2]}", parse_mode=None)
        await message.answer("✅ Отправлено.")
    except Exception:
        await message.answer("❌ Не удалось отправить сообщение (пользователь мог заблокировать бота).")


# ================================================================= пополнение по чеку: принять / отклонить
@router.callback_query(F.data.startswith("admtopup:"))
async def admin_topup_action(callback: CallbackQuery, state: FSMContext):
    if not _cb_admin(callback):
        return
    _, action, topup_id_s = callback.data.split(":")
    topup_id = int(topup_id_s)

    topup = await db.get_topup(topup_id)
    if not topup:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if topup["status"] != "В ожидании":
        await callback.answer(f"Заявка уже обработана: {topup['status']}", show_alert=True)
        return

    # ✅/❌ можно нажать под самим фото-чеком (callback.message — фото с подписью) или в
    # разделе "💳 Пополнения" (обычный текстовый список). Подпись редактируем только у фото.
    has_receipt_photo = bool(callback.message.photo)
    await state.update_data(
        topup_id=topup_id,
        msg_chat_id=callback.message.chat.id if has_receipt_photo else None,
        msg_id=callback.message.message_id if has_receipt_photo else None,
    )

    if action == "approve":
        await state.set_state(AdminTopupStates.waiting_amount)
        await callback.answer()
        await callback.message.reply(
            f"Введите сумму зачисления для заявки №{topup_id} (пользователь {topup['user_id']}).\n"
            f"Для отмены — /cancel"
        )
    else:
        await state.set_state(AdminTopupStates.waiting_reason)
        await callback.answer()
        await callback.message.reply(
            f"Введите причину отклонения заявки №{topup_id} (пользователь {topup['user_id']}).\n"
            f"Для отмены — /cancel"
        )


@router.message(Command("cancel"), AdminTopupStates.waiting_amount)
@router.message(Command("cancel"), AdminTopupStates.confirm_amount)
@router.message(Command("cancel"), AdminTopupStates.waiting_reason)
@router.message(Command("cancel"), AdminTopupStates.confirm_reason)
@router.message(Command("cancel"), AdminOrderStates.waiting_cancel_reason)
@router.message(Command("cancel"), BroadcastStates.waiting_user_id)
@router.message(Command("cancel"), BroadcastStates.waiting_text)
async def admin_flow_cancel(message: Message, state: FSMContext):
    if not _is_order_action_chat(message.chat.id, message.from_user):
        return
    await state.clear()
    await message.answer("Отменено.")


@router.message(AdminTopupStates.waiting_amount)
async def admin_topup_amount(message: Message, state: FSMContext):
    if not _is_admin_chat(message.chat.id, message.from_user):
        return
    amount = parse_uint((message.text or "").strip())
    if amount is None:
        await message.answer("❗ Введите сумму числом (только цифры, без пробелов и ID), либо /cancel для отмены.")
        return
    if amount < MIN_TOPUP_AMOUNT:
        await message.answer(f"❗ Сумма меньше минимальной ({MIN_TOPUP_AMOUNT} so'm). Введите другую сумму.")
        return

    data = await state.get_data()
    topup_id = data["topup_id"]
    topup = await db.get_topup(topup_id)
    if not topup or topup["status"] != "В ожидании":
        await message.answer("Эта заявка уже обработана.")
        await state.clear()
        return

    await state.update_data(kind="credit", amount=amount)
    await state.set_state(AdminTopupStates.confirm_amount)
    await message.answer(
        f"Подтвердите:\nНачислить <b>{amount} so'm</b> пользователю {topup['user_id']} "
        f"(заявка №{topup_id})?",
        reply_markup=admin_topup_confirm_kb(),
    )


@router.message(AdminTopupStates.waiting_reason)
async def admin_topup_reason(message: Message, state: FSMContext):
    if not _is_admin_chat(message.chat.id, message.from_user):
        return
    if not message.text:
        await message.answer("❗ Введите текст причины (или /cancel для отмены).")
        return
    reason = message.text.strip()

    data = await state.get_data()
    topup_id = data["topup_id"]
    topup = await db.get_topup(topup_id)
    if not topup or topup["status"] != "В ожидании":
        await message.answer("Эта заявка уже обработана.")
        await state.clear()
        return

    await state.update_data(kind="reject", reason=reason)
    await state.set_state(AdminTopupStates.confirm_reason)
    await message.answer(
        f"Подтвердите:\nОтклонить заявку №{topup_id} (пользователь {topup['user_id']}) с причиной:\n"
        f"«{html.escape(reason)}»?",
        reply_markup=admin_topup_confirm_kb(),
    )


@router.callback_query(F.data.startswith("admtopupconfirm:"))
async def admin_topup_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not _cb_admin(callback):
        return
    action = callback.data.split(":")[1]
    data = await state.get_data()
    kind = data.get("kind")

    if action == "abort":
        await state.clear()
        await callback.answer()
        await callback.message.edit_text("🚫 Отменено.")
        return

    if action == "retry":
        await callback.answer()
        if kind == "credit":
            await state.set_state(AdminTopupStates.waiting_amount)
            await callback.message.edit_text(
                f"Введите сумму зачисления для заявки №{data.get('topup_id')} заново.\nДля отмены — /cancel"
            )
        else:
            await state.set_state(AdminTopupStates.waiting_reason)
            await callback.message.edit_text(
                f"Введите причину отклонения для заявки №{data.get('topup_id')} заново.\nДля отмены — /cancel"
            )
        return

    # action == "go"
    topup_id = data.get("topup_id")
    if topup_id is None or kind not in ("credit", "reject"):
        await state.clear()
        await callback.answer("Сессия устарела. Откройте заявку заново.", show_alert=True)
        return
    topup = await db.get_topup(topup_id)
    if not topup or topup["status"] != "В ожидании":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        await state.clear()
        return

    if kind == "credit":
        amount = data["amount"]
        # смена статуса и зачисление — в одной транзакции
        new_balance = await db.approve_topup(topup_id, amount, callback.from_user.id)
        if new_balance is None:
            await callback.answer("Заявка уже была обработана другим способом.", show_alert=True)
            await state.clear()
            return

        await callback.answer("Зачислено ✅")
        await callback.message.edit_text(
            f"✅ Заявка №{topup_id} принята.\nНачислено {amount} so'm пользователю {topup['user_id']}.\n"
            f"Баланс: {new_balance} so'm"
        )

        if data.get("msg_id"):
            try:
                await bot.edit_message_caption(
                    chat_id=data["msg_chat_id"], message_id=data["msg_id"],
                    caption=f"💰 Заявка на пополнение №{topup_id}\n\n✅ Принято, начислено {amount} so'm",
                )
            except Exception:
                pass

        try:
            await bot.send_message(
                topup["user_id"],
                f"✅ Ваш счёт пополнен на {amount} so'm.\nТекущий баланс: {new_balance} so'm",
            )
        except Exception:
            await callback.message.answer(
                "⚠️ Деньги начислены, но сообщение клиенту не доставлено "
                "(возможно, он заблокировал бота)."
            )

    else:  # reject
        reason = data["reason"]
        ok = await db.set_topup_status(topup_id, "Отклонено")
        if not ok:
            await callback.answer("Заявка уже была обработана другим способом.", show_alert=True)
            await state.clear()
            return

        await callback.answer("Отклонено ❌")
        await callback.message.edit_text(
            f"❌ Заявка №{topup_id} отклонена.\nПричина: {html.escape(reason)}"
        )

        if data.get("msg_id"):
            try:
                await bot.edit_message_caption(
                    chat_id=data["msg_chat_id"], message_id=data["msg_id"],
                    caption=f"💰 Заявка на пополнение №{topup_id}\n\n❌ Отклонено\nПричина: {reason}",
                    parse_mode=None,
                )
            except Exception:
                pass

        try:
            await bot.send_message(
                topup["user_id"],
                f"❌ Ваш чек на пополнение отклонён.\nПричина: {html.escape(reason)}\n\n"
                f"Если считаете это ошибкой — напишите в поддержку.",
            )
        except Exception:
            await callback.message.answer(
                "⚠️ Заявка отклонена, но сообщение клиенту не доставлено "
                "(возможно, он заблокировал бота)."
            )

    await state.clear()


@router.callback_query(F.data == "admtopups:list")
async def admin_topups_list(callback: CallbackQuery):
    if not _cb_admin(callback):
        return
    topups = await db.get_pending_topups(limit=15)
    total = await db.count_pending_topups()
    if not topups:
        await callback.message.edit_text(
            "Заявок на пополнение в ожидании нет.", reply_markup=admin_panel_kb()
        )
        return
    text = (
        f"💳 <b>Заявки на пополнение</b> (в ожидании: {total})\n\n"
        f"Чек для каждой заявки был отправлен отдельным сообщением выше в этом чате — "
        f"пролистайте историю, чтобы свериться с фото.\n\n"
        f"Нажмите ✅, чтобы принять (потребуется ввести сумму), или ❌, чтобы отклонить (потребуется причина)."
    )
    await callback.message.edit_text(text, reply_markup=admin_pending_topups_kb(topups))


# ================================================================= статус заказа / отмена с возвратом
def _back_rows(message: Message) -> list:
    """Кнопка «Назад» (к списку заказов) из текущей клавиатуры сообщения — чтобы она не
    пропадала после смены статуса/подтверждения отмены."""
    markup = message.reply_markup
    if not markup:
        return []
    return [
        [b] for row in markup.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("admordlist:")
    ]


def _compose_kb(base: InlineKeyboardMarkup | None, message: Message) -> InlineKeyboardMarkup | None:
    rows = list(base.inline_keyboard) if base else []
    rows += _back_rows(message)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _refresh_order_message(message: Message, order_id: int, status: str):
    kb = _compose_kb(admin_order_kb(order_id, status), message)
    try:
        if message.text and "\nСтатус: " in message.text:
            # карточка заказа из админ-панели: обновляем и строку «Статус»
            new_text = re.sub(r"(?m)^Статус: .*$", lambda m: f"Статус: {status}", message.html_text)
            await message.edit_text(new_text, reply_markup=kb)
        else:
            await message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


async def _complete_cancel(bot: Bot, reply_to: Message, payload: dict, reason: str, admin_id: int | None):
    """Отмена заказа с возвратом денег (атомарно в БД) и уведомления."""
    order_id = payload["order_id"]
    order = await db.cancel_order_with_refund(order_id, admin_id, reason)
    if order is None:
        current = await db.get_order(order_id)
        if current:
            await reply_to.answer(f"Заказ уже в статусе «{current['status']}», отмена недоступна.")
        else:
            await reply_to.answer("Заказ не найден.")
        return

    await reply_to.answer(f"🚫 Заказ №{order_id} отменён. Причина: {html.escape(reason)}")

    try:
        await bot.edit_message_reply_markup(
            chat_id=payload["msg_chat_id"], message_id=payload["msg_id"], reply_markup=None,
        )
    except Exception:
        pass

    try:
        text = f"ℹ️ Статус вашей заявки №{order_id} изменён: Отменено\nПричина: {html.escape(reason)}"
        if order["price"] > 0:
            text += f"\n💰 Средства ({order['price']} so'm) возвращены на баланс."
        await bot.send_message(order["user_id"], text)
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm:"))
async def admin_order_status(callback: CallbackQuery, state: FSMContext, bot: Bot):
    msg = callback.message
    if not _is_order_action_chat(msg.chat.id, callback.from_user):
        return
    parts = callback.data.split(":")
    action, target_id = parts[1], int(parts[2])

    if action == "unban":
        await db.unban_user(target_id)  # тут target_id — это user_id (см. admin_ban_notice_kb)
        await callback.answer("Пользователь разбанен.")
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            await bot.send_message(target_id, "✅ Вы разблокированы администратором. Можете пользоваться ботом.")
        except Exception:
            pass
        return

    order_id = target_id
    order = await db.get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    # -------- отмена: требует подтверждения и причины, деньги возвращаются только после этого
    if action == "cancel":
        if order["status"] in FINAL:
            await callback.answer(f"Заказ уже в статусе «{order['status']}», отмена недоступна.", show_alert=True)
            return
        await callback.answer()
        try:
            await msg.edit_reply_markup(reply_markup=_compose_kb(admin_cancel_confirm_kb(order_id), msg))
        except Exception:
            pass
        return

    if action == "cancelabort":
        await callback.answer("Отмена заказа прервана.")
        try:
            await msg.edit_reply_markup(reply_markup=_compose_kb(admin_order_kb(order_id, order["status"]), msg))
        except Exception:
            pass
        return

    if action == "cancelconfirm":
        if order["status"] in FINAL:
            await callback.answer(f"Заказ уже в статусе «{order['status']}», отмена недоступна.", show_alert=True)
            try:
                await msg.edit_reply_markup(reply_markup=_compose_kb(admin_order_kb(order_id, order["status"]), msg))
            except Exception:
                pass
            return
        payload = dict(order_id=order_id, msg_chat_id=msg.chat.id, msg_id=msg.message_id)
        if msg.chat.type == "channel":
            await db.fsm_set_data(_cc_key(msg.chat.id), json.dumps(payload))
        else:
            await state.set_state(AdminOrderStates.waiting_cancel_reason)
            await state.update_data(**payload)
        await callback.answer()
        await msg.reply(
            f"Напишите причину отмены заказа №{order_id} (пользователь получит её в сообщении).\n"
            f"Для отмены действия — /cancel"
        )
        return

    # -------- статусы "В ожидании" / "Сделано"
    status = "В ожидании" if action == "pending" else "Сделано"
    if order["status"] in FINAL:
        await callback.answer(f"Заказ уже в статусе «{order['status']}», изменить нельзя.", show_alert=True)
        await _refresh_order_message(msg, order_id, order["status"])
        return
    if order["status"] == status:
        await callback.answer("Заказ уже в этом статусе.")
        return
    # условное обновление: устаревшая кнопка не перезапишет финальный статус
    if not await db.set_order_status(order_id, status):
        fresh = await db.get_order(order_id)
        await callback.answer(
            f"Заказ уже в статусе «{fresh['status'] if fresh else '—'}», изменить нельзя.", show_alert=True
        )
        return

    await callback.answer(f"Статус заявки №{order_id}: {status}")
    await _refresh_order_message(msg, order_id, status)
    try:
        await bot.send_message(order["user_id"], f"ℹ️ Статус вашей заявки №{order_id} изменён: {status}")
    except Exception:
        pass


@router.message(AdminOrderStates.waiting_cancel_reason)
async def admin_order_cancel_reason(message: Message, state: FSMContext, bot: Bot):
    if not _is_order_action_chat(message.chat.id, message.from_user):
        return
    if not message.text:
        await message.answer("❗ Введите текст причины отмены (или /cancel для отмены действия).")
        return
    data = await state.get_data()
    await state.clear()   # сначала сбрасываем состояние — повторное сообщение не отменит заказ второй раз
    if "order_id" not in data:
        await message.answer("Сессия устарела. Нажмите «Отменить» у заказа заново.")
        return
    admin_id = message.from_user.id if message.from_user else None
    await _complete_cancel(bot, message, data, message.text.strip(), admin_id)


@router.channel_post()
async def channel_cancel_input(message: Message, bot: Bot):
    """Причина отмены заказа, написанная прямо в канал заявок (см. комментарий вверху файла)."""
    if message.chat.id != ORDERS_CHAT_ID:
        return
    row = await db.fsm_get(_cc_key(message.chat.id))
    if not row:
        return
    pending = json.loads(row["data"])
    text = (message.text or "").strip()
    if not text:
        await message.answer("❗ Введите текст причины отмены (или /cancel для отмены действия).")
        return
    await db.fsm_delete(_cc_key(message.chat.id))
    if text.split()[0].split("@")[0].lower() == "/cancel":
        await message.answer("Отменено.")
        return
    await _complete_cancel(bot, message, pending, text, None)


# ================================================================= статистика
@router.callback_query(F.data == "admpanel:back")
async def admpanel_back(callback: CallbackQuery, state: FSMContext):
    if not _cb_admin(callback):
        return
    await state.clear()
    await callback.message.edit_text("🛠 Админ-панель:", reply_markup=admin_panel_kb())


@router.callback_query(F.data.startswith("admstat:"))
async def admin_stats(callback: CallbackQuery):
    if not _cb_admin(callback):
        return
    kind = callback.data.split(":")[1]

    if kind == "users":
        s = await db.stats_users()
        text = (
            f"📊 <b>Пользователи</b>\n\n"
            f"Всего: {s['total']}\n"
            f"Новых за сутки: {s['today']}\n"
            f"Заблокировано: {s['banned']}"
        )
    elif kind == "turnover":
        s = await db.stats_turnover()
        text = (
            f"📊 <b>Обороты</b>\n\n"
            f"Списано за заказы: {s['spent']} so'm\n"
            f"Начислено пополнений: {s['topped_up']} so'm\n"
            f"Возвращено за отмены: {s['refunded']} so'm"
        )
    else:  # top
        rows = await db.stats_top_services()
        if not rows:
            text = "📊 <b>Топ услуг</b>\n\nПока нет заказов."
        else:
            lines = ["📊 <b>Топ услуг</b>\n"]
            for r in rows:
                label = SERVICE_LABELS.get(r["service_key"], r["service_key"])
                lines.append(f"{label} — {r['cnt']} заказ(ов)")
            text = "\n".join(lines)

    await callback.message.edit_text(text, reply_markup=admin_panel_kb())


# ================================================================= заказы: платформа → услуга → список
@router.callback_query(F.data == "admord:platforms")
async def admord_platforms(callback: CallbackQuery):
    if not _cb_admin(callback):
        return
    s = await db.stats_orders()
    text = (
        f"📦 <b>Заказы</b>\n\n"
        f"Всего: {s['total']} · В ожидании: {s['pending']} · "
        f"Сделано: {s['done']} · Отменено: {s['cancelled']}\n\n"
        f"Выберите платформу:"
    )
    await callback.message.edit_text(text, reply_markup=admin_order_platforms_kb())


@router.callback_query(F.data.startswith("admordplat:"))
async def admordplat(callback: CallbackQuery):
    if not _cb_admin(callback):
        return
    platform = callback.data.split(":")[1]
    plat_name = PLATFORM_NAMES.get(platform, platform)
    await callback.message.edit_text(
        f"{plat_name} — выберите услугу:", reply_markup=admin_order_services_kb(platform)
    )


async def _render_order_list(callback: CallbackQuery, platform: str, service_key: str, offset: int, status: str):
    db_status = None if status == "all" else "В ожидании"
    db_service = None if service_key == "all" else service_key

    orders = await db.get_orders_page(
        offset=offset, limit=10, status=db_status, platform=platform, service_key=db_service
    )
    total = await db.count_orders(status=db_status, platform=platform, service_key=db_service)

    plat_name = PLATFORM_NAMES.get(platform, platform)
    svc_name = "Все услуги" if service_key == "all" else SERVICE_LABELS.get(service_key, service_key)

    if not orders:
        text = f"🧾 <b>{plat_name} — {svc_name}</b>\n\nЗаказов не найдено."
    else:
        text = f"🧾 <b>{plat_name} — {svc_name}</b> (всего: {total})\nВыберите заказ:"
    order_ids = [o["id"] for o in orders]
    await callback.message.edit_text(
        text, reply_markup=admin_order_list_kb(platform, service_key, offset, status, order_ids)
    )


@router.callback_query(F.data.startswith("admordsvc:"))
async def admordsvc(callback: CallbackQuery):
    if not _cb_admin(callback):
        return
    _, platform, service_key = callback.data.split(":")
    await _render_order_list(callback, platform, service_key, 0, "all")


@router.callback_query(F.data.startswith("admordlist:"))
async def admordlist(callback: CallbackQuery):
    if not _cb_admin(callback):
        return
    _, platform, service_key, offset, status = callback.data.split(":")
    await _render_order_list(callback, platform, service_key, int(offset), status)


@router.callback_query(F.data.startswith("admorditem:"))
async def admorditem(callback: CallbackQuery):
    if not _cb_admin(callback):
        return
    _, order_id, platform, service_key, offset, status = callback.data.split(":")
    order_id, offset = int(order_id), int(offset)
    order = await db.get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    platform_name = PLATFORM_NAMES.get(order["platform"], order["platform"])
    service_label = SERVICE_LABELS.get(order["service_key"], order["service_key"])
    price_text = "бесплатно" if order["price"] == 0 else f"{order['price']} so'm"

    text = (
        f"<b>Заявка №{order['id']}</b>\n\n"
        f"Пользователь: {order['user_id']}\n"
        f"Платформа: {platform_name}\n"
        f"Услуга: {service_label} ({order['variant']})\n"
        f"Количество: {order['amount']}\n"
        f"Ссылка: {html.escape(order['link'])}\n"
        f"Стоимость: {price_text}\n"
        f"Статус: {order['status']}\n"
        f"Создан: {fmt_local(order['created_at'])}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=admin_order_detail_kb(
            order_id, platform, service_key, offset, status, order_status=order["status"]
        ),
    )


# ================================================================= рассылка
@router.callback_query(F.data == "broadcast:start")
async def broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not _cb_admin(callback):
        return
    await state.set_state(BroadcastStates.waiting_target)
    await callback.message.edit_text(
        "📢 Кому отправить рассылку?", reply_markup=broadcast_target_kb()
    )


@router.callback_query(BroadcastStates.waiting_target, F.data.startswith("bctarget:"))
async def broadcast_target(callback: CallbackQuery, state: FSMContext):
    if not _cb_admin(callback):
        return
    target = callback.data.split(":")[1]
    await state.update_data(target=target)
    if target == "one":
        await state.set_state(BroadcastStates.waiting_user_id)
        await callback.message.edit_text("Введите ID пользователя (или /cancel):")
    else:
        await state.set_state(BroadcastStates.waiting_text)
        await callback.message.edit_text("Введите текст рассылки для всех пользователей (или /cancel):")


@router.message(BroadcastStates.waiting_user_id)
async def broadcast_user_id(message: Message, state: FSMContext):
    if not _is_admin_chat(message.chat.id, message.from_user):
        return
    user_id = parse_uint((message.text or "").strip())
    if user_id is None:
        await message.answer("❗ Введите числовой ID пользователя.")
        return
    await state.update_data(user_id=user_id)
    await state.set_state(BroadcastStates.waiting_text)
    await message.answer("Введите текст сообщения:")


@router.message(BroadcastStates.waiting_text)
async def broadcast_text(message: Message, state: FSMContext):
    if not _is_admin_chat(message.chat.id, message.from_user):
        return
    if not message.text:
        await message.answer("❗ Отправьте текстовое сообщение (или /cancel).")
        return
    # html_text сохраняет форматирование, сделанное в Telegram, и экранирует «<», «&»
    text = message.html_text
    if len(text) > 4000:
        await message.answer("❗ Текст слишком длинный (максимум около 4000 символов). Сократите его.")
        return
    await state.update_data(text=text)
    await state.set_state(BroadcastStates.confirm)
    data = await state.get_data()
    target_desc = f"пользователю {data['user_id']}" if data["target"] == "one" else "всем пользователям"
    await message.answer(
        f"Проверьте текст рассылки ({target_desc}):\n\n{text}",
        reply_markup=broadcast_confirm_kb(),
    )


async def _send_with_retry(bot: Bot, user_id: int, text: str) -> bool:
    for _ in range(3):
        try:
            await bot.send_message(user_id, text)
            return True
        except TelegramRetryAfter as e:   # флуд-лимит Telegram — ждём и повторяем, а не считаем ошибкой
            await asyncio.sleep(e.retry_after + 1)
        except Exception:
            return False
    return False


@router.callback_query(BroadcastStates.confirm, F.data == "bcsend")
async def broadcast_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not _cb_admin(callback):
        return
    data = await state.get_data()
    # состояние сбрасываем СРАЗУ: повторное нажатие «Отправить» уже не запустит рассылку второй раз
    await state.clear()
    await callback.answer()
    text = data.get("text")
    if not text:
        await callback.message.edit_text("Сессия устарела. Начните рассылку заново.")
        return

    if data.get("target") == "one":
        if await _send_with_retry(bot, data["user_id"], text):
            await callback.message.edit_text("✅ Сообщение отправлено.")
        else:
            await callback.message.edit_text("❌ Не удалось отправить (пользователь заблокировал бота или ID неверный).")
        return

    user_ids = await db.get_all_user_ids()
    await callback.message.edit_text(f"⏳ Рассылка запущена ({len(user_ids)} получателей)…")
    sent, failed = 0, 0
    for uid in user_ids:
        if await _send_with_retry(bot, uid, text):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)   # ~20 сообщений/сек — ниже лимита Telegram (30/сек)
    try:
        await callback.message.edit_text(f"✅ Рассылка завершена.\nДоставлено: {sent}\nНе доставлено: {failed}")
    except Exception:
        await callback.message.answer(f"✅ Рассылка завершена.\nДоставлено: {sent}\nНе доставлено: {failed}")
