"""Configuracao via variaveis de ambiente (ver GUIA.md)."""
import logging
import os

from cashflow.util import chave_telefone

_nivel_log = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), None)
logging.basicConfig(
    level=_nivel_log if isinstance(_nivel_log, int) else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("cashflow")


def _ligado(nome: str) -> bool:
    return os.environ.get(nome, "").strip().lower() in ("1", "true", "sim", "yes", "on")


def _inteiro(nome: str, padrao: int) -> int:
    try:
        return int(os.environ.get(nome, "") or padrao)
    except ValueError:
        log.warning("%s invalido, usando %s", nome, padrao)
        return padrao


# Obrigatorias
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]
EVOLUTION_URL = os.environ["EVOLUTION_URL"].rstrip("/")
EVOLUTION_KEY = os.environ["EVOLUTION_KEY"]
EVOLUTION_INSTANCE = os.environ["EVOLUTION_INSTANCE"]

# IA
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_MODEL_AUDIO = os.environ.get("GROQ_MODEL_AUDIO", "whisper-large-v3-turbo")
AUDIO_MAX_SEGUNDOS = _inteiro("AUDIO_MAX_SEGUNDOS", 180)

# Seguranca
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

# Assinatura (desligada por padrao: ninguem e bloqueado ate ASSINATURA_OBRIGATORIA=1)
ASSINATURA_OBRIGATORIA = _ligado("ASSINATURA_OBRIGATORIA")
DIAS_TESTE = _inteiro("DIAS_TESTE", 7)
LINK_PAGAMENTO = os.environ.get("LINK_PAGAMENTO", "https://pay.hub.la/brHaNisVeNGsyIqmR2Tg")
HUBLA_TOKEN = os.environ.get("HUBLA_TOKEN", "")
ADMIN_TELEFONES = {chave_telefone(n) for n in os.environ.get("ADMIN_TELEFONES", "").split(",") if n.strip()}

if not WEBHOOK_SECRET:
    log.warning("WEBHOOK_SECRET nao definido: o /webhook aceita requisicoes de qualquer origem.")
