#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Configura a VPS Contabo M do zero e sobe o projeto CNPJ
#
# Uso: bash deploy.sh
# Requisitos: Ubuntu 22.04/24.04, usuário com sudo
# =============================================================================
set -euo pipefail

PROJECT_DIR="$HOME/cnpj"
PG_VERSION=16
PG_DB=cnpj_db
PG_USER=cnpj_user

# ── Cores ─────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# =============================================================================
# 1. Atualização do sistema
# =============================================================================
info "Atualizando pacotes..."
sudo apt-get update -qq
sudo apt-get upgrade -y -qq

# =============================================================================
# 2. Docker
# =============================================================================
if ! command -v docker &>/dev/null; then
    info "Instalando Docker..."
    curl -fsSL https://get.docker.com | sudo bash
    sudo usermod -aG docker "$USER"
    info "Docker instalado. ATENÇÃO: faça logout/login para o grupo docker ter efeito."
else
    info "Docker já instalado: $(docker --version)"
fi

if ! command -v docker compose &>/dev/null 2>&1; then
    info "Instalando Docker Compose plugin..."
    sudo apt-get install -y docker-compose-plugin
fi

# =============================================================================
# 3. PostgreSQL $PG_VERSION
# =============================================================================
if ! command -v psql &>/dev/null; then
    info "Instalando PostgreSQL $PG_VERSION..."
    sudo apt-get install -y "postgresql-$PG_VERSION" "postgresql-contrib-$PG_VERSION"
    sudo systemctl enable postgresql
    sudo systemctl start postgresql
else
    info "PostgreSQL já instalado: $(psql --version)"
fi

# =============================================================================
# 4. Configurar PostgreSQL para aceitar conexões do Docker
# =============================================================================
info "Configurando PostgreSQL..."

PG_HBA="/etc/postgresql/$PG_VERSION/main/pg_hba.conf"
PG_CONF="/etc/postgresql/$PG_VERSION/main/postgresql.conf"

# Escutar em todas as interfaces (necessário para host.docker.internal)
if ! grep -q "^listen_addresses = '\*'" "$PG_CONF" 2>/dev/null; then
    sudo sed -i "s/^#listen_addresses = 'localhost'/listen_addresses = '*'/" "$PG_CONF"
    sudo sed -i "s/^listen_addresses = 'localhost'/listen_addresses = '*'/" "$PG_CONF"
fi

# Permitir conexão da rede Docker (172.17.0.0/16) e Docker Compose (172.18.0.0/16)
if ! grep -q "172.17.0.0/16" "$PG_HBA" 2>/dev/null; then
    echo "host    $PG_DB    $PG_USER    172.17.0.0/16    scram-sha-256" | sudo tee -a "$PG_HBA"
    echo "host    $PG_DB    $PG_USER    172.18.0.0/16    scram-sha-256" | sudo tee -a "$PG_HBA"
    echo "host    $PG_DB    $PG_USER    172.19.0.0/16    scram-sha-256" | sudo tee -a "$PG_HBA"
fi

sudo systemctl reload postgresql

# =============================================================================
# 5. Criar banco e usuário (pede senha interativamente)
# =============================================================================
info "Criando banco de dados e usuário PostgreSQL..."
read -rsp "Digite a senha para o usuário PostgreSQL '$PG_USER': " PG_PASS
echo

sudo -u postgres psql <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$PG_USER') THEN
        CREATE ROLE $PG_USER LOGIN PASSWORD '$PG_PASS';
    END IF;
END
\$\$;
CREATE DATABASE $PG_DB OWNER $PG_USER ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
GRANT ALL PRIVILEGES ON DATABASE $PG_DB TO $PG_USER;
SQL

sudo -u postgres psql -d "$PG_DB" <<SQL
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE SCHEMA IF NOT EXISTS cnpj_serving AUTHORIZATION $PG_USER;
SQL

info "Banco criado: $PG_DB / usuário: $PG_USER"

# =============================================================================
# 6. Clonar / atualizar projeto
# =============================================================================
if [[ -d "$PROJECT_DIR/.git" ]]; then
    info "Atualizando projeto em $PROJECT_DIR..."
    git -C "$PROJECT_DIR" pull
else
    warn "Copie os arquivos do projeto para $PROJECT_DIR e execute este script novamente."
    warn "Ou use: scp -r ./Baixar_Ncpj user@VPS_IP:$PROJECT_DIR"
    mkdir -p "$PROJECT_DIR"
fi

# =============================================================================
# 7. Criar .env a partir do template
# =============================================================================
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    if [[ -f "$PROJECT_DIR/.env.production" ]]; then
        cp "$PROJECT_DIR/.env.production" "$PROJECT_DIR/.env"
        # Substituir senha no .env
        sed -i "s/TROQUE_A_SENHA/$PG_PASS/" "$PROJECT_DIR/.env"
        warn "ATENÇÃO: gere uma API_KEY segura e edite $PROJECT_DIR/.env"
        warn "Comando: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    else
        error ".env.production não encontrado em $PROJECT_DIR"
    fi
else
    info ".env já existe, pulando criação."
fi

# =============================================================================
# 8. Firewall (ufw)
# =============================================================================
info "Configurando firewall..."
sudo ufw --force enable
sudo ufw allow ssh
sudo ufw allow 80/tcp      # HTTP (nginx)
sudo ufw deny  8000/tcp    # FastAPI nunca exposto diretamente
sudo ufw deny  5432/tcp    # PostgreSQL nunca exposto para internet
sudo ufw status

# =============================================================================
# 9. Subir containers
# =============================================================================
if [[ -f "$PROJECT_DIR/docker-compose.yml" ]]; then
    info "Subindo containers..."
    cd "$PROJECT_DIR"
    docker compose pull
    docker compose up -d --build
    docker compose ps
else
    warn "docker-compose.yml não encontrado em $PROJECT_DIR. Suba manualmente com 'make up'."
fi

# =============================================================================
# 10. Cron mensal para atualização ETL (executado no host)
# =============================================================================
CRON_JOB="0 3 1 * * cd $PROJECT_DIR && docker compose exec -T worker python -c \"from worker import enqueue_full_run; import asyncio; asyncio.run(enqueue_full_run())\" >> /var/log/cnpj_etl.log 2>&1"

if ! crontab -l 2>/dev/null | grep -q "cnpj_etl"; then
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    info "Cron mensal configurado: todo dia 1 às 03:00"
else
    info "Cron já configurado."
fi

# =============================================================================
info "Deploy concluído!"
echo ""
echo "  Próximos passos:"
echo "  1. Edite $PROJECT_DIR/.env e defina API_KEY"
echo "  2. Execute a primeira carga: cd $PROJECT_DIR && make enqueue"
echo "  3. Acompanhe logs: make logs"
echo "  4. Teste a API: curl http://SEU_IP_VPS/health"
