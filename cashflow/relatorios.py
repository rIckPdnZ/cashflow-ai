"""Textos dos relatorios enviados no WhatsApp."""
import csv
import io
from datetime import date

from cashflow import db
from cashflow.ia import categoria_por_nome, normalizar_categoria
from cashflow.util import (MESES, barra, fim_mes, fmt, fmt_sinal, hoje, nome_mes, periodo_hoje, periodo_mes,
                           sem_acento, somar_meses, ultimo_dia)

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

# WhatsApp aguenta mensagens longas, mas ninguem le 500 linhas: o resto vai no _exportar_.
MAX_EXTRATO = 150


def ecat(cat):
    return EMOJI_CAT.get(sem_acento(cat).lower(), "📦")


def totais(txs):
    ent = sum(float(t["valor"]) for t in txs if t["tipo"] == "entrada")
    sai = sum(float(t["valor"]) for t in txs if t["tipo"] == "saida")
    return ent, sai, ent - sai


def saidas_por_categoria(txs):
    cats = {}
    for t in txs:
        if t["tipo"] == "saida":
            cat = normalizar_categoria(t["categoria"])
            cats[cat] = cats.get(cat, 0.0) + float(t["valor"])
    return sorted(cats.items(), key=lambda x: -x[1])


def _marca(t):
    return t["descricao"].capitalize() + (" 🔁" if t.get("recorrente_id") else "")


# ─── Alertas de limite ────────────────────────────────────────────────────────

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


def alerta_categoria(telefone, categoria, txs_mes):
    limite = db.limites_categoria(telefone).get(categoria)

    if not limite:
        return ""

    gasto = sum(float(t["valor"]) for t in txs_mes
                if t["tipo"] == "saida" and normalizar_categoria(t["categoria"]) == categoria)
    porc = int(gasto / limite * 100)

    if gasto > limite:
        return "\n\n🚨 *Limite de {} estourado!* Passou {} do teto de {}.".format(categoria, fmt(gasto - limite), fmt(limite))
    elif porc >= 90:
        return "\n\n⚠️ *Atencao!* {}% do limite de {} usado.".format(porc, categoria)
    elif porc >= 75:
        return "\n\n💛 {}% do limite de {} usado — fica de olho!".format(porc, categoria)

    return ""


def alertas_depois_de_gastar(telefone, categorias):
    # Limite geral + limite de cada categoria que recebeu uma saida agora.
    ini, fim = periodo_mes()
    txs = db.buscar_transacoes(telefone, ini, fim)
    texto = alerta_limite(totais(txs)[1], db.buscar_limite(telefone))
    for cat in sorted(set(categorias)):
        texto += alerta_categoria(telefone, cat, txs)
    return texto


# ─── Relatorios ───────────────────────────────────────────────────────────────

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


def comparar_com_mes_anterior(telefone, ini, fim, sai):
    # Mes fechado compara com o mes anterior inteiro; mes corrente, com os mesmos dias do anterior.
    ini_ant = somar_meses(ini, -1)
    mes_inteiro = fim == fim_mes(fim)
    fim_ant = fim_mes(ini_ant) if mes_inteiro else date(
        ini_ant.year, ini_ant.month, min(fim.day, ultimo_dia(ini_ant.year, ini_ant.month)))

    sai_ant = totais(db.buscar_transacoes(telefone, ini_ant, fim_ant))[1]

    if sai_ant <= 0:
        return ""

    dif = (sai - sai_ant) / sai_ant * 100
    mes = nome_mes(ini_ant)
    quando = "" if mes_inteiro else " no mesmo periodo"

    if abs(dif) < 1:
        return "➡️ Mesmo gasto de {}{}.".format(mes, quando)
    if dif > 0:
        return "📈 Voce gastou {}% a mais que em {}{}.".format(round(dif), mes, quando)
    return "📉 Voce gastou {}% a menos que em {}{}.".format(round(-dif), mes, quando)


def relatorio_mes(telefone, ini, fim, titulo, limite=0.0):
    txs = db.buscar_transacoes(telefone, ini, fim)

    if not txs:
        if fim >= hoje():
            return "*{}*\n\nNenhuma movimentacao ainda.\n_mercado 85_ para comecar.".format(titulo)
        return "*{}*\n\nNenhuma movimentacao nesse periodo.".format(titulo)

    ent, sai, sal = totais(txs)

    linhas = [
        "*{}*\n".format(titulo),
        "💚 Entradas:  {}".format(fmt(ent)),
        "🔴 Saidas:    {}".format(fmt(sai)),
        "━━━━━━━━━━━━━━",
        "{} *Saldo: {}*".format("🟢" if sal >= 0 else "🔴", fmt_sinal(sal)),
    ]

    cats = saidas_por_categoria(txs)

    if cats:
        linhas.append("\n📊 *Onde foi o dinheiro:*")
        for cat, total in cats[:6]:
            linhas.append("{} {} — {} ({}%)".format(ecat(cat), cat, fmt(total), round(total / sai * 100)))
        if len(cats) > 6:
            resto = sum(total for _, total in cats[6:])
            linhas.append("📦 Demais — {} ({}%)".format(fmt(resto), round(resto / sai * 100)))

    comparacao = comparar_com_mes_anterior(telefone, ini, fim, sai)
    if comparacao:
        linhas.append("\n" + comparacao)

    top = sorted([t for t in txs if t["tipo"] == "saida"], key=lambda x: -float(x["valor"]))[:3]
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

    subtotais = dict(saidas_por_categoria(txs))
    exibidos = txs[-MAX_EXTRATO:]

    cats = {}
    for t in exibidos:
        if t["tipo"] == "saida":
            cats.setdefault(normalizar_categoria(t["categoria"]), []).append(t)

    linhas = [
        "🧾 *{}*".format(titulo),
        "_{} → {}_\n".format(label_ini, label_fim),
    ]

    if len(txs) > MAX_EXTRATO:
        linhas.append("_Mostrando os ultimos {} de {} lancamentos. Todos: exportar_\n".format(MAX_EXTRATO, len(txs)))

    entradas = [t for t in exibidos if t["tipo"] == "entrada"]

    if entradas:
        linhas.append("💚 *Entradas*")
        for t in entradas:
            linhas.append("  {} · {}  `{}`".format(_marca(t), fmt(float(t["valor"])), db.id_curto(t["id"])))
        linhas.append("  *Total: {}*\n".format(fmt(ent)))

    if cats:
        linhas.append("🔴 *Saidas*")
        for cat in sorted(cats, key=lambda c: -subtotais.get(c, 0)):
            linhas.append("\n{} *{}* — {}".format(ecat(cat), cat, fmt(subtotais.get(cat, 0))))

            for t in cats[cat]:
                linhas.append("  {} · {}  `{}`".format(_marca(t), fmt(float(t["valor"])), db.id_curto(t["id"])))

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
    txs = db.buscar_transacoes(telefone, ini, fim)
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
    txs = db.buscar_transacoes(telefone, ini, fim)

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
    limite = db.buscar_limite(telefone)

    if limite <= 0:
        return "Voce ainda nao definiu um limite.\n\nManda: _limite 2000_"

    ini, fim = periodo_mes()
    txs = db.buscar_transacoes(telefone, ini, fim)

    _, sai, _ = totais(txs)

    rest = limite - sai
    # Conta o dia de hoje: no ultimo dia do mes ainda resta 1 dia, nao 0.
    dias = ultimo_dia(fim.year, fim.month) - fim.day + 1

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
    txs = db.buscar_transacoes(telefone, ini, fim)
    ent, sai, sal = totais(txs)
    emoji = "🟢" if sal >= 0 else "🔴"
    hoje_str = ini.strftime("%d/%m")

    if not txs:
        return "Hoje ({})\n\nNenhuma movimentacao ainda.".format(hoje_str)

    return (
        "📊 *Hoje ({})*\n\n"
        "💚 Entradas:  {}\n"
        "🔴 Saidas:    {}\n"
        "━━━━━━━━━━━━━━\n"
        "{} Saldo: {}"
    ).format(hoje_str, fmt(ent), fmt(sai), emoji, fmt_sinal(sal))


def relatorio_ano(telefone, ano):
    h = hoje()
    ini = date(ano, 1, 1)

    if ini > h:
        return "📅 {} ainda nao comecou.".format(ano)

    por_mes = {}
    for r in db.totais_por_mes(telefone, ini, min(date(ano, 12, 31), h)):
        por_mes.setdefault(r["mes"], {"entrada": 0.0, "saida": 0.0})[r["tipo"]] = float(r["total"])

    if not por_mes:
        return "📅 *Resumo de {}*\n\nNenhuma movimentacao nesse ano.".format(ano)

    linhas = ["📅 *Resumo de {}*\n".format(ano)]

    for mes in sorted(por_mes):
        v = por_mes[mes]
        linhas.append("*{}*  🔴 {}  💚 {}".format(MESES[mes.month - 1][:3], fmt(v["saida"]), fmt(v["entrada"])))

    ent = sum(v["entrada"] for v in por_mes.values())
    sai = sum(v["saida"] for v in por_mes.values())

    linhas += [
        "━━━━━━━━━━━━━━",
        "💚 Entradas: {}".format(fmt(ent)),
        "🔴 Saidas:   {}".format(fmt(sai)),
        "{} *Saldo: {}*".format("🟢" if ent >= sai else "🔴", fmt_sinal(ent - sai)),
        "\n📊 Media de saidas: {}/mes".format(fmt(sai / len(por_mes))),
    ]

    if len(por_mes) > 1:
        mais_caro = max(por_mes, key=lambda m: por_mes[m]["saida"])
        linhas.append("📈 Mes mais caro: {} ({})".format(MESES[mais_caro.month - 1], fmt(por_mes[mais_caro]["saida"])))

    return "\n".join(linhas)


def _quando(rotulo):
    if rotulo in ("hoje", "ontem", "anteontem"):
        return rotulo
    if rotulo == "esta semana":
        return "nesta semana"
    if rotulo == "semana passada":
        return "na semana passada"
    return "em " + rotulo.lower()


def consulta(telefone, termo, tipo, ini, fim, rotulo):
    # "quanto gastei com uber mes passado": soma, media e os ultimos lancamentos.
    txs = [t for t in db.buscar_transacoes(telefone, ini, fim) if t["tipo"] == tipo]
    categoria = None

    if termo:
        # "uber" procura "uber" na descricao (nao a categoria Transporte inteira, que tem gasolina);
        # "alimentacao" ou "comida" (sem nenhum lancamento com esse nome) usam a categoria.
        chave = sem_acento(termo).lower()
        por_descricao = [t for t in txs if chave in sem_acento(t["descricao"]).lower()]
        categoria = categoria_por_nome(termo)

        if categoria and (not por_descricao or sem_acento(categoria).lower() == chave):
            txs = [t for t in txs if normalizar_categoria(t["categoria"]) == categoria]
        else:
            categoria = None
            txs = por_descricao

    if not txs:
        coisa = "Nenhuma entrada" if tipo == "entrada" else "Nenhum gasto"
        com = " com _{}_".format(termo) if termo else ""
        return "🔎 {}{} {}.".format(coisa, com, _quando(rotulo))

    nome = categoria or (termo.capitalize() if termo else ("Entradas" if tipo == "entrada" else "Saidas"))
    total = sum(float(t["valor"]) for t in txs)
    emoji = "💚" if tipo == "entrada" else "🔴"

    linhas = [
        "🔎 *{} — {}*\n".format(nome, rotulo[:1].upper() + rotulo[1:]),
        "{} *{}* em {} lancamento{}".format(emoji, fmt(total), len(txs), "" if len(txs) == 1 else "s"),
    ]

    if len(txs) > 1:
        linhas.append("📊 Media: {}".format(fmt(total / len(txs))))

    linhas.append("\n*Ultimos:*")
    for t in txs[-5:][::-1]:
        linhas.append("{} · {} · {}".format(t["data"].strftime("%d/%m"), t["descricao"].capitalize(), fmt(float(t["valor"]))))

    return "\n".join(linhas)


def dica_personalizada(telefone):
    # A "analise de onde voce pode economizar" prometida no site. None = poucos dados (usa dica generica).
    h = hoje()
    ini = h.replace(day=1)
    txs = db.buscar_transacoes(telefone, ini, h)
    saidas = [t for t in txs if t["tipo"] == "saida"]

    if len(saidas) < 3:
        return None

    total = sum(float(t["valor"]) for t in saidas)
    cats = saidas_por_categoria(txs)
    cat, valor = cats[0]

    linhas = [
        "💡 *Sua dica do mes*\n",
        "{} *{}* e onde mais sai dinheiro: {} ({}% das saidas). Cortando 20%, sobram {} por mes.".format(
            ecat(cat), cat, fmt(valor), round(valor / total * 100), fmt(valor * 0.2)),
    ]

    # Categoria que mais cresceu em relacao ao mesmo periodo do mes passado
    ini_ant = somar_meses(ini, -1)
    fim_ant = date(ini_ant.year, ini_ant.month, min(h.day, ultimo_dia(ini_ant.year, ini_ant.month)))
    antes = dict(saidas_por_categoria(db.buscar_transacoes(telefone, ini_ant, fim_ant)))
    subidas = [(c, v, antes[c]) for c, v in cats if antes.get(c, 0) > 0 and v - antes[c] >= 30 and v >= antes[c] * 1.2]

    if subidas:
        c, v, a = max(subidas, key=lambda x: x[1] - x[2])
        linhas.append("\n📈 *{}* subiu {}% em relacao a {} no mesmo periodo (+{}).".format(
            c, round((v / a - 1) * 100), nome_mes(ini_ant), fmt(v - a)))

    pequenos = [float(t["valor"]) for t in saidas if float(t["valor"]) <= 30]
    if len(pequenos) >= 8:
        linhas.append("\n☕ {} gastos pequenos (ate R$ 30) ja somam {}. Sao os que mais passam despercebidos.".format(
            len(pequenos), fmt(sum(pequenos))))

    if h.day >= 5:
        projecao = total / h.day * ultimo_dia(h.year, h.month)
        limite = db.buscar_limite(telefone)
        if limite > 0:
            linhas.append("\n🔮 No ritmo atual, voce fecha o mes com {} em saidas — {} do seu limite de {}.".format(
                fmt(projecao), "acima" if projecao > limite else "dentro", fmt(limite)))
        else:
            linhas.append("\n🔮 No ritmo atual, voce fecha o mes com {} em saidas. Que tal um teto? _limite {}_".format(
                fmt(projecao), int(round(projecao * 0.9, -1))))

    return "\n".join(linhas)


# ─── Limites, metas e contas fixas ────────────────────────────────────────────

def _linha_limite(nome, gasto, limite):
    porc = int(gasto / limite * 100)
    resto = limite - gasto
    situacao = "sobram {}".format(fmt(resto)) if resto >= 0 else "🚨 passou {}".format(fmt(-resto))
    return "*{}:* {} de {}\n[{}] {}% · {}\n".format(nome, fmt(gasto), fmt(limite), barra(porc), porc, situacao)


def texto_limites(telefone):
    geral = db.buscar_limite(telefone)
    por_categoria = db.limites_categoria(telefone)

    if not geral and not por_categoria:
        return ("🎯 Voce ainda nao tem limites.\n\n"
                "_limite 2000_ → teto do mes\n"
                "_limite alimentacao 800_ → teto de uma categoria")

    ini, fim = periodo_mes()
    txs = db.buscar_transacoes(telefone, ini, fim)
    gastos = dict(saidas_por_categoria(txs))

    linhas = ["🎯 *Seus limites — {}*\n".format(nome_mes(ini))]

    if geral:
        linhas.append(_linha_limite("Geral", totais(txs)[1], geral))

    for cat, limite in por_categoria.items():
        linhas.append(_linha_limite("{} {}".format(ecat(cat), cat), gastos.get(cat, 0.0), limite))

    linhas.append("_limite alimentacao 800_ · _remover limite alimentacao_")
    return "\n".join(linhas)


def texto_meta(meta):
    alvo, guardado = float(meta["alvo"]), float(meta["guardado"])
    porc = int(guardado / alvo * 100) if alvo > 0 else 0
    falta = alvo - guardado
    situacao = "faltam {}".format(fmt(falta)) if falta > 0 else "✅ meta batida!"
    return "*{}* — {} de {}\n[{}] {}% · {}".format(meta["nome"], fmt(guardado), fmt(alvo), barra(porc), porc, situacao)


def texto_metas(metas):
    if not metas:
        return "🎯 Voce ainda nao tem metas.\n\nCrie uma: _meta viagem 5000_\nDepois e so ir guardando: _guardei 200 viagem_"

    linhas = ["🎯 *Suas metas*\n"]
    for meta in metas:
        linhas.append(texto_meta(meta) + "\n")
    linhas.append("_guardei 200 viagem_ · _tirei 50 viagem_ · _apagar meta viagem_")
    return "\n".join(linhas)


def texto_fixos(recorrentes):
    if not recorrentes:
        return ("🔁 Voce ainda nao tem contas fixas.\n\n"
                "Crie uma: _fixo aluguel 1500 dia 5_\n"
                "Eu lanco sozinho todo mes. 😉")

    linhas = ["🔁 *Contas fixas*\n"]
    for i, r in enumerate(recorrentes, 1):
        linhas.append("{}. {} {} — {} · todo dia {}".format(
            i, "💚" if r["tipo"] == "entrada" else "🔴", r["descricao"], fmt(float(r["valor"])), r["dia"]))

    saidas = sum(float(r["valor"]) for r in recorrentes if r["tipo"] == "saida")
    entradas = sum(float(r["valor"]) for r in recorrentes if r["tipo"] == "entrada")

    linhas.append("")
    if saidas:
        linhas.append("🔴 Saidas fixas: {}/mes".format(fmt(saidas)))
    if entradas:
        linhas.append("💚 Entradas fixas: {}/mes".format(fmt(entradas)))

    linhas.append("\n_apagar fixo 1_ · _fixo netflix 55 dia 10_")
    return "\n".join(linhas)


# ─── Exportacao ───────────────────────────────────────────────────────────────

def gerar_csv(txs) -> bytes:
    # Separador ";" e virgula decimal: abre direto no Excel em portugues.
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Data", "Descricao", "Valor", "Tipo", "Categoria", "ID"])

    for t in txs:
        valor = float(t["valor"]) * (1 if t["tipo"] == "entrada" else -1)
        descricao = t["descricao"]
        if descricao[:1] in ("=", "+", "-", "@"):
            descricao = "'" + descricao  # evita que o Excel trate como formula
        w.writerow([
            t["data"].strftime("%d/%m/%Y"),
            descricao,
            "{:.2f}".format(valor).replace(".", ","),
            "Entrada" if t["tipo"] == "entrada" else "Saida",
            normalizar_categoria(t["categoria"]),
            db.id_curto(t["id"]),
        ])

    return ("﻿" + buf.getvalue()).encode("utf-8")
