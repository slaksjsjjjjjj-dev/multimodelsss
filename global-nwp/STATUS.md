# Situação da coleta global

- Repositório: `slaksjsjjjjjj-dev/multimodelsss`, branch `main`.
- Workflow: `.github/workflows/SIDERAL_GLOBAL_NWP_RELEASES.yml`.
- O workflow está configurado para duas coletas diárias, após as rodadas 00Z/12Z.
- Alterações em código/catálogo executam validação estática; pushes não iniciam downloads grandes.
- Os JSONs são divididos por modelo, região, validade e partes, e publicados em GitHub Releases somente se houver ao menos um pacote regional.
- Regiões: 14 recortes da América do Sul, Pacífico Sul, Brasil e Argentina.

## Validação operacional

A primeira execução agendada observada (run `41328776662`, com uma revisão anterior do coletor) não deve ser considerada uma publicação válida: ECMWF foi consultado antes da disponibilização dos arquivos, Herbie recebeu datetimes com timezone incompatível, e o regridding CDO do ICON reportou erros em alguns templates GRIB2. As correções já foram feitas no código atual: atraso de nove horas para escolher o ciclo, UTC sem tzinfo para Herbie e recorte direto da malha triangular nativa ICON.

O CMC-GDPS teve o job cancelado nessa primeira execução e o CMC-GEPS ainda estava executando quando a revisão foi observada. UKMET segue bloqueado por exigir credenciais do Met Office.

**Status honesto:** os scripts e o workflow estão versionados; a primeira execução não concluiu uma Release de dados. É necessário que a nova rodada agendada/manual confirme a coleta real de cada fonte antes de marcar todos os modelos como operacionais.
