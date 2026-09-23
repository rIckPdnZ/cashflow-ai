"""
Testes do Cash Flow IA. Rodam sem banco, sem Groq e sem Evolution:

    python -m unittest -v
"""
import os
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("GROQ_API_KEY", "teste")
os.environ.setdefault("DATABASE_URL", "postgresql://teste@localhost/teste")
os.environ.setdefault("EVOLUTION_URL", "http://evolution.teste")
os.environ.setdefault("EVOLUTION_KEY", "teste")
os.environ.setdefault("EVOLUTION_INSTANCE", "teste")

import app  # noqa: E402

TEL = "5511999990001@s.whatsapp.net"


def payload(texto, event="messages.upsert", **key):
    return {
        "event": event,
        "data": {
            "key": {"remoteJid": TEL, "fromMe": False, "id": "ABC123", **key},
            "message": {"conversation": texto},
        },
    }


def resposta_ia(conteudo):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=conteudo))])


class TestLeituraDaIA(unittest.TestCase):
    def test_para_valor(self):
        casos = [
            (85, 85.0), (35.9, 35.9), ("35,90", 35.9), ("1.234,56", 1234.56), ("1,234.56", 1234.56),
            ("1.500", 1500.0), ("R$ 50", 50.0), (-40, 40.0), (None, 0.0), ("", 0.0), ("abc", 0.0),
            (True, 0.0), (float("nan"), 0.0), (float("inf"), 0.0), (10 ** 12, 0.0), (10 ** 400, 0.0),
        ]
        for entrada, esperado in casos:
            with self.subTest(entrada=entrada):
                self.assertEqual(app.para_valor(entrada), esperado)

    def test_campos_null_nao_quebram(self):
        r = app.normalizar_ia({"intencao": "Relatório", "descricao": None, "valor": None, "tipo": None, "categoria": None})
        self.assertEqual(r, {"intencao": "relatorio", "descricao": "", "valor": 0.0, "tipo": "saida", "categoria": "Outros"})

    def test_intencao_e_tipo_fora_do_padrao(self):
        self.assertEqual(app.normalizar_ia({"intencao": "posso gastar"})["intencao"], "posso_gastar")
        self.assertEqual(app.normalizar_ia({"intencao": "vender_carro"})["intencao"], "outro")
        self.assertEqual(app.normalizar_ia({"tipo": "Entrada"})["tipo"], "entrada")
        self.assertEqual(app.normalizar_ia({"tipo": "Saída"})["tipo"], "saida")
        self.assertEqual(app.normalizar_ia({"tipo": "talvez"})["tipo"], "saida")

    def test_categoria_com_e_sem_acento_e_a_mesma(self):
        for c in ("Alimentação", "alimentacao", "ALIMENTACAO", " Alimentacao "):
            self.assertEqual(app.normalizar_categoria(c), "Alimentacao")
        self.assertEqual(app.normalizar_categoria("Pets"), "Pets")
        self.assertEqual(app.normalizar_categoria(None), "Outros")

    def test_extrato_junta_categorias_com_e_sem_acento(self):
        txs = [
            {"id": 1, "descricao": "Mercado", "valor": 85, "tipo": "saida", "categoria": "Alimentação"},
            {"id": 2, "descricao": "Feira", "valor": 30, "tipo": "saida", "categoria": "alimentacao"},
        ]
        texto = app.relatorio_extrato("Extrato", "01/09", "23/09/2026", txs)
        self.assertEqual(texto.count("*Alimentacao*"), 1)
        self.assertIn("*Alimentacao* — R$ 115,00", texto)


class TestDatas(unittest.TestCase):
    def test_hoje_e_o_dia_de_brasilia(self):
        # 01:30 UTC de 24/09 ainda e 22:30 de 23/09 em Brasilia
        agora_utc = datetime(2026, 9, 24, 1, 30, tzinfo=timezone.utc)

        class Relogio(datetime):
            @classmethod
            def now(cls, tz=None):
                return agora_utc.astimezone(tz)

        with mock.patch.object(app, "datetime", Relogio):
            self.assertEqual(app.hoje(), date(2026, 9, 23))

    def test_dias_restantes_conta_hoje(self):
        with mock.patch.object(app, "hoje", return_value=date(2026, 9, 30)):
            self.assertEqual(app.dias_restantes_mes(), 1)
        with mock.patch.object(app, "hoje", return_value=date(2026, 9, 1)):
            self.assertEqual(app.dias_restantes_mes(), 30)


class TestAlvoDeEditarApagar(unittest.TestCase):
    def test_extrair_id(self):
        self.assertEqual(app.extrair_id("ae3f06"), "ae3f06")
        self.assertEqual(app.extrair_id("apagar AE3F06"), "ae3f06")
        self.assertEqual(app.extrair_id("apagar 202020"), "202020")
        self.assertIsNone(app.extrair_id("apagar a decada"))
        self.assertIsNone(app.extrair_id("mercado"))

    def test_termo_busca(self):
        self.assertEqual(app.termo_busca("apagar o ultimo gasto do mercado 50"), "mercado")
        self.assertEqual(app.termo_busca("Excluir Almoço"), "almoco")
        self.assertEqual(app.termo_busca("apagar"), "")
        self.assertEqual(app.termo_busca("último"), "")


class TestExtrairMensagem(unittest.TestCase):
    def test_mensagem_normal(self):
        self.assertEqual(app.extrair_mensagem(payload("mercado 85")), (TEL, "mercado 85"))

    def test_ignora_mensagem_propria_grupo_e_historico(self):
        self.assertEqual(app.extrair_mensagem(payload("x", fromMe=True)), (None, None))
        self.assertEqual(app.extrair_mensagem(payload("x", remoteJid="123@g.us")), (None, None))

        historico = payload("mercado 500", event="messages.set")
        historico["data"] = [historico["data"]]
        self.assertEqual(app.extrair_mensagem(historico), (None, None))


class TestWebhook(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.enviadas = []

        def patch(nome, **kw):
            p = mock.patch.object(app, nome, **kw)
            self.addCleanup(p.stop)
            return p.start()

        patch("enviar", side_effect=lambda tel, texto: self.enviadas.append(texto))
        self.txs = [
            {"id": 1, "descricao": "Mercado", "valor": 85, "tipo": "saida", "categoria": "Alimentacao", "data": date(2026, 9, 1)},
            {"id": 2, "descricao": "Uber", "valor": 20, "tipo": "saida", "categoria": "Transporte", "data": date(2026, 9, 2)},
        ]
        patch("buscar_transacoes", return_value=self.txs)
        patch("buscar_limite", return_value=0.0)
        self.salvar = patch("salvar_transacao", return_value=(3, "eccbc8"))
        self.apagar = patch("apagar_transacao", return_value={"descricao": "Mercado", "valor": 85, "tipo": "saida"})

        p = mock.patch.object(app.groq_client.chat.completions, "create")
        self.addCleanup(p.stop)
        self.ia = p.start()

    def mandar(self, texto, ia=None, url="/webhook"):
        if ia is not None:
            self.ia.return_value = resposta_ia(ia)
        return self.client.post(url, json=payload(texto))

    def test_comando_exato_nao_chama_a_ia(self):
        self.mandar("Saldo?")
        self.ia.assert_not_called()
        self.assertIn("*Saldo", self.enviadas[0])

    def test_ia_fora_do_ar(self):
        self.ia.side_effect = RuntimeError("503")
        self.mandar("mercado 50")
        self.assertIn("problema tecnico", self.enviadas[0])

    def test_resposta_com_null_nao_quebra(self):
        self.mandar("como estou esse mes", '{"intencao":"relatorio","descricao":null,"valor":null,"tipo":null,"categoria":null}')
        self.assertIn("Saidas:    R$ 105,00", self.enviadas[0])

    def test_valor_em_formato_brasileiro(self):
        self.mandar("almoço 35,90", '{"intencao":"gasto","descricao":"almoço","valor":"35,90","tipo":"saida","categoria":"Alimentação"}')
        self.salvar.assert_called_once_with(TEL, "Almoço", 35.9, "saida", "Alimentacao")

    def test_apagar_por_descricao_apaga_o_lancamento_certo(self):
        self.mandar("apagar mercado", '{"intencao":"apagar","descricao":"mercado"}')
        self.apagar.assert_called_once_with(TEL, short_id=app.id_curto(1), usar_ultimo=False)

    def test_apagar_o_que_nao_existe_nao_apaga_nada(self):
        self.mandar("apagar netflix", '{"intencao":"apagar","descricao":"netflix"}')
        self.apagar.assert_not_called()
        self.assertIn("Nao achei _netflix_", self.enviadas[0])

    def test_apagar_ultimo(self):
        self.mandar("apagar ultimo", '{"intencao":"apagar","descricao":"ultimo"}')
        self.apagar.assert_called_once_with(TEL, short_id=None, usar_ultimo=True)

    def test_segredo_do_webhook(self):
        with mock.patch.object(app, "WEBHOOK_SECRET", "s3cr3t"):
            self.assertEqual(self.mandar("saldo").status_code, 401)
            self.assertEqual(self.mandar("saldo", url="/webhook?token=errado").status_code, 401)
            self.assertEqual(self.enviadas, [])

            self.assertEqual(self.mandar("saldo", url="/webhook?token=s3cr3t").status_code, 200)
            self.assertEqual(len(self.enviadas), 1)


class TestEnvio(unittest.TestCase):
    def test_lembra_o_formato_aceito_pela_evolution(self):
        # Evolution v2 so aceita {"text": ...}: a 1a mensagem testa os 2 formatos, as seguintes vao direto.
        sessao = mock.Mock()
        sessao.post.side_effect = lambda url, json, **kw: SimpleNamespace(
            status_code=201 if "text" in json else 400, text=""
        )

        with mock.patch.object(app, "http", return_value=sessao), mock.patch.object(app, "_formato_envio", 0):
            self.assertTrue(app.enviar(TEL, "oi"))
            self.assertEqual(sessao.post.call_count, 2)
            self.assertTrue(app.enviar(TEL, "oi de novo"))
            self.assertEqual(sessao.post.call_count, 3)


if __name__ == "__main__":
    unittest.main()
