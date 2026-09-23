"""
╔══════════════════════════════════════════════════════════════╗
║              Cash Flow IA — WhatsApp Bot v3.3                ║
║         Flask · Evolution API · Groq · PostgreSQL            ║
╠══════════════════════════════════════════════════════════════╣
║  Variáveis de ambiente obrigatórias:                         ║
║    DATABASE_URL       → postgresql://user:pass@host/db       ║
║    GROQ_API_KEY       → sua chave Groq                       ║
║    EVOLUTION_URL      → http://SEU-IP:8080                   ║
║    EVOLUTION_KEY      → apikey configurada no .env           ║
║    EVOLUTION_INSTANCE → nome da instância (ex: cashflow)     ║
║    PORT               → (opcional) padrão 5000               ║
║  Opcionais:                                                  ║
║    WEBHOOK_SECRET     → exige ?token=... na URL do webhook   ║
║    GROQ_MODEL         → padrão llama-3.1-8b-instant          ║
║    LOG_LEVEL          → padrão INFO (DEBUG mostra payloads)  ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import re
import hmac
import json
import math
import base64
import random
import hashlib
import logging
import threading
import unicodedata
import requests
from datetime import date, datetime, timedelta, timezone
from calendar import monthrange

import psycopg2
import psycopg2.extras
from flask import Flask, request
from groq import Groq

_nivel_log = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), None)
logging.basicConfig(
    level=_nivel_log if isinstance(_nivel_log, int) else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("cashflow")

app = Flask(__name__)

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
DATABASE_URL = os.environ["DATABASE_URL"]

EVOLUTION_URL = os.environ["EVOLUTION_URL"].rstrip("/")
EVOLUTION_KEY = os.environ["EVOLUTION_KEY"]
EVOLUTION_INSTANCE = os.environ["EVOLUTION_INSTANCE"]

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

if not WEBHOOK_SECRET:
    log.warning("WEBHOOK_SECRET nao definido: o /webhook aceita requisicoes de qualquer origem.")

# Timeout curto e 1 retry: uma IA lenta nao pode travar o worker do gunicorn.
groq_client = Groq(api_key=GROQ_API_KEY, timeout=12, max_retries=1)

# O servidor roda em UTC; o "hoje" do usuario e o de Brasilia.
try:
    from zoneinfo import ZoneInfo
    FUSO = ZoneInfo("America/Sao_Paulo")
except Exception:
    FUSO = timezone(timedelta(hours=-3))  # Brasilia nao tem horario de verao desde 2019

_local = threading.local()


def hoje() -> date:
    return datetime.now(FUSO).date()


def http() -> requests.Session:
    # Uma sessao por thread: reaproveita a conexao com a Evolution entre mensagens.
    if not hasattr(_local, "sessao"):
        _local.sessao = requests.Session()
    return _local.sessao


def mascarar(telefone: str) -> str:
    numero = re.sub(r"\D", "", telefone.split("@")[0])
    return "***" + numero[-4:]


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


# Indice do payload que a Evolution aceitou por ultimo (v1 usa textMessage, v2 usa text).
_formato_envio = 0


def enviar(telefone: str, texto: str):
    global _formato_envio

    numero = telefone.replace("@s.whatsapp.net", "").replace("@c.us", "")

    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"

    headers = {
        "Content-Type": "application/json",
        "apikey": EVOLUTION_KEY
    }

    payloads = [
        {
            "number": numero,
            "textMessage": {
                "text": texto
            }
        },
        {
            "number": numero,
            "text": texto
        }
    ]

    # Tenta primeiro o formato que funcionou da ultima vez: 1 requisicao em vez de 2.
    ordem = sorted(range(len(payloads)), key=lambda i: i != _formato_envio)
    erro = None

    for i in ordem:
        try:
            r = http().post(url, json=payloads[i], headers=headers, timeout=10)
        except requests.RequestException as e:
            erro = e
            continue

        if r.status_code < 400:
            if i != _formato_envio:
                log.info("Evolution: usando o formato de envio %s", i)
                _formato_envio = i
            log.debug("EVOLUTION SEND STATUS %s: %s", r.status_code, r.text[:500])
            return True

        erro = "status {}: {}".format(r.status_code, r.text[:300])

    log.error("Falha ao enviar mensagem para %s: %s", mascarar(telefone), erro)
    return False


def fmt(valor: float) -> str:
    return "R$ {:,.2f}".format(valor).replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_sinal(valor: float) -> str:
    sinal = "+" if valor >= 0 else ""
    return "R$ {}{:,.2f}".format(sinal, valor).replace(",", "X").replace(".", ",").replace("X", ".")


def dias_restantes_mes() -> int:
    # Conta o dia de hoje: no ultimo dia do mes ainda resta 1 dia, nao 0.
    h = hoje()
    ultimo = monthrange(h.year, h.month)[1]
    return ultimo - h.day + 1


_tabelas_prontas = False
_tabelas_lock = threading.Lock()


def get_conn():
    global _tabelas_prontas

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=10)

    # Cria as tabelas uma vez por processo, e nao a cada conexao.
    if not _tabelas_prontas:
        with _tabelas_lock:
            if not _tabelas_prontas:
                try:
                    criar_tabelas(conn)
                except Exception:
                    conn.close()
                    raise
                _tabelas_prontas = True

    return conn


def criar_tabelas(conn):
    with conn.cursor() as cur:
        # Evita dois workers criando as mesmas tabelas ao mesmo tempo num banco novo.
        cur.execute("SELECT pg_advisory_xact_lock(727274)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transacoes (
                id SERIAL PRIMARY KEY,
                telefone TEXT NOT NULL,
                descricao TEXT NOT NULL,
                valor NUMERIC(12,2) NOT NULL,
                tipo TEXT NOT NULL CHECK (tipo IN ('entrada','saida')),
                categoria TEXT NOT NULL DEFAULT 'Outros',
                data DATE NOT NULL DEFAULT CURRENT_DATE,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS limites (
                telefone TEXT PRIMARY KEY,
                limite_mensal NUMERIC(12,2) NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_transacoes_tel_data
            ON transacoes (telefone, data)
        """)
    conn.commit()


# Se mudar id_curto(), mude tambem o left(md5(id::text), 6) de SQL_POR_ID_CURTO.
def id_curto(pk: int) -> str:
    return hashlib.md5(str(pk).encode()).hexdigest()[:6]


SQL_POR_ID_CURTO = """
    SELECT id, descricao, valor, tipo, categoria
    FROM transacoes
    WHERE telefone=%s AND left(md5(id::text), 6)=%s
    ORDER BY id DESC
    LIMIT 1
"""


def salvar_transacao(telefone, descricao, valor, tipo, categoria):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transacoes (telefone, descricao, valor, tipo, categoria, data)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (telefone, descricao, valor, tipo, categoria, hoje())
            )
            pk = cur.fetchone()["id"]
        conn.commit()
        return pk, id_curto(pk)
    finally:
        conn.close()


def editar_transacao(telefone, short_id, novo_valor, nova_desc, usar_ultimo=False):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if usar_ultimo:
                cur.execute(
                    """
                    SELECT id, descricao, valor, tipo, categoria
                    FROM transacoes
                    WHERE telefone=%s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (telefone,)
                )
                row = cur.fetchone()
            else:
                cur.execute(SQL_POR_ID_CURTO, (telefone, (short_id or "").lower()))
                row = cur.fetchone()

            if not row:
                return None

            updates, params = [], []

            if novo_valor and novo_valor > 0:
                updates.append("valor=%s")
                params.append(novo_valor)

            if nova_desc:
                updates.append("descricao=%s")
                params.append(nova_desc.capitalize())

            if not updates:
                return dict(row)

            params.append(row["id"])

            cur.execute(
                "UPDATE transacoes SET {} WHERE id=%s RETURNING *".format(", ".join(updates)),
                params
            )

            updated = cur.fetchone()

        conn.commit()
        return dict(updated) if updated else None

    finally:
        conn.close()


def apagar_transacao(telefone, short_id=None, usar_ultimo=False):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if usar_ultimo:
                cur.execute(
                    """
                    SELECT id, descricao, valor, tipo
                    FROM transacoes
                    WHERE telefone=%s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (telefone,)
                )
                row = cur.fetchone()
            else:
                cur.execute(SQL_POR_ID_CURTO, (telefone, (short_id or "").lower()))
                row = cur.fetchone()

            if not row:
                return None

            cur.execute("DELETE FROM transacoes WHERE id=%s", (row["id"],))

        conn.commit()
        return dict(row)

    finally:
        conn.close()


def buscar_transacoes(telefone, data_ini, data_fim):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, descricao, valor, tipo, categoria, data
                FROM transacoes
                WHERE telefone=%s AND data BETWEEN %s AND %s
                ORDER BY data, id
                """,
                (telefone, data_ini, data_fim)
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def buscar_limite(telefone):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT limite_mensal FROM limites WHERE telefone=%s", (telefone,))
            row = cur.fetchone()
            return float(row["limite_mensal"]) if row else 0.0
    finally:
        conn.close()


def salvar_limite(telefone, valor):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO limites (telefone, limite_mensal)
                VALUES (%s, %s)
                ON CONFLICT (telefone)
                DO UPDATE SET limite_mensal=EXCLUDED.limite_mensal
                """,
                (telefone, valor)
            )
        conn.commit()
    finally:
        conn.close()


def periodo_hoje():
    h = hoje()
    return h, h


def periodo_semana():
    h = hoje()
    return h - timedelta(days=h.weekday()), h


def periodo_mes():
    h = hoje()
    return h.replace(day=1), h


MESES = [
    "Janeiro", "Fevereiro", "Marco", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
]


SYSTEM_PROMPT = """Voce e o Cash Flow IA, assistente financeiro no WhatsApp.
Responda APENAS com JSON valido, sem texto extra e sem markdown.

Formato:
{"intencao":"...","descricao":"...","valor":0.0,"tipo":"entrada|saida","categoria":"..."}

INTENCOES:
gasto -> registrar transacao
resumo -> resumo rapido de hoje
extrato -> extrato completo
relatorio -> mes ou relatorio mensal
semana -> relatorio semanal
saldo -> saldo mensal
top -> top gastos
posso_gastar -> quanto posso gastar
limite -> definir limite mensal
editar -> editar transacao
apagar -> apagar transacao
dica -> dica financeira
ajuda -> comandos
oi -> saudacoes sem numero
confirmacao -> pix ambiguo
duvida -> simulacao/calculo, nao registrar
outro -> qualquer outra coisa

TIPO:
entrada: salario, pagamento recebido, freela, pix recebido, transferencia recebida, retorno investimento, rendimento, lucro, ganhei, recebi, dividendo, venda, vendi
saida: compras, despesas, contas, servicos, assinaturas, pix enviado, investimento, investi, apliquei, aporte

AMBIGUO:
"pix 100" sem contexto -> {"intencao":"confirmacao","valor":100.0}

NAO REGISTRAR:
calcular, simular, prever, quanto ficaria, se eu gastar -> duvida

EDITAR:
"editar ultimo 120" -> {"intencao":"editar","descricao":"ultimo","valor":120.0}
"editar ae3f06 150" -> {"intencao":"editar","descricao":"ae3f06","valor":150.0}

APAGAR:
"apagar ultimo" -> {"intencao":"apagar","descricao":"ultimo"}
"excluir ae3f06" -> {"intencao":"apagar","descricao":"ae3f06"}

CATEGORIAS:
Saida: Alimentacao|Transporte|Lazer|Saude|Moradia|Educacao|Beleza e Cuidados|Roupas|Servicos|Investimentos|Outros
Entrada: Salario|Freela|Investimentos|Vendas|Transferencia|Outros

SOMENTE JSON."""


INTENCOES = {
    "gasto", "resumo", "hoje", "extrato", "relatorio", "semana", "saldo", "top", "posso_gastar",
    "limite", "editar", "apagar", "dica", "ajuda", "oi", "confirmacao", "duvida", "outro",
    "erro",  # interna: a API da IA falhou
}

CATEGORIAS = [
    "Alimentacao", "Transporte", "Lazer", "Saude", "Moradia", "Educacao", "Beleza e Cuidados", "Roupas",
    "Servicos", "Investimentos", "Salario", "Freela", "Vendas", "Transferencia", "Outros",
]

_CATEGORIA_POR_CHAVE = {c.lower(): c for c in CATEGORIAS}

# Acima disso e erro de leitura, nao um lancamento (e estouraria o NUMERIC(12,2)).
VALOR_MAXIMO = 1_000_000_000


def normalizar_categoria(categoria) -> str:
    # "Alimentação", "alimentacao" e "Alimentacao" sao a mesma categoria no extrato.
    nome = str(categoria or "").strip()
    if not nome:
        return "Outros"
    return _CATEGORIA_POR_CHAVE.get(sem_acento(nome).lower(), nome)


def para_valor(valor) -> float:
    # A IA as vezes manda texto: "85,90", "1.234,56", "R$ 50". Invalido vira 0.
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


def normalizar_ia(dados: dict) -> dict:
    # IAs pequenas mandam null, "Gasto", "saída", "posso gastar"... aqui tudo vira o formato esperado.
    intencao = sem_acento(str(dados.get("intencao") or "outro")).strip().lower().replace(" ", "_")
    tipo = sem_acento(str(dados.get("tipo") or "saida")).strip().lower()

    return {
        "intencao": intencao if intencao in INTENCOES else "outro",
        "descricao": str(dados.get("descricao") or "").strip(),
        "valor": para_valor(dados.get("valor")),
        "tipo": tipo if tipo in ("entrada", "saida") else "saida",
        "categoria": normalizar_categoria(dados.get("categoria")),
    }


# Comandos exatos (os da _ajuda_) nao precisam da IA: resposta mais rapida e sem erro de interpretacao.
ATALHOS = {
    "saldo": "saldo",
    "extrato": "extrato",
    "extrato completo": "extrato",
    "mes": "relatorio",
    "relatorio": "relatorio",
    "relatorio do mes": "relatorio",
    "relatorio mensal": "relatorio",
    "semana": "semana",
    "relatorio semanal": "semana",
    "hoje": "hoje",
    "resumo": "resumo",
    "top": "top",
    "ajuda": "ajuda",
    "comandos": "ajuda",
    "help": "ajuda",
    "dica": "dica",
    "dicas": "dica",
    "posso gastar": "posso_gastar",
    "quanto posso gastar": "posso_gastar",
}


def atalho(mensagem: str):
    chave = " ".join(re.sub(r"[^\w\s]", " ", sem_acento(mensagem).lower()).split())
    intencao = ATALHOS.get(chave)
    return normalizar_ia({"intencao": intencao}) if intencao else None


def chamar_ia(mensagem: str) -> dict:
    try:
        res = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": mensagem}
            ],
            temperature=0.15,
            max_tokens=200
        )
    except Exception as e:
        log.error("Erro na API da IA: %s", e)
        return normalizar_ia({"intencao": "erro"})

    try:
        raw = (res.choices[0].message.content or "").strip()
        raw = re.sub(r"```json|```", "", raw).strip()

        match = re.search(r"\{.*\}", raw, re.DOTALL)

        if not match:
            raise ValueError("Sem JSON na resposta: {}".format(raw))

        return normalizar_ia(json.loads(match.group()))

    except Exception as e:
        log.error("Resposta invalida da IA: %s", e)
        return normalizar_ia({})


EMOJI_CAT = {
    "alimentacao": "🍔",
    "transporte": "🚗",
    "lazer": "🎮",
    "saude": "💊",
    "moradia": "🏠",
    "educacao": "📚",
    "roupas": "👕",
    "beleza e cuidados": "💅",
    "servicos": "🔧",
    "investimentos": "📈",
    "salario": "💼",
    "freela": "💻",
    "vendas": "🛍️",
    "transferencia": "📲",
    "outros": "📦",
}


def ecat(cat):
    return EMOJI_CAT.get(sem_acento(cat).lower(), "📦")


def barra(porc, n=10):
    c = min(max(int(porc), 0) * n // 100, n)
    return "█" * c + "░" * (n - c)


def totais(txs):
    ent = sum(float(t["valor"]) for t in txs if t["tipo"] == "entrada")
    sai = sum(float(t["valor"]) for t in txs if t["tipo"] == "saida")
    return ent, sai, ent - sai


def alerta_limite(sai, limite):
    if limite <= 0:
        return ""

    porc = int((sai / limite) * 100)

    if sai > limite:
        return "\n\n🚨 *Limite estourado!* Passou {} do teto.".format(fmt(sai - limite))
    elif porc >= 90:
        return "\n\n⚠️ *Atencao!* {}% do limite usado.".format(porc)
    elif porc >= 75:
        return "\n\n💛 {}% do limite usado — fica de olho!".format(porc)

    return ""


def relatorio_resumo(titulo, txs, limite=0.0):
    if not txs:
        return "*{}*\n\nNenhuma movimentacao ainda.\n_mercado 85_ para comecar.".format(titulo)

    ent, sai, sal = totais(txs)
    emoji = "🟢" if sal >= 0 else "🔴"

    top = sorted(
        [t for t in txs if t["tipo"] == "saida"],
        key=lambda x: -float(x["valor"])
    )[:3]

    linhas = [
        "*{}*\n".format(titulo),
        "💚 Entradas:  {}".format(fmt(ent)),
        "🔴 Saidas:    {}".format(fmt(sai)),
        "━━━━━━━━━━━━━━",
        "{} *Saldo: {}*".format(emoji, fmt_sinal(sal)),
    ]

    if top:
        linhas.append("\n🏆 *Maiores saidas:*")
        for i, t in enumerate(top, 1):
            linhas.append("{}. {} — {}".format(i, t["descricao"].capitalize(), fmt(float(t["valor"]))))

    if limite > 0:
        porc = int((sai / limite) * 100)
        linhas.append("\n[{}] {}% do limite {}".format(barra(porc), porc, fmt(limite)))

    linhas.append("\n_extrato completo_ para ver todos os lancamentos")

    return "\n".join(linhas)


def relatorio_extrato(titulo, label_ini, label_fim, txs, limite=0.0):
    if not txs:
        return "*{}*\n\nNenhuma movimentacao neste periodo.".format(titulo)

    ent, sai, sal = totais(txs)
    emoji = "🟢" if sal >= 0 else "🔴"

    cats = {}

    for t in txs:
        if t["tipo"] == "saida":
            cats.setdefault(normalizar_categoria(t["categoria"]), []).append(t)

    linhas = [
        "🧾 *{}*".format(titulo),
        "_{} → {}_\n".format(label_ini, label_fim),
    ]

    entradas = [t for t in txs if t["tipo"] == "entrada"]

    if entradas:
        linhas.append("💚 *Entradas*")
        for t in entradas:
            linhas.append("  {} · {}  `{}`".format(t["descricao"].capitalize(), fmt(float(t["valor"])), id_curto(t["id"])))
        linhas.append("  *Total: {}*\n".format(fmt(ent)))

    if cats:
        linhas.append("🔴 *Saidas*")
        for cat, itens in sorted(cats.items(), key=lambda x: -sum(float(i["valor"]) for i in x[1])):
            sub = sum(float(i["valor"]) for i in itens)
            linhas.append("\n{} *{}* — {}".format(ecat(cat), cat, fmt(sub)))

            for t in itens:
                linhas.append("  {} · {}  `{}`".format(t["descricao"].capitalize(), fmt(float(t["valor"])), id_curto(t["id"])))

        linhas.append("\n  *Total: {}*".format(fmt(sai)))

    linhas.append("\n{} *Saldo: {}*".format(emoji, fmt_sinal(sal)))

    if limite > 0:
        porc = int((sai / limite) * 100)
        rest = limite - sai

        linhas.append(
            "\n*Limite: {}*\n[{}] {}%\n{} {}: {}".format(
                fmt(limite),
                barra(porc),
                porc,
                "✅" if rest >= 0 else "🚨",
                "Disponivel" if rest >= 0 else "Estourou",
                fmt(abs(rest))
            )
        )
    else:
        linhas.append("\n_limite 2000 para definir um teto mensal_")

    linhas.append("\n_apagar <ID>_  ·  _editar <ID> <valor>_")

    return "\n".join(linhas)


def relatorio_saldo(telefone):
    ini, fim = periodo_mes()
    txs = buscar_transacoes(telefone, ini, fim)
    ent, sai, sal = totais(txs)
    emoji = "🟢" if sal >= 0 else "🔴"

    return (
        "💰 *Saldo — {}*\n\n"
        "💚 Entradas:  {}\n"
        "🔴 Saidas:    {}\n"
        "━━━━━━━━━━━━━━\n"
        "{} *{}*"
    ).format(MESES[ini.month - 1], fmt(ent), fmt(sai), emoji, fmt_sinal(sal))


def relatorio_top(telefone, n=5):
    ini, fim = periodo_mes()
    txs = buscar_transacoes(telefone, ini, fim)

    saidas = sorted(
        [t for t in txs if t["tipo"] == "saida"],
        key=lambda x: -float(x["valor"])
    )

    if not saidas:
        return "Nenhuma saida registrada este mes."

    linhas = ["🏆 *Top {} maiores saidas do mes:*\n".format(min(n, len(saidas)))]

    for i, t in enumerate(saidas[:n], 1):
        linhas.append("{}. {} — {}  ({})".format(i, t["descricao"].capitalize(), fmt(float(t["valor"])), normalizar_categoria(t["categoria"])))

    return "\n".join(linhas)


def relatorio_posso_gastar(telefone):
    limite = buscar_limite(telefone)

    if limite <= 0:
        return "Voce ainda nao definiu um limite.\n\nManda: _limite 2000_"

    ini, fim = periodo_mes()
    txs = buscar_transacoes(telefone, ini, fim)

    _, sai, _ = totais(txs)

    rest = limite - sai
    dias = dias_restantes_mes()

    if rest <= 0:
        return (
            "🚨 *Limite estourado!*\n\n"
            "Passou {} do teto de {}.\n"
            "Segura os gastos ate o fim do mes! 💪"
        ).format(fmt(abs(rest)), fmt(limite))

    por_dia = rest / dias
    prazo = "ultimo dia do mes" if dias == 1 else "{} dias restantes".format(dias)

    return (
        "💰 *Voce ainda pode gastar:*\n\n"
        "*{}* ate o fim do mes\n"
        "_(aprox. {} por dia, {})_\n\n"
        "Baseado no seu limite de {}"
    ).format(fmt(rest), fmt(por_dia), prazo, fmt(limite))


def relatorio_hoje_resumo(telefone):
    ini, fim = periodo_hoje()
    txs = buscar_transacoes(telefone, ini, fim)
    ent, sai, sal = totais(txs)
    emoji = "🟢" if sal >= 0 else "🔴"
    hoje_str = hoje().strftime("%d/%m")

    if not txs:
        return "Hoje ({})\n\nNenhuma movimentacao ainda.".format(hoje_str)

    return (
        "📊 *Hoje ({})*\n\n"
        "💚 Entradas:  {}\n"
        "🔴 Saidas:    {}\n"
        "━━━━━━━━━━━━━━\n"
        "{} Saldo: {}"
    ).format(hoje_str, fmt(ent), fmt(sai), emoji, fmt_sinal(sal))


MSG_BEM_VINDO = (
    "Oi! 👋 Eu sou o *Cash Flow IA*, seu assistente financeiro aqui no WhatsApp.\n\n"
    "E simples: me manda o que gastou ou recebeu, e eu anoto tudo pra voce.\n\n"
    "*Exemplos rapidos:*\n"
    "• _mercado 87_ → saida\n"
    "• _uber 32_ → saida\n"
    "• _investimento 50_ → saida\n"
    "• _salario 2500_ → entrada\n"
    "• _pix recebido 300_ → entrada\n"
    "• _retorno investimento 200_ → entrada\n\n"
    "Pra ver seu extrato: _mes_, _hoje_ ou _saldo_ 📊\n\n"
    "Qual foi sua ultima movimentacao? 😊"
)


MSG_AJUDA = (
    "🤖 *Cash Flow IA — Comandos*\n\n"
    "📤 *Saida:*\n"
    "_mercado 85_ · _uber 32_ · _investimento 50_\n\n"
    "📥 *Entrada:*\n"
    "_salario 2500_ · _pix recebido 400_ · _retorno investimento 200_\n\n"
    "📊 *Relatorios:*\n"
    "_hoje_ · _semana_ · _mes_ · _saldo_ · _extrato completo_\n\n"
    "✏️ *Editar:*\n"
    "_editar ultimo 120_ · _editar ae3f06 150_\n\n"
    "🗑️ *Apagar:*\n"
    "_apagar ultimo_ · _apagar ae3f06_\n\n"
    "⚠️ *Limite:* _limite 2000_\n"
    "💡 *Dica:* _dica_"
)


DICAS = [
    "💡 *Regra 50/30/20:* 50% necessidades, 30% lazer, 20% poupar.",
    "💡 Pequenos gastos somam muito. Um cafe por dia pode virar R$ 100/mes.",
    "💡 Espera 24h antes de uma compra por impulso.",
    "💡 Define um limite: _limite 2000_. Eu aviso quando estiver chegando perto! 🔔",
    "💡 Revise assinaturas mensais. É comum pagar por algo que quase nao usa.",
    "💡 Quem anota os gastos tende a gastar menos. Voce ja esta no caminho! 💪",
]


SAUDACOES = [
    "👋 Oi! Registre um gasto ou mande _saldo_ pra ver como voce ta. 💸",
    "Oi! 🤖 Me manda algo tipo _mercado 80_ pra anotar, ou _resumo_ pra ver o dia.",
    "Ola! To aqui pra te ajudar com as financas. Qual foi a ultima movimentacao? 💰",
]


PALAVRAS_BEM_VINDO = {
    "oi", "ola", "olá", "hello", "hi", "hey",
    "inicio", "início", "start", "comecar", "começar", "menu"
}


# Palavras que nao identificam o lancamento em "apagar o ultimo gasto do mercado".
PALAVRAS_COMANDO = {
    "apagar", "apaga", "apague", "excluir", "exclui", "exclua", "deletar", "deleta", "delete",
    "remover", "remove", "remova", "editar", "edita", "edite", "alterar", "altera", "altere",
    "corrigir", "corrige", "corrija", "mudar", "muda", "mude", "trocar", "troca", "troque",
    "ultimo", "ultima", "last", "lancamento", "gasto", "registro", "valor", "entrada", "saida",
    "o", "a", "os", "as", "um", "uma", "do", "da", "dos", "das", "de", "no", "na", "para", "pra", "pro",
    "meu", "minha", "id",
}


def extrair_id(texto: str):
    # ID curto do extrato (6 hex). Numa frase, exige um digito: "decada" nao e ID.
    t = texto.strip().lower()
    if re.fullmatch(r"[a-f0-9]{6}", t):
        return t
    m = re.search(r"\b(?=[a-f]*\d)[a-f0-9]{6}\b", t)
    return m.group(0) if m else None


def termo_busca(texto: str) -> str:
    # "apagar o ultimo mercado 50" -> "mercado"
    t = re.sub(r"\b\d+(?:[.,]\d+)*\b", " ", sem_acento(texto).lower())
    return " ".join(p for p in re.findall(r"\w+", t) if p not in PALAVRAS_COMANDO)


def buscar_por_descricao(telefone, termo):
    # Lancamento mais recente do mes cuja descricao contem o termo.
    ini, fim = periodo_mes()
    txs = buscar_transacoes(telefone, ini, fim)
    matches = [t for t in txs if termo in sem_acento(t["descricao"]).lower()]
    return matches[-1] if matches else None


def extrair_mensagem(data: dict):
    try:
        if log.isEnabledFor(logging.DEBUG):
            log.debug("PAYLOAD RAW: %s", json.dumps(data, ensure_ascii=False)[:4000])

        if isinstance(data, dict) and data.get("base64"):
            decoded = base64.b64decode(data["base64"]).decode("utf-8")
            data = json.loads(decoded)
            if log.isEnabledFor(logging.DEBUG):
                log.debug("PAYLOAD BASE64 DECODIFICADO: %s", json.dumps(data, ensure_ascii=False)[:4000])

        event = str(data.get("event", "")).lower()
        log.debug("EVENTO: %s", event)

        if event and ("message" not in event and "messages" not in event):
            return None, None

        # Historico sincronizado ao reconectar o WhatsApp: mensagens antigas, nao registrar de novo.
        if event in ("messages.set", "messages_set"):
            return None, None

        msg_data = data.get("data", {})

        if isinstance(msg_data, list) and msg_data:
            msg_data = msg_data[0]

        if not isinstance(msg_data, dict):
            return None, None

        key = msg_data.get("key", {}) or {}

        if key.get("fromMe"):
            return None, None

        remote_jid = (
            key.get("remoteJid")
            or msg_data.get("remoteJid")
            or msg_data.get("jid")
            or ""
        )

        if not remote_jid or "g.us" in remote_jid:
            return None, None

        msg = msg_data.get("message", {}) or {}

        texto = (
            msg.get("conversation")
            or msg.get("extendedTextMessage", {}).get("text")
            or msg.get("imageMessage", {}).get("caption")
            or msg.get("videoMessage", {}).get("caption")
            or msg.get("ephemeralMessage", {}).get("message", {}).get("conversation")
            or msg.get("ephemeralMessage", {}).get("message", {}).get("extendedTextMessage", {}).get("text")
            or msg_data.get("messageText")
            or msg_data.get("text")
            or ""
        ).strip()

        if not texto:
            log.info("Mensagem sem texto reconhecido.")
            return None, None

        return remote_jid, texto

    except Exception as e:
        log.exception("Erro ao extrair mensagem: %s", e)
        return None, None


@app.route("/webhook", methods=["POST"])
def webhook():
    # Com WEBHOOK_SECRET definido, so aceita a URL configurada na Evolution: /webhook?token=SEGREDO
    if WEBHOOK_SECRET and not hmac.compare_digest(
        request.args.get("token", "").encode(), WEBHOOK_SECRET.encode()
    ):
        log.warning("Webhook recusado: token invalido ou ausente.")
        return "", 401

    data = request.get_json(silent=True) or {}

    telefone, mensagem = extrair_mensagem(data)

    if not telefone or not mensagem:
        return "", 200

    log.info("MSG de %s (%s caracteres)", mascarar(telefone), len(mensagem))
    log.debug("MSG %s: %s", telefone, mensagem)

    msg_limpa = mensagem.lower().strip()

    if msg_limpa in PALAVRAS_BEM_VINDO:
        enviar(telefone, MSG_BEM_VINDO)
        return "", 200

    try:
        r = atalho(mensagem) or chamar_ia(mensagem)

        intencao = r["intencao"]
        valor = r["valor"]
        tipo = r["tipo"]
        descricao = (r["descricao"] or mensagem).capitalize()
        categoria = r["categoria"]

        log.info("Intencao: %s", intencao)

        if intencao == "gasto":
            if valor <= 0:
                enviar(telefone, "🤖 Nao identifiquei o valor.\n\nTenta: _mercado 85_ ou _pix recebido 300_")
                return "", 200

            pk, short = salvar_transacao(telefone, descricao, valor, tipo, categoria)

            if tipo == "entrada":
                icone, label, item_e = "💚", "Entrada registrada", "💰"
            else:
                icone, label, item_e = "🔴", "Saida registrada", "🛒"

            msg = (
                "{} *{}*\n\n"
                "{} {}\n"
                "💵 {}\n"
                "🏷️ {}\n"
                "🔑 `{}`\n\n"
                "👉 _apagar ultimo_ · _editar ultimo {}_"
            ).format(icone, label, item_e, descricao, fmt(valor), categoria, short, int(valor))

            if tipo == "saida":
                limite = buscar_limite(telefone)
                if limite > 0:
                    ini, fim = periodo_mes()
                    txs = buscar_transacoes(telefone, ini, fim)
                    _, sai, _ = totais(txs)
                    msg += alerta_limite(sai, limite)

            enviar(telefone, msg)

        elif intencao == "resumo":
            enviar(telefone, relatorio_hoje_resumo(telefone))

        elif intencao == "hoje":
            ini, fim = periodo_hoje()
            txs = buscar_transacoes(telefone, ini, fim)
            label = ini.strftime("%d/%m/%Y")
            enviar(telefone, relatorio_resumo("Hoje — {}".format(label), txs))

        elif intencao == "semana":
            ini, fim = periodo_semana()
            txs = buscar_transacoes(telefone, ini, fim)
            enviar(
                telefone,
                relatorio_resumo(
                    "Esta semana ({} → {})".format(ini.strftime("%d/%m"), fim.strftime("%d/%m")),
                    txs
                )
            )

        elif intencao == "relatorio":
            ini, fim = periodo_mes()
            txs = buscar_transacoes(telefone, ini, fim)
            limite = buscar_limite(telefone)
            enviar(telefone, relatorio_resumo("{} {}".format(MESES[ini.month - 1], ini.year), txs, limite))

        elif intencao == "extrato":
            ini, fim = periodo_mes()
            txs = buscar_transacoes(telefone, ini, fim)
            limite = buscar_limite(telefone)
            enviar(
                telefone,
                relatorio_extrato(
                    "Extrato — {}".format(MESES[ini.month - 1]),
                    ini.strftime("%d/%m"),
                    fim.strftime("%d/%m/%Y"),
                    txs,
                    limite
                )
            )

        elif intencao == "saldo":
            enviar(telefone, relatorio_saldo(telefone))

        elif intencao == "top":
            enviar(telefone, relatorio_top(telefone))

        elif intencao == "posso_gastar":
            enviar(telefone, relatorio_posso_gastar(telefone))

        elif intencao == "limite":
            if valor > 0:
                salvar_limite(telefone, valor)

                ini, fim = periodo_mes()
                txs = buscar_transacoes(telefone, ini, fim)
                _, sai, _ = totais(txs)
                porc = int((sai / valor) * 100) if valor > 0 else 0

                enviar(
                    telefone,
                    "✅ *Limite definido: {}*\n\nSaidas este mes: {} ({}%)\n\nVou te avisar quando estiver chegando perto! 🔔".format(
                        fmt(valor),
                        fmt(sai),
                        porc
                    )
                )
            else:
                enviar(telefone, "🤖 Informe o valor. Ex: _limite 2000_")

        elif intencao == "editar":
            # Sem descricao da IA, usa a mensagem sem o valor novo do final ("editar mercado 120").
            alvo = r["descricao"] or re.sub(r"\s*\d+(?:[.,]\d+)?\s*$", "", mensagem)
            novo_val = valor if valor > 0 else None
            short_id = extrair_id(alvo)
            termo = "" if short_id else termo_busca(alvo)
            usar_ultimo = not short_id and not termo

            if termo:
                achado = buscar_por_descricao(telefone, termo)

                if not achado:
                    enviar(telefone, "🤖 Nao achei _{}_ este mes.\n\nUse o ID do extrato: _editar ae3f06 120_".format(termo))
                    return "", 200

                short_id = id_curto(achado["id"])

            if novo_val is None:
                enviar(telefone, "✏️ Qual o novo valor?\n\nEx: _editar ultimo 120_")
                return "", 200

            updated = editar_transacao(
                telefone,
                short_id,
                novo_valor=novo_val,
                nova_desc=None,
                usar_ultimo=usar_ultimo
            )

            if updated:
                enviar(
                    telefone,
                    "✏️ *Lancamento atualizado!*\n\n{} → {}\n🏷️ {} `{}`".format(
                        updated["descricao"].capitalize(),
                        fmt(float(updated["valor"])),
                        normalizar_categoria(updated["categoria"]),
                        id_curto(updated["id"])
                    )
                )
            else:
                enviar(telefone, "🤖 Nao encontrei esse lancamento. Confere o ID no extrato.")

        elif intencao == "apagar":
            usar_ultimo = bool(re.search(r"\b(ultimo|ultima|last)\b", sem_acento(mensagem).lower()))
            short_id = extrair_id(r["descricao"]) or extrair_id(mensagem)

            if not usar_ultimo and not short_id:
                # "apagar mercado" apaga o mercado, nunca outro lancamento no lugar.
                termo = termo_busca(r["descricao"] or mensagem)

                if termo:
                    achado = buscar_por_descricao(telefone, termo)

                    if not achado:
                        enviar(telefone, "🤖 Nao achei _{}_ este mes.\n\nUse o ID do extrato: _apagar ae3f06_".format(termo))
                        return "", 200

                    short_id = id_curto(achado["id"])
                else:
                    usar_ultimo = True

            deleted = apagar_transacao(telefone, short_id=short_id, usar_ultimo=usar_ultimo)

            if deleted:
                icone = "💚" if deleted["tipo"] == "entrada" else "🔴"
                ini, fim = periodo_mes()
                txs = buscar_transacoes(telefone, ini, fim)
                _, sai, _ = totais(txs)

                enviar(
                    telefone,
                    "🗑️ *Lancamento excluido!*\n\n{} — {} {}\n\nSaidas do mes: {}".format(
                        deleted["descricao"].capitalize(),
                        icone,
                        fmt(float(deleted["valor"])),
                        fmt(sai)
                    )
                )
            else:
                enviar(telefone, "🤖 Nao encontrei o lancamento. Confere o ID no extrato.")

        elif intencao == "confirmacao":
            val_str = fmt(valor) if valor > 0 else "esse valor"
            enviar(
                telefone,
                "Esse pix de {} foi *recebido* ou *enviado*?\n\n_pix recebido 100_ → entrada\n_pix enviado 100_ → saida 😊".format(val_str)
            )

        elif intencao == "duvida":
            enviar(
                telefone,
                "🤖 Parece que voce quer simular algo — ainda nao faco calculos.\n\nPra registrar um lancamento:\n_investimento 200_ · _salario 2500_ · _mercado 85_"
            )

        elif intencao == "dica":
            enviar(telefone, random.choice(DICAS))

        elif intencao == "oi":
            enviar(telefone, random.choice(SAUDACOES))

        elif intencao == "ajuda":
            enviar(telefone, MSG_AJUDA)

        elif intencao == "erro":
            enviar(telefone, "🤖 Tive um problema tecnico pra ler sua mensagem. Manda de novo em instantes!")

        else:
            enviar(
                telefone,
                "🤖 Nao entendi!\n\n_mercado 85_ → saida\n_salario 2500_ → entrada\n_mes_ → extrato · _ajuda_ → comandos"
            )

    except psycopg2.Error as e:
        log.exception("Erro banco: %s", e)
        enviar(telefone, "Tivemos um problema tecnico no banco. Tenta de novo em instantes.")

    except Exception as e:
        log.exception("Erro inesperado: %s", e)
        enviar(telefone, "🤖 Ops, algo deu errado. Tenta de novo em instantes.")

    return "", 200


@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "app": "Cash Flow IA", "version": "3.3"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
