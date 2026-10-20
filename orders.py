import html
import logging
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
import database as db
from config import ADMIN_CHAT_ID, ORDERS_CHAT_ID
from keyboards import (
    BTN_ORDER, BTN_BACK, BTN_FREE_PROMO,
    BTN_NUMBER, BTN_MY_ORDERS, BTN_TOPUP, BTN_BALANCE, BTN_HELP,
    main_menu_kb, back_only_kb, platforms_kb, services_kb, variants_kb,
    order_action_kb, confirm_order_kb, admin_order_kb, admin_ban_notice_kb,
    free_promo_platforms_kb, free_promo_services_kb,
)
from states import OrderStates
from services import (
    PLATFORM_NAMES, get_variant_cfg, build_info_text, calc_price, validate_link, LINK_HINTS,
)
from utils import user_ref, parse_uint, safe_delete, fmt_local, BANNED_TEXT
from handlers.menu import take_number, my_orders, my_balance, help_start
from handlers.balance import topup_start

router = Router()

# защита от повторного нажатия "Отправить" (двойной тап)
_processing_confirm: set[int] = set()


async def _reject_if_banned(target, user_id: int) -> bool:
    if await db.is_banned(user_id):
        if isinstance(target, CallbackQuery):
            await target.answer(BANNED_TEXT, show_alert=True)
        else:
            await target.answer(BANNED_TEXT)
        return True
    return False


# ----------------------------------------------------------------- вход в раздел заказа
@router.message(F.text == BTN_ORDER)
async def order_start(message: Message, state: FSMContext):
    if await _reject_if_banned(message, message.from_user.id):
        return
    # сбрасываем незавершённое оформление
    await state.clear()
    await message.answer("Выберите платформу:", reply_markup=platforms_kb())


@router.callback_query(F.data == "nav:menu")
async def nav_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_delete(callback.message)
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())


# ----------------------------------------------------------------- бесплатное продвижение
@router.message(F.text == BTN_FREE_PROMO)
async def free_promo_start(message: Message, state: FSMContext):
    if await _reject_if_banned(message, message.from_user.id):
        return
    await state.clear()
    await message.answer("🆓 Бесплатное продвижение — выберите платформу:", reply_markup=free_promo_platforms_kb())


@router.callback_query(F.data == "nav:freepromo")
async def nav_free_promo(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🆓 Бесплатное продвижение — выберите платформу:", reply_markup=free_promo_platforms_kb()
    )


@router.callback_query(F.data.startswith("freeplat:"))
async def choose_free_platform(callback: CallbackQuery):
    platform = callback.data.split(":")[1]
    await callback.message.edit_text(
        f"{PLATFORM_NAMES[platform]} — бесплатно, выберите:", reply_markup=free_promo_services_kb(platform)
    )


@router.callback_query(F.data == "nav:order")
async def nav_order(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Выберите платформу:", reply_markup=platforms_kb())


@router.callback_query(F.data.startswith("nav:platform:"))
async def nav_platform(callback: CallbackQuery, state: FSMContext):
    platform = callback.data.split(":")[2]
    await state.clear()
    await callback.message.edit_text(
        f"{PLATFORM_NAMES[platform]} — выберите услугу:", reply_markup=services_kb(platform)
    )


@router.callback_query(F.data.startswith("nav:service:"))
async def nav_service(callback: CallbackQuery, state: FSMContext):
    service_key = callback.data.split(":")[2]
    await state.clear()
    await callback.message.edit_text("Выберите вариант:", reply_markup=variants_kb(service_key))


# ----------------------------------------------------------------- платформа -> услуги
@router.callback_query(F.data.startswith("plat:"))
async def choose_platform(callback: CallbackQuery):
    platform = callback.data.split(":")[1]
    await callback.message.edit_text(
        f"{PLATFORM_NAMES[platform]} — выберите услугу:", reply_markup=services_kb(platform)
    )


# ----------------------------------------------------------------- услуга -> варианты
@router.callback_query(F.data.startswith("srv:"))
async def choose_service(callback: CallbackQuery):
    service_key = callback.data.split(":")[1]
    await callback.message.edit_text("Выберите вариант:", reply_markup=variants_kb(service_key))


def _cooldown_text(next_dt) -> str:
    return (
        "Бесплатное продвижение уже использовано.\n"
        f"Следующий раз доступно: {fmt_local(next_dt)}"
    )


# ----------------------------------------------------------------- вариант -> инфо + "дать заявку"
@router.callback_query(F.data.startswith("var:"))
async def choose_variant(callback: CallbackQuery):
    _, service_key, variant = callback.data.split(":")
    text = build_info_text(service_key, variant)

    if variant == "free":
        cfg = get_variant_cfg(service_key, variant)
        available, next_dt = await db.check_free_cooldown(
            callback.from_user.id, service_key, cfg["cooldown_days"]
        )
        if not available:
            await callback.answer(_cooldown_text(next_dt), show_alert=True)
            return

    await callback.message.edit_text(text, reply_markup=order_action_kb(service_key, variant))


# ----------------------------------------------------------------- начало ввода заказа
@router.callback_query(F.data.startswith("start_order:"))
async def start_order(callback: CallbackQuery, state: FSMContext):
    _, service_key, variant = callback.data.split(":")
    user_id = callback.from_user.id
    if await _reject_if_banned(callback, user_id):
        return

    try:
        cfg = get_variant_cfg(service_key, variant)
    except (KeyError, StopIteration):
        await callback.answer("Эта услуга недоступна.", show_alert=True)
        return

    # Старое сообщение с кнопкой «Дать заявку» остаётся в чате — лимит бесплатной услуги
    # нужно проверять и здесь (а окончательно — атомарно при подтверждении заказа).
    if variant == "free":
        available, next_dt = await db.check_free_cooldown(user_id, service_key, cfg["cooldown_days"])
        if not available:
            await callback.answer(_cooldown_text(next_dt), show_alert=True)
            return

    await state.clear()
    await state.update_data(service_key=service_key, variant=variant)
    await state.set_state(OrderStates.waiting_amount)

    await safe_delete(callback.message)
    await callback.message.answer(
        f"Введите нужное количество (от {cfg['min']} до {cfg['max']}):",
        reply_markup=back_only_kb(),
    )


_MENU_INTERRUPT_TEXTS = {
    BTN_NUMBER, BTN_MY_ORDERS, BTN_TOPUP, BTN_BALANCE, BTN_HELP,
    BTN_ORDER, BTN_FREE_PROMO,
}


@router.message(OrderStates.waiting_amount, F.text.in_(_MENU_INTERRUPT_TEXTS))
@router.message(OrderStates.waiting_link, F.text.in_(_MENU_INTERRUPT_TEXTS))
async def interrupt_order_flow(message: Message, state: FSMContext):
    """Если пользователь на шаге ввода количества/ссылки нажал другую кнопку
    главного меню — прерываем оформление заказа и переходим в нужный раздел."""
    await state.clear()
    text = message.text
    if text == BTN_NUMBER:
        await take_number(message)
    elif text == BTN_MY_ORDERS:
        await my_orders(message)
    elif text == BTN_TOPUP:
        await topup_start(message)
    elif text == BTN_BALANCE:
        await my_balance(message)
    elif text == BTN_HELP:
        await help_start(message, state)
    elif text == BTN_ORDER:
        await order_start(message, state)
    elif text == BTN_FREE_PROMO:
        await free_promo_start(message, state)


@router.message(OrderStates.waiting_amount, F.text == BTN_BACK)
async def amount_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


async def _invalid_input(message: Message, state: FSMContext, bot: Bot, error_text: str):
    """Учитывает некорректный ввод (антиспам). При превышении лимита — бан."""
    result = await db.register_invalid_event(message.from_user.id)
    if result["banned"]:
        await _notify_ban(message, result, bot)
        await state.clear()
        return
    await message.answer(error_text)


@router.message(OrderStates.waiting_amount)
async def amount_received(message: Message, state: FSMContext, bot: Bot):
    if await _reject_if_banned(message, message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    if "service_key" not in data:
        await state.clear()
        await message.answer("Сессия устарела. Начните заново.", reply_markup=main_menu_kb())
        return
    cfg = get_variant_cfg(data["service_key"], data["variant"])

    # parse_uint принимает только ASCII-цифры и не падает на фото/стикерах (text=None)
    # и на символах вроде '²', у которых str.isdigit() == True, а int() падает
    amount = parse_uint((message.text or "").strip())
    if amount is None:
        await _invalid_input(message, state, bot, "❗ Введите правильное число, без букв и символов.")
        return
    if amount < cfg["min"] or amount > cfg["max"]:
        await _invalid_input(
            message, state, bot, f"❗ Введите правильное число от {cfg['min']} до {cfg['max']}."
        )
        return

    await state.update_data(amount=amount)
    await state.set_state(OrderStates.waiting_link)
    await message.answer(LINK_HINTS.get(cfg["link_type"], "Отправьте ссылку:"))


@router.message(OrderStates.waiting_link, F.text == BTN_BACK)
async def link_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(OrderStates.waiting_link)
async def link_received(message: Message, state: FSMContext, bot: Bot):
    if await _reject_if_banned(message, message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    if "service_key" not in data or "amount" not in data:
        await state.clear()
        await message.answer("Сессия устарела. Начните заново.", reply_markup=main_menu_kb())
        return
    cfg = get_variant_cfg(data["service_key"], data["variant"])
    link = (message.text or "").strip()

    if not validate_link(cfg["link_type"], link):
        await _invalid_input(
            message, state, bot,
            "❗ Ссылка указана неверно.\n" + LINK_HINTS.get(cfg["link_type"], ""),
        )
        return

    price = calc_price(cfg["price_per_1000"], data["amount"])

    if price > 0:
        balance = await db.get_balance(message.from_user.id)
        if balance < price:
            await message.answer(
                f"❗ Недостаточно средств на балансе.\n"
                f"Нужно: {price} so'm, у вас: {balance} so'm.\n"
                f"Пополните счёт и попробуйте снова.",
                reply_markup=main_menu_kb(),
            )
            await state.clear()
            return

    await state.update_data(link=link, price=price)
    await state.set_state(OrderStates.confirm)

    price_text = "бесплатно" if price == 0 else f"{price} so'm"
    summary = (
        f"<b>Проверьте данные заказа:</b>\n\n"
        f"Услуга: {cfg['service_title']} — {cfg['label']}\n"
        f"Количество: {data['amount']}\n"
        f"Ссылка: {html.escape(link)}\n"
        f"Стоимость: {price_text}\n\n"
        f"Если все данные верны, нажмите «Отправить»."
    )
    await message.answer(summary, reply_markup=confirm_order_kb())


@router.callback_query(F.data == "cancel_order_draft")
async def cancel_order_draft(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_delete(callback.message)
    await callback.message.answer("Заказ отменён. Главное меню:", reply_markup=main_menu_kb())


async def _notify_new_order(bot: Bot, order_id: int, user_id: int, username: str | None,
                            cfg: dict, amount: int, link: str, price: int):
    price_text = "бесплатно" if price == 0 else f"{price} so'm"
    text = (
        f"📦 <b>Новая заявка №{order_id}</b>\n"
        f"Пользователь: {user_ref(user_id, username)}\n"
        f"Платформа: {PLATFORM_NAMES[cfg['platform']]}\n"
        f"Услуга: {cfg['service_title']} — {cfg['label']}\n"
        f"Количество: {amount}\n"
        f"Ссылка: {html.escape(link)}\n"
        f"Стоимость: {price_text}"
    )
    targets = [ORDERS_CHAT_ID] if ORDERS_CHAT_ID else []
    delivered = False
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text, reply_markup=admin_order_kb(order_id, "В ожидании"))
            delivered = True
        except Exception:
            logging.exception("Не удалось отправить заявку №%s в ORDERS_CHAT_ID", order_id)
    if not delivered and ADMIN_CHAT_ID:
        # заявка не должна потеряться: если канал недоступен, дублируем её админу в личку
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                "⚠️ Не удалось отправить заявку в канал заявок — дублирую сюда.\n\n" + text,
                reply_markup=admin_order_kb(order_id, "В ожидании"),
            )
        except Exception:
            logging.exception("Не удалось отправить заявку №%s и в ADMIN_CHAT_ID", order_id)


@router.callback_query(OrderStates.confirm, F.data == "confirm_order")
async def confirm_order(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id

    if user_id in _processing_confirm:
        # уже обрабатывается предыдущее нажатие той же кнопки — игнорируем повтор
        await callback.answer("Заявка уже обрабатывается, подождите…", show_alert=True)
        return
    _processing_confirm.add(user_id)
    try:
        if await _reject_if_banned(callback, user_id):
            await state.clear()
            return

        data = await state.get_data()
        if not all(k in data for k in ("service_key", "variant", "amount", "link")):
            await state.clear()
            await callback.answer("Эта заявка уже оформлена или устарела.", show_alert=True)
            return

        cfg = get_variant_cfg(data["service_key"], data["variant"])
        # цена пересчитывается на сервере, а не берётся из сохранённого состояния
        price = calc_price(cfg["price_per_1000"], data["amount"])

        # Списание/отметка бесплатного лимита и создание заказа — одна транзакция:
        # нельзя остаться без денег и без заказа, нельзя обойти дневной/недельный лимит.
        try:
            order_id, next_dt = await db.create_order_atomic(
                user_id=user_id, platform=cfg["platform"], service_key=data["service_key"],
                variant=data["variant"], amount=data["amount"], link=data["link"], price=price,
                cooldown_days=cfg["cooldown_days"], reason=f"Заказ: {cfg['service_title']}",
            )
        except db.InsufficientFunds:
            await state.clear()
            await callback.answer("Недостаточно средств.", show_alert=True)
            return
        if order_id is None:
            await state.clear()
            await callback.answer(_cooldown_text(next_dt), show_alert=True)
            await safe_delete(callback.message)
            await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
            return

        await state.clear()

        # Админа уведомляем ПЕРВЫМ: дальше идут действия с сообщением пользователя,
        # которые могут упасть (например, сообщение слишком старое для удаления).
        await _notify_new_order(
            bot, order_id, user_id, callback.from_user.username,
            cfg, data["amount"], data["link"], price,
        )
        await callback.answer()
        await safe_delete(callback.message)
        await callback.message.answer(
            f"✅ Заявка №{order_id} создана и передана в обработку.",
            reply_markup=main_menu_kb(),
        )
    finally:
        _processing_confirm.discard(user_id)


@router.callback_query(F.data == "confirm_order")
async def confirm_order_stale(callback: CallbackQuery):
    """Нажатие «Отправить» на устаревшем сообщении (состояние уже сброшено/изменилось)."""
    await callback.answer("Эта заявка уже оформлена или устарела.", show_alert=True)


async def _notify_ban(message: Message, result: dict, bot: Bot):
    if result["permanent"]:
        await message.answer(
            "⛔ Вы заблокированы навсегда за повторные некорректные данные.\n"
            "Обратитесь в поддержку для разблокировки.",
            reply_markup=main_menu_kb(),
        )
    else:
        until = fmt_local(result["until"], "%H:%M")
        await message.answer(
            f"⛔ Вы временно заблокированы до {until} за многократный ввод некорректных данных.\n"
            f"При повторном нарушении блокировка станет постоянной.",
            reply_markup=main_menu_kb(),
        )
    if ADMIN_CHAT_ID:
        kind = "постоянно" if result["permanent"] else f"до {fmt_local(result['until'], '%d.%m %H:%M')}"
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"🚫 Пользователь {user_ref(message.from_user.id, message.from_user.username)} "
                f"заблокирован {kind} за спам некорректными данными.",
                reply_markup=admin_ban_notice_kb(message.from_user.id),
            )
        except Exception:
            logging.exception("Не удалось отправить уведомление о бане в ADMIN_CHAT_ID")
