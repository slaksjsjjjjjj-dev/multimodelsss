# Estado da integração Global NWP

- O workflow de coleta completa é executado por agendamento ou pelo botão Run workflow.
- Pushs em `global-nwp/**` e no próprio workflow executam somente a validação estática; eles não iniciam downloads meteorológicos grandes.
- A coleta completa publica uma Release distinta por ciclo e lista em `catalog.json` apenas os modelos, variáveis, níveis e validades efetivamente obtidos.
- A primeira execução operacional deve ser acompanhada para medir volume, duração, disponibilidade de cada fonte e eventuais campos/validades ausentes.
- UKMET permanece bloqueado até a configuração da assinatura Global Atmospheric e credenciais do Met Office Weather DataHub.
