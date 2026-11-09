# SIDERAL — Global NWP JSON Branch

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

Cada `FNNN.json.gz` é um documento JSON independente, comprimido individualmente com gzip — não é um arquivo ZIP. Quando uma validade fica grande demais para um único blob do GitHub, ela é dividida em `FNNN.json.gz`, `FNNN-part002.json.gz` e assim por diante. O manifesto lista todas as partes em `validities[].files` e `validities[].download_urls`; clientes antigos ainda podem usar `file` e `download_url` para abrir a primeira parte.

A publicação é preparada em uma branch temporária. A branch `nwp-data` só é atualizada quando todos os lotes de dados, o catálogo e o relatório foram enviados e verificados; assim, falhas intermediárias não substituem a última publicação válida.

Links:
- Dados: https://github.com/slaksjsjjjjjj-dev/multimodelsss/tree/nwp-data/data
- Catálogo: https://github.com/slaksjsjjjjjj-dev/multimodelsss/blob/nwp-data/catalog.json
- Relatório: https://github.com/slaksjsjjjjjj-dev/multimodelsss/blob/nwp-data/run-report.json

## Cadência das previsões

O coletor grava **as validades efetivamente oferecidas por cada fonte** e não cria horários artificiais. O AIGFS, AIGEFS e HGEFS usam a cadência de seis horas documentada para esses produtos; GFS mantém F000–F120 horário e depois intervalos de três horas.

No GFS 0,25°, a NOAA publica previsões de hora em hora até F120 e, depois, a cada três horas até F384. Portanto, depois de F120, a sequência esperada é F123, F126, F129 etc.; F121 e F122 não são arquivos publicados nesse produto. Essa cadência é da fonte, não uma falha do coletor. Consulte o [inventário oficial de produtos GFS no NOMADS](https://www.nco.ncep.noaa.gov/pmb/products/gfs/nomads/) e a [descrição do conjunto GFS 0,25°](https://developers.google.com/earth-engine/datasets/catalog/NOAA_GFS0P25).

O campo `requested_forecast_hours` mostra o calendário esperado pelo coletor. `forecast_hours` lista as validades realmente encontradas e convertidas. `missing_forecast_hours` identifica as validades esperadas que não chegaram ao JSON regional. A ausência de um horário nunca é preenchida com interpolação ou cópia de outro F.

## Modelos configurados

| ID | Produto | Fonte | Resolução / horizonte catalogado |
|---|---|---|---|
| `ecmwf_ifs` | IFS HRES — atmosfera e ondas | ECMWF Open Data | 0,25°; até F240 atmosfera e F360 ondas em 00/12Z |
| `ecmwf_eps` | IFS ENS — membros, média, dispersão, probabilidades e ondas | ECMWF Open Data | 0,25°; até F360 em 00/12Z |
| `ecmwf_aifs` | AIFS Single — atmosfera e ondas | ECMWF Open Data | 0,25°; até F360 |
| `ecmwf_aifs_ens` | AIFS-ENS — controle, membros, média, dispersão, probabilidades e ondas | ECMWF Open Data | 0,25°; até F360 |
| `icon_dwd` | ICON global determinístico | DWD Open Data + regrade CDO | Malha triangular para lat/lon 0,25°; até F180 |
| `icon_ai` | AICON global | DWD Open Data + regrade CDO | Malha triangular para lat/lon 0,25°; até F180 |
| `gfs` | GFS / GEV3 — produtos principais e secundários | NOAA / NOMADS GRIB filter | 0,25°; até F384 |
| `gfs_ai` | AIGFS — superfície e pressão | NOAA / Herbie | 0,25°; até F384, intervalos 6h |
| `gefs_ai` | AIGEFS — média e dispersão de superfície e pressão | NOAA / Herbie | 0,25°; até F384, intervalos 6h |
| `hgefs` | HGEFS — média e dispersão de superfície e pressão | NOAA / Herbie | 0,25°; até F240, intervalos 6h |
| `cmc_gdps` | GDPS determinístico | ECCC Datamart | 0,15°; até F240 |
| `cmc_geps` | GEPS raw e produtos estatísticos | ECCC Datamart | 0,5°; até F384, com extensão F936 em ciclos específicos |
| `ukmet` | UKMET Global | Met Office Weather DataHub | Bloqueado até configurar assinatura e credenciais |

AIGEFS e HGEFS estão configurados para campos agregados de média/dispersão; não prometem arquivar todos os membros individuais. UKMET permanece bloqueado até existir uma assinatura Global Atmospheric e uma credencial do Weather DataHub.

## Coleta NOAA / GFS

Os downloads usam validação da assinatura GRIB e do terminador 7777, descartam arquivos truncados e tentam novamente falhas transitórias. GFS operacional usa o filtro regional do NOMADS; AIGFS, AIGEFS e HGEFS usam Herbie e solicitam apenas as validades de seis em seis horas. O processamento e a gravação regional continuam serializados para não corromper os arquivos de saída.

O arquivo `collection-progress.json` é atualizado após cada validade concluída e preserva:
- total de validades solicitadas;
- validades concluídas;
- validades com arquivo;
- validades sem arquivo;
- horário da última atualização.

O passo de coleta tem limite próprio de 340 minutos. ICON/AICON são baixados em lotes de seis validades e cada lote gera checkpoints, para que os dados já processados possam ser publicados mesmo quando uma execução é interrompida.

## Regiões

As áreas atualmente ativadas no coletor são WGS84 e definidas em `regions.json`:
- América do Sul.
- Pacífico Sul.

Os recortes específicos do Brasil e da Argentina estão temporariamente desativados nesta versão para evitar duplicação dentro da região América do Sul.

## Como consumir

1. Abra a branch **nwp-data** e navegue até `data/<modelo>/<regiao>/`.
2. Leia o `manifest.json` para obter as validades e a lista de arquivos JSON.
3. Baixe todas as partes listadas em `validities[].files` (ou `download_urls`) e descompacte cada `.json.gz` com gzip. Para validade sem divisão, a lista contém apenas `FNNN.json.gz`.
4. No JSON, use `metadata.forecast_hour`, `metadata.valid_time`, `fields[].short_name`, `fields[].level`, `fields[].grid_type` e `grids[fields[].grid_id]` para interpretar os valores e coordenadas.

## Status e diagnóstico

Um modelo pode terminar com status `published-ready`, `partial`, `failed` ou `blocked`. O catálogo nunca interpreta um modelo apenas configurado como modelo com dados. A disponibilidade operacional precisa ser confirmada pelo `model_status.json`, `collection-summary.json`, `collection-progress.json` e pelos manifestos regionais.

Cada job de modelo grava seu status e envia o artefato mesmo quando a coleta falha; o workflow marca explicitamente os modelos sem dados como falha. A publicação consolida os artefatos disponíveis, valida o tamanho dos blobs e só promove a branch de dados quando catálogo e arquivos estão completos.
