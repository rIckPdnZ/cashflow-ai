"""Conversas completas contra um PostgreSQL de verdade (Groq e Evolution sao falsos).

So rodam com TEST_DATABASE_URL apontando para um banco de TESTE (ele e apagado a cada teste):

    TEST_DATABASE_URL=postgresql://postgres@localhost/cashflow_test python -m unittest -v
"""
import base64
import json
import os
import unittest
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest import mock

TEST_DB = os.environ.get("TEST_DATABASE_URL")
TEL = "5511999990001@s.whatsapp.net"


def gasto(descricao, valor, categoria="Outros", tipo="saida"):
    return {"intencao": "gasto", "descricao": descricao, "valor": valor, "tipo": tipo, "categoria": categoria}


@unittest.skipUnless(TEST_DB, "defina TEST_DATABASE_URL (banco de teste, sera apagado) para rodar as conversas")
class Conversa(unittest.TestCase):
    def setUp(self):
        import app
        from cashflow import db, ia, util, whatsapp

        self.assertEqual(os.environ["DATABASE_URL"], TEST_DB, "os testes de conversa so rodam no banco de teste")
        self.db = db

        conn = db.conectar()
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            cur.execute("TRUNCATE {} RESTART IDENTITY".format(", ".join(r["tablename"] for r in cur.fetchall())))
            cur.execute("INSERT INTO usuarios (telefone) VALUES (%s)", (TEL,))  # usuario ja conhecido
        conn.commit()
        conn.close()

        # Relogio: quarta, 23/09/2026, 15h em Brasilia
        self.agora = datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)
        teste = self

        class Relogio(datetime):
            @classmethod
            def now(cls, tz=None):
                return teste.agora.astimezone(tz)

        self._patch(util, "datetime", Relogio)

        # IA falsa: responde o JSON cadastrado para cada texto
        self.respostas = {}
        self.chamadas_ia = []
        self._patch(ia.groq_client.chat.completions, "create", self._ia)
        self.transcricao = "mercado 85"
        self._patch(ia.groq_client.audio.transcriptions, "create", lambda **kw: SimpleNamespace(text=self.transcricao))

        # Evolution falsa (v2)
        self.enviadas = []
        self.documentos = []
        self._patch(whatsapp, "http", lambda: SimpleNamespace(post=self._evolution))
        self._patch(whatsapp, "_formato_envio", 1)

        self.client = app.app.test_client()

    def _patch(self, alvo, nome, valor):
        p = mock.patch.object(alvo, nome, valor)
        p.start()
        self.addCleanup(p.stop)

    def _ia(self, **kw):
        texto = kw["messages"][-1]["content"]
        self.chamadas_ia.append(texto)
        resposta = self.respostas.get(texto, {"intencao": "outro"})
        if resposta == "erro":
            raise RuntimeError("Groq fora do ar")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(resposta)))])

    def _evolution(self, url, json=None, headers=None, timeout=None):
        if "/message/sendText/" in url:
            self.enviadas.append((json["number"], json["text"]))
        elif "/message/sendMedia/" in url:
            self.documentos.append(json)
        elif "/chat/getBase64FromMediaMessage/" in url:
            return SimpleNamespace(status_code=201, text="", json=lambda: {"base64": base64.b64encode(b"ogg").decode()})
        return SimpleNamespace(status_code=201, text="{}")

    # ─── ajudantes ───

    def msg(self, texto=None, ia=None, message=None, **key):
        if ia is not None:
            self.respostas[texto] = ia
        antes = len(self.enviadas)
        corpo = {"event": "messages.upsert", "data": {
            "key": {"remoteJid": TEL, "fromMe": False, "id": uuid.uuid4().hex, **key},
            "message": message or {"conversation": texto}}}
        self.ultima_resposta = self.client.post("/webhook", json=corpo)
        return [t for n, t in self.enviadas[antes:] if n == TEL.split("@")[0]]

    def resp(self, *args, **kw):
        respostas = self.msg(*args, **kw)
        self.assertEqual(len(respostas), 1, respostas)
        return respostas[0]

    def linhas(self):
        with self.db.sessao(), self.db.transacao() as cur:
            cur.execute("SELECT descricao, valor::float AS valor, tipo, categoria, data FROM transacoes ORDER BY id")
            return [dict(r) for r in cur.fetchall()]

    # ─── registrar ───

    def test_gasto_simples(self):
        texto = self.resp("mercado 85", gasto("mercado", 85, "Alimentacao"))
        self.assertIn("Saida registrada", texto)
        self.assertEqual(self.linhas()[0]["data"], date(2026, 9, 23))

    def test_data_passada_sai_do_texto_antes_da_ia(self):
        self.respostas["mercado 200"] = gasto("mercado", 200, "Alimentacao")
        texto = self.resp("dia 15 mercado 200")
        self.assertEqual(self.chamadas_ia, ["mercado 200"])  # a IA nao ve o "15"
        self.assertIn("📅 15/09/2026", texto)
        self.assertEqual(self.linhas()[0]["data"], date(2026, 9, 15))

    def test_data_futura_nao_registra(self):
        self.respostas["farmacia 40"] = gasto("farmacia", 40)
        self.assertIn("futuro", self.resp("25/09 farmacia 40"))
        self.assertEqual(self.linhas(), [])

    def test_compra_parcelada(self):
        self.respostas["tv 3000"] = gasto("tv", 3000)
        texto = self.resp("tv 3000 em 10x")
        self.assertIn("10x de R$ 300,00 (total R$ 3.000,00)", texto)

        linhas = self.linhas()
        self.assertEqual(len(linhas), 10)
        self.assertEqual(linhas[0]["descricao"], "Tv (1/10)")
        self.assertEqual([l["data"] for l in linhas[:3]], [date(2026, 9, 23), date(2026, 10, 23), date(2026, 11, 23)])
        self.assertAlmostEqual(sum(l["valor"] for l in linhas), 3000)

        # "apagar ultimo" desfaz a compra inteira, nao so a 10a parcela
        self.assertIn("10 parcelas", self.resp("apagar ultimo"))
        self.assertEqual(self.linhas(), [])

    def test_valor_da_parcela(self):
        self.respostas["celular 250"] = gasto("celular", 250)
        self.resp("celular 12x de 250")
        self.assertEqual([l["valor"] for l in self.linhas()], [250.0] * 12)

    def test_varios_gastos_numa_mensagem(self):
        self.respostas.update({
            "mercado 50": gasto("mercado", 50, "Alimentacao"),
            "uber 20": gasto("uber", 20, "Transporte"),
            "farmácia 35": gasto("farmácia", 35, "Saude"),
        })
        texto = self.resp("ontem: mercado 50, uber 20 e farmácia 35")
        self.assertIn("3 lancamentos registrados", texto)
        self.assertIn("Saidas: R$ 105,00", texto)
        self.assertEqual({l["data"] for l in self.linhas()}, {date(2026, 9, 22)})

    def test_varios_com_um_que_nao_e_gasto(self):
        self.respostas.update({"mercado 50": gasto("mercado", 50), "blablabla 3": {"intencao": "outro"}})
        texto = self.resp("mercado 50, blablabla 3")
        self.assertIn("1 lancamento registrado", texto)
        self.assertIn("Nao entendi: _blablabla 3_", texto)

    # ─── perguntas em aberto ───

    def test_responder_so_o_valor_depois_de_editar(self):
        self.resp("mercado 85", gasto("mercado", 85))
        self.assertIn("Qual o novo valor?", self.resp("editar ultimo"))
        self.assertIn("R$ 120,00", self.resp("120"))
        self.assertEqual([l["valor"] for l in self.linhas()], [120.0])  # editou, nao criou um gasto "120"

    def test_pix_ambiguo(self):
        self.respostas["pix 100"] = {"intencao": "confirmacao", "valor": 100}
        self.respostas["pix recebido 100.00"] = gasto("pix recebido", 100, "Transferencia", "entrada")
        self.assertIn("recebido* ou *enviado", self.resp("pix 100"))
        self.assertIn("Entrada registrada", self.resp("recebido"))
        self.assertEqual(self.linhas()[0]["tipo"], "entrada")

    def test_valor_que_faltou(self):
        self.respostas["mercado"] = gasto("mercado", 0)
        self.respostas["mercado 85"] = gasto("mercado", 85, "Alimentacao")
        self.assertIn("Quanto foi?", self.resp("mercado"))
        self.assertIn("Saida registrada", self.resp("85"))

    def test_so_uma_data_nao_vai_para_a_ia(self):
        self.assertIn("Nao entendi", self.resp("dia 15"))
        self.assertEqual(self.chamadas_ia, [])

    def test_extrato_da_semana_nao_mostra_limite_do_mes(self):
        self.resp("limite 1000")
        self.resp("mercado 85", gasto("mercado", 85))
        self.assertNotIn("Limite:", self.resp("extrato da semana"))
        self.assertIn("Limite: R$ 1.000,00", self.resp("extrato"))

    def test_pergunta_em_aberto_nao_prende_o_usuario(self):
        self.resp("mercado 85", gasto("mercado", 85))
        self.resp("editar ultimo")
        self.assertIn("Saldo", self.resp("saldo"))  # outro comando segue normal
        self.assertIn("Nao entendi", self.resp("120"))  # e a pergunta ja foi esquecida

    # ─── corrigir ───

    def test_apagar_pelo_nome(self):
        self.resp("mercado 85", gasto("mercado", 85))
        self.resp("uber 20", gasto("uber", 20))
        self.resp("apagar mercado")
        self.assertEqual([l["descricao"] for l in self.linhas()], ["Uber"])
        self.assertIn("Nao achei _netflix_", self.resp("apagar netflix"))
        self.assertEqual(len(self.linhas()), 1)

    def test_mudar_categoria(self):
        self.resp("show 200", gasto("show", 200, "Outros"))
        self.assertIn("Lazer", self.resp("mudar categoria do ultimo para lazer"))
        self.assertEqual(self.linhas()[0]["categoria"], "Lazer")

    # ─── perguntar e relatorios ───

    def test_quanto_gastei_com(self):
        self.resp("uber 30", gasto("uber", 30, "Transporte"))
        self.resp("uber 20", gasto("uber", 20, "Transporte"))
        self.resp("mercado 50", gasto("mercado", 50, "Alimentacao"))
        texto = self.resp("quanto gastei com uber?")
        self.assertIn("R$ 50,00* em 2 lancamentos", texto)
        self.assertEqual(self.chamadas_ia, ["uber 30", "uber 20", "mercado 50"])  # pergunta sem IA
        self.assertIn("R$ 50,00* em 1 lancamento", self.resp("gastos com alimentacao"))
        self.assertIn("Nenhum gasto com _ifood_ em setembro", self.resp("quanto gastei com ifood"))

    def test_relatorio_do_mes_com_categorias_e_comparacao(self):
        self.respostas.update({"mercado 100": gasto("mercado", 100, "Alimentacao"),
                               "uber 50": gasto("uber", 50, "Transporte"),
                               "mercado 300": gasto("mercado", 300, "Alimentacao")})
        self.resp("15/08 mercado 100")
        self.resp("mercado 300")
        self.resp("uber 50")
        texto = self.resp("mes")
        self.assertIn("Onde foi o dinheiro", texto)
        self.assertIn("🍔 Alimentacao — R$ 300,00 (86%)", texto)
        self.assertIn("250% a mais que em Agosto no mesmo periodo", texto)
        self.assertIn("*Agosto*", self.resp("agosto"))
        self.assertIn("Resumo de 2026", self.resp("ano"))

    def test_dica_do_mes_personalizada(self):
        # o site promete "uma analise personalizada de onde voce pode economizar"
        self.respostas.update({"ifood 100": gasto("ifood", 100, "Alimentacao"),
                               "ifood 60": gasto("ifood", 60, "Alimentacao"),
                               "uber 30": gasto("uber", 30, "Transporte")})
        self.resp("10/08 ifood 60")
        for texto in ("ifood 100", "ifood 100", "uber 30"):
            self.resp(texto)
        dica = self.resp("dica do mês")
        self.assertIn("*Alimentacao* e onde mais sai dinheiro: R$ 200,00 (87% das saidas)", dica)
        self.assertIn("*Alimentacao* subiu 233% em relacao a Agosto", dica)
        self.assertIn("No ritmo atual, voce fecha o mes com R$ 300,00", dica)

    # ─── planejar ───

    def test_limite_por_categoria(self):
        self.assertIn("Limite de Alimentacao definido: R$ 100,00", self.resp("limite comida 100"))
        texto = self.resp("mercado 80", gasto("mercado", 80, "Alimentacao"))
        self.assertIn("80% do limite de Alimentacao", texto)
        self.assertIn("Alimentacao:* R$ 80,00 de R$ 100,00", self.resp("limites"))
        self.assertIn("removido", self.resp("remover limite alimentacao"))

    def test_metas(self):
        self.assertIn("Meta criada", self.resp("meta viagem 1000"))
        self.assertIn("20%", self.resp("guardei 200 viagem"))
        self.assertIn("Parabens", self.resp("guardei 800 na viagem"))
        self.assertIn("meta batida", self.resp("metas"))
        # sem meta com esse nome, vira lancamento normal (via IA)
        self.respostas["guardei 50 na poupanca"] = gasto("poupanca", 50, "Investimentos")
        self.assertIn("Saida registrada", self.resp("guardei 50 na poupanca"))

    def test_conta_fixa_lanca_sozinha_todo_mes(self):
        self.respostas["aluguel 1500"] = gasto("aluguel", 1500, "Moradia")
        self.assertIn("Primeiro lancamento: 05/10/2026", self.resp("fixo aluguel 1500 dia 5"))
        self.assertEqual(self.linhas(), [])

        self.agora = datetime(2026, 12, 10, 15, 0, tzinfo=timezone.utc)
        respostas = self.msg("saldo")
        self.assertIn("Lancei suas contas fixas", respostas[0])
        self.assertEqual([l["data"] for l in self.linhas()], [date(2026, 10, 5), date(2026, 11, 5), date(2026, 12, 5)])

        self.msg("saldo")  # nao lanca de novo
        self.assertEqual(len(self.linhas()), 3)

        self.assertIn("Aluguel", self.resp("fixos"))
        self.assertIn("cancelada", self.resp("apagar fixo 1"))
        self.agora = datetime(2027, 2, 10, 15, 0, tzinfo=timezone.utc)
        self.msg("saldo")
        self.assertEqual(len(self.linhas()), 3)

    def test_conta_fixa_repetida_atualiza_em_vez_de_duplicar(self):
        self.respostas["aluguel 1500"] = gasto("aluguel", 1500, "Moradia")
        self.respostas["aluguel 1600"] = gasto("aluguel", 1600, "Moradia")
        self.resp("fixo aluguel 1500 dia 5")
        self.assertIn("atualizada", self.resp("fixo aluguel 1600 dia 5"))
        self.agora = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
        self.msg("saldo")
        self.assertEqual([l["valor"] for l in self.linhas()], [1600.0])

    def test_lembrete_de_conta_fixa(self):
        from cashflow import bot
        self.respostas["internet 100"] = gasto("internet", 100, "Servicos")
        self.resp("fixo internet 100 dia 24")
        with self.db.sessao():
            self.assertEqual(bot.tarefas_diarias()["lembretes"], 1)
            self.assertEqual(bot.tarefas_diarias()["lembretes"], 0)  # um lembrete por dia
        self.assertIn("amanha vence *Internet*", self.enviadas[-1][1])

    # ─── dados ───

    def test_exportar_planilha(self):
        self.resp("almoço 35,90", gasto("almoço", 35.9, "Alimentacao"))
        self.msg("exportar")
        doc = self.documentos[0]
        csv = base64.b64decode(doc["media"]).decode("utf-8-sig")
        self.assertEqual(doc["fileName"], "cashflow-2026-09-23.csv")
        self.assertIn("23/09/2026;Almoço;-35,90;Saida;Alimentacao", csv)

    def test_apagar_meus_dados(self):
        self.resp("mercado 85", gasto("mercado", 85))
        self.assertIn("Tem certeza", self.resp("apagar meus dados"))
        self.assertIn("nao apaguei nada", self.resp("nao"))
        self.assertEqual(len(self.linhas()), 1)

        self.resp("apagar tudo")
        self.assertIn("Apaguei todos os seus dados (1 lancamentos)", self.resp("sim"))
        self.assertEqual(self.linhas(), [])

    # ─── entrada de mensagens ───

    def test_mensagem_repetida_pela_evolution_conta_uma_vez(self):
        self.respostas["mercado 85"] = gasto("mercado", 85)
        self.msg("mercado 85", id="MESMO-ID")
        self.msg("mercado 85", id="MESMO-ID")
        self.assertEqual(len(self.linhas()), 1)

    def test_audio(self):
        self.respostas["mercado 85"] = gasto("mercado", 85, "Alimentacao")
        respostas = self.msg(message={"audioMessage": {"seconds": 4}})  # sem base64: baixa da Evolution
        self.assertEqual(respostas[0], "🎤 _mercado 85_")
        self.assertIn("Saida registrada", respostas[1])
        self.assertIn("muito longo", self.resp(message={"audioMessage": {"seconds": 600}}))

    def test_foto_sem_legenda(self):
        self.assertIn("nao leio fotos", self.resp(message={"imageMessage": {"mimetype": "image/jpeg"}}))

    def test_texto_enorme_nao_vai_para_a_ia(self):
        self.assertIn("muito longa", self.resp("mercado 50, " * 200))
        self.assertEqual(self.chamadas_ia, [])

    def test_banco_ja_atualizado_nao_roda_ddl_de_novo(self):
        conn = self.db.conectar()
        try:
            with mock.patch.object(self.db, "SCHEMA", ["SELECT 1/0"]):  # se rodasse o SCHEMA, daria erro
                self.db.criar_tabelas(conn)
        finally:
            conn.close()

    def test_status_de_contato_e_ignorado(self):
        self.assertEqual(self.msg("bom dia", remoteJid="status@broadcast"), [])
        self.assertEqual(self.chamadas_ia, [])

    def test_usuario_novo_recebe_boas_vindas(self):
        respostas = self.msg("uber 20", gasto("uber", 20), remoteJid="5511888887777@s.whatsapp.net")
        self.assertEqual(respostas, [])  # foi para outro numero
        textos = [t for n, t in self.enviadas if n == "5511888887777"]
        self.assertIn("Eu sou o *Cash Flow IA*", textos[0])
        self.assertIn("Saida registrada", textos[1])

    # ─── assinatura (desligada por padrao) ───

    def test_assinatura_teste_gratis_e_hubla(self):
        from cashflow import config

        self._patch(config, "ASSINATURA_OBRIGATORIA", True)
        self._patch(config, "HUBLA_TOKEN", "tok")
        with self.db.sessao(), self.db.transacao() as cur:
            cur.execute("UPDATE usuarios SET criado_em = NOW() - INTERVAL '30 days'")

        self.assertIn("teste gratis", self.resp("saldo"))
        self.assertEqual(self.msg("saldo"), [])  # aviso no maximo a cada 12h

        evento = {"type": "customer.member_added", "version": "2.0.0",
                  "event": {"user": {"phone": "(11) 99999-0001"}}}  # sem o 55: tem que achar o mesmo usuario
        self.assertEqual(self.client.post("/hubla", json=evento).status_code, 401)
        r = self.client.post("/hubla", json=evento, headers={"x-hubla-token": "tok", "x-hubla-idempotency": "e1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Assinatura confirmada", self.enviadas[-1][1])

        self.assertIn("Saldo", self.resp("saldo"))

        evento["type"] = "customer.member_removed"
        self.client.post("/hubla", json=evento, headers={"x-hubla-token": "tok", "x-hubla-idempotency": "e2"})
        with self.db.sessao(), self.db.transacao() as cur:
            cur.execute("UPDATE usuarios SET aviso_assinatura_em = NULL")
        self.assertIn("teste gratis", self.resp("saldo"))

    def test_segredo_do_webhook(self):
        from cashflow import config

        self._patch(config, "WEBHOOK_SECRET", "s3cr3t")
        self.msg("saldo")
        self.assertEqual(self.ultima_resposta.status_code, 401)
        self.assertEqual(self.enviadas, [])


if __name__ == "__main__":
    unittest.main()
