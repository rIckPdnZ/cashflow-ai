import os
import json
import re
from datetime import datetime, timedelta
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import anthropic
import sqlite3

app = Flask(__name__)

# Clientes de API
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ─── BANCO DE DADOS ────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("cashflow.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            telefone TEXT PRIMARY KEY,
            nome TEXT,
            ativo INTEGER DEFAULT 1,
            criado_em TEXT
        )
    """)
    c.execute("""
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS limites (
            telefone TEXT PRIMARY KEY,
            limite_mensal REAL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def salvar_gasto(telefone, descricao, valor, categoria):
    conn = sqlite3.connect("cashflow.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO gastos (telefone, descricao, valor, categoria, data, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
        (telefone, descricao, valor, categoria, datetime.now().strftime("%Y-%m-%d"), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def buscar_gastos_mes(telefone, mes=None, ano=None):
    if not mes:
        mes = datetime.now().month
    if not ano:
        ano = datetime.now().year
    conn = sqlite3.connect("cashflow.db")
    c = conn.cursor()
    c.execute(
        "SELECT descricao, valor, categoria, data FROM gastos WHERE telefone=? AND strftime('%m', data)=? AND strftime('%Y', data)=? ORDER BY data",
        (telefone, f"{mes:02d}", str(ano))
    )
    gastos = c.fetchall()
    conn.close()
    return gastos

def buscar_total_mes(telefone):
    gastos = buscar_gastos_mes(telefone)
    return sum(g[1] for g in gastos)

def buscar_limite(telefone):
    conn = sqlite3.connect("cashflow.db")
    c = conn.cursor()
    c.execute("SELECT limite_mensal FROM limites WHERE telefone=?", (telefone,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def salvar_limite(telefone, valor):
    conn = sqlite3.connect("cashflow.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO limites (telefone, limite_mensal) VALUES (?, ?)", (telefone, valor))
    conn.commit()
    conn.close()

def usuario_ativo(telefone):
    conn = sqlite3.connect("cashflow.db")
    c = conn.cursor()
    c.execute("SELECT ativo FROM usuarios WHERE telefone=?", (telefone,))
    row = c.fetchone()
    conn.close()
    # Em produção, verifique no Hub.la se o pagamento está ativo
    # Por ora, todo mundo que escrever é considerado ativo
    return True

# ─── IA ────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é o CashFlow AI, um assistente financeiro pessoal simpático que vive no WhatsApp.

Seu trabalho é ajudar o usuário a registrar gastos e entender suas finanças de forma simples e sem julgamentos.

Ao receber uma mensagem, você deve identificar a INTENÇÃO e responder em JSON com este formato exato:

{
  "intencao": "gasto" | "relatorio" | "limite" | "ajuda" | "outro",
  "descricao": "nome do gasto (se for gasto)",
  "valor": 0.0,
  "categoria": "alimentacao|transporte|lazer|saude|moradia|educacao|roupas|outros",
  "resposta": "mensagem amigável para o usuário",
  "pedir_confirmacao": false
}

Regras:
- Se o usuário mandar algo como "uber 27", "mercado 150", "almoço 35,90" → é um GASTO
- Se mandar "relatório", "resumo", "quanto gastei" → é RELATORIO
- Se mandar "limite 2000", "meu limite é 3000" → é LIMITE
- Se mandar "oi", "help", "ajuda", "o que você faz" → é AJUDA
- Seja sempre simpático, use linguagem informal e brasileira
- NUNCA invente valores. Se não conseguir extrair o valor, pergunte
- Categorize de forma inteligente: uber/99/taxi = transporte, ifood/restaurante = alimentacao, etc.
- Responda APENAS o JSON, sem texto antes ou depois
"""

def processar_com_ia(mensagem, historico=[]):
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": mensagem}]
    )
    texto = response.content[0].text.strip()
    # Remove possíveis backticks se a IA colocar
    texto = re.sub(r"```json|```", "", texto).strip()
    return json.loads(texto)

# ─── GERADOR DE RELATÓRIO ──────────────────────────────────────────────────────

def gerar_relatorio(telefone, mes=None, ano=None):
    if not mes:
        mes = datetime.now().month
    if not ano:
        ano = datetime.now().year

    gastos = buscar_gastos_mes(telefone, mes, ano)

    if not gastos:
        meses = ["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"]
        return f"Nenhum gasto registrado em {meses[mes-1]}/{ano}. 🐷"

    # Agrupa por categoria
    categorias = {}
    for desc, valor, cat, data in gastos:
        if cat not in categorias:
            categorias[cat] = []
        categorias[cat].append((desc, valor))

    total = sum(g[1] for g in gastos)
    meses_nome = ["Janeiro","Fevereiro","Março","Abril","Maio","Junho",
                  "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]

    linhas = [f"📊 *Relatório Financeiro*"]
    linhas.append(f"Período: {meses_nome[mes-1]}/{ano}\n")

    emojis = {
        "alimentacao": "🍔", "transporte": "🚗", "lazer": "🎮",
        "saude": "💊", "moradia": "🏠", "educacao": "📚",
        "roupas": "👕", "outros": "📦"
    }

    for cat, itens in sorted(categorias.items(), key=lambda x: -sum(i[1] for i in x[1])):
        subtotal = sum(i[1] for i in itens)
        emoji = emojis.get(cat, "📦")
        linhas.append(f"{emoji} *{cat.capitalize()}* → R$ {subtotal:.2f}")
        for desc, val in itens:
            linhas.append(f"  • {desc}: R$ {val:.2f}")

    linhas.append(f"\n💰 *Total: R$ {total:.2f}*")

    limite = buscar_limite(telefone)
    if limite > 0:
        restante = limite - total
        if restante >= 0:
            linhas.append(f"✅ Limite: R$ {limite:.2f} | Sobra: R$ {restante:.2f}")
        else:
            linhas.append(f"⚠️ Limite: R$ {limite:.2f} | Passou: R$ {abs(restante):.2f}")

    return "\n".join(linhas)

# ─── WEBHOOK TWILIO ────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    telefone = request.form.get("From", "")
    mensagem = request.form.get("Body", "").strip()

    resp = MessagingResponse()

    if not mensagem:
        return str(resp)

    # Verifica se usuário tem acesso
    if not usuario_ativo(telefone):
        resp.message("Olá! Para usar o CashFlow AI, acesse o link e assine: https://seusite.com.br\n\nQualquer dúvida é só chamar! 😊")
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

                # Verifica limite
                total_mes = buscar_total_mes(telefone)
                limite = buscar_limite(telefone)
                alerta = ""
                if limite > 0 and total_mes >= limite * 0.9:
                    alerta = f"\n\n⚠️ Atenção: você já usou R$ {total_mes:.2f} do seu limite de R$ {limite:.2f}!"

                resposta = resultado.get("resposta", f"✅ Anotado! {descricao}: R$ {valor:.2f}")
                resp.message(resposta + alerta)
            else:
                resp.message("Não consegui identificar o valor. Pode me dizer quanto foi? Ex: *mercado 85,50*")

        elif intencao == "relatorio":
            relatorio = gerar_relatorio(telefone)
            resp.message(relatorio)

        elif intencao == "limite":
            valor = resultado.get("valor", 0)
            if valor > 0:
                salvar_limite(telefone, valor)
                resp.message(f"✅ Limite mensal definido: R$ {valor:.2f}\n\nVou te avisar quando estiver chegando perto! 🐷")
            else:
                resp.message("Qual valor você quer como limite mensal? Ex: *limite 2000*")

        elif intencao == "ajuda":
            resp.message(
                "👋 Olá! Sou o *CashFlow AI*, seu assistente financeiro no WhatsApp!\n\n"
                "É simples assim:\n\n"
                "💬 *Registrar gasto:*\nDigite o que gastou e o valor\n"
                "Ex: _uber 27_ ou _almoço 35,90_\n\n"
                "📊 *Ver relatório:*\nDigite _relatório_ ou _resumo_\n\n"
                "⚠️ *Definir limite mensal:*\nDigite _limite 2000_\n\n"
                "Vamos começar? Me conta um gasto de hoje! 😄"
            )
        else:
            resp.message(resultado.get("resposta", "Não entendi. Tente: _mercado 50_ para registrar um gasto, ou _relatório_ para ver o resumo."))

    except Exception as e:
        print(f"Erro: {e}")
        resp.message("Ops, tive um problema aqui. Tenta de novo! Se persistir, manda _ajuda_ 🐷")

    return str(resp)

@app.route("/", methods=["GET"])
def health():
    return "CashFlow AI está rodando! 🚀"

# Inicializa o banco sempre que o servidor sobe
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
