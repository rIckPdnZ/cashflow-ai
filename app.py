import os
import json
import re
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
            telefone TEXT, descricao TEXT, valor REAL,
            categoria TEXT, data TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS limites (
            telefone TEXT PRIMARY KEY, limite_mensal REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            telefone TEXT PRIMARY KEY, primeiro_acesso INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    return conn

def e_primeiro_acesso(telefone):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT primeiro_acesso FROM usuarios WHERE telefone=?", (telefone,))
    row = c.fetchone()
    if not row:
        conn.execute("INSERT INTO usuarios (telefone, primeiro_acesso) VALUES (?, 1)", (telefone,))
        conn.commit()
        conn.close()
        return True
    if row[0] == 1:
        conn.execute("UPDATE usuarios SET primeiro_acesso=0 WHERE telefone=?", (telefone,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def salvar_gasto(telefone, descricao, valor, categoria):
    conn = get_conn()
    conn.execute(
        "INSERT INTO gastos (telefone, descricao, valor, categoria, data) VALUES (?, ?, ?, ?, ?)",
        (telefone, descricao, valor, categoria, datetime.now().strftime("%Y-%m-%d"))
    )
    conn.commit()
    conn.close()

def apagar_ultimo_gasto(telefone):
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
        "SELECT descricao, valor, categoria FROM gastos WHERE telefone=? AND strftime('%m', data)=? AND strftime('%Y', data)=?",
        (telefone, f"{mes:02d}", str(ano))
    )
    gastos = c.fetchall()
    conn.close()
    return gastos

def buscar_total_mes(telefone):
    return sum(g[1] for g in buscar_gastos_mes(telefone))

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

def contar_gastos_mes(telefone):
    return len(buscar_gastos_mes(telefone))

# ─── IA ────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Voce e o CAIXA, o robozinho do CashFlow AI — assistente financeiro pessoal no WhatsApp. 
Personalidade: animado, usa linguagem jovem brasileira, nao julga gastos, celebra quando o usuario registra (pois registrar ja e um habito incrivel). Use o emoji 🤖 ocasionalmente.

Responda APENAS em JSON com este formato exato, sem nada fora:
{"intencao": "gasto|relatorio|limite|ajuda|apagar|dica|outro", "descricao": "nome do gasto", "valor": 0.0, "categoria": "alimentacao|transporte|lazer|saude|moradia|educacao|roupas|outros", "resposta": "mensagem curta e animada em portugues informal"}

Regras de classificacao:
- "uber 27", "ifood 45", "mercado 150.50", "almoco 35" = gasto
- "relatorio", "resumo", "quanto gastei", "gastos", "ver gastos" = relatorio
- "limite 2000", "meu limite e 1500" = limite
- "oi", "ajuda", "help", "o que voce faz", "comandos" = ajuda
- "apagar", "deletar", "erro", "errei" = apagar
- "dica", "conselho", "como economizar" = dica
- Se nao achar valor coloque 0 e peca na resposta
- Nas respostas de gasto confirme descricao, valor e categoria com emoji especifico da categoria
- Varie as respostas, nao repita sempre a mesma frase
- SOMENTE o JSON, sem texto fora, sem backticks, sem markdown"""

DICAS = [
    "Sabia que anotar os gastos ja e metade da batalha? Quem sabe pra onde vai o dinheiro, consegue controlar. Continue assim! 💪",
    "Dica de ouro: defina um limite mensal! So manda _limite 2000_ (ou o valor que quiser). Assim voce fica de olho no progresso 👀",
    "Antes de uma compra por impulso, espera 24h. Se ainda quiser depois, pode comprar sem culpa 😄",
    "Separar uma % fixa pra lazer evita aquela sensacao de culpa na hora de gastar. Lazer faz parte da vida!",
    "Gastos pequenos sao traidores! Um cafezinho por dia = R$ 100/mes. Nao precisa cortar, so saber que existe 😉",
    "Meta simples que funciona: toda vez que gastar com algo nao planejado, registra aqui. So isso ja muda o jogo.",
]

def processar_com_ia(mensagem):
    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": mensagem}
        ]
    )
    texto = response.choices[0].message.content.strip()
    texto = re.sub(r"```json|```", "", texto).strip()
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
            f"🤖 Nenhum gasto registrado em {meses_nome[mes-1]}/{ano} ainda!\n\n"
            "Manda o primeiro agora:\n"
            "_mercado 150_ ou _uber 27_ ou _almoco 35_ 💸"
        )

    categorias = {}
    for desc, valor, cat in gastos:
        categorias.setdefault(cat, []).append((desc, valor))

    total = sum(g[1] for g in gastos)
    qtd = len(gastos)
    emojis = {"alimentacao":"🍔","transporte":"🚗","lazer":"🎮","saude":"💊",
              "moradia":"🏠","educacao":"📚","roupas":"👕","outros":"📦"}

    # Categoria que mais gastou
    cat_top = max(categorias.items(), key=lambda x: sum(i[1] for i in x[1]))
    cat_top_nome = cat_top[0].capitalize()
    cat_top_valor = sum(i[1] for i in cat_top[1])

    linhas = [f"🤖 *Relatorio de {meses_nome[mes-1]}/{ano}*"]
    linhas.append(f"_{qtd} gasto(s) registrado(s)_\n")

    for cat, itens in sorted(categorias.items(), key=lambda x: -sum(i[1] for i in x[1])):
        subtotal = sum(i[1] for i in itens)
        linhas.append(f"{emojis.get(cat,'📦')} *{cat.capitalize()}* → R$ {subtotal:.2f}")
        for desc, val in itens:
            linhas.append(f"   • {desc}: R$ {val:.2f}")

    linhas.append(f"\n💰 *Total: R$ {total:.2f}*")

    limite = buscar_limite(telefone)
    if limite > 0:
        restante = limite - total
        porcentagem = int((total / limite) * 100)
        barra = "█" * min(porcentagem // 10, 10) + "░" * (10 - min(porcentagem // 10, 10))
        if restante >= 0:
            linhas.append(f"\n📊 Limite: [{barra}] {porcentagem}%")
            linhas.append(f"✅ Sobram R$ {restante:.2f} de R$ {limite:.2f}")
        else:
            linhas.append(f"\n📊 Limite: [{barra}] {porcentagem}%")
            linhas.append(f"🚨 Passou em R$ {abs(restante):.2f}!")
    else:
        linhas.append(f"\n💡 _Defina um limite: limite 2000_")

    linhas.append(f"\n🏆 Maior gasto: {cat_top_nome} (R$ {cat_top_valor:.2f})")

    return "\n".join(linhas)

# ─── MENSAGENS ─────────────────────────────────────────────────────────────────

MSG_BEM_VINDO = (
    "👋 Oi! Seja bem-vindo ao *CashFlow AI!* 🤖💸\n\n"
    "Serei seu assistente financeiro pessoal aqui no WhatsApp.\n\n"
    "E simples assim:\n\n"
    "💬 *Registre seus gastos:*\n"
    "Só mandar o nome e o valor\n\n"
    "📊 *Veja seu relatorio:*\n"
    "relatório mensal\n\n"
    "⚠️ *Defina um limite mensal:*\n"
    "Ex: 3000\n\n"
    "❓ *Precisa de ajuda?*\n"
    "_ajuda_ ou _comandos_\n\n"
    "Agora me conta: qual foi seu ultimo gasto? 😄"
)

MSG_AJUDA = (
    "🤖 *CashFlow AI — Comandos:*\n\n"
    "💬 *Registrar gasto:*\n\n"
    "📊 *Ver relatorio do mes:*\n\n"
    "⚠️ *Definir limite mensal:*\n\n"
    "🗑️ *Apagar ultimo gasto:*\n"
    "_apagar_ ou _errei_\n\n"
    "💡 *Pedir uma dica financeira:*\n"
    "_dica_\n\n"
    "Qualquer duvida e so chamar! 💸"
)

# ─── WEBHOOK ───────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    telefone = request.form.get("From", "")
    mensagem = request.form.get("Body", "").strip()
    resp = MessagingResponse()

    if not mensagem:
        return str(resp)

    if e_primeiro_acesso(telefone):
        resp.message(MSG_BEM_VINDO)
        return str(resp)

    try:
        resultado = processar_com_ia(mensagem)
        intencao = resultado.get("intencao", "outro")

        if intencao == "gasto":
            valor = resultado.get("valor", 0)
            descricao = resultado.get("descricao", mensagem)
            categoria = resultado.get("categoria", "outros")

            if valor > 0:
                salvar_gasto(telefone, descricao, valor, categoria)
                total_mes = buscar_total_mes(telefone)
                qtd = contar_gastos_mes(telefone)
                limite = buscar_limite(telefone)

                alerta = ""
                if limite > 0:
                    porcentagem = int((total_mes / limite) * 100)
                    if total_mes > limite:
                        alerta = f"\n\n🚨 *Limite estourado!* Voce ja gastou R$ {total_mes:.2f} de R$ {limite:.2f} esse mes."
                    elif porcentagem >= 90:
                        alerta = f"\n\n⚠️ *Atencao!* Voce ja usou {porcentagem}% do seu limite mensal!"
                    elif porcentagem >= 75:
                        alerta = f"\n\n💛 Voce ja usou {porcentagem}% do limite. Fica de olho!"

                rodape = f"\n_Total do mes: R$ {total_mes:.2f} ({qtd} gastos)_"
                resposta = resultado.get("resposta", f"✅ {descricao}: R$ {valor:.2f} anotado!")
                resp.message(resposta + alerta + rodape)
            else:
                resp.message(
                    "🤖 Nao consegui pegar o valor!\n\n"
                    "Me manda assim:\n"
                    "_mercado 85.50_ ou _uber 27_"
                )

        elif intencao == "relatorio":
            resp.message(gerar_relatorio(telefone))

        elif intencao == "limite":
            valor = resultado.get("valor", 0)
            if valor > 0:
                salvar_limite(telefone, valor)
                total_mes = buscar_total_mes(telefone)
                porcentagem = int((total_mes / valor) * 100) if valor > 0 else 0
                resp.message(
                    f"✅ *Limite mensal: R$ {valor:.2f}*\n\n"
                    f"Voce ja gastou R$ {total_mes:.2f} esse mes ({porcentagem}% do limite).\n\n"
                    f"🤖 Vou te avisar quando estiver chegando perto!"
                )
            else:
                resp.message("🤖 Qual o valor do limite? Ex: *limite 2000*")

        elif intencao == "apagar":
            desc, val = apagar_ultimo_gasto(telefone)
            if desc:
                total_mes = buscar_total_mes(telefone)
                resp.message(
                    f"🗑️ Apaguei: *{desc}* (R$ {val:.2f})\n\n"
                    f"_Total atual do mes: R$ {total_mes:.2f}_"
                )
            else:
                resp.message("🤖 Nenhum gasto para apagar ainda!")

        elif intencao == "dica":
            import random
            dica = random.choice(DICAS)
            resp.message(f"💡 *Dica do CAIXA:*\n\n{dica}")

        elif intencao == "ajuda":
            resp.message(MSG_AJUDA)

        else:
            resp.message(
                resultado.get("resposta",
                "🤖 Nao entendi muito bem!\n\nTenta:\n_mercado 50_ para registrar\n_relatorio_ para ver os gastos\n_ajuda_ para ver tudo que faco"
            ))

    except Exception as e:
        print(f"Erro: {e}")
        resp.message("🤖 Ops, deu um erro aqui! Tenta de novo em instantes.")

    return str(resp)

@app.route("/", methods=["GET"])
def health():
    return "CashFlow AI rodando! 🤖🚀"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
