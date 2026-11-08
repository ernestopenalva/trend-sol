# Trend-Sol — preparação da auditoria científica integral

## Entrega 0 — Inventário e plano técnico

**Preparado por:** Codex  
**Data:** 08/11/2026  
**Estado:** proposta para revisão de Ernesto e Claude  
**Escopo:** inventário e planejamento; sem implementação da base anual, campanha de replay, mudança operacional ou atualização da MC oficial.

## Síntese executiva

O material essencial para começar existe: repositório local versionado, quatro ou mais motores/adaptadores de replay, indicadores, reconstrutores de sinais e contextos, código congelado em pacotes históricos, dados de candles e uma Memória Científica navegável no Drive. A MC oficial está no Drive; a pasta local `mc_consolidacao_tmp` é material de consolidação, não substitui a fonte oficial.

Ainda não há um pacote único que dê aos dois auditores evidências equivalentes, comandos reproduzíveis e uma separação segura entre dados de desenvolvimento e o período reservado. Há anexos cujo caminho original era de sessão externa, resultados cuja paridade está apenas relatada/documentada, e mais de uma referência de janela final. Esses pontos são corrigíveis, mas precisam ser resolvidos antes de chamar o pacote reproduzível.

**Viabilidade:** sim. O download e os arquivos de mercado de um ano cabem com grande folga no disco local observado. O limitante mais provável é RAM ao materializar séries, carteiras e curvas intraminuto, sobretudo com quatro processos paralelos. A base e a maioria dos derivados são pequenos comparados aos 14 GB de dados históricos que já existem no workspace.

**Esforço indicativo:** 2–4 dias úteis para construir e verificar um pacote de evidências, caso as fontes anexadas estejam acessíveis; mais 1–3 dias se for necessário recuperar artefatos externos ou depurar paridade. Não é medição de tempo de execução. Replays anuais e downloads completos não foram feitos nesta Entrega 0.

**Gate metodológico principal:** usar novembro/2025–janeiro/2026 somente para hipóteses antigas com hipótese, regra e métrica congeladas previamente. Fevereiro–maio/2026 permanece integralmente reservada; nada de replay econômico, resultado ou derivado até autorização expressa de liberação. Como essa faixa precede junho–outubro/2026, ela pode ser amostra histórica retida, mas não deve ser chamada de “forward OOS”.

## A. Inventário

### A.1 Repositório e ambiente

- Raiz: `C:\dev\trend-sol`.
- HEAD observado: `e34629a5acab304bc0c69581479a8169112c7e19` (`Implementação de EMA-MACD-CLUSTER-ACT10-K1-01`, 08/11/2026).
- Árvore de trabalho Git limpa na inspeção; remoto `origin` aponta para `ernestopenalva/trend-sol` no GitHub. Nenhuma operação Git foi feita.
- 383 arquivos rastreados pelo Git na inspeção.
- Runtime declarado no Dockerfile: Python 3.12; dependências diretas em `requirements.txt` são fixadas em quatro versões, mas não há lockfile/transitivas com hash nem digest de imagem registrados.
- Máquina local no instante da consulta: 7,7 GiB RAM total, 0,8 GiB livre, 4 processadores lógicos, 28,85 GB livres no volume C:. O inventário `data/` soma aproximadamente 13,9 GB (`analysis`: 9,87 GiB; `studies`: 2,98 GiB, arredondados pelo PowerShell). São leituras pontuais, não garantia de espaço futuro.
- Pastas relevantes: `src/indicators/`, `src/monitor/`, `src/position/`, `tools/`, `tests/`, `config/config.yaml`, `requirements.txt`, `Dockerfile`, `docker-compose.yml`.
- O braço shadow atual `EMA_MACD_CLUSTER_ACT10_K1_SHADOW` foi iniciado em **08/11/2026 00:31 BRT**, conforme marco operacional informado por Ernesto. É uma coorte forward/shadow posterior e separada; não é um dos quatro comparadores históricos nem deve ser retroativamente inserido em seus replays.

### A.2 Memória Científica oficial

Fonte consultada: pasta raiz da MC no Google Drive, aberta pelo conector conectado.

- Índice oficial `README.md`, versão 19, última atualização declarada 07/11/2026.
- `PROTOCOLO.md`, v0.3.2, estado `APROVADO_GOVERNANCA`, regras R01–R12.
- `PAINEL.md` e `PAINEL.html`; cinco linhas científicas (MC-LIN-0001–0005), 29 decisões, 44 arquivos em metodologia, 17 registros/objetos na pasta de experimentos e 90 propostas. O índice tem referências a hipóteses e estudos por pergunta. A pasta `conclusoes/` não contém arquivos diretos no Drive consultado; o índice descreve a ausência de conclusões homologadas.
- A estrutura oficial também contém `cronologia`, `hipoteses`, `versoes`, `ferramentas`, manifestos administrativos e anexos. O índice e o protocolo orientam a rastreabilidade e exigem separar decisão, validade, estado científico e apresentação gerencial.
- Há manifestos externos com hashes, versões anteriores e fontes originais. Por exemplo, MC-EXP-0008 registra um ZIP de 52.861.373 bytes e SHA-256 `a6745554b2c1afc6a3df7fbae682cacfb5646cef6b7b5d285839b0cab9fda939`; a ficha afirma 46/46 verificações salvas, conferidas documentalmente por hash, mas não reexecutadas durante sua incorporação.
- MC-EXP-0010 registra `CHAVEADO_VIVO` e `CHAVEADO_VIVO_VERDE`, fontes e ESPEC com hashes, janela até 30/10/2026 00:23 BRT exclusiva, caminhos LOW_FIRST/HIGH_FIRST e resultados ainda pendentes de homologação. A ficha qualifica as paridades como relatos do autor; registra ausência de pré-registro autônomo anexado e dependências de sessão externa/intermediários.
- MC-EXP-0015 documenta `C04+ACT10`/`BE_OFF_CB_EMA_MACD_CLUSTER_ACT10`, comparador e arquivos originais de dados/script/hash. A ficha ressalta limites de reprodução e homologação.

**Prontidão documental:** a MC é navegável e os registros científicos separam fatos, validade e decisões. O pacote para auditoria ainda precisa baixar/espelhar de forma curada os arquivos referenciados pelos registros: caminhos do Drive, IDs, versões, hashes, disponibilidade, instruções de reprodução e anexos. A simples existência de hash não assegura backup, reprodução independente ou equivalência científica.

### A.3 Código, dados e motores disponíveis

Componentes locais relevantes:

| Componente | Localização | Observação para auditoria |
|---|---|---|
| Indicadores e geração de sinais | `src/indicators/indicators.py`, `src/monitor/entry_engine.py`, `src/monitor/market_context.py` | Os cálculos podem ser alimentados por candles ordenados; é necessário fixar a versão congelada e a ordem causal dos timeframes. |
| Runtime | `main.py`, `src/app.py`, `src/monitor/`, `src/position/` | WebSocket, callbacks, checkpoint, carteira, ordens e execução têm estados/temporização que OHLC não reproduz integralmente. |
| Regime / ONDA / MAROLA | `src/monitor/price_structure.py`, `tools/replay12_reconstruct.py`, `tools/marola_labels.py` | Há versões V1/V2 e regras de empate distintas por versão; usar a implementação congelada correspondente a cada estudo, não importar automaticamente a atual. |
| Replay de 12 braços | `tools/replay12_engine.py` e `data/analysis/replay_12bracos_20261021/frozen/` | Engine descreve fidelidade `IDEALIZED_OHLC_NOT_RUNTIME_EQUIVALENT`; inputs são materializados em memória e saídas incluem trajetória minuto a minuto. |
| Reconstrução causal | `tools/replay12_reconstruct.py` | Reexecuta entradas 1m e entrega fechada 5m/15m à engine; grava sinais/contextos e regimes. |
| Replay MAROLA / combos | `data/analysis/marola_20261030/codigo/`, `tools/marola_replay.py`, `tools/marola_labels.py` | A pasta local preserva cópias congeladas do adaptador e scripts. Orquestrador local usa quatro shards em paralelo; evitar esse paralelismo na primeira campanha anual. |
| Download de candles | `tools/download_1m_candles.py` | API pública REST, paginação de 1.000 candles, gravando OHLC, quote volume e número de trades. Datas são convertidas para milissegundos. |
| Configuração | `config/config.yaml`, `src/config_profiles.py`; `config.json` dentro de cada pacote | A configuração operacional atual não é substituto de configuração histórica congelada. O pacote deverá fixar configurações e SHA por braço. |
| Testes | `tests/` | Cobrem diversos componentes e contratos locais; nesta etapa não rodei a suíte nem inferi que todos os caminhos históricos estejam cobertos. |

Os pacotes locais mostram duas camadas de congelamento: o repositório principal atual está no HEAD de 08/11, enquanto os motores MAROLA/replay registram `frozen_code_head` `6a9ebff338857440abe12a30a71fd524f78ffc00`. O harness de replay também tem versões próprias/hash-addressed. Congelar apenas o `src/` ou citar apenas o HEAD atual não basta: cada execução deve carregar o hash do harness, do código-fonte congelado, da composição e do YAML/JSON efetivo.

### A.4 Pacotes e referências locais existentes

- `data/analysis/replay_12bracos_20261021/`: 245 arquivos, ~636 MiB. Contém 1m/5m/15m/1h, `signals.json`, contextos, regimes, 72 runs (três cenários de custo × dois paths × doze braços), configuração, manifests, reports e código congelado. O manifesto reporta `internal_missing_1m: 0`; candles de aquecimento começam em 29/05/2026.
- `data/analysis/marola_20261030/`: 238 arquivos, ~378 MiB descompactados, 38 runs e pacote de mercado estendido. O ZIP registrado pela MC tem ~50,4 MiB decimal. O manifesto reporta prefixo 1m anterior preservado byte a byte e zero lacunas internas, com cauda pública REST.
- `data/analysis/marola_extensao_20261106_work/`: diretório de trabalho local com 246 arquivos, ~487 MiB; não foi tratado como pacote oficial ou entregue porque a tarefa desta Entrega 0 não autorizou estender ou republicar esse material.
- `data/analysis/replay_stack_guard_2neg_20261026/` e outros diretórios contêm referências específicas, reports e verificações adicionais.
- Resultados de referência do motor de 12 braços ocupam ~410 MiB em runs; os 38 runs MAROLA ocupam ~228 MiB.

### A.5 Quatro braços que deverão ser congelados

Não se deve deduzir os quatro braços somente do `config.yaml` atual. A cadeia de proveniência precisa ligar cada braço a registro MC, ESPEC/decisão aplicável, configuração efetiva, script/harness e arquivo de resultado:

| Braço pedido | Fonte documental/artefato conhecido | Situação a esclarecer na Entrega 1 |
|---|---|---|
| `EMA_MACD_CLUSTER_ACT10` | MC-EXP-0015 (C04+ACT10), MC-LIN-0004 e `switch_run.py` preservado em fontes da proposta. Há resultado-base referenciado de 1.020/1.021 trades em material ligado ao estudo. | Obter a versão exata dos dados/script/config para toda a janela; não substituir por K1 ou por parâmetros atuais. Confirmar a qual ID de execução pertencem esses denominadores. |
| `CHAVEADO_VIVO` | MC-EXP-0010, MC-DEC-0023, versão anexada de `ESPEC_BOT_20261101.md`, leitura e hashes. | Reunir as fontes externas/intermediários citados e reexecutar, pois a ficha marca paridade como relatada, sem reprodução independente. |
| `CHAVEADO_VIVO_VERDE` | MC-EXP-0010 e seus mapas/configurações por casa. | Congelar o mapa de casas e as cinco casas verdes como eram na execução; guardar seleção original e não reotimizar no pacote. Mesmas ressalvas de dependências e paridade. |
| `BE_OFF_CB` | Controle nos pacotes MC-EXP-0008/0010, MC-LIN-0002/0004 e configurações congeladas. | Especificar exatamente a variante (p.ex. `BE_OFF_CB_SHADOW` ou adapter de mesmo rótulo), CB, custos, inicialização e ledger de referência; não presumir equivalência com o `BE_OFF_CB` operacional. |

**Braço forward separado:** `EMA_MACD_CLUSTER_ACT10_K1_SHADOW`, iniciado em 08/11/2026 00:31 BRT. Os runs históricos de K1 no cenário C04+ACT10 são testes de paridade da regra, não trades forward do shadow. Para avaliação forward, preservar ledger, eventos, versão e marco de início a partir do restart; não misturar esses registros com os quatro comparadores acima.

### A.6 Fonte de reprodução Claude (01–08/11/2026)

O pacote [MC-PROP-20261108-CLAUDE-PACOTE-SESSAO-01](https://drive.google.com/drive/folders/1QZhrYUk-aKhjgr3VvWe5dGSsjjGne_7d) foi inspecionado pelo README e manifesto. A pasta no Drive contém `pacote_sessao_claude_20261108.zip` (1.808.539 bytes), `README.md` e `SHA256SUMS.txt`. O ZIP contém scripts, comandos, rótulos e runs de referência selecionados; os ~400 MB de runs exploratórios não foram incluídos. O SHA256SUMS preserva hashes de membros; hash externo do ZIP não aparece no README/manifesto e deve ser calculado numa futura cópia de staging. O arquivo-fonte Drive deve ficar intacto.

O README determina `marola_20261030` como pacote de mercado autoritativo para todas essas execuções (corte exclusivo 30/10/2026 00:23 BRT, `end_ms=1793330580000`); `replay_12bracos_20261021/frozen/` fornece apenas o código congelado indicado pelo README. Os scripts mantêm caminhos da sessão Claude e monkeypatches em memória de `replay12_engine.py`; portanto, guardar os comandos/scripts exatos e reproduzi-los antes de qualquer refatoração.

| Referência declarada no README | LOW_FIRST (N; net) | HIGH_FIRST (N; net) | SHA256 dos runs LOW/HIGH no `SHA256SUMS.txt` |
|---|---:|---:|---|
| C04+ACT10 (`BE_OFF_CB_EMA_MACD_CLUSTER_ACT10`) | 1.020; −36,994041 | 1.021; −44,617765 | `45cd399c22339d9b5be0e54909257fffbeaa562319f47961c8d65731c8757b3d` / `38cdbcbd4107f2a7a2579203a6b162c843313cfe5ba086f498bf7b4fc07bf15a` |
| `CHAVEADO_VIVO` mapa final | 1.340; −39,610264 | 1.341; −48,923159 | `ee8d88ae1678ed06e29379b9fcb618ea400e7cd75878ab934ba0384e8bfd62c7` / `de0e26866ded78b1d836b57fe9ce86defb3c0e3930819bdb4c20c8d9fc37164e` |
| `CHAVEADO_VIVO_VERDE` estrito | 274; +4,250737 | 274; +2,123768 | `57db4a3a18cab3c764b5a5a54ac1ecf71f03220ec1d3e86941ada68eaae1a0fe` / `fa21f3f6b85b4d34e236e88571f35d401389c7dcf929359d85314ffe55284aa3` |
| K1, teste de paridade da regra | 942; −31,597134 | 943; −38,111394 | `d9fa7519f616f03e2c4164acf168142bb7972d6b83736eca7244385a475f7bb5` / `6889e2279467c46aeaf795e35e069346975340a0061ff0ffad7fb2edc0e13d79` |

O README relata que três variantes LOW_FIRST e `C04+ACT10` foram reexecutados na sessão Claude; isso é evidência relatada, não reprodução independente do Codex nem confirmação de ambos os paths. O pacote registra também rótulos V1/V2, scripts de velocidade, profundidade, ângulos, escadinhas e análises. Entre os hashes de membros estão `switch_run.py` `1bbcb3b9a50b062f7fdc51d1be74d7644d8a9a6adb122df623525302acdcf0f6`, `ladder_run2.py` `a67e97e18e8d2ac341ac6c88dc51ab284fa923ee4d169e8086f7169a197498ed`, `build_labels2.py` `6c2d1f68a84914d68e7fb26a48128e0ebe2111b063a5d1c76adc948d43472af`, `replay12_engine.py` `8ff2862905023f2c8d29885a0ac96e56a25227f8a2514484d4f94ff676e80fcc`, `velocity.py` `b6a2e93bba181e3ce6e52cab726e882374e97de23d15509b11205b2342da3b59` e os oito hashes de runs acima. Runs e derivados de 30/10 a 06/11 são material histórico já explorado; não compõem reserva independente.

O recibo já existente no workspace, `data/analysis/ema_macd_cluster_act10_k1/PARIDADE_COMPLETA.json`, registra validação Codex dos dois paths K1: LOW 942/net −31,597133876/186 bloqueios e HIGH 943/net −38,111394457/184 bloqueios; stats, admissões e campos econômicos por trade coincidem (exceto IDs de par). O campo de auditoria `fast_evaluated` difere em 942/943 trades. Esse recibo valida a comparação local K1 declarada nele, não substitui reprodução dos outros braços do pacote de sessão.

## B. Diagnóstico de prontidão

### O que já está pronto

1. A arquitetura de dados 1m → 5m/15m/1h, formato JSONL, manifests SHA-256, registro de versão congelada e validação de candles nativos existe em pacotes anteriores.
2. A reconstrução de sinais e contexto percorre candles fechados em ordem; indicadores e rótulos são reproduzíveis a partir das fontes e configurações congeladas, quando os inputs completos estão disponíveis.
3. Há pelo menos duas referências de replay sistêmico, com caminhos LOW_FIRST e HIGH_FIRST, métricas de trades/carteira e scripts de verificação.
4. A MC registra os objetos metodológicos, ressalvas, decisões, hipóteses e origens, e tem estrutura de versões e anexos.
5. O volume puro de candles de um ano é modesto. Espaço em disco não é impedimento hoje.

### O que falta ou está incerto

1. **Inventário consolidado de anexos:** obter todos os artefatos citados pela MC, conferir se continuam acessíveis no Drive, trazer os relevantes para uma entrega autocontida e mapear cada link/hash a um caminho incluído ou dependência externa explícita.
2. **Unicidade de referência temporal:** o manifesto do replay12 local acaba em **21/10/2026 23:56 BRT**; o manifesto MAROLA/MC-EXP-0008 e MC-EXP-0010 usam **30/10/2026 00:23 BRT exclusivo**. São objetos diferentes. Além disso, a paridade salva de alguns estudos usa um corte anterior. A Entrega 1 precisa listar, para cada braço/path, qual artefato é a referência autoritativa e qual intervalo efetivamente cobre. Não comparar uma execução anual contra nomes de runs de outro corte.
3. **Paridade não é homogênea:** MC-EXP-0008 registra 46/46 checks persistidos; MC-EXP-0010 declara resultados/paridade dos chaveados como relatados e não reproduzidos nesta incorporação. Evidência documental não equivale a uma reexecução independente. O pacote de sessão Claude recuperado agora identifica casos reexecutados na sessão de origem, mas não substitui a reprodução independente pelo Codex na Entrega 1.
4. **Código externo:** MC-EXP-0010 aponta dependências de caminhos de sessão Claude, pacotes e intermediários. Embora algumas cópias e hashes tenham sido preservados no Drive, é necessário recuperar e executar os artefatos originais num ambiente limpo, sem corrigir scripts em silêncio.
5. **Exatidão de dados e lacunas:** o prefixo histórico preservado e a nova fonte de arquivo precisam ser comparados. Dados atuais podem divergir de snapshots históricos por revisão da fonte, normalização ou agregação. A política para lacunas precisa ser decidida e registrada antes da reconstrução.
6. **Partições históricas:** junho–outubro/2026 para auditoria/desenvolvimento; novembro/2025–janeiro/2026 para revalidação limitada após congelar a hipótese antiga; fevereiro–maio/2026 como reserva integral sem qualquer replay econômico ou derivado antes da liberação. O período posterior a 06/11/2026 20:13 BRT será avaliado cronologicamente em objeto separado.
7. **Reprodução live impossível em alguns aspectos:** API histórica fornece candles, não o feed recebido, ordem de callbacks, estado exato de cada checkpoint, fills, spread/slippage intratick, erros de ordem ou disponibilidade histórica do websocket. Os outputs devem ser rotulados conforme o protocolo MC como `RUNTIME_RECONSTRUIDO` ou `REGRA_IDEALIZADA`, não como execução observada.
8. **Ambiente:** dependências diretas estão fixadas, mas não há lockfile de dependências transitivas nem digest OCI. Nenhum dado sensível/credencial deve ir ao pacote; os segredos precisam ser excluídos explicitamente e o pacote verificado.

### Limitações metodológicas já conhecidas a carregar para os auditores

- OHLC1m idealiza o caminho intrabar; LOW_FIRST/HIGH_FIRST são cenários, não amostras independentes nem ticks verdadeiros.
- Fills Phantom/Testnet e futuros fills reais diferem; não inferir execução real.
- Fees, spread, slippage e custo podem variar por estudo e precisam ser congelados por artefato, sem normalização retrospectiva silenciosa.
- Capital, sizing, CB, vagas, spacing, buckets e estados iniciais criam dependência de trajetória; resultado por trade isolado não substitui replay sistêmico.
- Fechamentos abertos no cutoff devem permanecer abertos e ter MTM conforme a convenção original, sem liquidação inventada.
- Janelas, labels, parâmetros e mapas foram reutilizados para várias hipóteses; seleção de casas e análise posterior podem ser pós-hoc.
- Resultados do replay, shadow e Testnet/forward são objetos distintos. Hash, teste de código e paridade numérica não homologam validade científica.
- Ausência de um artefato ou execução não deve ser preenchida por memória, números de conversa ou script reconstruído após os resultados.

## C. Plano de construção da Entrega 1

1. **Fechar o mapa de evidência:** congelar os quatro IDs de braço, path, versão do config, source/adapter, script, dados e resultados de referência; listar faltantes/ambíguos, sem os corrigir.
2. **Recuperar a evidência MC:** para cada item relevante, copiar a fonte original e preservar título, Drive ID/URL, data, bytes, SHA-256, versão, papel, licença/condição de acesso e regenerabilidade. Reconferir anexos zipados e manifests sem editar a MC oficial.
3. **Criar partições com permissões distintas:** (a) `development/`: junho–outubro/2026 para auditoria e desenvolvimento; (b) `legacy_revalidation/`: novembro/2025–janeiro/2026, utilizável apenas para hipóteses antigas previamente congeladas, com regra e métrica registradas antes de qualquer replay; (c) `reserved_new_candidates/`: fevereiro–maio/2026 sob custódia integral, sem replay econômico, resultados, sinais/labels derivados ou disponibilização até autorização explícita de liberação da reserva; (d) eventual `chronological_after/`: período posterior a 06/11/2026 20:13 BRT, em trilha cronológica separada, com início/fim e dados definidos em escopo futuro. Não incluir derivados da partição reservada nos outros volumes.
4. **Fixar cortes e unidade temporal:** guardar limites em ISO-8601 com BRT e UTC, epoch-ms exclusivo, alinhamento em UTC e regra explícita de fechamento. Janela técnica coberta: 01/11/2025 00:00 BRT inclusive → 30/10/2026 00:23 BRT exclusivo, com as partições de acesso acima. O limite 06/11/2026 20:13 BRT separa um período cronológico posterior e não justifica incorporar candles entre 30/10 e 06/11 como extensão da janela anual.
5. **Definir warmup por cálculo:** proposta técnica inicial: 14 dias anteriores ao início (até 18/10/2025 00:00 BRT), cobrindo 300 candles 1h configurados no `regime_study` (300 h = 12,5 dias), TREND_72H (72 h), ONDA 12 h, MAROLA_90 (90 min) e margem. Identificar esse segmento como dados técnicos de warmup, fora de qualquer métrica econômica. Reavaliar se o inventário de indicadores revelar cálculo com lookback maior.
6. **Preservar duas modalidades separadas:** (a) replay anual contínuo com carteira iniciada em 01/11/2025, apenas como coorte experimental própria; (b) reprodução de paridade original, reiniciada em 01/06/2026 00:00 BRT com o estado inicial e warmup exatos do pacote de referência. Não transportar saldo, posições, CB ou estado da modalidade (a) para (b), nem esperar que as trajetórias econômicas coincidam.
7. **Selecionar fonte com snapshot:** para replays Claude de 01–08/11, adotar `marola_20261030` como pacote de mercado autoritativo (corte exclusivo 30/10/2026 00:23 BRT; epoch-ms `1793330580000`). Usar `replay_12bracos_20261021/frozen/` somente como origem do código congelado nas versões indicadas pelo README do pacote de sessão, nunca como mercado alternativo. Preservar pacotes originais sem alteração, incluindo scripts, comandos, SHA256SUMS e runs. Para a base anual estendida, obter os arquivos Spot SOLUSDT 1m mensais oficiais, guardar ZIP/checksum/URL/data de obtenção; comparar amostra de bordas e candles com API pública REST e prefixo local congelado. Divergências ficam em relatório de fonte, não se resolvem por substituição silenciosa.
8. **Validar e agregar candles:** normalizar timestamp para UTC epoch-ms; validar ordenação, duplicatas, OHLC, close-time, intervalos, volume base/quote, trades e alinhamento UTC. Gerar 5m/15m/1h somente com todos os candles-filhos esperados e contíguos. Não preencher buracos com preço inventado. Marcar os intervalos ausentes; avaliar o período como indisponível até haver histórico causal suficiente, salvo se Ernesto aprovar outra política antes do processamento.
9. **Preservar e verificar o pacote da sessão Claude:** fonte [MC-PROP-20261108-CLAUDE-PACOTE-SESSAO-01](https://drive.google.com/drive/folders/1QZhrYUk-aKhjgr3VvWe5dGSsjjGne_7d), ZIP original `pacote_sessao_claude_20261108.zip` (1.808.539 bytes), `README.md` e `SHA256SUMS.txt`. O ZIP contém scripts, comandos, rótulos e runs de referência seletivos; os ~400 MB de runs exploratórios não foram incluídos. Manter ZIP e membros originais byte a byte. O manifesto lista hashes por membro; o SHA-256 externo do contêiner ZIP não consta no README/SHA256SUMS e deve ser calculado ao materializá-lo para staging, sem alterar o arquivo de origem.
10. **Reproduzir independentemente os trabalhos de 01–08/11:** preparar o layout de sessão descrito no README; conferir cada membro contra `SHA256SUMS.txt`; usar `marola_20261030`, as versões congeladas indicadas, comandos/scripts originais e monkeypatches declarados. Primeiro rodar os comandos de referência sem editar fontes. Registrar resultado do Codex separado dos resultados/paridades relatados por Claude. Não confundir reexecução parcial LOW_FIRST na sessão Claude com validação independente de ambos os paths.
11. **Executar teste pequeno de capacidade:** após autorização da Entrega 1, preparar um mês permitido e um braço/path serialmente, medir tempo, pico RSS e tamanho de saída. O piloto inicial deve usar apenas junho–outubro ou warmup anterior necessário, sem material derivado de fevereiro–maio. Não usar a VPS operacional para campanha anual na configuração atual.
12. **Separar saídas científicas:** manter em diretórios, manifests e relatórios distintos: (a) auditoria de fontes/MC/código; (b) testes de paridade/reprodução; (c) revalidação de hipóteses antigas congeladas em novembro–janeiro; (d) resultados estratégicos de candidatas com reserva de fevereiro–maio, somente após liberação; (e) futura avaliação cronológica posterior a 06/11 20:13 BRT. Nenhum resultado de um desses objetos deve ser rotulado como resultado de outro.
13. **Gerar manifesto/package e fazer QA:** listar caminho/tamanho/hash por arquivo, versões, fonte/tempo de captura, comandos, limitações, warmup, partições e referências. Testar extração numa pasta limpa e checksum integral. Criar cópias equivalentes para os auditores e verificar SHA total; usar sessões novas, sem compartilhar respostas antes de ambos concluírem. Não incluir `reserved_new_candidates/` ou derivados antes da liberação.

## D. Plano de paridade

### Procedimento

1. Usar `marola_20261030` como mercado autoritativo para toda referência Claude de 01–08/11; `replay12/frozen` é apenas código congelado. Conferir fonte e SHA do mercado antes de rodar.
2. Preservar exatamente `switch_run.py`, `ladder_run2.py`, comandos, parâmetros, mapa de casas, rótulos e runs originais, incluindo monkeypatches em memória. Não editar fontes nem regenerar runs antes de salvá-los e hasheá-los.
3. Para o trecho comum 01/06/2026 00:00 → 30/10/2026 00:23 BRT exclusivo, iniciar carteira em junho conforme objeto original. Não incluir continuidade de posição/capital do replay anual iniciado em novembro.
4. Reutilizar o prefixo original byte a byte onde o registro define o prefixo como canônico; comparar arquivo e hash. Para divergências de fonte 1m, classificar intervalo/origem. Sem diferença reconciliada, não declarar paridade.
5. Para cada braço/path, comparar trade a trade usando chave causal declarada (identidade de sinal/source e sequência da carteira), não apenas timestamp ou contagem total. Incluir posições abertas no cutoff, sinais bloqueados e eventos de CB.
6. Comparar também os intermediários antes da economia: candles, indicadores/contextos, signals, ONDA/MAROLA/TREND labels e janelas. Isso localiza a primeira divergência, em vez de mascará-la pelo resultado agregado.
7. Guardar a reprodução independente do Codex e as declarações da sessão Claude em relatórios separados, ligados aos mesmos hashes. Números no README não são validação independente até os comandos serem executados e os diffs registrados.

### Aceitação

- SHA-256 e tamanho iguais para arquivos congelados de referência e prefixos que devam ser idênticos.
- Zero candle duplicado; cada gap aparece no relatório; nenhuma barra agregada usa filho ausente ou fecha antes do último filho disponível.
- Sinais/contextos/labels idênticos no domínio comum quando se usam os mesmos inputs, código, warmup, versão e disponibilidade. Campos de apresentação podem diferir apenas com whitelist justificada e predeclarada.
- Igualdade de admission, trades, carteira, CB, eventos, posição aberta/MTM, custos, paths e métricas segundo os checks da referência. Divergência econômica, de decisão ou de estado é falha; divergência documental é classificada e justificada, nunca omitida.
- Referência com corte menor não prova paridade em data posterior. Resultado salvo declarado pelo autor não é “paridade reproduzida” até reexecução independente pelo harness congelado.
- O replay junho-reset e o replay anual contínuo têm manifests, estados e relatórios separados.
- Runs e artefatos de 30/10 a 06/11 incluídos ou citados no pacote de sessão Claude são material histórico já explorado; não são reserva independente nem parte da reserva fevereiro–maio.
- O braço `EMA_MACD_CLUSTER_ACT10_K1_SHADOW` e seus ledgers a partir de 08/11 00:31 BRT são observação forward separada. Runs K1 históricos são teste de paridade, não trades forward.

### Incompatibilidades já detectadas

- `replay_12bracos_20261021/market_manifest.json`: avaliação inicia 01/06 00:00 BRT, mas encerra 21/10 23:56 BRT (epoch final exclusivo 22/10 02:56 UTC). Seu warmup local começa em 29/05 00:00 UTC, aproximadamente 75 h antes do início da avaliação.
- `marola_20261030/market_manifest.json` e fichas MC-EXP-0008/0010: encerra em 30/10 00:23 BRT exclusivo; guarda candles de aquecimento desde 29/05 e aponta prefixo preservado byte a byte.
- Os runs comparativos salvos de outros pacotes podem usar cortes intermediários; a ficha MC-EXP-0008 relata 46/46 checks, porém a incorporação é documental e não os reexecutou. Portanto, a aceitação de 30/10 precisa se apoiar nos arquivos completos do MAROLA/MC-EXP-0010, braço a braço, ou ser declarada não comprovável onde o run original não exista.
- A janela temporal “fim em 30/10 00:23 BRT” é compatível com epoch-ms `1793330580000`; usar exatamente esse limite exclusivo. Não confundir com 30/10 00:23 UTC.

## E. Recursos e custo operacional

### Dados e armazenamento

Contagens aproximadas para 365 dias: 525.600 candles 1m. Com 14 dias de warmup, ~545.760 candles 1m. Agregações UTC completas: ~109.152 candles 5m, ~36.384 de 15m e ~9.096 de 1h (valores aproximados; finais parciais excluídos).

Base observada localmente: o JSONL 1m de replay12 tem 32,7 MB para ~210 mil candles; o pacote MAROLA, com ~222 mil candles incluindo warmup, tem ~34,5 MB. Extrapolando a ~145–155 bytes por candle no formato local, espera-se cerca de **75–85 MiB para 1m** com warmup anual; **95–110 MiB** somando as quatro granularidades. Contextos/sinais/labels/regimes e manifests podem acrescentar aproximadamente **50–100 MiB**. São extrapolações lineares do layout observado, não arquivos medidos para um ano.

| Item | Estimativa não medida |
|---|---:|
| Download bruto 1m mensal e checksums | 20–80 MB compactados, sujeito a arquivos/CSV e checksum reais |
| Candles 1m + 5m/15m/1h normalizados | 95–110 MiB |
| Sinais/contextos/rótulos/manifests auxiliares | 50–100 MiB |
| Runs por 4 braços × 2 paths, anual, trade/equity detalhado | ~0,3–0,8 GiB sem compactar |
| Pacote auditável curado, com código e evidências selecionadas | ~0,5–1,5 GiB descompactado; ~0,2–0,8 GiB ZIP provável |
| Pacote amplo para todos os 12 braços × 3 custos × 2 paths | ~1–3 GiB descompactado, estimativa de escala dos artefatos locais |

O espaço livre observado de 28,85 GB é suficiente para esses intervalos, preservando cópia temporária e final. O projeto já ocupa ~13,9 GB em `data/`; deve-se reservar pelo menos duas vezes o tamanho final para staging, ZIP e extração de QA.

### RAM, CPU, tempo e infraestrutura

- **RAM:** os scripts atuais carregam a série 1m, contextos, sinais e resultados em objetos Python; o motor materializa uma curva por minuto e o orquestrador MAROLA pode iniciar quatro processos. Não foi medido pico RSS de replay anual. Para campanha integral, planejar ≥8 GiB de RAM efetivamente disponíveis e executar serialmente no piloto; preferir 16 GiB se forem necessários múltiplos braços/paths em paralelo. A máquina atual tem 7,7 GiB no total e apenas 0,8 GiB livre nesta consulta; fechar aplicações/reduzir concorrência e medir RSS é pré-requisito prático.
- **CPU:** 4 processadores lógicos localmente. Processamento é essencialmente CPU-bound; execução em blocos de input é compatível com semântica se janelas móveis mantiverem todo o warmup e cada carteira/CB permanecer contínuo. Não dividir ou reinicializar estado econômico no limite de bloco.
- **Tempo:** 12 meses têm ~2,4× candles de um pacote de ~5 meses. A geração linear de dados deve ser curta. Para oito runs alvo (quatro braços, dois paths), estimativa grosseira é de dezenas de minutos a algumas horas serialmente; para 72 runs de 12 braços/três custos/dois paths, algumas horas até um dia, conforme objetos e escrita JSON. Não há benchmark cronometrado comparável; medir um mês e extrapolar antes da campanha.
- **VPS operacional:** não usar para replay anual nesta etapa. O histórico operacional descreve VPS de cerca de 1 GiB; não há margem demonstrada para engine que retém séries e curvas por minuto, e a regra da tarefa proíbe alterações no bot/VPS. Não é necessário comprar infraestrutura para gerar candles. Se o piloto provar que RAM/CPU locais falham, retornar com medição antes de propor máquina maior.
- **Custo externo:** downloads públicos não requerem serviço pago. Não há custo de nuvem/infra necessário conhecido. Custo de chamadas dos dois modelos auditores depende de tokens/planos e fica fora do custo técnico da Entrega 0; navegar por índice e evidências seletivas reduz ingestão indiscriminada.

## F. Riscos e decisões pendentes

1. **Lacunas:** recomendação inicial para decisão humana: preservar timestamps sem fabricar candles; agregar apenas intervalos íntegros; marcar gaps e bloquear cálculo/classificação que exija janela contaminada. Verificar se o runtime, depois de um gap, exige reinicializar lookback causal antes de reabilitar signals. Se o comportamento histórico só “pula” o minuto ausente, declarar essa diferença; não mudar engine para forçar paridade.
2. **Warmup:** 14 dias é proposta ampla que cobre 300 candles 1h com folga, mas não reproduz o warmup de ~75 h do June-start original. Para teste de paridade, usar o warmup e inicialização originais; para série anual, usar warmup suficiente independente e comparar os dois cálculos em junho como diagnóstico. O warmup anual não entra em trades/métricas.
3. **Segregação histórica decidida para planejamento:** junho–outubro/2026 é auditoria/desenvolvimento; novembro/2025–janeiro/2026 é apenas revalidação limitada de hipóteses antigas congeladas; fevereiro–maio/2026 fica integralmente reservada à validação futura de novas candidatas. Até autorização expressa de liberação, não rodar replay econômico nessa reserva nem produzir/disponibilizar resultados ou dados derivados dela. Como fevereiro–maio precede junho–outubro, a reserva é histórica retida, não forward OOS.
4. **Data posterior:** dados após 06/11/2026 20:13 BRT ficam em avaliação cronológica separada. Conteúdo entre 30/10 e 06/11 que conste nos materiais Claude é evidência histórica já explorada, não reserva independente. Não incluir esses dados como se fossem a extensão da janela de mercado autoritativa de 30/10.
5. **Fonte retrospectiva:** os candles de arquivo e REST de hoje não provam quais valores/candles foram observados pelo bot no período. Registrar fontes atuais, retrieval timestamp, checksums, paridade de prefixo e discrepâncias; separar verdade histórica do mercado da disponibilidade/feed operacional.
6. **Identidade dos braços:** resolver nome/versão/capital/fees/CB/custos por caminho em documento de mapping e declarar qual artefato é base autoritativa. Proibir usar YAML atual para preencher parâmetros passados.
7. **Custo computacional:** o limite maior é RAM e aumento de escrita JSON/tempo de CPU. Evitar 4-way run até benchmark e monitoramento; não executar na VPS.
8. **Independência:** os dois auditores recebem arquivos, prompts, manifests e limites iguais em sessões novas. Nenhum verá a análise do outro ou comentários de síntese antes de concluir. Ambos devem declarar conflitos de participação, material ausente, possíveis vieses e validações que realmente executaram.
9. **Decisão humana:** pré-registro e classificação de resultados devem anteceder qualquer leitura de holdout. Não alterar estado científico ou painel da MC sem aprovação humana explícita nos termos do protocolo v0.3.2.

## G. Critérios de aceite da Entrega 1

- [ ] Todos os quatro braços têm regra, versão, config e estado inicial ligados a arquivo original + hash e caminho reproduzível.
- [ ] Anexos MC relevantes foram baixados/lidos, hashes confirmados e fontes externas indisponíveis ou sessão-dependentes estão listadas como bloqueio ou ressalva.
- [ ] Dataset 1m tem timestamp UTC normalizado, ordenação, unicidade, cobertura, extremos, volume e mapa de gaps verificados; fonte/checksum/data de captura constam no manifesto.
- [ ] Agregações 5m/15m/1h têm alinhamento UTC, regra de candles-filhos completos, métricas/hashes e comparação independente contra fonte nativa em pontos e período de sobreposição.
- [ ] Warmup está separado da janela econômica e dimensionado por todos os cálculos usados; 14 dias ou outro limite foi aprovado/documentado. Nenhum sinal usa dados posteriores ao instante de decisão.
- [ ] Junho–outubro/2026, novembro/2025–janeiro/2026 e fevereiro–maio/2026 têm separação de acesso e manifestos próprios. N–Jan só é usado com hipótese/regra/métrica congelada; Fev–Mai não tem replay econômico nem derivados até autorização expressa.
- [ ] Replay junho-reset mantém seu próprio estado inicial e roda até o corte exclusivo 30/10 00:23 BRT com mesma fonte/config/custos/código/paths da referência.
- [ ] Paridade trade a trade e de trajetória fechou para cada braço e ambos paths, ou exceções estão declaradas e impedem chamar o braço de reproduzido. Logs/trades/eventos e diferenças estão anexados.
- [ ] Replay anual contínuo está em objeto, ledger, cutoff, warmup e métricas distintos, sem mesclar com o teste junho-reset.
- [ ] O pacote da sessão Claude foi preservado sem alteração; hashes dos membros passam; scripts e comandos originais foram reproduzidos independentemente pelo Codex. O relatório Codex de reprodução está separado do relatório/alegações Claude.
- [ ] `marola_20261030` é fonte de mercado dos experimentos Claude de 01–08/11, e o código congelado usado é a versão precisa de replay12 referenciada no README. O corte de mercado é exclusivo 30/10/2026 00:23 BRT.
- [ ] Junho–outubro está classificado como auditoria/desenvolvimento; novembro–janeiro é acessado somente com hipótese/regra/métrica congelada antes do replay; fevereiro–maio não tem replay econômico, output nem derivados liberados antes da autorização humana.
- [ ] Artefatos de 30/10–06/11 estão marcados como já explorados. Dados posteriores a 06/11 20:13 BRT ficam separados em objeto cronológico próprio.
- [ ] `EMA_MACD_CLUSTER_ACT10_K1_SHADOW` está inventariado como coorte forward iniciada em 08/11/2026 00:31 BRT e separado dos quatro comparadores históricos e dos runs históricos de paridade K1.
- [ ] Limitações de OHLC, intrabar, custos, fill, CB, exposição, reutilização de janela, seleção pós-hoc e diferenças replay/shadow/Testnet constam no pacote.
- [ ] Ambiente de execução é reconstruível (Python e dependências transitivas fixadas), e o QA registra pico de RSS e tempo do piloto.
- [ ] Manifesto inclui caminho/tamanho/SHA-256/fonte/versão/geração, instruções e política de segredos; busca confirma ausência de credenciais/tokens/chaves.
- [ ] Duas cópias do pacote e do prompt são equivalentes por SHA-256, legíveis em extração limpa e entregues a sessões independentes. O material reservado não é liberado antes da hora.
- [ ] Entrega revisada por Ernesto, Claude e Codex; nenhuma conclusão científica ou estado da MC alterado durante sua preparação.

## Fontes e evidências consultadas

**Repositório:** HEAD e arquivos locais em `C:\dev\trend-sol`; manifests em `data/analysis/replay_12bracos_20261021/` e `data/analysis/marola_20261030/`; configurações e scripts listados acima. Os resultados existentes foram lidos como artefatos, não recalculados.

**Memória Científica:** índice oficial v19, protocolo v0.3.2, MC-LIN-0001–0005, MC-EXP-0008, MC-EXP-0010, MC-EXP-0015 e estruturas de versões/anexos no Drive. Registros continuam sendo fontes autoritativas; esta proposta não os altera.

**Pacote de reprodução da sessão Claude:** [pasta no Drive](https://drive.google.com/drive/folders/1QZhrYUk-aKhjgr3VvWe5dGSsjjGne_7d), [README](https://drive.google.com/file/d/1K3ijE2S0a5Jt6ihcFTrVPu1825YkK80c/view), [ZIP original](https://drive.google.com/file/d/1cc9d1uGEORpPWLx1fNkphJsJeTf8erTh/view) e [SHA256SUMS dos membros](https://drive.google.com/file/d/1_BXoZbYaH48eTsSw7TO0GnvwD_e64_M4/view). Nesta atualização foram inspecionados README e hashes; o ZIP não foi extraído ou executado. Nenhum replay econômico foi feito.

**Dados públicos:** Binance Data Vision documenta arquivos Spot em pacotes mensais/diários e a estrutura dos klines; registra timestamps em microssegundos a partir de 01/01/2025. Como todo o período proposto é posterior a essa data, a conversão para epoch-ms precisará ser testada explicitamente. O formato inclui OHLC, volume base, close time, quote volume, número de trades e volumes taker. Fonte: [repositório oficial Binance Public Data](https://github.com/binance/binance-public-data). API REST pública de referência: `https://api.binance.com/api/v3/klines`.

## Conclusão desta Entrega 0

Há material e capacidade de armazenamento suficientes para prosseguir com planejamento detalhado da Entrega 1. A prontidão científica ainda é **parcial**: faltam (1) mapa completo de fontes/código/configuração por braço; (2) reprodução independente dos scripts/runs preservados; e (3) materializar e validar as partições sem abrir a reserva de fevereiro–maio. As decisões complementares de Ernesto estão incorporadas nesta proposta. A Entrega 1 permanece não iniciada: download integral, replay econômico, produção/liberação de derivados reservados e campanha anual dependem de autorização posterior.
