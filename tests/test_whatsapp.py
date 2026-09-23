import base64
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from cashflow import whatsapp
from cashflow.whatsapp import extrair_mensagem

TEL = "5511999990001@s.whatsapp.net"


def payload(message, event="messages.upsert", **key):
    return {"event": event, "data": {"key": {"remoteJid": TEL, "fromMe": False, "id": "ABC", **key}, "message": message}}


class TestExtrairMensagem(unittest.TestCase):
    def test_texto(self):
        m = extrair_mensagem(payload({"conversation": "mercado 85"}))
        self.assertEqual((m.telefone, m.texto, m.tipo, m.id), (TEL, "mercado 85", "texto", "ABC"))
        m = extrair_mensagem(payload({"ephemeralMessage": {"message": {"extendedTextMessage": {"text": "uber 20"}}}}))
        self.assertEqual(m.texto, "uber 20")

    def test_ignora_o_que_nao_e_conversa_com_o_bot(self):
        casos = {
            "propria": payload({"conversation": "x"}, fromMe=True),
            "grupo": payload({"conversation": "x"}, remoteJid="123@g.us"),
            "status dos contatos": payload({"extendedTextMessage": {"text": "bom dia"}}, remoteJid="status@broadcast"),
            "canal": payload({"conversation": "x"}, remoteJid="123@newsletter"),
            "figurinha": payload({"stickerMessage": {}}),
            "outro evento": payload({"conversation": "x"}, event="connection.update"),
        }
        historico = payload({"conversation": "mercado 500"}, event="messages.set")
        historico["data"] = [historico["data"]]
        casos["historico ao reconectar"] = historico

        for nome, dados in casos.items():
            with self.subTest(nome):
                self.assertIsNone(extrair_mensagem(dados))

    def test_contato_lid_usa_o_numero_real(self):
        m = extrair_mensagem(payload({"conversation": "x"}, remoteJid="123456@lid", remoteJidAlt=TEL))
        self.assertEqual(m.telefone, TEL)
        m = extrair_mensagem(payload({"conversation": "x"}, remoteJid="123456@lid"))
        self.assertEqual(m.telefone, "123456@lid")

    def test_midia(self):
        m = extrair_mensagem(payload({"audioMessage": {"seconds": 7}, "base64": "b2k="}))
        self.assertEqual((m.tipo, m.segundos, m.base64), ("audio", 7, "b2k="))
        self.assertEqual(extrair_mensagem(payload({"imageMessage": {}})).tipo, "imagem")
        self.assertEqual(extrair_mensagem(payload({"imageMessage": {"caption": "mercado 85"}})).texto, "mercado 85")

    def test_payload_inteiro_em_base64(self):
        embrulhado = {"base64": base64.b64encode(json.dumps(payload({"conversation": "oi"})).encode()).decode()}
        self.assertEqual(extrair_mensagem(embrulhado).texto, "oi")


class TestEnvio(unittest.TestCase):
    def test_lembra_o_formato_aceito_pela_evolution(self):
        # Evolution v2 so aceita o formato "achatado" ({"text": ...}, {"mediatype": ...}): a 1a mensagem
        # testa os 2 formatos, as seguintes vao direto no que funcionou.
        sessao = mock.Mock()
        sessao.post.side_effect = lambda url, json, **kw: SimpleNamespace(
            status_code=201 if ("text" in json or "mediatype" in json) else 400, text=""
        )

        with mock.patch.object(whatsapp, "http", return_value=sessao), mock.patch.object(whatsapp, "_formato_envio", 0):
            self.assertTrue(whatsapp.enviar(TEL, "oi"))
            self.assertEqual(sessao.post.call_count, 2)
            self.assertTrue(whatsapp.enviar(TEL, "oi de novo"))
            self.assertEqual(sessao.post.call_count, 3)
            # e o documento ja vai direto no formato v2
            self.assertTrue(whatsapp.enviar_documento(TEL, "a.csv", b"x", "text/csv"))
            self.assertEqual(sessao.post.call_count, 4)
            self.assertIn("mimetype", sessao.post.call_args.kwargs["json"])


if __name__ == "__main__":
    unittest.main()
