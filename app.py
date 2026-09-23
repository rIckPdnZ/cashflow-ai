"""
╔══════════════════════════════════════════════════════════════╗
║              Cash Flow IA — WhatsApp Bot v4.0                ║
║         Flask · Evolution API · Groq · PostgreSQL            ║
╠══════════════════════════════════════════════════════════════╣
║  Variáveis de ambiente obrigatórias:                         ║
║    DATABASE_URL       → postgresql://user:pass@host/db       ║
║    GROQ_API_KEY       → sua chave Groq                       ║
║    EVOLUTION_URL      → http://SEU-IP:8080                   ║
║    EVOLUTION_KEY      → apikey configurada no .env           ║
║    EVOLUTION_INSTANCE → nome da instância (ex: cashflow)     ║
║    PORT               → (opcional) padrão 5000               ║
║  Opcionais: veja o GUIA.md (segredo do webhook, assinatura,  ║
║  Hubla, lembretes, modelos da IA, nível de log).             ║
╚══════════════════════════════════════════════════════════════╝

O código do bot fica no pacote cashflow/: aqui só ficam as rotas HTTP.
"""
import hmac
import os

import psycopg2
from flask import Flask, request

from cashflow import bot, config, db, whatsapp
from cashflow.util import mascarar

log = config.log

app = Flask(__name__)


def token_ok(recebido, esperado) -> bool:
    return bool(esperado) and hmac.compare_digest((recebido or "").encode(), esperado.encode())


@app.route("/webhook", methods=["POST"])
def webhook():
    # Com WEBHOOK_SECRET definido, so aceita a URL configurada na Evolution: /webhook?token=SEGREDO
    if config.WEBHOOK_SECRET and not token_ok(request.args.get("token"), config.WEBHOOK_SECRET):
        log.warning("Webhook recusado: token invalido ou ausente.")
        return "", 401

    msg = whatsapp.extrair_mensagem(request.get_json(silent=True) or {})

    if not msg:
        return "", 200

    log.info("MSG de %s (%s)", mascarar(msg.telefone),
             "{} caracteres".format(len(msg.texto)) if msg.tipo == "texto" else msg.tipo)
    log.debug("MSG %s: %s", msg.telefone, msg.texto)

    try:
        with db.sessao():
            bot.processar(msg)

    except psycopg2.Error as e:
        log.exception("Erro banco: %s", e)
        whatsapp.enviar(msg.telefone, "Tivemos um problema tecnico no banco. Tenta de novo em instantes.")

    except Exception as e:
        log.exception("Erro inesperado: %s", e)
        whatsapp.enviar(msg.telefone, "🤖 Ops, algo deu errado. Tenta de novo em instantes.")

    return "", 200


@app.route("/hubla", methods=["POST"])
def hubla():
    # Pagamentos da Hubla (so com HUBLA_TOKEN definido). A Hubla manda o token no header x-hubla-token.
    if not token_ok(request.headers.get("x-hubla-token"), config.HUBLA_TOKEN):
        return "", 401

    dados = request.get_json(silent=True) or {}
    idempotencia = request.headers.get("x-hubla-idempotency")

    try:
        with db.sessao():
            if idempotencia and not db.marcar_processada("hubla:" + idempotencia):
                return "", 200
            bot.processar_hubla(dados)
    except Exception as e:
        log.exception("Erro no webhook da Hubla: %s", e)
        return "", 500

    return "", 200


@app.route("/tarefas", methods=["GET", "POST"])
def tarefas():
    # Chamado 1x por dia por um cron externo (so com CRON_SECRET): lanca contas fixas e manda lembretes.
    if not token_ok(request.args.get("token") or request.headers.get("x-cron-token"), config.CRON_SECRET):
        return "", 401

    with db.sessao():
        return bot.tarefas_diarias(), 200


@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "app": "Cash Flow IA", "version": "4.0"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
