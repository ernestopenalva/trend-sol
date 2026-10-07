# Auditoria diagnóstica da matriz EMA + MACD

## Comando reutilizável

```bash
python tools/forward_experiment_report.py --experiment ema_macd --matrix-audit --since "27/09/2026 20:07:29"
python tools/forward_experiment_report.py --experiment ema_macd --matrix-audit --pair "SHO+BE+" --since "27/09/2026 20:07:29"
```

`--top-n 10` controla somente a quantidade de winners positivos listados.
O critério de “important winners” é top N por net positivo, não um threshold
inventado de relevância econômica. Não muda cálculo, classificação ou matriz.
As flags novas só são válidas para `--experiment ema_macd --matrix-audit`.
Sem a flag, a apresentação existente permanece intacta.

O helper `tools/forward_matrix_audit.py` não tem CLI próprio nem cria bot,
cliente de ordens, replay ou estado. Chama a política real de `PolicyShadow`
para obter o status atual das 28 combinações, sem duplicar a matriz.

## Contabilidade

- Oportunidades são SIGNAL_OPPORTUNITY únicos por source_candle.
- Matrix eligible é o resultado da política atual no par persistido.
- Matrix pass observed requer admissão ou bloqueio posterior ao filtro;
  bloqueio por CB antes da matriz não é contado como avaliação aprovada.
- OPEN/ledger/estado determinam trades realmente abertos. Um OPEN sem registro
  final/estado é UNRESOLVED, não um trade fechado imputado.
- Pairs aceitos usam resultados do EMA_MACD. Bloqueados usam somente sources
  com ENTRY_BLOCKED_EMA_MACD efetivo e abertura real do controle.
- Common/control-only/experiment-only usam somente source_candle, incluindo
  abertos. Não há matching por preço/proximidade temporal.
- Duplicatas de source são deduplicadas; conflitos de trade/contexto e source
  simultaneamente aberto/fechado exigem reconciliação, não escolha silenciosa.
- Net reutiliza o cálculo de net percentual vezes notional do report.
  PL/TRAIL não necessariamente significam net positivo.
- HS/PL/TRAIL% têm N resolved/evaluable como denominador.
- DD do subconjunto é equity realizada iniciando em zero e ordenada por exit;
  empates são ordenados por source. Não é DD do portfólio nem MTM.
- Top 3 winner share usa o gross positive net, nunca o net total, que pode ser
  negativo/próximo de zero. Remover winners não é uma regra operacional.
- `--since` filtra admissão; posições anteriores não entram. A decomposição
  bruta reconcilia com a janela equivalente do report normal.

## Validade e N

Janela principal histórica: 27/09/2026 20:07:29 BRT. O warm-up anterior ao
marco comparável já auditado de 28/09/2026 11:52:14 aparece como observação,
mas não sustenta classificação comparativa. Cada par mostra N observado e
N suporte qualificado, além do intervalo de entradas e abertos/unresolved.

O freeze conhecido dos braços com CB de 29/09/2026 13:25:12 até o restart
de 30/09/2026 22:03:09 BRT invalida a trajetória de trades que o atravessam.
Esses registros continuam na reconciliação bruta, mas não nos resultados
evaluable usados para classificação. Não há reconstrução de trades perdidos
nem correção de estados/ledgers. A ferramenta conhece este incidente específico;
não certifica automaticamente a ausência de outros incidentes futuros.

Exit context é lido pelo validador temporal existente do report. STALE não
invalida automaticamente entry pair/net, mas nunca serve para inferir mudança
técnica na saída. Contextos indisponíveis não são inferidos de candles.

**N suporte < 10: INSUFFICIENT_SAMPLE e LOW_N / EXPLORATORY**, sem suporte para
mudança da matriz. O valor 10 é guardrail descritivo, não teste estatístico,
limiar científico de aprovação nem autorização de implantação.

## Classificação transparente

A classificação é uma triagem retrospectiva e descritiva, não otimização:

- Com N baixo, nenhum candidato de alteração é produzido.
- Net/PF positivos, saldo positivo sem os top 3 winners e pelo menos dois dias
  positivos permitem KEEP_ACCEPTED ou CANDIDATE_UNBLOCK exploratório.
- Net/PF e mediana negativos, saldo ainda negativo sem o pior loser e pelo
  menos dois dias negativos permitem CANDIDATE_BLOCK ou KEEP_BLOCKED descritivo.
- Resultado aceito misto/concentrado é REVIEW_ACCEPTED; bloqueado positivo sem
  robustez é candidato exploratório concentrado, nunca aprovação.
- Muitos pendentes ou divergência de contexto entre os braços impedem conclusão
  forte. HS% não determina classificação isoladamente.
- Dias são uma verificação descritiva de distribuição, não observações
  estatisticamente independentes/regimes certificados.

Nenhum candidato pode ir diretamente a produção. Precisa passar por replay
sistêmico histórico fora desta janela, períodos/regimes distintos e validação
forward independente. Alterar matriz muda slots, spacing, CB e futuras
admissões; não adicionar mecanicamente o net recusado ao saldo do experimento.

## Snapshot desta entrega

Dados originais: checkpoints commitados copiados da VPS, revisão `d1a062a`.
Capture de 06/10/2026: controle 22:23:43 BRT, EMA_MACD 22:23:57 BRT; último
evento de oportunidade 21:58 BRT. As cópias foram projetadas somente num root
isolado de análise, preservando os states locais/VPS reais. Hash/sequence e
timestamps estão em `data/analysis/ema_macd_matrix_audit_20261006/manifest.json`.

Artefatos: `resultado.md`, `matrix_audit.txt`, `matrix_audit.json` nessa pasta.
O TXT contém todas as seções, métricas, 28 pairs, seis HS aceitos, top winners
bloqueados e todas as 15 linhas do controle SHO+BE+.

Conclusão atual: REVIEW_ACCEPTED para BUL+BU+ (14 resolved, 0 unresolved) e
LON+BU+ (15 resolved, 2 unresolved); KEEP_BLOCKED exploratório para LON+BU-;
os outros 25 pares têm suporte insuficiente. SHO+BE+ tem 15 observados, mas
somente 9 pós-warm-up, portanto LOW_N / EXPLORATORY. Nenhum candidato sustentado
de BLOCK/UNBLOCK ou matriz v2 foi proposto.

Testes focados: **54 OK**, incluindo 13 novos testes de decomposição,
pareamento, contagem, concentração, STALE, since/pair, reconciliação,
duplicatas, conflitos, warm-up/freeze e guardrail de N. Saída normal testada.
Sem runtime, config, matriz, estado real, Git write, deploy ou restart.

Uma execução intermediária teve dois subcasos ACT20/ACT10 com WinError 5 em
atomic replace de checkpoints temporários no Windows. A repetição completa dos
54 testes passou; nenhum retry ou alteração de persistência foi introduzido.
