import os
import json
import re
from datetime import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import sqlite3

app = Flask(__name__)

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

DB_PATH = "/tmp/cashflow.db"

# ─── BANCO DE DADOS ────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gastos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telefone TEXT,
            descricao TEXT,
            valor REAL,
            categoria TEXT,
            data TEXT
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

SYSTEM_PROMPT = """Você é o CashFlow AI, um assistente financeiro pessoal simpático no WhatsApp.

Ao receber uma mensagem, identifique a intenção e responda APENAS em JSON com este formato:

{
  "intencao": "gasto" | "relatorio" | "limite" | "ajuda" | "outro",
  "descricao": "nome do gasto",
  "valor": 0.0,
  "categoria": "alimentacao|transporte|lazer|saude|moradia|educacao|roupas|outros",
  "resposta": "mensagem amigável em português informal"
}

Regras:
- "uber 27", "mercado 150", "almoço 35,90" → gasto
- "relatório", "resumo", "quanto gastei" → relatorio
- "limite 2000" → limite
- "oi", "ajuda", "help" → ajuda
- Se não conseguir extrair o valor, coloque 0 e peça na resposta
- Use emojis e linguagem brasileira informal
- Responda SOMENTE o JSON, sem texto fora dele
"""

def processar_com_ia(mensagem):
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": mensagem}]
    )
    texto = response.content[0].text.strip()
    texto = re.sub(r"```json|```", "", texto).strip()
    return json.loads(texto)

# ─── RELATÓRIO ─────────────────────────────────────────────────────────────────

def gerar_relatorio(telefone):
    mes = datetime.now().month
    ano = datetime.now().year
    gastos = buscar_gastos_mes(telefone)
    meses_nome = ["Janeiro","Fevereiro","Março","Abril","Maio","Junho",
                  "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]

    if not gastos:
        return f"Nenhum gasto registrado em {meses_nome[mes-1]}/{ano}. Começa mandando um gasto! 🐷"

    categorias = {}
    for desc, valor, cat in gastos:
        categorias.setdefault(cat, []).append((desc, valor))

    total = sum(g[1] for g in gastos)
    emojis = {"alimentacao":"🍔","transporte":"🚗","lazer":"🎮","saude":"💊",
              "moradia":"🏠","educacao":"📚","roupas":"👕","outros":"📦"}

    linhas = [f"📊 *Relatório - {meses_nome[mes-1]}/{ano}*\n"]
    for cat, itens in sorted(categorias.items(), key=lambda x: -sum(i[1] for i in x[1])):
        subtotal = sum(i[1] for i in itens)
        linhas.append(f"{emojis.get(cat,'📦')} *{cat.capitalize()}* → R$ {subtotal:.2f}")
        for desc, val in itens:
            linhas.append(f"  • {desc}: R$ {val:.2f}")

    linhas.append(f"\n💰 *Total: R$ {total:.2f}*")

    limite = buscar_limite(telefone)
    if limite > 0:
        restante = limite - total
        if restante >= 0:
            linhas.append(f"✅ Limite: R$ {limite:.2f} | Sobra: R$ {restante:.2f}")
        else:
            linhas.append(f"⚠️ Passou o limite em R$ {abs(restante):.2f}!")

    return "\n".join(linhas)

# ─── WEBHOOK ───────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    telefone = request.form.get("From", "")
    mensagem = request.form.get("Body", "").strip()
    resp = MessagingResponse()

    if not mensagem:
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
                    alerta = f"\n\n⚠️ Você já gastou R$ {total_mes:.2f} do limite de R$ {limite:.2f}!"
                resp.message(resultado.get("resposta", f"✅ {descricao}: R$ {valor:.2f} anotado!") + alerta)
            else:
                resp.message("Não consegui identificar o valor. Me diz assim: *mercado 85,50* 😊")

        elif intencao == "relatorio":
            resp.message(gerar_relatorio(telefone))

        elif intencao == "limite":
            valor = resultado.get("valor", 0)
            if valor > 0:
                salvar_limite(telefone, valor)
                resp.message(f"✅ Limite mensal definido: R$ {valor:.2f} 🐷")
            else:
                resp.message("Qual o valor do limite? Ex: *limite 2000*")

        elif intencao == "ajuda":
            resp.message(
                "👋 Olá! Sou o *CashFlow AI*!\n\n"
                "É simples assim:\n\n"
                "💬 *Registrar gasto:*\n_uber 27_ ou _almoço 35,90_\n\n"
                "📊 *Ver relatório:*\n_relatório_ ou _resumo_\n\n"
                "⚠️ *Definir limite mensal:*\n_limite 2000_\n\n"
                "Me conta um gasto de hoje! 😄"
            )
        else:
            resp.message(resultado.get("resposta", "Não entendi 😅 Tenta: _mercado 50_ ou manda _ajuda_"))

    except Exception as e:
        print(f"Erro: {e}")
        resp.message("Ops, tive um probleminha! Tenta de novo 🐷")

    return str(resp)

@app.route("/", methods=["GET"])
def health():
    return "CashFlow AI rodando! 🚀"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
