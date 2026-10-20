import html
import logging
import time
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
import database as db
from config import ADMIN_CHAT_ID, HELP_COOLDOWN_SECONDS
from keyboards import (
    BTN_NUMBER, BTN_MY_ORDERS, BTN_BALANCE, BTN_HELP, BTN_BACK,
    main_menu_kb, back_only_kb, take_number_kb,
)
from states import HelpStates
from utils import user_ref, BANNED_TEXT

router = Router()

_last_help: dict[int, float] = {}   # user_id -> время последнего обращения (антиспам)


async def _reset(state: FSMContext | None):
    # кнопки главного меню вызываются и напрямую (без state) из других обработчиков
    if state is not None:
        await state.clear()


@router.message(F.text == BTN_NUMBER)
async def take_number(message: Message, state: FSMContext | None = None):
    await _reset(state)
    await message.answer(
        "📱 Вы можете оформить номер через администратора.\n"
        "Функция автоматического добавления номера скоро будет доступна.",
        reply_markup=take_number_kb(),
    )


@router.message(F.text == BTN_MY_ORDERS)
async def my_orders(message: Message, state: FSMContext | None = None):
    await _reset(state)
    orders = await db.get_user_orders(message.from_user.id)
    if not orders:
        await message.answer("У вас пока нет заказов.")
        return
    lines = ["🧾 <b>Ваши заказы:</b>\n"]
    for o in orders:
        lines.append(
            f"№{o['id']} — {o['platform'].upper()} / {o['service_key']} "
            f"({o['amount']} шт.) — {o['price']} so'm — <b>{html.escape(o['status'])}</b>"
        )
    await message.answer("\n".join(lines))


@router.message(F.text == BTN_BALANCE)
async def my_balance(message: Message, state: FSMContext | None = None):
    await _reset(state)
    balance = await db.get_balance(message.from_user.id)
    await message.answer(f"💰 Ваш баланс: <b>{balance} so'm</b>")


@router.message(F.text == BTN_HELP)
async def help_start(message: Message, state: FSMContext):
    if await db.is_banned(message.from_user.id):
        await state.clear()
        await message.answer(BANNED_TEXT)
        return
    await state.set_state(HelpStates.waiting_message)
    await message.answer(
        "❓ Опишите ваш вопрос или проблему одним сообщением — мы передадим его администратору.",
        reply_markup=back_only_kb(),
    )


@router.message(HelpStates.waiting_message, F.text == BTN_BACK)
async def help_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(HelpStates.waiting_message)
async def help_received(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if await db.is_banned(user_id):
        await state.clear()
        await message.answer(BANNED_TEXT, reply_markup=main_menu_kb())
        return
    if not message.text:
        # фото/стикер раньше уходили админу как "None"
        await message.answer("❗ Пожалуйста, опишите вопрос текстом.")
        return

    now = time.monotonic()
    wait = HELP_COOLDOWN_SECONDS - (now - _last_help.get(user_id, -1e9))
    if wait > 0:
        await message.answer(f"⏳ Подождите ещё {int(wait) + 1} сек. перед следующим сообщением.")
        return

    if not ADMIN_CHAT_ID:
        await message.answer("⚠️ Поддержка временно недоступна. Попробуйте позже.")
        return
    try:
        # текст пользователя экранируется: бот работает в режиме HTML, и «<» в тексте
        # раньше ломал отправку — админ ничего не получал, а клиент видел «отправлено»
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"❓ Вопрос от пользователя {user_ref(user_id, message.from_user.username)}:\n\n"
            f"{html.escape(message.text[:3500])}\n\n"
            f"Ответить: /reply {user_id} текст_ответа",
        )
    except Exception:
        logging.exception("Не удалось отправить вопрос в поддержку в ADMIN_CHAT_ID")
        await message.answer("⚠️ Не удалось отправить сообщение. Попробуйте ещё раз чуть позже.")
        return

    _last_help[user_id] = now
    if len(_last_help) > 5000:   # чтобы словарь не рос бесконечно
        for uid in [u for u, t in _last_help.items() if now - t > HELP_COOLDOWN_SECONDS]:
            _last_help.pop(uid, None)
    await state.clear()
    await message.answer("✅ Ваше сообщение отправлено администратору. Ожидайте ответа.",
                         reply_markup=main_menu_kb())
