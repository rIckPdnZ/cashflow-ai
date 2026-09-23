# Cash Flow IA — Guia

Assistente financeiro no WhatsApp: a pessoa manda "mercado 85" (por texto ou áudio) e o bot registra, categoriza, avisa sobre limites e gera relatórios.

## Como funciona

```
WhatsApp ──► Evolution API ──► /webhook (Flask no Railway) ──► Groq (IA + áudio)
                                        │
                                        └──► PostgreSQL (Supabase)
Hubla (pagamento) ──► /hubla            Cron diário (opcional) ──► /tarefas
Site: GitHub Pages (pasta docs/, domínio cashflowia.app.br)
```

| Pasta/arquivo | O que tem |
|---|---|
| `app.py` | Rotas HTTP (é o que o gunicorn roda) |
| `cashflow/bot.py` | A conversa: o que fazer com cada mensagem |
| `cashflow/interpretar.py` | Comandos, datas, parcelas e vários gastos numa mensagem — sem IA |
| `cashflow/ia.py` | Groq: classificar mensagens e transcrever áudio |
| `cashflow/relatorios.py` | Textos dos relatórios |
| `cashflow/db.py` | Banco (as tabelas são criadas/atualizadas sozinhas) |
| `cashflow/whatsapp.py` | Evolution API (v1 e v2) |
| `tests/` | Testes automáticos |

## Variáveis de ambiente (Railway → Variables)

**Obrigatórias**

| Variável | Exemplo |
|---|---|
| `DATABASE_URL` | `postgresql://user:senha@host:5432/postgres` |
| `GROQ_API_KEY` | chave do console.groq.com |
| `EVOLUTION_URL` | `http://SEU-IP:8080` |
| `EVOLUTION_KEY` | apikey da Evolution |
| `EVOLUTION_INSTANCE` | nome da instância (ex: `cashflow`) |

**Opcionais** (sem elas, tudo funciona como antes)

| Variável | Para que serve |
|---|---|
| `WEBHOOK_SECRET` | Senha do webhook. Definiu? Mude a URL do webhook na Evolution para `.../webhook?token=SUA_SENHA` |
| `ASSINATURA_OBRIGATORIA` | `1` bloqueia quem não pagou depois do teste grátis. **Padrão: desligado** |
| `DIAS_TESTE` | Dias de teste grátis (padrão `7`) |
| `LINK_PAGAMENTO` | Link mostrado para quem precisa assinar (padrão: o link da Hubla do site) |
| `HUBLA_TOKEN` | Token da Hubla para o `/hubla` liberar/bloquear assinantes sozinho |
| `ADMIN_TELEFONES` | Seus números, separados por vírgula, para usar `ativar`/`desativar` pelo WhatsApp |
| `CRON_SECRET` | Senha do `/tarefas` (lembretes de contas fixas) |
| `GROQ_MODEL` | Modelo de texto (padrão `llama-3.1-8b-instant`) |
| `GROQ_MODEL_AUDIO` | Modelo de áudio (padrão `whisper-large-v3-turbo`) |
| `AUDIO_MAX_SEGUNDOS` | Áudio mais longo que isso é recusado (padrão `180`) |
| `LOG_LEVEL` | `DEBUG` mostra o conteúdo das mensagens nos logs (padrão `INFO`, que não mostra) |

## Evolution API

Webhook da instância:
- **URL:** `https://SEU-APP.up.railway.app/webhook` (com `?token=...` se usar `WEBHOOK_SECRET`)
- **Evento:** `MESSAGES_UPSERT` (os outros não são usados)
- **Base64 no webhook:** opcional. Ligado, o áudio já vem junto; desligado, o bot baixa pela Evolution.

## Banco de dados

Não precisa rodar nada: na primeira mensagem depois do deploy o bot cria as tabelas novas e acrescenta 2 colunas em `transacoes`. Os dados existentes não são alterados. O arquivo `schema` tem o SQL completo, só para referência.

**Correção única (opcional), depois do deploy da v4:** até a v3.2 o servidor usava o horário UTC, então gastos feitos entre 21h e meia-noite (Brasília) foram gravados no dia seguinte. No Supabase → SQL Editor, veja antes o que muda e depois corrija:

```sql
SELECT id, descricao, data AS gravado, (criado_em AT TIME ZONE 'America/Sao_Paulo')::date AS correto
FROM transacoes WHERE recorrente_id IS NULL AND grupo IS NULL
  AND data <> (criado_em AT TIME ZONE 'America/Sao_Paulo')::date AND criado_em < '2026-10-01';

UPDATE transacoes SET data = (criado_em AT TIME ZONE 'America/Sao_Paulo')::date
WHERE recorrente_id IS NULL AND grupo IS NULL
  AND data <> (criado_em AT TIME ZONE 'America/Sao_Paulo')::date AND criado_em < '2026-10-01';
```

(Troque `2026-10-01` pela data do deploy da v4. Depois dela as datas já saem certas, e lançamentos com data passada, parcelas e contas fixas têm data diferente de propósito.)

## Assinatura (opcional)

Desligada por padrão: ninguém é bloqueado. Para ligar:

1. **Antes**, libere quem já paga: com seu número em `ADMIN_TELEFONES`, mande no WhatsApp do bot `ativar 5511999999999` (e `desativar ...` para tirar).
2. Na Hubla, crie um webhook para `https://SEU-APP.up.railway.app/hubla` com os eventos **Membro: acesso concedido / removido** e copie o token da Hubla para `HUBLA_TOKEN`. Pagou → o bot libera e avisa a pessoa; cancelou → bloqueia.
3. Defina `ASSINATURA_OBRIGATORIA=1`.

Quem ainda não pagou tem `DIAS_TESTE` dias contados da primeira mensagem depois de ligar. Depois recebe o link de pagamento (no máximo 1 aviso a cada 12h).

## Lembretes de contas fixas (opcional)

Defina `CRON_SECRET` e crie um cron diário (Railway Cron, cron-job.org...) chamando:

```
https://SEU-APP.up.railway.app/tarefas?token=SEU_CRON_SECRET
```

Ele lança as contas fixas do dia e manda "amanhã vence Aluguel" para quem tem conta vencendo. Sem o cron, as contas fixas continuam sendo lançadas quando a pessoa manda qualquer mensagem.

## O que o bot entende

| A pessoa manda | O que acontece |
|---|---|
| `mercado 85` · 🎤 áudio | Registra a saída com categoria |
| `salario 2500` · `pix recebido 300` | Registra entrada |
| `ontem uber 32` · `dia 15 farmacia 40` · `15/09 luz 120` | Registra com data |
| `mercado 50, uber 20 e farmacia 35` | Registra os 3 de uma vez |
| `tv 3000 em 10x` · `celular 12x de 250` | Compra parcelada (uma parcela por mês) |
| `hoje` · `semana` · `mes` · `saldo` · `extrato` | Relatórios do período |
| `agosto` · `extrato de maio` · `mes passado` · `ano` | Relatórios de outros períodos |
| `quanto gastei com uber?` · `gastos com alimentacao` | Soma, média e últimos lançamentos |
| `editar ultimo 120` · `apagar mercado` · `desfazer` | Corrige |
| `mudar categoria do ultimo para lazer` | Troca a categoria |
| `limite 2000` · `limite alimentacao 800` · `limites` | Tetos com aviso em 75%, 90% e 100% |
| `meta viagem 5000` · `guardei 200 viagem` · `metas` | Metas de economia |
| `fixo aluguel 1500 dia 5` · `fixos` · `apagar fixo 1` | Contas fixas lançadas sozinhas todo mês |
| `dica do mes` | Análise do mês: onde mais gasta, o que subiu, projeção |
| `exportar` | Planilha (CSV) com todos os lançamentos |
| `apagar meus dados` | Apaga tudo (LGPD), com confirmação |
| `ajuda` | Lista de comandos |

## Testes

```bash
pip install -r requirements.txt
python -m unittest -v                     # rápidos, sem banco

# + conversas completas num banco de TESTE (ele é apagado!)
TEST_DATABASE_URL=postgresql://postgres@localhost/cashflow_test python -m unittest -v
```

## Próximos passos possíveis

- [ ] Ler foto de comprovante (visão)
- [ ] Painel web com gráficos
- [ ] Resumo semanal automático
