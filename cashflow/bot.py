"""A conversa: decide o que fazer com cada mensagem do WhatsApp."""
import base64
import binascii
import random
import re
import time
from datetime import date, timedelta

from cashflow import config, db, ia, interpretar, relatorios, textos, whatsapp
from cashflow.util import (MESES, agora, chave_telefone, fim_mes, fmt, hoje, mascarar, normalizar, numero_br,
                           para_valor, periodo_hoje, periodo_mes, periodo_semana, sem_acento, somar_meses)

log = config.log

# Mais que isso numa mensagem so e provavelmente texto colado, nao uma lista de gastos.
MAX_ITENS = 10
MAX_TEXTO = 1500

_ultima_limpeza = 0.0


def enviar(telefone, texto):
    return whatsapp.enviar(telefone, texto)


# ─── Entrada ──────────────────────────────────────────────────────────────────

def processar(msg):
    # Chamado pelo webhook, dentro de db.sessao().
    tel = msg.telefone

    if msg.id and not db.marcar_processada("{}:{}".format(tel, msg.id)):
        log.info("Mensagem repetida ignorada (%s)", mascarar(tel))
        return

    _limpeza_periodica()

    usuario = db.registrar_usuario(tel)

    if not acesso_liberado(tel, usuario):
        avisar_assinatura(tel, usuario)
        return

    if usuario["novo"] and not db.tem_lancamentos(tel):
        enviar(tel, textos.boas_vindas())
        if msg.tipo == "texto" and msg.texto.lower().strip() in textos.PALAVRAS_BEM_VINDO:
            return

    avisar_fixos_lancados(tel, db.gerar_recorrentes(tel))

    if msg.tipo == "audio":
        texto = ouvir_audio(msg)
        if texto:
            responder(tel, texto)
        return

    if msg.tipo in ("imagem", "documento"):
        enviar(tel, textos.SEM_FOTO)
        return

    responder(tel, msg.texto)


def _limpeza_periodica():
    global _ultima_limpeza
    if time.monotonic() - _ultima_limpeza > 3600:
        _ultima_limpeza = time.monotonic()
        db.limpar_processadas()


def ouvir_audio(msg):
    tel = msg.telefone

    if msg.segundos > config.AUDIO_MAX_SEGUNDOS:
        enviar(tel, textos.AUDIO_LONGO.format(max(1, config.AUDIO_MAX_SEGUNDOS // 60)))
        return None

    audio = None
    if msg.base64:
        try:
            audio = base64.b64decode(msg.base64)
        except (binascii.Error, ValueError):
            audio = None
    if not audio and msg.id:
        audio = whatsapp.baixar_midia(msg.id)

    texto = ia.transcrever(audio) if audio else None

    if not texto:
        enviar(tel, textos.AUDIO_FALHOU)
        return None

    log.info("Audio transcrito (%s caracteres)", len(texto))
    enviar(tel, "🎤 _{}_".format(texto[:300]))
    return texto


def responder(tel, texto):
    h = hoje()

    if len(texto) > MAX_TEXTO:
        enviar(tel, "📝 Mensagem muito longa pra mim. Manda seus gastos em mensagens menores, tipo _mercado 85_.")
        return

    # Resposta a uma pergunta do bot ("Qual o novo valor?" -> "120")
    pendencia = db.obter_pendencia(tel)
    if pendencia:
        db.limpar_pendencia(tel)
        if resolver_pendencia(tel, pendencia, texto, h):
            return

    if texto.lower().strip() in textos.PALAVRAS_BEM_VINDO:
        enviar(tel, textos.boas_vindas())
        return

    comando = interpretar.interpretar_comando(texto, h)
    if comando and executar(tel, comando, texto, h):
        return

    itens = interpretar.dividir_itens(texto)
    if len(itens) > 1:
        registrar_varios(tel, texto, itens, h)
        return

    usar_ia(tel, texto, h)


def usar_ia(tel, texto, h, data_forcada=None, parcelas_forcadas=None):
    # Data e parcelas saem do texto antes da IA: "dia 15 mercado 50" nao confunde 15 com o valor.
    data, texto_limpo, erro_data = interpretar.extrair_data(texto, h)
    n, eh_parcela, texto_limpo = interpretar.extrair_parcelas(texto_limpo)

    if data_forcada:
        data, erro_data = data_forcada, None
    if parcelas_forcadas and not n:
        n = parcelas_forcadas

    if not texto_limpo.strip():  # a mensagem era so uma data ("dia 15")
        enviar(tel, textos.NAO_ENTENDI)
        return

    r = ia.chamar_ia(texto_limpo)
    responder_intencao(tel, r, texto_limpo, h, data=data, n_parcelas=n, eh_parcela=eh_parcela, erro_data=erro_data)


# ─── Perguntas em aberto ──────────────────────────────────────────────────────

SO_VALOR = re.compile(r"(?:r\$\s*)?\d[\d.,]*(?:\s*(?:reais|real|conto|contos))?")


def _data(dados):
    return date.fromisoformat(dados["data"]) if dados.get("data") else None


def resolver_pendencia(tel, pendencia, texto, h):
    # True se a mensagem respondeu a pergunta; False para seguir o fluxo normal.
    acao, dados = pendencia["acao"], pendencia.get("dados") or {}
    t = normalizar(texto)
    valor = para_valor(t) if SO_VALOR.fullmatch(t) else 0.0

    if acao == "editar_valor" and valor > 0:
        editar(tel, dados.get("short_id"), bool(dados.get("usar_ultimo")), valor)
        return True

    if acao == "valor_gasto" and valor > 0:
        usar_ia(tel, "{} {}".format(dados.get("texto", ""), t).strip(), h,
                data_forcada=_data(dados), parcelas_forcadas=dados.get("parcelas"))
        return True

    if acao == "tipo_pix":
        if re.search(r"\b(recebi|recebido|recebida|entrada|entrou|ganhei|caiu)\b", t):
            palavra = "recebido"
        elif re.search(r"\b(enviei|enviado|enviada|mandei|paguei|pago|saida|saiu|transferi|fiz)\b", t):
            palavra = "enviado"
        else:
            return False
        usar_ia(tel, "pix {} {:.2f}".format(palavra, float(dados.get("valor") or 0)), h, data_forcada=_data(dados))
        return True

    if acao == "apagar_dados":
        if t in ("sim", "s", "confirmo", "confirmar", "pode apagar", "sim pode apagar", "apagar", "sim apagar"):
            apagados = db.apagar_dados(tel)
            enviar(tel, "🗑️ Pronto. Apaguei todos os seus dados ({} lancamentos).\n\n"
                        "Se quiser recomecar, e so mandar um gasto. 💚".format(apagados))
            return True
        if t in ("nao", "n", "cancelar", "cancela", "deixa", "nao apagar"):
            enviar(tel, "👍 Ok, nao apaguei nada.")
            return True

    return False


# ─── Comandos (sem IA) ────────────────────────────────────────────────────────

def executar(tel, cmd, texto, h):
    # True se tratou a mensagem; False para ela seguir para a IA.
    acao = cmd["acao"]

    if acao == "intencao":
        r = ia.normalizar_ia({"intencao": cmd["intencao"], "descricao": cmd.get("descricao"), "valor": cmd.get("valor")})
        responder_intencao(tel, r, texto, h)
        return True

    if acao == "periodo":
        relatorio_periodo(tel, cmd["formato"], cmd["ini"], cmd["fim"], cmd["titulo"], h)
    elif acao == "ano":
        enviar(tel, relatorios.relatorio_ano(tel, cmd["ano"]))
    elif acao == "consulta":
        enviar(tel, relatorios.consulta(tel, cmd["termo"], cmd["tipo"], cmd["ini"], cmd["fim"], cmd["rotulo"]))
    elif acao == "limites":
        enviar(tel, relatorios.texto_limites(tel))
    elif acao == "limite":
        definir_limite(tel, cmd["categoria"], cmd["valor"])
    elif acao == "metas":
        enviar(tel, relatorios.texto_metas(db.listar_metas(tel)))
    elif acao == "meta_criar":
        criar_meta(tel, cmd["nome"], cmd["valor"])
    elif acao == "meta_mover":
        return mover_meta(tel, cmd["nome"], cmd["valor"] * cmd["sinal"])
    elif acao == "meta_apagar":
        apagar_meta(tel, cmd["nome"])
    elif acao == "fixos":
        enviar(tel, relatorios.texto_fixos(db.listar_recorrentes(tel)))
    elif acao == "fixo_ajuda":
        enviar(tel, textos.FIXO_AJUDA)
    elif acao == "fixo_criar":
        criar_fixo(tel, cmd["texto"], cmd["dia"], h)
    elif acao == "fixo_apagar":
        apagar_fixo(tel, cmd["alvo"])
    elif acao == "exportar":
        exportar(tel)
    elif acao == "apagar_dados":
        db.definir_pendencia(tel, "apagar_dados")
        enviar(tel, textos.APAGAR_DADOS)
    elif acao == "lembretes":
        db.definir_lembretes(tel, cmd["ligado"])
        enviar(tel, "🔔 Lembretes de contas fixas ligados." if cmd["ligado"]
               else "🔕 Lembretes desligados. Pra ligar de novo: _lembretes on_")
    elif acao == "admin_assinatura":
        return admin_assinatura(tel, cmd)
    elif acao == "editar_categoria":
        editar_categoria(tel, cmd["alvo"], cmd["categoria"])
    else:
        return False

    return True


def relatorio_periodo(tel, formato, ini, fim, titulo, h):
    # O limite e mensal: so aparece no relatorio do mes atual inteiro (nao no da semana).
    mes_atual = (ini.year, ini.month) == (h.year, h.month) and ini.day == 1
    limite = db.buscar_limite(tel) if mes_atual else 0.0

    if formato == "extrato":
        txs = db.buscar_transacoes(tel, ini, fim)
        enviar(tel, relatorios.relatorio_extrato(
            "Extrato — {}".format(titulo), ini.strftime("%d/%m"), fim.strftime("%d/%m/%Y"), txs, limite))
    elif ini.day == 1 and fim in (fim_mes(ini), h):
        enviar(tel, relatorios.relatorio_mes(tel, ini, fim, titulo, limite))
    else:
        enviar(tel, relatorios.relatorio_resumo(titulo, db.buscar_transacoes(tel, ini, fim)))


# ─── Intencoes (vindas da IA ou de um atalho) ─────────────────────────────────

def responder_intencao(tel, r, mensagem, h, data=None, n_parcelas=None, eh_parcela=False, erro_data=None):
    intencao = r["intencao"]
    log.info("Intencao: %s", intencao)

    if intencao == "gasto":
        if erro_data:
            enviar(tel, textos.DATA_FUTURA if erro_data == "futuro" else textos.DATA_INVALIDA)
            return
        registrar_gasto(tel, r, mensagem, h, data, n_parcelas, eh_parcela)

    elif intencao == "resumo":
        enviar(tel, relatorios.relatorio_hoje_resumo(tel))

    elif intencao == "hoje":
        ini, fim = periodo_hoje()
        txs = db.buscar_transacoes(tel, ini, fim)
        enviar(tel, relatorios.relatorio_resumo("Hoje — {}".format(ini.strftime("%d/%m/%Y")), txs))

    elif intencao == "semana":
        ini, fim = periodo_semana()
        txs = db.buscar_transacoes(tel, ini, fim)
        enviar(tel, relatorios.relatorio_resumo(
            "Esta semana ({} → {})".format(ini.strftime("%d/%m"), fim.strftime("%d/%m")), txs))

    elif intencao == "relatorio":
        ini, fim = periodo_mes()
        enviar(tel, relatorios.relatorio_mes(
            tel, ini, fim, "{} {}".format(MESES[ini.month - 1], ini.year), db.buscar_limite(tel)))

    elif intencao == "extrato":
        ini, fim = periodo_mes()
        txs = db.buscar_transacoes(tel, ini, fim)
        enviar(tel, relatorios.relatorio_extrato(
            "Extrato — {}".format(MESES[ini.month - 1]), ini.strftime("%d/%m"), fim.strftime("%d/%m/%Y"),
            txs, db.buscar_limite(tel)))

    elif intencao == "saldo":
        enviar(tel, relatorios.relatorio_saldo(tel))

    elif intencao == "top":
        enviar(tel, relatorios.relatorio_top(tel))

    elif intencao == "posso_gastar":
        enviar(tel, relatorios.relatorio_posso_gastar(tel))

    elif intencao == "limite":
        if r["valor"] > 0:
            definir_limite(tel, None, r["valor"])
        else:
            enviar(tel, "🤖 Informe o valor. Ex: _limite 2000_")

    elif intencao == "editar":
        editar_por_texto(tel, r, mensagem)

    elif intencao == "apagar":
        apagar_por_texto(tel, r, mensagem)

    elif intencao == "consulta":
        t = normalizar(mensagem)
        ini, fim, rotulo, _ = interpretar.extrair_periodo(t, h)
        tipo = "entrada" if re.search(r"\b(recebi|ganhei|entrou|entradas)\b", t) else "saida"
        termo = interpretar.extrair_periodo(normalizar(r["descricao"]), h)[3] if r["descricao"] else ""
        enviar(tel, relatorios.consulta(tel, termo, tipo, ini, fim, rotulo))

    elif intencao == "confirmacao":
        db.definir_pendencia(tel, "tipo_pix", {"valor": r["valor"], "data": data.isoformat() if data else None})
        val_str = fmt(r["valor"]) if r["valor"] > 0 else "esse valor"
        enviar(tel, "Esse pix de {} foi *recebido* ou *enviado*?\n\n"
                    "_recebido_ → entrada\n_enviado_ → saida 😊".format(val_str))

    elif intencao == "duvida":
        enviar(tel, "🤖 Parece que voce quer simular algo — ainda nao faco calculos.\n\n"
                    "Pra registrar um lancamento:\n_investimento 200_ · _salario 2500_ · _mercado 85_")

    elif intencao == "dica":
        enviar(tel, relatorios.dica_personalizada(tel) or random.choice(textos.DICAS))

    elif intencao == "oi":
        enviar(tel, random.choice(textos.SAUDACOES))

    elif intencao == "ajuda":
        enviar(tel, textos.MSG_AJUDA)

    elif intencao == "erro":
        enviar(tel, textos.ERRO_IA)

    else:
        enviar(tel, textos.NAO_ENTENDI)


# ─── Registrar ────────────────────────────────────────────────────────────────

def registrar_gasto(tel, r, mensagem, h, data=None, n_parcelas=None, eh_parcela=False):
    valor, tipo, categoria = r["valor"], r["tipo"], r["categoria"]

    if valor <= 0:
        # Guarda o que ja sabemos: se a proxima mensagem for so o valor, completa o lancamento.
        db.definir_pendencia(tel, "valor_gasto", {
            "texto": mensagem, "data": data.isoformat() if data else None, "parcelas": n_parcelas})
        enviar(tel, "🤖 Nao identifiquei o valor.\n\nQuanto foi? Manda so o numero (ex: _85_) ou tenta: _mercado 85_")
        return

    descricao = (r["descricao"] or mensagem)[:ia.MAX_DESCRICAO].capitalize()

    if tipo == "entrada":
        icone, label, item_e = "💚", "Entrada registrada", "💰"
    else:
        icone, label, item_e = "🔴", "Saida registrada", "🛒"

    if n_parcelas:
        parcelas = db.salvar_parcelas(
            tel, descricao, tipo, categoria, n_parcelas,
            total=None if eh_parcela else valor, valor_parcela=valor if eh_parcela else None, data=data)
        total = sum(p["valor"] for p in parcelas)
        msg = (
            "💳 *{}*\n\n"
            "{} {}\n"
            "💵 {}x de {} (total {})\n"
            "🏷️ {}\n"
            "📅 1a parcela {} · ultima {}\n\n"
            "👉 _apagar ultimo_ apaga todas as parcelas"
        ).format("Recebimento parcelado registrado" if tipo == "entrada" else "Compra parcelada registrada",
                 item_e, descricao, n_parcelas, fmt(parcelas[0]["valor"]), fmt(total), categoria,
                 parcelas[0]["data"].strftime("%d/%m/%Y"), parcelas[-1]["data"].strftime("%d/%m/%Y"))
    else:
        _, short = db.salvar_transacao(tel, descricao, valor, tipo, categoria, data)
        quando = "📅 {}\n".format(data.strftime("%d/%m/%Y")) if data and data != h else ""
        msg = (
            "{} *{}*\n\n"
            "{} {}\n"
            "💵 {}\n"
            "🏷️ {}\n"
            "{}"
            "🔑 `{}`\n\n"
            "👉 _apagar ultimo_ · _editar ultimo {}_"
        ).format(icone, label, item_e, descricao, fmt(valor), categoria, quando, short, int(valor))

    if tipo == "saida":
        msg += relatorios.alertas_depois_de_gastar(tel, [categoria])

    enviar(tel, msg)


def registrar_varios(tel, texto, itens, h):
    # "mercado 50, uber 20 e farmacia 35": um lancamento por item, uma resposta so.
    data_msg, _, _ = interpretar.extrair_data(texto, h)
    linhas, nao_entendi, categorias = [], [], []
    entradas = saidas = 0.0

    for item in itens[:MAX_ITENS]:
        data, item_limpo, erro_data = interpretar.extrair_data(item, h)

        if not re.search(r"\d", item_limpo):
            continue  # era so a data ("dia 15,"), que ja vale para a mensagem toda

        if erro_data:
            nao_entendi.append(item)
            continue

        n, eh_parcela, item_limpo = interpretar.extrair_parcelas(item_limpo)
        r = ia.chamar_ia(item_limpo)

        if r["intencao"] == "erro":
            if not linhas:
                enviar(tel, textos.ERRO_IA)
                return
            nao_entendi.append(item)
            break

        if r["intencao"] != "gasto" or r["valor"] <= 0:
            nao_entendi.append(item)
            continue

        descricao = (r["descricao"] or item_limpo)[:ia.MAX_DESCRICAO].capitalize()
        data = data or data_msg
        icone = "💚" if r["tipo"] == "entrada" else "🔴"

        if n:
            parcelas = db.salvar_parcelas(
                tel, descricao, r["tipo"], r["categoria"], n,
                total=None if eh_parcela else r["valor"], valor_parcela=r["valor"] if eh_parcela else None, data=data)
            valor = parcelas[0]["valor"]
            linhas.append("{} {} — {}x de {} · {}".format(icone, descricao, n, fmt(valor), r["categoria"]))
        else:
            _, short = db.salvar_transacao(tel, descricao, r["valor"], r["tipo"], r["categoria"], data)
            valor = r["valor"]
            quando = " · {}".format(data.strftime("%d/%m")) if data and data != h else ""
            linhas.append("{} {} — {} · {}{} `{}`".format(icone, descricao, fmt(valor), r["categoria"], quando, short))

        if r["tipo"] == "entrada":
            entradas += valor
        else:
            saidas += valor
            categorias.append(r["categoria"])

    if not linhas:
        enviar(tel, "🤖 Nao entendi esses lancamentos.\n\nManda um por linha, tipo:\n_mercado 50_\n_uber 20_")
        return

    titulo = "1 lancamento registrado" if len(linhas) == 1 else "{} lancamentos registrados".format(len(linhas))
    msg = ["✅ *{}*\n".format(titulo)] + linhas + [""]

    if saidas:
        msg.append("🔴 Saidas: {}".format(fmt(saidas)))
    if entradas:
        msg.append("💚 Entradas: {}".format(fmt(entradas)))
    if nao_entendi:
        msg.append("\n🤔 Nao entendi: {}".format(", ".join("_{}_".format(i) for i in nao_entendi)))
    if len(itens) > MAX_ITENS:
        msg.append("\n⚠️ Registrei so os {} primeiros. Manda o resto em outra mensagem.".format(MAX_ITENS))

    msg.append("\n👉 _apagar ultimo_ · _extrato_")
    resposta = "\n".join(msg)

    if categorias:
        resposta += relatorios.alertas_depois_de_gastar(tel, categorias)

    enviar(tel, resposta)


# ─── Editar e apagar ──────────────────────────────────────────────────────────

# Palavras que nao identificam o lancamento em "apagar o ultimo gasto do mercado".
PALAVRAS_COMANDO = {
    "apagar", "apaga", "apague", "excluir", "exclui", "exclua", "deletar", "deleta", "delete",
    "remover", "remove", "remova", "editar", "edita", "edite", "alterar", "altera", "altere",
    "corrigir", "corrige", "corrija", "mudar", "muda", "mude", "trocar", "troca", "troque",
    "ultimo", "ultima", "last", "lancamento", "gasto", "registro", "valor", "entrada", "saida",
    "o", "a", "os", "as", "um", "uma", "do", "da", "dos", "das", "de", "no", "na", "para", "pra", "pro",
    "meu", "minha", "id",
}


def extrair_id(texto: str):
    # ID curto do extrato (6 hex). Numa frase, exige um digito: "decada" nao e ID.
    t = texto.strip().lower()
    if re.fullmatch(r"[a-f0-9]{6}", t):
        return t
    m = re.search(r"\b(?=[a-f]*\d)[a-f0-9]{6}\b", t)
    return m.group(0) if m else None


def termo_busca(texto: str) -> str:
    # "apagar o ultimo mercado 50" -> "mercado"
    t = re.sub(r"\b\d+(?:[.,]\d+)*\b", " ", sem_acento(texto).lower())
    return " ".join(p for p in re.findall(r"\w+", t) if p not in PALAVRAS_COMANDO)


def buscar_por_descricao(tel, termo):
    # Lancamento mais recente do mes cuja descricao contem o termo.
    ini, fim = periodo_mes()
    txs = db.buscar_transacoes(tel, ini, fim)
    matches = [t for t in txs if termo in sem_acento(t["descricao"]).lower()]
    return matches[-1] if matches else None


def localizar(tel, alvo):
    # Qual lancamento o usuario quis dizer. Retorna (short_id, usar_ultimo, termo_nao_encontrado).
    short_id = extrair_id(alvo)
    if short_id:
        return short_id, False, None

    termo = termo_busca(alvo)
    if not termo:
        return None, True, None

    achado = buscar_por_descricao(tel, termo)
    if not achado:
        return None, False, termo

    return db.id_curto(achado["id"]), False, None


def editar_por_texto(tel, r, mensagem):
    # Sem descricao, usa a mensagem sem o valor novo do final ("editar mercado 120").
    alvo = r["descricao"] or re.sub(r"\s*\d+(?:[.,]\d+)?\s*$", "", mensagem)
    short_id, usar_ultimo, nao_achado = localizar(tel, alvo)

    if nao_achado:
        enviar(tel, "🤖 Nao achei _{}_ este mes.\n\nUse o ID do extrato: _editar ae3f06 120_".format(nao_achado))
        return

    if r["valor"] <= 0:
        db.definir_pendencia(tel, "editar_valor", {"short_id": short_id, "usar_ultimo": usar_ultimo})
        enviar(tel, "✏️ Qual o novo valor?\n\nEx: _editar ultimo 120_")
        return

    editar(tel, short_id, usar_ultimo, r["valor"])


def editar(tel, short_id, usar_ultimo, valor):
    updated = db.editar_transacao(tel, short_id=short_id, usar_ultimo=usar_ultimo, novo_valor=valor)

    if not updated:
        enviar(tel, "🤖 Nao encontrei esse lancamento. Confere o ID no extrato.")
    elif updated.get("erro") == "parcelado":
        enviar(tel, "💳 *{}* e uma compra parcelada.\n\n"
                    "Pra mudar o valor, apague (_apagar {}_) e registre de novo.".format(
                        updated["descricao"], db.id_curto(updated["id"])))
    else:
        enviar(tel, "✏️ *Lancamento atualizado!*\n\n{} → {}\n🏷️ {} `{}`".format(
            updated["descricao"].capitalize(),
            fmt(float(updated["valor"])),
            ia.normalizar_categoria(updated["categoria"]),
            db.id_curto(updated["id"])
        ))


def editar_categoria(tel, alvo, nome_categoria):
    categoria = ia.categoria_por_nome(nome_categoria)

    if not categoria:
        enviar(tel, "🤖 Nao conheco a categoria _{}_.\n\nCategorias: {}".format(nome_categoria, ", ".join(ia.CATEGORIAS)))
        return

    short_id, usar_ultimo, nao_achado = localizar(tel, alvo)

    if nao_achado:
        enviar(tel, "🤖 Nao achei _{}_ este mes.".format(nao_achado))
        return

    updated = db.editar_transacao(tel, short_id=short_id, usar_ultimo=usar_ultimo, nova_categoria=categoria)

    if not updated:
        enviar(tel, "🤖 Nao encontrei esse lancamento. Confere o ID no extrato.")
        return

    enviar(tel, "🏷️ *Categoria atualizada!*\n\n{} → {} {}".format(
        updated["descricao"].capitalize(), relatorios.ecat(categoria), categoria))


def apagar_por_texto(tel, r, mensagem):
    short_id = extrair_id(r["descricao"]) or extrair_id(mensagem)
    usar_ultimo = False

    if not short_id:
        # "apagar mercado" apaga o mercado (nunca outro no lugar); "apagar" sozinho apaga o ultimo.
        termo = termo_busca(r["descricao"] or mensagem)

        if termo:
            achado = buscar_por_descricao(tel, termo)

            if not achado:
                enviar(tel, "🤖 Nao achei _{}_ este mes.\n\nUse o ID do extrato: _apagar ae3f06_".format(termo))
                return

            short_id = db.id_curto(achado["id"])
        else:
            usar_ultimo = True

    deleted = db.apagar_transacao(tel, short_id=short_id, usar_ultimo=usar_ultimo)

    if not deleted:
        enviar(tel, "🤖 Nao encontrei o lancamento. Confere o ID no extrato.")
        return

    ini, fim = periodo_mes()
    _, sai, _ = relatorios.totais(db.buscar_transacoes(tel, ini, fim))

    if deleted.get("parcelas"):
        descricao = re.sub(r"\s*\(\d+/\d+\)$", "", deleted["descricao"])
        enviar(tel, "🗑️ *Compra parcelada excluida!*\n\n{} — {} parcelas ({})\n\nSaidas do mes: {}".format(
            descricao.capitalize(), deleted["parcelas"], fmt(deleted["total"]), fmt(sai)))
        return

    icone = "💚" if deleted["tipo"] == "entrada" else "🔴"
    enviar(tel, "🗑️ *Lancamento excluido!*\n\n{} — {} {}\n\nSaidas do mes: {}".format(
        deleted["descricao"].capitalize(), icone, fmt(float(deleted["valor"])), fmt(sai)))


# ─── Limites ──────────────────────────────────────────────────────────────────

def definir_limite(tel, nome_categoria, valor):
    ini, fim = periodo_mes()

    if nome_categoria:
        categoria = ia.categoria_por_nome(nome_categoria)

        if not categoria:
            enviar(tel, "🤖 Nao conheco a categoria _{}_.\n\nCategorias: {}".format(nome_categoria, ", ".join(ia.CATEGORIAS)))
            return

        if valor <= 0:
            removido = db.remover_limite_categoria(tel, categoria)
            enviar(tel, "🗑️ Limite de {} removido.".format(categoria) if removido
                   else "Voce nao tinha limite de {}.".format(categoria))
            return

        db.salvar_limite_categoria(tel, categoria, valor)
        gasto = dict(relatorios.saidas_por_categoria(db.buscar_transacoes(tel, ini, fim))).get(categoria, 0.0)
        enviar(tel, "✅ *Limite de {} definido: {}*\n\nGasto este mes: {} ({}%)\n\n"
                    "Vou te avisar quando estiver chegando perto! 🔔".format(
                        categoria, fmt(valor), fmt(gasto), int(gasto / valor * 100)))
        return

    if valor <= 0:
        removido = db.remover_limite(tel)
        enviar(tel, "🗑️ Limite mensal removido." if removido else "Voce nao tinha limite mensal definido.")
        return

    db.salvar_limite(tel, valor)
    _, sai, _ = relatorios.totais(db.buscar_transacoes(tel, ini, fim))
    enviar(tel, "✅ *Limite definido: {}*\n\nSaidas este mes: {} ({}%)\n\n"
                "Vou te avisar quando estiver chegando perto! 🔔".format(fmt(valor), fmt(sai), int((sai / valor) * 100)))


# ─── Metas ────────────────────────────────────────────────────────────────────

def achar_meta(metas, nome):
    chave = " ".join(sem_acento(nome or "").lower().split())

    if not chave:
        return metas[0] if len(metas) == 1 else None

    for meta in metas:
        if sem_acento(meta["nome"]).lower() == chave:
            return meta

    for meta in metas:
        nome_meta = sem_acento(meta["nome"]).lower()
        if chave in nome_meta or nome_meta in chave:
            return meta

    return None


def criar_meta(tel, nome, valor):
    nome = " ".join(nome.split())

    if valor <= 0:
        enviar(tel, "🎯 Qual o valor da meta? Ex: _meta viagem 5000_")
        return

    meta = db.salvar_meta(tel, nome[:1].upper() + nome[1:], valor)

    if meta["criada"]:
        enviar(tel, "🎯 *Meta criada!*\n\n{}\n\nPra guardar dinheiro nela: _guardei 200 {}_".format(
            relatorios.texto_meta(meta), nome.lower()))
    else:
        enviar(tel, "🎯 *Meta atualizada!*\n\n{}".format(relatorios.texto_meta(meta)))


def mover_meta(tel, nome, delta):
    # False se nao existe meta com esse nome: ai "guardei 200 na poupanca" segue para a IA (vira investimento).
    meta = achar_meta(db.listar_metas(tel), nome)

    if not meta:
        return False

    antes = float(meta["guardado"])
    meta = db.movimentar_meta(meta["id"], delta)
    alvo, depois = float(meta["alvo"]), float(meta["guardado"])

    if delta > 0:
        titulo = "💰 *+{} na meta {}*".format(fmt(delta), meta["nome"])
    else:
        titulo = "💸 *-{} da meta {}*".format(fmt(antes - depois), meta["nome"])

    extra = "\n\n🎉 *Parabens! Voce bateu a meta!*" if antes < alvo <= depois else ""
    enviar(tel, "{}\n\n{}{}".format(titulo, relatorios.texto_meta(meta), extra))
    return True


def apagar_meta(tel, nome):
    meta = achar_meta(db.listar_metas(tel), nome)

    if not meta:
        enviar(tel, "🤖 Nao achei a meta _{}_.\n\nSuas metas: _metas_".format(nome))
        return

    db.apagar_meta(meta["id"])
    enviar(tel, "🗑️ Meta *{}* apagada.".format(meta["nome"]))


# ─── Contas fixas ─────────────────────────────────────────────────────────────

def criar_fixo(tel, texto, dia, h):
    r = ia.chamar_ia(texto)

    if r["intencao"] == "erro":
        enviar(tel, textos.ERRO_IA)
        return

    if r["valor"] <= 0:
        enviar(tel, textos.FIXO_AJUDA)
        return

    descricao = (r["descricao"] or texto)[:ia.MAX_DESCRICAO].capitalize()
    icone = "💚" if r["tipo"] == "entrada" else "🔴"

    # Mandou de novo a mesma conta? Atualiza em vez de duplicar (senao o aluguel cairia 2x todo mes).
    for rec in db.listar_recorrentes(tel):
        if sem_acento(rec["descricao"]).lower() == sem_acento(descricao).lower():
            db.atualizar_recorrente(rec["id"], r["valor"], r["tipo"], r["categoria"], dia)
            enviar(tel, "🔁 *Conta fixa atualizada*\n\n{} {} — {} todo dia {}\n🏷️ {}".format(
                icone, rec["descricao"], fmt(r["valor"]), dia, r["categoria"]))
            return

    # Primeiro lancamento: dia N deste mes se ainda nao passou, senao do mes que vem.
    primeira = db.ocorrencia(dia, h.year, h.month)
    if primeira < h:
        prox = somar_meses(h.replace(day=1), 1)
        primeira = db.ocorrencia(dia, prox.year, prox.month)

    db.criar_recorrente(tel, descricao, r["valor"], r["tipo"], r["categoria"], dia, primeira)
    lancou_hoje = bool(db.gerar_recorrentes(tel, h)) if primeira == h else False

    quando = "Ja lancei a de hoje." if lancou_hoje else "Primeiro lancamento: {}".format(primeira.strftime("%d/%m/%Y"))

    enviar(tel, "🔁 *Conta fixa criada*\n\n{} {} — {} todo dia {}\n🏷️ {}\n📅 {}\n\n"
                "Eu lanco sozinho todo mes. _fixos_ pra ver todas.".format(
                    icone, descricao, fmt(r["valor"]), dia, r["categoria"], quando))


def apagar_fixo(tel, alvo):
    recorrentes = db.listar_recorrentes(tel)
    chave = sem_acento(alvo).lower().strip().lstrip("#")
    rec = None

    if chave.isdigit() and 1 <= int(chave) <= len(recorrentes):
        rec = recorrentes[int(chave) - 1]
    else:
        for r in recorrentes:
            if chave and chave in sem_acento(r["descricao"]).lower():
                rec = r
                break

    if not rec:
        enviar(tel, "🤖 Nao achei a conta fixa _{}_.\n\nVeja a lista: _fixos_".format(alvo))
        return

    db.desativar_recorrente(rec["id"])
    enviar(tel, "🗑️ Conta fixa *{}* cancelada.\n\nO que ja foi lancado continua no extrato.".format(rec["descricao"]))


def avisar_fixos_lancados(tel, gerados):
    if not gerados:
        return

    linhas = ["🔁 *Lancei suas contas fixas:*"]
    for g in gerados[:10]:
        linhas.append("{} {} — {} ({})".format(
            "💚" if g["tipo"] == "entrada" else "🔴", g["descricao"], fmt(g["valor"]), g["data"].strftime("%d/%m")))
    if len(gerados) > 10:
        linhas.append("... e mais {}".format(len(gerados) - 10))

    enviar(tel, "\n".join(linhas))


# ─── Exportar ─────────────────────────────────────────────────────────────────

def exportar(tel):
    txs = db.todas_transacoes(tel)

    if not txs:
        enviar(tel, "📄 Voce ainda nao tem lancamentos pra exportar.")
        return

    conteudo = relatorios.gerar_csv(txs)
    nome = "cashflow-{}.csv".format(hoje().isoformat())
    legenda = "📄 {} lancamentos. Abre no Excel ou Google Planilhas.".format(len(txs))

    if not whatsapp.enviar_documento(tel, nome, conteudo, "text/csv", legenda):
        enviar(tel, "📄 Nao consegui enviar o arquivo agora. Tenta de novo em instantes.")


# ─── Assinatura ───────────────────────────────────────────────────────────────

def acesso_liberado(tel, usuario):
    if not config.ASSINATURA_OBRIGATORIA:
        return True

    chave = chave_telefone(tel)
    if chave in config.ADMIN_TELEFONES or db.assinatura_ativa(chave):
        return True

    return agora() < usuario["criado_em"] + timedelta(days=config.DIAS_TESTE)


def avisar_assinatura(tel, usuario):
    # No maximo um aviso a cada 12h, pra nao virar spam.
    ultimo = usuario.get("aviso_assinatura_em")
    if ultimo and agora() - ultimo < timedelta(hours=12):
        return

    enviar(tel, textos.aviso_assinatura())
    db.marcar_aviso_assinatura(tel)


def admin_assinatura(tel, cmd):
    if chave_telefone(tel) not in config.ADMIN_TELEFONES:
        return False

    numero = numero_br(cmd["numero"])
    db.definir_assinatura(chave_telefone(numero), cmd["ativa"], "admin")
    enviar(tel, "✅ Assinatura {} para ***{}.".format("ativada" if cmd["ativa"] else "desativada", numero[-4:]))

    if cmd["ativa"]:
        enviar(numero + "@s.whatsapp.net", textos.ASSINATURA_ATIVA)

    return True


def processar_hubla(dados):
    # Webhook da Hubla: customer.member_added libera, customer.member_removed bloqueia.
    tipo = str(dados.get("type") or "")
    usuario = (dados.get("event") or {}).get("user") or {}
    telefone = numero_br(usuario.get("phone"))

    if tipo not in ("customer.member_added", "customer.member_removed"):
        log.info("Hubla: evento %s ignorado", tipo)
        return

    if not telefone:
        log.warning("Hubla: evento %s sem telefone", tipo)
        return

    ativa = tipo == "customer.member_added"
    db.definir_assinatura(chave_telefone(telefone), ativa, "hubla")
    log.info("Hubla: assinatura %s para %s", "ativada" if ativa else "desativada", mascarar(telefone))

    if ativa:
        enviar(telefone + "@s.whatsapp.net", textos.ASSINATURA_ATIVA)


# ─── Tarefas diarias (opcional, chamadas por um cron externo) ────────────────

def tarefas_diarias():
    h = hoje()
    lancados = 0

    for tel in db.telefones_com_recorrentes():
        lancados += len(db.gerar_recorrentes(tel, h))

    amanha = h + timedelta(days=1)
    lembretes = 0

    for rec in db.recorrentes_para_lembrar(amanha):
        enviar(rec["telefone"], "⏰ *Lembrete:* amanha vence *{}* ({}).\n\n_lembretes off_ pra nao receber mais.".format(
            rec["descricao"], fmt(float(rec["valor"]))))
        db.marcar_lembrado(rec["id"])
        lembretes += 1

    return {"fixos_lancados": lancados, "lembretes": lembretes}
