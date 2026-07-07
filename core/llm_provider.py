"""Единая точка выбора LLM-провайдера для всех агентов проекта (Sentiment Analyst,
Reply Strategist). Переключить разом — поменять PROVIDER здесь, не в каждом файле.

Groq (llama-3.3-70b-versatile) — бесплатный, но дневной лимит 100k токенов кончается
за один прогон на полной истории отзывов. ProxyAPI (gpt-4o-mini) — платный
OpenAI-совместимый прокси (уже используется в Zerocoder/ad-agent), без дневного лимита
и, по ощущениям, увереннее рассуждает на многошаговых промптах (классификация + гайд тона).
"""
import os

from groq import Groq, RateLimitError as GroqRateLimitError
from openai import OpenAI, RateLimitError as OpenAIRateLimitError

from .env import load_env

load_env()

PROVIDER = "proxyapi"  # groq | proxyapi

_MODELS = {"groq": "llama-3.3-70b-versatile", "proxyapi": "gpt-4o-mini"}
MODEL = _MODELS[PROVIDER]

# Один из двух в зависимости от PROVIDER — main_*.py ловят его, чтобы остановиться
# на упоре в лимит, а не засыпать лог одинаковой ошибкой на каждый оставшийся отзыв.
RateLimitError = GroqRateLimitError if PROVIDER == "groq" else OpenAIRateLimitError


def get_client():
    if PROVIDER == "groq":
        return Groq(api_key=os.environ["GROQ_API_KEY"])
    return OpenAI(api_key=os.environ["PROXYAPI_KEY"], base_url="https://api.proxyapi.ru/openai/v1")
