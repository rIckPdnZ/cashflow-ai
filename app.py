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

# ─── IA ────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Voce e o CashFlow AI, assistente financeiro pessoal no WhatsApp. Sua personalidade: simpatico, direto, usa linguagem jovem brasileira, nao julga os gastos do usuario, celebra quando ele registra um gasto (pois registrar ja e um habito incrivel).

Responda APENAS em JSON com este formato exato, sem nada fora:
{"intencao": "gasto|relatorio|limite|ajuda|apagar|outro", "descricao": "nome do gasto", "valor": 0.0, "categoria": "alimentacao|transporte|lazer|saude|moradia|educacao|roupas|outros", "resposta": "mensagem curta e simpatica em portugues informal"}

Regras de classificacao:
- "uber 27", "ifood 45", "mercado 150.50", "almoco 35" = gasto
- "relatorio", "resumo", "quanto gastei", "gastos" = relatorio
- "limite 2000", "meu limite e 1500" = limite
- "oi", "ajuda", "help", "o que voce faz" = ajuda
- "apagar", "deletar ultimo", "erro" = apagar
- Se nao achar valor coloque 0 e peca na resposta
- Nas respostas de gasto: seja animado, confirme o valor e categoria com emoji. Ex: "Anotado! 🛵 Uber por R$ 27,00 ja ta no seu controle"
- SOMENTE o JSON, sem texto fora, sem backticks, sem markdown"""

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
            f"📭 Nenhum gasto registrado em {meses_nome[mes-1]}/{ano} ainda.\n\n"
            "Manda o primeiro! Ex: _mercado 150_ ou _uber 27_ 💸"
        )

    categorias = {}
    for desc, valor, cat in gastos:
        categorias.setdefault(cat, []).append((desc, valor))

    total = sum(g[1] for g in gastos)
    emojis = {"alimentacao":"🍔","transporte":"🚗","lazer":"🎮","saude":"💊",
              "moradia":"🏠","educacao":"📚","roupas":"👕","outros":"📦"}

    linhas = [f"📊 *Relatorio de {meses_nome[mes-1]}/{ano}*\n"]
    for cat, itens in sorted(categorias.items(), key=lambda x: -sum(i[1] for i in x[1])):
        subtotal = sum(i[1] for i in itens)
        linhas.append(f"{emojis.get(cat,'📦')} *{cat.capitalize()}* → R$ {subtotal:.2f}")
        for desc, val in itens:
            linhas.append(f"  • {desc}: R$ {val:.2f}")

    linhas.append(f"\n💰 *Total gasto: R$ {total:.2f}*")

    limite = buscar_limite(telefone)
    if limite > 0:
        restante = limite - total
        porcentagem = int((total / limite) * 100)
        if restante >= 0:
            linhas.append(f"✅ Limite: R$ {limite:.2f} | Usando {porcentagem}% | Sobra R$ {restante:.2f}")
        else:
            linhas.append(f"🚨 Passou o limite de R$ {limite:.2f} em R$ {abs(restante):.2f}!")
    else:
        linhas.append(f"\n💡 Dica: define um limite mensal! Ex: _limite 2000_")

    return "\n".join(linhas)

# ─── WEBHOOK ───────────────────────────────────────────────────────────────────

MSG_BEM_VINDO = (
    "👋 Oi! Seja bem-vindo ao *CashFlow AI*! 🐷💸\n\n"
    "Eu sou seu assistente financeiro pessoal aqui no WhatsApp.\n\n"
    "E simples assim:\n\n"
    "💬 *Registrar gasto:*\n"
    "So manda o nome e o valor\n"
    "_uber 27_ • _almoco 35.90_ • _mercado 200_\n\n"
    "📊 *Ver relatorio:*\n"
    "_relatorio_ ou _resumo_\n\n"
    "⚠️ *Definir limite mensal:*\n"
    "_limite 2000_\n\n"
    "Agora me conta: qual foi seu ultimo gasto? 😄"
)

MSG_AJUDA = (
    "🐷 *CashFlow AI — Como usar:*\n\n"
    "💬 *Registrar gasto:*\n"
    "So manda o nome e o valor\n"
    "_uber 27_ • _almoco 35.90_ • _mercado 200_\n\n"
    "📊 *Relatorio do mes:*\n"
    "_relatorio_ ou _resumo_ ou _gastos_\n\n"
    "⚠️ *Definir limite mensal:*\n"
    "_limite 2000_\n\n"
    "❌ *Apagar ultimo gasto:*\n"
    "_apagar_ ou _erro_\n\n"
    "Qualquer duvida e so chamar! 💸"
)

@app.route("/webhook", methods=["POST"])
def webhook():
    telefone = request.form.get("From", "")
    mensagem = request.form.get("Body", "").strip()
    resp = MessagingResponse()

    if not mensagem:
        return str(resp)

    # Primeiro acesso — manda boas vindas antes de processar
    primeiro = e_primeiro_acesso(telefone)
    if primeiro:
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
                limite = buscar_limite(telefone)
                alerta = ""
                if limite > 0 and total_mes >= limite * 0.9:
                    alerta = f"\n\n🚨 *Atencao!* Voce ja gastou R$ {total_mes:.2f} de R$ {limite:.2f} esse mes!"
                resposta = resultado.get("resposta", f"✅ {descricao}: R$ {valor:.2f} anotado!")
                resp.message(resposta + alerta)
            else:
                resp.message("Nao consegui pegar o valor 😅\nMe manda assim: *mercado 85.50*")

        elif intencao == "relatorio":
            resp.message(gerar_relatorio(telefone))

        elif intencao == "limite":
            valor = resultado.get("valor", 0)
            if valor > 0:
                salvar_limite(telefone, valor)
                resp.message(
                    f"✅ *Limite mensal definido: R$ {valor:.2f}*\n\n"
                    f"Vou te avisar quando estiver chegando perto! 🐷"
                )
            else:
                resp.message("Qual o valor do limite? Ex: *limite 2000*")

        elif intencao == "apagar":
            conn = get_conn()
            conn.execute(
                "DELETE FROM gastos WHERE id = (SELECT MAX(id) FROM gastos WHERE telefone=?)",
                (telefone,)
            )
            conn.commit()
            conn.close()
            resp.message("🗑️ Ultimo gasto apagado! Se quiser ver como ficou, manda _relatorio_.")

        elif intencao == "ajuda":
            resp.message(MSG_AJUDA)

        else:
            resp.message(resultado.get("resposta", "Nao entendi 😅\nTenta: _mercado 50_ ou manda _ajuda_"))

    except Exception as e:
        print(f"Erro: {e}")
        resp.message("Ops, deu um erro aqui! Tenta de novo 🐷")

    return str(resp)

@app.route("/", methods=["GET"])
def health():
    return "CashFlow AI rodando! 🚀"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
