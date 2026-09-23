"""Funções puras usadas no app todo: datas, texto, dinheiro. Nada aqui acessa banco ou rede."""
import math
import re
import unicodedata
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

# O servidor roda em UTC; o "hoje" do usuario e o de Brasilia.
try:
    from zoneinfo import ZoneInfo
    FUSO = ZoneInfo("America/Sao_Paulo")
except Exception:
    FUSO = timezone(timedelta(hours=-3))  # Brasilia nao tem horario de verao desde 2019

MESES = [
    "Janeiro", "Fevereiro", "Marco", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
]

MES_POR_NOME = {m.lower(): i for i, m in enumerate(MESES, 1)}

# Acima disso e erro de leitura, nao um lancamento (e estouraria o NUMERIC(12,2)).
VALOR_MAXIMO = 1_000_000_000


def agora() -> datetime:
    return datetime.now(FUSO)


def hoje() -> date:
    return datetime.now(FUSO).date()


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def normalizar(texto: str) -> str:
    # "Quanto gastei com Uber?" -> "quanto gastei com uber"  (mantem digitos, virgula e ponto de valores)
    t = sem_acento(texto).lower()
    t = re.sub(r"[^\w\s.,/$+-]", " ", t)
    t = re.sub(r"(?<!\d)[.,]|[.,](?!\d)", " ", t)
    return " ".join(t.split())


def mascarar(telefone: str) -> str:
    numero = re.sub(r"\D", "", telefone.split("@")[0])
    return "***" + numero[-4:]


def numero_br(numero: str) -> str:
    # So digitos, com o 55 na frente se vier sem ("11988887777" -> "5511988887777").
    digitos = re.sub(r"\D", "", str(numero or "").split("@")[0])
    return "55" + digitos if len(digitos) in (10, 11) else digitos


def chave_telefone(numero: str) -> str:
    # O mesmo celular pode vir como 5511988887777 (Hubla) ou 551188887777 (WhatsApp antigo, sem o 9).
    digitos = numero_br(numero)
    if len(digitos) == 13 and digitos.startswith("55") and digitos[4] == "9":
        digitos = digitos[:4] + digitos[5:]
    return digitos


def fmt(valor: float) -> str:
    return "R$ {:,.2f}".format(valor).replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_sinal(valor: float) -> str:
    sinal = "+" if valor >= 0 else ""
    return "R$ {}{:,.2f}".format(sinal, valor).replace(",", "X").replace(".", ",").replace("X", ".")


def barra(porc, n=10):
    c = min(max(int(porc), 0) * n // 100, n)
    return "█" * c + "░" * (n - c)


def para_valor(valor) -> float:
    # Aceita 85, "85,90", "1.234,56", "R$ 50". Invalido vira 0.
    if isinstance(valor, bool):
        return 0.0

    if isinstance(valor, (int, float)):
        try:
            numero = float(valor)
        except OverflowError:
            return 0.0
    else:
        texto = re.sub(r"[^\d,.-]", "", str(valor or ""))

        if "," in texto and "." in texto:
            if texto.rfind(",") > texto.rfind("."):
                texto = texto.replace(".", "").replace(",", ".")  # 1.234,56
            else:
                texto = texto.replace(",", "")  # 1,234.56
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", texto):
            texto = texto.replace(".", "")  # 1.500 = mil e quinhentos
        else:
            texto = texto.replace(",", ".")

        try:
            numero = float(texto)
        except ValueError:
            return 0.0

    numero = abs(numero)

    if not math.isfinite(numero) or numero >= VALOR_MAXIMO:
        return 0.0

    return round(numero, 2)


def ultimo_dia(ano: int, mes: int) -> int:
    return monthrange(ano, mes)[1]


def somar_meses(d: date, n: int, dia: int = None) -> date:
    # 31/01 + 1 mes = 28/02 (ou 29): o dia e limitado ao tamanho do mes.
    total = d.year * 12 + (d.month - 1) + n
    ano, mes = divmod(total, 12)
    mes += 1
    return date(ano, mes, min(dia or d.day, ultimo_dia(ano, mes)))


def inicio_mes(d: date) -> date:
    return d.replace(day=1)


def fim_mes(d: date) -> date:
    return d.replace(day=ultimo_dia(d.year, d.month))


def dividir_centavos(total: float, n: int) -> list:
    # 100 em 3x = 33,34 + 33,33 + 33,33 (a soma sempre bate com o total)
    total_c = int((Decimal(str(total)) * 100).to_integral_value())
    base = (Decimal(total_c) / n).to_integral_value(rounding=ROUND_DOWN)
    resto = total_c - int(base) * n
    return [float((int(base) + (1 if i < resto else 0)) / Decimal(100)) for i in range(n)]


def nome_mes(d: date, com_ano: bool = False) -> str:
    nome = MESES[d.month - 1]
    return "{} {}".format(nome, d.year) if com_ano else nome


def periodo_hoje():
    h = hoje()
    return h, h


def periodo_semana():
    h = hoje()
    return h - timedelta(days=h.weekday()), h


def periodo_mes():
    h = hoje()
    return h.replace(day=1), h
