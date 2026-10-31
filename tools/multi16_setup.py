"""Prospective local research setup. Writes only its new study/proposal."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import json,hashlib,shutil,sys
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_redutores_multi_20261025';OLD=ROOT/'data/analysis/replay_combinacoes9_20261025';MC=Path('G:/Meu Drive/trend-sol/Memória_Científica');DEST=MC/'propostas/MC-PROP-20261025-MULTI16-HS-01'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 S.mkdir(exist_ok=True);DEST.mkdir(exist_ok=True)
 sources=['PROTOCOLO.md','README.md','linhas/MC-LIN-0001_FAST_DROP_SELETORES.md','linhas/MC-LIN-0002_GESTAO_POSICOES_E_SAIDAS.md','linhas/MC-LIN-0003_FILTROS_ENTRADA_E_ADMISSAO.md','hipoteses/MC-HIP-0002_MACD_BU_MINUS.md','hipoteses/MC-HIP-0003_EMA_MACD.md','hipoteses/MC-HIP-0004_HIST_1M.md','cronologia/MC-CRO-0001_FAST_DROP.md','cronologia/MC-CRO-0002_PATCHES_FEED_PERSISTENCIA.md']
 (S/'mc_fontes').mkdir(exist_ok=True)
 manifest={}
 for n in sources:
  p=MC/n;p.read_text(encoding='utf-8-sig');manifest[n]=sha(p);shutil.copyfile(p,S/'mc_fontes'/p.name)
 (S/'FONTES_MC.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
 shutil.copyfile(OLD/'config.json',S/'config.json')
 text='''---
id: "MC-PROP-20261025-MULTI16-HS-01-PREREG"
tipo: "APOIO_DOCUMENTAL"
versao: 1
status_documental: "PROPOSTO_PARA_REVISAO"
linha: "MC-LIN-0002"
vinculos: ["MC-LIN-0001", "MC-LIN-0003", "MC-HIP-0002", "MC-HIP-0003", "MC-HIP-0004"]
autor: "Codex"
resumo_executivo: null
resumo_proposto: "Protocolo prospectivo das dezesseis combinacoes de redutores e referencias recalculadas."
---
# Pre-registro V1 — filtros × combinacoes de redutores

Objeto REGRA_IDEALIZADA / REPLAY_SISTEMICO; CONTRAFACTUAL_POR_TRADE separado. MC0.3.2, indice, linhas, hipoteses e cronologia consultados. Vinculo documental ao estudo das9duplas em propostas/MC-PROP-20261025-COMB9-FILTROS-HS-01, nao homologado. Nao cria novas linhas/hipoteses oficiais. Somente offline; bot/YAML/parametros/fontes operacionais intocados. Nenhum shadow operacional, deploy, restart, commit ou push. Revisao REVISAO_NAO_INDEPENDENTE; aprovacao cientifica pendente.

Desenho:4bases BE_OFF_CB, MACD_BU_MINUS, EMA_MACD, HIST_1M; emcadauma A=FAST+CLUSTER+CB_EXIT_ALL, B=FAST+CLUSTER, C=FAST+CB_EXIT_ALL, D=CLUSTER+CB_EXIT_ALL.16novas +16referencias (BE,3filtros,3redutores,9duplas) =32carteiras ×2paths =64runsBASE. REAL/ACT excluidos. Sem condicionar por regime, sem otimizar.

Janela01/06/2026 00BRT ate ultimo1m fechado na captura publica de25/10 (max fim25/10BRT). END congelado uma vez antes da economia, por servidor Binance. Prefixo25/10 preservado; cauda1m publica;5m/15m/1h completas UTC. Warmup anterior, sinais e contextos causais rolling300 e sobreposicao12h exata. Sem preencher gaps; interromper se causalidade ou mecanica incorreta. Recalcular referencias com mesmos dados/END, testar prefixo economico exato contra25/10.

Mesmo motor congelado6a9ebff338857440abe12a30a71fd524f78ffc00 e adaptador21/10 utilizado25/10. BASE3,5bp de friccao por lado +taxa0,10%porlado; mesma metodologia em todas as saidas. Gap no preco observado com friccao, nunca fill perfeito no target. OHLC1m HIGH_FIRST=O-H-L-C e LOW_FIRST=O-L-H-C, instantes0/20/40/59.999s e cruzamentos globais de stops em descidas. Cenarios, nao limites garantidos. Sinal1m fechado, entrada na abertura seguinte; nunca usar extremos anteriores a entrada. FAST primeira avaliacao elegivel, referencia minuto/boundary e consumo one-shot originais. Ausencia de referencia nao consome. Disponibilidade idealizada pontual de1m/EMA5m, sem lookahead.

Precedencia AUTORIZADA EXPRESSAMENTE pelo humano nesta conversa: inicio do minuto CB e eventual liquidacao antes de admissoes. Cada evento de preco FAST respeitando stops normais ja armados, stops normais, CLUSTER, CB_EXIT_ALL se transicao elegivel. Convencao DO REPLAY: combinacao nao existe no runtime; eventual implementacao forward tera que seguir mesma ordem. Relogio CB original: fechamentos entram no proximo boundary1m; nao acelerar detector para fabricar cascata instantanea. Contextos fechados5m disponibilizados antes da avaliacao do minuto. Comparadores de uma unica peca preservam motor25/10.

Cascatas: ligar saidas CLUSTER ao boundary de contabilizacao e crise com liquidacaoCB de sobreviventes. Contar separadamente transicoes com CLUSTER em due, vitimas, mesmo/proximo evento e atraso em eventos/ms. Teste de necessidade local: clonar estado do clock ANTES desse boundary e remover SOMENTE fechamentosCLUSTER de due; se transicao nao ocorrer, cluster necessario nesse boundary. Se transicao persistir, contribuicao concomitante, nao causalidade necessaria demonstrada. Detector mantem historico anterior em ambos; nao e contrafactual de carteira nem remove cluster historico. Reportar crises sem sobreviventes e lags maiores separadamente. Normalmente proximo boundary (nao mesmo tick), conforme regra congelada. Observar conflitos sem alterar avaliacao/consumo: elegibilidade por snapshot/etapa, flags de disponibilidade, sem contar como intervencao efetiva. Nao chamar elegibilidade meramente potencial de fechamento.

Capital nominal100, notional20, max5, spacing1ATR,1entrada/bucket5m, CB proprio6h; escadinhaBE_OFF_CB/pisoPL/trailing/HS/ATR originais. Sem cashgate/compounding, nao e conta autofinanciada100. Abertos=MTM liquido com custo estimado, separado de realizado; nao forcar saida. Cada record tera mare/onda entrada, motivo e redutor executado, net e statusOPEN/CLOSED.

Metrica primaria delta_mean=net_var/N_var - net_base/N_base (USDT/admissao); N inclui abertos. Reportar delta_total, delta_total/Nvar, HS/PL/Trail porcentagem dos FECHADOS, DD1m e exposicaoUSDT*h. Comparador principal base sozinha; filtrossozinhos vsBE; redutoressozinhos vsBE. Contrastes de composicao: cada dupla multiredutor contra ambas pecas individuais da mesma base; trio contra todas3duplas (retiraruma), como efeito de CARTEIRA, nao atribuicao isolada.

Classificacao mesmo criterio25/10: candidata se delta_mean>=.005 emAMBOSpaths, delta_mean>0 nasDUASmetades e apos retirar3melhoresdias de delta_total emambos, atuacao efetivaN>0 e soma beneficio condicional>0 emambos. Igual/pior delta_mean<=0 emalgumpath=SEM_EVIDENCIA_DE_BENEFICIO. Demais INCONCLUSIVA, inclusive apenas uma metade positiva; nao descartar por isso. Tolerancia1e-10. Baseline=REFERENCIA_SEM_COMPARADOR. FAST pode classificar mas NAO_RECOMENDAVEL_FORWARD_POR_FIDELIDADE_NAO_DEMONSTRADA. Net absoluto negativo nao e veto novo e sera destacado. IC nao e veto novo.

Metades AGORA exatamente START+(END-START)//2, data/hora informada, diferente do corte fixo11/08 de25/10 por mandato novo. Coortes por entrada, resultado completo. Top3dias de maior contribuicao positiva ao delta_total vsbase porpath; retirar dessas DUAScarteiras, recalcular medias, sem rerodar trajetoria. Se menos3 retirar os existentes. Relatar tambem contribuicoes negativas e incerteza. Bootstrap dias e blocos72h5000seed20261025,>=10blocos, trajetorias condicionais, IC95; apenas para pecas nao redundantes no caso de aprofundamento.

Intervencoes: contagem de vitimas e eventos por redutor e total. Pareados REAIS fontecomum com base e decomposicao delta_total=comuns+exclusivas_var-exclusivas_base. Adicional fix-entry contra saidaBE sem redutores mantendo entrytime/price/qty/ATR/path/custo/END, distinguindo pares reais vs fontes exclusivas. Beneficio condicional porredutor, residual total-direto como trajetoria SEM interpretacao causal isolada. Gate original de atuacao favoravel usa total de intervencoes contra saida da base. Para filtros sozinhos, rejeicao explicita em fonte realmente admitidaBE:0-netBE. Nao fabricar trade nao observado. Nao somar como contribuicoes independentes contrastes retirarpeca nem residuos sobrepostos.

Parada AUTORIZADA: peca de BAIXA_ATUACAO se (Nfechadas<5 OU Nfechadas/Ntrades_var<.01) em CADA um dos2paths. Registrar como redundante no sentido operacional nesta amostra, nao equivalencia economica geral. Mostrar TODAS metricas/contrastes/pareados exigidos, mas sem novos testes/otimizacao/sondas extras dessa peca; nenhuma amostra parcial para variantescompletas. Falta de fechamento por precedencia nao prova ausencia de elegibilidade; mostrar concorrencia quando reconstruivel.

Matriz16celulas BULL/BEAR/EXPA/COMP × mesmas, entrada, N/net/nettrade/%PL/%HS/%Trail, ambaspaths, todas32rodadas. Outros estados separados, nunca descartados silenciosamente. APENASDESCRITIVA, sem classificar celulas ou selecionar politica.

Multiplicidade:9duplasanteriores+16novas=25configuracoes compostas, mais3redutores e3filtros ebaseline nesta campanha=32carteiras; considerando demais estudos anteriores sao MAIS DE25configuracoes/hipoteses no mesmohistorico e inventario de tentativas e incompleto. Nao afirmar que9+16>25. Nenhuma candidata e validacao independente; cauda tambem nao sera chamada confirmacaoOOS. Resultados anteriores conhecidos. Parametros/criterios nao flexibilizados apos ver resultados. Possivel reclassificacao por nova janela/corte deve ser explicitada.

Entrega: nova pasta propostas/MC-PROP-20261025-MULTI16-HS-01, statusPROPOSTO_PARA_REVISAO, relatorio completo, tabelas32unidades, preregistrohash, manifestos, ZIP com runs/64 e todas evidencias regeneraveis/codigo local. Validacao documental compara baseline, nenhuma edicao oficial para corrigir errospreexistentes. Sem homologacao ou incorporacao.
'''
 p=S/'PREREGISTRO_V1.md';assert not p.exists();p.write_text(text+'\nPreparado BRT: '+datetime.now(timezone(timedelta(hours=-3))).isoformat()+'\n',encoding='utf-8');shutil.copyfile(p,DEST/p.name)
 (S/'PRE_REGISTRO_HASH.json').write_text(json.dumps({'sha256':sha(p),'MC_copy_identical':sha(p)==sha(DEST/p.name),'economic_runs_started':False},indent=2),encoding='utf-8')
 print('PREREG_FROZEN',sha(p),str(DEST),flush=True)
if __name__=='__main__':main()
