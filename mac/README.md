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

## Avisos no WhatsApp

Duas fontes, com responsabilidades separadas: **o coletor avisa o progresso, o
vigia avisa os problemas.** Se os dois avisassem falha, o mesmo incidente
renderia duas mensagens — e o vigia existe exatamente para o caso em que o Mac
mini não consegue falar.

O coletor manda três mensagens, e só no dia em que há mês novo:

| Momento | Mensagem |
|---|---|
| Passo 3 confirma que há trabalho | 🆕 A Receita publicou AAAA-MM — N arquivos, X GB |
| Passo 4 termina | ⬇️ Download concluído — X GB em N min |
| Passo 9 cria o job | ✅ Entregue à VPS — o processamento leva ~4h |

Nos outros ~29 dias do mês ele encerra em 6 segundos **sem dizer nada**. Aviso
diário de "nada novo" viraria ruído e treinaria quem lê a ignorar a mensagem
que importa.

O primeiro aviso sai depois do passo 3, e não na detecção: a Receita serve o
mesmo mês por semanas, então avisar na detecção mandaria a mesma mensagem todo
dia. O que é novidade é *haver trabalho*, e quem responde isso é o `job_queue`
da VPS.

O `--ensaio` não avisa ninguém: é teste manual, e "A Receita publicou" seria
alarme falso.

Desligar temporariamente (a carga continua normal, só as mensagens param):

```bash
CNPJ_AVISAR=0 ./.venv/bin/python mac/coletor.py
```

**Teste nunca envia.** O `avisar` recusa enviar quando `PYTEST_CURRENT_TEST`
está no ambiente. A trava existe porque em 21/09/2026 dois testes que chamam
`coletar()` com os passos mockados esqueceram de mockar o `avisar`, e a suite
disparou 8 mensagens para uma pessoa de verdade. Se a suite começar a demorar
dezenas de segundos em vez de menos de um, é sinal de que algum teste voltou a
fazer chamada real.

## Batimento e vigia

O modo de falha mais provável deste desenho não produz erro nenhum: Mac mini
desligado, sem rede ou com o LaunchDaemon parado não geram log em lugar algum —
o mês não entra e a API segue servindo o anterior sem reclamar. Silêncio se
parece com "a Receita ainda não publicou".

Por isso o coletor grava um batimento na VPS **no fim de toda execução**,
inclusive quando falha:

```
/var/lib/docker/volumes/cnpj_etl_checkpoints/_data/coletor_batimento.json
```

E `vps/vigia-cnpj.sh` roda no host da VPS de hora em hora (crontab do root,
minuto 15, com `flock`), avisando por WhatsApp:

| Gatilho | Quando você sabe |
|---|---|
| Sem batimento há +48h | ~2 dias |
| Último batimento diz `falha` | ~1 hora |
| Bridge do WhatsApp desconectada | ~1 hora |

Fala uma vez por incidente e avisa quando volta ao normal. O `--ensaio` não
grava batimento de propósito: é teste manual, e marcar presença por ele faria o
vigia aceitar como prova de vida algo que não prova que o daemon roda.

Conferir na mão, sem enviar nada:

```bash
ssh cnpj-vps 'bash /opt/cnpj/vps/vigia-cnpj.sh --dry-run'
ssh cnpj-vps 'cat /var/log/cnpj-vigia.log | tail'
```

**O `/health` da bridge mente.** Ela responde `{"status":"conectado"}` a partir
de uma variável interna que não acompanha a queda do socket do WhatsApp — visto
em 21/09/2026: um envio falhou com `Connection Closed` e o Baileys reconectou 12
segundos depois, com o `/health` dizendo "conectado" o tempo todo. Por isso o
vigia confere o HTTP do POST e só considera avisado o que saiu com 200; se
falhar, não grava a deduplicação e tenta de novo na hora seguinte.

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
