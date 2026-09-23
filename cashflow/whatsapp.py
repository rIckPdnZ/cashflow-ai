"""Evolution API: ler o webhook, enviar texto/arquivo e baixar midia. Funciona com a v1 e a v2."""
import base64
import json
import logging
import threading
from dataclasses import dataclass

import requests

from cashflow import config
from cashflow.util import mascarar

log = config.log

_local = threading.local()

# Indice do formato que a Evolution aceitou por ultimo: 0 = v1, 1 = v2.
_formato_envio = 0


@dataclass
class Mensagem:
    telefone: str
    id: str = ""
    texto: str = ""
    tipo: str = "texto"  # texto | audio | imagem | documento
    base64: str = ""     # midia ja embutida no webhook (opcao "base64" da Evolution)
    segundos: int = 0


def http() -> requests.Session:
    # Uma sessao por thread: reaproveita a conexao com a Evolution entre mensagens.
    if not hasattr(_local, "sessao"):
        _local.sessao = requests.Session()
    return _local.sessao


# Envelopes em que o WhatsApp embrulha a mensagem de verdade.
_ENVELOPES = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "documentWithCaptionMessage")


def extrair_mensagem(data: dict):
    # Retorna Mensagem, ou None se o evento nao e uma mensagem nova de uma pessoa.
    try:
        if log.isEnabledFor(logging.DEBUG):
            log.debug("PAYLOAD RAW: %s", json.dumps(data, ensure_ascii=False)[:4000])

        if isinstance(data, dict) and data.get("base64") and not data.get("data"):
            decoded = base64.b64decode(data["base64"]).decode("utf-8")
            data = json.loads(decoded)

        event = str(data.get("event", "")).lower()
        log.debug("EVENTO: %s", event)

        if event and ("message" not in event and "messages" not in event):
            return None

        # Historico sincronizado ao reconectar o WhatsApp: mensagens antigas, nao registrar de novo.
        if event in ("messages.set", "messages_set"):
            return None

        msg_data = data.get("data", {})

        if isinstance(msg_data, list) and msg_data:
            msg_data = msg_data[0]

        if not isinstance(msg_data, dict):
            return None

        key = msg_data.get("key", {}) or {}

        if key.get("fromMe"):
            return None

        jid = key.get("remoteJid") or msg_data.get("remoteJid") or msg_data.get("jid") or ""

        # Contatos com endereco "@lid" (privacidade do WhatsApp): usa o numero real se vier junto.
        if jid.endswith("@lid"):
            alternativo = key.get("remoteJidAlt") or key.get("senderPn") or msg_data.get("senderPn") or ""
            if alternativo.endswith("@s.whatsapp.net"):
                jid = alternativo

        # Grupos, status dos contatos e canais nao sao conversas com o bot.
        if not jid or "@g.us" in jid or jid.endswith("@broadcast") or jid.endswith("@newsletter"):
            return None

        msg = msg_data.get("message", {}) or {}

        for envelope in _ENVELOPES:
            if isinstance(msg.get(envelope), dict) and msg[envelope].get("message"):
                msg = {**msg[envelope]["message"], "base64": msg.get("base64")}

        mensagem = Mensagem(telefone=jid, id=str(key.get("id") or ""))

        texto = (
            msg.get("conversation")
            or (msg.get("extendedTextMessage") or {}).get("text")
            or (msg.get("imageMessage") or {}).get("caption")
            or (msg.get("videoMessage") or {}).get("caption")
            or (msg.get("documentMessage") or {}).get("caption")
            or msg_data.get("messageText")
            or msg_data.get("text")
            or ""
        ).strip()

        if texto:
            mensagem.texto = texto
            return mensagem

        if "audioMessage" in msg:
            mensagem.tipo = "audio"
            mensagem.segundos = int((msg["audioMessage"] or {}).get("seconds") or 0)
            mensagem.base64 = msg.get("base64") or ""
            return mensagem

        if "imageMessage" in msg:
            mensagem.tipo = "imagem"
            return mensagem

        if "documentMessage" in msg:
            mensagem.tipo = "documento"
            return mensagem

        # Figurinha, reacao, localizacao, mensagem apagada/editada...: nada a responder.
        log.info("Mensagem sem texto reconhecido.")
        return None

    except Exception as e:
        log.exception("Erro ao extrair mensagem: %s", e)
        return None


def _numero(telefone: str) -> str:
    return telefone.replace("@s.whatsapp.net", "").replace("@c.us", "")


def _postar(caminho: str, payloads: list, telefone: str, timeout=10) -> bool:
    # Tenta primeiro o formato (v1/v2) que funcionou da ultima vez: 1 requisicao em vez de 2.
    global _formato_envio

    url = "{}/{}/{}".format(config.EVOLUTION_URL, caminho, config.EVOLUTION_INSTANCE)
    headers = {"Content-Type": "application/json", "apikey": config.EVOLUTION_KEY}
    ordem = sorted(range(len(payloads)), key=lambda i: i != _formato_envio)
    erro = None

    for i in ordem:
        try:
            r = http().post(url, json=payloads[i], headers=headers, timeout=timeout)
        except requests.RequestException as e:
            erro = e
            continue

        if r.status_code < 400:
            if i != _formato_envio:
                log.info("Evolution: usando o formato de envio %s", i)
                _formato_envio = i
            log.debug("EVOLUTION SEND STATUS %s: %s", r.status_code, r.text[:500])
            return True

        erro = "status {}: {}".format(r.status_code, r.text[:300])

    log.error("Falha ao enviar para %s: %s", mascarar(telefone), erro)
    return False


def enviar(telefone: str, texto: str) -> bool:
    numero = _numero(telefone)
    return _postar("message/sendText", [
        {"number": numero, "textMessage": {"text": texto}},  # v1
        {"number": numero, "text": texto},                   # v2
    ], telefone)


def enviar_documento(telefone: str, nome: str, conteudo: bytes, mimetype: str, legenda: str = "") -> bool:
    numero = _numero(telefone)
    midia = base64.b64encode(conteudo).decode()
    return _postar("message/sendMedia", [
        {"number": numero, "mediaMessage": {"mediatype": "document", "fileName": nome, "caption": legenda, "media": midia}},
        {"number": numero, "mediatype": "document", "mimetype": mimetype, "fileName": nome, "caption": legenda, "media": midia},
    ], telefone, timeout=30)


def baixar_midia(mensagem_id: str):
    # Audio/imagem chegam criptografados; a Evolution devolve o arquivo em base64.
    url = "{}/chat/getBase64FromMediaMessage/{}".format(config.EVOLUTION_URL, config.EVOLUTION_INSTANCE)
    try:
        r = http().post(
            url,
            json={"message": {"key": {"id": mensagem_id}}, "convertToMp4": False},
            headers={"Content-Type": "application/json", "apikey": config.EVOLUTION_KEY},
            timeout=20,
        )
        if r.status_code >= 400:
            log.error("Evolution nao devolveu a midia (status %s): %s", r.status_code, r.text[:300])
            return None
        return base64.b64decode(r.json().get("base64") or "") or None
    except (requests.RequestException, ValueError) as e:
        log.error("Falha ao baixar midia: %s", e)
        return None
