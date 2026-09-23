import unittest
from datetime import date, datetime, timezone
from unittest import mock

from cashflow import util
from cashflow.ia import categoria_por_nome, normalizar_categoria, normalizar_ia


class TestValores(unittest.TestCase):
    def test_para_valor(self):
        casos = [
            (85, 85.0), (35.9, 35.9), ("35,90", 35.9), ("1.234,56", 1234.56), ("1,234.56", 1234.56),
            ("1.500", 1500.0), ("R$ 50", 50.0), (-40, 40.0), (None, 0.0), ("", 0.0), ("abc", 0.0),
            (True, 0.0), (float("nan"), 0.0), (float("inf"), 0.0), (10 ** 12, 0.0), (10 ** 400, 0.0),
        ]
        for entrada, esperado in casos:
            with self.subTest(entrada=entrada):
                self.assertEqual(util.para_valor(entrada), esperado)

    def test_parcelas_somam_o_total(self):
        self.assertEqual(util.dividir_centavos(100, 3), [33.34, 33.33, 33.33])
        self.assertEqual(util.dividir_centavos(3000, 10), [300.0] * 10)
        self.assertAlmostEqual(sum(util.dividir_centavos(999.99, 7)), 999.99, places=2)

    def test_somar_meses_limita_o_dia(self):
        self.assertEqual(util.somar_meses(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(util.somar_meses(date(2026, 11, 15), 3), date(2027, 2, 15))
        self.assertEqual(util.somar_meses(date(2026, 3, 1), -1), date(2026, 2, 1))

    def test_chave_telefone_ignora_o_nono_digito_e_o_55(self):
        mesmo_numero = ["+55 (11) 98864-6782", "551188646782@s.whatsapp.net", "(11) 98864-6782", "1188646782"]
        self.assertEqual(len({util.chave_telefone(n) for n in mesmo_numero}), 1)
        self.assertNotEqual(util.chave_telefone("5511988646782"), util.chave_telefone("5511988646783"))
        self.assertEqual(util.numero_br("(11) 98864-6782"), "5511988646782")

    def test_hoje_e_o_dia_de_brasilia(self):
        # 01:30 UTC de 24/09 ainda e 22:30 de 23/09 em Brasilia
        agora_utc = datetime(2026, 9, 24, 1, 30, tzinfo=timezone.utc)

        class Relogio(datetime):
            @classmethod
            def now(cls, tz=None):
                return agora_utc.astimezone(tz)

        with mock.patch.object(util, "datetime", Relogio):
            self.assertEqual(util.hoje(), date(2026, 9, 23))


class TestLeituraDaIA(unittest.TestCase):
    def test_campos_null_nao_quebram(self):
        r = normalizar_ia({"intencao": "Relatório", "descricao": None, "valor": None, "tipo": None, "categoria": None})
        self.assertEqual(r, {"intencao": "relatorio", "descricao": "", "valor": 0.0, "tipo": "saida", "categoria": "Outros"})

    def test_intencao_e_tipo_fora_do_padrao(self):
        self.assertEqual(normalizar_ia({"intencao": "posso gastar"})["intencao"], "posso_gastar")
        self.assertEqual(normalizar_ia({"intencao": "vender_carro"})["intencao"], "outro")
        self.assertEqual(normalizar_ia({"tipo": "Saída"})["tipo"], "saida")
        self.assertEqual(normalizar_ia({"tipo": "talvez"})["tipo"], "saida")

    def test_categorias(self):
        for c in ("Alimentação", "alimentacao", "ALIMENTACAO", " Alimentacao "):
            self.assertEqual(normalizar_categoria(c), "Alimentacao")
        self.assertEqual(normalizar_categoria("Pets"), "Pets")
        self.assertEqual(categoria_por_nome("comida"), "Alimentacao")
        self.assertEqual(categoria_por_nome("Saúde"), "Saude")
        self.assertIsNone(categoria_por_nome("pets"))


if __name__ == "__main__":
    unittest.main()
