"""
╔══════════════════════════════════════════════════════╗
║           Cash Flow IA — WhatsApp Bot                ║
║     Flask + Twilio + Groq + PostgreSQL/Supabase      ║
╚══════════════════════════════════════════════════════╝

Variáveis de ambiente necessárias:
  DATABASE_URL   → postgresql://user:pass@host:5432/dbname
  GROQ_API_KEY   → sua chave da API Groq
  PORT           → (opcional) porta do servidor, padrão 5000
"""

import os
import json
import re
import random
import hashlib
import logging
from datetime import datetime, date, timedelta

import psycopg2
import psycopg2.extras
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from groq import Groq

# ─── SETUP ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("cashflow")

app = Flask(__name__)

groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
DATABASE_URL = os.environ["DATABASE_URL"]

# ─── BANCO — PostgreSQL / Supabase ─────────────────────────────────────────────

def get_conn():
    """Abre conexão com o PostgreSQL e garante que as tabelas existam."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transacoes (
                id          SERIAL PRIMARY KEY,
                telefone    TEXT        NOT NULL,
                descricao   TEXT        NOT NULL,
                valor       NUMERIC(12,2) NOT NULL,
                tipo        TEXT        NOT NULL CHECK (tipo IN ('entrada','saida')),
                categoria   TEXT        NOT NULL DEFAULT 'Outros',
                data        DATE        NOT NULL,
                criado_em   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS limites (
                telefone        TEXT PRIMARY KEY,
                limite_mensal   NUMERIC(12,2) NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_transacoes_telefone_data
            ON transacoes (telefone, data)
        """)
    conn.commit()
    return conn


def id_curto(pk: int) -> str:
    """Hash MD5 curto de 6 chars baseado no PK da tabela."""
    return hashlib.md5(str(pk).encode()).hexdigest()[:6]


def salvar_transacao(telefone: str, descricao: str, valor: float,
                     tipo: str, categoria: str) -> tuple[int, str]:
    """Insere transação e retorna (pk, id_curto)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transacoes (telefone, descricao, valor, tipo, categoria, data)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (telefone, descricao, valor, tipo, categoria, date.today())
            )
            pk = cur.fetchone()["id"]
        conn.commit()
        return pk, id_curto(pk)
    finally:
        conn.close()


def apagar_por_id_curto(telefone: str, short_id: str) -> tuple[str | None, float | None, str | None]:
    """Apaga uma transação pelo id curto. Retorna (descricao, valor, tipo) ou (None, None, None)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, descricao, valor, tipo FROM transacoes WHERE telefone = %s",
                (telefone,)
            )
            rows = cur.fetchall()
            for row in rows:
                if id_curto(row["id"]) == short_id.lower():
                    cur.execute("DELETE FROM transacoes WHERE id = %s", (row["id"],))
                    conn.commit()
                    return row["descricao"], float(row["valor"]), row["tipo"]
        return None, None, None
    finally:
        conn.close()


def apagar_ultimo(telefone: str) -> tuple[str | None, float | None, str | None]:
    """Apaga o último registro do usuário. Retorna (descricao, valor, tipo)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, descricao, valor, tipo FROM transacoes WHERE telefone = %s ORDER BY id DESC LIMIT 1",
                (telefone,)
            )
            row = cur.fetchone()
            if row:
                cur.execute("DELETE FROM transacoes WHERE id = %s", (row["id"],))
                conn.commit()
                return row["descricao"], float(row["valor"]), row["tipo"]
        return None, None, None
    finally:
        conn.close()


def buscar_transacoes(telefone: str, data_ini: date, data_fim: date) -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, descricao, valor, tipo, categoria, data
                FROM transacoes
                WHERE telefone = %s AND data BETWEEN %s AND %s
                ORDER BY data, id
                """,
                (telefone, data_ini, data_fim)
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.close()


def buscar_limite(telefone: str) -> float:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT limite_mensal FROM limites WHERE telefone = %s", (telefone,))
            row = cur.fetchone()
            return float(row["limite_mensal"]) if row else 0.0
    finally:
        conn.close()


def salvar_limite(telefone: str, valor: float):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO limites (telefone, limite_mensal) VALUES (%s, %s) ON CONFLICT (telefone) DO UPDATE SET limite_mensal = EXCLUDED.limite_mensal",
                (telefone, valor)
            )
        conn.commit()
    finally:
        conn.close()


# ─── HELPERS DE PERÍODO ────────────────────────────────────────────────────────

def periodo_hoje() -> tuple[date, date]:
    hoje = date.today()
    return hoje, hoje


def periodo_semana() -> tuple[date, date]:
    hoje = date.today()
    return hoje - timedelta(days=hoje.weekday()), hoje


def periodo_mes() -> tuple[date, date]:
    hoje = date.today()
    return hoje.replace(day=1), hoje


def nome_mes(mes: int) -> str:
    nomes = ["Janeiro","Fevereiro","Março","Abril","Maio","Junho",
             "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]
    return nomes[mes - 1]


# ─── IA ────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é o assistente financeiro Cash Flow IA no WhatsApp. Responda APENAS com JSON válido, sem texto extra, sem markdown.

Formato exato:
{"intencao": "...", "descricao": "...", "valor": 0.0, "tipo": "entrada|saida", "categoria": "..."}

Valores de intencao permitidos:
  gasto       → qualquer transação financeira (entrada ou saída)
  relatorio   → "relatório", "resumo", "extrato", "quanto gastei"
  hoje        → "hoje", "gastos hoje", "o que gastei hoje"
  semana      → "semana", "essa semana", "esta semana"
  saldo       → "saldo", "quanto tenho", "balanço"
  top         → "top gastos", "maiores gastos", "onde gastei mais"
  limite      → "limite 2000", "meta 1500"
  apagar      → "apagar", "deletar", "errei", "desfazer"
  dica        → "dica", "conselho", "como economizar"
  ajuda       → "ajuda", "help", "comandos", "o que você faz"
  oi          → saudações sem número
  outro       → qualquer coisa que não se encaixe

Regras de tipo:
  saida  → compras, gastos, despesas, pagamentos, contas, serviços
  entrada → salário, freela, pix recebido, transferência recebida, renda, pagamento recebido, venda

Categorias para saida:
  Alimentação | Transporte | Lazer | Saúde | Moradia | Educação | Beleza e Cuidados | Roupas | Serviços | Outros

Categorias para entrada:
  Salário | Freela | Vendas | Transferência | Outros

Exemplos:
  "mercado 87"          → saida, Alimentação
  "uber 32"             → saida, Transporte
  "ifood 45"            → saida, Alimentação
  "farmácia 60"         → saida, Saúde
  "netflix 55"          → saida, Lazer
  "pix recebido 500"    → entrada, Transferência
  "salário 2500"        → entrada, Salário
  "freela 800"          → entrada, Freela
  "vendi tênis 200"     → entrada, Vendas

SOMENTE JSON. Nenhum texto fora do JSON."""


def processar_com_ia(mensagem: str) -> dict:
    """Chama Groq/LLaMA e devolve dict parseado. Levanta ValueError se inválido."""
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": mensagem}
            ],
            temperature=0.2,
            max_tokens=200
        )
        raw = response.choices[0].message.content.strip()
        # Remove blocos markdown se existirem
        raw = re.sub(r"```json|```", "", raw).strip()
        # Extrai só o JSON
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            raise ValueError(f"Nenhum JSON encontrado na resposta: {raw!r}")
        resultado = json.loads(match.group())
        # Garante campos obrigatórios
        resultado.setdefault("intencao", "outro")
        resultado.setdefault("descricao", mensagem)
        resultado.setdefault("valor", 0.0)
        resultado.setdefault("tipo", "saida")
        resultado.setdefault("categoria", "Outros")
        return resultado
    except json.JSONDecodeError as e:
        log.error("JSON inválido da IA: %s", e)
        raise ValueError(f"Resposta da IA inválida: {e}")


# ─── RELATÓRIOS ────────────────────────────────────────────────────────────────

EMOJIS_CAT = {
    "alimentação": "🍔",
    "transporte":  "🚗",
    "lazer":       "🎮",
    "saúde":       "💊",
    "moradia":     "🏠",
    "educação":    "📚",
    "roupas":      "👕",
    "beleza e cuidados": "💅",
    "serviços":    "🔧",
    "salário":     "💼",
    "freela":      "💻",
    "vendas":      "🛍️",
    "transferência": "📲",
    "outros":      "📦",
}


def emoji_cat(cat: str) -> str:
    return EMOJIS_CAT.get(cat.lower(), "📦")


def barra_progresso(porcentagem: int, tamanho: int = 10) -> str:
    cheio = min(porcentagem // (100 // tamanho), tamanho)
    return "█" * cheio + "░" * (tamanho - cheio)


def formatar_relatorio(titulo: str, label_ini: str, label_fim: str,
                        transacoes: list[dict], limite: float = 0) -> str:
    if not transacoes:
        return (
            f"📭 *{titulo}*\n\n"
            "Nenhuma transação encontrada neste período.\n\n"
            "Registre um gasto: _mercado 85_ ou uma entrada: _pix recebido 500_"
        )

    entradas = [t for t in transacoes if t["tipo"] == "entrada"]
    saidas   = [t for t in transacoes if t["tipo"] == "saida"]

    total_ent  = sum(float(t["valor"]) for t in entradas)
    total_sai  = sum(float(t["valor"]) for t in saidas)
    saldo      = total_ent - total_sai

    # Agrupa saídas por categoria
    cats: dict[str, list] = {}
    for t in saidas:
        cats.setdefault(t["categoria"], []).append(t)

    linhas = [
        f"📊 *{titulo}*",
        f"Período: {label_ini} → {label_fim}\n",
    ]

    # Entradas (resumido)
    if entradas:
        linhas.append("💚 *Entradas*")
        for t in entradas:
            short = id_curto(t["id"])
            linhas.append(f"  {t['descricao'].capitalize()}: R$ {float(t['valor']):.2f}  ·  _{short}_")
        linhas.append(f"  *Total entradas: R$ {total_ent:.2f}*\n")

    # Saídas por categoria
    if saidas:
        linhas.append("🔴 *Saídas por categoria*")
        for cat, itens in sorted(cats.items(), key=lambda x: -sum(float(i["valor"]) for i in x[1])):
            subtotal = sum(float(i["valor"]) for i in itens)
            linhas.append(f"\n{emoji_cat(cat)} *{cat}* — R$ {subtotal:.2f}")
            for t in itens:
                short = id_curto(t["id"])
                linhas.append(f"  {t['descricao'].capitalize()}: R$ {float(t['valor']):.2f}  ·  _{short}_")
        linhas.append(f"\n  *Total saídas: R$ {total_sai:.2f}*")

    # Saldo
    saldo_emoji = "🟢" if saldo >= 0 else "🔴"
    linhas.append(f"\n{saldo_emoji} *Saldo do período: R$ {saldo:+.2f}*")

    # Limite mensal (só em relatório de mês)
    if limite > 0:
        porc = int((total_sai / limite) * 100) if limite > 0 else 0
        barra = barra_progresso(porc)
        restante = limite - total_sai
        linhas.append(f"\n⚠️ *Limite mensal: R$ {limite:.2f}*")
        linhas.append(f"[{barra}] {porc}% usado")
        if restante >= 0:
            linhas.append(f"✅ Disponível: R$ {restante:.2f}")
        else:
            linhas.append(f"🚨 Estourou em R$ {abs(restante):.2f}!")
    else:
        linhas.append("\n💡 _Defina um limite: limite 2000_")

    linhas.append("\n🗑️ _Apagar: apagar + ID (ex: apagar ae3f06)_")

    return "\n".join(linhas)


def relatorio_top(telefone: str, n: int = 5) -> str:
    ini, fim = periodo_mes()
    transacoes = buscar_transacoes(telefone, ini, fim)
    saidas = sorted(
        [t for t in transacoes if t["tipo"] == "saida"],
        key=lambda x: -float(x["valor"])
    )
    if not saidas:
        return "📭 Nenhuma saída registrada este mês."

    linhas = [f"🏆 *Top {n} maiores saídas do mês:*\n"]
    for i, t in enumerate(saidas[:n], 1):
        linhas.append(f"{i}. {t['descricao'].capitalize()} — R$ {float(t['valor']):.2f}  ({t['categoria']})")
    return "\n".join(linhas)


def relatorio_saldo(telefone: str) -> str:
    ini, fim = periodo_mes()
    transacoes = buscar_transacoes(telefone, ini, fim)
    total_ent = sum(float(t["valor"]) for t in transacoes if t["tipo"] == "entrada")
    total_sai = sum(float(t["valor"]) for t in transacoes if t["tipo"] == "saida")
    saldo = total_ent - total_sai
    emoji = "🟢" if saldo >= 0 else "🔴"
    mes = nome_mes(ini.month)
    return (
        f"💰 *Saldo — {mes}*\n\n"
        f"💚 Entradas: R$ {total_ent:.2f}\n"
        f"🔴 Saídas:   R$ {total_sai:.2f}\n"
        f"─────────────────\n"
        f"{emoji} *Saldo: R$ {saldo:+.2f}*"
    )


# ─── MENSAGENS FIXAS ───────────────────────────────────────────────────────────

MSG_BEM_VINDO = (
    "Oi! 👋 Eu sou o *Cash Flow IA*, seu assistente financeiro aqui no WhatsApp.\n\n"
    "É simples: me manda o que gastou ou recebeu, e eu anoto tudo pra você.\n\n"
    "*Exemplos rápidos:*\n"
    "• _mercado 87_ → saída\n"
    "• _uber 32_ → saída\n"
    "• _investimento 50_ → saída\n"
    "• _salário 2500_ → entrada\n"
    "• _pix recebido 300_ → entrada\n"
    "• _retorno investimento 200_ → entrada\n\n"
    "Pra ver seu extrato, é só mandar _mês_, _hoje_ ou _saldo_.\n\n"
    "Qual foi sua última movimentação? 😊"
)

MSG_AJUDA = (
    "🤖 *Cash Flow IA — Comandos*\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "📤 *Saída:* _mercado 85_ · _uber 32_\n"
    "📥 *Entrada:* _salário 2500_ · _pix recebido 400_\n\n"
    "📊 *Relatórios:*\n"
    "  _hoje_ — movimentações de hoje\n"
    "  _semana_ — esta semana\n"
    "  _mês_ — mês atual completo\n"
    "  _saldo_ — balanço do mês\n"
    "  _top gastos_ — maiores saídas\n\n"
    "⚠️ *Limite mensal:* _limite 2000_\n\n"
    "🗑️ *Apagar último:* _apagar_\n"
    "🗑️ *Apagar por ID:* _apagar ae3f06_\n\n"
    "💡 *Dica financeira:* _dica_\n"
    "━━━━━━━━━━━━━━━━━━"
)

DICAS = [
    "💡 *Regra 50/30/20:* 50% pra necessidades, 30% pra lazer e 20% pra poupar. Simples e eficaz!",
    "💡 Registrar cada gasto já é metade do controle. Quem sabe pra onde vai o dinheiro consegue direcionar. 💪",
    "💡 Pequenos gastos somam muito. Um café por dia pode ser ~R$ 100/mês. Não precisa cortar — só saber que existe.",
    "💡 Espere 24h antes de uma compra por impulso. Se ainda quiser depois, compre sem culpa. 😄",
    "💡 *Dica rápida:* defina um limite mensal aqui. Manda _limite 2000_ e eu aviso quando estiver chegando perto!",
    "💡 Separar uma % fixa para lazer evita a culpa de gastar com o que você gosta. Lazer faz parte do orçamento!",
    "💡 Revise assinaturas mensais (streaming, apps). É comum pagar por serviços que mal usa.",
]

SAUDACOES = [
    "👋 Olá! Tudo bem? Registre um gasto ou mande _mês_ pra ver seu extrato. 💸",
    "Oi! 🤖 Pode mandar: _mercado 80_ pra registrar, ou _saldo_ pra ver seu balanço.",
    "Olá! Pronto pra te ajudar com as finanças. 💰 Qual foi a última movimentação?",
]


# ─── WEBHOOK ───────────────────────────────────────────────────────────────────

PALAVRAS_BEM_VINDO = {
    "oi", "olá", "ola", "hello", "hi", "hey",
    "inicio", "início", "start", "começar", "comecar"
}


@app.route("/webhook", methods=["POST"])
def webhook():
    telefone = request.form.get("From", "").strip()
    mensagem = request.form.get("Body", "").strip()
    resp = MessagingResponse()

    if not mensagem or not telefone:
        return str(resp)

    log.info("MSG de %s: %s", telefone, mensagem)

    # Atalho para bem-vindo
    if mensagem.lower() in PALAVRAS_BEM_VINDO:
        resp.message(MSG_BEM_VINDO)
        return str(resp)

    try:
        resultado = processar_com_ia(mensagem)
    except ValueError as e:
        log.error("Erro ao processar IA: %s", e)
        resp.message(
            "🤖 Não consegui entender sua mensagem.\n\n"
            "Tente algo como:\n"
            "_mercado 85_ · _uber 32_ · _salário 2500_\n\n"
            "Ou mande _ajuda_ para ver todos os comandos."
        )
        return str(resp)

    intencao  = resultado.get("intencao", "outro")
    valor     = float(resultado.get("valor", 0) or 0)
    tipo      = resultado.get("tipo", "saida").strip().lower()
    descricao = resultado.get("descricao", mensagem).strip().capitalize()
    categoria = resultado.get("categoria", "Outros").strip()

    try:
        # ── GASTO / ENTRADA ────────────────────────────────────────────────────
        if intencao == "gasto":
            if valor <= 0:
                resp.message(
                    "🤖 Não identifiquei o valor!\n\n"
                    "Exemplos:\n"
                    "_mercado 85.50_ · _pix recebido 300_"
                )
                return str(resp)

            pk, short = salvar_transacao(telefone, descricao, valor, tipo, categoria)

            if tipo == "entrada":
                icone, label = "💚", "Entrada Registrada"
            else:
                icone, label = "🔴", "Saída Registrada"

            msg = (
                f"{icone} *{label}*\n\n"
                f"📝 {descricao}\n"
                f"🏷️ {categoria}\n"
                f"💰 R$ {valor:.2f}\n"
                f"📅 {date.today().strftime('%d/%m/%Y')}\n"
                f"🔑 ID: {short}\n\n"
                f"🗑️ _Para apagar: apagar {short}_"
            )

            # Alertas de limite (somente para saídas)
            if tipo == "saida":
                limite = buscar_limite(telefone)
                if limite > 0:
                    ini, fim = periodo_mes()
                    txs = buscar_transacoes(telefone, ini, fim)
                    total_sai = sum(float(t["valor"]) for t in txs if t["tipo"] == "saida")
                    porc = int((total_sai / limite) * 100)
                    if total_sai > limite:
                        msg += f"\n\n🚨 *Limite estourado!* R$ {total_sai:.2f} de R$ {limite:.2f}"
                    elif porc >= 90:
                        msg += f"\n\n⚠️ *Atenção!* {porc}% do limite mensal usado."
                    elif porc >= 75:
                        msg += f"\n\n💛 {porc}% do limite usado — fique de olho!"

            resp.message(msg)

        # ── HOJE ───────────────────────────────────────────────────────────────
        elif intencao == "hoje":
            ini, fim = periodo_hoje()
            txs = buscar_transacoes(telefone, ini, fim)
            label = date.today().strftime("%d/%m/%Y")
            resp.message(formatar_relatorio("Movimentações de Hoje", label, label, txs))

        # ── SEMANA ─────────────────────────────────────────────────────────────
        elif intencao == "semana":
            ini, fim = periodo_semana()
            txs = buscar_transacoes(telefone, ini, fim)
            resp.message(formatar_relatorio(
                "Esta Semana",
                ini.strftime("%d/%m"), fim.strftime("%d/%m/%Y"), txs
            ))

        # ── MÊS / RELATÓRIO ────────────────────────────────────────────────────
        elif intencao in ("relatorio", "mês"):
            ini, fim = periodo_mes()
            txs = buscar_transacoes(telefone, ini, fim)
            limite = buscar_limite(telefone)
            mes_label = f"{nome_mes(ini.month)}/{ini.year}"
            resp.message(formatar_relatorio(
                f"Extrato — {mes_label}",
                ini.strftime("%d/%m"), fim.strftime("%d/%m/%Y"),
                txs, limite
            ))

        # ── SALDO ──────────────────────────────────────────────────────────────
        elif intencao == "saldo":
            resp.message(relatorio_saldo(telefone))

        # ── TOP GASTOS ─────────────────────────────────────────────────────────
        elif intencao == "top":
            resp.message(relatorio_top(telefone))

        # ── LIMITE ─────────────────────────────────────────────────────────────
        elif intencao == "limite":
            if valor > 0:
                salvar_limite(telefone, valor)
                ini, fim = periodo_mes()
                txs = buscar_transacoes(telefone, ini, fim)
                total_sai = sum(float(t["valor"]) for t in txs if t["tipo"] == "saida")
                porc = int((total_sai / valor) * 100) if valor > 0 else 0
                resp.message(
                    f"✅ *Limite mensal: R$ {valor:.2f}*\n\n"
                    f"Saídas este mês: R$ {total_sai:.2f} ({porc}%)\n\n"
                    f"Vou te avisar ao se aproximar do limite! 🔔"
                )
            else:
                resp.message("🤖 Informe o valor. Ex: *limite 2000*")

        # ── APAGAR ─────────────────────────────────────────────────────────────
        elif intencao == "apagar":
            # Procura ID de 6 chars hexadecimais na mensagem original
            match = re.search(r'\b([a-f0-9]{6})\b', mensagem.lower())
            if match:
                desc, val, tp = apagar_por_id_curto(telefone, match.group(1))
            else:
                desc, val, tp = apagar_ultimo(telefone)

            if desc:
                icone = "💚" if tp == "entrada" else "🔴"
                ini, fim = periodo_mes()
                txs = buscar_transacoes(telefone, ini, fim)
                total_sai = sum(float(t["valor"]) for t in txs if t["tipo"] == "saida")
                total_ent = sum(float(t["valor"]) for t in txs if t["tipo"] == "entrada")
                resp.message(
                    f"🗑️ *Removido:* {desc.capitalize()} ({icone} R$ {val:.2f})\n\n"
                    f"💚 Entradas: R$ {total_ent:.2f}\n"
                    f"🔴 Saídas:   R$ {total_sai:.2f}"
                )
            else:
                resp.message("🤖 Nenhuma transação encontrada para apagar.")

        # ── DICA ───────────────────────────────────────────────────────────────
        elif intencao == "dica":
            resp.message(random.choice(DICAS))

        # ── OI ─────────────────────────────────────────────────────────────────
        elif intencao == "oi":
            resp.message(random.choice(SAUDACOES))

        # ── AJUDA ──────────────────────────────────────────────────────────────
        elif intencao == "ajuda":
            resp.message(MSG_AJUDA)

        # ── FALLBACK ───────────────────────────────────────────────────────────
        else:
            resp.message(
                "🤖 Não entendi!\n\n"
                "Exemplos:\n"
                "_mercado 85_ → registrar saída\n"
                "_salário 2500_ → registrar entrada\n"
                "_mês_ → extrato mensal\n"
                "_ajuda_ → ver todos os comandos"
            )

    except Exception as e:
        log.exception("Erro no webhook: %s", e)
        resp.message(
            "🤖 Ops, algo deu errado! Por favor, tente novamente em instantes.\n\n"
            "Se o problema persistir, mande _ajuda_."
        )

    return str(resp)


@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "app": "Cash Flow IA", "version": "2.0"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
