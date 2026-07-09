# Robô Conexa → Webhook

Exporta a lista de clientes do Conexa (Prol Office), pega as 10 primeiras linhas e envia em JSON para o webhook. Roda a cada 8 horas via GitHub Actions.

## Como funciona

1. Loga em `proloffice.conexa.app` (POST no form de login, com token CSRF capturado da página)
2. Baixa `index.php?r=cliente/admin&show_all=1&export=excel` (o mesmo botão "Excel" da tela Listar Clientes)
3. Lê a planilha e envia as 10 primeiras linhas como JSON para o webhook

## Setup

1. Crie um repositório no GitHub e envie estes arquivos (a pasta `.github/workflows/` precisa ir junto).

2. No repositório: **Settings → Secrets and variables → Actions → New repository secret**. Crie:

   | Secret | Valor |
   |---|---|
   | `CONEXA_USER` | usuário do Conexa |
   | `CONEXA_PASS` | senha do Conexa |
   | `WEBHOOK_URL` | `https://webhook.skedula.com.br/webhook/webhook_listarclientes` |

3. Teste manualmente: aba **Actions → Exportar clientes Conexa → Run workflow**.

Depois disso ele roda sozinho a cada 8 horas (00:00, 08:00 e 16:00 UTC = 21:00, 05:00 e 13:00 em Brasília).

## Payload enviado ao webhook

```json
{
  "origem": "conexa-proloffice",
  "total_linhas_enviadas": 10,
  "colunas": ["...colunas do Excel..."],
  "clientes": [ { "Coluna": "valor", "...": "..." } ]
}
```

## Observações

- A exportação é gerada na hora pelo servidor e pode demorar alguns minutos — o script usa timeout de 10 min.
- Para mudar a quantidade de linhas, altere `NUM_LINHAS` em `exportar_clientes.py`.
- Para testar local: `pip install -r requirements.txt` e rode com as 3 variáveis de ambiente definidas.
