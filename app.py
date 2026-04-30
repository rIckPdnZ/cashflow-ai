import os
import json
import re
import random
from datetime import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import sqlite3
from groq import Groq

app = Flask(__name__)
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
DB_PATH = "/tmp/cashflow.db"

# ─── BANCO ─────────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gastos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telefone TEXT,
            descricao TEXT,
            valor REAL,
            categoria TEXT,
            data TEXT,
            criado_em TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS limites (
            telefone TEXT PRIMARY KEY,
            limite_mensal REAL DEFAULT 0
        )
    """)
    conn.commit()
    return conn

def gerar_id_curto(id_int):
    """Gera ID curto tipo 'ae3f06' baseado no id do banco"""
    import hashlib
    h = hashlib.md5(str(id_int).encode()).hexdigest()
    return h[:6]

def salvar_gasto(telefone, descricao, valor, categoria):
    conn = get_conn()
    agora = datetime.now()
    cursor = conn.execute(
        "INSERT INTO gastos (telefone, descricao, valor, categoria, data, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
        (telefone, descricao, valor, categoria, agora.strftime("%Y-%m-%d"), agora.isoformat())
    )
    gasto_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return gasto_id

def apagar_por_id_curto(telefone, id_curto):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, descricao, valor FROM gastos WHERE telefone=?", (telefone,))
    todos = c.fetchall()
    for row in todos:
        if gerar_id_curto(row[0]) == id_curto.lower():
            conn.execute("DELETE FROM gastos WHERE id=?", (row[0],))
            conn.commit()
            conn.close()
            return row[1], row[2]
    conn.close()
    return None, None

def apagar_ultimo(telefone):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, descricao, valor FROM gastos WHERE telefone=? ORDER BY id DESC LIMIT 1", (telefone,))
    row = c.fetchone()
    if row:
        conn.execute("DELETE FROM gastos WHERE id=?", (row[0],))
        conn.commit()
        conn.close()
        return row[1], row[2]
    conn.close()
    return None, None

def buscar_gastos_mes(telefone, mes=None, ano=None):
    mes = mes or datetime.now().month
    ano = ano or datetime.now().year
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, descricao, valor, categoria FROM gastos WHERE telefone=? AND strftime('%m', data)=? AND strftime('%Y', data)=? ORDER BY id",
        (telefone, f"{mes:02d}", str(ano))
    )
    gastos = c.fetchall()
    conn.close()
    return gastos

def buscar_total_mes(telefone):
    gastos = buscar_gastos_mes(telefone)
    return sum(g[2] for g in gastos)

def buscar_limite(telefone):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT limite_mensal FROM limites WHERE telefone=?", (telefone,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def salvar_limite(telefone, valor):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO limites (telefone, limite_mensal) VALUES (?, ?)", (telefone, valor))
    conn.commit()
    conn.close()

# ─── IA ────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Voce e o CashFlow AI, assistente financeiro no WhatsApp. Personalidade: simpatico, jovem, brasileiro, animado.

Responda APENAS em JSON exato sem nada fora:
{"intencao": "gasto|relatorio|limite|ajuda|apagar|dica|oi|outro", "descricao": "nome do gasto", "valor": 0.0, "categoria": "Alimentacao|Transporte|Lazer|Saude|Moradia|Educacao|Beleza e Cuidados|Roupas|Outros"}

Regras:
- "uber 27", "ifood 45", "mercado 150", "agua 5", "almoco 35" = gasto
- "relatorio", "resumo", "quanto gastei", "extrato" = relatorio
- "limite 2000" = limite (extraia o valor numerico)
- "oi", "ola", "bom dia", "ei" = oi
- "apagar", "deletar", "errei", "erro" = apagar (valor=0)
- "apagar abc123" = apagar com id_curto no campo descricao
- "ajuda", "help", "comandos" = ajuda
- "dica", "conselho" = dica
- Categorize com inteligencia: uber/taxi/onibus=Transporte, ifood/restaurante/almoco=Alimentacao, farmacia=Saude, etc
- Capitalize a primeira letra da categoria
- SOMENTE JSON, sem backticks"""

def processar_com_ia(mensagem):
    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": mensagem}
        ],
        temperature=0.3
    )
    texto = response.choices[0].message.content.strip()
    texto = re.sub(r"```json|```", "", texto).strip()
    # Pega só o JSON mesmo que venha texto antes
    match = re.search(r'\{.*\}', texto, re.DOTALL)
    if match:
        texto = match.group()
    return json.loads(texto)

# ─── RELATORIO ─────────────────────────────────────────────────────────────────

def gerar_relatorio(telefone):
    mes = datetime.now().month
    ano = datetime.now().year
    gastos = buscar_gastos_mes(telefone)
    meses_nome = ["Janeiro","Fevereiro","Marco","Abril","Maio","Junho",
                  "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]

    if not gastos:
        return (
            f"📭 *Nenhum gasto em {meses_nome[mes-1]}/{ano}*\n\n"
            "Registre seu primeiro gasto agora!\n"
            "Ex: _mercado 150_ ou _uber 27_"
        )

    categorias = {}
    for gid, desc, valor, cat in gastos:
        categorias.setdefault(cat, []).append((gid, desc, valor))

    total = sum(g[2] for g in gastos)
    qtd = len(gastos)

    emojis = {
        "alimentacao":"🍔","transporte":"🚗","lazer":"🎮","saude":"💊",
        "moradia":"🏠","educacao":"📚","roupas":"👕","beleza e cuidados":"💅",
        "outros":"📦"
    }

    def get_emoji(cat):
        return emojis.get(cat.lower(), "📦")

    inicio_mes = f"01/{mes:02d}/{ano}"
    hoje = datetime.now().strftime("%d/%m/%Y")

    linhas = [
        f"🧾 *Extrato Financeiro*",
        f"Período: {inicio_mes} a {hoje}\n"
    ]

    for cat, itens in sorted(categorias.items(), key=lambda x: -sum(i[2] for i in x[1])):
        subtotal = sum(i[2] for i in itens)
        linhas.append(f"{get_emoji(cat)} *{cat}* (R$ {subtotal:.2f})")
        for gid, desc, val in itens:
            id_curto = gerar_id_curto(gid)
            linhas.append(f"  {desc}: R$ {val:.2f}  |  ID: {id_curto}")

    linhas.append(f"\n💰 *Total: R$ {total:.2f}* ({qtd} gastos)")

    limite = buscar_limite(telefone)
    if limite > 0:
        restante = limite - total
        porcentagem = int((total / limite) * 100)
        barra = "█" * min(porcentagem // 10, 10) + "░" * (10 - min(porcentagem // 10, 10))
        if restante >= 0:
            linhas.append(f"\n📊 [{barra}] {porcentagem}% do limite")
            linhas.append(f"✅ Sobram R$ {restante:.2f}")
        else:
            linhas.append(f"\n📊 [{barra}] {porcentagem}%")
            linhas.append(f"🚨 Passou em R$ {abs(restante):.2f}!")
    else:
        linhas.append(f"\n💡 _Defina um limite: limite 2000_")

    linhas.append(f"\n🗑️ _Para apagar: apagar + ID do gasto_")
    linhas.append(f"_Ex: apagar ae3f06_")

    return "\n".join(linhas)

# ─── MENSAGENS FIXAS ───────────────────────────────────────────────────────────

MSG_BEM_VINDO = (
    "👋 Oi! Seja bem-vindo ao *CashFlow AI!* 🤖💸\n\n"
    "Serei seu assistente financeiro pessoal aqui no WhatsApp.\n\n"
    "É simples assim:\n\n"
    "💬 *Registre seus gastos:*\n"
    "Só manda o nome e o valor\n"
    "_uber 27_ • _almoço 35.90_ • _mercado 200_\n\n"
    "📊 *Veja seu relatório:*\n"
    "_relatório_ ou _resumo_\n\n"
    "⚠️ *Defina um limite mensal:*\n"
    "_limite 2000_\n\n"
    "❓ *Precisa de ajuda?*\n"
    "_ajuda_ ou _comandos_\n\n"
    "Agora me conta: qual foi seu último gasto? 😄"
)

MSG_AJUDA = (
    "🤖 *CashFlow AI — Comandos:*\n\n"
    "💬 *Registrar gasto:*\n"
    "_uber 27_ • _almoço 35.90_ • _mercado 200_\n\n"
    "📊 *Ver extrato do mês:*\n"
    "_relatório_ ou _resumo_\n\n"
    "⚠️ *Definir limite mensal:*\n"
    "_limite 2000_\n\n"
    "🗑️ *Apagar último gasto:*\n"
    "_apagar_\n\n"
    "🗑️ *Apagar gasto por ID:*\n"
    "_apagar ae3f06_ (ID aparece no extrato)\n\n"
    "💡 *Pedir uma dica:*\n"
    "_dica_\n\n"
    "Qualquer dúvida é só chamar! 💸"
)

DICAS = [
    "💡 Sabia que anotar os gastos já é metade da batalha? Quem sabe pra onde vai o dinheiro consegue controlar. Continue assim! 💪",
    "💡 Dica de ouro: defina um limite mensal! Manda _limite 2000_. Assim você fica de olho no progresso 👀",
    "💡 Antes de uma compra por impulso, espera 24h. Se ainda quiser depois, pode comprar sem culpa 😄",
    "💡 Separar uma % fixa pra lazer evita aquela sensação de culpa na hora de gastar. Lazer faz parte da vida!",
    "💡 Gastos pequenos são traiçoeiros! Um cafezinho por dia = ~R$ 100/mês. Não precisa cortar, só saber que existe 😉",
    "💡 Meta simples que funciona: toda vez que gastar algo, registra aqui. Só isso já muda o jogo.",
]

SAUDACOES = [
    "Oi! 🤖 Tô aqui! Manda um gasto ou _ajuda_ pra ver tudo que faço 💸",
    "Olá! Pronto pra te ajudar com suas finanças 💰 Manda um gasto ou _relatório_!",
    "Ei! 🤖 Me manda um gasto, tipo _mercado 80_, e eu anoto na hora!",
]

# ─── WEBHOOK ───────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    telefone = request.form.get("From", "")
    mensagem = request.form.get("Body", "").strip()
    resp = MessagingResponse()

    if not mensagem:
        return str(resp)

    # Detecta primeiro acesso por palavras de saudação sem número
    palavras_boas_vindas = ["oi", "olá", "ola", "hello", "hi", "inicio", "início", "start", "começar", "comecar"]
    msg_lower = mensagem.lower().strip()

    if msg_lower in palavras_boas_vindas:
        resp.message(MSG_BEM_VINDO)
        return str(resp)

    try:
        resultado = processar_com_ia(mensagem)
        intencao = resultado.get("intencao", "outro")

        # ── GASTO ──
        if intencao == "gasto":
            valor = resultado.get("valor", 0)
            descricao = resultado.get("descricao", mensagem).strip().capitalize()
            categoria = resultado.get("categoria", "Outros").strip()

            if valor > 0:
                gasto_id = salvar_gasto(telefone, descricao, valor, categoria)
                id_curto = gerar_id_curto(gasto_id)
                total_mes = buscar_total_mes(telefone)
                limite = buscar_limite(telefone)
                hoje = datetime.now().strftime("%d/%m/%Y")

                # Monta resposta no estilo da imagem
                msg = (
                    f"✅ *Novo Gasto Registrado!*\n\n"
                    f"📝 Descrição: {descricao}\n"
                    f"🏷️ Categoria: {categoria}\n"
                    f"💰 Valor: R$ {valor:.2f}\n"
                    f"📅 Data: {hoje}\n"
                    f"🔑 ID: {id_curto}\n\n"
                    f"🗑️ Para apagar: _apagar {id_curto}_"
                )

                # Alerta de limite
                if limite > 0:
                    porcentagem = int((total_mes / limite) * 100)
                    if total_mes > limite:
                        msg += f"\n\n🚨 *Limite estourado!* Você gastou R$ {total_mes:.2f} de R$ {limite:.2f}"
                    elif porcentagem >= 90:
                        msg += f"\n\n⚠️ *Atenção!* Você já usou {porcentagem}% do limite mensal!"
                    elif porcentagem >= 75:
                        msg += f"\n\n💛 Você já usou {porcentagem}% do limite. Fica de olho!"

                resp.message(msg)

            else:
                resp.message(
                    "🤖 Não consegui identificar o valor!\n\n"
                    "Me manda assim:\n"
                    "_mercado 85.50_ ou _uber 27_"
                )

        # ── RELATÓRIO ──
        elif intencao == "relatorio":
            resp.message(gerar_relatorio(telefone))

        # ── LIMITE ──
        elif intencao == "limite":
            valor = resultado.get("valor", 0)
            if valor > 0:
                salvar_limite(telefone, valor)
                total_mes = buscar_total_mes(telefone)
                porcentagem = int((total_mes / valor) * 100) if valor > 0 else 0
                resp.message(
                    f"✅ *Limite mensal definido: R$ {valor:.2f}*\n\n"
                    f"Você já gastou R$ {total_mes:.2f} esse mês ({porcentagem}% do limite).\n\n"
                    f"🤖 Vou te avisar quando estiver chegando perto!"
                )
            else:
                resp.message("🤖 Qual o valor do limite? Ex: *limite 2000*")

        # ── APAGAR ──
        elif intencao == "apagar":
            descricao_raw = resultado.get("descricao", "").strip()
            # Verifica se veio um ID na mensagem
            partes = mensagem.strip().split()
            id_curto = None
            for p in partes:
                if len(p) == 6 and re.match(r'^[a-f0-9]+$', p.lower()):
                    id_curto = p.lower()
                    break

            if id_curto:
                desc, val = apagar_por_id_curto(telefone, id_curto)
            else:
                desc, val = apagar_ultimo(telefone)

            if desc:
                total_mes = buscar_total_mes(telefone)
                resp.message(
                    f"🗑️ *Gasto removido:*\n"
                    f"_{desc}_ — R$ {val:.2f}\n\n"
                    f"💰 Total do mês: R$ {total_mes:.2f}"
                )
            else:
                resp.message("🤖 Nenhum gasto encontrado para apagar!")

        # ── DICA ──
        elif intencao == "dica":
            resp.message(random.choice(DICAS))

        # ── OI ──
        elif intencao == "oi":
            resp.message(random.choice(SAUDACOES))

        # ── AJUDA ──
        elif intencao == "ajuda":
            resp.message(MSG_AJUDA)

        else:
            resp.message(
                "🤖 Não entendi muito bem!\n\n"
                "Tenta:\n"
                "_mercado 50_ → registrar gasto\n"
                "_relatório_ → ver extrato\n"
                "_ajuda_ → ver todos os comandos"
            )

    except Exception as e:
        print(f"Erro: {e}")
        resp.message("🤖 Ops, deu um erro aqui! Tenta de novo em instantes.")

    return str(resp)

@app.route("/", methods=["GET"])
def health():
    return "CashFlow AI rodando! 🤖🚀"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
