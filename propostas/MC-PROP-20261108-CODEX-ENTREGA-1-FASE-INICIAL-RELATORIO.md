# Trend-Sol — Entrega 1, fase inicial

**Ponto de controle:** verificações iniciais concluídas em 08/11/2026. Pacote local preparado em `data/analysis/entrega1_20261108/package/`; validação do manifesto e do ZIP registrada separadamente. Esta entrega não inicia a campanha científica extensa.

## Paridade independente

Reexecutei os quatro comparadores em `LOW_FIRST` e `HIGH_FIRST`, trade a trade, com o mercado `marola_20261030` (corte exclusivo 30/10/2026 00:23 BRT) e código congelado `6a9ebff338857440abe12a30a71fd524f78ffc00`.

| Comparador | LOW_FIRST (N; net) | HIGH_FIRST (N; net) | Verificação |
|---|---:|---:|---|
| `BE_OFF_CB` | 1.840; −80,617681 | 1.840; −96,599092 | Trades e todos os campos da referência coincidem. O JSON direto não emite cinco campos documentais de proveniência presentes no run salvo. |
| `EMA_MACD_CLUSTER_ACT10` (C04+ACT10) | 1.020; −36,994041 | 1.021; −44,617765 | Igualdade integral com o produtor declarado `ladder_run2.py`. |
| `CHAVEADO_VIVO` | 1.340; −39,610264 | 1.341; −48,923159 | Igualdade integral do JSON. |
| `CHAVEADO_VIVO_VERDE` | 274; +4,250737 | 274; +2,123768 | Igualdade integral do JSON. |

Como verificação separada, K1 histórico também reproduziu exatamente os runs de referência: 942/943 trades, nets −31,597134/−38,111394. Isso não altera sua classificação: o shadow forward iniciado em 08/11 às 00:31 BRT segue como coorte distinta dos quatro comparadores.

**Divergências registradas:** a execução auxiliar de C04 via `switch_run.py` teve contagem, stats e campos econômicos iguais, mas diferiu em `pair_id`, `fast_evaluated`, rótulo do braço e payload de admissões. O caminho direto `ladder_run2.py`, que produziu os runs de referência, passou exatamente. `BE_OFF_CB` tem diferença só de cinco campos documentais no topo, sem divergência nos trades. Não houve correção silenciosa dos originais.

## Fontes e dados

- ZIP Claude: 1.808.539 bytes, SHA-256 `ea63804f6a5389927b22c7ccee407b4bbd8c13e6d54c56a5322c208a705199e7`; 32/32 entradas do manifesto interno verificadas.
- ZIP `marola_20261030`: 52.861.373 bytes, SHA-256 `a6745554b2c1afc6a3df7fbae682cacfb5646cef6b7b5d285839b0cab9fda939`; 224/224 hashes internos verificados. Config SHA-256 `89054f9821c220345fce2d94d4a31d1e5d1263f816a98e3b36ec68fca019d706`.
- Código congelado: 81 arquivos em `frozen/src`, árvore de hashes idêntica à referência. Adapter replay12 SHA-256 `8ff2862905023f2c8d29885a0ac96e56a25227f8a2514484d4f94ff676e80fcc`.
- Pré-registro anexado byte a byte com SHA-256 `3b1a040b040f07cd790a40d98705176f1f6b7c7472b6a83be4d3908124425f7d`. Status original preservado; não houve revalidação de Nov–Jan.
- Aquecimento BRT 18/10 00:00–01/11 00:00: 20.160 candles 1m, 4.032 5m, 1.344 15m e 336 1h, sem gap. São 14 dias, acima dos 300 candles 1h do maior lookback inventariado.
- Nov–Jan: 132.480 candles 1m e agregados 5m/15m/1h completos, sem gaps, duplicatas ou OHLC inválido.
- Jun–30/10 00:23: 217.463 candles 1m; 43.492 5m; 14.497 15m; 3.624 1h. Agregações recalculadas coincidiram com `marola_20261030`. A comparação com Binance Data Vision em Jun–Set bateu em 175.500 candles 1m.
- A reserva Fev–Mai está em diretório próprio com ZIPs mensais fonte e checksums. Não foi descompactada nem agregada. Apenas os 180 candles de 31/05 21:00–24:00 BRT foram colocados como 1m bruto na reserva, pois são a borda UTC inicial do ZIP de junho. Nenhum sinal, contexto, label, regime, replay ou métrica foi produzido para a reserva. A validação da reserva é no nível dos arquivos e checksums; não foi candle a candle.
- Janela principal: 01/11/2025 00:00 BRT → 30/10/2026 00:23 BRT exclusivo, duração 363 dias e 23 minutos. A diferença de três horas entre cortes BRT e arquivos mensais UTC foi tratada explicitamente. Os ZIPs originais permanecem intactos.

## Capacidade

Foram feitos dois pilotos seriais, sem campanha paralela:

1. Checagem técnica de candles/agregações em junho–outubro: 16,9 s; pico RSS ~127 MiB; pico privado ~120 MiB. Memória física disponível antes/depois: ~721/756 MiB.
2. Motor replay em junho, um braço/path/custo para capacidade apenas (`BE_OFF_CB_SHADOW`, `LOW_FIRST`, `BASE`): 7,0 s de parede, 6,8 s de CPU, pico RSS ~148 MiB e pico privado ~140 MiB. Memória física disponível antes/depois: ~859/983 MiB. O objeto econômico foi descartado em memória, sem valores econômicos impressos ou persistidos. Não medi I/O de swap.

## Limites e próximos gates

- A reserva Fev–Mai está preservada como fonte bruta isolada; seus candles ainda não foram descompactados nem validados individualmente. Esta limitação respeita a proibição de produzir derivados durante a reserva.
- Nenhuma revalidação Nov–Jan foi rodada. O pré-registro define a lista fechada e requer nova autorização para execução.
- Não houve replay anual contínuo, geração anual de sinais/labels/contextos/regimes, resultados estratégicos adicionais ou campanha extensa.
- Artefatos 30/10–06/11 no pacote da sessão Claude estão identificados como estudos já explorados, não holdout. Dados após 06/11/2026 20:13 BRT não foram incorporados à base.
- Nenhuma mudança no bot, runtime, YAML operacional, VPS ou estado científico da MC; nenhum commit ou deploy.

## Arquivos entregues

- Pacote: `data/analysis/entrega1_20261108/package/`
- Inventário de fontes: `package/reports/INVENTARIO_FONTES.json`
- Paridade independente: `package/reports/PARIDADE_INDEPENDENTE.json`
- Integridade da base: `package/reports/dataset_integrity.json`
- Piloto do motor: `package/reports/capacity/capacity_pilot_engine.json`
- Pré-registro: `package/source/revalidation_preregister/PREREGISTRO.md`



## Pacote compactado e QA final

O arquivo `data/analysis/entrega1_20261108/TREND_SOL_ENTREGA1_PACOTE_CIENTIFICO_20261108.zip` foi criado e validado. Tamanho: **94.761.787 bytes**; SHA-256: `89b3aab38776f1944dad3825885717d8dda1ec27ca2e582c7f2426423ab4c05b`. Contém 190 arquivos, 196.379.263 bytes descompactados. O teste CRC do ZIP passou e os 189 arquivos listados no manifesto interno conferem por tamanho e SHA-256. A pasta de reserva contém apenas ZIPs/checksums de origem e o arquivo bruto 1m de 180 candles da fronteira; não há agregações nem sinais/labels/contextos.

Recibo externo de QA: `data/analysis/entrega1_20261108/PACKAGE_ARCHIVE_RECEIPT.json`. Manifesto interno: `package/MANIFEST_SHA256.json` (SHA-256 `c0bd6352543b01db240c891e08433b49f350025f6a11299c8d01994f12fc5942`).
