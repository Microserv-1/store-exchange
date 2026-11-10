# Store Exchange Service

Serviço de câmbio em **Python/FastAPI** (`GET /exchanges/{from}/{to}`) integrado a provedores externos (**AwesomeAPI** como primário e **Open ExchangeRate-API** como fallback) para conversão de moedas na plataforma Microserv Store.

## Endpoints

| Método | Rota | Autenticação | Resposta |
|---|---|---|---|
| `GET` | `/exchanges/{from}/{to}` | Bearer JWT | `200 OK`: `sell`, `buy`, `date`, `id-account`; `422` para moeda não suportada; `503` em falha externa |
| `GET` | `/exchange/{from}/{to}` | Bearer JWT | Alias de compatibilidade para a rota canônica `/exchanges/{from}/{to}` |
| `GET` | `/health-check` | Público | Saúde do processo |
| `GET` | `/ready` | Público | Prontidão e moedas suportadas |
| `GET` | `/metrics` | Público (interno) | Métricas Prometheus (`exchange_external_requests_total`) |
