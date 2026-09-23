import unittest
from datetime import date

from cashflow.interpretar import dividir_itens, extrair_data, extrair_parcelas, interpretar_comando

H = date(2026, 9, 23)  # uma quarta-feira


def cmd(texto):
    return interpretar_comando(texto, H)


class TestComandos(unittest.TestCase):
    def test_atalhos_ignoram_acento_maiuscula_e_pontuacao(self):
        self.assertEqual(cmd("Saldo?"), {"acao": "intencao", "intencao": "saldo"})
        self.assertEqual(cmd("Mês"), {"acao": "intencao", "intencao": "relatorio"})
        self.assertEqual(cmd("desfazer"), {"acao": "intencao", "intencao": "apagar", "descricao": "ultimo"})

    def test_relatorio_de_um_mes(self):
        r = cmd("extrato de agosto")
        self.assertEqual((r["acao"], r["formato"], r["ini"], r["fim"]), ("periodo", "extrato", date(2026, 8, 1), date(2026, 8, 31)))
        # "dezembro" digitado em setembro e o dezembro passado
        self.assertEqual(cmd("dezembro")["ini"], date(2025, 12, 1))
        self.assertEqual(cmd("relatorio de julho de 2025")["titulo"], "Julho 2025")
        self.assertEqual(cmd("mes passado")["ini"], date(2026, 8, 1))
        self.assertEqual(cmd("ano"), {"acao": "ano", "ano": 2026})
        # os exemplos do site
        self.assertEqual(cmd("extrato de maio")["ini"], date(2026, 5, 1))
        self.assertEqual(cmd("relatório da semana"), {"acao": "intencao", "intencao": "semana"})
        self.assertEqual(cmd("extrato da semana")["ini"], date(2026, 9, 21))
        self.assertEqual(cmd("dica do mês"), {"acao": "intencao", "intencao": "dica"})

    def test_consultas(self):
        r = cmd("Quanto gastei com Uber esse mês?")
        self.assertEqual((r["termo"], r["tipo"], r["ini"], r["fim"]), ("uber", "saida", date(2026, 9, 1), H))
        r = cmd("quanto recebi em agosto")
        self.assertEqual((r["termo"], r["tipo"], r["ini"]), ("", "entrada", date(2026, 8, 1)))
        self.assertEqual(cmd("quanto foi o ifood semana passada")["ini"], date(2026, 9, 14))
        self.assertEqual(cmd("gastos com alimentação")["termo"], "alimentacao")

    def test_limites(self):
        self.assertEqual(cmd("limite 2.000"), {"acao": "limite", "categoria": None, "valor": 2000.0})
        self.assertEqual(cmd("limite alimentação 800"), {"acao": "limite", "categoria": "alimentacao", "valor": 800.0})
        self.assertEqual(cmd("remover limite comida"), {"acao": "limite", "categoria": "comida", "valor": 0.0})
        self.assertEqual(cmd("limites"), {"acao": "limites"})

    def test_metas(self):
        self.assertEqual(cmd("meta viagem 5000"), {"acao": "meta_criar", "nome": "viagem", "valor": 5000.0})
        self.assertEqual(cmd("guardei 200 na viagem"), {"acao": "meta_mover", "sinal": 1, "valor": 200.0, "nome": "viagem"})
        self.assertEqual(cmd("tirei 100 da meta viagem")["sinal"], -1)
        self.assertEqual(cmd("apagar meta viagem"), {"acao": "meta_apagar", "nome": "viagem"})

    def test_contas_fixas(self):
        esperado = {"acao": "fixo_criar", "texto": "aluguel 1500", "dia": 5}
        self.assertEqual(cmd("fixo aluguel 1500 dia 5"), esperado)
        self.assertEqual(cmd("aluguel 1500 todo dia 5"), esperado)
        self.assertEqual(cmd("netflix 55 todo mes")["dia"], 23)
        self.assertEqual(cmd("fixo netflix"), {"acao": "fixo_ajuda"})
        self.assertEqual(cmd("apagar fixo aluguel"), {"acao": "fixo_apagar", "alvo": "aluguel"})

    def test_apagar_e_editar_sem_ia(self):
        self.assertEqual(cmd("apagar mercado"), {"acao": "intencao", "intencao": "apagar", "descricao": "mercado"})
        self.assertEqual(cmd("editar mercado 99,90")["valor"], 99.9)
        self.assertEqual(cmd("editar ultimo")["valor"], 0.0)
        self.assertEqual(cmd("apagar meus dados"), {"acao": "apagar_dados"})
        self.assertEqual(cmd("mudar categoria do ultimo para lazer")["categoria"], "lazer")

    def test_lancamentos_vao_para_a_ia(self):
        for texto in ("mercado 50", "pix 100", "oi", "quanto posso gastar hoje", "tv 3000 em 10x"):
            with self.subTest(texto=texto):
                self.assertIsNone(cmd(texto))


class TestDatas(unittest.TestCase):
    def test_datas(self):
        casos = {
            "ontem uber 30": (date(2026, 9, 22), "uber 30"),
            "uber de ontem 30": (date(2026, 9, 22), "uber 30"),
            "anteontem gasolina 150": (date(2026, 9, 21), "gasolina 150"),
            "dia 15 mercado 200": (date(2026, 9, 15), "mercado 200"),
            "dia 28 mercado 200": (date(2026, 8, 28), "mercado 200"),  # dia 28 ainda nao chegou: mes passado
            "15/09 farmacia 40": (date(2026, 9, 15), "farmacia 40"),
            "25/12 presente 100": (date(2025, 12, 25), "presente 100"),
            "na sexta pizza 60": (date(2026, 9, 18), "pizza 60"),
            "sexta passada pizza 60": (date(2026, 9, 18), "pizza 60"),
        }
        for texto, (data, resto) in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(extrair_data(texto, H), (data, resto, None))

    def test_sem_data(self):
        for texto in ("almoço 35,90", "segunda parcela do carro 500", "bom dia 50", "quintal 50"):
            with self.subTest(texto=texto):
                self.assertEqual(extrair_data(texto, H), (None, texto, None))

    def test_datas_invalidas(self):
        self.assertEqual(extrair_data("25/09 farmacia 40", H), (None, "farmacia 40", "futuro"))
        self.assertEqual(extrair_data("29/02 x 10", H), (None, "x 10", "invalida"))


class TestParcelasEItens(unittest.TestCase):
    def test_parcelas(self):
        self.assertEqual(extrair_parcelas("tv 3000 em 10x"), (10, False, "tv 3000"))
        self.assertEqual(extrair_parcelas("tv 10x de 300"), (10, True, "tv 300"))
        self.assertEqual(extrair_parcelas("celular 2400 parcelado em 12 vezes"), (12, False, "celular 2400"))
        self.assertEqual(extrair_parcelas("camisa 3xl 80"), (None, False, "camisa 3xl 80"))
        self.assertEqual(extrair_parcelas("compra 1x 50"), (None, False, "compra 1x 50"))

    def test_varios_itens(self):
        self.assertEqual(dividir_itens("mercado 50, uber 20 e farmácia 35"), ["mercado 50", "uber 20", "farmácia 35"])
        self.assertEqual(dividir_itens("gastei 50 no mercado e 20 no uber"), ["gastei 50 no mercado", "20 no uber"])
        self.assertEqual(dividir_itens("mercado 50 reais, uber 20 reais"), ["mercado 50 reais", "uber 20 reais"])
        self.assertEqual(dividir_itens("gastos de hoje:\nmercado 50\nuber 20"), ["mercado 50", "uber 20"])

    def test_um_item_so(self):
        for texto in ("almoço 35,90", "arroz e feijão 30", "tv 3000 em 10x", "Mercado, 85 reais.",
                      "2 paes, 1 leite e 3 ovos por 20"):
            with self.subTest(texto=texto):
                self.assertEqual(dividir_itens(texto), [texto])


if __name__ == "__main__":
    unittest.main()
