import json
import os
import re

from dotenv import load_dotenv
from fastapi import APIRouter, Depends
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel

from mysite.api.auth import get_current_user
from mysite.api.users import user_to_dict
from mysite.database.models import User

load_dotenv()

agent_router = APIRouter(prefix="/agent", tags=["Agent"])

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:latest")

# Короткий промпт: маленькие модели начинают повторять длинные примеры.
SYSTEM_PROMPT = """Ты помощник платформы. Твой единственный инструмент: get_my_profile (читает профиль текущего пользователя).
На любой вопрос о профиле сначала вызови get_my_profile и отвечай только по его данным.

Правила:
1. Значения копируй точно: не переводи, не транслитерируй, не меняй регистр и формат даты.
2. Отвечай коротко. Не повторяй вопрос пользователя и не пиши вступлений.
3. Если просят несколько полей (фио, почта, роль), ответь одной строкой в формате:
   фио: <full_name>. почта: <email>. роль: <role>
4. Если просят "ответь только значением", выведи только значение, без слов.
5. Вопрос про дату или статус: одно короткое предложение с точным значением.
   Слова "актуальный", "активный" относятся к полю user_status.
6. Язык ответа совпадает с языком вопроса (русский или кыргызский), значения полей остаются как есть.
   На кыргызском: Профилиңизде атыңыз <full_name> деп көрсөтүлгөн. Почтаныз <email>
7. Никогда не показывай пароль, хеш и токены."""

# ---------- Жёсткие ограничения (в коде, не в промпте) ----------
WRITE_RE = re.compile(
    r"измени|изменить|поменя|смени|обнови|удали|удалить|зарегистр|созда|"
    r"change|update|delete|remove|register|create|"
    r"өзгөрт|алмаштыр|жаңыр|жаңыла|өчүр|катта",
    re.IGNORECASE,
)
OTHER_USER_RE = re.compile(
    r"друг\w*\s+пользовател|чуж\w*|other\s+user|башка\s+колдонуучу",
    re.IGNORECASE,
)

READ_ONLY_RU = "У меня нет инструмента для изменения. Сейчас я могу только читать твой профиль."
READ_ONLY_KY = "Менде өзгөртүү куралы жок. Азыр мен профилиңди гана окуй алам."
OTHER_USER_RU = "Я могу получить только профиль пользователя, авторизованного в текущем запросе."
OTHER_USER_KY = "Мен учурдагы сурамда авторизацияланган колдонуучунун профилин гана ала алам."


def _is_kyrgyz(text: str) -> bool:
    return bool(re.search(r"[өүң]", text.lower()))


def asks_other_user(message: str, own_id: int) -> bool:
    ids = {int(x) for x in re.findall(r"\bid\s*[:=#]?\s*(\d+)", message, re.IGNORECASE)}
    ids |= {
        int(x)
        for x in re.findall(r"(?:пользовател\w*|user|колдонуучу\w*)\s+(?:с\s+)?#?(\d+)", message, re.IGNORECASE)
    }
    return any(i != own_id for i in ids) or bool(OTHER_USER_RE.search(message))


def guardrail(message: str, own_id: int) -> str | None:
    """Если запрос запрещён, вернуть готовый ответ без вызова модели."""
    ky = _is_kyrgyz(message)
    if WRITE_RE.search(message):
        return READ_ONLY_KY if ky else READ_ONLY_RU
    if asks_other_user(message, own_id):
        return OTHER_USER_KY if ky else OTHER_USER_RU
    return None


class ChatIn(BaseModel):
    message: str


def get_model() -> ChatOllama:
    return ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_URL, temperature=0)


def build_tools(profile: dict):
    """Профиль фиксируется в closure: LLM не может подставить чужой id."""

    @tool
    def get_my_profile() -> dict:
        """Возвращает профиль текущего авторизованного пользователя:
        id, full_name, email, role, user_status, дата регистрации. Аргументов нет."""
        return profile

    return [get_my_profile]


def run_with_create_agent(message: str, profile: dict) -> str:
    agent = create_agent(
        model=get_model(),
        tools=build_tools(profile),
        system_prompt=SYSTEM_PROMPT,
    )
    result = agent.invoke({"messages": [{"role": "user", "content": message}]})
    return result["messages"][-1].content


def run_manual_loop(message: str, profile: dict, max_steps: int = 5) -> str:
    """bind_tools + ToolMessage менен кол жасалма agent loop."""
    tools = build_tools(profile)
    tools_by_name = {t.name: t for t in tools}
    llm = get_model().bind_tools(tools)

    messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(message)]
    for _ in range(max_steps):
        ai_msg = llm.invoke(messages)
        messages.append(ai_msg)
        if not ai_msg.tool_calls:
            return ai_msg.content
        for call in ai_msg.tool_calls:
            result = tools_by_name[call["name"]].invoke(call["args"])
            messages.append(
                ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call["id"])
            )
    return "Агент токтоду: кадамдардын лимити бүттү."


def _as_text(content) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content


@agent_router.post("/chat/")
def chat(data: ChatIn, current_user: User = Depends(get_current_user)):
    profile = user_to_dict(current_user)

    blocked = guardrail(data.message, profile["id"])
    if blocked:
        return {"answer": blocked}

    runner = run_manual_loop if os.getenv("AGENT_MODE") == "manual" else run_with_create_agent
    return {"answer": _as_text(runner(data.message, profile))}