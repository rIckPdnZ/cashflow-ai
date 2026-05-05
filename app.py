"""
╔══════════════════════════════════════════════════════════════╗
║              Cash Flow IA — WhatsApp Bot v3.1                ║
║         Flask · Evolution API · Groq · PostgreSQL            ║
╠══════════════════════════════════════════════════════════════╣
║  Variáveis de ambiente obrigatórias:                         ║
║    DATABASE_URL       → postgresql://user:pass@host/db       ║
║    GROQ_API_KEY       → sua chave Groq                       ║
║    EVOLUTION_URL      → http://SEU-IP:8080                   ║
║    EVOLUTION_KEY      → apikey configurada no .env           ║
║    EVOLUTION_INSTANCE → nome da instância (ex: cashflow)     ║
║    PORT               → (opcional) padrão 5000               ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import re
import json
import random
import hashlib
import logging
import requests
from datetime import date, timedelta
from calendar import monthrange

import psycopg2
import psycopg2.extras
from flask import Flask, request
from groq import Groq

# ═══════════════════════════════════════════════════════════════
#  SETUP
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("cashflow")

app = Flask(__name__)
groq_client   = Groq(api_key=os.environ["GROQ_API_KEY"])
DATABASE_URL  = os.environ["DATABASE_URL"]

# Evolution API
EVOLUTION_URL      = os.environ["EVOLUTION_URL"].rstrip("/")
EVOLUTION_KEY      = os.environ["EVOLUTION_KEY"]
EVOLUTION_INSTANCE = os.environ["EVOLUTION_INSTANCE"]


# ═══════════════════════════════════════════════════════════════
#  ENVIO DE MENSAGEM — Evolution API
# ═══════════════════════════════════════════════════════════════

def enviar(telefone: str, texto: str):
    """
    Envia mensagem de texto via Evolution API.
    telefone: formato JID — 5548999990000@s.whatsapp.net
    """
    url = "{}/message/sendText/{}".format(EVOLUTION_URL, EVOLUTION_INSTANCE)
    payload = {
        "number": telefone,
        "text":   texto
    }
    headers = {
        "Content-Type": "application/json",
        "apikey":       EVOLUTION_KEY
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Falha ao enviar mensagem para %s: %s", telefone, e)


# ═══════════════════════════════════════════════════════════════
#  FORMATACAO BRASILEIRA
# ═══════════════════════════════════════════════════════════════

def fmt(valor: float) -> str:
    """R$ 1.234,56"""
    return "R$ {:,.2f}".format(valor).replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_sinal(valor: float) -> str:
    """R$ +1.234,56 ou R$ -800,00"""
    sinal = "+" if valor >= 0 else ""
    return "R$ {}{:,.2f}".format(sinal, valor).replace(",", "X").replace(".", ",").replace("X", ".")


def dias_restantes_mes() -> int:
    hoje  = date.today()
    ultimo = monthrange(hoje.year, hoje.month)[1]
    return ultimo - hoje.day


# ═══════════════════════════════════════════════════════════════
#  BANCO — PostgreSQL / Supabase
# ═══════════════════════════════════════════════════════════════

def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transacoes (
                id          SERIAL PRIMARY KEY,
                telefone    TEXT            NOT NULL,
                descricao   TEXT            NOT NULL,
                valor       NUMERIC(12,2)   NOT NULL,
                tipo        TEXT            NOT NULL CHECK (tipo IN ('entrada','saida')),
                categoria   TEXT            NOT NULL DEFAULT 'Outros',
                data        DATE            NOT NULL DEFAULT CURRENT_DATE,
                criado_em   TIMESTAMPTZ     NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS limites (
                telefone      TEXT          PRIMARY KEY,
                limite_mensal NUMERIC(12,2) NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_transacoes_tel_data
            ON transacoes (telefone, data)
        """)
    conn.commit()
    return conn


def id_curto(pk: int) -> str:
    return hashlib.md5(str(pk).encode()).hexdigest()[:6]


# ── CRUD ────────────────────────────────────────────────────────

def salvar_transacao(telefone, descricao, valor, tipo, categoria):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO transacoes (telefone,descricao,valor,tipo,categoria,data) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (telefone, descricao, valor, tipo, categoria, date.today())
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
                    "SELECT id,descricao,valor,tipo,categoria FROM transacoes "
                    "WHERE telefone=%s ORDER BY id DESC LIMIT 1",
                    (telefone,)
                )
                row = cur.fetchone()
            else:
                cur.execute(
                    "SELECT id,descricao,valor,tipo,categoria FROM transacoes "
                    "WHERE telefone=%s", (telefone,)
                )
                rows = cur.fetchall()
                row  = next(
                    (r for r in rows if id_curto(r["id"]) == short_id.lower()),
                    None
                )

            if not row:
                return None

            updates, params = [], []
            if novo_valor and novo_valor > 0:
                updates.append("valor=%s");    params.append(novo_valor)
            if nova_desc:
                updates.append("descricao=%s"); params.append(nova_desc.capitalize())
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
                    "SELECT id,descricao,valor,tipo FROM transacoes "
                    "WHERE telefone=%s ORDER BY id DESC LIMIT 1",
                    (telefone,)
                )
                row = cur.fetchone()
            else:
                cur.execute(
                    "SELECT id,descricao,valor,tipo FROM transacoes WHERE telefone=%s",
                    (telefone,)
                )
                rows = cur.fetchall()
                row  = next(
                    (r for r in rows if id_curto(r["id"]) == short_id.lower()),
                    None
                )

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
                "SELECT id,descricao,valor,tipo,categoria,data FROM transacoes "
                "WHERE telefone=%s AND data BETWEEN %s AND %s ORDER BY data,id",
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
                "INSERT INTO limites (telefone,limite_mensal) VALUES (%s,%s) "
                "ON CONFLICT (telefone) DO UPDATE SET limite_mensal=EXCLUDED.limite_mensal",
                (telefone, valor)
            )
        conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
#  PERIODOS
# ═══════════════════════════════════════════════════════════════

def periodo_hoje():   h = date.today(); return h, h
def periodo_semana(): h = date.today(); return h - timedelta(days=h.weekday()), h
def periodo_mes():    h = date.today(); return h.replace(day=1), h

MESES = ["Janeiro","Fevereiro","Marco","Abril","Maio","Junho",
         "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]


# ═══════════════════════════════════════════════════════════════
#  IA — GROQ / LLaMA
# ═══════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """Voce e o Cash Flow IA, assistente financeiro no WhatsApp. Responda APENAS com JSON valido, sem texto extra e sem markdown.

Formato exato:
{"intencao":"...","descricao":"...","valor":0.0,"tipo":"entrada|saida","categoria":"..."}

INTENCOES:
gasto        -> registrar transacao (entrada ou saida)
resumo       -> "resumo", "hoje", "como to", "balanco rapido"
extrato      -> "extrato", "extrato completo", "detalhado", "listar tudo"
relatorio    -> "mes", "relatorio", "quanto gastei"
semana       -> "semana", "essa semana"
saldo        -> "saldo", "quanto tenho", "balanco"
top          -> "top gastos", "maiores gastos", "onde gastei mais"
posso_gastar -> "quanto posso gastar", "quanto ainda posso", "quanto sobrou"
limite       -> "limite 2000", "meta 1500", "teto 3000"
editar       -> "editar", "corrigir", "mudar", "alterar"
apagar       -> "apagar", "excluir", "deletar", "remover", "errei", "desfazer"
dica         -> "dica", "conselho", "como economizar"
ajuda        -> "ajuda", "help", "comandos"
oi           -> saudacoes sem numero
confirmacao  -> "pix" sozinho sem contexto claro
duvida       -> intencao de calcular/simular — NAO registrar
outro        -> qualquer outra coisa

TIPO:
ENTRADA: salario, pagamento recebido, freela, pix recebido, transferencia recebida,
  retorno investimento, rendimento, lucro, ganhei, recebi, dividendo, venda, vendi
SAIDA: compras, despesas, contas, servicos, assinaturas, pix enviado,
  investimento, investi, apliquei, aporte

AMBIGUO: "pix 100" sem contexto -> {"intencao":"confirmacao","valor":100.0}
NAO REGISTRAR: calcular, simular, prever, quanto ficaria, se eu gastar -> duvida

EDITAR:
  "editar ultimo 120"  -> {"intencao":"editar","descricao":"ultimo","valor":120.0}
  "editar ae3f06 150"  -> {"intencao":"editar","descricao":"ae3f06","valor":150.0}

APAGAR:
  "apagar ultimo"      -> {"intencao":"apagar","descricao":"ultimo"}
  "excluir ae3f06"     -> {"intencao":"apagar","descricao":"ae3f06"}

CATEGORIAS:
  Saida:   Alimentacao|Transporte|Lazer|Saude|Moradia|Educacao|Beleza e Cuidados|Roupas|Servicos|Investimentos|Outros
  Entrada: Salario|Freela|Investimentos|Vendas|Transferencia|Outros

SOMENTE JSON."""


def chamar_ia(mensagem: str) -> dict:
    try:
        res = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": mensagem}
            ],
            temperature=0.15,
            max_tokens=200
        )
        raw   = res.choices[0].message.content.strip()
        raw   = re.sub(r"```json|```", "", raw).strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            raise ValueError("Sem JSON na resposta: {!r}".format(raw))
        data  = json.loads(match.group())
        data.setdefault("intencao",  "outro")
        data.setdefault("descricao", mensagem)
        data.setdefault("valor",     0.0)
        data.setdefault("tipo",      "saida")
        data.setdefault("categoria", "Outros")
        return data
    except json.JSONDecodeError as e:
        raise ValueError("JSON invalido da IA: {}".format(e))


# ═══════════════════════════════════════════════════════════════
#  RELATORIOS
# ═══════════════════════════════════════════════════════════════

EMOJI_CAT = {
    "alimentacao":"🍔","alimentação":"🍔","transporte":"🚗","lazer":"🎮",
    "saude":"💊","saúde":"💊","moradia":"🏠","educacao":"📚","educação":"📚",
    "roupas":"👕","beleza e cuidados":"💅","servicos":"🔧","serviços":"🔧",
    "investimentos":"📈","salario":"💼","salário":"💼","freela":"💻",
    "vendas":"🛍️","transferencia":"📲","transferência":"📲","outros":"📦",
}

def ecat(cat): return EMOJI_CAT.get(cat.lower(), "📦")

def barra(porc, n=10):
    c = min(porc * n // 100, n)
    return "█" * c + "░" * (n - c)

def totais(txs):
    ent = sum(float(t["valor"]) for t in txs if t["tipo"] == "entrada")
    sai = sum(float(t["valor"]) for t in txs if t["tipo"] == "saida")
    return ent, sai, ent - sai

def alerta_limite(sai, limite):
    if limite <= 0: return ""
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
    top   = sorted([t for t in txs if t["tipo"] == "saida"],
                   key=lambda x: -float(x["valor"]))[:3]
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
            linhas.append("{}. {} — {}".format(
                i, t["descricao"].capitalize(), fmt(float(t["valor"]))))
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
    cats  = {}
    for t in txs:
        if t["tipo"] == "saida":
            cats.setdefault(t["categoria"], []).append(t)
    linhas = [
        "🧾 *{}*".format(titulo),
        "_{} → {}_\n".format(label_ini, label_fim),
    ]
    entradas = [t for t in txs if t["tipo"] == "entrada"]
    if entradas:
        linhas.append("💚 *Entradas*")
        for t in entradas:
            linhas.append("  {} · {}  `{}`".format(
                t["descricao"].capitalize(), fmt(float(t["valor"])), id_curto(t["id"])))
        linhas.append("  *Total: {}*\n".format(fmt(ent)))
    if cats:
        linhas.append("🔴 *Saidas*")
        for cat, itens in sorted(cats.items(), key=lambda x: -sum(float(i["valor"]) for i in x[1])):
            sub = sum(float(i["valor"]) for i in itens)
            linhas.append("\n{} *{}* — {}".format(ecat(cat), cat, fmt(sub)))
            for t in itens:
                linhas.append("  {} · {}  `{}`".format(
                    t["descricao"].capitalize(), fmt(float(t["valor"])), id_curto(t["id"])))
        linhas.append("\n  *Total: {}*".format(fmt(sai)))
    linhas.append("\n{} *Saldo: {}*".format(emoji, fmt_sinal(sal)))
    if limite > 0:
        porc = int((sai / limite) * 100)
        rest = limite - sai
        linhas.append("\n*Limite: {}*\n[{}] {}%\n{} {}: {}".format(
            fmt(limite), barra(porc), porc,
            "✅" if rest >= 0 else "🚨",
            "Disponivel" if rest >= 0 else "Estourou",
            fmt(abs(rest))
        ))
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
    txs  = buscar_transacoes(telefone, ini, fim)
    saidas = sorted([t for t in txs if t["tipo"] == "saida"],
                    key=lambda x: -float(x["valor"]))
    if not saidas:
        return "Nenhuma saida registrada este mes."
    linhas = ["🏆 *Top {} maiores saidas do mes:*\n".format(min(n, len(saidas)))]
    for i, t in enumerate(saidas[:n], 1):
        linhas.append("{}. {} — {}  ({})".format(
            i, t["descricao"].capitalize(), fmt(float(t["valor"])), t["categoria"]))
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
    por_dia = rest / dias if dias > 0 else rest
    return (
        "💰 *Voce ainda pode gastar:*\n\n"
        "*{}* ate o fim do mes\n"
        "_(aprox. {} por dia, {} dias restantes)_\n\n"
        "Baseado no seu limite de {}"
    ).format(fmt(rest), fmt(por_dia), dias, fmt(limite))


def relatorio_hoje_resumo(telefone):
    ini, fim = periodo_hoje()
    txs = buscar_transacoes(telefone, ini, fim)
    ent, sai, sal = totais(txs)
    emoji = "🟢" if sal >= 0 else "🔴"
    hoje_str = date.today().strftime("%d/%m")
    if not txs:
        return "Hoje ({})\n\nNenhuma movimentacao ainda.".format(hoje_str)
    return (
        "📊 *Hoje ({})*\n\n"
        "💚 Entradas:  {}\n"
        "🔴 Saidas:    {}\n"
        "━━━━━━━━━━━━━━\n"
        "{} Saldo: {}"
    ).format(hoje_str, fmt(ent), fmt(sai), emoji, fmt_sinal(sal))


# ═══════════════════════════════════════════════════════════════
#  MENSAGENS FIXAS
# ═══════════════════════════════════════════════════════════════

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
    "━━━━━━━━━━━━━━━━━━\n"
    "📤 *Saida:*\n"
    "  _mercado 85_ · _uber 32_ · _investimento 50_\n\n"
    "📥 *Entrada:*\n"
    "  _salario 2500_ · _pix recebido 400_\n"
    "  _retorno investimento 200_\n\n"
    "📊 *Relatorios:*\n"
    "  _resumo_ → hoje rapido\n"
    "  _hoje_ · _semana_ · _mes_ · _saldo_\n"
    "  _extrato completo_ → todos os lancamentos\n"
    "  _top gastos_ · _quanto posso gastar_\n\n"
    "✏️ *Editar:*\n"
    "  _editar ultimo 120_\n"
    "  _editar ae3f06 150_\n\n"
    "🗑️ *Apagar:*\n"
    "  _apagar ultimo_ · _apagar ae3f06_\n\n"
    "⚠️ *Limite mensal:* _limite 2000_\n"
    "💡 *Dica:* _dica_\n"
    "━━━━━━━━━━━━━━━━━━"
)

DICAS = [
    "💡 *Regra 50/30/20:* 50% necessidades, 30% lazer, 20% poupar. Simples e funciona!",
    "💡 Pequenos gastos somam muito. Um cafe por dia pode virar R$ 100/mes.",
    "💡 Espera 24h antes de uma compra por impulso. Se ainda quiser depois, ta liberado.",
    "💡 Define um limite: _limite 2000_. Eu aviso quando estiver chegando perto! 🔔",
    "💡 Revise assinaturas mensais — e comum pagar por algo que quase nao usa.",
    "💡 Quem anota os gastos tende a gastar ate 20% menos. Voce ja esta no caminho! 💪",
]

SAUDACOES = [
    "👋 Oi! Registre um gasto ou mande _saldo_ pra ver como voce ta. 💸",
    "Oi! 🤖 Me manda algo tipo _mercado 80_ pra anotar, ou _resumo_ pra ver o dia.",
    "Ola! To aqui pra te ajudar com as financas. Qual foi a ultima movimentacao? 💰",
]

PALAVRAS_BEM_VINDO = {
    "oi","ola","olá","hello","hi","hey",
    "inicio","início","start","comecar","começar","menu"
}


# ═══════════════════════════════════════════════════════════════
#  EXTRAI MENSAGEM DO PAYLOAD DA EVOLUTION API
# ═══════════════════════════════════════════════════════════════

def extrair_mensagem(data: dict):
    """
    A Evolution API envia diferentes estruturas dependendo do tipo de mensagem.
    Retorna (telefone, texto) ou (None, None) se nao for mensagem de texto.
    """
    try:
        # Ignora mensagens enviadas pelo proprio bot
        if data.get("data", {}).get("key", {}).get("fromMe"):
            return None, None

        # Ignora eventos que nao sao mensagens (status, grupos, etc.)
        event = data.get("event", "")
        if event not in ("messages.upsert", "MESSAGES_UPSERT"):
            return None, None

        msg_data = data.get("data", {})

        # Telefone: remove o sufixo de grupo/broadcast, pega so o numero
        remote_jid = msg_data.get("key", {}).get("remoteJid", "")
        if not remote_jid or "g.us" in remote_jid:
            # Ignora mensagens de grupo
            return None, None
        telefone = remote_jid  # ex: 5548999990000@s.whatsapp.net

        # Texto: tenta conversation primeiro, depois extendedTextMessage
        msg = msg_data.get("message", {})
        texto = (
            msg.get("conversation")
            or msg.get("extendedTextMessage", {}).get("text")
            or ""
        ).strip()

        if not texto:
            return None, None

        return telefone, texto

    except Exception as e:
        log.error("Erro ao extrair mensagem: %s | payload: %s", e, data)
        return None, None


# ═══════════════════════════════════════════════════════════════
#  WEBHOOK — recebe mensagens da Evolution API
# ═══════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    # Evolution API sempre espera HTTP 200 imediatamente
    data = request.get_json(silent=True) or {}

    telefone, mensagem = extrair_mensagem(data)

    # Mensagem invalida, de grupo ou do proprio bot → ignora silenciosamente
    if not telefone or not mensagem:
        return "", 200

    log.info("MSG %s: %s", telefone[-10:], mensagem)

    # ── Boas-vindas ───────────────────────────────────────────────
    if mensagem.lower().strip() in PALAVRAS_BEM_VINDO:
        enviar(telefone, MSG_BEM_VINDO)
        return "", 200

    # ── Chama IA ──────────────────────────────────────────────────
    try:
        r = chamar_ia(mensagem)
    except Exception as e:
        log.error("Erro IA: %s", e)
        enviar(telefone,
            "🤖 Nao entendi sua mensagem.\n\n"
            "Tenta: _mercado 85_ · _salario 2500_ · _ajuda_"
        )
        return "", 200

    intencao  = r.get("intencao", "outro")
    valor     = float(r.get("valor") or 0)
    tipo      = r.get("tipo", "saida").strip().lower()
    descricao = r.get("descricao", mensagem).strip().capitalize()
    categoria = r.get("categoria", "Outros").strip()

    try:

        # ── REGISTRAR TRANSACAO ────────────────────────────────────
        if intencao == "gasto":
            if valor <= 0:
                enviar(telefone,
                    "🤖 Nao identifiquei o valor!\n\n"
                    "Tenta: _mercado 85_ ou _pix recebido 300_"
                )
                return "", 200

            pk, short = salvar_transacao(telefone, descricao, valor, tipo, categoria)
            icone, label, item_e = ("💚","Entrada registrada","💰") if tipo == "entrada" \
                               else ("🔴","Saida registrada","🛒")

            msg = (
                "{} *{}*\n\n"
                "{} {}\n"
                "💵 {}\n"
                "🏷️ {}\n"
                "🔑 `{}`\n\n"
                "👉 _apagar ultimo_  ·  _editar ultimo {}_"
            ).format(icone, label, item_e, descricao,
                     fmt(valor), categoria, short, int(valor))

            if tipo == "saida":
                limite = buscar_limite(telefone)
                if limite > 0:
                    ini, fim = periodo_mes()
                    txs = buscar_transacoes(telefone, ini, fim)
                    _, sai, _ = totais(txs)
                    msg += alerta_limite(sai, limite)

            enviar(telefone, msg)

        # ── RESUMO RAPIDO ──────────────────────────────────────────
        elif intencao == "resumo":
            enviar(telefone, relatorio_hoje_resumo(telefone))

        # ── HOJE ──────────────────────────────────────────────────
        elif intencao == "hoje":
            ini, fim = periodo_hoje()
            txs   = buscar_transacoes(telefone, ini, fim)
            label = date.today().strftime("%d/%m/%Y")
            enviar(telefone, relatorio_resumo("Hoje — {}".format(label), txs))

        # ── SEMANA ────────────────────────────────────────────────
        elif intencao == "semana":
            ini, fim = periodo_semana()
            txs = buscar_transacoes(telefone, ini, fim)
            enviar(telefone, relatorio_resumo(
                "Esta semana ({} → {})".format(
                    ini.strftime("%d/%m"), fim.strftime("%d/%m")),
                txs
            ))

        # ── MES / RELATORIO ────────────────────────────────────────
        elif intencao == "relatorio":
            ini, fim = periodo_mes()
            txs    = buscar_transacoes(telefone, ini, fim)
            limite = buscar_limite(telefone)
            enviar(telefone, relatorio_resumo(
                "{} {}".format(MESES[ini.month - 1], ini.year), txs, limite))

        # ── EXTRATO COMPLETO ──────────────────────────────────────
        elif intencao == "extrato":
            ini, fim = periodo_mes()
            txs    = buscar_transacoes(telefone, ini, fim)
            limite = buscar_limite(telefone)
            enviar(telefone, relatorio_extrato(
                "Extrato — {}".format(MESES[ini.month - 1]),
                ini.strftime("%d/%m"), fim.strftime("%d/%m/%Y"),
                txs, limite
            ))

        # ── SALDO ─────────────────────────────────────────────────
        elif intencao == "saldo":
            enviar(telefone, relatorio_saldo(telefone))

        # ── TOP GASTOS ────────────────────────────────────────────
        elif intencao == "top":
            enviar(telefone, relatorio_top(telefone))

        # ── QUANTO POSSO GASTAR ───────────────────────────────────
        elif intencao == "posso_gastar":
            enviar(telefone, relatorio_posso_gastar(telefone))

        # ── LIMITE ────────────────────────────────────────────────
        elif intencao == "limite":
            if valor > 0:
                salvar_limite(telefone, valor)
                ini, fim = periodo_mes()
                txs = buscar_transacoes(telefone, ini, fim)
                _, sai, _ = totais(txs)
                porc = int((sai / valor) * 100) if valor > 0 else 0
                enviar(telefone,
                    "✅ *Limite definido: {}*\n\n"
                    "Saidas este mes: {} ({}%)\n\n"
                    "Vou te avisar quando estiver chegando perto! 🔔"
                    .format(fmt(valor), fmt(sai), porc)
                )
            else:
                enviar(telefone, "🤖 Informe o valor. Ex: _limite 2000_")

        # ── EDITAR ────────────────────────────────────────────────
        elif intencao == "editar":
            desc_raw    = r.get("descricao", "").strip().lower()
            novo_val    = valor if valor > 0 else None
            usar_ultimo = desc_raw in ("ultimo", "último", "")
            short_id    = None

            if not usar_ultimo:
                if re.match(r'^[a-f0-9]{6}$', desc_raw):
                    short_id = desc_raw
                else:
                    ini, fim = periodo_mes()
                    txs      = buscar_transacoes(telefone, ini, fim)
                    matches  = [t for t in txs if desc_raw in t["descricao"].lower()]
                    if matches:
                        short_id = id_curto(matches[-1]["id"])
                    else:
                        enviar(telefone,
                            "🤖 Nao achei _{}_  este mes.\n\n"
                            "Use o ID do extrato: _editar ae3f06 120_".format(desc_raw)
                        )
                        return "", 200

            if novo_val is None:
                enviar(telefone, "✏️ Qual o novo valor?\n\nEx: _editar ultimo 120_")
                return "", 200

            updated = editar_transacao(
                telefone, short_id,
                novo_valor=novo_val, nova_desc=None,
                usar_ultimo=usar_ultimo
            )
            if updated:
                enviar(telefone,
                    "✏️ *Lancamento atualizado!*\n\n"
                    "{} → {}\n🏷️ {}  `{}`"
                    .format(
                        updated["descricao"].capitalize(),
                        fmt(float(updated["valor"])),
                        updated["categoria"],
                        id_curto(updated["id"])
                    )
                )
            else:
                enviar(telefone, "🤖 Nao encontrei esse lancamento. Confere o ID no extrato.")

        # ── APAGAR ────────────────────────────────────────────────
        elif intencao == "apagar":
            msg_lower   = mensagem.lower()
            usar_ultimo = any(p in msg_lower for p in ("ultimo","último","last"))
            hex_match   = re.search(r'\b([a-f0-9]{6})\b', msg_lower)
            short_id    = hex_match.group(1) if hex_match else None
            if not usar_ultimo and not short_id:
                usar_ultimo = True

            deleted = apagar_transacao(telefone, short_id=short_id, usar_ultimo=usar_ultimo)
            if deleted:
                icone = "💚" if deleted["tipo"] == "entrada" else "🔴"
                ini, fim = periodo_mes()
                txs = buscar_transacoes(telefone, ini, fim)
                _, sai, _ = totais(txs)
                enviar(telefone,
                    "🗑️ *Lancamento excluido!*\n\n"
                    "{} — {} {}\n\nSaidas do mes: {}"
                    .format(
                        deleted["descricao"].capitalize(),
                        icone, fmt(float(deleted["valor"])),
                        fmt(sai)
                    )
                )
            else:
                enviar(telefone, "🤖 Nao encontrei o lancamento. Confere o ID no extrato.")

        # ── PIX AMBIGUO ───────────────────────────────────────────
        elif intencao == "confirmacao":
            val_str = fmt(valor) if valor > 0 else "esse valor"
            enviar(telefone,
                "Esse pix de {} foi *recebido* ou *enviado*?\n\n"
                "_pix recebido 100_ → entrada\n"
                "_pix enviado 100_ → saida 😊".format(val_str)
            )

        # ── DUVIDA / SIMULACAO ────────────────────────────────────
        elif intencao == "duvida":
            enviar(telefone,
                "🤖 Parece que voce quer simular algo — ainda nao faco calculos.\n\n"
                "Pra registrar um lancamento:\n"
                "_investimento 200_ · _salario 2500_ · _mercado 85_"
            )

        # ── DICA ──────────────────────────────────────────────────
        elif intencao == "dica":
            enviar(telefone, random.choice(DICAS))

        # ── OI ────────────────────────────────────────────────────
        elif intencao == "oi":
            enviar(telefone, random.choice(SAUDACOES))

        # ── AJUDA ─────────────────────────────────────────────────
        elif intencao == "ajuda":
            enviar(telefone, MSG_AJUDA)

        # ── FALLBACK ──────────────────────────────────────────────
        else:
            enviar(telefone,
                "🤖 Nao entendi!\n\n"
                "_mercado 85_ → saida\n"
                "_salario 2500_ → entrada\n"
                "_mes_ → extrato  ·  _ajuda_ → comandos"
            )

    except psycopg2.Error as e:
        log.exception("Erro banco: %s", e)
        enviar(telefone,
            "Tivemos um problema tecnico. Tenta de novo em instantes.\n\nSe persistir, mande _ajuda_."
        )
    except Exception as e:
        log.exception("Erro inesperado: %s", e)
        enviar(telefone, "🤖 Ops, algo deu errado! Tenta de novo em instantes.")

    # Evolution API sempre espera 200
    return "", 200


# ─── HEALTH CHECK ──────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "app": "Cash Flow IA", "version": "3.1"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
