"""Mensagens fixas do bot."""
from cashflow import config


def boas_vindas():
    teste = ""
    if config.ASSINATURA_OBRIGATORIA:
        teste = "🎁 Voce tem *{} dias gratis* pra testar tudo.\n\n".format(config.DIAS_TESTE)

    return (
        "Oi! 👋 Eu sou o *Cash Flow IA*, seu assistente financeiro aqui no WhatsApp.\n\n"
        "E simples: me manda o que gastou ou recebeu (por texto ou audio 🎤), e eu anoto tudo pra voce.\n\n"
        + teste +
        "*Exemplos rapidos:*\n"
        "• _mercado 87_ → saida\n"
        "• _uber 32_ → saida\n"
        "• _investimento 50_ → saida\n"
        "• _salario 2500_ → entrada\n"
        "• _pix recebido 300_ → entrada\n"
        "• _ontem farmacia 45_ → com data\n"
        "• _tv 3000 em 10x_ → parcelado\n\n"
        "Pra ver seu extrato: _mes_, _hoje_ ou _saldo_ 📊\n"
        "Todos os comandos: _ajuda_\n\n"
        "Qual foi sua ultima movimentacao? 😊"
    )


MSG_AJUDA = (
    "🤖 *Cash Flow IA — Comandos*\n\n"
    "📤 *Registrar:*\n"
    "_mercado 85_ · _salario 2500_ · _pix recebido 400_\n"
    "_ontem uber 32_ · _dia 15 farmacia 40_\n"
    "_mercado 50, uber 20_ (varios de uma vez)\n"
    "_tv 3000 em 10x_ (parcelado) · 🎤 audio tambem!\n\n"
    "📊 *Relatorios:*\n"
    "_hoje_ · _semana_ · _mes_ · _saldo_ · _extrato completo_\n"
    "_agosto_ · _mes passado_ · _ano_ · _exportar_\n\n"
    "🔎 *Perguntar:*\n"
    "_quanto gastei com uber?_ · _gastos com alimentacao_\n\n"
    "✏️ *Corrigir:*\n"
    "_editar ultimo 120_ · _editar ae3f06 150_\n"
    "_apagar ultimo_ · _apagar mercado_ · _apagar ae3f06_\n"
    "_mudar categoria do ultimo para lazer_\n\n"
    "🎯 *Planejar:*\n"
    "_limite 2000_ · _limite alimentacao 800_ · _limites_\n"
    "_meta viagem 5000_ · _guardei 200 viagem_ · _metas_\n"
    "_fixo aluguel 1500 dia 5_ · _fixos_\n\n"
    "💡 *Dica:* _dica_ · 🗑️ _apagar meus dados_"
)


DICAS = [
    "💡 *Regra 50/30/20:* 50% necessidades, 30% lazer, 20% poupar.",
    "💡 Pequenos gastos somam muito. Um cafe por dia pode virar R$ 100/mes.",
    "💡 Espera 24h antes de uma compra por impulso.",
    "💡 Define um limite: _limite 2000_. Eu aviso quando estiver chegando perto! 🔔",
    "💡 Revise assinaturas mensais. É comum pagar por algo que quase nao usa.",
    "💡 Quem anota os gastos tende a gastar menos. Voce ja esta no caminho! 💪",
    "💡 Cadastre suas contas fixas (_fixo aluguel 1500 dia 5_) e eu lanco sozinho todo mes.",
    "💡 Uma meta com nome motiva mais: _meta viagem 5000_ e depois _guardei 200 viagem_.",
    "💡 Parcelado tambem pesa nos proximos meses: _tv 3000 em 10x_ lanca cada parcela no mes certo.",
]


SAUDACOES = [
    "👋 Oi! Registre um gasto ou mande _saldo_ pra ver como voce ta. 💸",
    "Oi! 🤖 Me manda algo tipo _mercado 80_ pra anotar, ou _resumo_ pra ver o dia.",
    "Ola! To aqui pra te ajudar com as financas. Qual foi a ultima movimentacao? 💰",
]


PALAVRAS_BEM_VINDO = {
    "oi", "ola", "olá", "hello", "hi", "hey",
    "inicio", "início", "start", "comecar", "começar", "menu"
}


NAO_ENTENDI = "🤖 Nao entendi!\n\n_mercado 85_ → saida\n_salario 2500_ → entrada\n_mes_ → extrato · _ajuda_ → comandos"

ERRO_IA = "🤖 Tive um problema tecnico pra ler sua mensagem. Manda de novo em instantes!"

SEM_FOTO = ("📷 Ainda nao leio fotos nem arquivos.\n\n"
            "Me manda o valor escrito, tipo _mercado 85_, ou um audio 🎤")

AUDIO_LONGO = "🎤 Esse audio e muito longo pra mim. Manda um mais curto (ate {} min) ou escreve 😉"

AUDIO_FALHOU = "🎤 Nao consegui entender o audio. Tenta de novo ou escreve, tipo _mercado 85_"

DATA_FUTURA = ("📅 Nao registro lancamentos no futuro.\n\n"
               "Pra conta que ainda vai vencer, use uma conta fixa: _fixo aluguel 1500 dia 5_")

DATA_INVALIDA = "📅 Nao entendi a data. Use _ontem_, _dia 15_ ou _15/09_."

APAGAR_DADOS = ("⚠️ *Tem certeza?*\n\n"
                "Isso apaga *todos* os seus lancamentos, limites, metas e contas fixas. Nao da pra desfazer.\n\n"
                "Responda *SIM* para apagar tudo.")

FIXO_AJUDA = ("🔁 Pra criar uma conta fixa, me diz o valor e o dia:\n\n"
              "_fixo aluguel 1500 dia 5_\n_netflix 55 todo dia 10_")


def aviso_assinatura():
    return (
        "🔒 Seu teste gratis do *Cash Flow IA* terminou.\n\n"
        "Pra continuar organizando seu dinheiro por aqui, assine:\n{}\n\n"
        "Assim que o pagamento for confirmado, eu te aviso aqui. 💚"
    ).format(config.LINK_PAGAMENTO)


ASSINATURA_ATIVA = ("✅ *Assinatura confirmada!*\n\n"
                    "Bem-vindo(a) ao Cash Flow IA. Pode mandar seus gastos e receitas que eu cuido do resto. 💚\n\n"
                    "Comandos: _ajuda_")
