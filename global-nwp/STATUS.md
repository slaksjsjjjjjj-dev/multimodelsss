# Situação da coleta global

- Repositório: `slaksjsjjjjjj-dev/multimodelsss`, branch `main`.
- Workflow: `.github/workflows/SIDERAL_GLOBAL_NWP_RELEASES.yml`.
- Publicação dos dados: branch `nwp-data`.
- O workflow agenda coletas às 09Z e 21Z, selecionando a rodada com pelo menos nove horas de atraso.

## Correções aplicadas em 9 de novembro de 2026

A execução `42599167158` expôs falhas que impediam downloads e publicação. Foram aplicadas as seguintes correções:

- **GFS:** o catálogo usava o adaptador `nomads-filter`, mas o despachante não o reconhecia. Agora o GFS é roteado para o filtro regional NOMADS.
- **AIGFS, AIGEFS e HGEFS:** os horários solicitados foram alinhados aos arquivos de seis em seis horas publicados pela NOAA.
- **GRIB:** arquivos truncados, respostas HTML e downloads inválidos são descartados. Downloads ECMWF repetem tentativas quando há falha transitória.
- **ECMWF:** produtos probabilísticos são baixados em grupos menores; cada validade processada recebe checkpoint e atualiza `collection-progress.json`.
- **ICON/AICON:** DWD é consultado em lotes de seis validades; cada lote grava JSONs e progresso. Uma interrupção não deve apagar os dados já processados.
- **JSONs grandes:** cada validade pode ser dividida em `FNNN.json.gz` e `FNNN-partNNN.json.gz`. O manifesto lista todas as partes em `validities[].files` e `download_urls`.
- **Publicação:** os arquivos acima de 80 MiB são barrados antes do push. Os dados são enviados primeiro para uma branch temporária; `nwp-data` só é promovida depois da verificação completa do catálogo, relatório e árvore de arquivos.
- **Uso de disco:** a agregação move os arquivos dos artefatos para a saída e o publicador Git trabalha nessa pasta diretamente, sem criar outra cópia completa dos vários GB.
- **Status:** jobs sem dados deixam de aparecer falsamente como sucesso. Falhas tardias preservam as validades que já foram convertidas, marcando o modelo como parcial.

## Validação

O SIDERAL CI passou no commit `c6f4f34ad99bfa0a2c15b84a8ba0b15b09402e26`, incluindo compilação dos coletores/publicadores, validação dos catálogos e **6 testes automatizados**: cadência dos modelos NOAA, horários GFS, integridade GRIB/gzip, divisão de frames grandes e checkpoints ECMWF.

**Limite desta validação:** o CI não baixa nem processa uma rodada meteorológica real. Nenhuma nova execução completa do workflow NWP foi iniciada com este código nesta sessão; por isso, a branch `nwp-data` ainda não está confirmada como atualizada com uma rodada pós-correção. A próxima execução NWP precisa confirmar download, conversão e promoção da publicação.

## Limitações conhecidas

- O regrade CDO ainda pode falhar em alguns campos estatísticos do ICON com template GRIB2 PDT 8. Esses campos podem deixar ICON/AICON como `partial`, enquanto os demais campos continuam.
- O coletor CMC-GEPS publica atualmente os produtos estatísticos de `/products`; os GRIBs `/raw` de todos os membros continuam desativados.
- Os recortes separados do Brasil e da Argentina permanecem desativados; as regiões ativas são América do Sul e Pacífico Sul.

## Execução anterior

A execução de 9 de novembro `42599167158` não deve ser usada como publicação completa: GFS não reconheceu o adaptador, ICON-DWD excedeu o tempo de coleta e a publicação foi rejeitada por um arquivo de 109,15 MB, acima do limite de 100 MB do GitHub. A branch de dados daquela execução ficou incompleta.
