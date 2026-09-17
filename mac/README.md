# Coletor da Receita no Mac mini

O download dos dados da Receita sai da VPS e roda aqui. Desde 11/09/2026
`arquivos.receitafederal.gov.br` derruba toda conexão HTTPS vinda do IP da VPS,
que é de datacenter; uma conexão brasileira comum continua alcançando. O
processamento continua na VPS — este Mac mini só busca e entrega.

Contexto completo em `docs/HANDOFF-MAC-MINI.md`.

## O que roda aqui

| Peça | Onde |
|---|---|
| Coletor | `mac/coletor.py` — fora da imagem Docker |
| Agendamento | `mac/br.com.mbfinance.cnpj-coletor.plist` — 22:00 BRT |
| Modos sem rede que ele chama na VPS | `enqueue_job.py --status` e `--files-ready` |

O coletor não toca o Postgres e não guarda estado próprio: "esse mês já foi
processado?" é sempre perguntado ao `job_queue` da VPS por SSH.

## Instalação do agendamento

O plist é um **LaunchDaemon**, não um Agent: precisa rodar sem ninguém logado.
Um Agent só existe dentro de uma sessão gráfica, então depois de um reinício ele
só dispararia quando alguém fizesse login.

```bash
cd ~/cnpj/Baixar_Ncpj
sudo cp mac/br.com.mbfinance.cnpj-coletor.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/br.com.mbfinance.cnpj-coletor.plist
sudo chmod 644       /Library/LaunchDaemons/br.com.mbfinance.cnpj-coletor.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/br.com.mbfinance.cnpj-coletor.plist
```

Conferir que ficou carregado:

```bash
sudo launchctl print system/br.com.mbfinance.cnpj-coletor | grep -E "state|program|runs"
```

Disparar na mão, fora da janela das 22h:

```bash
sudo launchctl kickstart system/br.com.mbfinance.cnpj-coletor
```

Depois de editar o plist, recarregar:

```bash
sudo launchctl bootout system/br.com.mbfinance.cnpj-coletor
sudo cp mac/br.com.mbfinance.cnpj-coletor.plist /Library/LaunchDaemons/
sudo launchctl bootstrap system /Library/LaunchDaemons/br.com.mbfinance.cnpj-coletor.plist
```

## Rodar na mão

```bash
cd ~/cnpj/Baixar_Ncpj

./.venv/bin/python mac/coletor.py              # execução normal
./.venv/bin/python mac/coletor.py --ensaio     # para antes de renomear, não cria job
./.venv/bin/python mac/coletor.py --run-key 2026-10
```

O `--ensaio` envia para `<mes>.incoming/` na VPS, confere lá e para. Não renomeia
para `<mes>/`, não entrega manifest e não cria job — e, ao contrário da execução
normal, não encerra nem apaga nada quando o mês já está SUCCESS, que é o que
permite ensaiar com um mês já processado. A pasta `.incoming` fica no servidor
para você conferir; apague na mão depois.

## Logs

| Arquivo | O que tem |
|---|---|
| `~/cnpj/logs/etl_AAAA-MM-DD.log` | JSON estruturado, rotação diária, 30 dias |
| `~/cnpj/logs/coletor.launchd.log` | stdout legível capturado pelo launchd |
| `~/cnpj/logs/coletor.launchd.err` | stderr |

Os dois arquivos do launchd não rotacionam — crescem devagar, mas crescem.

## Pré-requisitos desta máquina

Conferidos em 17/09/2026:

| Item | Estado |
|---|---|
| Saída de rede | Algar, `177.69.202.213`, São Paulo — comum e brasileira. **Sem VPN nem proxy** |
| Acesso à Receita | `207` no PROPFIND em 0,2s |
| Energia e sono | `pmset -a sleep 0 disksleep 0 autorestart 1` |
| FileVault | Off — com ele ligado o Mac para na tela de desbloqueio e o launchd não roda |
| Fuso | `America/Sao_Paulo` |
| Python | 3.12 do Homebrew, venv em `.venv` |
| rsync | 3.5.0 do Homebrew, **não** o openrsync do macOS (que não tem `--partial` confiável) |
| Chave SSH | `~/.ssh/cnpj_vps`, alias `cnpj-vps`, root na VPS |
| Disco | ≥ 20 GB livres — 7,76 GB por mês, mantidos até o job dar SUCCESS |

Teste de acesso à Receita, o único que é go/no-go:

```bash
TOKEN=$(grep WEBDAV_TOKEN .env | cut -d= -f2)
curl -s -o /dev/null -w '%{http_code}\n' --max-time 30 \
  -X PROPFIND -u "$TOKEN:" -H 'Depth: 1' \
  "https://arquivos.receitafederal.gov.br/public.php/dav/files/$TOKEN/"
```

`207` passa. `000`, reset ou timeout significa que a Receita está barrando esta
saída também — e aí o plano inteiro para, porque não existe plano B nesta
máquina.

## Diagnóstico

| Sintoma | Provável causa |
|---|---|
| `outra execucao em andamento` | Execução anterior ainda rodando, ou travada. Ver `~/cnpj/coletor.lock` e `pgrep -fl coletor.py` |
| `foi publicado ha X h, menos que 2h` | Guarda proposital: a Receita pode estar no meio da publicação |
| `esta DEAD — nao vou mexer` | O job esgotou as 3 tentativas na VPS. Pede intervenção manual; os ZIPs locais ficam para reenvio |
| `ja existe na VPS e nao bate com o manifest` | `/data/<mes>` já existe lá com conteúdo divergente. Conferir na mão antes de rodar de novo |
| Nada nos logs às 22h | Máquina desligada, dormindo, ou plist não carregado. `sudo launchctl print system/br.com.mbfinance.cnpj-coletor` |

Hoje **não existe aviso de falha**: se este Mac mini estiver desligado ou sem
rede, o mês passa em silêncio até alguém notar dado velho. É decisão em aberto
no handoff.
