"""
core/llm_retry.py — Ortak Gemini istemcisi + 503 retry.

Bütün ajan araçları (dev_agent, project_agent, agent_task, wiki_agent,
code_helper) aynı deseni kopyalıyordu: genai.Client kur, generate_content
çağır, hata olursa pes et. Gemini'nin "503 UNAVAILABLE / high demand"
hataları geçici ve birkaç saniye içinde düzeliyor — ama retry olmadığı
için bir görevin tamamı tek geçici hataya kurban gidiyordu.

get_model(model_name) buradan alınır; generate_content otomatik olarak
503'te bekleyip tekrar dener. 429 (kota) BİLEREK tekrar denenmez — kota
dolmuşsa beklemek çözmez, çağıran taraf kendi fallback'ine düşmeli
(ör. web_search'ün DDG devre kesicisi).
"""
import json
import sys
import time
from pathlib import Path

DEFAULT_MODEL   = "gemini-flash-latest"
MAX_ATTEMPTS    = 3
BACKOFF_BASE_S  = 4.0        # 4s, 8s beklemeler — 503 tepeleri genelde saniyeler içinde iniyor
REQUEST_TIMEOUT_MS = 45_000  # istek asılı kalırsa (503 gibi hata FIRLATMADAN sonsuza kadar
                              # beklerse) burada kesilip retry mantığına düşsün diye zorunlu


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


API_CONFIG_PATH = _base_dir() / "config" / "api_keys.json"


def get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc)
    low = msg.lower()
    return (
        "503" in msg or "UNAVAILABLE" in msg or "overloaded" in low
        or "504" in msg or "DEADLINE_EXCEEDED" in msg
        or "timeout" in low or "timed out" in low
    )


class RetryingModel:
    """generate_content(contents) -> response; 503'te bekleyip tekrar dener."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self._model_name = model_name
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types
            self._client = genai.Client(
                api_key=get_api_key(),
                http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
            )
        return self._client

    def generate_content(self, contents, config=None):
        last_exc: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                kwargs = {"model": self._model_name, "contents": contents}
                if config is not None:
                    kwargs["config"] = config
                return self._get_client().models.generate_content(**kwargs)
            except Exception as e:
                last_exc = e
                if not _is_retryable(e) or attempt == MAX_ATTEMPTS:
                    raise
                wait = BACKOFF_BASE_S * (2 ** (attempt - 1))
                print(f"[LLM] 503/overload (deneme {attempt}/{MAX_ATTEMPTS}) — {wait:.0f}s bekleniyor...")
                time.sleep(wait)
        raise last_exc  # pragma: no cover — yukarıdaki raise'ler kapsıyor


def get_model(model_name: str = DEFAULT_MODEL) -> RetryingModel:
    return RetryingModel(model_name)
