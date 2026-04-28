# CashFlow AI — Guia de Deploy Completo

## O que você vai precisar (tudo gratuito para começar)

- Conta no GitHub: https://github.com
- Conta no Railway: https://railway.app
- Conta no Twilio: https://twilio.com
- Conta na Anthropic (API): https://console.anthropic.com
- Conta no Hub.la (pagamentos): https://hub.la

---

## PASSO 1 — Subir o código no GitHub

1. Acesse https://github.com e crie uma conta
2. Clique em "New repository"
3. Nome: `cashflow-ai`
4. Clique em "Create repository"
5. Faça upload dos 3 arquivos: `app.py`, `requirements.txt`, `Procfile`

---

## PASSO 2 — Deploy no Railway

1. Acesse https://railway.app e faça login com o GitHub
2. Clique em "New Project" → "Deploy from GitHub repo"
3. Selecione o repositório `cashflow-ai`
4. O Railway vai detectar automaticamente e fazer o deploy

### Adicionar variáveis de ambiente no Railway:
Vá em Settings → Variables e adicione:

```
ANTHROPIC_API_KEY = sua_chave_aqui
TWILIO_ACCOUNT_SID = sua_chave_aqui
TWILIO_AUTH_TOKEN = sua_chave_aqui
```

5. Após o deploy, copie a URL gerada (ex: cashflow-ai.up.railway.app)

---

## PASSO 3 — Configurar o Twilio (WhatsApp)

1. Acesse https://twilio.com e crie uma conta
2. Vá em "Messaging" → "Try it out" → "Send a WhatsApp message"
3. No Sandbox do WhatsApp, configure o Webhook:
   - "When a message comes in": `https://SUA-URL.railway.app/webhook`
   - Método: POST
4. Teste mandando uma mensagem pro número do sandbox

### Para número próprio (produção):
- Compre um número WhatsApp Business no Twilio (~$15/mês)
- Configure o webhook no número comprado

---

## PASSO 4 — Configurar pagamentos no Hub.la

1. Acesse https://hub.la e crie uma conta
2. Crie um produto digital chamado "CashFlow AI"
3. Configure os planos:
   - Mensal: R$ 19,90/mês
   - Anual: R$ 97/ano (ou 12x R$ 8,97)
4. O Hub.la gera um link de pagamento automático
5. Coloque esse link no seu TikTok/Instagram

---

## PASSO 5 — Verificar pagamento (versão simples)

No arquivo `app.py`, a função `usuario_ativo()` está retornando `True` para todos.

Para checar pagamento real, você tem duas opções:

### Opção A (mais fácil): Lista manual
Crie uma tabela `usuarios_pagos` no banco e adicione o WhatsApp
de cada cliente manualmente após a compra no Hub.la.

### Opção B (automático): Webhook do Hub.la
O Hub.la pode enviar uma notificação automática quando alguém compra.
Configure o webhook do Hub.la para chamar:
`https://SUA-URL.railway.app/nova-venda`

---

## Custos mensais estimados

| Serviço        | Custo           |
|----------------|-----------------|
| Railway        | ~$5/mês         |
| Twilio sandbox | Grátis (teste)  |
| Twilio número  | ~$15/mês        |
| Claude API     | ~$5-20/mês      |
| **Total**      | **~R$ 200/mês** |

Com 20 assinantes a R$ 19,90 = R$ 398/mês → já paga tudo.

---

## Comandos que o CashFlow AI entende

| O usuário manda    | O que acontece                     |
|--------------------|------------------------------------|
| `uber 27`          | Registra R$27 em Transporte        |
| `almoço 35,90`     | Registra R$35,90 em Alimentação    |
| `relatório`        | Envia resumo do mês atual          |
| `limite 2000`      | Define limite mensal de R$2.000    |
| `ajuda`            | Mostra instruções de uso           |

---

## Próximas funcionalidades para adicionar

- [ ] Registro por áudio (Whisper API)
- [ ] Registro por foto (visão do Claude)
- [ ] Webhook automático de pagamento Hub.la
- [ ] Painel web para o usuário ver gráficos
