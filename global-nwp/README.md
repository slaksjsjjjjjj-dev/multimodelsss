# SIDERAL Global NWP — coleta e Releases regionais

Este pacote pertence ao repositório \`slaksjsjjjjjj-dev/multimodelsss\`.

## O que é publicado

O workflow \`.github/workflows/SIDERAL_GLOBAL_NWP_RELEASES.yml\` agenda quatro tentativas por dia UTC (00Z, 06Z, 12Z e 18Z aproximados) e também pode ser iniciado manualmente. Cada execução cria uma GitHub Release com:

- \`catalog.json\`: definições dos modelos, regiões, status, variáveis, níveis, validades e nomes dos ZIPs.
- \`run-report.json\`: resumo dos modelos, regiões, frames e campos que de fato foram publicados.
- \`<model_key>--<region_id>.zip\`: pacote de uma região para um modelo; contém \`manifest.json\` e os arquivos \`forecast/fNNN.json.gz\`.

Cada \`fNNN.json.gz\`, quando descompactado, é um JSON UTF-8 com metadados, grades/coordenas e todos os campos GRIB2 que foram extraídos para aquela região/validade. O inventário registra nome curto, nome legível, unidade, parâmetro GRIB, tipo de nível, nível vertical, membro, tipo/intervalo de passo e tempo válido.

Um horário pode ter vários arquivos `partNNN` quando o modelo publica muitas variáveis, níveis ou membros. O manifesto liga cada parte ao F correspondente. A coleta usa os passos nativos disponíveis em cada produto. Não fabrica F horários que o provedor não publicou; por exemplo, um produto de 3 em 3 horas não ganha F001/F002 artificiais. O manifesto lista exatamente os F disponíveis. Ausências de arquivos, variáveis ou níveis são registradas como \`partial\` ou \`failed\`.

## Modelos configurados

| ID | Produto | Fonte/coletor | Alcance de catálogo |
|---|---|---|---:|
| \`ecmwf_ifs\` | ECMWF / IFS (oper) | ECMWF Open Data via Herbie | F240; ciclos 06/18 terminam antes |
| \`ecmwf_eps\` | ECMWF / EPS (enfo) | ECMWF Open Data via Herbie | F360 em 00/12; F144 em 06/18 |
| \`ecmwf_aifs\` | ECMWF / AIFS | ECMWF Open Data via Herbie | F360 |
| \`ecmwf_aifs_ens\` | ECMWF / AIFS-ENS | ECMWF Open Data via Herbie | F360 |
| \`icon_dwd\` | ICON / DWD | Catálogo DWD + DWD CDO weights | F180; saída regional em 0.125° |
| \`icon_ai\` | ICON / AI (AICON) | Catálogo DWD + DWD CDO weights | F180; saída regional em 0.125° |
| \`gfs\` | GFS / GEV3 (campos primários e secundários) | NOAA/NOMADS via Herbie | F384 |
| \`gfs_ai\` | GFS / AI (AIGFS) | NOAA/NOMADS via Herbie | F384 |
| \`gefs_ai\` | GEFS / AI (AIGEFS) | NOAA/NOMADS via Herbie; média e dispersão | F384 |
| \`hgefs\` | HGEFS | NOAA/NOMADS via Herbie; média e dispersão | F240 |
| \`cmc_gdps\` | CMC / GDPS | ECCC Datamart | F240; usa ciclos 00/12 |
| \`cmc_geps\` | CMC / GEPS | ECCC Datamart | F384 (conforme disponibilidade do diretório) |
| \`ukmet\` | UKMET | Met Office Weather DataHub | Bloqueado até configurar assinatura e credenciais |

**Observações importantes**

- O ECMWF IFS determinístico (stream \`oper\`) não publica o mesmo horizonte do EPS. No Open Data, HRES/IFS chega a F240 em 00/12 e F090 em 06/18; o EPS é que chega a F360 nos ciclos 00/12. A interface não deve mostrar validades que não apareçam no manifesto.
- O DWD publica ICON/AICON na grade nativa não estruturada. O workflow baixa os pesos oficiais DWD \`ICON_GLOBAL2WORLD_0125_EASY\` e regrida a saída regional para grade regular de 0.125° antes do recorte.
- EPS publica todos os campos dos produtos CF/EM/ES/EP e os produtos de ondas disponíveis. AIFS-ENS publica CF, PF (todos os membros quando o provedor entrega o conjunto no arquivo) e EP; não limitamos o `number` do PF.
- AIGEFS e HGEFS são coletados inicialmente como produtos agregados de média e dispersão; isso não equivale a arquivar cada membro individual.
- CMC/GEPS usa o diretório `raw`, não `products`, para capturar todos os campos e membros individuais do ensemble. O ECCC oferece F000–F384 e extensão até F936 em ciclos 00Z de segunda/quinta.
- UKMET requer assinatura Global Atmospheric e credencial de API do Weather DataHub. A entrada está no catálogo, mas cada execução a marca \`blocked\`; não é anunciada como dado operacional. Consulte a documentação oficial: https://datahub.metoffice.gov.uk/.
- GDPS/GEPS são publicados em 00Z/12Z. GDPS é consultado em `/today/model_gdps/15km/{HH}/{hhh}/`; GEPS usa `/today/ensemble/geps/grib2/{raw,products}/{HH}/{hhh}/`. Quando a execução geral roda às 06Z/18Z, o coletor usa o ciclo 00Z/12Z imediatamente anterior.

## Regiões

As regiões são definidas em \`regions.json\` (WGS84; limites west/south/east/north):

- América do Sul e Pacífico Sul: região central/leste, Pacífico Sul Ocidental e Pacífico Sul Central.
- Brasil: Norte, Nordeste, Centro-Oeste, Sudeste e Sul.
- Argentina: NOA, NEA, Cuyo, Pampa e Patagônia.

## Como acessar a Release

1. Abra o separador **Actions** do repositório e selecione **SIDERAL — Global NWP JSON Releases**.
2. Acompanhe o job **Publicar Releases regionais**.
3. Quando o job terminar, abra **Releases** e localize \`sideral-global-nwp-<data>-<ciclo>-<run_id>\`.
4. Baixe \`catalog.json\` para descobrir quais ZIPs existem e quais F estão disponíveis para cada modelo/região.
5. Dentro do ZIP, leia \`manifest.json\` e descompacte os frames \`forecast/fNNN.json.gz\`.

## Configuração e limites

Não há chaves de API embutidas no código. UKMET exige uma assinatura e credenciais próprias; não é habilitado automaticamente. Os provedores podem atrasar uma rodada, retirar dados antigos ou não oferecer todos os parâmetros em todos os níveis/passos. O manifesto de cada Release é a fonte de verdade para a disponibilidade concreta.

O primeiro disparo real deve ser acompanhado em **Actions**. A criação dos arquivos e a publicação do workflow no repositório não substituem o teste de uma execução completa contra as fontes externas.
