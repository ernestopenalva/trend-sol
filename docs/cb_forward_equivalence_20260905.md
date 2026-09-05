# REAL_A_CB_SHADOW — fechamento da correção de engenharia

## Status

Implementação e validação local concluídas. Suíte completa: **248 testes, todos aprovados**. `git diff --check` sem erro de whitespace. Não houve deploy, reinício de bot ou criação de nova coorte nesta sessão. Não há sessão SSH/VPS disponível ao agente; o marco exato da nova coorte depende da inicialização efetiva na VPS.

O primeiro ensaio completo encontrou apenas ausência de `tzdata` no Python Windows. A repetição utilizou a base IANA já instalada no runtime local, por `PYTHONTZPATH`, sem modificar dependências ou configuração do bot. Houve um aviso de depreciação preexistente em `ge_entry_audit.py`, não uma falha.

## Alterações de engenharia

### Sinal compartilhado, admissão independente

`src/app.py` entrega o mesmo `EntrySignal` aprovado pelo motor de entrada ao CB antes de chamar `registry.open_pair`. Uma recusa de capacidade/spacing ou rejeição da ordem real não impede a oportunidade do CB. Não foi criado outro cálculo de gates; `on_kline` do CB não avalia entradas.

Cada braço conserva suas posições, cinco slots e spacing. CB mantém no máximo uma entrada por bucket 5m do candle fonte; fechamento não libera novamente o bucket. Ordem dos bloqueios do CB: breaker, capacidade, admissão, spacing. Sinais repetidos não geram novas entradas.

Pausas globais de segurança que impedem a própria geração do sinal continuam como estavam. Não se inventam oportunidades quando o motor compartilhado não as produziu. Regras, gates e ordem de execução das operações do REAL_A não foram modificados; foram acrescentados apenas os hooks de observação do CB.

Após a tentativa real, o resultado `opened`, `blocked` ou `order_rejected` é registrado apenas como observação. Não alimenta detector, sizing ou admissão CB. H2 e demais shadows não foram alterados.

### Detector com semântica do replay

`src/monitor/cb_replay_clock.py` implementa o relógio discreto, comparado diretamente com `CircuitGuard` do replay:

- avaliação por minuto, inclusive minutos sem novo fechamento;
- equity e pico realizados próprios;
- rolling `(t - 4h, t]`;
- DD >= 1,5% do capital inicial, rolling PnL <= -0,5% e pelo menos dois fechamentos;
- disparo na transição false→true, não simplesmente quando não está pausado;
- deadline de 6h e extensão pela mesma semântica do replay;
- release no limite do deadline;
- nenhuma feature de mercado no detector.

Os ticks continuam reais. Um fechamento ocorrido no minuto `[m, m+1)` é incorporado ao detector na fronteira `m+1`, antes dos sinais desse minuto. O ledger conserva o horário do tick da saída. Por isso, entre a saída e a próxima fronteira há um fechamento pendente: equity do detector + net pendente = equity do ledger. O relatório informa essa pendência em vez de tratá-la como inconsistência.

Eventos são observados quando chega um tick/sinal. Na recuperação de minutos sem callbacks, o relógio percorre cada minuto e registra o timestamp lógico do trigger/release. Isso não é garantia de callback de parede no segundo zero. Preços históricos indisponíveis dessas fronteiras não são preenchidos com o preço futuro recebido.

### Ladder e persistência

A engine de saída permanece compartilhada. O adaptador CB preserva seu identificador, utiliza o piso econômico de PL do REAL_A e registra a saída sintética pelo timestamp de mercado. Não altera multiplicadores, fees, sizing, stop ou degraus.

Cada entrada de dados tem uma transação:

1. arquivo `.pending` registra o input com sequência e `fsync`;
2. processamento em memória;
3. checkpoint atômico com posições, peak, BE, PL, trailing, stops, entradas, equity, histórico, breaker e eventos;
4. ledger e eventos materializados idempotentemente a partir do checkpoint confirmado.

Falha antes do checkpoint: reaplicar o input pendente uma única vez. Falha após checkpoint: reparar somente a materialização. Escrita usa arquivo temporário, `fsync`, rename atômico e, em POSIX, `fsync` do diretório. Um erro de armazenamento não permite continuar processando com memória parcialmente confirmada.

Na restauração, as posições e o ledger são reconciliados: IDs únicos, nenhuma posição fechada restaurada aberta, pendências pertencentes aos registros, equity/pico/rolling iguais aos fechamentos contabilizados. Corrupção ou divergência não é convertida silenciosamente em saldo inicial. Checkpoints antigos são recusados: a coorte anterior deve ser arquivada, não misturada com este formato.

**Limite importante:** recuperação transacional cobre inputs registrados. Não recupera automaticamente ticks e sinais que nunca chegaram enquanto o processo estava desligado. Equivalência contínuo/restart é demonstrada sobre o mesmo fluxo de inputs; não é uma promessa de reproduzir mercado ausente. Entrada temporalmente atrasada que cruzaria minuto já avaliado é recusada e exige reconciliação, não reescrita silenciosa do passado.

### Relatório

`tools/circuit_breaker_shadow_report.py` agora separa:

- sobreposição de fechados versus sobreposição de entradas incluindo posições abertas;
- REAL_A-only explicado por bloqueio CB;
- CB-only com recusa real de admissão ou rejeição de execução observada;
- divergência sem explicação, marcada para auditoria de integração;
- entradas após release, limitadas até a próxima crise, sem contagem repetida;
- associação temporal pós-release versus substituição causal (não inferida automaticamente);
- input ainda em commit e fechamentos pendentes da próxima avaliação.

Não há conclusão FAVORÁVEL quando existe problema de integridade. Um aviso momentâneo de commit deve ser relido; persistência do aviso exige investigação. A ausência de uma ordem no ledger REAL_A ainda não prova falha do CB.

## Verificação executada

- 2.500 minutos sintéticos comparando exatamente detector original e novo: equity, pico, condição, crises, trigger, deadline, release, paused state, sinais bloqueados e minutos pausados; restaurações frequentes durante o fluxo.
- Integração sinal → cinco admissões → ticks → múltiplos HS → crise → bloqueio → 6h → release → nova entrada → BE/PL/trailing → fechamento → nova admissão.
- Execução contínua versus restart em etapas críticas: igualdade de entradas, saídas, PnL, posições abertas, eventos, detector e bloqueios.
- Falha antes do checkpoint em abertura, proteção e fechamento, recuperando input pendente sem duplicação.
- Falha após checkpoint e antes do ledger, com restaurações repetidas.
- Corrupção de equity recusada; input atrasado recusado; observação da admissão real incapaz de alterar o detector.
- Testes de wiring: oportunidade CB recebida mesmo quando REAL_A não abriu; ticks encaminhados uma vez; nenhum motor alternativo de entrada CB.
- Suíte completa de regressão: **248 testes / OK**.
- Smoke de persistência em disco temporário Windows: 1.000 ticks, cinco posições iniciais; média **6,866 ms**, p95 **9,122 ms**, máximo **15,100 ms**, checkpoint final 16.720 bytes. É medição local com coorte pequena, não garantia de throughput de longo prazo na VPS. A persistência é síncrona; crescimento do checkpoint e latência de disco precisam ser observados na operação.

## Arquivos desta correção

- `src/app.py`
- `src/monitor/circuit_breaker_shadow.py`
- `src/monitor/cb_replay_clock.py` (novo)
- `tools/circuit_breaker_shadow_report.py`
- `tests/test_circuit_breaker_shadow.py`
- `tests/test_circuit_breaker_wiring.py`
- `tests/test_cb_forward_equivalence.py` (novo)
- este relatório.

Ferramentas `*_hypothesis_audit*` e `*_admission_equivalence*` existentes no diff não são alterações no forward: pertencem às auditorias anteriores. Não houve mudança de YAML, detector do replay, ladder compartilhada, H2, states ou ledgers de produção.

## Atualização na VPS — ainda não executada

1. Parar o processo atual pelo mesmo mecanismo usado para iniciá-lo. Não executar dois `main.py` simultaneamente.
2. Transferir também os **arquivos novos**, não apenas os modificados.
3. Com o bot parado, executar:

```bash
python -m unittest discover -s tests
```

4. Somente com testes aprovados, arquivar exclusivamente a coorte CB anterior. O diretório novo evita sobrescrever arquivos já arquivados. Não mover os states/ledgers de REAL_A, H2 ou outros shadows:

```bash
mkdir -p data/archive
CB_ARCHIVE=$(mktemp -d data/archive/cb_before_equivalence_XXXXXXXX)
for f in \
  data/state/circuit_breaker_shadow.json \
  data/state/circuit_breaker_shadow.json.pending \
  data/trades/trades_circuit_breaker_shadow.jsonl \
  data/telemetry/circuit_breaker_shadow_events.jsonl
do
  if [ -e "$f" ]; then mv -- "$f" "$CB_ARCHIVE/" || exit 1; fi
done
printf 'Coorte CB preservada em: %s\n' "$CB_ARCHIVE"
```

Isso arquiva também eventuais posições sintéticas da coorte antiga; não vende nem modifica posições reais. A operação é recuperável pelos arquivos preservados. Não executar o arquivamento com o bot rodando.

5. Iniciar pelo mecanismo habitual; para execução manual já utilizada:

```bash
TZ=America/Sao_Paulo date -Iseconds
python main.py
```

6. Em outro terminal, obter o início efetivamente persistido da coorte, sem inventar um horário:

```bash
python -c 'import json; from pathlib import Path; from datetime import datetime; from zoneinfo import ZoneInfo; s=json.loads(Path("data/state/circuit_breaker_shadow.json").read_text()); t=s.get("cohort_started_at"); print(datetime.fromisoformat(t).astimezone(ZoneInfo("America/Sao_Paulo")).isoformat() if t else "aguardando primeiro tick")'
```

Usar o ISO retornado como `--since` no relatório. O arquivo pode ainda não existir antes do primeiro input. Esse é o timestamp a informar como nova coorte; nenhum timestamp foi atribuído neste relatório.

**Não chamar o novo forward de validado economicamente no startup.** Testes de engenharia aprovados permitem iniciar a observação; primeiros eventos devem confirmar integridade, e a hipótese econômica depende das crises futuras.
