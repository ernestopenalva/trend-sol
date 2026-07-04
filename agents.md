## Objetivo do projeto

Bot de trend-following para SOL/USDT em paper trading na Binance Spot Testnet.

## Diretriz estrategica

Objetivo primario: preservacao de capital e gestao de risco.

Objetivo secundario: comparar, com pares reais no testnet, uma saida simples por trailing da Binance contra uma engine completa de stop, breakeven e trailing.

## Comportamento esperado do agente

Nao aplicar mudancas mecanicamente se elas aumentarem risco, misturarem responsabilidades ou quebrarem logs de auditoria.

Antes de implementar uma ideia, avaliar:

1. Isso aumenta a probabilidade de aprendizado confiavel?
2. Isso reduz risco operacional?
3. Isso preserva a comparacao pareada A/B?
4. Isso mantem parametros operacionais no YAML?

## Invariantes

- Dados de mercado vem do mercado real.
- Ordens sao executadas apenas na Binance Spot Testnet.
- Nenhuma credencial deve entrar no YAML ou no Git.
- Position A nunca deve usar `stopPrice` no trailing.
- Duas posicoes nao podem vender a mesma quantidade reservada.
- Logs JSONL sao parte do produto, nao detalhe opcional.
