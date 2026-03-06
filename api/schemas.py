from typing import Optional
from pydantic import BaseModel


class CNPJResponse(BaseModel):
    cnpj_completo: str
    cnpj_basico: str
    razao_social: Optional[str] = None
    nome_fantasia: Optional[str] = None
    natureza_juridica: Optional[str] = None
    natureza_juridica_descricao: Optional[str] = None
    porte: Optional[str] = None
    porte_descricao: Optional[str] = None
    capital_social: Optional[str] = None
    situacao_cadastral: Optional[str] = None
    motivo_situacao_descricao: Optional[str] = None
    data_inicio_atividade: Optional[str] = None
    cnae_fiscal: Optional[str] = None
    cnae_fiscal_descricao: Optional[str] = None
    cnae_fiscal_secundaria: Optional[str] = None
    tipo_logradouro: Optional[str] = None
    logradouro: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cep: Optional[str] = None
    uf: Optional[str] = None
    municipio: Optional[str] = None
    municipio_descricao: Optional[str] = None
    ddd1: Optional[str] = None
    telefone1: Optional[str] = None
    correio_eletronico: Optional[str] = None
    identificador_matriz_filial: Optional[str] = None
    situacao_especial: Optional[str] = None
    data_situacao_especial: Optional[str] = None
    nm_cidade_exterior: Optional[str] = None
    pais: Optional[str] = None
    ddd2: Optional[str] = None
    telefone2: Optional[str] = None
    ddd_fax: Optional[str] = None
    fax: Optional[str] = None
    qualificacao_responsavel: Optional[str] = None
    ente_federativo_responsavel: Optional[str] = None
    data_situacao_cadastral: Optional[str] = None
    # Simples Nacional / MEI
    opcao_pelo_simples: Optional[str] = None    # "S" = optante, "N" = não optante
    data_opcao_simples: Optional[str] = None
    data_exclusao_simples: Optional[str] = None
    opcao_pelo_mei: Optional[str] = None        # "S" = MEI, "N" = não MEI
    data_opcao_mei: Optional[str] = None
    data_exclusao_mei: Optional[str] = None
    run_key: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    version: str = "1.0.0"
    last_success_run_key: Optional[str] = None
    mv_row_count: Optional[int] = None


class RunRecord(BaseModel):
    job_id: str
    run_key: str
    status: str
    attempts: int
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_error: Optional[str] = None
