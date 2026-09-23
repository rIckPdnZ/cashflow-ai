"""Groq: classificar mensagens e transcrever audios."""
import json
import re

from groq import Groq

from cashflow import config
from cashflow.util import para_valor, sem_acento

log = config.log

# Timeout curto e 1 retry: uma IA lenta nao pode travar o worker do gunicorn.
groq_client = Groq(api_key=config.GROQ_API_KEY, timeout=12, max_retries=1)


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
consulta -> pergunta sobre quanto gastou ou recebeu com algo (descricao = o que buscar)
dica -> dica financeira
ajuda -> comandos
oi -> saudacoes sem numero
confirmacao -> pix ambiguo
duvida -> simulacao/calculo, nao registrar
outro -> qualquer outra coisa

TIPO:
entrada: salario, pagamento recebido, freela, pix recebido, transferencia recebida, retorno investimento, rendimento, lucro, ganhei, recebi, dividendo, venda, vendi, estorno, reembolso, cashback, devolucao
saida: compras, despesas, contas, servicos, assinaturas, pix enviado, investimento, investi, apliquei, aporte

AMBIGUO:
"pix 100" sem contexto -> {"intencao":"confirmacao","valor":100.0}

NAO REGISTRAR:
calcular, simular, prever, quanto ficaria, se eu gastar -> duvida

CONSULTA:
"quanto gastei com uber" -> {"intencao":"consulta","descricao":"uber"}

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
    "limite", "editar", "apagar", "consulta", "dica", "ajuda", "oi", "confirmacao", "duvida", "outro",
    "erro",  # interna: a API da IA falhou
}

CATEGORIAS = [
    "Alimentacao", "Transporte", "Lazer", "Saude", "Moradia", "Educacao", "Beleza e Cuidados", "Roupas",
    "Servicos", "Investimentos", "Salario", "Freela", "Vendas", "Transferencia", "Outros",
]

_CATEGORIA_POR_CHAVE = {c.lower(): c for c in CATEGORIAS}

MAX_DESCRICAO = 120

# Outros nomes para a categoria inteira ("limite comida 800"). So palavras de categoria:
# "uber" ou "gasolina" sao lancamentos especificos, nao o Transporte todo.
SINONIMOS_CATEGORIA = {
    "comida": "Alimentacao", "alimentos": "Alimentacao", "mercado": "Alimentacao", "supermercado": "Alimentacao",
    "restaurante": "Alimentacao", "restaurantes": "Alimentacao",
    "locomocao": "Transporte", "transportes": "Transporte",
    "diversao": "Lazer", "passeios": "Lazer",
    "casa": "Moradia", "contas de casa": "Moradia",
    "estudos": "Educacao",
    "beleza": "Beleza e Cuidados", "cuidados": "Beleza e Cuidados", "cuidados pessoais": "Beleza e Cuidados",
    "roupa": "Roupas", "vestuario": "Roupas",
    "servico": "Servicos", "assinaturas": "Servicos",
    "investimento": "Investimentos", "salarios": "Salario", "freelas": "Freela", "venda": "Vendas",
    "transferencias": "Transferencia", "outro": "Outros",
}


def normalizar_categoria(categoria) -> str:
    # "Alimentação", "alimentacao" e "Alimentacao" sao a mesma categoria no extrato.
    nome = str(categoria or "").strip()
    if not nome:
        return "Outros"
    return _CATEGORIA_POR_CHAVE.get(sem_acento(nome).lower(), nome)


def categoria_por_nome(texto: str):
    # Categoria oficial a partir do que o usuario escreveu, ou None.
    chave = " ".join(sem_acento(str(texto or "")).lower().split())
    return _CATEGORIA_POR_CHAVE.get(chave) or SINONIMOS_CATEGORIA.get(chave)


def normalizar_ia(dados: dict) -> dict:
    # IAs pequenas mandam null, "Gasto", "saída", "posso gastar"... aqui tudo vira o formato esperado.
    intencao = sem_acento(str(dados.get("intencao") or "outro")).strip().lower().replace(" ", "_")
    tipo = sem_acento(str(dados.get("tipo") or "saida")).strip().lower()

    return {
        "intencao": intencao if intencao in INTENCOES else "outro",
        "descricao": str(dados.get("descricao") or "").strip()[:MAX_DESCRICAO],
        "valor": para_valor(dados.get("valor")),
        "tipo": tipo if tipo in ("entrada", "saida") else "saida",
        "categoria": normalizar_categoria(dados.get("categoria"))[:40],
    }


def chamar_ia(mensagem: str) -> dict:
    try:
        res = groq_client.chat.completions.create(
            model=config.GROQ_MODEL,
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


def transcrever(audio: bytes):
    # Audio do WhatsApp (ogg/opus) -> texto. None se falhar.
    try:
        res = groq_client.audio.transcriptions.create(
            file=("audio.ogg", audio),
            model=config.GROQ_MODEL_AUDIO,
            language="pt",
            response_format="json",
            temperature=0,
        )
        texto = (getattr(res, "text", None) or "").strip()
        return texto or None
    except Exception as e:
        log.error("Erro ao transcrever audio: %s", e)
        return None
