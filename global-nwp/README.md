# SIDERAL — Global NWP JSON Releases

Repositório: `slaksjsjjjjjj-dev/multimodelsss`.

## Publicação

O workflow `.github/workflows/SIDERAL_GLOBAL_NWP_RELEASES.yml` executa duas coletas por dia, às 09Z e 21Z. Cada execução seleciona a rodada de pelo menos nove horas antes (normalmente 00Z ou 12Z). Também existe execução manual em **Actions → SIDERAL — Global NWP JSON Releases → Run workflow**.

Os dados meteorológicos **não são empacotados em ZIP**. Cada execução publica uma árvore na branch `nwp-data`:

```text
nwp-data/
├── catalog.json
├── run-report.json
└── data/
    ├── ecmwf_ifs/
    │   ├── model_status.json
    │   ├── collection-summary.json
    │   ├── south_america/
    │   │   ├── manifest.json
    │   │   ├── F000.json.gz
    │   │   ├── F003.json.gz
    │   │   └── ...
    │   └── brazil_southeast/
    │       ├── manifest.json
    │       ├── F000.json.gz
    │       └── ...
    ├── gfs/
    │   ├── model_status.json
    │   ├── collection-summary.json
    │   └── south_america/
    │       ├── manifest.json
    │       ├── F000.json.gz
    │       ├── F001.json.gz
    │       └── ...
    └── ...
```

Cada `FNNN.json.gz` é um documento JSON independente, comprimido individualmente com gzip — não é um arquivo ZIP. Um arquivo contém todos os campos, níveis verticais e membros efetivamente coletados daquele modelo, daquela região e daquela validade. O manifesto fica na mesma pasta e lista os arquivos, variáveis, níveis, horas disponíveis e horas ausentes.

A branch `nwp-data` é substituída pela publicação mais recente para manter apenas a rodada atual acessível sem acumular todas as rodadas no histórico Git. A GitHub Release correspondente contém `catalog.json` e `run-report.json`, com link direto para a árvore de dados.

Links:
- Dados: https://github.com/slaksjsjjjjjj-dev/multimodelsss/tree/nwp-data/data
- Catálogo: https://github.com/slaksjsjjjjjj-dev/multimodelsss/blob/nwp-data/catalog.json
- Relatório: https://github.com/slaksjsjjjjjj-dev/multimodelsss/blob/nwp-data/run-report.json

## Cadência das previsões

O coletor grava **todas as validades que a fonte publica para cada modelo**, sem pular uma validade disponível por causa de um empacotamento. Não cria horários artificiais quando o provedor não fornece aquele F.

No GFS 0,25°, a NOAA publica previsões de hora em hora até F120 e, depois, a cada três horas até F384. Portanto, depois de F120, a sequência esperada é F123, F126, F129 etc.; F121 e F122 não são arquivos publicados nesse produto. Essa cadência é da fonte, não uma falha do coletor. Consulte o [inventário oficial de produtos GFS no NOMADS](https://www.nco.ncep.noaa.gov/pmb/products/gfs/nomads/) e a [descrição do conjunto GFS 0,25°](https://developers.google.com/earth-engine/datasets/catalog/NOAA_GFS0P25).

O campo `requested_forecast_hours` mostra o calendário esperado pelo coletor. `forecast_hours` lista as validades realmente encontradas e convertidas. `missing_forecast_hours` identifica as validades esperadas que não chegaram ao JSON regional. A ausência de um horário nunca é preenchida com interpolação ou cópia de outro F.

## Modelos configurados

| ID | Produto | Fonte | Resolução / horizonte catalogado |
|---|---|---|---|
| `ecmwf_ifs` | IFS HRES — atmosfera e ondas | ECMWF Open Data | 0,25°; até F240 atmosfera e F360 ondas em 00/12Z |
| `ecmwf_eps` | IFS ENS — membros, média, dispersão, probabilidades e ondas | ECMWF Open Data | 0,25°; até F360 em 00/12Z |
| `ecmwf_aifs` | AIFS Single — atmosfera e ondas | ECMWF Open Data | 0,25°; até F360 |
| `ecmwf_aifs_ens` | AIFS-ENS — controle, membros, média, dispersão, probabilidades e ondas | ECMWF Open Data | 0,25°; até F360 |
| `icon_dwd` | ICON global determinístico | DWD Open Data | Malha triangular nativa; até F180 |
| `icon_ai` | AICON global | DWD Open Data | Malha triangular nativa; até F180 |
| `gfs` | GFS / GEV3 — produtos principais e secundários | NOAA / NOMADS via Herbie | 0,25°; até F384 |
| `gfs_ai` | AIGFS — superfície e pressão | NOAA / NOMADS via Herbie | 0,25°; até F384 |
| `gefs_ai` | AIGEFS — média e dispersão de superfície e pressão | NOAA / NOMADS via Herbie | 0,25°; até F384 |
| `hgefs` | HGEFS — média e dispersão de superfície e pressão | NOAA / NOMADS via Herbie | 0,25°; até F240 |
| `cmc_gdps` | GDPS determinístico | ECCC Datamart | 0,15°; até F240 |
| `cmc_geps` | GEPS raw e produtos estatísticos | ECCC Datamart | 0,5°; até F384, com extensão F936 em ciclos específicos |
| `ukmet` | UKMET Global | Met Office Weather DataHub | Bloqueado até configurar assinatura e credenciais |

AIGEFS e HGEFS estão configurados para campos agregados de média/dispersão; não prometem arquivar todos os membros individuais. UKMET permanece bloqueado até existir uma assinatura Global Atmospheric e uma credencial do Weather DataHub.

## Coleta NOAA / GFS

Os downloads via Herbie agora usam erros explícitos em vez do padrão de apenas emitir aviso, tentam novamente falhas transitórias, verificam se o arquivo GRIB foi realmente salvo e executam downloads de até quatro validades em paralelo para GFS. O processamento e gravação dos campos regionais continuam serializados para não corromper os arquivos de saída.

O arquivo `collection-progress.json` é atualizado após cada validade concluída e preserva:
- total de validades solicitadas;
- validades concluídas;
- validades com arquivo;
- validades sem arquivo;
- horário da última atualização.

O passo de coleta tem limite próprio de 320 minutos, dentro do limite maior do job, reservando tempo para gravar o diagnóstico e enviar artefatos mesmo quando um provedor demora demais.

## Regiões

Todas as regiões são WGS84 e definidas em `regions.json` (limites oeste/sul/leste/norte):
- América do Sul.
- Pacífico Sul — faixa principal, Ocidental e Central.
- Brasil — Norte, Nordeste, Centro-Oeste, Sudeste e Sul.
- Argentina — NOA, NEA, Cuyo, Pampa e Patagônia.

## Como consumir

1. Abra a branch **nwp-data** e navegue até `data/<modelo>/<regiao>/`.
2. Leia o `manifest.json` para obter as validades e a lista de arquivos JSON.
3. Baixe o `FNNN.json.gz` desejado e descompacte-o com gzip.
4. No JSON, use `metadata.forecast_hour`, `metadata.valid_time`, `fields[].short_name`, `fields[].level`, `fields[].grid_type` e `grids[fields[].grid_id]` para interpretar os valores e coordenadas.

## Status e diagnóstico

Um modelo pode terminar com status `published-ready`, `partial`, `failed` ou `blocked`. O catálogo nunca interpreta um modelo apenas configurado como modelo com dados. A disponibilidade operacional precisa ser confirmada pelo `model_status.json`, `collection-summary.json`, `collection-progress.json` e pelos manifestos regionais.

A validação estática exige os 13 IDs de modelo e compara o conjunto com a matriz do workflow. As falhas de provedor são sinalizadas no GitHub Actions, mas os artefatos de diagnóstico são enviados antes de marcar o job como falho. A etapa de publicação roda mesmo se alguns modelos falharem.
