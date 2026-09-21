from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent / ".env"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8")

    # PostgreSQL
    postgres_url: str = "postgresql://user:pass@localhost:5432/cnpj_db"

    # Paths
    base_dir: Path = Path(__file__).parent
    data_dir: Path = Path(__file__).parent / "data"
    log_dir: Path = Path(__file__).parent / "logs"
    checkpoint_dir: Path = Path(__file__).parent / "checkpoints"
    control_db: Path = Path(__file__).parent / "pipeline_control.db"
    status_file: Path = Path(__file__).parent / "ultimo_status.json"

    # Download source
    webdav_share_url: str = "https://arquivos.receitafederal.gov.br/index.php/s/YggdBLfdninEJX9"
    webdav_token: str = ""
    download_timeout: int = 300
    download_chunk_size: int = 1024 * 1024  # 1 MB
    max_download_retries: int = 5

    # Pipeline
    max_job_attempts: int = 3
    lease_seconds: int = 3600
    heartbeat_interval: int = 30

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_workers: int = 8
    db_pool_min: int = 10
    db_pool_max: int = 50
    db_pool_timeout: float = 10.0   # segundos para desistir de esperar conexão do pool

    # Cache
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl: int = 2592000        # 30 dias — lookup de CNPJ individual
    cache_stale_ttl: int = 7776000  # 90 dias — fallback stale quando banco falha
    search_cache_ttl: int = 300     # 5min — resultados de busca

    # Rate limiting (requisições por minuto por IP)
    rate_limit_cnpj: str = "2000/minute"
    rate_limit_search: str = "1000/minute"

    # Segurança — API Key enviada pelo CRM no header X-API-Key
    # Deixe vazio para desabilitar (desenvolvimento)
    api_key: str = ""

    # Timeout de query no banco (ms) — evita query lenta travar o pool
    db_query_timeout_ms: int = 8000

    # Index step — disk safety and performance
    index_min_free_gb: int = 50       # mínimo de espaço livre antes do REFRESH CONCURRENTLY
    index_work_mem: str = "256MB"     # work_mem da sessão durante o REFRESH
    index_parallel_workers: int = 0   # 0 = sem paralelismo — evita alocação de /dev/shm (64MB limite Docker)

    # Scheduler — checagem automática diária da RF
    scheduler_enabled: bool = True
    scheduler_hour: int = 3                      # hora-alvo (0-23), no fuso scheduler_tz
    scheduler_tz: str = "America/Sao_Paulo"
    scheduler_poll_seconds: int = 1800           # 30 min entre polls

    # Postgres schemas (banco corporativo compartilhado — nunca usar "public")
    pg_schema: str = "cnpj"                  # dados finais tratados
    pg_staging_schema: str = "cnpj_staging"  # staging UNLOGGED (COPY rápido)
    pg_serving_schema: str = "cnpj_serving"  # materialized views para API
    pg_socio_schema: str = "socio"           # derivados de vínculo societário (migration 008)

    # ── Job socio_empresas — empresas irmãs por CNPJ ────────────────────────
    # A lista de CNPJs a processar NÃO mora neste banco: são os leads da
    # carteira, que vivem na tabela data_base do Supabase do CRM. Por isso o job
    # precisa de credencial de leitura de lá — é a única coisa que este projeto
    # busca fora da Receita.
    #
    # O nome SUPABASE_SERVICE_KEY é o mesmo que o mb-crm usa — de propósito, para
    # ser a variável que a pessoa já conhece em vez de mais um apelido.
    #
    # RESSALVA: a service key ignora RLS por completo. O job só faz SELECT em
    # data_base, então uma chave com leitura restrita a essa tabela bastaria e
    # seria bem menos perigosa nesta máquina, que é a do ETL da Receita e não
    # tinha credencial do CRM até agora.
    socio_job_enabled: bool = True
    socio_job_hour: int = 4                 # 4h: depois do scheduler da RF (3h)
    socio_job_tz: str = "America/Sao_Paulo"
    socio_job_poll_seconds: int = 1800
    socio_job_dias: int = 45                # revisita linha mais velha que isto
    socio_job_lote: int = 2000              # CNPJs por chamada de calcular_lote
    socio_job_max_lista: int = 100          # teto de CNPJs no campo cnpjs_irmas
    supabase_url: str = ""                  # ex.: https://xxxx.supabase.co
    supabase_service_key: str = ""          # chave com leitura em data_base
    supabase_base_table: str = "data_base"
    supabase_base_coluna: str = "CD_CPF_CNPJ_CLIENTE"

    # Arquivos a baixar (palavras-chave no nome do arquivo)
    # Portes removido: RF parou de publicar em 2025-12 (tabela mantida no schema por compatibilidade)
    wanted_files: list[str] = [
        "Empresas", "Estabelecimentos", "Socios", "Simples",
        "Cnaes", "Municipios", "Naturezas",
        "Qualificacoes", "Motivos", "Paises",
    ]

    def ensure_dirs(self) -> None:
        for d in [self.data_dir, self.log_dir, self.checkpoint_dir]:
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
