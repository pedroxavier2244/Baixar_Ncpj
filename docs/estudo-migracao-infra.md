# Estudo de Migração de Infraestrutura
**Data:** 2026-05-22 | **Referência:** `infra-vps-diagnostico.md`

---

## Contexto — O que precisa suportar

| Requisito | Detalhe |
|---|---|
| PostgreSQL | 70M+ linhas (CNPJ), cresce ~13 GB/mês |
| ETL mensal | Pipeline que usa ~20 GB temp de disco + pico de CPU/RAM |
| API REST | FastAPI com Redis cache, tempo de resposta é crítico para usuários BR |
| Múltiplos projetos | ~30 containers (CRM, bot builder, discador, organograma, etc.) |
| RAM mínima necessária | 16 GB (atual 12 GB já está com swap 100%) |
| Disco mínimo necessário | 400–500 GB (atual 193 GB enche em ~2 meses) |
| CPU mínima necessária | 6–8 vCPUs (atual 6 com 225% consumidos só pelo discador) |

---

## O Problema de Latência

O servidor atual está na **Europa (Contabo, Frankfurt/similar)**. Para um usuário em São Paulo:

```
Usuário BR → Internet → Atlântico → Europa → Servidor → resposta → Atlântico → Usuário BR
Latência de rede: ~160–200 ms só de ida e volta
```

Se a API leva 50ms para processar, o usuário sente **210–250ms por requisição**.

Com servidor em São Paulo:
```
Usuário BR → Internet local → SP → resposta
Latência de rede: ~5–20ms
```

O mesmo processamento de 50ms seria sentido como **55–70ms** — **4–5x mais rápido percebido**.

> Para a API CNPJ que serve CRMs e sistemas de vendas, essa diferença é significativa em fluxos que fazem múltiplas consultas (ex: validar CNPJ ao cadastrar cliente).

---

## Opção 0 — Manter Contabo + Upgrade de Plano *(sem migração)*

### O que muda
Atualizar o plano atual para um com mais RAM e disco. Sem mudança de datacenter, sem migração de dados.

### Planos disponíveis

| Plano | vCPU | RAM | Disco | Preço/mês |
|---|---|---|---|---|
| **Atual (estimado)** | 6 | 12 GB | 193 GB SSD | ~€5–7 |
| **Cloud VPS 30** ← recomendado | 8 | **24 GB** | **400 GB SSD** | **~€11 (~$15)** |
| Cloud VPS 40 | 12 | 48 GB | 500 GB SSD | ~€20 (~$22) |
| Cloud VPS 50 | 16 | 64 GB | 600 GB SSD | ~€30 (~$32) |
| Storage VPS 40 | 8 | 30 GB | **1.2 TB SSD** | ~€20 (~$22) |

> Contabo **não tem datacenter no Brasil**. Locais disponíveis: Frankfurt, Helsinki, Londres, New York, Seattle, St. Louis, Singapura, Tóquio, Sydney, Mumbai. O mais próximo do Brasil seria Nova York (~140–160ms).

### Prós
- Custo absurdamente baixo (~$15/mês para 8 vCPU / 24 GB / 400 GB)
- Zero migração — apenas resize do plano
- Já conhece o ambiente
- 400 GB SSD resolve o problema de disco por ~18 meses

### Contras
- Latência continua ~160–200ms para usuários brasileiros
- Sem managed database — você gerencia backup, vacuum, WAL, upgrades
- Sem SLA enterprise (Contabo é budget VPS)
- Sem escala automática — se crescer muito, migração manual de novo

### Custo total estimado
| Item | Valor |
|---|---|
| Cloud VPS 30 (8 vCPU / 24 GB / 400 GB) | ~$15/mês |
| Upgrade de disco adicional se precisar (+200 GB bloco) | ~$4/mês |
| **Total** | **~$15–19/mês** |

---

## Opção 1 — Hostinger VPS Brasil *(melhor custo-benefício com BR)*

### O que é
VPS KVM com datacenter **confirmado em São Paulo** (tier-3, AMD EPYC, NVMe). Inaugurado em dezembro de 2022. Preços promocionais com contrato de 24 meses.

### Planos disponíveis

| Plano | vCPU | RAM | NVMe | Preço/mês (24 meses) |
|---|---|---|---|---|
| KVM 4 | 4 | 16 GB | 200 GB | **$12.99** |
| **KVM 8** ← recomendado | **8** | **32 GB** | **400 GB** | **$25.99** |

### Prós
- **Datacenter em São Paulo** — latência de 5–10ms para usuários brasileiros
- Preço extremamente competitivo para especificação BR
- 32 GB RAM resolve o problema de swap completamente
- 400 GB NVMe é mais rápido que SSD do Contabo
- Suporte em português

### Contras
- **Contrato de 24 meses obrigatório** para esses preços. Mês a mês seria ~2–3x mais caro (~$50–70/mês para o KVM 8)
- Sem managed database — auto-gerenciado
- Hostinger é menos conhecida para workloads críticos de produção vs AWS/GCP
- Reputação de suporte técnico de nível médio (não enterprise)
- Migração de dados necessária (mover 96 GB de PostgreSQL + configurar tudo)

### Custo total estimado
| Item | Valor |
|---|---|
| KVM 8 (8 vCPU / 32 GB / 400 GB NVMe, SP) | $25.99/mês (24 meses) |
| **Total** | **$25.99/mês** |

### Veredicto
**Melhor relação custo-benefício com servidor no Brasil.** Para quem quer latência baixa sem pagar preço AWS, esta é a opção. O risco é o lock-in de 24 meses e a menor maturidade de infraestrutura vs AWS/GCP.

---

## Opção 2 — Vultr São Paulo *(flexível, datacenter BR)*

### O que é
VPS com datacenter **confirmado em São Paulo** (ISO 27001, SOC 2). Paga mensal, sem contrato.

### Planos disponíveis em São Paulo

| Plano | vCPU | RAM | NVMe | Preço/mês |
|---|---|---|---|---|
| High Frequency 4c/16GB | 4 | 16 GB | 384 GB | $96 |
| **High Performance 8c/16GB** ← recomendado | **8** | **16 GB** | **350 GB** | **$96** |
| High Performance 8c/32GB | 8 | 32 GB | 500 GB | $192 |

### Prós
- **Datacenter em São Paulo** — latência ~5–10ms
- **Sem contrato** — paga mensal, cancela quando quiser
- Reputação sólida no mercado
- Interface limpa, API robusta
- Managed PostgreSQL disponível (ver abaixo)

### Contras
- Preço 4–6x maior que Hostinger/Contabo
- Managed PostgreSQL em SP não tem preço confirmado (a verificar)
- Sem managed database documentado para SP com boas specs

### Custo total estimado
| Item | Valor |
|---|---|
| High Performance 8c/16GB (SP) | $96/mês |
| OU High Performance 8c/32GB (SP) | $192/mês |

### Veredicto
Boa opção se não quiser lock-in de 24 meses. ~4x mais caro que Hostinger, mas paga mês a mês. Para quem precisa de flexibilidade.

---

## Opção 3 — Hetzner Cloud *(melhor custo-benefício global, sem BR)*

### O que é
VPS europeia de alta qualidade, processadores dedicados AMD EPYC, melhor performance por euro do mercado. **Sem datacenter no Brasil ou América do Sul.**

### Planos CCX (CPU dedicada)

| Plano | vCPU | RAM | NVMe | Preço/mês |
|---|---|---|---|---|
| CCX23 | 4 | 16 GB | 160 GB | **~$34** |
| **CCX33** ← recomendado | **8** | **32 GB** | **240 GB** | **~$68** |
| CCX43 | 16 | 64 GB | 360 GB | ~$136 |

**Volumes adicionais:** €0.053/GB-mês. Para 300 GB extra: ~€16/mês.

### Prós
- Melhor performance de CPU dedicada pelo preço no mercado europeu
- Infraestrutura enterprise-grade, SLA 99.9%
- Volumes NVMe adicionais fáceis de anexar
- Muito bem avaliado pela comunidade técnica
- ~2x mais barato que Vultr para specs equivalentes

### Contras
- **Sem datacenter no Brasil** — latência ~200–230ms de São Paulo via Frankfurt
- Não resolve o problema de latência para usuários brasileiros
- Sem managed database

### Custo total estimado
| Item | Valor |
|---|---|
| CCX33 (8 vCPU / 32 GB / 240 GB NVMe) | ~$68/mês |
| Volume adicional 300 GB NVMe | ~$17/mês |
| **Total** | **~$85/mês** |

### Veredicto
**Se a latência para usuários finais NÃO for crítica** (ex: a API é consumida por sistemas internos, não diretamente pelo usuário), Hetzner é a melhor infra pelo preço. Para a API CNPJ que provavelmente é chamada de backends CRM, isso pode ser aceitável.

---

## Opção 4 — AWS EC2 + RDS São Paulo *(enterprise, mais caro)*

### O que é
Amazon Web Services com região completa em São Paulo (sa-east-1), 3 zonas de disponibilidade. Opção mais madura e confiável para produção crítica.

### EC2 (máquina virtual) em sa-east-1

| Instância | vCPU | RAM | Preço on-demand/mês | Preço reservado 1 ano/mês |
|---|---|---|---|---|
| t3.xlarge | 4 | 16 GB | **~$196** | ~$118 |
| **m6i.xlarge** ← recomendado | **4** | **16 GB** | **~$223** | **~$134** |
| m6i.2xlarge | 8 | 32 GB | ~$447 | ~$268 |

> `t3` é burstable (CPU limitada sob carga sustentada). Para ETL e banco de dados, usar `m6i` (CPU fixa).

**EBS gp3 em sa-east-1:** ~$0.12/GB-mês → 500 GB = **~$60/mês**

### RDS PostgreSQL gerenciado em sa-east-1

| Instância | vCPU | RAM | Preço on-demand/mês |
|---|---|---|---|
| db.t3.xlarge | 4 | 16 GB | **~$343** |
| db.m6g.large | 2 | 8 GB | **~$219** |

**Storage RDS gp3:** ~$0.14/GB-mês → 500 GB = **~$70/mês**

> RDS inclui: backups automáticos, point-in-time recovery, patching automático, Multi-AZ failover opcional.

### Prós
- **São Paulo** — latência ~5–15ms
- SLA de 99.99% com Multi-AZ
- **RDS gerenciado elimina toda gestão de banco** (backup automático, failover, patches)
- Escalabilidade horizontal em minutos
- Ecossistema maduro: CloudWatch, VPC, IAM, S3 para backups
- Reserved instances cortam custo em ~35–40%

### Contras
- **O mais caro de todas as opções** — 10–15x mais caro que Contabo
- Curva de aprendizado (VPC, IAM, security groups)
- Migração complexa (dados, DNS, variáveis de ambiente)
- Cobra por tráfego de saída (egress): ~$0.15/GB após os primeiros 100 GB

### Custo total estimado (auto-gerenciado, sem RDS)
| Item | Valor |
|---|---|
| EC2 m6i.xlarge on-demand | ~$223/mês |
| EBS gp3 500 GB | ~$60/mês |
| **Total sem RDS** | **~$283/mês** |

### Custo total estimado (com RDS gerenciado)
| Item | Valor |
|---|---|
| EC2 t3.xlarge (para APIs + outros containers) | ~$196/mês |
| RDS db.t3.xlarge + 500 GB gp3 | ~$413/mês |
| Tráfego egress estimado | ~$15/mês |
| **Total com RDS** | **~$624/mês** |

### Com reserved instances (1 ano)
| Cenário | Valor |
|---|---|
| Sem RDS (m6i.xlarge reservado + EBS) | ~$194/mês |
| Com RDS reservado | ~$380/mês |

### Veredicto
**Melhor opção se confiabilidade enterprise e managed database forem prioridade.** Preço proibitivo para estágio inicial. Faz sentido quando a receita justifica o custo (ex: $2000+/mês de faturamento relacionado à API).

---

## Opção 5 — Google Cloud (GCP) São Paulo *(bom custo-benefício gerenciado)*

### O que é
Google Cloud com região em São Paulo (southamerica-east1, data center em Jundiaí, SP). Geralmente 10–20% mais barato que AWS para specs equivalentes.

### Compute Engine em southamerica-east1

| Instância | vCPU | RAM | Preço on-demand/mês | Com Sustained Use (~30% off) |
|---|---|---|---|---|
| e2-standard-4 | 4 | 16 GB | **~$155** | **~$108** |
| **e2-standard-8** ← recomendado | **8** | **32 GB** | **~$311** | **~$217** |

> GCP aplica **Sustained Use Discount automaticamente** se você usar a instância por mais de 25% do mês — sem precisar fazer nada. Para 100% do mês, é 30% de desconto.

**Persistent Disk SSD 500 GB:** ~$0.187/GB-mês → **~$93/mês**

### Cloud SQL PostgreSQL em southamerica-east1

| Config | vCPU | RAM | Storage | Preço estimado/mês |
|---|---|---|---|---|
| db-custom-4-16384 | 4 | 16 GB | 500 GB SSD | **~$380** |
| db-custom-2-7680 | 2 | 7.5 GB | 300 GB SSD | **~$210** |

> Cloud SQL inclui: backups automáticos, alta disponibilidade com failover automático, replicas de leitura, patching gerenciado, point-in-time recovery.

### Prós
- **São Paulo** — latência ~5–15ms
- Sustained Use Discount automático (sem comprometer reservas)
- Cloud SQL é considerado mais fácil de usar que RDS
- Committed Use Discounts (1 ano) cortam mais ~37%
- Bom ecossistema para Python/FastAPI
- Egress para Brasil mais barato que AWS

### Contras
- Mais caro que Vultr/Hostinger/Contabo para VMs simples
- Cloud SQL em SP é caro (~$380/mês para 4 vCPU/16GB)
- Migração necessária

### Custo total estimado (auto-gerenciado, sem Cloud SQL)
| Item | Valor |
|---|---|
| e2-standard-8 (com SUD automático) | ~$217/mês |
| PD SSD 500 GB | ~$93/mês |
| **Total sem Cloud SQL** | **~$310/mês** |

### Custo total estimado (com Cloud SQL gerenciado)
| Item | Valor |
|---|---|
| e2-standard-4 para APIs/containers | ~$108/mês |
| Cloud SQL db-custom-4-16384 + 500 GB | ~$380/mês |
| **Total com Cloud SQL** | **~$488/mês** |

### Veredicto
Boa opção gerenciada com melhor custo que AWS. O Cloud SQL é excelente para quem quer tirar o banco do próprio servidor. Ainda caro, mas o SUD automático ajuda.

---

## Comparativo Final

| # | Provedor | DC Brasil | RAM | vCPU | Disco | Preço/mês | Banco Gerenciado | Latência SP |
|---|---|---|---|---|---|---|---|---|
| 0 | **Contabo Cloud VPS 30** | ❌ (NY mais próx.) | 24 GB | 8 | 400 GB SSD | **~$15** | ❌ | ~150ms |
| 1 | **Hostinger KVM 8** | ✅ São Paulo | 32 GB | 8 | 400 GB NVMe | **$25.99** *(24 meses)* | ❌ | ~5ms |
| 2 | **Vultr High Perf 8c/32GB** | ✅ São Paulo | 32 GB | 8 | 500 GB NVMe | $192 | ⚠️ (verificar) | ~5ms |
| 3 | **Hetzner CCX33 + volume** | ❌ (Europa) | 32 GB | 8 | 540 GB NVMe | **~$85** | ❌ | ~220ms |
| 4a | **AWS EC2 m6i.xlarge + EBS** | ✅ São Paulo | 16 GB | 4 | 500 GB | $283 | ❌ (self) | ~10ms |
| 4b | **AWS EC2 + RDS sa-east-1** | ✅ São Paulo | 16+16 GB | 4+4 | 500 GB | **$624** | ✅ | ~10ms |
| 5a | **GCP e2-standard-8 + PD** | ✅ São Paulo | 32 GB | 8 | 500 GB | $310 | ❌ (self) | ~10ms |
| 5b | **GCP e2-std-4 + Cloud SQL** | ✅ São Paulo | 16+16 GB | 4+4 | 500 GB | **$488** | ✅ | ~10ms |

---

## Matriz de Decisão

| Critério | Peso | Contabo | Hostinger BR | Vultr BR | Hetzner | AWS | GCP |
|---|---|---|---|---|---|---|---|
| Custo | 30% | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐ | ⭐ | ⭐ |
| Latência para BR | 25% | ⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| Confiabilidade | 20% | ⭐⭐ | ⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| Banco gerenciado | 10% | ❌ | ❌ | ⚠️ | ❌ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| Facilidade de uso | 10% | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐ |
| Escalabilidade | 5% | ⭐ | ⭐ | ⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |

---

## Recomendação por Cenário

### Cenário A — "Quero o mínimo de mudança, só resolver os gargalos"
**→ Contabo Cloud VPS 30 (~$15/mês)**

Dobra a RAM (12→24 GB), resolve o swap, quadruplica o disco (193→400 GB), ganha 2 vCPUs. Sem migração, sem aprendizado novo. Custo aumenta ~$8/mês. A latência para usuários BR continua alta, mas é o caminho mais rápido e barato.

### Cenário B — "Quero servidor no Brasil, custo agressivo, aceito contrato"
**→ Hostinger KVM 8 ($25.99/mês com 24 meses)**

32 GB RAM, 8 vCPU, 400 GB NVMe em São Paulo. Latência de 5ms para usuários BR. Custo ~$312/ano. O risco é lock-in de 2 anos e menor maturidade de suporte enterprise. Ideal para estágio atual do negócio.

### Cenário C — "Quero servidor no Brasil, sem contrato longo"
**→ Vultr High Performance São Paulo ($96–192/mês)**

Sem comprometimento de contrato. 8 vCPU, 16–32 GB RAM, NVMe em SP. Custo mensal flexível. 4–7x mais caro que Hostinger. Faz sentido se não quiser travar 2 anos ou se o negócio puder mudar de plano rapidamente.

### Cenário D — "Quero latência baixa BR e banco gerenciado, sem gerenciar infra"
**→ GCP e2-standard-4 + Cloud SQL São Paulo (~$488/mês)**

Tira toda a gestão de banco das suas mãos. Backups automáticos, failover, patching. Faz sentido quando o tempo gasto gerenciando banco vale mais que ~$460/mês de diferença vs auto-gerenciado. Considere quando tiver problemas frequentes com o PostgreSQL ou quando o sistema for crítico o suficiente para precisar de SLA.

### Cenário E — "Máxima confiabilidade, custo não é o principal critério"
**→ AWS EC2 + RDS São Paulo (~$624/mês, ou ~$380/mês com reserva de 1 ano)**

Infraestrutura enterprise-grade. RDS com Multi-AZ garante failover automático em < 2 min. Ideal para quando a API CNPJ for parte de um produto SaaS com clientes pagantes e SLA formal.

---

## Plano Sugerido (Progressivo)

```
AGORA (mai/2026)
  └─ Upgrade Contabo: VPS 30 (8 vCPU / 24 GB / 400 GB) → $15/mês
     ✓ Resolve disco (enche só em ~2027)
     ✓ Resolve RAM/swap
     ✗ Latência ainda alta para BR

3–6 MESES (jul–out 2026) — se a API tiver usuários ativos no Brasil
  └─ Migrar para Hostinger KVM 8 em São Paulo → $26/mês (24 meses)
     ✓ Latência 5ms para usuários BR
     ✓ Melhor hardware (NVMe vs SSD)
     ✓ Custo quase igual ao Contabo atual
     Processo: criar VPS → copiar dados → testar → trocar DNS

6–18 MESES (quando o negócio crescer)
  └─ Avaliar GCP/AWS se:
     - A API tiver SLA formal com clientes
     - Você precisar de réplica de leitura para não bloquear o ETL
     - O banco gerenciado se justificar pelo tempo economizado
```

---

## Opção 6 — Serverless / Pagar por Requisição *(sem VPS fixa)*

Em vez de pagar por uma máquina ligada 24/7, os modelos serverless cobram apenas pelo que você usa. Para a sua API CNPJ, os candidatos são **AWS Lambda**, **Google Cloud Run** e **Cloudflare Workers**.

> **Atenção:** serverless funciona bem para a **API** (stateless, requisições pontuais), mas o **banco de dados PostgreSQL** e o **worker ETL** ainda precisam de infraestrutura persistente.

---

### AWS Lambda + API Gateway (sa-east-1)

**Como funciona:** Cada requisição à API dispara um Lambda. Paga por execução e por GB-segundo de memória usada.

**Preços em sa-east-1:**
| Item | Preço |
|---|---|
| Primeiras 1M requisições/mês | **Grátis** |
| Acima de 1M: por 1M requisições | **$0.20** |
| Compute: por GB-segundo | **$0.00001667** |
| API Gateway (REST): por 1M chamadas | **$3.50** |

**Simulação de custo mensal por volume de requisições:**

| Requisições/mês | Lambda compute (512MB, 200ms/req) | API Gateway | **Total API** |
|---|---|---|---|
| 100 mil | ~$0 (free tier) | $0.35 | **~$0.35** |
| 500 mil | ~$0 (free tier) | $1.75 | **~$1.75** |
| 1 milhão | ~$0 (free tier) | $3.50 | **~$3.50** |
| 5 milhões | ~$0.85 | $17.50 | **~$18** |
| 10 milhões | ~$1.70 | $35.00 | **~$37** |
| 50 milhões | ~$8.50 | $175.00 | **~$184** |

> A partir de ~15–20 milhões de requisições/mês, uma VPS dedicada começa a ser mais barata.

**Banco de dados necessário mesmo assim:**
- RDS db.t3.medium (2 vCPU / 4 GB) em sa-east-1 ≈ **~$115/mês** (somente banco, sem app server)
- Ou: auto-gerenciado em VPS separada

**Total com Lambda + RDS:**

| Cenário | Lambda + API GW | RDS (banco) | Total |
|---|---|---|---|
| 500 mil req/mês | $1.75 | $115 | **~$117** |
| 5 milhões req/mês | $18 | $115 | **~$133** |
| 10 milhões req/mês | $37 | $115 | **~$152** |

**Prós Lambda:**
- Zero custo de API com baixo tráfego
- Escala automática — se viralizar, aguenta
- Sem gerenciamento de servidor para a API

**Contras Lambda:**
- **Cold start**: primeira requisição após ociosidade leva 500ms–2s (Python é mais lento)
- **PostgreSQL com Lambda** é problemático — cada invocação abre e fecha conexão; precisa de RDS Proxy (~$0.015/hora = +$11/mês) para não esgotar o pool
- O ETL mensal (worker de 2–3h rodando) **não é adequado para Lambda** (limite de 15 min por execução)
- Requer refatorar a API para handler Lambda (não roda FastAPI direto sem adaptador como Mangum)

---

### Google Cloud Run (southamerica-east1)

**Como funciona:** Roda containers Docker, escala para zero quando não há tráfego. Paga por CPU/RAM somente durante as requisições.

**Preços em southamerica-east1:**
| Item | Preço |
|---|---|
| CPU: por vCPU-segundo | $0.00002880 |
| Memória: por GB-segundo | $0.00000315 |
| Primeiras 2M requisições/mês | **Grátis** |
| Acima de 2M: por 1M requisições | $0.40 |

**Simulação de custo mensal (container 1 vCPU / 1 GB RAM, 200ms por req):**

| Requisições/mês | Compute | Requisições | **Total API** |
|---|---|---|---|
| 100 mil | ~$0.58 | $0 (free) | **~$0.58** |
| 500 mil | ~$2.88 | $0 (free) | **~$2.88** |
| 1 milhão | ~$5.76 | $0 (free) | **~$5.76** |
| 5 milhões | ~$28.80 | $1.20 | **~$30** |
| 10 milhões | ~$57.60 | $3.20 | **~$61** |

**Vantagem enorme do Cloud Run vs Lambda:** roda containers Docker **sem alteração de código**. Sua FastAPI atual pode ser deployada diretamente com um `Dockerfile`. Não precisa de adaptador.

**Banco de dados necessário:**
- Cloud SQL db-g1-small (1 vCPU / 1.7 GB) em SP ≈ **~$40/mês** (mínimo viável para teste)
- Cloud SQL db-custom-2-7680 (2 vCPU / 7.5 GB) ≈ **~$210/mês** (para produção com 70M linhas)

**Total com Cloud Run + Cloud SQL:**

| Cenário | Cloud Run | Cloud SQL (prod) | Total |
|---|---|---|---|
| 500 mil req/mês | $2.88 | $210 | **~$213** |
| 5 milhões req/mês | $30 | $210 | **~$240** |
| 10 milhões req/mês | $61 | $210 | **~$271** |

**Prós Cloud Run:**
- Roda Docker sem mudança de código
- Escala automática incluindo escala para zero
- Sem cold start severo (containers ficam "warm" com `min-instances=1` por ~$10/mês extra)
- O ETL worker pode rodar como Cloud Run Job (sem limite de 15 min, paga apenas durante execução)

**Contras Cloud Run:**
- Cloud SQL em São Paulo é caro (~$210/mês para produção)
- Múltiplos projetos (CRM, bot builder, etc.) ainda precisam de infraestrutura separada

---

### Comparativo: Serverless vs VPS para seu caso

| Modelo | Custo 500k req/mês | Custo 5M req/mês | Complexidade | BR Latência | ETL |
|---|---|---|---|---|---|
| **Contabo VPS 30** | $15 flat | $15 flat | Baixa | ❌ ~150ms | ✅ |
| **Hostinger KVM 8 (SP)** | $26 flat | $26 flat | Baixa | ✅ ~5ms | ✅ |
| **Lambda + RDS SP** | ~$117 | ~$133 | Alta | ✅ ~10ms | ❌ (limite 15min) |
| **Cloud Run + Cloud SQL SP** | ~$213 | ~$240 | Média | ✅ ~10ms | ✅ (Cloud Run Jobs) |
| **AWS EC2 + self-hosted PG SP** | $283 flat | $283 flat | Média | ✅ ~10ms | ✅ |

### Quando faz sentido serverless?

**Vale a pena se:**
- Tráfego muito irregular (dias sem uso, picos esporádicos)
- Você não quer gerenciar nenhum servidor
- Volume acima de 5–10M req/mês (o custo de VPS dedicada supera o serverless)
- Orçamento para Cloud SQL (~$210/mês só de banco)

**Não vale a pena se:**
- Tráfego constante e previsível (VPS é mais barato)
- Você precisa do worker ETL rodando por horas (Lambda não suporta)
- O banco é o maior custo de qualquer forma (Cloud SQL mínimo $210/mês)
- Você quer simplicidade de gestão (Docker Compose numa VPS é mais simples que serverless)

> **Para o seu caso atual:** o ETL de 2–3h inviabiliza Lambda. Cloud Run resolve o ETL via Jobs, mas o custo do Cloud SQL em SP ($210+/mês) torna o modelo mais caro que uma VPS. A menos que o volume de requisições ultrapasse 10M/mês, uma **VPS dedicada em São Paulo continua sendo a opção mais econômica**.

---

## Nota sobre Migração de Dados

Migrar 96 GB de PostgreSQL não é trivial mas é bem documentado:

```bash
# Na VPS atual — dump do banco
pg_dump -h localhost -U etl_user -d cnpj_db -Fc -f cnpj_db.dump  # ~5–10 GB comprimido
pg_dump -h localhost -U etl_user -d etl_db  -Fc -f etl_db.dump

# Transferir para novo servidor
rsync -avz --progress cnpj_db.dump usuario@novo-servidor:/tmp/

# No novo servidor — restaurar
pg_restore -h localhost -U etl_user -d cnpj_db -j 4 /tmp/cnpj_db.dump
```

Tempo estimado: 2–4h para dump, 4–8h para restore (depende do hardware do destino).
Downtime: pode usar replicação lógica para zero-downtime, ou aceitar janela de manutenção de 4–6h.

---

---

## Ideias de Solução — Arquiteturas Alternativas

Além de simplesmente "trocar de VPS", existem abordagens arquiteturais que podem resolver problemas específicos de forma mais inteligente.

---

### Ideia 1 — Separar o Banco de Dados da Máquina dos Apps

**Problema que resolve:** O PostgreSQL com 70M linhas consome 96 GB de disco e 1.2 GB de RAM, crescendo mês a mês. Compartilha a mesma máquina com APIs, workers e outros projetos, criando risco: se a máquina travar, tudo para.

**Como funciona:**
```
VPS pequena (apps)        Servidor dedicado de banco
├── cnpj_api              └── PostgreSQL 16
├── cnpj_worker                ├── cnpj_db (70M linhas)
├── implementation-api         └── etl_db (CRM)
├── bot-builder
└── outros containers
```

**Opções para o banco separado:**
- **Hetzner Dedicated Root Server** (AX41-NVMe): 6 cores / 64 GB RAM / 2×512 GB NVMe por ~€50/mês (~$55). Excelente para PostgreSQL pesado. Sem Brazil mas banco não precisa estar no Brasil se a API estiver.
- **Cloud SQL mínimo em SP** (2 vCPU / 7.5 GB): ~$210/mês gerenciado.
- **Neon.tech** (PostgreSQL serverless): paga por compute ativo. Plano free tem 0.5 vCPU, 1 GB RAM. Scale plan a partir de $19/mês com autoscaling. Não tem região Brasil ainda, mas tem US-East com ~80ms de SP.

**Benefício:** A VPS dos apps pode ser pequena e barata. O banco escala independente. Backups do banco isolados dos apps.

---

### Ideia 2 — CDN + Cache na Borda para a API CNPJ

**Problema que resolve:** ~180ms de latência para usuários BR porque o servidor está na Europa. A maioria das consultas CNPJ é para os mesmos 5–10% de CNPJs mais acessados (grandes empresas, fornecedores comuns).

**Como funciona:**
```
Usuário BR → Cloudflare Edge (SP) → cache hit → resposta em <5ms
                                   → cache miss → VPS Europa → resposta em ~200ms
```

**Ferramentas:**
- **Cloudflare Workers + KV**: grátis para 100k req/dia. Cached responses ficam em edge nodes em SP. Você define quais rotas cachear (ex: `/cnpj/{cnpj}` com TTL de 24h — dados da RF mudam só mensalmente).
- **Cloudflare Cache Rules**: sem código, só configuração no painel. Cacheia respostas GET da API na borda.
- **Redis na borda com Upstash**: banco Redis serverless com edge nodes globais. Plano free: 10k comandos/dia. $0.20 por 100k comandos acima disso. Permite armazenar respostas de CNPJs frequentes nos edge nodes mais próximos do usuário.

**Custo adicional:** $0–20/mês dependendo do volume.
**Impacto:** Para CNPJs que já estão no cache da borda, resposta cai de ~200ms para ~5ms sem mover o servidor. Para CNPJs novos, continua ~200ms.

**Limitação:** Cache de borda só funciona para leituras (GET). O ETL e operações de escrita não são afetados.

---

### Ideia 3 — Separar o Discador Asterisk para Máquina Própria

**Problema que resolve:** O Asterisk está consumindo 225% de CPU (2+ cores dos seus 6 vCPUs), prejudicando todos os outros serviços. VoIP com transcodificação de áudio é CPU-intensivo por natureza.

**Como funciona:**
```
VPS atual (sem Discador)        VPS Nova (só Discador)
├── cnpj_api                    └── projeto-discador-asterisk
├── cnpj_worker                 └── projeto-discador-interface
├── implementation-*             (VPS barata, sem necessidade de BR)
└── outros

Asterisk rodando isolado = sem competir com banco e APIs
```

**Opção de VPS para o Discador:**
- **Contabo Cloud VPS S**: 4 vCPU / 8 GB / 100 GB = ~€4/mês (~$5). Mais que suficiente para Asterisk isolado.
- O Discador não precisa de banco de dados grande nem de NVMe — apenas CPU para transcodificação de áudio.

**Benefício imediato:** Liberaria ~225% de CPU na VPS principal. Todos os outros serviços ganhariam performance imediatamente. Custo adicional: ~$5/mês.

---

### Ideia 4 — Desligar o ETL-HML quando não estiver em uso

**Problema que resolve:** O ambiente de homologação (`etl-hml-*`) está consumindo ~32% de CPU idle sem justificativa.

**Como funciona:** Criar um script simples que liga/desliga o stack quando necessário:

```bash
# Desligar (quando não estiver testando)
docker compose -f /opt/etl-hml/docker-compose.yml stop

# Ligar (quando for usar)
docker compose -f /opt/etl-hml/docker-compose.yml start
```

Ou via Coolify: pausar o ambiente com um clique.

**Benefício:** ~32% de CPU liberados instantaneamente. Sem custo adicional. Sem mudança de arquitetura.

---

### Ideia 5 — Réplica de Leitura para a API CNPJ

**Problema que resolve:** O ETL mensal faz carga pesada no PostgreSQL por 2–3h. Durante esse período, as queries da API CNPJ competem com o `COPY` e o `REFRESH MATERIALIZED VIEW`, causando lentidão.

**Como funciona:**
```
ETL Worker → escrita → PostgreSQL Primary (VPS principal)
                              ↓ replicação
API CNPJ → leitura → PostgreSQL Replica (VPS secundária menor)
```

**Opção simples:** Hetzner CX22 (2 vCPU / 4 GB / 40 GB) ~€4/mês com streaming replication. A API lê da réplica, o ETL escreve no primary. Durante o carregamento mensal, a API não percebe degradação.

**Custo adicional:** ~$5–10/mês de infra + configuração de streaming replication (~2h de setup).

---

### Ideia 6 — Particionamento do PostgreSQL por run_key

**Problema que resolve:** Com 70M linhas sem particionamento, as queries fazem full scan em todas as tabelas. Cada mês de dados novo incrementa o custo de todas as queries existentes.

**Como funciona:** Particionar `rf_estabelecimentos` por `run_key` (mês de carga):
```sql
-- Cada mês vira uma partição separada
CREATE TABLE cnpj.rf_estabelecimentos PARTITION BY LIST (run_key);
CREATE TABLE cnpj.rf_estabelecimentos_2026_05 PARTITION OF cnpj.rf_estabelecimentos
    FOR VALUES IN ('2026-05');
```

**Benefício:** Queries com filtro por `run_key` escaneiam apenas a partição do mês corrente. O drop de dados antigos vira `DROP TABLE` (instantâneo) em vez de `DELETE` (lento).

**Custo:** Zero. É configuração de banco.

**Limitação:** Requer refatorar o pipeline de carga (o `load_step` precisaria criar a partição antes do `COPY`). Médio prazo.

---

### Ideia 7 — Backup Automatizado para Object Storage

**Problema que resolve:** Atualmente não há indicação de backup automático do PostgreSQL. Se a VPS falhar, 96 GB de dados (histórico de CNPJ + CRM) se perdem.

**Como funciona:** `pg_dump` diário comprimido para armazenamento externo barato:

```bash
# Cron diário — dump comprimido para Backblaze B2 (mais barato que S3)
pg_dump -U etl_user cnpj_db | gzip | rclone rcat b2:backups/cnpj_db-$(date +%Y%m%d).gz
```

**Custo de armazenamento:**
- **Backblaze B2**: $0.006/GB-mês. 10 GB de dump comprimido × 30 dias = ~$1.80/mês
- **Cloudflare R2**: $0.015/GB-mês com egress gratuito. 10 GB × 30 dias = ~$4.50/mês
- **AWS S3 sa-east-1**: $0.023/GB-mês. 10 GB × 30 dias = ~$6.90/mês

**Benefício:** Proteção contra falha de disco, ransomware ou erro humano. Custo: ~$2–7/mês.

---

### Resumo das Ideias

| # | Ideia | Problema resolve | Complexidade | Custo adicional | Impacto |
|---|---|---|---|---|---|
| 1 | Separar banco dos apps | Disco e RAM crescentes | Média | $5–55/mês | Alto |
| 2 | CDN/cache de borda | Latência para usuários BR | Baixa | $0–20/mês | Alto para CNPJs frequentes |
| 3 | Isolar Discador | 225% CPU roubados | Baixa | ~$5/mês | **Imediato e alto** |
| 4 | Desligar ETL-HML | 32% CPU idle | Mínima | $0 | Imediato, médio |
| 5 | Réplica de leitura | ETL degrada API | Média | $5–10/mês | Alto no ETL |
| 6 | Particionamento PG | Performance queries crescente | Média | $0 | Médio prazo |
| 7 | Backup automatizado | Sem proteção de dados | Baixa | $2–7/mês | Crítico de segurança |

**Ação de maior impacto com menor custo:** Ideia 3 (isolar Discador, ~$5/mês) + Ideia 4 (desligar ETL-HML, $0) + Ideia 7 (backup, ~$2/mês). Juntas, liberam ~250% de CPU e protegem os dados por ~$7/mês a mais.

---

*Fontes: Contabo pricing page, Sparecores (AWS sa-east-1), GCloud-Compute.com (GCP), Hetzner Cloud pricing, Vultr São Paulo datacenter, Hostinger Brazil datacenter announcement, DigitalOcean regional availability docs.*
*Preços verificados em maio/2026 — podem variar. Sempre confirmar na página oficial antes de contratar.*
