"""Entende comandos e partes da mensagem sem IA: datas, parcelas, varios itens, periodos.

Tudo aqui e deterministico: o que casa com uma regra nunca depende do humor da IA.
"""
import re
from datetime import date, timedelta

from cashflow.util import (MES_POR_NOME, fim_mes, inicio_mes, nome_mes, normalizar, para_valor,
                           sem_acento, somar_meses, ultimo_dia)

MESES_RE = "janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro"
VALOR_RE = r"(?:r\$\s*)?(?P<v>\d[\d.,]*)(?:\s*reais)?"

# Comandos exatos (os da _ajuda_): resposta mais rapida e sem erro de interpretacao.
ATALHOS = {
    "saldo": "saldo",
    "extrato": "extrato",
    "extrato completo": "extrato",
    "mes": "relatorio",
    "relatorio": "relatorio",
    "relatorio do mes": "relatorio",
    "relatorio mensal": "relatorio",
    "resumo do mes": "relatorio",
    "semana": "semana",
    "relatorio semanal": "semana",
    "resumo da semana": "semana",
    "relatorio da semana": "semana",
    "hoje": "hoje",
    "resumo": "resumo",
    "top": "top",
    "ajuda": "ajuda",
    "comandos": "ajuda",
    "help": "ajuda",
    "dica": "dica",
    "dicas": "dica",
    "dica do mes": "dica",
    "me da uma dica": "dica",
    "posso gastar": "posso_gastar",
    "quanto posso gastar": "posso_gastar",
}

LISTAS = {
    "limites": "limites", "limite": "limites", "meus limites": "limites", "orcamento": "limites",
    "orcamentos": "limites",
    "metas": "metas", "meta": "metas", "minhas metas": "metas", "objetivos": "metas",
    "fixos": "fixos", "fixas": "fixos", "contas fixas": "fixos", "gastos fixos": "fixos",
    "minhas contas fixas": "fixos", "recorrentes": "fixos", "meus fixos": "fixos",
    "exportar": "exportar", "exporta": "exportar", "planilha": "exportar", "excel": "exportar", "csv": "exportar",
    "exportar extrato": "exportar", "exportar planilha": "exportar", "exportar dados": "exportar",
    "baixar extrato": "exportar", "baixar planilha": "exportar",
    "desfazer": "desfazer", "desfaz": "desfazer",
}


def _num(m):
    return para_valor(m.group("v"))


def _mes_ano(mes_nome, ano_txt, h):
    # "agosto" sem ano = o agosto mais recente (em fevereiro, "dezembro" e o do ano passado).
    mes = MES_POR_NOME[mes_nome]
    if ano_txt:
        return mes, int(ano_txt)
    return mes, h.year if mes <= h.month else h.year - 1


def periodo_do_mes(mes, ano, h):
    ini = date(ano, mes, 1)
    fim = fim_mes(ini)
    return ini, min(fim, h) if ini <= h <= fim else fim


# ─── Periodos em perguntas ("quanto gastei com uber mes passado") ─────────────

def extrair_periodo(t, h):
    # t ja normalizado. Retorna (ini, fim, rotulo, t sem o periodo). Sem periodo = mes atual.
    segunda = h - timedelta(days=h.weekday())
    mes_passado = somar_meses(inicio_mes(h), -1)

    regras = [
        (r"\b(?:de\s+|no\s+dia\s+de\s+)?hoje\b", lambda m: (h, h, "hoje")),
        (r"\b(?:de\s+)?anteontem\b", lambda m: (h - timedelta(days=2), h - timedelta(days=2), "anteontem")),
        (r"\b(?:de\s+)?ontem\b", lambda m: (h - timedelta(days=1), h - timedelta(days=1), "ontem")),
        (r"\b(?:na\s+|da\s+)?semana\s+passada\b",
         lambda m: (segunda - timedelta(days=7), segunda - timedelta(days=1), "semana passada")),
        (r"\b(?:n?essa|n?esta|na|da)\s+semana\b", lambda m: (segunda, h, "esta semana")),
        (r"\b(?:no\s+|do\s+)?mes\s+passado\b", lambda m: (mes_passado, fim_mes(mes_passado), nome_mes(mes_passado))),
        (r"\b(?:n?esse|n?este|no|do)\s+mes\b", lambda m: (inicio_mes(h), h, nome_mes(h))),
        (r"\b(?:n?esse|n?este|no|do)\s+ano\b", lambda m: (date(h.year, 1, 1), h, str(h.year))),
        (r"\b(?:(?:em|de|no\s+mes\s+de|no|do\s+mes\s+de)\s+)?(?P<mes>{})(?:\s+(?:de\s+)?(?P<ano>\d{{4}}))?\b".format(MESES_RE),
         lambda m: _periodo_nomeado(m, h)),
    ]

    for padrao, periodo in regras:
        m = re.search(padrao, t)
        if m:
            ini, fim, rotulo = periodo(m)
            resto = " ".join((t[:m.start()] + " " + t[m.end():]).split())
            return ini, fim, rotulo, resto

    return inicio_mes(h), h, nome_mes(h), t


def _periodo_nomeado(m, h):
    mes, ano = _mes_ano(m.group("mes"), m.group("ano"), h)
    ini, fim = periodo_do_mes(mes, ano, h)
    rotulo = nome_mes(ini) if ano == h.year else nome_mes(ini, com_ano=True)
    return ini, fim, rotulo


# ─── Comandos ─────────────────────────────────────────────────────────────────

def interpretar_comando(texto, h):
    # Retorna um dict {"acao": ...} ou None (ai a mensagem vai para a IA).
    t = normalizar(texto)

    if not t:
        return None

    if t in ATALHOS:
        return {"acao": "intencao", "intencao": ATALHOS[t]}

    if t in LISTAS:
        if LISTAS[t] == "desfazer":
            return {"acao": "intencao", "intencao": "apagar", "descricao": "ultimo"}
        return {"acao": LISTAS[t]}

    for regra in (_apagar_dados, _lembretes, _admin, _editar_categoria, _limite, _meta, _fixo, _relatorio_periodo,
                  _consulta, _apagar_editar):
        comando = regra(t, h)
        if comando:
            return comando

    return None


def _apagar_dados(t, h):
    if re.fullmatch(r"(?:apagar|apaga|excluir|exclui|deletar|deleta|remover)\s+(?:todos\s+)?(?:os\s+)?"
                    r"(?:meus\s+|minha\s+)?(?:dados|conta|tudo|historico|cadastro)", t):
        return {"acao": "apagar_dados"}


def _lembretes(t, h):
    m = re.fullmatch(r"lembretes?\s+(on|off|ligar|ligado|ligados|desligar|desligado|desligados|ativar|desativar|sim|nao)", t)
    if m:
        return {"acao": "lembretes", "ligado": m.group(1) in ("on", "ligar", "ligado", "ligados", "ativar", "sim")}


def _admin(t, h):
    m = re.fullmatch(r"(ativar|desativar)\s+(?:assinatura\s+)?(?:de\s+|do\s+)?\+?([\d\s-]{10,})", t)
    if m:
        return {"acao": "admin_assinatura", "ativa": m.group(1) == "ativar", "numero": re.sub(r"\D", "", m.group(2))}


def _editar_categoria(t, h):
    m = re.fullmatch(r"(?:mudar|muda|trocar|troca|alterar|altera|corrigir|corrige|mover|move)\s+(?:a\s+)?categoria\s+"
                     r"(?:do\s+|da\s+|de\s+)?(?P<alvo>.+?)\s+(?:para|pra|p)\s+(?P<cat>.+)", t)
    if m:
        return {"acao": "editar_categoria", "alvo": m.group("alvo"), "categoria": m.group("cat")}


def _limite(t, h):
    m = re.fullmatch(r"(?:remover|remove|tirar|tira|apagar|apaga|excluir|cancelar|zerar)\s+(?:o\s+)?limite"
                     r"(?:\s+(?:de\s+|da\s+|do\s+|com\s+|em\s+|para\s+|pra\s+)?(?P<cat>.+))?", t)
    if m:
        return {"acao": "limite", "categoria": m.group("cat"), "valor": 0.0}

    m = re.fullmatch(r"(?:definir\s+)?limite\s+(?:mensal\s+|geral\s+)?(?:de\s+)?" + VALOR_RE, t)
    if m:
        return {"acao": "limite", "categoria": None, "valor": _num(m)}

    m = re.fullmatch(r"(?:definir\s+)?limite\s+(?:de\s+|da\s+|do\s+|com\s+|em\s+|para\s+|pra\s+)?(?P<cat>[a-z][a-z ]*?)\s+"
                     r"(?:de\s+)?" + VALOR_RE, t)
    if m:
        return {"acao": "limite", "categoria": m.group("cat"), "valor": _num(m)}


def _meta(t, h):
    m = re.fullmatch(r"(?:apagar|apaga|excluir|remover|deletar|cancelar)\s+(?:a\s+)?meta\s+(?:de\s+|da\s+|do\s+)?(?P<nome>.+)", t)
    if m:
        return {"acao": "meta_apagar", "nome": m.group("nome")}

    m = re.fullmatch(r"(?:nova\s+|criar\s+)?meta\s+(?:de\s+|da\s+|do\s+|para\s+|pra\s+)?(?P<nome>.+?)\s+(?:de\s+)?" + VALOR_RE, t)
    if m:
        return {"acao": "meta_criar", "nome": m.group("nome"), "valor": _num(m)}

    destino = r"(?:\s+(?:na|no|pra|para|em|p|da|do|de)(?:\s+(?:a|o))?)?(?:\s+meta)?(?:\s+(?:de|da|do))?(?:\s+(?P<nome>.+))?"

    m = re.fullmatch(r"(?:guardar|guardei|guarda|poupar|poupei|depositar|depositei|juntar|juntei|reservar|reservei|"
                     r"separar|separei|coloquei|colocar)\s+" + VALOR_RE + destino, t)
    if m:
        return {"acao": "meta_mover", "sinal": 1, "valor": _num(m), "nome": m.group("nome") or ""}

    m = re.fullmatch(r"(?:retirar|retirei|tirar|tirei|resgatar|resgatei|sacar|saquei|usei|usar)\s+" + VALOR_RE + destino, t)
    if m:
        return {"acao": "meta_mover", "sinal": -1, "valor": _num(m), "nome": m.group("nome") or ""}


def _fixo(t, h):
    prefixo = r"(?:fixo|fixa|conta fixa|gasto fixo|recorrente|mensal)"

    m = re.fullmatch(r"(?:apagar|apaga|excluir|remover|cancelar|deletar|parar)\s+(?:o\s+|a\s+)?"
                     r"(?:fixo|fixa|conta fixa|gasto fixo|recorrente)\s+(?:de\s+|da\s+|do\s+)?(?P<alvo>.+)", t)
    if m:
        return {"acao": "fixo_apagar", "alvo": m.group("alvo")}

    padroes = [
        prefixo + r"\s+(?P<resto>.+?)\s+(?:todo\s+)?dia\s+(?P<dia>\d{1,2})",
        prefixo + r"\s+(?:todo\s+)?dia\s+(?P<dia>\d{1,2})\s+(?P<resto>.+)",
        r"todo\s+dia\s+(?P<dia>\d{1,2})\s+(?P<resto>.+)",
        r"(?P<resto>.+?)\s+todo\s+dia\s+(?P<dia>\d{1,2})",
        r"(?P<resto>.+?)\s+todo\s+mes(?:\s+(?:no\s+)?dia\s+(?P<dia>\d{1,2}))?",
        prefixo + r"\s+(?P<resto>.+)",
    ]

    for padrao in padroes:
        m = re.fullmatch(padrao, t)
        if m:
            dia = int(m.groupdict().get("dia") or h.day)
            resto = m.group("resto").strip()
            if not re.search(r"\d", resto) or not 1 <= dia <= 31:
                return {"acao": "fixo_ajuda"}
            return {"acao": "fixo_criar", "texto": resto, "dia": dia}


def _relatorio_periodo(t, h):
    tipo = r"(?:(?P<tipo>extrato|relatorio|resumo|balanco|gastos)\s+)?(?:(?:do|de|da)\s+)?"

    m = re.fullmatch(tipo + r"mes\s+passado", t)
    if m:
        ini = somar_meses(inicio_mes(h), -1)
        return _periodo(m, ini, fim_mes(ini), nome_mes(ini, com_ano=ini.year != h.year))

    m = re.fullmatch(tipo + r"(?:mes\s+(?:de\s+)?)?(?P<mes>{})(?:\s+(?:de\s+)?(?P<ano>\d{{4}}))?".format(MESES_RE), t)
    if m:
        mes, ano = _mes_ano(m.group("mes"), m.group("ano"), h)
        ini, fim = periodo_do_mes(mes, ano, h)
        return _periodo(m, ini, fim, nome_mes(ini, com_ano=ano != h.year))

    m = re.fullmatch(tipo + r"(?:dia\s+de\s+)?ontem", t)
    if m:
        d = h - timedelta(days=1)
        return _periodo(m, d, d, "Ontem — {}".format(d.strftime("%d/%m")))

    m = re.fullmatch(tipo + r"(?:(?:dessa|desta|nessa|nesta|essa|esta)\s+)?semana", t)
    if m:
        segunda = h - timedelta(days=h.weekday())
        return _periodo(m, segunda, h, "Esta semana ({} → {})".format(segunda.strftime("%d/%m"), h.strftime("%d/%m")))

    m = re.fullmatch(tipo + r"semana\s+passada", t)
    if m:
        segunda = h - timedelta(days=h.weekday())
        ini, fim = segunda - timedelta(days=7), segunda - timedelta(days=1)
        return _periodo(m, ini, fim, "Semana passada ({} → {})".format(ini.strftime("%d/%m"), fim.strftime("%d/%m")))

    m = (re.fullmatch(r"(?:(?:relatorio|resumo|balanco|extrato)\s+)?(?:(?:do|de)\s+)?(?:ano|anual)(?:\s+(?:de\s+)?(?P<ano>\d{4}))?", t)
         or re.fullmatch(r"(?:relatorio|resumo|balanco)\s+(?:de\s+|do\s+ano\s+(?:de\s+)?)?(?P<ano>\d{4})", t))
    if m:
        return {"acao": "ano", "ano": int(m.group("ano") or h.year)}


def _periodo(m, ini, fim, titulo):
    formato = "extrato" if m.group("tipo") == "extrato" else "relatorio"
    return {"acao": "periodo", "formato": formato, "ini": ini, "fim": fim, "titulo": titulo}


def _consulta(t, h):
    m = (re.fullmatch(r"(?:quanto|qto|qnt|quantos)\s+(?:reais\s+)?(?:que\s+)?(?:eu\s+)?"
                      r"(?P<verbo>gastei|gasto|paguei|recebi|ganhei|entrou|saiu|foi|foram|torrei)\b\s*(?P<resto>.*)", t)
         or re.fullmatch(r"(?P<verbo>gastos|despesas|saidas|entradas|recebimentos|ganhos)\s+"
                         r"(?:(?:com|de|em|no|na|do|da|dos|das)\s+)?(?P<resto>.+)", t))
    if not m:
        return None

    ini, fim, rotulo, resto = extrair_periodo(m.group("resto"), h)
    termo = re.sub(r"^(?:(?:com|em|no|na|nos|nas|de|do|da|dos|das|o|a|os|as|pra|para|pro)\s+)+", "", resto).strip()
    tipo = "entrada" if m.group("verbo") in ("recebi", "ganhei", "entrou", "entradas", "recebimentos", "ganhos") else "saida"

    return {"acao": "consulta", "termo": termo, "tipo": tipo, "ini": ini, "fim": fim, "rotulo": rotulo}


def _apagar_editar(t, h):
    # Vem por ultimo: "apagar meta X", "apagar fixo X" e "apagar meus dados" ja foram tratados antes.
    m = re.fullmatch(r"(?:apagar|apaga|apague|excluir|exclui|exclua|deletar|deleta|delete|remover|remove|remova)\s+(?P<alvo>.+)", t)
    if m:
        return {"acao": "intencao", "intencao": "apagar", "descricao": m.group("alvo")}

    m = re.fullmatch(r"(?:editar|edita|edite|corrigir|corrige|corrija|alterar|altera|altere)\s+(?P<alvo>.+?)"
                     r"(?:\s+(?:para\s+|pra\s+)?" + VALOR_RE + ")?", t)
    if m:
        return {"acao": "intencao", "intencao": "editar", "descricao": m.group("alvo"),
                "valor": _num(m) if m.group("v") else 0.0}


# ─── Partes de um lancamento ──────────────────────────────────────────────────

_DIAS_SEMANA = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "domingo": 6}


def _sem(texto, m):
    limpo = (texto[:m.start()] + " " + texto[m.end():])
    return " ".join(limpo.split()).strip(" ,;:-")


def extrair_data(texto, h):
    # "ontem uber 30" -> (ontem, "uber 30", None). Erro: "futuro" ou "invalida".
    m = re.search(r"(?:\b(?:no|na|em|dia)\s+)?\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b", texto, re.I)
    if m:
        dia, mes = int(m.group(1)), int(m.group(2))
        ano = int(m.group(3)) if m.group(3) else h.year
        ano += 2000 if ano < 100 else 0
        try:
            d = date(ano, mes, dia)
            if d > h:
                # Sem ano: "25/12" digitado em janeiro e o do ano passado. Perto de hoje e futuro mesmo.
                if m.group(3) or (d - h).days <= 60:
                    return None, _sem(texto, m), "futuro"
                d = date(ano - 1, mes, dia)
        except ValueError:
            return None, _sem(texto, m), "invalida"
        return d, _sem(texto, m), None

    m = re.search(r"\b(?:no\s+|em\s+)?dia\s+([1-9]|[12]\d|3[01])\b(?!\s*/)", texto, re.I)
    if m:
        dia = int(m.group(1))
        if dia <= h.day:
            return date(h.year, h.month, dia), _sem(texto, m), None
        # "dia 28" no dia 10 = dia 28 do mes passado
        anterior = somar_meses(inicio_mes(h), -1)
        return date(anterior.year, anterior.month, min(dia, ultimo_dia(anterior.year, anterior.month))), _sem(texto, m), None

    for palavra, dias in (("anteontem", 2), ("ontem", 1), ("hoje", 0)):
        m = re.search(r"\b(?:de\s+)?{}\b".format(palavra), texto, re.I)
        if m:
            return h - timedelta(days=dias), _sem(texto, m), None

    m = re.search(r"\b(?:(?:na|no|em)\s+)?(segunda|ter[cç]a|quarta|quinta|sexta|s[aá]bado|domingo)(?:[\s-]feira)?"
                  r"(?:\s+(passad[ao]))?\b", texto, re.I)
    if m and (re.match(r"(?:na|no|em)\s", m.group(0), re.I) or m.group(2)):
        alvo = _DIAS_SEMANA[sem_acento(m.group(1)).lower()]
        atras = (h.weekday() - alvo) % 7
        if atras == 0 and m.group(2):
            atras = 7
        return h - timedelta(days=atras), _sem(texto, m), None

    return None, texto, None


def extrair_parcelas(texto):
    # "tv 3000 em 10x" -> (10, False, "tv 3000"); "tv 10x de 300" -> (10, True, "tv 300").
    m = re.search(r"(?:\b(?:parcelad[oa]|dividid[oa])\s+)?(?:\bem\s+)?\b(\d{1,2})\s*(?:x|vezes)\b(?:\s+sem\s+juros)?"
                  r"(?:\s+de\s+(?:r\$\s*)?(\d[\d.,]*))?", texto, re.I)
    if not m:
        return None, False, texto

    n = int(m.group(1))
    if not 2 <= n <= 72:
        return None, False, texto

    if m.group(2):
        return n, True, " ".join((texto[:m.start()] + " " + m.group(2) + " " + texto[m.end():]).split())

    return n, False, _sem(texto, m)


_SEPARADORES = re.compile(
    r"\n+|;"
    r"|(?:(?<=\d)|(?<=reais)|(?<=real))\s*,\s+(?=[^\d\s])"  # "mercado 50, uber 20" / "50 reais, uber 20"
    r"|(?<=[^\d\s]),\s+(?=\d)"                              # "50 no mercado, 20 no uber" (audio)
    r"|(?:(?<=\d)|(?<=reais)|(?<=real))\s+e\s+(?=\D)"       # "mercado 50 e uber 20"
    r"|\s+e\s+(?=\d)",                                      # "50 no mercado e 20 no uber" (audio)
    re.I,
)


def dividir_itens(texto):
    # "mercado 50, uber 20 e farmacia 35" -> 3 itens. "almoco 35,90" continua sendo 1.
    # Com total no fim ("2 paes, 1 leite e 3 ovos por 20") e uma compra so.
    if re.search(r"\b(?:por|total|tudo|deu)\s+(?:de\s+)?(?:r\$\s*)?\d", texto, re.I):
        return [texto]

    partes = [p.strip(" ,.-") for p in _SEPARADORES.split(texto) if p and p.strip(" ,.-")]
    com_numero = [p for p in partes if re.search(r"\d", p)]
    return com_numero if len(com_numero) > 1 else [texto]
